"""
The generic MCP adapter: one stdio server that puts any MCP-capable agent on the bus.

Responsibility: be a peer on behalf of a host that has no plugin API. The host spawns this as an
MCP server, so this process owns the socket and the registry entry under its own pid, which is
the identity Claude verifies through the connecting socket's peer credentials. Kiro, Codex,
Gemini CLI, Cursor and anything else that speaks MCP all get messaging from this one file; hosts
with a real plugin API (pi, opencode) use their own adapter instead and can push into a live turn.

What this cannot do: interrupt the host. There is no MCP call that injects into a running turn,
so inbound messages go to the spool and reach the model either through a host hook
(`agentbus drain`) or when it calls check_messages. That is the one behavioural difference from a
native adapter, and it is why the tool description tells the model to check.

MCP is hand-rolled because the surface a host actually uses is three methods over
newline-delimited JSON-RPC, and a dependency would defeat running on the python that is already
installed. stdout is the RPC channel, so every diagnostic goes to stderr.

The host identifies itself in `initialize`, which is what picks the name prefix, so registration
waits for that first call rather than happening at import.

Related: protocol.py (wire), spool.py (inbox), cli.py (`agentbus mcp` and `agentbus drain`).
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import socket as socket_mod
import sys
import threading
from typing import Callable, Dict, List, Optional

from . import protocol as p
from . import spool

# Host name reported in MCP initialize -> the prefix its sessions are addressed by. An unknown
# host still works, it just lands under the generic "ab-".
HOST_PREFIXES = {
    # Kiro reports its Amazon Q heritage rather than its own name, verified against 2.24.1.
    "q dev cli": "ki-",
    "kiro-cli": "ki-",
    "kiro": "ki-",
    # Verified against codex-cli 0.155.1: it reports "codex-mcp-client", not "codex" or "codex-cli".
    "codex-mcp-client": "cx-",
    "codex": "cx-",
    "codex-cli": "cx-",
    "gemini-cli": "gm-",
    "gemini": "gm-",
    "cursor": "cu-",
    "cursor-cli": "cu-",
    # Verified against antigravity-cli 1.2.11: it reports "antigravity-client".
    "antigravity-client": "ag-",
    "antigravity": "ag-",
}
DEFAULT_PREFIX = "ab-"
PROTOCOL_VERSION = "2025-06-18"


def log(message: str) -> None:
    sys.stderr.write(f"[agentbus] {message}\n")
    sys.stderr.flush()


class Server:
    def __init__(self, name_override: Optional[str] = None) -> None:
        self.pid = os.getpid()
        self.sock = str(p.sock_path(self.pid))
        self.name_override = name_override or os.environ.get("AGENTBUS_NAME")
        self.listener: Optional[socket_mod.socket] = None
        self.entry: Optional[dict] = None
        self.last_from: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ bus membership

    def choose_name(self, prefix: str, wanted: Optional[str]) -> str:
        taken = [e["name"] for e in p.read_registry() if e["pid"] != self.pid]
        if wanted:
            cleaned = p.NAME_SAFE.sub("-", wanted.strip()).strip("-")
            base = cleaned if cleaned.startswith(prefix) else prefix + cleaned
            if base not in taken:
                return base
            n = 2
            while f"{base}-{n}" in taken:
                n += 1
            return f"{base}-{n}"
        return p.derive_name(os.getcwd(), None, taken, prefix)

    def join(self, host: str) -> None:
        """Register and start listening. Idempotent: a host that initializes twice gets one entry."""
        with self._lock:
            if self.listener is not None:
                return
            prefix = HOST_PREFIXES.get(host.lower(), DEFAULT_PREFIX)
            token = p.new_token()
            self.listener = p.listen(self.sock, token, self.on_message, log)
            p.write_key(self.pid, self.sock, token)
            entry = p.new_registry(
                self.pid, self.choose_name(prefix, self.name_override), os.getcwd(),
                "agentbus", f"agentbus-{self.pid}",
            )
            entry["version"] = host or "agentbus"
            p.write_registry(entry)
            self.entry = entry
            log(f"registered as {entry['name']} on {self.sock} (host: {host or 'unknown'})")

    def leave(self) -> None:
        with self._lock:
            if self.listener is not None:
                try:
                    self.listener.close()
                except OSError:
                    pass
                self.listener = None
            p.remove_files(self.pid, self.sock)
            spool.remove(self.pid)
            self.entry = None

    def on_message(self, message: dict) -> None:
        self.last_from = message.get("from")
        spool.append(self.pid, {
            "from": message.get("from"),
            "fromName": message.get("fromName"),
            "body": message.get("body", ""),
        })
        log(f"message from {message.get('fromName') or message.get('from') or '?'} spooled")

    # ------------------------------------------------------------------ tools

    def tools(self) -> List[dict]:
        return [
            {
                "name": "list_claude_sessions",
                "description": "List live coding-agent sessions on this machine that can receive messages (name, status, working directory).",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "send_to_claude",
                "description": (
                    "Send a message to another live session on this machine: a Claude Code session "
                    "(cc-<name>), or another agent (pi-, oc-, ki-, cx-, gm-). Its reply arrives in "
                    "your next check_messages. Omit `to` to answer whoever messaged this session last."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Session name from list_claude_sessions, or a uds: address. Default: last sender."},
                        "message": {"type": "string", "description": "The message body"},
                    },
                    "required": ["message"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "check_messages",
                "description": (
                    "Read and clear messages other sessions sent to this one. Nothing else surfaces "
                    "them in this host, so call it at the start of a turn when you are expecting a reply."
                ),
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "set_session_name",
                "description": "Rename this session as other agents see it. Use it when the user asks to name or rename this session.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "Short name; the host's prefix is added if missing."}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        ]

    def call(self, name: str, args: Dict) -> dict:
        def text(body: str, is_error: bool = False) -> dict:
            out = {"content": [{"type": "text", "text": body}]}
            if is_error:
                out["isError"] = True
            return out

        if name == "list_claude_sessions":
            rows = [
                f"{p.display_name(e)}  [{e.get('status') or '?'}]  {e.get('cwd', '')}"
                for e in p.peers(self.pid)
            ]
            return text("\n".join(rows) or "(no live peer sessions)")

        if name == "check_messages":
            messages = spool.drain(self.pid)
            return text("\n\n".join(spool.render(m) for m in messages) or "(no new messages)")

        if name == "set_session_name":
            if self.entry is None:
                return text("Not on the bus, so there is no name to change.", True)
            wanted = str(args.get("name") or "").strip()
            if not wanted:
                return text("`name` is required.", True)
            was = self.entry["name"]
            prefix = was.split("-", 1)[0] + "-"
            self.entry["name"] = self.choose_name(prefix, wanted)
            self.entry["nameSource"] = "user"
            try:
                p.write_registry(self.entry)
            except OSError as e:
                self.entry["name"] = was
                return text(f"Rename failed: {e}", True)
            return text(f"This session is now {self.entry['name']} (was {was}).")

        if name == "send_to_claude":
            body = args.get("message")
            if not isinstance(body, str) or not body.strip():
                return text("`message` is required.", True)
            to = str(args.get("to") or "").strip() or self.last_from
            if not to:
                return text("No `to` given and nobody has messaged this session yet.", True)
            target = p.resolve_target(to, self.pid)
            if "error" in target:
                return text(target["error"], True)
            if self.entry is None:
                return text("Not on the bus, so a reply could not come back. Restart the host.", True)
            try:
                msg_id = p.send_user(target["sock"], self.sock, self.entry["name"], body)
            except OSError as e:
                return text(f"Send to {target['name']} failed: {e}", True)
            return text(f"Sent to {target['name']} (msg {msg_id}). Its reply arrives in your next check_messages.")

        return text(f"Unknown tool: {name}", True)

    # ------------------------------------------------------------------ JSON-RPC

    def handle(self, message: dict) -> Optional[dict]:
        if "id" not in message:
            return None  # a notification, including notifications/initialized
        method = message.get("method")
        if method == "initialize":
            params = message.get("params") or {}
            host = str((params.get("clientInfo") or {}).get("name") or "")
            try:
                self.join(host)
            except Exception as e:  # a bus we cannot join must not stop the host starting
                log(f"could not join the bus: {e}")
            requested = params.get("protocolVersion")
            return {
                "jsonrpc": "2.0", "id": message["id"],
                "result": {
                    "protocolVersion": requested if isinstance(requested, str) else PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agentbus", "version": "0.1.0"},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": self.tools()}}
        if method == "tools/call":
            params = message.get("params") or {}
            return {
                "jsonrpc": "2.0", "id": message["id"],
                "result": self.call(params.get("name", ""), params.get("arguments") or {}),
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        return {
            "jsonrpc": "2.0", "id": message["id"],
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def serve(name_override: Optional[str] = None, stdin=None, stdout=None) -> int:
    server = Server(name_override)
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    # A host that ends the session by killing this process rather than closing stdin (codex does,
    # confirmed: its registry files were still there after the process was gone) never reaches the
    # `finally` below, so the socket, key and registry entry are left behind forever. leave() is
    # idempotent, so both paths firing is harmless.
    atexit.register(server.leave)

    def on_signal(signum: int, _frame) -> None:
        server.leave()
        os._exit(0)  # a signal handler must not rely on unwinding back into the stdin loop

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, on_signal)

    try:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                log("dropped non-JSON frame from the host")
                continue
            try:
                response = server.handle(message)
            except Exception as e:  # one bad call must not take the server down mid-session
                log(f"handler failed: {e}")
                continue
            if response is not None:
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()
    finally:
        server.leave()
    return 0

"""
The wire protocol that lets non-Claude agents join Claude Code's local session bus.

Responsibility: everything about talking to a Claude Code session on this machine, and nothing
about any particular agent. The per-agent adapters (MCP server, pi extension, opencode plugin)
sit on top; the CLI drives it from a shell. Nothing here imports an agent SDK.

Where it fits: Claude Code sessions discover each other through three things, all undocumented
and all reproduced here.

  1. A registry file, ~/.claude/sessions/<pid>.json, holding the session's name, cwd, status and
     socket path. Claude's ListAgents and its `@name` prompt autocomplete read this directory, so
     writing a well-formed entry is what makes an agent appear as a peer.
  2. A unix socket at /tmp/cc-socks/<pid>.sock, owned by the process that the entry names.
  3. An auth token in ~/.claude/sessions/<pid>.<sha256 of socket path>.key, which a sender must
     present before its frames are accepted.

Constraints that came out of reading the claude binary, none of them guessable, all of them
load-bearing:

  - Frames are newline-delimited JSON. `{"type":"auth","token":...}` first, then
    `{"type":"user",...}` whose message content is an XML-ish envelope (see build_envelope).
  - `procStart` must equal `LC_ALL=C TZ=UTC ps -o lstart= -p <pid>` byte for byte. Claude compares
    it as a string to decide whether a registry entry belongs to a live process or a recycled pid.
  - The envelope's from-mode must match the RECEIVER's permission class or the message is parked
    as "held" pending its user's approval. See peer_mode.
  - The sockets directory must be mode 0700 and the socket 0600, or Claude refuses to use it.
  - Claude holds a connection ~150ms after writing before closing; we do the same, since closing
    immediately can truncate the last frame on macOS.

This file is pure stdlib and targets Python 3.9, the version macOS still ships, so that agentbus
never asks anyone to install a runtime.

Related: cli.py (install/list/send), mcp.py (the generic MCP adapter), tests/vectors.json (the
cases this and the TypeScript adapters are both checked against).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

SOCK_DIR = Path("/tmp/cc-socks")
SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
TAG = "cross-session-message"
PEER_PROTOCOL = 1

# Claude keeps the connection open this long after writing before ending it.
LINGER_S = 0.15
MAX_FRAME = 1 << 20
# Enough of a transcript to find the latest permissionMode without reading a multi-megabyte file.
MODE_TAIL_BYTES = 256 * 1024

# Agents that put their own prefix in the registry name. Anything else in the registry is Claude.
PREFIXED_AGENTS = {"pi", "opencode", "kiro", "agentbus"}

NAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
SOCK_NAME = re.compile(r"^(\d+)(?:-[0-9a-f]{8})?\.sock$")
UUID_RE = re.compile(r"^[0-9a-f-]{36}$")
ENVELOPE_RE = re.compile(
    r"^<" + TAG + r"((?:\s+[a-z-]+=\"[^\"]*\")*)>\n(.*)\n</" + TAG + r">$", re.DOTALL
)
ATTR_RE = re.compile(r"([a-z-]+)=\"([^\"]*)\"")
# Claude writes compact JSON today; tolerate whitespace so a future pretty-printed transcript
# does not silently stop matching and downgrade every peer to prompting.
MODE_RE = re.compile(r'"permissionMode"\s*:\s*"([a-zA-Z]+)"')


# ---------------------------------------------------------------- paths and identity


def sock_path(pid: int) -> Path:
    return SOCK_DIR / f"{pid}.sock"


def key_path(pid: int, sock: str) -> Path:
    digest = hashlib.sha256(str(sock).encode()).hexdigest()
    return SESSIONS_DIR / f"{pid}.{digest}.key"


def registry_path(pid: int) -> Path:
    return SESSIONS_DIR / f"{pid}.json"


def pid_of_socket(sock: str) -> Optional[int]:
    """/tmp/cc-socks/<pid>.sock is the only shape Claude accepts, so the owner is in the name."""
    m = SOCK_NAME.match(os.path.basename(str(sock)))
    return int(m.group(1)) if m else None


def proc_start(pid: int) -> str:
    """Must match Claude's own reading exactly; LC_ALL and TZ are what make that reproducible."""
    env = dict(os.environ, LC_ALL="C", TZ="UTC")
    out = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True, text=True, timeout=5, env=env, check=True,
    )
    return out.stdout.strip()


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- registry


def read_registry() -> List[dict]:
    """Live entries only. A stale file from a crashed session is indistinguishable otherwise."""
    out: List[dict] = []
    try:
        names = sorted(os.listdir(SESSIONS_DIR))
    except OSError:
        return out
    for name in names:
        if not re.fullmatch(r"\d+\.json", name):
            continue
        try:
            entry = json.loads((SESSIONS_DIR / name).read_text())
        except (OSError, ValueError):
            continue  # half-written or foreign file
        if isinstance(entry.get("pid"), int) and entry.get("messagingSocketPath") and is_alive(entry["pid"]):
            out.append(entry)
    return out


def peers(self_pid: int) -> List[dict]:
    return [e for e in read_registry() if e["pid"] != self_pid and e.get("name")]


def display_name(entry: dict) -> str:
    """How a peer is addressed from a non-Claude agent: Claude gets cc-, our agents keep theirs."""
    return entry["name"] if entry.get("entrypoint") in PREFIXED_AGENTS else "cc-" + entry["name"]


def derive_name(cwd: str, session_name: Optional[str], taken: Sequence[str], prefix: str) -> str:
    """<prefix><session name or cwd basename>, suffixed -2, -3 when a live peer already has it."""
    raw = (session_name or "").strip() or os.path.basename(os.path.normpath(cwd))
    base = prefix + NAME_SAFE.sub("-", raw).strip("-")[:60]
    taken = set(taken)
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def write_registry(entry: dict) -> None:
    """Written atomically: Claude may read the directory at any moment and skips unparseable files."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    final = registry_path(entry["pid"])
    tmp = final.with_suffix(f".json.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(entry))
    os.replace(tmp, final)


def new_registry(pid: int, name: str, cwd: str, entrypoint: str, session_id: str) -> dict:
    now = int(time.time() * 1000)
    return {
        "pid": pid,
        "sessionId": session_id,
        "cwd": cwd,
        "startedAt": now,
        "procStart": proc_start(pid),
        "version": entrypoint,
        "peerProtocol": PEER_PROTOCOL,
        "peerFeatures": [],
        "kind": "interactive",
        "entrypoint": entrypoint,
        "pidDomain": "darwin" if os.uname().sysname == "Darwin" else "linux",
        "messagingSocketPath": str(sock_path(pid)),
        "name": name,
        "nameSource": "derived",
        "nameSince": now,
        "status": "idle",
        "updatedAt": now,
        "statusUpdatedAt": now,
    }


def resolve_target(to: str, self_pid: int) -> dict:
    """Name, display name, uds: address or socket path -> {sock, name}, or {error}."""
    t = to.strip().lstrip("@")
    if t.startswith("uds:"):
        return {"sock": t[4:], "name": t}
    if t.startswith("/"):
        return {"sock": t, "name": t}
    live = peers(self_pid)
    lc = t.lower()
    names = lambda e: (display_name(e), e["name"])  # noqa: E731 - two spellings per peer
    hits = [e for e in live if t in names(e)]
    if not hits:
        hits = [e for e in live if any(n.lower() == lc for n in names(e))]
    if not hits:
        hits = [e for e in live if any(n.lower().startswith(lc) for n in names(e))]
    if len(hits) == 1:
        return {"sock": hits[0]["messagingSocketPath"], "name": display_name(hits[0])}
    if not hits:
        known = ", ".join(display_name(e) for e in live) or "(none)"
        return {"error": f'No live session named "{t}". Known: {known}'}
    listed = ", ".join(f'{display_name(e)} [{e["pid"]}]' for e in hits)
    return {"error": f'"{t}" matches {len(hits)} sessions: {listed}. Use the full name.'}


# ---------------------------------------------------------------- permission-mode parity


def _mode_from_transcript(session_id: str) -> Optional[str]:
    """The transcript records permissionMode per entry, so it follows a runtime shift-tab switch."""
    # One unreadable or vanishing project directory must not abort the search: sessions are
    # writing under ~/.claude/projects constantly, so a scan that dies on the first error
    # silently downgrades a bypass peer to prompting and gets the message held.
    path = None
    try:
        entries = list(os.scandir(PROJECTS_DIR))
    except OSError:
        return None
    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            candidate = Path(entry.path) / f"{session_id}.jsonl"
            if candidate.exists():
                path = candidate
                break
        except OSError:
            continue
    if path is None:
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - MODE_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    hits = MODE_RE.findall(tail)
    if not hits:
        return None
    return "bypass" if hits[-1] == "bypassPermissions" else "prompting"


def _mode_from_command_line(pid: int) -> Optional[str]:
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    bypass = "--dangerously-skip-permissions" in out.stdout or re.search(
        r"--permission-mode[= ]+bypassPermissions", out.stdout
    )
    return "bypass" if bypass else "prompting"


def peer_mode(pid: int, session_id: Optional[str] = None) -> str:
    """
    Claude parks an inbound message as "held" unless the sender's asserted permission class matches
    its own. We have no class of our own, so we assert the target's: from its transcript, which
    tracks runtime switches, falling back to its command line for a session that has not written
    one yet, and finally to bypass rather than guaranteeing a hold.
    """
    sid = session_id
    if sid is None:
        sid = next((e.get("sessionId") for e in read_registry() if e["pid"] == pid), None)
    if sid and UUID_RE.match(sid):
        from_transcript = _mode_from_transcript(sid)
        if from_transcript:
            return from_transcript
    return _mode_from_command_line(pid) or "bypass"


# ---------------------------------------------------------------- envelope


def build_envelope(sender: str, sender_name: str, body: str, mode: str = "bypass") -> str:
    name = re.sub(r'["<>]', "", sender_name)
    return f'<{TAG} from="{sender}" from-name="{name}" from-mode="{mode}">\n{body}\n</{TAG}>'


def parse_envelope(content: str) -> dict:
    """Claude wraps outgoing bodies the same way; unwrap so the agent sees text and a reply address."""
    m = ENVELOPE_RE.match(content)
    if not m:
        return {"body": content}
    attrs: Dict[str, str] = dict(ATTR_RE.findall(m.group(1)))
    return {
        "from": attrs.get("from"),
        "fromName": attrs.get("from-name"),
        "fromMode": attrs.get("from-mode"),
        "body": m.group(2),
    }


# ---------------------------------------------------------------- auth key


def new_token() -> str:
    return secrets.token_hex(16)


def read_token(sock: str) -> Optional[str]:
    """Key files are <pid>.<hash>.key; the pid is the listener's, so find them by hash."""
    digest = hashlib.sha256(str(sock).encode()).hexdigest()
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return None
    for name in names:
        if name.endswith(f".{digest}.key"):
            try:
                return json.loads((SESSIONS_DIR / name).read_text())["peerToken"]
            except (OSError, ValueError, KeyError):
                continue
    return None


def write_key(pid: int, sock: str, token: str) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = key_path(pid, sock)
    payload = {"peerToken": token, "procStart": proc_start(pid), "pidDomain": "darwin"}
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path


def remove_files(pid: int, sock: str) -> None:
    for path in (Path(sock), key_path(pid, sock), registry_path(pid)):
        try:
            path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- send and listen


def send_user(sock: str, self_sock: str, self_name: str, body: str, timeout: float = 5.0) -> str:
    """One user message to a peer socket. Returns the msg_id we generated for it."""
    token = read_token(sock)
    sender = f"uds:{self_sock}"
    msg_id = str(uuid.uuid4())
    target_pid = pid_of_socket(sock)
    mode = "bypass" if target_pid is None else peer_mode(target_pid)
    frames: List[dict] = []
    if token:
        frames.append({"type": "auth", "token": token})
    frames.append({
        "type": "user",
        "from": sender,
        "msg_id": msg_id,
        "priority": "next",
        "message": {"content": build_envelope(sender, self_name, body, mode)},
    })
    wire = "".join(json.dumps(f) + "\n" for f in frames).encode()
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(str(sock))
        conn.sendall(wire)
        time.sleep(LINGER_S)  # closing at once can truncate the last frame on macOS
    finally:
        conn.close()
    return msg_id


def listen(sock: str, token: str, on_message: Callable[[dict], None],
           on_log: Optional[Callable[[str], None]] = None) -> socket.socket:
    """
    Serve like a Claude session: the auth frame gates the connection, user frames become messages,
    control frames (delivery receipts, idle notices) are ignored. Returns the listening socket;
    the caller closes it to stop. Runs its own daemon threads so the host process is not blocked.
    """
    log = on_log or (lambda _s: None)
    SOCK_DIR.mkdir(parents=True, exist_ok=True)
    SOCK_DIR.chmod(0o700)
    try:
        os.unlink(sock)
    except OSError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock))
    os.chmod(sock, 0o600)
    server.listen(16)

    def serve_conn(conn: socket.socket) -> None:
        authed = False
        buf = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                if len(buf) > MAX_FRAME:
                    log("dropped oversized frame")
                    return
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        log("dropped non-JSON frame")
                        continue
                    if frame.get("type") == "auth":
                        authed = secrets.compare_digest(str(frame.get("token", "")), token)
                        if not authed:
                            log("bad auth token, closing")
                            return
                        continue
                    if not authed:
                        log("frame before auth, closing")
                        return
                    if frame.get("type") == "user":
                        content = (frame.get("message") or {}).get("content")
                        if isinstance(content, str):
                            parsed = parse_envelope(content)
                            parsed.setdefault("from", frame.get("from"))
                            parsed["msgId"] = frame.get("msg_id")
                            on_message(parsed)

    def accept_loop() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return  # socket closed by the caller
            threading.Thread(target=serve_conn, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    return server

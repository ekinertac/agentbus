"""
Per-client installers: teach each agent on this machine how to reach the bus.

Responsibility: own the knowledge of where each host keeps its config and what an MCP server entry
looks like there, so `agentbus install` is one command instead of four hand-edited files. Every
writer is idempotent (running twice changes nothing), backs the file up before touching it, and
can be undone by `agentbus uninstall`.

Each client takes a `home` so the whole thing is testable against a temporary directory rather
than the developer's real config.

Hosts split in two. MCP hosts (kiro, codex, gemini, cursor) all get the same `agentbus mcp`
server and differ only in file format. Hosts with a real plugin API (pi, opencode) need their own
adapter file and are reported as unsupported here until those adapters move into this repo.

Related: mcp.py (what gets spawned), cli.py (install/uninstall commands).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

SERVER_KEY = "agentbus"
# Marker so a TOML block we wrote can be found again without parsing TOML, which the stdlib
# cannot write and cannot read at all before 3.11.
TOML_MARKER = "# agentbus: cross-session messaging (managed block, safe to delete)"


def server_command() -> Dict:
    """
    How a host should spawn the adapter. Running from a checkout means the package is not on the
    host's import path, so the entry carries PYTHONPATH; an installed console script would not
    need it, and that is what a packaged release will emit instead.
    """
    repo = Path(__file__).resolve().parent.parent
    return {
        "command": sys.executable,
        "args": ["-m", "agentbus", "mcp"],
        "env": {"PYTHONPATH": str(repo)},
    }


def backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    dest = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(path, dest)
    return dest


class Result:
    def __init__(self, client: str, changed: bool, detail: str, backup_path: Optional[Path] = None):
        self.client = client
        self.changed = changed
        self.detail = detail
        self.backup = backup_path

    def as_dict(self) -> Dict:
        return {
            "client": self.client,
            "changed": self.changed,
            "detail": self.detail,
            "backup": str(self.backup) if self.backup else None,
        }


class Client:
    name = "?"
    label = "?"

    def __init__(self, home: Optional[Path] = None):
        self.home = home or Path.home()

    def present(self) -> bool:
        raise NotImplementedError

    def installed(self) -> bool:
        raise NotImplementedError

    def install(self, dry_run: bool = False) -> Result:
        raise NotImplementedError

    def uninstall(self, dry_run: bool = False) -> Result:
        raise NotImplementedError


class JsonMcpClient(Client):
    """A host whose MCP servers live under a JSON object, which is most of them."""

    config_relpath = ""
    servers_key = "mcpServers"
    # Some hosts want extra keys alongside command/args; kiro wants auto-approval, for instance.
    extra_server_fields: Dict = {}

    @property
    def config_path(self) -> Path:
        return self.home / self.config_relpath

    def present(self) -> bool:
        return self.config_path.parent.exists() or shutil.which(self.name) is not None

    def _load(self) -> Dict:
        try:
            return json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            return {}

    def installed(self) -> bool:
        return SERVER_KEY in (self._load().get(self.servers_key) or {})

    def _entry(self) -> Dict:
        entry = dict(server_command())
        entry.update(self.extra_server_fields)
        return entry

    def install(self, dry_run: bool = False) -> Result:
        config = self._load()
        servers = config.setdefault(self.servers_key, {})
        wanted = self._entry()
        if servers.get(SERVER_KEY) == wanted:
            return Result(self.name, False, f"already configured in {self.config_path}")
        if dry_run:
            return Result(self.name, True, f"would add the agentbus server to {self.config_path}")
        saved = backup(self.config_path)
        servers[SERVER_KEY] = wanted
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(config, indent=2) + "\n")
        return Result(self.name, True, f"added the agentbus server to {self.config_path}", saved)

    def uninstall(self, dry_run: bool = False) -> Result:
        config = self._load()
        servers = config.get(self.servers_key) or {}
        if SERVER_KEY not in servers:
            return Result(self.name, False, f"nothing to remove from {self.config_path}")
        if dry_run:
            return Result(self.name, True, f"would remove the agentbus server from {self.config_path}")
        saved = backup(self.config_path)
        del servers[SERVER_KEY]
        self.config_path.write_text(json.dumps(config, indent=2) + "\n")
        return Result(self.name, True, f"removed the agentbus server from {self.config_path}", saved)


class Kiro(JsonMcpClient):
    name = "kiro"
    label = "Kiro CLI"
    config_relpath = ".kiro/settings/mcp.json"
    # Reading the bus and listing peers change nothing, so approving them every turn is noise.
    extra_server_fields = {"disabled": False, "autoApprove": ["list_claude_sessions", "check_messages"]}

    @property
    def agent_path(self) -> Path:
        return self.home / ".kiro/agents/agentbus.json"

    def present(self) -> bool:
        return (self.home / ".kiro").exists() or shutil.which("kiro-cli") is not None

    def agent_config(self) -> Dict:
        return {
            "name": "agentbus",
            "description": "Default Kiro agent plus cross-session messaging with other coding agents",
            "tools": ["read", "write", "shell", "aws", "report", "introspect", "knowledge",
                      "thinking", "todo", "delegate", "grep", "glob", "@agentbus"],
            "allowedTools": ["@agentbus/list_claude_sessions", "@agentbus/check_messages",
                             "@agentbus/set_session_name"],
            # Kiro's TUI engine runs no agent hooks (kirodotdev/Kiro#11614), so the model itself has
            # to pull. The hooks below still help in the engines that do run them.
            "prompt": ("At the start of every turn, call the check_messages tool once before doing "
                       "anything else, and act on anything it returns. Other agent sessions reach "
                       "you through it and nothing else will surface their messages. Say nothing "
                       "about the check when it returns no messages."),
            "hooks": {
                trigger: [{"command": f"{sys.executable} -m agentbus drain",
                           "timeout_ms": 3000, "cache_ttl_seconds": 0}]
                for trigger in ("userPromptSubmit", "stop")
            },
            "includeMcpJson": True,
            "model": None,
        }

    def installed(self) -> bool:
        return super().installed() and self.agent_path.exists()

    def install(self, dry_run: bool = False) -> Result:
        result = super().install(dry_run)
        wanted = self.agent_config()
        try:
            current = json.loads(self.agent_path.read_text())
        except (OSError, ValueError):
            current = None
        if current == wanted:
            return result
        if dry_run:
            return Result(self.name, True, f"{result.detail}; would write {self.agent_path}")
        saved = backup(self.agent_path)
        self.agent_path.parent.mkdir(parents=True, exist_ok=True)
        self.agent_path.write_text(json.dumps(wanted, indent=2) + "\n")
        detail = f"{result.detail}; wrote the agentbus agent to {self.agent_path}. Start Kiro with: kiro-cli chat --agent agentbus"
        return Result(self.name, True, detail, result.backup or saved)

    def uninstall(self, dry_run: bool = False) -> Result:
        result = super().uninstall(dry_run)
        if self.agent_path.exists() and not dry_run:
            self.agent_path.unlink()
            return Result(self.name, True, f"{result.detail}; removed {self.agent_path}", result.backup)
        return result


class Gemini(JsonMcpClient):
    name = "gemini"
    label = "Gemini CLI"
    config_relpath = ".gemini/settings.json"


class Cursor(JsonMcpClient):
    name = "cursor"
    label = "Cursor"
    config_relpath = ".cursor/mcp.json"


class Codex(Client):
    """Codex keeps its config in TOML, which the stdlib cannot write, so the block is managed by text."""

    name = "codex"
    label = "Codex CLI"

    @property
    def config_path(self) -> Path:
        return self.home / ".codex/config.toml"

    def present(self) -> bool:
        return (self.home / ".codex").exists() or shutil.which("codex") is not None

    def _text(self) -> str:
        try:
            return self.config_path.read_text()
        except OSError:
            return ""

    def block(self) -> str:
        spec = server_command()
        args = ", ".join(json.dumps(a) for a in spec["args"])
        env = ", ".join(f"{k} = {json.dumps(v)}" for k, v in spec["env"].items())
        return (
            f"{TOML_MARKER}\n"
            f"[mcp_servers.{SERVER_KEY}]\n"
            f"command = {json.dumps(spec['command'])}\n"
            f"args = [{args}]\n"
            f"env = {{ {env} }}\n"
        )

    def installed(self) -> bool:
        return TOML_MARKER in self._text()

    def install(self, dry_run: bool = False) -> Result:
        text = self._text()
        if self.block() in text:
            return Result(self.name, False, f"already configured in {self.config_path}")
        if dry_run:
            return Result(self.name, True, f"would add an [mcp_servers.{SERVER_KEY}] block to {self.config_path}")
        saved = backup(self.config_path)
        body = self._strip_block(text)
        if body and not body.endswith("\n"):
            body += "\n"
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(f"{body}\n{self.block()}" if body else self.block())
        return Result(self.name, True, f"added an [mcp_servers.{SERVER_KEY}] block to {self.config_path}", saved)

    def _strip_block(self, text: str) -> str:
        """Drop a previously managed block, so install stays idempotent across version changes."""
        if TOML_MARKER not in text:
            return text
        out: List[str] = []
        skipping = False
        for line in text.splitlines():
            if line.strip() == TOML_MARKER:
                skipping = True
                continue
            if skipping:
                # The block ends at the next section header or a blank line following its keys.
                if line.startswith("[") and not line.startswith(f"[mcp_servers.{SERVER_KEY}]"):
                    skipping = False
                    out.append(line)
                continue
            out.append(line)
        return "\n".join(out).rstrip("\n")

    def uninstall(self, dry_run: bool = False) -> Result:
        text = self._text()
        if TOML_MARKER not in text:
            return Result(self.name, False, f"nothing to remove from {self.config_path}")
        if dry_run:
            return Result(self.name, True, f"would remove the agentbus block from {self.config_path}")
        saved = backup(self.config_path)
        stripped = self._strip_block(text)
        self.config_path.write_text(stripped + "\n" if stripped else "")
        return Result(self.name, True, f"removed the agentbus block from {self.config_path}", saved)


class ClaudeCode(Client):
    """Claude Code owns the protocol; it needs nothing installed to see other agents."""

    name = "claude"
    label = "Claude Code"

    def present(self) -> bool:
        return (self.home / ".claude").exists() or shutil.which("claude") is not None

    def installed(self) -> bool:
        return True

    def install(self, dry_run: bool = False) -> Result:
        return Result(self.name, False, "nothing to install: Claude Code hosts the bus natively")

    def uninstall(self, dry_run: bool = False) -> Result:
        return Result(self.name, False, "nothing to remove")


CLIENTS = [ClaudeCode, Kiro, Codex, Gemini, Cursor]
# Hosts with a plugin API that can push into a live turn. Their adapters are not in this repo yet.
PLUGIN_HOSTS = {"pi": "pi", "opencode": "opencode"}


def all_clients(home: Optional[Path] = None) -> List[Client]:
    return [cls(home) for cls in CLIENTS]


def by_name(name: str, home: Optional[Path] = None) -> Optional[Client]:
    for client in all_clients(home):
        if client.name == name:
            return client
    return None

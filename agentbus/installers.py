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
import subprocess
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


class Antigravity(JsonMcpClient):
    """
    Confirmed by watching `agy mcp add` write to a throwaway HOME: despite the `agy`/`antigravity`
    binary name, its MCP config lives under `.gemini/config/mcp_config.json` (Antigravity shares
    its agent core with Gemini CLI), a different file from Gemini CLI's own `.gemini/settings.json`
    so the two installers cannot collide. `agy mcp add/list/remove` is the documented way to manage
    it, but a plain JSON write is simpler and this shape matches what `agy mcp add` itself produces.
    """

    name = "antigravity"
    label = "Antigravity CLI"
    config_relpath = ".gemini/config/mcp_config.json"
    extra_server_fields = {"disabled": False}

    def present(self) -> bool:
        return shutil.which("agy") is not None or shutil.which("antigravity") is not None


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


def _pi_extensions_dir(base: Path) -> Path:
    """
    Where extensions live under a pi agent directory. The standard layout nests them under agent/
    (~/.pi/agent/extensions); a wrapper pointing PI_CODING_AGENT_DIR at its own directory keeps
    them at the top level. Whichever exists wins, and a fresh directory gets the flat form.
    """
    if (base / "extensions").is_dir():
        return base / "extensions"
    if (base / "agent" / "extensions").is_dir() or (base / "agent").is_dir():
        return base / "agent" / "extensions"
    return base / "extensions"


class Pi(Client):
    """
    pi loads any directory under <agent dir>/extensions, so the adapter is symlinked in rather
    than copied: a git pull then updates every install at once.

    The agent dir is PI_CODING_AGENT_DIR when set, else ~/.pi/agent. Wrappers point that variable
    at a directory of their own and keep extensions at its top level rather than under agent/, so
    extra locations are passed in with --agent-dir instead of being guessed at.
    """

    name = "pi"
    label = "pi"

    def __init__(self, home: Optional[Path] = None, extra_dirs: Optional[List[Path]] = None):
        super().__init__(home)
        self.extra_dirs = list(extra_dirs or [])

    @property
    def adapter_source(self) -> Path:
        return Path(__file__).resolve().parent.parent / "adapters" / "pi"

    def extension_dirs(self) -> List[Path]:
        """Every extensions directory this install should land in, deduplicated, in order."""
        dirs: List[Path] = []
        env_dir = os.environ.get("PI_CODING_AGENT_DIR")
        if env_dir:
            dirs.append(Path(env_dir).expanduser())
        dirs.append(self.home / ".pi" / "agent")
        dirs.extend(self.extra_dirs)
        out: List[Path] = []
        for base in dirs:
            target = _pi_extensions_dir(base.expanduser())
            if target not in out:
                out.append(target)
        return out

    def present(self) -> bool:
        return (self.home / ".pi").exists() or shutil.which("pi") is not None or bool(self.extra_dirs)

    def _links(self) -> List[Path]:
        return [d / SERVER_KEY for d in self.extension_dirs()]

    def installed(self) -> bool:
        links = self._links()
        return bool(links) and all(
            link.is_symlink() and link.resolve() == self.adapter_source for link in links
        )

    def install(self, dry_run: bool = False) -> Result:
        todo = [link for link in self._links()
                if not (link.is_symlink() and link.resolve() == self.adapter_source)]
        if not todo:
            return Result(self.name, False, f"already linked into {len(self._links())} extensions directory(ies)")
        if dry_run:
            return Result(self.name, True, "would link the adapter into " + ", ".join(str(t) for t in todo))
        for link in todo:
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(self.adapter_source, target_is_directory=True)
        return Result(self.name, True,
                      "linked the adapter into " + ", ".join(str(t) for t in todo) + ". Run /reload in pi.")

    def uninstall(self, dry_run: bool = False) -> Result:
        present = [link for link in self._links() if link.is_symlink() or link.exists()]
        if not present:
            return Result(self.name, False, "nothing to remove")
        if dry_run:
            return Result(self.name, True, "would remove " + ", ".join(str(link) for link in present))
        for link in present:
            link.unlink()
        return Result(self.name, True, "removed " + ", ".join(str(link) for link in present))


class Opencode(Client):
    """
    opencode's plugin resolves @opencode-ai/plugin only from files under ~/.config/opencode, so the
    real module (adapters/opencode/index.ts, checked out wherever the repo lives) cannot be loaded
    directly. A shim there imports it by absolute path and supplies the tool() function opencode
    installs for its own plugins.

    The global plugin directory is not auto-scanned either, so the shim also needs an entry in the
    `plugin` array of opencode.json, or the file exists and does nothing.
    """

    name = "opencode"
    label = "opencode"

    @property
    def config_path(self) -> Path:
        return self.home / ".config/opencode/opencode.json"

    @property
    def shim_path(self) -> Path:
        return self.home / ".config/opencode/plugin/agentbus.ts"

    @property
    def adapter_source(self) -> Path:
        return Path(__file__).resolve().parent.parent / "adapters" / "opencode" / "index.ts"

    def present(self) -> bool:
        return (self.home / ".config/opencode").exists() or shutil.which("opencode") is not None

    def shim(self) -> str:
        lines = [
            "// agentbus: cross-session messaging shim, written by `agentbus install opencode`.",
            "// @opencode-ai/plugin only resolves from under ~/.config/opencode, so the real plugin",
            "// (in the agentbus checkout) cannot be imported directly; this file bridges the two.",
            'import { tool } from "@opencode-ai/plugin";',
            f'import make from "{self.adapter_source}";',
            "export default make(tool);",
        ]
        return "\n".join(lines) + "\n"

    def _plugin_url(self) -> str:
        return f"file://{self.shim_path}"

    def _load_config(self) -> Dict:
        try:
            return json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            return {}

    def installed(self) -> bool:
        if not self.shim_path.exists() or self.shim_path.read_text() != self.shim():
            return False
        return self._plugin_url() in self._load_config().get("plugin", [])

    def install(self, dry_run: bool = False) -> Result:
        if self.installed():
            return Result(self.name, False, f"already configured in {self.config_path}")
        if dry_run:
            return Result(self.name, True,
                          f"would write {self.shim_path} and register it in {self.config_path}")
        self.shim_path.parent.mkdir(parents=True, exist_ok=True)
        self.shim_path.write_text(self.shim())
        config = self._load_config()
        saved = backup(self.config_path)
        plugins = config.setdefault("plugin", [])
        if self._plugin_url() not in plugins:
            plugins.append(self._plugin_url())
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(config, indent=2) + "\n")
        return Result(self.name, True,
                      f"wrote {self.shim_path} and registered it in {self.config_path}", saved)

    def uninstall(self, dry_run: bool = False) -> Result:
        had_entry = self._plugin_url() in self._load_config().get("plugin", [])
        had_shim = self.shim_path.exists()
        if not had_entry and not had_shim:
            return Result(self.name, False, "nothing to remove")
        if dry_run:
            return Result(self.name, True, f"would remove the plugin entry and {self.shim_path}")
        saved = None
        if had_entry:
            config = self._load_config()
            saved = backup(self.config_path)
            config["plugin"] = [u for u in config.get("plugin", []) if u != self._plugin_url()]
            self.config_path.write_text(json.dumps(config, indent=2) + "\n")
        if had_shim:
            self.shim_path.unlink()
        return Result(self.name, True, f"removed the plugin entry and {self.shim_path}", saved)


class Hermes(Client):
    """
    Hermes Agent (NousResearch) keeps its config in ~/.hermes/config.yaml, a big hand-maintained
    file with real comments and sections. The stdlib has no YAML writer, and even a careful
    block-append is unsafe here: `mcp_servers:` is a top-level key that (unlike TOML's
    `[mcp_servers.x]` tables) most YAML parsers resolve by taking the LAST occurrence, so
    appending a second `mcp_servers:` block would silently wipe out any servers already there
    instead of adding to them.

    So this drives `hermes mcp add/list/remove` directly rather than touching the file: it is the
    one component that actually understands that format safely, and it is a stable, documented CLI.
    Every prompt gets an explicit "y" (closed stdin does not reliably default-accept, confirmed:
    the "enable all tools?" prompt cancels on EOF despite defaulting to Y) and `installed()` reads
    back through `mcp list` rather than the file, for the same reason.

    Hermes reports itself to MCP as literally "mcp" (confirmed against 0.21.0) - too generic to
    map to a prefix without risking misattributing some other, unrelated host that also lazily
    calls itself "mcp". It is deliberately left out of mcp.HOST_PREFIXES; its sessions register
    under the generic "ab-" prefix like any other unrecognised host, which is correct here, not a
    gap to close.
    """

    name = "hermes"
    label = "Hermes Agent"

    def present(self) -> bool:
        return shutil.which("hermes") is not None or (self.home / ".hermes").exists()

    def _run(self, args: List[str], input_text: str = "") -> "subprocess.CompletedProcess":
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        return subprocess.run(
            ["hermes", *args], input=input_text, capture_output=True, text=True,
            timeout=30, env=env,
        )

    def installed(self) -> bool:
        out = self._run(["mcp", "list"]).stdout
        # The table has a Status column; "enabled" only appears there, never in the name/path
        # columns for a server named "agentbus" (SERVER_KEY has no dash-adjacent "enabled" text).
        return SERVER_KEY in out and "enabled" in out

    def install(self, dry_run: bool = False) -> Result:
        if self.installed():
            return Result(self.name, False, "already configured (`hermes mcp list`)")
        if dry_run:
            return Result(self.name, True, "would run `hermes mcp add agentbus ...`")
        spec = server_command()
        args = [
            "mcp", "add", SERVER_KEY, "--command", spec["command"],
            "--env", *[f"{k}={v}" for k, v in spec["env"].items()],
            "--args", *spec["args"],
        ]
        # Two prompts possible (overwrite an existing entry, then enable its tools); an unneeded
        # extra "y" is simply left unread when the process exits after the first.
        result = self._run(args, input_text="y\ny\n")
        if "Saved" not in result.stdout:
            raise OSError(f"hermes mcp add did not report success: {result.stdout}{result.stderr}")
        return Result(self.name, True, "added via `hermes mcp add`, config at ~/.hermes/config.yaml")

    def uninstall(self, dry_run: bool = False) -> Result:
        if not self.installed():
            return Result(self.name, False, "nothing to remove")
        if dry_run:
            return Result(self.name, True, "would run `hermes mcp remove agentbus`")
        result = self._run(["mcp", "remove", SERVER_KEY], input_text="y\n")
        if "Removed" not in result.stdout:
            raise OSError(f"hermes mcp remove did not report success: {result.stdout}{result.stderr}")
        return Result(self.name, True, "removed via `hermes mcp remove`")


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


CLIENTS = [ClaudeCode, Pi, Opencode, Kiro, Codex, Gemini, Cursor, Antigravity, Hermes]
def all_clients(home: Optional[Path] = None, extra_dirs: Optional[List[Path]] = None) -> List[Client]:
    return [
        cls(home, extra_dirs) if cls is Pi else cls(home)  # type: ignore[call-arg]
        for cls in CLIENTS
    ]


def by_name(name: str, home: Optional[Path] = None, extra_dirs: Optional[List[Path]] = None) -> Optional[Client]:
    for client in all_clients(home, extra_dirs):
        if client.name == name:
            return client
    return None

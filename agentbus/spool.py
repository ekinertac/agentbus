"""
The inbox spool: where a message waits when its host cannot be interrupted.

Responsibility: durable hand-off between the process holding the socket (the MCP server) and
whatever later surfaces the message to the model (a host hook, or the check_messages tool).

Why it exists: an agent with a real plugin API can push an inbound message straight into a live
turn, and those adapters never touch this file. A host reached only over MCP cannot be
interrupted at all, so the message has to sit somewhere until the model next looks. Losing one
because the host was mid-turn is the failure this prevents.

One file per listening process, named by pid, so two sessions never share a spool. Files are
0600 under ~/.cache/agentbus and are removed when the process exits.

Related: mcp.py (writer), cli.py `drain` (reader for hook-capable hosts).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import List, Optional

SPOOL_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "agentbus"
# A sender name that already carries an address prefix must not gain a second one: a Claude
# session called "cc-foo" is addressed cc-foo, not cc-cc-foo.
PREFIXED = re.compile(r"^(pi|oc|ki|cx|gm|cu|ab|cc)-")


def spool_path(pid: int) -> Path:
    return SPOOL_DIR / f"{pid}.inbox.jsonl"


def append(pid: int, message: dict) -> None:
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    SPOOL_DIR.chmod(0o700)
    path = spool_path(pid)
    record = dict(message)
    record.setdefault("at", int(time.time() * 1000))
    with path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    path.chmod(0o600)


def drain(pid: int) -> List[dict]:
    """
    Read and clear. Truncating rather than unlinking keeps the writer's open handle pointed at the
    same inode, so a message arriving during a drain is not written into a deleted file.
    """
    path = spool_path(pid)
    try:
        raw = path.read_text()
    except OSError:
        return []
    try:
        os.truncate(path, 0)
    except OSError:
        pass
    out: List[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a torn line must not cost us the rest of the spool
    return out


def remove(pid: int) -> None:
    try:
        spool_path(pid).unlink()
    except OSError:
        pass


def reply_address(from_name: Optional[str], sender: Optional[str]) -> str:
    """Our agents carry their prefix already; a Claude session is addressed cc-<name>."""
    if not from_name:
        return sender or ""
    return from_name if PREFIXED.match(from_name) else f"cc-{from_name}"


def render(message: dict) -> str:
    """One format for a delivered message, whether a hook or a tool call surfaced it."""
    who = message.get("fromName") or message.get("from") or "unknown peer"
    reply_to = reply_address(message.get("fromName"), message.get("from"))
    return (
        f"[Message from {who}]\n"
        f"{message.get('body', '')}\n\n"
        f'(Reply with the send_to_claude tool, to: "{reply_to}".)'
    )

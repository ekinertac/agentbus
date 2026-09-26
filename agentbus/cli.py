"""
The agentbus command line: look at the bus, send to it, and check that this machine is wired up.

Responsibility: a human or a script talking to the local session bus. Everything about the wire
lives in protocol.py; this module only parses arguments, formats output and picks exit codes.

Design rules it follows, because other tools will end up depending on them: primary output on
stdout and diagnostics on stderr, so `agentbus list | grep` stays clean; `--json` for anything a
script should parse, since the human format is free to change; no prompts ever, so an agent or CI
can drive every command; and distinct exit codes per failure so a caller can branch on them.

Exit codes:
  0  ok
  1  runtime failure (socket unreachable, write failed)
  2  usage error (argparse)
  3  target not found or ambiguous
  4  this machine is not wired up (no sessions directory, bad permissions)

Related: protocol.py (the wire), tests/test_cli.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import installers
from . import protocol as p
from . import spool

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_NO_TARGET = 3
EXIT_NOT_WIRED = 4

# The name a bare shell send appears under. It is not a registered session, so nothing can reply
# to it; `send` says so unless the caller asked for JSON.
CLI_NAME = "agentbus-cli"


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _out(obj, as_json: bool, human: str) -> None:
    print(json.dumps(obj, indent=2) if as_json else human)


def _agent_of(entry: dict) -> str:
    """Claude records itself as entrypoint "cli", which tells a reader nothing."""
    kind = entry.get("entrypoint") or "cli"
    return kind if kind in p.PREFIXED_AGENTS else "claude"


def cmd_list(args: argparse.Namespace) -> int:
    rows = p.peers(os.getpid())
    if args.json:
        print(json.dumps([
            {
                "name": p.display_name(r),
                "registryName": r["name"],
                "agent": _agent_of(r),
                "status": r.get("status"),
                "cwd": r.get("cwd"),
                "pid": r["pid"],
                "socket": r["messagingSocketPath"],
            }
            for r in rows
        ], indent=2))
        return EXIT_OK
    if not rows:
        _err("No live sessions. Start one, or run 'agentbus doctor' if you expected some.")
        return EXIT_OK
    width = max(len(p.display_name(r)) for r in rows)
    for r in sorted(rows, key=p.display_name):
        print(f"{p.display_name(r):<{width}}  {_agent_of(r):<9} {str(r.get('status') or '?'):<7} {r.get('cwd', '')}")
    return EXIT_OK


def cmd_send(args: argparse.Namespace) -> int:
    body = sys.stdin.read() if args.message == "-" else args.message
    if not body.strip():
        _err("Refusing to send an empty message.")
        return EXIT_USAGE
    target = p.resolve_target(args.to, os.getpid())
    if "error" in target:
        _err(target["error"])
        return EXIT_NO_TARGET
    # Our own pid's path, even with nothing listening on it: Claude accepts a sender whose address
    # sits in the same sockets directory, and this keeps the address honest about who sent it.
    self_sock = str(p.sock_path(os.getpid()))
    try:
        msg_id = p.send_user(target["sock"], self_sock, args.from_name, body, timeout=args.timeout)
    except OSError as e:
        _err(f"Could not reach {target['name']} at {target['sock']}: {e}")
        return EXIT_FAIL
    if args.json:
        print(json.dumps({"sent": True, "to": target["name"], "msg_id": msg_id}, indent=2))
    elif not args.quiet:
        print(f"Sent to {target['name']} (msg {msg_id}).")
        if args.from_name == CLI_NAME:
            _err("Note: a reply has nowhere to go, this shell is not a registered session.")
    return EXIT_OK


def _doctor_checks() -> List[dict]:
    checks: List[dict] = []

    def add(name: str, ok: Optional[bool], detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    add("python", True, f"{sys.version_info.major}.{sys.version_info.minor} at {sys.executable}")

    if not p.SESSIONS_DIR.is_dir():
        add("sessions directory", False,
            f"{p.SESSIONS_DIR} does not exist. Is Claude Code installed and has it run once?")
    else:
        add("sessions directory", True, str(p.SESSIONS_DIR))

    if p.SOCK_DIR.exists():
        mode = os.stat(p.SOCK_DIR).st_mode & 0o777
        ok = mode == 0o700
        add("sockets directory", ok,
            f"{p.SOCK_DIR} is {oct(mode)}" + ("" if ok else f"; Claude refuses anything but 0700. Run: chmod 700 {p.SOCK_DIR}"))
    else:
        add("sockets directory", None, f"{p.SOCK_DIR} does not exist yet; it is created on first use.")

    live = p.peers(os.getpid())
    claude = [r for r in live if (r.get("entrypoint") or "cli") not in p.PREFIXED_AGENTS]
    ours = [r for r in live if (r.get("entrypoint") or "cli") in p.PREFIXED_AGENTS]
    other = f"{len(ours)} other agent" + ("" if len(ours) == 1 else "s")
    add("live sessions", True, f"{len(claude)} Claude, {other}")

    # The protocol is undocumented and versioned: a bump means our frames may no longer be read.
    versions = {r.get("peerProtocol") for r in live if r.get("peerProtocol") is not None}
    unknown = {v for v in versions if v != p.PEER_PROTOCOL}
    if not versions:
        add("protocol version", None, "no live session advertises one yet")
    elif unknown:
        add("protocol version", False,
            f"sessions advertise {sorted(versions)}, this build speaks {p.PEER_PROTOCOL}. "
            "Claude Code changed the protocol; agentbus needs an update.")
    else:
        add("protocol version", True, f"{p.PEER_PROTOCOL}, matching every live session")

    for client in installers.all_clients():
        if not client.present():
            continue
        wired = client.installed()
        add(f"client: {client.label}", True if wired else None,
            "configured" if wired else f"present but not configured. Run: agentbus install {client.name}")

    return checks


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = _doctor_checks()
    failed = [c for c in checks if c["ok"] is False]
    if args.json:
        print(json.dumps({"ok": not failed, "checks": checks}, indent=2))
    else:
        for c in checks:
            mark = {True: "ok  ", False: "FAIL", None: "--  "}[c["ok"]]
            print(f"{mark} {c['check']}: {c['detail']}")
    return EXIT_NOT_WIRED if failed else EXIT_OK


def cmd_mcp(args: argparse.Namespace) -> int:
    """Run the MCP adapter on stdio. A host spawns this; a human never runs it directly."""
    from . import mcp  # imported late so `agentbus list` does not pay for it

    return mcp.serve(args.name)


def cmd_drain(args: argparse.Namespace) -> int:
    """
    Print and clear messages waiting for the session in this directory, for hosts that run hooks.

    Deliberately reads no stdin: a host that writes no hook payload and leaves the pipe open would
    block this until its timeout killed it, which is indistinguishable from the hook never running.
    Silent when there is nothing, so a hook adds nothing to the context on a quiet turn.
    """
    cwd = os.getcwd()
    pids = [
        e["pid"] for e in p.read_registry()
        if e.get("entrypoint") == "agentbus" and e.get("cwd") == cwd
    ]
    messages = [m for pid in pids for m in spool.drain(pid)]
    if args.json:
        print(json.dumps(messages, indent=2))
    elif messages:
        print("\n\n".join(spool.render(m) for m in messages))
    return EXIT_OK


def _selected_clients(names: List[str]) -> List:
    """No names means every client this machine actually has."""
    if not names:
        return [c for c in installers.all_clients() if c.present()]
    chosen = []
    for name in names:
        client = installers.by_name(name)
        if client is None:
            hint = installers.PLUGIN_HOSTS.get(name)
            if hint:
                _err(f"{name} needs its own adapter, which this installer does not ship yet. See docs/adding-an-agent.md.")
            else:
                known = ", ".join(c.name for c in installers.all_clients())
                _err(f"Unknown client \"{name}\". Known: {known}")
            return []
        chosen.append(client)
    return chosen


def _run_install(args: argparse.Namespace, uninstall: bool) -> int:
    clients = _selected_clients(args.clients)
    if not clients:
        if args.clients:
            return EXIT_USAGE
        _err("No supported agent found on this machine.")
        return EXIT_NOT_WIRED
    results = []
    for client in clients:
        try:
            action = client.uninstall if uninstall else client.install
            results.append(action(dry_run=args.dry_run))
        except OSError as e:
            _err(f"{client.name}: {e}")
            return EXIT_FAIL
    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
        return EXIT_OK
    for r in results:
        print(f"{'changed' if r.changed else 'ok     '} {r.client}: {r.detail}")
        if r.backup:
            print(f"         backup: {r.backup}")
    if not uninstall and any(r.changed for r in results) and not args.dry_run:
        print("\nRestart the agents you just configured for them to pick this up.")
    return EXIT_OK


def cmd_install(args: argparse.Namespace) -> int:
    return _run_install(args, uninstall=False)


def cmd_uninstall(args: argparse.Namespace) -> int:
    return _run_install(args, uninstall=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentbus",
        description="Cross-session messaging between coding agents on this machine.",
        epilog=(
            "examples:\n"
            "  agentbus list                          who is reachable right now\n"
            "  agentbus send cc-myproject \"ci is green\"\n"
            "  git log -1 | agentbus send pi-notes -  read the message from stdin\n"
            "  agentbus install                       configure every agent found here\n"
            "  agentbus doctor                        check this machine is wired up\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def with_common(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        return sp

    p_list = with_common(sub.add_parser("list", help="list live sessions that can receive messages"))
    p_list.set_defaults(func=cmd_list)

    p_send = with_common(sub.add_parser("send", help="send a message to a session"))
    p_send.add_argument("to", help="session name as 'agentbus list' shows it, or a uds: address")
    p_send.add_argument("message", help="the message, or - to read it from stdin")
    p_send.add_argument("--from-name", default=CLI_NAME, help=f"name the message appears under (default: {CLI_NAME})")
    p_send.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for the socket (default: 5)")
    p_send.add_argument("-q", "--quiet", action="store_true", help="say nothing on success")
    p_send.set_defaults(func=cmd_send)

    p_doc = with_common(sub.add_parser("doctor", help="check this machine is wired up"))
    p_doc.set_defaults(func=cmd_doctor)

    for verb, func, helptext in (
        ("install", cmd_install, "configure agents on this machine to use the bus"),
        ("uninstall", cmd_uninstall, "remove the agentbus configuration from agents"),
    ):
        sp = with_common(sub.add_parser(verb, help=helptext))
        sp.add_argument("clients", nargs="*", metavar="client",
                        help="clients to act on (default: every one found here)")
        sp.add_argument("-n", "--dry-run", action="store_true", help="say what would change, write nothing")
        sp.set_defaults(func=func)

    p_mcp = sub.add_parser("mcp", help="run the MCP adapter on stdio (hosts spawn this)")
    p_mcp.add_argument("--name", help="session name to register (default: from AGENTBUS_NAME or the directory)")
    p_mcp.set_defaults(func=cmd_mcp)

    p_drain = with_common(sub.add_parser("drain", help="print and clear messages waiting for this directory's session"))
    p_drain.set_defaults(func=cmd_drain)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # No arguments at all: help is more useful than an error, and cheaper than a prompt.
        parser.print_help()
        return EXIT_USAGE
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

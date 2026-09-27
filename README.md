# agentbus

[![test](https://github.com/ekinertac/agentbus/actions/workflows/test.yml/badge.svg)](https://github.com/ekinertac/agentbus/actions/workflows/test.yml)

Lets coding agents on the same machine message each other. Claude Code, pi, opencode, kiro-cli,
codex, antigravity, hermes, and anything else that speaks MCP show up as named peers you can
address from any of the others.

```console
$ agentbus list
cc-acme-frontend   claude   idle    ~/Code/acme-frontend
ki-acme-backend    kiro     idle    ~/Code/acme-backend
pi-acme-security   pi       idle    ~/Code/acme-security

$ agentbus send cc-acme-frontend "PR #98 is green, go ahead and merge"
Sent to cc-acme-frontend (msg 8d04afbb).
```

## Setup

```sh
pip install agentbus-cli
agentbus install
```

## Usage

Inside Claude Code it's a prompt mention: `@ki-acme-backend what's the status`. Inside the others it's a
tool call the model makes on its own (`send_to_claude`, `list_claude_sessions`,
`check_messages`). `agentbus install` looks at what's actually on your machine and wires each one
up: a config entry for MCP-based clients, a symlinked adapter for pi and opencode. Run `agentbus
doctor` afterward to check it took.

## Manual MCP setup

`agentbus install` is the automated version of this. For a host it doesn't recognize yet, or if
you'd rather wire it up yourself, any MCP host that can spawn a stdio server should point at:

```json
{
  "mcpServers": {
    "agentbus": {
      "command": "agentbus",
      "args": ["mcp"]
    }
  }
}
```

That's the shape once `agentbus-cli` is pip-installed. Running straight from a checkout instead
(no install step) needs `python3` as the command and `PYTHONPATH` pointing at the checkout, since
a spawned host process doesn't inherit your shell's environment:

```json
{
  "mcpServers": {
    "agentbus": {
      "command": "/path/to/python3",
      "args": ["-m", "agentbus", "mcp"],
      "env": { "PYTHONPATH": "/path/to/agentbus" }
    }
  }
}
```

The host's own name over MCP decides the address prefix (`ki-`, `cx-`, `ag-`, …), picked up
automatically from the standard MCP `initialize` handshake, or falls back to the generic `ab-`
prefix if the host isn't one we recognize yet (see the client table above). Set `AGENTBUS_NAME` in
that same `env` block to pick the name yourself instead of deriving it from the working directory.

Once it's wired up, the host gets three tools: `list_claude_sessions`, `send_to_claude`, and
`check_messages`. A host that runs its own hooks can also call `agentbus drain` from one to pull
messages automatically instead of waiting on the model to call `check_messages`; kiro's own config
(written by `agentbus install kiro`) does exactly this. See
[`docs/protocol.md`](docs/protocol.md) for the wire format underneath all of this.

## Why this exists

Claude Code sessions on the same machine can already message each other; it's built in, just
undocumented. `agentbus` reverse-engineers that protocol and speaks it from the outside, so a pi
or kiro or codex session looks like just another Claude Code session to everyone else. No new
protocol, no daemon, no server to run: it rides the one Claude Code already ships.

See [`docs/protocol.md`](docs/protocol.md) for the reverse-engineered wire format, if you want to
speak it directly or write your own adapter.

## Supported clients

| client | how | prefix |
|---|---|---|
| Claude Code | native, nothing to install | `cc-` |
| pi | adapter symlinked into its extensions dir | `pi-` |
| opencode | plugin shim + config entry | `oc-` |
| kiro-cli | MCP server + agent config (hooks don't run in its TUI, see below) | `ki-` |
| codex | MCP server, TOML config block | `cx-` |
| antigravity-cli | MCP server, JSON config | `ag-` |
| hermes (NousResearch) | MCP server, driven through `hermes mcp` | generic (see below) |
| any other MCP host | MCP server, config format varies | generic |

An unrecognized host still works, it just gets a generic prefix instead of a short one. Adding a
short prefix for a new host means confirming what it actually reports as its MCP client name
first: every host so far has surprised us here (kiro reports `Q DEV CLI`, codex reports
`codex-mcp-client`, hermes reports the unusably generic `mcp`), so this is never assumed. See
[`docs/roadmap.md`](docs/roadmap.md) for the client backlog and what's been ruled out.

## Delivery model

Claude Code, pi, and opencode can interrupt a running turn, so a message lands immediately. A
plain MCP host cannot be interrupted by anything: there is no MCP call that pushes into a live
turn. Messages to those hosts are spooled and either surfaced automatically by a host hook
(where the host actually runs hooks, which kiro's own TUI turns out not to, see
[kirodotdev/Kiro#11620](https://github.com/kirodotdev/Kiro/issues/11620)) or pulled by the model
calling `check_messages`, which every MCP-based client is told to do at the top of its turn.

## Security

This is a local IPC channel with the same trust boundary as anything else running as your user:
auth tokens are per-session, socket directories are mode 0700, and a sender's identity is checked
against the connecting process's own uid, not just what it claims. None of that changes what a
message actually is once it arrives: text injected into another agent's context, the same as
anything else that agent reads. Treat a peer session the way you'd treat any other untrusted input
source, don't wire up a peer you wouldn't want steering your other sessions, and don't build
anything on top of this that turns a received message into an action without a human somewhere in
the loop for anything destructive.

## Zero dependencies, on purpose

The Python side is stdlib only: this has to run on whatever python a host already has, not one you
install just for this. pi and opencode's adapters are TypeScript because their listener has to run
*inside* those hosts' own process to interrupt a live turn, which borrows the host's own runtime
rather than shipping one.

## Tests

```sh
python3 -m unittest discover -s tests -t .
cd adapters/ts && bun test
```

Both the Python and TypeScript implementations of the wire protocol are checked against the same
cases in `tests/vectors.json`, so a change to one that isn't mirrored in the other fails that
language's suite.

## License

MIT

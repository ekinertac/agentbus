# Claude Code's local session-messaging protocol

The feature itself, cross-session messaging via `SendMessage` and `ListAgents`, is real and
documented: it has its own entries in Anthropic's [CHANGELOG.md](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
going back a while. What isn't published anywhere is the wire format underneath it, which is what
this file covers. Everything here was worked out by reading the `claude` binary (version 2.1.278
through 2.1.283 at the time of writing) with `strings` and reading the minified JS it unpacks to,
then confirmed by building a second implementation (`agentbus/protocol.py`) against it and
watching real sessions talk to each other. It can change without notice on any Claude Code
release; `agentbus doctor` checks the `peerProtocol` field described below and flags a version it
doesn't recognize.

If you just want to use the messaging, you don't need this file, `agentbus install` does the work.
This is for writing a new adapter, or for understanding what's actually happening when two
sessions message each other.

## The three things on disk

A Claude Code session that wants to be reachable does three things:

1. Writes a registry entry to `~/.claude/sessions/<pid>.json`.
2. Listens on a unix socket at `/tmp/cc-socks/<pid>.sock`.
3. Publishes an auth token to `~/.claude/sessions/<pid>.<sha256 of the socket path>.key`.

Any process that does the same three things is, to Claude Code, just another session. That's the
whole trick.

### The registry entry

```json
{
  "pid": 52210,
  "sessionId": "b1e2c3d4-...",
  "cwd": "/Users/you/Code/myproject",
  "startedAt": 1790000000000,
  "procStart": "Fri Sep 26 14:30:00 2026",
  "version": "2.1.283",
  "peerProtocol": 1,
  "peerFeatures": [],
  "kind": "interactive",
  "entrypoint": "cli",
  "pidDomain": "darwin",
  "messagingSocketPath": "/tmp/cc-socks/52210.sock",
  "name": "myproject",
  "nameSource": "derived",
  "nameSince": 1790000000000,
  "status": "idle",
  "updatedAt": 1790000123456,
  "statusUpdatedAt": 1790000123456
}
```

Claude Code's own `ListAgents` and its `@name` prompt autocomplete read every file matching
`~/.claude/sessions/*.json`, skip anything that fails to parse, and skip any entry whose `pid`
isn't a live process on this machine (`kill(pid, 0)`). `entrypoint` is how Claude Code tells its
own sessions (`"cli"`) apart from everyone else's; `agentbus`'s MCP adapter reports `"agentbus"`,
the pi and opencode adapters report `"pi"` and `"opencode"`. Writes are atomic (write to a temp
file, then rename) since this directory can be read at any moment.

`procStart` matters more than it looks: it has to equal
`LC_ALL=C TZ=UTC ps -o lstart= -p <pid>` **byte for byte**. Claude Code string-compares it against
its own reading of the same command to decide whether a registry entry belongs to the live process
that wrote it, or to a stale file left by a pid that has since been recycled by the OS.

### The socket

`/tmp/cc-socks` must be mode `0700`, and the socket itself `0600`, or Claude Code won't trust
anything in it. The socket accepts newline-delimited JSON frames, described below. On macOS,
close the connection about 150ms after the last write rather than immediately: closing at once can
truncate the final frame.

### The auth key

The key file's name encodes which socket it authorizes: `<pid>.<sha256(socket_path)>.key`,
containing `{"peerToken": "<32 hex chars>", ...}`. A sender reads the target's key file, and its
first frame on the socket must be `{"type": "auth", "token": "<that token>"}` before anything else
is accepted. A connection that sends a user frame before authenticating, or with the wrong token,
gets dropped.

## The wire frames

Everything is one JSON object per line (`\n`-terminated), sent over the socket after the auth
frame:

```json
{"type": "user", "from": "uds:/tmp/cc-socks/52210.sock", "msg_id": "<uuid4>", "priority": "next", "message": {"content": "<envelope, see below>"}}
```

`priority` is one of `"now"`, `"next"`, `"later"`; `agentbus` always sends `"next"`. `from` is the
sender's own socket address in `uds:<path>` form, so a reply can be sent back to it. `msg_id` is
just a random UUID the sender generates; Claude Code echoes it back in delivery-status control
frames (`held`, `delivered`, `refused`, `dropped`, `expired`), which `agentbus` doesn't currently
read but a more advanced adapter could, to know whether a send actually landed.

### The envelope

The message body isn't the raw text: it's wrapped in a small XML-shaped envelope that carries the
sender's identity, because the `from` socket address alone isn't something a human (or a model)
wants to read.

```
<cross-session-message from="uds:/tmp/cc-socks/52210.sock" from-name="myproject" from-mode="bypass">
the actual message text goes here, can contain newlines
</cross-session-message>
```

`from-name` is the sender's display name, with `"`, `<`, `>` stripped so it can't break out of the
attribute. `from-mode` is the one field that actually gates delivery, covered next.

## Permission-mode parity

This is the part that's easy to miss and causes messages to silently vanish (well, not silently:
they're held for the receiving user to approve, but a sender that doesn't know this happens reads
that as "message never arrived").

Claude Code enforces that a peer message's asserted `from-mode` matches the **receiver's own**
permission class, one of `"bypass"` (`--dangerously-skip-permissions`, or `--permission-mode
bypassPermissions`) or `"prompting"` (anything else, including plan mode). A mismatch, or the
absence of a claim, parks the message as **held** until the receiving user reviews and approves it
manually, rather than delivering it or rejecting it outright.

`agentbus` has no permission mode of its own, so it asserts whatever the **target** is running in.
That means reading the target's live mode before every send:

1. **First choice: the target's own session transcript**, at
   `~/.claude/projects/<slug>/<sessionId>.jsonl`. Each entry that mentions `"permissionMode"`
   records the mode active *at that point*, so the last one in the file reflects a runtime switch
   (shift-tab into plan mode, say) that a stale read of the launch command would miss entirely.
2. **Fallback: the target's command line**, via `ps -o command= -p <pid>`, checked for
   `--dangerously-skip-permissions` or `--permission-mode bypassPermissions`. Used when there's no
   transcript yet, or the pid isn't a Claude Code session with a `sessionId` in the first place.
3. **Last resort: assume bypass.** An unreadable target (permissions, timing) shouldn't
   *guarantee* a hold; bypass is the common case in practice.

Two bugs found in exactly this logic while building `agentbus`, both now covered by tests: a
transient failure reading any single entry under `~/.claude/projects` (sessions write there
constantly) used to abort the whole scan and silently downgrade a bypass peer to `prompting`; and
the `"permissionMode"` field match only handled Claude's exact compact-JSON spacing, so a
differently-formatted transcript line would have matched nothing.

## What a listener has to do

Bind the socket, accept connections, and for each one: read newline-delimited JSON, require an
`{"type":"auth",...}` frame first with the correct token, then treat `{"type":"user",...}` frames
as inbound messages (parse the envelope back apart to recover the sender's name and address for a
reply) and ignore anything else (control frames sent *to* a listener, like delivery receipts, only
matter to a sender, not a receiver). `agentbus/protocol.py`'s `listen()` is the reference
implementation; `adapters/ts/core.ts`'s `listen()` is the same thing in TypeScript for hosts that
have to run the listener inside their own process to interrupt a live turn.

## Naming and prefixes

Claude Code sessions pick their own name however they like (usually derived from the directory).
Everything `agentbus` puts on the bus prefixes its name so a human or model can tell at a glance
what kind of session they're addressing: `pi-`, `oc-`, `ki-`, `cx-`, `ag-`. A Claude Code session
gets addressed as `cc-<name>` *from* one of those, even though its own registry entry carries no
prefix at all, since from outside Claude Code it's just one more kind of peer.

Never assume a host's own binary or package name is what it reports as its MCP `clientInfo.name`;
every host checked so far has been surprising here (see the table in the README). A host that
reports something too generic to trust (Hermes Agent reports literally `"mcp"`) is left unmapped
on purpose rather than guessed at, since a wrong guess risks misattributing some other, unrelated
future host under the same short prefix.

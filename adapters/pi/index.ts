/**
 * The agentbus pi extension: makes this pi session a peer of every other agent on this machine.
 *
 * What it does:
 * - Registers in ~/.claude/sessions as `pi-<session name|cwd basename>` and listens on
 *   /tmp/cc-socks/<pid>.sock, so Claude's ListAgents shows it, `@pi-foo` autocompletes in
 *   Claude's prompt, and Claude's SendMessage lands here as a user message.
 * - Exposes a `send_to_claude` tool and a `list_claude_sessions` tool so pi can message any
 *   live Claude (or pi) session by name. From pi, Claude sessions are addressed as
 *   `cc-<name>` (mirror of the `pi-` prefix Claude sees), pi sessions as `pi-<name>`.
 * - Rewrites `@name` mentions in your prompt into a hint so the model reaches for the tool,
 *   and completes `@` with live session names (falls through to pi's file completion when
 *   the token matches no session, so `@src/` keeps working).
 *
 * Unlike the MCP adapter, pi can be interrupted: an inbound message is pushed straight into the
 * session with sendUserMessage, so nothing is spooled and nothing has to be polled.
 *
 * Protocol and file formats live in core.ts (pure, tested with `bun test`). This file is only pi
 * glue. `agentbus install pi` symlinks this directory into pi's extensions folder; a wrapper that
 * sets PI_CODING_AGENT_DIR keeps its extensions elsewhere, so pass --agent-dir for each one.
 *
 * Nothing here can break a turn: every filesystem/socket failure is caught and reported
 * with ctx.ui.notify, then the extension keeps running without peer messaging.
 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { AutocompleteProvider } from "@earendil-works/pi-tui";
import { Type } from "typebox";
import * as os from "node:os";
import * as net from "node:net";
import {
  deriveName, displayName, listen, newToken, peers, procStart, readRegistry, removeFiles, resolveTarget,
  sendUser, sockPath, writeKey, writeRegistry, PEER_PROTOCOL, type Registry,
} from "./core";

export default function (pi: ExtensionAPI) {
  const pid = process.pid;
  const sock = sockPath(pid);
  let server: net.Server | undefined;
  let reg: Registry | undefined;
  let lastFrom: string | undefined; // reply address of the most recent inbound message

  const save = (patch: Partial<Registry>) => {
    if (!reg) return;
    const now = Date.now();
    reg = { ...reg, ...patch, updatedAt: now, ...(patch.status ? { statusUpdatedAt: now } : {}) };
    try { writeRegistry(reg); } catch {}
  };

  const currentName = (ctx: ExtensionContext) => {
    const taken = new Set(readRegistry().filter((r) => r.pid !== pid).map((r) => r.name));
    return deriveName(ctx.cwd, pi.getSessionName(), taken);
  };

  const start = (ctx: ExtensionContext) => {
    if (server) return;
    try {
      const token = newToken();
      server = listen(sock, token, {
        onLog: (l) => ctx.ui.notify(l, "warning"),
        onMessage: (m) => {
          lastFrom = m.from;
          const who = m.fromName ? `${m.fromName} (${m.from ?? "?"})` : (m.from ?? "unknown peer");
          const replyTo = m.fromName ? (m.fromName.startsWith("pi-") ? m.fromName : `cc-${m.fromName}`) : (m.from ?? "");
          pi.sendUserMessage(
            `[Message from Claude Code session ${who}]\n${m.body}\n\n(Reply with the send_to_claude tool, to: "${replyTo}".)`,
            { deliverAs: "followUp" },
          );
        },
      });
      server.on("error", (e) => ctx.ui.notify(`agentbus listener error: ${e.message}`, "warning"));
      writeKey(pid, sock, token);
      const now = Date.now();
      reg = {
        pid, sessionId: ctx.sessionManager.getSessionId(), cwd: ctx.cwd, startedAt: now, procStart: procStart(pid),
        version: "pi", peerProtocol: PEER_PROTOCOL, peerFeatures: [], kind: "interactive", entrypoint: "pi",
        pidDomain: process.platform, messagingSocketPath: sock, name: currentName(ctx),
        nameSource: pi.getSessionName() ? "user" : "derived", nameSince: now,
        status: ctx.isIdle() ? "idle" : "busy", updatedAt: now, statusUpdatedAt: now,
      };
      writeRegistry(reg);
    } catch (e) {
      ctx.ui.notify(`agentbus: peer messaging off (${(e as Error).message})`, "warning");
      stop();
    }
  };

  const stop = () => {
    try { server?.close(); } catch {}
    server = undefined;
    removeFiles(pid, sock);
    reg = undefined;
  };

  const sessionCompletions = (current: AutocompleteProvider): AutocompleteProvider => ({
    async getSuggestions(lines, cursorLine, cursorCol, options) {
      const before = (lines[cursorLine] ?? "").slice(0, cursorCol);
      const m = before.match(/(?:^|\s)@([A-Za-z0-9._-]*)$/);
      if (m) {
        const q = m[1].toLowerCase();
        const items = peers(pid)
          .map((r) => ({ r, n: displayName(r) }))
          .filter(({ n }) => n.toLowerCase().startsWith(q))
          .map(({ r, n }) => ({ value: `@${n} `, label: `@${n}`, description: `${r.status ?? "?"}  ${r.cwd}` }));
        if (items.length > 0) return { items, prefix: `@${m[1]}` };
      }
      return current.getSuggestions(lines, cursorLine, cursorCol, options);
    },
    applyCompletion: (lines, l, c, item, prefix) => current.applyCompletion(lines, l, c, item, prefix),
    shouldTriggerFileCompletion: (lines, l, c) => current.shouldTriggerFileCompletion?.(lines, l, c) ?? true,
  });

  pi.on("session_start", (_e, ctx) => {
    stop();
    start(ctx);
    if (ctx.hasUI) ctx.ui.addAutocompleteProvider(sessionCompletions);
  });
  pi.on("session_shutdown", () => stop());
  pi.on("session_info_changed", (_e, ctx) => save({ name: currentName(ctx), nameSource: pi.getSessionName() ? "user" : "derived", nameSince: Date.now() }));
  pi.on("agent_start", () => save({ status: "busy" }));
  pi.on("agent_settled", () => save({ status: "idle" }));

  // `@code-cc do X` in the prompt: the model gets a nudge to use the tool, like Claude does for @-mentions.
  pi.on("input", (e) => {
    if (e.source !== "interactive") return { action: "continue" as const };
    const names = new Set(peers(pid).map(displayName));
    const hit = [...e.text.matchAll(/(?:^|\s)@([A-Za-z0-9._-]{1,64})/g)].map((m) => m[1]).filter((n) => names.has(n));
    if (hit.length === 0) return { action: "continue" as const };
    const quoted = hit.map((n) => `"${n}"`).join(", ");
    return {
      action: "transform" as const,
      text: `${e.text}\n\n[agentbus] The text above is your user's own prompt (not an incoming message). It @-mentions the live session(s) ${quoted}. If it is addressed to that session (tell it, ask it, relay to it), call send_to_claude now with to=${quoted} and the message text, then report that you sent it. Incoming messages from other sessions always start with "[Message from Claude Code session".`,
    };
  });

  pi.registerTool({
    name: "list_claude_sessions",
    label: "List peer sessions",
    description: "List live Claude Code and pi sessions on this machine that can receive messages (name, status, cwd).",
    promptSnippet: "list_claude_sessions: see which Claude Code / pi sessions are reachable by name",
    parameters: Type.Object({}),
    async execute() {
      const rows = peers(pid).map((r) => `${displayName(r)}  [${r.status ?? "?"}]  ${r.cwd}`);
      return { content: [{ type: "text", text: rows.join("\n") || "(no live peer sessions)" }], details: undefined };
    },
  });

  pi.registerTool({
    name: "send_to_claude",
    label: "Message a Claude session",
    description: "Send a message to a live Claude Code (or pi) session on this machine by name. The reply arrives here as a user message. Omit `to` to answer the session that last messaged you.",
    promptSnippet: "send_to_claude: message another Claude Code / pi session by name; replies come back as user messages",
    parameters: Type.Object({
      to: Type.Optional(Type.String({ description: "Session name from list_claude_sessions: cc-<name> for Claude Code sessions, pi-<name> for pi sessions, or a uds: address. Default: last sender." })),
      message: Type.String({ description: "The message body" }),
    }),
    async execute(_id, params, _signal, _update, ctx) {
      const to = params.to?.trim() || lastFrom;
      if (!to) return { content: [{ type: "text", text: "No `to` given and nobody has messaged this session yet." }], details: undefined, isError: true };
      const t = resolveTarget(to, pid);
      if ("error" in t) return { content: [{ type: "text", text: t.error }], details: undefined, isError: true };
      if (!server || !reg) return { content: [{ type: "text", text: "agentbus listener is not running, so no reply could come back. Restart pi." }], details: undefined, isError: true };
      try {
        const id = await sendUser(t.sock, { sock, name: reg.name }, params.message);
        return { content: [{ type: "text", text: `Sent to ${t.name} (msg ${id}). Its reply, if any, arrives as a user message.` }], details: undefined };
      } catch (e) {
        return { content: [{ type: "text", text: `Send to ${t.name} failed: ${(e as Error).message}` }], details: undefined, isError: true };
      }
    },
  });
}

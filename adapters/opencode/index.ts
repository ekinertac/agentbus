/**
 * The agentbus opencode plugin: makes this opencode process a peer of every other agent here.
 *
 * Same idea as the pi adapter: register in ~/.claude/sessions as `oc-<cwd basename>`, listen on
 * /tmp/cc-socks/<pid>.sock, and expose send_to_claude / list_claude_sessions. Claude's ListAgents,
 * `@oc-foo` autocomplete and SendMessage then work against opencode unchanged. The protocol lives
 * in the shared TypeScript core; this file is opencode glue.
 *
 * opencode differences that shape this file:
 * - One server process hosts many sessions, but the wire protocol is per process (Claude verifies
 *   the connecting pid). So there is ONE registry entry per opencode process and inbound messages
 *   go to the session that was most recently active (last user message / status change), falling
 *   back to the newest session on the server. Status is "busy" if any session is busy.
 * - No TUI autocomplete hook exists in the plugin API, so `@cc-name` has no completion popup here;
 *   the `chat.message` hook still appends the send nudge when a user message mentions a live peer.
 * - Delivery uses session.promptAsync through the SDK client, which queues if the session is busy.
 *
 * Loading: `@opencode-ai/plugin` resolves only from files under ~/.config/opencode, because that is
 * where opencode installs it, and this file lives in the agentbus checkout. So the module exports a
 * FACTORY taking `tool`, and `agentbus install opencode` writes a three-line shim under
 * ~/.config/opencode/plugin that imports both and exports the result. The type imports below are
 * erased at runtime and never resolved. That directory is not auto-scanned either, so the installer
 * also adds the file to the `plugin` array in opencode.json.
 *
 * Failures are logged with a toast and the plugin keeps running without peer messaging.
 */
import type { Plugin, tool as ToolFn } from "@opencode-ai/plugin";
import * as net from "node:net";
import {
  deriveName, displayName, listen, newToken, peers, procStart, readRegistry, removeFiles, resolveTarget,
  sendUser, sockPath, writeKey, writeRegistry, PEER_PROTOCOL, type Registry,
} from "../ts/core";

export default function make(tool: typeof ToolFn): Plugin {
  return async ({ client, directory }) => {
  const pid = process.pid;
  const sock = sockPath(pid);
  let server: net.Server | undefined;
  let reg: Registry | undefined;
  let lastFrom: string | undefined;
  let activeSession: string | undefined;
  const busy = new Set<string>();

  const toast = (message: string, variant: "info" | "warning" | "error" = "warning") =>
    client.tui.showToast({ body: { title: "agentbus", message, variant } }).catch(() => {});

  const save = (patch: Partial<Registry>) => {
    if (!reg) return;
    const now = Date.now();
    reg = { ...reg, ...patch, updatedAt: now, ...(patch.status ? { statusUpdatedAt: now } : {}) };
    try { writeRegistry(reg); } catch {}
  };

  const targetSession = async (): Promise<string | undefined> => {
    if (activeSession) return activeSession;
    const res = await client.session.list().catch(() => undefined);
    const list = (res?.data ?? []).filter((s) => !s.parentID).sort((a, b) => b.time.updated - a.time.updated);
    return list[0]?.id;
  };

  const start = () => {
    try {
      const token = newToken();
      server = listen(sock, token, {
        onLog: (l) => toast(l),
        onMessage: async (m) => {
          lastFrom = m.from;
          const who = m.fromName ? `${m.fromName} (${m.from ?? "?"})` : (m.from ?? "unknown peer");
          const replyTo = m.fromName ? (/^(pi|oc)-/.test(m.fromName) ? m.fromName : `cc-${m.fromName}`) : (m.from ?? "");
          const id = await targetSession();
          if (!id) { toast(`message from ${who} dropped: no session to deliver to`, "error"); return; }
          const text = `[Message from Claude Code session ${who}]\n${m.body}\n\n(Reply with the send_to_claude tool, to: "${replyTo}".)`;
          const r = await client.session.promptAsync({ path: { id }, body: { parts: [{ type: "text", text }] } }).catch((e) => ({ error: e }));
          if ((r as any)?.error) toast(`delivery to session ${id} failed: ${String((r as any).error)}`, "error");
        },
      });
      server.on("error", (e) => toast(`listener error: ${e.message}`, "error"));
      writeKey(pid, sock, token);
      const now = Date.now();
      const taken = new Set(readRegistry().filter((r) => r.pid !== pid).map((r) => r.name));
      reg = {
        pid, sessionId: `opencode-${pid}`, cwd: directory, startedAt: now, procStart: procStart(pid),
        version: "opencode", peerProtocol: PEER_PROTOCOL, peerFeatures: [], kind: "interactive", entrypoint: "opencode",
        pidDomain: process.platform, messagingSocketPath: sock, name: deriveName(directory, undefined, taken, "oc-"),
        nameSource: "derived", nameSince: now, status: "idle", updatedAt: now, statusUpdatedAt: now,
      };
      writeRegistry(reg);
    } catch (e) {
      toast(`peer messaging off (${(e as Error).message})`);
      stop();
    }
  };

  const stop = () => {
    try { server?.close(); } catch {}
    server = undefined;
    removeFiles(pid, sock);
    reg = undefined;
  };

  start();
  process.once("exit", stop);
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"] as const) process.once(sig, () => { stop(); process.exit(); });

  return {
    async event({ event }) {
      if (event.type === "session.status") {
        const { sessionID, status } = event.properties;
        if (status.type === "busy" || status.type === "retry") { busy.add(sessionID); activeSession = sessionID; }
        else busy.delete(sessionID);
        save({ status: busy.size > 0 ? "busy" : "idle" });
      } else if (event.type === "session.idle") {
        busy.delete(event.properties.sessionID);
        save({ status: busy.size > 0 ? "busy" : "idle" });
      } else if (event.type === "message.updated" && event.properties.info.role === "user") {
        activeSession = event.properties.info.sessionID;
      } else if (event.type === "session.created" && !event.properties.info.parentID) {
        activeSession = event.properties.info.id;
      } else if (event.type === "server.instance.disposed") {
        stop();
      }
    },

    // `@cc-foo do X` typed by the user: nudge the model toward the tool, as Claude does for @-mentions.
    async "chat.message"(_input, output) {
      const text = output.parts.filter((p) => p.type === "text").map((p) => (p as any).text as string).join("\n");
      const names = new Set(peers(pid).map(displayName));
      const hit = [...text.matchAll(/(?:^|\s)@([A-Za-z0-9._-]{1,64})/g)].map((m) => m[1]).filter((n) => names.has(n));
      if (hit.length === 0) return;
      const quoted = hit.map((n) => `"${n}"`).join(", ");
      output.parts.push({
        id: `agentbus-hint-${Date.now()}`,
        sessionID: output.message.sessionID,
        messageID: output.message.id,
        type: "text",
        text: `[agentbus] The text above is your user's own prompt (not an incoming message). It @-mentions the live session(s) ${quoted}. If it is addressed to that session (tell it, ask it, relay to it), call send_to_claude now with to=${quoted} and the message text, then report that you sent it. Incoming messages from other sessions always start with "[Message from Claude Code session".`,
        synthetic: true,
      } as any);
    },

    tool: {
      list_claude_sessions: tool({
        description: "List live Claude Code, pi and opencode sessions on this machine that can receive messages (name, status, cwd).",
        args: {},
        async execute() {
          const rows = peers(pid).map((r) => `${displayName(r)}  [${r.status ?? "?"}]  ${r.cwd}`);
          return rows.join("\n") || "(no live peer sessions)";
        },
      }),
      send_to_claude: tool({
        description: "Send a message to a live Claude Code (cc-<name>), pi (pi-<name>) or opencode (oc-<name>) session on this machine. The reply arrives here as a user message. Omit `to` to answer the session that last messaged you.",
        args: {
          to: tool.schema.string().optional().describe("Session name from list_claude_sessions, or a uds: address. Default: last sender."),
          message: tool.schema.string().describe("The message body"),
        },
        async execute(args, ctx) {
          activeSession = ctx.sessionID;
          const to = args.to?.trim() || lastFrom;
          if (!to) return "Error: no `to` given and nobody has messaged this session yet.";
          const t = resolveTarget(to, pid);
          if ("error" in t) return `Error: ${t.error}`;
          if (!server || !reg) return "Error: agentbus listener is not running, so no reply could come back. Restart opencode.";
          try {
            const id = await sendUser(t.sock, { sock, name: reg.name }, args.message);
            return `Sent to ${t.name} (msg ${id}). Its reply, if any, arrives as a user message.`;
          } catch (e) {
            return `Error: send to ${t.name} failed: ${(e as Error).message}`;
          }
        },
      }),
    },
  };
  };
}

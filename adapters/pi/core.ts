/**
 * The Claude Code session-bus protocol, in TypeScript, for hosts that load a JS plugin.
 *
 * Claude Code sessions talk to each other over unix sockets in /tmp/cc-socks/<pid>.sock,
 * find each other through ~/.claude/sessions/<pid>.json, and authenticate with a token
 * published in ~/.claude/sessions/<pid>.<sha256(socket path)>.key. Nothing here is
 * documented; it was read out of the claude 2.1.278 binary. A pi session that follows
 * the same three files is, to Claude, just another session: it shows in ListAgents,
 * `@pi-foo` autocompletes in the prompt, and SendMessage reaches it.
 *
 * This is the second implementation of what agentbus/protocol.py does, and it exists only because
 * a host that can push a message into a live turn has to run the listener inside its own process.
 * The two are held to the same cases in tests/vectors.json, so a change to one that is not
 * mirrored in the other fails the other language's suite.
 *
 * Pure protocol and filesystem, no pi imports, so `bun test` can drive it. index.ts wires it into
 * pi's event and tool API.
 *
 * Constraints that came from the binary, keep them:
 * - procStart must equal `LC_ALL=C TZ=UTC ps -o lstart= -p <pid>` verbatim; Claude
 *   compares it as a string before trusting a registry entry.
 * - Frames are newline-delimited JSON. First frame `{"type":"auth","token"}` when the
 *   target has a key file, then `{"type":"user",...}`.
 * - `from-mode` must MATCH the target's permission class or the message is parked as "held":
 *   see peerMode() below. We sniff it from the target's command line per send.
 * - Claude verifies the sending pid via SO_PEERCRED against the pid in `from`, so the
 *   socket write must come from the pi process itself, not a helper script.
 * - Socket dir must be mode 0700; reply targets must not be symlinks.
 * - Claude on macOS holds the connection ~150ms after writing, then ends it. Match that.
 */
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";

export const SOCK_DIR = "/tmp/cc-socks";
export const SESSIONS_DIR = path.join(os.homedir(), ".claude", "sessions");
export const TAG = "cross-session-message";
export const PEER_PROTOCOL = 1;
const LINGER_MS = 150;
const MAX_FRAME = 1 << 20;

export type Registry = {
  pid: number;
  sessionId: string;
  cwd: string;
  startedAt: number;
  procStart: string;
  version: string;
  peerProtocol: number;
  peerFeatures: string[];
  kind: "interactive";
  entrypoint: string;
  pidDomain: string;
  messagingSocketPath: string;
  name: string;
  nameSource: "derived" | "user";
  nameSince: number;
  status: "idle" | "busy";
  updatedAt: number;
  statusUpdatedAt: number;
};

export type Inbound = { from?: string; fromName?: string; fromMode?: string; body: string; msgId?: string };

export function sockPath(pid: number): string {
  return `${SOCK_DIR}/${pid}.sock`;
}

export function keyPath(pid: number, sock: string): string {
  return path.join(SESSIONS_DIR, `${pid}.${createHash("sha256").update(sock).digest("hex")}.key`);
}

export function registryPath(pid: number): string {
  return path.join(SESSIONS_DIR, `${pid}.json`);
}

export function procStart(pid: number): string {
  return execFileSync("ps", ["-o", "lstart=", "-p", String(pid)], {
    env: { ...process.env, LC_ALL: "C", TZ: "UTC" },
    encoding: "utf8",
    timeout: 1000,
  }).trim();
}

export function isAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** <prefix><session name> when the user named the session, else <prefix><cwd basename>. Suffix -2, -3 on collision with a live registry entry. */
export function deriveName(cwd: string, sessionName: string | undefined, taken: Set<string>, prefix = "pi-"): string {
  const base = prefix + (sessionName?.trim() || path.basename(cwd)).replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
  if (!taken.has(base)) return base;
  for (let i = 2; ; i++) if (!taken.has(`${base}-${i}`)) return `${base}-${i}`;
}

export function readRegistry(): Registry[] {
  let files: string[];
  try {
    files = fs.readdirSync(SESSIONS_DIR).filter((f) => /^\d+\.json$/.test(f));
  } catch {
    return [];
  }
  const out: Registry[] = [];
  for (const f of files) {
    try {
      const r = JSON.parse(fs.readFileSync(path.join(SESSIONS_DIR, f), "utf8")) as Registry;
      if (typeof r.pid === "number" && typeof r.messagingSocketPath === "string" && isAlive(r.pid)) out.push(r);
    } catch {
      /* half-written or foreign file: skip */
    }
  }
  return out;
}

/** Live sessions other than ourselves, for listing and name resolution. */
export function peers(selfPid: number): Registry[] {
  return readRegistry().filter((r) => r.pid !== selfPid && !!r.name);
}

/** Agents that write their own prefix into the registry name: pi- / oc- / ki-. Anything else in the registry is a Claude Code session. */
const PREFIXED_AGENTS = new Set(["pi", "opencode", "kiro", "agentbus"]);

/** How a peer is addressed from a non-Claude agent: Claude sessions get a `cc-` prefix, our own agents already carry theirs. */
export function displayName(r: Pick<Registry, "name" | "entrypoint">): string {
  return PREFIXED_AGENTS.has(r.entrypoint ?? "") ? r.name : `cc-${r.name}`;
}

/** Resolve a display name (`cc-foo`, `pi-bar`), raw registry name, `uds:` address or socket path to a socket path. Exact first, then case-insensitive, then unique prefix. */
export function resolveTarget(to: string, selfPid: number): { sock: string; name: string } | { error: string } {
  const t = to.trim().replace(/^@/, "");
  if (t.startsWith("uds:")) return { sock: decodeURIComponent(t.slice(4)), name: t };
  if (t.startsWith("/")) return { sock: t, name: t };
  const live = peers(selfPid);
  const lc = t.toLowerCase();
  const namesOf = (r: Registry) => [displayName(r), r.name];
  const exact = live.filter((r) => namesOf(r).includes(t));
  const ci = exact.length ? exact : live.filter((r) => namesOf(r).some((n) => n.toLowerCase() === lc));
  const pre = ci.length ? ci : live.filter((r) => namesOf(r).some((n) => n.toLowerCase().startsWith(lc)));
  if (pre.length === 1) return { sock: pre[0].messagingSocketPath, name: displayName(pre[0]) };
  if (pre.length === 0) return { error: `No live session named "${t}". Known: ${live.map(displayName).join(", ") || "(none)"}` };
  return { error: `"${t}" matches ${pre.length} sessions: ${pre.map((r) => `${displayName(r)} [${r.pid}]`).join(", ")}. Use the full name.` };
}

/**
 * Claude enforces permission-mode PARITY on inbound peer messages: a claim that does not match the
 * receiver's own class is parked as "held" until its user approves it, and so is a message with no
 * claim at all when the receiver bypasses. We are not a Claude session and have no mode of our own,
 * so we assert whatever the target is in.
 *
 * Two sources, in order. The session transcript (~/.claude/projects/<slug>/<sessionId>.jsonl) records
 * `permissionMode` on each entry and so follows a shift-tab switch into plan mode, which is what a
 * stale command-line read gets wrong; the command line (`--dangerously-skip-permissions`) is the
 * fallback for a session that has not written a transcript entry yet. Anything that is not
 * bypassPermissions counts as "prompting" - the enum has only the two classes.
 */
export type PeerMode = "bypass" | "prompting";

const MODE_TAIL_BYTES = 256 * 1024;

function modeFromTranscript(sessionId: string): PeerMode | undefined {
  let file: string | undefined;
  let dirs: string[];
  try {
    dirs = fs.readdirSync(path.join(os.homedir(), ".claude", "projects"));
  } catch {
    return undefined;
  }
  // One unreadable or vanishing project directory must not abort the search: sessions write
  // under ~/.claude/projects constantly, and a scan that dies on the first error silently
  // downgrades a bypass peer to prompting, which gets the message held.
  for (const dir of dirs) {
    try {
      const candidate = path.join(os.homedir(), ".claude", "projects", dir, `${sessionId}.jsonl`);
      if (fs.existsSync(candidate)) { file = candidate; break; }
    } catch {
      continue;
    }
  }
  if (!file) return undefined;
  try {
    const { size } = fs.statSync(file);
    const start = Math.max(0, size - MODE_TAIL_BYTES);
    const fd = fs.openSync(file, "r");
    const buf = Buffer.alloc(size - start);
    fs.readSync(fd, buf, 0, buf.length, start);
    fs.closeSync(fd);
    const hits = [...buf.toString("utf8").matchAll(/"permissionMode"\s*:\s*"([a-zA-Z]+)"/g)];
    const last = hits.at(-1)?.[1];
    return last === undefined ? undefined : last === "bypassPermissions" ? "bypass" : "prompting";
  } catch {
    return undefined;
  }
}

function modeFromCommandLine(pid: number): PeerMode | undefined {
  try {
    const cmd = execFileSync("ps", ["-o", "command=", "-p", String(pid)], { encoding: "utf8", timeout: 1000 });
    return /--dangerously-skip-permissions|--permission-mode[= ]+bypassPermissions/.test(cmd) ? "bypass" : "prompting";
  } catch {
    return undefined;
  }
}

export function peerMode(pid: number, sessionId?: string): PeerMode {
  const sid = sessionId ?? readRegistry().find((r) => r.pid === pid)?.sessionId;
  // A peer of ours (pi/oc/ki) ignores the field entirely, so only Claude's uuid sessions are looked up.
  if (sid && /^[0-9a-f-]{36}$/.test(sid)) {
    const fromTranscript = modeFromTranscript(sid);
    if (fromTranscript) return fromTranscript;
  }
  // Unreadable: assume the common case here rather than guaranteeing a hold.
  return modeFromCommandLine(pid) ?? "bypass";
}

/** /tmp/cc-socks/<pid>.sock is the only shape Claude accepts, so the owner's pid is in the name. */
export function pidOfSocket(sock: string): number | undefined {
  const m = path.basename(sock).match(/^(\d+)(?:-[0-9a-f]{8})?\.sock$/);
  return m ? Number(m[1]) : undefined;
}

export function buildEnvelope(from: string, fromName: string, body: string, mode: PeerMode = "bypass"): string {
  const name = fromName.replace(/["<>]/g, "");
  return `<${TAG} from="${from}" from-name="${name}" from-mode="${mode}">\n${body}\n</${TAG}>`;
}

/** Claude wraps its outgoing body the same way; strip it so pi sees the text and knows the reply address. */
export function parseEnvelope(content: string): Inbound {
  const m = content.match(new RegExp(`^<${TAG}((?:\\s+[a-z-]+="[^"]*")*)>\\n([\\s\\S]*)\\n</${TAG}>$`));
  if (!m) return { body: content };
  const attrs: Record<string, string> = {};
  for (const a of m[1].matchAll(/([a-z-]+)="([^"]*)"/g)) attrs[a[1]] = a[2];
  return { from: attrs["from"], fromName: attrs["from-name"], fromMode: attrs["from-mode"], body: m[2] };
}

export function readToken(sock: string): string | undefined {
  // Key files are named <pid>.<hash>.key; the pid is whatever the listener's pid is, so scan by hash.
  const hash = createHash("sha256").update(sock).digest("hex");
  let files: string[];
  try {
    files = fs.readdirSync(SESSIONS_DIR).filter((f) => f.endsWith(`.${hash}.key`));
  } catch {
    return undefined;
  }
  for (const f of files) {
    try {
      const k = JSON.parse(fs.readFileSync(path.join(SESSIONS_DIR, f), "utf8"));
      if (typeof k.peerToken === "string") return k.peerToken;
    } catch {
      /* skip */
    }
  }
  return undefined;
}

/** Send one user message to a socket. Resolves once the peer closes or after the macOS linger. */
export function sendUser(sock: string, self: { sock: string; name: string }, body: string): Promise<string> {
  const token = readToken(sock);
  const from = `uds:${self.sock}`;
  const msgId = randomUUID();
  const targetPid = pidOfSocket(sock);
  const mode = targetPid === undefined ? "bypass" : peerMode(targetPid);
  const frames = [
    ...(token ? [{ type: "auth", token }] : []),
    { type: "user", from, msg_id: msgId, priority: "next", message: { content: buildEnvelope(from, self.name, body, mode) } },
  ];
  const wire = frames.map((f) => JSON.stringify(f) + "\n").join("");
  return new Promise((resolve, reject) => {
    const c = net.createConnection({ path: sock });
    let done = false;
    const finish = (err?: Error) => {
      if (done) return;
      done = true;
      c.destroy();
      err ? reject(err) : resolve(msgId);
    };
    c.setTimeout(5000, () => finish(new Error(`timed out sending to ${sock}`)));
    c.on("error", (e) => finish(e));
    c.on("connect", () => {
      c.write(wire, () => setTimeout(() => { if (!c.destroyed) c.end(); }, LINGER_MS));
    });
    c.on("close", () => finish());
  });
}

export type ServerHandlers = { onMessage: (m: Inbound) => void; onLog?: (line: string) => void };

/** Listen like a Claude session: auth frame gates the connection, user frames become messages, control frames are ignored. */
export function listen(sock: string, token: string, h: ServerHandlers): net.Server {
  const server = net.createServer((conn) => {
    let buf = "";
    let authed = false;
    conn.on("data", (chunk) => {
      buf += chunk.toString("utf8");
      if (buf.length > MAX_FRAME) return conn.destroy();
      let nl: number;
      while ((nl = buf.indexOf("\n")) !== -1) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        let f: any;
        try {
          f = JSON.parse(line);
        } catch {
          h.onLog?.(`agentbus: dropped non-JSON frame`);
          continue;
        }
        if (f?.type === "auth") {
          authed = typeof f.token === "string" && f.token === token;
          if (!authed) { h.onLog?.("agentbus: bad auth token, closing"); conn.destroy(); }
          continue;
        }
        if (!authed) { h.onLog?.("agentbus: frame before auth, closing"); conn.destroy(); return; }
        if (f?.type === "user" && typeof f.message?.content === "string") {
          const parsed = parseEnvelope(f.message.content);
          h.onMessage({ ...parsed, from: parsed.from ?? f.from, msgId: typeof f.msg_id === "string" ? f.msg_id : undefined });
        }
        // control frames (peer_message_status receipts, notify_when_idle) carry nothing pi needs
      }
    });
    conn.on("error", () => {});
  });
  fs.mkdirSync(SOCK_DIR, { recursive: true, mode: 0o700 });
  try { fs.unlinkSync(sock); } catch {}
  server.listen(sock, () => fs.chmodSync(sock, 0o600));
  return server;
}

export function newToken(): string {
  return randomBytes(16).toString("hex");
}

export function writeKey(pid: number, sock: string, token: string): string {
  fs.mkdirSync(SESSIONS_DIR, { recursive: true, mode: 0o700 });
  const p = keyPath(pid, sock);
  fs.writeFileSync(p, JSON.stringify({ peerToken: token, procStart: procStart(pid), pidDomain: process.platform }), { mode: 0o600 });
  return p;
}

export function writeRegistry(r: Registry): void {
  const p = registryPath(r.pid);
  const tmp = `${p}.tmp.${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(r), { mode: 0o644 });
  fs.renameSync(tmp, p);
}

export function removeFiles(pid: number, sock: string): void {
  for (const p of [sock, keyPath(pid, sock), registryPath(pid)]) {
    try { fs.unlinkSync(p); } catch {}
  }
}

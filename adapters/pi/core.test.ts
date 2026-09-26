/**
 * The TypeScript core, checked against the same cases as the Python one plus the behaviour only a
 * live socket shows.
 *
 * Every shared case comes from tests/vectors.json. That file is the contract between the two
 * implementations: a change to one language that is not mirrored in the other fails here.
 *
 * Run: bun test   (bun is a development dependency only; users get pi's own runtime)
 */
import { describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  buildEnvelope, deriveName, displayName, keyPath, listen, parseEnvelope, peerMode, pidOfSocket,
  procStart, sendUser, sockPath, type Inbound,
} from "./core";

const vectors = JSON.parse(
  fs.readFileSync(path.join(import.meta.dir, "..", "..", "tests", "vectors.json"), "utf8"),
);

describe("shared vectors", () => {
  test("build_envelope", () => {
    for (const c of vectors.build_envelope) {
      expect(buildEnvelope(c.from, c.name, c.body, c.mode)).toBe(c.wire);
    }
  });

  test("parse_envelope", () => {
    for (const c of vectors.parse_envelope) {
      const got = parseEnvelope(c.content) as Record<string, unknown>;
      for (const [key, want] of Object.entries(c.expect)) expect(got[key]).toBe(want as never);
    }
  });

  test("derive_name", () => {
    for (const c of vectors.derive_name) {
      expect(deriveName(c.cwd, c.sessionName ?? undefined, new Set<string>(c.taken), c.prefix)).toBe(c.expect);
    }
  });

  test("display_name", () => {
    for (const c of vectors.display_name) {
      expect(displayName({ name: c.entry.name, entrypoint: c.entry.entrypoint ?? undefined })).toBe(c.expect);
    }
  });

  test("pid_of_socket", () => {
    for (const c of vectors.pid_of_socket) {
      expect(pidOfSocket(c.sock) ?? null).toBe(c.expect);
    }
  });

  test("permission_mode_line", () => {
    for (const c of vectors.permission_mode_line) {
      const hits = [...c.line.matchAll(/"permissionMode"\s*:\s*"([a-zA-Z]+)"/g)];
      expect(hits.at(-1)?.[1] ?? null).toBe(c.expect);
    }
  });
});

describe("proc start", () => {
  test("matches the format Claude string-compares against", () => {
    expect(procStart(process.pid)).toMatch(/^[A-Z][a-z]{2} [A-Z][a-z]{2} {1,2}\d{1,2} \d{2}:\d{2}:\d{2} \d{4}$/);
  });
});

describe("permission-mode parity", () => {
  test("prefers the transcript, so a runtime shift-tab switch is seen", () => {
    const sid = "00000000-1111-2222-3333-444444444444";
    const proj = path.join(os.homedir(), ".claude", "projects", "agentbus-ts-test");
    fs.mkdirSync(proj, { recursive: true });
    const file = path.join(proj, `${sid}.jsonl`);
    try {
      fs.writeFileSync(file, `{"permissionMode":"bypassPermissions"}\n{"permissionMode":"plan"}\n`);
      expect(peerMode(process.pid, sid)).toBe("prompting");
      fs.appendFileSync(file, `{"permissionMode":"bypassPermissions"}\n`);
      expect(peerMode(process.pid, sid)).toBe("bypass");
    } finally {
      fs.rmSync(proj, { recursive: true, force: true });
    }
  });

  test("falls back to the command line, then to bypass", () => {
    expect(peerMode(process.pid, "not-a-uuid")).toBe("prompting");
    expect(peerMode(2 ** 30, "not-a-uuid")).toBe("bypass");
  });
});

describe("socket exchange", () => {
  test("auth then user frame is delivered with the envelope parsed", async () => {
    const sock = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "agentbus-")), "t.sock");
    const token = "ab".repeat(16);
    const key = keyPath(process.pid, sock);
    fs.mkdirSync(path.dirname(key), { recursive: true });
    fs.writeFileSync(key, JSON.stringify({ peerToken: token }), { mode: 0o600 });
    const got: Inbound[] = [];
    const server = listen(sock, token, { onMessage: (m) => got.push(m) });
    try {
      await new Promise((r) => server.once("listening", r));
      const id = await sendUser(sock, { sock: sockPath(999999), name: "pi-test" }, "ping");
      await new Promise((r) => setTimeout(r, 50));
      expect(got).toHaveLength(1);
      expect(got[0].body).toBe("ping");
      expect(got[0].fromName).toBe("pi-test");
      expect(got[0].msgId).toBe(id);
    } finally {
      server.close();
      fs.unlinkSync(key);
    }
  });

  test("a wrong token delivers nothing", async () => {
    const sock = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "agentbus-")), "t.sock");
    const key = keyPath(process.pid, sock);
    fs.writeFileSync(key, JSON.stringify({ peerToken: "cd".repeat(16) }), { mode: 0o600 });
    const got: Inbound[] = [];
    const server = listen(sock, "ab".repeat(16), { onMessage: (m) => got.push(m) });
    try {
      await new Promise((r) => server.once("listening", r));
      await sendUser(sock, { sock: sockPath(999999), name: "pi-test" }, "ping").catch(() => {});
      await new Promise((r) => setTimeout(r, 50));
      expect(got).toHaveLength(0);
    } finally {
      server.close();
      fs.unlinkSync(key);
    }
  });
});

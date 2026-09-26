# agentbus roadmap

Client list sourced from `vercel-labs/skills` (`src/types.ts` AgentType, `src/agents.ts` config-dir
detection), ~80 identifiers as of 2026-09. Cross-referenced against GitHub for real open-source
repos and star counts on 2026-09-26. Numbers are a proxy for "worth building next", not a
guarantee of MCP support; each still needs its own config-format check before adding.

## Shipped and verified (7)

| client | prefix | delivery | status |
|---|---|---|---|
| Claude Code | `cc-` | native | protocol owner |
| pi / pig | `pi-` | pushed | done |
| opencode | `oc-` | pushed | done |
| kiro-cli | `ki-` | spooled, model pulls | done (kirodotdev/Kiro#11614, #11620 filed against its hook engine) |
| codex | `cx-` | spooled | done |
| antigravity-cli | `ag-` | spooled | done |
| hermes (NousResearch) | `ab-` (generic — reports itself as `"mcp"`, too generic to map safely) | spooled | done; 249,116★ (NousResearch/hermes-agent), by far the largest repo in this whole list, found by checking what was already installed locally rather than the ranked table below |

## Deprioritized / blocked

- **Gemini CLI** — skip. Homebrew formula deprecated 2026-12-18, free tier for individuals
  discontinued by Google mid-migration; antigravity-cli is the named replacement and is
  already shipped above.
- **Cursor (`cursor-agent`)** — installer written, untested. No login on this machine for a long
  time; a friend will test when they have one. Auth-gated, not a code problem.

## Next candidates, ranked by GitHub stars (real open-source repos only)

| agent | repo | stars | notes |
|---|---|---|---|
| Zed | zed-industries/zed | 90,918 | editor w/ agent panel, not a headless CLI — lower priority for a bus that's CLI-first |
| OpenHands | All-Hands-AI/OpenHands | 89,202 | was OpenDevin; strong MCP ecosystem presence |
| Warp | warpdotdev/warp | 65,166 | AGPL source-available; terminal app, not obviously scriptable headless |
| Cline | cline/cline | 69,365 | VS Code extension core; check for a standalone CLI/MCP path |
| Goose | block/goose | 54,665 | Block's agent, CLI-first, likely MCP-native |
| Continue | continuedev/continue | 36,029 | IDE-extension-first, check CLI story |
| Crush | charmbracelet/crush | 28,311 | Charm's terminal agent, CLI-native, good adapter candidate |
| Qwen Code | QwenLM/qwen-code | 28,138 | fork of gemini-cli's engine, same MCP shape likely |
| deepagents | langchain-ai/deepagents | 29,777 | a harness/library, not an interactive daily-driver CLI — probably skip |
| Kilo Code | Kilo-Org/kilocode | 27,423 | already contributing PRs here (see project_kilo-contrib memory) |
| Roo Code | RooCodeInc/Roo-Code | 24,297 | VS Code extension fork of Cline |
| Trae Agent | bytedance/trae-agent | 12,115 | ByteDance, CLI-first per README |
| GitHub Copilot CLI | github/copilot-cli | 11,211 | official, closed engine but CLI+MCP config is public |
| Forge | antinomyhq/forge | 7,639 | Rust CLI, MCP-aware per README |
| Neovate Code | neovateai/neovate-code | 1,560 | small but CLI-native and recent |
| AiderDesk | hotovo/aider-desk | 1,442 | Aider wrapped in a desktop/CLI shell |

## Confirmed closed-source / no adoptable repo (skip unless this changes)

Amp (Sourcegraph), Windsurf/Codeium, Augment, Factory Droid, Atlassian Rovo Dev, Devin (Cognition),
Replit Agent, JetBrains Junie, Zencoder, Firebender. All either fully proprietary or only have
community wrapper repos with no real star signal on the agent itself.

## Not agents (excluded from the list entirely)

MCPJam — an MCP inspector/debugging tool, not a coding-agent CLI (also carries a known RCE, CVE-2026-23744,
irrelevant to us but noted so nobody mistakes it for a client candidate later).

## Process for adding one

1. Confirm it's actually installed and licensed to test (skip anything requiring an account we
   don't have, note it here instead — see Gemini/Cursor above).
2. Check MCP support: does it have an `mcp add`/`mcp list` CLI, a JSON config file, or nothing.
3. Reuse `JsonMcpClient` if the config is a plain JSON `mcpServers` object (this covered Gemini,
   Cursor, Antigravity with zero new code beyond a path). Only write a new `Client` subclass for a
   different format (TOML did for Codex, hooks+agent-file did for Kiro).
4. Verify the real `clientInfo.name` it reports over MCP — every host so far has surprised us here
   (kiro: "Q DEV CLI", codex: "codex-mcp-client", not their binary names) — never assume, always test.
5. Install against the real config on this machine (not just a throwaway HOME) if it already
   existed, to catch anything the throwaway-HOME tests can't (see codex's SIGTERM-cleanup bug,
   found only by running for real).
6. Full round trip: ping from a live Claude session, verify the reply, verify cleanup on exit.

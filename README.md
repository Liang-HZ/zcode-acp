# zcode-acp

[![CI](https://github.com/Liang-HZ/zcode-acp/actions/workflows/ci.yml/badge.svg)](https://github.com/Liang-HZ/zcode-acp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/zcode-acp)](https://pypi.org/project/zcode-acp/)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**English** · [简体中文](README.zh-CN.md)

**Drive [ZCode](https://zcode.z.ai) from any ACP client — Zed, Codeg, or your own.**

ZCode (Zhipu's GLM coding agent for the desktop) only speaks a private app-server protocol and
exposes no ACP entry point. `zcode-acp` is a single-file Python ACP agent that bridges the two over
stdio, keeping the native experience intact — token-by-token thinking, streaming text, tool cards,
inline approvals, mid-turn steering, subagents — while keeping your subscription yours:

```
ACP client  ──stdio/JSON-RPC──▶  zcode-acp  ──stdio/JSON-RPC──▶  zcode.cjs app-server
(Zed / Codeg / any ACP client)                (legacy session/* + V4 v4/*)
```

- **No API proxying.** The adapter drives the ZCode engine already installed and signed in on your
  machine, with the providers you configured in the ZCode app — it never holds or moves a key.
- **No middle state.** No database, no sessions of its own; apart from read-only entitlement checks
  (`subscription/list` / `billing/balance`, mirroring the desktop app) it makes no network calls.

## Features

- **Streaming** — token-by-token thinking (`agent_thought_chunk`) and text (`agent_message_chunk`);
  three-stage tool cards with full input/output.
- **Sessions** — create / list / resume / fork / close, with history playback and workspace
  checkpoints.
- **Models** — the picker mirrors the engine registry: subscription plans (BigModel Individual /
  Z.AI) show up and are switchable; reasoning effort is a first-class option.
- **Approvals** — every tool or file action goes through `session/request_permission`, fail
  closed. An optional file-based bridge serves headless clients.
- **Steering** — inject a message into the *running* turn (`_session/steering`), with honest
  delivery receipts. Concurrent prompts ride the same lane.
- **Subagents** — a native sub-agent's token-level stream, tool cards, and lifecycle are forwarded
  to the client.
- **Usage** — context ring (`usage_update`), per-turn token detail (`PromptResponse.usage`), plus
  engine-private cache/occupancy extras.
- **Native commands** — the engine's slash commands: `goal`, `compact`, `init`, `plan`.
- **Auth** — terminal auth: `zcode-acp --login` checks the local engine and hands sign-in to the
  ZCode app.
- **Headless** — `zcode-acp run` emits an opencode-compatible NDJSON event stream; it
  auto-approves permissions, for trusted, unattended use only.

Every capability above is backed by an in-repo test or a real-engine measurement — see
[docs/capability-matrix.md](docs/capability-matrix.md) for the full ACP surface, per-item evidence,
and known boundaries.

## Requirements

- macOS — the ZCode desktop client is a macOS app.
- [ZCode](https://zcode.z.ai) installed and signed in, with at least one usable provider.
- Python 3.9+ (verified on 3.9.6 / 3.10.11 / 3.12.12 / 3.13.2 / 3.14.3).

## Install

From PyPI:

```bash
pipx install zcode-acp        # or: pip install zcode-acp
zcode-acp --version
```

Without installing (uv users):

```bash
uvx --from zcode-acp zcode-acp --version
```

From source (the launcher needs no install):

```bash
git clone https://github.com/Liang-HZ/zcode-acp
./zcode-acp/bin/zcode-acp --version
```

## Quick start

1. Point your ACP client at the agent. It is a plain stdio ACP agent, so clients only need
   `command` plus optional `args` / `env`:

   ```jsonc
   { "command": "zcode-acp", "args": [], "env": {} }
   ```

   Zed (`settings.json`):

   ```json
   {
     "agent_servers": {
       "ZCode": { "command": "zcode-acp", "args": [], "env": {} }
     }
   }
   ```

   Codeg and other clients take the same triple through their agent registry or custom-agent entry.

2. Start a new session, choose a working directory, and send `hello` — the reply streams in token
   by token (reasoning streams first when the model has any).

3. If the engine is not signed in yet, run `zcode-acp --login` once.

> Multiple `zcode-acp` binaries on `PATH`? Clients pick the first (`which -a zcode-acp` lists them
> all). Use an absolute path in `command` to remove the ambiguity.

## Authentication

`initialize` advertises one `terminal` auth method (`zcode-login`); `zcode-acp --login` checks the
local engine, opens the ZCode desktop app, and waits for sign-in. Authentication happens entirely in
the ZCode app — no credentials pass through the adapter.

## Configuration

Everything is configured through environment variables (set them per-client, in `env`):

| Variable | Default | Purpose |
|---|---|---|
| `ZCODE_CJS` | `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` | Engine bundle path |
| `ZCODE_ACP_MODEL` | engine default | `<provider>/<modelId>` applied after `session/new` |
| `ZCODE_ACP_THOUGHT` | `medium` | Initial reasoning effort |
| `ZCODE_ACP_TIMEOUT` | `1800` | Per-turn timeout (seconds) for `run` mode |
| `ZCODE_ACP_LOG_DIR` | `~/.zcode-acp/` | Diagnostic logs (pid-suffixed; instances never truncate each other) |
| `ZCODE_ACP_TRACE` | `1` | `0` disables the protocol trace |
| `ZCODE_ACP_APPROVAL_BRIDGE` / `ZCODE_ACP_APPROVAL_DIR` | off / `~/.zcode-acp/pending` | File-based approval bridge (below) |
| `ZCODE_ACP_STEER_SETTLE_MS` / `ZCODE_ACP_STEER_PERSIST_MS` / `ZCODE_ACP_STEER_FK_RETRY_MS` | `8000` / `3000` / `2000` | Steering timing knobs — see [docs/steering.md](docs/steering.md) |

## Optional: file-based approval bridge

For headless or custom clients. With `ZCODE_ACP_APPROVAL_BRIDGE=1` and an **existing**
`ZCODE_ACP_APPROVAL_DIR`, the adapter stops sending `session/request_permission` to the client and
writes each pending approval to `<dir>/<id>.json`:

```jsonc
{"id": "9f2c1a4b8d3e", "agent": "zcode", "tool_name": "Bash",
 "brief": "{\"command\": \"rm -rf build\"}",
 "payload": { /* the engine's raw permission request, unchanged */ },
 "created": 1789545167.27, "status": "pending"}
```

An external process edits the **same file's** `status` to `allow` or `deny` (polled once a second; the
value is returned to the engine as the decision). A 300s timeout, an unreadable file, or a `status`
left at `pending` → `deny` (fail closed). Tool approvals only — `elicitation/create` questions still
go to the client.

## Usage reporting

- **Context ring** (`usage_update.used/size`) — `used` is the engine's live occupancy (the same
  source as the desktop ring), `size` is the model's catalog `contextWindow`; no frame is sent
  before a real reading exists.
- **Per-turn detail** (`PromptResponse.usage`) — per-turn deltas (`totalTokens` / `inputTokens` /
  `outputTokens` / `thoughtTokens` / `cachedReadTokens` / `cachedWriteTokens`) per the ACP end-turn
  token-usage draft.
- **Engine extras** (`usage_update._meta.zcode.contextUsage`) — cache-hit stats and an occupancy
  breakdown, for clients that know about them.
- **Cost** is omitted when the engine cannot provide one — never faked as `amount: 0`.

## How it works

Each capability rides the engine's own channels: the legacy app-server methods carry sessions,
prompts, and tool cards; the V4 channel (`v4/*`) carries what legacy cannot — true stop, steering
delivery, live subagent streams, parent-session frames. The method-by-method map, with the evidence
behind every claim, is in [docs/capability-matrix.md](docs/capability-matrix.md).

## Known limitations

- **macOS only**; the ZCode app must be installed and signed in.
- **One active session per adapter process** — `session/new` spawns a fresh engine and takes over;
  run several processes for parallel sessions.
- **No `session/load`** (engine limitation) — history comes back via `session/list` +
  `session/resume`.
- **`session/fork` needs a workspace checkpoint** — it only works after a turn that actually
  changed files.
- **Subagents can be observed and cancelled, not injected into**; `session/delete` does not exist.
- **Steering is text-only** — attachments on the guide lane are downgraded by the engine.

## Development

```bash
git clone https://github.com/Liang-HZ/zcode-acp && cd zcode-acp
python3 -m pip install pytest

# Hermetic subset — what CI runs, no engine needed
ZCODE_ACP_SKIP_ENGINE=1 python3 -m pytest tests/test_model_layer.py -q

# Full suite — drives the real engine and a real model (spends your quota)
python3 -m pytest
```

The full suite probes your current model channel first (`ZCODE_ACP_SKIP_MODEL_PROBE=1` skips it),
answers approvals automatically, and isolates working directories and logs per test. Design notes
live in [docs/](docs/).

## Related work

- [Agent Client Protocol](https://agentclientprotocol.com) — the protocol this adapter implements.
- [Codeg](https://github.com/xintaofei/codeg) — ACP client used for interoperability testing.
- [pulpo](https://github.com/Liang-HZ/pulpo) — desktop shell that mounts this repo as a submodule.

## License

Apache-2.0 — see [LICENSE](LICENSE). Issues and PRs: [GitHub Issues](https://github.com/Liang-HZ/zcode-acp/issues).
Please bring measurements — every capability claim in this repo maps to a real-engine test or
measurement.

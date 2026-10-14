# Capability matrix

The full ACP surface `zcode-acp` exposes, method by method. Every row is backed by an in-repo test
or by real-engine measurements — the tests drive the real ZCode engine and a real model.

| ACP capability | Method / event | Evidence | Known boundary |
|---|---|---|---|
| Initialize | `initialize` | `test_handshake.py::test_initialize_shape`: `protocolVersion=1`, `sessionCapabilities{list,resume,fork,close}`, `promptCapabilities.embeddedContext=false` | `loadSession=false` — the engine has no `session/load`; history comes back via `session/list` + `session/resume` |
| New session | `session/new` | `test_handshake.py::test_session_new_shape`: top-level `configOptions` (mode / model / reasoning_effort), `models.availableModels` sourced from the engine registry | The model catalog is engine-authoritative (subscription plans enter via live entitlement checks; the local `~/.zcode/v2/config.json` is only a fallback); one adapter process serves one active session at a time (`session/new` spawns a fresh engine and takes over — run several processes for parallel sessions) |
| Session family | `session/list` / `resume` / `fork` / `close` | `test_session_family.py::test_list_resume_fork_close`: write a file → list finds it → close → resume replays the codeword from history → fork returns a new id | fork needs a workspace checkpoint; resume must use the session's own `(workspacePath, workspaceKey)` pair |
| Streaming text | `agent_message_chunk` | `test_steering.py::test_steering_injects_into_running_turn` matches a codeword against the concatenated text (per-token chunks never match piecewise) | — |
| Per-token thinking | `agent_thought_chunk` | Engine `model.streaming.reasoning_delta` forwarded line by line | Empty when the model produces no reasoning |
| Tool cards | `tool_call` / `tool_call_update` | `test_cancel.py::test_cancel_stops_running_turn` waits for `tool_call` before cancelling; three stages `pending → in_progress → completed/failed` | `content` must be `{type:"content",content:{type:"text"}}`; a bare `{type:"text"}` is dropped wholesale by strict clients |
| Approvals | `session/request_permission` | Every turn that touches tools or files; `tests/acp_client.py` must answer `allow_once` or the turn hangs until 600s | No answer within 600s / client replies cancelled → deny (fail closed) |
| Cancel | `session/cancel` | `test_cancel.py::test_cancel_stops_running_turn`: a `sleep 90` tool turn settles < 45s after cancel with `stopReason=cancelled` | Uses the engine's native `v4/command type=stop` (same path as the desktop stop button). The engine's `session/stop` only aborts the current model request and stream recovery resumes it — unusable |
| Steering | `_session/steering` | 8 tests — see [steering.md](steering.md) | Text blocks only |
| Concurrent prompt | `session/prompt` while a turn runs | `test_steering.py::test_concurrent_prompt_goes_through_guide`: both prompts settle in the same turn (< 5s apart) and the reply carries `_meta.steering` | Falls back to sequential execution when injection is refused |
| Subagents | child-session `session/update` stream | Token-level child thinking/text and full tool input/output over the V4 channel | Observable and cancellable (`session/cancelBackgroundTask`), but no way to inject a message into a running subagent. Sub-agent frames are forwarded on the **parent** session id with `_meta.zcode.parentToolUseId` (the claude-acp parent-attribution contract), so clients that consume parented frames file a child's work into the Agent capsule |
| Safety modes | `modes` + `session/set_mode` | `test_handshake.py::test_session_new_shape`: `default / acceptEdits / plan / bypassPermissions` ↔ engine `build / edit / plan / yolo` | — |
| Model & reasoning effort | `configOptions` + `session/set_config_option` | `supportsReasoningEffort` is always true; the model list is the engine registry — subscription plans (BigModel Individual / Z.AI) enter via live entitlement checks + a `provider/updateAccountConfig` snapshot, and `account:*` models get their reasoning level attached automatically (`test_model_layer.py` pins the resolution rules) | Subscription channels require a signed-in ZCode account on this machine; no API keys are proxied — runtime auth for `account:*` models is handed to the engine on demand via `interaction/requestProviderRuntimeHeaders` |
| Auth (Terminal Auth) | `initialize.authMethods` + `authenticate` + `zcode-acp --login` | Passes the ACP Registry's `client.py` auth check (type `terminal`); `--login` checks the install, opens the ZCode desktop app and waits for sign-in | Sign-in happens in the ZCode desktop app (the adapter moves no credentials) |
| Context usage | `usage_update` + `PromptResponse.usage` | Ring `used/size`: engine occupancy (`session/read` → `runtime.contextUsage.used`, the same source the ZCode desktop ring uses) + the model catalog's `contextWindow`. Per-turn detail: `session/usage` cumulative deltas → `usage{input,output,thought,cachedRead,cachedWrite}` (draft RFD, camelCase), pinned by `test_turn_usage.py` | The client must support `usage_update`; the per-turn detail is a draft field clients may ignore; no real cost from the engine → `cost` is omitted (never `amount: 0`) |
| Native slash commands | `availableCommands` | The engine reports 4: `goal` / `compact` / `init` / `plan` | They run as native engine methods, never hitting the model; the set is engine-defined |
| Structured questions | `elicitation/create` | Engine `interaction/requestUserInput` → ACP form | The client must implement `elicitation/create`; if it does not (or does not answer), the engine receives a decline |
| One-shot CLI | `zcode-acp run` | `run_oneshot()`: opencode-compatible NDJSON (`step_start` / `text` / `step_finish`) | **Auto-approves every permission** (no prompts); unattended use only |

## Engine quirks the adapter compensates for

- **`session/stop` is not a real stop** — it aborts only the current model request; the engine's
  stream recovery reconnects the interrupted stream as if it were network jitter. Real stop is the
  native `v4/command type=stop`.
- **Unix socket path limit** — the engine creates `znr-<uuid>.sock` under `TMPDIR`; once the path
  exceeds 103 bytes `listen()` fails with EINVAL and the engine dies silently. The adapter swaps an
  over-long `TMPDIR` for a short directory under `/tmp` (a measured 118-char sample crashes it) and
  inherits everything else untouched.
- **Session rows are written late** — `session/create` does not write the database; the `session`
  row appears only when the first `session/send` is processed. This matters for steering (see
  [steering.md](steering.md)).

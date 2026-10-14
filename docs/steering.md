# Steering: injecting into the running turn

Steering is the adapter's differentiating capability: it injects a supplemental message into the
**turn that is already running** instead of queueing it for the next one. The adapter advertises it
in the top-level `_meta` of the `initialize` reply:

```jsonc
"steering": { "supported": true, "method": "_session/steering",
              "tier": "extension", "boundary": "step",
              "idle": "promptRequired", "settlesOwnerTurn": false,
              "outcomes": ["injected","queued","promptRequired","startedNewTurn","failed"] }
```

Every field is backed by measurement:

| Fact | Evidence |
|---|---|
| `tier: extension` | The engine exposes no protocol method for "inject into the running turn"; the adapter implements `_session/steering` on top of the engine's `v4/command sendText requestedDelivery:"guide"` |
| `boundary: step` | The injection drains at the **next model request boundary**, not after the tool finishes. Measured: in a three-Bash-step turn `turn.steerQueued` (+20.9s) → `turn.steerDrained` (+24.9s); in a zero-tool text turn 43.5s → 44.1s (`toolCallCount: 0`, `modelRequestCount: 2` — the answer changed within the same turn) |
| `idle: promptRequired` | When idle it never touches the engine and answers `{"outcome":"promptRequired","reason":"noRunningTurn"}`, leaving the content to the client's own `session/prompt` |
| `settlesOwnerTurn: false` | When the injection returns, the owner turn is still running; it keeps producing and closes naturally with `end_turn` — exactly one `stopReason` overall. Guarded by `test_steering_injects_into_running_turn` |

## Receipts

The adapter waits for the engine's verdict instead of treating acceptance as a conclusion:

| Engine signal | Receipt |
|---|---|
| Admitted to the guide lane and later `turn.steerDrained` (`steer.state=guided`) | `{"outcome":"injected","_meta":{"steering":{"delivery":"guide","steerState":"guided","pendingInputId":…}}}` |
| `steer.state == "fellBack"` | `{"outcome":"queued", … "steerState":"fellBack","reasonCode":"guide.…"}` |
| In the guide lane but not drained within `ZCODE_ACP_STEER_SETTLE_MS` | `{"outcome":"injected", …"steerState":"steering"}` (honest: admitted, not yet settled) |
| Still not drained when the turn ends | `{"outcome":"queued", …"reason":"turnEndedBeforeDrain"}` |
| No running turn + request declared `idleBehavior:"promptRequired"` | `{"outcome":"promptRequired","reason":"noRunningTurn"}` |
| No running turn + no `idleBehavior` | Starts a detached new turn → `{"outcome":"startedNewTurn"}` |
| Engine refused / timed out (including exhausted FK retries) | `{"outcome":"failed", …}` with the engine's own `reason`/`detail` passed through verbatim |

`idleBehavior` only accepts `promptRequired`; anything else returns `-32602` (matching
claude-agent-acp). Steering takes `text` blocks only — attachments on the guide lane are downgraded
by the engine on admission.

## The engine injection protocol

Legacy `session/send` cannot do it: its zod schema has no delivery field at all, so sending
mid-turn is accepted but treated as plain queueing (measured: the V4 queue item is
`delivery:{requested:"queue",admitted:"queue"}`, `steer:{state:"notRequested"}`, and the turn's
answer is unaffected). The only channel is a V4 command:

```jsonc
{"id": 1042, "method": "v4/command", "params": {
  "commandId": "7d90b344d3224de8acc8212ba9dc7cbe",
  "clientId":  "zcode-acp",
  "sessionId": "sess_3579db83-a0c5-4f8b-97ba-60380cda60cf",
  "type":      "sendText",
  "payload":   {"text": "…", "requestedDelivery": "guide"},   // startNow|queue|guide
  "issuedAt":  1789545167275}}
```

The reply is `{"status":"accepted", …,"result":{"type":"inputAccepted","delivery":"queue",…}}`.

> ⚠️ That `result.delivery` is **not** the verdict: measured, it is still `"queue"` when a guide is
> fully accepted and injected. The real verdict lives in the event stream
> (`turn.steerQueued.delivery == "guide"` → admitted to the guide lane, `turn.steerDrained` →
> actually merged into the current turn) and in the V4 parent-session frames
> (`state.updated → patch.queue.items[].steer.state`, enum
> `notRequested | submitting | steering | guided | fellBack`). Downgrades are only visible in V4
> frames: the engine's `TurnSteerDeliveryChanged` falls into the default branch of the old
> channel's mapper. Known `fallbackReasonCode`s: `guide.turnInterrupted` (injection and cancel
> raced), `guide.noToolBoundary` (no later boundary in the turn), `guide.attachmentsUnsupported`
> (unreachable here — this adapter only sends plain text on the guide lane).

## Session persistence timing (the FOREIGN KEY root cause)

The engine's `session/create` **does not write the database**: the `session` row in sqlite
(`~/.zcode/cli/db/db.sqlite`) is inserted only when the **first `session/send` is processed**, while
`v4/command sendText` inserts a `session_input` row with a `references session(id)` foreign key.
Sending a guide before that row exists always yields
`fault.command.executionFailed / FOREIGN KEY constraint failed`. Probe measurements against the
engine directly (t = 0 at `session/send`):

| Time | Fact |
|---|---|
| `+0.000 ~ +0.300` | `v4/command sendText guide` → FOREIGN KEY failure (all five probes at 0 / 50 / 100 / 200 / 300ms) |
| `+0.222 ~ +0.310` | **the `session` row lands** (four measurements: 0.222 / 0.242 / 0.292 / 0.310s) |
| `+0.313 ~ +0.413` | **the session's first `session/event`** (`session.titleUpdated`, immediately followed by `turn.started`) — consistently 100–120ms after the row |
| `≥ +0.500` | guide commands are always `accepted` (500 / 800 / 1200ms all pass) |

For an idle session that was only created and never prompted, the `session` row never appears
within 25s while `session/read` answers at `+1.8s` — so **a readable `session/read` does not mean
the row is persisted** and cannot be used as a signal.

What the adapter does (`ZCode.steer()`): ① wait for the definite signal (the session's first
`session/event`), at most `ZCODE_ACP_STEER_PERSIST_MS` (measured need: 313–413ms); ② if it still
hits the FOREIGN KEY fault, retry with 100/200/400/800ms backoff within `ZCODE_ACP_STEER_FK_RETRY_MS`,
fresh `commandId` each time; ③ when the budget is exhausted reply `{"outcome":"failed"}` with the
engine's `reason`/`detail` verbatim. Guarded by
`test_steering_right_after_session_new_never_hits_foreign_key`: prompt immediately after
`session/new` and steer at 100 / 300 / 500ms — no FK failure allowed, and at least one must report
`injected`.

## What a concurrent prompt means

A second `session/prompt` while a turn is running **goes through the same guide lane into the
current turn** instead of falling back to queueing. That prompt then rides the current turn: it is
answered when the turn ends, with the turn's `stopReason`, and the receipt carries the delivery
tier honestly:

```jsonc
{"stopReason":"end_turn",
 "_meta":{"steering":{"outcome":"injected","delivery":"guide",
                      "steerState":"guided","pendingInputId":"queue_…"}}}
```

The rationale: in ACP, a `session/prompt` reply means "this prompt has been handled". Once its
content has been merged into the current turn, that handling ends with the turn — the client sees
one turn and one output. When injection fails (engine refusal, or the turn ended meanwhile) it falls
back to sequential execution as an independent turn.

## Timing knobs

| Variable | Default | Purpose |
|---|---|---|
| `ZCODE_ACP_STEER_SETTLE_MS` | `8000` | Max wait for the engine's steering verdict |
| `ZCODE_ACP_STEER_PERSIST_MS` | `3000` | Max wait for the session-persisted signal |
| `ZCODE_ACP_STEER_FK_RETRY_MS` | `2000` | Bounded retry budget for FOREIGN KEY faults |

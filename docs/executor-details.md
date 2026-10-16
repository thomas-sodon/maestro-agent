# Executor Details

Executor-level mechanics, telemetry persistence, and stream-liveness UX. For the high-level overview, see [CLAUDE.md](../CLAUDE.md). For SDK transport-level concerns, see [sdk-internals.md](sdk-internals.md).

## Bash Output Compression

`_compress_bash_output()` (in `maestro/executor.py`) strips ANSI escapes and progress glyphs, collapses identical lines, applies package-manager-aware filtering, and applies a head+tail elision under a hard cap. Two policies exist: the standard executor uses the `_BASH_HARD_CAP` / `_BASH_HEAD` / `_BASH_TAIL` triple; when the executor runs under the long-context model variant (`[loop] long_context_implementation_enabled`, so `Executor._long_context_enabled`), `Executor._process_step_result` selects the larger `_BASH_HARD_CAP_LONG_CONTEXT` / `_BASH_HEAD_LONG_CONTEXT` / `_BASH_TAIL_LONG_CONTEXT` triple so the stored `tool_calls` keep more of verbose build or test output.

It is applied to `QueryResult.tool_calls` post-query — safe because the result is never re-sent to the model. **Limitation**: it does not intercept SDK context replay; the model's own conversation transcript still carries the raw output until the next compaction.

## Executor Loop Detection

Post-hoc fingerprint-based detection. After the SDK query completes, consecutive identical tool-call fingerprints (`name + input hash + result hash`) at or above a configurable threshold (`[loop] executor_loop_detect_repeats`, default 3) abort the step with `loop_detected=True`.

**Limitation**: cannot intercept mid-stream — the SDK provides no streaming hook usable for this. By the time we detect the loop, the model has already burned tokens on the repeats.

## Empty-Response Retry

A step result is *productive* when the model emitted assistant text **or** ran at least one tool call that did not error (`_is_productive()` in `maestro/executor.py`). When `_process_step_result` encounters an unproductive result — a transient infrastructure failure such as a connection drop or stream stall that doesn't trip the `STREAM_STALL` classifier — `execute()` retries the query up to `[loop] executor_empty_response_retries` times (default 1 = one retry, two total attempts) before marking the step as failed.

Tool-only completions (assistant text empty but ≥1 successful tool call) are productive: late steps often skip the trailing wrap-up sentence after a long tool-execution run, and treating those as failures wastes the replan budget on work the model actually completed. For these, `_process_step_result` synthesizes the step output as `(no text output; completed N tool call(s): <sorted names>)` so the next step's `prior_context` snippet stays meaningful.

Step IDs are unique across the whole task, not just within one planner response. When a replan splices recovery steps after the retained (non-pending) steps, `_splice_replan` (`maestro/loop/step_execution.py`) renames any colliding ID and rewrites that response's own `depends_on` edges to match, so a historical step can never satisfy a dependency meant for a new one (#3960). `_build_execution_groups` raises `ValueError` on a duplicate ID, and a legacy checkpoint carrying duplicates is normalised on resume.

Only unproductive responses are retried. Non-empty failures (loop-detected, other errors) propagate without retry. An `SDKError(STREAM_STALL)` gets the same budget of cheap same-session retries only when its `session_reusable` flag is set (the turn completed unproductively); a stall that poisoned the session — stream-idle timeout, or a stream that ended with no `ResultMessage` — carries `session_reusable=False` and, on a persistent session, propagates at once so `run_loop`'s resume-in-place (`maestro/loop/iteration.py`) recreates the session, as do all other `SDKError` categories (a stateless `query()` step opens a fresh session per attempt and still retries). `0` disables retry so the first unproductive response fails the step immediately.

## Per-Iteration SDK Usage Persistence

A module-level usage sink callback (follows the `configure_cost_tracking` pattern) accumulates `QueryUsage` per iteration into `IterationUsage`, flushed to `usage.jsonl` at iteration end. `read_iteration_usage()` provides bounded tail access for trend detection.

### Turn accounting

`IterationUsage` tracks `total_turns` and `per_caller_turns` derived from `QueryUsage.num_turns` and `QueryUsage.session_id`:

- **`per_caller_turns`** — raw monotone sum (audit truth).
- **`total_turns`** — session-aware dedupe via `_session_peak_turns` (transient bookkeeping, not serialized) so a session whose SDK reports cumulative `num_turns` is credited only by the increment above the prior peak.

The dedupe is correct under per-call semantics with strictly-increasing values and under cumulative semantics. Under per-call semantics with multiple calls reporting the same `num_turns` on a reused session it under-reports — in that case `per_caller_turns` is the source of truth.

Cost takes a different route: a persistent session's `ResultMessage.total_cost_usd` is the CLI process's *running* total, so `query_on_session` converts it to a per-turn delta at the SDK seam (`_finalize_usage`'s `cost_baseline`, tracked per client and zeroed by a `ConversationResetMessage`) before `CostSample` or `IterationUsage.credit_cost` reads it. Every sink therefore sees per-call spend; a one-shot `query()` books its total unchanged (#4306).

### Cost accounting

`total_cost_usd` aggregates dollars from both `ResultMessage.total_cost_usd` (non-early-exit branch) and the `_estimate_cost_usd` estimator (early-exit branch). Each `cost_samples.jsonl` row carries an `estimated: bool` discriminator so reported-vs-derived spend stays auditable, plus the resolved `model` and the `effort` that actually reached the provider (the session's creation effort on a session turn, `"default"` when none was sent, `null` on rows written before attribution — proposal 140). `build_cost_rates` sums both kinds — for the burn-rate gate an estimated dollar is still a dollar.

### Resume-in-place caveat

`IterationUsage.per_caller`, `per_caller_turns`, and `total_turns` may double-count a phase's sub-calls on retry by up to one phase's worth (applies to rate-limit, stale-session, CLI-crash, and stream-stall retries). See [loop-lifecycle.md](loop-lifecycle.md) for the resume-in-place machinery.

## Thinking-Stream Liveness

`_build_sdk_options()` enables `include_partial_messages=True` on every SDK call so the SDK emits `StreamEvent` deltas during the response stream. `_consume_messages()` extracts `thinking_delta` and `text_delta` content and emits a throttled INFO line per `_STREAM_HEARTBEAT_INTERVAL_SECONDS` (default 10s) window **per kind**.

Each delta kind (`thinking`, `text`) has its own throttle window so the first delta of a new kind always emits regardless of the other kind's recent emission. This ensures the thinking→text transition (the most operationally interesting moment — the model has finished reasoning and is producing output) is always visible in logs, even when it falls within the 10s heartbeat window of the last thinking delta.

Per-session log files capture every delta with no throttling.

The first `ThinkingBlock` and first `TextBlock` per assistant content batch are also promoted from DEBUG to INFO (truncated to 120 chars) so the post-stream consolidated log line stays visible at INFO-only log levels. Subsequent blocks in the same batch stay at DEBUG.

## SDK Transcript Records

When `[logs] structured_transcripts_enabled` is on (the default since proposal 104), `SessionLogger` writes a provenance-stamped `.jsonl` transcript sidecar next to each SDK `.log` file under the dated `logs/sdk/<date>/` directory. The `.log` stays the human-readable, per-line-truncated blob (byte-compatible with the pre-proposal format); the sidecar carries the machine-readable attribution. Both are minted from the same timestamp and 6-hex id, so they share the `HH-MM-SS…_<hex>` stem — the sidecar additionally slugs the phase into its filename (`HH-MM-SS_<phase>_<hex>.jsonl`) so transcripts are self-describing and groupable.

Each sidecar is a single JSON line holding a `TranscriptRecord` (`maestro/transcript.py`): a provenance header stamping the phase (`caller`), the persistent-session label, the iteration number, and the task id, followed by one full-content turn per SDK turn. The record is assembled turn-by-turn via `SessionLogger.add_turn` and flushed once at `close()`. Turn content is bounded by `[logs] transcript_content_max` (default 20000 chars) — an independent, far more generous cap than the `.log`'s per-line truncation, so a real reasoning turn survives intact. The header also carries `tools_advertised` (proposal 142): the tool list the CLI announced in its init `SystemMessage`, stamped via `SessionLogger.set_tools_advertised` — the *enforced* surface, as opposed to the caller's declared `allowed_tools`. `None` means unmeasured (a pre-field sidecar, the `cli_paced` provider, or a turn with no init message) and is distinct from `[]`.

The sidecars have a **single reader owner**: `read_transcript_summaries` and `read_transcript_detail` in `maestro/logs.py` are the only callers of `transcript.parse`, and both the dashboard "Sessions" API and the `maestro logs --sdk` CLI consume them (attributed `phase=…/session=…` rows, typed turn blocks with a phase filter). This one-reader discipline is a load-bearing invariant — see [CLAUDE.md](../CLAUDE.md), enforced by `tests/test_conversation_transcript.py::test_transcript_parse_owned_by_logs`.

**Legacy fallback.** A `.log` with no adjacent sidecar — historical files, or any session written while the flag is off — still renders through the old raw-blob path, keyed only by its `HH-MM-SS_<hex>` filename with no phase/session attribution. Turning the flag on adds sidecars going forward; it never rewrites or backfills existing `.log` files.

This operator-facing reasoning record is distinct from the `maestro_sdk` CLI-child transcript sidecar referenced in [sdk-internals.md](sdk-internals.md) and [deployment.md](deployment.md): that one is a token-metering artifact the `cli_paced` provider scrapes for spend counts, not a browsable conversation record.

## Status Notifications Use HTML

The single iteration-start Telegram status message sent via `_send_status_update()` passes `parse_mode="HTML"` so the iteration header and run rate render as bold sections. `Notifier.notify()` accepts an optional `parse_mode`; all other Telegram messages (crash alerts, plan review, task failures, etc.) stay plain text with the default `parse_mode=None`.

Dynamic substrings in `_format_status_message` are passed through `html.escape()` before interpolation.

Per-phase Telegram headers were removed once the loop was stable enough that they were noise. `_phase_will_run()` still gates phase bodies (zero-SDK-cost or budget-throttled phases skip their work), but no header is emitted for skipped or executed phases — only the iteration-start `Iter N — Starting` ping fires per iteration.

### Best-effort notification helper

Best-effort notification call sites (status updates, throttle entry/exit, rate-limit idle entry/exit, task completion, response-to-Thomas, implementation failure, startup ping) all route through `MaestroAgent._notify_best_effort()`. The helper suppresses **only** `asyncio.CancelledError` — a `BaseException`, not an `Exception`, that can leak from a residual SDK teardown task (`async_generator_athrow` finalization from `_consume_messages` after the early-exit `client.interrupt()` path) into the next non-SDK `await`, which is typically the very next Telegram POST. Real `Exception` subclasses (network failures, programming errors, file I/O) are bugs that must propagate so the iteration-level handler classifies or crashes them rather than logging them at WARNING and silently surviving. `KeyboardInterrupt` and `SystemExit` continue to propagate as `BaseException` subclasses outside the catch.

The shutdown notify (`shutdown()`) intentionally uses a wider `except BaseException:` because it runs on the unwind path (`run_loop`'s `finally`) where swallowing `KeyboardInterrupt` is the right trade-off for completing the lifecycle write. The crash notify (`crash.handle_crash`) runs on the *startup* path instead and so follows the `_notify_best_effort` pattern — `except (Exception, asyncio.CancelledError):` — suppressing real notifier failures and the spurious SDK-teardown `CancelledError` (which would otherwise abort startup and skip the crash journal milestone), while letting a genuine `KeyboardInterrupt`/`SystemExit` propagate so an operator can abort a hung notifier (#465).

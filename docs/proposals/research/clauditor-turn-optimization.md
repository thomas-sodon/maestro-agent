# Clauditor → Maestro: Turn / Cost Optimization Study

| Field | Value |
|-------|-------|
| **Source repo** | [IyadhKhalfallah/clauditor](https://github.com/IyadhKhalfallah/clauditor) |
| **Source commit** | `983c907f42fdaf215ccf86e15e63bccaf5ac290c` |
| **Status** | research note (not a proposal) |
| **Related proposals** | [004-cost-aware-budgeting](../core/004-cost-aware-budgeting.md) |

## Why this study

Thomas asked whether clauditor — a quota-management tool for Claude Code — has anything Maestro can borrow for "turn optimization." A quick read of the README revealed an important framing mismatch that is itself the first finding:

- **Clauditor is not about `max_turns`.** It is about *session-level token waste*: every turn in a Claude Code session re-sends the entire conversation, so a session that begins at ~20k tokens/turn can balloon to 200k+/turn while doing the same work at 10× cost. Clauditor measures a "waste factor" and rotates the session when efficiency degrades.
- **Maestro's existing strategy** (per [`CLAUDE.md`](../../../CLAUDE.md) and [`maestro/sdk.py`](../../../maestro/sdk.py)) is the inverse: hardcoded `max_turns` per call site (2 for text-only, 3 for `output_format` JSON, unbounded for `maestro/executor.py`), differentiated `effort` levels, and `QueryUsage` capturing `num_turns` / tokens / cost — but never persisted across calls. Each query is budgeted in isolation; nothing watches the loop as a whole.

The interesting overlap, then, is **session-aware observability**: clauditor knows how much it has spent and how efficiently; Maestro does not. Below is a catalogue of every clauditor mechanism worth knowing about, with a verdict for each.

## Catalogue of clauditor mechanisms

Each entry: what it measures, the actual heuristic / threshold (read from source, not paraphrased), the trigger, and the file anchor.

### 1. Waste-factor session rotation
Ratio of `current_tokens_per_turn` (avg of last 5 turns) vs. `baseline_tokens_per_turn` (avg of first 5 turns). Blocks the session when `current ÷ baseline ≥ threshold` (auto-calibrated, default `10×`), gated by `minTurns = 30` so productive sessions are not killed early.
- Trigger: `UserPromptSubmit` blocks before next prompt; `PostToolUse` blocks mid-autonomy. On block, writes a markdown handoff (commits, key commands, files modified, last assistant message).
- `src/hooks/user-prompt-submit.ts`, `src/hooks/post-tool-use.ts`, `src/features/calibration.ts`

### 2. Auto-calibrated waste threshold
Reads the user's own past sessions from `.claude/projects/*.jsonl` (≥30 turns each), computes the break-even point (rotation cost ≈ 30 turns at baseline tokens), then picks the threshold by percentile of break-evens: <5 sessions → conservative `10×` default, 5–10 sessions → 75th percentile, 10+ sessions → median. Final value clamped to `[5×, 15×]`.
- Trigger: `clauditor calibrate` CLI; result persisted to `~/.clauditor/calibration.json`.
- `src/features/calibration.ts`

### 3. Cache-degradation detector
Last 4 turns: `cache_read_variance < 500` (flat) AND `cache_creation` grew `>1.5×` AND `cache_ratio < 0.5`. Categorised `healthy` (≥70%) / `degraded` (20–70%) / `broken` (<50% with the signature).
- Trigger: `PostToolUse` injects an *advisory* warning suggesting `/clear` or restart. Non-blocking.
- `src/features/cache-health.ts`, `src/hooks/post-tool-use.ts`

### 4. Resume anomaly detector
After a `compact_boundary` record, watches for either (a) output-token spike `> max(median_output × 10, 10k)` or (b) the same flat-read / growing-write / low-ratio cache pattern as #3. Catches known Claude Code bugs (#38029, #40524).
- Trigger: `PostToolUse` injects warning telling Claude to advise the user to start fresh.
- `src/features/resume-detector.ts`

### 5. Single-turn token spike
`lastTurnTokens > avgTokens × 5 AND lastTurnTokens > 100k`. Catches silent quota drains.
- Trigger: `PostToolUse` injects "notify user immediately" alert.
- `src/hooks/post-tool-use.ts`

### 6. Loop detector
Fingerprints each tool invocation as `toolName:inputHash:outputHash`. Blocks when the same fingerprint appears `≥3` consecutive turns.
- Trigger: `Stop` hook returns `decision: "block"`.
- `src/features/loop-detector.ts`

### 7. Bash output compression
For Bash output ≥500 chars: strips ANSI / progress bars (`[=+]`, `█░▓▒`), collapses runs of identical lines (`[line repeated N times]`), summarises package-manager output, keeps first/last 5 lines + lines matching `error|warn|fail|exception|✗`. Final hard cap 2000 chars (40% head + 40% tail + middle ellipsis).
- Trigger: `PostToolUse` rewrites the tool result before it returns to the model.
- `src/features/bash-filter.ts`

### 8. Edit thrashing detector
Per-session counter of edits to each file. At the 5th edit of the same file, fires advisory: "stop editing, explain goal to user, ask before resuming." Counts persisted at `~/.clauditor/edit-counts.json` because each hook invocation is a fresh process.
- `src/hooks/post-tool-use.ts`

### 9. Error index with proximity-based fix capture
On Bash error (matched against ~20 patterns: `error:`, `ENOENT`, `command not found`, …) records `(baseCommand, firstLineOfError[:200])`. If a Bash command with the same `baseCommand` succeeds within `FIX_PROXIMITY_MS = 60_000` of the failure, the success is recorded as the fix. Surface threshold: `≥2` occurrences if a fix is known, `≥3` without. Stored at `~/.clauditor/knowledge/<encoded-cwd>/errors.json`, expired after 90 days.
- Trigger: `PreToolUse` on Bash injects the known fix as advisory context before the command runs.
- `src/features/error-index.ts`

### 10. File hotspot tracker
Counts edits/reads per file per session (deduped). "Hot" = `≥5` edits across `≥3` sessions.
- Trigger: when a hot file is read, `PostToolUse` injects `"[clauditor]: <file> — N edits across M sessions, last edited DATE. Frequently modified — review carefully."`
- `src/features/file-tracker.ts`

### 11. Context-window fill alert
Sums `input + cache_creation + cache_read`. Knows the model from the JSONL transcript (Sonnet 200k, Opus 1M). At `≥95%`, injects a high-priority warning telling Claude to flush state to `CLAUDE.md` *before* the next turn (architectural decisions, files modified, task status, conventions) — a guard against "compaction amnesia."
- `src/hooks/post-tool-use.ts`

### 12. CLAUDE.md footprint audit
Scans the CLAUDE.md hierarchy (`/etc`, `~/.claude`, `./CLAUDE.md`, `./CLAUDE.local.md`, `.claude/rules/*.md`), estimates tokens (`chars / 4`), flags entries `>150` chars as inline data and files `>4000` tokens as oversized. Quota cost shown as `tokens / 1M × $0.30` per message.
- Trigger: `clauditor` CLI command.
- `src/features/memory-guard.ts`

### 13. Quota burn rate
Weighted token rate over the last 10 turns: `input × 1.0 + cache_read × 0.1 + cache_create × 1.25 + output × 5.0`. Status `critical` if `(cache <50% AND burn >30k/min)` OR `(cache <50% AND burn >10k/min)` OR `(<30 min remaining AND cache <70%)`.
- Trigger: `PostToolUse` advisory warning when critical.
- `src/features/quota-burn.ts`

### 14. Buggy-version detector
Looks for Claude Code version `2.1.69` – `2.1.89` in the JSONL (known prompt-cache bug causing 10–20× token inflation per upstream issues). Injects a critical "run `claude update`" warning.
- `src/hooks/post-tool-use.ts`

### 15. Session handoff & resume
On rotation block: extracts original task (first user message), last 3 user messages, git commits (parsed from heredoc), key commands matching `test|build|deploy|lint|install|migrate|seed`, files touched, last assistant message → writes `~/.clauditor/sessions/<encoded-cwd>/<ts>.md`. `SessionStart` hook auto-injects all handoffs from the last 24 h, labelling cross-project ones with `[project-name]`.
- `src/features/session-state.ts`

### 16. PostCompact summary capture
When Claude Code's `/compact` fires, the `PostCompact` hook receives Claude's *own* `compact_summary` — generated while the model still had full context, dramatically richer than mechanical extraction. Saved as a fallback handoff with `isPostCompact: true`.
- `src/features/session-state.ts`

### 17. Skill / workflow pattern detection
Across sessions, normalises Bash command hashes (Read/Edit are too generic), extracts contiguous subsequences of length 2–8, counts sessions containing each (deduped per session), keeps patterns appearing in `≥3` sessions, removes patterns that are substrings of longer patterns.
- Trigger: `PostToolUse` injects a one-shot nudge after 20+ tool calls with 3+ distinct tools, suggesting the user save the workflow as `.claude/skills/<name>/SKILL.md`.
- `src/features/skill-suggest.ts`

### 18. Health-check rate-limit
Health checks (#3, #4, #5, #11, #13) are expensive, so `PostToolUse` only runs them every `HEALTH_CHECK_INTERVAL_MS = 30_000`. Last-check timestamp persisted at `~/.clauditor/health-check-ts.json` (per `sessionId`) because each hook is a fresh process.
- `src/hooks/post-tool-use.ts`

## Verdicts for Maestro

For each idea: **adopt now**, **defer (worth a future proposal)**, or **skip (does not fit)**.

### Adopt now (small, high signal-to-noise)

**A1. Persist `QueryUsage` across the loop.** Maestro already captures `num_turns`, `total_tokens`, `total_cost_usd`, `duration_ms` in `QueryUsage` ([`maestro/sdk.py`](../../../maestro/sdk.py)) and logs them once — then throws them away. Aggregating per-iteration totals into `lifecycle.json` (or a sibling `usage.jsonl`) is a few dozen lines of code and unlocks every other idea below. This is the single highest-leverage change and is already implied by [proposal 004](../core/004-cost-aware-budgeting.md), which now ships a self-regulating burn-rate throttle as the sole dollar-budget mechanism.

**A2. Loop-detector on the executor.** [`maestro/executor.py`](../../../maestro/executor.py) is the only unbounded query site, and the natural place runaway turns happen. Clauditor's fingerprint scheme (`tool:inputHash:outputHash` repeated `≥3` consecutive turns) is ~30 lines and would let the executor short-circuit a step, write a journal realization, and surface a diagnostic instead of silently burning the budget. Pairs naturally with `_distill_findings` so the realization captures *what* was looping.

**A3. Bash output compression in the executor prompt path.** Clauditor's `bash-filter.ts` is portable: strip ANSI / progress bars, collapse repeated lines, prefer error-bearing lines, hard cap. Big chunks of Maestro's executor cost go to re-sending verbose Bash output in the conversation; trimming it before it re-enters context is pure win and has no semantic risk.

### Defer (worth a future proposal, but bigger than this study)

**D1. Auto-calibrated waste threshold from past sessions.** The single most novel idea in clauditor and the most directly applicable: read Maestro's own historical usage (once #A1 is in place), compute the break-even rotation point per past iteration, choose by percentile. Maestro is fully autonomous so it cannot literally "rotate the session," but the analogue is real: trigger an early `dream()` and start the next iteration with a fresh prompt rather than letting the executor's session balloon. Worth its own proposal alongside #004.

**D2. Cache-degradation + token-spike + context-fill checks → diagnostics.** [`maestro/diagnostics.py`](../../../maestro/diagnostics.py) already aggregates operational health and is gated by `is_noteworthy()` so it stays quiet on clean runs. It is the perfect place to surface clauditor-style signals (cache ratio collapse, sudden token spike, context >95% full). All three depend on `QueryUsage` persistence (#A1), so they slot in naturally as a follow-up.

**D3. Error index + proximity-based fix capture.** Clauditor's `(baseCommand, firstLineOfError) → fix-within-60s` index is a beautiful zero-supervision learning loop. Maestro already has `journal.search_journal` and a `memory` MCP server; the missing piece is *automatic* injection of "last time you ran `pytest tests/foo.py` it failed with X — the fix was Y" into the executor prompt. Pure upside, but it is its own subsystem with its own storage, so this deserves a proposal rather than a side change.

**D4. PostCompact-style rich handoffs.** Maestro's `dreaming.py` already does the moral equivalent — but the *insight* worth borrowing is that the compaction summary should be generated while the model still has full context, not reconstructed from raw history afterward. Worth checking that `maestro/dreaming.py` actually runs against fresh in-session context rather than against compressed historical state; if not, fix it.

### Skip (does not fit Maestro's shape)

- **The seven-hook plumbing.** Clauditor lives inside Claude Code's hook system; Maestro talks to the SDK directly. The hook architecture is a clauditor-implementation detail, not an idea.
- **Session-blocking with a "rotate now" UI.** Maestro is autonomous; there is no human to start a fresh session. The Maestro analogue is "trigger dream + reset", not "block."
- **CLAUDE.md footprint audit.** Useful one-shot for human users; Maestro's [`CLAUDE.md`](../../../CLAUDE.md) is hand-maintained by Thomas and not a quota concern.
- **Buggy-version detector.** Maestro pins its SDK version explicitly; this is a Claude Code IDE concern.
- **Skill / workflow pattern detection.** Cute, but Maestro's planner already decomposes goals into named steps; the right place for cross-task pattern reuse is `evolution.py`, not a new heuristic.
- **Cross-project session handoffs.** Maestro is single-project by design.

## Top three takeaways

1. **Maestro is flying blind on cumulative cost.** `QueryUsage` is captured per call and discarded. Persisting it per loop iteration (A1) is the prerequisite for *every* other quality-of-life improvement clauditor demonstrates. Estimated effort: small.
2. **The executor needs a loop guard.** It is the only unbounded query site in Maestro, and clauditor's three-repeat fingerprint heuristic (A2) is the simplest possible safety net. Pair it with bash output compression (A3) and the executor's worst-case behaviour gets dramatically cheaper without changing its happy path.
3. **The single most clever idea — auto-calibrated waste thresholds (D1) — is real and transferable**, but only after A1. It deserves its own proposal: read past iteration costs, compute break-even, trigger an early `dream()` instead of letting context explode. This is the autonomous-agent analogue of clauditor's session rotation.

## Architectural lesson

Clauditor's most valuable *pattern* is **observe heavily, intervene lightly**: every signal it computes is injected as advisory context, and only the loop detector and waste-factor breach actually block. Maestro should adopt the same default — instrument the loop, surface anomalies through the diagnostics snapshot, and let the agent's own reasoning decide what to do. Hard blocks are reserved for the cases where the model has already lost the plot.

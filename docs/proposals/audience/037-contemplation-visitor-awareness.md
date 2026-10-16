# 037: Contemplation Visitor Awareness

| Field | Value |
|-------|-------|
| **Impact** | LOWER |
| **Status** | proposed |
| **Affects** | `maestro/agent.py`, `maestro/visitor_tracking.py` (extend), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `CLAUDE.md` |

## Description

Thread a short visitor-context line into the contemplation prompt so Maestro becomes aware of its observers. The design avoids two obvious anti-patterns:

- **Do not extend `is_noteworthy()` in `diagnostics.py`.** Diagnostics is an alert channel with tight thresholds; folding visitor awareness into it would either flip diagnostics into always-inject mode (drowning the prompt) or coarsen the visitor signal to operational-event granularity. Visitor awareness gets its own parallel slot at the contemplation injection site.
- **On-change predicate, not per-iteration inject.** Constant bot probes would otherwise trigger the line every iteration forever, making ~100 tokens of background noise. The predicate only fires when the `(unique_visitors, browser_visits, bot_visits)` tuple over the 1 h window differs from the last injection — the agent hears about observers when the composition actually shifts.

The line is descriptive, not directive: *"Recent observers (1h): 3 unique, 12 events (2 browser / 10 bot)"*. Maestro decides what to make of it.

## Relevant Findings

- The existing injection site is the `is_noteworthy` block in `maestro/agent.py` (`MaestroAgent.contemplate`): `if is_noteworthy(snapshot): full_context = (f"Operational status: {snapshot.summary}\n{context}" if context else f"Operational status: {snapshot.summary}")`. The visitor slot lands alongside, not inside, this block — same accumulator (`full_context`), independent predicate.
- `build_visitor_summary` from proposal 034 is the read path; reading JSONL once per contemplation (every few minutes) is cheap at 14 d retention.

## Implementation Plan

1. Extend `maestro/visitor_tracking.py` (or add a small sibling helper) with:
   - `has_visitor_signal(summary, prev_signature) -> tuple[bool, tuple[int, int, int]]` — returns `(True, new_signature)` iff `summary.unique_visitors + summary.bot_visits > 0` AND `new_signature != prev_signature`, else `(False, prev_signature)`. Signature shape: `(unique_visitors, browser_visits, bot_visits)`.
   - `format_visitor_line(summary) -> str` — produces a ≤ 100 char one-liner, e.g. `"Recent observers (1h): 3 unique, 12 events (2 browser / 10 bot)"`. Enforce the length budget in the implementation.
2. Agent state: new instance attribute `_last_visitor_signature: tuple[int, int, int] = (0, 0, 0)`. Not persisted to `lifecycle.json` — it is transient noise-suppression state; a fresh process re-injecting once on the first iteration after restart is acceptable and self-healing because any non-empty summary's three-tuple differs from `(0, 0, 0)`.
3. Injection alongside the existing `is_noteworthy` block in `MaestroAgent.contemplate` (`maestro/agent.py`):
   ```python
   if self.config.visitor_awareness_enabled:
       summary = build_visitor_summary(self.config.visitor_events_path, window_hours=1.0)
       should_inject, new_sig = has_visitor_signal(summary, self._last_visitor_signature)
       if should_inject:
           visitor_line = format_visitor_line(summary)
           full_context = (
               f"{visitor_line}\n{full_context}" if full_context else visitor_line
           )
           self._last_visitor_signature = new_sig
   ```
   Placed alongside, not inside, the existing `is_noteworthy` branch — the two slots have independent predicates and independent growth trajectories.
4. Introduce `[loop] visitor_awareness_enabled` (default `true`) in `config.py`, `toml_config.py`, and `maestro.toml`. When false, the block above is skipped entirely and `_last_visitor_signature` stays at its initial `(0, 0, 0)`. Operators who want visitor capture (034) without prompt exposure can disable just this without disabling the rest.
5. Update `CLAUDE.md` with one Key-Design-Decisions bullet describing the contemplation visitor slot and the on-change predicate.

## Testing Strategy

Unit:
1. `has_visitor_signal` returns `(False, prev)` when summary's three-tuple matches `prev_signature` (e.g. `prev_signature=(3, 2, 10)` and summary yields `(3, 2, 10)`).
2. `has_visitor_signal` returns `(True, new_sig)` on first call (`prev_signature=(0, 0, 0)`) when traffic exists; `new_sig` is the three-tuple `(unique_visitors, browser_visits, bot_visits)` and differs from `(0, 0, 0)`.
3. `has_visitor_signal` returns `(False, prev)` when `summary.unique_visitors + summary.bot_visits == 0`, including the fresh-process case where `prev_signature == (0, 0, 0)` (the initial state).
4. `has_visitor_signal` returns `(True, new_sig)` when `unique_visitors` and `bot_visits` are unchanged from `prev_signature` but `browser_visits` differs (e.g. `prev_signature=(3, 2, 10)`, summary yields `(3, 5, 10)`). This is the regression case for #1434 — without `browser_visits` in the signature, a returning browser visitor's extra events would not flip the predicate.
5. `format_visitor_line` output is ≤ 100 chars for representative summaries (small counts, three-digit counts).

Component:
1. Mock `build_visitor_summary` to return a fixture summary; assert `context` is prefixed with the expected visitor line on the first contemplation.
2. Same fixture + second contemplation without summary change → no prefix added (signature-matched skip).
3. `visitor_awareness_enabled=false` → no prefix ever added regardless of traffic.

```bash
python3.12 -m pytest tests/test_visitor_awareness.py tests/component/test_contemplation_visitor_slot.py -v
```

## Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| Prompt bloat when traffic composition churns | Low | On-change gate bounds inject frequency; line ≤ 100 chars |
| Agent over-indexes on observers and drifts from tasks | Low | Line is descriptive, not directive; stage-specific prompts dominate behaviour |
| Visitor signal conflates bots and humans into one salience cue | Accepted | Deliberate — Maestro can differentiate from the (unique / browser / bot) breakdown in the line itself |
| JSONL read every contemplation | Low | Same read path as 035 / 036; sub-ms at 14 d retention |

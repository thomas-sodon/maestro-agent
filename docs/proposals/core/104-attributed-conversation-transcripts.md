# 104: Attributed Conversation Transcripts — One Provenance-Stamped Record for the Internal Monologue, Behind One Reader, Rendered as a Session/Phase Review View

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/sdk.py` (`SessionLogger`, `_truncate`), `maestro/logs.py` (`get_sdk_sessions_data`, `read_sdk_session_file`, `list_sdk_sessions`, `show_sdk_session`), new `maestro/transcript.py`, `maestro/dashboard/routes.py`, `maestro/dashboard/static/index.js`, `maestro/dashboard/templates/index.html`, `maestro/dashboard/static/index.css`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `docs/executor-details.md`, CLAUDE.md, new `tests/test_conversation_transcript.py`, `tests/test_config.py` |
| **What landed** | Steps 1–8 — the record substrate, the provenance-stamping writer, the single reader owner, the dashboard API routed through it, the review view rendering typed turns, the CLI rendering attributed rows and typed turns, and the default flip plus docs. Step 1 (PR #3266) — new `maestro/transcript.py`: the `TranscriptTurn` / `TranscriptRecord` dataclasses and the `to_jsonl_line` / `parse` round-trip helpers (tolerant per-line decode, malformed lines → `None`), plus `tests/test_conversation_transcript.py`. Step 2 (PR #3275) — `SessionLogger` (`maestro/sdk.py`) accepts `caller` / `session_label` / `iteration` / `task_id`, writes them as a one-line `.jsonl` sidecar header, and slugs `caller` into the sidecar filename (`HH-MM-SS_<caller>_<hex>.jsonl`) via `_safe_caller`, behind `[logs] structured_transcripts_enabled` (`maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`); the AST guard `test_every_session_logger_construction_passes_caller` pins it. Step 3 (PR #3286) — `SessionLogger.add_turn` mirrors each thinking / text / tool_call / tool_result turn into the sidecar at full content bounded by `[logs] transcript_content_max`, collapsing the per-chunk `Text delta:` stream to one `text` turn; `_truncate` and the `.log` lines are unchanged. Step 4 (PR #3296) — `maestro/logs.py` `read_transcript_summaries` / `read_transcript_detail`, the sole `transcript.parse` call site (degrading to the raw `.log` blob when no sidecar exists), pinned by the reader-chokepoint test `test_transcript_parse_owned_by_logs`. Step 5 (PR #3303) — the dashboard API (`maestro/dashboard/routes.py` `sdk_sessions` / `sdk_session_detail`) now reads through `read_transcript_summaries` / `read_transcript_detail` (off-thread via `asyncio.to_thread`), returning the provenance-rich summaries and typed-turn detail; the now-orphaned `get_sdk_sessions_data` is deleted from `maestro/logs.py`. Legacy label-less `.log` files carry `null` provenance and render as the raw blob, so with the flag off the response is byte-for-byte backward-compatible; `test_sdk_sessions_surfaces_provenance` / `test_sdk_session_detail_returns_typed_turns` pin it and `test_sdk_session_endpoints_stay_behind_auth` keeps both endpoints off the public allowlist. Step 6 (PR #3314) — the dashboard review view (`maestro/dashboard/static/index.js`, `templates/index.html`, `static/index.css`) replaces the raw-blob `el.textContent = data.content` with a typed-turn renderer: `loadSdkSessions` now caches the summaries and `renderSdkSessions` is the pure DOM build over the cache, drawing provenance-rich list rows (phase label + session/iter/task/turn-count meta) and a phase-filter row wired through the existing `FILTER_GROUPS` convention (`filterState.sdkPhase`); `renderSdkTranscript` builds a provenance header plus one block per turn (kind + role + content), keyed by a per-kind CSS class validated against `SDK_TURN_KINDS` so a malformed kind cannot break out of the class attribute. Every field passes through `escapeHtml` before insertion, and a legacy `.log` (or an unreadable / malformed sidecar — `legacy` set or non-array `turns`) keeps the `textContent` raw-blob fallback, so with the writer flag off the phase set is empty and the list degrades to today's filename-only view. `test_stamped_session_detail_renders_turn_blocks` / `test_sessions_list_shows_phase_and_filters` / `test_legacy_session_renders_raw_blob` / `test_turn_content_is_escaped` pin the typed render, the phase filter's single-active invariant, the legacy fallback, and turn-content XSS escaping. Step 7 (PR #3325) — the CLI (`maestro/logs.py` `list_sdk_sessions` / `show_sdk_session`) becomes a record consumer: `list_sdk_sessions` renders from `read_transcript_summaries` (which grew a `date` argument, applied before the `max_dates` recency slice so a `--date` older than the window still resolves), printing a compact `phase=… session=… iter=… task=… turns=…` row per stamped session and the legacy `filename (bytes)` row for a `.log`-only one; `show_sdk_session` resolves each session as a `.log` / `.jsonl` stem group (`_session_groups` / `_resolve_session_group`, correlated by `_session_stem_key`, with a metacharacter-safe substring `--session` match replacing the former escaped glob) and renders a stamped sidecar as typed turns via `read_transcript_detail` plus the new `_format_transcript_turns` helper, while a legacy / malformed / `--follow` path tails the raw `.log` exactly as before. With the writer flag off (no sidecars) both surfaces degrade to today's filename-only listing and raw-`.log` tail. `test_stamped_row_shows_phase` / `test_stamped_session_renders_typed_turns` / `test_date_filter_finds_dir_older_than_max_dates` and the legacy / malformed / sidecar-only fallback tests pin it. Step 8 (PR #3334) — `structured_transcripts_enabled` now defaults `true` in `maestro.toml` / `Config`, `docs/executor-details.md`'s `## SDK Transcript Records` section documents the record and view, and CLAUDE.md carries the one-reader transcript-chokepoint invariant line. |
| **What is missing** | None — all eight steps have landed. |
| **Derived from** | live production data via /propose, 2026-07-29 |

## Description

> Status updated to shipped on 2026-08-02 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Maestro already writes every SDK query's turns to disk and already exposes them
in the dashboard behind a "Sessions" button — but the surface is unusable for
the debugging-and-auditing purpose it exists for. The files it reads
(`logs/sdk/<date>/HH-MM-SS_<hex>.log`) carry no provenance: nothing on disk says
which phase (`contemplate`, `evolve`, `execute_step`, …), which persistent
session (A–D), which iteration, or which task produced a given conversation,
even though every one of those facts is in scope at the moment the file is
opened. Turn content is truncated to 500 characters (thinking, response text)
and 1000 (prompt) by `_truncate`, so the monologue is *lossy* — the reasoning
Thomas would audit is cut off mid-thought. And the dashboard renders the result
as a raw pre-wrapped blob (`el.textContent = data.content`) still carrying the
noisy per-chunk `Text delta:` streaming lines that duplicate the final answer.

The root is not any one of those defects: it is that the conversation transcript
has **no first-class owner**. It is a free-text debug side-effect that each
consumer — the dashboard reader, the CLI `show_sdk_session`, the frontend
renderer — re-handles as opaque text. Because the phase/session/iteration/task
labels were never persisted into the record, no view can ever group "by
session/phase" no matter how it is styled; and because content is truncated at
write time, no view can ever show the full monologue. Restyling the blob cannot
close the gap; the record itself must change.

This proposal gives the transcript one structured, provenance-stamped record
type, written by a single owner (`SessionLogger`) and read by a single owner (a
`transcript` reader in `maestro/logs.py`) that both the dashboard and the CLI
consume. The record captures each turn's full content under its own bound
(distinct from the human-readable log line) and stamps caller/phase, session
label, iteration, and task id from data already in scope. The end state: a clean
dashboard review view grouped and filterable by session and phase, showing full
untruncated turns as a readable conversation — and a CLI and a record that are
self-describing rather than a wall of `HH-MM-SS_<hex>.log` files.

## Relevant Findings

- `maestro/sdk.py` (`SessionLogger.__init__`) — names each file
  `HH-MM-SS_<6-hex>.log` and writes only a bare `SDK session started at …`
  header. No `caller`, session label, iteration, or task id is persisted, so the
  record cannot be attributed to a phase after the fact.
- `maestro/sdk.py` — at the query entrypoint the phase is already bound as
  `caller` from `_build_sdk_options(options, system)`, in scope well before the
  `SessionLogger` is constructed and the `Prompt:` line is written. The label
  Thomas wants to browse by exists at the write site and is discarded.
- `maestro/sdk.py` (`_truncate`) — turn content is written through
  `_truncate(block.thinking, 500)`, `_truncate(block.text, 500)`, and
  `_truncate(prompt, 1000)`; the on-disk record therefore ends `… [N chars
  total]` and the full monologue is unrecoverable from it.
- `maestro/sdk.py` — the streaming path logs a `Text delta:` line per chunk
  *and* a final `Text:` line, so the record contains the answer twice: once
  fragmented across deltas, once truncated.
- `maestro/logs.py` (`get_sdk_sessions_data`) — returns only `{date, filename,
  size_bytes}` per session; there is no field a client could group or filter by
  phase or session, because none is persisted.
- `maestro/logs.py` (`read_sdk_session_file`, `show_sdk_session`,
  `list_sdk_sessions`) — three separate readers each re-open the raw `.log` as
  unstructured text; `list_sdk_sessions` prints `filename (bytes)` with no
  provenance, `show_sdk_session` tails raw lines.
- `maestro/dashboard/static/index.js` (`loadSdkSessions`,
  `loadSdkSessionDetail`) — the list shows `filename` + size only, and the
  detail view sets `el.textContent = data.content`, i.e. dumps the raw file with
  no parsing, grouping, or turn structure.
- `maestro/dashboard/routes.py` (`sdk_sessions`, `sdk_session_detail`) — the API
  is a thin pass-through of the two `logs.py` readers; adding provenance is a
  matter of what the readers return, not new endpoints.
- `maestro/config.py` (`session_data_days`, `sdk_log_dir`) — retention is
  already operator-managed via `session_data_days` (default 10 date dirs); this
  proposal adds no new retention axis.
- `docs/proposals/core/062-cli-paced-slash-command-metrics.md`,
  `docs/proposals/core/075-cli-paced-marker-packs.md` — the only existing
  "transcript" work is `maestro_sdk`'s CLI-child transcript *sidecar*, mined for
  token/cost metering. It is not an operator-facing reasoning record and does
  not overlap this surface.

## Production Evidence

Live data dir: `/opt/maestro/data` (host bind-mount of `maestro-loop`).
All reads strictly read-only.

**Volume — the surface has abundant data and none of it is browsable.** Under
`logs/sdk/`, **9,367** session `.log` files across a ~35-day retention window
(2026-06-24 → 2026-07-29), **108 MB** total, at roughly 150–500 files/day
(e.g. 2026-07-22: 427 files / 5.2 MB; 2026-07-25: 256 / 2.5 MB). Every filename
is an opaque `HH-MM-SS_<hex>.log`; the dashboard list shows only these names and
a KB size.

**Provenance exists — in a sibling store, divorced from the transcript.** The
phase label lives in `cost_samples.jsonl` as `caller`. Distinct phases observed
in the last 20k samples include `introspect` (625), `contemplate` (565),
`execute_step` (494), `update_self_model` (238), `reflect` (160),
`distill_findings` (140), `respond_to_messages` (77), `propose_plan` (47),
`curate_pending_plans` (46), `planner` (27), `dream` (23), `implement_plan`
(12). These are exactly the "session/phase" axes Thomas asked to browse by — and
none of them is written into the transcript files those samples describe.

**Lossy, noisy record — one real session (redacted), window 2026-07-29 12:36.**
File `2026-07-29/12-36-28_1c645f.log` (31 lines, 2,980 bytes):

```
[12:36:28.865] SDK session started at 2026-07-29T12:36:28.865525+00:00
[12:36:28.865] Session follow-up: Distill the key findings … Outcome: success
  … - [done] Scan the task queue and stand-down verdict ledger … del... [3122 chars total]
[12:36:30.635] Text delta: Two
[12:36:31.200] Text delta:  own PRs were identified via the `is_pr_settled` contract (#3208, …
[12:36:32.212] Text delta: json`) is empty (`[]`) with zero lapped remediation slots, …
   … (11 more `Text delta:` lines) …
[12:36:35.030] Text: Two own PRs were identified via the `is_pr_settled` contract … [919 chars total]
[12:36:35.047] SDK session ended
```

The `[3122 chars total]` / `[919 chars total]` markers are `_truncate` cutting
the prompt and the answer; the twelve `Text delta:` lines are the same answer
fragmented. There is no line anywhere saying this was the `distill_findings`
phase on a follow-up turn of a task session — which is the first thing a debugger
needs to know.

## Prior Point Fixes (whack-a-mole ledger)

The monologue surface was assembled by accretion — each addition handled the
transcript as unstructured text rather than giving it a record — so no single
addition delivered "attributed, clean, readable." (This is accretion, not
repeated bug-fixing; the structural signal is that every consumer re-derives the
same opaque text.)

| Change | What it added | Why the class survived |
|---|---|---|
| `SessionLogger` free-text writer (proposal 061 lineage) | per-query `.log` of timestamped turn lines | the record was born label-less and free-text; no provenance, no schema |
| `_truncate(…, 500/1000)` on the `SessionLogger` writes | bounded log-line size so `.log` files stay small | made the monologue *lossy* — full reasoning is unrecoverable from the record |
| `get_sdk_sessions_data` + `read_sdk_session_file` + dashboard "Sessions" button | surfaced the raw `.log` in the dashboard | surfaced an opaque, truncated, label-less blob — none of "by session/phase," "clean," or "readable" |
| `perf: bound dashboard log/diagnostics I/O cost` (`d8ccc78b`) | capped read cost of the blob | bounded reads of an unstructured artifact; did not structure it |
| `list_sdk_sessions` / `show_sdk_session` CLI readers | CLI listing + tail of the same `.log` | a third consumer re-handling the raw text, still opaque |
| CLI-paced transcript sidecar + slash metrics (proposals 062, 075) | mined the CLI child's transcript for token/cost | metering-only; never exposed the reasoning turns to an operator |

**Retired by this design:** the truncation-only capture (superseded by
full-content capture in the record, step 3), the opaque-filename list (step 5),
the raw-blob renderer `el.textContent = data.content` (step 6), and the three
divergent readers collapsed to one (steps 4, 7). The design does not add a new
surface on top of the blob — it replaces the blob path with a structured record
read through one owner.

## Implementation Plan

Steps are independently mergeable and gated behind
`[logs] structured_transcripts_enabled` (default `false` until step 8). The
structured record is written to a **new** `.jsonl` sidecar alongside the
existing `.log`, so every intermediate step leaves the current blob view intact
and an interrupted rollout is safe. Provenance capture and full-content capture
are separate stores from the human-readable `.log` — this is not a raise of the
`_truncate` cap, which continues to bound the `.log` lines unchanged.

1. [x] **Structured transcript record + schema.** new `maestro/transcript.py` —
   define `TranscriptTurn` (kind: `prompt`/`thinking`/`text`/`tool_call`/
   `tool_result`/`event`, `role`, `ts`, `content`) and `TranscriptRecord`
   (provenance header + ordered turns), with `to_jsonl_line`/`parse` helpers.
   No writer/reader wired yet.
   - *Depends on:* none.
   - *Acceptance:* a `TranscriptRecord` round-trips through
     `parse(to_jsonl_line(...))` byte-for-byte in a unit test.
2. [x] **Provenance stamping at the writer.** `maestro/sdk.py` (`SessionLogger`)
   — accept `caller`, `session_label`, `iteration`, `task_id` at construction
   (all already in scope where `caller` is bound from `_build_sdk_options`),
   write them as the sidecar header, and encode `caller` into the sidecar
   filename (`HH-MM-SS_<caller>_<hex>.jsonl`). Behind the flag; the `.log` path
   is untouched.
   - *Depends on:* 1.
   - *Acceptance:* an AST test asserts every `SessionLogger(...)` construction
     passes a non-defaulted `caller`; a unit test asserts the header carries all
     four provenance fields.
3. [x] **Full-content turn capture.** `maestro/sdk.py` — mirror each
   `thinking`/`text`/`prompt`/tool turn into the sidecar at full content bounded
   by a new `[logs] transcript_content_max` (own cap, large enough for a real
   turn), and collapse the per-chunk `Text delta:` stream into one final `text`
   turn in the record. `_truncate` and the `.log` lines stay exactly as today.
   - *Depends on:* 2.
   - *Acceptance:* a unit test feeds a turn longer than 500 chars and asserts the
     sidecar turn is complete (up to `transcript_content_max`) while the `.log`
     line is still `_truncate`-bounded.
4. [x] **One reader owner (chokepoint).** `maestro/logs.py` — add
   `read_transcript_summaries()` (provenance-rich list: date, phase, session,
   iteration, task, turn count, outcome, size) and `read_transcript_detail()`
   (typed turns), the *sole* parsers of sidecar files, degrading to the raw
   `.log` blob when no sidecar exists.
   - *Depends on:* 1.
   - *Acceptance:* a golden/AST test (`tests/test_conversation_transcript.py`)
     asserts the `logs.py` reader is the only module parsing `.jsonl` transcript
     sidecars — the house chokepoint pattern of
     `tests/test_merge_truth_chokepoint.py`.
5. [x] **Dashboard API returns provenance.** `maestro/dashboard/routes.py`
   (`sdk_sessions`, `sdk_session_detail`) — return the reader's provenance-rich
   summaries and typed-turn detail; backward-compatible for label-less legacy
   files (they carry `phase: null` and render as the raw blob).
   - *Depends on:* 4.
   - *Acceptance:* a component test asserts `/api/logs/sdk-sessions` includes
     `phase` and `session` for a stamped record and `null` for a legacy `.log`.
6. [x] **Clean review view.** `maestro/dashboard/static/index.js`,
   `templates/index.html`, `static/index.css` — replace `el.textContent =
   data.content` with a turn renderer (prompt → thinking → text → tool calls),
   group the session list by date and phase, and add a phase/session filter.
   - *Depends on:* 5.
   - *Acceptance:* a browser test asserts the detail view renders distinct turn
     blocks and the list is filterable by phase, with no `Text delta:`
     duplication shown.
7. [x] **CLI reuses the record.** `maestro/logs.py` (`list_sdk_sessions`,
   `show_sdk_session`) — render from `read_transcript_summaries` /
   `read_transcript_detail` so the CLI listing shows phase/session and the detail
   shows typed turns; legacy `.log` files still tail as before.
   - *Depends on:* 4.
   - *Acceptance:* a unit test asserts `list_sdk_sessions` output includes the
     phase for a stamped record.
8. [x] **Flip default on + docs; retire the blob-only path.** `maestro.toml`,
   `docs/executor-details.md`, CLAUDE.md — default `structured_transcripts_enabled`
   to `true`, document the record and view, and drop the raw-blob renderer as the
   primary detail path (the legacy fallback remains for pre-rollout files). The
   one-reader transcript chokepoint (step 4) parallels the merge-truth and
   CI-truth chokepoints, which each carry one CLAUDE.md invariant line; this step
   adds exactly one such line for it, budgeted by an offsetting tightening of an
   existing telemetry bullet so no net word growth crosses
   `test_claude_md_brevity`'s cap — which is why CLAUDE.md is in the Affects row.
   - *Depends on:* 6, 7.
   - *Acceptance:* with the flag on, a stamped session renders as typed turns in
     both the dashboard and CLI; the single CLAUDE.md invariant line is present
     and `test_claude_md_brevity` passes.

## Testing Strategy

All tests use `mem_fs` for file I/O, mock the SDK/query layer, and never touch a
real network, subprocess, or `sleep`, per CLAUDE.md.

- **Unit — record round-trip (step 1):** `TranscriptRecord` → `to_jsonl_line` →
  `parse` is lossless; malformed lines are skipped, not raised.
- **Unit — full-content vs log-line (step 3):** a >500-char turn yields a
  complete sidecar turn and a `_truncate`-bounded `.log` line; the `Text delta:`
  stream collapses to one `text` turn.
- **AST — provenance at every writer (step 2):** every `SessionLogger(...)`
  construction in `maestro/` passes `caller`; regression-proof against a new
  call site forgetting it.
- **AST/golden — reader chokepoint (step 4):** the `logs.py` transcript reader is
  the sole parser of `.jsonl` sidecars, mirroring
  `tests/test_merge_truth_chokepoint.py` and `tests/test_ci_truth_chokepoint.py`.
- **Component — dashboard API (step 5):** `/api/logs/sdk-sessions` carries
  `phase`/`session` for stamped records and `null` for legacy `.log` files; the
  detail endpoint returns typed turns; path-traversal guards on the detail route
  are re-exercised.
- **Browser — review view (step 6):** the detail view renders distinct turn
  blocks with no `Text delta:` duplication; the list filters by phase; uses
  `create_router()` per the browser-fixture rule.
- **Unit — CLI reuse (step 7):** `list_sdk_sessions` / `show_sdk_session` render
  provenance from the record and fall back cleanly for legacy files.
- **Unit — config plumbing (steps 2, 3, 8):** `tests/test_config.py` asserts
  `[logs] structured_transcripts_enabled` and `transcript_content_max` round-trip
  through `Config` / `toml_config` / `maestro.toml` with their documented
  defaults, per the repo's 3-file flag pattern.
- **Fallback/guard branches:** legacy label-less `.log` (no sidecar) → blob
  fallback; flag off → byte-for-byte today's behaviour; sidecar present but
  unreadable → graceful degrade, no crash.

## Risk Assessment

- **Disk growth.** Full-content capture makes the sidecar larger than the
  truncated `.log`. Mitigation: `transcript_content_max` bounds each turn;
  retention is already operator-managed via `session_data_days`; the `.jsonl`
  sidecar is additive and can be excluded from retention independently. Measure
  the per-day delta after step 3 on the live dir before flipping the default.
- **Sensitive content.** Transcripts contain tool output and prompts that may
  include secrets. Mitigation: the view stays behind `BearerAuth` on `/account`
  and its API — never on the public `/` page — and the same secret-redaction
  applied elsewhere is applied before a turn is written to the sidecar. This is a
  trust-boundary touch: a test asserts the transcript endpoints are absent from
  the unauthenticated public surface.
- **Rollback.** Every behavioural change gates behind
  `structured_transcripts_enabled` (default `false` until step 8). With it off,
  the `.log` writer and the existing blob view are unchanged, so any step is
  safe to ship and the flag flip is the single revert point.
- **Migration.** No state-file schema migration: the sidecar is a new artifact
  alongside existing files. Historical label-less `.log` files are not
  backfilled; they degrade to the raw-blob view (see Out of scope).
- **CLAUDE.md budget.** The one-reader transcript chokepoint parallels the
  merge-truth / CI-truth chokepoints, which each carry one CLAUDE.md invariant
  line; this proposal intends exactly one such line (step 8). With only a few
  words of headroom under `test_claude_md_brevity`'s word cap, that line is
  budgeted by an offsetting tightening of an existing telemetry bullet — no net
  word growth — which is why CLAUDE.md is in the Affects row.

## Out of scope

- **Backfilling provenance** onto the ~9,367 historical label-less `.log` files
  — they remain viewable via the raw-blob fallback but carry no phase/session.
- **Live streaming** of an in-progress conversation — the view is post-hoc over
  completed sidecars, not a real-time tail.
- **The journal surface** (`/api/journal`) — Maestro's curated *subjective*
  narrative stays separate per the "journal over logs" invariant; this proposal
  targets the raw LLM turns, which today have only the inadequate blob.
- **Retention/rotation policy** for `logs/sdk/` — unchanged; operator-managed via
  `session_data_days`.
- **The `maestro_sdk` CLI transcript sidecar** (proposals 062/075) — a distinct
  token-metering artifact, untouched.

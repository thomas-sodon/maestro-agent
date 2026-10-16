# 053: `_self_review` Verification-Surface Scaling

| Field | Value |
|-------|-------|
| **Impact** | LOW (doc-only PR; phased instrumentation gated on lived data) |
| **Status** | proposed |
| **Affects** | `maestro/memory.py` (`VersionRecord`), `maestro/agent.py` (`_snapshot_version`), `maestro/evolution.py` (`_format_version_tail`, `_self_review` prompt), `maestro/migration.py` (schema fingerprint), tests |
| **Depends on** | none — adjacent to 009 and 045, neither blocks |
| **Derived from** | ultrathink concern: "Verification-surface scaling: 2-file per-file verification works when tests are self-contained, but unproven at 3+ files." |

## Description

`_self_review` (`maestro/evolution.py`) gives the reviewer LLM the plan
plus a single whole-diff blob. We've come to treat that one pass as if it
verified each touched file individually, but the property is empirically
warranted only at very small diffs. At 1–2 files the reviewer can hold the
full surface in working memory; at 3+ the implicit "per-file verification" is
unproven, particularly when test-side coupling (shared `conftest` fixtures,
module-level singletons, multi-file refactor implication chains) breaks the
"tests are self-contained" precondition the small-diff observation relied on.

This proposal does two things in sequence: it lands deterministic
instrumentation that lets us answer "do 3+ file evolutions actually fail more
often, and in which coupling classes?" from `version_archive.jsonl` plus the
existing `evolution_plans.jsonl` (Phase 1, telemetry only, zero behaviour
change), and — only if lived data justifies it — extends the existing
`_self_review` prompt with a soft *cross-file coupling rubric* gated on
`len(plan.files_affected) >= 3` (Phase 3, prompt-only). The pattern is
identical to the lineage proposals 044 and 045 established: deterministic
instrumentation → lived-data observation → soft surface → optional future hard
gate.

End state: when this track closes, Maestro can read a histogram of evolution
outcomes by file-count bucket from its own reflect-tail, and — if the data
warrants it — the reviewer LLM is asked to enumerate, for each touched file in
≥3-file diffs, which other touched files it couples to and why each remains
correct. Hard gates and partitioning are explicitly *not* part of this
proposal.

## Soft-vs-Hard Taxonomy reference

Proposal 044 defined the distinction. The Phase 3 rubric extension is a *soft
surface*: text rendered into the existing `_self_review` prompt that
influences the next LLM response only if the model weights it. The Phase 1
file-count telemetry is *not a surface at all* — it is data, not a control. A
hardening step (refuse PR creation when a rubric-elicited follow-up is
unresolved) is named in *Future bridge candidates* and explicitly
out-of-scope.

## Relevant Findings

- `maestro/memory.py` — `VersionRecord` shape. No `files_changed_count`
  field; the file-count signal exists upstream on `EvolutionPlan` but is never
  preserved on the merged record, so a histogram over the version tail is
  currently impossible without re-joining against `evolution_plans.jsonl`.
- `maestro/memory.py` — `EvolutionPlan` already carries
  `files_affected: list[str]` and a `status: PlanStatus` field that
  distinguishes `merged` / `failed` / `rejected`. The failure-side data needed
  for the histogram is therefore *already present* in
  `evolution_plans.jsonl` — only the success-side requires a schema change.
- `maestro/agent.py` — `_snapshot_version`. The single site that
  builds a `VersionRecord` for a just-merged plan; this is the slot to read
  `len(plan.files_affected)` and persist it into the new field.
- `maestro/evolution.py` — `run_quality_gates`. Whole-tree ruff/mypy/
  pytest. Catches *typed* cross-file misses (rename whose callers were
  missed) but is blind to test-side coupling that does not fail typing —
  fixture semantics drift, registry state, ordering-dependent pytest failures
  in unrelated modules. This is why Phase 3's rubric is concept-based, not
  type-based.
- `maestro/evolution.py` — `auto_fix` reads `git diff origin/main...HEAD` and is
  whole-diff. It is *not* per-file either; the same scaling concern applies.
  This proposal targets `_self_review` first because that is where the
  reviewer LLM is asked to produce a structured judgement; `auto_fix` is the
  later, repair-shaped pass.
- `maestro/evolution.py` — `_format_version_tail`. The exact slot the
  Phase 1 histogram line is appended to (or rendered alongside) — the line is
  consumed by `reflect()` and surfaced to the loop's deepest deliberation
  prompt, which is the right place for the file-count signal to land.
- `maestro/evolution.py` — `_self_review` body. Whole-diff context,
  truncated at `_SELF_REVIEW_DIFF_MAX_CHARS = 50_000`. No partitioning, no
  per-file walk, no rubric.
- `maestro/evolution.py` — exact prompt site. The numbered
  *Review Instructions* block is followed directly by the round footer; the
  Phase 3 cross-file coupling block fits between Instruction 5 and the round
  footer, gated off `len(plan.files_affected) >= 3`.
- `maestro/migration.py` — `ensure_schema()` rewrites state files when
  the SHA-256 fingerprint changes. The Phase 1 `VersionRecord` field default
  (`files_changed_count: int = 0`) plus a fingerprint bump is sufficient for
  backfill-tolerant load of legacy records.
- `docs/proposals/core/045-completeness-verification-threading-sweep.md` —
  adjacent. 045 surfaces *off-diff* sites for *changed symbols* — a
  per-symbol, deterministic, regex-driven precheck. This proposal targets a
  different miss class: *cross-file coupling* between *touched* files
  (shared fixtures, module-level state, refactor implications). The two
  compose; neither subsumes the other.
- `docs/proposals/core/009-evolution-scope-estimation.md` — adjacent. 009 is
  forward-looking complexity rating *before* implementation begins. This
  proposal is verification-side scaling *after* implementation lands. The
  same `files_affected` field is the natural input to both, but they sit on
  opposite sides of the implement step and do not overlap.
- `docs/proposals/core/044-actuator-gap-stall-escalation.md` — pattern this
  proposal reuses. Computed-on-read instrumentation; opt-in flag (when one is
  needed); soft surface first; hard gate only on lived-data evidence.
- `CLAUDE.md` — "Architecture invariants" and "Schema migration on startup"
  documents the `ensure_schema()` fingerprint contract; the Phase 1 schema
  change must update CLAUDE.md only when the code lands, not in this PR.

## Three readings of "per-file verification" — and why we settle on one

The phrase is overloaded. Disambiguating it before any code lands prevents
the wrong machinery from being built:

1. **`_self_review`'s whole-diff pass treated as per-file.** This is the
   reading this proposal addresses. The implicit assumption is that one LLM
   pass over a multi-file diff functionally amounts to a per-file check.
   Empirically reliable at 2 files; unproven at 3+.
2. **Proposal 045's per-symbol threading sweep.** Already
   well-formed — deterministic, regex-driven, surfaces off-diff sites for
   changed symbols. Different miss class. Not in scope here.
3. **Hypothetical scoped per-file pytest/mypy.** *Tempting and wrong.* The
   failure modes named below are cross-file by definition; partitioning the
   verification surface would *worsen* the property because it removes the
   only signal that catches them. Explicitly rejected — see *Designs
   considered and rejected* below.

## Concrete failure modes at 3+ files (that do not occur at 2)

These are *cross-file coupling* failures, not per-file review misses, which
is exactly why naive partitioning makes things worse:

- **Shared `conftest` fixture semantics shift.** A `conftest.py` edit changes
  fixture behaviour; tests in two distinct test modules that depend on the
  fixture pass individually but together exercise a path neither file alone
  reveals.
- **Module-level singleton / registry state.** A registry-pattern module is
  edited; two consumers in separate files mutate or read the registry; the
  diff covers all three but the reviewer's mental model treats each file in
  isolation.
- **Multi-file refactor implication chains.** File A's edit implies file B's
  untested branch now fires; B's tests pass because they cover the *prior*
  branch; the new branch is exercised only via the integration path, which
  no test exists for.
- **Test-collection ordering drift.** A new fixture or test file changes the
  pytest collection order; a module-level import-time side effect surfaces
  earlier; an unrelated test that depended on lazy initialisation now
  fails — typing-clean, ruff-clean, only fails at runtime.
- **`Protocol` / typing-stub widening with multiple downstream call-sites.**
  A `Protocol` member becomes optional; mypy is happy at every call-site;
  one of three call-sites assumed always-present and now silently degrades.

The common shape: each is *invisible* to a per-file partitioned pass. They
are visible only when the reviewer reasons about the *coupling* between
touched files, which is exactly what Phase 3's rubric asks for.

## Implementation Plan

Three independently mergeable phases. Each leaves the codebase in a working
state. The Phase 1 → Phase 3 gate is a lived-data check, not a calendar.

1. **Phase 1 — instrumentation only (later PR, doc-only here).**
   `maestro/memory.py` — add `files_changed_count: int = 0` to `VersionRecord`
   (default-zero so legacy rows roundtrip). `maestro/agent.py:_snapshot_version`
   — populate from `len(plan.files_affected)` at the existing snapshot site.
   `maestro/evolution.py:_format_version_tail` — emit one new histogram line
   summarising bucketed outcomes: `evolutions by files-touched: 1f X% / 2f
   Y% / 3+f Z%` where the percentages are success rates computed by joining
   the `VersionRecord` tail (success side, with the new field) against
   `evolution_plans.jsonl` (failure / rejection side, using the existing
   `files_affected` and `status` fields). `maestro/migration.py` — bump the
   relevant fingerprint so `ensure_schema()` rewrites legacy
   `version_archive.jsonl` rows with the default-zero field. Tests cover
   round-trip, backfill-tolerant load, and histogram rendering for empty /
   single-bucket / mixed inputs. **Zero behaviour change** to the loop.
2. **Phase 2 — lived-data observation window (no PR).** Approximately 30
   merged versions accumulate in `version_archive.jsonl`. **Decision
   criterion specified up front:** Phase 3 ships only if the 3+f bucket
   shows ≥ 10 percentage-point success-rate regression vs the 1f and 2f
   buckets *and* the recorded failures correlate with at least one of the
   five named cross-file coupling classes (verified by spot-reading the
   failed plans). **Minimum sample precondition:** ≥ 8 evolutions in each
   bucket before the gate fires. Otherwise: the proposal closes as
   `partial`, the instrumentation stays as a permanent telemetry surface,
   and the reflect-tail gains a useful new line at zero ongoing cost.
3. **Phase 3 — prompt-only rubric extension (contingent on Phase 2).**
   `maestro/evolution.py` — at the prompt site (`evolution.py`), when
   `len(plan.files_affected) >= 3`, append a `## Cross-File Coupling Check`
   block to the existing `_self_review` prompt. The block instructs the
   reviewer LLM, for each touched file, to list (a) which other touched
   files it couples to, (b) what shared state or fixture is at stake, (c)
   why that coupling does not regress. **No new SDK call, no new TOML key
   in V1** — the gate is `len(plan.files_affected) >= 3`. The reviewer's
   text is consumed in the same SDK round and is not durable. Tests cover
   prompt-shape (block present at ≥3, omitted at <3) and one happy-path
   assertion that the block leaves the LGTM-detection unchanged.
4. **Phase 4 — do nothing.** Explicit branch: if Phase 2 data does not
   justify Phase 3, the track closes here. The instrumentation remains; the
   prompt is unchanged. This is a legitimate outcome, not a failure.

## Testing Strategy

Phase 1 (deferred to a later PR — listed here for completeness):

- `tests/test_memory.py` — `VersionRecord` round-trip with the new
  `files_changed_count` field through `mem_fs`; legacy row load (the field
  absent from JSON) returns the dataclass default `0` without error.
- `tests/test_evolution.py` — `_format_version_tail` rendering test with an
  empty archive (no histogram line), single-bucket archive (only `1f`
  appears), and mixed archive (all three buckets present). Cover the join
  against `evolution_plans.jsonl` for the failure side.
- `tests/test_migration.py` — `ensure_schema()` fingerprint bump rewrites a
  fixture archive and the rewritten file roundtrips through `Memory`.
- `tests/test_agent.py` — `_snapshot_version` reads `len(plan.files_affected)`
  into the new field; mocked dependencies, `mem_fs`, no real I/O.

Phase 3 (deferred behind the lived-data gate — listed here for completeness):

- `tests/test_evolution.py` — prompt-shape assertion: build a fake plan with
  `files_affected=["a.py", "b.py"]` and confirm `## Cross-File Coupling Check`
  is **absent** from the constructed prompt; build a plan with three files
  and confirm the block is **present** with the expected language; confirm
  the block sits between Review Instruction 5 and the round footer.
- `tests/test_evolution.py` — happy-path assertion: with the rubric block
  active, an LLM response that satisfies the rubric and ends with `LGTM`
  still triggers the LGTM short-circuit.

All tests honour the project rules in CLAUDE.md: `mem_fs` for file I/O,
`SDKMock` for SDK calls, no real `sleep`, no real network, sub-5s runtime.

## Risk Assessment

- **Telemetry-only false confidence.** A 30-version window may be too short
  or biased toward simple plans; the histogram could mislead. Mitigation:
  the Phase 1 → Phase 3 gate has the explicit ≥ 8-evolutions-per-bucket
  precondition above, and the percentage-point threshold is paired with a
  qualitative "matches at least one of the five named coupling classes"
  check. Lived-data review, not raw count, decides.
- **Reviewer overload from rubric.** The Phase 3 prompt extension lengthens
  the prompt; in the worst case the rubric crowds out the reviewer's
  attention budget. Mitigation: the block is gated off `len(files_affected)
  >= 3`, so 1–2-file diffs (the dominant case empirically) are unchanged.
- **Schema migration.** `files_changed_count: int = 0` is safe under
  `ensure_schema()` rewrite; old rows roundtrip cleanly because the field
  default is supplied at dataclass construction. Verified by the migration
  test in the strategy above. Rollback posture: a single-field revert plus
  fingerprint rollback returns the schema to its prior state.
- **False positives in cross-file coupling rubric.** The rubric is soft;
  the reviewer LLM filters. Mitigation: lived-data tracks rubric-elicited
  follow-up rate (rubric block present → reviewer made any change → was
  the change useful) before any consideration of hardening to a gate.
- **Adjacency to 045.** Could the threading sweep subsume this proposal?
  No — 045 targets per-symbol off-diff drift (renames, removals, signature
  changes); this proposal targets cross-file coupling between *touched*
  files. The two compose. The Phase 3 rubric explicitly does *not* enumerate
  symbols; it asks the reviewer to reason about coupling, which is the
  failure-mode shape that 045 cannot address.

## Designs considered and rejected

- **Per-file partitioned `_self_review`.** Run one review pass per touched
  file with only that file's hunks visible. Rejected: this *destroys* the
  signal that catches the failure modes named above. The whole point is
  cross-file coupling; partitioning blinds the reviewer to it.
- **Scoped pytest-per-file (run only that file's tests).** Rejected for the
  same reason: the failure modes are tests in *other* files breaking because
  of fixture / singleton / collection-order drift in the touched files.
  Scoping the test run hides exactly the breakages we care about.
- **Soft cap on `len(files_affected)`.** Refuse to land a plan whose
  `files_affected` exceeds N. Rejected: coercive without diagnostic value;
  blocks legitimate refactors; addresses the symptom (size) rather than the
  root cause (coupling reasoning). The Phase 3 rubric addresses the root
  cause.
- **New TOML config flag for Phase 3 gating.** Rejected for V1: the
  `len(plan.files_affected) >= 3` constant is the gate; a flag is added
  only if Phase 4 hardens to a PR-creation gate, at which point it gains
  the same opt-in shape as `[completeness_check] enabled` in proposal 045
  and `[audit] stall_block_enabled` in proposal 044.

## Future bridge candidates

Each contingent on prior-phase lived data:

- **Hard cap on `len(files_affected)` for *self-directed* plans only**
  (preserve human-issued plans of any size). Precondition: Phase 2 shows the
  3+f regression *and* failures concentrate in self-directed plans.
- **Adoption of 045's threading sweep as a default-on companion when
  `len(plan.files_affected) >= 3`.** Precondition: 045 has shipped and its
  false-positive rate is acceptable.
- **Promotion of the rubric block to a hard PR-creation gate** when ≥ K
  rubric-elicited issues remain unresolved across all `self_review_max_rounds`
  rounds. Precondition: Phase 3 lived-data shows rubric-elicited follow-ups
  are useful (correlate with caught real issues, not noise).

## Out of scope

- **No code in this PR.** The Implementation Plan is the contract for a
  later PR; this PR is documentation only.
- **No new TOML key in V1.** Phase 3 gating is the constant
  `len(plan.files_affected) >= 3`. A key is added only if Phase 4 hardens
  the gate.
- **No persistence schema for the cross-file coupling rubric responses.**
  The reviewer's text is consumed in the same SDK round; it is not durable.
- **No automated mitigation.** The rubric is informational; no branch
  changes program control flow on its content in V1.
- **No new SDK call.** Phase 3 reuses the existing `_self_review` session.
- **No change to `auto_fix`, `run_quality_gates`, or `tend_evolution_prs`.**
  The same scaling concern applies to those code paths; addressing them is
  a separate proposal once Phase 3 lived data informs whether the rubric
  pattern generalises.
- **No update to `CLAUDE.md` in this PR.** The `Architecture invariants`
  section is updated when the code lands, not when the proposal does.

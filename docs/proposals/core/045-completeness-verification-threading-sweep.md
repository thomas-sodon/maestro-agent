# 045: Completeness-Verification Gap — Diff-Aware Threading Sweep

| Field | Value |
|-------|-------|
| **Impact** | LOW (opt-in, zero new SDK cost, zero schema change) |
| **Status** | proposed |
| **Affects** | `maestro/evolution.py` (new `threading_sweep()` + `_self_review` prompt extension), `maestro/agent.py` (call site), `maestro/vcs.py` (new helper `get_diff_files`), new `maestro/threading_sweep.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `CLAUDE.md`, `docs/self-evolution.md`, `tests/test_threading_sweep.py` |
| **Depends on** | none — closes a gap inside the existing `_self_review` contract |

## Description

LLM-driven implementation in Maestro has an asymmetric quality profile. The *reasoning about what to change* — planning, executor tool-calls, in-session deliberation — is strong. The *verification that the change was threaded through every site that should have been updated* degrades with surface area. As a diff grows, the reviewer is increasingly likely to miss off-diff sites: an old identifier still in a docstring, a sister module that called the renamed function, a comment describing the removed behavior, a TOML key referencing a dropped option, a README example using the prior API.

`_self_review` (`evolution.py`) gives the reviewer the plan plus the diff truncated at `_SELF_REVIEW_DIFF_MAX_CHARS`. It does not ship a cross-file reference map for the changed symbols. `run_quality_gates()` (`evolution.py`) catches *typed* misses — a renamed function whose callers were missed surfaces as a mypy error — but is blind to docstring drift, mock-fixture stubs that accept any name, comment drift, TOML drift, README drift. The enumerated drift checker `fix-inconsistencies` (`.claude/commands/fix-inconsistencies.md`) covers six fixed categories (modules / CLI / config / caps / links / Literal types) but is *category-based*, not *concept-based for the current diff*.

This proposal builds a deterministic precheck — a *threading sweep* — that runs immediately before `_self_review`. It walks the diff, extracts a small, tractable set of change-signatures (renamed top-level symbol, removed top-level symbol, retained-name signature change), and grep-scans the working tree for sites *outside the diff* that reference the affected symbol. The result is a `ThreadingReport` appended to `_self_review`'s prompt as a `## Threading Sweep` block. The reviewer LLM no longer has to *infer* what off-diff sites might exist; the sweep enumerates them.

End state: when an evolution implementation lands, the reviewer receives both *what changed* and *what plausibly should have changed but didn't* — surfaced by deterministic computation rather than stochastic re-reading. The sweep ships behind an opt-in flag (`[completeness_check] enabled`, default `false`) so we observe lived data — false-positive rate, reviewer follow-through — before defaulting on. The pattern (deterministic precheck → soft-surface delivery to LLM → opt-in flag → future hard gate) follows proposal 044's lineage.

## Soft-vs-Hard Taxonomy reference

Proposal 044 defined the distinction: a **soft surface** is text rendered into a prompt (it influences the next LLM response only if the model weights it); a **hard actuator** is a code-level branch that changes program control flow regardless of LLM output. Soft surfaces fail open; hard actuators fail closed.

`_self_review` today is a soft surface end-to-end: the reviewer reads a diff and produces text. This proposal does *not* convert it to a hard actuator. What it does is make the *generation* of the review's input deterministic over the V1 signature classes — so the reviewer can no longer miss what the sweep already surfaced. The text is still soft; what was undecidable ("did anyone reason about off-diff sites?") becomes decidable ("the sweep enumerated 0 / 7 / N off-diff sites; here they are"). Hardening — refusing PR creation until confirmed-misses are resolved — is named in *Future bridge candidates*.

## Relevant Findings

- `maestro/evolution.py` — `_self_review` body. Diff-only context, truncated. No reference map for changed symbols.
- `maestro/evolution.py` — exact prompt site. The `## Implementation Diff` section is followed directly by `## Review Instructions`; the new `## Threading Sweep` block fits between them.
- `maestro/agent.py` — `implement` → `_self_review` seam. The new `threading_sweep` runs in this gap, deterministically and outside the SDK, with its result threaded into the existing `_self_review(plan, base_hash, session=impl_session, ...)` call.
- `maestro/evolution.py` — `run_quality_gates`. Confirms why type/test gates do not catch docstring or mock-fixture drift: ruff/mypy/pytest cannot see semantic intent in unparsed strings.
- `.claude/commands/fix-inconsistencies.md` — adjacent enumerated-category drift checker. Six fixed categories; explicitly not concept-from-diff. The two are complementary, not overlapping.
- `docs/proposals/core/044-actuator-gap-stall-escalation.md` — pattern this proposal reuses. Computed-on-read, code-level branch, opt-in flag, lived data before defaulting on.

## Design — simplest viable

- **Inputs.** `(plan, base_hash, current_head)`. The diff is already read inside `_self_review`; the sweep takes the same `(base_hash, current_head)` pair and uses `vcs.get_diff(base_hash, current_head)` plus `vcs.get_diff_files(base_hash, current_head)` — a new helper added to `maestro/vcs.py` that returns the changed-paths list (e.g., wrapping `git diff --name-only base..head`).
- **Signature extraction (V1).** Three classes, regex-detected over diff hunks:
  - **Renamed top-level symbol.** A `def NAME(...)` (or `class NAME(...)`) line removed in a hunk *and* a `def OTHER(...)` (or `class OTHER(...)`) line added in the same file with the same arity but different name.
  - **Removed top-level symbol.** A `def NAME(...)` / `class NAME(...)` line removed with no matching add of the same shape in the same file.
  - **Retained-name signature change.** Same name in both removal and addition, parameter list differs.
  V1 caps to top-level definitions (column 0, no leading whitespace) — methods inside classes are out of scope until V2.
- **Sweep.** For each extracted symbol, a bounded ripgrep across the working tree, excluding the diff's own files (`vcs.get_diff_files` provides the exclusion list). For the rename and removal classes, the search target is the *old* name; for signature changes, the search target is the *retained* name (callers may pass the wrong arity). Findings are accumulated into the report with file path, line number, and a short context snippet.
- **Report shape.** Plain dataclass, in-process only:
  ```python
  @dataclass(frozen=True)
  class ThreadingFinding:
      symbol: str
      kind: Literal["renamed", "removed", "signature_changed"]
      sites: tuple[tuple[Path, int, str], ...]

  @dataclass(frozen=True)
  class ThreadingReport:
      confirmed: tuple[ThreadingFinding, ...]
      suspected: tuple[ThreadingFinding, ...]
      truncated: bool

      @property
      def clean(self) -> bool:
          return not self.confirmed and not self.suspected
  ```
  No JSONL persistence in V1. The report lives across the `_self_review` call only. No new tracked dataclass; no `compute_schema_fingerprint()` bump.
- **Prompt injection.** A new `## Threading Sweep` block appended to the `_self_review` prompt below `## Implementation Diff`. When `report.clean`, the block reads `Threading sweep clean — no off-diff references to renamed/removed/signature-changed symbols.` so the reviewer sees the deterministic confirmation. Otherwise the block lists confirmed before suspected, capped at `max_findings`, with a `(N more truncated)` tail when applicable. Format mirrors `_format_version_tail`'s brevity.
- **Configuration.** New `[completeness_check]` TOML section:
  - `enabled` (bool, default `false`) — gates the sweep entirely.
  - `max_findings` (int, default `20`, ≥ `1`) — cap on entries surfaced into the prompt.
  Two corresponding `Config` fields. Validated via the existing `toml_config` overlay path.
- **Call-site shape (`agent.py`).**
  ```python
  threading_report = (
      self.evolution.threading_sweep(plan, base_hash)
      if self.config.completeness_check_enabled
      else None
  )
  if self.config.self_review_max_rounds > 0:
      await self.evolution._self_review(
          plan, base_hash, session=impl_session,
          threading_report=threading_report,
      )
  ```
  The sweep runs once per implementation, before any `_self_review` round; rounds 2+ see the same report (the diff base is unchanged across rounds and the new symbols are stable).

## Self-referential proof-of-concept

The self-review subsystem now surfaces its own blind spot. Deterministic computation supplies what stochastic reading reliably misses. The pattern reused: precheck → soft-surface delivery → opt-in flag → future hard gate. After 044 wired the audit subsystem into a hard actuator, this proposal extends the bridge into the implementation-review subsystem — with the deliberate restraint of staying soft until lived data justifies hardening.

## Testing Strategy

Unit tests in `tests/test_threading_sweep.py`, mocking the VCS layer and using the `mem_fs` fixture for any working-tree reads:

- **Signature extraction.**
  - `test_detects_renamed_top_level_def` — a hunk that removes `def foo(a, b):` and adds `def bar(a, b):` produces a `renamed` finding for `foo`.
  - `test_detects_removed_top_level_class` — a hunk that removes `class Old:` with no matching add produces a `removed` finding.
  - `test_detects_signature_change_retained_name` — `def foo(a)` → `def foo(a, b)` produces a `signature_changed` finding for `foo`.
  - `test_ignores_method_inside_class` — V1 cap: indented `def` is not extracted.
  - `test_ignores_within_unchanged_block` — only `+`/`-` lines contribute.
- **Sweep behavior.**
  - `test_sweep_excludes_diff_files` — when the only off-diff usage is in a file already in the diff, no finding is produced.
  - `test_sweep_caps_at_max_findings` — when sites exceed `max_findings`, `truncated=True` and surplus is dropped.
  - `test_sweep_clean_when_no_offdiff_references` — produces `ThreadingReport(clean=True)`.
  - `test_disabled_returns_none` — when `[completeness_check] enabled=false`, `threading_sweep` is not called and the prompt does not include the block.
- **Prompt integration.**
  - `test_self_review_prompt_includes_threading_block_when_report_present` — a fake `ThreadingReport` produces the `## Threading Sweep` section in the prompt; absence omits the section entirely.
  - `test_self_review_prompt_renders_clean_marker` — clean report produces the `Threading sweep clean — …` line.
- **Fallback / guard.**
  - `test_truncated_diff_falls_back_to_file_walk` — when the diff is truncated by `_SELF_REVIEW_DIFF_MAX_CHARS`, signature extraction iterates the file list from `vcs.get_diff_files` rather than relying on the diff text.
  - `test_default_disabled` — proves the default `enabled=false` keeps `_self_review` byte-identical to today.

Tests use `mem_fs`, mock the VCS, and never touch real git or real I/O — consistent with CLAUDE.md's testing principles. No browser tests are needed (no UI surface).

## Risk Assessment

1. **False positives.** The old symbol name legitimately appears in a comment-of-record, a removed-feature changelog entry, or a string literal in an unrelated test. Mitigation: delivery is advisory; the reviewer LLM filters. Lived-data observation tunes V2 heuristics (e.g., suppress comment-only matches, suppress matches inside string literals).
2. **Regex-vs-AST.** A pure-regex parser is brittle for nested defs and decorators. Mitigation: V1 caps to top-level `def`/`class` at column 0; an AST-based extractor is a V2 concern when lived data surfaces concrete misses.
3. **Reviewer overload.** Too many findings push real signal off the prompt. Mitigation: `max_findings` cap (default 20), confirmed-before-suspected ordering, and a `(N more truncated)` tail.
4. **Reviewer ignores the report.** The proposal's whole premise is that reviewers miss things. Mitigation: lived-data observation tracks resolution rate; if low, the future hard-gate bridge is justified — the report becomes the input to a PR-block actuator.
5. **Performance.** Ripgrep is fast but unbounded grep × diff symbols × repo size grows. Mitigation: cap on signatures-per-diff (≤ `max_findings`) and on sites-per-signature; an explicit time budget is added if telemetry shows the sweep adding > 2 s per implementation.
6. **Phase resume.** Sweep is pure-read, idempotent. Safe under `_LOOP_PHASES` resume; if a crash happens between sweep and `_self_review`, the next attempt regenerates the report.
7. **Truncated diff.** When `_truncate_diff` clips the input, signature extraction sees only the prefix. Mitigation: walk the file list via `vcs.get_diff_files(base_hash, current_head)` for symbol enumeration, falling back to the diff text only when AST-of-file fails (or, in V1, when the regex extractor over-the-wire is sufficient).
8. **Renamed file.** A removed `def foo` in `old.py` and an added `def bar` in `new.py` is technically a rename + module-move; V1's "same file" heuristic misses it. Acceptable: this is a known V1 limitation — the rename is still in the diff and `_self_review` sees both halves; the sweep just doesn't add new information beyond what the reviewer already has.
9. **Threshold change mid-flight.** Lowering `max_findings` clips immediately (desired); raising relaxes (desired). No persistence to migrate.

## Future bridge candidates (out-of-scope for this proposal)

The pattern this proposal establishes opens the door for further self-knowledge → actuator bridges in the implementation-review subsystem:

- **Hard PR-creation gate.** When confirmed-miss count exceeds a threshold, refuse PR creation until either auto-fix resolves the misses or a human approves through the dashboard. Converts the soft surface into a hard actuator.
- **V2 signature classes.** New TOML key adds (Config dataclass + maestro.toml + docs alignment), new `_LOOP_PHASES` entries (telemetry / `_BUDGET_GATED_PHASES` / `_phase_will_run` alignment), new CLI commands (CLAUDE.md table + README usage), new module-level constants (alignment with documented caps in CLAUDE.md's Architecture invariants).
- **Migration-fingerprint sweep.** Detect dataclass-field changes to types in `_TRACKED_CLASSES` and surface "did you bump `compute_schema_fingerprint()`?" as a finding.
- **Sister-feature ripple.** When a function in `consciousness.py` is updated, surface adjacent functions in `purpose.py` / `dreaming.py` / `journal.py` that grep the same field, asking whether they need parallel updates.
- **Mock-fixture sweep.** Cross-reference renamed symbols against `tests/conftest.py` and component-test fixtures to surface fixtures whose surface still matches the old name.

Each follows the same template: deterministic computation, advisory by default, opt-in flag, lived-data validation before promotion.

## Out of scope

- No persistence schema change. No `_TRACKED_CLASSES` entry. No fingerprint bump.
- No PR-creation gate (named in Future bridge candidates).
- No automatic fix application.
- No new SDK call. The sweep is zero-cost.
- No CLAUDE.md "Architecture invariants" capacity-cap detection (deferred to V2).
- No method-level (inside-class) signature analysis (deferred to V2 alongside AST upgrade).

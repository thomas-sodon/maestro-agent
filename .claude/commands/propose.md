Author a comprehensive proposal doc that addresses a described concern at the structural level, grounded in live production data and the current code.

This command is for **systemic design, not spot fixes**. The free-form text in `$ARGUMENTS` describes a concern — functionality that is misbehaving, degrading, or repeatedly patched without getting better. The job is to treat that text as one *symptom of a class*, find every other manifestation of the same class in the live production data and the repo's fix history, identify the structural root that keeps spawning the symptoms, and write a proposal at `docs/proposals/<category>/NNN-<slug>.md` whose remedy closes the whole class. The house precedents for this shape are proposals 087 (one provenance-stamped merge-truth oracle behind one AST-enforced chokepoint, replacing scattered per-site merge checks), 070 (the emergent PR-remediation gate pile turned into an explicit state machine), and 080 (one merge oracle + lap budget + standdown ledger ending an open-ended remediation treadmill) — each retired a family of point fixes by changing the structure that produced them. `/rca` diagnoses one failure; `/bug-fix` fixes one issue; **this command exists so the third fix to the same subsystem becomes a redesign instead of a fourth fix**.

`$ARGUMENTS` is a space-separated list of tokens. Recognized flag tokens are listed below; **everything else is joined into the free-text concern** — required. No concern text → stop with `error=missing_concern` and an example invocation (`/propose PR remediation keeps re-lapping merged PRs; every fix adds another gate and the queue still clogs`).

Recognized tokens:
- `--data-dir=PATH` — explicit path to the live `data/` directory (overrides auto-detection)
- `--container=NAME` — read live data via `docker exec` on this container when the host bind-mount lookup fails (default: `maestro-loop`)
- `--category=<name>` — force the proposal category; must be one of `core`, `security`, `publication`, `audience`, `research`, `ux`, `technical-plan`, `operations`, else `error=invalid_argument`
- `--dry-run` — print the full proposal body and the README table row to stdout; write nothing

## Locate the live data directory

Production evidence is a first-class input but its absence is not fatal — the investigation degrades to code-and-history-first, and the proposal must say so. Resolution order (same as `/rca`):

1. If `--data-dir` is set, use it. Verify it contains `lifecycle.json`; if not, warn that the path is invalid and continue without live data.
2. Else try the host bind-mount of the loop container:
   ```
   docker inspect maestro-loop --format '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}'
   ```
   If the result is non-empty and `<source>/data/lifecycle.json` exists, use `<source>/data`.
3. Else fall back to `docker exec` against `--container` (default `maestro-loop`); all reads become `docker exec <container> cat <path>` / `... ls <dir>`. Note the fallback in the proposal's evidence section.
4. If none work, proceed without live data and record `no live data available — evidence is code + git history only` in the proposal.

Record the resolved dir as `$DATA_DIR`. Never read from `/opt/maestro-dev/data` — a stale dev-tree directory the live loop does not write to. All reads of `$DATA_DIR` are **strictly read-only**.

## Mine the production evidence

The point of this step is **quantification and enumeration**: how often does the class fire, in how many distinct shapes, since when, at what cost. Do not read every file — map the concern's domain to the relevant stores and mine those:

| Concern domain | Files under `$DATA_DIR` |
|---|---|
| Task outcomes, remediation laps, repeated `Remediate PR #N` | `task_history.jsonl` (+ cold sibling if rotated), `task_queue.json` |
| Loop health, phase failures, crashes, throttling | `lifecycle.json`, `logs/maestro.log` (tail ~400 lines), `crash` milestones in `journal.jsonl` |
| Self-model / audit findings, stalled questions | `audit_history.jsonl`, `self_model.json`, `self_model_archive.jsonl`, `question_resolutions.jsonl` |
| Evolution plans, rejected/failed/obsolete churn | `evolution_plans.jsonl`, `rejection_history.jsonl`, `version_archive.jsonl` |
| Gate firings, directives, escalations, standdowns | `gate_activations.jsonl`, `stand_down_verdicts.jsonl`, `dropped_directives.jsonl`, `lever_attempts.jsonl`, `escalation_standdowns.json` if present |
| Cost / call volume | `usage.jsonl` |
| SDK turn content around a specific incident | `logs/sdk/<date>/` (quote only the decisive span; redact) |
| Operator communication | `messages.jsonl` |
| Predictions / calibration | `predictions.jsonl` |

For each relevant store, extract **counts and instances, not impressions**: e.g. "14 of the last 40 tasks are `Remediate PR #N` laps against 3 PR lineages; PR #2731 alone consumed 6 laps, 5 stamped `partial`, over 9 days". Prefer small Python one-liners over eyeballing (`python3.14 -c` reading JSONL tails). Where a counter already exists in `maestro/remediation_tracker.py` / `maestro/audit_checks.py` (e.g. `measure_unlanded_remediations`, `measure_no_progress_laps`, `measure_noop_rate`), reuse its definition of the metric rather than inventing a rival one, and name the sensor in the proposal. Every number that ends up in the proposal must state the window it was measured over. Redact tokens, `Bearer ` headers, API keys, and Telegram bot tokens (`(?:bot)?\d+:[A-Za-z0-9_-]{30,}`) from anything quoted.

## Map the whack-a-mole history

This is the evidence that the current approach is point-fixing a structural problem — the justification for a proposal instead of another patch:

1. **Repo history at the implicated modules.** `git log --oneline -30 -- maestro/<module>.py` for each implicated module; wider `git log --oneline --grep=<key term> -20` for the concern's nouns. Collect the point fixes that touched the same behavior (issue-numbered fix branches, `fix-NNN-…` merges).
2. **Merged-plan history.** Scan `version_archive.jsonl` (live) and the Shipped table in `docs/proposals/README.md` for prior changes aimed at the same subsystem.
3. **Open/prior proposals.** Grep `docs/proposals/**/*.md` for the concern's key terms. Note which proposals already touched this ground and what they deliberately deferred.
4. Build the **whack-a-mole ledger**: a table of prior point fixes — identifier (SHA, PR number, or issue number), one-line what-it-patched, and why the class survived it. Three or more entries is strong evidence for a structural remedy; zero or one entry is a signal the concern may be a point defect (see the honesty valve below).

## Read the code holistically

Code-first, whole-subsystem — not just the function named in the concern:

- Map the concern's nouns to modules with Grep/Glob; read the implicated functions **in full plus their direct callers and callees**. Check the relevant CLAUDE.md invariants — many recurring symptom classes are an invariant that exists in prose but has no enforcement, or several sites each re-implementing a fact that has no single owner.
- Identify the **structural root**, which is usually one of: duplicated state with no single source of truth; a fact re-derived at many call sites with no chokepoint; a documented rule with no test enforcing it; missing feedback (failures invisible until they compound); logic living at the wrong layer or with the wrong owner; an accreted pile of per-symptom special cases where a named state machine or registry should be.
- Read the tests for the implicated code. A test asserting the buggy behavior, or a family of near-duplicate tests each pinning one whacked mole, is itself structural evidence.
- Use the **Explore** subagent for breadth (adjacent call sites, similar prior mechanisms to reuse); read the decisive code first-hand yourself. Up to 3 parallel Explore calls only when the concern genuinely spans loop + persistence + surface.

## Check overlap with existing proposals

Before authoring, decide the relationship to the existing corpus (from the grep in the history step):

- **Fully covered by an open proposal** → do not write a duplicate. Stop and report the covering proposal, what evidence this investigation adds, and suggest `/plan-proposal NNN` instead. (In `--dry-run`, print this verdict.)
- **Partially covered** → the new proposal names each adjacent proposal, states what it reuses via the `Depends on` metadata row, and scopes itself to the uncovered structural gap.
- **Supersedes a stale open proposal** → say so explicitly in the Description; do not edit or delete the old file (its status row is curated by hand at PR time).

## The honesty valve — when NOT to write a proposal

If the investigation shows the concern is a **single point defect** — one bad branch, no recurrence in production data, no trail of prior fixes, no structural root behind it — a proposal is the wrong deliverable. Stop, report the finding with its evidence, and recommend `/rca <description>` (diagnosis) or `/bug-fix` (fix) instead. Writing a "comprehensive" proposal around a one-line bug is its own kind of whack-a-mole: it dresses a mole up as architecture. The command must be willing to return "this does not need a proposal."

## Design the structural remedy

The remedy section is what separates this from a bug report. Requirements:

1. **Name the class, then close it.** State the failure class in one sentence. Enumerate every manifestation found (production evidence + whack-a-mole ledger) and show, item by item, how the design closes each — any manifestation the design does *not* close is listed under an explicit `## Out of scope`, never silently dropped.
2. **One structural change, not N patches.** Prefer, in this order: a single owner/oracle/chokepoint for the contested fact (087-style); an explicit named state machine or declarative registry replacing an accreted if-pile (070/069-style); a deterministic zero-LLM-cost invariant enforced by an AST or golden test (`tests/test_structural_guards.py`, `tests/test_no_silenced_exceptions.py` are the house pattern); a self-extinguishing sensor + actuator pair when the fix must act over time. If the design still needs per-symptom branches, that is a sign the root is one level deeper — iterate before writing.
3. **Retire the moles.** The design names which prior point fixes, gates, or special cases it subsumes and can be deleted once it lands. A structural remedy that only *adds* mechanism on top of the existing pile is suspect.
4. **Rollback posture.** Behavior changes gate behind a config flag (`[loop]` / `[audit]` / relevant section, default matching current behavior); steps are independently mergeable so an interrupted rollout leaves the system working.
5. **Falsifiability.** State the observable production metric that improves if the proposal is right (tie it to an existing sensor where one exists) and what result would show the design failed.

### Forbidden remedy patterns

The Implementation Plan MUST NOT be built from these (same family as `/rca`'s forbidden fixes):

- Bumping a timeout / cap / retry count / threshold as the primary remedy, unless the evidence proves the limit itself is the root.
- Adding one more special-case gate, marker, or exception to the existing pile for this symptom — that is the definition of the next mole.
- Silencing: broad `except`-and-continue, log-level demotions, `# type: ignore` on real type bugs, quality-gate bypasses (`pytest.skip`, coverage pragmas).
- Auto-recovery that hides the class instead of closing it (blind retries on non-transient failures, `restart: always`).

## Author the proposal document

1. **Number.** `NNN = max + 1` over every number that appears in `docs/proposals/**/[0-9][0-9][0-9]-*.md` filenames *and* both README tables (Shipped reserves its numbers). Zero-pad to 3 digits.
2. **Category.** Use `--category` if given; otherwise apply the eight-rule decision tree in `docs/proposals/README.md` § "How to pick the category" in order, first match wins.
3. **Slug.** Short kebab-case from the title; file is `docs/proposals/<category>/NNN-<slug>.md`.
4. **Layout.** Follow `docs/proposals/TEMPLATE.md` exactly (do not modify the template itself): metadata table (`Impact` HIGH/MEDIUM/LOWER, `Status` `proposed`, `Affects` with real current paths, `Depends on` from the overlap step, `Derived from` → `live production data via /propose, <date>`), then `## Description`, `## Relevant Findings`, `## Implementation Plan`, `## Testing Strategy`, `## Risk Assessment`. Add two optional sections the template's optional-section convention accommodates:
   - `## Production Evidence` — the quantified findings from the mining step, each with its measurement window and the `$DATA_DIR` file it came from (quoted spans tight, 5–15 lines, redacted). If no live data was available, this section says so in one line.
   - `## Prior Point Fixes (whack-a-mole ledger)` — the table from the history step, plus which entries the design retires.
5. **Citation discipline.** Cite by file path and **symbol** — `maestro/agent.py` (`is_pr_settled`), `Memory.archive()` — **never `file.py:<line>`**; `tests/test_no_line_citations_in_docs.py` bans the token outright and CI will fail on it. This applies to every section including the quoted-evidence markers.
6. **Implementation Plan shape.** Numbered `[ ]` checkbox steps per the template: bold title + files touched, 1–3 sentences, `Depends on:` step numbers, `Acceptance:` one testable invariant. Order so an interrupted run leaves the codebase working; flag-gate behavior changes. Steps that retire subsumed moles come after the replacement is proven, never before.
7. **Testing Strategy.** Honour CLAUDE.md testing principles (`mem_fs`, no real network/sleep/subprocess, mocked externals); name the deterministic enforcement test from the remedy design explicitly.

## Register it in README.md

Insert one row into the **Open Proposals** table of `docs/proposals/README.md`, in numeric order: `| [NNN](<category>/NNN-<slug>.md) | <Title> | <Impact> | proposed |`. Touch nothing else in the README — not the Shipped table, not the Recommended Implementation Order, not the track prose (prioritization is curated by hand).

## Verify

- `python3.14 -m pytest tests/test_no_line_citations_in_docs.py -v --no-cov` — must pass with the new file present.
- Re-read the written proposal once end-to-end: every manifestation is either closed by a step or listed out of scope; every number has a window; every citation is a symbol; the ledger's retired moles appear as retirement steps.

## Report

End with, in order: the proposal path and number; the one-sentence failure class; the structural root in one short paragraph; the headline production numbers; how many prior point fixes the design retires; and the follow-ups — `/create-pr` to put it up for human review (this command does not commit), and `/plan-proposal NNN` once merged. In `--dry-run`, print the full proposal body and the README row instead, clearly marked as not written. If the honesty valve or the fully-covered overlap verdict fired, report that verdict and the recommended command instead of a proposal path.

## What to skip

- Do not commit, stage, push, or open a PR — the proposal lands in the working tree for human review via `/create-pr`.
- Do not edit CLAUDE.md, `docs/proposals/TEMPLATE.md`, any existing proposal file, source code, or tests.
- Do not implement any part of the remedy, however small.
- Do not file GitHub issues (`/rca` and `/bug-hunt` own that surface).
- Do not add the proposal to the Recommended Implementation Order.

## Safety

- **Strictly read-only against `$DATA_DIR` and the live containers.** No writes to `data/`, no `docker stop`/`restart`/`compose` against the loop or dashboard — the loop's fail-loud posture depends on dead containers staying inspectable.
- The only writes are the new proposal file, the one README table row, and stdout in `--dry-run`.
- Redact tokens, OAuth headers, API keys, and Telegram bot tokens from every quoted span before it enters the proposal — the doc is destined for a public PR.
- If the evidence is too thin to defend a structural claim (no live data *and* no fix trail *and* the code reads clean), say so and stop rather than authoring speculation; a proposal built on impressions is worse than none.

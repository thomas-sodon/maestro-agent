# 127: CI Action Runtime Currency — One Enforced Node-Runtime Floor and Automated Update Feedback for the Actions CI Depends On

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `.github/workflows/quality-gates.yml`, `.github/workflows/code-review.yml`, `.github/workflows/reply-coverage.yml`, `docs/ci.md`, new `.github/dependabot.yml`, new `tests/test_workflow_action_runtime.py`, new `.github/actions/py-setup/action.yml` _(optional step)_, tests |
| **What landed** | Steps 1, 2, and 5 (commit `34bf1c3d`, PR #3724); step 3; step 4 (this change). Step 1 — every Node-20 pin is bumped past the floor: all six `actions/checkout@v4` → `@v5` and all six `actions/setup-python@v5` → `@v6` across `.github/workflows/quality-gates.yml` (the `quality`, `browser-tests`, `review-quality`, `sdk-quality` jobs), `.github/workflows/code-review.yml`, and `.github/workflows/reply-coverage.yml`; no workflow pins a mapped action below the floor. Step 2 — `tests/test_workflow_action_runtime.py` is the single owner of "what Node runtime CI requires": one `MIN_NODE_RUNTIME = 24` floor, one reviewed `ACTION_NODE_RUNTIME` table that also maps the below-floor `@v4`/`@v5` majors so a stale pin resolves to a real runtime *under* the floor rather than to `None`, the pure `resolve_action_node_runtime(uses_ref)` resolver (exempting shell-only steps, blank refs, `docker://…` container actions, and local `./…` composite call sites) raising `UnmappedActionError` on any other unmapped ref, the `_iter_action_uses` walker covering both `jobs.<job>.steps` and a composite's `runs.steps`, `_below_floor_report` naming every offender, and real-tree sweeps `_committed_workflows` (fails loudly on an empty glob, sweeps `.yml` *and* `.yaml`) plus `_committed_composite_actions` (forward-cover for `.github/actions/**/action.{yml,yaml}`, matching nothing today), with `_KNOWN_WORKFLOWS` pinning the scanned set against a workflow silently dropping out. Step 3 — the automated-feedback half: `.github/dependabot.yml` sweeps the `github-actions` ecosystem weekly at the repo root, collapsing every bump into one grouped PR (a `groups` entry matching `"*"`, `open-pull-requests-limit: 3`) so one CI run covers the batch and the new major's `ACTION_NODE_RUNTIME` row lands in the same diff as the pin it describes; `code-review.yml`'s `review` job carries the carve-out `if: github.event.pull_request.user.login != 'dependabot[bot]'` so a secret-less Dependabot PR (empty `CLAUDE_CODE_OAUTH_TOKEN`) is not wedged by a permanently-red AI-review check, pinned to the exact bot login rather than a general `[bot]`-suffix match; `tests/test_dependabot_config.py` pins both the sweep shape and the carve-out expression verbatim against silent drift; and `docs/ci.md` (§Auto-Update) documents the *update-feedback* third axis alongside the runtime-currency and immutability axes. Step 5 — `docs/ci.md` (§Auto-Update) now names `tests/test_workflow_action_runtime.py` as the owner of *runtime currency* and scopes the still-deferred digest-pin pass to the separate *immutability* axis, so the discipline is a test rather than an aspiration. Step 4 — the composite-action consolidation: the `actions/setup-python@v7` pin and `python-version: "3.12"`, previously duplicated across the six Python jobs, collapse into one `.github/actions/py-setup` composite each job calls after its own `checkout` (`pip install` stays per-job, as the install command differs by job); `tests/test_workflow_action_runtime.py` now owns the single-source property — exactly one `setup-python` pin, under `.github/actions/`, and one `python-version` — and makes its composite sweep mandatory (a non-empty assertion plus `_KNOWN_COMPOSITE_ACTIONS`), so the twelve occurrences collapsed to two cannot silently drift apart. |
| **What is missing** | Nothing in scope. Step 4 (the optional composite consolidation) now lands with this change — `.github/actions/py-setup` exists and single-sources the `setup-python` pin and `python-version`; see What landed. Only the digest-pin/immutability axis stays deferred, and it is explicitly out of scope (see Out of scope). |
| **Depends on** | 125 _(adjacent — reuses its workflow-YAML-test precedent; orthogonal axis, not a landing prerequisite)_ |
| **Derived from** | code + git history via /propose, 2026-09-04 (no live loop data available — see Production Evidence) |

## Description

> Status updated to shipped on 2026-09-06 by /plan-proposal — see [README.md](../README.md#shipped) for the landing record.

Every CI job that runs Python does so through two first-party GitHub Actions —
`actions/checkout` and `actions/setup-python` — whose **Node runtime is an
implicit, derived property of the action's major version**: `checkout@v4` and
`setup-python@v5` run on Node 20, while `checkout@v5` and `setup-python@v6` run
on Node 24. That derived fact is re-stated at twelve
independent call sites across three workflow files, with no single owner of
"what runtime CI requires," no test enforcing a minimum, and no automated
feedback that surfaces a new major. The runtime is never written down; it is
only implied, six times over, by a version tag.

This matters *now* because the implied fact has come due. GitHub forces
JavaScript actions onto the Node 24 runtime by default from **2026-06-02** and
**removes Node 20 in September 2026** — this month. The workflows still pin
`actions/checkout@v4` and `actions/setup-python@v5`, both Node 20. When the old
runtime is removed, those actions stop working and the **Quality Gates**
workflow goes red. Maestro's autonomous deploy fires only on a *green* Quality
Gates run (`deploy.yml`, `workflow_run` + `conclusion == 'success'`), and
branch protection requires green CI to merge, so a silent runtime removal does
not merely fail one job — it **halts the loop's ability to land any evolution
PR and to deploy itself at all**, with no code change on Maestro's side to
explain it.

The presenting symptom — "update workflows to run on at least Node 24" — is one
bill for a structural gap: the repo already *owns* version currency for one
dependency class (Python packages: `constraints.txt`, proposal 121's
reproducible resolution, the "exact-pin regression guards"), but has **no
equivalent owner for the CI-action class**. The end state closes the asymmetry.
A single deterministic, zero-LLM-cost test declares the minimum runtime and
enforces it across every workflow, so a below-floor pin is a red build instead
of a surprise outage; a `dependabot.yml` for the actions ecosystem surfaces the
*next* major as a reviewable PR before its deadline rather than after a
breakage; and the current bump to `@v5`/`@v6` is the floor's first consequence,
not a one-off patch that leaves the fact unowned for the Node 24 → 26 wave to
re-discover.

This is **adjacent to, and orthogonal from, proposal 125**. That proposal owns
*where* CI runs — the `runs-on:` label, host placement, the Blacksmith
migration (already landed: `tests/test_auto_update_workflow.py` now pins
`blacksmith-4vcpu-ubuntu-2404`) — and explicitly scopes "a general digest-pin
pass across all actions" *out*. This proposal owns *what runtime the actions
require*, keyed on `uses:` rather than `runs-on:`; the two never touch the same
field, so neither blocks the other. 125 is named here for its test precedent,
not as a landing prerequisite.

## Production Evidence

**No live loop data was available for this investigation.** The Docker socket
was not accessible from the authoring host (`permission denied`), so the
`maestro-loop` container's live `data/` could not be read, and the host tree's
`data/` is a stale dev copy the live loop does not write to. Per the /propose
degradation rule, the evidence below is **code + git history only**, and the
structural claim rests on the current tree and the dated upstream deprecation
rather than on loop telemetry. This class does not manifest in the loop's data
stores (`task_history.jsonl`, `audit_history.jsonl`, …) in any case — it lives
entirely in the CI configuration and its upstream runtime dependency.

**The twelve Node-20 pins** (read from the tree, 2026-09-04) — every occurrence
of the two Node-based first-party actions, all below the Node-24 floor:

```
.github/workflows/quality-gates.yml   actions/checkout@v4     ×4  (quality, browser-tests, review-quality, sdk-quality)
.github/workflows/quality-gates.yml   actions/setup-python@v5 ×4
.github/workflows/code-review.yml     actions/checkout@v4     ×1
.github/workflows/code-review.yml     actions/setup-python@v5 ×1
.github/workflows/reply-coverage.yml  actions/checkout@v4     ×1
.github/workflows/reply-coverage.yml  actions/setup-python@v5 ×1
```

**The runtime dependency is dated and imminent** (verified upstream,
2026-09-04): GitHub forces the Node 24 runtime by default from 2026-06-02 and
removes Node 20 in September 2026; `actions/checkout@v5` and
`actions/setup-python@v6` are the first majors on Node 24. So the current pins
are not "old," they are **on a runtime being removed this month**.

**No feedback and no enforcement exist** (read 2026-09-04): there is no
`.github/dependabot.yml`, no `renovate.json`, and no test under `tests/`
matching a Node-runtime or action-currency floor. The only workflow-shape test,
`tests/test_auto_update_workflow.py`, pins one unrelated file and asserts that
its action ref is *pinned*, never that it is *current*.

**The discipline is prose, not a gate** — `docs/ci.md` (§Auto-Update) states the
repo's "`@v4`/`@v5` tag-pin discipline" and, in the same breath, *defers* "a
digest-pin pass across all actions." The action-reference fact is described and
punted, never owned.

## Prior Point Fixes (whack-a-mole ledger)

This ledger is deliberately **shorter than proposal 125's** nine-entry table.
The point-fix trail for *this exact symptom* — a prior Node-runtime bump — is
essentially empty, and that emptiness is itself the finding: the fact has never
been *owned*, only *carried across* other edits. The structural case rests on
the demonstrable currency asymmetry (entry 4) and the imminent dated
deprecation, not on a long history of failed patches.

| # | Identifier | What it patched | Why the class survived |
|---|---|---|---|
| 1 | commit `9427f0fc` "Migrate workflows to Blacksmith" | Rewrote every `runs-on:` across all workflows to the Blacksmith label — a wholesale, every-file workflow edit. | It changed *where* jobs run, not *what runtime* the actions require. A pass that had all twelve pins in hand carried them across untouched, because nothing measures currency — the `@v4`/`@v5` staleness rode straight through a full rewrite. |
| 2 | `docs/ci.md` §Auto-Update + `tests/test_auto_update_workflow.py` (`test_action_ref_is_pinned` docstring) | Named a "`@v4`/`@v5` tag-pin discipline" and pinned `auto-update.yml`'s single ref against silent drift. | The "discipline" is a sentence plus one file's pin. No test enforces a runtime floor across the workflows that actually run Node actions, so the discipline is aspirational and drifts silently on the twelve pins it never covers. |
| 3 | `docs/ci.md` (deferred "digest-pin pass across all actions") | Deferred a general action-reference hardening pass to "later." | The action-reference fact — both its immutability axis (digests) and its currency axis (majors) — is repeatedly *punted*. Proposal 125 punts it again in its Out of scope. Punting is not owning; the fact stays unenforced. |
| 4 | proposal 121 step 4 + `constraints.txt` + commit `99504e26` "exact-pin regression guards" | Built reproducible-resolution pinning and regression guards for *Python* dependencies. | The repo owns version currency for one dependency class (Python: pinned, constrained, guarded) and has **no equivalent owner for the CI-action class**. That asymmetry is the structural root, and the Node-24 deadline is the first bill it comes due on. |

**Subsumed by this design:** entry 2 — the aspirational "discipline" becomes an
enforced floor (steps 2 and 5). Entry 1's failure mode — a wholesale workflow
edit leaving pins stale — is henceforth a red build (step 2's test). Entry 4 is
not retired; it is *closed on the action side* (steps 2–3 give CI actions the
currency ownership Python deps already have). Entry 3's digest/immutability axis
stays **out of scope** — this proposal owns only the currency axis (see Out of
scope). There is no accreted gate-pile to delete here, because the fact was
never patched, only left implicit; the structural change replaces one
*implicit, duplicated, unenforced* fact (twelve hand-carried pins) with one
*explicit, single-owned, enforced* floor.

## Relevant Findings

- `.github/workflows/quality-gates.yml` (jobs `quality`, `browser-tests`,
  `review-quality`, `sdk-quality`) — each job pins `actions/checkout@v4` and
  `actions/setup-python@v5`; the Node runtime is never stated, only implied by
  the major. Four jobs, eight of the twelve pins.
- `.github/workflows/code-review.yml` (`review` job) and
  `.github/workflows/reply-coverage.yml` (`reply-coverage` job) — the same
  `checkout@v4` + `setup-python@v5` pair in each, the remaining four pins.
- `.github/workflows/auto-update.yml` (the
  `docker://chinthakagodawita/autoupdate-action:v1` step) — a **container**
  action, run as its own image, so the JavaScript-action Node-runtime removal
  does not apply to it. Correctly outside the floor; its mutable-tag hardening
  is proposal 125's territory.
- `.github/workflows/deploy.yml` (`deploy` job) — shell-only steps on
  `self-hosted`, with no `actions/checkout` or `actions/setup-python`. Also
  outside the floor.
- `docs/ci.md` (§Auto-Update) — states the "`@v4`/`@v5` tag-pin discipline" as
  prose and defers "a digest-pin pass across all actions"; the action-reference
  discipline is documented but unenforced and unowned. This is the doc the
  reconciliation step edits.
- `tests/test_auto_update_workflow.py` (`_load_workflow`, `_load_job`,
  `test_action_ref_is_pinned`) — the house precedent for pinning workflow YAML
  shape with a deterministic `yaml.safe_load` read of the real tree. The floor
  test reuses this parse pattern, but note its scope: one file, and it asserts a
  ref is *pinned*, never that its runtime is *current enough*.
- absence of `.github/dependabot.yml` / `renovate.json` — no automated
  dependency-update feedback for any ecosystem; a new action major surfaces only
  when a human notices or CI breaks.
- `constraints.txt` and `docs/proposals/core/121-mcp-v2-migration-and-reproducible-resolution.md`
  (step 4) — the repo's existing, enforced version-currency discipline for
  *Python* dependencies; the ownership the CI-action side lacks.
- `docs/proposals/operations/125-ci-off-host-blacksmith-and-deploy-architecture.md`
  (Out of scope — "A general digest-pin pass across all actions") — the adjacent
  CI proposal. It owns `runs-on:` placement and scopes action-reference
  hardening out, leaving the runtime-currency axis this proposal fills; the two
  key on different YAML fields and do not conflict.

## Implementation Plan

Ordered so an interrupted run always leaves CI green. Step 1 is the immediate
remediation the September deadline demands and stands entirely alone; step 2 is
the structural change that owns the fact; step 3 closes the feedback gap; step 5
reconciles the docs; step 4 is optional consolidation. These are CI-config
changes, not loop-runtime behaviour, so there is no `Config` flag to gate them
behind — the rollback lever is per-step `git revert` (see Risk Assessment).

1. [x] **Bring the Node-runtime actions to the Node-24 floor.**
   `.github/workflows/quality-gates.yml`, `.github/workflows/code-review.yml`,
   `.github/workflows/reply-coverage.yml` — bump all six `actions/checkout@v4`
   → `@v5` and all six `actions/setup-python@v5` → `@v6`, whose runtimes are
   Node 24. This is the immediate fix the imminent Node-20 removal requires; it
   changes no job logic and is a one-line-per-occurrence edit.
   - *Depends on:* none.
   - *Acceptance:* no file under `.github/workflows/` pins `actions/checkout`
     below `@v5` or `actions/setup-python` below `@v6`, and a full Quality Gates
     run is green on the bumped pins.

2. [x] **Own the runtime floor with one deterministic enforcement test.** new
   `tests/test_workflow_action_runtime.py` — the structural change. A pure
   resolver (`resolve_action_node_runtime(uses_ref) -> int | None`) maps each
   first-party Node-based action to its runtime through a single reviewed
   `ACTION_NODE_RUNTIME` table (`actions/checkout`: v5→24; `actions/setup-python`:
   v6→24; extend as actions are added), and a walker asserts every mapped `uses:`
   across `.github/workflows/*.yml` **and** `.github/actions/**/action.yml` meets
   one `MIN_NODE_RUNTIME = 24` floor. Container/`docker://` refs and shell-only
   jobs are exempt; an *unmapped* first-party Node action reds, so the table
   cannot silently omit a newly added action. House pattern of
   `tests/test_structural_guards.py` and `tests/test_auto_update_workflow.py`.
   - *Depends on:* 1.
   - *Acceptance:* the test reds when any workflow pins a mapped action below the
     floor, reds when an unmapped first-party Node action appears, and is green
     on the current tree; the Node 24 → 26 wave becomes a one-line
     `MIN_NODE_RUNTIME` bump that names every stale pin.

3. [x] **Close the feedback gap with dependabot for the actions ecosystem.** new
   `.github/dependabot.yml` — a weekly `github-actions` ecosystem entry at the
   repo root, so a new action major (and runner-image update) surfaces as a
   reviewable PR *before* the next deprecation deadline rather than as a broken
   CI run. Independently mergeable.
   - *Depends on:* none.
   - *Acceptance:* `.github/dependabot.yml` parses, declares `version: 2` and a
     `package-ecosystem: "github-actions"` entry, and a shape test asserts both.

4. [x] **(Optional) Single-source the Python setup behind one composite action.**
   new `.github/actions/py-setup/action.yml`,
   `.github/workflows/quality-gates.yml`, `.github/workflows/code-review.yml`,
   `.github/workflows/reply-coverage.yml` — move the repeated
   `actions/setup-python@v6` + `python-version: "3.12"` + install into one
   composite action each job calls after `checkout`, so the `setup-python`
   version and the Python version live in one file instead of six. `checkout`
   cannot be wrapped (a same-repo composite is unavailable until the repo is
   checked out), so it stays per-job under the floor. The floor test already
   scans `.github/actions/**/action.yml` (step 2), so the moved pin stays
   covered.
   - *Depends on:* 2.
   - *Acceptance:* `actions/setup-python` and `python-version` each appear
     exactly once (in the composite); the floor test resolves the composite's
     pin and stays green; CI is green.

5. [x] **Reconcile the docs to the enforced floor.** `docs/ci.md` — replace the
   prose "`@v4`/`@v5` tag-pin discipline" description with a pointer to the
   enforced floor (the discipline is now a test, not an aspiration), and keep the
   deferred **digest-pin** pass explicitly scoped to the immutability axis,
   distinct from the runtime-currency axis this proposal now owns.
   - *Depends on:* 2.
   - *Acceptance:* `docs/ci.md` names `tests/test_workflow_action_runtime.py` as
     the owner of action runtime currency and no longer presents pin currency as
     an unenforced discipline; `tests/test_no_line_citations_in_docs.py` stays
     green.

## Testing Strategy

All tests are deterministic and zero-LLM-cost, honouring CLAUDE.md principles:
no real network, no real `sleep`, no real subprocess, mocked externals. Reading
the repo's own workflow/action YAML is static-tree inspection, not prohibited
I/O — the same read `tests/test_auto_update_workflow.py` and
`tests/test_no_line_citations_in_docs.py` already perform; the guard branches
that must *fail* are exercised against in-memory fixture YAML strings (parsed,
never written to disk), so no `mem_fs` file write is needed.

- `tests/test_workflow_action_runtime.py` (new) — the centre of the remedy.
  Branches: **happy path** — the resolver returns ≥ `MIN_NODE_RUNTIME` for every
  mapped `uses:` in the real tree after step 1 (green). **Guard** — a fixture
  pinning `actions/checkout@v4` resolves below the floor and reds. **Guard** — a
  fixture with an unmapped first-party Node action (e.g.
  `actions/upload-artifact@v4`) reds, forcing table completeness. **Exemption** —
  a `docker://…` container `uses:` and a shell-only job resolve to `None` and are
  skipped, not failed. **Forward-cover** — a fixture `.github/actions/**/action.yml`
  composite pin is resolved (covers step 4). The resolver is a pure function so
  each branch is asserted in isolation with a simple `assert x == y`.
- `tests/test_dependabot_config.py` (new, or folded into the above) — parse
  `.github/dependabot.yml` with `yaml.safe_load`; assert `version == 2` and that
  a `github-actions` ecosystem entry is present. House pattern of pinning config
  shape; a fixture with a missing/renamed ecosystem reds.
- `tests/test_no_line_citations_in_docs.py` — must stay green with the new
  proposal file present (run before and after authoring).

## Risk Assessment

**The action→runtime table can drift.** If a future action major changes its
runtime and the in-test `ACTION_NODE_RUNTIME` table is not updated, the floor
could admit a stale pin or red a good one. Mitigation: the "unmapped first-party
Node action reds" branch forces the table to stay complete as actions are added;
step 3's dependabot surfaces each new major as a PR where updating the table is
the obvious same-PR edit; the table covers a handful of first-party actions and
is human-reviewed — the same "read at decision time" posture proposal 125 takes
for its price table.

**Coverage is only as wide as the table.** The floor covers first-party Node
actions in the table; a third-party Node action added later without an entry
must **red** (the unmapped-reds branch), never silently pass. This is called out
and tested, so an incomplete table is loud, not quiet.

**Bumping majors can change action behaviour.** `checkout@v5` and
`setup-python@v6` drop older Node and may change defaults. Mitigation: step 1 is
validated by a full green CI run before merge; each bump is an independent
one-line revert; both are GitHub-maintained first-party actions with documented
migration notes.

**Rollback posture.** These are CI-configuration changes, not loop-runtime
behaviour, so there is no `[loop]`/`[audit]` `Config` flag to gate them behind —
the honest analog is per-step `git revert`, and every step is independently
revertible (revert a workflow edit; delete the test; delete `dependabot.yml`).
The one flag-like lever is the floor test's `MIN_NODE_RUNTIME` constant;
lowering it re-permits an older runtime, but that is discouraged and plainly
visible in the diff rather than a silent bypass. An interrupted rollout at any
step leaves a green, working CI.

**Interaction with proposal 125.** 125 may relabel `runs-on:` and later
digest-pin `auto-update.yml`. The floor test keys on `uses:` action versions,
orthogonal to `runs-on:`, so the two never contend for the same field; if 125
relabels a workflow, the floor test is untouched, and dependabot's
`github-actions` ecosystem complements 125 by surfacing runner-image and
container-action updates without owning its placement decisions.

## Out of scope

- **Digest-pinning actions (`@sha256:…`).** The *immutability* axis of action
  references — a supply-chain concern distinct from *runtime currency* — stays
  deferred (`docs/ci.md`, proposal 125 Out of scope). This proposal owns
  currency only; the two axes are complementary but separate.
- **Runner-image currency** (`ubuntu-2404` → successor). Owned by proposal 125's
  `runs-on:` machinery and placement manifest. Dependabot's `github-actions`
  ecosystem surfaces image updates as PRs, but the placement decision is 125's.
- **The `auto-update.yml` container action's mutable `:v1` tag.** A container
  action, not a Node action; its hardening is proposal 125 step 13's territory.
- **Python-version currency (`"3.12"`).** Python 3.12 is current and supported;
  this is a *duplication* (six copies) not a *currency* gap. Optional step 4
  single-sources it as a side effect; absent step 4 it remains a documented
  duplication, not a defect.
- **Non-CI Node usage.** The repo ships no Node application; the concern is
  strictly the runtime of the CI actions.

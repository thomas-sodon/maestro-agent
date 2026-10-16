# 080: Stabilize PR Tending — One Merge-Truth Oracle, a Conflict-Aware Merge-Block Gate, and a Terminal Per-PR Handoff

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/escalation_bridge.py` (`detect_unlanded_cure_handoff`, `deliver_handoff`), `maestro/remediation_tracker.py` (`measure_unlanded_remediations`), `maestro/agent.py` (`_probe_pr_merge_block`, `_pr_merged_in_git`, `_record_remediation_outcome`), `maestro/loop/pr_remediation.py` (`gate_merge_block`, `gate_branch_resolved`, `dequeue_gates`; reached via the `MaestroAgent._gate_pr_remediation_*` shims), `maestro/audit_checks.py` (`_git_pr_merged_default`, `format_known_state_summary`), `maestro.toml` (`[loop]`), tests |
| **Depends on** | _(composes with 070)_ |
| **What landed** | Step 1 (Phase 1) — the one host-API-first merge-truth oracle. `MaestroAgent._host_or_git_pr_merged` (`maestro/agent.py`) consults the code-host API first and falls back to the `_git_pr_merged` git-log oracle only when the host call raises — reading the **fail-loud** `code_host.is_pr_merged_strict`, since the best-effort `is_pr_merged` this step originally wired swallows `VCSError` into `False` and so left that fallback unreachable until #2954 corrected it; `_pr_merged_in_git` (the dequeue merged-exclusion gate) delegates to it, and the unlanded-remediation sensor (`format_unlanded_remediations` → `measure_unlanded_remediations`) and the escalation bridge (`detect_unlanded_cure_handoff`) are routed through it at their call sites (`maestro/agent.py`, `maestro/loop/task_generation.py`, `maestro/task_gates.py`) — converging the subsystem on the host oracle and removing the #2579 name-drop false-positive. Regression tests in `tests/test_agent.py` assert the host verdict wins over a `(#N)` git name-drop and that the git oracle is consulted only when the host call raises (PR #2639). Step 2 (Phase 2) — conflict-aware merge-block classification via an in-memory `git merge-tree` probe (`VCS.merge_tree_conflicts`) in `_probe_pr_merge_block` behind `[loop] merge_block_local_probe_enabled` (default on); a `BEHIND_MAIN` verdict that survives host-field refinement is reclassified `CONFLICTING` when the local probe detects a conflict, routing it to LLM remediation rather than skipping as Tend-clearable. Tested in `tests/test_agent_lifecycle.py` and `tests/test_vcs.py` (PR #2653). Step 3 (Phase 3) — the per-PR lap budget and durable terminal standdown. `[loop] pr_remediation_lap_budget` (default 6; `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`); a `pr_number`-keyed standdown ledger in `maestro/escalation_bridge.py` (`_load_standdowns` / `_write_standdowns` / `has_standdown` / `clear_standdown` / `prune_resolved_standdowns`, persisted to `escalation_handoffs.json` with a pre-080 `(pr, head_sha)`-key migration path) that re-keys `deliver_handoff` and `detect_unlanded_cure_handoff` from `(pr_number, head_sha)` to `pr_number`, so a fresh head no longer re-knocks; minting is suppressed past the budget in both the Tend route (`maestro/loop/pr_remediation.py` `PrRemediationController.route_feedback`) and the registered `_escalation_bridge_run` task gate (`maestro/task_gates.py`), each delivering exactly one merge-or-close handoff and clearing the standdown on a host-API merged/closed verdict. Tests in `tests/test_pr_lap_budget.py`. Step 4 (Phase 3) — lap-count and standdown observability: a `maestro pr-laps` read-only CLI (`maestro/cli.py` `cmd_pr_laps`) and a `PR-LAPS` line folded into `format_known_state_summary` (`maestro/audit_checks.py`, via the `pr_lap_context` parameter) populated by `format_pr_lap_summary` (`maestro/remediation_tracker.py`) over the host-first lap counts and active standdowns (`active_standdown_prs`, `maestro/escalation_bridge.py`), wired at the `agent.py` known-state call site, so a PR at ≥ budget/2 laps surfaces in contemplation and on the CLI before a pileup. Tests in `tests/test_cli.py` (PR #2675). Step 5 (Phase 4) — the unified dequeue-face disposition classifier: `classify_pr_remediation_disposition` in the new `maestro/pr_remediation_state.py` maps each of the eight `PrRemediationState` members to one of four `PrRemediationDisposition` actions (`RUN` / `TEND_WILL_CLEAR` / `HANDOFF` / `EVICT_MERGED`) through the total `_STATE_TO_DISPOSITION` table, and `PrRemediationController.dequeue_gates` (`maestro/loop/pr_remediation.py`) derives its run-vs-skip boolean from it as the single, unconditional decision site, evicting exactly when the state is not `REMEDIABLE` since `REMEDIABLE` is the sole `RUN` state. The flag-gated rollout (`[loop] unified_remediation_gate_enabled`, default off) and the legacy `state is not REMEDIABLE` comparison it proved parity against have since been retired from `maestro/config.py`, `maestro/toml_config.py`, and `maestro.toml`, leaving the classifier as the sole decision site. Routing test `test_evicts_exactly_when_not_remediable` in `tests/test_agent_pr_gates.py`. |
| **What is missing** | _(nothing — Step 5 / Phase 4 landed, completing all five steps)._ |
| **Derived from** | Live-data review of `data/task_history.jsonl`, `data/messages.jsonl`, `data/remediation_tracker.json`, `data/pr_ledger.json`, `data/version_archive.jsonl` on 2026-06-13 |

## Description

> Status updated to shipped on 2026-06-20 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

PR tending is the loop's least stable subsystem. Of 614 tasks in the live
`task_history.jsonl`, **290 are PR-remediation laps** — and a single PR, #2579,
accounts for **44 laps, every one `partial`, none of which landed anything**.
Its branch has been orphaned since cycle 2320 (the loop is now near 2378) and
the PR is still open. Three other PRs (#2348, #2359, #2376) each absorbed
32–36 laps before they merged, and #2362 took 21. The tending machinery is not
*failing loudly* — it is spinning quietly, re-minting work that cannot succeed.

The instability has three concrete, separately-fixable causes, each visible in
the data. **First, the loop runs two different "is this PR merged?" oracles
that disagree.** The merge-block gate (`_probe_pr_merge_block`) and Tend ask
the authoritative host API (`is_pr_merged`); the dequeue-face merged-exclusion
gate (`_pr_merged_in_git`), the unlanded-remediation sensor, and the escalation
bridge all ask a git-log substring oracle (`_git_pr_merged_default`).
On 2026-06-13 Maestro itself root-caused why the no-escalation bridge never
fired for #2579: the substring oracle returned `merged=True` because an
*unrelated* merge commit's message name-dropped "#2579", so the sensor filtered
the PR out of its own input and the handoff was never delivered. The bridge
built to hand off #2579 erased #2579 from its input by mentioning it.

**Second, the merge-block gate skips remediation on a false premise.** It
treats a `BEHIND_MAIN` verdict as "Tend will clear this, not the LLM" and
records a zero-cost `partial`. But Tend can only fast-forward `origin/main` into
a branch that merges *cleanly*; when the branch conflicts with a fast-moving
main, `BEHIND_MAIN` is **not** Tend-clearable — only the LLM can resolve it. The
gate reads GitHub's asynchronously-computed `mergeable`/`merge_state_status`,
which flaps (Maestro logged #2579 as `MERGEABLE, 0 behind / 4 ahead` at 09:10
while `gh` reported `DIRTY`), so the same PR oscillates between `[merge-block]`
and `[branch-resolved]` skips and never gets a real attempt.

**Third, there is no terminal state.** The branch-resolved gate "escalates" via
a best-effort notification, and the durable handoff (escalation bridge) is keyed
on `(pr_number, head_sha)`. Each time the LLM re-resolves the conflict the head
changes, so the standdown never sticks — and when the oracle bug hides the PR,
no handoff fires at all. The end state this proposal delivers: **one merge-truth
oracle for the whole subsystem, a merge-block gate that probes conflicts locally
instead of trusting flapping host fields, and a per-PR lap budget that delivers
exactly one handoff and then stops minting laps until the PR merges, closes, or
the operator clears it.**

## Relevant Findings

- `maestro/audit_checks.py` (`_git_pr_merged_default`) — decides "merged" by
  `git log origin/main --grep '#<pr>'` then a subject-only discriminator
  (`Merge pull request #N`, `(#N)$`). PR #2623 narrowed it from full-message to
  subject-only, but the approach is still structurally unreliable: it
  **false-positives** on a squash/revert subject that ends `(#N)` for an
  unrelated reason, and **false-negatives** on rebase-merges (whose commits keep
  their original subjects and carry no PR number at all).
- `maestro/vcs.py` (`is_pr_merged`) — the authoritative host-API oracle. The
  merge-block gate (`maestro/agent.py` `_probe_pr_merge_block`) and Tend
  (`maestro/evolution.py` `tend_evolution_prs`) already consult it; the dequeue
  merged-exclusion gate (`maestro/agent.py` `_pr_merged_in_git`, via the
  `_git_pr_merged` wrapper), the sensor, and the bridge do not — they read the
  git oracle, deliberately aligned with the sensors by #2523.
- `maestro/remediation_tracker.py` (`measure_unlanded_remediations`) and
  `maestro/escalation_bridge.py` (`detect_unlanded_cure_handoff`) — both default
  their `is_pr_merged` parameter to `_git_pr_merged_default`, i.e. the fragile
  oracle, while the merge-block gate that *routes* remediation uses the host API.
  The two disagreeing oracles are the proximate cause of #2579's 44-lap loop with
  no handoff.
- `maestro/agent.py` (`_probe_pr_merge_block`) — refines a verdict from
  `mergeable` / `merge_state_status` / `check_conclusion`, but those are
  GitHub-computed and lag; it never performs a deterministic local
  `git merge --no-commit origin/main` to decide whether `BEHIND_MAIN` is
  actually Tend-clearable. Live `task_history.jsonl` shows #2579 carrying both
  `[merge-block] … BEHIND_MAIN` and `[branch-resolved]` stamps — verdict flap.
- `maestro/loop/pr_remediation.py` (`gate_merge_block`, reached via the
  `MaestroAgent._gate_pr_remediation_merge_block` shim) — fires
  for `MERGE_BLOCK_BEHIND_MAIN` / `MERGE_BLOCK_AWAITING_PUSH` and records
  `partial`, never `success`; it removes the entry but does not stop Tend from
  re-adding the PR next cycle, so the lap repeats indefinitely.
- `maestro/escalation_bridge.py` (`HANDOFF_STAMP_THRESHOLD`, `deliver_handoff`)
  — idempotency keyed on `(pr_number, head_sha)`; a new head on a still-unmerged
  PR earns a fresh knock, so re-resolving a conflict resets the standdown rather
  than honoring it.
- `data/version_archive.jsonl` — **23 distinct PR-tending/remediation
  mechanisms shipped between 2026-05-13 and 2026-06-12** (merge-block gate,
  costume-signature gate, repetition gate, preflight gate, forced-read bridge,
  escalation bridge, no-escalation bridge, phantom-remediation rule 10,
  phantom-escalation rule 12, orphaned-cure rule 11, two readmission sensors,
  …). The proliferation is itself a stability signal: narrow gates sharing
  fragile primitives, several of which (#2579, #2622) are themselves stuck PRs.
- `docs/proposals/core/070-pr-remediation-explicit-state-machine.md` — proposes
  naming the gate states and making ordering data-driven, but explicitly assumes
  "the sequencing is correct today." This proposal fixes the *behavioral* bugs;
  070 is the *structural* complement and step 4 here composes with it.

## Implementation Plan

### Phase 1 — Trust one merge-truth oracle

1. [x] **Route the dequeue merged-exclusion gate, the unlanded-remediation
   sensor, and the escalation bridge through the host-API oracle.**
   `maestro/agent.py`, `maestro/escalation_bridge.py`,
   `maestro/remediation_tracker.py` — at the call sites that build
   `detect_unlanded_cure_handoff` / `measure_unlanded_remediations`, pass the
   agent's `code_host.is_pr_merged` as the `is_pr_merged` argument, falling back
   to `_git_pr_merged_default` only when the host call raises; both functions
   already take an injectable `is_pr_merged` seam, so only the default at the
   call site changes. Then route the dequeue merged-exclusion gate the same way:
   have `_pr_merged_in_git` (today a thin wrapper over `_git_pr_merged`) consult
   `code_host.is_pr_merged` first, falling back to the git oracle only when the
   host call raises. This converges the sensor, bridge, and dequeue gate on the
   host API — the oracle the merge-block gate and Tend already trust, delivering
   the "one merge-truth oracle for the whole subsystem" end-state — and removes
   the name-drop false-positive that blinded the bridge for #2579.
   - *Depends on:* none.
   - *Acceptance:* with a stub host API returning `False` for #N and a
     planted `origin/main` commit whose subject ends `(#N)`,
     `measure_unlanded_remediations` still counts #N as unlanded and
     `_pr_merged_in_git` still treats #N as not-merged (host API wins); a
     regression test asserts the git oracle is consulted only when the host call
     raises.

### Phase 2 — Conflict-aware merge-block classification

2. [x] **Decide Tend-clearability by a local probe-merge, not flapping host
   fields.** `maestro/agent.py` (`_probe_pr_merge_block`) — when the verdict
   resolves to `BEHIND_MAIN`, run an in-memory `git merge-tree` probe of the
   PR branch against `origin/main` (`VCS.merge_tree_conflicts`); if it
   conflicts, classify `CONFLICTING` (LLM-clearable) instead of `BEHIND_MAIN`.
   The probe writes only to the object DB and never touches the working tree,
   index, or HEAD, so there is no probe merge to abort. This makes the gate
   deterministic and immune to GitHub's `mergeable=UNKNOWN` recomputation lag.
   Guard behind `[loop] merge_block_local_probe_enabled` (default on; flag lets
   it be disabled if the extra git op is unwanted).
   - *Depends on:* none.
   - *Acceptance:* a branch that is behind `origin/main` **and** conflicts on
     local merge is classified `CONFLICTING` and routed to remediation, never
     stamped `[merge-block]`; a clean-but-behind branch is still `BEHIND_MAIN`.

### Phase 3 — A terminal per-PR handoff that sticks

3. [x] **Count laps per PR across all heads and add a durable standdown.**
   `maestro/loop/pr_remediation.py`, `maestro/escalation_bridge.py`,
   `maestro/remediation_tracker.py` — add `[loop] pr_remediation_lap_budget`
   (default ~6). When a PR's cumulative laps that landed nothing (per the host
   API) exceed the budget, write a `pr_number`-keyed standdown record, deliver
   exactly one handoff message, and suppress further minting for that PR.
   Re-knock only when the host API shows the PR is still open *and* its head
   changed *and* no standdown is recorded; clear the standdown when the host API
   reports merged/closed. Replace the `(pr_number, head_sha)` idempotency in
   `deliver_handoff` with `pr_number` keying plus an informational head field.
   - *Depends on:* 1.
   - *Acceptance:* a PR driven past `pr_remediation_lap_budget` receives exactly
     one handoff and zero further minted laps across ≥3 simulated head changes,
     until the host API reports it merged/closed.

4. [x] **Surface lap count and standdown state before a thrash reaches 44.**
   `maestro/remediation_tracker.py`, `maestro/audit_checks.py`
   (`format_known_state_summary`), `maestro/agent.py` (the call site that
   assembles the summary's inputs), `maestro/cli.py` — fold per-PR lap counts and
   any active standdowns into the known-state context block and expose a
   `maestro pr-laps` read-only subcommand. Makes a pileup observable in
   contemplation instead of only post-hoc in `task_history.jsonl`.
   - *Depends on:* 3.
   - *Acceptance:* a PR at ≥ budget/2 laps appears in
     `format_known_state_summary` output and in `maestro pr-laps`.

### Phase 4 — Consolidate the dequeue gates (composes with 070)

5. [x] **Fold the dequeue-face gates into one disposition classifier behind a
   flag.** `maestro/loop/pr_remediation.py` (`dequeue_gates`), new
   `maestro/pr_remediation_state.py` (shared with 070) — introduce
   `classify_pr_remediation_disposition()` returning one of
   `{RUN, TEND_WILL_CLEAR, HANDOFF, EVICT_MERGED}` and route `dequeue_gates`
   through it behind `[loop] unified_remediation_gate_enabled` (default off).
   This shrinks the surface area that keeps generating buggy gate interactions
   and gives 070's state enum a single decision site.
   - *Depends on:* 1, 2, 3.
   - *Acceptance:* with the flag on, a replay of recorded `pending_pr_requests`
     produces byte-identical evictions and outcome stamps to the legacy
     four-gate chain, proving parity before the old gates are retired.

## Testing Strategy

- **Phase 1** — `tests/test_escalation_bridge.py` / `tests/test_remediation_tracker.py`:
  inject a stub `code_host.is_pr_merged` and a planted git history; assert the
  host verdict wins over a `(#N)` subject name-drop (the #2579 reproduction), and
  that `_git_pr_merged_default` is invoked only on a raising host call (fallback
  branch). Use `mem_fs`; no real git or network.
- **Phase 2** — `tests/test_agent_lifecycle.py` and `tests/test_vcs.py`: mock the
  `VCS.merge_tree_conflicts` seam so the in-memory `git merge-tree` probe reports
  conflict vs. clean; assert `CONFLICTING` vs. `BEHIND_MAIN` classification. The
  object-DB probe touches no working tree, so there is no probe merge to abort.
  Cover the flag-off branch.
- **Phase 3** — new `tests/test_pr_lap_budget.py`: drive a PR past the budget
  across simulated head changes; assert exactly one handoff message is appended
  to `messages.jsonl` (`mem_fs`), the standdown suppresses minting, and a
  host-API merged/closed verdict clears it. Cover the re-knock guard.
- **Phase 4** — `tests/test_agent_pr_gates.py`: a parity harness replays a
  fixture of `pending_pr_requests` through both the legacy chain and
  `classify_pr_remediation_disposition`, asserting identical dispositions.
- All new config flags get a `tests/test_config.py` round-trip and a default
  assertion.

## Risk Assessment

- **Phase 1** carries the most behavioral weight but the least risk: it makes
  two sensors agree with the gate that already drives routing, and degrades to
  today's behavior when the host API is unreachable. Rollback is reverting the
  call-site default.
- **Phase 2** adds an in-memory `git merge-tree` probe (`VCS.merge_tree_conflicts`)
  per dequeue; it writes only to the object DB and never touches the working tree,
  index, or HEAD, so there is no abort and no dirty-worktree risk. It is flag-gated
  (`merge_block_local_probe_enabled`); rollback is disabling the flag.
- **Phase 3** changes a standdown key from `(pr, head_sha)` to `pr`. The
  migration concern is in-flight `escalation_handoffs.json` records keyed the old
  way; the loader should treat any existing `(pr, *)` entry as a delivered
  standdown for that `pr` so no duplicate knock fires on upgrade. The danger to
  guard against is *under*-escalating a genuinely new problem on the same PR —
  mitigated by clearing the standdown on a host-API merged/closed transition and
  on operator action.
- **Phase 4** is a pure refactor gated off by default; the parity test is the
  control that proves it before the legacy gates are deleted. It must not land
  before 070's enum if both are in flight — coordinate ordering via the shared
  `maestro/pr_remediation_state.py` module.
- **Trust chain:** none of these steps touch approvals, the action gate, or the
  sole-pusher invariant — Tend remains the only pusher and all code changes
  still require human merge. The handoff in Phase 3 is a *message*, not an
  auto-merge.

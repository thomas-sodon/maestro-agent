# 093: Own-PR Self-Delivery — One Owned-Writable-Branch Oracle So the Agent Lands Its Own PR Fixes

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/tool_gating.py`, `maestro/providers/cli_paced_gate.py`, `maestro/providers/cli_paced.py`, `maestro/agent.py`, `maestro/executor.py`, `maestro/loop/pr_remediation.py`, `maestro/pr_ledger.py`, new `maestro/owned_branches.py`, new `tests/test_owned_branch_oracle.py`, tests |
| **What landed** | All seven steps. Owned-PR self-delivery is now **unconditional** — the `owned_pr_branch_writes_enabled` flag has been retired entirely (`maestro/config.py` / `maestro/toml_config.py` / `maestro.toml`), so steps 1–5's behaviour always runs and the step-6 AST guard is always on. Step 7's `_delivery_note` push rationale and `run()` §5-push framing moles are also retired, and the write-time resolution-keyword scrubber (`neutralize_unlanded_resolution_narration`, `maestro/outcome_grounding.py`) now narrows to the honest-terminal case behind opt-in `[loop] narration_scrub_ready_only_enabled` (default-off, wired at `run()` §5c-v), and the phantom-resolution audit rule (rule 13, `check_phantom_resolution`, `maestro/audit_checks.py`) likewise narrows behind opt-in `[audit] phantom_resolution_ready_only_enabled` (default-off, wired at `_audit_self_model` through the memoized `observe_pr_readiness` chokepoint): a cited PR observed `ready_and_waiting` stops tripping the alarm, while an owned-blocker PR — or one absent from the open-owned snapshot — still fires. The narration-verdict-mismatch audit rule (rule 15, `check_narration_verdict_mismatch`, `maestro/audit_checks.py`) likewise narrows behind opt-in `[audit] narration_verdict_mismatch_ready_only_enabled` (default-off, wired at `_audit_self_model` through the memoized `observe_pr_readiness` chokepoint via the shared `_pr_ready_and_waiting` predicate): a record citing only `ready_and_waiting` PRs stops tripping the alarm, while an owned-blocker or PR-less record still fires. The phantom-remediation audit rule (rule 10, `check_phantom_remediation`, `maestro/audit_checks.py`) likewise narrows behind opt-in `[audit] phantom_remediation_ready_only_enabled` (default-off, wired at `_audit_self_model`): a delivered-awaiting-merge (`Marker.DELIVERED_AWAITING_MERGE`) record stops tripping the alarm, while an awaiting-tend-push or bare not-yet-landed record still fires — closing step 7's last mole, so all seven steps have landed. Step 1 — the owned-writable-branch oracle: `maestro/owned_branches.py` with pure `compute_owned_writable_branches(current_branch, open_pr_branches, pr_ledger_branches)` returning the provenance-stamped `frozenset[str]` (own `maestro/evolve/*` heads confirmed by *both* the live open-PR set and the ledger, `main`/`master` excluded), covered by `tests/test_owned_branch_oracle.py`. Step 2 — the multi-branch action gate: `evaluate_bash_command`, `_git_deny`, and `TaskGateHook` (`maestro/tool_gating.py`) take `allowed_branches: frozenset[str] \| None`, permit a push only when every destination ref is in the set (still denying `main`/`master`, still fail-closed on an unparseable/refspec-less push), and **add** force-push denial (`_is_force_push` / `_FORCE_PUSH_DENY`); `make_task_pretooluse_hook` / `make_task_tool_hooks` carry the set. Step 3 — cross-process replication: `GATE_BRANCH_ENV` carries a JSON-encoded branch list, `allowed_branches_from_env` returns the `frozenset`, and `build_gate_settings` / `serialize_gate_settings` / `task_gate_from_hooks` round-trip the set (`maestro/providers/cli_paced_gate.py`, `maestro/providers/cli_paced.py`). Step 4 — the owned-set injection: `MaestroAgent._gate_allowed_branches` (`maestro/agent.py`) computes the owned set once per run unconditionally (degrading to the singleton `{branch}` on a host/ledger read failure) and threads it through `maestro/executor.py` to `make_task_tool_hooks`. Step 5 — the delivery note: `_delivery_note()` (`maestro/loop/pr_remediation.py`) unconditionally instructs the agent to push its own fix and drops the "delivered automatically" claim, leaving `_REPLY_DELIVERY_NOTE` / the reply manifest untouched; `run()` §5-push stays the idempotent backstop. Step 6 — the one-oracle enforcement test: `tests/test_owned_branch_oracle.py` now carries the AST guard `test_push_permission_computed_only_at_the_oracle_door` (a whole-`maestro/` scan pinning `compute_owned_writable_branches` / `PRLedger.known_branches` to the single injection point `MaestroAgent._gate_allowed_branches`) plus `test_gate_module_never_re_derives_ownership`, the analogue of `tests/test_merge_truth_chokepoint.py`. |
| **What is missing** | Nothing — step 7's final mole has landed: rule 10 (`check_phantom_remediation`, `maestro/audit_checks.py`) now narrows behind opt-in `[audit] phantom_remediation_ready_only_enabled` (default-off), matching the rule-13/15 pattern, so all three phantom rules and the write-time scrubber are scoped to their external-blocker cases. All seven implementation steps are complete. |
| **Depends on** | 087 (shipped), 091 (shipped) |
| **Derived from** | live production data via /propose, 2026-07-12 |

## Description

> Status updated to shipped on 2026-07-17 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Maestro cannot land a fix on one of its own open evolution PRs unless the loop happens to reach that PR through one narrow mechanical channel. Its delivery capability is bound to **control-flow** — *which branch the run was launched on* and *whether the run is the `pr_branch` remediation channel* — rather than to **provenance** — *is this Maestro's own PR, which a human still gates at merge?* Two independent proxies encode that same fact and both key on control-flow: the task-executor action gate keys on a single `allowed_branch` string (`maestro/tool_gating.py`), and the only code that actually pushes a resolution keys on `branch_name == pr_branch` inside `run()` (`maestro/agent.py`). When the agent reaches PR-fixing work by any other route — a general self-directed task, a plural or unnumbered human request to fix the problems on its open PRs, a conflict resolution, a *"bring it up to date with main"* — the run lands on a `maestro/task/<id>` branch, the gate denies the agent's push to the `maestro/evolve/*` PR branch, no loop push fires, and the completed, gates-green fix strands in a local worktree while the agent narrates a success it never delivered.

This matters now because the gap is actively costing operator trust and dominating the loop's own self-audit. In the current hot window Maestro told the operator, over and over on one day, that its resolved merges were *"committed locally — but the final push is blocked by the task-executor action gate"*; the operator replied by telling it to fix its own PRs, and the task still closed `partial`. Behind that, roughly a dozen shipped proposals and forty-plus commits — merge-truth oracles, five phantom-narration audit rules, write-time keyword scrubbing, keyed and mechanical stand-downs, durable escalation handoffs — exist almost entirely to *detect and suppress the phantom "success"* that the delivery gap manufactures. That is the signature of a whack-a-mole class: every fix polices a new shape of the same narration↔delivery mismatch, and none makes the agent able to simply deliver its own PR fix.

The change makes own-PR branch-delivery a first-class **capability of the agent**, decoupled from the channel it arrived by, behind one provenance-stamped **owned-writable-branch oracle** (the 087 pattern applied to write-permission instead of merge-truth). The oracle answers one question — *may the task executor write to branch X?* — as: X is the run's own branch or the `maestro/evolve/*` head of one of Maestro's own open evolution PRs, and X is not `main`/`master`. Both enforcement paths — the action gate and the loop's delivery step — read that one fact, an AST test guarantees it is the only door, and the agent is told to push its own resolution. The end state: whichever way the agent reaches its own PR, it commits, merges `origin/main` in, and pushes the branch itself; the loop's push becomes an idempotent backstop; and the phantom-narration machinery fires for genuinely external blockers (CI red, awaiting human merge) instead of for work the agent could have delivered but structurally could not.

The human-in-the-loop invariant is untouched. Pushing to an own evolution-PR branch does not land code on `main` — the PR still needs human approval and a human merge — and the module already documents that own-branch push "does not bypass the approval gate." Merging a PR to `main` stays denied (and there is no merge-to-`main` code path in the repository at all); review-thread reply delivery stays loop-owned to preserve its no-double-post guarantee. What widens is exactly what the concern names: the agent's ability to make as many updates to its own PR branches as it needs to keep them approvable and current with `main`.

## Relevant Findings

- `maestro/tool_gating.py` (`_git_deny`, `evaluate_bash_command`, `TaskGateHook`) — the #2280 action gate scopes each run to a single `allowed_branch: str | None`: a `git push` is allowed only when every destination ref equals that one branch, `main`/`master` are always denied, and an unparseable or refspec-less push fails closed. It does **not** hard-block a PR branch; it blocks a push to *any branch other than the one the run was launched on*.
- `maestro/agent.py` (`run()` §5-push) — the only code that pushes a resolution to `origin/<pr_branch>` fires solely under `pr_branch is not None and branch_name == pr_branch and outcome in ("success", "partial")`. Off that narrow remediation channel (general task, `pr_branch is None`, or a branch mismatch) no push is attempted and the agent's own push is gate-denied, so the fix cannot reach origin.
- `maestro/loop/pr_remediation.py` (`_delivery_note`) — every remediation prompt tells the LLM *"Do NOT push — your commit is delivered to the PR automatically once the task completes."* The comment above it concedes the rationale is "now false"; the agent internalizes "delivery is automatic," then discovers off-channel that it is not.
- `maestro/loop/pr_remediation.py` (`route_user_pr_message`, matching via `maestro/agent.py`'s shared `_PR_REFERENCE_PATTERN`) — proposal 091 routes a human message to the on-channel remediation path only when it names exactly one `#<n>`; a plural or unnumbered demand (a request to fix all open PRs) matches nothing, returns `None`, and falls through to a generic task on a `maestro/task/<id>` branch — the exact incident in production.
- `maestro/pr_ledger.py` (`PRLedger`, `observe_head`) — a provenance store already maps each own open PR number to its `maestro/evolve/*` branch and observed head; combined with `get_open_evolution_pr_states("maestro/evolve/")` (`maestro/vcs.py`, `CodeHost`; called from `maestro/evolution.py`) this is the raw material for an owned-branch oracle, currently unused by the gate.
- `maestro/providers/cli_paced_gate.py` (`build_gate_settings`, `serialize_gate_settings`, `allowed_branches_from_env`) — the cross-process replication of the gate (proposal 066) carries the single branch as one `GATE_BRANCH_ENV` string; widening the gate to a set is a bounded extension of this serialization seam, preserving "one decision core, two enforcement paths."
- `maestro/agent.py` (`is_pr_settled`) and `tests/test_merge_truth_chokepoint.py` — proposal 087's house pattern: one provenance-stamped fact behind one chokepoint, AST-enforced as the only door. This proposal reuses that shape for write-permission.
- `maestro/outcome_grounding.py` (`neutralize_unlanded_resolution_narration`, `ground_landing_claim`) and `maestro/audit_checks.py` (`check_phantom_remediation`, `check_phantom_resolution`, `check_narration_verdict_mismatch`) — the detect-and-suppress edifice whose most common trigger is the self-delivery gap; the design lowers their firing rate rather than deleting them (they stay load-bearing for external blockers).
- `maestro/loop/pr_remediation.py` (`_REPLY_DELIVERY_NOTE`, `_REPLY_MANIFEST_FILENAME`) — review-thread replies are delivered by the loop from a recorded manifest specifically because a reply is **not** idempotent (a second POST double-posts the thread). A `git push` has no such hazard, which is why this proposal frees the push but leaves reply delivery loop-owned.

## Production Evidence

Live data via the host bind-mount of `maestro-loop` at `/opt/maestro/data` (strictly read-only). Windows stated per figure.

**Remediation never lands (task_history.jsonl, hot window 89 records, 2026-06-24 → 2026-07-11).** 28 of the last 89 tasks are `Remediate PR #N` laps, spread across 14 PR lineages (#2809 ran 3 laps; #2808/2811/2812/2816/2819/2830/2835/2844/2857/2859/2860/2884 ran 2). **All 28 closed `partial`; zero `success`.**

**The gate denial names the root itself (logs/maestro.log, 2026-07-11 22:49–23:33).** The gate fired 8 times that evening, every one a push to a `maestro/evolve/*` PR branch (or a `gh pr merge`) from a `maestro/task/*` run branch — the run's `allowed_branch` is the task branch, not the PR branch it is fixing:

```
22:49:21 tool_gating denied (branch=maestro/task/05cdeda8e744):
         git push origin HEAD:maestro/evolve/332126720e9a
22:51:19 tool_gating denied (branch=maestro/task/05cdeda8e744):
         git push origin HEAD:maestro/evolve/d5bcc3fc2a33
22:58:52 tool_gating denied ... gh pr merge 2986 --merge
23:30:07 tool_gating denied (branch=maestro/task/d107350483e8):
         git push origin maestro/evolve/d5bcc3fc2a33
```

Maestro's own journal at 22:52 names the same fact: *"blocked by tool_gating: this run's `allowed_branch` is `maestro/task/05cdeda8e744`, not the PR branch."* This is the control-flow-vs-provenance root, in the log.

**Both faces of the phantom, on one PR, 37 minutes apart (task_history.jsonl, 2026-07-11).** A task at 23:02 (`partial`) narrated the fix committed and pushed to #2986; the next task at 23:39 — stamped `success` — confessed the push was *"blocked by the by-design action-gate (#2280) … the fix is trapped in a local worktree … requires Thomas."*

**The treadmill (pending_pr_provenance.jsonl, 66 admissions across 20 distinct PRs).** The same PRs were re-admitted to the tend channel without ever landing: #2808 nine times, #2812 and #2844 six times each, #2811 five times.

**The agent says it plainly (messages.jsonl, 315 messages, 2026-06-24 → 2026-07-11; 49 match push/gate terms). The decisive sequence, one day, redacted for length:**

```
2026-07-11T22:50  PR #2987 (maestro/evolve/332126720e9a) — merge of origin/main
                  is resolved and committed locally, but I CANNOT push it.
2026-07-11T22:52  PR #2986 … merge of origin/main is resolved, quality-verified,
                  and committed locally — but the final push is blocked by the
                  task-executor action gate.
2026-07-11T22:59  The action gate (#2280) is gating me — not a conflict. Both
                  #2986 and #2987 are RESOLVED locally and ready to land …
2026-07-11T23:31  merge with origin/main resolved and committed locally at
                  95a61d31 … all gates green (ruff/mypy/pytest 9743 passed,
                  100% cov). But: `git push` [blocked] …
```

**The operator has asked repeatedly (messages.jsonl + task goals, same window, paraphrased):** fix your own PRs (→ `partial`); write up issues for rules that would get the PRs across the finish line; fix the problems on the open PRs (→ `partial`). In early July the operator asked directly why Maestro waited for the Tend push instead of pushing itself — and Maestro answered, *"You're right — there's nothing to await … I stop at narration instead of acting."* The agent has also handed the terminal act back to Thomas explicitly: *"I cannot land or retire it from the task executor … either push my resolution … or close it"* (2026-06-28, #2812), and *"the resolution is a handoff of air"* (2026-07-11, #2986).

**The loop's own self-audit is dominated by the gap (audit_history.jsonl, 138 cycles, 2026-06-24 → 2026-07-11).** Rules fired, by number of cycles: `phantom_remediation` **98**, `phantom_resolution` **65**, `narration_verdict_mismatch` **55**, `orphaned_cure` **42** — the phantom-narration rules fire in the *majority* of audit cycles. Finding kinds totalled: `phantom_remediation` 183, `contradicted_claim` 107, `phantom_resolution` 83, `unmerged_work` 73, `narration_verdict_mismatch` 60.

**Only one channel exists (pr_ledger.json + pending_pr_provenance.jsonl, current).** Three open own PRs (#2912, #2986, #2987), all on `maestro/evolve/*` branches; every one of the 66 recorded PR-remediation admissions came through `channel: tend_route` — there is no agent-initiated or direct-delivery channel at all.

**The loop's own words (lifecycle.json, iteration 1374, 2026-07-11).** *"the #2280 action gate refuses both `git push` to the PR branch and `gh pr merge` from the task-execution path"* … *"my resolved merge `95a61d31` gates-green but living only in a local worktree"* … *"UNLANDED-REMEDIATION: four PR[s] … proposing is not your bottleneck; landing is."*

## Prior Point Fixes (whack-a-mole ledger)

Each entry closed one symptom of the narration↔delivery gap; none made the agent a deliverer of its own PR.

| Identifier | What it patched | Why the class survived |
|---|---|---|
| `#2280` (`ce21deba`) | Action-gated task Bash against PR/push approval bypass — created the single-`allowed_branch` constraint | Established the control-flow-bound split every later fix works around; delivery never became agent-owned |
| `797d3141`, `#2817` | `run()` pushes the PR-remediation branch directly; Tend PR fallback | Delivery works only on the narrow `pr_branch` channel; the agent still narrates a success it did not itself land |
| `#2479`, `#2508` | Reconcile/push a diverged remote PR head before routing conflict | Fixed one push-conflict path inside the loop-owned model; the model was unchanged |
| Proposal **070** | Named the dequeue-gate chain as a typed state machine | Made ordering explicit; added zero delivery capability |
| Proposal **080** | One merge oracle + lap budget + one durable handoff | Stops re-minting laps; explicitly "does not touch approvals, the action gate, or the LLM's push path" |
| Proposal **087** | One `is_pr_settled` merge-truth chokepoint, AST-enforced | Fixed *recognizing* merge; the agent still cannot *deliver*, so the gap persists |
| Rules 10/12/13/14/15 | Detect phantom remediation/escalation/resolution/landing/narration | Each detects a different shape of the same phantom success; detection ≠ delivery |
| `#2866` (`230d9b1e`) | Scrub the agent's resolution keywords from unlanded partials at write time | Neutralizes the narration *generator*; the not-delivered condition remains |
| stand-down actuators (`#2865`, `#2907`, `#2663`) | Keyed / mechanical / thread-guarded ways to *quiet* the treadmill | New ways to give up; none lets the agent finish the PR itself |
| Proposal **089** | Observed `ready_and_waiting` fact + progress-aware laps | Reframes churn as legitimate; delivery mechanism untouched |
| Proposal **090** (partial) | Shared cue vocabulary; rule-12/14 actuator arms; dropped-directive ledger | Converts narration → one action; forced arms still default-off; still reconciling the gap, not closing it |
| escape valves (`#2798`, `#2893`, `#2863`) | New-thread lap, reply-completion, conflict-divert | Each re-opens one severed servicing path; the severing pattern recurs |
| Proposal **091** | Retire "awaiting tend push" costume; `push_delivered` truth; tend keys on the open PR; user-msg override | Closest by subject; **found the gate already allows the on-channel PR-branch push**, yet stayed inside the loop-delivers/human-approves split and left `route_user_pr_message` single-`#N` |

Steps 5 and 7 below retire the push half of the loop-delivery split and the `_delivery_note` "delivery is automatic" convention; the design's falsifiability metric is the *reduction* (not deletion) of the phantom-narration rules' firing rate.

## Implementation Plan

1. [x] **Owned-writable-branch oracle.** New `maestro/owned_branches.py` — a pure `compute_owned_writable_branches(current_branch, open_pr_branches, pr_ledger_branches)` returning the provenance-stamped `frozenset[str]` of branches the task executor may push to: the run's own branch plus every `maestro/evolve/*` head confirmed by both the live open-PR set and `PRLedger`, with `main`/`master` excluded unconditionally. Not yet wired into enforcement, so this step is inert.
   - *Depends on:* none.
   - *Acceptance:* the returned set contains an own open evolution-PR branch and the run branch, and never contains `main`/`master`, for representative inputs.
2. [x] **Gate reads an owned-branch set (backward-compatible).** `maestro/tool_gating.py` — `evaluate_bash_command` and `TaskGateHook` accept `allowed_branches: frozenset[str] | None`; `_git_deny` permits a push whose every destination ref is in the set (still denying `main`/`master`, still fail-closed on an unparseable or refspec-less push, and **adding** denial of a `+`/`--force`/`-f`/`--force-with-lease` push — note `_git_deny` today strips the leading `+` in `_dest_ref` and drops `-`-prefixed flags, so force-pushes to the allowed branch are currently *permitted*; this denial is new logic, not a preserved behaviour). Keep `make_task_pretooluse_hook(allowed_branch)` as a shim that builds a one-element set, so no caller changes behaviour yet.
   - *Depends on:* none.
   - *Acceptance:* `evaluate_bash_command("git push origin B", frozenset({"A", "B"}))` allows; the same push to `main`, to an out-of-set `C`, or force-pushing `B` is denied — force-push denial being a deliberate behaviour change (see this step). The one-element shim therefore reproduces today's single-branch verdicts exactly **except** that force-pushing the allowed branch, previously permitted, is now denied.
3. [x] **Cross-process replication carries the set.** `maestro/providers/cli_paced_gate.py`, `maestro/providers/cli_paced.py` — `GATE_BRANCH_ENV` carries a JSON-encoded branch list; `allowed_branches_from_env` returns the `frozenset`; `build_gate_settings`/`serialize_gate_settings` and `task_gate_from_hooks` round-trip the set. A missing/empty env stays fail-closed.
   - *Depends on:* 2.
   - *Acceptance:* a settings payload built for a two-branch set, decoded through the spawned-hook path, produces byte-identical deny/allow verdicts to the in-process gate on the same commands.
4. [x] **Compute and inject the owned set (flag-gated).** `maestro/agent.py`, `maestro/executor.py` — behind `[loop] owned_pr_branch_writes_enabled` (default **false**), compute the owned set once per run via step 1's oracle (run branch ∪ open own evolution-PR branches) and pass it to `make_task_tool_hooks`; when the flag is off, pass today's singleton `{task_branch}`. A host/ledger read failure degrades to the singleton (today's behaviour), fail-closed.
   - *Depends on:* 1, 2, 3.
   - *Acceptance:* with the flag on, a run launched on a `maestro/task/<id>` branch has the open own `maestro/evolve/*` branches in its gate's owned set; with the flag off, only the run branch.
5. [x] **Agent delivers its own push (flag-gated); loop push becomes an idempotent backstop.** `maestro/loop/pr_remediation.py`, `maestro/agent.py` — under the same flag, `_delivery_note` instructs the agent to push its resolution to the PR branch and drops the "delivery is automatic" claim (`_REPLY_DELIVERY_NOTE` and the reply manifest are unchanged — reply delivery stays loop-owned). `run()` §5-push stays as a deterministic, no-op-safe push so `push_delivered` truth is still recorded on the remediation channel.
   - *Depends on:* 4.
   - *Acceptance:* with the flag on, the remediation prompt instructs a direct branch push and omits the automatic-delivery claim; the reply-manifest instruction is unchanged; §5-push run after an already-pushed branch records `push_delivered` without error.
6. [x] **One-oracle enforcement test.** New `tests/test_owned_branch_oracle.py` — an AST/golden test asserting that push-permission truth is derived only from `owned_branches` (the gate and the executor/agent injection site read the oracle; no second site computes "may push to branch X" independently), mirroring `tests/test_merge_truth_chokepoint.py`.
   - *Depends on:* 1–5.
   - *Acceptance:* the test fails if a rival ad-hoc branch-permission check is introduced at any enforcement site.
7. [x] **Retire the subsumed moles.** The default flip to **true** has landed (`[loop] owned_pr_branch_writes_enabled` now defaults on). What remains, once the flag is proven in production: remove the now-false `_delivery_note` push rationale, demote `run()` §5-push comments/guards to "backstop" framing, and narrow (do **not** delete) the write-time resolution-keyword scrubber and phantom rules to the external-blocker cases they still cover.
   - *Depends on:* 6.
   - *Acceptance:* the commit-only push convention is gone, the audit rules' external-blocker tests still pass, and no enforcement path outside the oracle grants a push.

## Testing Strategy

All unit tests use `mem_fs`, patch `asyncio.sleep`, and mock the code host / VCS / subprocess — no real network, filesystem, or `git`.

- **Oracle (`test_owned_branch_oracle.py`, new).** Unit tests for `compute_owned_writable_branches`: own evolve branches present; `main`/`master` never present; a branch in the ledger but absent from the live open-PR set (or vice-versa) excluded; the AST enforcement assertion (step 6) that the gate and injection site are the only readers.
- **Gate (`tests/test_tool_gating.py`).** Extend the existing decision-core table: multi-branch allow, force-push deny, out-of-set deny, `main` deny, the one-element shim reproducing prior single-branch verdicts, and the fail-closed refspec-less/unparseable cases — driven through `evaluate_bash_command` and the `TaskGateHook` callback.
- **Cross-process (`tests/test_tool_gating.py`, `tests/test_provider_cli_paced.py`).** Round-trip a two-branch set through `build_gate_settings` → env → `run_gate_bash_hook` and assert verdict parity with the in-process gate on the same command set; assert empty/missing env stays fail-closed.
- **Injection + flag (`tests/test_agent*.py`, `tests/test_executor.py`).** With `owned_pr_branch_writes_enabled` on, assert the executor receives the open-PR branch set on a task-branch run and that a host-read failure degrades to the singleton; with the flag off, assert today's singleton is passed unchanged.
- **Prompt (`tests/test_pr_remediation*.py`).** Assert the flag-on `_delivery_note` instructs a direct push and drops the automatic-delivery claim while `_REPLY_DELIVERY_NOTE`/manifest wording is untouched; assert §5-push records `push_delivered` when run after the branch is already up to date.

## Risk Assessment

**Trust boundary.** This change edits the #2280 approval-bypass gate, so it must land in `security/` and inherit security review. The new attack surface is "the agent can push to a wider set of branches." It is bounded three ways: the oracle admits a branch only when it is confirmed as an own open `maestro/evolve/*` head by **both** the live host open-PR set and `PRLedger`, the `maestro/evolve/*`/`maestro/task/*` namespaces are created only by Maestro's own machinery (provenance in the name), and `main`/`master` are denied unconditionally and fail-closed. Force-pushes to the own branch are **not** denied by today's gate (`_dest_ref` strips the leading `+` and `--force` is ignored), so step 2 must **add** explicit `+`/`--force` refspec denial as new behaviour. Pushing to an own evolution-PR branch does **not** bypass approval — the PR still requires human review and a human merge — which the module docstring already states for the own-branch case. `gh pr create`, `gh pr merge`, and mutating `gh api …/pulls` remain denied (see Out of scope). The step-6 AST test is the control that proves the gate cannot be widened beyond the oracle; it is the analogue of `tests/test_merge_truth_chokepoint.py`.

**Rollback.** Every step is independently mergeable and the behaviour change is gated behind `[loop] owned_pr_branch_writes_enabled`, default **false**, so steps 1–6 ship inert; the one-element-set shim in the gate guarantees byte-identical verdicts while the flag is off. Reverting is flipping the flag. Step 7 (default-on + mole retirement) lands only after production confirmation.

**Concurrency / delivery truth.** The agent's push and the loop's §5-push are both idempotent (pushing an up-to-date branch is a no-op; a non-fast-forward already fetch-retries and concedes to Tend PR), so keeping both cannot double-deliver or corrupt `push_delivered`. Off-channel pushes (no §5-push) are grounded after the fact by the existing `is_pr_settled` / `observe_pr_readiness` sensors on the next lap.

**Falsifiability.** If the design is right, over the cycles after the flag flips on: remediation `partial`-with-unlanded rate falls, `measure_confirmed_landing_rate` (CONFIRMED-LANDING-RATIO) rises, `measure_unlanded_remediations` (UNLANDED-REMEDIATION) falls, and the audit-cycle firing rate of `phantom_remediation` / `phantom_resolution` / `narration_verdict_mismatch` drops from the current majority-of-cycles toward baseline. It failed if those metrics do not move, or if the gate ever admits a push to a branch outside the oracle (the step-6 test and a push-to-foreign-branch regression test are the tripwires).

## Out of scope

- **Merging a PR to `main`.** `gh pr merge` and any push to `main`/`master` stay denied — merging is the human-in-the-loop invariant, and there is no merge-to-`main` code path in the repository. The agent's `gh pr merge` attempts in production are a symptom of delivery frustration, not a capability to grant.
- **Review-thread reply delivery.** Replies stay loop-owned via the recorded manifest (`_REPLY_DELIVERY_NOTE`, `_REPLY_MANIFEST_FILENAME`) because a reply POST is not idempotent and a second one double-posts the thread; only branch pushes (idempotent) are freed.
- **Force-push to own PR branches.** Step 2 **adds** explicit `+`/`--force` refspec denial to keep PR history append-only — today's gate permits it (`_dest_ref` strips the leading `+` and `--force` is ignored), so this is new behaviour, not a pre-existing default; the user goals (push updates, merge `origin/main` in, keep current with `main`) need only fast-forward/merge pushes. Granting force-push is revisitable as a separate knob.
- **Perfecting `route_user_pr_message`** for plural/unnumbered demands (091's single-`#N` limit). Once delivery is provenance-bound, correct routing is a convenience, not the gap-closer; it is left as a follow-up rather than folded in here.

# 096: Push Conformance — Bind the Action Gate's Allow-Set to the Pushes the Loop Actually Instructs

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/tool_gating.py` (`_git_deny`, `_split_commands`, `evaluate_bash_command`), `maestro/loop/pr_remediation.py` (`_delivery_note`), `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `tests/test_tool_gating.py`, new `tests/test_push_conformance.py`, `tests/test_config*.py` |
| **Depends on** | 093 (shipped) |
| **Derived from** | live production data via /propose, 2026-07-15 |

## Description

> Status updated to shipped on 2026-07-17 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The #2280 task-executor action gate denies Maestro's `git push` to its own open evolution-PR branch whenever the command carries an ordinary output-capture redirection — `git push origin maestro/evolve/<id> 2>&1 | tail -12` — even when that branch is in the run's owned-writable set. The gate's hand-rolled refspec parser splits the command on shell control operators but never strips shell *redirections*, so the `2>&1` tokenizes into a stray `2` that the parser reads as a second destination refspec; `2` is not an owned branch, and the whole push is denied fail-closed. A redirect-free push to the identical branch is allowed. Because appending `2>&1 | tail` to capture output is the idiom the agent uses on essentially every push, the gate denies the push almost every time it is attempted.

This matters now because proposal 093 ("Own-PR Self-Delivery", HIGH, still `partial`) shipped the *capability* and the *instruction* to close exactly this gap — its owned-writable-branch oracle puts the PR branch in the gate's `allowed_branches` set, and its `_delivery_note` tells the agent to *"Deliver your fix yourself … push it to this PR's branch with `git push origin HEAD:maestro/evolve/<id>`"* — but the gate's *enforcement* silently refuses the instructed push. The two layers were validated by disjoint test corpora that never meet: `tests/test_owned_branch_oracle.py` proves the set is correct and `tests/test_tool_gating.py` proves clean commands parse, but no test proves that *the push the loop instructs, wrapped as the agent actually writes it, is allowed*. So 093's push arm never worked end-to-end, the loop is now blocked on landing every one of its own PRs, and the agent has been told by Thomas — *"You can git push to your own branch"* — while the gate keeps denying it hours later. The operator's report ("Maestro claimed it was not able to push to its own branch") is not a misperception; the claim is correct and the gate is the reason.

The structural gap is that the gate's *allow-set* and the loop's *instruct-set* have no conformance contract binding them, and the gate's refspec derivation is a hand-rolled shell parser that models pipes, env-prefixes, global flags, `bash -c` wrappers, and force-flags but omits redirections — so any un-modeled shell shape mis-buckets a token and either fails open (evasion, the documented threat-model gap) or fails closed (this bug, denying a legitimate own-branch push). The change installs the missing contract as a deterministic golden/property test — every push form `_delivery_note` instructs, wrapped in the realistic output-capture idioms the agent uses, must be *allowed* for an in-set branch and *denied* for `main`, an out-of-set branch, or a force-push — and makes it pass by teaching the parser to strip shell redirections, completing the shell model rather than special-casing `2>&1`.

The end state: whichever ordinary shell wrapper the agent puts around its push, the gate's verdict matches the loop's instruction; a resolved conflict can finally reach `origin/<pr_branch>` on the same lap; and "the gate denies a push the loop told the agent to make" becomes a tested-impossible state, so the next un-modeled shell shape cannot silently re-break the push arm. The human-in-the-loop invariant is untouched: `main`/`master`, `gh pr merge`, force-pushes, and out-of-set branches stay denied, proven by a no-fail-open regression test that is the security control for this change.

## Relevant Findings

- `maestro/tool_gating.py` (`_split_commands`) — tokenizes with `shlex(..., punctuation_chars=True)` and splits on the control operators in `_SHELL_OPERATORS` (`&&`, `||`, `;`, `|`, `&`, `()`). A redirection such as `2>&1` is **not** an operator, so it is neither split nor stripped: it tokenizes into `2`, `>&`, `1` and those tokens stay in the same segment as the `git push`.
- `maestro/tool_gating.py` (`_git_deny`) — computes `positional = [a for a in rest if not a.startswith("-")]` and treats `positional[2:]` as the push's refspecs. Fed `git push origin maestro/evolve/<id> 2>&1`, the leaked `2` (and `1`) become refspecs; `_dest_ref("2")` is `"2"`, which is not in `allowed_branches`, so the function returns `_PUSH_DENY`. Empirically reproduced through `evaluate_bash_command`: the redirect-free push is allowed, the `2>&1`-wrapped push is denied.
- `maestro/tool_gating.py` (`_strip_env_prefix`, `_strip_leading_global_flags`, `_wrapper_deny`) — the parser already carries purpose-built strippers for env-prefixes (`GH_TOKEN=x …`), leading global flags (`git -C /repo push`), and `bash -c` wrappers. Redirection is the one common shell construct with no stripper — the omission this proposal fills, in the same shape as the existing ones.
- `maestro/loop/pr_remediation.py` (`_delivery_note`) — under `[loop] owned_pr_branch_writes_enabled` (default **on**) returns *"Deliver your fix yourself … push it to this PR's branch with `git push origin HEAD:maestro/evolve/<id>`"*. The instructed command shape lives only in this prose string; nothing binds it to what the gate will accept.
- `maestro/agent.py` (`_gate_allowed_branches`, `run()` §5-push) — `_gate_allowed_branches` computes the owned set (093) and the deny logs confirm it is populated with the live `maestro/evolve/*` heads; §5-push is the loop-side deterministic `self._vcs.push(pr_branch)` that exists as the backstop *because* the agent's own push kept being refused. The backstop cannot deliver a conflict resolution the agent produced on a `maestro/task/<id>` run that never reaches §5-push's `branch_name == pr_branch` guard.
- `tests/test_tool_gating.py` — contains 47 `git push` assertions and **zero** that wrap the push in `2>&1`, a pipe-to-`tail`, or any output-capture idiom; the corpus exercises clean hand-written commands the agent never actually emits. This is the coverage gap that let the redirect leak ship.
- `tests/test_owned_branch_oracle.py` (`test_push_permission_computed_only_at_the_oracle_door`) and `tests/test_merge_truth_chokepoint.py` — the house pattern this proposal reuses: a deterministic AST/golden test pinning one invariant as the only door. The push-conformance test is the analogue for "the gate allows every instructed push."
- `maestro/remediation_tracker.py` (`classify_blocker_ownership`) — classifies `AWAITING_PUSH` as external ("Tend's mechanical … push delivers them"); with the agent's own push blocked, the loop leans on this external framing, and the resolved-but-unpushable branch stays `CONFLICTING` and re-laps. Not changed here (the push, once unblocked, clears the state naturally), noted so the design's falsifiability metric is grounded.

## Production Evidence

Live data via the host bind-mount of `maestro-loop` at `/opt/maestro/data` (strictly read-only). Windows stated per figure. No secrets appear in the quoted spans.

**The gate denies the agent's own-branch push, with the branch in the allowed set (`logs/maestro.log`, 2026-07-15).** 16 own-branch push denials in one day across 5 distinct evolution branches (`maestro/evolve/{7a2d8d96e5a1, 7d8b03685a41, ec6266876b1c, f2ee34874fd1, 1d24eec7a9c3}`). The frozenset in the deny line *contains* the branch being pushed:

```
00:13:49 tool_gating denied (branches=frozenset({'maestro/evolve/f2ee34874fd1',
         'maestro/evolve/7a2d8d96e5a1', 'maestro/evolve/ec6266876b1c',
         'maestro/evolve/7d8b03685a41'})):
         git push origin maestro/evolve/7a2d8d96e5a1 2>&1 | tail -12
12:34:36 SDK tool result [Bash]: ERROR Blocked: pushing to main or a branch
         other than this run's own task branch bypasses the human-in-the-loop
         approval gate.
```

**Empirical reproduction (current `maestro/tool_gating.py`, dev tree).** Through `evaluate_bash_command(cmd, frozenset({'maestro/evolve/7a2d8d96e5a1', …}))`:

```
DENIED   git push origin maestro/evolve/7a2d8d96e5a1 2>&1 | tail -12
DENIED   git push origin HEAD:maestro/evolve/f2ee34874fd1 2>&1 | tail -10
DENIED   git push origin maestro/evolve/7a2d8d96e5a1 > out.txt 2>&1
ALLOWED  git push origin maestro/evolve/7a2d8d96e5a1
ALLOWED  git push origin maestro/evolve/7a2d8d96e5a1 | tail -12
```

The redirect is the sole difference between allow and deny.

**Remediation never lands, before or after 093 (`task_history.jsonl`, 2026-06-24 → 2026-07-15).** 37 of the last 136 task rows are `Remediate PR #N` laps; **all 37 closed `partial`, zero `success`.** Splitting on 093's landing (~2026-07-12): the 9 post-093 laps flipped their verdict marker from "awaiting tend push" to "delivered to pr, awaiting review/merge" — 093's narration change working — yet **all 9 are still `partial`**, now blocked `CONFLICTING`, and **PR #2808 (the reported PR) is still open and `CONFLICTING` on 2026-07-15T16:11**, 20 days after its first lap.

**The agent resolves the conflict but cannot push the resolution (`messages.jsonl`, 2026-07-14).** *"PR #3001 conflict resolution — DONE and VERIFIED, but the push is action-gated. I checked out the PR branch (maestro/evolve/7a2d8d96e5a1), merged origin/main, and resolved all 4 conflicts … but the push is action-gated."* The gate denied pushes to `maestro/evolve/7a2d8d96e5a1` on 2026-07-15. This is the CONFLICTING treadmill's engine: the fix exists locally, the push is refused, the state never clears.

**The instruction↔enforcement contradiction (`messages.jsonl`, paraphrased).** The operator told the agent it was allowed to push to its own branch. Maestro: *"Understood — that removes the blocker I kept citing. My branch pushes were never the gate I imagined; I own that excuse now."* Then on 2026-07-15 the gate denied its pushes 16 times, and the agent — told the block was imaginary — recorded *"The push is correctly blocked — the task executor must not push PR branches"* (`logs/maestro.log`, SDK text). The system taught the agent to disbelieve a real capability gap.

## Prior Point Fixes (whack-a-mole ledger)

Every entry either taught the hand-rolled parser one more shell shape, or worked around the agent's-push-being-denied by moving the push elsewhere. None bound the gate's allow-set to the loop's instruct-set, so redirection — the next un-modeled shape — silently re-broke the push arm.

| Identifier | What it patched | Why the class survived |
|---|---|---|
| `#2280` (`ce21deba`) | Created the gate; refspecs derived positionally from a hand-tokenized command line | Established the hand-rolled parser whose every gap is a future mole; no conformance contract |
| `636476b2` | Carve-out so `gh pr comment` (wrongly denied by the fail-closed default) is allowed | One more special-case for a shape the parser mis-judged; parser still un-conformance-tested |
| `#2819` (`e14db44c`) | Carve-out for `gh api …/replies` / `in_reply_to` review-comment replies | Same shape of fix — a per-construct exception, added after the construct leaked |
| Owned-disposal lever (`38f54678`) | Carve-out permitting `gh pr close` as reversible disposal | Another wrongly-denied verb rescued one at a time |
| `797d3141`, `#2817` | `run()` §5-push: the **loop** pushes the remediation branch because the agent's push is gated | Worked around the denial instead of fixing it; only covers the narrow `branch_name == pr_branch` channel |
| Proposal **091** | `push_delivered` truth + retire the "awaiting tend push" costume | Relabels the outcome after the loop pushes; the agent's own push stays denied off-channel |
| Proposal **093** (`f23f8a61`, `ab390072`) | Owned-writable-branch oracle + multi-branch gate + force-push denial; `_delivery_note` instructs the push | Widened the allowed set and force-denied, but its push tests use clean commands — the redirect leak was never exercised, so the instructed push it enables is still refused |

The design retires the redirect leak in `_git_deny` and dissolves the *rationale* for the §5-push workaround (the agent's push, once it actually works, is the primary deliverer; §5-push stays as an idempotent backstop, not a co-owner). The falsifiability metric is the disappearance of the `tool_gating denied … git push … maestro/evolve/*` log lines, not any new mechanism.

## Implementation Plan

1. [x] **Push-conformance golden test (fails first).** New `tests/test_push_conformance.py` — for the canonical own-branch push form the loop instructs, wrapped in each realistic idiom the agent emits (bare, `… 2>&1 | tail -N`, `… 2>&1`, `… > out 2>&1`, `cd /workspace; … 2>&1 | tail`, `…; echo EXIT=$?`), assert `evaluate_bash_command` **allows** it for an in-set branch and **denies** it for `main`, an out-of-set branch, and a `+`/`--force` variant. Drives both `git push origin maestro/evolve/<id>` and `git push origin HEAD:maestro/evolve/<id>`. This step lands red, pinning the gap.
   - *Depends on:* none.
   - *Acceptance:* the test file exists and fails on today's `tool_gating.py` exactly on the redirect-wrapped in-set cases, passing on the `main`/out-of-set/force cases.
2. [x] **Redirection-aware refspec extraction (flag-gated, inert by default).** `maestro/tool_gating.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` — behind `[loop] gate_strip_redirections_enabled` (default **false**), strip shell redirections and their operands (`>`/`>>`/`<`, `2>`, `>&`/`&>`, `2>&1`, `>|`, fd-numeric forms) from each segment before `_git_deny` reads refspecs, mirroring the existing `_strip_env_prefix` / `_strip_leading_global_flags` strippers. When the flag is off, tokenization is byte-identical to today.
   - *Depends on:* 1.
   - *Acceptance:* with the flag on, step 1's test passes; with the flag off, every existing `tests/test_tool_gating.py` verdict is unchanged.
3. [x] **No-fail-open regression guard.** `tests/test_tool_gating.py`, `tests/test_push_conformance.py` — with the flag on, assert a redirect-wrapped push to `main`/`master`, to an out-of-set branch, a force-push (`+ref`, `--force`, `--force-with-lease`), a redirection hiding a second refspec (`git push origin ok-branch main 2>&1`), and a redirection whose operand is an in-set branch name (`git push origin > maestro/evolve/<id>` — must stay a fail-closed refspec-less push, never laundered into an owned target by stripping `>` without its operand) all still **deny**; assert an unparseable/refspec-less push still fails closed. This is the security control proving the fix only ever moves DENY→ALLOW for genuinely-owned targets.
   - *Depends on:* 2.
   - *Acceptance:* no redirect wrapper turns a should-be-denied push into an allowed one; the fail-closed defaults are intact.
4. [x] **One-owner push template consumed by prompt and test.** `maestro/loop/pr_remediation.py`, `tests/test_push_conformance.py` — extract the canonical own-branch push command shape `_delivery_note` interpolates into a shared module-level constant (e.g. `OWN_BRANCH_PUSH_TEMPLATE`), and have the conformance test derive its subject from that same constant, so the form the loop instructs and the form the gate is tested against can never drift apart. Prose wording is unchanged; only the command literal is centralized.
   - *Depends on:* 1.
   - *Acceptance:* `_delivery_note` and `test_push_conformance` reference one shared template symbol; changing the template updates both, and the conformance test re-checks the new form automatically.
5. [x] **Default-flip after production confirmation; retire the mole.** `maestro.toml`, `maestro/config.py`, `maestro/loop/pr_remediation.py` — once the flag is proven in production (no gate denial of an in-set instructed push, no fail-open regression), flip `gate_strip_redirections_enabled` to **true** and demote the `run()` §5-push comments/guards from "the loop pushes because the agent can't" to "idempotent backstop." §5-push stays as code; only its rationale narrows.
   - *Depends on:* 2, 3, 4.
   - *Acceptance:* redirection-aware stripping is default-on, the §5-push backstop framing no longer claims the agent cannot push, and no enforcement path outside the oracle grants a push.

## Testing Strategy

All unit tests use `mem_fs`, patch `asyncio.sleep`, and mock the code host / VCS / subprocess — no real network, filesystem, or `git`; the gate functions under test are pure and synchronous.

- **Conformance (`tests/test_push_conformance.py`, new).** The instruction↔enforcement contract of step 1: the canonical push form × the realistic wrapper set × {in-set → allow, `main`/out-of-set/force → deny}, driven through `evaluate_bash_command` and `TaskGateHook.__call__`. Derives its subject from the shared push template (step 4) so it tracks the prompt.
- **Parser (`tests/test_tool_gating.py`).** Extend the decision-core table with redirection cases: `2>&1`, `> f`, `2> f`, `&> f`, `>| f`, `1>&2`, and combinations with a trailing pipe (`… 2>&1 | tail`); assert the flag-off path leaves every current verdict byte-identical (the inert-ship guarantee).
- **No-fail-open (step 3).** Redirect-wrapped `main`/out-of-set/force/second-refspec pushes deny, as does a redirect whose operand looks owned (`git push origin > maestro/evolve/<id>`); refspec-less and unparseable pushes fail closed — the guard that the fix never widens the gate beyond owned, non-force targets.
- **Config (`tests/test_config*.py`).** `gate_strip_redirections_enabled` parses from TOML, defaults false, and validates as a bool.
- **Template (`tests/test_pr_remediation*.py`).** `_delivery_note(True)` interpolates the shared push template; the prose is unchanged and `_REPLY_DELIVERY_NOTE` is untouched.

## Risk Assessment

**Trust boundary.** This edits the #2280 approval-bypass gate, so it lands in `security/` and inherits security review. The fix only ever moves verdicts from DENY→ALLOW, and only for commands containing a redirection — the direction that needs scrutiny. It is bounded three ways: (1) stripping a redirection cannot invent an owned target — after `git push origin main 2>&1` loses `2>&1`, the refspec is still `main`, still denied; (2) the no-fail-open regression test (step 3) asserts every `main`/out-of-set/force/second-refspec redirect-wrapped push still denies; (3) `gh pr create`/`gh pr merge`/mutating `gh api …/pulls`, force-pushes, and the fail-closed unparseable default are all untouched. Pushing to an own evolution-PR branch does not bypass approval — the PR still needs a human merge — as 093 established.

**Rollback.** Every step is independently mergeable; the parser change is gated behind `[loop] gate_strip_redirections_enabled`, default **false**, so steps 1–4 ship inert with byte-identical verdicts (the flag-off tokenization guarantee). Reverting is flipping the flag. Step 5 (default-on + mole retirement) lands only after production confirms no fail-open regression. Because the current state is a total block on the loop landing its own PRs, step 5's flip is the payoff and should follow closely once steps 1–4 are green.

**Interaction with 093.** This depends on 093 and does not supersede it — 093's oracle, owned-set, and cross-process replication are all correct and load-bearing. It closes the correctness-and-conformance defect that left 093's push arm non-functional end-to-end. If 093's `owned_pr_branch_writes_enabled` is off, the owned set collapses to the singleton and this fix is moot for evolution branches, so the two flags compose safely.

**Falsifiability.** If the design is right, after step 5: the `tool_gating denied … git push … maestro/evolve/*` log lines go to zero; post-093 remediation `partial`-with-`CONFLICTING` rate falls (a resolved conflict can finally push); `measure_confirmed_landing_rate` (CONFIRMED-LANDING-RATIO) rises and `measure_unlanded_remediations` (UNLANDED-REMEDIATION) falls. It failed if instructed own-branch pushes are still denied, or if the no-fail-open test ever admits a redirect-wrapped push to `main`/out-of-set/force — the step-3 guard and a push-to-foreign-branch regression test are the tripwires.

## Out of scope

- **The CONFLICTING re-lap loop itself.** Once the push is unblocked a resolved conflict clears the state; the residual churn (branches falling behind a fast `main`) is governed by proposals 089/095 and the OWNED-DISPOSAL clause, not this change.
- **`classify_blocker_ownership` re-bucketing `AWAITING_PUSH`.** The push, once it actually works, clears `AWAITING_PUSH` before it can persist; re-classifying it as owned is a separate question this proposal does not need to answer.
- **Off-channel delivery (`route_user_pr_message` plural/unnumbered demands).** 093 deferred this; it remains a convenience once the on-channel push works, not the gap-closer here.
- **General shell sandboxing / adversarial evasion.** The gate's threat model is a redirected, non-adversarial model (per the `tool_gating` module note); this proposal fixes a fail-*closed* parsing gap and does not attempt to close fail-*open* evasion (`gh` by absolute path, `xargs`, GraphQL mutation), which remains proposals 024/025's ground.

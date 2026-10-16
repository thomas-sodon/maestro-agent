"""Task-delivery stages carved out of ``maestro.agent`` (proposal 106).

Holds the four stages ``MaestroAgent.run`` executes between the remediation
verdict's git-truth bind and the summary composition: the artifact commit
(``# 5.``), the backstop push plus delivery-truth marker reconciliation
(``# 5-push.``), the addressed-thread auto-resolution (``# 5-push-ii.``), and
the completion-state observations (``# 5a.`` / ``# 5a-ii.``). They form one
concern — the lap *delivering* what it produced and then observing what the
completed lap looks like — and ``run`` is their sole caller.

The extraction is behaviour-preserving: the same bodies, the same call order,
the same effects — only the receiver changed from ``self`` to a first
positional ``agent`` argument, matching
:func:`maestro.loop.task_outcome.classify_task_outcome` and
:func:`maestro.loop.remediation_stamps.apply_host_state_stamp_gate`.
``check_merge_clean`` and the ``_OWNED_PUSH_FIRE_OR_CLOSE_DIRECTIVE``
constant are imported from their defining modules; tests patch
``check_merge_clean`` on this module.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from maestro.contributions import check_merge_clean
from maestro.evolution_tend import _resolvable_thread_ids
from maestro.loop.pr_remediation import _OWNED_PUSH_FIRE_OR_CLOSE_DIRECTIVE
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    BranchReachability,
    classify_branch_reachability,
    resolve_push_delivery_marker,
)

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.types import TaskOutcome

logger = logging.getLogger("maestro")


def commit_task_artifacts(
    agent: MaestroAgent,
    *,
    branch_name: str | None,
    pr_branch: str | None,
    outcome: TaskOutcome,
    branch_base_sha: str | None,
    task_id: str,
    goal: str,
) -> str | None:
    """Commit this lap's task artifacts if the branch is worth keeping, and
    return the kept branch name (or ``None``) — ``run``'s ``# 5.`` block,
    extracted verbatim.

    Branch/working-tree cleanup runs in ``run``'s outer ``finally`` via
    ``_teardown_task_branch()`` so it happens on graceful exit AND on
    escaping exceptions. A clean tree is NOT proof of no work: steps may
    have committed their own work mid-run (Bash permits ``git commit``), so
    an owned task branch (``pr_branch is None``) that carries commits of its
    own is kept too (#2938) — measured by ``_branch_carries_own_commits``,
    which narrows the raw ahead-of-``main`` count past a merge of ``main``,
    past upstream commits fast-forwarded in from ``origin/main``, and past
    commits inherited from a stale cut. The widening must stay scoped to
    owned branches: a remediation PR branch is nearly always ahead of
    ``main``, and the ``addressed_red`` consumer reads ``kept_branch`` as
    the loop's own commit — it measures an agent self-commit separately,
    from the PR-head checkout watermark — so this keep arm stays narrow on
    purpose.
    """
    kept_branch: str | None = None
    try:
        if branch_name and outcome in ("success", "partial"):
            if agent._vcs.has_changes():
                agent._vcs.stage_all()
                agent._vcs.commit(
                    f"maestro: task {task_id} — {goal[:80]}",
                )
                kept_branch = branch_name
            elif pr_branch is None and agent._branch_carries_own_commits(
                branch_name, branch_base_sha,
            ):
                kept_branch = branch_name
    except Exception:
        logger.warning(
            "Failed to commit task artifacts", exc_info=True,
        )
    return kept_branch


def apply_push_delivery_reconciliation(
    agent: MaestroAgent,
    *,
    branch_name: str | None,
    pr_branch: str | None,
    outcome: TaskOutcome,
    remediation_note: str | None,
    remediation_pr_num: int | None,
) -> tuple[bool | None, str | None, str | None]:
    """Deliver the backstop push, then reconcile the record's marker with
    delivery truth — ``run``'s ``# 5-push.`` block, extracted verbatim
    (proposal 106 / 091 steps 3-4).

    The idempotent backstop push sits *behind* the agent's own delivery:
    the remediation prompt already instructs the agent to push its fix to
    the PR branch (proposal 093), so this is normally a no-op safety net
    that closes the gap when the agent did not push (or its push raced),
    delivering the fix to ``origin/<pr_branch>`` on this lap instead of
    waiting for the next tend pass. It is a deterministic Python VCS call
    the Bash action-gate never sees, scoped to a deliverable remediation
    outcome on the PR branch we are on (never a task branch, checkout
    fallback, or failure). ``push_delivered`` records this lap's delivery
    truth (proposal 091 step 3): ``True`` the resolution reached
    ``origin/<pr_branch>`` (after at most one fetch+retry), ``False`` both
    attempts failed, ``None`` the push was never attempted; ``push_error``
    carries the retry's failure message.

    Then the marker reconciliation (proposal 091 step 4, made *total* by
    proposal 098) maps that delivery truth onto the honest marker inside
    ``remediation_note``: a successful push swaps the ``AWAITING_TEND_PUSH``
    costume for ``DELIVERED_AWAITING_MERGE``; a failed push, or one never
    attempted on a branch git proves is ``OWNED_AHEAD``, records
    ``OWNED_PUSH_AVAILABLE`` (with a PUSH FAILED suffix on the failed arm)
    and fires the bounded fire-or-close directive into an *empty*
    ``next_task_directive`` slot — never the external-deferral costume,
    since pushing the resolution is an owned act. Returns the possibly
    rewritten ``(push_delivered, push_error, remediation_note)`` for the
    downstream stages and the ``TaskRecord`` constructor to read.
    """
    push_delivered: bool | None = None
    push_error: str | None = None
    if (
        pr_branch is not None
        and branch_name == pr_branch
        and outcome in ("success", "partial")
    ):
        try:
            agent._vcs.push(pr_branch)
            push_delivered = True
        except Exception:
            # Origin may have advanced concurrently: best-effort fetch
            # then one retry before conceding delivery to Tend PR.
            try:
                agent._vcs.fetch("origin")
            except Exception:
                logger.warning(
                    "Fetch before push retry failed for PR branch %s",
                    pr_branch,
                    exc_info=True,
                )
            try:
                agent._vcs.push(pr_branch)
                push_delivered = True
            except Exception as retry_exc:
                push_delivered = False
                push_error = str(retry_exc)
                logger.warning(
                    "Failed to push remediation to PR branch %s after "
                    "retry; leaving delivery to Tend PR",
                    pr_branch,
                    exc_info=True,
                )

    # Reconcile the recorded marker with delivery truth (proposal 091
    # step 4). A successful push turns the pre-push ``awaiting tend
    # push`` costume into the honest ``delivered … awaiting review/merge``
    # marker so ``_frame_remediation_note`` embeds the correct signature
    # into ``result_summary`` / ``key_findings``. Proposal 098 makes
    # this reconciliation *total* across every delivery-truth state in a
    # single unconditional path: a push that failed both attempts, and a
    # push never attempted on a branch git proves is ``OWNED_AHEAD``,
    # both record ``Marker.OWNED_PUSH_AVAILABLE`` (with a PUSH FAILED
    # suffix on the failed arm) and route a bounded fire-or-close
    # directive — never the ``AWAITING_TEND_PUSH`` external-deferral
    # costume, because pushing the resolution is *my* act
    # (``classify_blocker_ownership`` classifies ``AWAITING_PUSH``
    # owned), so keeping it would have the loop write, in its own hand,
    # the verdict its own remediation preamble forbids for an owned
    # blocker. That deferral record is not inert: it feeds the
    # costume-signature brake, FORCED-QUIET-RATIO, the verdict-fetch gate
    # and the audit rules that police narrated-but-unlanded resolutions,
    # so the honest marker keeps the loop from manufacturing the exact
    # signal its audit layer spends its budget contesting. A
    # never-attempted push with nothing local to push keeps the legacy
    # marker — that deferral is honest.
    #
    # ``resolve_push_delivery_marker`` maps all three delivery-truth
    # states to the honest marker. The never-attempted arm is grounded in
    # git rather than assumed: ``_branch_ahead_of_origin_count`` is
    # fetch-first (so a commit a prior Tend lap already pushed does not
    # read as still-local) and per-iteration memoized (so the chosen
    # stand-down guard's own later read costs nothing extra). Only the
    # narrow never-attempted-yet-on-a-PR-branch case probes: a checkout
    # fallback left ``branch_name != pr_branch``, or the outcome was not
    # deliverable.
    #
    # The reconciliation keys on the lap's delivery truth, not on whether
    # §4c/§4d happened to author a note (#4161): a note-less remediation
    # lap (e.g. a step-count ``partial`` with zero outstanding review
    # items) whose push failed, or was never attempted on an ahead
    # branch, mints a §4d-form note carrying the owned marker so every
    # ``[owned-push-available]`` sensor sees it — unless the PR already
    # settled, where an owned stamp would be false. Only a deliverable
    # (``success``/``partial``) lap enters: a ``failed`` lap's push was
    # deliberately skipped, so a "Resolved partially" note directing the
    # push would contradict it. The note-less arm adds the fetch-first
    # ahead probe on a never-attempted push, and the merge-truth read
    # runs only once the marker resolves owned.
    if remediation_note is not None or (
        remediation_pr_num is not None
        and pr_branch is not None
        and outcome in ("success", "partial")
    ):
        reachability: BranchReachability | None = None
        if push_delivered is None and pr_branch is not None:
            reachability = classify_branch_reachability(
                agent._branch_ahead_of_origin_count(pr_branch),
            )
        resolved_marker = resolve_push_delivery_marker(
            push_delivered, reachability,
        )
        if remediation_note is None:
            if (
                resolved_marker is not Marker.OWNED_PUSH_AVAILABLE
                or remediation_pr_num is None
                or agent.is_pr_settled(remediation_pr_num).settled
            ):
                return push_delivered, push_error, None
            remediation_note = (
                f"Resolved partially — {Marker.OWNED_PUSH_AVAILABLE} "
                f"the resolution is still local — push+land or close it."
            )
        # Swap whichever marker §4c stamped for the resolved one.
        # When the resolver returns it unchanged (nothing local to
        # push) the replace is a no-op and the legacy note survives
        # byte-identical.
        remediation_note = remediation_note.replace(
            Marker.AWAITING_TEND_PUSH, resolved_marker,
        )
        if (
            resolved_marker is Marker.OWNED_PUSH_AVAILABLE
            and Marker.OWNED_PUSH_AVAILABLE not in remediation_note
        ):
            # §4c is not this note's only author, so the swap above
            # is not total: §4d's completeness gate composes its own
            # "Resolved partially — …" note on a lap that never
            # entered §4c (its guard is ``outcome in (success,
            # partial)``, not §4c's ``success``), and §4e/§4f rewrite
            # what they find. On such a note the replace matches
            # nothing, so without this the directive below would fire
            # and the warning log loudly while the *record* carried
            # no owned marker at all — invisible to every sensor
            # step 6 extended (``_UNDELIVERED_PARTIAL_MARKERS``,
            # ``_FORCED_QUIET_MARKERS``, ``_ON_BRANCH_HANDOFF_MARKERS``,
            # ``_has_unconsumed_awaiting_push_verdict``), which is
            # the precise coverage the marker swap exists to keep.
            # Append rather than replace, mirroring
            # ``_refuse_owned_push_standdown``, so the delivery truth
            # reaches the record on every owned-push lap.
            remediation_note = (
                f"{remediation_note} {Marker.OWNED_PUSH_AVAILABLE} "
                f"the resolution is still local — push+land or "
                f"close it."
            )
        if push_delivered is False:
            remediation_note = (
                f"{remediation_note} PUSH FAILED: {push_error}"
            )
        if resolved_marker is Marker.OWNED_PUSH_AVAILABLE:
            # Fail-loud on both non-delivery arms, then route the
            # next task at the act I actually own. Deferring to a
            # non-empty slot mirrors ``_refuse_owned_push_standdown``:
            # a higher-precedence bridge that already spoke wins.
            logger.warning(
                "Remediation on PR branch %s did not deliver its "
                "push (%s) — recording an owned-push partial and "
                "routing to fire-or-close rather than deferring to "
                "Tend.",
                pr_branch,
                "push failed" if push_delivered is False
                else "push never attempted, branch ahead of origin",
            )
            if remediation_pr_num is not None and pr_branch:
                model = agent.consciousness.load_self_model()
                if not model.next_task_directive:
                    model.next_task_directive = (
                        _OWNED_PUSH_FIRE_OR_CLOSE_DIRECTIVE.format(
                            pr=remediation_pr_num, branch=pr_branch,
                        )
                    )
                    agent.consciousness.save_self_model(model)
    return push_delivered, push_error, remediation_note


def resolve_addressed_review_threads(
    agent: MaestroAgent,
    *,
    delivered_ids: set[int],
    push_delivered: bool | None,
    remediation_pr_num: int | None,
) -> None:
    """Resolve the addressed AI-finding threads — ``run``'s ``# 5-push-ii.``
    block, extracted verbatim (proposal 091 step 5).

    After a remediation lap confirmed BOTH deliveries — each terminal reply
    posted without error (``delivered_ids``) and the fix pushed to
    ``origin/<pr_branch>`` (``push_delivered`` True; a reply-only lap still
    qualifies because pushing an up-to-date branch succeeds) — GitHub still
    shows the addressed threads unresolved forever, since nothing else can
    resolve a thread. The loop is already the sole trusted reply-poster
    (§4c-ii) and pusher (§5-push); it is the only resolver here too, so the
    remediation prompt's "Do NOT resolve any thread" contract stays intact.
    Strictly guarded by ``_resolvable_thread_ids``: only threads rooted in an
    AI finding whose reply this lap delivered and that carry no marker-less
    (human) reply are resolved — human participation is never auto-resolved.
    Behind ``resolve_addressed_threads_enabled`` for rollback. Re-fetch the
    thread nodes once so the just-posted terminal replies are visible;
    resolution is single-shot by design (see ``_resolvable_thread_ids``): a
    replied thread is dropped from all future routing, so a stale re-fetch
    missing the reply skips resolution and degrades that thread to the
    pre-091 status quo rather than resolving it blind. Own-PR scoping comes
    free from the enclosing remediation-lap guards (``delivered_ids``
    non-empty implies §4c-ii ran, so ``branch_name == pr_branch`` and
    ``remediation_pr_num`` are set). Per-thread best-effort exactly like
    §4c-ii: a host error logs and the lap continues.
    """
    if not (
        agent.config.resolve_addressed_threads_enabled
        and delivered_ids
        and push_delivered
        and remediation_pr_num is not None
    ):
        return
    resolvable = _resolvable_thread_ids(
        agent.evolution.code_host.get_pr_review_threads(
            remediation_pr_num,
        ),
        delivered_ids,
    )
    for thread_id in resolvable:
        try:
            agent.evolution.code_host.resolve_review_thread(
                thread_id,
            )
        except Exception:
            logger.warning(
                "Failed to resolve review thread %s on PR #%d; "
                "leaving it for the next remediation lap",
                thread_id, remediation_pr_num,
                exc_info=True,
            )


def observe_task_completion_state(
    agent: MaestroAgent,
    *,
    outcome: TaskOutcome,
    kept_branch: str | None,
    pr_branch: str | None,
    remediation_pr_num: int | None,
) -> tuple[bool | None, dict[str, object] | None]:
    """Observe what this lap's completion looked like — ``run``'s ``# 5a.`` +
    ``# 5a-ii.`` blocks, extracted verbatim.

    One stage answering one question from two *disjoint* domains, so the
    mutual exclusivity two adjacent ``if`` guards only happened to encode is
    now structural: the merge-readiness arm fires only for an owned task
    branch (``pr_branch is None``), the readiness-snapshot arm only for a
    remediation lap (``pr_branch is not None``). Both returns are pure sinks
    — they flow into the ``TaskRecord`` constructor (and, for
    ``merge_clean``, ``task_record.apply_merge_clean_promotion_directive`` after the
    record is persisted) and nowhere else — so this reads run-scoped values
    only, reassigns none of them, and touches no outcome machinery: a
    behaviour-preserving delegate, not a new stage.

    ``# 5a.`` — self-directed task branches rot while waiting for manual
    promotion campaigns, so probe the kept branch against ``origin/main`` at
    the moment it is freshest: a zero-SDK-cost ``git merge-tree`` with no
    working-tree side effects. The verdict rides the task record so future
    triage can see which branches were clean at completion; the clean-merge
    promotion directive is applied later (``# 5e.``). Scoped to genuine task
    branches — PR remediation branches are delivered to the PR by
    run()/Tend PR and merged onto ``main`` by Tend PR, so a ``source_task_id``
    promotion is meaningless for them. Only fires for a kept (committed)
    branch. ``merge_clean`` is clean-mergeability, NOT landing.

    ``# 5a-ii.`` — observe the PR's aggregate readiness at lap completion
    (proposal 089 step 4) so the consecutive-no-progress sensor can tell a
    lap that cleared a freshly-arrived blocker from one that ground in
    place. This PR's readiness memo is dropped *first* so the snapshot
    reflects the post-lap state, not a dequeue-time observation a gate may
    have memoized earlier in the iteration. ``None`` for non-remediation and
    branchless tasks. Gated on the progress-aware predicate (the dedicated
    flag or the salvage master flag, proposal 094) or the observed-readiness
    flag, so the observation's git/host I/O stays off the hot remediation
    path until a consumer is actually enabled — all off → inert.

    Precondition: *outcome* must be the **pre-bind** value, so this must stay
    ahead of ``apply_ci_red_read_bind`` / ``apply_mechanical_outcome_authority``,
    which may downgrade it — a post-bind ``outcome`` would silently stop
    probing a branch whose lap succeeded. And it must run after the lap's
    work, so the readiness observation is genuinely post-lap rather than a
    snapshot of the state the lap was handed.
    """
    merge_clean: bool | None = None
    if outcome == "success" and kept_branch and pr_branch is None:
        merge_clean = check_merge_clean(
            kept_branch, cwd=str(agent.config.workspace),
        )

    pr_readiness_snapshot_stamp: dict[str, object] | None = None
    if (
        pr_branch is not None
        and remediation_pr_num is not None
        and (
            agent.config.progress_aware_lap_budget_active
            or agent.config.observed_ready_standdown_enabled
        )
    ):
        agent._pr_readiness_memo.pop(remediation_pr_num, None)
        pr_readiness_snapshot_stamp = asdict(
            agent.observe_pr_readiness(remediation_pr_num, pr_branch),
        )

    return merge_clean, pr_readiness_snapshot_stamp

"""PR-remediation stamping stages carved out of ``maestro.agent`` (proposal 106).

Holds the four stages ``MaestroAgent.run`` executes between step execution and
artifact commit to bind a PR remediation's terminal word to what the host and
git actually show: the host-state stamp gate (``# 4c.``), the trusted
review-thread reply delivery (``# 4c-ii.``), the comment-completeness gate
(``# 4d.``), and the verdict-fetch floor plus record-time merge coercion taken
as one stage (``# 4e.`` / ``# 4f.``). They run as one contiguous straight-line
stretch of ``run``, they are each other's only readers of ``remediation_note``
and ``remediation_pr_num``, and ``run`` is their sole caller.

The extraction is behaviour-preserving: the same bodies, the same call order,
the same effects — only the receiver changed from ``self`` to a first positional
``agent`` argument, matching
:func:`maestro.loop.record_binds.apply_remediation_narration_framing` and
:func:`maestro.loop.lifecycle.teardown_task_run`. ``read_thread_reply_manifest`` and
the ``# 4d.`` selectors ``_eligible_bug_comments`` /
``_fetch_eligible_overflow_findings`` are imported from their defining modules;
tests patch them on this module.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, cast

from maestro.escalation_bridge import mark_comment_ids_serviced
from maestro.evolution_tend import _eligible_bug_comments, _fetch_eligible_overflow_findings
from maestro.loop.pr_remediation import _REMEDIATION_DEDUP_PREFIX, read_thread_reply_manifest
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    BLOCKER_OWNERSHIP_OWNED,
    MERGE_BLOCK_AWAITING_PUSH,
    classify_blocker_ownership,
    coerce_verdict_to_settled_on_merge,
    gate_remediation_verdict,
)
from maestro.vcs import CodeHost, VCSError

if TYPE_CHECKING:
    from pathlib import Path

    from maestro.agent import MaestroAgent
    from maestro.types import TaskOutcome

logger = logging.getLogger("maestro")


async def apply_host_state_stamp_gate(
    agent: MaestroAgent,
    *,
    outcome: TaskOutcome,
    pr_branch: str | None,
    goal: str,
    task_id: str,
) -> tuple[TaskOutcome, str | None, int | None]:
    """Stamp a PR remediation's terminal outcome against host merge state —
    the verbatim lift of ``run``'s ``# 4c.`` host-state stamp gate
    (proposal 106).

    A behaviour-preserving delegate: it reads every run-scoped value as a
    keyword param, reassigns none of the caller's locals, and returns the
    shaped ``(outcome, remediation_note, remediation_pr_num)`` tuple the
    enclosing scope reads back — ``remediation_note`` and
    ``remediation_pr_num`` are first-declared here and consumed by the
    downstream ``# 4c-ii.`` / ``# 4d.`` stages that already read them from
    ``run``'s scope. Its responsibility is composite: it records the
    action-zero fetch-truth marker (and, flag-gated, the obligation-fork
    marker) the later verdict-fetch gate depends on, then — when the
    outcome would otherwise stamp ``success`` — runs the two-arm stamp gate
    that forces ``partial`` when merge truth shows the resolution has not
    landed. Its existing instance side effects
    (``agent._last_remediation_merge_block`` / ``_source`` / ``_observed_at``,
    already ``__init__``-initialised and reset at the top of ``run()``) stay
    as plain writes.
    """
    # 4c. Host-state stamp gate for PR remediation. Remediation
    # commits reach the PR branch (run() pushes them directly at §5-push
    # below, with Tend PR as the fallback deliverer) but never ``main``
    # — so all-green steps do not mean the PR reached a mergeable/merged
    # host state. When a remediation would be stamped
    # ``success``, probe the host: if the resolution has not landed
    # (still CONFLICTING/behind/failing, or the commits are not yet on
    # ``origin/<branch>``), force ``partial`` and record the
    # deterministic merge-block reason so the stamp is honest and the
    # repetition gate can see a host no-op (#2457). External-request
    # remediations carry no acceptance_criteria, so 4b never fires for
    # them — this is the only terminal-state check on that path.
    remediation_note: str | None = None
    # PR number parsed once for the remediation gates below (4c/4d):
    # ``None`` when the goal is not a well-formed ``Remediate PR #N:``
    # marker, which short-circuits both gates exactly as a failed
    # per-gate match did before.
    remediation_pr_num: int | None = None
    if pr_branch is not None and goal.startswith(_REMEDIATION_DEDUP_PREFIX):
        rem_match = re.match(r"Remediate PR #(\d+):", goal)
        if rem_match:
            remediation_pr_num = int(rem_match.group(1))
        # Fetch-before-word action-zero: git fetch + --is-ancestor,
        # recording a per-task fetch-truth marker BEFORE any status
        # string is persisted so the verdict-fetch gate (4e) can prove
        # git spoke first. The first physical act on a Remediate goal is
        # the fetch — no status word may precede it.
        if remediation_pr_num is not None:
            agent._record_remediation_fetch_truth(
                task_id, remediation_pr_num, pr_branch,
            )
            # Indicting action-zero (PR #3211, flag-gated): beside the
            # blameless fetch-truth read above, record the HEAD-vs-
            # origin/<branch> obligation-fork marker so §4e's
            # gate_remediation_verdict can fork an owed push from an
            # awaited merge. The fetch just performed freshened the
            # remote ref this compare reads. Default off ⇒ never written.
            if agent.config.obligation_fork_floor_enabled:
                agent._record_remediation_obligation_fork(
                    task_id, remediation_pr_num, pr_branch,
                )
    if (
        outcome == "success"
        and pr_branch is not None
        and remediation_pr_num is not None
    ):
        pr_num = remediation_pr_num
        if agent.config.prestamp_merge_truth_gate_enabled:
            # Pre-stamp merge-truth gate (proposal 087 step 5). Refuse a
            # ``success`` stamp the single chokepoint cannot confirm
            # landed, at write time: any ``#N`` whose ``is_pr_settled``
            # reports unmerged is downgraded to ``partial``, regardless
            # of the host probe's mergeability read. This moves the catch
            # that audit rule 14 makes post-hoc to pre-stamp,
            # so the false success never
            # becomes load-bearing. The refusal is stamped with the
            # observation's provenance (``source`` / ``observed_at``),
            # threaded into ``_record_remediation_outcome`` for the
            # post-task tracker.
            truth = agent.is_pr_settled(pr_num)
            if not truth.branch_landed:
                outcome = "partial"
                agent._last_remediation_merge_block = (
                    MERGE_BLOCK_AWAITING_PUSH
                )
                agent._last_remediation_merge_source = truth.source
                agent._last_remediation_merge_observed_at = (
                    truth.observed_at
                )
                remediation_note = (
                    f"Resolved on branch, {Marker.AWAITING_TEND_PUSH} — "
                    f"PR #{pr_num} not yet landed per merge-truth "
                    f"(source={truth.source}, "
                    f"observed_at={truth.observed_at:.0f})."
                )
        else:
            landed_ok, block_reason = await agent._probe_pr_merge_block(
                pr_num, pr_branch,
            )
            # Git-merge-truth confirmation. The host probe can read a
            # branch as cleanly mergeable (``landed_ok``) while the PR's
            # SHA never reached ``main``. Unless the host itself reports
            # an explicit MERGED state, require git to show a commit
            # referencing ``#<pr>`` on ``origin/main`` before the
            # ``success`` stamp survives; without it the resolution lives
            # only on the branch, so force ``partial``. This anchors the
            # closure gate to the shared git-merge-truth oracle (#2457).
            if (
                landed_ok
                and not agent.evolution.code_host.is_pr_merged(pr_num)
                and not agent._git_pr_merged(pr_num)
            ):
                landed_ok = False
                block_reason = MERGE_BLOCK_AWAITING_PUSH
            if not landed_ok:
                outcome = "partial"
                recorded_block = agent._refine_ci_review_blocker(
                    pr_num, pr_branch, block_reason,
                )
                agent._last_remediation_merge_block = recorded_block
                remediation_note = (
                    f"Resolved on branch, {Marker.AWAITING_TEND_PUSH} — "
                    f"PR #{pr_num} not yet landed "
                    f"(merge blocker: {recorded_block})."
                )
    return outcome, remediation_note, remediation_pr_num


def deliver_review_thread_replies(
    agent: MaestroAgent,
    *,
    branch_name: str | None,
    pr_branch: str | None,
    remediation_pr_num: int | None,
    outcome: TaskOutcome,
    artifacts_path: Path,
) -> set[int]:
    """Deliver this lap's recorded review-thread replies — the verbatim
    lift of ``run``'s ``# 4c-ii.`` trusted reply-delivery step
    (proposal 106).

    A behaviour-preserving delegate: it reads every run-scoped value as a
    keyword param, reassigns none of the caller's locals, and returns the
    ``delivered_ids`` set — the comment ids whose reply POST returned
    without raising — that ``task_delivery.resolve_addressed_review_threads``
    consumes.
    Its two side effects (the per-reply ``reply_to_review_comment`` POSTs
    and, for the confirmed ids, the ``mark_comment_ids_serviced`` stamp)
    are unchanged; the early-return arm yields an empty set exactly where
    the inline guard used to skip the block, so the hoisted ``delivered_ids``
    declaration can disappear from ``run``.

    The loop is the single trusted deliverer of per-thread terminal
    replies: the remediation task RECORDS each terminal-marker reply into
    the ``thread-replies.jsonl`` manifest under its artifacts dir rather
    than posting it itself (a task-side POST would double-post the thread —
    the ``pulls/.../comments`` reply endpoint is action-gate-allowed since
    #2819, but the loop stays the sole deliverer). Deliver them here, the
    same shape as the §5-push commit delivery: a deterministic Python
    ``reply_to_review_comment`` host call. Placed BEFORE the §4d
    completeness gate so a lap that answered every thread sees its own
    markers and reports ``success`` this same lap (Tend stops re-routing).
    Scoped exactly like §5-push — a deliverable outcome on the PR branch we
    are on, with a parsed PR number. Per-reply best-effort: a host error
    logs loudly and leaves the undelivered marker for the next remediation
    lap to re-record and re-deliver (the §4d gate below still sees the
    thread as unanswered, forcing a partial that Tend re-routes), without
    crashing this lap. There is no Tend-side reply POST — Tend only
    re-routes a fresh lap; this is the sole reply deliverer.
    """
    if not (
        pr_branch is not None
        and branch_name == pr_branch
        and remediation_pr_num is not None
        and outcome in ("success", "partial")
    ):
        return set()
    delivered_ids: set[int] = set()
    # Comment ids whose reply POST returned without raising: only a
    # confirmed delivery marks a thread serviced (proposal 091). The
    # per-reply ``except`` below records no delivery, so a thread
    # whose reply raised stays eligible for the next tend lap (a
    # correct retry) instead of being muted forever by a serviced
    # stamp that fired before the reply ever posted.
    for comment_id, body in read_thread_reply_manifest(
        artifacts_path,
    ):
        try:
            agent.evolution.code_host.reply_to_review_comment(
                remediation_pr_num, comment_id, body,
            )
        except Exception:
            logger.warning(
                "Failed to deliver review-thread reply to "
                "comment %d on PR #%d; next remediation lap "
                "will re-record and re-deliver",
                comment_id, remediation_pr_num,
                exc_info=True,
            )
        else:
            delivered_ids.add(comment_id)
    # A review thread is called answered only after its reply is
    # confirmed delivered: stamp exactly the delivered ids serviced
    # under this PR's standdown. ``mark_comment_ids_serviced`` no-ops
    # when the PR has no standdown, so non-standdown PRs are
    # unaffected (proposal 091). Skip the call entirely when nothing
    # delivered — an empty stamp only re-acquires the ledger lock to
    # rewrite unchanged content.
    if delivered_ids:
        mark_comment_ids_serviced(
            agent.escalation_standdowns, remediation_pr_num,
            delivered_ids,
        )
    return delivered_ids


def items_outstanding_or_unverified(host: CodeHost, pr_number: int) -> bool:
    """Whether an open PR still has outstanding review items — fail-loud.

    The ``None``-count fallback of ``_record_remediation_outcome``: a failed
    host read is not "zero outstanding" (#4065), so an unverifiable scan
    reports ``True`` (a no-op lap) rather than crediting the lap.
    """
    try:
        thread_nodes = host.get_pr_review_threads_strict(pr_number)
    except VCSError:
        logger.warning(
            "PR #%s: no-op thread re-read failed", pr_number, exc_info=True,
        )
        return True
    overflow, overflow_ok = _fetch_eligible_overflow_findings(
        host, pr_number,
    )
    return (
        not overflow_ok
        or bool(_eligible_bug_comments(thread_nodes))
        or bool(overflow)
    )


def apply_comment_completeness_gate(
    agent: MaestroAgent,
    *,
    pr_branch: str | None,
    remediation_pr_num: int | None,
    outcome: TaskOutcome,
    remediation_note: str | None,
) -> tuple[TaskOutcome, str | None]:
    """Downgrade an incomplete PR remediation — the verbatim lift of
    ``run``'s ``# 4d.`` comment-completeness gate (proposal 106).

    A behaviour-preserving delegate: it reads every run-scoped value as a
    keyword param, reassigns none of the caller's locals, writes its one
    existing side effect (``agent._last_remediation_items_remaining``, reset
    at the top of ``run()``), and returns the shaped
    ``(outcome, remediation_note)`` the enclosing scope reads back.

    A remediation is routed with the whole list of unresolved review items
    and told to address every one in a single pass; one that left work
    behind has not finished it, even when it committed and the branch merely
    awaits the tend push. The gate re-reads the open PR's outstanding items
    once and forces ``partial`` with a count-bearing note when any remain,
    skipping a merged/closed PR (where a stand-down is legitimate) via the
    shared ``is_pr_settled`` chokepoint.
    """
    # 4d. Comment-completeness gate. A PR remediation is routed with the
    # whole list of unresolved review items and instructed to address
    # every one in a single pass; one that left work behind has not
    # finished it, even when it committed and the branch merely awaits
    # the tend push. Re-read the open PR's outstanding items once — both
    # inline review threads lacking a terminal reply (via
    # ``_eligible_bug_comments``) and body-overflow findings lacking an
    # acknowledgement (via ``_fetch_eligible_overflow_findings``, the
    # same selector the tend router re-routes on, so the gate and the
    # router never disagree). Record the combined count for
    # ``_record_remediation_outcome`` to reuse (the repetition gate's
    # no-op, #2663), and — when any remain — force ``partial`` with a
    # count-bearing note so an incomplete pass is distinguishable from a
    # complete-but-unpushed one. Skipped for merged/closed PRs, where a
    # stand-down is legitimate.
    if (
        pr_branch is not None
        and remediation_pr_num is not None
        and outcome in ("success", "partial")
    ):
        completeness_pr = remediation_pr_num
        # Skip for a settled PR (merged or closed) — a stand-down there
        # is legitimate. The open-vs-settled fork reads through the
        # shared ``is_pr_settled`` chokepoint (proposal 087), reusing the
        # per-iteration, provenance-stamped MergeTruth the dequeue gates
        # already observed rather than re-deriving merge state here.
        if not agent.is_pr_settled(completeness_pr).settled:
            # A failed host read must not read as "zero outstanding"
            # (#4065): leave the count ``None`` — "no count this lap", so
            # ``_record_remediation_outcome`` credits no progress — and
            # refuse to call the pass complete.
            threads_ok = True
            try:
                thread_nodes = (
                    agent.evolution.code_host.get_pr_review_threads_strict(
                        completeness_pr,
                    )
                )
            except VCSError:
                logger.warning(
                    "PR #%s: completeness thread scan failed",
                    completeness_pr, exc_info=True,
                )
                thread_nodes = []
                threads_ok = False
            remaining_threads = _eligible_bug_comments(
                thread_nodes,
            )
            # Skip the overflow fetch once the thread scan failed — the
            # verdict is already decided and the host just refused a read.
            remaining_overflow: list[Any] = []
            overflow_ok = True
            if threads_ok:
                remaining_overflow, overflow_ok = (
                    _fetch_eligible_overflow_findings(
                        agent.evolution.code_host, completeness_pr,
                    )
                )
            if not (threads_ok and overflow_ok):
                outcome = "partial"
                scan_note = (
                    f"PR #{completeness_pr}: outstanding-item scan did not "
                    "complete (host read failed); completeness unverified."
                )
                remediation_note = (
                    f"{remediation_note} {scan_note}"
                    if remediation_note is not None
                    else f"Resolved partially — {scan_note}"
                )
                return outcome, remediation_note
            agent._last_remediation_items_remaining = (
                len(remaining_threads) + len(remaining_overflow)
            )
            if remaining_threads or remaining_overflow:
                outcome = "partial"
                detail_parts: list[str] = []
                if remaining_threads:
                    detail_parts.append(
                        f"{len(remaining_threads)} inline review "
                        "thread(s) still lack a terminal reply marker",
                    )
                if remaining_overflow:
                    detail_parts.append(
                        f"{len(remaining_overflow)} overflow finding(s) "
                        "still lack an acknowledgement marker",
                    )
                items_note = (
                    f"PR #{completeness_pr}: "
                    + " and ".join(detail_parts) + "."
                )
                # Preserve any awaiting-push marker the stamp gate
                # (4c) already framed — the costume-signature gate
                # (``remediation_tracker``) and audit rules key on
                # that marker text in the summary, so augment rather
                # than clobber it; otherwise lead with the items
                # note.
                remediation_note = (
                    f"{remediation_note} {items_note}"
                    if remediation_note is not None
                    else f"Resolved partially — {items_note}"
                )
    return outcome, remediation_note


def bind_remediation_verdict_to_git_truth(
    agent: MaestroAgent,
    *,
    task_id: str,
    remediation_pr_num: int | None,
    outcome: TaskOutcome,
    remediation_note: str | None,
) -> tuple[TaskOutcome, str | None]:
    """Bind a PR remediation's record-time verdict to git's read — the
    verbatim lift of ``run``'s ``# 4e.`` verdict-fetch gate and ``# 4f.``
    record-time merge-truth binding, taken as one stage (proposal 106).

    A behaviour-preserving delegate: it reads every run-scoped value as a
    keyword param, reassigns none of the caller's locals, and returns the
    shaped ``(outcome, remediation_note)`` the enclosing scope reads back.

    The two lifted blocks carried a **byte-identical** guard —
    ``if remediation_pr_num is not None and remediation_note is not None:``
    — repeated verbatim in two adjacent ``if`` statements, and the
    repetition was not even load-bearing: ``gate_remediation_verdict``
    returns a ``str`` verdict, so once the first guard passes the second can
    never fail. The two guards encoded one precondition, collapsed here into
    a single early return.

    Their **order is load-bearing** and is preserved: the fetch floor
    (``# 4e.``) must run *before* the merge coercion (``# 4f.``) so a PR
    git-merge-truth shows merged records a clean settled verdict rather than
    a ``[fetch-missing]`` partial wearing a merged PR's name.
    """
    if remediation_pr_num is None or remediation_note is None:
        return outcome, remediation_note

    # 4e. Verdict-fetch gate — the enforcement floor under the
    # fetch-before-word value. A remediation outcome carrying an
    # awaiting-tend-push / CONFLICTING / AWAITING_PUSH narrative is
    # downgraded to a forced ``[fetch-missing]`` partial unless the 4c
    # action-zero recorded a fetch-truth marker for this task — making
    # the status word physically impossible to persist until git has
    # spoken. Blocks and downgrades; observes nothing.
    # Obligation-fork floor activation (PR #3211, flag-gated). Derive
    # owned_awaiting_push from this lap's recorded merge-block:
    # classify_blocker_ownership puts AWAITING_PUSH in my own court
    # (own_push=True), so the floor forks an owed push from an
    # awaited merge instead of letting the AWAITING_TEND_PUSH note
    # wear external deferral. Default off ⇒ owned_awaiting_push stays
    # False and the call is byte-identical to the pre-wiring path.
    owned_awaiting_push = (
        agent.config.obligation_fork_floor_enabled
        and classify_blocker_ownership(
            agent._last_remediation_merge_block or "",
            own_push=True,
        )
        == BLOCKER_OWNERSHIP_OWNED
    )
    gated_outcome, remediation_note = gate_remediation_verdict(
        task_id,
        remediation_note,
        outcome,
        agent.config.memory_dir,
        owned_awaiting_push=owned_awaiting_push,
        obligation_fork=agent.obligation_fork,
        fetch_truth=agent.fetch_truth,
    )
    outcome = cast("TaskOutcome", gated_outcome)

    # 4f. Record-time merge-truth binding — the inverse of the pre-stamp
    # downgrade gate (4c). A phantom not-landed / awaiting-tend-push
    # verdict is unwriteable on a PR git-merge-truth shows merged
    # (#2809/#2810): reading merged-state solely through the
    # per-iteration-memoized ``is_pr_settled`` chokepoint, a verdict for
    # a PR whose branch landed is coerced to a settled ``success``,
    # overwriting any stale not-landed stamp. Closes the phantom at the
    # record rather than relying on a post-hoc audit rule.
    coerced_outcome, remediation_note = (
        coerce_verdict_to_settled_on_merge(
            remediation_pr_num,
            remediation_note,
            outcome,
            is_pr_merged=(
                lambda n: agent.is_pr_settled(n).branch_landed
            ),
        )
    )
    outcome = cast("TaskOutcome", coerced_outcome)

    return outcome, remediation_note

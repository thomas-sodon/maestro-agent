"""PR tending for evolution PRs — the ``tend_evolution_prs`` cluster split out of :mod:`maestro.evolution` (proposal 147).

The moved ``EvolutionEngine`` methods live here as ``fn(engine, ...)`` functions;
the engine keeps one-line delegators so callers and ``patch.object`` targets are unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from maestro.audit_checks import (
    log_gate_activation,
)
from maestro.gate_observation import (
    Attribution,
    attribute,
    render_identities,
)
from maestro.memory import (
    EvolutionPlan,
    TaskRecord,
)
from maestro.pr_facts import (
    MergeTruth,
    PrStaleness,
    observe_pr_staleness,
)
from maestro.pr_owed_act import (
    OwedAct,
    check_runs_from_conclusion,
    classify_pr_owed_act,
    derive_owed_act_blocker,
)
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    COOLDOWN_SKIP_GATE,
    format_cooldown_skip_detail,
    pr_number_from_remediation_goal,
)
from maestro.vcs import (
    VCSError,
    evolution_branch,
)

if TYPE_CHECKING:
    from maestro.evolution import EvolutionEngine, QualityGateResult
    from maestro.vcs import CodeHost

logger = logging.getLogger("maestro")


#: Bounds for rendering the inherited base-failure identities into a
#: ``quality_gates_routed`` remediation summary when the post-merge tend arm
#: holds a merge on an ``INHERITED`` red (proposal 124, step 10): the routed
#: task's blocker names the base failures that actually held the PR rather than
#: the coarse two-word :meth:`~EvolutionEngine._summarize_failed_gates` label,
#: so it is generous — sized like ``agent._INHERITED_DEMOTE_REASON_*``.
_INHERITED_TEND_SUMMARY_IDENTITY_LIMIT = 50
_INHERITED_TEND_SUMMARY_IDENTITY_CHARS = 500

@dataclass
class TendPRsResult:
    """Summary of one pass of :meth:`EvolutionEngine.tend_evolution_prs`.

    The counts are additive across all plans tended in the pass; ``errors``
    carries any per-plan failure strings that did not short-circuit the
    whole pass.  The phase itself never raises — infrastructure errors
    (fetch, tamper check) return early with a single error recorded here.
    """

    tended: int = 0
    enumerated: bool = False
    """True once the open-PR listing succeeded — ``tended`` is then an
    authoritative count of open evolution PRs for this pass, even when
    later per-branch work appended to ``errors`` (reminder / merge /
    push failures all happen *after* counting).  The pre-enumeration
    short-circuits (tending disabled, fetch failure, tamper check,
    list-PR failure) leave this False, so their transient ``tended=0``
    never overwrites a valid count held by the loop.  A list-PR failure
    means any listing the host cannot confirm — the strict listing raises
    rather than returning ``{}`` — so an unconfirmable open set never
    prunes the PR ledger or reports PRs as settled.
    """
    merged_updated: int = 0
    conflicts_cooldown_skipped: int = 0
    stale_review_resumed: int = 0
    reminders_sent: int = 0
    ready_for_review_sent: int = 0
    """Count of PRs for which a "ready for re-review" Telegram ping fired this
    pass — Maestro stood down from tending after pushing a branch that is
    current with ``main`` with all review comments addressed (see
    :meth:`_send_pr_ready_for_review`).
    """
    orphans_repaired: int = 0
    skipped_for_review: int = 0
    deferred_for_ci: int = 0
    """Count of PRs where ``_tend_single_pr`` short-circuited because the
    head's CI rollup was still pending. The tend pass returns immediately
    without merging ``origin/main`` so the in-flight CI run isn't
    invalidated.
    """
    changes_requested_prs: list[
        tuple[int, str, str, str, str, str]
    ] = field(default_factory=list)
    """``(pr_number, pr_title, plan_id, pr_body, pr_url, head_ref)`` for PRs
    skipped due to non-stale CHANGES_REQUESTED. ``plan_id`` + ``pr_body``
    flow through into
    :meth:`MaestroAgent._route_pr_feedback_to_pending_requests` so the
    rendered remediation prompt embeds the originating
    :class:`EvolutionPlan` and the PR description verbatim. ``head_ref``
    is the PR's head branch — carried so the routing pass can thread it
    into the ``PendingPRRequest.pr_branch`` field on the FIFO entry it
    appends.
    """
    conflicts_routed: list[
        tuple[int, str, list[str], str, str, str, str]
    ] = field(default_factory=list)
    """``(pr_number, pr_title, conflicted_files, plan_id, pr_body, pr_url, head_ref)``
    for PRs with merge conflicts.

    Appended to ``SelfModel.pending_pr_requests`` by
    :meth:`MaestroAgent._route_pr_feedback_to_pending_requests` (in
    ``maestro/agent.py``) so ``_run_task_phase`` drains the FIFO on the
    next iteration and executes the remediation task — bypassing
    ``generate_task``'s cadence and idea-generation cost. The plan_id
    and PR body are snapshotted at routing time so the remediation
    prompt sees the plan even if it is later archived to cold storage.
    The trailing ``head_ref`` is the PR branch on which the remediation
    task will run — run() pushes it directly, Tend PR is the fallback.
    """
    quality_gates_routed: list[
        tuple[int, str, str, str, str, str, str]
    ] = field(default_factory=list)
    """``(pr_number, pr_title, gate_summary, plan_id, pr_body, pr_url, head_ref)``
    for PRs that broke quality gates after a clean main-merge, plus PRs
    whose head CI rollup was already failing before the tend pass merged
    main.

    Appended to ``pending_pr_requests`` the same way.
    """
    bug_comments_routed: list[
        tuple[int, str, list[tuple[int, str, str, str]], str, str, str, str]
    ] = field(default_factory=list)
    """``(pr_number, pr_title, bug_comments, plan_id, pr_body, pr_url, head_ref)``
    for PRs carrying unresolved AI review comments or human threads.
    ``bug_comments`` is a list of ``(comment_id, path, title, comment_type)``
    from :func:`_eligible_bug_comments`; ``comment_type`` is one of ``"bug"``,
    ``"suggestion"``, ``"nit"``, or ``"human"``.

    Detected independently of merge state (a mergeable PR can still carry an
    unresolved bug comment — the reviewer's advisory check is excluded from the
    CI rollup), then appended to ``pending_pr_requests`` the same way. The
    branch is unchanged at routing time, so — like ``ci_failure_on_head`` — this
    route does NOT reset the per-PR remediation no-op counter.
    """
    reply_completion_routed: list[
        tuple[int, str, list[tuple[int, str, str, str]], str, str, str, str]
    ] = field(default_factory=list)
    """``(pr_number, pr_title, comments, plan_id, pr_body, pr_url, head_ref)``
    for PRs carrying unresolved, un-replied review threads GitHub flagged
    ``isOutdated`` (#2893). Same tuple shape as ``bug_comments_routed``;
    ``comments`` is the :func:`_reply_needed_threads`-minus-:func:`_eligible_bug_comments`
    delta — exactly the outdated + unresolved + un-replied threads that fix
    routing deliberately drops. Consumed by ``route_feedback`` only under an
    active lap-budget standdown, to route one bounded reply-completion lap that
    posts each thread its terminal marker (never re-doing fix work). Reads
    GitHub state only, so — like ``bug_comments_routed`` — this route does NOT
    reset the per-PR remediation no-op counter.
    """
    bug_overflow_routed: list[
        tuple[int, str, list[OverflowBugFinding], str, str, str, str]
    ] = field(default_factory=list)
    """``(pr_number, pr_title, findings, plan_id, pr_body, pr_url, head_ref)``
    for PRs whose latest AI review body carries unaddressed overflow findings
    of any AI severity (``[bug]``/``[suggestion]``/``[nit]``, proposal 064).
    ``findings`` come from :func:`_eligible_overflow_bug_findings`. Folded into the same remediation
    entry as ``bug_comments_routed`` by
    ``_route_pr_feedback_to_pending_requests``; like that channel, detection
    reads GitHub state only and does NOT reset the per-PR remediation no-op
    counter.
    """
    settled_pr_numbers: list[int] = field(default_factory=list)
    """PR numbers that left the open evolution-PR set this lap — the per-PR
    ledger entries :meth:`EvolutionEngine.tend_evolution_prs` pruned because
    their PRs are no longer open upstream. On-settlement record reconciliation
    (proposal 103) drive candidates: consumed by
    :meth:`MaestroAgent._reconcile_settled_tend_prs`, which re-gates each on the
    merge-truth chokepoint so a merely-transient disappearance is a no-op. The
    prune already runs every tend pass, so this reuses it with no new host call.
    """
    owed_acts_observed: dict[int, str] = field(default_factory=dict)
    """``{pr_number: OwedAct.value}`` measured for each open own PR this lap
    (proposal 135) — the same carry-a-measured-fact-out shape
    ``settled_pr_numbers`` has. Populated unconditionally (proposal 137): the
    *release* path keeps its own ``[loop] refutable_standdown_enabled`` guard at
    its call site, so gating the measurement too only left this input missing
    wherever the canary is not deployed.

    Composed from the per-PR
    :meth:`EvolutionEngine._observe_pr_staleness` reading, the host ``pr`` fields
    in hand, and the rollup conclusion the walk resolves once per PR per lap
    (proposal 137) — so a consumer reads the live owed act with **no** extra
    host, merge-oracle or SDK call of its own. Feeding the real conclusion is
    what lets a concluded-red rollup read ``code-owed`` rather than the blind
    ``wait-ci``; an unreadable or in-flight rollup still reads ``WAIT_CI``, which
    the validity oracle reads as holding, so the fail-safe direction
    :mod:`maestro.standdown_validity` documents as safe is preserved — a missed
    refutation costs latency, a spurious one would release a latch doing its job.

    Consumed by ``PrRemediationController.route_feedback``'s
    :func:`~maestro.escalation_bridge.prune_refuted_standdowns` call; a PR absent
    here (unmeasured — the walk short-circuited before the observation) keeps its
    latch for that pass.
    """
    owned_blockers_observed: dict[int, str] = field(default_factory=dict)
    """``{pr_number: merge-block value}`` measured alongside
    ``owed_acts_observed`` — the ``classify_merge_block`` rung
    (:data:`~maestro.remediation_tracker.MERGE_BLOCK_CONFLICTING` and friends)
    :meth:`_observe_owed_act` derives on its way to the owed act
    and used to discard (proposal 135).

    Populated unconditionally at the same call site, from the same
    already-measured facts — so it costs nothing extra. It gives the refuted-release withdrawal
    message the *concrete reason* the latched claim no longer holds
    (``CONFLICTING``) rather than only the owed-act value, so the retraction
    Thomas reads names the thing that changed.
    """
    errors: list[str] = field(default_factory=list)


# Tapered reminder schedule — hours since the most recent notable event on
# the PR (``updated_at`` or the last reminder we sent) before sending the
# next reminder.  Chosen to nudge Thomas at escalating cadence without
# spamming:  48h after the PR goes quiet, then 72h after that reminder,
# then weekly.
_PR_REMINDER_HOURS = (48.0, 72.0, 168.0)


# Task-history lookback for ``_has_unconsumed_awaiting_push_verdict`` — how many
# recent records to scan for the latest dedicated ``Remediate PR #N`` lap whose
# verdict deferred its push to tend (#2825). Matches the 30-record window the
# sibling remediation-lineage scans use (``_pending_pr_head_stalled``).
_AWAITING_PUSH_VERDICT_LOOKBACK = 30


# Sentinel prefix for the ``gate_summary`` slot of a ``quality_gates_routed``
# entry that took the *pre-merge* "head CI already failing" route in
# ``_tend_single_pr``. That route returns before any ``origin/main`` merge, so
# the branch state is unchanged — the agent-side reset gate must distinguish it
# from a post-merge quality-gate failure or it would clear the per-PR no-op
# counter every tend cycle and defeat the repetition gate (#2440). The optional
# ``": <names>"`` suffix carries the real failing check name(s) so the
# remediation directive names the actual breakage instead of ruff/mypy/tests.
CI_FAILURE_ON_HEAD_SUMMARY = "ci_failure_on_head"


def is_ci_failure_on_head_summary(gate_summary: str) -> bool:
    """Return ``True`` when *gate_summary* is the pre-merge head-CI-failure route.

    Matches both the bare sentinel and the ``"<sentinel>: <names>"`` form that
    Fix 4 threads the failing check name(s) into (#2440).
    """
    return gate_summary == CI_FAILURE_ON_HEAD_SUMMARY or gate_summary.startswith(
        f"{CI_FAILURE_ON_HEAD_SUMMARY}:",
    )


def _pr_needs_conflict_resolution(merge_state_status: str, mergeable: str) -> bool:
    """Return ``True`` when a PR's host-reported state implies a main conflict.

    ``merge_state_status`` (``DIRTY``/``BLOCKED``/``UNSTABLE``) and ``mergeable``
    (``CONFLICTING``) are GitHub's computed signals. Used both to pre-filter
    candidates in ``tend_evolution_prs`` and — post Fix 2 (#2440) — to keep a
    conflicting PR out of the pre-merge head-CI-failure short-circuit so it
    still reaches conflict resolution.
    """
    status = (merge_state_status or "").upper()
    mrg = (mergeable or "").upper()
    return status in ("DIRTY", "BLOCKED", "UNSTABLE") or mrg == "CONFLICTING"


# Matches an AI reviewer's *bug* comment: a 🐛 prefix followed by a bold
# title — the severity ``maestro-review`` fails the build on.  The emoji and
# bold-title shape come from the reviewer's posted-comment format; ``maestro``
# may not import ``maestro_review`` (strict package separation), so the
# convention is pinned by ``tests/test_tend_bug_comments.py``.
_AI_BUG_COMMENT_RE = re.compile(r"^\U0001f41b\s+\*\*(.+?)\*\*")

# Reviewer-emitted icon prefixes for suggestion (💡) and nit (✏️) findings.
# Duplicated from ``maestro_review.review`` (``maestro`` may not import it);
# kept in sync by ``tests/test_tend_bug_comments.py``.
_AI_SUGGESTION_COMMENT_RE = re.compile(r"^\U0001f4a1\s+\*\*(.+?)\*\*")
_AI_NIT_COMMENT_RE = re.compile(r"^✏️\s+\*\*(.+?)\*\*")

# Reply markers that mean a thread already received a final verdict from the
# comment-remediation workflow — re-flagging it would loop.  This list is the
# authoritative core-side set for the autonomous path; ``maestro`` does not
# import ``maestro_review``.  It recognises every marker the retired standalone
# comment processor emitted ("Could not prove this defect"/"bug", "Fixed
# automatically", "Fixed locally with implementation-plan context.", "Tracked
# in #"), so threads closed under the old agent are never re-routed.
_BUG_COMMENT_TERMINAL_MARKERS: tuple[str, ...] = (
    "Could not prove this defect",
    "Could not prove this bug",
    "Fixed automatically",
    "Fixed locally with implementation-plan context.",
    "Tracked in #",
)

# Additional terminal markers emitted by the comment-remediation workflow for
# suggestion, nit, and human threads — a reply carrying any of these closes
# the thread without re-routing it.
_NON_BUG_COMMENT_TERMINAL_MARKERS: tuple[str, ...] = (
    "Declined with implementation-plan context.",
    "Answered with implementation-plan context.",
)

# Union of all terminal marker sets — used when checking reply terminals for
# non-bug threads and for the overflow acknowledgement comment.
_ALL_COMMENT_TERMINAL_MARKERS: tuple[str, ...] = (
    *_BUG_COMMENT_TERMINAL_MARKERS,
    *_NON_BUG_COMMENT_TERMINAL_MARKERS,
)


def _select_review_threads(
    nodes: list[dict[str, Any]], *, include_outdated: bool,
) -> list[tuple[int, str, str, str]]:
    """Select unresolved AI/human review threads needing loop attention.

    Shared body behind :func:`_eligible_bug_comments` (fix routing) and
    :func:`_reply_needed_threads` (reply-completion routing, #2893).  *nodes*
    are the raw GraphQL review-thread dicts returned by
    :meth:`CodeHost.get_pr_review_threads`.  A thread is selected when it is
    unresolved, its root comment is an AI finding (🐛 bug, 💡 suggestion,
    ✏️ nit) or a human comment (non-bot author), and no reply already carries a
    terminal marker for that comment type.  *include_outdated* is the only axis
    that differs between the two callers: fix routing excludes ``isOutdated``
    threads (the flagged code already changed, so re-routing a fix lap would
    spin the treadmill), while reply-completion includes them (an outdated
    thread that never got a terminal reply still needs one so it stops counting
    as outstanding).  Returns ``(comment_id, path, title, comment_type)`` per
    selected thread in thread order — computed at zero SDK cost so the tend
    phase can route a remediation task.  ``comment_type`` is one of ``"bug"``,
    ``"suggestion"``, ``"nit"``, or ``"human"``.
    """
    eligible: list[tuple[int, str, str, str]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("isResolved"):
            continue
        if node.get("isOutdated") and not include_outdated:
            continue
        comments = (node.get("comments") or {}).get("nodes") or []
        comments = [c for c in comments if isinstance(c, dict)]
        if not comments:
            continue
        root = comments[0]
        body = root.get("body") or ""

        bug_match = _AI_BUG_COMMENT_RE.match(body)
        if bug_match:
            comment_type = "bug"
            title = bug_match.group(1)
            terminal_markers = _BUG_COMMENT_TERMINAL_MARKERS
        else:
            sug_match = _AI_SUGGESTION_COMMENT_RE.match(body)
            if sug_match:
                comment_type = "suggestion"
                title = sug_match.group(1)
                terminal_markers = _ALL_COMMENT_TERMINAL_MARKERS
            else:
                nit_match = _AI_NIT_COMMENT_RE.match(body)
                if nit_match:
                    comment_type = "nit"
                    title = nit_match.group(1)
                    terminal_markers = _ALL_COMMENT_TERMINAL_MARKERS
                else:
                    # Not an AI finding — treat as human if author is not a bot.
                    author_login = (
                        (root.get("author") or {}).get("login") or ""
                    )
                    if author_login.endswith("[bot]"):
                        continue
                    comment_type = "human"
                    title = body.split("\n")[0][:100].strip()
                    terminal_markers = _ALL_COMMENT_TERMINAL_MARKERS

        reply_bodies = [c.get("body") or "" for c in comments[1:]]
        if any(
            marker in reply
            for reply in reply_bodies
            for marker in terminal_markers
        ):
            continue
        comment_id = root.get("databaseId")
        if not isinstance(comment_id, int):
            continue
        eligible.append((comment_id, root.get("path") or "", title, comment_type))
    return eligible


def _eligible_bug_comments(
    nodes: list[dict[str, Any]],
) -> list[tuple[int, str, str, str]]:
    """Select unresolved, non-outdated AI/human review threads for fix routing.

    Thin wrapper over :func:`_select_review_threads` with
    ``include_outdated=False`` — the fix-routing selector.  Excluding outdated
    threads keeps stale findings (whose anchored code already changed) from
    re-opening a remediation lap (the treadmill invariant).  See
    :func:`_select_review_threads` for the full selection contract and the
    ``(comment_id, path, title, comment_type)`` tuple shape.
    """
    return _select_review_threads(nodes, include_outdated=False)


def count_eligible_threads_fail_safe(code_host: CodeHost, pr_number: int) -> int:
    """Count eligible review threads, reading a failed scan as one blocker.

    The best-effort ``get_pr_review_threads`` returns ``[]`` on a
    :class:`VCSError`, so a failed scan would read as zero unresolved
    threads and let an unscanned PR be observed ready (#4308, #4387). Read
    through the fail-loud strict reader instead and surface one blocker on
    failure — the same fail-safe shape the readiness overflow oracle uses.
    """
    try:
        threads = code_host.get_pr_review_threads_strict(pr_number)
    except VCSError:
        logger.warning(
            "PR #%d review-thread scan failed; counting it as one blocker",
            pr_number, exc_info=True,
        )
        return 1
    return len(_eligible_bug_comments(threads))


def _reply_needed_threads(
    nodes: list[dict[str, Any]],
) -> list[tuple[int, str, str, str]]:
    """Select unresolved, un-replied review threads — outdated ones included (#2893).

    Thin wrapper over :func:`_select_review_threads` with
    ``include_outdated=True`` — the reply-completion selector.  Its result minus
    :func:`_eligible_bug_comments` is exactly the outdated, unresolved,
    un-replied threads: threads that will never receive a fix lap yet still need
    a terminal reply marker so they stop counting as outstanding (and, under a
    merge-or-close standdown, stop freezing the PR).  Same tuple shape as
    :func:`_select_review_threads`.
    """
    return _select_review_threads(nodes, include_outdated=True)


def _resolvable_thread_ids(
    nodes: list[dict[str, Any]], delivered_ids: set[int],
) -> list[str]:
    """Select GraphQL ids of AI-finding threads safe to auto-resolve (proposal 091).

    A thread qualifies for loop-side resolution only when ALL of the following
    hold:

    * it is not already ``isResolved``;
    * its root comment matches an AI-finding pattern (🐛 bug, 💡 suggestion,
      ✏️ nit) — human-rooted threads are **never** resolved (author-login checks
      cannot distinguish Maestro's token identity from Thomas's, so
      classification stays body-based like :func:`_select_review_threads`);
    * the root ``databaseId`` is in *delivered_ids* — its terminal reply was
      confirmed delivered on this remediation lap; and
    * it carries at least one non-root comment and **every** one of them carries
      a terminal marker — a marker-less reply means a human joined the
      discussion, so the thread is left untouched.

    Requiring a visible non-root reply is a deliberate, single-shot guard, not
    an oversight.  Resolution runs exactly once — right after this lap's reply
    is posted — because a terminal-marker reply drops the thread from all future
    routing (:func:`_select_review_threads`), so no later lap re-attempts it.  If
    the same-lap re-fetch is stale and does not yet show the just-posted reply,
    the thread is skipped rather than resolved blind: the strict
    ``never-human-participation`` guarantee (we can only detect a human reply
    from a fresh read) takes precedence over guaranteeing resolution, so such a
    thread degrades to the pre-091 status quo (left for manual resolution).  This
    trades a rare missed auto-resolution for never resolving a thread whose
    current state we cannot read.

    Returns the GraphQL thread node ``id`` strings in thread order, at zero SDK
    cost.  Reuses the AI-finding vocabulary and terminal-marker set of
    :func:`_select_review_threads` rather than inventing a second one.
    """
    resolvable: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("isResolved"):
            continue
        thread_id = node.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            continue
        comments = (node.get("comments") or {}).get("nodes") or []
        comments = [c for c in comments if isinstance(c, dict)]
        if not comments:
            continue
        root = comments[0]
        body = root.get("body") or ""
        is_ai_finding = (
            _AI_BUG_COMMENT_RE.match(body)
            or _AI_SUGGESTION_COMMENT_RE.match(body)
            or _AI_NIT_COMMENT_RE.match(body)
        )
        if not is_ai_finding:
            continue
        comment_id = root.get("databaseId")
        if not isinstance(comment_id, int) or comment_id not in delivered_ids:
            continue
        reply_bodies = [c.get("body") or "" for c in comments[1:]]
        if not reply_bodies:
            continue
        if not all(
            any(marker in reply for marker in _ALL_COMMENT_TERMINAL_MARKERS)
            for reply in reply_bodies
        ):
            continue
        resolvable.append(thread_id)
    return resolvable


# Stable delimiter pair wrapping the overflow-findings list in an AI review
# body.  Overflow findings have no inline thread, so the autonomous tend phase
# and the human-paced comment-remediation surface parse the bug-severity lines out of the body between
# these sentinels.  Duplicated from ``maestro_review.review`` (``maestro`` may
# not import ``maestro_review`` — strict package separation) and kept in sync by
# ``tests/test_tend_bug_comments.py`` (proposal 064).
_OVERFLOW_SENTINEL_OPEN = "<!-- maestro:overflow -->"
_OVERFLOW_SENTINEL_CLOSE = "<!-- /maestro:overflow -->"

# Legacy header that preceded the overflow list before the sentinel pair landed
# (proposal 064 Step 1).  An interrupted upgrade can leave a review body that
# carries the header but no sentinel; the parser falls back to it so detection
# is never lost.  Kept in sync with ``review.py`` by the cross-package test.
_OVERFLOW_LEGACY_HEADER = "**Additional findings** (could not be placed inline):"

# Roll-up acknowledgement comment header.  An overflow finding has no thread to
# reply to or resolve, so terminal verdicts are recorded as ``of-<hash> —
# <marker>`` lines under this sentinel on a single PR comment.  A fingerprint
# listed here against any :data:`_BUG_COMMENT_TERMINAL_MARKERS` phrase is
# treated as addressed and dropped from the eligible set.
_OVERFLOW_ACK_SENTINEL = "<!-- maestro:overflow-ack -->"

# Filler words stripped from a finding title before fingerprinting.  Mirrors
# ``maestro_review.review._TITLE_STOPWORDS``; kept in sync by the cross-package
# stopwords test so a recomputed legacy fingerprint matches the reviewer's
# embedded handle for the same finding.
_OVERFLOW_TITLE_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "of", "in", "on", "to", "for",
    "with", "and", "or",
    "detected", "found", "missing", "potential", "possible",
})

# Parses one rendered overflow line:
#   ``- [of-<hash> ]**<file>:<line>** [<severity>] — <title>[: <description>]``
# The ``of-<hash>`` handle is present in the hardened block and absent in the
# legacy block (recomputed via :func:`_overflow_fingerprint` when missing).
_OVERFLOW_LINE_RE = re.compile(
    r"^-\s+"
    r"(?:(of-[0-9a-f]{8})\s+)?"
    r"\*\*(.+?):(-?\d+)\*\*\s+"
    r"\[([a-z]+)\]\s*"
    r"—\s*"
    r"(.+?)"
    # Title/description boundary is the reviewer's ``": "`` (colon + space); a
    # bare ``A:B`` colon stays in the title.  A lone trailing colon (the stripped
    # ``"Title: "`` empty-description form) closes the title without a description.
    r"(?::\s+(.*)|:)?$"
)

# Matches an ``of-<hash>`` handle anywhere in an acknowledgement-comment line.
_OVERFLOW_ACK_LINE_RE = re.compile(r"of-[0-9a-f]{8}")


@dataclass(frozen=True)
class OverflowBugFinding:
    """An AI reviewer finding the reviewer could not place inline.

    Parsed from the sentinel-delimited overflow block in an AI review body.
    ``fingerprint`` is the stable ``of-<hash8>`` handle consumers reference when
    remediating and acknowledging — taken verbatim from the hardened block or
    recomputed from the parsed fields for a legacy line.  ``severity`` is one of
    ``"bug"``, ``"suggestion"``, or ``"nit"``; defaults to ``"bug"`` for
    backward compatibility with code that constructs the object without it.
    """

    fingerprint: str
    file: str
    line: int
    title: str
    description: str
    severity: str = "bug"


def _normalize_overflow_title(title: str) -> frozenset[str]:
    """Return the significant lowercase tokens in *title*.

    Mirrors ``maestro_review.review._normalize_title`` so a recomputed legacy
    fingerprint matches the reviewer's embedded handle for the same finding.
    """
    cleaned = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return frozenset(
        token
        for token in cleaned.split()
        if len(token) >= 2 and token not in _OVERFLOW_TITLE_STOPWORDS
    )


def _overflow_fingerprint(severity: str, file: str, line: int, title: str) -> str:
    """Return the ``of-<hash8>`` handle for an overflow finding's identity.

    Mirrors ``maestro_review.review._overflow_fingerprint`` (which takes a
    ``ReviewFinding``); derives the same digest from ``severity|file|line``
    when *line* is a real diff line, so a legacy overflow line without an
    embedded handle dedupes against a hardened one.  *title* is then excluded
    from the digest — it is LLM-authored prose the reviewer re-words on nearly
    every run, so folding it in minted a fresh handle per review and no
    acknowledgement could ever stick (issue #3326).

    When ``line <= 0`` the site alone is not an identity — every file-level
    finding of one severity in that file would collapse to a single handle, so
    acking one would suppress the rest — and the sorted normalized *title* is
    folded back in, making the handle identical to
    :func:`_legacy_overflow_fingerprint`.

    Raises ``ValueError`` when *severity* or *file* is empty: the identity is
    then not re-derivable, so the handle could never be acked — fail loudly at
    mint time rather than emit it.
    """
    if not severity or not file:
        raise ValueError(
            "overflow fingerprint needs a non-empty severity and file; "
            f"got severity={severity!r} file={file!r}",
        )
    if line <= 0:
        return _legacy_overflow_fingerprint(severity, file, line, title)
    raw = f"{severity}|{file}|{line}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"of-{digest}"


def _legacy_overflow_fingerprint(
    severity: str, file: str, line: int, title: str,
) -> str:
    """Return the pre-#3326 ``severity|file|line|sorted-normalized-title`` handle.

    Retained only so an acknowledgement posted before the identity moved still
    matches the finding it answered; nothing emits this handle any more.
    """
    tokens = " ".join(sorted(_normalize_overflow_title(title)))
    raw = f"{severity}|{file}|{line}|{tokens}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"of-{digest}"


def _acking_overflow_handles(finding: OverflowBugFinding) -> set[str]:
    """Return every handle that may legitimately acknowledge *finding*.

    The finding's own embedded handle, the canonical coarse
    ``severity|file|line`` handle, and the legacy title-derived one, all
    recomputed from the parsed block line's own fields — so an ack posted under
    any of those keys still answers the finding.

    The canonical handle is required in its own right: a finding parsed out of a
    pre-#3326 block carries the *title*-derived handle as its ``fingerprint``,
    so without it the set would be the legacy key twice and an ack recorded
    under the coarse handle would leave the legacy-rendered copy unfiltered — it
    then merges back under the canonical key in
    :func:`_fetch_eligible_overflow_findings` (which ack-filters per block,
    before the union) and re-routes an already-answered finding.

    Same-site collapse is deliberate and *asymmetric with the union*: for
    ``line > 0`` two genuinely-different findings at one ``severity|file|line``
    share a handle, so :func:`_fetch_eligible_overflow_findings` keeps both as
    separate entries while an ack for either handle answers both.  That is
    intentional — the handle must stay derivable from facts the reviewer
    re-derives identically on every run, and folding the title back in only when
    a sibling finding happens to share the site would make one finding's handle
    depend on whether the *other* was emitted that review, flipping it between
    the coarse and title-derived keys and resurrecting the #3326 treadmill the
    coarse handle exists to end.  So distinct same-site findings cannot be
    acknowledged independently; the union still preserves both because dropping
    one loses it even from an *unacked* audit, which is the strictly worse
    failure.
    """
    return {
        finding.fingerprint,
        _overflow_merge_key(finding),
        _legacy_overflow_fingerprint(
            finding.severity, finding.file, finding.line, finding.title,
        ),
    }


def _overflow_merge_key(finding: OverflowBugFinding) -> str:
    """Return the canonical cross-block dedupe key for a *parsed* finding.

    Mirrors ``maestro_review.review._overflow_merge_key``.  The union merge must
    not key on the raw embedded ``of-<hash>`` handle: a block rendered before
    #3326 embedded the *title*-derived handle, re-minted on every reworded
    review, so one still-open finding carries a different handle in each
    historical block and would survive the union once per legacy review instead
    of collapsing to a single entry — minting duplicate remediation tasks for
    one finding.  Recomputing the coarse ``severity|file|line`` handle
    canonicalizes every such rendering onto one key.

    A file-level finding (``line <= 0``) has no coarse identity — there the
    handle *is* the title-derived key — so its own fingerprint is the key,
    keeping two distinct file-level findings apart.
    """
    if finding.line <= 0:
        return finding.fingerprint
    return _overflow_fingerprint(
        finding.severity, finding.file, finding.line, finding.title,
    )


def _extract_overflow_block(review_body: str) -> str | None:
    """Return the raw overflow-list text from a review *body*, or ``None``.

    Prefers the sentinel-delimited block; falls back to the legacy
    ``**Additional findings**`` header, collecting the bullet lines up to the
    next blank-line section boundary.
    """
    if not review_body:
        return None
    open_idx = review_body.find(_OVERFLOW_SENTINEL_OPEN)
    if open_idx != -1:
        close_idx = review_body.find(_OVERFLOW_SENTINEL_CLOSE, open_idx)
        if close_idx != -1:
            return review_body[open_idx + len(_OVERFLOW_SENTINEL_OPEN):close_idx]
    header_idx = review_body.find(_OVERFLOW_LEGACY_HEADER)
    if header_idx == -1:
        return None
    rest = review_body[header_idx + len(_OVERFLOW_LEGACY_HEADER):]
    chunk = rest.split("\n\n", 1)[0]
    lines = [ln for ln in chunk.splitlines() if ln.strip().startswith("- ")]
    return "\n".join(lines) if lines else None


def _parse_overflow_line(line: str) -> OverflowBugFinding | None:
    """Parse one rendered overflow *line* into an :class:`OverflowBugFinding`.

    Returns ``None`` for a non-matching line or an unrecognised severity.
    Keeps ``bug``, ``suggestion``, and ``nit`` findings; uses the embedded
    ``of-<hash>`` handle when present, otherwise recomputes it.
    """
    match = _OVERFLOW_LINE_RE.match(line.strip())
    if match is None:
        return None
    handle, file, line_str, severity, title, description = match.groups()
    if severity not in ("bug", "suggestion", "nit"):
        return None
    title = title.strip()
    line_no = int(line_str)
    fingerprint = handle or _overflow_fingerprint(severity, file, line_no, title)
    return OverflowBugFinding(
        fingerprint=fingerprint,
        file=file,
        line=line_no,
        title=title,
        description=(description or "").strip(),
        severity=severity,
    )


def _acknowledged_overflow_fingerprints(ack_body: str) -> set[str]:
    """Return the ``of-<hash>`` handles marked terminal in *ack_body*.

    A line carrying an ``of-<hash>`` handle and any
    :data:`_ALL_COMMENT_TERMINAL_MARKERS` phrase (covering bug, suggestion,
    nit, and human verdicts) is an addressed finding.
    """
    addressed: set[str] = set()
    if not ack_body:
        return addressed
    for line in ack_body.splitlines():
        if not any(marker in line for marker in _ALL_COMMENT_TERMINAL_MARKERS):
            continue
        match = _OVERFLOW_ACK_LINE_RE.search(line)
        if match is not None:
            addressed.add(match.group(0))
    return addressed


def _eligible_overflow_bug_findings(
    review_body: str,
    ack_body: str = "",
) -> list[OverflowBugFinding]:
    """Select unaddressed overflow findings from a review *body*.

    Mirrors :func:`_eligible_bug_comments` for the body-text overflow path:
    parse the sentinel-delimited block (legacy-header fallback), keep
    ``bug``, ``suggestion``, and ``nit`` severity lines, and drop any whose
    fingerprint already carries a terminal marker on the roll-up
    acknowledgement comment (*ack_body*).  A finding is acknowledged under
    *either* of :func:`_acking_overflow_handles` — the current coarse handle or
    the legacy title-derived one — so acks posted before the identity moved
    (issue #3326) keep suppressing their finding.  Computed at zero SDK cost so
    the tend phase can route a remediation task.
    """
    block = _extract_overflow_block(review_body)
    if block is None:
        return []
    acknowledged = _acknowledged_overflow_fingerprints(ack_body)
    eligible: list[OverflowBugFinding] = []
    for line in block.splitlines():
        finding = _parse_overflow_line(line)
        if finding is None:
            continue
        if _acking_overflow_handles(finding) & acknowledged:
            continue
        eligible.append(finding)
    return eligible


def _fetch_eligible_overflow_findings(
    host: CodeHost, pr_number: int,
) -> tuple[list[OverflowBugFinding], bool]:
    """Fetch and select a PR's still-unaddressed overflow findings.

    Unions the overflow block across *every* review body carrying one (the
    reviewer emits a non-cumulative, per-commit block, so a finding that
    appeared only in a superseded middle review is still open — issue #3326),
    fetches the roll-up acknowledgement comments, and returns the findings of
    every severity not yet acknowledged via
    :func:`_eligible_overflow_bug_findings`.  Terminal markers are likewise
    unioned across every sentinel-carrying comment body, matching
    ``maestro_review.review._parse_overflow_ack``.
    The acknowledgement comments are fetched only when an overflow block is
    actually present; a host read error on either side returns no findings
    rather than degrading, since acting without the acknowledgement state could
    treat already-addressed findings as outstanding.

    Returns ``(findings, scan_ok)``. ``scan_ok`` is ``True`` when the scan
    completed — including the "no overflow block" and "nothing unaddressed"
    cases — and ``False`` only when a host fetch (review bodies or the
    acknowledgement comment) errored, so the ready-for-review confirmation gate
    can tell a clean scan from a failed one rather than reading an errored
    fetch as "zero outstanding".

    Shared by the tend router (:meth:`_route_overflow_findings`,
    which re-dispatches the findings) and the post-run completeness gate
    (``run_task`` §4d, which downgrades a remediation that left them); both
    agree exactly on what "unaddressed overflow" means — the same lockstep the
    thread path gets from reusing :func:`_eligible_bug_comments`.
    """
    try:
        review_bodies = host.get_pr_review_bodies_strict(pr_number)
    except VCSError:
        logger.warning(
            "PR #%s: could not fetch review bodies for overflow scan",
            pr_number, exc_info=True,
        )
        return [], False
    # Union every block, oldest-first, so a finding carried only by a superseded
    # middle review stays selectable; the newest copy of a line wins on display
    # text because a later block's parse overwrites the earlier dict entry.
    blocks = [
        body for body in review_bodies if _extract_overflow_block(body) is not None
    ]
    if not blocks:
        return [], True
    try:
        comment_bodies = host.get_pr_comment_bodies_strict(pr_number)
    except VCSError:
        logger.warning(
            "PR #%s: could not fetch the overflow acknowledgement comment",
            pr_number, exc_info=True,
        )
        return [], False
    # Union the terminal markers across every sentinel-carrying comment body,
    # matching ``maestro_review.review._parse_overflow_ack``: reading only the
    # newest roll-up dropped acks recorded on an earlier one.
    ack_body = "\n".join(
        body for body in comment_bodies if _OVERFLOW_ACK_SENTINEL in body
    )
    # Dedupe across blocks only.  With the coarse ``severity|file|line`` handle
    # two genuinely-different findings at one out-of-diff site share a
    # fingerprint, so collapsing by fingerprint alone would silently drop one of
    # them; within a block repeated handles are distinct findings and all are
    # kept, while across blocks the same handle is the same finding re-rendered.
    # The block carrying the most occurrences of a handle wins (newest on a tie).
    # The key is the *canonical* one (``_overflow_merge_key``), not the raw
    # embedded handle: a pre-#3326 block embedded a title-derived handle that was
    # re-minted each review, so keying on it would carry one still-open finding
    # once per legacy review.
    merged: dict[str, list[OverflowBugFinding]] = {}
    for body in blocks:
        per_body: dict[str, list[OverflowBugFinding]] = {}
        for finding in _eligible_overflow_bug_findings(body, ack_body):
            per_body.setdefault(_overflow_merge_key(finding), []).append(finding)
        for key, occurrences in per_body.items():
            if len(occurrences) >= len(merged.get(key, ())):
                merged[key] = occurrences
    # Sort explicitly: the union walk's insertion order is oldest-block-first,
    # which is neither the pre-union order (newest block) nor meaningful to a
    # consumer.  Site order is stable across runs and independent of how many
    # reviews happened to carry the block.
    return sorted(
        (f for occurrences in merged.values() for f in occurrences),
        key=lambda f: (f.file, f.line, f.fingerprint, f.title),
    ), True


def _signature_component_differs(
    current: str | None, stamped: str | None,
) -> bool:
    """Whether one signature component has *provably* changed.

    Proposal 137. A component differs only when both sides are known
    (truthy) and unequal — mirroring the truthiness guards the
    base-advance release in :meth:`_in_conflict_cooldown` already uses.
    An unmeasurable current reading, or a legacy anchor carrying no
    stamp, is deliberately not a difference: the failure direction is
    toward today's behaviour (the time window stays intact) rather than
    toward releasing a cooldown on an absence of evidence.
    """
    return bool(current) and bool(stamped) and current != stamped

def _signature_released(
    engine: EvolutionEngine,
    *,
    current: tuple[str | None, ...],
    stamped: tuple[str | None, ...],
) -> bool:
    """Whether the measured signature releases a cooldown anchor.

    Proposal 137. ``True`` when ``[loop] cooldown_signature_keying_enabled``
    is on *and* any component of the current signature provably differs
    from the one the anchor was stamped against — the cooldown is bouncing
    off a world that no longer exists, so it must not bounce the walk
    again. ``False`` with the flag off, which leaves both predicates their
    pure wall-clock shape.

    The two callers pass different-width signatures, and the width is the
    claim. The conflict cooldown keys on the full ``(branch head,
    origin/main base, CI rollup)`` triple, because a base advance is
    precisely the race it guards — that is the proposal-099 release this
    generalises. The quality-fix cooldown keys on ``(branch head, CI
    rollup)`` only: ``origin/main`` advances several times a day
    independently of the PR, so admitting it there would release the 24h
    window on a move that says nothing about whether this PR's own red
    changed, collapsing the throttle before proposal 137's step-7 backoff
    exists to bound it.
    """
    if not engine._config.cooldown_signature_keying_enabled:
        return False
    return any(
        engine._signature_component_differs(cur, stamp)
        for cur, stamp in zip(current, stamped, strict=True)
    )

def _next_cooldown_attempts(
    engine: EvolutionEngine,
    *,
    current: tuple[str | None, ...],
    stamped: tuple[str | None, ...],
    prior: int,
    anchored: bool,
    consumed: bool,
) -> int:
    """The attempt count to stamp alongside a freshly-written anchor.

    Proposal 137. ``0`` when any signature component has *provably*
    changed — the next lap faces a different world, so the backoff ladder
    starts over — and ``prior + 1`` otherwise, recording that one more lap
    has now bounced off this same observation.

    Deliberately consults :meth:`_signature_component_differs` directly
    rather than :meth:`_signature_released`: like the sibling signature
    stamps written at the same call sites, the counting is unconditional
    bookkeeping, so the flag governs only what
    :meth:`_cooldown_window_hours` does with the count, never what is
    recorded.

    *anchored* is whether an anchor timestamp existed before this write. A
    first-ever stamp has no prior lap to have bounced off, so its count is
    ``0`` — the ladder's first rung — rather than the ``prior + 1`` the
    all-``None`` stamped signature would otherwise produce.

    *consumed* is whether this lap genuinely *bounced* off the cooldown —
    the premise :meth:`_cooldown_window_hours` reads every rung as. When
    ``False`` the count is returned unchanged, ahead of the difference
    scan: such a lap proves neither that the world moved (which would
    reset to ``0``) nor that another attempt was spent against it. Three
    reachable laps are non-consuming:

    * a **bypassed** lap — the conflict cooldown was holding, but one of
      its escape hatches (:meth:`_branch_has_local_commits_ahead`,
      :meth:`_has_unconsumed_awaiting_push_verdict`) admitted the walk
      anyway, so the re-route it stamps was never governed by the rung;
    * a lap that **pushed** — the ahead-of-origin arm advanced
      ``origin/<branch>`` and then fell through to the conflict route, so
      the anchor's head is the pre-push one the outcome was never
      measured against;
    * an **unmeasurable** lap — the head did not resolve or the CI rollup
      never concluded, so the lap observed nothing to have bounced off.

    The ``anchored`` first-stamp rule and the provably-changed reset keep
    their precedence whenever *consumed* is ``True``.
    """
    if not anchored:
        return 0
    if not consumed:
        return prior
    if any(
        engine._signature_component_differs(cur, stamp)
        for cur, stamp in zip(current, stamped, strict=True)
    ):
        return 0
    return prior + 1

def _cooldown_window_hours(
    engine: EvolutionEngine,
    *,
    ceiling: float,
    attempts: int,
    comparable: bool,
) -> float:
    """The hours a cooldown anchor holds for, given its attempt count.

    Proposal 137. With ``[loop] cooldown_signature_keying_enabled`` off
    this is *ceiling* — the flat configured window of today, so both
    predicates are unchanged. On, the window decays to a bounded
    exponential backoff over the laps already spent against this same
    signature: ``[loop] cooldown_backoff_base_hours`` for the first
    re-route, doubling each unchanged lap, clamped at *ceiling* — so a lap
    that changed nothing is retried within the hour while a persistently
    broken head converges back to today's cadence within a handful of laps.
    The exponent is clamped so the doubling can never overflow to
    ``inf``; a base at or above the ceiling degrades to the flat window.

    Expected burn-in spend: the ladder starts at rung ``0``, so a PR whose
    signature never moves is re-routed at roughly 1h/3h/7h/15h/31h of
    elapsed time — about five routes in the first day against today's one,
    each minting a remediation directive and its SDK spend. That front-
    loading is the instrument's whole point (a no-op lap is cheap to retry
    and the fix may be one lap's work) and it is self-limiting: the window
    reaches the 24h ceiling by the fifth unchanged lap, so the excess is
    bounded to the burn-in window rather than recurring. It is also why the
    flag ships default-off — the delta is measured before promotion.

    Every rung this reads is a lap that *genuinely bounced*:
    :meth:`_next_cooldown_attempts` advances the count only for a lap the
    cooldown itself governed, that did not push a new head, and that could
    measure its own signature — so the ladder's position describes real
    retries against this observation rather than laps that merely passed
    the anchor write.

    *comparable* is false when no signature component is known on both
    sides — a legacy anchor — or when this lap's own reading is not one a
    rung could have been consumed against (:meth:`_lap_signature_measurable`
    is false: the head did not resolve, or the rollup never concluded).
    Both halves are required, and they are the exact reciprocal of the
    consumption rule: a lap that cannot advance the ladder must not read a
    position off it either, or a PR whose head never resolves would freeze
    at its current rung and be re-routed at the short first window forever
    instead of holding the ceiling. The count then describes nothing, so
    the window falls back to *ceiling*, keeping the same
    fail-toward-current-behaviour posture
    :meth:`_signature_component_differs` already takes.
    """
    if not engine._config.cooldown_signature_keying_enabled:
        return ceiling
    if not comparable:
        return ceiling
    base = engine._config.cooldown_backoff_base_hours
    return min(float(base * 2 ** min(max(attempts, 0), 60)), ceiling)

def _signature_comparable(
    current: tuple[str | None, ...],
    stamped: tuple[str | None, ...],
) -> bool:
    """Whether any signature component is known on *both* sides.

    Proposal 137. The backoff count only describes a real observation when
    at least one component of the anchor's signature can be compared with
    this lap's reading; otherwise the anchor is legacy or unmeasurable and
    the window stays the configured ceiling.
    """
    return any(
        bool(cur) and bool(stamp)
        for cur, stamp in zip(current, stamped, strict=True)
    )

def _lap_signature_measurable(
    *,
    origin_head: str | None,
    check_conclusion: str,
) -> bool:
    """Whether this lap's reading is usable as a *bounce*.

    Proposal 137. A rung records that one more lap faced the same world
    and bounced, so the reading it is stamped against must actually be a
    reading: the ``origin/<branch>`` head resolved (``origin_head`` is
    ``None`` when :meth:`VCS.rev_parse` could not resolve the branch) and
    the CI rollup *concluded*.

    "Concluded" is decided by
    :func:`~maestro.pr_owed_act.check_runs_from_conclusion` — the single
    owner of that question — rather than a second local truthiness rule:
    it yields a non-empty sequence for ``success`` / ``failure`` /
    ``none`` and an empty one for the ``""`` fetch-error degrade and for
    a still-``pending`` rollup. Pure: no host or VCS call.

    At today's call sites the rollup half is defence in depth rather than
    a live discriminator: ``get_pr_check_conclusion`` has a closed domain
    (``success`` / ``failure`` / ``none`` / ``""``) and
    :meth:`_tend_single_pr`'s CI-wait gate already returns before any
    anchor write on ``""``, so the conclusion reaching a stamp has always
    concluded. It is asserted here anyway so the premise the rung rests on
    is stated where the rung is computed, not inferred from a caller's
    early return.
    """
    return bool(origin_head) and bool(
        check_runs_from_conclusion(check_conclusion),
    )

def _in_conflict_cooldown(
    engine: EvolutionEngine,
    plan: EvolutionPlan,
    now: datetime,
    *,
    head_sha: str | None = None,
    base_sha: str | None = None,
    ci_conclusion: str | None = None,
) -> bool:
    """Whether a recent failed auto-merge attempt is still cooling down.

    Tend writes ``last_conflict_resolution_attempt_at`` every time it
    routes a conflict into ``next_task_directive`` so the same PR is
    not re-routed on every iteration.  The cooldown bounces tend off
    that PR until the directive has had a chance to land a fix.

    Proposal 099: a moved ``origin/main`` invalidates the cooldown exactly
    as a moved head does (#2821). The base SHA the anchor was stamped
    against is recorded in ``last_conflict_resolution_base_sha``; when the
    current base has provably advanced from it (both known and differing,
    mirroring the head-advance block's truthiness guards) the freshly-resolved
    head now faces a base it was never merged against — a new race — so
    re-attempt rather than bounce. An unmeasurable current base leaves the
    time-based cooldown intact.

    Proposal 137 generalises that release to the whole signature the anchor
    was stamped against. *head_sha*, *base_sha* and *ci_conclusion* are the
    walk's already-measured locals for this PR this lap; with
    ``[loop] cooldown_signature_keying_enabled`` on, a provable change in
    any of them releases the cooldown before the hours comparison. With the
    flag off the triple is ignored and the base-SHA release below is the
    only keyed one, exactly as today.
    """
    last = engine._parse_iso(plan.last_conflict_resolution_attempt_at)
    if last is None:
        return False
    if _signature_released(
        engine,
        current=(head_sha, base_sha, ci_conclusion),
        stamped=(
            plan.last_conflict_resolution_head_sha,
            plan.last_conflict_resolution_base_sha,
            plan.last_conflict_resolution_ci_conclusion,
        ),
    ):
        return False
    current_base = engine._vcs.rev_parse("origin/main")
    if (
        current_base
        and plan.last_conflict_resolution_base_sha
        and current_base != plan.last_conflict_resolution_base_sha
    ):
        return False
    hours = (now - last).total_seconds() / 3600.0
    window = _cooldown_window_hours(
        engine,
        ceiling=engine._config.conflict_resolution_cooldown_hours,
        attempts=plan.conflict_resolution_signature_attempts,
        comparable=(
            engine._signature_comparable(
                (head_sha, base_sha, ci_conclusion),
                (
                    plan.last_conflict_resolution_head_sha,
                    plan.last_conflict_resolution_base_sha,
                    plan.last_conflict_resolution_ci_conclusion,
                ),
            )
            and engine._lap_signature_measurable(
                origin_head=head_sha,
                check_conclusion=ci_conclusion or "",
            )
        ),
    )
    return hours < window

def _in_quality_fix_cooldown(
    engine: EvolutionEngine,
    plan: EvolutionPlan,
    now: datetime,
    *,
    head_sha: str | None = None,
    base_sha: str | None = None,
    ci_conclusion: str | None = None,
) -> bool:
    """Whether a recent quality-gate-fix attempt is still cooling down.

    Mirrors :meth:`_in_conflict_cooldown` but reads the dedicated
    ``last_quality_fix_attempt_at`` timestamp so the two cooldowns
    do not interfere with each other on the same PR.

    Proposal 137: this cooldown, written later as an explicit mirror,
    copied the timestamp shape and none of the conflict cooldown's ad-hoc
    releases — so a quality-fix verdict about a replaced head bounced the
    walk for the rest of the window. With
    ``[loop] cooldown_signature_keying_enabled`` on, a provable change in
    the ``(head, CI rollup)`` pair releases it before the hours comparison;
    with the flag off it is the pure time window of today. *base_sha* is
    accepted for call-site symmetry with :meth:`_in_conflict_cooldown` and
    is still stamped onto the anchor as a record, but is deliberately not a
    release component here — see :meth:`_signature_released`.
    """
    last = engine._parse_iso(plan.last_quality_fix_attempt_at)
    if last is None:
        return False
    if _signature_released(
        engine,
        current=(head_sha, ci_conclusion),
        stamped=(
            plan.last_quality_fix_head_sha,
            plan.last_quality_fix_ci_conclusion,
        ),
    ):
        return False
    hours = (now - last).total_seconds() / 3600.0
    window = _cooldown_window_hours(
        engine,
        ceiling=engine._config.quality_fix_cooldown_hours,
        attempts=plan.quality_fix_signature_attempts,
        comparable=(
            engine._signature_comparable(
                (head_sha, ci_conclusion),
                (
                    plan.last_quality_fix_head_sha,
                    plan.last_quality_fix_ci_conclusion,
                ),
            )
            and engine._lap_signature_measurable(
                origin_head=head_sha,
                check_conclusion=ci_conclusion or "",
            )
        ),
    )
    return hours < window

def _reminder_threshold_hours(reminders_sent: int) -> float:
    """Hours to wait before the next reminder given how many have been sent."""
    idx = min(reminders_sent, len(_PR_REMINDER_HOURS) - 1)
    return _PR_REMINDER_HOURS[idx]

def _should_send_reminder(
    engine: EvolutionEngine,
    plan: EvolutionPlan,
    pr: Any,
    now: datetime,
) -> bool:
    """True when the PR has been quiet long enough for the next nudge."""
    # Anchor: the most recent of (pr.updated_at, last reminder we sent).
    last_reminder = engine._parse_iso(plan.last_pr_reminder_at)
    pr_updated = engine._parse_iso(getattr(pr, "updated_at", None))
    candidates = [t for t in (last_reminder, pr_updated) if t is not None]
    if not candidates:
        return False
    anchor = max(candidates)
    threshold = engine._reminder_threshold_hours(plan.pr_reminder_count)
    hours = (now - anchor).total_seconds() / 3600.0
    return hours >= threshold

async def _send_pr_reminder(
    engine: EvolutionEngine, plan: EvolutionPlan, pr: Any,
) -> bool:
    """Send Thomas a Telegram nudge about an unreviewed PR.

    Returns ``True`` once the notifier has accepted the message and
    ``False`` when the send was cancelled mid-flight. The caller gates its
    durable bookkeeping on that result, so a swallowed cancellation leaves
    the reminder retryable instead of taper-suppressing the next nudge.
    """
    url = getattr(pr, "url", "") or plan.pr_url or ""
    message = (
        f"Evolution PR awaiting review: {plan.title}\n"
        f"{url}\n"
        f"Priority: {plan.priority} | Category: {plan.category}"
    )
    try:
        await engine._notifier.notify(message)
    except asyncio.CancelledError:
        logger.warning(
            "Failed to send reminder for plan %s", plan.plan_id,
            exc_info=True,
        )
        return False
    return True

async def _send_pr_ready_for_review(
    engine: EvolutionEngine, plan: EvolutionPlan, pr: Any,
) -> bool:
    """Tell Thomas a PR is stable and ready for re-review.

    Fired on a post-delivery confirmation sweep (from
    :meth:`_confirm_ready_for_review`) once the delivered head's CI rollup
    is settled green (``success`` / ``none``), the branch is current with
    ``main`` again, and no review comment is outstanding — not on the push
    pass itself, where CI on the fresh merge commit is still pending.
    Telegram-only (no ``messages.jsonl`` write), mirroring
    :meth:`_send_pr_reminder` — including its delivery result: ``True`` on
    a send the notifier accepted, ``False`` on a swallowed cancellation, so
    the caller keeps the retry stamp set rather than burning it.
    """
    url = getattr(pr, "url", "") or plan.pr_url or ""
    message = (
        f"PR ready for re-review: {plan.title}\n"
        f"{url}\n"
        "Synced with main, all review comments addressed, fixes pushed."
    )
    try:
        await engine._notifier.notify(message)
    except asyncio.CancelledError:
        logger.warning(
            "Failed to send ready-for-review for plan %s", plan.plan_id,
            exc_info=True,
        )
        return False
    return True

async def _confirm_ready_for_review(
    engine: EvolutionEngine, plan: EvolutionPlan, pr: Any, branch: str,
    result: TendPRsResult, *, check_conclusion: str,
) -> None:
    """Send the deferred "ready for re-review" ping once CI confirms green.

    Runs on a post-delivery sweep when the branch is clean and the plan
    carries a ``ready_for_review_pending_sha`` stamped at push time. The
    ping fires only when that exact head's CI rollup is settled green
    (``success`` / ``none``) with no open comments — the caller has already
    confirmed "no comments" via the strict ``comment_scan_ok`` gate. The
    pending stamp is the dedup token: cleared on a *confirmed* notify /
    CI-failure / head-staleness, so the ping fires at most once per
    delivered head. An undelivered ping — a notifier exception, or a
    cancellation :meth:`_send_pr_ready_for_review` swallowed and reported
    as ``False`` — leaves the stamp set and is retried next sweep.

    *check_conclusion* is the rollup reading ``tend_evolution_prs`` resolved
    once for this PR this lap (proposal 137) — the same value this method
    used to fetch itself, including the ``""`` degrade on a ``VCSError``.
    """
    current = _resolve_pr_head_sha(engine, pr, branch)
    if current != plan.ready_for_review_pending_sha:
        # Head moved since delivery (a fresh push, or the stamp is stale) —
        # this is not the head we were asked to confirm. Drop the stamp;
        # the new head re-stamps on its own delivery pass.
        engine._memory.update_plan_tend_fields(
            plan.plan_id,
            ready_for_review_pending_sha=None,
        )
        return
    conclusion = check_conclusion
    if conclusion == "":
        # CI still pending on the delivered head — keep the stamp and
        # reprobe next sweep.
        return
    if conclusion == "failure":
        # The delivered head is broken; the failure-routing paths handle
        # remediation. Drop the stamp so we never ping a red head.
        engine._memory.update_plan_tend_fields(
            plan.plan_id,
            ready_for_review_pending_sha=None,
        )
        return
    # ``success`` or ``none`` (no CI configured — vacuously green, matching
    # the #2109 no-CI semantics in ``_tend_single_pr``) fires the ping;
    # otherwise a no-CI repo would strand every PR in permanent pending.
    # Isolated like the reminder — a ``CancelledError`` raised *past* the
    # helper re-raises to preserve shutdown discipline; any other blip is
    # captured so one bad notify never aborts the sweep. Clear the stamp +
    # count only on a confirmed send, so any undelivered ping — a Telegram
    # blip, or a cancellation the helper itself swallowed and reported as
    # ``False`` — leaves the stamp set and retries next pass.
    try:
        delivered = await _send_pr_ready_for_review(engine, plan, pr)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result.errors.append(
            "ready-for-review notify failed for plan "
            f"{plan.plan_id}: {exc}",
        )
    else:
        if not delivered:
            result.errors.append(
                "ready-for-review notify cancelled for plan "
                f"{plan.plan_id}",
            )
            return
        engine._memory.update_plan_tend_fields(
            plan.plan_id,
            ready_for_review_pending_sha=None,
        )
        result.ready_for_review_sent += 1

def _resolve_pr_head_sha(engine: EvolutionEngine, pr: Any, branch: str) -> str | None:
    """Return the current head SHA of *pr*'s branch.

    Prefers ``pr.head_oid`` when the host adapter exposes it (zero
    subprocess cost) and falls back to ``rev_parse(origin/<branch>)``
    otherwise. Called only from paths that actually need the SHA —
    CHANGES_REQUESTED staleness, the conflicted-merge path, and the
    ready-for-review confirmation (:meth:`_confirm_ready_for_review`) —
    so CLEAN / MERGEABLE PRs without a pending ready-for-review stamp
    pay no resolution cost.
    """
    head_sha = getattr(pr, "head_oid", None) or None
    if head_sha is None:
        head_sha = engine._vcs.rev_parse(f"origin/{branch}")
    return head_sha

def _review_decision_is_stale(
    engine: EvolutionEngine, pr: Any, current_head_sha: str | None,
) -> bool:
    """Whether a CHANGES_REQUESTED decision is stale relative to head.

    Stale = the reviewer left a decision on an earlier commit and the
    branch head has since advanced. Returns ``False`` when the
    adapter exposes no signal that lets us decide — the loop then
    falls back to today's strict skip behaviour. See the
    ``tend_evolution_prs`` invariant in ``CLAUDE.md`` for the
    operator-visible contract.
    """
    review_head_sha = getattr(pr, "review_decision_head_sha", None) or None
    if review_head_sha:
        return (
            current_head_sha is not None
            and review_head_sha != current_head_sha
        )
    review_at = engine._parse_iso(getattr(pr, "review_decision_at", None))
    head_committed_at = engine._parse_iso(
        getattr(pr, "head_committed_at", None),
    )
    if review_at is not None and head_committed_at is not None:
        return head_committed_at > review_at
    logger.debug(
        "review staleness undecidable for PR %s — adapter exposes "
        "neither review_decision_head_sha nor "
        "(review_decision_at + head_committed_at)",
        getattr(pr, "url", "?"),
    )
    return False

async def tend_evolution_prs(
    engine: EvolutionEngine, *, settled_oracle: Callable[[int], MergeTruth],
) -> TendPRsResult:
    """Keep open evolution PR branches updated with ``origin/main``.

    Purely mechanical — no SDK calls.  For every plan whose PR is
    still open upstream this method:

    * sends a tapered reminder to Thomas (48h / 72h / 168h) when the
      PR has gone quiet;
    * merges ``origin/main`` into stale ("BEHIND") PR branches with
      no rebase / force-push (the branch protection on ``push()``
      makes force-push structurally impossible);
    * for ``DIRTY`` / ``BLOCKED`` / ``UNSTABLE`` PRs, records the
      conflicted file list onto :attr:`TendPRsResult.conflicts_routed`
      (cooldown-throttled) — the LLM never touches a conflicted tree
      here.  :meth:`MaestroAgent._route_pr_feedback_to_pending_requests`
      (in ``maestro/agent.py``) appends that entry to
      ``SelfModel.pending_pr_requests`` so the next
      ``_run_task_phase`` drains it and runs the remediation task;
    * for clean-merge-followed-by-broken-gates PRs, resets the merge
      and records the gate summary onto
      :attr:`TendPRsResult.quality_gates_routed` (cooldown-throttled)
      for the same FIFO routing;
    * respects ``CHANGES_REQUESTED`` review decisions — skips all
      tending on those PRs until the reviewer returns.

    *settled_oracle* is the merge-truth chokepoint
    (:meth:`~maestro.agent.MaestroAgent.is_pr_settled`) threaded down to
    :meth:`_reconcile_orphan_branches`, which reads it to recognise a
    merged-but-not-yet-flipped orphan branch through the same
    provenance-stamped door every other PR-servicing face uses (proposal
    087).  It is required rather than defaulted: an absent oracle would let
    the orphan scan observe no merge state at all — a silent degradation the
    merge-truth fence cannot see, since that guard forbids *calling* a raw
    primitive, not skipping the observation entirely.  Production wires
    ``self.is_pr_settled`` from ``_phase_tend_evolution_prs``; tests build
    one with :func:`tests.support.merge_truth.merge_oracle`.

    This method never raises; infrastructure failures are captured
    in :attr:`TendPRsResult.errors`.
    """
    result = TendPRsResult()
    if not engine._config.tend_prs_enabled:
        return result

    # Keep tending deterministic — refuse to run from a dirty tree or
    # a non-main branch left over by a prior crash.
    engine.ensure_main_branch()

    try:
        # ``--prune`` unconditionally: the reconcile's
        # ``remote_refs_containing`` reachability check reads local
        # remote-tracking refs, and a stale ref for a branch deleted on the
        # remote would otherwise make an unpushed local-``main`` commit look
        # "still pushed" and wrongly authorize the hard reset (proposal 095).
        engine._vcs.fetch("origin", prune=True)
    except VCSError as exc:
        result.errors.append(f"fetch origin failed: {exc}")
        return engine._emit_tend_errors(result)

    # Main-branch tamper check — refuse to tend if local ``main`` has
    # drifted ahead of ``origin/main``.  A merge-from-main attempt
    # while ``main`` is dirty would splice local-only commits into
    # every tended branch, which is exactly what tending must avoid.
    local_main = engine._vcs.rev_parse("main")
    remote_main = engine._vcs.rev_parse("origin/main")
    if local_main and remote_main and local_main != remote_main:
        ahead, _behind = engine._vcs.commits_ahead_behind(
            "main", "origin/main",
        )
        if ahead > 0:
            # Heal ``main`` back to ``origin/main`` iff every
            # local-``main``-only commit is already reachable from an
            # ``origin/*`` ref (already pushed — safe to drop, since the
            # loop commits only to task/evolve branches by invariant). A
            # genuinely unpushed commit is real work: preserve it, refuse.
            try:
                ahead_shas = engine._vcs.local_main_ahead_commits()
            except VCSError as exc:
                result.errors.append(
                    "local main is ahead of origin/main and enumerating"
                    f" the drift failed: {exc} — refusing to tend PRs"
                )
                return engine._emit_tend_errors(result)
            sha_refs = {
                sha: engine._vcs.remote_refs_containing(sha)
                for sha in ahead_shas
            }
            unpushed = [sha for sha, refs in sha_refs.items() if not refs]
            if unpushed:
                result.errors.append(
                    "unpushed local main commit(s) "
                    f"{', '.join(sha[:8] for sha in unpushed)}: local main"
                    " is ahead of origin/main — refusing to tend PRs"
                )
                return engine._emit_tend_errors(result)
            try:
                engine._vcs.reset_hard("origin/main")
            except VCSError as exc:
                result.errors.append(
                    "local main is ahead of origin/main and healing it to"
                    f" origin/main failed: {exc} — refusing to tend PRs"
                )
                return engine._emit_tend_errors(result)
            stranded = "; ".join(
                f"{sha[:8]} on {', '.join(refs)}"
                for sha, refs in sha_refs.items()
            )
            logger.warning(
                "tend_evolution_prs: reconciled local main ahead of "
                "origin/main by %d already-pushed commit(s) — hard-reset "
                "to origin/main [%s]",
                ahead, stranded,
            )
            engine._journal.record_event(
                content=(
                    "Found local main drifted ahead of origin/main by "
                    f"{ahead} already-pushed commit(s); healed it back with "
                    "a hard reset to origin/main so PR tending could resume. "
                    f"Stranded: {stranded}."
                ),
                entry_type="evolution_note",
                triggered_by="tend_reconcile_local_main",
            )

    # Strict: the best-effort default returns ``{}`` on any ``gh``
    # failure, which tend would read as "no open PRs" — pruning the whole
    # PR ledger and zeroing the open-PR count (#2813, #4153).
    try:
        pr_states = engine._host.get_open_evolution_pr_states(
            "maestro/evolve/", strict=True,
        )
    except VCSError as exc:
        result.errors.append(f"listing open PRs failed: {exc}")
        return engine._emit_tend_errors(result)
    result.enumerated = True

    # Prune ledger entries for PRs no longer in the open-PR set so the
    # stuck-summary never surfaces closed/merged PRs. The dropped numbers
    # are exactly the "this PR left the open set" signal on-settlement
    # record reconciliation drives on (proposal 103); capture them for the
    # loop's post-tend reconcile face — no new observation, no new host call.
    result.settled_pr_numbers = engine._pr_ledger.prune(
        {pr.number for pr in pr_states.values()},
    )

    # Base-branch baseline (proposal 124). ``ensure_main_branch()`` has put
    # HEAD on ``main`` and the reconcile above has healed it to
    # ``origin/main`` wherever it had drifted ahead, so the seam's own HEAD
    # check passes on exactly the laps where the tree really *is* the base —
    # and silently records nothing on the behind-but-not-ahead case the
    # reconcile deliberately leaves alone. Nothing between the enumeration
    # above and here touches the tree, so this is still "after the reconcile,
    # before any PR branch".
    #
    # Two things keep a whole ruff + mypy + pytest sweep out of the way of a
    # phase that is top-priority, runs every iteration, and is deliberately
    # exempt from burn-rate gating. It is **conditional on there being an
    # open evolution PR at all**: the baseline exists for the consumers that
    # attribute a PR branch's red against the base it was cut from, so a lap
    # with an empty worklist — the common quiet lap — pays nothing, and
    # ``implement`` captures its own base regardless. And it runs **off the
    # event loop**, so the heartbeat and every other waiter keep running for
    # the duration. The cache then makes it one gate run per new
    # ``origin/main`` head, not one per lap; the return value is unused here,
    # the point is the warmed cache every later branch's red is attributed
    # against.
    if pr_states:
        await asyncio.to_thread(engine._observe_baseline, remote_main or "")

    now = datetime.now(UTC)

    def _pr_age_key(plan: EvolutionPlan) -> str:
        pr = pr_states.get(evolution_branch(plan.plan_id))
        if pr is None or not pr.created_at:
            return "\uffff"
        return pr.created_at

    for plan in sorted(engine._memory.get_all_plans(), key=_pr_age_key):
        branch = evolution_branch(plan.plan_id)
        pr = pr_states.get(branch)
        if pr is None:
            # PR closed/merged upstream — check_merged_plans handles it.
            continue
        if (pr.state or "").upper() != "OPEN":
            continue
        # Admission is keyed on the *open PR*, not the plan status. An
        # ``implemented`` plan keeps its ``pr_url`` orphan guard. A plan
        # that has left ``implemented`` (retired to ``obsolete`` by a
        # self-prune or the orphaned-cure sweep while its PR is still open)
        # is admitted only when ``tend_planless_prs_enabled`` — so retiring
        # a plan can no longer orphan its still-open PR into a zombie. With
        # the flag off the worklist is byte-for-byte the old implemented +
        # ``pr_url`` set.
        if plan.status == "implemented":
            if not plan.pr_url:
                continue
        elif not engine._config.tend_planless_prs_enabled:
            continue
        result.tended += 1

        # Record the current origin head so the PR ledger can detect
        # genuine head movement and reset the remediation budget. Capture
        # the prior head *before* ``observe_head`` overwrites it so a
        # genuine advance can be read on this same lap.
        _origin_head = engine._vcs.rev_parse(f"origin/{branch}") or None
        _prior_ledger = engine._pr_ledger.get(pr.number)
        _prior_head = _prior_ledger.observed_head if _prior_ledger else None
        engine._pr_ledger.observe_head(pr.number, branch, _origin_head)

        # Head-advance invalidates the conflict cooldown (#2821). The
        # cooldown anchor (``last_conflict_resolution_attempt_at``) is a
        # bare wall-clock stamp keyed to *when* a conflict was routed, with
        # no awareness of the PR head; once the resolution is pushed to
        # origin the local-commits escape hatch closes and the fixed-window
        # cooldown strands a freshly-resolved head that re-conflicts. An
        # advance from a *known* prior head is exactly the signal that the
        # prior resolution was delivered, so release the stale cooldown and
        # let tend re-attempt the merge on the fresh head. A genuinely
        # unchanged head (or the first observation, ``_prior_head`` empty)
        # keeps its cooldown, preserving the anti-re-route intent. The
        # local assignment is required alongside the store write because
        # ``update_plan_tend_fields`` mutates a disk-reloaded copy, not the
        # ``get_all_plans`` snapshot the cooldown-skip branch reads below in
        # this same lap.
        if (
            _prior_head
            and _origin_head
            and _origin_head != _prior_head
            and plan.last_conflict_resolution_attempt_at is not None
        ):
            engine._memory.update_plan_tend_fields(
                plan.plan_id,
                last_conflict_resolution_attempt_at=None,
            )
            plan.last_conflict_resolution_attempt_at = None

        # Reminder (tapered). Always runs, even under CHANGES_REQUESTED
        # — a stale CHANGES_REQUESTED is the exact case the nudge is
        # for.
        if _should_send_reminder(engine, plan, pr, now):
            # Typed-return isolation: a Telegram blip on plan A must
            # not abort the for-loop and silently skip every plan
            # after it. A ``CancelledError`` raised past the helper
            # re-raises so shutdown discipline survives; everything
            # else is captured into ``result.errors`` and the
            # persistence step is gated on the helper's *delivery
            # result* — not merely on "did the await raise?" — so a
            # cancellation the helper swallowed still retries next
            # iteration instead of tapering the nudge away.
            try:
                delivered = await _send_pr_reminder(engine, plan, pr)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result.errors.append(
                    f"reminder failed for plan {plan.plan_id}: {exc}",
                )
            else:
                if delivered:
                    engine._memory.update_plan_tend_fields(
                        plan.plan_id,
                        last_pr_reminder_at=now.isoformat(),
                        pr_reminder_count=plan.pr_reminder_count + 1,
                    )
                    result.reminders_sent += 1
                else:
                    result.errors.append(
                        f"reminder cancelled for plan {plan.plan_id}",
                    )

        # The PR head SHA feeds two gates — the CHANGES_REQUESTED
        # stale-review check below and the strategy-exhausted head
        # pin inside ``_tend_single_pr``. Both are off the hot path
        # (CLEAN / MERGEABLE PRs never need it), so we resolve
        # lazily on demand rather than once per loop iteration.
        head_sha: str | None = None

        if (pr.review_decision or "").upper() == "CHANGES_REQUESTED":
            head_sha = _resolve_pr_head_sha(engine, pr, branch)
            if _review_decision_is_stale(engine, pr, head_sha):
                result.stale_review_resumed += 1
                logger.info(
                    "Resuming tend on PR %s — review is stale "
                    "(head advanced past the reviewed commit)",
                    plan.pr_url or "?",
                )
            else:
                result.skipped_for_review += 1
                result.changes_requested_prs.append(
                    (
                        pr.number,
                        pr.title,
                        plan.plan_id,
                        pr.body or "",
                        pr.url or "",
                        pr.head_ref,
                    ),
                )
                continue

        # Unresolved AI bug comments need remediation regardless of merge
        # state: the reviewer's advisory ``review`` check is excluded from
        # the CI rollup, so a PR can look CLEAN while a 🐛 finding sits
        # open. Detect here — before the ``is_current`` short-circuit
        # below — so a mergeable PR still routes a comment-processing task.
        # Snapshot bug/overflow routing so the "ready for re-review" ping
        # below can tell whether *this* PR still carries an unaddressed
        # review comment. ``_route_bug_comments`` (which also runs the
        # overflow path) appends one entry per outstanding comment set, so
        # a length delta is exactly "all comments addressed?".
        _bug_routed_before = len(result.bug_comments_routed) + len(
            result.bug_overflow_routed,
        )
        # ``comment_scan_ok`` positively confirms the comment scan ran and
        # succeeded; the ready-for-review confirmation below requires it so
        # a swallowed host-fetch error never reads as "zero comments". When
        # comment-tending is disabled the scan is skipped and ``True`` is
        # vacuous — the confirmation also gates on ``tend_bug_comments_enabled``.
        comment_scan_ok = True
        if engine._config.tend_bug_comments_enabled:
            comment_scan_ok = _route_bug_comments(engine, plan, pr, result)
        had_outstanding_comments = (
            len(result.bug_comments_routed)
            + len(result.bug_overflow_routed)
            > _bug_routed_before
        )

        # PR up-to-date with main is usually a no-op. Exception:
        # a remediation task may have committed locally on this
        # branch while we were idle, leaving commits ahead of
        # origin (run() pushes them directly, but its push can fail —
        # ``_tend_single_pr`` is the fallback deliverer), so fall
        # through when local commits are present; the merge body is a
        # no-op against an ancestor and the trailing ``push(branch)``
        # delivers the remediation commits.
        #
        # Proposal 095: the catch-up no-op and conflict-route decisions
        # key on a directly measured distance-behind-``origin/main`` fact
        # (``observe_pr_staleness``) instead of the async host
        # ``mergeStateStatus`` / ``mergeable`` proxy — which can mask a
        # behind-but-clean branch as a no-op during the host's recompute
        # window. Observe once per PR.
        staleness = engine._observe_pr_staleness(branch)
        # Rollup conclusion, resolved once per PR per lap (proposal 137).
        # A lap-scoped local beside ``staleness``, not an engine attribute:
        # the engine has no fresh-iteration reset hook, and a local cannot
        # go stale across laps. The ``""`` degrade is cached along with
        # everything else, so a failed fetch is not retried later in the
        # same lap and every downstream reader sees the identical verdict.
        # The two sites that previously fetched it themselves
        # (``_tend_single_pr``, ``_confirm_ready_for_review``) now take it
        # as a parameter, so this hoist adds a call only for a PR the walk
        # short-circuits before reaching either — bounded by
        # ``max_open_evolution_prs`` fetches per lap.
        try:
            check_conclusion = engine._host.get_pr_check_conclusion(pr.number)
        except VCSError:
            check_conclusion = ""
        # Owed-act observation (proposal 135, fed a measured CI reading by
        # proposal 137). Placed immediately after the staleness reading and
        # *before* both the ``is_current`` short-circuit and the
        # conflict-cooldown skip below, so a PR the walk stops tending for
        # either reason is still measured — those are exactly the laps where
        # a latched handoff claim goes stale unnoticed. Unconditional: the
        # release path (``prune_refuted_standdowns``) carries its own
        # ``refutable_standdown_enabled`` guard at its call site, so gating
        # the *measurement* too only left the chokepoint's input missing
        # wherever the canary is not deployed.
        act, blocker = _observe_owed_act(
            engine,
            pr, staleness, result, check_conclusion,
        )
        result.owed_acts_observed[pr.number] = act
        result.owned_blockers_observed[pr.number] = blocker
        is_current = staleness.commits_behind == 0
        if is_current and not _branch_has_local_commits_ahead(
            engine,
            pr.head_ref,
        ):
            # Deferred ready-for-review confirmation. The branch is clean —
            # current with ``main`` again — so a head stamped on a prior
            # delivery pass now has settled CI to inspect. Cheapest clause
            # first so idle PRs pay nothing: only a plan carrying a pending
            # stamp reaches ``_confirm_ready_for_review``. ``comment_scan_ok``
            # is the strict-path guard — it positively confirms the comment
            # scan ran and found nothing, never a swallowed fetch error.
            if (
                engine._config.pr_ready_for_review_notify_enabled
                and plan.ready_for_review_pending_sha is not None
                and engine._config.tend_bug_comments_enabled
                and comment_scan_ok
                and not had_outstanding_comments
            ):
                await _confirm_ready_for_review(
                    engine,
                    plan, pr, branch, result,
                    check_conclusion=check_conclusion,
                )
            continue

        # Grounded conflict-route decision (proposal 095): a branch is
        # behind-and-conflicting exactly when it trails ``origin/main`` and
        # no longer merges cleanly. The actual conflict *routing* is
        # unaffected — the local ``merge_from("origin/main")`` result inside
        # ``_tend_single_pr`` stays the source of truth.
        needs_conflict_resolution = (
            staleness.commits_behind > 0
            and not staleness.cleanly_mergeable
        )
        # Conflict-cooldown skip, carrying the same local-commits escape
        # hatch as the ``is_current`` short-circuit above: when a prior
        # conflict-remediation task committed the resolution locally on
        # this branch (run() pushes it directly, but its push can fail —
        # Tend PR is the fallback deliverer), fall through to
        # ``_tend_single_pr`` so its trailing ``push(branch)`` delivers
        # the resolution and the host
        # recomputes mergeability out of DIRTY, rather than stranding the
        # commits for the full cooldown while ``main`` re-conflicts.
        #
        # The unconsumed-awaiting-push disjunct (#2825) is the same fall-
        # through for the case where the resolution was committed on-branch
        # then *reset away* (so no local commits remain) but the dedicated
        # ``#N`` remediation deferred the push to tend. It keys strictly on
        # ``_origin_head`` — the head ``observe_head`` just stored —
        # matching the verdict's recorded head. A delivered resolution
        # stops it: a successful tend push advances ``origin/<branch>``,
        # consuming the verdict and tripping the #2821 head-advance release
        # that nulls the cooldown anchor. But this disjunct does NOT itself
        # cap re-routing — when the resolution was reset away there is
        # nothing to push, so ``_tend_single_pr`` re-conflicts, re-routes,
        # and re-stamps the anchor with the head unmoved and the verdict
        # still live; the bypass then re-fires on the same head every lap
        # until a push lands or a downstream gate (costume-signature,
        # remediation-repetition, escalation-bridge) bounds the lineage.
        #
        # Proposal 137 splits the compound condition so the cooldown
        # verdict and each hatch are evaluated exactly once — both the
        # skip below and the ``cooldown_bypassed`` fact handed to
        # ``_tend_single_pr`` read the same evaluation, and
        # ``_branch_has_local_commits_ahead`` (which shells out) is not
        # called twice. The skip behaviour and the
        # ``conflicts_cooldown_skipped`` counter are unchanged.
        conflict_cooldown_holding = (
            needs_conflict_resolution
            and _in_conflict_cooldown(
                engine,
                plan, now,
                head_sha=_origin_head,
                base_sha=staleness.base_head_sha,
                ci_conclusion=check_conclusion,
            )
        )
        cooldown_bypassed = False
        if conflict_cooldown_holding:
            cooldown_bypassed = (
                _branch_has_local_commits_ahead(engine, pr.head_ref)
                or _has_unconsumed_awaiting_push_verdict(
                    engine,
                    pr.number, _origin_head,
                )
            )
        if conflict_cooldown_holding and not cooldown_bypassed:
            result.conflicts_cooldown_skipped += 1
            last_attempt = engine._parse_iso(
                plan.last_conflict_resolution_attempt_at
            )
            cooldown_until = (
                last_attempt
                + timedelta(
                    hours=engine._config.conflict_resolution_cooldown_hours
                )
                if last_attempt is not None
                else None
            )
            logger.info(
                "Tend PR #%s: in conflict cooldown until %s; skipping",
                pr.number,
                cooldown_until.isoformat() if cooldown_until else "unknown",
            )
            _stamp_cooldown_skip(
                engine,
                pr_number=pr.number, cooldown="conflict", result=result,
            )
            continue

        # Inverted default: assume cleanup is needed; clear only after
        # the tend body completes. Any unexpected exception (programming
        # bug, OSError, network failure) propagates so the loop's
        # iteration handler can classify or crash it. ``CancelledError``
        # is re-raised explicitly to preserve shutdown semantics; the
        # finally block runs in either case so a half-merged tree is
        # always reset before the next pass.
        needs_cleanup = True
        pushed_before = result.merged_updated
        try:
            await _tend_single_pr(
                engine,
                plan=plan,
                pr=pr,
                result=result,
                now=now,
                check_conclusion=check_conclusion,
                origin_head=_origin_head,
                base_head_sha=staleness.base_head_sha,
                cooldown_bypassed=cooldown_bypassed,
            )
            needs_cleanup = False
        except asyncio.CancelledError:
            raise
        finally:
            if needs_cleanup:
                with contextlib.suppress(VCSError):
                    engine._vcs.abort_merge()
                with contextlib.suppress(VCSError):
                    engine._vcs.discard_changes()
                with contextlib.suppress(VCSError):
                    engine._vcs.checkout("main")

        # Deferred stand-down signal. ``_tend_single_pr`` only increments
        # ``merged_updated`` after a clean ``origin/main`` merge, passing
        # gates, and a successful push — so a delta means the branch is now
        # current with ``main`` and the fixes are delivered. But that push
        # just invalidated CI on the old head and kicked off a fresh run on
        # the new merge commit, which is pending *right now* — so we must
        # NOT claim "ready for re-review" yet. Instead, stamp the delivered
        # head on the plan; a later sweep confirms (at the ``is_current``
        # short-circuit above, once the branch is current with ``main``)
        # that this head's CI rollup is green with no open comments, and
        # only then sends the ping. Gating on ``tend_bug_comments_enabled``
        # keeps the feature coherent: confirmation's "no comment
        # unaddressed" claim is meaningful only when comment-tending is the
        # source of truth — we never assert "all comments addressed" when we
        # never looked. Each push is a fresh head → fresh stamp, so the ping
        # fires at most once per delivered head.
        if (
            engine._config.pr_ready_for_review_notify_enabled
            and engine._config.tend_bug_comments_enabled
            and result.merged_updated > pushed_before
        ):
            # Stamp the *local* post-push head: ``pr`` is the pre-merge
            # snapshot from the top of this sweep, so only the local ref
            # sees the commit ``_tend_single_pr`` just pushed.
            # ``_confirm_ready_for_review`` resolves the head differently —
            # via ``_resolve_pr_head_sha`` (``head_oid``-preferred, else
            # ``origin/<branch>``) — but since Tend just pushed this head
            # the two coincide once the push has settled.
            delivered_head = engine._vcs.rev_parse(pr.head_ref)
            if delivered_head is None:
                logger.warning(
                    "Tend PR #%s: could not resolve delivered head SHA "
                    "on %s; skipping ready-for-review stamp",
                    pr.number, pr.head_ref,
                )
            else:
                engine._memory.update_plan_tend_fields(
                    plan.plan_id,
                    ready_for_review_pending_sha=delivered_head,
                )

    # Orphan reconciliation — remote branches with no open PR and a
    # local plan that is still ``approved`` (implement crashed after
    # branch push but before PR creation). Best-effort and narrow:
    # we only log + record, leaving the actual PR re-creation to
    # ``_implement_approved_plans`` which is idempotent.
    await _reconcile_orphan_branches(
        engine,
        pr_states, result, settled_oracle=settled_oracle,
    )

    return engine._emit_tend_errors(result)

def _emit_tend_errors(result: TendPRsResult) -> TendPRsResult:
    """Surface every isolated tend failure at WARNING, then return *result*.

    ``tend_evolution_prs`` isolates per-branch and preamble failures into
    ``result.errors`` so one bad branch never aborts the sweep, but the
    sole consumer (``run_loop``) reads the list only as a boolean gate.
    Without this sink a swallowed fetch/list/merge/push/checkout failure is
    invisible in the logs — contrary to the fail-loud doctrine. Routing
    every exit (the normal return AND the early fetch / list / local-main-
    ahead returns) through here logs each entry exactly once, while the
    broad ``except`` handlers stay typed-return-only so the AST fail-loud
    gate (``tests/test_no_silenced_exceptions.py``) does not flag them.
    """
    for err in result.errors:
        logger.warning("tend_evolution_prs: %s", err)
    return result

def _stamp_cooldown_skip(
    engine: EvolutionEngine, *, pr_number: int, cooldown: str, result: TendPRsResult,
) -> None:
    """Record that a tend lap declined *pr_number* while a cooldown held.

    Proposal 137's measurement leg. The two cooldown skips
    (``conflict`` in :meth:`tend_evolution_prs`, ``quality_fix`` in
    :meth:`_tend_single_pr`) previously left nothing durable behind but a
    ``logger.info`` line, so nothing could say how often a window held off a
    PR whose owed act — measured one frame away by :meth:`_observe_owed_act`
    and already sitting in :attr:`TendPRsResult.owed_acts_observed` — was
    ``code-owed``: a red the loop could have cleared itself.

    The row is stamped ``"diverged"`` on exactly that reading and
    ``"aligned"`` otherwise, through the one shared renderer
    :func:`~maestro.remediation_tracker.format_cooldown_skip_detail` the
    sensor parses back. Reading the already-populated observation costs
    nothing — no host, VCS or SDK call — and the enum member appears only as
    a comparison operand, so the owed-act chokepoint's sole-owner guard stays
    clean. Unflagged: the reading must be takeable with ``[loop]
    cooldown_signature_keying_enabled`` off. Changes no cooldown verdict, no
    route, and no counter.
    """
    owed_act = result.owed_acts_observed.get(pr_number, "")
    log_gate_activation(
        engine._gate_activations,
        gate=COOLDOWN_SKIP_GATE,
        action=(
            "diverged" if owed_act == OwedAct.CODE_OWED.value else "aligned"
        ),
        detail=format_cooldown_skip_detail(pr_number, cooldown, owed_act),
    )

def _observe_owed_act(
    engine: EvolutionEngine, pr: Any, staleness: PrStaleness, result: TendPRsResult,
    check_conclusion: str,
) -> tuple[str, str]:
    """Return the live ``(owed-act, merge-blocker)`` pair for *pr*.

    Proposal 135's measured-fact composer for
    :attr:`TendPRsResult.owed_acts_observed` and
    :attr:`TendPRsResult.owned_blockers_observed` — the blocker it derives on
    the way to the act is returned rather than discarded, so the refuted-
    release withdrawal can name it. Pure composition over state the
    tend walk already holds — the measured *staleness* reading, the host
    ``pr`` fields, and the *check_conclusion* the walk resolved once for
    this PR this lap — so it performs **no** host, VCS, merge-oracle or SDK
    call of its own.

    The blocker comes from
    :func:`~maestro.pr_owed_act.derive_owed_act_blocker` — the single
    derivation ``maestro pr-owed-act`` shares, which documents the
    ``CI_PENDING`` collapse and the conflict refinement (measured merge-tree
    reading or host ``mergeable == "CONFLICTING"``, outranking
    ``CI_FAILING``). It reuses the local *pr* rather than re-enumerating the
    open-PR states, which is the whole reason this costs nothing.

    The check-run sequence comes from
    :func:`~maestro.pr_owed_act.check_runs_from_conclusion` (proposal 137),
    so a rollup that concluded **red** reads
    :attr:`~maestro.pr_owed_act.OwedAct.CODE_OWED` — *I must write code* —
    rather than the blind "the clock is running" the previously-hardcoded
    empty sequence forced. The fail-safe direction is unchanged: ``""``,
    ``"pending"`` and anything unrecognised expand to the empty sequence →
    ``RUNNING`` → :attr:`~maestro.pr_owed_act.OwedAct.WAIT_CI`, which the
    validity oracle reads as *holds*, so an unreadable or in-flight rollup
    can never read as green and can never spuriously refute a latch.

    The unresolved-thread count comes from the ``bug_comments_routed`` entry
    :meth:`_route_bug_comments` appended for this PR earlier in the same
    iteration — the same ``_eligible_bug_comments`` selection, read back off
    the result rather than re-fetching the threads. Zero when the scan routed
    nothing, errored, or comment tending is disabled.
    """
    blocker = derive_owed_act_blocker(
        commits_behind=staleness.commits_behind,
        check_conclusion=check_conclusion,
        merge_state_status=getattr(pr, "merge_state_status", "") or "",
        mergeable=getattr(pr, "mergeable", "") or "",
        cleanly_mergeable=staleness.cleanly_mergeable,
        tend_catch_up=engine._config.tend_main_merge_enabled,
    )
    unresolved_threads = sum(
        len(comments)
        for number, _title, comments, *_rest in result.bug_comments_routed
        if number == pr.number
    )
    act = classify_pr_owed_act(
        pr.number,
        blocker=blocker,
        check_runs=check_runs_from_conclusion(check_conclusion),
        settled_pr_numbers=set(result.settled_pr_numbers),
        unresolved_threads=unresolved_threads,
    )
    # ``act`` is ``None`` only for a settled PR; this walk iterates the *open*
    # set, so the settled short-circuit is unreachable here — but a PR pruned
    # from the ledger this same lap would land in ``settled_pr_numbers``, and
    # "settled" is exactly the case the merge/close pruner already releases.
    # The blocker rides out alongside the act rather than being discarded:
    # it is the concrete reason a refuted release names in its withdrawal.
    return (act.value if act is not None else "", blocker)

def _route_bug_comments(
    engine: EvolutionEngine, plan: EvolutionPlan, pr: Any, result: TendPRsResult,
) -> bool:
    """Append a ``bug_comments_routed`` entry when *pr* has open review threads.

    Best-effort and purely mechanical (GraphQL/REST reads, no SDK call):
    fetches the PR's review threads, keeps the unresolved AI findings
    (🐛 bug, 💡 suggestion, ✏️ nit) and human threads via
    :func:`_eligible_bug_comments`, and records them so
    ``_route_pr_feedback_to_pending_requests`` can compose a
    comment-processing remediation task; body-overflow findings route the
    same way via :meth:`_route_overflow_findings` (proposal 064). A host
    error degrades to a no-op — the next tend cycle reprobes.

    Returns ``scan_ok`` — ``True`` when both the thread scan and the
    overflow scan completed (whether or not anything routed), ``False``
    when a host fetch errored. The ready-for-review confirmation gate
    reads this so a swallowed fetch error never masquerades as "zero
    comments"; overflow is still scanned even when the thread fetch fails,
    so routing behaviour is unchanged (``threads_ok and overflow_ok``).
    """
    threads_ok = True
    try:
        nodes = engine._host.get_pr_review_threads_strict(pr.number)
    except VCSError:
        logger.warning(
            "Tend PR #%s: could not fetch review threads", pr.number,
            exc_info=True,
        )
        nodes = []
        threads_ok = False
    review_comments = _eligible_bug_comments(nodes)
    if review_comments:
        logger.info(
            "Tend PR #%s: %d unresolved review comment(s) — routing "
            "remediation",
            pr.number, len(review_comments),
        )
        result.bug_comments_routed.append(
            (
                pr.number,
                pr.title,
                review_comments,
                plan.plan_id,
                pr.body or "",
                pr.url or "",
                pr.head_ref,
            ),
        )
    # Reply-completion delta (#2893): unresolved, un-replied threads GitHub
    # flagged ``isOutdated`` — the reply-needed selection minus the
    # fix-routing selection above. Fix routing (``_eligible_bug_comments``)
    # excludes outdated threads so stale findings never re-open a lap, but
    # an outdated thread that never received a terminal reply keeps counting
    # as outstanding and, under a merge-or-close standdown, freezes the PR.
    # Pure GraphQL read (no SDK) — one extra pass over the already-fetched
    # ``nodes``. Keyed by comment id so a twin thread whose sibling was
    # already replied is excluded by construction (only the un-replied twin
    # appears in ``_reply_needed_threads``).
    eligible_ids = {cid for cid, _p, _t, _c in review_comments}
    reply_completion = [
        thread
        for thread in _reply_needed_threads(nodes)
        if thread[0] not in eligible_ids
    ]
    if reply_completion:
        logger.info(
            "Tend PR #%s: %d outdated un-replied thread(s) — routing "
            "reply-completion",
            pr.number, len(reply_completion),
        )
        result.reply_completion_routed.append(
            (
                pr.number,
                pr.title,
                reply_completion,
                plan.plan_id,
                pr.body or "",
                pr.url or "",
                pr.head_ref,
            ),
        )
    overflow_ok = _route_overflow_findings(engine, plan, pr, result)
    return threads_ok and overflow_ok

def _route_overflow_findings(
    engine: EvolutionEngine, plan: EvolutionPlan, pr: Any, result: TendPRsResult,
) -> bool:
    """Append a ``bug_overflow_routed`` entry for unaddressed overflow findings.

    Mirrors :meth:`_route_bug_comments` for the review-body overflow path
    (proposal 064): keep the AI findings of every severity (🐛 bug,
    💡 suggestion, ✏️ nit) not yet acknowledged on the roll-up
    acknowledgement comment. The fetch + eligibility selection lives in
    :func:`_fetch_eligible_overflow_findings` so this router and the
    post-run completeness gate (``run_task`` §4d) agree exactly on what is
    outstanding; a host read error there returns no findings rather than
    degrading, so routing never re-dispatches already-addressed findings.

    Returns ``scan_ok`` — ``True`` when the scan completed (including the
    "no overflow block" and "nothing unaddressed" cases), ``False`` only
    when a host fetch (review bodies or the acknowledgement comment)
    errored, so the ready-for-review gate can distinguish a clean scan
    from a failed one.
    """
    findings, scan_ok = _fetch_eligible_overflow_findings(
        engine._host, pr.number,
    )
    if not findings:
        return scan_ok
    logger.info(
        "Tend PR #%s: %d unaddressed overflow finding(s) — routing "
        "remediation",
        pr.number, len(findings),
    )
    result.bug_overflow_routed.append(
        (
            pr.number,
            pr.title,
            findings,
            plan.plan_id,
            pr.body or "",
            pr.url or "",
            pr.head_ref,
        ),
    )
    return scan_ok

async def _tend_single_pr(
    engine: EvolutionEngine,
    *,
    plan: EvolutionPlan,
    pr: Any,
    result: TendPRsResult,
    now: datetime,
    check_conclusion: str,
    origin_head: str | None = None,
    base_head_sha: str | None = None,
    cooldown_bypassed: bool = False,
) -> None:
    """Mechanically merge ``origin/main`` into a single PR branch.

    Purely subprocess-driven — no SDK calls.  The local merge is the
    source of truth: a host-flagged conflict that merges cleanly here
    takes the gates + push path, while only a genuine local conflict
    records its conflicted file list on
    :attr:`TendPRsResult.conflicts_routed` so the LLM-driven
    remediation runs through the normal ``generate_task`` path on
    the next iteration.  Post-merge gate failures reset the branch
    and append to :attr:`TendPRsResult.quality_gates_routed` the
    same way.

    *check_conclusion* is the rollup reading ``tend_evolution_prs`` resolved
    once for this PR this lap (proposal 137) — the same value the CI-wait
    gate below used to fetch itself, including the ``""`` degrade on a
    ``VCSError``.  *origin_head* and *base_head_sha* complete that lap's
    cooldown signature (proposal 137): the ``origin/<branch>`` head the walk
    resolved for the PR ledger and the ``origin/main`` base carried on the
    staleness observation.  All three are stamped onto every cooldown anchor
    this method writes, and read back by the two cooldown predicates when
    ``[loop] cooldown_signature_keying_enabled`` is on.  Neither costs a new
    host or VCS call — both are live locals at the call site.

    *cooldown_bypassed* is whether the conflict cooldown was holding for
    this PR and one of its escape hatches admitted the lap anyway
    (proposal 137).  Such a lap re-routes and re-stamps the anchor, but it
    never bounced off the rung, so it must not consume one — see
    :meth:`_next_cooldown_attempts`.  ``tend_evolution_prs`` is the only
    production caller; the default keeps the parameter additive.
    """
    head_ref = pr.head_ref
    branch = head_ref
    # Proposal 137: whether this lap's reading can stand as a bounce at
    # all, derived once from the two facts the walk already measured.
    lap_measurable = engine._lap_signature_measurable(
        origin_head=origin_head, check_conclusion=check_conclusion,
    )
    # Set by the ahead-of-origin arm below when its ``push(branch)``
    # succeeds: that lap advanced ``origin/<branch>``, so the conflict
    # anchor it then stamps describes a head that no longer exists.
    pushed_this_lap = False
    try:
        engine._vcs.checkout(branch)
    except VCSError as exc:
        # Branch not present locally — try tracking it from origin.
        try:
            # ``checkout`` on git will auto-track ``origin/<branch>``
            # when a local branch of the same name does not exist.
            engine._vcs.checkout(f"origin/{branch}")
            engine._vcs.checkout_new_branch(branch)
        except VCSError:
            result.errors.append(
                f"{plan.plan_id}: could not checkout {branch}: {exc}"
            )
            return

    # CI-wait gate. Pushing to a PR branch invalidates any in-flight CI
    # run on the existing head, so we defer the main-merge until the
    # rollup has settled. ``""`` means
    # pending/unknown (the host hasn't reported a conclusion yet) —
    # log + return so the next tend cycle reprobes. ``"failure"``
    # means the existing head is already broken; route through
    # ``quality_gates_routed`` so a remediation task addresses it
    # rather than masking the failure under a fresh main-merge.
    # ``"none"`` means no CI is configured for this repo/branch — there
    # is nothing to wait for, so fall through to the main-merge instead
    # of deferring forever (#2109); only ``""`` defers.
    conclusion = check_conclusion
    if conclusion == "":
        result.deferred_for_ci += 1
        logger.info(
            "Tend PR #%s deferred: CI rollup still pending on %s",
            pr.number, branch,
        )
        with contextlib.suppress(VCSError):
            engine._vcs.checkout("main")
        return
    if (
        conclusion == "failure"
        and not _pr_needs_conflict_resolution(
            pr.merge_state_status, pr.mergeable,
        )
        and not _branch_has_local_commits_ahead(engine, branch)
    ):
        # Pre-merge head-CI-failure route: the existing PR head already
        # has a failing (non-advisory) CI check and the branch is clean
        # against main, so there is no conflict to resolve — route a
        # remediation that fixes the failing gate. A *conflicting* PR with
        # a failing head instead falls through to the merge below, which
        # detects the conflict and routes ``conflicts_routed``: head CI
        # cannot be meaningfully validated on a branch that will not
        # merge, so conflict routing takes precedence (#2440).
        #
        # Local-commits escape hatch (#2472), mirroring the ``is_current``
        # and conflict-cooldown short-circuits above: when a prior
        # ``quality_gates_routed`` remediation task already committed the
        # fix locally on this branch, the origin head CI is stale-red and
        # — if run()'s direct push did not deliver it — the fix never
        # reached origin to trigger a fresh CI run. When
        # ``_branch_has_local_commits_ahead(branch)`` is true we therefore
        # fall through to the merge → gates → push path below: the
        # post-merge gates validate the local fix before delivery (rewinding
        # to ``pre_merge_head`` on failure, #2456) and only ``push`` breaks
        # the deadlock by re-running CI on the delivered commits. Note that
        # the post-merge gates are themselves gated on
        # ``not _in_quality_fix_cooldown``: a fall-through that lands inside
        # the cooldown window pushes the local commits WITHOUT re-running
        # the local gates, so the post-push CI re-run is the sole validator
        # in that window. That is the correct resolution of the deadlock —
        # pushing is the only thing that re-runs CI on the stale-red head.
        #
        # Cooldown-throttle the routing so a persistently-broken head is
        # not re-routed on every tend cycle, mirroring the post-merge
        # gate-failure path below (#2110). The ``checkout("main")`` +
        # ``return`` stay OUTSIDE the guard: even within cooldown the
        # head's CI is still failing, so the branch must not fall through
        # to the merge/push path (merging would mask the failure).
        # Name the real failing check(s) so both the remediation directive
        # and the deferral log line below identify the actual failing gate
        # rather than misattributing it to ruff/mypy/tests (#2440). Lifted
        # out of the cooldown guard (#2836) so the names are available to
        # the log line on every lap, including within the cooldown window.
        try:
            failing = engine._host.get_pr_failing_check_names(pr.number)
        except VCSError:
            failing = []
        failing_summary = ", ".join(failing) or "unknown checks"
        if not _in_quality_fix_cooldown(
            engine,
            plan, now,
            head_sha=origin_head,
            base_sha=base_head_sha,
            ci_conclusion=check_conclusion,
        ):
            engine._memory.update_plan_tend_fields(
                plan.plan_id,
                # Stamp attempt-resolution time, not the iteration-start
                # ``now`` threaded through the loop, so the record marks
                # when this attempt actually happened (#1679). The check
                # above keeps the threaded ``now`` so every PR in one
                # iteration shares a single consistent cooldown threshold.
                last_quality_fix_attempt_at=datetime.now(UTC).isoformat(),
                # Proposal 137: the world this anchor is bouncing off, so a
                # later lap can release the cooldown the moment any of it
                # provably changes. Unconditional bookkeeping — nothing reads
                # it until ``cooldown_signature_keying_enabled`` is on.
                last_quality_fix_head_sha=origin_head,
                last_quality_fix_base_sha=base_head_sha,
                last_quality_fix_ci_conclusion=check_conclusion,
                # Proposal 137: one more lap has now bounced off this same
                # observation — or the observation moved and the ladder
                # restarts. Unconditional bookkeeping, like the stamps above.
                quality_fix_signature_attempts=_next_cooldown_attempts(
                    engine,
                    current=(origin_head, check_conclusion),
                    stamped=(
                        plan.last_quality_fix_head_sha,
                        plan.last_quality_fix_ci_conclusion,
                    ),
                    prior=plan.quality_fix_signature_attempts,
                    anchored=plan.last_quality_fix_attempt_at is not None,
                    # Proposal 137: this write is reached only when
                    # ``_in_quality_fix_cooldown`` returned ``False``, so
                    # the lap is a genuine bounce by construction — the
                    # only remaining question is whether it could measure
                    # the signature it stamps.
                    consumed=lap_measurable,
                ),
            )
            gate_summary = CI_FAILURE_ON_HEAD_SUMMARY
            if failing:
                gate_summary = (
                    f"{CI_FAILURE_ON_HEAD_SUMMARY}: {', '.join(failing)}"
                )
            result.quality_gates_routed.append(
                (
                    pr.number,
                    pr.title,
                    gate_summary,
                    plan.plan_id,
                    pr.body or "",
                    pr.url or "",
                    head_ref,
                ),
            )
            # Fail-loud (#2836): announce the load-bearing merge-refusal so
            # a stranded BEHIND PR with a red head is greppable in the log
            # rather than reading as a silent "no tend"; mirror the sibling
            # CI-pending / conflict-cooldown skip lines.
            logger.info(
                "Tend PR #%s: head CI failing (%s) — deferring main-merge, "
                "routing quality-gate remediation",
                pr.number, failing_summary,
            )
        else:
            logger.info(
                "Tend PR #%s: head CI failing (%s) — deferring main-merge, "
                "in quality-fix cooldown",
                pr.number, failing_summary,
            )
            _stamp_cooldown_skip(
                engine,
                pr_number=pr.number, cooldown="quality_fix", result=result,
            )
        with contextlib.suppress(VCSError):
            engine._vcs.checkout("main")
        return

    # Merge-queue cutover kill-switch (proposal 128). When
    # ``tend_main_merge_enabled`` is off, the tend phase no longer eagerly
    # re-syncs a behind PR with ``origin/main``: under a GitHub merge queue
    # each PR is tested once, at the front of the queue, against the state
    # it will actually land into, so the pre-emptive main-merge-and-push is
    # pure waste (and, now that CI is billed per runner-minute, a redundant
    # dollar cost). Every other tend role is preserved — quiet-PR reminders
    # and review-thread handling already ran in the outer tend loop ahead of
    # this method, and the CI-wait deferral plus the head-CI-failure
    # ``quality_gates_routed`` route above stay untouched. This branch keeps
    # the two remaining pre-merge roles and skips only the ``origin/main``
    # merge-and-push; the flag-on path below is byte-identical to the
    # pre-128 behaviour.
    if not engine._config.tend_main_merge_enabled:
        # Preserve delivery of a local remediation commit already on the
        # branch: when the branch has commits its own ``origin/<branch>``
        # head lacks (a fix a prior ``_run_task_phase`` committed that
        # run()'s direct push did not deliver), push it — a legitimate fix
        # delivery, not a redundant main-resync. ``_branch_has_local_commits
        # _ahead`` returns False on any VCS quirk, so a clean branch never
        # pushes here.
        if _branch_has_local_commits_ahead(engine, branch):
            # A diverged own-remote head (``origin/<branch>`` carries a
            # commit the local branch lacks — a GitHub "Update branch"
            # merge, or a remote merge orphaned when the local branch was
            # reset/recreated) must be reconciled before the push, or the
            # push is rejected non-fast-forward every lap and — being the
            # ``if`` arm — the ``elif`` conflict route never fires, so the
            # PR is stranded (#2508/#2568). Absorb ``origin/<branch>`` the
            # same way the flag-on divergence guard below does; a ``False``
            # return already recorded the error / routed the conflict and
            # checked the tree back out to ``main``. ``commits_ahead_behind``
            # never raises (the helper above proved the ref resolves) and
            # collapses a transient failure to ``(0, 0)``, so a spurious
            # ``behind == 0`` only skips the reconcile and lets the push
            # itself fail-soft into ``result.errors`` — never a wrong merge.
            _, behind = engine._vcs.commits_ahead_behind(
                branch, f"origin/{branch}",
            )
            if behind > 0:
                # Capture the pre-reconcile head so a gate-red reconcile
                # merge can be rewound rather than delivered to CI.
                pre_reconcile_head = engine._vcs.rev_parse(branch)
                if not _reconcile_remote_pr_head(
                    engine,
                    branch=branch, plan=plan, pr=pr,
                    head_ref=head_ref, result=result,
                ):
                    return
                # The reconcile just created a NEW merge commit absorbing
                # ``origin/<branch>`` — unlike the plain-ahead arm (whose
                # commits were validated at creation), this merge has never
                # faced the gates. Mirror the flag-on divergence guard,
                # which never delivers a gate-red merge to CI: validate it
                # locally and, on red, rewind the merge (restoring the
                # diverged state so a later lap re-gates through this same
                # arm rather than the ``behind == 0`` fast-forward push
                # blind-delivering it) and route a quality-gate remediation
                # instead of pushing. Cooldown-throttled like the
                # clean-merge gate below (#2110); within the cooldown the
                # gate is skipped but the unvalidated merge is still
                # rewound, never pushed.
                if _in_quality_fix_cooldown(
                    engine,
                    plan, now,
                    head_sha=origin_head,
                    base_sha=base_head_sha,
                    ci_conclusion=check_conclusion,
                ):
                    if pre_reconcile_head is not None:
                        with contextlib.suppress(VCSError):
                            engine._vcs.reset_hard(pre_reconcile_head)
                    with contextlib.suppress(VCSError):
                        engine._vcs.checkout("main")
                    return
                review = engine.run_quality_gates()
                if not review.passed:
                    if pre_reconcile_head is not None:
                        with contextlib.suppress(VCSError):
                            engine._vcs.reset_hard(pre_reconcile_head)
                    engine._memory.update_plan_tend_fields(
                        plan.plan_id,
                        last_quality_fix_attempt_at=datetime.now(
                            UTC,
                        ).isoformat(),
                        # Proposal 137: see the pre-merge route above.
                        last_quality_fix_head_sha=origin_head,
                        last_quality_fix_base_sha=base_head_sha,
                        last_quality_fix_ci_conclusion=check_conclusion,
                        quality_fix_signature_attempts=_next_cooldown_attempts(
                            engine,
                            current=(origin_head, check_conclusion),
                            stamped=(
                                plan.last_quality_fix_head_sha,
                                plan.last_quality_fix_ci_conclusion,
                            ),
                            prior=plan.quality_fix_signature_attempts,
                            anchored=(
                                plan.last_quality_fix_attempt_at is not None
                            ),
                            # Proposal 137: see the pre-merge route above —
                            # a genuine bounce by construction.
                            consumed=lap_measurable,
                        ),
                    )
                    result.quality_gates_routed.append(
                        (
                            pr.number,
                            pr.title,
                            engine._summarize_failed_gates(review),
                            plan.plan_id,
                            pr.body or "",
                            pr.url or "",
                            head_ref,
                        ),
                    )
                    with contextlib.suppress(VCSError):
                        engine._vcs.checkout("main")
                    return
            try:
                engine._vcs.push(branch)
            except VCSError as exc:
                result.errors.append(
                    f"{plan.plan_id}: push of local commits failed: {exc}"
                )
        # Route a conflict WITHOUT a working-tree merge: the in-memory
        # ``merge_tree_conflicts`` probe (the same one ``_observe_pr_staleness``
        # uses) detects a behind, not-cleanly-mergeable PR so remediation
        # still runs on the next iteration. The routed conflicted-file list
        # is advisory — the downstream remediation re-derives the conflict
        # on-branch — so an empty list is acceptable and leaves ``vcs.py``
        # untouched.
        elif engine._vcs.merge_tree_conflicts("origin/main", f"origin/{branch}"):
            result.conflicts_routed.append(
                (
                    pr.number,
                    pr.title,
                    [],
                    plan.plan_id,
                    pr.body or "",
                    pr.url or "",
                    head_ref,
                ),
            )
        # Else: a cleanly-behind PR with no local commits — a no-op. The
        # eager re-sync is exactly what this flag eliminates.
        with contextlib.suppress(VCSError):
            engine._vcs.checkout("main")
        return

    # Reset hard to the remote head so a prior failed local merge does
    # not leave stale state on the working tree. Gated on
    # ``commits_ahead_behind == 0`` so a remediation task's local
    # commits (which arrived via the FIFO pop in ``_run_task_phase``
    # and committed on this branch) are preserved through the merge
    # and the trailing ``push``.
    # ``commits_ahead_behind`` never raises — it collapses a transient
    # failure (timeout, lock, garbled rev-list output) AND a genuinely-even
    # branch both to ``(0, 0)`` (#2457 hit the same ambiguity in
    # ``_probe_pr_merge_block``). A reset on a *false* zero would silently
    # destroy a prior remediation task's local conflict resolution, leaving
    # nothing for the trailing ``push`` to deliver. So trust ``ahead == 0``
    # only when origin/<branch> resolves to the SAME commit as the local
    # branch; any disagreement (missing ref, differing HEAD, unresolvable
    # local branch) is indeterminate → fail SAFE and skip the reset to
    # preserve any local resolution.
    ahead: int | None
    ahead, behind = engine._vcs.commits_ahead_behind(
        branch, f"origin/{branch}",
    )
    if ahead == 0:
        local_head = engine._vcs.rev_parse(branch)
        remote_head = engine._vcs.rev_parse(f"origin/{branch}")
        if (
            local_head is None
            or remote_head is None
            or local_head != remote_head
        ):
            logger.warning(
                "Tend PR #%s: ahead/behind read (0, 0) but %s and "
                "origin/%s disagree; skipping pre-merge reset to "
                "preserve any local resolution",
                pr.number, branch, branch,
            )
            ahead = None
    if ahead == 0:
        try:
            engine._vcs.reset_hard(f"origin/{branch}")
        except VCSError:
            logger.warning(
                "Could not reset %s to origin/%s — proceeding anyway",
                branch, branch,
            )
    elif ahead is None:
        # Indeterminate — preserve local work, per the fail-safe above:
        # the SHA check proved local and ``origin/<branch>`` heads
        # differ, but not in which direction. The reset is correctly
        # skipped, yet doing *nothing* here leaves a branch that is
        # genuinely behind its remote permanently unreconciled — the
        # trailing ``push`` is then rejected non-fast-forward on every
        # tend lap (#2568). Absorb ``origin/<branch>`` exactly like the
        # diverged arm below.
        if not _reconcile_remote_pr_head(
            engine,
            branch=branch, plan=plan, pr=pr,
            head_ref=head_ref, result=result,
        ):
            return
        # Reconcile succeeded: ``origin/<branch>`` is now an ancestor
        # of the local branch, so the trailing push is a fast-forward.
        # ``behind`` is deliberately NOT cleared here: its only
        # downstream reader — the local-resolution push gate in the
        # ``origin/main``-conflict path — also requires
        # ``ahead is not None``, and ``ahead`` stays ``None`` on this
        # arm, keeping that push closed on an unresolved ref by design.
    else:
        logger.info(
            "Tend PR #%s: local remediation commits present "
            "(ahead=%d); skipping pre-merge reset on %s",
            pr.number, ahead, branch,
        )
        # The branch's own remote head has diverged: ``origin/<branch>``
        # carries a commit the local branch lacks (a GitHub "Update
        # branch" merge, or a remote merge orphaned when the local branch
        # was later reset/recreated from a different base) while local
        # remediation commits are simultaneously ahead. ``reset_hard``
        # above was correctly skipped to preserve the local commits, but
        # nothing has reconciled the foreign remote commit — so the
        # trailing ``push`` is rejected non-fast-forward every cycle and
        # the resolution never lands (#2508). Absorb ``origin/<branch>``
        # into the local branch (mirroring the ``origin/main`` merge
        # below) so the push is a true fast-forward. Force-push stays
        # banned (#2280); this only adds the missing "reconcile the remote
        # PR-branch head" step.
        # (``ahead == 0`` with ``behind > 0`` already heals via the
        # ``reset_hard(origin/<branch>)`` above; ``ahead is None`` heals
        # via the indeterminate-arm merge above (#2568); ``ahead > 0`` —
        # this branch — needs the explicit merge here.)
        if behind > 0:
            if not _reconcile_remote_pr_head(
                engine,
                branch=branch, plan=plan, pr=pr,
                head_ref=head_ref, result=result,
            ):
                return
            # Reconcile succeeded: the local branch now contains
            # ``origin/<branch>`` as an ancestor, so it is no longer
            # diverged from its own remote — only strictly ahead. Clear
            # ``behind`` to reflect that, so the local-resolution push in
            # the ``origin/main``-conflict path below (gated on
            # ``behind == 0``) fires this cycle rather than waiting for the
            # next tend to re-measure (#2508).
            behind = 0

    # Record the pre-merge HEAD (including any local remediation commits)
    # so a post-merge gate failure can rewind only the failed merge rather
    # than blunt-resetting to ``origin/<branch>`` and discarding unpushed
    # local work. ``None`` (indeterminate ref) triggers a fail-safe below.
    pre_merge_head = engine._vcs.rev_parse(branch)

    # Host-flagged conflicts are no longer trusted blindly: the local
    # merge below is the source of truth.  A host that flagged a stale
    # ``CONFLICTING`` status but merges cleanly here lands on the
    # success path (gates + push); only a genuine local conflict in the
    # ``not merge.success`` branch routes a remediation directive.
    merge_msg = f"maestro: merge main into {branch}"
    try:
        merge = engine._vcs.merge_from(
            "origin/main", message=merge_msg,
        )
    except VCSError as exc:
        result.errors.append(
            f"{plan.plan_id}: merge failed: {exc}"
        )
        with contextlib.suppress(VCSError):
            engine._vcs.checkout("main")
        return

    if not merge.success:
        # Unexpected conflict on a branch the host adapter called clean.
        # Abort and route the same way as the flagged-conflict path so
        # the next iteration's ``generate_task`` produces a fix task.
        engine._vcs.abort_merge()

        # Deliver a gate-validated local resolution before routing the
        # conflict (#2479). When the branch strictly extends its OWN
        # remote head (local commits ahead, not diverged), ship that head
        # now. Otherwise a hot-file branch whose local resolution
        # re-conflicts against a moving ``main`` never lands: the only
        # push sits behind a clean fresh main-merge, so the resolved-but-
        # unpushed head stays frozen and every tend cycle re-routes a
        # remediation task with zero PR progress. Pushing here advances
        # the PR head so CI re-runs on real progress and the host
        # recomputes mergeability; the conflict that is still routed below
        # then spans only ``main``'s movement since this push — bounded,
        # not a treadmill. Force-push stays banned (#2280).
        # ``ahead``/``behind`` were measured above against
        # ``origin/<branch>`` (the branch's own remote head), NOT main:
        # the stranded branch is behind main but strictly ahead of its
        # remote. ``ahead is None`` is the indeterminate fail-safe from
        # the pre-merge reset guard — never push on an unresolved ref.
        #
        # Unlike the clean-merge gate run below, this gate is NOT
        # cooldown-throttled — but it is naturally bounded: a successful
        # push advances ``origin/<branch>`` so the next tend cycle reads
        # ``ahead == 0`` and skips this block entirely. Re-running the
        # gates each cycle happens only when the push never lands — either
        # a persistently-failing push (rare — a diverged
        # ``origin/<branch>`` head is now reconciled upstream before this
        # point (#2508), so the push is a fast-forward
        # rather than rejected non-fast-forward) OR persistently-failing
        # gates (``review.passed`` stays False, so no push fires and
        # ``ahead`` stays > 0). Both recurrences are caught by the
        # remediation-repetition gate downstream, which dequeues the PR
        # once the routed conflict task makes no progress.
        if ahead is not None and ahead > 0 and behind == 0:
            review = engine.run_quality_gates()
            if review.passed:
                try:
                    engine._vcs.push(branch)
                except VCSError as exc:
                    result.errors.append(
                        f"{plan.plan_id}: push of local resolution "
                        f"failed: {exc}"
                    )
                else:
                    pushed_this_lap = True

        # Proposal 099: stamp the base this cooldown anchor raced against so
        # ``_in_conflict_cooldown`` can release it on a later base advance.
        # Proposal 137 completes that record with the branch head and the CI
        # rollup, and reuses the base the walk already measured for this PR
        # this lap rather than a second ``rev_parse("origin/main")``; the
        # fall-back keeps the pre-137 reading when the walk could not measure
        # one, so the proposal-099 release never regresses.
        tend_fields: dict[str, object] = {
            "last_conflict_resolution_attempt_at": now.isoformat(),
            "last_conflict_resolution_base_sha": (
                base_head_sha or engine._vcs.rev_parse("origin/main")
            ),
            "last_conflict_resolution_head_sha": origin_head,
            "last_conflict_resolution_ci_conclusion": check_conclusion,
            # Proposal 137: the lap count this anchor's backoff window is
            # derived from — ``0`` when the signature moved, one more rung
            # when it did not, and *unchanged* when this lap did not
            # genuinely bounce off the cooldown. The rule enforced here:
            # a rung is consumed only by a lap the cooldown actually
            # governed (no escape hatch admitted it), that did not itself
            # move the remote head, and that could measure the signature
            # it is stamping. A bypassed, pushing or unmeasurable lap
            # leaves the ladder where it was — neither reset (nothing was
            # proven to have changed) nor advanced (nothing was proven to
            # have been retried).
            "conflict_resolution_signature_attempts": (
                _next_cooldown_attempts(
                    engine,
                    current=(origin_head, base_head_sha, check_conclusion),
                    stamped=(
                        plan.last_conflict_resolution_head_sha,
                        plan.last_conflict_resolution_base_sha,
                        plan.last_conflict_resolution_ci_conclusion,
                    ),
                    prior=plan.conflict_resolution_signature_attempts,
                    anchored=(
                        plan.last_conflict_resolution_attempt_at is not None
                    ),
                    consumed=(
                        not cooldown_bypassed
                        and not pushed_this_lap
                        and lap_measurable
                    ),
                )
            ),
        }
        engine._memory.update_plan_tend_fields(plan.plan_id, **tend_fields)
        result.conflicts_routed.append(
            (
                pr.number,
                pr.title,
                list(merge.conflicted_files),
                plan.plan_id,
                pr.body or "",
                pr.url or "",
                head_ref,
            ),
        )
        with contextlib.suppress(VCSError):
            engine._vcs.checkout("main")
        return

    if not _in_quality_fix_cooldown(
        engine,
        plan, now,
        head_sha=origin_head,
        base_sha=base_head_sha,
        ci_conclusion=check_conclusion,
    ):
        review = engine.run_quality_gates()
        if not review.passed:
            # Proposal 124: attribute the post-merge red against this PR's
            # diff and a baseline of the ``origin/main`` head ``tend_evolution
            # _prs`` already warmed at the top of the lap — ``plan.files_
            # affected`` is the diff side, so no new VCS diff call is needed.
            # Flag-off (or with no observation) ``attribution`` stays ``None``
            # and every branch below is byte-identical to the pre-124 rewind.
            attribution: Attribution | None = None
            if (
                engine._config.gate_observation_enabled
                and review.observation is not None
            ):
                attribution = attribute(
                    review.observation,
                    plan.files_affected,
                    engine._observe_baseline(
                        engine._vcs.rev_parse("origin/main") or "",
                    ),
                )
            if attribution == "INHERITED":
                # The red belongs to origin/main, not this PR's diff. Hold the
                # clean merge — skip the reset_hard, exactly as the
                # indeterminate-``pre_merge_head`` arm below already does — and
                # route a remediation whose summary names the base failures
                # rather than the coarse two-word gate label, so the fixer is
                # pointed at the base rather than this PR's own work.
                observation = review.observation
                identities = (
                    render_identities(
                        observation,
                        limit=_INHERITED_TEND_SUMMARY_IDENTITY_LIMIT,
                        max_chars=_INHERITED_TEND_SUMMARY_IDENTITY_CHARS,
                    )
                    if observation is not None
                    else ""
                )
                logger.info(
                    "Tend PR #%s: post-merge gate RED but INHERITED from "
                    "origin/main (%s) for %s; holding the clean merge (no "
                    "reset), routing base-repair remediation",
                    pr.number, identities or "base failures", branch,
                )
                gate_summary = (
                    f"{Marker.INHERITED_RED} inherited base red from "
                    f"origin/main: {identities or 'base failures'}"
                )
            else:
                # Rewind only the just-created (clean) merge, preserving any
                # local remediation commits. When ``ahead == 0`` this equals
                # ``origin/<branch>`` so the no-remediation path is unchanged.
                if pre_merge_head is not None:
                    with contextlib.suppress(VCSError):
                        engine._vcs.reset_hard(pre_merge_head)
                else:
                    # Indeterminate pre-merge HEAD — fail SAFE, mirroring the
                    # pre-merge gate: leave the clean merge in place rather
                    # than risk discarding unpushed remediation commits via
                    # ``origin/<branch>``.
                    logger.info(
                        "Tend PR #%s: pre-merge HEAD unknown for %s; "
                        "skipping post-merge gate-failure reset to "
                        "preserve local work",
                        pr.number, branch,
                    )
                gate_summary = engine._summarize_failed_gates(review)
            engine._memory.update_plan_tend_fields(
                plan.plan_id,
                # Fresh completion timestamp: ``run_quality_gates()`` above
                # consumes real wall-clock time, so recording the stale
                # iteration-start ``now`` would understate the cooldown
                # (#1679). The cooldown check above still reads the threaded
                # ``now`` for one consistent per-iteration threshold.
                last_quality_fix_attempt_at=datetime.now(UTC).isoformat(),
                # Proposal 137: see the pre-merge route above.
                last_quality_fix_head_sha=origin_head,
                last_quality_fix_base_sha=base_head_sha,
                last_quality_fix_ci_conclusion=check_conclusion,
                quality_fix_signature_attempts=_next_cooldown_attempts(
                    engine,
                    current=(origin_head, check_conclusion),
                    stamped=(
                        plan.last_quality_fix_head_sha,
                        plan.last_quality_fix_ci_conclusion,
                    ),
                    prior=plan.quality_fix_signature_attempts,
                    anchored=plan.last_quality_fix_attempt_at is not None,
                    # Proposal 137: this write is reached only when
                    # ``_in_quality_fix_cooldown`` returned ``False``, so
                    # the lap is a genuine bounce by construction — the
                    # only remaining question is whether it could measure
                    # the signature it stamps.
                    consumed=lap_measurable,
                ),
            )
            result.quality_gates_routed.append(
                (
                    pr.number,
                    pr.title,
                    gate_summary,
                    plan.plan_id,
                    pr.body or "",
                    pr.url or "",
                    head_ref,
                ),
            )
            with contextlib.suppress(VCSError):
                engine._vcs.checkout("main")
            return

    try:
        engine._vcs.push(branch)
    except VCSError:
        # Origin may have advanced between the divergence measurement
        # above and this push (#2568), mirroring run()'s
        # ``push_delivered`` fetch+retry. The fetch is best-effort —
        # even when the retry below still fails, it refreshes
        # ``origin/<branch>`` so the next lap's divergence measurement
        # reads fresh state instead of repeating a stale-ref mistake.
        try:
            engine._vcs.fetch("origin")
        except VCSError:
            logger.warning(
                "Fetch before tend push retry failed for %s",
                branch, exc_info=True,
            )
        try:
            engine._vcs.push(branch)
        except VCSError as exc:
            result.errors.append(
                f"{plan.plan_id}: push after clean merge failed: {exc}"
            )
            with contextlib.suppress(VCSError):
                engine._vcs.checkout("main")
            return
    result.merged_updated += 1
    with contextlib.suppress(VCSError):
        engine._vcs.checkout("main")

def _reconcile_remote_pr_head(
    engine: EvolutionEngine,
    *,
    branch: str,
    plan: EvolutionPlan,
    pr: Any,
    head_ref: str,
    result: TendPRsResult,
) -> bool:
    """Absorb ``origin/<branch>`` into the checked-out local branch.

    Shared by the indeterminate (``ahead is None``, #2568) and
    diverged (``ahead > 0 and behind > 0``, #2508) arms of the
    pre-merge divergence guard in :meth:`_tend_single_pr`, so their
    error strings and routed conflict tuples cannot drift. The merge
    is non-destructive either way: a no-op when the remote commits
    are already contained, an absorb when they are not. Force-push
    stays banned (#2280).

    Returns ``True`` when ``origin/<branch>`` is now an ancestor of
    the local branch, so tending may continue and the trailing
    ``push`` is a fast-forward. Returns ``False`` when tending this
    PR must stop: a hard :class:`VCSError` is recorded on
    ``result.errors``; a conflict with local work aborts the merge
    and routes ``result.conflicts_routed`` so the next
    ``generate_task`` produces a fix task — never push a diverged
    head, never force-push, never retry the rejected push. Both
    stop paths check the working tree back out to ``main``.
    """
    reconcile_msg = f"maestro: reconcile diverged origin/{branch}"
    try:
        reconcile = engine._vcs.merge_from(
            f"origin/{branch}", message=reconcile_msg,
        )
    except VCSError as exc:
        result.errors.append(
            f"{plan.plan_id}: reconcile of diverged "
            f"origin/{branch} failed: {exc}"
        )
        with contextlib.suppress(VCSError):
            engine._vcs.checkout("main")
        return False
    if not reconcile.success:
        engine._vcs.abort_merge()
        result.conflicts_routed.append(
            (
                pr.number,
                pr.title,
                list(reconcile.conflicted_files),
                plan.plan_id,
                pr.body or "",
                pr.url or "",
                head_ref,
            ),
        )
        with contextlib.suppress(VCSError):
            engine._vcs.checkout("main")
        return False
    return True

def _observe_pr_staleness(engine: EvolutionEngine, branch: str) -> PrStaleness:
    """Observe how stale PR head *branch* is against ``origin/main``.

    The :class:`~maestro.pr_facts.PrStaleness` adapter for
    :meth:`tend_evolution_prs` (proposal 095): wraps the pure
    :func:`~maestro.pr_facts.observe_pr_staleness` with this engine's
    real git seams, mirroring how
    :meth:`MaestroAgent.observe_pr_readiness` wraps its pure observer.
    Both oracles measure the *remote* head ``origin/<branch>`` — the
    freshly-fetched ref the tend loop already reads via
    ``rev_parse(f"origin/{branch}")`` — because the gate runs before
    :meth:`_tend_single_pr` checks the branch out locally:

    * ``commits_behind`` is the ``behind`` element of
      ``commits_ahead_behind("origin/<branch>", "origin/main")``;
    * ``cleanly_mergeable`` is ``not merge_tree_conflicts("origin/main",
      "origin/<branch>")`` — an in-memory ``git merge-tree`` probe.

    A third, argument-less ``base_sha_fn`` oracle supplies
    :attr:`~maestro.pr_facts.PrStaleness.base_head_sha` — the
    ``origin/main`` head SHA this observation raced against (proposal 099
    step 2) — from the same local-git seam this adapter already owns via
    ``rev_parse("origin/main")``. Unlike the two branch-taking oracles it
    takes no branch, because the base is ``origin/main`` regardless of the
    PR branch observed.

    All three VCS calls are best-effort (each swallows ``VCSError`` and
    returns ``(0, 0)`` / ``False`` / ``None``), so this makes no host or SDK
    call and never raises.
    """
    return observe_pr_staleness(
        branch,
        commits_behind_fn=lambda b: engine._vcs.commits_ahead_behind(
            f"origin/{b}", "origin/main",
        )[1],
        merge_clean_fn=lambda b: not engine._vcs.merge_tree_conflicts(
            "origin/main", f"origin/{b}",
        ),
        base_sha_fn=lambda: engine._vcs.rev_parse("origin/main"),
    )

def _branch_has_local_commits_ahead(engine: EvolutionEngine, branch: str) -> bool:
    """Return True when local *branch* has commits not on ``origin/<branch>``.

    Used by ``tend_evolution_prs`` to detect remediation commits
    that a prior ``_run_task_phase`` left on the PR branch. When
    present, the CLEAN/MERGEABLE short-circuit must NOT skip the
    tend body — if run()'s direct push did not deliver them, the
    commits would otherwise sit unpushed until Tend PR delivers them.
    Probes via ``rev_parse`` first
    to avoid a ``commits_ahead_behind`` call for branches that
    aren't even checked out locally; returns False on any VCS
    error so a quirky working tree does not cause a re-tend.
    """
    try:
        if engine._vcs.rev_parse(branch) is None:
            return False
        ahead, _behind = engine._vcs.commits_ahead_behind(
            branch, f"origin/{branch}",
        )
    except VCSError:
        return False
    return ahead > 0

def _has_unconsumed_awaiting_push_verdict(
    engine: EvolutionEngine, pr_number: int, observed_head: str | None,
) -> bool:
    """Whether #*pr_number*'s latest remediation deferred its push to tend.

    A third conflict-cooldown escape hatch (#2825), sibling to
    :meth:`_branch_has_local_commits_ahead`. A CONFLICTING PR whose routed
    remediation *resolved the conflict on-branch* but then deferred the
    push back to tend (:attr:`Marker.AWAITING_TEND_PUSH`, "awaiting tend
    push") clears neither existing hatch: the #2821 head-advance release
    keys on ``origin/<branch>`` which never advances while the resolution
    sits un-pushed, and once the on-branch commit is reset away between
    phases the local-commits hatch stops firing too — so the fixed-window
    cooldown strands the PR for the full window. Tend is the designated
    deliverer of that deferred push, so the cooldown skip must fall through
    to ``_tend_single_pr`` whose trailing ``push`` delivers it.

    A verdict is *unconsumed* when the head it was recorded against
    (:attr:`TaskRecord.pr_head_sha`) still equals the PR's current observed
    origin head *observed_head* (the value :meth:`PRLedger.observe_head`
    stored this lap). A successful tend push advances ``origin/<branch>`` —
    tripping the #2821 release that nulls
    ``last_conflict_resolution_attempt_at`` — so the bypass stops once the
    resolution is delivered. It does NOT bound re-routing on its own,
    though: when the resolution was reset away there is nothing to push, so
    the head never advances and the verdict stays unconsumed, and the
    bypass re-fires on the same head every lap until a push lands or a
    downstream gate (costume-signature, remediation-repetition,
    escalation-bridge) bounds the lineage. Returns False on any task-store
    read error so a quirky store never forces a re-tend.
    """
    if not observed_head:
        return False
    try:
        recent = engine._memory.get_recent_tasks(
            _AWAITING_PUSH_VERDICT_LOOKBACK,
        )
    except OSError:
        return False
    # ``get_recent_tasks`` yields oldest-first, so the last matching lap in
    # iteration order is the most recent dedicated ``Remediate PR #N`` task.
    latest: TaskRecord | None = None
    for rec in recent:
        if pr_number_from_remediation_goal(rec.goal or "") == pr_number:
            latest = rec
    if latest is None or latest.pr_head_sha != observed_head:
        return False
    blob = (
        f"{latest.result_summary or ''}\n{latest.key_findings or ''}".lower()
    )
    # Match both push markers (proposal 091 step 6): a lap that delivered
    # (``DELIVERED_AWAITING_MERGE``) then had its head reset back still
    # deserves the conflict-cooldown bypass so Tend PR re-enters and
    # re-pushes. The ``latest.pr_head_sha != observed_head`` guard above
    # already limits the bypass to same-head laps.
    #
    # ``OWNED_PUSH_AVAILABLE`` joins them (proposal 098): §5-push now stamps
    # that marker on the very laps this hatch exists for — a resolution
    # sitting un-pushed on the branch — so without it a failed-push lineage
    # would silently lose the #2825 bypass the moment the marker swapped.
    #
    # ``PUSH_UNCONFIRMED`` joins them (proposal 099): a no-op push (exit 0,
    # origin un-advanced) leaves the resolution sitting un-pushed on the
    # branch — precisely the #2825 condition — so it earns the same
    # conflict-cooldown bypass as its OWNED_PUSH_AVAILABLE sibling.
    return (
        Marker.AWAITING_TEND_PUSH in blob
        or Marker.DELIVERED_AWAITING_MERGE in blob
        or Marker.OWNED_PUSH_AVAILABLE in blob
        or Marker.PUSH_UNCONFIRMED in blob
    )

def _summarize_failed_gates(review: QualityGateResult) -> str:
    """Compact one-line summary of which gates failed for the directive prompt."""
    failed = [
        label for label, ok in (
            ("ruff", review.ruff_ok),
            ("mypy", review.mypy_ok),
            ("tests", review.tests_ok),
        ) if not ok
    ]
    if not failed:
        return "quality gates failed"
    return ", ".join(failed) + " failed"

async def _reconcile_orphan_branches(
    engine: EvolutionEngine,
    pr_states: dict[str, Any],
    result: TendPRsResult,
    *,
    settled_oracle: Callable[[int], MergeTruth],
) -> None:
    """Detect and repair remote evolution branches without a matching open PR.

    A *merged* PR is no longer open, so its lingering branch scans as an
    orphan from the moment it merges until the iteration-cadence
    ``check_merged_plans`` phase flips the plan out of ``implemented`` —
    a window that can span hours under budget throttle (#2569).  To avoid
    re-flagging already-landed work during that lag, an ``implemented``
    orphan carrying a ``pr_url`` whose PR has merged is recognised inline
    through *settled_oracle* — the merge-truth chokepoint
    (:meth:`~maestro.agent.MaestroAgent.is_pr_settled`) whose
    :attr:`~maestro.pr_facts.MergeTruth.branch_landed` answers the same
    "did it merge?" question ``check_merged_plans`` asks, but off the shared
    provenance-stamped, per-iteration-memoized door (proposal 087) rather
    than a raw host read: the orphan warning and repair credit are
    suppressed, but the plan is *not* flipped here.  Leaving it
    ``implemented`` lets ``check_merged_plans`` own the flip and the merge
    side-effects it drives (``VersionRecord``, ``evolution_count`` bump,
    milestone journal); flipping inline would move the plan out of
    ``implemented``, making that phase skip it and silently drop those
    side-effects.  *settled_oracle* is required (not defaulted), so this
    recognition can never be silently skipped by a caller that forgot to
    thread the chokepoint down.

    For plans with ``status=='implemented'`` and no ``pr_url`` (the
    crash-between-implement-and-create_pr case), resets the plan to
    ``approved`` so ``_implement_approved_plans`` picks it up on the next
    iteration — unless the branch is already an ancestor of
    ``origin/main`` (work already landed), in which case it is flipped to
    ``merged`` so no re-implementation session runs for landed work.  There
    is no PR to defer to for this arm, so the flip is terminal.
    Plans already in ``approved`` status are logged but left as-is since
    the re-implementation pipeline already handles them.

    ``orphans_repaired`` counts only genuine state changes this scan makes
    itself — the no-``pr_url`` reset-to-``approved`` or already-landed
    flip-to-``merged``.  The merged-``pr_url`` case is deferred to
    ``check_merged_plans``, not repaired here, so it is never counted.
    """
    try:
        remote = engine._vcs.list_remote_branches("origin")
    except Exception:
        logger.warning("orphan scan: list_remote_branches failed", exc_info=True)
        return

    evolution_branches = {
        b for b in remote if b.startswith("maestro/evolve/")
    }
    pr_heads = set(pr_states)
    orphans = evolution_branches - pr_heads
    if not orphans:
        return
    for branch in sorted(orphans):
        plan_id = branch.removeprefix("maestro/evolve/")
        plan = engine._memory.get_plan(plan_id)
        if plan is None or plan.status not in ("approved", "implemented"):
            continue
        # A merged PR is no longer open, so its lingering branch looks
        # orphaned until the iteration-cadence check_merged_plans phase
        # catches up. Recognise the merge here through the shared
        # is_pr_settled chokepoint (proposal 087) and suppress the orphan
        # warning + false repair credit — but do NOT flip the plan. Leaving
        # it 'implemented'+pr_url lets check_merged_plans own the flip AND
        # the merge side-effects it drives in agent.py (VersionRecord,
        # evolution_count bump, milestone journal); flipping inline would
        # move the plan out of 'implemented' so that phase skips it and
        # silently drops those side-effects (#2569 review concern).
        if plan.status == "implemented" and plan.pr_url:
            try:
                pr_number = engine._pr_number_from_url(plan.pr_url)
            except ValueError:
                pr_number = None
            if pr_number is not None:
                try:
                    merged = settled_oracle(pr_number).branch_landed
                except Exception:
                    # Isolate a merge-read failure to this branch — fall
                    # through to the log-only flag arm rather than
                    # aborting the whole scan.  Absorbing the fail-loud
                    # chokepoint is safe for the same reason it is in
                    # ``check_merged_plans``: this arm authors no verdict
                    # (no status flip, no repair credit), it only forgoes
                    # suppressing one orphan warning.
                    logger.warning(
                        "orphan scan: merge check failed for %s",
                        branch, exc_info=True,
                    )
                else:
                    if merged:
                        # Merged but not yet flipped by check_merged_plans:
                        # defer the flip (and its side-effects) to that phase
                        # and neither warn nor credit a repair for
                        # already-landed work.
                        logger.info(
                            "Orphan evolution branch %s belongs to merged "
                            "PR #%s — deferring flip to check_merged_plans",
                            branch, pr_number,
                        )
                        continue
        # Stranded 'implemented' plans with no PR need resetting so
        # _implement_approved_plans can re-run them — unless the branch is
        # already on origin/main (work landed), in which case flip to
        # 'merged' so no re-implementation session runs for landed work.
        if plan.status == "implemented" and not plan.pr_url:
            if engine._vcs.is_ancestor("origin/" + branch, "origin/main"):
                engine._memory.update_plan_status(plan.plan_id, "merged")
                logger.info(
                    "Orphan evolution branch %s already on origin/main "
                    "(plan status=implemented, no pr_url) — flipped to "
                    "merged; work already landed",
                    branch,
                )
            else:
                engine._memory.update_plan_status(plan.plan_id, "approved")
                logger.warning(
                    "Orphan evolution branch %s has no open PR "
                    "(plan status=%s, no pr_url) — reset to approved "
                    "for re-implementation",
                    branch, plan.status,
                )
            result.orphans_repaired += 1
        else:
            logger.warning(
                "Orphan evolution branch %s has no open PR "
                "(plan status=%s) — flagged for re-implementation",
                branch, plan.status,
            )

"""PR-remediation cluster carved out of ``maestro.agent`` (proposal 068, step 1).

Holds the tend-phase feedback router, the dequeue-face remediation gates,
and the pre-remediation preflight behind :class:`PrRemediationController`,
plus (proposal 147 Step 8) the mechanical stand-down disposal, the
owned-branch push helpers and remediation outcome recording.
``MaestroAgent`` keeps thin delegating shims with the historical method
names; the controller dispatches back through those agent attributes at
call time so instance-level monkey-patches in the existing test suite
keep intercepting the flow (the staged-extraction contract: existing
tests pass unchanged against the delegating shim).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from maestro import audit_checks, audit_checks_phantom, deed_ledger, escalation_bridge, gate_capacity, red_root
from maestro.ci_diagnosis import diagnose_ci_failure, render_ci_failure_section
from maestro.consciousness import PendingPRRequest, SelfModel
from maestro.escalation_bridge import UnlandedCureHandoff, deliver_handoff_cfg
from maestro.evolution_tend import is_ci_failure_on_head_summary
from maestro.gate_observation import Attribution, attribute, render_identities
from maestro.loop.implementation import _GATE_RECORD_IDENTITY_CHARS, _GATE_RECORD_IDENTITY_LIMIT
from maestro.memory import EvolutionPlan, TaskRecord
from maestro.pr_facts import (
    MergeTruth,
    Salvage,
    ci_state_from_conclusion,
    classify_merge_blocker_label,
    verify_push_advanced_remote_head,
)
from maestro.pr_owed_act import OwedAct
from maestro.pr_remediation_state import (
    Marker,
    PrRemediationDisposition,
    PrRemediationState,
    classify_pr_remediation_disposition,
)
from maestro.prompts.acceptance import format_acceptance_criteria_block
from maestro.prompts.remediation import (
    merge_truth_first_preamble,
    size_pin_cure_hint,
)
from maestro.red_root import classify_ci_conclusions
from maestro.remediation_tracker import (
    _UNLANDED_REMEDIATION_CYCLES,
    BLOCKER_OWNERSHIP_EXTERNAL,
    HOST_CLOSED_UNMERGED,
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CI_FAILING,
    MERGE_BLOCK_CI_PENDING,
    MERGE_BLOCK_NONE,
    PR_ADMISSION_CHANNEL_TEND,
    BranchReachability,
    StandDownVerdict,
    classify_blocker_ownership,
    classify_branch_reachability,
    classify_committed_unpushed_push,
    classify_host_pr_terminal,
    classify_noop,
    classify_owned_blocker,
    classify_remediation_outcome_state,
    count_branch_resolved_partials,
    find_stand_down,
    has_live_stand_down,
    leading_same_sha_streak,
    measure_no_progress_laps,
    measure_unlanded_remediations,
    mechanical_standdown_verdict,
    pr_number_from_remediation_goal,
    record_stand_down,
    release_stand_downs,
)
from maestro.session_landing import (
    UNDELIVERED_STATUSES,
    LandOutcome,
    LandSeams,
    classify_gate_red,
    land_uncommitted_edits,
    owned_push_seam,
)
from maestro.standdown_validity import StanddownClaim
from maestro.types import ACCEPTANCE_CRITERIA_MAX_ITEMS, TaskSource
from maestro.vcs import VCSError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from maestro.agent import MaestroAgent
    from maestro.evolution import QualityGateResult
    from maestro.evolution_tend import OverflowBugFinding, TendPRsResult

    # One dequeue gate: given the controller and the popped request's context,
    # decide (await) whether this gate fires for the entry. Bound to the agent
    # delegating shims at call time so instance monkey-patches keep intercepting.
    _DequeueGateFn = Callable[
        ["PrRemediationController", "_DequeueContext"], Awaitable[bool],
    ]

logger = logging.getLogger("maestro")


class RedRootDiagnosisError(Exception):
    """The action-zero red-root diagnosis crashed (#4100, #4216).

    Raised ``from`` the original failure so the preflight's broad
    degrade-to-``False`` handler can let it through by type.
    """


@dataclass
class _PRRemediationAccumulator:
    """In-process aggregator for one PR routed by ``tend_evolution_prs``.

    Multiple categories firing on the same PR within a single tend
    pass collapse into one accumulator (and thus one
    ``pending_pr_requests`` entry); ``problems`` holds the per-reason
    blocks in arrival order.
    """

    pr_number: int
    pr_title: str
    plan_id: str
    pr_body: str
    pr_url: str
    head_ref: str
    problems: list[str] = field(default_factory=list)
    #: The ``ci_failure_on_head`` gate summary (proposal 102), carrying the
    #: failing check names in its ``": <names>"`` suffix. Set from the
    #: ``quality_gates_routed`` loop when the entry is a pre-merge head-CI
    #: failure; empty for every other route. Read at the compose site to gate
    #: the one deferred failing-run log read behind the diagnosis flag.
    ci_failure_summary: str = ""


_REMEDIATION_DEDUP_PREFIX = "Remediate PR #"

#: Canonical own-branch self-delivery push command shape (proposal 093/096).
#: The single source for the push form the loop *instructs* — interpolated into
#: :func:`_delivery_note` — so it stays the exact plain (non-force) push the
#: action gate is proven to *allow* by the push-conformance golden test
#: (``tests/test_push_conformance.py``). ``{branch}`` is the ``maestro/evolve/*``
#: PR head (named in the prompt's Remediation Guidance).
OWN_BRANCH_PUSH_TEMPLATE = "git push origin HEAD:{branch}"


def _delivery_note() -> str:
    """Return the trailing delivery-note instruction for a remediation prompt.

    Shared by every PR-remediation prompt so the delivery wording cannot drift
    across the ~7 prompt sites. Owned-PR self-delivery is unconditional
    (proposal 093): the task-execution action gate always widens to the
    ``maestro/evolve/*`` head of the agent's own open PR, so the note always
    instructs the agent to deliver the fix itself with a plain (non-force) push
    to the PR branch. It authorizes only that push: never a force-push, a
    ``gh pr merge``, or a push to ``main``/``master`` — the human-in-the-loop
    approval gate still owns the merge. The loop's post-task push stays a
    no-op-safe idempotent backstop *behind* this push, so the two cannot
    double-deliver (pushing an up-to-date branch is a no-op).
    """
    push_command = OWN_BRANCH_PUSH_TEMPLATE.format(branch="maestro/evolve/<id>")
    return (
        "Deliver your fix yourself: after committing on this branch (first "
        "merge `origin/main` in if the branch is behind), push it to this PR's "
        f"branch with `{push_command}` (the exact "
        "branch is named under Remediation Guidance below). Do NOT force-push, "
        "do NOT run `gh pr merge`, and never push to `main`/`master` — the "
        "human-in-the-loop approval gate still owns the merge."
    )


# Deterministic, code-owned artifact contract for terminal review-thread
# replies (#2817). ``run()`` delivers each reply to its thread via a trusted
# Python ``reply_to_review_comment`` host call (mirroring the trusted commit
# push) — the single trusted deliverer — so a remediation task RECORDS each
# terminal-marker reply into this manifest under its artifacts directory rather
# than posting it itself (a task-side POST would double-post the thread; the
# ``…/pulls/<n>/comments`` reply endpoint is action-gate-allowed since #2819,
# but the loop stays the sole deliverer). One JSON object per line:
# ``comment_id`` (int) + ``body`` (the terminal-marker reply).
_REPLY_MANIFEST_FILENAME = "thread-replies.jsonl"

# Shared reply-delivery instruction for every inline-review-thread prompt
# section (bug / suggestion / nit / human). Replaces the old "reply on that
# thread" wording — which had the model POST the reply itself — with a
# record-into-manifest contract the loop reads and delivers, so the loop is the
# single trusted deliverer and the model never double-posts. Centralised so the
# wording cannot drift across the sections. The trailing "Do NOT resolve"
# preserves the resolve-vs-reply discipline the completeness gate depends on (a
# resolved thread carries no terminal marker).
_REPLY_DELIVERY_NOTE = (
    "Do NOT resolve any thread, and do NOT post the reply yourself with a "
    "`gh api .../pulls/<n>/comments` request — the loop delivers each recorded "
    "reply for you, so posting it yourself would double-post the thread. "
    "Instead RECORD each terminal-marker reply by appending one line of JSON to "
    f'"{_REPLY_MANIFEST_FILENAME}" in your artifacts directory — each line a '
    'JSON object with a "comment_id" field (the integer comment id shown above '
    'for that thread) and a "body" field (the exact terminal-marker wording '
    "followed by your one-line note). The loop reads that manifest and delivers "
    "each reply to its thread automatically once the task completes — the same "
    "trusted delivery your commit already gets."
)


def read_thread_reply_manifest(artifacts_dir: Path) -> list[tuple[int, str]]:
    """Read recorded terminal review-thread replies from a task's artifacts.

    Returns ``(comment_id, body)`` for each well-formed line of
    ``<artifacts_dir>/thread-replies.jsonl`` (the :data:`_REPLY_MANIFEST_FILENAME`
    manifest the remediation prompt instructs the task to write). The loop
    delivers each via :meth:`CodeHost.reply_to_review_comment` (#2817).

    Tolerant by contract: a missing, empty, or partly-malformed manifest yields
    only the lines that parse — there is no terminal reply to deliver for a line
    the model never wrote correctly, and a single bad line must not drop the
    well-formed ones. Each line must be a JSON object with an integer
    ``comment_id`` and a non-empty string ``body``; anything else is skipped.
    Specific-exception handling only (this module is not on the fail-loud
    allowlist) — a malformed line is logged and skipped, never silently
    swallowed by a broad handler.

    Deduplicated by ``comment_id`` (last write wins): the manifest is
    append-only and accumulates across all of the task's plan steps, so a
    :meth:`Planner.replan` re-run of a recording step can append a second line
    for the same thread. The loop posts every returned entry verbatim, so
    returning both would double-post the thread — the exact outcome the
    record-then-deliver design (loop as sole deliverer) exists to prevent. One
    reply per thread keeps that guarantee even when the model emits more than
    one line for a thread.
    """
    path = artifacts_dir / _REPLY_MANIFEST_FILENAME
    if not path.exists():
        return []
    replies: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "Skipping malformed thread-reply manifest line in %s", path,
            )
            continue
        if not isinstance(record, dict):
            continue
        comment_id = record.get("comment_id")
        body = record.get("body")
        if isinstance(comment_id, int) and isinstance(body, str) and body:
            replies[comment_id] = body  # dedupe by comment_id; last write wins
    return list(replies.items())

# Stable problem-type markers used by ``_preflight_pr_remediation`` to
# decide whether a ``PendingPRRequest`` came from the quality-gates
# routing source (the only category where green ruff/mypy/pytest on the
# PR branch genuinely means the problem self-resolved). Reviewer-feedback
# problems are typically design/correctness issues invisible to the
# gates, and conflict-routed problems can pass branch-local gates while
# ``origin/main`` still refuses to merge — preflight-skipping either
# would silently close requests without acting on them. The markers are
# substrings of the canonical strings produced by
# ``_format_quality_gates_problem`` / ``_format_review_feedback_problem``
# / ``_format_conflict_problem`` / ``_format_bug_comments_problem``; keep
# them in sync with those helpers.
_QUALITY_GATES_PROBLEM_MARKER = ", quality gates failed:"
_REVIEW_FEEDBACK_PROBLEM_MARKER = "A reviewer requested changes on PR #"
_CONFLICT_PROBLEM_MARKER = " conflicts with origin/main."
_BUG_COMMENTS_PROBLEM_MARKER = "unresolved AI bug review "
# Markers for non-bug AI findings (suggestions/nits) and human threads — used
# alongside the bug/review-feedback markers to block preflight-skipping (and the
# merge-block skip) when these comment types are present in a directive.
_NON_BUG_COMMENTS_PROBLEM_MARKER = "unresolved AI review "
_HUMAN_COMMENTS_PROBLEM_MARKER = "unresolved human reviewer "
# Marker for the reply-completion directive (#2893) — posting a terminal marker
# to an outdated, un-replied thread. Like the other comment-review work it is
# LLM-only (Tend PR's mechanical forward-merge never posts a thread reply), so
# it too must block preflight-skipping, the merge-block skip, and the stand-down
# skip; a standdown PR is frequently BEHIND_MAIN, and — since the serviced stamp
# is deferred to reply-delivery (proposal 091), not stamped on admission — a
# merge-block- or stood-down-skipped reply-completion lap would leave the thread
# un-serviced and re-admitted every iteration, an unbounded treadmill (#2968).
# Substring of ``_format_reply_completion_problem``; keep them in sync.
_REPLY_COMPLETION_PROBLEM_MARKER = (
    "outdated review thread(s) needing a terminal reply marker"
)

# Reviewer/comment-driven problem markers. Their work — applying or declining an
# AI bug/suggestion/nit finding, answering a human thread, posting the overflow
# acknowledgement — is LLM-only and invisible to ruff/mypy/pytest *and* to Tend
# PR's mechanical forward-merge. Four consumers consult this single set, through
# ``carries_comment_review_marker``, so a directive carrying any of these is never
# skipped or falsely stood down: ``_is_pure_quality_gates_directive`` (the
# preflight gate), ``gate_merge_block`` (the merge-block dequeue gate), ``gate_stood_down`` (the stand-down dequeue
# gate, ordered ahead of ``gate_merge_block`` — #2968), and
# ``MaestroAgent._mechanical_standdown_verdict`` (the zero-SDK mechanical
# stand-down actuator, which reads the set to force ``has_unserviced_threads``
# True so a marker-present lineage is never mechanically stood down). Keeping one
# definition means the four cannot drift as new markers are added.
_COMMENT_REVIEW_PROBLEM_MARKERS = (
    _REVIEW_FEEDBACK_PROBLEM_MARKER,
    _BUG_COMMENTS_PROBLEM_MARKER,
    _NON_BUG_COMMENTS_PROBLEM_MARKER,
    _HUMAN_COMMENTS_PROBLEM_MARKER,
    _REPLY_COMPLETION_PROBLEM_MARKER,
)


def carries_comment_review_marker(directive: str) -> bool:
    """Return True when ``directive`` carries any comment/review problem marker."""
    return any(marker in directive for marker in _COMMENT_REVIEW_PROBLEM_MARKERS)


def _is_pure_quality_gates_directive(directive: str) -> bool:
    """Return True when ``directive`` is exclusively a quality-gates
    remediation request.

    Multiple problem types can be aggregated into a single
    ``PendingPRRequest.directive`` when several routing sources fire for
    the same PR. The preflight gate is safe only when every problem in
    the directive is a quality-gates failure — otherwise green branch
    gates do not imply the request is resolved. Review-comment problems
    (bug, suggestion, nit, and human threads), in particular, are
    reviewer-found issues invisible to ruff/mypy/pytest, so a directive
    carrying one must never be preflight-skipped.
    """
    if _QUALITY_GATES_PROBLEM_MARKER not in directive:
        return False
    if carries_comment_review_marker(directive):
        return False
    return _CONFLICT_PROBLEM_MARKER not in directive


def _format_review_feedback_problem(pr_number: int) -> str:
    return (
        f"A reviewer requested changes on PR #{pr_number}. Fetch the "
        f"latest review comments via `gh pr view {pr_number} --comments` "
        f"and `gh pr view {pr_number} --json reviews`, then address each "
        "blocking thread. Keep the existing commits; add fix commits on "
        "top of the current branch head."
    )


def _format_conflict_problem(
    pr_number: int, conflicted_files: list[str],
) -> str:
    files_clause = (
        ", ".join(conflicted_files)
        if conflicted_files
        else (
            "(probe could not enumerate; run `git merge --no-commit "
            "origin/main` to discover)"
        )
    )
    return (
        f"PR #{pr_number} conflicts with origin/main. The tend phase ran "
        "a probe-merge and reset the branch — the working tree is clean, "
        f"the conflict is latent. Conflicted files: {files_clause}. "
        "Resolve by merging origin/main into this branch and fixing the "
        "conflicts in these files only (do not rewrite history). Commit the "
        f"resolution on this branch. {_delivery_note()}"
    )


def _format_quality_gates_problem(
    pr_number: int, gate_summary: str,
) -> str:
    if is_ci_failure_on_head_summary(gate_summary):
        # Pre-merge route: the existing PR head already has a failing CI check
        # before any main-merge, so no reset/re-merge happened. Name the real
        # failing check(s) from the ``"<sentinel>: <names>"`` suffix instead of
        # claiming ruff/mypy/tests failed (#2440).
        _sentinel, _sep, names = gate_summary.partition(":")
        failing = names.strip()
        detail = (
            f"the failing CI check(s): {failing}"
            if failing
            else "the failing CI check(s) on the PR head"
        )
        return (
            f"PR #{pr_number}'s existing head has a failing CI check before "
            f"any main-merge. Fix {detail}; do not skip or weaken any gate. "
            f"Commit the fix on this branch. "
            f"{_delivery_note()}"
        )
    return (
        f"After merging origin/main into PR #{pr_number}, quality gates "
        f"failed: {gate_summary}. The tend phase has already reset the "
        "branch to origin/<branch> — the failure is reproducible by "
        "re-merging main. Fix the underlying breakage (the gate summary "
        "names which of ruff / mypy / tests failed); do not skip or "
        "weaken any gate."
    )


# Reviewer-emitted icons per comment type / overflow severity.
_COMMENT_TYPE_ICONS = {
    "bug": "🐛",
    "suggestion": "💡",
    "nit": "✏️",
}


def _format_bug_comments_problem(
    pr_number: int,
    comments: list[tuple[int, str, str, str]],
    overflow_findings: Sequence[OverflowBugFinding] = (),
) -> str:
    """Render the problem block for unresolved AI and human review comments.

    Mirrors the human-paced comment-remediation workflow inside the loop across every
    comment type the reviewer and humans leave: 🐛 bugs, 💡 suggestions,
    ✏️ nits (proposals 063/064), and human review threads.

    *comments* are ``(comment_id, path, title, comment_type)`` tuples from
    :func:`maestro.evolution_tend._eligible_bug_comments`; ``comment_type`` is one
    of ``"bug"``, ``"suggestion"``, ``"nit"``, or ``"human"``.  Bugs carry the
    retired comment-processor's validate-with-a-failing-test/fix/repair-broken-
    tests discipline plus the disagreement ("Could not prove this defect") and
    tracked-issue ("Tracked in #N") terminal paths.  Suggestions and nits are
    validated as in-scope improvements, applied or declined ("Declined with
    implementation-plan context.").  Human threads are applied, declined, or
    answered ("Answered with implementation-plan context.").  Every verdict is
    a terminal-marker reply, never a thread resolution; the task RECORDS each
    reply into the :data:`_REPLY_MANIFEST_FILENAME` manifest rather than posting
    it itself (#2817) and ``run()`` delivers them via
    :meth:`CodeHost.reply_to_review_comment`, the same trusted loop-side delivery
    as the commit push — the single deliverer, so the model never double-posts.
    The markers keep a thread from being re-flagged on the next tend cycle.

    *overflow_findings* are body-overflow findings (any severity) with no thread
    to reply to or resolve; their sections carry the same per-severity
    discipline but record each verdict on the roll-up acknowledgement comment
    instead.  Each section's leading phrase contains the substring the matching
    ``*_PROBLEM_MARKER`` keys on — keep them in sync.
    """
    bug_comments = [
        (cid, path, title)
        for cid, path, title, ctype in comments
        if ctype == "bug"
    ]
    suggestion_nit_comments = [
        (cid, path, title, ctype)
        for cid, path, title, ctype in comments
        if ctype in ("suggestion", "nit")
    ]
    human_comments = [
        (cid, path, title)
        for cid, path, title, ctype in comments
        if ctype == "human"
    ]
    bug_overflow = [f for f in overflow_findings if f.severity == "bug"]
    nonbug_overflow = [
        f for f in overflow_findings if f.severity in ("suggestion", "nit")
    ]

    sections: list[str] = []
    if bug_comments:
        listing = "; ".join(
            f"🐛 {title} ({path}, comment {cid})"
            for cid, path, title in bug_comments
        )
        sections.append(
            f"PR #{pr_number} has {len(bug_comments)} unresolved AI bug review "
            f"comment(s) flagged by the code reviewer: {listing}. Process them "
            "like the comment-processor: for each thread, first validate it is a "
            "genuine defect by writing a failing test that reproduces it (fall "
            "back to static analysis only when a test is impractical — e.g. a "
            "race condition, or a type-level or security concern). If the defect "
            "is confirmed, apply a minimal, targeted fix plus the test; if "
            "applying the fix breaks pre-existing tests, repair those tests to "
            "match the corrected behaviour rather than weakening the fix. Then "
            'record the terminal reply "Fixed locally with implementation-plan '
            'context." for that thread, followed by a one-line description of the '
            "fix. If you cannot prove a defect, leave the code unchanged and "
            'record the disagreement reply "Could not prove this defect" for that '
            "thread, followed by your reasoning, so the next reviewer run "
            "escalates it to a tracked issue. If a defect is confirmed but you "
            "cannot land a fix (too large, or otherwise blocked), do not weaken "
            "or fake it: open a deduplicated GitHub issue for it (search the open "
            "issues first and reuse a matching one rather than filing a "
            'duplicate) and record the reply "Tracked in #N" with the issue '
            "number for that thread. "
            f"Fetch the threads with `gh api graphql` (the PR #{pr_number} "
            f"``reviewThreads``). Commit fixes on this branch. {_delivery_note()} "
            f"{_REPLY_DELIVERY_NOTE} Each terminal reply wording (\"Fixed locally "
            'with implementation-plan context.", "Tracked in #N", "Could not '
            'prove this defect") keeps the thread from being re-flagged on the '
            "next tend cycle.",
        )
    if suggestion_nit_comments:
        listing = "; ".join(
            f"{_COMMENT_TYPE_ICONS[ctype]} {title} ({path}, comment {cid})"
            for cid, path, title, ctype in suggestion_nit_comments
        )
        sections.append(
            f"PR #{pr_number} has {len(suggestion_nit_comments)} unresolved AI "
            "review suggestion/nit comment(s) flagged by the code reviewer: "
            f"{listing}. For each thread, validate that the change is a genuine, "
            "in-scope improvement — correct, a real improvement to clarity, "
            "correctness, efficiency, or test coverage, and consistent with the "
            "implementation plan's intent and the architecture invariants in "
            "CLAUDE.md; prefer a concrete check (a new or adapted test) "
            "demonstrating the improved behaviour. If it is worth applying, make "
            "a minimal, targeted change plus a test where the behaviour is "
            "testable (a purely stylistic nit with no behavioural change needs "
            "no test), then record the terminal reply \"Fixed locally with "
            'implementation-plan context." for that thread, followed by a '
            "one-line description. If the suggestion is wrong, redundant, or "
            "conflicts with a deliberate design choice the plan documents, leave "
            'the code unchanged and record the reply "Declined with '
            'implementation-plan context." for that thread, followed by your '
            "reasoning. "
            f"Fetch the threads with `gh api graphql` (the PR #{pr_number} "
            f"``reviewThreads``). Commit any changes on this branch. "
            f"{_delivery_note()} {_REPLY_DELIVERY_NOTE} Each terminal reply wording "
            "(\"Fixed locally with implementation-plan context.\", \"Declined "
            'with implementation-plan context.") keeps the thread from being '
            "re-flagged on the next tend cycle.",
        )
    if human_comments:
        listing = "; ".join(
            f"{title} ({path}, comment {cid})"
            for cid, path, title in human_comments
        )
        sections.append(
            f"PR #{pr_number} has {len(human_comments)} unresolved human "
            f"reviewer thread(s) on the PR: {listing}. Each is a human change "
            "request or question — address it on its own merits with the "
            "implementation plan in view. If it requests an actionable change "
            "that is correct and in scope, make a minimal, targeted change plus "
            "a test where the behaviour is testable, then record the terminal "
            'reply "Fixed locally with implementation-plan context." for that '
            "thread, followed by a one-line description. If the request is not "
            "worth applying (wrong, out of scope, or conflicts with a deliberate "
            "design choice the plan documents), change nothing and record the "
            'reply "Declined with implementation-plan context." for that thread '
            "with your reasoning. If it is a question rather than a change "
            "request, answer it from the code and the plan and record the reply "
            '"Answered with implementation-plan context." for that thread with '
            "the answer. "
            f"Fetch the threads with `gh api graphql` (the PR #{pr_number} "
            f"``reviewThreads``). Commit any changes on this branch. "
            f"{_delivery_note()} {_REPLY_DELIVERY_NOTE} Each terminal reply wording "
            "(\"Fixed locally with implementation-plan context.\", \"Declined "
            'with implementation-plan context.", "Answered with implementation-plan '
            'context.") keeps the thread from being re-flagged on the next tend '
            "cycle.",
        )
    if bug_overflow:
        overflow_listing = "; ".join(
            f"{f.fingerprint} 🐛 {f.title} ({f.file}:{f.line})"
            for f in bug_overflow
        )
        sections.append(
            f"PR #{pr_number} has {len(bug_overflow)} unresolved AI bug "
            f"review overflow finding(s) — defects the reviewer could not "
            "place as inline comments, listed in the latest AI review body "
            "between `<!-- maestro:overflow -->` and "
            f"`<!-- /maestro:overflow -->`: {overflow_listing}. Apply the "
            "same discipline as for a bug thread: validate each finding by "
            "writing a failing test that reproduces it (fall back to static "
            "analysis only when a test is impractical), apply a minimal, "
            "targeted fix plus the test, and repair pre-existing tests the "
            "fix breaks rather than weakening the fix. An overflow finding "
            "has no review thread to reply to or resolve. Record each "
            "verdict instead by upserting the single roll-up acknowledgement "
            "comment on the PR: fetch the PR's issue comments and find the "
            "one containing `<!-- maestro:overflow-ack -->`, append one line "
            "per finding in the form `of-<hash> — <verdict>`, and update it "
            "via `gh api --method PATCH "
            "repos/<owner>/<repo>/issues/comments/<comment-id>`; when no "
            "such comment exists yet, create it with `gh pr comment "
            f"{pr_number}`, starting the body with that sentinel line. The "
            "verdict per fingerprint is "
            '"Fixed locally with implementation-plan context." for a fixed '
            'finding, "Could not prove this defect" followed by your '
            'reasoning for an unproven one, or "Tracked in #N" after opening '
            "a deduplicated GitHub issue for a confirmed defect you cannot "
            "fix here. Never rewrite or remove existing acknowledgement "
            f"lines — only append. Commit fixes on this branch. {_delivery_note()}",
        )
    if nonbug_overflow:
        overflow_listing = "; ".join(
            f"{f.fingerprint} {_COMMENT_TYPE_ICONS[f.severity]} {f.title} "
            f"({f.file}:{f.line})"
            for f in nonbug_overflow
        )
        sections.append(
            f"PR #{pr_number} has {len(nonbug_overflow)} unresolved AI review "
            "suggestion/nit overflow finding(s) — improvements the reviewer "
            "could not place as inline comments, listed in the latest AI "
            "review body between `<!-- maestro:overflow -->` and "
            f"`<!-- /maestro:overflow -->`: {overflow_listing}. Validate each "
            "as a genuine, in-scope improvement (prefer a concrete check), "
            "apply a minimal, targeted change plus a test where the behaviour "
            "is testable, or decline it if it is wrong, redundant, or "
            "conflicts with a documented design choice. An overflow finding "
            "has no review thread to reply to or resolve. Record each verdict "
            "instead by upserting the single roll-up acknowledgement comment "
            "on the PR: fetch the PR's issue comments and find the one "
            "containing `<!-- maestro:overflow-ack -->`, append one line per "
            "finding in the form `of-<hash> — <verdict>`, and update it via "
            "`gh api --method PATCH "
            "repos/<owner>/<repo>/issues/comments/<comment-id>`; when no such "
            "comment exists yet, create it with `gh pr comment "
            f"{pr_number}`, starting the body with that sentinel line. The "
            "verdict per fingerprint is "
            '"Fixed locally with implementation-plan context." for an applied '
            'finding or "Declined with implementation-plan context." followed '
            "by your reasoning for a declined one. Never rewrite or remove "
            "existing acknowledgement lines — only append. Commit any changes "
            f"on this branch. {_delivery_note()}",
        )
    # Single-pass mandate (the loop's counterpart to the human-paced comment-remediation
    # "all eligible comments are addressed in one invocation" contract): when
    # more than one item is routed, lead with an explicit completeness
    # requirement so the task addresses the whole list in this run instead of
    # the first few — leftovers stay eligible and tend re-routes them lap after
    # lap. ``run()``'s completeness gate (agent step 4d) downgrades a pass that
    # leaves any eligible item un-replied to ``partial``.
    total_items = (
        len(bug_comments)
        + len(suggestion_nit_comments)
        + len(human_comments)
        + len(bug_overflow)
        + len(nonbug_overflow)
    )
    if total_items >= 2:
        sections.insert(
            0,
            f"Address EVERY one of the {total_items} review item(s) listed "
            f"below for PR #{pr_number} in THIS single task run — do not stop "
            f"after the first few. The task is not complete until all "
            f"{total_items} item(s) carry the terminal marker described "
            "for their type below.",
        )
    return "\n\n".join(sections)


def _format_reply_completion_problem(
    pr_number: int,
    comments: list[tuple[int, str, str, str]],
) -> str:
    """Render the problem block for outdated, un-replied review threads (#2893).

    Sibling of :func:`_format_bug_comments_problem` for the reply-completion
    path. The listed threads are **outdated** — GitHub flagged them because the
    code they anchored to has already changed on the branch (typically the very
    fix that addressed them, or a twin thread on the same defect that already
    got the reply). :func:`maestro.evolution_tend._eligible_bug_comments` drops
    outdated threads from *fix* routing so stale findings never re-open a
    remediation lap (the treadmill invariant), but a thread that never received
    a terminal-marker reply keeps counting as outstanding and — under a
    merge-or-close standdown — freezes the PR forever. This block routes exactly
    one bounded lap to give each such thread its terminal marker; it must NOT
    re-do the fix work.

    *comments* are ``(comment_id, path, title, comment_type)`` tuples from the
    :func:`maestro.evolution_tend._reply_needed_threads`-minus-``_eligible_bug_comments``
    delta. For each thread the task reads the current code, decides which
    terminal verdict is now true, and RECORDS the matching terminal-marker reply
    (the same vocabulary as :func:`_format_bug_comments_problem`) into the
    manifest via :data:`_REPLY_DELIVERY_NOTE`; the loop delivers it. The leading
    phrase carries the :data:`_REPLY_COMPLETION_PROBLEM_MARKER` substring so the
    preflight and merge-block gates treat it as comment-review work, never as a
    Tend-clearable merge blocker.
    """
    parts: list[str] = []
    for cid, path, title, ctype in comments:
        icon = _COMMENT_TYPE_ICONS.get(ctype, "")
        prefix = f"{icon} " if icon else ""
        parts.append(f"{prefix}{title} ({path}, comment {cid})")
    listing = "; ".join(parts)
    return (
        f"PR #{pr_number} has {len(comments)} outdated review thread(s) "
        f"needing a terminal reply marker: {listing}. Each thread is OUTDATED "
        "— the code it anchored to already changed on this branch (usually the "
        "fix that addressed it, or a twin thread on the same defect that "
        "already got the reply), so GitHub froze it and it can no longer be "
        "re-routed for fix work. Do NOT re-do any fix and do NOT change code "
        "for these threads. For each one, read the CURRENT code on this branch "
        "and decide which terminal verdict is now true, then record the "
        'matching terminal-marker reply: "Fixed locally with implementation-plan '
        'context." if the change it asked for is already present, "Could not '
        'prove this defect" if it never was a real defect, "Declined with '
        'implementation-plan context." if it was a suggestion or nit you are '
        'declining, "Answered with implementation-plan context." for a human '
        'question, or "Tracked in #N" if a genuine defect remains and you open '
        "(or reuse) a deduplicated GitHub issue for it. "
        f"Fetch the threads with `gh api graphql` (the PR #{pr_number} "
        f"``reviewThreads``). {_REPLY_DELIVERY_NOTE} Each terminal reply wording "
        "keeps the thread from being re-flagged on the next tend cycle."
    )


def _format_user_demand_problem(
    pr_number: int, message: str,
) -> str:
    """Render the problem block for a human message naming an open own PR (091).

    Thomas has directly named this PR as unsettled — the highest-authority
    "not settled" signal in the system. His verbatim words
    are quoted so the task addresses exactly what he is raising, followed by the
    shared delivery note (:func:`_delivery_note`) and the reply-manifest
    thread-routing contract (:data:`_REPLY_DELIVERY_NOTE`): fetch the open
    review threads, commit fixes on this branch, and RECORD each terminal reply
    into the manifest — the loop delivers.
    """
    return (
        f"Thomas sent a direct instruction naming PR #{pr_number}:\n\n"
        f"> {message}\n\n"
        "His message is the authoritative signal that this PR is NOT settled — "
        "answer it. Fetch the open review threads on the PR "
        f"(`gh api graphql` — the PR #{pr_number} ``reviewThreads``), address "
        "whatever he is raising, and commit any fixes on this branch. "
        f"{_delivery_note()} {_REPLY_DELIVERY_NOTE}"
    )


def _render_head_ci_failure_section(
    agent: MaestroAgent, pr_num: int, ci_failure_summary: str,
) -> str:
    """Render the ``## CI Failure`` excerpt for a pre-merge head-CI failure.

    Returns ``""`` unless *ci_failure_summary* is a populated
    ``ci_failure_on_head`` summary — so every non-CI route (whose
    ``ci_failure_summary`` is empty) fires no log read and renders nothing.
    Otherwise it parses the failing
    check names from the summary's ``": <names>"`` suffix and pairs each with
    the failing run's first concrete error lines, reading the log exactly once
    through the redacted, char-capped
    :meth:`~maestro.vcs.CodeHost.read_run_log_failed` seam (the sole side
    effect, deferred to here so it fires only for a CI-failing PR that survived
    the ledger/repetition/preflight skip gates). Mirrors the red-root call
    shape (:meth:`MaestroAgent._diagnose_red_root_on_ci_red`): an empty name
    list falls back to ``("",)`` so a bare sentinel still renders one (unnamed)
    check.
    """
    if not ci_failure_summary:
        return ""
    names = [
        name.strip()
        for name in ci_failure_summary.partition(":")[2].split(",")
        if name.strip()
    ]
    failures = diagnose_ci_failure(
        tuple(names) or ("",),
        lambda: agent.evolution.code_host.read_run_log_failed(pr_num),
    )
    return render_ci_failure_section(failures)


def _compose_pr_remediation_prompt(
    *,
    pr_number: int,
    pr_title: str,
    plan: EvolutionPlan | None,
    plan_id: str,
    pr_body: str,
    pr_url: str,
    problems: list[str],
    ci_failure_section: str = "",
) -> str:
    """Render the multi-section remediation prompt for one PR.

    Immediately after the parseable ``Remediate PR #<n>:`` header the
    rendered prompt carries the merge-truth-first preamble rendered by
    :func:`~maestro.prompts.remediation.merge_truth_first_preamble` so the
    first two physical actions of the turn are ``git fetch`` then
    ``git merge-base --is-ancestor`` — the verdict is composed from the
    freshly-fetched tree, not from memory. The header stays the literal
    first line because the dedup prefix and dequeue gates anchor on it. The
    preamble names ``AWAITING_PUSH`` as an owned push (proposal 098), derived
    from :func:`~maestro.remediation_tracker.classify_blocker_ownership`.

    Composed once at tend-routing time so the :class:`EvolutionPlan`
    snapshot and PR body survive the FIFO hop even if the plan is
    later archived to cold storage. ``plan`` is ``None`` for
    archived-plan PRs — the rendered prompt notes the absence and
    proceeds with the PR description plus the problem block alone.

    *ci_failure_section* (proposal 102, empty unless the head-CI oracle ran)
    is the pre-rendered ``## CI Failure`` block from
    :func:`~maestro.ci_diagnosis.render_ci_failure_section`. When non-empty it
    is placed immediately after ``## Problem`` — so the concrete failing
    test/lint line sits beside the failing-check *name* the problem block
    already carries — and before ``## Original Evolution Plan``. The default
    ``""`` appends nothing, so every existing caller and every non-CI route
    append no section. When that section names the source-size guard, :func:`~maestro.prompts.remediation.size_pin_cure_hint` appends one
    bounded cure line directly beneath it; every other failing gate's excerpt
    leaves the composed prompt byte-identical.
    """
    lines: list[str] = []
    # The ``Remediate PR #<n>: <title>`` header MUST remain the literal first
    # line — the dedup prefix and every dequeue-gate ``re.match`` anchor on it.
    # The merge-truth-first preamble follows immediately so it is still the
    # first *instruction* the agent acts on, upstream of any status word.
    lines.append(f"Remediate PR #{pr_number}: {pr_title}")
    lines.append("")
    lines.append(merge_truth_first_preamble())
    lines.append("## Problem")
    lines.append("\n\n".join(problems))
    lines.append("")
    # Proposal 102: the pre-rendered head-CI-failure excerpt sits directly
    # under ``## Problem`` so the concrete failing line is next to the failing
    # check name. ``render_ci_failure_section`` already ends with a newline; the
    # empty-string default is skipped entirely, so a non-CI route adds no
    # section.
    if ci_failure_section:
        lines.append(ci_failure_section)
        # The one per-gate cure hint: a size-guard failure is cured by moving
        # the addition into a concern-named sibling, never by trimming to fit
        # or raising SPLIT_BUDGET. The
        # trigger is a read of the already-rendered section, so no extra log
        # fetch; every other failing gate's text yields ``""`` and appends
        # nothing.
        cure_hint = size_pin_cure_hint(ci_failure_section)
        if cure_hint:
            lines.append(cure_hint)
    lines.append("## Original Evolution Plan")
    if plan is None:
        lines.append(
            "> note: original EvolutionPlan archived; working from PR "
            "description and problem-class alone.",
        )
    else:
        lines.append(f"- Plan ID: {plan.plan_id}")
        lines.append(f"- Category: {plan.category} / Priority: {plan.priority}")
        lines.append(f"- Title: {plan.title}")
        lines.append(f"- Rationale: {plan.rationale}")
        if plan.proposed_changes:
            lines.append("- Proposed changes:")
            for change in plan.proposed_changes:
                lines.append(f"  - {change}")
        if plan.files_affected:
            lines.append(
                f"- Files affected: {', '.join(plan.files_affected)}",
            )
        if len(plan.acceptance_criteria) > ACCEPTANCE_CRITERIA_MAX_ITEMS:
            # A legacy over-cap plan (#3712) whose PR was opened before the
            # count bound landed. The renderer refuses the list — correctly,
            # it is the choke that keeps an unbounded contract out of context
            # — but this prompt is composed for a PR that already exists, so
            # there is no plan to dispose. Name the omission honestly instead
            # of letting the refusal escape as a crash, or silently dropping
            # criteria the writer would then think it had satisfied.
            lines.append(
                f"- Acceptance criteria: omitted — this plan carries "
                f"{len(plan.acceptance_criteria)} criteria, over the "
                f"{ACCEPTANCE_CRITERIA_MAX_ITEMS} cap, so they are not "
                "rendered. Work from the rationale and proposed changes, and "
                "do not claim the criteria are met.",
            )
        elif plan.acceptance_criteria:
            lines.append(
                format_acceptance_criteria_block(
                    plan.acceptance_criteria, style="remediation",
                ),
            )
    lines.append("")
    lines.append("## Original PR Description")
    lines.append(
        pr_body if pr_body else "(none — PR was opened without a body)",
    )
    lines.append("")
    lines.append("## Remediation Guidance")
    lines.append(f"Branch: maestro/evolve/{plan_id}")
    if pr_url:
        lines.append(f"PR URL: {pr_url}")
    lines.append(
        "Stay on this branch. Do not start a fresh task branch. The "
        "acceptance criteria above remain the success contract — your "
        "changes must still satisfy them after remediation.",
    )
    # The commit-delivery clause is unconditional (proposal 093): the agent
    # delivers its own committed fix by pushing it to the PR branch. The
    # reply-manifest / thread-routing wording above and the "you may not open,
    # merge, or ready a PR" clause are unchanged.
    delivery_clause = (
        "and you deliver your committed fix yourself by pushing it to this "
        "PR branch (a plain `git push origin HEAD:maestro/evolve/<id>` to "
        "the branch named above — never a force-push, a `gh pr merge`, or a "
        "push to `main`/`master`)"
    )
    lines.append(
        "Tools available on this branch: record each inline review-thread "
        "terminal reply into the `thread-replies.jsonl` manifest in your "
        "artifacts directory — the loop delivers each to its thread "
        "automatically once the task completes, so do NOT post the reply "
        "yourself with a `gh api .../pulls/<n>/comments` request (posting it "
        "yourself would double-post the thread). A top-level `gh pr comment` "
        "does NOT attach to a thread, so use `gh pr comment` only to record "
        "general status or the "
        "overflow-finding acknowledgement roll-up, and read review/CI state "
        "with `gh pr view`/`gh pr checks`. You may not open, merge, or ready a "
        f"PR — the human-in-the-loop approval gate owns that, {delivery_clause}. "
        "The merge verdict still comes only from the merge-truth check above, "
        "never from a `gh`-reported state.",
    )
    return "\n".join(lines)


@dataclass(frozen=True)
class _DequeueContext:
    """Immutable per-dequeue snapshot threaded through the transition table.

    Bundles the four values a popped ``pending_pr_requests`` entry carries
    (``goal`` is the entry's directive, ``pr_branch`` its head ref, ``source``
    the always-``external`` task source, and ``entry`` the request itself) so
    every gate runner in :data:`_DEQUEUE_GATE_TABLE` shares one signature.
    """

    goal: str
    pr_branch: str | None
    source: TaskSource
    entry: PendingPRRequest


async def _run_merged_gate(
    ctrl: PrRemediationController, ctx: _DequeueContext,
) -> bool:
    """Dequeue-face merged-PR exclusion (git-merge-truth)."""
    return ctrl._agent._gate_pr_remediation_merged(ctx.goal, ctx.pr_branch)


async def _run_landed_gate(
    ctrl: PrRemediationController, ctx: _DequeueContext,
) -> bool:
    """Commit-ancestry eviction — head already an ancestor of origin/main."""
    return ctrl._agent._gate_pr_remediation_landed(ctx.entry, ctx.source)


async def _run_host_closed_gate(
    ctrl: PrRemediationController, ctx: _DequeueContext,
) -> bool:
    """Human-closed terminal — host reports the PR CLOSED-and-unmerged."""
    return await ctrl._agent._gate_pr_remediation_host_closed(
        ctx.entry, ctx.source,
    )


async def _run_ready_gate(
    ctrl: PrRemediationController, ctx: _DequeueContext,
) -> bool:
    """Observed ready-and-waiting stand-down — every blocker clear (089)."""
    return ctrl._agent._gate_pr_remediation_ready(ctx.entry, ctx.source)


async def _run_stood_down_gate(
    ctrl: PrRemediationController, ctx: _DequeueContext,
) -> bool:
    """Honor my own prior keyed stand-down verdict (proposal 080)."""
    return await ctrl._agent._gate_pr_remediation_stood_down(
        ctx.goal, ctx.pr_branch, ctx.source,
    )


async def _run_merge_block_gate(
    ctrl: PrRemediationController, ctx: _DequeueContext,
) -> bool:
    """Skip SDK spend on a Tend-PR-clearable merge blocker."""
    return await ctrl._agent._gate_pr_remediation_merge_block(
        ctx.goal, ctx.pr_branch, ctx.source,
    )


async def _run_branch_resolved_gate(
    ctrl: PrRemediationController, ctx: _DequeueContext,
) -> bool:
    """Stop the wheel on a PR that keeps resolving-on-branch without landing."""
    return await ctrl._agent._gate_pr_remediation_branch_resolved(
        ctx.goal, ctx.pr_branch, ctx.source,
    )


async def _run_preflight_gate(
    ctrl: PrRemediationController, ctx: _DequeueContext,
) -> bool:
    """Close a request whose merged tree already passes all quality gates."""
    return await ctrl._run_preflight_dequeue(ctx)


# The dequeue chain as data: an ordered ``(state, gate_fn)`` tuple the
# ``classify_dequeue`` runner walks, returning the first matching
# ``PrRemediationState`` (or ``REMEDIABLE`` when none fire). The order is
# load-bearing — merged-exclusion MUST precede the merge-block gate (so an
# already-merged PR never stamps a lap), which precedes branch-resolved and
# preflight; the stand-down gate sits between landed and merge-block. The
# commit-ancestry landed gate sits immediately after merged-exclusion (and
# before merge-block/preflight) so a PR whose branch HEAD is already an ancestor
# of ``origin/main`` — content landed while the PR object reads OPEN, the #2662
# phantom — is evicted on git truth before any merge-block/preflight spend. The
# host-closed gate sits immediately after the landed gate (still ahead of the
# ready/stand-down/merge-block arms) so a PR the human already CLOSED-and-unmerged
# — a terminal the mergedAt-keyed merged/landed arms structurally miss — is
# evicted and its plan routed to ``rejected`` before any downstream spend. The
# observed ready-and-waiting gate (proposal 089) sits after it and before
# stand-down so its live, freshly-observed ``[ready]`` fact supersedes the stale
# ``(pr, head_sha)``-keyed stand-down verdict — a blocker that reappeared on the
# same head re-admits the PR the keyed verdict alone would keep evicting. Every
# zero-cost evicting arm spares a reply-completion goal (stand-down and
# merge-block via ``carries_comment_review_marker``; ready via the reply marker
# alone, since readiness already counts every non-outdated thread), so a
# force-admitted reply lap reaches ``run()`` (#2968, #4122). Pinning the order
# here (rather than in the prose comments that used to guard the bare
# ``if gate(...): return`` sequence) lets the order-invariant guard
# test in ``tests/test_loop_pr_remediation.py`` fail loudly on a reorder instead
# of the invariant silently breaking (proposal 070, step 2).
_DEQUEUE_GATE_TABLE: tuple[tuple[PrRemediationState, _DequeueGateFn], ...] = (
    (PrRemediationState.MERGED, _run_merged_gate),
    (PrRemediationState.LANDED, _run_landed_gate),
    (PrRemediationState.HOST_CLOSED_UNMERGED, _run_host_closed_gate),
    (PrRemediationState.READY_AND_WAITING, _run_ready_gate),
    (PrRemediationState.STOOD_DOWN, _run_stood_down_gate),
    (PrRemediationState.MERGE_BLOCKED, _run_merge_block_gate),
    (PrRemediationState.BRANCH_RESOLVED, _run_branch_resolved_gate),
    (PrRemediationState.PREFLIGHT_CLEAN, _run_preflight_gate),
)


def _tend_clearable_blockers(*, tend_catch_up: bool) -> frozenset[str]:
    """Return the merge-block codes Tend PR clears mechanically.

    The single derivation both dequeue gates share — ``gate_stood_down`` (is my
    prior "Tend delivers this" verdict still honest?) and ``gate_merge_block``
    (is an LLM lap redundant with Tend's next pass?) ask the same question, so
    they must not answer it twice (proposal 098).

    :data:`MERGE_BLOCK_BEHIND_MAIN` is Tend's mechanical catch-up merge and the
    sole Tend-clearable blocker. :data:`MERGE_BLOCK_AWAITING_PUSH` is *owned* — an
    unpushed on-branch resolution is the agent's own next act (one push lands it)
    per :func:`~maestro.remediation_tracker.classify_blocker_ownership`, so it is
    never Tend-clearable, and both callers fall through to a real lap (the push)
    rather than deferring to a Tend delivery that is not the honest next step.
    Precondition: ``BEHIND_MAIN`` is Tend-clearable only because the probe has
    already refined a behind head whose CI is red to ``CI_FAILING`` via
    :func:`~maestro.remediation_tracker.refine_merge_block_reason` — Tend
    refuses the catch-up merge on a red head, so it never clears one (#4096).
    The catch-up exists only while ``[loop] tend_main_merge_enabled``
    (*tend_catch_up*) is on; under the merge-queue model Tend never merges
    ``origin/main`` into a PR, so nothing is Tend-clearable and the set is
    empty (#4307). Pure, deterministic, zero SDK cost.
    """
    if not tend_catch_up:
        return frozenset()
    return frozenset({MERGE_BLOCK_BEHIND_MAIN})


class PreflightRestorationError(RuntimeError):
    """The shared worktree could not be proven restored after a preflight probe.

    ``PrRemediationController.preflight`` checks out a PR branch in the
    *shared* workspace and probe-merges ``origin/main`` into it. Its
    ``finally`` block is responsible for undoing both. A failure to undo
    them is neither transient nor recoverable in place: every path that
    runs next (full LLM remediation, the next iteration's task run, a
    push) would mutate a contaminated tree. So the failure is raised
    rather than downgraded into a gate verdict, reaching
    ``crash.py::handle_crash`` through the loop's normal unwind — the
    "Fail loud" invariant applied to the one site that silently breaks
    "Task branch isolation" (#3715).
    """


def _run_git_restore(
    cmd: list[str], *, timeout: float, cwd: str,
) -> subprocess.CompletedProcess[str]:
    """Run a restoration git *cmd*, raising unless it exits ``0``.

    The shared restoration runner for ``preflight``'s ``finally`` block: each
    repair command and each verification read goes through here, so a
    non-zero exit names the failing command and its ``stderr`` in one place
    instead of being swallowed at DEBUG level (#3715). The command is taken
    as the caller's list rather than read back off ``CompletedProcess.args``,
    which is untyped — a ``str`` command read from there would render one
    space-separated character at a time.
    """
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
    )
    if proc.returncode != 0:
        raise PreflightRestorationError(
            f"preflight: {' '.join(cmd)} exited {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:500]}",
        )
    return proc


def _sync_pr_head_to_origin(workspace: str, branch: str, timeout: int) -> bool:
    """Position the checked-out PR head on the tree CI runs on (#4601).

    Shares ``_measure_pr_head``'s contract — a gate run measures only the
    tree CI ran on. Equal heads, or a local head ahead of origin (commits
    Tend will deliver), are admissible as-is; a local head behind origin
    is fast-forwarded. An unresolvable origin, a failed fast-forward, or
    diverged history returns ``False``.
    """
    origin_ref = f"origin/{branch}"

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], capture_output=True, text=True,
            timeout=timeout, cwd=workspace,
        )

    origin_proc = _git("rev-parse", "--verify", origin_ref)
    origin_sha = origin_proc.stdout.strip()
    if origin_proc.returncode != 0 or not origin_sha:
        logger.info("Preflight: %s unresolvable — not skipping", origin_ref)
        return False
    if _git("rev-parse", "HEAD").stdout.strip() == origin_sha:
        return True
    if _git("merge-base", "--is-ancestor", "HEAD", origin_ref).returncode == 0:
        if _git("merge", "--ff-only", origin_ref).returncode != 0:
            logger.info("Preflight: fast-forward to %s failed", origin_ref)
            return False
        return True
    if _git("merge-base", "--is-ancestor", origin_ref, "HEAD").returncode == 0:
        return True
    logger.info(
        "Preflight: %s diverged from %s — not the tree CI runs",
        branch, origin_ref,
    )
    return False


def _porcelain_entries(stdout: str) -> frozenset[str]:
    """Split ``git status --porcelain`` output into a set of entry lines.

    ``preflight``'s restoration postcondition compares the end state against
    the baseline it measured before the probe, so the reading has to be a set
    of entries rather than one blob: only entries the probe *added* are its
    responsibility (#3715).
    """
    return frozenset(line for line in stdout.splitlines() if line.strip())


# Extracts the first PR number a task goal references — ``#1234`` or
# ``PR 1234`` (case-insensitive). Feeds the merge-truth-first stand-down
# preflight (:meth:`MaestroAgent._preflight_merge_truth_standdown`), the
# third merge-truth face that closes the user-request channel the dequeue-face
# guards never covered (#2579).
_PR_REFERENCE_PATTERN = re.compile(r"(?:#|\bPR\s+)(\d+)", re.IGNORECASE)


def _same_base_partial_streak(
    pr_num: int,
    current_head: str | None,
    current_base: str | None,
    recent_tasks: list[TaskRecord],
) -> int:
    """Count the trailing run of same-``(head, base)`` ``partial`` laps for a PR.

    Walks *recent_tasks* most-recent first, restricted to laps for PR *pr_num*,
    and counts the consecutive run whose outcome is ``partial`` **and** whose
    :attr:`~maestro.memory.TaskRecord.pr_head_sha` equals *current_head* **and**
    whose :attr:`~maestro.memory.TaskRecord.base_head_sha` equals *current_base*
    (proposal 099). The first PR-matching lap that is not ``partial``, whose head
    differs, or whose base differs ends the run: a moved head means the prior
    resolution landed, and a moved base means the lap raced a world that has since
    changed — neither is unchanged, externally-blocked work, so the streak breaks
    exactly as it already does on a moved head. Interleaved non-PR laps are
    skipped, not treated as breaks (mirroring
    :func:`_is_stalled_remediation_lineage`).

    Returns 0 for a missing/empty *current_head* or *current_base* — an
    unmeasurable head or base can start no same-base streak, which fails toward
    re-entry. Pure, zero SDK cost: an in-memory scan of records.
    """
    if not current_head or not current_base:
        return 0
    streak = 0
    for task in reversed(recent_tasks):
        if pr_number_from_remediation_goal(task.goal) != pr_num:
            continue
        if (
            task.outcome == "partial"
            and task.pr_head_sha == current_head
            and task.base_head_sha == current_base
        ):
            streak += 1
        else:
            break
    return streak


# A ``Remediate PR #N`` head of ``pending_pr_requests`` that has been
# re-serviced this many consecutive times — all ``partial`` outcomes carrying
# the *same* unchanged origin head SHA — is a stalled lineage: externally
# blocked work the loop keeps re-routing without the PR head ever advancing.
# ``_run_task_phase`` skips such a head for the iteration (leaving the entry
# queued) and falls through to the self-directed queue (``generate_task``), so
# a landable construction task gets the slot instead of another no-op lap. The
# brake auto-releases when the head SHA advances or the PR merges/closes.
STALLED_REMEDIATION_LAP_CAP = 2


# The CI/review-family merge-block codes ``run()`` refines from the flat rollup
# read into the honest ``classify_merge_blocker_label`` verdict before recording
# a not-landed remediation. ``CI_PENDING`` is the costume the split targets;
# ``AWAITING_APPROVAL`` a green PR's review wait (recorded as the grounded
# ``AWAITING_REVIEW``); ``CI_FAILING`` is folded in so a timing skew between the
# probe and the per-check read still resolves to the same three-way label. The
# git-side blockers (CONFLICTING / BEHIND_MAIN / AWAITING_PUSH) name a different
# gate and are left exactly as ``_probe_pr_merge_block`` classified them.
_CI_REVIEW_MERGE_BLOCKERS = frozenset(
    {MERGE_BLOCK_CI_PENDING, MERGE_BLOCK_CI_FAILING, MERGE_BLOCK_AWAITING_APPROVAL},
)


# Extracts the merge-blocker reason code ``run()`` stamps into a remediation
# lap's ``result_summary`` as ``(merge blocker: <CODE>)`` — the most-recent of
# these per lineage is what :meth:`MaestroAgent._mechanical_standdown_verdict`
# classifies as owned-vs-external.
_MERGE_BLOCKER_RE = re.compile(r"merge blocker:\s*([A-Za-z_]+)", re.IGNORECASE)


# Fire-or-close directive written when the chosen-stand-down guard refuses a
# ``[stood-down]`` verdict because git proves the PR branch is ahead of origin —
# a pushable, owned act, never an external quiet. ``{pr}`` / ``{branch}`` name
# the target. Kept under ``Consciousness.MAX_DIRECTIVE_CHARS`` (200): ~130 static
# chars plus a short branch name and PR number stay below the cap (which
# ``save_self_model`` enforces by truncation regardless).
_OWNED_PUSH_FIRE_OR_CLOSE_DIRECTIVE = (
    "Fire: PR #{pr} ({branch}) is ahead of origin — an owned, pushable act, not "
    "an external wait. Push+land it via the PR, or close it (gh pr close #{pr})."
)


@dataclass(frozen=True)
class _OwnedPushResult:
    """Outcome of the mechanical ahead-of-origin push actuator.

    ``pushed`` is True only when ``git push`` accepted the owned branch *and* a
    same-turn ``verify_push_advanced_remote_head`` read confirmed
    ``origin/<branch>`` physically advanced to the pushed SHA — never on the push
    subprocess's raw return alone. ``unconfirmed`` is True when the push
    subprocess exited 0 but that verifier read showed the remote head did not
    advance (a no-op push, or a fetch/read failure): the delivery claim is
    unproven, so the caller records a ``[push-unconfirmed]`` partial rather than
    a false delivery. ``refused`` is True when the owned-branches oracle rejected
    the branch — in that case **no** ``git push`` subprocess ran, so the
    human-in-the-loop / Tend-sole-pusher invariants can never be widened by this
    path. ``error`` carries the git failure message on a genuine push failure
    (fail-loud — the caller keeps the loud OWNED_PUSH_AVAILABLE partial rather
    than a false delivery).
    """

    pushed: bool
    refused: bool = False
    unconfirmed: bool = False
    error: str | None = None


#: Deterministic commit subject the uncommitted-edit arm of the ahead-of-origin
#: actuator stamps onto a gate-clean working-tree edit before pushing. Fixed so
#: the deed carries no narration — the loop's hand, not a composed sentence.
_UNCOMMITTED_LAND_COMMIT_MESSAGE = (
    "maestro: land gate-clean uncommitted remediation edit"
)


def _is_stalled_remediation_lineage(
    goal: str,
    current_head: str | None,
    recent_tasks: list[TaskRecord],
    *,
    cap: int = STALLED_REMEDIATION_LAP_CAP,
    current_base: str | None = None,
) -> bool:
    """Return True when *goal* is a stalled ``Remediate PR #N`` lineage.

    Stalled means: *goal* is a ``Remediate PR #<n>:`` directive whose trailing
    task-history laps (most-recent first, restricted to laps for the same PR)
    are ``>= cap`` consecutive ``partial`` outcomes that all carry the same
    origin head SHA *current_head*. That is a PR the loop has re-serviced *cap*
    times without the head advancing — externally blocked, unchanged work.

    The consecutive run is counted over PR-matching laps only; interleaved
    non-remediation tasks are ignored, not treated as breaks. The first
    PR-matching lap that is not ``partial`` or whose ``pr_head_sha`` differs
    from *current_head* ends the run (the head advanced, so prior laps were
    real deliveries — the brake releases).

    *current_base* re-keys the streak on base advance (proposal 099). When it is
    ``None`` (the flag-off path) the count is the exact legacy head-only scan.
    When it is non-``None`` the count delegates to
    :func:`_same_base_partial_streak`, so a lap whose ``base_head_sha`` differs
    from *current_base* ends the streak too — a moved ``origin/main`` is a new
    race the head has never been re-run against, not unchanged work.

    Zero SDK cost: a pure scan of in-memory records. Returns False (route the
    remediation normally) for a non-positive *cap*, a missing/empty
    *current_head*, or a *goal* that is not a remediation directive.
    """
    if cap <= 0:
        return False
    if not current_head:
        return False
    pr_num = pr_number_from_remediation_goal(goal)
    if pr_num is None:
        return False
    if current_base is None:
        streak = 0
        for task in reversed(recent_tasks):
            if pr_number_from_remediation_goal(task.goal) != pr_num:
                continue
            if task.outcome == "partial" and task.pr_head_sha == current_head:
                streak += 1
            else:
                break
    else:
        streak = _same_base_partial_streak(
            pr_num, current_head, current_base, recent_tasks,
        )
    return streak >= cap


def _pending_request_references_pr(pending: list[str], pr_number: int) -> bool:
    """True when any queued human message names ``pr_number``.

    The absolute-veto predicate behind ``[loop]
    user_request_blocks_standdown_enabled`` (proposal 091): an unconsumed
    ``pending_user_requests`` entry that explicitly names a PR (``#NNNN`` /
    ``PR NNNN``, via the shared :data:`_PR_REFERENCE_PATTERN`) is the strongest
    "this is not settled" signal there is, so a stand-down for that PR must never
    be authored while such a message sits queued. Pure, deterministic, zero-cost:
    every match in every entry is compared, so a single message naming several
    PRs vetoes each of them.
    """
    return any(
        int(match.group(1)) == pr_number
        for message in pending
        for match in _PR_REFERENCE_PATTERN.finditer(message)
    )


class PrRemediationController:
    """PR-remediation collaborator for :class:`~maestro.agent.MaestroAgent`.

    Constructed with the agent and dispatching through its attributes at
    call time: the cluster's intra-flow calls (``is_pr_settled``,
    ``_probe_pr_merge_block``, ``_escalate_stuck_pr``,
    ``_notify_best_effort``, the gate shims themselves) are historically
    monkey-patched on the agent *instance* by the test suite, so binding
    them any earlier than the call would silently detach those patches.
    """

    def __init__(self, agent: MaestroAgent) -> None:
        self._agent = agent
        # (pr_number, workspace HEAD sha) pairs whose green-handoff gate run
        # already read RED. The green path self-extinguishes through the keyed
        # stand-down; the red path leaves no artifact, so without this memo every
        # later pre-pop selection of the same unchanged head would re-pay for a
        # blocking ruff+mypy subprocess. A new commit changes the sha and the
        # reading is taken again.
        self._green_handoff_red_heads: set[tuple[int, str]] = set()
        # ``{pr_number: delivered_at}`` for the stand-downs this controller's
        # green-handoff arm handed up — the ``delivered_at`` read back from the
        # ledger row the arm's own delivery authored. The arm's local pair is
        # ruff + mypy only — no pytest, no coverage, no browser — so "locally
        # green" is genuinely weaker than CI green, while the stand-down it
        # records suppresses Tend re-minting for that PR across every head until
        # it merges or closes. Keeping the arm's own deliveries here lets
        # :meth:`_release_green_handoff_standdown` clear exactly those when CI
        # later concludes RED, so the loop can still mint a lap to fix its own
        # red. The value is the *identity* of the row, not merely the PR: the
        # arm's suppression can be cleared out from under it by a non-owner
        # (a drained user message through :meth:`route_user_pr_message`, the
        # merge/close pruner) and a foreign deliverer (lap budget, rule 22,
        # reality refutation) can then record its own row under the same key —
        # matching the timestamp is what keeps the release from deleting that
        # foreign row. In-memory by design: a restart loses the map and the
        # stand-down then behaves like any other handoff's (merge/close release
        # only), which is the pre-existing, conservative outcome.
        self._green_handoff_delivered: dict[int, str] = {}

    def route_user_pr_message(
        self, message: str,
    ) -> tuple[str, str] | None:
        """Route a human message naming an open own PR to PR remediation (091).

        When *message* names one of Maestro's own open ``maestro/evolve/*`` PRs,
        returns ``(directive, head_ref)``: a full remediation directive (the
        parseable ``Remediate PR #<n>:`` header, merge-truth-first preamble, the
        human-demand problem block carrying Thomas's verbatim words, and
        plan/PR context) plus the PR's branch ref. As a side effect — *before*
        returning, so a follow-up tend lap cannot instantly re-mute the demand —
        it releases both suppression stores for that PR: the head-agnostic
        stand-down-verdict release (:func:`release_stand_downs`, appended only
        when :func:`has_live_stand_down` finds a live verdict — an unguarded
        release row would bank a never-stood-down PR as disposed, #4314) and
        the escalation-ledger standdown (:func:`escalation_bridge.clear_standdown`).

        Returns ``None`` when the message names no PR, or the named PR is not an
        open own ``maestro/evolve/*`` PR (settled/foreign/absent) — the caller
        then falls through to the generic user-request path, where ``run()``'s
        settled-PR preflight still applies and neither store is touched. Reads
        the open-PR set through the code host in one call; the store releases
        are fail-loud under ``file_lock``.
        """
        match = _PR_REFERENCE_PATTERN.search(message)
        if match is None:
            return None
        pr_number = int(match.group(1))
        agent = self._agent
        pr_states = agent.evolution.code_host.get_open_evolution_pr_states(
            "maestro/evolve/",
        )
        state = next(
            (s for s in pr_states.values() if s.number == pr_number),
            None,
        )
        if state is None:
            return None
        head_ref = state.head_ref
        # Release both suppression stores before the human-demanded run so the
        # strongest "not settled" signal is not answered with silence.
        if has_live_stand_down(pr_number, agent.stand_down_verdicts):
            release_stand_downs(pr_number, agent.stand_down_verdicts)
        escalation_bridge.clear_standdown(agent.escalation_standdowns, pr_number)
        plan_id = head_ref.removeprefix("maestro/evolve/")
        plan = agent.memory.get_plan(plan_id)
        directive = _compose_pr_remediation_prompt(
            pr_number=pr_number,
            pr_title=state.title,
            plan=plan,
            plan_id=plan_id,
            pr_body=state.body,
            pr_url=state.url,
            problems=[
                _format_user_demand_problem(pr_number, message),
            ],
        )
        return directive, head_ref

    def route_feedback(self, tend_result: TendPRsResult) -> None:
        """Append tend-phase PR feedback to ``pending_pr_requests``.

        Four categories share this FIFO — non-stale CHANGES_REQUESTED
        PRs, PRs with mechanical conflict detection, PRs whose quality
        gates broke after a clean main-merge, and PRs carrying unresolved
        AI 🐛 bug review findings (inline threads and body-overflow
        findings fold into one problem block). Each remediation
        goal is rendered as a multi-section Markdown prompt
        (originating :class:`EvolutionPlan` snapshot + PR description
        + per-reason problem block + branch hint) so the Planner has
        complete context when ``_run_task_phase`` later pops the
        entry. Multiple categories firing on the same PR within a
        single tend pass collapse into one queue entry with a unified
        ``## Problem`` block.

        Per-PR dedup across tend cycles uses the header marker
        ``Remediate PR #<n>:`` to avoid stacking duplicate goals for
        the same PR.

        Ordering contract (#2531): the operational admission is persisted
        via ``mutate_self_model`` *before* any ``log_pending_pr_provenance``
        write. The provenance log is measurement-only ("gates nothing"),
        so a failed append must under-count the readmission sensor rather
        than unwind the function and silently drop the queued PR.
        """
        agent = self._agent
        memory_dir = agent.config.memory_dir
        lap_budget = agent.config.pr_remediation_lap_budget

        # Lap-budget standdown upkeep (proposal 080 Phase 3): clear any durable
        # standdown whose PR the host API now reports merged/closed, so a
        # genuinely new problem on a reopened PR can earn a fresh knock. Cheap —
        # consults the host oracle only for PRs that currently carry a
        # standdown. Runs ahead of the early return so stale records are cleared
        # every tend pass, not only when feedback is routed.
        if lap_budget > 0:
            def _pr_resolved(pr: int) -> bool:
                # Centralized observation (proposal 087): MergeTruth.settled
                # folds together the merged and closed-unmerged forks — the
                # "resolved, clear the standdown" question, read once through
                # the one provenance-stamped chokepoint (shared per-iteration
                # memo).
                return agent.is_pr_settled(pr).settled

            escalation_bridge.prune_resolved_standdowns(
                agent.escalation_standdowns, _pr_resolved,
            )

            # Refutation-based upkeep (proposal 135): the third release path.
            # The two pruners release on facts *about the record* (the host says
            # merged/closed; a deadline passed); this one re-asks the question a
            # ``handoff-ready`` record answered — "no owned code work remains" —
            # against the owed act the tend walk just measured, and drops every
            # record the live reading refutes. Under the same ``lap_budget > 0``
            # guard as the merge/close pruner: both release *PR handoff* latches,
            # whose only writer (``deliver_handoff``) is itself lap-budget-gated,
            # so a ``lap_budget == 0`` deployment writes none to release.
            if agent.config.refutable_standdown_enabled:
                observed = tend_result.owed_acts_observed

                def _live_owed_act(pr: int) -> OwedAct | None:
                    # ``None`` for a PR the walk did not measure — an unmeasured
                    # PR keeps its latch for this pass rather than being released
                    # on an absent reading. No host call: the whole reading comes
                    # off ``TendPRsResult``.
                    raw = observed.get(pr)
                    return OwedAct(raw) if raw else None

                outcome = escalation_bridge.prune_refuted_standdowns(
                    agent.escalation_standdowns,
                    _live_owed_act,
                    data_dir=agent.config.memory_dir,
                    blockers=tend_result.owned_blockers_observed,
                    release_cap=(
                        agent.config.active_refutation_release_cap
                    ),
                )
                for released in outcome.released:
                    act = observed.get(released, "")
                    logger.info(
                        "Released refuted stand-down for PR #%s — live owed act "
                        "is %s, refuting the handoff claim",
                        released, act,
                    )
                    audit_checks.log_gate_activation(
                        agent.gate_activations,
                        gate="standdown_refuted",
                        action="released",
                        detail=(
                            f"PR #{released}: handoff-ready stand-down released "
                            f"— live owed act {act}"
                        ),
                    )
                for capped in outcome.capped:
                    # The release budget is spent: the latch is now permanent and
                    # one terminal message says so. A PR that exhausted its
                    # budget is a bounded failure the operator should see, so
                    # this logs at WARNING while a plain release logs at INFO.
                    logger.warning(
                        "Refuted stand-down for PR #%s hit its release cap "
                        "(%d) — latch is now permanent; terminal merge-or-close "
                        "message delivered once",
                        capped,
                        agent.config.standdown_refutation_release_cap,
                    )
                    audit_checks.log_gate_activation(
                        agent.gate_activations,
                        gate="standdown_refuted",
                        action="capped",
                        detail=(
                            f"PR #{capped}: release budget of "
                            f"{agent.config.standdown_refutation_release_cap} "
                            f"spent — stand-down is now permanent"
                        ),
                    )

        # Age-based upkeep (proposal 090): issue-keyed claimed-escalation
        # standdowns carry an ``expires_at`` because the merge/close pruner
        # above can never release a plain issue number. Its writer,
        # ``deliver_claimed_escalation``, is gated on
        # ``phantom_escalation_delivery_enabled`` — independent of
        # ``pr_remediation_lap_budget`` — so the expiry prune must run every
        # tend pass, not only when ``lap_budget > 0``. Nested under
        # ``lap_budget > 0`` it would leave a delivery-enabled +
        # ``lap_budget == 0`` deployment writing issue-keyed standdowns that no
        # path ever clears: the ledger would grow unbounded and the plain-issue
        # key would permanently suppress remediation minting. Cheap — a no-op
        # when no record carries ``expires_at``.
        escalation_bridge.prune_expired_standdowns(
            agent.escalation_standdowns, now=datetime.now(UTC).isoformat(),
        )

        aggregated: dict[int, _PRRemediationAccumulator] = {}

        def _ensure(
            pr_num: int,
            pr_title: str,
            plan_id: str,
            pr_body: str,
            pr_url: str,
            head_ref: str,
        ) -> _PRRemediationAccumulator:
            rec = aggregated.get(pr_num)
            if rec is None:
                rec = _PRRemediationAccumulator(
                    pr_number=pr_num,
                    pr_title=pr_title,
                    plan_id=plan_id,
                    pr_body=pr_body,
                    pr_url=pr_url,
                    head_ref=head_ref,
                )
                aggregated[pr_num] = rec
            return rec

        for cr_entry in tend_result.changes_requested_prs:
            pr_num, pr_title, plan_id, pr_body, pr_url, head_ref = cr_entry
            rec = _ensure(
                pr_num, pr_title, plan_id, pr_body, pr_url, head_ref,
            )
            rec.problems.append(_format_review_feedback_problem(pr_num))
        for conflict_entry in tend_result.conflicts_routed:
            (
                pr_num, pr_title, files,
                plan_id, pr_body, pr_url, head_ref,
            ) = conflict_entry
            rec = _ensure(
                pr_num, pr_title, plan_id, pr_body, pr_url, head_ref,
            )
            rec.problems.append(
                _format_conflict_problem(pr_num, files),
            )
        for qg_entry in tend_result.quality_gates_routed:
            (
                pr_num, pr_title, gate_summary,
                plan_id, pr_body, pr_url, head_ref,
            ) = qg_entry
            rec = _ensure(
                pr_num, pr_title, plan_id, pr_body, pr_url, head_ref,
            )
            rec.problems.append(
                _format_quality_gates_problem(pr_num, gate_summary),
            )
            # Proposal 102: remember the pre-merge head-CI-failure summary so the
            # compose site can render the concrete failing-log excerpt behind the
            # diagnosis flag. Plain string assignment — no I/O here, so the log
            # read stays deferred past the ledger/repetition/preflight skip gates.
            if is_ci_failure_on_head_summary(gate_summary):
                rec.ci_failure_summary = gate_summary
        # Overflow findings (proposal 064) fold into the bug-comments problem
        # block: when both channels fire for the same PR within one tend pass,
        # the rendered directive carries the thread section and the overflow
        # section as one problem.
        overflow_by_pr: dict[int, list[OverflowBugFinding]] = {}
        for of_entry in tend_result.bug_overflow_routed:
            overflow_by_pr.setdefault(of_entry[0], []).extend(of_entry[2])
        for bug_entry in tend_result.bug_comments_routed:
            (
                pr_num, pr_title, bug_comments,
                plan_id, pr_body, pr_url, head_ref,
            ) = bug_entry
            rec = _ensure(
                pr_num, pr_title, plan_id, pr_body, pr_url, head_ref,
            )
            rec.problems.append(
                _format_bug_comments_problem(
                    pr_num, bug_comments, overflow_by_pr.pop(pr_num, []),
                ),
            )
        for of_entry in tend_result.bug_overflow_routed:
            (
                pr_num, pr_title, _findings,
                plan_id, pr_body, pr_url, head_ref,
            ) = of_entry
            remaining = overflow_by_pr.pop(pr_num, None)
            if remaining is None:
                # Already folded into this PR's bug-comments problem above.
                continue
            rec = _ensure(
                pr_num, pr_title, plan_id, pr_body, pr_url, head_ref,
            )
            rec.problems.append(
                _format_bug_comments_problem(pr_num, [], remaining),
            )
        # Reply-completion (#2893): outdated, un-replied review threads need a
        # terminal marker but must NOT re-open fix routing (the treadmill
        # invariant fix routing preserves by dropping outdated threads). Surface
        # such a PR into ``aggregated`` ONLY under an active lap-budget
        # standdown, so it rides the #2798 escape valve for exactly one bounded
        # reply lap (the ``has_standdown`` branch below force-admits its
        # un-serviced ids from ``pass_comment_ids``) — never the non-standdown
        # lap-accrual path, which would spin the treadmill and burn SDK cost on
        # a PR already handed to Thomas. A budget <= 0 disables the standdown
        # machinery entirely, so reply-completion never surfaces there either.
        for rc_entry in tend_result.reply_completion_routed:
            (
                pr_num, pr_title, rc_comments,
                plan_id, pr_body, pr_url, head_ref,
            ) = rc_entry
            if lap_budget <= 0 or not escalation_bridge.has_standdown(
                agent.escalation_standdowns, pr_num,
            ):
                continue
            rec = _ensure(
                pr_num, pr_title, plan_id, pr_body, pr_url, head_ref,
            )
            rec.problems.append(
                _format_reply_completion_problem(pr_num, rc_comments),
            )

        if not aggregated:
            return

        # Reset the remediation tracker and grant a fresh PR-ledger budget
        # for PRs whose branch state actually changed.
        # ``conflicts_routed`` and *post-merge* quality-gate failures
        # both follow a main-merge that put new commits on the branch. The
        # pre-merge ``ci_failure_on_head`` route, however, returns *before* any
        # merge — the branch is unchanged — so resetting it would clear the
        # per-PR no-op counter every tend cycle and defeat the repetition gate
        # (#2440); skip those entries.
        # ``bug_comments_routed`` and ``bug_overflow_routed`` are deliberately
        # absent: bug-comment and overflow detection read GitHub state and
        # never touch the branch, so resetting the no-op counter for them
        # would defeat the repetition gate the same way ``ci_failure_on_head``
        # would.
        branch_changed_prs: set[int] = set()
        for conflict_entry in tend_result.conflicts_routed:
            branch_changed_prs.add(conflict_entry[0])
        for qg_entry in tend_result.quality_gates_routed:
            if is_ci_failure_on_head_summary(qg_entry[2]):
                continue
            branch_changed_prs.add(qg_entry[0])
        for pr_num in branch_changed_prs:
            agent.remediation_tracker.reset(pr_num)
            # The PR-ledger gate below runs ahead of the tracker gate and
            # would otherwise override this deliberate fresh-attempt grant:
            # origin head can only advance by pushing the very resolution
            # an exhausted budget blocks, so without the grant a
            # budget-exhausted PR that develops a new conflict (or
            # post-merge gate failure) could never recover autonomously.
            agent.pr_ledger.grant_fresh_attempts(
                pr_num, reason="branch-state change routed by tend",
            )

        model = agent.consciousness.load_self_model()

        # Header-based dedup: the unified ``Remediate PR #<n>:`` marker
        # (with trailing colon) is unambiguous and avoids the
        # digit-prefix false positive (#42 vs #420) the previous
        # substring scheme had to guard against.
        def _pending_holds_pr(
            entries: list[PendingPRRequest], num: int,
        ) -> bool:
            marker = f"{_REMEDIATION_DEDUP_PREFIX}{num}:"
            return any(marker in entry.directive for entry in entries)

        def _pr_already_pending(num: int) -> bool:
            return _pending_holds_pr(model.pending_pr_requests, num)

        # Cumulative per-PR unlanded lap counts across all heads (host-API
        # merge truth), measured once for the lap-budget standdown gate below.
        # Empty when the budget is disabled, so the gate is fully bypassed.
        unlanded_laps: dict[int, int] = {}
        if lap_budget > 0:
            unlanded_laps = measure_unlanded_remediations(
                memory_dir, _UNLANDED_REMEDIATION_CYCLES,
                is_pr_merged=agent._host_or_git_pr_merged,
            )

        # Progress-aware lap counts (proposal 094): when the progress-aware
        # predicate is active (the dedicated flag or the salvage master flag), the
        # Tend-route handoff arms on *consecutive no-progress* laps instead of the
        # gross unlanded count, so a PR advancing its readiness each lap is never
        # handed up while a genuinely stuck one still is. Measured once here to
        # match the earlier-running Tend route with the task-generation gate's
        # progress-aware check — otherwise this route would hand off a converging
        # PR before the task gate could spare it. Empty (⇒ gross-count fallback)
        # while both flags are off or the budget is disabled.
        no_progress_laps: dict[int, int] = {}
        if lap_budget > 0 and agent.config.progress_aware_lap_budget_active:
            no_progress_laps = measure_no_progress_laps(
                memory_dir, _UNLANDED_REMEDIATION_CYCLES,
            )

        # Genuinely-unanswered review-thread comment ids per PR, feeding the
        # lap-budget standdown carve-out below (#2798). Each ``bug_comments_routed``
        # entry is ``(pr_number, pr_title, bug_comments, plan_id, pr_body, pr_url,
        # head_ref)``; ``bug_comments`` (index 2) is the list of
        # ``(comment_id, path, title, comment_type)`` tuples from
        # ``_eligible_bug_comments``, which already excludes resolved, outdated,
        # and terminally-answered threads — so every id here is reviewer work the
        # loop has never answered. Overflow findings (``bug_overflow_routed``) are
        # deliberately out of scope: they key on a string ``fingerprint``, not the
        # int ``databaseId`` the serviced set tracks, so an overflow-only new
        # finding stays under the standdown (the threshold-gated handoff bounds
        # it). Only built when the budget is enabled — the gate is otherwise off.
        pass_comment_ids: dict[int, set[int]] = {}
        if lap_budget > 0:
            for bug_entry in tend_result.bug_comments_routed:
                pass_comment_ids.setdefault(bug_entry[0], set()).update(
                    cid for cid, _path, _title, _ctype in bug_entry[2]
                )
            # Reply-completion ids (#2893) feed the same carve-out: an outdated
            # un-replied thread is genuinely-unanswered reviewer work, so the
            # ``has_standdown`` branch below force-admits exactly one reply lap
            # for it (only PRs the standdown-gated ``_ensure`` above added to
            # ``aggregated`` are iterated, so ids for a standdown-less PR are
            # never consumed). Same ``(comment_id, ...)`` tuple shape.
            for rc_entry in tend_result.reply_completion_routed:
                pass_comment_ids.setdefault(rc_entry[0], set()).update(
                    cid for cid, _path, _title, _ctype in rc_entry[2]
                )

        appended: list[int] = []
        new_entries: list[tuple[int, PendingPRRequest]] = []
        # Sort by PR number so the FIFO ends up oldest-PR-first.
        for pr_num in sorted(aggregated):
            rec = aggregated[pr_num]
            # Standdown carve-out state, reset per PR so it never leaks across
            # iterations (#2798): ``force_admit`` routes one lap for an
            # un-serviced review thread past the GitHub-only-signal gates, and
            # ``unserviced`` gates whether that lap is admitted (empty → skip).
            # The serviced stamp itself moved to reply-delivery (proposal 091);
            # admission no longer pre-declares the routed threads answered.
            force_admit = False
            unserviced: set[int] = set()
            # Admission-face merged-PR guard: refuse to admit an already-landed
            # PR. Routing already-merged work is a phantom lap — the prior
            # dequeue-face eviction only evicted it after admission (already
            # counted by the readmission sensor, already a lap begun). Reading
            # merge-truth here (never task stamps) prevents the *first* lap.
            # Fires before the dedup path so a merged PR that is already absent
            # from the queue stays absent.
            #
            # The read goes through the shared :meth:`MaestroAgent.is_pr_settled`
            # chokepoint (proposal 087): a host-confirmed landing
            # (``branch_landed``) refuses admission immediately. The PR-object
            # observation cannot see a cure that landed via squash, a sibling
            # PR, or a prior Tend merge while the PR object reads OPEN — so when
            # the host does not confirm a landing, fall back to this face's
            # existing ``origin/<branch>`` ancestry probe for exactly that case,
            # mirroring the host-confirmed-then-branch-ancestry pattern
            # :meth:`MaestroAgent._gate_pr_remediation_landed` uses. The
            # ancestry probe runs against the ``origin/<branch>`` remote-tracking
            # ref (the form that resolves in the loop container, mirroring the
            # ``rev_parse`` below); an empty/unreadable branch defaults to
            # ``False`` (admit), so a git failure never hides genuine work.
            already_landed = agent.is_pr_settled(pr_num).branch_landed
            if not already_landed:
                already_landed = agent._branch_merged_into_main(
                    f"origin/{rec.head_ref}",
                )
            if already_landed:
                logger.info(
                    "[admission-merged] PR #%s branch %s already merged into "
                    "origin/main — refusing admission to pending_pr_requests",
                    pr_num, rec.head_ref,
                )
                continue
            # Durable per-PR lap-budget standdown (proposal 080 Phase 3). The
            # remediation wheel turns *here* in tend, not in the generate_task
            # escalation gate (which rarely runs while tend keeps re-minting),
            # so this is where the standdown must bite. Once a PR's cumulative
            # laps that landed nothing (host-API merge truth) reach the budget,
            # hand it up to Thomas exactly once and stop minting laps for it
            # across all heads until the host reports it merged/closed (the
            # prune above clears the standdown on that transition).
            if lap_budget > 0:
                if escalation_bridge.has_standdown(
                    agent.escalation_standdowns, pr_num,
                ):
                    # #2798 carve-out: a stood-down PR still admits exactly ONE
                    # lap for a genuinely-new (un-serviced) review thread so the
                    # loop can answer it autonomously (reply + optional fix,
                    # human-independent). The treadmill the standdown exists for
                    # — conflicts, quality-gate re-fires, already-serviced
                    # threads — stays suppressed. ``unserviced`` is the routed
                    # ids minus those already serviced under this standdown.
                    #
                    # Self-heal first (proposal 091): the currently-eligible
                    # reply-needed ids in ``pass_comment_ids`` each lack a
                    # terminal Maestro reply by construction, so any of them
                    # already in the serviced set was stamped by the pre-091
                    # admission path before its reply ever delivered. Drop those
                    # falsely-serviced ids so they re-enter the ``unserviced``
                    # set recomputed just below, reopening the valve; a
                    # genuinely-answered thread carries a terminal marker, never
                    # appears here, and is left untouched.
                    reopened = escalation_bridge.reconcile_serviced_comment_ids(
                        agent.escalation_standdowns, pr_num,
                        pass_comment_ids.get(pr_num, set()),
                    )
                    if reopened:
                        logger.info(
                            "[lap-budget] PR #%s self-healed %d falsely-serviced "
                            "review thread(s) %s — reopening the carve-out valve",
                            pr_num, len(reopened), sorted(reopened),
                        )
                    unserviced = escalation_bridge.unserviced_comment_ids(
                        agent.escalation_standdowns, pr_num,
                        pass_comment_ids.get(pr_num, set()),
                    )
                    if not unserviced:
                        logger.info(
                            "[lap-budget] PR #%s under active standdown — "
                            "suppressing remediation minting until it "
                            "merges/closes",
                            pr_num,
                        )
                        continue
                    # Fall through past the GitHub-only-signal gates for this
                    # one lap. The routed thread is NOT marked serviced here
                    # (proposal 091): the serviced stamp fires only once its
                    # terminal reply is confirmed delivered (agent.py §4c-ii), so
                    # a thread whose reply never posted stays eligible for a retry
                    # instead of being muted at enqueue.
                    force_admit = True
                    logger.info(
                        "[lap-budget] PR #%s under active standdown but carries "
                        "%d un-serviced review thread(s) — routing one lap to "
                        "answer them",
                        pr_num, len(unserviced),
                    )
                else:
                    # Stamped unlanded laps *plus* repetition-gate suppressions
                    # (#2783). A re-route the repetition gate below skips mints
                    # no task and stamps no outcome, so it never reaches
                    # ``measure_unlanded_remediations`` — yet it is a lap that
                    # landed nothing. Counting those suppressed laps here makes
                    # the standdown reachable for a PR the cheap gates keep
                    # skipping (the #2727 shape: many "consecutive no-ops" skips,
                    # zero success/partial stamps). The two counts are disjoint
                    # events (a skip mints no stamp; a stamp means a task ran),
                    # so summing them never double-counts. The admission-face
                    # guard above already evicted any branch-merged PR, so a PR
                    # reaching here is git-unmerged. ``skip_count`` is reset on a
                    # branch-state change via ``remediation_tracker.reset``, so
                    # delivered progress earns the budget back.
                    #
                    # Under the progress-aware predicate (proposal 094) the base
                    # lap total is the consecutive *no-progress* count in place of
                    # the gross unlanded count, so a PR advancing its readiness
                    # each lap never arms the handoff while a genuinely stuck one
                    # still does. The ``+ skip_count`` term is kept in both modes:
                    # a repetition-gate skip is itself a no-progress lap, and
                    # ``skip_count`` already resets on delivered progress. Empty
                    # ⇒ ``.get(pr_num, 0)`` yields 0, so both fallbacks (flag off,
                    # budget disabled) reduce to the gross-count behavior.
                    base_laps = (
                        no_progress_laps
                        if agent.config.progress_aware_lap_budget_active
                        else unlanded_laps
                    )
                    laps = (
                        base_laps.get(pr_num, 0)
                        + agent.remediation_tracker.skip_count(pr_num)
                    )
                    if laps >= lap_budget:
                        head_sha = (
                            agent._vcs.rev_parse(f"origin/{rec.head_ref}") or ""
                        )
                        handoff = escalation_bridge.UnlandedCureHandoff(
                            pr_number=pr_num, head_sha=head_sha,
                            stamp_count=laps,
                        )
                        if escalation_bridge.deliver_handoff_cfg(
                            agent.config, handoff,
                            backend=agent.escalation_standdowns,
                            plan_lookup=agent._plan_for_pr_number,
                            claim=StanddownClaim.LAP_BUDGET_EXHAUSTED,
                        ):
                            logger.info(
                                "[lap-budget] PR #%s reached %d unlanded lap(s) "
                                "(budget %d) — delivered one merge-or-close "
                                "handoff and recorded a standdown; suppressing "
                                "further minting",
                                pr_num, laps, lap_budget,
                            )
                        continue
            if _pr_already_pending(pr_num):
                continue
            # The PR-ledger, repetition, and preflight-pass SHA gates all
            # suppress the same GitHub-only re-fires the standdown carve-out is
            # breaking through (constraint 2 of #2798): skip all three when
            # ``force_admit`` so a genuinely-new review thread reaches the
            # enqueue instead of dying one gate later. The admission-merged
            # guard and the ``_pr_already_pending`` dedup above stay in force
            # regardless — a merged PR is never admitted and an already-pending
            # entry is never double-enqueued.
            if not force_admit:
                # PR-ledger gate: skip re-routing for PRs that have exhausted
                # their remediation budget at the current origin head. Head
                # movement (Tend pushed something) resets the budget via
                # ``observe_head()`` in ``tend_evolution_prs``, and a genuine
                # branch-state change was granted fresh attempts above — so a
                # stuck PR unblocks only on delivered progress or new
                # on-branch work, never on GitHub-only signals (bug comments,
                # CHANGES_REQUESTED), which re-fire every pass.
                if not agent.pr_ledger.can_attempt(pr_num):
                    logger.info(
                        "PR ledger gate: skipping re-route for PR #%s "
                        "(exhausted budget at current head)",
                        pr_num,
                    )
                    continue
                # Remediation repetition gate: skip re-routing after consecutive
                # no-op remediations (resets above already fired for PRs with
                # branch-state changes, so those will pass through).
                if not agent.remediation_tracker.should_reroute(pr_num):
                    # Count this suppressed lap toward the lap-budget standdown
                    # (#2783): the skip mints no task and stamps no outcome, so
                    # without this the lap-budget gate above (which reads
                    # ``skip_count``) would never see the PR loop. Only when the
                    # budget is enabled — a ``<= 0`` budget keeps the standdown
                    # fully disabled, so a disabled budget records nothing.
                    if lap_budget > 0:
                        agent.remediation_tracker.record_skip(pr_num)
                    logger.info(
                        "Remediation repetition gate: skipping re-route for "
                        "PR #%s (consecutive no-ops)",
                        pr_num,
                    )
                    continue
                # Preflight-pass SHA gate: when this PR's current head SHA
                # already passed the preflight gate, the merged tree is
                # unchanged and re-routing it is a confirmed no-op — skip it
                # (#2348). A head that has advanced since the recorded pass
                # falls through.
                head_sha = agent._vcs.rev_parse(f"origin/{rec.head_ref}") or ""
                if agent.preflight_passes.should_skip(pr_num, head_sha):
                    logger.info(
                        "Preflight-pass SHA gate: skipping re-route for PR #%s "
                        "(head %s already confirmed passing)",
                        pr_num, head_sha[:12],
                    )
                    continue
            plan = agent.memory.get_plan(rec.plan_id)
            if plan is None:
                logger.warning(
                    "tend: EvolutionPlan %s archived; routing PR #%s "
                    "with PR-body-only context",
                    rec.plan_id, pr_num,
                )
            # Proposal 102: render the head-CI-failure excerpt just before the
            # compose call, so the one ``read_run_log_failed`` ``gh`` read fires
            # only for a CI-failing PR that survived every skip gate above. Empty
            # (⇒ no section) unless this PR carries a ``ci_failure_on_head``
            # summary.
            ci_failure_section = _render_head_ci_failure_section(
                agent, pr_num, rec.ci_failure_summary,
            )
            prompt = _compose_pr_remediation_prompt(
                pr_number=rec.pr_number,
                pr_title=rec.pr_title,
                plan=plan,
                plan_id=rec.plan_id,
                pr_body=rec.pr_body,
                pr_url=rec.pr_url,
                problems=rec.problems,
                ci_failure_section=ci_failure_section,
            )
            # ``pr_branch`` threads the PR's head ref forward so
            # ``_run_task_phase`` runs the remediation task on the PR
            # branch — run() pushes it directly, Tend PR is the fallback.
            new_entries.append((pr_num, PendingPRRequest(
                directive=prompt,
                pr_branch=rec.head_ref,
            )))
            appended.append(pr_num)
            # A routed thread is NOT marked serviced here (proposal 091): the
            # serviced stamp now fires only when its terminal reply is confirmed
            # delivered, in the §4c-ii reply-delivery loop. Stamping at admission
            # muted any thread whose reply never posted; deferring it to delivery
            # keeps an undelivered thread eligible for the next tend lap. The
            # ``force_admit`` decision and ``unserviced`` computation above are
            # unchanged — they still gate whether this lap is admitted.
        if not appended:
            return
        # Persist the operational admission FIRST: a provenance/measurement
        # write failure must never unwind before the queue is saved (it is
        # "measurement-only; gates nothing") (#2531).
        # Transactional append (#3716): the load above is a snapshot taken
        # before this pass's ``gh``/git reads, so a blind whole-model save here
        # would revert any field another writer changed in that window. The
        # callback re-reads the live model under the lock and performs only the
        # queue extension.
        def _extend(live: SelfModel) -> None:
            # Re-run the header dedup against the *live* queue, not the stale
            # snapshot ``_pr_already_pending`` closed over: a writer that
            # enqueued the same PR during the ``gh``/git window above would
            # otherwise be duplicated here — the dual of the lost update this
            # transaction fixes.
            for num, entry in new_entries:
                if _pending_holds_pr(live.pending_pr_requests, num):
                    continue
                live.pending_pr_requests.append(entry)

        agent.consciousness.mutate_self_model(_extend)
        logger.info(
            "tend: routed PR feedback into pending_pr_requests (PRs: %s)",
            appended,
        )
        # Instrument the tend-route admission seam so the readmission sensor
        # can measure how often the same PR re-enters the FIFO-exempt queue.
        # Runs after the durable save so a failed append under-counts the
        # measurement rather than dropping the admission. Routes through the
        # agent's ``pr_provenance`` backend (JSONL sidecar or ``state.db`` behind
        # ``[paths] sqlite_state_enabled``), which drops a non-positive PR number.
        for pr_num in appended:
            agent.pr_provenance.record(pr_num, PR_ADMISSION_CHANNEL_TEND)

    async def preflight(self, pr_request: PendingPRRequest) -> bool:
        """Run quality gates on the *merged* PR tree before LLM invocation.

        Returns ``True`` only when the PR branch merges ``origin/main``
        cleanly **and** ruff, mypy, and pytest all pass on the resulting
        merged tree — meaning the problem that triggered the remediation
        request has already self-resolved (e.g. a prior remediation
        commit was pushed by the next tend cycle, or a main-merge
        resolved the conflict). When ``True``, the caller skips the
        full LLM-driven task at zero SDK cost.

        The merge step is load-bearing: the routed directive's problem
        is defined relative to the *merged* tree (e.g.
        ``_format_quality_gates_problem`` describes a post-merge CI
        failure). A branch that is green in isolation but conflicts with
        or fails after ``origin/main`` is **not** resolved, so gating on
        the un-merged branch would falsely skip genuine remediation and
        spin a zero-cost no-op loop (#2402). The probe-merge is reset
        afterwards — preflight never pushes, and the merged tree is a
        throwaway. The one intended local mutation is the origin-head
        sync: a local branch strictly behind ``origin/<branch>`` stays
        fast-forwarded to it.

        The *gate* result degrades safely: the body is wrapped in a
        broad ``except Exception`` so a preflight failure (checkout
        error, subprocess crash, OS fault) never blocks genuine
        remediation — it simply returns ``False`` and lets the normal
        flow proceed.

        Restoring the *shared* worktree is not part of that degradation.
        It is a checked postcondition: once the probe has actually moved
        the worktree, the ``finally`` block proves the probe-merge was
        reset away, that HEAD is back on ``main``, and that
        ``git status --porcelain`` carries no entry the probe added. If
        any of those readings fails, :class:`PreflightRestorationError`
        is raised — superseding the body's verdict by design, so no
        ``True`` pass is recorded and no remediation begins on an
        unproven tree (#3715).

        The postcondition is scoped to what the probe *owns*. Dirt the
        workspace already carried is measured as a baseline before the
        PR-branch checkout and subtracted from the end-state reading, and
        a probe that never got as far as that checkout changed nothing,
        so it asserts nothing. A clean tree is a precondition preflight
        never established; asserting it would crash the loop over
        another phase's leftovers.

        Before any measurement the positioned head is synced to
        ``origin/<branch>`` (:func:`_sync_pr_head_to_origin`, the sibling
        of ``_measure_pr_head``'s guard): a stale local branch is
        fast-forwarded, and an unresolvable or diverged origin returns
        ``False`` so a pass is never recorded for a tree CI never ran on.
        """
        agent = self._agent
        if pr_request.pr_branch is None:
            return False
        workspace = str(agent.config.workspace)
        # HEAD of the PR branch before the probe-merge; non-empty only
        # once a merge commit may have been created, so the ``finally``
        # block knows whether to reset it away.
        pre_merge_head = ""
        # The (pr_number, head_sha) preflight pass to stamp once restoration
        # is proven; ``None`` until every gate passes on the merged tree.
        pending_pass: tuple[int, str] | None = None
        # Entries the shared workspace already carried before the probe. Only
        # entries the probe *adds* on top of these are its restoration debt.
        baseline_dirt: frozenset[str] = frozenset()
        # Flipped once the PR-branch checkout succeeds — the first command
        # that moves the shared worktree. Until then preflight owes no
        # restoration, so the ``finally`` block stands down.
        probe_moved_worktree = False
        # Flipped when the probe unwinds on cancellation, which is the loop
        # tearing down rather than a preflight failure.
        cancelled = False
        try:
            baseline_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True,
                timeout=agent.config.git_timeout, cwd=workspace, check=True,
            )
            baseline_dirt = _porcelain_entries(baseline_proc.stdout)
            # Check out the PR branch so quality gates run against its tree,
            # falling back to a fresh local branch off ``origin/<branch>`` when
            # none exists locally, so the red-root diagnosis below is not lost
            # to a missing local ref (#4100).
            checkout_proc = subprocess.run(
                ["git", "checkout", pr_request.pr_branch],
                capture_output=True, text=True,
                timeout=agent.config.git_timeout, cwd=workspace,
            )
            if checkout_proc.returncode != 0:
                subprocess.run(
                    ["git", "checkout", "-b", pr_request.pr_branch,
                     f"origin/{pr_request.pr_branch}"],
                    capture_output=True, text=True,
                    timeout=agent.config.git_timeout, cwd=workspace, check=True,
                )
            probe_moved_worktree = True
            # Origin-head guard (#4601): measure only the tree CI ran on. A
            # local branch trailing ``origin/<branch>`` is fast-forwarded; an
            # unresolvable or diverged origin falls through to full remediation.
            if not _sync_pr_head_to_origin(
                workspace, pr_request.pr_branch, agent.config.git_timeout,
            ):
                return False
            # Red-root diagnosis is action-zero (#4100) on every admitted
            # CI-red/pending lap; non-pure-QG laps get it in
            # ``_run_preflight_dequeue`` instead (#4216). Run it now, on the
            # PR head this checkout just positioned and before the probe-merge,
            # so it measures the PR's own tree rather than ``main`` and sits
            # inside the ``finally`` that proves restoration. Its failures
            # propagate — wrapped in a dedicated type the handler below
            # re-raises — as they did when it ran ahead of this probe.
            try:
                agent._diagnose_red_root_on_ci_red(pr_request)
            except Exception as exc:
                raise RedRootDiagnosisError(
                    f"red-root diagnosis failed for {pr_request.pr_branch}",
                ) from exc
            # Record the branch HEAD so the probe-merge commit can be
            # reset away afterwards (never pushed).
            head_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True,
                timeout=agent.config.git_timeout, cwd=workspace, check=True,
            )
            pre_merge_head = head_proc.stdout.strip()
            # Probe-merge ``origin/main`` so the gates run against the
            # merged tree, reproducing the post-merge condition the
            # routed directive describes. ``--no-ff`` forces a merge
            # commit so the reset in ``finally`` always restores HEAD.
            merge_proc = subprocess.run(
                ["git", "merge", "--no-ff", "--no-edit", "origin/main"],
                capture_output=True, text=True,
                timeout=agent.config.git_timeout, cwd=workspace,
            )
            if merge_proc.returncode != 0:
                # The branch does not merge cleanly — genuine remediation
                # is required. Abort the half-applied merge so the tree is
                # clean; a *successful* abort restores HEAD, so the reset
                # below is suppressed. A failed abort leaves the merge
                # half-applied, so ``pre_merge_head`` is kept and the
                # ``finally`` reset still fires over it (#3715).
                abort_proc = subprocess.run(
                    ["git", "merge", "--abort"],
                    capture_output=True, text=True,
                    timeout=agent.config.git_timeout, cwd=workspace,
                )
                if abort_proc.returncode == 0:
                    pre_merge_head = ""
                return False
            # The gate source/tests paths come from the WorkTarget owner
            # (proposal 107); both default to ``maestro/`` / ``tests/`` (plus the
            # lint-only ``scripts/`` extra) so the gate commands match CI. ``--cov`` derives from the
            # source atom (``--cov=maestro`` by default).
            wt = agent.config.work_target()
            source, tests = wt.gate_source_path, wt.gate_tests_path
            extra = wt.gate_lint_extra_paths
            # One slot covers the ruff -> mypy -> pytest sequence on the merged
            # tree; the granted width is spliced into the pytest gate. The lease is
            # released when this ``with`` exits — before the ``finally`` git repair
            # resets the probe-merge commit — so no slot is held across the reset.
            # Flag off, ``worker_args`` returns ``()`` and the commands stay
            # byte-identical.
            with gate_capacity.lease_for_gate_run(
                agent.config.gate_lease_dir,
                runs_path=agent.config.gate_runs_path,
                cgroup_reserve=agent.config.gate_lease_cgroup_reserve_enabled,
                admission_cap=agent.config.gate_lease_admission_cap_enabled,
                # Preflight spends the flat ``selfcheck_timeout``, never a
                # lease-granted deadline, so naming its source keeps its rows out
                # of ``derive_deadline``'s input — an ``in_process`` row here
                # between two budgeted suite runs would reset the escalation to
                # the floor and it would never converge.
                source="preflight",
            ) as run:
                for cmd in [
                    ["ruff", "check", source, tests, *extra],
                    ["mypy", source, *extra],
                    ["pytest", tests, "-x", "--tb=short", "-q",
                     f"--cov={source.rstrip('/')}",
                     f"--cov-fail-under={agent.config.cov_fail_under}",
                     *gate_capacity.worker_args(run.lease)],
                ]:
                    proc = subprocess.run(
                        cmd, capture_output=True, text=True,
                        timeout=agent.config.selfcheck_timeout, cwd=workspace,
                    )
                    if proc.returncode != 0:
                        # Stamp the failing outcome before the return exits the
                        # lease context (the seam records on exit).
                        run.outcome = "FAILED"
                        return False
                # Every gate passed on the merged tree.
                run.outcome = "CLEAN"
            # Gates pass on the merged tree at ``pre_merge_head``. Record the
            # confirmed-passing head SHA so the next tend cycle skips
            # re-routing this PR unless its head advances (#2348).
            pr_match = re.match(
                r"Remediate PR #(\d+):", pr_request.directive,
            )
            if pr_match and pre_merge_head:
                # Deferred to the ``finally`` block's tail: a recorded pass
                # makes the next tend cycle skip this PR, so it is only
                # honest once the shared worktree is *proven* restored
                # (#3715). A restoration failure raises before it lands.
                pending_pass = (int(pr_match.group(1)), pre_merge_head)
            return True
        except asyncio.CancelledError:
            # Cancellation is the loop shutting down, not a preflight
            # failure. Flagged so the ``finally`` block stands down instead
            # of replacing the cancellation with a restoration verdict of
            # its own (#3715).
            cancelled = True
            raise
        except RedRootDiagnosisError:
            # A diagnosis crash is loud, never degraded into ``False``.
            raise
        except Exception:
            logger.debug(
                "preflight quality gate check failed — proceeding to "
                "full remediation", exc_info=True,
            )
            return False
        finally:
            # Reset away the probe-merge commit (if one was created) so
            # the PR branch is never left with an un-pushed merge commit,
            # then return to main so the working tree is clean for
            # whatever runs next (the LLM remediation or the next
            # iteration).
            #
            # Restoration is a *checked postcondition*, not best effort: a
            # non-zero exit from either repair command, or an end state that
            # is not a ``main`` carrying no new dirt, raises
            # ``PreflightRestorationError`` (#3715). Raising from ``finally``
            # supersedes the body's verdict by design — that is the point.
            #
            # Two guards keep the assertion scoped to what the probe owns: a
            # probe that never reached the PR-branch checkout moved nothing,
            # and a cancellation must reach the loop intact rather than being
            # replaced by a restoration error raised on the way out.
            if cancelled:
                logger.warning(
                    "preflight: cancelled mid-probe — shared-worktree "
                    "restoration skipped, tree may be left on %s",
                    pr_request.pr_branch,
                )
            elif probe_moved_worktree:
                git_timeout = agent.config.git_timeout
                if pre_merge_head:
                    _run_git_restore(
                        ["git", "reset", "--hard", pre_merge_head],
                        timeout=git_timeout, cwd=workspace,
                    )
                _run_git_restore(
                    ["git", "checkout", "main"],
                    timeout=git_timeout, cwd=workspace,
                )
                # Verify the end state directly rather than trusting the two
                # repair commands' exit codes alone.
                branch_proc = _run_git_restore(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    timeout=git_timeout, cwd=workspace,
                )
                if branch_proc.stdout.strip() != "main":
                    raise PreflightRestorationError(
                        "preflight: worktree left on branch "
                        f"{branch_proc.stdout.strip()!r}, expected 'main'",
                    )
                status_proc = _run_git_restore(
                    ["git", "status", "--porcelain"],
                    timeout=git_timeout, cwd=workspace,
                )
                new_dirt = _porcelain_entries(status_proc.stdout) - baseline_dirt
                if new_dirt:
                    raise PreflightRestorationError(
                        "preflight: worktree left dirty after restoration: "
                        f"{' | '.join(sorted(new_dirt))[:500]}",
                    )
                # Restoration is proven, so the confirmed-passing head SHA can
                # be stamped: the next tend cycle skips re-routing this PR
                # unless its head advances (#2348).
                if pending_pass is not None:
                    agent.preflight_passes.record(*pending_pass)

    def gate_merged(self, goal: str, pr_branch: str | None) -> bool:
        """Exclude an already-merged PR at the ``pending_pr_requests`` dequeue.

        Consults the reconciled merged set first: a PR the standing settlement
        reconciler already upserted into ``settled_verdicts.jsonl`` this cycle
        (``settled_verdicts.merged_settled_prs()``) is evicted at zero
        merge-oracle cost, closing the one-cycle lag in which the per-iteration
        ``is_pr_settled`` memo, warmed stale earlier this iteration, would defer
        the eviction to next cycle. On a miss (or with the reconciler disabled)
        it reads merge-truth through the shared ``MaestroAgent.is_pr_settled``
        chokepoint (proposal 087) at most once for the dequeued PR —
        ``branch_landed`` is the host-API-first, git-subject-fallback merge
        verdict. When the PR has already merged into ``origin/main`` there is
        nothing left to remediate, and the ``pop(0)`` in ``_run_task_phase`` has
        already evicted the entry from the queue — so return ``True`` and let
        the caller return *without* routing a ``Remediate PR #N`` task or
        recording any :class:`TaskRecord` stamp. That starves the
        false-success-recurrence counter at its source: a merged PR never
        becomes a counted remediation lap.

        Ordering is deliberate — this runs *before* the merge-block gate and
        preflight. The merge-block gate keys on ``BEHIND_MAIN`` /
        ``AWAITING_PUSH`` and explicitly passes a merged PR through (its
        ``_probe_pr_merge_block`` reports ``landed_ok`` → the gate returns
        ``False``); preflight closes a *mergeable* branch but still spends a
        routing decision and a ``success`` stamp. This gate keys on *merged*
        and acts before any count, so neither downstream site ever stamps a
        merged PR. Sharing the one chokepoint observation keeps the exclusion
        and the merge-truth sensors agreeing on a single truth source.

        A directive without a parseable ``Remediate PR #<n>:`` header returns
        ``False`` (nothing to key on); zero SDK cost.
        """
        agent = self._agent
        match = re.match(r"Remediate PR #(\d+):", goal)
        if not match:
            return False
        pr_num = int(match.group(1))
        # Reconciled-settled short-circuit: the standing settlement reconciler
        # (:func:`~maestro.audit_checks_settlement.reconcile_settled_verdicts`) upserts every
        # merged PR into the durable ``settled_verdicts.jsonl`` ledger each cycle
        # and threads that same set into the phantom audit rules as their
        # pre-filter. Consult the *merged* subset of that reconciled set FIRST —
        # a PR already recorded merged is evicted now at zero merge-oracle cost,
        # closing the one-cycle lag in which the per-iteration ``is_pr_settled``
        # memo, warmed to a stale pre-merge read earlier this iteration, would
        # defer the eviction to next cycle and re-serve landed work as a
        # remediation task. ``merged_settled_prs`` (not ``effective_settled_prs``)
        # keeps this gate's ``branch_landed`` semantics: a closed-unmerged PR is
        # left for the host-closed gate to route to ``rejected``. Gated on the
        # reconciler master switch — with it off the ledger is unmaintained, so
        # fall through to the live oracle read (byte-identical pre-097 behavior).
        if (
            agent.config.settlement_reconciler_enabled
            and pr_num in agent.settled_verdicts.merged_settled_prs()
        ):
            logger.info(
                "run_task: dequeue-face merged-PR exclusion (reconciled) — "
                "PR #%s in the current-cycle reconciled settled set; evicted "
                "from pending_pr_requests at zero merge-oracle cost (branch %s)",
                pr_num, pr_branch or "<none>",
            )
            return True
        # Centralized observation (proposal 087): read the shared,
        # provenance-stamped MergeTruth through the ``is_pr_settled`` chokepoint
        # and evict only a genuinely landed (MERGED) PR. ``branch_landed`` is
        # True exactly for a MERGED PR; the decision stays this gate's own —
        # only the observation is centralized.
        if not agent.is_pr_settled(pr_num).branch_landed:
            return False
        logger.info(
            "run_task: dequeue-face merged-PR exclusion — PR #%s already "
            "merged into origin/main; evicted from pending_pr_requests with "
            "no routed remediation task or stamp (branch %s)",
            pr_num, pr_branch or "<none>",
        )
        return True

    def next_unheld_pr_index(
        self,
        entries: list[PendingPRRequest],
        start: int,
        recent: list[TaskRecord] | None = None,
    ) -> int | None:
        """Index of the first entry from *start* neither stalled nor CI-held.

        *start* is :meth:`MaestroAgent._first_non_stalled_pr_index`'s pick, so
        its stall probe is not repeated; every later entry is probed for a
        stall against *recent* — the tail that pick already read, else one
        lazy ``get_recent_tasks(30)`` read — before its CI-pending
        hold is evaluated. A held entry stays queued — the hold's own
        activation and green-handoff side effects still fire — and the slot
        moves on instead of idling (#4064). ``None`` means every remaining entry
        is stalled or held, so the caller falls through to the queue head.
        """
        agent = self._agent
        for i in range(start, len(entries)):
            if i > start:
                if recent is None:
                    recent = agent.memory.get_recent_tasks(30)
                if agent._pending_pr_head_stalled(entries[i], recent):
                    continue
            if not agent._gate_pr_remediation_ci_pending_hold(entries[i]):
                return i
        return None

    def gate_ci_pending_hold(self, entry: PendingPRRequest) -> bool:
        """Yield the remediation slot when CI is the sole live blocker.

        The refusal-by-correctness gate for the CI-pending owned-PR monoculture:
        ten consecutive remediation partials on the same PRs are one behavior —
        opening a session on an owned PR whose only blocker is CI *still running*,
        then reconcile-pushing and resetting the very CI clock the loop is waiting
        on (``push-resets-the-ci-clock``). When the sole live blocker is
        CI_PENDING there is nothing to fix: the correct act is to leave the head
        untouched so CI concludes on its own clock.

        Called from :meth:`_run_task_phase` *before* the selected
        ``pending_pr_requests`` entry is popped, so a fired gate leaves the PR
        queued (no eviction) and opens no SDK session, unlike the eviction /
        stand-down gates in the post-pop :data:`_DEQUEUE_GATE_TABLE`. It writes no
        stand-down verdict, escalates nothing, and never closes the PR — the PR
        stays fully tend-eligible and a later pop re-evaluates once CI concludes.

        Reads CI state first and cheaply — the same
        ``get_pr_check_conclusion`` → :func:`ci_state_from_conclusion` read
        ``_tend_single_pr`` uses to defer main-merge on pending CI — and returns
        ``False`` immediately unless CI is still running (``PENDING``). Only for a
        genuinely CI-pending head does it then consult the
        :meth:`observe_pr_readiness` chokepoint (the merge-truth / CI / thread /
        overflow oracles folded into one provenance-stamped
        :class:`~maestro.pr_facts.PRReadiness`, memoized per iteration) to
        confirm **no self-clearable blocker remains**: the PR is open
        (``not settled`` — neither merged nor closed), no merge conflict (``not branch_conflicting``),
        no behind-main catch-up (``not branch_behind``), and no unaddressed
        AI-finding thread / overflow (``unresolved_threads == 0`` and
        ``unaddressed_overflow == 0``). A failing gate reads ``FAILING`` (not
        ``PENDING``) at the cheap first read, so it never reaches the observation.
        This is exactly the observed-ready terminal with CI *pending* instead of
        *green*. Reading CI first keeps the heavier readiness observation (and its
        merge-truth fetch / merge-block probe) off every non-pending selection.

        One-shot green-handoff arm (#3630, ``[loop]
        ci_pending_green_handoff_enabled``, default off). A CI-pending hold is
        *correct* but silent: nothing tells Thomas the PR is otherwise finished, so
        it is re-selected and re-yielded while no owned terminal act is recorded.
        Rule 22's ``green_ready_handoff`` needs every check concluded SUCCESS and
        the escalation bridge needs the lap budget spent, so both fire too late.
        With the flag on, at this already-established "CI-pending is the sole live
        blocker" point the arm runs the same demanding gate pair (ruff → mypy)
        :func:`~maestro.red_root.diagnose_red_root` owns against the head — no
        ``check_runs`` fold, since CI is pending by construction — and on
        ``all_passed`` delivers the merge-or-close handoff through the same
        delivery-truth path rule 22 uses (:func:`~maestro.audit_checks_phantom.confirm_handoff_delivery`
        wrapping :func:`~maestro.escalation_bridge.deliver_handoff`, binding
        'delivered' to a ``pr_number``-keyed ``stand_down_verdicts.jsonl`` row
        written during this fire rather than the actuator's return), plus
        one ``ci_pending_green_handoff`` activation and one
        :attr:`~maestro.deed_ledger.DeedType.HANDOFF_DELIVERED` deed. No push,
        merge, or close capability is added, and the gate's return value is
        unchanged in every branch: the slot is still yielded and the PR still left
        queued.

        The arm's stand-down is released on a RED conclusion, not only on
        merge/close (:meth:`_release_green_handoff_standdown`): the local pair
        is ruff + mypy alone — no pytest, coverage or browser — so local green is
        strictly weaker than CI green, and a CI that later concludes FAILING
        refutes the premise the handoff asserted. Since the arm *yields* rather
        than evicts, the PR is still queued for the next selection, whose cheap
        CI read is where that release fires. The handoff's own message names the
        CI-pending premise and that release
        (:func:`~maestro.escalation_bridge.ci_pending_green_handoff_message`)
        rather than reusing the default lap-exhaustion text.

        The ordering of the arm's conditions is load-bearing, not incidental: the
        :func:`~maestro.escalation_bridge.has_standdown` read is evaluated
        *before* the gate run, mirroring rule 22's own docstring, because it is the
        self-extinguishing gate — a PR already handed up must never pay for a
        ruff/mypy subprocess or checkout on any later selection. The gate runner
        measures the *workspace* tree, so :meth:`_measure_pr_head` then checks
        the PR branch out (restoring the entry branch afterwards), and
        :meth:`_green_handoff_gate_target` asserts the checkout landed on the PR
        branch; a head whose run already read RED is memoized so the red
        path — which leaves no durable artifact to extinguish on — is not re-paid
        for on every later selection of the same head.

        On fire, appends one ``[ci-pending-hold]`` :func:`log_gate_activation`
        record — plus, when the green-handoff arm fires too, a second
        ``ci_pending_green_handoff`` record — and returns ``True`` (the caller
        yields the slot, PR left queued). Returns ``False`` — remediation proceeds
        to the normal dequeue/route path — when the flag is off, when the
        directive has no parseable ``Remediate PR #<n>:`` header or branch, when
        CI has already concluded, or when any self-clearable blocker is present.
        Zero SDK cost; the I/O is the CI conclusion read, the memoized readiness
        observation (CI-pending only), the activation append(s), and — only on the
        green-handoff arm — the branch/head reads, the gate subprocesses, and the
        arm's three delivery artifacts.
        """
        agent = self._agent
        if not agent.config.ci_pending_hold_dequeue_enabled:
            return False
        goal = entry.directive
        match = re.match(r"Remediate PR #(\d+):", goal)
        if not match:
            return False
        pr_branch = entry.pr_branch
        if not pr_branch:
            return False
        pr_num = int(match.group(1))
        # Cheap CI read first: only a CI-*pending* head can be a CI-pending-hold,
        # so a concluded rollup (GREEN / FAILING / NONE) short-circuits before the
        # heavier readiness observation runs — keeping its merge-truth fetch and
        # merge-block probe off every non-pending remediation selection.
        conclusion = agent.evolution.code_host.get_pr_check_conclusion(pr_num)
        if ci_state_from_conclusion(conclusion) != "PENDING":
            # CI concluded. If this arm handed the PR up while it was pending,
            # a RED conclusion refutes the premise it asserted — release the
            # stand-down so remediation can be minted for the PR's own red.
            self._release_green_handoff_standdown(pr_num, conclusion)
            return False
        readiness = agent.observe_pr_readiness(pr_num, pr_branch)
        # CI-pending-hold terminal: open (not ``settled``) PR, CI still running, and every
        # *self-clearable* blocker clear. A behind-main / conflicting branch sets
        # its flag — either makes this False so the entry falls through to the
        # real dequeue/route path (the merge-block gate handles behind/conflict).
        ci_is_sole_blocker = (
            not readiness.settled
            and readiness.ci_state == "PENDING"
            and not readiness.branch_conflicting
            and not readiness.branch_behind
            and readiness.unresolved_threads == 0
            and readiness.unaddressed_overflow == 0
        )
        if not ci_is_sole_blocker:
            return False
        # One-shot green-handoff arm (#3630). The ``has_standdown`` read comes
        # first by design (see the docstring): it self-extinguishes the arm, so an
        # already-handed-off PR never pays for the ruff/mypy subprocess below.
        measured: tuple[str, red_root.RedRootDiagnosis] | None = None
        if (
            agent.config.ci_pending_green_handoff_enabled
            and not escalation_bridge.has_standdown(agent.escalation_standdowns, pr_num)
        ):
            measured = self._measure_pr_head(pr_num, pr_branch)
        if measured is not None:
            head_sha, diagnosis = measured
            if diagnosis.all_passed:
                # Delivery-truth bind, identical to rule 22's: 'delivered' binds to
                # a ``pr_number``-keyed stand-down row written during this fire —
                # the one artifact narration cannot author — not to the actuator's
                # return; a stale row from an earlier act does not count.
                measured_sha = head_sha

                def _deliver(
                    pr_num: int = pr_num, head_sha: str = measured_sha,
                ) -> bool:
                    return escalation_bridge.deliver_handoff(
                        agent.config.memory_dir,
                        escalation_bridge.UnlandedCureHandoff(
                            pr_number=pr_num,
                            # The gate target already confirmed the checkout is
                            # ``pr_branch``, so this *is* the PR head that was
                            # measured (``""`` only when the hash is unreadable).
                            head_sha=head_sha,
                            stamp_count=1,
                        ),
                        backend=agent.escalation_standdowns,
                        message_store=agent.message_store,
                        # Not lap exhaustion: this arm's premise is "locally
                        # green, CI still pending", so it renders its own body
                        # rather than overclaiming the default's spent-lap text.
                        message=escalation_bridge.ci_pending_green_handoff_message(
                            pr_num,
                        ),
                        claim=StanddownClaim.HANDOFF_READY,
                    )

                delivered = audit_checks_phantom.confirm_handoff_delivery(
                    agent.stand_down_verdicts,
                    pr_num,
                    _deliver,
                    # Deliberately *not* stamped with the measured head: a
                    # non-empty head makes the backfilled verdict row live for
                    # ``find_stand_down``, which would let ``gate_stood_down``
                    # evict this PR's queued entry instead of yielding — and an
                    # evicted entry never re-reaches this gate, so the CI-RED
                    # release below could never fire. The row stays inert and
                    # informational; the escalation standdown above carries the
                    # head.
                )
                audit_checks.log_gate_activation(
                    agent.gate_activations,
                    gate="ci_pending_green_handoff",
                    action="handoff" if delivered else "handoff_row_persisted",
                    detail=(
                        f"[ci-pending-green-handoff] PR #{pr_num} branch "
                        f"{pr_branch} — local gates all passed while CI is still "
                        f"pending; handed off (row "
                        f"{'left by actuator' if delivered else 'backfilled'})"
                    ),
                )
                # The ``has_standdown`` filter above guarantees
                # ``deliver_handoff`` genuinely dispatched (never the idempotent
                # no-op), so this is a fired terminal act: exactly one deed.
                agent.deed_ledger.record(
                    deed_ledger.DeedType.HANDOFF_DELIVERED,
                    pr_num,
                    detail=(
                        f"ci_pending_green_handoff handed off PR #{pr_num} "
                        f"(row {'left by actuator' if delivered else 'backfilled'})"
                    ),
                )
                # Memo the row's *identity* (its ``delivered_at``) by re-reading
                # the ledger, not the actuator's return, so the CI-RED release
                # below can tell the arm's own stand-down from a foreign row
                # later recorded under the same PR key. An absent row (the
                # backfill case) memos nothing: the arm owns no suppression to
                # release.
                authored = agent.escalation_standdowns.read().get(pr_num)
                if authored is not None:
                    self._green_handoff_delivered[pr_num] = (
                        authored["delivered_at"]
                    )
                logger.info(
                    "run_task: ci-pending green-handoff — PR #%s branch %s is "
                    "locally green with CI still pending; delivered one "
                    "merge-or-close handoff + keyed stand-down",
                    pr_num, pr_branch,
                )
            else:
                # RED leaves no durable artifact to self-extinguish on, so memo
                # the reading against this head: the next selection of the same
                # unchanged head skips the subprocess, and a new commit (new sha)
                # takes a fresh reading.
                self._green_handoff_red_heads.add((pr_num, head_sha))
        audit_checks.log_gate_activation(
            agent.gate_activations,
            gate="ci_pending_hold",
            action="yielded",
            detail=(
                f"[ci-pending-hold] PR #{pr_num} branch {pr_branch} — CI still "
                f"pending is the sole live blocker (no conflict, no failing "
                f"gate, no unaddressed thread); left queued, slot yielded, no "
                f"session"
            ),
        )
        logger.info(
            "run_task: ci-pending-hold gate — PR #%s branch %s CI still "
            "pending is the sole live blocker; slot yielded, PR left queued, no "
            "session opened",
            pr_num, pr_branch,
        )
        return True

    def _diagnose_red_root_on_pr_head(self, pr_request: PendingPRRequest) -> None:
        """Run the red-root diagnosis on a non-pure-QG lap's PR head (#4216).

        Readiness is read first (memoized per iteration, so the diagnosis's own
        read is free): a header-less directive or a ``GREEN``/``NONE`` PR pays
        for no checkout. The PR head is then positioned by
        :meth:`_positioned_on_pr_head` — a dirty tree or a failed checkout
        skips the diagnosis (info log) and the lap is still admitted — which
        restores the entry branch afterwards. A diagnosis failure is
        wrapped in :class:`RedRootDiagnosisError`, as :meth:`preflight` does.
        """
        agent = self._agent
        pr_branch = pr_request.pr_branch
        match = re.match(r"Remediate PR #(\d+):", pr_request.directive)
        if pr_branch is None or match is None:
            return
        pr_num = int(match.group(1))
        readiness = agent.observe_pr_readiness(pr_num, pr_branch)
        if readiness.ci_state not in ("FAILING", "PENDING"):
            return
        with self._positioned_on_pr_head(pr_num, pr_branch, "red-root") as positioned:
            if not positioned:
                return
            try:
                agent._diagnose_red_root_on_ci_red(pr_request)
            except Exception as exc:
                raise RedRootDiagnosisError(
                    f"red-root diagnosis failed for {pr_branch}",
                ) from exc

    @contextmanager
    def _positioned_on_pr_head(
        self, pr_num: int, pr_branch: str, label: str,
    ) -> Iterator[bool]:
        """Check *pr_branch* out for a measurement, restoring the entry branch.

        The one worktree-moving protocol both the dequeue red-root read and the
        green-handoff gate share, so it cannot drift: a clean tree is required
        to move, a failed local checkout falls back to ``origin/<branch>`` +
        ``checkout_new_branch`` (as
        :func:`~maestro.loop.task_setup.prepare_task_branch` does), and the
        entry branch is restored in a ``finally`` whose
        :class:`~maestro.vcs.VCSError` propagates loudly. Yields ``True`` once
        the PR head is positioned, or ``False`` (after an info log naming
        *label*) when the workspace is unreadable, dirty, or the checkout fails.
        """
        vcs = self._agent._vcs
        try:
            entry_branch = vcs.get_current_branch()
            dirty = bool(vcs.porcelain_status().strip())
        except VCSError as exc:
            logger.info(
                "run_task: %s — skipping PR #%s: cannot read the workspace "
                "state (%s)", label, pr_num, exc,
            )
            yield False
            return
        moved = entry_branch != pr_branch
        if moved:
            if dirty:
                logger.info(
                    "run_task: %s — skipping PR #%s: workspace on %s is dirty, "
                    "so checking out %s is unsafe",
                    label, pr_num, entry_branch, pr_branch,
                )
                yield False
                return
            try:
                vcs.checkout(pr_branch)
            except VCSError:
                try:
                    vcs.checkout(f"origin/{pr_branch}")
                    vcs.checkout_new_branch(pr_branch)
                except VCSError as exc:
                    logger.info(
                        "run_task: %s — skipping PR #%s: cannot check out %s "
                        "(%s)", label, pr_num, pr_branch, exc,
                    )
                    vcs.checkout(entry_branch)
                    yield False
                    return
        try:
            yield True
        finally:
            if moved:
                vcs.checkout(entry_branch)

    def _measure_pr_head(
        self, pr_num: int, pr_branch: str,
    ) -> tuple[str, red_root.RedRootDiagnosis] | None:
        """Position the PR head, run ruff → mypy on it, and restore the tree.

        The gate fires *before the pop*, when the loop has left the workspace
        on ``main``, so the arm checks ``pr_branch`` out itself (#4100) — with
        the ``origin/<branch>`` + ``checkout_new_branch`` fallback
        :func:`~maestro.loop.task_setup.prepare_task_branch` uses — and returns
        to the entry branch in a ``finally``. A checkout pushes nothing, so the
        hold's don't-reset-the-CI-clock contract holds. A dirty tree or a
        failed checkout skips the arm (info log) rather than stashing; a failed
        restoration raises :class:`~maestro.vcs.VCSError` loudly. The arm reads
        ``origin/<branch>`` first — a head already memoized RED pays for no
        checkout — and skips when the positioned local head differs from it,
        since a local branch behind or ahead of origin is not the tree CI runs.

        Returns ``(head_sha, diagnosis)``, or ``None`` when the arm is skipped
        (including every skip :meth:`_green_handoff_gate_target` decides).
        """
        vcs = self._agent._vcs
        # The head CI is running is ``origin/<branch>``; a local branch behind
        # or ahead of it is not the tree the handoff would ask to merge. Read
        # it first so a head already known RED pays for no checkout.
        remote_sha = vcs.rev_parse(f"origin/{pr_branch}")
        if not remote_sha:
            logger.info(
                "run_task: ci-pending green-handoff — skipping PR #%s: "
                "origin/%s is unresolvable", pr_num, pr_branch,
            )
            return None
        if (pr_num, remote_sha) in self._green_handoff_red_heads:
            logger.info(
                "run_task: ci-pending green-handoff — skipping PR #%s: head %s "
                "already read RED this run", pr_num, remote_sha,
            )
            return None
        with self._positioned_on_pr_head(
            pr_num, pr_branch, "ci-pending green-handoff",
        ) as positioned:
            if not positioned:
                return None
            head_sha = self._green_handoff_gate_target(pr_num, pr_branch)
            if head_sha is None:
                return None
            if head_sha != remote_sha:
                logger.info(
                    "run_task: ci-pending green-handoff — skipping PR #%s: "
                    "local %s is at %s, not origin's %s, so a gate run would "
                    "measure a tree CI is not running", pr_num, pr_branch,
                    head_sha or "<unreadable>", remote_sha,
                )
                return None
            wt = self._agent.config.work_target()
            # No ``check_runs`` / ``remote_ci_status``: the remote-CI fold is
            # irrelevant here — CI is pending by construction, and the only
            # question the arm asks is whether *my* head is locally green.
            return head_sha, red_root.diagnose_red_root(
                self._agent._red_root_gate_runner,
                red_root.build_red_root_gates(
                    wt.gate_source_path, wt.gate_tests_path, wt.gate_lint_extra_paths,
                ),
            )

    def _green_handoff_gate_target(
        self, pr_num: int, pr_branch: str,
    ) -> str | None:
        """Assert the green-handoff arm reads *this* PR's workspace tree.

        :meth:`~maestro.agent.MaestroAgent._red_root_gate_runner` runs ruff/mypy
        with ``cwd=config.workspace``. :meth:`_measure_pr_head` checks the PR
        branch out first, so this is the loud post-positioning guard: a green
        reading of an unrelated tree (a task branch, ``main``) would deliver a
        durable handoff plus keyed stand-down premised on a PR nobody measured,
        so the arm runs only when the checked-out branch *is* ``pr_branch``.

        Returns the workspace HEAD sha (``""`` when unreadable, which
        :meth:`_measure_pr_head` then refuses as not matching ``origin``), or
        ``None`` on a foreign/detached checkout.

        The RED memo (:attr:`_green_handoff_red_heads`), consulted on
        ``origin``'s sha before any checkout, keys on the *committed* HEAD sha, so an uncommitted edit
        that turns a RED tree green does not release it — the arm waits for the
        commit. That is benign (it only delays a handoff, never delivers a wrong
        one) and deliberate: an uncommitted tree is not the head CI is running
        against, which is the head the handoff asks Thomas to merge.
        """
        vcs = self._agent._vcs
        try:
            current = vcs.get_current_branch()
        except VCSError as exc:
            logger.info(
                "run_task: ci-pending green-handoff — skipping PR #%s: cannot "
                "read the checked-out branch (%s)", pr_num, exc,
            )
            return None
        if current != pr_branch:
            logger.info(
                "run_task: ci-pending green-handoff — skipping PR #%s: workspace "
                "is on %s, not the PR branch %s, so a local gate run would "
                "measure an unrelated tree", pr_num, current, pr_branch,
            )
            return None
        return vcs.get_head_hash() or ""

    def _release_green_handoff_standdown(
        self, pr_num: int, conclusion: str,
    ) -> None:
        """Release a green-handoff stand-down whose CI then concluded RED.

        The arm hands a PR up on a *local* ruff + mypy green while CI is still
        pending, and the ``pr_number``-keyed stand-down it records suppresses
        Tend re-minting for that PR across every head until it merges or closes.
        The local pair is strictly weaker than CI (no pytest, no coverage, no
        browser), so a CI that later concludes RED refutes the premise the
        handoff asserted — and without this release the loop would be muted on
        exactly the red it is able to fix.

        Fires only for a PR :attr:`_green_handoff_delivered` records *this*
        arm handing up, so a rule-22 / lap-budget / reality-refutation stand-down
        on the same PR is never cleared here. Ownership is checked against the
        *live* row, not merely the PR number: the arm's own suppression can be
        cleared by a non-owner (:meth:`route_user_pr_message`'s human-demand
        release, the merge/close pruner) and a foreign deliverer can then record
        its own row under the same key, so the release clears only while the
        live row still carries the ``delivered_at`` the arm memoed at delivery.
        A row that is gone or no longer the arm's drops the memo and releases
        nothing. A green or absent conclusion releases nothing either: the
        handoff's premise still holds, and merge/close stays its normal release.
        Zero SDK cost — one ledger read, one mutate and one activation append,
        and only on the transition.
        """
        authored_at = self._green_handoff_delivered.get(pr_num)
        if authored_at is None:
            return
        if ci_state_from_conclusion(conclusion) != "FAILING":
            return
        agent = self._agent
        del self._green_handoff_delivered[pr_num]
        # ``expected_delivered_at`` makes the release ownership-scoped inside the
        # ledger's own mutate: ``False`` means the row is gone (a merge/close
        # prune, a drained user message) or is a foreign deliverer's recorded
        # under the same key afterwards — either way the arm holds no
        # suppression to release, and nothing is deleted or stamped.
        if not escalation_bridge.clear_standdown(
            agent.escalation_standdowns, pr_num,
            expected_delivered_at=authored_at,
        ):
            return
        audit_checks.log_gate_activation(
            agent.gate_activations,
            gate="ci_pending_green_handoff",
            action="released",
            detail=(
                f"[ci-pending-green-handoff] PR #{pr_num} — CI concluded RED "
                f"after the locally-green handoff; stand-down released so a "
                f"remediation lap can be minted for the PR's own red"
            ),
        )
        logger.info(
            "run_task: ci-pending green-handoff — PR #%s CI concluded RED "
            "after a locally-green handoff; keyed stand-down released",
            pr_num,
        )

    async def gate_stood_down(
        self, goal: str, pr_branch: str | None, source: TaskSource,
    ) -> bool:
        """Honor my own prior keyed stand-down verdict at the dequeue.

        The read side of the chosen-quiet brake. Consults the durable
        ``(pr, head_sha)``-keyed verdict
        (:func:`maestro.remediation_tracker.find_stand_down`) authored by a
        prior remediation task that deliberately stood down. The verdict is
        honored — the request evicted at zero SDK cost — only when **both** hold:

        * a verdict exists for the PR keyed to the PR branch's *current* origin
          head SHA (a new commit changes the head, the lookup misses, and the
          brake auto-releases so a genuine new lap is never masked); **and**
        * the existing merge-truth helper (``_probe_pr_merge_block``) confirms
          the head is still unlanded and the sole remaining gate is Tend PR's.
          That Tend-clearable set is the ownership classifier's, not this gate's
          (proposal 098): :data:`MERGE_BLOCK_BEHIND_MAIN` (Tend merges forward) is
          the sole Tend-clearable blocker. :data:`MERGE_BLOCK_AWAITING_PUSH` is
          *owned* — an unpushed on-branch resolution is the agent's own next act —
          so it is never in the set and the brake releases into a real lap (the
          push) rather than honoring a verdict premised on Tend delivering it. A
          landed or cleanly-mergeable head (``landed_ok``) or any other blocker
          releases the brake too.

        On a honored verdict it records a zero-cost ``partial`` no-op stamped
        ``[stood-down]`` (no fresh ``success``, no remediation minted) so the
        forced-vs-chosen-quiet mirror counts the chosen quiet, counts it as a
        merge-block deferral
        (``record_outcome(pr, noop=True, merge_block=True)``, like the
        same-blocker sibling :meth:`gate_merge_block`) so the eviction is
        visible to the tracker (#2760) without feeding the goal-blind
        ``should_reroute`` outcome list — the clean Tend catch-up merge that
        releases the verdict never resets that list, so a plain no-op would
        exile the PR's still-unattempted comment/overflow work (#2796) — and
        returns ``True`` so the caller
        returns without executing. Ordered immediately
        after :meth:`gate_merged`. A directive without a parseable
        ``Remediate PR #<n>:`` header, an unresolvable branch head, a missing
        verdict, or a released merge-truth state all return ``False``.

        A comment/review-driven goal (any :data:`_COMMENT_REVIEW_PROBLEM_MARKERS`
        marker) is never evicted, matching the sibling :meth:`gate_merge_block`:
        that work is LLM-only — Tend PR's mechanical forward-merge never applies
        a finding or posts a thread reply — so the verdict's "consulting it
        replaces re-curing it" premise is false for it. Because this gate is
        ordered ahead of :meth:`gate_merge_block` in :data:`_DEQUEUE_GATE_TABLE`,
        the check must live here too, or the one force-admitted reply lap on a
        stood-down, still-``BEHIND_MAIN`` PR is evicted before it can deliver the
        reply and stamp the thread serviced (#2968). Zero SDK cost.
        """
        agent = self._agent
        match = re.match(r"Remediate PR #(\d+):", goal)
        if not match:
            return False
        pr_num = int(match.group(1))
        # A comment/review-driven goal (apply or decline an AI bug/suggestion/nit
        # finding, answer a human thread, post the overflow-ack, or the outdated
        # reply-completion marker) is LLM-only work that Tend PR's mechanical
        # forward-merge never does — the verdict's "consulting the verdict
        # replaces re-curing it" premise is false for it, so never evict. This
        # mirrors the sibling ``gate_merge_block`` (checked before the git probe
        # so a comment/overflow goal also avoids the cheap probe); without it the
        # STOOD_DOWN-before-MERGE_BLOCKED table order (#2968) would evict a
        # force-admitted reply lap before ``gate_merge_block``'s matching
        # exemption could spare it, freezing the thread while the head stays
        # static.
        if carries_comment_review_marker(goal):
            return False
        # The verdict is keyed to the EXACT origin head SHA the write side
        # recorded; an unresolvable branch head can never match a verdict.
        head_sha = ""
        if pr_branch:
            head_sha = agent._vcs.rev_parse(f"origin/{pr_branch}") or ""
        if not head_sha:
            return False
        verdict = find_stand_down(pr_num, head_sha, agent.stand_down_verdicts)
        if verdict is None:
            return False
        # Re-verify git merge-truth on every consult: honor the verdict only
        # while the head is genuinely unlanded and Tend PR is the sole remaining
        # pusher. A landed/mergeable head or a non-Tend-clearable blocker
        # releases the brake rather than masking a fork that has actually moved.
        landed_ok, reason = await agent._probe_pr_merge_block(pr_num, pr_branch)
        # The Tend-clearable set is the classifier's, shared verbatim with the
        # sibling ``gate_merge_block`` (proposal 098): AWAITING_PUSH is owned, not
        # in it, so a verdict premised on "Tend delivers this push" is false and
        # the brake releases into a real lap rather than masking a PR the agent
        # should push.
        if landed_ok or reason not in _tend_clearable_blockers(
            tend_catch_up=agent.config.tend_main_merge_enabled,
        ):
            return False
        task_id = uuid.uuid4().hex[:12]
        finding = (
            f"[stood-down] PR #{pr_num} remediation skipped at zero SDK cost "
            f"on branch {pr_branch} — honoring my own prior stand-down verdict "
            f"at head {head_sha[:12]} (git-confirmed unlanded, {reason}); "
            f"consulting the verdict replaces re-curing it"
        )
        record = TaskRecord(
            task_id=task_id,
            goal=goal,
            plan=[],
            # A stand-down lands nothing on main — stamp ``partial``, never
            # ``success``. The marker feeds the chosen-quiet mirror.
            outcome="partial",
            result_summary=finding,
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source=source,
            key_findings=finding,
            pr_head_sha=head_sha,
        )
        agent.memory.record_task(record)
        # Record the eviction via the merge-block class, NOT the
        # ``should_reroute`` outcome list, mirroring the same-blocker sibling
        # ``gate_merge_block`` (#2796): this gate fires only on BEHIND_MAIN, and
        # the clean Tend catch-up merge that advances the head (releasing the
        # verdict) routes nothing through the branch-state
        # ``remediation_tracker.reset`` — so a plain no-op would arm re-mint
        # suppression that outlives the verdict and exiles the PR's
        # still-unattempted comment/overflow work. The ``partial``
        # ``TaskRecord`` above still feeds ``measure_unlanded_remediations``,
        # keeping the lap-budget standdown reachable for a genuinely stuck PR
        # (#2760).
        agent.remediation_tracker.record_outcome(
            pr_num, noop=True, merge_block=True,
        )
        logger.info(
            "run_task: stood-down gate — honoring prior (pr=#%s, head=%s) "
            "stand-down verdict; evicting with no routed task (task %s)",
            pr_num, head_sha[:12], task_id,
        )
        return True

    async def gate_merge_block(
        self, goal: str, pr_branch: str | None, source: TaskSource,
    ) -> bool:
        """Skip SDK spend on a PR whose blocker Tend PR clears mechanically.

        Probes the PR's deterministic merge-blocker *before* ``run()`` opens
        an SDK session. When the reason falls in the classifier-derived
        :func:`_tend_clearable_blockers` set — :data:`MERGE_BLOCK_BEHIND_MAIN`
        (Tend PR merges ``origin/main`` forward), the sole Tend-clearable blocker
        (:data:`MERGE_BLOCK_AWAITING_PUSH` is *owned*, not Tend-clearable — the
        agent pushes its own on-branch resolution) — an
        LLM remediation would burn budget re-doing work the mechanical Tend PR
        pass owns — so record a zero-cost no-op :class:`TaskRecord`, count it as
        a merge-block deferral (the separate ``merge_block`` class, *not* the
        ``should_reroute`` outcome list, so it never exiles a PR's unattempted
        comment/overflow work — #2796), surface the named blocker via
        ``MaestroAgent._notify_best_effort``, and return ``True`` so the caller
        returns without executing. Any other reason — a conflict, CI failure
        (including on a behind head, which the probe reads as ``CI_FAILING``),
        reviewer feedback, or a cleanly mergeable branch — returns ``False`` so
        the normal remediation path proceeds. Zero SDK cost; reuses
        ``MaestroAgent._probe_pr_merge_block`` so the gate and the post-run
        escalation agree on one host verdict.
        """
        agent = self._agent
        match = re.match(r"Remediate PR #(\d+):", goal)
        if not match:
            return False
        pr_num = int(match.group(1))
        # A comment/review-driven goal (apply or decline an AI bug/suggestion/nit
        # finding, answer a human thread, post the overflow-ack) is LLM-only work
        # that Tend PR's mechanical forward-merge never does — the BEHIND_MAIN
        # "Tend clears this" premise is false for it, so never skip. Only the
        # genuinely Tend-clearable verdict below should skip, mirroring
        # ``_is_pure_quality_gates_directive``. Checked before the git probe so a
        # comment/overflow goal also avoids the (cheap) probe.
        if carries_comment_review_marker(goal):
            return False
        landed_ok, reason = await agent._probe_pr_merge_block(pr_num, pr_branch)
        # The Tend-clearable set is the classifier's, shared verbatim with the
        # sibling ``gate_stood_down`` (proposal 098): AWAITING_PUSH is owned, not
        # in it — an unpushed on-branch resolution is the agent's own next act — so
        # the gate must NOT skip it to Tend, leaving the request runnable for the
        # loop's push backstop.
        if landed_ok or reason not in _tend_clearable_blockers(
            tend_catch_up=agent.config.tend_main_merge_enabled,
        ):
            return False
        # Stamp the origin head so ``count_branch_resolved_partials`` can
        # discount this lap once Tend's catch-up merge advances the branch
        # (#2595, #4218). Resolved only after the probe decides to skip, so a
        # pass-through pays no extra rev-parse. ``base_head_sha`` stays
        # unstamped to match ``gate_stood_down`` — change both or neither.
        head_sha = (
            agent._vcs.rev_parse(f"origin/{pr_branch}") if pr_branch else None
        )
        task_id = uuid.uuid4().hex[:12]
        finding = (
            f"{Marker.MERGE_BLOCK} PR #{pr_num} remediation skipped at zero SDK "
            f"cost on branch {pr_branch} — blocker {reason} is cleared by Tend "
            f"PR, not the LLM"
        )
        record = TaskRecord(
            task_id=task_id,
            goal=goal,
            plan=[],
            # git-merge-truth shows the PR unlanded (BEHIND_MAIN), so a zero-cost
            # skip must not stamp ``success`` — that would be a false-success the
            # recurrence sensor counts (Trust-git-over-stamps).
            # Record ``partial``: the skip is a valid no-op, but nothing landed.
            outcome="partial",
            result_summary=finding,
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source=source,
            key_findings=finding,
            pr_head_sha=head_sha or None,
        )
        agent.memory.record_task(record)
        # Record via the merge-block class, NOT the ``should_reroute`` outcome
        # list (#2796). A merge-block skip is a deliberate "Tend PR clears this
        # mechanically" deferral, not a failed LLM remediation — the
        # comment/overflow nit was never read, let alone attempted. Feeding it to
        # the goal-blind repetition gate would permanently exile a PR's
        # still-unattempted review/overflow work from re-route. The ``partial``
        # ``TaskRecord`` above still feeds ``measure_unlanded_remediations``, so
        # the #2783 lap-budget standdown stays reachable for a genuinely
        # BEHIND_MAIN-stuck PR.
        agent.remediation_tracker.record_outcome(
            pr_num, noop=True, merge_block=True,
        )
        logger.info(
            "run_task: merge-block gate — skipping PR remediation for "
            "branch %s (blocker %s, task %s)",
            pr_branch, reason, task_id,
        )
        # Only BEHIND_MAIN reaches here (proposal 098): AWAITING_PUSH is owned,
        # not Tend-clearable, so the gate above never skips it. Name the
        # forward-merge call-to-action so the escalation is actionable.
        action = (
            "Branch trails origin/main; Tend PR merges it forward — if this "
            "recurs, Tend PR may be stalled."
        )
        await agent._notify_best_effort(
            f"PR #{pr_num} dequeued for remediation but blocked by {reason}; "
            f"skipping LLM remediation at zero SDK cost. {action}",
            failure_log=(
                f"merge-block gate escalation notify failed for #{pr_num}"
            ),
        )
        return True

    async def gate_branch_resolved(
        self, goal: str, pr_branch: str | None, source: TaskSource,
    ) -> bool:
        """Stop the wheel on a PR that keeps resolving-on-branch without landing.

        Detects the agent's own documented costume by its literal signatures:
        ``count_branch_resolved_partials`` counts prior ``Remediate PR #<n>``
        history laps stamped ``partial`` that carry **any** undelivered
        signature — the on-branch costume wording, the mechanical
        ``[merge-block]`` skip, *or* the owned ``[owned-push-available]`` stamp
        of a lap whose push failed — each one a non-event where the resolution
        exists only on the branch and nothing reached ``main``. The count
        accumulates by ``#NNNN`` lineage **regardless of which blocker
        label** (CONFLICTING / AWAITING_PUSH / BEHIND_MAIN) each prior lap
        recorded, so a verdict that shifts between laps cannot reset or dodge it.
        Counting the owned stamp is what keeps this cheap brake armed (proposal
        098): an AWAITING_PUSH lineage is *owned*, so it no longer accrues the
        Tend-clearable markers at all — without the owned stamp a
        repeatedly-failing push would run full SDK laps to the escalation-bridge
        lap budget instead of latching here.

        Progress-aware: laps whose recorded ``pr_head_sha`` is a proper
        ancestor of the current ``origin/<pr_branch>`` head are discounted —
        Tend pushed that lap's resolution, so it was not a costume. Laps with
        no ``pr_head_sha`` (legacy records) and laps whose head has not advanced
        keep counting (#2595). The gate's own skip stamps are always excluded
        from the count so the latch cannot re-arm itself.

        When the undelivered count reaches
        ``[loop] branch_resolved_repetition_threshold``, an LLM remediation
        would just produce the same costume again, so record a zero-cost
        ``partial`` no-op stamped ``[branch-resolved]`` (never ``success`` —
        git-merge-truth shows the PR unlanded), open no SDK session, escalate
        **once** to the only actors who can land it (Thomas or Tend PR) via
        ``MaestroAgent._notify_best_effort``, and return ``True`` so the caller
        returns without executing.

        Unlike the sibling :meth:`gate_stood_down` / :meth:`gate_merge_block`, a
        comment/review-driven goal is **not** exempted here (#2968), and that is
        deliberate: those two evict at zero cost *before* the lap runs, so their
        exemption is what lets the one force-admitted reply lap actually deliver;
        this gate instead fires only after
        ``branch_resolved_repetition_threshold`` laps have already **run** and
        each attempted delivery, resolving on-branch without landing. A reply lap
        that delivers stamps its thread serviced and breaks the treadmill before
        this gate can count it, so only a reply the loop repeatedly fails to
        deliver reaches the threshold — and escalating that once to Thomas / Tend
        PR (the sole actors who can land it) is the intended bound, the opposite
        of the #2968 zero-attempt treadmill rather than a recurrence of it one
        gate later.

        Ordered immediately after :meth:`gate_merge_block`. Below threshold, or
        when ``[loop] branch_resolved_gate_enabled`` is False, or when the
        directive has no parseable ``Remediate PR #<n>:`` header, returns
        ``False`` so the normal remediation path proceeds. Zero SDK cost.
        """
        agent = self._agent
        if not agent.config.branch_resolved_gate_enabled:
            return False
        match = re.match(r"Remediate PR #(\d+):", goal)
        if not match:
            return False
        pr_num = int(match.group(1))
        recent = [asdict(t) for t in agent.memory.get_recent_tasks(50)]

        # Fast-path: if the unfiltered count is already below threshold there is
        # nothing to do — skip the VCS call entirely so this path stays zero-cost
        # for the common (non-firing) case.
        unfiltered = count_branch_resolved_partials(recent, pr_num)
        if unfiltered < agent.config.branch_resolved_repetition_threshold:
            return False

        # Build the progress predicate: a lap is "delivered" when its resolution
        # reached origin — the origin head advanced past the SHA the lap
        # recorded. Use the VCS layer (no new network fetch; run() pushes PR
        # branches directly and tend_evolution_prs runs top-priority every
        # iteration, so origin refs are fresh). Fall back to unfiltered counting
        # if the branch is absent or git fails.
        current_head: str | None = None
        is_delivered = None
        if pr_branch:
            current_head = agent._vcs.rev_parse(f"origin/{pr_branch}")
        if current_head:
            vcs = agent._vcs

            def is_delivered(recorded_sha: str) -> bool:
                # Proper ancestor: the recorded SHA must be strictly before
                # current_head. is_ancestor(x, x) is True (equal), so guard
                # against the equal case — a lap stamped at the current head
                # has NOT been outrun by a Tend push yet.
                return (
                    recorded_sha != current_head
                    and vcs.is_ancestor(recorded_sha, current_head)
                )

        count = count_branch_resolved_partials(
            recent, pr_num, is_delivered=is_delivered,
        )
        if count < agent.config.branch_resolved_repetition_threshold:
            return False

        # Capture the current origin head SHA so this skip's own record is
        # honest (and harmless under the self-stamp exclusion in the counter).
        task_id = uuid.uuid4().hex[:12]
        finding = (
            f"{Marker.BRANCH_RESOLVED} PR #{pr_num} remediation skipped at zero "
            f"SDK cost on branch {pr_branch} — {count} undelivered Tend-clearable "
            f"laps landed nothing on main; escalating instead of re-lapping"
        )
        record = TaskRecord(
            task_id=task_id,
            goal=goal,
            plan=[],
            # The costume is a non-event: the resolution never reached main, so
            # the skip must stamp ``partial`` (nothing landed), never
            # ``success`` — Merge-or-nothing.
            outcome="partial",
            result_summary=finding,
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source=source,
            key_findings=finding,
            pr_head_sha=current_head,
        )
        agent.memory.record_task(record)
        logger.info(
            "run_task: branch-resolved gate — skipping PR remediation for "
            "branch %s (%d undelivered laps, task %s)",
            pr_branch, count, task_id,
        )
        await agent._notify_best_effort(
            f"PR #{pr_num} has been resolved-on-branch {count} time(s) without "
            f"landing on main; skipping further LLM remediation at zero SDK "
            f"cost. Only Tend PR (push) or a human can land it — check why "
            f"the on-branch resolution is not merging.",
            failure_log=(
                f"branch-resolved gate escalation notify failed for #{pr_num}"
            ),
        )
        return True

    async def classify_dequeue(
        self, ctx: _DequeueContext,
    ) -> PrRemediationState:
        """Classify a popped PR request into its dequeue-face lifecycle state.

        Walks :data:`_DEQUEUE_GATE_TABLE` in order and returns the
        :class:`PrRemediationState` of the **first** gate that fires — each
        gate's own side effects (the zero-cost ``TaskRecord``, the escalation
        notify) run as it fires, exactly as the bare ``if gate(...): return``
        sequence did before. When no gate fires the request becomes a real
        task, so this returns :attr:`PrRemediationState.REMEDIABLE`.

        The chain order is data, not prose: :data:`_DEQUEUE_GATE_TABLE` pins
        merged-exclusion ahead of the commit-ancestry landed gate, then the
        host-closed, ready, stand-down, merge-block, branch-resolved and preflight
        gates, and a guard test fails if the tuple is reordered (proposal 070,
        step 2). Gate
        dispatch goes through the agent's delegating shims — not this
        controller's methods directly — because the test suite monkey-patches
        those shims on the agent instance.
        """
        for state, gate_fn in _DEQUEUE_GATE_TABLE:
            if await gate_fn(self, ctx):
                return state
        return PrRemediationState.REMEDIABLE

    async def dequeue_gates(
        self,
        *,
        goal: str,
        pr_branch: str | None,
        source: TaskSource,
        entry: PendingPRRequest,
    ) -> bool:
        """Run the dequeue-face gate sequence for a popped PR request.

        Returns ``True`` when any gate fired (the caller returns without
        executing a task); ``False`` — the request classified
        :attr:`PrRemediationState.REMEDIABLE`, the sole runnable state — lets the
        normal remediation path proceed. Delegates the ordered walk to
        :meth:`classify_dequeue`, whose :data:`_DEQUEUE_GATE_TABLE` is the single
        definition site for the chain order.

        The run-vs-skip boolean is the single decision site
        :func:`~maestro.pr_remediation_state.classify_pr_remediation_disposition`
        — it folds the eight dequeue-face states into the four dispositions that
        give 070's state enum one run-vs-skip site, returning
        :attr:`~maestro.pr_remediation_state.PrRemediationDisposition.RUN` only
        for :attr:`PrRemediationState.REMEDIABLE`. The gates' side effects (the
        zero-cost ``TaskRecord``, the escalation notify) have already run inside
        :meth:`classify_dequeue` by the time their state is returned, so only the
        final boolean is derived here.
        """
        ctx = _DequeueContext(
            goal=goal, pr_branch=pr_branch, source=source, entry=entry,
        )
        state = await self.classify_dequeue(ctx)
        disposition = classify_pr_remediation_disposition(state)
        return disposition is not PrRemediationDisposition.RUN

    async def _run_preflight_dequeue(self, ctx: _DequeueContext) -> bool:
        """Pre-remediation quality gate: close a self-resolved request.

        If the directive is a pure quality-gates request *and* the PR branch's
        merged tree already passes all gates, a prior remediation commit or a
        main-merge resolved the problem before the LLM touched it — record a
        zero-cost ``success`` no-op stamped ``[preflight]``, route the no-op
        outcome (which trips the repetition gate after enough skips), and
        return ``True``. CHANGES_REQUESTED (reviewer feedback) and
        conflict-routed entries are *not* implied resolved by green
        ruff/mypy/pytest on the branch, so they fall through (``False``). The
        preflight runner in :data:`_DEQUEUE_GATE_TABLE` maps this to
        :attr:`PrRemediationState.PREFLIGHT_CLEAN`.

        Red-root diagnosis covers *every* admitted CI-red/pending lap (#4216):
        a pure QG directive gets it inside :meth:`preflight`, every other
        directive (the pre-merge ``ci_failure_on_head`` route, conflicts,
        reviewer feedback) gets it here via
        :meth:`_diagnose_red_root_on_pr_head` — so each lap runs it exactly
        once. The pure-QG filter scopes only the zero-cost skip.
        """
        agent = self._agent
        if not _is_pure_quality_gates_directive(ctx.goal):
            self._diagnose_red_root_on_pr_head(ctx.entry)
            return False
        if not await agent._preflight_pr_remediation(ctx.entry):
            return False
        task_id = uuid.uuid4().hex[:12]
        finding = (
            f"{Marker.PREFLIGHT} Quality gates already pass on branch "
            f"{ctx.pr_branch} — remediation skipped at zero SDK cost"
        )
        record = TaskRecord(
            task_id=task_id,
            goal=ctx.goal,
            plan=[],
            outcome="success",
            result_summary=finding,
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source=ctx.source,
            key_findings=finding,
        )
        agent.memory.record_task(record)
        logger.info(
            "run_task: preflight pass — skipping PR remediation "
            "for branch %s (task %s)",
            ctx.pr_branch, task_id,
        )
        # A preflight-skip reports ``success`` but changes nothing — count it
        # as a no-op so the repetition gate trips when a PR keeps skipping
        # without ever merging, then escalate the real merge-blocker to the
        # only actor who can clear it.
        await agent._record_preflight_skip_outcome(ctx.goal, ctx.pr_branch)
        return True

    def _gate_pr_remediation_landed(
        self, pr_request: PendingPRRequest, source: TaskSource,
    ) -> bool:
        """Evict a PR whose branch HEAD is already an ancestor of ``origin/main``.

        The commit-ancestry complement to :meth:`_gate_pr_remediation_merged`.
        Where the merged-exclusion gate asks the *PR object* whether it merged
        (host API / git-log subject), this gate asks git the one settling
        question the #2662 phantom slips past: is the PR branch HEAD already an
        ancestor of ``origin/main``? A cure can land via squash, a sibling PR, or
        a prior Tend merge while the PR object stays OPEN — its content is in
        ``main`` yet the merged gate, keyed on the object, never evicts it and the
        remediation wheel re-routes already-landed work.

        Fetch-first via the chokepoint — :meth:`is_pr_settled` runs a fail-loud
        ``git fetch origin`` (on the per-iteration cache miss) before returning,
        freshening every remote-tracking ref so the ancestry read sees fresh
        refs, not a stale local tree that would *miss* an already-landed cure.
        The gate adds no second fetch of its own — symmetric with the
        admission-face guard, which also leans on the chokepoint's fetch; a
        chokepoint fetch failure propagates loudly, so no merge-truth verdict is
        ever authored on unfreshened refs — the value made machinery, not recited.

        On a confirmed ancestor (:meth:`_branch_merged_into_main` against the
        ``origin/<branch>`` remote-tracking ref) the work has already landed, so
        record a zero-cost ``partial`` no-op stamped :attr:`Marker.LANDED`
        (``partial``, never ``success`` — this task *caused* nothing; the landing
        predates it), count it as a repetition-gate no-op so the wheel stops
        re-routing it, and return ``True`` to evict without opening an SDK
        session. A non-ancestor head — including the fail-closed ``False`` from an
        empty/unreadable branch — returns ``False`` so genuine remediation
        proceeds untouched. A directive without a parseable
        ``Remediate PR #<n>:`` header or a branchless request also returns
        ``False``. Zero SDK cost; the only direct I/O is the ancestry
        ``git merge-base`` subprocess (the chokepoint owns the fetch).
        """
        agent = self._agent
        goal = pr_request.directive
        match = re.match(r"Remediate PR #(\d+):", goal)
        if not match:
            return False
        pr_branch = pr_request.pr_branch
        if not pr_branch:
            return False
        pr_num = int(match.group(1))
        # Centralized observation (proposal 087): a host-confirmed landing
        # (``branch_landed``) evicts immediately. The PR-object observation
        # cannot see a cure that landed via squash, a sibling PR, or a prior
        # Tend merge while the PR object reads OPEN — so when the host does not
        # confirm a landing, fall back to this gate's existing
        # ``origin/<branch>`` ancestry probe for exactly that case.
        #
        # No second fetch of our own: ``is_pr_settled`` already ran a fail-loud
        # ``git fetch origin`` (on the per-iteration cache miss) BEFORE
        # returning, freshening every remote-tracking ref — including
        # ``origin/<branch>`` — so the ancestry probe below reads fresh refs
        # without a redundant re-fetch, exactly as the admission-face guard
        # does. A chokepoint fetch failure already propagated loudly, so no
        # verdict is ever authored on unfreshened refs. The ``and`` short-
        # circuits: a host-confirmed landing skips the ancestry probe entirely;
        # otherwise git is asked the settling question against the
        # chokepoint-freshened ``origin/<branch>`` ref (the form that resolves
        # in the loop container). A non-ancestor head — or the fail-closed
        # ``False`` from an empty/unreadable branch — falls through.
        if (
            not agent.is_pr_settled(pr_num).branch_landed
            and not agent._branch_merged_into_main(f"origin/{pr_branch}")
        ):
            return False
        task_id = uuid.uuid4().hex[:12]
        finding = (
            f"{Marker.LANDED} PR #{pr_num} remediation skipped at zero SDK cost "
            f"on branch {pr_branch} — git merge-base confirms the head is "
            f"already an ancestor of origin/main (the cure landed before this "
            f"task via squash/sibling/Tend merge); refusing to re-route "
            f"landed work"
        )
        record = TaskRecord(
            task_id=task_id,
            goal=goal,
            plan=[],
            # The cure landed, but THIS task moved nothing — it caused no merge.
            # Stamp ``partial`` (never ``success``): a success here would feed the
            # false-success-recurrence sensor the eviction exists to starve.
            outcome="partial",
            result_summary=finding,
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source=source,
            key_findings=finding,
        )
        agent.memory.record_task(record)
        # Count the eviction as a repetition-gate no-op so a PR that keeps
        # re-presenting an already-landed head stops being re-routed.
        agent.remediation_tracker.record_outcome(pr_num, noop=True)
        logger.info(
            "run_task: landed gate — PR #%s branch %s head already an ancestor "
            "of origin/main; evicted with a %s no-op, no routed task "
            "(task %s)",
            pr_num, pr_branch, Marker.LANDED, task_id,
        )
        return True

    def _gate_pr_remediation_ready(
        self, pr_request: PendingPRRequest, source: TaskSource,
    ) -> bool:
        """Stand down on an open PR whose every code-clearable blocker is clear.

        The observed-readiness complement to :meth:`_gate_pr_remediation_landed`
        (proposal 089 step 3). Where the landed gate asks git "has this already
        landed?", this gate asks a live
        :class:`~maestro.pr_facts.PRReadiness` "is there still work I can do
        on this PR?" — and when the answer is no (the PR is open and every
        code-clearable blocker is clear, so the sole remaining gate is Thomas's
        approval or Tend's push), the work is done and the PR is in Thomas's
        court. The readiness fact is recomputed live each pop, so a reappearing
        blocker re-admits the PR immediately. A reply-completion goal returns
        ``False`` before observing: readiness counts only non-outdated threads,
        so it cannot see the outdated threads a reply lap answers (#4122).

        Gated behind ``[loop] observed_ready_standdown_enabled`` (default on):
        with the flag off this returns ``False`` immediately, so the dequeue
        chain is byte-identical to the pre-089 chain. On a ready-and-waiting
        verdict, record a zero-cost ``partial``
        :class:`~maestro.memory.TaskRecord` stamped
        :attr:`Marker.READY` — **never ``success``** (this lap landed nothing) —
        carrying the readiness snapshot for the no-progress sensor, count it a
        repetition-gate no-op so the wheel stops re-routing it, and return
        ``True`` to evict without opening an SDK session. A directive without a
        parseable ``Remediate PR #<n>:`` header or a branchless request returns
        ``False`` so genuine remediation proceeds. Zero SDK cost; the only I/O is
        the memoized readiness observation's host/VCS reads.
        """
        agent = self._agent
        if not agent.config.observed_ready_standdown_enabled:
            return False
        goal = pr_request.directive
        if _REPLY_COMPLETION_PROBLEM_MARKER in goal:
            return False
        match = re.match(r"Remediate PR #(\d+):", goal)
        if not match:
            return False
        pr_branch = pr_request.pr_branch
        if not pr_branch:
            return False
        pr_num = int(match.group(1))
        readiness = agent.observe_pr_readiness(pr_num, pr_branch)
        if not readiness.ready_and_waiting:
            return False
        task_id = uuid.uuid4().hex[:12]
        finding = (
            f"{Marker.READY} PR #{pr_num} remediation skipped at zero SDK cost "
            f"on branch {pr_branch} — observed readiness reports every "
            f"code-clearable blocker clear (CI {readiness.ci_state}, "
            f"0 unresolved threads, 0 unaddressed overflow, branch current); "
            f"the work is done and the PR awaits approval/push. Standing down "
            f"rather than re-touching finished work"
        )
        record = TaskRecord(
            task_id=task_id,
            goal=goal,
            plan=[],
            # The work is done, but THIS lap landed nothing — stamp ``partial``
            # (never ``success``): a success here would feed the
            # false-success-recurrence sensor the stand-down exists to starve.
            outcome="partial",
            result_summary=finding,
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source=source,
            key_findings=finding,
            pr_readiness_snapshot=asdict(readiness),
        )
        agent.memory.record_task(record)
        # Count the stand-down as a repetition-gate no-op so a PR that keeps
        # re-presenting a ready-and-waiting head stops being re-routed.
        agent.remediation_tracker.record_outcome(pr_num, noop=True)
        logger.info(
            "run_task: ready gate — PR #%s branch %s observed ready-and-waiting; "
            "evicted with a %s no-op, no routed task (task %s)",
            pr_num, pr_branch, Marker.READY, task_id,
        )
        return True

    def _plan_for_pr_number(self, pr_num: int) -> EvolutionPlan | None:
        """Return the plan whose ``pr_url`` resolves to *pr_num*, else ``None``.

        The backing-plan lookup shared by the host-closed dequeue gate and the
        local version-archive merge-truth tier: walks
        :meth:`Memory.get_all_plans`, parsing each ``pr_url`` through
        :meth:`EvolutionEngine._pr_number_from_url`, and returns the first match.
        An unparseable ``pr_url`` is skipped (never a crash). Zero SDK cost; the
        only I/O is the bounded plans read.
        """
        agent = self._agent
        for plan in agent.memory.get_all_plans():
            if not plan.pr_url:
                continue
            try:
                if agent.evolution._pr_number_from_url(plan.pr_url) == pr_num:
                    return plan
            except ValueError:
                continue
        return None

    async def _gate_pr_remediation_host_closed(
        self, pr_request: PendingPRRequest, source: TaskSource,
    ) -> bool:
        """Evict a remediation lap on a PR the host reports CLOSED-and-unmerged.

        The reality-grounding terminal the ``mergedAt``-keyed settled arms
        (:meth:`_gate_pr_remediation_merged` / :meth:`_gate_pr_remediation_landed`)
        structurally miss: both key eviction on the branch having *reached*
        ``main`` (``branch_landed`` / ``git merge-base --is-ancestor``), so a PR
        the human already CLOSED **without** merging reads ``mergedAt`` null →
        not-landed → re-admitted every iteration, and a full remediation lap runs
        on a PR no lap can land (#3112). This gate reads the same per-iteration
        :meth:`is_pr_settled` :class:`~maestro.pr_facts.MergeTruth` — no new
        network — and, when
        :func:`~maestro.remediation_tracker.classify_host_pr_terminal` reports the
        :data:`~maestro.remediation_tracker.HOST_CLOSED_UNMERGED` terminal (host
        state CLOSED, branch unmerged), evicts at zero SDK cost: it routes an
        ``implemented`` backing plan to the ``rejected`` terminal via the same
        ``update_plan_status(..., "rejected", ...)`` the tend-time PR-close path
        uses (so ``_process_rejected_plans`` later applies
        :meth:`EvolutionEngine.learn_from_rejection` on its own session — this arm
        opens none), records a ``partial`` :attr:`Marker.HOST_CLOSED` no-op,
        stamps one ``[host-closed]`` ``gate_activations.jsonl`` row, and notifies
        once. Honoring a terminal the human authored is the opposite of "giving
        up on a live PR" — the close is already chosen; the loop merely reads it.

        Ordered after the merged/landed settled arms and before the session mint.
        A directive without a parseable ``Remediate PR #<n>:`` header, an OPEN PR,
        or a merged PR returns ``False`` so genuine remediation proceeds. Only an
        ``implemented`` plan is routed (mirroring ``check_merged_plans``, under
        ``expect_status``); any other status — ``rejected`` by a prior pop, or the
        agent's own ``obsolete``/``failed`` terminal — is evicted (``True``)
        untouched, with **no** deed, activation row or escalation, so a
        plan-backed PR escalates at most once; a PR with no locatable plan has no
        status to latch on and escalates on each pop. Returns ``True`` when the gate
        fires. Zero SDK cost; the only I/O is the memoized merge-truth read, the
        plans read, the plan-status/task writes, and the single activation append.
        """
        agent = self._agent
        goal = pr_request.directive
        pr_num = pr_number_from_remediation_goal(goal)
        if pr_num is None:
            return False
        if (
            classify_host_pr_terminal(agent.is_pr_settled(pr_num))
            != HOST_CLOSED_UNMERGED
        ):
            return False
        pr_branch = pr_request.pr_branch
        # Route the backing plan to ``rejected`` only from ``implemented``, as
        # the tend-time PR-close path does; ``expect_status`` checks that under
        # the plans lock, so any other status (or a racing retirement) refuses. ``escalate`` is the
        # escalate-at-most-once guard: a plan a prior pop rejected, or one the
        # agent already retired (``obsolete``/``failed``), evicts silently (#4076).
        plan = agent._plan_for_pr_number(pr_num)
        routed = (
            "backing plan routed to rejected" if plan is not None
            else "no backing plan located"
        )
        escalate = plan is None or agent.memory.update_plan_status(
            plan.plan_id,
            "rejected",
            rejection_reason="PR closed without merging (host CLOSED-unmerged)",
            expect_status="implemented",
        )
        task_id = uuid.uuid4().hex[:12]
        finding = (
            f"{Marker.HOST_CLOSED} PR #{pr_num} remediation evicted at zero SDK "
            f"cost on branch {pr_branch} — the code host reports it CLOSED "
            f"without merging (the human's terminal); no lap can land a closed "
            f"PR, so it is evicted instead of re-lapping"
        )
        record = TaskRecord(
            task_id=task_id,
            goal=goal,
            plan=[],
            # The human closed it, but THIS lap landed nothing — stamp
            # ``partial`` (never ``success``): a success here would feed the
            # false-success-recurrence sensor the eviction exists to starve.
            outcome="partial",
            result_summary=finding,
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source=source,
            key_findings=finding,
        )
        agent.memory.record_task(record)
        # Count the eviction as a repetition-gate no-op so a PR that keeps
        # re-presenting a closed-unmerged head stops being re-routed.
        agent.remediation_tracker.record_outcome(pr_num, noop=True)
        if not escalate:
            logger.info(
                "run_task: host-closed gate — PR #%s branch %s backing plan is "
                "%s, not routed; evicted silently, no escalation (task %s)",
                pr_num, pr_branch, plan.status if plan else "gone", task_id,
            )
            return True
        # Deed ledger: this pop is the one that honors the ``gh pr close``
        # terminal (routing an ``implemented`` plan, or no plan at all), once.
        agent.deed_ledger.record(
            deed_ledger.DeedType.PR_CLOSE,
            pr_num,
            detail=(
                f"host-closed terminal honored for PR #{pr_num} on branch "
                f"{pr_branch}; {routed}"
            ),
        )
        audit_checks.log_gate_activation(
            agent.gate_activations,
            gate="host_closed",
            action="evicted",
            detail=(
                f"{Marker.HOST_CLOSED} PR #{pr_num} branch {pr_branch} — host "
                f"reports CLOSED-unmerged; evicted, {routed}, no session"
            ),
        )
        logger.info(
            "run_task: host-closed gate — PR #%s branch %s reported CLOSED "
            "without merging; evicted with a %s no-op, %s, no routed task "
            "(task %s)",
            pr_num, pr_branch, Marker.HOST_CLOSED, routed, task_id,
        )
        await agent._notify_best_effort(
            f"PR #{pr_num} was closed without merging — honoring that terminal: "
            f"evicting its remediation ({routed}). No further laps will run on it.",
            failure_log=f"host-closed gate escalation notify failed for #{pr_num}",
        )
        return True

    async def _escalate_stuck_pr(
        self, pr_num: int, pr_branch: str | None,
        *, items_remaining: int | None = None,
    ) -> None:
        """Notify the operator that a serviced PR keeps not merging.

        Computes the deterministic merge-blocker reason from already-fetched
        VCS / code-host state (no SDK call) and routes a one-shot notification.
        A git-driven latch returns silently once the probe reports no git
        blocker; an items-driven latch returns only once ``is_pr_settled``
        reads the PR settled, since open review items are not a git fact.

        ``items_remaining`` carries the count of outstanding review items the
        finishing lap measured. When it is positive the latch is *items-driven*
        (unresolved review threads / overflow findings), which has no git
        footprint — so the merge-block probe would latch onto a transient
        ``CI_PENDING`` / ``CI_FAILING`` and mislabel a self-clearing state as
        needing manual action. In that case name the real review-items blocker
        instead (#2744); a ``None`` / zero count keeps the git merge-block CTA.
        """
        agent = self._agent
        items_driven = items_remaining is not None and items_remaining > 0
        reason = ""
        if items_driven:
            if agent.is_pr_settled(pr_num).settled:
                return
        else:
            landed_ok, reason = await agent._probe_pr_merge_block(
                pr_num, pr_branch,
            )
            if landed_ok:
                return
        # Flight-record the escalation on the PR ledger so the entry shows
        # whether the operator was already notified for this stuck episode;
        # observe_head() clears the stamp once the head moves.
        agent.pr_ledger.mark_escalated(pr_num)
        # Durable hand-up (proposal 080 Phase 3, #2783). ``mark_escalated`` above
        # is head-scoped — ``observe_head()`` wipes it on the next head-advance —
        # so a PR that keeps advancing its head (as #2727 did 17x) is
        # re-escalated forever and never durably handed up. ``deliver_handoff``
        # records a ``pr_number``-keyed standdown that survives head churn:
        # it sends exactly one merge-or-close message and then suppresses
        # further minting until the host reports the PR merged/closed (the Tend
        # route's ``prune_resolved_standdowns`` clears it on that transition).
        # The PR is confirmed unsettled above (``is_pr_settled`` for an
        # items-driven latch, the git probe otherwise), so this is the right
        # terminal for both the items-driven and git-merge-block branches
        # below. Gated on the lap budget being enabled, mirroring the Tend
        # gate guard — a ``<= 0`` budget keeps the durable standdown fully
        # disabled.
        if agent.config.pr_remediation_lap_budget > 0:
            head_sha = (
                agent._vcs.rev_parse(f"origin/{pr_branch}") or ""
                if pr_branch is not None
                else ""
            )
            deliver_handoff_cfg(
                agent.config,
                UnlandedCureHandoff(
                    pr_number=pr_num,
                    head_sha=head_sha,
                    stamp_count=agent.remediation_tracker.noop_count(pr_num),
                ),
                backend=agent.escalation_standdowns,
                plan_lookup=agent._plan_for_pr_number,
                claim=StanddownClaim.LAP_BUDGET_EXHAUSTED,
            )
        if items_driven:
            logger.info(
                "Remediation repetition gate: PR #%s stuck unmerged after "
                "consecutive no-ops (%d unresolved review item(s)) — "
                "escalating, no re-route",
                pr_num, items_remaining,
            )
            await agent._notify_best_effort(
                f"PR #{pr_num} keeps being serviced without merging. "
                f"{items_remaining} unresolved review item(s) remain — review "
                f"threads awaiting a terminal reply or overflow findings "
                f"awaiting acknowledgement. Manual action required to resolve "
                f"them.",
                failure_log=f"stuck-PR escalation notify failed for #{pr_num}",
            )
            return
        logger.info(
            "Remediation repetition gate: PR #%s stuck unmerged after "
            "consecutive no-ops (merge blocker: %s) — escalating, no re-route",
            pr_num, reason,
        )
        # AWAITING_PUSH is not a host-side gate the operator clears by hand: the
        # resolution is already on-branch, so the call-to-action differs from the
        # manual-resolution blockers (#2457). It is *owned* (proposal 098): the
        # push is the agent's own terminal act, so name it.
        if reason == MERGE_BLOCK_AWAITING_PUSH:
            if pr_branch:
                # The on-branch resolution is my own next act — one push lands it
                # — so name the owned push as the terminal act. AWAITING_PUSH
                # already implies a pushable ``pr_branch`` (``_probe_pr_merge_block``
                # only derives the code inside an ``if pr_branch:`` origin
                # comparison), but the condition re-states it structurally so the
                # interpolation cannot render a literal ``None`` into an
                # operator-facing git command if that probe invariant ever
                # changes; a ``None`` branch falls through to the branch-less
                # wording, which names no command.
                action = (
                    "Resolution is committed on the branch but not yet pushed — "
                    f"push it to land the fix (git push origin HEAD:{pr_branch})."
                )
            else:
                action = (
                    "Resolution is committed on the branch but not yet pushed; "
                    "check why Tend PR has not pushed it."
                )
        else:
            action = "Manual action required to clear it."
        await agent._notify_best_effort(
            f"PR #{pr_num} keeps being serviced without merging. "
            f"Merge blocker: {reason}. {action}",
            failure_log=f"stuck-PR escalation notify failed for #{pr_num}",
        )

    def _remediation_streak_capped(self) -> bool:
        """Return True when the consecutive-remediation cap is engaged.

        The cap (``[loop] max_consecutive_remediation``) bounds how many
        task slots in a row may be served from ``pending_pr_requests``. When
        ``self._consecutive_remediation`` has reached the cap, the next slot
        bypasses remediation and falls through to the queue head, forcing
        task-shape diversity — but ``_run_task_phase`` engages it only when a
        queue head exists, so an empty queue leaves remediation eligible. A
        cap ``<= 0`` disables the throttle and preserves remediation's
        unconditional precedence.
        """
        agent = self._agent
        cap = agent.config.max_consecutive_remediation
        return cap > 0 and agent._consecutive_remediation >= cap

    def _pending_pr_head_stalled(
        self,
        entry: PendingPRRequest,
        recent: list[TaskRecord] | None = None,
    ) -> bool:
        """True when the head ``pending_pr_requests`` *entry* is stalled.

        Resolves the PR branch's current origin head SHA (local
        ``rev_parse`` fallback) and delegates to
        :func:`_is_stalled_remediation_lineage`: ``>= STALLED_REMEDIATION_LAP_CAP``
        consecutive same-SHA ``partial`` laps for the same ``#N`` means the loop
        has re-serviced an externally-blocked PR without its head advancing.
        When True, :meth:`_run_task_phase` leaves the entry queued and the slot
        goes to the first non-stalled entry instead.

        *recent* lets a sibling scan share one ``get_recent_tasks(30)`` tail
        read across every entry it inspects (:meth:`_first_non_stalled_pr_index`);
        omit it for a standalone single-entry check and it is read here.

        Zero SDK cost: only an in-memory history scan plus, when the history
        actually holds remediation laps for this PR, a single ``git rev-parse``.
        A fresh PR with no prior laps can never be stalled, so the git
        round-trip is skipped entirely in the common case.
        """
        agent = self._agent
        if entry.pr_branch is None:
            return False
        pr_num = pr_number_from_remediation_goal(entry.directive)
        if pr_num is None:
            return False
        if recent is None:
            recent = agent.memory.get_recent_tasks(30)
        if not any(
            pr_number_from_remediation_goal(task.goal) == pr_num
            for task in recent
        ):
            return False
        current_head = agent._vcs.rev_parse(
            f"origin/{entry.pr_branch}",
        ) or agent._vcs.rev_parse(entry.pr_branch)
        # Proposal 099: a head that did not advance against a base that *did*
        # move is a lost race, not a stall. Re-key the streak on the current
        # ``origin/main``; an unmeasurable base fails toward re-entry (never
        # stalls) so an unreadable git state can never wedge the PR.
        current_base = agent._vcs.rev_parse("origin/main")
        if current_base is None:
            return False
        return _is_stalled_remediation_lineage(
            entry.directive, current_head, recent,
            current_base=current_base,
        )

    def _mechanical_standdown_verdict(
        self, entry: PendingPRRequest,
    ) -> StandDownVerdict | None:
        """Compute the mechanical chosen-stand-down verdict for *entry*, or None.

        Gathers the inputs :func:`mechanical_standdown_verdict` reasons over —
        the PR number, its branch's current origin head SHA, the trailing run of
        same-PR ``partial``-lap head SHAs, and whether unserviced review-thread
        work remains — read from **both** the directive marker and the live
        :attr:`PRReadiness.unresolved_threads` count (#2907), so the
        thread-clarity precondition is the completion predicate, not a proxy for
        it — then delegates the all-conditions-hold decision to the pure
        predicate. Returns a :class:`StandDownVerdict` only for a stalled
        (``>= STALLED_REMEDIATION_LAP_CAP`` same-SHA partial laps),
        thread-clear, unsettled lineage (per the readiness chokepoint's
        ``settled``, #4102) whose merge state could be observed;
        ``None`` otherwise. Whether the blocker is clearable is not decided
        here: clearability is governed by the caller's salvage oracle
        (:meth:`_dispose_mechanical_standdown` via :meth:`_assess_pr_salvage`).

        The trailing ``partial``-lap run is gathered exactly as
        :func:`_is_stalled_remediation_lineage` counts it — most-recent first,
        ending at the first non-``partial`` same-PR lap (a non-partial lap means
        the head advanced and real work landed) — and the most-recent lap's
        ``(merge blocker: <CODE>)`` is parsed as evidence the merge state was
        observed. On a marker-clear directive whose
        same-SHA run has stalled, the memoized :meth:`observe_pr_readiness`
        chokepoint is consulted once (oracle-last, only after those cheap
        conditions hold): it supplies the live ``unresolved_threads`` count
        folded into the thread-clarity gate (a positive count is owned
        thread-work that blocks the stand-down exactly as a directive marker
        does) and, when no lap named its blocker, derives it live —
        ``branch_conflicting`` → CONFLICTING, ``branch_behind`` →
        BEHIND_MAIN, ``awaiting_approval`` → AWAITING_APPROVAL. An observation
        that confirms *no* blocker code — like an unreadable marker — never
        qualifies: the safe direction for a suppression actuator.

        Zero SDK cost: an in-memory history scan plus, only when the PR has lap
        history, a single ``git rev-parse``, the memoized ``is_pr_settled`` (the
        predicate consults it last, after every cheap condition passes), and — on
        the marker-absent path only — the memoized readiness observation.
        """
        agent = self._agent
        if entry.pr_branch is None:
            return None
        pr_num = pr_number_from_remediation_goal(entry.directive)
        if pr_num is None:
            return None
        recent = agent.memory.get_recent_tasks(30)
        if not any(
            pr_number_from_remediation_goal(task.goal) == pr_num
            for task in recent
        ):
            return None
        current_head = agent._vcs.rev_parse(
            f"origin/{entry.pr_branch}",
        ) or agent._vcs.rev_parse(entry.pr_branch)
        if not current_head:
            return None
        lap_shas: list[str] = []
        blocker: str | None = None
        for task in reversed(recent):
            if pr_number_from_remediation_goal(task.goal or "") != pr_num:
                continue
            if task.outcome != "partial":
                break
            lap_shas.append(task.pr_head_sha or "")
            if blocker is None:
                match = _MERGE_BLOCKER_RE.search(task.result_summary or "")
                if match is not None:
                    blocker = match.group(1)
        marker_threads = carries_comment_review_marker(entry.directive)
        # Thread-clarity is the completion predicate, not a proxy for it: a live
        # unresolved review thread is owned work to answer or refute, never an
        # external gate a merge-blocker status word can excuse (#2907). Fold the
        # live ``unresolved_threads`` count into ``has_unserviced_threads``
        # alongside the directive marker, reading it from the same observation
        # that already backs the blocker fallback. The observation is consulted
        # only after the cheap conditions the pure predicate re-checks already
        # hold — a marker-clear directive and a stalled same-SHA run — so a
        # non-qualifying lineage never pays the host read (oracle-last, mirroring
        # the git oracle the predicate consults last). A marker-present lineage
        # can never stand down, so it short-circuits without the read.
        readiness_unresolved = 0
        if not marker_threads:
            streak = leading_same_sha_streak(lap_shas, current_head)
            if streak >= STALLED_REMEDIATION_LAP_CAP:
                readiness = agent.observe_pr_readiness(pr_num, entry.pr_branch)
                if readiness.settled:
                    # Merged or host-closed (#4102): leave the entry for the
                    # dequeue MERGED / HOST_CLOSED gates to dispose of.
                    return None
                readiness_unresolved = readiness.unresolved_threads
                if blocker is None:
                    # No lap named its ``(merge blocker: <CODE>)``. Rather than
                    # disqualify the lineage outright (the starved-gate bug — the
                    # actuator relied on the LLM having named the blocker), derive
                    # the blocker from the same live observation. An observation
                    # confirming no blocker code (``MERGE_BLOCK_NONE``) leaves it
                    # None and the suppression-safe refusal below declines.
                    derived = readiness.merge_block_reason()
                    if derived != MERGE_BLOCK_NONE:
                        blocker = derived
        has_unserviced_threads = marker_threads or readiness_unresolved > 0
        if blocker is None:
            # Suppression-safe refusal: the actuator does not mechanically
            # stand down a lineage whose merge state it could not observe. An
            # unread lap marker AND a live observation that confirms no blocker
            # code (a CI-failing/pending, already-clear, or otherwise
            # unclassifiable merge state sets none of the three readiness
            # fields) leave the merge state unknown, and an *unknown* state can
            # never be routed into a stand-down verdict.
            return None
        return mechanical_standdown_verdict(
            pr_num,
            current_head,
            lap_shas,
            has_unserviced_threads,
            STALLED_REMEDIATION_LAP_CAP,
            is_pr_merged=lambda n: agent.is_pr_settled(n).branch_landed,
        )

    def _dispose_mechanical_standdown(self, model: SelfModel) -> bool:
        """Mechanically stand down on the head ``pending_pr_requests`` entry.

        The deterministic actuator for the chosen-stand-down clause — the act the
        self-model names as repeatedly avoided at the LLM pop. When the head
        entry is a stalled, thread-clear, unsettled (readiness chokepoint)
        remediation lineage whose merge state could be observed (:meth:`_mechanical_standdown_verdict`) —
        clearability governed by the salvage oracle below — this authors
        the ``[stood-down]`` verdict in loop code: it dequeues the entry, persists the
        durable ``(pr, head_sha)`` verdict the read-side gate
        (:meth:`_gate_pr_remediation_stood_down`) honors on future pops, records a
        zero-cost ``partial`` ``[stood-down]`` :class:`TaskRecord` plus a
        repetition-gate no-op — all WITHOUT opening an SDK remediation session —
        and returns ``True``. Returns ``False`` (no mutation) when the head does
        not qualify, so the normal remediation route runs unchanged, or
        when the head left the live queue (#4403).

        A new commit changes the branch head SHA, dropping the stalled run below
        threshold, so the lineage auto-releases into normal remediation on the
        next pop. Zero SDK cost.
        """
        agent = self._agent
        entry = model.pending_pr_requests[0]
        verdict = agent._mechanical_standdown_verdict(entry)
        if verdict is None:
            return False
        # Human-demand veto (proposal 091): an unconsumed pending_user_requests
        # message naming this PR is an absolute veto on authoring the mechanical
        # stand-down too, independent of thread/blocker state. Return False
        # without popping — mirroring the ``verdict is None`` early return above
        # — so the entry stays queued and the normal remediation route runs.
        # This keeps the veto uniform across both stand-down faces even though
        # the ``elif`` ordering below the user-request branch in
        # :meth:`_run_task_phase` shields this path in the common case.
        if (
            agent.config.user_request_blocks_standdown_enabled
            and _pending_request_references_pr(
                model.pending_user_requests, verdict.pr,
            )
        ):
            logger.warning(
                "run_task: mechanical stand-down on PR #%s vetoed — an "
                "unconsumed queued human message names it; leaving the entry "
                "queued for the normal remediation route.",
                verdict.pr,
            )
            return False
        # Salvageability veto (proposal 094): behind
        # ``salvage_before_disengage_enabled`` (default-on), consult the pure
        # ``assess_pr_salvage`` oracle through :meth:`_assess_pr_salvage`. A
        # ``SALVAGE_AND_LAND`` verdict — a code-clearable blocker remains on
        # non-disposable work — means the loop must fix-and-land this PR, not
        # give up on it: return False without popping (mirroring the
        # ``verdict is None`` and user-request early returns above) so the entry
        # stays queued for the normal remediation route. Flag off ⇒ the probe
        # never runs.
        if (
            agent.config.salvage_before_disengage_enabled
            and agent._assess_pr_salvage(verdict.pr, entry.pr_branch)
            is Salvage.SALVAGE_AND_LAND
        ):
            logger.warning(
                "run_task: mechanical stand-down on PR #%s vetoed — the PR is "
                "salvageable (SALVAGE_AND_LAND): a code-clearable blocker "
                "remains and its work is not disposable; leaving the entry "
                "queued for the normal remediation route.",
                verdict.pr,
            )
            return False
        if agent.consciousness.dequeue_pr_request(entry) is None:
            return False
        record_stand_down(
            verdict.pr,
            verdict.head_sha,
            agent.stand_down_verdicts,
            now=verdict.recorded_at,
        )
        task_id = uuid.uuid4().hex[:12]
        finding = (
            f"[stood-down] PR #{verdict.pr} remediation skipped at zero SDK "
            f"cost on branch {entry.pr_branch} — mechanical chosen-stand-down: "
            f"a stalled, git-unlanded lineage (>= "
            f"{STALLED_REMEDIATION_LAP_CAP} same-SHA partial laps at head "
            f"{verdict.head_sha[:12]}) with no unserviced review thread "
            f"remaining; authoring the stand-down in loop code instead of "
            f"re-lapping it at the pop"
        )
        record = TaskRecord(
            task_id=task_id,
            goal=entry.directive,
            plan=[],
            # A stand-down lands nothing on main — stamp ``partial``, never
            # ``success``. The marker feeds the chosen-quiet mirror.
            outcome="partial",
            result_summary=finding,
            errors=[],
            duration_secs=0.0,
            timestamp=datetime.now(UTC).isoformat(),
            source="external",
            key_findings=finding,
            pr_head_sha=verdict.head_sha,
        )
        agent.memory.record_task(record)
        # Count the stand-down as a repetition-gate no-op so a PR that keeps
        # re-presenting the same stalled head stops being re-routed.
        agent.remediation_tracker.record_outcome(verdict.pr, noop=True)
        logger.info(
            "run_task: mechanical stand-down — authored (pr=#%s, head=%s) "
            "stand-down verdict at the pop; evicted with a partial no-op, no "
            "SDK session (task %s)",
            verdict.pr, verdict.head_sha[:12], task_id,
        )
        return True

    def _owned_act_drought_findings(
        self, plans: list[EvolutionPlan],
    ) -> list[audit_checks.AuditFinding]:
        """Rule-18 owned-act-drought findings over *plans* (shared predicate).

        The single call site for :func:`audit_checks.check_owned_act_drought`,
        used by both the audit path (``_audit_self_model``) and the slot
        preemption predicate (:meth:`_owned_act_drought_active`). Wires the same
        config-driven window/backlog/reward-landing arguments and the same
        proposal-094 salvage disqualifier closure, so the two consumers read one
        drought verdict rather than diverging copies. Zero SDK cost — bounded
        reads of the plan store, stand-down verdicts, and task records.
        """
        agent = self._agent
        def _obsolete_on_salvageable_pr(pr_url: str) -> bool:
            # Salvage disqualifier (proposal 094): the inverse-polarity twin
            # of the sibling ``_salvage_permits_retire`` — it answers "must
            # this obsolete event NOT be credited?" rather than "may I
            # retire?". Behind ``salvage_before_disengage_enabled`` (the
            # closure is passed only when the flag is on), an ``obsolete``
            # plan-status event stops counting toward the owned-act drought
            # when it backs an open own PR the loop could still clear and
            # land (``SALVAGE_AND_LAND``): obsoleting recoverable work is
            # give-up, not a rewarded owned act, so the drought must stay
            # armed. An empty/unparseable ``pr_url`` protects nothing, and a
            # PR absent from a *confirmed* open-owned snapshot is closed or
            # merged, so both keep crediting the event (return ``False``). A
            # failed listing confirms nothing, so it disqualifies (#4107).
            if not pr_url:
                return False
            try:
                pr_number = agent.evolution._pr_number_from_url(pr_url)
            except ValueError:
                return False
            try:
                states = agent._open_owned_pr_states(strict=True)
            except VCSError as exc:
                logger.warning(
                    "obsolete event on PR #%s not credited toward the drought "
                    "— open-PR listing failed: %s", pr_number, exc,
                )
                return True
            branch_by_pr = {
                state.number: head_ref
                for head_ref, state in states.items()
            }
            branch = branch_by_pr.get(pr_number)
            if branch is None:
                return False
            return (
                agent._assess_pr_salvage(pr_number, branch)
                is Salvage.SALVAGE_AND_LAND
            )

        return audit_checks.check_owned_act_drought(
            plans,
            agent.config.memory_dir,
            window=agent.config.owned_act_drought_window,
            min_backlog=agent.config.owned_act_drought_min_backlog,
            enabled=agent.config.owned_act_drought_enabled,
            reward_landing=agent.config.wip_primacy_enabled,
            salvage_disqualifies=(
                _obsolete_on_salvageable_pr
                if agent.config.salvage_before_disengage_enabled
                else None
            ),
            stand_down_backend=agent.stand_down_verdicts,
            version_archive_backend=agent.version_archive,
        )

    def _owned_act_drought_active(self) -> bool:
        """True when the rule-18 owned-act drought is currently armed.

        Reuses the shared :meth:`_owned_act_drought_findings` predicate rather
        than adding a second detector: the drought is *active* exactly when that
        predicate emits its one finding (zero owned terminal acts over the
        window while a prunable ``pending_review`` backlog sits). This is the
        gate the owned-act slot preemption keys on, so it self-extinguishes the
        instant one owned terminal act (a ``mark_plan_obsolete`` obsolete event
        or a CHOSEN stand-down verdict row) lands in the window — the same
        release the audit rule uses. Zero SDK cost.
        """
        agent = self._agent
        return bool(agent._owned_act_drought_findings(agent.memory.get_all_plans()))

    def _preempt_yielded_slot_with_owned_prune(self) -> None:
        """Fill a drought-yielded remediation slot with the owned prune.

        The owned-act preemption actuator (``owned_act_preempt_enabled``). When
        a stalled ``pending_pr_requests`` head yields its slot to the next
        non-stalled remediation entry (#2837) *while the owned-act drought is
        armed*, this runs the existing zero-SDK mechanical prune
        (:meth:`Memory.sweep_clustered_pending_plans` then
        :meth:`Memory.sweep_restatement_pending_plans`) in place of re-lapping an
        externally-addressed PR — moving net ``pending_review`` (the number the
        backlog sensor tracks) instead of counting the fire. Each obsoletion is an
        owned terminal act recorded to disk, so the drought self-extinguishes on
        the next selection. Journals the pruned ids; a no-op sweep still leaves
        the stalled head queued and the slot spent on an owned attempt, never a
        re-lap. No SDK session.
        """
        agent = self._agent
        swept = agent.memory.sweep_clustered_pending_plans(
            threshold=agent.config.loop_pending_cluster_threshold,
            min_cluster_size=agent.config.loop_pending_cluster_max,
            lever_attempts=agent.lever_attempts,
        )
        restated = agent.memory.sweep_restatement_pending_plans(
            threshold=agent.config.loop_duplicate_pending_plan_threshold,
            lever_attempts=agent.lever_attempts,
        )
        pruned = swept + restated
        if pruned:
            # Deed ledger: the owned prune fired — each swept id is a
            # ``mark_plan_obsolete`` fire that ``fire_and_capture`` recorded
            # ``True`` (its raw return and a ``verify_obsolete_persisted``
            # re-read both held); a refused fire is never in *pruned*. Record
            # one deed for the prune event (``pr_number`` None — a prune ends no single PR); the empty-sweep
            # ``else`` branch below is the no-op and records nothing.
            agent.deed_ledger.record(
                deed_ledger.DeedType.OWNED_PRUNE,
                None,
                detail=(
                    f"owned-act preemption pruned {len(pruned)} redundant "
                    f"pending plan(s): {', '.join(pruned)}"
                ),
            )
            agent.journal.record_event(
                "Owned-act preemption: a stalled remediation head yielded its "
                "slot under the owned-act drought, so I pruned "
                f"{len(pruned)} redundant pending plan(s) instead of re-lapping "
                f"an externally-addressed PR: {', '.join(pruned)}",
                entry_type="reflection",
                triggered_by="owned_act_preempt",
            )
        else:
            agent.journal.record_event(
                "Owned-act preemption fired under the drought but the "
                "mechanical prune found no redundant pending plan to retire; "
                "the yielded slot was spent on an owned attempt rather than a "
                "re-lap of an externally-addressed PR.",
                entry_type="reflection",
                triggered_by="owned_act_preempt",
            )

    def _first_non_stalled_pr_index(
        self, entries: list[PendingPRRequest], recent: list[TaskRecord] | None = None,
    ) -> int | None:
        """Index of the first non-stalled ``pending_pr_requests`` entry, else None.

        :meth:`_run_task_phase` consumes ``pending_pr_requests`` head-first, but a
        stalled head (``>= STALLED_REMEDIATION_LAP_CAP`` same-SHA ``partial`` laps
        whose origin head never advances) can never self-release through the loop:
        its head only moves by landing a remediation push, which the stall itself
        prevents. Gating on index 0 alone (#2837) therefore wedges the whole FIFO
        — every healthy entry *behind* the stuck head is starved. Scanning for the
        first non-stalled entry lets a landable PR take the slot while the stuck
        head stays queued (still never re-lapped, still auto-released on a new SHA
        or merge/close). ``None`` means every entry is stalled — the caller then
        falls through to the self-directed queue, exactly as the head-only check
        did for a lone stalled head. Zero SDK cost: one shared
        ``get_recent_tasks(30)`` tail read for the whole scan — passed into every
        :meth:`_pending_pr_head_stalled` probe so K leading stalled entries cost
        one read, not K — plus, only for a PR with prior laps, a single ``git
        rev-parse`` (*recent* supplies a tail already read).
        """
        agent = self._agent
        if recent is None:
            recent = agent.memory.get_recent_tasks(30)
        for i, entry in enumerate(entries):
            if not agent._pending_pr_head_stalled(entry, recent):
                return i
        return None

    def _refuse_owned_push_standdown(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
        ahead_count: int,
    ) -> None:
        """Coerce a false external-quiet into an owned-push partial + directive.

        The chosen-stand-down guard's action when git proves the PR branch is
        ahead of origin: the caller took this branch *instead of*
        :func:`~maestro.remediation_tracker.record_stand_down`, so **no**
        ``[stood-down]`` verdict is minted. Here the finished lap's record is
        re-stamped ``partial`` with an :attr:`Marker.OWNED_PUSH_AVAILABLE` note,
        and a bounded fire-or-close ``next_task_directive`` naming the branch is
        written into an *empty* directive slot so the next task pushes or closes
        rather than re-narrating a quiet (deferring to any directive a
        higher-precedence bridge already set). Logs loudly — fail-loud, not a
        silent pass.
        """
        agent = self._agent
        logger.warning(
            "Chosen stand-down on PR #%s refused: branch %s is %d commit(s) "
            "ahead of origin — an owned, pushable act, not an external quiet. "
            "Recording an owned-push partial and routing to fire-or-close.",
            pr_num, pr_branch, ahead_count,
        )
        note = (
            f"{Marker.OWNED_PUSH_AVAILABLE} PR #{pr_num} branch {pr_branch} is "
            f"{ahead_count} commit(s) ahead of origin — push+land or close it; "
            f"a pushable branch is never an external quiet."
        )
        if record is not None:
            record.outcome = "partial"
            record.result_summary = (
                f"{record.result_summary} {note}".strip()
                if record.result_summary
                else note
            )
            record.key_findings = (
                f"{record.key_findings} {note}".strip()
                if record.key_findings
                else note
            )
        model = agent.consciousness.load_self_model()
        if not model.next_task_directive:
            model.next_task_directive = _OWNED_PUSH_FIRE_OR_CLOSE_DIRECTIVE.format(
                pr=pr_num, branch=pr_branch,
            )
            agent.consciousness.save_self_model(model)

    def _commit_all(self, message: str) -> None:
        """Stage every working-tree change and commit it with *message*.

        The commit seam the post-session landing actuator injects: it turns the
        agent-authored (but uncommitted) session edits into a real commit on the
        current branch. Kept tiny and side-effect-only so
        :func:`maestro.session_landing.land_uncommitted_edits` stays pure.
        """
        agent = self._agent
        agent._vcs.stage_all()
        agent._vcs.commit(message)

    def _run_session_landing(
        self, pr_branch: str, pr_num: int | None,
    ) -> LandOutcome:
        """Mechanically land gate-clean session edits on the owned PR branch.

        Builds the injected seams for
        :func:`maestro.session_landing.land_uncommitted_edits` from the agent's
        real effects — working-tree status, the quality-gate runner, the
        stage+commit seam, the owned-writable-branch oracle
        (:meth:`_gate_allowed_branches`, the sole push door), the
        non-raising :meth:`_push_owned_branch`, and the rewind — and runs the
        actuator; red is attributed by :meth:`_attribute_owned_gate_red`. A
        landed push drops *pr_num*'s merge-truth memo so §4c's
        ``is_pr_settled`` reads re-fetch the pushed head. Zero SDK cost.
        """
        agent = self._agent
        push_errors: list[str] = []
        seams = LandSeams(
            has_edits=agent._vcs.has_changes,
            run_gates=lambda: classify_gate_red(
                agent.evolution.run_quality_gates(),
                agent._attribute_owned_gate_red,
            ),
            commit=agent._commit_all,
            can_push=lambda branch: branch
            in (agent._gate_allowed_branches("") or frozenset()),
            push=owned_push_seam(agent._push_owned_branch, push_errors),
            rewind=agent._vcs.discard_changes,
        )
        outcome = land_uncommitted_edits(pr_branch, seams)
        if outcome.pushed and pr_num is not None:
            # Drop the memo so §4c's chokepoint read re-fetches the pushed head.
            agent._merge_truth_memo.pop(pr_num, None)
        logger.log(
            30 if outcome.status in UNDELIVERED_STATUSES else 20,
            "Session-landing actuator on PR branch %s (pr=%s): status=%s "
            "pushed=%s committed=%s gates_ran=%s error=%s.",
            pr_branch, pr_num, outcome.status.value, outcome.pushed,
            outcome.committed, outcome.gates_ran,
            push_errors[0] if push_errors else None,
        )
        return outcome

    def _push_owned_branch(self, branch: str) -> _OwnedPushResult:
        """Mechanically push *branch* to origin — the ahead-of-origin actuator.

        The detector half of the chosen-stand-down guard already *counts* the
        owned, un-pushed commits; this is the actuator that runs the push with a
        loop command before any verdict string is composed, so a pushable owned
        branch is delivered rather than deferred to a future LLM turn that
        re-narrates "awaiting". Zero SDK cost.

        Gated through the same single owned-writable-branch door the task
        executor's action gate reads — :meth:`_gate_allowed_branches` (proposal
        093), the sole producer of push-permission truth. It is consulted with an
        **empty** ``current_branch`` (``""``) so the branch under consideration is
        *not* auto-trusted as "the run's own branch": the returned set is exactly
        the ``maestro/evolve/*`` heads confirmed by *both* the live open-PR set
        and the persistent PR ledger, giving a strict provenance check on the PR
        head rather than a "we happen to be on it" trust. A branch the oracle
        rejects is refused with **no** ``git push`` subprocess ever invoked
        (``refused=True``). On a genuine git failure the error is returned, not
        swallowed (fail-loud), so the caller keeps the loud OWNED_PUSH_AVAILABLE
        partial.

        The ``pushed`` flag binds to a same-turn physical read, not the push
        subprocess's return: after the push exits 0, ``origin/<branch>`` is
        re-fetched and read back through
        :func:`~maestro.pr_facts.verify_push_advanced_remote_head` and
        compared to the pushed SHA. A confirmed advance returns
        ``pushed=True``; an unchanged remote head (a no-op push) or a fetch/read
        failure returns ``unconfirmed=True`` — closing the false-delivery gap
        where a no-op push exiting 0 was narrated as delivered. This mirrors the
        one discipline proven to correct this behavior
        (``verify_obsolete_persisted``): a physical read bound to the claim,
        synchronous with the act.
        """
        agent = self._agent
        # ``""`` never fails closed to ``None`` (only a ``None`` branch does), so
        # the door always yields the confirmed-head frozenset; ``or frozenset()``
        # is a mypy-narrowing no-op that keeps the type ``frozenset[str]``.
        owned = agent._gate_allowed_branches("") or frozenset()
        if branch not in owned:
            logger.warning(
                "Refusing mechanical push of branch %r: not an owned-writable "
                "evolution head per the owned-branches oracle — no push run.",
                branch,
            )
            return _OwnedPushResult(pushed=False, refused=True)
        # The SHA we intend to deliver: the local branch head. ``git push`` does
        # not move the local head, so this is the SHA ``origin/<branch>`` must
        # equal for the push to have physically advanced the remote.
        expected_sha = agent._vcs.rev_parse(branch)
        try:
            agent._vcs.push(branch)
        except Exception as exc:
            # Fail-loud via a typed-failure return, not a swallowed log: the
            # caller's fallback (``_refuse_owned_push_standdown``) surfaces the
            # failure with its own loud WARNING while retaining the owned-push
            # partial. The error string travels back for that framing.
            return _OwnedPushResult(pushed=False, error=str(exc))
        # Bind 'delivered' to the remote branch head advancing, not the push
        # command's raw return: a no-op push exits 0 while ``origin/<branch>``
        # stays put. Re-read the remote head this turn and compare.
        advanced = verify_push_advanced_remote_head(
            branch,
            expected_sha or "",
            fetch=agent._fetch_remote_branch_ok,
            read_remote_head=lambda b: agent._vcs.rev_parse(f"origin/{b}"),
        )
        if not advanced:
            logger.warning(
                "Mechanical push of branch %r exited 0 but origin/%s did not "
                "advance to %s — recording an unconfirmed push, not a delivery.",
                branch, branch, expected_sha,
            )
            return _OwnedPushResult(pushed=False, unconfirmed=True)
        logger.info("Mechanically pushed owned branch %r to origin.", branch)
        return _OwnedPushResult(pushed=True)

    def _fetch_remote_branch_ok(self, branch: str) -> bool:
        """Fetch ``origin/<branch>`` and report success as a bool for the verifier.

        The injected ``fetch`` seam for
        :func:`~maestro.pr_facts.verify_push_advanced_remote_head`: a clean
        fetch returns ``True``, a :class:`~maestro.vcs.VCSError` returns
        ``False`` (a fetch failure the verifier maps to an unconfirmed advance —
        surfaced loudly by the caller, never silently swallowed).
        """
        agent = self._agent
        try:
            agent._vcs.fetch("origin", branch)
        except VCSError:
            return False
        return True

    def _record_owned_push_delivered(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
        settled: MergeTruth,
    ) -> None:
        """Stamp the honest post-push verdict after the ahead-of-origin actuator.

        Called only when :meth:`_push_owned_branch` delivered the owned commits
        to origin. The un-pushed ``[owned-push-available]`` costume is now false
        — the push happened — so re-derive merge state fetch-first: a settled
        head stamps the :attr:`Marker.LANDED` marker; an unlanded head is
        honestly :attr:`Marker.DELIVERED_AWAITING_MERGE` (delivered to origin,
        awaiting review/merge). Recorded ``partial`` (this lap merged nothing
        into ``main`` by its own hand) and — unlike the failure path — writes
        **no** fire-or-close directive, because the fire already happened.
        """
        if settled.settled:
            note = (
                f"{Marker.LANDED} PR #{pr_num} branch {pr_branch}: pushed the "
                "owned ahead-of-origin commits; git now shows the PR settled."
            )
        else:
            note = (
                f"{Marker.DELIVERED_AWAITING_MERGE} PR #{pr_num} branch "
                f"{pr_branch}: mechanically pushed the owned ahead-of-origin "
                "commits to origin — awaiting review/merge, not an owned quiet."
            )
        logger.info(
            "Ahead-of-origin actuator delivered PR #%s branch %s (settled=%s).",
            pr_num, pr_branch, settled.settled,
        )
        if record is not None:
            record.outcome = "partial"
            record.result_summary = (
                f"{record.result_summary} {note}".strip()
                if record.result_summary
                else note
            )
            record.key_findings = (
                f"{record.key_findings} {note}".strip()
                if record.key_findings
                else note
            )

    def _record_push_unconfirmed(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
    ) -> None:
        """Stamp a ``[push-unconfirmed]`` partial when the head did not advance.

        Called when :meth:`_push_owned_branch` ran ``git push`` (exit 0) but the
        same-turn ``verify_push_advanced_remote_head`` read showed
        ``origin/<pr_branch>`` did **not** advance to the pushed SHA — a no-op
        push, or a fetch/read failure. Delivery is unproven, so the lap is
        recorded ``partial`` (this hand moved no remote head) with a
        :attr:`Marker.PUSH_UNCONFIRMED` note, and a bounded fire-or-close
        ``next_task_directive`` naming the branch is written into an *empty*
        directive slot only — never overwriting a directive a higher-precedence
        bridge already set. Logs loudly — fail-loud, not a silent pass.
        """
        agent = self._agent
        logger.warning(
            "Owned push of PR #%s branch %s exited 0 but origin/%s did not "
            "advance to the pushed SHA — recording a push-unconfirmed partial "
            "and routing to fire-or-close, not a false delivery.",
            pr_num, pr_branch, pr_branch,
        )
        note = (
            f"{Marker.PUSH_UNCONFIRMED} PR #{pr_num} branch {pr_branch}: the "
            "mechanical push exited 0 but a same-turn remote-head read showed "
            f"origin/{pr_branch} did not advance — delivery unproven, push+land "
            "or close it."
        )
        if record is not None:
            record.outcome = "partial"
            record.result_summary = (
                f"{record.result_summary} {note}".strip()
                if record.result_summary
                else note
            )
            record.key_findings = (
                f"{record.key_findings} {note}".strip()
                if record.key_findings
                else note
            )
        model = agent.consciousness.load_self_model()
        if not model.next_task_directive:
            model.next_task_directive = _OWNED_PUSH_FIRE_OR_CLOSE_DIRECTIVE.format(
                pr=pr_num, branch=pr_branch,
            )
            agent.consciousness.save_self_model(model)

    def _branch_has_uncommitted_owned_edit(self, branch: str) -> bool:
        """True when *branch* is oracle-owned and its working tree is dirty.

        The detector half of the uncommitted-edit arm of the ahead-of-origin
        push actuator. The committed-but-unpushed actuator reads
        :meth:`~maestro.vcs.VCS.commits_not_on_remote`; the *uncommitted*
        working-tree edit — the fix authored in the tree yet never committed,
        the deepest read-substitutes-for-write failure — is invisible to that
        count. This reads it directly: a ``git status --porcelain`` (which
        includes untracked files) via :meth:`~maestro.vcs.VCS.has_changes`,
        gated through the same single owned-writable-branch door
        :meth:`_push_owned_branch` uses (:meth:`_gate_allowed_branches` with an
        empty ``current_branch`` so the branch is not auto-trusted as "the run's
        own branch") so a foreign dirty tree never triggers a commit+push. Zero
        SDK cost.
        """
        agent = self._agent
        owned = agent._gate_allowed_branches("") or frozenset()
        if branch not in owned:
            return False
        if not agent._is_checked_out(branch):
            # Post-``run()`` teardown has already discarded the tree and returned
            # to ``main``, so any dirty tree here is not *branch*'s edit: gating
            # or committing it would land on whatever is checked out (#4043).
            # Pre-teardown uncommitted landing is ``session_landing``'s domain.
            return False
        return agent._vcs.has_changes()

    def _is_checked_out(self, branch: str) -> bool:
        """True iff *branch* is the checked-out branch (``False`` on a VCS error).

        The current-branch precondition both record-time landing arms share, so
        neither can measure, commit to, or rewind a branch other than the PR
        branch it names (#4043). Logs the branch actually checked out at INFO
        when it refuses. Zero SDK cost.
        """
        agent = self._agent
        try:
            current = agent._vcs.get_current_branch()
        except VCSError:
            logger.info(
                "Record-time landing arm: current branch unreadable — refusing "
                "to act on %s", branch, exc_info=True,
            )
            return False
        if current != branch:
            logger.info(
                "Record-time landing arm: %s is checked out, not %s — refusing "
                "to gate, commit, or rewind it", current, branch,
            )
            return False
        return True

    def _attribute_owned_gate_red(
        self, review: QualityGateResult,
    ) -> Attribution | None:
        """Classify a red owned-branch gate as the diff's or the base's (proposal 124).

        The one attribution reader the two record-time landing arms share. It
        answers the single question those destructive arms cannot ask today —
        *did this branch's diff cause the red, or did it inherit it from its
        base?* — so an ``INHERITED`` red holds the tree instead of destroying
        work the branch never broke.

        Returns ``None`` when ``[loop] gate_observation_enabled`` is off or the
        *review* carries no :class:`~maestro.gate_observation.GateRunObservation`
        (both the byte-identical pre-124 path), so the arms keep today's rewind.
        Otherwise it reads the current head, derives the branch's base as
        ``merge_base(HEAD, "origin/main")``, reads the base→head diff, and hands
        :func:`~maestro.gate_observation.attribute` the observation, that diff,
        and the base-commit baseline served from
        :meth:`~maestro.evolution.EvolutionEngine._observe_baseline`'s cache. A
        missing head or base short-circuits to ``UNATTRIBUTABLE`` — today's
        behaviour — since without a base there is no baseline to charge the red
        to.

        It never calls :meth:`~maestro.evolution.EvolutionEngine.run_quality_gates`
        itself: the arms already hold their review, and ``_observe_baseline`` is
        the (already-allowlisted) single gate call site it reaches. On a
        Tend-maintained evolution branch the merge-base normally equals the
        ``origin/main`` head ``tend_evolution_prs`` already banked, so the
        baseline lookup is a cache hit, not a fresh sweep. Zero SDK cost.
        """
        agent = self._agent
        if not agent.config.gate_observation_enabled:
            return None
        observation = review.observation
        if observation is None:
            return None
        head = agent._vcs.get_head_hash()
        if not head:
            return "UNATTRIBUTABLE"
        base = agent._vcs.merge_base(head, "origin/main")
        if not base:
            return "UNATTRIBUTABLE"
        paths = agent._vcs.changed_paths(base, head)
        baseline = agent.evolution._observe_baseline(base)
        return attribute(observation, paths, baseline)

    def _land_uncommitted_owned_edit(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
    ) -> None:
        """Commit+push — or rewind — a gate-clean uncommitted owned edit at record time.

        The uncommitted-edit arm of the ahead-of-origin push actuator, and the
        will-free countermeasure to *read-substitutes-for-write*: the deed (or
        its refusal) happens at record time whether or not the session's resolve
        survived. Runs the quality gates on the dirty tree; on **GREEN** commits
        with a deterministic message and delivers via the same oracle-gated
        :meth:`_push_owned_branch` the committed-work actuator uses, re-deriving
        the marker fetch-first through :meth:`is_pr_settled` (LANDED vs
        DELIVERED_AWAITING_MERGE, or an ``OWNED_PUSH_AVAILABLE`` /
        ``[push-unconfirmed]`` partial on a refused/failed/no-op push). On
        **RED** a ``git reset --hard`` rewinds the unverified edit and the lap is
        recorded ``partial`` with a :attr:`Marker.UNCOMMITTED_RED` note —
        :meth:`_push_owned_branch` is never invoked, so a red head is never
        pushed. Zero SDK cost.

        The RED rewind uses :meth:`~maestro.vcs.VCS.discard_changes` (reset +
        ``checkout -- .`` + ``git clean -fd``), not a bare ``reset --hard``: the
        detector fires on :meth:`~maestro.vcs.VCS.has_changes`, which counts
        *untracked* files, so the likeliest dirty tree is a newly authored,
        never-committed test/module file. ``reset --hard HEAD`` leaves such a
        file in place; ``discard_changes`` sweeps it, so the tree is actually
        cleaned and the next lap re-reads a genuinely clean working tree.
        """
        agent = self._agent
        review = agent.evolution.run_quality_gates()
        if not review.passed:
            attribution = agent._attribute_owned_gate_red(review)
            if attribution == "INHERITED":
                # INHERITED (proposal 124): the identical failure identity is
                # already present in a baseline of the branch's own base, so the
                # red belongs to origin/main, not this edit's diff. Hold the tree
                # — no ``discard_changes``, nothing committed or pushed, the head
                # never moves — and record a partial naming the base failures, so
                # the next lap repairs the base rather than discarding work the
                # branch never broke.
                observation = review.observation
                identities = (
                    render_identities(
                        observation,
                        limit=_GATE_RECORD_IDENTITY_LIMIT,
                        max_chars=_GATE_RECORD_IDENTITY_CHARS,
                    )
                    if observation is not None
                    else ""
                )
                logger.warning(
                    "Uncommitted-edit arm on PR #%s branch %s: quality gates RED "
                    "but INHERITED from origin/main (%s) — held the working tree "
                    "(no discard_changes), committed and pushed nothing.",
                    pr_num, pr_branch, identities or "base failures",
                )
                note = (
                    f"{Marker.INHERITED_RED} PR #{pr_num} branch {pr_branch}: an "
                    "uncommitted working-tree edit's quality-gate red is "
                    f"inherited from origin/main ({identities or 'base failures'})"
                    ", not this diff — held the tree (nothing discarded, "
                    "committed, or pushed, the head never moved); the base needs "
                    "the fix."
                )
            else:
                # RED and the branch's own (ATTRIBUTED / UNATTRIBUTABLE, or the
                # flag-off ``None``): never ship an unverified edit. Rewind the
                # whole working tree — tracked *and* untracked — and record a
                # partial; the head SHA never moves and no push is run.
                # ``discard_changes`` (not ``reset --hard``) is required because
                # the edit is most likely an untracked new file, which ``reset
                # --hard HEAD`` would leave behind.
                agent._vcs.discard_changes()
                logger.warning(
                    "Uncommitted-edit arm on PR #%s branch %s: quality gates RED "
                    "— discard_changes (reset + clean) rewound the working tree, "
                    "recording an unverified-edit partial and pushing nothing.",
                    pr_num, pr_branch,
                )
                note = (
                    f"{Marker.UNCOMMITTED_RED} PR #{pr_num} branch {pr_branch}: an "
                    "uncommitted working-tree edit failed the quality gates — "
                    "discard_changes rewound it (removing untracked files too); "
                    "nothing committed or pushed, the head never moved."
                )
            if record is not None:
                record.outcome = "partial"
                record.result_summary = (
                    f"{record.result_summary} {note}".strip()
                    if record.result_summary
                    else note
                )
                record.key_findings = (
                    f"{record.key_findings} {note}".strip()
                    if record.key_findings
                    else note
                )
            return
        # GREEN: the edit is gate-clean, so commit it with a deterministic
        # message and deliver via the oracle-gated push actuator. The marker is
        # re-derived from the observed post-push head, never narrated.
        agent._commit_all(_UNCOMMITTED_LAND_COMMIT_MESSAGE)
        push_result = agent._push_owned_branch(pr_branch)
        if push_result.pushed:
            agent._merge_truth_memo.pop(pr_num, None)
            settled = agent.is_pr_settled(pr_num)
            agent._record_owned_push_delivered(
                pr_num, pr_branch, record, settled,
            )
        elif push_result.unconfirmed:
            agent._record_push_unconfirmed(pr_num, pr_branch, record)
        else:
            agent._refuse_owned_push_standdown(
                pr_num, pr_branch, record,
                agent._branch_ahead_of_origin_count(pr_branch),
            )

    def _branch_is_owned_writable(self, branch: str) -> bool:
        """True when *branch* is an oracle-confirmed owned-writable evolution head.

        Reads the same single owned-writable-branch door
        :meth:`_push_owned_branch` and :meth:`_branch_has_uncommitted_owned_edit`
        use — :meth:`_gate_allowed_branches` with an **empty** ``current_branch``
        so the branch is not auto-trusted as "the run's own branch": the returned
        set is exactly the ``maestro/evolve/*`` heads confirmed by both the live
        open-PR set and the PR ledger. Zero SDK cost; no working-tree read.
        """
        agent = self._agent
        owned = agent._gate_allowed_branches("") or frozenset()
        return branch in owned

    def _push_committed_unpushed_owned_head(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
    ) -> bool:
        """Fire the owed push — or red-gate rewind — on a committed-unpushed owned head.

        The committed-work sibling of :meth:`_land_uncommitted_owned_edit`, and the
        will-free countermeasure to *delivered-on-branch-is-not-a-terminal*: when
        the fix is **already committed** on the owned PR branch and the head is
        ahead of ``origin/<pr_branch>`` under an :data:`MERGE_BLOCK_AWAITING_PUSH`
        blocker, the single owed deed is the push — so it happens here at record
        time, before any LLM lap can narrate "delivered, awaiting review/merge" in
        its place.

        The cheap preconditions gate the expensive quality-gate run: the branch
        must be oracle-owned (:meth:`_branch_is_owned_writable`), its working tree
        clean (a dirty tree is :meth:`_land_uncommitted_owned_edit`'s domain),
        committed ahead of origin (``ahead_count > 0``), under the AWAITING_PUSH
        blocker. When any is absent this is a strict no-op returning ``False``,
        leaving every downstream verdict path byte-identical.

        When the shape is present the quality gates run as the red-gate floor
        (:func:`~maestro.remediation_tracker.classify_committed_unpushed_push`
        folds ``gates_pass`` into the push decision), measured on *pr_branch*
        checked out for the run (the post-``run()`` workspace sits on ``main``,
        which is restored afterwards). On **RED** the local head is
        rewound to ``origin/<pr_branch>`` (``reset_branch_to``, by name) so the broken commit is
        discarded and the ahead-count self-extinguishes, recording a
        :attr:`Marker.COMMITTED_RED` partial — :meth:`_push_owned_branch` is never
        invoked, so a red head never reaches origin. On **GREEN** delivery goes
        through the same oracle-gated :meth:`_push_owned_branch` (whose ``pushed``
        binds to a same-turn ``verify_push_advanced_remote_head`` read, never the
        subprocess return), re-deriving the marker fetch-first through
        :meth:`is_pr_settled` (LANDED vs DELIVERED_AWAITING_MERGE, or an
        ``OWNED_PUSH_AVAILABLE`` / ``[push-unconfirmed]`` partial on a
        refused/failed/no-op push). Returns ``True`` when the arm handled the
        branch. Zero SDK cost.
        """
        agent = self._agent
        blocker = agent._last_remediation_merge_block or ""
        # Cheap preconditions before the expensive gate run, cheapest first: the
        # AWAITING_PUSH blocker (a bare string compare) is the rarest and most
        # selective, so it short-circuits before the oracle read, the
        # working-tree read, and the fetch-first ahead-of-origin count. An
        # unowned branch, a dirty tree (the uncommitted arm's domain), or a
        # caught-up remote each fall through untouched.
        if blocker.strip().upper() != MERGE_BLOCK_AWAITING_PUSH:
            return False
        if not agent._branch_is_owned_writable(pr_branch):
            return False
        if agent._vcs.has_changes():
            return False
        ahead_count = agent._branch_ahead_of_origin_count(pr_branch)
        if ahead_count <= 0:
            return False
        # Post-``run()`` teardown left the workspace on ``main`` (#4043): check
        # out the PR branch so the gates measure — and the attribution and push
        # read — the head this arm is about to push, then restore the branch
        # that was checked out. A failed checkout makes the arm a no-op rather
        # than falling through to gating ``main``.
        try:
            restore_to = agent._vcs.get_current_branch()
            if restore_to != pr_branch:
                agent._vcs.checkout(pr_branch)
        except VCSError:
            logger.info(
                "Committed-unpushed arm on PR #%s: could not check out %s — "
                "skipping (no gate run, no push, no rewind)",
                pr_num, pr_branch, exc_info=True,
            )
            return False
        try:
            if not agent._is_checked_out(pr_branch):
                return False
            review = agent.evolution.run_quality_gates()
            return agent._settle_committed_unpushed_head(
                pr_num, pr_branch, record, blocker, ahead_count, review,
            )
        finally:
            if restore_to != pr_branch:
                try:
                    agent._vcs.checkout(restore_to)
                except VCSError:
                    logger.error(
                        "Committed-unpushed arm on PR #%s: checkout(%r) failed "
                        "restoring the workspace after the gate run",
                        pr_num, restore_to, exc_info=True,
                    )

    def _settle_committed_unpushed_head(
        self,
        pr_num: int,
        pr_branch: str,
        record: TaskRecord | None,
        blocker: str,
        ahead_count: int,
        review: QualityGateResult,
    ) -> bool:
        """Push the checked-out *pr_branch* head on a green *review*, else rewind it.

        The body of :meth:`_push_committed_unpushed_owned_head` once *pr_branch*
        is checked out and gated, so the red attribution and the push read the
        head that was measured. The red rewind moves *pr_branch* by name
        (:meth:`~maestro.vcs.VCS.reset_branch_to`), never whichever branch is
        checked out. Always returns ``True``.
        """
        agent = self._agent
        gates_pass = review.passed
        if not classify_committed_unpushed_push(
            blocker=blocker,
            ahead_count=ahead_count,
            has_uncommitted_edit=False,
            gates_pass=gates_pass,
        ):
            # RED gate (the only reason ``classify_committed_unpushed_push``
            # returns ``False`` once the four preconditions above hold): never
            # push a red head.
            attribution = agent._attribute_owned_gate_red(review)
            if attribution == "INHERITED":
                # INHERITED (proposal 124): the red belongs to origin/main, not
                # this committed head's diff. Hold the head — no
                # ``reset_hard``, no push seam, the head never moves — and record
                # a partial naming the base failures so the base repair, not a
                # rewind of the branch's own work, clears the shared red. Still
                # return ``True``: the arm handled the branch this lap.
                observation = review.observation
                identities = (
                    render_identities(
                        observation,
                        limit=_GATE_RECORD_IDENTITY_LIMIT,
                        max_chars=_GATE_RECORD_IDENTITY_CHARS,
                    )
                    if observation is not None
                    else ""
                )
                logger.warning(
                    "Committed-unpushed arm on PR #%s branch %s: quality gates "
                    "RED but INHERITED from origin/main (%s) — held the local "
                    "head (no reset --hard), pushing nothing.",
                    pr_num, pr_branch, identities or "base failures",
                )
                note = (
                    f"{Marker.INHERITED_RED} PR #{pr_num} branch {pr_branch}: a "
                    "committed-but-unpushed head's quality-gate red is inherited "
                    f"from origin/main ({identities or 'base failures'}), not "
                    "this diff — held the head (no reset --hard, nothing pushed, "
                    "the head never moved); the base needs the fix."
                )
            else:
                # RED and the branch's own (ATTRIBUTED / UNATTRIBUTABLE, or the
                # flag-off ``None``): rewind the local head to the remote so the
                # broken committed work is discarded and the ahead count
                # self-extinguishes; record a partial and push nothing.
                agent._vcs.reset_branch_to(pr_branch, f"origin/{pr_branch}")
                logger.warning(
                    "Committed-unpushed arm on PR #%s branch %s: quality gates "
                    "RED — reset --hard origin/%s rewound the local head, "
                    "recording a committed-red partial and pushing nothing.",
                    pr_num, pr_branch, pr_branch,
                )
                note = (
                    f"{Marker.COMMITTED_RED} PR #{pr_num} branch {pr_branch}: a "
                    "committed-but-unpushed head failed the quality gates — reset "
                    f"--hard origin/{pr_branch} rewound it; nothing pushed, a red "
                    "head never reached origin."
                )
            if record is not None:
                record.outcome = "partial"
                record.result_summary = (
                    f"{record.result_summary} {note}".strip()
                    if record.result_summary
                    else note
                )
                record.key_findings = (
                    f"{record.key_findings} {note}".strip()
                    if record.key_findings
                    else note
                )
            return True
        # GREEN: the committed head is gate-clean, so deliver via the
        # oracle-gated push actuator. The marker is re-derived from the observed
        # post-push head, never narrated.
        push_result = agent._push_owned_branch(pr_branch)
        if push_result.pushed:
            agent._merge_truth_memo.pop(pr_num, None)
            settled = agent.is_pr_settled(pr_num)
            agent._record_owned_push_delivered(
                pr_num, pr_branch, record, settled,
            )
        elif push_result.unconfirmed:
            agent._record_push_unconfirmed(pr_num, pr_branch, record)
        else:
            agent._refuse_owned_push_standdown(
                pr_num, pr_branch, record, ahead_count,
            )
        return True

    async def _record_remediation_outcome(
        self,
        *,
        goal: str,
        pr_branch: str | None,
        record: TaskRecord | None,
        head_before: str | None,
    ) -> None:
        """Record a PR-remediation outcome for the repetition gate and escalate.

        A remediation that produces no new commit on the PR branch is a no-op
        regardless of how the LLM framed its summary (#2440) — detect that
        deterministically by comparing the branch HEAD before/after ``run()``.
        When a commit *was* made, defer to ``classify_noop`` of the free-text
        summary/findings as before. A remediation that committed locally but the
        host stamp gate (step 4c) found never landed is a host no-op too (#2457):
        ``run()`` recorded the verdict on ``_last_remediation_merge_block``, so
        fold it in here without re-fetching host state. When the gate then
        refuses to re-route a still-unmerged PR, surface the deterministic merge
        blocker (mirrors :meth:`_record_preflight_skip_outcome`).
        """
        agent = self._agent
        if pr_branch is None or not goal.startswith(_REMEDIATION_DEDUP_PREFIX):
            return
        match = re.match(r"Remediate PR #(\d+):", goal)
        if not match:
            return
        pr_num = int(match.group(1))
        head_after = agent._vcs.rev_parse(pr_branch)
        made_commit = (
            head_before is not None
            and head_after is not None
            and head_before != head_after
        )
        summary = getattr(record, "result_summary", None) or ""
        findings = getattr(record, "key_findings", None) or ""
        # The post-task downgrade/escalation lifecycle, classified once into a
        # typed state instead of re-derived inline at the consumer (proposal
        # 070). STOOD_DOWN -> persist the keyed stand-down verdict; BRANCH_RESOLVED
        # -> the host stamp gate downgraded the lap, so it counts as a host
        # no-op. The orthogonal no-op axes (made_commit / text_noop / items_noop)
        # stay independent. Recorded on the agent so tests and future telemetry
        # have a typed handle without re-parsing the summary.
        state = classify_remediation_outcome_state(
            summary if isinstance(summary, str) else "",
            findings if isinstance(findings, str) else "",
            merge_block_reason=agent._last_remediation_merge_block,
        )
        agent._last_remediation_state = state
        # Committed-but-unpushed push actuator (flag-gated, always-on — not
        # scoped to STOOD_DOWN): a gate-clean *committed* owned head ahead of
        # ``origin/<pr_branch>`` under an AWAITING_PUSH blocker owes exactly one
        # deed — ``git push`` — regardless of how this lap's LLM framed its
        # summary. Fire it here (green → push, red → reset-to-origin rewind)
        # BEFORE any stand-down verdict or narrated "delivered, awaiting merge"
        # can substitute description for the deed. When it handles the branch,
        # the stand-down minting below is skipped: the push (or its red rewind)
        # is the honest record, not an external-quiet verdict.
        committed_push_handled = (
            agent.config.owned_committed_push_actuator_enabled
            and agent._push_committed_unpushed_owned_head(pr_num, pr_branch, record)
        )
        # Write side of the keyed stand-down gate: when a remediation task
        # closes with a deliberate [stood-down] outcome, persist a durable
        # (pr, head_sha) verdict so the dequeue-face gate honors it on the next
        # iteration instead of re-minting the cured lap. Keyed to the same
        # origin head SHA the gate reads, so the consult matches exactly; a new
        # commit changes the head and auto-releases the brake. An unresolvable
        # origin head records nothing (no key to match on).
        if state is PrRemediationState.STOOD_DOWN and not committed_push_handled:
            # Two orthogonal vetoes gate the durable verdict; either one
            # refuses it (proposals 091, 094). (1) An unconsumed
            # ``pending_user_requests`` message that names this PR is the
            # strongest "this is not settled" signal there is — behind
            # ``user_request_blocks_standdown_enabled`` (default-on) it is an
            # absolute veto, independent of salvageability (not subsumed by
            # the oracle: a human demand must block even a ready-and-waiting
            # PR), so it can never be answered with silence. (2) Behind
            # ``salvage_before_disengage_enabled`` (default-on), the pure
            # ``assess_pr_salvage`` oracle reads the live readiness fetched
            # here: while a code-clearable blocker remains on non-disposable
            # work it is ``SALVAGE_AND_LAND`` and the loop must fix-and-land
            # rather than give up. Unresolved AI review threads (#2907) make
            # the PR not ready-and-waiting, so the oracle refuses exactly
            # where the retired threads-only veto did — while a genuinely
            # disposable PR with an open thread may now stand down. When
            # either holds, refuse the verdict, log loudly (fail-loud, not a
            # silent pass) naming which condition vetoed, and leave the PR
            # eligible for the next tend lap. When neither holds — or every
            # gating flag is off — persist exactly as before.
            readiness = (
                agent.observe_pr_readiness(pr_num, pr_branch)
                if agent.config.salvage_before_disengage_enabled
                else None
            )
            user_request_veto = (
                agent.config.user_request_blocks_standdown_enabled
                and _pending_request_references_pr(
                    agent.consciousness.load_self_model().pending_user_requests,
                    pr_num,
                )
            )
            salvage_veto = False
            salvage_blocker = ""
            if readiness is not None:
                salvage_veto = (
                    agent._assess_pr_salvage(
                        pr_num, pr_branch, readiness=readiness,
                        unpersisted=record,
                    )
                    is Salvage.SALVAGE_AND_LAND
                )
                owned_blocker = classify_owned_blocker(
                    readiness.merge_block_reason(),
                    readiness.unresolved_threads,
                )
                # Only append the thread-count clause when a thread is actually
                # open: a conflict/behind blocker with zero threads would
                # otherwise log the misleading "CONFLICTING: 0 unresolved AI
                # review thread(s)" — the blocker name alone is the honest
                # report of a non-thread merge gate.
                salvage_blocker = (
                    f"{owned_blocker}: {readiness.unresolved_threads} "
                    f"unresolved AI review thread(s)"
                    if readiness.unresolved_threads > 0
                    else owned_blocker
                )
            if user_request_veto or salvage_veto:
                reasons: list[str] = []
                if user_request_veto:
                    reasons.append(
                        "an unconsumed queued human message names the PR"
                    )
                if salvage_veto:
                    reasons.append(
                        "the PR is salvageable (SALVAGE_AND_LAND) — a "
                        f"code-clearable blocker remains ({salvage_blocker}) "
                        "and its work is not disposable"
                    )
                logger.warning(
                    "Chosen stand-down on PR #%s voided by %s — not persisting "
                    "the stand-down verdict; the PR stays eligible for the next "
                    "tend lap.",
                    pr_num,
                    " and ".join(reasons),
                )
            else:
                # Ahead-of-origin guard: a chosen stand-down claims an
                # *external* blocker, but if git proves the PR branch is ahead of
                # origin — commits on no ``origin`` ref — the branch is an owned,
                # pushable act, so "stand down and wait for someone else" is a
                # false quiet (verdict-restaged, not honored). Refuse to MINT the
                # verdict; record an ``[owned-push-available]`` partial and route
                # to fire-or-close instead. Only an *externally*-classified
                # blocker is coerced — an owned CONFLICTING/DIRTY branch is
                # already the salvage veto's domain. The reachability fact is
                # stamped onto every verdict we *do* write, so an honest external
                # quiet carries the evidence that it was genuinely nothing to
                # push.
                blocker_external = (
                    classify_blocker_ownership(
                        agent._last_remediation_merge_block or "",
                    )
                    == BLOCKER_OWNERSHIP_EXTERNAL
                )
                if blocker_external and agent._branch_has_uncommitted_owned_edit(
                    pr_branch,
                ):
                    # Uncommitted-edit arm of the ahead-of-origin actuator: the
                    # fix authored in the working tree but never committed is
                    # invisible to the ahead-of-origin commit count, yet it is
                    # the actual thing read-substitutes-for-write abandons. Land
                    # it here — gate-clean commit+push, red-gate reset --hard
                    # rewind — BEFORE composing any external-quiet verdict, the
                    # deed happening at record time whether or not the session's
                    # resolve survived.
                    agent._land_uncommitted_owned_edit(pr_num, pr_branch, record)
                else:
                    ahead_count = agent._branch_ahead_of_origin_count(pr_branch)
                    reachability = classify_branch_reachability(ahead_count)
                    if (
                        reachability is BranchReachability.OWNED_AHEAD
                        and blocker_external
                    ):
                        # Actuator, not just a re-label (ahead-of-origin push
                        # actuator): the branch carries owned, un-pushed commits,
                        # so push it with a loop command BEFORE composing any
                        # verdict — removing the anesthetic "awaiting" word's
                        # power at the exact chokepoint. Only on a genuine push
                        # failure (or an oracle refusal) do we fall back to the
                        # loud OWNED_PUSH_AVAILABLE partial + fire-or-close
                        # directive.
                        push_result = agent._push_owned_branch(pr_branch)
                        if push_result.pushed:
                            # ``pushed`` is True only when a same-turn remote-head
                            # read confirmed origin/<branch> advanced to the
                            # pushed SHA — never on the subprocess return alone.
                            # Re-derive merge state fetch-first: drop the
                            # per-iteration memo so the next chokepoint read
                            # re-fetches against the just-pushed origin head. A
                            # landed head coerces to a settled note; an unlanded
                            # one is now honestly delivered (awaiting
                            # review/merge), never OWNED_PUSH_AVAILABLE.
                            agent._merge_truth_memo.pop(pr_num, None)
                            settled = agent.is_pr_settled(pr_num)
                            agent._record_owned_push_delivered(
                                pr_num, pr_branch, record, settled,
                            )
                        elif push_result.unconfirmed:
                            # The push exited 0 but origin/<branch> did not
                            # advance (no-op push, or a fetch/read failure):
                            # delivery is unproven, so stamp a [push-unconfirmed]
                            # partial rather than a false delivery or a false
                            # external quiet.
                            agent._record_push_unconfirmed(
                                pr_num, pr_branch, record,
                            )
                        else:
                            agent._refuse_owned_push_standdown(
                                pr_num, pr_branch, record, ahead_count,
                            )
                    else:
                        stand_down_head = (
                            agent._vcs.rev_parse(f"origin/{pr_branch}") or ""
                        )
                        if stand_down_head:
                            record_stand_down(
                                pr_num, stand_down_head, agent.stand_down_verdicts,
                                branch_ahead_of_origin=(
                                    reachability
                                    is BranchReachability.OWNED_AHEAD
                                ),
                            )
        text_noop = bool(
            (isinstance(summary, str) and classify_noop(summary))
            or (isinstance(findings, str) and classify_noop(findings))
        )
        # Host verdict from run()'s stamp gate: a local-only commit that never
        # reached a mergeable/merged host state is a no-op from the host's
        # perspective, so a PR stuck despite real local work still trips the
        # gate and escalates. Read off the classified state (BRANCH_RESOLVED) so
        # the host-no-op decision has one named classification site.
        host_noop = state is PrRemediationState.BRANCH_RESOLVED
        # Outstanding-item check for open PRs (#2663). Any remediation that
        # completes while an eligible review item remains unaddressed — an
        # inline 🐛 thread without a terminal reply, or a body-overflow finding
        # without an acknowledgement — is a failure: the item stays eligible and
        # tend re-routes forever. Skipped for merged/closed PRs where a
        # merge-truth stand-down is legitimate. When ``run()``'s completeness
        # gate (step 4d) already counted the remaining eligible items this lap,
        # reuse its combined count rather than re-querying the host for the
        # same answer.
        #
        # Comment/review remediation is LLM-only and never moves the branch
        # HEAD, so the ``made_commit`` axis alone marks every comment lap a
        # no-op — and the only reset path (``RemediationTracker.reset`` on a
        # branch-state change) never fires for a comment-only PR, so the gate
        # latches forever (#2744). Measure comment progress by *terminal-reply
        # delivery* instead: compare this lap's remaining-item count against the
        # PR's previously recorded count and treat a **strict decrease** as
        # progress. A lap that did not reduce the count keeps ``items_noop``
        # True and latches as before, preserving the #2440 / #2663 guard against
        # re-routing genuine no-op comment work forever.
        items_noop = False
        items_progress = False
        current_items = agent._last_remediation_items_remaining
        prev_items = agent.remediation_tracker.last_items_remaining(pr_num)
        if current_items is not None:
            if prev_items is not None and current_items < prev_items:
                items_progress = True
            else:
                items_noop = current_items > 0
        elif not agent.is_pr_settled(pr_num).settled:
            # Open PR (not settled): re-read outstanding items for the no-op
            # check. The open-vs-settled fork reads through the shared
            # ``is_pr_settled`` chokepoint (proposal 087), reusing the
            # per-iteration MergeTruth rather than re-deriving merge state.
            # Imported here: ``remediation_stamps`` imports this module.
            from maestro.loop.remediation_stamps import items_outstanding_or_unverified

            items_noop = items_outstanding_or_unverified(
                agent.evolution.code_host, pr_num,
            )
        # A delivered-progress comment lap is still a branch no-op (no commit),
        # so resetting the gate — not flipping ``noop`` — is what re-opens the
        # PR for its still-open threads. Clear the latch and grant fresh
        # PR-ledger attempts (mirroring the branch-state-change reset in
        # ``route_feedback``) *before* recording this lap, so ``should_reroute``
        # then sees a single fresh outcome and the PR is re-routable next cycle.
        if items_progress:
            agent.remediation_tracker.reset(pr_num)
            agent.pr_ledger.grant_fresh_attempts(
                pr_num,
                reason="comment remediation delivered a terminal reply",
            )
        noop = (not made_commit) or text_noop or host_noop or items_noop
        # Carry this lap's remaining-item count forward as the next lap's
        # baseline so the strict-decrease comparison above has something to
        # measure against; ``None`` (no count this lap) leaves any prior
        # baseline untouched.
        agent.remediation_tracker.record_outcome(
            pr_num, noop=noop, items_remaining=current_items,
        )
        # PR ledger: charge one attempt regardless of noop/progress; builds
        # the objective attempt count at this head SHA so stuck detection
        # does not depend on text-pattern heuristics.
        if noop:
            if items_noop:
                stuck_reason = "unresolved review items"
            elif host_noop and agent._last_remediation_merge_block:
                stuck_reason = f"merge-blocked: {agent._last_remediation_merge_block}"
                # Provenance from the pre-stamp merge-truth gate (proposal 087
                # step 5): record HOW and WHEN merge state was read so the
                # tracker's stuck record carries the same provenance as the
                # refused stamp, not just the block reason. The gate stamps
                # ``source`` and ``observed_at`` together or not at all.
                source = agent._last_remediation_merge_source
                observed_at = agent._last_remediation_merge_observed_at
                if source is not None and observed_at is not None:
                    stuck_reason += (
                        f" (source={source}, observed_at={observed_at:.0f})"
                    )
            elif not made_commit:
                stuck_reason = "no commit produced"
            else:
                stuck_reason = "text-classified noop"
        else:
            stuck_reason = "in-progress"
        agent.pr_ledger.record_attempt(
            pr_num,
            outcome="noop" if noop else "progress",
            stuck_reason=stuck_reason,
            branch=pr_branch,
        )
        if agent.remediation_tracker.should_reroute(pr_num):
            return
        # Hand the latch up with the real blocker: an items-driven latch
        # (current_items > 0) names the unresolved-review-items cause rather
        # than the transient git merge-block snapshot the probe would compute
        # (#2744); a None / zero count keeps the git-merge-block CTA.
        await agent._escalate_stuck_pr(
            pr_num, pr_branch, items_remaining=current_items,
        )

    def _record_remediation_fetch_truth(
        self, task_id: str, pr_num: int, pr_branch: str,
    ) -> bool:
        """Fetch origin and record this task's git-ancestry fetch-truth marker.

        The fetch-before-word action-zero for a remediation verdict: ``git
        fetch origin`` (loud — ``check=True``, so a fetch failure raises and no
        status word may precede it), then a ``git merge-base --is-ancestor``
        read against ``origin/<pr_branch>`` via
        :meth:`_branch_merged_into_main`. The ancestry verdict is persisted as a
        per-task fetch-truth marker (:func:`record_fetch_truth`) BEFORE any
        status string is written — carrying the observed origin head SHA
        (empty when the branch cannot be resolved) so
        :func:`fetch_truth_relap` can later prove a *later* lap re-read the same
        head — so :func:`gate_remediation_verdict` can prove git spoke first.
        Returns the ancestry verdict. Zero SDK cost; the only I/O is the bounded
        git fetch plus the ancestry subprocess and one marker append. Isolated
        as a method so tests mock it without a real subprocess.
        """
        agent = self._agent
        subprocess.run(
            ["git", "fetch", "origin"],
            capture_output=True,
            text=True,
            check=True,
            timeout=agent.config.git_timeout,
            cwd=agent.config.workspace,
        )
        is_ancestor = agent._branch_merged_into_main(f"origin/{pr_branch}")
        head_sha = agent._vcs.rev_parse(f"origin/{pr_branch}") or ""
        agent.fetch_truth.record(
            task_id, pr_num, is_ancestor, head_sha=head_sha,
        )
        return is_ancestor

    def _record_remediation_obligation_fork(
        self, task_id: str, pr_num: int, pr_branch: str,
    ) -> None:
        """Record the HEAD-vs-``origin/<pr_branch>`` obligation-fork marker.

        The *indicting* action-zero read that forks "I owe a push" (local
        ``HEAD`` ahead of ``origin/<pr_branch>``) from "Thomas owes a merge"
        (``HEAD`` already on ``origin/<pr_branch>``): ``git rev-parse HEAD`` vs
        ``git rev-parse origin/<pr_branch>``, persisted via
        :func:`record_obligation_fork_truth` so :func:`gate_remediation_verdict`
        can prove the compare ran before an owned AWAITING_PUSH verdict wears
        external-deferral phrasing. Runs beside :meth:`_record_remediation_fetch_truth`
        at the §4c action-zero, so the fetch it performed already freshened
        ``origin/<pr_branch>``. Also records whether ``origin/<pr_branch>`` is an
        ancestor of ``HEAD`` (``git merge-base --is-ancestor``), so the gate only
        emits the "push is mine to fire" [push-owed] language for a genuinely
        *ahead* head — a diverged/behind local branch (whose origin counterpart
        Tend advanced) owes a reconcile, not a fast-forward push. An unresolvable
        ref records an empty SHA and a ``False`` ancestry bit, so the marker
        reports no push owed. Zero SDK cost; the only I/O is two ``rev-parse``
        subprocesses plus one ancestry probe and one marker append. Isolated as a
        method so tests mock it without a real subprocess. Only invoked when
        ``obligation_fork_floor_enabled`` is set.
        """
        agent = self._agent
        head_sha = agent._vcs.rev_parse("HEAD") or ""
        remote_sha = agent._vcs.rev_parse(f"origin/{pr_branch}") or ""
        remote_is_ancestor = agent._vcs.is_ancestor(f"origin/{pr_branch}", "HEAD")
        agent.obligation_fork.record(
            task_id, pr_num, head_sha, remote_sha, pr_branch,
            remote_is_ancestor=remote_is_ancestor,
        )

    def _refine_ci_review_blocker(
        self, pr_num: int, pr_branch: str | None, block_reason: str,
    ) -> str:
        """Split a flat CI/review merge blocker into the honest three-way label.

        The rollup-derived ``_probe_pr_merge_block`` verdict collapses a
        running-CI wait, a review wait, and a CI failure under one flat code
        (chiefly ``CI_PENDING``). When *block_reason* names that CI/review family
        (:data:`_CI_REVIEW_MERGE_BLOCKERS`), re-read the already-observed
        readiness fact (the memoized :meth:`observe_pr_readiness` chokepoint) and
        the per-check :func:`classify_ci_conclusions` verdict over
        :meth:`_observe_pr_check_runs`, then hand both to the pure
        :func:`~maestro.pr_facts.classify_merge_blocker_label` so the recorded
        ``merge blocker:`` names the real gate — ``CI_RUNNING`` (hold),
        ``AWAITING_REVIEW`` (hand to Thomas), or ``CI_FAILING`` (fix). A
        git-side blocker (CONFLICTING / BEHIND_MAIN / AWAITING_PUSH) names a
        different gate and is returned unchanged. A refined verdict of
        ``NONE`` — a timing skew where the checks flipped green-and-approved
        between the two reads — is discarded in favour of the original code, so a
        not-landed lap never records a self-contradicting "blocker: NONE". Zero
        SDK cost; reuses the readiness/CI reads already backing the PR path.
        """
        agent = self._agent
        if block_reason not in _CI_REVIEW_MERGE_BLOCKERS:
            return block_reason
        readiness = agent.observe_pr_readiness(pr_num, pr_branch)
        ci_conclusions = classify_ci_conclusions(
            agent._observe_pr_check_runs(pr_num),
        )
        refined = classify_merge_blocker_label(readiness, ci_conclusions)
        return refined if refined != MERGE_BLOCK_NONE else block_reason

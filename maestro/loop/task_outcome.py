"""Task-outcome stages carved out of ``maestro.agent`` (proposal 106).

Holds the four stages ``MaestroAgent.run`` executes around step execution and
before the PR-remediation stamping stretch: the lap-start head watermark read
(``capture_lap_start_head_watermark``, which runs *before* step execution, so
before any in-lap push — #4567), outcome classification (``# 4.`` / ``# 4a.``
/ ``# 4b.``), the post-session watermark capture (``# 4b-i.``), and the
post-session mechanical landing actuator (``# 4b-ii.``). They form one
concern — answering "what is this lap's verdict, and which head SHAs did it
start from" before any stamping stage speaks — and ``run`` is their sole caller.

The extraction is behaviour-preserving: the same bodies, the same call order,
the same effects — only the receiver changed from ``self`` to a first positional
``agent`` argument, matching
:func:`maestro.loop.remediation_stamps.apply_host_state_stamp_gate` and
:func:`maestro.loop.lifecycle.teardown_task_run`. ``_is_api_error_finding``
and its transport-prefix constants live here, beside their sole caller.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from maestro.loop.pr_remediation import _REMEDIATION_DEDUP_PREFIX

if TYPE_CHECKING:
    from maestro.agent import MaestroAgent
    from maestro.memory import PlanStep
    from maestro.providers.base import Session
    from maestro.types import TaskOutcome

logger = logging.getLogger("maestro")


# Known API/transport error prefixes that, when a step output starts with
# one, indicate the "successful" step actually captured an infrastructure
# error rather than meaningful work output.  Deterministic pre-check fires
# before any LLM-based outcome classification at zero SDK cost.
#
# These specific transport-shaped prefixes are unique enough that any
# step output starting with one is overwhelmingly an infrastructure error
# and never legitimate analysis text.
_API_ERROR_PREFIXES = (
    "API Error:",
    "HTTPError:",
    "ConnectionError:",
    "TimeoutError:",
)

# The bare "Error:" prefix is too broad to use unconditionally — legitimate
# multi-line step output may start with "Error: file not found while parsing
# config, adding default and continuing" etc.  Only treat it as a hard-fail
# signal when the *entire* step output is error-shaped: single-line and
# short, matching the silhouette of a raw error string rather than narrative
# analysis text.  This caps the false-positive surface (defect:
# bare 'Error:' prefix can flag legitimate step output).
_GENERIC_ERROR_PREFIX = "Error:"
_GENERIC_ERROR_MAX_LEN = 200


def _is_api_error_finding(finding: str) -> bool:
    """Return True when *finding* looks like a raw API/transport error.

    Specific transport-shaped prefixes (``API Error:``, ``HTTPError:``,
    ``ConnectionError:``, ``TimeoutError:``) match unconditionally at the
    prefix position (after stripping whitespace).

    The broader ``Error:`` prefix only matches when the entire stripped
    finding is single-line and at most :data:`_GENERIC_ERROR_MAX_LEN`
    characters — i.e. the output is shaped like a raw error string rather
    than narrative analysis that merely starts with the word "Error:".

    Mid-text mentions of error strings are intentionally ignored to avoid
    false positives on legitimate analysis output.
    """
    stripped = finding.strip()
    if any(stripped.startswith(prefix) for prefix in _API_ERROR_PREFIXES):
        return True
    return (
        stripped.startswith(_GENERIC_ERROR_PREFIX)
        and "\n" not in stripped
        and len(stripped) <= _GENERIC_ERROR_MAX_LEN
    )


async def classify_task_outcome(
    agent: MaestroAgent,
    *,
    steps: list[PlanStep],
    all_output: list[str],
    acceptance_criteria: list[str] | None,
    goal: str,
    session: Session | None,
) -> tuple[TaskOutcome, list[dict[str, str]] | None, int]:
    """Classify a completed task's outcome verdict — the verbatim lift of
    ``run``'s ``# 4.`` + ``# 4a.`` + ``# 4b.`` blocks (proposal 106).

    A behaviour-preserving delegate: it reads every run-scoped value as a
    keyword param, reassigns none of the caller's locals, and returns the
    ``(outcome, criteria_verdicts, failed)`` trio the enclosing scope reads
    back — ``outcome`` into every downstream stamping stage,
    ``criteria_verdicts`` onto the ``TaskRecord``, and ``failed`` into the
    post-task reflection. The two purely-internal locals (``skipped`` and
    ``api_error_guard_tripped``) never escape the block, so they stay
    confined here.

    ``# 4.`` derives the base outcome from the step pass/fail/skip counts
    (empty plan or all-failed/skipped → ``failure``; none failed or skipped
    → ``success``; otherwise ``partial``). ``# 4a.`` is the API-error guard
    (``_is_api_error_finding``), which forces ``failure`` at zero SDK cost
    when a "successful" step output is actually a raw transport error.
    ``# 4b.`` is the criteria-anchored override: when *acceptance_criteria*
    exist and the guard did not trip, per-criterion MET/UNMET assessment
    (``_assess_criteria_outcome``, still session-attached) gates the final
    outcome so narrative framing cannot corrupt downstream consumers.
    """
    # 4. Determine outcome
    failed = sum(1 for s in steps if s.status == "failed")
    skipped = sum(1 for s in steps if s.status == "pending")
    outcome: TaskOutcome
    if not steps:
        # Empty plan — nothing was accomplished
        outcome = "failure"
    elif failed == 0 and skipped == 0:
        outcome = "success"
    elif failed + skipped == len(steps):
        # All steps either failed or were skipped — nothing succeeded
        outcome = "failure"
    else:
        outcome = "partial"

    # 4a. API-error outcome guard: force failure when any
    # "successful" step output actually starts with a known
    # API/transport error prefix.  Fires at zero SDK cost before
    # criteria-based classification to prevent error messages from
    # passing through the outcome classifier unchallenged.
    api_error_guard_tripped = False
    if outcome != "failure":
        for output_text in all_output:
            if _is_api_error_finding(output_text):
                logger.warning(
                    "Forcing outcome to 'failed': step output starts "
                    "with API error prefix: %s",
                    output_text[:100],
                )
                outcome = "failure"
                api_error_guard_tripped = True
                break

    # 4b. Criteria-anchored override: when acceptance_criteria
    # exist, per-criterion MET/UNMET assessment gates the overall
    # outcome so narrative framing cannot corrupt downstream
    # consumers (health_check, audit, contemplation, rejection).
    # Skipped when the API-error guard already forced failure —
    # otherwise the LLM's MET/UNMET verdicts (assessed against an
    # error-message step output) could overturn the guard.
    criteria_verdicts: list[dict[str, str]] | None = None
    if acceptance_criteria and not api_error_guard_tripped:
        outcome, criteria_verdicts = await agent._assess_criteria_outcome(
            outcome, acceptance_criteria, goal, steps,
            session=session,
        )

    return outcome, criteria_verdicts, failed


def capture_lap_start_head_watermark(
    agent: MaestroAgent,
    *,
    pr_branch: str | None,
    goal: str,
) -> str | None:
    """Read the ``origin/<pr_branch>`` head at the *start* of a
    ``Remediate PR #N:`` lap — the ``TaskRecord.pr_head_sha`` watermark.

    ``run`` calls this after workspace preparation (checkout only, never a
    push) and before step execution, so the read precedes *any* push in the
    lap: the agent's own in-session ``git push origin HEAD:<branch>`` as well
    as the ``# 4b-ii.`` session-landing actuator. Both move the local
    remote-tracking ref, so a later read would equal the just-pushed head and
    the stalled-lineage brake / costume-signature discount would mis-compare
    a delivering lap as a same-SHA no-op (#4567, PR #3155). Regression-guarded
    by ``tests/test_session_landing.py``. A ``None`` read (missing ref) is
    passed through and fails toward re-entry. Non-remediation goals and a
    missing ``pr_branch`` return ``None`` without reading git.
    """
    if pr_branch is None or not goal.startswith(_REMEDIATION_DEDUP_PREFIX):
        return None
    return agent._vcs.rev_parse(f"origin/{pr_branch}")


def capture_prepush_watermarks(
    agent: MaestroAgent,
    *,
    pr_branch: str | None,
    goal: str,
) -> tuple[str | None, str | None]:
    """Capture the two post-session, pre-§4b-ii watermarks — the verbatim
    lift of ``run``'s ``# 4b-i.`` block (proposal 106).

    A behaviour-preserving delegate: it reads every run-scoped value as a
    keyword param, reassigns none of the caller's locals, and returns the
    ``(pr_head_sha_stamp, base_head_sha_stamp)`` pair the enclosing scope
    reads back.

    The head value here is the post-session ``origin/<pr_branch>`` head read
    *before* the ``# 4b-ii.`` session-landing actuator; it feeds only
    ``apply_mergeable_flip_actuator``'s ``(pr_number, head_sha)`` idempotency
    key. It is NOT the ``TaskRecord.pr_head_sha`` watermark: the agent may
    self-push during step execution, moving the local tracking ref, so the
    lap-start head comes from :func:`capture_lap_start_head_watermark`
    instead (#4567).

    ``base_head_sha_stamp`` is the ``origin/main`` SHA the lap raced against
    (proposal 099 step 1), read post-session because the agent may merge
    main mid-lap. ``rev_parse`` is best-effort (``None`` on a missing ref),
    so a stamp failure records ``None`` rather than raising.
    """
    pr_head_sha_stamp: str | None = None
    base_head_sha_stamp: str | None = None
    if pr_branch is not None and goal.startswith(_REMEDIATION_DEDUP_PREFIX):
        pr_head_sha_stamp = agent._vcs.rev_parse(f"origin/{pr_branch}")
        base_head_sha_stamp = agent._vcs.rev_parse("origin/main")
    return pr_head_sha_stamp, base_head_sha_stamp


def apply_session_landing_actuator(
    agent: MaestroAgent,
    *,
    pr_branch: str | None,
    branch_name: str | None,
    goal: str,
) -> None:
    """Run the post-session mechanical landing actuator — the verbatim lift
    of ``run``'s ``# 4b-ii.`` block (proposal 106).

    A behaviour-preserving delegate: it reads every run-scoped value as a
    keyword param, reassigns none of the caller's locals, and returns
    nothing. The compound guard becomes early-return arms in the same order
    the inline conjunction evaluated them — flag off, no PR branch or a
    checkout-fallback ``branch_name != pr_branch``, or a non-remediation
    goal each short-circuits — then the unchanged ``Remediate PR #N:`` parse
    and the delegated ``_run_session_landing(pr_branch, pr_num)`` call fire,
    discarding the returned ``LandOutcome`` exactly as the inline block did.
    """
    # 4b-ii. Post-session mechanical landing actuator (opt-in
    # ``[loop] session_landing_enabled``, default off). The remediation
    # session authors the fix (the judgment the agent is good at) but the
    # terminal commit+push is where *read-substitutes-for-write* strikes:
    # the agent narrates "delivered" while the head SHA never moves. Run
    # the will-bypassing actuator here — after the session completes and
    # BEFORE verdict composition (§4c) — so gate-clean working-tree edits
    # on the owned PR branch are mechanically committed and pushed by the
    # loop's hand, not a composed sentence. On a landed push the
    # per-iteration merge-truth memo is dropped so §4c's ``is_pr_settled``
    # reads observe the just-pushed origin head, grounding the composed
    # verdict on the moved SHA rather than a narrated delivery. Flag off
    # ⇒ the actuator is never invoked and the legacy commit-only path is
    # unchanged.
    #
    # ``branch_name == pr_branch`` is load-bearing (mirrors §5-push): a
    # PR-branch checkout that fell back to the current branch leaves
    # ``branch_name is None`` and the run continues on ``main``, so
    # committing there and pushing ``pr_branch`` would land task edits on
    # the wrong branch. Only land when the run is actually on the PR head.
    if not agent.config.session_landing_enabled:
        return
    if pr_branch is None or branch_name != pr_branch:
        return
    if not goal.startswith(_REMEDIATION_DEDUP_PREFIX):
        return
    match = re.match(r"Remediate PR #(\d+):", goal)
    agent._run_session_landing(
        pr_branch,
        int(match.group(1)) if match else None,
    )

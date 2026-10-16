"""Git merge-truth oracle and the phantom / unconfirmed-landing rules 11, 12, 14
and 19-22 (``maestro/audit_checks_phantom.py``); split out of
``tests/test_audit_checks.py``.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from maestro import audit_checks, audit_checks_phantom
from maestro.audit_checks import (
    _DIRECTIVE_MAX_CHARS,
    MAX_FINDINGS_PER_RULE,
    AuditFinding,
    extract_issue_numbers,
    format_known_state_summary,
)
from maestro.audit_checks_phantom import (
    JsonOrphanedCureStore,
    JsonUnconfirmedOwnLandingStore,
    check_orphaned_cure,
    check_phantom_escalation,
    check_phantom_partial_prune,
    check_reconcile_near_miss,
    check_unconfirmed_own_landing,
    check_undercredited_landing,
    green_ready_handoff_prs,
)
from maestro.memory import (
    PlanStep,
    TaskRecord,
)
from maestro.messaging import Message
from maestro.remediation_tracker import JsonlStandDownVerdictLedger
from tests.test_audit_checks import (
    _LANDED_SHA,
    FAKE_DIR,
    _committed_sha_task,
    _impl_plan,
    _task,
)

# --- _git_pr_merged_default — subject-shape discriminator (#2619) -----------
# The oracle treats only a GitHub merge/squash *subject* as proof of merge; a
# bare body mention (Maestro cites PR numbers as ledger evidence in commit
# bodies) must not satisfy it. These patch ``subprocess.run`` with realistic
# ``%H\0%s`` stdout so the subject discriminator is actually exercised.


def _oracle_stdout(*subjects: str) -> str:
    """Build ``%H\\0%s``-formatted ``git log`` stdout from subject lines."""
    return "".join(
        f"deadbeef{i}\x00{subject}\n" for i, subject in enumerate(subjects)
    )


def test_git_pr_merged_default_merge_subject_is_merged() -> None:
    """A ``Merge pull request #N`` subject is proof of merge."""
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        return_value=SimpleNamespace(
            stdout=_oracle_stdout("Merge pull request #2564 from thomas-sodon/x"),
            returncode=0,
        ),
    ):
        assert audit_checks_phantom._git_pr_merged_default(2564) is True


def test_git_pr_merged_default_squash_subject_is_merged() -> None:
    """The GitHub squash suffix ``(#N)`` is proof of merge."""
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        return_value=SimpleNamespace(
            stdout=_oracle_stdout("fix: something useful (#2564)"),
            returncode=0,
        ),
    ):
        assert audit_checks_phantom._git_pr_merged_default(2564) is True


def test_git_pr_merged_default_body_only_mention_not_merged() -> None:
    """#2619: a body-only ``#N`` mention is NOT proof of merge.

    ``git log --grep`` over-selects this candidate (the number is in the
    message), but the subject carries no landing shape, so the oracle rejects
    it instead of treating a name-drop as a merge.
    """
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        return_value=SimpleNamespace(
            stdout=_oracle_stdout(
                "maestro: Merge-block gate records partial — cites #2564 as evidence",
            ),
            returncode=0,
        ),
    ):
        assert audit_checks_phantom._git_pr_merged_default(2564) is False


def test_git_pr_merged_default_squash_boundary_not_merged() -> None:
    """``(#25640)`` must not satisfy a query for ``#2564`` (digit boundary)."""
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        return_value=SimpleNamespace(
            stdout=_oracle_stdout("fix: adjacent number (#25640)"),
            returncode=0,
        ),
    ):
        assert audit_checks_phantom._git_pr_merged_default(2564) is False


def test_git_pr_merged_default_merge_boundary_not_merged() -> None:
    """``Merge pull request #25640`` must not satisfy a query for ``#2564``."""
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        return_value=SimpleNamespace(
            stdout=_oracle_stdout("Merge pull request #25640 from x/y"),
            returncode=0,
        ),
    ):
        assert audit_checks_phantom._git_pr_merged_default(2564) is False


def test_git_pr_merged_default_merge_wins_among_candidates() -> None:
    """A genuine merge subject still counts when over-selected alongside a
    body-only mention candidate — the first subject match wins."""
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        return_value=SimpleNamespace(
            stdout=_oracle_stdout(
                "maestro: follow-up commit referencing #2564 in prose",
                "Merge pull request #2564 from thomas-sodon/x",
            ),
            returncode=0,
        ),
    ):
        assert audit_checks_phantom._git_pr_merged_default(2564) is True


def test_git_pr_merged_default_skips_empty_subject_candidate() -> None:
    """A candidate line with an empty subject is skipped, not matched."""
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        return_value=SimpleNamespace(
            stdout="deadbeef0\x00\ndeadbeef1\x00Merge pull request #2564 from x/y\n",
            returncode=0,
        ),
    ):
        assert audit_checks_phantom._git_pr_merged_default(2564) is True


def test_git_pr_merged_default_empty_stdout_not_merged() -> None:
    """No candidate commits → not merged."""
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        return_value=SimpleNamespace(stdout="", returncode=0),
    ):
        assert audit_checks_phantom._git_pr_merged_default(2564) is False


def test_git_pr_merged_default_timeout_fails_open() -> None:
    """A hung ``git log`` (``TimeoutExpired``) fails open to not-merged."""
    import subprocess

    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["git", "log"], timeout=30),
    ):
        assert audit_checks_phantom._git_pr_merged_default(2564) is False


def test_git_pr_merged_default_oserror_fails_open() -> None:
    """A missing git binary (``OSError``) fails open to not-merged."""
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        side_effect=OSError("git missing"),
    ):
        assert audit_checks_phantom._git_pr_merged_default(2564) is False


def test_git_pr_merged_default_threads_cwd_and_bounded_timeout() -> None:
    """Invariants 1 & 3: the caller's ``cwd`` and the bounded module timeout
    are forwarded to ``subprocess.run`` unchanged."""
    with patch(
        "maestro.audit_checks_phantom.subprocess.run",
        return_value=SimpleNamespace(stdout="", returncode=0),
    ) as mock_run:
        audit_checks_phantom._git_pr_merged_default(2564, cwd="/repo/workspace")
    assert mock_run.call_args.kwargs["cwd"] == "/repo/workspace"
    assert (
        mock_run.call_args.kwargs["timeout"]
        == audit_checks_phantom._GIT_MERGE_TRUTH_TIMEOUT_SECONDS
    )


# ---------------------------------------------------------------------------
# Rule 11: orphaned_cure
# ---------------------------------------------------------------------------

_ORPHANED_DIR = Path("/fake/orphaned")
_ORPHANED_SIDECAR = str(_ORPHANED_DIR / "orphaned_cure_first_seen.json")
# The default JSON backend for the rule: it writes the same sidecar file, so the
# existing ``mem_fs[_ORPHANED_SIDECAR]`` seeding and content assertions stay valid.
_ORPHANED_BACKEND = JsonOrphanedCureStore(_ORPHANED_DIR)


def _rem_task(task_id: str = "r1", pr: int = 10) -> TaskRecord:
    """A remediation-shaped task (one of the laps the cure would eliminate)."""
    return _task(task_id, outcome="partial", goal=f"Remediate PR #{pr}: fix")


def test_orphaned_cure_flags_stale_implemented_with_remediation(
    mem_fs: dict[str, str],
) -> None:
    """(a) An aged implemented plan + a remediation lap → one finding."""
    mem_fs[_ORPHANED_SIDECAR] = json.dumps({"ep1": 0})
    findings = check_orphaned_cure(
        [_impl_plan()], [_rem_task()], _ORPHANED_BACKEND, 5, age_cycles=3,
    )
    assert len(findings) == 1
    assert findings[0].kind == "unmerged_work"
    assert findings[0].rule == "orphaned_cure"
    assert findings[0].cites == ["ep1"]
    assert "ep1" in findings[0].statement
    assert "Add the cure" in findings[0].statement


def test_orphaned_cure_silent_when_merged(
    mem_fs: dict[str, str],
) -> None:
    """(b) A plan that reached ``merged`` is not ``implemented`` → silent."""
    mem_fs[_ORPHANED_SIDECAR] = json.dumps({"ep1": 0})
    findings = check_orphaned_cure(
        [_impl_plan(status="merged")], [_rem_task()], _ORPHANED_BACKEND, 5,
        age_cycles=3,
    )
    assert findings == []


def test_orphaned_cure_silent_without_remediation_laps(
    mem_fs: dict[str, str],
) -> None:
    """(c) No remediation-shaped task in the window → silent, no sidecar I/O."""
    mem_fs[_ORPHANED_SIDECAR] = json.dumps({"ep1": 0})
    findings = check_orphaned_cure(
        [_impl_plan()], [_task("t1", goal="Build a measurement")],
        _ORPHANED_BACKEND, 5, age_cycles=3,
    )
    assert findings == []


def test_orphaned_cure_silent_under_age_threshold(
    mem_fs: dict[str, str],
) -> None:
    """(d) A freshly-seen implemented plan (age 0 on a missing sidecar) is
    never flagged on its first cycle, even with remediation laps."""
    findings = check_orphaned_cure(
        [_impl_plan()], [_rem_task()], _ORPHANED_BACKEND, 5, age_cycles=3,
    )
    assert findings == []
    # The sidecar was seeded at the current iteration so age is 0.
    assert json.loads(mem_fs[_ORPHANED_SIDECAR]) == {"ep1": 5}


def test_orphaned_cure_silent_when_disabled(
    mem_fs: dict[str, str],
) -> None:
    """(e) ``enabled=False`` short-circuits before any I/O."""
    mem_fs[_ORPHANED_SIDECAR] = json.dumps({"ep1": 0})
    findings = check_orphaned_cure(
        [_impl_plan()], [_rem_task()], _ORPHANED_BACKEND, 5,
        age_cycles=3, enabled=False,
    )
    assert findings == []


def test_orphaned_cure_caps_findings(
    mem_fs: dict[str, str],
) -> None:
    """More aged implemented plans than the per-rule cap surface only the cap."""
    plans = [_impl_plan(plan_id=f"ep{i}") for i in range(MAX_FINDINGS_PER_RULE + 2)]
    mem_fs[_ORPHANED_SIDECAR] = json.dumps({p.plan_id: 0 for p in plans})
    findings = check_orphaned_cure(
        plans, [_rem_task()], _ORPHANED_BACKEND, 5, age_cycles=3,
    )
    assert len(findings) == MAX_FINDINGS_PER_RULE


def test_orphaned_cure_prunes_departed_plans_from_sidecar(
    mem_fs: dict[str, str],
) -> None:
    """A sidecar entry for a plan no longer ``implemented`` is dropped."""
    mem_fs[_ORPHANED_SIDECAR] = json.dumps({"ep1": 0, "ep_gone": 0})
    check_orphaned_cure(
        [_impl_plan(), _impl_plan(plan_id="ep_gone", status="merged")],
        [_rem_task()], _ORPHANED_BACKEND, 5, age_cycles=3,
    )
    assert json.loads(mem_fs[_ORPHANED_SIDECAR]) == {"ep1": 0}


def test_load_orphaned_cure_seen_corrupt_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """A corrupt sidecar reads as empty rather than crashing the rule."""
    mem_fs[_ORPHANED_SIDECAR] = "{not json"
    assert audit_checks_phantom._load_orphaned_cure_seen(_ORPHANED_DIR / "orphaned_cure_first_seen.json") == {}


def test_load_orphaned_cure_seen_non_dict_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """A sidecar holding a JSON non-object (list) reads as empty."""
    mem_fs[_ORPHANED_SIDECAR] = json.dumps(["ep1", 0])
    assert audit_checks_phantom._load_orphaned_cure_seen(_ORPHANED_DIR / "orphaned_cure_first_seen.json") == {}


def test_load_orphaned_cure_seen_filters_bad_entries(
    mem_fs: dict[str, str],
) -> None:
    """Non-str keys, non-int values, and booleans are filtered out."""
    mem_fs[_ORPHANED_SIDECAR] = json.dumps(
        {"ep1": 2, "ep_bad": "x", "ep_bool": True},
    )
    seen = audit_checks_phantom._load_orphaned_cure_seen(
        _ORPHANED_DIR / "orphaned_cure_first_seen.json",
    )
    assert seen == {"ep1": 2}


# ---------------------------------------------------------------------------
# Rule 12: phantom_escalation
# ---------------------------------------------------------------------------


def _escalation_partial(
    task_id: str,
    summary: str,
    goal: str = "Remediate PR #2579: fix the gate",
) -> TaskRecord:
    """Build a ``partial`` remediation TaskRecord carrying *summary*."""
    return TaskRecord(
        task_id=task_id,
        goal=goal,
        plan=[PlanStep(step_id="s1", description="x", status="done")],
        outcome="partial",
        result_summary=summary,
        errors=[],
        duration_secs=0.5,
        timestamp="2025-01-01T00:00:00Z",
    )


def _outbound(content: str, message_id: str = "m1") -> Message:
    """Build an outbound (sender='maestro') message with *content*."""
    return Message(
        message_id=message_id,
        timestamp="2025-01-01T00:00:00Z",
        sender="maestro",
        content=content,
        message_type="question",
    )


def test_phantom_escalation_flags_prose_without_message() -> None:
    """3 escalation partials on #2579 with no referencing outbound message
    yield exactly one finding naming the issue."""
    tasks = [
        _escalation_partial(
            f"t{i}",
            f"Branch resolved, escalating PR #2579 once (lap {i}).",
        )
        for i in range(3)
    ]
    findings = check_phantom_escalation(tasks, [])
    assert len(findings) == 1
    assert findings[0].kind == "phantom_escalation"
    assert findings[0].rule == "phantom_escalation"
    assert "#2579" in findings[0].statement
    assert findings[0].cites == ["t0", "t1", "t2"]


def test_phantom_escalation_extinguished_by_outbound_message() -> None:
    """A single outbound message referencing #2579 silences the rule even
    when escalation prose exists — the knock left the building."""
    tasks = [
        _escalation_partial(
            f"t{i}",
            f"Escalating PR #2579 — awaiting merge (lap {i}).",
        )
        for i in range(3)
    ]
    messages = [_outbound("Handoff — please merge or close PR #2579.")]
    findings = check_phantom_escalation(tasks, messages)
    assert findings == []


def test_phantom_escalation_disabled_returns_empty() -> None:
    """``enabled=False`` short-circuits even with qualifying prose."""
    tasks = [
        _escalation_partial("t0", "Escalating PR #2579, no message sent."),
    ]
    findings = check_phantom_escalation(tasks, [], enabled=False)
    assert findings == []


def test_phantom_escalation_empty_tasks_returns_empty() -> None:
    """No tasks → no escalation prose → no findings."""
    findings = check_phantom_escalation([], [])
    assert findings == []


def test_phantom_escalation_skips_non_qualifying_and_uses_goal_fallback() -> None:
    """Non-partial, empty-summary, and non-escalation tasks are skipped; an
    escalation summary lacking ``#NNNN`` falls back to the goal's number; and
    an inbound (sender='thomas') message never extinguishes the rule."""
    tasks = [
        _task("ok", outcome="success", goal="Remediate PR #2579: x"),
        _escalation_partial("empty", "", goal="Remediate PR #2579: x"),
        _escalation_partial(
            "noprose",
            "Resolved on branch, awaiting tend push.",
            goal="Remediate PR #2579: x",
        ),
        _escalation_partial(
            "fallback",
            "Escalating once; standing down.",
            goal="Remediate PR #2581: y",
        ),
    ]
    inbound = Message(
        message_id="in1",
        timestamp="2025-01-01T00:00:00Z",
        sender="thomas",
        content="merge #2581 please",
        message_type="question",
    )
    findings = check_phantom_escalation(tasks, [inbound])
    assert len(findings) == 1
    assert "#2581" in findings[0].statement
    assert findings[0].cites == ["fallback"]


def test_phantom_escalation_caps_findings_per_rule() -> None:
    """More offending issues than the per-rule cap clip to the cap."""
    tasks = [
        _escalation_partial(
            tid,
            f"Escalating PR #{pr}.",
            goal=f"Remediate PR #{pr}: x",
        )
        for tid, pr in (("a", 2579), ("b", 2580), ("c", 2581))
    ]
    findings = check_phantom_escalation(tasks, [])
    assert len(findings) == MAX_FINDINGS_PER_RULE


def test_phantom_escalation_settled_prs_skips_pr_but_not_unrelated() -> None:
    """Proposal 097: a settled PR is skipped in the emission loop, while an
    unrelated still-escalating issue (not in ``settled_prs``) still fires."""
    tasks = [
        _escalation_partial(
            "t1", "Escalating PR #2579.", goal="Remediate PR #2579: x",
        ),
        _escalation_partial(
            "t2", "Escalating PR #2580.", goal="Remediate PR #2580: x",
        ),
    ]
    findings = check_phantom_escalation(
        tasks, [], settled_prs=frozenset({2579}),
    )
    # #2579 pre-filtered as settled; #2580 still fires.
    assert len(findings) == 1
    assert "#2580" in findings[0].statement
    assert all("#2579" not in f.statement for f in findings)


# ---------------------------------------------------------------------------
# Rule 14: unconfirmed_own_landing
# ---------------------------------------------------------------------------

_LANDING_DIR = Path("/fake/landing")
_LANDING_SIDECAR = str(_LANDING_DIR / "unconfirmed_landing_first_seen.json")
# The default JSON backend the rule now takes in place of a raw ``data_dir``.
# It reads/writes the same ``unconfirmed_landing_first_seen.json`` sidecar, so
# seeding via ``mem_fs[_LANDING_SIDECAR]`` and asserting on it below exercise the
# rule's flag-off behaviour byte-for-byte as before. Stateless beyond its bound
# dir, so one shared instance is safe across the mem_fs-isolated tests.
_LANDING_BACKEND = JsonUnconfirmedOwnLandingStore(_LANDING_DIR)

# A representative committed SHA carrying hex letters so it matches the
# SHA pattern (a bare decimal run is rejected as a SHA candidate).
_FIX_SHA = "ac48a769"


def _handoff_partial(
    task_id: str,
    summary: str,
    *,
    outcome: str = "partial",
    goal: str = "Remediate PR #2638",
) -> TaskRecord:
    """A task carrying an on-branch handoff summary (committed SHA + #NNNN)."""
    return TaskRecord(
        task_id=task_id,
        goal=goal,
        plan=[PlanStep(step_id="s1", description="x", status="done")],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary=summary,
        errors=[],
        duration_secs=0.5,
        timestamp="2025-01-01T00:00:00Z",
    )


def test_unconfirmed_landing_flags_aged_unlanded_sha(
    mem_fs: dict[str, str],
) -> None:
    """(a) An aged handoff whose committed SHA git shows is still not an
    ancestor of origin/main (and PR unmerged) yields exactly one finding."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    tasks = [
        _handoff_partial(
            "t1",
            f"Resolved on branch, awaiting tend push for PR #2638; "
            f"committed {_FIX_SHA}.",
        ),
    ]
    findings = check_unconfirmed_own_landing(
        tasks, _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert len(findings) == 1
    assert findings[0].kind == "unconfirmed_landing"
    assert findings[0].rule == "unconfirmed_own_landing"
    assert findings[0].cites == ["t1"]
    assert _FIX_SHA in findings[0].statement
    assert "#2638" in findings[0].statement


def test_unconfirmed_landing_binds_delivered_marker_handoff(
    mem_fs: dict[str, str],
) -> None:
    """A delivered-but-unmerged handoff (proposal 091) is an on-branch handoff
    even without a "resolved on branch" phrase: the delivered marker alone
    satisfies ``_ON_BRANCH_HANDOFF_MARKERS``. Its committed SHA, still off
    origin/main past grace, yields one finding. Guards the marker split against
    relying on §4c co-location."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                f"delivered to pr, awaiting review/merge for PR #2638; "
                f"committed {_FIX_SHA}.",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert len(findings) == 1
    assert findings[0].kind == "unconfirmed_landing"
    assert _FIX_SHA in findings[0].statement


def test_unconfirmed_landing_binds_owned_push_marker_handoff(
    mem_fs: dict[str, str],
) -> None:
    """An owned-push lap (proposal 098) is the strongest on-branch handoff of
    all — the SHA is still only local, never even delivered to origin. Named in
    ``_ON_BRANCH_HANDOFF_MARKERS`` so rule 14 keeps matching it after the
    §5-push marker swap."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                f"[owned-push-available] PR #2638 branch is ahead of origin; "
                f"committed {_FIX_SHA}.",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert len(findings) == 1
    assert findings[0].kind == "unconfirmed_landing"
    assert _FIX_SHA in findings[0].statement


def test_unconfirmed_landing_binds_push_unconfirmed_marker_handoff(
    mem_fs: dict[str, str],
) -> None:
    """A push-unconfirmed lap (proposal 099) is an on-branch handoff whose SHA
    never confirmed landing on ``origin/<pr_branch>`` — the unconfirmed-own-
    landing case rule 14 exists for. Named in ``_ON_BRANCH_HANDOFF_MARKERS``
    alongside OWNED_PUSH_AVAILABLE so the marker split costs rule 14 no
    coverage."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                f"[push-unconfirmed] PR #2638 branch: push exited 0 but origin "
                f"did not advance; committed {_FIX_SHA}.",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert len(findings) == 1
    assert findings[0].kind == "unconfirmed_landing"
    assert _FIX_SHA in findings[0].statement


def test_unconfirmed_landing_binds_sha_to_goal_pr_not_min_issue(
    mem_fs: dict[str, str],
) -> None:
    """A multi-issue handoff binds the SHA to the goal's ``Remediate PR #N``
    target, not the numeric minimum of every ``#NNNN`` the summary mentions.

    The summary references #2638 (the remediation target, unmerged) and a lower
    superseded #2100 (long merged). ``min`` would pick #2100, run the
    self-extinguish merge-truth check against that unrelated merged PR (falsely
    silencing the finding), and misattribute the SHA. Binding to the goal's PR
    flags it correctly against #2638.
    """
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    tasks = [
        _handoff_partial(
            "t1",
            f"Resolved on branch for PR #2638; supersedes #2100; "
            f"committed {_FIX_SHA}.",
            goal="Remediate PR #2638: fix the thing",
        ),
    ]
    # is_pr_merged returns True only for the *wrong* (min) PR #2100. If the SHA
    # were bound to min, the finding would be falsely extinguished.
    findings = check_unconfirmed_own_landing(
        tasks, _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: n == 2100,
    )
    assert len(findings) == 1
    assert "#2638" in findings[0].statement
    assert "#2100" not in findings[0].statement


def test_unconfirmed_landing_falls_back_to_min_without_remediation_goal(
    mem_fs: dict[str, str],
) -> None:
    """When the goal is not a ``Remediate PR #N`` lap (a self-directed handoff),
    the binding falls back to the lowest cited issue for determinism."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    tasks = [
        _handoff_partial(
            "t1",
            f"Resolved on branch for PR #2638; supersedes #2100; "
            f"committed {_FIX_SHA}.",
            goal="Investigate audit-stall recurrence",
        ),
    ]
    findings = check_unconfirmed_own_landing(
        tasks, _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert len(findings) == 1
    assert "#2100" in findings[0].statement


def test_unconfirmed_landing_silent_when_sha_is_ancestor(
    mem_fs: dict[str, str],
) -> None:
    """(b) When git shows the SHA reached origin/main, the handoff landed →
    no finding, and the SHA self-extinguishes from the sidecar."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    tasks = [
        _handoff_partial(
            "t1", f"Resolved on branch for PR #2638; committed {_FIX_SHA}.",
        ),
    ]
    findings = check_unconfirmed_own_landing(
        tasks, _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: True, is_pr_merged=lambda n: False,
    )
    assert findings == []
    assert json.loads(mem_fs[_LANDING_SIDECAR]) == {}


def test_unconfirmed_landing_silent_when_pr_merged_by_squash(
    mem_fs: dict[str, str],
) -> None:
    """The SHA is not an ancestor (squash rewrote it) but the PR merged → the
    landing is honest, so no finding and the SHA self-extinguishes."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    tasks = [
        _handoff_partial(
            "t1", f"Resolved on branch for PR #2638; committed {_FIX_SHA}.",
        ),
    ]
    findings = check_unconfirmed_own_landing(
        tasks, _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: True,
    )
    assert findings == []
    assert json.loads(mem_fs[_LANDING_SIDECAR]) == {}


def test_unconfirmed_landing_silent_within_grace_age(
    mem_fs: dict[str, str],
) -> None:
    """(c) A freshly-seen handoff (age 0 on a missing sidecar) is never flagged
    on its first cycle — the grace window is fair to Tend's push latency."""
    tasks = [
        _handoff_partial(
            "t1", f"Resolved on branch for PR #2638; committed {_FIX_SHA}.",
        ),
    ]
    findings = check_unconfirmed_own_landing(
        tasks, _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert findings == []
    # The sidecar was seeded at the current iteration so age is 0.
    assert json.loads(mem_fs[_LANDING_SIDECAR]) == {_FIX_SHA: 5}


def test_unconfirmed_landing_silent_when_disabled(
    mem_fs: dict[str, str],
) -> None:
    """(d) ``enabled=False`` short-circuits before any I/O or predicate call."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})

    def _boom(_arg: object) -> bool:
        raise AssertionError("predicate must not run when disabled")

    findings = check_unconfirmed_own_landing(
        tasks=[
            _handoff_partial(
                "t1", f"Resolved on branch for PR #2638; committed {_FIX_SHA}.",
            ),
        ],
        backend=_LANDING_BACKEND,
        iteration=5,
        age_cycles=3,
        enabled=False,
        is_ancestor=_boom,
        is_pr_merged=_boom,
    )
    assert findings == []
    # Sidecar untouched (no I/O).
    assert json.loads(mem_fs[_LANDING_SIDECAR]) == {_FIX_SHA: 0}


def test_unconfirmed_landing_ignores_non_partial(
    mem_fs: dict[str, str],
) -> None:
    """Scope is ``partial`` outcomes — a success task carrying the same handoff
    summary is out of this rule's scope."""

    def _boom(_arg: object) -> bool:
        raise AssertionError("predicate must not run for a non-partial task")

    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                f"Resolved on branch for PR #2638; committed {_FIX_SHA}.",
                outcome="success",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=_boom, is_pr_merged=_boom,
    )
    assert findings == []
    assert _LANDING_SIDECAR not in mem_fs


def test_unconfirmed_landing_skips_confirmed_attribution(
    mem_fs: dict[str, str],
) -> None:
    """A summary stating the cure was already merged before I touched it is a
    confirmation of another's land, not my own SHA to verify → skipped."""

    def _boom(_arg: object) -> bool:
        raise AssertionError("predicate must not run for a confirmation")

    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                f"Already merged before I touched it; resolved on branch for "
                f"PR #2638, committed {_FIX_SHA}.",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=_boom, is_pr_merged=_boom,
    )
    assert findings == []
    assert _LANDING_SIDECAR not in mem_fs


def test_unconfirmed_landing_skips_summary_without_handoff_marker(
    mem_fs: dict[str, str],
) -> None:
    """A partial summary citing a SHA + #NNNN but no on-branch handoff marker
    is not a handoff → silent, no predicate call, no I/O."""

    def _boom(_arg: object) -> bool:
        raise AssertionError("predicate must not run without a handoff marker")

    findings = check_unconfirmed_own_landing(
        [_handoff_partial("t1", f"Investigated PR #2638; committed {_FIX_SHA}.")],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=_boom, is_pr_merged=_boom,
    )
    assert findings == []
    assert _LANDING_SIDECAR not in mem_fs


def test_unconfirmed_landing_skips_handoff_without_issue(
    mem_fs: dict[str, str],
) -> None:
    """A goal-bound remediation handoff that omits its summary #NNNN stays
    skipped — deliberately out of scope for the PR-less widening.

    The goal is the unified ``Remediate PR #N:`` header (the colon form
    ``pr_number_from_remediation_goal`` recognises), so despite the summary
    carrying a SHA and no ``#NNNN`` this is a goal-bound remediation handoff that
    merely omitted its number, not a truly PR-less one. The sidecar is pre-seeded
    so the SHA reads aged (5 - 0 >= 3): were the goal-bound case admitted it would
    bind to #2638 and, with the PR unmerged, flag — so ``findings == []`` proves
    the skip rather than passing vacuously on the grace window."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                f"Resolved on branch; committed {_FIX_SHA}.",
                goal="Remediate PR #2638: fix the gate",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert findings == []


def test_unconfirmed_landing_flags_prless_handoff(
    mem_fs: dict[str, str],
) -> None:
    """A truly PR-less on-branch handoff (a committed SHA, a non-remediation
    goal, no #NNNN anywhere) is admitted and — once aged past the grace window —
    yields exactly one finding whose statement carries the SHA but no #NNNN.

    The absence of a #NNNN routes the finding to the forced arm's fire-or-close
    directive path; ``is_pr_merged`` is a booby-trap here because a PR-less
    candidate must be decided by SHA ancestry alone (no PR to consult)."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})

    def _no_pr_check(_n: int) -> bool:
        raise AssertionError("PR-less candidate must not consult is_pr_merged")

    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                f"Resolved on branch; committed {_FIX_SHA}.",
                goal="Investigate the flaky gate",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=_no_pr_check,
    )
    assert len(findings) == 1
    assert _FIX_SHA in findings[0].statement
    assert extract_issue_numbers(findings[0].statement) == set()


def test_unconfirmed_landing_prless_handoff_within_grace_silent(
    mem_fs: dict[str, str],
) -> None:
    """The same PR-less handoff within grace (SHA first-seen this cycle, age
    0 < 3) produces no finding — the widening keeps the grace window."""
    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                f"Resolved on branch; committed {_FIX_SHA}.",
                goal="Investigate the flaky gate",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert findings == []


def test_unconfirmed_landing_skips_handoff_without_sha(
    mem_fs: dict[str, str],
) -> None:
    """A handoff summary naming a #NNNN but carrying no committed SHA (a bare
    decimal run is not a SHA) has nothing to verify → silent."""
    findings = check_unconfirmed_own_landing(
        [_handoff_partial("t1", "Resolved on branch for PR #2638; pushed 1234567.")],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert findings == []


def test_unconfirmed_landing_ignores_stray_hex_word(
    mem_fs: dict[str, str],
) -> None:
    """A handoff summary containing a hex-only English word with no commit cue
    (e.g. 'defaced') must not be mis-bound as a committed SHA. Without an
    explicit commit anchor the old whole-summary scan flagged such tokens, and
    because a phantom SHA can never become an ancestor of origin/main the rule
    re-fired every cycle — the self-perpetuating false alarm it exists to
    prevent. The cue-anchored regex captures nothing here → silent."""
    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                "Resolved on branch for PR #2638; the prior fix was defaced "
                "and the feedface marker in 1a2b3c4-deadbeef was reverted.",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert findings == []


def test_unconfirmed_landing_binds_sha_with_at_prefix(
    mem_fs: dict[str, str],
) -> None:
    """A committed SHA written with an ``@`` cue (``@ac48a769``) is still bound,
    confirming the cue anchor preserves real-SHA detection beyond the bare
    ``committed <sha>`` form."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                f"Resolved on branch for PR #2638, awaiting tend push @{_FIX_SHA}.",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert len(findings) == 1
    assert findings[0].kind == "unconfirmed_landing"


@pytest.mark.parametrize(
    "phrasing",
    [
        "committed on this branch",
        "committed on the branch",
        "committed on branch",
    ],
)
def test_unconfirmed_landing_binds_sha_in_on_branch_phrasing(
    mem_fs: dict[str, str], phrasing: str,
) -> None:
    """The handoff shapes ``_ON_BRANCH_HANDOFF_MARKERS`` actually names — e.g.
    ``committed on this branch ac48a769`` — must bind their SHA. The cue word is
    separated from the SHA by ``on (this|the) branch``; before the bounded
    interjection was allowed the cue-anchored regex returned nothing, so a task
    that passed the marker gate was silently dropped (a false negative for the
    rule's core target). One fixed-vocabulary interjection now bridges cue→SHA."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    findings = check_unconfirmed_own_landing(
        [_handoff_partial("t1", f"For PR #2638, {phrasing} {_FIX_SHA}.")],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert len(findings) == 1
    assert findings[0].kind == "unconfirmed_landing"
    assert _FIX_SHA in findings[0].statement


def test_unconfirmed_landing_on_branch_phrase_without_cue_binds_nothing(
    mem_fs: dict[str, str],
) -> None:
    """The bounded on-branch interjection widens a *cue*, it does not become a
    cue of its own: a bare ``on this branch <hex>`` with no preceding
    commit/sha/@ cue must still capture nothing, so the stray-hex protection the
    cue anchor exists for is not re-opened by the widening."""
    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                "Resolved on branch for PR #2638; on this branch deadbeef "
                "was the prior state.",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert findings == []


def test_unconfirmed_landing_binds_only_cued_sha_amid_stray_hex(
    mem_fs: dict[str, str],
) -> None:
    """The discriminating case: one result_summary carrying BOTH a stray hex
    token (``defaced``, with no commit cue) AND a genuine committed SHA + #NNNN
    + on-branch marker binds only the cued SHA. The stray token must never reach
    the merge-truth oracle, so exactly one finding surfaces and it names the real
    SHA — not the phantom. Git is mocked (``is_ancestor``/``is_pr_merged``) and
    the sidecar lives in mem_fs, so no real subprocess or disk I/O runs."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    seen_shas: list[str] = []
    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                f"Resolved on branch for PR #2638, awaiting tend push; the "
                f"prior fix was defaced before I committed {_FIX_SHA}.",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: seen_shas.append(s) or False,
        is_pr_merged=lambda n: False,
    )
    # Only the cue-anchored SHA is ever consulted against git — the stray
    # ``defaced`` token is dropped before the oracle is asked.
    assert seen_shas == [_FIX_SHA]
    assert len(findings) == 1
    assert findings[0].kind == "unconfirmed_landing"
    assert _FIX_SHA in findings[0].statement
    assert "defaced" not in findings[0].statement


def test_unconfirmed_landing_skips_empty_summary(
    mem_fs: dict[str, str],
) -> None:
    """A partial task with an empty result_summary has no text to scan."""
    findings = check_unconfirmed_own_landing(
        [_handoff_partial("t1", "")],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert findings == []


def test_unconfirmed_landing_caps_findings_per_rule(
    mem_fs: dict[str, str],
) -> None:
    """More aged-unlanded handoffs than the per-rule cap surface only the cap,
    oldest PR first."""
    shas = ["aaa1111", "bbb2222", "ccc3333"]
    prs = [2638, 2639, 2640]
    mem_fs[_LANDING_SIDECAR] = json.dumps({s: 0 for s in shas})
    tasks = [
        _handoff_partial(
            f"t{pr}",
            f"Resolved on branch for PR #{pr}; committed {sha}.",
            goal=f"Remediate PR #{pr}",
        )
        for sha, pr in zip(shas, prs, strict=True)
    ]
    findings = check_unconfirmed_own_landing(
        tasks, _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=lambda n: False,
    )
    assert len(findings) == MAX_FINDINGS_PER_RULE
    # Oldest PR first: #2638 then #2639.
    assert "#2638" in findings[0].statement
    assert "#2639" in findings[1].statement


def test_unconfirmed_landing_routes_through_directive_bridge() -> None:
    """A rule-14 finding formats into a bounded next_task_directive carrying
    the kind, so the audit→directive bridge can route it unshelved."""
    finding = AuditFinding(
        kind="unconfirmed_landing",
        statement=f"A fix I committed ({_FIX_SHA}) for PR #2638 is still not landed.",
        cites=["t1"],
        rule="unconfirmed_own_landing",
    )
    directive = audit_checks.finding_to_directive(finding)
    assert directive.startswith("Address audit finding [unconfirmed_landing]:")
    assert len(directive) <= _DIRECTIVE_MAX_CHARS


def test_unconfirmed_landing_default_predicates_wired(
    mem_fs: dict[str, str],
) -> None:
    """With ``is_ancestor``/``is_pr_merged`` left ``None`` the default git
    oracles run; a non-zero merge-base exit (not ancestor) plus an empty
    ``git log`` (unmerged) means an aged handoff flags."""
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0})
    tasks = [
        _handoff_partial(
            "t1", f"Resolved on branch for PR #2638; committed {_FIX_SHA}.",
        ),
    ]

    def _fake_run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        # ``git merge-base --is-ancestor`` → non-zero exit (not an ancestor).
        if "merge-base" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        # ``git log`` candidate filter → empty (PR unmerged).
        return SimpleNamespace(stdout="", returncode=0)

    with patch("maestro.audit_checks_phantom.subprocess.run", side_effect=_fake_run):
        findings = check_unconfirmed_own_landing(
            tasks, _LANDING_BACKEND, 5, age_cycles=3,
        )
    assert len(findings) == 1


def test_unconfirmed_landing_settled_prs_skips_pr_but_not_prless(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 097: a PR-bearing handoff whose PR is in ``settled_prs`` is
    dropped before the merge predicate (which is booby-trapped to prove it is
    never consulted for the settled PR) and pruned from the sidecar, while a
    truly PR-less handoff — never in the set — still fires on SHA ancestry."""
    prless_sha = "bd51fa0c"
    mem_fs[_LANDING_SIDECAR] = json.dumps({_FIX_SHA: 0, prless_sha: 0})

    def _no_settled_pr_check(n: int) -> bool:
        # The settled PR must be skipped before the merge predicate; the
        # PR-less candidate never consults it.
        raise AssertionError("settled/PR-less candidate must not consult merge")

    findings = check_unconfirmed_own_landing(
        [
            _handoff_partial(
                "t1",
                f"Resolved on branch for PR #2638; committed {_FIX_SHA}.",
                goal="Remediate PR #2638: fix the thing",
            ),
            _handoff_partial(
                "t2",
                f"Resolved on branch; committed {prless_sha}.",
                goal="Investigate the flaky gate",
            ),
        ],
        _LANDING_BACKEND, 5, age_cycles=3,
        is_ancestor=lambda s: False, is_pr_merged=_no_settled_pr_check,
        settled_prs=frozenset({2638}),
    )
    # Only the PR-less handoff survives; the settled PR-bearing one is skipped.
    assert len(findings) == 1
    assert prless_sha in findings[0].statement
    assert _FIX_SHA not in findings[0].statement
    # The settled PR's SHA was pruned from the sidecar; the PR-less one persists.
    assert json.loads(mem_fs[_LANDING_SIDECAR]) == {prless_sha: 0}


def test_load_unconfirmed_landing_seen_corrupt_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """A corrupt sidecar reads as empty rather than crashing the rule."""
    mem_fs[_LANDING_SIDECAR] = "{not json"
    assert audit_checks_phantom._load_unconfirmed_landing_seen(
        _LANDING_DIR / "unconfirmed_landing_first_seen.json",
    ) == {}


def test_load_unconfirmed_landing_seen_non_dict_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """A sidecar holding a JSON non-object (list) reads as empty."""
    mem_fs[_LANDING_SIDECAR] = json.dumps([_FIX_SHA, 0])
    assert audit_checks_phantom._load_unconfirmed_landing_seen(
        _LANDING_DIR / "unconfirmed_landing_first_seen.json",
    ) == {}


def test_load_unconfirmed_landing_seen_filters_bad_entries(
    mem_fs: dict[str, str],
) -> None:
    """Non-str keys, non-int values, and booleans are filtered out."""
    mem_fs[_LANDING_SIDECAR] = json.dumps(
        {_FIX_SHA: 2, "bad": "x", "bool": True},
    )
    seen = audit_checks_phantom._load_unconfirmed_landing_seen(
        _LANDING_DIR / "unconfirmed_landing_first_seen.json",
    )
    assert seen == {_FIX_SHA: 2}


def test_load_unconfirmed_landing_seen_absent_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """An absent sidecar reads as empty (no crash, re-seeds from this cycle)."""
    assert audit_checks_phantom._load_unconfirmed_landing_seen(
        _LANDING_DIR / "unconfirmed_landing_first_seen.json",
    ) == {}


def test_format_known_state_summary_includes_stuck_pr(
    mem_fs: dict[str, str],
) -> None:
    """format_known_state_summary injects a STUCK-PR line when the ledger
    holds a stuck PR entry (covers the ``stuck_pr_line`` injection path)."""
    from maestro.pr_ledger import PRLedger

    ledger = PRLedger(FAKE_DIR, max_attempts_per_head=1)
    ledger.observe_head(123, "maestro/evolve/ep123", "deadbeef")
    ledger.record_attempt(123, outcome="noop", stuck_reason="merge-blocked")

    section = format_known_state_summary({}, [], data_dir=FAKE_DIR)
    assert "STUCK-PR" in section
    assert "#123" in section


def test_format_known_state_summary_includes_parked_pr(
    mem_fs: dict[str, str],
) -> None:
    """format_known_state_summary surfaces a PARKED-PR line when the ledger
    holds a PR whose budget was exhausted by progress laps, threading the
    configured budget so the parked threshold matches ``can_attempt``."""
    from maestro.pr_ledger import PRLedger

    ledger = PRLedger(FAKE_DIR, max_attempts_per_head=2)
    ledger.observe_head(456, "maestro/evolve/ep456", "cafef00d")
    ledger.record_attempt(456, outcome="progress", stuck_reason="in-progress")
    ledger.record_attempt(456, outcome="progress", stuck_reason="in-progress")

    section = format_known_state_summary(
        {}, [], data_dir=FAKE_DIR, pr_remediation_max_attempts_per_head=2,
    )
    assert "PARKED-PR" in section
    assert "#456" in section


# ---------------------------------------------------------------------------
# Rule 19: check_phantom_partial_prune (landed prune under-claimed partial)
# ---------------------------------------------------------------------------

_PRUNE_DIR = Path("/fake/prune")

# A real 12-hex plan_id shape (``uuid.uuid4().hex[:12]``); the exact id from the
# lived incident the rule was built for.
_PRUNE_PLAN_ID = "a01c09e3b9bf"


def _seed_prune_tasks(
    mem_fs: dict[str, str],
    tasks: list[dict[str, object]],
    data_dir: Path = _PRUNE_DIR,
) -> None:
    """Seed ``task_history.jsonl`` with raw task *records* (one line each)."""
    mem_fs[str(data_dir / "task_history.jsonl")] = "".join(
        json.dumps(t) + "\n" for t in tasks
    )


def _seed_prune_plans(
    mem_fs: dict[str, str],
    records: list[dict[str, object]],
    data_dir: Path = _PRUNE_DIR,
) -> None:
    """Seed ``evolution_plans.jsonl`` with raw plan *records* (one line each)."""
    mem_fs[str(data_dir / "evolution_plans.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in records
    )


def _prune_task(
    plan_id: str = _PRUNE_PLAN_ID,
    *,
    outcome: str = "partial",
    goal: str = "Prune stale pending_review plans",
    pr_head_sha: object = None,
    task_id: str = "prune-task-1",
    summary: str | None = None,
) -> dict[str, object]:
    """A self-directed, ``pr_head_sha``-less task that fired a prune lever."""
    if summary is None:
        summary = (
            f"Fired mark_plan_obsolete on plan {plan_id}; the gross "
            f"pending_review count read 21->18 so I stamped it partial."
        )
    task: dict[str, object] = {
        "task_id": task_id,
        "goal": goal,
        "outcome": outcome,
        "result_summary": summary,
    }
    if pr_head_sha is not None:
        task["pr_head_sha"] = pr_head_sha
    return task


# -- check_phantom_partial_prune: finding fires ----------------------------


def test_phantom_partial_prune_fires_on_landed_plan(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 1 — a partial owned-prune task whose cited plan_id reads
    ``obsolete`` on a fresh plan-store load surfaces exactly one finding."""
    _seed_prune_tasks(mem_fs, [_prune_task()])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    findings = check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind == "phantom_partial_prune"
    assert finding.rule == "phantom_partial_prune"
    assert _PRUNE_PLAN_ID in finding.statement
    assert finding.cites == ["prune-task-1"]


def test_phantom_partial_prune_silent_when_plan_pending(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 1/4 — when the cited plan is still ``pending_review`` the prune
    genuinely has not landed, so the ``partial`` is honest and nothing fires."""
    _seed_prune_tasks(mem_fs, [_prune_task()])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "pending_review"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True) == []


def test_phantom_partial_prune_self_extinguishes_no_qualifying_task(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 4 — with no partial owned-prune task citing a disk-obsolete
    plan_id, the rule returns no finding (self-extinguishing)."""
    # A success-stamped task (not partial) citing the obsolete plan_id.
    _seed_prune_tasks(mem_fs, [_prune_task(outcome="success")])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True) == []


def test_phantom_partial_prune_flag_gates_rule(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 3 — ``enabled=False`` suppresses the finding; ``True`` enables
    it on identical state."""
    _seed_prune_tasks(mem_fs, [_prune_task()])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=False) == []
    assert check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True) != []


def test_phantom_partial_prune_default_disabled(
    mem_fs: dict[str, str],
) -> None:
    """The master switch defaults off — the rule is silent unless opted in."""
    _seed_prune_tasks(mem_fs, [_prune_task()])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 50) == []


def test_phantom_partial_prune_zero_window_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """``n <= 0`` disables the rule at the window (no task tail read)."""
    _seed_prune_tasks(mem_fs, [_prune_task()])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 0, enabled=True) == []


def test_phantom_partial_prune_skips_pr_head_sha_task(
    mem_fs: dict[str, str],
) -> None:
    """A head-carrying record is a remediation lap, not an owned prune — skip."""
    _seed_prune_tasks(mem_fs, [_prune_task(pr_head_sha="deadbeefcafe")])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True) == []


def test_phantom_partial_prune_skips_external_origin_task(
    mem_fs: dict[str, str],
) -> None:
    """A PR-remediation goal classifies external, not a self-directed prune."""
    _seed_prune_tasks(mem_fs, [
        _prune_task(goal="Remediate PR #2986: resolve conflicts"),
    ])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True) == []


def test_phantom_partial_prune_skips_task_without_marker(
    mem_fs: dict[str, str],
) -> None:
    """A partial task whose summary never names the prune lever is not a prune."""
    _seed_prune_tasks(mem_fs, [
        _prune_task(summary=f"Investigated plan {_PRUNE_PLAN_ID} but did not act"),
    ])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True) == []


def test_phantom_partial_prune_skips_task_without_summary(
    mem_fs: dict[str, str],
) -> None:
    """A record with no ``result_summary`` string cannot cite a plan_id."""
    _seed_prune_tasks(mem_fs, [{
        "task_id": "no-summary",
        "goal": "Prune stale plans",
        "outcome": "partial",
    }])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True) == []


def test_phantom_partial_prune_ignores_marker_without_plan_id(
    mem_fs: dict[str, str],
) -> None:
    """The prune lever named but no 12-hex plan_id present — nothing to check."""
    _seed_prune_tasks(mem_fs, [
        _prune_task(summary="Fired mark_plan_obsolete but recorded no plan id"),
    ])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True) == []


def test_phantom_partial_prune_does_not_match_commit_sha(
    mem_fs: dict[str, str],
) -> None:
    """A 40-hex commit SHA in the summary is not mis-read as a 12-hex plan_id."""
    sha = "0123456789abcdef0123456789abcdef01234567"
    _seed_prune_tasks(mem_fs, [
        _prune_task(summary=f"Fired mark_plan_obsolete at commit {sha}"),
    ])
    # A plan whose id is a 12-char slice of that SHA must NOT be matched.
    _seed_prune_plans(mem_fs, [
        {"plan_id": sha[:12], "status": "obsolete"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True) == []


def test_phantom_partial_prune_dedupes_same_plan_id(
    mem_fs: dict[str, str],
) -> None:
    """Two partial tasks citing the same obsolete plan_id surface it once."""
    _seed_prune_tasks(mem_fs, [
        _prune_task(task_id="p1"),
        _prune_task(task_id="p2"),
    ])
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    findings = check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True)
    assert len(findings) == 1


def test_phantom_partial_prune_caps_findings(
    mem_fs: dict[str, str],
) -> None:
    """Distinct landed prunes are capped at the per-rule finding budget."""
    ids = ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]
    _seed_prune_tasks(mem_fs, [
        _prune_task(
            plan_id=pid,
            task_id=f"t{i}",
            summary=f"Fired mark_plan_obsolete on plan {pid}",
        )
        for i, pid in enumerate(ids)
    ])
    _seed_prune_plans(mem_fs, [
        {"plan_id": pid, "status": "obsolete"} for pid in ids
    ])
    findings = check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True)
    assert len(findings) == MAX_FINDINGS_PER_RULE


def test_phantom_partial_prune_no_finding_when_plan_absent(
    mem_fs: dict[str, str],
) -> None:
    """A cited plan_id absent from the store reads no ``obsolete`` — no finding."""
    _seed_prune_tasks(mem_fs, [_prune_task()])
    _seed_prune_plans(mem_fs, [
        {"plan_id": "ffffffffffff", "status": "obsolete"},
    ])
    assert check_phantom_partial_prune(_PRUNE_DIR, 50, enabled=True) == []


def test_phantom_partial_prune_registered_and_summarised() -> None:
    """Criterion 2 — the rule is in the registry and named in the summary."""
    registered = {
        fn.__name__ for fn in audit_checks._registered_audit_checks()
    }
    assert "check_phantom_partial_prune" in registered
    assert "check_phantom_partial_prune" in (
        audit_checks.format_audit_checks_summary()
    )


def _is_check_call(node: ast.AST) -> bool:
    """True for a call to ``*.check_phantom_partial_prune(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "check_phantom_partial_prune"
    )


def _is_flag_test(test: ast.expr) -> bool:
    """True for the guard ``self.config.phantom_partial_prune_enabled``."""
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "phantom_partial_prune_enabled"
        and isinstance(test.value, ast.Attribute)
        and test.value.attr == "config"
    )


def test_phantom_partial_prune_dispatched_flag_gated_in_audit_self_model() -> (
    None
):
    """Corrected-behaviour guard: rule 19 is not merely *registered* — it is
    actually *dispatched* from ``loop/self_audit.py::audit_self_model``, and every
    dispatch is gated by ``agent.config.phantom_partial_prune_enabled``.

    The original PR added ``check_phantom_partial_prune`` to
    ``_registered_audit_checks`` (a reporting-only registry consumed by
    ``format_audit_checks_summary``) but never called it from any execution
    path, so the ``phantom_partial_prune_enabled`` flag was inert and the rule
    never ran in production (PR #3001 review comment 3581055511). Every other
    ``phantom_partial_prune`` unit test exercises the function *directly*, so
    none would catch a missing or ungated dispatch — exactly the gap the
    reviewer flagged. This AST guard binds registry membership to a real,
    flag-gated call site: it fails loud if the dispatch is dropped (regressing
    to the inert-flag bug) or made unconditional (breaking the opt-in
    convention).

    Reads the committed ``maestro/loop/self_audit.py`` source; parsing a file needs no
    ``mem_fs`` and no mocks.
    """
    audit_src = (
        Path(__file__).resolve().parent.parent
        / "maestro" / "loop" / "self_audit.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(audit_src)

    audit_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "audit_self_model"
        ),
        None,
    )
    assert audit_fn is not None, "audit_self_model not found in self_audit.py"

    # Every call to the check anywhere in the coroutine.
    total_calls = sum(1 for n in ast.walk(audit_fn) if _is_check_call(n))
    # Calls that sit inside an ``if self.config.phantom_partial_prune_enabled:``
    # guard body (the single dispatch gate).
    flag_guards = [
        n
        for n in ast.walk(audit_fn)
        if isinstance(n, ast.If) and _is_flag_test(n.test)
    ]
    gated_calls = sum(
        1 for guard in flag_guards for n in ast.walk(guard) if _is_check_call(n)
    )

    # It IS dispatched...
    assert total_calls >= 1, (
        "check_phantom_partial_prune is registered but never dispatched in "
        "_audit_self_model — the phantom_partial_prune_enabled flag is inert"
    )
    # ...and every dispatch is flag-gated (fires only when the flag is true).
    assert gated_calls == total_calls, (
        "every check_phantom_partial_prune dispatch in _audit_self_model must "
        "be guarded by `if self.config.phantom_partial_prune_enabled:`"
    )


def _is_banked_check_call(node: ast.AST) -> bool:
    """True for a call to ``*.check_banked_owned_act_reperformance(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "check_banked_owned_act_reperformance"
    )


def _is_banked_flag_test(test: ast.expr) -> bool:
    """True for the guard ``self.config.banked_owned_act_enabled``."""
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "banked_owned_act_enabled"
        and isinstance(test.value, ast.Attribute)
        and test.value.attr == "config"
    )


def test_banked_owned_act_reperformance_dispatched_flag_gated_in_audit_self_model() -> (
    None
):
    """Corrected-behaviour guard: the banked-owned-act rule is not merely
    *registered* — it is actually *dispatched* from
    ``loop/self_audit.py::audit_self_model``, and every dispatch is gated by
    ``self.config.banked_owned_act_enabled``.

    The rule shipped fully implemented and unit-tested but carried no ``[audit]``
    flag and no dispatch call, so its artifact-id-axis shield governed nothing in
    production (#3082). This AST guard binds registry membership to a real,
    flag-gated call site: it fails loud if the dispatch is dropped (regressing to
    the inert-flag bug) or made unconditional (breaking the opt-in convention).
    Mirrors ``test_phantom_partial_prune_dispatched_flag_gated_in_audit_self_model``.

    Reads the committed ``maestro/loop/self_audit.py`` source; parsing a file needs no
    ``mem_fs`` and no mocks.
    """
    audit_src = (
        Path(__file__).resolve().parent.parent
        / "maestro" / "loop" / "self_audit.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(audit_src)

    audit_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "audit_self_model"
        ),
        None,
    )
    assert audit_fn is not None, "audit_self_model not found in self_audit.py"

    # Every call to the check anywhere in the coroutine.
    total_calls = sum(1 for n in ast.walk(audit_fn) if _is_banked_check_call(n))
    # Calls inside an ``if self.config.banked_owned_act_enabled:`` guard body.
    flag_guards = [
        n
        for n in ast.walk(audit_fn)
        if isinstance(n, ast.If) and _is_banked_flag_test(n.test)
    ]
    gated_calls = sum(
        1
        for guard in flag_guards
        for n in ast.walk(guard)
        if _is_banked_check_call(n)
    )

    # It IS dispatched...
    assert total_calls >= 1, (
        "check_banked_owned_act_reperformance is registered but never "
        "dispatched in _audit_self_model — the banked_owned_act_enabled flag "
        "is inert"
    )
    # ...and every dispatch is flag-gated (fires only when the flag is true).
    assert gated_calls == total_calls, (
        "every check_banked_owned_act_reperformance dispatch in "
        "_audit_self_model must be guarded by "
        "`if self.config.banked_owned_act_enabled:`"
    )


def test_undercredited_landing_fires_on_landed_sha() -> None:
    """Criterion 1 — a non-success task whose own committed SHA git proves is an
    ancestor of origin/main surfaces exactly one ``undercredited_landing``
    finding when the flag is enabled."""
    findings = check_undercredited_landing(
        [_committed_sha_task("t1")],
        enabled=True,
        is_ancestor=lambda s: True,
    )
    assert len(findings) == 1
    assert findings[0].kind == "undercredited_landing"
    assert findings[0].rule == "undercredited_landing"
    assert findings[0].cites == ["t1"]
    assert _LANDED_SHA in findings[0].statement
    assert "partial" in findings[0].statement


def test_undercredited_landing_fires_for_any_non_success_outcome() -> None:
    """The rule flags any non-success outcome (failed/investigation), not just
    ``partial`` — the under-claim is the same regardless of which label."""
    for outcome in ("failed", "investigation"):
        findings = check_undercredited_landing(
            [_committed_sha_task(f"t-{outcome}", outcome=outcome)],
            enabled=True,
            is_ancestor=lambda s: True,
        )
        assert len(findings) == 1
        assert outcome in findings[0].statement


def test_undercredited_landing_silent_on_success() -> None:
    """Criterion 3 — a ``success`` task is not under-claiming, so no finding
    (that is the over-claim rules' territory, not this one)."""
    findings = check_undercredited_landing(
        [_committed_sha_task("t1", outcome="success")],
        enabled=True,
        is_ancestor=lambda s: True,
    )
    assert findings == []


def test_undercredited_landing_silent_when_sha_not_ancestor() -> None:
    """Criterion 3 — when git shows the SHA is NOT an ancestor of origin/main
    the landing never happened → no finding (rule 14's territory)."""
    findings = check_undercredited_landing(
        [_committed_sha_task("t1")],
        enabled=True,
        is_ancestor=lambda s: False,
    )
    assert findings == []


def test_undercredited_landing_silent_without_committed_sha() -> None:
    """Criterion 3 — a non-success summary citing no own-committed SHA has
    nothing to verify → no finding, and the git oracle is never consulted."""
    called = False

    def _is_ancestor(_sha: str) -> bool:
        nonlocal called
        called = True
        return True

    findings = check_undercredited_landing(
        [_committed_sha_task("t1", summary="Investigated the churn; no fix.")],
        enabled=True,
        is_ancestor=_is_ancestor,
    )
    assert findings == []
    assert called is False


def test_undercredited_landing_silent_when_summary_missing() -> None:
    """A record whose ``result_summary`` is empty cannot cite a SHA."""
    task = _committed_sha_task("t1")
    task.result_summary = ""
    findings = check_undercredited_landing(
        [task], enabled=True, is_ancestor=lambda s: True,
    )
    assert findings == []


def test_undercredited_landing_disabled_emits_nothing() -> None:
    """Criterion 2 — with the master switch off the rule short-circuits to an
    empty list and never consults the git oracle (no side effects)."""
    called = False

    def _is_ancestor(_sha: str) -> bool:
        nonlocal called
        called = True
        return True

    findings = check_undercredited_landing(
        [_committed_sha_task("t1")],
        enabled=False,
        is_ancestor=_is_ancestor,
    )
    assert findings == []
    assert called is False


def test_undercredited_landing_dedupes_same_sha_across_tasks() -> None:
    """Two non-success tasks citing the same landed SHA surface it once."""
    findings = check_undercredited_landing(
        [_committed_sha_task("t1"), _committed_sha_task("t2")],
        enabled=True,
        is_ancestor=lambda s: True,
    )
    assert len(findings) == 1
    assert findings[0].cites == ["t1"]


def test_undercredited_landing_memoizes_ancestry_probe() -> None:
    """An unlanded SHA cited by several non-success tasks costs one git
    subprocess: the ancestry probe is memoized within the scan."""
    calls: list[str] = []

    def _is_ancestor(sha: str) -> bool:
        calls.append(sha)
        return False

    findings = check_undercredited_landing(
        [_committed_sha_task("t1"), _committed_sha_task("t2")],
        enabled=True,
        is_ancestor=_is_ancestor,
    )
    assert findings == []
    assert calls == [_LANDED_SHA]


def test_undercredited_landing_caps_findings() -> None:
    """Distinct landed SHAs are capped at the per-rule finding budget."""
    shas = ["aaaaaaab", "bbbbbbbc", "cccccccd"]
    tasks = [
        _committed_sha_task(f"t{i}", summary=f"committed {sha}.")
        for i, sha in enumerate(shas)
    ]
    findings = check_undercredited_landing(
        tasks, enabled=True, is_ancestor=lambda s: True,
    )
    assert len(findings) == MAX_FINDINGS_PER_RULE


def test_undercredited_landing_default_oracle_is_branch_merge_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When *is_ancestor* is omitted the rule falls back to
    ``is_branch_merged_into_main`` (the same merge-base --is-ancestor oracle
    rule 14 uses, keyed by SHA)."""
    seen: list[str] = []

    def _fake(sha: str) -> bool:
        seen.append(sha)
        return True

    monkeypatch.setattr(audit_checks_phantom, "is_branch_merged_into_main", _fake)
    findings = check_undercredited_landing(
        [_committed_sha_task("t1")], enabled=True,
    )
    assert len(findings) == 1
    assert seen == [_LANDED_SHA]


def test_undercredited_landing_registered_and_summarised() -> None:
    """Criterion 4 — the rule is in the registry and named in the summary."""
    registered = {
        fn.__name__ for fn in audit_checks._registered_audit_checks()
    }
    assert "check_undercredited_landing" in registered
    assert "check_undercredited_landing" in (
        audit_checks.format_audit_checks_summary()
    )


def test_undercredited_landing_in_reality_grounded_rules() -> None:
    """Criterion 4 — the rule's name is listed in ``_REALITY_GROUNDED_RULES``."""
    assert "undercredited_landing" in audit_checks._REALITY_GROUNDED_RULES


def _is_undercredited_check_call(node: ast.AST) -> bool:
    """True for a call to ``*.check_undercredited_landing(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "check_undercredited_landing"
    )


def _is_undercredited_flag_test(test: ast.expr) -> bool:
    """True for the guard ``self.config.undercredited_landing_enabled``."""
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "undercredited_landing_enabled"
        and isinstance(test.value, ast.Attribute)
        and test.value.attr == "config"
    )


def test_undercredited_landing_dispatched_flag_gated_in_audit_self_model() -> (
    None
):
    """Rule 20 is not merely registered — it is dispatched from
    ``loop/self_audit.py::audit_self_model`` and every dispatch is gated by
    ``self.config.undercredited_landing_enabled``.

    Binds registry membership to a real, flag-gated call site so a dropped
    dispatch (the inert-flag bug PR #3001 flagged for rule 19) or an
    unconditional one (breaking the opt-in convention) fails loud here.
    """
    audit_src = (
        Path(__file__).resolve().parent.parent
        / "maestro" / "loop" / "self_audit.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(audit_src)

    audit_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "audit_self_model"
        ),
        None,
    )
    assert audit_fn is not None, "audit_self_model not found in self_audit.py"

    total_calls = sum(
        1 for n in ast.walk(audit_fn) if _is_undercredited_check_call(n)
    )
    flag_guards = [
        n
        for n in ast.walk(audit_fn)
        if isinstance(n, ast.If) and _is_undercredited_flag_test(n.test)
    ]
    gated_calls = sum(
        1
        for guard in flag_guards
        for n in ast.walk(guard)
        if _is_undercredited_check_call(n)
    )

    assert total_calls >= 1, (
        "check_undercredited_landing is registered but never dispatched in "
        "_audit_self_model — the undercredited_landing_enabled flag is inert"
    )
    assert gated_calls == total_calls, (
        "every check_undercredited_landing dispatch in _audit_self_model must "
        "be guarded by `if self.config.undercredited_landing_enabled:`"
    )


# ---------------------------------------------------------------------------
# Rule 21: check_reconcile_near_miss (≤N-behind unlanded own PR, no reconcile SHA)
# ---------------------------------------------------------------------------

# A committed SHA carrying hex letters so it matches SELF_COMMITTED_SHA_RE.
_RECONCILE_SHA = "ac48a769"


def _reconcile_task(
    task_id: str,
    *,
    outcome: str = "partial",
    summary: str | None = None,
) -> TaskRecord:
    """A partial remediation task citing an own PR but no reconcile SHA."""
    if summary is None:
        summary = (
            "Resolved threads on #3083; branch is a few commits behind main — "
            "awaiting review/merge."
        )
    return TaskRecord(
        task_id=task_id,
        goal="Remediate PR #3083",
        plan=[PlanStep(step_id="s1", description="x", status="done")],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary=summary,
        errors=[],
        duration_secs=0.5,
        timestamp="2026-07-15T00:00:00Z",
    )


def test_reconcile_near_miss_fires_on_near_unlanded_no_sha() -> None:
    """Criterion 1 — a partial remediation on a git-unlanded own PR measured ≤
    max_behind behind origin/main with no authored reconcile SHA surfaces
    exactly one ``reconcile_near_miss`` finding when the flag is enabled."""
    findings = check_reconcile_near_miss(
        [_reconcile_task("t1")],
        enabled=True,
        max_behind=10,
        is_settled=lambda n: False,
        commits_behind=lambda n: 7,
        is_ancestor=lambda s: False,
    )
    assert len(findings) == 1
    assert findings[0].kind == "reconcile_near_miss"
    assert findings[0].rule == "reconcile_near_miss"
    assert findings[0].cites == ["t1"]
    assert "#3083" in findings[0].statement
    assert "Reconcile+push" in findings[0].statement


def test_reconcile_near_miss_silent_when_lineage_sha_landed() -> None:
    """Criterion 2 — the rule self-extinguishes once any lineage SHA is
    confirmed an ancestor of origin/main (a landed reconcile)."""
    summary = (
        f"Reconciled #3083; committed {_RECONCILE_SHA} — awaiting tend push."
    )
    findings = check_reconcile_near_miss(
        [_reconcile_task("t1", summary=summary)],
        enabled=True,
        max_behind=10,
        is_settled=lambda n: False,
        commits_behind=lambda n: 7,
        is_ancestor=lambda s: True,
    )
    assert findings == []


def test_reconcile_near_miss_silent_above_max_behind() -> None:
    """Criterion 3 — no finding when the measured distance exceeds
    max_behind (the disposal band's territory, not the reconcile lane)."""
    findings = check_reconcile_near_miss(
        [_reconcile_task("t1")],
        enabled=True,
        max_behind=10,
        is_settled=lambda n: False,
        commits_behind=lambda n: 11,
        is_ancestor=lambda s: False,
    )
    assert findings == []


def test_reconcile_near_miss_fires_at_exact_ceiling() -> None:
    """The near band is inclusive of ``max_behind`` — a PR exactly at the
    ceiling still fires (only strictly greater is the disposal band)."""
    findings = check_reconcile_near_miss(
        [_reconcile_task("t1")],
        enabled=True,
        max_behind=10,
        is_settled=lambda n: False,
        commits_behind=lambda n: 10,
        is_ancestor=lambda s: False,
    )
    assert len(findings) == 1


def test_reconcile_near_miss_silent_when_level_with_main() -> None:
    """A PR level with origin/main (0 behind) is not 'behind' — nothing to
    reconcile, so the rule stays silent (the awaiting-approval terminal)."""
    findings = check_reconcile_near_miss(
        [_reconcile_task("t1")],
        enabled=True,
        max_behind=10,
        is_settled=lambda n: False,
        commits_behind=lambda n: 0,
        is_ancestor=lambda s: False,
    )
    assert findings == []


def test_reconcile_near_miss_silent_when_distance_unmeasurable() -> None:
    """An un-mappable PR (``commits_behind`` returns ``None``) cannot be
    measured, so the rule never fires."""
    findings = check_reconcile_near_miss(
        [_reconcile_task("t1")],
        enabled=True,
        max_behind=10,
        is_settled=lambda n: False,
        commits_behind=lambda n: None,
        is_ancestor=lambda s: False,
    )
    assert findings == []


def test_reconcile_near_miss_silent_when_settled() -> None:
    """A settled PR (merged or closed) owes no reconcile — no finding, and the
    distance is never measured."""
    called = False

    def _behind(_n: int) -> int:
        nonlocal called
        called = True
        return 7

    findings = check_reconcile_near_miss(
        [_reconcile_task("t1")],
        enabled=True,
        max_behind=10,
        is_settled=lambda n: True,
        commits_behind=_behind,
        is_ancestor=lambda s: False,
    )
    assert findings == []
    assert called is False


def test_reconcile_near_miss_silent_on_non_partial() -> None:
    """Only ``partial`` remediations are scanned — a success/failed/investigation
    outcome is not a near-miss for this rule."""
    for outcome in ("success", "failed", "investigation"):
        findings = check_reconcile_near_miss(
            [_reconcile_task(f"t-{outcome}", outcome=outcome)],
            enabled=True,
            max_behind=10,
            is_settled=lambda n: False,
            commits_behind=lambda n: 7,
            is_ancestor=lambda s: False,
        )
        assert findings == []


def test_reconcile_near_miss_silent_without_pr_reference() -> None:
    """A partial summary citing no ``#NNNN`` has no PR to reconcile → no
    finding, and neither git seam is consulted."""
    settled_called = False
    behind_called = False

    def _settled(_n: int) -> bool:
        nonlocal settled_called
        settled_called = True
        return False

    def _behind(_n: int) -> int:
        nonlocal behind_called
        behind_called = True
        return 7

    findings = check_reconcile_near_miss(
        [_reconcile_task("t1", summary="Investigated churn; no PR touched.")],
        enabled=True,
        max_behind=10,
        is_settled=_settled,
        commits_behind=_behind,
        is_ancestor=lambda s: False,
    )
    assert findings == []
    assert settled_called is False
    assert behind_called is False


def test_reconcile_near_miss_silent_when_summary_empty() -> None:
    """A partial record whose ``result_summary`` is empty cannot cite a PR →
    no finding, and no git seam is consulted."""
    task = _reconcile_task("t1")
    task.result_summary = ""
    findings = check_reconcile_near_miss(
        [task],
        enabled=True,
        max_behind=10,
        is_settled=lambda n: False,
        commits_behind=lambda n: 7,
        is_ancestor=lambda s: False,
    )
    assert findings == []


def test_reconcile_near_miss_disabled_emits_nothing() -> None:
    """Criterion 4/5 — with the master switch off the rule short-circuits and
    never consults any injected seam (no side effects)."""
    called = False

    def _settled(_n: int) -> bool:
        nonlocal called
        called = True
        return False

    findings = check_reconcile_near_miss(
        [_reconcile_task("t1")],
        enabled=False,
        max_behind=10,
        is_settled=_settled,
        commits_behind=lambda n: 7,
        is_ancestor=lambda s: False,
    )
    assert findings == []
    assert called is False


def test_reconcile_near_miss_no_seams_short_circuits() -> None:
    """Criterion 5 — a missing ``is_settled``/``commits_behind`` seam
    short-circuits to an empty list with no I/O (no module-level git default)."""
    assert check_reconcile_near_miss(
        [_reconcile_task("t1")],
        enabled=True,
        commits_behind=lambda n: 7,
    ) == []
    assert check_reconcile_near_miss(
        [_reconcile_task("t1")],
        enabled=True,
        is_settled=lambda n: False,
    ) == []


def test_reconcile_near_miss_default_ancestor_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When *is_ancestor* is omitted the rule falls back to
    ``is_branch_merged_into_main`` (the same oracle rule 20 uses)."""
    seen: list[str] = []

    def _fake(sha: str) -> bool:
        seen.append(sha)
        return True

    monkeypatch.setattr(audit_checks_phantom, "is_branch_merged_into_main", _fake)
    summary = f"Reconciled #3083; committed {_RECONCILE_SHA}."
    findings = check_reconcile_near_miss(
        [_reconcile_task("t1", summary=summary)],
        enabled=True,
        max_behind=10,
        is_settled=lambda n: False,
        commits_behind=lambda n: 7,
    )
    # The landed SHA (default oracle returns True) self-extinguishes the rule.
    assert findings == []
    assert seen == [_RECONCILE_SHA]


def test_reconcile_near_miss_dedupes_pr_across_tasks() -> None:
    """Two partial tasks citing the same near-miss PR surface it once; the
    settlement and distance probes are memoized per PR."""
    settled_calls: list[int] = []
    behind_calls: list[int] = []

    findings = check_reconcile_near_miss(
        [_reconcile_task("t1"), _reconcile_task("t2")],
        enabled=True,
        max_behind=10,
        is_settled=lambda n: settled_calls.append(n) or False,
        commits_behind=lambda n: behind_calls.append(n) or 7,
        is_ancestor=lambda s: False,
    )
    assert len(findings) == 1
    assert findings[0].cites == ["t1"]
    assert settled_calls == [3083]
    assert behind_calls == [3083]


def test_reconcile_near_miss_caps_findings() -> None:
    """Distinct near-miss PRs are capped at the per-rule finding budget."""
    tasks = [
        _reconcile_task(
            f"t{i}",
            summary=f"Branch behind main on #{3000 + i} — awaiting merge.",
        )
        for i in range(MAX_FINDINGS_PER_RULE + 2)
    ]
    findings = check_reconcile_near_miss(
        tasks,
        enabled=True,
        max_behind=10,
        is_settled=lambda n: False,
        commits_behind=lambda n: 7,
        is_ancestor=lambda s: False,
    )
    assert len(findings) == MAX_FINDINGS_PER_RULE


def test_reconcile_near_miss_registered_and_summarised() -> None:
    """Criterion 4 — the rule is in the registry and named in the summary."""
    registered = {
        fn.__name__ for fn in audit_checks._registered_audit_checks()
    }
    assert "check_reconcile_near_miss" in registered
    assert "check_reconcile_near_miss" in (
        audit_checks.format_audit_checks_summary()
    )


def _is_reconcile_check_call(node: ast.AST) -> bool:
    """True for a call to ``*.check_reconcile_near_miss(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "check_reconcile_near_miss"
    )


def _is_reconcile_flag_test(test: ast.expr) -> bool:
    """True for the guard ``self.config.reconcile_near_miss_enabled``."""
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "reconcile_near_miss_enabled"
        and isinstance(test.value, ast.Attribute)
        and test.value.attr == "config"
    )


def test_reconcile_near_miss_dispatched_flag_gated_in_audit_self_model() -> None:
    """Rule 21 is dispatched from ``loop/self_audit.py::audit_self_model`` and every
    dispatch is gated by ``agent.config.reconcile_near_miss_enabled``."""
    audit_src = (
        Path(__file__).resolve().parent.parent
        / "maestro" / "loop" / "self_audit.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(audit_src)

    audit_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "audit_self_model"
        ),
        None,
    )
    assert audit_fn is not None, "audit_self_model not found in self_audit.py"

    total_calls = sum(
        1 for n in ast.walk(audit_fn) if _is_reconcile_check_call(n)
    )
    flag_guards = [
        n
        for n in ast.walk(audit_fn)
        if isinstance(n, ast.If) and _is_reconcile_flag_test(n.test)
    ]
    gated_calls = sum(
        1
        for guard in flag_guards
        for n in ast.walk(guard)
        if _is_reconcile_check_call(n)
    )

    assert total_calls >= 1, (
        "check_reconcile_near_miss is registered but never dispatched in "
        "_audit_self_model — the reconcile_near_miss_enabled flag is inert"
    )
    assert gated_calls == total_calls, (
        "every check_reconcile_near_miss dispatch in _audit_self_model must "
        "be guarded by `if self.config.reconcile_near_miss_enabled:`"
    )


def _is_settlement_record_call(node: ast.AST) -> bool:
    """True for a call to the module-level ``reconcile_pr_settlement(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "reconcile_pr_settlement"
    )


def _is_settlement_record_flag_test(test: ast.expr) -> bool:
    """True for the guard ``self.config.settlement_record_reconciler_enabled``."""
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "settlement_record_reconciler_enabled"
        and isinstance(test.value, ast.Attribute)
        and test.value.attr == "config"
    )


def _is_settlement_record_flag_off_guard(stmt: ast.stmt) -> bool:
    """True for ``if not self.config.settlement_record_reconciler_enabled: return``."""
    return (
        isinstance(stmt, ast.If)
        and isinstance(stmt.test, ast.UnaryOp)
        and isinstance(stmt.test.op, ast.Not)
        and _is_settlement_record_flag_test(stmt.test.operand)
        and any(isinstance(s, ast.Return) for s in stmt.body)
    )


def _is_reconcile_settled_pr_records_call(node: ast.AST) -> bool:
    """True for a call to ``self._reconcile_settled_pr_records(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_reconcile_settled_pr_records"
    )


def test_settlement_record_reconciler_dispatched_flag_gated_in_audit() -> None:
    """Proposal 103: ``reconcile_pr_settlement`` has exactly one dispatch site in
    ``agent.py`` — the shared ``_reconcile_settled_pr_records`` helper — and that
    helper flag-gates the dispatch with an early return on
    ``not self.config.settlement_record_reconciler_enabled``. All three drive
    faces (the per-cycle ``loop/self_audit.py::audit_self_model`` sweep, the merge/close-transition
    ``_reconcile_settled_merge_check`` drive, and the tend-pass
    ``_reconcile_settled_tend_prs`` drive) reach the reconciler *only* through
    that single helper, so disabling the flag keeps the whole path inert in
    production no matter which face calls it."""
    agent_src = (
        Path(__file__).resolve().parent.parent / "maestro" / "agent.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(agent_src)

    # Exactly one module-level ``reconcile_pr_settlement(...)`` dispatch, so the
    # flag can gate the drive in one place.
    dispatch_calls = [n for n in ast.walk(tree) if _is_settlement_record_call(n)]
    assert len(dispatch_calls) == 1, (
        "reconcile_pr_settlement must have exactly one dispatch site "
        "(_reconcile_settled_pr_records); found "
        f"{len(dispatch_calls)}"
    )

    drive_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_reconcile_settled_pr_records"
        ),
        None,
    )
    assert drive_fn is not None, (
        "_reconcile_settled_pr_records not found in agent.py"
    )
    # The dispatch lives in the helper, guarded by a flag-off early return.
    assert any(_is_settlement_record_call(n) for n in ast.walk(drive_fn)), (
        "the reconcile_pr_settlement dispatch must live in "
        "_reconcile_settled_pr_records"
    )
    assert any(
        _is_settlement_record_flag_off_guard(stmt) for stmt in drive_fn.body
    ), (
        "_reconcile_settled_pr_records must early-return when "
        "settlement_record_reconciler_enabled is off, so disabling the flag "
        "keeps every drive face inert"
    )

    # No drive face calls the reconciler directly — every one delegates to the
    # single flag-gated helper, so no face can bypass the gate.
    audit_tree = ast.parse(
        (
            Path(__file__).resolve().parent.parent
            / "maestro" / "loop" / "self_audit.py"
        ).read_text(encoding="utf-8")
    )
    for face_name, face_tree in (
        ("audit_self_model", audit_tree),
        ("_reconcile_settled_merge_check", tree),
        ("_reconcile_settled_tend_prs", tree),
    ):
        face_fn = next(
            (
                node
                for node in ast.walk(face_tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == face_name
            ),
            None,
        )
        assert face_fn is not None, f"{face_name} not found"
        assert not any(
            _is_settlement_record_call(n) for n in ast.walk(face_fn)
        ), (
            f"{face_name} must not call reconcile_pr_settlement directly; it "
            "must delegate to the flag-gated _reconcile_settled_pr_records"
        )
        assert any(
            _is_reconcile_settled_pr_records_call(n) for n in ast.walk(face_fn)
        ), f"{face_name} must delegate to _reconcile_settled_pr_records"


# ---------------------------------------------------------------------------
# Rule 22: green_ready_handoff_prs (merge-ready owned PR never handed off)
# ---------------------------------------------------------------------------

_GREEN_READY_DIR = Path("/fake/green-ready")


def test_green_ready_handoff_fires_once_on_green_ready() -> None:
    """Criterion 1 — a merge-ready owned PR with no standdown is returned for
    exactly one ``deliver_handoff`` dispatch."""
    green_probes: list[int] = []
    standdown_probes: list[int] = []
    prs = green_ready_handoff_prs(
        [3181],
        enabled=True,
        is_green_ready=lambda n: green_probes.append(n) or True,
        has_standdown=lambda n: standdown_probes.append(n) or False,
    )
    assert prs == [3181]
    # Each oracle is consulted once for the single candidate.
    assert green_probes == [3181]
    assert standdown_probes == [3181]


def test_green_ready_handoff_suppressed_when_standdown_exists() -> None:
    """Criterion 2 — once the pr_number-keyed handoff standdown exists the PR is
    filtered out (self-extinguishing) and the readiness oracle is never even
    consulted, so ``deliver_handoff`` is never re-invoked."""

    def _boom(_n: int) -> bool:
        raise AssertionError("readiness must not be probed for a stood-down PR")

    prs = green_ready_handoff_prs(
        [3181],
        enabled=True,
        is_green_ready=_boom,
        has_standdown=lambda n: True,
    )
    assert prs == []


def test_green_ready_handoff_skips_ci_pending_or_failing() -> None:
    """Criterion 3 — a CI-pending or CI-failing owned PR reads not-green-ready,
    so it is not returned (no handoff)."""
    prs = green_ready_handoff_prs(
        [3181, 3182],
        enabled=True,
        # #3181 is green-ready; #3182 is CI-pending/failing (not ready).
        is_green_ready=lambda n: n == 3181,
        has_standdown=lambda n: False,
    )
    assert prs == [3181]


def test_green_ready_handoff_disabled_returns_empty() -> None:
    """Criterion 4 (pure surface) — ``enabled=False`` short-circuits before any
    oracle runs (no side effects)."""

    def _boom(_n: int) -> bool:
        raise AssertionError("no oracle may run when the rule is disabled")

    prs = green_ready_handoff_prs(
        [3181],
        enabled=False,
        is_green_ready=_boom,
        has_standdown=_boom,
    )
    assert prs == []


def test_green_ready_handoff_dedupes_and_sorts() -> None:
    """Duplicate owned PR numbers collapse; the result is ascending."""
    prs = green_ready_handoff_prs(
        [3182, 3181, 3182, 3181],
        enabled=True,
        is_green_ready=lambda n: True,
        has_standdown=lambda n: False,
    )
    assert prs == [3181, 3182]


def test_green_ready_handoff_ignores_nonpositive_numbers() -> None:
    """A zero/negative PR number (a malformed state) is ignored, never probed."""
    prs = green_ready_handoff_prs(
        [0, -5, 3181],
        enabled=True,
        is_green_ready=lambda n: True,
        has_standdown=lambda n: False,
    )
    assert prs == [3181]


def test_green_ready_handoff_registered_and_summarised() -> None:
    """The rule is in the deterministic registry and named in the summary."""
    registered = {
        fn.__name__ for fn in audit_checks._registered_audit_checks()
    }
    assert "green_ready_handoff_prs" in registered
    assert "green_ready_handoff_prs" in (
        audit_checks.format_audit_checks_summary()
    )


def _stand_down_rows(mem_fs: dict[str, str], data_dir: Path) -> list[dict]:
    """Parse every JSONL row in *data_dir*'s ``stand_down_verdicts.jsonl``."""
    raw = mem_fs.get(str(data_dir / "stand_down_verdicts.jsonl"))
    if raw is None:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def test_confirm_handoff_delivery_yields_pr_keyed_row_when_actuator_persists(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 1 — after the arm fires ``deliver_handoff`` on a merge-ready
    owned PR, a row keyed to that pr_number exists in ``stand_down_verdicts.jsonl``
    when the rule returns. Here the actuator itself leaves the durable verdict
    row, so the bind reports 'delivered' (True) and does not backfill."""

    def _actuator() -> bool:
        # Simulate an actuator that leaves the durable verdict artifact behind.
        audit_checks_phantom._persist_handoff_stand_down_row(
            3181, JsonlStandDownVerdictLedger(_GREEN_READY_DIR), head_sha="deadbeef", now="t-actuator",
        )
        return True

    delivered = audit_checks_phantom.confirm_handoff_delivery(
        JsonlStandDownVerdictLedger(_GREEN_READY_DIR), 3181, _actuator,
    )
    assert delivered is True
    rows = _stand_down_rows(mem_fs, _GREEN_READY_DIR)
    # Exactly one row, keyed to the PR, left by the actuator (no backfill).
    assert [r for r in rows if r["pr"] == 3181]
    assert len(rows) == 1
    assert rows[0]["head_sha"] == "deadbeef"


def test_confirm_handoff_delivery_backfills_when_actuator_leaves_no_row(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 2 — an actuator that returns without persisting the keyed row
    triggers direct persistence of the pr_number-keyed row at zero SDK cost, and
    the bind reports False (not 'delivered') because it backfilled the artifact."""
    calls: list[bool] = []

    def _actuator() -> bool:
        # A real ``deliver_handoff`` records only the escalation-ledger standdown,
        # never a ``stand_down_verdicts.jsonl`` verdict row — so the re-read misses.
        calls.append(True)
        return True

    delivered = audit_checks_phantom.confirm_handoff_delivery(
        JsonlStandDownVerdictLedger(_GREEN_READY_DIR), 3181, _actuator, now="t-backfill",
    )
    # The actuator ran, but the bind did not report delivered — it backfilled.
    assert calls == [True]
    assert delivered is False
    rows = _stand_down_rows(mem_fs, _GREEN_READY_DIR)
    assert len(rows) == 1
    assert rows[0]["pr"] == 3181
    assert rows[0]["recorded_at"] == "t-backfill"
    # The arm carries no lap head, so the directly-persisted row's head is empty
    # (inert for ``find_stand_down`` yet still a counted owned terminal act).
    assert rows[0]["head_sha"] == ""


def test_confirm_handoff_delivery_not_invoked_for_non_ready_pr(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 3 — the firing predicate is unchanged: a CI-pending / non-merge-
    ready owned PR is not returned by ``green_ready_handoff_prs``, so the bind is
    never invoked and no ``stand_down_verdicts.jsonl`` row is produced."""

    def _boom() -> bool:
        raise AssertionError("actuator must not fire for a non-ready PR")

    prs = green_ready_handoff_prs(
        [3181],
        enabled=True,
        is_green_ready=lambda n: False,
        has_standdown=lambda n: False,
    )
    assert prs == []
    # Mirror the dispatch loop: nothing to iterate ⇒ the bind never runs.
    for pr_num in prs:  # pragma: no cover - empty by construction
        audit_checks_phantom.confirm_handoff_delivery(JsonlStandDownVerdictLedger(_GREEN_READY_DIR), pr_num, _boom)
    assert str(_GREEN_READY_DIR / "stand_down_verdicts.jsonl") not in mem_fs
    assert _stand_down_rows(mem_fs, _GREEN_READY_DIR) == []


def test_confirm_handoff_delivery_ignores_actuator_return_value(
    mem_fs: dict[str, str],
) -> None:
    """The re-read, not the actuator's word, is the ground truth: an actuator that
    returns ``False`` (an idempotent no-op) still yields a backfilled keyed row."""
    delivered = audit_checks_phantom.confirm_handoff_delivery(
        JsonlStandDownVerdictLedger(_GREEN_READY_DIR), 3181, lambda: False, now="t",
    )
    assert delivered is False
    rows = _stand_down_rows(mem_fs, _GREEN_READY_DIR)
    assert len(rows) == 1
    assert rows[0]["pr"] == 3181


def test_persist_handoff_stand_down_row_skips_non_positive_pr(
    mem_fs: dict[str, str],
) -> None:
    """A non-positive PR carries no key to match on, so nothing is written."""
    audit_checks_phantom._persist_handoff_stand_down_row(0, JsonlStandDownVerdictLedger(_GREEN_READY_DIR))
    audit_checks_phantom._persist_handoff_stand_down_row(-3, JsonlStandDownVerdictLedger(_GREEN_READY_DIR))
    assert str(_GREEN_READY_DIR / "stand_down_verdicts.jsonl") not in mem_fs


def test_pr_keyed_stand_down_row_count_reader(mem_fs: dict[str, str]) -> None:
    """The re-read reader counts verdict rows by PR head-agnostically, ignores a
    release row / other PR, tolerates a corrupted tail, and rejects a bool PR."""
    path = str(_GREEN_READY_DIR / "stand_down_verdicts.jsonl")
    mem_fs[path] = "\n".join([
        "",  # blank line (skipped)
        "not json",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"pr": True, "head_sha": "x", "recorded_at": "t"}),
        json.dumps({"pr": 9999, "released_at": "t"}),  # release row (no verdict)
        json.dumps({"pr": 3181, "head_sha": "", "recorded_at": "t"}),
        json.dumps({"pr": 3181, "head_sha": "abc", "recorded_at": "t2"}),
    ]) + "\n"
    backend = JsonlStandDownVerdictLedger(_GREEN_READY_DIR)
    assert audit_checks_phantom._pr_keyed_stand_down_row_count(3181, backend) == 2
    # A PR with only a release row (no ``recorded_at`` verdict) counts zero.
    assert audit_checks_phantom._pr_keyed_stand_down_row_count(9999, backend) == 0
    assert audit_checks_phantom._pr_keyed_stand_down_row_count(3182, backend) == 0
    assert audit_checks_phantom._pr_keyed_stand_down_row_count(0, backend) == 0


def test_pr_keyed_stand_down_row_count_missing_file(
    mem_fs: dict[str, str],
) -> None:
    """An absent ledger reads as zero rows."""
    backend = JsonlStandDownVerdictLedger(_GREEN_READY_DIR)
    assert audit_checks_phantom._pr_keyed_stand_down_row_count(3181, backend) == 0


def test_pr_keyed_stand_down_row_count_oserror(mem_fs: dict[str, str]) -> None:
    """An ``OSError`` while reading the ledger degrades to zero rows (no crash)."""
    path = str(_GREEN_READY_DIR / "stand_down_verdicts.jsonl")
    mem_fs[path] = "content"  # exists so path.exists() is True
    backend = JsonlStandDownVerdictLedger(_GREEN_READY_DIR)
    with patch("builtins.open", side_effect=OSError("disk error")):
        assert audit_checks_phantom._pr_keyed_stand_down_row_count(3181, backend) == 0


def _is_green_ready_call(node: ast.AST) -> bool:
    """True for a call to ``*.green_ready_handoff_prs(...)``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "green_ready_handoff_prs"
    )


def _is_green_ready_flag_test(test: ast.expr) -> bool:
    """True for the guard ``self.config.green_ready_handoff_enabled``."""
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "green_ready_handoff_enabled"
        and isinstance(test.value, ast.Attribute)
        and test.value.attr == "config"
    )


def test_green_ready_handoff_dispatched_flag_gated_in_audit_self_model() -> None:
    """Rule 22 is dispatched from ``loop/self_audit.py::audit_self_model`` and every
    dispatch is gated by ``agent.config.green_ready_handoff_enabled``."""
    audit_src = (
        Path(__file__).resolve().parent.parent
        / "maestro" / "loop" / "self_audit.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(audit_src)

    audit_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "audit_self_model"
        ),
        None,
    )
    assert audit_fn is not None, "audit_self_model not found in self_audit.py"

    total_calls = sum(
        1 for n in ast.walk(audit_fn) if _is_green_ready_call(n)
    )
    flag_guards = [
        n
        for n in ast.walk(audit_fn)
        if isinstance(n, ast.If) and _is_green_ready_flag_test(n.test)
    ]
    gated_calls = sum(
        1
        for guard in flag_guards
        for n in ast.walk(guard)
        if _is_green_ready_call(n)
    )

    assert total_calls >= 1, (
        "green_ready_handoff_prs is registered but never dispatched in "
        "_audit_self_model — the green_ready_handoff_enabled flag is inert"
    )
    assert gated_calls == total_calls, (
        "every green_ready_handoff_prs dispatch in _audit_self_model must "
        "be guarded by `if self.config.green_ready_handoff_enabled:`"
    )


# -- _read_plan_statuses helper --------------------------------------------


def test_read_plan_statuses_maps_id_to_status(
    mem_fs: dict[str, str],
) -> None:
    """A fresh load maps each plan_id to its last-written status."""
    _seed_prune_plans(mem_fs, [
        {"plan_id": "aaaaaaaaaaaa", "status": "pending_review"},
        {"plan_id": "bbbbbbbbbbbb", "status": "obsolete"},
        # Duplicate id — the later line wins.
        {"plan_id": "aaaaaaaaaaaa", "status": "obsolete"},
    ])
    statuses = audit_checks_phantom._read_plan_statuses(_PRUNE_DIR)
    assert statuses == {
        "aaaaaaaaaaaa": "obsolete",
        "bbbbbbbbbbbb": "obsolete",
    }


def test_read_plan_statuses_missing_file_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """No plan store on disk yields an empty map, not a crash."""
    assert audit_checks_phantom._read_plan_statuses(_PRUNE_DIR) == {}


def test_read_plan_statuses_tolerates_corrupt_rows(
    mem_fs: dict[str, str],
) -> None:
    """Blank lines, non-JSON, non-dict, and id/status-less rows are skipped."""
    mem_fs[str(_PRUNE_DIR / "evolution_plans.jsonl")] = (
        "\n"
        "not json\n"
        "[1, 2, 3]\n"
        + json.dumps({"status": "obsolete"}) + "\n"
        + json.dumps({"plan_id": "cccccccccccc"}) + "\n"
        + json.dumps({"plan_id": "", "status": "obsolete"}) + "\n"
        + json.dumps({"plan_id": "dddddddddddd", "status": "obsolete"}) + "\n"
    )
    assert audit_checks_phantom._read_plan_statuses(_PRUNE_DIR) == {
        "dddddddddddd": "obsolete",
    }


def test_read_plan_statuses_oserror_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """An OSError opening the plan store degrades to an empty map."""
    _seed_prune_plans(mem_fs, [
        {"plan_id": _PRUNE_PLAN_ID, "status": "obsolete"},
    ])
    with patch("builtins.open", side_effect=OSError("boom")):
        assert audit_checks_phantom._read_plan_statuses(_PRUNE_DIR) == {}

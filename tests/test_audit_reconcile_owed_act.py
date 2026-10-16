"""Rule 21's owed-act binding — the ``gh pr close`` clause (137, step 2).

Covers the ``owed_act`` seam :func:`maestro.audit_checks_phantom.check_reconcile_near_miss`
gained: while the PR reads ``CODE_OWED`` the finding demands the reconcile alone,
because a failing gate the loop clears itself is not a co-equal reason to close.
Every other reading — and the flag-off ``None`` seam — must reproduce today's
statement byte-for-byte.

The cases live here rather than in ``tests/test_audit_checks.py``, which is
held below ``SPLIT_BUDGET`` by ``tests/test_source_file_size.py``; the same split
``tests/test_audit_banked_refutation_prefixes.py`` uses. Seams are bare inline
lambdas; no agent, no host, no I/O.
"""

from __future__ import annotations

from maestro.audit_checks_phantom import (
    check_reconcile_near_miss,
)
from maestro.memory import TaskRecord
from maestro.pr_owed_act import OwedAct

_CLOSE_CLAUSE = "or gh pr close #4242"


def _partial_task() -> TaskRecord:
    """One ``partial`` remediation record citing PR #4242, no landing SHA."""
    return TaskRecord(
        task_id="t1",
        goal="remediate PR #4242",
        outcome="partial",
        result_summary="Pushed the fix to #4242 — awaiting review/merge.",
        plan=[],
        errors=[],
        duration_secs=0.0,
        timestamp="2026-09-22T00:00:00+00:00",
    )


def _findings(owed_act: object) -> list[str]:
    """Run rule 21 over one near-miss task with *owed_act* as its seam."""
    findings = check_reconcile_near_miss(
        [_partial_task()],
        enabled=True,
        is_settled=lambda _n: False,
        commits_behind=lambda _n: 3,
        is_ancestor=lambda _s: False,
        owed_act=owed_act,  # type: ignore[arg-type]
    )
    return [f.statement for f in findings]


def test_code_owed_suppresses_the_close_clause() -> None:
    """A ``CODE_OWED`` reading drops ``or gh pr close #N`` from the demand."""
    statements = _findings(lambda _n: OwedAct.CODE_OWED)
    assert len(statements) == 1
    assert _CLOSE_CLAUSE not in statements[0]
    assert statements[0].startswith("Reconcile+push #4242: it is 3 ")


def test_code_owed_keeps_the_rest_of_the_statement_verbatim() -> None:
    """Only the demand changes — the measured evidence is unchanged."""
    bound = _findings(lambda _n: OwedAct.CODE_OWED)[0]
    unbound = _findings(None)[0]
    assert bound == unbound.replace(f"#4242 {_CLOSE_CLAUSE}", "#4242")


def test_handoff_ready_reproduces_todays_statement() -> None:
    """A non-code-owed act leaves the close clause standing."""
    assert _CLOSE_CLAUSE in _findings(lambda _n: OwedAct.HANDOFF_READY)[0]


def test_wait_ci_reproduces_todays_statement() -> None:
    """``WAIT_CI`` is not code-owed — the clause stands."""
    assert _CLOSE_CLAUSE in _findings(lambda _n: OwedAct.WAIT_CI)[0]


def test_none_reading_reproduces_todays_statement() -> None:
    """An unmeasurable reading never suppresses the clause."""
    assert _CLOSE_CLAUSE in _findings(lambda _n: None)[0]


def test_missing_seam_reproduces_todays_statement() -> None:
    """The flag-off path (``None`` seam) is byte-identical to today."""
    assert _CLOSE_CLAUSE in _findings(None)[0]


def test_seam_consulted_at_most_once_per_pr() -> None:
    """The scan's ``emitted`` dedupe is what bounds the seam to one call.

    The seam is consulted only on the path that goes on to ``emitted.add(pr)``,
    and every later citation of that PR is skipped ahead of it — so the act
    needs no memo of its own.
    """
    calls: list[int] = []

    def _seam(pr_num: int) -> OwedAct:
        calls.append(pr_num)
        return OwedAct.CODE_OWED

    tasks = [_partial_task(), _partial_task()]
    tasks[1].task_id = "t2"
    check_reconcile_near_miss(
        tasks,
        enabled=True,
        is_settled=lambda _n: False,
        commits_behind=lambda _n: 3,
        is_ancestor=lambda _s: False,
        owed_act=_seam,
    )
    assert calls == [4242]


def _offers(owed_act: object, *, behind: int = 3) -> list[int]:
    """Run rule 21 with a recording ``on_disposal_offer`` callback."""
    offered: list[int] = []
    check_reconcile_near_miss(
        [_partial_task(), _partial_task()],
        enabled=True,
        is_settled=lambda _n: False,
        commits_behind=lambda _n: behind,
        is_ancestor=lambda _s: False,
        owed_act=owed_act,  # type: ignore[arg-type]
        on_disposal_offer=offered.append,
    )
    return offered


def test_disposal_offer_fires_once_per_emitted_pr() -> None:
    """A close-clause finding records its PR exactly once across citations."""
    assert _offers(None) == [4242]
    assert _offers(lambda _n: OwedAct.WAIT_CI) == [4242]


def test_disposal_offer_skipped_when_clause_dropped() -> None:
    """A ``CODE_OWED`` reading drops the clause, so no offer is recorded."""
    assert _offers(lambda _n: OwedAct.CODE_OWED) == []


def test_disposal_offer_skipped_when_no_finding() -> None:
    """Out-of-band distance emits no finding and records nothing."""
    assert _offers(None, behind=0) == []


def test_disposal_offer_callback_leaves_statement_unchanged() -> None:
    """The callback is measurement only — the finding text is identical."""
    with_cb = check_reconcile_near_miss(
        [_partial_task()],
        enabled=True,
        is_settled=lambda _n: False,
        commits_behind=lambda _n: 3,
        is_ancestor=lambda _s: False,
        on_disposal_offer=lambda _n: None,
    )
    assert [f.statement for f in with_cb] == _findings(None)

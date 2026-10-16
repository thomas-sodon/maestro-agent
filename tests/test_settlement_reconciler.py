"""Tests for the proposal-097 Phase-1 standing settlement reconciler.

``reconcile_settled_verdicts`` reads the trailing remediation verdicts, consults
the injected ``is_pr_settled`` chokepoint once per distinct PR, and records every
settled (merged *or* closed) PR to the durable ``settled_verdicts.jsonl`` ledger;
``load_settled_prs`` reads that ledger back. Both are pure, zero-SDK, and touch
only the file system, so every test injects a ``MergeTruth`` fake (no real git,
no subprocess, no network, no sleep) and asserts simple equalities over the
in-memory ``mem_fs`` filesystem.
"""

import ast
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from maestro.audit_checks import (
    AuditFinding,
)
from maestro.audit_checks_phantom import (
    _UNCONFIRMED_LANDING_SIDECAR,
    JsonUnconfirmedOwnLandingStore,
    check_unconfirmed_own_landing,
)
from maestro.audit_checks_settlement import (
    _SETTLED_VERDICTS_LEDGER,
    JsonlSettledVerdictsLedger,
)
from maestro.audit_checks_settlement import (
    load_settled_prs as _load_settled_prs_impl,
)
from maestro.audit_checks_settlement import (
    reconcile_settled_verdicts as _reconcile_settled_verdicts_impl,
)
from maestro.audit_checks_settlement import (
    upsert_settled_pr as _upsert_settled_pr_impl,
)
from maestro.memory import PlanStep, TaskRecord
from maestro.pr_facts import MergeTruth

_DATA_DIR = Path("/fake/data")


# The settled-verdicts pre-filter moved behind a ``SettledVerdictsBackend``
# (proposal 106): ``load_settled_prs`` / ``reconcile_settled_verdicts`` /
# ``upsert_settled_pr`` now take a backend rather than a ``data_dir``. These
# thin wrappers bind the production-default :class:`JsonlSettledVerdictsLedger`
# over the shared test ``data_dir``, so the ``mem_fs`` test bodies below keep
# exercising the *real* functions through their flag-off JSONL path — the exact
# behaviour these tests pin — without threading a backend at every call site.


def load_settled_prs(data_dir: Path) -> frozenset[int]:
    """Read the effective settled set via the default JSONL backend."""
    return _load_settled_prs_impl(JsonlSettledVerdictsLedger(data_dir))


def reconcile_settled_verdicts(
    data_dir: Path,
    n: int,
    iteration: int,
    *,
    is_pr_settled: Callable[[int], MergeTruth],
) -> frozenset[int]:
    """Reconcile trailing verdicts, binding the default JSONL backend."""
    return _reconcile_settled_verdicts_impl(
        data_dir, n, iteration,
        is_pr_settled=is_pr_settled,
        backend=JsonlSettledVerdictsLedger(data_dir),
    )


def upsert_settled_pr(
    data_dir: Path,
    pr_number: int,
    truth: MergeTruth,
    iteration: int,
) -> bool:
    """Upsert one settlement observation via the default JSONL backend."""
    return _upsert_settled_pr_impl(
        JsonlSettledVerdictsLedger(data_dir), pr_number, truth, iteration,
    )


def _seed_history(
    mem_fs: dict[str, str], tasks: list[dict[str, object]],
) -> None:
    """Write *tasks* as ``task_history.jsonl`` lines into the in-memory fs."""
    mem_fs[str(_DATA_DIR / "task_history.jsonl")] = "".join(
        json.dumps(t) + "\n" for t in tasks
    )


def _merge_truth(pr: int, *, settled: bool, landed: bool) -> MergeTruth:
    """Build a ``MergeTruth`` with a settled/landed shape for the fake oracle."""
    if landed:
        state = "MERGED"
    elif settled:
        state = "CLOSED"
    else:
        state = "OPEN"
    return MergeTruth(
        pr_number=pr,
        settled=settled,
        state=state,
        branch_landed=landed,
        source="host_api",
        observed_at=0.0,
    )


def _ledger_rows(mem_fs: dict[str, str]) -> list[dict[str, object]]:
    """Parse the ledger rows written under ``settled_verdicts.jsonl``."""
    raw = mem_fs.get(str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER), "")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def test_reconcile_returns_merged_and_closed_excludes_open(
    mem_fs: dict[str, str],
) -> None:
    """A merged PR and a closed PR land in the returned set and the ledger with
    the right ``kind`` and citing ``verdict_task_id``; an open PR does not."""
    _seed_history(mem_fs, [
        {"task_id": "t1", "goal": "Remediate PR #100: Fix widget"},
        {"task_id": "t2", "goal": "Remediate PR #200: Fix gadget"},
        {"task_id": "t3", "goal": "Remediate PR #300: Fix gizmo"},
    ])
    truths = {
        100: _merge_truth(100, settled=True, landed=True),
        200: _merge_truth(200, settled=True, landed=False),
        300: _merge_truth(300, settled=False, landed=False),
    }

    result = reconcile_settled_verdicts(
        _DATA_DIR, 20, 42, is_pr_settled=lambda n: truths[n],
    )

    assert result == frozenset({100, 200})
    rows = {row["pr_number"]: row for row in _ledger_rows(mem_fs)}
    assert set(rows) == {100, 200}
    assert rows[100]["kind"] == "merged"
    assert rows[100]["verdict_task_id"] == "t1"
    assert rows[100]["settled_at_iteration"] == 42
    assert rows[200]["kind"] == "closed"
    assert rows[200]["verdict_task_id"] == "t2"


def test_reconcile_idempotent_over_same_tail(
    mem_fs: dict[str, str],
) -> None:
    """A second reconcile over the same tail appends no duplicate rows."""
    _seed_history(mem_fs, [
        {"task_id": "t1", "goal": "Remediate PR #100: Fix widget"},
    ])
    truths = {100: _merge_truth(100, settled=True, landed=True)}

    first = reconcile_settled_verdicts(
        _DATA_DIR, 20, 1, is_pr_settled=lambda n: truths[n],
    )
    second = reconcile_settled_verdicts(
        _DATA_DIR, 20, 2, is_pr_settled=lambda n: truths[n],
    )

    assert first == frozenset({100})
    assert second == frozenset({100})
    assert len(_ledger_rows(mem_fs)) == 1


def test_reconcile_scrolled_out_pr_stays_prefiltered_without_reappend(
    mem_fs: dict[str, str],
) -> None:
    """A settled PR that scrolls out of the trailing window stays in the returned
    pre-filter — from the ledger, not the window — and is not re-appended.

    This is the durable contract at the heart of the increment: before it, the
    returned set was built purely from the PRs matched *in this cycle's window*,
    so a settled PR dropped out of the pre-filter the moment its ``Remediate PR
    #N`` record scrolled off the tail, and the phantom sensors keyed on summary
    text re-fired for as long as the claiming record survived.
    """
    _seed_history(mem_fs, [
        {"task_id": "t1", "goal": "Remediate PR #100: Fix widget"},
    ])
    reconcile_settled_verdicts(
        _DATA_DIR, 20, 1,
        is_pr_settled=lambda n: _merge_truth(100, settled=True, landed=True),
    )

    # The window now holds a different, still-open PR — #100 is gone.
    _seed_history(mem_fs, [
        {"task_id": "t2", "goal": "Remediate PR #300: Fix gizmo"},
    ])
    result = reconcile_settled_verdicts(
        _DATA_DIR, 20, 2,
        is_pr_settled=lambda n: _merge_truth(300, settled=False, landed=False),
    )

    assert result == frozenset({100})
    rows = _ledger_rows(mem_fs)
    assert len(rows) == 1
    assert rows[0]["pr_number"] == 100


def test_reconcile_ledger_pr_absent_from_window_stays_prefiltered(
    mem_fs: dict[str, str],
) -> None:
    """A ledger-recorded PR absent from the window is returned with no chokepoint
    call — the pre-filter is ledger-backed, not window-bound.

    This also pins the deliberate asymmetry the reconciler's docstring names:
    because the chokepoint is consulted *only* for PRs a trailing ``Remediate PR
    #N`` record cites, a ledger-only PR is never re-verified and so can never be
    retracted while it stays out of the window. Retraction is window-bound where
    the pre-filter is not — the price of spending zero chokepoint calls on PRs
    the window does not name.
    """
    mem_fs[str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER)] = (
        '{"pr_number": 100, "settled_at_iteration": 1, "kind": "closed"}\n'
    )
    _seed_history(mem_fs, [
        {"task_id": "t9", "goal": "Investigate the widget subsystem"},
    ])

    def oracle(n: int) -> MergeTruth:  # pragma: no cover - must never run
        raise AssertionError("chokepoint consulted for a ledger-only PR")

    result = reconcile_settled_verdicts(_DATA_DIR, 20, 5, is_pr_settled=oracle)

    assert result == frozenset({100})
    assert len(_ledger_rows(mem_fs)) == 1


def test_reconcile_retracts_recorded_pr_the_chokepoint_now_calls_unsettled(
    mem_fs: dict[str, str],
) -> None:
    """A recorded PR the chokepoint now reports unsettled — a closed PR reopened
    — earns one retraction row and leaves the returned set."""
    mem_fs[str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER)] = (
        '{"pr_number": 100, "settled_at_iteration": 1, "kind": "closed"}\n'
    )
    _seed_history(mem_fs, [
        {"task_id": "t2", "goal": "Remediate PR #100: Fix widget"},
    ])

    result = reconcile_settled_verdicts(
        _DATA_DIR, 20, 7,
        is_pr_settled=lambda n: _merge_truth(100, settled=False, landed=False),
    )

    assert result == frozenset()
    rows = _ledger_rows(mem_fs)
    assert len(rows) == 2
    assert rows[1]["pr_number"] == 100
    assert rows[1]["retracted_at_iteration"] == 7
    assert load_settled_prs(_DATA_DIR) == frozenset()


def test_reconcile_unsettled_unrecorded_pr_writes_no_retraction(
    mem_fs: dict[str, str],
) -> None:
    """An unsettled PR the ledger never recorded is simply skipped — a retraction
    row is only ever written to supersede an existing settle row."""
    _seed_history(mem_fs, [
        {"task_id": "t1", "goal": "Remediate PR #300: Fix gizmo"},
    ])

    result = reconcile_settled_verdicts(
        _DATA_DIR, 20, 3,
        is_pr_settled=lambda n: _merge_truth(300, settled=False, landed=False),
    )

    assert result == frozenset()
    assert _ledger_rows(mem_fs) == []


def test_reconcile_repeated_unsettled_records_write_one_retraction(
    mem_fs: dict[str, str],
) -> None:
    """Two records citing the same retracted PR append exactly one retraction
    row — the ``pr_number not in retracted`` guard holds within a cycle."""
    mem_fs[str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER)] = (
        '{"pr_number": 100, "settled_at_iteration": 1, "kind": "closed"}\n'
    )
    _seed_history(mem_fs, [
        {"task_id": "t1", "goal": "Remediate PR #100: First lap"},
        {"task_id": "t2", "goal": "Remediate PR #100: Second lap"},
    ])

    result = reconcile_settled_verdicts(
        _DATA_DIR, 20, 4,
        is_pr_settled=lambda n: _merge_truth(100, settled=False, landed=False),
    )

    assert result == frozenset()
    rows = _ledger_rows(mem_fs)
    assert len(rows) == 2
    assert rows[1]["retracted_at_iteration"] == 4


def test_reconcile_re_settle_after_retraction_appends_fresh_settle_row(
    mem_fs: dict[str, str],
) -> None:
    """A PR that settles, retracts, then settles again earns a *fresh* settle row
    and re-enters the returned set rather than staying permanently retracted."""
    mem_fs[str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER)] = (
        '{"pr_number": 100, "settled_at_iteration": 1, "kind": "closed"}\n'
        '{"pr_number": 100, "retracted_at_iteration": 2}\n'
    )
    _seed_history(mem_fs, [
        {"task_id": "t3", "goal": "Remediate PR #100: Fix widget"},
    ])

    result = reconcile_settled_verdicts(
        _DATA_DIR, 20, 9,
        is_pr_settled=lambda n: _merge_truth(100, settled=True, landed=True),
    )

    assert result == frozenset({100})
    rows = _ledger_rows(mem_fs)
    assert len(rows) == 3
    assert rows[2]["pr_number"] == 100
    assert rows[2]["settled_at_iteration"] == 9
    assert rows[2]["kind"] == "merged"
    assert load_settled_prs(_DATA_DIR) == frozenset({100})


def test_reconcile_second_pass_preserves_prior_rows(
    mem_fs: dict[str, str],
) -> None:
    """A later reconcile recording a new distinct PR appends to — rather than
    truncates — the existing ledger, so both rows survive.

    This guards the durable-append semantics at the heart of the ledger: a
    regression from ``open(path, "a")`` to ``open(path, "w")`` would silently
    discard the first-settled PR on the second write, and this test would fail.
    The idempotent and scrolled-out passes above never write on their second
    call, so only a genuine second append into a non-empty file exercises it.
    """
    _seed_history(mem_fs, [
        {"task_id": "t1", "goal": "Remediate PR #100: Fix widget"},
    ])
    first = reconcile_settled_verdicts(
        _DATA_DIR, 20, 1,
        is_pr_settled=lambda n: _merge_truth(100, settled=True, landed=True),
    )

    # A fresh tail cites a different settled PR; #100 has scrolled out of view,
    # so the second pass writes exactly one *new* row into the populated ledger.
    _seed_history(mem_fs, [
        {"task_id": "t2", "goal": "Remediate PR #200: Fix gadget"},
    ])
    second = reconcile_settled_verdicts(
        _DATA_DIR, 20, 2,
        is_pr_settled=lambda n: _merge_truth(200, settled=True, landed=False),
    )

    assert first == frozenset({100})
    # #100 rides the durable ledger even though it has left the window.
    assert second == frozenset({100, 200})
    rows = {row["pr_number"]: row for row in _ledger_rows(mem_fs)}
    assert len(_ledger_rows(mem_fs)) == 2
    assert set(rows) == {100, 200}
    assert rows[100]["kind"] == "merged"
    assert rows[200]["kind"] == "closed"
    assert load_settled_prs(_DATA_DIR) == frozenset({100, 200})


def test_reconcile_consults_chokepoint_once_per_distinct_pr(
    mem_fs: dict[str, str],
) -> None:
    """Two records citing the same PR consult the chokepoint once and write one
    ledger row."""
    _seed_history(mem_fs, [
        {"task_id": "t1", "goal": "Remediate PR #100: First lap"},
        {"task_id": "t2", "goal": "Remediate PR #100: Second lap"},
    ])
    calls: list[int] = []

    def oracle(n: int) -> MergeTruth:
        calls.append(n)
        return _merge_truth(100, settled=True, landed=True)

    result = reconcile_settled_verdicts(
        _DATA_DIR, 20, 1, is_pr_settled=oracle,
    )

    assert result == frozenset({100})
    assert calls == [100]
    rows = _ledger_rows(mem_fs)
    assert len(rows) == 1
    # The first citing record wins the recorded ``verdict_task_id``.
    assert rows[0]["verdict_task_id"] == "t1"


def test_reconcile_records_none_verdict_task_id_when_absent(
    mem_fs: dict[str, str],
) -> None:
    """A settled record without a ``task_id`` records ``verdict_task_id`` None."""
    _seed_history(mem_fs, [
        {"goal": "Remediate PR #100: Fix widget"},
    ])

    reconcile_settled_verdicts(
        _DATA_DIR, 20, 1,
        is_pr_settled=lambda n: _merge_truth(100, settled=True, landed=True),
    )

    rows = _ledger_rows(mem_fs)
    assert len(rows) == 1
    assert rows[0]["verdict_task_id"] is None


def test_reconcile_uses_title_fallback_when_goal_absent(
    mem_fs: dict[str, str],
) -> None:
    """A record whose ``goal`` is absent falls back to ``title``."""
    _seed_history(mem_fs, [
        {"task_id": "t1", "title": "Remediate PR #400: Fix widget"},
    ])

    result = reconcile_settled_verdicts(
        _DATA_DIR, 20, 1,
        is_pr_settled=lambda n: _merge_truth(400, settled=True, landed=False),
    )

    assert result == frozenset({400})
    assert _ledger_rows(mem_fs)[0]["kind"] == "closed"


def test_reconcile_skips_non_remediation_and_titleless_records(
    mem_fs: dict[str, str],
) -> None:
    """A non-remediation goal and a record with neither goal nor title are both
    skipped, so nothing is recorded and the chokepoint is never consulted."""
    _seed_history(mem_fs, [
        {"task_id": "t1", "goal": "Investigate the widget subsystem"},
        {"task_id": "t2"},
        {"task_id": "t3", "goal": ""},
    ])

    def oracle(n: int) -> MergeTruth:  # pragma: no cover - must never run
        raise AssertionError("chokepoint consulted for a non-remediation record")

    result = reconcile_settled_verdicts(_DATA_DIR, 20, 1, is_pr_settled=oracle)

    assert result == frozenset()
    assert _ledger_rows(mem_fs) == []


def test_reconcile_non_positive_n_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """``n <= 0`` short-circuits: empty set, no ledger, no chokepoint call."""
    _seed_history(mem_fs, [
        {"task_id": "t1", "goal": "Remediate PR #100: Fix widget"},
    ])

    def oracle(n: int) -> MergeTruth:  # pragma: no cover - must never run
        raise AssertionError("chokepoint consulted despite n <= 0")

    result = reconcile_settled_verdicts(_DATA_DIR, 0, 1, is_pr_settled=oracle)

    assert result == frozenset()
    assert str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER) not in mem_fs


def test_load_settled_prs_absent_ledger_is_empty(
    mem_fs: dict[str, str],
) -> None:
    """An absent ledger reads cleanly as an empty set."""
    assert load_settled_prs(_DATA_DIR) == frozenset()


def test_load_settled_prs_round_trips_recorded_set(
    mem_fs: dict[str, str],
) -> None:
    """``load_settled_prs`` returns exactly the PRs a reconcile recorded."""
    _seed_history(mem_fs, [
        {"task_id": "t1", "goal": "Remediate PR #100: Fix widget"},
        {"task_id": "t2", "goal": "Remediate PR #200: Fix gadget"},
    ])
    truths = {
        100: _merge_truth(100, settled=True, landed=True),
        200: _merge_truth(200, settled=True, landed=False),
    }
    reconcile_settled_verdicts(
        _DATA_DIR, 20, 1, is_pr_settled=lambda n: truths[n],
    )

    assert load_settled_prs(_DATA_DIR) == frozenset({100, 200})


def test_load_settled_prs_tolerates_corrupt_and_typed_rows(
    mem_fs: dict[str, str],
) -> None:
    """Blank lines, malformed JSON, non-dict rows, and non-int / ``bool``
    ``pr_number`` values are skipped; only integer PR numbers contribute."""
    mem_fs[str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER)] = (
        "\n"                                    # blank line
        "{not valid json}\n"                    # malformed
        "[1, 2, 3]\n"                           # non-dict row
        '{"pr_number": true}\n'                 # bool rejected (subclasses int)
        '{"pr_number": "500"}\n'                # string rejected
        '{"settled_at_iteration": 3}\n'         # missing pr_number
        '{"pr_number": 100, "kind": "merged"}\n'
    )

    assert load_settled_prs(_DATA_DIR) == frozenset({100})


def test_load_settled_prs_collapses_settle_retract_settle_to_settled(
    mem_fs: dict[str, str],
) -> None:
    """Last row wins: a settle → retract → settle chain reads as settled, while a
    settle → retract chain reads as absent."""
    mem_fs[str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER)] = (
        '{"pr_number": 100, "settled_at_iteration": 1, "kind": "merged"}\n'
        '{"pr_number": 100, "retracted_at_iteration": 2}\n'
        '{"pr_number": 100, "settled_at_iteration": 3, "kind": "merged"}\n'
        '{"pr_number": 200, "settled_at_iteration": 1, "kind": "closed"}\n'
        '{"pr_number": 200, "retracted_at_iteration": 2}\n'
    )

    assert load_settled_prs(_DATA_DIR) == frozenset({100})


def test_load_settled_prs_reads_legacy_row_without_retraction_key_as_settled(
    mem_fs: dict[str, str],
) -> None:
    """A ledger row written before the retraction shape existed — no
    ``retracted_at_iteration`` key at all — still reads as settled, so the new
    reader is backward-compatible with every already-written row."""
    mem_fs[str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER)] = (
        '{"pr_number": 100, "settled_at_iteration": 1, "kind": "merged",'
        ' "verdict_task_id": "t1"}\n'
        '{"pr_number": 200, "settled_at_iteration": 2, "kind": "closed"}\n'
    )

    assert load_settled_prs(_DATA_DIR) == frozenset({100, 200})


def test_load_settled_prs_damaged_retraction_row_is_skipped_not_a_settle(
    mem_fs: dict[str, str],
) -> None:
    """A row carrying ``retracted_at_iteration`` with a non-integer value is a
    *damaged retraction*, so it is skipped and leaves prior state alone —
    it is never re-read as a settle.

    The load-bearing case is PR 100: a valid retraction already voided it, and a
    following corrupt ``true`` row must not resurrect it. Reading a damaged
    retraction as a settle would silently darken both phantom rules for that
    PR, which is the failure direction that actually hurts. PRs 200/300 cover
    the no-prior-state case: a damaged retraction alone leaves the PR absent
    rather than conjuring a settlement out of a corrupt row.
    """
    mem_fs[str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER)] = (
        '{"pr_number": 100, "settled_at_iteration": 1, "kind": "merged"}\n'
        '{"pr_number": 100, "retracted_at_iteration": 2}\n'
        '{"pr_number": 100, "retracted_at_iteration": true}\n'
        '{"pr_number": 200, "retracted_at_iteration": true}\n'
        '{"pr_number": 300, "retracted_at_iteration": "2"}\n'
    )

    assert load_settled_prs(_DATA_DIR) == frozenset()


def test_load_settled_prs_damaged_retraction_leaves_a_settled_pr_settled(
    mem_fs: dict[str, str],
) -> None:
    """Skipping a damaged retraction is state-preserving in both directions: a
    settled PR whose only following row is corrupt stays settled, because the
    row is dropped rather than applied as a retraction."""
    mem_fs[str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER)] = (
        '{"pr_number": 100, "settled_at_iteration": 1, "kind": "merged"}\n'
        '{"pr_number": 100, "retracted_at_iteration": "nope"}\n'
    )

    assert load_settled_prs(_DATA_DIR) == frozenset({100})


def test_load_settled_prs_unreadable_ledger_is_empty(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``OSError`` reading the ledger degrades to an empty set."""
    mem_fs[str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER)] = '{"pr_number": 100}\n'

    def boom(self: Path, encoding: str | None = None,
             errors: str | None = None) -> str:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "read_text", boom)

    assert load_settled_prs(_DATA_DIR) == frozenset()


# ── Proposal-103 step-6 observation upsert ──────────────────────────────────
#
# ``upsert_settled_pr`` records *any* settlement the caller already observed into
# the same durable ledger ``reconcile_settled_verdicts`` writes, so the
# ``load_settled_prs`` pre-filter no longer depends on a ``Remediate PR #N``
# record surviving the rolling window. It is settled-gated, idempotent, and
# writes the identical settle-row shape, so both writers stay collapsible
# last-row-wins.


def test_upsert_never_remediated_pr_writes_one_settle_row(
    mem_fs: dict[str, str],
) -> None:
    """A never-remediated settled PR the reconciler window never named enters the
    ledger with exactly one settle row and ``load_settled_prs`` returns it."""
    wrote = upsert_settled_pr(
        _DATA_DIR, 500, _merge_truth(500, settled=True, landed=True), 42,
    )

    assert wrote is True
    rows = _ledger_rows(mem_fs)
    assert len(rows) == 1
    assert rows[0]["pr_number"] == 500
    assert rows[0]["kind"] == "merged"
    assert rows[0]["settled_at_iteration"] == 42
    assert rows[0]["verdict_task_id"] is None
    assert load_settled_prs(_DATA_DIR) == frozenset({500})


def test_upsert_second_call_is_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """A second observation of an already-recorded PR appends no row and reports
    that nothing was written."""
    first = upsert_settled_pr(
        _DATA_DIR, 500, _merge_truth(500, settled=True, landed=True), 1,
    )
    second = upsert_settled_pr(
        _DATA_DIR, 500, _merge_truth(500, settled=True, landed=True), 2,
    )

    assert first is True
    assert second is False
    assert len(_ledger_rows(mem_fs)) == 1


def test_upsert_not_settled_truth_writes_nothing(
    mem_fs: dict[str, str],
) -> None:
    """An unsettled ``truth`` writes no row and creates no ledger — retraction of
    a reopened PR stays owned by ``reconcile_settled_verdicts``."""
    wrote = upsert_settled_pr(
        _DATA_DIR, 500, _merge_truth(500, settled=False, landed=False), 3,
    )

    assert wrote is False
    assert str(_DATA_DIR / _SETTLED_VERDICTS_LEDGER) not in mem_fs
    assert load_settled_prs(_DATA_DIR) == frozenset()


def test_upsert_kind_is_closed_for_closed_unmerged_pr(
    mem_fs: dict[str, str],
) -> None:
    """A settled-but-not-landed PR records ``kind`` ``"closed"``."""
    upsert_settled_pr(
        _DATA_DIR, 500, _merge_truth(500, settled=True, landed=False), 7,
    )

    rows = _ledger_rows(mem_fs)
    assert len(rows) == 1
    assert rows[0]["kind"] == "closed"
    assert load_settled_prs(_DATA_DIR) == frozenset({500})


def test_upsert_settle_row_superseded_by_later_windowed_retraction(
    mem_fs: dict[str, str],
) -> None:
    """A PR upserted as settled, then re-observed *unsettled* by
    ``reconcile_settled_verdicts`` while a ``Remediate PR #N`` record still names
    it (a windowed reopen), earns a superseding retraction row and drops out of
    the effective set — proving the two writers share one collapsible ledger."""
    upsert_settled_pr(
        _DATA_DIR, 500, _merge_truth(500, settled=True, landed=False), 1,
    )
    assert load_settled_prs(_DATA_DIR) == frozenset({500})

    _seed_history(mem_fs, [
        {"task_id": "t1", "goal": "Remediate PR #500: reopened"},
    ])
    result = reconcile_settled_verdicts(
        _DATA_DIR, 20, 9,
        is_pr_settled=lambda n: _merge_truth(500, settled=False, landed=False),
    )

    assert result == frozenset()
    rows = _ledger_rows(mem_fs)
    assert len(rows) == 2
    assert rows[0]["settled_at_iteration"] == 1
    assert rows[1]["pr_number"] == 500
    assert rows[1]["retracted_at_iteration"] == 9
    assert load_settled_prs(_DATA_DIR) == frozenset()


# ── Proposal-097 golden-registry AST guard ──────────────────────────────────
#
# The standing settlement reconciler (``reconcile_settled_verdicts``) records
# every settled PR to ``settled_verdicts.jsonl``, and ``_audit_self_model``
# threads that set into the two PR-keyed phantom audit rules as their
# ``settled_prs`` pre-filter, so a settled PR's phantom finding self-extinguishes
# even after its verdict record has frozen in append-only history. The structural
# guards below pin that contract — every PR-keyed phantom rule *carries* the
# pre-filter, actually *consumes* it, and the reconciler reaches merge-truth
# *solely* through its injected ``is_pr_settled`` chokepoint (never a bare git
# subprocess). Modeled on ``tests/test_merge_truth_chokepoint.py``.

# Proposal 147 Step 9 split the phantom rules and the settlement reconciler out
# of ``audit_checks.py`` into ``audit_checks_*.py`` siblings, so the guards parse
# the whole family: a ``settled_prs`` rule added to any sibling is still caught.
_AUDIT_CHECKS_PATHS = tuple(
    sorted((Path(__file__).resolve().parent.parent / "maestro").glob("audit_checks*.py"))
)

# The PR-keyed rules that MUST carry the ``settled_prs`` pre-filter.
# ``check_orphaned_cure`` (rule 11) joins the two phantom rules: its plan's
# ``pr_url`` binds it to a PR, so a settled cure must self-extinguish even while
# the plan's ``implemented`` → ``merged`` status flip lags behind a real merge.
# Deliberate exclusion (kept out by design, not oversight — the drift guard
# asserts the registry matches *exactly*): ``check_phantom_partial_prune``
# (rule 19, keyed on ``plan_id`` — no PR to filter).
_SETTLED_PR_RULES: frozenset[str] = frozenset({
    "check_phantom_escalation",
    "check_unconfirmed_own_landing",
    "check_orphaned_cure",
})

# The subset of :data:`_SETTLED_PR_RULES` that *has* a merge predicate to gate.
# ``check_phantom_escalation`` is excluded because it is git-free — it
# cross-references outbound messages and declares no ``is_pr_merged`` param —
# and the registry-consistency guard below proves that exclusion by measurement
# (the declared-param set) rather than by hand, so a rule gaining or losing a
# merge predicate fails the build until the registry follows.
_PREDICATE_BEARING_RULES: frozenset[str] = frozenset({
    "check_unconfirmed_own_landing",
})


def _parse_audit_checks() -> ast.Module:
    """Parse every ``maestro/audit_checks*.py`` into one AST module for the guards."""
    assert len(_AUDIT_CHECKS_PATHS) >= 4  # vacuity guard: parent + three siblings
    body: list[ast.stmt] = []
    for path in _AUDIT_CHECKS_PATHS:
        body.extend(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body)
    return ast.Module(body=body, type_ignores=[])


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    """Return the top-level ``FunctionDef`` named *name*, or raise if absent."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def _kwonly_defaults(node: ast.FunctionDef) -> dict[str, ast.expr | None]:
    """Map each keyword-only param name to its default node (``None`` if absent)."""
    return {
        arg.arg: default
        for arg, default in zip(
            node.args.kwonlyargs, node.args.kw_defaults, strict=True,
        )
    }


def _defaults_to_empty_frozenset(default: ast.expr | None) -> bool:
    """True when *default* is a bare ``frozenset()`` call with no arguments."""
    return (
        isinstance(default, ast.Call)
        and isinstance(default.func, ast.Name)
        and default.func.id == "frozenset"
        and not default.args
        and not default.keywords
    )


def _loaded_name_ids(node: ast.AST) -> set[str]:
    """Collect the ids of every ``Name`` read (``Load`` ctx) within *node*."""
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    }


# The module's bare git-subprocess merge readers. Either one re-derives
# merge-truth from git directly, so the reconciler must call neither — it reaches
# merge-truth solely through the injected ``is_pr_settled`` chokepoint. Both are
# fenced (not just ``_git_pr_merged_default``): ``is_branch_merged_into_main``
# runs its own ``subprocess.run`` inside its body, so a reconciler that called it
# would bypass merge-truth as a bare ``Name`` call while the subprocess stayed
# invisible to a walk over the reconciler node — the exact regression this guard
# exists to catch (proposal 097).
_FORBIDDEN_GIT_PRIMITIVES: frozenset[str] = frozenset({
    "_git_pr_merged_default",
    "is_branch_merged_into_main",
})


def _is_bare_git_merged_call(call: ast.Call) -> bool:
    """True when *call* invokes a forbidden git primitive as a bare ``Name``."""
    return (
        isinstance(call.func, ast.Name)
        and call.func.id in _FORBIDDEN_GIT_PRIMITIVES
    )


def _is_subprocess_call(call: ast.Call) -> bool:
    """True when *call* is a ``subprocess.<attr>(...)`` attribute call."""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "subprocess"
    )


def _offchokepoint_merge_calls(node: ast.AST) -> list[str]:
    """Describe every off-chokepoint merge call within *node* (empty when pure).

    Flags a bare call to any :data:`_FORBIDDEN_GIT_PRIMITIVES` name
    (``_git_pr_merged_default`` / ``is_branch_merged_into_main``) or any
    ``subprocess.<attr>(...)`` attribute call — the ways a body could re-derive
    merge-truth instead of reading the injected ``is_pr_settled``.
    """
    described: list[str] = []
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        if _is_bare_git_merged_call(call):
            assert isinstance(call.func, ast.Name)  # narrowed by the guard above
            described.append(f"{call.func.id}() @L{call.lineno}")
        elif _is_subprocess_call(call):
            described.append(f"subprocess.* @L{call.lineno}")
    return described


def test_find_function_raises_on_missing_name() -> None:
    """The ``FunctionDef`` locator raises for a name absent from the module."""
    tree = ast.parse("def present() -> None:\n    pass\n")
    with pytest.raises(AssertionError):
        _find_function(tree, "absent")
    assert _find_function(tree, "present").name == "present"


@pytest.mark.timeout(15)
def test_settled_pr_registry_matches_declared_prefilter_rules() -> None:
    """Exactly the registered rules declare ``settled_prs=frozenset()``.

    Bidirectional drift guard: a registered rule that drops the pre-filter param,
    and a newly-added rule that adds the param without registering it, both fail.
    This is the structural spine of proposal 097's self-extinguish contract —
    every PR-keyed phantom rule must carry the settlement pre-filter, and only
    those. ``@timeout(15)`` per the full-file AST-scan convention.
    """
    tree = _parse_audit_checks()
    declared = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and _defaults_to_empty_frozenset(
            _kwonly_defaults(node).get("settled_prs"),
        )
    }

    assert declared == _SETTLED_PR_RULES, (
        f"settled_prs pre-filter drift — declared {sorted(declared)} != "
        f"registry {sorted(_SETTLED_PR_RULES)}. Every PR-keyed phantom rule must "
        "carry a keyword-only settled_prs=frozenset() param and be registered in "
        "_SETTLED_PR_RULES (proposal 097); update the registry to match, or "
        "restore the dropped param."
    )


@pytest.mark.timeout(15)
def test_each_registered_rule_consumes_settled_prs() -> None:
    """Each registered rule *reads* ``settled_prs`` in its body, not just declares it.

    A rule that keeps the param but never loads it would pre-filter nothing while
    still passing the drift guard above — this catches that silent regression.
    """
    tree = _parse_audit_checks()
    ignored = [
        name
        for name in sorted(_SETTLED_PR_RULES)
        if "settled_prs" not in _loaded_name_ids(_find_function(tree, name))
    ]

    assert not ignored, (
        f"registered phantom rule declares settled_prs but never reads it: "
        f"{ignored} — the pre-filter is inert (proposal 097)."
    )


@pytest.mark.timeout(15)
def test_reconciler_reaches_merge_truth_only_through_chokepoint() -> None:
    """``reconcile_settled_verdicts`` never calls a bare merge primitive or subprocess.

    Merge-truth must be reached solely through the injected ``is_pr_settled``
    chokepoint. A refactor that wires a raw call to either forbidden git primitive
    (``_git_pr_merged_default(...)`` / ``is_branch_merged_into_main(...)``) or a
    ``subprocess.run(...)`` into the reconciler body would re-derive merge-truth
    off-chokepoint and desync the settled ledger from the live loop — this fails
    the build the moment it does.
    """
    tree = _parse_audit_checks()
    node = _find_function(tree, "reconcile_settled_verdicts")

    violations = _offchokepoint_merge_calls(node)

    assert not violations, (
        f"reconcile_settled_verdicts reaches merge-truth off-chokepoint: "
        f"{violations} — route the read through the injected is_pr_settled "
        "(proposal 097)."
    )


def test_settled_pr_registry_names_real_functions() -> None:
    """Every ``_SETTLED_PR_RULES`` entry names a real top-level function.

    Guards against a rename silently emptying the registry (which would make the
    drift and consumption guards vacuously pass), mirroring
    ``test_allowlist_entries_name_real_functions`` in the merge-truth guard.
    """
    tree = _parse_audit_checks()
    top_level = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    missing = sorted(_SETTLED_PR_RULES - top_level)

    assert not missing, f"registry names non-existent functions: {missing}"


@pytest.mark.timeout(15)
def test_predicate_bearing_registry_matches_declared_merge_predicates() -> None:
    """``_PREDICATE_BEARING_RULES`` is exactly the registered rules taking ``is_pr_merged``.

    Measured, not hand-maintained: the git-free ``check_phantom_escalation`` is
    excluded because it declares no merge predicate, and a registered rule that
    gains or loses one fails here until the sub-registry follows — keeping the
    ordering guard below aimed at every rule that actually has a predicate to
    gate. ``@timeout(15)`` per the full-file AST-scan convention.
    """
    tree = _parse_audit_checks()
    declared = {
        name
        for name in _SETTLED_PR_RULES
        if "is_pr_merged" in _kwonly_defaults(_find_function(tree, name))
    }

    assert declared == _PREDICATE_BEARING_RULES, (
        f"predicate-bearing drift — declared {sorted(declared)} != registry "
        f"{sorted(_PREDICATE_BEARING_RULES)}. A registered phantom rule gained "
        "or lost its is_pr_merged predicate; update _PREDICATE_BEARING_RULES so "
        "the settled-set ordering guard still covers every rule with a "
        "predicate to gate (proposal 097)."
    )
    assert set(_RULE_FIXTURES) == _PREDICATE_BEARING_RULES, (
        "every predicate-bearing rule needs a fixture in _RULE_FIXTURES so the "
        "ordering guard exercises it"
    )


# ---------------------------------------------------------------------------
# Behavioral ordering guard: a settled PR costs zero merge-predicate calls
# ---------------------------------------------------------------------------
#
# The structural guards above prove each rule *declares* and *reads*
# ``settled_prs`` — both of which a rule satisfies while still consulting the
# merge predicate first (an earlier phantom rule did exactly that until
# proposal 097's second increment). The contract the ``[audit]
# settlement_reconciler_enabled`` comment states is an *ordering* one: the
# settled set is consulted **before** the predicate, so a settled PR costs no
# merge-oracle read — in the live dispatch that predicate is
# ``MaestroAgent._host_or_git_pr_merged``, one code-host round-trip per call.
# A call-counting fake encodes that contract directly: it survives refactors of
# the rule bodies (unlike an AST domination check, which is brittle) and it
# fails on the exact regression the declare-and-read guards let through.

# The PR every fixture below cites, and the sole member of the settled set.
_SETTLED_PR = 4242

# An own committed SHA for the rule-14 handoff fixture; matches
# ``COMMITTED_SHA_RE`` (7-40 hex chars including at least one letter).
_HANDOFF_SHA = "ac48a769"


def _task_record(task_id: str, goal: str, outcome: str, summary: str) -> TaskRecord:
    """A minimal ``TaskRecord`` for the rules that take a task list."""
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


def _invoke_unconfirmed_own_landing(
    mem_fs: dict[str, str],
    settled: frozenset[int],
    predicate: Callable[[int], bool],
) -> list[AuditFinding]:
    """Rule 14 over an aged on-branch handoff bound to :data:`_SETTLED_PR`."""
    mem_fs[str(_DATA_DIR / _UNCONFIRMED_LANDING_SIDECAR)] = json.dumps(
        {_HANDOFF_SHA: 0},
    )
    tasks = [
        _task_record(
            "t1",
            f"Remediate PR #{_SETTLED_PR}: Fix the widget",
            "partial",
            f"Resolved on branch for PR #{_SETTLED_PR}; committed {_HANDOFF_SHA}.",
        ),
    ]
    return check_unconfirmed_own_landing(
        tasks, JsonUnconfirmedOwnLandingStore(_DATA_DIR), 5, age_cycles=3,
        is_ancestor=lambda sha: False,
        is_pr_merged=predicate, settled_prs=settled,
    )


# Per-rule ``(invoker, predicate reply that makes the fixture fire)``. Rule 14
# fires on an *unmerged* PR — so the control call's fake answers
# ``False`` per rule for the fixture to emit its finding.
_RULE_FIXTURES: dict[
    str,
    tuple[
        Callable[
            [dict[str, str], frozenset[int], Callable[[int], bool]],
            list[AuditFinding],
        ],
        bool,
    ],
] = {
    "check_unconfirmed_own_landing": (_invoke_unconfirmed_own_landing, False),
}


@pytest.mark.parametrize("rule_name", sorted(_PREDICATE_BEARING_RULES))
def test_settled_pr_costs_zero_merge_predicate_calls(
    mem_fs: dict[str, str], rule_name: str,
) -> None:
    """A PR in ``settled_prs`` never reaches the rule's merge predicate.

    The settled pre-filter must precede the predicate, not merely suppress the
    finding after it: the ledger exists so the audit layer stops re-litigating
    facts git has settled, and a post-predicate filter still pays a code-host
    round-trip per settled PR to compute a verdict it then discards.

    The control half — the same fixture with an empty settled set — asserts the
    predicate *is* consulted and the finding *does* fire, so a fixture that
    silently stopped matching its rule cannot make the zero-call assertion pass
    vacuously.
    """
    invoke, merged_reply = _RULE_FIXTURES[rule_name]
    calls: list[int] = []

    def _counting(pr_number: int) -> bool:
        calls.append(pr_number)
        return merged_reply

    findings = invoke(mem_fs, frozenset({_SETTLED_PR}), _counting)

    assert calls == [], (
        f"{rule_name} consulted the merge predicate for settled PR "
        f"#{_SETTLED_PR} — the settled pre-filter must precede the predicate "
        "(proposal 097)."
    )
    assert findings == []

    calls.clear()
    findings = invoke(mem_fs, frozenset(), _counting)

    assert calls == [_SETTLED_PR]
    assert len(findings) == 1


def test_guards_flag_a_synthetic_bypass() -> None:
    """The helpers actually catch the regressions the guards exist to prevent.

    Helper drift would silently make the real guards above pass on a genuine
    violator — exactly the failure this self-test forecloses. Build a synthetic
    module with (a) a phantom-shaped rule missing the ``settled_prs`` param,
    (b) a rule that declares but never reads it, and (c) a reconciler-shaped
    function that calls every off-chokepoint route — both forbidden git
    primitives (``_git_pr_merged_default(...)`` and
    ``is_branch_merged_into_main(...)``) and ``subprocess.run(...)`` — run the
    *same* helpers, and assert each violation is flagged (the canonical
    ``test_guard_flags_a_synthetic_bypass`` pattern).
    """
    source = (
        "import subprocess\n"
        "def check_phantom_new(data_dir, n, *, settled_prs=frozenset()):\n"
        "    return list(settled_prs)\n"
        "def check_phantom_broken(data_dir, n):\n"
        "    return []\n"
        "def check_phantom_inert(data_dir, n, *, settled_prs=frozenset()):\n"
        "    return []\n"
        "def reconcile_bypass(data_dir, n, *, is_pr_settled):\n"
        "    if _git_pr_merged_default(n):\n"
        "        return frozenset()\n"
        "    if is_branch_merged_into_main('maestro/task/x'):\n"
        "        return frozenset()\n"
        "    subprocess.run(['git', 'log'])\n"
        "    return frozenset({n})\n"
    )
    tree = ast.parse(source)

    # Drift guard: only the two rules that declare settled_prs=frozenset() surface;
    # the param-less ``check_phantom_broken`` is not among them.
    declared = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and _defaults_to_empty_frozenset(
            _kwonly_defaults(node).get("settled_prs"),
        )
    }
    assert declared == {"check_phantom_new", "check_phantom_inert"}

    # Consumption guard: the inert rule declares but never reads the param; the
    # compliant one does.
    assert "settled_prs" not in _loaded_name_ids(
        _find_function(tree, "check_phantom_inert"),
    )
    assert "settled_prs" in _loaded_name_ids(
        _find_function(tree, "check_phantom_new"),
    )

    # Purity guard: the bypass reconciler is flagged for BOTH forbidden git
    # primitives and the subprocess attribute call.
    violations = _offchokepoint_merge_calls(
        _find_function(tree, "reconcile_bypass"),
    )
    assert len(violations) == 3
    assert any("_git_pr_merged_default" in v for v in violations)
    assert any("is_branch_merged_into_main" in v for v in violations)
    assert any("subprocess" in v for v in violations)


def test_guards_pass_a_compliant_shape() -> None:
    """Mirror negative case: a compliant rule / reconciler triggers no guard.

    A rule that declares AND consumes ``settled_prs``, and a reconciler that
    reaches merge-truth only through the injected ``is_pr_settled`` (passing a
    primitive as an oracle *reference* is a reference, not a call), are not
    flagged — proving the guards discriminate rather than fire indiscriminately.
    """
    source = (
        "def check_phantom_ok(data_dir, n, *, settled_prs=frozenset()):\n"
        "    return [pr for pr in range(n) if pr not in settled_prs]\n"
        "def reconcile_ok(data_dir, n, *, is_pr_settled):\n"
        "    predicate = _git_pr_merged_default\n"
        "    return frozenset(\n"
        "        pr for pr in range(n) if is_pr_settled(pr).settled\n"
        "    ) or predicate\n"
    )
    tree = ast.parse(source)

    ok = _find_function(tree, "check_phantom_ok")
    assert _defaults_to_empty_frozenset(_kwonly_defaults(ok).get("settled_prs"))
    assert "settled_prs" in _loaded_name_ids(ok)

    # ``predicate = _git_pr_merged_default`` is a reference (Name load), not a
    # call, and ``is_pr_settled(pr)`` is the injected chokepoint — neither trips
    # the purity guard.
    assert _offchokepoint_merge_calls(_find_function(tree, "reconcile_ok")) == []

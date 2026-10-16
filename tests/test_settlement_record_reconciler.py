"""Tests for the proposal-103 on-settlement record-reconciler scaffold.

``reconcile_pr_settlement`` drives an ordered registry of per-store reconcilers
over one settled PR and appends one provenance row per reconciler that *changed*
its store (a clear, or a no-clear pass with a detail note) to the durable
``settlement_reconciliations.jsonl`` ledger — a silent no-op writes nothing, so
a recurring drive over a clean store cannot grow it. It is pure, zero-SDK, and
touches only the file system, so every test injects a ``MergeTruth`` fake and
small reconciler stubs (no real git, no subprocess, no network, no sleep) and
asserts simple equalities over the in-memory ``mem_fs`` filesystem. The shipped
module :data:`RECONCILER_REGISTRY` holds one concrete reconciler —
:class:`SelfModelReconciler`; the five stateful reconcilers
(:class:`EscalationStanddownReconciler`, :class:`RemediationTrackerReconciler`,
the record-only :class:`SettledVerdictReconciler`,
:class:`StandDownVerdictReconciler`, and :class:`PRLedgerReconciler`) are
agent-composed. Callers that pass an explicit empty registry keep the
pure-no-op path.
"""

import json
from pathlib import Path

import pytest

from maestro.audit_checks_settlement import (
    JsonlSettledVerdictsLedger,
)
from maestro.audit_checks_settlement import (
    load_settled_prs as _load_settled_prs_impl,
)
from maestro.consciousness import Consciousness, SelfModel
from maestro.escalation_bridge import (
    _DELIVERY_FILE,
    JsonEscalationLedger,
    has_standdown,
)
from maestro.pr_facts import MergeTruth
from maestro.pr_ledger import PRLedger
from maestro.remediation_tracker import (
    JsonlStandDownVerdictLedger,
    RemediationTracker,
    find_stand_down,
    has_live_stand_down,
    record_stand_down,
)
from maestro.settlement_record_reconciler import (
    _SETTLEMENT_RECONCILIATIONS_LEDGER,
    RECONCILER_REGISTRY,
    EscalationStanddownReconciler,
    JsonlSettlementReconciliationLedger,
    PRLedgerReconciler,
    ReconcileResult,
    RemediationTrackerReconciler,
    SelfModelReconciler,
    SettledVerdictReconciler,
    SettlementReconciler,
    SettlementReconciliationRecord,
    StandDownVerdictReconciler,
    load_all_settlement_reconciliations,
    reconcile_pr_settlement,
)

_DATA_DIR = Path("/fake/data")

#: Shared default (flag-off) provenance backend the driven tests pass to
#: ``reconcile_pr_settlement``; stateless (binds only ``_DATA_DIR``), so it writes
#: every row to ``_DATA_DIR / settlement_reconciliations.jsonl`` under ``mem_fs`` —
#: exactly the path ``_ledger_rows`` reads back.
_LEDGER_BACKEND = JsonlSettlementReconciliationLedger(_DATA_DIR)

#: The flag-off escalation-standdown backend the escalation reconciler holds
#: (proposal 106) — it reads/writes ``_DATA_DIR / escalation_handoffs.json``, the
#: same file ``_seed_standdown`` / ``has_standdown`` touch, so the raw-JSON seeds
#: below stay valid. Stateless (binds only the path), so one instance is shared.
_ESCALATION_BACKEND = JsonEscalationLedger(_DATA_DIR)


def load_settled_prs(data_dir: Path) -> frozenset[int]:
    """Read the effective settled set via the default JSONL backend.

    The settled-verdicts pre-filter moved behind a ``SettledVerdictsBackend``
    (proposal 106); this wrapper binds the production-default
    :class:`JsonlSettledVerdictsLedger` over the shared test ``data_dir`` so the
    ``mem_fs`` assertions below keep reading the flag-off JSONL path.
    """
    return _load_settled_prs_impl(JsonlSettledVerdictsLedger(data_dir))


def _seed_standdown(mem_fs: dict[str, str], pr: int) -> None:
    """Seed a ``pr``-keyed escalation standdown under ``_DATA_DIR``.

    Mirrors ``tests/test_escalation_bridge.py::_record_standdown``: writes the
    minimal ``{head_sha, delivered_at}`` record shape that ``_load_standdowns``
    accepts, so ``has_standdown`` / ``clear_standdown`` see a live standdown.
    """
    path = str(_DATA_DIR / _DELIVERY_FILE)
    existing = json.loads(mem_fs[path]) if mem_fs.get(path) else {}
    existing[str(pr)] = {
        "head_sha": "abc123", "delivered_at": "2026-01-01T00:00:00+00:00",
    }
    mem_fs[path] = json.dumps(existing)


def _merge_truth(pr: int, *, settled: bool, landed: bool) -> MergeTruth:
    """Build a ``MergeTruth`` with a settled/landed shape for the fake signal."""
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
    """Parse the rows written under ``settlement_reconciliations.jsonl``."""
    raw = mem_fs.get(str(_DATA_DIR / _SETTLEMENT_RECONCILIATIONS_LEDGER), "")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


class _FakeReconciler:
    """A per-store reconciler stub reporting a fixed ``cleared``/``detail``."""

    def __init__(
        self, name: str, *, cleared: bool = True, detail: str | None = None,
    ) -> None:
        self.name = name
        self._cleared = cleared
        self._detail = detail

    def reconcile(
        self, data_dir: Path, pr_number: int, truth: MergeTruth, iteration: int,
    ) -> ReconcileResult:
        return ReconcileResult(cleared=self._cleared, detail=self._detail)


class _RaisingReconciler:
    """A reconciler stub whose ``reconcile`` raises, to prove fail-loud."""

    name = "boom"

    def reconcile(
        self, data_dir: Path, pr_number: int, truth: MergeTruth, iteration: int,
    ) -> ReconcileResult:
        raise RuntimeError("reconcile blew up")


def test_shipped_registry_holds_only_self_model_reconciler() -> None:
    """The shipped module registry holds one entry — the self-model reconciler,
    the sole reconciler constructible without agent context. The five stateful
    reconcilers (escalation-standdown, remediation-tracker, record-only
    settled-verdict, stand-down-verdict, and pr-ledger) are agent-composed, so
    they do *not* appear here (see the drive-site tests)."""
    assert len(RECONCILER_REGISTRY) == 1
    assert [r.name for r in RECONCILER_REGISTRY] == ["self_model"]
    assert isinstance(RECONCILER_REGISTRY[0], SelfModelReconciler)
    # The escalation-standdown reconciler is agent-composed (holds the
    # flag-selected EscalationLedgerBackend, proposal 106), not a module registry
    # entry, yet still satisfies the protocol surface.
    assert isinstance(
        EscalationStanddownReconciler(_ESCALATION_BACKEND),
        SettlementReconciler,
    )
    # The record-only settled-verdict reconciler is agent-composed, not a module
    # registry entry, yet still satisfies the protocol surface.
    assert isinstance(
        SettledVerdictReconciler(JsonlSettledVerdictsLedger(_DATA_DIR)),
        SettlementReconciler,
    )
    # The stand-down-verdict reconciler is likewise agent-composed (holds the
    # selected StandDownVerdictBackend, proposal 106), not a module registry
    # entry, yet still satisfies the protocol surface.
    assert isinstance(
        StandDownVerdictReconciler(JsonlStandDownVerdictLedger(_DATA_DIR)),
        SettlementReconciler,
    )
    # The pr-ledger reconciler is likewise agent-composed (holds the flag-selected
    # PRLedger, proposal 106), not a module registry entry, yet still satisfies
    # the protocol surface.
    assert isinstance(
        PRLedgerReconciler(PRLedger(_DATA_DIR)),
        SettlementReconciler,
    )
    # Structural: every shipped entry and the stub satisfy the
    # runtime-checkable protocol surface.
    assert all(isinstance(r, SettlementReconciler) for r in RECONCILER_REGISTRY)
    assert isinstance(_FakeReconciler("x"), SettlementReconciler)


def test_empty_registry_writes_no_ledger_and_is_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """An explicit empty-registry pass writes no ledger file, returns ``[]``, and
    repeats harmlessly — the pure-no-op path callers keep by passing
    ``registry=()`` (the shipped default now carries one entry, exercised
    separately)."""
    truth = _merge_truth(100, settled=True, landed=True)
    ledger_key = str(_DATA_DIR / _SETTLEMENT_RECONCILIATIONS_LEDGER)

    first = reconcile_pr_settlement(
        _DATA_DIR, 100, truth, registry=(), iteration=0, backend=_LEDGER_BACKEND,
    )
    assert first == []
    assert ledger_key not in mem_fs

    # Repeating leaves nothing behind either.
    second = reconcile_pr_settlement(
        _DATA_DIR, 100, truth, registry=(), iteration=2, backend=_LEDGER_BACKEND,
    )
    assert second == []
    assert ledger_key not in mem_fs


def test_populated_registry_writes_one_row_with_documented_keys(
    mem_fs: dict[str, str],
) -> None:
    """A single reconciler writes exactly one row carrying the documented
    keys/values, including ``at_iteration`` and a null ``detail``, and returns
    its result."""
    truth = _merge_truth(200, settled=True, landed=False)
    reconciler = _FakeReconciler("self_model", cleared=True)

    result = reconcile_pr_settlement(
        _DATA_DIR, 200, truth, registry=(reconciler,), iteration=7,
        backend=_LEDGER_BACKEND,
    )

    assert result == [ReconcileResult(cleared=True)]
    rows = _ledger_rows(mem_fs)
    assert rows == [
        {
            "pr_number": 200,
            "reconciler": "self_model",
            "cleared": True,
            "detail": None,
            "at_iteration": 7,
        }
    ]


def test_reconcile_result_detail_is_persisted_to_ledger_row(
    mem_fs: dict[str, str],
) -> None:
    """A reconciler's optional ``detail`` note reaches the durable ledger row —
    not only the caller-facing :class:`ReconcileResult`."""
    truth = _merge_truth(210, settled=True, landed=True)
    reconciler = _FakeReconciler(
        "task_queue", cleared=True, detail="dropped 1 stale trace",
    )

    result = reconcile_pr_settlement(
        _DATA_DIR, 210, truth, registry=(reconciler,), iteration=9,
        backend=_LEDGER_BACKEND,
    )

    assert result == [
        ReconcileResult(cleared=True, detail="dropped 1 stale trace"),
    ]
    rows = _ledger_rows(mem_fs)
    assert rows == [
        {
            "pr_number": 210,
            "reconciler": "task_queue",
            "cleared": True,
            "detail": "dropped 1 stale trace",
            "at_iteration": 9,
        }
    ]


def test_registry_writes_rows_only_for_reconcilers_that_changed_a_store(
    mem_fs: dict[str, str],
) -> None:
    """The driver appends a provenance row only for a reconciler that *changed*
    the store: a clear (``cleared=True``), or a no-clear pass carrying a
    ``detail`` note. A silent no-op (``cleared=False`` with no detail) writes no
    row, so a recurring drive over an already-clean store cannot grow the
    ledger. Every reconciler still runs and returns its result; the rows that do
    land preserve registry order across the skipped no-op."""
    truth = _merge_truth(300, settled=True, landed=True)
    registry = (
        _FakeReconciler("first", cleared=True),
        _FakeReconciler("second", cleared=False),  # silent no-op — no row
        _FakeReconciler("third", cleared=False, detail="noted"),  # informative
        _FakeReconciler("fourth", cleared=True),
    )

    result = reconcile_pr_settlement(
        _DATA_DIR, 300, truth, registry=registry, iteration=3, backend=_LEDGER_BACKEND,
    )

    # Every reconciler ran and its result is returned...
    assert [res.cleared for res in result] == [True, False, False, True]
    # ...but only the three that changed a store leave a ledger row, in order.
    rows = _ledger_rows(mem_fs)
    assert [row["reconciler"] for row in rows] == ["first", "third", "fourth"]
    assert [row["cleared"] for row in rows] == [True, False, True]
    assert [row["detail"] for row in rows] == [None, "noted", None]
    assert all(row["pr_number"] == 300 for row in rows)
    assert all(row["at_iteration"] == 3 for row in rows)


def test_raising_reconciler_propagates_and_leaves_no_ledger(
    mem_fs: dict[str, str],
) -> None:
    """A raising reconciler is not swallowed — it propagates (fail-loud) and no
    partial ledger row is left behind."""
    truth = _merge_truth(400, settled=True, landed=True)

    with pytest.raises(RuntimeError, match="reconcile blew up"):
        reconcile_pr_settlement(
            _DATA_DIR, 400, truth,
            registry=(_RaisingReconciler(),), iteration=0,
            backend=_LEDGER_BACKEND,
        )

    assert str(_DATA_DIR / _SETTLEMENT_RECONCILIATIONS_LEDGER) not in mem_fs


def test_mid_loop_raise_preserves_completed_reconciler_rows(
    mem_fs: dict[str, str],
) -> None:
    """A reconciler that raises mid-loop still propagates (fail-loud), but the
    rows of the reconcilers that already ran — and cleared their PR-keyed
    stores — stay durably recorded: provenance follows the side effects that
    actually happened, so a later raise cannot erase an earlier clear's record.
    The raising reconciler (and everything after it) writes no row."""
    truth = _merge_truth(500, settled=True, landed=True)
    registry = (
        _FakeReconciler("first", cleared=True, detail="cleared A"),
        _RaisingReconciler(),
        _FakeReconciler("third", cleared=True),
    )

    with pytest.raises(RuntimeError, match="reconcile blew up"):
        reconcile_pr_settlement(
            _DATA_DIR, 500, truth, registry=registry, iteration=4, backend=_LEDGER_BACKEND,
        )

    rows = _ledger_rows(mem_fs)
    assert rows == [
        {
            "pr_number": 500,
            "reconciler": "first",
            "cleared": True,
            "detail": "cleared A",
            "at_iteration": 4,
        }
    ]


def test_escalation_reconciler_clears_seeded_standdown_then_noops(
    mem_fs: dict[str, str],
) -> None:
    """``EscalationStanddownReconciler.reconcile`` removes a seeded standdown
    (``cleared=True`` with a note) and is idempotent — a second pass over the
    now-clean store is a no-op (``cleared=False``, ``detail=None``)."""
    _seed_standdown(mem_fs, 600)
    truth = _merge_truth(600, settled=True, landed=True)
    reconciler = EscalationStanddownReconciler(_ESCALATION_BACKEND)
    assert has_standdown(_ESCALATION_BACKEND, 600) is True

    first = reconciler.reconcile(_DATA_DIR, 600, truth, 0)
    assert first == ReconcileResult(
        cleared=True, detail="removed escalation standdown",
    )
    assert has_standdown(_ESCALATION_BACKEND, 600) is False

    # Idempotent: the store is already clean, so the recurring pass no-ops.
    second = reconciler.reconcile(_DATA_DIR, 600, truth, 0)
    assert second == ReconcileResult(cleared=False, detail=None)


def test_escalation_reconciler_noops_when_no_standdown(
    mem_fs: dict[str, str],
) -> None:
    """With no standdown for the PR, the reconciler reports ``cleared=False`` and
    writes nothing to the standdown store."""
    truth = _merge_truth(610, settled=True, landed=True)
    result = EscalationStanddownReconciler(_ESCALATION_BACKEND).reconcile(_DATA_DIR, 610, truth, 0)
    assert result == ReconcileResult(cleared=False, detail=None)
    assert has_standdown(_ESCALATION_BACKEND, 610) is False


def test_escalation_reconciler_skips_clear_when_truth_not_settled(
    mem_fs: dict[str, str],
) -> None:
    """The clear is gated on ``truth.settled``: a live standdown for a PR the
    signal now reports **not** settled (a reopened PR the ledger-backed candidate
    set still names) is left intact and the reconciler reports ``cleared=False``
    — the clear-on-settlement contract holds even when the caller over-includes."""
    _seed_standdown(mem_fs, 620)
    truth = _merge_truth(620, settled=False, landed=False)
    assert has_standdown(_ESCALATION_BACKEND, 620) is True

    result = EscalationStanddownReconciler(_ESCALATION_BACKEND).reconcile(_DATA_DIR, 620, truth, 0)

    assert result == ReconcileResult(cleared=False, detail=None)
    # The standdown survives — a not-settled PR is never cleared.
    assert has_standdown(_ESCALATION_BACKEND, 620) is True


def test_default_registry_drives_escalation_standdown_row(
    mem_fs: dict[str, str],
) -> None:
    """Driving a settled PR through an explicit registry mirroring the
    agent-composed order (the escalation-standdown reconciler first, then the
    self-model one) clears its escalation standdown, writing one
    ``escalation_standdown`` provenance row carrying the documented keys. The
    registry's second entry — the self-model reconciler — has no sole-PR
    self-model trace seeded for this PR, so it is a silent no-op; only the
    standdown clear changes a store, so the ledger holds exactly that one row. The
    other agent-composed reconcilers (remediation-tracker, record-only
    settled-verdict, stand-down-verdict, and pr-ledger) are omitted here — the
    driven settled-verdict path is covered by
    ``test_settled_verdict_reconciler_driven_writes_one_row...``, the driven
    stand-down path by ``test_stand_down_verdict_reconciler_driven_writes_one_row...``,
    and the driven pr-ledger path by
    ``test_pr_ledger_reconciler_driven_writes_one_row...``."""
    _seed_standdown(mem_fs, 700)
    truth = _merge_truth(700, settled=True, landed=True)

    result = reconcile_pr_settlement(
        _DATA_DIR, 700, truth, iteration=12, backend=_LEDGER_BACKEND,
        registry=(
            EscalationStanddownReconciler(_ESCALATION_BACKEND),
            SelfModelReconciler(),
        ),
    )

    assert result == [
        ReconcileResult(cleared=True, detail="removed escalation standdown"),
        ReconcileResult(cleared=False, detail=None),
    ]
    assert has_standdown(_ESCALATION_BACKEND, 700) is False
    rows = _ledger_rows(mem_fs)
    assert rows == [
        {
            "pr_number": 700,
            "reconciler": "escalation_standdown",
            "cleared": True,
            "detail": "removed escalation standdown",
            "at_iteration": 12,
        },
    ]


def test_recurring_noop_drive_over_clean_store_writes_no_ledger(
    mem_fs: dict[str, str],
) -> None:
    """Regression (proposal-103 review): driving a settled PR whose stores are
    already in their target state writes **no** ledger row, and re-driving it every
    cycle never accumulates rows. Without this guard the audit cycle re-drives each
    warm-memo settled PR each iteration and, once its escalation standdown was
    cleared on an earlier pass, appends a fresh ``cleared=False`` no-op row every
    cycle — growing the durable ledger without bound in a static-window regime.
    Uses an explicit registry of the two clearing reconcilers —
    escalation-standdown and self-model — with no standdown and no sole-PR
    self-model trace, so every reconciler pass is a pure no-op."""
    truth = _merge_truth(710, settled=True, landed=True)
    ledger_key = str(_DATA_DIR / _SETTLEMENT_RECONCILIATIONS_LEDGER)
    registry = (
        EscalationStanddownReconciler(_ESCALATION_BACKEND),
        SelfModelReconciler(),
    )

    first = reconcile_pr_settlement(
        _DATA_DIR, 710, truth, iteration=1, backend=_LEDGER_BACKEND,
        registry=registry,
    )
    assert first == [
        ReconcileResult(cleared=False, detail=None),
        ReconcileResult(cleared=False, detail=None),
    ]
    assert ledger_key not in mem_fs

    # Recurring passes stay no-ops and never accumulate a row.
    reconcile_pr_settlement(
        _DATA_DIR, 710, truth, iteration=2, backend=_LEDGER_BACKEND,
        registry=registry,
    )
    reconcile_pr_settlement(
        _DATA_DIR, 710, truth, iteration=3, backend=_LEDGER_BACKEND,
        registry=registry,
    )
    assert ledger_key not in mem_fs


# --- SelfModelReconciler (proposal 103, store 2) ----------------------------
#
# ``SelfModel.reconcile_settled_pr`` is a pure in-memory mutation, so its tests
# construct a ``SelfModel`` directly and assert over its lists/directive; the
# ``SelfModelReconciler.reconcile`` tests exercise the disk read-modify-write
# and the driver's provenance row under ``mem_fs``.


def test_reconcile_settled_pr_evicts_sole_pr_limitation_and_its_source() -> None:
    """A settled PR's sole-cited ``[limitation]`` ``open_questions`` row and the
    ``limitations_known`` entry behind it are both evicted, and the method
    reports a change; an unrelated question survives."""
    model = SelfModel(
        limitations_known=["Blocked forever on PR #100"],
        open_questions=["[limitation] PR #100 is stuck", "What am I?"],
    )

    changed = model.reconcile_settled_pr(
        100, _merge_truth(100, settled=True, landed=True),
    )

    assert changed is True
    assert model.limitations_known == []
    assert model.open_questions == ["What am I?"]


def test_reconcile_settled_pr_evicts_limitation_question_orphaned_by_truncation(
) -> None:
    """Regression (PR #3288 review): a >160-char limitation whose sole ``#NNNN``
    citation falls past char 160 surfaces a ``[limitation]`` question truncated
    below that citation by ``surface_limitations_as_questions`` — the mirror
    loses the ``#NNNN`` and cannot be matched by ``_sole_cited``. The full-length
    source is still evicted, so without cross-referencing the evicted sources the
    citation-less mirror would linger forever, defeating the store's cleanup
    contract. The mirror must be evicted alongside its source; an unrelated
    question is untouched."""
    filler = "the reconciler review is still pending downstream "  # 50 chars
    long_limitation = (filler * 3) + "and blocked on PR #3131"
    # The construction genuinely reproduces the orphan case: the entry is longer
    # than the 160-char surface cap (but within the 200-char item cap) and its
    # sole citation starts past char 160, so the surfaced mirror drops it.
    assert 160 < len(long_limitation) <= Consciousness.MAX_ITEM_CHARS
    assert long_limitation.index("#3131") >= 160

    model = SelfModel(
        limitations_known=[long_limitation],
        open_questions=["What am I?"],
    )
    Consciousness.surface_limitations_as_questions(model)
    surfaced = model.open_questions[-1]
    assert surfaced.startswith("[limitation]")
    # The surfaced mirror was truncated below the citation — it has no ``#``.
    assert "#" not in surfaced

    changed = model.reconcile_settled_pr(
        3131, _merge_truth(3131, settled=True, landed=True),
    )

    assert changed is True
    assert model.limitations_known == []
    # The orphaned citation-less mirror is evicted; the unrelated row survives.
    assert model.open_questions == ["What am I?"]


def test_reconcile_settled_pr_evicts_sole_pr_audit_row() -> None:
    """A settled PR's sole-cited ``[audit]`` ``open_questions`` row is evicted
    (settlement covers a *closed* PR too, not only a merged one)."""
    model = SelfModel(
        open_questions=["[audit] phantom_escalation: PR #200", "keep me"],
    )

    changed = model.reconcile_settled_pr(
        200, _merge_truth(200, settled=True, landed=False),
    )

    assert changed is True
    assert model.open_questions == ["keep me"]


def test_reconcile_settled_pr_preserves_multi_pr_and_unprefixed_rows() -> None:
    """The sole-PR rule never over-clears: a multi-PR row (even with the PR
    settled), a prefixed row citing a *different* PR, and a non-prefixed row
    citing the settled PR are all preserved, so the pass reports no change."""
    model = SelfModel(
        limitations_known=["Both #100 and #200 pending", "unrelated #300"],
        open_questions=[
            "[audit] multi #100 #200",
            "[limitation] other PR #300",
            "plain row citing #100 with no bracket prefix",
        ],
    )

    changed = model.reconcile_settled_pr(
        100, _merge_truth(100, settled=True, landed=True),
    )

    assert changed is False
    assert model.limitations_known == [
        "Both #100 and #200 pending", "unrelated #300",
    ]
    assert model.open_questions == [
        "[audit] multi #100 #200",
        "[limitation] other PR #300",
        "plain row citing #100 with no bracket prefix",
    ]


def test_reconcile_settled_pr_preserves_mirror_of_preserved_multi_pr_limitation(
) -> None:
    """Regression (PR #3288 review): a multi-PR limitation citing a *settled* PR
    early and a *live* co-cited PR past the 160-char surface cap is correctly
    PRESERVED — the full entry still names live work, so ``_sole_cited`` on the
    source is False. But its surfaced ``[limitation]`` mirror is truncated below
    the live citation, so the mirror textually cites only the settled PR. The
    mirror must NOT be evicted while its source survives: evicting it re-surfaces
    the identical mirror next cycle and re-evicts it, emitting a fresh ``cleared``
    ledger row every cycle (the unbounded growth the module forbids). The pass
    must report no change."""
    filler = "the co-cited review remains open downstream still "  # 50 chars
    long_limitation = (
        "settled PR #3131 co-cited with " + (filler * 3) + "live PR #4242"
    )
    # #3131 (settled) sits before the 160-char surface cap; #4242 (live) past it,
    # so the truncated mirror keeps only #3131 while the source keeps both.
    assert long_limitation.index("#3131") < 160
    assert long_limitation.index("#4242") >= 160
    assert 160 < len(long_limitation) <= Consciousness.MAX_ITEM_CHARS

    model = SelfModel(
        limitations_known=[long_limitation],
        open_questions=["What am I?"],
    )
    Consciousness.surface_limitations_as_questions(model)
    surfaced = model.open_questions[-1]
    assert surfaced.startswith("[limitation]")
    assert "#3131" in surfaced
    assert "#4242" not in surfaced

    changed = model.reconcile_settled_pr(
        3131, _merge_truth(3131, settled=True, landed=True),
    )

    # Source names live work → preserved; its surviving-source mirror is
    # preserved too, so nothing changed (no ledger row will be written).
    assert changed is False
    assert model.limitations_known == [long_limitation]
    assert model.open_questions == ["What am I?", surfaced]


def test_reconcile_settled_pr_clears_sole_pr_directive() -> None:
    """A ``next_task_directive`` whose sole cited PR is the settled one is
    cleared to ``None``."""
    model = SelfModel(next_task_directive="Finish PR #100")

    changed = model.reconcile_settled_pr(
        100, _merge_truth(100, settled=True, landed=True),
    )

    assert changed is True
    assert model.next_task_directive is None


def test_reconcile_settled_pr_preserves_directive_citing_other_or_extra_pr(
) -> None:
    """A directive naming a *different* PR, or the settled PR *plus* another, is
    preserved — only a sole-PR directive is cleared."""
    settled = _merge_truth(100, settled=True, landed=True)

    other = SelfModel(next_task_directive="Finish PR #200")
    assert other.reconcile_settled_pr(100, settled) is False
    assert other.next_task_directive == "Finish PR #200"

    extra = SelfModel(next_task_directive="Land PR #100 then hand off PR #200")
    assert extra.reconcile_settled_pr(100, settled) is False
    assert extra.next_task_directive == "Land PR #100 then hand off PR #200"


def test_reconcile_settled_pr_not_settled_is_full_noop() -> None:
    """A not-settled ``truth`` leaves every store untouched and reports no
    change, even when a sole-PR row/source/directive is present — the reopened-PR
    guard the escalation reconciler also relies on."""
    model = SelfModel(
        limitations_known=["Blocked on PR #100"],
        open_questions=["[audit] PR #100"],
        next_task_directive="Finish PR #100",
    )

    changed = model.reconcile_settled_pr(
        100, _merge_truth(100, settled=False, landed=False),
    )

    assert changed is False
    assert model.limitations_known == ["Blocked on PR #100"]
    assert model.open_questions == ["[audit] PR #100"]
    assert model.next_task_directive == "Finish PR #100"


def test_reconcile_settled_pr_ignores_non_string_entries() -> None:
    """Defensive guard: a non-string ``limitations_known`` entry — a malformed
    in-memory model that never passed through ``load_self_model``'s str coercion
    — is treated as un-cited and preserved, never crashing the citation scan;
    the sibling string row whose sole cited PR settled is still evicted."""
    model = SelfModel(limitations_known=[123, "Blocked on PR #100"])

    changed = model.reconcile_settled_pr(
        100, _merge_truth(100, settled=True, landed=True),
    )

    assert changed is True
    assert model.limitations_known == [123]


def test_self_model_reconciler_driven_persists_eviction_and_writes_row(
    mem_fs: dict[str, str],
) -> None:
    """Driving a settled PR through ``reconcile_pr_settlement`` with the
    self-model reconciler loads, shrinks, and *persists* the self-model, and
    writes exactly one ``self_model`` provenance row carrying the documented
    keys on the real eviction."""
    Consciousness(_DATA_DIR).save_self_model(SelfModel(
        limitations_known=["Blocked on PR #808"],
        open_questions=["[limitation] PR #808 stuck", "What am I?"],
        next_task_directive="Finish PR #808",
    ))
    truth = _merge_truth(808, settled=True, landed=True)

    result = reconcile_pr_settlement(
        _DATA_DIR, 808, truth,
        registry=(SelfModelReconciler(),), iteration=5,
        backend=_LEDGER_BACKEND,
    )

    assert result == [
        ReconcileResult(cleared=True, detail="evicted settled-PR self-model rows"),
    ]
    # The eviction is durable — reloading from disk shows the traces gone.
    reloaded = Consciousness(_DATA_DIR).load_self_model()
    assert reloaded.limitations_known == []
    assert reloaded.open_questions == ["What am I?"]
    assert reloaded.next_task_directive is None
    # Exactly one provenance row, naming the self-model reconciler.
    rows = _ledger_rows(mem_fs)
    assert rows == [
        {
            "pr_number": 808,
            "reconciler": "self_model",
            "cleared": True,
            "detail": "evicted settled-PR self-model rows",
            "at_iteration": 5,
        }
    ]


def test_self_model_reconciler_driven_noop_writes_no_row_and_keeps_model(
    mem_fs: dict[str, str],
) -> None:
    """A settled PR with no sole-cited self-model trace is a no-op: the model is
    left intact on disk and no provenance row is written, so a recurring drive
    over an already-clean self-model cannot grow the ledger."""
    seed = SelfModel(
        limitations_known=["Both #808 and #900 pending"],
        open_questions=["[audit] other PR #900"],
        next_task_directive="Finish PR #900",
    )
    Consciousness(_DATA_DIR).save_self_model(seed)
    truth = _merge_truth(808, settled=True, landed=True)

    result = reconcile_pr_settlement(
        _DATA_DIR, 808, truth,
        registry=(SelfModelReconciler(),), iteration=6,
        backend=_LEDGER_BACKEND,
    )

    assert result == [ReconcileResult(cleared=False, detail=None)]
    reloaded = Consciousness(_DATA_DIR).load_self_model()
    assert reloaded.limitations_known == ["Both #808 and #900 pending"]
    assert reloaded.open_questions == ["[audit] other PR #900"]
    assert reloaded.next_task_directive == "Finish PR #900"
    assert str(_DATA_DIR / _SETTLEMENT_RECONCILIATIONS_LEDGER) not in mem_fs


def test_self_model_reconciler_driven_multi_pr_mirror_writes_no_row_across_drives(
    mem_fs: dict[str, str],
) -> None:
    """Regression (PR #3288 review): when a settled PR's only self-model trace is
    the truncated mirror of a *preserved* multi-PR limitation (source still names
    a live co-cited PR past char 160), the drive writes no provenance row — not on
    the first drive and not on a repeat — so a live-work mirror cannot churn the
    ledger unbounded across cycles."""
    filler = "the co-cited review remains open downstream still "  # 50 chars
    long_limitation = (
        "settled PR #3131 co-cited with " + (filler * 3) + "live PR #4242"
    )
    seed = SelfModel(limitations_known=[long_limitation])
    Consciousness.surface_limitations_as_questions(seed)
    mirror = seed.open_questions[-1]
    Consciousness(_DATA_DIR).save_self_model(seed)
    truth = _merge_truth(3131, settled=True, landed=True)

    for iteration in (8, 9):
        result = reconcile_pr_settlement(
            _DATA_DIR, 3131, truth,
            registry=(SelfModelReconciler(),), iteration=iteration,
            backend=_LEDGER_BACKEND,
        )
        assert result == [ReconcileResult(cleared=False, detail=None)]

    # The preserved source and its live-work mirror survive both drives, and the
    # ledger was never touched — the churn the review flagged does not occur.
    reloaded = Consciousness(_DATA_DIR).load_self_model()
    assert reloaded.limitations_known == [long_limitation]
    assert mirror in reloaded.open_questions
    assert str(_DATA_DIR / _SETTLEMENT_RECONCILIATIONS_LEDGER) not in mem_fs


def test_self_model_reconciler_not_settled_leaves_model_and_writes_no_row(
    mem_fs: dict[str, str],
) -> None:
    """When the live signal reports the candidate PR *not* settled (a reopened
    PR the caller's set still names), the reconciler makes no change, persists
    nothing, and writes no provenance row."""
    Consciousness(_DATA_DIR).save_self_model(SelfModel(
        limitations_known=["Blocked on PR #808"],
        open_questions=["[audit] PR #808"],
        next_task_directive="Finish PR #808",
    ))
    truth = _merge_truth(808, settled=False, landed=False)

    result = reconcile_pr_settlement(
        _DATA_DIR, 808, truth,
        registry=(SelfModelReconciler(),), iteration=7,
        backend=_LEDGER_BACKEND,
    )

    assert result == [ReconcileResult(cleared=False, detail=None)]
    reloaded = Consciousness(_DATA_DIR).load_self_model()
    assert reloaded.limitations_known == ["Blocked on PR #808"]
    assert reloaded.open_questions == ["[audit] PR #808"]
    assert reloaded.next_task_directive == "Finish PR #808"
    assert str(_DATA_DIR / _SETTLEMENT_RECONCILIATIONS_LEDGER) not in mem_fs


# --- RemediationTrackerReconciler (proposal 103, store 3) -------------------
#
# The reconciler is stateful: it holds the single long-lived ``RemediationTracker``
# instance and mutates it directly, so its tests construct one tracker, seed a
# PR across all four axes, and inject the *same* instance into the reconciler.


def _seed_tracker(pr: int) -> RemediationTracker:
    """Build a ``RemediationTracker`` under ``_DATA_DIR`` with *pr* on all axes.

    Seeds the no-op outcome list (past threshold, so ``should_reroute`` is
    ``False``), the outstanding-items baseline, the repetition-gate skip count,
    and the merge-block skip count, so a full eviction is observable across every
    axis. Relies on the caller's active ``mem_fs`` fixture for the persistence.
    """
    tracker = RemediationTracker(_DATA_DIR, threshold=2)
    tracker.record_outcome(pr, noop=True, items_remaining=4)
    tracker.record_outcome(pr, noop=True)
    tracker.record_skip(pr)
    tracker.record_outcome(pr, noop=True, merge_block=True)
    return tracker


def test_remediation_tracker_reconciler_evicts_then_noops(
    mem_fs: dict[str, str],
) -> None:
    """``RemediationTrackerReconciler.reconcile`` forgets a settled PR's records
    on the first pass (``cleared=True`` with a detail note) and is idempotent — a
    second pass over the now-empty store is a no-op (``cleared=False``,
    ``detail=None``)."""
    tracker = _seed_tracker(900)
    reconciler = RemediationTrackerReconciler(tracker)
    truth = _merge_truth(900, settled=True, landed=True)
    assert tracker.should_reroute(900) is False

    first = reconciler.reconcile(_DATA_DIR, 900, truth, 0)
    assert first == ReconcileResult(
        cleared=True, detail="evicted settled-PR remediation-tracker entry",
    )
    # Every axis was purged from the shared instance.
    assert tracker.should_reroute(900) is True
    assert tracker.last_items_remaining(900) is None
    assert tracker.skip_count(900) == 0
    assert tracker.merge_block_skip_count(900) == 0

    # Idempotent: the store is already clean, so the recurring pass no-ops.
    second = reconciler.reconcile(_DATA_DIR, 900, truth, 0)
    assert second == ReconcileResult(cleared=False, detail=None)


def test_remediation_tracker_reconciler_noops_when_tracker_empty(
    mem_fs: dict[str, str],
) -> None:
    """With no records for the PR, the reconciler reports ``cleared=False`` and
    leaves the store untouched."""
    tracker = RemediationTracker(_DATA_DIR, threshold=2)
    truth = _merge_truth(910, settled=True, landed=True)
    result = RemediationTrackerReconciler(tracker).reconcile(_DATA_DIR, 910, truth, 0)
    assert result == ReconcileResult(cleared=False, detail=None)


def test_remediation_tracker_reconciler_skips_clear_when_truth_not_settled(
    mem_fs: dict[str, str],
) -> None:
    """The clear is gated on ``truth.settled``: a PR the caller's candidate set
    still names but the live signal reports **not** settled (a reopened PR) keeps
    its tracker records and the reconciler reports ``cleared=False`` — the
    clear-on-settlement contract holds even when the caller over-includes."""
    tracker = _seed_tracker(920)
    truth = _merge_truth(920, settled=False, landed=False)

    result = RemediationTrackerReconciler(tracker).reconcile(_DATA_DIR, 920, truth, 0)

    assert result == ReconcileResult(cleared=False, detail=None)
    # The records survive — a not-settled PR is never forgotten.
    assert tracker.should_reroute(920) is False


def test_remediation_tracker_reconciler_driven_writes_one_row_then_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """Driving a settled PR through ``reconcile_pr_settlement`` with the tracker
    reconciler evicts its records and writes exactly one ``remediation_tracker``
    provenance row on the transition; a recurring re-drive over the now-clean
    store writes nothing, so the durable ledger cannot grow unbounded."""
    tracker = _seed_tracker(930)
    registry = (RemediationTrackerReconciler(tracker),)
    truth = _merge_truth(930, settled=True, landed=True)

    result = reconcile_pr_settlement(
        _DATA_DIR, 930, truth, registry=registry, iteration=3, backend=_LEDGER_BACKEND,
    )

    assert result == [
        ReconcileResult(
            cleared=True, detail="evicted settled-PR remediation-tracker entry",
        ),
    ]
    assert tracker.should_reroute(930) is True
    assert _ledger_rows(mem_fs) == [
        {
            "pr_number": 930,
            "reconciler": "remediation_tracker",
            "cleared": True,
            "detail": "evicted settled-PR remediation-tracker entry",
            "at_iteration": 3,
        }
    ]

    # Re-driving the now-clean store writes no further row (idempotent).
    reconcile_pr_settlement(
        _DATA_DIR, 930, truth, registry=registry, iteration=4, backend=_LEDGER_BACKEND,
    )
    assert len(_ledger_rows(mem_fs)) == 1


# --- StandDownVerdictReconciler (proposal 103, store 4; proposal 106 seam) ---
#
# The reconciler is now stateful: it holds the agent-selected
# ``StandDownVerdictBackend`` (proposal 106) and ignores the ``data_dir`` passed to
# ``reconcile``. Its tests seed a verdict with ``record_stand_down`` over a
# ``JsonlStandDownVerdictLedger`` and assert over ``find_stand_down`` /
# ``has_live_stand_down`` (same backend) under ``mem_fs``.


def test_stand_down_verdict_reconciler_releases_then_noops(
    mem_fs: dict[str, str],
) -> None:
    """``StandDownVerdictReconciler.reconcile`` releases a live stand-down on the
    first pass (``cleared=True`` with a detail note) — the verdict then reads
    voided at *any* head — and is idempotent: a second pass over the now-clear
    store is a no-op (``cleared=False``, ``detail=None``)."""
    backend = JsonlStandDownVerdictLedger(_DATA_DIR)
    record_stand_down(940, "sha1", backend, now="2026-06-16T00:00:00+00:00")
    reconciler = StandDownVerdictReconciler(backend)
    truth = _merge_truth(940, settled=True, landed=True)
    assert has_live_stand_down(940, backend) is True

    first = reconciler.reconcile(_DATA_DIR, 940, truth, 0)
    assert first == ReconcileResult(
        cleared=True, detail="released settled-PR stand-down verdict",
    )
    # The suppression is lifted head-agnostically: find_stand_down misses at the
    # recorded head and at any other head, and the PR is no longer live.
    assert find_stand_down(940, "sha1", backend) is None
    assert find_stand_down(940, "otherSHA", backend) is None
    assert has_live_stand_down(940, backend) is False

    # Idempotent: the store is already clear, so the recurring pass no-ops.
    second = reconciler.reconcile(_DATA_DIR, 940, truth, 0)
    assert second == ReconcileResult(cleared=False, detail=None)


def test_stand_down_verdict_reconciler_noops_when_no_live_verdict(
    mem_fs: dict[str, str],
) -> None:
    """With no live stand-down for the PR, the reconciler reports
    ``cleared=False`` and appends no release row — an absent store stays absent."""
    truth = _merge_truth(950, settled=True, landed=True)
    result = StandDownVerdictReconciler(
        JsonlStandDownVerdictLedger(_DATA_DIR),
    ).reconcile(_DATA_DIR, 950, truth, 0)
    assert result == ReconcileResult(cleared=False, detail=None)
    assert str(_DATA_DIR / "stand_down_verdicts.jsonl") not in mem_fs


def test_stand_down_verdict_reconciler_skips_clear_when_truth_not_settled(
    mem_fs: dict[str, str],
) -> None:
    """The clear is gated on ``truth.settled``: a live stand-down for a PR the
    caller's candidate set still names but the signal reports **not** settled (a
    reopened PR) is left intact, the reconciler reports ``cleared=False``, and no
    release row is appended — the clear-on-settlement contract holds even when the
    caller over-includes."""
    backend = JsonlStandDownVerdictLedger(_DATA_DIR)
    record_stand_down(960, "sha1", backend, now="2026-06-16T00:00:00+00:00")
    truth = _merge_truth(960, settled=False, landed=False)

    result = StandDownVerdictReconciler(backend).reconcile(_DATA_DIR, 960, truth, 0)

    assert result == ReconcileResult(cleared=False, detail=None)
    # The verdict survives — a not-settled PR is never released.
    assert has_live_stand_down(960, backend) is True
    # Only the one seeded verdict line remains; no release row was appended.
    raw = mem_fs[str(_DATA_DIR / "stand_down_verdicts.jsonl")]
    assert [json.loads(line) for line in raw.splitlines() if line.strip()] == [
        {
            "pr": 960,
            "head_sha": "sha1",
            "recorded_at": "2026-06-16T00:00:00+00:00",
        },
    ]


def test_stand_down_verdict_reconciler_driven_writes_one_row_then_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """Driving a settled PR through ``reconcile_pr_settlement`` with the
    stand-down-verdict reconciler releases its verdict and writes exactly one
    ``stand_down_verdict`` provenance row on the transition; a recurring re-drive
    over the now-clear store writes nothing, so the durable ledger cannot grow
    unbounded."""
    backend = JsonlStandDownVerdictLedger(_DATA_DIR)
    record_stand_down(970, "sha1", backend, now="2026-06-16T00:00:00+00:00")
    registry = (StandDownVerdictReconciler(backend),)
    truth = _merge_truth(970, settled=True, landed=True)

    result = reconcile_pr_settlement(
        _DATA_DIR, 970, truth, registry=registry, iteration=3, backend=_LEDGER_BACKEND,
    )

    assert result == [
        ReconcileResult(
            cleared=True, detail="released settled-PR stand-down verdict",
        ),
    ]
    assert has_live_stand_down(970, backend) is False
    assert _ledger_rows(mem_fs) == [
        {
            "pr_number": 970,
            "reconciler": "stand_down_verdict",
            "cleared": True,
            "detail": "released settled-PR stand-down verdict",
            "at_iteration": 3,
        }
    ]

    # Re-driving the now-clear store writes no further provenance row.
    reconcile_pr_settlement(
        _DATA_DIR, 970, truth, registry=registry, iteration=4, backend=_LEDGER_BACKEND,
    )
    assert len(_ledger_rows(mem_fs)) == 1


# --- PRLedgerReconciler (proposal 103, store 5) -----------------------------
#
# The reconciler is now stateful (proposal 106): it holds the agent-selected live
# ``PRLedger`` and evicts through it. Its tests inject a ``PRLedger(_DATA_DIR)``
# (the default JSON backend under ``mem_fs``), seed an entry with
# ``observe_head``, and assert over ``PRLedger.get``.


def test_pr_ledger_reconciler_evicts_then_noops(
    mem_fs: dict[str, str],
) -> None:
    """``PRLedgerReconciler.reconcile`` forgets a settled PR's ledger entry on the
    first pass (``cleared=True`` with a detail note) and is idempotent: a second
    pass over the now-absent entry is a no-op (``cleared=False``,
    ``detail=None``)."""
    ledger = PRLedger(_DATA_DIR)
    ledger.observe_head(980, "b980", "sha1")
    reconciler = PRLedgerReconciler(ledger)
    truth = _merge_truth(980, settled=True, landed=True)
    assert ledger.get(980) is not None

    first = reconciler.reconcile(_DATA_DIR, 980, truth, 0)
    assert first == ReconcileResult(
        cleared=True, detail="evicted settled-PR ledger entry",
    )
    assert ledger.get(980) is None

    # Idempotent: the entry is already gone, so the recurring pass no-ops.
    second = reconciler.reconcile(_DATA_DIR, 980, truth, 0)
    assert second == ReconcileResult(cleared=False, detail=None)


def test_pr_ledger_reconciler_noops_when_ledger_empty(
    mem_fs: dict[str, str],
) -> None:
    """With no ledger entry for the PR, the reconciler reports ``cleared=False``
    and writes no file — an absent store stays absent."""
    truth = _merge_truth(981, settled=True, landed=True)
    result = PRLedgerReconciler(PRLedger(_DATA_DIR)).reconcile(
        _DATA_DIR, 981, truth, 0,
    )
    assert result == ReconcileResult(cleared=False, detail=None)
    assert str(_DATA_DIR / "pr_ledger.json") not in mem_fs


def test_pr_ledger_reconciler_skips_clear_when_truth_not_settled(
    mem_fs: dict[str, str],
) -> None:
    """The clear is gated on ``truth.settled``: a ledger entry for a PR the
    caller's candidate set still names but the signal reports **not** settled (a
    reopened PR) is left intact, the reconciler reports ``cleared=False``, and no
    eviction happens — the clear-on-settlement contract holds even when the caller
    over-includes."""
    ledger = PRLedger(_DATA_DIR)
    ledger.observe_head(982, "b982", "sha1")
    truth = _merge_truth(982, settled=False, landed=False)

    result = PRLedgerReconciler(ledger).reconcile(_DATA_DIR, 982, truth, 0)

    assert result == ReconcileResult(cleared=False, detail=None)
    # The entry survives — a not-settled PR is never evicted.
    assert ledger.get(982) is not None


def test_pr_ledger_reconciler_driven_writes_one_row_then_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """Driving a settled PR through ``reconcile_pr_settlement`` with the pr-ledger
    reconciler evicts its entry and writes exactly one ``pr_ledger`` provenance row
    on the transition; a recurring re-drive over the now-absent entry writes
    nothing, so the durable ledger cannot grow unbounded."""
    ledger = PRLedger(_DATA_DIR)
    ledger.observe_head(983, "b983", "sha1")
    registry = (PRLedgerReconciler(ledger),)
    truth = _merge_truth(983, settled=True, landed=True)

    result = reconcile_pr_settlement(
        _DATA_DIR, 983, truth, registry=registry, iteration=3, backend=_LEDGER_BACKEND,
    )

    assert result == [
        ReconcileResult(cleared=True, detail="evicted settled-PR ledger entry"),
    ]
    assert ledger.get(983) is None
    assert _ledger_rows(mem_fs) == [
        {
            "pr_number": 983,
            "reconciler": "pr_ledger",
            "cleared": True,
            "detail": "evicted settled-PR ledger entry",
            "at_iteration": 3,
        }
    ]

    # Re-driving the now-absent entry writes no further provenance row.
    reconcile_pr_settlement(
        _DATA_DIR, 983, truth, registry=registry, iteration=4, backend=_LEDGER_BACKEND,
    )
    assert len(_ledger_rows(mem_fs)) == 1


# ── SettledVerdictReconciler ───────────────────────────────────────────────────
#
# The one *record-only* reconciler: it writes a settle record into the
# window-independent settled-verdicts pre-filter through ``upsert_settled_pr``
# rather than clearing a trace, so ``cleared`` reads as "recorded a settlement
# this pass" and an already-recorded PR is a silent no-op. Since the pre-filter
# moved behind a ``SettledVerdictsBackend`` (proposal 106), the reconciler is
# stateful — it holds the injected backend — so its tests construct it over a
# ``JsonlSettledVerdictsLedger`` bound to the shared ``_DATA_DIR`` and assert over
# ``load_settled_prs`` under ``mem_fs``.


def test_settled_verdict_reconciler_records_then_noops(
    mem_fs: dict[str, str],
) -> None:
    """``SettledVerdictReconciler.reconcile`` records a settled PR into the
    settled-verdicts pre-filter on the first pass (``cleared=True`` with a detail
    note) and is idempotent: a second pass over the now-recorded PR is a no-op
    (``cleared=False``, ``detail=None``), so the recurring drive never
    re-appends."""
    reconciler = SettledVerdictReconciler(JsonlSettledVerdictsLedger(_DATA_DIR))
    truth = _merge_truth(1100, settled=True, landed=True)
    assert load_settled_prs(_DATA_DIR) == frozenset()

    first = reconciler.reconcile(_DATA_DIR, 1100, truth, 7)
    assert first == ReconcileResult(
        cleared=True,
        detail="recorded settlement into settled-verdicts pre-filter",
    )
    assert load_settled_prs(_DATA_DIR) == frozenset({1100})

    # Idempotent: the PR is already recorded, so the recurring pass no-ops.
    second = reconciler.reconcile(_DATA_DIR, 1100, truth, 8)
    assert second == ReconcileResult(cleared=False, detail=None)
    assert load_settled_prs(_DATA_DIR) == frozenset({1100})


def test_settled_verdict_reconciler_skips_record_when_truth_not_settled(
    mem_fs: dict[str, str],
) -> None:
    """The record is gated on ``truth.settled``: a PR the caller's candidate set
    still names but the signal reports **not** settled (a reopened PR) is never
    recorded, the reconciler reports ``cleared=False``, and no ledger file is
    written — the record-on-settlement contract holds even when the caller
    over-includes."""
    truth = _merge_truth(1101, settled=False, landed=False)

    result = SettledVerdictReconciler(
        JsonlSettledVerdictsLedger(_DATA_DIR),
    ).reconcile(_DATA_DIR, 1101, truth, 0)

    assert result == ReconcileResult(cleared=False, detail=None)
    assert load_settled_prs(_DATA_DIR) == frozenset()
    assert str(_DATA_DIR / "settled_verdicts.jsonl") not in mem_fs


def test_settled_verdict_reconciler_driven_writes_one_row_then_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """Driving a settled PR through ``reconcile_pr_settlement`` with the
    settled-verdict reconciler records it and writes exactly one ``settled_verdict``
    provenance row on the transition; a recurring re-drive over the now-recorded PR
    writes nothing, so the durable ledger cannot grow unbounded."""
    registry = (SettledVerdictReconciler(JsonlSettledVerdictsLedger(_DATA_DIR)),)
    truth = _merge_truth(1102, settled=True, landed=True)

    result = reconcile_pr_settlement(
        _DATA_DIR, 1102, truth, registry=registry, iteration=5,
        backend=_LEDGER_BACKEND,
    )

    assert result == [
        ReconcileResult(
            cleared=True,
            detail="recorded settlement into settled-verdicts pre-filter",
        ),
    ]
    assert load_settled_prs(_DATA_DIR) == frozenset({1102})
    assert _ledger_rows(mem_fs) == [
        {
            "pr_number": 1102,
            "reconciler": "settled_verdict",
            "cleared": True,
            "detail": "recorded settlement into settled-verdicts pre-filter",
            "at_iteration": 5,
        }
    ]

    # Re-driving the now-recorded PR writes no further provenance row.
    reconcile_pr_settlement(
        _DATA_DIR, 1102, truth, registry=registry, iteration=6,
        backend=_LEDGER_BACKEND,
    )
    assert len(_ledger_rows(mem_fs)) == 1


# --- load_all_settlement_reconciliations (proposal 106 SQLite seed source) ---


def test_load_all_settlement_reconciliations_parses_valid_skips_malformed(
    mem_fs: dict[str, str],
) -> None:
    """The SQLite seed source returns valid rows oldest-first and silently skips
    blank lines, undecodable lines, non-object rows, and rows with a missing or
    wrong-typed field — the corrupt-tail tolerance the flag-flip seed relies on."""
    valid_a = {
        "pr_number": 10, "reconciler": "self_model", "cleared": True,
        "detail": "evicted rows", "at_iteration": 1,
    }
    valid_b = {
        "pr_number": 11, "reconciler": "pr_ledger", "cleared": False,
        "detail": None, "at_iteration": 2,
    }
    lines = [
        json.dumps(valid_a),
        "",                                 # blank — skipped
        "not json at all",                  # undecodable — skipped
        json.dumps([1, 2, 3]),              # decodes to a non-dict — skipped
        json.dumps(                         # missing pr_number — skipped
            {"reconciler": "x", "cleared": True, "at_iteration": 3},
        ),
        json.dumps(valid_b),
    ]
    mem_fs[str(_DATA_DIR / _SETTLEMENT_RECONCILIATIONS_LEDGER)] = (
        "\n".join(lines) + "\n"
    )

    assert load_all_settlement_reconciliations(_DATA_DIR) == [
        SettlementReconciliationRecord(
            pr_number=10, reconciler="self_model", cleared=True,
            detail="evicted rows", at_iteration=1,
        ),
        SettlementReconciliationRecord(
            pr_number=11, reconciler="pr_ledger", cleared=False,
            detail=None, at_iteration=2,
        ),
    ]


def test_load_all_settlement_reconciliations_absent_file_is_empty() -> None:
    """A missing ledger yields an empty list — the seed source fails open."""
    assert load_all_settlement_reconciliations(Path("/fake/absent-recon")) == []

"""Tests for the owned-lever capture ledger and distinct-target selector.

Covers :mod:`maestro.lever_capture`:

* :func:`fire_and_capture` records a post-fire persistence-verified boolean
  (when a ``verify`` callable is supplied) rather than the lever's raw return,
  and stays backward-compatible (raw return) when none is.
* :func:`select_distinct_obsolete_target` never re-selects an id already
  recorded ``True`` (the set ``S``) nor the forbidden null id, and stands down
  (``None``) when the whole backlog is excluded.
* :func:`verify_obsolete_persisted` reads the plan store fresh and only
  affirms a literal ``obsolete`` status.

Fully mocked I/O via ``mem_fs``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro import lever_capture
from maestro.lever_capture import (
    FORBIDDEN_OBSOLETE_TARGET,
    DirectiveObsoleteKind,
    JsonlLeverAttemptsLedger,
    LeverAttempt,
    build_lever_attempt,
    fire_and_capture,
    fire_directive_named_obsolete,
    load_all_lever_attempts,
    read_already_true_plan_ids,
    select_distinct_obsolete_target,
    verify_obsolete_persisted,
)
from maestro.memory import _OBSOLETE_REASON_MAX_CHARS, EvolutionPlan, Memory
from maestro.state_store import SqliteLeverAttemptsLedger, StateStore

FAKE_DIR = Path("/fake/mem")
_LEDGER = FAKE_DIR / "lever_attempts.jsonl"


def _make_memory() -> Memory:
    return Memory(FAKE_DIR)


def _ledger() -> JsonlLeverAttemptsLedger:
    """The default JSONL backend over the shared fake memory dir.

    Reads/writes the same ``FAKE_DIR / lever_attempts.jsonl`` that
    :func:`_seed_ledger` seeds and the row assertions read, so swapping the old
    ``_LEDGER`` path argument for this backend is behaviour-preserving.
    """
    return JsonlLeverAttemptsLedger(FAKE_DIR)


def _plan(plan_id: str, created_at: str, status: str = "pending_review") -> EvolutionPlan:
    return EvolutionPlan(
        plan_id=plan_id,
        title=f"Plan {plan_id}",
        category="capability",
        rationale=f"Rationale for {plan_id}",
        proposed_changes=[],
        files_affected=[],
        expected_impact="",
        status=status,  # type: ignore[arg-type]
        created_at=created_at,
    )


def _seed_ledger(mem_fs: dict[str, str], rows: list[dict[str, object]]) -> None:
    mem_fs[str(_LEDGER)] = "".join(json.dumps(r) + "\n" for r in rows)


def _true_row(plan_id: str) -> dict[str, object]:
    return {
        "lever": "mark_plan_obsolete",
        "args": [plan_id, "disposal reason"],
        "verified": True,
        "result": True,
    }


# ---------------------------------------------------------------------------
# read_already_true_plan_ids
# ---------------------------------------------------------------------------


def test_read_already_true_missing_file_is_empty(mem_fs: dict[str, str]) -> None:
    """A ledger that does not exist yields the empty set, not an error."""
    assert read_already_true_plan_ids(_ledger()) == set()


def test_read_already_true_collects_only_true_rows(mem_fs: dict[str, str]) -> None:
    """Only ``result is True`` rows contribute their args[0] plan_id to S."""
    _seed_ledger(
        mem_fs,
        [
            _true_row("aaa"),
            {"lever": "mark_plan_obsolete", "args": ["bbb", "r"], "result": False},
            _true_row("ccc"),
        ],
    )
    assert read_already_true_plan_ids(_ledger()) == {"aaa", "ccc"}


def test_read_already_true_ignores_other_levers(mem_fs: dict[str, str]) -> None:
    """A True row of another lever (its args[0] is no plan_id) stays out of S."""
    _seed_ledger(
        mem_fs,
        [_true_row("aaa"), {"lever": "split_due_directive", "args": ["Split x"],
                            "verified": True, "result": True}],
    )
    assert read_already_true_plan_ids(_ledger()) == {"aaa"}


def test_read_already_true_skips_blank_and_corrupt_lines(
    mem_fs: dict[str, str],
) -> None:
    """Blank and unparseable lines are skipped, not raised."""
    mem_fs[str(_LEDGER)] = (
        "\n"
        + "not json at all\n"
        + json.dumps(_true_row("aaa"))
        + "\n"
        + json.dumps({"lever": "x", "args": [], "result": True})  # shapeless args
        + "\n"
    )
    assert read_already_true_plan_ids(_ledger()) == {"aaa"}


# ---------------------------------------------------------------------------
# select_distinct_obsolete_target
# ---------------------------------------------------------------------------


def test_selector_returns_oldest_not_in_S(mem_fs: dict[str, str]) -> None:
    """(a) The oldest pending plan whose id is not in S is returned."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan("old", "2025-01-01T00:00:00Z"))
    mem.save_evolution_plan(_plan("mid", "2025-01-02T00:00:00Z"))
    mem.save_evolution_plan(_plan("new", "2025-01-03T00:00:00Z"))
    _seed_ledger(mem_fs, [_true_row("old")])  # "old" already subtracted

    chosen = select_distinct_obsolete_target(mem, _ledger())

    assert chosen is not None
    assert chosen.plan_id == "mid"


def test_selector_never_returns_forbidden_even_when_oldest(
    mem_fs: dict[str, str],
) -> None:
    """(b) The forbidden null id is skipped even as the oldest candidate."""
    mem = _make_memory()
    mem.save_evolution_plan(
        _plan(FORBIDDEN_OBSOLETE_TARGET, "2025-01-01T00:00:00Z"),
    )
    mem.save_evolution_plan(_plan("later", "2025-01-02T00:00:00Z"))

    chosen = select_distinct_obsolete_target(mem, _ledger())

    assert chosen is not None
    assert chosen.plan_id == "later"


def test_selector_returns_none_when_all_in_S(mem_fs: dict[str, str]) -> None:
    """(c) Every pending plan already in S ⇒ None (no re-fire of a True id)."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan("a", "2025-01-01T00:00:00Z"))
    mem.save_evolution_plan(_plan("b", "2025-01-02T00:00:00Z"))
    _seed_ledger(mem_fs, [_true_row("a"), _true_row("b")])

    assert select_distinct_obsolete_target(mem, _ledger()) is None


def test_selector_returns_none_when_only_forbidden_remains(
    mem_fs: dict[str, str],
) -> None:
    """A backlog of only the forbidden id yields None (nothing distinct)."""
    mem = _make_memory()
    mem.save_evolution_plan(
        _plan(FORBIDDEN_OBSOLETE_TARGET, "2025-01-01T00:00:00Z"),
    )

    assert select_distinct_obsolete_target(mem, _ledger()) is None


def test_selector_empty_backlog_returns_none(mem_fs: dict[str, str]) -> None:
    """No pending plans ⇒ None."""
    mem = _make_memory()
    assert select_distinct_obsolete_target(mem, _ledger()) is None


def test_selector_ignores_non_pending_plans(mem_fs: dict[str, str]) -> None:
    """Only ``pending_review`` plans are candidates; others are skipped."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan("done", "2025-01-01T00:00:00Z", status="merged"))
    mem.save_evolution_plan(_plan("live", "2025-01-02T00:00:00Z"))

    chosen = select_distinct_obsolete_target(mem, _ledger())

    assert chosen is not None
    assert chosen.plan_id == "live"


# ---------------------------------------------------------------------------
# verify_obsolete_persisted
# ---------------------------------------------------------------------------


def test_verify_true_on_flipped_status(mem_fs: dict[str, str]) -> None:
    """(d) A plan whose status was flipped to obsolete verifies True."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan("p", "2025-01-01T00:00:00Z"))
    assert mem.mark_plan_obsolete("p", "disposed") is True

    assert verify_obsolete_persisted(mem, "p") is True


def test_verify_false_on_unchanged_status(mem_fs: dict[str, str]) -> None:
    """(d) A still-pending plan verifies False."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan("p", "2025-01-01T00:00:00Z"))

    assert verify_obsolete_persisted(mem, "p") is False


def test_verify_false_on_missing_plan(mem_fs: dict[str, str]) -> None:
    """An unknown plan id verifies False, never raises."""
    mem = _make_memory()
    assert verify_obsolete_persisted(mem, "ghost") is False


# ---------------------------------------------------------------------------
# fire_and_capture
# ---------------------------------------------------------------------------


def test_fire_and_capture_records_verified_result_over_raw(
    mem_fs: dict[str, str],
) -> None:
    """(e) With a verify callable, the recorded result is the verified boolean.

    A lever that returns ``True`` but whose change did not persist (verify
    ``False``) must record ``False`` — the null act cannot stamp success.
    """
    calls: list[str] = []

    def fake_lever(plan_id: str, reason: str) -> bool:
        calls.append(plan_id)
        return True  # raw return claims success...

    result = fire_and_capture(
        _ledger(),
        "mark_plan_obsolete",
        fake_lever,
        "p",
        "reason",
        verify=lambda: False,  # ...but persistence says otherwise
    )

    assert result is False
    assert calls == ["p"]
    rows = [json.loads(line) for line in mem_fs[str(_LEDGER)].strip().splitlines()]
    assert len(rows) == 1
    assert rows[0]["result"] is False
    assert rows[0]["verified"] is True
    assert rows[0]["args"] == ["p", "reason"]


def test_fire_and_capture_verify_true_but_raw_false_records_false(
    mem_fs: dict[str, str],
) -> None:
    """A refused lever is recorded False even when the target already reads obsolete.

    Another path may have made the plan ``obsolete`` already; the re-read then
    says ``True`` but this fire changed nothing, so the recorded result is the
    conjunction ``raw and verify`` — ``False`` — and verify is never consulted.
    """
    verify_calls: list[bool] = []

    def verify() -> bool:
        verify_calls.append(True)
        return True

    result = fire_and_capture(
        _ledger(),
        "mark_plan_obsolete",
        lambda pid, reason: False,
        "p",
        "reason",
        verify=verify,
    )

    assert result is False
    assert verify_calls == []
    rows = [json.loads(line) for line in mem_fs[str(_LEDGER)].strip().splitlines()]
    assert rows[0]["result"] is False
    # No re-read ran, so the row does not claim one.
    assert rows[0]["verified"] is False


def test_fire_and_capture_records_verified_true(mem_fs: dict[str, str]) -> None:
    """A genuinely-persisted flip records and returns True."""
    result = fire_and_capture(
        _ledger(),
        "mark_plan_obsolete",
        lambda pid, reason: True,
        "p",
        "reason",
        verify=lambda: True,
    )

    assert result is True
    rows = [json.loads(line) for line in mem_fs[str(_LEDGER)].strip().splitlines()]
    assert rows[0]["result"] is True
    assert rows[0]["verified"] is True


def test_fire_and_capture_without_verify_records_raw(
    mem_fs: dict[str, str],
) -> None:
    """Legacy path (no verify) records the lever's literal return unchanged."""
    result = fire_and_capture(
        _ledger(),
        "mark_plan_obsolete",
        lambda pid, reason: True,
        "p",
        "reason",
    )

    assert result is True
    rows = [json.loads(line) for line in mem_fs[str(_LEDGER)].strip().splitlines()]
    assert rows[0]["result"] is True
    assert rows[0]["verified"] is False


def test_fire_and_capture_verify_runs_after_lever(mem_fs: dict[str, str]) -> None:
    """The verify callable observes state produced by the fired lever."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan("p", "2025-01-01T00:00:00Z"))

    result = fire_and_capture(
        _ledger(),
        "mark_plan_obsolete",
        mem.mark_plan_obsolete,
        "p",
        "disposed via distinct-target selector",
        verify=lambda: verify_obsolete_persisted(mem, "p"),
    )

    assert result is True
    assert mem.get_plan("p").status == "obsolete"  # type: ignore[union-attr]
    rows = [json.loads(line) for line in mem_fs[str(_LEDGER)].strip().splitlines()]
    assert rows[0]["result"] is True


def test_fire_and_capture_verify_false_on_forbidden_target(
    mem_fs: dict[str, str],
) -> None:
    """Firing on an already-terminal plan: raw False, verify False, id not in S.

    The whole loop closes here — a fire that changes nothing is recorded
    ``False``, so ``read_already_true_plan_ids`` never admits the id into S.
    """
    mem = _make_memory()
    mem.save_evolution_plan(
        _plan("gone", "2025-01-01T00:00:00Z", status="merged"),
    )

    result = fire_and_capture(
        _ledger(),
        "mark_plan_obsolete",
        mem.mark_plan_obsolete,
        "gone",
        "attempted disposal",
        verify=lambda: verify_obsolete_persisted(mem, "gone"),
    )

    assert result is False
    assert read_already_true_plan_ids(_ledger()) == set()


# ---------------------------------------------------------------------------
# load_all_lever_attempts / _parse_lever_attempt shape tolerance
# ---------------------------------------------------------------------------


class TestLoadAllLeverAttempts:
    """The oldest-first seed-source read and its shapeless-row tolerance."""

    def test_returns_oldest_first(self, mem_fs: dict[str, str]) -> None:
        _seed_ledger(mem_fs, [_true_row("a"), _true_row("b"), _true_row("c")])
        attempts = load_all_lever_attempts(FAKE_DIR)
        # Forward (append) order — the seed order for the SQLite adapter.
        assert [at.args[0] for at in attempts] == ["a", "b", "c"]

    def test_absent_ledger_returns_empty(self, mem_fs: dict[str, str]) -> None:
        assert load_all_lever_attempts(Path("/fake/other")) == []

    def test_skips_shapeless_and_corrupt_rows(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Blank, non-JSON, non-dict, and mis-shaped rows are all dropped."""
        mem_fs[str(_LEDGER)] = (
            "\n"  # blank
            + "not json at all\n"  # JSONDecodeError
            + json.dumps([1, 2, 3]) + "\n"  # valid JSON, not a dict
            + json.dumps({"args": ["x"], "result": True}) + "\n"  # no lever
            + json.dumps(
                {"lever": "y", "args": "nope", "result": True},
            ) + "\n"  # non-list args
            + json.dumps(_true_row("keep")) + "\n"  # the one valid row
        )
        attempts = load_all_lever_attempts(FAKE_DIR)
        assert len(attempts) == 1
        assert attempts[0].args[0] == "keep"

    def test_verified_and_result_coerce_to_literal_bool(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A missing ``verified`` and a non-bool ``result`` coerce via ``is True``."""
        mem_fs[str(_LEDGER)] = (
            json.dumps({"lever": "l", "args": ["p"], "result": "truthy"}) + "\n"
        )
        attempts = load_all_lever_attempts(FAKE_DIR)
        assert len(attempts) == 1
        assert attempts[0].verified is False
        assert attempts[0].result is False


class TestJsonlLeverAttemptsAdapter:
    """The JSONL adapter round-trips one row through record → load_all."""

    def test_record_appends_and_load_all_round_trips(
        self, mem_fs: dict[str, str],
    ) -> None:
        ledger = JsonlLeverAttemptsLedger(FAKE_DIR)
        attempt = build_lever_attempt(
            "mark_plan_obsolete", ["p", "reason"], True, True,
        )
        ledger.record(attempt)
        lines = [ln for ln in mem_fs[str(_LEDGER)].splitlines() if ln.strip()]
        assert len(lines) == 1
        loaded = ledger.load_all()
        assert loaded == [attempt]


# ---------------------------------------------------------------------------
# Backend parity — JSONL vs in-memory SQLite
# ---------------------------------------------------------------------------


class TestBackendParity:
    """JSONL and SQLite lever-attempts backends must be interchangeable.

    Proves the flag-flip is behaviour-preserving before the JSONL path is ever
    conceded: recording the same fire sequence through both yields equal
    ``load_all`` output, equal ``read_already_true_plan_ids`` sets, and an equal
    distinct-target selection. Mirrors ``tests/test_deed_ledger.py``'s parity
    block.
    """

    @staticmethod
    def _shared_attempts() -> list[LeverAttempt]:
        # A True fire, a null-act False fire, and an un-verified True fire, so
        # both the args round-trip and the two boolean flags exercise 0/1.
        return [
            build_lever_attempt("mark_plan_obsolete", ["aaa", "restated"], True, True),
            build_lever_attempt("mark_plan_obsolete", ["bbb", "null act"], True, False),
            build_lever_attempt("mark_plan_obsolete", ["ccc", "restated"], False, True),
        ]

    def _both(
        self,
    ) -> tuple[JsonlLeverAttemptsLedger, SqliteLeverAttemptsLedger]:
        jsonl = JsonlLeverAttemptsLedger(FAKE_DIR)
        sqlite = SqliteLeverAttemptsLedger(StateStore(":memory:"))
        for attempt in self._shared_attempts():
            jsonl.record(attempt)
            sqlite.record(attempt)
        return jsonl, sqlite

    def test_load_all_parity(self, mem_fs: dict[str, str]) -> None:
        jsonl, sqlite = self._both()
        assert jsonl.load_all() == sqlite.load_all()

    def test_read_already_true_parity(self, mem_fs: dict[str, str]) -> None:
        jsonl, sqlite = self._both()
        from_jsonl = read_already_true_plan_ids(jsonl)
        from_sqlite = read_already_true_plan_ids(sqlite)
        assert from_jsonl == from_sqlite
        assert from_jsonl == {"aaa", "ccc"}

    def test_selector_parity(self, mem_fs: dict[str, str]) -> None:
        mem = _make_memory()
        mem.save_evolution_plan(_plan("aaa", "2025-01-01T00:00:00Z"))
        mem.save_evolution_plan(_plan("ddd", "2025-01-02T00:00:00Z"))
        jsonl, sqlite = self._both()

        chosen_jsonl = select_distinct_obsolete_target(mem, jsonl)
        chosen_sqlite = select_distinct_obsolete_target(mem, sqlite)

        assert chosen_jsonl is not None
        assert chosen_sqlite is not None
        # "aaa" is already ``True`` in S, so the oldest *distinct* pick is "ddd".
        assert chosen_jsonl.plan_id == "ddd"
        assert chosen_sqlite.plan_id == "ddd"


# ---------------------------------------------------------------------------
# fire_directive_named_obsolete
# ---------------------------------------------------------------------------

_PID_A = "46e575f898de"
_PID_B = "0123456789ab"


def _ledger_rows(mem_fs: dict[str, str]) -> list[dict[str, object]]:
    raw = mem_fs.get(str(_LEDGER), "")
    return [json.loads(line) for line in raw.strip().splitlines() if line]


def test_directive_reason_fits_obsolete_reason_cap() -> None:
    """The fixed disposal reason stays under ``mark_plan_obsolete``'s cap."""
    assert len(lever_capture._DIRECTIVE_OBSOLETE_REASON) < _OBSOLETE_REASON_MAX_CHARS


def test_directive_single_pending_id_fires(mem_fs: dict[str, str]) -> None:
    """One named pending plan is retired, one True row recorded, FIRED."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(),
        f"Obsolete pending plan {_PID_A} via MARK_PLAN_OBSOLETE.",
    )

    assert outcome.kind == DirectiveObsoleteKind.FIRED
    assert outcome.plan_id == _PID_A
    plan = mem.get_plan(_PID_A)
    assert plan is not None
    assert plan.status == "obsolete"
    rows = _ledger_rows(mem_fs)
    assert len(rows) == 1
    assert rows[0]["result"] is True
    assert rows[0]["args"] == [_PID_A, lever_capture._DIRECTIVE_OBSOLETE_REASON]


def test_directive_without_lever_signature_is_noop(mem_fs: dict[str, str]) -> None:
    """A directive not naming the lever never fires."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(), f"Review plan {_PID_A} carefully.",
    )

    assert outcome.kind == DirectiveObsoleteKind.NOOP
    assert outcome.plan_id is None
    assert _ledger_rows(mem_fs) == []
    plan = mem.get_plan(_PID_A)
    assert plan is not None
    assert plan.status == "pending_review"


def test_directive_two_pending_ids_is_noop(mem_fs: dict[str, str]) -> None:
    """Two aimed pending plans are ambiguous — never guess which to retire."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z"))
    mem.save_evolution_plan(_plan(_PID_B, "2025-01-02T00:00:00Z"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(), f"mark_plan_obsolete {_PID_A} or {_PID_B}.",
    )

    assert outcome.kind == DirectiveObsoleteKind.NOOP
    assert _ledger_rows(mem_fs) == []
    for pid in (_PID_A, _PID_B):
        plan = mem.get_plan(pid)
        assert plan is not None
        assert plan.status == "pending_review"


def test_directive_rationale_cited_survivor_is_never_fired(
    mem_fs: dict[str, str],
) -> None:
    """"obsolete A as it restates B" with A gone never retires survivor B."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z", "obsolete"))
    mem.save_evolution_plan(_plan(_PID_B, "2025-01-02T00:00:00Z"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(), f"mark_plan_obsolete {_PID_A} as it restates {_PID_B}",
    )

    assert outcome.kind == DirectiveObsoleteKind.NOOP
    assert _ledger_rows(mem_fs) == []
    survivor = mem.get_plan(_PID_B)
    assert survivor is not None
    assert survivor.status == "pending_review"


def test_directive_keep_clause_is_not_aimed(mem_fs: dict[str, str]) -> None:
    """"obsolete A, keep its twin B" aims only at A — B is preserved."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z"))
    mem.save_evolution_plan(_plan(_PID_B, "2025-01-02T00:00:00Z"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(),
        f"mark_plan_obsolete {_PID_A}, keep its twin {_PID_B}.",
    )

    assert outcome.kind == DirectiveObsoleteKind.FIRED
    assert outcome.plan_id == _PID_A
    keeper = mem.get_plan(_PID_B)
    assert keeper is not None
    assert keeper.status == "pending_review"


def test_directive_negated_clause_never_fires(mem_fs: dict[str, str]) -> None:
    """"Do NOT mark_plan_obsolete <keeper>" never aims at the keeper."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(), f"Do NOT mark_plan_obsolete {_PID_A}; it is the keeper.",
    )

    assert outcome.kind == DirectiveObsoleteKind.NOOP
    assert _ledger_rows(mem_fs) == []
    plan = mem.get_plan(_PID_A)
    assert plan is not None
    assert plan.status == "pending_review"


@pytest.mark.parametrize("reason", [
    "as it is no longer needed",
    "because it is not relevant",
])
def test_directive_justification_negation_still_fires(
    mem_fs: dict[str, str], reason: str,
) -> None:
    """A "no longer needed" justification is a reason to obsolete, not a veto."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(), f"mark_plan_obsolete {_PID_A} {reason}.",
    )

    assert outcome.kind == DirectiveObsoleteKind.FIRED
    assert outcome.plan_id == _PID_A


def test_directive_spaced_negation_never_fires(mem_fs: dict[str, str]) -> None:
    """"<id> should never be marked obsolete" never aims at the id."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(), f"mark_plan_obsolete: plan {_PID_A} should never be marked obsolete.",
    )

    assert outcome.kind == DirectiveObsoleteKind.NOOP
    assert _ledger_rows(mem_fs) == []


def test_directive_incidental_id_is_not_stale(mem_fs: dict[str, str]) -> None:
    """An id outside any obsolete clause is never cleared as STALE."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z", "approved"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(),
        f"Implement approved plan {_PID_A}. Avoid mark_plan_obsolete churn.",
    )

    assert outcome.kind == DirectiveObsoleteKind.NOOP
    assert outcome.plan_id is None


def test_directive_single_already_obsolete_id_is_stale(
    mem_fs: dict[str, str],
) -> None:
    """A single named plan that left pending_review is STALE, no fire."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z", "obsolete"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(), f"mark_plan_obsolete {_PID_A} now.",
    )

    assert outcome.kind == DirectiveObsoleteKind.STALE
    assert outcome.plan_id == _PID_A
    assert _ledger_rows(mem_fs) == []


def test_directive_two_non_pending_ids_is_noop(mem_fs: dict[str, str]) -> None:
    """Several named ids, none pending, is not a single stale target."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z", "obsolete"))
    mem.save_evolution_plan(_plan(_PID_B, "2025-01-02T00:00:00Z", "rejected"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(), f"mark_plan_obsolete {_PID_A} and {_PID_B}.",
    )

    assert outcome.kind == DirectiveObsoleteKind.NOOP


def test_directive_unknown_or_missing_id_is_noop(mem_fs: dict[str, str]) -> None:
    """An unknown id, or no id at all, leaves the directive untouched."""
    mem = _make_memory()

    unknown = fire_directive_named_obsolete(
        mem, _ledger(), f"mark_plan_obsolete {_PID_A}.",
    )
    no_id = fire_directive_named_obsolete(
        mem, _ledger(), "mark_plan_obsolete the redundant plan.",
    )

    assert unknown.kind == DirectiveObsoleteKind.NOOP
    assert no_id.kind == DirectiveObsoleteKind.NOOP
    assert _ledger_rows(mem_fs) == []


def test_directive_forbidden_target_is_unfireable(mem_fs: dict[str, str]) -> None:
    """The known-null forbidden id is never fired, and the directive is dead."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(FORBIDDEN_OBSOLETE_TARGET, "2025-01-01T00:00:00Z"))

    outcome = fire_directive_named_obsolete(
        mem, _ledger(), f"mark_plan_obsolete {FORBIDDEN_OBSOLETE_TARGET}.",
    )

    assert outcome.kind == DirectiveObsoleteKind.UNFIREABLE
    assert outcome.plan_id == FORBIDDEN_OBSOLETE_TARGET
    assert _ledger_rows(mem_fs) == []


def test_directive_already_true_id_is_unfireable(mem_fs: dict[str, str]) -> None:
    """An id already recorded True in the ledger is never re-fired."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z"))
    _seed_ledger(mem_fs, [_true_row(_PID_A)])

    outcome = fire_directive_named_obsolete(
        mem, _ledger(), f"mark_plan_obsolete {_PID_A}.",
    )

    assert outcome.kind == DirectiveObsoleteKind.UNFIREABLE
    assert outcome.plan_id == _PID_A
    assert len(_ledger_rows(mem_fs)) == 1


def test_directive_null_act_records_false_and_is_noop(
    mem_fs: dict[str, str],
) -> None:
    """A lever returning True without persisting is captured False, not FIRED."""
    mem = _make_memory()
    mem.save_evolution_plan(_plan(_PID_A, "2025-01-01T00:00:00Z"))

    with patch.object(mem, "mark_plan_obsolete", return_value=True):
        outcome = fire_directive_named_obsolete(
            mem, _ledger(), f"mark_plan_obsolete {_PID_A}.",
        )

    assert outcome.kind == DirectiveObsoleteKind.NOOP
    rows = _ledger_rows(mem_fs)
    assert len(rows) == 1
    assert rows[0]["result"] is False
    assert rows[0]["verified"] is True

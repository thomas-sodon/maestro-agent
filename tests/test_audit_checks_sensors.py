"""Gate-activation trace, phantom-landing divergence, directive compliance, task
origin and outcome-trend sensors; split out of ``tests/test_audit_checks.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro.audit_checks import (
    _VERIFIABLE_MARKERS,
    GateActivation,
    _check_criteria_verifiability,
    classify_task_origin,
    format_false_success_recurrence,
    format_known_state_summary,
    format_origin_summary,
    format_outcome_trend_summary,
    goal_opens_or_merges_pr,
    measure_false_success_recurrence,
    measure_outcome_trends,
    measure_pr_lineage_recurrence,
    measure_success_by_origin,
)
from maestro.state_store import (
    SqliteGateActivationLedger,
    StateStore,
)
from tests.test_audit_checks import (
    FAKE_DIR,
    _gate_backend,
    _write_gate_activations,
)

# ---------------------------------------------------------------------------
# Gate-activation trace: log_gate_activation / measure_gate_triggers
# ---------------------------------------------------------------------------


def test_log_gate_activation_writes_jsonl_record(
    mem_fs: dict[str, str],
) -> None:
    """A single call writes a valid JSONL record with all expected fields."""
    import json as json_mod

    from maestro.audit_checks import log_gate_activation

    log_gate_activation(_gate_backend(), gate="audit_stall", action="blocked", detail="fired 3x")
    path = str(FAKE_DIR / "gate_activations.jsonl")
    assert path in mem_fs
    records = [json_mod.loads(line) for line in mem_fs[path].strip().splitlines()]
    assert len(records) == 1
    r = records[0]
    assert r["gate"] == "audit_stall"
    assert r["action"] == "blocked"
    assert r["detail"] == "fired 3x"
    assert "timestamp" in r


def test_log_gate_activation_appends_multiple_records(
    mem_fs: dict[str, str],
) -> None:
    """Multiple calls produce multiple lines."""
    import json as json_mod

    from maestro.audit_checks import log_gate_activation

    log_gate_activation(_gate_backend(), gate="cost_risk", action="blocked", detail="high cost")
    log_gate_activation(_gate_backend(), gate="terrain_exhaustion", action="rejected", detail="stale terrain")
    log_gate_activation(_gate_backend(), gate="cost_risk", action="blocked", detail="again")
    path = str(FAKE_DIR / "gate_activations.jsonl")
    records = [json_mod.loads(line) for line in mem_fs[path].strip().splitlines()]
    assert len(records) == 3
    assert records[0]["gate"] == "cost_risk"
    assert records[1]["gate"] == "terrain_exhaustion"
    assert records[2]["gate"] == "cost_risk"


def test_log_gate_activation_truncates_detail(
    mem_fs: dict[str, str],
) -> None:
    """Long detail strings are truncated to 200 characters."""
    import json as json_mod

    from maestro.audit_checks import log_gate_activation

    long_detail = "x" * 500
    log_gate_activation(_gate_backend(), gate="g", action="a", detail=long_detail)
    path = str(FAKE_DIR / "gate_activations.jsonl")
    record = json_mod.loads(mem_fs[path].strip())
    assert len(record["detail"]) == 200


def test_measure_gate_triggers_counts_per_gate(
    mem_fs: dict[str, str],
) -> None:
    """measure_gate_triggers returns correct per-gate counts."""
    import json as json_mod

    from maestro.audit_checks import measure_gate_triggers

    path = str(FAKE_DIR / "gate_activations.jsonl")
    lines = [
        json_mod.dumps({"gate": "audit_stall", "action": "blocked", "timestamp": "t", "detail": "d"}),
        json_mod.dumps({"gate": "terrain_exhaustion", "action": "rejected", "timestamp": "t", "detail": "d"}),
        json_mod.dumps({"gate": "audit_stall", "action": "blocked", "timestamp": "t", "detail": "d"}),
        json_mod.dumps({"gate": "cost_risk", "action": "blocked", "timestamp": "t", "detail": "d"}),
    ]
    mem_fs[path] = "\n".join(lines) + "\n"
    counts = measure_gate_triggers(_gate_backend(), n=100)
    assert counts == {"audit_stall": 2, "terrain_exhaustion": 1, "cost_risk": 1}


def test_measure_gate_triggers_respects_n_window(
    mem_fs: dict[str, str],
) -> None:
    """Only the tail *n* records are counted."""
    import json as json_mod

    from maestro.audit_checks import measure_gate_triggers

    path = str(FAKE_DIR / "gate_activations.jsonl")
    lines = [
        json_mod.dumps({"gate": "old_gate", "action": "a", "timestamp": "t", "detail": "d"}),
        json_mod.dumps({"gate": "new_gate", "action": "a", "timestamp": "t", "detail": "d"}),
        json_mod.dumps({"gate": "new_gate", "action": "a", "timestamp": "t", "detail": "d"}),
    ]
    mem_fs[path] = "\n".join(lines) + "\n"
    counts = measure_gate_triggers(_gate_backend(), n=2)
    assert counts == {"new_gate": 2}
    assert "old_gate" not in counts


def test_measure_gate_triggers_missing_file(
    mem_fs: dict[str, str],
) -> None:
    """Missing file returns empty dict."""
    from maestro.audit_checks import measure_gate_triggers

    counts = measure_gate_triggers(_gate_backend(), n=100)
    assert counts == {}


def test_measure_gate_triggers_empty_file(
    mem_fs: dict[str, str],
) -> None:
    """Empty file returns empty dict."""
    from maestro.audit_checks import measure_gate_triggers

    path = str(FAKE_DIR / "gate_activations.jsonl")
    mem_fs[path] = ""
    counts = measure_gate_triggers(_gate_backend(), n=100)
    assert counts == {}


def test_measure_gate_triggers_skips_malformed_lines(
    mem_fs: dict[str, str],
) -> None:
    """Malformed lines are silently skipped."""
    import json as json_mod

    from maestro.audit_checks import measure_gate_triggers

    path = str(FAKE_DIR / "gate_activations.jsonl")
    lines = [
        json_mod.dumps({"gate": "good", "action": "a", "timestamp": "t", "detail": "d"}),
        "NOT VALID JSON",
        json_mod.dumps({"gate": "also_good", "action": "a", "timestamp": "t", "detail": "d"}),
    ]
    mem_fs[path] = "\n".join(lines) + "\n"
    counts = measure_gate_triggers(_gate_backend(), n=100)
    assert counts == {"good": 1, "also_good": 1}


def test_measure_gate_triggers_oserror_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """When the trace read raises OSError, measure_gate_triggers returns {}."""
    from maestro.audit_checks import measure_gate_triggers

    path = str(FAKE_DIR / "gate_activations.jsonl")
    mem_fs[path] = "valid content"  # File exists so path.exists() returns True

    with patch.object(Path, "read_text", side_effect=OSError("disk error")):
        counts = measure_gate_triggers(_gate_backend(), n=100)
    assert counts == {}


def test_measure_gate_triggers_skips_blank_lines(
    mem_fs: dict[str, str],
) -> None:
    """Blank lines interspersed with valid records are silently skipped."""
    import json as json_mod

    from maestro.audit_checks import measure_gate_triggers

    path = str(FAKE_DIR / "gate_activations.jsonl")
    lines = [
        json_mod.dumps({"gate": "g1", "action": "a", "timestamp": "t", "detail": "d"}),
        "",
        "   ",
        json_mod.dumps({"gate": "g2", "action": "a", "timestamp": "t", "detail": "d"}),
    ]
    mem_fs[path] = "\n".join(lines) + "\n"
    counts = measure_gate_triggers(_gate_backend(), n=100)
    assert counts == {"g1": 1, "g2": 1}


def test_measure_gate_triggers_skips_non_object_lines(
    mem_fs: dict[str, str],
) -> None:
    """Valid-JSON but non-object lines (scalar/array) are skipped, not crashed.

    A partial/corrupted append can leave a line that parses as JSON yet is not
    an object — a bare number, string, or array. ``record.get(...)`` would raise
    AttributeError on such a value; the reader must tolerate it like any other
    corrupted-tail line rather than crash.
    """
    import json as json_mod

    from maestro.audit_checks import measure_gate_triggers

    path = str(FAKE_DIR / "gate_activations.jsonl")
    lines = [
        json_mod.dumps({"gate": "good", "action": "a", "timestamp": "t", "detail": "d"}),
        "123",
        '"a bare string"',
        "[1, 2, 3]",
        json_mod.dumps({"gate": "also_good", "action": "a", "timestamp": "t", "detail": "d"}),
    ]
    mem_fs[path] = "\n".join(lines) + "\n"
    counts = measure_gate_triggers(_gate_backend(), n=100)
    # Negative control: only the two object rows count; the three non-object
    # lines neither crash the reader nor contribute a phantom key.
    assert counts == {"good": 1, "also_good": 1}


def test_measure_gate_triggers_skips_field_incomplete_rows(
    mem_fs: dict[str, str],
) -> None:
    """A dict row missing required GateActivation fields is dropped (not crashed).

    Exercises the JSONL read's structural-corruption tolerance: a well-formed
    JSON object that lacks ``action`` / ``timestamp`` / ``detail`` cannot hydrate
    into a :class:`GateActivation`, so ``_parse_gate_activation_dict`` returns
    ``None`` and the reader skips it — matching the SQLite path's per-row drop.
    """
    import json as json_mod

    from maestro.audit_checks import measure_gate_triggers

    path = str(FAKE_DIR / "gate_activations.jsonl")
    lines = [
        json_mod.dumps({"gate": "good", "action": "a", "timestamp": "t", "detail": "d"}),
        json_mod.dumps({"gate": "incomplete"}),  # missing action/timestamp/detail
    ]
    mem_fs[path] = "\n".join(lines) + "\n"
    counts = measure_gate_triggers(_gate_backend(), n=100)
    assert counts == {"good": 1}


def test_measure_gate_triggers_backend_parity_jsonl_vs_sqlite(
    mem_fs: dict[str, str],
) -> None:
    """The count is identical whether the trace is JSONL- or SQLite-backed.

    The flag-flip invariance the fold guarantees (proposal 106): a shared corpus
    fed to both a :class:`JsonlGateActivationLedger` and an in-memory
    :class:`SqliteGateActivationLedger` yields the same ``measure_gate_triggers``
    output, so ``[paths] sqlite_state_enabled`` never changes what the sensor sees.
    """
    from maestro.audit_checks import measure_gate_triggers

    corpus = [
        GateActivation(gate="g1", action="a", timestamp="t", detail="d"),
        GateActivation(gate="g1", action="a", timestamp="t", detail="d"),
        GateActivation(gate="g2", action="a", timestamp="t", detail="d"),
    ]
    jsonl = _gate_backend(Path("/fake/parity-reader"))
    sqlite = SqliteGateActivationLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        for record in corpus:
            backend.append(record)
    assert measure_gate_triggers(jsonl, n=100) == {"g1": 2, "g2": 1}
    assert measure_gate_triggers(sqlite, n=100) == measure_gate_triggers(jsonl, n=100)


# ---------------------------------------------------------------------------
# Bounded tail read: load_recent_gate_activations / read_tail (#2566 review)
# ---------------------------------------------------------------------------


def test_load_recent_gate_activations_returns_bounded_tail(
    mem_fs: dict[str, str],
) -> None:
    """Only the newest *limit* rows are returned, oldest-first."""
    from maestro.audit_checks import load_recent_gate_activations

    backend = _gate_backend(FAKE_DIR)
    for i in range(4):
        backend.append(
            GateActivation(gate=f"g{i}", action="blocked", timestamp="t", detail="d"),
        )
    tail = load_recent_gate_activations(FAKE_DIR, 2)
    assert [r.gate for r in tail] == ["g2", "g3"]
    # The ledger's read_tail delegates to the same reader.
    assert backend.read_tail(2) == tail


def test_load_recent_gate_activations_nonpositive_limit_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """A limit of zero or negative short-circuits to an empty list."""
    from maestro.audit_checks import load_recent_gate_activations

    backend = _gate_backend(FAKE_DIR)
    backend.append(
        GateActivation(gate="g", action="blocked", timestamp="t", detail="d"),
    )
    assert load_recent_gate_activations(FAKE_DIR, 0) == []
    assert load_recent_gate_activations(FAKE_DIR, -3) == []


def test_load_recent_gate_activations_missing_file_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """An absent trace file yields an empty tail, not a crash."""
    from maestro.audit_checks import load_recent_gate_activations

    assert load_recent_gate_activations(FAKE_DIR, 5) == []


def test_load_recent_gate_activations_oserror_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """When the tail read raises OSError the reader returns an empty list."""
    from maestro.audit_checks import load_recent_gate_activations

    path = str(FAKE_DIR / "gate_activations.jsonl")
    mem_fs[path] = "valid content"  # exists so path.exists() is True
    with patch.object(Path, "read_text", side_effect=OSError("disk error")):
        assert load_recent_gate_activations(FAKE_DIR, 5) == []


def test_load_recent_gate_activations_skips_corrupt_tail_rows(
    mem_fs: dict[str, str],
) -> None:
    """Blank, non-JSON, non-object, and field-short rows in the tail are dropped."""
    import json as json_mod

    from maestro.audit_checks import load_recent_gate_activations

    path = str(FAKE_DIR / "gate_activations.jsonl")
    good = json_mod.dumps(
        {"gate": "g_ok", "action": "blocked", "timestamp": "t", "detail": "d"},
    )
    lines = [
        good,
        "",  # blank → filtered before the window
        "not-json",  # JSONDecodeError → skipped
        "42",  # valid JSON but non-object → skipped
        json_mod.dumps({"gate": "only-gate"}),  # missing fields → parser None
    ]
    mem_fs[path] = "\n".join(lines) + "\n"
    # Window of 4 raw non-blank lines: the three corrupt ones drop, leaving one.
    tail = load_recent_gate_activations(FAKE_DIR, 4)
    assert [r.gate for r in tail] == ["g_ok"]


def test_gate_activation_read_tail_jsonl_vs_sqlite_parity(
    mem_fs: dict[str, str],
) -> None:
    """The bounded tail read is identical on both substrates (proposal 106)."""
    corpus = [
        GateActivation(gate=f"g{i}", action="blocked", timestamp="t", detail="d")
        for i in range(5)
    ]
    jsonl = _gate_backend(Path("/fake/tail-parity"))
    sqlite = SqliteGateActivationLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        for record in corpus:
            backend.append(record)
    assert jsonl.read_tail(3) == sqlite.read_tail(3)
    assert [r.gate for r in jsonl.read_tail(3)] == ["g2", "g3", "g4"]
    # A limit beyond the trace length returns the whole (short) trace on both.
    assert jsonl.read_tail(99) == sqlite.read_tail(99) == corpus


# ---------------------------------------------------------------------------
# Phantom-landing divergence sensor: measure_phantom_landing_rate (proposal 105)
# ---------------------------------------------------------------------------


def test_mechanical_outcome_authority_tokens_are_stable() -> None:
    """The shadow-ledger token values must stay byte-stable.

    The prior increment's writer stamped these strings inline; the constants
    only refactor the source-of-truth, so their *values* must not change or the
    reader would stop matching rows already appended to the ledger.
    """
    from maestro.audit_checks import (
        MECHANICAL_OUTCOME_AGREE_ACTION,
        MECHANICAL_OUTCOME_AUTHORITY_GATE,
        MECHANICAL_OUTCOME_DIVERGED_ACTION,
    )

    assert MECHANICAL_OUTCOME_AUTHORITY_GATE == "mechanical_outcome_authority"
    assert MECHANICAL_OUTCOME_DIVERGED_ACTION == "diverged"
    assert MECHANICAL_OUTCOME_AGREE_ACTION == "agree"


def test_measure_phantom_landing_rate_missing_file(
    mem_fs: dict[str, str],
) -> None:
    """No gate_activations.jsonl yields the all-zero result."""
    from maestro.audit_checks_phantom import (
        measure_phantom_landing_rate,
    )

    result = measure_phantom_landing_rate(_gate_backend())
    assert result == {
        "total": 0,
        "diverged": 0,
        "agree": 0,
        "divergence_rate": 0.0,
    }


def test_measure_phantom_landing_rate_empty_file(
    mem_fs: dict[str, str],
) -> None:
    """An empty (present) ledger also yields the all-zero result."""
    from maestro.audit_checks_phantom import (
        measure_phantom_landing_rate,
    )

    path = str(FAKE_DIR / "gate_activations.jsonl")
    mem_fs[path] = ""
    result = measure_phantom_landing_rate(_gate_backend())
    assert result == {
        "total": 0,
        "diverged": 0,
        "agree": 0,
        "divergence_rate": 0.0,
    }


def test_measure_phantom_landing_rate_counts_and_rate(
    mem_fs: dict[str, str],
) -> None:
    """A mix of diverged/agree shadow rows yields exact counts and rate."""
    from maestro.audit_checks import (
        MECHANICAL_OUTCOME_AGREE_ACTION,
        MECHANICAL_OUTCOME_AUTHORITY_GATE,
        MECHANICAL_OUTCOME_DIVERGED_ACTION,
    )
    from maestro.audit_checks_phantom import (
        measure_phantom_landing_rate,
    )

    gate = MECHANICAL_OUTCOME_AUTHORITY_GATE
    div = MECHANICAL_OUTCOME_DIVERGED_ACTION
    agr = MECHANICAL_OUTCOME_AGREE_ACTION
    _write_gate_activations(
        mem_fs,
        [
            {"gate": gate, "action": div, "timestamp": "t", "detail": "d"},
            {"gate": gate, "action": agr, "timestamp": "t", "detail": "d"},
            {"gate": gate, "action": agr, "timestamp": "t", "detail": "d"},
            {"gate": gate, "action": div, "timestamp": "t", "detail": "d"},
        ],
    )
    result = measure_phantom_landing_rate(_gate_backend(), n=100)
    assert result["total"] == 4
    assert result["diverged"] == 2
    assert result["agree"] == 2
    assert result["divergence_rate"] == 0.5
    # Negative controls: not the all-agree 0.0 nor the all-diverged 1.0 rate.
    assert result["divergence_rate"] != 0.0
    assert result["divergence_rate"] != 1.0


def test_measure_phantom_landing_rate_all_agree_is_zero(
    mem_fs: dict[str, str],
) -> None:
    """All-agree rows give a 0.0 divergence rate (the honest-narration case)."""
    from maestro.audit_checks import (
        MECHANICAL_OUTCOME_AGREE_ACTION,
        MECHANICAL_OUTCOME_AUTHORITY_GATE,
    )
    from maestro.audit_checks_phantom import (
        measure_phantom_landing_rate,
    )

    gate = MECHANICAL_OUTCOME_AUTHORITY_GATE
    agr = MECHANICAL_OUTCOME_AGREE_ACTION
    _write_gate_activations(
        mem_fs,
        [
            {"gate": gate, "action": agr, "timestamp": "t", "detail": "d"},
            {"gate": gate, "action": agr, "timestamp": "t", "detail": "d"},
        ],
    )
    result = measure_phantom_landing_rate(_gate_backend(), n=100)
    assert result["total"] == 2
    assert result["agree"] == 2
    # Negative control: diverged is genuinely zero, not merely a zero rate.
    assert result["diverged"] == 0
    assert result["divergence_rate"] == 0.0


def test_measure_phantom_landing_rate_ignores_other_gates(
    mem_fs: dict[str, str],
) -> None:
    """Rows from other gates never enter the denominator, even same-action."""
    from maestro.audit_checks import (
        MECHANICAL_OUTCOME_AUTHORITY_GATE,
        MECHANICAL_OUTCOME_DIVERGED_ACTION,
    )
    from maestro.audit_checks_phantom import (
        measure_phantom_landing_rate,
    )

    gate = MECHANICAL_OUTCOME_AUTHORITY_GATE
    div = MECHANICAL_OUTCOME_DIVERGED_ACTION
    _write_gate_activations(
        mem_fs,
        [
            {"gate": "audit_stall", "action": "blocked", "timestamp": "t", "detail": "d"},
            {"gate": gate, "action": div, "timestamp": "t", "detail": "d"},
            # A foreign gate that even reuses the "diverged" action must not leak.
            {"gate": "cost_risk", "action": div, "timestamp": "t", "detail": "d"},
        ],
    )
    result = measure_phantom_landing_rate(_gate_backend(), n=100)
    assert result["total"] == 1
    assert result["diverged"] == 1
    assert result["agree"] == 0
    assert result["divergence_rate"] == 1.0
    # Negative control: had the two foreign rows leaked in, total would be 3.
    assert result["total"] != 3


def test_measure_phantom_landing_rate_respects_last_window(
    mem_fs: dict[str, str],
) -> None:
    """--last N bounds the denominator to the last N *shadow* rows.

    Foreign-gate rows interleaved between shadow rows must not consume window
    slots — the window is over matching-gate rows, not raw file lines.
    """
    from maestro.audit_checks import (
        MECHANICAL_OUTCOME_AGREE_ACTION,
        MECHANICAL_OUTCOME_AUTHORITY_GATE,
        MECHANICAL_OUTCOME_DIVERGED_ACTION,
    )
    from maestro.audit_checks_phantom import (
        measure_phantom_landing_rate,
    )

    gate = MECHANICAL_OUTCOME_AUTHORITY_GATE
    div = MECHANICAL_OUTCOME_DIVERGED_ACTION
    agr = MECHANICAL_OUTCOME_AGREE_ACTION
    _write_gate_activations(
        mem_fs,
        [
            {"gate": gate, "action": agr, "timestamp": "t", "detail": "d"},
            {"gate": "audit_stall", "action": "blocked", "timestamp": "t", "detail": "d"},
            {"gate": gate, "action": agr, "timestamp": "t", "detail": "d"},
            {"gate": gate, "action": agr, "timestamp": "t", "detail": "d"},
            {"gate": "audit_stall", "action": "blocked", "timestamp": "t", "detail": "d"},
            {"gate": gate, "action": div, "timestamp": "t", "detail": "d"},
            {"gate": gate, "action": div, "timestamp": "t", "detail": "d"},
        ],
    )
    # Window of the last 2 shadow rows: both diverged.
    windowed = measure_phantom_landing_rate(_gate_backend(), n=2)
    assert windowed["total"] == 2
    assert windowed["diverged"] == 2
    assert windowed["divergence_rate"] == 1.0
    # Negative control: over all 5 shadow rows the rate drops to 2/5, proving n
    # actually bounded the denominator (and interleaved gates did not dilute it).
    full = measure_phantom_landing_rate(_gate_backend(), n=100)
    assert full["total"] == 5
    assert full["diverged"] == 2
    assert full["divergence_rate"] == 0.4


def test_measure_phantom_landing_rate_tolerates_malformed_line(
    mem_fs: dict[str, str],
) -> None:
    """A malformed JSON line is skipped; surrounding shadow rows still count."""
    from maestro.audit_checks import (
        MECHANICAL_OUTCOME_AGREE_ACTION,
        MECHANICAL_OUTCOME_AUTHORITY_GATE,
        MECHANICAL_OUTCOME_DIVERGED_ACTION,
    )
    from maestro.audit_checks_phantom import (
        measure_phantom_landing_rate,
    )

    gate = MECHANICAL_OUTCOME_AUTHORITY_GATE
    good_div = json.dumps(
        {"gate": gate, "action": MECHANICAL_OUTCOME_DIVERGED_ACTION,
         "timestamp": "t", "detail": "d"},
    )
    good_agr = json.dumps(
        {"gate": gate, "action": MECHANICAL_OUTCOME_AGREE_ACTION,
         "timestamp": "t", "detail": "d"},
    )
    path = str(FAKE_DIR / "gate_activations.jsonl")
    mem_fs[path] = good_div + "\n{ not valid json\n" + good_agr + "\n"
    result = measure_phantom_landing_rate(_gate_backend(), n=100)
    assert result["total"] == 2
    assert result["diverged"] == 1
    assert result["agree"] == 1
    assert result["divergence_rate"] == 0.5


def test_measure_phantom_landing_rate_skips_blank_lines(
    mem_fs: dict[str, str],
) -> None:
    """Blank lines interspersed with valid shadow rows are silently skipped."""
    from maestro.audit_checks import (
        MECHANICAL_OUTCOME_AGREE_ACTION,
        MECHANICAL_OUTCOME_AUTHORITY_GATE,
        MECHANICAL_OUTCOME_DIVERGED_ACTION,
    )
    from maestro.audit_checks_phantom import (
        measure_phantom_landing_rate,
    )

    gate = MECHANICAL_OUTCOME_AUTHORITY_GATE
    good_div = json.dumps(
        {"gate": gate, "action": MECHANICAL_OUTCOME_DIVERGED_ACTION,
         "timestamp": "t", "detail": "d"},
    )
    good_agr = json.dumps(
        {"gate": gate, "action": MECHANICAL_OUTCOME_AGREE_ACTION,
         "timestamp": "t", "detail": "d"},
    )
    path = str(FAKE_DIR / "gate_activations.jsonl")
    mem_fs[path] = good_div + "\n\n   \n" + good_agr + "\n"
    result = measure_phantom_landing_rate(_gate_backend(), n=100)
    assert result["total"] == 2
    assert result["diverged"] == 1
    assert result["agree"] == 1


def test_measure_phantom_landing_rate_tolerates_non_object_line(
    mem_fs: dict[str, str],
) -> None:
    """Valid-JSON but non-object lines are skipped, not crashed.

    A corrupted/partial append can leave a line that parses as JSON yet is not
    an object (a bare number, string, or array). ``record.get(...)`` would raise
    AttributeError on such a value — caught by neither the JSONDecodeError nor
    the OSError handler — so the sensor must skip it like any other
    corrupted-tail line while still counting the surrounding shadow rows.
    """
    from maestro.audit_checks import (
        MECHANICAL_OUTCOME_AGREE_ACTION,
        MECHANICAL_OUTCOME_AUTHORITY_GATE,
        MECHANICAL_OUTCOME_DIVERGED_ACTION,
    )
    from maestro.audit_checks_phantom import (
        measure_phantom_landing_rate,
    )

    gate = MECHANICAL_OUTCOME_AUTHORITY_GATE
    good_div = json.dumps(
        {"gate": gate, "action": MECHANICAL_OUTCOME_DIVERGED_ACTION,
         "timestamp": "t", "detail": "d"},
    )
    good_agr = json.dumps(
        {"gate": gate, "action": MECHANICAL_OUTCOME_AGREE_ACTION,
         "timestamp": "t", "detail": "d"},
    )
    path = str(FAKE_DIR / "gate_activations.jsonl")
    mem_fs[path] = (
        good_div + "\n123\n" + '"a bare string"\n' + "[1, 2, 3]\n" + good_agr + "\n"
    )
    result = measure_phantom_landing_rate(_gate_backend(), n=100)
    # Negative control: only the two object shadow rows count; the three
    # non-object lines are tolerated (not crashed) and contribute nothing.
    assert result["total"] == 2
    assert result["diverged"] == 1
    assert result["agree"] == 1
    assert result["divergence_rate"] == 0.5


def test_measure_phantom_landing_rate_oserror_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """When the trace read raises OSError, the sensor returns the all-zero result."""
    from maestro.audit_checks_phantom import (
        measure_phantom_landing_rate,
    )

    path = str(FAKE_DIR / "gate_activations.jsonl")
    # File must exist so path.exists() is True and we reach the read_text call.
    mem_fs[path] = "valid content"

    with patch.object(Path, "read_text", side_effect=OSError("disk error")):
        result = measure_phantom_landing_rate(_gate_backend(), n=100)
    assert result == {
        "total": 0,
        "diverged": 0,
        "agree": 0,
        "divergence_rate": 0.0,
    }


def _write_task_history(
    mem_fs: dict[str, str],
    records: list[dict[str, str]],
) -> None:
    """Helper: write task-history records to mem_fs."""
    import json as json_mod

    path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[path] = "\n".join(json_mod.dumps(r) for r in records) + "\n"


def test_measure_directive_compliance_empty_no_file(
    mem_fs: dict[str, str],
) -> None:
    """No gate_activations.jsonl yields zero total and 0.0 rate (case a)."""
    from maestro.audit_checks import measure_directive_compliance

    result = measure_directive_compliance(_gate_backend(), FAKE_DIR)
    assert result["total_activations"] == 0
    assert result["compliant_count"] == 0
    assert result["compliance_rate"] == 0.0
    assert result["per_gate"] == {}


def test_measure_directive_compliance_no_tasks_file(
    mem_fs: dict[str, str],
) -> None:
    """Directive activations but no task history -> zero denominator."""
    from maestro.audit_checks import measure_directive_compliance

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    result = measure_directive_compliance(_gate_backend(), FAKE_DIR)
    assert result["total_activations"] == 0
    assert result["compliance_rate"] == 0.0


def test_format_directive_compliance_summary_below_minimum(
    mem_fs: dict[str, str],
) -> None:
    """Fewer than 3 directive activations -> empty string (case b)."""
    from maestro.audit_checks import format_directive_compliance_summary

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
            {
                "gate": "partial_construction",
                "action": "directive_set",
                "timestamp": "2026-06-01T02:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    _write_task_history(
        mem_fs,
        [
            {
                "goal": "build a new measurement instrument",
                "timestamp": "2026-06-01T01:00:00+00:00",
            },
            {
                "goal": "implement the partial follow-up",
                "timestamp": "2026-06-01T03:00:00+00:00",
            },
        ],
    )
    assert format_directive_compliance_summary(_gate_backend(), FAKE_DIR) == ""


def test_measure_directive_compliance_full_compliance(
    mem_fs: dict[str, str],
) -> None:
    """All subsequent tasks contain intent keywords -> rate 1.0 (case c)."""
    from maestro.audit_checks import measure_directive_compliance

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
            {
                "gate": "partial_construction",
                "action": "directive_set",
                "timestamp": "2026-06-01T02:00:00+00:00",
                "detail": "d",
            },
            {
                "gate": "self_directed_success",
                "action": "directive_set",
                "timestamp": "2026-06-01T04:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    _write_task_history(
        mem_fs,
        [
            {
                "goal": "build a new instrument",
                "timestamp": "2026-06-01T01:00:00+00:00",
            },
            {
                "goal": "complete the partial task with a fix",
                "timestamp": "2026-06-01T03:00:00+00:00",
            },
            {
                "goal": "implement the bounded construction",
                "timestamp": "2026-06-01T05:00:00+00:00",
            },
        ],
    )
    result = measure_directive_compliance(_gate_backend(), FAKE_DIR)
    assert result["total_activations"] == 3
    assert result["compliant_count"] == 3
    assert result["compliance_rate"] == 1.0


def test_measure_directive_compliance_zero_compliance(
    mem_fs: dict[str, str],
) -> None:
    """No keyword matches -> rate 0.0 (case d)."""
    from maestro.audit_checks import measure_directive_compliance

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
            {
                "gate": "partial_construction",
                "action": "directive_set",
                "timestamp": "2026-06-01T02:00:00+00:00",
                "detail": "d",
            },
            {
                "gate": "self_directed_success",
                "action": "directive_set",
                "timestamp": "2026-06-01T04:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    _write_task_history(
        mem_fs,
        [
            {
                "goal": "measure the prediction calibration window",
                "timestamp": "2026-06-01T01:00:00+00:00",
            },
            {
                "goal": "investigate why questions stall",
                "timestamp": "2026-06-01T03:00:00+00:00",
            },
            {
                "goal": "analyze recent outcome trends only",
                "timestamp": "2026-06-01T05:00:00+00:00",
            },
        ],
    )
    result = measure_directive_compliance(_gate_backend(), FAKE_DIR)
    assert result["total_activations"] == 3
    assert result["compliant_count"] == 0
    assert result["compliance_rate"] == 0.0


def test_measure_directive_compliance_mixed_per_gate(
    mem_fs: dict[str, str],
) -> None:
    """Mixed gate types with partial compliance produce per_gate stats (case e)."""
    from maestro.audit_checks import measure_directive_compliance

    _write_gate_activations(
        mem_fs,
        [
            # investigation_streak: subsequent task complies (build).
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
            # investigation_streak: subsequent task does NOT comply.
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T02:00:00+00:00",
                "detail": "d",
            },
            # integration_cadence: subsequent task complies (merge).
            {
                "gate": "integration_cadence",
                "action": "directive_set",
                "timestamp": "2026-06-01T04:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    _write_task_history(
        mem_fs,
        [
            {
                "goal": "build a measurement helper",
                "timestamp": "2026-06-01T01:00:00+00:00",
            },
            {
                "goal": "reflect on identity drift",
                "timestamp": "2026-06-01T03:00:00+00:00",
            },
            {
                "goal": "merge the stale branch into main",
                "timestamp": "2026-06-01T05:00:00+00:00",
            },
        ],
    )
    result = measure_directive_compliance(_gate_backend(), FAKE_DIR)
    assert result["total_activations"] == 3
    assert result["compliant_count"] == 2
    per_gate = result["per_gate"]
    assert per_gate["investigation_streak"] == {"total": 2, "compliant": 1}
    assert per_gate["integration_cadence"] == {"total": 1, "compliant": 1}


def test_measure_directive_compliance_excludes_last_activation(
    mem_fs: dict[str, str],
) -> None:
    """An activation with no subsequent task is excluded from the denominator (case f)."""
    from maestro.audit_checks import measure_directive_compliance

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
            {
                "gate": "partial_construction",
                "action": "directive_set",
                "timestamp": "2026-06-01T02:00:00+00:00",
                "detail": "d",
            },
            # Fired after the last recorded task -> no subsequent task.
            {
                "gate": "self_directed_success",
                "action": "directive_set",
                "timestamp": "2026-06-01T09:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    _write_task_history(
        mem_fs,
        [
            {
                "goal": "build the instrument",
                "timestamp": "2026-06-01T01:00:00+00:00",
            },
            {
                "goal": "complete and fix the partial",
                "timestamp": "2026-06-01T03:00:00+00:00",
            },
        ],
    )
    result = measure_directive_compliance(_gate_backend(), FAKE_DIR)
    # Only two activations have a subsequent task; the 09:00 one is excluded.
    assert result["total_activations"] == 2
    assert result["compliant_count"] == 2
    assert "self_directed_success" not in result["per_gate"]


def test_measure_directive_compliance_ignores_non_directive_gates(
    mem_fs: dict[str, str],
) -> None:
    """Non-directive gates and non-directive actions are filtered out."""
    from maestro.audit_checks import measure_directive_compliance

    _write_gate_activations(
        mem_fs,
        [
            # Blocking gate — not a directive setter.
            {
                "gate": "audit_stall",
                "action": "blocked",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
            # Directive gate but wrong action.
            {
                "gate": "investigation_streak",
                "action": "exempted",
                "timestamp": "2026-06-01T02:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    _write_task_history(
        mem_fs,
        [
            {
                "goal": "build something",
                "timestamp": "2026-06-01T01:00:00+00:00",
            },
            {
                "goal": "build something else",
                "timestamp": "2026-06-01T03:00:00+00:00",
            },
        ],
    )
    result = measure_directive_compliance(_gate_backend(), FAKE_DIR)
    assert result["total_activations"] == 0


def test_format_directive_compliance_summary_renders_block(
    mem_fs: dict[str, str],
) -> None:
    """3+ activations render a DIRECTIVE COMPLIANCE line with breakdown."""
    from maestro.audit_checks import format_directive_compliance_summary

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
            {
                "gate": "partial_construction",
                "action": "directive_set",
                "timestamp": "2026-06-01T02:00:00+00:00",
                "detail": "d",
            },
            {
                "gate": "self_directed_success",
                "action": "directive_set",
                "timestamp": "2026-06-01T04:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    _write_task_history(
        mem_fs,
        [
            {
                "goal": "build the instrument",
                "timestamp": "2026-06-01T01:00:00+00:00",
            },
            {
                "goal": "complete and fix the partial",
                "timestamp": "2026-06-01T03:00:00+00:00",
            },
            {
                "goal": "just reflect quietly",
                "timestamp": "2026-06-01T05:00:00+00:00",
            },
        ],
    )
    summary = format_directive_compliance_summary(_gate_backend(), FAKE_DIR)
    assert summary.startswith("DIRECTIVE COMPLIANCE: 2/3 (67%)")
    assert "investigation_streak 1/1" in summary
    assert "self_directed_success 0/1" in summary


def test_format_known_state_summary_includes_directive_compliance(
    mem_fs: dict[str, str],
) -> None:
    """format_known_state_summary surfaces the section with 3+ activations (case g)."""
    from maestro.audit_checks import format_known_state_summary

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
            {
                "gate": "partial_construction",
                "action": "directive_set",
                "timestamp": "2026-06-01T02:00:00+00:00",
                "detail": "d",
            },
            {
                "gate": "self_directed_success",
                "action": "directive_set",
                "timestamp": "2026-06-01T04:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    _write_task_history(
        mem_fs,
        [
            {
                "goal": "build the instrument",
                "timestamp": "2026-06-01T01:00:00+00:00",
            },
            {
                "goal": "complete and fix the partial",
                "timestamp": "2026-06-01T03:00:00+00:00",
            },
            {
                "goal": "implement the bounded change",
                "timestamp": "2026-06-01T05:00:00+00:00",
            },
        ],
    )
    section = format_known_state_summary(
        {"compound_scope_gate_enabled": True},
        ["recent goal"],
        data_dir=FAKE_DIR,
        gate_activation_backend=_gate_backend(),
    )
    assert "DIRECTIVE COMPLIANCE" in section


def test_format_known_state_summary_no_directive_without_data_dir(
    mem_fs: dict[str, str],
) -> None:
    """Without data_dir the directive-compliance section is absent (pure path)."""
    from maestro.audit_checks import format_known_state_summary

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    section = format_known_state_summary(
        {"compound_scope_gate_enabled": True},
        ["recent goal"],
    )
    assert "DIRECTIVE COMPLIANCE" not in section


def test_measure_directive_compliance_gate_oserror(
    mem_fs: dict[str, str],
) -> None:
    """OSError reading the gate-activation trace returns the empty result."""
    from maestro.audit_checks import measure_directive_compliance

    path = str(FAKE_DIR / "gate_activations.jsonl")
    mem_fs[path] = "content"  # exists so path.exists() is True
    with patch.object(Path, "read_text", side_effect=OSError("disk error")):
        result = measure_directive_compliance(_gate_backend(), FAKE_DIR)
    assert result["total_activations"] == 0
    assert result["compliance_rate"] == 0.0


def test_measure_directive_compliance_skips_malformed_and_blank(
    mem_fs: dict[str, str],
) -> None:
    """Blank and malformed gate lines are skipped; valid records still count."""
    import json as json_mod

    from maestro.audit_checks import measure_directive_compliance

    gate_path = str(FAKE_DIR / "gate_activations.jsonl")
    lines = [
        "",
        "   ",
        "{not valid json",
        json_mod.dumps({
            "gate": "investigation_streak",
            "action": "directive_set",
            "timestamp": "2026-06-01T00:00:00+00:00",
            "detail": "d",
        }),
    ]
    mem_fs[gate_path] = "\n".join(lines) + "\n"
    _write_task_history(
        mem_fs,
        [{"goal": "build it", "timestamp": "2026-06-01T01:00:00+00:00"}],
    )
    result = measure_directive_compliance(_gate_backend(), FAKE_DIR)
    assert result["total_activations"] == 1
    assert result["compliant_count"] == 1


def test_measure_directive_compliance_n_zero(
    mem_fs: dict[str, str],
) -> None:
    """n <= 0 yields no activations and the empty result."""
    from maestro.audit_checks import measure_directive_compliance

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    result = measure_directive_compliance(_gate_backend(), FAKE_DIR, n=0)
    assert result["total_activations"] == 0


def test_load_directive_compliance_tasks_oserror(
    mem_fs: dict[str, str],
) -> None:
    """OSError reading task_history.jsonl returns an empty list."""
    from maestro.audit_checks import _load_directive_compliance_tasks

    path = str(FAKE_DIR / "task_history.jsonl")
    mem_fs[path] = "content"  # exists so path.exists() is True
    with patch("builtins.open", side_effect=OSError("disk error")):
        assert _load_directive_compliance_tasks(FAKE_DIR) == []


def test_load_directive_compliance_tasks_skips_bad_records(
    mem_fs: dict[str, str],
) -> None:
    """Blank, malformed, and field-incomplete task records are skipped."""
    import json as json_mod

    from maestro.audit_checks import _load_directive_compliance_tasks

    path = str(FAKE_DIR / "task_history.jsonl")
    lines = [
        "",
        "   ",
        "{bad json",
        json_mod.dumps({"goal": "no timestamp"}),
        json_mod.dumps({"timestamp": "2026-06-01T00:00:00+00:00"}),
        json_mod.dumps({
            "goal": "good record",
            "timestamp": "2026-06-01T01:00:00+00:00",
        }),
    ]
    mem_fs[path] = "\n".join(lines) + "\n"
    result = _load_directive_compliance_tasks(FAKE_DIR)
    assert result == [("2026-06-01T01:00:00+00:00", "good record")]


def test_load_directive_compliance_tasks_skips_empty_goal(
    mem_fs: dict[str, str],
) -> None:
    """An empty-string ``goal`` is degenerate and must be skipped.

    An empty goal can never match a directive keyword, so admitting it would
    inflate ``total_activations`` (the compliance-rate denominator) without
    ever incrementing ``compliant_count``, silently deflating the rate. The
    ``goal`` guard must check truthiness, matching the ``timestamp`` guard.
    """
    import json as json_mod

    from maestro.audit_checks import _load_directive_compliance_tasks

    path = str(FAKE_DIR / "task_history.jsonl")
    lines = [
        json_mod.dumps({
            "goal": "",
            "timestamp": "2026-06-01T00:00:00+00:00",
        }),
        json_mod.dumps({
            "goal": "real goal",
            "timestamp": "2026-06-01T01:00:00+00:00",
        }),
    ]
    mem_fs[path] = "\n".join(lines) + "\n"
    result = _load_directive_compliance_tasks(FAKE_DIR)
    assert result == [("2026-06-01T01:00:00+00:00", "real goal")]


def test_measure_directive_compliance_empty_goal_not_in_denominator(
    mem_fs: dict[str, str],
) -> None:
    """An empty-goal subsequent task must not deflate ``compliance_rate``.

    Reproduces the PR #2443 review defect (comment 3359795520): when the
    first task following a directive activation carries an empty-string
    ``goal``, the buggy guard (``isinstance(goal, str)`` without a
    truthiness check) admitted it as the "subsequent task". An empty goal
    can never match a directive keyword, so it increments
    ``total_activations`` (the denominator) but never
    ``compliant_count`` — silently deflating ``compliance_rate``.

    Here a directive activation is followed by an empty-goal task and then
    a genuinely compliant task. With the empty goal correctly skipped, the
    compliant task becomes the observed subsequent task: one activation,
    one compliant, rate 1.0. Under the defect the empty goal would be
    observed instead, yielding compliant_count 0 and compliance_rate 0.0.
    """
    from maestro.audit_checks import measure_directive_compliance

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            },
        ],
    )
    _write_task_history(
        mem_fs,
        [
            # Degenerate empty-goal task immediately after the activation.
            {
                "goal": "",
                "timestamp": "2026-06-01T01:00:00+00:00",
            },
            # The real subsequent task — matches the investigation_streak
            # keyword set ("build").
            {
                "goal": "build the new instrument",
                "timestamp": "2026-06-01T02:00:00+00:00",
            },
        ],
    )
    result = measure_directive_compliance(_gate_backend(), FAKE_DIR)
    assert result["total_activations"] == 1
    assert result["compliant_count"] == 1
    assert result["compliance_rate"] == 1.0


def test_investigation_rate_is_a_directive_gate_type() -> None:
    """``investigation_rate`` must be a recognized directive-setting gate.

    Reproduces the PR #2443 review defect (comments 3374096750 /
    3359797337): ``agent.generate_task`` logs the investigation-rate bridge
    via ``log_gate_activation(gate="investigation_rate",
    action="directive_set")`` (agent.py ~L5530), yet the gate name was
    absent from :data:`DIRECTIVE_GATE_TYPES` and
    :data:`DIRECTIVE_INTENT_KEYWORDS`. The two sets must stay in lockstep
    with the directive-set call sites, so a directive the rate bridge sets
    is never silently dropped from the compliance measurement.
    """
    from maestro.audit_checks import (
        DIRECTIVE_GATE_TYPES,
        DIRECTIVE_INTENT_KEYWORDS,
    )

    assert "investigation_rate" in DIRECTIVE_GATE_TYPES
    assert "investigation_rate" in DIRECTIVE_INTENT_KEYWORDS


def test_measure_directive_compliance_counts_investigation_rate(
    mem_fs: dict[str, str],
) -> None:
    """An ``investigation_rate`` directive activation is measured.

    Reproduces the PR #2443 review defect: because ``investigation_rate``
    was missing from :data:`DIRECTIVE_GATE_TYPES`, a directive it set was
    filtered out of :func:`measure_directive_compliance`, so the bridge's
    effectiveness was invisible. The rate bridge's directive text steers
    toward construction ("build, fix, wire, integrate, or promote a
    branch"); a subsequent construction-shaped task must be counted as one
    compliant activation.
    """
    from maestro.audit_checks import measure_directive_compliance

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_rate",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": (
                    "Next task must be construction-shaped: build, fix, "
                    "wire, integrate, or promote a branch."
                ),
            },
        ],
    )
    _write_task_history(
        mem_fs,
        [
            {
                "goal": "build the construction-shaped follow-up",
                "timestamp": "2026-06-01T01:00:00+00:00",
            },
        ],
    )
    result = measure_directive_compliance(_gate_backend(), FAKE_DIR)
    assert result["total_activations"] == 1
    assert result["compliant_count"] == 1
    assert result["compliance_rate"] == 1.0
    assert result["per_gate"]["investigation_rate"] == {
        "total": 1,
        "compliant": 1,
    }


# ---------------------------------------------------------------------------
# goal_opens_or_merges_pr — PR-mutation goal filter (issue #2280)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal",
    [
        "Promote branch maestro/task/3ce652 into main via PR",
        "Open a PR for the dashboard refactor",
        "open a pull request to merge the feature",
        "create a PR with the new tests",
        "merge the PR for issue #12",
        "submit a pull request",
        "gh pr create --title x",
        "gh pr merge 5",
        "Promote the branch via a PR",
    ],
)
def test_goal_opens_or_merges_pr_detects_pr_goals(goal: str) -> None:
    assert goal_opens_or_merges_pr(goal) is True


@pytest.mark.parametrize(
    "goal",
    [
        "Promote branch maestro/task/abc via source_task_id follow-up plan",
        "Fix the create_pr timeout bug in vcs.py",
        "Add a test for tend_evolution_prs orphan reconciliation",
        "Review the PR remediation routing logic",
        "Refactor the prepare step in planner.py",
        "Investigate why the journal omits dream digests",
        "",
    ],
)
def test_goal_opens_or_merges_pr_ignores_benign_goals(goal: str) -> None:
    assert goal_opens_or_merges_pr(goal) is False


# ---------------------------------------------------------------------------
# _check_criteria_verifiability — verifiability-marker heuristic
# ---------------------------------------------------------------------------


def test_check_criteria_verifiability_all_activity_verbs_returns_false() -> None:
    """Criteria containing only activity verbs (analyze, review, assess)
    and no verifiable markers return False."""
    criteria = [
        "Analyze the codebase to understand architectural patterns",
        "Review the current design and assess potential improvements",
        "Investigate the root cause and explore alternative approaches",
    ]
    assert _check_criteria_verifiability(criteria) is False


def test_check_criteria_verifiability_one_marker_returns_true() -> None:
    """A single verifiable marker in any one criterion makes the set pass."""
    criteria = [
        "Analyze the codebase to understand patterns",
        "New test file covers the cost_gate validation path",
    ]
    assert _check_criteria_verifiability(criteria) is True


def test_check_criteria_verifiability_mixed_abstract_concrete_returns_true() -> None:
    """Mixed abstract and concrete criteria pass — one marker suffices."""
    criteria = [
        "Explore the relationship between reflection depth and task outcome",
        "Function returns non-zero exit code when ruff reports E302",
        "Assess the impact of the change on overall system behaviour",
    ]
    assert _check_criteria_verifiability(criteria) is True


def test_check_criteria_verifiability_empty_list_returns_true() -> None:
    """Empty list is a vacuous pass — no criteria to check."""
    assert _check_criteria_verifiability([]) is True


def test_check_criteria_verifiability_whole_word_matching() -> None:
    """Whole-word matching: 'testified' does not match 'test'."""
    criteria = [
        "The developer testified that the architecture was reviewed thoroughly",
    ]
    assert _check_criteria_verifiability(criteria) is False


def test_check_criteria_verifiability_case_insensitive() -> None:
    """Case insensitivity: 'DELETED' matches the 'deleted' marker."""
    criteria = [
        "The obsolete configuration DELETED from the repository",
    ]
    assert _check_criteria_verifiability(criteria) is True


def test_check_criteria_verifiability_integration_validate_success_criteria() -> None:
    """Fourth heuristic rejects via validate_success_criteria when no
    criterion in the surviving batch has verifiable markers."""
    from maestro.evolution import EvolutionEngine

    # These criteria pass heuristics 1-3 (long enough, low filler ratio,
    # non-generic nouns) but fail heuristic 4 (no verifiable markers).
    criteria = [
        "Analyze the codebase to understand architectural patterns thoroughly",
        "Review the design philosophy and assess alignment with stated goals",
        "Investigate the relationship between reflection depth and outcome quality",
    ]
    result = EvolutionEngine.validate_success_criteria(criteria)
    assert result == []


def test_check_criteria_verifiability_markers_frozenset_size() -> None:
    """Sanity check: the markers frozenset has a reasonable number of entries."""
    assert len(_VERIFIABLE_MARKERS) >= 60
    assert len(_VERIFIABLE_MARKERS) <= 150


def test_check_criteria_verifiability_does_not_reject_good_criteria() -> None:
    """Good criteria with concrete verbs and nouns survive the fourth heuristic."""
    from maestro.evolution import EvolutionEngine

    criteria = [
        "cost_gate rule 3 counts both failure and partial outcomes in the numerator",
        "run_quality_gates returns non-zero exit code when ruff reports E302",
    ]
    result = EvolutionEngine.validate_success_criteria(criteria)
    assert len(result) == 2


def test_verifiability_handles_trailing_punctuation() -> None:
    """Canonical 'verifiable state' phrases with trailing punctuation accepted.

    Regression: ``set(criterion.lower().split())`` only splits on whitespace,
    leaving trailing periods attached to tokens (``"pass."``). The fix
    tokenises with ``re.findall(r"[a-zA-Z]+", ...)`` to strip punctuation,
    consistent with heuristics 1-3 in ``evolution.py``.
    """
    # Canonical 'verifiable state' phrases from the docstring with trailing
    # punctuation should still be accepted as verifiable.
    assert _check_criteria_verifiability(["All tests pass."]) is True
    assert _check_criteria_verifiability(["Code refactored."]) is True
    assert _check_criteria_verifiability(["PR merged."]) is True


# ---------------------------------------------------------------------------
# Task-origin classification
# ---------------------------------------------------------------------------


def test_classify_task_origin_pr_remediation() -> None:
    """PR remediation tasks are classified as external."""
    task: dict[str, object] = {"goal": "Remediate PR #123: fix lint errors"}
    assert classify_task_origin(task) == "external"


def test_classify_task_origin_github_issue() -> None:
    """Tasks referencing a GitHub issue are classified as external."""
    task: dict[str, object] = {"goal": "Investigate flaky test from #456"}
    assert classify_task_origin(task) == "external"


def test_classify_task_origin_branch_promotion() -> None:
    """Branch promotion tasks are classified as external."""
    task: dict[str, object] = {"goal": "Promote branch abc123 into main"}
    assert classify_task_origin(task) == "external"


def test_classify_task_origin_self_directed() -> None:
    """Tasks without external markers are classified as self_directed."""
    task: dict[str, object] = {"goal": "Add a new measurement instrument"}
    assert classify_task_origin(task) == "self_directed"


def test_classify_task_origin_missing_goal() -> None:
    """Tasks with no goal field default to self_directed."""
    task: dict[str, object] = {"result_summary": "did something"}
    assert classify_task_origin(task) == "self_directed"


def test_classify_task_origin_non_string_goal() -> None:
    """Tasks with non-string goal default to self_directed."""
    task: dict[str, object] = {"goal": None}
    assert classify_task_origin(task) == "self_directed"


def test_measure_success_by_origin(mem_fs: dict[str, str]) -> None:
    """Reads task_history.jsonl and computes per-origin success rates."""
    import json as json_mod

    data_dir = Path("/fake/data")
    path = str(data_dir / "task_history.jsonl")
    records = [
        {"goal": "Remediate PR #10: fix types", "outcome": "success"},
        {"goal": "Remediate PR #11: fix lint", "outcome": "failure"},
        {"goal": "Add BM25 ranking", "outcome": "success"},
        {"goal": "Investigate #99 regression", "outcome": "success"},
        {"goal": "Extend audit framework", "outcome": "failure"},
        {"goal": "Promote branch task42 into main", "outcome": "success"},
    ]
    mem_fs[path] = "\n".join(json_mod.dumps(r) for r in records) + "\n"

    result = measure_success_by_origin(data_dir, n=50)

    # External: PR #10 success, PR #11 failure, #99 success, promote success
    assert result["external"]["total"] == 4
    assert result["external"]["success"] == 3
    assert result["external"]["rate"] == 3 / 4

    # Self-directed: BM25 success, audit failure
    assert result["self_directed"]["total"] == 2
    assert result["self_directed"]["success"] == 1
    assert result["self_directed"]["rate"] == 1 / 2


def test_measure_success_by_origin_missing_file(mem_fs: dict[str, str]) -> None:
    """Returns zeroed buckets when task_history.jsonl is missing."""
    data_dir = Path("/fake/data")
    result = measure_success_by_origin(data_dir, n=50)
    assert result["external"]["total"] == 0
    assert result["self_directed"]["total"] == 0


def test_format_origin_summary(mem_fs: dict[str, str]) -> None:
    """Produces a non-empty string containing both origin labels."""
    import json as json_mod

    data_dir = Path("/fake/data")
    path = str(data_dir / "task_history.jsonl")
    records = [
        {"goal": "Remediate PR #1: fix stuff", "outcome": "success"},
        {"goal": "Build something new", "outcome": "failure"},
    ]
    mem_fs[path] = "\n".join(json_mod.dumps(r) for r in records) + "\n"

    summary = format_origin_summary(data_dir, n=50)
    assert "external" in summary
    assert "self_directed" in summary
    assert len(summary) > 0


def test_format_origin_summary_empty_when_no_tasks(mem_fs: dict[str, str]) -> None:
    """Returns empty string when no tasks exist."""
    data_dir = Path("/fake/data")
    assert format_origin_summary(data_dir, n=50) == ""


def test_measure_success_by_origin_malformed_lines(mem_fs: dict[str, str]) -> None:
    """Malformed lines and non-dict JSON are silently skipped."""
    import json as json_mod

    data_dir = Path("/fake/data")
    path = str(data_dir / "task_history.jsonl")
    lines = [
        "not valid json",
        "",
        json_mod.dumps(["a", "list"]),  # valid JSON but not a dict
        json_mod.dumps({"goal": "Remediate PR #5: fix", "outcome": "success"}),
    ]
    mem_fs[path] = "\n".join(lines) + "\n"

    result = measure_success_by_origin(data_dir, n=50)
    assert result["external"]["total"] == 1
    assert result["external"]["success"] == 1
    assert result["self_directed"]["total"] == 0


def test_measure_success_by_origin_oserror(mem_fs: dict[str, str]) -> None:
    """OSError when reading task_history.jsonl returns zeroed buckets."""
    data_dir = Path("/fake/data")
    path = str(data_dir / "task_history.jsonl")
    mem_fs[path] = "some content"  # File exists so path.exists() returns True

    with patch("builtins.open", side_effect=OSError("disk error")):
        result = measure_success_by_origin(data_dir, n=50)
    assert result["external"]["total"] == 0
    assert result["self_directed"]["total"] == 0


def _remediation_task(
    pr_number: int,
    outcome: str = "success",
) -> dict[str, object]:
    """Build a remediation task dict for false-success recurrence tests."""
    return {
        "goal": f"Remediate PR #{pr_number}: fix the failing CI gate",
        "outcome": outcome,
        "result_summary": "No remediation needed",
    }


class TestMeasureFalseSuccessRecurrence:
    """Group remediation outcomes by PR; count twice-stamped successes."""

    def test_pr_with_two_successes_is_counted(self) -> None:
        history: list[dict[str, object]] = [
            _remediation_task(101),
            _remediation_task(101),
        ]
        result = measure_false_success_recurrence(history, n=0)
        assert result["count"] == 1
        assert result["by_pr"] == {101: 2}

    def test_pr_with_one_success_not_counted(self) -> None:
        history: list[dict[str, object]] = [
            _remediation_task(202),
        ]
        result = measure_false_success_recurrence(history, n=0)
        assert result["count"] == 0
        assert result["by_pr"] == {}

    def test_empty_history_returns_zero(self) -> None:
        result = measure_false_success_recurrence([], n=0)
        assert result["count"] == 0
        assert result["by_pr"] == {}

    def test_non_remediation_tasks_ignored(self) -> None:
        history: list[dict[str, object]] = [
            {"goal": "Build a new audit gate", "outcome": "success"},
            {"goal": "Build a new audit gate", "outcome": "success"},
        ]
        result = measure_false_success_recurrence(history, n=0)
        assert result["count"] == 0

    def test_title_without_pr_number_does_not_crash(self) -> None:
        history: list[dict[str, object]] = [
            {"goal": "Remediate PR (no number here)", "outcome": "success"},
            {"goal": "Remediate PR (no number here)", "outcome": "success"},
        ]
        result = measure_false_success_recurrence(history, n=0)
        assert result["count"] == 0

    def test_non_success_outcomes_not_counted(self) -> None:
        history: list[dict[str, object]] = [
            _remediation_task(303, outcome="failure"),
            _remediation_task(303, outcome="partial"),
        ]
        result = measure_false_success_recurrence(history, n=0)
        assert result["count"] == 0

    def test_window_limits_to_tail(self) -> None:
        history: list[dict[str, object]] = [
            _remediation_task(505),
            _remediation_task(606),
            _remediation_task(606),
        ]
        # Window of 2 drops the first #505 entry; #606 still has 2 in window.
        result = measure_false_success_recurrence(history, n=2)
        assert result["count"] == 1
        assert result["by_pr"] == {606: 2}

    def test_missing_goal_falls_back_to_title(self) -> None:
        history: list[dict[str, object]] = [
            {"title": "Remediate PR #707: x", "outcome": "success"},
            {"title": "Remediate PR #707: x", "outcome": "success"},
        ]
        result = measure_false_success_recurrence(history, n=0)
        assert result["count"] == 1
        assert result["by_pr"] == {707: 2}

    def test_multiple_prs_only_recurrent_counted(self) -> None:
        history: list[dict[str, object]] = [
            _remediation_task(11),
            _remediation_task(11),
            _remediation_task(22),
            _remediation_task(33),
            _remediation_task(33),
            _remediation_task(33),
        ]
        result = measure_false_success_recurrence(history, n=0)
        assert result["count"] == 2
        assert result["by_pr"] == {11: 2, 33: 3}


class TestFormatFalseSuccessRecurrence:
    """Render the FALSE-SUCCESS RECURRENCE line only when count > 0."""

    def test_returns_line_when_recurrence_present(self) -> None:
        history: list[dict[str, object]] = [
            _remediation_task(101),
            _remediation_task(101),
        ]
        line = format_false_success_recurrence(history, n=0)
        assert "FALSE-SUCCESS RECURRENCE" in line
        assert "#101" in line

    def test_returns_empty_when_no_recurrence(self) -> None:
        history: list[dict[str, object]] = [_remediation_task(101)]
        assert format_false_success_recurrence(history, n=0) == ""

    def test_returns_empty_for_empty_history(self) -> None:
        assert format_false_success_recurrence([], n=0) == ""


class TestKnownStateSummaryFalseSuccessInjection:
    """format_known_state_summary injects the line only when count > 0."""

    def test_line_included_when_recurrence_present(self) -> None:
        history: list[dict[str, object]] = [
            _remediation_task(101),
            _remediation_task(101),
        ]
        section = format_known_state_summary(
            {}, [], task_history=history,
        )
        assert "FALSE-SUCCESS RECURRENCE" in section

    def test_line_omitted_when_count_zero(self) -> None:
        history: list[dict[str, object]] = [_remediation_task(101)]
        section = format_known_state_summary(
            {"compound_scope_gate_enabled": True}, [], task_history=history,
        )
        assert "FALSE-SUCCESS RECURRENCE" not in section

    def test_no_history_omits_line(self) -> None:
        section = format_known_state_summary(
            {"compound_scope_gate_enabled": True}, [],
        )
        assert "FALSE-SUCCESS RECURRENCE" not in section


# ---------------------------------------------------------------------------
# measure_pr_lineage_recurrence — resolved-recurrence detector
# ---------------------------------------------------------------------------


def _lineage_task(
    pr_number: int,
    *,
    outcome: str = "success",
    result_summary: str = "Confirmed existing state — already green",
    duration_secs: float = 0.0,
    goal: str | None = None,
) -> dict[str, object]:
    """Build a zero-cost success task referencing ``#pr_number``."""
    return {
        "goal": goal if goal is not None else f"Tend PR #{pr_number} after merge",
        "outcome": outcome,
        "result_summary": result_summary,
        "duration_secs": duration_secs,
    }


class TestMeasurePrLineageRecurrence:
    """Group zero-cost success tasks by ``#NNNN`` lineage above a threshold."""

    def test_four_zero_cost_successes_yield_lineage(self) -> None:
        tasks = [_lineage_task(2348) for _ in range(4)]
        assert measure_pr_lineage_recurrence(tasks) == {2348: 4}

    def test_below_threshold_yields_empty(self) -> None:
        tasks = [_lineage_task(2348) for _ in range(2)]
        assert measure_pr_lineage_recurrence(tasks) == {}

    def test_exactly_at_threshold_included(self) -> None:
        tasks = [_lineage_task(2348) for _ in range(3)]
        assert measure_pr_lineage_recurrence(tasks) == {2348: 3}

    def test_one_below_threshold_excluded(self) -> None:
        # Threshold default is 3; two successes must not surface.
        tasks = [_lineage_task(2348) for _ in range(2)]
        assert measure_pr_lineage_recurrence(tasks) == {}

    def test_noop_classification_counts(self) -> None:
        # Non-zero duration but a no-op result_summary still counts.
        tasks = [
            _lineage_task(
                900,
                result_summary="No remediation needed; already resolved",
                duration_secs=120.0,
            )
            for _ in range(3)
        ]
        assert measure_pr_lineage_recurrence(tasks) == {900: 3}

    def test_preflight_skip_marker_counts(self) -> None:
        tasks = [
            _lineage_task(
                901,
                result_summary="[preflight] merged origin/main, closed",
                duration_secs=99.0,
            )
            for _ in range(3)
        ]
        assert measure_pr_lineage_recurrence(tasks) == {901: 3}

    def test_zero_duration_counts(self) -> None:
        tasks = [
            _lineage_task(
                902,
                result_summary="some neutral summary with no marker",
                duration_secs=0.0,
            )
            for _ in range(3)
        ]
        assert measure_pr_lineage_recurrence(tasks) == {902: 3}

    def test_substantive_success_not_counted(self) -> None:
        # High duration, no no-op/preflight marker → real work, excluded.
        tasks = [
            _lineage_task(
                903,
                result_summary="Implemented retry logic and added tests",
                duration_secs=300.0,
            )
            for _ in range(4)
        ]
        assert measure_pr_lineage_recurrence(tasks) == {}

    def test_non_success_outcomes_ignored(self) -> None:
        tasks = [_lineage_task(904, outcome="partial") for _ in range(4)]
        assert measure_pr_lineage_recurrence(tasks) == {}

    def test_tasks_without_pr_reference_ignored(self) -> None:
        # No "#NNNN" token in any text field, so nothing is grouped.
        tasks: list[dict[str, object]] = [
            {
                "goal": "Investigate executor loop behaviour",
                "outcome": "success",
                "result_summary": "confirmed existing state",
                "duration_secs": 0.0,
            }
            for _ in range(4)
        ]
        assert measure_pr_lineage_recurrence(tasks) == {}

    def test_bool_duration_not_treated_as_zero_cost(self) -> None:
        # A bool duration must not satisfy the numeric near-zero branch.
        tasks = [
            {
                "goal": "Tend PR #905 after merge",
                "outcome": "success",
                "result_summary": "neutral summary, no marker here",
                "duration_secs": False,
            }
            for _ in range(4)
        ]
        assert measure_pr_lineage_recurrence(tasks) == {}

    def test_duplicate_pr_in_one_task_counts_once(self) -> None:
        tasks = [
            _lineage_task(
                906,
                goal="Tend PR #906 — see #906 again",
            )
            for _ in range(3)
        ]
        assert measure_pr_lineage_recurrence(tasks) == {906: 3}

    def test_custom_threshold_respected(self) -> None:
        tasks = [_lineage_task(907) for _ in range(2)]
        assert measure_pr_lineage_recurrence(tasks, threshold=2) == {907: 2}

    def test_multiple_lineages_sorted(self) -> None:
        tasks = (
            [_lineage_task(50) for _ in range(3)]
            + [_lineage_task(10) for _ in range(4)]
            + [_lineage_task(99) for _ in range(2)]  # below threshold
        )
        result = measure_pr_lineage_recurrence(tasks)
        assert result == {10: 4, 50: 3}
        assert list(result.keys()) == [10, 50]


class TestKnownStateSummaryResolvedRecurrenceInjection:
    """format_known_state_summary injects RESOLVED-RECURRENCE lines."""

    def test_line_included_when_lineage_present(self) -> None:
        history = [_lineage_task(2348) for _ in range(4)]
        section = format_known_state_summary({}, [], task_history=history)
        assert "RESOLVED-RECURRENCE: PR #2348 — 4 zero-cost" in section
        assert "further tending likely adds no value" in section

    def test_line_omitted_when_below_threshold(self) -> None:
        history = [_lineage_task(2348) for _ in range(2)]
        section = format_known_state_summary(
            {"compound_scope_gate_enabled": True}, [], task_history=history,
        )
        assert "RESOLVED-RECURRENCE" not in section

    def test_no_history_omits_line(self) -> None:
        section = format_known_state_summary(
            {"compound_scope_gate_enabled": True}, [],
        )
        assert "RESOLVED-RECURRENCE" not in section

    def test_recurrence_only_still_returns_section(self) -> None:
        # No gates/goals/contexts — only the lineage should drive output.
        history = [_lineage_task(2348) for _ in range(3)]
        section = format_known_state_summary({}, [], task_history=history)
        assert section.startswith("## Already Known State")
        assert "RESOLVED-RECURRENCE: PR #2348" in section


# ---------------------------------------------------------------------------
# measure_outcome_trends / format_outcome_trend_summary
# ---------------------------------------------------------------------------


def _trend_task(
    outcome: str,
    goal: str = "Extend the executor module with retry logic",
) -> dict[str, object]:
    """Build a task dict for outcome-trend tests.

    The default *goal* is module-extension shaped (non-investigation), so
    investigation rate stays 0 unless a caller overrides it.
    """
    return {"outcome": outcome, "goal": goal}


class TestMeasureOutcomeTrends:
    """Tests for :func:`measure_outcome_trends`."""

    def test_empty_list_returns_none(self) -> None:
        assert measure_outcome_trends([]) is None

    def test_three_tasks_returns_none(self) -> None:
        tasks = [_trend_task("success") for _ in range(3)]
        assert measure_outcome_trends(tasks) is None

    def test_non_positive_window_returns_none(self) -> None:
        tasks = [_trend_task("success") for _ in range(10)]
        assert measure_outcome_trends(tasks, window=0) is None

    def test_ten_tasks_correct_rates_in_both_halves(self) -> None:
        # older half: 4 success, 1 failed -> 0.8 success
        older = (
            [_trend_task("success")] * 4
            + [_trend_task("failed")]
        )
        # newer half: 2 success, 3 failed -> 0.4 success
        newer = (
            [_trend_task("success")] * 2
            + [_trend_task("failed")] * 3
        )
        trend = measure_outcome_trends(older + newer)
        assert trend is not None
        assert trend.older_window == 5
        assert trend.newer_window == 5
        assert trend.older_success_rate == 0.8
        assert trend.newer_success_rate == 0.4

    def test_declining_success_negative_delta(self) -> None:
        older = [_trend_task("success")] * 4
        newer = [_trend_task("failed")] * 4
        trend = measure_outcome_trends(older + newer)
        assert trend is not None
        assert trend.success_delta == -1.0

    def test_improving_success_positive_delta(self) -> None:
        older = [_trend_task("failed")] * 4
        newer = [_trend_task("success")] * 4
        trend = measure_outcome_trends(older + newer)
        assert trend is not None
        assert trend.success_delta == 1.0

    def test_partial_rate_tracked(self) -> None:
        older = [_trend_task("partial")] * 4
        newer = [_trend_task("success")] * 4
        trend = measure_outcome_trends(older + newer)
        assert trend is not None
        assert trend.older_partial_rate == 1.0
        assert trend.newer_partial_rate == 0.0
        assert trend.partial_delta == -1.0

    def test_investigation_rate_tracks_classify_task_shape(self) -> None:
        # older half: all investigation-shaped (measurement)
        older = [
            _trend_task("success", goal="Audit the recent task history")
            for _ in range(4)
        ]
        # newer half: all construction-shaped (module_extension)
        newer = [_trend_task("success") for _ in range(4)]
        trend = measure_outcome_trends(older + newer)
        assert trend is not None
        assert trend.older_investigation_rate == 1.0
        assert trend.newer_investigation_rate == 0.0
        assert trend.investigation_delta == -1.0

    def test_window_truncates_to_most_recent(self) -> None:
        # 6 old failures then 4 recent successes; window=4 keeps only the
        # successes, which then split 2/2 -> all success.
        tasks = [_trend_task("failed")] * 6 + [_trend_task("success")] * 4
        trend = measure_outcome_trends(tasks, window=4)
        assert trend is not None
        assert trend.older_window == 2
        assert trend.newer_window == 2
        assert trend.older_success_rate == 1.0
        assert trend.newer_success_rate == 1.0

    def test_odd_window_splits_smaller_half_older(self) -> None:
        tasks = [_trend_task("success") for _ in range(5)]
        trend = measure_outcome_trends(tasks)
        assert trend is not None
        assert trend.older_window == 2
        assert trend.newer_window == 3


def test_outcome_half_rates_empty_returns_zeros() -> None:
    """The empty-half guard returns all-zero rates."""
    from maestro.audit_checks import _outcome_half_rates

    assert _outcome_half_rates([]) == (0.0, 0.0, 0.0)


class TestFormatOutcomeTrendSummary:
    """Tests for :func:`format_outcome_trend_summary`."""

    def test_none_returns_empty_string(self) -> None:
        assert format_outcome_trend_summary(None) == ""

    def test_valid_trend_returns_directional_string(self) -> None:
        older = [_trend_task("failed")] * 4
        newer = [_trend_task("success")] * 4
        trend = measure_outcome_trends(older + newer)
        summary = format_outcome_trend_summary(trend)
        assert summary != ""
        assert "OUTCOME TREND" in summary
        # success rose 0% -> 100%, a +100pp climb
        assert "success 0%→100% ↑100pp" in summary

    def test_regressing_shows_down_arrow(self) -> None:
        older = [_trend_task("success")] * 4
        newer = [_trend_task("failed")] * 4
        summary = format_outcome_trend_summary(
            measure_outcome_trends(older + newer)
        )
        assert "success 100%→0% ↓100pp" in summary

    def test_stable_shows_flat_arrow(self) -> None:
        tasks = [_trend_task("success") for _ in range(8)]
        summary = format_outcome_trend_summary(measure_outcome_trends(tasks))
        assert "success 100%→100% →0pp" in summary


class TestKnownStateOutcomeTrendLine:
    """``format_known_state_summary`` outcome-trend injection."""

    def test_includes_outcome_trend_line_when_sufficient(self) -> None:
        history = [_trend_task("success") for _ in range(8)]
        section = format_known_state_summary({}, [], task_history=history)
        assert "OUTCOME TREND" in section

    def test_omits_outcome_trend_line_when_insufficient(self) -> None:
        history = [_trend_task("success") for _ in range(3)]
        section = format_known_state_summary(
            {"compound_scope_gate_enabled": True}, [], task_history=history,
        )
        assert "OUTCOME TREND" not in section

    def test_no_history_omits_outcome_trend_line(self) -> None:
        section = format_known_state_summary(
            {"compound_scope_gate_enabled": True}, [],
        )
        assert "OUTCOME TREND" not in section


class TestKnownStateSettledRelapLine:
    """``format_known_state_summary`` SETTLED-RELAP injection."""

    @staticmethod
    def _noop(result_summary: str) -> dict[str, object]:
        return {"outcome": "success", "result_summary": result_summary}

    def test_includes_settled_relap_line_above_threshold(self) -> None:
        # 3 settled-relap, 1 live-fork → rate 0.75 > default 0.4.
        history = [
            self._noop("No remediation needed — PR already merged."),
            self._noop("Already resolved upstream."),
            self._noop("No code changes — already handed off."),
            self._noop("Confirmed existing state, awaiting tend push."),
        ]
        section = format_known_state_summary({}, [], task_history=history)
        assert "SETTLED-RELAP:" in section
        assert "3/4" in section

    def test_omits_settled_relap_line_below_threshold(self) -> None:
        # 1 settled-relap, 3 live-fork → rate 0.25 <= default 0.4.
        history = [
            self._noop("No remediation needed — PR already merged."),
            self._noop("Confirmed existing state, awaiting tend push."),
            self._noop("Already passing all tests on the open PR."),
            self._noop("No code changes; gates green at a live fork."),
        ]
        section = format_known_state_summary(
            {"compound_scope_gate_enabled": True}, [], task_history=history,
        )
        assert "SETTLED-RELAP" not in section

    def test_no_history_omits_settled_relap_line(self) -> None:
        section = format_known_state_summary(
            {"compound_scope_gate_enabled": True}, [],
        )
        assert "SETTLED-RELAP" not in section

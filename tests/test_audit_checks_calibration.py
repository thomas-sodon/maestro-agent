"""Premise accuracy, narration conversion, outcome predictions and data
sufficiency (``maestro/audit_checks_calibration.py``); split out of
``tests/test_audit_checks.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from maestro.audit_checks import (
    GateActivation,
    format_known_state_summary,
)
from maestro.audit_checks_calibration import (
    JsonlPremiseLedger,
    load_latest_reflection_prediction,
    measure_prediction_accuracy,
    measure_self_calibration,
    predict_task_outcome,
    record_prediction,
    record_reflection_prediction,
    record_reflection_verdict,
)
from maestro.state_store import (
    SqliteGateActivationLedger,
    SqlitePredictionLedger,
    SqlitePremiseLedger,
    StateStore,
)
from tests.test_audit_checks import (
    FAKE_DIR,
    _gate_backend,
    _prediction_backend,
    _write_gate_activations,
)


def _premise_backend(data_dir: Path = FAKE_DIR) -> JsonlPremiseLedger:
    """JSONL premise backend over *data_dir* (default ``FAKE_DIR``).

    The default-off adapter binding the premise-observation trace (issue #2304)
    to ``premise_observations.jsonl`` under ``mem_fs``. Stateless (re-reads the
    file on each ``read_all``), so a fresh instance per call is fine.
    """
    return JsonlPremiseLedger(data_dir)


# ---------------------------------------------------------------------------
# Premise-accuracy trace: log_premise_observation / measure_premise_accuracy
# (issue #2304 leading indicator)
# ---------------------------------------------------------------------------


def test_log_premise_observation_writes_jsonl_record(
    mem_fs: dict[str, str],
) -> None:
    """A single call writes a valid JSONL row with all expected fields."""
    import json as json_mod

    from maestro.audit_checks_calibration import (
        log_premise_observation,
    )

    frozen = datetime(2026, 4, 26, tzinfo=UTC)
    with patch("maestro.audit_checks_calibration.datetime") as mock_dt:
        mock_dt.now.return_value = frozen
        log_premise_observation(
            _premise_backend(), task_id="t1", referenced=2, missing=1,
        )
    path = str(FAKE_DIR / "premise_observations.jsonl")
    assert path in mem_fs
    records = [
        json_mod.loads(line) for line in mem_fs[path].strip().splitlines()
    ]
    assert len(records) == 1
    r = records[0]
    assert r["task_id"] == "t1"
    assert r["referenced"] == 2
    assert r["missing"] == 1
    assert r["timestamp"] == frozen.isoformat()


def test_log_premise_observation_appends_multiple_rows(
    mem_fs: dict[str, str],
) -> None:
    """Multiple calls append multiple lines (append-only trace)."""
    import json as json_mod

    from maestro.audit_checks_calibration import (
        log_premise_observation,
    )

    log_premise_observation(
        _premise_backend(), task_id="a", referenced=1, missing=0,
    )
    log_premise_observation(
        _premise_backend(), task_id="b", referenced=1, missing=1,
    )
    path = str(FAKE_DIR / "premise_observations.jsonl")
    records = [
        json_mod.loads(line) for line in mem_fs[path].strip().splitlines()
    ]
    assert len(records) == 2
    assert records[0]["task_id"] == "a"
    assert records[1]["task_id"] == "b"


def test_measure_premise_accuracy_three_cases(
    mem_fs: dict[str, str],
) -> None:
    """A miss, a hit, and a no-premise row: only the first two are measured.

    A goal referencing a nonexistent ``.py`` path is a premise-miss (``missing
    >= 1``); a goal referencing only existing files is a hit (``missing == 0``);
    a goal referencing no ``.py`` path (``referenced == 0``) has no premise and
    is excluded from the denominator. Accuracy is hits / measured = 1/2.
    """
    from maestro.audit_checks_calibration import (
        log_premise_observation,
        measure_premise_accuracy,
    )

    backend = _premise_backend()
    log_premise_observation(backend, task_id="miss", referenced=1, missing=1)
    log_premise_observation(backend, task_id="hit", referenced=3, missing=0)
    log_premise_observation(backend, task_id="none", referenced=0, missing=0)

    report = measure_premise_accuracy(backend, n=100)
    assert report.total_measured == 2
    assert report.hits == 1
    assert report.accuracy == 0.5
    assert report.recent_misses == ["miss"]


def test_measure_premise_accuracy_empty_trace(
    mem_fs: dict[str, str],
) -> None:
    """An empty trace reports zero measured, zero accuracy, no misses."""
    from maestro.audit_checks_calibration import (
        measure_premise_accuracy,
    )

    report = measure_premise_accuracy(_premise_backend(), n=100)
    assert report.total_measured == 0
    assert report.hits == 0
    assert report.accuracy == 0.0
    assert report.recent_misses == []


def test_measure_premise_accuracy_windows_last_n(
    mem_fs: dict[str, str],
) -> None:
    """Only the last *n* rows are measured (the tail window)."""
    from maestro.audit_checks_calibration import (
        log_premise_observation,
        measure_premise_accuracy,
    )

    backend = _premise_backend()
    # Oldest row is a hit; the tail window of 1 must exclude it and see only
    # the newest (a miss).
    log_premise_observation(backend, task_id="old", referenced=1, missing=0)
    log_premise_observation(backend, task_id="new", referenced=1, missing=1)
    report = measure_premise_accuracy(backend, n=1)
    assert report.total_measured == 1
    assert report.hits == 0
    assert report.recent_misses == ["new"]


def test_measure_premise_accuracy_recent_misses_newest_first_bounded(
    mem_fs: dict[str, str],
) -> None:
    """Recent misses are newest-first and bounded by *recent_limit*."""
    from maestro.audit_checks_calibration import (
        log_premise_observation,
        measure_premise_accuracy,
    )

    backend = _premise_backend()
    for i in range(5):
        log_premise_observation(
            backend, task_id=f"m{i}", referenced=1, missing=1,
        )
    report = measure_premise_accuracy(backend, n=100, recent_limit=2)
    assert report.total_measured == 5
    assert report.hits == 0
    # Newest-first, capped at 2.
    assert report.recent_misses == ["m4", "m3"]


def test_measure_premise_accuracy_backend_parity_jsonl_vs_sqlite(
    mem_fs: dict[str, str],
) -> None:
    """The report is identical whether the trace is JSONL- or SQLite-backed.

    The flag-flip invariance the fold guarantees: a shared corpus fed to both a
    :class:`JsonlPremiseLedger` and an in-memory :class:`SqlitePremiseLedger`
    yields the same :func:`measure_premise_accuracy` output, so ``[paths]
    sqlite_state_enabled`` never changes what the CLI reads.
    """
    from maestro.audit_checks_calibration import (
        PremiseObservation,
        measure_premise_accuracy,
    )

    corpus = [
        PremiseObservation(task_id="a", referenced=1, missing=1, timestamp="t"),
        PremiseObservation(task_id="b", referenced=2, missing=0, timestamp="t"),
        PremiseObservation(task_id="c", referenced=0, missing=0, timestamp="t"),
    ]
    jsonl = _premise_backend(Path("/fake/premise-parity"))
    sqlite = SqlitePremiseLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        for record in corpus:
            backend.append(record)
    assert measure_premise_accuracy(jsonl, n=100) == measure_premise_accuracy(
        sqlite, n=100,
    )


def test_load_all_premise_observations_skips_corrupt_and_missing(
    mem_fs: dict[str, str],
) -> None:
    """Corrupt/blank/non-dict lines are dropped (never raised); missing → ``[]``."""
    from maestro.audit_checks_calibration import (
        load_all_premise_observations,
    )

    # Missing file → empty.
    assert load_all_premise_observations(Path("/fake/nope")) == []

    # One good row, then every skip branch: a blank line, a non-JSON line, a
    # JSON value that is not an object, and a JSON object missing a required
    # field. Only the good row survives.
    data_dir = Path("/fake/premise-corrupt")
    path = str(data_dir / "premise_observations.jsonl")
    mem_fs[path] = (
        '{"task_id": "ok", "referenced": 1, "missing": 0, "timestamp": "t"}\n'
        "\n"
        "not json at all\n"
        "[1, 2, 3]\n"
        '{"task_id": "bad"}\n'
    )
    rows = load_all_premise_observations(data_dir)
    assert len(rows) == 1
    assert rows[0].task_id == "ok"


def test_load_all_premise_observations_read_error_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """An ``OSError`` reading an existing trace is swallowed, returning ``[]``."""
    from maestro.audit_checks_calibration import (
        load_all_premise_observations,
    )

    data_dir = Path("/fake/premise-oserror")
    mem_fs[str(data_dir / "premise_observations.jsonl")] = (
        '{"task_id": "x", "referenced": 1, "missing": 0, "timestamp": "t"}\n'
    )
    with patch.object(Path, "read_text", side_effect=OSError("boom")):
        assert load_all_premise_observations(data_dir) == []


def test_data_sufficiency_counts_gate_activations_via_backend(
    mem_fs: dict[str, str],
) -> None:
    """A supplied backend sources the ``gate_activations`` target count.

    ``check_data_sufficiency`` / ``format_data_sufficiency_context`` count the
    gate-activation target via ``len(backend.read_all())`` when a backend is
    given (proposal 106) — so an SQLite-backed trace, whose rows never touch the
    JSONL file, still satisfies the threshold — while the ``predictions`` /
    ``usage`` targets stay filename-counted.
    """
    from maestro.audit_checks_calibration import (
        DATA_SUFFICIENCY_MIN_OBSERVATIONS,
        check_data_sufficiency,
        format_data_sufficiency_context,
    )

    backend = SqliteGateActivationLedger(StateStore(":memory:"))
    for _ in range(DATA_SUFFICIENCY_MIN_OBSERVATIONS):
        backend.append(GateActivation(gate="g", action="a", timestamp="t", detail="d"))
    # No JSONL gate file exists, yet the backend count clears the threshold, so
    # "gate_activations" is not in the insufficient list (predictions/usage are).
    insufficient = check_data_sufficiency(
        FAKE_DIR, gate_activation_backend=backend,
    )
    assert "gate_activations" not in insufficient
    assert "predictions" in insufficient
    assert "usage" in insufficient
    ctx = format_data_sufficiency_context(
        FAKE_DIR, gate_activation_backend=backend,
    )
    assert (
        f"gate_activations: {DATA_SUFFICIENCY_MIN_OBSERVATIONS}/"
        f"{DATA_SUFFICIENCY_MIN_OBSERVATIONS} records — MET"
    ) in ctx


# ---------------------------------------------------------------------------
# Narration→action conversion sensor (proposal 090)
# ---------------------------------------------------------------------------


def _always_merged(_pr: int) -> bool:
    return True


def _never_merged(_pr: int) -> bool:
    return False


def _always_landed(_sha: str) -> bool:
    return True


def _never_landed(_sha: str) -> bool:
    return False


def test_dropped_directive_replay_is_a_directive_gate_type() -> None:
    """``dropped_directive_replay`` must be a recognized directive-setting gate.

    The chain gate logs ``action="directive_set"`` via ``run_gate_chain`` but
    was never added to :data:`DIRECTIVE_GATE_TYPES`, so a directive it re-offers
    was silently dropped from :func:`measure_directive_compliance`. Proposal 090
    closes the lockstep; both sets must carry the gate (with a keyword set tuned
    to its fire-or-close text).
    """
    from maestro.audit_checks import (
        DIRECTIVE_GATE_TYPES,
        DIRECTIVE_INTENT_KEYWORDS,
    )

    assert "dropped_directive_replay" in DIRECTIVE_GATE_TYPES
    assert "dropped_directive_replay" in DIRECTIVE_INTENT_KEYWORDS


def test_measure_narration_conversion_missing_file(
    mem_fs: dict[str, str],
) -> None:
    """No gate_activations.jsonl yields the all-zero result."""
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    result = measure_narration_conversion(
        _gate_backend(), is_pr_merged=_never_merged, is_sha_landed=_never_landed,
    )
    assert result == {
        "detections": 0,
        "actuations": 0,
        "dropped": 0,
        "settled": 0,
        "per_gate": {},
    }


def test_measure_narration_conversion_counts_per_gate(
    mem_fs: dict[str, str],
) -> None:
    """Detections, actuations, and dropped counts split per gate."""
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "rule_14_pr_less",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "pr=#10",
            },
            {
                "gate": "rule_14_pr_less",
                "action": "dropped",
                "timestamp": "2026-06-01T01:00:00+00:00",
                "detail": "pr=#11",
            },
            {
                "gate": "unconfirmed_landing_forced",
                "action": "handoff",
                "timestamp": "2026-06-01T02:00:00+00:00",
                "detail": "pr=#12",
            },
        ],
    )
    result = measure_narration_conversion(
        _gate_backend(), is_pr_merged=_never_merged, is_sha_landed=_never_landed,
    )
    assert result["detections"] == 3
    assert result["actuations"] == 2
    assert result["dropped"] == 1
    assert result["settled"] == 0
    assert result["per_gate"]["rule_14_pr_less"] == {
        "detections": 2, "actuations": 1, "dropped": 1, "settled": 0,
    }
    assert result["per_gate"]["unconfirmed_landing_forced"] == {
        "detections": 1, "actuations": 1, "dropped": 0, "settled": 0,
    }


def test_measure_narration_conversion_ignores_non_narration_gates(
    mem_fs: dict[str, str],
) -> None:
    """Rows whose gate is not a narration writer are skipped entirely."""
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "investigation_rate",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "build something #99",
            },
            {
                "gate": "audit_stall",
                "action": "blocked",
                "timestamp": "2026-06-01T01:00:00+00:00",
                "detail": "stalled",
            },
        ],
    )
    result = measure_narration_conversion(
        _gate_backend(), is_pr_merged=_always_merged, is_sha_landed=_always_landed,
    )
    assert result["detections"] == 0
    assert result["per_gate"] == {}


def test_measure_narration_conversion_settlement_pr_and_sha(
    mem_fs: dict[str, str],
) -> None:
    """Actuation rows settle when the oracle confirms the cited PR/SHA."""
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "rule_14_pr_less",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "pr=#100",
            },
            {
                "gate": "audit_directive_bridge",
                "action": "directive_set",
                "timestamp": "2026-06-01T01:00:00+00:00",
                "detail": "sha=abc1234",
            },
        ],
    )
    result = measure_narration_conversion(
        _gate_backend(), is_pr_merged=_always_merged, is_sha_landed=_always_landed,
    )
    assert result["actuations"] == 2
    assert result["settled"] == 2


def test_measure_narration_conversion_unmerged_not_settled(
    mem_fs: dict[str, str],
) -> None:
    """A cited-but-unmerged PR is an actuation but not settled."""
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "unconfirmed_landing_forced",
                "action": "handoff",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "pr=#200",
            },
        ],
    )
    result = measure_narration_conversion(
        _gate_backend(), is_pr_merged=_never_merged, is_sha_landed=_never_landed,
    )
    assert result["actuations"] == 1
    assert result["settled"] == 0


def test_measure_narration_conversion_ungrounded_excluded_from_settlement(
    mem_fs: dict[str, str],
) -> None:
    """An actuation with no parseable grounding never settles.

    Even when the oracles would answer merged/landed, a detail carrying no
    ``#NNNN`` and no ``sha=<hex>`` token falls out of the settlement numerator.
    """
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "audit_directive_bridge",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "Investigate the measurement gap in cost tracking",
            },
        ],
    )
    result = measure_narration_conversion(
        _gate_backend(), is_pr_merged=_always_merged, is_sha_landed=_always_landed,
    )
    assert result["actuations"] == 1
    assert result["settled"] == 0


def test_measure_narration_conversion_dropped_never_actuates_or_settles(
    mem_fs: dict[str, str],
) -> None:
    """A dropped offer is neither an actuation nor settled, even if landed."""
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "rule_14_pr_less",
                "action": "dropped",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "pr=#300",
            },
        ],
    )
    result = measure_narration_conversion(
        _gate_backend(), is_pr_merged=_always_merged, is_sha_landed=_always_landed,
    )
    assert result["detections"] == 1
    assert result["actuations"] == 0
    assert result["dropped"] == 1
    assert result["settled"] == 0


def test_measure_narration_conversion_memoizes_oracle_per_key(
    mem_fs: dict[str, str],
) -> None:
    """Each distinct PR/SHA grounding is probed at most once per measurement.

    A still-unlanded grounding recurs across many actuation rows, and the SHA
    oracle (``is_branch_merged_into_main``) shells out to ``git merge-base``
    per call — so the settlement join must dedup by key, not re-probe every row
    (ledger r1).
    """
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    pr_calls: list[int] = []
    sha_calls: list[str] = []

    def _counting_pr(pr: int) -> bool:
        pr_calls.append(pr)
        return False

    def _counting_sha(sha: str) -> bool:
        sha_calls.append(sha)
        return False

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "unconfirmed_landing_forced",
                "action": "handoff",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "pr=#42",
            },
            {
                "gate": "unconfirmed_landing_forced",
                "action": "handoff",
                "timestamp": "2026-06-01T01:00:00+00:00",
                "detail": "pr=#42",
            },
            {
                "gate": "audit_directive_bridge",
                "action": "directive_set",
                "timestamp": "2026-06-01T02:00:00+00:00",
                "detail": "sha=deadbee",
            },
            {
                "gate": "audit_directive_bridge",
                "action": "directive_set",
                "timestamp": "2026-06-01T03:00:00+00:00",
                "detail": "sha=deadbee",
            },
        ],
    )
    result = measure_narration_conversion(
        _gate_backend(), is_pr_merged=_counting_pr, is_sha_landed=_counting_sha,
    )
    assert result["actuations"] == 4
    assert result["settled"] == 0
    # Each distinct grounding probed exactly once despite two rows apiece.
    assert pr_calls == [42]
    assert sha_calls == ["deadbee"]


def test_measure_narration_conversion_tolerates_malformed_lines(
    mem_fs: dict[str, str],
) -> None:
    """A corrupt line in the trace is skipped, not fatal."""
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    path = str(FAKE_DIR / "gate_activations.jsonl")
    good = json.dumps(
        {
            "gate": "rule_14_pr_less",
            "action": "directive_set",
            "timestamp": "2026-06-01T00:00:00+00:00",
            "detail": "sha=deadbee",
        },
    )
    mem_fs[path] = "not json\n" + good + "\n"
    result = measure_narration_conversion(
        _gate_backend(), is_pr_merged=_never_merged, is_sha_landed=_always_landed,
    )
    assert result["detections"] == 1
    assert result["settled"] == 1


def test_measure_narration_conversion_respects_window(
    mem_fs: dict[str, str],
) -> None:
    """Only the trailing *n* rows are read."""
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "rule_14_pr_less",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "pr=#1",
            },
            {
                "gate": "rule_14_pr_less",
                "action": "directive_set",
                "timestamp": "2026-06-01T01:00:00+00:00",
                "detail": "pr=#2",
            },
        ],
    )
    result = measure_narration_conversion(
        _gate_backend(), n=1, is_pr_merged=_never_merged, is_sha_landed=_never_landed,
    )
    assert result["detections"] == 1


def test_measure_narration_conversion_oserror_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """When the trace read raises OSError, the sensor returns the all-zero result."""
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    path = str(FAKE_DIR / "gate_activations.jsonl")
    mem_fs[path] = "valid content"  # File exists so path.exists() is True.
    with patch.object(Path, "read_text", side_effect=OSError("disk error")):
        result = measure_narration_conversion(
            _gate_backend(), is_pr_merged=_never_merged, is_sha_landed=_never_landed,
        )
    assert result["detections"] == 0
    assert result["per_gate"] == {}


def test_measure_narration_conversion_skips_blank_lines(
    mem_fs: dict[str, str],
) -> None:
    """Blank lines interspersed with narration rows are silently skipped."""
    from maestro.audit_checks_calibration import (
        measure_narration_conversion,
    )

    path = str(FAKE_DIR / "gate_activations.jsonl")
    lines = [
        json.dumps(
            {
                "gate": "unconfirmed_landing_forced",
                "action": "handoff",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "pr=#7",
            },
        ),
        "",
        "   ",
    ]
    mem_fs[path] = "\n".join(lines) + "\n"
    result = measure_narration_conversion(
        _gate_backend(), is_pr_merged=_never_merged, is_sha_landed=_never_landed,
    )
    assert result["detections"] == 1
    assert result["actuations"] == 1


def test_format_narration_conversion_summary_golden(
    mem_fs: dict[str, str],
) -> None:
    """Formatter renders the totals + per-gate breakdown line."""
    from maestro.audit_checks_calibration import (
        format_narration_conversion_summary,
    )

    _write_gate_activations(
        mem_fs,
        [
            {
                "gate": "rule_14_pr_less",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "pr=#10",
            },
            {
                "gate": "rule_14_pr_less",
                "action": "dropped",
                "timestamp": "2026-06-01T01:00:00+00:00",
                "detail": "pr=#11",
            },
        ],
    )
    line = format_narration_conversion_summary(
        _gate_backend(), is_pr_merged=_always_merged, is_sha_landed=_never_landed,
    )
    assert line == (
        "NARRATION-CONVERSION: 2 detections → 1 actuations (1 dropped) → "
        "1 settled [rule_14_pr_less 2/1/1]"
    )


def test_format_narration_conversion_summary_empty(
    mem_fs: dict[str, str],
) -> None:
    """Empty history renders nothing."""
    from maestro.audit_checks_calibration import (
        format_narration_conversion_summary,
    )

    line = format_narration_conversion_summary(
        _gate_backend(), is_pr_merged=_never_merged, is_sha_landed=_never_landed,
    )
    assert line == ""


def test_format_known_state_summary_includes_narration_conversion() -> None:
    """The NARRATION-CONVERSION context is appended when non-empty."""
    from maestro.audit_checks import format_known_state_summary

    out = format_known_state_summary(
        {}, [], narration_conversion_context="NARRATION-CONVERSION: 1 → 1",
    )
    assert "NARRATION-CONVERSION: 1 → 1" in out


def test_format_known_state_summary_omits_narration_conversion() -> None:
    """No NARRATION-CONVERSION line when the context is empty."""
    from maestro.audit_checks import format_known_state_summary

    out = format_known_state_summary(
        {"g": True}, [], narration_conversion_context="",
    )
    assert "NARRATION-CONVERSION" not in out


# ---------------------------------------------------------------------------
# Outcome prediction tracker: predict_task_outcome
# ---------------------------------------------------------------------------


def test_predict_task_outcome_success_threshold() -> None:
    """Rate ≥ 0.70 predicts success."""
    # 7/10 = 0.70 — exactly at threshold
    tasks: list[dict[str, object]] = [
        {"goal": "measure audit rate", "outcome": "success"}
        for _ in range(7)
    ] + [
        {"goal": "measure audit rate", "outcome": "failure"}
        for _ in range(3)
    ]
    assert predict_task_outcome(tasks, "measure something") == "success"


def test_predict_task_outcome_partial_threshold() -> None:
    """Rate ≥ 0.40 but < 0.70 predicts partial."""
    # 4/10 = 0.40 — exactly at partial threshold
    tasks: list[dict[str, object]] = [
        {"goal": "measure audit rate", "outcome": "success"}
        for _ in range(4)
    ] + [
        {"goal": "measure audit rate", "outcome": "failure"}
        for _ in range(6)
    ]
    assert predict_task_outcome(tasks, "measure something") == "partial"


def test_predict_task_outcome_failure_threshold() -> None:
    """Rate < 0.40 predicts failure."""
    # 3/10 = 0.30 — below partial threshold
    tasks: list[dict[str, object]] = [
        {"goal": "measure audit rate", "outcome": "success"}
        for _ in range(3)
    ] + [
        {"goal": "measure audit rate", "outcome": "failure"}
        for _ in range(7)
    ]
    assert predict_task_outcome(tasks, "measure something") == "failure"


def test_predict_task_outcome_boundary_069() -> None:
    """Rate 0.69 (just below success threshold) predicts partial."""
    # Construct ~0.69 rate: 69/100 is cleanest
    tasks: list[dict[str, object]] = [
        {"goal": "measure x", "outcome": "success"}
        for _ in range(69)
    ] + [
        {"goal": "measure x", "outcome": "failure"}
        for _ in range(31)
    ]
    assert predict_task_outcome(tasks, "measure y") == "partial"


def test_predict_task_outcome_boundary_039() -> None:
    """Rate 0.39 (just below partial threshold) predicts failure."""
    # 39/100 = 0.39
    tasks: list[dict[str, object]] = [
        {"goal": "measure x", "outcome": "success"}
        for _ in range(39)
    ] + [
        {"goal": "measure x", "outcome": "failure"}
        for _ in range(61)
    ]
    assert predict_task_outcome(tasks, "measure y") == "failure"


def test_predict_task_outcome_empty_history() -> None:
    """Empty task history falls back to partial."""
    assert predict_task_outcome([], "fix #123") == "partial"


def test_predict_task_outcome_no_matching_shape() -> None:
    """No tasks matching the goal's shape falls back to partial."""
    tasks: list[dict[str, object]] = [
        {"goal": "fix #123", "outcome": "success"},  # github_issue shape
    ]
    # "measure audit" is measurement shape — no match with github_issue
    assert predict_task_outcome(tasks, "measure audit rate") == "partial"


def test_predict_task_outcome_github_issue_shape() -> None:
    """GitHub issue shape uses only issue-shaped history."""
    tasks: list[dict[str, object]] = [
        {"goal": "fix #100", "outcome": "success"},
        {"goal": "fix #101", "outcome": "success"},
        {"goal": "fix #102", "outcome": "success"},
        {"goal": "fix #103", "outcome": "success"},
        {"goal": "fix #104", "outcome": "success"},
        {"goal": "fix #105", "outcome": "success"},
        {"goal": "fix #106", "outcome": "success"},
        # measurement tasks should not count
        {"goal": "measure something", "outcome": "failure"},
        {"goal": "measure audit", "outcome": "failure"},
        {"goal": "measure gate", "outcome": "failure"},
    ]
    assert predict_task_outcome(tasks, "fix #200") == "success"


def test_predict_task_outcome_branch_promotion_shape() -> None:
    """Branch promotion shape classification works."""
    tasks: list[dict[str, object]] = [
        {"goal": "branch promotion of task_123", "outcome": "failure"},
        {"goal": "branch promotion of task_456", "outcome": "failure"},
        {"goal": "branch promotion of task_789", "outcome": "failure"},
    ]
    assert predict_task_outcome(tasks, "branch promotion of task_abc") == "failure"


# ---------------------------------------------------------------------------
# Outcome prediction tracker: record_prediction
# ---------------------------------------------------------------------------


def test_record_prediction_single_write(
    mem_fs: dict[str, str],
) -> None:
    """Single prediction write creates valid JSONL."""
    import json

    d = Path("/fake/pred")
    record_prediction(_prediction_backend(d), "task_001", "success")
    path_str = str(d / "predictions.jsonl")
    assert path_str in mem_fs
    lines = mem_fs[path_str].strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["task_id"] == "task_001"
    assert record["predicted_outcome"] == "success"
    assert "timestamp" in record


def test_record_prediction_multiple_writes(
    mem_fs: dict[str, str],
) -> None:
    """Multiple prediction writes append separate JSONL lines."""
    import json

    d = Path("/fake/pred")
    record_prediction(_prediction_backend(d), "task_001", "success")
    record_prediction(_prediction_backend(d), "task_002", "partial")
    record_prediction(_prediction_backend(d), "task_003", "failure")
    path_str = str(d / "predictions.jsonl")
    lines = mem_fs[path_str].strip().splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert records[0]["task_id"] == "task_001"
    assert records[1]["predicted_outcome"] == "partial"
    assert records[2]["predicted_outcome"] == "failure"


# ---------------------------------------------------------------------------
# Outcome prediction tracker: measure_prediction_accuracy
# ---------------------------------------------------------------------------


def test_measure_prediction_accuracy_exact_match(
    mem_fs: dict[str, str],
) -> None:
    """Correct predictions counted accurately."""
    d = Path("/fake/pred")
    record_prediction(_prediction_backend(d), "t1", "success")
    record_prediction(_prediction_backend(d), "t2", "partial")
    record_prediction(_prediction_backend(d), "t3", "failure")

    task_history: list[dict[str, object]] = [
        {"task_id": "t1", "outcome": "success", "goal": "fix #1"},
        {"task_id": "t2", "outcome": "partial", "goal": "measure x"},
        {"task_id": "t3", "outcome": "failure", "goal": "do something"},
    ]
    result = measure_prediction_accuracy(_prediction_backend(d), task_history)
    assert result["total"] == 3
    assert result["correct"] == 3
    assert result["accuracy"] == 1.0


def test_measure_prediction_accuracy_mismatch(
    mem_fs: dict[str, str],
) -> None:
    """Mismatched predictions reflected in accuracy."""
    d = Path("/fake/pred")
    record_prediction(_prediction_backend(d), "t1", "success")
    record_prediction(_prediction_backend(d), "t2", "success")

    task_history: list[dict[str, object]] = [
        {"task_id": "t1", "outcome": "success", "goal": "fix #1"},
        {"task_id": "t2", "outcome": "failure", "goal": "measure x"},
    ]
    result = measure_prediction_accuracy(_prediction_backend(d), task_history)
    assert result["total"] == 2
    assert result["correct"] == 1
    assert result["accuracy"] == 0.5


def test_measure_prediction_accuracy_missing_outcome(
    mem_fs: dict[str, str],
) -> None:
    """Tasks not yet completed are excluded from count."""
    d = Path("/fake/pred")
    record_prediction(_prediction_backend(d), "t1", "success")
    record_prediction(_prediction_backend(d), "t2", "partial")

    # Only t1 has a matching task record
    task_history: list[dict[str, object]] = [
        {"task_id": "t1", "outcome": "success", "goal": "fix #1"},
    ]
    result = measure_prediction_accuracy(_prediction_backend(d), task_history)
    assert result["total"] == 1
    assert result["correct"] == 1
    assert result["accuracy"] == 1.0


def test_measure_prediction_accuracy_empty_file(
    mem_fs: dict[str, str],
) -> None:
    """Missing predictions file returns zeroed result."""
    d = Path("/fake/pred")
    result = measure_prediction_accuracy(_prediction_backend(d), [])
    assert result["total"] == 0
    assert result["correct"] == 0
    assert result["accuracy"] == 0.0
    assert result["by_shape"] == {}


def test_measure_prediction_accuracy_by_shape(
    mem_fs: dict[str, str],
) -> None:
    """by_shape breakdown groups results by task shape."""
    d = Path("/fake/pred")
    record_prediction(_prediction_backend(d), "t1", "success")
    record_prediction(_prediction_backend(d), "t2", "failure")

    task_history: list[dict[str, object]] = [
        {"task_id": "t1", "outcome": "success", "goal": "fix #100"},
        {"task_id": "t2", "outcome": "failure", "goal": "measure audit"},
    ]
    result = measure_prediction_accuracy(_prediction_backend(d), task_history)
    by_shape = result["by_shape"]
    assert isinstance(by_shape, dict)
    assert "github_issue" in by_shape
    assert "measurement" in by_shape
    assert by_shape["github_issue"]["total"] == 1  # type: ignore[index]
    assert by_shape["github_issue"]["correct"] == 1  # type: ignore[index]
    assert by_shape["measurement"]["total"] == 1  # type: ignore[index]
    assert by_shape["measurement"]["correct"] == 1  # type: ignore[index]


def test_measure_prediction_accuracy_respects_n(
    mem_fs: dict[str, str],
) -> None:
    """Only the last n predictions are considered."""
    d = Path("/fake/pred")
    # Write 5 predictions, read only last 2
    for i in range(5):
        record_prediction(_prediction_backend(d), f"t{i}", "success")

    task_history: list[dict[str, object]] = [
        {"task_id": f"t{i}", "outcome": "success", "goal": "x"}
        for i in range(5)
    ]
    result = measure_prediction_accuracy(_prediction_backend(d), task_history, n=2)
    assert result["total"] == 2


def test_failure_prediction_scores_correct_against_production_failure_literal(
    mem_fs: dict[str, str],
) -> None:
    """A predicted failure scores correct against the literal production writes.

    Regression for #2919. The predictor returned the ``PlanStatus`` spelling
    ``"failed"`` while a completed task records the
    :data:`~maestro.types.TaskOutcome` spelling ``"failure"``, so the bare
    ``predicted == actual`` join in :func:`measure_prediction_accuracy` scored
    every correctly-predicted failure as wrong — silently deflating the
    failure-case accuracy that feeds the task-generation calibration context.
    """
    d = Path("/fake/pred")
    # 1/10 = 0.10 — below the partial threshold, so the shape predicts failure.
    history: list[dict[str, object]] = [
        {"goal": "measure audit rate", "outcome": "success"},
    ] + [
        {"goal": "measure audit rate", "outcome": "failure"}
        for _ in range(9)
    ]
    predicted = predict_task_outcome(history, "measure something")
    assert predicted == "failure"

    record_prediction(_prediction_backend(d), "t-fail", predicted)
    task_history: list[dict[str, object]] = [
        {"task_id": "t-fail", "outcome": "failure", "goal": "measure something"},
    ]
    result = measure_prediction_accuracy(_prediction_backend(d), task_history)
    assert result["total"] == 1
    assert result["correct"] == 1


# ---------------------------------------------------------------------------
# format_known_state_summary — prediction accuracy context
# ---------------------------------------------------------------------------


def test_measure_prediction_accuracy_os_error(
    mem_fs: dict[str, str],
) -> None:
    """OSError when reading predictions file returns zeroed result."""
    d = Path("/fake/pred")
    # Create an entry so the file "exists" for the path check
    record_prediction(_prediction_backend(d), "t1", "success")
    # Now make reading raise OSError
    path_str = str(d / "predictions.jsonl")
    original_open = __builtins__["open"] if isinstance(__builtins__, dict) else __builtins__.open  # type: ignore[union-attr]

    def error_open(file: object, mode: str = "r", *a: object, **kw: object) -> object:
        if str(file) == path_str and mode == "r":
            raise OSError("simulated read error")
        return original_open(file, mode, *a, **kw)  # type: ignore[arg-type]

    with patch("builtins.open", side_effect=error_open):
        result = measure_prediction_accuracy(_prediction_backend(d), [])
    assert result["total"] == 0


def test_measure_prediction_accuracy_malformed_lines(
    mem_fs: dict[str, str],
) -> None:
    """Malformed JSON lines in predictions file are skipped."""
    d = Path("/fake/pred")
    # Write valid prediction then corrupt the file
    record_prediction(_prediction_backend(d), "t1", "success")
    path_str = str(d / "predictions.jsonl")
    mem_fs[path_str] = "not valid json\n" + mem_fs[path_str]

    task_history: list[dict[str, object]] = [
        {"task_id": "t1", "outcome": "success", "goal": "fix #1"},
    ]
    result = measure_prediction_accuracy(_prediction_backend(d), task_history)
    # Only the valid line should be counted
    assert result["total"] == 1
    assert result["correct"] == 1


def test_measure_prediction_accuracy_empty_lines_skipped(
    mem_fs: dict[str, str],
) -> None:
    """Empty lines in predictions file are silently skipped."""
    d = Path("/fake/pred")
    record_prediction(_prediction_backend(d), "t1", "success")
    path_str = str(d / "predictions.jsonl")
    mem_fs[path_str] = "\n\n" + mem_fs[path_str] + "\n\n"

    task_history: list[dict[str, object]] = [
        {"task_id": "t1", "outcome": "success", "goal": "fix #1"},
    ]
    result = measure_prediction_accuracy(_prediction_backend(d), task_history)
    assert result["total"] == 1


def test_measure_prediction_accuracy_no_valid_predictions(
    mem_fs: dict[str, str],
) -> None:
    """File with only malformed lines returns zeroed result."""
    d = Path("/fake/pred")
    path_str = str(d / "predictions.jsonl")
    d.mkdir(parents=True, exist_ok=True)
    mem_fs[path_str] = "not json\nalso not json\n"

    result = measure_prediction_accuracy(_prediction_backend(d), [])
    assert result["total"] == 0
    assert result["accuracy"] == 0.0


# ---------------------------------------------------------------------------
# Prediction backend — JSONL round-trip and JSONL↔SQLite parity (proposal 106)
# ---------------------------------------------------------------------------


def _prediction_corpus() -> list[dict[str, object]]:
    """One row of each of the three heterogeneous prediction shapes."""
    return [
        {
            "task_id": "t1",
            "predicted_outcome": "success",
            "timestamp": "2026-04-26T00:00:00+00:00",
        },
        {
            "source": "reflection",
            "prediction_id": "reflection-1",
            "prediction": "I will land PR #9",
            "loss_condition": "PR #9 stays open",
            "verdict": "unscored",
            "timestamp": "2026-04-26T00:01:00+00:00",
        },
        {
            "source": "reflection_verdict",
            "prediction_id": "reflection-1",
            "verdict": "correct",
            "timestamp": "2026-04-26T00:02:00+00:00",
        },
    ]


def test_jsonl_prediction_ledger_round_trip(mem_fs: dict[str, str]) -> None:
    """The JSONL adapter appends and reads back all three shapes verbatim."""
    backend = _prediction_backend(FAKE_DIR)
    corpus = _prediction_corpus()
    for row in corpus:
        backend.append(row)
    assert backend.read_all() == corpus


def test_prediction_writers_jsonl_sqlite_parity(mem_fs: dict[str, str]) -> None:
    """Each writer produces identical rows through both backends.

    ``record_prediction`` / ``record_reflection_prediction`` /
    ``record_reflection_verdict`` build the same record dict regardless of
    substrate, so the JSONL and SQLite traces are equal — the authored timestamp
    is frozen here so the comparison is exact.
    """
    frozen = datetime(2026, 4, 26, tzinfo=UTC)
    jsonl = _prediction_backend(FAKE_DIR)
    sqlite = SqlitePredictionLedger(StateStore(":memory:"))
    with patch("maestro.audit_checks_calibration.datetime") as mock_dt:
        mock_dt.now.return_value = frozen
        for backend in (jsonl, sqlite):
            record_prediction(backend, "t1", "success")
            record_reflection_prediction(backend, "r1", "p", "loss")
            record_reflection_verdict(backend, "r1", "correct")
    assert jsonl.read_all() == sqlite.read_all()
    assert all(
        row["timestamp"] == frozen.isoformat() for row in jsonl.read_all()
    )


def test_record_reflection_prediction_omits_calibration_keys_by_default(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 129: with neither additive field supplied the row is byte-identical
    to before — no ``confidence`` / ``loss_condition_kind`` key is written."""
    backend = _prediction_backend(FAKE_DIR)
    record_reflection_prediction(backend, "r1", "p", "loss")
    (row,) = backend.read_all()
    assert "confidence" not in row
    assert "loss_condition_kind" not in row


def test_record_reflection_prediction_writes_calibration_keys_when_supplied(
    mem_fs: dict[str, str],
) -> None:
    """When supplied, the confidence and probe kind land on the row, and the JSONL
    and SQLite substrates produce identical rows."""
    frozen = datetime(2026, 4, 26, tzinfo=UTC)
    jsonl = _prediction_backend(FAKE_DIR)
    sqlite = SqlitePredictionLedger(StateStore(":memory:"))
    with patch("maestro.audit_checks_calibration.datetime") as mock_dt:
        mock_dt.now.return_value = frozen
        for backend in (jsonl, sqlite):
            record_reflection_prediction(
                backend, "r1", "p", "loss",
                confidence=0.8, loss_condition_kind="GATE_FIRING",
            )
    assert jsonl.read_all() == sqlite.read_all()
    (row,) = jsonl.read_all()
    assert row["confidence"] == 0.8
    assert row["loss_condition_kind"] == "GATE_FIRING"


def test_reflection_readers_tolerate_calibration_keys(
    mem_fs: dict[str, str],
) -> None:
    """``load_latest_reflection_prediction`` and ``measure_prediction_accuracy``
    ignore the two additive keys — a calibrated row reads and scores like a bare
    one."""
    backend = _prediction_backend(FAKE_DIR)
    record_reflection_prediction(
        backend, "r1", "bet A", "loss A",
        confidence=0.9, loss_condition_kind="PR_SETTLEMENT",
    )
    record_reflection_verdict(backend, "r1", "correct")

    latest = load_latest_reflection_prediction(backend)
    assert latest is not None
    assert latest["prediction"] == "bet A"
    assert latest["verdict"] == "correct"  # override applied
    assert "confidence" not in latest  # reader projects only canonical fields

    # The verdict-folded reflection row scores correct without tripping on the
    # extra keys.
    result = measure_prediction_accuracy(backend, [])
    assert result["total"] == 1
    assert result["correct"] == 1


def test_load_latest_reflection_prediction_jsonl_sqlite_parity(
    mem_fs: dict[str, str],
) -> None:
    """The latest-reflection reader agrees across both substrates.

    A prediction plus a later verdict override scores identically whether the
    trace lives in ``predictions.jsonl`` or ``state.db``.
    """
    jsonl = _prediction_backend(FAKE_DIR)
    sqlite = SqlitePredictionLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        record_reflection_prediction(backend, "r1", "bet A", "loss A")
        record_reflection_verdict(backend, "r1", "correct")
    latest_jsonl = load_latest_reflection_prediction(jsonl)
    latest_sqlite = load_latest_reflection_prediction(sqlite)
    assert latest_jsonl == latest_sqlite
    assert latest_jsonl is not None
    assert latest_jsonl["prediction"] == "bet A"
    assert latest_jsonl["verdict"] == "correct"  # override applied


def test_measure_prediction_accuracy_jsonl_sqlite_parity(
    mem_fs: dict[str, str],
) -> None:
    """The prediction-accuracy sensor scores identically on both substrates."""
    jsonl = _prediction_backend(FAKE_DIR)
    sqlite = SqlitePredictionLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        record_prediction(backend, "t1", "success")
        record_prediction(backend, "t2", "failure")
    task_history: list[dict[str, object]] = [
        {"task_id": "t1", "outcome": "success", "goal": "fix #1"},
        {"task_id": "t2", "outcome": "success", "goal": "fix #2"},
    ]
    result_jsonl = measure_prediction_accuracy(jsonl, task_history)
    result_sqlite = measure_prediction_accuracy(sqlite, task_history)
    assert result_jsonl == result_sqlite
    assert result_jsonl["total"] == 2
    assert result_jsonl["correct"] == 1


def test_measure_self_calibration_jsonl_sqlite_parity(
    mem_fs: dict[str, str],
) -> None:
    """The calibration reading agrees across both prediction substrates."""
    jsonl = _prediction_backend(FAKE_DIR)
    sqlite = SqlitePredictionLedger(StateStore(":memory:"))
    for backend in (jsonl, sqlite):
        record_reflection_prediction(
            backend, "reflection-1", "p", "loss", confidence=0.8,
        )
        record_reflection_verdict(
            backend, "reflection-1", "correct",
            verdict_source="measured", self_verdict="correct",
        )
        record_reflection_prediction(backend, "reflection-2", "p", "loss")
        record_reflection_verdict(
            backend, "reflection-2", "correct", verdict_source="narrated",
        )
    reading_jsonl = measure_self_calibration(jsonl)
    reading_sqlite = measure_self_calibration(sqlite)
    assert reading_jsonl == reading_sqlite
    assert reading_jsonl.stamped_total == 2
    assert reading_jsonl.narrated == 1


def test_data_sufficiency_counts_predictions_via_backend(
    mem_fs: dict[str, str],
) -> None:
    """A supplied ``prediction_backend`` is the count's source of truth.

    With the flag on, ``predictions.jsonl`` is frozen and new rows land in
    ``state.db``; the data-sufficiency instrument reads the live backend, not the
    stale filename. Here the JSONL file under ``FAKE_DIR`` is missing while the
    SQLite backend holds enough rows, so only the backend-routed count clears the
    threshold.
    """
    from maestro.audit_checks_calibration import (
        DATA_SUFFICIENCY_MIN_OBSERVATIONS,
        check_data_sufficiency,
        format_data_sufficiency_context,
    )

    backend = SqlitePredictionLedger(StateStore(":memory:"))
    for i in range(DATA_SUFFICIENCY_MIN_OBSERVATIONS):
        row: dict[str, object] = {
            "task_id": f"t{i}", "predicted_outcome": "success",
        }
        backend.append(row)
    targets = {"predictions": "predictions.jsonl"}
    # Filename-counted (no backend): the missing file is insufficient.
    assert check_data_sufficiency(FAKE_DIR, targets) == ["predictions"]
    # Backend-counted: the SQLite rows clear the threshold.
    assert check_data_sufficiency(
        FAKE_DIR, targets, prediction_backend=backend,
    ) == []
    ctx = format_data_sufficiency_context(
        FAKE_DIR, targets, prediction_backend=backend,
    )
    assert "predictions" in ctx
    assert "MET" in ctx


def test_known_state_summary_includes_prediction_accuracy() -> None:
    """When prediction_accuracy_context is provided, it appears in the output."""
    result = format_known_state_summary(
        {"gate": True}, ["goal1"],
        prediction_accuracy_context="Prediction accuracy: 75% (8 predictions)",
    )
    assert "Prediction accuracy: 75%" in result
    assert "## Already Known State" in result


def test_known_state_summary_omits_prediction_when_empty() -> None:
    """When prediction_accuracy_context is empty, no prediction line appears."""
    result = format_known_state_summary({"gate": True}, ["goal1"])
    assert "Prediction accuracy" not in result


def test_known_state_summary_prediction_only() -> None:
    """prediction_accuracy_context alone produces non-empty output."""
    result = format_known_state_summary(
        {}, [],
        prediction_accuracy_context="Prediction accuracy: 50% (10 predictions)",
    )
    assert "## Already Known State" in result
    assert "50%" in result


# ---------------------------------------------------------------------------
# check_data_sufficiency
# ---------------------------------------------------------------------------


def test_check_data_sufficiency_all_sufficient(
    mem_fs: dict[str, str],
) -> None:
    """All targets above threshold returns empty list."""
    from maestro.audit_checks_calibration import (
        DATA_SUFFICIENCY_MIN_OBSERVATIONS,
        check_data_sufficiency,
    )

    targets = {"alpha": "alpha.jsonl", "beta": "beta.jsonl"}
    for fname in targets.values():
        path = str(FAKE_DIR / fname)
        mem_fs[path] = "\n".join(
            [f'{{"x": {i}}}' for i in range(DATA_SUFFICIENCY_MIN_OBSERVATIONS)]
        ) + "\n"
    result = check_data_sufficiency(FAKE_DIR, targets)
    assert result == []


def test_check_data_sufficiency_some_insufficient(
    mem_fs: dict[str, str],
) -> None:
    """Only targets below threshold are returned."""
    from maestro.audit_checks_calibration import (
        DATA_SUFFICIENCY_MIN_OBSERVATIONS,
        check_data_sufficiency,
    )

    targets = {"enough": "enough.jsonl", "short": "short.jsonl"}
    mem_fs[str(FAKE_DIR / "enough.jsonl")] = "\n".join(
        [f'{{"x": {i}}}' for i in range(DATA_SUFFICIENCY_MIN_OBSERVATIONS)]
    ) + "\n"
    mem_fs[str(FAKE_DIR / "short.jsonl")] = '{"x": 1}\n{"x": 2}\n'
    result = check_data_sufficiency(FAKE_DIR, targets)
    assert result == ["short"]


def test_check_data_sufficiency_missing_file(
    mem_fs: dict[str, str],
) -> None:
    """Missing files are reported as insufficient."""
    from maestro.audit_checks_calibration import (
        check_data_sufficiency,
    )

    result = check_data_sufficiency(FAKE_DIR, {"gone": "gone.jsonl"})
    assert result == ["gone"]


def test_check_data_sufficiency_empty_file(
    mem_fs: dict[str, str],
) -> None:
    """Empty file is reported as insufficient."""
    from maestro.audit_checks_calibration import (
        check_data_sufficiency,
    )

    mem_fs[str(FAKE_DIR / "empty.jsonl")] = ""
    result = check_data_sufficiency(FAKE_DIR, {"empty": "empty.jsonl"})
    assert result == ["empty"]


def test_check_data_sufficiency_blank_lines_not_counted(
    mem_fs: dict[str, str],
) -> None:
    """Blank lines are not counted as observations."""
    from maestro.audit_checks_calibration import (
        check_data_sufficiency,
    )

    lines = ['{"a":1}', "", "  ", '{"a":2}']
    mem_fs[str(FAKE_DIR / "sparse.jsonl")] = "\n".join(lines) + "\n"
    result = check_data_sufficiency(FAKE_DIR, {"sparse": "sparse.jsonl"})
    assert result == ["sparse"]  # Only 2 non-blank lines < 10


def test_check_data_sufficiency_defaults(
    mem_fs: dict[str, str],
) -> None:
    """When targets is None, DEFAULT_DATA_SUFFICIENCY_TARGETS is used."""
    from maestro.audit_checks_calibration import (
        DEFAULT_DATA_SUFFICIENCY_TARGETS,
        check_data_sufficiency,
    )

    # No files exist → all defaults should be insufficient.
    result = check_data_sufficiency(FAKE_DIR)
    assert set(result) == set(DEFAULT_DATA_SUFFICIENCY_TARGETS.keys())


def test_check_data_sufficiency_oserror(
    mem_fs: dict[str, str],
) -> None:
    """OSError during file read treats target as insufficient."""
    from maestro.audit_checks_calibration import (
        check_data_sufficiency,
    )

    mem_fs[str(FAKE_DIR / "bad.jsonl")] = "content"
    with patch("builtins.open", side_effect=OSError("disk")):
        result = check_data_sufficiency(FAKE_DIR, {"bad": "bad.jsonl"})
    assert result == ["bad"]


def test_check_data_sufficiency_exact_threshold(
    mem_fs: dict[str, str],
) -> None:
    """Exactly at threshold is sufficient (not insufficient)."""
    from maestro.audit_checks_calibration import (
        DATA_SUFFICIENCY_MIN_OBSERVATIONS,
        check_data_sufficiency,
    )

    mem_fs[str(FAKE_DIR / "exact.jsonl")] = "\n".join(
        [f'{{"i": {i}}}' for i in range(DATA_SUFFICIENCY_MIN_OBSERVATIONS)]
    ) + "\n"
    result = check_data_sufficiency(FAKE_DIR, {"exact": "exact.jsonl"})
    assert result == []


# ---------------------------------------------------------------------------
# format_data_sufficiency_context
# ---------------------------------------------------------------------------


def test_data_sufficiency_context_met(
    mem_fs: dict[str, str],
) -> None:
    """Instruments above threshold show MET."""
    from maestro.audit_checks_calibration import (
        DATA_SUFFICIENCY_MIN_OBSERVATIONS,
        format_data_sufficiency_context,
    )

    targets = {"alpha": "alpha.jsonl"}
    path = str(FAKE_DIR / "alpha.jsonl")
    mem_fs[path] = "\n".join(
        [f'{{"x": {i}}}' for i in range(DATA_SUFFICIENCY_MIN_OBSERVATIONS + 5)]
    ) + "\n"
    result = format_data_sufficiency_context(FAKE_DIR, targets)
    assert "Data sufficiency:" in result
    count = DATA_SUFFICIENCY_MIN_OBSERVATIONS + 5
    assert f"alpha: {count}/{DATA_SUFFICIENCY_MIN_OBSERVATIONS} records" in result
    assert "MET" in result
    assert "UNMET" not in result


def test_data_sufficiency_context_unmet(
    mem_fs: dict[str, str],
) -> None:
    """Instruments below threshold show UNMET with count."""
    from maestro.audit_checks_calibration import (
        DATA_SUFFICIENCY_MIN_OBSERVATIONS,
        format_data_sufficiency_context,
    )

    targets = {"beta": "beta.jsonl"}
    mem_fs[str(FAKE_DIR / "beta.jsonl")] = '{"x": 1}\n{"x": 2}\n'
    result = format_data_sufficiency_context(FAKE_DIR, targets)
    assert "Data sufficiency:" in result
    assert f"beta: 2/{DATA_SUFFICIENCY_MIN_OBSERVATIONS} records" in result
    assert "UNMET" in result


def test_data_sufficiency_context_missing(
    mem_fs: dict[str, str],
) -> None:
    """Missing JSONL files produce 'no data' without exceptions."""
    from maestro.audit_checks_calibration import (
        format_data_sufficiency_context,
    )

    result = format_data_sufficiency_context(
        FAKE_DIR, {"gone": "gone.jsonl"},
    )
    assert "gone: no data" in result
    assert "MET" not in result
    assert "UNMET" not in result


def test_data_sufficiency_context_line_cap(
    mem_fs: dict[str, str],
) -> None:
    """Each instrument line is ≤200 characters."""
    from maestro.audit_checks_calibration import (
        _DATA_SUFFICIENCY_LINE_CAP,
        format_data_sufficiency_context,
    )

    # Use a very long instrument name to trigger the cap.
    long_name = "x" * 250
    result = format_data_sufficiency_context(
        FAKE_DIR, {long_name: "long.jsonl"},
    )
    for line in result.splitlines():
        assert len(line) <= _DATA_SUFFICIENCY_LINE_CAP


def test_data_sufficiency_context_empty_targets() -> None:
    """Empty targets dict returns empty string."""
    from maestro.audit_checks_calibration import (
        format_data_sufficiency_context,
    )

    assert format_data_sufficiency_context(FAKE_DIR, {}) == ""


def test_data_sufficiency_context_oserror(
    mem_fs: dict[str, str],
) -> None:
    """OSError during file read produces 'no data'."""
    from maestro.audit_checks_calibration import (
        format_data_sufficiency_context,
    )

    mem_fs[str(FAKE_DIR / "bad.jsonl")] = "content"
    with patch("builtins.open", side_effect=OSError("disk")):
        result = format_data_sufficiency_context(
            FAKE_DIR, {"bad": "bad.jsonl"},
        )
    assert "bad: no data" in result

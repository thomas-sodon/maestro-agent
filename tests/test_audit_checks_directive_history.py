"""Directive-compliance reader tolerance for non-object history rows (#4079).

Sibling of ``tests/test_audit_checks.py``, which is pinned shrink-only by
``tests/test_source_file_size.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from maestro.audit_checks import (
    JsonlGateActivationLedger,
    _load_directive_compliance_tasks,
    measure_directive_compliance,
)

FAKE_DIR = Path("/fake/mem")


def test_load_directive_compliance_tasks_skips_non_object_lines(
    mem_fs: dict[str, str],
) -> None:
    """Valid-JSON lines that are not objects are skipped, not crashed on (#4079)."""
    path = str(FAKE_DIR / "task_history.jsonl")
    lines = [
        "[]",
        "null",
        "123",
        json.dumps({
            "goal": "good record",
            "timestamp": "2026-06-01T01:00:00+00:00",
        }),
        '"str"',
    ]
    mem_fs[path] = "\n".join(lines) + "\n"
    result = _load_directive_compliance_tasks(FAKE_DIR)
    assert result == [("2026-06-01T01:00:00+00:00", "good record")]


def test_measure_directive_compliance_tolerates_non_object_history_line(
    mem_fs: dict[str, str],
) -> None:
    """A non-object history row does not abort the measurement (#4079)."""
    mem_fs[str(FAKE_DIR / "gate_activations.jsonl")] = (
        json.dumps(
            {
                "gate": "investigation_streak",
                "action": "directive_set",
                "timestamp": "2026-06-01T00:00:00+00:00",
                "detail": "d",
            }
        )
        + "\n"
    )
    mem_fs[str(FAKE_DIR / "task_history.jsonl")] = (
        "[]\n"
        + json.dumps({
            "goal": "build a new instrument",
            "timestamp": "2026-06-01T01:00:00+00:00",
        })
        + "\n"
    )
    result = measure_directive_compliance(
        JsonlGateActivationLedger(FAKE_DIR), FAKE_DIR
    )
    assert result["total_activations"] == 1
    assert result["compliant_count"] == 1

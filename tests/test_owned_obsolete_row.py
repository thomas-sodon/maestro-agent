"""Tests for :func:`maestro.remediation_tracker.is_owned_obsolete_row` (#4385).

The predicate is the single owner of the #4157 rule that a door-refused mint
is not an owned prune; the structural guard pins every obsolete-row reader to
it so a future reader cannot hand-copy the status check and drop the skip.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import ModuleType

import pytest

from maestro import audit_checks, remediation_tracker, task_gates
from maestro.remediation_tracker import is_owned_obsolete_row


@pytest.mark.parametrize(
    ("rec", "expected"),
    [
        ({"status": "pending_review"}, False),
        ({"status": "pending_review", "refused_at_submission": False}, False),
        ({"status": "obsolete", "refused_at_submission": True}, False),
        ({"status": "obsolete", "refused_at_submission": False}, True),
        ({"status": "obsolete"}, True),
        # Only a literal ``True`` stamp refuses, matching the prior ``is True``.
        ({"status": "obsolete", "refused_at_submission": 1}, True),
        ({"status": "obsolete", "refused_at_submission": "true"}, True),
    ],
)
def test_is_owned_obsolete_row(rec: dict[str, object], expected: bool) -> None:
    assert is_owned_obsolete_row(rec) is expected


_OWNED_ACT_READERS = (
    (task_gates, "_obsolete_plan_touched_ids"),
    (audit_checks, "_banked_obsolete_plan_ids"),
    (audit_checks, "_read_obsolete_plan_events"),
    (remediation_tracker, "read_obsolete_plan_acts"),
)


def _function(module: ModuleType, name: str) -> ast.FunctionDef:
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    return next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


@pytest.mark.parametrize(("module", "name"), _OWNED_ACT_READERS)
def test_owned_act_readers_route_through_predicate(
    module: ModuleType, name: str,
) -> None:
    func = _function(module, name)
    calls = {
        node.func.id for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    obsolete_literals = [
        node for node in ast.walk(func)
        if isinstance(node, ast.Constant) and node.value == "obsolete"
    ]
    assert "is_owned_obsolete_row" in calls
    assert obsolete_literals == []

"""Tests for the WorkTarget value object (proposal 107) — in-memory only.

These tests exercise the value object itself: it carries all four facets and is
frozen. Resolver behaviour (``Config.work_target``) is covered in ``test_config.py``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from maestro.work_target import WorkTarget


def _sample() -> WorkTarget:
    return WorkTarget(
        repo_workspace=Path("/repo"),
        github_repo="owner/repo",
        gate_source_path="maestro/",
        gate_tests_path="tests/",
        gate_lint_extra_paths=("scripts/",),
        task_framing="self_evolution",
        focus_profile="self_evolution",
    )


def test_work_target_holds_all_facets() -> None:
    """Every facet is stored verbatim and readable back."""
    target = _sample()
    assert target.repo_workspace == Path("/repo")
    assert target.github_repo == "owner/repo"
    assert target.gate_source_path == "maestro/"
    assert target.gate_tests_path == "tests/"
    assert target.gate_lint_extra_paths == ("scripts/",)
    assert target.task_framing == "self_evolution"
    assert target.focus_profile == "self_evolution"


def test_work_target_is_frozen() -> None:
    """A resolved target cannot drift — assigning a field raises."""
    target = _sample()
    with pytest.raises(FrozenInstanceError):
        target.focus_profile = "external_delivery"  # type: ignore[misc]




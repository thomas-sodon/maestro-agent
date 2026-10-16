"""Regression guard: PRs for evolution plans funnel through one call site.

Issue #2280: the task-execution path could open/merge a PR via Bash, bypassing
the human-in-the-loop approval gate.  These deterministic source-scanning
tests pin the invariant that:

1. The ``gh pr create`` *command* is constructed in exactly one module
   (``maestro/vcs.py``, the VCS primitive).
2. No task-execution module or step/plan prompt builder constructs or instructs
   a ``gh pr create|merge|ready`` operation.
3. ``EvolutionEngine.create_pr`` (the approval-gated path) is the production
   method that opens an evolution PR, routing through the VCS primitive.

If a future change reintroduces a PR-opening shortcut on the task path, one of
these tests fails.
"""

from __future__ import annotations

import re
from pathlib import Path

_MAESTRO_ROOT = Path(__file__).resolve().parent.parent / "maestro"

#: The subprocess argv that builds a ``gh pr create`` command, e.g.
#: ``["gh", "pr", "create", ...]``.  Quoting/spacing tolerant.
_GH_PR_CREATE_ARGV = re.compile(
    r"""['"]gh['"]\s*,\s*['"]pr['"]\s*,\s*['"]create['"]""",
)

#: Free-text / shell-string PR mutations.
_GH_PR_MUTATION_TEXT = re.compile(r"gh\s+pr\s+(?:create|merge|ready)")

#: Modules that run on the task-execution path or build its prompts.  None of
#: these may construct or instruct a PR mutation.
_TASK_PATH_MODULES = (
    "executor.py",
    "planner.py",
    "prompts/defaults.py",
    "prompts/__init__.py",
)


def _iter_py_files() -> list[Path]:
    return [
        p
        for p in _MAESTRO_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def test_gh_pr_create_argv_only_in_vcs() -> None:
    """The ``gh pr create`` command is constructed only in ``vcs.py``."""
    matching = {
        p.relative_to(_MAESTRO_ROOT).as_posix()
        for p in _iter_py_files()
        if _GH_PR_CREATE_ARGV.search(p.read_text(encoding="utf-8"))
    }
    assert matching == {"vcs.py"}


def test_task_path_modules_have_no_pr_mutation() -> None:
    """No task-execution module builds or instructs a PR mutation."""
    offenders: dict[str, list[str]] = {}
    for rel in _TASK_PATH_MODULES:
        source = (_MAESTRO_ROOT / rel).read_text(encoding="utf-8")
        hits = _GH_PR_MUTATION_TEXT.findall(source)
        hits += _GH_PR_CREATE_ARGV.findall(source)
        if hits:
            offenders[rel] = hits
    assert offenders == {}


def test_evolution_create_pr_routes_through_vcs_primitive() -> None:
    """``EvolutionEngine.create_pr`` opens the evolution PR via the host."""
    source = (_MAESTRO_ROOT / "evolution.py").read_text(encoding="utf-8")
    assert "async def create_pr(" in source
    assert "self._host.create_pr(" in source

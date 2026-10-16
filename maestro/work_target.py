"""One owner for which repo, which gates, which framing, and how much self-focus.

Proposal 107, increment 1. Maestro's loop is wired to work on *itself*: four
separate facts encode that assumption — which repo (``Config.workspace`` /
``Config.github_repo``), which gates (the ``maestro/`` + ``tests/`` path atoms
baked into every quality-gate command list), which framing (task-generation /
evolution prompts framed as self-improvement), and how much self-focus (the
introspective phases running on fixed cadences). Each is re-derived independently
at many call sites with no single owner.

:class:`WorkTarget` is that owner — a frozen value object assembling those four
facets so a subsystem reads a facet through one place instead of re-deriving it,
and so an operator can later redirect the whole loop at a customer repo by editing
config alone.

The two gate-path atoms are consumed by the four quality-gate readers
(``gate_check.build_gate_commands``, ``red_root.build_red_root_gates``,
``evolution.run_quality_gates``, ``loop/pr_remediation.preflight``), each
substituting them into its own reader-specific flag set — enforced by
``tests/test_gate_command_owner.py``. The two repo-location facets
(``repo_workspace`` / ``github_repo``) are consumed by the loop-facing git /
GitHub client construction seams (``cli._build_agent``,
``cli._build_mcp_serve_registry``, ``cli.cmd_pr_owed_act``, ``cli.cmd_issues``,
and the ``MaestroAgent`` / ``EvolutionEngine`` fallback constructions), so a
``[work]`` redirect points the constructed clients at another checkout/repo.
The focus dial is read by ``loop/lifecycle.phase_will_run``, which suppresses
``_FOCUS_SUPPRESSED_PHASES`` under ``external_delivery``; the framing is read by
the ``prompts`` selector ``select_task_generation_prompt`` from
``loop/task_generation``.

The gate facet is modeled as the two redirectable path atoms
(:attr:`WorkTarget.gate_source_path`, :attr:`WorkTarget.gate_tests_path`) rather
than full command tuples, because the four gate readers each embed those atoms
inside their own reader-specific flag sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: How a unit of work is framed and how much the loop favours it over
#: introspection. ``self_evolution`` is today's behaviour (Maestro works on
#: itself); ``external_delivery`` points effort at another repo and dials down
#: the navel-gazing. Validated by ``Config.validate`` against
#: ``VALID_WORK_FRAMINGS``.
WorkFraming = Literal["self_evolution", "external_delivery"]

@dataclass(frozen=True)
class WorkTarget:
    """The facets of where Maestro's effort is pointed, resolved once.

    Assembled solely by :meth:`maestro.config.Config.work_target`; this module
    performs no I/O and reads no config of its own. Frozen so a resolved target
    cannot drift after assembly.
    """

    #: The git checkout the loop operates on (``cwd=`` for subprocesses). Defaults
    #: to ``Config.workspace`` when no ``[work]`` override is set.
    repo_workspace: Path
    #: The ``owner/repo`` PR target, or ``None`` when unset. Defaults to
    #: ``Config.github_repo``.
    github_repo: str | None
    #: The source-tree path atom the quality gates lint/type-check (``maestro/``
    #: for self). One of the two redirectable gate atoms.
    gate_source_path: str
    #: The tests path atom the quality gates run (``tests/`` for self). The second
    #: redirectable gate atom.
    gate_tests_path: str
    #: Extra path atoms the lint/type-check gates (ruff, mypy) cover beyond
    #: ``gate_source_path`` — ``("scripts/",)`` for self, matching CI's
    #: ``quality`` job. pytest never collects them and coverage never measures them. A
    #: ``[work]`` retarget of the source/tests atoms must override this too (e.g.
    #: to ``[]``), or ruff/mypy fail on the inherited missing ``scripts/`` path.
    gate_lint_extra_paths: tuple[str, ...]
    #: How task-generation / evolution prompts frame a unit of work.
    task_framing: WorkFraming
    #: How much the loop favours delivery work over the introspective phases.
    focus_profile: WorkFraming

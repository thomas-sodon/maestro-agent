"""The eight quality-gate sites, named once so four guards share one list.

Proposal 126. Several invariants pin *how a gate run is spelled* across the tree,
and all must scan the same set of files or one can drift out from under another:

* ``tests/test_gate_command_owner.py`` (proposal 107) — no Python reader may
  re-hardcode a gate command's *path* atoms; it AST-scans :data:`PYTHON_GATE_READERS`.
* ``tests/test_gate_lease_owner.py`` (proposal 126, step 6) — no gate site may
  name a *worker count*; it AST-scans the same Python readers and text-scans
  :data:`SHELL_GATE_RUNNER` plus :data:`COMMAND_DOC_GATE_SITES`.
* ``tests/test_gate_lease_owner.py`` (proposal 134) — the same module's
  deadline and scope rules: no Python reader may name a *literal timeout* and
  exactly one of them may derive the lease *deadline*, both AST-scanned over
  :data:`PYTHON_GATE_READERS`; and exactly one ``run_quality_gates`` call may
  narrow the *suite scope*, with the fix loop's certifying reads provably whole.
* ``tests/test_quality_gate_scheduler.py`` — pins the ``--dist loadfile``
  scheduler on :data:`SHELL_GATE_RUNNER` and :data:`COMMAND_DOC_GATE_SITES`.

Exporting the paths from one module means adding, renaming, or removing a gate
site is a single edit here rather than four copies that can silently diverge —
so a gate-site rename is known to move four invariants, not three.
Pure path constants — importing this module has no side effects.
"""

from __future__ import annotations

from pathlib import Path

#: Repo root — this file lives at ``tests/support/gate_sites.py``.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The ``maestro/`` package root the Python-reader scans resolve against.
MAESTRO_ROOT = REPO_ROOT / "maestro"

#: The four in-process gate readers, ``maestro``-relative. Each builds a pytest
#: argv; a re-hardcoded gate *path* (proposal 107), *worker count* (proposal
#: 126), literal *deadline* or narrowed *scope* (proposal 134) at any of them is
#: what the AST guards forbid.
PYTHON_GATE_READERS: tuple[str, ...] = (
    "gate_check.py",
    "red_root.py",
    "evolution_quality.py",
    "loop/pr_remediation.py",
)

#: The only scripted gate site that names a worker count — the shell runner the
#: Stop hook and the ``/quality-gates`` / ``/merge-main`` docs delegate to.
SHELL_GATE_RUNNER = REPO_ROOT / "scripts" / "quality-gates.sh"

#: The three command docs that prescribe the core suite as a raw command the
#: agent runs by hand — the path that actually produces simultaneous full-width
#: runs, so routing it is what makes the lease observable.
COMMAND_DOC_GATE_SITES: tuple[Path, ...] = (
    REPO_ROOT / ".claude" / "commands" / "impl.md",
    REPO_ROOT / ".claude" / "commands" / "polish.md",
    REPO_ROOT / ".claude" / "commands" / "process-comments.md",
)

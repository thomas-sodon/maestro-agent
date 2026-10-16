"""Regression: the sibling ``maestro_sdk/src`` tree stays on mypy's search path.

``maestro/providers/cli_paced.py`` imports the sibling ``maestro_sdk`` package,
which is installed separately (editable) in CI but is absent on the loop devbox
where self-review runs ``mypy``. Without ``maestro_sdk/src`` on mypy's
module-search path, that file reported ~20 phantom ``attr-defined`` errors
("Module 'maestro_sdk' has no attribute ...") that poisoned the RED/GREEN
instrument — health summaries, red-root diagnosis, and self-review verdicts all
inherited fabricated failures, so real RED could not be told from noise.

``[tool.mypy] mypy_path = "maestro_sdk/src"`` in ``pyproject.toml`` (the
authoritative config that governs ``python3.14 -m mypy maestro/``) cures this.
This test kills the regression where a future config rewrite silently drops the
sibling path and re-introduces phantom RED.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

# The authoritative mypy configuration file. No setup.cfg / mypy.ini exists in
# this tree, so pyproject.toml's ``[tool.mypy]`` table is what mypy reads.
_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

_SIBLING_SRC = "maestro_sdk/src"


def _resolved_mypy_path() -> list[str]:
    """Return ``[tool.mypy] mypy_path`` as a list of path entries.

    mypy accepts ``mypy_path`` as either a single string or a list of strings;
    normalise both shapes so the assertion below is shape-agnostic.
    """
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    raw = data["tool"]["mypy"]["mypy_path"]
    if isinstance(raw, str):
        return [raw]
    return list(raw)


def test_maestro_sdk_src_on_mypy_path() -> None:
    """``maestro_sdk/src`` must be a resolved ``mypy_path`` entry."""
    assert _SIBLING_SRC in _resolved_mypy_path()

"""Regression guard for the packaging boundary in ``pyproject.toml``.

Maestro's distribution is discovered with ``include = ["maestro*"]``, a pattern
greedy enough to swallow the sibling source trees ``maestro_sdk/`` and
``maestro_review/`` — both live at the repo root, both carry their own
``pyproject.toml``, and both are installed separately. The single
``exclude = ["maestro_review*", "maestro_sdk*"]`` line is the whole enforcement.

Dropping it is silently catastrophic for ``maestro_sdk``: the repo-root
directory holds no importable module (the real package is under
``maestro_sdk/src/maestro_sdk``), so setuptools maps it as a PEP 420 namespace
rooted at an empty directory, shadowing the real package and turning
``import maestro_sdk.mcp_config`` into ``ModuleNotFoundError`` whenever the
sibling is not installed first. ``maestro_review`` is merely absorbed into the
wrong distribution. Neither failure reaches ruff, mypy, or pytest — it surfaces
only on a fresh-environment install. These tests make that one-line invariant
fail loud in CI.

Matching is done with :func:`fnmatch.fnmatchcase`, the matcher setuptools' own
``PackageFinder`` uses for ``packages.find`` patterns, so the assertions test
the *effect* of the patterns rather than byte-comparing a list — a reordering
or a differently-spelled but equivalent ``exclude`` pattern does not produce a
false red.

One deliberate exception to pure effect-testing: the *mechanism* is pinned too.
CLAUDE.md states the invariant as "sibling ``maestro_sdk*``/``maestro_review*``
are excluded from maestro's ``packages.find``", so the siblings must be kept out
by an ``exclude`` pattern rather than by a narrowed ``include``. A narrowed
include would be equally correct at the distribution level but silently stops
covering any future sibling, which is exactly the regression this guard exists
to catch — so it is held red on purpose.
"""

from __future__ import annotations

import tomllib
from fnmatch import fnmatchcase
from importlib.machinery import PathFinder
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: The sibling source trees that must stay out of maestro's distribution, each
#: paired with a dotted submodule so a pattern that only matches the bare
#: distribution name cannot pass. Pinned by name deliberately: a future third
#: sibling is a decision that should update this tuple.
_SIBLINGS = ("maestro_sdk", "maestro_review")

_SIBLING_MODULES = (
    "maestro_sdk",
    "maestro_sdk.mcp_config",
    "maestro_review",
    "maestro_review.review",
)

#: Maestro's own distribution, which the exclude patterns must *not* touch.
_OWN_MODULES = ("maestro", "maestro.agent")

_INVARIANT = (
    "CLAUDE.md 'Packaging boundary' invariant: sibling maestro_sdk*/maestro_review* "
    "must stay excluded from maestro's packages.find, or a bare `pip install -e .` "
    "shadows the real maestro_sdk package with an empty namespace and turns "
    "`import maestro_sdk.mcp_config` into ModuleNotFoundError."
)


def _packages_find() -> dict[str, Any]:
    """Return the real ``[tool.setuptools.packages.find]`` table.

    Reading the real ``pyproject.toml`` is ground-truth input for "what the
    build backend actually discovers" — it is not test-generated I/O, so no
    ``mem_fs`` fixture is involved.
    """
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    find = data["tool"]["setuptools"]["packages"]["find"]
    assert isinstance(find, dict)
    return find


def _matches_any(name: str, patterns: list[str]) -> bool:
    """Whether ``name`` matches any pattern, the way setuptools matches."""
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def _is_discovered(name: str, find: dict[str, Any]) -> bool:
    """Whether ``find`` discovers ``name``: included and not excluded."""
    return _matches_any(name, list(find["include"])) and not _matches_any(
        name, list(find.get("exclude", []))
    )


def test_siblings_are_excluded_from_maestro_distribution() -> None:
    """No sibling name or submodule may survive discovery, via ``exclude``.

    The second assertion pins the mechanism deliberately — see the module
    docstring: a narrowed ``include`` is rejected even though it would exclude
    today's siblings just as well.
    """
    find = _packages_find()
    for name in _SIBLING_MODULES:
        assert not _is_discovered(name, find), f"{name} is discovered. {_INVARIANT}"
        assert _matches_any(name, list(find["exclude"])), (
            f"{name} must be kept out by an exclude pattern, not by a narrowed "
            f"include. {_INVARIANT}"
        )


def test_include_alone_would_sweep_the_siblings_in() -> None:
    """Seeded offender: with ``exclude`` dropped, the siblings are discovered.

    This is what proves the exclude line is load-bearing rather than
    decorative — the *same* predicate, evaluated against an in-memory table
    whose ``exclude`` key has been removed, must discover every sibling.
    """
    without_exclude: dict[str, Any] = {"include": list(_packages_find()["include"])}
    for name in _SIBLING_MODULES:
        assert _is_discovered(name, without_exclude), (
            f"{name} is no longer swept in by include alone — the seeded-offender "
            "control has gone vacuous."
        )


def test_maestro_itself_stays_in_the_distribution() -> None:
    """An over-broad exclude that drops maestro's own packages is a failure."""
    find = _packages_find()
    for name in _OWN_MODULES:
        assert _is_discovered(name, find), f"{name} is not discovered"


def test_pinned_siblings_are_real_sibling_projects() -> None:
    """Vacuity pin: each pinned sibling is a real root-level project today.

    Without this, a sibling rename would leave the exclude assertions passing
    against names that no longer exist anywhere in the tree.
    """
    for name in _SIBLINGS:
        sibling = _REPO_ROOT / name
        assert sibling.is_dir(), f"{name} is not a root-level directory"
        assert (sibling / "pyproject.toml").is_file(), f"{name} has no pyproject.toml"


#: The in-repo source tree the pytest import-path fallback points at, so a run
#: on an environment lacking the separate ``pip install -e "maestro_sdk/"``
#: resolves the sibling instead of aborting collection.
_SDK_SRC = "maestro_sdk/src"


def _pytest_pythonpath() -> list[str]:
    """The configured ``[tool.pytest.ini_options] pythonpath`` entries."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    ini: dict[str, Any] = data["tool"]["pytest"]["ini_options"]
    entries = ini.get("pythonpath", [])
    assert isinstance(entries, list), "pythonpath must be a list"
    return [str(entry) for entry in entries]


def test_pytest_pythonpath_resolves_the_sibling_sdk() -> None:
    """The configured pytest import path resolves ``maestro_sdk`` on its own.

    Two test modules import ``maestro_sdk`` at module top, so an environment
    without the separate editable install aborts at *collection* and degrades
    the whole run to "could not run". Resolving the spec against only the
    configured entries — no site-packages — proves the fallback works without
    importing the sibling.
    """
    search_path = [str(_REPO_ROOT / entry) for entry in _pytest_pythonpath()]
    spec = PathFinder.find_spec("maestro_sdk", search_path)
    assert spec is not None, f"maestro_sdk unresolvable from {search_path}"
    assert spec.origin is not None, "maestro_sdk resolved without an origin"
    assert Path(spec.origin).is_file(), f"{spec.origin} is not a real file"


def test_pytest_pythonpath_is_pinned_to_the_real_sdk_package() -> None:
    """Vacuity pin: the configured fallback names the sibling's real source tree.

    A move of ``maestro_sdk/src/maestro_sdk`` must fail loud here rather than
    silently reinstating the collection abort.
    """
    assert _SDK_SRC in _pytest_pythonpath(), f"{_SDK_SRC} is not on pytest's pythonpath"
    package_init = _REPO_ROOT / _SDK_SRC / "maestro_sdk" / "__init__.py"
    assert package_init.is_file(), f"{package_init} does not exist"

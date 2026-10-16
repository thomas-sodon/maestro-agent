"""Regression guard: the ``maestro.sdk_backends`` package stays retired (proposal 074).

The provider abstraction re-homed the CLI-paced backend directly into
``maestro/providers/cli_paced.py`` and removed the old ``maestro/sdk_backends/``
package, so nothing under ``maestro/`` imports the retired path any more. This
deterministic, zero-LLM-cost scan — the same ``pathlib``-over-``maestro/`` guard
family as ``tests/test_legacy_toml_parser_retired.py`` (AST symbol-scope) and
``tests/test_rank_bm25_retired.py`` (token/regex) — pins that no shipping module
re-imports the retired package and that no live ``.py`` source lingers in its
directory, so a stray ``import maestro.sdk_backends`` cannot silently resurrect a
superseded code path against whatever stale ``.pyc`` happens to be on disk.

It matches on the parsed syntax tree, not a raw text scan, deliberately: the
retired package name legitimately appears in module docstrings (in
``maestro/providers/cli_paced.py`` and ``maestro/providers/registry.py``), so a
text scan would false-positive on that prose while an AST walk sees only real
``import`` / ``from ... import`` statements. It reads the real source files, so it
touches no ``mem_fs``, network, or subprocess.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAESTRO_ROOT = _REPO_ROOT / "maestro"
_RETIRED_DIR = _MAESTRO_ROOT / "sdk_backends"

#: The retired package's importable identities — its fully-qualified dotted name
#: and the bare top-level leaf (an ``import sdk_backends`` relying on ``maestro/``
#: being on ``sys.path``). A dotted module path is retired when it equals one of
#: these or is a submodule of one.
_RETIRED_MODULES = frozenset({"sdk_backends", "maestro.sdk_backends"})


def _iter_py_files() -> list[Path]:
    """Every ``maestro/**/*.py`` source, excluding compiled-bytecode dirs."""
    return [p for p in _MAESTRO_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _references_retired(module: str) -> bool:
    """True when a dotted module path targets the retired package or a submodule."""
    return (
        module in _RETIRED_MODULES
        or module.startswith("maestro.sdk_backends.")
        or module.startswith("sdk_backends.")
    )


def _imported_modules(source: str) -> set[str]:
    """Return every module path an ``import`` statement in *source* could pull in.

    Pure over its *source* string so the real-file scan and the seeded negative
    controls share one definition. Walks the whole tree (:func:`ast.walk`) so an
    import nested inside a function counts too. For ``import a.b`` records
    ``"a.b"``; for ``from a.b import c`` records both the source module ``"a.b"``
    and ``"a.b.c"`` (a ``from`` import can pull a submodule *or* a name — both are
    recorded so a submodule import is caught); for a relative ``from . import c``
    (no absolute module) records the bare leaf ``"c"``.
    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base:
                modules.add(base)
            for alias in node.names:
                modules.add(f"{base}.{alias.name}" if base else alias.name)
    return modules


def _retired_import_offenders(sources: dict[str, str]) -> dict[str, list[str]]:
    """Return ``{relpath: [retired module reference, ...]}`` for re-importers.

    Pure over its *sources* mapping (relative path -> file text), so the real-file
    scan and the seeded negative controls share one definition of "re-imports the
    retired package". A source referencing none contributes no entry.
    """
    offenders: dict[str, list[str]] = {}
    for rel, source in sources.items():
        hits = sorted(m for m in _imported_modules(source) if _references_retired(m))
        if hits:
            offenders[rel] = hits
    return offenders


def test_no_maestro_module_imports_retired_package() -> None:
    """No shipping module under ``maestro/`` imports the retired ``sdk_backends``."""
    sources = {
        p.relative_to(_MAESTRO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in _iter_py_files()
    }
    # Vacuous-pass guard: a mis-scoped path or empty parse would let the absence
    # assertion below pass without validating anything.
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    offenders = _retired_import_offenders(sources)
    assert offenders == {}, (
        "The provider abstraction retired the maestro.sdk_backends package; a "
        f"shipping module re-imported it: {offenders}. The CLI-paced backend lives "
        "in maestro.providers.cli_paced now — delete the import."
    )


def test_retired_directory_holds_no_live_source() -> None:
    """``maestro/sdk_backends/`` holds no live ``.py`` source.

    ``Path.rglob`` yields nothing for an absent directory, so this passes whether
    the retired directory is gone entirely (a clean checkout — it was never
    tracked) or lingers on a developer tree with only ignored bytecode. Re-adding
    ``__init__.py`` or any module there trips CI.
    """
    live = [p for p in _RETIRED_DIR.rglob("*.py") if "__pycache__" not in p.parts]
    assert live == [], (
        "The retired maestro.sdk_backends package must hold no live source; found "
        f"{[p.relative_to(_REPO_ROOT).as_posix() for p in live]}. The seam is dead — "
        "do not re-add modules to it."
    )


def test_scan_flags_each_seeded_import_form() -> None:
    """Negative control: every retired import form is flagged; a docstring-only
    mention is not.

    Without this, a broken scan (returning nothing) would make the retirement
    assertion above pass vacuously. Runs over in-memory source strings, so it
    touches no filesystem — exercising ``import maestro.sdk_backends``,
    ``from maestro.sdk_backends import ...``, ``from maestro import sdk_backends``,
    a bare ``import sdk_backends`` and a submodule of it, a relative
    ``from . import sdk_backends``, and a clean source whose only ``sdk_backends``
    mention sits in a docstring (the property distinguishing this AST guard from a
    raw text scan, pinning the real docstring mentions in cli_paced.py /
    registry.py).
    """
    seeded = {
        "dotted.py": "import maestro.sdk_backends\n",
        "from_pkg.py": "from maestro.sdk_backends import cli_paced\n",
        "from_parent.py": "from maestro import sdk_backends\n",
        "bare.py": "import sdk_backends\n",
        "bare_submodule.py": "import sdk_backends.cli_paced\n",
        "relative.py": "from . import sdk_backends\n",
        "docstring_only.py": '"""Mentions maestro.sdk_backends in prose only."""\nx = 1\n',
    }
    offenders = _retired_import_offenders(seeded)
    assert offenders == {
        "dotted.py": ["maestro.sdk_backends"],
        "from_pkg.py": ["maestro.sdk_backends", "maestro.sdk_backends.cli_paced"],
        "from_parent.py": ["maestro.sdk_backends"],
        "bare.py": ["sdk_backends"],
        "bare_submodule.py": ["sdk_backends.cli_paced"],
        "relative.py": ["sdk_backends"],
    }
    assert "docstring_only.py" not in offenders

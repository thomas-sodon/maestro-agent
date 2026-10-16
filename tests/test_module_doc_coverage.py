"""Guard: every top-level ``maestro/*.py`` module has a ``docs/modules.md`` row.

``docs/modules.md`` is the module-by-module reference table linked from
``CLAUDE.md``, but nothing binds it to the actual set of Python modules on disk,
so it drifts silently: a new ``maestro/*.py`` module can land undocumented, and a
deleted one can linger in the table with nothing to flag either. This source-scan
guard — modeled on ``tests/test_no_line_citations_in_docs.py`` — asserts every
top-level module is cited and no cited top-level module is absent from disk,
turning "does the module doc match reality?" into a CI signal that fails loudly
the next time a module is added (or removed) without touching the table, instead
of a periodic manual archaeology sweep.

Scoped to top-level ``maestro/*.py`` only: subpackage files (``maestro/loop/…``,
``maestro/providers/…``, ``maestro/prompts/…``) and directory rows
(``maestro/dashboard/``) are out of scope, excluded by a no-slash cell regex.

The cell regex is anchored to a table row's leading cell, so "documented" means
a module owns its own row — a module named only in another row's prose does not
count (and, symmetrically, a prose citation of an absent module is not read as a
phantom row).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAESTRO_ROOT = _REPO_ROOT / "maestro"
_MODULES_DOC = _REPO_ROOT / "docs" / "modules.md"

# A backtick-wrapped top-level module cell in the *leading* column of a table
# row: ``| `maestro/<name>.py` | …`` where <name> holds no slash, so subpackage
# rows (``maestro/loop/pr_remediation.py``) and directory rows
# (``maestro/dashboard/``) are naturally excluded — the character class stops at
# the first ``/`` and never reaches the ``.py`` suffix.
#
# Anchoring to the row's leading cell (``^\|`` under ``re.MULTILINE``) — rather
# than matching anywhere in the doc — is deliberate: coverage means a module owns
# its own table row, so a module referenced in *another* row's prose (e.g. a
# ``consumed by `maestro/x.py``` cross-ref) must not falsely count as documented
# when its own row has been deleted, nor a prose citation of an absent module
# read as a phantom row. The doc uses bare names in cross-refs today, but the
# anchor makes the "owns a row" semantics robust to that drift.
_TOP_LEVEL_CELL = re.compile(r"^\|\s*`maestro/([A-Za-z0-9_]+)\.py`", re.MULTILINE)


def _top_level_modules() -> set[str]:
    """Every top-level ``maestro/*.py`` module stem (excluding ``__init__``).

    Non-recursive ``glob("*.py")`` so subpackage files are out of scope, mirroring
    the top-level-only reach of the doc-cell regex.
    """
    return {p.stem for p in _MAESTRO_ROOT.glob("*.py") if p.stem != "__init__"}


def _documented_modules(text: str) -> set[str]:
    """Top-level module names owning a ``| `maestro/<name>.py` |`` leading row cell.

    Scoped to the leading table cell only (see ``_TOP_LEVEL_CELL``), so a module
    named only in another row's prose does not count as documented.
    """
    return set(_TOP_LEVEL_CELL.findall(text))


def test_modules_doc_and_module_set_are_non_empty() -> None:
    """Vacuous-pass guard: the doc and the module set must both be present.

    Without this, a missing ``docs/modules.md`` or an empty module glob would let
    the coverage assertions below pass without validating anything (a symmetric
    difference against an empty set is empty).
    """
    assert _MODULES_DOC.is_file(), (
        f"Expected {_MODULES_DOC} to exist; the module reference table was moved "
        "or renamed, so the coverage guard would validate nothing."
    )
    modules = _top_level_modules()
    assert modules, (
        f"Expected at least one top-level maestro/*.py module under "
        f"{_MAESTRO_ROOT}; the glob found none, so the coverage guard would pass "
        "vacuously."
    )


def test_every_top_level_module_is_documented() -> None:
    """Every top-level ``maestro/*.py`` module must have a ``docs/modules.md`` row."""
    documented = _documented_modules(_MODULES_DOC.read_text(encoding="utf-8"))
    undocumented = sorted(_top_level_modules() - documented)
    assert not undocumented, (
        "Top-level maestro modules missing a docs/modules.md row: "
        + ", ".join(undocumented)
        + ". Add one `| `maestro/<name>.py` | <purpose> |` row per module "
        "(purpose drawn from the module docstring), or the module doc drifts "
        "silently behind the tree."
    )


def test_no_documented_module_is_absent_from_disk() -> None:
    """``docs/modules.md`` must not cite a top-level module absent from disk."""
    documented = _documented_modules(_MODULES_DOC.read_text(encoding="utf-8"))
    phantom = sorted(documented - _top_level_modules())
    assert not phantom, (
        "docs/modules.md cites top-level maestro modules absent from disk: "
        + ", ".join(phantom)
        + ". Prune each stale row, or the table points readers at modules that "
        "were deleted or renamed."
    )


def test_undocumented_detector_fires_on_seeded_module() -> None:
    """Negative control: a fabricated undocumented module must be flagged.

    Seeds the fault purely in-memory (a module name absent from a doc string), so
    a regression in ``_documented_modules`` that matched everything would be
    caught here instead of letting the forward assertion pass vacuously.
    """
    documented = _documented_modules("| `maestro/agent.py` | Core loop. |")
    modules = {"agent", "fabricated_undocumented_module"}
    undocumented = sorted(modules - documented)
    assert undocumented == ["fabricated_undocumented_module"]


def test_phantom_detector_fires_on_seeded_citation() -> None:
    """Negative control: a fabricated ``maestro/does_not_exist.py`` cell must be flagged."""
    documented = _documented_modules("| `maestro/does_not_exist.py` | Phantom row. |")
    phantom = sorted(documented - {"agent", "journal"})
    assert phantom == ["does_not_exist"]


def test_cell_regex_excludes_subpackage_and_directory_rows() -> None:
    """The no-slash cell regex must ignore subpackage and directory rows.

    Proves the parser scopes coverage to top-level modules only — without this a
    regex that swept slashes in would over-count subpackage/directory rows and
    silently weaken both forward assertions.
    """
    doc = (
        "| `maestro/agent.py` | top-level module row |\n"
        "| `maestro/loop/pr_remediation.py` | subpackage row |\n"
        "| `maestro/dashboard/` | directory row |\n"
    )
    assert _documented_modules(doc) == {"agent"}


def test_cell_regex_ignores_prose_cross_reference() -> None:
    """A module named only in another row's prose must not count as documented.

    Locks in the leading-cell anchor: ``agent`` owns a row, but ``journal`` is
    named only inside ``agent``'s purpose cell, so a whole-doc regex would falsely
    count ``journal`` as documented (masking a deleted ``journal`` row) — and
    a phantom prose citation as a phantom row.
    """
    doc = "| `maestro/agent.py` | Core loop; consumed by `maestro/journal.py`. |\n"
    assert _documented_modules(doc) == {"agent"}

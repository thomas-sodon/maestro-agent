"""Regression guard: the hand-maintained TOML parser stays retired (proposal 106).

Proposal 106 made the declarative ``pydantic-settings`` parser in
``maestro/config_settings.py`` the **sole** ``maestro.toml`` parser and deleted the
hand-built loader: the ``_TOML_TO_CONFIG`` mapping table, the ``_flatten``
flatten-and-coerce step, and the ``load_toml_layers`` layered loader. The
``_read_toml`` / ``_deep_merge`` file-read + overlay-merge primitives and the
``_KNOWN_EXTERNAL_SECTIONS`` external-section registry survive — the declarative
builder calls them.

This deterministic, zero-LLM-cost AST + token scan — the same ``pathlib``-over-
``maestro/`` guard family as ``tests/test_migration_rewrite_retired.py`` (AST
symbol-scope) and ``tests/test_rank_bm25_retired.py`` (token/regex) — pins that
``toml_config.py`` no longer defines the retired symbols while the survivors
remain, and that the retired ``pydantic_settings_enabled`` cut-over flag token is
gone from every site it touched. Matching on the parsed syntax tree (not a raw
``load_toml_layers`` text scan) is what keeps a symbol named only in a comment or
docstring from masquerading as a live definition. It reads the real source files,
so it touches no ``mem_fs``, network, or subprocess.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOML_CONFIG = _REPO_ROOT / "maestro" / "toml_config.py"

#: Module-level symbols proposal 106 deleted from ``toml_config.py``: the two
#: retired functions (the flatten step and the layered loader) and the mapping
#: table (a module-level assignment, not a function).
_RETIRED_FUNCTIONS = frozenset({"_flatten", "load_toml_layers"})
_RETIRED_ASSIGNMENTS = frozenset({"_TOML_TO_CONFIG"})

#: The read + overlay-merge primitives and the external-section registry that must
#: survive the collapse — asserting they remain defined stops the guard passing by
#: scanning the wrong (or an empty) file.
_SURVIVING_FUNCTIONS = frozenset({"_read_toml", "_deep_merge"})
_SURVIVING_ASSIGNMENTS = frozenset({"_KNOWN_EXTERNAL_SECTIONS"})

#: The retired cut-over flag token and every site it touched — it must be gone
#: from all of them (the field, the mapping/loader module, the parser, the
#: manifest, and the committed defaults).
_FLAG_TOKEN = "pydantic_settings_enabled"
_FLAG_SITES = (
    "maestro/config.py",
    "maestro/toml_config.py",
    "maestro/config_settings.py",
    "maestro/flag_lifecycle.py",
    "maestro.toml",
)


def _module_level_functions(source: str) -> set[str]:
    """Return the names of every module-level ``def`` in *source*.

    Pure over its *source* string so the real-file scan and the seeded negative
    control share one definition. Nested defs (methods, closures) are ignored —
    only the module body counts.
    """
    tree = ast.parse(source)
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _module_level_assignments(source: str) -> set[str]:
    """Return the names bound by a module-level assignment in *source*.

    Handles both a bare ``NAME = ...`` (:class:`ast.Assign`) and an annotated
    ``NAME: T = ...`` (:class:`ast.AnnAssign`) — ``_TOML_TO_CONFIG`` was declared
    as the latter. Pure over its *source* string so the real-file scan and the
    seeded negative control share one definition.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _sites_with_token(token: str, sources: dict[str, str]) -> list[str]:
    """Return the sorted relpaths whose text contains *token*.

    Pure over its *sources* mapping (relpath -> file text), so the real-file scan
    and the seeded negative control share one definition of "still references".
    """
    return sorted(rel for rel, text in sources.items() if token in text)


def test_legacy_parser_functions_and_table_retired() -> None:
    """``toml_config.py`` defines neither retired function nor the mapping table,
    while the read/merge survivors remain."""
    source = _TOML_CONFIG.read_text(encoding="utf-8")
    functions = _module_level_functions(source)
    assignments = _module_level_assignments(source)
    # Vacuous-pass guard: a mis-scoped path or empty parse would let the absence
    # assertions below pass without validating anything.
    assert functions >= _SURVIVING_FUNCTIONS, (
        f"Expected the surviving read/merge functions {sorted(_SURVIVING_FUNCTIONS)} "
        f"to remain defined in {_TOML_CONFIG}; found {sorted(functions)}"
    )
    assert assignments >= _SURVIVING_ASSIGNMENTS, (
        f"Expected {sorted(_SURVIVING_ASSIGNMENTS)} to remain defined in "
        f"{_TOML_CONFIG}; found {sorted(assignments)}"
    )
    resurrected_fns = _RETIRED_FUNCTIONS & functions
    assert resurrected_fns == set(), (
        "Proposal 106 deleted the hand-built loader; "
        f"{sorted(resurrected_fns)} reappeared in {_TOML_CONFIG}. The declarative "
        "pydantic parser in config_settings.py is the sole parser."
    )
    resurrected_tables = _RETIRED_ASSIGNMENTS & assignments
    assert resurrected_tables == set(), (
        "Proposal 106 deleted the (section, key) -> (field, type) mapping table; "
        f"{sorted(resurrected_tables)} reappeared in {_TOML_CONFIG}. The section "
        "models in config_settings.py are the source of truth."
    )


def test_scan_flags_seeded_retired_and_keeps_survivors() -> None:
    """Negative control: the AST scans detect a seeded retired function/table and
    do not misclassify a survivor.

    Without this, a broken scan (returning nothing) would make the retirement
    assertions above pass vacuously. Runs over an in-memory source string, so it
    touches no filesystem — exercising a retired function, the retired mapping
    assignment, and the surviving read/merge symbols that must not be flagged.
    """
    seeded = (
        "_TOML_TO_CONFIG = {}\n"
        "_KNOWN_EXTERNAL_SECTIONS = frozenset()\n"
        "def _flatten(data):\n    return {}\n"
        "def load_toml_layers(config_dir, env=None):\n    return {}\n"
        "def _read_toml(path):\n    return {}\n"
        "def _deep_merge(base, overlay):\n    return base\n"
    )
    functions = _module_level_functions(seeded)
    assignments = _module_level_assignments(seeded)
    assert _RETIRED_FUNCTIONS & functions == {"_flatten", "load_toml_layers"}
    assert _RETIRED_ASSIGNMENTS & assignments == {"_TOML_TO_CONFIG"}
    # The survivors present in the seeded source are still recognised.
    assert functions >= _SURVIVING_FUNCTIONS
    assert assignments >= _SURVIVING_ASSIGNMENTS


def test_flag_token_absent_from_every_site() -> None:
    """The retired ``pydantic_settings_enabled`` token is gone from every site."""
    sources: dict[str, str] = {}
    for rel in _FLAG_SITES:
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        # Vacuous-pass guard: an unreadable/empty file would pass hollowly.
        assert text, f"Expected a non-empty {rel}"
        sources[rel] = text
    offenders = _sites_with_token(_FLAG_TOKEN, sources)
    assert offenders == [], (
        f"Proposal 106 retired the {_FLAG_TOKEN} cut-over flag; the token "
        f"reappeared in {offenders}. Remove it — the pydantic parser is "
        "unconditional, so the gate is gone."
    )


def test_token_scan_flags_a_seeded_reference() -> None:
    """Negative control: the token scan reports a site that still references the
    flag and never a clean one.

    Without this, a broken scan (matching nothing) would make the clean-tree
    assertion above pass vacuously. Hands in-memory sources to the pure helper, so
    it touches no filesystem.
    """
    seeded = {
        "maestro/config.py": "pydantic_settings_enabled: bool = False\n",
        "maestro.toml": '[github]\nrepo = ""\n',
    }
    flagged = _sites_with_token(_FLAG_TOKEN, seeded)
    assert flagged == ["maestro/config.py"]

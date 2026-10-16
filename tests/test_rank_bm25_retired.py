"""Regression guard: the ``rank_bm25`` dependency stays retired (proposal 106).

Proposal 106's Phase B Step 4 cut journal / evolution-plan search over to
SQLite FTS5's native ``bm25()`` as the sole ranker and dropped the third-party
``rank_bm25`` dependency. This deterministic, zero-LLM-cost source scan — the
same ``pathlib``-over-``maestro/`` guard family as
``tests/test_removed_dead_pr_fact_functions.py`` — pins that no shipping module
reintroduces a reference to ``rank_bm25`` (or its ``BM25Okapi`` class), and that
the dependency stays out of ``pyproject.toml``, so a future edit cannot silently
resurrect it. It reads the real source files, so it touches no ``mem_fs``,
network, or subprocess.

The module scan is scoped to ``maestro/`` deliberately: the fault-seeding
fixtures under ``tests/`` historically carried ``rank_bm25`` / ``BM25Okapi``
example strings, which are not shipping imports and must not trip this guard.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAESTRO_ROOT = _REPO_ROOT / "maestro"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: The retired dependency and its public class. Each pattern matches a reference
#: to the removed symbol; the ``\b`` boundaries keep a longer name that merely
#: starts with one of these (``rank_bm25_v2`` / ``BM25OkapiPlus``) from
#: false-matching.
_RETIRED_SYMBOLS = (
    re.compile(r"\brank_bm25\b"),
    re.compile(r"\bBM25Okapi\b"),
)


def _iter_py_files() -> list[Path]:
    return [p for p in _MAESTRO_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _scan_for_retired_symbols(sources: dict[str, str]) -> dict[str, list[str]]:
    """Return ``{relpath: [matched pattern, ...]}`` for reintroduced references.

    Pure over its *sources* mapping (relative path -> file text), so the real-file
    scan and the seeded negative control below share one definition of
    "reintroduced". A source matching none contributes no entry.
    """
    offenders: dict[str, list[str]] = {}
    for rel, source in sources.items():
        hits = [pattern.pattern for pattern in _RETIRED_SYMBOLS if pattern.search(source)]
        if hits:
            offenders[rel] = hits
    return offenders


def test_rank_bm25_symbols_stay_absent_from_maestro() -> None:
    """No module under ``maestro/`` references ``rank_bm25`` or ``BM25Okapi``."""
    sources = {
        p.relative_to(_MAESTRO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in _iter_py_files()
    }
    # Vacuous-pass guard: an empty scan would let the absence assertion pass
    # without validating anything (mirrors the sibling golden tests).
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    offenders = _scan_for_retired_symbols(sources)
    assert offenders == {}, (
        "Proposal 106 retired the rank_bm25 dependency; a shipping module "
        f"reintroduced a reference: {offenders}. Ranking runs on SQLite FTS5's "
        "native bm25() via maestro.search.bm25_rank — delete the reference."
    )


def test_rank_bm25_absent_from_pyproject() -> None:
    """``rank_bm25`` is no longer a declared project dependency."""
    text = _PYPROJECT.read_text(encoding="utf-8")
    # Vacuous-pass guard: an unreadable/empty pyproject would pass hollowly.
    assert text, f"Expected a non-empty pyproject at {_PYPROJECT}"
    assert "rank_bm25" not in text, (
        "Proposal 106 dropped rank_bm25 from [project] dependencies; it "
        "reappeared in pyproject.toml. Remove it — FTS5 is the sole ranker."
    )


def test_scan_flags_a_seeded_reference() -> None:
    """Negative control: a seeded rank_bm25 / BM25Okapi reference is flagged.

    Without this, a broken pattern (matching nothing) would make the clean-tree
    assertion above pass vacuously. Hands in-memory sources to the pure scan, so
    it touches no filesystem — and exercises the import line (both patterns), an
    attribute reference (one pattern), and a clean source that must not match.
    """
    seeded = {
        "search.py": "from rank_bm25 import BM25Okapi\n",
        "diagnostic.py": "bm25 = BM25Okapi(corpus)\n",
        "clean.py": "def rank(corpus, query):\n    return []\n",
    }
    offenders = _scan_for_retired_symbols(seeded)
    assert offenders == {
        "search.py": [r"\brank_bm25\b", r"\bBM25Okapi\b"],
        "diagnostic.py": [r"\bBM25Okapi\b"],
    }

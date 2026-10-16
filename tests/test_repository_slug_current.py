"""Regression: the checked-in tree names the repository's public namespace.

The public repository lives at ``thomas-sodon/maestro-agent``. The project was
developed under a private organization namespace, and a reintroduced
private-namespace literal would point ``[github] repo`` (or a doc's clone URL)
at a repository readers of this tree cannot reach.

Banning the private namespace deterministically, at zero LLM cost, is the same
checked-in-literal guard family as ``tests/test_no_line_citations_in_docs.py``:
a text scan of the committed tree that reds the build. The companion positive
assertions pin the live production values, so this file guards both directions
— the private namespace is absent *and* the public slug is what the runtime
actually loads.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from maestro.config_settings import build_config_via_pydantic

#: Project root — the parent of ``tests/``, and the ``config_dir`` argument
#: ``build_config_via_pydantic`` resolves ``maestro.toml`` and the
#: ``maestro.{env}.toml`` overlay under (mirrors ``test_config_flag_lifecycle``).
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: The private development namespace, spelled in pieces so this guard does not
#: carry the banned literal itself. Any checked-in occurrence is drift.
_STALE_NAMESPACE = "-".join(("Toes", "In", "The", "Sand"))

#: The public namespace: the repository owner and the namespace the publication
#: pipeline creates published repos under (``[publication] owner``).
_OWNER = "thomas-sodon"

#: The public slug every reference must name instead.
_CURRENT_SLUG = f"{_OWNER}/maestro-agent"

#: Matches the private namespace case-insensitively, in any form (a slug, a
#: clone URL, a merge-commit subject).
_STALE_RE = re.compile(re.escape(_STALE_NAMESPACE), re.IGNORECASE)

#: This file spells the banned namespace only in its detector controls, so the
#: walk skips itself rather than reporting the guard as its own first offender.
_SELF = Path(__file__).resolve()

#: Text extensions the slug can hide in: config overlays (``.toml``), operator
#: docs and proposals (``.md``), workflows (``.yml``/``.yaml``), sources and
#: fixtures (``.py``), scripts (``.sh``), dashboard assets (``.js``/``.html``),
#: and deploy config (``.json``/``.conf``). ``.jsonl`` is deliberately absent:
#: the append-only runtime ledgers record PR URLs that were true when written,
#: and rewriting history is out of scope.
_TEXT_SUFFIXES = frozenset(
    {".py", ".md", ".toml", ".yml", ".yaml", ".sh", ".json", ".js", ".html", ".conf"}
)

#: Extension-less text files that still carry deploy-time references.
_TEXT_FILENAMES = frozenset({"Dockerfile"})

#: Directory names pruned from the walk. The walk reads the *working tree*, not
#: ``git ls-files`` (enumerating the index would mean a real subprocess, which
#: unit tests may not run), so the prune list is what keeps "working tree" and
#: "checked-in tree" the same set of files.
#:
#: First group — derived bytes and runtime state that are not source: ``.git``
#: and the tool caches. ``artifacts`` and ``data`` hold gitignored runtime
#: state — the loop runs this very suite inside the production container, where
#: ``data/`` carries recorded PR URLs that are *correct* history and
#: must not red the gate. ``worktrees`` prunes ``.claude/worktrees/``, whose
#: nested checkouts of this repo may sit on older branches.
#:
#: Second group — the gitignored dependency and build trees ``.gitignore``
#: already anticipates (``.venv``/``venv``/``env``, ``node_modules``, ``dist``,
#: ``build``, ``.eggs``, ``htmlcov``). None exist in a clean checkout, but a
#: developer's root-level virtualenv would otherwise make this walk
#: ``read_text()`` every ``.py``/``.json``/``.html`` under ``site-packages`` —
#: risking the 5s unit-test timeout and reporting dependency metadata as
#: offenders. Pruning them is what keeps the guard bound to *this* project's
#: sources. ``*.egg-info`` is pruned by suffix in ``_scanned_files``.
_SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "artifacts",
        "data",
        "worktrees",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "dist",
        "build",
        ".eggs",
        "htmlcov",
    }
)

#: Gitignored derived files that survive the directory prune because they sit
#: at the repo root. ``coverage.json`` is a ~1 MB machine-generated report the
#: coverage gate rewrites on every run; reading it each time buys nothing.
_SKIP_FILENAMES = frozenset({"coverage.json"})


def _is_text_candidate(path: Path) -> bool:
    """Return whether ``path`` is a text file this guard should read."""
    if path.name in _SKIP_FILENAMES:
        return False
    return path.suffix in _TEXT_SUFFIXES or path.name in _TEXT_FILENAMES


def _find_stale_slug(text: str) -> list[str]:
    """Return every occurrence of the private namespace in ``text``."""
    return _STALE_RE.findall(text)


def _scanned_files() -> list[Path]:
    """Return every scannable text file under the repo root, self excluded."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        # Prune in place so the walk never descends into skipped trees.
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.endswith(".egg-info")
        ]
        for filename in filenames:
            path = Path(dirpath) / filename
            if _is_text_candidate(path) and path.resolve() != _SELF:
                found.append(path)
    return sorted(found)


def test_no_stale_repository_slug_in_tree() -> None:
    """No checked-in text file may name the private development namespace."""
    scanned = _scanned_files()
    relative = {str(path.relative_to(_REPO_ROOT)) for path in scanned}
    # Vacuous-pass guard: a broken walk (wrong root, an over-broad skip entry)
    # yields nothing and would let the offender assertion below pass without
    # reading a single file. Naming two files the scan *must* reach — the live
    # config overlay and the operator doc, the two highest-consequence hosts of
    # the slug — catches a prune that silently swallowed the tree, which a bare
    # non-empty check would not.
    assert "maestro.production.toml" in relative, (
        "The tree scan did not reach maestro.production.toml, the overlay whose "
        f"[github] repo points the loop at its own repository (scanned "
        f"{len(scanned)} files); the walk root or _SKIP_DIRS is wrong, so this "
        "guard would pass without validating the load-bearing file."
    )
    assert os.path.join("docs", "vps-setup.md") in relative, (
        "The tree scan did not reach docs/vps-setup.md, which carries the "
        f"operator clone and runner-registration URLs (scanned {len(scanned)} "
        "files); the walk root or _SKIP_DIRS is wrong, so this guard would pass "
        "without validating the operator documentation."
    )

    offenders: list[str] = []
    for path in scanned:
        if _find_stale_slug(path.read_text(encoding="utf-8", errors="replace")):
            offenders.append(str(path.relative_to(_REPO_ROOT)))

    assert not offenders, (
        "These files name the private development namespace: "
        + ", ".join(offenders)
        + f". The public repository lives at {_CURRENT_SLUG!r}."
    )


def test_detector_fires_on_seeded_stale_slug() -> None:
    """Negative control: the detector must catch a seeded private slug.

    Without this, a detector regression (a broken pattern matching nothing)
    would make the clean-tree assertion above pass vacuously.
    """
    seeded = f"see https://github.com/{_STALE_NAMESPACE}/maestro/issues/1"
    assert _find_stale_slug(seeded) == [_STALE_NAMESPACE]


def test_detector_is_case_insensitive() -> None:
    """A lower-cased clone URL is still the private namespace."""
    seeded = f"git clone https://github.com/{_STALE_NAMESPACE.lower()}/maestro.git"
    assert len(_find_stale_slug(seeded)) == 1


def test_detector_ignores_public_slug() -> None:
    """The public owner and slug stay allowed."""
    assert _find_stale_slug('reviewer = "thomas-sodon"') == []
    assert _find_stale_slug("https://github.com/thomas-sodon/maestro-agent") == []


def test_scan_skips_gitignored_derived_files() -> None:
    """The walk excludes the root coverage report and dependency trees."""
    assert not _is_text_candidate(_REPO_ROOT / "coverage.json")
    assert _is_text_candidate(_REPO_ROOT / "maestro.production.toml")
    # Every pruned name is a directory ``.gitignore`` already excludes, so no
    # scanned file may live under one. Parts are taken relative to the repo
    # root: the checkout's own ancestors are not part of the tree, and a
    # checkout under ``.claude/worktrees/`` would otherwise trip ``worktrees``.
    scanned_parts = {
        part
        for path in _scanned_files()
        for part in path.relative_to(_REPO_ROOT).parts
    }
    assert not (scanned_parts & _SKIP_DIRS)


def test_production_overlay_targets_current_repo() -> None:
    """The production overlay must resolve to the current repository slug.

    Absence of the dead slug is only half the invariant: a typo'd or emptied
    ``[github] repo`` also contains no banned literal. Parsing the committed
    ``maestro.toml`` + ``maestro.production.toml`` through the same
    ``build_config_via_pydantic`` load path the runtime uses pins the live value
    the loop actually points its PR traffic at.
    """
    config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    assert config.github_repo == _CURRENT_SLUG, (
        "maestro.production.toml's [github] repo resolves to "
        f"{config.github_repo!r}, but the repository lives at {_CURRENT_SLUG!r}; "
        "the loop would point its PR traffic at the wrong (or no) repository."
    )


def test_publication_owner_targets_public_namespace() -> None:
    """Publications are created under the configured public namespace.

    ``[publication] owner`` decides this: when it is empty,
    :meth:`SubprocessCodeHost.resolve_owner` falls back to ``gh api user``.
    Pinning the resolved value through the real load path keeps an overlay edit
    from quietly moving publications to another namespace.
    """
    config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    assert config.publication_owner == _OWNER, (
        f"[publication] owner resolves to {config.publication_owner!r}, but "
        f"publications belong to the {_OWNER!r} namespace."
    )

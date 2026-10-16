"""Every relative ``.md`` link in the tracked docs resolves to a file on disk.

Deleting a doc (as the revenue-lineage retirement did) leaves links into it
dangling; this guard walks every ``*.md`` under the repo root (skipping VCS,
tool-cache, virtualenv and runtime-output trees) read-only and fails on any relative ``](path.md)`` /
``](path.md#anchor)`` link whose target is missing.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: A markdown link target ending in ``.md``, optionally with an anchor.
_MD_LINK = re.compile(r"\]\(([^)\s]+?\.md)(?:#[^)\s]*)?\)")

#: Fewer parsed links than this means the extractor or the walk broke.
_MIN_LINKS = 50


def extract_md_links(text: str) -> list[str]:
    """Return every relative ``.md`` link target in *text* (no URLs)."""
    return [
        target
        for target in _MD_LINK.findall(text)
        if not target.startswith(("http://", "https://", "mailto:"))
    ]


#: Directory names never walked: VCS, tool caches, virtualenvs, build output
#: and runtime state (the gitignored trees a local checkout may hold).
_SKIP_DIRS = frozenset({
    ".claude", ".git", "node_modules", ".venv", "venv", "env", ".tox", ".eggs",
    "htmlcov", ".taskmaster", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "artifacts", "data", "logs", "build", "dist",
})


def _is_skipped(rel: Path) -> bool:
    return bool(_SKIP_DIRS.intersection(rel.parts))


def _doc_files() -> list[Path]:
    return sorted(
        p for p in _REPO_ROOT.rglob("*.md")
        if not _is_skipped(p.relative_to(_REPO_ROOT))
    )


def _dangling(doc: Path, text: str) -> list[str]:
    return [t for t in extract_md_links(text) if not (doc.parent / t).resolve().is_file()]


def test_every_relative_md_link_resolves() -> None:
    """No doc links to a missing ``.md`` file."""
    parsed = 0
    broken: list[str] = []
    for doc in _doc_files():
        text = doc.read_text(encoding="utf-8")
        parsed += len(extract_md_links(text))
        rel = doc.relative_to(_REPO_ROOT)
        broken.extend(f"{rel}: {t}" for t in _dangling(doc, text))
    assert parsed >= _MIN_LINKS
    assert broken == []


def test_extractor_skips_urls_and_keeps_anchored_links() -> None:
    """URLs are skipped; an anchored relative link yields its path."""
    text = "[a](https://x.example/a.md) [b](docs/b.md#sec) [c](c.md)"
    assert extract_md_links(text) == ["docs/b.md", "c.md"]


def test_dangling_link_is_reported() -> None:
    """Negative control: an in-memory link to a missing file is flagged."""
    doc = _REPO_ROOT / "README.md"
    assert _dangling(doc, "[gone](docs/no-such-doc-xyz.md)") == ["docs/no-such-doc-xyz.md"]


def test_gitignored_local_trees_are_skipped() -> None:
    """A local virtualenv or tool cache is never walked."""
    for rel in ("venv/lib/x/README.md", "env/a.md", ".tox/py/b.md", ".eggs/c.md", "htmlcov/d.md"):
        assert _is_skipped(Path(rel)) is True
    assert _is_skipped(Path("docs/cli.md")) is False

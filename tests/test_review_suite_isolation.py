"""Regression: review-package tests must not import the main ``maestro`` package.

The merged ``package-quality`` CI job (`.github/workflows/quality-gates.yml`)
installs ``maestro_review[dev]`` **and** ``maestro_sdk[dev]`` — but still not the
main ``maestro`` package, since neither sibling depends on it — and runs
``pytest tests/test_maestro_review`` (alongside the ``maestro_sdk`` gates). The
main ``maestro`` package — and its runtime dependencies (``fastapi``,
``python-telegram-bot``, ``jinja2``, …) — are absent there. A test under
``tests/test_maestro_review/`` that does ``import maestro`` / ``from maestro …``
raises ``ModuleNotFoundError`` at collection time, failing the whole file in that
job (PR #2546). Such a test belongs in the main ``tests/`` suite (the ``quality``
job), alongside the other command-doc guards, where ``maestro`` is installed.

This guard parses every module under ``tests/test_maestro_review/`` and asserts
none imports a top-level module named exactly ``maestro`` (``maestro_review`` is
the package those tests legitimately exercise).
"""

from __future__ import annotations

import ast
from pathlib import Path

_REVIEW_TESTS_DIR = Path(__file__).resolve().parent / "test_maestro_review"


def _imported_roots(tree: ast.AST) -> set[str]:
    """Top-level module names imported by *tree* (absolute imports only)."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        # Relative imports (``level > 0``) have no top-level module name and
        # stay within the package, so they cannot reach ``maestro``.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_review_tests_do_not_import_main_maestro_package() -> None:
    offenders: list[str] = []
    for path in sorted(_REVIEW_TESTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if "maestro" in _imported_roots(tree):
            offenders.append(path.name)

    assert not offenders, (
        "Tests under tests/test_maestro_review/ must not import the main "
        "'maestro' package — the package-quality CI job installs "
        "maestro_review[dev] and maestro_sdk[dev] but not maestro, so the import "
        "raises ModuleNotFoundError at collection time. Move these to tests/ "
        f"instead: {offenders}"
    )

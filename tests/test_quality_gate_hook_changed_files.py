"""Regression: changed-package detection must cover the index (issue #168).

The Stop hook (``.claude/quality-gate-hook.sh``) decides which package gates to
run by delegating to ``scripts/quality-gates.sh changed``. That script's
``detect_changed`` builds the changed-file set from three sources:
``git diff --name-only HEAD`` (working-tree vs ``HEAD``),
``git diff --name-only --cached`` (index vs ``HEAD``), and
``git ls-files --others --exclude-standard`` (untracked). The ``--cached`` index
source is the one whose absence was the #168 false negative: a file that was
``git add``-ed and then had its working-tree copy restored to ``HEAD`` content
lives only in the index, so ``git diff HEAD`` sees nothing for it and the gates
for that package are silently skipped.

The detection logic used to live inline in the hook; it now lives in
``scripts/quality-gates.sh`` — the single source of truth shared with the
``/quality-gates`` and ``/merge-main`` slash commands. These tests pin the three
detection sources *and* the scope-token table in that script so a future edit
cannot silently drop a detection source or a gate group.

Scope note: these are deliberately *static-text* pins, not behavioral tests. The
script's runtime behavior — scope dedup, the no-source-changes ``exit 0`` no-op,
the return-code-to-``FAIL(<stage>)`` mapping, the unknown-scope fallthrough, and
the ``^maestro/``-vs-``maestro_review/`` boundary in ``detect_changed`` — is
intentionally left unguarded here, because exercising it would require running
the shell script as a real subprocess against a real git tree, which the unit
suite forbids (see "Testing Principles" in ``CLAUDE.md``). The pins catch the
silent-drop regression class; behavioral coverage is left to manual review.
"""

from __future__ import annotations

from pathlib import Path

_SCRIPT_FILE = (
    Path(__file__).resolve().parent.parent / "scripts" / "quality-gates.sh"
)

# All three sources ``detect_changed`` must combine. ``--cached`` is the index
# source whose absence was the #168 bug; the other two guard against a future
# edit silently dropping working-tree or untracked coverage.
_REQUIRED_SOURCES = (
    "git diff --name-only HEAD",
    "git diff --name-only --cached",
    "git ls-files --others --exclude-standard",
)

# Every scope token the script documents and routes via its ``case`` table.
# Pinned so a future edit that drops a branch (silently disabling a gate group)
# fails the test. Each appears as ``<scope>)`` in the ``case "$s" in`` block.
_REQUIRED_SCOPES = (
    "all",
    "unit",
    "maestro",
    "review",
    "sdk",
    "browser",
    "changed",
)


def test_changed_file_detection_covers_the_index() -> None:
    """``detect_changed`` must diff the index against ``HEAD`` with ``--cached``.

    Without this source a purely-staged change (staged, then worktree restored
    to ``HEAD``) is invisible to ``git diff HEAD`` and the gates are skipped.
    """
    content = _SCRIPT_FILE.read_text(encoding="utf-8")
    assert "git diff --name-only --cached" in content, (
        "scripts/quality-gates.sh changed-file detection is missing the index "
        "source 'git diff --name-only --cached'. Purely-staged changes (#168) "
        "would be invisible to 'git diff HEAD' and silently skip the gates."
    )


def test_changed_file_detection_combines_all_three_sources() -> None:
    """Working-tree, index, and untracked sources must all be present.

    Pinned together so that silently dropping any one detection source — not
    just the index one — fails the test.
    """
    content = _SCRIPT_FILE.read_text(encoding="utf-8")
    for source in _REQUIRED_SOURCES:
        assert source in content, (
            f"scripts/quality-gates.sh changed-file detection is missing the "
            f"source {source!r}. All three of {_REQUIRED_SOURCES!r} must "
            f"combine so no class of change (working-tree, staged, untracked) "
            f"is silently dropped."
        )


def test_scope_tokens_all_routed() -> None:
    """Every documented scope token must have a ``case`` branch in the script.

    The script is the single source of truth for the quality-gate groups; a
    dropped ``case`` branch would silently route a scope to the ``*)`` catch-all
    (treated as ``all``) or disable a group entirely. Pinning each token guards
    against that drift.
    """
    content = _SCRIPT_FILE.read_text(encoding="utf-8")
    for scope in _REQUIRED_SCOPES:
        assert f"{scope})" in content, (
            f"scripts/quality-gates.sh is missing a 'case' branch for the "
            f"scope token {scope!r} (expected '{scope})'). All of "
            f"{_REQUIRED_SCOPES!r} must be routed so no gate group is silently "
            f"dropped."
        )

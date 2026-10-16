"""Which diff paths the uncalled-symbol scan considers (#4382).

Shared by both copies of the uncalled-symbol check —
``loop.implementation._check_uncalled_symbol_gate`` and
``EvolutionEngine._find_uncalled_symbols`` — plus
``evolution._extract_added_source``, so the symbol exemption and the
call-site grep scope cannot drift apart.
"""

from __future__ import annotations

#: Grep pathspecs excluding test code from the call-site search: the root
#: ``tests/`` tree, any nested ``tests`` tree (e.g. ``maestro_sdk/tests/``), and
#: ``test_*.py`` / ``conftest.py`` anywhere — matching
#: :func:`counts_for_uncalled_scan`.
UNCALLED_GREP_EXCLUDES: tuple[str, ...] = (
    ":!tests/",
    ":!**/tests/**",
    ":(exclude,glob)**/test_*.py",
    ":(exclude,glob)**/conftest.py",
)


def counts_for_uncalled_scan(path: str) -> bool:
    """Return ``True`` when diff *path* contributes symbols to the scan.

    Only non-test ``.py`` files count.  Test code is any path with a ``tests``
    component or a ``test_*.py`` / ``conftest.py`` basename — test functions
    and fixtures are not expected to have production call sites.  Non-Python
    files (e.g. a ``def`` in a Markdown code sample) are never scanned.
    """
    if not path.endswith(".py"):
        return False
    *dirs, basename = path.split("/")
    if "tests" in dirs:
        return False
    return not (basename.startswith("test_") or basename == "conftest.py")

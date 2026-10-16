"""Tests for _find_uncalled_symbols and its integration into self_review.

Focused test file for the uncalled-symbol detection feature added to
EvolutionEngine.  Covers:
- Diff parsing (def, class, async def, multi-file)
- Symbol filtering (dunders, _test helpers, test-file definitions)
- Subprocess interaction (grep hits, misses, errors)
- self_review prompt injection (warning block, checklist item, coexistence
  with acceptance criteria, session vs. fresh-context paths)
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro import evolution_quality
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine, _symbol_has_call_site
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory
from maestro.notifications.base import NullNotifier
from maestro.uncalled_scope import UNCALLED_GREP_EXCLUDES, counts_for_uncalled_scan
from tests.conftest import MockQueryResult

_DIR = Path("/fake/uncalled")


def _engine(mem_fs: dict[str, str]) -> EvolutionEngine:
    m = Memory(_DIR)
    c = Consciousness(_DIR)
    c.load_self_model()
    j = Journal(_DIR)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    return EvolutionEngine(m, c, j, n, issues=issues, config=Config())


def _plan(**overrides: object) -> EvolutionPlan:
    defaults: dict[str, object] = dict(
        plan_id="p1", title="t",
        category="capability", rationale="r", proposed_changes=["x"],
        files_affected=["f.py"], expected_impact="t",
        status="approved", created_at="2025-01-01T00:00:00Z",
    )
    defaults.update(overrides)
    return EvolutionPlan(**defaults)  # type: ignore[arg-type]


# =====================================================================
# _find_uncalled_symbols — diff parsing & subprocess
# =====================================================================

_DIFF_CALLED = """\
diff --git a/maestro/helpers.py b/maestro/helpers.py
--- a/maestro/helpers.py
+++ b/maestro/helpers.py
@@ -1,0 +2,3 @@
+def compute_score(data):
+    return sum(data)
"""

_DIFF_UNCALLED = """\
diff --git a/maestro/orphan.py b/maestro/orphan.py
--- a/maestro/orphan.py
+++ b/maestro/orphan.py
@@ -0,0 +1,4 @@
+def lonely_func():
+    pass
+class LonelyClass:
+    pass
"""

_DIFF_MULTI_FILE = """\
diff --git a/maestro/alpha.py b/maestro/alpha.py
--- a/maestro/alpha.py
+++ b/maestro/alpha.py
@@ -0,0 +1,2 @@
+def alpha_func():
+    pass
diff --git a/maestro/beta.py b/maestro/beta.py
--- a/maestro/beta.py
+++ b/maestro/beta.py
@@ -0,0 +1,2 @@
+class BetaClass:
+    pass
"""

_DIFF_PRIVATE_NOT_TEST = """\
diff --git a/maestro/internal.py b/maestro/internal.py
--- a/maestro/internal.py
+++ b/maestro/internal.py
@@ -0,0 +1,2 @@
+def _private_helper():
+    pass
"""

_DIFF_DUNDERS = """\
diff --git a/maestro/model.py b/maestro/model.py
--- a/maestro/model.py
+++ b/maestro/model.py
@@ -0,0 +1,6 @@
+    def __init__(self):
+        pass
+    def __eq__(self, other):
+        return True
+    def __hash__(self):
+        return 0
"""

_DIFF_TEST_HELPERS = """\
diff --git a/maestro/utils.py b/maestro/utils.py
--- a/maestro/utils.py
+++ b/maestro/utils.py
@@ -0,0 +1,4 @@
+def _test_make_fixture():
+    pass
+def _test_helper():
+    pass
"""

_DIFF_TEST_FILE = """\
diff --git a/tests/test_foo.py b/tests/test_foo.py
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -0,0 +1,4 @@
+def test_something():
+    assert True
+def helper_for_tests():
+    pass
"""

_DIFF_CALL_ONLY_IN_TESTS = """\
diff --git a/maestro/new_util.py b/maestro/new_util.py
--- a/maestro/new_util.py
+++ b/maestro/new_util.py
@@ -0,0 +1,2 @@
+def test_only_caller():
+    pass
"""


def _no_matches(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    """Simulate git grep finding nothing."""
    return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")


def _has_matches(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    """Simulate ``git grep -n`` finding an external call site.

    The row lives in a different file from the symbol's definition, so it
    counts as a real call site under the definition-line filter.
    """
    return subprocess.CompletedProcess(
        cmd, returncode=0, stdout="maestro/agent.py:42:    compute_score(x)\n",
        stderr="",
    )


# --- (1) symbol IS called → no warning ---

def test_called_symbol_produces_no_warning(mem_fs: dict[str, str]) -> None:
    """A new function with grep hits must not generate a warning."""
    e = _engine(mem_fs)
    with patch("subprocess.run", side_effect=_has_matches):
        result = e._find_uncalled_symbols(_DIFF_CALLED)
    assert result == []


# --- (2) symbol with zero call sites → warning ---

def test_uncalled_symbol_produces_warning(mem_fs: dict[str, str]) -> None:
    """A new function with zero grep hits must generate a warning."""
    e = _engine(mem_fs)
    with patch("subprocess.run", side_effect=_no_matches):
        result = e._find_uncalled_symbols(_DIFF_UNCALLED)
    assert len(result) == 2
    assert any("lonely_func" in w for w in result)
    assert any("LonelyClass" in w for w in result)
    for w in result:
        assert "maestro/orphan.py" in w


def test_warning_message_format(mem_fs: dict[str, str]) -> None:
    """Warning string must contain the file path and symbol name."""
    e = _engine(mem_fs)
    with patch("subprocess.run", side_effect=_no_matches):
        result = e._find_uncalled_symbols(_DIFF_CALLED)
    assert len(result) == 1
    w = result[0]
    assert "maestro/helpers.py" in w
    assert "compute_score" in w
    assert "no non-test call sites" in w
    assert "definition line" in w


# --- (3) dunders and test helpers excluded ---

def test_dunder_methods_always_excluded(mem_fs: dict[str, str]) -> None:
    """__init__, __eq__, __hash__ must never trigger grep or warnings."""
    e = _engine(mem_fs)
    calls: list[list[str]] = []

    def spy(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _no_matches(cmd, **kw)

    with patch("subprocess.run", side_effect=spy):
        result = e._find_uncalled_symbols(_DIFF_DUNDERS)

    assert result == []
    assert calls == []  # no subprocess invocations at all


def test_test_helpers_always_excluded(mem_fs: dict[str, str]) -> None:
    """Functions starting with _test must never trigger grep or warnings."""
    e = _engine(mem_fs)
    calls: list[list[str]] = []

    def spy(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _no_matches(cmd, **kw)

    with patch("subprocess.run", side_effect=spy):
        result = e._find_uncalled_symbols(_DIFF_TEST_HELPERS)

    assert result == []
    assert calls == []


def test_private_non_test_helper_is_checked(mem_fs: dict[str, str]) -> None:
    """A private method starting with _ (but not _test) must be checked."""
    e = _engine(mem_fs)
    with patch("subprocess.run", side_effect=_no_matches):
        result = e._find_uncalled_symbols(_DIFF_PRIVATE_NOT_TEST)
    assert len(result) == 1
    assert "_private_helper" in result[0]


# --- Symbols defined in test files are skipped ---

def test_symbols_in_test_files_skipped(mem_fs: dict[str, str]) -> None:
    """Symbols defined in tests/ files must not be checked or warned about,
    since test functions and fixtures are not expected to have production
    call sites."""
    e = _engine(mem_fs)
    calls: list[list[str]] = []

    def spy(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _no_matches(cmd, **kw)

    with patch("subprocess.run", side_effect=spy):
        result = e._find_uncalled_symbols(_DIFF_TEST_FILE)

    assert result == []
    assert calls == []  # no grep calls — all symbols skipped at parse


# --- Symbols with call sites only in tests/ are flagged ---

def test_symbol_called_only_in_tests_is_flagged(mem_fs: dict[str, str]) -> None:
    """A production symbol whose only call sites are in tests/ must be
    flagged, because git grep excludes tests/."""
    e = _engine(mem_fs)
    # git grep with :!tests/ will find nothing → returncode 1
    with patch("subprocess.run", side_effect=_no_matches):
        result = e._find_uncalled_symbols(_DIFF_CALL_ONLY_IN_TESTS)
    assert len(result) == 1
    assert "test_only_caller" in result[0]
    assert "maestro/new_util.py" in result[0]


# --- Multi-file diff ---

def test_multi_file_diff_attributes_symbols_correctly(
    mem_fs: dict[str, str],
) -> None:
    """Symbols from different files must each reference their own file."""
    e = _engine(mem_fs)
    captured: list[list[str]] = []

    def spy(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        captured.append(list(cmd))
        return _no_matches(cmd, **kw)

    with patch("subprocess.run", side_effect=spy):
        result = e._find_uncalled_symbols(_DIFF_MULTI_FILE)

    assert len(result) == 2
    # First symbol from alpha.py, second from beta.py
    alpha_warn = [w for w in result if "alpha_func" in w]
    beta_warn = [w for w in result if "BetaClass" in w]
    assert len(alpha_warn) == 1
    assert "maestro/alpha.py" in alpha_warn[0]
    assert len(beta_warn) == 1
    assert "maestro/beta.py" in beta_warn[0]

    # grep now searches the whole tree (minus tests/) with -n and filters the
    # definition line in-process — it no longer excludes the defining file.
    for cmd in captured:
        assert "-n" in cmd
        assert ":!tests/" in cmd
        assert not any(
            isinstance(part, str) and part.startswith(":!maestro/")
            for part in cmd
        )


# --- Error resilience ---

def test_grep_timeout_skips_symbol(mem_fs: dict[str, str]) -> None:
    """TimeoutExpired must not crash — symbol is silently skipped."""
    e = _engine(mem_fs)

    def timeout(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout=30)

    with patch("subprocess.run", side_effect=timeout):
        result = e._find_uncalled_symbols(_DIFF_UNCALLED)
    assert result == []


def test_grep_oserror_skips_symbol(mem_fs: dict[str, str]) -> None:
    """OSError must not crash — symbol is silently skipped."""
    e = _engine(mem_fs)

    def oserror(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise OSError("git not found")

    with patch("subprocess.run", side_effect=oserror):
        result = e._find_uncalled_symbols(_DIFF_UNCALLED)
    assert result == []


# --- Empty / edge-case diffs ---

def test_empty_diff_no_warnings(mem_fs: dict[str, str]) -> None:
    """An empty string must produce no warnings and no subprocess calls."""
    e = _engine(mem_fs)
    with patch("subprocess.run") as mock:
        result = e._find_uncalled_symbols("")
    mock.assert_not_called()
    assert result == []


def test_diff_with_only_context_lines(mem_fs: dict[str, str]) -> None:
    """Lines without + prefix (context lines) must not be picked up."""
    diff = """\
diff --git a/maestro/m.py b/maestro/m.py
--- a/maestro/m.py
+++ b/maestro/m.py
@@ -1,3 +1,3 @@
 def existing_func():
-    old_body
+    new_body
"""
    e = _engine(mem_fs)
    with patch("subprocess.run") as mock:
        result = e._find_uncalled_symbols(diff)
    # "def existing_func" has no + prefix — not a new addition
    mock.assert_not_called()
    assert result == []


def test_renamed_symbol_detected(mem_fs: dict[str, str]) -> None:
    """A symbol that was renamed (old removed, new added) should be
    checked — the added line carries the new name."""
    diff = """\
diff --git a/maestro/refactored.py b/maestro/refactored.py
--- a/maestro/refactored.py
+++ b/maestro/refactored.py
@@ -1,2 +1,2 @@
-def old_name():
+def new_name():
     pass
"""
    e = _engine(mem_fs)
    with patch("subprocess.run", side_effect=_no_matches):
        result = e._find_uncalled_symbols(diff)
    assert len(result) == 1
    assert "new_name" in result[0]
    assert "maestro/refactored.py" in result[0]


# =====================================================================
# Definition-line filter — new git grep -n parsing (#2410)
# =====================================================================

_DIFF_INTRA_HELPER = """\
diff --git a/maestro/feature.py b/maestro/feature.py
--- a/maestro/feature.py
+++ b/maestro/feature.py
@@ -0,0 +1,2 @@
+def _intra_helper():
+    return 1
"""

_DIFF_DEAD_ORGAN = """\
diff --git a/maestro/dead.py b/maestro/dead.py
--- a/maestro/dead.py
+++ b/maestro/dead.py
@@ -0,0 +1,2 @@
+def truly_dead():
+    pass
"""

_DIFF_DUPLICATE_NAME = """\
diff --git a/maestro/a.py b/maestro/a.py
--- a/maestro/a.py
+++ b/maestro/a.py
@@ -0,0 +1,2 @@
+def duplicate():
+    pass
"""

_DIFF_ASYNC_DEAD = """\
diff --git a/maestro/asyncmod.py b/maestro/asyncmod.py
--- a/maestro/asyncmod.py
+++ b/maestro/asyncmod.py
@@ -0,0 +1,2 @@
+async def lonely_async():
+    pass
"""


def _grep_n(stdout: str) -> object:
    """Build a ``subprocess.run`` side effect returning canned ``git grep -n``
    stdout (rows of the form ``path:lineno:content``)."""

    def run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout=stdout, stderr="",
        )

    return run


def test_intra_module_call_site_passes(mem_fs: dict[str, str]) -> None:
    """Regression for #2410: a private helper whose only call site is inside
    its own module must pass — ``git grep -n`` returns the definition line plus
    an intra-module call, and only the definition line is discounted."""
    e = _engine(mem_fs)
    stdout = (
        "maestro/feature.py:1:def _intra_helper():\n"
        "maestro/feature.py:9:    return _intra_helper()\n"
    )
    with patch("subprocess.run", side_effect=_grep_n(stdout)):
        result = e._find_uncalled_symbols(_DIFF_INTRA_HELPER)
    assert result == []


def test_definition_line_only_is_flagged(mem_fs: dict[str, str]) -> None:
    """A symbol whose sole ``git grep -n`` hit is its own definition line (a
    genuinely dead organ) must still be flagged. A malformed grep row that is
    not ``path:lineno:content`` is ignored rather than counted as a call."""
    e = _engine(mem_fs)
    stdout = (
        "orphan_row_without_colons\n"  # malformed — skipped, not a call site
        "maestro/dead.py:1:def truly_dead():\n"
    )
    with patch("subprocess.run", side_effect=_grep_n(stdout)):
        result = e._find_uncalled_symbols(_DIFF_DEAD_ORGAN)
    assert len(result) == 1
    assert "truly_dead" in result[0]
    assert "maestro/dead.py" in result[0]


def test_async_def_definition_line_recognised(mem_fs: dict[str, str]) -> None:
    """An ``async def`` whose only hit is its own definition line must be
    flagged — the definition filter must recognise the async form rather than
    mistaking it for a call site."""
    e = _engine(mem_fs)
    stdout = "maestro/asyncmod.py:1:async def lonely_async():\n"
    with patch("subprocess.run", side_effect=_grep_n(stdout)):
        result = e._find_uncalled_symbols(_DIFF_ASYNC_DEAD)
    assert len(result) == 1
    assert "lonely_async" in result[0]


def test_class_definition_line_recognised(mem_fs: dict[str, str]) -> None:
    """A class whose only ``git grep -n`` hit is its own definition line must be
    flagged — the definition filter must recognise the ``class`` form rather than
    mistaking it for a call site."""
    _DIFF_DEAD_CLASS = """\
diff --git a/maestro/dead.py b/maestro/dead.py
--- a/maestro/dead.py
+++ b/maestro/dead.py
@@ -0,0 +1,2 @@
+class DeadClass:
+    pass
"""
    e = _engine(mem_fs)
    stdout = "maestro/dead.py:1:class DeadClass:\n"
    with patch("subprocess.run", side_effect=_grep_n(stdout)):
        result = e._find_uncalled_symbols(_DIFF_DEAD_CLASS)
    assert len(result) == 1
    assert "DeadClass" in result[0]
    assert "maestro/dead.py" in result[0]


def test_external_call_site_still_passes(mem_fs: dict[str, str]) -> None:
    """A call site in a different file from the definition still counts — the
    row is not a ``def``/``class`` declaration for the symbol, so the
    declaration filter does not apply and it is a real call site."""
    e = _engine(mem_fs)
    stdout = "maestro/other.py:5:    truly_dead()\n"
    with patch("subprocess.run", side_effect=_grep_n(stdout)):
        result = e._find_uncalled_symbols(_DIFF_DEAD_ORGAN)
    assert result == []


def test_duplicate_definition_in_other_file_is_not_a_call_site(
    mem_fs: dict[str, str],
) -> None:
    """Regression for #3797: a same-named ``def`` in *another* file is still a
    declaration, not a use.  Two dead ``duplicate()`` definitions must not
    vouch for each other — the symbol is reported as uncalled."""
    e = _engine(mem_fs)
    stdout = "maestro/a.py:1:def duplicate():\nmaestro/b.py:1:def duplicate():\n"
    with patch("subprocess.run", side_effect=_grep_n(stdout)):
        result = e._find_uncalled_symbols(_DIFF_DUPLICATE_NAME)
    assert len(result) == 1
    assert "duplicate" in result[0]
    assert "maestro/a.py" in result[0]


# =====================================================================
# self_review prompt integration
# =====================================================================

@pytest.mark.asyncio
async def test_self_review_prompt_includes_uncalled_block(
    mem_fs: dict[str, str],
) -> None:
    """(4) When _find_uncalled_symbols returns warnings, the prompt must
    contain the ## Uncalled Symbol Warnings block and the uncalled-symbols
    checklist item (item 5 here: flag off, no criteria, no hollow)."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = "diff --git a/x b/x\n+def foo(): pass"
    e._vcs = vcs

    warnings = ["maestro/x.py: symbol 'foo' has no external call sites"]
    with patch.object(e, "_find_uncalled_symbols", return_value=warnings), \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "## Uncalled Symbol Warnings" in prompt
    assert "symbol 'foo'" in prompt
    # Flag off, no criteria, no hollow: uncalled is the sole optional item,
    # so computed numbering places it at item 5 (right after Tests).
    assert "5. **Uncalled symbols**" in prompt
    assert "documented reason for being unused" in prompt


@pytest.mark.asyncio
async def test_self_review_prompt_omits_block_when_no_warnings(
    mem_fs: dict[str, str],
) -> None:
    """(5) When _find_uncalled_symbols returns [], neither the block nor
    the uncalled-symbols checklist item should appear."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = "diff --git a/x b/x\n"
    e._vcs = vcs

    with patch.object(e, "_find_uncalled_symbols", return_value=[]), \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "## Uncalled Symbol Warnings" not in prompt
    assert "**Uncalled symbols**" not in prompt


@pytest.mark.asyncio
async def test_self_review_uncalled_block_between_diff_and_instructions(
    mem_fs: dict[str, str],
) -> None:
    """Warning block must sit between the closing ``` and ## Review Instructions."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = "diff --git a/x b/x\n"
    e._vcs = vcs

    warnings = ["maestro/x.py: symbol 'orphan' has no external call sites"]
    with patch.object(e, "_find_uncalled_symbols", return_value=warnings), \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base")

    prompt = mock_q.call_args.kwargs["prompt"]
    diff_fence_end = prompt.index("```\n", prompt.index("```diff"))
    uncalled_pos = prompt.index("## Uncalled Symbol Warnings")
    review_pos = prompt.index("## Review Instructions")
    assert diff_fence_end < uncalled_pos < review_pos


@pytest.mark.asyncio
async def test_self_review_criteria_and_uncalled_coexist(
    mem_fs: dict[str, str],
) -> None:
    """When both acceptance criteria and uncalled warnings exist, they must
    both appear in the checklist without overwriting each other. Flag off and
    no hollow item, so computed numbering makes them items 5 and 6."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    plan = _plan(acceptance_criteria=["criterion A", "criterion B"])
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = "diff --git a/x b/x\n"
    e._vcs = vcs

    warnings = ["maestro/x.py: symbol 'orphan' has no external call sites"]
    with patch.object(e, "_find_uncalled_symbols", return_value=warnings), \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(plan, base_hash="base")

    prompt = mock_q.call_args.kwargs["prompt"]
    # Acceptance criteria block + checklist item 5
    assert "## Acceptance Criteria (binding)" in prompt
    assert "5. **Acceptance criteria**" in prompt
    # Uncalled block + checklist item 6
    assert "## Uncalled Symbol Warnings" in prompt
    assert "6. **Uncalled symbols**" in prompt
    # The uncalled item must come after the acceptance-criteria item
    assert prompt.index("5. **Acceptance criteria**") < prompt.index(
        "6. **Uncalled symbols**"
    )


@pytest.mark.asyncio
async def test_self_review_session_path_includes_uncalled_block(
    mem_fs: dict[str, str],
) -> None:
    """The session-reuse path (query_on_session) must also inject the
    uncalled-symbol block — the prompt is shared, not path-specific."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = "diff --git a/x b/x\n"
    e._vcs = vcs

    session = MagicMock()
    warnings = ["maestro/x.py: symbol 'z' has no external call sites"]
    with patch.object(e, "_find_uncalled_symbols", return_value=warnings), \
         patch(
             "maestro.evolution_quality.query_on_session", new_callable=AsyncMock,
         ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base", session=session)

    # prompt is the second positional arg: query_on_session(session, prompt, ...)
    prompt_arg = mock_qos.call_args.args[1]
    assert "## Uncalled Symbol Warnings" in prompt_arg
    # Flag off, no criteria, no hollow: uncalled is item 5.
    assert "5. **Uncalled symbols**" in prompt_arg


@pytest.mark.asyncio
async def test_self_review_passes_diff_to_find_uncalled(
    mem_fs: dict[str, str],
) -> None:
    """self_review must pass the (potentially truncated) diff to
    _find_uncalled_symbols — not the raw VCS output."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    raw_diff = "diff --git a/x b/x\n+def something(): pass\n"
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = raw_diff
    e._vcs = vcs

    received_diffs: list[str] = []

    def capture(diff: str) -> list[str]:
        received_diffs.append(diff)
        return []

    with patch.object(e, "_find_uncalled_symbols", side_effect=capture), \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base")

    assert len(received_diffs) == 1
    assert received_diffs[0] == raw_diff


@pytest.mark.asyncio
async def test_self_review_multiple_uncalled_warnings_all_listed(
    mem_fs: dict[str, str],
) -> None:
    """When multiple symbols are uncalled, all must appear as bullet items."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = "diff --git a/x b/x\n"
    e._vcs = vcs

    warnings = [
        "maestro/a.py: symbol 'func_a' has no external call sites",
        "maestro/b.py: symbol 'ClassB' has no external call sites",
        "maestro/c.py: symbol 'func_c' has no external call sites",
    ]
    with patch.object(e, "_find_uncalled_symbols", return_value=warnings), \
         patch.object(evolution_quality, "query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "- maestro/a.py: symbol 'func_a'" in prompt
    assert "- maestro/b.py: symbol 'ClassB'" in prompt
    assert "- maestro/c.py: symbol 'func_c'" in prompt


# =====================================================================
# Batched git grep — one subprocess for all symbols (#1856)
# =====================================================================

_DIFF_SUBSTRING_OVERLAP = """\
diff --git a/maestro/foo_mod.py b/maestro/foo_mod.py
--- a/maestro/foo_mod.py
+++ b/maestro/foo_mod.py
@@ -0,0 +1,2 @@
+def foo():
+    pass
diff --git a/maestro/foobar_mod.py b/maestro/foobar_mod.py
--- a/maestro/foobar_mod.py
+++ b/maestro/foobar_mod.py
@@ -0,0 +1,2 @@
+def foobar():
+    pass
"""


def test_batched_single_subprocess_invocation(mem_fs: dict[str, str]) -> None:
    """#1856 regression: a multi-symbol diff must fork ``git grep`` exactly
    once (carrying one ``-e <name>`` per symbol), not once per symbol."""
    e = _engine(mem_fs)
    calls: list[list[str]] = []

    def spy(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(cmd))
        return _no_matches(cmd, **kw)

    with patch("subprocess.run", side_effect=spy):
        result = e._find_uncalled_symbols(_DIFF_MULTI_FILE)

    assert len(calls) == 1
    assert calls[0].count("-e") == 2
    assert "alpha_func" in calls[0]
    assert "BetaClass" in calls[0]
    # Empty stdout → both symbols flagged.
    assert len(result) == 2


def test_batched_per_symbol_attribution(mem_fs: dict[str, str]) -> None:
    """A single batched grep stdout carrying rows for both symbols must be
    attributed per symbol: the one with an external call site passes, the one
    appearing only on its own definition line is warned."""
    e = _engine(mem_fs)
    stdout = (
        "maestro/alpha.py:1:def alpha_func():\n"
        "maestro/caller.py:7:    alpha_func()\n"
        "maestro/beta.py:1:class BetaClass:\n"
    )
    with patch("subprocess.run", side_effect=_grep_n(stdout)):
        result = e._find_uncalled_symbols(_DIFF_MULTI_FILE)

    assert len(result) == 1
    assert "BetaClass" in result[0]
    assert "maestro/beta.py" in result[0]


def test_batched_substring_overlap_preserved(mem_fs: dict[str, str]) -> None:
    """``--fixed-strings`` substring semantics are preserved across batching:
    ``foo`` matches a line containing ``foobar`` (so ``foobar``'s definition in
    a *different* file counts as a call site for ``foo``), while ``foobar``
    does not match a ``foo``-only line — identical to per-symbol grep."""
    e = _engine(mem_fs)
    # Only the two definition lines; no other call sites anywhere.
    stdout = (
        "maestro/foo_mod.py:1:def foo():\n"
        "maestro/foobar_mod.py:1:def foobar():\n"
    )
    with patch("subprocess.run", side_effect=_grep_n(stdout)):
        result = e._find_uncalled_symbols(_DIFF_SUBSTRING_OVERLAP)

    # foo: its own def (foo_mod.py, discounted) + foobar's def line in a
    # different file whose content contains the substring 'foo' → counts as an
    # external call site, so foo is NOT flagged. foobar: only its own def line
    # contains 'foobar' (foo's def does not) → flagged.
    assert len(result) == 1
    assert "foobar" in result[0]
    assert "maestro/foobar_mod.py" in result[0]


def test_batched_empty_symbols_skips_subprocess(mem_fs: dict[str, str]) -> None:
    """A diff whose only added symbols are filtered out (all dunders) yields an
    empty symbol list, so the empty-symbol guard must skip the ``git grep``
    fork entirely."""
    e = _engine(mem_fs)
    with patch("subprocess.run") as mock:
        result = e._find_uncalled_symbols(_DIFF_DUNDERS)
    mock.assert_not_called()
    assert result == []


def test_batched_grep_failure_skips_all_symbols(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A single batched-grep failure skips *all* symbols (the new blast radius),
    returns no warnings without raising, and logs the symbol count at debug."""
    e = _engine(mem_fs)

    def boom(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout=30)

    with caplog.at_level(logging.DEBUG, logger="maestro"), \
         patch("subprocess.run", side_effect=boom):
        result = e._find_uncalled_symbols(_DIFF_MULTI_FILE)

    assert result == []
    assert "batched git grep failed for 2 symbol(s)" in caplog.text


def test_symbol_has_call_site_ignores_malformed_rows() -> None:
    """The shared helper parses raw ``git grep`` stdout directly — the
    ``_check_uncalled_symbol_gate`` path in ``loop/implementation.py`` feeds it unfiltered.
    A row that is not ``path:lineno:content`` (e.g. a binary-match notice) must
    be skipped rather than counted as a call site, so a symbol whose only
    well-formed row is its own definition line is reported as uncalled.

    (The batched ``_find_uncalled_symbols`` path pre-filters malformed rows
    before reconstructing blobs, so this contract is exercised here directly.)
    """
    grep_output = (
        "no_colons_here_so_not_a_row\n"  # malformed — skipped, not a call site
        "maestro/dead.py:1:def truly_dead():\n"
    )
    assert _symbol_has_call_site(grep_output, "truly_dead", "maestro/dead.py") is False


def test_symbol_has_call_site_counts_other_file_method_declaration() -> None:
    """An *indented* ``def`` for the symbol in another file is an interface or
    base-class declaration the new method implements — conformance is a genuine
    use, so it counts as a call site even with no explicit invocation."""
    grep_output = "maestro/base.py:10:    def run(self) -> None:\n"
    assert _symbol_has_call_site(grep_output, "run", "maestro/sub.py") is True


def test_symbol_has_call_site_ignores_own_file_method_declaration() -> None:
    """The exemption is file-qualified: an indented ``def`` in the symbol's own
    file is its own definition line, so it must not vouch for itself."""
    grep_output = "maestro/sub.py:10:    def run(self) -> None:\n"
    assert _symbol_has_call_site(grep_output, "run", "maestro/sub.py") is False


# ---------------------------------------------------------------------------
# #4312 — the implement_plan gate runs after convergence's auto_fix rounds
# ---------------------------------------------------------------------------

_POST_CONVERGE_UNWIRED = """\
diff --git a/maestro/new.py b/maestro/new.py
+++ b/maestro/new.py
@@ -0,0 +1,2 @@
+def converge_added_helper():
+    pass
"""


async def _run_gate_after_convergence(
    make_agent,  # type: ignore[no-untyped-def]
    post_converge_diff: str,
) -> tuple[object, AsyncMock, list[str]]:
    """Drive ``implement_plan`` with convergence mocked to swap the diff.

    Returns the agent, the ``create_pr`` mock and the ordered event log.
    """
    from maestro.evolution import QualityGateResult
    from maestro.gate_convergence import ConvergenceResult
    from tests.test_agent_lifecycle import (
        _grep_no_match,
        _make_approved_plan,
        _sub_side_effect_factory,
    )

    agent = make_agent()
    agent.config = Config(
        memory_dir=agent.config.memory_dir,
        workspace=agent.config.workspace,
        self_review_max_rounds=0,
        enable_session_reuse=False,
        uncalled_symbol_gate_enabled=True,
        uncalled_symbol_max_allowed=0,
        post_implement_review=True,
    )
    plan = _make_approved_plan("gate_conv", "Gate after convergence")
    agent.memory.save_evolution_plan(plan)
    agent.memory.update_plan_status("gate_conv", "approved")

    events: list[str] = []
    clean = QualityGateResult(
        passed=True, ruff_ok=True, mypy_ok=True, tests_ok=True, raw_output="",
    )

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
         patch("maestro.evolution.subprocess.run") as mock_sub, \
         patch.object(agent.evolution, "_vcs") as mock_vcs, \
         patch.object(agent.evolution, "cleanup_branch"), \
         patch.object(
             agent.evolution, "create_pr",
             AsyncMock(return_value="https://github.com/repo/pull/1"),
         ) as mock_pr, \
         patch("maestro.agent.subprocess.run", side_effect=_grep_no_match):
        mock_q.return_value = MockQueryResult(response="implemented")
        mock_sub.side_effect = _sub_side_effect_factory()
        mock_vcs.get_head_hash.return_value = "head123"
        # Pre-convergence the cumulative diff adds no defs at all.
        mock_vcs.get_diff.return_value = ""

        def _diff_read(*args: object, **kwargs: object) -> str:
            events.append("get_diff")
            return str(mock_vcs.get_diff.return_value)

        mock_vcs.get_diff.side_effect = _diff_read

        async def _converge(*args: object, **kwargs: object) -> object:
            events.append("converge")
            # Stand-in for an auto_fix commit that adds a def.
            mock_vcs.get_diff.return_value = post_converge_diff
            return ConvergenceResult(outcome="green", review=clean, rounds_used=1)

        with patch.object(
            agent.evolution, "converge_to_green", AsyncMock(side_effect=_converge),
        ):
            await agent.implement_plan("gate_conv")

    return agent, mock_pr, events


@pytest.mark.asyncio
async def test_uncalled_symbol_gate_sees_convergence_auto_fix_defs(make_agent) -> None:  # type: ignore[no-untyped-def]
    """#4312: a def ``auto_fix`` adds while converging is gated before the PR."""
    agent, mock_pr, events = await _run_gate_after_convergence(
        make_agent, _POST_CONVERGE_UNWIRED,
    )

    saved = agent.memory.get_plan("gate_conv")  # type: ignore[attr-defined]
    assert saved is not None
    assert saved.status == "failed"
    assert saved.last_failure_reason is not None
    assert "Uncalled-symbol gate" in saved.last_failure_reason
    assert "converge_added_helper" in saved.last_failure_reason
    mock_pr.assert_not_awaited()
    assert "converge" in events
    assert "get_diff" in events[events.index("converge"):]


@pytest.mark.asyncio
async def test_uncalled_symbol_gate_passes_clean_post_convergence_diff(make_agent) -> None:  # type: ignore[no-untyped-def]
    """A def-free post-convergence diff passes the gate and opens the PR."""
    agent, mock_pr, events = await _run_gate_after_convergence(
        make_agent, "diff --git a/maestro/x.py b/maestro/x.py\n+x = 1\n",
    )

    mock_pr.assert_awaited_once()
    saved = agent.memory.get_plan("gate_conv")  # type: ignore[attr-defined]
    assert saved is not None
    assert saved.status == "approved"
    assert "get_diff" in events[events.index("converge"):]


def test_uncalled_symbol_gate_follows_convergence_in_source() -> None:
    """Structural: the gate call sits after the ``converge_to_green`` await."""
    import ast
    import inspect
    import textwrap

    from maestro.loop import implementation

    tree = ast.parse(textwrap.dedent(inspect.getsource(implementation.implement_plan)))
    gate_lines: list[int] = []
    converge_lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "_check_uncalled_symbol_gate":
            gate_lines.append(node.lineno)
        if isinstance(func, ast.Attribute) and func.attr == "converge_to_green":
            converge_lines.append(node.lineno)
    assert len(gate_lines) == 1
    assert len(converge_lines) == 1
    assert gate_lines[0] > converge_lines[0]


# --- #4382: nested test trees and non-Python files contribute no symbols ---

_DIFF_NESTED_TESTS = """\
diff --git a/maestro_sdk/tests/test_client.py b/maestro_sdk/tests/test_client.py
--- /dev/null
+++ b/maestro_sdk/tests/test_client.py
@@ -0,0 +1,2 @@
+def test_new_behaviour():
+    pass
"""

_DIFF_DOCS_SAMPLE = """\
diff --git a/docs/foo.md b/docs/foo.md
--- a/docs/foo.md
+++ b/docs/foo.md
@@ -0,0 +1,2 @@
+    def example():
+        pass
"""

_DIFF_PROD_SAME_DEF = """\
diff --git a/maestro/foo.py b/maestro/foo.py
--- a/maestro/foo.py
+++ b/maestro/foo.py
@@ -0,0 +1,2 @@
+def example():
+    pass
"""


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tests/x.py", False),
        ("maestro_sdk/tests/x.py", False),
        ("maestro/test_helpers_x.py", False),
        ("maestro_sdk/conftest.py", False),
        ("docs/x.md", False),
        ("maestro/x.pyi", False),
        ("maestro/x.py", True),
        ("maestro/attests.py", True),
    ],
)
def test_counts_for_uncalled_scan(path: str, expected: bool) -> None:
    """Only non-test ``.py`` paths contribute symbols to the uncalled scan."""
    assert counts_for_uncalled_scan(path) is expected


@pytest.mark.parametrize("diff", [_DIFF_NESTED_TESTS, _DIFF_DOCS_SAMPLE])
def test_nested_tests_and_non_python_symbols_skipped(
    mem_fs: dict[str, str], diff: str,
) -> None:
    """A def under ``maestro_sdk/tests/`` or in a Markdown sample yields no
    symbol, so no grep is forked and nothing is warned about."""
    e = _engine(mem_fs)
    run = MagicMock(side_effect=_no_matches)
    with patch("subprocess.run", run):
        result = e._find_uncalled_symbols(diff)
    assert result == []
    run.assert_not_called()


def test_gate_nested_tests_and_docs_pass(mem_fs: dict[str, str]) -> None:
    """The blocking gate passes on nested-test and docs defs under default
    config, while the identical def in a production module still blocks."""
    from maestro.loop.implementation import _check_uncalled_symbol_gate

    cfg = Config()
    stdout = "maestro/foo.py:1:def example():\n"
    with patch("maestro.agent.subprocess.run", side_effect=_grep_n(stdout)):
        assert _check_uncalled_symbol_gate(_DIFF_NESTED_TESTS, cfg) == (True, "")
        assert _check_uncalled_symbol_gate(_DIFF_DOCS_SAMPLE, cfg) == (True, "")
        ok, msg = _check_uncalled_symbol_gate(_DIFF_PROD_SAME_DEF, cfg)
    assert ok is False
    assert "example" in msg


def test_grep_pathspec_excludes_nested_tests(mem_fs: dict[str, str]) -> None:
    """Both gate copies exclude the root and nested test trees from grep."""
    from maestro.loop.implementation import _check_uncalled_symbol_gate

    assert UNCALLED_GREP_EXCLUDES == (
        ":!tests/",
        ":!**/tests/**",
        ":(exclude,glob)**/test_*.py",
        ":(exclude,glob)**/conftest.py",
    )
    engine_run = MagicMock(side_effect=_no_matches)
    with patch("subprocess.run", engine_run):
        _engine(mem_fs)._find_uncalled_symbols(_DIFF_PROD_SAME_DEF)
    gate_run = MagicMock(side_effect=_no_matches)
    with patch("maestro.agent.subprocess.run", gate_run):
        _check_uncalled_symbol_gate(_DIFF_PROD_SAME_DEF, Config())
    for run in (engine_run, gate_run):
        argv = run.call_args.args[0]
        assert argv[argv.index("--"):] == ["--", ".", *UNCALLED_GREP_EXCLUDES]

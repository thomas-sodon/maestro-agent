"""Tests for the literal-only-function (hollow-artifact) detector.

Covers the self-review forcing function added to catch the "noun-not-verb"
failure mode — a newly-added function whose body merely returns a frozen
literal (the exact shape of the original ``self_audit_banner()`` mistake):

- ``_find_literal_only_functions`` — positive (literal return, docstring +
  literal, container literals, async), negative (parametered/computational,
  multi-statement, bare return, non-function source), and the unparseable
  best-effort contract.
- ``_extract_added_source`` — added-line reconstruction, ``tests/`` and
  non-``.py`` exclusion, context-line skipping.
- ``self_review`` prompt injection — the ## Hollow Artifact Warnings block
  and the hollow-artifacts checklist item reach the prompt (mocked SDK,
  mem_fs), and are omitted when no function is hollow.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import EvolutionEngine
from maestro.evolution_quality import (
    _extract_added_source,
    _find_literal_only_functions,
    _is_literal_constant,
)
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import EvolutionPlan, Memory
from maestro.notifications.base import NullNotifier
from tests.conftest import MockQueryResult

_DIR = Path("/fake/self_review")


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
# _find_literal_only_functions — positive cases
# =====================================================================

def test_literal_only_function_is_flagged() -> None:
    """A def whose body is a single ``return <literal>`` must be flagged."""
    src = 'def self_audit_banner():\n    return "all systems nominal"\n'
    result = _find_literal_only_functions(src)
    assert len(result) == 1
    assert "self_audit_banner" in result[0]
    assert "hollow artifact" in result[0]


def test_docstring_plus_return_literal_is_flagged() -> None:
    """A leading docstring is allowed — the function is still hollow."""
    src = 'def banner():\n    """Say hi."""\n    return "hi"\n'
    result = _find_literal_only_functions(src)
    assert len(result) == 1
    assert "banner" in result[0]


def test_async_literal_only_function_is_flagged() -> None:
    """``async def`` bodies are inspected the same as sync ones."""
    src = "async def fetch_banner():\n    return 42\n"
    result = _find_literal_only_functions(src)
    assert len(result) == 1
    assert "fetch_banner" in result[0]


def test_parameterless_return_with_unused_param_is_flagged() -> None:
    """A function taking a param but returning a constant (no reference)
    is still hollow — the literal does not depend on the input."""
    src = "def f(x):\n    return 5\n"
    result = _find_literal_only_functions(src)
    assert len(result) == 1
    assert "f" in result[0]


@pytest.mark.parametrize(
    "body",
    [
        "return [1, 2, 3]",
        "return (1, 2)",
        "return {1, 2}",
        'return {"a": 1, "b": 2}',
        "return [1, [2, 3]]",
        "return []",
    ],
)
def test_container_literal_returns_are_flagged(body: str) -> None:
    """Lists/tuples/sets/dicts of constants (nested too) are frozen literals."""
    src = f"def make():\n    {body}\n"
    result = _find_literal_only_functions(src)
    assert len(result) == 1
    assert "make" in result[0]


@pytest.mark.parametrize(
    "body",
    [
        "return -1",       # unary-minus over an int constant
        "return -3.5",     # unary-minus over a float constant
        "return +7",       # unary-plus over an int constant
        "return - -1",     # nested unary signs over a constant
    ],
)
def test_unary_signed_literal_return_is_flagged(body: str) -> None:
    """A unary-signed numeric literal (``-1``, ``+7``) is a frozen constant —
    in the AST it is ``UnaryOp(USub/UAdd, Constant)``, not a signed
    ``Constant`` — and must be flagged as hollow."""
    src = f"def f():\n    {body}\n"
    result = _find_literal_only_functions(src)
    assert len(result) == 1
    assert "f" in result[0]


def test_multiple_hollow_functions_all_flagged() -> None:
    """Every hollow function in the source yields its own warning."""
    src = (
        'def a():\n    return "x"\n'
        'def b():\n    return "y"\n'
    )
    result = _find_literal_only_functions(src)
    assert len(result) == 2
    assert any("a:" in w for w in result)
    assert any("b:" in w for w in result)


# =====================================================================
# _find_literal_only_functions — negative cases
# =====================================================================

@pytest.mark.parametrize(
    "body",
    [
        "return x + 1",          # computation on a parameter
        "return compute(x)",     # function call
        "return obj.attr",       # attribute access
        "return f'{x}'",         # f-string
        "return [y for y in x]",  # comprehension
        "return",                # bare return (no value)
        "return -x",             # unary minus on a name
        "return ~x",             # bitwise-invert (not a numeric sign)
        "return not x",          # boolean-not (not a numeric sign)
    ],
)
def test_computational_function_is_not_flagged(body: str) -> None:
    """A function that computes its result (or returns nothing) is not
    hollow and must not be flagged."""
    src = f"def f(x):\n    {body}\n"
    assert _find_literal_only_functions(src) == []


@pytest.mark.parametrize(
    "body",
    [
        "return [compute()]",          # container holding a call
        'return {"a": compute()}',     # dict value is a call
        "return {**base}",             # dict unpacking → key is None
    ],
)
def test_container_with_non_literal_is_not_flagged(body: str) -> None:
    """A container is only literal if every element is — a single computed
    element (or ``**`` unpacking) disqualifies it."""
    src = f"def f():\n    {body}\n"
    assert _find_literal_only_functions(src) == []


def test_multi_statement_function_is_not_flagged() -> None:
    """A function with statements beyond the return is doing work."""
    src = "def f():\n    x = 1\n    return 5\n"
    assert _find_literal_only_functions(src) == []


def test_docstring_only_function_is_not_flagged() -> None:
    """A docstring with no return statement is not a literal-only return."""
    src = 'def f():\n    """Just docs."""\n    pass\n'
    assert _find_literal_only_functions(src) == []


def test_pass_only_function_is_not_flagged() -> None:
    """A single non-return statement (``pass``) is not flagged."""
    src = "def f():\n    pass\n"
    assert _find_literal_only_functions(src) == []


def test_non_docstring_leading_expr_is_not_flagged() -> None:
    """A leading bare expression that is not a string is not stripped, so the
    body has two statements and is not flagged."""
    src = "def f():\n    42\n    return 5\n"
    assert _find_literal_only_functions(src) == []


def test_empty_source_returns_no_warnings() -> None:
    """An empty string produces no warnings."""
    assert _find_literal_only_functions("") == []


def test_no_function_source_returns_no_warnings() -> None:
    """Module-level statements without any function definition are ignored."""
    src = "x = 1\ny = x + 2\n"
    assert _find_literal_only_functions(src) == []


def test_unparseable_source_returns_no_warnings() -> None:
    """Best-effort contract: a syntax error yields [] rather than raising."""
    src = "def broken(:\n    return 1\n"
    assert _find_literal_only_functions(src) == []


# =====================================================================
# _is_literal_constant — direct unit coverage
# =====================================================================

def test_is_literal_constant_rejects_bare_return() -> None:
    """``value is None`` (a bare ``return``) is not a literal value."""
    assert _is_literal_constant(None) is False


# =====================================================================
# _extract_added_source — diff reconstruction
# =====================================================================

def test_extract_added_source_includes_only_added_python_lines() -> None:
    """Added lines from a non-test ``.py`` file are reconstructed; the ``+``
    header, context lines, and removed lines are excluded."""
    diff = (
        "diff --git a/maestro/x.py b/maestro/x.py\n"
        "--- a/maestro/x.py\n"
        "+++ b/maestro/x.py\n"
        "@@ -0,0 +1,3 @@\n"
        " context_line\n"
        "-removed_line\n"
        "+def banner():\n"
        '+    return "x"\n'
    )
    source = _extract_added_source(diff)
    assert source == 'def banner():\n    return "x"'


def test_extract_added_source_excludes_test_and_non_python_files() -> None:
    """Added lines from ``tests/`` files and non-``.py`` files are skipped —
    only the production module's additions survive."""
    diff = (
        "diff --git a/maestro/real.py b/maestro/real.py\n"
        "--- a/maestro/real.py\n"
        "+++ b/maestro/real.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def banner():\n"
        '+    return "real"\n'
        "diff --git a/tests/test_real.py b/tests/test_real.py\n"
        "--- a/tests/test_real.py\n"
        "+++ b/tests/test_real.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def fixture():\n"
        '+    return "from_test"\n'
        "diff --git a/CLAUDE.md b/CLAUDE.md\n"
        "--- a/CLAUDE.md\n"
        "+++ b/CLAUDE.md\n"
        "@@ -0,0 +1,1 @@\n"
        "+some doc line\n"
    )
    source = _extract_added_source(diff)
    assert "banner" in source
    assert "from_test" not in source
    assert "some doc line" not in source


def test_extract_added_source_excludes_nested_tests_tree() -> None:
    """A nested pytest tree (``maestro_sdk/tests/``) is test code too (#4382)."""
    diff = (
        "+++ b/maestro_sdk/tests/test_client.py\n"
        "+def fixture():\n"
        '+    return "nested"\n'
    )
    assert _extract_added_source(diff) == ""


def test_extract_added_source_empty_diff() -> None:
    """An empty diff reconstructs to an empty string."""
    assert _extract_added_source("") == ""


def test_extract_then_detect_end_to_end() -> None:
    """A real diff adding a hollow function flows through both helpers."""
    diff = (
        "diff --git a/maestro/x.py b/maestro/x.py\n"
        "--- a/maestro/x.py\n"
        "+++ b/maestro/x.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def self_audit_banner():\n"
        '+    return "frozen"\n'
    )
    result = _find_literal_only_functions(_extract_added_source(diff))
    assert len(result) == 1
    assert "self_audit_banner" in result[0]


# =====================================================================
# self_review prompt integration
# =====================================================================

_HOLLOW_DIFF = (
    "diff --git a/maestro/x.py b/maestro/x.py\n"
    "--- a/maestro/x.py\n"
    "+++ b/maestro/x.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def self_audit_banner():\n"
    '+    return "frozen"\n'
)

_COMPUTATIONAL_DIFF = (
    "diff --git a/maestro/x.py b/maestro/x.py\n"
    "--- a/maestro/x.py\n"
    "+++ b/maestro/x.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def add_one(x):\n"
    "+    return x + 1\n"
)


@pytest.mark.asyncio
async def test_self_review_prompt_includes_hollow_block(
    mem_fs: dict[str, str],
) -> None:
    """When a newly-added function only returns a literal, the prompt must
    contain the ## Hollow Artifact Warnings block and the hollow-artifacts
    checklist item (item 5 here: flag off, no criteria, no uncalled)."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = _HOLLOW_DIFF
    e._vcs = vcs

    with patch.object(e, "_find_uncalled_symbols", return_value=[]), \
         patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "## Hollow Artifact Warnings" in prompt
    assert "self_audit_banner" in prompt
    # Flag off, no criteria, no uncalled: hollow is the sole optional item,
    # so computed numbering places it at item 5 (right after Tests).
    assert "5. **Hollow artifacts**" in prompt


@pytest.mark.asyncio
async def test_self_review_uncalled_parser_reads_untruncated_diff(
    mem_fs: dict[str, str],
) -> None:
    """Regression for #4217: when ``_truncate_diff`` shortens the prompt diff,
    ``_find_uncalled_symbols`` still receives the complete diff."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    big_diff = _COMPUTATIONAL_DIFF + "".join(
        f"+y_{i} = {i}\n" for i in range(20_000)
    )
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = big_diff
    e._vcs = vcs

    with patch.object(e, "_find_uncalled_symbols", return_value=[]) as mock_find, \
         patch(
             "maestro.evolution_quality._extract_added_source", return_value="",
         ) as mock_extract, \
         patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "truncated" in prompt
    mock_find.assert_called_once_with(big_diff)
    mock_extract.assert_called_once_with(big_diff)


@pytest.mark.asyncio
async def test_self_review_caps_warning_bullets(
    mem_fs: dict[str, str],
) -> None:
    """With the parsers reading the uncapped diff (#4217), the uncalled and
    hollow warning lists entering the prompt are capped."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = _COMPUTATIONAL_DIFF
    e._vcs = vcs
    warnings = [f"w_{i}" for i in range(13)]

    with patch.object(e, "_find_uncalled_symbols", return_value=warnings), \
         patch(
             "maestro.evolution_quality._find_literal_only_functions",
             return_value=warnings,
         ), \
         patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert prompt.count("- w_9\n") == 2
    assert "- w_10" not in prompt
    assert prompt.count("- ... and 3 more") == 2


@pytest.mark.asyncio
async def test_self_review_prompt_omits_hollow_block_when_none(
    mem_fs: dict[str, str],
) -> None:
    """A computational function leaves neither block nor the hollow-artifacts
    checklist item."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = _COMPUTATIONAL_DIFF
    e._vcs = vcs

    with patch.object(e, "_find_uncalled_symbols", return_value=[]), \
         patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "## Hollow Artifact Warnings" not in prompt
    assert "**Hollow artifacts**" not in prompt


@pytest.mark.asyncio
async def test_self_review_hollow_block_between_diff_and_instructions(
    mem_fs: dict[str, str],
) -> None:
    """The hollow block must sit between the diff fence and ## Review
    Instructions, after any uncalled-symbol block."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = _HOLLOW_DIFF
    e._vcs = vcs

    with patch.object(e, "_find_uncalled_symbols", return_value=[]), \
         patch("maestro.evolution_quality.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base")

    prompt = mock_q.call_args.kwargs["prompt"]
    diff_fence_end = prompt.index("```\n", prompt.index("```diff"))
    hollow_pos = prompt.index("## Hollow Artifact Warnings")
    review_pos = prompt.index("## Review Instructions")
    assert diff_fence_end < hollow_pos < review_pos


@pytest.mark.asyncio
async def test_self_review_session_path_includes_hollow_block(
    mem_fs: dict[str, str],
) -> None:
    """The session-reuse path (query_on_session) must also inject the block."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = _HOLLOW_DIFF
    e._vcs = vcs

    session = MagicMock()
    with patch.object(e, "_find_uncalled_symbols", return_value=[]), \
         patch(
             "maestro.evolution_quality.query_on_session", new_callable=AsyncMock,
         ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response="LGTM")
        await e.self_review(_plan(), base_hash="base", session=session)

    prompt_arg = mock_qos.call_args.args[1]
    assert "## Hollow Artifact Warnings" in prompt_arg
    # Flag off, no criteria, no uncalled: hollow is item 5 (see the
    # non-session sibling test for the numbering rationale).
    assert "5. **Hollow artifacts**" in prompt_arg


# =====================================================================
# Empty-diff guard — the no-HEAD (loud WARNING) vs no-changes (benign
# INFO) split (issue #458).  A single conflated "could not obtain diff"
# warning cannot tell an anomalous VCS state apart from "nothing changed".
# =====================================================================

@pytest.mark.asyncio
async def test_self_review_skips_and_warns_when_head_unobtainable(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unresolvable HEAD is an anomalous VCS state: self_review returns
    early without querying the model and logs a WARNING naming the
    HEAD-obtain failure (loud, per the ``fail loud`` invariant)."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = ""
    e._vcs = vcs

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
            caplog.at_level(logging.INFO, logger="maestro"):
        await e.self_review(_plan(), base_hash="base")

    mock_q.assert_not_called()
    head_records = [
        r for r in caplog.records if "could not obtain HEAD" in r.message
    ]
    assert len(head_records) == 1
    assert head_records[0].levelno == logging.WARNING
    assert "no changes to review" not in caplog.text


@pytest.mark.asyncio
async def test_self_review_skips_with_info_when_no_changes(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A valid HEAD but an empty diff (e.g. base_hash == current_head) is a
    benign no-op: self_review returns early without querying the model and
    logs a distinct "no changes to review" message at INFO — explicitly not
    the HEAD-obtain-failure warning."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    vcs.get_diff.return_value = ""
    e._vcs = vcs

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
            caplog.at_level(logging.INFO, logger="maestro"):
        await e.self_review(_plan(), base_hash="base")

    mock_q.assert_not_called()
    no_change_records = [
        r for r in caplog.records if "no changes to review" in r.message
    ]
    assert len(no_change_records) == 1
    assert no_change_records[0].levelno == logging.INFO
    assert "could not obtain HEAD" not in caplog.text


@pytest.mark.asyncio
async def test_self_review_skips_and_warns_when_base_hash_empty(
    mem_fs: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An empty base_hash (HEAD unobtainable upstream at implement time) with
    a valid current_head is an anomalous VCS state, not a benign no-op:
    ``get_diff("", current_head)`` returns "" silently, so without a guard it
    would misroute into the INFO "no changes to review" branch. self_review
    must instead return early — before ever calling get_diff — without querying
    the model and log a distinct WARNING naming the missing base commit,
    explicitly not the benign no-changes message."""
    e = _engine(mem_fs)
    e._config = Config(self_review_max_rounds=1)
    vcs = MagicMock()
    vcs.get_head_hash.return_value = "abc"
    e._vcs = vcs

    with patch("maestro.evolution.query", new_callable=AsyncMock) as mock_q, \
            caplog.at_level(logging.INFO, logger="maestro"):
        await e.self_review(_plan(), base_hash="")

    mock_q.assert_not_called()
    vcs.get_diff.assert_not_called()
    base_records = [
        r for r in caplog.records if "no base commit" in r.message
    ]
    assert len(base_records) == 1
    assert base_records[0].levelno == logging.WARNING
    assert "no changes to review" not in caplog.text

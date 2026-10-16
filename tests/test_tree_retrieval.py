"""Unit tests for the confined tree-retrieval owner (proposal 143).

All file reads go through ``mem_fs`` under a fake ``/fake/repo`` root with an
injected tracked set; nothing touches the real tree.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from maestro import tree_retrieval
from maestro.providers.anthropic_tool_adapter import AnthropicToolAdapter
from maestro.tree_retrieval import (
    GREP_MAX_MATCHES,
    GREP_MAX_PATTERN_CHARS,
    MAX_TREE_TOOL_CALLS,
    READ_ONLY_TREE_TOOLS,
    REJECTION_MAX_MATCHES,
    REJECTION_SNIPPET_CHARS,
    TREE_SERVER_NAME,
    TREE_SESSION_MAX_TURNS,
    TREE_TOOL_RESULT_CHAR_CAP,
    RejectionEntry,
    TreeView,
    build_bounded_tree_tool_set,
    find_definition,
    grep_source,
    is_confined_locator,
    list_cli_surface,
    read_door_corpus,
    read_door_text,
    read_source,
    resolve_tool_path,
    search_rejections,
    shipped_cli_surface,
)

_ROOT = Path("/fake/repo")

_CLI_SOURCE = (
    "import argparse\n"
    "parser = argparse.ArgumentParser()\n"
    "parser.add_argument('--verbose')\n"
    "sub = parser.add_subparsers()\n"
    "p_cost = sub.add_parser('cost-report')\n"
    "p_cost.add_argument('--json', help='emit json')\n"
    "sub.add_parser('status')\n"
)


def _seed(mem_fs: dict[str, str], files: dict[str, str]) -> TreeView:
    for rel, text in files.items():
        mem_fs[str(_ROOT / rel)] = text
    return TreeView(root=_ROOT, tracked=frozenset(files))


# ── Door reader ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("locator", "expected"),
    [("maestro/x.py", True), ("/etc/passwd", False), ("../x", False), ("a/../b", False)],
)
def test_is_confined_locator(locator: str, expected: bool) -> None:
    assert is_confined_locator(locator) is expected


def test_read_door_text_reads_confined_file(mem_fs: dict[str, str]) -> None:
    mem_fs["/fake/repo/maestro/x.py"] = "body"
    assert read_door_text(_ROOT, "maestro/x.py") == "body"


def test_read_door_text_refuses_unconfined_and_missing(mem_fs: dict[str, str]) -> None:
    assert read_door_text(_ROOT, "/etc/passwd") is None
    assert read_door_text(_ROOT, "../x") is None
    assert read_door_text(_ROOT, "maestro/missing.py") is None
    assert read_door_text(_ROOT, "maestro/nul\x00.py") is None


def test_read_door_corpus_sorts_and_skips_unreadable(mem_fs: dict[str, str]) -> None:
    mem_fs["/fake/repo/maestro/b.py"] = "B"
    mem_fs["/fake/repo/maestro/a.py"] = "A"
    paths = [
        Path("/fake/repo/maestro/b.py"),
        Path("/fake/repo/maestro/gone.py"),
        Path("/fake/repo/maestro/a.py"),
    ]
    with patch.object(Path, "rglob", return_value=iter(paths)):
        assert read_door_corpus(_ROOT) == "A\nB"


# ── Tool reader admission ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../x",
        "data/lifecycle.json",
        ".env",
        "maestro_sdk/.coverage",
        "Dockerfile",
        "maestro/__pycache__/x.py",
        "maestro/data/x.py",
        "docs/.env.local",
        "maestro/pkg.egg-info/PKG",
    ],
)
def test_tool_reader_refuses_without_reading(mem_fs: dict[str, str], path: str) -> None:
    files = {
        "data/lifecycle.json": "secret",
        ".env": "TOKEN=x",
        "Dockerfile": "FROM x",
        "maestro/__pycache__/x.py": "x",
        "maestro/data/x.py": "x",
        "docs/.env.local": "x",
        "maestro/pkg.egg-info/PKG": "x",
    }
    view = _seed(mem_fs, files)
    assert resolve_tool_path(view, path) is None
    with patch.object(Path, "read_text", side_effect=AssertionError("read")):
        assert read_source(view, path).startswith("refused:")


def test_tool_reader_admits_allowlisted_root_files(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"README.md": "r", "maestro.dev.toml": "t", "maestro/x.py": "x"})
    assert resolve_tool_path(view, "README.md") == _ROOT / "README.md"
    assert resolve_tool_path(view, "maestro.dev.toml") == _ROOT / "maestro.dev.toml"
    assert resolve_tool_path(view, "maestro/x.py") == _ROOT / "maestro/x.py"


def test_tool_reader_refuses_symlink_escaping_root(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/link.py": "x"})
    real_resolve = Path.resolve

    def fake_resolve(self: Path, strict: bool = False) -> Path:
        if self.name == "link.py":
            return Path("/etc/passwd")
        return real_resolve(self, strict=strict)

    with patch.object(Path, "resolve", fake_resolve):
        assert resolve_tool_path(view, "maestro/link.py") is None


def test_read_source_refuses_unreadable_tracked_file(mem_fs: dict[str, str]) -> None:
    view = TreeView(root=_ROOT, tracked=frozenset({"maestro/missing.py"}))
    assert read_source(view, "maestro/missing.py").startswith("refused:")


# ── read_source ────────────────────────────────────────────────────────────────


def test_read_source_numbers_and_honours_offset_limit(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/x.py": "a\nb\nc\nd\n"})
    assert read_source(view, "maestro/x.py", offset=1, limit=2) == (
        "2\tb\n3\tc\n[truncated] 4 lines total"
    )
    assert read_source(view, "maestro/x.py", offset=2) == "3\tc\n4\td"


def test_read_source_marks_empty_window(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/x.py": "a\nb\n"})
    assert read_source(view, "maestro/x.py", offset=9) == "[no lines] 2 lines total"
    assert read_source(view, "maestro/x.py", limit=0) == "[no lines] 2 lines total"


def test_grep_source_admits_safe_quantified_regex(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "abab x"})
    assert grep_source(view, "ab+ x?", regex=True) == "maestro/a.py:1: abab x"


# ── grep_source ────────────────────────────────────────────────────────────────


def test_grep_source_literal_match_skips_unadmitted(mem_fs: dict[str, str]) -> None:
    view = _seed(
        mem_fs,
        {"maestro/a.py": "x = foo(\ny = 1\n", "tests/t.py": "foo(", "maestro/__pycache__/c.py": "foo("},
    )
    assert grep_source(view, "foo(") == "maestro/a.py:1: x = foo("
    assert grep_source(view, "nothing") == "no matches"


def test_grep_source_regex_mode(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "def run():\n    pass\n"})
    assert grep_source(view, r"^def \w+", regex=True) == "maestro/a.py:1: def run():"


def test_grep_source_skips_unreadable_tracked_file(mem_fs: dict[str, str]) -> None:
    view = TreeView(root=_ROOT, tracked=frozenset({"maestro/gone.py"}))
    assert grep_source(view, "x") == "no matches"


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("(a+)+$", "unbounded-backtracking"),
        ("(?:x*)*", "unbounded-backtracking"),
        ("((a+))+$", "unbounded-backtracking"),
        ("(?:(a+))+$", "unbounded-backtracking"),
        (r"(.|\s)*X", "unbounded-backtracking"),
        (r"(?:\s|.)*X", "unbounded-backtracking"),
        ("(foo|bar)+x", "unbounded-backtracking"),
        (r"\s*\s*\s*X", "unbounded-backtracking"),
        (".*.*X", "unbounded-backtracking"),
        (r"\s{0,50}\s*X", "unbounded-backtracking"),
        ("(?=a*)b*c", "unbounded-backtracking"),
        (r"(a)\1*", "unbounded-backtracking"),
        ("(?=(a)+)b", "unbounded-backtracking"),
        ("a?" * 66 + "X", "unbounded-backtracking"),
        ("(?:a|.)" * 28 + "X", "unbounded-backtracking"),
        (r"(.*)\1X", "unbounded-backtracking"),
        ("(?P<a>.*)(?P=a)X", "unbounded-backtracking"),
        ("(a)(?(1)b|c)", "unbounded-backtracking"),
        ("a" * (GREP_MAX_PATTERN_CHARS + 1), "longer than"),
        ("(unclosed", "invalid regex"),
    ],
)
def test_grep_source_refuses_unsafe_regex(
    mem_fs: dict[str, str], pattern: str, reason: str,
) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "aaaa"})
    result = grep_source(view, pattern, regex=True)
    assert result.startswith("refused:")
    assert reason in result


@pytest.mark.parametrize(
    ("pattern", "line"),
    [
        (r"^def \w+", "def run():"),
        ("_classify_error.*", "x = _classify_error(e)"),
        ("(?:a|b)*X", "abX"),
        (r"colou?r_\w+", "colour_red"),
        ("[a-z]+?x", "abx"),
        (r"\bfoo\b", "a foo b"),
        ("(?=d)de?f+", "def"),
        ("(?:foo|bar)_x?y", "bar_xy"),
    ],
)
def test_grep_source_admits_single_repeat_regex(
    mem_fs: dict[str, str], pattern: str, line: str,
) -> None:
    view = _seed(mem_fs, {"maestro/a.py": line})
    assert grep_source(view, pattern, regex=True) == f"maestro/a.py:1: {line}"


def test_grep_source_refuses_empty_pattern(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "x"})
    assert grep_source(view, "") == "refused: empty pattern"


def test_grep_source_stops_at_match_cap(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "hit\n" * (GREP_MAX_MATCHES + 5)})
    lines = grep_source(view, "hit").splitlines()
    assert len(lines) == GREP_MAX_MATCHES + 1
    assert lines[-1] == "[truncated] match budget reached"


def test_grep_source_stops_at_byte_cap(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "hit\n", "maestro/b.py": "hit\n"})
    with patch.object(tree_retrieval, "GREP_MAX_BYTES_SCANNED", 5):
        lines = grep_source(view, "hit").splitlines()
    assert lines == ["maestro/a.py:1: hit", "[truncated] byte budget reached"]


def test_exact_count_repeats_are_not_unbounded() -> None:
    assert tree_retrieval._regex_choice_paths(r"\d{4}-\d{2}") == (1, 17)
    assert tree_retrieval._regex_choice_paths(r"x{3}\w+") == (1, 10)


def test_exact_count_width_is_charged_in_tail_weight() -> None:
    assert tree_retrieval._regex_choice_paths(r".*a{250}b") == (1, 259)
    assert tree_retrieval._regex_choice_paths(r"(a{5})b") == (1, 12)
    assert tree_retrieval._is_unsafe_regex(r"\d{2,4}-\d+") is True


def test_grep_source_flags_end_anchor_hit_at_clip(mem_fs: dict[str, str]) -> None:
    line = "a" * (tree_retrieval.GREP_MAX_REGEX_LINE_CHARS - 1) + "x" + "b" * 5
    view = _seed(mem_fs, {"maestro/a.py": line})
    assert grep_source(view, "x$", regex=True).splitlines()[0] == (
        f"maestro/a.py:1: {line} [clipped]"
    )


def test_choice_path_cap_boundary() -> None:
    assert tree_retrieval._MAX_CHOICE_PATHS == 8
    assert tree_retrieval._is_unsafe_regex("a?" * 3 + "X") is False
    assert tree_retrieval._is_unsafe_regex("a?" * 4 + "X") is True
    assert tree_retrieval._is_unsafe_regex("(?:ab|cd|ef|gh)(?:ij|kl)X") is False
    assert tree_retrieval._is_unsafe_regex("(?:ab|cd|ef)(?:gh|ij|kl)X") is True
    assert tree_retrieval._is_unsafe_regex("(?:a?|b?|c?|d?)X") is False
    assert tree_retrieval._is_unsafe_regex("(?:a?|b?|c?|d?|e)X") is True


def test_grep_source_regex_refusal_names_choice_cap(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "aaaa"})
    assert "at most 8 paths" in grep_source(view, "a?" * 66 + "X", regex=True)


def test_grep_source_regex_searches_only_line_prefix(mem_fs: dict[str, str]) -> None:
    line = "a" * tree_retrieval.GREP_MAX_REGEX_LINE_CHARS + "QZ"
    view = _seed(mem_fs, {"maestro/a.py": line})
    assert grep_source(view, ".*QZ", regex=True) == (
        "no matches\n[truncated] 1 line(s) searched only to the first "
        f"{tree_retrieval.GREP_MAX_REGEX_LINE_CHARS} chars"
    )
    assert grep_source(view, "QZ") == f"maestro/a.py:1: {line}"


def test_grep_source_stops_at_regex_work_budget(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "hit\n", "maestro/b.py": "hit\n"})
    with patch.object(tree_retrieval, "GREP_MAX_REGEX_WORK", 27):
        lines = grep_source(view, "h.t", regex=True).splitlines()
    assert lines == ["maestro/a.py:1: hit", "[truncated] regex work budget reached"]


def test_grep_source_clip_note_follows_hits(mem_fs: dict[str, str]) -> None:
    long_line = "Q" + "a" * tree_retrieval.GREP_MAX_REGEX_LINE_CHARS
    view = _seed(mem_fs, {"maestro/a.py": f"{long_line}\nQ"})
    lines = grep_source(view, "Q", regex=True).splitlines()
    assert lines == [
        f"maestro/a.py:1: {long_line} [clipped]",
        "maestro/a.py:2: Q",
        "[truncated] 1 line(s) searched only to the first "
        f"{tree_retrieval.GREP_MAX_REGEX_LINE_CHARS} chars",
    ]


# ── find_definition ────────────────────────────────────────────────────────────


def test_find_definition_matches_definition_shapes_only(mem_fs: dict[str, str]) -> None:
    view = _seed(
        mem_fs,
        {
            "maestro/a.py": "# widget is great\ndef widget():\n    pass\n",
            "maestro/b.py": "class widget:\n    pass\nwidget = 1\n",
            "tests/c.py": "x = widget()\n",
        },
    )
    assert find_definition(view, "widget") == (
        "maestro/a.py:2\nmaestro/b.py:1\nmaestro/b.py:3"
    )
    assert find_definition(view, "absent") == "no definition of 'absent'"
    assert find_definition(view, " ") == "refused: empty symbol"


def test_find_definition_stops_at_match_cap(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "def f():\n    pass\n" * (GREP_MAX_MATCHES + 1)})
    lines = find_definition(view, "f").splitlines()
    assert len(lines) == GREP_MAX_MATCHES + 1
    assert lines[-1] == "[truncated] match budget reached"


# ── list_cli_surface / shipped_cli_surface ────────────────────────────────────


def test_list_cli_surface_renders_seeded_cli(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/cli.py": _CLI_SOURCE})
    assert list_cli_surface(view) == (
        "global flags: --verbose\ncost-report: --json\nstatus: -"
    )


def test_list_cli_surface_refuses_untracked_cli(mem_fs: dict[str, str]) -> None:
    view = TreeView(root=_ROOT, tracked=frozenset())
    assert list_cli_surface(view).startswith("refused:")


def test_list_cli_surface_no_global_flags(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/cli.py": "sub.add_parser('x')\n"})
    assert list_cli_surface(view) == "global flags: -\nx: -"


def test_shipped_cli_surface_parses_and_tolerates_syntax_error() -> None:
    subcommands, flags, global_flags = shipped_cli_surface(_CLI_SOURCE)
    assert subcommands == {"cost-report", "status"}
    assert flags == {"cost-report": {"--json"}}
    assert global_flags == {"--verbose"}
    assert shipped_cli_surface("def (") == (set(), {}, set())


def test_shipped_cli_surface_omits_nested_subparser_names() -> None:
    source = (
        "parser = argparse.ArgumentParser()\n"
        "sub = parser.add_subparsers()\n"
        "p_x = sub.add_parser('x')\n"
        "x_sub = p_x.add_subparsers()\n"
        "p_send = x_sub.add_parser('send')\n"
        "p_send.add_argument('--to')\n"
        "x_sub.add_parser('inbox')\n"
        "deep_sub = p_send.add_subparsers()\n"
        "p_deep = deep_sub.add_parser('deep')\n"
        "p_deep.add_argument('--deeper')\n"
    )
    subcommands, flags_by_sub, global_flags = shipped_cli_surface(source)
    assert subcommands == {"x"}
    assert "send" not in flags_by_sub
    assert flags_by_sub == {}
    assert global_flags == set()


def test_shipped_cli_surface_credits_argument_group_flags() -> None:
    source = (
        "parser = argparse.ArgumentParser()\n"
        "sub = parser.add_subparsers()\n"
        "p_y = sub.add_parser('y')\n"
        "g = p_y.add_mutually_exclusive_group()\n"
        "g.add_argument('--since')\n"
        "h = p_y.add_argument_group('opts')\n"
        "inner = h.add_mutually_exclusive_group()\n"
        "inner.add_argument('--compare')\n"
        "rg = parser.add_argument_group('g')\n"
        "rg.add_argument('--quiet')\n"
        "orphan = unknown.add_argument_group('z')\n"
        "orphan.add_argument('--lost')\n"
    )
    subcommands, flags_by_sub, global_flags = shipped_cli_surface(source)
    assert subcommands == {"y"}
    assert flags_by_sub == {"y": {"--since", "--compare"}}
    assert global_flags == {"--quiet"}


def test_shipped_cli_surface_unknown_add_parser_receiver_is_top_level() -> None:
    source = (
        "p_a = mystery.add_parser('a')\n"
        "p_a.add_argument('--flag')\n"
        "p_b = mystery.add_parser(name)\n"
        "p_b.add_argument('--unnamed')\n"
    )
    subcommands, flags_by_sub, global_flags = shipped_cli_surface(source)
    assert subcommands == {"a"}
    assert flags_by_sub == {"a": {"--flag"}}
    assert global_flags == set()


def test_shipped_cli_surface_rebound_subparsers_keeps_triple_consistent() -> None:
    source = (
        "parser = argparse.ArgumentParser()\n"
        "sub = parser.add_subparsers()\n"
        "p_x = sub.add_parser('x')\n"
        "p_x.add_argument('--flag')\n"
        "sub.add_parser('bare')\n"
        "register(sub.add_parser('wrapped'))\n"
        "sub = p_x.add_subparsers()\n"
        "sub.add_parser('nested')\n"
    )
    subcommands, flags_by_sub, _ = shipped_cli_surface(source)
    assert subcommands == {"x", "bare"}
    assert flags_by_sub == {"x": {"--flag"}}


def test_find_definition_skips_file_not_mentioning_symbol(
    mem_fs: dict[str, str],
) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "def other():\n    pass\n"})
    with patch.object(tree_retrieval, "_has_definition") as spy:
        assert find_definition(view, "widget") == "no definition of 'widget'"
    spy.assert_not_called()


def test_find_definition_skips_unreadable_tracked_file(mem_fs: dict[str, str]) -> None:
    view = TreeView(root=_ROOT, tracked=frozenset({"maestro/gone.py"}))
    assert find_definition(view, "f") == "no definition of 'f'"


# ── Bounded tool surface ─────────────────────────────────────────────────────────


def _tools(view: TreeView) -> tuple[dict[str, Any], tree_retrieval.TreeReadReceipt]:
    registry, receipt = build_bounded_tree_tool_set(view)
    return {t.name: t for t in registry[TREE_SERVER_NAME]}, receipt


def _call(tools: dict[str, Any], name: str, args: object) -> dict[str, Any]:
    result: dict[str, Any] = asyncio.run(tools[name].handler(args))
    return result


def test_tree_surface_exposes_exactly_the_allowlist(mem_fs: dict[str, str]) -> None:
    registry, _ = build_bounded_tree_tool_set(_seed(mem_fs, {}))
    assert list(registry) == [TREE_SERVER_NAME]
    # Without a rejection corpus the sixth tool is unserved: the five-tool set.
    expected = READ_ONLY_TREE_TOOLS - {"search_rejections"}
    assert {t.name for t in registry[TREE_SERVER_NAME]} == expected
    assert len(registry[TREE_SERVER_NAME]) == 5


def test_tree_session_max_turns_sits_above_budget() -> None:
    assert TREE_SESSION_MAX_TURNS > MAX_TREE_TOOL_CALLS


def test_tree_surface_budget_is_shared_and_graceful(mem_fs: dict[str, str]) -> None:
    tools, _ = _tools(_seed(mem_fs, {"maestro/a.py": "x = 1\n"}))
    for index in range(MAX_TREE_TOOL_CALLS):
        name = "read_source" if index % 2 else "find_definition"
        args = {"path": "maestro/a.py"} if index % 2 else {"symbol": "x"}
        assert "is_error" not in _call(tools, name, args)
    with patch.object(tree_retrieval, "grep_source") as grep:
        over = _call(tools, "grep_source", {"pattern": "x"})
    grep.assert_not_called()
    assert over["is_error"] is True
    assert over["text"] == "<tree read budget exhausted; finish your answer now>"


def test_tree_surface_invocations_are_independent(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "x = 1\n"})
    first, first_receipt = _tools(view)
    for _ in range(MAX_TREE_TOOL_CALLS):
        _call(first, "read_source", {"path": "maestro/a.py"})
    second, second_receipt = _tools(view)
    assert "is_error" not in _call(second, "read_source", {"path": "maestro/a.py"})
    assert len(first_receipt.entries) == MAX_TREE_TOOL_CALLS
    assert len(second_receipt.entries) == 1


def test_tree_surface_caps_over_cap_result(mem_fs: dict[str, str]) -> None:
    line = "y" * 100
    view = _seed(mem_fs, {"maestro/big.py": "\n".join([line] * 200)})
    tools, _ = _tools(view)
    text = _call(tools, "read_source", {"path": "maestro/big.py"})["text"]
    full = read_source(view, "maestro/big.py")
    cut = full.rfind("\n", 0, TREE_TOOL_RESULT_CHAR_CAP + 1)
    kept = full[:cut]
    last_number = kept.rsplit("\n", 1)[-1].split("\t", 1)[0]
    extra = len(full) - cut
    expected = f"{kept}\n\n[truncated — {extra} more chars; page with offset={last_number}]"
    assert text == expected
    resumed = _call(tools, "read_source", {"path": "maestro/big.py", "offset": int(last_number)})
    first_resumed = resumed["text"].split("\t", 1)[0]
    assert first_resumed == str(int(last_number) + 1)


def test_tree_surface_mid_line_cut_advances_past_overlong_line(
    mem_fs: dict[str, str],
) -> None:
    long_line = "w" * (TREE_TOOL_RESULT_CHAR_CAP + 100)
    view = _seed(mem_fs, {"docs/long.md": f"a\n{long_line}\nb\n"})
    tools, _ = _tools(view)
    text = _call(tools, "read_source", {"path": "docs/long.md", "offset": 1})["text"]
    assert text.endswith(
        "; line 2 exceeds the cap and cannot be shown in full; page with offset=2]"
    )
    resumed = _call(tools, "read_source", {"path": "docs/long.md", "offset": 2})["text"]
    assert resumed.split("\t", 1)[0] == "3"


def test_tree_surface_caps_unnumbered_result_without_offset_hint(
    mem_fs: dict[str, str],
) -> None:
    text = "z" * (TREE_TOOL_RESULT_CHAR_CAP + 50)
    with patch.object(tree_retrieval, "grep_source", return_value=text):
        tools, _ = _tools(_seed(mem_fs, {}))
        result = _call(tools, "grep_source", {"pattern": "z"})["text"]
    assert result == text[:TREE_TOOL_RESULT_CHAR_CAP] + "\n\n[truncated — 50 more chars]"


def test_tree_surface_bad_argument_spends_budget(mem_fs: dict[str, str]) -> None:
    tools, receipt = _tools(_seed(mem_fs, {"maestro/a.py": "x = 1\n"}))
    for _ in range(MAX_TREE_TOOL_CALLS):
        _call(tools, "read_source", {"path": 3})
    over = _call(tools, "read_source", {"path": "maestro/a.py"})
    assert over["is_error"] is True
    assert receipt.entries == []


def test_tree_surface_under_cap_result_is_identical(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {"maestro/a.py": "x = 1\n"})
    tools, _ = _tools(view)
    result = _call(tools, "read_source", {"path": "maestro/a.py", "offset": 0, "limit": 5})
    assert result == {"type": "text", "text": read_source(view, "maestro/a.py", 0, 5)}


def test_tree_surface_receipt_records_admission(mem_fs: dict[str, str]) -> None:
    view = _seed(mem_fs, {
        "maestro/a.py": "a = 1\n",
        "maestro/b.py": "b = 1\n",
        "maestro/cli.py": _CLI_SOURCE,
        "data/lifecycle.json": "{}",
    })
    tools, receipt = _tools(view)
    _call(tools, "read_source", {"path": "maestro/a.py"})
    _call(tools, "read_source", {"path": "data/lifecycle.json"})
    _call(tools, "read_source", {"path": "maestro/b.py"})
    _call(tools, "read_source", {"path": "maestro/a.py"})
    _call(tools, "grep_source", {"pattern": "a =", "regex": False})
    _call(tools, "list_cli_surface", {})
    assert receipt.entries == [
        ("read_source", "maestro/a.py", True),
        ("read_source", "data/lifecycle.json", False),
        ("read_source", "maestro/b.py", True),
        ("read_source", "maestro/a.py", True),
        ("grep_source", "a =", True),
        ("list_cli_surface", "maestro/cli.py", True),
    ]
    assert receipt.read_paths() == ["maestro/a.py", "maestro/b.py", "maestro/cli.py"]


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", ".env"])
def test_tree_surface_passes_refusals_through(
    mem_fs: dict[str, str], path: str,
) -> None:
    tools, receipt = _tools(_seed(mem_fs, {".env": "SECRET=1"}))
    text = _call(tools, "read_source", {"path": path})["text"]
    assert text.startswith("refused:")
    assert receipt.read_paths() == []


@pytest.mark.parametrize(
    ("name", "args", "key"),
    [
        ("read_source", {"path": 3}, "path"),
        ("read_source", {"path": "maestro/a.py", "offset": "1"}, "offset"),
        ("read_source", {"path": "maestro/a.py", "limit": True}, "limit"),
        ("grep_source", {"pattern": "x", "regex": "yes"}, "regex"),
        ("find_definition", {"symbol": None}, "symbol"),
    ],
)
def test_tree_surface_refuses_bad_argument_types(
    mem_fs: dict[str, str], name: str, args: dict[str, object], key: str,
) -> None:
    tools, receipt = _tools(_seed(mem_fs, {"maestro/a.py": "x = 1\n"}))
    result = _call(tools, name, args)
    assert result == {"type": "text", "text": f"refused: bad argument {key!r}"}
    assert receipt.entries == []


def test_tree_surface_defaults_missing_and_non_dict_input(
    mem_fs: dict[str, str],
) -> None:
    tools, receipt = _tools(_seed(mem_fs, {"maestro/a.py": "x = 1\n"}))
    assert _call(tools, "find_definition", "not a dict")["text"] == "refused: empty symbol"
    assert _call(tools, "grep_source", {"pattern": "x = 1"})["text"] == "maestro/a.py:1: x = 1"
    assert receipt.entries[0] == ("find_definition", "", False)


def test_tree_surface_materialises_via_anthropic_adapter(
    mem_fs: dict[str, str],
) -> None:
    registry, _ = build_bounded_tree_tool_set(_seed(mem_fs, {}))
    names, servers = AnthropicToolAdapter().materialize(registry)
    served = READ_ONLY_TREE_TOOLS - {"search_rejections"}
    assert sorted(names) == sorted(f"mcp__tree__{n}" for n in served)
    assert list(servers) == [TREE_SERVER_NAME]


# ── read_reality_inventory ─────────────────────────────────────────────────────


def _inventory_tools(
    mem_fs: dict[str, str], reader: Any,
) -> tuple[dict[str, Any], tree_retrieval.TreeReadReceipt]:
    registry, receipt = build_bounded_tree_tool_set(
        _seed(mem_fs, {"maestro/a.py": "x = 1\n"}), inventory=reader,
    )
    return {t.name: t for t in registry[TREE_SERVER_NAME]}, receipt


def test_inventory_tool_returns_injected_text_capped(mem_fs: dict[str, str]) -> None:
    kinds: list[str | None] = []

    def _reader(kind: str | None) -> str:
        kinds.append(kind)
        return "y" * (TREE_TOOL_RESULT_CHAR_CAP + 10) if kind is None else "status"

    tools, receipt = _inventory_tools(mem_fs, _reader)
    assert _call(tools, "read_reality_inventory", {"kind": "cli"})["text"] == "status"
    over = _call(tools, "read_reality_inventory", {"kind": ""})["text"]
    assert over.startswith("y" * TREE_TOOL_RESULT_CHAR_CAP)
    assert "[truncated" in over
    assert kinds == ["cli", None]
    assert receipt.entries == [
        ("read_reality_inventory", "inventory:cli", True),
        ("read_reality_inventory", "inventory:*", True),
    ]
    assert receipt.read_paths() == []


def test_inventory_tool_without_reader_is_refused(mem_fs: dict[str, str]) -> None:
    tools, receipt = _inventory_tools(mem_fs, None)
    result = _call(tools, "read_reality_inventory", {})
    assert result["text"] == "refused: reality inventory unavailable"
    assert receipt.entries == [("read_reality_inventory", "inventory:*", False)]


def test_inventory_tool_spends_shared_budget(mem_fs: dict[str, str]) -> None:
    tools, _ = _inventory_tools(mem_fs, lambda _kind: "ok")
    for _ in range(MAX_TREE_TOOL_CALLS - 1):
        _call(tools, "read_source", {"path": "maestro/a.py"})
    assert _call(tools, "read_reality_inventory", {})["text"] == "ok"
    over = _call(tools, "read_reality_inventory", {})
    assert over["is_error"] is True


def test_inventory_tool_null_kind_is_overview(mem_fs: dict[str, str]) -> None:
    kinds: list[str | None] = []

    def _reader(kind: str | None) -> str:
        kinds.append(kind)
        return "ok"

    tools, receipt = _inventory_tools(mem_fs, _reader)
    assert _call(tools, "read_reality_inventory", {"kind": None})["text"] == "ok"
    assert kinds == [None]
    assert receipt.entries == [("read_reality_inventory", "inventory:*", True)]


def test_inventory_tool_refuses_bad_kind_type(mem_fs: dict[str, str]) -> None:
    tools, receipt = _inventory_tools(mem_fs, lambda _kind: "ok")
    result = _call(tools, "read_reality_inventory", {"kind": 3})
    assert result["text"] == "refused: bad argument 'kind'"
    assert receipt.entries == []


# ── Reader chokepoint (proposal 143 step 5) ─────────────────────────────────────

_GROUNDING_MODULES = (
    "evolution.py", "evolution_submission.py", "plan_grounding.py", "reality_inventory.py",
)
_DIRECT_READ_ATTRS = frozenset({"read_text", "read_bytes", "open"})


def _direct_file_reads(source: str) -> list[int]:
    """Line numbers of direct file-read calls in *source*.

    A bare ``open(...)``, or any attribute call named ``read_text`` /
    ``read_bytes`` / ``open`` (covering ``io.open``, ``os.open`` and
    ``Path.open``) — every way to read text that bypasses ``tree_retrieval``.
    """
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id == "open") or (
            isinstance(func, ast.Attribute) and func.attr in _DIRECT_READ_ATTRS
        ):
            lines.append(node.lineno)
    return lines


def test_grounding_modules_read_source_only_through_tree_retrieval() -> None:
    """The door, the inventory and the grounding owner read through one owner.

    ``tree_retrieval`` is the sole reader of source text for grounding, so the
    door probe, the CLI-surface inventory and the planner's tool surface can
    never drift onto separate readers. ``EvolutionEngine._run_lint_probe`` (the
    ``LINT_PRESENT`` ruff runner) sits outside the chokepoint by design: it
    produces lint findings via ``subprocess``, not file text, so the detector
    never matches it and needs no carve-out.
    """
    root = Path(tree_retrieval.__file__).resolve().parent
    violators = [
        f"maestro/{name}:{line}"
        for name in _GROUNDING_MODULES
        for line in _direct_file_reads((root / name).read_text(encoding="utf-8"))
    ]
    assert violators == []


def test_grounding_chokepoint_detector_flags_a_seeded_rival_reader() -> None:
    """Negative control: a rival reader is flagged, the owner's door is not."""
    rival = (
        "def probe(root, loc):\n"
        "    return Path(root / loc).read_text(encoding='utf-8')\n"
        "def other(path):\n"
        "    return open(path).read()\n"
    )
    assert _direct_file_reads(rival) == [2, 4]
    sanctioned = "def probe(root, loc):\n    return tree_retrieval.read_door_text(root, loc)\n"
    assert _direct_file_reads(sanctioned) == []


# ── search_rejections leaf ───────────────────────────────────────────────────


_REJECTIONS = (
    RejectionEntry(
        title="Parallel gate beside the chokepoint",
        status="rejected",
        reason="duplicate of the shipped owed-act chokepoint",
        rationale="Add a second gate that classifies PR ownership next to it.",
    ),
    RejectionEntry(
        title="A kept pending idea",
        status="pending_review",
        reason="",
        rationale="This gate idea is still live and must not be matched.",
    ),
    RejectionEntry(
        title="An unrelated refusal",
        status="rejected",
        reason="wrong approach",
        rationale="Rewrite the telemetry flush cadence for dashboards.",
    ),
)


def test_search_rejections_returns_matching_rejected_entries() -> None:
    out = search_rejections(_REJECTIONS, "classify ownership gate chokepoint")
    assert "Parallel gate beside the chokepoint" in out
    assert "duplicate of the shipped owed-act chokepoint" in out
    # A pending (non-rejected) entry is never returned even on a word overlap.
    assert "kept pending idea" not in out
    # A rejected entry with no content overlap is excluded.
    assert "unrelated refusal" not in out


def test_search_rejections_reads_only_the_corpus_and_only_rejected() -> None:
    only_pending = (
        RejectionEntry(
            title="Gate idea", status="pending_review", reason="", rationale="gate gate",
        ),
    )
    assert search_rejections(only_pending, "gate") == "no matching rejected plans"


def test_search_rejections_refuses_malformed_without_raising() -> None:
    assert search_rejections(_REJECTIONS, "   ").startswith("refused:")
    assert search_rejections(_REJECTIONS, "a an to").startswith("refused:")
    # A non-string query is refused rather than raising.
    assert search_rejections(_REJECTIONS, None).startswith("refused:")  # type: ignore[arg-type]


def test_search_rejections_respects_result_and_match_caps() -> None:
    long_rationale = "chokepoint " * 500
    many = tuple(
        RejectionEntry(
            title=f"plan {i}", status="rejected", reason="dup",
            rationale=long_rationale,
        )
        for i in range(REJECTION_MAX_MATCHES + 5)
    )
    out = search_rejections(many, "chokepoint")
    assert "match budget reached" in out
    # Each rendered rationale snippet is clipped to the snippet cap.
    assert long_rationale not in out
    assert len(long_rationale) > REJECTION_SNIPPET_CHARS


def test_search_rejections_tool_absent_without_corpus(mem_fs: dict[str, str]) -> None:
    registry, _ = build_bounded_tree_tool_set(_seed(mem_fs, {}))
    names = {t.name for t in registry[TREE_SERVER_NAME]}
    assert "search_rejections" not in names


def test_search_rejections_tool_present_with_corpus(mem_fs: dict[str, str]) -> None:
    registry, receipt = build_bounded_tree_tool_set(
        _seed(mem_fs, {}), rejections=_REJECTIONS,
    )
    tools = {t.name: t for t in registry[TREE_SERVER_NAME]}
    assert "search_rejections" in tools
    assert len(registry[TREE_SERVER_NAME]) == 6
    result = _call(tools, "search_rejections", {"query": "chokepoint gate"})
    assert "Parallel gate beside the chokepoint" in result["text"]
    assert receipt.entries[-1][0] == "search_rejections"
    assert receipt.entries[-1][2] is True


def test_search_rejections_tool_shares_call_budget(mem_fs: dict[str, str]) -> None:
    registry, _ = build_bounded_tree_tool_set(
        _seed(mem_fs, {"maestro/a.py": "x = 1\n"}), rejections=_REJECTIONS,
    )
    tools = {t.name: t for t in registry[TREE_SERVER_NAME]}
    for _ in range(MAX_TREE_TOOL_CALLS):
        _call(tools, "search_rejections", {"query": "gate"})
    over = _call(tools, "search_rejections", {"query": "gate"})
    assert over["is_error"] is True


def test_search_rejections_tool_refuses_bad_argument(mem_fs: dict[str, str]) -> None:
    registry, _ = build_bounded_tree_tool_set(
        _seed(mem_fs, {}), rejections=_REJECTIONS,
    )
    tools = {t.name: t for t in registry[TREE_SERVER_NAME]}
    result = _call(tools, "search_rejections", {"query": 7})
    assert result["text"].startswith("refused: bad argument")
    assert "is_error" not in result

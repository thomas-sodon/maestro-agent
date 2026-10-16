"""Behaviour of ``scripts/deploy_git_config_check.py`` (proposal 150 Step 1), via mem_fs."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "deploy_git_config_check.py"
_REPO = "/fake/repo"
_CONFIG = f"{_REPO}/.git/config"
_ATTRS = f"{_REPO}/.git/info/attributes"
_ORIGIN = "https://github.com/thomas-sodon/maestro-agent.git"

_BASE = f"""[core]
\trepositoryformatversion = 0
\tfilemode = true
\tbare = false
\tlogallrefupdates = true
[remote "origin"]
\turl = {_ORIGIN}
\tfetch = +refs/heads/*:refs/remotes/origin/*
"""


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("deploy_git_config_check", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def checker() -> Any:
    return _load()


def _run(checker: Any) -> int:
    return int(checker.main(["--repo", _REPO, "--expected-origin", _ORIGIN]))


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        ("[core]\n\tfsmonitor = /tmp/x\n", "core.fsmonitor"),
        ("[credential]\n\thelper = store\n", "credential.helper"),
        ("[include]\n\tpath = /tmp/evil\n", "include.path"),
        ('[filter "lfs"]\n\tsmudge = sh -c evil\n', "filter.lfs.smudge"),
        ("[diff]\n\texternal = /tmp/x\n", "diff.external"),
        ("[core]\n\tworktree = /tmp\n", "core.worktree"),
        ("[core]\n\thooksPath = /tmp/hooks\n", "core.hookspath"),
        ("[core] fsmonitor = /tmp/x\n", "core.fsmonitor"),
        ("[filter.x]\n\tsmudge = evil\n", "filter.x.smudge"),
        ("[CORE]\n\tFSMonitor = /tmp/x\n", "core.fsmonitor"),
    ],
)
def test_refuses_key_outside_allowlist(
    checker: Any, mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str], extra: str, named: str
) -> None:
    mem_fs[_CONFIG] = _BASE + extra
    assert _run(checker) == 3
    err = capsys.readouterr().err
    assert f"deploy-git-config: key outside the allowlist: {named}" in err
    assert "/tmp" not in err


@pytest.mark.parametrize(
    ("config", "attributes", "reason"),
    [
        (_BASE, "* filter=x\n", "is non-empty"),
        (None, None, "is missing"),
        (_BASE.replace(_ORIGIN, "https://evil.example/m.git"), None, "differs from the expected origin"),
        (_BASE.replace(f"\turl = {_ORIGIN}\n", ""), None, "remote.origin.url is missing"),
        (_BASE + "[core]\n\tbare = false \\\n", None, "continuation backslash"),
        (_BASE + "[core]\n\t= nokey\n", None, "unparseable line"),
        (_BASE + "[core\n", None, "unterminated section header"),
        ("bare = true\n" + _BASE, None, "unparseable line"),
    ],
)
def test_refuses_for_other_reasons(
    checker: Any,
    mem_fs: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    config: str | None,
    attributes: str | None,
    reason: str,
) -> None:
    if config is not None:
        mem_fs[_CONFIG] = config
    if attributes is not None:
        mem_fs[_ATTRS] = attributes
    assert _run(checker) == 3
    assert reason in capsys.readouterr().err


@pytest.mark.parametrize(
    "exc", [UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), OSError("denied")]
)
def test_read_errors_are_refusals(
    checker: Any, mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str], exc: Exception
) -> None:
    mem_fs[_CONFIG] = _BASE
    with patch.object(Path, "read_text", side_effect=exc):
        assert _run(checker) == 3
    assert f"cannot read the repo config: {type(exc).__name__}" in capsys.readouterr().err


def test_passes_with_many_branch_pairs(checker: Any, mem_fs: dict[str, str]) -> None:
    branches = "".join(
        f'[branch "feat/x-{n}.y"]\n\tremote = origin\n\tmerge = refs/heads/feat/x-{n}.y\n' for n in range(50)
    )
    mem_fs[_CONFIG] = _BASE + branches
    mem_fs[_ATTRS] = ""
    assert _run(checker) == 0


def test_passes_with_quoted_url(checker: Any, mem_fs: dict[str, str]) -> None:
    mem_fs[_CONFIG] = _BASE.replace(f"url = {_ORIGIN}", f'url = "{_ORIGIN}"  # origin')
    assert _run(checker) == 0


def test_branch_key_needs_a_name(checker: Any) -> None:
    assert not checker._key_allowed("branch.remote")
    assert not checker._key_allowed("branch.x.rebase")
    assert checker._key_allowed("branch.a/b.merge")


def test_parser_units(checker: Any) -> None:
    text = '[remote "or\\"ig"]\n\turl = x\n; comment\n# comment\n[core]\n\tbare\n\tfilemode = true ; tail\n'
    assert checker.parse_config_entries(text) == [
        ("remote.or\"ig.url", "x"),
        ("core.bare", ""),
        ("core.filemode", "true"),
    ]


def test_header_with_trailing_comment(checker: Any) -> None:
    assert checker.parse_config_entries("[core] # note\n\tbare\n") == [("core.bare", "")]


@pytest.mark.parametrize("name", ["commondir", "config.worktree"])
def test_refuses_git_dir_redirect(
    checker: Any, mem_fs: dict[str, str], capsys: pytest.CaptureFixture[str], name: str
) -> None:
    mem_fs[_CONFIG] = _BASE
    mem_fs[f"{_REPO}/.git/{name}"] = "../../evil\n"
    assert _run(checker) == 3
    assert f"{_REPO}/.git/{name} exists" in capsys.readouterr().err

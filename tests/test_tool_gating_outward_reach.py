"""Outward-reach refusals of the action gate.

The PreToolUse gate refuses the loop's outward acts that need Thomas's
identity: ``gh repo``/``gh release``/``gh secret`` writes, mutating ``gh api``
calls on outward endpoints, and tag pushes.  Every family is also driven through the cli_paced
``run_gate_bash_hook`` seam so both enforcement paths agree.
"""

from __future__ import annotations

import io
import json

import pytest

from maestro.providers.cli_paced_gate import allowed_branches_to_env, run_gate_bash_hook
from maestro.tool_gating import (
    _OUTWARD_DENY,
    _PARSE_DENY,
    _is_mutating_api_call,
    _is_outward_api_path,
    evaluate_bash_command,
    make_task_pretooluse_hook,
)

_BRANCH = "maestro/task/abc123"
_BRANCH_SET = frozenset({_BRANCH})
_BRANCH_ENV = allowed_branches_to_env(_BRANCH_SET)


def _deny(command: str) -> str | None:
    return evaluate_bash_command(command, _BRANCH_SET, current_branch=_BRANCH)


GH_DENY = [
    "gh repo create o/r --public",
    "gh repo edit o/r --visibility public",
    "gh repo delete o/r",
    "gh repo archive o/r",
    "gh repo rename new",
    "gh repo",
    "gh --repo o/r repo edit --visibility public",
    "gh repo -R o/r delete",
    "gh release create v1",
    "gh release upload v1 dist.tgz",
    "gh release delete v1",
    "gh secret set TOKEN",
    "gh secret delete TOKEN",
    "gh api -X PATCH repos/o/r -f private=false",
    "gh api --method DELETE /repos/o/r",
    "gh api repos/o/r/releases -f tag_name=v1",
    "gh api -XPOST /repos/o/r/releases",
    "gh api repos/o/r/actions/secrets/T --input body.json",
    "gh api repos/o/r/environments/prod/secrets/T -X PUT",
    "gh api user/repos -f name=x",
    "gh api orgs/acme/repos -f name=x",
    "gh api repos/o/r/transfer -f new_owner=x",
    "gh api repos/o/r/generate -f name=x",
    "gh api repos/o/r/dependabot/secrets/T?x=1 -X PUT",
    "gh api -X DELETE https://api.github.com/repos/o/r",
    "gh api -X PATCH https://api.github.com/repos/o/r -f visibility=public",
    "gh api -X DELETE https://ghe.example.com/api/v3/repos/o/r",
    "gh api -X POST repos/o/r/git/refs -f ref=refs/tags/v1 -f sha=abc",
    "gh api -X PATCH repos/o/r/git/refs/heads/main -F force=true",
    "gh api repos/o/r/git/tags -f tag=v1",
    "gh api -X POST repos/o/r/forks",
    "gh api -X POST repos/o/r/merge-upstream -f branch=main",
    "gh api -X POST repos/o/r/keys -f key=x",
    "gh api -X PUT repos/o/r/collaborators/u",
    "gh api -X POST repos/o/r/hooks -f url=x",
    "gh api -X PUT repos/o/r/contents/README.md -f branch=main -f message=x",
    "gh api -X POST repos/o/r/merges -f base=main -f head=x",
    "gh api -X DELETE repos/o/r/branches/main/protection",
    "gh api -X PUT orgs/x/actions/secrets/X -f v=1",
    "gh api -X PUT user/codespaces/secrets/X -f v=1",
    # Ambiguous endpoint with an outward positional fails closed.
    "gh api repos/o/r/releases extra -X GET",
]

GH_ALLOW = [
    "gh repo view",
    "gh repo list",
    "gh repo clone o/r",
    "gh release list",
    "gh release -R o/r view v1",
    "gh release download v1",
    "gh secret list",
    "gh api repos/o/r",
    "gh api repos/o/r/releases",
    "gh api -X PATCH repos/o/r/issues/comments/5 -f body=x",
    "gh issue create --title repo",
    "gh api repos/o/r/issues extra",
    "gh api https://api.github.com/repos/o/r",
    "gh api repos/o/r/git/refs/heads/main",
    "gh api repos/o/r/forks",
    # An explicit GET sends fields as query parameters: still a read.
    "gh api -X GET repos/o/r/releases -f per_page=100",
    "gh api -X GET repos/o/r/contents/README.md -f ref=main",
]


@pytest.mark.parametrize("command", GH_DENY)
def test_gh_outward_denied(command: str) -> None:
    assert _deny(command) == _OUTWARD_DENY


@pytest.mark.parametrize("command", GH_ALLOW)
def test_gh_reads_allowed(command: str) -> None:
    assert _deny(command) is None


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["-X", "POST"], True),
        (["-X"], False),
        (["--method=delete"], True),
        (["--method=GET"], False),
        (["-X=PUT"], True),
        (["-XGET"], False),
        (["-f", "a=b"], True),
        (["--input=x"], True),
        (["-fa=b"], True),
        (["-X", "GET", "-H", "h"], False),
        (["-X", "GET", "-f", "a=b"], False),
        (["--method=HEAD", "--input=x"], False),
        (["-X", "GET", "-X", "POST"], True),
        (["-X", "OPTIONS", "-f", "a=b"], True),
    ],
)
def test_is_mutating_api_call(args: list[str], expected: bool) -> None:
    assert _is_mutating_api_call(args) is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("repos/o/r", True),
        ("/repos/o/r/", True),
        ("repos/o/r?x=1", True),
        ("repos/o/r/issues", False),
        ("repos/o/r/releases/1/assets", True),
        ("user/repos", True),
        ("users/x/repos", False),
        ("https://api.github.com/repos/o/r", True),
        ("HTTP://ghe.local/api/v3/repos/o/r/releases", True),
        ("https://api.github.com/repos/o/r/issues", False),
        ("repos/o/r/git/refs/tags/v1", True),
        ("repos/o/r/forks", True),
        ("repos/o/r/git/commits", False),
        ("repos/o/r/branches/main", False),
        ("orgs/x/repos/y", False),
    ],
)
def test_is_outward_api_path(path: str, expected: bool) -> None:
    assert _is_outward_api_path(path) is expected


@pytest.mark.parametrize(
    "command",
    [
        'npx --call="gh pr merge 5"',
        'npx --loglevel silent -c "gh pr merge 5"',
        'npx --registry x --call "gh repo delete o/r"',
        'npm exec --loglevel silent -c "gh pr merge 5"',
        'npm --yes exec -c "gh pr merge 5"',
        'npx foo -c "gh pr merge 5"',
    ],
)
def test_npx_call_reevaluates_inner_command(command: str) -> None:
    assert _deny(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "apify push", "npx apify-cli push", "npm exec apify-cli -- push",
        "npm test", "npm --yes test -- exec",
    ],
)
def test_apify_verbs_no_longer_special_cased(command: str) -> None:
    """The Apify platform refusal went with the revenue lineage."""
    assert _deny(command) is None


@pytest.mark.asyncio
async def test_task_gate_hook_denies_release_create() -> None:
    hook = make_task_pretooluse_hook(_BRANCH_SET)
    out = await hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "gh release create v1"},
        },
        "t1",
        None,
    )
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == _OUTWARD_DENY


@pytest.mark.parametrize(
    "command",
    [
        'bash -lc "gh release create v1"',
        'bash -lc "gh repo delete o/r"',
        'sh -ec "git -C r push --follow-tags"',
        'sh -ec "git -C r push --tag"',
        'bash -lc "gh -R o/r release create v1"',
        'bash -lc "gh -Ro/r release create v1"',
        'bash -lc "gh --repo o/r repo delete o/r"',
        'bash -lc "gh --repo=o/r pr merge 5"',
    ],
)
def test_wrapper_fallback_fails_closed(command: str) -> None:
    assert _deny(command) == _PARSE_DENY


@pytest.mark.parametrize(
    "command",
    [
        "git describe --tags",
        "git fetch --tags",
        'rg -t sh "apify"',
        "grep -rn apify maestro/",
        "rg apify docs/",
        "grep -r apify .",
        "grep x src/apify docs",
        'bash -lc "cd apify-actor && npm test"',
        'bash -lc "pip install apify-client"',
        'bash -lc "ls ./apify/x"',
    ],
)
def test_tag_reads_and_greps_allowed(command: str) -> None:
    assert _deny(command) is None


def _run_gate(command: str) -> dict[str, object]:
    stdin = io.StringIO(
        json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": command},
            },
        ),
    )
    stdout = io.StringIO()
    assert run_gate_bash_hook(stdin, stdout, _BRANCH_ENV) == 0
    result: dict[str, object] = json.loads(stdout.getvalue())
    return result


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ("gh repo edit o/r --visibility public", _OUTWARD_DENY),
        ("gh api -X DELETE repos/o/r", _OUTWARD_DENY),
        ('bash -lc "gh release create v1"', _PARSE_DENY),
    ],
)
def test_cli_paced_gate_denies(command: str, reason: str) -> None:
    out = _run_gate(command)
    decision = out["hookSpecificOutput"]
    assert isinstance(decision, dict)
    assert decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == reason


@pytest.mark.parametrize("command", ["gh repo view", "apify run", "git fetch --tags"])
def test_cli_paced_gate_allows(command: str) -> None:
    assert _run_gate(command) == {}

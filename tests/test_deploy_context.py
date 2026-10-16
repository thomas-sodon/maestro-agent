"""Text pins for proposal 150 Step 1: entrypoint lock files, safe-deploy git hardening.

Pure text reads — no subprocess, container or network.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ENTRYPOINT = _ROOT / "scripts" / "docker-entrypoint.sh"
_SAFE_DEPLOY = _ROOT / "scripts" / "safe-deploy.sh"
_DEPLOYMENT_DOC = _ROOT / "docs" / "deployment.md"

_CHECKER_CALL = "deploy_git_config_check.py"
_FIRST_GIT = "ROLLBACK_HEAD=$(git rev-parse HEAD)"
_ROLLBACK_TRAP = "trap 'rollback' ERR"


def _code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _first_line_index(lines: list[str], needle: str) -> int:
    for index, line in enumerate(lines):
        if needle in line:
            return index
    raise AssertionError(f"{needle!r} not found")


def _rollback_bounds(lines: list[str]) -> tuple[int, int]:
    start = _first_line_index(lines, "rollback() {")
    end = start
    while lines[end] != "}":
        end += 1
    return start, end


def _top_level_lines(text: str) -> list[str]:
    """Code lines with the ``rollback`` function body removed."""
    lines = _code_lines(text)
    start, end = _rollback_bounds(lines)
    return lines[:start] + lines[end + 1 :]


def _rollback_body(text: str) -> list[str]:
    lines = _code_lines(text)
    start, end = _rollback_bounds(lines)
    return lines[start : end + 1]


def _ordering_holds(text: str) -> bool:
    lines = _top_level_lines(text)
    checker = _first_line_index(lines, _CHECKER_CALL)
    return checker < _first_line_index(lines, _FIRST_GIT) and checker < _first_line_index(lines, _ROLLBACK_TRAP)


def _refusal_block(text: str) -> str:
    lines = _top_level_lines(text)
    start = _first_line_index(lines, _CHECKER_CALL)
    end = start
    while lines[end] != "fi":
        end += 1
    return "\n".join(lines[start : end + 1])


def _hardening_tokens(text: str) -> list[str]:
    line = next(line for line in _code_lines(text) if line.startswith("DEPLOY_GIT_HARDENING=("))
    return shlex.split(line[len("DEPLOY_GIT_HARDENING=(") : line.rindex(")")])


_LOCK_DELETERS = re.compile(r"\brm\b|\bunlink\b|-delete\b")


def _deletes_memory_dir_locks(text: str) -> bool:
    return any(_LOCK_DELETERS.search(line) and "MEMORY_DIR" in line and ".lock" in line for line in _code_lines(text))


def test_entrypoint_keeps_memory_dir_lock_files() -> None:
    text = _ENTRYPOINT.read_text(encoding="utf-8")
    code = "\n".join(_code_lines(text))
    assert not _deletes_memory_dir_locks(text)
    assert "lockfile" not in code
    assert 'MEMORY_DIR="${MAESTRO_MEMORY_DIR:-/workspace/data}"' in code


@pytest.mark.parametrize(
    "seeded",
    [
        'rm -f "$MEMORY_DIR"/*.lock',
        "rm -f $MEMORY_DIR/*.lock",
        'rm -f "${MEMORY_DIR}"/*.lock',
        "find \"$MEMORY_DIR\" -name '*.lock' -delete",
        'for f in "$MEMORY_DIR"/*.lock; do unlink "$f"; done',
    ],
)
def test_seeded_control_memory_dir_lock_deletion_is_caught(seeded: str) -> None:
    text = _ENTRYPOINT.read_text(encoding="utf-8") + "\n" + seeded + "\n"
    assert _deletes_memory_dir_locks(text)


_LOCK_CHOWN = "find \"$MEMORY_DIR\" -maxdepth 1 -name '*.lock' -type f -exec chown 1002:1002 {} + 2>/dev/null || true"


def test_entrypoint_reowns_memory_dir_lock_files_as_root() -> None:
    lines = [line.strip() for line in _code_lines(_ENTRYPOINT.read_text(encoding="utf-8"))]
    root_gate = lines.index('if [ "$(id -u)" = "0" ]; then')
    chown = lines.index(_LOCK_CHOWN)
    assert root_gate < chown
    assert "fi" not in lines[root_gate + 1 : chown]


def test_entrypoint_still_removes_git_index_lock() -> None:
    code = "\n".join(_code_lines(_ENTRYPOINT.read_text(encoding="utf-8")))
    assert "rm -f /workspace/.git/index.lock" in code


def test_allowlist_check_precedes_first_git_command() -> None:
    assert _ordering_holds(_SAFE_DEPLOY.read_text(encoding="utf-8"))


def test_seeded_control_checker_below_first_git_fails_ordering() -> None:
    text = _SAFE_DEPLOY.read_text(encoding="utf-8")
    block = _refusal_block(text)
    mutated = text.replace(block, "").replace(_FIRST_GIT, _FIRST_GIT + "\n" + block)
    assert mutated != text
    assert not _ordering_holds(mutated)


def test_allowlist_refusal_exits_4_on_first_run_and_2_on_reexec() -> None:
    block = _refusal_block(_SAFE_DEPLOY.read_text(encoding="utf-8"))
    assert '--expected-origin "$DEPLOY_EXPECTED_ORIGIN"' in block
    assert "python3 -I" in block
    assert "exit 3" not in block
    assert "exit 4" in block
    assert "exit 2" in block
    assert block.index("exit 2") < block.index("exit 4")
    assert "SKIP_PULL" in block
    assert "rollback" not in block
    assert re.search(r"^\s*git |\$\(git ", block, re.M) is None


def test_git_wrapper_applies_hardening() -> None:
    code = _code_lines(_SAFE_DEPLOY.read_text(encoding="utf-8"))
    assert 'git() { command git "${DEPLOY_GIT_HARDENING[@]}" "$@"; }' in code
    wrapper = _first_line_index(code, "git() {")
    assert wrapper < _first_line_index(code, "git reset --hard")


def test_hardening_clears_then_installs_fixed_helper() -> None:
    tokens = _hardening_tokens(_SAFE_DEPLOY.read_text(encoding="utf-8"))
    values = tokens[1::2]
    assert tokens[0::2] == ["-c"] * len(values)
    assert values[0] == "safe.bareRepository=explicit"
    assert "core.fsmonitor=" in values
    assert "core.hooksPath=/dev/null" in values
    assert "core.sshCommand=" in values
    assert values.count("credential.helper=") == 1
    cleared = values.index("credential.helper=")
    assert values[cleared + 1] == "credential.https://github.com.helper=$DEPLOY_GIT_CREDENTIAL_HELPER"
    assert not any(value.startswith("credential.helper=!") for value in values)


def test_fixed_helper_answers_get_with_deploy_token() -> None:
    text = _SAFE_DEPLOY.read_text(encoding="utf-8")
    match = re.search(r"^DEPLOY_GIT_CREDENTIAL_HELPER='([^']*)'$", text, re.M)
    assert match is not None
    helper = match.group(1)
    assert helper.startswith("!")
    assert 'test "$1" = get' in helper
    assert "echo username=x-access-token" in helper
    assert 'echo "password=$MAESTRO_DEPLOY_GIT_TOKEN"' in helper
    assert 'export MAESTRO_DEPLOY_GIT_TOKEN="${GITHUB_TOKEN:-}"' in text
    assert "export GIT_TERMINAL_PROMPT=0" in text


def test_no_git_bypass_of_wrapper() -> None:
    code = [line for line in _code_lines(_SAFE_DEPLOY.read_text(encoding="utf-8")) if not line.startswith("git() {")]
    joined = "\n".join(code)
    assert "command git" not in joined
    assert "/usr/bin/git" not in joined
    assert "env git" not in joined
    assert "\\git" not in joined


def test_runbook_creates_deploy_clone() -> None:
    doc = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    section = doc[doc.index("### Deploy clone (host trust root") :]
    assert "/opt/maestro-deploy" in section
    assert "git clone https://github.com/thomas-sodon/maestro-agent.git /opt/maestro-deploy" in section
    assert "sudo -u maestro test -w" in section
    assert "Never copy or clone the loop checkout" in section


def test_config_rechecked_before_fetch_and_before_reset() -> None:
    lines = _top_level_lines(_SAFE_DEPLOY.read_text(encoding="utf-8"))
    calls = [index for index, line in enumerate(lines) if _CHECKER_CALL in line]
    assert len(calls) == 3
    fetch = _first_line_index(lines, "git fetch origin main")
    stop = _first_line_index(lines, "docker compose stop -t 1800 loop")
    reset = _first_line_index(lines, 'git reset --hard "$DEPLOY_TARGET"')
    assert calls[1] < fetch
    assert "exit 4" in "\n".join(lines[calls[1] : fetch])
    assert stop < calls[2] < reset
    assert "exit 2" in "\n".join(lines[calls[2] : reset])


def test_rollback_rechecks_config_before_its_reset() -> None:
    body = _rollback_body(_SAFE_DEPLOY.read_text(encoding="utf-8"))
    checker = _first_line_index(body, _CHECKER_CALL)
    reset = _first_line_index(body, 'git reset --hard "$ROLLBACK_HEAD"')
    assert checker < reset
    assert "rc=1" in "\n".join(body[checker:reset])


def test_rollback_config_refusal_skips_restart() -> None:
    body = "\n".join(_rollback_body(_SAFE_DEPLOY.read_text(encoding="utf-8")))
    assert "config_refused=true" in body
    restart = body.index("docker compose up -d --remove-orphans")
    guard = body.index('if [ "$config_refused" = true ]; then')
    assert guard < restart


def test_rollback_config_refusal_stops_services() -> None:
    body = "\n".join(_rollback_body(_SAFE_DEPLOY.read_text(encoding="utf-8")))
    guard = body.index('if [ "$config_refused" = true ]; then')
    stop = body.index("docker compose stop ||")
    restart = body.index("docker compose up -d --remove-orphans")
    assert guard < stop < restart


def test_deploy_agent_keeps_attempt_marker_on_config_refusal() -> None:
    agent = (_SAFE_DEPLOY.parent / "deploy-agent.sh").read_text(encoding="utf-8")
    arm = re.search(r"^\s*4\)\n(?P<body>.*?);;", agent, re.M | re.S)
    assert arm is not None
    assert "ATTEMPT_FILE" not in arm.group("body")
    assert "HEADLINE=" in arm.group("body")


def test_exit_code_contract_documents_config_refusal_as_4() -> None:
    """Headers and runbook file the persistent config refusal under exit 4, not 3."""
    header = _SAFE_DEPLOY.read_text(encoding="utf-8").split("set -Eeuo", 1)[0]
    agent = (_ROOT / "scripts" / "deploy-agent.sh").read_text(encoding="utf-8")
    doc = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    assert "#   4 — git config refused" in header
    assert "allowlist" not in header.split("#   4 —", 1)[0]
    assert "#   4 — git config refused" in agent
    assert "before the fetch exits 3" not in doc
    assert "before the fetch exits 4" in doc

"""Tests for scripts/safe-deploy.sh loop-heartbeat diagnostic path.

Issue #492: the deploy smoke-timeout diagnostic used to hardcode the container
heartbeat path (/workspace/data/loop.heartbeat) directly in the
`docker compose exec` command — twice, decoupled from docker-compose.yml, which
is the source of truth (the `.:/workspace` mount + MAESTRO_WORKSPACE=/workspace,
referenced by the loop healthcheck's os.stat('...')). A mount or filename change
in compose that wasn't mirrored in the script would leave the diagnostic
silently reporting the wrong path, sending a deploy operator chasing a phantom
"loop never wrote its heartbeat" failure. The fix single-sources the path into a
LOOP_HEARTBEAT_PATH constant; these tests fail loudly if that constant drifts
from the compose healthcheck or if the literal is duplicated in the script.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "safe-deploy.sh"
_COMPOSE = _ROOT / "docker-compose.yml"

_HEARTBEAT_LITERAL = "/workspace/data/loop.heartbeat"


def _compose_heartbeat_path(text: str) -> str:
    """Extract the loop-service healthcheck heartbeat path from compose."""
    match = re.search(r"os\.stat\('([^']+)'\)", text)
    assert match is not None, (
        "could not find the loop healthcheck os.stat('...') literal in "
        "docker-compose.yml"
    )
    return match.group(1)


def _script_heartbeat_path(text: str) -> str:
    """Extract the LOOP_HEARTBEAT_PATH constant value from safe-deploy.sh."""
    match = re.search(r'LOOP_HEARTBEAT_PATH="([^"]+)"', text)
    assert match is not None, (
        "could not find the LOOP_HEARTBEAT_PATH constant in safe-deploy.sh"
    )
    return match.group(1)


def test_heartbeat_path_matches_compose() -> None:
    """Issue #492: safe-deploy diagnostic path must match the compose healthcheck.

    A mount or filename change in docker-compose.yml not mirrored in
    safe-deploy.sh's LOOP_HEARTBEAT_PATH now fails this gate instead of silently
    reporting the wrong path to a deploy operator.
    """
    compose_path = _compose_heartbeat_path(_COMPOSE.read_text(encoding="utf-8"))
    script_path = _script_heartbeat_path(_SCRIPT.read_text(encoding="utf-8"))
    assert script_path == compose_path


def test_heartbeat_path_defined_once() -> None:
    """Issue #492: the heartbeat literal must be single-sourced, not duplicated.

    The diagnostic must reference $LOOP_HEARTBEAT_PATH rather than re-hardcoding
    the path, so the literal appears at most once (the constant definition).
    """
    text = _SCRIPT.read_text(encoding="utf-8")
    assert text.count(_HEARTBEAT_LITERAL) <= 1


def _diagnostic_block(text: str) -> str:
    """Return the loop-heartbeat diagnostic command block from safe-deploy.sh.

    Anchored on the stable ``Loop heartbeat file status:`` banner the diagnostic
    prints (structure-independent of the path reference the assertions below
    check) through to the ``|| log`` / ``|| true`` terminator of the heartbeat
    probe. This captures the whole inlined ``python3 -c`` command whether it
    reads the host bind-mount (#491) or exec-s into the container, so the matcher
    does not depend on the very thing the assertions check.
    """
    lines = text.splitlines()
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if "Loop heartbeat file status:" in line
        ),
        None,
    )
    assert start is not None, (
        "could not find the loop-heartbeat diagnostic banner in safe-deploy.sh"
    )
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if "|| log" in lines[i] or "|| true" in lines[i]
        ),
        None,
    )
    assert end is not None, (
        "could not find the terminator of the loop-heartbeat diagnostic in "
        "safe-deploy.sh"
    )
    return "\n".join(lines[start : end + 1])


def test_diagnostic_references_constant() -> None:
    """Issue #492 (r1): the diagnostic body must reference LOOP_HEARTBEAT_PATH.

    test_heartbeat_path_matches_compose and test_heartbeat_path_defined_once
    guard drift and duplication of the literal, but neither ensures the
    diagnostic actually *uses* the constant: a future edit that re-hardcodes a
    different (wrong) literal in the diagnostic while leaving LOOP_HEARTBEAT_PATH
    defined-but-unused would slip past both — the constant would still match
    compose and the old literal would still appear at most once. Anchor the
    diagnostic to the constant so a dead constant fails loudly, and forbid the
    literal from reappearing inside the diagnostic itself. Post-#491 the
    diagnostic reads the host bind-mount and derives its target from the constant
    (``${LOOP_HEARTBEAT_PATH#/workspace/}``) rather than exec-ing into the
    container, so the reference is checked over the whole diagnostic block.
    """
    diag = _diagnostic_block(_SCRIPT.read_text(encoding="utf-8"))
    assert "LOOP_HEARTBEAT_PATH" in diag, (
        "the heartbeat diagnostic must derive its target from "
        "LOOP_HEARTBEAT_PATH, not a re-hardcoded literal"
    )
    assert _HEARTBEAT_LITERAL not in diag, (
        "the heartbeat diagnostic must not re-hardcode the heartbeat path literal"
    )


def _compose_loop_mount_target(text: str) -> str:
    """Extract the loop-service repo-root bind-mount container target from compose.

    The loop service mounts the repo root (``.``) at this path; the heartbeat
    diagnostic strips exactly this prefix to recover the host-relative path.
    Scoped to the ``loop:`` service block so the dashboard's own ``.:/workspace``
    mount cannot satisfy the match.
    """
    loop_block = re.search(r"\n  loop:\n(.*?)(?=\n  \w+:\n)", text, re.DOTALL)
    assert loop_block is not None, (
        "could not isolate the loop service block in docker-compose.yml"
    )
    match = re.search(r"-\s+\.:([^\s:]+)", loop_block.group(1))
    assert match is not None, (
        "could not find the loop-service '.:<target>' bind mount in "
        "docker-compose.yml"
    )
    return match.group(1)


def _script_strip_prefix(text: str) -> str:
    """Extract the mount prefix the diagnostic strips from LOOP_HEARTBEAT_PATH."""
    match = re.search(r"\$\{LOOP_HEARTBEAT_PATH#([^}]+)\}", text)
    assert match is not None, (
        "could not find the ${LOOP_HEARTBEAT_PATH#<prefix>} strip expression in "
        "safe-deploy.sh"
    )
    return match.group(1)


def test_host_heartbeat_derivation_matches_compose_mount() -> None:
    """Issue #492 (r2): the host-relative strip must track the compose mount.

    test_heartbeat_path_matches_compose ties the full LOOP_HEARTBEAT_PATH
    constant to the compose healthcheck, but the diagnostic reads the host
    bind-mount via ``${LOOP_HEARTBEAT_PATH#/workspace/}`` — a *second* hardcoding
    of the ``/workspace`` mount prefix that no test tied to compose. If the
    compose mount prefix changed (e.g. to ``/app``), the first test would force
    LOOP_HEARTBEAT_PATH to ``/app/data/loop.heartbeat`` but the ``#/workspace/``
    strip would become a no-op, leaving the host path as the absolute *container*
    path — the exact silent "wrong path / phantom heartbeat-never-written" drift
    #492 set out to eliminate. Derive the host-relative path from both files and
    assert they agree so a mount-prefix change fails loudly here too, not just a
    filename change.
    """
    compose_text = _COMPOSE.read_text(encoding="utf-8")
    script_text = _SCRIPT.read_text(encoding="utf-8")

    mount_target = _compose_loop_mount_target(compose_text)
    compose_heartbeat = _compose_heartbeat_path(compose_text)
    compose_host_relative = os.path.relpath(compose_heartbeat, mount_target)

    strip_prefix = _script_strip_prefix(script_text)
    script_heartbeat = _script_heartbeat_path(script_text)
    script_host_relative = script_heartbeat.removeprefix(strip_prefix)

    assert script_host_relative == compose_host_relative

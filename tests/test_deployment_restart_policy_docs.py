"""Tests for the Restart Policy documentation in docs/deployment.md.

Issue #563: the ``## Restart Policy`` section framed the manual-restart
requirement entirely around crashes ("Behaviour on crash", "When the loop exits
non-zero, it stays down"). But ``restart: "no"`` has a second, less-obvious
consequence the docs never stated: the loop container also does *not* come back
after a host reboot or a Docker daemon restart — Docker never auto-starts a
``restart: "no"`` container on daemon startup, regardless of how it last exited.
An operator who reboots the host and expects the loop to resume finds it silently
``exited``, unalerted until someone looks — and then, on the manual restart, gets
a "Maestro crashed" ping for what was a planned reboot. This guard asserts the
section documents the reboot / daemon-restart case, names the manual recovery
action, and warns about that crash signal, so the crash-centric framing can never
again omit it — and that the compose rationale comment the doc mirrors stays
broader than crash-only.

Assertions target only wording that arrived *with* the fix: the pre-fix section
already contained ``restart: "no"``, ``docker compose ps`` and ``docker compose
start loop``, so asserting on those alone would pass against the unfixed doc.

Like ``test_safe_deploy_heartbeat_path.py``, these are content assertions over
committed repo source (no subprocess, network, or sleep), so the
``mem_fs``/mock-externals rule does not apply.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DEPLOYMENT_DOC = _ROOT / "docs" / "deployment.md"
_COMPOSE = _ROOT / "docker-compose.yml"


def _restart_policy_section(text: str) -> str:
    """Return the body of the ``## Restart Policy`` section of the doc.

    Spans from the ``## Restart Policy`` heading to the next top-level ``## ``
    heading (or end of file), so the assertions are scoped to that section and a
    stray "reboot" elsewhere in the doc cannot satisfy them.
    """
    match = re.search(
        r"^## Restart Policy\n(.*?)(?=^## |\Z)",
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, (
        "could not find the '## Restart Policy' section in docs/deployment.md"
    )
    return match.group(1)


def _loop_restart_comment(text: str) -> str:
    """Return the comment block directly above the loop service's ``restart``.

    Walks backwards from the ``restart: "no"`` key over the contiguous run of
    comment lines that documents *why* the loop opts out of auto-restart, so the
    assertion is scoped to that rationale rather than the whole compose file.
    """
    lines = text.splitlines()
    restart_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == 'restart: "no"'),
        None,
    )
    assert restart_idx is not None, (
        'could not find the loop service\'s `restart: "no"` key in '
        "docker-compose.yml"
    )

    comment: list[str] = []
    for line in reversed(lines[:restart_idx]):
        if not line.strip().startswith("#"):
            break
        comment.append(line.strip().lstrip("#").strip())
    assert comment, (
        'the loop service\'s `restart: "no"` must keep its rationale comment '
        "explaining why auto-restart is declined"
    )
    return "\n".join(reversed(comment))


def test_restart_policy_documents_host_reboot() -> None:
    """Issue #563: the Restart Policy section must document the reboot case.

    The section must state, alongside the loop ``restart: "no"`` policy, that the
    container will not auto-start after a host reboot / Docker daemon restart and
    name the manual recovery action — so the crash-centric framing cannot again
    leave an operator believing a clean reboot behaves differently.
    """
    section = _restart_policy_section(_DEPLOYMENT_DOC.read_text(encoding="utf-8"))
    assert 'restart: "no"' in section, (
        'the Restart Policy section must reference the loop restart: "no" policy'
    )
    assert "reboot" in section, (
        "the Restart Policy section must document the host-reboot case, not just "
        "crashes"
    )
    assert "daemon" in section, (
        "the Restart Policy section must document the Docker daemon-restart case "
        "alongside the host reboot"
    )
    assert "docker compose up -d" in section, (
        "the Restart Policy section must name the manual recovery command that "
        "brings a non-auto-started loop back"
    )


def test_restart_policy_reboot_note_names_eager_shutdown_marker() -> None:
    """Issue #4082: the reboot note must match the eager shutdown marker.

    A reboot stops the container with SIGTERM before any SIGKILL, and
    ``_on_shutdown_signal`` eagerly writes ``last_shutdown``, so the restart
    normally reports a clean stop. The note must say so, keep pointing at Crash
    Handling for the exception cases, and never regress to claiming a reboot
    "almost always" SIGKILLs the loop into a crash report.
    """
    section = _restart_policy_section(_DEPLOYMENT_DOC.read_text(encoding="utf-8"))
    assert "last_shutdown" in section, (
        "the reboot note must name the last_shutdown marker"
    )
    assert "_on_shutdown_signal" in section, (
        "the reboot note must name the eager _on_shutdown_signal handler"
    )
    assert (
        "loop-lifecycle.md#eager-shutdown-marker-sigterm-during-long-phase-protection"
        in section
    ), "the reboot note must link the Eager Shutdown Marker section"
    assert "Crash Handling" in section, (
        "the reboot note must point at Crash Handling for the exception cases"
    )
    assert "almost always SIGKILLs" not in section


def test_compose_loop_rationale_covers_more_than_crashes() -> None:
    """Issue #563: the compose rationale must not narrow back to crash-only.

    docs/deployment.md and the compose comment state the same policy; the doc is
    gated above, so this gate keeps its compose counterpart from silently
    reverting to "on crash, stay down" wording that omits the reboot case.
    """
    comment = _loop_restart_comment(_COMPOSE.read_text(encoding="utf-8"))
    assert "reboot" in comment, (
        'the loop service\'s `restart: "no"` rationale comment must state that '
        "the loop also stays down across host reboots, not just crashes"
    )

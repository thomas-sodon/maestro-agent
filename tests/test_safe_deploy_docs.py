"""Doc-fidelity guard for the safe-deploy sequence in docs/deployment.md.

Issue #4318: the "What ``safe-deploy.sh`` does" list described an older deploy
(``git pull`` → rebuild → disk guard → stop, with a ``git checkout`` rollback),
while the script now fetches read-only, runs the disk guard and stop *before*
touching the tree, then ``git reset --hard``s to a pinned target. The smoke
bullet also claimed ``contemplate`` drops to ``effort="low"``, contradicting
the ``run_loop`` docstring. An operator following the stale doc by hand would
reintroduce both hazards the script's ordering removes.

These tests pin the doc section to the script and anchor the script's own
ordering, so neither can drift silently. Like the other deployment-doc tests,
they are content assertions over committed repo source (no subprocess,
network, or sleep), so the ``mem_fs``/mock-externals rule does not apply.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DEPLOYMENT_DOC = _ROOT / "docs" / "deployment.md"
_SAFE_DEPLOY = _ROOT / "scripts" / "safe-deploy.sh"

_SECTION_START = "**What `safe-deploy.sh` does, once the host agent runs it:**"
_SECTION_END = "**Failure visibility:**"

_SMOKE_SKIPPED_PHASES = (
    "respond_to_messages",
    "implement_approved_plans",
    "check_merged_plans",
    "process_rejected_plans",
    "generate_task",
    "evolve",
    "dream",
)


def _deploy_section() -> str:
    """Return the step list plus rollback bullets of the safe-deploy section."""
    text = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    start = text.find(_SECTION_START)
    assert start != -1
    end = text.find(_SECTION_END, start)
    assert end != -1
    return text[start:end]


def _rollback_bullets(section: str) -> str:
    start = section.find("**Rollback behavior:**")
    assert start != -1
    return section[start:]


def _step_list(section: str) -> str:
    end = section.find("**Rollback behavior:**")
    assert end != -1
    return section[:end]


def _smoke_bullet(section: str) -> str:
    start = section.find("**Single iteration (smoke mode)**")
    assert start != -1
    end = section.find("\n", start)
    return section[start:end]


def _script_code_lines() -> list[str]:
    """Non-comment lines of safe-deploy.sh (comments may cite history)."""
    lines = _SAFE_DEPLOY.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if not line.lstrip().startswith("#")]


def _first_line_index(lines: list[str], needle: str) -> int:
    for index, line in enumerate(lines):
        if needle in line:
            return index
    raise AssertionError(f"{needle!r} not found in scripts/safe-deploy.sh")


def test_section_names_no_pull_or_checkout() -> None:
    section = _deploy_section()
    assert "git pull" not in section
    assert "git checkout" not in section


def test_section_uses_reset_hard_for_deploy_and_rollback() -> None:
    section = _deploy_section()
    assert "git reset --hard" in _step_list(section)
    assert "git reset --hard $ROLLBACK_HEAD" in _rollback_bullets(section)


def test_steps_order_guard_stop_reset_rebuild() -> None:
    steps = _step_list(_deploy_section())
    guard = steps.find("disk-space guard")
    stop = steps.find("Gracefully stops the loop")
    reset = steps.find("git reset --hard")
    rebuild = steps.find("Rebuilds the Docker image")
    assert guard != -1
    assert guard < stop
    assert stop < reset
    assert reset < rebuild


def test_script_has_no_pull_or_checkout() -> None:
    code = "\n".join(_script_code_lines())
    assert "git pull" not in code
    assert "git checkout" not in code


def test_script_orders_fetch_stop_reset_build() -> None:
    lines = _script_code_lines()
    fetch = _first_line_index(lines, "git fetch origin main")
    stop = _first_line_index(lines, "docker compose stop -t 1800 loop")
    reset = _first_line_index(lines, 'git reset --hard "$DEPLOY_TARGET"')
    build = _first_line_index(lines, "docker compose build")
    assert fetch < stop
    assert stop < reset
    assert reset < build


def test_smoke_bullet_matches_run_loop_semantics() -> None:
    bullet = _smoke_bullet(_deploy_section())
    assert 'effort="low"' not in bullet
    for phase in _SMOKE_SKIPPED_PHASES:
        assert f"`{phase}`" in bullet

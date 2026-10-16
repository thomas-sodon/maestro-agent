"""Tests for scripts/safe-deploy.sh smoke-test timeout wrappers.

Issue #178: both smoke-test invocations in safe-deploy.sh must be wrapped with
`timeout` so a hung container cannot tie up the self-hosted deploy runner
indefinitely. The deploy job runs under `concurrency: deploy-production` with
`cancel-in-progress: false` and no job-level `timeout-minutes:`, so the fix
lives at the smoke-test boundary.
"""

from __future__ import annotations

from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "safe-deploy.sh"


def _pipeline_containing(marker: str, text: str) -> str:
    """Return the `if !` block whose body contains marker."""
    idx = text.index(marker)
    start = text.rfind("if !", 0, idx)
    assert start != -1, f"no `if !` block found for {marker!r}"
    return text[start:idx]


def test_preflight_smoke_test_is_timeout_wrapped() -> None:
    """Issue #178: preflight smoke test must be wrapped with `timeout`."""
    text = _SCRIPT.read_text(encoding="utf-8")
    pipeline = _pipeline_containing("loop preflight", text)
    assert "timeout" in pipeline, (
        "preflight smoke test is not wrapped with `timeout` — a hung "
        "container would tie up the self-hosted deploy runner indefinitely"
    )


def test_single_iteration_smoke_test_is_timeout_wrapped() -> None:
    """Issue #178: single-iteration smoke test must be wrapped with `timeout`."""
    text = _SCRIPT.read_text(encoding="utf-8")
    pipeline = _pipeline_containing("loop loop --smoke", text)
    assert "timeout" in pipeline, (
        "single-iteration smoke test is not wrapped with `timeout` — a hung "
        "container would tie up the self-hosted deploy runner indefinitely"
    )

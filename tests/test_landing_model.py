"""Tests for ``remediation_tracker.apply_landing_model`` (#4307).

The single owner of what a ``BEHIND_MAIN`` merge-block verdict means under the
active landing model: a Tend catch-up blocker while
``[loop] tend_main_merge_enabled`` is on, no blocker at all under the merge
queue (bar a conflict), with the CI/approval rungs behind it still speaking.
"""

from __future__ import annotations

import pytest

from maestro.remediation_tracker import (
    MERGE_BLOCK_AWAITING_APPROVAL,
    MERGE_BLOCK_AWAITING_PUSH,
    MERGE_BLOCK_BEHIND_MAIN,
    MERGE_BLOCK_CI_FAILING,
    MERGE_BLOCK_CI_PENDING,
    MERGE_BLOCK_CONFLICTING,
    MERGE_BLOCK_NONE,
    apply_landing_model,
)


@pytest.mark.parametrize(
    ("clean", "conclusion", "state", "expected"),
    [
        (True, "success", "BEHIND", MERGE_BLOCK_NONE),
        (True, "success", "", MERGE_BLOCK_NONE),
        (False, "success", "BEHIND", MERGE_BLOCK_CONFLICTING),
        (True, "failure", "BEHIND", MERGE_BLOCK_CI_FAILING),
        (True, "pending", "BEHIND", MERGE_BLOCK_CI_PENDING),
        (True, "success", "BLOCKED", MERGE_BLOCK_AWAITING_APPROVAL),
    ],
)
def test_behind_under_merge_queue_drops_behindness(
    clean: bool, conclusion: str, state: str, expected: str,
) -> None:
    reason = apply_landing_model(
        MERGE_BLOCK_BEHIND_MAIN,
        tend_catch_up=False,
        cleanly_mergeable=clean,
        check_conclusion=conclusion,
        merge_state_status=state,
    )
    assert reason == expected


@pytest.mark.parametrize("clean", [True, False])
def test_behind_under_tend_catch_up_is_unchanged(clean: bool) -> None:
    reason = apply_landing_model(
        MERGE_BLOCK_BEHIND_MAIN,
        tend_catch_up=True,
        cleanly_mergeable=clean,
        check_conclusion="success",
        merge_state_status="BEHIND",
    )
    assert reason == MERGE_BLOCK_BEHIND_MAIN


@pytest.mark.parametrize("tend_catch_up", [True, False])
@pytest.mark.parametrize(
    "code",
    [
        MERGE_BLOCK_NONE,
        MERGE_BLOCK_CONFLICTING,
        MERGE_BLOCK_CI_FAILING,
        MERGE_BLOCK_CI_PENDING,
        MERGE_BLOCK_AWAITING_APPROVAL,
        MERGE_BLOCK_AWAITING_PUSH,
    ],
)
def test_non_behind_codes_pass_through(code: str, tend_catch_up: bool) -> None:
    reason = apply_landing_model(
        code,
        tend_catch_up=tend_catch_up,
        cleanly_mergeable=False,
        check_conclusion="success",
        merge_state_status="",
    )
    assert reason == code

"""Acceptance tests for the per-PR lap-budget standdown (proposal 080 Phase 3).

Exercises the integrated lap-budget cycle the Tend route runs: count a PR's
cumulative unlanded laps across all heads (host-API merge truth via
:func:`~maestro.remediation_tracker.measure_unlanded_remediations`), deliver
**exactly one** merge-or-close handoff when the budget is reached, suppress
further minting across simulated head changes via the durable
``pr_number``-keyed standdown, and clear the standdown on a host-API
merged/closed verdict so a still-open PR can re-knock. ``mem_fs`` only — no real
git or network.
"""

from __future__ import annotations

import json
from pathlib import Path

from maestro.escalation_bridge import (
    JsonEscalationLedger,
    UnlandedCureHandoff,
    clear_standdown,
    deliver_handoff,
    detect_unlanded_cure_handoff,
    has_standdown,
    prune_resolved_standdowns,
)
from maestro.remediation_tracker import measure_unlanded_remediations
from maestro.standdown_validity import StanddownClaim

_DIR = Path("/fake/lap")

#: The default JSON escalation backend the corpus threads (proposal 106) —
#: reads/writes ``_DIR/escalation_handoffs.json``, the file these tests touch.
_BE = JsonEscalationLedger(_DIR)

# The lap budget under test — the value the Tend route reads from
# ``[loop] pr_remediation_lap_budget`` and passes as the detector threshold.
_BUDGET = 3


def _seed_history(
    mem_fs: dict[str, str], records: list[dict[str, object]],
) -> None:
    path = str(_DIR / "task_history.jsonl")
    mem_fs[path] = "".join(json.dumps(r) + "\n" for r in records)


def _remediation(pr: int, *, head_sha: str = "") -> dict[str, object]:
    rec: dict[str, object] = {
        "task_id": f"t-{pr}-{head_sha}",
        "goal": f"Remediate PR #{pr}: fix the thing",
        "outcome": "partial",
    }
    if head_sha:
        rec["pr_head_sha"] = head_sha
    return rec


def _messages(mem_fs: dict[str, str]) -> list[dict[str, object]]:
    raw = mem_fs.get(str(_DIR / "messages.jsonl"), "")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _never_merged(_pr: int) -> bool:
    return False


def _always_merged(_pr: int) -> bool:
    return True


def _route_pass(mem_fs: dict[str, str], pr: int, head: str, now: str) -> bool:
    """Replicate the Tend route's lap-budget decision for one pass.

    Returns ``True`` when minting is suppressed for *pr* this pass (an active
    standdown, or a fresh handoff delivered because the budget was reached),
    ``False`` when the PR would be admitted for remediation. Mirrors
    ``route_feedback``: standdown check first, then the host-truth lap count.
    """
    if has_standdown(_BE, pr):
        return True
    laps = measure_unlanded_remediations(
        _DIR, 50, is_pr_merged=_never_merged,
    ).get(pr, 0)
    if laps >= _BUDGET:
        deliver_handoff(
            _DIR, UnlandedCureHandoff(pr, head, laps), backend=_BE, now=now,
            claim=StanddownClaim.LAP_BUDGET_EXHAUSTED,
        )
        return True
    return False


def test_one_handoff_across_head_changes_then_cleared(
    mem_fs: dict[str, str],
) -> None:
    """Exactly one handoff, suppressed across ≥3 heads, cleared on merged."""
    pr = 2579
    # ``_BUDGET`` unlanded laps already recorded — the PR is at budget.
    _seed_history(
        mem_fs, [_remediation(pr, head_sha=f"h{i}") for i in range(_BUDGET)],
    )

    suppressed = 0
    # Four passes, each a fresh head (≥3 head changes).
    for i, head in enumerate(["h3", "h4", "h5", "h6"]):
        now = f"2026-06-12T0{i}:00:00+00:00"
        if _route_pass(mem_fs, pr, head, now):
            suppressed += 1

    # Exactly one merge-or-close message, despite the head churn.
    msgs = _messages(mem_fs)
    assert len(msgs) == 1
    assert msgs[0]["sender"] == "maestro"
    assert "#2579" in msgs[0]["content"]
    # Every pass suppressed minting (1 delivery + 3 standdown hits).
    assert suppressed == 4
    assert has_standdown(_BE, pr) is True

    # A host-API merged/closed verdict clears the standdown.
    cleared = prune_resolved_standdowns(_BE, _always_merged)
    assert cleared == [pr]
    assert has_standdown(_BE, pr) is False


def test_below_budget_no_handoff_and_admits(mem_fs: dict[str, str]) -> None:
    """Below the budget, no handoff fires and the PR is not suppressed."""
    pr = 100
    _seed_history(
        mem_fs, [_remediation(pr, head_sha=f"h{i}") for i in range(_BUDGET - 1)],
    )
    suppressed = _route_pass(mem_fs, pr, "h9", "2026-06-12T00:00:00+00:00")
    assert suppressed is False
    assert _messages(mem_fs) == []
    assert has_standdown(_BE, pr) is False


def test_reknock_after_clear_when_still_open(mem_fs: dict[str, str]) -> None:
    """The re-knock guard: a cleared standdown on a still-open, over-budget PR
    can earn a fresh knock (host open + no standdown)."""
    pr = 2579
    _seed_history(
        mem_fs, [_remediation(pr, head_sha="h0") for _ in range(_BUDGET)],
    )
    # First knock + standdown.
    first = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=_BUDGET, is_pr_merged=_never_merged,
    )
    assert first is not None
    assert first.pr_number == pr
    assert deliver_handoff(_DIR, first, backend=_BE, now="2026-06-12T00:00:00+00:00", claim=StanddownClaim.LAP_BUDGET_EXHAUSTED) is True
    # The standdown suppresses re-detection across heads.
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=_BUDGET, is_pr_merged=_never_merged,
        )
        is None
    )
    # Clear it (e.g. a transient close), but the PR is still open + over budget.
    assert clear_standdown(_BE, pr) is True
    # Re-knock is now possible: no standdown, host reports open.
    again = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=_BUDGET, is_pr_merged=_never_merged,
    )
    assert again is not None
    assert again.pr_number == pr


def test_merged_pr_never_knocks(mem_fs: dict[str, str]) -> None:
    """A PR the host reports merged is never handed off, even over budget."""
    pr = 2579
    _seed_history(
        mem_fs,
        [_remediation(pr, head_sha=f"h{i}") for i in range(_BUDGET + 2)],
    )
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=_BUDGET, is_pr_merged=_always_merged,
        )
        is None
    )
    assert _messages(mem_fs) == []
    assert has_standdown(_BE, pr) is False

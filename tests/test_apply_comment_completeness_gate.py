"""Unit test for ``loop.remediation_stamps.apply_comment_completeness_gate``.

The stage is the verbatim lift of ``run``'s ``# 4d.`` comment-completeness gate
(proposal 106): when a PR remediation would otherwise stamp ``success`` /
``partial`` it re-reads the open PR's outstanding review items — inline threads
lacking a terminal reply (``_eligible_bug_comments``) and body-overflow findings
lacking an acknowledgement (``_fetch_eligible_overflow_findings``) — records the
combined count in ``self._last_remediation_items_remaining`` and forces
``partial`` with a count-bearing note when any remain, skipping a settled PR.

These tests call the stage directly, agent-first-positional, on a bare
``make_agent`` agent (no ``agent.run()``, no SDK patching) and pin every arm so
it holds 100 % coverage on its own; the full-``run`` regression harness
(``tests/test_pr_remediation_lap.py`` "Comment-completeness gate (step 4d)" tests)
exercises the same block end-to-end and must pass unchanged.
``is_pr_settled`` and the host read methods are
MagicMocks / mem_fs-backed conftest stubs, so nothing shells out.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from maestro.evolution_tend import _OVERFLOW_SENTINEL_CLOSE, _OVERFLOW_SENTINEL_OPEN
from maestro.loop.remediation_stamps import apply_comment_completeness_gate
from maestro.pr_facts import MergeState, MergeTruth
from maestro.vcs import VCSError

_PR = 4242
_AWAITING_NOTE = "Resolved on branch — awaiting tend push."


def _bug_thread_node(comment_id: int, path: str = "a.py") -> dict[str, object]:
    """One raw GraphQL review-thread node for an unresolved bug finding.

    Matches the shape ``_eligible_bug_comments`` selects: unresolved,
    non-outdated, and a leading bug glyph in the comment body.
    """
    return {
        "id": f"PRRT_{comment_id}",
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {
                    "databaseId": comment_id,
                    "path": path,
                    "body": "🐛 **Off-by-one in loop**\n\nDetail.",
                },
            ],
        },
    }


def _overflow_review_body(*lines: str) -> str:
    """An AI review body carrying a sentinel-wrapped overflow block."""
    block = "\n".join(lines)
    return (
        "Summary.\n\n"
        f"{_OVERFLOW_SENTINEL_OPEN}\n{block}\n{_OVERFLOW_SENTINEL_CLOSE}\n"
    )


def _settled(*, settled: bool) -> MergeTruth:
    """Build a ``MergeTruth`` whose ``.settled`` drives the skip arm."""
    state: MergeState = "MERGED" if settled else "OPEN"
    return MergeTruth(
        pr_number=_PR,
        settled=settled,
        state=state,
        branch_landed=settled,
        source="host_api",
        observed_at=0.0,
    )


def test_no_pr_branch_returns_pair_unchanged(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Guard arm A: ``pr_branch is None`` short-circuits — the pair returns
    unchanged, the count stays ``None``, and the merge oracle is not touched."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    outcome, note = apply_comment_completeness_gate(
        agent,
        pr_branch=None,
        remediation_pr_num=_PR,
        outcome="success",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "success"
    assert note == _AWAITING_NOTE
    assert agent._last_remediation_items_remaining is None
    agent.is_pr_settled.assert_not_called()


def test_no_pr_number_returns_pair_unchanged(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Guard arm B: ``remediation_pr_num is None`` short-circuits the same way."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    outcome, note = apply_comment_completeness_gate(
        agent,
        pr_branch="maestro/evolve/ep1",
        remediation_pr_num=None,
        outcome="success",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "success"
    assert note == _AWAITING_NOTE
    assert agent._last_remediation_items_remaining is None
    agent.is_pr_settled.assert_not_called()


def test_failure_outcome_returns_pair_unchanged(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Guard arm C: ``outcome == "failure"`` fails the
    ``outcome in ("success", "partial")`` clause, so the gate short-circuits
    without consulting merge state."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock()  # type: ignore[method-assign]

    outcome, note = apply_comment_completeness_gate(
        agent,
        pr_branch="maestro/evolve/ep1",
        remediation_pr_num=_PR,
        outcome="failure",
        remediation_note=None,
    )

    assert outcome == "failure"
    assert note is None
    assert agent._last_remediation_items_remaining is None
    agent.is_pr_settled.assert_not_called()


def test_settled_pr_skips_reread(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A settled (merged/closed) PR skips the re-read entirely — a stand-down
    there is legitimate — so the count stays ``None`` and the thread fetch is
    never called."""
    agent = make_agent()
    agent.is_pr_settled = MagicMock(  # type: ignore[method-assign]
        return_value=_settled(settled=True),
    )
    threads_spy = MagicMock(return_value=[_bug_thread_node(100)])
    agent.evolution.code_host.get_pr_review_threads = threads_spy  # type: ignore[attr-defined]

    outcome, note = apply_comment_completeness_gate(
        agent,
        pr_branch="maestro/evolve/ep1",
        remediation_pr_num=_PR,
        outcome="success",
        remediation_note=None,
    )

    assert outcome == "success"
    assert note is None
    assert agent._last_remediation_items_remaining is None
    threads_spy.assert_not_called()


def test_open_pr_with_no_outstanding_items_records_zero(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """An unsettled PR with no eligible threads or overflow findings (the
    ``make_agent`` defaults) records a zero remaining count and leaves the
    outcome/note untouched."""
    agent = make_agent()
    # ``is_pr_settled`` defaults to an unsettled OPEN MergeTruth (conftest);
    # the host reads default to ``[]``.

    outcome, note = apply_comment_completeness_gate(
        agent,
        pr_branch="maestro/evolve/ep1",
        remediation_pr_num=_PR,
        outcome="success",
        remediation_note=None,
    )

    assert outcome == "success"
    assert note is None
    assert agent._last_remediation_items_remaining == 0


def test_outstanding_threads_only_force_partial(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """One eligible inline thread forces ``partial`` and authors a fresh note
    naming the thread count; the remaining count is that thread count."""
    agent = make_agent()
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[_bug_thread_node(100)],
    )

    outcome, note = apply_comment_completeness_gate(
        agent,
        pr_branch="maestro/evolve/ep1",
        remediation_pr_num=_PR,
        outcome="success",
        remediation_note=None,
    )

    assert outcome == "partial"
    assert agent._last_remediation_items_remaining == 1
    assert note is not None
    assert "1 inline review thread(s) still lack a terminal reply marker" in note
    assert "overflow finding(s)" not in note
    assert note.startswith(f"Resolved partially — PR #{_PR}: ")


def test_outstanding_overflow_only_force_partial(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """One unacknowledged overflow finding (no ack comment) forces ``partial``
    and authors a note naming the overflow count only."""
    agent = make_agent()
    agent.evolution.code_host.get_pr_review_bodies = MagicMock(  # type: ignore[attr-defined]
        return_value=[
            _overflow_review_body(
                "- of-aaaaaaaa **a.py:0** [bug] — Null deref: boom",
            ),
        ],
    )

    outcome, note = apply_comment_completeness_gate(
        agent,
        pr_branch="maestro/evolve/ep1",
        remediation_pr_num=_PR,
        outcome="success",
        remediation_note=None,
    )

    assert outcome == "partial"
    assert agent._last_remediation_items_remaining == 1
    assert note is not None
    assert "1 overflow finding(s) still lack an acknowledgement marker" in note
    assert "inline review thread(s)" not in note


def test_threads_and_overflow_combine_into_one_note(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When both an inline thread and an overflow finding remain, the gate sums
    them into the remaining count and joins the two clauses with `` and ``."""
    agent = make_agent()
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[_bug_thread_node(100)],
    )
    agent.evolution.code_host.get_pr_review_bodies = MagicMock(  # type: ignore[attr-defined]
        return_value=[
            _overflow_review_body(
                "- of-aaaaaaaa **a.py:0** [bug] — Null deref: boom",
            ),
        ],
    )

    outcome, note = apply_comment_completeness_gate(
        agent,
        pr_branch="maestro/evolve/ep1",
        remediation_pr_num=_PR,
        outcome="success",
        remediation_note=None,
    )

    assert outcome == "partial"
    assert agent._last_remediation_items_remaining == 2
    assert note is not None
    assert "inline review thread(s) still lack a terminal reply marker" in note
    assert "overflow finding(s) still lack an acknowledgement marker" in note
    assert " and " in note


def test_existing_note_is_augmented_not_clobbered(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """When a note is already set on entry (e.g. an upstream awaiting-push
    marker), the items note is appended to it rather than replacing it — the
    downstream costume-signature/audit reads on that marker text survive."""
    agent = make_agent()
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[_bug_thread_node(100)],
    )

    outcome, note = apply_comment_completeness_gate(
        agent,
        pr_branch="maestro/evolve/ep1",
        remediation_pr_num=_PR,
        outcome="partial",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "partial"
    assert agent._last_remediation_items_remaining == 1
    assert note is not None
    assert note.startswith(f"{_AWAITING_NOTE} PR #{_PR}: ")
    assert "1 inline review thread(s) still lack a terminal reply marker" in note


def test_failed_thread_scan_leaves_items_unknown(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """A failed thread read is not "zero outstanding" (#4065): the count stays
    ``None`` (no progress credit) and the lap cannot read as complete."""
    agent = make_agent()
    agent.evolution.code_host.get_pr_review_threads_strict = MagicMock(  # type: ignore[attr-defined]
        side_effect=VCSError("gh timed out"),
    )
    bodies_spy = MagicMock(return_value=[])
    agent.evolution.code_host.get_pr_review_bodies_strict = bodies_spy  # type: ignore[attr-defined]

    outcome, note = apply_comment_completeness_gate(
        agent,
        pr_branch="maestro/evolve/ep1",
        remediation_pr_num=_PR,
        outcome="success",
        remediation_note=None,
    )

    assert outcome == "partial"
    assert agent._last_remediation_items_remaining is None
    assert note == (
        f"Resolved partially — PR #{_PR}: outstanding-item scan did not "
        "complete (host read failed); completeness unverified."
    )
    # The verdict is already decided: no overflow read against a failing host.
    bodies_spy.assert_not_called()


def test_failed_overflow_scan_leaves_items_unknown(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """Threads read clean but the review-bodies read fails: the overflow scan
    reports ``scan_ok=False`` and the gate keeps the count unknown, augmenting
    (not clobbering) an existing awaiting-push note."""
    agent = make_agent()
    agent.evolution.code_host.get_pr_review_bodies_strict = MagicMock(  # type: ignore[attr-defined]
        side_effect=VCSError("secondary rate limit"),
    )

    outcome, note = apply_comment_completeness_gate(
        agent,
        pr_branch="maestro/evolve/ep1",
        remediation_pr_num=_PR,
        outcome="success",
        remediation_note=_AWAITING_NOTE,
    )

    assert outcome == "partial"
    assert agent._last_remediation_items_remaining is None
    assert note is not None
    assert note.startswith(_AWAITING_NOTE)
    assert "outstanding-item scan did not complete" in note

"""Tests for the extracted ``loop.remediation_stamps.deliver_review_thread_replies``.

This is ``run``'s ``# 4c-ii.`` block lifted into a named, independently-testable
module-level stage (proposal 106 decomposition): the loop's sole trusted deliverer of
per-thread terminal review-thread replies (#2817). A remediation task RECORDS
each terminal-marker reply into ``thread-replies.jsonl`` under its artifacts dir
rather than posting it; this stage reads that manifest, posts each reply via
``reply_to_review_comment``, collects the ids whose POST returned without
raising, and stamps exactly those serviced under the PR's standdown. Only the
confirmed ids flow out as the ``delivered_ids`` set that
``task_delivery.resolve_addressed_review_threads`` consumes.

The extraction is behaviour-preserving, so these tests pin every arm of the
stage's own contract — each early-return condition (``pr_branch`` ``None``, a
branch mismatch, ``remediation_pr_num`` ``None``, a non-deliverable outcome), an
empty manifest, a successful multi-reply delivery, a selective host failure, the
all-failed case, and the no-standdown no-op — the way
``tests/test_resolve_addressed_review_threads.py`` pins its sibling. Like that
file this one takes ``mem_fs`` and calls the stage directly on a bare
``make_agent()`` agent; no ``agent.run``, no SDK patching. Two wiring details
matter (proposal 106): ``make_agent`` does **not** stub
``code_host.reply_to_review_comment``, so every test reaching the fire arm
assigns a ``MagicMock`` (else the real subprocess host would shell out to
``gh``); and ``agent.escalation_standdowns`` is a **real** ledger over
``mem_fs``, so the serviced-stamp arms pre-seed a standdown record and assert on
the persisted ledger.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, call

from maestro.agent import MaestroAgent
from maestro.loop.pr_remediation import _REPLY_MANIFEST_FILENAME
from maestro.loop.remediation_stamps import deliver_review_thread_replies
from maestro.vcs import VCSError

#: The PR every fire arm delivers replies on, and its branch (the deliverable
#: guard requires ``branch_name == pr_branch``).
_PR = 91
_BRANCH = "maestro/evolve/ep91"
#: A task's artifacts dir the manifest is planted under; the method reads
#: ``<artifacts_path>/thread-replies.jsonl`` from it.
_ARTIFACTS = Path("/fake/agent/artifacts/task-x")


def _seed_manifest(
    mem_fs: dict[str, str], replies: list[tuple[int, str]],
) -> None:
    """Plant a ``thread-replies.jsonl`` manifest for *replies* into mem_fs."""
    mem_fs[str(_ARTIFACTS / _REPLY_MANIFEST_FILENAME)] = "".join(
        json.dumps({"comment_id": cid, "body": body}) + "\n"
        for cid, body in replies
    )


def _ledger_path(agent: MaestroAgent) -> str:
    """The escalation-standdown ledger file backing ``escalation_standdowns``."""
    return str(agent.config.memory_dir / "escalation_handoffs.json")


def _seed_standdown(
    mem_fs: dict[str, str],
    agent: MaestroAgent,
    serviced: list[int] | None = None,
) -> None:
    """Plant a live standdown for ``_PR`` so the serviced stamp records."""
    mem_fs[_ledger_path(agent)] = json.dumps({
        str(_PR): {
            "head_sha": "h",
            "delivered_at": "2026-01-01T00:00:00+00:00",
            "serviced_comment_ids": serviced or [],
        },
    })


def _serviced(mem_fs: dict[str, str], agent: MaestroAgent) -> list[int]:
    """The persisted serviced set for ``_PR`` (``[]`` when no ledger written)."""
    raw = mem_fs.get(_ledger_path(agent), "")
    return json.loads(raw)[str(_PR)]["serviced_comment_ids"] if raw else []


def test_no_pr_branch_returns_empty(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(a) ``pr_branch is None`` (an owned task branch, not a remediation lap) →
    the guard's first arm returns an empty set with no host I/O."""
    agent = make_agent()
    agent.evolution.code_host.reply_to_review_comment = MagicMock()  # type: ignore[attr-defined]
    _seed_manifest(mem_fs, [(100, "reply 100")])

    result = deliver_review_thread_replies(
        agent,
        branch_name=_BRANCH,
        pr_branch=None,
        remediation_pr_num=_PR,
        outcome="success",
        artifacts_path=_ARTIFACTS,
    )

    assert result == set()
    agent.evolution.code_host.reply_to_review_comment.assert_not_called()


def test_branch_mismatch_returns_empty(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(b) The lap ran on a different branch than the PR's → not the PR branch
    we would deliver on, so the guard short-circuits."""
    agent = make_agent()
    agent.evolution.code_host.reply_to_review_comment = MagicMock()  # type: ignore[attr-defined]
    _seed_manifest(mem_fs, [(100, "reply 100")])

    result = deliver_review_thread_replies(
        agent,
        branch_name="maestro/task/other",
        pr_branch=_BRANCH,
        remediation_pr_num=_PR,
        outcome="success",
        artifacts_path=_ARTIFACTS,
    )

    assert result == set()
    agent.evolution.code_host.reply_to_review_comment.assert_not_called()


def test_no_remediation_pr_returns_empty(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(c) ``remediation_pr_num is None`` (no parsed PR number) → nothing to
    deliver a reply against, so the guard short-circuits."""
    agent = make_agent()
    agent.evolution.code_host.reply_to_review_comment = MagicMock()  # type: ignore[attr-defined]
    _seed_manifest(mem_fs, [(100, "reply 100")])

    result = deliver_review_thread_replies(
        agent,
        branch_name=_BRANCH,
        pr_branch=_BRANCH,
        remediation_pr_num=None,
        outcome="success",
        artifacts_path=_ARTIFACTS,
    )

    assert result == set()
    agent.evolution.code_host.reply_to_review_comment.assert_not_called()


def test_non_deliverable_outcome_returns_empty(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(d) A ``failure`` outcome is not deliverable (only ``success``/``partial``
    deliver), so the guard short-circuits before any reply posts."""
    agent = make_agent()
    agent.evolution.code_host.reply_to_review_comment = MagicMock()  # type: ignore[attr-defined]
    _seed_manifest(mem_fs, [(100, "reply 100")])

    result = deliver_review_thread_replies(
        agent,
        branch_name=_BRANCH,
        pr_branch=_BRANCH,
        remediation_pr_num=_PR,
        outcome="failure",
        artifacts_path=_ARTIFACTS,
    )

    assert result == set()
    agent.evolution.code_host.reply_to_review_comment.assert_not_called()


def test_empty_manifest_delivers_nothing(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(e) The guard passes but the manifest has no lines → the loop never runs,
    nothing posts, and the serviced-stamp call is skipped (empty set)."""
    agent = make_agent()
    agent.evolution.code_host.reply_to_review_comment = MagicMock()  # type: ignore[attr-defined]
    _seed_manifest(mem_fs, [])
    _seed_standdown(mem_fs, agent)

    result = deliver_review_thread_replies(
        agent,
        branch_name=_BRANCH,
        pr_branch=_BRANCH,
        remediation_pr_num=_PR,
        outcome="success",
        artifacts_path=_ARTIFACTS,
    )

    assert result == set()
    agent.evolution.code_host.reply_to_review_comment.assert_not_called()
    assert _serviced(mem_fs, agent) == []


def test_delivers_all_replies_and_stamps_serviced(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(f) The fire arm: every manifest reply posts to its thread and exactly the
    delivered ids are returned and stamped serviced under the PR's standdown."""
    agent = make_agent()
    agent.evolution.code_host.reply_to_review_comment = MagicMock()  # type: ignore[attr-defined]
    _seed_manifest(mem_fs, [(100, "reply 100"), (200, "reply 200")])
    _seed_standdown(mem_fs, agent)

    result = deliver_review_thread_replies(
        agent,
        branch_name=_BRANCH,
        pr_branch=_BRANCH,
        remediation_pr_num=_PR,
        outcome="partial",
        artifacts_path=_ARTIFACTS,
    )

    assert result == {100, 200}
    assert agent.evolution.code_host.reply_to_review_comment.call_args_list == [
        call(_PR, 100, "reply 100"),
        call(_PR, 200, "reply 200"),
    ]
    assert _serviced(mem_fs, agent) == [100, 200]


def test_selective_host_failure_excludes_id(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str], caplog,
) -> None:
    """(g) One raising reply is best-effort: the raising id is left out of BOTH
    the returned set and the serviced stamp while the other replies still post,
    and the failure logs a warning (so the thread is retried next lap)."""
    agent = make_agent()

    def _reply(_pr: int, comment_id: int, _body: str) -> None:
        if comment_id == 300:
            raise VCSError("host down")

    agent.evolution.code_host.reply_to_review_comment = MagicMock(  # type: ignore[attr-defined]
        side_effect=_reply,
    )
    _seed_manifest(
        mem_fs, [(100, "reply 100"), (200, "reply 200"), (300, "reply 300")],
    )
    _seed_standdown(mem_fs, agent)

    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = deliver_review_thread_replies(
            agent,
            branch_name=_BRANCH,
            pr_branch=_BRANCH,
            remediation_pr_num=_PR,
            outcome="success",
            artifacts_path=_ARTIFACTS,
        )

    assert result == {100, 200}
    assert agent.evolution.code_host.reply_to_review_comment.call_count == 3
    assert _serviced(mem_fs, agent) == [100, 200]
    assert any(
        "Failed to deliver review-thread reply to comment 300" in r.message
        for r in caplog.records
    )


def test_all_failed_stamps_nothing(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str], caplog,
) -> None:
    """(h) Every reply raises → the returned set is empty, both attempts log a
    warning, and the ``if delivered_ids`` guard skips the serviced stamp so the
    standdown's serviced set stays empty."""
    agent = make_agent()
    agent.evolution.code_host.reply_to_review_comment = MagicMock(  # type: ignore[attr-defined]
        side_effect=VCSError("host down"),
    )
    _seed_manifest(mem_fs, [(100, "reply 100"), (200, "reply 200")])
    _seed_standdown(mem_fs, agent)

    with caplog.at_level(logging.WARNING, logger="maestro"):
        result = deliver_review_thread_replies(
            agent,
            branch_name=_BRANCH,
            pr_branch=_BRANCH,
            remediation_pr_num=_PR,
            outcome="success",
            artifacts_path=_ARTIFACTS,
        )

    assert result == set()
    assert agent.evolution.code_host.reply_to_review_comment.call_count == 2
    assert _serviced(mem_fs, agent) == []
    assert sum(
        "Failed to deliver review-thread reply" in r.message
        for r in caplog.records
    ) == 2


def test_no_standdown_delivers_but_stamps_nothing(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(i) With no standdown for the PR the reply still posts and its id is
    returned, but ``mark_comment_ids_serviced`` legitimately no-ops — the ledger
    stays absent/empty."""
    agent = make_agent()
    agent.evolution.code_host.reply_to_review_comment = MagicMock()  # type: ignore[attr-defined]
    _seed_manifest(mem_fs, [(100, "reply 100")])

    result = deliver_review_thread_replies(
        agent,
        branch_name=_BRANCH,
        pr_branch=_BRANCH,
        remediation_pr_num=_PR,
        outcome="success",
        artifacts_path=_ARTIFACTS,
    )

    assert result == {100}
    agent.evolution.code_host.reply_to_review_comment.assert_called_once_with(
        _PR, 100, "reply 100",
    )
    raw = mem_fs.get(_ledger_path(agent), "")
    assert (json.loads(raw) if raw else {}) == {}

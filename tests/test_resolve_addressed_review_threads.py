"""Tests for the extracted
``loop.task_delivery.resolve_addressed_review_threads``.

This is ``run``'s ``# 5-push-ii.`` block lifted into a named, independently-
testable method (proposal 106 decomposition): the addressed-thread
auto-resolution step (proposal 091 step 5). After a remediation lap confirms
BOTH deliveries — each terminal reply posted (``delivered_ids``) and the fix
pushed (``push_delivered``) — it resolves exactly the AI-finding threads it
addressed, behind the ``resolve_addressed_threads_enabled`` flag, on a
best-effort per-thread basis (a host error logs and the lap continues), never
touching a human-participated thread.

The extraction is behaviour-preserving, so these tests pin every arm of the
method's own gate — the flag-off no-op, empty ``delivered_ids``, ``push_delivered``
``False``/``None``, ``remediation_pr_num`` ``None``, the resolvable-thread fire arm,
a human-touched thread left untouched, and the best-effort host-error path — the
way ``tests/test_apply_mergeable_flip_actuator.py`` pins its sibling. Like that
file this one takes ``mem_fs`` and calls the method directly on a bare
``make_agent()`` agent with the two ``code_host`` methods stubbed; no ``agent.run``,
no SDK patching.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from maestro.agent import MaestroAgent
from maestro.loop.task_delivery import resolve_addressed_review_threads
from maestro.vcs import VCSError

#: The PR every fire arm resolves threads on.
_PR = 91
#: A terminal-marker reply body — the ``— patched`` suffix is one of
#: ``_resolvable_thread_ids``' recognised terminal markers, so a thread carrying
#: only this reply qualifies for auto-resolution.
_TERMINAL_REPLY = "Fixed locally with implementation-plan context. — patched"


def _bug_thread(
    comment_id: int, replies: list[str] | None = None,
) -> dict[str, object]:
    """A 🐛 bug thread node with an AI-finding root and optional replies.

    Mirrors ``tests/test_pr_remediation_lap.py``'s ``_bug_thread_node_with_reply`` shape: an
    unresolved thread whose root matches the AI-bug pattern, followed by one
    non-root comment per entry in *replies*.
    """
    nodes: list[dict[str, object]] = [
        {
            "databaseId": comment_id,
            "path": "a.py",
            "body": "🐛 **Off-by-one in loop**\n\nDetail.",
        },
    ]
    for offset, reply in enumerate(replies or [], start=1):
        nodes.append(
            {"databaseId": comment_id + offset, "path": "a.py", "body": reply},
        )
    return {
        "id": f"PRRT_{comment_id}",
        "isResolved": False,
        "isOutdated": False,
        "comments": {"nodes": nodes},
    }


def _stub_host(agent: MaestroAgent) -> None:
    """Stub the two ``code_host`` methods the block reaches with recorders."""
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[_bug_thread(100, replies=[_TERMINAL_REPLY])],
    )
    agent.evolution.code_host.resolve_review_thread = MagicMock()  # type: ignore[attr-defined]


def test_flag_off_is_a_noop(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(a) ``resolve_addressed_threads_enabled`` off → the guard's first arm.

    The rollback flag gates the whole block, so neither ``code_host`` method is
    consulted (no host I/O) even with both deliveries confirmed.
    """
    agent = make_agent()
    agent.config.resolve_addressed_threads_enabled = False
    _stub_host(agent)

    resolve_addressed_review_threads(
        agent,
        delivered_ids={100}, push_delivered=True, remediation_pr_num=_PR,
    )

    agent.evolution.code_host.get_pr_review_threads.assert_not_called()
    agent.evolution.code_host.resolve_review_thread.assert_not_called()


def test_empty_delivered_ids_is_a_noop(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(b) No reply was delivered this lap → the empty-set guard short-circuits."""
    agent = make_agent()
    _stub_host(agent)

    resolve_addressed_review_threads(
        agent,
        delivered_ids=set(), push_delivered=True, remediation_pr_num=_PR,
    )

    agent.evolution.code_host.get_pr_review_threads.assert_not_called()
    agent.evolution.code_host.resolve_review_thread.assert_not_called()


def test_push_failed_is_a_noop(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(c) ``push_delivered`` ``False`` → the fix never reached origin, so the
    thread stays open for the next lap and no host read fires."""
    agent = make_agent()
    _stub_host(agent)

    resolve_addressed_review_threads(
        agent,
        delivered_ids={100}, push_delivered=False, remediation_pr_num=_PR,
    )

    agent.evolution.code_host.get_pr_review_threads.assert_not_called()
    agent.evolution.code_host.resolve_review_thread.assert_not_called()


def test_push_unknown_is_a_noop(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(d) ``push_delivered`` ``None`` (never attempted) is falsy → also a no-op."""
    agent = make_agent()
    _stub_host(agent)

    resolve_addressed_review_threads(
        agent,
        delivered_ids={100}, push_delivered=None, remediation_pr_num=_PR,
    )

    agent.evolution.code_host.get_pr_review_threads.assert_not_called()
    agent.evolution.code_host.resolve_review_thread.assert_not_called()


def test_no_remediation_pr_is_a_noop(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(e) ``remediation_pr_num`` ``None`` → no PR to fetch threads for, no-op."""
    agent = make_agent()
    _stub_host(agent)

    resolve_addressed_review_threads(
        agent,
        delivered_ids={100}, push_delivered=True, remediation_pr_num=None,
    )

    agent.evolution.code_host.get_pr_review_threads.assert_not_called()
    agent.evolution.code_host.resolve_review_thread.assert_not_called()


def test_resolves_addressed_thread_exactly_once(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(f) The fire arm: both deliveries confirmed and the thread qualifies, so
    its node is re-fetched for the cited PR and resolved exactly once."""
    agent = make_agent()
    _stub_host(agent)

    resolve_addressed_review_threads(
        agent,
        delivered_ids={100}, push_delivered=True, remediation_pr_num=_PR,
    )

    agent.evolution.code_host.get_pr_review_threads.assert_called_once_with(_PR)
    agent.evolution.code_host.resolve_review_thread.assert_called_once_with(
        "PRRT_100",
    )


def test_human_touched_thread_is_left_unresolved(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str],
) -> None:
    """(g) A delivered AI thread carrying a marker-less (human) reply is refused
    by ``_resolvable_thread_ids`` — the node is re-fetched but nothing resolves."""
    agent = make_agent()
    _stub_host(agent)
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[
            _bug_thread(100, replies=[_TERMINAL_REPLY, "Thanks, but what about X?"]),
        ],
    )

    resolve_addressed_review_threads(
        agent,
        delivered_ids={100}, push_delivered=True, remediation_pr_num=_PR,
    )

    agent.evolution.code_host.get_pr_review_threads.assert_called_once_with(_PR)
    agent.evolution.code_host.resolve_review_thread.assert_not_called()


def test_host_error_is_logged_and_lap_continues(  # type: ignore[no-untyped-def]
    make_agent, mem_fs: dict[str, str], caplog,
) -> None:
    """(h) A raising ``resolve_review_thread`` is best-effort per thread: the
    first thread's error logs a warning and the loop still resolves the next."""
    agent = make_agent()
    agent.evolution.code_host.get_pr_review_threads = MagicMock(  # type: ignore[attr-defined]
        return_value=[
            _bug_thread(100, replies=[_TERMINAL_REPLY]),
            _bug_thread(200, replies=[_TERMINAL_REPLY]),
        ],
    )
    agent.evolution.code_host.resolve_review_thread = MagicMock(  # type: ignore[attr-defined]
        side_effect=[VCSError("resolve failed"), None],
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        resolve_addressed_review_threads(
            agent,
            delivered_ids={100, 200},
            push_delivered=True,
            remediation_pr_num=_PR,
        )

    assert agent.evolution.code_host.resolve_review_thread.call_count == 2
    assert any(
        "Failed to resolve review thread PRRT_100" in r.message
        for r in caplog.records
    )

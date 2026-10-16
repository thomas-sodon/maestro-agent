"""Tests for tend-phase routing of unresolved AI bug review comments.

The tend phase scans each open evolution PR for unresolved 🐛 bug findings
left by ``maestro-review`` and routes a comment-remediation task
into ``pending_pr_requests`` — even when the PR is otherwise
mergeable (the reviewer's advisory check is excluded from the CI rollup, so a
PR can look CLEAN while a bug sits open).

Covered here: the pure :func:`_eligible_bug_comments` classifier, the
``evolution_tend._route_bug_comments`` helper, the ``tend_evolution_prs``
integration (clean PR routes; flag-off skips the scan), and a pin of the
reviewer's 🐛 bold-title comment format that the locally-defined bug regex
must keep matching (``maestro`` may not import ``maestro_review``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from maestro import evolution_tend
from maestro.evolution_tend import (
    _AI_BUG_COMMENT_RE,
    _BUG_COMMENT_TERMINAL_MARKERS,
    _eligible_bug_comments,
)
from maestro.vcs import VCSError
from tests.support.merge_truth import merge_oracle
from tests.test_evolution_tend import _make_engine, _plan, _pr_state

_BUG = "\U0001f41b"  # 🐛


def _thread(
    *,
    comment_id: int = 100,
    path: str = "maestro/agent.py",
    body: str | None = None,
    is_resolved: bool = False,
    is_outdated: bool = False,
    replies: list[str] | None = None,
) -> dict[str, Any]:
    """Build one raw GraphQL review-thread node."""
    root_body = body if body is not None else f"{_BUG} **Off-by-one in loop**\n\nDetail."
    nodes = [{"databaseId": comment_id, "path": path, "body": root_body}]
    for reply in replies or []:
        nodes.append({"databaseId": comment_id + 1, "path": path, "body": reply})
    return {
        "id": f"PRRT_{comment_id}",
        "isResolved": is_resolved,
        "isOutdated": is_outdated,
        "comments": {"nodes": nodes},
    }


# ---------------------------------------------------------------------------
# _eligible_bug_comments classifier
# ---------------------------------------------------------------------------


def test_eligible_bug_comment_extracted() -> None:
    out = _eligible_bug_comments([_thread(comment_id=7, path="a.py")])
    assert out == [(7, "a.py", "Off-by-one in loop", "bug")]


def test_resolved_thread_skipped() -> None:
    assert _eligible_bug_comments([_thread(is_resolved=True)]) == []


def test_outdated_thread_skipped() -> None:
    assert _eligible_bug_comments([_thread(is_outdated=True)]) == []


def test_reply_needed_includes_outdated() -> None:
    """``_reply_needed_threads`` keeps an outdated, un-replied thread that
    ``_eligible_bug_comments`` drops, but still excludes a terminally-answered
    thread and a resolved thread (#2893)."""
    from maestro.evolution_tend import _reply_needed_threads

    outdated = _thread(comment_id=20, path="a.py", is_outdated=True)
    answered = _thread(
        comment_id=21, path="b.py", is_outdated=True,
        replies=["Could not prove this defect — no repro found."],
    )
    resolved = _thread(comment_id=22, path="c.py", is_resolved=True)
    nodes = [outdated, answered, resolved]

    # Fix routing drops all three (outdated or resolved).
    assert _eligible_bug_comments(nodes) == []
    # Reply-completion keeps only the outdated + un-replied + un-answered thread;
    # the terminally-answered and resolved threads stay excluded.
    assert _reply_needed_threads(nodes) == [
        (20, "a.py", "Off-by-one in loop", "bug"),
    ]


def test_suggestion_and_nit_emoji_extracted() -> None:
    """💡 suggestion and ✏️ nit threads are now routed alongside 🐛 bugs."""
    suggestion = _thread(
        comment_id=8, path="a.py",
        body="\U0001f4a1 **Consider a helper**\n\nSuggestion.",
    )
    nit = _thread(comment_id=9, path="b.py", body="✏️ **Typo**\n\nFix.")
    assert _eligible_bug_comments([suggestion, nit]) == [
        (8, "a.py", "Consider a helper", "suggestion"),
        (9, "b.py", "Typo", "nit"),
    ]


def test_human_thread_extracted() -> None:
    """A non-AI, non-bot root comment is routed as a human thread."""
    human = _thread(
        comment_id=10, path="a.py",
        body="Please rename this for clarity\n\nmore detail",
    )
    human["comments"]["nodes"][0]["author"] = {"login": "thomas-sodon"}
    assert _eligible_bug_comments([human]) == [
        (10, "a.py", "Please rename this for clarity", "human"),
    ]


def test_bot_authored_non_ai_thread_skipped() -> None:
    """A non-AI root authored by a bot is out of scope — not a human thread."""
    bot = _thread(comment_id=11, body="CI status update, no emoji")
    bot["comments"]["nodes"][0]["author"] = {"login": "dependabot[bot]"}
    assert _eligible_bug_comments([bot]) == []


def test_human_thread_missing_author_treated_as_human() -> None:
    """A non-AI root with no author info has an empty login, which is not a
    bot suffix, so it is treated as a human thread."""
    plain = _thread(comment_id=12, path="c.py", body="just a human comment")
    # No author key at all → login defaults to "" → not a bot → human.
    assert _eligible_bug_comments([plain]) == [
        (12, "c.py", "just a human comment", "human"),
    ]


def test_terminal_marker_reply_skipped() -> None:
    for marker in _BUG_COMMENT_TERMINAL_MARKERS:
        thread = _thread(replies=[f"{marker} — done."])
        assert _eligible_bug_comments([thread]) == [], marker


def test_non_bug_terminal_markers_skip_suggestion() -> None:
    """A suggestion thread carrying a Declined/Answered reply is terminal."""
    from maestro.evolution_tend import _NON_BUG_COMMENT_TERMINAL_MARKERS

    for marker in _NON_BUG_COMMENT_TERMINAL_MARKERS:
        thread = _thread(
            body="\U0001f4a1 **Consider a helper**\n\nSuggestion.",
            replies=[f"{marker} — done."],
        )
        assert _eligible_bug_comments([thread]) == [], marker


def test_bug_thread_ignores_non_bug_terminal_marker() -> None:
    """A 🐛 thread is only terminated by a bug marker — a Declined reply (a
    non-bug terminal) does not close it, so it stays eligible."""
    thread = _thread(
        comment_id=13,
        replies=["Declined with implementation-plan context. — n/a"],
    )
    assert _eligible_bug_comments([thread]) == [
        (13, "maestro/agent.py", "Off-by-one in loop", "bug"),
    ]


def test_historical_terminal_markers_excluded() -> None:
    """Each marker the retired standalone comment processor emitted must still
    terminate a thread, so threads it closed are never re-routed once that
    module is gone.  Pinned to literals (not derived from
    ``_BUG_COMMENT_TERMINAL_MARKERS``) so dropping a marker from the canonical
    list fails this test instead of silently passing."""
    historical = (
        "Could not prove this defect",
        "Could not prove this bug",
        "Fixed automatically",
        "Fixed locally with implementation-plan context.",
        "Tracked in #",
    )
    for marker in historical:
        thread = _thread(replies=[f"{marker} — done."])
        assert _eligible_bug_comments([thread]) == [], marker


def test_non_terminal_reply_still_eligible() -> None:
    thread = _thread(comment_id=9, replies=["Working on a fix"])
    assert _eligible_bug_comments([thread]) == [
        (9, "maestro/agent.py", "Off-by-one in loop", "bug"),
    ]


def test_non_int_database_id_skipped() -> None:
    thread = _thread()
    thread["comments"]["nodes"][0]["databaseId"] = None
    assert _eligible_bug_comments([thread]) == []


def test_empty_and_non_dict_nodes_skipped() -> None:
    empty = {"id": "x", "isResolved": False, "comments": {"nodes": []}}
    assert _eligible_bug_comments([empty, "not-a-dict"]) == []  # type: ignore[list-item]


def test_missing_comments_key_skipped() -> None:
    assert _eligible_bug_comments([{"id": "x", "isResolved": False}]) == []


def test_multiple_mixed_threads() -> None:
    out = _eligible_bug_comments([
        _thread(comment_id=1, path="a.py"),
        _thread(comment_id=2, is_resolved=True),
        _thread(comment_id=3, path="b.py", replies=["Fixed automatically"]),
        _thread(comment_id=4, path="c.py"),
    ])
    assert out == [
        (1, "a.py", "Off-by-one in loop", "bug"),
        (4, "c.py", "Off-by-one in loop", "bug"),
    ]


# ---------------------------------------------------------------------------
# _resolvable_thread_ids selector (proposal 091)
# ---------------------------------------------------------------------------


_TERMINAL = "Fixed locally with implementation-plan context."


def test_resolvable_happy_path_bug() -> None:
    """A delivered bug thread whose only reply is terminal is resolvable."""
    from maestro.evolution_tend import _resolvable_thread_ids

    thread = _thread(comment_id=100, replies=[f"{_TERMINAL} — patched"])
    assert _resolvable_thread_ids([thread], {100}) == ["PRRT_100"]


def test_resolvable_suggestion_and_nit() -> None:
    """💡 suggestion and ✏️ nit AI threads are resolvable alongside 🐛 bugs."""
    from maestro.evolution_tend import _resolvable_thread_ids

    suggestion = _thread(
        comment_id=8, body="\U0001f4a1 **Consider a helper**\n\nSuggestion.",
        replies=["Answered with implementation-plan context. — done"],
    )
    nit = _thread(
        comment_id=9, body="✏️ **Typo**\n\nFix.",
        replies=[f"{_TERMINAL} — fixed"],
    )
    assert _resolvable_thread_ids([suggestion, nit], {8, 9}) == [
        "PRRT_8", "PRRT_9",
    ]


def test_resolvable_skips_already_resolved() -> None:
    from maestro.evolution_tend import _resolvable_thread_ids

    thread = _thread(
        comment_id=100, is_resolved=True, replies=[f"{_TERMINAL} — patched"],
    )
    assert _resolvable_thread_ids([thread], {100}) == []


def test_resolvable_skips_human_rooted_thread() -> None:
    """A non-AI (human) root is never resolved, even if delivered + terminal."""
    from maestro.evolution_tend import _resolvable_thread_ids

    human = _thread(
        comment_id=100, body="Please rename this\n\ndetail",
        replies=[f"{_TERMINAL} — done"],
    )
    assert _resolvable_thread_ids([human], {100}) == []


def test_resolvable_skips_when_root_not_delivered() -> None:
    """A thread whose root id is not in delivered_ids is skipped."""
    from maestro.evolution_tend import _resolvable_thread_ids

    thread = _thread(comment_id=100, replies=[f"{_TERMINAL} — patched"])
    assert _resolvable_thread_ids([thread], set()) == []
    assert _resolvable_thread_ids([thread], {999}) == []


def test_resolvable_skips_marker_less_human_reply() -> None:
    """A delivered AI thread with a marker-less (human) reply is left untouched."""
    from maestro.evolution_tend import _resolvable_thread_ids

    thread = _thread(
        comment_id=100,
        replies=[f"{_TERMINAL} — patched", "Thanks, but what about X?"],
    )
    assert _resolvable_thread_ids([thread], {100}) == []


def test_resolvable_skips_when_no_reply_present() -> None:
    """A delivered thread the re-fetch shows with no reply yet is not resolved."""
    from maestro.evolution_tend import _resolvable_thread_ids

    thread = _thread(comment_id=100)
    assert _resolvable_thread_ids([thread], {100}) == []


def test_resolvable_skips_non_int_database_id() -> None:
    from maestro.evolution_tend import _resolvable_thread_ids

    thread = _thread(comment_id=100, replies=[f"{_TERMINAL} — patched"])
    thread["comments"]["nodes"][0]["databaseId"] = None
    assert _resolvable_thread_ids([thread], {100}) == []


def test_resolvable_skips_missing_or_empty_id() -> None:
    from maestro.evolution_tend import _resolvable_thread_ids

    thread = _thread(comment_id=100, replies=[f"{_TERMINAL} — patched"])
    thread["id"] = ""
    assert _resolvable_thread_ids([thread], {100}) == []
    del thread["id"]
    assert _resolvable_thread_ids([thread], {100}) == []


def test_resolvable_skips_empty_and_non_dict_nodes() -> None:
    from maestro.evolution_tend import _resolvable_thread_ids

    empty = {"id": "x", "isResolved": False, "comments": {"nodes": []}}
    assert _resolvable_thread_ids(
        [empty, "not-a-dict"], {100},  # type: ignore[list-item]
    ) == []


def test_resolvable_skips_missing_comments_key() -> None:
    from maestro.evolution_tend import _resolvable_thread_ids

    assert _resolvable_thread_ids([{"id": "x", "isResolved": False}], {1}) == []


def test_resolvable_mixed_threads_returns_only_qualifying() -> None:
    from maestro.evolution_tend import _resolvable_thread_ids

    qualifying = _thread(comment_id=1, replies=[f"{_TERMINAL} — patched"])
    resolved = _thread(
        comment_id=2, is_resolved=True, replies=[f"{_TERMINAL} — patched"],
    )
    undelivered = _thread(comment_id=3, replies=[f"{_TERMINAL} — patched"])
    human_touched = _thread(
        comment_id=4, replies=[f"{_TERMINAL} — patched", "one more thing"],
    )
    assert _resolvable_thread_ids(
        [qualifying, resolved, undelivered, human_touched], {1, 2, 4},
    ) == ["PRRT_1"]


# ---------------------------------------------------------------------------
# Reviewer comment-format convention (maestro may not import maestro_review)
# ---------------------------------------------------------------------------


def test_bug_regex_matches_review_emoji() -> None:
    match = _AI_BUG_COMMENT_RE.match("🐛 **A bug title**")
    assert match is not None
    assert match.group(1) == "A bug title"


def test_suggestion_and_nit_regexes_match_reviewer_rendered_bodies() -> None:
    """The locally-pinned suggestion/nit regexes match the reviewer's rendered
    comment bodies (``maestro`` may not import ``maestro_review``)."""
    from maestro.evolution_tend import _AI_NIT_COMMENT_RE, _AI_SUGGESTION_COMMENT_RE
    from maestro_review.review import PRReviewer, ReviewFinding

    suggestion_body = PRReviewer._format_finding_body(
        ReviewFinding(file="a.py", line=1, severity="suggestion",
                      title="Use a helper", description="d", fix_plan="f"),
    )
    nit_body = PRReviewer._format_finding_body(
        ReviewFinding(file="a.py", line=1, severity="nit",
                      title="Trailing space", description="d", fix_plan="f"),
    )
    sug_match = _AI_SUGGESTION_COMMENT_RE.match(suggestion_body)
    assert sug_match is not None
    assert sug_match.group(1) == "Use a helper"
    nit_match = _AI_NIT_COMMENT_RE.match(nit_body)
    assert nit_match is not None
    assert nit_match.group(1) == "Trailing space"
    # A bug body must NOT match the suggestion/nit regexes (icon disjointness).
    bug_body = PRReviewer._format_finding_body(
        ReviewFinding(file="a.py", line=1, severity="bug",
                      title="Boom", description="d", fix_plan="f"),
    )
    assert _AI_SUGGESTION_COMMENT_RE.match(bug_body) is None
    assert _AI_NIT_COMMENT_RE.match(bug_body) is None


# ---------------------------------------------------------------------------
# evolution_tend._route_bug_comments
# ---------------------------------------------------------------------------


def test_route_bug_comments_appends_entry(mem_fs: dict[str, str]) -> None:
    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_threads_strict.return_value = [  # type: ignore[attr-defined]
        _thread(comment_id=5, path="a.py"),
    ]
    from maestro.evolution_tend import TendPRsResult

    res = TendPRsResult()
    scan_ok = evolution_tend._route_bug_comments(engine, _plan(), _pr_state(number=3), res)
    assert scan_ok is True
    assert len(res.bug_comments_routed) == 1
    pr_num, _title, comments, plan_id, _body, _url, head_ref = res.bug_comments_routed[0]
    assert pr_num == 3
    assert comments == [(5, "a.py", "Off-by-one in loop", "bug")]
    assert plan_id == "ep1"
    assert head_ref == "maestro/evolve/ep1"


def test_route_bug_comments_no_threads(mem_fs: dict[str, str]) -> None:
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_threads_strict.return_value = []  # type: ignore[attr-defined]
    res = TendPRsResult()
    scan_ok = evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    # A clean scan that routes nothing still reports success.
    assert scan_ok is True
    assert res.bug_comments_routed == []


def test_route_bug_comments_host_error_is_best_effort(mem_fs: dict[str, str]) -> None:
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_threads_strict.side_effect = VCSError("gh down")  # type: ignore[attr-defined]
    res = TendPRsResult()
    scan_ok = evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    # A thread-fetch error degrades to a no-op AND reports ``scan_ok=False`` so
    # the ready-for-review gate never reads it as "zero comments".
    assert scan_ok is False
    assert res.bug_comments_routed == []


def test_route_bug_comments_reply_completion_twin(mem_fs: dict[str, str]) -> None:
    """Twins on the same defect (#2893): A is outdated with a terminal reply, B
    is outdated and un-replied. Both are dropped from fix routing (outdated), and
    only the un-replied twin B is routed for reply-completion."""
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    twin_a = _thread(
        comment_id=30, path="a.py", is_outdated=True,
        replies=["Fixed locally with implementation-plan context. Done."],
    )
    twin_b = _thread(comment_id=32, path="a.py", is_outdated=True)
    engine._host.get_pr_review_threads_strict.return_value = [  # type: ignore[attr-defined]
        twin_a, twin_b,
    ]
    res = TendPRsResult()
    scan_ok = evolution_tend._route_bug_comments(engine, _plan(), _pr_state(number=3), res)
    assert scan_ok is True
    # Fix routing drops both twins (outdated).
    assert res.bug_comments_routed == []
    # Reply-completion routes only the un-replied twin B.
    assert len(res.reply_completion_routed) == 1
    (
        pr_num, _title, comments, plan_id, _body, _url, head_ref,
    ) = res.reply_completion_routed[0]
    assert pr_num == 3
    assert comments == [(32, "a.py", "Off-by-one in loop", "bug")]
    assert plan_id == "ep1"
    assert head_ref == "maestro/evolve/ep1"


def test_route_bug_comments_reply_completion_empty_for_live_thread(
    mem_fs: dict[str, str],
) -> None:
    """A live (non-outdated) un-replied thread routes only to
    ``bug_comments_routed``; the reply-completion delta (reply-needed minus
    eligible) is empty for it, so no duplicate reply-completion lap is minted
    (#2893)."""
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_threads_strict.return_value = [  # type: ignore[attr-defined]
        _thread(comment_id=40, path="a.py"),
    ]
    res = TendPRsResult()
    evolution_tend._route_bug_comments(engine, _plan(), _pr_state(number=3), res)
    assert len(res.bug_comments_routed) == 1
    assert res.reply_completion_routed == []


# ---------------------------------------------------------------------------
# tend_evolution_prs integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_pr_with_bug_comment_routes(mem_fs: dict[str, str]) -> None:
    """A CLEAN, mergeable PR still routes when it carries an open bug comment."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="CLEAN"),
    }
    engine._host.get_pr_review_threads_strict.return_value = [  # type: ignore[attr-defined]
        _thread(comment_id=11, path="maestro/agent.py"),
    ]
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    # Clean PR: the merge path is skipped, but the bug comment is routed.
    vcs.merge_from.assert_not_called()
    assert len(result.bug_comments_routed) == 1
    assert result.bug_comments_routed[0][0] == 1
    assert result.bug_comments_routed[0][2] == [
        (11, "maestro/agent.py", "Off-by-one in loop", "bug"),
    ]


@pytest.mark.asyncio
async def test_flag_disabled_skips_scan(mem_fs: dict[str, str]) -> None:
    from maestro.config import Config

    cfg = Config(memory_dir=Path("/fake/tend"), workspace=Path("/fake/tend"))
    cfg.tend_bug_comments_enabled = False
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs, config=cfg)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="CLEAN"),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    engine._host.get_pr_review_threads_strict.assert_not_called()  # type: ignore[attr-defined]
    assert result.bug_comments_routed == []


# ---------------------------------------------------------------------------
# evolution_tend._route_overflow_findings (proposal 064 Step 3)
# ---------------------------------------------------------------------------


def test_route_overflow_findings_appends_entry(mem_fs: dict[str, str]) -> None:
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body("- of-aaaaaaaa **a.py:0** [bug] — Null deref: boom"),
    ]
    engine._host.get_pr_comment_bodies_strict.return_value = []  # type: ignore[attr-defined]
    res = TendPRsResult()
    scan_ok = evolution_tend._route_bug_comments(engine, _plan(), _pr_state(number=3), res)
    assert scan_ok is True
    assert res.bug_comments_routed == []
    assert len(res.bug_overflow_routed) == 1
    pr_num, _title, findings, plan_id, _body, _url, head_ref = res.bug_overflow_routed[0]
    assert pr_num == 3
    assert len(findings) == 1
    assert findings[0].fingerprint == "of-aaaaaaaa"
    assert findings[0].file == "a.py"
    assert plan_id == "ep1"
    assert head_ref == "maestro/evolve/ep1"


def test_route_overflow_findings_returns_true_on_clean_scan(
    mem_fs: dict[str, str],
) -> None:
    """A completed overflow scan (no block to route) reports ``scan_ok=True``."""
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        "A review without any overflow block.",
    ]
    res = TendPRsResult()
    scan_ok = evolution_tend._route_overflow_findings(engine, _plan(), _pr_state(), res)
    assert scan_ok is True
    assert res.bug_overflow_routed == []


def test_route_overflow_findings_returns_false_on_fetch_error(
    mem_fs: dict[str, str],
) -> None:
    """A review-body fetch error reports ``scan_ok=False`` from the overflow scan."""
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.side_effect = VCSError("gh down")  # type: ignore[attr-defined]
    res = TendPRsResult()
    scan_ok = evolution_tend._route_overflow_findings(engine, _plan(), _pr_state(), res)
    assert scan_ok is False
    assert res.bug_overflow_routed == []


def test_route_overflow_all_acknowledged_routes_nothing(
    mem_fs: dict[str, str],
) -> None:
    from maestro.evolution_tend import _OVERFLOW_ACK_SENTINEL, TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body(
            "- of-aaaaaaaa **a.py:0** [bug] — Null deref: boom",
            "- of-bbbbbbbb **b.py:9** [bug] — Race: two writers",
        ),
    ]
    engine._host.get_pr_comment_bodies_strict.return_value = [  # type: ignore[attr-defined]
        "unrelated human comment",
        f"{_OVERFLOW_ACK_SENTINEL}\n"
        "of-aaaaaaaa — Fixed locally with implementation-plan context.\n"
        "of-bbbbbbbb — Tracked in #99",
    ]
    res = TendPRsResult()
    evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    assert res.bug_overflow_routed == []


def test_route_overflow_no_block_skips_comment_fetch(
    mem_fs: dict[str, str],
) -> None:
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        "A review without any overflow block.",
    ]
    res = TendPRsResult()
    evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    assert res.bug_overflow_routed == []
    engine._host.get_pr_comment_bodies_strict.assert_not_called()  # type: ignore[attr-defined]


def test_route_overflow_unions_across_review_bodies(mem_fs: dict[str, str]) -> None:
    """The reviewer's block is non-cumulative, so a finding carried only by a
    superseded middle review is still outstanding and must stay routable
    (issue #3326) — the newest copy of a *repeated* handle wins on text."""
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body("- of-aaaaaaaa **a.py:0** [bug] — Older finding: still open"),
        _overflow_body(
            "- of-bbbbbbbb **b.py:4** [bug] — New finding: live",
            "- of-aaaaaaaa **a.py:0** [bug] — Older finding reworded: still open",
        ),
    ]
    engine._host.get_pr_comment_bodies_strict.return_value = []  # type: ignore[attr-defined]
    res = TendPRsResult()
    evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    assert len(res.bug_overflow_routed) == 1
    findings = res.bug_overflow_routed[0][2]
    assert sorted(f.fingerprint for f in findings) == ["of-aaaaaaaa", "of-bbbbbbbb"]
    by_fp = {f.fingerprint: f for f in findings}
    assert by_fp["of-aaaaaaaa"].title == "Older finding reworded"


def test_route_overflow_unions_acks_across_comment_bodies(
    mem_fs: dict[str, str],
) -> None:
    """Terminal markers are unioned across *every* sentinel-carrying comment, so
    an ack recorded on an earlier roll-up is not lost to a later one."""
    from maestro.evolution_tend import _OVERFLOW_ACK_SENTINEL, TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body(
            "- of-aaaaaaaa **a.py:0** [bug] — Null deref: boom",
            "- of-bbbbbbbb **b.py:9** [bug] — Race: two writers",
        ),
    ]
    engine._host.get_pr_comment_bodies_strict.return_value = [  # type: ignore[attr-defined]
        f"{_OVERFLOW_ACK_SENTINEL}\nof-aaaaaaaa — Fixed automatically",
        f"{_OVERFLOW_ACK_SENTINEL}\nof-bbbbbbbb — Tracked in #99",
    ]
    res = TendPRsResult()
    evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    assert res.bug_overflow_routed == []


def test_overflow_fingerprint_survives_title_rewording(
    mem_fs: dict[str, str],
) -> None:
    """A re-worded title re-renders to the *same* handle, so a re-render dedupes
    to one finding instead of minting an un-ackable new identity (#3326)."""
    from maestro.evolution_tend import TendPRsResult, _overflow_fingerprint

    handle = _overflow_fingerprint("bug", "a.py", 4, "Null deref")
    assert _overflow_fingerprint("bug", "a.py", 4, "Nil pointer hazard") == handle
    assert _overflow_fingerprint("nit", "a.py", 4, "Null deref") != handle
    assert _overflow_fingerprint("bug", "b.py", 4, "Null deref") != handle
    assert _overflow_fingerprint("bug", "a.py", 5, "Null deref") != handle

    # A legacy (handle-less) line from each of two reviews, titled differently,
    # recomputes to one fingerprint and dedupes across the union walk.
    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body("- **a.py:4** [bug] — Null deref: boom"),
        _overflow_body("- **a.py:4** [bug] — Nil pointer hazard: boom"),
    ]
    engine._host.get_pr_comment_bodies_strict.return_value = []  # type: ignore[attr-defined]
    res = TendPRsResult()
    evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    assert [f.fingerprint for f in res.bug_overflow_routed[0][2]] == [handle]


def test_route_overflow_keeps_distinct_findings_sharing_one_handle(
    mem_fs: dict[str, str],
) -> None:
    """Two different findings at one out-of-diff ``severity|file|line`` share the
    coarse handle, so a fingerprint-keyed union merge would silently drop one.
    Both stay routable, and a re-render across bodies still collapses to the pair
    — including when the newer block re-renders only one of them."""
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    both = _overflow_body(
        "- of-aaaaaaaa **a.py:4** [bug] — Race condition: boom",
        "- of-aaaaaaaa **a.py:4** [bug] — Unbounded growth: boom",
    )
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        both,
        _overflow_body("- of-aaaaaaaa **a.py:4** [bug] — Race condition: boom"),
    ]
    engine._host.get_pr_comment_bodies_strict.return_value = []  # type: ignore[attr-defined]
    res = TendPRsResult()
    evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    findings = res.bug_overflow_routed[0][2]
    assert sorted(f.title for f in findings) == ["Race condition",
                                                 "Unbounded growth"]


def test_route_overflow_collapses_legacy_title_derived_renderings(
    mem_fs: dict[str, str],
) -> None:
    """A PR reviewed across the #3326 identity change carries a *different*
    embedded handle per legacy block for one still-open finding.  Keying the
    union on those raw handles minted a duplicate remediation entry per
    historical review; the canonical key collapses them onto the coarse handle."""
    from maestro.evolution_tend import (
        TendPRsResult,
        _legacy_overflow_fingerprint,
        _overflow_fingerprint,
    )

    coarse = _overflow_fingerprint("bug", "a.py", 7, "Race condition")
    legacy_one = _legacy_overflow_fingerprint("bug", "a.py", 7, "Race condition")
    legacy_two = _legacy_overflow_fingerprint("bug", "a.py", 7, "Deadlock hazard")
    assert len({coarse, legacy_one, legacy_two}) == 3

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body(f"- {legacy_one} **a.py:7** [bug] — Race condition: boom"),
        _overflow_body(f"- {legacy_two} **a.py:7** [bug] — Deadlock hazard: boom"),
        _overflow_body(f"- {coarse} **a.py:7** [bug] — Race condition: boom"),
    ]
    engine._host.get_pr_comment_bodies_strict.return_value = []  # type: ignore[attr-defined]
    res = TendPRsResult()
    evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    findings = res.bug_overflow_routed[0][2]
    assert [f.fingerprint for f in findings] == [coarse]


def test_route_overflow_coarse_ack_suppresses_legacy_block_copy(
    mem_fs: dict[str, str],
) -> None:
    """An ack recorded under the *coarse* handle must also suppress the copy of
    that finding read from a pre-#3326 legacy block.  The per-block ack filter
    runs before the union, so a legacy-rendered copy that escapes it merges back
    under the coarse key and re-routes an already-answered finding."""
    from maestro.evolution_tend import (
        _OVERFLOW_ACK_SENTINEL,
        TendPRsResult,
        _legacy_overflow_fingerprint,
        _overflow_fingerprint,
    )

    coarse = _overflow_fingerprint("bug", "a.py", 7, "Race condition")
    legacy = _legacy_overflow_fingerprint("bug", "a.py", 7, "Race condition")
    assert coarse != legacy

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body(f"- {legacy} **a.py:7** [bug] — Race condition: boom"),
        _overflow_body(f"- {coarse} **a.py:7** [bug] — Race condition: boom"),
    ]
    engine._host.get_pr_comment_bodies_strict.return_value = [  # type: ignore[attr-defined]
        f"{_OVERFLOW_ACK_SENTINEL}\n- {coarse} — Fixed automatically",
    ]
    res = TendPRsResult()
    evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    assert res.bug_overflow_routed == []


def test_route_overflow_merge_key_keeps_file_level_renderings_distinct(
    mem_fs: dict[str, str],
) -> None:
    """At ``line <= 0`` there is no coarse identity, so the canonical merge key
    falls back to the finding's own handle and two distinct file-level findings
    still survive the union separately."""
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body("- of-aaaaaaaa **a.py:0** [bug] — Race condition: boom"),
        _overflow_body("- of-bbbbbbbb **a.py:0** [bug] — Unbounded growth: boom"),
    ]
    engine._host.get_pr_comment_bodies_strict.return_value = []  # type: ignore[attr-defined]
    res = TendPRsResult()
    evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    findings = res.bug_overflow_routed[0][2]
    assert sorted(f.title for f in findings) == ["Race condition",
                                                 "Unbounded growth"]


def test_overflow_fingerprint_keeps_file_level_findings_distinct() -> None:
    """At ``line <= 0`` the site alone is not an identity, so the title folds
    back in: two distinct file-level findings must not collapse to one handle
    that a single ack would suppress."""
    from maestro.evolution_tend import _legacy_overflow_fingerprint, _overflow_fingerprint

    first = _overflow_fingerprint("bug", "a.py", 0, "Null deref")
    second = _overflow_fingerprint("bug", "a.py", 0, "Unbounded list growth")
    assert first != second
    # There the current handle *is* the legacy key, so historical acks match.
    assert first == _legacy_overflow_fingerprint("bug", "a.py", 0, "Null deref")


@pytest.mark.parametrize(("severity", "file"), [("", "a.py"), ("bug", "")])
def test_overflow_fingerprint_rejects_non_derivable_identity(
    severity: str, file: str,
) -> None:
    """An empty severity or file yields an un-ackable handle, so minting raises."""
    from maestro.evolution_tend import _overflow_fingerprint

    with pytest.raises(ValueError, match="non-empty severity and file"):
        _overflow_fingerprint(severity, file, 4, "t")


def test_legacy_title_keyed_ack_still_suppresses_finding() -> None:
    """An ack posted under the pre-#3326 title-derived handle still answers the
    finding, so historical acknowledgements are not resurfaced once."""
    from maestro.evolution_tend import (
        _OVERFLOW_ACK_SENTINEL,
        _eligible_overflow_bug_findings,
        _legacy_overflow_fingerprint,
        _overflow_fingerprint,
    )

    # A real line number, so the current handle is the coarse site-only one and
    # genuinely differs from the legacy title-derived key.
    legacy = _legacy_overflow_fingerprint("bug", "a.py", 4, "Null deref")
    assert legacy != _overflow_fingerprint("bug", "a.py", 4, "Null deref")
    body = _overflow_body(
        f"- {_overflow_fingerprint('bug', 'a.py', 4, 'Null deref')} "
        "**a.py:4** [bug] — Null deref: boom",
    )
    ack = f"{_OVERFLOW_ACK_SENTINEL}\n{legacy} — Fixed automatically"
    assert _eligible_overflow_bug_findings(body, ack) == []
    # An ack keyed to a *different* title at the same site does not suppress it.
    other = _legacy_overflow_fingerprint("bug", "a.py", 4, "Totally other defect")
    assert len(_eligible_overflow_bug_findings(
        body, f"{_OVERFLOW_ACK_SENTINEL}\n{other} — Fixed automatically",
    )) == 1


def test_route_overflow_review_fetch_error_is_best_effort(
    mem_fs: dict[str, str],
) -> None:
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.side_effect = VCSError("gh down")  # type: ignore[attr-defined]
    res = TendPRsResult()
    scan_ok = evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    # The overflow review-body fetch errored, so the composite scan fails even
    # though the (default-empty) thread scan succeeded.
    assert scan_ok is False
    assert res.bug_overflow_routed == []
    engine._host.get_pr_comment_bodies_strict.assert_not_called()  # type: ignore[attr-defined]


def test_route_overflow_ack_fetch_error_skips_routing(
    mem_fs: dict[str, str],
) -> None:
    """An unreadable acknowledgement comment must skip the route, not degrade:
    routing without the acknowledgement state could re-dispatch findings that
    were already addressed."""
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body("- of-aaaaaaaa **a.py:0** [bug] — Null deref: boom"),
    ]
    engine._host.get_pr_comment_bodies_strict.side_effect = VCSError("gh down")  # type: ignore[attr-defined]
    res = TendPRsResult()
    scan_ok = evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    # The acknowledgement-comment fetch errored → composite scan reports failure.
    assert scan_ok is False
    assert res.bug_overflow_routed == []


def test_route_overflow_thread_error_still_routes_overflow(
    mem_fs: dict[str, str],
) -> None:
    """A review-thread fetch failure must not abort the overflow scan."""
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_threads_strict.side_effect = VCSError("gh down")  # type: ignore[attr-defined]
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body("- of-aaaaaaaa **a.py:0** [bug] — Null deref: boom"),
    ]
    engine._host.get_pr_comment_bodies_strict.return_value = []  # type: ignore[attr-defined]
    res = TendPRsResult()
    scan_ok = evolution_tend._route_bug_comments(engine, _plan(), _pr_state(), res)
    # Overflow still routes, but the thread-fetch error makes the composite
    # ``threads_ok and overflow_ok`` report ``False`` — the strict-path gate
    # must not treat a partial scan as a clean "zero comments".
    assert scan_ok is False
    assert res.bug_comments_routed == []
    assert len(res.bug_overflow_routed) == 1


def test_route_threads_and_overflow_both_route(mem_fs: dict[str, str]) -> None:
    from maestro.evolution_tend import TendPRsResult

    engine = _make_engine(mem_fs)
    engine._host.get_pr_review_threads_strict.return_value = [  # type: ignore[attr-defined]
        _thread(comment_id=5, path="a.py"),
    ]
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body("- of-aaaaaaaa **b.py:0** [bug] — Null deref: boom"),
    ]
    engine._host.get_pr_comment_bodies_strict.return_value = []  # type: ignore[attr-defined]
    res = TendPRsResult()
    evolution_tend._route_bug_comments(engine, _plan(), _pr_state(number=3), res)
    assert len(res.bug_comments_routed) == 1
    assert len(res.bug_overflow_routed) == 1
    assert res.bug_overflow_routed[0][0] == 3


@pytest.mark.asyncio
async def test_clean_pr_with_overflow_finding_routes(
    mem_fs: dict[str, str],
) -> None:
    """A CLEAN, mergeable PR still routes an unaddressed overflow finding."""
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="CLEAN"),
    }
    engine._host.get_pr_review_bodies_strict.return_value = [  # type: ignore[attr-defined]
        _overflow_body("- of-aaaaaaaa **a.py:0** [bug] — Null deref: boom"),
    ]
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    vcs.merge_from.assert_not_called()
    assert len(result.bug_overflow_routed) == 1
    assert result.bug_overflow_routed[0][0] == 1


@pytest.mark.asyncio
async def test_flag_disabled_skips_overflow_scan(
    mem_fs: dict[str, str],
) -> None:
    from maestro.config import Config

    cfg = Config(memory_dir=Path("/fake/tend"), workspace=Path("/fake/tend"))
    cfg.tend_bug_comments_enabled = False
    vcs = MagicMock()
    vcs.get_current_branch.return_value = "main"
    vcs.rev_parse.return_value = "h"
    vcs.commits_ahead_behind.return_value = (0, 0)
    vcs.list_remote_branches.return_value = []
    engine = _make_engine(mem_fs, vcs=vcs, config=cfg)
    engine._memory.save_evolution_plan(_plan())
    engine._host.get_open_evolution_pr_states.return_value = {  # type: ignore[attr-defined]
        "maestro/evolve/ep1": _pr_state(merge_state_status="CLEAN"),
    }
    result = await engine.tend_evolution_prs(settled_oracle=merge_oracle())
    engine._host.get_pr_review_bodies_strict.assert_not_called()  # type: ignore[attr-defined]
    assert result.bug_overflow_routed == []


# ---------------------------------------------------------------------------
# _eligible_overflow_bug_findings parser (proposal 064 Step 2)
# ---------------------------------------------------------------------------


def _overflow_body(*lines: str, hardened: bool = True) -> str:
    """Assemble a review body carrying the overflow block built from *lines*.

    ``hardened`` wraps the block in the sentinel pair (post-Step-1); otherwise
    the lines follow the bare legacy header (the interrupted-upgrade case).
    """
    from maestro.evolution_tend import (
        _OVERFLOW_LEGACY_HEADER,
        _OVERFLOW_SENTINEL_CLOSE,
        _OVERFLOW_SENTINEL_OPEN,
    )

    block = "\n".join(lines)
    if hardened:
        return (
            "Summary text.\n\n"
            f"{_OVERFLOW_LEGACY_HEADER}\n"
            f"{_OVERFLOW_SENTINEL_OPEN}\n{block}\n{_OVERFLOW_SENTINEL_CLOSE}"
            "\n\n---\n*AI-generated review — advisory only*"
        )
    return (
        "Summary text.\n\n"
        f"{_OVERFLOW_LEGACY_HEADER}\n{block}"
        "\n\n**Findings already covered by open issues** (1 skipped):\n"
        "- **x.py:9** — Tracked elsewhere"
    )


def test_overflow_parser_hardened_block_keeps_all_ai_severities() -> None:
    from maestro.evolution_tend import _eligible_overflow_bug_findings

    body = _overflow_body(
        "- of-aaaaaaaa **a.py:0** [bug] — Null deref: crashes on None",
        "- of-bbbbbbbb **b.py:5** [suggestion] — Rename var: clarity",
        "- of-cccccccc **c.py:7** [nit] — Trailing space: style",
        "- of-dddddddd **d.py:9** [chatter] — Not a real severity: dropped",
    )
    found = _eligible_overflow_bug_findings(body)
    assert len(found) == 3
    assert [(f.fingerprint, f.severity) for f in found] == [
        ("of-aaaaaaaa", "bug"),
        ("of-bbbbbbbb", "suggestion"),
        ("of-cccccccc", "nit"),
    ]
    assert found[0].file == "a.py"
    assert found[0].line == 0
    assert found[0].title == "Null deref"
    assert found[0].description == "crashes on None"


def test_overflow_parser_title_without_description() -> None:
    from maestro.evolution_tend import _eligible_overflow_bug_findings

    body = _overflow_body("- of-eeeeeeee **a.py:1** [bug] — Title only")
    found = _eligible_overflow_bug_findings(body)
    assert len(found) == 1
    assert found[0].title == "Title only"
    assert found[0].description == ""


def test_overflow_parser_keeps_colon_inside_title() -> None:
    from maestro.evolution_tend import _eligible_overflow_bug_findings

    # A bare colon with no following space (``A:B``) belongs to the title; the
    # real ``": "`` separator is the title/description boundary the reviewer emits.
    body = _overflow_body(
        "- of-aaaaaaaa **a.py:1** [bug] — Use A:B format: crashes",
    )
    found = _eligible_overflow_bug_findings(body)
    assert len(found) == 1
    assert found[0].title == "Use A:B format"
    assert found[0].description == "crashes"


def test_overflow_parser_legacy_colon_title_recomputes_full_fingerprint() -> None:
    from maestro.evolution_tend import _eligible_overflow_bug_findings, _overflow_fingerprint

    # For a legacy line the fingerprint is recomputed from the parsed title, so a
    # truncated title would hash differently from the reviewer's embedded handle
    # and break ack-comment dedup.
    body = _overflow_body(
        "- **a.py:3** [bug] — Use A:B format: crashes",
        hardened=False,
    )
    found = _eligible_overflow_bug_findings(body)
    assert len(found) == 1
    assert found[0].title == "Use A:B format"
    assert found[0].fingerprint == _overflow_fingerprint(
        "bug", "a.py", 3, "Use A:B format",
    )


def test_overflow_parser_empty_description_with_colon_separator() -> None:
    from maestro.evolution_tend import _eligible_overflow_bug_findings

    # The reviewer renders an empty description as ``"— Title only: "`` (colon,
    # space, empty); the parser strips the line first, so the title must stay
    # clean rather than retaining a trailing colon.
    body = _overflow_body("- of-eeeeeeee **a.py:1** [bug] — Title only: ")
    found = _eligible_overflow_bug_findings(body)
    assert len(found) == 1
    assert found[0].title == "Title only"
    assert found[0].description == ""


def test_overflow_parser_legacy_header_recomputes_fingerprint() -> None:
    from maestro.evolution_tend import _eligible_overflow_bug_findings, _overflow_fingerprint

    body = _overflow_body(
        "- **a.py:3** [bug] — Race condition: two writers",
        hardened=False,
    )
    found = _eligible_overflow_bug_findings(body)
    assert len(found) == 1
    assert found[0].fingerprint == _overflow_fingerprint(
        "bug", "a.py", 3, "Race condition",
    )
    assert found[0].file == "a.py"
    assert found[0].line == 3


@pytest.mark.parametrize("marker", _BUG_COMMENT_TERMINAL_MARKERS)
def test_overflow_parser_drops_acknowledged_fingerprint(marker: str) -> None:
    from maestro.evolution_tend import _OVERFLOW_ACK_SENTINEL, _eligible_overflow_bug_findings

    body = _overflow_body("- of-deadbeef **a.py:0** [bug] — Null deref: boom")
    ack = f"{_OVERFLOW_ACK_SENTINEL}\nof-deadbeef — {marker}1234"
    assert _eligible_overflow_bug_findings(body, ack) == []
    # A terminal marker on a *different* fingerprint does not suppress it.
    other_ack = f"{_OVERFLOW_ACK_SENTINEL}\nof-00000000 — {marker}"
    assert len(_eligible_overflow_bug_findings(body, other_ack)) == 1


def test_overflow_parser_marker_without_handle_does_not_suppress() -> None:
    from maestro.evolution_tend import _eligible_overflow_bug_findings

    body = _overflow_body("- of-deadbeef **a.py:0** [bug] — Null deref: boom")
    # Marker phrase present but no ``of-`` handle on the line → not an ack.
    assert len(_eligible_overflow_bug_findings(body, "Fixed automatically")) == 1


def test_overflow_parser_empty_and_malformed_bodies() -> None:
    from maestro.evolution_tend import (
        _OVERFLOW_LEGACY_HEADER,
        _OVERFLOW_SENTINEL_OPEN,
        _eligible_overflow_bug_findings,
    )

    assert _eligible_overflow_bug_findings("") == []
    assert _eligible_overflow_bug_findings("No overflow section here.") == []
    # Sentinel open without a close, and no legacy header → no block.
    assert _eligible_overflow_bug_findings(
        f"text {_OVERFLOW_SENTINEL_OPEN}\n- of-aaaaaaaa **a.py:0** [bug] — X: y",
    ) == []
    # Legacy header present but no bullet lines → no block.
    assert _eligible_overflow_bug_findings(
        f"{_OVERFLOW_LEGACY_HEADER}\n\nplain prose, no findings",
    ) == []
    # Block present but the line is unstructured prose → parsed away.
    assert _eligible_overflow_bug_findings(
        _overflow_body("- just some prose without structure"),
    ) == []


# ---------------------------------------------------------------------------
# Cross-package overflow constant / fingerprint consistency
# (maestro may not import maestro_review)
# ---------------------------------------------------------------------------


def test_overflow_sentinels_match_review() -> None:
    from maestro.evolution_tend import _OVERFLOW_SENTINEL_CLOSE, _OVERFLOW_SENTINEL_OPEN
    from maestro_review.review import (
        _OVERFLOW_SENTINEL_CLOSE as review_close,
    )
    from maestro_review.review import (
        _OVERFLOW_SENTINEL_OPEN as review_open,
    )

    assert review_open == _OVERFLOW_SENTINEL_OPEN
    assert review_close == _OVERFLOW_SENTINEL_CLOSE


def test_overflow_ack_sentinel_matches_review() -> None:
    """The reviewer's read-side ack sentinel must equal the core writer's, so the
    reply-coverage gate (proposal 083) reads the same roll-up the loop writes."""
    from maestro.evolution_tend import _OVERFLOW_ACK_SENTINEL
    from maestro_review.review import (
        _OVERFLOW_ACK_SENTINEL as review_ack,
    )

    assert review_ack == _OVERFLOW_ACK_SENTINEL


def test_overflow_legacy_header_matches_review() -> None:
    """The reviewer's read-side legacy header must equal the core constant so the
    sentinel-less fallback parses the same bodies on both sides."""
    from maestro.evolution_tend import _OVERFLOW_LEGACY_HEADER
    from maestro_review.review import (
        _OVERFLOW_LEGACY_HEADER as review_header,
    )

    assert review_header == _OVERFLOW_LEGACY_HEADER


def test_all_comment_terminal_markers_match_review() -> None:
    """The reviewer's terminal-marker set (used by ``_parse_overflow_ack``) must
    equal the core union so both ack parsers agree on which lines close a finding;
    a drift here would let one side ack a verdict the other ignores."""
    from maestro.evolution_tend import _ALL_COMMENT_TERMINAL_MARKERS
    from maestro_review.markers import (
        _ALL_COMMENT_TERMINAL_MARKERS as review_markers,
    )

    assert review_markers == _ALL_COMMENT_TERMINAL_MARKERS


def test_overflow_ack_parsers_agree_terminal_only() -> None:
    """The reviewer's read-side ack parser must collect exactly the fingerprints the
    core consumer does for the same roll-up body: only handles on a line carrying a
    terminal verdict.  A handle listed under the sentinel without one is
    acknowledged by neither — guarding the leniency flagged on PR #2694."""
    from maestro.evolution_tend import _OVERFLOW_ACK_SENTINEL, _acknowledged_overflow_fingerprints
    from maestro_review.review import _parse_overflow_ack

    body = (
        f"{_OVERFLOW_ACK_SENTINEL}\n"
        "of-11111111 — Fixed automatically\n"
        "of-22222222 — Declined with implementation-plan context. — out of scope\n"
        "of-33333333 — still pending, no verdict yet\n"  # not terminal → not acked
        # Multi-handle terminal line: both parsers ack only the keyed (first)
        # handle, never the quoted of-bbbbbbbb (core uses .search(), the reviewer
        # slices findall(...)[:1]).  Guards the divergence flagged on PR #2694.
        "of-aaaaaaaa — Fixed automatically (was a dup of of-bbbbbbbb)\n"
    )

    review_set = _parse_overflow_ack([body])
    core_set = _acknowledged_overflow_fingerprints(body)

    assert review_set == core_set
    assert review_set == {"of-11111111", "of-22222222", "of-aaaaaaaa"}


def test_review_overflow_parser_reads_back_reviewer_payload() -> None:
    """The reviewer's own read-side parser round-trips the body it renders, keeping
    every severity and matching each finding's fingerprint."""
    from maestro_review.review import (
        PRReviewer,
        ReviewFinding,
        _overflow_fingerprint,
        _parse_overflow_findings,
    )

    reviewer = PRReviewer("owner/repo", code_host=MagicMock())
    findings = [
        ReviewFinding(file="a.py", line=0, severity="bug",
                      title="Null line bug", description="off-hunk", fix_plan="f"),
        ReviewFinding(file="b.py", line=5, severity="suggestion",
                      title="Rename for clarity", description="d", fix_plan="f"),
    ]
    payload = reviewer._build_review_payload(findings, {"a.py": {1, 2}}, "Summary")

    parsed = _parse_overflow_findings(payload["body"])
    assert {f.severity for f in parsed} == {"bug", "suggestion"}
    assert {f.fingerprint for f in parsed} == {
        _overflow_fingerprint(findings[0]),
        _overflow_fingerprint(findings[1]),
    }


def test_overflow_fingerprint_matches_review() -> None:
    from maestro.evolution_tend import _overflow_fingerprint
    from maestro_review.review import (
        ReviewFinding,
    )
    from maestro_review.review import (
        _overflow_fingerprint as review_fingerprint,
    )

    finding = ReviewFinding(
        file="maestro/agent.py", line=42, severity="bug",
        title="Off-by-one in the retry loop", description="d", fix_plan="f",
    )
    assert _overflow_fingerprint(
        finding.severity, finding.file, finding.line, finding.title,
    ) == review_fingerprint(finding)


def test_overflow_title_stopwords_match_review() -> None:
    from maestro.evolution_tend import _OVERFLOW_TITLE_STOPWORDS
    from maestro_review.review import _TITLE_STOPWORDS

    assert _OVERFLOW_TITLE_STOPWORDS == _TITLE_STOPWORDS


def test_overflow_parser_round_trips_reviewer_payload() -> None:
    """The core parser extracts the AI findings (all severities) the reviewer
    renders into the overflow block."""
    from maestro.evolution_tend import _eligible_overflow_bug_findings
    from maestro_review.review import (
        PRReviewer,
        ReviewFinding,
        _overflow_fingerprint,
    )

    reviewer = PRReviewer("owner/repo", code_host=MagicMock())
    findings = [
        ReviewFinding(file="a.py", line=0, severity="bug",
                      title="Null line bug", description="off-hunk", fix_plan="fix"),
        ReviewFinding(file="b.py", line=0, severity="suggestion",
                      title="Rename for clarity", description="d", fix_plan="fix"),
    ]
    # line=0 never lands on a valid diff line, so both findings overflow.
    payload = reviewer._build_review_payload(findings, {}, "Summary")
    found = _eligible_overflow_bug_findings(payload["body"])
    assert len(found) == 2
    by_fp = {f.fingerprint: f for f in found}
    assert by_fp[_overflow_fingerprint(findings[0])].severity == "bug"
    assert by_fp[_overflow_fingerprint(findings[1])].severity == "suggestion"


def test_overflow_legacy_header_matches_review_payload() -> None:
    """The legacy header constant matches the reviewer's rendered body verbatim.

    ``_OVERFLOW_LEGACY_HEADER`` is duplicated from the inline literal in
    ``maestro_review.review._build_review_payload`` (``maestro`` may not import
    ``maestro_review``).  The round-trip test above exercises the sentinel path,
    and the legacy-path tests build their bodies from this same constant — so
    nothing would catch the constant drifting from ``review.py``'s literal.
    Asserting the constant appears in an *actual* reviewer payload closes that
    circular gap: a rename of the inline header in ``review.py`` fails here.
    """
    from maestro.evolution_tend import _OVERFLOW_LEGACY_HEADER
    from maestro_review.review import PRReviewer, ReviewFinding

    reviewer = PRReviewer("owner/repo", code_host=MagicMock())
    findings = [
        ReviewFinding(file="a.py", line=0, severity="bug",
                      title="Null line bug", description="off-hunk", fix_plan="fix"),
    ]
    # line=0 never lands on a valid diff line, so this finding always overflows.
    payload = reviewer._build_review_payload(findings, {}, "Summary")
    assert _OVERFLOW_LEGACY_HEADER in payload["body"]


def test_count_eligible_threads_fail_safe_counts_eligible_threads() -> None:
    """Success branch: the strict scan's eligible threads are counted."""
    from maestro.evolution_tend import count_eligible_threads_fail_safe

    host = MagicMock()
    # Two eligible threads plus a resolved one — 2 is distinguishable from
    # the fail-safe 1, so this proves the threads are genuinely counted.
    host.get_pr_review_threads_strict.return_value = [
        _thread(comment_id=1),
        _thread(comment_id=3),
        _thread(comment_id=5, is_resolved=True),
    ]
    assert count_eligible_threads_fail_safe(host, 7) == 2
    host.get_pr_review_threads_strict.assert_called_once_with(7)


def test_count_eligible_threads_fail_safe_failed_scan_is_one_blocker() -> None:
    """Regression (#4387): a failed scan reads as one blocker, never zero."""
    from maestro.evolution_tend import count_eligible_threads_fail_safe

    host = MagicMock()
    host.get_pr_review_threads_strict.side_effect = VCSError("graphql 403")
    assert count_eligible_threads_fail_safe(host, 7) == 1

"""Tests for the ``is_pr_settled`` merge-truth chokepoint.

Proposal 087. ``MaestroAgent.is_pr_settled`` is the single door every
PR-servicing face reads merge state through: one fetch-first, provenance-stamped
:class:`~maestro.pr_facts.MergeTruth`, memoized per iteration with a freshness
TTL. These tests pin the oracle wiring, the per-iteration memo (one fetch per PR
even across two faces), and the TTL re-observation — all with mocked git/host
I/O, per Testing Principles.
"""

from __future__ import annotations

import json
import logging
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from maestro.memory import EvolutionPlan, VersionRecord
from maestro.remediation_tracker import (
    _FETCH_TRUTH_FILE,
    has_fetch_truth,
    record_fetch_truth,
)
from maestro.vcs import SubprocessCodeHost, VCSError


def _completed(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
    """A benign ``CompletedProcess`` for a mocked ``git`` invocation.

    Absorbs ``subprocess.run``'s keyword args (``capture_output`` / ``check`` /
    ``timeout`` / ``cwd`` / ``text``) so it can stand in as a ``side_effect``.
    """
    return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")


class _Clock:
    """A mutable zero-arg clock so a test can advance time between calls."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _stub_host(agent, *, merged: bool, closed: bool = False) -> None:  # type: ignore[no-untyped-def]
    """Wire the agent's code-host merge/close oracles to fixed verdicts.

    Both merge reads are stubbed: the chokepoint wires the fail-loud
    ``is_pr_merged_strict`` (#2954), while other faces still read the
    best-effort ``is_pr_merged`` — they must agree, or a test would silently
    exercise a different tier than it names.
    """
    agent.evolution._host.is_pr_merged = MagicMock(return_value=merged)
    agent.evolution._host.is_pr_merged_strict = MagicMock(return_value=merged)
    agent.evolution._host.is_pr_closed = MagicMock(return_value=closed)


def _make_real(make_agent):  # type: ignore[no-untyped-def]
    """An agent with the *real* ``is_pr_settled`` bound.

    The ``make_agent`` fixture default-stubs ``is_pr_settled`` (so unrelated
    remediation tests do no real git/host I/O); this file's whole purpose is the
    real chokepoint, so drop the instance stub to expose the class method."""
    agent = make_agent()
    del agent.is_pr_settled
    return agent


# ── is_pr_settled wiring ────────────────────────────────────────────────


def test_is_pr_settled_host_merged(make_agent):  # type: ignore[no-untyped-def]
    """Host reports merged → MERGED / settled / landed, provenance host_api."""
    agent = _make_real(make_agent)
    _stub_host(agent, merged=True)
    with patch("maestro.agent.subprocess.run", side_effect=_completed) as run:
        truth = agent.is_pr_settled(2662)
    assert truth.state == "MERGED"
    assert truth.settled is True
    assert truth.branch_landed is True
    assert truth.source == "host_api"
    # Fetch-first: the chokepoint freshened refs before observing.
    assert run.call_args_list[0].args[0][:3] == ["git", "fetch", "origin"]


def test_is_pr_settled_open_not_landed(make_agent):  # type: ignore[no-untyped-def]
    """Host unmerged and not closed → OPEN / unsettled / unlanded."""
    agent = _make_real(make_agent)
    _stub_host(agent, merged=False, closed=False)
    with patch("maestro.agent.subprocess.run", side_effect=_completed):
        truth = agent.is_pr_settled(7)
    assert truth.state == "OPEN"
    assert truth.settled is False
    assert truth.branch_landed is False


def test_is_pr_settled_falls_back_to_git_subject_when_host_raises(make_agent):  # type: ignore[no-untyped-def]
    """When the host oracle raises, the chokepoint consults the git-subject
    branch oracle and stamps source ``git_subject``."""
    agent = _make_real(make_agent)
    # The chokepoint wires the fail-loud ``is_pr_merged_strict`` as
    # ``host_oracle`` (#2954) — stub *that* read, so this test drives the tier
    # it names rather than reaching it through the conftest delegation shim.
    agent.evolution._host.is_pr_merged_strict = MagicMock(
        side_effect=RuntimeError("host down"),
    )
    agent.evolution._host.is_pr_closed = MagicMock(return_value=False)
    with (
        patch("maestro.agent.subprocess.run", side_effect=_completed),
        patch("maestro.agent._git_pr_merged", return_value=True) as git_oracle,
    ):
        truth = agent.is_pr_settled(2662)
    assert truth.state == "MERGED"
    assert truth.branch_landed is True
    assert truth.source == "git_subject"
    git_oracle.assert_called_once()


def test_is_pr_settled_falls_back_to_git_subject_when_host_subprocess_fails(make_agent):  # type: ignore[no-untyped-def]
    """A *real* failing ``gh`` — not an injected raising mock — reaches the fallback.

    The #2954 regression: the only production ``CodeHost`` is
    :class:`~maestro.vcs.SubprocessCodeHost`, whose best-effort
    ``is_pr_merged`` catches ``VCSError`` / ``JSONDecodeError`` and returns
    ``False``. Wiring *that* method as the chokepoint's ``host_oracle`` made the
    observer's ``except`` branch dead code, so a transient ``gh`` failure was
    laundered into a confident ``MergeTruth(branch_landed=False,
    source="host_api")``. Only the fail-loud ``is_pr_merged_strict`` lets the
    documented git-subject tier answer. The sibling test above injects a raising
    ``MagicMock`` and so passed either way — which is exactly why the dead
    fallback went unnoticed.
    """
    agent = _make_real(make_agent)
    # The real host, failing the way a rate-limited / offline ``gh`` fails.
    host = SubprocessCodeHost("owner/repo", workspace=agent.config.workspace)
    host._run = MagicMock(side_effect=VCSError("gh: API rate limit exceeded"))  # type: ignore[method-assign]
    agent.evolution._host = host
    with (
        patch("maestro.agent.subprocess.run", side_effect=_completed),
        patch("maestro.agent._git_pr_merged", return_value=True) as git_oracle,
    ):
        truth = agent.is_pr_settled(2954)
    assert truth.source == "git_subject"
    assert truth.branch_landed is True
    assert truth.state == "MERGED"
    assert truth.settled is True
    git_oracle.assert_called_once()


def test_is_pr_settled_closed_is_settled_not_landed(make_agent):  # type: ignore[no-untyped-def]
    """Host unmerged but the close oracle reports closed → CLOSED / settled."""
    agent = _make_real(make_agent)
    _stub_host(agent, merged=False, closed=True)
    with patch("maestro.agent.subprocess.run", side_effect=_completed):
        truth = agent.is_pr_settled(9)
    assert truth.state == "CLOSED"
    assert truth.settled is True
    assert truth.branch_landed is False


def test_is_pr_settled_squash_merge_mergedat_overrides_non_ancestor(make_agent):  # type: ignore[no-untyped-def]
    """A squash-merged PR settles via the host ``mergedAt`` override.

    Regression guard for the phantom-not-landed defect the loop hit on real PRs
    #3001 / #3043: a squash merge sets the code host's ``mergedAt`` (the PR
    genuinely landed on ``main``), yet the squash commit is **not** the branch
    head's linear ancestor — so a landing read shaped like ``git merge-base
    --is-ancestor HEAD origin/main`` exits 1 and the primary merged read
    (``is_pr_merged``) reports the branch unmerged. The squash-merge-aware
    chokepoint consults the authoritative ``has_merged_at`` override and stamps
    the PR settled / landed / MERGED, source ``host_api`` — so the already-merged
    PR is never re-routed as unlanded remediation.

    Before the fix this scenario returned OPEN / not-settled (the ``mergedAt``
    signal was never consulted); the assertions below lock in the corrected
    behavior.
    """
    agent = _make_real(make_agent)
    pr = 3001
    # Primary merged read is squash-blind: a squash commit is not the branch
    # head's linear ancestor, so ``--is-ancestor`` exits 1 and it reports False.
    agent.evolution._host.is_pr_merged = MagicMock(return_value=False)
    # The PR is not closed-unmerged — it genuinely merged via a squash commit.
    agent.evolution._host.is_pr_closed = MagicMock(return_value=False)
    # Ground truth: the code host records a non-null ``mergedAt`` (merged). The
    # squash-merge override consults this and promotes the verdict to MERGED.
    has_merged_at = MagicMock(return_value=True)
    agent.evolution._host.has_merged_at = has_merged_at
    with patch("maestro.agent.subprocess.run", side_effect=_completed):
        truth = agent.is_pr_settled(pr)
    # The squash-merged PR is correctly observed as settled / landed / MERGED.
    assert truth.settled is True
    assert truth.state == "MERGED"
    assert truth.branch_landed is True
    assert truth.source == "host_api"
    # The override consulted the authoritative ``mergedAt`` signal exactly once.
    has_merged_at.assert_called_once_with(pr)


def test_is_pr_settled_memoized_within_ttl(make_agent):  # type: ignore[no-untyped-def]
    """A second observation of the same PR within the TTL reuses the memo —
    no second fetch, no second host call."""
    agent = _make_real(make_agent)
    _stub_host(agent, merged=True)
    clock = _Clock(1000.0)
    with (
        patch("maestro.agent.subprocess.run", side_effect=_completed) as run,
        patch("maestro.agent.time.time", clock),
    ):
        first = agent.is_pr_settled(2662)
        second = agent.is_pr_settled(2662)
    assert first is second
    fetches = [c for c in run.call_args_list if c.args[0][:3] == ["git", "fetch", "origin"]]
    assert len(fetches) == 1
    assert agent.evolution._host.is_pr_merged_strict.call_count == 1


def test_is_pr_settled_reobserves_past_ttl(make_agent):  # type: ignore[no-untyped-def]
    """A memoized fact older than the TTL is re-observed (re-fetched), not reused."""
    agent = _make_real(make_agent)
    _stub_host(agent, merged=True)
    agent.config.merge_truth_ttl_seconds = 300
    clock = _Clock(1000.0)
    with (
        patch("maestro.agent.subprocess.run", side_effect=_completed) as run,
        patch("maestro.agent.time.time", clock),
    ):
        agent.is_pr_settled(2662)
        clock.value = 2000.0  # advance well past the 300s TTL
        agent.is_pr_settled(2662)
    fetches = [c for c in run.call_args_list if c.args[0][:3] == ["git", "fetch", "origin"]]
    assert len(fetches) == 2
    assert agent.evolution._host.is_pr_merged_strict.call_count == 2


def test_is_pr_settled_zero_ttl_always_reobserves(make_agent):  # type: ignore[no-untyped-def]
    """``merge_truth_ttl_seconds == 0`` forces a fresh observation every call."""
    agent = _make_real(make_agent)
    _stub_host(agent, merged=True)
    agent.config.merge_truth_ttl_seconds = 0
    clock = _Clock(1000.0)
    with (
        patch("maestro.agent.subprocess.run", side_effect=_completed) as run,
        patch("maestro.agent.time.time", clock),
    ):
        agent.is_pr_settled(2662)
        agent.is_pr_settled(2662)
    fetches = [c for c in run.call_args_list if c.args[0][:3] == ["git", "fetch", "origin"]]
    assert len(fetches) == 2


def test_two_faces_share_per_iteration_memo(make_agent):  # type: ignore[no-untyped-def]
    """Two routed faces asking about the same PR in one iteration share the
    memo: one fetch, one host call (proposal 087).

    The task-entry stand-down preflight observes first; a later face (here a
    direct ``is_pr_settled`` read, standing in for the dequeue seam) reuses the
    memoized fact without re-fetching — the per-PR-per-iteration guarantee."""
    agent = _make_real(make_agent)
    agent.config.merge_truth_ttl_seconds = 300
    _stub_host(agent, merged=True)
    clock = _Clock(1000.0)
    with (
        patch("maestro.agent.subprocess.run", side_effect=_completed) as run,
        patch("maestro.agent.time.time", clock),
    ):
        first = agent._preflight_merge_truth_standdown("Remediate PR #2662: x")
        truth = agent.is_pr_settled(2662)
    assert first == 2662
    assert truth.branch_landed is True
    fetches = [
        c for c in run.call_args_list
        if c.args[0][:3] == ["git", "fetch", "origin"]
    ]
    assert len(fetches) == 1
    assert agent.evolution._host.is_pr_merged_strict.call_count == 1


# ── Behavioral: the chokepoint derives merge truth from the live, fetch-first
#    observation, NEVER from a stored verdict stamp ────────────────────────────


def _merged_git_router(pr_number: int):  # type: ignore[no-untyped-def]
    """A ``subprocess.run`` side-effect: benign fetch, git-log shows PR merged.

    Every git call the chokepoint makes routes here — no real process runs. The
    fetch-first ``git fetch origin`` returns clean, and the git-subject
    merge-truth query (``git log origin/main`` in ``_git_pr_merged_default``)
    reports a GitHub landing subject for *pr_number*, so the branch oracle
    observes the PR merged. Any other git call degrades to a benign success.
    """

    def _run(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if cmd[:3] == ["git", "fetch", "origin"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["git", "log"]:
            landed = f"{'a' * 40}\x00Merge pull request #{pr_number} from x/y"
            return subprocess.CompletedProcess(cmd, 0, landed + "\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return _run


def test_is_pr_settled_derives_from_live_git_not_stored_stamp(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
):
    """A stored not-landed stamp is ignored; the live fetch-first observation wins.

    Behavioral complement to the AST guard. The merge-truth invariant
    (proposal 087) is that "is this PR settled?" is *observed* from fresh remote
    state each iteration, never read back from a durable verdict stamp. Here a
    fetch-truth marker on disk records the PR as NOT landed
    (``is_ancestor=False``) — the kind of stamp a prior remediation lap persists
    — while live git now shows the PR merged. ``is_pr_settled`` must return
    ``settled`` / ``branch_landed`` from the live observation and must leave the
    stored stamp byte-for-byte untouched, proving the verdict is derived from the
    fetch-first check and never from the stamp.

    NOTE on ``git merge-base --is-ancestor``: the chokepoint's git fallback is
    the git-subject oracle (``_git_pr_merged`` → ``git log origin/main`` subject
    match), *not* ``--is-ancestor``. The ancestry read is the **sibling**
    fetch-truth path (``_branch_merged_into_main`` / ``record_fetch_truth``)
    whose stored ``is_ancestor`` marker this test seeds as not-landed and proves
    ``is_pr_settled`` never consults. All git I/O and the filesystem are mocked
    per Testing Principles (no real processes, no real disk).
    """
    agent = _make_real(make_agent)
    pr = 2662

    # A durable stored verdict stamp that reads NOT-LANDED (the ancestry check on
    # some earlier lap said the PR was not on ``origin/main``).
    record_fetch_truth(
        "remediate-2662", pr, False, agent.config.memory_dir, head_sha="b" * 40,
    )
    marker_path = str(agent.config.memory_dir / _FETCH_TRUTH_FILE)
    stamp_before = mem_fs[marker_path]
    assert json.loads(stamp_before.splitlines()[0])["is_ancestor"] is False

    # Host API unreachable → the fetch-first git fallback authors the verdict.
    agent.evolution._host.is_pr_merged = MagicMock(
        side_effect=RuntimeError("host down"),
    )
    agent.evolution._host.is_pr_closed = MagicMock(return_value=False)

    router = _merged_git_router(pr)
    # ``maestro.agent`` and ``maestro.audit_checks`` share the one ``subprocess``
    # module object, so a single ``.run`` patch intercepts both the agent's
    # ``git fetch origin`` and the audit-checks ``git log`` merge-truth query.
    with patch("maestro.agent.subprocess.run", side_effect=router) as git_run:
        truth = agent.is_pr_settled(pr)

    git_calls = [c.args[0] for c in git_run.call_args_list]
    # Fetch-first: the chokepoint freshened refs before observing merge state.
    assert git_calls[0][:3] == ["git", "fetch", "origin"]
    # The git-subject merge-truth oracle was the one consulted (host raised).
    assert any(c[:2] == ["git", "log"] for c in git_calls)

    # Live git wins: the PR is settled and landed, sourced from the git
    # observation — despite the stored stamp reading not-landed.
    assert truth.settled is True
    assert truth.branch_landed is True
    assert truth.state == "MERGED"
    assert truth.source == "git_subject"

    # The stored not-landed stamp is byte-for-byte untouched: the chokepoint
    # neither read it to decide nor rewrote it — the verdict came from live git.
    assert mem_fs[marker_path] == stamp_before
    assert has_fetch_truth("remediate-2662", agent.config.memory_dir) is True
    assert json.loads(mem_fs[marker_path].splitlines()[0])["is_ancestor"] is False


# ── Behavioral: the chokepoint is squash-merge-aware — a non-null host
#    ``mergedAt`` overrides a not-ancestor linear read (the squash-merge case) ──


def test_is_pr_settled_genuinely_unmerged_no_mergedat_stays_unsettled(  # type: ignore[no-untyped-def]
    make_agent,
):
    """A null host ``mergedAt`` does NOT settle a genuinely-unmerged PR (crit. 2).

    No false-positive settlement: primary read not-merged AND ``mergedAt`` null →
    the override finds nothing to promote and the PR stays OPEN / unsettled /
    unlanded.
    """
    agent = _make_real(make_agent)
    pr = 3002

    agent.evolution._host.is_pr_merged = MagicMock(return_value=False)
    agent.evolution._host.has_merged_at = MagicMock(return_value=False)
    agent.evolution._host.is_pr_closed = MagicMock(return_value=False)

    with patch("maestro.agent.subprocess.run", side_effect=_completed):
        truth = agent.is_pr_settled(pr)

    assert truth.settled is False
    assert truth.branch_landed is False
    assert truth.state == "OPEN"
    agent.evolution._host.has_merged_at.assert_called_once_with(pr)


def test_is_pr_settled_squash_fallback_reads_fresh_per_iteration_host_state(  # type: ignore[no-untyped-def]
    make_agent,
):
    """The squash override reads fresh per-iteration host state, not a stamp (crit. 3).

    The chokepoint memoizes one observation per iteration. A first iteration
    observes ``mergedAt`` null → OPEN. A fresh iteration clears the per-iteration
    merge-truth memo (``run_loop``'s reset), the host now records the squash
    merge, and the second observation returns MERGED — proving the fallback
    re-reads live host state each iteration rather than returning a stale
    verdict. The ``has_merged_at`` oracle is consulted once per observation.
    """
    agent = _make_real(make_agent)
    pr = 3043

    agent.evolution._host.is_pr_merged = MagicMock(return_value=False)
    agent.evolution._host.is_pr_closed = MagicMock(return_value=False)
    has_merged_at = MagicMock(return_value=False)
    agent.evolution._host.has_merged_at = has_merged_at

    with patch("maestro.agent.subprocess.run", side_effect=_completed):
        first = agent.is_pr_settled(pr)
    assert first.settled is False
    assert first.state == "OPEN"

    # A fresh loop iteration clears the per-iteration merge-truth memo (the same
    # reset ``run_loop`` performs); the squash merge is observed only now.
    agent._merge_truth_memo.clear()
    has_merged_at.return_value = True

    with patch("maestro.agent.subprocess.run", side_effect=_completed):
        second = agent.is_pr_settled(pr)
    assert second.settled is True
    assert second.state == "MERGED"

    # Live host state authored each verdict — the oracle was re-consulted on the
    # fresh iteration, never read back from a persisted stamp.
    assert has_merged_at.call_count == 2


# ── Behavioral: the chokepoint's host-independent version-archive corroboration
#    tier — a matching VersionRecord settles a squash PR whose host mergedAt is
#    null/unreadable and whose commit is not the branch head's linear ancestor ──


def _impl_plan_for_pr(pr_num: int, plan_id: str) -> EvolutionPlan:
    """An ``implemented`` plan whose ``pr_url`` resolves to *pr_num*."""
    return EvolutionPlan(
        plan_id=plan_id,
        title="Version-archive corroboration",
        category="capability",
        rationale="wire the local landing signal into settlement",
        proposed_changes=["change one"],
        files_affected=["maestro/remediation_tracker.py"],
        expected_impact="steadier",
        status="implemented",
        pr_url=f"https://github.com/o/r/pull/{pr_num}",
    )


def _landed_version(plan_id: str) -> VersionRecord:
    """A ``VersionRecord`` (written only on a real landing) for *plan_id*."""
    return VersionRecord(
        version_id=f"v-{plan_id}",
        plan_id=plan_id,
        plan_title="Version-archive corroboration",
        commit_sha="c" * 40,
        merged_at="2026-07-23T00:00:00+00:00",
        window_tasks_total=1,
        window_tasks_succeeded=1,
        window_tasks_failed=0,
        window_sdk_cost_usd=0.0,
        window_sdk_calls=0,
        window_cache_hit_rate=0.0,
        window_rate_limit_hits=0,
        window_error_count=0,
        quality_gate_pass=True,
        notes=None,
    )


def test_is_pr_settled_version_archive_corroborates_null_mergedat(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
):
    """A matching VersionRecord settles a squash PR with null mergedAt (crit. 1).

    The squash-merge case the host ``mergedAt`` override cannot reach: the
    primary merged read reports not-merged (the ``--is-ancestor`` exit-1 shape),
    the host ``mergedAt`` is null, yet a ``VersionRecord`` for the backing plan —
    written only on an actual landing — sits on local disk. The host-independent
    version-archive corroboration tier settles the PR, stamping provenance
    ``version-archive``. All git I/O and the filesystem are mocked.
    """
    agent = _make_real(make_agent)
    pr = 3179
    plan_id = "ep-3179"

    agent.memory.save_evolution_plan(_impl_plan_for_pr(pr, plan_id))
    agent.memory.record_version(_landed_version(plan_id))

    # Primary read not-merged AND host mergedAt null/unreadable — only the
    # local version-archive corroboration can settle the PR.
    agent.evolution._host.is_pr_merged = MagicMock(return_value=False)
    agent.evolution._host.has_merged_at = MagicMock(return_value=False)
    agent.evolution._host.is_pr_closed = MagicMock(return_value=False)

    with patch("maestro.agent.subprocess.run", side_effect=_completed):
        truth = agent.is_pr_settled(pr)

    assert truth.settled is True
    assert truth.branch_landed is True
    assert truth.state == "MERGED"
    assert truth.source == "version-archive"
    # Corroboration runs strictly after the fetch + host mergedAt reads.
    agent.evolution._host.has_merged_at.assert_called_once_with(pr)


def test_is_pr_settled_no_version_record_stays_unsettled(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
):
    """No mergedAt and no matching VersionRecord → unchanged / unsettled (crit. 2).

    The plan is present (so the PR maps to a plan_id) but ``version_archive.jsonl``
    carries no record for it — nothing landed on disk — and the host ``mergedAt``
    is null. The corroboration finds nothing to promote and the PR stays OPEN /
    unsettled / unlanded, exactly as before this tier existed.
    """
    agent = _make_real(make_agent)
    pr = 3181
    plan_id = "ep-3181"

    # A plan maps the PR to a plan_id, but NO VersionRecord is written for it.
    agent.memory.save_evolution_plan(_impl_plan_for_pr(pr, plan_id))

    agent.evolution._host.is_pr_merged = MagicMock(return_value=False)
    agent.evolution._host.has_merged_at = MagicMock(return_value=False)
    agent.evolution._host.is_pr_closed = MagicMock(return_value=False)

    with patch("maestro.agent.subprocess.run", side_effect=_completed):
        truth = agent.is_pr_settled(pr)

    assert truth.settled is False
    assert truth.branch_landed is False
    assert truth.state == "OPEN"


def test_version_archive_settled_skips_unmappable_plans(  # type: ignore[no-untyped-def]
    make_agent, mem_fs,
):
    """The corroboration returns False when no plan maps to the PR.

    Covers the plan→PR mapping guards in ``_version_archive_settled``: a plan
    with no ``pr_url`` is skipped, a plan whose ``pr_url`` is unparseable is
    skipped (``ValueError``), a plan whose ``pr_url`` *parses to a different PR*
    is skipped (the ``== pr_num`` False loop-back edge), and when nothing maps to
    the queried PR the method short-circuits to ``False`` without ever reading
    the archive. Called directly — the mapping does no git I/O.
    """
    agent = make_agent()
    no_url = _impl_plan_for_pr(1, "ep-no-url")
    no_url.pr_url = None
    bad_url = _impl_plan_for_pr(2, "ep-bad-url")
    bad_url.pr_url = "not-a-pr-url"
    # A valid ``pr_url`` that resolves to a *different* PR than the query: the
    # loop parses it, finds ``1234 != 9999``, and continues to the next plan
    # (the ``7596->7592`` back-edge) rather than matching.
    other_pr = _impl_plan_for_pr(1234, "ep-other-pr")
    agent.memory.save_evolution_plan(no_url)
    agent.memory.save_evolution_plan(bad_url)
    agent.memory.save_evolution_plan(other_pr)

    assert agent._version_archive_settled(9999) is False


def test_fetch_origin_for_merge_truth_logs_git_stderr_on_failure(  # type: ignore[no-untyped-def]
    make_agent, caplog,
):
    """A fetch exit-128 logs git's stderr at ERROR before re-raising.

    Regression for #2855: a stale ``GITHUB_TOKEN`` makes the fetch-first
    merge-truth chokepoint's ``git fetch origin`` exit 128, and the loop is
    designed to die hard there. The defect was that git's captured stderr — the
    ``Authentication failed`` line naming the credential problem — was never
    surfaced, so the crash record showed only ``exit status 128``. The fix logs
    that stderr before re-raising: fail-loud is preserved (the
    ``CalledProcessError`` still propagates) but the failure is now
    fail-*informative*.
    """
    agent = make_agent()
    stderr = "fatal: Authentication failed for 'https://github.com/o/r.git'"
    boom = subprocess.CalledProcessError(
        128, ["git", "fetch", "origin"], stderr=stderr,
    )
    with (
        patch("maestro.agent.subprocess.run", side_effect=boom),
        caplog.at_level(logging.ERROR, logger="maestro"),
        pytest.raises(subprocess.CalledProcessError) as exc_info,
    ):
        agent._fetch_origin_for_merge_truth()

    # (a) fail-loud preserved — the exact exception re-raises.
    assert exc_info.value.returncode == 128
    # (b) git's stderr was surfaced at ERROR before the re-raise.
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("Authentication failed" in r.getMessage() for r in error_records)

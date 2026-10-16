"""Tests for the grounded merge-truth fact — now homed in :mod:`maestro.pr_facts`.

Proposal 087 / 106. The observer is pure and fully injectable, so every test
wires plain callables (no real network, no real git, no real clock) and asserts
on the returned :class:`MergeTruth` and on the order in which the injected
side effects fire. Retained after proposal 106 folded ``world_state.py`` into
``pr_facts.py`` as the fact's parity corpus, proving the relocated observer
behaves identically at the new import path.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest

from maestro.pr_facts import MergeTruth, observe_pr_merge_truth


def _fixed_clock(value: float) -> Callable[[], float]:
    """A zero-argument clock that always returns *value*."""
    return lambda: value


def _raise(_pr: int) -> bool:
    """An oracle that always raises — stands in for an unreachable backend."""
    raise RuntimeError("backend unreachable")


def test_observe_merged_via_host_api() -> None:
    """Host API reporting merged → MERGED / settled / landed, source host_api."""
    truth = observe_pr_merge_truth(
        2662,
        fetch=lambda: None,
        host_oracle=lambda _pr: True,
        branch_oracle=_raise,
        now=_fixed_clock(100.0),
    )
    assert truth == MergeTruth(
        pr_number=2662,
        settled=True,
        state="MERGED",
        branch_landed=True,
        source="host_api",
        observed_at=100.0,
    )


def test_observe_open_via_host_api_no_close_oracle() -> None:
    """Host reports unmerged and no close oracle → OPEN / unsettled / unlanded."""
    truth = observe_pr_merge_truth(
        7,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(5.0),
    )
    assert truth.state == "OPEN"
    assert truth.settled is False
    assert truth.branch_landed is False
    assert truth.source == "host_api"
    assert truth.observed_at == 5.0


def test_observe_closed_via_close_oracle() -> None:
    """Unmerged but the close oracle reports closed → CLOSED / settled / unlanded."""
    truth = observe_pr_merge_truth(
        9,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(1.0),
        close_oracle=lambda _pr: True,
    )
    assert truth.state == "CLOSED"
    assert truth.settled is True
    assert truth.branch_landed is False
    assert truth.source == "host_api"


def test_observe_open_when_close_oracle_reports_open() -> None:
    """Unmerged and the close oracle reports still-open → OPEN / unsettled."""
    truth = observe_pr_merge_truth(
        9,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(1.0),
        close_oracle=lambda _pr: False,
    )
    assert truth.state == "OPEN"
    assert truth.settled is False


def test_fetch_runs_first_and_is_fail_loud() -> None:
    """A raising fetch propagates and no oracle is ever consulted."""
    consulted: list[str] = []

    def _boom() -> None:
        raise RuntimeError("network down")

    def _record_host(_pr: int) -> bool:
        consulted.append("host")
        return True

    with pytest.raises(RuntimeError, match="network down"):
        observe_pr_merge_truth(
            1,
            fetch=_boom,
            host_oracle=_record_host,
            branch_oracle=_raise,
            now=_fixed_clock(0.0),
        )
    assert consulted == []


def test_fetch_called_before_oracles() -> None:
    """The fetch side effect fires before the merge oracle is consulted."""
    order: list[str] = []

    def _fetch() -> None:
        order.append("fetch")

    def _host(_pr: int) -> bool:
        order.append("host")
        return True

    observe_pr_merge_truth(
        1,
        fetch=_fetch,
        host_oracle=_host,
        branch_oracle=_raise,
        now=_fixed_clock(0.0),
    )
    assert order == ["fetch", "host"]


def test_host_raise_falls_back_to_branch_oracle() -> None:
    """Host unreachable → branch oracle answers, stamping the default git source."""
    truth = observe_pr_merge_truth(
        42,
        fetch=lambda: None,
        host_oracle=_raise,
        branch_oracle=lambda _pr: True,
        now=_fixed_clock(3.0),
    )
    assert truth.state == "MERGED"
    assert truth.branch_landed is True
    assert truth.source == "git_ancestry"


def test_branch_source_override_stamps_git_subject() -> None:
    """A subject-based branch oracle stamps source ``git_subject`` on fallback."""
    truth = observe_pr_merge_truth(
        42,
        fetch=lambda: None,
        host_oracle=_raise,
        branch_oracle=lambda _pr: False,
        now=_fixed_clock(3.0),
        branch_source="git_subject",
    )
    assert truth.state == "OPEN"
    assert truth.source == "git_subject"


def test_both_oracles_raise_propagates() -> None:
    """When host and branch oracles both fail, no verdict is authored."""
    with pytest.raises(RuntimeError, match="backend unreachable"):
        observe_pr_merge_truth(
            1,
            fetch=lambda: None,
            host_oracle=_raise,
            branch_oracle=_raise,
            now=_fixed_clock(0.0),
        )


def test_close_oracle_raise_degrades_to_open() -> None:
    """A raising close oracle is fail-safe: the PR degrades to OPEN, not a crash."""
    truth = observe_pr_merge_truth(
        9,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(2.0),
        close_oracle=_raise,
    )
    assert truth.state == "OPEN"
    assert truth.settled is False


def test_merged_at_oracle_overrides_unmerged_to_merged() -> None:
    """A non-null host ``mergedAt`` promotes an unmerged verdict to MERGED.

    The squash-merge override: the primary merged read (host or branch fallback)
    reports not-merged — the shape a ``merge-base --is-ancestor`` landing signal
    takes for a squash commit — yet the authoritative host ``mergedAt`` is set,
    so the PR settles/lands and the source is re-stamped ``host_api``.
    """
    truth = observe_pr_merge_truth(
        3001,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(7.0),
        merged_at_oracle=lambda _pr: True,
    )
    assert truth.state == "MERGED"
    assert truth.settled is True
    assert truth.branch_landed is True
    assert truth.source == "host_api"
    assert truth.observed_at == 7.0


def test_merged_at_oracle_null_leaves_pr_unmerged() -> None:
    """A null host ``mergedAt`` does not promote a genuinely-unmerged PR.

    No false-positive settlement: the override consults ``mergedAt``, finds it
    null, and the PR stays OPEN / unsettled / unlanded.
    """
    truth = observe_pr_merge_truth(
        3002,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(8.0),
        merged_at_oracle=lambda _pr: False,
    )
    assert truth.state == "OPEN"
    assert truth.settled is False
    assert truth.branch_landed is False


def test_merged_at_oracle_raise_degrades_to_not_merged() -> None:
    """A raising ``mergedAt`` oracle is fail-safe: the not-merged verdict stands.

    Never a crashed observation and never a false ``MERGED`` — the override
    degrades to the already-computed not-merged verdict, mirroring
    ``close_oracle``'s fail-safe.
    """
    truth = observe_pr_merge_truth(
        3003,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(9.0),
        merged_at_oracle=_raise,
    )
    assert truth.state == "OPEN"
    assert truth.settled is False


def test_merged_at_oracle_not_consulted_when_already_merged() -> None:
    """The override short-circuits: a merged primary verdict skips ``mergedAt``.

    ``not merged`` is already ``False``, so the ``mergedAt`` oracle is never
    called — proven by wiring a raising oracle that would blow up if consulted.
    """
    truth = observe_pr_merge_truth(
        3004,
        fetch=lambda: None,
        host_oracle=lambda _pr: True,
        branch_oracle=_raise,
        now=_fixed_clock(10.0),
        merged_at_oracle=_raise,
    )
    assert truth.state == "MERGED"
    assert truth.source == "host_api"


def test_merged_at_override_stamps_host_api_over_branch_fallback() -> None:
    """The override re-stamps ``host_api`` even after a branch-fallback source.

    Host unreachable → branch oracle answers not-merged (``git_subject``); the
    ``mergedAt`` read then authors the merged verdict, so the source reflects the
    host ``mergedAt`` observation, not the stale branch fallback.
    """
    truth = observe_pr_merge_truth(
        3005,
        fetch=lambda: None,
        host_oracle=_raise,
        branch_oracle=lambda _pr: False,
        now=_fixed_clock(11.0),
        branch_source="git_subject",
        merged_at_oracle=lambda _pr: True,
    )
    assert truth.state == "MERGED"
    assert truth.branch_landed is True
    assert truth.source == "host_api"


def test_version_archive_oracle_settles_unmerged_pr() -> None:
    """A matching ``VersionRecord`` settles a PR the ancestry+mergedAt reads miss.

    The host-independent corroboration tier: the primary merged read reports
    not-merged (the squash-commit ``--is-ancestor`` shape) AND the host
    ``mergedAt`` override finds nothing, yet a ``VersionRecord`` written on the
    actual landing exists on local disk — so the PR settles/lands, stamped
    ``version-archive``.
    """
    truth = observe_pr_merge_truth(
        3179,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(12.0),
        merged_at_oracle=lambda _pr: False,
        version_archive_oracle=lambda _pr: True,
    )
    assert truth.state == "MERGED"
    assert truth.settled is True
    assert truth.branch_landed is True
    assert truth.source == "version-archive"
    assert truth.observed_at == 12.0


def test_version_archive_oracle_absent_record_leaves_pr_unmerged() -> None:
    """No matching ``VersionRecord`` → the PR stays OPEN / unsettled / unlanded.

    No false-positive settlement: with the ancestry read, the ``mergedAt``
    override, and the version-archive corroboration all empty, the PR is still
    owed remediation.
    """
    truth = observe_pr_merge_truth(
        3180,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(13.0),
        merged_at_oracle=lambda _pr: False,
        version_archive_oracle=lambda _pr: False,
    )
    assert truth.state == "OPEN"
    assert truth.settled is False
    assert truth.branch_landed is False


def test_version_archive_oracle_raise_degrades_to_not_merged() -> None:
    """A raising version-archive oracle is fail-safe: the not-merged verdict stands.

    Never a crashed observation and never a false ``MERGED`` — the corroboration
    degrades to the already-computed not-merged verdict, mirroring
    ``merged_at_oracle`` / ``close_oracle``.
    """
    truth = observe_pr_merge_truth(
        3181,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(14.0),
        merged_at_oracle=lambda _pr: False,
        version_archive_oracle=_raise,
    )
    assert truth.state == "OPEN"
    assert truth.settled is False


def test_version_archive_oracle_not_consulted_when_mergedat_settles() -> None:
    """The host ``mergedAt`` override short-circuits the version-archive tier.

    When ``mergedAt`` already promotes the PR to MERGED, the version-archive
    oracle is never called — proven by wiring a raising oracle that would blow
    up if consulted — so the host answer wins its ``host_api`` provenance.
    """
    truth = observe_pr_merge_truth(
        3182,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(15.0),
        merged_at_oracle=lambda _pr: True,
        version_archive_oracle=_raise,
    )
    assert truth.state == "MERGED"
    assert truth.source == "host_api"


def test_version_archive_corroboration_runs_after_fetch_and_ancestry() -> None:
    """Fetch + ancestry read fire before the version-archive corroboration.

    Criterion 3: the corroboration executes strictly after the fetch-first
    ancestry read. The injected side effects record their order, and the
    version-archive oracle is last — proving the fetch action-zero and the
    merge/mergedAt reads all precede the local-disk corroboration.
    """
    order: list[str] = []

    def _fetch() -> None:
        order.append("fetch")

    def _host(_pr: int) -> bool:
        order.append("ancestry")
        return False

    def _mergedat(_pr: int) -> bool:
        order.append("mergedAt")
        return False

    def _version_archive(_pr: int) -> bool:
        order.append("version-archive")
        return True

    truth = observe_pr_merge_truth(
        3183,
        fetch=_fetch,
        host_oracle=_host,
        branch_oracle=_raise,
        now=_fixed_clock(16.0),
        merged_at_oracle=_mergedat,
        version_archive_oracle=_version_archive,
    )
    assert order == ["fetch", "ancestry", "mergedAt", "version-archive"]
    assert truth.source == "version-archive"


def test_observed_at_stamped_from_injected_clock() -> None:
    """``observed_at`` is read from the injected clock, not wall time."""
    truth = observe_pr_merge_truth(
        1,
        fetch=lambda: None,
        host_oracle=lambda _pr: False,
        branch_oracle=_raise,
        now=_fixed_clock(1234.5),
    )
    assert truth.observed_at == 1234.5


def test_merge_truth_is_frozen() -> None:
    """:class:`MergeTruth` is immutable — a verdict cannot be edited in place."""
    truth = observe_pr_merge_truth(
        1,
        fetch=lambda: None,
        host_oracle=lambda _pr: True,
        branch_oracle=_raise,
        now=_fixed_clock(0.0),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        truth.settled = False  # type: ignore[misc]


def _truth_at(observed_at: float) -> MergeTruth:
    """A minimal :class:`MergeTruth` stamped at *observed_at* for TTL tests."""
    return MergeTruth(
        pr_number=1,
        settled=True,
        state="MERGED",
        branch_landed=True,
        source="host_api",
        observed_at=observed_at,
    )


def test_is_fresh_within_ttl() -> None:
    """An observation younger than the TTL is fresh and may author a verdict."""
    truth = _truth_at(100.0)
    assert truth.is_fresh(now=150.0, ttl_seconds=300) is True


def test_is_fresh_past_ttl_is_stale() -> None:
    """An observation older than the TTL is stale — the chokepoint re-observes."""
    truth = _truth_at(100.0)
    assert truth.is_fresh(now=500.0, ttl_seconds=300) is False


def test_is_fresh_exactly_at_ttl_boundary_is_stale() -> None:
    """The boundary is exclusive: an age equal to the TTL is already stale."""
    truth = _truth_at(100.0)
    assert truth.is_fresh(now=400.0, ttl_seconds=300) is False


def test_is_fresh_zero_ttl_always_stale() -> None:
    """``ttl_seconds == 0`` forces a fresh observation on every call."""
    truth = _truth_at(100.0)
    assert truth.is_fresh(now=100.0, ttl_seconds=0) is False


def test_is_fresh_clock_not_advanced_is_fresh() -> None:
    """Within-iteration reuse (clock unmoved) keeps the observation fresh."""
    truth = _truth_at(100.0)
    assert truth.is_fresh(now=100.0, ttl_seconds=300) is True

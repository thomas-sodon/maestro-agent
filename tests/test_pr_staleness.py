"""Tests for the grounded PR-staleness fact — now homed in :mod:`maestro.pr_facts`.

Proposal 095 / 106. The observer is pure and fully injectable, so every test
wires plain lambda oracles (no ``mem_fs``, no real git, no network, no clock)
and asserts on the returned :class:`PrStaleness`. Modelled on
``tests/test_pr_readiness.py``. Retained after proposal 106 folded
``pr_staleness.py`` into ``pr_facts.py`` as the fact's parity corpus, proving
the relocated observer behaves identically at the new import path.
"""

from __future__ import annotations

import dataclasses

import pytest

from maestro.pr_facts import PrStaleness, observe_pr_staleness


def _observe(
    *,
    branch: str = "maestro/evolve/x",
    commits_behind: int = 0,
    cleanly_mergeable: bool = True,
    base_head_sha: str | None = "basesha0000",
) -> PrStaleness:
    """Observe a branch with lambda oracles surfacing the given git facts."""
    return observe_pr_staleness(
        branch,
        commits_behind_fn=lambda _b: commits_behind,
        merge_clean_fn=lambda _b: cleanly_mergeable,
        base_sha_fn=lambda: base_head_sha,
    )


@pytest.mark.parametrize(
    ("commits_behind", "cleanly_mergeable", "expected_catchup"),
    [
        # Behind and cleanly mergeable — the one state a mechanical catch-up
        # merge both needs and can perform.
        (3, True, True),
        # Behind but conflicting — needs the LLM conflict route, not a merge.
        (2, False, False),
        # Level with origin/main — nothing to catch up.
        (0, True, False),
        # Not-behind wins whatever the clean-merge probe would say (it is not
        # even consulted when commits_behind == 0).
        (0, False, False),
    ],
)
def test_needs_catchup_truth_table(
    commits_behind: int, cleanly_mergeable: bool, expected_catchup: bool,
) -> None:
    """``needs_catchup`` is ``commits_behind > 0 and cleanly_mergeable``."""
    staleness = _observe(
        commits_behind=commits_behind, cleanly_mergeable=cleanly_mergeable,
    )
    assert staleness.needs_catchup is expected_catchup


def test_git_facts_are_carried_through_verbatim() -> None:
    """The two measured git facts land on the frozen fact unchanged."""
    staleness = _observe(commits_behind=4, cleanly_mergeable=True)
    assert staleness.commits_behind == 4
    assert staleness.cleanly_mergeable is True


def test_branch_is_recorded_on_the_fact() -> None:
    """The observed branch name is recorded verbatim on the fact."""
    staleness = _observe(branch="maestro/evolve/feature-y")
    assert staleness.branch == "maestro/evolve/feature-y"


def test_current_branch_skips_merge_clean_probe() -> None:
    """An up-to-date branch is cleanly mergeable without running the probe.

    When ``commits_behind == 0`` nothing needs catching up and no consumer reads
    ``cleanly_mergeable``, so the observer must short-circuit the in-memory
    ``git merge-tree`` probe entirely and report ``cleanly_mergeable`` ``True``
    by definition — an empty merge cannot conflict. The spy raises if the probe
    is consulted, so reaching a fact proves it was skipped.
    """

    def _clean_spy(_branch: str) -> bool:
        raise AssertionError(
            "merge_clean_fn must not run when commits_behind == 0",
        )

    staleness = observe_pr_staleness(
        "maestro/evolve/current",
        commits_behind_fn=lambda _b: 0,
        merge_clean_fn=_clean_spy,
        base_sha_fn=lambda: "basesha0000",
    )
    assert staleness.cleanly_mergeable is True
    assert staleness.needs_catchup is False


def test_observer_only_calls_injected_git_oracles() -> None:
    """The observer's sole collaborators are the three injected git oracles.

    Each branch spy raises if handed anything but the branch name, the base spy
    takes no argument (the base is ``origin/main`` regardless of branch), and
    the observer has no other injectable input (no host client, SDK session, or
    clock), so reaching a :class:`PrStaleness` proves it performed no host/SDK
    call — it consulted only local git through the three adapters.
    """
    calls: list[tuple[str, str]] = []

    def _behind_spy(branch: str) -> int:
        if branch != "maestro/evolve/only":
            raise AssertionError(f"unexpected branch: {branch!r}")
        calls.append(("behind", branch))
        return 5

    def _clean_spy(branch: str) -> bool:
        if branch != "maestro/evolve/only":
            raise AssertionError(f"unexpected branch: {branch!r}")
        calls.append(("clean", branch))
        return True

    def _base_spy() -> str:
        # No branch argument — the base comparand is fixed at ``origin/main``.
        calls.append(("base", ""))
        return "basesha0000"

    staleness = observe_pr_staleness(
        "maestro/evolve/only",
        commits_behind_fn=_behind_spy,
        merge_clean_fn=_clean_spy,
        base_sha_fn=_base_spy,
    )
    assert calls == [
        ("behind", "maestro/evolve/only"),
        ("clean", "maestro/evolve/only"),
        ("base", ""),
    ]
    assert staleness.needs_catchup is True
    assert staleness.base_head_sha == "basesha0000"


def test_base_head_sha_is_carried_through_verbatim() -> None:
    """The base SHA the injected oracle yields lands on the fact unchanged
    (proposal 099 step 2)."""
    staleness = _observe(base_head_sha="abc123def456")
    assert staleness.base_head_sha == "abc123def456"


def test_base_head_sha_none_round_trips() -> None:
    """A ``None`` from the base oracle (unresolvable ``origin/main``) round-trips
    as ``base_head_sha is None`` — the best-effort posture of ``rev_parse``."""
    staleness = _observe(base_head_sha=None)
    assert staleness.base_head_sha is None


def test_staleness_fact_is_frozen() -> None:
    """:class:`PrStaleness` is immutable — a fact cannot be edited in place."""
    staleness = _observe()
    with pytest.raises(dataclasses.FrozenInstanceError):
        staleness.base_head_sha = "mutated"  # type: ignore[misc]

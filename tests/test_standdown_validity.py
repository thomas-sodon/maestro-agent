"""Tests for the stand-down validity oracle (proposal 135).

The oracle names one contradiction and nothing more: a ``handoff-ready`` latch
standing over a live reading that says owned code work is owed. Its whole truth
table is pinned here — including every combination that must read ``HOLDS``,
since failing toward "the latch is doing its job" is the safety property a later
release path will lean on.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from maestro import standdown_validity
from maestro.pr_owed_act import OwedAct
from maestro.standdown_validity import (
    StanddownClaim,
    StanddownValidity,
    assess_standdown_validity,
    measure_contradicted_standdowns,
    measure_refuted_standdowns,
    parse_standdown_claim,
)
from tests.support.structural_guard import imported_modules


@pytest.mark.parametrize("raw", ["handoff-ready", "HANDOFF-READY", "  handoff-ready  "])
def test_parse_standdown_claim_reads_a_known_value(raw: str) -> None:
    """A known claim string parses to its member, case- and space-normalised."""
    assert parse_standdown_claim(raw) is StanddownClaim.HANDOFF_READY


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("reality-refuted", StanddownClaim.REALITY_REFUTED),
        ("retirement-pending", StanddownClaim.RETIREMENT_PENDING),
        ("escalation-claimed", StanddownClaim.ESCALATION_CLAIMED),
    ],
)
def test_parse_standdown_claim_covers_every_writer(
    raw: str, expected: StanddownClaim,
) -> None:
    """Each of the four ledger writers' stamped strings has a member."""
    assert parse_standdown_claim(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "   ", "not-a-claim", "handoff_ready"])
def test_parse_standdown_claim_degrades_to_unclaimed(raw: str | None) -> None:
    """An absent, empty or unrecognised value reads unclaimed, never defaulted."""
    assert parse_standdown_claim(raw) is None


def test_assess_refutes_a_handoff_claim_against_owned_code_work() -> None:
    """The single contradiction: handoff-ready latch, code owed live."""
    verdict = assess_standdown_validity(
        StanddownClaim.HANDOFF_READY, OwedAct.CODE_OWED,
    )
    assert verdict is StanddownValidity.REFUTED


@pytest.mark.parametrize(
    "live_act",
    [OwedAct.HANDOFF_READY, OwedAct.WAIT_CI, OwedAct.EXTERNAL_BLOCKED, None],
)
def test_assess_holds_a_handoff_claim_against_every_other_reading(
    live_act: OwedAct | None,
) -> None:
    """Still green, still on the CI clock, externally blocked, or settled — all hold."""
    verdict = assess_standdown_validity(StanddownClaim.HANDOFF_READY, live_act)
    assert verdict is StanddownValidity.HOLDS


@pytest.mark.parametrize(
    "claim",
    [
        StanddownClaim.REALITY_REFUTED,
        StanddownClaim.LAP_BUDGET_EXHAUSTED,
        StanddownClaim.RETIREMENT_PENDING,
        StanddownClaim.ESCALATION_CLAIMED,
        None,
    ],
)
@pytest.mark.parametrize(
    "live_act",
    [
        OwedAct.CODE_OWED,
        OwedAct.HANDOFF_READY,
        OwedAct.WAIT_CI,
        OwedAct.EXTERNAL_BLOCKED,
        None,
    ],
)
def test_assess_holds_for_every_non_handoff_claim(
    claim: StanddownClaim | None, live_act: OwedAct | None,
) -> None:
    """A non-handoff or unclaimed record is never refuted, whatever the reading.

    The full remaining truth table: five claim values (four non-handoff plus
    unclaimed) against all five readings. Only the handoff claim asserts the
    thing the owed-act oracle answers, so nothing else can be contradicted — a
    retirement-disposition record must not be released because code is owed.
    """
    assert assess_standdown_validity(claim, live_act) is StanddownValidity.HOLDS


def test_measure_returns_only_the_contradicted_subset() -> None:
    """Only a refuted handoff claim with a live reading contributes."""
    claims = {
        11: "handoff-ready",  # refuted — code owed
        12: "handoff-ready",  # holds — still green
        13: "retirement-pending",  # holds — wrong claim to refute
        14: "handoff-ready",  # absent from the owed-act map
        15: "not-a-claim",  # unclaimed after a tolerant parse
    }
    owed_acts = {
        11: OwedAct.CODE_OWED,
        12: OwedAct.HANDOFF_READY,
        13: OwedAct.CODE_OWED,
        15: OwedAct.CODE_OWED,
    }
    assert measure_contradicted_standdowns(claims, owed_acts) == {
        11: OwedAct.CODE_OWED,
    }


def test_measure_reports_nothing_when_no_claim_is_contradicted() -> None:
    """A fully-consistent ledger yields an empty map, not a falsy surprise."""
    claims = {21: "handoff-ready", 22: "escalation-claimed"}
    owed_acts = {21: OwedAct.WAIT_CI, 22: OwedAct.CODE_OWED}
    assert measure_contradicted_standdowns(claims, owed_acts) == {}


@pytest.mark.parametrize(
    ("claims", "owed_acts"),
    [
        ({}, {}),
        ({}, {31: OwedAct.CODE_OWED}),
        ({31: "handoff-ready"}, {}),
    ],
)
def test_measure_handles_empty_inputs(
    claims: dict[int, str], owed_acts: dict[int, OwedAct],
) -> None:
    """No standdowns, no snapshots, or neither — all measure empty."""
    assert measure_contradicted_standdowns(claims, owed_acts) == {}


#: Timestamps framing one latch: a snapshot before it, the latch, one after.
_BEFORE = "2026-01-01T00:00:00+00:00"
_LATCHED = "2026-01-02T00:00:00+00:00"
_AFTER = "2026-01-03T00:00:00+00:00"


def _history_record(pr: int, **snapshot: object) -> dict[str, object]:
    """A remediation task record carrying a persisted readiness snapshot."""
    fields: dict[str, object] = {
        "branch_conflicting": False,
        "branch_behind": False,
        "awaiting_approval": False,
        "ci_state": "GREEN",
        "unresolved_threads": 0,
        "unaddressed_overflow": 0,
        "settled": False,
    }
    fields.update(snapshot)
    return {
        "goal": f"Remediate PR #{pr}: clear the blocker",
        "pr_readiness_snapshot": fields,
    }


def test_measure_refuted_standdowns_names_a_contradicted_latch() -> None:
    """A handoff latch over a now-conflicting PR is refuted, end to end."""
    history = [_history_record(41, branch_conflicting=True)]
    assert measure_refuted_standdowns({41: "handoff-ready"}, history) == {41}


def test_measure_refuted_standdowns_holds_a_green_latch() -> None:
    """The same latch over a still-green PR is left alone."""
    assert measure_refuted_standdowns({41: "handoff-ready"}, [_history_record(41)]) == set()


def test_measure_refuted_standdowns_drops_a_settled_pr() -> None:
    """A merged/closed PR is owed nothing, so it refutes nothing."""
    history = [_history_record(41, branch_conflicting=True, settled=True)]
    assert measure_refuted_standdowns({41: "handoff-ready"}, history) == set()


def test_measure_refuted_standdowns_ignores_a_pr_absent_from_history() -> None:
    """Unmeasured is not refuted — a standdown with no snapshot stays quiet."""
    assert measure_refuted_standdowns({41: "handoff-ready"}, []) == set()


def test_measure_refuted_standdowns_reads_the_latest_snapshot() -> None:
    """A PR that has since rotated back to green is no longer refuted."""
    history = [
        _history_record(41, branch_conflicting=True),
        _history_record(41),
    ]
    assert measure_refuted_standdowns({41: "handoff-ready"}, history) == set()


def test_measure_refuted_standdowns_ignores_a_pre_latch_snapshot() -> None:
    """A snapshot observed before the latch cannot refute the claim it predates.

    Snapshots stop being stamped once the latch suppresses the remediation whose
    completion writes them, so without the ``delivered_at`` floor the "live"
    reading is older than the claim by construction — a stale pre-handoff
    conflicting lap would read as a refutation.
    """
    history = [
        _history_record(41, branch_conflicting=True) | {"timestamp": _BEFORE},
    ]
    assert measure_refuted_standdowns(
        {41: "handoff-ready"}, history, delivered_at={41: _LATCHED},
    ) == set()


def test_measure_refuted_standdowns_reads_a_post_latch_snapshot() -> None:
    """The same conflict observed *after* the latch does refute it."""
    history = [
        _history_record(41, branch_conflicting=True) | {"timestamp": _AFTER},
    ]
    assert measure_refuted_standdowns(
        {41: "handoff-ready"}, history, delivered_at={41: _LATCHED},
    ) == {41}


def test_measure_refuted_standdowns_unfiltered_without_a_floor() -> None:
    """An unparseable floor (or none at all) leaves the reading unfiltered."""
    history = [
        _history_record(41, branch_conflicting=True) | {"timestamp": _BEFORE},
    ]
    assert measure_refuted_standdowns(
        {41: "handoff-ready"}, history, delivered_at={41: "not-a-timestamp"},
    ) == {41}


def test_oracle_module_is_a_pure_leaf() -> None:
    """``ast``: the module reaches no I/O and never imports the ledger.

    Its only ``maestro`` imports are the owed-act composer and the
    latest-readiness reader that composer already depends on — so the dependency
    still runs one way and nothing new is coupled. Importing
    ``escalation_bridge`` would invite the sensor to *release* the latch, which
    is deliberately out of scope; the structural guard pins the same two
    properties from the registry, and this asserts them where the oracle is
    specified.
    """
    tree = ast.parse(inspect.getsource(standdown_validity))
    resolved = imported_modules(tree, "maestro")
    assert "maestro.escalation_bridge" not in resolved
    # Every spelling resolves to ``maestro.<module>[.<name>]``; compare at module
    # granularity so an imported symbol does not read as a new dependency.
    maestro_imports = {
        ".".join(module.split(".")[:2])
        for module in resolved
        if module.startswith("maestro.")
    }
    assert maestro_imports == {
        "maestro.pr_owed_act",
        "maestro.remediation_tracker",
    }
    forbidden = {"open", "read_text", "write_text", "Path", "urlopen", "run"}
    named = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert named & forbidden == set()


def test_lap_budget_exhausted_claim_holds_under_code_owed() -> None:
    """An exhaustion latch expects owned work to remain, so it holds (#4110)."""
    assert assess_standdown_validity(
        StanddownClaim.LAP_BUDGET_EXHAUSTED, OwedAct.CODE_OWED,
    ) is StanddownValidity.HOLDS
    assert parse_standdown_claim(
        "lap-budget-exhausted",
    ) is StanddownClaim.LAP_BUDGET_EXHAUSTED

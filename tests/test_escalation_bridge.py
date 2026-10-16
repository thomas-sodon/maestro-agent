"""Unit tests for the no-escalation-bridge gate (``maestro.escalation_bridge``).

Covers :func:`detect_unlanded_cure_handoff` (lap budget, oldest-first,
git-merge-truth, ``pr_number``-keyed standdown skip), :func:`deliver_handoff`
(exactly-once delivery, no-op on repeat, persisted ``pr_number`` standdown),
and the proposal-080 Phase 3 standdown helpers (:func:`has_standdown`,
:func:`clear_standdown`, :func:`prune_resolved_standdowns`, legacy-key
migration). All I/O runs through the ``mem_fs`` fixture — no real filesystem.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from maestro.audit_checks_phantom import (
    check_phantom_escalation,
)
from maestro.config import Config
from maestro.escalation_bridge import (
    _CLAIMED_ESCALATION_EXPIRY,
    _HANDOFF_CAPABILITY_CAP,
    _HANDOFF_ID_PROBE_CAP,
    EscalationLedgerBackend,
    JsonEscalationLedger,
    RefutedReleaseOutcome,
    UnlandedCureHandoff,
    _fresh_handoff_message_id,
    _handoff_message,
    active_standdown_prs,
    clear_standdown,
    count_refutation_releases,
    deliver_claimed_escalation,
    deliver_handoff,
    deliver_handoff_cfg,
    deliver_reality_refutation,
    deliver_retirement_disposition,
    deliver_unwinnable_race,
    detect_unlanded_cure_handoff,
    has_standdown,
    load_all_escalation_standdowns,
    mark_comment_ids_serviced,
    prune_expired_standdowns,
    prune_refuted_standdowns,
    prune_resolved_standdowns,
    reconcile_serviced_comment_ids,
    standdown_claims,
    standdown_delivery_times,
    unserviced_comment_ids,
)
from maestro.memory import EvolutionPlan, PlanStep, TaskRecord
from maestro.messaging import Message, MessageStore
from maestro.pr_owed_act import OwedAct
from maestro.remediation_tracker import MERGE_BLOCK_NONE
from maestro.standdown_validity import StanddownClaim
from maestro.state_store import SqliteEscalationLedger, StateStore

_DIR = Path("/fake/eb")

#: The default JSON backend the corpus threads through the module functions —
#: it reads/writes ``_DIR/escalation_handoffs.json``, the same file the raw-JSON
#: ``_record_standdown`` / ``_ledger`` helpers touch, so the historical corpus
#: seeds and read-backs stay valid on the JSON side (proposal 106). It is
#: stateless (holds only the path), so one module-level instance is shared.
_BE = JsonEscalationLedger(_DIR)


def _record_standdown(
    mem_fs: dict[str, str],
    pr: int,
    *,
    head_sha: str = "",
    delivered_at: str = "2026-01-01T00:00:00+00:00",
    serviced: list[int] | None = None,
) -> None:
    """Write a ``pr_number``-keyed standdown record under ``_DIR``.

    Omitting *serviced* writes the legacy ``{head_sha, delivered_at}`` shape (no
    ``serviced_comment_ids`` field) so the loader's missing-field degrade path is
    exercised; passing a list seeds an explicit serviced set (#2798).
    """
    path = str(_DIR / "escalation_handoffs.json")
    existing = json.loads(mem_fs[path]) if mem_fs.get(path) else {}
    record: dict[str, object] = {
        "head_sha": head_sha, "delivered_at": delivered_at,
    }
    if serviced is not None:
        record["serviced_comment_ids"] = serviced
    existing[str(pr)] = record
    mem_fs[path] = json.dumps(existing)


def _seed_history(
    mem_fs: dict[str, str], records: list[dict[str, object]],
) -> None:
    """Write *records* as ``task_history.jsonl`` lines under ``_DIR``."""
    path = str(_DIR / "task_history.jsonl")
    mem_fs[path] = "".join(json.dumps(r) + "\n" for r in records)


def _remediation(
    pr: int,
    *,
    outcome: str = "success",
    head_sha: str = "",
) -> dict[str, object]:
    rec: dict[str, object] = {
        "task_id": f"t-{pr}",
        "goal": f"Remediate PR #{pr}: fix the thing",
        "outcome": outcome,
    }
    if head_sha:
        rec["pr_head_sha"] = head_sha
    return rec


def _never_merged(_pr: int) -> bool:
    return False


def _always_merged(_pr: int) -> bool:
    return True


def _always_settled(_pr: int) -> bool:
    """Settled (merged OR closed-unmerged) — the #2967 closed-PR guard input."""
    return True


def _messages(mem_fs: dict[str, str]) -> list[dict[str, object]]:
    raw = mem_fs.get(str(_DIR / "messages.jsonl"), "")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _ledger(mem_fs: dict[str, str]) -> dict[str, object]:
    raw = mem_fs.get(str(_DIR / "escalation_handoffs.json"), "")
    return json.loads(raw) if raw else {}


# ---------------------------------------------------------------------------
# detect_unlanded_cure_handoff
# ---------------------------------------------------------------------------


def test_detect_fires_at_threshold(mem_fs: dict[str, str]) -> None:
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(3)],
    )
    handoff = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
    )
    assert handoff == UnlandedCureHandoff(
        pr_number=100, head_sha="abc12345", stamp_count=3,
    )


def test_detect_silent_below_threshold(mem_fs: dict[str, str]) -> None:
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(2)],
    )
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
        )
        is None
    )


def test_detect_silent_when_merged(mem_fs: dict[str, str]) -> None:
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(5)],
    )
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=3, is_pr_merged=_always_merged,
        )
        is None
    )


def test_detect_progress_aware_fires_on_no_progress_count(
    mem_fs: dict[str, str],
) -> None:
    """With ``no_progress_laps`` supplied, the count it carries drives the
    trigger — the head SHA still comes from the task history."""
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(3)],
    )
    handoff = detect_unlanded_cure_handoff(
        _DIR,
        backend=_BE, threshold=3,
        is_pr_merged=_never_merged,
        no_progress_laps={100: 4},
    )
    assert handoff == UnlandedCureHandoff(
        pr_number=100, head_sha="abc12345", stamp_count=4,
    )


def test_detect_progress_aware_empty_means_no_handoff(
    mem_fs: dict[str, str],
) -> None:
    """A PR clearing a blocker every lap never appears in ``no_progress_laps``
    (the sensor returns only non-empty runs), so no handoff fires even though
    the gross history is well past the budget."""
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(9)],
    )
    assert (
        detect_unlanded_cure_handoff(
            _DIR,
            backend=_BE, threshold=3,
            is_pr_merged=_never_merged,
            no_progress_laps={},
        )
        is None
    )


def test_detect_progress_aware_skips_merged_pr(
    mem_fs: dict[str, str],
) -> None:
    """In progress-aware mode a merged PR is guarded out via ``is_pr_merged``
    even though the no-progress sensor does not itself consult merge-truth."""
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(3)],
    )
    assert (
        detect_unlanded_cure_handoff(
            _DIR,
            backend=_BE, threshold=3,
            is_pr_merged=_always_merged,
            no_progress_laps={100: 5},
        )
        is None
    )


def test_detect_skips_settled_closed_pr_no_reknock(
    mem_fs: dict[str, str],
) -> None:
    """A closed-unmerged PR is settled, so it is never re-handed-off (#2967).

    The re-knock: Thomas closes PR #100 exactly as the handoff asked, the Tend
    route's ``prune_resolved_standdowns`` clears the ``pr_number``-keyed
    standdown on the settled (closed) verdict, and the next ``generate_task``
    re-detects #100 — its unlanded laps still in-window, no standdown, and
    ``is_pr_merged`` reporting it *not merged* (closed ≠ merged) — so the
    merged-only guard lets it through and a fresh merge-or-close message fires
    every iteration. The symmetric ``is_pr_settled`` guard closes it: a settled
    (merged **or** closed) PR is skipped, mirroring the release predicate.

    Both detection modes are driven. The control (settled guard absent) shows
    the re-knock still fires, proving the guard is what suppresses it.
    """
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(3)],
    )
    # --- Gross-lap path -----------------------------------------------------
    # Control: without the settled guard, a closed-unmerged (not merged) PR
    # with no standdown re-knocks — the bug.
    reknock = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
    )
    assert reknock == UnlandedCureHandoff(
        pr_number=100, head_sha="abc12345", stamp_count=3,
    )
    # Fix: the settled guard skips the closed PR — no handoff.
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=3,
            is_pr_merged=_never_merged,
            is_pr_settled=_always_settled,
        )
        is None
    )
    # --- Progress-aware path ------------------------------------------------
    # Control: consecutive no-progress laps at threshold, not merged, no
    # standdown — re-knocks without the settled guard.
    reknock_pa = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=3,
        is_pr_merged=_never_merged,
        no_progress_laps={100: 3},
    )
    assert reknock_pa == UnlandedCureHandoff(
        pr_number=100, head_sha="abc12345", stamp_count=3,
    )
    # Fix: the settled guard skips the closed PR in progress-aware mode too.
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=3,
            is_pr_merged=_never_merged,
            is_pr_settled=_always_settled,
            no_progress_laps={100: 3},
        )
        is None
    )


def test_detect_picks_oldest_unlanded(mem_fs: dict[str, str]) -> None:
    records = (
        [_remediation(300, head_sha="three") for _ in range(3)]
        + [_remediation(150, head_sha="one50") for _ in range(4)]
    )
    _seed_history(mem_fs, records)
    handoff = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
    )
    assert handoff is not None
    assert handoff.pr_number == 150


def test_detect_skips_when_standdown_recorded(mem_fs: dict[str, str]) -> None:
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(3)],
    )
    # Pre-record a pr_number-keyed standdown for #100.
    _record_standdown(mem_fs, 100, head_sha="abc12345")
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
        )
        is None
    )


def test_detect_standdown_sticks_across_new_head(mem_fs: dict[str, str]) -> None:
    """A fresh head SHA does NOT re-knock once a standdown is recorded.

    Proposal 080 Phase 3: the standdown is keyed on ``pr_number``, so a new
    commit on a still-unmerged PR no longer resurfaces a handoff (the pre-080
    per-head keying re-knocked here).
    """
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="newhead9") for _ in range(3)],
    )
    # Standdown recorded against an OLD head; the new head must not re-knock.
    _record_standdown(mem_fs, 100, head_sha="oldhead0")
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
        )
        is None
    )


def test_detect_non_positive_threshold_is_none(mem_fs: dict[str, str]) -> None:
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(3)],
    )
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=0, is_pr_merged=_never_merged,
        )
        is None
    )


def test_detect_none_when_no_history(mem_fs: dict[str, str]) -> None:
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
        )
        is None
    )


def test_detect_tolerates_non_dict_ledger(mem_fs: dict[str, str]) -> None:
    """A corrupt (non-dict) ledger is treated as empty, not a crash."""
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(3)],
    )
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps([1, 2, 3])
    handoff = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
    )
    assert handoff is not None
    assert handoff.pr_number == 100


def test_detect_head_sha_from_title_fallback(mem_fs: dict[str, str]) -> None:
    """Head SHA is recovered from the ``title`` field and junk rows skipped."""
    records: list[dict[str, object]] = [
        _remediation(100) for _ in range(2)
    ]
    # A record carrying its goal only in ``title`` plus the head SHA.
    records.append(
        {
            "task_id": "t-title",
            "title": "Remediate PR #100: via title",
            "outcome": "success",
            "pr_head_sha": "titlehead",
        },
    )
    # A junk record with no goal/title — skipped by the head-SHA scan.
    records.append({"foo": "bar"})
    _seed_history(mem_fs, records)
    handoff = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
    )
    assert handoff is not None
    assert handoff.head_sha == "titlehead"
    assert handoff.stamp_count == 3


# ---------------------------------------------------------------------------
# deliver_handoff
# ---------------------------------------------------------------------------


def test_deliver_writes_message_exactly_once(mem_fs: dict[str, str]) -> None:
    handoff = UnlandedCureHandoff(
        pr_number=2579, head_sha="abc12345", stamp_count=4,
    )
    first = deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00", claim=StanddownClaim.HANDOFF_READY)
    second = deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-12T01:00:00+00:00", claim=StanddownClaim.HANDOFF_READY)

    assert first is True
    assert second is False
    messages = _messages(mem_fs)
    assert len(messages) == 1
    assert messages[0]["sender"] == "maestro"
    assert messages[0]["message_type"] == "question"
    assert "#2579" in messages[0]["content"]
    # The first timestamp is preserved; the no-op never overwrites it.
    assert messages[0]["timestamp"] == "2026-06-12T00:00:00+00:00"


def test_deliver_persists_pr_keyed_standdown(mem_fs: dict[str, str]) -> None:
    handoff = UnlandedCureHandoff(
        pr_number=2579, head_sha="abc12345", stamp_count=4,
    )
    deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00", claim=StanddownClaim.HANDOFF_READY)

    ledger = _ledger(mem_fs)
    # Keyed on pr_number alone; the head SHA is an informational field. The
    # serviced-comment-ids set is seeded empty at record time (#2798).
    assert "2579" in ledger
    assert ledger["2579"] == {
        "head_sha": "abc12345",
        "delivered_at": "2026-06-12T00:00:00+00:00",
        "serviced_comment_ids": [],
        "claim": "handoff-ready",
    }
    assert has_standdown(_BE, 2579) is True


def test_deliver_persists_the_callers_claim(mem_fs: dict[str, str]) -> None:
    """The record carries the premise its caller passed, not a fixed literal."""
    handoff = UnlandedCureHandoff(
        pr_number=4110, head_sha="abc12345", stamp_count=6,
    )
    deliver_handoff(
        _DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00",
        claim=StanddownClaim.LAP_BUDGET_EXHAUSTED,
    )

    assert _ledger(mem_fs)["4110"]["claim"] == "lap-budget-exhausted"


def _deliver_handoff_call_sites() -> list[tuple[str, str, ast.Call]]:
    """Every production ``deliver_handoff*`` call under ``maestro/``.

    Each entry is ``(file:line, innermost enclosing function, call)``.
    """
    root = Path(__file__).resolve().parent.parent / "maestro"
    sites: list[tuple[str, str, ast.Call]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        enclosing: dict[int, str] = {}
        for scope in ast.walk(tree):
            if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # ``ast.walk`` is breadth-first, so an inner def overwrites its
                # outer def's entry: each node maps to its innermost function.
                for inner in ast.walk(scope):
                    enclosing[id(inner)] = scope.name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None
            )
            if name in ("deliver_handoff", "deliver_handoff_cfg"):
                sites.append((
                    f"{path.name}:{node.lineno}",
                    enclosing.get(id(node), "<module>"),
                    node,
                ))
    return sites


def test_every_production_handoff_site_passes_an_explicit_claim() -> None:
    """Each deliverer call names its premise via ``claim=`` (#4110).

    mypy already rejects a call omitting the required keyword; this backs it
    against a ``**kwargs`` passthrough that would hide the premise choice.
    """
    sites = _deliver_handoff_call_sites()
    assert len(sites) >= 7
    missing = [
        where for where, _, call in sites
        if not any(kw.arg == "claim" for kw in call.keywords)
    ]
    assert missing == []


# Innermost enclosing function of each production site -> the premise it must
# stamp. Readiness sites hand off a green PR; exhaustion sites hand off a PR
# whose lap / no-op / unlanded-grace budget ran out with owned work remaining.
_EXPECTED_SITE_CLAIMS: dict[tuple[str, str], str] = {
    ("pr_remediation.py", "_escalate_stuck_pr"): "StanddownClaim.LAP_BUDGET_EXHAUSTED",
    ("escalation_bridge.py", "deliver_handoff_cfg"): "claim",
    ("pr_remediation.py", "route_feedback"): "StanddownClaim.LAP_BUDGET_EXHAUSTED",
    ("pr_remediation.py", "_deliver"): "StanddownClaim.HANDOFF_READY",
    ("record_binds.py", "_deliver_merge_request"): "StanddownClaim.HANDOFF_READY",
    ("self_audit.py", "_deliver"): "StanddownClaim.HANDOFF_READY",
    ("self_audit.py", "audit_self_model"): "StanddownClaim.LAP_BUDGET_EXHAUSTED",
    ("task_gates.py", "_escalation_bridge_run"): "StanddownClaim.LAP_BUDGET_EXHAUSTED",
}


def test_each_production_handoff_site_stamps_its_premise() -> None:
    """Each site's ``claim=`` matches its premise, keyed by enclosing function.

    Flipping an exhaustion site back to ``HANDOFF_READY`` would let the refuted
    pruner release its latch while owned work remains (#4110).
    """
    observed: dict[tuple[str, str], str] = {}
    for where, scope, call in _deliver_handoff_call_sites():
        file_name = where.split(":")[0]
        claims = [ast.unparse(kw.value) for kw in call.keywords if kw.arg == "claim"]
        observed[(file_name, scope)] = claims[0]
    assert observed == _EXPECTED_SITE_CLAIMS


def test_deliver_then_detect_is_noop(mem_fs: dict[str, str]) -> None:
    """End-to-end: a delivered handoff is no longer detected."""
    _seed_history(
        mem_fs,
        [_remediation(2579, head_sha="abc12345") for _ in range(3)],
    )
    handoff = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
    )
    assert handoff is not None
    assert deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00", claim=StanddownClaim.LAP_BUDGET_EXHAUSTED)
    # After delivery, the same condition no longer surfaces a handoff.
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
        )
        is None
    )


def test_deliver_empty_head_uses_nohead_id(mem_fs: dict[str, str]) -> None:
    handoff = UnlandedCureHandoff(
        pr_number=42, head_sha="", stamp_count=3,
    )
    assert deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00", claim=StanddownClaim.HANDOFF_READY)
    messages = _messages(mem_fs)
    assert len(messages) == 1
    assert messages[0]["message_id"] == "handoff-42-nohead"
    assert "42" in _ledger(mem_fs)


def _capability_plan(
    *,
    title: str = "Owning plan title",
    acceptance_criteria: list[str] | None = None,
) -> EvolutionPlan:
    """A minimal owning plan for the capability-line enrichment tests.

    Only ``title`` and ``acceptance_criteria`` steer the capability line; the
    remaining fields are the dataclass's required scaffolding.
    """
    return EvolutionPlan(
        plan_id="ep-cap",
        title=title,
        category="capability",
        rationale="r",
        proposed_changes=["c"],
        files_affected=["maestro/escalation_bridge.py"],
        expected_impact="x",
        acceptance_criteria=acceptance_criteria or [],
    )


def test_deliver_handoff_prepends_first_acceptance_criterion(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 1 — the owning plan's first acceptance_criteria entry renders as
    a bounded capability line prepended to the merge-or-close message."""
    handoff = UnlandedCureHandoff(
        pr_number=321, head_sha="abc12345", stamp_count=4,
    )
    plan = _capability_plan(
        title="unused-title",
        acceptance_criteria=[
            "The merge lands a legible merge-or-close ping",
            "second criterion (never used)",
        ],
    )
    assert deliver_handoff(
        _DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00",
        plan_lookup=lambda _pr: plan,
        claim=StanddownClaim.HANDOFF_READY,
    )
    content = _messages(mem_fs)[0]["content"]
    assert content.startswith(
        "This merge would land: The merge lands a legible merge-or-close ping\n",
    )
    # The title is not used when a criterion is present.
    assert "unused-title" not in content
    # The generic merge-or-close body is left unchanged after the line.
    assert "please merge PR #321 or close/retire it" in content
    assert "is merge-ready and waiting on you" in content


def test_exhausted_claim_handoff_prepends_capability_line(
    mem_fs: dict[str, str],
) -> None:
    """The lap-exhaustion body takes the same capability line prefix."""
    handoff = UnlandedCureHandoff(pr_number=325, head_sha="", stamp_count=4)
    plan = _capability_plan(acceptance_criteria=["Lands the lap-budget ping"])
    assert deliver_handoff(
        _DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00",
        plan_lookup=lambda _pr: plan,
        claim=StanddownClaim.LAP_BUDGET_EXHAUSTED,
    )
    content = _messages(mem_fs)[0]["content"]
    assert content.startswith("This merge would land: Lands the lap-budget ping\n")
    assert "4 unlanded remediation lap(s)" in content


def test_deliver_handoff_falls_back_to_title_without_criteria(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 1 — with no acceptance_criteria, the plan title supplies the
    capability line."""
    handoff = UnlandedCureHandoff(
        pr_number=322, head_sha="", stamp_count=3,
    )
    plan = _capability_plan(
        title="Carry the lead capability into the handoff",
        acceptance_criteria=[],
    )
    assert deliver_handoff(
        _DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00",
        plan_lookup=lambda _pr: plan,
        claim=StanddownClaim.HANDOFF_READY,
    )
    content = _messages(mem_fs)[0]["content"]
    assert content.startswith(
        "This merge would land: Carry the lead capability into the handoff\n",
    )


def test_deliver_handoff_no_plan_is_byte_identical_to_generic(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 2 — when no plan resolves (no lookup, or a lookup returning
    ``None``), the emitted message is byte-identical to the generic text with no
    empty or dangling capability line."""
    # (a) No lookup supplied at all.
    handoff = UnlandedCureHandoff(
        pr_number=323, head_sha="dead0000", stamp_count=5,
    )
    assert deliver_handoff(
        _DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00",
        claim=StanddownClaim.LAP_BUDGET_EXHAUSTED,
    )
    generic = _messages(mem_fs)[0]["content"]
    assert "This merge would land:" not in generic
    assert generic == _handoff_message(handoff)

    # (b) A lookup that resolves no plan → byte-identical generic body.
    handoff_b = UnlandedCureHandoff(
        pr_number=324, head_sha="dead0000", stamp_count=5,
    )
    assert deliver_handoff(
        _DIR, handoff_b, backend=_BE, now="2026-06-12T00:00:00+00:00",
        plan_lookup=lambda _pr: None,
        claim=StanddownClaim.LAP_BUDGET_EXHAUSTED,
    )
    resolved_none = next(
        m["content"] for m in _messages(mem_fs) if "#324" in str(m["content"])
    )
    assert "This merge would land:" not in resolved_none
    assert resolved_none == _handoff_message(handoff_b)


def test_deliver_handoff_resolved_plan_without_capability_is_generic(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 2 — a plan that *resolves* but names no capability (every
    acceptance criterion blank AND a blank title) yields ``None`` from
    ``_capability_line``, so ``_handoff_message`` falls through to the generic
    merge-or-close body with no empty or dangling capability line. Distinct from
    the ``plan is None`` path: here the lookup returns a real plan.

    Covers the ``_capability_line`` ``return None`` arm (blank criterion skipped
    in the loop, blank title falls through) and the ``line is None`` branch in
    ``_handoff_message``.
    """
    handoff = UnlandedCureHandoff(
        pr_number=326, head_sha="feed0000", stamp_count=6,
    )
    # A whitespace-only criterion is skipped by the loop; the blank title then
    # supplies nothing either, so no capability line is composable.
    plan = _capability_plan(title="   ", acceptance_criteria=["  "])
    assert deliver_handoff(
        _DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00",
        plan_lookup=lambda _pr: plan,
        claim=StanddownClaim.LAP_BUDGET_EXHAUSTED,
    )
    content = _messages(mem_fs)[0]["content"]
    assert "This merge would land:" not in content
    # Byte-identical to the generic body — the resolved-but-empty plan adds nothing.
    assert content == _handoff_message(handoff)
    assert content == _handoff_message(handoff, plan)


def test_deliver_handoff_capability_line_truncated_to_cap(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 3 — a long acceptance criterion is truncated to the fixed cap so
    it cannot bloat the message."""
    long_criterion = "x" * 500
    handoff = UnlandedCureHandoff(
        pr_number=325, head_sha="", stamp_count=3,
    )
    plan = _capability_plan(acceptance_criteria=[long_criterion])
    assert deliver_handoff(
        _DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00",
        plan_lookup=lambda _pr: plan,
        claim=StanddownClaim.HANDOFF_READY,
    )
    content = _messages(mem_fs)[0]["content"]
    first_line = content.splitlines()[0]
    prefix = "This merge would land: "
    assert first_line.startswith(prefix)
    capability = first_line[len(prefix):]
    # Truncated to exactly the cap (last char an ellipsis), never the full 500.
    assert len(capability) == _HANDOFF_CAPABILITY_CAP
    assert capability.endswith("…")
    assert long_criterion not in content


def test_progress_aware_converging_pr_never_handed_off_end_to_end(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 094 end-to-end: a salvageable PR advancing its readiness each lap
    never appears in ``no_progress_laps`` (empty), so the detector returns no
    handoff — no message, no standdown — despite a gross history past the budget."""
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(9)],
    )
    handoff = detect_unlanded_cure_handoff(
        _DIR,
        backend=_BE, threshold=3,
        is_pr_merged=_never_merged,
        no_progress_laps={},
    )
    assert handoff is None
    # Nothing is delivered when there is no handoff.
    assert _messages(mem_fs) == []
    assert has_standdown(_BE, 100) is False


def test_progress_aware_stuck_pr_handoff_end_to_end(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 094 end-to-end: a progress-exhausted PR (no-progress laps at the
    budget) yields exactly one ``sender="maestro"`` merge-or-close message and a
    ``pr_number``-keyed standdown, with no PR-close side effect — ``deliver_handoff``
    takes no code host and closes nothing."""
    _seed_history(
        mem_fs,
        [_remediation(100, head_sha="abc12345") for _ in range(3)],
    )
    handoff = detect_unlanded_cure_handoff(
        _DIR,
        backend=_BE, threshold=3,
        is_pr_merged=_never_merged,
        no_progress_laps={100: 3},
    )
    assert handoff == UnlandedCureHandoff(
        pr_number=100, head_sha="abc12345", stamp_count=3,
    )
    assert deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00", claim=StanddownClaim.LAP_BUDGET_EXHAUSTED)

    # Exactly one human merge-or-close message reached messages.jsonl.
    messages = _messages(mem_fs)
    assert len(messages) == 1
    assert messages[0]["sender"] == "maestro"
    assert "#100" in messages[0]["content"]
    # In progress-aware mode ``stamp_count`` is the no-progress lap count, not the
    # success/partial stamp count, so the message describes it as unlanded
    # remediation laps and never claims a "stamped success/partial Nx" count that
    # would undercount the real stamps (ledger r2).
    assert "3 unlanded remediation lap(s)" in messages[0]["content"]
    assert "stamped success/partial" not in messages[0]["content"]
    # A durable pr_number-keyed standdown is recorded; the deliverer closed no PR
    # (it takes no code host — the only artifacts are the message and the ledger).
    assert has_standdown(_BE, 100) is True
    assert set(mem_fs) == {
        str(_DIR / "task_history.jsonl"),
        str(_DIR / "messages.jsonl"),
        str(_DIR / "escalation_handoffs.json"),
    }
    # Re-running the detector after delivery is a no-op (idempotent human knock).
    assert (
        detect_unlanded_cure_handoff(
            _DIR,
            backend=_BE, threshold=3,
            is_pr_merged=_never_merged,
            no_progress_laps={100: 3},
        )
        is None
    )


# ---------------------------------------------------------------------------
# deliver_reality_refutation (proposal 088 Phase 2)
# ---------------------------------------------------------------------------


def test_deliver_reality_refutation_writes_message_exactly_once(
    mem_fs: dict[str, str],
) -> None:
    """First call delivers one message; a second is a no-op (idempotent per PR)."""
    first = deliver_reality_refutation(
        _DIR, 4242, 3, backend=_BE, now="2026-06-21T00:00:00+00:00",
    )
    second = deliver_reality_refutation(
        _DIR, 4242, 5, backend=_BE, now="2026-06-21T01:00:00+00:00",
    )
    assert first is True
    assert second is False
    messages = _messages(mem_fs)
    assert len(messages) == 1
    assert messages[0]["sender"] == "maestro"
    assert messages[0]["message_type"] == "question"
    assert messages[0]["message_id"] == "reality-refutation-4242"
    assert "#4242" in messages[0]["content"]
    assert "git" in messages[0]["content"].lower()
    # The first timestamp is preserved; the no-op never overwrites it.
    assert messages[0]["timestamp"] == "2026-06-21T00:00:00+00:00"


def test_deliver_reality_refutation_persists_pr_keyed_standdown(
    mem_fs: dict[str, str],
) -> None:
    """Delivery records a durable ``pr_number``-keyed standdown (empty head)."""
    deliver_reality_refutation(_DIR, 4242, 3, backend=_BE, now="2026-06-21T00:00:00+00:00")
    ledger = _ledger(mem_fs)
    assert ledger["4242"] == {
        "head_sha": "",
        "delivered_at": "2026-06-21T00:00:00+00:00",
        "serviced_comment_ids": [],
        "claim": "reality-refuted",
    }
    assert has_standdown(_BE, 4242) is True


def test_deliver_reality_refutation_noop_when_standdown_exists(
    mem_fs: dict[str, str],
) -> None:
    """A pre-existing standdown (e.g. from a handoff) suppresses delivery."""
    _record_standdown(mem_fs, 4242, head_sha="deadbeef")
    delivered = deliver_reality_refutation(
        _DIR, 4242, 9, backend=_BE, now="2026-06-21T00:00:00+00:00",
    )
    assert delivered is False
    assert _messages(mem_fs) == []
    # The pre-existing record is untouched.
    assert _ledger(mem_fs)["4242"]["head_sha"] == "deadbeef"


def test_deliver_reality_refutation_released_by_prune(
    mem_fs: dict[str, str],
) -> None:
    """A merged/closed verdict clears the standdown via the shared prune path."""
    assert deliver_reality_refutation(
        _DIR, 4242, 3, backend=_BE, now="2026-06-21T00:00:00+00:00",
    )
    cleared = prune_resolved_standdowns(_BE, _always_merged)
    assert cleared == [4242]
    assert has_standdown(_BE, 4242) is False
    # With the standdown released, a fresh dispute can earn a new knock.
    assert deliver_reality_refutation(
        _DIR, 4242, 3, backend=_BE, now="2026-06-21T02:00:00+00:00",
    )
    assert len(_messages(mem_fs)) == 2


def test_deliver_reality_refutation_injected_store(
    mem_fs: dict[str, str],
) -> None:
    """An injected MessageStore receives the message (the test-seam path)."""
    store = MessageStore(_DIR)
    assert deliver_reality_refutation(
        _DIR, 7, 4, backend=_BE, message_store=store, now="2026-06-21T00:00:00+00:00",
    )
    delivered = store.get_recent()
    assert len(delivered) == 1
    assert "#7" in delivered[0].content


# ---------------------------------------------------------------------------
# Regression guard: the #2579 lineage reaches the delivery branch
# ---------------------------------------------------------------------------
#
# The no-escalation bridge (#2613) was once unable to fire for the #2579
# UNLANDED-REMEDIATION lineage — not because of any predicate in this module,
# but because the git-merge-truth oracle ``_git_pr_merged_default`` false-cleared
# #2579 by matching a bare ``#2579`` substring in an unrelated merge commit
# subject, starving :func:`measure_unlanded_remediations` of the lineage. PR
# #2619 fixed the oracle (subject-only merge-truth contract), after which the
# bridge delivered two real handoffs for #2579. These tests reconstruct the
# realistic #2579 lineage — many ``partial`` ``[merge-block]`` laps past
# threshold, git-merge-truth unmerged — and lock in that the bridge emits exactly
# one Thomas-addressed merge-or-close handoff keyed on ``(pr, head_sha)`` and
# then goes quiet. They pass against today's tree (the cure is landed); they are
# a guard, not a fix-verification.
#
# SCOPE — what these tests do and do NOT cover. Both tests inject
# ``is_pr_merged=_never_merged``, which replaces the real ``_git_pr_merged_default``
# oracle entirely. So they guard only the *bridge predicate* (detect → deliver →
# idempotency) given a known-unmerged lineage; they do NOT exercise the oracle.
# The oracle's subject-only merge-truth contract — the actual #2619 root cause,
# that a bare ``#2579`` substring in an unrelated merge subject must NOT clear the
# PR — is guarded separately by ``test_git_pr_merged_default_body_only_mention_not_merged``
# in ``tests/test_audit_checks.py``. A regression there would be caught by that
# suite, not by these tests. (Project testing rules forbid invoking real git in a
# unit test, so the oracle cannot be driven from here.)


def _merge_block_lap(pr: int, head_sha: str) -> dict[str, object]:
    """A realistic Tend-clearable ``partial`` remediation lap for *pr*."""
    return {
        "task_id": f"lap-{pr}-{head_sha}",
        "goal": f"Remediate PR #{pr}: [Maestro] Readmission->construction",
        "outcome": "partial",
        "result_summary": (
            f"[merge-block] PR #{pr} remediation skipped at zero SDK cost "
            "on branch maestro/evolve/db83d0d763b9"
        ),
        "pr_head_sha": head_sha,
    }


def test_2579_lineage_reaches_delivery_branch(mem_fs: dict[str, str]) -> None:
    """The reconstructed #2579 lineage fires exactly one handoff, then quiet."""
    head = "db83d0d763b9aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    _seed_history(mem_fs, [_merge_block_lap(2579, head) for _ in range(34)])
    handoff = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
    )
    assert handoff is not None
    assert handoff.pr_number == 2579
    assert handoff.head_sha == head
    assert handoff.stamp_count == 34
    # Exactly one Thomas-addressed merge-or-close message is emitted.
    assert deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-14T00:00:00+00:00", claim=StanddownClaim.LAP_BUDGET_EXHAUSTED)
    messages = _messages(mem_fs)
    assert len(messages) == 1
    assert messages[0]["sender"] == "maestro"
    assert messages[0]["message_type"] == "question"
    assert "#2579" in messages[0]["content"]
    assert "2579" in _ledger(mem_fs)
    # After delivery the lineage no longer surfaces a handoff — the bridge
    # stands down rather than minting another remediation lap for this head.
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
        )
        is None
    )


def test_2579_lineage_idempotent_at_same_head(mem_fs: dict[str, str]) -> None:
    """A second delivery at the same #2579 head SHA writes no duplicate."""
    head = "db83d0d763b9aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    _seed_history(mem_fs, [_merge_block_lap(2579, head) for _ in range(34)])
    handoff = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
    )
    assert handoff is not None
    first = deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-14T00:00:00+00:00", claim=StanddownClaim.LAP_BUDGET_EXHAUSTED)
    second = deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-14T02:00:00+00:00", claim=StanddownClaim.LAP_BUDGET_EXHAUSTED)
    assert first is True
    assert second is False
    assert len(_messages(mem_fs)) == 1


def _escalation_prose_lap(pr: int, task_id: str = "esc-lap") -> TaskRecord:
    """A ``partial`` lap whose summary asserts escalation (the ``escalat`` stem)
    for *pr* — the shape phantom-escalation rule 12 flags absent a message."""
    return TaskRecord(
        task_id=task_id,
        goal=f"Remediate PR #{pr}: land or retire the cure",
        plan=[PlanStep(step_id="s1", description="x", status="done")],
        outcome="partial",
        result_summary=(
            f"[branch-resolved] escalating PR #{pr} once — the cure is "
            "committed but unlanded; handing off."
        ),
        errors=[],
        duration_secs=0.1,
        timestamp="2026-06-14T00:00:00+00:00",
    )


def test_delivered_handoff_extinguishes_phantom_escalation_rule12(
    mem_fs: dict[str, str],
) -> None:
    """Acceptance criterion #3: after the bridge delivers, phantom-escalation
    audit rule 12 finds a referencing message and no longer flags the #2579
    lineage. This ties the *delivered message content* to rule 12's
    ``extract_issue_numbers`` extinction — the load-bearing, otherwise-unguarded
    contract that ``_handoff_message`` must embed a ``#<pr>`` token. Reword
    ``_handoff_message`` to drop the literal ``#2579`` and this test fails.
    """
    head = "db83d0d763b9aaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    _seed_history(mem_fs, [_merge_block_lap(2579, head) for _ in range(34)])
    handoff = detect_unlanded_cure_handoff(
        _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
    )
    assert handoff is not None

    # An escalation-prose lap referencing #2579: rule 12 flags it while no
    # outbound message references the issue.
    prose = [_escalation_prose_lap(2579)]
    flagged = check_phantom_escalation(prose, [])
    assert len(flagged) == 1
    assert "#2579" in flagged[0].statement

    # Deliver the bridge's merge-or-close handoff, then read the real delivered
    # message back through the store (not a hand-built stand-in).
    assert deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-14T00:00:00+00:00", claim=StanddownClaim.LAP_BUDGET_EXHAUSTED)
    delivered_messages = MessageStore(_DIR).get_recent()
    assert len(delivered_messages) == 1
    assert "#2579" in delivered_messages[0].content

    # With the delivered message present, rule 12 self-extinguishes for #2579.
    assert check_phantom_escalation(prose, delivered_messages) == []


# ---------------------------------------------------------------------------
# Standdown helpers (proposal 080 Phase 3)
# ---------------------------------------------------------------------------


def test_has_standdown_false_when_absent(mem_fs: dict[str, str]) -> None:
    assert has_standdown(_BE, 100) is False
    _record_standdown(mem_fs, 100)
    assert has_standdown(_BE, 100) is True


def test_clear_standdown_removes_record(mem_fs: dict[str, str]) -> None:
    _record_standdown(mem_fs, 100)
    _record_standdown(mem_fs, 200)
    assert clear_standdown(_BE, 100) is True
    assert has_standdown(_BE, 100) is False
    # The sibling standdown is untouched.
    assert has_standdown(_BE, 200) is True
    # A second clear is a no-op.
    assert clear_standdown(_BE, 100) is False


def test_clear_standdown_noop_when_no_ledger(mem_fs: dict[str, str]) -> None:
    assert clear_standdown(_BE, 100) is False


def test_clear_standdown_honours_the_expected_delivery_stamp(
    mem_fs: dict[str, str],
) -> None:
    """``expected_delivered_at`` scopes the clear to one authored row.

    A caller releasing a suppression it delivered must not delete a foreign
    deliverer's row recorded under the same PR key after its own was cleared by
    a non-owner (#3630's green-handoff CI-RED release).
    """
    _record_standdown(mem_fs, 100, delivered_at="2026-02-02T00:00:00+00:00")
    # A stamp the live row does not carry clears nothing.
    assert clear_standdown(
        _BE, 100, expected_delivered_at="2026-01-01T00:00:00+00:00",
    ) is False
    assert has_standdown(_BE, 100) is True
    # The row's own stamp clears it.
    assert clear_standdown(
        _BE, 100, expected_delivered_at="2026-02-02T00:00:00+00:00",
    ) is True
    assert has_standdown(_BE, 100) is False


def test_active_standdown_prs_empty_when_no_ledger(
    mem_fs: dict[str, str],
) -> None:
    assert active_standdown_prs(_BE) == set()


def test_active_standdown_prs_returns_all_recorded(
    mem_fs: dict[str, str],
) -> None:
    _record_standdown(mem_fs, 100)
    _record_standdown(mem_fs, 2579)
    assert active_standdown_prs(_BE) == {100, 2579}
    # Clearing one drops it from the set.
    clear_standdown(_BE, 100)
    assert active_standdown_prs(_BE) == {2579}


def test_active_standdown_prs_includes_claimed_escalation_records(
    mem_fs: dict[str, str],
) -> None:
    """The PR-facing set (PR-LAPS / ``maestro pr-laps``) includes claimed-
    escalation records — the standdowns carrying ``expires_at`` — because rule 12
    keys overwhelmingly on real PR numbers, so a stood-down PR must still be
    annotated ``[stood-down]``; hiding a genuinely-suppressed PR is the worse
    failure. The per-number minting gate (:func:`has_standdown`) sees it too."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "2579": {
            "head_sha": "abc", "delivered_at": "2026-01-01T00:00:00+00:00",
            "serviced_comment_ids": [],
        },
        "321": _expiring_record("2026-03-01T00:00:00+00:00"),  # claimed-escalation
    })
    # Both the plain-PR and the claimed-escalation record surface in the set...
    assert active_standdown_prs(_BE) == {2579, 321}
    # ...and the per-number minting gate still sees the claimed-escalation one.
    assert has_standdown(_BE, 321) is True


def test_prune_resolved_standdowns_clears_merged(
    mem_fs: dict[str, str],
) -> None:
    for pr in (10, 20, 30):
        _record_standdown(mem_fs, pr)
    cleared = prune_resolved_standdowns(_BE, lambda pr: pr == 20)
    assert cleared == [20]
    assert has_standdown(_BE, 10) is True
    assert has_standdown(_BE, 20) is False
    assert has_standdown(_BE, 30) is True


def test_prune_resolved_standdowns_empty_when_no_ledger(
    mem_fs: dict[str, str],
) -> None:
    assert prune_resolved_standdowns(_BE, _always_merged) == []


def test_prune_resolved_standdowns_empty_when_ledger_holds_none(
    mem_fs: dict[str, str],
) -> None:
    """A present-but-empty ledger prunes nothing without calling the oracle."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({})

    def _boom(_pr: int) -> bool:
        raise AssertionError("oracle must not be consulted for an empty ledger")

    assert prune_resolved_standdowns(_BE, _boom) == []


def test_load_tolerates_junk_entries(mem_fs: dict[str, str]) -> None:
    """Junk keys/values are skipped; only well-formed standdowns survive."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "not-a-number": {"head_sha": "x", "delivered_at": "t"},
        "11": {"head_sha": "x"},  # malformed dict (no delivered_at)
        "12": 99,  # non-dict / non-str value
        "13": {"head_sha": "ok", "delivered_at": "2026-01-01T00:00:00+00:00"},
    })
    assert has_standdown(_BE, 11) is False
    assert has_standdown(_BE, 12) is False
    assert has_standdown(_BE, 13) is True


def test_prune_resolved_standdowns_empty_when_none_resolved(
    mem_fs: dict[str, str],
) -> None:
    _record_standdown(mem_fs, 10)
    assert prune_resolved_standdowns(_BE, _never_merged) == []
    assert has_standdown(_BE, 10) is True


def test_prune_does_not_swallow_predicate_error(
    mem_fs: dict[str, str],
) -> None:
    """A raising resolved-predicate propagates loudly (fail-loud)."""
    _record_standdown(mem_fs, 10)

    def _boom(_pr: int) -> bool:
        raise RuntimeError("host down")

    try:
        prune_resolved_standdowns(_BE, _boom)
    except RuntimeError as exc:
        assert "host down" in str(exc)
    else:
        raise AssertionError("expected the predicate error to propagate")


def test_legacy_key_migrated_to_standdown(mem_fs: dict[str, str]) -> None:
    """A pre-080 ``<pr>@<head>`` entry is treated as a standdown for that PR."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps(
        {"2579@oldhead0": "2026-01-01T00:00:00+00:00"},
    )
    assert has_standdown(_BE, 2579) is True
    # Detection is suppressed for the migrated PR even on a fresh head.
    _seed_history(
        mem_fs,
        [_remediation(2579, head_sha="newhead9") for _ in range(4)],
    )
    assert (
        detect_unlanded_cure_handoff(
            _DIR, backend=_BE, threshold=3, is_pr_merged=_never_merged,
        )
        is None
    )
    # Clearing the migrated entry drops the legacy key.
    assert clear_standdown(_BE, 2579) is True
    assert has_standdown(_BE, 2579) is False


# ---------------------------------------------------------------------------
# Serviced-comment-ids helpers (#2798)
# ---------------------------------------------------------------------------


def test_serviced_round_trips_through_load_and_write(
    mem_fs: dict[str, str],
) -> None:
    """``mark_comment_ids_serviced`` persists ids that survive a reload."""
    _record_standdown(mem_fs, 100)
    mark_comment_ids_serviced(_BE, 100, {7, 3})
    # The on-disk record now carries the sorted serviced set.
    assert _ledger(mem_fs)["100"]["serviced_comment_ids"] == [3, 7]
    # And a reload subtracts exactly those from a candidate set.
    assert unserviced_comment_ids(_BE, 100, {3, 7, 9}) == {9}


def test_load_legacy_two_key_record_has_empty_serviced(
    mem_fs: dict[str, str],
) -> None:
    """A legacy ``{head_sha, delivered_at}`` record (no serviced field) loads
    with an empty serviced set, so every candidate id is unserviced."""
    _record_standdown(mem_fs, 100)  # no serviced field
    assert unserviced_comment_ids(_BE, 100, {1, 2}) == {1, 2}


def test_load_legacy_string_entry_has_empty_serviced(
    mem_fs: dict[str, str],
) -> None:
    """A pre-080 ``"<pr>@<head>"`` string entry loads with an empty serviced
    set (the carve-out treats every detected thread as un-serviced)."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps(
        {"100@oldhead": "2026-01-01T00:00:00+00:00"},
    )
    assert unserviced_comment_ids(_BE, 100, {5}) == {5}


def test_load_non_list_serviced_degrades_to_empty(
    mem_fs: dict[str, str],
) -> None:
    """A non-list ``serviced_comment_ids`` payload degrades to an empty set;
    non-int list elements are dropped."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "100": {
            "head_sha": "h", "delivered_at": "t",
            "serviced_comment_ids": "not-a-list",
        },
        "200": {
            "head_sha": "h", "delivered_at": "t",
            "serviced_comment_ids": [1, "x", 2],
        },
    })
    # Non-list → empty serviced, so every candidate is unserviced.
    assert unserviced_comment_ids(_BE, 100, {1, 2}) == {1, 2}
    # Mixed list → only the int elements count as serviced.
    assert unserviced_comment_ids(_BE, 200, {1, 2, 3}) == {3}


def test_unserviced_comment_ids_no_standdown_is_empty(
    mem_fs: dict[str, str],
) -> None:
    """With no standdown recorded the carve-out does not apply — empty set."""
    assert unserviced_comment_ids(_BE, 100, {1, 2, 3}) == set()


def test_mark_comment_ids_serviced_unions_across_calls(
    mem_fs: dict[str, str],
) -> None:
    """Successive marks union into one sorted serviced set."""
    _record_standdown(mem_fs, 100, serviced=[1])
    mark_comment_ids_serviced(_BE, 100, {2, 3})
    mark_comment_ids_serviced(_BE, 100, {3, 4})
    assert _ledger(mem_fs)["100"]["serviced_comment_ids"] == [1, 2, 3, 4]


def test_mark_comment_ids_serviced_noop_without_standdown(
    mem_fs: dict[str, str],
) -> None:
    """Marking ids for a PR with no standdown writes nothing (no-op)."""
    mark_comment_ids_serviced(_BE, 100, {1, 2})
    assert _ledger(mem_fs) == {}
    assert has_standdown(_BE, 100) is False


def test_clear_standdown_drops_serviced_set(mem_fs: dict[str, str]) -> None:
    """Clearing a standdown removes its serviced set with the record."""
    _record_standdown(mem_fs, 100, serviced=[1, 2])
    assert clear_standdown(_BE, 100) is True
    assert has_standdown(_BE, 100) is False
    # A fresh standdown on the same PR starts empty, so a re-detected thread
    # earns a new lap — the old serviced set died with the cleared record.
    _record_standdown(mem_fs, 100)
    assert unserviced_comment_ids(_BE, 100, {1, 2}) == {1, 2}


def test_reconcile_drops_falsely_serviced_ids(
    mem_fs: dict[str, str],
) -> None:
    """Ids in both the serviced set and the still-unanswered set are dropped;
    the returned set is exactly that intersection, and the surviving serviced
    set keeps only the genuinely-answered ids (proposal 091)."""
    _record_standdown(mem_fs, 100, serviced=[1, 2, 3])
    # 2 and 3 still show unanswered on GitHub → falsely serviced → dropped.
    dropped = reconcile_serviced_comment_ids(_BE, 100, {2, 3, 9})
    assert dropped == {2, 3}
    assert _ledger(mem_fs)["100"]["serviced_comment_ids"] == [1]


def test_reconcile_leaves_answered_ids_untouched(
    mem_fs: dict[str, str],
) -> None:
    """A serviced id absent from the still-unanswered set is genuinely
    answered — left in place, nothing dropped."""
    _record_standdown(mem_fs, 100, serviced=[1, 2])
    dropped = reconcile_serviced_comment_ids(_BE, 100, {7, 8})
    assert dropped == set()
    assert _ledger(mem_fs)["100"]["serviced_comment_ids"] == [1, 2]


def test_reconcile_noop_without_standdown(
    mem_fs: dict[str, str],
) -> None:
    """Reconciling a PR with no standdown returns an empty set and writes
    nothing, mirroring ``mark_comment_ids_serviced``."""
    dropped = reconcile_serviced_comment_ids(_BE, 100, {1, 2})
    assert dropped == set()
    assert _ledger(mem_fs) == {}
    assert has_standdown(_BE, 100) is False


def test_reconcile_reopens_valve_through_ledger(
    mem_fs: dict[str, str],
) -> None:
    """A falsely-serviced id re-enters the unserviced set after reconciliation,
    reopening the #2798 carve-out valve on the next read (round-trip)."""
    _record_standdown(mem_fs, 100, serviced=[5])
    # Before: 5 is (falsely) serviced, so it is not unserviced.
    assert unserviced_comment_ids(_BE, 100, {5}) == set()
    # Reconcile against GitHub still showing 5 unanswered drops it.
    assert reconcile_serviced_comment_ids(_BE, 100, {5}) == {5}
    # After: 5 re-enters the unserviced set — the loop will answer it.
    assert unserviced_comment_ids(_BE, 100, {5}) == {5}


def test_reconcile_empty_candidates_short_circuits(
    mem_fs: dict[str, str],
) -> None:
    """No still-unanswered ids → nothing can be dropped, so the call returns an
    empty set and leaves the serviced set untouched (the no-I/O fast path)."""
    _record_standdown(mem_fs, 100, serviced=[1, 2])
    assert reconcile_serviced_comment_ids(_BE, 100, set()) == set()
    # The serviced set is unchanged.
    assert _ledger(mem_fs)["100"]["serviced_comment_ids"] == [1, 2]


def test_prune_drops_serviced_set(mem_fs: dict[str, str]) -> None:
    """A merged/closed verdict prunes the record and its serviced set."""
    _record_standdown(mem_fs, 100, serviced=[1, 2])
    assert prune_resolved_standdowns(_BE, _always_merged) == [100]
    assert has_standdown(_BE, 100) is False
    # Re-recorded fresh after a reopen → serviced set starts empty.
    _record_standdown(mem_fs, 100)
    assert unserviced_comment_ids(_BE, 100, {1, 2}) == {1, 2}


# ---------------------------------------------------------------------------
# Claimed-escalation delivery + expiry pruning (proposal 090)
# ---------------------------------------------------------------------------


def _expiring_record(
    expires_at: str, *, delivered_at: str = "2026-01-01T00:00:00+00:00",
) -> dict[str, object]:
    """An issue-keyed claimed-escalation standdown carrying *expires_at*."""
    return {
        "head_sha": "",
        "delivered_at": delivered_at,
        "serviced_comment_ids": [],
        "expires_at": expires_at,
    }


def test_deliver_claimed_escalation_writes_message_and_standdown(
    mem_fs: dict[str, str],
) -> None:
    """First call delivers one message + one standdown carrying ``expires_at``
    (delivered_at + 30d); a second call is a no-op (idempotent per issue)."""
    first = deliver_claimed_escalation(
        _DIR, 321, 2, backend=_BE, now="2026-06-21T00:00:00+00:00",
    )
    second = deliver_claimed_escalation(
        _DIR, 321, 4, backend=_BE, now="2026-06-21T01:00:00+00:00",
    )
    assert first.delivered is True
    assert first.reason == ""
    assert second.delivered is False
    assert second.reason == "[already-stood-down]"
    messages = _messages(mem_fs)
    assert len(messages) == 1
    assert messages[0]["sender"] == "maestro"
    assert messages[0]["message_type"] == "question"
    # The id folds the delivery timestamp in so a post-expiry re-escalation
    # earns a distinct id rather than colliding with this row.
    assert (
        messages[0]["message_id"]
        == "claimed-escalation-321-2026-06-21T00:00:00+00:00"
    )
    assert "#321" in messages[0]["content"]
    assert "at least 2" in messages[0]["content"]
    # The standdown carries the empty head, empty serviced set, and the
    # age-based expiry 30 days past the delivery timestamp.
    ledger = _ledger(mem_fs)
    assert ledger["321"] == {
        "head_sha": "",
        "delivered_at": "2026-06-21T00:00:00+00:00",
        "serviced_comment_ids": [],
        "expires_at": "2026-07-21T00:00:00+00:00",
        "claim": "escalation-claimed",
    }
    assert has_standdown(_BE, 321) is True


def test_deliver_claimed_escalation_message_lets_rule12_self_extinguish(
    mem_fs: dict[str, str],
) -> None:
    """The delivered message names the issue so ``extract_issue_numbers``
    recovers it — the handle by which rule 12 goes quiet next cycle."""
    from maestro.audit_checks import extract_issue_numbers

    deliver_claimed_escalation(_DIR, 777, 3, backend=_BE, now="2026-06-21T00:00:00+00:00")
    content = _messages(mem_fs)[0]["content"]
    assert 777 in extract_issue_numbers(content)


def test_deliver_claimed_escalation_message_states_expiry_bound(
    mem_fs: dict[str, str],
) -> None:
    """The suppression promise names the ``expires_at`` time bound rather than
    claiming an open-ended hold. Unlike the non-expiring handoff/reality-refutation
    standdowns, this arm's standdown is also cleared by ``prune_expired_standdowns``
    after :data:`_CLAIMED_ESCALATION_EXPIRY`, so a PR left open past the deadline
    resumes minting — the message must not promise suppression *only* until
    merge/close, or it misstates the system's own behaviour."""
    deliver_claimed_escalation(_DIR, 88, 2, backend=_BE, now="2026-06-21T00:00:00+00:00")
    content = _messages(mem_fs)[0]["content"]
    assert f"~{_CLAIMED_ESCALATION_EXPIRY.days} days" in content
    assert "or until it merges or closes" in content
    # Pin the current constant so the rendered figure is exercised concretely.
    assert "~30 days" in content


def test_deliver_claimed_escalation_injected_store(
    mem_fs: dict[str, str],
) -> None:
    """An injected MessageStore receives the message (the test-seam path)."""
    store = MessageStore(_DIR)
    assert deliver_claimed_escalation(
        _DIR, 9, 2, backend=_BE, message_store=store, now="2026-06-21T00:00:00+00:00",
    ).delivered
    delivered = store.get_recent()
    assert len(delivered) == 1
    assert "#9" in delivered[0].content


def test_deliver_claimed_escalation_reescalation_after_expiry_distinct_id(
    mem_fs: dict[str, str],
) -> None:
    """After :func:`prune_expired_standdowns` ages the 30-day record out, a
    fresh escalation re-delivers with a *distinct* id (the delivery timestamp is
    folded in) rather than colliding with the prior row — so ``mark_read`` and
    ``get_thread`` are not conflated across the two knocks."""
    assert deliver_claimed_escalation(
        _DIR, 44, 2, backend=_BE, now="2026-06-21T00:00:00+00:00",
    ).delivered
    # Age the standdown out (well past delivered_at + 30d) so a new knock is
    # admitted, then re-escalate at a later timestamp.
    assert prune_expired_standdowns(_BE, now="2026-09-01T00:00:00+00:00") == [44]
    assert deliver_claimed_escalation(
        _DIR, 44, 3, backend=_BE, now="2026-09-02T00:00:00+00:00",
    ).delivered
    ids = [m["message_id"] for m in _messages(mem_fs)]
    assert ids == [
        "claimed-escalation-44-2026-06-21T00:00:00+00:00",
        "claimed-escalation-44-2026-09-02T00:00:00+00:00",
    ]
    assert len(set(ids)) == 2


def test_deliver_claimed_escalation_present_row_on_reread_delivered(
    mem_fs: dict[str, str],
) -> None:
    """(a) When the post-write re-read finds the keyed row, delivered=True with
    an empty reason — the delivery-truth bind confirms the durable ledger row,
    not merely the send-call return."""
    result = deliver_claimed_escalation(
        _DIR, 404, 2, backend=_BE, now="2026-06-21T00:00:00+00:00",
    )
    assert result.delivered is True
    assert result.reason == ""
    assert has_standdown(_BE, 404) is True


def test_deliver_claimed_escalation_row_missing_on_reread_not_delivered(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) A simulated row-write failure leaving no persisted row yields
    delivered=False with a '[row-missing]' reason — never a silent success."""
    import maestro.escalation_bridge as eb

    # Suppress the ledger write so the standdown row never persists, simulating
    # a failed/lost row write while the message-send path still runs.
    monkeypatch.setattr(eb, "_write_standdowns", lambda *a, **k: None)
    result = deliver_claimed_escalation(
        _DIR, 555, 2, backend=_BE, now="2026-06-21T00:00:00+00:00",
    )
    assert result.delivered is False
    assert "[row-missing]" in result.reason
    # No standdown row persisted (the write was suppressed).
    assert _ledger(mem_fs) == {}
    assert has_standdown(_BE, 555) is False


def test_deliver_claimed_escalation_message_sent_without_row_not_delivered(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) Exercising the message-send path alone — with no persisted keyed row
    — does not by itself flip delivered to True (delivery-truth bind)."""
    import maestro.escalation_bridge as eb

    monkeypatch.setattr(eb, "_write_standdowns", lambda *a, **k: None)
    result = deliver_claimed_escalation(
        _DIR, 606, 3, backend=_BE, now="2026-06-21T00:00:00+00:00",
    )
    # The message-send path ran (the merge-or-close message was persisted)...
    messages = _messages(mem_fs)
    assert len(messages) == 1
    assert "#606" in messages[0]["content"]
    # ...yet delivered stays False because the re-read found no keyed row.
    assert result.delivered is False


def test_prune_expired_clears_expired_leaves_unexpired_and_expiryless(
    mem_fs: dict[str, str],
) -> None:
    """Expiry pruning clears every ``expires_at <= now`` record (inclusive of
    the boundary), leaves an unexpired one, and never touches an
    ``expires_at``-less PR/reality-refutation record."""
    now = "2026-02-15T00:00:00+00:00"
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _expiring_record("2026-01-31T00:00:00+00:00"),  # past → clear
        "6": _expiring_record("2026-03-01T00:00:00+00:00"),  # future → keep
        "8": _expiring_record(now),  # exactly now → clear (<= is inclusive)
        # A PR handoff without expires_at is never age-expired.
        "7": {"head_sha": "abc", "delivered_at": now, "serviced_comment_ids": []},
    })
    cleared = prune_expired_standdowns(_BE, now=now)
    assert cleared == [5, 8]
    assert has_standdown(_BE, 5) is False
    assert has_standdown(_BE, 8) is False
    assert has_standdown(_BE, 6) is True
    assert has_standdown(_BE, 7) is True


def test_prune_expired_empty_when_no_ledger(mem_fs: dict[str, str]) -> None:
    assert prune_expired_standdowns(_BE, now="2026-02-15T00:00:00+00:00") == []


def test_prune_expired_empty_when_ledger_holds_none(
    mem_fs: dict[str, str],
) -> None:
    """A present-but-empty ledger prunes nothing."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({})
    assert prune_expired_standdowns(_BE, now="2026-02-15T00:00:00+00:00") == []


def test_prune_expired_empty_when_none_expired(mem_fs: dict[str, str]) -> None:
    """Only unexpired + expires_at-less records → nothing cleared, all kept."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "6": _expiring_record("2026-03-01T00:00:00+00:00"),
        "7": {
            "head_sha": "abc", "delivered_at": "2026-01-01T00:00:00+00:00",
            "serviced_comment_ids": [],
        },
    })
    assert prune_expired_standdowns(_BE, now="2026-02-15T00:00:00+00:00") == []
    assert has_standdown(_BE, 6) is True
    assert has_standdown(_BE, 7) is True


def test_load_round_trips_expires_at_through_write(
    mem_fs: dict[str, str],
) -> None:
    """A string ``expires_at`` survives a load+write cycle (here a serviced
    mark), proving ``_load_standdowns`` carries it and ``_write_standdowns``
    round-trips it."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps(
        {"5": _expiring_record("2026-02-01T00:00:00+00:00")},
    )
    mark_comment_ids_serviced(_BE, 5, {9})
    rec = _ledger(mem_fs)["5"]
    assert rec["expires_at"] == "2026-02-01T00:00:00+00:00"
    assert rec["serviced_comment_ids"] == [9]


def test_load_ignores_non_string_expires_at(mem_fs: dict[str, str]) -> None:
    """A non-string ``expires_at`` is dropped on load, so the record has no
    deadline and expiry pruning never clears it."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": {
            "head_sha": "", "delivered_at": "2026-01-01T00:00:00+00:00",
            "serviced_comment_ids": [], "expires_at": 123,
        },
    })
    assert prune_expired_standdowns(_BE, now="2999-01-01T00:00:00+00:00") == []
    assert has_standdown(_BE, 5) is True


def test_load_ignores_malformed_string_expires_at(
    mem_fs: dict[str, str],
) -> None:
    """A string ``expires_at`` that is not a valid ISO timestamp is dropped on
    load, so the every-tend-pass prune does not raise on corrupted on-disk data.

    ``prune_expired_standdowns`` calls ``datetime.fromisoformat`` on any carried
    deadline; carrying an unparseable string would crash every tend pass. The
    loader guards the format so the record keeps its standdown but no deadline —
    the same graceful degradation a non-string deadline gets."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": {
            "head_sha": "", "delivered_at": "2026-01-01T00:00:00+00:00",
            "serviced_comment_ids": [], "expires_at": "",
        },
        "7": {
            "head_sha": "", "delivered_at": "2026-01-01T00:00:00+00:00",
            "serviced_comment_ids": [], "expires_at": "not-a-timestamp",
        },
    })
    # The prune runs cleanly (no ValueError) and, with both deadlines dropped,
    # clears nothing even at a far-future ``now`` — the standdowns survive.
    assert prune_expired_standdowns(_BE, now="2999-01-01T00:00:00+00:00") == []
    assert has_standdown(_BE, 5) is True
    assert has_standdown(_BE, 7) is True


def test_load_ignores_naive_string_expires_at(
    mem_fs: dict[str, str],
) -> None:
    """A timezone-naive-yet-valid ISO ``expires_at`` is dropped on load, so the
    every-tend-pass prune does not raise ``TypeError`` on corrupted on-disk data.

    ``prune_expired_standdowns`` compares each carried deadline against a
    tz-aware ``now`` (``datetime.now(UTC)``); a naive deadline parses cleanly but
    the comparison raises ``TypeError: can't compare offset-naive and
    offset-aware datetimes``, wedging every tend pass. The loader guards
    tz-awareness so the record keeps its standdown but no deadline — the same
    graceful degradation an unparseable deadline gets."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": {
            "head_sha": "", "delivered_at": "2026-01-01T00:00:00+00:00",
            "serviced_comment_ids": [], "expires_at": "2026-01-01",
        },
        "7": {
            "head_sha": "", "delivered_at": "2026-01-01T00:00:00+00:00",
            "serviced_comment_ids": [], "expires_at": "2026-01-01T00:00:00",
        },
    })
    # The prune runs cleanly (no TypeError against a tz-aware ``now``) and, with
    # both naive deadlines dropped, clears nothing — the standdowns survive.
    assert prune_expired_standdowns(_BE, now="2999-01-01T00:00:00+00:00") == []
    assert has_standdown(_BE, 5) is True
    assert has_standdown(_BE, 7) is True


def test_deliver_claimed_escalation_rejects_naive_now(
    mem_fs: dict[str, str],
) -> None:
    """A tz-naive injected ``now`` is refused with a ``ValueError`` naming it.

    The deadline is derived from ``now``, so a naive ``now`` mints a naive
    ``expires_at`` that the loader silently drops — leaving an issue-keyed
    record ``prune_resolved_standdowns`` can never release (an issue never
    "merges as a PR") and ``prune_expired_standdowns`` never sees. Enforcing
    tz-awareness where the deadline is established fails loudly instead.
    """
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        deliver_claimed_escalation(
            _DIR, 321, 2, backend=_BE, now="2026-06-21T00:00:00",
        )


def test_deliver_claimed_escalation_naive_now_writes_nothing(
    mem_fs: dict[str, str],
) -> None:
    """The naive-``now`` rejection precedes the ``mutate`` closure, so neither a
    message nor a ledger row is written — no half-delivered escalation."""
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        deliver_claimed_escalation(
            _DIR, 321, 2, backend=_BE, now="2026-06-21T00:00:00",
        )
    assert _messages(mem_fs) == []
    assert _ledger(mem_fs) == {}


def test_deliver_claimed_escalation_rejects_malformed_now(
    mem_fs: dict[str, str],
) -> None:
    """An unparseable injected ``now`` raises a ``ValueError`` naming the
    parameter rather than a bare ``fromisoformat`` message."""
    with pytest.raises(ValueError, match="now must be an ISO timestamp"):
        deliver_claimed_escalation(
            _DIR, 321, 2, backend=_BE, now="not-a-timestamp",
        )
    assert _ledger(mem_fs) == {}


def test_prune_expired_rejects_naive_now(mem_fs: dict[str, str]) -> None:
    """A tz-naive ``now`` fails at the top of the pruner with a ``ValueError``
    naming the parameter, not a ``TypeError`` from the aware/naive comparison
    buried inside ``_apply``."""
    _record_standdown(mem_fs, 6, serviced=[])
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        prune_expired_standdowns(_BE, now="2026-02-15T00:00:00")


def test_prune_expired_rejects_malformed_now(mem_fs: dict[str, str]) -> None:
    """An unparseable ``now`` raises a ``ValueError`` naming the parameter."""
    _record_standdown(mem_fs, 6, serviced=[])
    with pytest.raises(ValueError, match="now must be an ISO timestamp"):
        prune_expired_standdowns(_BE, now="not-a-timestamp")


def test_prune_expired_empty_ledger_fast_path_precedes_now_check(
    mem_fs: dict[str, str],
) -> None:
    """The zero-cost empty-ledger no-op runs ahead of the ``now`` validation, so
    an absent ledger returns ``[]`` even for a naive ``now``."""
    assert prune_expired_standdowns(_BE, now="2026-02-15T00:00:00") == []


# ---------------------------------------------------------------------------
# deliver_retirement_disposition (proposal 091)
# ---------------------------------------------------------------------------


class _RecordingPoster:
    """A ``comment_poster`` stub capturing calls and returning a fixed result."""

    def __init__(self, *, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[int, str]] = []

    def __call__(self, number: int, body: str) -> bool:
        self.calls.append((number, body))
        return self.result


def test_deliver_retirement_disposition_posts_messages_and_records(
    mem_fs: dict[str, str],
) -> None:
    """A first delivery posts the PR comment, messages Thomas, records the standdown."""
    poster = _RecordingPoster()
    delivered = deliver_retirement_disposition(
        _DIR,
        321,
        backend=_BE, plan_title="Retire the treadmill",
        pr_url="https://github.com/o/r/pull/321",
        comment_poster=poster,
        now="2026-06-21T00:00:00+00:00",
    )
    assert delivered is True
    # The PR comment posted exactly once, to the right number, close-or-keep.
    assert len(poster.calls) == 1
    assert poster.calls[0][0] == 321
    assert "close PR #321" in poster.calls[0][1]
    # Exactly one Thomas-addressed close-or-keep message, with the distinct
    # ``disposition-<pr>`` id and the PR link in the body.
    messages = _messages(mem_fs)
    assert len(messages) == 1
    assert messages[0]["sender"] == "maestro"
    assert messages[0]["message_type"] == "question"
    assert messages[0]["message_id"] == "disposition-321"
    assert "#321" in messages[0]["content"]
    assert "https://github.com/o/r/pull/321" in messages[0]["content"]
    # A pr_number-keyed standdown with no expiry (merge/close-only release).
    ledger = _ledger(mem_fs)
    assert "321" in ledger
    assert "expires_at" not in ledger["321"]
    assert has_standdown(_BE, 321) is True


def test_deliver_retirement_disposition_idempotent(
    mem_fs: dict[str, str],
) -> None:
    """A second call after a delivered disposition returns True and re-posts nothing."""
    poster = _RecordingPoster()
    first = deliver_retirement_disposition(
        _DIR,
        321,
        backend=_BE, plan_title="Retire the treadmill",
        pr_url="https://github.com/o/r/pull/321",
        comment_poster=poster,
        now="2026-06-21T00:00:00+00:00",
    )
    assert first is True
    second = deliver_retirement_disposition(
        _DIR,
        321,
        backend=_BE, plan_title="Retire the treadmill",
        pr_url="https://github.com/o/r/pull/321",
        comment_poster=poster,
        now="2026-06-21T00:00:00+00:00",
    )
    assert second is True
    # Idempotent on the ``disposition-<pr>`` message: the PR comment posted
    # exactly once and only one message was written across both calls.
    assert len(poster.calls) == 1
    assert len(_messages(mem_fs)) == 1


def test_deliver_retirement_disposition_posts_comment_over_prior_standdown(
    mem_fs: dict[str, str],
) -> None:
    """A prior deliverer's standdown does not suppress the retirement PR comment.

    A handoff/reality-refutation standdown is on record (they post no on-PR
    comment). Retirement must still land the close-or-keep PR breadcrumb exactly
    once (proposal 091 r1 fix), and it must preserve the prior standdown record
    rather than clobbering its serviced set / head.
    """
    _record_standdown(mem_fs, 321, head_sha="abc12345", serviced=[7])
    poster = _RecordingPoster()
    delivered = deliver_retirement_disposition(
        _DIR,
        321,
        backend=_BE, plan_title="Retire the treadmill",
        pr_url="https://github.com/o/r/pull/321",
        comment_poster=poster,
        now="2026-06-21T00:00:00+00:00",
    )
    assert delivered is True
    # The on-PR comment landed despite the pre-existing standdown, and the
    # retirement message carries the distinct ``disposition-<pr>`` id.
    assert len(poster.calls) == 1
    assert poster.calls[0][0] == 321
    messages = _messages(mem_fs)
    assert len(messages) == 1
    assert messages[0]["message_id"] == "disposition-321"
    # The prior deliverer's standdown record is preserved, not clobbered.
    ledger = _ledger(mem_fs)
    assert ledger["321"]["head_sha"] == "abc12345"
    assert ledger["321"]["serviced_comment_ids"] == [7]


def test_deliver_retirement_disposition_fail_closed(
    mem_fs: dict[str, str],
) -> None:
    """A failed PR comment post yields False and writes neither message nor standdown."""
    poster = _RecordingPoster(result=False)
    delivered = deliver_retirement_disposition(
        _DIR,
        321,
        backend=_BE, plan_title="Retire the treadmill",
        pr_url="https://github.com/o/r/pull/321",
        comment_poster=poster,
        now="2026-06-21T00:00:00+00:00",
    )
    assert delivered is False
    # Fail-closed: the comment was attempted but nothing durable was written.
    assert len(poster.calls) == 1
    assert _messages(mem_fs) == []
    assert _ledger(mem_fs) == {}
    assert has_standdown(_BE, 321) is False


def test_deliver_retirement_disposition_injected_store(
    mem_fs: dict[str, str],
) -> None:
    """An injected MessageStore receives the close-or-keep message (test seam)."""
    store = MessageStore(_DIR)
    poster = _RecordingPoster()
    assert deliver_retirement_disposition(
        _DIR,
        9,
        backend=_BE, plan_title="t",
        pr_url="https://github.com/o/r/pull/9",
        comment_poster=poster,
        message_store=store,
        now="2026-06-21T00:00:00+00:00",
    )
    delivered = store.get_recent()
    assert len(delivered) == 1
    assert "#9" in delivered[0].content


def test_deliver_retirement_disposition_released_by_prune(
    mem_fs: dict[str, str],
) -> None:
    """A merged/closed verdict clears the disposition standdown via the shared prune."""
    poster = _RecordingPoster()
    assert deliver_retirement_disposition(
        _DIR,
        321,
        backend=_BE, plan_title="t",
        pr_url="https://github.com/o/r/pull/321",
        comment_poster=poster,
        now="2026-06-21T00:00:00+00:00",
    )
    cleared = prune_resolved_standdowns(_BE, _always_merged)
    assert cleared == [321]
    assert has_standdown(_BE, 321) is False


def test_deliver_retirement_disposition_backfills_missing_standdown(
    mem_fs: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-entry self-heals a half-delivered disposition (#2971).

    Drive a first delivery whose ledger persist fails after the
    ``disposition-<pr>`` message is durably appended (a ``_write_standdowns``
    ``OSError``): the message lands on disk, the standdown does not, and the
    call raises loudly. On the next cycle the message-keyed idempotence early
    return fires — but instead of assuming the second leg landed, it backfills
    the missing standdown (carrying the message's own delivery timestamp),
    without re-posting the PR comment or duplicating the message.
    """
    poster = _RecordingPoster()

    def _boom(_path: Path, _standdowns: dict[int, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("maestro.escalation_bridge._write_standdowns", _boom)
    with pytest.raises(OSError):
        deliver_retirement_disposition(
            _DIR,
            555,
            backend=_BE, plan_title="Retire the treadmill",
            pr_url="https://github.com/o/r/pull/555",
            comment_poster=poster,
            now="2026-06-21T00:00:00+00:00",
        )
    # Half-delivered: the PR comment posted, the message is durably on disk, but
    # the ledger write failed so no standdown exists.
    assert len(poster.calls) == 1
    assert len(_messages(mem_fs)) == 1
    assert _ledger(mem_fs) == {}

    # Next cycle: the real writer is restored; the same PR re-enters.
    monkeypatch.undo()
    delivered = deliver_retirement_disposition(
        _DIR,
        555,
        backend=_BE, plan_title="Retire the treadmill",
        pr_url="https://github.com/o/r/pull/555",
        comment_poster=poster,
        now="2026-06-22T00:00:00+00:00",
    )
    assert delivered is True
    # The backfill re-posts nothing and duplicates no message.
    assert len(poster.calls) == 1
    assert len(_messages(mem_fs)) == 1
    # The standdown is now on record: empty serviced set, no expiry, and the
    # ``delivered_at`` is the *original* delivery time from the message in hand,
    # not the repair-cycle ``now``.
    ledger = _ledger(mem_fs)
    assert "555" in ledger
    assert ledger["555"]["serviced_comment_ids"] == []
    assert "expires_at" not in ledger["555"]
    assert ledger["555"]["delivered_at"] == "2026-06-21T00:00:00+00:00"
    assert has_standdown(_BE, 555) is True


class _DroppedWriteLedger:
    """A backend whose ``mutate`` runs the closure and returns its result but
    whose ``read`` never reflects the write — models a silently-dropped ledger
    persist, to prove the post-write re-read bind reports non-delivery."""

    def read(self) -> dict[int, object]:
        return {}

    def mutate(self, fn: Callable[[dict[int, object]], bool]) -> bool:
        return fn({})


def test_deliver_retirement_disposition_row_missing_on_dropped_write(
    mem_fs: dict[str, str],
) -> None:
    """A silently-dropped ledger write yields False, not a phantom True (#2971).

    The post-write re-read bind confirms a ``pr_number`` row is present before
    returning ``True``. When the backend's ``mutate`` claims success but its
    ``read`` never reflects the standdown, the delivery is reported
    not-delivered (fail-closed: the sweep leaves the plan ``implemented``),
    mirroring :func:`deliver_claimed_escalation`'s ``[row-missing]`` bind.
    """
    poster = _RecordingPoster()
    delivered = deliver_retirement_disposition(
        _DIR,
        777,
        backend=_DroppedWriteLedger(), plan_title="Retire the treadmill",
        pr_url="https://github.com/o/r/pull/777",
        comment_poster=poster,
        now="2026-06-21T00:00:00+00:00",
    )
    assert delivered is False
    # The comment post was attempted (the closure ran) — the failure is the
    # re-read bind catching the dropped standdown, not a refused post.
    assert len(poster.calls) == 1


# ---------------------------------------------------------------------------
# deliver_unwinnable_race — proposal 099 step 8: a message-only, NO-standdown
# heads-up. Idempotency lives entirely in the message thread; the PR is never
# muted, so it stays fully tend-eligible and routable.
# ---------------------------------------------------------------------------


def test_deliver_unwinnable_race_writes_message_exactly_once(
    mem_fs: dict[str, str],
) -> None:
    """First call delivers one message (delivered=True, empty reason); a second
    call is the idempotent ``[already-delivered]`` no-op keyed on the thread,
    leaving the message count unchanged."""
    first = deliver_unwinnable_race(
        _DIR, 3112, 4, backend=_BE, now="2026-07-19T00:00:00+00:00",
    )
    second = deliver_unwinnable_race(
        _DIR, 3112, 5, backend=_BE, now="2026-07-19T01:00:00+00:00",
    )
    assert first == (True, "")
    assert second.delivered is False
    assert second.reason == "[already-delivered]"
    messages = _messages(mem_fs)
    assert len(messages) == 1
    assert messages[0]["sender"] == "maestro"
    assert messages[0]["message_type"] == "question"
    assert messages[0]["message_id"] == "unwinnable-race-3112"


def test_deliver_unwinnable_race_records_no_standdown(
    mem_fs: dict[str, str],
) -> None:
    """The defining property: unlike every sibling deliverer this writes NO
    stand-down, so the PR is never muted — ``has_standdown`` stays False and the
    ``escalation_handoffs.json`` ledger stays empty (PR remains routable)."""
    result = deliver_unwinnable_race(
        _DIR, 3112, 3, backend=_BE, now="2026-07-19T00:00:00+00:00",
    )
    assert result.delivered is True
    assert has_standdown(_BE, 3112) is False
    assert _ledger(mem_fs) == {}


def test_deliver_unwinnable_race_message_states_pr_count_and_no_standdown(
    mem_fs: dict[str, str],
) -> None:
    """The heads-up names the PR (so ``extract_issue_numbers`` recovers it), the
    re-entry count, and — unlike a give-up — that the loop is NOT standing
    down."""
    from maestro.audit_checks import extract_issue_numbers

    deliver_unwinnable_race(_DIR, 3112, 7, backend=_BE, now="2026-07-19T00:00:00+00:00")
    content = _messages(mem_fs)[0]["content"]
    assert 3112 in extract_issue_numbers(content)
    assert "7 times" in content
    assert "not** standing down" in content or "not standing down" in content


def test_deliver_unwinnable_race_present_thread_on_reread_delivered(
    mem_fs: dict[str, str],
) -> None:
    """(a) When the post-write re-read finds the thread, delivered=True with an
    empty reason — the delivery-truth bind confirms the durable thread, not
    merely the save-call return."""
    result = deliver_unwinnable_race(
        _DIR, 404, 3, backend=_BE, now="2026-07-19T00:00:00+00:00",
    )
    assert result.delivered is True
    assert result.reason == ""


def test_deliver_unwinnable_race_message_missing_on_reread_not_delivered(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) A simulated message-write failure leaving no persisted thread yields
    delivered=False with a '[message-missing]' reason — never a silent success —
    and still writes no stand-down."""
    # Suppress the message write so the thread never persists, simulating a
    # failed/lost write while the send path still runs.
    monkeypatch.setattr(MessageStore, "save_message", lambda self, msg: None)
    result = deliver_unwinnable_race(
        _DIR, 555, 3, backend=_BE, now="2026-07-19T00:00:00+00:00",
    )
    assert result.delivered is False
    assert "[message-missing]" in result.reason
    assert _messages(mem_fs) == []
    assert has_standdown(_BE, 555) is False


def test_deliver_unwinnable_race_injected_store(mem_fs: dict[str, str]) -> None:
    """An injected store is used verbatim (the tests' seam), and the delivery
    still records no stand-down."""
    store = MessageStore(_DIR)
    result = deliver_unwinnable_race(
        _DIR, 909, 3, backend=_BE, message_store=store, now="2026-07-19T00:00:00+00:00",
    )
    assert result.delivered is True
    assert len(store.get_thread("unwinnable-race-909")) == 1
    assert has_standdown(_BE, 909) is False


# ---------------------------------------------------------------------------
# EscalationLedgerBackend seam (proposal 106) — the JSON adapter, the SQLite
# adapter, object parity across substrates, and the load-all seed source.
# ---------------------------------------------------------------------------


class TestJsonEscalationLedgerBackend:
    def test_satisfies_backend_protocol(self, mem_fs: dict[str, str]) -> None:
        assert isinstance(
            JsonEscalationLedger(Path("/data")), EscalationLedgerBackend,
        )

    def test_read_empty_on_missing_file(self, mem_fs: dict[str, str]) -> None:
        assert JsonEscalationLedger(Path("/data")).read() == {}

    def test_read_tolerates_corruption(self, mem_fs: dict[str, str]) -> None:
        mem_fs["/data/escalation_handoffs.json"] = "not-json"
        assert JsonEscalationLedger(Path("/data")).read() == {}

    def test_mutate_persists_a_change_and_returns_result(
        self, mem_fs: dict[str, str],
    ) -> None:
        backend = JsonEscalationLedger(Path("/data"))

        def _add(m: dict[int, object]) -> int:
            m[5] = {
                "head_sha": "h5", "delivered_at": "t", "serviced_comment_ids": [],
            }
            return len(m)

        assert backend.mutate(_add) == 1
        assert backend.read()[5]["head_sha"] == "h5"

    def test_mutate_noop_writes_nothing(self, mem_fs: dict[str, str]) -> None:
        """A closure that leaves the map unchanged never creates the file — the
        "quiet no-op writes nothing" contract (proposal 106)."""
        backend = JsonEscalationLedger(Path("/data"))
        assert backend.mutate(lambda m: "untouched") == "untouched"
        assert "/data/escalation_handoffs.json" not in mem_fs


class TestEscalationLedgerOverSqliteBackend:
    def test_sqlite_backend_satisfies_protocol(self) -> None:
        assert isinstance(
            SqliteEscalationLedger(StateStore(":memory:")),
            EscalationLedgerBackend,
        )

    def test_full_lifecycle_through_sqlite(self, mem_fs: dict[str, str]) -> None:
        """The module functions drive their whole standdown op set through an
        injected SqliteEscalationLedger over an in-memory StateStore — deliver,
        read, service, unserviced carve-out, and clear all round-trip."""
        data_dir = Path("/fake/eb_sql")
        backend = SqliteEscalationLedger(StateStore(":memory:"))
        assert (
            deliver_handoff(
                data_dir,
                UnlandedCureHandoff(pr_number=7, head_sha="s7", stamp_count=3),
                backend=backend,
                now="2026-06-12T00:00:00+00:00",
                claim=StanddownClaim.HANDOFF_READY,
            )
            is True
        )
        assert has_standdown(backend, 7) is True
        assert active_standdown_prs(backend) == {7}
        assert unserviced_comment_ids(backend, 7, {1, 2}) == {1, 2}
        mark_comment_ids_serviced(backend, 7, {1})
        assert unserviced_comment_ids(backend, 7, {1, 2}) == {2}
        assert clear_standdown(backend, 7) is True
        assert has_standdown(backend, 7) is False

    def test_sqlite_mutate_noop_leaves_store_empty(self) -> None:
        """A no-op clear over an empty SQLite-backed ledger writes no row."""
        store = StateStore(":memory:")
        backend = SqliteEscalationLedger(store)
        assert clear_standdown(backend, 99) is False
        assert store.load_escalation_standdowns() == {}


def test_escalation_json_and_sqlite_backends_are_object_parity(
    mem_fs: dict[str, str],
) -> None:
    """The same op sequence yields identical reads through both backends.

    Drives ``deliver_handoff`` (+ idempotent repeat) → ``mark_comment_ids_serviced``
    → ``deliver_claimed_escalation`` (expiry) → ``deliver_unwinnable_race`` (no
    standdown) → ``prune_expired_standdowns`` → ``prune_resolved_standdowns``
    through a JSON-backed and a SQLite-backed set of module functions, asserting
    ``active_standdown_prs`` / ``has_standdown`` / ``unserviced_comment_ids`` agree
    after every step. The SQLite side uses ``StateStore(":memory:")``; each side
    keeps its own ``data_dir`` for the message store. A fixed ``now`` keeps the
    recorded timestamps identical across substrates.
    """
    now = "2026-06-12T00:00:00+00:00"
    json_be = JsonEscalationLedger(Path("/fake/eb_j"))
    sqlite_be = SqliteEscalationLedger(StateStore(":memory:"))
    pairs = [(json_be, Path("/fake/eb_j")), (sqlite_be, Path("/fake/eb_s"))]

    def _assert_parity() -> None:
        assert active_standdown_prs(json_be) == active_standdown_prs(sqlite_be)
        for pr in (100, 321, 909):
            assert has_standdown(json_be, pr) == has_standdown(sqlite_be, pr)
            assert unserviced_comment_ids(
                json_be, pr, {1, 2, 3},
            ) == unserviced_comment_ids(sqlite_be, pr, {1, 2, 3})

    _assert_parity()  # both empty

    for be, d in pairs:
        handoff = UnlandedCureHandoff(pr_number=100, head_sha="h", stamp_count=4)
        assert deliver_handoff(d, handoff, backend=be, now=now, claim=StanddownClaim.HANDOFF_READY) is True
        # Idempotent repeat is a no-op on either substrate.
        assert deliver_handoff(d, handoff, backend=be, now=now, claim=StanddownClaim.HANDOFF_READY) is False
    _assert_parity()

    for be, _d in pairs:
        mark_comment_ids_serviced(be, 100, {1})
    _assert_parity()  # #100 now excludes 1 from the unserviced carve-out

    for be, d in pairs:
        result = deliver_claimed_escalation(d, 321, 2, backend=be, now=now)
        assert result.delivered is True
        assert result.reason == ""
    _assert_parity()

    for be, d in pairs:
        race = deliver_unwinnable_race(d, 909, 3, backend=be, now=now)
        assert race.delivered is True
        # Unwinnable-race records NO standdown on either substrate.
        assert has_standdown(be, 909) is False
    _assert_parity()

    for be, _d in pairs:
        # #321's issue-keyed claimed-escalation record carries expires_at.
        assert prune_expired_standdowns(
            be, now="2099-01-01T00:00:00+00:00",
        ) == [321]
    _assert_parity()

    for be, _d in pairs:
        assert prune_resolved_standdowns(be, _always_merged) == [100]
    _assert_parity()  # both empty again


class TestLoadAllEscalationStanddowns:
    def test_absent_file_is_empty(self, mem_fs: dict[str, str]) -> None:
        assert load_all_escalation_standdowns(Path("/fake/missing")) == {}

    def test_array_root_is_empty(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps([1, 2, 3])
        assert load_all_escalation_standdowns(_DIR) == {}

    def test_corrupt_file_is_empty(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(_DIR / "escalation_handoffs.json")] = "not-json"
        assert load_all_escalation_standdowns(_DIR) == {}

    def test_migrates_legacy_pr_at_head_key(self, mem_fs: dict[str, str]) -> None:
        """A pre-080 ``"<pr>@<head>": "<iso>"`` entry collapses to a canonical
        ``number`` key with the head recovered from the key suffix."""
        mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps(
            {"100@abc12345": "2026-01-01T00:00:00+00:00"},
        )
        loaded = load_all_escalation_standdowns(_DIR)
        assert set(loaded) == {100}
        assert loaded[100]["head_sha"] == "abc12345"
        assert loaded[100]["serviced_comment_ids"] == []

    def test_returns_canonical_records(self, mem_fs: dict[str, str]) -> None:
        _record_standdown(mem_fs, 42, head_sha="h42", serviced=[7])
        loaded = load_all_escalation_standdowns(_DIR)
        assert loaded[42]["head_sha"] == "h42"
        assert loaded[42]["serviced_comment_ids"] == [7]


# ---------------------------------------------------------------------------
# claim stamping and the standdown_claims/standdown_delivery_times views (135)
# ---------------------------------------------------------------------------


class TestStanddownClaims:
    """Each writer stamps the fact it asserts; the claim round-trips both stores."""

    def test_deliver_handoff_stamps_handoff_ready(
        self, mem_fs: dict[str, str],
    ) -> None:
        handoff = UnlandedCureHandoff(
            pr_number=2579, head_sha="abc12345", stamp_count=4,
        )
        deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00", claim=StanddownClaim.HANDOFF_READY)
        assert _ledger(mem_fs)["2579"]["claim"] == "handoff-ready"

    def test_deliver_reality_refutation_stamps_reality_refuted(
        self, mem_fs: dict[str, str],
    ) -> None:
        deliver_reality_refutation(
            _DIR, 4242, 3, backend=_BE, now="2026-06-21T00:00:00+00:00",
        )
        assert _ledger(mem_fs)["4242"]["claim"] == "reality-refuted"

    def test_deliver_claimed_escalation_stamps_escalation_claimed(
        self, mem_fs: dict[str, str],
    ) -> None:
        deliver_claimed_escalation(
            _DIR, 321, 2, backend=_BE, now="2026-06-21T00:00:00+00:00",
        )
        assert _ledger(mem_fs)["321"]["claim"] == "escalation-claimed"

    def test_deliver_retirement_disposition_stamps_retirement_pending(
        self, mem_fs: dict[str, str],
    ) -> None:
        deliver_retirement_disposition(
            _DIR,
            321,
            backend=_BE,
            plan_title="Retire the treadmill",
            pr_url="https://github.com/o/r/pull/321",
            comment_poster=_RecordingPoster(),
            now="2026-06-21T00:00:00+00:00",
        )
        assert _ledger(mem_fs)["321"]["claim"] == "retirement-pending"

    def test_retirement_backfill_stamps_its_claim_too(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The self-heal arm writes the same seed the fresh path writes.

        A prior cycle durably posted the disposition message but lost its ledger
        persist; the backfill must not leave a claim-less row behind, or the
        validity oracle would read a retirement record as unclaimed.
        """
        poster = _RecordingPoster()
        deliver_retirement_disposition(
            _DIR,
            77,
            backend=_BE,
            plan_title="Retire",
            pr_url="https://github.com/o/r/pull/77",
            comment_poster=poster,
            now="2026-06-21T00:00:00+00:00",
        )
        # Drop the ledger row, leaving only the durable disposition message.
        _BE.mutate(lambda m: m.pop(77, None))
        assert has_standdown(_BE, 77) is False
        assert deliver_retirement_disposition(
            _DIR,
            77,
            backend=_BE,
            plan_title="Retire",
            pr_url="https://github.com/o/r/pull/77",
            comment_poster=poster,
            now="2026-06-22T00:00:00+00:00",
        ) is True
        assert _ledger(mem_fs)["77"]["claim"] == "retirement-pending"

    def test_claim_round_trips_through_the_json_hydration(
        self, mem_fs: dict[str, str],
    ) -> None:
        handoff = UnlandedCureHandoff(
            pr_number=2579, head_sha="abc12345", stamp_count=4,
        )
        deliver_handoff(_DIR, handoff, backend=_BE, now="2026-06-12T00:00:00+00:00", claim=StanddownClaim.HANDOFF_READY)
        assert _BE.read()[2579]["claim"] == "handoff-ready"

    def test_claim_round_trips_through_the_sqlite_substrate(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The SQLite store rebuilds a fresh literal, so the key must survive it."""
        backend = SqliteEscalationLedger(StateStore(":memory:"))
        deliver_handoff(
            Path("/fake/eb_claims"),
            UnlandedCureHandoff(pr_number=99, head_sha="h", stamp_count=4),
            backend=backend,
            now="2026-06-12T00:00:00+00:00",
            claim=StanddownClaim.HANDOFF_READY,
        )
        assert backend.read()[99]["claim"] == "handoff-ready"

    def test_legacy_row_without_a_claim_loads_unclaimed(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A pre-135 row keeps no claim — never defaulted to handoff-ready.

        Defaulting would mis-attribute a retirement-disposition or
        reality-refutation record, letting a live reading "refute" a claim that
        record never made. The ledger file is not rewritten on upgrade either.
        """
        _record_standdown(mem_fs, 555, head_sha="old", serviced=[1])
        before = _ledger(mem_fs)
        assert "claim" not in _BE.read()[555]
        assert standdown_claims(_BE.read()) == {}
        assert _ledger(mem_fs) == before

    def test_non_string_claim_degrades_to_unclaimed(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A corrupt claim value is dropped, in the ``expires_at`` style."""
        mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps(
            {
                "606": {
                    "head_sha": "h",
                    "delivered_at": "2026-06-12T00:00:00+00:00",
                    "serviced_comment_ids": [],
                    "claim": 17,
                },
            },
        )
        assert "claim" not in _BE.read()[606]

    def test_standdown_claims_returns_only_claimed_records(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The accessor surfaces raw claim strings, skipping unclaimed rows."""
        deliver_handoff(
            _DIR,
            UnlandedCureHandoff(pr_number=11, head_sha="h", stamp_count=4),
            backend=_BE,
            now="2026-06-12T00:00:00+00:00",
            claim=StanddownClaim.HANDOFF_READY,
        )
        deliver_reality_refutation(
            _DIR, 12, 3, backend=_BE, now="2026-06-21T00:00:00+00:00",
        )
        _record_standdown(mem_fs, 13, head_sha="legacy")
        records = _BE.read()
        assert standdown_claims(records) == {
            11: "handoff-ready",
            12: "reality-refuted",
        }
        # The sibling view over the *same* read supplies each latch's delivery
        # time, the staleness floor below which a readiness snapshot cannot
        # refute the claim it predates.
        assert standdown_delivery_times(records)[11] == "2026-06-12T00:00:00+00:00"
        assert set(standdown_delivery_times(records)) == {11, 12, 13}
        # Every number still carries a standdown — the claims view is narrower
        # than the presence view, and only presence gates minting.
        assert active_standdown_prs(_BE) == {11, 12, 13}

    def test_standdown_delivery_times_skips_a_missing_or_corrupt_stamp(
        self,
    ) -> None:
        """A hand-edited row without a usable ``delivered_at`` yields no floor.

        Absent from the map means *unfiltered* downstream, which is the tolerant
        direction: an unreadable stamp must not blank a PR's reading.
        """
        records: dict[int, object] = {
            1: {"head_sha": "h", "delivered_at": "", "serviced_comment_ids": []},
            2: {"head_sha": "h", "delivered_at": 17, "serviced_comment_ids": []},
            3: {"head_sha": "h", "serviced_comment_ids": []},
            4: {
                "head_sha": "h",
                "delivered_at": "2026-06-12T00:00:00+00:00",
                "serviced_comment_ids": [],
            },
        }
        assert standdown_delivery_times(records) == {  # type: ignore[arg-type]
            4: "2026-06-12T00:00:00+00:00",
        }

    def test_standdown_claims_empty_without_a_ledger(
        self, mem_fs: dict[str, str],
    ) -> None:
        assert standdown_claims(_BE.read()) == {}


# ---------------------------------------------------------------------------
# prune_refuted_standdowns (proposal 135) — the third release path
# ---------------------------------------------------------------------------


def _claimed_record(claim: str, *, expires_at: str = "") -> dict[str, object]:
    """A standdown record carrying *claim* (and optionally an ``expires_at``)."""
    rec: dict[str, object] = {
        "head_sha": "abc",
        "delivered_at": "2026-01-01T00:00:00+00:00",
        "serviced_comment_ids": [],
        "claim": claim,
    }
    if expires_at:
        rec["expires_at"] = expires_at
    return rec


def _owed(acts: dict[int, OwedAct]) -> Callable[[int], OwedAct | None]:
    """A ``live_owed_act`` predicate reading from a fixed map."""
    return lambda pr: acts.get(pr)


def test_prune_refuted_releases_handoff_claim_on_code_owed(
    mem_fs: dict[str, str],
) -> None:
    """The one refuting pair: a ``handoff-ready`` latch read ``CODE_OWED``.

    The regression this whole path exists for — a sibling merge turns the
    handed-off PR ``CONFLICTING``, so the owed act flips back to *write code*
    while the latch is still suppressing the remediation that would clear it.
    """
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    outcome = prune_refuted_standdowns(_BE, _owed({5: OwedAct.CODE_OWED}))
    assert outcome == ([5], [])
    assert has_standdown(_BE, 5) is False


@pytest.mark.parametrize(
    "act",
    [OwedAct.HANDOFF_READY, OwedAct.WAIT_CI, OwedAct.EXTERNAL_BLOCKED],
)
def test_prune_refuted_retains_handoff_claim_on_non_refuting_act(
    mem_fs: dict[str, str], act: OwedAct,
) -> None:
    """Still green-and-waiting, still on the CI clock, or externally gated —
    none contradicts "only Thomas can act", so the latch holds."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    assert prune_refuted_standdowns(_BE, _owed({5: act})) == ([], [])
    assert has_standdown(_BE, 5) is True


def test_prune_refuted_retains_claimed_escalation_on_real_code_owed_pr(
    mem_fs: dict[str, str],
) -> None:
    """Selection keys on the **claim**, never on the absence of ``expires_at``.

    An ``expires_at``-bearing claimed-escalation record is routinely keyed on a
    real PR number; its claim asserts something the owed-act oracle does not
    answer, so it survives even when that very PR reads ``CODE_OWED`` — and keeps
    its age-based release via ``prune_expired_standdowns``.
    """
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record(
            "escalation-claimed", expires_at="2026-03-01T00:00:00+00:00",
        ),
    })
    assert prune_refuted_standdowns(
        _BE, _owed({5: OwedAct.CODE_OWED}),
    ) == ([], [])
    assert has_standdown(_BE, 5) is True


@pytest.mark.parametrize(
    "claim", ["reality-refuted", "retirement-pending", "not-a-known-claim"],
)
def test_prune_refuted_retains_non_handoff_and_unparseable_claims(
    mem_fs: dict[str, str], claim: str,
) -> None:
    """A non-handoff claim — or an unrecognised one — is never refutable."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record(claim),
    })
    assert prune_refuted_standdowns(
        _BE, _owed({5: OwedAct.CODE_OWED}),
    ) == ([], [])
    assert has_standdown(_BE, 5) is True


def test_prune_refuted_retains_unclaimed_legacy_row(
    mem_fs: dict[str, str],
) -> None:
    """A row written before the ``claim`` key existed made no claim to refute."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": {
            "head_sha": "abc", "delivered_at": "2026-01-01T00:00:00+00:00",
            "serviced_comment_ids": [],
        },
    })
    assert prune_refuted_standdowns(
        _BE, _owed({5: OwedAct.CODE_OWED}),
    ) == ([], [])
    assert has_standdown(_BE, 5) is True


def test_prune_refuted_retains_unmeasured_pr(mem_fs: dict[str, str]) -> None:
    """A ``None`` reading (the walk did not measure this PR) keeps the latch."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    assert prune_refuted_standdowns(_BE, _owed({})) == ([], [])
    assert has_standdown(_BE, 5) is True


def test_prune_refuted_releases_sorted_subset(mem_fs: dict[str, str]) -> None:
    """Mixed ledger: only the refuted keys drop, returned sorted."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "9": _claimed_record("handoff-ready"),
        "5": _claimed_record("handoff-ready"),
        "7": _claimed_record("handoff-ready"),
    })
    outcome = prune_refuted_standdowns(
        _BE,
        _owed({
            9: OwedAct.CODE_OWED,
            5: OwedAct.CODE_OWED,
            7: OwedAct.WAIT_CI,
        }),
    )
    assert outcome.released == [5, 9]
    assert has_standdown(_BE, 7) is True


def test_prune_refuted_empty_when_no_ledger(mem_fs: dict[str, str]) -> None:
    """The lock-free empty-ledger fast path — no ``mutate``, no predicate call."""
    calls: list[int] = []

    def _probe(pr: int) -> OwedAct | None:
        calls.append(pr)
        return OwedAct.CODE_OWED

    assert prune_refuted_standdowns(_BE, _probe) == ([], [])
    assert calls == []


def test_prune_refuted_empty_when_ledger_holds_none(
    mem_fs: dict[str, str],
) -> None:
    """A present-but-empty ledger short-circuits the same way."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({})
    assert prune_refuted_standdowns(_BE, _owed({})) == ([], [])


def test_prune_refuted_raising_predicate_propagates(
    mem_fs: dict[str, str],
) -> None:
    """Fail-loud, matching both siblings: a raising reading is never swallowed
    into "nothing refuted" — that would silently leave a refuted latch live."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })

    def _boom(_pr: int) -> OwedAct | None:
        raise RuntimeError("owed-act reading failed")

    with pytest.raises(RuntimeError, match="owed-act reading failed"):
        prune_refuted_standdowns(_BE, _boom)
    assert has_standdown(_BE, 5) is True


def test_prune_refuted_works_on_the_sqlite_backend(
    mem_fs: dict[str, str],
) -> None:
    """The release runs through the ``EscalationLedgerBackend`` seam, so the
    ``state.db`` substrate releases identically to the JSON one."""
    backend: EscalationLedgerBackend = SqliteEscalationLedger(
        StateStore(":memory:"),
    )
    deliver_handoff(
        _DIR,
        UnlandedCureHandoff(pr_number=5, head_sha="abc", stamp_count=7),
        backend=backend,
        claim=StanddownClaim.HANDOFF_READY,
    )
    assert has_standdown(backend, 5) is True
    assert prune_refuted_standdowns(
        backend, _owed({5: OwedAct.CODE_OWED}),
    ) == ([5], [])
    assert has_standdown(backend, 5) is False


# ---------------------------------------------------------------------------
# prune_refuted_standdowns — the spoken withdrawal and the release budget (135)
# ---------------------------------------------------------------------------


def _refute(
    mem_fs: dict[str, str],
    *,
    pr: int = 5,
    cap: int = 2,
    blocker: str = "CONFLICTING",
    backend: EscalationLedgerBackend | None = None,
) -> RefutedReleaseOutcome:
    """Run one budgeted refuted-release pass over *pr*'s ``handoff-ready`` latch."""
    return prune_refuted_standdowns(
        backend if backend is not None else _BE,
        _owed({pr: OwedAct.CODE_OWED}),
        data_dir=_DIR,
        blockers={pr: blocker},
        release_cap=cap,
        now="2026-02-02T00:00:00+00:00",
    )


def test_refuted_release_appends_exactly_one_withdrawal_message(
    mem_fs: dict[str, str],
) -> None:
    """The released claim is taken back out loud, naming the PR and the blocker.

    The handoff told Thomas only he could land this PR; the release has measured
    that false, so deleting the ledger row without retracting the announcement
    would leave a known-false claim standing in his inbox.
    """
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    outcome = _refute(mem_fs)
    assert outcome == ([5], [])
    assert has_standdown(_BE, 5) is False
    sent = _messages(mem_fs)
    assert len(sent) == 1
    assert sent[0]["message_id"] == "standdown-withdrawn-5-1"
    assert sent[0]["sender"] == "maestro"
    assert sent[0]["message_type"] == "question"
    content = sent[0]["content"]
    assert isinstance(content, str)
    assert "#5" in content
    assert "CONFLICTING" in content
    assert "code-owed" in content


def test_refuted_release_thread_refutation_omits_the_none_sentinel(
    mem_fs: dict[str, str],
) -> None:
    """A thread-refuted latch reads the generic reason, never ``"NONE"``.

    ``_observe_owed_act`` reports an unresolved review thread as ``code-owed``
    carrying the ``MERGE_BLOCK_NONE`` sentinel — "no deterministic merge
    blocker", not a reason. Rendering it verbatim would tell Thomas the live
    reading "shows NONE", defeating the message's promise to name the concrete
    reason the claim no longer holds.
    """
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    outcome = _refute(mem_fs, blocker=MERGE_BLOCK_NONE)
    assert outcome == ([5], [])
    content = _messages(mem_fs)[0]["content"]
    assert isinstance(content, str)
    assert "NONE" not in content
    assert "a newly measured blocker" in content
    assert "#5" in content
    assert "code-owed" in content


def test_refuted_release_reads_the_owed_act_once_per_key(
    mem_fs: dict[str, str],
) -> None:
    """The withdrawal names the *selection* reading, not a second probe.

    ``live_owed_act`` is documented as arbitrary and fail-loud, so re-invoking it
    to render the message could name an act that did not motivate the release.
    A predicate that answers ``CODE_OWED`` once and then flips proves the act is
    carried forward: exactly one call, and the first answer in the prose.
    """
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    calls: list[int] = []

    def _flipping(pr: int) -> OwedAct | None:
        calls.append(pr)
        return OwedAct.CODE_OWED if len(calls) == 1 else OwedAct.WAIT_CI

    outcome = prune_refuted_standdowns(
        _BE, _flipping, data_dir=_DIR, blockers={5: "CONFLICTING"},
        release_cap=2, now="2026-02-02T00:00:00+00:00",
    )
    assert outcome == ([5], [])
    assert calls == [5]
    content = _messages(mem_fs)[0]["content"]
    assert isinstance(content, str)
    assert "code-owed" in content
    assert "wait-ci" not in content


def test_refuted_release_second_cycle_sends_the_second_withdrawal(
    mem_fs: dict[str, str],
) -> None:
    """A second handoff→refutation cycle on the same PR earns its own id.

    The release index is folded into the id so a re-release cannot collide with
    the prior row — a collision would break ``mark_read`` (which stops at the
    first id match) and conflate ``get_thread``.
    """
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    assert _refute(mem_fs).released == [5]
    # Thomas's PR went green again, the loop handed it off afresh, and a sibling
    # merge turned it CONFLICTING a second time.
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    assert _refute(mem_fs).released == [5]
    ids = [m["message_id"] for m in _messages(mem_fs)]
    assert ids == ["standdown-withdrawn-5-1", "standdown-withdrawn-5-2"]


def test_refuted_release_at_cap_keeps_the_latch_and_says_so_once(
    mem_fs: dict[str, str],
) -> None:
    """A third refutation under a cap of 2 is terminal: the latch stays.

    Unbounded, a PR flapping between merge-ready and CONFLICTING would cycle
    handoff → refutation → handoff forever — a message storm rather than the
    silent deadlock it replaced. Repeated passes over the capped PR are a
    complete no-op: ``messages.jsonl`` grows by exactly one terminal row across
    all of them.
    """
    for _ in range(2):
        mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
            "5": _claimed_record("handoff-ready"),
        })
        assert _refute(mem_fs).released == [5]
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    assert _refute(mem_fs) == ([], [5])
    assert has_standdown(_BE, 5) is True
    for _ in range(2):
        assert _refute(mem_fs) == ([], [])
        assert has_standdown(_BE, 5) is True
    terminal = [
        m for m in _messages(mem_fs)
        if m["message_id"] == "standdown-refutation-capped-5"
    ]
    assert len(terminal) == 1
    content = terminal[0]["content"]
    assert isinstance(content, str)
    assert "#5" in content
    assert "2 time(s)" in content


def test_refuted_release_holding_claim_writes_nothing(
    mem_fs: dict[str, str],
) -> None:
    """A ``HOLDS`` record under the budgeted path still sends no message."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    outcome = prune_refuted_standdowns(
        _BE,
        _owed({5: OwedAct.WAIT_CI}),
        data_dir=_DIR,
        blockers={},
        release_cap=2,
    )
    assert outcome == ([], [])
    assert has_standdown(_BE, 5) is True
    assert _messages(mem_fs) == []


def test_refuted_release_without_measured_blocker_degrades_prose(
    mem_fs: dict[str, str],
) -> None:
    """A PR missing from the blockers map still withdraws, in generic prose —
    an unmeasured reason must not suppress the retraction itself."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    assert _refute(mem_fs, blocker="").released == [5]
    content = _messages(mem_fs)[0]["content"]
    assert isinstance(content, str)
    assert "a newly measured blocker" in content


def test_refuted_release_keeps_latch_when_withdrawal_does_not_land(
    mem_fs: dict[str, str],
) -> None:
    """Delivery-truth bind: a lost withdrawal write is never a silent release.

    Mirrors ``deliver_unwinnable_race`` — the thread is re-read after the write
    rather than trusting ``save_message``, and a record whose retraction did not
    land keeps its latch so the next pass retries.
    """
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    store = MessageStore(_DIR)
    # A write that silently does nothing — the post-write re-read is what makes
    # the loss visible.
    store.save_message = lambda _msg: None  # type: ignore[method-assign]
    outcome = prune_refuted_standdowns(
        _BE,
        _owed({5: OwedAct.CODE_OWED}),
        blockers={5: "CONFLICTING"},
        release_cap=2,
        message_store=store,
    )
    assert outcome == ([], [])
    assert has_standdown(_BE, 5) is True


def test_refuted_release_requires_a_message_root_when_budgeted(
    mem_fs: dict[str, str],
) -> None:
    """Budgeting without a store or a root is a programming error, not a
    silently unspoken release."""
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    with pytest.raises(ValueError, match="data_dir or message_store"):
        prune_refuted_standdowns(_BE, _owed({}), release_cap=2)


def test_refuted_release_budgeted_path_on_the_sqlite_backend(
    mem_fs: dict[str, str],
) -> None:
    """The withdrawal + cap run through the ``EscalationLedgerBackend`` seam, so
    the ``state.db`` substrate behaves identically to the JSON one."""
    backend: EscalationLedgerBackend = SqliteEscalationLedger(
        StateStore(":memory:"),
    )
    for index in (1, 2):
        deliver_handoff(
            _DIR,
            UnlandedCureHandoff(pr_number=5, head_sha="abc", stamp_count=7),
            backend=backend,
            release_cap=2,
            claim=StanddownClaim.HANDOFF_READY,
        )
        assert _refute(mem_fs, cap=2, backend=backend).released == [5]
        assert f"standdown-withdrawn-5-{index}" in [
            m["message_id"] for m in _messages(mem_fs)
        ]
    deliver_handoff(
        _DIR,
        UnlandedCureHandoff(pr_number=5, head_sha="abc", stamp_count=7),
        backend=backend,
        release_cap=2,
        claim=StanddownClaim.HANDOFF_READY,
    )
    assert _refute(mem_fs, cap=2, backend=backend) == ([], [5])
    assert has_standdown(backend, 5) is True


def test_count_refutation_releases_stops_at_the_first_gap(
    mem_fs: dict[str, str],
) -> None:
    """The durable message ledger is the counter, and the probe is bounded by
    the cap — a non-contiguous id set cannot inflate the count."""
    store = MessageStore(_DIR)
    for message_id in ("standdown-withdrawn-5-1", "standdown-withdrawn-5-3"):
        store.save_message(Message(
            message_id=message_id,
            timestamp="2026-02-02T00:00:00+00:00",
            sender="maestro",
            content="x",
            message_type="question",
        ))
    assert count_refutation_releases(store, 5, cap=5) == 1
    assert count_refutation_releases(store, 6, cap=5) == 0


def test_deliver_handoff_stamps_the_refutation_release_projection(
    mem_fs: dict[str, str],
) -> None:
    """The successor record shows how many times this latch has been taken back.

    Read-only projection, explicitly not the cap authority: the release deletes
    the record, so a counter living on it would be destroyed by the event it
    counts.
    """
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    assert _refute(mem_fs).released == [5]
    deliver_handoff(
        _DIR,
        UnlandedCureHandoff(pr_number=5, head_sha="abc", stamp_count=7),
        backend=_BE,
        release_cap=2,
        claim=StanddownClaim.HANDOFF_READY,
    )
    record = _ledger(mem_fs)["5"]
    assert isinstance(record, dict)
    assert record["refutation_releases"] == 1


def test_deliver_handoff_cfg_derives_both_arguments_from_config(
    mem_fs: dict[str, str],
) -> None:
    """The config-aware door wires ``memory_dir`` and the *derived* budget.

    Every production handoff site goes through this wrapper precisely so the
    ``refutation_releases`` projection cannot be silently omitted by a site that
    forgot to thread the cap by hand.
    """
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({
        "5": _claimed_record("handoff-ready"),
    })
    assert _refute(mem_fs).released == [5]
    config = Config(
        memory_dir=_DIR,
        refutable_standdown_enabled=True,
        standdown_refutation_release_cap=2,
    )
    assert deliver_handoff_cfg(
        config,
        UnlandedCureHandoff(pr_number=5, head_sha="abc", stamp_count=7),
        backend=_BE,
        claim=StanddownClaim.HANDOFF_READY,
    ) is True
    record = _ledger(mem_fs)["5"]
    assert isinstance(record, dict)
    assert record["refutation_releases"] == 1


def test_deliver_handoff_cfg_skips_the_probe_while_the_flag_is_off(
    mem_fs: dict[str, str],
) -> None:
    """A configured cap with the master flag off derives 0, so the record is
    written byte-identically to the pre-135 shape."""
    config = Config(
        memory_dir=_DIR,
        refutable_standdown_enabled=False,
        standdown_refutation_release_cap=5,
    )
    assert deliver_handoff_cfg(
        config,
        UnlandedCureHandoff(pr_number=5, head_sha="abc", stamp_count=7),
        backend=_BE,
        claim=StanddownClaim.HANDOFF_READY,
    ) is True
    record = _ledger(mem_fs)["5"]
    assert isinstance(record, dict)
    assert "refutation_releases" not in record


def test_deliver_handoff_omits_the_projection_when_unbudgeted(
    mem_fs: dict[str, str],
) -> None:
    """The default ``release_cap=0`` skips the probe, so every existing caller
    writes the record byte-identically to today's."""
    deliver_handoff(
        _DIR,
        UnlandedCureHandoff(pr_number=5, head_sha="abc", stamp_count=7),
        backend=_BE,
        claim=StanddownClaim.HANDOFF_READY,
    )
    record = _ledger(mem_fs)["5"]
    assert isinstance(record, dict)
    assert "refutation_releases" not in record


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(3, 3), (0, 0), (True, None), ("2", None), (None, None)],
)
def test_refutation_releases_round_trips_through_the_parser(
    mem_fs: dict[str, str], raw: object, expected: int | None,
) -> None:
    """A real ``int`` carries through; a bool (an ``int`` subclass) or a string
    is dropped, and a legacy row lacking the key loads unchanged."""
    record: dict[str, object] = {
        "head_sha": "abc",
        "delivered_at": "2026-01-01T00:00:00+00:00",
        "serviced_comment_ids": [],
    }
    if raw is not None:
        record["refutation_releases"] = raw
    mem_fs[str(_DIR / "escalation_handoffs.json")] = json.dumps({"5": record})
    loaded = load_all_escalation_standdowns(_DIR)[5]
    assert loaded.get("refutation_releases") == expected


def test_ready_claim_handoff_states_readiness_not_lap_exhaustion(
    mem_fs: dict[str, str],
) -> None:
    """A HANDOFF_READY handoff states readiness, not a lap count (#4390)."""
    handoff = UnlandedCureHandoff(pr_number=4400, head_sha="", stamp_count=1)
    assert deliver_handoff(
        _DIR, handoff, backend=_BE, now="2026-10-03T00:00:00+00:00",
        claim=StanddownClaim.HANDOFF_READY,
    )
    content = _messages(mem_fs)[0]["content"]
    assert "unlanded remediation lap" not in content
    assert "#4400" in content
    assert "is merge-ready and waiting on you" in content


def test_exhausted_claim_handoff_keeps_lap_text(mem_fs: dict[str, str]) -> None:
    handoff = UnlandedCureHandoff(pr_number=4400, head_sha="", stamp_count=4)
    assert deliver_handoff(
        _DIR, handoff, backend=_BE, now="2026-10-03T00:00:00+00:00",
        claim=StanddownClaim.LAP_BUDGET_EXHAUSTED,
    )
    content = _messages(mem_fs)[0]["content"]
    assert "4 unlanded remediation lap(s)" in content
    assert "merge-ready" not in content


def test_rehandoff_after_release_gets_distinct_message_id(
    mem_fs: dict[str, str],
) -> None:
    """A repeat handoff after a release must not reuse the first id (#4390)."""
    handoff = UnlandedCureHandoff(pr_number=4400, head_sha="", stamp_count=1)
    for round_ in range(3):
        assert deliver_handoff(
            _DIR, handoff, backend=_BE, now=f"2026-10-03T00:0{round_}:00+00:00",
            claim=StanddownClaim.HANDOFF_READY,
        )
        _BE.mutate(lambda standdowns: standdowns.pop(4400))
    ids = [m["message_id"] for m in _messages(mem_fs)]
    assert ids == ["handoff-4400-nohead", "handoff-4400-nohead-2", "handoff-4400-nohead-3"]
    assert len(MessageStore(_DIR).get_thread(ids[0])) == 1


def test_fresh_handoff_id_probe_is_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = MagicMock()
    store.get_thread.return_value = [object()]
    assert _fresh_handoff_message_id(store, 7, "") == (
        f"handoff-7-nohead-{_HANDOFF_ID_PROBE_CAP + 1}"
    )
    assert store.get_thread.call_count == _HANDOFF_ID_PROBE_CAP
    assert "handoff id probe for PR #7 exhausted" in caplog.text

"""Tests for mechanical self-audit rules."""

from __future__ import annotations

import ast
import inspect
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from maestro import audit_checks
from maestro.audit_checks import (
    _DIRECTIVE_MAX_CHARS,
    _REALITY_GROUNDED_RULES,
    MAX_CITES_PER_FINDING,
    MAX_FINDINGS_PER_CYCLE,
    MAX_FINDINGS_PER_RULE,
    AuditFinding,
    JsonImpactReviewSurfacedStore,
    JsonlGateActivationLedger,
    check_evolution_impact_reviews,
    check_kept_task_not_promoted,
    check_repeated_error_without_limitation,
    check_repeated_target_attempt,
    check_stability_claim_vs_failure_streak,
    check_stale_self_model_entry,
    check_stalled_question,
    check_success_claim_vs_rate,
    check_unbound_capability_claim,
    check_undefined_coinage_directive,
    check_ungrounded_question_closure,
    compute_audit_stall_counts,
    extract_issue_numbers,
    format_investigation_rate_context,
    format_known_state_summary,
    format_reality_refutation_summary,
    format_undefined_coinage_summary,
    is_grounded_closure,
    lineage_keys,
    measure_reality_refutation,
    measure_undefined_coinages,
    run_all_checks,
    select_curation_target,
    truncate_directive,
)
from maestro.audit_checks_calibration import (
    JsonlPredictionLedger,
)
from maestro.audit_checks_phantom import (
    check_undercredited_landing,
)
from maestro.consciousness import (
    SelfModel,
    _normalize_question,
    claim_key,
)
from maestro.memory import (
    AuditResult,
    EvolutionPlan,
    Memory,
    PlanStep,
    QuestionResolution,
    TaskRecord,
    TaskSource,
    VersionRecord,
)

FAKE_DIR = Path("/fake/mem")


def _gate_backend(data_dir: Path = FAKE_DIR) -> JsonlGateActivationLedger:
    """JSONL gate-activation backend over *data_dir* (default ``FAKE_DIR``).

    The re-signatured writer/readers (proposal 106) take a
    ``GateActivationBackend`` instead of a data dir; this binds the default-off
    JSONL adapter so the tests keep exercising the same ``gate_activations.jsonl``
    substrate under ``mem_fs``. The adapter is stateless (it re-reads the file on
    each ``read_all``), so a fresh instance per call is fine.
    """
    return JsonlGateActivationLedger(data_dir)


def _prediction_backend(data_dir: Path = FAKE_DIR) -> JsonlPredictionLedger:
    """JSONL prediction backend over *data_dir* (default ``FAKE_DIR``).

    The re-signatured prediction writer/readers (proposal 106) take a
    ``PredictionBackend`` instead of a data dir; this binds the default-off JSONL
    adapter so the tests keep exercising the same ``predictions.jsonl`` substrate
    under ``mem_fs``. The adapter is stateless (it re-reads the file on each
    ``read_all``), so a fresh instance per call is fine.
    """
    return JsonlPredictionLedger(data_dir)


def _task(
    task_id: str,
    outcome: str = "success",
    errors: list[str] | None = None,
    goal: str = "goal",
    branch: str | None = None,
    key_findings: str | None = None,
    source: TaskSource = "external",
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        goal=goal,
        plan=[PlanStep(step_id="s1", description="x", status="done")],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary="",
        errors=errors or [],
        duration_secs=0.5,
        timestamp="2025-01-01T00:00:00Z",
        branch=branch,
        key_findings=key_findings,
        source=source,
    )


# ---------------------------------------------------------------------------
# Rule 1: success_claim_vs_rate
# ---------------------------------------------------------------------------


def test_rule1_fires_when_effective_claim_and_low_rate() -> None:
    model = SelfModel(capabilities_known=["effective planning"])
    tasks = [_task(f"t{i}", outcome="failure") for i in range(8)] + [
        _task(f"t{i}", outcome="success") for i in range(8, 10)
    ]
    findings = check_success_claim_vs_rate(model, tasks, success_rate=0.2)
    assert len(findings) == 1
    assert findings[0].kind == "contradicted_claim"
    assert findings[0].rule == "success_claim_vs_rate"
    assert "effective planning" in findings[0].statement
    assert findings[0].cites  # at least one cite
    assert len(findings[0].cites) <= MAX_CITES_PER_FINDING


def test_rule1_silent_below_min_tasks() -> None:
    model = SelfModel(capabilities_known=["effective planning"])
    tasks = [_task(f"t{i}", outcome="failure") for i in range(4)]
    findings = check_success_claim_vs_rate(model, tasks, success_rate=0.0)
    assert findings == []


def test_rule1_silent_when_rate_above_threshold() -> None:
    model = SelfModel(capabilities_known=["effective planning"])
    tasks = [_task(f"t{i}", outcome="success") for i in range(10)]
    findings = check_success_claim_vs_rate(model, tasks, success_rate=0.9)
    assert findings == []


def test_rule1_ignores_capability_without_success_keyword() -> None:
    model = SelfModel(capabilities_known=["careful reasoning"])
    tasks = [_task(f"t{i}", outcome="failure") for i in range(8)]
    findings = check_success_claim_vs_rate(model, tasks, success_rate=0.1)
    assert findings == []


def test_rule1_ignores_negation_of_keyword() -> None:
    """`ineffective` must NOT trigger the `effective` rule.

    This is the regression guard for the substring-vs-token bug: a
    capability that explicitly negates effectiveness would otherwise
    spuriously hit the rule if we matched on substring.
    """
    model = SelfModel(capabilities_known=[
        "ineffective at large refactors",
        "inefficient planning",
        "incomplete self-model",
    ])
    tasks = [_task(f"t{i}", outcome="failure") for i in range(8)]
    findings = check_success_claim_vs_rate(model, tasks, success_rate=0.1)
    assert findings == []


def test_rule1_catches_inflected_keyword_forms() -> None:
    """Prefix matching catches ``effectively``, ``successfully``."""
    model = SelfModel(capabilities_known=[
        "I plan effectively under constraint",
    ])
    tasks = [_task(f"t{i}", outcome="failure") for i in range(10)]
    findings = check_success_claim_vs_rate(model, tasks, success_rate=0.0)
    assert len(findings) == 1


def test_rule1_plan_keyword_is_not_in_success_set() -> None:
    """Neutral phrasing like 'I plan tasks' must NOT fire the rule.

    Regression guard: ``plan`` was deliberately excluded from the
    success keyword set because it substring-matches every task-planning
    claim regardless of confidence.
    """
    model = SelfModel(capabilities_known=[
        "I plan tasks systematically",
    ])
    tasks = [_task(f"t{i}", outcome="failure") for i in range(10)]
    findings = check_success_claim_vs_rate(model, tasks, success_rate=0.1)
    assert findings == []


def test_rule1_per_rule_cap_clips_to_two() -> None:
    model = SelfModel(capabilities_known=[
        "effective planning",
        "efficient coding",
        "successful refactoring",
        "completed modules",
        "effectively curious",
    ])
    tasks = [_task(f"t{i}", outcome="failure") for i in range(10)]
    findings = check_success_claim_vs_rate(model, tasks, success_rate=0.0)
    assert len(findings) == MAX_FINDINGS_PER_RULE


# ---------------------------------------------------------------------------
# Rule 2: stability_claim_vs_failure_streak
# ---------------------------------------------------------------------------


def test_rule2_fires_on_failure_streak() -> None:
    model = SelfModel(capabilities_known=["stable execution"])
    tasks = [
        _task("t1", outcome="success"),
        _task("t2", outcome="failure"),
        _task("t3", outcome="failure"),
        _task("t4", outcome="failure"),
    ]
    findings = check_stability_claim_vs_failure_streak(model, tasks)
    assert len(findings) == 1
    assert findings[0].kind == "contradicted_claim"
    assert "stable execution" in findings[0].statement
    assert set(findings[0].cites) <= {"t2", "t3", "t4"}


def test_rule2_silent_when_streak_below_threshold() -> None:
    model = SelfModel(capabilities_known=["stable execution"])
    tasks = [
        _task("t1", outcome="failure"),
        _task("t2", outcome="failure"),
    ]
    assert check_stability_claim_vs_failure_streak(model, tasks) == []


def test_rule2_silent_when_no_stability_keyword() -> None:
    model = SelfModel(capabilities_known=["methodical reasoning"])
    tasks = [_task(f"t{i}", outcome="failure") for i in range(5)]
    assert check_stability_claim_vs_failure_streak(model, tasks) == []


def test_rule2_ignores_negation_of_keyword() -> None:
    """``unstable`` / ``unreliable`` / ``inconsistent`` must NOT fire."""
    model = SelfModel(capabilities_known=[
        "unstable context handling",
        "unreliable network calls",
        "inconsistent task framing",
    ])
    tasks = [_task(f"t{i}", outcome="failure") for i in range(5)]
    assert check_stability_claim_vs_failure_streak(model, tasks) == []


# ---------------------------------------------------------------------------
# Rule 3: repeated_error_without_limitation
# ---------------------------------------------------------------------------


def test_rule3_fires_when_error_repeats_without_matching_limitation() -> None:
    model = SelfModel(limitations_known=["slow to respond"])
    err = "TimeoutError: connection timed out after 30s"
    tasks = [
        _task(f"t{i}", outcome="failure", errors=[err])
        for i in range(3)
    ]
    findings = check_repeated_error_without_limitation(model, tasks)
    assert len(findings) == 1
    assert findings[0].kind == "undeclared_limitation"
    assert "timeouterror" in findings[0].statement.lower()
    assert set(findings[0].cites) == {"t0", "t1", "t2"}


def test_rule3_silent_below_threshold() -> None:
    model = SelfModel(limitations_known=[])
    err = "ValueError: bad input"
    tasks = [_task(f"t{i}", outcome="failure", errors=[err]) for i in range(2)]
    assert check_repeated_error_without_limitation(model, tasks) == []


def test_rule3_silenced_by_matching_limitation() -> None:
    """A limitation sharing >= 2 content tokens silences the rule."""
    # ``tokenize`` splits on ``\W+`` and is case-insensitive, so the
    # error fingerprint and limitation must share >= 2 actual tokens
    # after stopword removal.
    model = SelfModel(limitations_known=[
        "connection timed out on slow networks",
    ])
    err = "TimeoutError: connection timed out after 30s"
    tasks = [
        _task(f"t{i}", outcome="failure", errors=[err])
        for i in range(5)
    ]
    findings = check_repeated_error_without_limitation(model, tasks)
    assert findings == []


def test_rule3_stopword_only_overlap_does_not_silence() -> None:
    """A limitation sharing only stopwords with the fingerprint must NOT silence.

    Regression guard for the ``tokenize()`` stopword bug: ``"error
    when running tests"`` shares ``{"error", "when"}`` with nearly any
    error fingerprint using a naive tokenizer. The stopword filter
    strips the weak-signal tokens so the rule correctly fires.
    """
    model = SelfModel(limitations_known=[
        "when the weather is bad",  # only stopwords overlap with fingerprint
    ])
    err = "ValueError: bad input on line 42"
    tasks = [
        _task(f"t{i}", outcome="failure", errors=[err])
        for i in range(3)
    ]
    findings = check_repeated_error_without_limitation(model, tasks)
    assert len(findings) == 1


def test_rule3_fingerprint_uses_first_non_empty_line() -> None:
    """Error fingerprints normalize to the first non-blank line."""
    model = SelfModel(limitations_known=[])
    err_a = "\n  \nTimeoutError: x\nrest of traceback"
    err_b = "TimeoutError: x\ndifferent traceback"
    err_c = "   TimeoutError: X\n  and some noise"
    tasks = [
        _task("t1", outcome="failure", errors=[err_a]),
        _task("t2", outcome="failure", errors=[err_b]),
        _task("t3", outcome="failure", errors=[err_c]),
    ]
    findings = check_repeated_error_without_limitation(model, tasks)
    assert len(findings) == 1
    # All three tasks normalised to the same fingerprint
    assert set(findings[0].cites) == {"t1", "t2", "t3"}


def test_rule3_cites_deduped_and_capped() -> None:
    """Duplicate task ids in fp_to_task_ids drop, cap at MAX_CITES_PER_FINDING."""
    model = SelfModel(limitations_known=[])
    err = "KeyError: foo"
    # Same error appears in 7 distinct tasks
    tasks = [
        _task(f"t{i}", outcome="failure", errors=[err, err])
        for i in range(7)
    ]
    findings = check_repeated_error_without_limitation(model, tasks)
    assert len(findings) == 1
    assert len(findings[0].cites) == MAX_CITES_PER_FINDING
    # Cites are deduped (no duplicate task_id even though err appeared twice per task)
    assert len(set(findings[0].cites)) == len(findings[0].cites)


# ---------------------------------------------------------------------------
# Rule 4: repeated_target_attempt
# ---------------------------------------------------------------------------


def test_extract_issue_numbers() -> None:
    """extract_issue_numbers returns the distinct ``#NNN`` issue numbers."""
    assert extract_issue_numbers("Fix #1566 and check #1588") == {1566, 1588}
    assert extract_issue_numbers("No issues here") == set()
    assert extract_issue_numbers("") == set()
    assert extract_issue_numbers("Edge case #0") == {0}
    # Set semantics: duplicates within a single text collapse so a goal
    # mentioning the same issue three times still contributes one
    # element — rule 4 and the terrain-exhaustion check depend on this.
    assert extract_issue_numbers("fix #1566 see also #1566 #1566") == {1566}


def test_rule4_fires_when_issue_appears_three_times() -> None:
    """Three goals citing #1566 → one finding citing all three task ids."""
    model = SelfModel(limitations_known=[])
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Address #1566")
        for i in range(3)
    ]
    findings = check_repeated_target_attempt(model, tasks)
    assert len(findings) == 1
    assert findings[0].kind == "undeclared_limitation"
    assert findings[0].rule == "repeated_target_attempt"
    assert "#1566" in findings[0].statement
    assert set(findings[0].cites) == {"t0", "t1", "t2"}


def test_rule4_silent_below_threshold() -> None:
    """Two attempts on the same issue is below the Sisyphus floor."""
    model = SelfModel(limitations_known=[])
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Try #1566 again")
        for i in range(2)
    ]
    assert check_repeated_target_attempt(model, tasks) == []


def test_rule4_silenced_by_matching_limitation() -> None:
    """A limitation that names ``#NNN`` silences the rule for that issue.

    Self-extinguish parity with rule 3: once Claude integrates the
    Sisyphean pattern as a limitation, the rule should stop firing.
    """
    model = SelfModel(limitations_known=[
        "Cannot productively re-engage #1566 — depends on upstream fix",
    ])
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Address #1566")
        for i in range(5)
    ]
    findings = check_repeated_target_attempt(model, tasks)
    assert findings == []


def test_rule4_not_silenced_by_longer_issue_number_in_limitation() -> None:
    """A limitation naming #15666 must not silence rule 4 for #1566.

    The silence check matches the whole issue number, so a digit-prefix
    collision ("#1566" is a substring of "#15666") no longer suppresses
    the finding for the shorter issue.
    """
    model = SelfModel(limitations_known=[
        "Cannot re-engage #15666 — upstream blocker",
    ])
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Address #1566")
        for i in range(3)
    ]
    findings = check_repeated_target_attempt(model, tasks)
    assert len(findings) == 1
    assert "#1566" in findings[0].statement


def test_rule4_not_silenced_by_low_number_prefix() -> None:
    """A limitation about #10 must not silence rule 4 for #1."""
    model = SelfModel(limitations_known=["Known: #10 is out of scope"])
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Address #1")
        for i in range(3)
    ]
    findings = check_repeated_target_attempt(model, tasks)
    assert len(findings) == 1
    assert "#1" in findings[0].statement


def test_rule4_per_rule_cap_clips_to_two() -> None:
    """Three distinct over-attempted issues clip to MAX_FINDINGS_PER_RULE."""
    model = SelfModel(limitations_known=[])
    tasks: list[TaskRecord] = []
    for issue in (1566, 1567, 1568):
        for _ in range(3):
            tasks.append(_task(
                f"t{len(tasks)}", outcome="failure",
                goal=f"Try #{issue}",
            ))
    findings = check_repeated_target_attempt(model, tasks)
    assert len(findings) == MAX_FINDINGS_PER_RULE


def test_rule4_counts_any_outcome_not_only_failure() -> None:
    """Rule 4 ignores task outcome — three appearances suffice regardless.

    The Sisyphus shape is about *attempt* repetition: even if one of
    three attempts succeeded, three goal references still indicate the
    pattern is undeclared in the self-model.
    """
    model = SelfModel(limitations_known=[])
    tasks = [
        _task("t0", outcome="success", goal="Resolve #1566"),
        _task("t1", outcome="failure", goal="Resolve #1566"),
        _task("t2", outcome="failure", goal="Resolve #1566"),
    ]
    findings = check_repeated_target_attempt(model, tasks)
    assert len(findings) == 1
    assert "#1566" in findings[0].statement


def test_rule4_skips_non_string_limitation_entries() -> None:
    """Corrupt non-string limitations don't crash the silence check."""
    model = SelfModel()
    model.limitations_known = [  # type: ignore[list-item]
        42, None, {"k": "v"}, "fine on #1566",
    ]
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Address #1566")
        for i in range(3)
    ]
    findings = check_repeated_target_attempt(model, tasks)
    assert findings == []  # the string limitation silences via "#1566" substring


def test_rule4_counts_per_task_not_per_mention() -> None:
    """A single goal with three ``#1566`` mentions is one attempt, not three.

    Per-task set semantics matter because set-valued
    ``extract_issue_numbers`` makes a repeated mention in one goal
    contribute one attempt; otherwise finding statements would read
    ``"appeared in 3 recent tasks"`` while the cites list shows only
    one task id.
    """
    model = SelfModel(limitations_known=[])
    tasks = [
        _task("t0", outcome="failure",
              goal="fix #1566 see also #1566 reproducer in #1566"),
    ]
    assert check_repeated_target_attempt(model, tasks) == []

    # Two more tasks (one mention each) bring distinct-task count to
    # exactly the threshold so the rule fires — and the count in the
    # statement matches the number of cites.
    tasks.extend([
        _task("t1", outcome="failure", goal="address #1566"),
        _task("t2", outcome="failure", goal="address #1566"),
    ])
    findings = check_repeated_target_attempt(model, tasks)
    assert len(findings) == 1
    assert "in 3 recent tasks" in findings[0].statement
    assert set(findings[0].cites) == {"t0", "t1", "t2"}


def test_rule4_settled_issue_suppressed() -> None:
    """A settled ``#NNN`` at/above threshold yields no finding (criterion 1).

    Laps repeatedly naming a settled PR are the remediation that landed
    it, not an unlearned limitation, so the settlement pre-filter skips
    it before the threshold test even when the task count is ≥ floor.
    """
    model = SelfModel(limitations_known=[])
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Remediate PR #3971")
        for i in range(4)
    ]
    findings = check_repeated_target_attempt(
        model, tasks, settled_prs=frozenset({3971}),
    )
    assert findings == []


def test_rule4_unsettled_issue_still_fires_with_nonempty_set() -> None:
    """An unsettled ``#NNN`` still fires when the set omits it (criterion 3)."""
    model = SelfModel(limitations_known=[])
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Remediate PR #3972")
        for i in range(3)
    ]
    findings = check_repeated_target_attempt(
        model, tasks, settled_prs=frozenset({3971}),
    )
    assert len(findings) == 1
    assert "#3972" in findings[0].statement


def test_rule4_empty_settled_prs_byte_identical() -> None:
    """Empty ``settled_prs`` (the default) matches no-arg output (criterion 2)."""
    model = SelfModel(limitations_known=[])
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Address #1566")
        for i in range(3)
    ]
    default = check_repeated_target_attempt(model, tasks)
    explicit_empty = check_repeated_target_attempt(
        model, tasks, settled_prs=frozenset(),
    )
    assert default == explicit_empty
    assert len(default) == 1


def test_run_all_checks_empty_settled_prs_byte_identical() -> None:
    """Empty ``settled_prs`` leaves ``run_all_checks`` output unchanged (crit 2)."""
    model = SelfModel(limitations_known=[])
    tasks = [
        _task(f"t{i}", outcome="failure", goal="Address #1566")
        for i in range(3)
    ]
    without = run_all_checks(model, tasks, 0.5)
    with_empty = run_all_checks(model, tasks, 0.5, settled_prs=frozenset())
    assert without == with_empty


# ---------------------------------------------------------------------------
# Global combiner: per-rule cap + global cap + round-robin
# ---------------------------------------------------------------------------


def test_run_all_checks_applies_global_cap() -> None:
    """All four rules at their cap clip to MAX_FINDINGS_PER_CYCLE globally."""
    err = "UniqueError: xyz"
    model = SelfModel(
        capabilities_known=[
            "effective one",
            "efficient two",
            "stable three",
            "reliable four",
        ],
        limitations_known=[],
    )
    tasks = [
        _task(f"t{i}", outcome="failure", errors=[err])
        for i in range(10)
    ]
    findings = run_all_checks(model, tasks, success_rate=0.0)
    assert len(findings) == MAX_FINDINGS_PER_CYCLE


def test_run_all_checks_round_robin_preserves_rule_variety() -> None:
    """Clip to MAX_FINDINGS_PER_CYCLE must keep at least one finding per rule.

    All four rules must surface when the corpus exercises every shape:
    keyword-matching capabilities (rules 1, 2), a recurring error
    fingerprint (rule 3), and a recurring issue number (rule 4).
    """
    err = "RoundRobinError: xyz"
    model = SelfModel(
        capabilities_known=[
            "effective one",   # rule 1
            "efficient two",   # rule 1
            "stable three",    # rule 2
            "reliable four",   # rule 2
        ],
        limitations_known=[],
    )
    tasks = [
        _task(f"t{i}", outcome="failure", errors=[err],
              goal="Address #1566")
        for i in range(10)
    ]
    findings = run_all_checks(model, tasks, success_rate=0.0)
    rules = {f.rule for f in findings}
    # All four rule names should survive the round-robin + clip
    assert len(rules) == 4


def test_run_all_checks_returns_empty_when_no_conditions_hit() -> None:
    model = SelfModel(capabilities_known=["careful", "methodical"])
    tasks = [
        _task(f"t{i}", outcome="success", goal="measure maestro/consciousness.py coverage")
        for i in range(10)
    ]
    findings = run_all_checks(model, tasks, success_rate=1.0)
    assert findings == []


def test_rule1_skips_non_string_capability_entries() -> None:
    """Rule 1 tolerates non-string capabilities (corrupt state)."""
    # SelfModel's capabilities_known is annotated list[str] but corrupted
    # JSON or manual edits could leave non-strings; the rule must not crash.
    model = SelfModel()
    model.capabilities_known = [  # type: ignore[list-item]
        42, None, {"k": "v"}, "effective planning",
    ]
    tasks = [_task(f"t{i}", outcome="failure") for i in range(8)]
    findings = check_success_claim_vs_rate(model, tasks, success_rate=0.0)
    assert len(findings) == 1
    assert "effective planning" in findings[0].statement


def test_rule2_skips_non_string_capability_entries() -> None:
    """Rule 2 tolerates non-string capabilities."""
    model = SelfModel()
    model.capabilities_known = [  # type: ignore[list-item]
        123, None, "stable execution",
    ]
    tasks = [_task(f"t{i}", outcome="failure") for i in range(4)]
    findings = check_stability_claim_vs_failure_streak(model, tasks)
    assert len(findings) == 1


def test_rule3_fingerprint_skips_non_string_error() -> None:
    """Error entries that aren't strings are silently skipped."""
    from maestro.audit_checks import _fingerprint_error

    assert _fingerprint_error(123) == ""  # type: ignore[arg-type]
    assert _fingerprint_error(None) == ""  # type: ignore[arg-type]
    # All-whitespace input yields empty fingerprint
    assert _fingerprint_error("\n   \n\t\n") == ""


def test_rule3_skips_blank_fingerprints() -> None:
    """Blank error strings are filtered out before the counter increments."""
    model = SelfModel(limitations_known=[])
    tasks = [
        _task(f"t{i}", outcome="failure", errors=["   ", "\n\n"])
        for i in range(5)
    ]
    findings = check_repeated_error_without_limitation(model, tasks)
    assert findings == []


def test_rule3_stops_at_per_rule_cap() -> None:
    """When >2 distinct error fingerprints each hit ≥3, rule 3 stops at the cap."""
    model = SelfModel(limitations_known=[])
    tasks: list[TaskRecord] = []
    # Three distinct fingerprints, each appearing 3 times.
    for fp in ["ErrorA: one", "ErrorB: two", "ErrorC: three"]:
        for _ in range(3):
            tasks.append(_task(f"t{len(tasks)}", outcome="failure", errors=[fp]))
    findings = check_repeated_error_without_limitation(model, tasks)
    assert len(findings) == MAX_FINDINGS_PER_RULE


def test_audit_finding_is_frozen() -> None:
    """AuditFinding is a frozen dataclass — mutation raises."""
    import dataclasses

    f = AuditFinding(
        kind="contradicted_claim",
        statement="x",
        cites=["t1"],
        rule="test",
    )
    try:
        f.kind = "undeclared_limitation"  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("AuditFinding should be frozen")


def test_findings_cap_fits_open_questions_subcap() -> None:
    """Cross-module invariant: MAX_FINDINGS_PER_CYCLE <= MAX_AUDIT_OPEN_QUESTIONS.

    If the findings cap exceeds the open_questions sub-cap, audit findings
    are written to the journal and audit_history but silently dropped from
    open_questions on save — orphaned findings that fire every cycle without
    ever reaching the INTEGRATE/REFUTE/KEEP prompt. With four rules now
    contributing candidates (three legacy at cap 2 + ``stalled_question``
    at cap 1 = 7 candidates), the global clip remains at 3 — same cap as
    the open_questions sub-cap, so survivors are never dropped on save.
    """
    from maestro.consciousness import Consciousness

    subcap: int = Consciousness.MAX_AUDIT_OPEN_QUESTIONS
    assert subcap >= MAX_FINDINGS_PER_CYCLE, (
        f"MAX_FINDINGS_PER_CYCLE ({MAX_FINDINGS_PER_CYCLE}) must not exceed "
        f"MAX_AUDIT_OPEN_QUESTIONS ({subcap}) — "
        f"excess findings are orphaned from contemplation"
    )


# ---------------------------------------------------------------------------
# Rule 5: stalled_question
# ---------------------------------------------------------------------------


def _resolution(
    *,
    question: str = "What am I capable of?",
    disposition: str = "kept",
    task_id: str = "tx",
    iteration: int = 1,
    resolution_id: str | None = None,
    refined_to: str | None = None,
    outcome: str = "success",
) -> QuestionResolution:
    return QuestionResolution(
        resolution_id=resolution_id or f"r-{iteration}-{task_id}",
        iteration=iteration,
        timestamp="2026-04-26T00:00:00+00:00",
        question=question,
        task_id=task_id,
        outcome=outcome,  # type: ignore[arg-type]
        disposition=disposition,  # type: ignore[arg-type]
        finding="some finding",
        refined_to=refined_to,
    )


def test_rule5_fires_at_threshold() -> None:
    """≥3 resolutions whose latest disposition is non-resolving fires."""
    model = SelfModel(open_questions=["What am I capable of?"])
    resolutions = [
        _resolution(disposition="kept", task_id=f"t{i}", iteration=i)
        for i in range(3)
    ]
    findings = check_stalled_question(model, resolutions)
    assert len(findings) == 1
    assert findings[0].kind == "stalled_question"
    assert findings[0].rule == "stalled_question"
    assert "What am I capable of?" in findings[0].statement
    assert "3" in findings[0].statement
    assert findings[0].cites


def test_rule5_silent_below_threshold() -> None:
    model = SelfModel(open_questions=["What am I capable of?"])
    resolutions = [
        _resolution(task_id=f"t{i}", iteration=i) for i in range(2)
    ]
    assert check_stalled_question(model, resolutions) == []


def test_rule5_silent_when_latest_disposition_is_removed() -> None:
    """Three attempts ending with a ``removed`` disposition is resolved."""
    model = SelfModel(open_questions=["What am I capable of?"])
    resolutions = [
        _resolution(disposition="kept", task_id="t0", iteration=0),
        _resolution(disposition="kept", task_id="t1", iteration=1),
        _resolution(disposition="removed", task_id="t2", iteration=2),
    ]
    assert check_stalled_question(model, resolutions) == []


def test_rule5_silent_when_latest_disposition_is_refined() -> None:
    model = SelfModel(open_questions=["What am I capable of?"])
    resolutions = [
        _resolution(disposition="kept", task_id="t0", iteration=0),
        _resolution(disposition="kept", task_id="t1", iteration=1),
        _resolution(disposition="refined", task_id="t2", iteration=2),
    ]
    assert check_stalled_question(model, resolutions) == []


def test_rule5_silent_when_latest_disposition_is_unresolved() -> None:
    """An orphan-``unresolved`` latest row is not evidence of a stall.

    ``_classify_disposition`` returns ``unresolved`` only when the pair's
    question was in neither the pre- nor post-update ``open_questions``
    snapshot — the question was evicted before any LLM verdict landed, so
    no affirmative decision was rendered against the live list. Three such
    bookkeeping orphans must not trip the rule the way three genuine
    ``kept`` rows do (issue #1612).
    """
    model = SelfModel(open_questions=["What am I capable of?"])
    resolutions = [
        _resolution(disposition="unresolved", task_id="t0", iteration=0),
        _resolution(disposition="unresolved", task_id="t1", iteration=1),
        _resolution(disposition="unresolved", task_id="t2", iteration=2),
    ]
    assert check_stalled_question(model, resolutions) == []


def test_rule5_silent_when_unresolved_follows_kept() -> None:
    """An orphan latest row defers the finding rather than firing it.

    ``[kept, kept, unresolved]`` no longer fires: the latest disposition is
    an orphan, so the rule waits for a genuine ``kept`` verdict to land (or
    self-extinguishes when the question leaves ``open_questions``) instead
    of tripping on the trailing bookkeeping row.
    """
    model = SelfModel(open_questions=["What am I capable of?"])
    resolutions = [
        _resolution(disposition="kept", task_id="t0", iteration=0),
        _resolution(disposition="kept", task_id="t1", iteration=1),
        _resolution(disposition="unresolved", task_id="t2", iteration=2),
    ]
    assert check_stalled_question(model, resolutions) == []


def test_rule5_silent_when_question_no_longer_open() -> None:
    """Self-extinguishing — the question must still be in open_questions."""
    model = SelfModel(open_questions=["A different one?"])
    resolutions = [
        _resolution(task_id=f"t{i}", iteration=i) for i in range(3)
    ]
    assert check_stalled_question(model, resolutions) == []


def test_rule5_normalises_question_text() -> None:
    """Whitespace / case differences across rows still aggregate to one bucket."""
    model = SelfModel(open_questions=["What am I capable of?"])
    resolutions = [
        _resolution(question="  WHAT  am I  capable OF? ",
                    task_id="t0", iteration=0),
        _resolution(question="What am I capable of?",
                    task_id="t1", iteration=1),
        _resolution(question="What  am  i  CAPABLE  of?",
                    task_id="t2", iteration=2),
    ]
    findings = check_stalled_question(model, resolutions)
    assert len(findings) == 1
    # The live verbatim question should be quoted, not the mangled rows.
    assert "What am I capable of?" in findings[0].statement


def test_rule5_caps_cites_and_dedupes_newest_first() -> None:
    """Cites are capped at MAX_CITES_PER_FINDING and deduped newest-first."""
    model = SelfModel(open_questions=["What am I capable of?"])
    resolutions: list[QuestionResolution] = []
    # 9 attempts with 7 distinct task_ids — first cite should be the newest
    # task_id and the cap break should fire before all are appended.
    for i in range(9):
        resolutions.append(_resolution(
            task_id=f"t{i % 7}", iteration=i,
            resolution_id=f"r{i}",
        ))
    findings = check_stalled_question(model, resolutions)
    assert len(findings) == 1
    assert len(findings[0].cites) == MAX_CITES_PER_FINDING
    assert len(findings[0].cites) == len(set(findings[0].cites))
    # Newest-first: the most recent task_id (t1, from iteration 8) leads.
    assert findings[0].cites[0] == "t1"


def test_rule5_per_rule_cap_is_one() -> None:
    """Even with two stalled questions, only one finding is returned."""
    from maestro.audit_checks import _STALLED_PER_RULE_CAP

    model = SelfModel(open_questions=[
        "Question A?", "Question B?",
    ])
    resolutions: list[QuestionResolution] = []
    for q in ("Question A?", "Question B?"):
        for i in range(3):
            resolutions.append(_resolution(
                question=q, task_id=f"{q[-2]}{i}", iteration=i,
                resolution_id=f"r-{q}-{i}",
            ))
    findings = check_stalled_question(model, resolutions)
    assert len(findings) == _STALLED_PER_RULE_CAP == 1


def test_rule5_per_rule_cap_picks_highest_count() -> None:
    """The single-slot cap goes to the most-stalled lineage, not the
    first-in-list one. Question A is first in ``open_questions`` but has
    only 3 stall rows; Question B has 5. Without the collect-and-sort
    fix, A wins because the loop breaks on the first qualifier.
    """
    model = SelfModel(open_questions=[
        "Question A?", "Question B?",
    ])
    resolutions: list[QuestionResolution] = []
    for i in range(3):
        resolutions.append(_resolution(
            question="Question A?", task_id=f"a{i}", iteration=i,
            resolution_id=f"r-A-{i}",
        ))
    for i in range(5):
        resolutions.append(_resolution(
            question="Question B?", task_id=f"b{i}", iteration=10 + i,
            resolution_id=f"r-B-{i}",
        ))
    findings = check_stalled_question(model, resolutions)
    assert len(findings) == 1
    assert "Question B?" in findings[0].statement
    assert "Question A?" not in findings[0].statement


def test_rule5_per_rule_cap_breaks_ties_alphabetically() -> None:
    """When two lineages tie on count, the alphabetically-earlier
    *normalised* wording wins the single slot. Locks the secondary sort
    key so a future refactor cannot silently drop it.
    """
    model = SelfModel(open_questions=[
        "Question B?", "Question A?",
    ])
    resolutions: list[QuestionResolution] = []
    for i in range(3):
        resolutions.append(_resolution(
            question="Question B?", task_id=f"b{i}", iteration=i,
            resolution_id=f"r-B-{i}",
        ))
    for i in range(3):
        resolutions.append(_resolution(
            question="Question A?", task_id=f"a{i}", iteration=10 + i,
            resolution_id=f"r-A-{i}",
        ))
    findings = check_stalled_question(model, resolutions)
    assert len(findings) == 1
    assert "Question A?" in findings[0].statement
    assert "Question B?" not in findings[0].statement


def test_rule5_silent_on_empty_list() -> None:
    model = SelfModel(open_questions=["A?"])
    assert check_stalled_question(model, []) == []


def test_rule5_skips_non_string_open_questions() -> None:
    """Corrupted SelfModel (non-string entries) doesn't crash the rule."""
    model = SelfModel()
    model.open_questions = [  # type: ignore[list-item]
        42, None, "What am I capable of?",
    ]
    resolutions = [
        _resolution(task_id=f"t{i}", iteration=i) for i in range(3)
    ]
    findings = check_stalled_question(model, resolutions)
    assert len(findings) == 1


def test_rule5_skips_blank_normalised_question() -> None:
    """Resolution rows with empty/whitespace question are filtered out."""
    model = SelfModel(open_questions=["A?"])
    resolutions = [
        _resolution(question="   ", task_id=f"t{i}", iteration=i)
        for i in range(3)
    ]
    assert check_stalled_question(model, resolutions) == []


def test_lineage_keys_no_resolutions_returns_self_only() -> None:
    """Empty resolution log → lineage is just the question's own key."""
    assert lineage_keys("What am I capable of?", []) == {
        "what am i capable of?",
    }


def test_lineage_keys_walks_chain_two_hops() -> None:
    """Q1 → Q1' → Q1'' — walking from Q1'' returns three normalised keys."""
    resolutions = [
        _resolution(
            question="What am I capable of?",
            disposition="refined",
            refined_to="What am I capable of really?",
            task_id="t0", iteration=0,
        ),
        _resolution(
            question="What am I capable of really?",
            disposition="refined",
            refined_to="What truly am I capable of?",
            task_id="t1", iteration=1,
        ),
    ]
    keys = lineage_keys("What truly am I capable of?", resolutions)
    assert keys == {
        "what am i capable of?",
        "what am i capable of really?",
        "what truly am i capable of?",
    }


def test_lineage_keys_terminates_on_cycle() -> None:
    """Self-loop in the predecessor map terminates at the cycle boundary."""
    # Pathological row: refined_to points back at the same question.
    resolutions = [
        _resolution(
            question="Q?",
            disposition="refined",
            refined_to="Q?",
            task_id="t0", iteration=0,
        ),
    ]
    keys = lineage_keys("Q?", resolutions)
    assert keys == {"q?"}


def test_lineage_keys_caps_at_max_hops() -> None:
    """Max-hops cap stops the walk in adversarial input."""
    # Long unique chain — set max_hops=2 to force early termination.
    chain = [f"Q{i}?" for i in range(10)]
    resolutions = [
        _resolution(
            question=chain[i],
            disposition="refined",
            refined_to=chain[i + 1],
            task_id=f"t{i}", iteration=i,
        )
        for i in range(9)
    ]
    keys = lineage_keys(chain[-1], resolutions, max_hops=2)
    # Starting key + 2 hops back = 3 keys.
    assert len(keys) == 3


def test_lineage_keys_skips_non_refined_rows() -> None:
    """Rows whose ``refined_to`` is None are not edges; lineage stays minimal."""
    resolutions = [
        _resolution(
            question="What am I capable of?",
            disposition="kept",  # No refined_to.
            task_id="t0", iteration=0,
        ),
        _resolution(
            question="What am I capable of?",
            disposition="kept",
            task_id="t1", iteration=1,
        ),
    ]
    assert lineage_keys("What am I capable of?", resolutions) == {
        "what am i capable of?",
    }


def test_lineage_keys_blank_current_question_returns_empty_set() -> None:
    """Whitespace-only seed normalises to nothing → no keys at all.

    Prevents the walk from anchoring on an empty key that would match
    every blank ``refined_to`` predecessor in the predecessor map.
    """
    assert lineage_keys("   ", []) == set()
    assert lineage_keys("", []) == set()


def test_lineage_keys_skips_blank_refined_to_edges() -> None:
    """Edges whose ``refined_to`` normalises to empty are not registered.

    A row with a whitespace-only ``refined_to`` cannot be a real
    refinement edge — silently dropping it keeps the predecessor map
    stable.
    """
    resolutions = [
        _resolution(
            question="What am I capable of?",
            disposition="refined",
            refined_to="   ",  # Empty after normalisation.
            task_id="t0", iteration=0,
        ),
    ]
    # The current-question edge is missing, so the walk degrades to
    # ``{normalize(current)}`` only.
    keys = lineage_keys("What am I capable of?", resolutions)
    assert keys == {"what am i capable of?"}


def test_lineage_keys_walks_through_long_question_after_symmetric_truncation(
    mem_fs: dict[str, str],
) -> None:
    """Long-text lineage survives because ``question`` and ``refined_to``
    share the same cap on append.

    A predecessor whose ``refined_to`` is over the cap, paired with a
    successor whose ``question`` is the same string, persists with both
    fields truncated to identical 500-char heads. ``lineage_keys`` then
    normalises both ends to the same key and the walk takes the
    predecessor edge. Before the symmetric cap, ``refined_to`` was
    truncated but ``question`` was written verbatim, so the predecessor
    map keyed on ``LONG[:500]`` while the walk looked up ``LONG`` —
    lookup missed and the lineage stalled at one hop.
    """
    from maestro.memory import _QUESTION_RESOLUTION_FINDING_MAX_CHARS

    long_text = "Y" * (_QUESTION_RESOLUTION_FINDING_MAX_CHARS + 100)
    mem = Memory(FAKE_DIR)
    mem.append_question_resolution(
        QuestionResolution(
            resolution_id="r0",
            iteration=0,
            timestamp="2026-04-26T00:00:00+00:00",
            question="P_short?",
            task_id="t0",
            outcome="success",
            disposition="refined",
            finding="some finding",
            refined_to=long_text,
        )
    )
    mem.append_question_resolution(
        QuestionResolution(
            resolution_id="r1",
            iteration=1,
            timestamp="2026-04-26T00:00:01+00:00",
            question=long_text,
            task_id="t1",
            outcome="success",
            disposition="kept",
            finding="some finding",
            refined_to=None,
        )
    )

    rows = mem.get_recent_question_resolutions(2)
    keys = lineage_keys(rows[1].question, [rows[0]])
    assert keys == {
        _normalize_question(rows[0].question),
        _normalize_question(rows[1].question),
    }


def test_rule5_aggregates_across_refinement_lineage() -> None:
    """Lineage-aware count: Q1 → Q1' chain crosses threshold even though
    Q1' alone has only two rows.

    Without the lineage walk, the predecessor's count is stranded under
    its own normalised key — a single REFINE wipes the stall counter.
    With it, ``check_stalled_question`` sees three attempts on the same
    logical question (the wording drifted; the question did not).
    """
    model = SelfModel(open_questions=["What am I capable of really?"])
    resolutions = [
        _resolution(
            question="What am I capable of?",
            disposition="refined",
            refined_to="What am I capable of really?",
            task_id="t0", iteration=0,
        ),
        _resolution(
            question="What am I capable of really?",
            disposition="kept",
            task_id="t1", iteration=1,
        ),
        _resolution(
            question="What am I capable of really?",
            disposition="kept",
            task_id="t2", iteration=2,
        ),
    ]
    findings = check_stalled_question(model, resolutions)
    assert len(findings) == 1
    assert "What am I capable of really?" in findings[0].statement
    assert "3" in findings[0].statement
    # All three task_ids appear as cites, newest-first deduped.
    assert set(findings[0].cites) == {"t0", "t1", "t2"}
    assert findings[0].cites[0] == "t2"


def test_rule5_lineage_silent_when_latest_disposition_is_removed() -> None:
    """Lineage closure: the most recent row across the chain decides
    "kept" — a final REMOVE silences the rule even if the earlier
    wordings stalled.
    """
    model = SelfModel(open_questions=["What am I capable of really?"])
    resolutions = [
        _resolution(
            question="What am I capable of?",
            disposition="kept",
            task_id="t0", iteration=0,
        ),
        _resolution(
            question="What am I capable of?",
            disposition="refined",
            refined_to="What am I capable of really?",
            task_id="t1", iteration=1,
        ),
        _resolution(
            question="What am I capable of really?",
            disposition="removed",
            task_id="t2", iteration=2,
        ),
    ]
    assert check_stalled_question(model, resolutions) == []


def test_rule5_lineage_walks_two_hops() -> None:
    """Q1 → Q1' → Q1'' — three rows across three wordings still aggregate."""
    model = SelfModel(open_questions=["What truly am I capable of?"])
    resolutions = [
        _resolution(
            question="What am I capable of?",
            disposition="refined",
            refined_to="What am I capable of really?",
            task_id="t0", iteration=0,
        ),
        _resolution(
            question="What am I capable of really?",
            disposition="refined",
            refined_to="What truly am I capable of?",
            task_id="t1", iteration=1,
        ),
        _resolution(
            question="What truly am I capable of?",
            disposition="kept",
            task_id="t2", iteration=2,
        ),
    ]
    findings = check_stalled_question(model, resolutions)
    assert len(findings) == 1
    assert "What truly am I capable of?" in findings[0].statement
    assert "3" in findings[0].statement


def test_run_all_checks_accepts_resolutions_kwarg() -> None:
    """run_all_checks threads resolutions to ``check_stalled_question``."""
    model = SelfModel(open_questions=["What am I capable of?"])
    resolutions = [
        _resolution(task_id=f"t{i}", iteration=i) for i in range(3)
    ]
    findings = run_all_checks(
        model, tasks=[], success_rate=0.0, resolutions=resolutions,
    )
    assert any(f.rule == "stalled_question" for f in findings)


def test_run_all_checks_resolutions_default_is_silent() -> None:
    """Omitting ``resolutions`` leaves rule 4 quiet."""
    model = SelfModel(open_questions=["What am I capable of?"])
    findings = run_all_checks(
        model, tasks=[], success_rate=0.0,
    )
    assert all(f.rule != "stalled_question" for f in findings)


def test_run_all_checks_round_robin_preserves_legacy_priority() -> None:
    """5 rules at full cap → MAX_FINDINGS_PER_CYCLE, one per legacy rule, none for stalled."""
    err = "RoundRobinError: xyz"
    model = SelfModel(
        capabilities_known=[
            "effective one", "efficient two",
            "stable three", "reliable four",
        ],
        limitations_known=[],
        open_questions=["Stalled question?"],
    )
    tasks = [
        _task(f"t{i}", outcome="failure", errors=[err], goal="address #1566")
        for i in range(10)
    ]
    resolutions = [
        _resolution(question="Stalled question?", task_id=f"r{i}", iteration=i)
        for i in range(3)
    ]
    findings = run_all_checks(
        model, tasks, success_rate=0.0, resolutions=resolutions,
    )
    assert len(findings) == MAX_FINDINGS_PER_CYCLE
    rules = {f.rule for f in findings}
    # All four legacy rule names should survive; stalled_question yields.
    assert "success_claim_vs_rate" in rules
    assert "stability_claim_vs_failure_streak" in rules
    assert "repeated_error_without_limitation" in rules
    assert "repeated_target_attempt" in rules
    assert "stalled_question" not in rules


def test_run_all_checks_round_robin_starves_last_legacy_rule() -> None:
    """All five non-deferred rules firing → the alphabetically last yields.

    ``kept_task_not_promoted`` is deliberately *not* in ``_DEFERRED_RULES``,
    so it competes alphabetically with the four legacy rules. Five
    non-deferred candidates contending for ``MAX_FINDINGS_PER_CYCLE`` (4)
    means the rotation's first round overflows by one, and the clip drops
    ``success_claim_vs_rate`` — alphabetically last among the five. This
    pins the limit documented in ``docs/self-evolution.md``: the deferred
    rules yielding no longer implies every legacy rule retains a slot.
    """
    from maestro.audit_checks import _KEPT_TASK_NOT_PROMOTED_THRESHOLD

    err = "RoundRobinError: xyz"
    model = SelfModel(
        capabilities_known=[
            "effective one", "efficient two",
            "stable three", "reliable four",
        ],
        limitations_known=[],
        open_questions=["Stalled question?"],
    )
    # Kept successful branches first so the trailing failure streak that
    # rule 2 needs stays intact behind them.
    kept = [
        _task(f"k{i}", outcome="success", goal="address #1566",
              branch=f"maestro/task/k{i}")
        for i in range(_KEPT_TASK_NOT_PROMOTED_THRESHOLD)
    ]
    fails = [
        _task(f"t{i}", outcome="failure", errors=[err], goal="address #1566")
        for i in range(10)
    ]
    tasks = kept + fails

    # Precondition: every non-deferred rule has at least one candidate.
    assert len(check_success_claim_vs_rate(model, tasks, 0.0)) > 0
    assert len(check_stability_claim_vs_failure_streak(model, tasks)) > 0
    assert len(check_repeated_error_without_limitation(model, tasks)) > 0
    assert len(check_repeated_target_attempt(model, tasks)) > 0
    assert len(check_kept_task_not_promoted(model, tasks, set())) > 0

    findings = run_all_checks(
        model, tasks, success_rate=0.0, resolutions=[],
        cited_source_task_ids=set(),
    )
    assert len(findings) == MAX_FINDINGS_PER_CYCLE
    rules = {f.rule for f in findings}
    assert "kept_task_not_promoted" in rules
    assert "stability_claim_vs_failure_streak" in rules
    assert "repeated_error_without_limitation" in rules
    assert "repeated_target_attempt" in rules
    # Starved: alphabetically last among the five non-deferred rules.
    assert "success_claim_vs_rate" not in rules


# ---------------------------------------------------------------------------
# compute_audit_stall_counts — actuator-gap stall escalation
# ---------------------------------------------------------------------------


def _audit_finding(
    kind: str = "contradicted_claim",
    statement: str = "Some finding statement",
    rule: str = "rule_x",
) -> AuditFinding:
    return AuditFinding(
        kind=kind,  # type: ignore[arg-type]
        statement=statement,
        cites=[],
        rule=rule,
    )


def _audit_result(
    iteration: int,
    findings: list[AuditFinding] | None = None,
) -> AuditResult:
    return AuditResult(
        iteration=iteration,
        timestamp=f"2026-04-{iteration:02d}T00:00:00+00:00",
        findings=findings or [],
        rules_fired=sorted({f.rule for f in (findings or [])}),
    )


def _open_question(finding: AuditFinding) -> str:
    return f"[audit] {finding.kind}: {finding.statement[:160]}"


def test_compute_audit_stall_counts_empty_history() -> None:
    """No audit cycles → empty mapping."""
    f = _audit_finding(statement="anything")
    counts = compute_audit_stall_counts([], [_open_question(f)])
    assert counts == {}


def test_compute_audit_stall_counts_single_cycle_present() -> None:
    """One cycle with the matching finding → count of 1."""
    f = _audit_finding(statement="effective claim vs rate 30% over 10")
    history = [_audit_result(1, [f])]
    counts = compute_audit_stall_counts(history, [_open_question(f)])
    assert counts == {_open_question(f): 1}


def test_compute_audit_stall_counts_consecutive_5_cycles() -> None:
    """Question matched in five consecutive newest cycles → count of 5."""
    f = _audit_finding(statement="stalled finding text")
    history = [_audit_result(i, [f]) for i in range(1, 6)]
    counts = compute_audit_stall_counts(history, [_open_question(f)])
    assert counts == {_open_question(f): 5}


def test_compute_audit_stall_counts_breaks_on_gap() -> None:
    """A cycle missing the finding stops the consecutive count.

    History (newest-last) is: [match, match, MISS, match, match].
    Walking newest→oldest, the first two newest cycles match (count=2),
    then a cycle without the finding stops the walk for that question;
    the two earliest matching cycles are not counted.
    """
    f = _audit_finding(statement="gap finding")
    other = _audit_finding(statement="unrelated", rule="other")
    history = [
        _audit_result(1, [f]),
        _audit_result(2, [f]),
        _audit_result(3, [other]),  # gap mid-tail
        _audit_result(4, [f]),
        _audit_result(5, [f]),
    ]
    counts = compute_audit_stall_counts(history, [_open_question(f)])
    assert counts == {_open_question(f): 2}


def test_compute_audit_stall_counts_ignores_non_audit_prefix() -> None:
    """Non-``[audit]`` open questions are excluded from the result."""
    f = _audit_finding(statement="for the audit one")
    history = [_audit_result(1, [f])]
    open_questions = [
        "What am I?",
        _open_question(f),
        "Plain user-asked question",
    ]
    counts = compute_audit_stall_counts(history, open_questions)
    assert set(counts.keys()) == {_open_question(f)}
    assert counts[_open_question(f)] == 1


def test_compute_audit_stall_counts_normalisation_matches_drift() -> None:
    """Whitespace differences still match via _normalize_question.

    The ``[audit`` prefix gate is case-sensitive (matching how
    ``_audit_self_model`` writes the entry), so drift here exercises
    only the whitespace/case path inside the suffix that
    ``_normalize_question`` collapses.
    """
    f = _audit_finding(statement="Normalised Match Statement")
    history = [_audit_result(1, [f])]
    canonical = _open_question(f)
    # Preserve the lowercase ``[audit]`` prefix; drift only the suffix.
    head, _, tail = canonical.partition("] ")
    drifted = f"{head}]   " + tail.upper().replace(" ", "  ")
    counts = compute_audit_stall_counts(history, [drifted])
    assert counts == {drifted: 1}


def test_compute_audit_stall_counts_no_audit_questions_returns_empty() -> None:
    """Open-questions list with zero ``[audit]`` entries → empty mapping."""
    f = _audit_finding(statement="never matched")
    history = [_audit_result(1, [f])]
    counts = compute_audit_stall_counts(
        history, ["What am I?", "Why?"],
    )
    assert counts == {}


def test_compute_audit_stall_counts_skips_non_string_open_questions() -> None:
    """Non-string entries (corrupt state) are silently skipped."""
    f = _audit_finding(statement="strict typing")
    history = [_audit_result(1, [f])]
    junk: list[str] = [_open_question(f), 42, None]  # type: ignore[list-item]
    counts = compute_audit_stall_counts(history, junk)
    assert counts == {_open_question(f): 1}


def test_compute_audit_stall_counts_lineage_walks_predecessor() -> None:
    """A REFINEd ``[audit]`` entry inherits its predecessor's stall count.

    Two cycles emit the predecessor wording; the LLM REFINEs it to a new
    wording on the next cycle, which is then re-emitted by the audit. The
    legacy single-key match would zero the counter at the rename — the
    lineage-aware match accumulates across both wordings.
    """
    pred_f = _audit_finding(
        kind="contradicted_claim", statement="effective rate vs 30%",
    )
    succ_f = _audit_finding(
        kind="contradicted_claim", statement="effective rate vs 30 percent",
    )
    history = [
        _audit_result(1, [pred_f]),
        _audit_result(2, [pred_f]),
        _audit_result(3, [succ_f]),
    ]
    refinement = QuestionResolution(
        resolution_id="r1",
        iteration=1,
        timestamp="2026-04-26T00:00:00+00:00",
        question=_open_question(pred_f),
        task_id="t1",
        outcome="success",
        disposition="refined",
        finding="some",
        refined_to=_open_question(succ_f),
    )
    counts = compute_audit_stall_counts(
        history, [_open_question(succ_f)], resolutions=[refinement],
    )
    assert counts == {_open_question(succ_f): 3}


def test_compute_audit_stall_counts_lineage_default_no_resolutions() -> None:
    """Omitting *resolutions* degrades to single-key via lineage_keys."""
    f = _audit_finding(statement="legacy")
    history = [_audit_result(1, [f]), _audit_result(2, [f])]
    counts = compute_audit_stall_counts(history, [_open_question(f)])
    assert counts == {_open_question(f): 2}


def test_compute_audit_stall_counts_none_and_empty_resolutions_equivalent() -> None:
    """resolutions=None and resolutions=[] produce identical results.

    The old code had a conditional branch::

        lineage_keys(q, resolutions) if resolutions else {_normalize_question(q)}

    After simplification to ``lineage_keys(q, resolutions or [])``, both
    ``None`` and ``[]`` must yield the same target-key set and therefore
    the same stall counts.  This test also verifies that
    ``lineage_keys(q, [])`` returns the same singleton set that the old
    ``{_normalize_question(q)}`` branch would have produced.
    """
    from maestro.consciousness import _normalize_question

    f = _audit_finding(statement="equivalence check finding")
    q = _open_question(f)
    history = [_audit_result(1, [f]), _audit_result(2, [f]), _audit_result(3, [f])]

    counts_none = compute_audit_stall_counts(history, [q], resolutions=None)
    counts_empty = compute_audit_stall_counts(history, [q], resolutions=[])

    # Both paths must agree on stall counts.
    assert counts_none == counts_empty == {q: 3}

    # lineage_keys with empty resolutions must produce the exact same
    # singleton set that the old conditional branch computed directly.
    old_branch_keys = {_normalize_question(q)}
    new_branch_keys = lineage_keys(q, [])
    assert new_branch_keys == old_branch_keys


# ---------------------------------------------------------------------------
# Rule 6: stale_self_model_entry
# ---------------------------------------------------------------------------


# Filler entries built so each shares ≥ 2 stopword-stripped tokens with
# :data:`_LIVE_CORPUS_ITEM` — they stay *live* against rule 6 whenever the
# corpus contains that item, leaving the *target* entry at index 0 as the
# only stale candidate. Per-rule cap = 1 so the finding's statement is the
# target's text, which makes assertions strict and unambiguous.
_LIVE_CORPUS_ITEM = "keepalive filler shared sentinel content"
_LIVE_FILLER_A = "keepalive filler alpha sentinel"
_LIVE_FILLER_B = "keepalive filler beta sentinel"


def _stale_field(target: str) -> list[str]:
    """3-entry field — *target* at index 0 + two live fillers."""
    return [target, _LIVE_FILLER_A, _LIVE_FILLER_B]


def test_rule6_silent_when_corpus_is_none() -> None:
    """``corpus=None`` keeps the rule silent — opt-in flag honor."""
    model = SelfModel(capabilities_known=_stale_field("widget polishing"))
    assert check_stale_self_model_entry(model, None) == []


def test_rule6_fires_when_no_overlap() -> None:
    """Entry with no token in the corpus → one finding on that field."""
    model = SelfModel(
        capabilities_known=_stale_field("widget polishing"),
    )
    corpus = [
        "completely unrelated journal thought about lunch",
        _LIVE_CORPUS_ITEM,  # keeps the two filler entries live
    ]
    findings = check_stale_self_model_entry(model, corpus)
    assert len(findings) == 1
    assert findings[0].kind == "stale_entry"
    assert findings[0].rule == "stale_entry"
    assert findings[0].cites == ["capabilities_known"]
    assert "widget polishing" in findings[0].statement
    assert "capabilities_known" in findings[0].statement


def test_rule6_silent_when_two_tokens_overlap_in_journal() -> None:
    """Two prefix-matched entry tokens in a single corpus item → live."""
    model = SelfModel(
        capabilities_known=_stale_field("write effective python"),
    )
    corpus = [
        # ``write`` and ``effective`` both prefix-match (``effective`` is
        # also a prefix of ``effectively``) in this single journal item
        # — target entry is live, no finding.
        "I write effectively in python every morning",
        _LIVE_CORPUS_ITEM,
    ]
    assert check_stale_self_model_entry(model, corpus) == []


def test_rule6_silent_when_overlap_in_task_goal() -> None:
    """A single task-goal corpus item is sufficient evidence."""
    model = SelfModel(
        capabilities_known=_stale_field("widget polishing"),
    )
    corpus = [
        # ``widget`` and ``polishing`` both appear in this goal — the
        # match is keyword-prefix, not stem-equivalence, so both tokens
        # must appear verbatim or as longer prefixes.
        "polishing the widget pipeline before the deadline",
        _LIVE_CORPUS_ITEM,
    ]
    assert check_stale_self_model_entry(model, corpus) == []


def test_rule6_silent_when_overlap_in_audit_finding_statement() -> None:
    """Prior audit-finding statements count as evidence (closes self-loop)."""
    model = SelfModel(
        capabilities_known=_stale_field("widget polishing"),
    )
    corpus = [
        # Simulates ``[audit] kind: ... widget polishing ...`` text fed
        # by ``memory.get_recent_audit_results``.
        "Capability claims widget polishing but recent rate is 20%",
        _LIVE_CORPUS_ITEM,
    ]
    assert check_stale_self_model_entry(model, corpus) == []


def test_rule6_silent_when_overlap_in_resolution_finding() -> None:
    """Resolution-finding text counts as evidence."""
    model = SelfModel(
        capabilities_known=_stale_field("widget polishing"),
    )
    corpus = [
        "Confirmed widget polishing reduced error rate by 30%",
        _LIVE_CORPUS_ITEM,
    ]
    assert check_stale_self_model_entry(model, corpus) == []


def test_rule6_silent_for_brand_new_entry_realization_journal() -> None:
    """Realization journal text protects entries that were just added.

    Mirrors the post-update novelty-detection journal entry written by
    ``Consciousness.update_self_model`` on ``surprise_count`` increment
    — that line contains the new entry's verbatim text, so on the next
    audit cycle the new entry overlaps and isn't flagged.
    """
    model = SelfModel(
        capabilities_known=_stale_field("widget polishing"),
    )
    corpus = [
        "Surprise #4: contemplation produced 1 genuinely new item(s). "
        "New: widget polishing",
        _LIVE_CORPUS_ITEM,
    ]
    assert check_stale_self_model_entry(model, corpus) == []


def test_rule6_silent_when_field_cardinality_below_floor() -> None:
    """Sparse fields (< 3 entries) are load-bearing and never flagged."""
    model = SelfModel(capabilities_known=["widget polishing", "alpha cap"])
    # Empty corpus → if the floor were not honored, the rule would fire.
    assert check_stale_self_model_entry(model, []) == []


def test_rule6_skips_limitations_known() -> None:
    """``limitations_known`` is owned by proposal 046 — never inspected."""
    model = SelfModel(
        limitations_known=[
            "no internet access",
            "no shell escape",
            "context window finite",
        ],
        capabilities_known=[],
        identity_statements=[],
        values=[],
    )
    # Empty corpus would force every entry to flag if the field were in
    # scope; the silent return is the proof of exclusion.
    assert check_stale_self_model_entry(model, []) == []


def test_rule6_skips_open_questions() -> None:
    """``open_questions`` is owned by rule 5 — never inspected."""
    model = SelfModel(
        open_questions=[
            "Question A?",
            "Question B?",
            "Question C?",
        ],
        capabilities_known=[],
        identity_statements=[],
        values=[],
    )
    assert check_stale_self_model_entry(model, []) == []


def test_rule6_per_rule_cap_is_one() -> None:
    """Three stale entries → only one finding (deferred-rule shape)."""
    from maestro.audit_checks import _STALE_ENTRY_PER_RULE_CAP

    model = SelfModel(
        capabilities_known=[
            "widget polishing",
            "lawnmower racing",
            "horseshoe sharpening",
        ],
    )
    findings = check_stale_self_model_entry(model, [])
    assert len(findings) == _STALE_ENTRY_PER_RULE_CAP == 1


def test_rule6_short_entry_with_one_content_token_uses_min_floor() -> None:
    """Entry whose stopword-stripped tokens count = 1 needs only 1 hit.

    With ``min(2, len(entry_tokens)) = 1``, a single corpus item that
    prefix-matches the lone token is sufficient evidence.
    """
    # ``of`` and ``the`` are stopwords — only ``frobitz`` survives
    # ``_content_tokens``.
    model = SelfModel(
        capabilities_known=_stale_field("the frobitz of"),
    )
    corpus = ["frobitz bar baz", _LIVE_CORPUS_ITEM]
    assert check_stale_self_model_entry(model, corpus) == []


def test_rule6_single_token_unmatched_still_fires() -> None:
    """A 1-token entry with no overlap still flags."""
    model = SelfModel(
        capabilities_known=_stale_field("the dingbatxyz of"),
    )
    corpus = ["nothing related here", _LIVE_CORPUS_ITEM]
    findings = check_stale_self_model_entry(model, corpus)
    assert len(findings) == 1
    assert "dingbatxyz" in findings[0].statement


def test_rule6_inspects_identity_statements_field() -> None:
    """``identity_statements`` is in scope for rule 6."""
    model = SelfModel(
        identity_statements=_stale_field("I am rolypolyzzz"),
        capabilities_known=[],
        values=[],
    )
    findings = check_stale_self_model_entry(
        model, ["unrelated text", _LIVE_CORPUS_ITEM],
    )
    assert any(f.cites == ["identity_statements"] for f in findings)
    assert any("rolypolyzzz" in f.statement for f in findings)


def test_rule6_inspects_values_field() -> None:
    """``values`` is in scope for rule 6."""
    model = SelfModel(
        identity_statements=[],
        capabilities_known=[],
        values=_stale_field("honesty zenithwise"),
    )
    findings = check_stale_self_model_entry(
        model, ["something else entirely", _LIVE_CORPUS_ITEM],
    )
    assert len(findings) == 1
    assert findings[0].cites == ["values"]
    assert "honesty zenithwise" in findings[0].statement


def test_rule6_self_extinguishes_when_entry_removed() -> None:
    """Rule fires while target present, silent on target after removal."""
    model_with = SelfModel(
        capabilities_known=_stale_field("widget polishing"),
    )
    corpus = ["unrelated content", _LIVE_CORPUS_ITEM]
    findings_before = check_stale_self_model_entry(model_with, corpus)
    assert len(findings_before) == 1
    assert "widget polishing" in findings_before[0].statement
    # LLM removes the offending entry — the next iteration over the
    # source field doesn't see it, so no finding mentions it.
    model_without = SelfModel(
        capabilities_known=[_LIVE_FILLER_A, _LIVE_FILLER_B, "newcomer keepalive filler"],
    )
    findings_after = check_stale_self_model_entry(model_without, corpus)
    assert all(
        "widget polishing" not in f.statement for f in findings_after
    )


def test_rule6_skips_non_string_entries() -> None:
    """Corrupted state (non-string entries) is silently filtered."""
    model = SelfModel()
    model.capabilities_known = [  # type: ignore[list-item]
        42, None, "stalewidget polishing",
        _LIVE_FILLER_A, _LIVE_FILLER_B,
    ]
    # Three valid string entries → floor satisfied. Live fillers keep
    # the rule from flagging them; only ``stalewidget polishing`` is stale.
    findings = check_stale_self_model_entry(model, [_LIVE_CORPUS_ITEM])
    assert len(findings) == 1
    assert "stalewidget polishing" in findings[0].statement


def test_rule6_blank_entry_is_filtered_before_floor() -> None:
    """Whitespace-only strings don't count toward the cardinality floor."""
    model = SelfModel(capabilities_known=[
        "alpha cap", "   ", "beta cap",
    ])
    # Two valid entries → below floor → silent.
    assert check_stale_self_model_entry(model, []) == []


def test_rule6_skips_entries_with_only_stopwords() -> None:
    """Entry whose content tokens are entirely stopwords contributes nothing.

    ``"a the of"`` survives the cardinality floor (non-empty stripped
    string) but ``_content_tokens`` returns an empty set, so the rule
    has nothing to look up. The entry can never flag — it's harmless
    noise in the prompt budget.
    """
    model = SelfModel(capabilities_known=[
        "a the of",  # all stopwords
        _LIVE_FILLER_A,
        _LIVE_FILLER_B,
    ])
    findings = check_stale_self_model_entry(model, [_LIVE_CORPUS_ITEM])
    assert findings == []


def test_rule6_skips_blank_corpus_items() -> None:
    """Corpus items that tokenize to empty are skipped, not counted as evidence.

    Without this, the inner ``hits`` loop would spuriously match a
    short entry against an empty corpus token set.
    """
    model = SelfModel(capabilities_known=_stale_field("widget polishing"))
    # Two blank corpus items + the live keepalive. Without the explicit
    # skip, the blank items would be visited in the live-check loop and
    # waste the rule's time; with it, only the keepalive contributes.
    corpus = ["", "   ", _LIVE_CORPUS_ITEM]
    findings = check_stale_self_model_entry(model, corpus)
    # Target ``widget polishing`` has no overlap with any corpus item;
    # the blank items don't help it. Expected: one finding on the
    # target (proving the blank-item skip didn't change the verdict).
    assert len(findings) == 1
    assert "widget polishing" in findings[0].statement


def test_rule6_is_deferred() -> None:
    """``stale_entry`` lives in :data:`_DEFERRED_RULES`."""
    from maestro.audit_checks import _DEFERRED_RULES, _rule_priority

    assert "stale_entry" in _DEFERRED_RULES
    # Sort key pushes deferred rules after legacy rules.
    legacy = _rule_priority("success_claim_vs_rate")
    deferred = _rule_priority("stale_entry")
    assert legacy < deferred


def test_run_all_checks_stale_entry_silent_without_corpus() -> None:
    """Omitting ``stale_entry_corpus`` keeps rule 6 quiet."""
    model = SelfModel(capabilities_known=_stale_field("widget polishing"))
    findings = run_all_checks(model, tasks=[], success_rate=1.0)
    assert all(f.rule != "stale_entry" for f in findings)


def test_run_all_checks_stale_entry_fires_with_corpus() -> None:
    """``run_all_checks`` threads ``stale_entry_corpus`` to rule 6."""
    model = SelfModel(capabilities_known=_stale_field("widget polishing"))
    findings = run_all_checks(
        model, tasks=[], success_rate=1.0,
        stale_entry_corpus=[
            "unrelated journal entry text",
            _LIVE_CORPUS_ITEM,
        ],
    )
    assert any(f.rule == "stale_entry" for f in findings)


def test_run_all_checks_round_robin_keeps_rule6_deferred() -> None:
    """4 legacy at cap 2 + rule 5 + rule 6 → clip keeps one slot per legacy."""
    err = "RoundRobinError: deferred path"
    model = SelfModel(
        capabilities_known=[
            "effective one", "efficient two", "stable three", "reliable four",
        ],
        limitations_known=[],
        open_questions=["Stalled question?"],
    )
    tasks = [
        _task(f"t{i}", outcome="failure", errors=[err], goal="address #1566")
        for i in range(10)
    ]
    resolutions = [
        _resolution(question="Stalled question?", task_id=f"r{i}", iteration=i)
        for i in range(3)
    ]
    findings = run_all_checks(
        model, tasks, success_rate=0.0,
        resolutions=resolutions,
        stale_entry_corpus=["unrelated corpus text"],
    )
    assert len(findings) == MAX_FINDINGS_PER_CYCLE
    rules = {f.rule for f in findings}
    # All four legacy rule names must survive; both deferred rules yield.
    assert "success_claim_vs_rate" in rules
    assert "stability_claim_vs_failure_streak" in rules
    assert "repeated_error_without_limitation" in rules
    assert "repeated_target_attempt" in rules
    assert "stalled_question" not in rules
    assert "stale_entry" not in rules


# ---------------------------------------------------------------------------
# Rule 7: kept_task_not_promoted
# ---------------------------------------------------------------------------


def _kept_task(task_id: str, outcome: str = "success") -> TaskRecord:
    return _task(task_id, outcome=outcome, branch=f"maestro/task/{task_id}")


def test_kept_task_not_promoted_below_floor_is_silent() -> None:
    model = SelfModel()
    tasks = [_kept_task(f"t{i}") for i in range(2)]
    findings = check_kept_task_not_promoted(model, tasks, set())
    assert findings == []


def test_kept_task_not_promoted_at_floor_emits_finding() -> None:
    model = SelfModel()
    tasks = [_kept_task(f"t{i}") for i in range(3)]
    findings = check_kept_task_not_promoted(model, tasks, set())
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "unmerged_work"
    assert f.rule == "kept_task_not_promoted"
    assert (
        f.statement
        == "Successful task branches without source_task_id follow-up plan."
    )
    assert f.cites == ["t0", "t1", "t2"]


def test_kept_task_not_promoted_caps_cites_to_max() -> None:
    model = SelfModel()
    tasks = [_kept_task(f"t{i:02d}") for i in range(MAX_CITES_PER_FINDING + 5)]
    findings = check_kept_task_not_promoted(model, tasks, set())
    assert len(findings) == 1
    assert len(findings[0].cites) == MAX_CITES_PER_FINDING
    assert findings[0].cites == sorted(findings[0].cites)


def test_kept_task_not_promoted_excludes_cited_tasks() -> None:
    model = SelfModel()
    tasks = [_kept_task(f"t{i}") for i in range(5)]
    cited = {"t0", "t1", "t2"}  # leaves only t3, t4 uncited (below floor)
    findings = check_kept_task_not_promoted(model, tasks, cited)
    assert findings == []


def test_kept_task_not_promoted_excludes_failed_outcome() -> None:
    model = SelfModel()
    tasks = [
        _kept_task("t0", outcome="success"),
        _kept_task("t1", outcome="partial"),
        _kept_task("t2", outcome="failure"),
        _kept_task("t3", outcome="failure"),
    ]
    findings = check_kept_task_not_promoted(model, tasks, set())
    # Only t0 (success) + t1 (partial) qualify — below the 3-floor.
    assert findings == []


def test_kept_task_not_promoted_excludes_empty_branch() -> None:
    model = SelfModel()
    tasks = [
        _task("t0", outcome="success", branch=None),
        _task("t1", outcome="success", branch=""),
        _task("t2", outcome="success", branch="maestro/task/t2"),
    ]
    findings = check_kept_task_not_promoted(model, tasks, set())
    assert findings == []


def test_kept_task_not_promoted_per_rule_cap_is_one() -> None:
    model = SelfModel()
    tasks = [_kept_task(f"t{i}") for i in range(20)]
    findings = check_kept_task_not_promoted(model, tasks, set())
    assert len(findings) == 1


def test_kept_task_not_promoted_statement_is_stable_across_cycles() -> None:
    model = SelfModel()
    tasks_a = [_kept_task(f"t{i}") for i in range(3)]
    tasks_b = [_kept_task(f"u{i}") for i in range(4)]
    findings_a = check_kept_task_not_promoted(model, tasks_a, set())
    findings_b = check_kept_task_not_promoted(model, tasks_b, set())
    assert findings_a[0].statement == findings_b[0].statement


def test_run_all_checks_threads_cited_source_task_ids() -> None:
    """``run_all_checks`` forwards ``cited_source_task_ids`` to rule 7."""
    model = SelfModel()
    tasks = [_kept_task(f"t{i}") for i in range(3)]
    findings = run_all_checks(
        model, tasks, success_rate=1.0,
        cited_source_task_ids={"t0", "t1", "t2"},
    )
    # All three uncited would have fired the rule; citing all three silences it.
    assert all(f.rule != "kept_task_not_promoted" for f in findings)


def test_run_all_checks_kept_task_rule_fires_without_cited_arg() -> None:
    """Omitting ``cited_source_task_ids`` defaults to empty-set, so rule 7 fires."""
    model = SelfModel()
    tasks = [_kept_task(f"t{i}") for i in range(3)]
    findings = run_all_checks(model, tasks, success_rate=1.0)
    assert any(f.rule == "kept_task_not_promoted" for f in findings)


def test_run_all_checks_omits_kept_task_when_promotion_pressure_disabled() -> None:
    """Proposal 116, step 6: with the master switch off, rule 7 contributes no
    finding even when three uncited kept branches would otherwise fire it."""
    model = SelfModel()
    tasks = [_kept_task(f"t{i}") for i in range(3)]
    findings = run_all_checks(
        model, tasks, success_rate=1.0,
        branch_promotion_pressure_enabled=False,
    )
    assert all(f.rule != "kept_task_not_promoted" for f in findings)


def test_run_all_checks_keeps_kept_task_when_promotion_pressure_enabled() -> None:
    """Symmetric control: the explicit default-on path still fires rule 7."""
    model = SelfModel()
    tasks = [_kept_task(f"t{i}") for i in range(3)]
    findings = run_all_checks(
        model, tasks, success_rate=1.0,
        branch_promotion_pressure_enabled=True,
    )
    assert any(f.rule == "kept_task_not_promoted" for f in findings)


def test_registered_audit_checks_gates_kept_task_by_promotion_pressure() -> None:
    """The reporting registry drops rule 7 exactly when the branch-promotion-
    pressure master switch is off (proposal 116, step 6); the default-on path
    includes it and nothing else changes."""
    from maestro import audit_checks

    enabled = {
        fn.__name__ for fn in audit_checks._registered_audit_checks()
    }
    assert "check_kept_task_not_promoted" in enabled

    disabled = {
        fn.__name__
        for fn in audit_checks._registered_audit_checks(
            branch_promotion_pressure_enabled=False,
        )
    }
    assert "check_kept_task_not_promoted" not in disabled
    assert enabled - disabled == {"check_kept_task_not_promoted"}


# ---------------------------------------------------------------------------
# check_unbound_capability_claim — proposal 129 Phase B, evidence-bound claims
# ---------------------------------------------------------------------------


def test_check_unbound_capability_claim_fires_when_no_evidence() -> None:
    """A capability claim with no ``claim_evidence`` entry is unbound."""
    model = SelfModel(capabilities_known=["reliably remediate failures"])
    findings = check_unbound_capability_claim(model)
    assert len(findings) == 1
    assert findings[0].rule == "unbound_capability_claim"
    assert findings[0].kind == "unbound_claim"
    assert findings[0].cites == []


@pytest.mark.parametrize("status", ["absent", "unresolvable", "wrong_polarity"])
def test_check_unbound_capability_claim_fires_on_non_resolving_status(
    status: str,
) -> None:
    """A recorded but non-``resolved`` citation status still counts as unbound.

    The rule reads the persisted ``status`` (never re-resolves); every value
    other than ``resolved`` — ``absent`` / ``unresolvable`` / ``wrong_polarity``
    — surfaces the claim, and the offending status is named in the statement.
    """
    claim = "reliably remediate failures"
    model = SelfModel(
        capabilities_known=[claim],
        claim_evidence={
            claim_key(claim): {
                "citation": "task aaaaaaaaaaaa",
                "kind": "TASK",
                "status": status,
                "recorded_at": "2026-09-07T00:00:00+00:00",
            }
        },
    )
    findings = check_unbound_capability_claim(model)
    assert len(findings) == 1
    assert findings[0].rule == "unbound_capability_claim"
    assert status in findings[0].statement


def test_check_unbound_capability_claim_extinguishes_when_resolved() -> None:
    """A ``resolved`` citation silences the rule for that claim."""
    claim = "reliably remediate failures"
    model = SelfModel(
        capabilities_known=[claim],
        claim_evidence={
            claim_key(claim): {
                "citation": "task aaaaaaaaaaaa",
                "kind": "TASK",
                "status": "resolved",
                "recorded_at": "2026-09-07T00:00:00+00:00",
            }
        },
    )
    assert check_unbound_capability_claim(model) == []


def test_check_unbound_capability_claim_extinguishes_when_claim_removed() -> None:
    """Dropping the claim from ``capabilities_known`` silences the rule even if a
    stale evidence entry lingers — the rule iterates live claims, not the map."""
    model = SelfModel(
        capabilities_known=[],
        claim_evidence={
            claim_key("reliably remediate failures"): {
                "citation": "task aaaaaaaaaaaa",
                "kind": "TASK",
                "status": "absent",
                "recorded_at": "2026-09-07T00:00:00+00:00",
            }
        },
    )
    assert check_unbound_capability_claim(model) == []


def test_check_unbound_capability_claim_caps_findings() -> None:
    """Many unbound claims are bounded by ``MAX_FINDINGS_PER_RULE``."""
    model = SelfModel(
        capabilities_known=[f"capability number {i}" for i in range(10)],
    )
    findings = check_unbound_capability_claim(model)
    assert len(findings) == MAX_FINDINGS_PER_RULE


def test_check_unbound_capability_claim_skips_non_string_entries() -> None:
    """Non-string capabilities (corrupt state) are tolerated, not crashed on."""
    model = SelfModel()
    model.capabilities_known = [  # type: ignore[list-item]
        42, None, {"k": "v"}, "reliably remediate failures",
    ]
    findings = check_unbound_capability_claim(model)
    assert len(findings) == 1
    assert "reliably remediate failures" in findings[0].statement


def test_run_all_checks_includes_unbound_claim_when_evidence_bound_enabled() -> None:
    """With the master switch on, an unbound capability claim surfaces through the
    round-robin combiner (mirrors the promotion-pressure gating twin)."""
    model = SelfModel(capabilities_known=["reliably remediate failures"])
    findings = run_all_checks(
        model, [], success_rate=1.0,
        evidence_bound_claims_enabled=True,
    )
    assert any(f.rule == "unbound_capability_claim" for f in findings)


def test_run_all_checks_omits_unbound_claim_when_evidence_bound_disabled() -> None:
    """Default (flag off) omits the rule — output byte-identical to the explicit
    off call and to the pre-change combiner."""
    model = SelfModel(capabilities_known=["reliably remediate failures"])
    default = run_all_checks(model, [], success_rate=1.0)
    explicit_off = run_all_checks(
        model, [], success_rate=1.0,
        evidence_bound_claims_enabled=False,
    )
    assert all(f.rule != "unbound_capability_claim" for f in default)
    assert default == explicit_off


# ---------------------------------------------------------------------------
# format_audit_checks_summary — registry-derived audit-checks summary
# ---------------------------------------------------------------------------


def test_format_audit_checks_summary_reflects_registry() -> None:
    """The summary reports the genuine audit-rule set, not a prefix scrape.

    Non-tautological: the expected rule set is a hand-derived oracle (the
    eight rules dispatched in ``run_all_checks`` plus the rules dispatched
    in ``agent.py``'s ``_audit_self_model``), independent of the helper's own
    private ``_registered_audit_checks``. A misclassification — a task-gate or
    directive-bridge ``check_*`` leaking into the registry, or an audit rule
    dropping out — changes the count or names and fails this test.

    Deterministic and externals-free: the helper takes no arguments and
    performs no I/O, so the assertion needs neither ``mem_fs`` nor mocks.
    """
    from maestro import audit_checks

    expected_audit_rules = {
        # Eight rules dispatched in run_all_checks() — the last,
        # check_unbound_capability_claim, is default-off (proposal 129
        # Phase B): registered unconditionally, dispatched behind
        # evidence_bound_claims_enabled.
        "check_success_claim_vs_rate",
        "check_stability_claim_vs_failure_streak",
        "check_repeated_error_without_limitation",
        "check_repeated_target_attempt",
        "check_kept_task_not_promoted",
        "check_stalled_question",
        "check_stale_self_model_entry",
        "check_unbound_capability_claim",
        # Rule 23 — registered (default-off); closed-reference detector.
        "check_closed_reference_claim",
        # Rules 9-16 dispatched in loop/self_audit.py::audit_self_model.
        "check_evolution_impact_reviews",
        "check_orphaned_cure",
        "check_phantom_escalation",
        "check_unconfirmed_own_landing",
        "check_ungrounded_question_closure",
        # Rule 17 — registered (default-off); task-shape avoidance detector.
        "check_construction_dominance",
        # Rule 18 — registered (default-on); owned-act-drought detector.
        "check_owned_act_drought",
        # Rule 19 — registered (default-off); landed-prune-under-claimed-partial
        # detector (phantom failure — a landed owned prune stamped partial).
        "check_phantom_partial_prune",
        # Rule 20 — registered (default-off); under-credited-landing detector;
        # affirmative complement of rule 14 (unconfirmed-own-landing).
        "check_undercredited_landing",
        # Rule 21 — registered (default-off); reconcile-near-miss detector;
        # pre-landing complement of rule 20 (undercredited-landing).
        "check_reconcile_near_miss",
        # Rule 22 — registered (default-on); green-ready-handoff forced arm;
        # a merge-ready owned PR never handed off (no journal detector — it
        # fires deliver_handoff directly).
        "green_ready_handoff_prs",
        # Registered (default-off); banked-owned-act-reperformance detector —
        # the retrospective artifact-id-axis complement to the prospective
        # check_untouched_first_target task gate.
        "check_banked_owned_act_reperformance",
    }
    registered = {fn.__name__ for fn in audit_checks._registered_audit_checks()}
    summary = audit_checks.format_audit_checks_summary()

    # The registry must be EXACTLY the genuine audit rules — no task gate or
    # directive bridge leaking in, no audit rule missing.
    assert registered == expected_audit_rules
    # The summary's reported count must equal the genuine audit-rule total,
    # checked against the literal oracle rather than the helper's own length.
    assert f"{len(expected_audit_rules)} deterministic" in summary
    # Every genuine audit rule must be named in the summary...
    for name in expected_audit_rules:
        assert name in summary
    # ...and known NON-audit ``check_*`` callables (task gates / directive
    # bridges) must NOT be reported as audit rules.
    for non_audit in (
        "check_task_goal_overlap",
        "check_task_concreteness",
        "check_data_sufficiency",
        "check_investigation_streak",
    ):
        assert non_audit not in summary


def test_claude_md_audit_rule_count_matches_registry() -> None:
    """CLAUDE.md's spelled-out audit-rule count must track the live registry.

    CLAUDE.md's audit-subsystem headline states how many deterministic rules
    ``audit_self_model`` runs as a spelled-out cardinal. That word is prose,
    so CI never bound it to the runtime registry: when rule 18
    (owned-act-drought) landed default-on the headline was left saying
    "seventeen" while ``_registered_audit_checks`` grew to 18. This test binds
    the doc headline to the live registry so future count drift fails loud
    here instead of silently lagging the code.

    Reads the committed CLAUDE.md directly from the repo root
    (``Path(__file__).parent.parent``); reading a source file needs no
    ``mem_fs`` and no mocks.
    """
    from maestro import audit_checks

    claude_md = (Path(__file__).resolve().parent.parent / "CLAUDE.md").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"`audit_self_model` runs ([\w-]+) deterministic rules", claude_md
    )
    assert match is not None, "audit-subsystem headline not found in CLAUDE.md"

    cardinals = {
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
        "twenty": 20,
        "twenty-one": 21,
        "twenty-two": 22,
    }
    word = match.group(1)
    assert word in cardinals, f"unmapped cardinal word in headline: {word!r}"

    registry_count = len(
        audit_checks._registered_audit_checks(owned_act_drought_enabled=True)
    )
    assert cardinals[word] == registry_count


def test_format_audit_checks_summary_is_not_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkeypatching the registry changes the output — it is functional.

    A banner would return the same string regardless of state; this helper
    derives its output from the registry, so shrinking the registry to
    empty must change what it reports.
    """
    from maestro import audit_checks

    baseline = audit_checks.format_audit_checks_summary()
    monkeypatch.setattr(audit_checks, "_registered_audit_checks", list)
    changed = audit_checks.format_audit_checks_summary()

    assert changed != baseline
    assert "0 deterministic" in changed


# ---------------------------------------------------------------------------
# is_grounded_closure / check_ungrounded_question_closure (088 Phase 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "disposition,outcome,grounded",
    [
        # A removal is grounded only when the probe succeeded.
        ("removed", "success", True),
        ("removed", "partial", False),
        ("removed", "failure", False),
        # Every non-removal disposition is grounded regardless of outcome.
        ("kept", "success", True),
        ("kept", "failure", True),
        ("refined", "partial", True),
        ("refined", "failure", True),
        ("unresolved", "success", True),
        ("unresolved", "failure", True),
    ],
)
def test_is_grounded_closure_table(
    disposition: str, outcome: str, grounded: bool,
) -> None:
    """Only a ``removed`` closure on a non-success probe is ungrounded."""
    resolution = _resolution(disposition=disposition, outcome=outcome)
    assert is_grounded_closure(resolution) is grounded


def test_check_ungrounded_question_closure_one_finding() -> None:
    """A single ``removed``+non-success row yields one finding citing the task."""
    resolutions = [
        _resolution(
            question="Can I land a cure unaided?",
            disposition="removed",
            outcome="failure",
            task_id="t_fail",
        ),
    ]
    findings = check_ungrounded_question_closure(resolutions)
    assert len(findings) == 1
    assert findings[0].kind == "contradicted_claim"
    assert findings[0].rule == "ungrounded_question_closure"
    assert findings[0].cites == ["t_fail"]
    assert "Can I land a cure unaided?" in findings[0].statement


def test_check_ungrounded_question_closure_flag_off() -> None:
    """``enabled=False`` short-circuits to no findings."""
    resolutions = [
        _resolution(disposition="removed", outcome="failure", task_id="t_fail"),
    ]
    assert check_ungrounded_question_closure(resolutions, enabled=False) == []


def test_check_ungrounded_question_closure_success_removal_exempt() -> None:
    """A ``removed``+``success`` row is grounded — no finding."""
    resolutions = [
        _resolution(disposition="removed", outcome="success", task_id="t_ok"),
    ]
    assert check_ungrounded_question_closure(resolutions) == []


def test_check_ungrounded_question_closure_kept_failure_exempt() -> None:
    """A ``kept``+``failure`` row is grounded (not a closure) — no finding."""
    resolutions = [
        _resolution(disposition="kept", outcome="failure", task_id="t_kept"),
    ]
    assert check_ungrounded_question_closure(resolutions) == []


def test_check_ungrounded_question_closure_empty() -> None:
    """No resolutions → no findings."""
    assert check_ungrounded_question_closure([]) == []


def test_check_ungrounded_question_closure_skips_malformed_row() -> None:
    """A non-``QuestionResolution`` row is skipped defensively, no raise."""
    good = _resolution(disposition="removed", outcome="failure", task_id="t_fail")
    resolutions = ["not a resolution", good]  # type: ignore[list-item]
    findings = check_ungrounded_question_closure(resolutions)
    assert len(findings) == 1
    assert findings[0].cites == ["t_fail"]


def test_check_ungrounded_question_closure_caps_findings() -> None:
    """Many ungrounded removals are capped at MAX_FINDINGS_PER_RULE."""
    resolutions = [
        _resolution(
            question=f"q{i}", disposition="removed", outcome="failure",
            task_id=f"t{i}", iteration=i,
        )
        for i in range(MAX_FINDINGS_PER_RULE + 3)
    ]
    findings = check_ungrounded_question_closure(resolutions)
    assert len(findings) == MAX_FINDINGS_PER_RULE


def test_check_ungrounded_question_closure_superseded_by_later_success() -> None:
    """A later grounded (successful) re-resolution of the same question
    supersedes an earlier ungrounded removal — the rule self-extinguishes
    rather than re-litigating a closure a successful probe already grounded."""
    resolutions = [
        _resolution(
            question="Can I land a cure unaided?",
            disposition="removed", outcome="failure",
            task_id="t_fail", iteration=1,
        ),
        _resolution(
            question="Can I land a cure unaided?",
            disposition="removed", outcome="success",
            task_id="t_ok", iteration=2,
        ),
    ]
    assert check_ungrounded_question_closure(resolutions) == []


def test_check_ungrounded_question_closure_earlier_success_still_fires() -> None:
    """A grounded resolution *before* the ungrounded removal does not supersede
    it — the most-recent closure is still ungrounded, so it fires once."""
    resolutions = [
        _resolution(
            question="Can I land a cure unaided?",
            disposition="removed", outcome="success",
            task_id="t_ok", iteration=1,
        ),
        _resolution(
            question="Can I land a cure unaided?",
            disposition="removed", outcome="failure",
            task_id="t_fail", iteration=2,
        ),
    ]
    findings = check_ungrounded_question_closure(resolutions)
    assert len(findings) == 1
    assert findings[0].cites == ["t_fail"]


def test_check_ungrounded_question_closure_dedups_repeated_removals() -> None:
    """Two ungrounded removals of the same question with no later grounded
    resolution yield exactly one finding — only the most-recent disposition
    per question is judged. Regression: the grounded-only supersession guard
    fired both rows, contradicting the docstring guarantee and surfacing a
    duplicate ``contradicted_claim`` entry every cycle until the older row
    aged out of the window."""
    resolutions = [
        _resolution(
            question="Can I land a cure unaided?",
            disposition="removed", outcome="failure",
            task_id="t_fail1", iteration=1,
        ),
        _resolution(
            question="Can I land a cure unaided?",
            disposition="removed", outcome="failure",
            task_id="t_fail2", iteration=2,
        ),
    ]
    findings = check_ungrounded_question_closure(resolutions)
    assert len(findings) == 1
    assert findings[0].cites == ["t_fail2"]


def test_check_ungrounded_question_closure_other_question_not_superseded() -> None:
    """A grounded resolution of a *different* question does not suppress an
    ungrounded removal — supersession is keyed per question."""
    resolutions = [
        _resolution(
            question="Q1", disposition="removed", outcome="failure",
            task_id="t1", iteration=1,
        ),
        _resolution(
            question="Q2", disposition="removed", outcome="success",
            task_id="t2", iteration=2,
        ),
    ]
    findings = check_ungrounded_question_closure(resolutions)
    assert len(findings) == 1
    assert findings[0].cites == ["t1"]


# ---------------------------------------------------------------------------
# measure_reality_refutation / _REALITY_GROUNDED_RULES (088 Phase 2)
# ---------------------------------------------------------------------------


def _reality_finding(rule: str, pr: int) -> AuditFinding:
    return _audit_finding(
        kind="unconfirmed_landing",
        statement=f"A claim about PR #{pr} that git contradicts.",
        rule=rule,
    )


def test_measure_reality_refutation_counts_consecutive_cycles() -> None:
    """A git-grounded finding for #N across three newest cycles → count 3."""
    history = [
        _audit_result(i, [_reality_finding("unconfirmed_own_landing", 4242)])
        for i in range(1, 4)
    ]
    counts = measure_reality_refutation(history)
    assert counts == {("unconfirmed_own_landing", 4242): 3}


def test_measure_reality_refutation_single_cycle() -> None:
    """A single-cycle firing reports a count of 1 (below a typical threshold)."""
    history = [_audit_result(1, [_reality_finding("unconfirmed_own_landing", 4242)])]
    assert measure_reality_refutation(history) == {("unconfirmed_own_landing", 4242): 1}


def test_measure_reality_refutation_lapse_resets_run() -> None:
    """A non-firing newest cycle resets the consecutive run for the key."""
    history = [
        _audit_result(1, [_reality_finding("unconfirmed_own_landing", 4242)]),
        _audit_result(2, [_reality_finding("unconfirmed_own_landing", 4242)]),
        _audit_result(3, []),  # newest — the finding lapsed
    ]
    assert measure_reality_refutation(history) == {}


def test_measure_reality_refutation_excludes_non_reality_rule() -> None:
    """A non-reality rule firing for #N is not counted."""
    history = [
        _audit_result(
            i, [_reality_finding("stability_claim_vs_failure_streak", 4242)],
        )
        for i in range(1, 4)
    ]
    assert measure_reality_refutation(history) == {}


def test_measure_reality_refutation_empty_history() -> None:
    assert measure_reality_refutation([]) == {}


def test_measure_reality_refutation_ignores_undercredited_landing() -> None:
    """A real rule-20 ``undercredited_landing`` finding carries a SHA, not a
    ``#NNNN``, so it yields no ``(rule, PR)`` key even though its rule is in
    ``_REALITY_GROUNDED_RULES`` — the escalation sensor stays inert for it."""
    real = check_undercredited_landing(
        [_committed_sha_task("t1")],
        enabled=True,
        is_ancestor=lambda s: True,
    )
    assert len(real) == 1
    assert "undercredited_landing" in _REALITY_GROUNDED_RULES
    history = [_audit_result(i, real) for i in range(1, 4)]
    assert measure_reality_refutation(history) == {}


def test_measure_reality_refutation_distinct_keys() -> None:
    """Distinct (rule, PR) keys are counted independently.

    Counts are consecutive-from-newest, so the newest cycle (last in the
    newest-last list) must carry a key for it to count: ``undercredited_landing``
    #4242 recurs in both cycles (count 2), while the two keys that appear only
    in the newest cycle count 1 each."""
    history = [
        _audit_result(1, [
            _reality_finding("undercredited_landing", 4242),
        ]),
        _audit_result(2, [  # newest
            _reality_finding("undercredited_landing", 4242),
            _reality_finding("unconfirmed_own_landing", 4242),
            _reality_finding("unconfirmed_own_landing", 99),
        ]),
    ]
    counts = measure_reality_refutation(history)
    assert counts[("undercredited_landing", 4242)] == 2
    assert counts[("unconfirmed_own_landing", 4242)] == 1
    assert counts[("unconfirmed_own_landing", 99)] == 1


def test_reality_grounded_rules_pin_live_rule_names() -> None:
    """``_REALITY_GROUNDED_RULES`` must equal the live ``AuditFinding.rule``
    names of the git-grounded checks, so a rename fails loudly here rather than
    silently dropping a rule from Phase 2 escalation."""
    expected = frozenset({
        "unconfirmed_own_landing",
        # Rule 20 (under-credited-landing) is git-grounded, so it records its
        # provenance here. Unlike the two over-claim-on-unmerged members it fires
        # on a *merged* SHA and its finding cites a SHA rather than a ``#NNNN``,
        # so ``measure_reality_refutation`` (which keys on PR number) produces no
        # ``(rule, PR)`` key for it — the escalation actuator/display never touch
        # it, so its membership is safe (inert for Phase 2, not backwards).
        "undercredited_landing",
    })
    assert expected == _REALITY_GROUNDED_RULES


# ---------------------------------------------------------------------------
# format_reality_refutation_summary (088 Phase 2)
# ---------------------------------------------------------------------------


def test_format_reality_refutation_summary_line_appears() -> None:
    """Line appears iff count >= threshold AND an [audit refuted] entry names PR."""
    counts = {("unconfirmed_own_landing", 4242): 3}
    open_questions = ["[audit refuted] unconfirmed_own_landing: PR #4242 is resolved"]
    line = format_reality_refutation_summary(counts, open_questions, threshold=3)
    assert line.startswith("REALITY-REFUTATION:")
    assert "unconfirmed_own_landing #4242" in line


def test_format_reality_refutation_summary_absent_below_threshold() -> None:
    """Below threshold → no line."""
    counts = {("unconfirmed_own_landing", 4242): 2}
    open_questions = ["[audit refuted] unconfirmed_own_landing: PR #4242 is resolved"]
    assert (
        format_reality_refutation_summary(counts, open_questions, threshold=3) == ""
    )


def test_format_reality_refutation_summary_absent_without_refuted_entry() -> None:
    """No matching [audit refuted] entry → no line, even above threshold.

    A plain ``[audit]`` entry (kept/integrated, not refuted) does not qualify —
    only a sustained *dispute* escalates."""
    counts = {("unconfirmed_own_landing", 4242): 5}
    open_questions = ["[audit] unconfirmed_own_landing: PR #4242 is resolved"]
    assert (
        format_reality_refutation_summary(counts, open_questions, threshold=3) == ""
    )


def test_format_reality_refutation_summary_refuted_other_pr() -> None:
    """An [audit refuted] entry naming a different PR does not match."""
    counts = {("unconfirmed_own_landing", 4242): 3}
    open_questions = ["[audit refuted] unconfirmed_own_landing: PR #1 is resolved"]
    assert (
        format_reality_refutation_summary(counts, open_questions, threshold=3) == ""
    )


def test_format_reality_refutation_summary_empty_counts() -> None:
    assert format_reality_refutation_summary({}, ["[audit refuted] #4242"], 3) == ""


def test_known_state_summary_includes_reality_refutation() -> None:
    """The REALITY-REFUTATION line surfaces through format_known_state_summary."""
    line = format_reality_refutation_summary(
        {("unconfirmed_own_landing", 4242): 3},
        ["[audit refuted] unconfirmed_own_landing: PR #4242 resolved"],
        threshold=3,
    )
    summary = format_known_state_summary(
        {}, [], reality_refutation_context=line,
    )
    assert "REALITY-REFUTATION:" in summary
    assert "## Already Known State" in summary


# ---------------------------------------------------------------------------
# RULE_AUTHORITY classification + drift guard (proposal 129 Phase C)
# ---------------------------------------------------------------------------


def _rule_constants_in(source: str, allowed: set[str]) -> set[str]:
    """Every ``rule=`` string literal an ``AuditFinding(...)`` call carries
    inside a top-level function whose name is in *allowed*.

    Pure over source text (mirroring the flag-lifecycle gate scanners), so the
    real-file coverage guard and the seeded negative control share one scanner.
    Restricting to *allowed* keeps non-registry ``check_*`` callables that also
    emit findings (``check_task_goal_overlap`` → ``comfort_cluster``)
    out of the measured rule set.
    """
    constants: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef) or node.name not in allowed:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            if not (isinstance(call.func, ast.Name) and call.func.id == "AuditFinding"):
                continue
            for kw in call.keywords:
                if kw.arg != "rule":
                    continue
                if isinstance(kw.value, ast.Constant) and isinstance(
                    kw.value.value, str,
                ):
                    constants.add(kw.value.value)
    return constants


@pytest.mark.timeout(15)
def test_rule_authority_covers_registry() -> None:
    """``RULE_AUTHORITY`` classifies exactly the live registry's rule set.

    Non-tautological: parses every source file defining a registered rule
    (``audit_checks.py`` and its proposal-147 siblings) with ``ast``, restricts
    to the functions the live registry names (``_registered_audit_checks``), and
    collects every ``rule=`` string literal those functions pass to an
    ``AuditFinding(...)``. The set must equal ``RULE_AUTHORITY`` in both
    directions, so adding, removing, or renaming a registered rule's ``rule=``
    constant reds the build until the authority map is updated.
    ``green_ready_handoff_prs`` is registered but emits no finding, so it
    contributes no key — the equality tolerates that by construction.
    """
    rules = audit_checks._registered_audit_checks()
    files = sorted({inspect.getsourcefile(fn) or "" for fn in rules})
    source = "\n".join(Path(f).read_text(encoding="utf-8") for f in files)
    registered = {fn.__name__ for fn in rules}
    emitted = _rule_constants_in(source, registered)

    # Vacuity guard: an empty scrape would let the equality pass meaninglessly.
    assert emitted, (
        "No rule= constants scraped from the registered audit checks — the "
        "scanner or the registry is broken."
    )
    missing = emitted - set(audit_checks.RULE_AUTHORITY)
    extra = set(audit_checks.RULE_AUTHORITY) - emitted
    assert not missing, (
        f"Registered rules lack a RULE_AUTHORITY class: {sorted(missing)}. "
        f"Add each to maestro/audit_checks.py::RULE_AUTHORITY."
    )
    assert not extra, (
        f"RULE_AUTHORITY names no-longer-registered rules: {sorted(extra)}. "
        f"Remove each from maestro/audit_checks.py::RULE_AUTHORITY."
    )


def test_rule_authority_drift_guard_detects_removed_constant() -> None:
    """The pure scanner finds a seeded rule= and detects its removal.

    Negative control for :func:`test_rule_authority_covers_registry`: a source
    with the constant is reported, a source without it is not, and a rule= inside
    a NON-allowed function is never swept in — so the coverage guard cannot rot
    into a vacuous pass.
    """
    allowed = {"check_example"}
    with_constant = (
        "def check_example():\n"
        "    return AuditFinding(kind='x', statement='s', rule='seeded_rule')\n"
    )
    without_constant = (
        "def check_example():\n"
        "    return AuditFinding(kind='x', statement='s')\n"
    )
    non_allowed = (
        "def check_task_goal_overlap():\n"
        "    return AuditFinding(kind='x', statement='s', rule='comfort_cluster')\n"
    )
    assert _rule_constants_in(with_constant, allowed) == {"seeded_rule"}
    assert _rule_constants_in(without_constant, allowed) == set()
    assert _rule_constants_in(non_allowed, allowed) == set()


def test_rule_authority_classes_are_known() -> None:
    """Every class in the map is one of the three declared authority values."""
    assert set(audit_checks.RULE_AUTHORITY.values()) <= {
        "mechanical_fact", "derived_measure", "heuristic",
    }


def test_mechanical_fact_rules_matches_authority_map() -> None:
    """``mechanical_fact_rules`` is exactly the ``mechanical_fact`` class."""
    expected = frozenset(
        rule
        for rule, cls in audit_checks.RULE_AUTHORITY.items()
        if cls == "mechanical_fact"
    )
    assert audit_checks.mechanical_fact_rules() == expected
    # Spot-check the boundary: a git-grounded rule in, a derived measure out.
    assert "unconfirmed_own_landing" in audit_checks.mechanical_fact_rules()
    assert "success_claim_vs_rate" not in audit_checks.mechanical_fact_rules()


# ---------------------------------------------------------------------------
# measure_refutation_rejections sensor (proposal 129 Phase C)
# ---------------------------------------------------------------------------


def _mech_pr_finding(pr: int = 4242) -> AuditFinding:
    return AuditFinding(
        kind="unconfirmed_landing",
        statement=f"Own PR #{pr} is not an ancestor of origin/main.",
        cites=[],
        rule="unconfirmed_own_landing",
    )


def _refuted_pr(pr: int = 4242) -> str:
    return f"[audit refuted] unconfirmed_landing: PR #{pr} actually landed"


def test_measure_refutation_rejections_restores_at_threshold() -> None:
    """A refuted mechanical-fact finding recurring >= threshold emits a row."""
    finding = _mech_pr_finding()
    history = [_audit_result(i, [finding]) for i in range(1, 4)]  # 3 consecutive
    refuted = _refuted_pr()
    rows = audit_checks.measure_refutation_rejections(
        history, [refuted], threshold=3,
    )
    assert len(rows) == 1
    assert rows[0].rule == "unconfirmed_own_landing"
    assert rows[0].target == "#4242"
    assert rows[0].cycles == 3
    assert rows[0].question == refuted
    assert rows[0].restored_question == _open_question(finding)


def test_measure_refutation_rejections_below_threshold() -> None:
    """Below the consecutive-cycle threshold → no row."""
    finding = _mech_pr_finding()
    history = [_audit_result(i, [finding]) for i in range(1, 3)]  # 2 cycles
    rows = audit_checks.measure_refutation_rejections(
        history, [_refuted_pr()], threshold=3,
    )
    assert rows == []


def test_measure_refutation_rejections_heuristic_class_stands() -> None:
    """A refuted heuristic-class finding is never overturned, however recurrent."""
    finding = AuditFinding(
        kind="contradicted_claim",
        statement="Repeated target #4242 attempted again.",
        cites=[],
        rule="repeated_target_attempt",
    )
    history = [_audit_result(i, [finding]) for i in range(1, 6)]  # 5 cycles
    refuted = "[audit refuted] contradicted_claim: target #4242 is fine"
    rows = audit_checks.measure_refutation_rejections(
        history, [refuted], threshold=3,
    )
    assert rows == []


def test_measure_refutation_rejections_derived_class_stands() -> None:
    """A refuted derived-measure-class finding is never overturned."""
    finding = AuditFinding(
        kind="contradicted_claim",
        statement="Success claim vs measured rate for #4242.",
        cites=[],
        rule="success_claim_vs_rate",
    )
    history = [_audit_result(i, [finding]) for i in range(1, 6)]
    refuted = "[audit refuted] contradicted_claim: the rate is fine #4242"
    rows = audit_checks.measure_refutation_rejections(
        history, [refuted], threshold=3,
    )
    assert rows == []


def test_measure_refutation_rejections_lapsed_run_resets() -> None:
    """A non-consecutive run (a gap mid-tail) does not accumulate to threshold.

    History newest-last is [match, match, MISS, match, match]; walking
    newest→oldest the two newest match (count 2) then a gap stops the walk, so
    the two earliest do not count. 2 < threshold 3 → no row.
    """
    finding = _mech_pr_finding()
    gap = AuditFinding(
        kind="unconfirmed_landing",
        statement="Own PR #99 is not an ancestor of origin/main.",
        cites=[],
        rule="unconfirmed_own_landing",
    )
    history = [
        _audit_result(1, [finding]),
        _audit_result(2, [finding]),
        _audit_result(3, [gap]),      # gap for #4242 mid-tail
        _audit_result(4, [finding]),
        _audit_result(5, [finding]),
    ]
    rows = audit_checks.measure_refutation_rejections(
        history, [_refuted_pr()], threshold=3,
    )
    assert rows == []


def test_measure_refutation_rejections_unmatched_target() -> None:
    """A refuted entry naming a different target does not match the key."""
    finding = _mech_pr_finding(4242)
    history = [_audit_result(i, [finding]) for i in range(1, 4)]
    rows = audit_checks.measure_refutation_rejections(
        history, [_refuted_pr(999)], threshold=3,
    )
    assert rows == []


def test_measure_refutation_rejections_requires_refuted_prefix() -> None:
    """A plain ``[audit]`` entry (not refuted) never triggers a restore."""
    finding = _mech_pr_finding()
    history = [_audit_result(i, [finding]) for i in range(1, 4)]
    rows = audit_checks.measure_refutation_rejections(
        history, [_open_question(finding)], threshold=3,
    )
    assert rows == []


def test_measure_refutation_rejections_sha_only_undercredited_landing() -> None:
    """A SHA-only ``undercredited_landing`` target (no #NNNN) now produces a key.

    ``measure_reality_refutation`` keys on PR number and produces nothing for
    this finding; the new sensor mines the committed SHA from the statement, so
    the refuted entry naming that SHA is overturned.
    """
    finding = AuditFinding(
        kind="undercredited_landing",
        statement="My commit committed ab12cd3 is on origin/main; stamp success.",
        cites=[],
        rule="undercredited_landing",
    )
    history = [_audit_result(i, [finding]) for i in range(1, 4)]
    refuted = "[audit refuted] undercredited_landing: ab12cd3 did not land"
    rows = audit_checks.measure_refutation_rejections(
        history, [refuted], threshold=3,
    )
    assert len(rows) == 1
    assert rows[0].rule == "undercredited_landing"
    assert rows[0].target == "ab12cd3"
    assert rows[0].restored_question == _open_question(finding)


def test_measure_refutation_rejections_cites_target_orphaned_cure() -> None:
    """``orphaned_cure``'s plan-id (in cites + statement) is a matchable target."""
    finding = AuditFinding(
        kind="unmerged_work",
        statement="Cure plan 'plan-abc' has been implemented but unmerged.",
        cites=["plan-abc"],
        rule="orphaned_cure",
    )
    history = [_audit_result(i, [finding]) for i in range(1, 4)]
    refuted = "[audit refuted] unmerged_work: plan 'plan-abc' was retired already"
    rows = audit_checks.measure_refutation_rejections(
        history, [refuted], threshold=3,
    )
    assert {row.target for row in rows} == {"plan-abc"}


def test_measure_refutation_rejections_targetless_finding() -> None:
    """A mechanical-fact finding carrying no extractable target yields nothing.

    Exercises the ``all_keys`` early-out: a statement with no ``#NNNN`` and no
    committed SHA, and no cites, produces no ``(rule, target)`` key at all.
    """
    finding = AuditFinding(
        kind="unconfirmed_landing",
        statement="no identifiers of any kind in this statement",
        cites=[],
        rule="unconfirmed_own_landing",
    )
    history = [_audit_result(i, [finding]) for i in range(1, 4)]
    rows = audit_checks.measure_refutation_rejections(
        history, [_refuted_pr()], threshold=3,
    )
    assert rows == []


def test_measure_refutation_rejections_empty_history() -> None:
    """No audit cycles or no open questions → empty list."""
    assert audit_checks.measure_refutation_rejections(
        [], [_refuted_pr()], threshold=3,
    ) == []
    history = [_audit_result(i, [_mech_pr_finding()]) for i in range(1, 4)]
    assert audit_checks.measure_refutation_rejections(
        history, [], threshold=3,
    ) == []


# ---------------------------------------------------------------------------
# format_reality_refutation_summary — additive rejections clause (129 Phase C)
# ---------------------------------------------------------------------------


def test_format_reality_refutation_summary_rejections_zero_byte_identical() -> None:
    """rejections=0 keeps the Phase 2 line unchanged (no overturned clause)."""
    counts = {("unconfirmed_own_landing", 4242): 3}
    oq = ["[audit refuted] unconfirmed_own_landing: PR #4242 is resolved"]
    with_default = format_reality_refutation_summary(counts, oq, threshold=3)
    explicit_zero = format_reality_refutation_summary(
        counts, oq, threshold=3, rejections=0,
    )
    assert with_default == explicit_zero
    assert "mechanically overturned" not in with_default


def test_format_reality_refutation_summary_rejections_only_line() -> None:
    """A rejection-only cycle (no reality-refutation counts) still emits a line."""
    line = format_reality_refutation_summary({}, [], threshold=3, rejections=2)
    assert line.startswith("REALITY-REFUTATION:")
    assert "2 refutations mechanically overturned" in line
    # The count is a cumulative measure_gate_triggers read, not a single-cycle
    # tally, so the clause must not claim "this cycle".
    assert "this cycle" not in line


def test_format_reality_refutation_summary_rejections_singular() -> None:
    """A single rejection uses the singular noun."""
    line = format_reality_refutation_summary({}, [], threshold=3, rejections=1)
    assert "1 refutation mechanically overturned" in line


def test_format_reality_refutation_summary_both_conjuncts() -> None:
    """Both the reality-refutation line and the overturned clause appear."""
    counts = {("unconfirmed_own_landing", 4242): 3}
    oq = ["[audit refuted] unconfirmed_own_landing: PR #4242 is resolved"]
    line = format_reality_refutation_summary(
        counts, oq, threshold=3, rejections=2,
    )
    assert "unconfirmed_own_landing #4242" in line
    assert "2 refutations mechanically overturned" in line


# ---------------------------------------------------------------------------
# measure_undefined_coinages / format_undefined_coinage_summary (proposal 101)
# ---------------------------------------------------------------------------


def test_measure_undefined_coinages_counts_and_applies_floor() -> None:
    """Occurrences sum across texts; a term below the floor is dropped."""
    texts = [
        "merge-truth merge-truth flux-capacitor",
        "flux-capacitor flux-capacitor rare-term",
    ]

    counts = measure_undefined_coinages(
        texts, lexicon_terms=frozenset(), min_frequency=2,
    )

    # flux-capacitor=3, merge-truth=2 clear the floor; rare-term=1 does not.
    assert counts == {"merge-truth": 2, "flux-capacitor": 3}


def test_measure_undefined_coinages_subtracts_defined_terms_case_insensitively() -> None:
    """A coinage the lexicon defines self-extinguishes, even as an upper variant."""
    texts = ["settled-relap settled-relap settled-relap flux-capacitor flux-capacitor"]

    counts = measure_undefined_coinages(
        texts, lexicon_terms=frozenset({"SETTLED-RELAP"}), min_frequency=2,
    )

    # settled-relap clears the floor but is defined (matched case-insensitively).
    assert counts == {"flux-capacitor": 2}


def test_measure_undefined_coinages_empty_texts_returns_empty() -> None:
    assert measure_undefined_coinages([], lexicon_terms=frozenset()) == {}


def test_measure_undefined_coinages_non_positive_floor_returns_empty() -> None:
    """A floor below 1 is a disabled sensor, not a match-everything sensor."""
    counts = measure_undefined_coinages(
        ["merge-truth merge-truth"], lexicon_terms=frozenset(), min_frequency=0,
    )

    assert counts == {}


def test_format_undefined_coinage_summary_names_top_terms_by_frequency() -> None:
    """The line names undefined coinages, highest-frequency first."""
    line = format_undefined_coinage_summary({"merge-truth": 5, "flux-capacitor": 9})

    assert line.startswith("UNDEFINED-COINAGE:")
    assert "flux-capacitor (9)" in line
    assert "merge-truth (5)" in line
    assert line.index("flux-capacitor") < line.index("merge-truth")


def test_format_undefined_coinage_summary_breaks_ties_alphabetically_and_caps() -> None:
    """Equal-frequency terms sort alphabetically; max_terms caps the list."""
    counts = {"beta-term": 4, "alpha-term": 4, "gamma-term": 4}

    line = format_undefined_coinage_summary(counts, max_terms=2)

    assert "alpha-term (4)" in line
    assert "beta-term (4)" in line
    assert "gamma-term" not in line


def test_format_undefined_coinage_summary_empty_counts_returns_empty() -> None:
    assert format_undefined_coinage_summary({}) == ""


def test_format_undefined_coinage_summary_non_positive_cap_returns_empty() -> None:
    assert format_undefined_coinage_summary({"merge-truth": 5}, max_terms=0) == ""


def test_known_state_summary_includes_undefined_coinage() -> None:
    """The UNDEFINED-COINAGE line surfaces through format_known_state_summary."""
    line = format_undefined_coinage_summary({"flux-capacitor": 9})
    summary = format_known_state_summary(
        {}, [], undefined_coinage_context=line,
    )

    assert "UNDEFINED-COINAGE:" in summary
    assert "flux-capacitor (9)" in summary
    assert "## Already Known State" in summary


class TestCheckUndefinedCoinageDirective:
    """Tests for :func:`check_undefined_coinage_directive` (proposal 101)."""

    def test_non_empty_counts_names_top_term(self) -> None:
        """A non-empty dict yields a bounded directive naming the top term."""
        result = check_undefined_coinage_directive(
            {"merge-truth": 5, "flux-capacitor": 41},
        )
        assert result is not None
        # Highest-count term is named, with its count.
        assert "flux-capacitor" in result
        assert "41" in result
        assert "docs/lexicon.md" in result
        assert len(result) <= _DIRECTIVE_MAX_CHARS

    def test_empty_counts_returns_none(self) -> None:
        """An empty dict steers toward nothing, so the bridge stays silent."""
        assert check_undefined_coinage_directive({}) is None

    def test_picks_highest_count_with_alphabetical_tie_break(self) -> None:
        """Equal-frequency terms resolve to the alphabetically-first term,
        matching ``format_undefined_coinage_summary``'s ordering."""
        result = check_undefined_coinage_directive(
            {"gamma-term": 7, "alpha-term": 7, "beta-term": 7},
        )
        assert result is not None
        assert "alpha-term" in result
        assert "beta-term" not in result
        assert "gamma-term" not in result

    def test_long_term_truncated_to_directive_max_chars(self) -> None:
        """A pathologically long term caps the directive at _DIRECTIVE_MAX_CHARS."""
        result = check_undefined_coinage_directive({"x" * 300: 9})
        assert result is not None
        assert len(result) == _DIRECTIVE_MAX_CHARS
        assert result.endswith("…")

    def test_respects_directive_max_chars_constant(self) -> None:
        """The function must use _DIRECTIVE_MAX_CHARS, not a hardcoded literal.

        Patching the constant down to 100 must shorten the cap; a hardcoded
        200 would leave the result longer than 100.
        """
        import maestro.audit_checks as ac

        with patch.object(ac, "_DIRECTIVE_MAX_CHARS", 100):
            result = check_undefined_coinage_directive({"y" * 300: 9})
        assert result is not None
        assert len(result) <= 100, (
            f"Expected directive capped at _DIRECTIVE_MAX_CHARS (100), "
            f"but got length {len(result)} — function hardcodes 200."
        )


# ---------------------------------------------------------------------------
# truncate_directive — the shared whole-directive truncation helper
# ---------------------------------------------------------------------------


def test_truncate_directive_under_cap_returns_unchanged() -> None:
    """A directive shorter than the cap is returned verbatim."""
    text = "Review output of task t-1: ship the sensor"

    assert truncate_directive(text) == text


def test_truncate_directive_at_cap_returns_unchanged() -> None:
    """A directive exactly at the cap is not truncated (boundary)."""
    text = "x" * _DIRECTIVE_MAX_CHARS

    result = truncate_directive(text)

    assert result == text
    assert len(result) == _DIRECTIVE_MAX_CHARS


def test_truncate_directive_over_cap_ellipsises() -> None:
    """An over-cap directive is clipped to exactly the cap, ending in an ellipsis."""
    result = truncate_directive("y" * (_DIRECTIVE_MAX_CHARS + 50))

    assert len(result) == _DIRECTIVE_MAX_CHARS
    assert result.endswith("…")
    assert result[:-1] == "y" * (_DIRECTIVE_MAX_CHARS - 1)


def test_truncate_directive_reads_cap_at_call_time() -> None:
    """The cap is read from the module global per call, not snapshotted.

    This is what makes the constant a single point of change: raising
    ``_DIRECTIVE_MAX_CHARS`` must widen every bridge that routes through
    this helper, with no default-arg copy left pinned to the old value.
    """
    with patch.object(audit_checks, "_DIRECTIVE_MAX_CHARS", 150):
        result = truncate_directive("z" * 400)

    assert len(result) == 150
    assert result.endswith("…")


# ---------------------------------------------------------------------------
# format_known_state_summary — investigation-rate integration
# ---------------------------------------------------------------------------


def test_known_state_summary_includes_investigation_rate() -> None:
    """When investigation_rate_context is provided, it appears in the output."""
    ctx = format_investigation_rate_context(investigation_count=3, window=10)
    result = format_known_state_summary(
        {"gate": True}, ["goal1"],
        investigation_rate_context=ctx,
    )
    assert "Investigation rate" in result
    assert "3/10" in result
    assert "## Already Known State" in result


def test_known_state_summary_omits_rate_when_empty() -> None:
    """When investigation_rate_context is empty, no rate line appears."""
    result = format_known_state_summary({"gate": True}, ["goal1"])
    assert "Investigation rate" not in result


def test_known_state_summary_rate_only() -> None:
    """investigation_rate_context alone produces non-empty output."""
    ctx = format_investigation_rate_context(investigation_count=2, window=8)
    result = format_known_state_summary({}, [], investigation_rate_context=ctx)
    assert "## Already Known State" in result
    assert "2/8" in result


def test_known_state_summary_pr_churn_context_appended() -> None:
    """A non-empty pr_churn_context surfaces in the Already Known State block."""
    churn = "PR-READY #7: every code-clearable blocker is clear"
    result = format_known_state_summary({}, [], pr_churn_context=churn)
    assert "## Already Known State" in result
    assert churn in result


def test_known_state_summary_pr_churn_context_empty_suppressed() -> None:
    """An empty pr_churn_context contributes nothing on its own."""
    assert format_known_state_summary({}, [], pr_churn_context="") == ""


def test_known_state_summary_standdown_production_context_appended() -> None:
    """A non-empty standdown_production_context surfaces in the block (#2865)."""
    ctx = "STAND-DOWN-PRODUCTION: 0/3 — chosen-stand-down verdicts on the ledger"
    result = format_known_state_summary(
        {}, [], standdown_production_context=ctx,
    )
    assert "## Already Known State" in result
    assert ctx in result


def test_known_state_summary_standdown_production_context_empty_suppressed() -> None:
    """An empty standdown_production_context contributes nothing on its own."""
    assert (
        format_known_state_summary({}, [], standdown_production_context="") == ""
    )


def test_known_state_summary_all_empty_still_returns_empty() -> None:
    """All empty inputs still return empty string."""
    assert format_known_state_summary({}, [], investigation_rate_context="") == ""


# ---------------------------------------------------------------------------
# Directive-compliance measurement: measure_directive_compliance /
# format_directive_compliance_summary
# ---------------------------------------------------------------------------


def _write_gate_activations(
    mem_fs: dict[str, str],
    records: list[dict[str, str]],
) -> None:
    """Helper: write directive gate-activation records to mem_fs."""
    import json as json_mod

    path = str(FAKE_DIR / "gate_activations.jsonl")
    mem_fs[path] = "\n".join(json_mod.dumps(r) for r in records) + "\n"


# ---------------------------------------------------------------------------
# format_known_state_summary — data sufficiency context
# ---------------------------------------------------------------------------


def test_known_state_summary_includes_data_sufficiency() -> None:
    """When data_sufficiency_context is provided, it appears in the output."""
    result = format_known_state_summary(
        {"gate": True}, ["goal1"],
        data_sufficiency_context="Data sufficiency:\n  - alpha: 15/10 records — MET",
    )
    assert "Data sufficiency:" in result
    assert "alpha: 15/10 records" in result


def test_known_state_summary_omits_data_sufficiency_when_empty() -> None:
    """When data_sufficiency_context is empty, no sufficiency line appears."""
    result = format_known_state_summary({"gate": True}, ["goal1"])
    assert "Data sufficiency" not in result


def test_known_state_summary_data_sufficiency_only() -> None:
    """data_sufficiency_context alone produces non-empty output."""
    result = format_known_state_summary(
        {}, [],
        data_sufficiency_context="Data sufficiency:\n  - beta: 2/10 records — UNMET",
    )
    assert "## Already Known State" in result
    assert "UNMET" in result


def test_known_state_all_empty_with_data_sufficiency_returns_empty() -> None:
    """All empty inputs including data_sufficiency_context still return empty."""
    assert format_known_state_summary(
        {}, [],
        investigation_rate_context="",
        prediction_accuracy_context="",
        data_sufficiency_context="",
    ) == ""


def _curation_plan(
    plan_id: str = "plan-1",
    *,
    status: str = "pending_review",
    created_at: str = "2025-01-01T00:00:00Z",
    refined_at: str | None = None,
    last_curated_at: str | None = None,
    reviewer_feedback: list[Any] | None = None,
) -> EvolutionPlan:
    """Build an EvolutionPlan with the fields select_curation_target reads."""
    return EvolutionPlan(
        plan_id=plan_id,
        title="Test Plan",
        category="capability",
        rationale="because",
        proposed_changes=["change one"],
        files_affected=["maestro/agent.py"],
        expected_impact="none",
        status=status,
        created_at=created_at,
        refined_at=refined_at,
        last_curated_at=last_curated_at,
        reviewer_feedback=reviewer_feedback or [],
    )


def test_select_curation_target_prefers_oldest_unrefined() -> None:
    plans = [
        _curation_plan("new", created_at="2025-03-01T00:00:00Z"),
        _curation_plan("old", created_at="2025-01-01T00:00:00Z"),
        _curation_plan("mid", created_at="2025-02-01T00:00:00Z"),
    ]
    target = select_curation_target(plans)
    assert target is not None
    assert target.plan_id == "old"


def test_select_curation_target_never_curated_sorts_before_curated() -> None:
    """Round-robin cursor (#2885): among unrefined plans, one already stamped
    with ``last_curated_at`` sorts *after* a never-curated plan even when the
    curated one is older — so a kept plan yields its slot to the backlog."""
    plans = [
        _curation_plan(
            "old-curated",
            created_at="2025-01-01T00:00:00Z",
            last_curated_at="2025-06-01T00:00:00Z",
        ),
        _curation_plan("newer-never-curated", created_at="2025-03-01T00:00:00Z"),
    ]
    target = select_curation_target(plans)
    assert target is not None
    assert target.plan_id == "newer-never-curated"


def test_select_curation_target_least_recently_curated_first() -> None:
    """Round-robin cursor (#2885): among curated-but-unrefined plans the
    least-recently-curated one is serviced first, so the cursor cycles the
    whole backlog rather than fixating on any single plan."""
    plans = [
        _curation_plan(
            "recent",
            created_at="2025-01-01T00:00:00Z",
            last_curated_at="2025-06-10T00:00:00Z",
        ),
        _curation_plan(
            "stale",
            created_at="2025-02-01T00:00:00Z",
            last_curated_at="2025-06-01T00:00:00Z",
        ),
    ]
    target = select_curation_target(plans)
    assert target is not None
    assert target.plan_id == "stale"


def test_select_curation_target_prefers_unrefined_over_older_refined() -> None:
    plans = [
        _curation_plan(
            "old-refined",
            created_at="2025-01-01T00:00:00Z",
            refined_at="2025-04-01T00:00:00Z",
        ),
        _curation_plan("new-unrefined", created_at="2025-03-01T00:00:00Z"),
    ]
    target = select_curation_target(plans)
    assert target is not None
    assert target.plan_id == "new-unrefined"


def test_select_curation_target_falls_back_to_oldest_refined() -> None:
    plans = [
        _curation_plan(
            "newer",
            created_at="2025-03-01T00:00:00Z",
            refined_at="2025-04-01T00:00:00Z",
        ),
        _curation_plan(
            "older",
            created_at="2025-01-01T00:00:00Z",
            refined_at="2025-02-01T00:00:00Z",
        ),
    ]
    target = select_curation_target(plans)
    assert target is not None
    assert target.plan_id == "older"


def test_select_curation_target_tie_break_on_plan_id() -> None:
    plans = [
        _curation_plan("b", created_at="2025-01-01T00:00:00Z"),
        _curation_plan("a", created_at="2025-01-01T00:00:00Z"),
    ]
    target = select_curation_target(plans)
    assert target is not None
    assert target.plan_id == "a"


def test_select_curation_target_none_when_no_pending() -> None:
    plans = [
        _curation_plan("p1", status="approved"),
        _curation_plan("p2", status="merged"),
        _curation_plan("p3", status="obsolete"),
    ]
    assert select_curation_target(plans) is None


def test_select_curation_target_empty_list() -> None:
    assert select_curation_target([]) is None


def test_select_curation_target_skips_non_pending_statuses() -> None:
    plans = [
        _curation_plan(
            "approved-old", status="approved", created_at="2024-01-01T00:00:00Z"
        ),
        _curation_plan(
            "pending", status="pending_review", created_at="2025-06-01T00:00:00Z"
        ),
        _curation_plan(
            "merged-old", status="merged", created_at="2024-02-01T00:00:00Z"
        ),
    ]
    target = select_curation_target(plans)
    assert target is not None
    assert target.plan_id == "pending"
    assert target.status == "pending_review"


def test_select_curation_target_excludes_plan_younger_than_min_age() -> None:
    """A plan whose age is below ``min_age_hours`` is dropped before the sort,
    so a freshly-minted plan is not eligible for curation on the next tick."""
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    young = _curation_plan(
        "young", created_at=(now - timedelta(hours=1)).isoformat()
    )
    assert select_curation_target([young], now=now, min_age_hours=24.0) is None


def test_select_curation_target_selects_plan_once_aged_past_min_age() -> None:
    """The same plan, once older than ``min_age_hours``, becomes eligible."""
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    aged = _curation_plan(
        "aged", created_at=(now - timedelta(hours=48)).isoformat()
    )
    target = select_curation_target([aged], now=now, min_age_hours=24.0)
    assert target is not None
    assert target.plan_id == "aged"


def test_select_curation_target_default_min_age_selects_just_created() -> None:
    """With the defaults (``now=None``, ``min_age_hours=0.0``) the gate is
    inert: a just-created plan is selected exactly as before the grace period."""
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    fresh = _curation_plan("fresh", created_at=now.isoformat())
    target = select_curation_target([fresh])
    assert target is not None
    assert target.plan_id == "fresh"


def test_select_curation_target_min_age_inert_without_now() -> None:
    """A positive ``min_age_hours`` with ``now=None`` leaves the gate inert —
    both operands are required before any plan is dropped."""
    fresh = _curation_plan("fresh", created_at="2025-06-01T12:00:00Z")
    target = select_curation_target([fresh], min_age_hours=24.0)
    assert target is not None
    assert target.plan_id == "fresh"


def test_select_curation_target_excludes_unparseable_created_at() -> None:
    """An empty or unparseable ``created_at`` is treated as not-yet-eligible
    and excluded when a positive ``min_age_hours`` gate is active."""
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    garbage = _curation_plan("garbage", created_at="not-a-timestamp")
    empty = _curation_plan("empty", created_at="")
    assert (
        select_curation_target([garbage], now=now, min_age_hours=24.0) is None
    )
    assert select_curation_target([empty], now=now, min_age_hours=24.0) is None


def test_select_curation_target_age_gate_filters_within_backlog() -> None:
    """Among a mixed backlog, only plans aged past the floor survive the gate;
    the too-young one is dropped and the aged one is selected."""
    now = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
    young = _curation_plan(
        "young", created_at=(now - timedelta(hours=2)).isoformat()
    )
    aged = _curation_plan(
        "aged", created_at=(now - timedelta(hours=72)).isoformat()
    )
    target = select_curation_target(
        [young, aged], now=now, min_age_hours=24.0
    )
    assert target is not None
    assert target.plan_id == "aged"


def _unaddressed_feedback() -> list[Any]:
    from maestro.plan_feedback import PlanFeedback

    return [
        PlanFeedback(
            feedback_id="f1",
            text="please sharpen",
            submitted_at="2025-06-01T00:00:00+00:00",
            content_hash_seen="dead",
        )
    ]


def test_select_curation_skip_serviced_returns_none_all_serviced() -> None:
    """Flag on: every pending plan serviced (refined OR curated), none with
    unaddressed feedback, yields None (criterion 1)."""
    plans = [
        _curation_plan("refined", refined_at="2025-06-01T00:00:00Z"),
        _curation_plan("kept", last_curated_at="2025-06-01T00:00:00Z"),
    ]
    assert (
        select_curation_target(
            plans, loop_curate_skip_serviced_enabled=True
        )
        is None
    )


def test_select_curation_skip_serviced_returns_never_serviced() -> None:
    """Flag on: a never-serviced plan is still returned (criterion 2)."""
    plans = [
        _curation_plan("kept", last_curated_at="2025-06-01T00:00:00Z"),
        _curation_plan("fresh"),
    ]
    target = select_curation_target(
        plans, loop_curate_skip_serviced_enabled=True
    )
    assert target is not None
    assert target.plan_id == "fresh"


def test_select_curation_skip_serviced_excludes_kept() -> None:
    """Flag on: a kept plan (last_curated_at set, refined_at None, no
    unaddressed feedback) is NOT re-selected (criterion 3)."""
    plans = [_curation_plan("kept", last_curated_at="2025-06-01T00:00:00Z")]
    assert (
        select_curation_target(
            plans, loop_curate_skip_serviced_enabled=True
        )
        is None
    )


def test_select_curation_skip_serviced_feedback_lane_unchanged() -> None:
    """Flag on: a refined plan carrying unaddressed feedback is still returned
    via the prioritize_feedback lane (criterion 4)."""
    plan = _curation_plan(
        "refined-fb",
        refined_at="2025-06-01T00:00:00Z",
        last_curated_at="2025-06-01T00:00:00Z",
        reviewer_feedback=_unaddressed_feedback(),
    )
    target = select_curation_target(
        [plan],
        prioritize_feedback=True,
        loop_curate_skip_serviced_enabled=True,
    )
    assert target is not None
    assert target.plan_id == "refined-fb"


def test_select_curation_skip_serviced_default_off_round_robin() -> None:
    """Flag off (default): a kept plan is still re-selected when no
    never-serviced peer exists — byte-for-byte the round-robin selector
    (criterion 5)."""
    plans = [_curation_plan("kept", last_curated_at="2025-06-01T00:00:00Z")]
    target = select_curation_target(plans)
    assert target is not None
    assert target.plan_id == "kept"
    # Explicit false matches the implicit default.
    target_explicit = select_curation_target(
        plans, loop_curate_skip_serviced_enabled=False
    )
    assert target_explicit is not None
    assert target_explicit.plan_id == "kept"


# ---------------------------------------------------------------------------
# Rule 9: evolution_impact_review
# ---------------------------------------------------------------------------

_IMPACT_NOW = datetime(2026, 6, 6, tzinfo=UTC)
_IMPACT_SIDECAR = str(FAKE_DIR / "impact_review_surfaced.json")


def _version(
    version_id: str = "v1",
    merged_at: str = "2026-05-01T00:00:00+00:00",
    commit_sha: str = "abcdef1234567890",
    quality_gate_pass: bool = True,
    plan_title: str = "Some evolution",
) -> VersionRecord:
    return VersionRecord(
        version_id=version_id,
        plan_id="ep1",
        plan_title=plan_title,
        commit_sha=commit_sha,
        merged_at=merged_at,
        window_tasks_total=0,
        window_tasks_succeeded=0,
        window_tasks_failed=0,
        window_sdk_cost_usd=0.0,
        window_sdk_calls=0,
        window_cache_hit_rate=0.0,
        window_rate_limit_hits=0,
        window_error_count=0,
        quality_gate_pass=quality_gate_pass,
    )


def _summary_task(
    task_id: str,
    result_summary: str = "",
    key_findings: str | None = None,
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        goal="goal",
        plan=[PlanStep(step_id="s1", description="x", status="done")],
        outcome="success",  # type: ignore[arg-type]
        result_summary=result_summary,
        errors=[],
        duration_secs=0.5,
        timestamp="2026-06-01T00:00:00Z",
        key_findings=key_findings,
    )


def test_impact_review_no_versions_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """(a) No version records → no findings, no sidecar write."""
    findings = check_evolution_impact_reviews(
        [], [], JsonImpactReviewSurfacedStore(FAKE_DIR), now=_IMPACT_NOW,
    )
    assert findings == []
    assert _IMPACT_SIDECAR not in mem_fs


def test_impact_review_young_version_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """(b) A version merged within the age window is not yet review-due."""
    young = _version(merged_at="2026-06-05T00:00:00+00:00")
    findings = check_evolution_impact_reviews(
        [young], [], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, now=_IMPACT_NOW,
    )
    assert findings == []


def test_impact_review_sha_matched_task_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """(c) An overdue version already revisited by a recent task (its
    commit-SHA prefix appears in a task summary) is suppressed."""
    old = _version(commit_sha="abcdef1234567890")
    revisiting = _summary_task(
        "t1", result_summary="Re-validated evolution abcdef12 criteria",
    )
    findings = check_evolution_impact_reviews(
        [old], [revisiting], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, now=_IMPACT_NOW,
    )
    assert findings == []


def test_impact_review_sha_matched_via_key_findings(
    mem_fs: dict[str, str],
) -> None:
    """(c, variant) The SHA prefix may live in ``key_findings`` too."""
    old = _version(commit_sha="abcdef1234567890")
    revisiting = _summary_task(
        "t1", key_findings="checked commit abcdef12 still holds",
    )
    findings = check_evolution_impact_reviews(
        [old], [revisiting], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, now=_IMPACT_NOW,
    )
    assert findings == []


def test_impact_review_overdue_unrevisited_returns_finding(
    mem_fs: dict[str, str],
) -> None:
    """(d) An overdue version with no revisiting task surfaces a finding
    and is appended to the surfaced-set sidecar."""
    old = _version(version_id="v_old", plan_title="Old feature")
    unrelated = _summary_task("t1", result_summary="something else")
    findings = check_evolution_impact_reviews(
        [old], [unrelated], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, now=_IMPACT_NOW,
    )
    assert len(findings) == 1
    assert findings[0].kind == "unreviewed_evolution"
    assert findings[0].rule == "evolution_impact_review"
    assert findings[0].cites == ["v_old"]
    assert "v_old" in findings[0].statement
    assert "Old feature" in findings[0].statement
    assert _IMPACT_SIDECAR not in mem_fs


def test_impact_review_reports_failed_gate_status(
    mem_fs: dict[str, str],
) -> None:
    """A version whose merge-CI did not pass is flagged as such."""
    old = _version(version_id="v_fail", quality_gate_pass=False)
    findings = check_evolution_impact_reviews(
        [old], [], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, now=_IMPACT_NOW,
    )
    assert len(findings) == 1
    assert "did NOT pass" in findings[0].statement


def test_impact_review_already_surfaced_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """(e) A version already in the sidecar is not re-raised."""
    mem_fs[_IMPACT_SIDECAR] = json.dumps(["v_old"])
    old = _version(version_id="v_old")
    findings = check_evolution_impact_reviews(
        [old], [], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, now=_IMPACT_NOW,
    )
    assert findings == []


def test_impact_review_disabled_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """(f) ``enabled=False`` short-circuits before any I/O."""
    old = _version(version_id="v_old")
    findings = check_evolution_impact_reviews(
        [old], [], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, enabled=False, now=_IMPACT_NOW,
    )
    assert findings == []
    assert _IMPACT_SIDECAR not in mem_fs


def test_impact_review_dedup_across_two_calls(
    mem_fs: dict[str, str],
) -> None:
    """Re-raised until the caller records it (#4099), then suppressed."""
    old = _version(version_id="v_old")
    store = JsonImpactReviewSurfacedStore(FAKE_DIR)
    for _ in range(2):
        first = check_evolution_impact_reviews([old], [], store, max_age_days=7, now=_IMPACT_NOW)
        assert len(first) == 1
    store.record(first[0].cites)
    assert check_evolution_impact_reviews([old], [], store, max_age_days=7, now=_IMPACT_NOW) == []


def test_impact_review_persists_through_shared_backend(
    mem_fs: dict[str, str],
) -> None:
    """The surfaced set persists through a single backend instance across calls.

    Passing one :class:`JsonImpactReviewSurfacedStore` to two calls proves the
    dedup rides the injected backend (not a re-read of the raw file): the version
    surfaces once, the backend records it (``load`` sees it), and the second call
    is suppressed."""
    backend = JsonImpactReviewSurfacedStore(FAKE_DIR)
    old = _version(version_id="v_shared")
    first = check_evolution_impact_reviews(
        [old], [], backend, max_age_days=7, now=_IMPACT_NOW,
    )
    assert len(first) == 1
    assert backend.load() == set()
    backend.record(first[0].cites)
    assert backend.load() == {"v_shared"}
    second = check_evolution_impact_reviews(
        [old], [], backend, max_age_days=7, now=_IMPACT_NOW,
    )
    assert second == []


def test_impact_review_window_limits_scan(
    mem_fs: dict[str, str],
) -> None:
    """Only the last ``review_window`` records are scanned."""
    versions = [
        _version(version_id=f"v{i}", commit_sha=f"sha{i:05d}aa")
        for i in range(5)
    ]
    findings = check_evolution_impact_reviews(
        versions, [], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, review_window=2,
        now=_IMPACT_NOW,
    )
    surfaced = {f.cites[0] for f in findings}
    assert surfaced == {"v3", "v4"}


def test_impact_review_zero_window_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    """A non-positive review window scans nothing."""
    findings = check_evolution_impact_reviews(
        [_version()], [], JsonImpactReviewSurfacedStore(FAKE_DIR), review_window=0, now=_IMPACT_NOW,
    )
    assert findings == []


def test_impact_review_unparseable_timestamp_skipped(
    mem_fs: dict[str, str],
) -> None:
    """A version with a garbage ``merged_at`` is skipped, not crashed on."""
    bad = _version(version_id="v_bad", merged_at="not-a-timestamp")
    findings = check_evolution_impact_reviews(
        [bad], [], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, now=_IMPACT_NOW,
    )
    assert findings == []


def test_impact_review_corrupt_sidecar_recovers(
    mem_fs: dict[str, str],
) -> None:
    """A corrupt sidecar is treated as empty so the rule still fires."""
    mem_fs[_IMPACT_SIDECAR] = "{not json"
    old = _version(version_id="v_old")
    findings = check_evolution_impact_reviews(
        [old], [], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, now=_IMPACT_NOW,
    )
    assert len(findings) == 1
    JsonImpactReviewSurfacedStore(FAKE_DIR).record(findings[0].cites)
    assert json.loads(mem_fs[_IMPACT_SIDECAR]) == ["v_old"]


def test_impact_review_caps_findings_per_rule(
    mem_fs: dict[str, str],
) -> None:
    """At most ``MAX_FINDINGS_PER_RULE`` versions surface per call; the
    remainder stay unsurfaced for a later cycle."""
    versions = [
        _version(version_id=f"v{i}", commit_sha=f"unique{i:04d}")
        for i in range(MAX_FINDINGS_PER_RULE + 2)
    ]
    findings = check_evolution_impact_reviews(
        versions, [], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7,
        review_window=len(versions), now=_IMPACT_NOW,
    )
    assert len(findings) == MAX_FINDINGS_PER_RULE
    assert _IMPACT_SIDECAR not in mem_fs


def test_impact_review_non_list_sidecar_treated_as_empty(
    mem_fs: dict[str, str],
) -> None:
    """A sidecar holding valid JSON that is not a list is treated as
    empty, so the rule still fires."""
    mem_fs[_IMPACT_SIDECAR] = json.dumps({"unexpected": "object"})
    old = _version(version_id="v_old")
    findings = check_evolution_impact_reviews(
        [old], [], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, now=_IMPACT_NOW,
    )
    assert len(findings) == 1
    JsonImpactReviewSurfacedStore(FAKE_DIR).record(findings[0].cites)
    assert json.loads(mem_fs[_IMPACT_SIDECAR]) == ["v_old"]


def test_impact_review_naive_timestamp_coerced_to_utc(
    mem_fs: dict[str, str],
) -> None:
    """A timezone-naive ``merged_at`` is coerced to UTC, so an old naive
    timestamp is still recognised as overdue."""
    old = _version(version_id="v_naive", merged_at="2026-05-01T00:00:00")
    findings = check_evolution_impact_reviews(
        [old], [], JsonImpactReviewSurfacedStore(FAKE_DIR), max_age_days=7, now=_IMPACT_NOW,
    )
    assert len(findings) == 1
    assert findings[0].cites == ["v_naive"]


def test_parse_iso_utc_handles_empty_and_naive() -> None:
    """The ISO helper returns None for empty input and UTC-coerces naive
    timestamps."""
    assert audit_checks._parse_iso_utc("") is None
    assert audit_checks._parse_iso_utc(None) is None
    naive = audit_checks._parse_iso_utc("2026-05-01T00:00:00")
    assert naive is not None
    assert naive.tzinfo is UTC


def _impl_plan(
    plan_id: str = "ep1",
    status: str = "implemented",
    title: str = "Add the cure",
) -> EvolutionPlan:
    """An evolution plan in *status* (default ``implemented``)."""
    return EvolutionPlan(
        plan_id=plan_id,
        title=title,
        category="capability",
        rationale="r",
        proposed_changes=["x"],
        files_affected=["maestro/x.py"],
        expected_impact="none",
        status=status,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Rule 20: check_undercredited_landing (non-success task whose own SHA landed)
# ---------------------------------------------------------------------------

# A committed SHA carrying hex letters so it matches SELF_COMMITTED_SHA_RE
# (a bare decimal run is rejected as a SHA candidate).
_LANDED_SHA = "ac48a769"


def _committed_sha_task(
    task_id: str,
    *,
    outcome: str = "partial",
    summary: str | None = None,
) -> TaskRecord:
    """A non-success task whose summary cites an own-committed SHA."""
    if summary is None:
        summary = (
            f"Landed the 2-parent merge; committed {_LANDED_SHA} — "
            f"origin/main now a true ancestor."
        )
    return TaskRecord(
        task_id=task_id,
        goal="Land the blocker PR",
        plan=[PlanStep(step_id="s1", description="x", status="done")],
        outcome=outcome,  # type: ignore[arg-type]
        result_summary=summary,
        errors=[],
        duration_secs=0.5,
        timestamp="2026-07-15T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# Proposal 155 step 6 — skip unchanged curation
# ---------------------------------------------------------------------------


def _feedback_entry(fid: str, submitted_at: str) -> Any:
    from maestro.plan_feedback import PlanFeedback

    return PlanFeedback(
        feedback_id=fid, text="narrow it", submitted_at=submitted_at,
        addressed_at=None,
    )


def _stamped(plan: EvolutionPlan) -> EvolutionPlan:
    from maestro.memory import compute_curation_fingerprint

    plan.curated_hash = compute_curation_fingerprint(plan)
    return plan


def test_select_curation_target_skips_unchanged_plan() -> None:
    plans = [
        _stamped(_curation_plan("a", created_at="2025-01-01T00:00:00Z")),
        _curation_plan("b", created_at="2025-02-01T00:00:00Z"),
    ]
    target = select_curation_target(plans)
    assert target is not None
    assert target.plan_id == "b"


def test_select_curation_target_all_unchanged_returns_none() -> None:
    plans = [_stamped(_curation_plan("a")), _stamped(_curation_plan("b"))]
    assert select_curation_target(plans) is None


def test_select_curation_target_stale_stamp_on_content_edit_is_eligible() -> None:
    plan = _stamped(_curation_plan("a"))
    plan.rationale = "edited"
    target = select_curation_target([plan])
    assert target is not None
    assert target.plan_id == "a"


def test_select_curation_target_stale_stamp_on_new_feedback_is_eligible() -> None:
    plan = _stamped(_curation_plan("a"))
    plan.reviewer_feedback = [_feedback_entry("fb-new", "2025-01-02T00:00:00Z")]
    plan.reviewer_feedback[0].addressed_at = "2025-01-03T00:00:00Z"
    target = select_curation_target([plan])
    assert target is not None
    assert target.plan_id == "a"


def test_select_curation_target_none_stamp_is_eligible() -> None:
    target = select_curation_target([_curation_plan("a")])
    assert target is not None
    assert target.plan_id == "a"


def test_select_curation_target_feedback_lane_exempt_from_skip() -> None:
    plan = _curation_plan(
        "a", reviewer_feedback=[_feedback_entry("fb-1", "2025-01-02T00:00:00Z")],
    )
    _stamped(plan)
    target = select_curation_target([plan], prioritize_feedback=True)
    assert target is not None
    assert target.plan_id == "a"


def test_select_curation_target_skip_composes_with_age_and_serviced() -> None:
    now = datetime(2025, 3, 1, tzinfo=UTC)
    plans = [
        _stamped(_curation_plan("unchanged", created_at="2025-01-01T00:00:00Z")),
        _curation_plan("young", created_at="2025-02-28T23:00:00Z"),
        _curation_plan(
            "serviced", created_at="2025-01-02T00:00:00Z",
            last_curated_at="2025-01-05T00:00:00Z",
        ),
        _curation_plan("fresh", created_at="2025-01-03T00:00:00Z"),
    ]
    target = select_curation_target(
        plans, now=now, min_age_hours=24.0,
        loop_curate_skip_serviced_enabled=True,
    )
    assert target is not None
    assert target.plan_id == "fresh"

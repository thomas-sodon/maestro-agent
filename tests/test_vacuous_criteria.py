"""Adversarial test suite for the vacuous-criteria validator.

Validates that ``EvolutionEngine.validate_success_criteria`` achieves
>= 90% true-positive rate on vacuous inputs and <= 5% false-positive rate
on legitimate inputs, including edge cases (unicode, mixed case,
punctuation-heavy).
"""

from __future__ import annotations

import logging

import pytest

from maestro.evolution import EvolutionEngine

# ---------------------------------------------------------------------------
# Vacuous criteria — must be rejected (>= 90% true-positive rate)
# ---------------------------------------------------------------------------

_VACUOUS: list[str] = [
    # Too short (< 25 chars)
    "improve performance",
    "fix the bug",
    "handle errors",
    "make it better",
    "works correctly now",
    "add proper tests",
    "code quality improved",
    # Filler-dominated
    "it should be properly handled and working well with the system",
    "the implementation should be improved to handle things correctly",
    "this should work properly and ensure it is handled well",
    "it must be ensured that the system is working properly and correctly",
    "the code should be working and handling everything appropriately",
    "ensure it is properly improved and working well for the system",
    "it should ensure that things are handled correctly by the system",
    "the tests should ensure that everything is properly working",
    # Generic-noun-only (non-filler tokens are all generic nouns)
    "the code and the tests and the system should handle the errors",
    "the implementation and functionality of the module handles the output",
    "the test code for the methods and functions processes the data results",
    "the system output and the process data should be the result of changes",
    "tests for the module functions and methods handle errors and problems",
    "the code quality and performance of the implementation and functionality",
    "system behavior and test results of the module implementation",
]

# ---------------------------------------------------------------------------
# Legitimate criteria — must survive (<= 5% false-positive rate)
# ---------------------------------------------------------------------------

_LEGITIMATE: list[str] = [
    "task_cost_gate rule 3 counts both `failure` and `partial` outcomes in the failure-cost numerator",
    "run_quality_gates returns non-zero exit code when ruff reports E302",
    "BM25 scorer ranks exact-match documents above partial-match by a factor of 2x",
    "_CRITERIA_MIN_CHARS constant equals 25 in evolution.py",
    "propose_plan filters criteria shorter than 25 characters before building EvolutionPlan",
    "audit_checks.lineage_keys walks the chain capped at 10 hops across rewordings",
    "EvolutionPlan.acceptance_criteria is capped to 10 items after vacuous filtering",
    "dream_every iteration counter triggers context compression into DreamDigest",
    "stream_idle_timeout_seconds raises STREAM_STALL after expiry in asyncio.wait_for",
    "rejection_history.jsonl records one RejectionCategory row per closed PR within 7-day cooldown",
    "The filler-word ratio threshold is 0.40 and tokens are matched case-insensitively",
    "UNICODE: règle numéro 3 rejects proposals with acceptance_criteria containing only fillers",
]


class TestVacuousCriteriaAdversarial:
    """Aggregate true-positive and false-positive rates."""

    def test_vacuous_rejection_rate(self) -> None:
        """At least 90% of vacuous criteria must be rejected."""
        results = EvolutionEngine.validate_success_criteria(_VACUOUS)
        survived = len(results)
        rejected = len(_VACUOUS) - survived
        rate = rejected / len(_VACUOUS)
        assert rate >= 0.90, (
            f"vacuous rejection rate {rate:.0%} < 90%; "
            f"survivors: {results}"
        )

    def test_legitimate_survival_rate(self) -> None:
        """At most 5% of legitimate criteria may be falsely rejected."""
        results = EvolutionEngine.validate_success_criteria(_LEGITIMATE)
        false_positives = len(_LEGITIMATE) - len(results)
        fp_rate = false_positives / len(_LEGITIMATE)
        assert fp_rate <= 0.05, (
            f"false-positive rate {fp_rate:.0%} > 5%; "
            f"rejected: {set(_LEGITIMATE) - set(results)}"
        )


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestVacuousCriteriaEdgeCases:
    """Individual edge-case coverage."""

    def test_empty_list(self) -> None:
        assert EvolutionEngine.validate_success_criteria([]) == []

    def test_whitespace_only(self) -> None:
        assert EvolutionEngine.validate_success_criteria(["   ", "\t\n"]) == []

    def test_mixed_case_filler(self) -> None:
        """Filler detection is case-insensitive."""
        vacuous = [
            "It SHOULD Be Properly Handled And Working Well With The System",
        ]
        assert EvolutionEngine.validate_success_criteria(vacuous) == []

    def test_unicode_legitimate(self) -> None:
        """Non-ASCII characters don't cause false positives."""
        criteria = [
            "règle numéro 3 rejects proposals with acceptance_criteria containing only fillers",
        ]
        result = EvolutionEngine.validate_success_criteria(criteria)
        assert len(result) == 1

    def test_punctuation_heavy_legitimate(self) -> None:
        """Heavy punctuation doesn't cause false positives on specific content."""
        criteria = [
            "`validate_success_criteria()` returns [] when all inputs are < 25 chars (length gate)",
        ]
        result = EvolutionEngine.validate_success_criteria(criteria)
        assert len(result) == 1

    def test_punctuation_heavy_vacuous(self) -> None:
        """Punctuation can't rescue an otherwise vacuous criterion."""
        criteria = [
            "!!!the system... should... be... working... properly!!!",
        ]
        assert EvolutionEngine.validate_success_criteria(criteria) == []

    def test_numbers_in_specific_criteria(self) -> None:
        """Numeric tokens (stripped by alpha regex) don't cause false rejects."""
        criteria = [
            "cost_gate rejects plans when per-iteration cost exceeds $0.50 threshold (50 cents)",
        ]
        result = EvolutionEngine.validate_success_criteria(criteria)
        assert len(result) == 1

    def test_exactly_at_min_chars(self) -> None:
        """Criterion at exactly _CRITERIA_MIN_CHARS boundary passes length gate."""
        # 25 chars, specific content with a verifiable marker ("path")
        criterion = "path BM25 exact match ~ok"
        assert len(criterion) == 25
        result = EvolutionEngine.validate_success_criteria([criterion])
        assert len(result) == 1

    def test_one_below_min_chars(self) -> None:
        """Criterion at _CRITERIA_MIN_CHARS - 1 fails length gate."""
        criterion = "BM25 ranks exact match x"  # 24 chars
        assert len(criterion) == 24
        result = EvolutionEngine.validate_success_criteria([criterion])
        assert len(result) == 0

    def test_filler_exactly_at_boundary_passes(self) -> None:
        """Exactly 40% filler survives — the `>` comparison admits the boundary."""
        # 10 tokens, 4 filler (the, on, a, with) = 40% exactly; specific non-filler
        # tokens (ruff, linter, gate, strict, e, checks) keep heuristic 3 from firing.
        criterion = "the ruff linter gate on a strict E302 with checks"
        result = EvolutionEngine.validate_success_criteria([criterion])
        assert len(result) == 1

    def test_filler_just_over_boundary_rejected(self) -> None:
        """Just over 40% filler is rejected by the filler-ratio gate."""
        # 10 tokens, 5 filler (the, on, a, with, by) = 50%; non-filler tokens are
        # specific, so the rejection is the filler-ratio gate, not heuristic 3.
        criterion = "the ruff on a gate with E302 by strict checks"
        result = EvolutionEngine.validate_success_criteria([criterion])
        assert len(result) == 0

    def test_mixed_vacuous_and_specific(self) -> None:
        """Only specific criteria survive a mixed batch."""
        criteria = [
            "fix bugs",
            "the system should work properly and handle everything correctly",
            "cost_gate rule 3 counts failure and partial outcomes in the numerator",
            "improve quality",
        ]
        result = EvolutionEngine.validate_success_criteria(criteria)
        assert len(result) == 1
        assert "cost_gate" in result[0]

    def test_no_alpha_tokens_rejected(self) -> None:
        """Criterion with >= 25 chars but zero alpha tokens is rejected."""
        criterion = "12345 67890 --- !!! ??? ..."
        assert len(criterion) >= 25
        result = EvolutionEngine.validate_success_criteria([criterion])
        assert len(result) == 0

    def test_all_generic_nouns_without_filler(self) -> None:
        """Criterion with only generic nouns (no filler) is still rejected."""
        # "code tests system errors results data" would be too short (< 25),
        # so use a longer version composed entirely of generic nouns
        longer = "code tests system errors results data implementation quality output"
        result = EvolutionEngine.validate_success_criteria([longer])
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Drop telemetry — visibility into vacuous filtering
# ---------------------------------------------------------------------------


class TestVacuousCriteriaTelemetry:
    """``validate_success_criteria`` logs when it drops criteria."""

    def test_total_filter_out_warns(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """All-vacuous non-empty input emits one WARNING naming the rejects."""
        criteria = ["fix the bug", "improve performance"]
        with caplog.at_level(logging.INFO, logger="maestro"):
            result = EvolutionEngine.validate_success_criteria(criteria)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "fix the bug" in message
        assert "improve performance" in message

    def test_partial_drop_logs_info(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Mixed input emits exactly one INFO naming the dropped count."""
        criteria = [
            "fix the bug",
            "run_quality_gates returns non-zero exit code when ruff reports E302",
        ]
        with caplog.at_level(logging.INFO, logger="maestro"):
            result = EvolutionEngine.validate_success_criteria(criteria)
        assert len(result) == 1
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1
        assert "dropped 1 of 2" in infos[0].getMessage()

    def test_all_survive_no_log(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """All-survive input emits no telemetry from this path."""
        criteria = [
            "run_quality_gates returns non-zero exit code when ruff reports E302",
            "_CRITERIA_MIN_CHARS constant equals 25 in evolution.py",
        ]
        with caplog.at_level(logging.INFO, logger="maestro"):
            result = EvolutionEngine.validate_success_criteria(criteria)
        assert len(result) == 2
        emitted = [
            r
            for r in caplog.records
            if "validate_success_criteria" in r.getMessage()
        ]
        assert emitted == []

    def test_empty_input_no_warning(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Whitespace-only input returns [] without a WARNING."""
        criteria = ["   ", "\t\n"]
        with caplog.at_level(logging.INFO, logger="maestro"):
            result = EvolutionEngine.validate_success_criteria(criteria)
        assert result == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []

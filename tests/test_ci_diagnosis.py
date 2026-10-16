"""Tests for the CI-failure diagnosis oracle (proposal 102).

:mod:`maestro.ci_diagnosis` turns a failing check's log text into a bounded,
concrete first-error excerpt. It is pure given its injected ``read_log`` seam,
so these tests inject a stub callable and never touch real I/O, per the Testing
Principles. They pin: the first-error extraction (matched markers, blank/no-match
drops, the line and count caps), the seam being called exactly once (and not at
all for an empty failing-check list), and the artifact-section renderer.
"""

from __future__ import annotations

from maestro.ci_diagnosis import (
    _MAX_ERROR_LINES,
    _MAX_LINE_CHARS,
    FAILING_CONCLUSIONS,
    SUCCESS_CONCLUSIONS,
    CheckRun,
    CIFailure,
    classify_ci_conclusions,
    diagnose_ci_failure,
    extract_first_errors,
    failing_check_names,
    is_failing_conclusion,
    is_success_conclusion,
    render_ci_failure_section,
    rollup_item_name,
)

# ── CI classification vocabulary + predicates (the single owner) ────────────


def test_failing_success_vocabularies_are_lowercase_and_disjoint() -> None:
    """The canonical vocabularies are lower-cased and share no member."""
    assert {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
        "error",
    } == FAILING_CONCLUSIONS
    assert {"success", "neutral", "skipped"} == SUCCESS_CONCLUSIONS
    assert FAILING_CONCLUSIONS.isdisjoint(SUCCESS_CONCLUSIONS)
    # All canonical, so the case-normalising predicates are the only entry point.
    assert all(token == token.lower() for token in FAILING_CONCLUSIONS)
    assert all(token == token.lower() for token in SUCCESS_CONCLUSIONS)


def test_is_failing_conclusion_normalises_case_and_whitespace() -> None:
    """Uppercase rollup ``state`` and lower-cased ``conclusion`` resolve alike."""
    for value in ("FAILURE", "failure", "  Startup_Failure ", "TIMED_OUT"):
        assert is_failing_conclusion(value) is True
    for value in ("success", "SUCCESS", "in_progress", "queued", ""):
        assert is_failing_conclusion(value) is False


def test_is_success_conclusion_normalises_case_and_whitespace() -> None:
    """Success / neutral / skipped read as concluded-good; failures do not."""
    for value in ("SUCCESS", "success", " Neutral ", "SKIPPED"):
        assert is_success_conclusion(value) is True
    for value in ("failure", "STARTUP_FAILURE", "in_progress", ""):
        assert is_success_conclusion(value) is False


def test_rollup_item_name_reads_name_then_context_then_empty() -> None:
    """``name`` wins; a legacy status context is the fallback; else ``""``."""
    assert rollup_item_name({"name": "quality", "context": "legacy"}) == "quality"
    assert rollup_item_name({"context": "legacy-status"}) == "legacy-status"
    assert rollup_item_name({"name": "", "context": ""}) == ""
    assert rollup_item_name({}) == ""


# ── relocated check-run classifiers, exercised from the owner ───────────────


def test_classify_ci_conclusions_reachable_from_owner() -> None:
    """The relocated classifier reads the step-1 vocabulary from its new home.

    ``red_root`` re-exports these, but this pins them working directly from
    ``ci_diagnosis`` — the single owner — across the three verdicts.
    """
    failing = [
        CheckRun(name="tests", conclusion="success"),
        CheckRun(name="deploy", conclusion="startup_failure"),
    ]
    assert classify_ci_conclusions(failing) == "FAILING"
    assert failing_check_names(failing) == ("deploy",)

    green = [CheckRun(name="tests", conclusion="SUCCESS")]
    assert classify_ci_conclusions(green) == "GREEN"

    running = [CheckRun(name="tests", conclusion="in_progress")]
    assert classify_ci_conclusions(running) == "RUNNING"
    assert failing_check_names(running) == ()


# ── extract_first_errors: pure line-scan ───────────────────────────────────


def test_extract_first_errors_matches_pytest_failed_and_assertion() -> None:
    """A pytest ``FAILED`` summary and its ``E`` assertion line are extracted."""
    log = (
        "quality\tRun tests\t2026-07-26 collecting ...\n"
        "quality\tRun tests\t2026-07-26 FAILED tests/test_foo.py::test_bar\n"
        "quality\tRun tests\t2026-07-26 E   AssertionError: assert 1 == 2\n"
        "quality\tRun tests\t2026-07-26 1 failed in 0.4s\n"
    )
    assert extract_first_errors(log) == (
        "quality\tRun tests\t2026-07-26 FAILED tests/test_foo.py::test_bar",
        "quality\tRun tests\t2026-07-26 E   AssertionError: assert 1 == 2",
    )


def test_extract_first_errors_matches_mypy_error_diagnostic() -> None:
    """A mypy ``error:`` diagnostic line is extracted; noise is dropped."""
    log = (
        "types\tRun mypy\tSuccess: no issues in 3 files\n"
        "types\tRun mypy\tmaestro/x.py:9: error: Incompatible return value\n"
    )
    assert extract_first_errors(log) == (
        "types\tRun mypy\tmaestro/x.py:9: error: Incompatible return value",
    )


def test_extract_first_errors_matches_capitalized_step_error() -> None:
    """A GitHub step's capitalized ``Error:`` line is matched (ledger r2).

    ``error:`` is matched case-insensitively so a non-pytest/mypy step whose only
    signal is ``Error: Process completed with exit code 1`` still yields a
    concrete line; the surrounding noise is dropped.
    """
    log = (
        "quality\tRun step\tsome step output here\n"
        "quality\tRun step\tError: Process completed with exit code 1\n"
    )
    assert extract_first_errors(log) == (
        "quality\tRun step\tError: Process completed with exit code 1",
    )


def test_extract_first_errors_no_match_returns_empty() -> None:
    """A log with no recognisable error line yields ``()``."""
    assert extract_first_errors("all green\npassed\n\n   \n") == ()


def test_extract_first_errors_empty_log_returns_empty() -> None:
    """An empty log yields ``()`` (the unreadable-log path)."""
    assert extract_first_errors("") == ()


def test_extract_first_errors_does_not_false_match_midword_e() -> None:
    """A mid-word ``E`` followed by spaces (``THE   value``) is not a match."""
    assert extract_first_errors("THE   value is fine\nall good\n") == ()


def test_extract_first_errors_caps_line_count() -> None:
    """No more than ``_MAX_ERROR_LINES`` lines are kept, in order."""
    log = "\n".join(f"FAILED tests/test_{i}.py::t" for i in range(20))
    result = extract_first_errors(log)
    assert len(result) == _MAX_ERROR_LINES
    assert result[0] == "FAILED tests/test_0.py::t"
    assert result[-1] == f"FAILED tests/test_{_MAX_ERROR_LINES - 1}.py::t"


def test_extract_first_errors_truncates_long_line() -> None:
    """Each kept line is truncated to ``_MAX_LINE_CHARS``."""
    long_line = "error: " + "x" * (_MAX_LINE_CHARS * 2)
    result = extract_first_errors(long_line)
    assert len(result) == 1
    assert len(result[0]) == _MAX_LINE_CHARS


# ── diagnose_ci_failure: injected read_log seam ────────────────────────────


def test_diagnose_ci_failure_empty_checks_returns_empty_no_read() -> None:
    """No failing checks → ``()`` and the log seam is never called."""
    calls = 0

    def _read() -> str:
        nonlocal calls
        calls += 1
        return "FAILED x"

    assert diagnose_ci_failure((), _read) == ()
    assert calls == 0


def test_diagnose_ci_failure_pairs_lines_with_each_check() -> None:
    """Each failing check gets one CIFailure carrying the same excerpt; read once."""
    calls = 0

    def _read() -> str:
        nonlocal calls
        calls += 1
        return "FAILED tests/test_a.py::t\nE   AssertionError: nope\n"

    failures = diagnose_ci_failure(("quality", "review-quality"), _read)
    assert calls == 1
    assert failures == (
        CIFailure(
            check_name="quality",
            first_error_lines=(
                "FAILED tests/test_a.py::t",
                "E   AssertionError: nope",
            ),
        ),
        CIFailure(
            check_name="review-quality",
            first_error_lines=(
                "FAILED tests/test_a.py::t",
                "E   AssertionError: nope",
            ),
        ),
    )


def test_diagnose_ci_failure_unnamed_check_still_reads_log() -> None:
    """The unnamed-FAILING shape (``("",)``) yields one empty-named CIFailure."""
    failures = diagnose_ci_failure(("",), lambda: "error: boom\n")
    assert failures == (
        CIFailure(check_name="", first_error_lines=("error: boom",)),
    )


def test_diagnose_ci_failure_unreadable_log_gives_empty_lines() -> None:
    """A ``""`` log (fetch miss) still pairs a lineless CIFailure per check."""
    failures = diagnose_ci_failure(("quality",), lambda: "")
    assert failures == (
        CIFailure(check_name="quality", first_error_lines=()),
    )


# ── render_ci_failure_section: artifact block ──────────────────────────────


def test_render_ci_failure_section_empty_is_blank() -> None:
    """No failures → ``""`` so a caller stays byte-identical on the empty path."""
    assert render_ci_failure_section(()) == ""


def test_render_ci_failure_section_renders_checks_and_lines() -> None:
    """A failure renders a heading, a per-check bullet, and indented error lines."""
    section = render_ci_failure_section((
        CIFailure(
            check_name="quality",
            first_error_lines=("FAILED tests/test_a.py::t", "E   nope"),
        ),
    ))
    assert section == (
        "## CI Failure\n"
        "- quality:\n"
        "  - FAILED tests/test_a.py::t\n"
        "  - E   nope\n"
    )


def test_render_ci_failure_section_unnamed_and_lineless() -> None:
    """An empty check name renders ``(unnamed check)``; no lines is noted."""
    section = render_ci_failure_section((
        CIFailure(check_name="", first_error_lines=()),
    ))
    assert section == (
        "## CI Failure\n"
        "- (unnamed check):\n"
        "  - (no concrete error line extracted)\n"
    )

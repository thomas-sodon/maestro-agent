"""Unit tests for the local gate-observation owner (proposal 124, phase A).

The owner is pure and I/O-free — :func:`parse_gate_output` reads *injected*
captured streams — so these build runner-output strings in memory and assert the
enumerated failures plus their provenance (``kind`` / ``location`` /
``identifier`` / ``message``) and the resulting signatures. No filesystem,
network, or subprocess is touched; no pytest fixtures and no ``mem_fs`` are
needed, mirroring ``tests/test_plan_grounding.py``.

The fixture strings are authored from the real output formats: no captured pytest
collection-error or coverage-threshold text exists anywhere in the suite to copy
from, and the mypy block reproduces the shape mypy emits under the flags
``run_quality_gates`` actually passes. ``_RUFF_FULL_DIAGNOSTICS`` is a *captured*
ruff 0.15 block — structure, indentation and all — because ruff's default output
format changed in 0.9 and an authored-from-memory fixture is exactly how the
concise-only parser this replaced stayed green against output it could not read.
"""

from __future__ import annotations

import dataclasses

import pytest

from maestro.gate_convergence import plan_next_round
from maestro.gate_environment import GateRunnabilityVerdict
from maestro.gate_observation import (
    _MAX_FAILURES,
    _MAX_MESSAGE_CHARS,
    _MAX_OUTPUT_CHARS,
    GateFailure,
    GateObservation,
    GateRunObservation,
    _split_pytest_node_and_message,
    attribute,
    is_pytest_timeout_line,
    parse_gate_output,
    parse_non_answer,
    render_failure_report,
    render_identities,
    truncated_by_first_failure,
)

# ── Captured-shape output constants ────────────────────────────────────────────

# pytest's short-test-summary block for a four-failure run: one plain failure, one
# parametrized failure with *no* trailing message (pytest omits the ``- msg`` half
# when the failure has no one-line representation), one class-nested failure, and
# one whose message carries the ``-`` separator inside its own text.
_PYTEST_FOUR_FAILURES = (
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_alpha.py::test_one - ValueError: bad value\n"
    "FAILED tests/test_alpha.py::test_two[case-3]\n"
    "FAILED tests/test_beta.py::TestThing::test_three - KeyError: 'missing'\n"
    "FAILED tests/test_gamma.py::test_four - TypeError: a - b unsupported\n"
    "========================= 4 failed, 118 passed in 8.31s ========================\n"
)

# A collection abort: pytest could not import the module, so no test ran. The
# banner line and the ``Interrupted:`` tail must not register as failures.
_PYTEST_COLLECTION_ERROR = (
    "==================================== ERRORS ====================================\n"
    "_____________________ ERROR collecting tests/test_broken.py ____________________\n"
    "ImportError while importing test module '/repo/tests/test_broken.py'.\n"
    "=========================== short test summary info ============================\n"
    "ERROR tests/test_broken.py - ImportError: cannot import name 'gone'\n"
    "!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
)

# A ``pytest -x --tb=short -q`` report whose failing test logged at ERROR level.
# ``--tb=short`` prints a ``Captured log call`` section, whose records pytest pads to
# ``%(levelname)-8s`` — so ``ERROR    pkg.mod:mod.py:12 …`` and any captured stdout
# line beginning with ``ERROR ``/``FAILED `` satisfy the summary-row pattern by
# construction. Only the one row under the real banner is a failure identity.
_PYTEST_CAPTURED_LOG_ERROR = (
    "=================================== FAILURES ===================================\n"
    "___________________________ test_logs_then_fails ____________________________\n"
    "tests/test_probe.py:18: in test_logs_then_fails\n"
    "    assert probe() == 1\n"
    "E   AssertionError: assert 0 == 1\n"
    "------------------------------ Captured log call -------------------------------\n"
    "ERROR    pkg.mod:mod.py:12 boom while probing plan 7f3a\n"
    "WARNING  pkg.mod:mod.py:14 retrying\n"
    "----------------------------- Captured stdout call -----------------------------\n"
    "ERROR reconciling ledger row 4\n"
    "FAILED to reach the host\n"
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_probe.py::test_logs_then_fails - AssertionError: assert 0 == 1\n"
    "!!!!!!!!!!!!!!!!!!!!!!!!!! stopping after 1 failures !!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
)

# A killed runner's partial output: the report body arrived, the summary banner
# never did. The fallback whole-text scan is what salvages its rows.
_PYTEST_PARTIAL_NO_BANNER = (
    "=================================== FAILURES ===================================\n"
    "FAILED tests/test_alpha.py::test_one - ValueError: bad value\n"
    "------------------------------ Captured log call -------------------------------\n"
    "ERROR    pkg.mod:mod.py:12 boom while probing plan 7f3a\n"
)

# A ``-q`` run — what the gate actually invokes — whose run-summary line carries no
# ``=`` padding, followed by output that begins with ``ERROR ``/``FAILED ``. Only
# the row inside the section is a failure identity; the trailing lines are past the
# section's undecorated terminator.
_PYTEST_QUIET_SUMMARY_THEN_TRAILING_OUTPUT = (
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_alpha.py::test_one - ValueError: bad value\n"
    "1 failed, 118 passed, 2 warnings in 8.31s\n"
    "ERROR reconciling ledger row 4\n"
    "FAILED to reach the host\n"
)

# pytest-cov's threshold shortfall — the one red that emits no failure line at all.
_PYTEST_COVERAGE_SHORTFALL = (
    "1184 passed in 61.20s\n"
    "\n"
    "---------- coverage: platform linux, python 3.12.3-final-0 -----------\n"
    "TOTAL                                     18043    431    97%\n"
    "\n"
    "FAIL Required test coverage of 100% not reached. Total coverage: 97.61%\n"
)

# ruff's ``full`` format — the default since ruff 0.9 and therefore what the gate
# actually captures, since ``run_quality_gates`` passes no ``--output-format``.
# Each diagnostic spans a header line and an indented ``-->`` location line,
# followed by a source snippet and an optional ``help:`` hint; none of the snippet
# lines, the hint, or the two trailing summary lines may register as a failure.
# Captured from ruff 0.15 and re-pathed onto maestro-shaped files.
_RUFF_FULL_DIAGNOSTICS = (
    "E402 Module level import not at top of file\n"
    " --> maestro/foo.py:12:5\n"
    "   |\n"
    "10 | def thing() -> None:\n"
    "11 |     pass\n"
    "12 | import os\n"
    "   |     ^^^^^^^^^\n"
    "   |\n"
    "\n"
    "F401 [*] `os` imported but unused\n"
    " --> maestro/foo.py:3:1\n"
    "  |\n"
    "1 | import os\n"
    "  |        ^^\n"
    "2 | x = 1\n"
    "  |\n"
    "help: Remove unused import: `os`\n"
    "\n"
    "PLR0913 Too many arguments in function definition (7 > 5)\n"
    " --> tests/test_x.py:9:9\n"
    "  |\n"
    "9 | def wide(a, b, c, d, e, f, g):\n"
    "  |         ^^^^\n"
    "  |\n"
    "\n"
    "Found 3 errors.\n"
    "[*] 1 fixable with the `--fix` option.\n"
)

# A ``full``-format ruff *syntax* diagnostic — no rule code, same two-line shape.
_RUFF_FULL_SYNTAX = (
    "invalid-syntax: Expected a parameter or the end of the parameter list\n"
    " --> maestro/broken.py:1:7\n"
    "  |\n"
    "1 | def f(:\n"
    "  |       ^\n"
    "  |\n"
    "Found 1 error.\n"
)

# One ordinary diagnostic in a file the diff never touched — the baseline half
# of the mixed-output case.
_RUFF_INHERITED_F401 = (
    "F401 [*] `os` imported but unused\n"
    " --> maestro/old.py:1:8\n"
    "  |\n"
    "1 | import os\n"
    "  |        ^^\n"
    "  |\n"
    "Found 1 error.\n"
)

# The mixed run the bug was about: the inherited ``F401`` above plus a *new*
# syntax error, spelled as one ruff invocation really emits it — both diagnostics
# under a single ``Found 2 errors.`` summary, not two concatenated captures.
_RUFF_MIXED_INHERITED_AND_SYNTAX = (
    "F401 [*] `os` imported but unused\n"
    " --> maestro/old.py:1:8\n"
    "  |\n"
    "1 | import os\n"
    "  |        ^^\n"
    "  |\n"
    "help: Remove unused import: `os`\n"
    "\n"
    "invalid-syntax: Expected a parameter or the end of the parameter list\n"
    " --> maestro/broken.py:1:7\n"
    "  |\n"
    "1 | def f(:\n"
    "  |       ^\n"
    "  |\n"
    "\n"
    "Found 2 errors.\n"
    "[*] 1 fixable with the `--fix` option.\n"
)

# ruff's ``concise`` format — the pre-0.9 default, still emitted under an explicit
# ``output-format = "concise"``. Same three diagnostics as the block above, so the
# two formats' identities can be asserted equal.
_RUFF_CONCISE_DIAGNOSTICS = (
    "maestro/foo.py:12:5: E402 Module level import not at top of file\n"
    "maestro/foo.py:3:1: F401 [*] `os` imported but unused\n"
    "tests/test_x.py:9:9: PLR0913 Too many arguments in function definition (7 > 5)\n"
    "Found 3 errors.\n"
    "[*] 1 fixable with the `--fix` option.\n"
)

# A mypy error block with an interleaved ``note:`` line, a column-carrying error,
# an error with no bracketed code, and the ``Found N errors`` summary — all three
# non-error shapes must be skipped.
_MYPY_ERRORS = (
    'maestro/bar.py:42: error: Incompatible return value type (got "int", '
    'expected "str")  [return-value]\n'
    "maestro/bar.py:42: note: Perhaps you need to return a string here\n"
    'maestro/bar.py:88:9: error: Name "missing" is not defined  [name-defined]\n'
    "maestro/baz.py:7: error: Cannot determine type of anything\n"
    "Found 3 errors in 2 files (checked 143 source files)\n"
)


# ── pytest parsing ─────────────────────────────────────────────────────────────


def test_pytest_enumerates_every_failed_summary_line() -> None:
    """Four ``FAILED`` lines become four rows carrying their nodeids.

    This is the fact the pre-124 gate could not represent: with ``-x`` at most one
    failure was ever visible, so a fix round that cleared three of four was
    indistinguishable from one that changed nothing.
    """
    observation = parse_gate_output("tests", _PYTEST_FOUR_FAILURES, "", 1)

    assert observation.kind == "FAILED"
    assert len(observation.failures) == 4
    assert [f.identifier for f in observation.failures] == [
        "tests/test_alpha.py::test_one",
        "tests/test_alpha.py::test_two[case-3]",
        "tests/test_beta.py::TestThing::test_three",
        "tests/test_gamma.py::test_four",
    ]
    assert [f.kind for f in observation.failures] == ["FAILED"] * 4


def test_pytest_failure_rows_carry_path_and_message() -> None:
    """Each row's ``location`` is the bare file path and ``message`` the detail."""
    observation = parse_gate_output("tests", _PYTEST_FOUR_FAILURES, "", 1)
    first, second, _third, fourth = observation.failures

    assert first.location == "tests/test_alpha.py"
    assert first.message == "ValueError: bad value"
    # No ``- msg`` half printed: the message is empty, not the nodeid.
    assert second.location == "tests/test_alpha.py"
    assert second.message == ""
    # A ``-`` inside the message text is not re-split.
    assert fourth.message == "TypeError: a - b unsupported"


def test_pytest_signature_is_one_identity_per_failing_test() -> None:
    """The signature is failure-granular, so clearing three of four shrinks it."""
    all_four = parse_gate_output("tests", _PYTEST_FOUR_FAILURES, "", 1)
    one_left = parse_gate_output(
        "tests",
        "FAILED tests/test_gamma.py::test_four - TypeError: a - b unsupported\n",
        "",
        1,
    )

    assert len(all_four.signature()) == 4
    assert one_left.signature() == frozenset({"tests:tests/test_gamma.py::test_four"})
    assert one_left.signature() < all_four.signature()


def test_pytest_parametrized_id_containing_a_space_is_enumerated() -> None:
    """A string-parametrized nodeid keeps its spaces and must still parse.

    ``pytest.mark.parametrize("v", ["a b"])`` yields ``test_x[a b]``, so a
    non-space node pattern matches nothing on such a line and drops the row
    entirely — the failing set understated by exactly the failures this owner
    exists to enumerate. Captured verbatim from a real ``-q --tb=no`` run.
    """
    observation = parse_gate_output(
        "tests",
        "FAILED t/test_b.py::test_x[a b] - AssertionError: assert 'a b' == 'zzz'\n"
        "FAILED t/test_b.py::test_x[c d] - AssertionError: assert 'c d' == 'zzz'\n",
        "",
        1,
    )

    assert len(observation.failures) == 2
    assert [f.identifier for f in observation.failures] == [
        "t/test_b.py::test_x[a b]",
        "t/test_b.py::test_x[c d]",
    ]
    assert observation.failures[0].location == "t/test_b.py"
    assert observation.failures[0].message == "AssertionError: assert 'a b' == 'zzz'"


def test_pytest_parametrized_id_containing_the_separator_is_split_correctly() -> None:
    """A parametrized id carrying its own `` - `` splits at the *real* separator.

    ``pytest.mark.parametrize("v", ["a - b"])`` yields ``test_x[a - b]``, so a
    split at the first `` - `` cuts the nodeid in half: the identifier loses its
    tail and the message gains the rest of the id. The row survives either way and
    its identity is stable across rounds, so progress still measures — but the
    identifier a later increment intersects with a plan's changed paths, and the
    message a fixer reads, are both wrong. The split is bracket-aware for exactly
    this reason.
    """
    observation = parse_gate_output(
        "tests",
        "FAILED t/test_b.py::test_x[a - b] - ValueError: bad value\n"
        # Two brackets deep, and a separator inside each: the split must still
        # land on the one that closes the whole parametrization.
        "FAILED t/test_b.py::test_y[a - b][c - d] - KeyError: 'missing'\n",
        "",
        1,
    )

    assert [f.identifier for f in observation.failures] == [
        "t/test_b.py::test_x[a - b]",
        "t/test_b.py::test_y[a - b][c - d]",
    ]
    assert [f.message for f in observation.failures] == [
        "ValueError: bad value",
        "KeyError: 'missing'",
    ]
    assert observation.failures[0].location == "t/test_b.py"


def test_pytest_summary_row_with_no_separator_is_all_node() -> None:
    """A row pytest printed with no message half yields an empty message.

    The fallback when neither balance test accepts any candidate and the tail
    carries no surplus ``]`` — including a parametrized id whose own `` - `` is the
    only candidate, which must not be mistaken for the message join.
    """
    observation = parse_gate_output(
        "tests", "FAILED t/test_b.py::test_x[a - b]\n", "", 1,
    )

    row = observation.failures[0]
    assert row.identifier == "t/test_b.py::test_x[a - b]"
    assert row.message == ""


def test_pytest_cov_error_prefix_is_not_a_failure_row() -> None:
    """pytest-cov's ``ERROR: Coverage failure:`` line is not an ``ERROR <node>`` row.

    It is the only real output line that starts with a status word and is not a
    failure; the mandatory whitespace after the word is what excludes it, and the
    coverage red is enumerated from its own ``FAIL Required test coverage`` line.
    """
    observation = parse_gate_output(
        "tests",
        "ERROR: Coverage failure: total of 97 is less than fail-under=100\n"
        "FAIL Required test coverage of 100% not reached. Total coverage: 97.61%\n",
        "",
        1,
    )

    assert observation.kind == "COVERAGE"
    assert len(observation.failures) == 1
    assert observation.failures[0].identifier == "coverage"


def test_pytest_collection_error_is_its_own_kind() -> None:
    """An ``ERROR <path>`` collection abort is one ``COLLECTION_ERROR`` row.

    The abort is a different fact from a failing test: no test ran at all, so the
    row's identifier is the *path*, not a nodeid.
    """
    observation = parse_gate_output("tests", _PYTEST_COLLECTION_ERROR, "", 2)

    assert observation.kind == "COLLECTION_ERROR"
    assert len(observation.failures) == 1
    row = observation.failures[0]
    assert row.kind == "COLLECTION_ERROR"
    assert row.location == "tests/test_broken.py"
    assert row.identifier == "tests/test_broken.py"
    assert row.message == "ImportError: cannot import name 'gone'"
    assert observation.signature() == frozenset({"tests:tests/test_broken.py"})


def test_pytest_fixture_error_row_is_enumerated_as_collection_error() -> None:
    """A fixture setup/teardown ``ERROR`` row is enumerated under the same kind.

    pytest spells a fixture error's summary row exactly like a collection abort's
    — ``ERROR <node>`` — differing only in that the node is a full *nodeid*
    rather than a bare path, so this owner (which reads only the summary) gives
    both one kind. Pinned because the kind's name reads narrower than its domain:
    the test here *did* run, and the row must still parse, split its location back
    to the file, and land in the aborting kinds that gate the enumerate re-run.
    """
    observation = parse_gate_output(
        "tests",
        "ERROR tests/test_fix.py::test_uses_fixture - ValueError: fixture blew up\n",
        "",
        1,
    )

    assert observation.kind == "COLLECTION_ERROR"
    row = observation.failures[0]
    assert row.kind == "COLLECTION_ERROR"
    assert row.location == "tests/test_fix.py"
    assert row.identifier == "tests/test_fix.py::test_uses_fixture"
    assert row.message == "ValueError: fixture blew up"
    assert truncated_by_first_failure(observation) is True


def test_pytest_coverage_shortfall_is_its_own_kind() -> None:
    """A coverage-only red — which emits no ``FAILED`` line — is one ``COVERAGE`` row.

    Without an explicit alternative for pytest-cov's threshold line this red would
    parse as an empty failure set, the exact "unparsed red reads as green" hazard
    the signature contract exists to make impossible.
    """
    observation = parse_gate_output("tests", _PYTEST_COVERAGE_SHORTFALL, "", 1)

    assert observation.kind == "COVERAGE"
    assert len(observation.failures) == 1
    row = observation.failures[0]
    assert row.kind == "COVERAGE"
    # A threshold shortfall is a property of the run, not of any one file.
    assert row.location == ""
    assert row.identifier == "coverage"
    assert "97.61%" in row.message
    assert observation.signature() == frozenset({"tests:coverage"})


def test_captured_log_error_lines_are_not_enumerated_as_failures() -> None:
    """A ``--tb=short`` report's captured ``ERROR`` lines are not failure identities.

    The real gate runs ``pytest -x --tb=short -q``, which prints a ``Captured log
    call`` section for every failing test whose code path logged at WARNING or
    above. pytest pads the level name to ``%(levelname)-8s``, so a captured
    ``ERROR`` record — and any captured stdout line beginning with ``ERROR ``/
    ``FAILED `` — satisfies the summary-row pattern by construction. Enumerating
    them mints phantom identities that a volatile payload (a plan id, a counter)
    flips between rounds, reading as "stuck" to ``plan_next_round`` and withholding
    ``INHERITED`` from ``attribute()``. Only pytest's own summary section counts.
    """
    observation = parse_gate_output("tests", _PYTEST_CAPTURED_LOG_ERROR, "", 1)

    assert observation.kind == "FAILED"
    assert len(observation.failures) == 1
    row = observation.failures[0]
    assert row.kind == "FAILED"
    assert row.identifier == "tests/test_probe.py::test_logs_then_fails"
    assert row.location == "tests/test_probe.py"
    assert [f.kind for f in observation.failures] == ["FAILED"]
    assert len(observation.signature()) == 1


def test_pytest_banner_less_partial_output_still_salvages_its_rows() -> None:
    """A killed runner's banner-less partial output keeps its salvaged rows.

    ``parse_non_answer`` reuses this enumeration on whatever a runner killed at
    its timeout had already written, and that text can stop before pytest's
    ``short test summary info`` banner ever prints. With no section to scope to,
    the walk falls back to the whole text, so the salvage path keeps its rows
    rather than reading as a non-answer that named nothing.
    """
    observation = parse_non_answer("tests", "TIMEOUT", _PYTEST_PARTIAL_NO_BANNER, "")

    assert observation.kind == "TIMEOUT"
    identifiers = [f.identifier for f in observation.failures]
    assert identifiers == ["tests/test_alpha.py::test_one"]


def test_banner_less_salvage_still_excludes_captured_log_records() -> None:
    """The fallback scan closes at a captured-stream header, not only at a banner.

    A runner killed before the summary banner printed still emits a ``Captured log
    call`` section, so leaving every fallback line eligible would readmit exactly
    the phantom ``ERROR``-padded identities the section scope exists to exclude —
    on the one path (``parse_non_answer``) whose rows are already partial.
    """
    observation = parse_non_answer("tests", "TIMEOUT", _PYTEST_PARTIAL_NO_BANNER, "")

    assert [f.kind for f in observation.failures] == ["FAILED"]
    assert observation.signature() == frozenset(
        {"tests:tests/test_alpha.py::test_one", "tests:TIMEOUT"},
    )


def test_coverage_shortfall_matches_outside_the_summary_section() -> None:
    """pytest-cov's shortfall line is matched whether or not a banner precedes it.

    The threshold line is printed *outside* the summary section — after the run
    summary, and on a coverage-only red with no banner at all — so narrowing the
    summary-row scope must leave the coverage matcher unconditional. Pinned from
    both sides: banner-less, and after a real section's terminating line.
    """
    banner_less = parse_gate_output("tests", _PYTEST_COVERAGE_SHORTFALL, "", 1)
    assert banner_less.signature() == frozenset({"tests:coverage"})

    after_section = parse_gate_output(
        "tests", _PYTEST_FOUR_FAILURES + _PYTEST_COVERAGE_SHORTFALL, "", 1,
    )
    # Pinned as the exact set, so a section-close regression that readmits
    # post-summary rows is caught rather than hidden by a membership assertion.
    assert after_section.signature() == frozenset(
        {
            "tests:tests/test_alpha.py::test_one",
            "tests:tests/test_alpha.py::test_two[case-3]",
            "tests:tests/test_beta.py::TestThing::test_three",
            "tests:tests/test_gamma.py::test_four",
            "tests:coverage",
        },
    )

    quiet_section = parse_gate_output(
        "tests",
        _PYTEST_QUIET_SUMMARY_THEN_TRAILING_OUTPUT + _PYTEST_COVERAGE_SHORTFALL,
        "",
        1,
    )
    assert quiet_section.signature() == frozenset(
        {"tests:tests/test_alpha.py::test_one", "tests:coverage"},
    )


def test_quiet_run_summary_closes_the_section_without_equals_padding() -> None:
    """The ``-q`` run summary ends the section even though it carries no padding.

    ``run_quality_gates`` invokes pytest with ``-q``, which prints the run summary
    undecorated (``1 failed, 118 passed, 2 warnings in 8.31s``). A terminator that
    required the ``=`` runs would leave the section open to EOF on every real gate
    run, so anything printed afterwards that begins with ``ERROR ``/``FAILED `` —
    a trailing captured stream, a coverage block's neighbours — would re-enter the
    enumeration as a phantom identity.
    """
    observation = parse_gate_output(
        "tests", _PYTEST_QUIET_SUMMARY_THEN_TRAILING_OUTPUT, "", 1,
    )

    assert observation.kind == "FAILED"
    assert [f.identifier for f in observation.failures] == [
        "tests/test_alpha.py::test_one",
    ]


def test_pytest_failing_tests_outrank_a_coverage_shortfall() -> None:
    """A run that both failed tests and missed coverage is ``FAILED``.

    Worst-first precedence: the failing tests are the fact worth fixing, and the
    coverage row still contributes its own identity to the signature.
    """
    combined = _PYTEST_FOUR_FAILURES + _PYTEST_COVERAGE_SHORTFALL
    observation = parse_gate_output("tests", combined, "", 1)

    assert observation.kind == "FAILED"
    assert len(observation.failures) == 5
    assert "tests:coverage" in observation.signature()


# ── ruff parsing ───────────────────────────────────────────────────────────────


def test_ruff_enumerates_full_format_diagnostics_and_skips_the_rest() -> None:
    """Three ``full``-format diagnostics parse; nothing else in the block does.

    The regression the concise-only parser had: ruff has defaulted to this
    two-line shape since 0.9, so every real ruff red collapsed to the single
    synthetic ``ruff:FAILED`` identity this owner exists to replace. The snippet
    lines, the ``help:`` hint and the two summary lines must all stay unmatched.
    """
    observation = parse_gate_output("ruff", _RUFF_FULL_DIAGNOSTICS, "", 1)

    assert observation.kind == "FAILED"
    assert len(observation.failures) == 3
    assert [(f.location, f.identifier) for f in observation.failures] == [
        ("maestro/foo.py", "maestro/foo.py:12:5:E402"),
        ("maestro/foo.py", "maestro/foo.py:3:1:F401"),
        ("tests/test_x.py", "tests/test_x.py:9:9:PLR0913"),
    ]
    # The ``[*]`` fixable marker is consumed, not left on the message.
    assert observation.failures[1].message == "`os` imported but unused"


def test_ruff_concise_format_yields_the_same_identities() -> None:
    """The pre-0.9 one-line format parses to identities equal to the full one's.

    An operator who sets ``output-format = "concise"`` (or pins an older ruff)
    must not get a different signature for the same three diagnostics.
    """
    full = parse_gate_output("ruff", _RUFF_FULL_DIAGNOSTICS, "", 1)
    concise = parse_gate_output("ruff", _RUFF_CONCISE_DIAGNOSTICS, "", 1)

    assert len(concise.failures) == 3
    assert concise.signature() == full.signature()
    assert concise.failures[1].message == "`os` imported but unused"


def test_ruff_syntax_error_is_enumerated_with_a_path_qualified_identity() -> None:
    """A syntax error's ``invalid-syntax:`` header is a first-class failure.

    It carries no rule code, so the literal ``invalid-syntax`` token stands in for
    one; the gate stays red, but on the diagnostic's own path-qualified identity
    rather than the synthetic ``ruff:FAILED`` that named no file.
    """
    observation = parse_gate_output("ruff", _RUFF_FULL_SYNTAX, "", 1)

    assert observation.kind == "FAILED"
    assert [(f.location, f.identifier) for f in observation.failures] == [
        ("maestro/broken.py", "maestro/broken.py:1:7:invalid-syntax"),
    ]
    assert observation.signature() == frozenset(
        {"ruff:maestro/broken.py:1:7:invalid-syntax"},
    )
    assert observation.failures[0].message == (
        "Expected a parameter or the end of the parameter list"
    )
    assert observation.truncated is False


def test_ruff_mixed_syntax_and_rule_diagnostics_are_both_enumerated() -> None:
    """A syntax error alongside an ordinary diagnostic reaches the attributor.

    The bug: with a nonempty failure tuple no synthetic identity was added, so a
    run that mixed an inherited ``F401`` with a *new* syntax error had the same
    signature as the baseline — and ``attribute`` returned ``INHERITED`` for a red
    the diff had just caused.
    """
    baseline = _run(parse_gate_output("ruff", _RUFF_INHERITED_F401, "", 1))
    current = parse_gate_output("ruff", _RUFF_MIXED_INHERITED_AND_SYNTAX, "", 1)

    assert current.signature() == frozenset({
        "ruff:maestro/old.py:1:8:F401",
        "ruff:maestro/broken.py:1:7:invalid-syntax",
    })
    assert current.signature() != baseline.signature()
    assert attribute(_run(current), ["maestro/broken.py"], baseline) == "ATTRIBUTED"


def test_ruff_concise_syntax_yields_the_same_identity() -> None:
    """The ``concise`` spelling of a syntax error parses to the full one's identity.

    Both formats route through ``_ruff_failure``, so an operator who pins
    ``output-format = "concise"`` cannot get a different signature for the same
    broken file.
    """
    full = parse_gate_output("ruff", _RUFF_FULL_SYNTAX, "", 1)
    concise = parse_gate_output(
        "ruff",
        "maestro/broken.py:1:7: invalid-syntax: Expected a parameter or the end "
        "of the parameter list\n"
        "Found 1 error.\n",
        "",
        1,
    )

    assert len(concise.failures) == 1
    assert concise.signature() == full.signature()
    assert concise.failures[0].message == full.failures[0].message


def test_ruff_unrecognised_header_marks_the_enumeration_truncated() -> None:
    """A ``-->`` line with no header before it is the mirror-image loss.

    A diagnostic whose header shape this parser does not recognise leaves its
    location line with nothing to complete, so the row is dropped — stamped
    ``truncated`` rather than passed off as a complete enumeration.
    """
    observation = parse_gate_output(
        "ruff",
        "some-future-diagnostic: a header shape this parser does not know\n"
        " --> maestro/broken.py:1:7\n"
        "Found 1 error.\n",
        "",
        1,
    )

    assert observation.failures == ()
    assert observation.truncated is True


def test_ruff_unjoined_header_marks_the_enumeration_truncated() -> None:
    """A header with no ``-->`` line after it is a row this walk could not build.

    Rather than dropping it in silence, the observation is stamped ``truncated``
    — the documented "known incomplete" flag ``attribute`` reads to refuse
    ``INHERITED``, since the lost diagnostic was never compared to the base.
    """
    observation = parse_gate_output(
        "ruff",
        "F401 [*] `os` imported but unused\n"
        " --> maestro/old.py:1:8\n"
        "invalid-syntax: Expected a parameter\n"
        "Found 2 errors.\n",
        "",
        1,
    )

    assert len(observation.failures) == 1
    assert observation.truncated is True
    baseline = _run(parse_gate_output("ruff", _RUFF_INHERITED_F401, "", 1))
    assert attribute(_run(observation), ["maestro/new.py"], baseline) == (
        "UNATTRIBUTABLE"
    )


def test_ruff_identities_are_path_qualified() -> None:
    """The same rule on the same line in two files yields two identities.

    A bare ``line:col:CODE`` identity would collide here and silently understate
    how many failures the gate has — which would read as progress on a fix round
    that changed nothing.
    """
    observation = parse_gate_output(
        "ruff",
        "E402 Module level import not at top of file\n"
        " --> maestro/a.py:12:5\n"
        "E402 Module level import not at top of file\n"
        " --> maestro/b.py:12:5\n",
        "",
        1,
    )

    assert len(observation.failures) == 2
    assert observation.signature() == frozenset(
        {"ruff:maestro/a.py:12:5:E402", "ruff:maestro/b.py:12:5:E402"},
    )


# ── mypy parsing ───────────────────────────────────────────────────────────────


def test_mypy_enumerates_errors_and_skips_notes_and_summary() -> None:
    """Three ``error:`` lines parse; the ``note:`` line and summary do not."""
    observation = parse_gate_output("mypy", _MYPY_ERRORS, "", 1)

    assert observation.kind == "FAILED"
    assert len(observation.failures) == 3
    assert [f.identifier for f in observation.failures] == [
        "maestro/bar.py:42:return-value",
        "maestro/bar.py:88:name-defined",
        # No bracketed code printed, so the identity falls back to path:line.
        "maestro/baz.py:7",
    ]
    assert [f.location for f in observation.failures] == [
        "maestro/bar.py",
        "maestro/bar.py",
        "maestro/baz.py",
    ]


def test_mypy_message_excludes_the_bracketed_code() -> None:
    """The trailing ``[code]`` bracket is captured as the code, not the message."""
    observation = parse_gate_output("mypy", _MYPY_ERRORS, "", 1)

    assert observation.failures[0].message == (
        'Incompatible return value type (got "int", expected "str")'
    )
    assert observation.failures[1].message == 'Name "missing" is not defined'


# ── Green, non-answer, and unrecognised output ─────────────────────────────────


def test_zero_returncode_is_clean_regardless_of_output() -> None:
    """A green run is ``CLEAN`` with no failures — its warnings are not failures."""
    observation = parse_gate_output(
        "tests", "1184 passed in 61.20s\nDeprecationWarning: old thing\n", "", 0,
    )

    assert observation.kind == "CLEAN"
    assert observation.failures == ()
    assert observation.signature() == frozenset()


def test_stderr_is_parsed_alongside_stdout() -> None:
    """A runner that writes diagnostics to ``stderr`` parses identically."""
    observation = parse_gate_output("ruff", "", _RUFF_FULL_DIAGNOSTICS, 1)

    assert len(observation.failures) == 3
    assert observation.output.startswith("E402 Module level import")


def test_unrecognised_red_is_failed_with_a_synthetic_identity() -> None:
    """Output the parser does not recognise stays red, never green.

    The fail-closed half of the signature contract: a red gate whose output
    produced no parseable failure contributes one synthetic ``tool:kind`` identity
    rather than an empty set.
    """
    observation = parse_gate_output("ruff", "totally unexpected noise", "", 1)

    assert observation.kind == "FAILED"
    assert observation.failures == ()
    assert observation.signature() == frozenset({"ruff:FAILED"})


def test_empty_streams_on_a_red_return_stay_red() -> None:
    """A red exit code with no output at all is still red."""
    observation = parse_gate_output("mypy", "", "", 1)

    assert observation.kind == "FAILED"
    assert observation.output == ""
    assert observation.signature() == frozenset({"mypy:FAILED"})


def test_timeout_observation_signature_is_non_empty() -> None:
    """A ``TIMEOUT`` gate's signature is ``{"tests:TIMEOUT"}``, never empty.

    A non-answer that salvaged nothing has the *kind* as its whole fact; an empty
    signature would let a downstream greenness reader mistake a runner that never
    answered for a clean run.
    """
    observation = GateObservation(
        tool="tests", kind="TIMEOUT", failures=(), output="",
    )

    assert observation.signature() == frozenset({"tests:TIMEOUT"})


def test_unavailable_observation_signature_is_non_empty() -> None:
    """An ``UNAVAILABLE`` gate's signature names the missing runner."""
    observation = GateObservation(
        tool="mypy", kind="UNAVAILABLE", failures=(), output="",
    )

    assert observation.signature() == frozenset({"mypy:UNAVAILABLE"})


# ── parse_non_answer ─────────────────────────────────────────────────


def test_non_answer_salvages_the_partial_output_it_was_given() -> None:
    """A killed runner's partial output is enumerated, not discarded.

    ``subprocess.TimeoutExpired`` carries whatever the runner wrote before the
    kill, and on a slow suite that text already names real failures — the only
    material a fixer has for a gate that never answered. Building the observation
    empty threw it away.
    """
    observation = parse_non_answer(
        "tests",
        "TIMEOUT",
        "FAILED tests/test_alpha.py::test_one - ValueError: bad\n"
        "FAILED tests/test_alpha.py::test_two - KeyError: 'k'\n",
        "",
    )

    assert observation.kind == "TIMEOUT"
    assert [f.identifier for f in observation.failures] == [
        "tests/test_alpha.py::test_one",
        "tests/test_alpha.py::test_two",
    ]
    assert observation.failures[0].message == "ValueError: bad"


def test_non_answer_signature_keeps_the_kind_beside_the_salvaged_rows() -> None:
    """A salvaged row never lets a timed-out gate read as a smaller failure set.

    Without the synthetic identity, a run that timed out after two salvaged rows
    would signature-compare as *progress* against a completed run with three — a
    gate that never finished, read as a shrinking failing set.
    """
    observation = parse_non_answer(
        "tests", "TIMEOUT", "FAILED tests/test_alpha.py::test_one - boom\n", "",
    )

    assert observation.signature() == frozenset(
        {"tests:TIMEOUT", "tests:tests/test_alpha.py::test_one"},
    )


def test_non_answer_kind_survives_a_salvaged_coverage_row() -> None:
    """No salvaged row may relabel the non-answer as an ordinary red.

    ``parse_gate_output`` would call this output a ``COVERAGE`` red; the gate did
    not answer, so the kind stays ``TIMEOUT`` however the partial text reads.
    """
    observation = parse_non_answer(
        "tests", "TIMEOUT", _PYTEST_COVERAGE_SHORTFALL, "",
    )

    assert observation.kind == "TIMEOUT"
    assert observation.failures[0].kind == "COVERAGE"


def test_non_answer_with_no_output_is_the_bare_kind() -> None:
    """An ``UNAVAILABLE`` runner never started, so it salvages nothing."""
    observation = parse_non_answer("mypy", "UNAVAILABLE", "", "")

    assert observation.kind == "UNAVAILABLE"
    assert observation.failures == ()
    assert observation.output == ""
    assert observation.signature() == frozenset({"mypy:UNAVAILABLE"})


def test_non_answer_output_is_bounded_like_any_other() -> None:
    """The salvaged text honours the same tail cap a finished run's does."""
    observation = parse_non_answer(
        "tests", "TIMEOUT", "x" * (_MAX_OUTPUT_CHARS + 500), "",
    )

    assert len(observation.output) == _MAX_OUTPUT_CHARS


# ── Bounds ─────────────────────────────────────────────────────────────────────


def test_failure_count_is_capped() -> None:
    """A catastrophically red suite is bounded at ``_MAX_FAILURES`` rows."""
    lines = "".join(
        f"FAILED tests/test_big.py::test_{index}\n"
        for index in range(_MAX_FAILURES + 25)
    )
    observation = parse_gate_output("tests", lines, "", 1)

    assert len(observation.failures) == _MAX_FAILURES
    # And the row set says so: a capped enumeration is not the whole failing set,
    # which is what keeps ``attribute`` from excusing it as wholly ``INHERITED``.
    assert observation.truncated is True


def test_ruff_failure_count_is_capped() -> None:
    """The ruff parser honours the same bound as the pytest parser."""
    lines = "".join(
        f"F401 [*] `os` imported but unused\n --> maestro/foo.py:{index}:1\n"
        for index in range(1, _MAX_FAILURES + 20)
    )
    observation = parse_gate_output("ruff", lines, "", 1)

    assert len(observation.failures) == _MAX_FAILURES
    assert observation.truncated is True


def test_mypy_failure_count_is_capped() -> None:
    """The mypy parser honours the same bound as its two siblings."""
    lines = "".join(
        f"maestro/bar.py:{index}: error: Something wrong  [misc]\n"
        for index in range(1, _MAX_FAILURES + 20)
    )
    observation = parse_gate_output("mypy", lines, "", 1)

    assert len(observation.failures) == _MAX_FAILURES
    assert observation.truncated is True


def test_an_enumeration_inside_the_cap_is_not_truncated() -> None:
    """The default is "complete": only a producer that cut the set stamps it.

    The complement of the three cap tests — a red that fit well inside the bound,
    and a green gate, both report a trustworthy failing set, which is what lets
    ``attribute`` reach ``INHERITED`` at all.
    """
    red = parse_gate_output("mypy", _MYPY_ERRORS, "", 1)
    green = parse_gate_output("tests", "1184 passed\n", "", 0)

    assert len(red.failures) == 3
    assert red.truncated is False
    assert green.truncated is False


def test_message_is_truncated() -> None:
    """A huge failure message is truncated before it can bloat a prompt."""
    huge = "x" * (_MAX_MESSAGE_CHARS + 500)
    observation = parse_gate_output(
        "tests", f"FAILED tests/test_a.py::test_b - {huge}\n", "", 1,
    )

    assert len(observation.failures[0].message) == _MAX_MESSAGE_CHARS


def test_output_is_retained_far_beyond_the_pre_124_tail() -> None:
    """The observation keeps the whole captured output of a realistic red run.

    Retaining it is the point of the runner refactor: the 1000-character tail
    routinely began mid-word and filled with warnings instead of the failure.
    """
    long_output = "noise line\n" * 400 + "FAILED tests/test_a.py::test_b - boom\n"
    observation = parse_gate_output("tests", long_output, "", 1)

    assert observation.output == long_output
    assert len(observation.output) > 1000


def test_output_is_bounded_and_keeps_the_tail() -> None:
    """A pathologically long run is capped, tail-first, without losing a failure.

    ``output`` reaches a prompt and an artifact, so CLAUDE.md's context-bloat
    invariant admits no unbounded string. The failures are enumerated from the
    *untruncated* text, so the head that is dropped costs no row — asserted here
    by a ``FAILED`` line that sits far outside the retained tail.
    """
    head = "FAILED tests/test_early.py::test_one - boom\n"
    huge = head + "noise line\n" * (_MAX_OUTPUT_CHARS // 5)
    observation = parse_gate_output("tests", huge, "", 1)

    assert len(huge) > _MAX_OUTPUT_CHARS
    assert len(observation.output) == _MAX_OUTPUT_CHARS
    # The tail is what survives, so the dropped head is no longer in ``output`` —
    # but its failure is still enumerated.
    assert observation.output == huge[-_MAX_OUTPUT_CHARS:]
    assert head not in observation.output
    assert observation.signature() == frozenset({"tests:tests/test_early.py::test_one"})


def test_clean_output_is_bounded_too() -> None:
    """A green run's retained output honours the same cap as a red one's."""
    observation = parse_gate_output("tests", "x" * (_MAX_OUTPUT_CHARS + 500), "", 0)

    assert observation.kind == "CLEAN"
    assert len(observation.output) == _MAX_OUTPUT_CHARS


# ── truncated_by_first_failure ─────────────────────────────────────────────────


def test_a_failing_test_means_the_probe_was_cut_short() -> None:
    """A ``FAILED`` row means ``-x`` stopped the run before the rest of the set."""
    observation = parse_gate_output("tests", _PYTEST_FOUR_FAILURES, "", 1)

    assert truncated_by_first_failure(observation) is True


def test_a_collection_error_means_the_probe_was_cut_short() -> None:
    """A collection abort stops the run just as a failing test does."""
    observation = parse_gate_output("tests", _PYTEST_COLLECTION_ERROR, "", 2)

    assert truncated_by_first_failure(observation) is True


def test_a_coverage_only_red_was_not_cut_short() -> None:
    """A threshold shortfall comes from a run that *completed*.

    Every test passed and the probe already carries the threshold line, so a
    second ``-x``-less run would burn a whole suite to reproduce it — this is the
    predicate that keeps the runner from doing so.
    """
    observation = parse_gate_output("tests", _PYTEST_COVERAGE_SHORTFALL, "", 1)

    assert truncated_by_first_failure(observation) is False


def test_a_clean_or_unparsed_run_was_not_cut_short() -> None:
    """A green run and a non-answer both carry no aborting failure."""
    green = parse_gate_output("tests", "1184 passed in 61.20s\n", "", 0)
    timeout = GateObservation(tool="tests", kind="TIMEOUT", failures=(), output="")

    assert truncated_by_first_failure(green) is False
    assert truncated_by_first_failure(timeout) is False


# ── GateFailure.identity fallbacks ─────────────────────────────────────────────


def test_identity_falls_back_to_location_then_kind() -> None:
    """Identity is never a bare tool name, even with empty provenance fields."""
    located = GateFailure(
        tool="ruff", kind="FAILED", location="maestro/x.py", identifier="", message="",
    )
    bare = GateFailure(
        tool="mypy", kind="FAILED", location="", identifier="", message="",
    )

    assert located.identity() == "ruff:maestro/x.py"
    assert bare.identity() == "mypy:FAILED"


# ── GateRunObservation ─────────────────────────────────────────────────────────


def test_run_signature_unions_every_gate() -> None:
    """The run-level signature is the union of the per-gate signatures."""
    run = GateRunObservation(
        gates=(
            parse_gate_output("ruff", _RUFF_FULL_DIAGNOSTICS, "", 1),
            parse_gate_output("mypy", _MYPY_ERRORS, "", 1),
            GateObservation(tool="tests", kind="TIMEOUT", failures=(), output=""),
        ),
    )

    assert len(run.signature()) == 7
    assert "tests:TIMEOUT" in run.signature()
    assert "ruff:maestro/foo.py:3:1:F401" in run.signature()


def test_run_is_clean_only_when_every_gate_is_clean() -> None:
    """``all_clean`` requires every gate green; one red gate is enough to fail."""
    green = parse_gate_output("ruff", "", "", 0)
    red = parse_gate_output("mypy", _MYPY_ERRORS, "", 1)

    assert GateRunObservation(gates=(green,)).all_clean() is True
    assert GateRunObservation(gates=(green, red)).all_clean() is False
    assert GateRunObservation(gates=(green,)).signature() == frozenset()


def test_a_run_with_no_gates_is_not_clean() -> None:
    """Nothing observed is not evidence of greenness — the fail-closed posture."""
    empty = GateRunObservation(gates=())

    assert empty.all_clean() is False
    assert empty.signature() == frozenset()


# ── attribute ──────────────────────────────────────────────────────────────────
#
# The verdict matrix. ``attribute`` is pure, so every case is two hand-built
# ``GateRunObservation``s and a path list — no engine, no VCS, no config.


def _run(*gates: GateObservation) -> GateRunObservation:
    """A whole-run observation over *gates* — the attributor's input shape."""
    return GateRunObservation(gates=gates)


def _mypy_red() -> GateObservation:
    """A red mypy gate: three errors across ``maestro/bar.py`` and ``maestro/baz.py``."""
    return parse_gate_output("mypy", _MYPY_ERRORS, "", 1)


def _collection_red() -> GateObservation:
    """A red pytest gate: one ``COLLECTION_ERROR`` for ``tests/test_broken.py``."""
    return parse_gate_output("tests", _PYTEST_COLLECTION_ERROR, "", 2)


def test_attribute_blames_the_diff_for_a_failure_in_a_changed_file() -> None:
    """One enumerated failure inside a changed path is enough for ``ATTRIBUTED``."""
    observation = _run(_mypy_red())

    verdict = attribute(observation, ["maestro/baz.py"], None)

    assert verdict == "ATTRIBUTED"


def test_attribute_prefers_the_diff_even_when_the_base_was_red_too() -> None:
    """``ATTRIBUTED`` outranks ``INHERITED`` on a file the diff actually touched.

    A failure in a file this diff changed is the diff's to answer for even when
    the base carried the identical failure — the diff had the chance to fix it and
    did not, and the alternative (excusing it) is how a real regression in a
    long-red file goes unnoticed.
    """
    observation = _run(_mypy_red())
    baseline = _run(_mypy_red())

    verdict = attribute(observation, ["maestro/bar.py", "maestro/baz.py"], baseline)

    assert verdict == "ATTRIBUTED"


def test_attribute_calls_a_baseline_confirmed_failure_inherited() -> None:
    """A collection abort outside the diff, already red on the base, is inherited.

    The proposal's headline case: an import error in a file the diff never touched
    that the branch was cut onto. Nothing about it is the plan's fault.
    """
    observation = _run(_collection_red())
    baseline = _run(_collection_red())

    verdict = attribute(observation, ["maestro/agent.py"], baseline)

    assert verdict == "INHERITED"


def test_attribute_will_not_inherit_what_the_baseline_does_not_carry() -> None:
    """The same failure absent from the baseline stays ``UNATTRIBUTABLE``.

    Conservative by construction: ``INHERITED`` is the verdict that will later
    hold a destructive arm off, so it is claimed only on identity evidence.
    """
    observation = _run(_collection_red())
    baseline = _run(_mypy_red())

    verdict = attribute(observation, ["maestro/agent.py"], baseline)

    assert verdict == "UNATTRIBUTABLE"


def test_attribute_never_infers_inherited_without_a_baseline() -> None:
    """A ``baseline`` of ``None`` can only ever yield ``ATTRIBUTED`` or the default.

    A base with no captured baseline is the ordinary case on a cold process, and
    it must read as today's behaviour rather than as an excuse.
    """
    observation = _run(_collection_red())

    assert attribute(observation, ["maestro/agent.py"], None) == "UNATTRIBUTABLE"
    assert attribute(observation, ["tests/test_broken.py"], None) == "ATTRIBUTED"


def test_attribute_cannot_reach_attributed_with_no_changed_paths() -> None:
    """An empty diff list intersects nothing — the ``INHERITED`` arm still runs."""
    observation = _run(_collection_red())

    assert attribute(observation, [], None) == "UNATTRIBUTABLE"
    assert attribute(observation, [], _run(_collection_red())) == "INHERITED"


def test_attribute_refuses_to_inherit_across_a_non_answer() -> None:
    """A gate that timed out proves nothing, even with every salvaged row matched.

    ``signature()`` keeps a non-answer's synthetic ``"tests:TIMEOUT"`` identity
    alongside the rows its partial output salvaged, and that surplus identity is
    exactly what the attributor reads as "this run did not finish".
    """
    timed_out = parse_non_answer("tests", "TIMEOUT", _PYTEST_COLLECTION_ERROR, "")
    observation = _run(timed_out)
    baseline = _run(timed_out)

    verdict = attribute(observation, ["maestro/agent.py"], baseline)

    assert verdict == "UNATTRIBUTABLE"


def test_attribute_refuses_to_inherit_beside_an_unparsed_red() -> None:
    """An unrecognised red contributes a synthetic identity, which blocks inheriting.

    The fail-safe direction for parser drift: output this owner cannot read might
    be anything, including a genuine regression, so a run carrying one is never
    excused on the strength of the failures it *could* enumerate.
    """
    unparsed = parse_gate_output("ruff", "ruff exploded in a new way\n", "", 1)
    observation = _run(_collection_red(), unparsed)
    baseline = _run(_collection_red(), unparsed)

    assert unparsed.failures == ()
    assert attribute(observation, ["maestro/agent.py"], baseline) == "UNATTRIBUTABLE"


def test_attribute_needs_every_failure_confirmed_to_inherit() -> None:
    """One inherited failure beside one the baseline does not carry is not inherited."""
    observation = _run(_collection_red(), _mypy_red())
    baseline = _run(_collection_red())

    verdict = attribute(observation, ["maestro/agent.py"], baseline)

    assert verdict == "UNATTRIBUTABLE"


def test_attribute_has_nothing_to_say_about_a_clean_run() -> None:
    """A green observation is ``UNATTRIBUTABLE`` — there is no failure to blame."""
    clean = _run(parse_gate_output("ruff", "", "", 0))

    assert clean.all_clean() is True
    assert attribute(clean, ["maestro/agent.py"], _run(_mypy_red())) == "UNATTRIBUTABLE"


def test_attribute_has_nothing_to_enumerate_on_a_bare_non_answer() -> None:
    """A run that named no failures at all cannot be attributed either way.

    An ``UNAVAILABLE`` gate never started, so it has no rows and no location — and
    a baseline that also failed to start adds no evidence about the diff.
    """
    bare = parse_non_answer("tests", "UNAVAILABLE", "", "")
    observation = _run(bare)

    assert attribute(observation, ["maestro/agent.py"], _run(bare)) == "UNATTRIBUTABLE"


def test_attribute_does_not_cross_match_same_named_files() -> None:
    """Two same-named files in different directories are different files.

    Exact normalised-path equality, no basename fallback: a false ``ATTRIBUTED``
    blames the diff for a failure it did not cause, which is the destructive
    direction.
    """
    failure = GateFailure(
        tool="mypy", kind="FAILED", location="maestro/dashboard/routes.py",
        identifier="maestro/dashboard/routes.py:9:name-defined", message="",
    )
    observation = _run(
        GateObservation(tool="mypy", kind="FAILED", failures=(failure,), output=""),
    )

    verdict = attribute(observation, ["maestro/notifications/routes.py"], None)

    assert verdict == "UNATTRIBUTABLE"


def test_attribute_normalises_a_leading_dot_slash_on_either_side() -> None:
    """``./maestro/x.py`` and ``maestro/x.py`` are the same path to the attributor.

    A runner's repo-relative spelling and a plan's ``files_affected`` entry are
    written by different hands; only whitespace is stripped where the latter is
    stored, so the folding happens here.
    """
    failure = GateFailure(
        tool="mypy", kind="FAILED", location="./maestro/x.py",
        identifier="./maestro/x.py:9:name-defined", message="",
    )
    observation = _run(
        GateObservation(tool="mypy", kind="FAILED", failures=(failure,), output=""),
    )

    assert attribute(observation, ["maestro/x.py"], None) == "ATTRIBUTED"
    assert attribute(observation, ["  ./maestro/x.py  "], None) == "ATTRIBUTED"


def test_attribute_ignores_a_failure_that_names_no_file() -> None:
    """A coverage shortfall has an empty location, which must match no changed path.

    Both sides drop empties, so a plan that (wrongly) lists an empty path cannot
    be blamed for the one red that names no file — and the coverage red still
    inherits cleanly when the base already carried it.
    """
    coverage = parse_gate_output("tests", _PYTEST_COVERAGE_SHORTFALL, "", 1)
    observation = _run(coverage)

    assert coverage.failures[0].location == ""
    assert attribute(observation, ["", "   "], None) == "UNATTRIBUTABLE"
    assert attribute(observation, ["", "   "], _run(coverage)) == "INHERITED"


def test_attribute_refuses_to_inherit_a_truncated_enumeration() -> None:
    """A known-short failing set can never be excused as wholly inherited.

    The producer stamps ``truncated`` two ways — the ``_MAX_FAILURES`` cap, and a
    ``-x`` probe kept because its ``-x``-less enumerate re-run did not itself
    answer — and in both the failures past the cut were never compared to the
    base at all. Calling that ``INHERITED`` on the strength of the rows that *did*
    fit is the exact false excuse this owner exists to refuse.
    """
    short = dataclasses.replace(_collection_red(), truncated=True)
    observation = _run(short)
    baseline = _run(_collection_red())

    # Identical rows, identical identities — only the stamp differs.
    assert short.signature() == _collection_red().signature()
    assert attribute(_run(_collection_red()), ["maestro/agent.py"], baseline) == (
        "INHERITED"
    )
    assert attribute(observation, ["maestro/agent.py"], baseline) == "UNATTRIBUTABLE"


def test_attribute_still_blames_the_diff_on_a_truncated_enumeration() -> None:
    """Truncation withholds ``INHERITED`` only — it never withholds ``ATTRIBUTED``.

    A visible failure in a file the diff touched is the diff's to answer for
    regardless of how many more the runner never got to; the unseen rows can only
    add blame, never remove it.
    """
    observation = _run(dataclasses.replace(_collection_red(), truncated=True))

    verdict = attribute(observation, ["tests/test_broken.py"], _run(_collection_red()))

    assert verdict == "ATTRIBUTED"


def test_attribute_reads_a_salvaged_baseline_row_as_real_evidence() -> None:
    """A baseline that timed out still proves what its salvaged rows named.

    The asymmetry is deliberate. A short *observation* may hide a diff-caused
    failure, so it is refused; a short *baseline* only shrinks the set the subset
    test must fit inside, which makes ``INHERITED`` harder to reach rather than
    easier. So the rows a killed baseline runner did print are honest evidence
    that the base already carried them — and an identity it never named still
    falls through to ``UNATTRIBUTABLE``.
    """
    baseline = _run(parse_non_answer("tests", "TIMEOUT", _PYTEST_COLLECTION_ERROR, ""))
    salvaged = attribute(_run(_collection_red()), ["maestro/agent.py"], baseline)
    unnamed = attribute(_run(_mypy_red()), ["maestro/agent.py"], baseline)

    assert "tests:tests/test_broken.py" in baseline.signature()
    assert salvaged == "INHERITED"
    assert unnamed == "UNATTRIBUTABLE"


# ── render_identities ────────────────────────────────────────────────────────
#
# The bounded, sorted join the proposal-124 inherited-hold arm slices into both
# the base-repair directive and the terminal-demote reason. Pure — a hand-built
# ``GateRunObservation`` and two integer bounds.


def _identity_run(*identifiers: str) -> GateRunObservation:
    """A tests run whose ``signature()`` carries one ``tests:<identifier>`` apiece."""
    return GateRunObservation(
        gates=(
            GateObservation(
                tool="tests", kind="FAILED",
                failures=tuple(
                    GateFailure(
                        tool="tests", kind="FAILED", location="tests/test_x.py",
                        identifier=ident, message="",
                    )
                    for ident in identifiers
                ),
                output="",
            ),
        ),
    )


def test_render_identities_is_sorted() -> None:
    """Identities render in sorted order, so the output is deterministic."""
    rendered = render_identities(_identity_run("c", "a", "b"), limit=10, max_chars=1000)

    assert rendered == "tests:a, tests:b, tests:c"


def test_render_identities_caps_the_count() -> None:
    """At most *limit* identities are joined (the first *limit* in sorted order)."""
    rendered = render_identities(
        _identity_run("a", "b", "c", "d", "e"), limit=2, max_chars=1000,
    )

    assert rendered == "tests:a, tests:b"


def test_render_identities_caps_the_chars() -> None:
    """The joined string is truncated to *max_chars* characters."""
    rendered = render_identities(
        _identity_run("aaaa", "bbbb", "cccc"), limit=10, max_chars=15,
    )

    assert len(rendered) == 15
    assert rendered == "tests:aaaa, tes"


def test_render_identities_all_clean_is_empty() -> None:
    """An all-``CLEAN`` observation — an empty signature — renders the empty string."""
    clean = GateRunObservation(
        gates=(
            GateObservation(tool="ruff", kind="CLEAN", failures=(), output=""),
            GateObservation(tool="mypy", kind="CLEAN", failures=(), output=""),
        ),
    )

    assert render_identities(clean, limit=5, max_chars=100) == ""


# ── render_failure_report ──────────────────────────────────────────────────────
#
# The multi-line ``## Quality gate failures`` block the proposal-124 auto-fix
# prompt reads in place of the front-truncated ``raw_output`` tail. Pure — a
# hand-built ``GateRunObservation`` and two integer bounds.


def test_render_failure_report_multi_gate_red() -> None:
    """A multi-gate red renders the heading, one bullet per non-CLEAN gate, and
    one sub-bullet per enumerated failure carrying its identity and message."""
    run = GateRunObservation(
        gates=(
            GateObservation(tool="ruff", kind="CLEAN", failures=(), output=""),
            GateObservation(
                tool="mypy", kind="FAILED",
                failures=(
                    GateFailure(
                        tool="mypy", kind="FAILED", location="maestro/x.py",
                        identifier="maestro/x.py:12:name-defined",
                        message="Name 'foo' is not defined",
                    ),
                ),
                output="",
            ),
            GateObservation(
                tool="tests", kind="FAILED",
                failures=(
                    GateFailure(
                        tool="tests", kind="FAILED", location="tests/test_a.py",
                        identifier="tests/test_a.py::test_one",
                        message="assert 1 == 2",
                    ),
                ),
                output="",
            ),
        ),
    )

    rendered = render_failure_report(run, limit=50, max_chars=2000)

    assert rendered.startswith("## Quality gate failures")
    # The CLEAN ruff gate contributes no bullet.
    assert "ruff" not in rendered
    assert "- mypy (FAILED):" in rendered
    assert "- tests (FAILED):" in rendered
    assert (
        "  - mypy:maestro/x.py:12:name-defined — Name 'foo' is not defined"
        in rendered
    )
    assert "  - tests:tests/test_a.py::test_one — assert 1 == 2" in rendered


def test_render_failure_report_red_gate_with_no_failures_gets_placeholder() -> None:
    """A red gate that enumerated nothing (a coverage-only red, a TIMEOUT) still
    renders a placeholder sub-bullet naming the kind — never a silent empty gate."""
    run = GateRunObservation(
        gates=(
            GateObservation(
                tool="tests", kind="COVERAGE", failures=(), output="",
            ),
        ),
    )

    rendered = render_failure_report(run, limit=50, max_chars=2000)

    assert "- tests (COVERAGE):" in rendered
    assert "  - (no failure line parsed — kind is COVERAGE)" in rendered


def test_render_failure_report_truncated_gate_marks_incomplete() -> None:
    """A gate stamped ``truncated`` gets the trailing incomplete-enumeration line
    so no reader treats its failures as the whole failing set."""
    run = GateRunObservation(
        gates=(
            GateObservation(
                tool="tests", kind="FAILED",
                failures=(
                    GateFailure(
                        tool="tests", kind="FAILED", location="tests/test_a.py",
                        identifier="tests/test_a.py::test_one", message="boom",
                    ),
                ),
                output="", truncated=True,
            ),
        ),
    )

    rendered = render_failure_report(run, limit=50, max_chars=2000)

    assert "  - tests:tests/test_a.py::test_one — boom" in rendered
    assert "  - (enumeration incomplete — more failures may exist)" in rendered


def test_render_failure_report_all_clean_is_empty() -> None:
    """An all-``CLEAN`` observation renders the empty string, so a caller can
    append it unconditionally and stay byte-identical on the green path."""
    clean = GateRunObservation(
        gates=(
            GateObservation(tool="ruff", kind="CLEAN", failures=(), output=""),
            GateObservation(tool="mypy", kind="CLEAN", failures=(), output=""),
        ),
    )

    assert render_failure_report(clean, limit=5, max_chars=100) == ""


def test_render_failure_report_caps_the_row_count() -> None:
    """*limit* caps the enumerated failure sub-bullets across the whole run; the
    heading and gate bullet are structural and never counted against it."""
    run = GateRunObservation(
        gates=(
            GateObservation(
                tool="tests", kind="FAILED",
                failures=tuple(
                    GateFailure(
                        tool="tests", kind="FAILED", location="tests/test_a.py",
                        identifier=f"tests/test_a.py::test_{i}", message="boom",
                    )
                    for i in range(5)
                ),
                output="",
            ),
        ),
    )

    rendered = render_failure_report(run, limit=2, max_chars=2000)

    # Exactly two enumerated failure sub-bullets survive the row cap.
    assert rendered.count("  - tests:tests/test_a.py::test_") == 2
    assert "test_0" in rendered
    assert "test_1" in rendered
    assert "test_2" not in rendered
    # The three unshown failures earn the incomplete marker — the gate is not
    # ``truncated``, so only the row cap can have hidden them.
    assert "  - (enumeration incomplete — more failures may exist)" in rendered


def test_render_failure_report_row_cap_marks_later_gate_incomplete() -> None:
    """When an earlier gate spends the run-wide row cap, a later red gate with
    failures still gets the incomplete marker instead of rendering as a bare
    header that reads like a clean gate."""
    run = GateRunObservation(
        gates=(
            GateObservation(
                tool="ruff", kind="FAILED",
                failures=(
                    GateFailure(
                        tool="ruff", kind="FAILED", location="maestro/x.py",
                        identifier="maestro/x.py:1:0:E501",
                        message="line too long",
                    ),
                ),
                output="",
            ),
            GateObservation(
                tool="tests", kind="FAILED",
                failures=(
                    GateFailure(
                        tool="tests", kind="FAILED", location="tests/test_a.py",
                        identifier="tests/test_a.py::test_one", message="boom",
                    ),
                ),
                output="",
            ),
        ),
    )

    # limit=1: the ruff failure spends the only row, so the tests gate renders
    # header-only. It must still carry the incomplete marker.
    rendered = render_failure_report(run, limit=1, max_chars=2000)

    assert "- tests (FAILED):" in rendered
    assert "tests:tests/test_a.py::test_one" not in rendered
    assert "  - (enumeration incomplete — more failures may exist)" in rendered


def test_render_failure_report_caps_the_chars() -> None:
    """The joined block is truncated to *max_chars* with a visible marker, keeping
    its head (heading + earliest failures) and dropping the tail."""
    run = GateRunObservation(
        gates=(
            GateObservation(
                tool="tests", kind="FAILED",
                failures=tuple(
                    GateFailure(
                        tool="tests", kind="FAILED", location="tests/test_a.py",
                        identifier=f"tests/test_a.py::test_{i}",
                        message="a very long assertion message " * 5,
                    )
                    for i in range(5)
                ),
                output="",
            ),
        ),
    )

    rendered = render_failure_report(run, limit=50, max_chars=40)

    assert len(rendered) == 40
    assert rendered.startswith("## Quality gate failures")
    # The tail was dropped, so a marker signals the cut rather than a silent slice.
    assert rendered.endswith("[truncated]")


def test_render_failure_report_char_cap_below_marker_is_bare_slice() -> None:
    """When *max_chars* is smaller than the truncation marker itself, the block is
    a bare head slice with no marker — the ``len(result) <= max_chars`` bound still
    holds (a marker would overshoot the cap)."""
    run = GateRunObservation(
        gates=(
            GateObservation(
                tool="tests", kind="FAILED",
                failures=(
                    GateFailure(
                        tool="tests", kind="FAILED", location="tests/test_a.py",
                        identifier="tests/test_a.py::test_one", message="boom",
                    ),
                ),
                output="",
            ),
        ),
    )

    rendered = render_failure_report(run, limit=50, max_chars=5)

    assert rendered == "## Qu"
    assert "[truncated]" not in rendered


def test_render_failure_report_no_marker_when_within_char_cap() -> None:
    """A block within *max_chars* is returned verbatim — no truncation marker."""
    run = GateRunObservation(
        gates=(
            GateObservation(
                tool="tests", kind="FAILED",
                failures=(
                    GateFailure(
                        tool="tests", kind="FAILED", location="tests/test_a.py",
                        identifier="tests/test_a.py::test_one", message="boom",
                    ),
                ),
                output="",
            ),
        ),
    )

    rendered = render_failure_report(run, limit=50, max_chars=2000)

    assert "[truncated]" not in rendered
    assert rendered.endswith("boom")


# ── The ENVIRONMENT attribution (proposal 133) ─────────────────────────────────


def _runnability(
    kind: str, identities: frozenset[str],
) -> GateRunnabilityVerdict:
    """A non-``SOUND`` runnability verdict naming *identities*."""
    return GateRunnabilityVerdict(
        kind=kind,  # type: ignore[arg-type]
        fingerprint="fp",
        identities=identities,
        detail="collection COLLECTION_ERROR",
    )


def test_attribute_names_the_environment_when_the_verdict_covers_every_identity() -> None:
    """A red the runnability verdict wholly names is the environment's."""
    observation = _run(_collection_red())

    verdict = attribute(
        observation,
        ["maestro/agent.py"],
        None,
        runnability=_runnability("UNCOLLECTABLE", observation.signature()),
    )

    assert verdict == "ENVIRONMENT"


def test_attribute_environment_outranks_attributed() -> None:
    """A gate that cannot reach a verdict judged no diff, changed path or not."""
    observation = _run(_collection_red())

    verdict = attribute(
        observation,
        ["tests/test_broken.py"],
        None,
        runnability=_runnability("UNCOLLECTABLE", observation.signature()),
    )

    assert verdict == "ENVIRONMENT"


def test_attribute_ignores_a_sound_runnability_verdict() -> None:
    """A ``SOUND`` verdict is evidence the environment is fine — never a hold."""
    observation = _run(_collection_red())

    verdict = attribute(
        observation,
        ["tests/test_broken.py"],
        None,
        runnability=GateRunnabilityVerdict(
            kind="SOUND", fingerprint="fp", identities=observation.signature(),
        ),
    )

    assert verdict == "ATTRIBUTED"


def test_attribute_without_a_runnability_verdict_is_unchanged() -> None:
    """The default ``None`` keeps every pre-133 call site byte-identical."""
    observation = _run(_collection_red())

    assert attribute(observation, ["tests/test_broken.py"], None) == "ATTRIBUTED"
    assert attribute(observation, ["maestro/agent.py"], None) == "UNATTRIBUTABLE"


def test_attribute_refuses_environment_for_an_identity_the_verdict_misses() -> None:
    """One failing identity the verdict does not name withholds ``ENVIRONMENT``.

    The verdict measured *some* break; a red carrying a failure beyond it is not
    wholly explained by the environment, so the ordinary precedence resumes.
    """
    observation = _run(_collection_red(), _mypy_red())

    verdict = attribute(
        observation,
        ["maestro/agent.py"],
        None,
        runnability=_runnability("UNCOLLECTABLE", _run(_collection_red()).signature()),
    )

    assert verdict == "UNATTRIBUTABLE"


def test_only_the_convergence_reader_passes_a_runnability_verdict() -> None:
    """``runnability=`` is supplied at exactly one call site in the tree.

    The two non-convergence callers — the post-merge tend read in
    ``evolution.py`` and ``agent.MaestroAgent._classify_owned_red`` — keep passing
    no verdict, so their ``UNATTRIBUTABLE`` / ``ATTRIBUTED`` arms can never see
    ``ENVIRONMENT``. Asserted structurally rather than inferred from a green suite.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "maestro"
    total = 0
    with_verdict = 0
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(
                func, "id", "",
            )
            if name != "attribute":
                continue
            total += 1
            if any(kw.arg == "runnability" for kw in node.keywords):
                with_verdict += 1

    assert total == 3
    assert with_verdict == 1


# ── The two dead-arm regressions (proposal 133, step 5 polish) ──────────────────


def _timeout_non_answer() -> GateObservation:
    """A pytest gate that never answered: a lone synthetic ``tests:TIMEOUT``.

    Zero enumerated failures — the exact shape ``probe_fit``'s ``UNFIT`` verdict
    describes, and the observation step 5's own acceptance criterion names.
    """
    return GateObservation(tool="tests", kind="TIMEOUT", failures=(), output="")


def test_unfit_verdict_owns_a_red_despite_carrying_no_identities() -> None:
    """An identity-less verdict is a *whole-run* claim, not an empty one.

    ``GateRunnabilityVerdict.identities`` is populated for ``UNCOLLECTABLE``
    alone, so a bare ``signature() <= identities`` subset test is ``False`` for
    every ``UNPROVISIONED`` / ``UNFIT`` verdict — making the arm dead for the two
    kinds the proposal names as motivating. A sibling distribution that will not
    import, or a suite that cannot finish in its budget, means the run as a whole
    reached no verdict.
    """
    observation = _run(_mypy_red())

    verdict = attribute(
        observation,
        ["maestro/agent.py"],
        None,
        runnability=_runnability("UNFIT", frozenset()),
    )

    assert verdict == "ENVIRONMENT"


def test_unprovisioned_verdict_owns_a_red_in_a_changed_path() -> None:
    """The whole-run claim outranks ``ATTRIBUTED`` like any other ``ENVIRONMENT``."""
    observation = _run(_mypy_red())

    verdict = attribute(
        observation,
        ["maestro/baz.py"],
        None,
        runnability=_runnability("UNPROVISIONED", frozenset()),
    )

    assert verdict == "ENVIRONMENT"


def test_environment_outranks_the_enumerated_nothing_unattributable_arm() -> None:
    """A bare non-answer reaches the ``ENVIRONMENT`` arm.

    A lone ``tests:TIMEOUT`` enumerates no failures, so the "nothing to attribute"
    arm claimed it before the verdict was ever consulted — precisely the class an
    ``UNFIT`` environment produces, and precisely the case step 5's acceptance
    criterion requires to classify ``HOLD_ENVIRONMENT``.
    """
    observation = _run(_timeout_non_answer())
    assert observation.signature() == frozenset({"tests:TIMEOUT"})

    verdict = attribute(
        observation,
        ["maestro/agent.py"],
        None,
        runnability=_runnability("UNFIT", frozenset()),
    )

    assert verdict == "ENVIRONMENT"


def test_a_bare_non_answer_without_a_verdict_stays_unattributable() -> None:
    """Moving the arm up does not widen the no-verdict path — still pre-133."""
    observation = _run(_timeout_non_answer())

    assert attribute(observation, ["maestro/agent.py"], None) == "UNATTRIBUTABLE"
    assert attribute(
        observation,
        ["maestro/agent.py"],
        None,
        runnability=GateRunnabilityVerdict(kind="SOUND", fingerprint="fp"),
    ) == "UNATTRIBUTABLE"


def test_a_named_verdict_still_withholds_environment_from_a_wider_red() -> None:
    """The whole-run widening applies to *empty* identities only.

    A verdict that does name identities keeps its narrow scope: a collection abort
    alongside a mypy regression the diff introduced is not the environment's to
    excuse.
    """
    observation = _run(_collection_red(), _mypy_red())

    verdict = attribute(
        observation,
        ["maestro/baz.py"],
        None,
        runnability=_runnability("UNCOLLECTABLE", _run(_collection_red()).signature()),
    )

    assert verdict == "ATTRIBUTED"


def test_a_clean_run_is_never_the_environments_fault() -> None:
    """``all_clean`` still short-circuits above the arm — a green run is green."""
    clean = _run(GateObservation(tool="tests", kind="CLEAN", failures=(), output=""))

    verdict = attribute(
        clean, ["maestro/agent.py"], None,
        runnability=_runnability("UNFIT", frozenset()),
    )

    assert verdict == "UNATTRIBUTABLE"


def test_pytest_open_bracket_parametrized_id_keeps_its_message_out_of_the_identity() -> None:
    """A parameter value ``[`` renders an id no *head* balance test can split.

    ``pytest.mark.parametrize("v", ["["])`` yields ``test_case[[]`` — a literal
    unmatched ``[`` pytest preserves verbatim. Keying the split on the head would
    find no balanced candidate and swallow the whole assertion message into the
    identifier, so editing the message would mint a *different* identity for the
    same failing test and break the strict-subset progress predicate. The message
    half carries no surplus ``]``, so the real separator is found.
    """
    observation = parse_gate_output(
        "tests",
        "FAILED tests/test_probe.py::test_case[[] - AssertionError: attempt 1\n",
        "",
        1,
    )

    assert [f.identifier for f in observation.failures] == [
        "tests/test_probe.py::test_case[[]",
    ]
    assert [f.message for f in observation.failures] == ["AssertionError: attempt 1"]
    assert observation.failures[0].location == "tests/test_probe.py"


def test_pytest_bracket_ids_carrying_the_separator_stay_distinct_identities() -> None:
    """Two ids differing only *after* an early-balancing ``]`` keep two identities.

    ``pytest.mark.parametrize("v", ["a] - left", "a] - right"])`` renders
    ``test_case[a] - left]`` and ``test_case[a] - right]``. A head-balance test
    accepts the separator right after ``[a]`` for both rows, collapsing two
    distinct failures onto one identifier and hiding a regression from the union
    signature. The tail test rejects that candidate — ``left] - ...`` carries a
    surplus ``]`` — and lands on the separator pytest printed.
    """
    observation = parse_gate_output(
        "tests",
        "FAILED tests/test_probe.py::test_case[a] - left] - ValueError: boom\n"
        "FAILED tests/test_probe.py::test_case[a] - right] - ValueError: boom\n",
        "",
        1,
    )

    assert [f.identifier for f in observation.failures] == [
        "tests/test_probe.py::test_case[a] - left]",
        "tests/test_probe.py::test_case[a] - right]",
    ]
    assert [f.message for f in observation.failures] == [
        "ValueError: boom",
        "ValueError: boom",
    ]


def test_pytest_message_carrying_a_surplus_bracket_still_splits() -> None:
    """A *message* with its own stray ``]`` splits rather than joining the identity.

    No candidate leaves a surplus-``]``-free message half, and the whole tail is
    itself surplus-``]`` — the signature of a message, not of a message-less
    parametrized id — so the first candidate is the fallback. Swallowing it whole
    would make the identity move with the assertion text.
    """
    observation = parse_gate_output(
        "tests",
        "FAILED tests/test_probe.py::test_case - AssertionError: assert ']' == 'x'\n",
        "",
        1,
    )

    assert [f.identifier for f in observation.failures] == [
        "tests/test_probe.py::test_case",
    ]
    assert [f.message for f in observation.failures] == [
        "AssertionError: assert ']' == 'x'",
    ]


def test_bracket_id_progress_reads_as_continue_across_a_changed_message() -> None:
    """A shrinking failure set over a bracket id measures as progress, not ``stuck``.

    The end-to-end consequence the splitter exists for: the open-bracket row's
    identity must be a pure function of the *nodeid*, so a round that clears
    ``test_other`` while the bracket failure's assertion text changes is a strict
    subset of the prior round — ``continue``, not ``stuck``.
    """
    prev = parse_gate_output(
        "tests",
        "FAILED tests/test_probe.py::test_case[[] - AssertionError: attempt 1\n"
        "FAILED tests/test_probe.py::test_other - AssertionError: nope\n",
        "",
        1,
    )
    curr = parse_gate_output(
        "tests",
        "FAILED tests/test_probe.py::test_case[[] - AssertionError: attempt 2\n",
        "",
        1,
    )

    decision = plan_next_round(
        GateRunObservation(gates=(prev,)).signature(),
        GateRunObservation(gates=(curr,)).signature(),
        all_clean=False,
        rounds_used=1,
        ceiling=5,
    )

    assert decision == "continue"


def test_pytest_bracket_id_survives_a_message_carrying_a_stray_bracket() -> None:
    """A parametrized id keeps its identity when the *message* has a surplus ``]``.

    ``test_case[a - b] - AssertionError: missing ']'`` defeats the message-half
    balance test on its own — no candidate leaves a surplus-``]``-free message — so
    keying the split only on the message would fall through to the first candidate
    and cut the nodeid in half at ``test_case[a``, the exact mis-split the splitter
    exists to prevent. The node-half test still names the real separator, so the
    ranked pass lands on it.
    """
    observation = parse_gate_output(
        "tests",
        "FAILED tests/test_probe.py::test_case[a - b] - AssertionError: missing ']'\n",
        "",
        1,
    )

    assert [f.identifier for f in observation.failures] == [
        "tests/test_probe.py::test_case[a - b]",
    ]
    assert [f.message for f in observation.failures] == ["AssertionError: missing ']'"]


def test_pytest_row_no_balance_test_can_read_splits_at_the_first_separator() -> None:
    """A tail both balance tests reject splits rather than swallowing its message.

    ``test_case[[ - ]]]`` opens brackets no head closes *and* leaves a surplus
    ``]`` in every message half, so neither test accepts any candidate. The tail
    itself carries a surplus ``]`` — the signature of a message, not of a
    message-less parametrized id — so the first candidate decides and the row
    splits rather than swallowing its message into the identity.
    """
    node, message = _split_pytest_node_and_message("test_case[[ - ]]]")

    assert node == "test_case[["
    assert message == "]]]"


# ── Environmental exhaustion (#4213) ───────────────────────────────────────────

_SUMMARY_BANNER = (
    "=========================== short test summary info ============================\n"
)
_EMFILE_ROW = (
    "FAILED tests/test_mem.py::test_{n} - OSError: [Errno 24] Too many open files: "
    "'/fake/x'\n"
)


def _pytest_summary(*rows: str) -> str:
    return _SUMMARY_BANNER + "".join(rows) + "==== failed in 1.00s ====\n"


def test_all_emfile_red_is_the_unavailable_non_answer() -> None:
    """A red made only of descriptor exhaustion reached no verdict on the code."""
    output = _pytest_summary(
        _EMFILE_ROW.format(n=1),
        "FAILED tests/test_mem.py::test_2 - OSError: [Errno 28] No space left on device\n",
        "ERROR tests/test_mem.py::test_3 - MemoryError\n",
        "FAIL Required test coverage of 100% not reached. Total coverage: 97.00%\n",
    )

    observation = parse_gate_output("tests", output, "", 1)

    assert observation.kind == "UNAVAILABLE"
    # The run is still recorded honestly: every salvaged row is kept.
    assert len(observation.failures) == 4
    assert "tests:UNAVAILABLE" in observation.signature()


def test_emfile_mixed_with_an_assertion_stays_red() -> None:
    output = _pytest_summary(
        _EMFILE_ROW.format(n=1),
        "FAILED tests/test_real.py::test_bug - AssertionError: 1 != 2\n",
    )

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_all_assertion_red_stays_red() -> None:
    output = _pytest_summary(
        "FAILED tests/test_real.py::test_bug - AssertionError: 1 != 2\n",
    )

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_real_failure_past_the_row_cap_keeps_an_emfile_run_red() -> None:
    """The exhaustion check walks past ``_MAX_FAILURES``; a late real red still counts."""
    rows = [_EMFILE_ROW.format(n=n) for n in range(_MAX_FAILURES + 5)]
    rows.append("FAILED tests/test_real.py::test_bug - AssertionError: late\n")

    observation = parse_gate_output("tests", _pytest_summary(*rows), "", 1)

    assert observation.kind == "FAILED"
    assert observation.truncated is True


def test_coverage_only_red_is_not_exhaustion() -> None:
    """With no exhausted row to blame, a coverage shortfall stays ``COVERAGE``."""
    output = "FAIL Required test coverage of 100% not reached. Total coverage: 97.00%\n"

    assert parse_gate_output("tests", output, "", 1).kind == "COVERAGE"


def test_exhaustion_relabel_is_tests_only() -> None:
    """A mypy message quoting ``MemoryError`` is still a mypy red."""
    output = "maestro/x.py:3: error: Name \"MemoryError\" is odd  [misc]\n"

    assert parse_gate_output("mypy", output, "", 1).kind == "FAILED"


@pytest.mark.parametrize(
    "row",
    [
        "FAILED tests/test_x.py::test_a - Failed: DID NOT RAISE <class 'MemoryError'>\n",
        "FAILED tests/test_x.py::test_b - AssertionError: assert "
        "'No space left on device' in ''\n",
        "FAILED tests/test_x.py::test_c - AssertionError: 'Too many open files'\n",
        "FAILED tests/test_x.py::test_d - AssertionError: assert "
        "\"worker 'gw1' crashed while running 'x'\" == ''\n",
        "FAILED tests/test_x.py::test_e - AssertionError: "
        "'[gw1] node down: Not properly terminated'\n",
    ],
)
def test_a_red_that_only_quotes_an_exhaustion_name_stays_red(row: str) -> None:
    """Exhaustion is read from the leading exception type, not anywhere in the text."""
    assert parse_gate_output("tests", _pytest_summary(row), "", 1).kind == "FAILED"


_XDIST_CRASH_ROW = (
    "FAILED tests/test_big.py::test_heavy - "
    "worker 'gw2' crashed while running 'tests/test_big.py::test_heavy'\n"
)


def test_xdist_worker_crash_red_is_the_unavailable_non_answer() -> None:
    """An OOM-killed xdist worker surfaces only as xdist's crash row (#4315)."""
    output = _pytest_summary(
        _XDIST_CRASH_ROW,
        "FAIL Required test coverage of 100% not reached. Total coverage: 97.41%\n",
    )

    observation = parse_gate_output("tests", output, "", 1)

    assert observation.kind == "UNAVAILABLE"
    assert len(observation.failures) == 2


def test_xdist_worker_crash_mixed_with_an_assertion_stays_red() -> None:
    output = _pytest_summary(
        _XDIST_CRASH_ROW,
        "FAILED tests/test_real.py::test_bug - AssertionError: 1 != 2\n",
    )

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_double_quoted_xdist_crash_row_is_recognised() -> None:
    """xdist reprs the nodeid, so an apostrophe in it switches to double quotes."""
    output = _pytest_summary(
        "FAILED tests/test_big.py::test_x[it's] - "
        "worker 'gw0' crashed while running \"tests/test_big.py::test_x[it's]\"\n",
    )

    assert parse_gate_output("tests", output, "", 1).kind == "UNAVAILABLE"


#: Real non-CI xdist output: the crash text lives in the FAILURES section, and
#: the summary row is bare or trimmed to the terminal width (#4315).
_NON_CI_CRASH_OUTPUT = (
    "[gw1] node down: Not properly terminated\n"
    "F\n"
    "replacing crashed worker gw1\n"
    "=================================== FAILURES ===================================\n"
    "_______________________ tests/test_big.py::test_heavy ________________________\n"
    "[gw1] linux -- Python 3.14.0 /usr/bin/python3.14\n"
    "worker 'gw1' crashed while running 'tests/test_big.py::test_heavy'\n"
    "_____________________ tests/test_big.py::test_other[it's] _____________________\n"
    "[gw1] linux -- Python 3.14.0 /usr/bin/python3.14\n"
    "worker 'gw1' crashed while running \"tests/test_big.py::test_other[it's]\"\n"
)


def test_non_ci_bare_crash_rows_bind_to_the_failures_section() -> None:
    """A trimmed or message-less summary row still reads the full crash line."""
    output = _NON_CI_CRASH_OUTPUT + _pytest_summary(
        "FAILED tests/test_big.py::test_heavy - worker 'gw1' crashed while runni...\n",
        "FAILED tests/test_big.py::test_other[it's]\n",
        "FAIL Required test coverage of 100% not reached. Total coverage: 97.41%\n",
    )

    assert parse_gate_output("tests", output, "", 1).kind == "UNAVAILABLE"


def test_non_ci_crash_line_does_not_relabel_an_unrelated_bare_row() -> None:
    output = _NON_CI_CRASH_OUTPUT + _pytest_summary(
        "FAILED tests/test_big.py::test_heavy\n",
        "FAILED tests/test_real.py::test_bug\n",
    )

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_a_quoted_crash_line_in_a_traceback_is_not_a_crash_report() -> None:
    output = (
        "E   AssertionError: worker 'gw1' crashed while running "
        "'tests/test_x.py::test_d'\n"
    ) + _pytest_summary("FAILED tests/test_x.py::test_d\n")

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_qualified_exhaustion_types_are_recognised() -> None:
    output = _pytest_summary(
        "FAILED tests/test_x.py::test_a - builtins.OSError: [Errno 24] Too many open files\n",
        "FAILED tests/test_x.py::test_b - PermissionError: [Errno 28] No space left\n",
    )

    assert parse_gate_output("tests", output, "", 1).kind == "UNAVAILABLE"


# ---------------------------------------------------------------------------
# #4309 — the position-free progress vocabulary and the full enumeration count.
# ---------------------------------------------------------------------------


def test_progress_key_drops_ruff_line_and_column() -> None:
    """The locating identity keeps the position; the progress key does not."""
    obs = parse_gate_output("ruff", "maestro/x.py:4:1: F401 `os`  imported\n", "", 1)
    failure = obs.failures[0]
    assert failure.code == "F401"
    assert failure.identity() == "ruff:maestro/x.py:4:1:F401"
    assert failure.progress_key() == "ruff:maestro/x.py:F401"


def test_progress_key_of_a_pytest_row_is_its_identity() -> None:
    """A nodeid is already position-free, so it is returned unchanged."""
    failure = GateFailure(
        tool="tests", kind="FAILED", location="tests/test_a.py",
        identifier="tests/test_a.py::test_x", message="boom",
    )
    assert failure.progress_key() == failure.identity()


def test_clean_gate_has_empty_progress_signature_and_zero_count() -> None:
    obs = parse_gate_output("mypy", "Success", "", 0)
    assert obs.progress_signature() == frozenset()
    assert obs.progress_count() == 0


def test_progress_signature_keeps_the_synthetic_non_answer_identity() -> None:
    """A timed-out gate keeps ``tool:TIMEOUT`` beside its salvaged rows, and
    counts it, so it can never read as fewer failures than the gate answering."""
    obs = parse_non_answer(
        "mypy", "TIMEOUT", "maestro/x.py:3: error: Bad  [misc]\n", "",
    )
    assert obs.progress_signature() == frozenset({
        "mypy:maestro/x.py:misc", "mypy:TIMEOUT",
    })
    assert obs.progress_count() == 2


def test_unparsed_red_counts_its_synthetic_identity() -> None:
    obs = parse_gate_output("mypy", "garbage\n", "", 1)
    assert obs.progress_signature() == frozenset({"mypy:FAILED"})
    assert obs.progress_count() == 1


def test_hand_built_observation_counts_its_rows() -> None:
    """``enumerated_count`` unset reads as ``len(failures)``."""
    failure = GateFailure(
        tool="tests", kind="FAILED", location="tests/test_a.py",
        identifier="tests/test_a.py::test_x", message="",
    )
    obs = GateObservation(tool="tests", kind="FAILED", failures=(failure,), output="")
    assert obs.progress_count() == 1


def test_mypy_and_ruff_enumeration_count_past_the_cap() -> None:
    """The stored rows stop at the cap; the count and ``truncated`` do not."""
    total = _MAX_FAILURES + 7
    mypy = parse_gate_output("mypy", "".join(
        f"maestro/x.py:{n}: error: Bad  [misc]\n" for n in range(total)
    ), "", 1)
    ruff = parse_gate_output("ruff", "".join(
        f"maestro/x.py:{n}:1: E501 Line too long\n" for n in range(total)
    ), "", 1)
    run = GateRunObservation(gates=(mypy, ruff))
    assert len(mypy.failures) == _MAX_FAILURES
    assert mypy.enumerated_count == total
    assert len(ruff.failures) == _MAX_FAILURES
    assert ruff.enumerated_count == total
    assert run.any_truncated() is True
    assert run.enumerated_count() == 2 * total


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("+" * 35 + " Timeout " + "+" * 35, True),
        ("  " + "+" * 10 + " Timeout " + "+" * 10 + "\n", True),
        ("E   Failed: Timeout >5.0s", True),
        ("FAILED tests/test_x.py::test_y - Failed: Timeout >5.0s", True),
        ("E       Failed: Timeout >15s", True),
        ("E       Failed: Timeout (>1.0s) from pytest-timeout.", True),
        (
            "FAILED tests/t.py::test_slow - Failed: Timeout (>1.0s) from "
            "pytest-timeout.",
            True,
        ),
        # Unanchored failure text and ``-v`` progress lines whose parametrized
        # ids quote it are not kill evidence.
        ("Failed: Timeout >15s", False),
        (
            "tests/test_gate_observation.py::test_is_pytest_timeout_line"
            "[E   Failed: Timeout >5.0s-True] PASSED [ 50%]",
            False,
        ),
        (
            "tests/test_x.py::test_y[FAILED a - Failed: Timeout >5.0s-True] PASSED",
            False,
        ),
        ("tests/test_x.py::test_handles_Timeout PASSED", False),
        ('E   assert "+++++ Timeout +++++" == ""', False),
        ("+++", False),
        ("Timeout", False),
    ],
)
def test_is_pytest_timeout_line(line: str, expected: bool) -> None:
    """The thread banner and signal failure text match; quotes and names do not."""
    assert is_pytest_timeout_line(line) is expected


# ── Crashed-worker progress lines (OOM-killed xdist worker) ───────────────────

_NODE_DOWN = "[gw2] node down: Not properly terminated\n"
_CRASH_PROGRESS = "[gw2] [ 51%] FAILED tests/test_big.py::test_heavy\n"
_REPLACING = "replacing crashed worker gw2\n"
_CONTROLLER_KEYERROR = "INTERNALERROR> KeyError: <WorkerController gw2>\n"


def test_crashed_worker_progress_failure_is_the_unavailable_non_answer() -> None:
    output = (
        _NODE_DOWN + _CRASH_PROGRESS + _REPLACING
        + _pytest_summary("FAILED tests/test_big.py::test_heavy\n")
    )

    assert parse_gate_output("tests", output, "", 1).kind == "UNAVAILABLE"


def test_crashed_worker_with_loadscope_internalerror_and_no_summary_is_unavailable() -> None:
    output = (
        "[gw0] [ 10%] PASSED tests/test_ok.py::test_ok\n"
        + _NODE_DOWN + _CRASH_PROGRESS + _REPLACING
        + "INTERNALERROR> Traceback (most recent call last):\n"
        + _CONTROLLER_KEYERROR
    )

    assert parse_gate_output("tests", output, "", 1).kind == "UNAVAILABLE"


def test_genuine_progress_failure_alongside_a_crash_stays_red() -> None:
    output = (
        "[gw1] [ 20%] FAILED tests/test_real.py::test_bug\n"
        + _NODE_DOWN + _CRASH_PROGRESS + _REPLACING
    )

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_genuine_summary_row_alongside_a_crash_stays_red() -> None:
    output = _NODE_DOWN + _CRASH_PROGRESS + _pytest_summary(
        "FAILED tests/test_big.py::test_heavy\n",
        "FAILED tests/test_real.py::test_bug - AssertionError: 1 != 2\n",
    )

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_error_row_alongside_a_crash_stays_red() -> None:
    output = _NODE_DOWN + _CRASH_PROGRESS + "[gw1] [ 60%] ERROR tests/test_fx.py::test_setup\n"

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_failed_line_not_immediately_after_node_down_is_not_a_crash() -> None:
    output = _NODE_DOWN + _REPLACING + _CRASH_PROGRESS

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_failed_line_of_another_worker_after_node_down_is_not_a_crash() -> None:
    output = _NODE_DOWN + "[gw3] [ 51%] FAILED tests/test_big.py::test_heavy\n"

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_unrelated_internalerror_keeps_a_crash_red() -> None:
    output = (
        _NODE_DOWN + _CRASH_PROGRESS
        + "INTERNALERROR> ValueError: something else broke\n"
    )

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_controller_keyerror_for_a_worker_that_never_went_down_keeps_red() -> None:
    output = (
        _NODE_DOWN + _CRASH_PROGRESS
        + "INTERNALERROR> KeyError: <WorkerController gw5>\n"
    )

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"


def test_node_down_with_no_failure_at_all_is_not_unavailable() -> None:
    """A downed worker with nothing failing gives the predicate nothing to excuse."""
    output = _NODE_DOWN + _REPLACING

    assert parse_gate_output("tests", output, "", 1).kind != "UNAVAILABLE"


def test_unrelated_internalerror_does_not_veto_a_pre_existing_exhaustion_shape() -> None:
    """The INTERNALERROR veto guards only progress-bound crash evidence."""
    output = _pytest_summary(
        "FAILED tests/test_x.py::test_a - OSError: [Errno 28] No space left on device\n",
    ) + "INTERNALERROR> OSError: [Errno 28] No space left on device\n"

    assert parse_gate_output("tests", output, "", 1).kind == "UNAVAILABLE"


def test_progress_failure_with_a_spaced_parametrized_id_keeps_the_run_red() -> None:
    """A genuine unsummarised progress FAILED on ``test_x[a b]`` is still collected."""
    output = (
        "[gw1] [ 20%] FAILED tests/test_real.py::test_x[a b]\n"
        + _NODE_DOWN + _CRASH_PROGRESS + _REPLACING
    )

    assert parse_gate_output("tests", output, "", 1).kind == "FAILED"

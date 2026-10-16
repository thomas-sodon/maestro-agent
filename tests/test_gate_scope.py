"""Tests for the gate-scope oracle (proposal 134) — pure selection, no disk.

The policy half (:func:`maestro.gate_scope.select_impacted_tests`) takes its
corpus as an injected ``{relative test path: source text}`` mapping, so every
table below runs with zero I/O. The one disk-reading helper
(:func:`maestro.gate_scope.read_test_sources`) is exercised under ``mem_fs``
with ``Path.rglob`` patched, so no real filesystem is touched.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro.gate_observation import GateFailure, GateObservation, GateRunObservation
from maestro.gate_scope import (
    count_test_modules,
    impacted_tests,
    read_test_sources,
    restrict_signature,
    select_impacted_tests,
)

_SOURCE = "maestro"
_TESTS = "tests"


def _select(
    changed: list[str], sources: dict[str, str],
) -> tuple[str, ...]:
    """Run the pure selector with the self-shaped path atoms."""
    return select_impacted_tests(
        changed, sources, source_path=_SOURCE, tests_path=_TESTS,
    )


# ---------------------------------------------------------------------------
# The import-graph half — all three spellings
# ---------------------------------------------------------------------------


def test_plain_dotted_import_selects() -> None:
    """``import maestro.red_root`` selects the importing test module."""
    sources = {"tests/test_unrelated.py": "import maestro.red_root\n"}
    assert _select(["maestro/red_root.py"], sources) == ("tests/test_unrelated.py",)


def test_aliased_dotted_import_selects() -> None:
    """``import maestro.red_root as rr`` selects — the alias is not the name."""
    sources = {"tests/test_unrelated.py": "import maestro.red_root as rr\n"}
    assert _select(["maestro/red_root.py"], sources) == ("tests/test_unrelated.py",)


def test_from_dotted_module_import_selects() -> None:
    """``from maestro.red_root import CheckRun`` selects."""
    sources = {
        "tests/test_unrelated.py": "from maestro.red_root import CheckRun\n",
    }
    assert _select(["maestro/red_root.py"], sources) == ("tests/test_unrelated.py",)


def test_from_package_module_alias_selects() -> None:
    """``from maestro import red_root`` selects — the module is the alias."""
    sources = {"tests/test_unrelated.py": "from maestro import red_root\n"}
    assert _select(["maestro/red_root.py"], sources) == ("tests/test_unrelated.py",)


def test_unrelated_import_does_not_select() -> None:
    """Importing a sibling module is not a selection for the changed one."""
    sources = {"tests/test_unrelated.py": "from maestro import journal\n"}
    assert _select(["maestro/red_root.py"], sources) == ()


def test_relative_import_is_skipped() -> None:
    """A relative import carries no resolvable target and never selects."""
    sources = {"tests/test_unrelated.py": "from . import red_root\n"}
    assert _select(["maestro/red_root.py"], sources) == ()


def test_package_change_selects_every_submodule_importer() -> None:
    """A changed ``__init__.py`` maps to its package, matching submodules."""
    sources = {"tests/test_unrelated.py": "from maestro.journal import Journal\n"}
    assert _select(["maestro/__init__.py"], sources) == ("tests/test_unrelated.py",)


def test_subpackage_module_matches_its_dotted_name() -> None:
    """A changed subpackage file resolves to its full dotted name."""
    sources = {
        "tests/test_unrelated.py": "from maestro.loop.pr_remediation import x\n",
    }
    assert _select(["maestro/loop/pr_remediation.py"], sources) == (
        "tests/test_unrelated.py",
    )


# ---------------------------------------------------------------------------
# The naming-convention half — the ``_``-bounded prefix rule
# ---------------------------------------------------------------------------


def test_exact_paired_test_selects_without_importing() -> None:
    """``tests/test_memory.py`` selects for ``maestro/memory.py`` on name alone."""
    sources = {"tests/test_memory.py": "x = 1\n"}
    assert _select(["maestro/memory.py"], sources) == ("tests/test_memory.py",)


def test_underscore_bounded_prefix_selects() -> None:
    """``test_memory_archive.py`` belongs to the ``memory`` family."""
    sources = {"tests/test_memory_archive.py": "x = 1\n"}
    assert _select(["maestro/memory.py"], sources) == (
        "tests/test_memory_archive.py",
    )


def test_raw_prefix_share_does_not_select() -> None:
    """``test_memorial.py`` shares a raw prefix but not a ``_``-bounded one."""
    sources = {"tests/test_memorial.py": "x = 1\n"}
    assert _select(["maestro/memory.py"], sources) == ()


def test_newly_added_module_is_visible_to_the_family_rule() -> None:
    """The family comes from the changed module, not a static registry."""
    sources = {"tests/test_brand_new_thing.py": "x = 1\n"}
    assert _select(["maestro/brand_new.py"], sources) == (
        "tests/test_brand_new_thing.py",
    )


# ---------------------------------------------------------------------------
# The "full" sentinel and the changed-path rejections
# ---------------------------------------------------------------------------


def test_empty_changed_paths_yields_the_full_sentinel() -> None:
    """No changed path at all means "do not narrow"."""
    sources = {"tests/test_memory.py": "from maestro import memory\n"}
    assert _select([], sources) == ()


def test_non_python_changed_path_contributes_nothing() -> None:
    """A docs-only diff yields the full sentinel, not a narrowed empty run."""
    sources = {"tests/test_memory.py": "from maestro import memory\n"}
    assert _select(["README.md", "maestro/prompts/defaults.md"], sources) == ()


def test_changed_test_file_contributes_nothing() -> None:
    """A test file lives outside ``source_path``, so it selects nothing."""
    sources = {"tests/test_memory.py": "from maestro import memory\n"}
    assert _select(["tests/test_memory.py"], sources) == ()


def test_changed_file_outside_source_path_contributes_nothing() -> None:
    """A file in a sibling package is out of scope for this gate's atoms."""
    sources = {"tests/test_memory.py": "from maestro import memory\n"}
    assert _select(["maestro_review/runner.py"], sources) == ()


def test_in_scope_change_selecting_nothing_still_yields_the_sentinel() -> None:
    """An in-scope change no test reaches returns the empty "full" tuple."""
    sources = {"tests/test_other.py": "from maestro import journal\n"}
    assert _select(["maestro/red_root.py"], sources) == ()


def test_source_prefixed_test_file_named_test_is_rejected_by_stem() -> None:
    """A ``maestro/test_helper.py`` never bridges to a paired test of its own."""
    sources = {"tests/test_helper.py": "x = 1\n"}
    assert _select(["maestro/test_helper.py"], sources) == ()


# ---------------------------------------------------------------------------
# Corpus shape, ordering, dedupe, and parse tolerance
# ---------------------------------------------------------------------------


def test_selection_is_sorted_and_deduped() -> None:
    """Both halves union into one sorted selection with no repeats."""
    sources = {
        "tests/test_memory.py": "from maestro import memory\n",
        "tests/test_alpha.py": "import maestro.memory\n",
    }
    assert _select(["maestro/memory.py", "maestro/memory.py"], sources) == (
        "tests/test_alpha.py",
        "tests/test_memory.py",
    )


def test_corpus_entry_outside_tests_path_is_ignored() -> None:
    """Only files under ``tests_path`` are selectable."""
    sources = {"scripts/test_memory.py": "from maestro import memory\n"}
    assert _select(["maestro/memory.py"], sources) == ()


def test_non_python_corpus_entry_is_ignored() -> None:
    """A non-``.py`` file under the tests tree is never a test module."""
    sources = {"tests/fixtures.json": "{}"}
    assert _select(["maestro/memory.py"], sources) == ()


def test_nested_test_module_selects() -> None:
    """A subdirectory test module participates in both halves."""
    sources = {"tests/component/test_memory.py": "x = 1\n"}
    assert _select(["maestro/memory.py"], sources) == (
        "tests/component/test_memory.py",
    )


def test_unparseable_test_module_is_skipped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A syntax error skips the import half rather than crashing the selector."""
    sources = {"tests/test_broken.py": "def (:\n"}
    with caplog.at_level(logging.WARNING):
        assert _select(["maestro/memory.py"], sources) == ()
    assert "unparseable test module" in caplog.text


def test_unparseable_module_still_selects_on_its_name() -> None:
    """The stem half reads the filename, so a broken body still selects."""
    sources = {"tests/test_memory.py": "def (:\n"}
    assert _select(["maestro/memory.py"], sources) == ("tests/test_memory.py",)


def test_leading_dot_slash_path_is_normalized() -> None:
    """A ``./maestro/memory.py`` still matches the source prefix."""
    sources = {"tests/test_memory.py": "x = 1\n"}
    assert _select(["./maestro/memory.py"], sources) == ("tests/test_memory.py",)


def test_trailing_slash_path_atoms_are_tolerated() -> None:
    """``maestro/`` and ``tests/`` atoms behave as their bare forms do."""
    selected = select_impacted_tests(
        ["maestro/memory.py"],
        {"tests/test_memory.py": "x = 1\n"},
        source_path="maestro/",
        tests_path="tests/",
    )
    assert selected == ("tests/test_memory.py",)


def test_one_shot_iterator_changed_paths_still_selects_the_import_half() -> None:
    """A generator argument is materialized once, so both halves see it.

    ``changed_paths`` is typed ``Iterable[str]``, so a one-shot iterator is a
    legitimate argument. Both halves consume it, and the import half runs
    second — if the argument were iterated twice the generator would already be
    exhausted and the import-graph selection would silently vanish.
    """
    sources = {"tests/test_unrelated.py": "import maestro.red_root\n"}
    selected = select_impacted_tests(
        (path for path in ["maestro/red_root.py"]),
        sources,
        source_path=_SOURCE,
        tests_path=_TESTS,
    )
    assert selected == ("tests/test_unrelated.py",)


# ---------------------------------------------------------------------------
# The confined disk reader and the ``impacted_tests`` wrapper
# ---------------------------------------------------------------------------


def test_read_test_sources_reads_relative_keys(mem_fs: dict[str, str]) -> None:
    """Keys are repo-relative, so the pure selector never sees an absolute path."""
    workspace = Path("/fake/ws")
    found = workspace / "tests" / "test_memory.py"
    mem_fs[str(found)] = "from maestro import memory\n"
    with patch.object(Path, "rglob", return_value=[found]):
        sources = read_test_sources(workspace, "tests")
    assert sources == {"tests/test_memory.py": "from maestro import memory\n"}


def test_read_test_sources_skips_unreadable_file(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """An ``OSError`` skips that one file with a warning, never a broad silence."""
    workspace = Path("/fake/ws")
    good = workspace / "tests" / "test_memory.py"
    bad = workspace / "tests" / "test_bad.py"
    mem_fs[str(good)] = "x = 1\n"

    def _read(self: Path, encoding: str | None = None, **kwargs: object) -> str:
        if self == bad:
            raise OSError("permission denied")
        return mem_fs[str(self)]

    with patch.object(Path, "rglob", return_value=[good, bad]), \
         patch.object(Path, "read_text", _read), \
         caplog.at_level(logging.WARNING):
        sources = read_test_sources(workspace, "tests")

    assert sources == {"tests/test_memory.py": "x = 1\n"}
    assert "unreadable test file" in caplog.text


def test_read_test_sources_skips_undecodable_file(mem_fs: dict[str, str]) -> None:
    """A ``UnicodeDecodeError`` is tolerated the same narrow way."""
    workspace = Path("/fake/ws")
    bad = workspace / "tests" / "test_bad.py"

    def _read(self: Path, encoding: str | None = None, **kwargs: object) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    with patch.object(Path, "rglob", return_value=[bad]), \
         patch.object(Path, "read_text", _read):
        assert read_test_sources(workspace, "tests") == {}


def test_read_test_sources_falls_back_to_absolute_key(
    mem_fs: dict[str, str],
) -> None:
    """A path outside the workspace keeps its absolute key rather than raising."""
    workspace = Path("/fake/ws")
    outside = Path("/elsewhere/tests/test_memory.py")
    mem_fs[str(outside)] = "x = 1\n"
    with patch.object(Path, "rglob", return_value=[outside]):
        sources = read_test_sources(workspace, "tests")
    assert sources == {"/elsewhere/tests/test_memory.py": "x = 1\n"}


def test_impacted_tests_delegates_to_the_pure_selector(
    mem_fs: dict[str, str],
) -> None:
    """The wrapper reads the tree once and returns the pure selection."""
    workspace = Path("/fake/ws")
    found = workspace / "tests" / "test_memory.py"
    mem_fs[str(found)] = "x = 1\n"
    with patch.object(Path, "rglob", return_value=[found]):
        selected = impacted_tests(
            ["maestro/memory.py"],
            workspace=workspace,
            source_path="maestro",
            tests_path="tests",
        )
    assert selected == ("tests/test_memory.py",)


def test_impacted_tests_returns_the_full_sentinel_on_a_docs_diff(
    mem_fs: dict[str, str],
) -> None:
    """A docs-only diff narrows nothing through the wrapper either."""
    workspace = Path("/fake/ws")
    found = workspace / "tests" / "test_memory.py"
    mem_fs[str(found)] = "x = 1\n"
    with patch.object(Path, "rglob", return_value=[found]):
        selected = impacted_tests(
            ["README.md"],
            workspace=workspace,
            source_path="maestro",
            tests_path="tests",
        )
    assert selected == ()


# ---------------------------------------------------------------------------
# Candidate rule — only collectable ``test_`` modules (ledger r1/r2)
# ---------------------------------------------------------------------------


def test_conftest_importing_the_changed_module_is_not_selected() -> None:
    """A ``conftest.py`` collects no tests, so the import half must skip it.

    Regression: the import branch used to run for every ``.py`` under the tests
    tree, so a conftest importing the changed module landed in the selection —
    a path a later consumer would splice into pytest argv for zero tests.
    """
    sources = {"tests/conftest.py": "import maestro.red_root\n"}
    assert _select(["maestro/red_root.py"], sources) == ()


def test_tests_support_helper_importing_the_changed_module_is_not_selected(
) -> None:
    """A ``tests/support/`` helper is excluded for the same reason."""
    sources = {"tests/support/fakes.py": "from maestro import red_root\n"}
    assert _select(["maestro/red_root.py"], sources) == ()


def test_test_module_importing_the_changed_module_still_selects() -> None:
    """The exclusion is by name, not by directory — a nested test still selects."""
    sources = {
        "tests/conftest.py": "import maestro.red_root\n",
        "tests/component/test_flow.py": "import maestro.red_root\n",
    }
    assert _select(["maestro/red_root.py"], sources) == (
        "tests/component/test_flow.py",
    )


def test_transitive_import_is_not_selected() -> None:
    """The import half is one hop: an intermediate importer does not bridge.

    Documented limitation — ``tests/test_flow.py`` imports ``maestro.agent``,
    which imports the changed module, and is deliberately not selected.
    """
    sources = {"tests/test_flow.py": "import maestro.agent\n"}
    assert _select(["maestro/red_root.py"], sources) == ()


def test_count_test_modules_counts_only_test_prefixed_modules() -> None:
    """The fraction's denominator excludes conftests, helpers and ``__init__``."""
    sources = {
        "tests/test_memory.py": "",
        "tests/component/test_flow.py": "",
        "tests/conftest.py": "",
        "tests/__init__.py": "",
        "tests/support/fakes.py": "",
    }
    assert count_test_modules(sources, tests_path=_TESTS) == 2


def test_count_test_modules_ignores_entries_outside_the_tests_tree() -> None:
    """A non-``.py`` entry and a path outside *tests_path* are both excluded."""
    sources = {
        "tests/test_memory.py": "",
        "tests/fixture.json": "",
        "maestro/test_helper.py": "",
    }
    assert count_test_modules(sources, tests_path=_TESTS) == 1


# ---------------------------------------------------------------------------
# restrict_signature — projecting a full-scope red onto a narrowed suite
# ---------------------------------------------------------------------------


def _tests_failure(location: str, identifier: str) -> GateFailure:
    """One enumerated pytest failure at *location*."""
    return GateFailure(
        tool="tests", kind="FAILED", location=location,
        identifier=identifier, message="boom",
    )


def _run(*gates: GateObservation) -> GateRunObservation:
    """Wrap *gates* as one run observation."""
    return GateRunObservation(gates=gates)


def test_restrict_signature_retains_every_non_tests_identity() -> None:
    """Ruff and mypy still run whole, so their identities survive verbatim.

    A narrowed round narrows only the pytest argv; dropping a ruff diagnostic from
    the baseline would let the next round read it as newly cleared.
    """
    ruff = GateObservation(
        tool="ruff", kind="FAILED",
        failures=(
            GateFailure(
                tool="ruff", kind="FAILED", location="maestro/a.py",
                identifier="maestro/a.py:1:1:F401", message="unused",
                code="F401",
            ),
        ),
        output="",
    )
    mypy = GateObservation(
        tool="mypy", kind="FAILED",
        failures=(
            GateFailure(
                tool="mypy", kind="FAILED", location="maestro/b.py",
                identifier="maestro/b.py:9:arg-type", message="bad",
                code="arg-type",
            ),
        ),
        output="",
    )
    # Projected through the position-free progress vocabulary the convergence
    # loop compares (#4309), so no line/column appears in the member.
    assert restrict_signature(_run(ruff, mypy), ["tests/test_a.py"]) == frozenset({
        "ruff:maestro/a.py:F401", "mypy:maestro/b.py:arg-type",
    })


def test_restrict_signature_keeps_in_scope_and_drops_out_of_scope_tests() -> None:
    """Only the pytest failures the narrowed suite would re-observe survive."""
    tests = GateObservation(
        tool="tests", kind="FAILED",
        failures=(
            _tests_failure("tests/test_alpha.py", "tests/test_alpha.py::test_one"),
            _tests_failure("tests/test_beta.py", "tests/test_beta.py::test_two"),
        ),
        output="",
    )
    assert restrict_signature(_run(tests), ["tests/test_alpha.py"]) == frozenset({
        "tests:tests/test_alpha.py::test_one",
    })


def test_restrict_signature_drops_the_coverage_identity() -> None:
    """A narrowed round runs ``--no-cov``, so a coverage shortfall is unobservable.

    Retaining it would pin the baseline permanently non-empty and let every
    narrowed round read as a stall.
    """
    tests = GateObservation(
        tool="tests", kind="FAILED",
        failures=(
            GateFailure(
                tool="tests", kind="FAILED", location="",
                identifier="coverage", message="97%",
            ),
            _tests_failure("tests/test_alpha.py", "tests/test_alpha.py::test_one"),
        ),
        output="",
    )
    assert restrict_signature(_run(tests), ["tests/test_alpha.py"]) == frozenset({
        "tests:tests/test_alpha.py::test_one",
    })


def test_restrict_signature_drops_a_synthetic_non_answer_identity() -> None:
    """A killed whole-suite run says nothing about whether the narrowed probe answers.

    The lone ``tests:TIMEOUT`` identity leaves an empty restriction — the "do not
    narrow" signal the caller reads.
    """
    tests = GateObservation(tool="tests", kind="TIMEOUT", failures=(), output="")
    assert restrict_signature(_run(tests), ["tests/test_alpha.py"]) == frozenset()


def test_restrict_signature_retains_a_non_answer_of_the_run_it_restricts() -> None:
    """``retain_non_answer`` keeps the synthetic identity of the narrowed run itself.

    Regression: the narrowed round's own timed-out probe restricted to the empty
    set, which the convergence loop reads as maximal progress — from a gate that
    never finished.
    """
    tests = GateObservation(tool="tests", kind="TIMEOUT", failures=(), output="")
    assert restrict_signature(
        _run(tests), ["tests/test_alpha.py"], retain_non_answer=True,
    ) == frozenset({"tests:TIMEOUT"})


def test_restrict_signature_retains_a_non_answer_beside_salvaged_rows() -> None:
    """A narrowed non-answer keeps "and it did not answer" alongside what it salvaged.

    Otherwise a probe killed with one salvaged in-scope row would read as a
    strictly smaller set than a completed round with two.
    """
    tests = GateObservation(
        tool="tests", kind="TIMEOUT",
        failures=(
            _tests_failure("tests/test_alpha.py", "tests/test_alpha.py::one"),
            _tests_failure("tests/test_far.py", "tests/test_far.py::two"),
        ),
        output="",
    )
    assert restrict_signature(
        _run(tests), ["tests/test_alpha.py"], retain_non_answer=True,
    ) == frozenset({"tests:TIMEOUT", "tests:tests/test_alpha.py::one"})


def test_restrict_signature_retains_an_unparsed_red_of_its_own_run() -> None:
    """A non-``CLEAN`` gate that enumerated nothing keeps its synthetic identity too.

    Mirrors ``GateObservation.signature``'s fail-closed rule exactly: an
    unrecognised red must never restrict to the empty "all clear" set.
    """
    tests = GateObservation(tool="tests", kind="FAILED", failures=(), output="")
    assert restrict_signature(
        _run(tests), ["tests/test_alpha.py"], retain_non_answer=True,
    ) == frozenset({"tests:FAILED"})


def test_restrict_signature_retain_flag_adds_nothing_to_a_clean_gate() -> None:
    """A ``CLEAN`` tests gate stays empty under ``retain_non_answer``."""
    tests = GateObservation(tool="tests", kind="CLEAN", failures=(), output="")
    assert restrict_signature(
        _run(tests), ["tests/test_alpha.py"], retain_non_answer=True,
    ) == frozenset()


def test_restrict_signature_is_empty_when_the_red_is_wholly_out_of_scope() -> None:
    """Every failure outside the selection restricts to nothing."""
    tests = GateObservation(
        tool="tests", kind="FAILED",
        failures=(_tests_failure("tests/test_beta.py", "tests/test_beta.py::test_two"),),
        output="",
    )
    assert restrict_signature(_run(tests), ["tests/test_alpha.py"]) == frozenset()


def test_restrict_signature_matches_a_bare_basename_location() -> None:
    """A runner that reports a bare module name still matches the selection.

    Only when the location carries no directory at all — a qualified path must
    match the selected path exactly, so two same-named modules in different
    directories cannot be conflated.
    """
    tests = GateObservation(
        tool="tests", kind="FAILED",
        failures=(
            _tests_failure("test_alpha.py", "test_alpha.py::test_one"),
            _tests_failure("other/test_alpha.py", "other/test_alpha.py::test_two"),
        ),
        output="",
    )
    assert restrict_signature(_run(tests), ["tests/test_alpha.py"]) == frozenset({
        "tests:test_alpha.py::test_one",
    })


def test_restrict_signature_of_an_empty_selection_drops_every_test_identity() -> None:
    """No selection means nothing is in scope — the caller's do-not-narrow path."""
    tests = GateObservation(
        tool="tests", kind="FAILED",
        failures=(_tests_failure("tests/test_alpha.py", "tests/test_alpha.py::test_one"),),
        output="",
    )
    assert restrict_signature(_run(tests), []) == frozenset()


def test_restrict_signature_drops_a_locationless_failure() -> None:
    """A failure naming no file cannot be attributed to a selected module.

    A collection abort that printed no path is not a coverage shortfall — it has
    its own identifier — but it is equally unattributable, so it is dropped rather
    than being carried into a baseline the narrowed probe can never clear.
    """
    tests = GateObservation(
        tool="tests", kind="FAILED",
        failures=(
            GateFailure(
                tool="tests", kind="FAILED", location="",
                identifier="INTERNALERROR", message="aborted",
            ),
        ),
        output="",
    )
    assert restrict_signature(_run(tests), ["tests/test_alpha.py"]) == frozenset()

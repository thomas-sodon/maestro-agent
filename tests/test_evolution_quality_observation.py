"""Attributed gate observation in the quality pipeline (proposal 124,
``maestro/evolution_quality.py``); split out of ``tests/test_evolution.py``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro import gate_capacity, gate_environment
from maestro.config import Config
from maestro.consciousness import Consciousness
from maestro.evolution import (
    EvolutionEngine,
    QualityGateResult,
)
from maestro.gate_observation import (
    Attribution,
    GateFailure,
    GateObservation,
    GateRunObservation,
)
from maestro.issues import IssueTracker
from maestro.journal import Journal
from maestro.memory import (
    EvolutionPlan,
    Memory,
)
from maestro.notifications.base import NullNotifier
from tests.test_evolution import (
    _make,
    _observing_engine,
)

#: A red four-failure pytest summary, as the runner would capture it on stdout.
_FOUR_FAILURES = (
    "FAILED tests/test_alpha.py::test_one - ValueError: bad\n"
    "FAILED tests/test_alpha.py::test_two - KeyError: 'k'\n"
    "FAILED tests/test_beta.py::test_three - TypeError: nope\n"
    "FAILED tests/test_beta.py::test_four\n"
)


def _ruff_red_observation() -> GateRunObservation:
    """A one-failure red ruff observation whose failure sits in ``maestro/x.py``."""
    return GateRunObservation(
        gates=(
            GateObservation(
                tool="ruff", kind="FAILED",
                failures=(
                    GateFailure(
                        tool="ruff", kind="FAILED", location="maestro/x.py",
                        identifier="ruff:maestro/x.py:1:0:E501", message="",
                    ),
                ),
                output="",
            ),
        ),
    )


def _attribute(
    e: EvolutionEngine, review: QualityGateResult,
) -> tuple[Attribution | None, gate_environment.GateRunnabilityVerdict | None]:
    """Call ``_attribute_review`` with a stubbed VCS whose base→HEAD diff is
    ``maestro/x.py`` — the path the red observation's failure sits in.

    The reader derives the changed paths from the VCS, not from the plan's
    declared ``files_affected``, so every flag-on case has to supply a head and a
    diff; no subprocess runs because both reads are patched.
    """
    with (
        patch.object(e._vcs, "get_head_hash", return_value="head1"),
        patch.object(e._vcs, "changed_paths", return_value=("maestro/x.py",)),
    ):
        return e._attribute_review(review, "base1")


def test_attribute_review_flag_off_returns_none_without_baseline(
    mem_fs: dict[str, str],
) -> None:
    """Flag-off, ``_attribute_review`` is the byte-identical pre-124 path: it
    returns ``None`` before ever consulting ``_observe_baseline`` — even when the
    review happens to carry an observation."""
    e, _m, _c, _j = _make(mem_fs)
    assert e._config.gate_observation_enabled is False
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red", observation=_ruff_red_observation(),
    )

    with patch.object(e, "_observe_baseline") as baseline:
        verdict, runnability = e._attribute_review(review, "deadbeef")

    assert verdict is None
    assert runnability is None
    baseline.assert_not_called()


def test_attribute_review_flag_on_no_observation_returns_none(
    mem_fs: dict[str, str],
) -> None:
    """Flag-on but no observation attached, ``_attribute_review`` returns ``None``
    without consulting the baseline — the defensive fallback the terminal's inline
    derivation used to spell as ``if observation is not None``."""
    e = _observing_engine()
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red",  # observation defaults to None
    )

    with patch.object(e, "_observe_baseline") as baseline:
        verdict, runnability = e._attribute_review(review, "deadbeef")

    assert verdict is None
    assert runnability is None
    baseline.assert_not_called()


def test_attribute_review_flag_on_with_observation_attributes(
    mem_fs: dict[str, str],
) -> None:
    """Flag-on with an observation, ``_attribute_review`` classifies the red via
    ``attribute`` — ATTRIBUTED when an enumerated failure sits in a changed path."""
    e = _observing_engine()
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red", observation=_ruff_red_observation(),
    )

    # ``_observe_baseline`` is a cache miss on the stub base (no subprocess, the
    # gate is never run here); the failure location is in the VCS-derived diff, so
    # ``attribute`` short-circuits to ATTRIBUTED before the baseline would matter.
    verdict, runnability = _attribute(e, review)

    assert verdict == "ATTRIBUTED"
    assert runnability is None


def test_attribute_review_passes_the_cached_verdict_flag_on(
    mem_fs: dict[str, str],
) -> None:
    """Flag-on, the cached runnability verdict reaches ``attribute`` — read, not
    probed: ``read_verdict`` is consulted and ``refresh_verdict`` never is, so the
    convergence reader issues no subprocess (proposal 133)."""
    e = _observing_engine(
        Config(gate_observation_enabled=True, gate_environment_oracle_enabled=True),
    )
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red", observation=_ruff_red_observation(),
    )
    cached = gate_environment.GateRunnabilityVerdict(
        kind="UNCOLLECTABLE",
        fingerprint="fp",
        identities=_ruff_red_observation().signature(),
    )

    with (
        patch.object(
            gate_environment, "read_verdict", return_value=cached,
        ) as read,
        patch.object(gate_environment, "refresh_verdict") as refresh,
    ):
        verdict, runnability = _attribute(e, review)

    assert verdict == "ENVIRONMENT"
    # The verdict is *returned* alongside the attribution, not left for the
    # terminal to re-read: that is what keeps ``ConvergenceResult.runnability``
    # non-``None`` exactly when the attribution is ``ENVIRONMENT``.
    assert runnability is cached
    assert read.call_count == 1
    refresh.assert_not_called()


def test_attribute_review_reads_no_verdict_flag_off(mem_fs: dict[str, str]) -> None:
    """Flag-off, no verdict is read at all and the attribution is the pre-133 one."""
    e = _observing_engine()
    assert e._config.gate_environment_oracle_enabled is False
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red", observation=_ruff_red_observation(),
    )

    with patch.object(gate_environment, "read_verdict") as read:
        verdict, runnability = _attribute(e, review)

    assert verdict == "ATTRIBUTED"
    assert runnability is None
    read.assert_not_called()


def test_attribute_review_flag_on_with_no_cached_row_is_unchanged(
    mem_fs: dict[str, str],
) -> None:
    """Flag-on but nothing probed yet, ``None`` is passed and nothing changes."""
    e = _observing_engine(
        Config(gate_observation_enabled=True, gate_environment_oracle_enabled=True),
    )
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red", observation=_ruff_red_observation(),
    )

    with patch.object(gate_environment, "read_verdict", return_value=None):
        verdict, runnability = _attribute(e, review)

    assert verdict == "ATTRIBUTED"
    assert runnability is None


def test_attribute_review_ignores_over_declared_plan_paths(
    mem_fs: dict[str, str],
) -> None:
    """A path the plan *declares* but the branch never touched cannot make a
    base-inherited red the diff's fault (#3793).

    ``plan.files_affected`` is model-authored proposal metadata, not a
    measurement: before the fix it was handed to ``attribute`` as "the diff", so
    an over-declared path turned an ``INHERITED`` red into ``ATTRIBUTED`` and
    spent an implementation attempt on a failure the branch never caused.
    """
    e = _observing_engine()
    observation = _ruff_red_observation()
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red", observation=observation,
    )

    with (
        patch.object(e._vcs, "get_head_hash", return_value="head1"),
        patch.object(e._vcs, "changed_paths", return_value=()) as changed,
        patch.object(e, "_observe_baseline", return_value=observation),
    ):
        verdict, runnability = e._attribute_review(review, "base1")

    assert verdict == "INHERITED"
    assert runnability is None
    changed.assert_called_once_with("base1", "head1")


def test_attribute_review_reads_under_declared_touched_paths(
    mem_fs: dict[str, str],
) -> None:
    """A path the branch touched but the plan never declared still attributes.

    The mirror of the over-declared case: with an empty ``files_affected`` the
    old reader could never reach the ``ATTRIBUTED`` arm, so a genuinely caused
    red slipped past as the base's.
    """
    e = _observing_engine()
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red", observation=_ruff_red_observation(),
    )

    with (
        patch.object(e._vcs, "get_head_hash", return_value="head1"),
        patch.object(e._vcs, "changed_paths", return_value=("maestro/x.py",)),
        patch.object(e, "_observe_baseline", return_value=None),
    ):
        verdict, _runnability = e._attribute_review(review, "base1")

    assert verdict == "ATTRIBUTED"


def test_attribute_review_missing_head_is_unattributable(
    mem_fs: dict[str, str],
) -> None:
    """No head to diff against short-circuits to ``UNATTRIBUTABLE``, no diff read."""
    e = _observing_engine()
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red", observation=_ruff_red_observation(),
    )

    with (
        patch.object(e._vcs, "get_head_hash", return_value=None),
        patch.object(e._vcs, "changed_paths") as changed,
        patch.object(e, "_observe_baseline") as baseline,
    ):
        verdict, runnability = e._attribute_review(review, "base1")

    assert verdict == "UNATTRIBUTABLE"
    assert runnability is None
    changed.assert_not_called()
    baseline.assert_not_called()


def test_attribute_review_missing_base_is_unattributable(
    mem_fs: dict[str, str],
) -> None:
    """No base commit means no boundary to measure the red against."""
    e = _observing_engine()
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red", observation=_ruff_red_observation(),
    )

    with (
        patch.object(e._vcs, "get_head_hash", return_value="head1"),
        patch.object(e._vcs, "changed_paths") as changed,
        patch.object(e, "_observe_baseline") as baseline,
    ):
        verdict, runnability = e._attribute_review(review, None)

    assert verdict == "UNATTRIBUTABLE"
    assert runnability is None
    changed.assert_not_called()
    baseline.assert_not_called()


def test_attribute_review_missing_base_still_yields_environment(
    mem_fs: dict[str, str],
) -> None:
    """An environment-owned red keeps ``ENVIRONMENT`` even with no base to diff.

    The missing-head/base case routes through ``attribute`` with an empty diff
    and no baseline rather than returning ``UNATTRIBUTABLE`` directly, because
    the proposal-133 arm reads only the runnability verdict — outranking every
    boundary-derived verdict — and a short-circuit above it would charge a
    ``failure_count`` for a gate that measurably never judged the diff.
    """
    e = _observing_engine(
        Config(gate_observation_enabled=True, gate_environment_oracle_enabled=True),
    )
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red", observation=_ruff_red_observation(),
    )
    cached = gate_environment.GateRunnabilityVerdict(
        kind="UNPROVISIONED", fingerprint="fp", identities=frozenset(),
    )

    with (
        patch.object(gate_environment, "read_verdict", return_value=cached),
        patch.object(e._vcs, "get_head_hash", return_value=None),
        patch.object(e._vcs, "changed_paths") as changed,
        patch.object(e, "_observe_baseline") as baseline,
    ):
        verdict, runnability = e._attribute_review(review, "base1")

    assert verdict == "ENVIRONMENT"
    assert runnability is cached
    changed.assert_not_called()
    baseline.assert_not_called()


def test_attribute_review_flag_off_touches_no_vcs(mem_fs: dict[str, str]) -> None:
    """Flag-off the reader stays byte-identical: ``(None, None)``, no VCS read."""
    e, _m, _c, _j = _make(mem_fs)
    assert e._config.gate_observation_enabled is False
    review = QualityGateResult(
        passed=False, ruff_ok=False, mypy_ok=True, tests_ok=True,
        raw_output="ruff red", observation=_ruff_red_observation(),
    )

    with (
        patch.object(e._vcs, "get_head_hash") as head,
        patch.object(e._vcs, "changed_paths") as changed,
    ):
        verdict, runnability = e._attribute_review(review, "base1")

    assert verdict is None
    assert runnability is None
    head.assert_not_called()
    changed.assert_not_called()


def test_run_quality_gates_flag_off_is_byte_identical(mem_fs: dict[str, str]) -> None:
    """Flag-off, the argv and ``raw_output`` match the pre-124 runner exactly.

    The rollback guarantee: proposal 124 adds a *fact*, not a behaviour change, so
    with the flag off the recorded commands, the report string (including the
    ``"could not run"`` non-answer text), and the three booleans must be
    indistinguishable from before — and ``observation`` must be ``None``.
    """
    e, _m, _c, _j = _make(mem_fs)
    # gate_lease_enabled now defaults on (graduated to fix #3760); this test is
    # the flag-*off* rollback guarantee, so pin it off explicitly — the byte-for-
    # byte pre-lease argv is what must survive on the documented rollback path.
    e._config.gate_lease_enabled = False
    assert e._config.gate_observation_enabled is False
    assert e._config.gate_lease_enabled is False

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "mypy" in cmd:
            raise subprocess.TimeoutExpired(cmd, 60)
        if "pytest" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout=_FOUR_FAILURES, stderr="",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect) as mock_run:
        result = e.run_quality_gates()

    assert result.observation is None
    # No enumerate re-run: exactly the three probes, in the pre-124 argv shape —
    # asserted as whole argv lists, since a substring check would not notice a
    # dropped or reordered flag, which is the rollback guarantee this test is for.
    assert mock_run.call_count == 3
    assert mock_run.call_args_list[0].args[0] == ["ruff", "check", "maestro/", "tests/", "scripts/"]
    assert mock_run.call_args_list[1].args[0] == ["mypy", "maestro/", "scripts/"]
    assert mock_run.call_args_list[2].args[0] == [
        "pytest", "tests/", "-x", "--tb=short", "-q",
        "--cov=maestro", f"--cov-fail-under={e._config.cov_fail_under}",
    ]
    assert result.passed is False
    assert result.ruff_ok is True
    assert result.mypy_ok is False
    assert result.tests_ok is False
    # The whole report string, not three substrings: the ``"could not run"`` text,
    # the ``"<label> issues:"`` prefix, the blank-line join and the untouched
    # 1000-char tail are all part of what must not drift.
    assert result.raw_output == (
        "Ruff: clean\n\nMypy: could not run\n\nTests issues:\n" + _FOUR_FAILURES
    )
    # The enforced budget is part of the rollback guarantee too (proposal 134):
    # every gate, the suite included, runs under the flat ``selfcheck_timeout``.
    assert [call.kwargs["timeout"] for call in mock_run.call_args_list] == [
        e._config.selfcheck_timeout,
    ] * 3
    # Proposal 134's scope seam defaults empty, so the no-argument call above is
    # the whole rollback guarantee: the ``--cov`` atoms are present, ``--no-cov``
    # is absent, and no gate's enforced timeout moved.
    assert e._config.gate_scope_impacted_enabled is False
    assert "--no-cov" not in mock_run.call_args_list[2].args[0]


def test_run_quality_gates_scope_narrows_only_the_tests_argv(
    mem_fs: dict[str, str],
) -> None:
    """A non-empty ``scope`` narrows the pytest argv and stamps ``scope="impacted"``.

    The selected modules replace the whole ``tests/`` atom, the two ``--cov`` atoms
    give way to ``--no-cov`` (a coverage floor is not observable over a subset),
    ruff and mypy argv are untouched, the ``-x``-less enumerate re-run derives from
    the narrowed probe, and the telemetry row is marked narrowed so it never enters
    ``derive_deadline`` or ``measure-gate-contention``.
    """
    d = Path("/fake/evo_scope")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(
        m, c, j, n, issues=issues,
        config=Config(gate_observation_enabled=True, gate_lease_enabled=True),
    )

    captured_scope: list[object] = []

    @contextmanager
    def fake_lease(
        slot_dir: object, *, scope: str = "full", **kwargs: object,
    ) -> Iterator[gate_capacity.GateRun]:
        captured_scope.append(scope)
        yield gate_capacity.GateRun(lease=gate_capacity.GateLease(workers=2, slots=8))

    calls: list[list[str]] = []

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "pytest" not in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=cmd, returncode=1,
            stdout="FAILED tests/test_alpha.py::test_one - ValueError: bad\n",
            stderr="",
        )

    with (
        patch("maestro.gate_capacity.lease_for_gate_run", fake_lease),
        patch("maestro.evolution.subprocess.run", side_effect=side_effect),
    ):
        result = e.run_quality_gates(scope=["tests/test_alpha.py", "tests/test_beta.py"])

    assert captured_scope == ["impacted"]
    assert calls[0] == ["ruff", "check", "maestro/", "tests/", "scripts/"]
    assert calls[1] == ["mypy", "maestro/", "scripts/"]
    assert calls[2] == [
        "pytest", "tests/test_alpha.py", "tests/test_beta.py",
        "-x", "--tb=short", "-q", "--no-cov", "-n", "2", "--dist", "loadfile",
    ]
    assert not [arg for arg in calls[2] if arg.startswith("--cov")]
    # The enumerate re-run derives from the narrowed probe's own argv, minus -x.
    assert len(calls) == 4
    assert calls[3] == [arg for arg in calls[2] if arg != "-x"]
    assert result.tests_ok is False


def test_run_quality_gates_default_scope_stamps_the_full_row(
    mem_fs: dict[str, str],
) -> None:
    """The no-argument call still reports ``scope="full"`` to the lease.

    The narrowed row is excluded from the budget derivation, so a default call
    mis-stamped as narrowed would silently starve ``derive_deadline`` of input.
    """
    d = Path("/fake/evo_scope_full")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(
        m, c, j, n, issues=issues, config=Config(gate_lease_enabled=True),
    )

    captured_scope: list[object] = []

    @contextmanager
    def fake_lease(
        slot_dir: object, *, scope: str = "full", **kwargs: object,
    ) -> Iterator[gate_capacity.GateRun]:
        captured_scope.append(scope)
        yield gate_capacity.GateRun(lease=gate_capacity.GateLease(workers=1, slots=8))

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with (
        patch("maestro.gate_capacity.lease_for_gate_run", fake_lease),
        patch("maestro.evolution.subprocess.run", side_effect=side_effect),
    ):
        result = e.run_quality_gates()

    assert captured_scope == ["full"]
    assert result.passed is True


def test_run_quality_gates_flag_on_splices_worker_args_into_tests_and_rerun(
    mem_fs: dict[str, str],
) -> None:
    """Flag-on, the Tests argv carries the granted ``-n``/``--dist loadfile`` width,
    and the ``-x``-less enumerate re-run keeps it (proposal 126).

    The width comes from the shared ``lease_for_gate_run`` seam via
    ``worker_args``, spliced beside the ``--cov`` atoms; the re-run derives its
    argv from the probe's own (minus ``-x``), so the granted width flows through
    unchanged inside the same held slot.
    """
    d = Path("/fake/evo_lease")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    n = NullNotifier()
    issues = IssueTracker(github_repo=None)
    e = EvolutionEngine(
        m, c, j, n, issues=issues,
        config=Config(
            gate_observation_enabled=True,
            gate_lease_enabled=True,
            gate_lease_cgroup_reserve_enabled=True,
            gate_lease_admission_cap_enabled=True,
        ),
    )

    captured_slot_dir: list[object] = []
    captured_reserve: list[bool] = []
    captured_cap: list[object] = []

    @contextmanager
    def fake_lease(
        slot_dir: object, *, runs_path: object = None, cgroup_reserve: bool = False,
        **kwargs: object,
    ) -> Iterator[gate_capacity.GateRun]:
        captured_slot_dir.append(slot_dir)
        captured_reserve.append(cgroup_reserve)
        captured_cap.append(kwargs.get("admission_cap"))
        yield gate_capacity.GateRun(
            lease=gate_capacity.GateLease(workers=4, slots=8),
        )

    calls: list[list[str]] = []

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "pytest" not in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if "-x" in cmd:
            # The probe aborts at the first failure — one visible failure only.
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="FAILED tests/test_alpha.py::test_one - ValueError: bad\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout=_FOUR_FAILURES, stderr="",
        )

    with (
        patch("maestro.gate_capacity.lease_for_gate_run", fake_lease),
        patch("maestro.evolution.subprocess.run", side_effect=side_effect),
    ):
        result = e.run_quality_gates()

    # The seam is keyed off the derived slot-dir property (flag on -> the slot dir).
    assert captured_slot_dir == [e._config.gate_lease_slot_dir]
    # ...and the bound-aware reserve opt-in reaches the seam's worker plan.
    assert captured_reserve == [True]
    assert captured_cap == [True]
    # The probe's Tests argv carries the granted width beside the --cov atoms.
    assert calls[2] == [
        "pytest", "tests/", "-x", "--tb=short", "-q",
        "--cov=maestro", f"--cov-fail-under={e._config.cov_fail_under}",
        "-n", "4", "--dist", "loadfile",
    ]
    # The -x-less enumerate re-run keeps the width — the probe's argv minus -x.
    assert len(calls) == 4
    assert "-x" not in calls[3]
    assert calls[3] == [arg for arg in calls[2] if arg != "-x"]
    assert calls[3][-4:] == ["-n", "4", "--dist", "loadfile"]
    assert result.tests_ok is False


def _lease_granting(
    deadline: int,
) -> Callable[..., AbstractContextManager[gate_capacity.GateRun]]:
    """A ``lease_for_gate_run`` stand-in granting a fixed width and budget."""

    @contextmanager
    def fake_lease(slot_dir: object, **kwargs: object) -> Iterator[gate_capacity.GateRun]:
        yield gate_capacity.GateRun(
            lease=gate_capacity.GateLease(
                workers=1, slots=8, deadline_seconds=deadline,
            ),
        )

    return fake_lease


def test_run_quality_gates_spends_the_lease_deadline_on_the_tests_gate_only(
    mem_fs: dict[str, str],
) -> None:
    """Flag on, only the suite runs under the granted budget (proposal 134).

    Ruff and mypy are fast, answered gates whose ceiling is unchanged; the suite is
    the one command whose true cost depends on the granted width, so it — and the
    ``-x``-less enumerate re-run derived from its argv — is the only one that
    spends the lease's derived deadline.
    """
    e = _evolution_engine_for_lease(Config(
        gate_observation_enabled=True,
        gate_lease_enabled=True,
        gate_budget_lease_owned_enabled=True,
    ))

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "pytest" not in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if "-x" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="FAILED tests/test_alpha.py::test_one - ValueError: bad\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout=_FOUR_FAILURES, stderr="",
        )

    with (
        patch("maestro.gate_capacity.lease_for_gate_run", _lease_granting(675)),
        patch(
            "maestro.evolution.subprocess.run", side_effect=side_effect,
        ) as mock_run,
    ):
        e.run_quality_gates()

    timeouts = [call.kwargs["timeout"] for call in mock_run.call_args_list]
    # ruff, mypy -> the flat ceiling; the probe and its enumerate re-run -> the grant.
    assert timeouts == [
        e._config.selfcheck_timeout, e._config.selfcheck_timeout, 675, 675,
    ]


def test_run_quality_gates_budgeted_suite_still_names_the_timeout_non_answer(
    mem_fs: dict[str, str],
) -> None:
    """A kill at the *derived* deadline records the same ``TIMEOUT`` non-answer.

    Only *when* the kill happens moves; the recorded outcome vocabulary — and the
    salvaged partial output that names what the killed runner had already said —
    is unchanged, so nothing downstream has to learn a new word.
    """
    e = _evolution_engine_for_lease(Config(
        gate_observation_enabled=True,
        gate_lease_enabled=True,
        gate_budget_lease_owned_enabled=True,
    ))

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "pytest" in cmd:
            raise subprocess.TimeoutExpired(
                cmd, kwargs["timeout"], output="FAILED tests/test_a.py::test_one\n",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with (
        patch("maestro.gate_capacity.lease_for_gate_run", _lease_granting(675)),
        patch(
            "maestro.evolution.subprocess.run", side_effect=side_effect,
        ) as mock_run,
    ):
        result = e.run_quality_gates()

    assert mock_run.call_args_list[2].kwargs["timeout"] == 675
    assert result.tests_ok is False
    assert result.observation is not None
    assert "tests:TIMEOUT" in result.observation.signature()
    assert "Tests: could not run" in result.raw_output


def test_run_quality_gates_stamps_timeout_with_observation_off(
    mem_fs: dict[str, str],
) -> None:
    """A killed suite records ``TIMEOUT`` even when observation is off.

    The coarse CLEAN/FAILED branch would otherwise call a kill ``FAILED``, and
    ``gate_capacity.derive_deadline`` keys on a recorded ``TIMEOUT`` — so the
    budget flag would be silently inert unless ``[loop]
    gate_observation_enabled`` happened to be on too.
    """
    e = _evolution_engine_for_lease(Config(
        gate_observation_enabled=False,
        gate_lease_enabled=True,
        gate_budget_lease_owned_enabled=True,
    ))
    captured: list[gate_capacity.GateRun] = []

    @contextmanager
    def fake_lease(slot_dir: object, **kwargs: object) -> Iterator[gate_capacity.GateRun]:
        run = gate_capacity.GateRun(
            lease=gate_capacity.GateLease(workers=1, slots=8, deadline_seconds=675),
        )
        captured.append(run)
        yield run

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "pytest" in cmd:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with (
        patch("maestro.gate_capacity.lease_for_gate_run", fake_lease),
        patch("maestro.evolution.subprocess.run", side_effect=side_effect),
    ):
        result = e.run_quality_gates()

    assert result.observation is None
    assert captured[0].outcome == "TIMEOUT"


@contextmanager
def _capturing_lease(
    captured: list[gate_capacity.GateRun], *, deadline: int = 675,
) -> Iterator[gate_capacity.GateRun]:
    """A ``lease_for_gate_run`` stand-in that hands the yielded run back to the test."""
    run = gate_capacity.GateRun(
        lease=gate_capacity.GateLease(workers=1, slots=8, deadline_seconds=deadline),
    )
    captured.append(run)
    yield run


def _advancing_clock(
    clock: list[float], per_command: dict[str, float],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """A ``subprocess.run`` stand-in that advances *clock* by each command's cost."""

    def side_effect(
        cmd: list[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        key = "rerun" if "pytest" in cmd and "-x" not in cmd else cmd[0]
        clock[0] += per_command[key]
        if "pytest" not in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr="",
            )
        if "-x" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="FAILED tests/test_alpha.py::test_one - ValueError: bad\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout=_FOUR_FAILURES, stderr="",
        )

    return side_effect


def test_run_quality_gates_stamps_the_longest_pytest_command_time(
    mem_fs: dict[str, str],
) -> None:
    """``tests_seconds`` is the longest single pytest command, ruff/mypy excluded.

    The ``-x`` probe is cut short by its first failure, so ``_observe_gate`` re-runs
    the suite without ``-x`` to enumerate it — two pytest commands, each budgeted at
    the same per-command deadline, so what the budget must cover is the *longer* of
    them, never their sum and never with ruff and mypy folded in.
    """
    e = _evolution_engine_for_lease(Config(
        gate_observation_enabled=True,
        gate_lease_enabled=True,
        gate_budget_lease_owned_enabled=True,
    ))
    captured: list[gate_capacity.GateRun] = []
    clock = [0.0]
    side_effect = _advancing_clock(
        clock, {"ruff": 9.0, "mypy": 40.0, "pytest": 120.0, "rerun": 380.0},
    )

    with (
        patch(
            "maestro.gate_capacity.lease_for_gate_run",
            lambda *a, **k: _capturing_lease(captured),
        ),
        patch("maestro.gate_capacity.time.monotonic", lambda: clock[0]),
        patch("maestro.evolution.subprocess.run", side_effect=side_effect),
    ):
        e.run_quality_gates()

    assert captured[0].tests_seconds == 380.0


def test_run_quality_gates_stamps_the_probe_when_it_is_the_longer_command(
    mem_fs: dict[str, str],
) -> None:
    """A probe slower than its enumerate re-run is what the field keeps."""
    e = _evolution_engine_for_lease(Config(
        gate_observation_enabled=True,
        gate_lease_enabled=True,
        gate_budget_lease_owned_enabled=True,
    ))
    captured: list[gate_capacity.GateRun] = []
    clock = [0.0]
    side_effect = _advancing_clock(
        clock, {"ruff": 9.0, "mypy": 40.0, "pytest": 400.0, "rerun": 30.0},
    )

    with (
        patch(
            "maestro.gate_capacity.lease_for_gate_run",
            lambda *a, **k: _capturing_lease(captured),
        ),
        patch("maestro.gate_capacity.time.monotonic", lambda: clock[0]),
        patch("maestro.evolution.subprocess.run", side_effect=side_effect),
    ):
        e.run_quality_gates()

    assert captured[0].tests_seconds == 400.0


def test_run_quality_gates_stamps_tests_seconds_for_a_killed_suite(
    mem_fs: dict[str, str],
) -> None:
    """A timed-out suite still records a figure — a censored reading is evidence.

    The derivation is what decides to exclude it from the completed p50; the timer
    stamps it regardless, because the pytest command raised inside the ``with``.
    """
    e = _evolution_engine_for_lease(Config(
        gate_observation_enabled=True,
        gate_lease_enabled=True,
        gate_budget_lease_owned_enabled=True,
    ))
    captured: list[gate_capacity.GateRun] = []
    clock = [0.0]

    def side_effect(
        cmd: list[str], **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if "pytest" in cmd:
            clock[0] += 675.0
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output="")
        clock[0] += 9.0
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with (
        patch(
            "maestro.gate_capacity.lease_for_gate_run",
            lambda *a, **k: _capturing_lease(captured),
        ),
        patch("maestro.gate_capacity.time.monotonic", lambda: clock[0]),
        patch("maestro.evolution.subprocess.run", side_effect=side_effect),
    ):
        e.run_quality_gates()

    assert captured[0].outcome == "TIMEOUT"
    assert captured[0].tests_seconds == 675.0


def test_run_quality_gates_coarse_outcome_still_failed_on_a_red_suite(
    mem_fs: dict[str, str],
) -> None:
    """With no non-answer, the coarse branch is unchanged: a red suite is ``FAILED``."""
    e = _evolution_engine_for_lease(Config(
        gate_observation_enabled=False,
        gate_lease_enabled=True,
    ))
    captured: list[gate_capacity.GateRun] = []

    @contextmanager
    def fake_lease(slot_dir: object, **kwargs: object) -> Iterator[gate_capacity.GateRun]:
        run = gate_capacity.GateRun(lease=gate_capacity.GateLease(workers=1, slots=8))
        captured.append(run)
        yield run

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        code = 1 if "pytest" in cmd else 0
        return subprocess.CompletedProcess(args=cmd, returncode=code, stdout="", stderr="")

    with (
        patch("maestro.gate_capacity.lease_for_gate_run", fake_lease),
        patch("maestro.evolution.subprocess.run", side_effect=side_effect),
    ):
        e.run_quality_gates()

    assert captured[0].outcome == "FAILED"


def _evolution_engine_for_lease(config: Config) -> EvolutionEngine:
    """An EvolutionEngine wired for the gate-run recording tests."""
    d = Path("/fake/evo_runs")
    m = Memory(d)
    c = Consciousness(d)
    c.load_self_model()
    j = Journal(d)
    issues = IssueTracker(github_repo=None)
    return EvolutionEngine(
        m, c, j, NullNotifier(), issues=issues, config=config,
    )


def test_run_quality_gates_records_one_gate_run_row_when_flag_on(
    mem_fs: dict[str, str],
) -> None:
    """Flag on, run_quality_gates records exactly one telemetry row for the run.

    The real ``lease_for_gate_run`` seam runs (only the host readers are mocked),
    so the row lands via the actual recording path with the outcome the site
    stamps. A clean run records ``CLEAN``.
    """
    config = Config(gate_observation_enabled=True, gate_lease_enabled=True)
    e = _evolution_engine_for_lease(config)

    @contextmanager
    def fake_acquire(lock_dir: object, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        yield "slot"

    def all_pass(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with (
        patch(
            "maestro.gate_capacity.measure_host_capacity",
            return_value=gate_capacity.HostCapacity(cores=8, available_mib=64000),
        ),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        patch("maestro.evolution.subprocess.run", side_effect=all_pass),
    ):
        result = e.run_quality_gates()

    assert result.passed is True
    lines = mem_fs[str(config.gate_runs_path)].strip().splitlines()
    assert len(lines) == 1
    record = gate_capacity._parse_gate_run_dict(json.loads(lines[0]))
    assert record is not None
    assert record.admission == "slot"
    assert record.outcome == "CLEAN"


def test_run_quality_gates_records_no_row_when_flag_off(
    mem_fs: dict[str, str],
) -> None:
    """Flag off, no telemetry row is written and no host is read."""
    config = Config(gate_observation_enabled=True, gate_lease_enabled=False)
    e = _evolution_engine_for_lease(config)

    def all_pass(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    mock_measure = MagicMock()
    with (
        patch("maestro.gate_capacity.measure_host_capacity", mock_measure),
        patch("maestro.evolution.subprocess.run", side_effect=all_pass),
    ):
        e.run_quality_gates()

    assert str(config.gate_runs_path) not in mem_fs
    mock_measure.assert_not_called()


def test_run_quality_gates_observation_enumerates_failing_suite(
    mem_fs: dict[str, str],
) -> None:
    """Flag-on, a red pytest gate yields one ``GateFailure`` row per failing test.

    The ``-x`` probe aborts at the first failure, so the enumeration comes from
    the second, ``-x``-less run — the whole point of the re-run.
    """
    e = _observing_engine()
    calls: list[list[str]] = []

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "pytest" not in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if "-x" in cmd:
            # The probe aborted at the first failure — one visible failure only.
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="FAILED tests/test_alpha.py::test_one - ValueError: bad\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout=_FOUR_FAILURES, stderr="",
        )

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert result.observation is not None
    tests_gate = result.observation.gates[2]
    assert tests_gate.tool == "tests"
    assert tests_gate.kind == "FAILED"
    assert len(tests_gate.failures) == 4
    assert len(tests_gate.signature()) == 4
    # A red probe costs one extra run; the re-run is the probe's argv minus -x.
    assert len(calls) == 4
    assert "-x" not in calls[3]
    assert calls[3] == [arg for arg in calls[2] if arg != "-x"]


def test_run_quality_gates_green_run_issues_exactly_three_calls(
    mem_fs: dict[str, str],
) -> None:
    """Flag-on, a green probe is never re-run — three subprocess calls, all clean."""
    e = _observing_engine()

    with patch("maestro.evolution.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        )
        result = e.run_quality_gates()

    assert mock_run.call_count == 3
    assert result.passed is True
    assert result.observation is not None
    assert result.observation.all_clean() is True
    assert result.observation.signature() == frozenset()


def test_run_quality_gates_observation_names_a_timeout(mem_fs: dict[str, str]) -> None:
    """A gate that exceeds ``selfcheck_timeout`` is ``TIMEOUT``, not ``"could not run"``.

    Its ``failures`` is empty because there is no output to parse, but its
    signature is the non-empty ``{"tests:TIMEOUT"}`` — a non-answer can never read
    as a clean gate downstream.
    """
    e = _observing_engine()

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "pytest" in cmd:
            raise subprocess.TimeoutExpired(cmd, 60)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert result.tests_ok is False
    assert result.observation is not None
    tests_gate = result.observation.gates[2]
    assert tests_gate.kind == "TIMEOUT"
    assert tests_gate.failures == ()
    assert tests_gate.signature() == frozenset({"tests:TIMEOUT"})
    assert result.observation.all_clean() is False


def test_run_quality_gates_timeout_salvages_the_partial_output(
    mem_fs: dict[str, str],
) -> None:
    """A killed runner's partial output is enumerated, not thrown away.

    ``subprocess.run`` populates ``TimeoutExpired.stdout`` with whatever it read
    before killing the process — as *bytes* on POSIX, because the exception is
    raised inside ``Popen._communicate`` before ``text=True``'s newline
    translation runs. On a slow suite that text already names real failures, and
    it is the only material a fixer has for a gate that never answered.
    """
    e = _observing_engine()

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "pytest" in cmd:
            raise subprocess.TimeoutExpired(
                cmd, 60,
                output=b"FAILED tests/test_alpha.py::test_one - ValueError: bad\n"
                       b"FAILED tests/test_alpha.py::test_two - KeyError: 'k'\n",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert result.tests_ok is False
    assert "Tests: could not run" in result.raw_output
    assert result.observation is not None
    tests_gate = result.observation.gates[2]
    # The gate still did not answer — no salvaged row may relabel it an ordinary
    # red — but the rows it did print are enumerated, and the kind stays in the
    # signature so the truncated set can never read as progress.
    assert tests_gate.kind == "TIMEOUT"
    assert [f.identifier for f in tests_gate.failures] == [
        "tests/test_alpha.py::test_one",
        "tests/test_alpha.py::test_two",
    ]
    assert tests_gate.signature() == frozenset({
        "tests:TIMEOUT",
        "tests:tests/test_alpha.py::test_one",
        "tests:tests/test_alpha.py::test_two",
    })
    assert "test_two" in tests_gate.output


def test_run_quality_gates_timeout_salvages_undecodable_and_text_streams(
    mem_fs: dict[str, str],
) -> None:
    """All three shapes ``TimeoutExpired`` can carry normalise to text.

    ``None`` when nothing was written, ``bytes`` on POSIX (possibly cut
    mid-character, which must not raise), and ``str`` on Windows, where
    ``subprocess.run`` re-reads the pipes after the kill.
    """
    e = _observing_engine()

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ruff" in cmd:
            # POSIX bytes, truncated mid-UTF-8-sequence by the kill.
            raise subprocess.TimeoutExpired(
                cmd, 60, output=b"E501 Line too long\n --> maestro/a.py:1:90\n\xe2\x82",
            )
        if "mypy" in cmd:
            # Windows: already-decoded text.
            raise subprocess.TimeoutExpired(
                cmd, 60,
                output="maestro/b.py:7: error: Bad thing  [assignment]\n",
            )
        # Nothing written before the kill at all.
        raise subprocess.TimeoutExpired(cmd, 60)

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert result.observation is not None
    ruff_gate, mypy_gate, tests_gate = result.observation.gates
    assert ruff_gate.signature() == frozenset(
        {"ruff:TIMEOUT", "ruff:maestro/a.py:1:90:E501"},
    )
    # The undecodable tail is replaced, not raised on.
    assert "\ufffd" in ruff_gate.output
    assert mypy_gate.signature() == frozenset(
        {"mypy:TIMEOUT", "mypy:maestro/b.py:7:assignment"},
    )
    assert tests_gate.failures == ()
    assert tests_gate.output == ""


def test_run_quality_gates_observation_names_an_unavailable_runner(
    mem_fs: dict[str, str],
) -> None:
    """A runner that cannot be executed at all is ``UNAVAILABLE``, its own kind.

    The pre-124 runner collapsed this into the same ``"could not run"`` string as a
    timeout, so the fixer could not tell a missing binary from a slow suite.
    """
    e = _observing_engine()

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "mypy" in cmd:
            raise OSError("No such file or directory: 'mypy'")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert result.mypy_ok is False
    assert "Mypy: could not run" in result.raw_output
    assert result.observation is not None
    mypy_gate = result.observation.gates[1]
    assert mypy_gate.kind == "UNAVAILABLE"
    assert mypy_gate.signature() == frozenset({"mypy:UNAVAILABLE"})


def test_run_quality_gates_observation_covers_ruff_and_mypy(
    mem_fs: dict[str, str],
) -> None:
    """The non-pytest gates are observed too, each keyed to its own tool."""
    e = _observing_engine()

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "ruff" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                # ruff's ``full`` default — the two-line shape the gate really gets.
                stdout="F401 [*] `os` imported but unused\n"
                       " --> maestro/foo.py:3:1\n"
                       "  |\n"
                       "3 | import os\n"
                       "  |\n"
                       "Found 1 error.\n",
                stderr="",
            )
        if "mypy" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout='maestro/bar.py:42: error: Bad return  [return-value]\n',
                stderr="",
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert result.observation is not None
    ruff_gate, mypy_gate, tests_gate = result.observation.gates
    assert ruff_gate.signature() == frozenset({"ruff:maestro/foo.py:3:1:F401"})
    assert mypy_gate.signature() == frozenset({"mypy:maestro/bar.py:42:return-value"})
    assert tests_gate.kind == "CLEAN"


def test_run_quality_gates_enumerate_rerun_failure_keeps_the_gate_red(
    mem_fs: dict[str, str],
) -> None:
    """A non-answering enumerate re-run falls back to the probe's own output.

    The probe stays the authority on redness, so a re-run that itself times out
    still yields a red observation carrying the probe's one visible failure.
    """
    e = _observing_engine()

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "pytest" not in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if "-x" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="FAILED tests/test_alpha.py::test_one - ValueError: bad\n",
                stderr="",
            )
        raise subprocess.TimeoutExpired(cmd, 60)

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert result.observation is not None
    tests_gate = result.observation.gates[2]
    assert tests_gate.kind == "FAILED"
    assert tests_gate.signature() == frozenset({"tests:tests/test_alpha.py::test_one"})
    # ...and it says so: the ``-x`` probe is the only reading left, so the row set
    # is known-short. Without the stamp, ``gate_observation.attribute`` would read
    # this one salvaged row as the whole failing set and could excuse the branch
    # as ``INHERITED`` while every failure past the ``-x`` abort went uncompared.
    assert tests_gate.truncated is True


def test_run_quality_gates_green_enumerate_rerun_never_reads_clean(
    mem_fs: dict[str, str],
) -> None:
    """A re-run that comes back green does not turn a red gate into a clean one.

    The probe's ``returncode`` is the authority, so the worst case is an empty
    ``failures`` tuple reported as the synthetic ``tests:FAILED`` identity —
    consistent with ``tests_ok is False`` rather than contradicting it.
    """
    e = _observing_engine()

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "pytest" not in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        if "-x" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1,
                stdout="FAILED tests/test_alpha.py::test_one - ValueError: bad\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="1184 passed\n", stderr="",
        )

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert result.tests_ok is False
    assert result.observation is not None
    tests_gate = result.observation.gates[2]
    assert tests_gate.kind == "FAILED"
    assert tests_gate.signature() == frozenset({"tests:FAILED"})
    assert result.observation.all_clean() is False
    # The re-run *answered* — it just answered green — so the enumeration is not
    # short, only empty. The synthetic ``tests:FAILED`` identity is what keeps it
    # out of ``INHERITED``, not the truncation stamp.
    assert tests_gate.truncated is False


def test_run_quality_gates_coverage_only_red_is_not_re_run(
    mem_fs: dict[str, str],
) -> None:
    """A coverage-only red is enumerated from the probe — no second suite.

    ``-x`` cut nothing short here: every test passed and the run completed, so the
    probe's own output already carries the threshold line. Re-running would repeat
    a full suite — up to ``gate_convergence_ceiling`` times, at
    ``selfcheck_timeout`` each — to parse the identical fact.
    """
    e = _observing_engine()
    calls: list[list[str]] = []

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "pytest" not in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=cmd, returncode=1,
            stdout="1184 passed in 61.20s\n"
                   "FAIL Required test coverage of 100% not reached. "
                   "Total coverage: 97.61%\n",
            stderr="",
        )

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert len(calls) == 3
    assert result.tests_ok is False
    assert result.observation is not None
    tests_gate = result.observation.gates[2]
    assert tests_gate.kind == "COVERAGE"
    assert tests_gate.signature() == frozenset({"tests:coverage"})


def test_run_quality_gates_unparsed_red_is_not_re_run(
    mem_fs: dict[str, str],
) -> None:
    """A red the parser cannot read is not re-run either — and stays red.

    Nothing in the probe's output says ``-x`` stopped it, so a second suite would
    only reproduce the same unreadable text; the gate keeps its synthetic
    ``tests:FAILED`` identity, which can never be mistaken for green.
    """
    e = _observing_engine()
    calls: list[list[str]] = []

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        code = 1 if "pytest" in cmd else 0
        return subprocess.CompletedProcess(
            args=cmd, returncode=code, stdout="INTERNALERROR> nothing parseable\n",
            stderr="",
        )

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        result = e.run_quality_gates()

    assert len(calls) == 3
    assert result.observation is not None
    tests_gate = result.observation.gates[2]
    assert tests_gate.kind == "FAILED"
    assert tests_gate.signature() == frozenset({"tests:FAILED"})


def test_run_quality_gates_observation_honours_work_target_atoms(
    mem_fs: dict[str, str],
) -> None:
    """The enumerate re-run inherits the redirected gate paths, never a literal.

    The re-run's argv is derived from the probe's own, so a ``[work]``-overridden
    gate target flows into it without the paths being re-hardcoded.
    """
    e = _observing_engine(
        Config(
            gate_observation_enabled=True,
            work_gate_source_path="widget/",
            work_gate_tests_path="widget_tests/",
        ),
    )
    calls: list[list[str]] = []

    def side_effect(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if "pytest" not in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout=_FOUR_FAILURES, stderr="",
        )

    with patch("maestro.evolution.subprocess.run", side_effect=side_effect):
        e.run_quality_gates()

    assert len(calls) == 4
    assert "widget_tests/" in calls[3]
    assert "--cov=widget" in calls[3]
    assert "tests/" not in calls[3]


def test_cap_text_under_budget_returns_unchanged() -> None:
    from maestro.evolution import _cap_text
    text = "small text"
    assert _cap_text(text, 100) == text


def test_cap_text_over_budget_keeps_tail_with_marker() -> None:
    from maestro.evolution import _cap_text
    text = "HEAD" + ("x" * 1000) + "TAIL_MARKER"
    capped = _cap_text(text, 100)
    assert "TAIL_MARKER" in capped
    assert "HEAD" not in capped
    assert "chars truncated" in capped
    assert len(capped) <= 100


def test_cap_text_max_smaller_than_marker_returns_marker_prefix() -> None:
    """When ``max_chars`` is smaller than the truncation marker itself, the
    function returns a marker prefix bounded to ``max_chars`` (degenerate
    case — should not raise).
    """
    from maestro.evolution import _cap_text
    text = "x" * 100
    capped = _cap_text(text, 5)
    assert len(capped) <= 5


def test_cap_text_marker_reports_true_omitted_count() -> None:
    """The marker's reported count equals the number of *source* characters
    actually removed — the marker line consumes budget, so the true dropped
    count is ``len(text) - keep``, not ``len(text) - max_chars``.
    """
    import re

    from maestro.evolution import _cap_text
    text = "A" * 1000
    capped = _cap_text(text, 100)
    match = re.match(r"\n\[\.\.\. (\d+) chars truncated \.\.\.\]\n", capped)
    assert match is not None
    n = int(match.group(1))
    # The retained original tail is whatever follows the marker.
    keep = len(capped) - match.end()
    # Reported count must equal the true number of source chars dropped.
    assert n == len(text) - keep
    # The length contract is not weakened by the fix.
    assert len(capped) <= 100


def test_close_linked_issue(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="p1", title="Fix",
        category="capability", rationale="", proposed_changes=[],
        files_affected=[], expected_impact="",
        priority="resilience", issue_number=42, created_at="",
    )

    with patch.object(e._issues, "close") as mock_close:
        e.close_linked_issue(plan)

    mock_close.assert_called_once_with(42)


def test_close_linked_issue_none(mem_fs: dict[str, str]) -> None:
    e, _m, _c, _j = _make(mem_fs)
    plan = EvolutionPlan(
        plan_id="p1", title="No issue",
        category="capability", rationale="", proposed_changes=[],
        files_affected=[], expected_impact="", created_at="",
    )

    with patch.object(e._issues, "close") as mock_close:
        e.close_linked_issue(plan)

    mock_close.assert_not_called()


@pytest.mark.asyncio
async def test_submit_dedup_checks_all_plans(mem_fs: dict[str, str]) -> None:
    e, m, _c, _j = _make(mem_fs)

    for i in range(15):
        plan = EvolutionPlan(
            plan_id=f"p{i:03d}", title=f"Plan {i}" if i > 0 else "Original Title",
            category="capability", rationale="test",
            proposed_changes=["change"], files_affected=[], expected_impact="",
            created_at="2025-01-01T00:00:00Z",
        )
        m.save_evolution_plan(plan)

    duplicate = EvolutionPlan(
        plan_id="pdup", title="Original Title",
        category="capability", rationale="test again",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2025-01-02T00:00:00Z",
    )

    await e.submit_for_review(duplicate)

    all_plans = m.get_all_plans()
    titles = [p.title for p in all_plans if p.plan_id == "pdup"]
    assert len(titles) == 0


@pytest.mark.asyncio
async def test_submit_dedup_allows_resubmission_of_rejected_plans(
    mem_fs: dict[str, str],
) -> None:
    """Old rejected plans (outside the cooldown window) can be re-proposed."""
    e, m, _c, _j = _make(mem_fs)

    rejected = EvolutionPlan(
        plan_id="prej", title="Improve Error Handling",
        category="capability", rationale="initial attempt",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="Too broad",
        created_at="2025-01-01T00:00:00Z",
        reviewed_at="2025-01-01T00:00:00Z",
    )
    m.save_evolution_plan(rejected)

    retry = EvolutionPlan(
        plan_id="pretry", title="Improve Error Handling",
        category="capability", rationale="focused approach",
        proposed_changes=["targeted change"], files_affected=[], expected_impact="",
        created_at="2025-01-02T00:00:00Z",
    )

    await e.submit_for_review(retry)

    all_plans = m.get_all_plans()
    retry_plans = [p for p in all_plans if p.plan_id == "pretry"]
    assert len(retry_plans) == 1


@pytest.mark.asyncio
async def test_submit_dedup_allows_resubmission_of_obsolete_plans(
    mem_fs: dict[str, str],
) -> None:
    """A self-pruned (``obsolete``) plan does not block a new proposal with the same title.

    Self-pruning means the agent concluded the prior plan was no longer
    live — a re-proposal is legitimate continuation, not duplication.
    Parallels the rejected/failed exclusions in active_plans dedup.
    """
    e, m, _c, _j = _make(mem_fs)

    obsoleted = EvolutionPlan(
        plan_id="pobs", title="Refresh Error Handling",
        category="capability", rationale="initial draft",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="obsolete", obsolete_reason="superseded by later thinking",
        created_at="2025-01-01T00:00:00Z",
        reviewed_at="2025-01-01T00:00:00Z",
    )
    m.save_evolution_plan(obsoleted)

    retry = EvolutionPlan(
        plan_id="pretry", title="Refresh Error Handling",
        category="capability", rationale="focused approach",
        proposed_changes=["targeted change"], files_affected=[], expected_impact="",
        created_at="2025-01-02T00:00:00Z",
    )

    await e.submit_for_review(retry)

    all_plans = m.get_all_plans()
    retry_plans = [p for p in all_plans if p.plan_id == "pretry"]
    assert len(retry_plans) == 1


@pytest.mark.asyncio
async def test_submit_for_review_blocks_recent_rejection_resubmission(
    mem_fs: dict[str, str],
) -> None:
    """A plan rejected within the last 7 days must not be re-proposed.

    The reflect() prompt tells the LLM to avoid rejected approaches but
    nothing previously enforced it.  This test pins down the cooldown
    behavior so a confused LLM cannot re-submit the same idea every loop
    iteration.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    e, m, _c, _j = _make(mem_fs)

    fixed_now = _dt(2026, 4, 8, tzinfo=_UTC)
    # Rejected 2 days ago — well inside the cooldown.
    recent_rejection_ts = (fixed_now - _td(days=2)).isoformat()
    rejected = EvolutionPlan(
        plan_id="prej_recent", title="Improve Error Handling",
        category="capability", rationale="first try",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="Too broad",
        created_at=recent_rejection_ts, reviewed_at=recent_rejection_ts,
    )
    m.save_evolution_plan(rejected)

    retry = EvolutionPlan(
        plan_id="pretry_recent", title="Improve Error Handling",
        category="capability", rationale="another try",
        proposed_changes=["targeted change"], files_affected=[],
        expected_impact="", created_at=fixed_now.isoformat(),
    )

    class _FrozenDateTime(_dt):  # type: ignore[misc]
        @classmethod
        def now(cls, tz: object = None) -> _dt:  # type: ignore[override]
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    with patch("maestro.evolution_submission.datetime", _FrozenDateTime):
        await e.submit_for_review(retry)

    all_plans = m.get_all_plans()
    retry_plans = [p for p in all_plans if p.plan_id == "pretry_recent"]
    assert len(retry_plans) == 0


@pytest.mark.asyncio
async def test_submit_for_review_allows_resubmission_after_cooldown(
    mem_fs: dict[str, str],
) -> None:
    """A plan rejected more than 7 days ago can be re-proposed."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    e, m, _c, _j = _make(mem_fs)

    fixed_now = _dt(2026, 4, 8, tzinfo=_UTC)
    old_rejection_ts = (fixed_now - _td(days=10)).isoformat()
    rejected = EvolutionPlan(
        plan_id="prej_old", title="Refactor Storage Layer",
        category="capability", rationale="first try",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="Premature",
        created_at=old_rejection_ts, reviewed_at=old_rejection_ts,
    )
    m.save_evolution_plan(rejected)

    retry = EvolutionPlan(
        plan_id="pretry_old", title="Refactor Storage Layer",
        category="capability", rationale="now ready",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at=fixed_now.isoformat(),
    )

    class _FrozenDateTime(_dt):  # type: ignore[misc]
        @classmethod
        def now(cls, tz: object = None) -> _dt:  # type: ignore[override]
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    with patch("maestro.evolution_submission.datetime", _FrozenDateTime):
        await e.submit_for_review(retry)

    all_plans = m.get_all_plans()
    retry_plans = [p for p in all_plans if p.plan_id == "pretry_old"]
    assert len(retry_plans) == 1


@pytest.mark.asyncio
async def test_submit_for_review_cooldown_fails_open_on_unparseable_timestamp(
    mem_fs: dict[str, str],
) -> None:
    """An unparseable rejection timestamp must not block forever — fail open."""
    e, m, _c, _j = _make(mem_fs)

    rejected = EvolutionPlan(
        plan_id="prej_bad_ts", title="Some Plan",
        category="capability", rationale="first try",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="bad",
        created_at="not-a-timestamp", reviewed_at="not-a-timestamp",
    )
    m.save_evolution_plan(rejected)

    retry = EvolutionPlan(
        plan_id="pretry_bad_ts", title="Some Plan",
        category="capability", rationale="retry",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2026-04-08T00:00:00+00:00",
    )

    await e.submit_for_review(retry)

    retry_plans = [p for p in m.get_all_plans() if p.plan_id == "pretry_bad_ts"]
    assert len(retry_plans) == 1


@pytest.mark.asyncio
async def test_submit_for_review_cooldown_ignores_unrelated_rejected_titles(
    mem_fs: dict[str, str],
) -> None:
    """Cooldown only applies to plans with the same normalized title — an
    unrelated rejected plan must not block submission of a different idea.
    """
    e, m, _c, _j = _make(mem_fs)

    rejected = EvolutionPlan(
        plan_id="prej_other", title="Completely Different Plan",
        category="capability", rationale="x",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="bad",
        created_at="2026-04-07T00:00:00+00:00",
        reviewed_at="2026-04-07T00:00:00+00:00",
    )
    m.save_evolution_plan(rejected)

    new_plan = EvolutionPlan(
        plan_id="pnew", title="Unrelated Idea",
        category="capability", rationale="y",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2026-04-08T00:00:00+00:00",
    )
    await e.submit_for_review(new_plan)

    saved = [p for p in m.get_all_plans() if p.plan_id == "pnew"]
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_submit_for_review_cooldown_skips_rejection_with_no_timestamp(
    mem_fs: dict[str, str],
) -> None:
    """A rejected plan with no created_at and no reviewed_at provides no
    information about *when* it was rejected, so the cooldown check
    cannot fire — fail open and allow the resubmission.
    """
    e, m, _c, _j = _make(mem_fs)

    rejected = EvolutionPlan(
        plan_id="prej_no_ts", title="Timestampless Plan",
        category="capability", rationale="x",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="bad",
        created_at="", reviewed_at=None,
    )
    m.save_evolution_plan(rejected)

    retry = EvolutionPlan(
        plan_id="pretry_no_ts", title="Timestampless Plan",
        category="capability", rationale="retry",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at="2026-04-08T00:00:00+00:00",
    )
    await e.submit_for_review(retry)

    saved = [p for p in m.get_all_plans() if p.plan_id == "pretry_no_ts"]
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_submit_for_review_cooldown_handles_naive_timestamp(
    mem_fs: dict[str, str],
) -> None:
    """A rejection timestamp with no timezone info must be treated as UTC,
    not crash with a tz-aware vs tz-naive ``TypeError``.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    e, m, _c, _j = _make(mem_fs)

    fixed_now = _dt(2026, 4, 8, tzinfo=_UTC)
    # Naive ISO timestamp (no offset / Z) — 2 days before fixed_now.
    naive_ts = (fixed_now - _td(days=2)).replace(tzinfo=None).isoformat()

    rejected = EvolutionPlan(
        plan_id="prej_naive", title="Naive TS Plan",
        category="capability", rationale="x",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        status="rejected", rejection_reason="bad",
        created_at=naive_ts, reviewed_at=naive_ts,
    )
    m.save_evolution_plan(rejected)

    retry = EvolutionPlan(
        plan_id="pretry_naive", title="Naive TS Plan",
        category="capability", rationale="retry",
        proposed_changes=["change"], files_affected=[], expected_impact="",
        created_at=fixed_now.isoformat(),
    )

    class _FrozenDateTime(_dt):  # type: ignore[misc]
        @classmethod
        def now(cls, tz: object = None) -> _dt:  # type: ignore[override]
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    with patch("maestro.evolution_submission.datetime", _FrozenDateTime):
        await e.submit_for_review(retry)

    # Within cooldown — must be skipped.
    saved = [p for p in m.get_all_plans() if p.plan_id == "pretry_naive"]
    assert len(saved) == 0

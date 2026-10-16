"""Tests for the gate runnability oracle (proposal 133, step 1).

Every probe takes its evidence injected — a ``find_spec`` callable, a collector
callable, a sequence of telemetry readings — so nothing here spawns a subprocess,
reads a clock, or sleeps; every file goes through ``mem_fs``.
"""

from __future__ import annotations

import dataclasses
import json
from importlib.machinery import ModuleSpec
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from maestro import gate_environment as ge
from maestro.config import Config

VERDICT_PATH = Path("/fake/data/gate_environment.json")


def _spec(name: str, origin: str | None) -> ModuleSpec:
    """Build a ``ModuleSpec`` with the given *origin* (``None`` = namespace)."""
    return ModuleSpec(name, loader=None, origin=origin)


def _finder(resolved: dict[str, str | None]) -> Any:
    """A ``find_spec`` returning a spec per entry, ``None`` for anything else."""

    def find(name: str) -> ModuleSpec | None:
        if name not in resolved:
            return None
        return _spec(name, resolved[name])

    return find


_ALL_RESOLVED = {
    "maestro_sdk": "/site/maestro_sdk/__init__.py",
    "maestro_sdk.mcp_config": "/site/maestro_sdk/mcp_config.py",
    "maestro_review": "/site/maestro_review/__init__.py",
    "maestro_review.review": "/site/maestro_review/review.py",
}


@dataclasses.dataclass(frozen=True)
class _Reading:
    """Minimal structural stand-in for ``GateRunRecord``."""

    outcome: str = "CLEAN"
    workers: int = 1
    source: str = "in_process"
    scope: str = "full"
    tests_seconds: float = 100.0


def _clean_collector() -> tuple[int, str, str]:
    """A collection run that succeeded."""
    return (0, "collected 4200 items", "")


# ---------------------------------------------------------------------------
# probe_provisioning
# ---------------------------------------------------------------------------


def test_provisioning_sound_when_every_sibling_resolves() -> None:
    """Both packages and both declared submodules resolving is ``SOUND``."""
    verdict = ge.probe_provisioning(finder=_finder(dict(_ALL_RESOLVED)))

    assert verdict.kind == "SOUND"
    assert verdict.identities == frozenset()
    assert verdict.detail == "siblings provisioned"


def test_provisioning_flags_a_namespace_shadow() -> None:
    """A package that resolves while its declared submodule does not is the shadow."""
    resolved = dict(_ALL_RESOLVED)
    del resolved["maestro_sdk.mcp_config"]
    verdict = ge.probe_provisioning(finder=_finder(resolved))

    assert verdict.kind == "UNPROVISIONED"
    assert "maestro_sdk.mcp_config" in verdict.detail
    assert "namespace shadow" in verdict.detail
    assert 'pip install -e "maestro_sdk/"' in verdict.detail


def test_provisioning_flags_an_absent_package() -> None:
    """A sibling package that does not resolve at all is named directly."""
    resolved = {
        k: v for k, v in _ALL_RESOLVED.items() if not k.startswith("maestro_review")
    }
    verdict = ge.probe_provisioning(finder=_finder(resolved))

    assert verdict.kind == "UNPROVISIONED"
    assert verdict.detail == (
        'maestro_review does not resolve — install the sibling: '
        'pip install -e "maestro_review/"'
    )


def test_provisioning_treats_an_origin_less_spec_as_unresolved() -> None:
    """A ``ModuleSpec`` with ``origin is None`` is the namespace portion."""
    resolved: dict[str, str | None] = dict(_ALL_RESOLVED)
    resolved["maestro_sdk"] = None
    verdict = ge.probe_provisioning(finder=_finder(resolved))

    assert verdict.kind == "UNPROVISIONED"
    assert verdict.detail == (
        'maestro_sdk does not resolve — install the sibling: '
        'pip install -e "maestro_sdk/"'
    )


@pytest.mark.parametrize("exc", [ModuleNotFoundError("gone"), ValueError("no spec")])
def test_provisioning_reads_a_raising_finder_as_unresolved(exc: Exception) -> None:
    """A shadowed parent making ``find_spec`` raise reads as unresolved, not a crash."""

    def find(name: str) -> ModuleSpec | None:
        raise exc

    verdict = ge.probe_provisioning(finder=find)

    assert verdict.kind == "UNPROVISIONED"


def test_provisioning_fingerprint_distinguishes_package_from_submodule() -> None:
    """The unresolved names are what the ``UNPROVISIONED`` hash is about."""
    absent = dict(_ALL_RESOLVED)
    del absent["maestro_sdk"]
    del absent["maestro_sdk.mcp_config"]
    shadowed = dict(_ALL_RESOLVED)
    del shadowed["maestro_sdk.mcp_config"]

    first = ge.probe_provisioning(finder=_finder(absent))
    second = ge.probe_provisioning(finder=_finder(shadowed))

    assert first.fingerprint != second.fingerprint


# ---------------------------------------------------------------------------
# probe_collection
# ---------------------------------------------------------------------------


def test_collection_sound_on_a_clean_collect() -> None:
    """A zero-returncode collect run is ``SOUND`` with no identities."""
    verdict = ge.probe_collection(_clean_collector)

    assert verdict.kind == "SOUND"
    assert verdict.identities == frozenset()


def test_collection_carries_the_observation_signature() -> None:
    """A seeded collection ``ERROR`` row surfaces as ``UNCOLLECTABLE`` identities."""

    def collector() -> tuple[int, str, str]:
        return (2, "ERROR tests/test_x.py - ImportError: no module maestro_sdk", "")

    verdict = ge.probe_collection(collector)

    assert verdict.kind == "UNCOLLECTABLE"
    assert verdict.identities
    assert all(i.startswith("tests:") for i in verdict.identities)


def test_collection_parses_a_collector_non_answer() -> None:
    """A bare ``TIMEOUT`` literal is the collector's own non-answer."""
    verdict = ge.probe_collection(lambda: "TIMEOUT")

    assert verdict.kind == "UNCOLLECTABLE"
    assert verdict.identities == frozenset({"tests:TIMEOUT"})
    assert verdict.detail == "collection TIMEOUT"


def test_collection_fingerprint_moves_with_the_head() -> None:
    """A merge can repair a collection red, so the head enters the hash."""
    first = ge.probe_collection(lambda: "TIMEOUT", head="aaa")
    second = ge.probe_collection(lambda: "TIMEOUT", head="bbb")

    assert first.fingerprint != second.fingerprint


# ---------------------------------------------------------------------------
# probe_fit
# ---------------------------------------------------------------------------


def test_fit_sound_with_no_qualifying_rows() -> None:
    """No measurement is ``SOUND`` with ``unmeasured``, never a synthesised pass."""
    verdict = ge.probe_fit((), workers=1, deadline_seconds=600)

    assert verdict.kind == "SOUND"
    assert verdict.detail == "unmeasured"


def test_fit_sound_when_the_suite_fits() -> None:
    """A p50 comfortably below the deadline fraction fits."""
    readings = [_Reading(tests_seconds=300.0) for _ in range(3)]
    verdict = ge.probe_fit(readings, workers=1, deadline_seconds=600)

    assert verdict.kind == "SOUND"
    assert verdict.detail == "suite fits its deadline"


def test_fit_unfit_on_a_censored_timeout_majority() -> None:
    """Three of the last five killed runs is the right-censored reading."""
    readings = [
        _Reading(outcome="TIMEOUT", tests_seconds=0.0),
        _Reading(outcome="CLEAN", tests_seconds=10.0),
        _Reading(outcome="TIMEOUT", tests_seconds=0.0),
        _Reading(outcome="CLEAN", tests_seconds=10.0),
        _Reading(outcome="TIMEOUT", tests_seconds=0.0),
    ]
    verdict = ge.probe_fit(readings, workers=1, deadline_seconds=600)

    assert verdict.kind == "UNFIT"
    assert "killed at their deadline" in verdict.detail


def test_fit_unfit_on_a_completed_p50_above_the_threshold() -> None:
    """A median above 0.9 of the deadline is unfit even with no timeouts."""
    readings = [_Reading(tests_seconds=580.0) for _ in range(3)]
    verdict = ge.probe_fit(readings, workers=1, deadline_seconds=600)

    assert verdict.kind == "UNFIT"
    assert "exceeds" in verdict.detail


def test_fit_ignores_rows_of_another_source_scope_or_width() -> None:
    """Only ``in_process`` / ``full`` rows at the planned width are read."""
    readings = [
        _Reading(tests_seconds=5000.0, source="wrapper"),
        _Reading(tests_seconds=5000.0, scope="impacted"),
        _Reading(tests_seconds=5000.0, workers=4),
    ]
    verdict = ge.probe_fit(readings, workers=1, deadline_seconds=600)

    assert verdict.kind == "SOUND"
    assert verdict.detail == "unmeasured"


def test_fit_ignores_censored_timeout_rows_on_the_p50_arm() -> None:
    """A killed run times its own deadline, so it must not enter the median.

    Two ``TIMEOUT`` rows sit *below* the 3-of-5 censored threshold, and each
    carries roughly the deadline itself (``tests_seconds`` is stamped in a
    ``finally``). Folding them in would median to a false ``UNFIT`` even though
    the only run that actually finished was fast.
    """
    readings = [
        _Reading(outcome="TIMEOUT", tests_seconds=600.0),
        _Reading(outcome="TIMEOUT", tests_seconds=600.0),
        _Reading(outcome="CLEAN", tests_seconds=120.0),
    ]
    verdict = ge.probe_fit(readings, workers=1, deadline_seconds=600)

    assert verdict.kind == "SOUND"
    assert verdict.detail == "suite fits its deadline"


def test_fit_ignores_a_truncated_failed_row_on_the_p50_arm() -> None:
    """A ``-x`` probe cut short at its first failure times where it stopped."""
    readings = [
        _Reading(outcome="FAILED", tests_seconds=5.0),
        _Reading(outcome="FAILED", tests_seconds=5.0),
        _Reading(outcome="CLEAN", tests_seconds=580.0),
    ]
    verdict = ge.probe_fit(readings, workers=1, deadline_seconds=600)

    assert verdict.kind == "UNFIT"
    assert "exceeds" in verdict.detail


def test_fit_reads_a_coverage_row_as_completed() -> None:
    """``COVERAGE`` is a shortfall only computable after the last test ran."""
    readings = [_Reading(outcome="COVERAGE", tests_seconds=580.0) for _ in range(3)]
    verdict = ge.probe_fit(readings, workers=1, deadline_seconds=600)

    assert verdict.kind == "UNFIT"


def test_fit_ignores_a_zero_tests_seconds_row() -> None:
    """``0.0`` is the unmeasured sentinel, not a run that took no time."""
    readings = [_Reading(tests_seconds=0.0), _Reading(tests_seconds=0.0)]
    verdict = ge.probe_fit(readings, workers=1, deadline_seconds=600)

    assert verdict.kind == "SOUND"
    assert verdict.detail == "suite fits its deadline"


def test_fit_fingerprint_is_width_and_deadline_only() -> None:
    """The ``UNFIT`` hash is about the cgroup's planned width and its budget."""
    readings = [_Reading(tests_seconds=580.0) for _ in range(3)]
    first = ge.probe_fit(readings, workers=1, deadline_seconds=600)
    second = ge.probe_fit(readings * 2, workers=1, deadline_seconds=600)
    third = ge.probe_fit(readings, workers=1, deadline_seconds=610)

    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != third.fingerprint


def test_fit_cap_below_the_deadline_leaves_the_escalation_nothing_to_buy() -> None:
    """``cap_seconds`` under the granted rung cannot spare anything.

    The escalation has already climbed past the cap, so both arms return
    ``UNFIT``. This is *not* the no-cap reading in general — the p50 arm judges
    against the lower ceiling, so a cap below the deadline is strictly stricter
    than no cap (see the companion test below); it only coincides here because
    both ceilings condemn these rows.
    """
    censored = [_Reading(outcome="TIMEOUT", tests_seconds=0.0) for _ in range(3)]
    slow = [_Reading(tests_seconds=580.0) for _ in range(3)]

    assert ge.probe_fit(
        censored, workers=1, deadline_seconds=600, cap_seconds=500,
    ).kind == "UNFIT"
    assert ge.probe_fit(
        slow, workers=1, deadline_seconds=600, cap_seconds=500,
    ).kind == "UNFIT"


def test_fit_cap_below_the_deadline_is_stricter_than_no_cap_on_the_p50_arm() -> None:
    """A median that fits the granted rung can still exceed a lower cap.

    ``p50=460`` is under ``0.9 * 600`` but over ``0.9 * 500``: with no cap the
    suite fits, with the cap it does not. The cap is a ceiling on the reading,
    not a no-op.
    """
    readings = [_Reading(tests_seconds=460.0) for _ in range(3)]

    assert ge.probe_fit(readings, workers=1, deadline_seconds=600).kind == "SOUND"
    assert ge.probe_fit(
        readings, workers=1, deadline_seconds=600, cap_seconds=500,
    ).kind == "UNFIT"


def test_fit_p50_detail_names_the_cap_when_one_is_known() -> None:
    """The ceiling the detail quotes is the cap, so it must not say "deadline".

    Reporting ``500s deadline`` for a run the host actually kills at 600s
    misdirects the repair: the number is the escalation cap, not the granted
    budget.
    """
    readings = [_Reading(tests_seconds=460.0) for _ in range(3)]

    capped = ge.probe_fit(
        readings, workers=1, deadline_seconds=600, cap_seconds=500,
    )
    uncapped = ge.probe_fit(readings, workers=1, deadline_seconds=400)

    assert "500s cap" in capped.detail
    assert "deadline" not in capped.detail
    assert "400s deadline" in uncapped.detail


def test_fit_cap_at_the_deadline_is_the_no_cap_reading() -> None:
    """A rung that has *reached* the cap still reads ``UNFIT`` on both arms."""
    censored = [_Reading(outcome="TIMEOUT", tests_seconds=0.0) for _ in range(3)]
    slow = [_Reading(tests_seconds=580.0) for _ in range(3)]

    assert ge.probe_fit(
        censored, workers=1, deadline_seconds=600, cap_seconds=600,
    ).kind == "UNFIT"
    assert ge.probe_fit(
        slow, workers=1, deadline_seconds=600, cap_seconds=600,
    ).kind == "UNFIT"


def test_fit_spares_a_censored_majority_while_the_escalation_climbs() -> None:
    """Killed runs below the cap are the escalation working, not a broken host."""
    readings = [_Reading(outcome="TIMEOUT", tests_seconds=0.0) for _ in range(3)]
    verdict = ge.probe_fit(
        readings, workers=1, deadline_seconds=600, cap_seconds=1350,
    )

    assert verdict.kind == "SOUND"
    assert verdict.detail == "escalating"


def test_fit_spares_a_p50_above_the_rung_but_below_the_cap() -> None:
    """The median is judged against the cap once one is known."""
    readings = [_Reading(tests_seconds=580.0) for _ in range(3)]
    verdict = ge.probe_fit(
        readings, workers=1, deadline_seconds=600, cap_seconds=1350,
    )

    assert verdict.kind == "SOUND"
    assert verdict.detail == "escalating"


def test_fit_unfit_on_a_p50_above_the_cap_itself() -> None:
    """A median the cap cannot rescue is unfit even mid-escalation."""
    readings = [_Reading(tests_seconds=1300.0) for _ in range(3)]
    verdict = ge.probe_fit(
        readings, workers=1, deadline_seconds=600, cap_seconds=1350,
    )

    assert verdict.kind == "UNFIT"
    assert "1350s cap" in verdict.detail


def test_fit_comfortable_suite_below_the_cap_is_not_called_escalating() -> None:
    """``escalating`` names real pressure, not every rung sitting below the cap."""
    readings = [_Reading(tests_seconds=100.0) for _ in range(3)]
    verdict = ge.probe_fit(
        readings, workers=1, deadline_seconds=600, cap_seconds=1350,
    )

    assert verdict.kind == "SOUND"
    assert verdict.detail == "suite fits its deadline"


def test_fit_fingerprint_folds_the_cap() -> None:
    """The same rows and rung under a different cap are a different fact."""
    readings = [_Reading(tests_seconds=1300.0) for _ in range(3)]
    capped = ge.probe_fit(
        readings, workers=1, deadline_seconds=600, cap_seconds=1350,
    )
    uncapped = ge.probe_fit(readings, workers=1, deadline_seconds=600)

    assert capped.kind == "UNFIT"
    assert uncapped.kind == "UNFIT"
    assert capped.fingerprint != uncapped.fingerprint


def test_runnability_forwards_the_cap_to_the_fit_probe() -> None:
    """``probe_runnability`` derives no cap; it hands the caller's through."""
    readings = [_Reading(tests_seconds=580.0) for _ in range(3)]
    spared = ge.probe_runnability(
        collector=_clean_collector,
        readings=readings,
        workers=1,
        deadline_seconds=600,
        cap_seconds=1350,
        finder=_finder(dict(_ALL_RESOLVED)),
    )
    unspared = ge.probe_runnability(
        collector=_clean_collector,
        readings=readings,
        workers=1,
        deadline_seconds=600,
        finder=_finder(dict(_ALL_RESOLVED)),
    )

    assert spared.kind == "SOUND"
    assert spared.detail == "escalating"
    assert unspared.kind == "UNFIT"


def test_refresh_verdict_forwards_the_cap_to_the_fit_probe(
    mem_fs: dict[str, str],
) -> None:
    """The persisting door forwards the cap unchanged too."""
    readings = [_Reading(tests_seconds=580.0) for _ in range(3)]
    verdict = ge.refresh_verdict(
        VERDICT_PATH,
        iteration_key="i-cap",
        collector=_clean_collector,
        readings=readings,
        workers=1,
        deadline_seconds=600,
        cap_seconds=1350,
        finder=_finder(dict(_ALL_RESOLVED)),
        measured_at="2026-09-18T00:00:00+00:00",
    )

    assert verdict.kind == "SOUND"
    assert verdict.detail == "escalating"


# ---------------------------------------------------------------------------
# probe_runnability — precedence
# ---------------------------------------------------------------------------


def test_runnability_precedence_provisioning_first() -> None:
    """An unprovisioned sibling wins, and the collector is never invoked."""
    calls: list[int] = []

    def collector() -> tuple[int, str, str]:
        calls.append(1)
        return _clean_collector()

    resolved = dict(_ALL_RESOLVED)
    del resolved["maestro_sdk.mcp_config"]
    verdict = ge.probe_runnability(
        collector=collector,
        readings=[_Reading(tests_seconds=5000.0)],
        workers=1,
        deadline_seconds=600,
        finder=_finder(resolved),
    )

    assert verdict.kind == "UNPROVISIONED"
    assert calls == []


def test_runnability_precedence_collection_before_fit() -> None:
    """A suite that cannot be collected has no measurable fit."""
    verdict = ge.probe_runnability(
        collector=lambda: "TIMEOUT",
        readings=[_Reading(tests_seconds=5000.0)],
        workers=1,
        deadline_seconds=600,
        finder=_finder(dict(_ALL_RESOLVED)),
    )

    assert verdict.kind == "UNCOLLECTABLE"


def test_runnability_reaches_fit_and_then_sound() -> None:
    """All three passing is one ``SOUND`` verdict; a bad fit surfaces as ``UNFIT``."""
    sound = ge.probe_runnability(
        collector=_clean_collector,
        readings=[_Reading(tests_seconds=100.0) for _ in range(3)],
        workers=1,
        deadline_seconds=600,
        finder=_finder(dict(_ALL_RESOLVED)),
        measured_at="2026-09-18T00:00:00+00:00",
    )
    unfit = ge.probe_runnability(
        collector=_clean_collector,
        readings=[_Reading(tests_seconds=5000.0) for _ in range(3)],
        workers=1,
        deadline_seconds=600,
        finder=_finder(dict(_ALL_RESOLVED)),
    )

    assert sound.kind == "SOUND"
    assert sound.measured_at == "2026-09-18T00:00:00+00:00"
    assert unfit.kind == "UNFIT"


# ---------------------------------------------------------------------------
# The cached row
# ---------------------------------------------------------------------------


def _refresh(
    mem_fs: dict[str, str],
    *,
    iteration_key: str,
    collector: Any,
    readings: Any = (),
) -> ge.GateRunnabilityVerdict:
    """Call ``refresh_verdict`` with the sound-environment defaults."""
    return ge.refresh_verdict(
        VERDICT_PATH,
        iteration_key=iteration_key,
        collector=collector,
        readings=readings,
        workers=1,
        deadline_seconds=600,
        finder=_finder(dict(_ALL_RESOLVED)),
        measured_at="2026-09-18T00:00:00+00:00",
    )


def test_refresh_writes_the_row_and_read_verdict_round_trips(
    mem_fs: dict[str, str],
) -> None:
    """The persisted row reads back as the same verdict."""
    written = _refresh(mem_fs, iteration_key="i-1", collector=lambda: "TIMEOUT")
    read_back = ge.read_verdict(VERDICT_PATH)

    assert written.kind == "UNCOLLECTABLE"
    assert read_back == written
    assert json.loads(mem_fs[str(VERDICT_PATH)])["iteration_key"] == "i-1"


def test_refresh_issues_no_second_probe_within_one_iteration(
    mem_fs: dict[str, str],
) -> None:
    """A cached row for the same iteration key is returned without probing."""
    calls: list[int] = []

    def collector() -> tuple[int, str, str]:
        calls.append(1)
        return _clean_collector()

    first = _refresh(mem_fs, iteration_key="i-1", collector=collector)
    second = _refresh(mem_fs, iteration_key="i-1", collector=collector)

    assert calls == [1]
    assert second == first


def test_refresh_reprobes_on_a_new_iteration_key(mem_fs: dict[str, str]) -> None:
    """A different iteration key is a fresh measurement."""
    calls: list[int] = []

    def collector() -> tuple[int, str, str]:
        calls.append(1)
        return _clean_collector()

    _refresh(mem_fs, iteration_key="i-1", collector=collector)
    _refresh(mem_fs, iteration_key="i-2", collector=collector)

    assert calls == [1, 1]


def test_refresh_reprobes_over_a_corrupt_row(mem_fs: dict[str, str]) -> None:
    """A malformed cached row is re-probed rather than trusted."""
    mem_fs[str(VERDICT_PATH)] = "{not json"
    verdict = _refresh(mem_fs, iteration_key="i-1", collector=_clean_collector)

    assert verdict.kind == "SOUND"
    assert json.loads(mem_fs[str(VERDICT_PATH)])["kind"] == "SOUND"


def test_read_verdict_returns_none_when_nothing_was_probed(
    mem_fs: dict[str, str],
) -> None:
    """A missing file is the honest "nothing probed yet", never a synthesised SOUND."""
    assert ge.read_verdict(VERDICT_PATH) is None


@pytest.mark.parametrize(
    "payload",
    [
        "not json at all",
        json.dumps(["a", "list"]),
        json.dumps({"kind": "WHATEVER", "fingerprint": "x", "iteration_key": "i"}),
        json.dumps({"kind": "SOUND", "fingerprint": 7, "iteration_key": "i"}),
        json.dumps({"kind": "SOUND", "fingerprint": "x", "iteration_key": 7}),
        json.dumps(
            {
                "kind": "SOUND",
                "fingerprint": "x",
                "iteration_key": "i",
                "identities": "tests:TIMEOUT",
                "detail": "",
                "measured_at": "",
            },
        ),
        json.dumps(
            {
                "kind": "SOUND",
                "fingerprint": "x",
                "iteration_key": "i",
                "identities": [7],
                "detail": "",
                "measured_at": "",
            },
        ),
        json.dumps(
            {
                "kind": "SOUND",
                "fingerprint": "x",
                "iteration_key": "i",
                "identities": [],
                "detail": 7,
                "measured_at": "",
            },
        ),
        json.dumps(
            {
                "kind": "SOUND",
                "fingerprint": "x",
                "iteration_key": "i",
                "identities": [],
                "detail": "",
                "measured_at": 7,
            },
        ),
    ],
)
def test_read_verdict_tolerates_every_corruption_shape(
    mem_fs: dict[str, str], payload: str,
) -> None:
    """Every malformed row shape returns ``None`` rather than raising."""
    mem_fs[str(VERDICT_PATH)] = payload

    assert ge.read_verdict(VERDICT_PATH) is None


def test_read_verdict_runs_no_subprocess(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reader is the zero-subprocess door — it only reads its own row."""
    _refresh(mem_fs, iteration_key="i-1", collector=_clean_collector)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("read_verdict must not spawn a process")

    monkeypatch.setattr("subprocess.run", explode)
    monkeypatch.setattr("subprocess.Popen", explode)

    assert ge.read_verdict(VERDICT_PATH) is not None


def test_refresh_creates_the_parent_directory_before_locking(
    mem_fs: dict[str, str],
) -> None:
    """``file_lock`` opens its ``.lock`` with ``O_CREAT`` — the dir must exist.

    On a checkout whose memory directory has not been created yet, locking
    before the ``mkdir`` would raise ``FileNotFoundError`` out of the first
    probe. Recorded rather than observed because ``mem_fs`` fakes the
    filesystem, so the omission is invisible to a behavioural assertion.
    """
    calls: list[tuple[str, bool, bool]] = []

    def record_mkdir(
        self: Path, *, parents: bool = False, exist_ok: bool = False,
    ) -> None:
        calls.append((str(self), parents, exist_ok))

    # A ``with`` block rather than ``monkeypatch``: the spy must be unwound
    # *inside* the still-active ``mem_fs`` patches. A ``monkeypatch.setattr``
    # here would capture ``mem_fs``'s fake as the value to restore and — when
    # its finalizer runs after ``mem_fs`` has already put the real ``mkdir``
    # back — reinstall that no-op fake process-wide, breaking ``tmp_path`` for
    # every later test.
    with patch.object(Path, "mkdir", record_mkdir):
        _refresh(mem_fs, iteration_key="i-1", collector=_clean_collector)

    assert (str(VERDICT_PATH.parent), True, True) in calls


def test_config_exposes_the_verdict_path() -> None:
    """The cached row sits beside the gate-run telemetry under ``memory_dir``."""
    config = Config(memory_dir=Path("/fake/data"))

    assert config.gate_environment_path == Path("/fake/data/gate_environment.json")
    assert config.gate_environment_path.parent == config.gate_runs_path.parent


# ---------------------------------------------------------------------------
# The ask-once notice sidecar (step 1)
# ---------------------------------------------------------------------------

NOTICE_PATH = Path("/fake/data/gate_environment_notice.json")


def test_notice_key_pairs_the_kind_with_its_sorted_identities() -> None:
    """The key is the fact — the kind plus its identities, in a stable order."""
    one = ge.GateRunnabilityVerdict(
        kind="UNCOLLECTABLE", fingerprint="aaaa",
        identities=frozenset({"tests:COLLECTION_ERROR", "tests:FAILED"}),
    )
    two = dataclasses.replace(
        one, identities=frozenset({"tests:FAILED", "tests:COLLECTION_ERROR"}),
    )

    assert ge.notice_key(one) == ge.notice_key(two)
    assert ge.notice_key(one).startswith("UNCOLLECTABLE")


def test_notice_key_is_stable_across_a_moving_fingerprint() -> None:
    """A merge moves an ``UNCOLLECTABLE`` fingerprint; the fact is unchanged."""
    at_old_head = ge.probe_collection(
        lambda: (2, "E   ImportError: no module named x", ""), head="oldhead",
    )
    at_new_head = ge.probe_collection(
        lambda: (2, "E   ImportError: no module named x", ""), head="newhead",
    )

    assert at_old_head.fingerprint != at_new_head.fingerprint
    assert ge.notice_key(at_old_head) == ge.notice_key(at_new_head)


def test_notice_key_separates_two_distinct_kinds() -> None:
    """A different way of being unable to answer is a different key."""
    unprovisioned = ge.GateRunnabilityVerdict(kind="UNPROVISIONED", fingerprint="a")
    unfit = ge.GateRunnabilityVerdict(kind="UNFIT", fingerprint="a")

    assert ge.notice_key(unprovisioned) != ge.notice_key(unfit)


def test_notice_round_trips(mem_fs: dict[str, str]) -> None:
    """``record_notice`` writes what ``read_notice`` reads back."""
    ge.record_notice(NOTICE_PATH, "UNPROVISIONED")

    assert json.loads(mem_fs[str(NOTICE_PATH)]) == {"key": "UNPROVISIONED"}
    assert ge.read_notice(NOTICE_PATH) == "UNPROVISIONED"


def test_notice_round_trips_an_empty_clearing_key(mem_fs: dict[str, str]) -> None:
    """A ``SOUND`` verdict clears the notice by writing the empty key."""
    ge.record_notice(NOTICE_PATH, "UNPROVISIONED")
    ge.record_notice(NOTICE_PATH, "")

    assert ge.read_notice(NOTICE_PATH) == ""


def test_notice_overwrites_rather_than_appending(mem_fs: dict[str, str]) -> None:
    """The sidecar is a single row, not a ledger."""
    ge.record_notice(NOTICE_PATH, "UNPROVISIONED")
    ge.record_notice(NOTICE_PATH, "UNFIT")

    assert ge.read_notice(NOTICE_PATH) == "UNFIT"
    assert json.loads(mem_fs[str(NOTICE_PATH)]) == {"key": "UNFIT"}


def test_read_notice_on_a_missing_file_is_none(mem_fs: dict[str, str]) -> None:
    """Nothing asked yet reads as ``None``, never as a match."""
    assert ge.read_notice(NOTICE_PATH) is None


@pytest.mark.parametrize(
    "raw", ["{not json", '["a list"]', '{"key": 7}', '{"other": "x"}'],
)
def test_read_notice_tolerates_a_corrupt_row(
    mem_fs: dict[str, str], raw: str,
) -> None:
    """Malformed JSON, a non-dict row, and a mistyped/absent key all read None."""
    mem_fs[str(NOTICE_PATH)] = raw

    assert ge.read_notice(NOTICE_PATH) is None


def test_record_notice_creates_the_parent_directory(
    mem_fs: dict[str, str],
) -> None:
    """``file_lock`` opens an adjacent ``.lock``, so the parent must exist."""
    calls: list[tuple[str, bool, bool]] = []

    def record_mkdir(
        self: Path, *, parents: bool = False, exist_ok: bool = False,
    ) -> None:
        calls.append((str(self), parents, exist_ok))

    with patch.object(Path, "mkdir", record_mkdir):
        ge.record_notice(NOTICE_PATH, "UNFIT")

    assert (str(NOTICE_PATH.parent), True, True) in calls


def test_config_exposes_the_notice_path() -> None:
    """The notice sidecar sits beside the verdict row under ``memory_dir``."""
    config = Config(memory_dir=Path("/fake/data"))

    assert config.gate_environment_notice_path == NOTICE_PATH
    assert (
        config.gate_environment_notice_path.parent
        == config.gate_environment_path.parent
    )


# ---------------------------------------------------------------------------
# The declining predicates — which verdicts refuse a gate run, and under which
# identities a refusal is reported (proposal 133 lease-seam binding).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "declines"),
    [
        ("SOUND", False),
        ("UNPROVISIONED", True),
        ("UNCOLLECTABLE", True),
        ("UNFIT", False),
    ],
)
def test_declines_gate_run_covers_every_kind(kind: str, declines: bool) -> None:
    """Only the two "cannot answer at all" kinds refuse a run; UNFIT still runs."""
    verdict = ge.GateRunnabilityVerdict(kind=kind, fingerprint="f")  # type: ignore[arg-type]

    assert ge.declines_gate_run(verdict) is declines


@pytest.mark.parametrize(
    ("kind", "blames"),
    [
        ("SOUND", False),
        ("UNPROVISIONED", True),
        ("UNCOLLECTABLE", False),
        ("UNFIT", False),
    ],
)
def test_blames_missing_sibling_covers_every_kind(kind: str, blames: bool) -> None:
    """Only a missing sibling install explains a red lint/type gate."""
    verdict = ge.GateRunnabilityVerdict(kind=kind, fingerprint="f")  # type: ignore[arg-type]

    assert ge.blames_missing_sibling(verdict) is blames


def test_blames_missing_sibling_is_false_without_a_verdict() -> None:
    """Nothing probed yet blames nothing."""
    assert ge.blames_missing_sibling(None) is False


def test_declined_identities_pass_through_a_carrying_verdict() -> None:
    """An identity-carrying verdict is reported under its own identities."""
    identities = frozenset({"tests:a.py::test_one", "tests:COLLECTION_ERROR"})
    verdict = ge.GateRunnabilityVerdict(
        kind="UNCOLLECTABLE", fingerprint="f", identities=identities,
    )

    assert ge.declined_gate_identities(verdict) == identities


@pytest.mark.parametrize(
    "kind", ["SOUND", "UNPROVISIONED", "UNCOLLECTABLE", "UNFIT"],
)
def test_declined_identities_synthesize_a_collection_abort(kind: str) -> None:
    """An identity-less verdict makes the whole-run claim: a collection abort."""
    verdict = ge.GateRunnabilityVerdict(kind=kind, fingerprint="f")  # type: ignore[arg-type]

    assert ge.declined_gate_identities(verdict) == frozenset(
        {"tests:COLLECTION_ERROR"},
    )


def test_declined_identities_match_the_collection_probe_signature() -> None:
    """The synthesized identity is the spelling a real collection red produces."""
    verdict = ge.probe_collection(lambda: (2, "ERROR collecting tests/x.py", ""))

    assert ge.declined_gate_identities(verdict) == verdict.identities
    assert ge.declines_gate_run(verdict) is True


# ---------------------------------------------------------------------------
# format_verdict — the one-line operator rendering (step 9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", ["SOUND", "UNPROVISIONED", "UNCOLLECTABLE", "UNFIT"],
)
def test_format_verdict_leads_with_the_kind(kind: str) -> None:
    """Every kind renders as one line naming itself and its detail."""
    verdict = ge.GateRunnabilityVerdict(
        kind=kind, fingerprint="f", detail="why",  # type: ignore[arg-type]
    )

    assert ge.format_verdict(verdict) == f"gate runnability: {kind} — why"


def test_format_verdict_appends_sorted_identities() -> None:
    """A verdict carrying identities renders them sorted and bracketed."""
    verdict = ge.GateRunnabilityVerdict(
        kind="UNCOLLECTABLE",
        fingerprint="f",
        identities=frozenset({"tests:b", "tests:a"}),
        detail="collection COLLECTION_ERROR",
    )

    assert ge.format_verdict(verdict) == (
        "gate runnability: UNCOLLECTABLE — collection COLLECTION_ERROR "
        "[tests:a, tests:b]"
    )


def test_format_verdict_omits_an_empty_detail_and_empty_identities() -> None:
    """The bare verdict renders as the kind alone — no dangling separators."""
    verdict = ge.GateRunnabilityVerdict(kind="SOUND", fingerprint="f")

    assert ge.format_verdict(verdict) == "gate runnability: SOUND"


def test_format_verdict_truncates_an_unbounded_identity_set() -> None:
    """An unbounded identities set cannot render a screenful (context-bloat cap)."""
    verdict = ge.GateRunnabilityVerdict(
        kind="UNCOLLECTABLE",
        fingerprint="f",
        identities=frozenset(f"tests:module_{i:03d}" for i in range(200)),
        detail="collection COLLECTION_ERROR",
    )

    line = ge.format_verdict(verdict)

    assert len(line) == ge._VERDICT_LINE_MAX
    assert line.endswith("…")


def test_format_verdict_renders_the_provisioning_repair() -> None:
    """The preflight FAIL line carries the actionable install command."""
    resolved = dict(_ALL_RESOLVED)
    del resolved["maestro_sdk.mcp_config"]

    line = ge.format_verdict(ge.probe_provisioning(finder=_finder(resolved)))

    assert "namespace shadow" in line
    assert 'pip install -e "maestro_sdk/"' in line

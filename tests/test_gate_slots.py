"""Tests for the gate slot planner and lease slot scoping in ``maestro/gate_capacity.py``.

``plan_gate_slots`` divides a host's usable CPUs into CPU-disjoint gate slots
sized by both cores and total memory, so ``scripts/gate-slot.sh`` adapts to any
machine instead of assuming 8 vCPU / 24 GB; ``_slot_scope`` keeps two pinned,
core-disjoint gate runs from serialising on one lease lock. The planner is pure;
the readers are exercised against mocked kernel/sysfs files and a mocked
``os.sched_getaffinity`` — no real ``/proc`` or ``/sys`` read.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from maestro import gate_capacity as gc

#: One admitted run's resident need at the minimum width — derived from the
#: module's own figures so these tests track a retune.
_RUN_MIB = gc.SESSION_BASELINE_MIB + gc.MIN_WORKERS_PER_ADMITTED_RUN * gc.PER_WORKER_MIB
_MIN = gc.MIN_WORKERS_PER_ADMITTED_RUN
_BIG_MIB = 256 * 1024


def _singles(ids: range | list[int]) -> list[tuple[int, ...]]:
    """One group per CPU — a host without SMT."""
    return [(i,) for i in ids]


def _mib_for_slots(slots: int) -> int:
    """Total MiB at which exactly *slots* runs fit the memory budget."""
    return gc.SYSTEM_RESERVE_MIB + slots * _RUN_MIB


def _file(body: str) -> MagicMock:
    stub = MagicMock()
    stub.read_text.return_value = body
    return stub


def _absent() -> MagicMock:
    stub = MagicMock()
    stub.read_text.side_effect = FileNotFoundError("absent")
    return stub


@pytest.fixture(autouse=True)
def _neutralize_gate_lease() -> Iterator[None]:
    """Opt this module out of conftest's gate-lease neutralizer, as
    ``tests/test_gate_capacity.py`` does: these tests drive the real
    ``acquire_slot`` / ``count_in_flight`` bodies, with every OS call mocked."""
    yield


@pytest.fixture(autouse=True)
def _no_proc_self_cgroup() -> Iterator[None]:
    """``/proc/self/cgroup`` absent, so no test walks the real cgroup ancestry."""
    with patch.object(gc, "_PROC_SELF_CGROUP_PATH", _absent()):
        yield


@pytest.fixture
def no_cgroup_memory() -> Iterator[None]:
    """Both cgroup memory-limit files absent, so only ``MemTotal`` bounds memory."""
    with (
        patch.object(gc, "_CGROUP_V2_MEMORY_MAX_PATH", _absent()),
        patch.object(gc, "_CGROUP_V1_MEMORY_LIMIT_PATH", _absent()),
    ):
        yield


# --- plan_gate_slots (pure) -------------------------------------------------


def test_reference_host_splits_into_two_disjoint_slots() -> None:
    """8 vCPU / 24 GB: two slots, CPUs 0-3 and 4-7, at the minimum width each."""
    plan = gc.plan_gate_slots(_singles(range(8)), 0, 24 * 1024)

    assert plan.cpus == ((0, 1, 2, 3), (4, 5, 6, 7))
    assert plan.workers == _MIN


def test_cores_bound_the_slot_count_on_a_big_memory_host() -> None:
    plan = gc.plan_gate_slots(_singles(range(16)), 0, _BIG_MIB)

    assert len(plan.cpus) == 16 // _MIN
    assert plan.workers == _MIN


def test_memory_bounds_slots_and_pins_to_the_clamped_width() -> None:
    """16 cores but memory for two runs: two slots, each pinned to exactly the
    memory-clamped width so the lease's affinity read enforces the clamp."""
    total = _mib_for_slots(2)
    plan = gc.plan_gate_slots(_singles(range(16)), 0, total)

    per_slot = (total - gc.SYSTEM_RESERVE_MIB) // 2
    width = min(8, (per_slot - gc.SESSION_BASELINE_MIB) // gc.PER_WORKER_MIB)
    assert len(plan.cpus) == 2
    assert plan.workers == width
    assert plan.cpus[0] == tuple(range(width))
    assert plan.cpus[1] == tuple(range(8, 8 + width))


def test_slots_never_share_a_cpu() -> None:
    plan = gc.plan_gate_slots(_singles(range(24)), 0, _BIG_MIB)
    seen: set[int] = set()
    for cpus in plan.cpus:
        assert seen.isdisjoint(cpus)
        seen.update(cpus)


def test_smt_siblings_stay_in_one_slot() -> None:
    """Siblings numbered i / i+4 (common on cloud vCPUs) are kept together, so
    the two slots own disjoint physical cores."""
    groups = [(0, 4), (1, 5), (2, 6), (3, 7)]
    plan = gc.plan_gate_slots(groups, 0, 24 * 1024)

    assert plan.cpus == ((0, 4, 1, 5), (2, 6, 3, 7))


def test_pins_to_real_cpu_ids_of_a_cpuset() -> None:
    plan = gc.plan_gate_slots(_singles(range(4, 12)), 0, 24 * 1024)

    assert plan.cpus == ((4, 5, 6, 7), (8, 9, 10, 11))


def test_cfs_quota_claims_cpus_without_pinning() -> None:
    """A 2-core CFS quota on an 8-CPU affinity set: one slot of width 2 that claims
    two CPU ids from the shared pool but is not confined to them."""
    plan = gc.plan_gate_slots(_singles(range(8)), 2, 24 * 1024)

    assert plan.cpus == ((0, 1),)
    assert plan.workers == 2
    assert plan.pinned is False


def test_cpuset_plans_are_pinned() -> None:
    assert gc.plan_gate_slots(_singles(range(8)), 0, 24 * 1024).pinned is True


def test_quota_at_or_above_the_cpu_count_is_not_a_cap() -> None:
    plan = gc.plan_gate_slots(_singles(range(8)), 8, 24 * 1024)

    assert plan.cpus == ((0, 1, 2, 3), (4, 5, 6, 7))


def test_remainder_cores_idle_rather_than_oversubscribe() -> None:
    plan = gc.plan_gate_slots(_singles(range(9)), 0, _BIG_MIB)

    assert plan.cpus == ((0, 1, 2, 3), (4, 5, 6, 7))


def test_small_host_floors_to_one_slot_and_one_worker() -> None:
    """2 cores / 2 GB: one slot; memory admits a single worker, pinned to one CPU."""
    plan = gc.plan_gate_slots(_singles(range(2)), 0, 2048)

    assert plan.cpus == ((0,),)
    assert plan.workers == 1


def test_unknown_memory_sizes_by_cores_alone() -> None:
    plan = gc.plan_gate_slots(_singles(range(8)), 0, 0)

    assert plan.cpus == ((0, 1, 2, 3), (4, 5, 6, 7))
    assert plan.workers == _MIN


def test_unknown_cpu_ids_yield_one_unpinned_slot_of_unknown_width() -> None:
    plan = gc.plan_gate_slots([], 0, 24 * 1024)

    assert plan.cpus == ((),)
    assert plan.workers == 0
    assert plan.pinned is False


def test_duplicate_empty_and_unsorted_groups_are_normalised() -> None:
    groups: list[tuple[int, ...]] = [(7,), (3,), (3,), (), (5,), (1,), (0,), (2,), (4,), (6,)]
    plan = gc.plan_gate_slots(groups, 0, 24 * 1024)

    assert plan.cpus == ((0, 1, 2, 3), (4, 5, 6, 7))


# --- readers ----------------------------------------------------------------


@pytest.mark.usefixtures("no_cgroup_memory")
def test_total_mib_reads_memtotal() -> None:
    meminfo = _file("MemTotal:       25165824 kB\nMemAvailable:   16384000 kB\n")
    with patch.object(gc, "_MEMINFO_PATH", meminfo):
        assert gc._read_total_mib() == 24576


def test_total_mib_takes_the_smaller_cgroup_v2_limit() -> None:
    with (
        patch.object(gc, "_MEMINFO_PATH", _file("MemTotal:       25165824 kB\n")),
        patch.object(gc, "_CGROUP_V2_MEMORY_MAX_PATH", _file(str(8 * 1024**3))),
        patch.object(gc, "_CGROUP_V1_MEMORY_LIMIT_PATH", _absent()),
    ):
        assert gc._read_total_mib() == 8192


def test_total_mib_ignores_unlimited_cgroup_sentinels() -> None:
    with (
        patch.object(gc, "_MEMINFO_PATH", _file("MemTotal:       25165824 kB\n")),
        patch.object(gc, "_CGROUP_V2_MEMORY_MAX_PATH", _file("max")),
        patch.object(
            gc, "_CGROUP_V1_MEMORY_LIMIT_PATH", _file(str(gc._CGROUP_UNLIMITED_BYTES)),
        ),
    ):
        assert gc._read_total_mib() == 24576


def test_total_mib_falls_back_to_cgroup_limit_without_meminfo() -> None:
    with (
        patch.object(gc, "_MEMINFO_PATH", _absent()),
        patch.object(gc, "_CGROUP_V2_MEMORY_MAX_PATH", _absent()),
        patch.object(gc, "_CGROUP_V1_MEMORY_LIMIT_PATH", _file(str(4 * 1024**3))),
    ):
        assert gc._read_total_mib() == 4096


@pytest.mark.usefixtures("no_cgroup_memory")
def test_total_mib_is_zero_when_nothing_is_known() -> None:
    with patch.object(gc, "_MEMINFO_PATH", _absent()):
        assert gc._read_total_mib() == 0


def test_meminfo_mib_rejects_a_malformed_field() -> None:
    with patch.object(gc, "_MEMINFO_PATH", _file("MemTotal:       lots kB\n")):
        assert gc._meminfo_mib("MemTotal") == 0


def test_meminfo_mib_absent_field_is_zero() -> None:
    with patch.object(gc, "_MEMINFO_PATH", _file("MemFree:   1024 kB\n")):
        assert gc._meminfo_mib("MemTotal") == 0


def test_affinity_cpu_ids_are_sorted() -> None:
    with patch("maestro.gate_capacity.os.sched_getaffinity", return_value={6, 4, 5}):
        assert gc._read_affinity_cpu_ids() == (4, 5, 6)


def test_affinity_cpu_ids_empty_without_sched_getaffinity() -> None:
    with patch(
        "maestro.gate_capacity.os.sched_getaffinity",
        side_effect=AttributeError("no sched_getaffinity"),
    ):
        assert gc._read_affinity_cpu_ids() == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0,4\n", (0, 4)),
        ("0-1\n", (0, 1)),
        ("0-1,8\n", (0, 1, 8)),
        ("3", (3,)),
        ("x", ()),
        ("1-y", ()),
        ("", ()),
    ],
)
def test_parse_cpu_list(text: str, expected: tuple[int, ...]) -> None:
    assert gc._parse_cpu_list(text) == expected


def _topology(siblings: dict[int, str | None]) -> MagicMock:
    """A stand-in for ``_CPU_TOPOLOGY_DIR`` whose per-CPU sibling files read
    *siblings* (``None`` = unreadable)."""

    def child(_self: object, name: str) -> MagicMock:
        cpu = int(name.removeprefix("cpu"))
        body = siblings[cpu]
        leaf = _absent() if body is None else _file(body)
        topology = MagicMock()
        topology.__truediv__ = lambda _t, _n: leaf
        node = MagicMock()
        node.__truediv__ = lambda _n2, _x: topology
        return node

    root = MagicMock(spec=Path)
    root.__truediv__ = child
    return root


def test_cpu_groups_pair_smt_siblings_within_the_affinity_set() -> None:
    siblings = {0: "0,4", 1: "1,5", 4: "0,4", 5: "1,5"}
    with patch.object(gc, "_CPU_TOPOLOGY_DIR", _topology(siblings)):
        assert gc._read_cpu_groups([0, 1, 4, 5]) == ((0, 4), (1, 5))


def test_cpu_groups_drop_siblings_outside_the_affinity_set() -> None:
    with patch.object(gc, "_CPU_TOPOLOGY_DIR", _topology({0: "0,4", 1: "1,5"})):
        assert gc._read_cpu_groups([0, 1]) == ((0,), (1,))


def test_cpu_groups_fall_back_to_singletons_when_unreadable() -> None:
    with patch.object(gc, "_CPU_TOPOLOGY_DIR", _topology({2: None, 3: "garbage"})):
        assert gc._read_cpu_groups([2, 3]) == ((2,), (3,))


def test_measure_gate_slots_composes_the_readings() -> None:
    with (
        patch.object(gc, "_read_affinity_cpu_ids", return_value=tuple(range(8))),
        patch.object(gc, "_read_cpu_groups", return_value=tuple(_singles(range(8)))),
        patch.object(gc, "_read_cgroup_quota_cores", return_value=0),
        patch.object(gc, "_read_total_mib", return_value=24 * 1024),
    ):
        plan = gc.measure_gate_slots()

    assert plan == gc.plan_gate_slots(_singles(range(8)), 0, 24 * 1024)


# --- lease slot scoping -----------------------------------------------------


def test_unpinned_process_keeps_historical_slot_names() -> None:
    with (
        patch.object(gc, "_read_affinity_cpu_ids", return_value=tuple(range(8))),
        patch("maestro.gate_capacity.os.cpu_count", return_value=8),
    ):
        assert gc._slot_scope() == ""


def test_unknown_affinity_keeps_historical_slot_names() -> None:
    with patch.object(gc, "_read_affinity_cpu_ids", return_value=()):
        assert gc._slot_scope() == ""


def test_pinned_process_scopes_slots_by_cpu_ranges() -> None:
    with (
        patch.object(gc, "_read_affinity_cpu_ids", return_value=(0, 1, 2, 3, 8, 10, 11)),
        patch("maestro.gate_capacity.os.cpu_count", return_value=16),
    ):
        assert gc._slot_scope() == "cpus0-3_8_10-11-"


def test_slot_lock_path_joins_scope_and_index() -> None:
    assert gc._slot_lock_path(Path("/locks"), "cpus4-7-", 0) == Path(
        "/locks/gate-slot-cpus4-7-0.lock",
    )
    assert gc._slot_lock_path(Path("/locks"), "", 1) == Path("/locks/gate-slot-1.lock")


@pytest.mark.parametrize("probe", ["acquire", "count"])
def test_lease_probes_open_the_scoped_slot_files(probe: str) -> None:
    """Both lease probes open the CPU-scoped file, so two disjoint pinned runs
    never contend for — or count — each other's slot."""
    lock_dir = Path("/locks")
    mock_open = MagicMock(return_value=42)
    with (
        patch.object(gc, "_slot_scope", return_value="cpus4-7-"),
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", mock_open),
        patch("maestro.gate_capacity.os.close"),
        patch("maestro.gate_capacity.fcntl.flock"),
    ):
        if probe == "acquire":
            with gc.acquire_slot(lock_dir, slots=1) as admission:
                assert admission == "slot"
        else:
            assert gc.count_in_flight(lock_dir, slots=1) == 0

    opened = mock_open.call_args.args[0]
    assert opened == str(lock_dir / "gate-slot-cpus4-7-0.lock")


# --- overlap-aware lease counting --------------------------------------------


@pytest.mark.parametrize(
    ("scope", "expected"),
    [("", None), ("cpus0-3-", frozenset({0, 1, 2, 3})), ("cpus0-1_4_6-7-", frozenset({0, 1, 4, 6, 7}))],
)
def test_scope_cpus_parses_the_file_scope(scope: str, expected: frozenset[int] | None) -> None:
    assert gc._scope_cpus(scope) == expected


_LISTING = [
    "gate-slot-0.lock",            # unscoped: covers every CPU
    "gate-slot-cpus0-3-0.lock",    # pinned 0-3
    "gate-slot-cpus2-5-0.lock",    # pinned 2-5
    "gate-slot-cpus4-7-0.lock",    # pinned 4-7
    "gate-runs.log",               # not a slot file
]


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        # pinned 0-3: the unscoped lease and 2-5 overlap; 4-7 is disjoint; its own file is skipped
        ("cpus0-3-", 2),
        # unpinned: every pinned scope overlaps it; its own (unscoped) file is skipped
        ("", 3),
        # pinned 4-7: the unscoped lease and 2-5 overlap; 0-3 is disjoint
        ("cpus4-7-", 2),
    ],
)
def test_foreign_held_counts_only_overlapping_scopes(scope: str, expected: int) -> None:
    with (
        patch("maestro.gate_capacity.os.listdir", return_value=list(_LISTING)),
        patch.object(gc, "_probe_held", return_value=True),
    ):
        assert gc._foreign_held(Path("/locks"), scope) == expected


def test_foreign_held_skips_free_files() -> None:
    with (
        patch("maestro.gate_capacity.os.listdir", return_value=list(_LISTING)),
        patch.object(gc, "_probe_held", return_value=False),
    ):
        assert gc._foreign_held(Path("/locks"), "") == 0


def test_foreign_held_without_a_lock_dir_is_zero() -> None:
    with patch("maestro.gate_capacity.os.listdir", side_effect=FileNotFoundError("absent")):
        assert gc._foreign_held(Path("/locks"), "cpus0-3-") == 0


def test_probe_held_reads_a_busy_file_as_held() -> None:
    mock_close = MagicMock()
    with (
        patch("maestro.gate_capacity.os.open", return_value=7),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch("maestro.gate_capacity.fcntl.flock", side_effect=BlockingIOError),
    ):
        assert gc._probe_held(Path("/locks/x.lock")) is True
    mock_close.assert_called_once_with(7)


def test_probe_held_releases_a_free_file() -> None:
    mock_flock = MagicMock()
    mock_close = MagicMock()
    with (
        patch("maestro.gate_capacity.os.open", return_value=7),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch("maestro.gate_capacity.fcntl.flock", mock_flock),
    ):
        assert gc._probe_held(Path("/locks/x.lock")) is False
    assert mock_flock.call_count == 2
    mock_close.assert_called_once_with(7)


def test_probe_held_treats_a_vanished_file_as_free() -> None:
    with patch("maestro.gate_capacity.os.open", side_effect=FileNotFoundError("gone")):
        assert gc._probe_held(Path("/locks/x.lock")) is False


def test_probe_held_closes_and_reraises_an_unexpected_flock_error() -> None:
    mock_close = MagicMock()
    with (
        patch("maestro.gate_capacity.os.open", return_value=7),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch("maestro.gate_capacity.fcntl.flock", side_effect=OSError("ENOLCK")),
        pytest.raises(OSError, match="ENOLCK"),
    ):
        gc._probe_held(Path("/locks/x.lock"))
    mock_close.assert_called_once_with(7)


def test_count_in_flight_adds_overlapping_foreign_leases() -> None:
    with (
        patch.object(gc, "_slot_scope", return_value="cpus0-3-"),
        patch.object(gc, "_foreign_held", return_value=2),
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", return_value=42),
        patch("maestro.gate_capacity.os.close"),
        patch("maestro.gate_capacity.fcntl.flock"),
    ):
        assert gc.count_in_flight(Path("/locks"), slots=1) == 2


def _hold(foreign: int, own_peer_held: set[int], slots: int) -> tuple[int | None, MagicMock]:
    """Run ``_hold_first_free_slot`` with every own-scope slot free to take, *foreign*
    overlapping leases, and same-scope peers holding the indices in *own_peer_held*."""
    mock_flock = MagicMock()

    def probe(path: Path) -> bool:
        return any(path.name == f"gate-slot-cpus0-3-{i}.lock" for i in own_peer_held)

    with (
        patch.object(gc, "_slot_scope", return_value="cpus0-3-"),
        patch.object(gc, "_foreign_held", return_value=foreign),
        patch.object(gc, "_probe_held", side_effect=probe),
        patch("maestro.gate_capacity.os.open", return_value=42),
        patch("maestro.gate_capacity.os.close"),
        patch("maestro.gate_capacity.fcntl.flock", mock_flock),
    ):
        return gc._hold_first_free_slot(Path("/locks"), slots), mock_flock


def test_hold_admits_within_the_shared_budget() -> None:
    """slots=2, one overlapping foreign lease, no same-scope peer: 1 + 0 + 1 <= 2."""
    fd, flock = _hold(foreign=1, own_peer_held=set(), slots=2)
    assert fd == 42
    assert flock.call_count == 1  # the exclusive take only — nothing released


def test_hold_counts_a_peer_at_a_high_index() -> None:
    """slots=2, one foreign lease and a same-scope peer on slot 1 (slot 0 free after
    its holder exited): taking slot 0 would make three runs, so it is released."""
    fd, flock = _hold(foreign=1, own_peer_held={1}, slots=2)
    assert fd is None
    assert flock.call_args_list[-1].args[1] == gc.fcntl.LOCK_UN


def test_hold_rechecks_foreign_leases_after_taking_the_slot() -> None:
    """Under a one-slot budget an overlapping run seen only after taking the slot
    (the simultaneous-start race) makes this run back off rather than both proceed."""
    fd, flock = _hold(foreign=1, own_peer_held=set(), slots=1)
    assert fd is None
    assert flock.call_args_list[-1].args[1] == gc.fcntl.LOCK_UN


def test_acquire_waits_when_overlapping_leases_fill_the_budget() -> None:
    """A run whose CPUs are fully leased by overlapping scopes keeps no slot of its
    own: it waits out the bounded acquire and fails open."""
    with (
        patch.object(gc, "_slot_scope", return_value=""),
        patch.object(gc, "_foreign_held", return_value=2),
        patch.object(gc, "_probe_held", return_value=False),
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", return_value=42),
        patch("maestro.gate_capacity.os.close"),
        patch("maestro.gate_capacity.fcntl.flock"),
        patch("maestro.gate_capacity.time.monotonic", side_effect=[0.0, 99.0]),
        patch("maestro.gate_capacity.time.sleep"),
        gc.acquire_slot(Path("/locks"), slots=2, wait_seconds=1.0) as admission,
    ):
        assert admission == "fallback"

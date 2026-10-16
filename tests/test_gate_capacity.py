"""Tests for maestro.gate_capacity — the pure lease policy, the confined
counting-semaphore seam, and the live-host readers (proposal 126).

The policy (`plan_lease`, `HostCapacity`, `GateLease`) is pure, so those tests
run in-memory with no patching. The impure symbols — the `acquire_slot`
semaphore and the `measure_host_capacity` / `count_in_flight` readers — patch
their I/O (`os.open`/`os.close`/`fcntl.flock`, `os.cpu_count`, the
`_MEMINFO_PATH` read, and `time.monotonic`/`time.sleep`) so there is no real
lock contention, host read, or sleep — mirroring `tests/test_fileutil.py`'s
`file_lock` coverage.

The capacity readers are cgroup-aware — `_read_available_mib` and
`_available_cores` take the tighter of the host figure and the cgroup limit — so
they also read eight cgroup path constants (seven under `/sys/fs/cgroup` plus
`/proc/self/cgroup`). The autouse
`_no_cgroup_limits` fixture patches all eight to an absent file by default, so
every existing host-figure test stays deterministic (no cgroup limit ⇒ the host
read governs) and no test ever touches the real `/sys/fs/cgroup` tree; a test
that needs a limit present patches the specific path(s) it exercises.
"""

from __future__ import annotations

import dataclasses
import fcntl
import json
import logging
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, get_args
from unittest.mock import MagicMock, call, patch

import pytest

from maestro import gate_capacity as gc
from maestro.gate_observation import GateObservationKind

LOCK_DIR = Path("/fake/gate-slots")
#: The telemetry sidecar path the recording tests write to under ``mem_fs``.
RUNS_PATH = Path("/fake/data/gate_runs.jsonl")
#: The exclusive non-blocking probe acquire_slot holds a slot with.
_NB = fcntl.LOCK_EX | fcntl.LOCK_NB
#: The *shared* non-blocking probe count_in_flight reads the slot set with, so
#: two concurrent counters do not mis-read each other's transient probe locks.
_SH = fcntl.LOCK_SH | fcntl.LOCK_NB

#: The eight cgroup path constants the capacity readers consult, so the autouse
#: fixture below patches exactly the set the readers read.
_CGROUP_PATH_ATTRS = (
    "_CGROUP_V2_MEMORY_MAX_PATH",
    "_CGROUP_V2_MEMORY_CURRENT_PATH",
    "_CGROUP_V1_MEMORY_LIMIT_PATH",
    "_CGROUP_V1_MEMORY_USAGE_PATH",
    "_CGROUP_V2_CPU_MAX_PATH",
    "_CGROUP_V1_CPU_QUOTA_PATH",
    "_CGROUP_V1_CPU_PERIOD_PATH",
    "_PROC_SELF_CGROUP_PATH",
)


def _mock_file(body: str) -> MagicMock:
    """A cgroup path whose ``read_text`` returns *body* (a present, readable file)."""
    stub = MagicMock()
    stub.read_text.return_value = body
    return stub


def _absent_file() -> MagicMock:
    """A cgroup path whose ``read_text`` raises — the file is absent (off Linux /
    a different cgroup version), the documented ``OSError`` the readers fall to 0 on."""
    stub = MagicMock()
    stub.read_text.side_effect = FileNotFoundError
    return stub


@pytest.fixture(autouse=True)
def _no_cgroup_limits() -> Iterator[None]:
    """Default every test to a host with no cgroup limits.

    The cgroup-aware readers consult eight cgroup path constants
    (``/sys/fs/cgroup`` plus ``/proc/self/cgroup``).
    Patching them all to an absent file by default keeps every existing
    host-figure test deterministic (no cgroup limit ⇒ the host read governs) and
    guarantees no test touches the real ``/sys/fs/cgroup`` tree. A test that needs
    a limit present patches the specific path(s) it exercises, overriding this
    default for its own duration.
    """
    with ExitStack() as stack:
        for attr in _CGROUP_PATH_ATTRS:
            stack.enter_context(patch(f"maestro.gate_capacity.{attr}", _absent_file()))
        yield


@pytest.fixture(autouse=True)
def _neutralize_gate_lease() -> Iterator[None]:
    """Opt this module *out* of conftest's gate-lease neutralizer.

    ``tests/conftest.py`` patches ``measure_host_capacity`` / ``acquire_slot`` to
    host-free stand-ins so every *consumer* test keeps off the real host now that
    ``gate_lease_enabled`` defaults on. This module is the one that tests those
    very primitives (and ``lease_for_gate_run``) against their real bodies, so it
    shadows that fixture by name with a no-op: the real functions run, their host
    reads mocked by each test's own patches plus the ``_no_cgroup_limits`` autouse
    fixture above.
    """
    yield


# ---------------------------------------------------------------------------
# plan_lease — the pure capacity policy
# ---------------------------------------------------------------------------


def test_plan_lease_single_tenant_grants_full_core_width() -> None:
    """With no other run in flight and ample memory, the grant is the full core width."""
    lease = gc.plan_lease(gc.HostCapacity(cores=4, available_mib=8000), in_flight=0)
    assert lease.workers == 4
    assert lease.slots == 4


def test_uncontended_width_matches_the_admission_seam_under_cgroup_reserve() -> None:
    """The oracle's width is the seam's ``in_flight=0`` grant under the same reserve.

    On a cgroup-bounded host the reserve flag changes the grant, so a width
    derived without it would judge ``gate_runs.jsonl`` rows no real run records.
    """
    capacity = gc.HostCapacity(cores=4, available_mib=1400, bounded_by="cgroup")
    reserved = gc.uncontended_width(capacity, cgroup_reserve=True)
    unreserved = gc.uncontended_width(capacity, cgroup_reserve=False)

    assert reserved == gc.plan_lease(capacity, 0, cgroup_reserve=True).workers
    assert unreserved == gc.plan_lease(capacity, 0, cgroup_reserve=False).workers
    assert reserved != unreserved


def test_plan_lease_is_monotonic_non_increasing_and_floors_at_one() -> None:
    """As in-flight runs rise the worker grant only shrinks, saturating at one."""
    capacity = gc.HostCapacity(cores=4, available_mib=3000)
    grants = [gc.plan_lease(capacity, in_flight=n).workers for n in range(8)]

    # The core-bound term is cores // (in_flight + 1); on four cores that is
    # 4, 2, 1, 1, … and it dominates the memory term here.
    assert grants == [4, 2, 1, 1, 1, 1, 1, 1]
    # Never rises as contention grows.
    assert all(grants[i] >= grants[i + 1] for i in range(len(grants) - 1))
    # Saturates at the one-worker floor — never zero, never negative.
    assert min(grants) == 1
    assert grants[-1] == 1
    # Never wider than the host's core count.
    assert max(grants) == capacity.cores


def test_plan_lease_splits_cores_across_concurrency_even_with_ample_memory() -> None:
    """The core-bound term divides by concurrency, so aggregate demand is bounded.

    On a big-memory host memory never binds, so this isolates the core split:
    the grant must still shrink as ``in_flight`` rises (each run gets an even
    share of the cores) rather than every run independently claiming full width —
    the oversubscription (``cores x cores``) the lease exists to prevent.
    """
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    # cores // (in_flight + 1): 8, 4, 2, 2, 1 for in_flight 0, 1, 3, 3, 7.
    assert gc.plan_lease(capacity, in_flight=0).workers == 8
    assert gc.plan_lease(capacity, in_flight=1).workers == 4
    assert gc.plan_lease(capacity, in_flight=3).workers == 2
    assert gc.plan_lease(capacity, in_flight=7).workers == 1
    # The aggregate of k evenly-sharing runs never exceeds the core count.
    for k in range(1, 9):
        per_run = gc.plan_lease(capacity, in_flight=k - 1).workers
        assert per_run * k <= capacity.cores


def test_plan_lease_negative_in_flight_does_not_divide_by_zero() -> None:
    """A negative in_flight floors the concurrency divisor at one, never 0/negative."""
    capacity = gc.HostCapacity(cores=4, available_mib=8000)
    # in_flight + 1 == 0 would divide by zero without the max(1, …) floor.
    lease = gc.plan_lease(capacity, in_flight=-1)
    assert lease.workers == 4
    # in_flight + 1 == -1 would flip the sign of the grant without the floor.
    assert gc.plan_lease(capacity, in_flight=-2).workers == 4


def test_plan_lease_is_memory_bound_when_memory_is_the_tighter_limit() -> None:
    """Eight cores but memory for only five workers → memory bounds the grant."""
    lease = gc.plan_lease(gc.HostCapacity(cores=8, available_mib=3000), in_flight=0)
    # (3000 - 1100) // 350 == 5, which is below the 8 cores available.
    assert lease.workers == 5
    assert lease.slots == 8


def test_plan_lease_is_core_bound_when_cores_are_the_tighter_limit() -> None:
    """Ample memory but only two cores → the core count bounds the grant."""
    lease = gc.plan_lease(gc.HostCapacity(cores=2, available_mib=8000), in_flight=0)
    # (8000 - 1100) // 350 == 19, clamped down to the 2 cores available.
    assert lease.workers == 2
    assert lease.slots == 2


@pytest.mark.parametrize("cores", [0, -4])
def test_plan_lease_clamps_to_floor_on_absurd_cores(cores: int) -> None:
    """Zero/negative cores clamp to the floor rather than dividing against them."""
    lease = gc.plan_lease(gc.HostCapacity(cores=cores, available_mib=8000), in_flight=0)
    assert lease == gc.GateLease(workers=1, slots=1)


@pytest.mark.parametrize("available_mib", [0, -500])
def test_plan_lease_clamps_to_floor_on_absurd_memory(available_mib: int) -> None:
    """Zero/negative memory clamps to the floor rather than dividing it out."""
    lease = gc.plan_lease(gc.HostCapacity(cores=4, available_mib=available_mib), in_flight=0)
    assert lease == gc.GateLease(workers=1, slots=1)


# ---- the bound-aware system reserve (gate_lease_cgroup_reserve_enabled) -----
#
# SYSTEM_RESERVE_MIB (1100 MiB) holds back memory for the OS and the non-agent
# baseline — processes that live *outside* a loop cgroup, and whose residency a
# cgroup's `memory.max - memory.current` figure has already netted out. Applied
# to a cgroup-bounded reading it is a double-count that eats most of a small
# container's headroom before a single worker is budgeted.


@pytest.mark.parametrize("in_flight", [0, 1, 2, 3])
def test_plan_lease_flag_off_is_identical_on_either_bound(in_flight: int) -> None:
    """At the default the reserve applies to both bounds — the pre-flag behaviour."""
    host = gc.HostCapacity(cores=2, available_mib=1400, bounded_by="host")
    cgroup = gc.HostCapacity(cores=2, available_mib=1400, bounded_by="cgroup")

    assert gc.plan_lease(cgroup, in_flight) == gc.plan_lease(host, in_flight)
    assert gc.plan_lease(cgroup, in_flight) == gc.plan_lease(
        cgroup, in_flight, cgroup_reserve=False
    )


def test_plan_lease_cgroup_reserve_frees_a_second_worker_in_a_container() -> None:
    """A 2-core/1400 MiB cgroup grants 2 workers flag-on, 1 flag-off.

    Flag-off the memory term is (1400 - 1100) // 350 == 0, floored to one worker
    — the serial suite. Flag-on it is 1400 // 350 == 4, so the core split (2)
    binds instead and the container gets the width it can actually run.
    """
    capacity = gc.HostCapacity(cores=2, available_mib=1400, bounded_by="cgroup")

    assert gc.plan_lease(capacity, in_flight=0).workers == 1
    assert gc.plan_lease(capacity, in_flight=0, cgroup_reserve=True).workers == 2


def test_plan_lease_cgroup_reserve_keeps_the_in_flight_session_term() -> None:
    """A peer's session baseline is still subtracted — only the host reserve goes.

    At in_flight=1 the core split (2 // 2 == 1) binds either way, and the memory
    term flag-on is (1400 - 300) // 350 == 3, proving the peer's 300 MiB is still
    charged rather than the whole subtraction being dropped.
    """
    capacity = gc.HostCapacity(cores=2, available_mib=1400, bounded_by="cgroup")

    assert gc.plan_lease(capacity, in_flight=1).workers == 1
    assert gc.plan_lease(capacity, in_flight=1, cgroup_reserve=True).workers == 1
    # Widen the cores so the memory term, not the split, is what is observed.
    wide = gc.HostCapacity(cores=8, available_mib=1400, bounded_by="cgroup")
    assert gc.plan_lease(wide, in_flight=1, cgroup_reserve=True).workers == 3
    assert gc.plan_lease(wide, in_flight=3, cgroup_reserve=True).workers == 1


@pytest.mark.parametrize("in_flight", [0, 1, 2, 3])
def test_plan_lease_flag_on_leaves_a_host_bounded_grant_alone(in_flight: int) -> None:
    """Host-bounded capacity keeps the reserve even with the flag on."""
    capacity = gc.HostCapacity(cores=4, available_mib=5600, bounded_by="host")

    assert gc.plan_lease(capacity, in_flight, cgroup_reserve=True) == gc.plan_lease(
        capacity, in_flight
    )


@pytest.mark.parametrize("available_mib", [0, -500])
def test_plan_lease_degenerate_floor_is_unchanged_by_the_flag(
    available_mib: int,
) -> None:
    """The degenerate-capacity floor precedes the reserve, so the flag cannot reach it."""
    capacity = gc.HostCapacity(
        cores=4, available_mib=available_mib, bounded_by="cgroup"
    )
    assert gc.plan_lease(capacity, in_flight=0, cgroup_reserve=True) == gc.GateLease(
        workers=1, slots=1
    )


# ---------------------------------------------------------------------------
# Frozen value objects
# ---------------------------------------------------------------------------


def test_host_capacity_is_frozen() -> None:
    """A measured capacity snapshot cannot drift — assigning a field raises."""
    capacity = gc.HostCapacity(cores=4, available_mib=8000)
    with pytest.raises(dataclasses.FrozenInstanceError):
        capacity.cores = 8  # type: ignore[misc]


def test_gate_lease_is_frozen() -> None:
    """A planned grant cannot drift between planning and use — assignment raises."""
    lease = gc.GateLease(workers=2, slots=4)
    with pytest.raises(dataclasses.FrozenInstanceError):
        lease.workers = 3  # type: ignore[misc]


# ---------------------------------------------------------------------------
# acquire_slot — the confined counting-semaphore seam
# ---------------------------------------------------------------------------


def test_acquire_slot_holds_first_free_slot() -> None:
    """A free slot is held with LOCK_EX|LOCK_NB and released with LOCK_UN on exit."""
    mock_flock = MagicMock()
    mock_open = MagicMock(return_value=42)
    mock_close = MagicMock()

    with (
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", mock_open),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch("maestro.gate_capacity.fcntl.flock", mock_flock),
        patch("maestro.gate_capacity.time.monotonic", return_value=0.0),
        patch("maestro.gate_capacity.time.sleep"),
        # An unpinned process keeps the historical slot names (the gate runner may
        # itself be pinned, so the scope is fixed here rather than read live).
        patch("maestro.gate_capacity._slot_scope", return_value=""),
        gc.acquire_slot(LOCK_DIR, slots=2) as admission,
    ):
        assert admission == "slot"
        # The first slot is opened and non-blocking-locked; a held slot 0
        # means slot 1 is never probed.
        mock_open.assert_called_once_with(
            str(LOCK_DIR / "gate-slot-0.lock"), os.O_CREAT | os.O_RDWR,
        )
        assert mock_flock.call_args_list == [call(42, _NB)]

    # After the block exits: exact acquire-then-release sequence on the same fd,
    # closed once. The mocks are our own objects, so their calls persist here.
    assert mock_flock.call_args_list == [call(42, _NB), call(42, fcntl.LOCK_UN)]
    mock_close.assert_called_once_with(42)


def test_acquire_slot_skips_a_busy_slot_and_holds_the_next() -> None:
    """A busy slot (BlockingIOError) is closed and the next slot is tried."""
    mock_open = MagicMock(side_effect=[10, 11])
    mock_close = MagicMock()

    def flock_side_effect(fd: int, op: int) -> None:
        if fd == 10:
            raise BlockingIOError  # slot 0 is held by another run

    mock_flock = MagicMock(side_effect=flock_side_effect)

    with (
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", mock_open),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch("maestro.gate_capacity.fcntl.flock", mock_flock),
        patch("maestro.gate_capacity.time.monotonic", return_value=0.0),
        patch("maestro.gate_capacity.time.sleep"),
        gc.acquire_slot(LOCK_DIR, slots=2) as admission,
    ):
        assert admission == "slot"

    # Slot 0's probe fd is closed after the busy signal; slot 1 is acquired,
    # released, and closed on exit.
    assert mock_close.call_args_list == [call(10), call(11)]
    assert mock_flock.call_args_list == [
        call(10, _NB),  # slot 0 probe -> BlockingIOError
        call(11, _NB),  # slot 1 probe -> held
        call(11, fcntl.LOCK_UN),  # released on exit
    ]


def test_acquire_slot_closes_fd_and_reraises_on_unexpected_flock_error() -> None:
    """An unexpected flock error (not the BlockingIOError busy signal) still
    closes the just-opened probe fd instead of leaking it."""
    mock_open = MagicMock(return_value=9)
    mock_close = MagicMock()
    mock_flock = MagicMock(side_effect=OSError("ENOLCK"))

    with (
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", mock_open),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch("maestro.gate_capacity.fcntl.flock", mock_flock),
        patch("maestro.gate_capacity.time.monotonic", return_value=0.0),
        patch("maestro.gate_capacity.time.sleep"),
        pytest.raises(OSError, match="ENOLCK"),
        gc.acquire_slot(LOCK_DIR, slots=1),
    ):
        pass  # unreachable: the flock error propagates before the manager yields

    # The probe fd is closed even though flock raised something other than
    # the documented "slot busy" signal.
    mock_close.assert_called_once_with(9)


def test_acquire_slot_fails_open_when_all_slots_stay_busy() -> None:
    """Every slot busy for the whole bounded wait -> yield 'fallback', hold nothing."""
    mock_open = MagicMock(return_value=7)
    mock_close = MagicMock()
    mock_flock = MagicMock(side_effect=BlockingIOError)
    mock_sleep = MagicMock()
    # deadline calc (0.0), first check (10.0 < 30 -> sleep), second check (40.0 -> give up).
    mock_monotonic = MagicMock(side_effect=[0.0, 10.0, 40.0])

    with (
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", mock_open),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch("maestro.gate_capacity.fcntl.flock", mock_flock),
        patch("maestro.gate_capacity.time.monotonic", mock_monotonic),
        patch("maestro.gate_capacity.time.sleep", mock_sleep),
        gc.acquire_slot(LOCK_DIR, slots=2) as admission,
    ):
        assert admission == "fallback"

    # Waited once between the two full-slot-set probes, then failed open.
    mock_sleep.assert_called_once_with(gc._ACQUIRE_PROBE_INTERVAL_SECONDS)
    # Two probe rounds over two slots = four opens, each probe fd closed after
    # its busy signal; no slot is held past the fallback (no LOCK_UN).
    assert mock_open.call_count == 4
    assert mock_close.call_count == 4
    released_ops = [op for _fd, op in (c.args for c in mock_flock.call_args_list)]
    assert fcntl.LOCK_UN not in released_ops


def test_acquire_slot_releases_on_exception() -> None:
    """A raise inside the body still releases the slot and closes the fd."""
    mock_flock = MagicMock()
    mock_close = MagicMock()

    with (
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", return_value=5),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch("maestro.gate_capacity.fcntl.flock", mock_flock),
        patch("maestro.gate_capacity.time.monotonic", return_value=0.0),
        patch("maestro.gate_capacity.time.sleep"),
        pytest.raises(RuntimeError, match="boom"),
        gc.acquire_slot(LOCK_DIR, slots=1) as admission,
    ):
        assert admission == "slot"
        raise RuntimeError("boom")

    # Released despite the raise, and the fd is not leaked.
    mock_flock.assert_any_call(5, fcntl.LOCK_UN)
    mock_close.assert_called_once_with(5)


def test_acquire_slot_creates_lock_dir() -> None:
    """The lock directory is created (exist_ok) so a caller need not pre-make it."""
    mock_makedirs = MagicMock()

    with (
        patch("maestro.gate_capacity.os.makedirs", mock_makedirs),
        patch("maestro.gate_capacity.os.open", return_value=3),
        patch("maestro.gate_capacity.os.close"),
        patch("maestro.gate_capacity.fcntl.flock"),
        patch("maestro.gate_capacity.time.monotonic", return_value=0.0),
        patch("maestro.gate_capacity.time.sleep"),
        gc.acquire_slot(LOCK_DIR, slots=1) as admission,
    ):
        assert admission == "slot"

    mock_makedirs.assert_called_once_with(str(LOCK_DIR), exist_ok=True)


@pytest.mark.parametrize("slots", [0, -1])
def test_acquire_slot_rejects_non_positive_slots(slots: int) -> None:
    """A non-positive slots budget raises loudly before any lock file or wait."""
    mock_makedirs = MagicMock()
    mock_open = MagicMock()

    # pytest.raises is entered before acquire_slot, so it catches the ValueError
    # raised by acquire_slot's __enter__ before the manager ever yields.
    with (
        patch("maestro.gate_capacity.os.makedirs", mock_makedirs),
        patch("maestro.gate_capacity.os.open", mock_open),
        pytest.raises(ValueError, match="at least one slot"),
        gc.acquire_slot(LOCK_DIR, slots=slots),
    ):
        pass  # unreachable: the manager raises on entry, never yields

    # Fails loud before touching the filesystem — no dir made, no slot probed.
    mock_makedirs.assert_not_called()
    mock_open.assert_not_called()


# ---------------------------------------------------------------------------
# measure_host_capacity — the live-host snapshot reader
# ---------------------------------------------------------------------------


def test_measure_host_capacity_reads_cores_and_memory() -> None:
    """The affinity set size and a MemAvailable KiB line fold into a HostCapacity."""
    meminfo = MagicMock()
    meminfo.read_text.return_value = (
        "MemTotal:       32000000 kB\n"
        "MemAvailable:   16384000 kB\n"
    )
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value=set(range(8))),
        patch("maestro.gate_capacity._MEMINFO_PATH", meminfo),
    ):
        capacity = gc.measure_host_capacity()

    assert capacity.cores == 8
    # 16384000 KiB / 1024 = 16000 MiB.
    assert capacity.available_mib == 16000


def test_measure_host_capacity_uses_affinity_not_machine_total() -> None:
    """Absent a cgroup CFS quota, a cpuset-pinned process sizes cores to its
    affinity set, not the host total.

    Affinity is the ceiling only when no cgroup quota is present (the autouse
    fixture leaves the cgroup absent); a quota *tighter* than affinity binds
    below it — see ``test_available_cores_cgroup_v2_quota_binds_below_affinity``
    and its v1 twin, which is the container case #3760 was about.
    """
    meminfo = MagicMock()
    meminfo.read_text.return_value = "MemAvailable:   8192000 kB\n"
    with (
        # Two CPUs in the affinity set on an eight-logical-CPU host: the reader
        # must take the affinity count (what pytest-xdist can actually use), not
        # os.cpu_count()'s machine-wide 8.
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1}),
        patch("maestro.gate_capacity.os.cpu_count", return_value=8),
        patch("maestro.gate_capacity._MEMINFO_PATH", meminfo),
    ):
        capacity = gc.measure_host_capacity()

    assert capacity.cores == 2


def test_measure_host_capacity_falls_back_to_cpu_count_off_linux() -> None:
    """Where os.sched_getaffinity is absent (macOS/Windows) cpu_count is used."""
    meminfo = MagicMock()
    meminfo.read_text.return_value = "MemAvailable:   2048000 kB\n"
    with (
        patch(
            "maestro.gate_capacity.os.sched_getaffinity",
            side_effect=AttributeError("no sched_getaffinity"),
        ),
        patch("maestro.gate_capacity.os.cpu_count", return_value=4),
        patch("maestro.gate_capacity._MEMINFO_PATH", meminfo),
    ):
        capacity = gc.measure_host_capacity()

    assert capacity.cores == 4


def test_measure_host_capacity_floors_cores_when_topology_unknown() -> None:
    """Neither affinity nor cpu_count available floors cores at 1 rather than crashing."""
    meminfo = MagicMock()
    meminfo.read_text.return_value = "MemAvailable:   2048000 kB\n"
    with (
        patch(
            "maestro.gate_capacity.os.sched_getaffinity",
            side_effect=AttributeError("no sched_getaffinity"),
        ),
        patch("maestro.gate_capacity.os.cpu_count", return_value=None),
        patch("maestro.gate_capacity._MEMINFO_PATH", meminfo),
    ):
        capacity = gc.measure_host_capacity()

    assert capacity.cores == 1


def test_measure_host_capacity_unreadable_meminfo_falls_back_to_zero() -> None:
    """An unreadable /proc/meminfo yields available_mib=0 so plan_lease's floor wins."""
    meminfo = MagicMock()
    meminfo.read_text.side_effect = OSError("no /proc here")
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        patch("maestro.gate_capacity._MEMINFO_PATH", meminfo),
    ):
        capacity = gc.measure_host_capacity()

    assert capacity.available_mib == 0
    # Degenerate memory clamps plan_lease to the one-worker, one-slot floor.
    assert gc.plan_lease(capacity, in_flight=0) == gc.GateLease(workers=1, slots=1)


def test_measure_host_capacity_missing_memavailable_field_falls_back_to_zero() -> None:
    """A /proc/meminfo without a MemAvailable line yields available_mib=0."""
    meminfo = MagicMock()
    meminfo.read_text.return_value = "MemTotal: 32000000 kB\nMemFree: 100000 kB\n"
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        patch("maestro.gate_capacity._MEMINFO_PATH", meminfo),
    ):
        capacity = gc.measure_host_capacity()

    assert capacity.available_mib == 0


def test_measure_host_capacity_nonnumeric_memavailable_falls_back_to_zero() -> None:
    """A MemAvailable line whose value is not digits yields available_mib=0."""
    meminfo = MagicMock()
    meminfo.read_text.return_value = "MemAvailable:   notanumber kB\n"
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        patch("maestro.gate_capacity._MEMINFO_PATH", meminfo),
    ):
        capacity = gc.measure_host_capacity()

    assert capacity.available_mib == 0


# ---------------------------------------------------------------------------
# The cgroup-aware readers — the container-sizing fix (#3760)
# ---------------------------------------------------------------------------
#
# The host free-memory / affinity figures see *through* a container to the whole
# host, so inside the loop's 2 GiB / 2-CPU cgroup they overstate what a gate run
# may use and plan_lease grants a host-sized ``-n 4`` that OOM-thrashes. These
# tests prove the readers now take the tighter of the host figure and the cgroup
# limit, across cgroup v2 (unified) and v1 (legacy) layouts.


@contextmanager
def _cgroup(**bodies: str) -> Iterator[None]:
    """Patch each named ``gate_capacity`` cgroup path constant to a present file
    whose ``read_text`` returns the given body; paths not named stay absent (the
    autouse ``_no_cgroup_limits`` default), so a test declares only the cgroup
    files it exercises.
    """
    with ExitStack() as stack:
        for attr, body in bodies.items():
            stack.enter_context(
                patch(f"maestro.gate_capacity.{attr}", _mock_file(body))
            )
        yield


# ---- _read_available_mib — memory is the tighter of host and cgroup ---------


def test_read_available_mib_cgroup_v2_headroom_binds_below_host() -> None:
    """A v2 memory.max/current headroom below host MemAvailable is the binding limit."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   5734400 kB\n"),  # 5600 MiB host-free
        ),
        _cgroup(
            _CGROUP_V2_MEMORY_MAX_PATH="2147483648\n",  # 2 GiB cap
            _CGROUP_V2_MEMORY_CURRENT_PATH="536870912\n",  # 512 MiB in use
        ),
    ):
        # (2147483648 - 536870912) // (1024*1024) == 1536 MiB, below host's 5600.
        assert gc._read_available_mib() == 1536


def test_read_available_mib_host_binds_below_cgroup() -> None:
    """When the host figure is the smaller candidate, ``min`` picks the host."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   1048576 kB\n"),  # 1024 MiB host-free
        ),
        _cgroup(
            _CGROUP_V2_MEMORY_MAX_PATH="8589934592\n",  # 8 GiB cap
            _CGROUP_V2_MEMORY_CURRENT_PATH="0\n",
        ),
    ):
        assert gc._read_available_mib() == 1024


def test_read_available_mib_v2_unlimited_falls_to_host() -> None:
    """A v2 memory.max of 'max' (no cap) leaves the host read governing."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   3072000 kB\n"),  # 3000 MiB
        ),
        _cgroup(_CGROUP_V2_MEMORY_MAX_PATH="max\n"),
    ):
        assert gc._read_available_mib() == 3000


def test_read_available_mib_v2_current_unreadable_falls_to_host() -> None:
    """A v2 cap present but current-usage read unavailable ⇒ cgroup 0, host governs."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   3072000 kB\n"),
        ),
        # memory.max present, memory.current stays absent (autouse default).
        _cgroup(_CGROUP_V2_MEMORY_MAX_PATH="2147483648\n"),
    ):
        assert gc._read_available_mib() == 3000


def test_read_available_mib_v2_exhausted_limit_binds_at_zero() -> None:
    """A v2 cap its usage has reached binds at 0 even with host memory free.

    The exhausted-container case: discarding the measured zero in favour of the
    host figure is what grants a multi-worker width into an OOM kill.
    """
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   5734400 kB\n"),  # 5600 MiB host-free
        ),
        _cgroup(
            _CGROUP_V2_MEMORY_MAX_PATH="2147483648\n",  # 2 GiB cap
            _CGROUP_V2_MEMORY_CURRENT_PATH="2147483648\n",  # fully charged
        ),
    ):
        assert gc._read_available_mib() == 0


def test_read_available_mib_v2_usage_above_limit_binds_at_zero() -> None:
    """Usage charged *past* the v2 cap still binds at 0, never a negative."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   5734400 kB\n"),
        ),
        _cgroup(
            _CGROUP_V2_MEMORY_MAX_PATH="2147483648\n",
            _CGROUP_V2_MEMORY_CURRENT_PATH="2415919104\n",  # 2.25 GiB charged
        ),
    ):
        assert gc._read_available_mib() == 0


def test_read_available_mib_v1_exhausted_limit_binds_at_zero() -> None:
    """A v1 usage_in_bytes at or above limit_in_bytes binds at 0 too."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   5734400 kB\n"),
        ),
        _cgroup(
            _CGROUP_V1_MEMORY_LIMIT_PATH="2147483648\n",
            _CGROUP_V1_MEMORY_USAGE_PATH="2147483648\n",
        ),
    ):
        assert gc._read_available_mib() == 0


def test_read_memory_bound_reports_cgroup_for_an_exhausted_limit() -> None:
    """An exhausted cgroup cap reports the *cgroup* bound, not a host fallback."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   5734400 kB\n"),
        ),
        _cgroup(
            _CGROUP_V2_MEMORY_MAX_PATH="2147483648\n",
            _CGROUP_V2_MEMORY_CURRENT_PATH="2147483648\n",
        ),
    ):
        assert gc._read_memory_bound() == (0, "cgroup")


def test_plan_lease_floors_on_an_exhausted_cgroup() -> None:
    """The measured zero reaches plan_lease's floor: one worker, one slot."""
    capacity = gc.HostCapacity(cores=8, available_mib=0, bounded_by="cgroup")
    assert gc.plan_lease(capacity, in_flight=0) == gc.GateLease(workers=1, slots=1)


def test_cgroup_memory_rejects_a_negative_headroom() -> None:
    """The constructor guard holds for a caller the readers' max(0, ...) does not cover."""
    with pytest.raises(ValueError, match="non-negative"):
        gc._CgroupMemory(enforced=True, headroom_mib=-1)


def test_read_available_mib_v1_headroom_when_no_v2() -> None:
    """With no v2 hierarchy, the v1 limit/usage headroom binds."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   5734400 kB\n"),  # 5600 MiB
        ),
        # v2 memory.max absent (autouse) ⇒ the reader falls to v1.
        _cgroup(
            _CGROUP_V1_MEMORY_LIMIT_PATH="2147483648\n",  # 2 GiB
            _CGROUP_V1_MEMORY_USAGE_PATH="536870912\n",  # 512 MiB
        ),
    ):
        assert gc._read_available_mib() == 1536


def test_read_available_mib_v1_unlimited_sentinel_falls_to_host() -> None:
    """A v1 limit at/above the ~PAGE_COUNTER_MAX sentinel is unlimited ⇒ host governs."""
    sentinel = str(1 << 62)  # == _CGROUP_UNLIMITED_BYTES
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   3072000 kB\n"),
        ),
        _cgroup(_CGROUP_V1_MEMORY_LIMIT_PATH=f"{sentinel}\n"),
    ):
        assert gc._read_available_mib() == 3000


def test_read_available_mib_v1_usage_unparseable_falls_to_host() -> None:
    """A v1 limit present but usage unparseable ⇒ cgroup 0, host governs."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   3072000 kB\n"),
        ),
        _cgroup(
            _CGROUP_V1_MEMORY_LIMIT_PATH="2147483648\n",
            _CGROUP_V1_MEMORY_USAGE_PATH="garbage\n",  # non-integer ⇒ None
        ),
    ):
        assert gc._read_available_mib() == 3000


def test_read_available_mib_no_cgroup_uses_host() -> None:
    """No cgroup memory limit at all (every path absent) ⇒ the host read stands."""
    with patch(
        "maestro.gate_capacity._MEMINFO_PATH",
        _mock_file("MemAvailable:   3072000 kB\n"),
    ):
        assert gc._read_available_mib() == 3000


def test_read_available_mib_all_unknown_is_zero() -> None:
    """Neither host nor cgroup readable ⇒ 0, so plan_lease's floor takes over."""
    meminfo = MagicMock()
    meminfo.read_text.side_effect = OSError("no /proc")
    with patch("maestro.gate_capacity._MEMINFO_PATH", meminfo):
        # cgroup absent (autouse) + host OSError ⇒ no positive candidate.
        assert gc._read_available_mib() == 0


# ---- _read_memory_bound — the figure *and* which bound produced it ---------
#
# SYSTEM_RESERVE_MIB is a *host* measurement of a baseline that lives outside a
# loop cgroup, and memory.current already charges everything inside one. So
# plan_lease can only apply the reserve honestly if the capacity snapshot says
# which reading won; these tests pin that bound across every reader path, and
# pin the conservative "host" tie-break that retains the reserve.


def test_read_memory_bound_reports_cgroup_when_v2_headroom_binds() -> None:
    """A v2 headroom strictly below the host figure reports the cgroup bound."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   5734400 kB\n"),  # 5600 MiB host-free
        ),
        _cgroup(
            _CGROUP_V2_MEMORY_MAX_PATH="2147483648\n",  # 2 GiB cap
            _CGROUP_V2_MEMORY_CURRENT_PATH="536870912\n",  # 512 MiB in use
        ),
    ):
        assert gc._read_memory_bound() == (1536, "cgroup")


def test_read_memory_bound_reports_cgroup_when_v1_headroom_binds() -> None:
    """The legacy v1 layout reports the cgroup bound just as v2 does."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   5734400 kB\n"),
        ),
        _cgroup(
            _CGROUP_V1_MEMORY_LIMIT_PATH="2147483648\n",
            _CGROUP_V1_MEMORY_USAGE_PATH="536870912\n",
        ),
    ):
        assert gc._read_memory_bound() == (1536, "cgroup")


def test_read_memory_bound_reports_host_when_host_binds() -> None:
    """A host figure below an ample cgroup headroom is host-bounded."""
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   1048576 kB\n"),  # 1024 MiB
        ),
        _cgroup(
            _CGROUP_V2_MEMORY_MAX_PATH="8589934592\n",  # 8 GiB cap
            _CGROUP_V2_MEMORY_CURRENT_PATH="0\n",
        ),
    ):
        assert gc._read_memory_bound() == (1024, "host")


def test_read_memory_bound_reports_host_when_no_cgroup_limit() -> None:
    """No cgroup cap at all ⇒ the host read governs and names itself."""
    with patch(
        "maestro.gate_capacity._MEMINFO_PATH",
        _mock_file("MemAvailable:   3072000 kB\n"),
    ):
        assert gc._read_memory_bound() == (3000, "host")


def test_read_memory_bound_tie_reports_host() -> None:
    """Equal candidates report the conservative host bound, which keeps the reserve.

    The cgroup wins only when *strictly* tighter — an equal reading is not
    evidence that the reserve's baseline has been netted out.
    """
    with (
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   1572864 kB\n"),  # 1536 MiB
        ),
        _cgroup(
            _CGROUP_V2_MEMORY_MAX_PATH="2147483648\n",
            _CGROUP_V2_MEMORY_CURRENT_PATH="536870912\n",  # also 1536 MiB
        ),
    ):
        assert gc._read_memory_bound() == (1536, "host")


def test_read_memory_bound_cgroup_governs_when_host_unreadable() -> None:
    """An unreadable /proc/meminfo leaves a positive cgroup figure governing."""
    meminfo = MagicMock()
    meminfo.read_text.side_effect = OSError("no /proc")
    with (
        patch("maestro.gate_capacity._MEMINFO_PATH", meminfo),
        _cgroup(
            _CGROUP_V2_MEMORY_MAX_PATH="2147483648\n",
            _CGROUP_V2_MEMORY_CURRENT_PATH="536870912\n",
        ),
    ):
        assert gc._read_memory_bound() == (1536, "cgroup")


def test_read_memory_bound_all_unknown_is_zero_host() -> None:
    """No candidate at all ⇒ (0, "host"), the reading that retains the reserve."""
    meminfo = MagicMock()
    meminfo.read_text.side_effect = OSError("no /proc")
    with patch("maestro.gate_capacity._MEMINFO_PATH", meminfo):
        assert gc._read_memory_bound() == (0, "host")


def test_measure_host_capacity_carries_the_cgroup_bound() -> None:
    """The snapshot's available_mib and bounded_by come from one selection."""
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1}),
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   5734400 kB\n"),
        ),
        _cgroup(
            _CGROUP_V2_MEMORY_MAX_PATH="2147483648\n",
            _CGROUP_V2_MEMORY_CURRENT_PATH="536870912\n",
        ),
    ):
        capacity = gc.measure_host_capacity()

    assert capacity.available_mib == 1536
    assert capacity.bounded_by == "cgroup"


def test_host_capacity_bounded_by_defaults_to_host() -> None:
    """A keyword construction omitting the new field stays valid and conservative."""
    assert gc.HostCapacity(cores=4, available_mib=8000).bounded_by == "host"


# ---- _read_int_file — the single-integer cgroup-body parser ----------------


def test_read_int_file_parses_positive_and_negative_sentinel() -> None:
    """A plain integer and the lone ``-1`` unlimited sentinel both parse."""
    assert gc._read_int_file(_mock_file("2147483648\n")) == 2147483648
    assert gc._read_int_file(_mock_file("-1\n")) == -1


def test_read_int_file_multi_dash_body_is_none_not_a_crash() -> None:
    """A pathological multi-dash body (``--1``) returns None, never a ValueError.

    ``str.lstrip('-').isdigit()`` would accept ``--1`` yet ``int('--1')`` raises;
    stripping only a single leading ``-`` rejects it as a non-integer instead.
    """
    assert gc._read_int_file(_mock_file("--1\n")) is None


def test_read_int_file_non_integer_and_absent_are_none() -> None:
    """A non-numeric body and an absent file (OSError) both return None."""
    assert gc._read_int_file(_mock_file("max\n")) is None
    assert gc._read_int_file(_mock_file("")) is None
    assert gc._read_int_file(_absent_file()) is None


# ---- _available_cores — cores is the tighter of affinity and CFS quota ------


def test_available_cores_cgroup_v2_quota_binds_below_affinity() -> None:
    """A v2 cpu.max quota of 2 cores binds below a 4-CPU affinity set."""
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        _cgroup(_CGROUP_V2_CPU_MAX_PATH="200000 100000\n"),  # 2 cores
    ):
        assert gc._available_cores() == 2


def test_available_cores_cgroup_v2_quota_fractional_floors_to_one() -> None:
    """A sub-core v2 quota (0.5 core) floors to a single core, never zero."""
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        _cgroup(_CGROUP_V2_CPU_MAX_PATH="50000 100000\n"),  # 0.5 core -> 1
    ):
        assert gc._available_cores() == 1


def test_available_cores_cgroup_v2_unlimited_uses_affinity() -> None:
    """A v2 cpu.max of 'max <period>' (no quota) leaves affinity governing."""
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        _cgroup(_CGROUP_V2_CPU_MAX_PATH="max 100000\n"),
    ):
        assert gc._available_cores() == 4


def test_available_cores_cgroup_v2_single_field_uses_affinity() -> None:
    """A malformed single-field cpu.max is not a quota ⇒ affinity governs."""
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        _cgroup(_CGROUP_V2_CPU_MAX_PATH="200000\n"),  # no period field
    ):
        assert gc._available_cores() == 4


def test_available_cores_cgroup_v2_zero_period_uses_affinity() -> None:
    """A v2 zero period cannot divide ⇒ no quota, affinity governs."""
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        _cgroup(_CGROUP_V2_CPU_MAX_PATH="200000 0\n"),
    ):
        assert gc._available_cores() == 4


def test_available_cores_cgroup_v1_quota_binds_below_affinity() -> None:
    """With no v2 hierarchy, a v1 CFS quota of 2 cores binds below affinity 4."""
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        _cgroup(
            _CGROUP_V1_CPU_QUOTA_PATH="200000\n",
            _CGROUP_V1_CPU_PERIOD_PATH="100000\n",
        ),
    ):
        assert gc._available_cores() == 2


def test_available_cores_cgroup_v1_unlimited_quota_uses_affinity() -> None:
    """A v1 cfs_quota_us of -1 (unlimited) leaves affinity governing."""
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        _cgroup(_CGROUP_V1_CPU_QUOTA_PATH="-1\n"),
    ):
        assert gc._available_cores() == 4


def test_available_cores_cgroup_v1_period_unreadable_uses_affinity() -> None:
    """A v1 quota present but period unreadable ⇒ no quota, affinity governs."""
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        # cfs_period_us stays absent (autouse default).
        _cgroup(_CGROUP_V1_CPU_QUOTA_PATH="200000\n"),
    ):
        assert gc._available_cores() == 4


def test_available_cores_cgroup_v1_zero_period_uses_affinity() -> None:
    """A v1 zero period cannot divide ⇒ no quota, affinity governs."""
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        _cgroup(
            _CGROUP_V1_CPU_QUOTA_PATH="200000\n",
            _CGROUP_V1_CPU_PERIOD_PATH="0\n",
        ),
    ):
        assert gc._available_cores() == 4


def test_available_cores_no_cgroup_quota_uses_affinity() -> None:
    """No cgroup CPU quota at all ⇒ the affinity count stands unchanged."""
    with patch(
        "maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}
    ):
        assert gc._available_cores() == 4


def test_available_cores_affinity_below_quota_binds_affinity() -> None:
    """When affinity is the smaller of the two, ``min`` still picks affinity."""
    with (
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1}),
        _cgroup(_CGROUP_V2_CPU_MAX_PATH="800000 100000\n"),  # 8-core quota
    ):
        assert gc._available_cores() == 2


def test_host_cores_ignores_cgroup_quota() -> None:
    """``host_cores`` is the affinity count, never clamped by the CFS quota (#4080)."""
    with (
        patch("maestro.gate_capacity._read_affinity_cores", return_value=4),
        patch("maestro.gate_capacity._read_cgroup_quota_cores", return_value=2),
    ):
        assert gc.host_cores() == 4
        assert gc._available_cores() == 2


def test_host_cores_floors_at_one() -> None:
    """A zero affinity count still yields one core, so division stays safe."""
    with patch("maestro.gate_capacity._read_affinity_cores", return_value=0):
        assert gc.host_cores() == 1


# ---- measure_host_capacity + plan_lease under container limits (#3760) ------


def test_measure_host_capacity_container_limits_grant_is_container_sized() -> None:
    """The #3760 regression, end to end.

    Under a 2 GiB / 2-CPU cgroup on a 4-core host with ~5.6 GiB free,
    ``measure_host_capacity`` reports the *container* figures and ``plan_lease``
    grants a container-sized worker count — never the host-sized ``-n 4`` that
    OOM-thrashed the loop.
    """
    with (
        # 4-core host, ~5.6 GiB free — the affinity/host figures the old readers took.
        patch("maestro.gate_capacity.os.sched_getaffinity", return_value={0, 1, 2, 3}),
        patch("maestro.gate_capacity.os.cpu_count", return_value=4),
        patch(
            "maestro.gate_capacity._MEMINFO_PATH",
            _mock_file("MemAvailable:   5734400 kB\n"),  # 5600 MiB
        ),
        _cgroup(
            _CGROUP_V2_MEMORY_MAX_PATH="2147483648\n",  # 2 GiB
            _CGROUP_V2_MEMORY_CURRENT_PATH="536870912\n",  # 512 MiB used -> 1536 free
            _CGROUP_V2_CPU_MAX_PATH="200000 100000\n",  # 2 cores
        ),
    ):
        capacity = gc.measure_host_capacity()

    # The snapshot is the container's, not the host's 4 cores / 5600 MiB.
    assert capacity.cores == 2
    assert capacity.available_mib == 1536

    container_grant = gc.plan_lease(capacity, in_flight=0)
    # The host-sized snapshot the old readers produced would have granted -n 4.
    host_grant = gc.plan_lease(gc.HostCapacity(cores=4, available_mib=5600), in_flight=0)
    assert host_grant.workers == 4
    # Container-sized: memory-bound to a serial floor under 2 GiB — never host -n 4.
    assert container_grant.workers < host_grant.workers
    assert container_grant.workers == 1
    assert container_grant.slots == 2
    assert gc.worker_args(container_grant) == ()


# ---------------------------------------------------------------------------
# count_in_flight — the live in-flight-run reader
# ---------------------------------------------------------------------------


def test_count_in_flight_all_free_returns_zero() -> None:
    """Every slot lockable => zero in flight, and each probe lock is released."""
    mock_flock = MagicMock()
    mock_open = MagicMock(side_effect=[10, 11, 12])
    mock_close = MagicMock()

    with (
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", mock_open),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch("maestro.gate_capacity.fcntl.flock", mock_flock),
    ):
        held = gc.count_in_flight(LOCK_DIR, slots=3)

    assert held == 0
    # Each free slot is shared-locked then released, and every probe fd closed.
    assert mock_flock.call_args_list == [
        call(10, _SH), call(10, fcntl.LOCK_UN),
        call(11, _SH), call(11, fcntl.LOCK_UN),
        call(12, _SH), call(12, fcntl.LOCK_UN),
    ]
    assert mock_close.call_args_list == [call(10), call(11), call(12)]


def test_count_in_flight_counts_held_slots() -> None:
    """A BlockingIOError probe means that slot is held: counted, fd closed, not released."""
    mock_open = MagicMock(side_effect=[10, 11, 12])
    mock_close = MagicMock()

    def flock_side_effect(fd: int, op: int) -> None:
        if fd in (10, 12):
            raise BlockingIOError  # slots 0 and 2 are held by other runs

    mock_flock = MagicMock(side_effect=flock_side_effect)

    with (
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", mock_open),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch("maestro.gate_capacity.fcntl.flock", mock_flock),
    ):
        held = gc.count_in_flight(LOCK_DIR, slots=3)

    assert held == 2
    # Held slots probed once (no LOCK_UN); the free middle slot released.
    assert mock_flock.call_args_list == [
        call(10, _SH),  # held
        call(11, _SH), call(11, fcntl.LOCK_UN),  # free -> released
        call(12, _SH),  # held
    ]
    # Every probe fd is closed regardless of held/free.
    assert mock_close.call_args_list == [call(10), call(11), call(12)]


def test_count_in_flight_all_held_returns_slot_count() -> None:
    """Every slot held => count equals the slot budget, and nothing is released."""
    mock_open = MagicMock(side_effect=[20, 21])
    mock_close = MagicMock()
    mock_flock = MagicMock(side_effect=BlockingIOError)

    with (
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", mock_open),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch("maestro.gate_capacity.fcntl.flock", mock_flock),
    ):
        held = gc.count_in_flight(LOCK_DIR, slots=2)

    assert held == 2
    released_ops = [op for _fd, op in (c.args for c in mock_flock.call_args_list)]
    assert fcntl.LOCK_UN not in released_ops
    assert mock_close.call_args_list == [call(20), call(21)]


def test_count_in_flight_creates_lock_dir() -> None:
    """The lock dir is created (exist_ok) so counting may precede any acquire."""
    mock_makedirs = MagicMock()

    with (
        patch("maestro.gate_capacity.os.makedirs", mock_makedirs),
        patch("maestro.gate_capacity.os.open", return_value=1),
        patch("maestro.gate_capacity.os.close"),
        patch("maestro.gate_capacity.fcntl.flock"),
    ):
        gc.count_in_flight(LOCK_DIR, slots=1)

    mock_makedirs.assert_called_once_with(str(LOCK_DIR), exist_ok=True)


def test_count_in_flight_closes_fd_and_reraises_on_unexpected_flock_error() -> None:
    """An unexpected flock error closes the probe fd instead of leaking it, and propagates."""
    mock_close = MagicMock()

    with (
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", return_value=9),
        patch("maestro.gate_capacity.os.close", mock_close),
        patch(
            "maestro.gate_capacity.fcntl.flock",
            MagicMock(side_effect=OSError("ENOLCK")),
        ),
        pytest.raises(OSError, match="ENOLCK"),
    ):
        gc.count_in_flight(LOCK_DIR, slots=1)

    # The probe fd is closed even though flock raised something other than the
    # documented "slot busy" signal — no descriptor leak.
    mock_close.assert_called_once_with(9)


# ---------------------------------------------------------------------------
# worker_args — the pure pytest-xdist argv atom
# ---------------------------------------------------------------------------


def test_worker_args_none_lease_is_empty() -> None:
    """A None lease (flag off) emits no worker atoms — the argv stays byte-identical."""
    assert gc.worker_args(None) == ()


def test_worker_args_one_worker_grant_is_empty() -> None:
    """A one-worker floor grant runs serially — no `-n 1` that buys no parallelism."""
    assert gc.worker_args(gc.GateLease(workers=1, slots=4)) == ()


def test_worker_args_multi_worker_grant_pairs_n_with_dist_loadfile() -> None:
    """A multi-worker grant spells the width as `-n <count>` paired with `--dist loadfile`."""
    assert gc.worker_args(gc.GateLease(workers=4, slots=8)) == (
        "-n", "4", "--dist", "loadfile",
    )


def test_worker_args_never_emits_n_without_dist_loadfile() -> None:
    """Across every grant width, `-n` is never emitted without its `--dist loadfile`.

    Keeping the scheduler pin welded to the width in one function is the point: a
    reader that splices these atoms can never get `-n` without `--dist loadfile`.
    """
    for workers in range(1, 9):
        args = gc.worker_args(gc.GateLease(workers=workers, slots=8))
        if "-n" in args:
            assert "--dist" in args
            assert "loadfile" in args
            # Paired adjacently in the fixed order the readers splice in.
            assert args == ("-n", str(workers), "--dist", "loadfile")


# ---------------------------------------------------------------------------
# lease_for_gate_run — the shared admission seam
# ---------------------------------------------------------------------------


def test_lease_for_gate_run_none_slot_dir_reads_no_host_takes_no_slot() -> None:
    """A None slot_dir (flag off) yields None, reading no host and taking no slot."""
    mock_measure = MagicMock()
    mock_acquire = MagicMock()
    mock_count = MagicMock()
    with (
        patch("maestro.gate_capacity.measure_host_capacity", mock_measure),
        patch("maestro.gate_capacity.acquire_slot", mock_acquire),
        patch("maestro.gate_capacity.count_in_flight", mock_count),
        gc.lease_for_gate_run(None) as run,
    ):
        assert run.lease is None

    mock_measure.assert_not_called()
    mock_acquire.assert_not_called()
    mock_count.assert_not_called()


def test_lease_for_gate_run_slot_admission_replans_against_in_flight_minus_one() -> None:
    """On a 'slot' admission the grant is re-planned against ``count_in_flight - 1``.

    The caller's own slot is held, so ``count_in_flight`` (which counts it) is
    decremented to the *other* in-flight runs ``plan_lease`` expects.
    """
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    events: list[tuple[str, int]] = []

    @contextmanager
    def fake_acquire(lock_dir: Path, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        events.append(("acquire", slots))
        try:
            yield "slot"
        finally:
            events.append(("release", slots))

    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", return_value=3) as mock_count,
        gc.lease_for_gate_run(LOCK_DIR) as run,
    ):
        # slots is in_flight-independent: plan_lease(cap, 0).slots == cores == 8.
        # count_in_flight 3 -> in_flight 2 -> plan_lease(cap, 2): 8 // 3 == 2 workers.
        assert run.lease == gc.plan_lease(capacity, in_flight=2)
        assert run.lease is not None
        assert run.lease.workers == 2

    # The slot budget planned before the hold is the cores; acquired then released;
    # the count is read once against that same in_flight-independent budget.
    assert events == [("acquire", 8), ("release", 8)]
    mock_count.assert_called_once_with(LOCK_DIR, 8)


def test_lease_for_gate_run_fallback_admission_yields_floor_without_reading_count() -> None:
    """On a 'fallback' admission the one-worker floor is yielded, holding no slot."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)

    @contextmanager
    def fake_acquire(lock_dir: Path, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        yield "fallback"

    mock_count = MagicMock()
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", mock_count),
        gc.lease_for_gate_run(LOCK_DIR) as run,
    ):
        assert run.lease == gc.GateLease(workers=1, slots=8)

    # Fail-open holds no slot, so it never reads the in-flight count.
    mock_count.assert_not_called()


def test_lease_for_gate_run_releases_slot_on_exception() -> None:
    """A raise inside the body still releases the slot (acquire_slot's own finally)."""
    capacity = gc.HostCapacity(cores=4, available_mib=8000)
    events: list[str] = []

    @contextmanager
    def fake_acquire(lock_dir: Path, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        events.append("acquire")
        try:
            yield "slot"
        finally:
            events.append("release")

    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        pytest.raises(RuntimeError, match="boom"),
        gc.lease_for_gate_run(LOCK_DIR) as run,
    ):
        assert run.lease is not None
        raise RuntimeError("boom")

    assert events == ["acquire", "release"]


# ---------------------------------------------------------------------------
# Gate-run telemetry — GateRunRecord, observation_outcome, record_gate_run,
# _parse_gate_run_dict, and the seam's one-row-per-run recording
# ---------------------------------------------------------------------------


def _make_record(**overrides: Any) -> gc.GateRunRecord:
    """A valid GateRunRecord with named-field overrides, for the writer tests."""
    base: dict[str, Any] = {
        "started_at": "2026-09-08T00:00:00+00:00",
        "duration_seconds": 1.5,
        "workers": 4,
        "in_flight": 1,
        "admission": "slot",
        "outcome": "CLEAN",
        "available_mib": 8000,
        "bounded_by": "host",
    }
    base.update(overrides)
    return gc.GateRunRecord(**base)


def test_gate_run_record_is_frozen() -> None:
    record = _make_record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.workers = 8  # type: ignore[misc]


def test_observation_outcome_all_clean_returns_clean() -> None:
    assert gc.observation_outcome(["CLEAN", "CLEAN", "CLEAN"]) == "CLEAN"


def test_observation_outcome_empty_returns_clean() -> None:
    """A run with no observed gates produced no red, so it is CLEAN."""
    assert gc.observation_outcome([]) == "CLEAN"


def test_observation_outcome_picks_worst_red_by_precedence() -> None:
    """FAILED outranks COVERAGE — the failing tests are the fact worth fixing."""
    assert gc.observation_outcome(["CLEAN", "COVERAGE", "FAILED"]) == "FAILED"
    assert gc.observation_outcome(["COVERAGE", "COLLECTION_ERROR"]) == "COLLECTION_ERROR"


def test_observation_outcome_non_answers_outrank_reds() -> None:
    """A killed/unrunnable gate outranks an answered red so it stays separable."""
    assert gc.observation_outcome(["FAILED", "TIMEOUT"]) == "TIMEOUT"
    assert gc.observation_outcome(["FAILED", "UNAVAILABLE"]) == "UNAVAILABLE"
    assert gc.observation_outcome(["UNAVAILABLE", "TIMEOUT"]) == "TIMEOUT"


def test_outcome_precedence_covers_every_non_clean_kind() -> None:
    """``_OUTCOME_PRECEDENCE`` mirrors the non-``CLEAN`` ``GateObservationKind`` set.

    A completeness guard: a future kind added to the ``GateObservationKind``
    vocabulary that nobody adds here would be silently graded ``CLEAN`` by
    :func:`observation_outcome`. The precedence tuple must therefore list every
    member except ``CLEAN`` — no more, no less. ``GateObservationKind`` is a union
    of nested ``Literal``\\ s, so its args are recursively flattened to the leaf
    string members before comparing.
    """

    def _flatten(alias: object) -> set[str]:
        members: set[str] = set()
        for arg in get_args(alias):
            if isinstance(arg, str):
                members.add(arg)
            else:
                members |= _flatten(arg)
        return members

    all_kinds = _flatten(GateObservationKind)
    assert "CLEAN" in all_kinds  # guard the flatten actually resolved the members
    assert set(gc._OUTCOME_PRECEDENCE) == all_kinds - {"CLEAN"}


def test_record_gate_run_round_trips_through_parse(mem_fs: dict[str, str]) -> None:
    record = _make_record()
    gc.record_gate_run(RUNS_PATH, record)
    line = mem_fs[str(RUNS_PATH)].strip()
    assert gc._parse_gate_run_dict(json.loads(line)) == record


def test_record_gate_run_appends_one_line_per_call(mem_fs: dict[str, str]) -> None:
    gc.record_gate_run(RUNS_PATH, _make_record(outcome="CLEAN"))
    gc.record_gate_run(RUNS_PATH, _make_record(outcome="FAILED"))
    lines = mem_fs[str(RUNS_PATH)].strip().splitlines()
    assert len(lines) == 2
    first = gc._parse_gate_run_dict(json.loads(lines[0]))
    second = gc._parse_gate_run_dict(json.loads(lines[1]))
    assert first is not None
    assert second is not None
    assert first.outcome == "CLEAN"
    assert second.outcome == "FAILED"


def test_record_gate_run_uses_file_lock(mem_fs: dict[str, str]) -> None:
    """The append serializes on ``file_lock`` so a concurrent rotation cannot lose it."""
    locked: list[Any] = []

    @contextmanager
    def tracking_lock(path: Any) -> Iterator[None]:
        locked.append(path)
        yield

    with patch("maestro.gate_capacity.file_lock", tracking_lock):
        gc.record_gate_run(RUNS_PATH, _make_record())

    assert len(locked) == 1
    assert str(locked[0]).endswith("gate_runs.jsonl")
    assert mem_fs[str(RUNS_PATH)].strip() != ""


def test_record_gate_run_creates_parent_directory(mem_fs: dict[str, str]) -> None:
    """The writer makes its own parent before appending (mirrors append_cost_sample)."""
    nested = Path("/fake/nested/data/gate_runs.jsonl")
    with patch.object(Path, "mkdir", autospec=True) as mock_mkdir:
        gc.record_gate_run(nested, _make_record())
    mock_mkdir.assert_called_once_with(nested.parent, parents=True, exist_ok=True)
    assert mem_fs[str(nested)].strip() != ""


def test_parse_gate_run_dict_drops_row_missing_required_field() -> None:
    """A row missing a required field is dropped (None), never raised."""
    assert gc._parse_gate_run_dict({"workers": 4}) is None


def test_parse_gate_run_dict_filters_unknown_future_field() -> None:
    """An unknown extra field is dropped, not fatal — forward-compatible read."""
    row = dataclasses.asdict(_make_record())
    row["some_future_field"] = "ignored"
    assert gc._parse_gate_run_dict(row) == _make_record()


def test_parse_gate_run_dict_skips_corrupt_row_without_raising() -> None:
    """A structurally wrong row returns None rather than propagating."""
    assert gc._parse_gate_run_dict({"not": "a record"}) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("in_flight", None),
        ("in_flight", []),
        ("in_flight", "2"),
        ("workers", None),
        ("available_mib", None),
        ("deadline_seconds", "60"),
        ("duration_seconds", None),
        ("duration_seconds", "10"),
        ("duration_seconds", float("nan")),
        ("duration_seconds", float("inf")),
        ("duration_seconds", float("-inf")),
        ("tests_seconds", None),
        ("started_at", 5),
        ("admission", None),
        ("outcome", 3),
        ("bounded_by", []),
        ("source", 1),
        ("scope", None),
    ],
)
def test_parse_gate_run_dict_rejects_wrong_typed_numeric_fields(
    field: str, value: Any,
) -> None:
    """A structurally complete row carrying a wrong-typed value is dropped.

    A dataclass enforces none of its annotations at runtime, so without the
    value check these rows would parse and only fail later inside the sorts and
    medians of measure_gate_contention.
    """
    row = dataclasses.asdict(_make_record())
    row[field] = value
    assert gc._parse_gate_run_dict(row) is None


@pytest.mark.parametrize(
    "field", ["workers", "in_flight", "available_mib", "deadline_seconds"],
)
def test_parse_gate_run_dict_rejects_bool_where_int_required(field: str) -> None:
    """bool is an int subclass but is corruption in a count field."""
    row = dataclasses.asdict(_make_record())
    row[field] = True
    assert gc._parse_gate_run_dict(row) is None


@pytest.mark.parametrize("field", ["duration_seconds", "tests_seconds"])
def test_parse_gate_run_dict_rejects_bool_where_real_required(field: str) -> None:
    """A boolean duration is not a measurement, finite though it is."""
    row = dataclasses.asdict(_make_record())
    row[field] = False
    assert gc._parse_gate_run_dict(row) is None


def test_parse_gate_run_dict_accepts_int_for_real_field() -> None:
    """JSON renders 10.0 as 10, so an int duration must still round-trip."""
    row = dataclasses.asdict(_make_record(duration_seconds=10.0))
    row["duration_seconds"] = 10
    parsed = gc._parse_gate_run_dict(row)
    assert parsed is not None
    assert parsed.duration_seconds == 10


def test_parse_gate_run_dict_keeps_defaults_for_absent_optional_fields() -> None:
    """An optional field absent from the row takes its default, unchecked."""
    row = dataclasses.asdict(_make_record())
    del row["scope"]
    del row["tests_seconds"]
    parsed = gc._parse_gate_run_dict(row)
    assert parsed is not None
    assert parsed.scope == "full"
    assert parsed.tests_seconds == 0.0


def test_parse_gate_run_dict_warning_names_the_offending_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The rejection WARNING must be actionable: name the field and both types.

    A bare "Skipping corrupted gate_run row" leaves an operator no way to tell
    which value was bad. The raw value is deliberately absent — a corrupt line
    can carry an arbitrarily long string into an unbounded log record.
    """
    row = dataclasses.asdict(_make_record())
    row["in_flight"] = "2"
    with caplog.at_level(logging.WARNING, logger=gc.logger.name):
        assert gc._parse_gate_run_dict(row, source=Path("/ledger.jsonl")) is None
    message = caplog.records[-1].getMessage()
    assert "/ledger.jsonl" in message
    assert "in_flight: expected int, got str" in message
    assert '"2"' not in message


def test_parse_gate_run_dict_warning_names_a_non_finite_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The non-finite rejection gets its own message, not the wrong-type one."""
    row = dataclasses.asdict(_make_record())
    row["duration_seconds"] = float("nan")
    with caplog.at_level(logging.WARNING, logger=gc.logger.name):
        assert gc._parse_gate_run_dict(row) is None
    message = caplog.records[-1].getMessage()
    assert "duration_seconds: expected a finite number" in message


def test_parse_gate_run_dict_round_trips_well_typed_row() -> None:
    """The well-typed row is unaffected by the value check."""
    record = _make_record()
    assert gc._parse_gate_run_dict(dataclasses.asdict(record)) == record


def test_lease_for_gate_run_records_one_row_on_slot_admission(
    mem_fs: dict[str, str],
) -> None:
    """A 'slot' admission with runs_path given records exactly one well-formed row."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)

    @contextmanager
    def fake_acquire(lock_dir: Path, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        yield "slot"

    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", return_value=3),
        gc.lease_for_gate_run(LOCK_DIR, runs_path=RUNS_PATH) as run,
    ):
        run.outcome = "CLEAN"

    lines = mem_fs[str(RUNS_PATH)].strip().splitlines()
    assert len(lines) == 1
    record = gc._parse_gate_run_dict(json.loads(lines[0]))
    assert record is not None
    assert record.admission == "slot"
    # count_in_flight 3 -> in_flight 2 -> plan_lease(cap, 2): 8 // 3 == 2 workers.
    assert record.workers == 2
    assert record.in_flight == 2
    assert record.outcome == "CLEAN"
    assert record.duration_seconds >= 0.0
    assert record.started_at != ""


def test_lease_for_gate_run_records_fallback_admission_as_saturation(
    mem_fs: dict[str, str],
) -> None:
    """A 'fallback' admission records admission='fallback', the floor width, and

    the full slot budget as the saturation in_flight — without reading the count.
    """
    capacity = gc.HostCapacity(cores=8, available_mib=64000)

    @contextmanager
    def fake_acquire(lock_dir: Path, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        yield "fallback"

    mock_count = MagicMock()
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", mock_count),
        gc.lease_for_gate_run(LOCK_DIR, runs_path=RUNS_PATH) as run,
    ):
        run.outcome = "FAILED"

    lines = mem_fs[str(RUNS_PATH)].strip().splitlines()
    assert len(lines) == 1
    record = gc._parse_gate_run_dict(json.loads(lines[0]))
    assert record is not None
    assert record.admission == "fallback"
    assert record.workers == 1
    assert record.in_flight == 8
    assert record.outcome == "FAILED"
    # The fail-open path holds no slot and reads no count.
    mock_count.assert_not_called()


def test_lease_for_gate_run_stamps_planned_headroom_on_both_admissions(
    mem_fs: dict[str, str],
) -> None:
    """Every recorded row carries the headroom the grant was planned against.

    Unconditional and additive — the row is stamped on the slot and fallback
    branches alike, and regardless of ``cgroup_reserve``, which is what makes the
    policy flag flippable against recorded evidence rather than an assumption.
    """
    capacity = gc.HostCapacity(cores=2, available_mib=1400, bounded_by="cgroup")

    for admission in ("slot", "fallback"):

        @contextmanager
        def fake_acquire(
            lock_dir: Path, slots: int, _admission: str = admission, wait_seconds: float = 0.0
        ) -> Iterator[str]:
            yield _admission

        mem_fs.pop(str(RUNS_PATH), None)
        with (
            patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
            patch("maestro.gate_capacity.acquire_slot", fake_acquire),
            patch("maestro.gate_capacity.count_in_flight", return_value=1),
            gc.lease_for_gate_run(LOCK_DIR, runs_path=RUNS_PATH) as run,
        ):
            run.outcome = "CLEAN"

        lines = mem_fs[str(RUNS_PATH)].strip().splitlines()
        assert len(lines) == 1
        record = gc._parse_gate_run_dict(json.loads(lines[0]))
        assert record is not None
        assert record.admission == admission
        assert record.available_mib == 1400
        assert record.bounded_by == "cgroup"


def test_lease_for_gate_run_passes_cgroup_reserve_to_the_worker_plan(
    mem_fs: dict[str, str],
) -> None:
    """The seam's opt-in reaches the post-admission plan, widening a container grant."""
    capacity = gc.HostCapacity(cores=2, available_mib=1400, bounded_by="cgroup")

    @contextmanager
    def fake_acquire(lock_dir: Path, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        yield "slot"

    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(
            LOCK_DIR, runs_path=RUNS_PATH, cgroup_reserve=True
        ) as run,
    ):
        assert run.lease is not None
        # Flag-off this is the one-worker floor: (1400 - 1100) // 350 == 0.
        assert run.lease.workers == 2

    record = gc._parse_gate_run_dict(
        json.loads(mem_fs[str(RUNS_PATH)].strip().splitlines()[0])
    )
    assert record is not None
    assert record.workers == 2
    assert record.bounded_by == "cgroup"


def test_parse_gate_run_dict_reads_a_pre_change_row_to_the_defaults() -> None:
    """A row written before the headroom fields still parses, not dropped.

    Without the field defaults every historical row would raise ``TypeError`` and
    be skipped, silently wiping the contention window the ledger exists to serve.
    """
    row = dataclasses.asdict(_make_record())
    del row["available_mib"]
    del row["bounded_by"]

    record = gc._parse_gate_run_dict(row)

    assert record is not None
    assert record.available_mib == 0
    assert record.bounded_by == "host"
    assert record.workers == _make_record().workers


def test_lease_for_gate_run_flag_off_writes_no_row(mem_fs: dict[str, str]) -> None:
    """A None slot_dir (flag off) records nothing even when runs_path is given."""
    with gc.lease_for_gate_run(None, runs_path=RUNS_PATH) as run:
        assert run.lease is None
        run.outcome = "CLEAN"
    assert str(RUNS_PATH) not in mem_fs


def test_lease_for_gate_run_no_runs_path_writes_no_row(mem_fs: dict[str, str]) -> None:
    """Recording is opt-in: flag on but no runs_path writes no row."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)

    @contextmanager
    def fake_acquire(lock_dir: Path, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        yield "slot"

    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(LOCK_DIR) as run,
    ):
        run.outcome = "CLEAN"
    assert str(RUNS_PATH) not in mem_fs


def test_lease_for_gate_run_records_row_even_when_body_raises(
    mem_fs: dict[str, str],
) -> None:
    """A body that raises still records one row, carrying the default UNKNOWN outcome."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)

    @contextmanager
    def fake_acquire(lock_dir: Path, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
        yield "slot"

    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        pytest.raises(RuntimeError, match="boom"),
        gc.lease_for_gate_run(LOCK_DIR, runs_path=RUNS_PATH) as run,
    ):
        assert run.lease is not None
        raise RuntimeError("boom")

    lines = mem_fs[str(RUNS_PATH)].strip().splitlines()
    assert len(lines) == 1
    record = gc._parse_gate_run_dict(json.loads(lines[0]))
    assert record is not None
    assert record.admission == "slot"
    assert record.outcome == "UNKNOWN"


# ---------------------------------------------------------------------------
# The read surface — read_gate_runs / read_recent_gate_runs, _percentile, and
# measure_gate_contention (proposal 126)
# ---------------------------------------------------------------------------


def _row(record: gc.GateRunRecord) -> str:
    """Serialize one GateRunRecord to its JSONL line, as the writer would."""
    return json.dumps(dataclasses.asdict(record))


def test_read_gate_runs_missing_file_is_empty(mem_fs: dict[str, str]) -> None:
    """A ledger that was never written reads as an empty list, not a crash."""
    assert gc.read_gate_runs(RUNS_PATH) == []


def test_read_gate_runs_round_trips_written_rows(mem_fs: dict[str, str]) -> None:
    """Rows written by the writer read back oldest-first through the shared reader."""
    gc.record_gate_run(RUNS_PATH, _make_record(outcome="CLEAN"))
    gc.record_gate_run(RUNS_PATH, _make_record(outcome="FAILED"))
    records = gc.read_gate_runs(RUNS_PATH)
    assert [r.outcome for r in records] == ["CLEAN", "FAILED"]


def test_read_gate_runs_skips_corrupt_blank_and_non_dict_lines(
    mem_fs: dict[str, str],
) -> None:
    """Blank, non-JSON, non-dict, and structurally-wrong lines are dropped, never fatal."""
    valid = _row(_make_record(outcome="CLEAN"))
    RUNS_PATH.write_text(
        "\n".join([
            valid,
            "",
            "   ",
            "not json at all",
            "[1, 2, 3]",
            json.dumps({"missing": "required fields"}),
            valid,
        ]) + "\n",
    )
    records = gc.read_gate_runs(RUNS_PATH)
    assert len(records) == 2
    assert all(r.outcome == "CLEAN" for r in records)


def test_read_gate_runs_oserror_is_empty(mem_fs: dict[str, str]) -> None:
    """An OSError on the ledger read yields [], matching the sibling activation reader."""
    RUNS_PATH.write_text(_row(_make_record()) + "\n")
    with patch.object(Path, "read_text", side_effect=OSError("disk gone")):
        assert gc.read_gate_runs(RUNS_PATH) == []


@pytest.mark.parametrize("limit", [0, -1])
def test_read_recent_gate_runs_non_positive_limit_is_empty(
    limit: int, mem_fs: dict[str, str],
) -> None:
    """A non-positive window reads nothing before touching the file."""
    RUNS_PATH.write_text(_row(_make_record()) + "\n")
    assert gc.read_recent_gate_runs(RUNS_PATH, limit) == []


def test_read_recent_gate_runs_missing_file_is_empty(mem_fs: dict[str, str]) -> None:
    """A missing ledger reads as [] even with a positive limit."""
    assert gc.read_recent_gate_runs(RUNS_PATH, 5) == []


def test_read_recent_gate_runs_returns_the_tail_oldest_first(
    mem_fs: dict[str, str],
) -> None:
    """The count window keeps only the newest N rows, still oldest-first."""
    for i in range(5):
        gc.record_gate_run(RUNS_PATH, _make_record(in_flight=i))
    records = gc.read_recent_gate_runs(RUNS_PATH, 2)
    assert [r.in_flight for r in records] == [3, 4]


def test_read_recent_gate_runs_drops_corrupt_line_inside_window(
    mem_fs: dict[str, str],
) -> None:
    """A non-JSON and a non-dict line inside the tail window are both dropped.

    So fewer than N rows may return — the same corruption tolerance as the full
    reader, exercised through the tail path.
    """
    valid = _row(_make_record(outcome="CLEAN"))
    RUNS_PATH.write_text("\n".join([valid, "garbage", "[1, 2, 3]", valid]) + "\n")
    records = gc.read_recent_gate_runs(RUNS_PATH, 4)
    assert len(records) == 2


def test_read_recent_gate_runs_oserror_is_empty(mem_fs: dict[str, str]) -> None:
    """An OSError on the tail read yields [], matching read_gate_runs."""
    RUNS_PATH.write_text(_row(_make_record()) + "\n")
    with patch.object(Path, "read_text", side_effect=OSError("disk gone")):
        assert gc.read_recent_gate_runs(RUNS_PATH, 5) == []


def test_percentile_empty_is_zero() -> None:
    assert gc._percentile([], 95.0) == 0.0


def test_percentile_single_value_is_that_value() -> None:
    assert gc._percentile([4.2], 50.0) == 4.2


def test_percentile_nearest_rank() -> None:
    """Nearest-rank: p50 of 1..10 is the upper-middle (rank ceil(5) -> index 4)."""
    values = [float(v) for v in range(1, 11)]
    assert gc._percentile(values, 50.0) == 5.0
    assert gc._percentile(values, 95.0) == 10.0


def test_percentile_p95_of_short_list_is_max() -> None:
    """A short bucket's p95 is its maximum — the rank clamps to N."""
    assert gc._percentile([2.0, 9.0], 95.0) == 9.0


def test_measure_gate_contention_empty_is_zeroed_report() -> None:
    """No rows returns a well-formed zeroed report — the guarded division path."""
    report = gc.measure_gate_contention([])
    assert report["total"] == 0
    assert report["buckets"] == []
    assert report["resource_artifact_rate"] == 0.0
    assert report["fallback_rate"] == 0.0


def test_measure_gate_contention_buckets_by_in_flight_ascending() -> None:
    """Buckets are one-per-distinct-in_flight, ascending, each with p50/p95."""
    records = [
        _make_record(in_flight=2, duration_seconds=10.0),
        _make_record(in_flight=0, duration_seconds=1.0),
        _make_record(in_flight=0, duration_seconds=3.0),
        _make_record(in_flight=2, duration_seconds=30.0),
    ]
    report = gc.measure_gate_contention(records)
    assert report["total"] == 4
    buckets = report["buckets"]
    assert isinstance(buckets, list)
    assert [b["in_flight"] for b in buckets] == [0, 2]
    assert buckets[0]["count"] == 2
    # p50 nearest-rank of [1.0, 3.0]: rank ceil(0.5*2)=1 -> index 0 -> 1.0
    assert buckets[0]["p50_seconds"] == 1.0
    # p95 nearest-rank of [10.0, 30.0]: rank ceil(0.95*2)=2 -> index 1 -> 30.0
    assert buckets[1]["p95_seconds"] == 30.0


def test_measure_gate_contention_reports_p50_wait_per_bucket() -> None:
    """Each bucket carries the median queue wait apart from its wall time."""
    records = [
        _make_record(in_flight=1, duration_seconds=300.0, wait_seconds=100.0),
        _make_record(in_flight=1, duration_seconds=200.0, wait_seconds=60.0),
        _make_record(in_flight=0, duration_seconds=150.0),
    ]
    buckets = gc.measure_gate_contention(records)["buckets"]
    assert isinstance(buckets, list)
    assert buckets[0]["p50_wait_seconds"] == 0.0
    assert buckets[1]["p50_wait_seconds"] == 60.0
    assert buckets[1]["p50_seconds"] == 200.0


def test_measure_gate_contention_resource_artifact_rate() -> None:
    """TIMEOUT / UNAVAILABLE / COVERAGE count as artifacts; an answered FAILED does not."""
    records = [
        _make_record(outcome="TIMEOUT"),
        _make_record(outcome="UNAVAILABLE"),
        _make_record(outcome="COVERAGE"),
        _make_record(outcome="FAILED"),
        _make_record(outcome="CLEAN"),
    ]
    report = gc.measure_gate_contention(records)
    assert report["resource_artifact_rate"] == pytest.approx(3 / 5)


def test_measure_gate_contention_fallback_rate() -> None:
    """The fallback rate is the fraction of rows that fell open under saturation."""
    records = [
        _make_record(admission="fallback"),
        _make_record(admission="slot"),
        _make_record(admission="slot"),
        _make_record(admission="fallback"),
    ]
    report = gc.measure_gate_contention(records)
    assert report["fallback_rate"] == pytest.approx(2 / 4)


# ---------------------------------------------------------------------------
# Proposal 134 — the lease owns the run's budget as well as its width:
# derive_deadline's censored escalation, deadline_or_default, and the seam's
# source / deadline_seconds telemetry fields
# ---------------------------------------------------------------------------


def _censored(**overrides: Any) -> gc.GateRunRecord:
    """A row recording a run killed at its own deadline, for the derivation tests."""
    base: dict[str, Any] = {
        "workers": 1,
        "outcome": "TIMEOUT",
        "source": "in_process",
        "deadline_seconds": 300,
    }
    base.update(overrides)
    return _make_record(**base)


def test_derive_deadline_unmeasured_width_returns_the_floor() -> None:
    """No row at this width: nothing measured, so the budget is ``selfcheck``."""
    assert gc.derive_deadline([], workers=1, selfcheck=300, selfcheck_max=900) == 300


def test_derive_deadline_escalates_each_censored_reading() -> None:
    """A killed run says only "more than this" — so the next budget steps up 1.5x."""
    for censored_at, expected in ((300, 450), (450, 675), (675, 900)):
        records = [_censored(deadline_seconds=censored_at)]
        assert gc.derive_deadline(
            records, workers=1, selfcheck=300, selfcheck_max=900,
        ) == expected


def test_derive_deadline_clamps_at_the_hard_cap() -> None:
    """A run censored *at* the cap derives the cap again — a genuine hang stays bounded."""
    records = [_censored(deadline_seconds=900)]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 900


def test_derive_deadline_completed_run_retains_its_budget() -> None:
    """A run that *finished* — even red — keeps the budget it finished under.

    Collapsing back to ``selfcheck`` would hand a suite that needed 450s exactly
    300s on the next run, kill it, and oscillate forever instead of settling.
    """
    for outcome in ("CLEAN", "FAILED", "COVERAGE"):
        records = [_censored(outcome=outcome, deadline_seconds=450)]
        assert gc.derive_deadline(
            records, workers=1, selfcheck=300, selfcheck_max=900,
        ) == 450


def test_derive_deadline_completed_run_below_the_floor_returns_the_floor() -> None:
    """A pre-change or flag-off completed row (``0``) still floors at ``selfcheck``."""
    records = [_censored(outcome="CLEAN", deadline_seconds=0)]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 300


def test_derive_deadline_completed_run_is_clamped_at_the_cap() -> None:
    """A retained budget is still bounded by the hard cap, never carried above it."""
    records = [_censored(outcome="CLEAN", deadline_seconds=1200)]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 900


def test_derive_deadline_converges_instead_of_oscillating() -> None:
    """The escalation settles on a fit: 300 -> 450 -> 675, then holds at 675."""
    budget = gc.derive_deadline([], workers=1, selfcheck=300, selfcheck_max=900)
    assert budget == 300
    trace = []
    for _ in range(2):
        records = [_censored(deadline_seconds=budget)]
        budget = gc.derive_deadline(
            records, workers=1, selfcheck=300, selfcheck_max=900,
        )
        trace.append(budget)
    assert trace == [450, 675]
    # The suite now completes at 675 — the next run keeps 675, it is not reset.
    landed = gc.derive_deadline(
        [_censored(outcome="CLEAN", deadline_seconds=675)],
        workers=1, selfcheck=300, selfcheck_max=900,
    )
    assert landed == 675


@pytest.mark.parametrize("sibling", ["preflight", "gate_check", "wrapper"])
def test_derive_deadline_ignores_every_unbudgeted_sibling_source(
    sibling: str,
) -> None:
    """Only the loop's own budgeted suite rows are evidence.

    ``preflight`` and ``gate_check`` route through the same seam for the *width*
    but spend the flat ``selfcheck`` constant, so an escalating lineage must not
    be reset to the floor by one of their rows landing between two suite runs.
    """
    records = [
        _censored(deadline_seconds=450),
        _make_record(
            workers=1, outcome="CLEAN", source=sibling, deadline_seconds=0,
        ),
    ]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 675


def test_derive_deadline_ignores_a_wrapper_row() -> None:
    """A hand run enforced no deadline and ran a different argv: not evidence."""
    records = [_censored(source="wrapper", deadline_seconds=900)]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 300


def test_derive_deadline_ignores_a_row_at_another_width() -> None:
    """Suite cost is width-specific, so an 8-worker reading says nothing about 1."""
    records = [_censored(workers=8, deadline_seconds=600)]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 300


def test_derive_deadline_reads_a_pre_change_row_as_censored_at_selfcheck() -> None:
    """A ``deadline_seconds == 0`` row was really killed at the flat constant."""
    records = [_censored(deadline_seconds=0)]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 450


def test_derive_deadline_reads_only_the_most_recent_row_at_the_width() -> None:
    """The newest reading wins: an old censored run is not re-escalated off."""
    records = [
        _censored(deadline_seconds=450),
        _censored(outcome="CLEAN", deadline_seconds=300),
    ]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 300


def test_derive_deadline_is_pure() -> None:
    """The derivation reads its rows and mutates nothing (no I/O, no list edits)."""
    records = [_censored()]
    before = list(records)
    gc.derive_deadline(records, workers=1, selfcheck=300, selfcheck_max=900)
    assert records == before


def test_deadline_or_default_none_lease_falls_back() -> None:
    assert gc.deadline_or_default(None, 300) == 300


def test_deadline_or_default_zero_sentinel_falls_back() -> None:
    """A grant carrying the "no budget granted" sentinel resolves to ``selfcheck``."""
    assert gc.deadline_or_default(gc.GateLease(workers=4, slots=8), 300) == 300


def test_deadline_or_default_returns_the_granted_budget() -> None:
    lease = gc.GateLease(workers=4, slots=8, deadline_seconds=675)
    assert gc.deadline_or_default(lease, 300) == 675


def test_gate_run_record_round_trips_source_and_deadline(
    mem_fs: dict[str, str],
) -> None:
    """The two new telemetry fields survive the write -> parse round trip."""
    record = _make_record(source="wrapper", deadline_seconds=675)
    gc.record_gate_run(RUNS_PATH, record)
    parsed = gc._parse_gate_run_dict(
        json.loads(mem_fs[str(RUNS_PATH)].strip())
    )
    assert parsed == record


def test_parse_gate_run_dict_reads_a_row_without_the_budget_fields() -> None:
    """A pre-134 row still parses, defaulting to in-process with no budget."""
    row = dataclasses.asdict(_make_record())
    del row["source"]
    del row["deadline_seconds"]

    parsed = gc._parse_gate_run_dict(row)

    assert parsed is not None
    assert parsed.source == "in_process"
    assert parsed.deadline_seconds == 0
    assert parsed.tests_seconds == 0.0


# ---------------------------------------------------------------------------
# Proposal 134 — the *completed* half: a finished suite's measured tests_seconds
# is what the next budget is derived from, not the rung escalation stopped on
# ---------------------------------------------------------------------------


def _completed(**overrides: Any) -> gc.GateRunRecord:
    """A row recording a run that *finished*, carrying a measured pytest time."""
    base: dict[str, Any] = {
        "workers": 1,
        "outcome": "CLEAN",
        "source": "in_process",
        "deadline_seconds": 675,
        "tests_seconds": 380.0,
    }
    base.update(overrides)
    return _make_record(**base)


def test_derive_deadline_completed_p50_sets_the_budget_with_headroom() -> None:
    """A measured 380s suite is budgeted at ceil(380 x 1.25) = 475, not its rung."""
    assert gc.derive_deadline(
        [_completed()], workers=1, selfcheck=300, selfcheck_max=900,
    ) == 475


def test_derive_deadline_completed_p50_is_the_median_of_the_readings() -> None:
    """Several completed rows: the p50, not the latest or the maximum."""
    records = [
        _completed(tests_seconds=200.0),
        _completed(tests_seconds=380.0),
        _completed(tests_seconds=800.0),
    ]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 475


def test_derive_deadline_completed_p50_below_the_floor_returns_the_floor() -> None:
    """A fast suite still gets the ``selfcheck`` floor — the budget never shrinks below it."""
    assert gc.derive_deadline(
        [_completed(tests_seconds=60.0)],
        workers=1, selfcheck=300, selfcheck_max=900,
    ) == 300


def test_derive_deadline_completed_p50_above_the_cap_returns_the_cap() -> None:
    """A suite measured past the cap is still bounded by it: a hang stays bounded."""
    assert gc.derive_deadline(
        [_completed(tests_seconds=1000.0)],
        workers=1, selfcheck=300, selfcheck_max=900,
    ) == 900


def test_derive_deadline_censored_latest_still_escalates_past_completed_rows() -> None:
    """The censored arm takes precedence: an unfinished suite keeps climbing.

    A completed neighbour measured 380s, but the *most recent* run at this width
    was killed at 450 — so this width is not fitting today, and the budget steps
    up rather than collapsing back onto the stale measurement.
    """
    records = [_completed(tests_seconds=380.0), _censored(deadline_seconds=450)]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 675


def test_derive_deadline_serial_p50_stands_in_at_a_wider_grant() -> None:
    """Only one-worker runs are measured: the serial p50 is split across the width.

    ``ceil(380 / 2 x 1.25) = 238`` is below the floor, so the floor stands — the
    stand-in is deliberately conservative.
    """
    records = [
        _completed(workers=1, tests_seconds=380.0),
        _completed(workers=2, deadline_seconds=500, tests_seconds=0.0),
    ]
    assert gc.derive_deadline(
        records, workers=2, selfcheck=300, selfcheck_max=900,
    ) == 300


def test_derive_deadline_serial_p50_stand_in_can_exceed_the_floor() -> None:
    """A slow serial suite split across two workers still derives above the floor."""
    records = [
        _completed(workers=1, tests_seconds=800.0),
        # 400 is a retained-budget value the fallback *would* produce, and is
        # deliberately not the 500 the stand-in derives (800 / 2 x 1.25), so
        # deleting the stand-in arm fails this test instead of passing by luck.
        _completed(workers=2, deadline_seconds=400, tests_seconds=0.0),
    ]
    assert gc.derive_deadline(
        records, workers=2, selfcheck=300, selfcheck_max=900,
    ) == 500


def test_derive_deadline_prefers_the_granted_width_over_the_serial_stand_in() -> None:
    """With a measurement at this very width, the serial stand-in is not consulted."""
    records = [
        _completed(workers=1, tests_seconds=800.0),
        _completed(workers=2, tests_seconds=320.0),
    ]
    assert gc.derive_deadline(
        records, workers=2, selfcheck=300, selfcheck_max=900,
    ) == 400


@pytest.mark.parametrize("sibling", ["preflight", "gate_check", "wrapper"])
def test_derive_deadline_p50_excludes_unbudgeted_sibling_sources(
    sibling: str,
) -> None:
    """A row from a caller that spent a different budget is not a fit measurement."""
    records = [
        _completed(tests_seconds=380.0),
        _completed(source=sibling, tests_seconds=60.0),
    ]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 475


def test_derive_deadline_p50_excludes_other_width_rows() -> None:
    """A wider grant ran a different (faster) suite: not evidence about this one."""
    records = [
        _completed(workers=8, tests_seconds=60.0),
        _completed(workers=1, tests_seconds=380.0),
    ]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 475


def test_derive_deadline_p50_excludes_censored_readings() -> None:
    """A killed run's ``tests_seconds`` measures the deadline, not the suite."""
    records = [
        _censored(deadline_seconds=300, tests_seconds=300.0),
        _completed(tests_seconds=380.0),
    ]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 475


@pytest.mark.parametrize("truncated", ["FAILED", "COLLECTION_ERROR", "UNAVAILABLE"])
def test_derive_deadline_p50_excludes_truncated_readings(truncated: str) -> None:
    """A run cut short times where it stopped, so it is not a suite measurement."""
    records = [
        _completed(outcome=truncated, tests_seconds=5.0),
        _completed(tests_seconds=380.0),
    ]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 475


def test_derive_deadline_p50_excludes_an_unknown_reading() -> None:
    """An ungraded row holds no claim that the suite ran to completion."""
    records = [
        _completed(outcome="UNKNOWN", tests_seconds=5.0),
        _completed(tests_seconds=380.0),
    ]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 475


def test_derive_deadline_p50_reads_a_coverage_row_as_completed() -> None:
    """A coverage shortfall is only computable after the last test ran."""
    assert gc.derive_deadline(
        [_completed(outcome="COVERAGE", tests_seconds=380.0)],
        workers=1, selfcheck=300, selfcheck_max=900,
    ) == 475


def test_derive_deadline_p50_medians_only_the_recent_window() -> None:
    """Stale readings past the window cannot pin the budget to an old suite."""
    records = [
        *[_completed(tests_seconds=100.0) for _ in range(gc._DEADLINE_P50_WINDOW)],
        *[_completed(tests_seconds=380.0) for _ in range(gc._DEADLINE_P50_WINDOW)],
    ]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 475


def test_derive_deadline_without_tests_seconds_retains_the_finished_budget() -> None:
    """A pre-134 completed row carries no measurement: today's retained budget stands."""
    assert gc.derive_deadline(
        [_completed(tests_seconds=0.0)],
        workers=1, selfcheck=300, selfcheck_max=900,
    ) == 675


def test_gate_run_record_round_trips_tests_seconds(mem_fs: dict[str, str]) -> None:
    """The measured pytest time survives the write -> parse round trip."""
    record = _make_record(tests_seconds=380.5)
    gc.record_gate_run(RUNS_PATH, record)
    parsed = gc._parse_gate_run_dict(
        json.loads(mem_fs[str(RUNS_PATH)].strip())
    )
    assert parsed == record


def test_time_tests_command_keeps_the_longest_of_two_invocations() -> None:
    """Probe and enumerate re-run both fold in; the longer one wins (not their sum)."""
    run = gc.GateRun(lease=None)
    ticks = iter([0.0, 12.0, 100.0, 105.0])
    with patch("maestro.gate_capacity.time.monotonic", lambda: next(ticks)):
        with run.time_tests_command():
            pass
        with run.time_tests_command():
            pass
    assert run.tests_seconds == 12.0


def test_time_tests_command_stamps_even_when_the_body_raises() -> None:
    """A killed suite is a censored reading, and evidence too: it is still stamped."""
    run = gc.GateRun(lease=None)
    ticks = iter([0.0, 300.0])
    with (
        patch("maestro.gate_capacity.time.monotonic", lambda: next(ticks)),
        pytest.raises(RuntimeError),
        run.time_tests_command(),
    ):
        raise RuntimeError("killed")
    assert run.tests_seconds == 300.0


@contextmanager
def _slot_admission(lock_dir: Path, slots: int, wait_seconds: float = 0.0) -> Iterator[str]:
    """Stand in for ``acquire_slot``, always granting a real slot."""
    yield "slot"


def test_lease_for_gate_run_flag_off_grants_no_budget(mem_fs: dict[str, str]) -> None:
    """Budget flag off, the grant keeps the ``0`` sentinel and the row records it."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", _slot_admission),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(LOCK_DIR, runs_path=RUNS_PATH) as run,
    ):
        assert run.lease is not None
        assert run.lease.deadline_seconds == 0
        # The reader's fallback is what the gate actually enforces.
        assert gc.deadline_or_default(run.lease, 300) == 300

    record = gc._parse_gate_run_dict(
        json.loads(mem_fs[str(RUNS_PATH)].strip().splitlines()[0])
    )
    assert record is not None
    assert record.deadline_seconds == 0
    assert record.source == "in_process"


def test_lease_for_gate_run_flag_on_grants_the_derived_budget(
    mem_fs: dict[str, str],
) -> None:
    """Flag on, the grant carries the escalation derived at the granted width."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    gc.record_gate_run(RUNS_PATH, _censored(workers=8, deadline_seconds=300))

    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", _slot_admission),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(
            LOCK_DIR,
            runs_path=RUNS_PATH,
            budget_lease_owned=True,
            selfcheck=300,
            selfcheck_max=900,
        ) as run,
    ):
        assert run.lease is not None
        assert run.lease.workers == 8
        assert run.lease.deadline_seconds == 450

    record = gc._parse_gate_run_dict(
        json.loads(mem_fs[str(RUNS_PATH)].strip().splitlines()[-1])
    )
    assert record is not None
    assert record.deadline_seconds == 450


def test_lease_for_gate_run_records_the_timed_tests_seconds(
    mem_fs: dict[str, str],
) -> None:
    """The row carries what the body timed through ``time_tests_command``."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    ticks = iter([0.0, 0.5, 1.0, 381.0, 2.0])
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", _slot_admission),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        patch("maestro.gate_capacity.time.monotonic", lambda: next(ticks)),
        gc.lease_for_gate_run(LOCK_DIR, runs_path=RUNS_PATH) as run,
        run.time_tests_command(),
    ):
        pass

    record = gc._parse_gate_run_dict(
        json.loads(mem_fs[str(RUNS_PATH)].strip().splitlines()[0])
    )
    assert record is not None
    assert record.tests_seconds == 380.0
    assert record.wait_seconds == 0.5


def test_lease_for_gate_run_untimed_body_records_zero_tests_seconds(
    mem_fs: dict[str, str],
) -> None:
    """A caller that never times a suite records the unmeasured ``0.0`` sentinel."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", _slot_admission),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(LOCK_DIR, runs_path=RUNS_PATH),
    ):
        pass

    record = gc._parse_gate_run_dict(
        json.loads(mem_fs[str(RUNS_PATH)].strip().splitlines()[0])
    )
    assert record is not None
    assert record.tests_seconds == 0.0


def test_lease_for_gate_run_flag_on_without_runs_path_grants_no_budget() -> None:
    """With no ledger to read there is nothing to derive from: the sentinel stands."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", _slot_admission),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(
            LOCK_DIR, budget_lease_owned=True, selfcheck=300, selfcheck_max=900,
        ) as run,
    ):
        assert run.lease is not None
        assert run.lease.deadline_seconds == 0


def test_lease_for_gate_run_stamps_the_caller_source(mem_fs: dict[str, str]) -> None:
    """A wrapper-sourced run is named on its row, keeping it out of the derivation."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", _slot_admission),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(
            LOCK_DIR, runs_path=RUNS_PATH, source="wrapper",
        ) as run,
    ):
        run.outcome = "TIMEOUT"

    record = gc._parse_gate_run_dict(
        json.loads(mem_fs[str(RUNS_PATH)].strip().splitlines()[0])
    )
    assert record is not None
    assert record.source == "wrapper"


# ---------------------------------------------------------------------------
# GateScope — the recorded scope field, its budget filter, and the per-width
# fit aggregation (proposal 134)
# ---------------------------------------------------------------------------


def test_gate_run_record_defaults_scope_to_full() -> None:
    """A row constructed without a scope records the whole suite, as today's runs are."""
    assert _make_record().scope == "full"


def test_gate_run_record_round_trips_an_explicit_scope(mem_fs: dict[str, str]) -> None:
    """A narrowed row survives the writer -> reader round trip with its scope intact."""
    gc.record_gate_run(RUNS_PATH, _make_record(scope="impacted"))
    assert [r.scope for r in gc.read_gate_runs(RUNS_PATH)] == ["impacted"]


def test_parse_gate_run_dict_reads_a_pre_change_row_as_full_scope() -> None:
    """A row written before the field existed parses, defaulting to the full suite."""
    row = dataclasses.asdict(_make_record())
    del row["scope"]
    record = gc._parse_gate_run_dict(row)
    assert record is not None
    assert record.scope == "full"


def test_lease_for_gate_run_stamps_full_scope_by_default(
    mem_fs: dict[str, str],
) -> None:
    """Every caller keeps the default today, so the recorded row reads ``"full"``."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", _slot_admission),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(LOCK_DIR, runs_path=RUNS_PATH) as run,
    ):
        run.outcome = "CLEAN"

    assert gc.read_gate_runs(RUNS_PATH)[0].scope == "full"


def test_lease_for_gate_run_stamps_an_explicit_scope(mem_fs: dict[str, str]) -> None:
    """The seam is where a narrowed caller names its scope, and it lands on the row."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", _slot_admission),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(
            LOCK_DIR, runs_path=RUNS_PATH, scope="impacted",
        ) as run,
    ):
        run.outcome = "CLEAN"

    assert gc.read_gate_runs(RUNS_PATH)[0].scope == "impacted"


def test_completed_tests_p50_ignores_a_narrowed_scope_reading() -> None:
    """A narrowed run measured a different command, so its cost is not the suite's."""
    records = [
        _completed(tests_seconds=380.0),
        _completed(tests_seconds=20.0, scope="impacted"),
    ]
    assert gc._completed_tests_p50(records, workers=1) == 380.0


def test_derive_deadline_ignores_a_narrowed_latest_row() -> None:
    """A narrowed newest row does not become the budget's latest reading.

    Left unfiltered, the 20s ``impacted`` row would drag the p50 down and hand the
    next *full* run a budget the suite cannot fit inside.
    """
    records = [
        _completed(tests_seconds=380.0),
        _completed(tests_seconds=20.0, scope="impacted"),
    ]
    assert gc.derive_deadline(
        records, workers=1, selfcheck=300, selfcheck_max=900,
    ) == 475


def test_measure_gate_contention_width_buckets_empty_ledger() -> None:
    """No rows: a well-formed empty fit table, not a missing key."""
    assert gc.measure_gate_contention([])["width_buckets"] == []


def test_measure_gate_contention_width_buckets_report_fit_per_width() -> None:
    """One ascending entry per observed width, carrying its p50s, ratio and kills."""
    records = [
        _completed(workers=1, tests_seconds=400.0, deadline_seconds=800),
        _completed(workers=1, tests_seconds=600.0, deadline_seconds=800),
        _censored(workers=1, deadline_seconds=800),
        _completed(workers=2, tests_seconds=300.0, deadline_seconds=600),
    ]
    report = gc.measure_gate_contention(records)
    assert report["width_buckets"] == [
        {
            "workers": 1,
            "count": 3,
            "p50_tests_seconds": 400.0,
            "p50_deadline_seconds": 800.0,
            "fit": 0.5,
            "censored": 1,
        },
        {
            "workers": 2,
            "count": 1,
            "p50_tests_seconds": 300.0,
            "p50_deadline_seconds": 600.0,
            "fit": 0.5,
            "censored": 0,
        },
    ]


def test_measure_gate_contention_width_buckets_exclude_sibling_sources() -> None:
    """Wrapper and sibling-reader rows ran a different command: no width of their own."""
    records = [
        _completed(workers=1, tests_seconds=400.0, deadline_seconds=800),
        _completed(workers=2, source="wrapper", tests_seconds=10.0),
        _completed(workers=4, source="preflight", tests_seconds=10.0),
        _completed(workers=8, source="gate_check", tests_seconds=10.0),
    ]
    buckets: list[dict[str, object]] = (
        gc.measure_gate_contention(records)["width_buckets"]  # type: ignore[assignment]
    )
    assert [b["workers"] for b in buckets] == [1]


def test_measure_gate_contention_width_bucket_fit_is_none_without_a_budget() -> None:
    """A flag-off or pre-change width records no budget, so the ratio is unavailable."""
    records = [_completed(workers=1, tests_seconds=400.0, deadline_seconds=0)]
    buckets: list[dict[str, object]] = (
        gc.measure_gate_contention(records)["width_buckets"]  # type: ignore[assignment]
    )
    assert buckets[0]["fit"] is None
    assert buckets[0]["p50_deadline_seconds"] == 0.0


def test_measure_gate_contention_width_bucket_fit_is_none_without_a_cost() -> None:
    """An all-truncated width measures no suite cost, so it reports no ratio."""
    records = [_censored(workers=1, deadline_seconds=450)]
    buckets: list[dict[str, object]] = (
        gc.measure_gate_contention(records)["width_buckets"]  # type: ignore[assignment]
    )
    assert buckets[0]["fit"] is None
    assert buckets[0]["censored"] == 1


def test_measure_gate_contention_width_buckets_ignore_narrowed_rows() -> None:
    """A narrowed row is excluded from every figure of a full-suite width's bucket."""
    records = [
        _completed(workers=1, tests_seconds=400.0, deadline_seconds=800),
        _completed(
            workers=1, tests_seconds=20.0, deadline_seconds=100, scope="impacted",
        ),
    ]
    buckets: list[dict[str, object]] = (
        gc.measure_gate_contention(records)["width_buckets"]  # type: ignore[assignment]
    )
    assert buckets[0]["count"] == 1
    assert buckets[0]["p50_tests_seconds"] == 400.0
    assert buckets[0]["p50_deadline_seconds"] == 800.0


def test_measure_gate_contention_width_buckets_omit_a_narrowed_only_width() -> None:
    """A width seen only by narrowed runs is no full-suite fit reading at all."""
    records = [
        _completed(workers=1, tests_seconds=400.0, deadline_seconds=800),
        _censored(workers=2, deadline_seconds=100, scope="impacted"),
    ]
    buckets: list[dict[str, object]] = (
        gc.measure_gate_contention(records)["width_buckets"]  # type: ignore[assignment]
    )
    assert [b["workers"] for b in buckets] == [1]
    assert buckets[0]["censored"] == 0


def test_measure_gate_contention_keeps_the_concurrency_keys_untouched() -> None:
    """The new key is additive: every existing reader of the report keeps working."""
    records = [_completed(workers=1, tests_seconds=400.0, deadline_seconds=800)]
    report = gc.measure_gate_contention(records)
    assert set(report) == {
        "total",
        "buckets",
        "width_buckets",
        "resource_artifact_rate",
        "fallback_rate",
        "declined_rate",
    }


# ---------------------------------------------------------------------------
# The runnability consult at the seam — a declining cached verdict refuses the
# run before any host read, slot or subprocess (proposal 133).
# ---------------------------------------------------------------------------

VERDICT_PATH = Path("/fake/data/gate_environment.json")


def _write_verdict(mem_fs: dict[str, str], kind: str, **extra: Any) -> None:
    """Write one cached runnability row of *kind* at :data:`VERDICT_PATH`."""
    row: dict[str, Any] = {
        "kind": kind,
        "fingerprint": "fp",
        "identities": [],
        "detail": "seeded",
        "measured_at": "2026-09-21T00:00:00+00:00",
        "iteration_key": "iter-1",
    }
    row.update(extra)
    mem_fs[str(VERDICT_PATH)] = json.dumps(row)


def test_lease_for_gate_run_declines_on_an_uncollectable_verdict(
    mem_fs: dict[str, str],
) -> None:
    """A declining verdict refuses the run: no host read, no slot, no count."""
    _write_verdict(mem_fs, "UNCOLLECTABLE", identities=["tests:COLLECTION_ERROR"])
    mock_measure = MagicMock()
    mock_acquire = MagicMock()
    mock_count = MagicMock()
    with (
        patch("maestro.gate_capacity.measure_host_capacity", mock_measure),
        patch("maestro.gate_capacity.acquire_slot", mock_acquire),
        patch("maestro.gate_capacity.count_in_flight", mock_count),
        gc.lease_for_gate_run(LOCK_DIR, verdict_path=VERDICT_PATH) as run,
    ):
        assert run.lease is None
        assert run.runnability is not None
        assert run.runnability.kind == "UNCOLLECTABLE"

    mock_measure.assert_not_called()
    mock_acquire.assert_not_called()
    mock_count.assert_not_called()


def test_lease_for_gate_run_records_one_declined_row(
    mem_fs: dict[str, str],
) -> None:
    """The refusal is recorded once, carrying the caller's source and scope."""
    _write_verdict(mem_fs, "UNPROVISIONED")
    with (
        patch("maestro.gate_capacity.measure_host_capacity", MagicMock()),
        patch("maestro.gate_capacity.acquire_slot", MagicMock()),
        gc.lease_for_gate_run(
            LOCK_DIR,
            runs_path=RUNS_PATH,
            verdict_path=VERDICT_PATH,
            source="gate_check",
            scope="impacted",
        ) as run,
    ):
        run.outcome = "COLLECTION_ERROR"

    lines = mem_fs[str(RUNS_PATH)].strip().splitlines()
    assert len(lines) == 1
    record = gc._parse_gate_run_dict(json.loads(lines[0]))
    assert record is not None
    assert record.admission == "declined"
    assert record.outcome == "COLLECTION_ERROR"
    assert record.workers == 0
    assert record.in_flight == 0
    assert record.source == "gate_check"
    assert record.scope == "impacted"
    assert record.duration_seconds < 1.0


@pytest.mark.parametrize("kind", ["SOUND", "UNFIT"])
def test_lease_for_gate_run_admits_a_non_declining_verdict(
    mem_fs: dict[str, str], kind: str,
) -> None:
    """A sound environment — and a poorly-fitting one — are admitted as today."""
    _write_verdict(mem_fs, kind)
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", _slot_admission),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(LOCK_DIR, verdict_path=VERDICT_PATH) as run,
    ):
        assert run.runnability is None
        assert run.lease is not None


def test_lease_for_gate_run_admits_when_nothing_has_been_probed(
    mem_fs: dict[str, str],
) -> None:
    """An absent cache row is the honest "nothing probed yet" — admit, never refuse."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", _slot_admission),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(LOCK_DIR, verdict_path=VERDICT_PATH) as run,
    ):
        assert run.runnability is None
        assert run.lease is not None


def test_lease_for_gate_run_reads_no_verdict_without_a_path(
    mem_fs: dict[str, str],
) -> None:
    """No verdict_path (flag off) means no read at all, even with a declining row."""
    _write_verdict(mem_fs, "UNCOLLECTABLE")
    mock_read = MagicMock()
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    with (
        patch("maestro.gate_capacity.read_verdict", mock_read),
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", _slot_admission),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(LOCK_DIR) as run,
    ):
        assert run.runnability is None

    mock_read.assert_not_called()


def test_declined_rows_are_excluded_from_the_contention_buckets() -> None:
    """A sub-second refusal is no reading about contention, and says so."""
    records = [
        _make_record(admission="slot", in_flight=0, duration_seconds=100.0),
        _make_record(admission="declined", in_flight=0, duration_seconds=0.01),
        _make_record(admission="declined", in_flight=0, duration_seconds=0.02),
    ]

    report = gc.measure_gate_contention(records)

    buckets: list[dict[str, Any]] = report["buckets"]  # type: ignore[assignment]
    assert len(buckets) == 1
    assert buckets[0]["count"] == 1
    assert buckets[0]["p50_seconds"] == 100.0
    assert report["declined_rate"] == pytest.approx(2 / 3)
    assert report["total"] == 3


def test_declined_rows_are_excluded_from_the_width_fit_buckets() -> None:
    """A run granted no width is a reading about neither cost nor budget."""
    records = [
        _make_record(
            admission="slot", workers=2, outcome="CLEAN",
            tests_seconds=50.0, deadline_seconds=100,
        ),
        _make_record(admission="declined", workers=0, outcome="COLLECTION_ERROR"),
    ]

    report = gc.measure_gate_contention(records)

    width_buckets: list[dict[str, Any]] = (
        report["width_buckets"]  # type: ignore[assignment]
    )
    assert [bucket["workers"] for bucket in width_buckets] == [2]


def test_declined_rate_is_zero_without_declined_rows() -> None:
    """Off-flag histories report the rate as a well-formed zero."""
    assert gc.measure_gate_contention([])["declined_rate"] == 0.0
    assert gc.measure_gate_contention(
        [_make_record(admission="slot")],
    )["declined_rate"] == 0.0


def test_declined_rows_leave_the_two_lease_rates_undiluted() -> None:
    """The lease rates denominate over admitted rows, not the whole window.

    One fallback and one artifact among two admitted runs is a 50% reading for
    each; eight refusals alongside them measured no host and held no slot, so
    they must not drag either figure down to 10%.
    """
    records = [
        _make_record(admission="fallback", outcome="TIMEOUT"),
        _make_record(admission="slot", outcome="CLEAN"),
        *[
            _make_record(admission="declined", outcome="COLLECTION_ERROR")
            for _ in range(8)
        ],
    ]

    report = gc.measure_gate_contention(records)

    assert report["resource_artifact_rate"] == pytest.approx(1 / 2)
    assert report["fallback_rate"] == pytest.approx(1 / 2)
    assert report["declined_rate"] == pytest.approx(8 / 10)
    assert report["total"] == 10


def test_an_all_declined_window_zeroes_the_lease_rates() -> None:
    """With no admitted row the two rates guard their zero denominator."""
    records = [
        _make_record(admission="declined", outcome="COLLECTION_ERROR"),
        _make_record(admission="declined", outcome="COLLECTION_ERROR"),
    ]

    report = gc.measure_gate_contention(records)

    assert report["resource_artifact_rate"] == 0.0
    assert report["fallback_rate"] == 0.0
    assert report["declined_rate"] == 1.0
    assert report["total"] == 2


def test_lease_off_reads_no_verdict_even_with_a_verdict_path() -> None:
    """The consult is scoped by the lease rollback as well as by its own flag.

    ``slot_dir=None`` is the documented pure no-op: it reads nothing and records
    nothing, so a *verdict_path* handed in alongside it is never read and the
    run is admitted (pre-133 behaviour), rather than a refusal escaping from a
    seam that is supposed to be inert.
    """
    with (
        patch("maestro.gate_capacity.read_verdict") as mock_read,
        gc.lease_for_gate_run(None, verdict_path=Path("/fake/verdict.json")) as run,
    ):
        assert run.runnability is None
        assert run.lease is None

    mock_read.assert_not_called()


@pytest.mark.parametrize(
    ("cores", "expected_slots"),
    [(8, 2), (16, 4), (4, 1), (2, 1), (0, 1), (-3, 1)],
)
def test_plan_lease_admission_cap_bounds_slots_by_min_workers(
    cores: int, expected_slots: int,
) -> None:
    """Flag on, the budget is ``max(1, cores // MIN_WORKERS_PER_ADMITTED_RUN)``."""
    capacity = gc.HostCapacity(cores=cores, available_mib=64000)
    assert gc.plan_lease(capacity, 0, admission_cap=True).slots == expected_slots


@pytest.mark.parametrize("cores", [1, 2, 4, 8, 16])
def test_plan_lease_admission_cap_off_keeps_slots_at_cores(cores: int) -> None:
    """Flag off, the slot budget is the core count exactly as before."""
    capacity = gc.HostCapacity(cores=cores, available_mib=64000)
    assert gc.plan_lease(capacity, 0).slots == cores


@pytest.mark.parametrize("in_flight", [0, 1, 2, 3, 7, 20])
def test_plan_lease_admission_cap_leaves_worker_grant_unchanged(in_flight: int) -> None:
    """The worker grant is identical with the flag on and off at every in_flight."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    on = gc.plan_lease(capacity, in_flight, admission_cap=True)
    off = gc.plan_lease(capacity, in_flight)
    assert on.workers == off.workers


def test_acquire_slot_honours_wait_seconds(caplog: pytest.LogCaptureFixture) -> None:
    """A saturated set waits out the passed ``wait_seconds``, not the 30s default.

    The 40s probe would have failed open under the default wait; with a 600s
    wait it keeps waiting until the clock passes 600s. The wait is announced once.
    """
    mock_sleep = MagicMock()
    mock_monotonic = MagicMock(side_effect=[0.0, 40.0, 300.0, 601.0])
    with (
        caplog.at_level(logging.INFO, logger="maestro.gate_capacity"),
        patch("maestro.gate_capacity.os.makedirs"),
        patch("maestro.gate_capacity.os.open", MagicMock(return_value=7)),
        patch("maestro.gate_capacity.os.close"),
        patch("maestro.gate_capacity.fcntl.flock", MagicMock(side_effect=BlockingIOError)),
        patch("maestro.gate_capacity.time.monotonic", mock_monotonic),
        patch("maestro.gate_capacity.time.sleep", mock_sleep),
        gc.acquire_slot(LOCK_DIR, slots=2, wait_seconds=600.0) as admission,
    ):
        assert admission == "fallback"

    assert mock_sleep.call_count == 2
    waiting = [r for r in caplog.records if "gate slots busy" in r.getMessage()]
    assert len(waiting) == 1


def test_lease_for_gate_run_admission_cap_uses_capped_wait_and_records_wait_seconds(
    mem_fs: dict[str, str],
) -> None:
    """Flag on, the seam probes the capped budget with the capped wait.

    The row stamps the queue time from seam entry to admission.
    """
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    acquire_calls: list[tuple[int, float]] = []

    @contextmanager
    def fake_acquire(
        lock_dir: Path, slots: int, wait_seconds: float = 0.0,
    ) -> Iterator[str]:
        acquire_calls.append((slots, wait_seconds))
        yield "slot"

    ticks = iter([0.0, 120.0, 400.0])
    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", return_value=2) as mock_count,
        patch("maestro.gate_capacity.time.monotonic", lambda: next(ticks)),
        gc.lease_for_gate_run(
            LOCK_DIR, runs_path=RUNS_PATH, admission_cap=True, source="wrapper",
        ),
    ):
        pass

    assert acquire_calls == [(2, gc._CAPPED_ACQUIRE_WAIT_SECONDS)]
    mock_count.assert_called_once_with(LOCK_DIR, 2)
    record = gc._parse_gate_run_dict(json.loads(mem_fs[str(RUNS_PATH)].strip()))
    assert record is not None
    assert record.wait_seconds == 120.0
    assert record.in_flight == 1


@pytest.mark.parametrize("source", ["in_process", "preflight"])
def test_lease_for_gate_run_admission_cap_skips_in_loop_sources(
    mem_fs: dict[str, str], source: gc.GateRunSource,
) -> None:
    """Flag on, an in-loop run keeps ``cores`` slots and the 30s default wait."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    acquire_calls: list[tuple[int, float]] = []

    @contextmanager
    def fake_acquire(
        lock_dir: Path, slots: int, wait_seconds: float = 0.0,
    ) -> Iterator[str]:
        acquire_calls.append((slots, wait_seconds))
        yield "slot"

    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(LOCK_DIR, admission_cap=True, source=source),
    ):
        pass

    assert acquire_calls == [(8, gc._ACQUIRE_WAIT_SECONDS)]


def test_lease_for_gate_run_admission_cap_off_keeps_default_wait(
    mem_fs: dict[str, str],
) -> None:
    """Flag off, the seam probes ``cores`` slots with the 30s default wait."""
    capacity = gc.HostCapacity(cores=8, available_mib=64000)
    acquire_calls: list[tuple[int, float]] = []

    @contextmanager
    def fake_acquire(
        lock_dir: Path, slots: int, wait_seconds: float = 0.0,
    ) -> Iterator[str]:
        acquire_calls.append((slots, wait_seconds))
        yield "slot"

    with (
        patch("maestro.gate_capacity.measure_host_capacity", return_value=capacity),
        patch("maestro.gate_capacity.acquire_slot", fake_acquire),
        patch("maestro.gate_capacity.count_in_flight", return_value=1),
        gc.lease_for_gate_run(LOCK_DIR),
    ):
        pass

    assert acquire_calls == [(8, gc._ACQUIRE_WAIT_SECONDS)]


def test_parse_gate_run_dict_defaults_missing_wait_seconds() -> None:
    """A pre-change row without ``wait_seconds`` still parses, reading ``0.0``."""
    row = {
        "started_at": "2026-10-01T00:00:00+00:00",
        "duration_seconds": 10.0,
        "workers": 4,
        "in_flight": 0,
        "admission": "slot",
        "outcome": "CLEAN",
    }
    record = gc._parse_gate_run_dict(row)
    assert record is not None
    assert record.wait_seconds == 0.0


def test_parse_gate_run_dict_rejects_non_finite_wait_seconds() -> None:
    """``wait_seconds`` is type-checked like the other real-valued fields."""
    row = {
        "started_at": "2026-10-01T00:00:00+00:00",
        "duration_seconds": 10.0,
        "workers": 4,
        "in_flight": 0,
        "admission": "slot",
        "outcome": "CLEAN",
        "wait_seconds": "slow",
    }
    assert gc._parse_gate_run_dict(row) is None

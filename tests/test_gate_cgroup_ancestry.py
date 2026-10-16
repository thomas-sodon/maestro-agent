"""Ancestor cgroup v2 limits: the readers walk the process's cgroup path to the root.

On a bare host the binding ``memory.max`` / ``cpu.max`` sits on an ancestor
slice (``user-<uid>.slice``), not on the leaf scope or the real root. Every read
here goes through ``mem_fs`` under ``/fake``; an unpopulated path raises
``FileNotFoundError`` exactly like an absent kernel file.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro import gate_capacity as gc

_ROOT = "/fake/cgroup"
_PROC = "/fake/proc/self/cgroup"
_SLICE = "user.slice/user-1003.slice"
_LEAF = f"{_SLICE}/session-566.scope"
_GIB = 1024 * 1024 * 1024


@pytest.fixture
def fs(mem_fs: dict[str, str]) -> Iterator[dict[str, str]]:
    root = Path(_ROOT)
    with (
        patch.object(gc, "_PROC_SELF_CGROUP_PATH", Path(_PROC)),
        patch.object(gc, "_CGROUP_V2_ROOT", root),
        patch.object(gc, "_CGROUP_V2_MEMORY_MAX_PATH", root / "memory.max"),
        patch.object(gc, "_CGROUP_V2_MEMORY_CURRENT_PATH", root / "memory.current"),
        patch.object(gc, "_CGROUP_V2_CPU_MAX_PATH", root / "cpu.max"),
        patch.object(gc, "_CGROUP_V1_MEMORY_LIMIT_PATH", Path("/fake/v1/limit")),
        patch.object(gc, "_CGROUP_V1_MEMORY_USAGE_PATH", Path("/fake/v1/usage")),
        patch.object(gc, "_CGROUP_V1_CPU_QUOTA_PATH", Path("/fake/v1/quota")),
        patch.object(gc, "_CGROUP_V1_CPU_PERIOD_PATH", Path("/fake/v1/period")),
        patch.object(gc, "_MEMINFO_PATH", Path("/fake/meminfo")),
    ):
        mem_fs[_PROC] = f"0::/{_LEAF}\n"
        yield mem_fs


def _put(fs: dict[str, str], rel: str, name: str, body: str) -> None:
    fs[f"{_ROOT}/{rel}/{name}" if rel else f"{_ROOT}/{name}"] = body


# --- parsing ----------------------------------------------------------------


def test_ancestor_dirs_are_leaf_first_below_the_root(fs: dict[str, str]) -> None:
    assert gc._cgroup_v2_ancestor_dirs() == (
        Path(_ROOT, _LEAF),
        Path(_ROOT, _SLICE),
        Path(_ROOT, "user.slice"),
    )


@pytest.mark.parametrize(
    "body",
    ["0::/\n", "1:name=systemd:/x\n", "0::/../..\n", "0::relative/x\n", "0::/a/./b\n"],
)
def test_ancestor_dirs_are_empty_for_root_missing_or_escaping(
    fs: dict[str, str], body: str,
) -> None:
    fs[_PROC] = body

    assert gc._cgroup_v2_ancestor_dirs() == ()


def test_ancestor_dirs_are_empty_when_proc_file_is_unreadable(fs: dict[str, str]) -> None:
    del fs[_PROC]

    assert gc._cgroup_v2_ancestor_dirs() == ()


# --- memory ------------------------------------------------------------------


def test_total_mib_takes_the_ancestor_slice_limit(fs: dict[str, str]) -> None:
    _put(fs, _LEAF, "memory.max", "max\n")
    _put(fs, _SLICE, "memory.max", f"{6 * _GIB}\n")
    fs["/fake/meminfo"] = "MemTotal:       24117248 kB\n"

    assert gc._read_total_mib() == 6144


def test_total_mib_takes_the_tightest_of_several_ancestors(fs: dict[str, str]) -> None:
    _put(fs, _LEAF, "memory.max", f"{8 * _GIB}\n")
    _put(fs, _SLICE, "memory.max", f"{3 * _GIB}\n")
    _put(fs, "user.slice", "memory.max", f"{5 * _GIB}\n")

    assert gc._read_total_mib() == 3072


def test_memory_bound_takes_the_tightest_ancestor_headroom(fs: dict[str, str]) -> None:
    _put(fs, _LEAF, "memory.max", "max\n")
    _put(fs, _LEAF, "memory.current", "0\n")
    _put(fs, _SLICE, "memory.max", f"{6 * _GIB}\n")
    _put(fs, _SLICE, "memory.current", f"{5 * _GIB}\n")
    _put(fs, "user.slice", "memory.max", f"{10 * _GIB}\n")
    _put(fs, "user.slice", "memory.current", f"{5 * _GIB}\n")
    fs["/fake/meminfo"] = "MemAvailable:   16777216 kB\n"

    assert gc._read_memory_bound() == (1024, "cgroup")


def test_memory_bound_ancestor_with_unreadable_current_binds_nothing(
    fs: dict[str, str],
) -> None:
    _put(fs, _SLICE, "memory.max", f"{4 * _GIB}\n")
    fs["/fake/meminfo"] = "MemAvailable:   16777216 kB\n"

    assert gc._read_cgroup_v2_available_mib() == gc._CgroupMemory(enforced=False)
    assert gc._read_memory_bound() == (16384, "host")


def test_v2_memory_absent_at_every_level_falls_through_to_v1(fs: dict[str, str]) -> None:
    fs["/fake/v1/limit"] = f"{2 * _GIB}\n"
    fs["/fake/v1/usage"] = f"{1 * _GIB}\n"

    assert gc._read_cgroup_v2_available_mib() is None
    assert gc._read_cgroup_memory() == gc._CgroupMemory(enforced=True, headroom_mib=1024)
    assert gc._read_cgroup_v2_limit_bytes() is None


# --- CPU ---------------------------------------------------------------------


def test_quota_cores_takes_the_tightest_ancestor_quota(fs: dict[str, str]) -> None:
    _put(fs, _LEAF, "cpu.max", "max 100000\n")
    _put(fs, _SLICE, "cpu.max", "200000 100000\n")
    _put(fs, "user.slice", "cpu.max", "400000 100000\n")

    assert gc._read_cgroup_quota_cores() == 2


def test_quota_cores_unlimited_at_every_level_is_zero(fs: dict[str, str]) -> None:
    _put(fs, _LEAF, "cpu.max", "max 100000\n")

    assert gc._read_cgroup_v2_quota_cores() == 0


def test_quota_cores_absent_at_every_level_falls_through_to_v1(fs: dict[str, str]) -> None:
    fs["/fake/v1/quota"] = "300000\n"
    fs["/fake/v1/period"] = "100000\n"

    assert gc._read_cgroup_v2_quota_cores() is None
    assert gc._read_cgroup_quota_cores() == 3


# --- integration ------------------------------------------------------------


def test_measure_gate_slots_on_a_slice_limited_bare_host(fs: dict[str, str]) -> None:
    _put(fs, _SLICE, "memory.max", f"{4 * _GIB}\n")
    fs["/fake/meminfo"] = "MemTotal:       24117248 kB\n"
    cpus = tuple(range(8))
    groups = tuple((c,) for c in cpus)
    with (
        patch.object(gc, "_read_affinity_cpu_ids", return_value=cpus),
        patch.object(gc, "_read_cpu_groups", return_value=groups),
    ):
        plan = gc.measure_gate_slots()

    assert plan == gc.plan_gate_slots(groups, 0, 4096)
    # The slice bound must change the plan, or a regression to the root-only
    # read (falling back to MemTotal) would pass unnoticed.
    assert plan != gc.plan_gate_slots(groups, 0, 23552)


def test_namespace_root_reads_only_the_root_files(fs: dict[str, str]) -> None:
    fs[_PROC] = "0::/\n"
    _put(fs, "", "memory.max", f"{4 * _GIB}\n")
    _put(fs, "", "memory.current", f"{1 * _GIB}\n")
    _put(fs, "", "cpu.max", "300000 100000\n")
    # A stray deeper file must not be consulted under a namespace root.
    _put(fs, _LEAF, "memory.max", f"{1 * _GIB}\n")

    assert gc._read_cgroup_v2_available_mib() == gc._CgroupMemory(
        enforced=True, headroom_mib=3072,
    )
    assert gc._read_cgroup_v2_limit_bytes() == 4 * _GIB
    assert gc._read_cgroup_v2_quota_cores() == 3

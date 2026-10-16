"""Tests for maestro.host_capacity — the four live-host readings, their degrade
branches, and the pure saturation policy (proposal 125).

The pure policy (`saturation_conditions`) and the frozen `HostCapacityReading`
run in-memory with no patching. The impure readers patch their I/O — the
`_MEMINFO_PATH` read, `os.getloadavg`, `shutil.disk_usage`, and
`gate_capacity.host_cores` (for the host-wide core count) — so
there is no real host read, mirroring `tests/test_gate_capacity.py`'s idiom under
the no-real-filesystem rule.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock, patch

import pytest

from maestro import host_capacity as hc

# ---------------------------------------------------------------------------
# HostCapacityReading — the frozen value object
# ---------------------------------------------------------------------------


def test_host_capacity_reading_is_frozen() -> None:
    """A measured reading cannot drift — assigning a field raises."""
    reading = hc.HostCapacityReading(
        load_per_core=0.5,
        memory_used_fraction=0.5,
        swap_used_fraction=0.0,
        docker_root_free_gib=100.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        reading.load_per_core = 1.0  # type: ignore[misc]


def test_host_capacity_reading_is_empty_when_all_fields_none() -> None:
    """An all-None reading is produced-but-empty — it carries no signal."""
    reading = hc.HostCapacityReading(
        load_per_core=None,
        memory_used_fraction=None,
        swap_used_fraction=None,
        docker_root_free_gib=None,
    )
    assert reading.is_empty() is True


def test_host_capacity_reading_is_not_empty_when_any_field_present() -> None:
    """A single readable field makes the reading a measurement, not empty."""
    reading = hc.HostCapacityReading(
        load_per_core=None,
        memory_used_fraction=None,
        swap_used_fraction=None,
        docker_root_free_gib=100.0,  # one source read — not empty
    )
    assert reading.is_empty() is False


# ---------------------------------------------------------------------------
# saturation_conditions — the pure policy
# ---------------------------------------------------------------------------


def test_saturation_conditions_none_reading_is_silent() -> None:
    """A None reading (sensor off, or a snapshot with none) contributes nothing."""
    assert hc.saturation_conditions(None) == []


def test_saturation_conditions_healthy_reading_is_silent() -> None:
    """A reading with every resource below its threshold names nothing."""
    reading = hc.HostCapacityReading(
        load_per_core=0.5,
        memory_used_fraction=0.60,
        swap_used_fraction=0.0,
        docker_root_free_gib=50.0,
    )
    assert hc.saturation_conditions(reading) == []


def test_saturation_conditions_exactly_at_threshold_is_silent() -> None:
    """A reading exactly at each threshold stays silent (strict >/<)."""
    reading = hc.HostCapacityReading(
        load_per_core=1.0,   # == _LOAD_PER_CORE_THRESHOLD
        memory_used_fraction=0.90,  # == _MEMORY_USED_THRESHOLD
        swap_used_fraction=0.10,  # == _SWAP_USED_THRESHOLD
        docker_root_free_gib=5.0,  # == _DOCKER_ROOT_FREE_GIB_THRESHOLD
    )
    assert hc.saturation_conditions(reading) == []


def test_saturation_conditions_saturated_reading_names_each_resource() -> None:
    """A reading over every threshold names all four resources, one condition each."""
    reading = hc.HostCapacityReading(
        load_per_core=2.5,
        memory_used_fraction=0.95,
        swap_used_fraction=0.40,
        docker_root_free_gib=1.0,
    )
    conditions = hc.saturation_conditions(reading)
    assert len(conditions) == 4
    joined = " ".join(conditions)
    assert "load per core" in joined
    assert "memory" in joined
    assert "swap" in joined
    assert "free disk" in joined


def test_saturation_conditions_all_fields_none_is_silent() -> None:
    """An all-unavailable reading names nothing — every field is skipped."""
    reading = hc.HostCapacityReading(
        load_per_core=None,
        memory_used_fraction=None,
        swap_used_fraction=None,
        docker_root_free_gib=None,
    )
    assert hc.saturation_conditions(reading) == []


def test_saturation_conditions_partial_reading_evaluates_the_others() -> None:
    """A None field is skipped while a saturated present field still fires."""
    reading = hc.HostCapacityReading(
        load_per_core=None,  # unavailable — skipped, not a false 0
        memory_used_fraction=0.97,  # saturated — the only condition
        swap_used_fraction=None,
        docker_root_free_gib=None,
    )
    conditions = hc.saturation_conditions(reading)
    assert len(conditions) == 1
    assert "memory" in conditions[0]


# ---------------------------------------------------------------------------
# read_host_capacity — the impure snapshot reader
# ---------------------------------------------------------------------------


def test_read_host_capacity_folds_all_four_readings() -> None:
    """Every source populating yields a fully-populated reading."""
    meminfo = MagicMock()
    meminfo.read_text.return_value = (
        "MemTotal:       8000000 kB\n"
        "MemAvailable:   2000000 kB\n"
        "SwapTotal:      1000000 kB\n"
        "SwapFree:        900000 kB\n"
    )
    with (
        patch("maestro.host_capacity.os.getloadavg", return_value=(6.0, 5.0, 4.0)),
        patch("maestro.host_capacity.gate_capacity.host_cores", return_value=4),
        patch("maestro.host_capacity._MEMINFO_PATH", meminfo),
        patch(
            "maestro.host_capacity.shutil.disk_usage",
            return_value=MagicMock(free=10 * 1024**3),
        ),
    ):
        reading = hc.read_host_capacity()

    # load1 6.0 / 4 cores == 1.5
    assert reading.load_per_core == 1.5
    # 1 - 2000000/8000000 == 0.75
    assert reading.memory_used_fraction == 0.75
    # 1 - 900000/1000000 == 0.10 (rounded exactly)
    assert reading.swap_used_fraction == pytest.approx(0.10)
    # 10 GiB free
    assert reading.docker_root_free_gib == 10.0
    assert reading.docker_root_source == "docker_root"


# ---------------------------------------------------------------------------
# _read_load_per_core — the load reading and its degrade
# ---------------------------------------------------------------------------


def test_read_load_per_core_divides_by_affinity_cores() -> None:
    """The 1-minute average is divided by the affinity-aware core count."""
    with (
        patch("maestro.host_capacity.os.getloadavg", return_value=(3.0, 2.0, 1.0)),
        patch("maestro.host_capacity.gate_capacity.host_cores", return_value=2),
    ):
        assert hc._read_load_per_core() == 1.5


def test_read_load_per_core_ignores_cgroup_cpu_quota() -> None:
    """A CFS quota below the host core count must not inflate host-wide load.

    ``/proc/loadavg`` is the whole host's run queue, so it is normalised by the
    host's 4 cores, not the container's 2-core quota (#4080).
    """
    with (
        patch("maestro.gate_capacity._read_affinity_cores", return_value=4),
        patch("maestro.gate_capacity._read_cgroup_quota_cores", return_value=2),
        patch("maestro.host_capacity.os.getloadavg", return_value=(2.5, 0.0, 0.0)),
    ):
        assert hc._read_load_per_core() == 2.5 / 4


def test_read_load_per_core_getloadavg_raising_degrades_to_none() -> None:
    """os.getloadavg raising OSError degrades the load reading to None."""
    with patch(
        "maestro.host_capacity.os.getloadavg",
        side_effect=OSError("load average unobtainable"),
    ):
        assert hc._read_load_per_core() is None


def test_read_load_per_core_getloadavg_absent_degrades_to_none() -> None:
    """os.getloadavg absent (non-Linux) raises AttributeError → degrades to None.

    On a platform where ``os.getloadavg`` does not exist (e.g. Windows),
    ``os.getloadavg()`` raises ``AttributeError`` rather than ``OSError``.
    The handler catches it too, honoring the module's degrade contract and the
    ``is_empty`` docstring's "non-Linux host with no os.getloadavg" case, instead
    of letting the ``AttributeError`` escape ``read_host_capacity``.
    """
    with patch(
        "maestro.host_capacity.os.getloadavg",
        side_effect=AttributeError(
            "module 'os' has no attribute 'getloadavg'"
        ),
    ):
        assert hc._read_load_per_core() is None


# ---------------------------------------------------------------------------
# _read_memory_and_swap — the /proc/meminfo readings and their degrades
# ---------------------------------------------------------------------------


def test_read_memory_and_swap_parses_both_fractions() -> None:
    """MemTotal/MemAvailable and SwapTotal/SwapFree fold into two fractions."""
    meminfo = MagicMock()
    meminfo.read_text.return_value = (
        "MemTotal:       4000000 kB\n"
        "MemAvailable:   1000000 kB\n"
        "SwapTotal:      2000000 kB\n"
        "SwapFree:       1000000 kB\n"
    )
    with patch("maestro.host_capacity._MEMINFO_PATH", meminfo):
        memory, swap = hc._read_memory_and_swap()
    assert memory == 0.75  # 1 - 1000000/4000000
    assert swap == 0.50    # 1 - 1000000/2000000


def test_read_memory_and_swap_unreadable_meminfo_degrades_both() -> None:
    """An unreadable /proc/meminfo degrades both fractions to None together."""
    meminfo = MagicMock()
    meminfo.read_text.side_effect = OSError("no /proc here")
    with patch("maestro.host_capacity._MEMINFO_PATH", meminfo):
        memory, swap = hc._read_memory_and_swap()
    assert memory is None
    assert swap is None


def test_read_memory_and_swap_absent_memavailable_degrades_memory_only() -> None:
    """An absent MemAvailable field degrades memory to None; swap still reads."""
    meminfo = MagicMock()
    meminfo.read_text.return_value = (
        "MemTotal:       4000000 kB\n"
        "SwapTotal:      2000000 kB\n"
        "SwapFree:       1000000 kB\n"
    )
    with patch("maestro.host_capacity._MEMINFO_PATH", meminfo):
        memory, swap = hc._read_memory_and_swap()
    assert memory is None
    assert swap == 0.50


def test_read_memory_and_swap_absent_swaptotal_degrades_swap_only() -> None:
    """An absent SwapTotal field degrades swap to None; memory still reads."""
    meminfo = MagicMock()
    meminfo.read_text.return_value = (
        "MemTotal:       4000000 kB\n"
        "MemAvailable:   1000000 kB\n"
    )
    with patch("maestro.host_capacity._MEMINFO_PATH", meminfo):
        memory, swap = hc._read_memory_and_swap()
    assert memory == 0.75
    assert swap is None


def test_read_memory_and_swap_zero_swaptotal_reads_zero() -> None:
    """A SwapTotal of 0 (no swap device) reads a genuine 0% residency, not None."""
    meminfo = MagicMock()
    meminfo.read_text.return_value = (
        "MemTotal:       4000000 kB\n"
        "MemAvailable:   1000000 kB\n"
        "SwapTotal:            0 kB\n"
        "SwapFree:             0 kB\n"
    )
    with patch("maestro.host_capacity._MEMINFO_PATH", meminfo):
        memory, swap = hc._read_memory_and_swap()
    assert memory == 0.75
    assert swap == 0.0


def test_read_memory_and_swap_nonnumeric_field_degrades_to_none() -> None:
    """A field line whose value is not digits degrades that fraction to None."""
    meminfo = MagicMock()
    meminfo.read_text.return_value = (
        "MemTotal:       notanumber kB\n"
        "MemAvailable:   1000000 kB\n"
        "SwapTotal:      2000000 kB\n"
        "SwapFree:       1000000 kB\n"
    )
    with patch("maestro.host_capacity._MEMINFO_PATH", meminfo):
        memory, swap = hc._read_memory_and_swap()
    assert memory is None
    assert swap == 0.50


# ---------------------------------------------------------------------------
# _read_docker_root_free_gib — the free-disk reading and its degrade
# ---------------------------------------------------------------------------


def test_read_docker_root_free_gib_reports_gib() -> None:
    """shutil.disk_usage's free bytes fold to GiB."""
    with patch(
        "maestro.host_capacity.shutil.disk_usage",
        return_value=MagicMock(free=20 * 1024**3),
    ):
        assert hc._read_docker_root_free_gib() == (20.0, "docker_root")


def test_read_docker_root_free_gib_prefers_docker_root_over_workspace() -> None:
    """When the Docker root is readable it wins; the workspace is never statted."""
    disk_usage = MagicMock(return_value=MagicMock(free=30 * 1024**3))
    with patch("maestro.host_capacity.shutil.disk_usage", disk_usage):
        assert hc._read_docker_root_free_gib() == (30.0, "docker_root")
    disk_usage.assert_called_once_with(str(hc._DOCKER_ROOT_PATH))


def test_read_docker_root_free_gib_falls_back_to_workspace() -> None:
    """An absent Docker root (the loop container) falls back to the workspace."""

    def _disk_usage(path: str) -> MagicMock:
        if path == str(hc._DOCKER_ROOT_PATH):
            raise FileNotFoundError(path)
        assert path == str(hc._WORKSPACE_FALLBACK_PATH)
        return MagicMock(free=7 * 1024**3)

    with patch("maestro.host_capacity.shutil.disk_usage", side_effect=_disk_usage):
        assert hc._read_docker_root_free_gib() == (7.0, "workspace")


def test_read_docker_root_free_gib_both_missing_degrades_to_none() -> None:
    """Neither the Docker root nor the workspace readable degrades to None."""
    with patch(
        "maestro.host_capacity.shutil.disk_usage",
        side_effect=FileNotFoundError("missing"),
    ):
        assert hc._read_docker_root_free_gib() == (None, None)


def test_read_host_capacity_records_workspace_source() -> None:
    """The fallback's provenance reaches the reading and its saturation text."""

    def _disk_usage(path: str) -> MagicMock:
        if path == str(hc._DOCKER_ROOT_PATH):
            raise FileNotFoundError(path)
        return MagicMock(free=2 * 1024**3)

    with (
        patch("maestro.host_capacity.shutil.disk_usage", side_effect=_disk_usage),
        patch("maestro.host_capacity._read_memory_and_swap", return_value=(None, None)),
        patch("maestro.host_capacity._read_load_per_core", return_value=None),
    ):
        reading = hc.read_host_capacity()

    assert reading.docker_root_free_gib == 2.0
    assert reading.docker_root_source == "workspace"
    assert hc.saturation_conditions(reading) == [
        "Host free disk 2.0 GiB on the Docker root (read via workspace) "
        "is below 5.0 GiB",
    ]


def test_host_capacity_reading_source_alone_is_empty() -> None:
    """A source without a figure carries no signal, so the reading stays empty."""
    reading = hc.HostCapacityReading(
        load_per_core=None,
        memory_used_fraction=None,
        swap_used_fraction=None,
        docker_root_free_gib=None,
        docker_root_source="workspace",
    )
    assert reading.is_empty() is True

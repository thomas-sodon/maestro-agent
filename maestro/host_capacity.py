"""Host-capacity sensor — a stdlib-only reading of how loaded the host is.

The loop runs on a small production VPS it shares with the operator's interactive
shell, the two containerised Maestro services, and incidental desktop services.
``deploy/placement.toml`` now declares who runs where and ``deploy/systemd/``
bounds each unit's CPU/memory, but nothing in the loop *reads* the host it runs
on. Every capacity collision to date — an OOM-killed test worker, a disk filled to
100%, a runnable queue that starved the shell — was discovered only after it had
deformed something. :meth:`maestro.diagnostics.DiagnosticSnapshot.blind_spots`
already names the gaps the snapshot cannot see; host capacity is one of them.

This module is the sensor for that gap. It mirrors the
:mod:`maestro.gate_capacity` skeleton exactly — module-level :class:`~pathlib.Path`
constants for the kernel files, a pure/impure split, and a narrow ``except
OSError`` around every read (never a broad handler, so it is honest with
``tests/test_no_silenced_exceptions.py`` *and* exempt by construction) — but reads
four resources rather than two:

* **load per core** — ``os.getloadavg()[0]`` (host-wide) over the host's
  affinity/nproc-equivalent core count (:func:`maestro.gate_capacity.host_cores`),
  deliberately not the CFS-quota-clamped gate-worker count;
* **memory used fraction** — ``1 - MemAvailable/MemTotal`` from ``/proc/meminfo``;
* **swap used fraction** — ``1 - SwapFree/SwapTotal`` from ``/proc/meminfo``;
* **free disk on the Docker root** — ``shutil.disk_usage(/var/lib/docker).free``,
  falling back to the ``/workspace`` bind mount when the Docker root is not
  visible (inside the loop container, which never mounts the host Docker root);
  :attr:`HostCapacityReading.docker_root_source` records which path answered.

The one impure reader :func:`read_host_capacity` fills a frozen
:class:`HostCapacityReading` whose four fields are each ``float | None``: ``None``
means *that* reading was unavailable while the others still stand. The one pure
policy :func:`saturation_conditions` turns a reading into zero or more
human-readable strings naming each saturated resource, comparing with strict
``>`` / ``<`` so a reading exactly at a threshold stays silent (matching the
residency convention in :mod:`maestro.health_check`).

The four thresholds are *measured*, not invented: load per core above ``1.0`` (the
runnable queue at or past the core count — the ``ldavg-1 < nproc`` anchor the
placement work uses), memory used above ``0.90``, swap used above ``0.10``, and
free disk below ``5.0`` GiB (the ``MIN_FREE_KB`` default the deploy disk guard in
``scripts/safe-deploy.sh`` already encodes, reused rather than re-chosen). Turning
the sensor on and calibrating these against real saturation events is a separate,
reversible ``[loop] host_capacity_sensor_enabled`` flip — this module only
provides the reading, and it is silent until that flag is set.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from maestro import gate_capacity

#: ``/proc/meminfo`` — the kernel file whose ``MemTotal`` / ``MemAvailable`` /
#: ``SwapTotal`` / ``SwapFree`` lines report memory and swap residency. Named once
#: so the reader and its tests agree on the path.
#: :func:`maestro.gate_capacity._read_available_mib` parses only ``MemAvailable``
#: behind a documented ``0`` sentinel that cannot express "unreadable", so this
#: module owns a general field parse rather than reusing that narrower reader.
_MEMINFO_PATH = Path("/proc/meminfo")

#: The conventional Docker data root — the same ``/var/lib/docker`` fallback
#: ``scripts/safe-deploy.sh`` uses when ``docker info`` cannot report the real one.
#: A relocated data-root raises :class:`FileNotFoundError` (an ``OSError``) and the
#: reading degrades to ``None``/unreadable rather than to a wrong number.
_DOCKER_ROOT_PATH = Path("/var/lib/docker")

#: The loop container's workspace bind mount (``.:/workspace`` in
#: ``docker-compose.yml``) — the free-disk fallback when :data:`_DOCKER_ROOT_PATH`
#: is absent, as it always is inside the container. The mount sits on the host
#: root filesystem, which on this VPS is also where the Docker root lives, so its
#: free space is the same figure. The host Docker root is deliberately *not*
#: bind-mounted into the container: that would expose every container's layers.
_WORKSPACE_FALLBACK_PATH = Path("/workspace")

#: Which path produced :attr:`HostCapacityReading.docker_root_free_gib`.
DiskSource = Literal["docker_root", "workspace"]

#: Load per core above which the runnable queue is at or past the core count — the
#: same ``ldavg-1 < nproc`` anchor the placement work uses. Strict ``>`` keeps a
#: reading exactly at the threshold silent.
_LOAD_PER_CORE_THRESHOLD = 1.0

#: Memory used fraction above which memory is saturated (headroom nearly gone).
_MEMORY_USED_THRESHOLD = 0.90

#: Swap used fraction above which the host is swapping enough to matter — any
#: sustained swap on this box has historically preceded a stall.
_SWAP_USED_THRESHOLD = 0.10

#: Free disk on the Docker root, in GiB, below which disk is saturated — the
#: ``MIN_FREE_KB`` default (5 GiB) from ``scripts/safe-deploy.sh``, reused not
#: re-chosen. Strict ``<`` keeps a reading exactly at the threshold silent.
_DOCKER_ROOT_FREE_GIB_THRESHOLD = 5.0

#: Bytes per GiB — the divisor turning ``shutil.disk_usage(...).free`` into GiB.
_BYTES_PER_GIB = 1024**3


@dataclass(frozen=True)
class HostCapacityReading:
    """A measured reading of the host a loop iteration is running on.

    Each field is ``float | None``: ``None`` means *that* reading was unavailable
    (an unreadable ``/proc/meminfo``, an absent field, ``os.getloadavg`` raising,
    a relocated Docker root) while the others still stand — the sensor never
    collapses a partial read into a single "unknown". Frozen so a snapshot cannot
    drift after it is taken; :func:`read_host_capacity` fills it from the live
    host and :func:`saturation_conditions` takes it as an argument and stays pure.
    """

    load_per_core: float | None
    memory_used_fraction: float | None
    swap_used_fraction: float | None
    docker_root_free_gib: float | None
    #: Which path produced :attr:`docker_root_free_gib` — ``"docker_root"`` for
    #: the conventional Docker root, ``"workspace"`` for the container's
    #: workspace-mount fallback, ``None`` when neither was readable. Provenance
    #: only: it carries no signal of its own, so :meth:`is_empty` ignores it.
    docker_root_source: DiskSource | None = None

    def is_empty(self) -> bool:
        """Return ``True`` when every field is ``None`` — nothing was read.

        :func:`read_host_capacity` always constructs a reading, even when all
        four sources fail at once (a non-Linux host with no ``os.getloadavg``,
        an unreadable ``/proc/meminfo``, and neither the Docker root nor the
        workspace mount readable together),
        yielding an all-``None`` object that carries no signal. Such a reading
        is *produced-but-empty*, not a measurement, so a caller naming
        measurement gaps (see
        :meth:`maestro.diagnostics.DiagnosticSnapshot.blind_spots`) treats it as
        equivalent to no reading at all and still names the host-capacity gap,
        rather than reading the host as measured-and-healthy on no observation.
        """
        return (
            self.load_per_core is None
            and self.memory_used_fraction is None
            and self.swap_used_fraction is None
            and self.docker_root_free_gib is None
        )


def _parse_meminfo_kib(text: str, field: str) -> int | None:
    """Return the KiB value of a ``/proc/meminfo`` *field* line, or ``None``.

    ``/proc/meminfo`` reports each figure in KiB as ``Field:   <count> kB``. A
    missing field (absent on very old kernels, or simply not present) and a
    non-numeric value both return ``None`` so the caller can express "that reading
    was unavailable" rather than guess a number. Pure over its *text* argument.
    """
    prefix = f"{field}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
            return None
    return None


def _used_fraction(total: int | None, free: int | None) -> float | None:
    """Return ``1 - free/total`` as a used fraction, or ``None`` when unknown.

    ``None`` when either field was absent/unparseable (:func:`_parse_meminfo_kib`
    returned ``None``); ``0.0`` when *total* is zero or negative — the genuine "no
    swap device configured" reading (a real 0% residency, not an unknown) — which
    also guards the division. Pure.
    """
    if total is None or free is None:
        return None
    if total <= 0:
        return 0.0
    return 1.0 - free / total


def _read_load_per_core() -> float | None:
    """Return the 1-minute load average per core, or ``None``.

    ``os.getloadavg()[0]`` is the 1-minute runnable-plus-uninterruptible average;
    ``/proc/loadavg`` is not namespaced, so it is the *host's* run queue and is
    divided by :func:`maestro.gate_capacity.host_cores` — the affinity/
    nproc-equivalent host count (never below one, so the division is safe), not
    the CFS-quota-clamped ``measure_host_capacity().cores`` gate-worker count,
    which would inflate the reading on a quota-limited container. ``os.getloadavg``
    raises :class:`OSError` when the average is unobtainable, and is *absent*
    entirely on a non-Linux host (``os.getloadavg()`` then raises
    :class:`AttributeError`) — both degrade to ``None``, honoring the module's
    "each reading degrades to ``None`` on its own if its source is unavailable"
    contract and the ``is_empty`` docstring's "non-Linux host with no
    ``os.getloadavg``" case. Both are narrow named types, so
    ``tests/test_no_silenced_exceptions.py`` still exempts the handler by
    construction. Impure: reads the scheduler.
    """
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        return None
    return load1 / gate_capacity.host_cores()


def _read_memory_and_swap() -> tuple[float | None, float | None]:
    """Return ``(memory_used_fraction, swap_used_fraction)`` from ``/proc/meminfo``.

    One parse of ``/proc/meminfo`` yields both. An unreadable file (a narrow
    :class:`OSError`, never a blind ``except``) degrades *both* fractions to
    ``None`` together, since the single read failed. Impure: reads the kernel.
    """
    try:
        text = _MEMINFO_PATH.read_text(encoding="utf-8")
    except OSError:
        return None, None
    memory_used = _used_fraction(
        _parse_meminfo_kib(text, "MemTotal"),
        _parse_meminfo_kib(text, "MemAvailable"),
    )
    swap_used = _used_fraction(
        _parse_meminfo_kib(text, "SwapTotal"),
        _parse_meminfo_kib(text, "SwapFree"),
    )
    return memory_used, swap_used


def _read_docker_root_free_gib() -> tuple[float | None, DiskSource | None]:
    """Return ``(free GiB, source)`` for the Docker data root, or ``(None, None)``.

    The conventional ``/var/lib/docker`` wins when it exists. When it does not —
    always the case inside the loop container, which mounts only the workspace —
    ``shutil.disk_usage`` raises :class:`FileNotFoundError` (an ``OSError``) and
    the reading falls back to :data:`_WORKSPACE_FALLBACK_PATH`, which lives on the
    same host root filesystem. Only when both are unreadable does it degrade to
    ``None`` rather than to a wrong number for some unrelated mount. The second
    element names which path answered. Impure: stats the filesystem.
    """
    try:
        free_bytes = shutil.disk_usage(str(_DOCKER_ROOT_PATH)).free
    except OSError:
        try:
            free_bytes = shutil.disk_usage(str(_WORKSPACE_FALLBACK_PATH)).free
        except OSError:
            return None, None
        return free_bytes / _BYTES_PER_GIB, "workspace"
    return free_bytes / _BYTES_PER_GIB, "docker_root"


def read_host_capacity() -> HostCapacityReading:
    """Snapshot the live host into a :class:`HostCapacityReading`.

    Each of the four readings degrades to ``None`` on its own if its source is
    unavailable, so a partial host (no ``/proc/meminfo``, a relocated Docker root)
    still yields the readings it can take. Impure: the four readings are this
    function's reason to exist.
    """
    memory_used, swap_used = _read_memory_and_swap()
    disk_free, disk_source = _read_docker_root_free_gib()
    return HostCapacityReading(
        load_per_core=_read_load_per_core(),
        memory_used_fraction=memory_used,
        swap_used_fraction=swap_used,
        docker_root_free_gib=disk_free,
        docker_root_source=disk_source,
    )


def saturation_conditions(reading: HostCapacityReading | None) -> list[str]:
    """Return human-readable strings naming each saturated resource in *reading*.

    Pure, zero I/O. A ``None`` *reading* — the sensor disabled, or a snapshot that
    carries none — returns ``[]``, so a caller folding this into a condition list
    (see :func:`maestro.health_check.summarize_health`) stays byte-identical to
    today when no reading is present. A ``None`` *field* is skipped so an
    unavailable reading never manufactures a false saturation. Comparisons are
    strict (``>`` / ``<``) so a resource exactly at its threshold stays silent.
    """
    if reading is None:
        return []
    conditions: list[str] = []
    load = reading.load_per_core
    if load is not None and load > _LOAD_PER_CORE_THRESHOLD:
        conditions.append(
            f"Host load per core {load:.2f} exceeds {_LOAD_PER_CORE_THRESHOLD:.2f} "
            "(runnable queue at or past the core count)"
        )
    memory = reading.memory_used_fraction
    if memory is not None and memory > _MEMORY_USED_THRESHOLD:
        conditions.append(
            f"Host memory {memory:.0%} used exceeds {_MEMORY_USED_THRESHOLD:.0%}"
        )
    swap = reading.swap_used_fraction
    if swap is not None and swap > _SWAP_USED_THRESHOLD:
        conditions.append(
            f"Host swap {swap:.0%} used exceeds {_SWAP_USED_THRESHOLD:.0%}"
        )
    disk = reading.docker_root_free_gib
    if disk is not None and disk < _DOCKER_ROOT_FREE_GIB_THRESHOLD:
        conditions.append(
            f"Host free disk {disk:.1f} GiB on the Docker root "
            f"(read via {reading.docker_root_source or 'unknown'}) is below "
            f"{_DOCKER_ROOT_FREE_GIB_THRESHOLD:.1f} GiB"
        )
    return conditions

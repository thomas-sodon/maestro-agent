"""Gate execution lease — the single owner of how much of the host a gate run
may take, and whether it may start right now.

Quality-gate runs currently assume they own the whole machine. ``pytest -n auto``
resolves its worker count from a static topology read (physical cores), never
from *available* capacity or from how many other gate runs are already in flight,
so several concurrent full-width suites oversubscribe a small box: the runnable
queue climbs, free memory collapses, and the wall-clock test timeout plus the
100%-coverage floor then *manufacture* red verdicts out of pure contention. The
fact nobody owns today is: **how many workers may this gate run use, and may it
start now?**

This module is that owner. It answers the first half with a pure policy
(:func:`plan_lease`) that derives a bounded worker grant plus a concurrency slot
budget from measured host constants and the observed number of in-flight runs,
and the second half with one confined counting-semaphore seam
(:func:`acquire_slot`) that admits a run against a fixed number of slots and
*fails open* under saturation, so a busy host slows a gate rather than
deadlocking it.

The policy is pure — zero I/O, no subprocess: the caller supplies the measured
:class:`HostCapacity`, so :func:`plan_lease` and the two frozen value objects
touch no filesystem, mirroring the :mod:`maestro.gate_observation` /
:mod:`maestro.plan_grounding` skeleton (a ``Literal`` closed vocabulary,
``@dataclass(frozen=True)`` value objects, module-level pure ``def``s). The lone
impurity is :func:`acquire_slot`, the ``fcntl.flock`` seam whose shape is
borrowed from :func:`maestro.fileutil.file_lock` — but as a *counting* semaphore
(``LOCK_EX | LOCK_NB`` probed across N adjacent lock files) rather than a
blocking mutex.

Two impure *readers* fill the pure policy's inputs from the live host:
:func:`measure_host_capacity` snapshots the core count and available memory into
a :class:`HostCapacity` — each the *tighter* of the host figure and the cgroup
limit, so a run inside a memory-/cpu-limited container sizes to the container,
not the host it shares — and :func:`count_in_flight` probes the slot set to count
how many gate runs are already admitted (the ``in_flight`` argument
:func:`plan_lease` needs). They are reachable through the ``maestro gate-lease``
exec wrapper and ``maestro gate-classify`` CLI surfaces behind
``[loop] gate_lease_enabled``. The shell runner (``scripts/quality-gates.sh``)
and the three hand-run command docs route their core suite through that wrapper,
and the in-process loop-side gate readers route through the shared
:func:`lease_for_gate_run` seam here — ``EvolutionEngine.run_quality_gates``,
``gate_check.run_gate_check``, and ``PrRemediationController.preflight`` each hold
one slot for their gate sequence and run ``pytest`` at the granted width
(:func:`worker_args`), behind the same flag and byte-identical when it is off.

Above the lease sits the host-wide gate *slot* layout: the pure
:func:`plan_gate_slots` divides the usable CPUs into CPU-disjoint slots sized by
both cores and total memory, and :func:`measure_gate_slots` reads the live host
for ``scripts/gate-slot.sh``, which pins each concurrent gate run to its slot. A
pinned run scopes its lease slot files by its CPU set (:func:`_slot_scope`), so
two core-disjoint gate slots never serialise on one lease lock.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import re
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Literal

from maestro.fileutil import file_lock
from maestro.gate_environment import (
    GateRunnabilityVerdict,
    declines_gate_run,
    read_verdict,
)
from maestro.gate_observation import GateObservationKind

logger = logging.getLogger(__name__)

#: Resident footprint, in MiB, of one ``pytest-xdist`` worker in steady state —
#: the sizing figure the memory-bound worker budget divides by. Measured at
#: 250-380 MiB per worker on the reference host; rounded to a conservative
#: ceiling so the grant errs toward under- rather than over-subscription.
PER_WORKER_MIB = 350

#: Resident baseline, in MiB, of one idle interactive session holding no suite —
#: subtracted once per *other* in-flight run, since each concurrent run keeps a
#: session resident whether or not its workers are spawned. Measured at ~300 MiB.
SESSION_BASELINE_MIB = 300

#: Memory, in MiB, held back for the operating system and the non-agent system
#: baseline (the dashboard, background services) so a full grant never starves
#: the host. Measured at ~1100 MiB of non-agent baseline on the reference host.
SYSTEM_RESERVE_MIB = 1100

#: How a gate run was admitted. ``"slot"`` — a free slot was held for the body's
#: lifetime; ``"fallback"`` — every slot was occupied for the whole bounded wait,
#: so the run proceeds holding no slot (fail-open). Those two are
#: :func:`acquire_slot`'s outcomes. ``"declined"`` is the third and is decided
#: *before* :func:`acquire_slot` is ever reached: the cached runnability verdict
#: says the local gate cannot reach a verdict at all, so the run is refused
#: without reading the host, taking a slot, or spawning a subprocess. Kept a
#: ``Literal`` so a new outcome is a one-line vocabulary edit.
LeaseAdmission = Literal["slot", "fallback", "declined"]

#: Which of the two memory readings produced a :class:`HostCapacity`'s
#: ``available_mib`` — ``"host"`` for ``/proc/meminfo``'s ``MemAvailable``,
#: ``"cgroup"`` for the tightest ``memory.max - memory.current`` headroom along
#: the process's own cgroup path — a container's root, or an ancestor slice such
#: as ``user-<uid>.slice`` on a bare host. The distinction is load-bearing for
#: :data:`SYSTEM_RESERVE_MIB`: the reserve is a *host* measurement of a baseline
#: that lives outside the bounding cgroup, so subtracting it from a
#: cgroup-bounded figure double-counts what that cgroup's limit already excludes. Kept a ``Literal`` so a third bound is
#: a one-line vocabulary edit.
MemoryBound = Literal["host", "cgroup"]

#: How an admitted gate run ended, as recorded in the telemetry trace. The whole
#: :data:`~maestro.gate_observation.GateObservationKind` vocabulary (``CLEAN`` /
#: the reds / the two non-answers) plus an ``"UNKNOWN"`` sentinel for a caller
#: that holds no classification (e.g. a gate that raised before it could be
#: graded). Built *from* ``GateObservationKind`` rather than restated, so a new
#: gate kind is a one-line edit there and this vocabulary follows.
GateRunOutcome = GateObservationKind | Literal["UNKNOWN"]

#: Which caller produced a telemetry row. ``"in_process"`` is the loop's own
#: ``run_quality_gates`` sequence — the *only* rows a deadline derivation may read,
#: since they are the runs a granted ``deadline_seconds`` was actually enforced on.
#: Every other caller names itself so it cannot be mistaken for one: ``"preflight"``
#: and ``"gate_check"`` are the sibling in-process readers
#: (``loop.pr_remediation.preflight``, ``gate_check.run_gate_check``), which route
#: through this seam for the *width* but spend the flat ``selfcheck`` constant, and
#: ``"wrapper"`` is a hand or shell run through ``maestro gate-lease``, which
#: enforces no deadline and runs whatever argv the caller gave. A row from any of
#: the three says nothing about what the budgeted in-process suite needs, and
#: letting one land as ``"in_process"`` would reset a converging escalation to the
#: floor. Kept a ``Literal`` so a further source is a one-line vocabulary edit.
GateRunSource = Literal["in_process", "preflight", "gate_check", "wrapper"]

#: What suite a gate run's ``tests_seconds`` measured. ``"full"`` is the whole
#: test suite — every row written today, since nothing narrows what a gate run
#: executes — and ``"impacted"`` names a run narrowed to the tests a change can
#: reach. The distinction is load-bearing for the budget: :func:`derive_deadline`
#: reads a row's ``tests_seconds`` as *the cost of the suite*, so folding a
#: narrowed run's (much smaller) cost into that derivation would hand the next
#: full run a budget the suite cannot fit inside, and the kill/escalate
#: oscillation the retained budget exists to prevent would re-open. Kept a
#: ``Literal`` so a further scope is a one-line vocabulary edit.
GateScope = Literal["full", "impacted"]

# Seconds a contended acquisition spends re-probing the slot set before it fails
# open — long enough to ride out a brief burst, short enough that a saturated
# host is only slowed, never deadlocked.
_ACQUIRE_WAIT_SECONDS = 30.0

# Seconds a contended acquisition waits under ``[loop]
# gate_lease_admission_cap_enabled``. The capped budget admits only a couple of
# runs at once, so a queued run must be able to outlast one running suite — the
# median uncontended full run takes roughly 170-215s. With the 30s wait above,
# every waiter would fall open into an uncapped run and the cap would do nothing.
# Still bounded and still fail-open, so a saturated host is never deadlocked.
_CAPPED_ACQUIRE_WAIT_SECONDS = 600.0

# Gate-run sources the admission cap never binds: both run inside the loop
# process, called synchronously from async code, so the capped 600s queue would
# block the event loop (and the one-slot budget a four-core loop container caps
# to would push them onto the one-worker fail-open floor).
_UNCAPPED_SOURCES: frozenset[str] = frozenset({"in_process", "preflight"})

# Narrowest width an admitted run should be guaranteed once its peers take their
# share of the cores: under the admission cap the slot budget is
# ``cores // MIN_WORKERS_PER_ADMITTED_RUN`` (floored at one).
MIN_WORKERS_PER_ADMITTED_RUN = 4

# Seconds slept between slot-set re-probes during the bounded wait above.
_ACQUIRE_PROBE_INTERVAL_SECONDS = 0.5


@dataclass(frozen=True)
class HostCapacity:
    """Measured capacity of the host a gate run is about to contend for.

    ``cores`` is the physical core count and ``available_mib`` the currently
    available memory in MiB — both *measured*, never assumed. ``bounded_by``
    records *which* reading won that memory figure (:data:`MemoryBound`), so
    :func:`plan_lease` can tell a container's headroom from the host's free
    memory and apply the host reserve only to the latter; it defaults to the
    conservative ``"host"``, the reading that retains the reserve. Frozen so a
    lease is planned against a stable snapshot. :func:`measure_host_capacity`
    fills this from the live host; :func:`plan_lease` takes it as an argument and
    stays pure.
    """

    cores: int
    available_mib: int
    bounded_by: MemoryBound = "host"


@dataclass(frozen=True)
class GateLease:
    """The grant :func:`plan_lease` hands a gate run.

    ``workers`` is the bounded worker count the run may use (``1..cores``);
    ``slots`` is the concurrency budget — how many gate runs the host admits at
    once, so :func:`acquire_slot` probes exactly this many lock files. Frozen so
    a planned grant cannot drift between planning and use.

    ``deadline_seconds`` is the *budget* half of the grant (proposal 134): the
    seconds the run's ``pytest`` command may take, derived by
    :func:`derive_deadline` from what the telemetry ledger measured at this same
    granted width. It is filled by :func:`lease_for_gate_run`, not by the pure
    :func:`plan_lease` — a deadline is a fact about history, not about capacity —
    and defaults to ``0``, the "no budget granted" sentinel every reader resolves
    through :func:`deadline_or_default`, so every pre-existing ``GateLease(...)``
    literal still constructs.
    """

    workers: int
    slots: int
    deadline_seconds: int = 0


def plan_lease(
    capacity: HostCapacity,
    in_flight: int,
    *,
    cgroup_reserve: bool = False,
    admission_cap: bool = False,
) -> GateLease:
    """Derive a bounded worker grant and slot budget for one gate run.

    The worker grant is the smaller of the *memory-bound* budget — the headroom
    left after :data:`SYSTEM_RESERVE_MIB` and the resident baselines of the
    ``in_flight`` other runs, over one worker's :data:`PER_WORKER_MIB` footprint —
    and the *core-bound* budget, clamped to ``[1, cores]``. It is therefore
    monotonically non-increasing as ``in_flight`` rises, saturates at a floor of
    one worker, and never returns zero, a negative, or more workers than cores.

    The core-bound budget is ``capacity.cores`` split evenly across the runs
    sharing the host — this run plus its ``in_flight`` peers — so the *aggregate*
    worker demand across concurrent runs degrades toward the core count instead
    of each admitted run independently claiming the whole width. Without that
    split a big-memory host lets ``slots`` (== ``cores``) concurrent runs each
    take ``cores`` workers — ``cores x cores`` in aggregate — which is the
    oversubscription the lease exists to prevent (proposal 126 promises the lease
    "caps the total worker count"). The divisor is floored at one so a negative
    ``in_flight`` never divides by zero or a non-positive quantity. Because each
    run plans once at admission and a launch-time-fixed ``pytest`` worker count
    cannot shrink afterwards, staggered starts (an early run planning full width
    before a later one joins) can still momentarily exceed the core count; the
    split bounds the steady state, not every transient.

    The slot budget is ``capacity.cores`` — at most ``cores`` runs admitted at
    once. It is deliberately independent of ``in_flight`` so every concurrent
    caller probes the *same* fixed set of slot files. *admission_cap* (the
    ``[loop] gate_lease_admission_cap_enabled`` opt-in) shrinks it to
    ``max(1, cores // MIN_WORKERS_PER_ADMITTED_RUN)``: splitting the worker
    grant alone caps total workers but not wall-clock contention, which times out
    the per-test ``pytest-timeout`` budget, so only a few runs are admitted at
    once and the rest queue. The worker grant is unaffected by the flag.

    *cgroup_reserve* (the ``[loop] gate_lease_cgroup_reserve_enabled`` opt-in)
    makes the host reserve bound-aware: with it ``True`` *and*
    ``capacity.bounded_by == "cgroup"``, :data:`SYSTEM_RESERVE_MIB` is omitted,
    because it measures a host baseline living *outside* the cgroup that
    ``memory.current`` has already charged — subtracting it from a container's
    headroom double-counts it and floors the grant at one worker. The
    ``in_flight`` session-baseline term is kept for *both* bounds: those peers
    planned at admission and may not have spawned their workers yet, so their
    residency is not yet reflected in either reading. At the default ``False``
    this function is byte-identical to its pre-flag behaviour on every path.

    Absurd or degenerate capacity (non-positive cores or memory) clamps to the
    floor — one worker, one slot — rather than dividing by or against a
    non-positive quantity. Pure, zero I/O: the caller supplies the measured
    capacity.
    """
    if capacity.cores <= 0 or capacity.available_mib <= 0:
        return GateLease(workers=1, slots=1)
    reserve = (
        0
        if cgroup_reserve and capacity.bounded_by == "cgroup"
        else SYSTEM_RESERVE_MIB
    )
    memory_budget = (
        capacity.available_mib - reserve - in_flight * SESSION_BASELINE_MIB
    ) // PER_WORKER_MIB
    # This run plus its in-flight peers share the cores; floor the divisor at one
    # so a negative in_flight cannot divide by zero or a non-positive quantity.
    runs_sharing = max(1, in_flight + 1)
    core_budget = capacity.cores // runs_sharing
    workers = max(1, min(memory_budget, core_budget))
    slots = (
        max(1, capacity.cores // MIN_WORKERS_PER_ADMITTED_RUN)
        if admission_cap
        else capacity.cores
    )
    return GateLease(workers=workers, slots=slots)


def uncontended_width(capacity: HostCapacity, *, cgroup_reserve: bool) -> int:
    """Return the width a gate run would be granted if it started now.

    The same derivation :func:`lease_for_gate_run` spends post-admission, taken at
    ``in_flight=0`` — the single owner of the uncontended width the runnability
    oracle judges ``gate_runs.jsonl`` rows against, so the oracle can never plan a
    width no real run records. *cgroup_reserve* is required (no default) so no
    caller can silently get reserve-off semantics while
    ``[loop] gate_lease_cgroup_reserve_enabled`` is up.
    """
    return plan_lease(capacity, 0, cgroup_reserve=cgroup_reserve).workers


def worker_args(lease: GateLease | None) -> tuple[str, ...]:
    """Return the ``pytest-xdist`` argv atoms for *lease*'s granted width, or ``()``.

    Pure, zero-I/O. Returns ``()`` for a ``None`` lease — the flag-off rollback,
    so a routed reader's ``pytest`` argv stays byte-identical to today — *and* for
    a one-worker grant, since a single worker buys no parallelism yet pays for a
    worker process, so a floor grant runs serially rather than under ``-n 1``.
    Otherwise returns ``("-n", str(lease.workers), "--dist", "loadfile")``. Keeping
    ``-n`` and ``--dist loadfile`` paired in one function means the scheduler pin
    can never be separated from the width at any reader that splices these atoms
    into its command.
    """
    if lease is None or lease.workers <= 1:
        return ()
    return ("-n", str(lease.workers), "--dist", "loadfile")


def _hold_first_free_slot(lock_dir: Path, slots: int) -> int | None:
    """Return an fd holding an exclusive lock on the first free slot, or ``None``.

    Probes slot files ``0..slots-1`` with ``LOCK_EX | LOCK_NB``. A
    :class:`BlockingIOError` is the documented "slot busy" signal — explicit
    control flow, not a silenced error — so its probe fd is closed and the next
    slot tried. Any other error from ``fcntl.flock`` (e.g. ``OSError``/``ENOLCK``
    on a filesystem without lock support) also closes the just-opened probe fd
    before propagating, so an unexpected failure never leaks a descriptor. On
    the first free slot the still-open, still-locked fd is returned; the caller
    owns releasing and closing it.

    Leases held in *overlapping* scopes (:func:`_foreign_held`) share these CPUs,
    so they use up the same budget. They are read only *after* an own-scope slot
    is held: of two overlapping runs starting together, at least the second sees
    the first, so neither check-then-act window lets both in. When any is found,
    every other own-scope slot is counted too (a same-scope peer may hold any
    index, not just the low ones); if the total with this run exceeds *slots*, the
    slot is released and ``None`` returned so the caller keeps waiting. With no
    overlapping lease the path is exactly the single-scope one.
    """
    scope = _slot_scope()
    for index in range(slots):
        slot_path = _slot_lock_path(lock_dir, scope, index)
        fd = os.open(str(slot_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        except BaseException:
            os.close(fd)
            raise
        foreign = _foreign_held(lock_dir, scope)
        if foreign:
            own_others = sum(
                _probe_held(_slot_lock_path(lock_dir, scope, other))
                for other in range(slots) if other != index
            )
            if foreign + own_others + 1 > slots:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
                return None
        return fd
    return None


@contextmanager
def acquire_slot(
    lock_dir: Path, slots: int, *, wait_seconds: float = _ACQUIRE_WAIT_SECONDS,
) -> Iterator[LeaseAdmission]:
    """Admit a gate run against a fixed budget of *slots* concurrent slots.

    Probes ``slots`` adjacent lock files under *lock_dir* with a non-blocking
    exclusive ``fcntl.flock`` (:func:`_hold_first_free_slot`). The first free
    slot is held for the body's lifetime and the manager yields ``"slot"``; the
    lock is released and the fd closed on exit, even when the body raises (a
    nested ``try/finally`` mirroring :func:`maestro.fileutil.file_lock`). A
    holder that is killed has its slot reclaimed by the OS when its fd closes.

    When every slot is occupied, it waits a bounded *wait_seconds* (default
    :data:`_ACQUIRE_WAIT_SECONDS`; a ``time.monotonic`` deadline, logging once at
    INFO when the first probe finds every slot busy so a queued run reads as
    waiting rather than hung, re-probing every
    :data:`_ACQUIRE_PROBE_INTERVAL_SECONDS`) and then **fails open**: it yields
    ``"fallback"`` holding no slot, so a saturated host slows this gate rather
    than deadlocking it. This is the only impure symbol in the module.

    *lock_dir* is created (``exist_ok``) on entry, so a caller need not pre-make
    it. A non-positive *slots* is a programmer error — it would otherwise probe
    no lock files and burn the whole bounded wait before failing open — so it
    raises :class:`ValueError` loudly before any lock file is touched or any
    wait begins.
    """
    if slots < 1:
        raise ValueError(f"acquire_slot needs at least one slot, got {slots!r}")
    os.makedirs(str(lock_dir), exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    logged_wait = False
    while True:
        held_fd = _hold_first_free_slot(lock_dir, slots)
        if held_fd is not None:
            try:
                yield "slot"
            finally:
                try:
                    fcntl.flock(held_fd, fcntl.LOCK_UN)
                finally:
                    os.close(held_fd)
            return
        if not logged_wait:
            logger.info(
                "All %d gate slots busy; waiting up to %.0fs for one to free",
                slots, wait_seconds,
            )
            logged_wait = True
        if time.monotonic() >= deadline:
            yield "fallback"
            return
        time.sleep(_ACQUIRE_PROBE_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# The live-host readers — the two impurities that fill plan_lease's inputs.
# ---------------------------------------------------------------------------

#: The kernel file whose ``MemAvailable`` line reports how much memory a new
#: allocation can claim without swapping — the figure a memory-bound worker grant
#: must divide, not total or free memory. Named once so the reader and its tests
#: agree on the path. Inside a container this reports the *host's* free memory,
#: not the cgroup's headroom, so it is only one of the two candidates
#: :func:`_read_available_mib` takes the tighter of.
_MEMINFO_PATH = Path("/proc/meminfo")

#: Bytes per MiB — the divisor turning a cgroup byte count into the MiB the rest
#: of the policy speaks in.
_MIB_BYTES = 1024 * 1024

#: cgroup v2 unified-hierarchy memory files: ``memory.max`` is the enforced hard
#: limit (the literal ``max`` when unlimited) and ``memory.current`` the live
#: charge; their difference is the headroom a gate run may claim.
_CGROUP_V2_MEMORY_MAX_PATH = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V2_MEMORY_CURRENT_PATH = Path("/sys/fs/cgroup/memory.current")

#: This process's cgroup membership; its ``0::<path>`` line names the v2 cgroup
#: whose ancestors (up to :data:`_CGROUP_V2_ROOT`) may carry the binding limit.
_PROC_SELF_CGROUP_PATH = Path("/proc/self/cgroup")

#: The cgroup v2 unified-hierarchy mount root the ``0::`` path is relative to.
_CGROUP_V2_ROOT = Path("/sys/fs/cgroup")

#: cgroup v1 memory-controller limit / usage files (the pre-unified layout).
_CGROUP_V1_MEMORY_LIMIT_PATH = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
_CGROUP_V1_MEMORY_USAGE_PATH = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")

#: A cgroup v1 ``memory.limit_in_bytes`` at or above this is the kernel's
#: "unlimited" sentinel (``PAGE_COUNTER_MAX`` rounded to a page), never a real
#: container cap — no realistic limit approaches 2**62 bytes.
_CGROUP_UNLIMITED_BYTES = 1 << 62


@dataclass(frozen=True)
class _CgroupMemory:
    """A cgroup memory reading: whether a cap is enforced, and its headroom.

    Private to this module. ``enforced=False`` means *this hierarchy binds
    nothing* — no cap, or an unreadable usage file — so the host figure governs.
    ``enforced=True`` carries a measured ``headroom_mib``, which may legitimately
    be ``0`` when ``memory.current`` has reached ``memory.max``; keeping the two
    facts apart is what stops an exhausted container limit reading as "no limit".
    """

    enforced: bool
    headroom_mib: int = 0

    def __post_init__(self) -> None:
        """Reject a negative headroom at construction — every caller, not just the readers.

        A negative figure would silently reverse the ``<`` tie-break in
        :func:`_read_memory_bound`, making an exhausted cgroup look *looser* than
        the host. Raising (rather than asserting) keeps the guard under ``-O``,
        and siting it on the constructor binds a future caller too.
        """
        if self.headroom_mib < 0:
            raise ValueError(f"headroom_mib must be non-negative, got {self.headroom_mib}")


#: cgroup v2 CPU bandwidth file: ``"<quota> <period>"`` microseconds, or
#: ``"max <period>"`` when no quota is enforced. The quota/period ratio is the
#: fractional-core CFS budget a ``cpus:`` limit imposes — invisible to
#: :func:`os.sched_getaffinity`, which sees only a cpuset pin.
_CGROUP_V2_CPU_MAX_PATH = Path("/sys/fs/cgroup/cpu.max")

#: cgroup v1 CPU-controller CFS quota / period files (microseconds). A quota of
#: ``-1`` means unlimited; otherwise quota/period is the fractional-core budget.
_CGROUP_V1_CPU_QUOTA_PATH = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
_CGROUP_V1_CPU_PERIOD_PATH = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")


def _read_int_file(path: Path) -> int | None:
    """Return the single integer in cgroup file *path*, or ``None``.

    A narrow :class:`OSError` (the file is absent — a different cgroup version,
    or off Linux) returns ``None`` so the caller can try the other cgroup layout
    or fall back to the host read; a body that is not a single integer also
    returns ``None``. The ``OSError`` is the documented file-absent signal, never
    a blind ``except``. Impure: reads the kernel.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    # Strip a *single* leading ``-`` (a lone ``-1`` is the cfs_quota_us "unlimited"
    # sentinel), then require pure digits: a multi-dash body like ``--1`` passes a
    # naive ``lstrip('-').isdigit()`` yet crashes ``int()``, so it is rejected here.
    body = text[1:] if text.startswith("-") else text
    if body.isdigit():
        return int(text)
    return None


def _cgroup_v2_ancestor_dirs() -> tuple[Path, ...]:
    """This process's cgroup v2 directories below the mount root, leaf first.

    Reads the ``0::<path>`` line of :data:`_PROC_SELF_CGROUP_PATH` and returns
    each directory from the process's own cgroup up to (but excluding) the
    mount root :data:`_CGROUP_V2_ROOT`, whose files the root-path constants
    already name. On a bare host the binding limit can sit on an ancestor slice
    (``user-<uid>.slice``) while the leaf and the real root carry none; inside a
    cgroup namespace the line reads ``0::/`` and this returns ``()``, so the
    readers reduce to the root-only read. An unreadable file (narrow
    :class:`OSError`), no ``0::`` line, a relative path, or any ``.``/``..``
    component also returns ``()`` — never a path escaping the root. Impure.
    """
    try:
        text = _PROC_SELF_CGROUP_PATH.read_text(encoding="utf-8")
    except OSError:
        return ()
    for line in text.splitlines():
        if not line.startswith("0::"):
            continue
        raw = line[3:].strip()
        if not raw.startswith("/"):
            return ()
        parts = [part for part in raw.split("/") if part]
        if any(part in {".", ".."} for part in parts):
            return ()
        return tuple(
            _CGROUP_V2_ROOT.joinpath(*parts[:depth]) for depth in range(len(parts), 0, -1)
        )
    return ()


def _cgroup_v2_memory_levels() -> tuple[tuple[Path, Path], ...]:
    """``(memory.max, memory.current)`` path pairs leaf first, ending at the root."""
    return (
        *((d / "memory.max", d / "memory.current") for d in _cgroup_v2_ancestor_dirs()),
        (_CGROUP_V2_MEMORY_MAX_PATH, _CGROUP_V2_MEMORY_CURRENT_PATH),
    )


def _read_v2_memory_level(max_path: Path, current_path: Path) -> _CgroupMemory | None:
    """One cgroup v2 level's memory headroom (``memory.max`` minus ``memory.current``).

    ``None`` when ``memory.max`` is absent at this level. :class:`_CgroupMemory`
    with ``enforced=False`` when the limit is the ``max`` sentinel (no enforced
    cap) or the usage read is unavailable — *this level binds nothing*.
    Otherwise ``enforced=True`` carrying the measured headroom, **including a
    measured zero**: a cgroup whose ``memory.current`` has reached its
    ``memory.max`` has no headroom left, and that fact must survive the
    host/cgroup selection rather than being confused with "no cap here". Impure.
    """
    try:
        limit_text = max_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not limit_text.isdigit():
        # ``max`` (unlimited) or an unparseable body: no enforced v2 cap.
        return _CgroupMemory(enforced=False)
    current = _read_int_file(current_path)
    if current is None or current < 0:
        return _CgroupMemory(enforced=False)
    headroom = max(0, (int(limit_text) - current) // _MIB_BYTES)
    return _CgroupMemory(enforced=True, headroom_mib=headroom)


def _read_cgroup_v2_available_mib() -> _CgroupMemory | None:
    """cgroup v2 memory headroom: the tightest across this process's cgroup path.

    Folds :func:`_read_v2_memory_level` over every level from the process's own
    cgroup up to the mount root (:func:`_cgroup_v2_ancestor_dirs`). ``None`` only
    when ``memory.max`` is absent at every level — not a v2 hierarchy, so the
    caller should try v1. ``enforced=True`` with the smallest headroom (a
    measured zero kept) when any level enforces a cap; otherwise
    ``enforced=False``. Impure.
    """
    readings = [
        reading
        for reading in (_read_v2_memory_level(m, c) for m, c in _cgroup_v2_memory_levels())
        if reading is not None
    ]
    if not readings:
        return None
    enforced = [r.headroom_mib for r in readings if r.enforced]
    if enforced:
        return _CgroupMemory(enforced=True, headroom_mib=min(enforced))
    return _CgroupMemory(enforced=False)


def _read_cgroup_v2_limit_bytes() -> int | None:
    """The tightest finite cgroup v2 ``memory.max`` across this process's path, or ``None``."""
    limits = [
        limit
        for limit in (_read_int_file(m) for m, _ in _cgroup_v2_memory_levels())
        if limit is not None and 0 < limit < _CGROUP_UNLIMITED_BYTES
    ]
    return min(limits) if limits else None


def _read_cgroup_v1_available_mib() -> _CgroupMemory | None:
    """cgroup v1 memory headroom (``limit_in_bytes`` minus ``usage_in_bytes``) in MiB.

    ``None`` when ``memory.limit_in_bytes`` is absent — not a v1 hierarchy.
    :class:`_CgroupMemory` with ``enforced=False`` when the limit is the
    ~``PAGE_COUNTER_MAX`` unlimited sentinel or the usage read is unavailable.
    Otherwise ``enforced=True`` carrying the measured headroom, **including a
    measured zero** (an exhausted container limit), on the same tri-state
    contract as the v2 reader. Impure.
    """
    limit = _read_int_file(_CGROUP_V1_MEMORY_LIMIT_PATH)
    if limit is None:
        return None
    if limit >= _CGROUP_UNLIMITED_BYTES:
        return _CgroupMemory(enforced=False)
    usage = _read_int_file(_CGROUP_V1_MEMORY_USAGE_PATH)
    if usage is None or usage < 0:
        return _CgroupMemory(enforced=False)
    headroom = max(0, (limit - usage) // _MIB_BYTES)
    return _CgroupMemory(enforced=True, headroom_mib=headroom)


def _read_cgroup_memory() -> _CgroupMemory:
    """cgroup memory headroom across v2 then v1, as a tri-state reading.

    Tries the v2 unified hierarchy first and the v1 layout second; an
    ``enforced=False`` result when neither layout enforces a memory limit (or the
    files are absent/unreadable), so the host read governs
    :func:`_read_memory_bound`. An ``enforced=True`` result carries the measured
    headroom, which may be ``0``. Impure.
    """
    v2 = _read_cgroup_v2_available_mib()
    if v2 is not None:
        return v2
    v1 = _read_cgroup_v1_available_mib()
    if v1 is not None:
        return v1
    return _CgroupMemory(enforced=False)


def _read_host_available_mib() -> int:
    """Return ``MemAvailable`` from :data:`_MEMINFO_PATH` in MiB, or ``0``.

    ``/proc/meminfo`` reports ``MemAvailable`` in KiB; this divides to MiB. The
    field is missing on very old kernels and the whole file is absent off Linux,
    so an unreadable file (a narrow :class:`OSError`, never a blind ``except``) or
    an absent/unparseable field returns a documented ``0``. Impure: reads the kernel.
    """
    return _meminfo_mib("MemAvailable")


def _read_memory_bound() -> tuple[int, MemoryBound]:
    """Return the memory a gate run may claim and *which* bound produced it.

    The tighter-of-two selection :func:`_read_available_mib` has always
    performed, but returning the winning figure *with* its :data:`MemoryBound`
    so :func:`plan_lease` can apply the host-measured
    :data:`SYSTEM_RESERVE_MIB` only where it is not already netted out.

    The cgroup figure counts whenever the cgroup *enforces* a limit — including
    a measured headroom of ``0``, an exhausted container cap, which must reach
    :func:`plan_lease`'s degenerate-capacity floor rather than be discarded in
    favour of a positive host figure. It wins only when it is *strictly* tighter
    than a positive host figure: on a tie, and in the no-candidate ``0`` case,
    the bound reported is ``"host"`` — the conservative reading, since it is the
    one that retains the reserve. Impure: both candidates read the kernel.
    """
    host = _read_host_available_mib()
    cgroup = _read_cgroup_memory()
    if cgroup.enforced and (host <= 0 or cgroup.headroom_mib < host):
        return cgroup.headroom_mib, "cgroup"
    return max(0, host), "host"


def _read_available_mib() -> int:
    """Return the memory a gate run may claim, in MiB — the tighter of host and cgroup.

    ``/proc/meminfo``'s ``MemAvailable`` (:func:`_read_host_available_mib`) reports
    the *host's* free memory, which inside a memory-limited container overstates
    what the process may allocate before the cgroup OOM-kills it. So this also
    reads the cgroup memory headroom (:func:`_read_cgroup_memory`) and returns
    the tighter of the host figure and an *enforced* cgroup headroom — which may
    be a measured ``0``, an exhausted container cap that binds — or ``0`` when
    neither is known, which makes :func:`plan_lease`'s ``available_mib <= 0``
    floor grant one worker rather than inventing a second guard here. The
    integer-returning façade over :func:`_read_memory_bound`, which owns the
    selection and additionally reports which bound won. Impure.
    """
    return _read_memory_bound()[0]


def _read_v2_cpu_level(path: Path) -> int | None:
    """One cgroup v2 level's CFS-quota core count from ``cpu.max`` (``"<quota> <period>"``).

    ``max(1, quota // period)`` when a numeric quota is enforced — a fractional
    quota floors to a single core, never ``0`` — ``0`` when the quota is the
    ``max`` sentinel or the line is malformed, and ``None`` when ``cpu.max`` is
    absent at this level. Impure.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    fields = text.split()
    if len(fields) < 2 or not fields[0].isdigit() or not fields[1].isdigit():
        # ``max <period>`` (no quota) or a malformed line: no enforced v2 quota.
        return 0
    period = int(fields[1])
    if period <= 0:
        return 0
    return max(1, int(fields[0]) // period)


def _read_cgroup_v2_quota_cores() -> int | None:
    """cgroup v2 CFS-quota cores: the tightest ``cpu.max`` across this process's path.

    Folds :func:`_read_v2_cpu_level` over every level from the process's own
    cgroup up to the mount root. ``None`` when ``cpu.max`` is absent at every
    level (not a v2 hierarchy); the smallest positive quota when any level
    enforces one; otherwise ``0``. Impure.
    """
    paths = (*(d / "cpu.max" for d in _cgroup_v2_ancestor_dirs()), _CGROUP_V2_CPU_MAX_PATH)
    readings = [r for r in (_read_v2_cpu_level(p) for p in paths) if r is not None]
    if not readings:
        return None
    quotas = [r for r in readings if r > 0]
    return min(quotas) if quotas else 0


def _read_cgroup_v1_quota_cores() -> int | None:
    """cgroup v1 CFS-quota cores from ``cpu.cfs_quota_us`` / ``cpu.cfs_period_us``.

    ``max(1, quota // period)`` when a positive quota is enforced; ``0`` when the
    quota is ``-1`` (unlimited), non-positive, or the period is unreadable; and
    ``None`` when ``cpu.cfs_quota_us`` is absent (not a v1 hierarchy). Impure.
    """
    quota = _read_int_file(_CGROUP_V1_CPU_QUOTA_PATH)
    if quota is None:
        return None
    if quota <= 0:
        # ``-1`` (unlimited) or any non-positive value: no enforced quota.
        return 0
    period = _read_int_file(_CGROUP_V1_CPU_PERIOD_PATH)
    if period is None or period <= 0:
        return 0
    return max(1, quota // period)


def _read_cgroup_quota_cores() -> int:
    """CFS-quota core count (``>= 1``) across cgroup v2 then v1, or ``0`` if none.

    Tries the v2 unified hierarchy first and the v1 layout second; ``0`` when
    neither enforces a CPU quota (or the files are absent/unreadable), so the
    affinity count governs :func:`_available_cores`. Impure.
    """
    v2 = _read_cgroup_v2_quota_cores()
    if v2 is not None:
        return v2
    v1 = _read_cgroup_v1_quota_cores()
    if v1 is not None:
        return v1
    return 0


def _read_affinity_cores() -> int:
    """Return the number of CPUs this process is scheduled onto (cpuset-aware).

    :func:`os.sched_getaffinity` reflects a Docker ``--cpuset-cpus`` pin or a
    ``taskset`` — a restriction on *which* CPUs, invisible to :func:`os.cpu_count`
    (the machine's whole logical-CPU total). It is Linux-only, so on a platform
    without it (macOS/Windows) fall back to :func:`os.cpu_count`, and to ``1``
    when even that is unknown, so an unknown topology is a single core rather than
    a crash. Impure: reads the scheduler.
    """
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def host_cores() -> int:
    """Return the host's nproc-equivalent core count, floored at one.

    This is the denominator a *host-global* load average (``/proc/loadavg`` is not
    namespaced) is normalised against: the cpuset-aware affinity count, deliberately
    *not* clamped by the cgroup CFS quota — a ``cpus: '2.0'`` limit caps what this
    container may burn, not how many cores the host's run queue spreads over.
    Worker sizing must use ``measure_host_capacity().cores`` instead. Impure: reads
    the scheduler.
    """
    return max(1, _read_affinity_cores())


def _available_cores() -> int:
    """Return the number of CPUs a gate run may actually use.

    The smaller of the cpuset-aware affinity count (:func:`_read_affinity_cores`)
    and the cgroup CFS quota (:func:`_read_cgroup_quota_cores`). Affinity reflects
    only a *cpuset* pin (``--cpuset-cpus`` / ``taskset``); it does **not** see the
    CFS bandwidth quota a ``cpus: '2.0'`` limit imposes, so on a quota-limited
    container ``sched_getaffinity`` still returns the host core count. Taking the
    ``min`` with the quota (when one is enforced) sizes the grant to the cores the
    container may actually burn, floored at one; with no quota the affinity count
    stands unchanged. Impure: reads the scheduler and the kernel.
    """
    affinity = _read_affinity_cores()
    quota = _read_cgroup_quota_cores()
    if quota > 0:
        return max(1, min(affinity, quota))
    return affinity


def measure_host_capacity() -> HostCapacity:
    """Snapshot the live host into a :class:`HostCapacity` for :func:`plan_lease`.

    ``cores`` is :func:`_available_cores` — the smaller of the cpuset-aware
    affinity count and the cgroup CFS quota, i.e. the CPUs a gate run may actually
    burn (not the host-wide count :func:`host_cores` returns).
    ``available_mib`` and ``bounded_by`` come from the single
    :func:`_read_memory_bound` selection, so the figure and the bound that
    produced it can never disagree. All three are *measured* here and then
    frozen, so the whole lease is planned against one stable snapshot. Impure:
    the readings are this function's reason to exist.
    """
    available_mib, bounded_by = _read_memory_bound()
    return HostCapacity(
        cores=_available_cores(),
        available_mib=available_mib,
        bounded_by=bounded_by,
    )


@dataclass(frozen=True)
class GateSlotPlan:
    """How a host's usable CPUs divide into concurrent, CPU-disjoint gate slots.

    ``cpus`` holds one CPU-id set per slot: disjoint, SMT siblings together, and
    exactly as wide as the slot's admitted width. Every slot *claims* its set —
    one lock per CPU id in the wrapper — so the CPUs claimed by all concurrent
    runs on a host never exceed the CPUs it has, however each run planned. When
    ``pinned`` the run is also confined to its set (``taskset``), so it never
    shares a CPU — nor, when the width is a multiple of the SMT width, a physical
    core — with another slot's run, and the lease, which reads the affinity,
    grants at most that width. ``pinned`` is false when a CFS quota rather than a
    cpuset limits the CPUs: the set is still claimed, but confining every
    quota-limited container to the lowest CPU ids would only crowd them. An empty
    set means the CPU ids are unknown (no ``sched_getaffinity``): one unpinned,
    unclaimed slot. ``workers`` is the ``pytest-xdist`` width one slot admits,
    ``0`` when it cannot be measured (leave ``-n auto`` to xdist).
    """

    cpus: tuple[tuple[int, ...], ...]
    workers: int
    pinned: bool = True


def plan_gate_slots(
    cpu_groups: Iterable[Iterable[int]], quota_cores: int, total_mib: int,
) -> GateSlotPlan:
    """Divide the usable CPUs into CPU-disjoint gate slots sized by cores *and* memory.

    *cpu_groups* is the process's affinity set grouped by physical core (SMT
    siblings together); the usable cores are its CPUs, capped at *quota_cores*
    when a CFS quota is enforced (``> 0``). The slot count is the smaller of two
    budgets, floored at one:

    * **cores** — ``cores // MIN_WORKERS_PER_ADMITTED_RUN``, so every admitted run
      gets at least the width whose per-test timeouts are sized for it;
    * **memory** — *total_mib* less :data:`SYSTEM_RESERVE_MIB`, over one run's
      resident need at that width (:data:`SESSION_BASELINE_MIB` plus
      ``MIN_WORKERS_PER_ADMITTED_RUN`` x :data:`PER_WORKER_MIB`). An unknown
      memory bound (``total_mib <= 0``) leaves the cores budget alone.

    *total_mib* is the host's (or container's) **total** memory bound, not the
    live headroom, so the plan is stable across invocations. Each slot's width is
    an equal share of the cores clamped by its memory share, and its CPU set is
    exactly that many CPUs (contiguous in sibling order) — claimed always, pinned
    unless a quota is the limit — so the lease's affinity read enforces the memory
    clamp too, and any remainder idles rather than oversubscribe. Pure: the caller
    supplies the readings.
    """
    groups = sorted(
        (tuple(sorted(set(group))) for group in cpu_groups if group), key=min,
    )
    ordered: list[int] = []
    seen: set[int] = set()
    for group in groups:
        for cpu in group:
            if cpu not in seen:
                seen.add(cpu)
                ordered.append(cpu)
    if not ordered:
        return GateSlotPlan(cpus=((),), workers=0, pinned=False)
    quota_capped = 0 < quota_cores < len(ordered)
    cores = quota_cores if quota_capped else len(ordered)
    core_slots = max(1, cores // MIN_WORKERS_PER_ADMITTED_RUN)
    budget_mib = total_mib - SYSTEM_RESERVE_MIB
    run_mib = SESSION_BASELINE_MIB + MIN_WORKERS_PER_ADMITTED_RUN * PER_WORKER_MIB
    slots = (
        max(1, min(core_slots, budget_mib // run_mib)) if total_mib > 0 else core_slots
    )
    chunk = cores // slots
    width = chunk
    if total_mib > 0:
        memory_width = (budget_mib // slots - SESSION_BASELINE_MIB) // PER_WORKER_MIB
        width = max(1, min(chunk, memory_width))
    cpus = tuple(tuple(ordered[i * chunk:i * chunk + width]) for i in range(slots))
    return GateSlotPlan(cpus=cpus, workers=width, pinned=not quota_capped)


def _meminfo_mib(field: str) -> int:
    """Return ``/proc/meminfo``'s *field* (``"MemTotal"`` / ``"MemAvailable"``) in MiB, or ``0``.

    The file reports KiB; this divides to MiB. An unreadable file (a narrow
    :class:`OSError` — off Linux, never a blind ``except``), an absent field, or an
    unparseable value returns a documented ``0``. Impure: reads the kernel.
    """
    try:
        text = _MEMINFO_PATH.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        if line.startswith(f"{field}:"):
            parts = line.split()
            # ``MemTotal:   24576000 kB`` — the KiB count is the middle field.
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) // 1024
            return 0
    return 0


def _read_total_mib() -> int:
    """Return the total memory bound in MiB: the cgroup limit if enforced, else ``MemTotal``.

    The cgroup v2 ``memory.max`` (the literal ``max`` when unlimited, so it reads
    as ``None``) or v1 ``memory.limit_in_bytes`` (at or above
    :data:`_CGROUP_UNLIMITED_BYTES` when unlimited) caps a container below the
    host; the smaller of that limit and ``MemTotal`` is the bound. The v2 limit
    is the tightest finite ``memory.max`` along this process's cgroup path
    (:func:`_read_cgroup_v2_limit_bytes`) — an ancestor slice on a bare host,
    the root alone inside a cgroup namespace; v1 reads the root only. ``0`` when nothing is known, which
    :func:`plan_gate_slots` treats as an unknown memory bound. Impure.
    """
    limits = [
        limit // _MIB_BYTES
        for limit in (
            _read_cgroup_v2_limit_bytes(),
            _read_int_file(_CGROUP_V1_MEMORY_LIMIT_PATH),
        )
        if limit is not None and 0 < limit < _CGROUP_UNLIMITED_BYTES
    ]
    bounds = [b for b in (_meminfo_mib("MemTotal"), *limits) if b > 0]
    return min(bounds) if bounds else 0


def _read_affinity_cpu_ids() -> tuple[int, ...]:
    """Return the CPU ids this process may run on, or ``()`` when unknowable.

    The id set (not just the count :func:`_read_affinity_cores` returns), because
    slots pin to *real* CPU ids — a ``--cpuset-cpus 4-11`` container's CPUs are
    not ``0..7``. ``()`` off Linux (no :func:`os.sched_getaffinity`), which
    :func:`plan_gate_slots` turns into one unpinned slot. Impure: reads the
    scheduler.
    """
    try:
        return tuple(sorted(os.sched_getaffinity(0)))
    except AttributeError:
        return ()


#: Per-CPU topology directory; ``cpu<N>/topology/thread_siblings_list`` names the
#: SMT siblings sharing CPU ``N``'s physical core (``0,4`` or ``0-1``).
_CPU_TOPOLOGY_DIR = Path("/sys/devices/system/cpu")


def _parse_cpu_list(text: str) -> tuple[int, ...]:
    """Parse a kernel CPU list (``0,4`` / ``0-1`` / ``0-1,8``) into ids; ``()`` if malformed. Pure."""
    ids: list[int] = []
    for part in text.strip().split(","):
        lo, sep, hi = part.partition("-")
        if not lo.isdigit() or (sep and not hi.isdigit()):
            return ()
        ids.extend(range(int(lo), int(hi) + 1) if sep else [int(lo)])
    return tuple(ids)


def _read_cpu_groups(cpu_ids: Iterable[int]) -> tuple[tuple[int, ...], ...]:
    """Group *cpu_ids* by physical core from sysfs SMT topology; one group per CPU if unknown.

    Each CPU's ``thread_siblings_list`` is intersected with *cpu_ids* (a sibling
    outside the affinity set is not ours to pin). An unreadable file (a narrow
    :class:`OSError`) or a malformed list leaves that CPU in a group of its own.
    Impure: reads sysfs.
    """
    wanted = set(cpu_ids)
    groups: dict[tuple[int, ...], None] = {}
    for cpu in sorted(wanted):
        path = _CPU_TOPOLOGY_DIR / f"cpu{cpu}" / "topology" / "thread_siblings_list"
        try:
            siblings = _parse_cpu_list(path.read_text(encoding="utf-8"))
        except OSError:
            siblings = ()
        group = tuple(sorted(wanted.intersection(siblings) | {cpu}))
        groups[group] = None
    return tuple(groups)


def measure_gate_slots() -> GateSlotPlan:
    """Plan the gate slots for the live host. Impure: reads affinity, topology, quota, memory."""
    return plan_gate_slots(
        _read_cpu_groups(_read_affinity_cpu_ids()),
        _read_cgroup_quota_cores(),
        _read_total_mib(),
    )


def _slot_scope() -> str:
    """Return the lease lock-name scope for this process's CPU set: ``""`` when unpinned.

    Runs pinned (``taskset`` / a cpuset) to disjoint CPU sets never compete for
    the same cores, so they must not compete for the same lease slots either — or
    two core-disjoint gate slots would serialise on one lease lock. A pinned
    process therefore scopes its slot files by its CPU ids (``cpus0-3-``); an
    unpinned one (affinity covering every CPU, or unknowable) keeps the
    historical names. Scopes only separate the *files*: runs whose CPU sets
    overlap still count each other through :func:`_foreign_held`, an unscoped
    lease covering every CPU. Impure: reads the scheduler.
    """
    ids = _read_affinity_cpu_ids()
    if not ids or len(ids) >= (os.cpu_count() or 0):
        return ""
    runs: list[str] = []
    start = prev = ids[0]
    for cpu in (*ids[1:], None):
        if cpu is not None and cpu == prev + 1:
            prev = cpu
            continue
        runs.append(str(start) if start == prev else f"{start}-{prev}")
        if cpu is not None:
            start = prev = cpu
    return f"cpus{'_'.join(runs)}-"


def _slot_lock_path(lock_dir: Path, scope: str, index: int) -> Path:
    """The lease slot file for *index* within *scope* (see :func:`_slot_scope`). Pure."""
    return lock_dir / f"gate-slot-{scope}{index}.lock"


#: A lease slot file name: an optional CPU scope (``cpus0-3_8-``) then the index.
_SLOT_FILE_RE = re.compile(r"gate-slot-(?P<scope>cpus[\d_-]*-)?(?P<index>\d+)\.lock")


def _scope_cpus(scope: str) -> frozenset[int] | None:
    """The CPU ids a lease *scope* covers, ``None`` for every CPU (unscoped). Pure."""
    if not scope:
        return None
    ids: set[int] = set()
    for part in scope.removeprefix("cpus").removesuffix("-").split("_"):
        lo, _, hi = part.partition("-")
        ids.update(range(int(lo), int(hi or lo) + 1))
    return frozenset(ids)


def _probe_held(path: Path) -> bool:
    """Whether another process holds the lease file *path* (a shared, non-blocking probe).

    A file removed between the directory listing and the probe (a narrow
    :class:`FileNotFoundError`) is not held. Impure.
    """
    try:
        fd = os.open(str(path), os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return True
    except BaseException:
        os.close(fd)
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
    return False


def _foreign_held(lock_dir: Path, scope: str) -> int:
    """Count held lease slots in *other* scopes whose CPUs overlap *scope*'s.

    Scoping keeps core-disjoint pinned runs from serialising on one lock, but a
    run must still see every lease that shares a CPU with it — a pinned run and
    an unpinned one (whose unscoped lease covers every CPU) both. An absent lock
    directory (a narrow :class:`FileNotFoundError`) holds nothing. Impure.
    """
    try:
        names = os.listdir(str(lock_dir))
    except FileNotFoundError:
        return 0
    mine = _scope_cpus(scope)
    held = 0
    for name in sorted(names):
        match = _SLOT_FILE_RE.fullmatch(name)
        if match is None or (match.group("scope") or "") == scope:
            continue
        theirs = _scope_cpus(match.group("scope") or "")
        if mine is not None and theirs is not None and not (mine & theirs):
            continue
        if _probe_held(lock_dir / name):
            held += 1
    return held


def count_in_flight(lock_dir: Path, slots: int) -> int:
    """Count how many of *slots* gate slots are currently held under *lock_dir*.

    The ``in_flight`` argument :func:`plan_lease` needs, read from the live slot
    set: probes ``gate-slot-{index}.lock`` for ``index`` in ``0..slots-1`` with a
    non-blocking *shared* ``fcntl.flock``. A :class:`BlockingIOError` is the
    documented "slot busy" signal — explicit control flow, not a silenced error —
    so that slot is counted (a holder's :func:`acquire_slot` ``LOCK_EX`` excludes
    a shared probe) and its probe fd closed. A slot this reader *could* lock is
    free: it is immediately released and closed so the count never itself holds a
    slot or reads as in-flight. The probe is **shared**, not exclusive, so two
    counters running at once do not each transiently ``LOCK_EX`` a free slot and
    mis-read the other's probe as a holder; a shared probe still blocks against a
    real ``LOCK_EX`` holder, so held slots are counted correctly either way. Any
    other ``fcntl.flock`` error closes the just-opened probe fd before
    propagating, so an unexpected failure never leaks a descriptor.

    *lock_dir* is created (``exist_ok``) on entry, mirroring :func:`acquire_slot`,
    so this may be called before any run has acquired a slot. Impure.
    """
    os.makedirs(str(lock_dir), exist_ok=True)
    held = 0
    scope = _slot_scope()
    for index in range(slots):
        slot_path = _slot_lock_path(lock_dir, scope, index)
        fd = os.open(str(slot_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            held += 1
            os.close(fd)
            continue
        except BaseException:
            os.close(fd)
            raise
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return held + _foreign_held(lock_dir, scope)


# ---------------------------------------------------------------------------
# Gate-run telemetry — one append-only row per admitted gate run, so the lease
# can be measured (does p95 wall time under concurrency converge?) rather than
# assumed. The writer/parser mirror the cost-samples / gate-activations ledgers.
# ---------------------------------------------------------------------------

#: Worst-first precedence for collapsing a whole gate run's per-gate observation
#: kinds into one recorded outcome. The two non-answers (a killed or unrunnable
#: gate) rank ahead of the reds so a later read surface can separate a resource
#: artifact from an answered red; among the reds the order mirrors
#: :data:`maestro.gate_observation._RED_KIND_PRECEDENCE`. ``CLEAN`` is absent —
#: it is the outcome only when no non-clean kind is present at all.
_OUTCOME_PRECEDENCE: tuple[GateObservationKind, ...] = (
    "TIMEOUT",
    "UNAVAILABLE",
    "FAILED",
    "COLLECTION_ERROR",
    "COVERAGE",
)


@dataclass(frozen=True)
class GateRunRecord:
    """One admitted gate run, as appended to ``gate_runs.jsonl``.

    ``started_at`` is an ISO-8601 UTC timestamp stamped at seam entry;
    ``duration_seconds`` the wall time from that entry (host measurement, slot
    acquisition, and the whole gate sequence) to context exit; ``workers`` the
    granted ``pytest-xdist`` width; ``in_flight`` the number of *other* gate runs
    observed sharing the host when this one started; ``admission`` whether a real
    slot was held (``"slot"``), the run fell open under saturation
    (``"fallback"``), or it was refused before any host read because the cached
    runnability verdict says the gate cannot reach a verdict (``"declined"``);
    ``outcome`` how it ended (:data:`GateRunOutcome`);
    ``available_mib`` the headroom *measured at admission* and ``bounded_by``
    which reading produced it (:data:`MemoryBound`), so whether a wider grant is
    reachable at a given cgroup size is a measured fact rather than an
    assumption. That headroom is what the grant was planned against only on an
    ``admission="slot"`` row; a ``"fallback"`` row's grant is the hardcoded
    one-worker floor, so its ``available_mib`` records what the host looked like
    at admission, not what the grant was derived from. A ``"declined"`` row
    measured no host at all, so it carries the zero defaults for both, and its
    ``workers`` / ``in_flight`` are zero: nothing was granted and nothing was
    counted. Those last two are stamped whether or not
    ``gate_lease_cgroup_reserve_enabled`` is on — that is what makes the flag
    flippable against recorded evidence — and carry defaults so a pre-change row
    still parses (rather than being dropped by
    :func:`_parse_gate_run_dict`, which would silently wipe the contention
    window). ``source`` names which caller produced the row
    (:data:`GateRunSource`) and ``deadline_seconds`` the budget that was actually
    *enforced* on it — both stamped regardless of
    ``gate_budget_lease_owned_enabled``, so the flag is flipped against measured
    evidence. Together they make "killed at *its own* deadline" a recorded fact
    :func:`derive_deadline` can read, rather than an inference from the whole-slot
    ``duration_seconds`` (which also covers host measurement and slot acquisition).
    A ``deadline_seconds`` of ``0`` is a pre-change row — or a run the flag was off
    for — and reads as censored at the caller's ``selfcheck`` constant.
    ``tests_seconds`` is the wall time of the run's **longest single pytest
    command** — the probe, or the ``-x``-less enumerate re-run when that one ran
    longer — which is precisely the quantity ``deadline_seconds`` is enforced
    against, and so the only one a *completed* run's budget can be derived from.
    It is deliberately distinct from ``duration_seconds``, which covers the whole
    slot: host measurement, slot acquisition, ruff, mypy, and both pytest
    invocations together. Stamped unconditionally, defaulted to ``0.0`` so a
    pre-change row still parses, and ``0.0`` reads as "unmeasured" —
    :func:`derive_deadline` requires a *positive* figure. ``scope`` names *which
    suite* that figure measured (:data:`GateScope`), defaulted to ``"full"`` so a
    pre-change row parses as what it was; it exists so a narrowed run's cost can
    never be read as the whole suite's by a derivation that assumes otherwise.
    ``wait_seconds`` is the monotonic time from seam entry to admission — the
    queue time, kept apart from run time so the admission cap can be judged on
    whether its queue costs less than the contention it replaces; defaulted to
    ``0.0`` so a pre-change row still parses.
    Frozen — a recorded run is history.
    """

    started_at: str
    duration_seconds: float
    workers: int
    in_flight: int
    admission: LeaseAdmission
    outcome: GateRunOutcome
    available_mib: int = 0
    bounded_by: MemoryBound = "host"
    source: GateRunSource = "in_process"
    deadline_seconds: int = 0
    tests_seconds: float = 0.0
    scope: GateScope = "full"
    wait_seconds: float = 0.0


def observation_outcome(kinds: Iterable[GateObservationKind]) -> GateRunOutcome:
    """Collapse a gate run's per-gate observation kinds into one outcome.

    Walks :data:`_OUTCOME_PRECEDENCE` worst-first and returns the first kind
    present, so a run that both timed out and failed records the more urgent
    ``TIMEOUT``. Returns ``"CLEAN"`` when no non-clean kind is present at all —
    including the empty case, since a run with no observed gates produced no red.
    Pure, zero I/O.
    """
    present = set(kinds)
    for candidate in _OUTCOME_PRECEDENCE:
        if candidate in present:
            return candidate
    return "CLEAN"


def record_gate_run(path: Path, record: GateRunRecord) -> None:
    """Append one :class:`GateRunRecord` as a JSONL line to *path*.

    Mirrors :func:`maestro.cost_tracking.append_cost_sample` exactly: the append
    takes :func:`maestro.fileutil.file_lock` so a concurrent rotation's
    ``read_text`` -> ``atomic_write`` rewrite cannot lose it (an advisory
    ``flock`` only excludes writers who also take it, so an unlocked append
    landing inside a rename is destroyed). The ``mkdir`` stays above the lock:
    ``file_lock`` creates its ``.lock`` file adjacent to *path* and needs the
    parent to exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path), open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record)) + "\n")


#: Fields of :class:`GateRunRecord` whose decoded value must be a ``str``.
_GATE_RUN_STR_FIELDS = (
    "started_at", "admission", "outcome", "bounded_by", "source", "scope",
)

#: Fields whose decoded value must be an ``int`` (``bool`` excluded — it is an
#: ``int`` subclass, and a boolean concurrency is corruption, not a count).
_GATE_RUN_INT_FIELDS = (
    "workers", "in_flight", "available_mib", "deadline_seconds",
)

#: Fields whose decoded value must be a *finite* real number. ``int`` is
#: admitted alongside ``float`` because JSON renders ``10.0`` as ``10``; ``nan``
#: and the infinities are rejected because they poison every sort and median
#: they reach.
_GATE_RUN_REAL_FIELDS = ("duration_seconds", "tests_seconds", "wait_seconds")


def _wrong_type_message(name: str, value: object, expected: str) -> str:
    """Name the offending field and both types, without echoing the value.

    The value itself is deliberately left out: a corrupt ledger line can carry
    an arbitrarily long string, and this message goes to an unbounded WARNING.
    The field name plus what was expected is what an operator needs to find the
    bad row. Pure, zero I/O.
    """
    return f"{name}: expected {expected}, got {type(value).__name__}"


def _check_gate_run_value_types(row: dict[str, object]) -> None:
    """Raise ``TypeError`` when any present field of *row* is wrong-typed.

    A :class:`GateRunRecord` is a plain dataclass, so its annotations are not
    enforced at construction — a structurally complete row carrying
    ``in_flight: null`` or ``duration_seconds: "10"`` would otherwise parse
    cleanly and only fail much later, inside the sorts and medians of
    :func:`measure_gate_contention`. Checking here routes such a row through the
    caller's existing rejection path instead. Fields absent from *row* are not
    checked: they take their dataclass defaults (or the row is dropped anyway
    for missing a required one). Pure, zero I/O.
    """
    for name in _GATE_RUN_STR_FIELDS:
        if name not in row:
            continue
        if not isinstance(row[name], str):
            raise TypeError(_wrong_type_message(name, row[name], "str"))
    for name in _GATE_RUN_INT_FIELDS:
        if name not in row:
            continue
        value = row[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(_wrong_type_message(name, value, "int"))
    for name in _GATE_RUN_REAL_FIELDS:
        if name not in row:
            continue
        value = row[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(_wrong_type_message(name, value, "int or float"))
        if not math.isfinite(value):
            raise TypeError(f"{name}: expected a finite number, got {value}")


def _parse_gate_run_dict(
    data: dict[str, object], *, source: Path | None = None,
) -> GateRunRecord | None:
    """Coerce an already-decoded JSON row into a :class:`GateRunRecord`.

    Field-filters against ``{f.name for f in fields(GateRunRecord)}`` (so an
    unknown future field is dropped, not fatal) and returns ``None`` on any
    structural failure (``KeyError`` / ``TypeError`` / ``AttributeError``) — a
    row missing a required field is dropped, not raised. Structural presence is
    not enough: every *value* is type-checked against the dataclass contract
    first (:func:`_check_gate_run_value_types`), because a frozen dataclass
    enforces none of its annotations at runtime, so a complete-but-wrong-typed
    row would escape this boundary and crash a downstream sort instead. A
    violation takes the same single rejection path as a missing key. Mirrors
    :func:`maestro.audit_checks._parse_gate_activation_dict` so a future read
    surface and the round-trip test share one corruption-tolerant parser. The
    ``json.loads`` that can raise ``JSONDecodeError`` lives in the shared reader.
    *source*, when given, names the ledger in the warning, which also carries
    the rejecting exception so the offending field is legible to an operator.
    """
    known = {f.name for f in fields(GateRunRecord)}
    try:
        filtered = {k: v for k, v in data.items() if k in known}
        _check_gate_run_value_types(filtered)
        return GateRunRecord(**filtered)  # type: ignore[arg-type]
    except (KeyError, TypeError, AttributeError) as exc:
        logger.warning("Skipping corrupted gate_run row in %s: %s", source, exc)
        return None


# Multiplier a censored run's budget escalates by. A suite killed at its deadline
# tells us only that its true cost lies *above* that reading, so the next budget
# steps up geometrically — big enough to reach a fit within a few runs, small
# enough that a genuine hang burns only bounded extra wall time before the cap.
_DEADLINE_ESCALATION = 1.5

# Headroom a *completed* run's measured cost is multiplied by to become the next
# budget. A suite that finished in N seconds must be granted more than N — the
# suite grows, the host varies — but not so much more that a genuine hang runs
# unbounded before the cap catches it.
_DEADLINE_HEADROOM = 1.25


#: The outcomes that mean the suite actually **ran to completion**, so its
#: ``tests_seconds`` measures the suite rather than where it was cut off.
#: ``CLEAN`` is a full green run and ``COVERAGE`` is a shortfall only computable
#: after the last test ran; every other outcome is truncated — the ``-x`` probe
#: stops at its first failure (``FAILED``), collection never reached the tests
#: (``COLLECTION_ERROR``), the runner died or never started (``UNAVAILABLE``),
#: the deadline killed it (``TIMEOUT``), and ``UNKNOWN`` holds no claim at all.
#: Folding a truncated reading into the budget would drag it toward the
#: ``selfcheck`` floor and re-open the kill/escalate oscillation the retained
#: budget exists to prevent.
_COMPLETED_TESTS_OUTCOMES: frozenset[GateRunOutcome] = frozenset({"CLEAN", "COVERAGE"})

#: How many of the newest qualifying readings the completed p50 medians over. A
#: median across *all* history mixes suite sizes and host conditions from months
#: ago and moves arbitrarily slowly as rows accumulate, so the budget would stop
#: tracking the suite it is meant to fit; a short tail window keeps the reading
#: recent while still being a median rather than a single noisy sample.
_DEADLINE_P50_WINDOW = 20


def _completed_tests_p50(
    records: list[GateRunRecord], *, workers: int,
) -> float:
    """Median ``tests_seconds`` of completed in-process runs at *workers* width.

    Reads only rows that carry a *positive* ``tests_seconds`` (``0.0`` is a
    pre-change or unmeasured row, not a zero-cost suite), that ``source ==
    "in_process"`` (a wrapper row ran a different argv under no ceiling, and a
    sibling reader spent the flat constant), that ``scope == "full"`` (a narrowed
    run measured a different command, so its cost is not the suite's), and whose
    ``outcome`` is in
    :data:`_COMPLETED_TESTS_OUTCOMES` — a truncated run (a ``-x`` probe cut short
    at its first failure, a collection error, a runner that died, a killed suite)
    times *where it stopped*, not the suite. Only the newest
    :data:`_DEADLINE_P50_WINDOW` qualifying readings are medianed, so the budget
    tracks the suite as it grows instead of being pinned by stale history.
    ``0.0`` when nothing qualifies. Pure, zero I/O.
    """
    measured = [
        r.tests_seconds
        for r in records
        if r.source == "in_process"
        and r.scope == "full"
        and r.workers == workers
        and r.outcome in _COMPLETED_TESTS_OUTCOMES
        and r.tests_seconds > 0
    ]
    return _percentile(measured[-_DEADLINE_P50_WINDOW:], 50)


def derive_deadline(
    records: list[GateRunRecord],
    *,
    workers: int,
    selfcheck: int,
    selfcheck_max: int,
) -> int:
    """Derive the seconds budget the next gate run at *workers* width may take.

    Both halves of the fit derivation (proposal 134). A run killed at its deadline
    is a *censored* reading —
    it says the suite needs more than that, never how much more — so the budget
    escalates by :data:`_DEADLINE_ESCALATION` off the reading that was censored and
    tries again, converging on a fit within a few runs instead of recording a
    synthetic, never-shrinking ``tests:TIMEOUT`` identity forever.

    Only the **most recent** full-scope ``source="in_process"`` row at exactly
    *workers* matters: every other :data:`GateRunSource` either enforced no deadline
    or ran a different argv, a narrowed :data:`GateScope` measured a different
    command, and a row at another width measures a different suite cost. With
    no such row the budget is the ``selfcheck`` floor. When that row was killed, the
    budget is ``ceil(max(selfcheck, its deadline_seconds) * 1.5)`` clamped to
    *selfcheck_max*: the ``max`` floors a pre-change or flag-off row (whose
    ``deadline_seconds`` is ``0``) at the constant it was really killed at, and the
    clamp is what keeps a genuine hang bounded — a row censored at *selfcheck_max*
    derives *selfcheck_max* again, never above it.

    When that row *finished* (any outcome but ``"TIMEOUT"``, including a red, which
    is an answer) the budget is derived from what the suite **measurably cost**:
    ``min(selfcheck_max, max(selfcheck, ceil(p50 * 1.25)))`` over the positive
    ``tests_seconds`` of the recent in-process rows at this width that **ran to
    completion** (:data:`_COMPLETED_TESTS_OUTCOMES`; a truncated row times where it
    stopped, not the suite). That is the
    completed half of the fit derivation — the censored escalation above learns only
    that the suite costs *more* than some reading, while a finished run's
    ``tests_seconds`` is the cost itself, so the budget can shrink toward it when an
    escalation rung overshot and grow with it when the suite does.

    When no row at *workers* carries a measured figure but one-worker rows do, the
    serial p50 **stands in** divided by *workers*: a suite whose cost is known
    serially is the only evidence available about its cost at a wider grant, and
    perfect scaling is the conservative (tighter) estimate — the ``selfcheck`` floor
    catches the resulting under-estimate. With neither reading, the budget the row
    finished under is **retained** as the floor — ``max(selfcheck, its
    deadline_seconds)``, still clamped — rather than collapsing back to ``selfcheck``.
    A suite that needed 675s and reached a verdict there would otherwise be handed
    300s on the very next run and killed again, and the escalation would oscillate
    300 -> 450 -> 675 -> 300 forever instead of settling on the fit this derivation
    exists to find.

    Pure, zero I/O: the caller supplies the already-read rows.
    """
    latest = next(
        (
            r for r in reversed(records)
            if r.source == "in_process"
            and r.scope == "full"
            and r.workers == workers
        ),
        None,
    )
    if latest is None:
        return selfcheck
    if latest.outcome != "TIMEOUT":
        measured = _completed_tests_p50(records, workers=workers)
        if measured <= 0 and workers > 1:
            # Only serial runs are measured: stand in with the serial cost split
            # across the granted width. The floor catches an over-optimistic split.
            measured = _completed_tests_p50(records, workers=1) / workers
        if measured > 0:
            return min(
                selfcheck_max,
                max(selfcheck, ceil(measured * _DEADLINE_HEADROOM)),
            )
        return min(selfcheck_max, max(selfcheck, latest.deadline_seconds))
    censored_at = max(selfcheck, latest.deadline_seconds)
    return min(selfcheck_max, ceil(censored_at * _DEADLINE_ESCALATION))


def deadline_or_default(lease: GateLease | None, selfcheck: int) -> int:
    """Return *lease*'s granted budget, or the *selfcheck* fallback.

    The single reader for the budget half of a grant, mirroring what
    :func:`worker_args` is for the width half: a ``None`` lease (the lease-off
    rollback) and a lease carrying the ``0`` "no budget granted" sentinel (the
    budget flag off, or a fixture-constructed grant) both resolve to *selfcheck*,
    so no call site re-derives that fallback or has to know which flag produced it.
    Pure, zero I/O.
    """
    if lease is None or lease.deadline_seconds <= 0:
        return selfcheck
    return lease.deadline_seconds


# ---------------------------------------------------------------------------
# The shared admission seam — the one place the measure -> plan -> acquire ->
# re-plan sequence lives, so every in-process gate reader routes through it.
# ---------------------------------------------------------------------------


@dataclass
class GateRun:
    """A live handle to one admitted gate run, yielded by :func:`lease_for_gate_run`.

    ``lease`` is the granted :class:`GateLease` (or ``None`` on the flag-off
    rollback and on a declined admission, and never otherwise), unchanged in meaning from what the seam used
    to yield directly — a caller reads ``run.lease`` where it used to read the
    bare ``lease``. ``outcome`` is settable: a caller stamps how the run ended
    before the ``with`` block exits and the seam reads it into the recorded row;
    it defaults to ``"UNKNOWN"`` so a caller that never classifies (or a body
    that raises before it can) still yields a well-formed row. Mutable by design
    — the recorded outcome is whatever the body last set.

    ``runnability`` is set only when the seam *declined* the run: it carries the
    cached verdict that refused it, so the caller can synthesize its own failing
    result from the verdict rather than re-reading the cache. It is ``None`` on
    every admitted run, which is what makes ``run.runnability is not None`` the
    caller's short-circuit test.
    """

    lease: GateLease | None
    outcome: GateRunOutcome = "UNKNOWN"
    tests_seconds: float = 0.0
    runnability: GateRunnabilityVerdict | None = None

    @contextmanager
    def time_tests_command(self) -> Iterator[None]:
        """Bracket one pytest invocation, folding its wall time into ``tests_seconds``.

        The clock lives here rather than at the call site because this module
        already owns the run's other timings (``started_at`` /
        ``duration_seconds``), so a test patches one clock in one place and the
        gate runner needs no ``time`` import of its own.

        Folds with ``max``, not ``+``: a gate run can issue *two* pytest commands
        — the ``-x`` probe and :meth:`~maestro.evolution.EvolutionEngine._observe_gate`'s
        ``-x``-less enumerate re-run — and each runs under the *same* per-command
        deadline, so the quantity that budget must cover is the longest single
        command, never their sum. Stamped on exit even when the body raises (the
        ``finally``), since a killed suite's censored reading is evidence too.
        """
        start = time.monotonic()
        try:
            yield
        finally:
            self.tests_seconds = max(self.tests_seconds, time.monotonic() - start)


@contextmanager
def lease_for_gate_run(
    slot_dir: Path | None,
    *,
    runs_path: Path | None = None,
    cgroup_reserve: bool = False,
    admission_cap: bool = False,
    source: GateRunSource = "in_process",
    scope: GateScope = "full",
    budget_lease_owned: bool = False,
    selfcheck: int = 0,
    selfcheck_max: int = 0,
    verdict_path: Path | None = None,
) -> Iterator[GateRun]:
    r"""Admit one in-process gate run under the host execution lease, or no-op.

    The single admission seam every in-process gate reader (and the ``maestro
    gate-lease`` wrapper) routes through, so the measure -> plan -> acquire ->
    re-plan sequence lives in exactly one place and no caller re-derives it. The
    lone :class:`Path` ``|`` :data:`None` *slot_dir* carries both the flag and the
    directory, so the flag read is not restated at any reader; the keyword-only
    *runs_path* makes telemetry recording opt-in per caller (``None`` records
    nothing even when the lease is on). The keyword-only *cgroup_reserve* carries
    ``[loop] gate_lease_cgroup_reserve_enabled`` through to the post-admission
    :func:`plan_lease`; the pre-admission slot-budget call is reserve-independent
    (the in_flight-independent slot budget), so it is not passed there. The keyword-only
    *admission_cap* carries ``[loop] gate_lease_admission_cap_enabled`` to both
    :func:`plan_lease` calls (shrinking the slot budget) and lengthens the
    :func:`acquire_slot` wait to :data:`_CAPPED_ACQUIRE_WAIT_SECONDS` so a queued
    run can outlast a running suite — for out-of-process *source*\ s only; an
    in-process or preflight run (:data:`_UNCAPPED_SOURCES`) ignores it. The keyword-only *source*
    names which caller produced the recorded row (:data:`GateRunSource`); every caller
    but ``run_quality_gates`` passes its own value, so a run that spent the flat
    ``selfcheck`` constant — the sibling ``preflight`` / ``gate_check`` readers — or
    one that enforced no deadline at all — the hand-run ``maestro gate-lease``
    wrapper — cannot pollute :func:`derive_deadline`'s input and reset a converging
    escalation to the floor. The keyword-only *scope* names what suite the run's
    timed pytest command covered (:data:`GateScope`); every caller keeps the
    ``"full"`` default today, since nothing narrows a gate run yet, so the stamped
    field is universally ``"full"`` and no behaviour changes. It exists as the seam
    the one caller that will ever narrow its scope names that fact at, so the
    budget derivation can already refuse a reading that measured a different
    command.

    The keyword-only *budget_lease_owned* carries ``[loop]
    gate_budget_lease_owned_enabled``: with it ``True`` the grant's
    ``deadline_seconds`` is :func:`derive_deadline` over the *runs_path* ledger at
    the granted width, bounded by *selfcheck* / *selfcheck_max*; with it ``False``
    (or with no ledger to read) the grant carries the ``0`` sentinel and every
    reader resolves :func:`deadline_or_default` to its own ``selfcheck``, so the
    enforced budget is byte-identical to today.

    The keyword-only *verdict_path* carries ``[loop]
    gate_environment_oracle_enabled`` in the same single-``Path`` ``|`` ``None``
    shape *slot_dir* uses — the flag and the location in one argument, so the flag
    read is not restated at any reader. When it is given, the cached runnability
    verdict is read there (:func:`~maestro.gate_environment.read_verdict`)
    *before* the host is measured, and a verdict
    :func:`~maestro.gate_environment.declines_gate_run` accepts refuses the run:
    the seam yields ``GateRun(lease=None, runnability=verdict)`` having measured
    no host and taken no slot, and on exit records one ``admission="declined"``
    row carrying the caller's *source* and *scope* and the outcome the body
    stamped. The read is deliberately a *read* and never a refresh: probing would
    run the collector, which itself runs through this seam, so a refresh here
    would recurse. Everything else falls through to today's path byte-identically
    — a ``None`` *verdict_path*, a ``None`` verdict (nothing probed yet), and any
    verdict the predicate does not accept.

    That consult sits *below* the ``slot_dir is None`` rollback, so the binding
    is scoped by ``[loop] gate_lease_enabled`` as well as by its own flag: with
    the lease off this seam is a pure no-op by contract — it reads nothing and
    records nothing — and a declining verdict therefore does not refuse the run.
    That is deliberate rather than incidental: the lease-off rollback's value is
    that it restores the pre-lease path byte-for-byte, and a refusal issued from
    inside a seam that is supposed to be inert would make the rollback
    unreliable in exactly the situation it exists for. The cost is that turning
    the lease off also turns the refusal off, leaving the pre-133 behaviour (the
    gate runs, and reds honestly) — the safe direction.

    A ``None`` *slot_dir* means the lease is off — the flag-off rollback: yield a
    ``GateRun(lease=None)`` immediately, reading no host (no
    :func:`measure_host_capacity`), taking no slot (no :func:`acquire_slot`), and
    writing no row, so the reader's behaviour is byte-identical to today and
    :func:`worker_args` returns ``()``.

    Otherwise measure the host, plan the ``in_flight``-independent slot budget
    (:func:`plan_lease` with ``in_flight=0``; the slot budget never depends on
    ``in_flight``, so it is planned before the slot is held), and hold a concurrency slot for the body's lifetime.
    On a ``"slot"`` admission the worker grant is re-planned against
    :func:`count_in_flight` *minus one* — the caller's own slot is held now, so it
    is subtracted to get the *other* in-flight runs :func:`plan_lease` expects. On
    a ``"fallback"`` admission (every slot busy for the whole bounded wait) the
    one-worker floor grant is used holding no slot, so a saturated host is slowed
    rather than deadlocked; the ``in_flight`` recorded there is the full slot
    budget — a saturation marker taken from the already-known ``slots``, *not* a
    fresh :func:`count_in_flight`, so the fail-open path still reads no count. The
    :class:`GateRun` is yielded in both cases.

    On context exit — normal or exceptional — exactly one :class:`GateRunRecord`
    is appended to *runs_path* when it is given, stamping the measured
    ``started_at`` / ``duration_seconds``, the granted ``workers``, the observed
    ``in_flight``, the ``admission`` :func:`acquire_slot` yielded, the
    ``outcome`` the body last set, the ``tests_seconds`` the body timed through
    :meth:`GateRun.time_tests_command`, and the ``available_mib`` / ``bounded_by``
    measured at admission (stamped on both the ``"slot"`` and ``"fallback"``
    branches, and regardless of *cgroup_reserve*; only the ``"slot"`` branch
    plans its grant against them), plus the *source* and the ``deadline_seconds``
    actually granted (``0`` when the budget flag is off — the row then reads as
    censored at the reader's own ``selfcheck``), plus the *scope* the timed pytest
    command covered. The slot is
    then released (:func:`acquire_slot`
    releases in a ``finally``), so the record lands while the slot is still held.
    """
    if slot_dir is None:
        yield GateRun(lease=None)
        return
    started_at = datetime.now(UTC).isoformat()
    start = time.monotonic()
    if verdict_path is not None:
        verdict = read_verdict(verdict_path)
        if verdict is not None and declines_gate_run(verdict):
            # Refused before any measurement: no host read, no slot, no
            # subprocess. The caller reads the verdict off the handle and
            # synthesizes the failing result the run would have produced.
            declined = GateRun(lease=None, runnability=verdict)
            try:
                yield declined
            finally:
                if runs_path is not None:
                    record_gate_run(
                        runs_path,
                        GateRunRecord(
                            started_at=started_at,
                            duration_seconds=time.monotonic() - start,
                            workers=0,
                            in_flight=0,
                            admission="declined",
                            outcome=declined.outcome,
                            source=source,
                            scope=scope,
                        ),
                    )
            return
    capacity = measure_host_capacity()
    # The slot budget is in_flight-independent (plan_lease's slots ignore it),
    # so it is planned before the slot is held; the worker grant is re-planned
    # only after admission, once count_in_flight can see true concurrency.
    # The cap binds only out-of-process callers: an in-process run is invoked
    # synchronously from the loop's async code, where a long queue would stall
    # the event loop, so it keeps today's budget and wait.
    capped = admission_cap and source not in _UNCAPPED_SOURCES
    slots = plan_lease(capacity, in_flight=0, admission_cap=capped).slots
    wait = _CAPPED_ACQUIRE_WAIT_SECONDS if capped else _ACQUIRE_WAIT_SECONDS
    with acquire_slot(slot_dir, slots, wait_seconds=wait) as admission:
        wait_seconds = time.monotonic() - start
        if admission == "slot":
            # Our own slot is held now, so the count includes us — subtract one
            # to get the *other* in-flight runs plan_lease's in_flight expects.
            in_flight = max(0, count_in_flight(slot_dir, slots) - 1)
            lease = plan_lease(
                capacity,
                in_flight,
                cgroup_reserve=cgroup_reserve,
                admission_cap=capped,
            )
        else:
            # Fail-open under saturation: hold no slot, take the one-worker floor.
            # Every slot was busy for the whole bounded wait, so record the full
            # slot budget as the saturation marker rather than reading the count
            # again — the fail-open path holds no slot and reads no count.
            in_flight = slots
            lease = GateLease(workers=1, slots=slots)
        # The budget is derived *after* the width is granted: it is a fact about
        # what this ledger measured at this same width, so a narrower grant reads
        # a different (slower) history than a wide one. Off-flag — or with no
        # ledger to read — the grant keeps the 0 sentinel and every reader falls
        # back to its own selfcheck constant.
        if budget_lease_owned and runs_path is not None:
            lease = replace(
                lease,
                deadline_seconds=derive_deadline(
                    read_gate_runs(runs_path),
                    workers=lease.workers,
                    selfcheck=selfcheck,
                    selfcheck_max=selfcheck_max,
                ),
            )
        run = GateRun(lease=lease)
        try:
            yield run
        finally:
            if runs_path is not None:
                record_gate_run(
                    runs_path,
                    GateRunRecord(
                        started_at=started_at,
                        duration_seconds=time.monotonic() - start,
                        workers=lease.workers,
                        in_flight=in_flight,
                        admission=admission,
                        outcome=run.outcome,
                        available_mib=capacity.available_mib,
                        bounded_by=capacity.bounded_by,
                        source=source,
                        deadline_seconds=lease.deadline_seconds,
                        tests_seconds=run.tests_seconds,
                        scope=scope,
                        wait_seconds=wait_seconds,
                    ),
                )


# ---------------------------------------------------------------------------
# The read surface — the corruption-tolerant readers over gate_runs.jsonl and
# the pure aggregation that makes the lease falsifiable (proposal 126).
# ---------------------------------------------------------------------------


def read_gate_runs(path: Path) -> list[GateRunRecord]:
    """Parse every well-formed row from the gate-run telemetry ledger, oldest-first.

    The shared reader over ``gate_runs.jsonl`` — the corruption-tolerant line
    iteration the :func:`_parse_gate_run_dict` docstring already anticipates,
    wrapping that parser rather than introducing a new one. It **skips corrupt
    rows and never raises** (a malformed line anywhere, interior or trailing, is
    dropped, not propagated) and returns ``[]`` on a missing file or an
    ``OSError``. Order is oldest-first, matching the append order, so
    ``read_gate_runs(path)[-n:]`` is the tail window a reader takes. Mirrors
    :func:`maestro.audit_checks.load_all_gate_activations` line-for-line.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[GateRunRecord] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        record = _parse_gate_run_dict(row, source=path)
        if record is not None:
            records.append(record)
    return records


def read_recent_gate_runs(path: Path, limit: int) -> list[GateRunRecord]:
    """Parse only the newest *limit* well-formed rows, oldest-first.

    A count-bounded tail read: unlike :func:`read_gate_runs` — which hydrates
    *every* row — this ``json``-parses only the last *limit* non-empty lines, so
    a caller needing a short window does not pay the O(history) hydration cost.
    Corruption-tolerance matches the full reader: corrupt lines inside the tail
    window are dropped (never raised), so the result may hold fewer than *limit*
    rows; a missing file or ``OSError`` yields ``[]``, and ``limit <= 0`` yields
    ``[]``. Order is oldest-first, mirroring
    :func:`maestro.audit_checks.load_recent_gate_activations`.
    """
    if limit <= 0:
        return []
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[GateRunRecord] = []
    lines = [line for line in text.splitlines() if line.strip()]
    for line in lines[-limit:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        record = _parse_gate_run_dict(row, source=path)
        if record is not None:
            records.append(record)
    return records


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of *values* at *pct* (0-100). Pure, zero-I/O.

    An empty list is ``0.0`` and a single value is that value. Uses the
    nearest-rank method: the value at ordinal ``ceil(pct/100 * N)`` in the
    ascending sort (rank clamped to ``[1, N]``), so ``p95`` of a short list is
    its maximum. Written from scratch — there is no percentile utility in
    :mod:`maestro` — and kept beside the module's other pure policy functions.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    rank = math.ceil((pct / 100.0) * n)
    index = min(max(rank, 1), n) - 1
    return ordered[index]


#: The subset of :data:`GateRunOutcome` marking a red that host *contention*
#: manufactured rather than an answered failure — a killed worker's non-answer
#: (``TIMEOUT`` / ``UNAVAILABLE``, which includes an OOM-killed xdist worker's
#: crash report on its in-flight item) and the coverage shortfall such a kill leaves
#: behind when no item was in flight (``COVERAGE``). If the lease binds, this rate trends
#: toward zero. Keyed off the vocabulary this module already owns.
_RESOURCE_ARTIFACT_OUTCOMES: frozenset[GateRunOutcome] = frozenset(
    {"TIMEOUT", "UNAVAILABLE", "COVERAGE"},
)


def _measure_width_fit(records: list[GateRunRecord]) -> list[dict[str, object]]:
    """Bucket the fit reading — measured cost against granted budget — per width.

    One entry per distinct ``workers`` value observed among full-scope ``source ==
    "in_process"`` rows, ascending. Concurrency is the wrong axis for fit: the
    budget is *derived* per width, so whether it fits has to be read per width,
    and the quantity the deadline is enforced against is ``tests_seconds``, not
    the whole-slot ``duration_seconds`` the concurrency buckets aggregate. Every
    figure in a bucket reads the *same* population — in-process rows at that width
    whose :data:`GateScope` is ``"full"`` — so a narrowed run, which ran a
    different command under a budget of its own, can neither be counted at a
    full-suite width nor inflate its kill tally.

    ``p50_tests_seconds`` reuses :func:`_completed_tests_p50`, so the completed
    full-scope predicate the budget derivation reads is the same one the fit
    reading reports against — the two can never disagree about which rows count.
    ``p50_deadline_seconds`` medians the enforced budget over those same completed
    rows, *further* restricted to the ones carrying a positive budget (``0`` is a
    pre-change or flag-off row that enforced no budget of its own) — so on a width
    whose history mixes unbudgeted and budgeted rows the two tail windows need not
    cover the same rows, and ``fit`` is then a ratio of two overlapping but
    distinct populations rather than a paired reading. ``fit`` is their ratio, or
    ``None`` when either median is missing. ``censored`` counts the width's
    full-scope ``TIMEOUT`` rows — a killed run has no cost reading to fold in, but
    how often the budget was hit is exactly the complement of the fit figure.
    ``"declined"`` rows are excluded for the same reason they are kept out of the
    concurrency buckets: they were granted no width and ran no command, so they
    are a reading about neither cost nor budget.
    Pure, zero I/O.
    """
    full_scope = [
        r for r in records
        if r.source == "in_process"
        and r.scope == "full"
        and r.admission != "declined"
    ]
    widths = sorted({r.workers for r in full_scope})
    buckets: list[dict[str, object]] = []
    for workers in widths:
        at_width = [r for r in full_scope if r.workers == workers]
        p50_tests = _completed_tests_p50(records, workers=workers)
        budgets = [
            float(r.deadline_seconds)
            for r in at_width
            if r.outcome in _COMPLETED_TESTS_OUTCOMES
            and r.tests_seconds > 0
            and r.deadline_seconds > 0
        ]
        p50_deadline = _percentile(budgets[-_DEADLINE_P50_WINDOW:], 50)
        buckets.append(
            {
                "workers": workers,
                "count": len(at_width),
                "p50_tests_seconds": p50_tests,
                "p50_deadline_seconds": p50_deadline,
                "fit": (
                    p50_tests / p50_deadline
                    if p50_tests > 0 and p50_deadline > 0
                    else None
                ),
                "censored": sum(1 for r in at_width if r.outcome == "TIMEOUT"),
            },
        )
    return buckets


def measure_gate_contention(records: list[GateRunRecord]) -> dict[str, object]:
    """Aggregate gate-run telemetry into the lease's falsifiability figures.

    Pure, zero-I/O over an already-loaded ``list[GateRunRecord]`` (the caller
    reads them via :func:`read_recent_gate_runs`). Returns a report dict:

    - ``total`` — the number of rows aggregated.
    - ``buckets`` — one entry per distinct observed ``in_flight`` concurrency,
      ascending, each ``{"in_flight", "count", "p50_seconds", "p95_seconds"}``
      over that bucket's ``duration_seconds``, plus ``p50_wait_seconds`` over its
      ``wait_seconds`` — the share of that wall time spent queued for a slot,
      which the admission cap trades for contention. If serialising runs is the binding
      constraint, p95 under concurrency should converge toward the single-tenant
      p50 as the lease is turned on. ``"declined"`` rows are excluded: a
      sub-second refusal that measured no host and ran no gate is not a reading
      about contention, and leaving it in would drag the p50 down by counting
      runs that never competed for anything.
    - ``width_buckets`` — one entry per distinct ``workers`` width observed among
      full-scope ``source == "in_process"`` rows, ascending, each ``{"workers",
      "count", "p50_tests_seconds", "p50_deadline_seconds", "fit", "censored"}``.
      This is the *fit* reading: ``p50_tests_seconds`` is what the suite measurably
      cost at that granted width (:func:`_completed_tests_p50`, so completed
      full-scope rows only), ``p50_deadline_seconds`` the median budget those same
      completed rows were granted when they carried one, ``fit`` their ratio —
      below ``1.0`` means the derived budget covers the suite at the width the host
      actually grants — and ``censored`` the number of full-scope ``TIMEOUT`` rows
      at that width. ``fit`` is ``None`` when either median is unavailable (a
      pre-change or flag-off row records no budget, and an all-truncated width
      measures no cost). Sibling-source, wrapper and narrowed-scope rows are
      excluded: they ran a different argv, spent a flat constant, or covered a
      different suite, so neither their cost nor their budget is a reading about
      the budgeted full suite.
    - ``resource_artifact_rate`` — the fraction of *admitted* rows whose
      ``outcome`` is a contention-manufactured red
      (:data:`_RESOURCE_ARTIFACT_OUTCOMES`).
    - ``fallback_rate`` — the fraction of *admitted* rows admitted ``"fallback"``
      (every slot busy, fail-open), measuring how often the host saturated.
    - ``declined_rate`` — the fraction of rows admitted ``"declined"``: how often
      the cached runnability verdict refused a gate run before it spent a
      subprocess. This is the falsifiable evidence the oracle-to-lease binding
      fired at all; it is ``0.0`` whenever ``[loop]
      gate_environment_oracle_enabled`` is off, since the seam then reads no
      verdict.

    The two lease-falsifiability rates are denominated over the *admitted* rows
    only — a declined row measured no host, held no slot and ran no command, so
    counting it would silently deflate both figures in proportion to how often
    the oracle fired (an 80%-decline window would make a saturating host read
    five times calmer than it is). ``declined_rate`` alone is denominated over
    *total*, since "how much of the window the binding refused" is exactly the
    question it answers.

    All rates guard division by zero, so an empty record list — or one of nothing
    but declined rows — returns a well-formed zeroed report (``total`` the row
    count, empty ``buckets`` and ``width_buckets``, every rate ``0.0``).
    Co-located with the
    :data:`GateRunOutcome` / :data:`LeaseAdmission` vocabularies it keys off,
    adding no cross-module import.

    The bucketing below hashes and sorts ``in_flight`` and sorts
    ``duration_seconds``, which rests on every record having been *value*-
    validated at the parse boundary (:func:`_parse_gate_run_dict`), not merely
    on the dataclass annotation — a ``None`` or list concurrency read straight
    off a corrupt ledger line would raise here rather than being skipped there.
    The assertion makes that dependency fail loudly, and at the right place, for
    a future caller that builds records by some other route.
    """
    total = len(records)
    by_in_flight: dict[int, list[float]] = {}
    waits_by_in_flight: dict[int, list[float]] = {}
    for record in records:
        if record.admission == "declined":
            continue
        assert isinstance(record.in_flight, int)
        by_in_flight.setdefault(record.in_flight, []).append(record.duration_seconds)
        waits_by_in_flight.setdefault(record.in_flight, []).append(
            record.wait_seconds,
        )
    buckets: list[dict[str, object]] = [
        {
            "in_flight": in_flight,
            "count": len(durations),
            "p50_seconds": _percentile(durations, 50.0),
            "p95_seconds": _percentile(durations, 95.0),
            "p50_wait_seconds": _percentile(waits_by_in_flight[in_flight], 50.0),
        }
        for in_flight, durations in sorted(by_in_flight.items())
    ]
    width_buckets = _measure_width_fit(records)
    admitted = [r for r in records if r.admission != "declined"]
    admitted_total = len(admitted)
    artifact_count = sum(
        1 for r in admitted if r.outcome in _RESOURCE_ARTIFACT_OUTCOMES
    )
    fallback_count = sum(1 for r in admitted if r.admission == "fallback")
    declined_count = total - admitted_total
    return {
        "total": total,
        "buckets": buckets,
        "width_buckets": width_buckets,
        "resource_artifact_rate": (
            artifact_count / admitted_total if admitted_total else 0.0
        ),
        "fallback_rate": fallback_count / admitted_total if admitted_total else 0.0,
        "declined_rate": declined_count / total if total else 0.0,
    }

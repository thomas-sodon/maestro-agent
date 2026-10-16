#!/usr/bin/env bash
# Host-wide gate semaphore: run one quality-gate command (ruff / mypy / the test
# suite / quality-gates.sh) in a CPU-disjoint slot so concurrent gate runs from
# several checkouts or agents never oversubscribe the machine.
#
# The slot layout is not hard-coded: maestro.gate_capacity.measure_gate_slots()
# derives it from the CPUs this process may use (its affinity set grouped by SMT
# sibling, capped by any cgroup CPU quota) and the total memory bound (cgroup
# limit or MemTotal), with the same per-worker / per-run / reserve figures as the
# gate lease. A pinned slot is locked CPU by CPU (one lock file per CPU id), so
# two runs can never hold an overlapping CPU even if they planned different
# layouts; the command then runs under `taskset` on exactly those CPUs, so the
# lease (which reads the affinity, and scopes its own slot files by it) grants at
# most that width. Unpinned slots (CPU ids unknown, or a CFS quota rather than a
# cpuset limits the CPUs) are locked by index. `-n auto` is sized to the slot's
# admitted width via PYTEST_XDIST_AUTO_NUM_WORKERS when that width is known.
#
# Usage: scripts/gate-slot.sh <command...>
#   GATE_LOCK_DIR  per-host lock directory; every checkout and agent must share it
#                  (default: $HOME/.maestro/gate-slot-locks)
#   GATE_WAIT      seconds to wait for a free slot (default 300); on timeout exits
#                  75 = "all slots busy, re-run the same command"
# Exit codes: the gated command's own code; 75 = every slot busy; 125 = the wrapper
# itself cannot run (with a "gate-slot:" line on stderr) — distinct from the 2 that
# pytest and mypy use, so a red gate is never mistaken for a broken wrapper.
#   GATE_PYTHON    interpreter used to plan the slots (default python3.14)
# Requires: flock (util-linux); taskset to pin (without it runs unpinned, warned).
set -uo pipefail

[[ $# -eq 0 ]] && { echo "gate-slot: no command given" >&2; exit 125; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_DIR="${GATE_LOCK_DIR:-${HOME:-/tmp}/.maestro/gate-slot-locks}"
WAIT="${GATE_WAIT:-300}"
PY="${GATE_PYTHON:-python3.14}"

command -v flock >/dev/null 2>&1 || { echo "gate-slot: flock not found (install util-linux)" >&2; exit 125; }
mkdir -p "$LOCK_DIR" 2>/dev/null && [[ -w "$LOCK_DIR" ]] \
  || { echo "gate-slot: lock directory $LOCK_DIR is not writable" >&2; exit 125; }
LOG="$LOCK_DIR/gate-runs.log"
ts() { date -u +%FT%TZ; }

# Plan from *this script's* checkout: `-P` keeps the caller's cwd off sys.path so a
# `maestro/` there cannot shadow PYTHONPATH. Line 1 is the per-slot width (0 =
# unknown), line 2 "pin" or "claim" (claim the CPUs without confining the run to
# them), then one comma-joined CPU set per slot (empty = CPU ids unknown).
mapfile -t PLAN < <(PYTHONPATH="$ROOT" "$PY" -P -c '
from maestro.gate_capacity import measure_gate_slots
plan = measure_gate_slots()
print(plan.workers)
print("pin" if plan.pinned else "claim")
for cpus in plan.cpus:
    print(",".join(str(c) for c in cpus))
')
if [[ ${#PLAN[@]} -lt 3 || ! "${PLAN[0]}" =~ ^[0-9]+$ || ! "${PLAN[1]}" =~ ^(pin|claim)$ ]]; then
  echo "gate-slot: could not plan slots with $PY from $ROOT" >&2
  exit 125
fi
WORKERS="${PLAN[0]}"
PIN_MODE="${PLAN[1]}"
SLOT_CPUS=("${PLAN[@]:2}")
SLOTS=${#SLOT_CPUS[@]}
if (( WORKERS > 0 )); then
  export PYTEST_XDIST_AUTO_NUM_WORKERS="${PYTEST_XDIST_AUTO_NUM_WORKERS:-$WORKERS}"
fi
HAVE_TASKSET=1
command -v taskset >/dev/null 2>&1 || HAVE_TASKSET=0
if (( ! HAVE_TASKSET )) && [[ "$PIN_MODE" == pin && -n "${SLOT_CPUS[0]}" ]]; then
  echo "gate-slot: taskset not found; running unpinned (slots still bound concurrency)" >&2
fi

# Lock files for slot $1: one per CPU id it claims (pinned or not), so every plan
# draws on one host-wide CPU pool; one per index only when the CPU ids are unknown.
slot_lock_files() {
  local cpus="${SLOT_CPUS[$1]}"
  if [[ -z "$cpus" ]]; then
    echo "$LOCK_DIR/.gate.unpinned$1"
  else
    local c
    for c in ${cpus//,/ }; do echo "$LOCK_DIR/.gate.cpu$c"; done
  fi
}

HELD=()
release_held() {
  local fd
  for fd in "${HELD[@]}"; do exec {fd}>&-; done
  HELD=()
}
# Take every lock of slot $1 without blocking, or none of them. Returns 1 when a
# lock is busy, 2 when a lock file cannot be opened (a wrapper failure, not "busy").
try_slot() {
  local f fd
  while read -r f; do
    # The brace group keeps the new fd but restores stderr afterwards (a bare
    # `exec ... 2>/dev/null` would silence this shell and the gated command).
    if ! { exec {fd}>>"$f"; } 2>/dev/null; then
      release_held
      OPEN_FAILED="$f"
      return 2
    fi
    if ! flock -n "$fd"; then
      exec {fd}>&-
      release_held
      return 1
    fi
    HELD+=("$fd")
  done < <(slot_lock_files "$1")
  return 0
}

echo "$(ts) WAIT  pid=$$ slots=$SLOTS $PWD :: $*" >> "$LOG"
deadline=$((SECONDS + WAIT)); slot=""
while [[ -z "$slot" ]]; do
  for ((i = 0; i < SLOTS; i++)); do
    try_slot "$i"; got=$?
    if (( got == 0 )); then slot=$i; break; fi
    if (( got == 2 )); then
      echo "gate-slot: cannot open lock file $OPEN_FAILED" >&2
      exit 125
    fi
  done
  [[ -n "$slot" ]] && break
  if (( SECONDS >= deadline )); then
    echo "$(ts) BUSY  pid=$$ $PWD" >> "$LOG"
    echo "gate-slot: all $SLOTS gate slots busy (waited ${WAIT}s). Re-run the exact same command to keep waiting." >&2
    exit 75
  fi
  sleep 2
done

CPUS="${SLOT_CPUS[$slot]}"
PIN=()
if [[ "$PIN_MODE" == pin && -n "$CPUS" ]] && (( HAVE_TASKSET )); then
  taskset -c "$CPUS" true 2>/dev/null \
    || { echo "gate-slot: cannot pin to CPUs $CPUS (offline or outside this cgroup)" >&2; release_held; exit 125; }
  PIN=(taskset -c "$CPUS")
fi
echo "$(ts) START pid=$$ slot=$slot cpus=${CPUS:-unknown} mode=$PIN_MODE workers=$WORKERS $PWD :: $*" >> "$LOG"
# The command must not inherit the lock fds: a backgrounded grandchild would
# otherwise keep the slot locked after this run ends. This shell keeps them, so it
# must not exit while the command runs: TERM/HUP/INT are forwarded to the command
# and the locks are released only once it has exited. (A SIGKILL of this shell
# cannot be intercepted; stop gate runs with TERM.)
child=""; pending=""
trap 'if [[ -n "$child" ]]; then kill -TERM "$child" 2>/dev/null; else pending=1; fi' TERM HUP INT
(
  for fd in "${HELD[@]}"; do exec {fd}>&-; done
  exec "${PIN[@]}" "$@"
) <&0 &
child=$!
# A signal that arrived before the command started is delivered to it now.
[[ -n "$pending" ]] && kill -TERM "$child" 2>/dev/null
wait "$child"; rc=$?
while kill -0 "$child" 2>/dev/null; do wait "$child"; rc=$?; done
trap - TERM HUP INT
echo "$(ts) END   pid=$$ slot=$slot rc=$rc $PWD" >> "$LOG"
release_held
exit $rc

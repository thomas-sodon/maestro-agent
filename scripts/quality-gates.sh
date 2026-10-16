#!/usr/bin/env bash
# Single source of truth for Maestro's quality-gate command groups.
#
# Mirrors the four groups documented under "Quality Gates" in CLAUDE.md so the
# gate definitions never drift between the /quality-gates and /merge-main slash
# commands and the Stop hook (.claude/quality-gate-hook.sh). Each consumer
# selects a scope; the command strings live only here.
#
# Usage: scripts/quality-gates.sh [scope ...]
#   Scopes (default: all):
#     all      maestro + review + sdk + browser
#     unit     maestro + review + sdk            (skip browser)
#     maestro  the maestro/ package gates
#     review   the maestro_review/ package gates
#     sdk      the maestro_sdk/ package gates
#     browser  the browser tests
#     changed  auto-detect maestro/review/sdk from git changes; never browser;
#              no source changes -> run nothing and exit 0 (Stop-hook mode)
#
# Output: streams each group's tool output, then one summary line per group:
#   "GATE <name>: PASS" or "GATE <name>: FAIL(<stage>)"
# Exit: 0 if every in-scope group passed, 1 otherwise.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1
PY=python3.14

# Resolve the `maestro` CLI from *this* checkout rather than a globally-installed
# console script. `pip install -e` binds a single `~/.local/bin/maestro` — and a
# single `import maestro` — to one checkout per interpreter, so on a host running
# several sibling checkouts that one console script drives the *wrong* tree's CLI
# for every other checkout: its `gate-lease` / `gate-classify` then read the wrong
# `maestro.toml` and the wrong `maestro.gate_capacity` — the "CLI does not match
# the checkout" mismatch, and the exact failure the lease exists to avoid. This
# script already cd'd to the checkout root above, so cwd is `python -m`'s
# sys.path[0] and shadows any user-site editable install; running the CLI as a
# module (maestro/cli.py carries an `if __name__ == "__main__": main()` guard)
# self-resolves to the checkout under test, with no per-checkout venv to maintain
# and no reliance on PATH ordering. `$PY` is the same interpreter the other gates
# use, so no dependency beyond today's is introduced (the deps a bare console
# script would have needed are already required by the ruff/mypy/pytest gates).
# Defined as a shell function so the gate_maestro call sites below still read as
# plain `maestro ...`; a command-substitution/pipeline subshell inherits it.
maestro() { "$PY" -m maestro.cli "$@"; }

# --- canonical command groups (return 1=ruff, 2=mypy, 3=pytest on failure) ---
# classify_gate_run <rc> <captured-output-file>  ->  prints one outcome token
#
# Turns a *failed* core-suite run into a single GateObservationKind token so
# gate_maestro can tell a resource artifact (retry-worthy) from an answered red
# rather than blindly re-running the whole suite. The captured output is fed to
# `maestro gate-classify` (proposal 124's classifier, exposed as the
# `gate-classify` subcommand by proposal 126): an exit of 127 is the gate-lease
# wrapper's missing-binary status and an exit above 128 is its `128 + N`
# signal-death status — two non-answers no transcript can carry, so the runner
# names them explicitly via --kind. Any other status is derived from the
# transcript by --returncode. An empty or otherwise unreadable classification
# defaults to FAILED, the no-retry branch: the lease path has already proven
# `maestro` is on PATH, so a missing token must not silently re-open the serial
# amplifier. `${tok:-FAILED}` supplies that default explicitly because the script
# runs under `set -u`, where an unset token would abort the function instead.
classify_gate_run() {
  local rc="$1" out_file="$2" kind="" tok
  if [[ "$rc" -eq 127 ]]; then
    kind=UNAVAILABLE
  elif [[ "$rc" -gt 128 ]]; then
    kind=TIMEOUT
  fi
  if [[ -n "$kind" ]]; then
    tok="$(maestro gate-classify --tool tests --returncode "$rc" --kind "$kind" <"$out_file")"
  else
    tok="$(maestro gate-classify --tool tests --returncode "$rc" <"$out_file")"
  fi
  echo "${tok:-FAILED}"
}
gate_maestro() {
  # scripts/ is in scope: scripts/wtf.py is real Python that nothing else
  # type-checks, so leaving it out let it rot outside every enforced gate.
  $PY -m ruff check maestro/ tests/ scripts/ || return 1
  $PY -m mypy maestro/ scripts/              || return 2
  # -n auto parallelises the ~11.7k-test suite across cores (pytest-xdist);
  # pytest-cov combines per-worker coverage so the 100% gate is preserved.
  # Halves wall time (~200s -> ~100s) so a foreground run fits under the
  # agent's Bash timeout instead of being backgrounded and reaped.
  # --dist loadfile keeps a file's tests on one worker instead of xdist's
  # default `load` scheduler, which round-robins individual tests across
  # workers. That matters because tests/test_structural_guards.py hoists
  # whole-tree AST reads into module-scoped fixtures "so they happen once";
  # under `load` every worker that draws one of that module's tests rebuilds
  # the whole-tree parse, multiplying the cost. Combined with wall-clock
  # timeouts (timeout_method="thread") sized against the logical CPU count,
  # that can push the budget-tight structural-guard/fault-seeding cases past
  # their timeout under contention. loadfile amortises the shared fixture.
  #
  # Worker width comes from the gate execution lease (proposal 126) when
  # `[loop] gate_lease_enabled` is on: `maestro gate-lease` measures the host,
  # counts in-flight gate runs, and holds a concurrency slot so several checkouts
  # on one small host cannot each claim full width and oversubscribe it. stderr
  # is silenced so a checkout without `maestro` on PATH (probe -> 127) simply
  # falls through to today's `-n auto` rollback path.
  #
  # Lease path (flag on): the parallel run is captured (tee keeps it streaming
  # live to the caller) and, on any failure, classified rather than blindly
  # re-run. --dist loadfile bounds fixture-rebuild cost but not peak memory, so a
  # memory-constrained runner can still OOM-kill an xdist worker whose lost
  # coverage trips --cov-fail-under=100 even though every test passed — the exact
  # spurious failure that kept CI serial (PR #3385). classify_gate_run tells that
  # resource artifact (COVERAGE / TIMEOUT / UNAVAILABLE — the latter covering the
  # OOM-killed worker's "crashed while running" report) apart from a red that
  # already carries its whole answer (FAILED / COLLECTION_ERROR): the artifact
  # earns exactly one retry, re-acquiring a lease so it runs at a granted width
  # instead of full width, while an answered red is returned red with no second
  # run. A green parallel run returns immediately, so the common case keeps the
  # full speed-up.
  #
  # Rollback path (flag off): unchanged from today — the parallel `-n auto` run
  # short-circuits green on success, and on any failure the whole suite is re-run
  # serially so an OOM-killed worker's lost coverage can't fail the 100% gate
  # spuriously (PR #3385). This flag-off block is the rollback contract for
  # `[loop] gate_lease_enabled`; the blanket serial fallback disappears only when
  # the flag graduates and this branch is deleted, not before.
  if maestro gate-lease --enabled 2>/dev/null; then
    local out_file rc kind
    out_file="$(mktemp)"
    trap 'rm -f "$out_file"' RETURN
    maestro gate-lease -- $PY -m pytest tests/ -v {worker_args} 2>&1 | tee "$out_file"
    rc="${PIPESTATUS[0]}"
    [[ "$rc" -eq 0 ]] && return 0
    kind="$(classify_gate_run "$rc" "$out_file")"
    case "$kind" in
      COVERAGE|TIMEOUT|UNAVAILABLE)
        echo "gate_maestro: parallel run failed ($kind); retrying once at leased width (PR #3385)" >&2
        maestro gate-lease -- $PY -m pytest tests/ -v {worker_args} || return 3
        return 0
        ;;
      *)
        echo "gate_maestro: parallel run failed ($kind); answered red, no retry" >&2
        return 3
        ;;
    esac
  else
    $PY -m pytest tests/ -v -n auto --dist loadfile && return 0
    echo "gate_maestro: parallel run failed; re-running serially (PR #3385)" >&2
    $PY -m pytest tests/ -v || return 3
  fi
}
gate_review() {
  $PY -m ruff check maestro_review/ tests/test_maestro_review/ || return 1
  $PY -m mypy maestro_review/                                  || return 2
  $PY -m pytest tests/test_maestro_review/ -v --no-cov         || return 3
}
gate_sdk() {
  ( cd maestro_sdk && $PY -m ruff check src/ tests/ ) || return 1
  ( cd maestro_sdk && $PY -m mypy src/ )              || return 2
  ( cd maestro_sdk && $PY -m pytest tests/ -v )       || return 3
}
gate_browser() {
  $PY -m pytest tests/browser/ -v -m browser --no-cov --browser chromium || return 3
}

stage_name() {  # rc -> failing stage label
  case "$1" in
    1) echo ruff ;;
    2) echo mypy ;;
    3) echo pytest ;;
    *) echo run ;;
  esac
}

# --- scope -> group list (deduped, order-preserving) -------------------------
GATES=()
add_group() {
  local g existing found
  for g in "$@"; do
    found=0
    if [[ ${#GATES[@]} -gt 0 ]]; then
      for existing in "${GATES[@]}"; do
        [[ "$existing" == "$g" ]] && found=1 && break
      done
    fi
    [[ $found -eq 0 ]] && GATES+=("$g")
  done
}

detect_changed() {
  local changed src
  changed=$(
    {
      git diff --name-only HEAD 2>/dev/null
      git diff --name-only --cached 2>/dev/null
      git ls-files --others --exclude-standard 2>/dev/null
    } | sort -u
  )
  src=$(echo "$changed" | grep -E '\.(py|toml)$' || true)
  [[ -z "$src" ]] && return 0
  if echo "$src" | grep -qE '^maestro/'; then
    add_group maestro
  elif echo "$src" | grep -E '^tests/' | grep -qvE '^tests/test_maestro_review/'; then
    add_group maestro
  fi
  echo "$src" | grep -qE '^(maestro_review/|tests/test_maestro_review/)' && add_group review
  echo "$src" | grep -qE '^maestro_sdk/' && add_group sdk
  echo "$src" | grep -qE '^(pyproject\.toml|maestro\.toml|setup\.cfg)' && add_group maestro review
}

SCOPES=("$@")
[[ ${#SCOPES[@]} -eq 0 ]] && SCOPES=(all)
for s in "${SCOPES[@]}"; do
  case "$s" in
    all)     add_group maestro review sdk browser ;;
    unit)    add_group maestro review sdk ;;
    maestro) add_group maestro ;;
    review)  add_group review ;;
    sdk)     add_group sdk ;;
    browser) add_group browser ;;
    changed) detect_changed ;;
    *) echo "quality-gates: unknown scope '$s' (treating as 'all')" >&2
       add_group maestro review sdk browser ;;
  esac
done

# --- run -------------------------------------------------------------------
FAILED=0
SUMMARY=()
if [[ ${#GATES[@]} -gt 0 ]]; then
  for g in "${GATES[@]}"; do
    echo "=== Quality gates: $g ==="
    "gate_$g"
    rc=$?
    if [[ $rc -eq 0 ]]; then
      SUMMARY+=("GATE $g: PASS")
    else
      SUMMARY+=("GATE $g: FAIL($(stage_name "$rc"))")
      FAILED=1
    fi
  done
fi

# No in-scope gates ran (e.g. 'changed' with no source changes) -> stay silent,
# matching the Stop hook's no-op exit-0 contract. Only emit the summary/OVERALL
# block when at least one gate actually ran.
if [[ ${#GATES[@]} -gt 0 ]]; then
  echo
  printf '%s\n' "${SUMMARY[@]}"
  if [[ $FAILED -eq 0 ]]; then
    echo "OVERALL: PASS"
  else
    echo "OVERALL: FAIL"
  fi
fi
exit $FAILED

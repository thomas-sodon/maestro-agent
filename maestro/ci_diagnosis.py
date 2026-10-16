"""CI-failure diagnosis — turn a failing check's log into a concrete excerpt.

On a CI-red remediation lap the loop can name the failing check (``quality``)
but never reads *why* it is red: the red-root diagnostic runs only ruff → mypy,
which excludes pytest, coverage, browser, and the sibling suites where nearly
every real CI failure lives. So the recorded blocker degrades to "look to
pytest, a CI-specific check" and the lap re-laps without a concrete fix target.

This module is the missing read, kept pure. :func:`extract_first_errors` scans a
failing-run log and returns the first concrete error lines (``FAILED …`` /
``error:`` / a pytest assertion line), bounded to a small cap.
:func:`diagnose_ci_failure` pairs those lines with each failing check name,
calling its injected ``read_log`` seam exactly once — that callable is the ONLY
side-effect seam, so the diagnosis is deterministic under test with no I/O of its
own. :func:`render_ci_failure_section` renders the ``## CI Failure`` artifact
block, mirroring :func:`maestro.red_root.render_remote_ci_section`.

It is also the single owner of *whether* a ``statusCheckRollup`` item is failing:
:data:`FAILING_CONCLUSIONS` / :data:`SUCCESS_CONCLUSIONS` are the canonical
lower-cased vocabulary, :func:`is_failing_conclusion` / :func:`is_success_conclusion`
case-normalise a rollup ``conclusion`` / ``state`` before testing it, and
:func:`rollup_item_name` reads an item's check name. :mod:`maestro.vcs` and
:mod:`maestro.red_root` route through these rather than keeping hand-mirrored
copies, so a terminal ``error`` / ``startup_failure`` can never read as
still-running because one copy drifted from the other.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Literal

# ---------------------------------------------------------------------------
# CI status-check classification vocabulary (the single owner)
# ---------------------------------------------------------------------------

#: Check-run ``conclusion`` / rollup ``state`` values that mean the check
#: *failed* — the canonical, lower-cased failure vocabulary. This is the single
#: owner of the "did this check fail?" decision: :mod:`maestro.vcs` (uppercase
#: rollup states) and :mod:`maestro.red_root` (lower-cased check-run conclusions)
#: both test membership through :func:`is_failing_conclusion` rather than keeping
#: a private copy, so a terminal ``error`` / ``startup_failure`` can never read as
#: still-running because one hand-mirrored copy drifted from the other.
FAILING_CONCLUSIONS: frozenset[str] = frozenset(
    {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "startup_failure",
        "error",
    },
)

#: Check-run conclusions that concluded *without* failing (success / neutral /
#: skipped) — the canonical, lower-cased success vocabulary. A rollup all of whose
#: non-advisory items are success-conclusions (and at least one present) is green.
SUCCESS_CONCLUSIONS: frozenset[str] = frozenset({"success", "neutral", "skipped"})


def is_failing_conclusion(value: str) -> bool:
    """Return True when *value* is a failing check conclusion/state.

    Case- and whitespace-normalises *value* before testing membership in
    :data:`FAILING_CONCLUSIONS`, so an uppercase rollup ``state`` (``"FAILURE"``)
    and a lower-cased check-run ``conclusion`` (``"failure"``) resolve
    identically. Pure.
    """
    return value.strip().lower() in FAILING_CONCLUSIONS


def is_success_conclusion(value: str) -> bool:
    """Return True when *value* concluded without failing (success/neutral/skipped).

    Case- and whitespace-normalises *value* before testing membership in
    :data:`SUCCESS_CONCLUSIONS`, mirroring :func:`is_failing_conclusion`. Pure.
    """
    return value.strip().lower() in SUCCESS_CONCLUSIONS


def rollup_item_name(item: Mapping[str, object]) -> str:
    """Return a ``statusCheckRollup`` item's check name.

    ``gh pr view --json statusCheckRollup`` exposes the name under ``name`` for
    check runs and ``context`` for legacy status contexts; read both, falling
    back to ``""``. Pure.
    """
    return str(item.get("name") or item.get("context") or "")


#: The three remote-CI states the check-run classifier distinguishes. ``FAILING``
#: is a *concrete fix target* (a concluded check reported a failure); ``RUNNING``
#: is honestly not-mine-this-turn (checks queued/in-progress, none concluded);
#: ``GREEN`` means every check concluded good (the red, if any, lives elsewhere).
CIStatus = Literal["FAILING", "RUNNING", "GREEN"]


@dataclasses.dataclass(frozen=True)
class CheckRun:
    """One remote check-run: its display name and lowered conclusion.

    :attr:`conclusion` is the check-run's concluded result, lower-cased
    (``"failure"``, ``"success"``, …), or an *unconcluded* marker — a status
    like ``"queued"`` / ``"in_progress"`` or ``""`` — when the check has not
    finished. :func:`classify_ci_conclusions` reads only :attr:`conclusion`;
    :attr:`name` names the check for the failing-check surface.
    """

    name: str
    conclusion: str


def classify_ci_conclusions(check_runs: Sequence[CheckRun]) -> CIStatus:
    """Classify remote check-runs into a single :data:`CIStatus`.

    Splits a concluded *failure* (a real fix target) from a still-*running* CI
    (honestly not-mine-this-turn) so the red-root lap can target the one and
    stand down on the other:

    * ``FAILING`` — any check's conclusion is a :func:`is_failing_conclusion`.
    * ``GREEN`` — at least one check and *every* check concluded a
      :func:`is_success_conclusion` (success / neutral / skipped).
    * ``RUNNING`` — otherwise: some check is queued / in-progress / unconcluded
      and none has failed (an empty sequence is ``RUNNING`` — nothing concluded).

    Pure: reads only the check-runs' conclusion strings, case-normalised through
    the :data:`FAILING_CONCLUSIONS` / :data:`SUCCESS_CONCLUSIONS` owner.
    """
    if any(is_failing_conclusion(run.conclusion) for run in check_runs):
        return "FAILING"
    if check_runs and all(
        is_success_conclusion(run.conclusion) for run in check_runs
    ):
        return "GREEN"
    return "RUNNING"


def failing_check_names(check_runs: Sequence[CheckRun]) -> tuple[str, ...]:
    """Return the de-duplicated names of check-runs whose conclusion failed.

    A check-run contributes its :attr:`~CheckRun.name` iff its conclusion is a
    :func:`is_failing_conclusion` and the name is non-empty. Order-preserving and
    de-duplicated, so the surfaced fix targets read cleanly. Pure.
    """
    seen: dict[str, None] = {}
    for run in check_runs:
        if is_failing_conclusion(run.conclusion) and run.name:
            seen.setdefault(run.name, None)
    return tuple(seen)


#: Hard cap on the number of error lines :func:`extract_first_errors` keeps —
#: the excerpt is a first-error signpost, not the whole log, so a bounded head is
#: enough (context-bloat cap: this text reaches an artifact and a journal entry).
_MAX_ERROR_LINES = 5

#: Hard per-line char cap — a single log line (a long assertion repr, a stack
#: frame) can be huge, so each kept line is truncated before it can bloat the
#: artifact/journal it lands in.
_MAX_LINE_CHARS = 200

#: A log line is a concrete first-error signpost when it matches any of: a pytest
#: ``FAILED`` summary/inline, a case-insensitive ``error:`` diagnostic (lowercase
#: ruff/mypy ``error:`` *and* a GitHub step's capitalized ``Error: Process
#: completed with exit code 1``), a pytest assertion-detail line (a standalone
#: ``E`` followed by ≥3 spaces, per pytest's left-margin format), or an
#: ``AssertionError``. ``\bE {3,}\S`` uses a word boundary so a mid-word
#: ``…E   `` (e.g. ``THE   value``) does not false-match; only the ``error:``
#: alternative carries ``(?i:…)`` so the capital-``E`` margin token and the
#: ``FAILED`` / ``AssertionError`` tokens stay case-exact.
_ERROR_LINE_RE = re.compile(r"FAILED\b|(?i:error:)|AssertionError|\bE {3,}\S")


@dataclasses.dataclass(frozen=True)
class CIFailure:
    """One failing CI check and its first concrete error lines.

    :attr:`check_name` is the failing check's name (``""`` when the FAILING
    rollup named no check — the unnamed-FAILING shape). :attr:`first_error_lines`
    is the bounded, per-line-capped head of concrete error lines pulled from the
    failing-run log; it may be empty when the log was unreadable or carried no
    recognisable error line.
    """

    check_name: str
    first_error_lines: tuple[str, ...]


def extract_first_errors(log_text: str) -> tuple[str, ...]:
    """Return the first concrete error lines from *log_text*, bounded.

    Scans *log_text* line by line and keeps each stripped, non-empty line that
    matches :data:`_ERROR_LINE_RE` — a pytest ``FAILED`` summary, a ruff/mypy
    ``error:`` diagnostic, a pytest assertion-detail line, or an
    ``AssertionError`` — truncating each to :data:`_MAX_LINE_CHARS` and stopping
    after :data:`_MAX_ERROR_LINES`. Returns ``()`` when no line matches. Pure.
    """
    lines: list[str] = []
    for raw in log_text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if _ERROR_LINE_RE.search(stripped):
            lines.append(stripped[:_MAX_LINE_CHARS])
            if len(lines) >= _MAX_ERROR_LINES:
                break
    return tuple(lines)


def diagnose_ci_failure(
    failing_checks: Sequence[str],
    read_log: Callable[[], str],
) -> tuple[CIFailure, ...]:
    """Pair the failing-run log's first error lines with each failing check.

    Returns ``()`` when *failing_checks* is empty — nothing failed, so the log is
    never fetched. Otherwise calls *read_log* exactly once (the only side-effect
    seam), extracts the first concrete error lines via
    :func:`extract_first_errors`, and returns one :class:`CIFailure` per failing
    check carrying those lines. *read_log* reads only the *first* failing run's
    log, so the one excerpt is attached to every check as a shared best-effort
    signpost: when the failing checks span multiple workflow runs it names the
    first run's error, not each check's own.
    """
    if not failing_checks:
        return ()
    error_lines = extract_first_errors(read_log())
    return tuple(
        CIFailure(check_name=name, first_error_lines=error_lines)
        for name in failing_checks
    )


def render_ci_failure_section(failures: Sequence[CIFailure]) -> str:
    """Render the ``## CI Failure`` artifact block for *failures*.

    Returns ``""`` when *failures* is empty, so a caller can unconditionally
    append the section and stay byte-identical on the empty path (mirroring
    :func:`maestro.red_root.render_remote_ci_section`). Otherwise renders a
    ``## CI Failure`` heading followed by one bullet per failing check and its
    indented error lines; a check with no extracted lines notes that explicitly.
    """
    if not failures:
        return ""
    lines = ["## CI Failure"]
    for failure in failures:
        header = failure.check_name or "(unnamed check)"
        lines.append(f"- {header}:")
        if failure.first_error_lines:
            lines.extend(f"  - {line}" for line in failure.first_error_lines)
        else:
            lines.append("  - (no concrete error line extracted)")
    return "\n".join(lines) + "\n"

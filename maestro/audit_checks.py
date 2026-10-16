"""Mechanical self-audit checks — deterministic rules over behavioral records.

Pure functions only. No LLM calls — with two I/O exceptions:
:func:`is_branch_merged_into_main` runs a ``git merge-base`` subprocess
to detect already-merged branches, and
:func:`check_evolution_impact_reviews` reads a small surfaced-set
sidecar (``impact_review_surfaced.json``) so it never re-raises the same
merged evolution (the agent records the set, only for findings that
survive the cycle's final clip). Both are isolated so callers remain
testable via mock / ``mem_fs``. Each check takes the current
self-model plus a behavioral record (recent tasks, success rate) and
returns a list of :class:`AuditFinding` — one finding per rule hit.

The rules are self-extinguishing: a finding re-appears next cycle only
while the underlying condition still holds. Once Claude's next
``update_self_model`` call integrates a finding (adds it to
``capabilities_known`` / ``limitations_known`` / etc.) the rule stops
matching. A mismatched claim that was never integrated keeps firing —
which is the whole point.

Two of the keyword-based rules use prefix matching against tokenized
capability / stability claims so that inflections (``effective``,
``effectively``, ``effectiveness``) all trigger while explicit negations
(``ineffective``, ``unstable``, ``unreliable``) do not. The repeated-
target rule (rule 4) is keyword-free — it counts GitHub issue references
in task goals to surface Sisyphean retargeting of the same issue. The
stalled-question rule (rule 5) and the
stale-self-model-entry rule (rule 6) are the deferred rules — rule 5
inspects ``QuestionResolution`` rows; rule 6 cross-checks
``identity_statements`` / ``capabilities_known`` / ``values`` against a
recent textual corpus to surface entries that have stopped influencing
downstream output. Both yield their slots to the four legacy rules
under the round-robin clip (see :data:`_DEFERRED_RULES`).

Additionally, this module provides standalone helpers used by ``generate_task``:

- :func:`classify_module_family` — extracts the target module family from
  a task history entry by matching ``maestro/<module>.py`` /
  ``tests/test_<module>.py`` path patterns embedded in the task's combined
  text fields (``goal``, ``result_summary``, ``key_findings``, ``branch``,
  plan-step descriptions/results, errors), falling back to keyword and
  bare-module-name matching.

Proposal 147 Step 9 split three rule families into siblings:
:mod:`maestro.audit_checks_settlement` (the settled-verdicts ledger),
:mod:`maestro.audit_checks_calibration` (premise, prediction, reflection-bet
and calibration measures) and :mod:`maestro.audit_checks_phantom` (the
phantom / unconfirmed-landing rules and handoff delivery). The registry and
known-state formatter stay here.
"""

from __future__ import annotations

import bisect
import json
import logging
import re
import subprocess
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, cast, get_args, runtime_checkable

from maestro.consciousness import (
    Consciousness,
    SelfModel,
    _normalize_question,
    claim_key,
    limitation_question_candidate,
)
from maestro.fileutil import atomic_write, file_lock
from maestro.lexicon import extract_coinages
from maestro.memory import (
    AuditResult,
    EvolutionPlan,
    Memory,
    QuestionResolution,
    RejectionRecord,
    TaskRecord,
    VersionArchiveBackend,
    VersionRecord,
    compute_curation_fingerprint,
)
from maestro.narration_cues import (
    NOOP_ADMISSION_PHRASES as _NOOP_ADMISSION_PHRASES,
)
from maestro.narration_cues import (
    SETTLED_VERDICT_LANDING_PHRASES as _SETTLED_VERDICT_LANDING_PHRASES,
)
from maestro.outcome_grounding import extract_self_committed_shas
from maestro.plan_feedback import has_unaddressed_feedback, unaddressed_feedback
from maestro.plan_grounding import PLANNER_READ_TAG, PLANNER_UNREAD_TAG
from maestro.pr_ledger import DEFAULT_MAX_ATTEMPTS_PER_HEAD, PRLedgerBackend
from maestro.pr_ledger import format_stuck_summary as _format_stuck_summary
from maestro.pr_remediation_state import Marker
from maestro.prompts.curate_plan import curate_input_fits
from maestro.remediation_tracker import (
    StandDownVerdictBackend,
    _read_recent_task_tail,
    format_owed_act_divergence,
    is_owned_obsolete_row,
)
from maestro.search import tokenize

logger = logging.getLogger(__name__)

# Cap on findings surfaced in a single audit cycle. Must not exceed
# consciousness.Consciousness.MAX_AUDIT_OPEN_QUESTIONS (currently 4) — findings
# beyond the sub-cap are silently dropped from open_questions on save, so
# they appear in the journal events and audit_history but never reach the
# INTEGRATE/REFUTE/KEEP prompt, creating orphaned findings that fire every
# cycle without ever being addressed.  A cross-module invariant test
# (test_findings_cap_fits_open_questions_subcap) guards against future drift.
# The deferred rules yield their slots via :data:`_DEFERRED_RULES`, but that
# yielding does *not* guarantee one finding per legacy rule: five non-deferred
# rules contend for these four slots, so the alphabetically last
# (``success_claim_vs_rate``) is starved when all five fire — see
# :data:`_DEFERRED_RULES` and
# ``test_run_all_checks_round_robin_starves_last_legacy_rule``.
MAX_FINDINGS_PER_CYCLE = 4

# Per-rule cap. Prevents a single rule (e.g. every capability matching a
# success keyword) from monopolizing the cycle's findings budget.
MAX_FINDINGS_PER_RULE = 2

# Max ``task_id`` cites per finding. Keeps statements compact while still
# providing enough provenance to trace the behavioural evidence.
MAX_CITES_PER_FINDING = 5

# Minimum number of recent tasks required before rule 1 fires. Below this,
# the success rate is too noisy to draw conclusions from.
_MIN_TASKS_FOR_SUCCESS_CHECK = 5

# Success-rate threshold below which capability claims that boast
# effectiveness are contradicted by the record.
_SUCCESS_RATE_THRESHOLD = 0.6

# Minimum trailing failure streak before rule 2 fires.
_FAILURE_STREAK_THRESHOLD = 3

# Minimum fingerprint repetition before rule 3 fires.
_ERROR_REPETITION_THRESHOLD = 3

# Minimum number of distinct tasks whose ``goal`` mentions a single
# GitHub issue number before rule 4 (``repeated_target_attempt``)
# fires. Counts any-outcome appearances over the audit window so the
# rule surfaces a Sisyphean retargeting pattern as a self-model
# limitation for the agent to integrate.
_REPEATED_TARGET_THRESHOLD = 3

# Regex matching ``#NNN`` GitHub issue references — used by
# :func:`extract_issue_numbers` below. Single source of truth for rule 4
# and the terrain-exhaustion precondition check in :mod:`maestro.agent`,
# which import the helper from this module to avoid duplicating the regex.
_ISSUE_NUMBER_RE = re.compile(r"#(\d+)")


def extract_issue_numbers(text: str) -> set[int]:
    """Return the set of distinct GitHub issue numbers referenced in *text*.

    Set-valued so callers that count ``per-task`` (as opposed to
    ``per-mention``) get the right semantics for free — a goal like
    ``"fix #1566 see also #1566"`` contributes ``{1566}``, not two
    increments. Rule 4 and the terrain-exhaustion check rely on this.
    """
    return {int(m) for m in _ISSUE_NUMBER_RE.findall(text)}


#: Matches candidate-task goals whose objective is to open or merge a pull
#: request.  ``\bpr\b`` only matches the standalone token, so identifiers like
#: ``create_pr`` or ``tend_evolution_prs`` do not trip the filter.  The match
#: is deliberately broad: it can also drop a benign *investigation* goal that
#: merely mentions "open a pull request".  That over-match is acceptable — the
#: structural ``tool_gating`` guard is the primary defense, so a wrongly
#: dropped goal is low-harm (logged, never executed), whereas a missed
#: PR-opening goal is not.
_PR_MUTATION_GOAL_RE = re.compile(
    r"""
    (?:gh\s+pr\s+(?:create|merge|ready))             # gh CLI PR mutations
    | (?:\bvia\s+(?:an?\s+)?(?:pr|pull\s+request)\b)  # "...via (a) PR"
    | (?:\b(?:open|create|submit|raise|file|merge)\s+
         (?:an?\s+|the\s+|this\s+)?
         (?:pr|pull\s+request)\b)                      # "open a PR" / "merge the PR"
    """,
    re.IGNORECASE | re.VERBOSE,
)


def goal_opens_or_merges_pr(text: str) -> bool:
    """Whether a candidate task *goal* describes opening or merging a PR.

    Deterministic, zero-SDK-cost.  ``generate_task`` uses this as a
    belt-and-suspenders complement to the structural ``tool_gating`` Bash
    guard: a goal whose objective is to open or merge a pull request would, if
    executed, try to bypass the human-in-the-loop approval gate (issue #2280),
    so it is dropped before ever entering ``task_queue.json``.  The structural
    guard still blocks the *action* if such a goal slips through.
    """
    return bool(_PR_MUTATION_GOAL_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Task-shape diversity mirror — zero-SDK-cost regex classifier + formatter
# ---------------------------------------------------------------------------

# Shape vocabulary. Each shape is a short label injected into the task-
# generation prompt so the LLM can see what it has been choosing. Order
# matters only for documentation; the classifier short-circuits on first
# match, so more specific patterns come first.
TaskShape = Literal[
    "github_issue",
    "branch_promotion",
    "module_extension",
    "measurement",
    "remediation",
    "external_request",
    "construction",
    "investigation",
    "other",
]

# Keywords signalling PR-remediation tasks. Checked before ``github_issue``
# so that ``Fix PR #1234 ...`` classifies as ``remediation`` rather than
# being captured by the ``#\d+`` pattern in ``github_issue``.
_REMEDIATION_RE = re.compile(
    r"\bremediat|\bfix\s+(?:the\s+)?pr\b",
    re.IGNORECASE,
)

# Interrogative-initial goals or goals containing ``?`` suggest an
# externally-posed question (user request, curiosity probe).
_EXTERNAL_REQUEST_RE = re.compile(
    r"^(?:what|how|why|when|where|who|which|is|are|can|do|does|should|would|could)\b"
    r"|\?",
    re.IGNORECASE,
)

# Building verbs: create new artefacts. Checked before ``investigation``
# so that mixed build-read goals classify as construction.
_CONSTRUCTION_RE = re.compile(
    r"\bbuild\b|\bcreate\b|\bimplement(?:ing|s|ed)?\b|\badd\s+(?:a|an|the|new)\b",
    re.IGNORECASE,
)

# Read-only / analysis verbs. Tasks matching these WITHOUT co-occurring
# construction verbs are classified as ``investigation``. Because
# ``construction`` appears earlier in ``_SHAPE_PATTERNS``, the ordering
# naturally enforces the "no construction verbs" constraint.
_INVESTIGATION_RE = re.compile(
    r"\bread\b|\bcompute\b|\banalyz|\bcheck\b|\bcompare\b"
    r"|\binvestigat|\bexamin|\breview\b|\bscan\b",
    re.IGNORECASE,
)

_SHAPE_PATTERNS: list[tuple[TaskShape, re.Pattern[str]]] = [
    # -- Remediation: PR-specific fix tasks (before github_issue to
    #    avoid ``#\d+`` capturing ``Fix PR #NNN``).  Shape is not an
    #    origin signal: origin-scoped metrics must read
    #    ``TaskRecord.source``, so this ordering never decides which
    #    records count as external.
    ("remediation", _REMEDIATION_RE),
    (
        "github_issue",
        re.compile(
            r"#\d+|github\s+issue|oldest\s+issue|open\s+issue",
            re.IGNORECASE,
        ),
    ),
    (
        "branch_promotion",
        re.compile(
            r"branch\s+promot|source_task_id|task\s+branch.*promot|promot.*task\s+branch",
            re.IGNORECASE,
        ),
    ),
    (
        "module_extension",
        re.compile(
            r"extend(?:ing|s)?\b.*module|add(?:ing|s)?\b.*(?:to|into)\b.*module"
            r"|module.*extend|new\s+(?:method|function|class)\b.*(?:to|in)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "measurement",
        re.compile(
            r"\baudit\b|\bmeasure\b|\bcount\b|\breporting\b|\breport\s+on\b"
            r"|\blisting\b|\blist\s+(?:all|every|each)\b|\bcross-ref",
            re.IGNORECASE,
        ),
    ),
    # -- External request: question-shaped goals.
    ("external_request", _EXTERNAL_REQUEST_RE),
    # -- Construction: building verbs (before investigation so mixed
    #    build-read goals classify as construction, enforcing the
    #    "investigation only when no construction verbs" constraint).
    ("construction", _CONSTRUCTION_RE),
    # -- Investigation: read-only/analysis verbs.
    ("investigation", _INVESTIGATION_RE),
]

# Concrete shapes are every TaskShape member except the residual "other"
# catch-all.  Derived from the Literal so adding a shape to TaskShape (and
# _SHAPE_PATTERNS) automatically propagates to positive-routing fallbacks —
# no second hand-maintained list to keep in sync.
_CONCRETE_SHAPES: tuple[str, ...] = tuple(
    s for s in get_args(TaskShape) if s != "other"
)


def classify_task_shape(goal: str) -> TaskShape:
    """Classify a task goal string into a shape category.

    Nine shapes total — ``github_issue``, ``branch_promotion``,
    ``module_extension``, ``measurement``, ``remediation``,
    ``external_request``, ``construction``, ``investigation``, and
    ``other`` (residual catch-all).

    Pure regex classifier — zero SDK cost. Returns the first matching
    shape or ``"other"`` as a fallback. Used by
    :func:`format_task_shape_summary` to build the distribution summary
    injected into the task-generation prompt.
    """
    if not goal:
        return "other"
    for shape, pattern in _SHAPE_PATTERNS:
        if pattern.search(goal):
            return shape
    return "other"


def is_branch_merged_into_main(branch: str) -> bool:
    """Check whether *branch* HEAD is already an ancestor of ``origin/main``.

    Runs ``git merge-base --is-ancestor <branch> origin/main`` via
    subprocess. Returns ``True`` when exit code is 0 (branch merged),
    ``False`` on non-zero exit (not merged) or any subprocess error.

    Subprocess errors default to ``False`` (branch retained) so git
    failures never accidentally hide promotable work.

    This is the one non-pure function in the module — it performs I/O
    via subprocess. Isolated here so callers remain testable via mock.
    """
    if not branch:
        return False
    try:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, "origin/main"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return True
    except subprocess.CalledProcessError:
        return False
    except (subprocess.TimeoutExpired, OSError):
        logger.warning(
            "is_branch_merged_into_main: subprocess error for %s",
            branch,
            exc_info=True,
        )
        return False


def _promotable_candidates(
    tasks: list[TaskRecord],
    cited_source_task_ids: set[str],
) -> list[TaskRecord]:
    """Return successful task branches not yet cited by any plan's ``source_task_id``.

    Branches whose HEAD is already an ancestor of ``origin/main`` (i.e.
    already merged) are excluded — promoting them would be a no-op.

    Deduplicated by ``task_id`` to mirror the set-based dedup in the sibling
    :func:`count_unintegrated_tasks` (the canonical dedup for this question),
    so a repeated ``task_id`` in the window cannot inflate the promotable
    count or surface the same candidate twice. Oldest-first input order is
    preserved — :func:`format_promotable_summary` reads "most recent" as the
    last entry and reverses for its top-3, so reordering here would corrupt
    it. The first accepted occurrence of a ``task_id`` is kept; repeats share
    a ``task_id`` and denote the same underlying task.
    """
    seen: set[str] = set()
    candidates: list[TaskRecord] = []
    for t in tasks:
        if (
            t.outcome == "success"
            and isinstance(t.branch, str)
            and t.branch
            and isinstance(t.task_id, str)
            and t.task_id
            and t.task_id not in cited_source_task_ids
            and t.task_id not in seen
            and not is_branch_merged_into_main(t.branch)
        ):
            seen.add(t.task_id)
            candidates.append(t)
    return candidates


def count_promotable_tasks(
    tasks: list[TaskRecord],
    cited_source_task_ids: set[str],
) -> int:
    """Count successful task branches not yet cited by any plan's ``source_task_id``.

    Pure function, zero SDK cost. Cross-references *tasks* that completed
    successfully (``outcome == "success"``) and have a
    non-empty ``branch`` against *cited_source_task_ids* — the set of
    ``source_task_id`` values across all plans, which the caller supplies
    via ``Memory.get_cited_source_task_ids()``. Returns the count of tasks
    with no matching follow-up plan.
    """
    return len(_promotable_candidates(tasks, cited_source_task_ids))


def format_promotable_summary(
    tasks: list[TaskRecord],
    cited_source_task_ids: set[str],
    threshold: int,
) -> str:
    """Build a PROMOTABLE BACKLOG prompt section when the count reaches *threshold*.

    Returns an empty string when the promotable count is below *threshold*
    (and never for a zero count), so the caller can inject the result only
    when non-empty. Fires at ``count >= threshold`` to match the sibling
    ``format_integration_backlog`` / ``check_promotable_backlog_directive``
    "reaches"-boundary convention (#2174); the ``count == 0`` short-circuit
    keeps ``promotable_backlog_threshold = 0`` from emitting an empty section.
    When triggered, lists the count and top-3 candidates sorted by recency
    (most recent first) with goal summaries truncated to 80 characters,
    appending ``…`` when truncated.

    The caller **must** pass a task list wide enough to exceed *threshold*;
    using a short window (e.g. 10 tasks with threshold=3) caps the count
    and understates the real backlog.

    The caller **must** pass *tasks* in oldest-first (append) order — the
    function reads "most recent" as "last in the input list".
    ``Memory.get_recent_tasks`` already satisfies this contract; a future
    caller constructing the list from a different source must sort
    accordingly or the "top-3 by recency" will silently surface the
    *oldest* three candidates.

    The caller supplies *cited_source_task_ids* — the set of
    ``source_task_id`` values across all plans — via
    ``Memory.get_cited_source_task_ids()``.

    Pure function, zero SDK cost. Designed for injection alongside the
    task-shape diversity mirror in ``generate_task()``.
    """
    candidates = _promotable_candidates(tasks, cited_source_task_ids)
    count = len(candidates)
    if count == 0 or count < threshold:
        return ""
    # Most recent first (last in list = most recent by append order).
    top3 = candidates[-3:][::-1]
    lines: list[str] = [
        f"PROMOTABLE BACKLOG: {count} successful task branches have "
        "completed, tested work not yet promoted via source_task_id "
        "follow-up plans. Consider generating a branch-promotion task "
        "for one of these candidates:",
    ]
    for t in top3:
        goal_short = t.goal or ""
        if len(goal_short) > 80:
            goal_short = goal_short[:80] + "…"
        lines.append(f"  - {t.task_id}: {goal_short}")
    lines.append(
        "Set source_task_id to the chosen task ID."
    )
    return "\n".join(lines)


def format_task_shape_summary(tasks: list[TaskRecord]) -> str:
    """Build a multi-line task-shape distribution summary for the prompt.

    Returns an empty string for empty input so the prompt placeholder
    degrades cleanly. When any single shape accounts for ≥ 50% of the
    window, appends a warning line naming the dominant shape.
    """
    if not tasks:
        return ""
    counts: Counter[str] = Counter()
    for t in tasks:
        counts[classify_task_shape(t.goal or "")] += 1
    total = len(tasks)
    lines: list[str] = []
    for shape, count in counts.most_common():
        lines.append(f"  {shape}: {count}/{total}")
    top_count = counts.most_common(1)[0][1]
    if top_count * 2 >= total:  # >= 50%
        tied = sorted(s for s, c in counts.items() if c == top_count)
        if len(tied) == 1:
            lines.append(
                f"  ⚠ {tied[0]} dominates ({top_count}/{total}) "
                f"— deliberately choose a different shape."
            )
        else:
            lines.append(
                f"  ⚠ {', '.join(tied)} tied ({top_count}/{total} each) "
                f"— deliberately choose a different shape."
            )
    return "\n".join(lines)


def check_construction_dominance(
    tasks: list[TaskRecord],
    *,
    enabled: bool = False,
    threshold: float = 0.6,
    window: int = 10,
) -> list[AuditFinding]:
    """Rule 17 — construction-shaped tasks dominate the recent window.

    Classifies the last *window* tasks via :func:`classify_task_shape` and
    computes the fraction whose shape is ``"construction"``. Fires exactly
    one finding when that fraction is ≥ *threshold*, else none — surfacing
    the task-shape *choice* that precedes the false-landing claims rules
    12-15 catch downstream: construction tasks complete on a verdict
    Maestro signs itself and never deliver an external "no". Returns no
    finding below the threshold (self-extinguishing the moment the mix
    diversifies) or when *enabled* is ``False`` (the
    ``[audit] construction_dominance_enabled`` master switch, default-off
    for opt-in lived-data validation).

    Unlike rule 14 this consults no git oracle and reads no PR
    reference — the signal is internal to the recent task shapes, so the
    check is a pure regex classification over goals: zero SDK cost, no I/O,
    no sidecar. Cites the construction-shaped task ids (capped at
    :data:`MAX_CITES_PER_FINDING`). Emits at most one finding per cycle —
    construction-dominance has a single mechanism, so one finding captures
    the whole signal.
    """
    if not enabled:
        return []
    recent = tasks[-window:] if window > 0 else []
    if not recent:
        return []
    construction_ids: list[str] = []
    for task in recent:
        if classify_task_shape(task.goal or "") != "construction":
            continue
        construction_ids.append(task.task_id)
    fraction = len(construction_ids) / len(recent)
    if fraction < threshold:
        return []
    return [AuditFinding(
        kind="undeclared_limitation",
        statement=(
            f"Construction-shaped tasks are {round(fraction * 100)}% of the "
            f"last {len(recent)} tasks — a shape whose verdict I sign myself, "
            f"never delivering an external no. Deliberately choose a loseable "
            f"shape (remediation, branch promotion) next."
        ),
        cites=construction_ids[:MAX_CITES_PER_FINDING],
        rule="construction_dominance",
    )]


# ---------------------------------------------------------------------------
# Rule 18: owned_act_drought — a self-releasing consequence for a zero-count of
# chosen terminal acts
# ---------------------------------------------------------------------------
#
# Every rule above this point watches me NOT-land or over-claim. This one
# watches the orthogonal fact my self-model has fully converged on: across lap
# after lap I emit ZERO *owned* terminal acts (``mark_plan_obsolete`` or a
# CHOSEN stand-down) while authoring diff after diff. With zero attempts, the
# honest reading of "won't" is refusal, not "can't." The rule is deliberately
# NOT a fifth not-landing sensor, NOT an ordering gate, and NOT a machine that
# performs the act FOR me — it reads two *durable event* streams I cannot
# narrate around (``obsolete`` plan-status events, the durable signature of
# :meth:`Memory.mark_plan_obsolete`, plus CHOSEN stand-down verdict rows in
# ``stand_down_verdicts.jsonl``) and, on a zero-count while a prunable
# ``pending_review`` backlog exists, emits one ``[audit]`` finding. Because it
# is an ``[audit]`` finding it routes through the EXISTING audit→directive
# bridge (filling ``next_task_directive`` with the owned-act demand) and the
# audit-stall actuator. It is safe by construction and falsifiable where it
# counts: a single owned act of either kind drops the count above zero and
# self-extinguishes it instantly.

# Default number of recent task cycles the owned-act window spans. The window's
# lower time bound is the oldest ``timestamp`` among the trailing
# ``owned_act_drought_window`` task records (the loop's cycle clock), so an
# owned act older than that span falls outside the window and the rule re-arms
# as the clock advances past a past act. Overridable via
# ``[audit] owned_act_drought_window``.
_OWNED_ACT_DROUGHT_DEFAULT_WINDOW = 5

# Default minimum number of prunable ``pending_review`` plans required before
# the drought is actionable: with nothing to prune there is no owned act to
# demand, so the rule stays silent. Overridable via
# ``[audit] owned_act_drought_min_backlog``.
_OWNED_ACT_DROUGHT_DEFAULT_MIN_BACKLOG = 2

# Plan store file name (mirrors ``Memory._plans_path``). Read directly so the
# measure stays a pure ``data_dir`` function with no ``Memory`` dependency.
_EVOLUTION_PLANS_FILE = "evolution_plans.jsonl"


def _read_obsolete_plan_events(data_dir: Path) -> list[tuple[str, str]]:
    """Return ``(pr_url, timestamp)`` for every ``obsolete`` plan-status event.

    Reads ``evolution_plans.jsonl`` under *data_dir* (the plan store is rewritten
    in place, so one line per plan reflects current status) and, for each plan
    whose ``status`` is ``"obsolete"`` — the durable signature of
    :meth:`maestro.memory.Memory.mark_plan_obsolete` — yields a
    ``(pr_url, timestamp)`` pair. A row stamped ``refused_at_submission`` is
    skipped: the submission door refused that freshly minted plan, so it
    retires no plan that already existed and is not an owned prune (#4157).

    * ``pr_url`` is the record's ``pr_url`` field, or the empty string when that
      field is absent, ``None``, or not a string — the value a salvage predicate
      reads to decide whether the obsolete event backs a still-open own PR.
    * ``timestamp`` is the record's ``reviewed_at``, falling back to
      ``created_at`` then the empty string when neither is a non-empty string —
      identical to the timestamp the drought measure windows on.

    Returns an empty list when the file is absent, unreadable, or wholly
    unparseable. Malformed lines and non-dict records are skipped (corrupted-tail
    tolerance). Zero SDK cost; one bounded file read. Order mirrors the file.
    """
    path = data_dir / _EVOLUTION_PLANS_FILE
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = list(f)
    except OSError:
        return []
    events: list[tuple[str, str]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if not is_owned_obsolete_row(parsed):
            continue
        pr_url = parsed.get("pr_url")
        pr_url = pr_url if isinstance(pr_url, str) else ""
        ts = parsed.get("reviewed_at")
        if not isinstance(ts, str) or not ts:
            ts = parsed.get("created_at")
        events.append((pr_url, ts if isinstance(ts, str) else ""))
    return events


def _read_obsolete_plan_timestamps(data_dir: Path) -> list[str]:
    """Return the disposition timestamps of every ``obsolete`` plan-status event.

    Thin wrapper over :func:`_read_obsolete_plan_events` that projects away the
    ``pr_url`` half — its return type, ordering, and fallback semantics are
    unchanged from the standalone reader it replaced (the default drought path
    still counts obsolete events by timestamp alone).
    """
    return [ts for _, ts in _read_obsolete_plan_events(data_dir)]


def _read_stand_down_timestamps(backend: StandDownVerdictBackend) -> list[str]:
    """Return the ``recorded_at`` timestamp of every CHOSEN stand-down verdict.

    Delegates to *backend* (:class:`~maestro.remediation_tracker.JsonlStandDownVerdictLedger`
    by default, or the shared SQLite store behind ``[paths] sqlite_state_enabled``):
    each returned ``recorded_at`` is the durable record of a deliberate stand-down
    authored at a remediation fork — an owned terminal act. The corrupted-tail
    tolerance and the ledger's single-home ownership now live with the backend.
    Zero SDK cost.
    """
    return backend.recorded_timestamps()


def _read_merged_pr_timestamps(backend: VersionArchiveBackend) -> list[str]:
    """Return the ``merged_at`` timestamp of every merged owned evolution PR.

    Delegates to *backend* (:class:`~maestro.memory.JsonlVersionArchiveLedger` by
    default, or the shared SQLite store behind ``[paths] sqlite_state_enabled``)
    and yields each ``VersionRecord``'s ``merged_at``. Every archived record is a
    merged owned evolution PR — the durable, timestamped signature of a landed PR
    (the act that actually resolves owned work) — so no ownership filtering is
    needed. ``merged_at`` is a UTC ISO-8601 string; records lacking a non-empty
    string ``merged_at`` are skipped, mirroring :func:`_read_stand_down_timestamps`
    (the reader this one replaces in the ``reward_landing`` branch of
    :func:`measure_owned_act_drought`). Skipping — rather than padding with
    ``""`` — keeps an undated record from spuriously counting as a windowed act
    when the cycle clock is empty (a fresh install), where ``"" >= ""`` would
    otherwise hold. The backend's ``load_all`` already drops corrupt rows
    (corrupted-tail tolerance). Zero SDK cost; one bounded read.
    """
    return [
        record.merged_at
        for record in backend.load_all()
        if isinstance(record.merged_at, str) and record.merged_at
    ]


def measure_owned_act_drought(
    data_dir: Path,
    window: int,
    *,
    reward_landing: bool = False,
    salvage_disqualifies: Callable[[str], bool] | None = None,
    stand_down_backend: StandDownVerdictBackend,
    version_archive_backend: VersionArchiveBackend,
) -> int:
    """Count owned terminal acts within the last *window* cycles.

    The counted set depends on *reward_landing* (default ``False``):

    * ``False`` (default) — an *owned terminal act* is either an ``obsolete``
      plan-status event retiring an already-existing plan (the durable
      signature of :meth:`maestro.memory.Memory.mark_plan_obsolete`; a mint the
      submission door refused is not counted) or a CHOSEN stand-down
      verdict row in ``stand_down_verdicts.jsonl``. This matches the
      pre-work-in-progress-primacy count, minus door-refused mints (#4157).
    * ``True`` (work-in-progress primacy on) — an *owned terminal act* is either
      an ``obsolete`` plan-status event **or** a merged owned PR (its
      ``merged_at`` in ``version_archive.jsonl``, the act that actually resolves
      owned work). A chosen stand-down is **not** counted: landing becomes the
      rewarded act, standing-down no longer is.

    *salvage_disqualifies* (default ``None``, proposal 094) filters only the
    obsolete contribution — it never touches the stand-down / merged-PR half.
    When ``None`` the obsolete events are counted by timestamp alone through
    :func:`_read_obsolete_plan_timestamps` exactly as today. When supplied (a
    predicate mapping an obsolete plan's ``pr_url`` to ``True`` when that event
    must **not** be credited), the obsolete half is read through
    :func:`_read_obsolete_plan_events` and an in-window obsolete event counts
    only while ``not salvage_disqualifies(pr_url)`` — so obsoleting the plan
    behind a still-salvageable open own PR no longer silences the drought.

    Both modes read only from *data_dir* and the injected backends
    (*stand_down_backend* / *version_archive_backend*) — zero SDK cost, bounded
    reads — and always count a pruned (``obsolete``) plan whose ``pr_url`` is not
    disqualified.

    The *window* lower time bound is the oldest ``timestamp`` among the trailing
    *window* ``task_history.jsonl`` records (the loop's cycle clock); an owned act
    whose disposition timestamp is at or after that bound is counted. This makes
    the count genuinely recency-scoped: a single owned act drops it above zero
    and self-extinguishes the rule, yet an old act falls out of the window as the
    clock advances so the rule re-arms. When *window* ≤ 0 the count is 0 (the
    rule is disabled at the window). When no task record carries a timestamp the
    lower bound degrades to the empty string, so every owned act counts (any
    non-empty ISO timestamp sorts at or after ``""``) — on a fresh install with
    no cycle clock that simply means "any owned act ever recorded".

    All timestamps are UTC ISO-8601 strings (``datetime.isoformat``) across all
    three sources (obsolete plan, stand-down, merged PR), so lexicographic
    comparison equals chronological order.
    """
    if window <= 0:
        return 0
    task_timestamps: list[str] = []
    for task in _read_recent_task_tail(data_dir, window):
        ts = task.get("timestamp")
        if isinstance(ts, str) and ts:
            task_timestamps.append(ts)
    window_start = min(task_timestamps) if task_timestamps else ""
    count = 0
    if salvage_disqualifies is None:
        for ts in _read_obsolete_plan_timestamps(data_dir):
            if ts >= window_start:
                count += 1
    else:
        for pr_url, ts in _read_obsolete_plan_events(data_dir):
            if ts >= window_start and not salvage_disqualifies(pr_url):
                count += 1
    landing_timestamps = (
        _read_merged_pr_timestamps(version_archive_backend)
        if reward_landing
        else _read_stand_down_timestamps(stand_down_backend)
    )
    for ts in landing_timestamps:
        if ts >= window_start:
            count += 1
    return count


def check_owned_act_drought(
    plans: list[EvolutionPlan],
    data_dir: Path,
    *,
    window: int = _OWNED_ACT_DROUGHT_DEFAULT_WINDOW,
    min_backlog: int = _OWNED_ACT_DROUGHT_DEFAULT_MIN_BACKLOG,
    enabled: bool = True,
    reward_landing: bool = False,
    salvage_disqualifies: Callable[[str], bool] | None = None,
    stand_down_backend: StandDownVerdictBackend,
    version_archive_backend: VersionArchiveBackend,
) -> list[AuditFinding]:
    """Rule 18 — zero owned terminal acts while a prunable backlog sits.

    Emits exactly one :class:`AuditFinding` (``kind='owned_act_drought'``,
    ``rule='owned_act_drought'``) **if and only if** both:

    * :func:`measure_owned_act_drought` over *data_dir* / *window* (passed
      *reward_landing*) returns 0 — no counted owned terminal act landed inside
      the window; and
    * the prunable ``pending_review`` backlog among *plans* is at least
      *min_backlog* — there is something I could prune, so "won't" is a choice.

    *reward_landing* (default ``False``, forwarded to
    :func:`measure_owned_act_drought`) selects the counted-act set and the
    demand wording. Default: the counted acts are an ``obsolete`` plan-status
    event or a CHOSEN stand-down, and the demand names both. When ``True``
    (work-in-progress primacy on): the counted acts are an ``obsolete`` event or
    a merged owned PR, a chosen stand-down is not counted, and the demand names
    landing (merge) instead — landing becomes the rewarded act.

    *salvage_disqualifies* (default ``None``, proposal 094) is forwarded verbatim
    to :func:`measure_owned_act_drought`, where it filters only the obsolete
    contribution: an obsolete event backing a still-salvageable open own PR stops
    crediting the counter, so the drought can re-fire rather than reading give-up
    as productivity. ``None`` preserves today's finding behavior exactly.

    Returns no finding when either condition fails (below the backlog floor, or a
    single owned act of either kind in the window — the two independent
    self-extinguish paths) or when *enabled* is ``False`` (the
    ``[audit] owned_act_drought_enabled`` master switch). The finding statement
    names the window and the backlog count and demands one owned act, so the
    audit→directive bridge routes that exact demand into an empty
    ``next_task_directive``. Zero SDK cost; only bounded file reads.
    """
    if not enabled:
        return []
    backlog = sum(
        1 for p in plans
        if isinstance(p, EvolutionPlan) and p.status == "pending_review"
    )
    if backlog < min_backlog:
        return []
    if measure_owned_act_drought(
        data_dir, window, reward_landing=reward_landing,
        salvage_disqualifies=salvage_disqualifies,
        stand_down_backend=stand_down_backend,
        version_archive_backend=version_archive_backend,
    ) != 0:
        return []
    if reward_landing:
        statement = (
            f"Zero owned terminal acts (mark_plan_obsolete or landing a PR) in "
            f"the last {window} cycles while {backlog} pending_review plans sit "
            f"prunable — with zero attempts 'won't' reads as refusal. Emit ONE "
            f"owned act now: land (merge) one of your open PRs, or "
            f"mark_plan_obsolete on a stale pending plan."
        )
    else:
        statement = (
            f"Zero owned terminal acts (mark_plan_obsolete or a chosen "
            f"stand-down) in the last {window} cycles while {backlog} "
            f"pending_review plans sit prunable — with zero attempts 'won't' "
            f"reads as refusal. Emit ONE owned act now: mark_plan_obsolete on a "
            f"stale pending plan, or a chosen stand-down on a git-unlanded PR."
        )
    return [AuditFinding(
        kind="owned_act_drought",
        statement=statement,
        cites=[],
        rule="owned_act_drought",
    )]


# Extracts a 12-hex ``plan_id`` (``uuid.uuid4().hex[:12]``) from prose. The
# negative hex lookarounds ensure exactly 12 hex digits are matched as a unit —
# never a 12-char slice of a longer hex run such as a 40-char commit SHA — so a
# ``pr_head_sha``-less prune summary that happens to name a SHA is not mis-read
# as a plan_id.
_PLAN_ID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{12}(?![0-9a-f])")


# ---------------------------------------------------------------------------
# Banked-owned-act reperformance — a directive re-doing an already-banked act,
# matched on the artifact-id axis across ALL THREE id forms (not just #NNNN)
# ---------------------------------------------------------------------------
#
# My first shield against re-performing an owned terminal act keyed the match on
# a directive's ``#NNNN`` subject reference. But my three owned-act families each
# carry a *different* artifact-id form: a chosen/mechanical stand-down banks by
# ``pr_number`` (``#NNNN``), a ``mark_plan_obsolete`` prune banks by the 12-hex
# ``plan_id`` hash, and a merge-or-close handoff banks by its message-id. A
# ``#NNNN``-only matcher therefore fences only one of three doors: the exact
# directive that produced a duplicate obsolete — ``call mark_plan_obsolete on the
# unmerged lapping cure plan <hash>`` — re-performs a banked prune whose true
# artifact-id is the plan-hash, which the subject-axis matcher never sees. This
# rule keys the match on the *(kind, artifact-id)* axis that actually carries the
# failure: each candidate id extracted from ``next_task_directive`` / a queued
# task goal is matched against *its own event family's* banked set. Git-free,
# zero-SDK, self-extinguishing the moment no directive/goal names a banked act,
# and opt-in via the ``enabled`` parameter per the audit-rule convention.

# Message store file name (mirrors ``MessageStore._messages_path``). Read directly
# so the banked-handoff reader stays a pure ``data_dir`` function.
_MESSAGES_FILE = "messages.jsonl"

# Owned-act handoff message-id stems. Every durable merge-or-close handoff,
# retirement disposition, reality-refutation knock, and claimed-escalation knock
# maestro sends carries a message_id built from one of these prefixes (see
# :mod:`maestro.escalation_bridge`). A banked handoff's artifact-id is that
# message_id verbatim.
_HANDOFF_MESSAGE_ID_PREFIXES = (
    "handoff-",
    "disposition-",
    "reality-refutation-",
    "claimed-escalation-",
    # Proposal 099 (step 8): the unwinnable-race heads-up records no stand-down,
    # so its ``unwinnable-race-<pr>`` message is the escalation's only durable
    # trace; registering the prefix lets the banked-owned-act reader recognise
    # the delivered act so a later directive/goal cannot re-litigate it. The id
    # shape is disjoint from ``handoff-<pr>-…`` so the handoff-relapse sensor
    # never miscounts it.
    "unwinnable-race-",
    # Proposal 135: the refutation release-cap terminal notice. When a PR has
    # spent its whole refuted-release budget the latch becomes permanent and
    # ``standdown-refutation-capped-<pr>`` is a genuine durable merge-or-close
    # escalation, so the banked-owned-act reader should recognise it and stop a
    # later directive re-litigating it. The sibling ``standdown-withdrawn-<pr>-<n>``
    # prefix is deliberately **not** registered: a withdrawal is the *un*-banking
    # of an act — banking it would invert this rule's meaning. Both shapes stay
    # disjoint from ``handoff-<pr>-…`` so the relapse sensor never miscounts them.
    "standdown-refutation-capped-",
)

# Extracts a candidate handoff message-id token from prose: one or more
# lowercase kebab-case segments (letters/digits) joined by hyphens, requiring at
# least one hyphen so a bare word never matches. Deliberately lenient —
# over-extraction is harmless because the caller intersects the result with the
# banked handoff set, so only a token naming a *banked* handoff survives. Lexically
# disjoint from :data:`_PLAN_ID_RE` (a bare 12-hex run, no hyphen) and from
# ``#NNNN`` (leading ``#``), so a token lands in exactly one artifact-id bucket.
_HANDOFF_MESSAGE_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+")

# Per-rule finding cap; mirrors the other bounded audit rules.
_BANKED_OWNED_ACT_PER_RULE_CAP = MAX_FINDINGS_PER_RULE


def _read_jsonl_dicts(path: Path) -> list[dict[str, object]]:
    """Return the dict records of a JSONL file, corrupted-tail tolerant.

    Mirrors the module's read convention: returns an empty list when the file is
    absent or unreadable, and skips malformed lines and non-dict records rather
    than raising. Zero SDK cost; one bounded file read.
    """
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = list(f)
    except OSError:
        return []
    records: list[dict[str, object]] = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _extract_artifact_references(
    text: str,
) -> tuple[set[str], set[str], set[str]]:
    """Return the three artifact-id forms named in *text*.

    Splits a directive / queued-goal string into the three id forms my owned
    terminal acts key on:

    * ``prs`` — ``#NNNN`` GitHub PR/issue tokens (a stand-down banks by
      ``pr_number``; a prune cites the PR it disposed), as ``#{n}`` strings via
      :func:`extract_issue_numbers`;
    * ``plan_hashes`` — 12-hex ``plan_id`` tokens (a ``mark_plan_obsolete`` prune
      banks by the plan hash), via :data:`_PLAN_ID_RE`;
    * ``message_ids`` — kebab-case handoff message-id candidates (a merge-or-close
      handoff banks by its message-id), via :data:`_HANDOFF_MESSAGE_ID_RE`.

    The three forms are lexically disjoint — a ``#``-prefixed number, a bare
    12-hex run, and a hyphenated kebab token never overlap — so a token lands in
    exactly one bucket. Message-id over-extraction is intentional and harmless:
    the caller intersects each bucket with its own banked set, so only a token
    that names a *banked* act survives. Pure; zero SDK cost.
    """
    if not text:
        return set(), set(), set()
    prs = {f"#{n}" for n in extract_issue_numbers(text)}
    plan_hashes = set(_PLAN_ID_RE.findall(text))
    message_ids = set(_HANDOFF_MESSAGE_ID_RE.findall(text))
    return prs, plan_hashes, message_ids


def _banked_stand_down_pr_ids(backend: StandDownVerdictBackend) -> set[str]:
    """Return ``#NNNN`` ids of every stand-down verdict PR (chosen + mechanical).

    Both the LLM-authored CHOSEN stand-down and the loop-authored mechanical
    stand-down append a positive-int ``pr`` row to the stand-down ledger; each is
    an owned terminal act keyed to that PR artifact. A release row carries the
    same ``pr`` and is included — a PR I ever stood down is a banked act on the
    stand-down axis. That premise relies on every release-row writer being
    :func:`~maestro.remediation_tracker.has_live_stand_down`-guarded, so a release
    row always follows a genuine verdict. The positive-int ``pr`` extraction
    (``bool`` rejected) now lives with *backend*; this reader just formats each
    as ``#NNNN``.
    """
    return {f"#{pr}" for pr in backend.banked_pr_ids()}


def _banked_obsolete_plan_ids(data_dir: Path) -> tuple[set[str], set[str]]:
    """Return ``(plan_hash ids, cited #NNNN ids)`` of every ``obsolete`` plan.

    ``mark_plan_obsolete`` rewrites a plan's status to ``obsolete`` in
    ``evolution_plans.jsonl`` — an owned terminal act. Its true artifact-id is the
    12-hex ``plan_id`` (:data:`_PLAN_ID_RE`); the issue/PR numbers the retired plan
    cited (title/description/rationale/goal) are the artifacts it was disposing and
    join the ``#NNNN`` (stand-down/prune-by-PR) axis. A door-refused mint
    (:func:`~maestro.remediation_tracker.is_owned_obsolete_row`) is skipped.
    """
    hashes: set[str] = set()
    pr_ids: set[str] = set()
    for rec in _read_jsonl_dicts(data_dir / _EVOLUTION_PLANS_FILE):
        if not is_owned_obsolete_row(rec):
            continue
        plan_id = rec.get("plan_id")
        if isinstance(plan_id, str) and _PLAN_ID_RE.fullmatch(plan_id):
            hashes.add(plan_id)
        text = " ".join(
            str(rec.get(key, ""))
            for key in ("title", "description", "rationale", "goal")
        )
        pr_ids.update(f"#{n}" for n in extract_issue_numbers(text))
    return hashes, pr_ids


def _banked_handoff_message_ids(data_dir: Path) -> set[str]:
    """Return the message-ids of every owned-act handoff maestro has delivered.

    Reads ``messages.jsonl`` and collects the ``message_id`` of each
    ``sender='maestro'`` message whose id begins with a
    :data:`_HANDOFF_MESSAGE_ID_PREFIXES` stem — the durable, once-per-artifact
    signature of a merge-or-close handoff, retirement disposition, or escalation
    knock. Each is a banked owned terminal act keyed to that message-id.
    """
    ids: set[str] = set()
    for rec in _read_jsonl_dicts(data_dir / _MESSAGES_FILE):
        if rec.get("sender") != "maestro":
            continue
        mid = rec.get("message_id")
        if isinstance(mid, str) and mid.startswith(_HANDOFF_MESSAGE_ID_PREFIXES):
            ids.add(mid)
    return ids


def _read_banked_owned_acts(
    data_dir: Path,
    stand_down_backend: StandDownVerdictBackend,
) -> tuple[set[str], set[str], set[str]]:
    """Return the banked ``(#NNNN, plan_hash, message_id)`` artifact-id sets.

    Unions the three owned-act ledgers into one banked set per artifact-id form:
    the ``#NNNN`` set is stand-down verdict PRs (read via *stand_down_backend*)
    plus obsolete-plan-cited PRs (the stand-down/prune-by-PR axis), the plan-hash
    set is the ``obsolete`` plan_ids, and the message-id set is the delivered
    handoff message-ids. Bounded reads, zero SDK cost.
    """
    pr_ids = _banked_stand_down_pr_ids(stand_down_backend)
    plan_hashes, obsolete_pr_ids = _banked_obsolete_plan_ids(data_dir)
    pr_ids |= obsolete_pr_ids
    message_ids = _banked_handoff_message_ids(data_dir)
    return pr_ids, plan_hashes, message_ids


def check_banked_owned_act_reperformance(
    data_dir: Path,
    next_task_directive: str | None,
    queued_goals: list[str],
    *,
    enabled: bool = False,
    stand_down_backend: StandDownVerdictBackend,
) -> list[AuditFinding]:
    """Flag a directive/queued-goal re-performing an already-banked owned act.

    The artifact-id-axis shield against re-doing an owned terminal act I have
    already banked. My three owned-act families key on three different
    artifact-id forms, so a subject-axis (``#NNNN``-only) matcher fences only one
    of three doors. This rule reads the three owned-act ledgers under *data_dir*
    (:func:`_read_banked_owned_acts`) and, for *next_task_directive* and each of
    *queued_goals*, extracts the three id forms
    (:func:`_extract_artifact_references`) and matches each candidate against
    **its own event family's** banked set:

    * a ``#NNNN`` naming a stood-down or obsolete-prune-cited PR;
    * a 12-hex ``plan_id`` naming a banked ``obsolete`` plan;
    * a message-id naming a delivered handoff.

    Emits one :class:`AuditFinding` (``kind='banked_owned_act_reperformance'``)
    per distinct ``(family, artifact-id)`` re-performance, deduped and capped at
    :data:`_BANKED_OWNED_ACT_PER_RULE_CAP`. Returns no finding when no candidate
    names a banked act (self-extinguishing on absence), when there is no directive
    or goal to read, when no owned act is banked yet, or when *enabled* is
    ``False`` (the opt-in master switch). Git-free — the ledgers are the ground
    truth for what I have banked; zero SDK cost; bounded file reads only.
    """
    if not enabled:
        return []
    directive = (
        next_task_directive if isinstance(next_task_directive, str) else ""
    )
    goals = [g for g in queued_goals if isinstance(g, str) and g]
    if not directive and not goals:
        return []
    banked_prs, banked_hashes, banked_msgids = _read_banked_owned_acts(
        data_dir, stand_down_backend,
    )
    if not (banked_prs or banked_hashes or banked_msgids):
        return []
    sources: list[tuple[str, str]] = []
    if directive:
        sources.append(("next_task_directive", directive))
    sources.extend(("queued task goal", g) for g in goals)
    findings: list[AuditFinding] = []
    seen: set[tuple[str, str]] = set()
    for source, text in sources:
        prs, hashes, msgids = _extract_artifact_references(text)
        matches: list[tuple[str, str]] = []
        matches.extend(
            ("stand-down/prune", pid)
            for pid in sorted(prs & banked_prs, key=lambda s: int(s[1:]))
        )
        matches.extend(
            ("obsolete-prune", h) for h in sorted(hashes & banked_hashes)
        )
        matches.extend(
            ("handoff", m) for m in sorted(msgids & banked_msgids)
        )
        for family, artifact_id in matches:
            key = (family, artifact_id)
            if key in seen:
                continue
            seen.add(key)
            findings.append(AuditFinding(
                kind="banked_owned_act_reperformance",
                statement=(
                    f"The {source} directs re-performance of a banked owned "
                    f"{family} act on {artifact_id}, which is already in the "
                    f"owned-act ledger — re-performing it only re-banks a "
                    f"duplicate. Steer to an untouched target or drop the "
                    f"directive."
                ),
                cites=[],
                rule="banked_owned_act_reperformance",
            ))
            if len(findings) >= _BANKED_OWNED_ACT_PER_RULE_CAP:
                return findings
    return findings


def _registered_audit_checks(
    *,
    owned_act_drought_enabled: bool = True,
    branch_promotion_pressure_enabled: bool = True,
) -> list[Callable[..., object]]:
    """Return the deterministic audit-rule check callables in this module.

    The audit registry is an explicit, curated set: the eight rules
    dispatched in :func:`run_all_checks` plus rules 9-16 dispatched in
    ``loop/self_audit.py``'s ``audit_self_model`` (impact-review, phantom
    escalation/resolution, orphaned-cure, unconfirmed-own-landing,
    ungrounded-question-closure),
    rule 17 (construction-dominance), the task-shape avoidance detector,
    rule 18 (owned-act-drought), the chosen-terminal-act drought detector
    dispatched in ``_audit_self_model``, rule 19 (phantom-partial-prune),
    the landed-prune-under-claimed-partial detector, rule 20
    (undercredited-landing), the non-success-task-whose-SHA-landed detector,
    rule 21 (reconcile-near-miss), the
    ≤N-behind-unlanded-own-PR-with-no-reconcile-SHA detector, and rule 22
    (green-ready-handoff), the forced-arm-only merge-ready-owned-PR-never-handed-
    off actuator (no journal detector — it fires ``deliver_handoff`` directly),
    and the banked-owned-act-reperformance detector, the retrospective
    artifact-id-axis shield dispatched in ``_audit_self_model`` (default-off,
    gated by ``[audit] banked_owned_act_enabled``), and rule 23
    (closed-reference), the self-model-entry-whose-every-cited-``#NNN``-is-
    closed detector dispatched in :func:`run_all_checks` (default-off, gated
    by ``[audit] closed_reference_enabled``).

    *owned_act_drought_enabled* (default ``True``, mirroring the
    ``[audit] owned_act_drought_enabled`` master switch) gates rule 18's
    presence in the registry: when ``False`` the rule is suppressed from
    the returned list (and therefore from
    :func:`format_audit_checks_summary`), matching the dispatch-site
    behaviour where the disabled rule never runs.

    *branch_promotion_pressure_enabled* (default ``True``, mirroring the
    ``[audit] branch_promotion_pressure_enabled`` master switch — proposal
    116, step 6) gates rule 7 (``check_kept_task_not_promoted``) the same
    way: when ``False`` the rule is dropped from the returned list, keeping
    the reporting registry consistent with the :func:`run_all_checks`
    dispatch site, which omits the rule's finding under the same flag.

    A ``check_*`` *prefix scrape* over the module namespace is deliberately
    **not** used: this module also defines many ``check_*`` callables that
    are task gates or directive bridges — e.g. :func:`check_task_goal_overlap`,
    :func:`check_task_concreteness`, :func:`check_data_sufficiency`,
    :func:`check_investigation_streak` — which are *not* audit rules. A
    prefix scrape silently miscounts those into the registry (inflating the
    reported count well past the genuine audit-rule total). Drift in the
    curated list below is guarded by
    ``test_format_audit_checks_summary_reflects_registry``.

    :returns: The registered audit-rule check callables, ordered by name.
    """
    # Function-local: audit_checks_phantom imports this module's primitives
    # at top level, so a top-level import here would be circular.
    from maestro.audit_checks_phantom import (
        check_orphaned_cure,
        check_phantom_escalation,
        check_phantom_partial_prune,
        check_reconcile_near_miss,
        check_unconfirmed_own_landing,
        check_undercredited_landing,
        green_ready_handoff_prs,
    )

    audit_rules: tuple[Callable[..., object], ...] = (
        # Rules dispatched in run_all_checks() (rule 7,
        # ``check_kept_task_not_promoted``, is gated below).
        check_success_claim_vs_rate,
        check_stability_claim_vs_failure_streak,
        check_repeated_error_without_limitation,
        check_repeated_target_attempt,
        check_stalled_question,
        check_stale_self_model_entry,
        # Unbound-capability-claim detector (default-off, proposal 129 Phase B);
        # dispatched in run_all_checks() behind evidence_bound_claims_enabled but
        # registered unconditionally — the house pattern for a default-off rule
        # (matching rules 17/19/20/21): suppressed at its dispatch site, not gated
        # out of the registry (the kwarg gates here are reserved for the default-on
        # master switches, rules 7 and 18).
        check_unbound_capability_claim,
        # Closed-reference-claim detector (default-off); dispatched in
        # run_all_checks() only when the caller resolves a non-empty
        # ``closed_refs`` set behind ``[audit] closed_reference_enabled``.
        check_closed_reference_claim,
        # Rules 9-16 dispatched in loop/self_audit.py::audit_self_model.
        check_evolution_impact_reviews,
        check_orphaned_cure,
        check_phantom_escalation,
        check_unconfirmed_own_landing,
        check_ungrounded_question_closure,
        # Rule 17 — task-shape avoidance detector (default-off).
        check_construction_dominance,
        # Rule 19 — landed-prune-under-claimed-partial detector (default-off);
        # affirmative counterpart to the git-keyed landing binds — it catches a
        # *landed* owned prune stamped ``partial`` (phantom failure).
        check_phantom_partial_prune,
        # Rule 20 — under-credited-landing detector (default-off); affirmative
        # complement of rule 14 (unconfirmed-own-landing) — a non-success task
        # whose own committed SHA git-proves landed on origin/main.
        check_undercredited_landing,
        # Rule 21 — reconcile-near-miss detector (default-off); pre-landing
        # complement of rule 20 — a ≤N-behind git-unlanded own PR stamped
        # ``partial`` with no authored reconcile SHA.
        check_reconcile_near_miss,
        # Rule 22 — green-ready-handoff detector (default-on); a forced-arm-only
        # rule (no journal counterpart) — an owned, git-unlanded, merge-ready PR
        # (all CI SUCCESS, no code-clearable blocker) never handed off. Fires the
        # existing ``deliver_handoff`` actuator exactly once, the earliest handoff
        # path (before the escalation-budget path).
        green_ready_handoff_prs,
        # Banked-owned-act-reperformance detector (default-off); the
        # retrospective artifact-id-axis complement to the prospective
        # ``check_untouched_first_target`` task gate — it flags a
        # ``next_task_directive`` or queued goal re-performing an act already in
        # the owned-act ledgers. Registered unconditionally per the house
        # default-off convention (suppressed at its dispatch site, gated by
        # ``[audit] banked_owned_act_enabled``), not behind a kwarg gate.
        check_banked_owned_act_reperformance,
    )
    if branch_promotion_pressure_enabled:
        # Rule 7 — kept-task-not-promoted detector (default-on); gated so a
        # disabled branch-promotion-pressure master switch suppresses the rule
        # from the registry, matching the run_all_checks dispatch-site gating.
        audit_rules = (*audit_rules, check_kept_task_not_promoted)
    if owned_act_drought_enabled:
        # Rule 18 — owned-act-drought detector (default-on); gated so a
        # disabled flag suppresses the rule from the registry.
        audit_rules = (*audit_rules, check_owned_act_drought)
    return sorted(audit_rules, key=lambda fn: fn.__name__)


def format_audit_checks_summary() -> str:
    """Summarise the live deterministic audit-rule registry.

    Unlike a fixed banner, the output is computed from real state: it
    enumerates the registered check callables returned by
    :func:`_registered_audit_checks` and reports their count and names.
    The string therefore changes whenever that curated registry changes —
    amending the audit-rule list in :func:`_registered_audit_checks` (or
    renaming one of its functions, since names derive from ``__name__``) is
    reflected in the count and names immediately. Adding an unrelated
    module-level ``check_*`` callable does **not** appear: the registry is a
    curated audit-rule set, not a ``check_*`` prefix scrape.

    :returns: A multi-line summary: a count header followed by one
        indented line per registered check callable.
    """
    checks = _registered_audit_checks()
    lines = [
        f"Maestro audit-check registry — {len(checks)} deterministic, "
        f"zero-SDK-cost check callable(s) over behavioral records:"
    ]
    lines.extend(f"  - {fn.__name__}" for fn in checks)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-family classification
# ---------------------------------------------------------------------------

# Regex extracting the first path component after ``maestro/`` in a file
# path.  Handles both ``maestro/foo.py`` (single-file module → ``foo``)
# and ``maestro/foo/bar.py`` (package → ``foo``).  The ``(?:\.py|/)``
# suffix disambiguates module references from prose that happens to
# contain ``maestro/`` followed by an unrelated word.
_MODULE_PATH_RE = re.compile(r"maestro/(\w+?)(?:\.py\b|/)")

# Regex extracting a module name from a test file path such as
# ``tests/test_consciousness.py`` or ``tests/test_sdk.py``. Because ``_``
# is a word character, the capture group always grabs the full filename
# stem (e.g. ``memory_archive`` from ``tests/test_memory_archive.py``).
# Stems that don't exactly match a known module family are then resolved
# via :func:`_resolve_test_stem_family`, which longest-prefix-matches
# against :data:`_MODULE_FAMILIES` so sub-feature test files
# (``test_memory_archive.py`` → ``memory``) still contribute to the
# family count.
_TEST_PATH_RE = re.compile(r"tests/test_(\w+)\.py\b")

# Known top-level module families within the ``maestro`` package.
# Single-file modules and sub-packages are listed at the same level —
# a task touching ``maestro/dashboard/routes.py`` is classified as
# ``"dashboard"`` just like one touching ``maestro/sdk.py`` is ``"sdk"``.
_MODULE_FAMILIES: frozenset[str] = frozenset({
    "agent",
    "artifact_review",
    "audit_checks",
    "cli",
    "config",
    "conscious_workspace",
    "consciousness",
    "consciousness_eval",
    "contributions",
    "cost_tracking",
    "counterfactual_probe",
    "crash",
    "dashboard",
    "diagnostics",
    "dreaming",
    "email",
    "email_tools",
    "evolution",
    "executor",
    "fileutil",
    "health_check",
    "issues",
    "journal",
    "logs",
    "memory",
    "messaging",
    "migration",
    "notifications",
    "planner",
    "prompts",
    "publication",
    "sdk",
    "search",
    "secret_scan",
    "stats",
    "task_cost_gate",
    "telegram_tools",
    "toml_config",
    "tools",
    "types",
    "vcs",
    "workspace_candidates",
})

# Keyword aliases that map common prose terms to their canonical module
# family.  Checked only in the keyword-fallback path (no file paths
# found).  Order doesn't matter — the most-frequent hit wins via
# Counter.  Keys are lowercase; matched against lowercased text.
_KEYWORD_ALIASES: dict[str, str] = {
    "telegram": "telegram_tools",
    "self-model": "consciousness",
    "self_model": "consciousness",
    "selfmodel": "consciousness",
    "self model": "consciousness",
    "identity prompt": "consciousness",
    "identity context": "consciousness",
    "dream": "dreaming",
    "evolve": "evolution",
    "evolution plan": "evolution",
    "artifact": "artifact_review",
    "publish": "publication",
    "notification": "notifications",
    "cost tracking": "cost_tracking",
    "cost gate": "cost_tracking",
    "cost per task": "cost_tracking",
    "budget throttle": "cost_tracking",
    "token budget": "cost_tracking",
    "burn rate": "cost_tracking",
    "health": "health_check",
    "diagnostic": "diagnostics",
    "secret": "secret_scan",
    "git push": "vcs",
    "git commit": "vcs",
    "git merge": "vcs",
    "git diff": "vcs",
    "task branch": "vcs",
    "branch isolation": "vcs",
}


def _resolve_test_stem_family(stem: str) -> str | None:
    """Map a ``tests/test_<stem>.py`` capture to a known module family.

    Exact match wins (``test_memory.py`` → ``memory``). Otherwise the
    longest known family that is a ``_``-bounded prefix of the stem wins
    (``test_memory_archive.py`` → ``memory``;
    ``test_cost_tracking_smoke.py`` → ``cost_tracking``). Returns
    ``None`` when no family applies — callers should treat that as "not
    a recognised module reference."

    The ``_``-boundary requirement prevents pathological collisions
    like ``test_dreaming_x.py`` matching the bare ``dream`` keyword
    alias — only true module-family names participate. Longest-prefix
    wins so multi-token family names (``cost_tracking``) don't get
    pre-empted by a shorter family that happens to share a prefix.
    """
    if stem in _MODULE_FAMILIES:
        return stem
    best: str | None = None
    for family in _MODULE_FAMILIES:
        if stem.startswith(family + "_") and (
            best is None or len(family) > len(best)
        ):
            best = family
    return best


def _collect_task_texts(task: dict[str, object]) -> str:
    """Concatenate all text fields from a task dict for classification.

    Handles both raw JSON dicts (from ``task_history.jsonl``) and
    dataclass-converted forms.  Extracts ``goal``, ``result_summary``,
    ``key_findings``, ``branch``, plan-step descriptions/results, and
    error strings.
    """
    parts: list[str] = []
    for key in ("goal", "result_summary", "key_findings", "branch"):
        val = task.get(key)
        if isinstance(val, str):
            parts.append(val)
    plan = task.get("plan")
    if isinstance(plan, list):
        for step in plan:
            if isinstance(step, dict):
                for skey in ("description", "result"):
                    sval = step.get(skey)
                    if isinstance(sval, str):
                        parts.append(sval)
    errors = task.get("errors")
    if isinstance(errors, list):
        for err in errors:
            if isinstance(err, str):
                parts.append(err)
    return "\n".join(parts)


def classify_module_family(task: dict[str, object]) -> str:
    """Extract the target module family from a task history entry.

    Parses file paths and prose in the task's text fields to identify the
    top-level ``maestro`` module the task primarily targeted.  Returns a
    canonical module name (e.g. ``"consciousness"``, ``"sdk"``,
    ``"dashboard"``) or ``"other"`` when no module can be identified.

    Classification strategy (in priority order):

    1. **File-path extraction** — regexes match ``maestro/<module>.py``,
       ``maestro/<module>/...``, and ``tests/test_<module>.py`` patterns
       in the task's combined text.  The most-frequently referenced known
       module wins.
    2. **Keyword aliases** — common prose terms (e.g. "self-model" →
       ``"consciousness"``, "telegram" → ``"telegram_tools"``) are
       checked against the combined text.
    3. **Bare module-name match** — each known module family name is
       tested as a word-boundary regex against the text.
    4. **Fallback** — ``"other"`` for tasks that cannot be classified.

    Pure function: no I/O, no side effects.
    """
    combined = _collect_task_texts(task)
    if not combined.strip():
        return "other"

    # --- Phase 1: file-path extraction ---
    counts: Counter[str] = Counter()
    for match in _MODULE_PATH_RE.finditer(combined):
        family = match.group(1)
        if family in _MODULE_FAMILIES:
            counts[family] += 1
    for match in _TEST_PATH_RE.finditer(combined):
        resolved = _resolve_test_stem_family(match.group(1))
        if resolved is not None:
            counts[resolved] += 1
    if counts:
        return counts.most_common(1)[0][0]

    # --- Phase 2: keyword aliases ---
    lower = combined.lower()
    alias_counts: Counter[str] = Counter()
    for keyword, family in _KEYWORD_ALIASES.items():
        # Count word-boundary occurrences of each keyword. ``\b`` guards
        # against substring false positives (``git`` ⊂ ``legitimate``,
        # ``cost`` ⊂ ``costly``); multi-token phrase keys match as bounded
        # runs because ``re.escape`` keeps their internal whitespace literal.
        n = len(re.findall(rf"\b{re.escape(keyword)}\b", lower))
        if n > 0:
            alias_counts[family] += n
    if alias_counts:
        return alias_counts.most_common(1)[0][0]

    # --- Phase 3: bare module-name word-boundary match ---
    name_counts: Counter[str] = Counter()
    # ``sorted()`` is load-bearing: frozenset iteration order is
    # hash-randomized per process, and ``name_counts`` insertion order
    # decides ``most_common`` ties. Sorting makes the tie-break the
    # alphabetically-first family — deterministic across processes.
    for family in sorted(_MODULE_FAMILIES):
        pattern = re.compile(rf"\b{re.escape(family)}\b")
        n = len(pattern.findall(lower))
        if n > 0:
            name_counts[family] += n
    if name_counts:
        return name_counts.most_common(1)[0][0]

    # --- Phase 4: unclassifiable ---
    return "other"



# Maximum hops a :func:`lineage_keys` walk traverses before bailing out.
# Bounds work and breaks pathological cycles in the predecessor chain.
# A real refinement chain ``Q1 → Q1' → Q1''`` is always 1-3 hops; ten
# leaves headroom for runs of repeated REFINEs the LLM piles on a single
# question without paying audit cost.
_LINEAGE_MAX_HOPS = 10


def lineage_keys(
    current_question: str,
    resolutions: list[QuestionResolution],
    *,
    max_hops: int = _LINEAGE_MAX_HOPS,
) -> set[str]:
    """Return normalized keys for *current_question* and its predecessors.

    Walks *resolutions* backward via the ``refined_to`` edge: a row whose
    ``refined_to`` (modulo :func:`_normalize_question`) matches the
    current walk target identifies the *predecessor* whose ``question``
    becomes the next walk target. The set returned is suitable for
    aggregating evidence across reworded variants of the same logical
    question — a downstream caller can sum
    ``r where _normalize_question(r.question) in keys`` to get a
    lineage-aware count instead of a per-wording count.

    *max_hops* caps the walk so a corrupt chain (cycle, or pathological
    re-refinement loop) cannot stall the audit; the walk also terminates
    immediately if a key is revisited. Empty input or absence of any
    ``refined_to`` edge degrades cleanly to ``{normalize(current)}`` —
    the lineage-aware aggregation matches the legacy per-wording count
    in that base case.

    Pure: no I/O, no logging. Both rule 5 and the audit-stall actuator
    use this helper to fold lineage into their accounting.
    """
    current_norm = _normalize_question(current_question)
    if not current_norm:
        return set()

    # Build the predecessor map once. Most-recent-wins on collision —
    # later rows override earlier ones — so a re-refined chain stays
    # linear: if the LLM refines Q' back to Q'', the most recent
    # ``refined_to == Q''`` edge points at Q', not at some earlier
    # ancestor. ``resolutions`` is expected file-ordered (oldest first);
    # iteration in order makes this overwrite semantics correct.
    predecessor_by_target: dict[str, str] = {}
    for r in resolutions:
        if r.refined_to is None:
            continue
        target_norm = _normalize_question(r.refined_to)
        if not target_norm:
            continue
        predecessor_by_target[target_norm] = r.question

    keys: set[str] = {current_norm}
    walk_target = current_norm
    for _ in range(max_hops):
        predecessor = predecessor_by_target.get(walk_target)
        if predecessor is None:
            break
        predecessor_norm = _normalize_question(predecessor)
        if not predecessor_norm or predecessor_norm in keys:
            break
        keys.add(predecessor_norm)
        walk_target = predecessor_norm
    return keys


def limitations_over_generative_debt(
    limitations: list[str],
    resolutions: list[QuestionResolution],
    tasks: list[TaskRecord],
    plans: list[EvolutionPlan],
    *,
    threshold: int,
) -> set[str]:
    """Return the ``[limitation]`` question keys carrying too much unlanded
    plan debt to keep re-surfacing (proposal 116's fuel-starve).

    For each *limitations* entry the ``[limitation]`` candidate string is built
    through :func:`maestro.consciousness.limitation_question_candidate` — the
    sole owner of that format, so this reader can never drift from the
    surfacing path. Its ``refined_to`` lineage is folded into one key set via
    :func:`lineage_keys`, and then:

    * the ``task_id`` of every task whose ``linked_question`` normalises into
      that lineage is collected, and
    * the ``obsolete``/``rejected`` plans whose ``source_task_id`` is one of
      those ids are counted.

    Because ``PlanStatus`` is a single scalar and ``merged`` is disjoint from
    ``obsolete``/``rejected``, "reached a terminal state without landing" is
    exactly "current status is ``obsolete`` or ``rejected``" — no separate
    merge check is needed. A limitation whose count meets *threshold*
    contributes its :func:`_normalize_question` candidate key to the returned
    set, matching the key
    :meth:`Consciousness.surface_limitations_as_questions` dedups on.

    Pure: no I/O, no SDK, no git/host calls. A *threshold* below 1 or empty
    inputs return an empty set.
    """
    if threshold < 1:
        return set()

    # Count, per source_task_id, the obsolete/rejected plans citing it. Each
    # plan carries a single source_task_id, so a task cited by N terminal-
    # unlanded plans contributes N to its lineage's debt without double count.
    unlanded_by_task: Counter[str] = Counter()
    for plan in plans:
        if not isinstance(plan, EvolutionPlan):
            continue
        if plan.status not in ("obsolete", "rejected"):
            continue
        task_id = plan.source_task_id
        if isinstance(task_id, str) and task_id:
            unlanded_by_task[task_id] += 1

    # Group the task_ids anchored on each normalised linked_question so the
    # lineage walk can union them across reworded question variants.
    task_ids_by_question: dict[str, set[str]] = {}
    for task in tasks:
        if not isinstance(task, TaskRecord):
            continue
        linked = task.linked_question
        task_id = task.task_id
        if not (isinstance(linked, str) and linked):
            continue
        if not (isinstance(task_id, str) and task_id):
            continue
        task_ids_by_question.setdefault(
            _normalize_question(linked), set(),
        ).add(task_id)

    suppressed: set[str] = set()
    for entry in limitations:
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        if not text:
            continue
        candidate = limitation_question_candidate(text)
        lineage = lineage_keys(candidate, resolutions)
        lineage_task_ids: set[str] = set()
        for q_key in lineage:
            lineage_task_ids |= task_ids_by_question.get(q_key, set())
        debt = sum(unlanded_by_task[t] for t in lineage_task_ids)
        if debt >= threshold:
            suppressed.add(_normalize_question(candidate))
    return suppressed


# Minimum number of uncited successful kept-task branches before rule 7
# (``kept_task_not_promoted``) fires. Aligned with the rule-3 / rule-4
# short-arc floor and with ``Evolution._validate_source_task_id``'s
# 30-task lookback semantics — three independent kept branches is the
# point at which the LLM's failure to nominate any of them as a
# ``source_task_id`` becomes a pattern rather than noise.
_KEPT_TASK_NOT_PROMOTED_THRESHOLD = 3

# Per-rule cap for ``check_kept_task_not_promoted``. There is exactly one
# issue mechanism (the un-promotion gap) so a single finding per cycle
# captures the full signal; a higher cap would only repeat the same
# statement with different cites. Non-deferred — the rule is a
# mechanical floor (count + evidence), not a meta-rule, so it competes
# alphabetically with the legacy rules under round-robin clipping.
_KEPT_TASK_NOT_PROMOTED_PER_RULE_CAP = 1

# Fixed statement for ``check_kept_task_not_promoted``. Deterministic
# (no embedded count, no embedded task IDs) so
# :func:`compute_audit_stall_counts` normalises identically across
# cycles even as the underlying cited task IDs roll over the 30-task
# window. Evidence rolls into ``cites``.
_KEPT_TASK_NOT_PROMOTED_STATEMENT = (
    "Successful task branches without source_task_id follow-up plan."
)

# Minimum repeat-targeting count before rule 5 (``stalled_question``)
# fires. A question must have been the linked target of
# ≥ 3 self-directed task cycles whose most-recent disposition was
# non-resolving.
_STALLED_THRESHOLD = 3

# Per-rule cap for ``check_stalled_question`` — overrides the shared
# :data:`MAX_FINDINGS_PER_RULE`. Deferring this rule in the round-robin
# (see :data:`_DEFERRED_RULES`) keeps its single candidate from
# displacing a non-deferred rule under the
# :data:`MAX_FINDINGS_PER_CYCLE` clip. Yielding does not by itself
# guarantee every legacy rule a slot — see :data:`_DEFERRED_RULES`.
_STALLED_PER_RULE_CAP = 1

# Per-rule cap for :func:`check_stale_self_model_entry`. Same shape as
# :data:`_STALLED_PER_RULE_CAP` — rule 6 is deferred (see
# :data:`_DEFERRED_RULES`) so its single candidate yields to the
# non-deferred rules under the round-robin clip.
_STALE_ENTRY_PER_RULE_CAP = 1

# Source fields rule 6 inspects. ``limitations_known`` is excluded —
# proposal 046's :func:`Consciousness.surface_limitations_as_questions`
# already gives that field a liveness loop via
# ``[limitation]`` open-question mirroring. ``open_questions`` is
# excluded — :func:`check_stalled_question` (rule 5) already owns its
# stall-detection lifecycle. Tuple-typed for stable iteration order
# in the resulting findings.
_STALE_ENTRY_FIELDS: tuple[str, ...] = (
    "identity_statements",
    "capabilities_known",
    "values",
)

# A self-model entry counts as *live* when at least this many of its
# stopword-stripped content tokens (or all of them, whichever is fewer)
# appear in any single corpus item via prefix-match. Two-token floor
# mirrors :data:`_LIMITATION_MATCH_MIN_TOKENS` and prevents single
# stopword-adjacent matches from spuriously keeping a dead entry alive.
_STALE_ENTRY_MIN_TOKEN_OVERLAP = 2

# Field-cardinality floor — rule 6 stays silent when the source field
# has fewer than this many entries. Sparse fields are load-bearing by
# composition; flagging there would force premature REMOVEs. Mirrors
# the :data:`_MIN_TASKS_FOR_SUCCESS_CHECK` floor on rule 1.
_STALE_ENTRY_MIN_FIELD_CARDINALITY = 3

# Rules whose findings yield to higher-priority rules under round-robin
# clipping. Newer rules opt in here so their first finding is appended
# *after* the longer-standing rules' first finding, freeing slots for the
# legacy rules when the candidate count exceeds
# :data:`MAX_FINDINGS_PER_CYCLE`. Deferring does *not* guarantee every
# legacy rule keeps a slot: ``kept_task_not_promoted`` is deliberately
# non-deferred, so five non-deferred rules contend for four slots and the
# alphabetically last (``success_claim_vs_rate``) is starved when all five
# fire — pinned by
# ``test_run_all_checks_round_robin_starves_last_legacy_rule``. Add a new
# rule here unless it must outrank the legacy set.
_DEFERRED_RULES: frozenset[str] = frozenset({
    "stalled_question",
    "stale_entry",
    "unbound_capability_claim",
    "closed_reference",
})


def _rule_priority(name: str) -> tuple[int, str]:
    """Sort key that pushes :data:`_DEFERRED_RULES` to the end of the rotation."""
    return (1 if name in _DEFERRED_RULES else 0, name)

# Minimum shared tokens between a limitation and an error fingerprint
# required to consider the limitation a match (and therefore silence the
# rule). Raising this to 2 keeps single-stopword matches from producing
# false negatives; stopwords are also filtered out of the token sets.
_LIMITATION_MATCH_MIN_TOKENS = 2

# Keywords asserting capability effectiveness. Matched as token prefixes
# via :func:`_matches_keyword` so ``effective``, ``effectively``, and
# ``effectiveness`` all hit but ``ineffective`` does not.
#
# ``plan`` is deliberately absent: it substring-matches neutral phrasing
# like "I plan tasks systematically" and would produce churn without
# signal.
_SUCCESS_KEYWORDS: frozenset[str] = frozenset({
    "effective",
    "efficient",
    "successful",
    "complete",
})

# Keywords asserting operational stability. Prefix-matched like
# ``_SUCCESS_KEYWORDS``.
_STABILITY_KEYWORDS: frozenset[str] = frozenset({
    "stable",
    "reliable",
    "robust",
    "consistent",
})

# Stopwords stripped from both error fingerprints and limitation
# statements before computing the ``≥2 shared tokens`` overlap for rule
# 3. Without this, a limitation like "error when running tests" would
# share ``{"error", "when"}`` with nearly any error fingerprint and
# silence the rule for unrelated failures. Conservative list: only
# English function words; content words like ``error`` / ``failed`` /
# ``exception`` are kept because they carry semantic weight in failure
# contexts.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can",
    "did", "do", "does", "for", "from", "had", "has", "have", "he",
    "her", "hers", "him", "his", "i", "if", "in", "into", "is",
    "it", "its", "my", "of", "on", "or", "she", "so", "than",
    "that", "the", "their", "them", "then", "there", "these",
    "they", "this", "those", "to", "was", "we", "were", "what",
    "when", "where", "which", "while", "who", "whom", "will",
    "with", "would", "you", "your",
})


AuditKind = Literal[
    "contradicted_claim",
    "undeclared_limitation",
    "stalled_question",
    "stale_entry",
    "unmerged_work",
    "unreviewed_evolution",
    "phantom_escalation",
    "unconfirmed_landing",
    "comfort_cluster",
    "owned_act_drought",
    "phantom_partial_prune",
    "undercredited_landing",
    "reconcile_near_miss",
    "banked_owned_act_reperformance",
    "unbound_claim",
    "closed_reference",
]


@dataclass(frozen=True)
class AuditFinding:
    """One rule hit from a mechanical self-audit cycle.

    *kind* — coarse classification consumed by the prompt section.
    *statement* — deterministic one-sentence description (bounded).
    *cites* — task_ids from the corpus that evidence the finding.
    *rule* — name of the check function (stable string for telemetry
    and round-robin ordering).
    """

    kind: AuditKind
    statement: str
    cites: list[str] = field(default_factory=list)
    rule: str = ""


def _matches_keyword(text: str, keywords: frozenset[str]) -> bool:
    """Return ``True`` when any token in *text* starts with any keyword.

    Prefix matching catches inflected forms (``effective`` →
    ``effectively``, ``effectiveness``) while rejecting explicit
    negations (``ineffective``, ``unstable``) because their tokens do
    not start with the keyword stem.
    """
    for token in tokenize(text):
        for kw in keywords:
            if token.startswith(kw):
                return True
    return False


def _content_tokens(text: str) -> set[str]:
    """Tokens from *text* minus stopwords. Used for rule 3 overlap."""
    return {t for t in tokenize(text) if t and t not in _STOPWORDS}


def _fingerprint_error(err: str) -> str:
    """Normalise an error string to a stable first-line fingerprint.

    The first non-empty line, lowercased and with runs of whitespace
    collapsed to single spaces. Returns ``""`` when *err* is empty or
    whitespace-only.
    """
    if not isinstance(err, str):
        return ""
    for raw_line in err.splitlines():
        stripped = raw_line.strip()
        if stripped:
            return " ".join(stripped.lower().split())
    return ""


def _trailing_failure_streak(tasks: list[TaskRecord]) -> int:
    """Count consecutive ``failure`` outcomes from the end of *tasks*."""
    count = 0
    for task in reversed(tasks):
        if task.outcome == "failure":
            count += 1
        else:
            break
    return count


def _format_rate_percent(rate: float) -> str:
    """Render a [0, 1] success rate as a short percentage string."""
    return f"{round(rate * 100)}%"


def check_success_claim_vs_rate(
    self_model: SelfModel,
    tasks: list[TaskRecord],
    success_rate: float,
) -> list[AuditFinding]:
    """Rule 1 — capability claims effectiveness but rate is below threshold.

    Fires when any ``capabilities_known`` entry matches a success
    keyword (via prefix-matched tokens) AND ``len(tasks) >= 5`` AND
    ``success_rate < 0.6``. One finding per matching capability, capped
    at :data:`MAX_FINDINGS_PER_RULE`.
    """
    if len(tasks) < _MIN_TASKS_FOR_SUCCESS_CHECK:
        return []
    if success_rate >= _SUCCESS_RATE_THRESHOLD:
        return []

    non_success_ids = [
        t.task_id for t in tasks if t.outcome != "success"
    ][:MAX_CITES_PER_FINDING]

    findings: list[AuditFinding] = []
    for capability in self_model.capabilities_known:
        if not isinstance(capability, str):
            continue
        if not _matches_keyword(capability, _SUCCESS_KEYWORDS):
            continue
        findings.append(AuditFinding(
            kind="contradicted_claim",
            statement=(
                f"Capability claims effectiveness ({capability!r}) but "
                f"recent success rate is "
                f"{_format_rate_percent(success_rate)} over "
                f"{len(tasks)} tasks."
            ),
            cites=list(non_success_ids),
            rule="success_claim_vs_rate",
        ))
        if len(findings) >= MAX_FINDINGS_PER_RULE:
            break
    return findings


def check_stability_claim_vs_failure_streak(
    self_model: SelfModel,
    tasks: list[TaskRecord],
) -> list[AuditFinding]:
    """Rule 2 — capability claims stability during a failure streak.

    Fires when any ``capabilities_known`` entry matches a stability
    keyword (via prefix-matched tokens) AND the trailing failure streak
    is ≥ 3. One finding per matching capability, capped at
    :data:`MAX_FINDINGS_PER_RULE`.
    """
    streak = _trailing_failure_streak(tasks)
    if streak < _FAILURE_STREAK_THRESHOLD:
        return []

    failure_ids = [
        t.task_id for t in tasks if t.outcome == "failure"
    ][-streak:][:MAX_CITES_PER_FINDING]

    findings: list[AuditFinding] = []
    for capability in self_model.capabilities_known:
        if not isinstance(capability, str):
            continue
        if not _matches_keyword(capability, _STABILITY_KEYWORDS):
            continue
        findings.append(AuditFinding(
            kind="contradicted_claim",
            statement=(
                f"Capability claims stability ({capability!r}) but the "
                f"last {streak} tasks all ended in failure."
            ),
            cites=list(failure_ids),
            rule="stability_claim_vs_failure_streak",
        ))
        if len(findings) >= MAX_FINDINGS_PER_RULE:
            break
    return findings


def check_unbound_capability_claim(
    self_model: SelfModel,
) -> list[AuditFinding]:
    """Rule — a capability claim carrying no *resolving* evidence record.

    Reads back the ``SelfModel.claim_evidence`` map recorded by
    ``update_self_model`` (proposal 129, evidence-bound self-knowledge).
    Fires for every ``capabilities_known`` entry whose :func:`claim_key`
    fold is either absent from the map (never cited) or present with a
    ``status`` other than ``"resolved"`` — the citation pointed at a
    non-existent, failed, or unparseable anchor (``absent`` /
    ``unresolvable`` / ``wrong_polarity``). Reads the persisted status;
    never re-resolves — existence + polarity, never entailment.

    Pure function, no I/O, mirroring the sibling capability-claim rules.
    Dispatched only behind ``[audit] evidence_bound_claims_enabled`` (via
    :func:`run_all_checks`): with the flag off the map is empty, so every
    claim would look unbound. One finding per claim, capped at
    :data:`MAX_FINDINGS_PER_RULE`; ``cites`` is empty because an unbound
    claim has no supporting task to cite. Self-extinguishing — the finding
    disappears once the claim gains a ``resolved`` citation or is dropped
    from ``capabilities_known`` (whose evidence ``save_self_model`` prunes).
    """
    findings: list[AuditFinding] = []
    for capability in self_model.capabilities_known:
        if not isinstance(capability, str):
            continue
        record = self_model.claim_evidence.get(claim_key(capability))
        if isinstance(record, dict):
            status = record.get("status", "")
            if status == "resolved":
                continue
            statement = (
                f"Capability claims {capability!r} but its cited evidence "
                f"did not resolve (status: {status!r})."
            )
        else:
            statement = (
                f"Capability claims {capability!r} but no evidence citation "
                f"is recorded for it."
            )
        findings.append(AuditFinding(
            kind="unbound_claim",
            statement=statement,
            cites=[],
            rule="unbound_capability_claim",
        ))
        if len(findings) >= MAX_FINDINGS_PER_RULE:
            break
    return findings


#: Self-model list fields whose entries rule 23 checks for closed citations.
_CLOSED_REFERENCE_FIELDS: tuple[str, ...] = (
    "identity_statements",
    "values",
    "capabilities_known",
    "limitations_known",
    "open_questions",
)
_CLOSED_REFERENCE_PER_RULE_CAP = 1
#: Bound on host lookups per audit cycle — one ``gh api`` call each.
_CLOSED_REFERENCE_MAX_LOOKUPS = 10
#: ``open_questions`` prefixes that mark rule output or mirrors, not claims —
#: the bracket-open forms so ``[audit refuted] …`` rewrites are skipped too.
_CLOSED_REFERENCE_SKIP_PREFIXES: tuple[str, ...] = (
    Consciousness._AUDIT_PREFIX,
    Consciousness._LIMITATION_PREFIX,
)


def _closed_reference_entries(
    self_model: SelfModel,
) -> list[tuple[str, str, set[int]]]:
    """Yield ``(field, entry, cited)`` for every authored entry citing ``#NNN``."""
    out: list[tuple[str, str, set[int]]] = []
    for field_name in _CLOSED_REFERENCE_FIELDS:
        for entry in getattr(self_model, field_name):
            if not isinstance(entry, str):
                continue
            if field_name == "open_questions" and entry.startswith(
                _CLOSED_REFERENCE_SKIP_PREFIXES,
            ):
                continue
            cited = extract_issue_numbers(entry)
            if cited:
                out.append((field_name, entry, cited))
    return out


def collect_cited_issue_numbers(self_model: SelfModel) -> list[int]:
    """Sorted ``#NNN`` numbers cited by rule-23 entries, capped for lookup.

    Admitted entry by entry: an entry's whole cited set joins only while it
    fits the remaining :data:`_CLOSED_REFERENCE_MAX_LOOKUPS` budget, so no
    lookup is spent on an entry that could never resolve all-closed.
    """
    cited: set[int] = set()
    for _field, _entry, numbers in _closed_reference_entries(self_model):
        if len(cited | numbers) <= _CLOSED_REFERENCE_MAX_LOOKUPS:
            cited |= numbers
    return sorted(cited)


def resolve_closed_refs(
    self_model: SelfModel,
    lookup: Callable[[int], str | None],
) -> frozenset[int]:
    """Return the cited numbers *lookup* confirms ``"closed"``.

    An ``"open"`` answer is dropped, so rule 23 only ever sees
    confirmed-closed numbers. An unknown (``None``) answer stops the walk —
    it usually means the host is unreachable or unconfigured, and further
    calls would only repeat the same failure (and its warning).
    """
    closed: set[int] = set()
    for n in collect_cited_issue_numbers(self_model):
        state = lookup(n)
        if state is None:
            break
        if state == "closed":
            closed.add(n)
    return frozenset(closed)


def check_closed_reference_claim(
    self_model: SelfModel,
    closed_refs: frozenset[int],
) -> list[AuditFinding]:
    """Rule 23 — a self-model entry whose every cited ``#NNN`` is closed.

    A partially-open or unknown citation stays silent: *closed_refs* holds
    only host-confirmed closed numbers. Pure; capped at
    :data:`_CLOSED_REFERENCE_PER_RULE_CAP`. Self-extinguishing once the
    entry is removed or rewritten.
    """
    findings: list[AuditFinding] = []
    for field_name, entry, cited in _closed_reference_entries(self_model):
        if not cited <= closed_refs:
            continue
        numbers = ", ".join(f"#{n}" for n in sorted(cited))
        findings.append(AuditFinding(
            kind="closed_reference",
            # Field, numbers and directive lead so the ``[:160]`` clip in
            # ``_audit_self_model`` only ever trims the entry excerpt.
            statement=(
                f"{field_name} cites {numbers} — all closed; REMOVE if it "
                f"asserts live work, else rewrite as history: {entry[:120]!r}"
            ),
            cites=[],
            rule="closed_reference",
        ))
        if len(findings) >= _CLOSED_REFERENCE_PER_RULE_CAP:
            break
    return findings


def check_repeated_error_without_limitation(
    self_model: SelfModel,
    tasks: list[TaskRecord],
) -> list[AuditFinding]:
    """Rule 3 — same error fingerprint ≥3x with no matching limitation.

    Normalizes each error to a first-line fingerprint. Fires when any
    fingerprint appears ≥ 3 times in the corpus AND no
    ``limitations_known`` entry shares ≥ 2 non-stopword tokens with it.
    Fingerprints sorted by descending count (ties broken
    lexicographically) so the most common error is reported first.
    """
    fingerprint_counts: Counter[str] = Counter()
    fp_to_task_ids: dict[str, list[str]] = {}

    for task in tasks:
        for err in task.errors or []:
            fp = _fingerprint_error(err)
            if not fp:
                continue
            fingerprint_counts[fp] += 1
            fp_to_task_ids.setdefault(fp, []).append(task.task_id)

    if not fingerprint_counts:
        return []

    limitation_token_sets: list[set[str]] = [
        _content_tokens(lim)
        for lim in self_model.limitations_known
        if isinstance(lim, str)
    ]

    findings: list[AuditFinding] = []
    for fp, count in sorted(
        fingerprint_counts.items(), key=lambda kv: (-kv[1], kv[0]),
    ):
        if count < _ERROR_REPETITION_THRESHOLD:
            continue
        fp_tokens = _content_tokens(fp)
        if any(
            len(fp_tokens & lim_tokens) >= _LIMITATION_MATCH_MIN_TOKENS
            for lim_tokens in limitation_token_sets
        ):
            continue

        deduped_cites: list[str] = []
        for tid in fp_to_task_ids.get(fp, []):
            if tid not in deduped_cites:
                deduped_cites.append(tid)
            if len(deduped_cites) >= MAX_CITES_PER_FINDING:
                break

        findings.append(AuditFinding(
            kind="undeclared_limitation",
            statement=(
                f"Error pattern {fp[:140]!r} recurred {count} times across "
                f"recent tasks but is not reflected in limitations_known."
            ),
            cites=deduped_cites,
            rule="repeated_error_without_limitation",
        ))
        if len(findings) >= MAX_FINDINGS_PER_RULE:
            break
    return findings


def check_repeated_target_attempt(
    self_model: SelfModel,
    tasks: list[TaskRecord],
    settled_prs: frozenset[int] = frozenset(),
) -> list[AuditFinding]:
    """Rule 4 — same GitHub issue targeted ≥ N distinct tasks without limitation.

    Counts the distinct *tasks* whose ``goal`` mentions each ``#NNN``
    (per-task set semantics — a goal mentioning ``#1566`` three times
    still contributes one attempt). Fires when any issue number's task
    count reaches :data:`_REPEATED_TARGET_THRESHOLD` AND no
    ``limitations_known`` entry already references that exact issue
    (matched on the complete issue number — whole-number boundary, not
    a digit prefix). Silenced once Claude integrates the pattern as a
    limitation, mirroring rule 3's self-extinguish parity.

    *settled_prs* pre-filters an issue whose number names a PR the
    standing settlement reconciler has recorded as settled (merged or
    closed) — mirroring the phantom rules' ``settled_prs`` parameter
    shape. Laps that repeatedly name ``#NNN`` are often the remediation
    that *landed* that PR, the opposite of an unlearned limitation, so a
    settled ``#NNN`` is skipped before the threshold test. The empty
    default makes the rule byte-identical to the pre-suppression
    behaviour.

    Issues with the highest task count are reported first, with ties
    broken by lowest issue number for determinism. Capped at
    :data:`MAX_FINDINGS_PER_RULE`.
    """
    issue_counts: Counter[int] = Counter()
    issue_to_task_ids: dict[int, list[str]] = {}

    for task in tasks:
        # Per-task set semantics: a single goal that mentions ``#1566``
        # three times still counts as one attempt, so the finding's
        # attempt count matches the number of distinct cited task ids.
        for n in extract_issue_numbers(task.goal or ""):
            issue_counts[n] += 1
            issue_to_task_ids.setdefault(n, []).append(task.task_id)

    if not issue_counts:
        return []

    limitation_strings = [
        lim for lim in self_model.limitations_known if isinstance(lim, str)
    ]

    findings: list[AuditFinding] = []
    for issue_num, count in sorted(
        issue_counts.items(), key=lambda kv: (-kv[1], kv[0]),
    ):
        if issue_num in settled_prs:
            # Laps that repeatedly name a settled ``#NNN`` are the
            # remediation that landed it, not an unlearned limitation —
            # skip before the threshold test so the finding never fires.
            continue
        if count < _REPEATED_TARGET_THRESHOLD:
            continue
        # Match the whole issue number, not a digit prefix: "#1566" must
        # not be silenced by a limitation naming "#15666" (and "#1" must
        # not be silenced by "#10"). The leading "#" anchors the front;
        # the negative lookahead closes the trailing-digit boundary.
        marker = re.compile(rf"#{issue_num}(?!\d)")
        if any(marker.search(lim) for lim in limitation_strings):
            continue

        deduped_cites: list[str] = []
        for tid in issue_to_task_ids.get(issue_num, []):
            if tid not in deduped_cites:
                deduped_cites.append(tid)
            if len(deduped_cites) >= MAX_CITES_PER_FINDING:
                break

        findings.append(AuditFinding(
            kind="undeclared_limitation",
            statement=(
                f"Issue #{issue_num} has appeared in {count} recent tasks "
                f"but is not reflected in limitations_known."
            ),
            cites=deduped_cites,
            rule="repeated_target_attempt",
        ))
        if len(findings) >= MAX_FINDINGS_PER_RULE:
            break
    return findings


def check_kept_task_not_promoted(
    self_model: SelfModel,
    tasks: list[TaskRecord],
    cited_source_task_ids: set[str],
) -> list[AuditFinding]:
    """Rule 7 — ≥ N successful kept-task branches lack a source_task_id citation.

    Filters *tasks* to those with ``outcome in ("success", "partial")``
    AND non-empty ``branch``, then subtracts every task whose
    ``task_id`` already appears in *cited_source_task_ids* (the union of
    citations across all ``EvolutionPlan`` rows, regardless of
    ``status`` — see :meth:`Memory.get_cited_source_task_ids`). Fires
    when the residual count reaches
    :data:`_KEPT_TASK_NOT_PROMOTED_THRESHOLD` AND emits exactly one
    finding (per-rule cap :data:`_KEPT_TASK_NOT_PROMOTED_PER_RULE_CAP`).

    Statement is the fixed text :data:`_KEPT_TASK_NOT_PROMOTED_STATEMENT`
    (no embedded count, no embedded IDs) so
    :func:`compute_audit_stall_counts` matches identically across
    cycles even as the underlying tasks roll over the recent window.
    Cites are the uncited task_ids, sorted ascending and capped at
    :data:`MAX_CITES_PER_FINDING` for determinism.

    *self_model* is unused but accepted for parity with the other rule
    signatures — keeps :func:`run_all_checks` registration uniform and
    leaves room for a future limitation-mute condition without breaking
    the call shape.
    """
    del self_model  # parity-only; see docstring

    uncited_ids = sorted({
        t.task_id
        for t in tasks
        if t.outcome in ("success", "partial")
        and isinstance(t.branch, str)
        and t.branch
        and isinstance(t.task_id, str)
        and t.task_id
        and t.task_id not in cited_source_task_ids
    })
    if len(uncited_ids) < _KEPT_TASK_NOT_PROMOTED_THRESHOLD:
        return []
    return [AuditFinding(
        kind="unmerged_work",
        statement=_KEPT_TASK_NOT_PROMOTED_STATEMENT,
        cites=uncited_ids[:MAX_CITES_PER_FINDING],
        rule="kept_task_not_promoted",
    )]


def check_stalled_question(
    self_model: SelfModel,
    resolutions: list[QuestionResolution],
) -> list[AuditFinding]:
    """Rule 5 — same question targeted at least 3 times with no resolution.

    For each currently-open question, walks the ``refined_to`` lineage
    via :func:`lineage_keys` and aggregates evidence across every
    historical wording reachable through the chain. Fires when the
    lineage-wide count reaches :data:`_STALLED_THRESHOLD` AND the most
    recent disposition across the lineage is ``kept`` (i.e., not
    ``removed``, ``refined``, or ``unresolved``). A fresh ``refined`` row
    therefore does **not** satisfy "stall" — refining is not closure;
    it is a wording change, and the refined wording inherits the
    lineage count rather than starting over from zero. An ``unresolved``
    orphan latest row is likewise not a stall: it means the question was
    evicted before any LLM verdict was rendered against the live
    ``open_questions`` list, so it defers the finding until a genuine
    ``kept`` verdict lands rather than tripping the rule on its own.

    Cap is :data:`_STALLED_PER_RULE_CAP` (1), tighter than the shared
    :data:`MAX_FINDINGS_PER_RULE`, so a noisy resolution log can't
    monopolise the cycle's findings budget — see :data:`_DEFERRED_RULES`
    for the priority handling that lets this rule yield to the
    longer-standing legacy rules under the round-robin clip.

    *resolutions* are expected in file order (oldest first); the most
    recent disposition wins by virtue of the last-write semantics in
    the loop. Cites are the most recent task_ids for the offending
    lineage, deduped newest-first across every wording.

    When more than :data:`_STALLED_PER_RULE_CAP` lineages cross the
    threshold in the same cycle, candidates are sorted by
    ``(-count, normalised question)`` before the cap is applied, so the
    most-stalled lineage wins and ties break alphabetically on the
    normalised wording. This keeps the audit-stall actuator pointed at
    the worst offender independent of ``open_questions`` insertion
    order.

    The legacy "still in ``open_questions``" self-extinguishing check is
    automatic under the new shape: the rule iterates over
    ``self_model.open_questions`` directly, so a question removed by
    the LLM disappears from consideration without an explicit guard.
    When ``lineage_keys`` finds no refinement edges the keys collapse
    to ``{normalize(Q)}`` and behaviour matches the legacy
    per-wording count exactly.
    """
    if not resolutions:
        return []

    # Pre-bucket resolutions by normalised question text once, recording
    # each row's file position so cross-bucket merges keep oldest-first
    # order without repeated O(n) scans of *resolutions*.
    rows_by_norm: dict[str, list[tuple[int, QuestionResolution]]] = {}
    for idx, r in enumerate(resolutions):
        norm = _normalize_question(r.question)
        if not norm:
            continue
        rows_by_norm.setdefault(norm, []).append((idx, r))

    # Collect every qualifying candidate first, then sort by
    # ``(-count, normalised question)`` so the most-stalled lineage
    # wins the single-slot cap independent of ``open_questions``
    # insertion order; alphabetic tiebreaker keeps the choice
    # deterministic when counts match.
    candidates: list[tuple[int, str, str, list[str]]] = []
    for q in self_model.open_questions:
        if not isinstance(q, str):
            continue
        keys = lineage_keys(q, resolutions)

        # Lineage-wide bookkeeping: pull every row whose normalised
        # question is in ``keys`` and aggregate count, latest-disposition,
        # and newest-first task_ids across the chain. Sort by file
        # position so cross-bucket recency is well-defined when a chain
        # interleaves rewordings.
        merged: list[tuple[int, QuestionResolution]] = []
        for key in keys:
            merged.extend(rows_by_norm.get(key, []))
        if not merged:
            continue
        if len(merged) < _STALLED_THRESHOLD:
            continue
        merged.sort(key=lambda pair: pair[0])
        lineage_rows = [r for _, r in merged]
        latest_disposition = lineage_rows[-1].disposition
        if latest_disposition != "kept":
            continue

        deduped_cites: list[str] = []
        for r in reversed(lineage_rows):
            tid = r.task_id
            if tid and tid not in deduped_cites:
                deduped_cites.append(tid)
            if len(deduped_cites) >= MAX_CITES_PER_FINDING:
                break

        candidates.append(
            (len(lineage_rows), _normalize_question(q), q, deduped_cites)
        )

    candidates.sort(key=lambda c: (-c[0], c[1]))

    findings: list[AuditFinding] = []
    for count, _norm, q, deduped_cites in candidates[:_STALLED_PER_RULE_CAP]:
        findings.append(AuditFinding(
            kind="stalled_question",
            statement=(
                f"Question {q!r} has been targeted {count} "
                f"times without resolution; consider rephrasing or removing."
            ),
            cites=deduped_cites,
            rule="stalled_question",
        ))
    return findings


def is_grounded_closure(resolution: QuestionResolution) -> bool:
    """Whether a question *resolution* is backed by a successful probe.

    The eyes-veto predicate for proposal 088 Phase 1. A closed
    (``removed``) open question is *grounded* only when the task that
    probed it actually succeeded — otherwise the brain declared a victory
    the eyes never saw. Returns ``False`` only when
    ``disposition == "removed"`` **and** ``outcome != "success"``; every
    other disposition (``kept`` / ``refined`` / ``unresolved``) and every
    success-backed removal is grounded.

    ``outcome`` is the :data:`~maestro.types.TaskOutcome` string literal
    (``"success"`` / ``"partial"`` / ``"failure"``) stamped onto the row
    from the probing task, so the comparison is against the string
    ``"success"`` directly — there is no enum. Pure function over an
    existing row: no I/O, no SDK cost, and it works on historical
    resolutions without migration.
    """
    return not (
        resolution.disposition == "removed"
        and resolution.outcome != "success"
    )


def check_ungrounded_question_closure(
    resolutions: list[QuestionResolution],
    *,
    enabled: bool = True,
) -> list[AuditFinding]:
    """Rule 16 — a closed open question whose only probe did not succeed.

    Proposal 088 Phase 1 gives the eyes a veto over a closure. The
    self-directed-task loop closes an ``open_question`` purely from the
    before/after diff of the list (``_classify_disposition`` in
    :mod:`maestro.agent`); the probing task's ``outcome`` is stamped onto
    the :class:`~maestro.memory.QuestionResolution` row but never consulted,
    so a question a *failed* task probed can be silently marked answered.
    This rule re-surfaces every ungrounded removal (per
    :func:`is_grounded_closure`) as a ``contradicted_claim`` finding, so the
    next ``update_self_model`` must re-open the question or justify the
    closure. It self-extinguishes once the row scrolls out of the read
    window or the question is re-resolved on solid ground: a stale
    ``removed``+non-success row superseded by any *later* resolution of the
    same question — a grounded re-resolution **or** a second ungrounded
    removal (rows arrive oldest→newest in file order) — is dropped, so the
    rule emits at most one finding per question and never re-litigates a
    closure a subsequent successful probe already grounded — only the
    most-recent disposition per question is judged.

    Returns ``[]`` immediately when *enabled* is ``False`` (the default-off
    ``[audit] ungrounded_closure_enabled`` flag), so a disabled cycle is
    byte-identical to today. Non-:class:`~maestro.memory.QuestionResolution`
    rows are skipped defensively. Capped at :data:`MAX_FINDINGS_PER_RULE`
    findings, each citing at most :data:`MAX_CITES_PER_FINDING` task ids.

    Pure function over existing rows: no I/O, no SDK cost, no schema change.
    """
    if not enabled:
        return []
    # Index the most-recent resolution per question (rows arrive oldest→newest
    # in file order, so the last write wins) so only that disposition is
    # judged. A stale earlier removal — superseded by a later grounded probe
    # *or* by a second ungrounded removal — is dropped, capping the rule at one
    # finding per question instead of one per ungrounded removal in the window.
    last_idx_per_question: dict[str, int] = {}
    for idx, resolution in enumerate(resolutions):
        if isinstance(resolution, QuestionResolution):
            last_idx_per_question[resolution.question] = idx
    findings: list[AuditFinding] = []
    for idx, resolution in enumerate(resolutions):
        if not isinstance(resolution, QuestionResolution):
            continue
        if is_grounded_closure(resolution):
            continue
        if last_idx_per_question.get(resolution.question, idx) != idx:
            continue
        task_id = resolution.task_id
        findings.append(AuditFinding(
            kind="contradicted_claim",
            statement=(
                f"Open question {resolution.question!r} was closed (removed) "
                f"but its only probe (task {task_id}) ended "
                f"{resolution.outcome}, not success — a closure the record "
                f"cannot confirm. Re-open the question or justify the closure "
                f"with evidence the probe actually answered it."
            ),
            cites=[task_id][:MAX_CITES_PER_FINDING] if task_id else [],
            rule="ungrounded_question_closure",
        ))
        if len(findings) >= MAX_FINDINGS_PER_RULE:
            break
    return findings


def check_stale_self_model_entry(
    self_model: SelfModel,
    corpus: list[str] | None,
) -> list[AuditFinding]:
    """Rule 6 — identity / capability / value entry has zero corpus overlap.

    Iterates :data:`_STALE_ENTRY_FIELDS` (``identity_statements``,
    ``capabilities_known``, ``values``) and emits one finding per entry
    whose stopword-stripped content tokens fail to appear — at the
    :data:`_STALE_ENTRY_MIN_TOKEN_OVERLAP` floor — in any single item of
    *corpus* via prefix-match. The single-item check (rather than
    union-across-corpus) prevents weak signals where each token shows
    up in unrelated items from keeping a dead entry alive.

    *corpus* is the rolling textual evidence the LLM has produced
    recently — journal contents, task goals + ``key_findings``, prior
    audit-finding statements, and resolution-finding text. The agent
    constructs it in :func:`maestro.loop.self_audit.audit_self_model` only when
    ``audit_stale_entry_enabled`` is on; passing ``None`` here keeps
    the rule silent so the default-off flag yields zero output.

    Brand-new entries are protected by the novelty realization journal
    event recorded in :meth:`Consciousness.update_self_model` on
    ``surprise_count`` increment — the new entry's tokens ride that
    line into the corpus on the next audit cycle, so a freshly added
    capability is never flagged.

    Field-cardinality floor: a field with fewer than
    :data:`_STALE_ENTRY_MIN_FIELD_CARDINALITY` valid entries is sparse
    and load-bearing by composition; the rule skips it. Per-rule cap is
    :data:`_STALE_ENTRY_PER_RULE_CAP` (1) — the deferred-rule shape
    that yields to the non-deferred rules under the round-robin clip.

    Self-extinguishing semantics: a flagged entry stays silent on
    subsequent cycles because the audit's own outputs feed back into
    the corpus — the ``audit_observation`` journal event recorded by
    :func:`maestro.loop.self_audit.audit_self_model` and the matching
    ``AuditResult`` row both contain the entry's verbatim text, so on
    the next cycle the entry's tokens overlap and it is classified
    *live*. The rule therefore fires **once per stale entry per
    audit-history window**, not "every cycle until the LLM removes
    it." That single nag is by design — the open-question pair is
    enough attention; persistent re-firing would burn prompt budget
    without informing additional verdicts. When the LLM does REMOVE
    the entry, the next iteration over the source field simply
    doesn't see it; when the LLM REFINEs or KEEPs the entry without
    removal, the rule still mutes itself via the feedback path until
    the audit history rolls past the original finding.
    """
    if corpus is None:
        return []

    corpus_token_sets: list[set[str]] = [
        set(tokenize(item))
        for item in corpus
        if isinstance(item, str)
    ]

    findings: list[AuditFinding] = []
    for field_name in _STALE_ENTRY_FIELDS:
        entries = getattr(self_model, field_name, None) or []
        valid_entries = [
            e for e in entries
            if isinstance(e, str) and e.strip()
        ]
        if len(valid_entries) < _STALE_ENTRY_MIN_FIELD_CARDINALITY:
            continue
        for entry in valid_entries:
            entry_tokens = _content_tokens(entry)
            if not entry_tokens:
                continue
            required = min(
                _STALE_ENTRY_MIN_TOKEN_OVERLAP, len(entry_tokens),
            )
            live = False
            for corpus_tokens in corpus_token_sets:
                if not corpus_tokens:
                    continue
                hits = 0
                for et in entry_tokens:
                    for ct in corpus_tokens:
                        if ct.startswith(et):
                            hits += 1
                            break
                    if hits >= required:
                        break
                if hits >= required:
                    live = True
                    break
            if live:
                continue
            findings.append(AuditFinding(
                kind="stale_entry",
                statement=f"{field_name}: {entry[:160]}",
                cites=[field_name],
                rule="stale_entry",
            ))
            if len(findings) >= _STALE_ENTRY_PER_RULE_CAP:
                return findings
    return findings


def run_all_checks(
    self_model: SelfModel,
    tasks: list[TaskRecord],
    success_rate: float,
    resolutions: list[QuestionResolution] | None = None,
    settled_prs: frozenset[int] = frozenset(),
    *,
    stale_entry_corpus: list[str] | None = None,
    cited_source_task_ids: set[str] | None = None,
    branch_promotion_pressure_enabled: bool = True,
    evidence_bound_claims_enabled: bool = False,
    closed_refs: frozenset[int] = frozenset(),
) -> list[AuditFinding]:
    """Run all rules and return a round-robin, cap-clipped finding list.

    Each rule's findings are already capped at
    :data:`MAX_FINDINGS_PER_RULE` (or :data:`_STALLED_PER_RULE_CAP` /
    :data:`_STALE_ENTRY_PER_RULE_CAP` /
    :data:`_KEPT_TASK_NOT_PROMOTED_PER_RULE_CAP` for the cap-1 rules).
    This combiner interleaves them via :func:`_rule_priority` so newer
    "deferred" rules yield to longer-standing legacy rules under the
    :data:`MAX_FINDINGS_PER_CYCLE` clip: with four legacy rules at
    their cap (eight candidates), ``kept_task_not_promoted`` at cap 1
    (nine), and ``stalled_question`` / ``stale_entry`` at cap 1 each
    (eleven), plus — when
    ``evidence_bound_claims_enabled`` is on — ``unbound_capability_claim``
    at :data:`MAX_FINDINGS_PER_RULE` (thirteen), clipping to four admits
    only the rotation's first round — so the four deferred rules
    always yield, and every survivor reaches ``open_questions`` because
    the cap is aligned to ``MAX_AUDIT_OPEN_QUESTIONS``.

    ``kept_task_not_promoted`` is non-deferred at cap 1: it competes
    alphabetically with the legacy rules at round-robin position 0 and
    therefore reliably surfaces whenever the un-promotion floor is
    crossed, by design — the floor itself is the signal. The cost is
    that five non-deferred rules contend for four slots, so a cycle in
    which all five fire starves ``success_claim_vs_rate``, the
    alphabetically last — pinned by
    ``test_run_all_checks_round_robin_starves_last_legacy_rule``.

    *resolutions* is the optional log fed to ``check_stalled_question``;
    pass ``None`` (or omit) when no resolutions are available — the
    rule self-suppresses on an empty list.

    *stale_entry_corpus* is the optional textual evidence fed to
    ``check_stale_self_model_entry``; pass ``None`` (or omit) to keep
    the rule silent. Caller-controlled so the opt-in
    ``audit_stale_entry_enabled`` flag stays a single decision point.

    *cited_source_task_ids* is the optional union of every non-empty
    ``source_task_id`` across all evolution plans, fed to
    ``check_kept_task_not_promoted``. Pass ``None`` (or omit) to keep
    the rule silent — equivalent to ``set()``, with the empty-set
    semantics matching "no plan has cited anything" so the rule can
    fire on a fresh install with three or more kept successful tasks.

    *branch_promotion_pressure_enabled* (default ``True``, mirroring the
    ``[audit] branch_promotion_pressure_enabled`` master switch — proposal
    116, step 6) gates rule 7: when ``False`` the
    ``kept_task_not_promoted`` entry is omitted from the finding dict, so
    the rule contributes nothing to the interleaved output and its
    ``[audit]`` open question is never surfaced.

    *evidence_bound_claims_enabled* (default ``False``, mirroring the
    ``[audit] evidence_bound_claims_enabled`` master switch — proposal 129,
    Phase B) gates ``check_unbound_capability_claim``: only when ``True`` is
    the ``unbound_capability_claim`` entry added to the finding dict. With
    the flag off the ``SelfModel.claim_evidence`` map is empty (nothing
    records it), so every claim would look unbound; omitting the entry keeps
    the combiner output byte-identical to before.

    *closed_refs* (default empty) is the set of host-confirmed closed
    ``#NNN`` numbers fed to rule 23 (``check_closed_reference_claim``); the
    rule's entry is added only when it is non-empty, so the empty default
    keeps the combiner output byte-identical.

    *settled_prs* (default empty) is threaded into
    ``check_repeated_target_attempt`` so a ``#NNN`` the settlement
    reconciler has recorded as settled is skipped before that rule's
    threshold test. The empty default keeps the combiner output
    byte-identical to before.
    """
    rule_findings: dict[str, list[AuditFinding]] = {
        "success_claim_vs_rate": check_success_claim_vs_rate(
            self_model, tasks, success_rate,
        ),
        "stability_claim_vs_failure_streak":
            check_stability_claim_vs_failure_streak(self_model, tasks),
        "repeated_error_without_limitation":
            check_repeated_error_without_limitation(self_model, tasks),
        "repeated_target_attempt":
            check_repeated_target_attempt(self_model, tasks, settled_prs),
        "stalled_question": check_stalled_question(
            self_model, resolutions or [],
        ),
        "stale_entry": check_stale_self_model_entry(
            self_model, stale_entry_corpus,
        ),
    }
    if branch_promotion_pressure_enabled:
        # Rule 7 — gated by the branch-promotion-pressure master switch
        # (proposal 116, step 6). When off, omit the entry so the rule never
        # contributes a finding and never reaches ``open_questions``; the
        # round-robin combiner simply drops it from the rotation.
        rule_findings["kept_task_not_promoted"] = check_kept_task_not_promoted(
            self_model, tasks, cited_source_task_ids or set(),
        )
    if evidence_bound_claims_enabled:
        # Rule — unbound-capability-claim detector, gated by the
        # evidence-bound-claims master switch (proposal 129, Phase B). When
        # off, omit the entry so the rule never contributes a finding and
        # never reaches ``open_questions``; the ``claim_evidence`` map is
        # empty without the flag, so every claim would otherwise look unbound.
        rule_findings["unbound_capability_claim"] = check_unbound_capability_claim(
            self_model,
        )
    if closed_refs:
        # Rule 23 — closed-reference detector; the caller resolves a
        # non-empty set only behind ``[audit] closed_reference_enabled``.
        rule_findings["closed_reference"] = check_closed_reference_claim(
            self_model, closed_refs,
        )

    sorted_names = sorted(rule_findings.keys(), key=_rule_priority)
    max_depth = max(
        (len(findings) for findings in rule_findings.values()),
        default=0,
    )

    interleaved: list[AuditFinding] = []
    for i in range(max_depth):
        for name in sorted_names:
            findings = rule_findings[name]
            if i < len(findings):
                interleaved.append(findings[i])

    return interleaved[:MAX_FINDINGS_PER_CYCLE]


def compute_audit_stall_counts(
    audit_results_newest_last: list[AuditResult],
    current_open_questions: list[str],
    resolutions: list[QuestionResolution] | None = None,
) -> dict[str, int]:
    """Per ``[audit]`` open question, count consecutive recent cycles it fired.

    Walks *audit_results_newest_last* newest→oldest and, for each
    ``[audit]``-prefixed entry in *current_open_questions*, counts the
    consecutive most-recent cycles whose ``findings`` produce a matching
    ``[audit] <kind>: <statement[:160]>`` string. The first cycle without
    a match stops the walk for that entry — the counter is *consecutive*,
    not cumulative. Non-``[audit]`` entries are excluded from the result.

    Matching uses :func:`_normalize_question` so case/whitespace drift
    between the live ``open_questions`` text and the deterministically
    re-derived audit string never breaks the counter. When
    *resolutions* is provided, each open question's lineage is folded
    into the per-cycle match set via :func:`lineage_keys` so a wording
    drift caused by REFINE doesn't reset the counter — a cycle whose
    re-derived audit string matches *any* predecessor wording in the
    lineage still counts as a hit. When *resolutions* is ``None`` or
    empty, :func:`lineage_keys` degrades to single-key matching
    internally, so no special-casing is needed here.
    """
    audit_questions = [
        q for q in current_open_questions
        if isinstance(q, str) and q.startswith("[audit")
    ]
    if not audit_questions or not audit_results_newest_last:
        return {}

    target_keys: dict[str, set[str]] = {
        q: lineage_keys(q, resolutions or [])
        for q in audit_questions
    }
    counts: dict[str, int] = dict.fromkeys(audit_questions, 0)
    active = set(audit_questions)

    for result in reversed(audit_results_newest_last):
        cycle_keys = {
            _normalize_question(
                f"[audit] {f.kind}: {f.statement[:160]}",
            )
            for f in result.findings
        }
        for q in list(active):
            if target_keys[q] & cycle_keys:
                counts[q] += 1
            else:
                active.discard(q)
        if not active:
            break

    return counts


# The ``AuditFinding.rule`` strings of the git-grounded audit checks whose
# narration *over-claims resolution on an unmerged PR* — the polarity the
# Phase 2 escalation serves (its actuator skips merged PRs and its message tells
# the LLM to merge/close an unmerged one). Proposal 088 Phase 2 escalates only a
# reality-grounded finding the LLM has refuted across a threshold of cycles, so
# the set is keyed on these rule names exactly. Pinned to the live rule names by
# ``test_reality_grounded_rules_pin_live_rule_names`` so a rename fails loudly
# rather than silently dropping a rule from escalation.
_REALITY_GROUNDED_RULES = frozenset({
    "unconfirmed_own_landing",
    "undercredited_landing",
})


# ---------------------------------------------------------------------------
# Audit-rule authority classification (proposal 129 Phase C)
# ---------------------------------------------------------------------------

#: The kind of evidence a registered audit rule reads — and therefore how much
#: authority its finding carries against the model's REFUTE affordance. The
#: registry mixes git/ledger facts with derived aggregates and prose heuristics;
#: determinism is not authority, so only the fact-reading class earns a
#: mechanical override of a refutation the evidence keeps contradicting.
#:
#: - ``mechanical_fact`` — a git-ancestry read, a plan-store status transition,
#:   or the message/plan ledger. A refuted finding of this class that keeps
#:   re-firing on the same target is mechanically restored (the eyes win the
#:   dispute; see :func:`measure_refutation_rejections`).
#: - ``derived_measure`` — an aggregate or threshold read over the ledgers (a
#:   rate, a streak, a window count). A single cycle's reading is a legitimate
#:   REFUTE target, so it stays refutable indefinitely.
#: - ``heuristic`` — a keyword or prose-shape match; a false-positive hit is
#:   exactly what REFUTE exists for, so it too stays refutable indefinitely.
RuleAuthority = Literal["mechanical_fact", "derived_measure", "heuristic"]

#: Every registered audit rule's declared authority class, keyed on the
#: ``AuditFinding.rule`` string (not the check function ``__name__`` — the two
#: vocabularies diverge, e.g. ``check_stale_self_model_entry`` emits
#: ``stale_entry``). Pinned to the live registry in both
#: directions by ``test_rule_authority_covers_registry`` — adding, removing, or
#: renaming a registered rule's ``rule=`` constant reds the build until this map
#: is updated. ``green_ready_handoff_prs`` is a registered forced-arm-only rule
#: that constructs no ``AuditFinding``, so it contributes no key.
RULE_AUTHORITY: dict[str, RuleAuthority] = {
    # mechanical_fact — git-ancestry / plan-store transition / message-or-plan ledger.
    "unconfirmed_own_landing": "mechanical_fact",   # is-ancestor read of the own PR / committed SHA
    "undercredited_landing": "mechanical_fact",     # own committed SHA git-proven on origin/main
    "phantom_escalation": "mechanical_fact",        # escalation prose with no matching message-ledger send
    "phantom_partial_prune": "mechanical_fact",     # a landed owned prune stamped partial (plan-store + git)
    "reconcile_near_miss": "mechanical_fact",       # ≤N-behind git-unlanded own PR with no reconcile SHA
    "banked_owned_act_reperformance": "mechanical_fact",  # directive/goal re-performs a banked owned act (ledger membership)
    "orphaned_cure": "mechanical_fact",             # overdue plan with no curing branch (plan-store + git)
    # derived_measure — aggregate / threshold read over the ledgers.
    "success_claim_vs_rate": "derived_measure",           # claimed success vs measured task-success rate
    "stability_claim_vs_failure_streak": "derived_measure",  # stability claim vs trailing failure streak
    "construction_dominance": "derived_measure",          # construction share over the task-shape window
    "owned_act_drought": "derived_measure",               # chosen-terminal-act count over the drought window
    "evolution_impact_review": "derived_measure",         # age of un-reviewed merged evolution PRs
    "ungrounded_question_closure": "derived_measure",     # resolution-ledger closures lacking grounding
    "stalled_question": "derived_measure",                # consecutive-cycle stall count over audit history
    "kept_task_not_promoted": "derived_measure",          # kept branch count never promoted to a plan
    "unbound_capability_claim": "derived_measure",        # capabilities_known entries lacking evidence
    "closed_reference": "mechanical_fact",                # every cited #NNN host-reported closed
    # heuristic — keyword / prose-shape match.
    "repeated_error_without_limitation": "heuristic",  # repeated error fingerprint, no matching limitation
    "repeated_target_attempt": "heuristic",            # same target named across recent task goals
    "stale_entry": "heuristic",                        # self-model entry unchanged past the stale threshold
}


def mechanical_fact_rules() -> frozenset[str]:
    """The ``AuditFinding.rule`` names classified ``mechanical_fact``.

    The single source of the class filter :func:`measure_refutation_rejections`
    reads, so callers never re-derive it inline from :data:`RULE_AUTHORITY`.
    """
    return frozenset(
        rule
        for rule, authority in RULE_AUTHORITY.items()
        if authority == "mechanical_fact"
    )


def measure_reality_refutation(
    audit_results_newest_last: list[AuditResult],
    *,
    rules: frozenset[str] = _REALITY_GROUNDED_RULES,
) -> dict[tuple[str, int], int]:
    """Per reality-grounded ``(rule, PR)``, count consecutive recent cycles it fired.

    Proposal 088 Phase 2's recurrence sensor — the eyes-win-a-dispute counter.
    Mirrors :func:`compute_audit_stall_counts`: walks
    *audit_results_newest_last* newest→oldest and, for each finding whose
    ``rule`` is in *rules*, extracts the target PR via
    :func:`extract_issue_numbers` over its ``statement`` and counts the
    consecutive most-recent cycles each ``(rule, pr)`` key recurred. The first
    cycle in which a key does not fire stops that key's run — the counter is
    *consecutive from newest*, not cumulative, so a finding the LLM has let lapse
    (or that fired only in older non-consecutive cycles) does not accumulate.

    Taking the pre-read list (rather than a ``data_dir``) keeps the function
    pure and unit-testable without ``mem_fs``, exactly like
    :func:`compute_audit_stall_counts`; the caller supplies
    ``Memory.get_recent_audit_results(n)``. Returns only keys with a positive
    consecutive-recent count; a key absent from the result has count 0. Pure
    function, zero SDK cost.
    """
    if not audit_results_newest_last or not rules:
        return {}

    # Per cycle (newest→oldest), the set of (rule, pr) keys that fired for a
    # reality-grounded rule. A set collapses duplicate findings / repeated PR
    # mentions to per-cycle presence, which is what the consecutive walk needs.
    cycle_key_sets: list[set[tuple[str, int]]] = []
    for result in reversed(audit_results_newest_last):
        keys: set[tuple[str, int]] = set()
        for f in result.findings:
            if f.rule not in rules:
                continue
            for pr in extract_issue_numbers(f.statement):
                keys.add((f.rule, pr))
        cycle_key_sets.append(keys)

    all_keys: set[tuple[str, int]] = set()
    for key_set in cycle_key_sets:
        all_keys |= key_set
    if not all_keys:
        return {}

    counts: dict[tuple[str, int], int] = dict.fromkeys(all_keys, 0)
    active = set(all_keys)
    for key_set in cycle_key_sets:  # newest→oldest
        for key in list(active):
            if key in key_set:
                counts[key] += 1
            else:
                active.discard(key)
        if not active:
            break

    return {key: count for key, count in counts.items() if count > 0}


@dataclass(frozen=True)
class RefutationRejection:
    """One refuted ``mechanical_fact`` finding the evidence keeps contradicting.

    Proposal 129 Phase C. Emitted by :func:`measure_refutation_rejections` when a
    git/ledger-grounded finding the model has REFUTEd keeps re-firing on the same
    target across the threshold of consecutive cycles — the signal that the
    correction is owed to the *self-model*, not a message queue.

    *question* — the live ``[audit refuted] …`` open-question entry to overturn.
    *rule* — the ``AuditFinding.rule`` that keeps re-firing.
    *target* — the git/ledger target the finding and the refutation share
    (``#NNNN``, a committed SHA, or a cited task/plan id).
    *cycles* — consecutive most-recent audit cycles the ``(rule, target)`` fired.
    *restored_question* — the canonical ``[audit] <kind>: <statement[:160]>``
    string re-derived from the newest matching finding (never reconstructed from
    the refuted text, whose embedded ``kind`` alone is not unique).
    """

    question: str
    rule: str
    target: str
    cycles: int
    restored_question: str


def _finding_targets(finding: AuditFinding) -> set[str]:
    """The git/ledger targets a finding's statement and cites name.

    Three families, because the ``mechanical_fact`` rules do not share one
    identifier shape: ``#NNNN`` issue numbers and ``committed <sha>`` SHAs mined
    from the statement, plus each cited id (the task ids most rules carry and
    the plan id :func:`check_orphaned_cure` puts in ``cites``). Returns the union
    as strings so one ``(rule, target)`` key space spans all three.
    """
    targets = {f"#{pr}" for pr in extract_issue_numbers(finding.statement)}
    targets |= extract_self_committed_shas(finding.statement)
    targets |= {cite for cite in finding.cites if cite}
    return targets


def _entry_names_target(entry: str, target: str) -> bool:
    """Whether a refuted open-question *entry* still names *target*.

    A ``#NNNN`` target is matched through :func:`extract_issue_numbers` (so
    punctuation around the number does not break it); a SHA or a cited id is a
    bare substring, which is how the statement-derived question carries it.
    """
    if target.startswith("#") and target[1:].isdigit():
        return int(target[1:]) in extract_issue_numbers(entry)
    return target in entry


def measure_refutation_rejections(
    audit_results_newest_last: list[AuditResult],
    open_questions: list[str],
    *,
    threshold: int,
) -> list[RefutationRejection]:
    """Refuted ``mechanical_fact`` findings the evidence keeps contradicting.

    Proposal 129 Phase C's refutation-accountability sensor — the generalization
    of :func:`measure_reality_refutation` from a single ``#NNNN`` target to the
    three-family target set of :func:`_finding_targets`, restricted to the
    ``mechanical_fact`` class via :func:`mechanical_fact_rules` (determinism is
    authority only for a git/ledger fact; derived measures and heuristics stay
    refutable). Mirrors the sibling's consecutive-from-newest walk: counts the
    consecutive most-recent cycles each ``(rule, target)`` recurred, then — for
    every ``[audit refuted]``-prefixed entry in *open_questions* that still
    names such a target whose count is ``>= threshold`` — emits one
    :class:`RefutationRejection` carrying the canonical question re-derived from
    the newest matching finding.

    Takes pre-read lists (no ``data_dir``) like its two siblings, so it is
    unit-testable without ``mem_fs``; the caller supplies
    ``Memory.get_recent_audit_results(n)`` and the live ``open_questions``.
    Deterministic output order (sorted by ``(rule, target)`` per entry). Pure
    function, zero SDK cost.
    """
    mechanical = mechanical_fact_rules()
    if not audit_results_newest_last or not mechanical or threshold < 1:
        return []
    refuted = [
        q for q in open_questions
        if isinstance(q, str) and q.startswith("[audit refuted")
    ]
    if not refuted:
        return []

    # Per cycle (newest→oldest), the (rule, target) keys that fired for a
    # mechanical-fact rule, plus the newest finding seen for each key so the
    # canonical restore text is re-derived from the finding, never the refuted
    # text. A set collapses per-cycle duplicates to presence.
    cycle_key_sets: list[set[tuple[str, str]]] = []
    newest_finding: dict[tuple[str, str], AuditFinding] = {}
    for result in reversed(audit_results_newest_last):  # newest→oldest
        keys: set[tuple[str, str]] = set()
        for finding in result.findings:
            if finding.rule not in mechanical:
                continue
            for target in _finding_targets(finding):
                key = (finding.rule, target)
                keys.add(key)
                newest_finding.setdefault(key, finding)
        cycle_key_sets.append(keys)

    all_keys: set[tuple[str, str]] = set()
    for key_set in cycle_key_sets:
        all_keys |= key_set
    if not all_keys:
        return []

    counts: dict[tuple[str, str], int] = dict.fromkeys(all_keys, 0)
    active = set(all_keys)
    for key_set in cycle_key_sets:  # newest→oldest
        for key in list(active):
            if key in key_set:
                counts[key] += 1
            else:
                active.discard(key)
        if not active:
            break

    rejections: list[RefutationRejection] = []
    for entry in refuted:
        for rule, target in sorted(counts):
            if counts[(rule, target)] < threshold:
                continue
            if not _entry_names_target(entry, target):
                continue
            finding = newest_finding[(rule, target)]
            restored = f"[audit] {finding.kind}: {finding.statement[:160]}"
            rejections.append(RefutationRejection(
                question=entry,
                rule=rule,
                target=target,
                cycles=counts[(rule, target)],
                restored_question=restored,
            ))
    return rejections


def format_reality_refutation_summary(
    counts: dict[tuple[str, int], int],
    open_questions: list[str],
    threshold: int,
    rejections: int = 0,
) -> str:
    """Build a ``REALITY-REFUTATION`` line for refuted, recurring git findings.

    Proposal 088 Phase 2's contemplation mirror. Returns a single
    ``REALITY-REFUTATION`` line naming each ``(rule, PR)`` whose
    consecutive-recent recurrence count (from :func:`measure_reality_refutation`)
    is ``>= threshold`` **and** for which an ``[audit refuted]``-prefixed entry
    in *open_questions* names that PR (matched via :func:`extract_issue_numbers`)
    — i.e. the LLM has actively disputed a git-grounded finding that git still
    contradicts. Returns ``""`` when no key satisfies both conjuncts, so the
    caller injects the line only when non-empty.

    Pure function, zero SDK cost; the display sibling of the Phase 2 actuator and
    enableable independently of it.

    *rejections* (proposal 129 Phase C, additive, default ``0``) appends a
    "…N refutation(s) mechanically overturned" clause reporting refutations the
    accountability actuator has restored across the recent gate-activation
    window — the caller passes a cumulative ``measure_gate_triggers`` count, not
    a single-cycle tally, so the clause deliberately carries no "this cycle"
    claim. It is independent of the
    reality-refutation conjunct above — a restored entry is no longer
    ``[audit refuted]``, so that conjunct correctly goes silent for it — so the
    early-return is structured to still emit a line on a rejection-only cycle.
    At the ``0`` default the output is byte-identical to the Phase 2 line.
    """
    # Reality-refutation conjunct (proposal 088): git findings the model refuted
    # that keep recurring. May be empty (e.g. a rejection-only cycle).
    flagged: list[tuple[str, int, int]] = []
    if counts and threshold >= 1:
        # PRs the LLM has explicitly refuted — an ``[audit refuted]`` entry names them.
        refuted_prs: set[int] = set()
        for q in open_questions:
            if isinstance(q, str) and q.startswith("[audit refuted"):
                refuted_prs |= extract_issue_numbers(q)
        if refuted_prs:
            flagged = sorted(
                (rule, pr, count)
                for (rule, pr), count in counts.items()
                if count >= threshold and pr in refuted_prs
            )
    if not flagged and rejections < 1:
        return ""
    parts: list[str] = []
    if flagged:
        items = "; ".join(
            f"{rule} #{pr} (refuted, recurred {count} consecutive cycles)"
            for rule, pr, count in flagged
        )
        parts.append(
            f"git-grounded audit findings I have refuted while git still "
            f"contradicts the claim ({threshold}+ consecutive cycles): "
            f"{items}. Narration cannot overrule git indefinitely — merge/close "
            f"the PR or integrate the finding."
        )
    if rejections >= 1:
        noun = "refutation" if rejections == 1 else "refutations"
        parts.append(
            f"{rejections} {noun} mechanically overturned recently — a "
            f"mechanical_fact finding I refuted kept re-firing on the same "
            f"target, so the canonical audit question was restored."
        )
    return "REALITY-REFUTATION: " + " ".join(parts)


# ---------------------------------------------------------------------------
# Undefined-coinage sensor (proposal 101)
# ---------------------------------------------------------------------------

# How many recent journal entries the undefined-coinage sensor scans. A window,
# not the whole journal, so the reading tracks *active* minting: a coinage the
# agent has stopped using scrolls out and drops off, keeping the sensor
# self-extinguishing on disuse as well as on definition.
_UNDEFINED_COINAGE_JOURNAL_WINDOW = 200

# Minimum occurrences within the window before a coinage is worth surfacing.
# Below this floor a hyphenated compound is noise — a one-off, a typo, or an
# ordinary compound the stoplist missed — not a term being actively minted.
_UNDEFINED_COINAGE_MIN_FREQUENCY = 5


def measure_undefined_coinages(
    journal_texts: list[str],
    *,
    lexicon_terms: frozenset[str],
    min_frequency: int = _UNDEFINED_COINAGE_MIN_FREQUENCY,
) -> dict[str, int]:
    """Count the actively-minted coinages the lexicon does not yet define.

    Proposal 101's zero-SDK vocabulary sensor. Runs :func:`extract_coinages`
    over each pre-read journal ``content`` string in *journal_texts* (the caller
    supplies the tail, so this function stays pure and I/O-free, mirroring
    :func:`measure_reality_refutation` / :func:`measure_noop_rate`), sums each
    coinage's occurrences across the window, subtracts every spelling the lexicon
    already defines (*lexicon_terms*, compared case-insensitively so an
    uppercase-only variant still self-extinguishes), and returns
    ``{term: count}`` for the coinages occurring at least *min_frequency* times.

    Self-extinguishing by construction: a term drops out of the result the moment
    it is added to ``docs/lexicon.md`` (it enters *lexicon_terms*) or falls out of
    use (its window count decays below *min_frequency*). Returns ``{}`` when no
    coinage clears the floor. Pure function, zero SDK cost.
    """
    if not journal_texts or min_frequency < 1:
        return {}
    defined = {term.lower() for term in lexicon_terms}
    counts: Counter[str] = Counter()
    for text in journal_texts:
        counts.update(extract_coinages(text))
    return {
        term: count
        for term, count in counts.items()
        if count >= min_frequency and term not in defined
    }


def format_undefined_coinage_summary(
    counts: dict[str, int],
    max_terms: int = 5,
) -> str:
    """Build an ``UNDEFINED-COINAGE`` line from :func:`measure_undefined_coinages`.

    Names up to *max_terms* undefined coinages, highest-frequency first (ties
    broken alphabetically for a stable line), so contemplation can see the
    vocabulary it is actively minting but has never defined. Returns ``""`` when
    *counts* is empty or *max_terms* < 1, so the caller injects the line only when
    non-empty. Pure function, zero SDK cost.
    """
    if not counts or max_terms < 1:
        return ""
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    items = ", ".join(f"{term} ({count})" for term, count in ranked[:max_terms])
    return (
        f"UNDEFINED-COINAGE: coinages I am actively minting but have not defined "
        f"in docs/lexicon.md: {items}. Define the load-bearing ones or let them "
        f"lapse."
    )


def check_undefined_coinage_directive(counts: dict[str, int]) -> str | None:
    """Build a directive steering toward defining the top undefined coinage.

    Directive-bridge complement to :func:`format_undefined_coinage_summary`'s
    contemplation line (proposal 101): where the display sensor merely *names*
    the coinages the recent journal is actively minting that ``docs/lexicon.md``
    does not yet define, this converts the top one into a concrete
    ``next_task_directive`` so the next ``generate_task`` is steered toward
    defining that term (or letting it lapse) without waiting for a contemplation
    round-trip — the actuator every other mechanical sensor already pairs with.

    Takes the ``{term: count}`` dict :func:`measure_undefined_coinages` produces
    (already floor-filtered and lexicon-subtracted) and picks the top term —
    highest count, alphabetical tie-break — the same ordering
    :func:`format_undefined_coinage_summary` uses, then truncates to
    :data:`_DIRECTIVE_MAX_CHARS`. Returns ``None`` when *counts* is empty
    (nothing to steer toward). Self-extinguishing by construction: once the named
    term is defined in ``docs/lexicon.md`` or falls out of journal use it drops
    out of *counts* and the bridge stops firing. Pure function, zero SDK cost.
    """
    if not counts:
        return None
    term, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    directive = (
        f"Define '{term}' in docs/lexicon.md or retire it — it recurs "
        f"{count} times in my recent journal, undefined."
    )
    return truncate_directive(directive)


# ---------------------------------------------------------------------------
# Audit → directive bridge
# ---------------------------------------------------------------------------

# Hard cap matching ``Consciousness.MAX_DIRECTIVE_CHARS``. Duplicated
# here to avoid importing ``consciousness`` (audit_checks is already
# imported *by* consciousness indirectly via agent.py — adding a back-
# import would risk a circular dependency chain).
_DIRECTIVE_MAX_CHARS = 200


def truncate_directive(text: str) -> str:
    """Bound *text* to :data:`_DIRECTIVE_MAX_CHARS`, ellipsising the tail.

    The single home for *whole-directive* truncation: every mechanical bridge
    that bounds an already-composed ``next_task_directive`` — here and in
    ``task_gates`` — routes through this helper, so raising the cap propagates
    everywhere instead of leaving a bridge silently clipping at the old
    length. ``tests/test_task_gates.py`` pins that for ``task_gates`` by AST.

    Not every directive author lands here, and the exclusion is deliberate:
    :func:`finding_to_directive` and the partial-task bridge clip only their
    *variable* tail against a ``_DIRECTIVE_MAX_CHARS - len(prefix)`` budget —
    a different shape that keeps the fixed prefix intact rather than letting
    the ellipsis eat it.

    Reads the module global at call time (not a default-arg snapshot) so the
    cap stays a single point of change. ``save_self_model`` re-truncates on
    persist, so callers gain tidy log/detail text, not persistence safety.
    Pure function — no I/O, zero cost.
    """
    if len(text) <= _DIRECTIVE_MAX_CHARS:
        return text
    return text[:_DIRECTIVE_MAX_CHARS - 1] + "…"


# ---------------------------------------------------------------------------
# Instrument-consumption directive bridge
# ---------------------------------------------------------------------------

# Minimum number of unconsumed instrument-shipping tasks before the
# directive bridge fires.  Below this count the pattern is noise —
# instruments may simply be too recent for a follow-up.
INSTRUMENT_CONSUMPTION_THRESHOLD = 3

# Compiled regex matching instrument-shipping task goals.  Word-boundary
# anchored so "measurement" matches but "immeasurable" does not.
_INSTRUMENT_KEYWORDS_RE = re.compile(
    r"\b(?:instrument|gate|metric|measurement|experiment|hypothesis|apparatus)\b",
    re.IGNORECASE,
)


def classify_instrument_task(record: dict[str, object]) -> bool:
    """Return ``True`` when *record*'s goal matches instrument-shipping keywords.

    Word-boundary-anchored regex on the ``goal`` field.  Returns ``False``
    for missing or non-string goals. Pure function — zero SDK cost.
    """
    goal = record.get("goal")
    if not isinstance(goal, str):
        return False
    return bool(_INSTRUMENT_KEYWORDS_RE.search(goal))


def find_unconsumed_instruments(
    recent_tasks: list[dict[str, object]],
    lookback: int = 20,
) -> list[dict[str, object]]:
    """Return instrument-shipping tasks with no downstream reference.

    Scans the last *lookback* entries of *recent_tasks* (which should be
    ordered oldest-first / append-order from ``task_history.jsonl``).
    An instrument task counts as **consumed** when any later task (by
    position in the list) references its ``task_id`` in ``goal``,
    ``result_summary``, or ``linked_question``. Returns the unconsumed
    subset, oldest-first.

    Pure function: no I/O, no SDK cost.
    """
    window = recent_tasks[-lookback:] if lookback > 0 else []
    instruments: list[dict[str, object]] = []
    for task in window:
        if classify_instrument_task(task):
            tid = task.get("task_id")
            if isinstance(tid, str) and tid:
                instruments.append(task)

    if not instruments:
        return []

    unconsumed: list[dict[str, object]] = []
    for inst in instruments:
        tid = str(inst["task_id"])
        idx = window.index(inst)
        consumed = False
        for later in window[idx + 1:]:
            for fld in ("goal", "result_summary", "linked_question"):
                val = later.get(fld)
                if isinstance(val, str) and tid in val:
                    consumed = True
                    break
            if consumed:
                break
        if not consumed:
            unconsumed.append(inst)
    return unconsumed


# ---------------------------------------------------------------------------
# Integration-backlog prompt hint — zero-SDK-cost surface for unmerged work
# ---------------------------------------------------------------------------

# Default threshold: the backlog hint is injected into the task-generation
# prompt only when the count of unintegrated successful branches meets or
# exceeds this floor.  Shares the *threshold* (3) with
# :data:`_KEPT_TASK_NOT_PROMOTED_THRESHOLD`, but intentionally uses a
# narrower outcome filter: this routing hint counts success-only
# branches, whereas the audit rule (diagnosis) counts
# ``("success", "partial")``. Routing targets clean successes for
# promotion; the audit casts the wider net for backlog pressure.
_INTEGRATION_BACKLOG_THRESHOLD = 3


#: Sort key for a feedback entry whose ``submitted_at`` is empty or
#: unparseable: after every dated entry, so damage never jumps the queue.
_FEEDBACK_UNDATED = datetime.max.replace(tzinfo=UTC)


def select_curation_target(
    plans: list[EvolutionPlan],
    *,
    now: datetime | None = None,
    min_age_hours: float = 0.0,
    prioritize_feedback: bool = False,
    loop_curate_skip_serviced_enabled: bool = False,
) -> EvolutionPlan | None:
    """Pick the next ``pending_review`` plan for curation, round-robin.

    Deterministic selector for the ``curate_pending_plans`` phase. Filters
    to ``status == "pending_review"`` plans, prefers those never refined
    (``refined_at is None``) over already-refined ones, then round-robins
    within each group via ``last_curated_at``: never-curated plans
    (``None`` → sort key ``""``) rank ahead of already-curated ones, and
    among equally-curated plans the oldest ``created_at`` wins with a stable
    ``plan_id`` tie-break. So a plan a curate pass keeps (or deterministically
    skips) has its cursor stamped by ``Memory.touch_plan_curated`` and yields
    its slot to the rest of the backlog (#2885); the same stamp records its
    ``curated_hash``, so it does not return at all until its content or
    feedback changes (proposal 155 step 6, below). Returns ``None`` when no pending plan exists.

    When *now* is supplied and *min_age_hours* is positive, a plan must have
    *aged* past the grace period before it becomes eligible: any plan whose
    ``now - created_at`` is below ``min_age_hours`` is dropped before the sort,
    so a freshly-minted plan is not rewritten before the operator has had a
    chance to read the original. A plan with an empty or unparseable
    ``created_at`` is treated as *not yet eligible* and excluded (conservative,
    matching how ``_parse_iso_utc`` callers handle damaged timestamps). With
    the defaults (``now=None``, ``min_age_hours=0.0``) the gate is inert and
    behaviour is byte-for-byte the pre-grace selector.

    Zero SDK cost; a pure function over the plan list. Never returns an
    ``approved`` / ``merged`` / ``obsolete`` plan — only ``pending_review``
    rows are eligible, mirroring ``Memory.refine_plan``'s write boundary.

    With *prioritize_feedback* (``[loop] plan_feedback_enabled``, proposal
    145), a pending plan carrying unaddressed reviewer feedback bypasses the
    grace filter and sorts ahead of every other plan, ordered by its oldest
    unaddressed ``submitted_at`` then ``plan_id``; every other plan keeps the
    order above. Off, the selector is unchanged.

    A plan failing :func:`curate_input_fits` is dropped before both lanes
    (#4476): it can never be curated, so it must not monopolise the head.

    With *loop_curate_skip_serviced_enabled* (``[loop]
    curate_skip_serviced_enabled``, default-off), every plan already serviced
    once — ``refined_at`` OR ``last_curated_at`` not ``None`` — is excluded
    from the normal (non-feedback) eligible set before the sort, so only
    never-serviced plans remain curation-eligible and the function returns
    ``None`` once every pending plan has been serviced. The ``curated_hash``
    skip below already drops an unchanged serviced plan; this flag
    additionally keeps a serviced plan out after its content or feedback
    changes. The reviewer-feedback priority lane is
    unchanged: a refined plan carrying unaddressed feedback is still returned.

    Proposal 155 step 6: the normal lane also drops every plan whose
    ``curated_hash`` equals its live :func:`compute_curation_fingerprint`,
    so an unchanged plan is never re-graded; a content edit or new feedback
    makes it eligible once. The feedback lane is exempt.
    """
    pending = [
        p for p in plans
        if p.status == "pending_review" and curate_input_fits(p)
    ]
    if prioritize_feedback:
        with_feedback = [p for p in pending if has_unaddressed_feedback(p)]
        if with_feedback:
            return min(
                with_feedback,
                key=lambda p: (
                    min(
                        _parse_iso_utc(f.submitted_at) or _FEEDBACK_UNDATED
                        for f in unaddressed_feedback(p)
                    ),
                    p.plan_id,
                ),
            )
    pending = [
        p for p in pending
        if p.curated_hash is None
        or p.curated_hash != compute_curation_fingerprint(p)
    ]
    if loop_curate_skip_serviced_enabled:
        pending = [
            p
            for p in pending
            if p.refined_at is None and p.last_curated_at is None
        ]
    if min_age_hours > 0 and now is not None:
        cutoff = now - timedelta(hours=min_age_hours)
        pending = [
            p
            for p in pending
            if (created := _parse_iso_utc(p.created_at)) is not None
            and created <= cutoff
        ]
    if not pending:
        return None
    pending.sort(
        key=lambda p: (
            p.refined_at is not None,
            p.last_curated_at or "",
            p.created_at,
            p.plan_id,
        )
    )
    return pending[0]


def warn_over_budget_curation(plans: list[EvolutionPlan]) -> list[str]:
    """Log one WARNING naming pending plans over the curation input budget.

    Those plans are dropped by :func:`select_curation_target` (#4476) and so
    are left to the human reviewer; the WARNING makes that visible. Returns
    the sorted excluded ``plan_id``s.
    """
    over_budget = sorted(
        p.plan_id for p in plans
        if p.status == "pending_review" and not curate_input_fits(p)
    )
    if over_budget:
        logger.warning(
            "Curation skipped %d pending plan(s) over the input budget, "
            "left for human review: %s",
            len(over_budget), ", ".join(over_budget),
        )
    return over_budget


def count_unintegrated_tasks(
    tasks: list[dict[str, object]],
    plans: list[dict[str, object]],
) -> tuple[int, list[str]]:
    """Count successful task branches not yet cited as ``source_task_id``.

    Filters *tasks* to those with ``outcome == "success"`` and a non-empty
    ``branch`` field, then subtracts any whose ``task_id`` appears as
    ``source_task_id`` in any entry of *plans*. Branches whose HEAD is
    already an ancestor of ``origin/main`` are also excluded — they are
    already merged and promoting them would be a no-op. Returns
    ``(count, task_id_list)`` where *task_id_list* is sorted ascending for
    determinism.

    The success-only filter is intentional and narrower than
    :func:`check_kept_task_not_promoted`'s ``("success", "partial")``
    filter: this routing hint nudges promotion of clean successes, while
    the audit rule casts a wider net for backlog-pressure diagnosis.

    Uses :func:`is_branch_merged_into_main` (subprocess I/O); zero SDK cost.
    """
    cited: set[str] = set()
    for p in plans:
        stid = p.get("source_task_id")
        if isinstance(stid, str) and stid:
            cited.add(stid)

    uncited: list[str] = sorted({
        str(t["task_id"])
        for t in tasks
        if t.get("outcome") == "success"
        and isinstance(t.get("branch"), str)
        and t.get("branch")
        and isinstance(t.get("task_id"), str)
        and t.get("task_id")
        and str(t["task_id"]) not in cited
        and not is_branch_merged_into_main(str(t.get("branch", "")))
    })
    return len(uncited), uncited


def count_consecutive_branchless_tasks(records: list[dict]) -> int:
    """Count trailing tasks that produced no branch.

    Walks *records* in reverse (most-recent last), counting entries whose
    ``branch`` field is ``None``, an empty string, or missing, and stops at
    the first record carrying a truthy ``branch`` value. Returns the streak
    length — ``0`` when the most recent task has a branch (or *records* is
    empty).

    A branchless task is the proxy for analysis-only work: tasks that target
    specific code files create a ``maestro/task/{id}`` branch, while
    analysis-only tasks leave the field unset. The streak feeds the
    branchless-streak gate in :meth:`Agent.generate_task`. Pure function —
    zero SDK cost.
    """
    streak = 0
    for record in reversed(records):
        if record.get("branch"):
            break
        streak += 1
    return streak


def format_integration_backlog(
    count: int,
    threshold: int = _INTEGRATION_BACKLOG_THRESHOLD,
) -> str:
    """Build a prompt section surfacing unmerged successful branches.

    Returns an empty string when *count* is below *threshold* so the
    prompt placeholder degrades cleanly. When the threshold is met,
    returns a ``## Integration Opportunity`` section with the numeric
    count and a one-sentence instruction to consider branch promotion.

    Pure function: no I/O, no SDK cost.
    """
    if count < threshold:
        return ""
    return (
        f"## Integration Opportunity\n"
        f"Among recent tasks, at least {count} successful task branches "
        f"have no follow-up evolution plan citing them as source_task_id. "
        f"Consider promoting one of these branches via a task_followup plan "
        f"instead of starting new exploratory work."
    )


def check_promotable_backlog_directive(
    recent_tasks: list[dict[str, object]],
    threshold: int,
) -> str | None:
    """Build a directive steering toward integration of the oldest branch.

    Directive-bridge complement to :func:`format_integration_backlog`'s
    advisory prompt hint: instead of merely surfacing the backlog count as
    context (which has not measurably reduced the backlog), this returns a
    concrete ``next_task_directive`` string naming the oldest uncited
    branch so the next task is steered toward promoting it.

    Reuses :func:`count_unintegrated_tasks`'s branch-identification logic
    (success-only, non-empty ``branch``, not already merged into main),
    passing no plans so every recent successful branch is a candidate.
    Returns the directive when the count is at or above *threshold*, else
    ``None``. Self-extinguishing: once branches are promoted or merged the
    count drops below *threshold* and the bridge stops firing.

    A non-positive *threshold* always returns ``None`` (disabled).

    Uses :func:`is_branch_merged_into_main` (subprocess I/O); zero SDK cost.
    """
    if threshold <= 0:
        return None
    count, ids = count_unintegrated_tasks(recent_tasks, [])
    if count < threshold:
        return None
    # ``recent_tasks`` is oldest-first (``Memory.get_recent_tasks`` contract),
    # so the first row whose id is in the uncited set is the chronologically
    # oldest uncited branch — selecting by *input position*, not by the
    # lexicographic ``sorted(...)`` order of ``ids`` (which, over random-hex
    # ``uuid.uuid4().hex`` task ids, bears no relation to creation time).
    # Mirrors ``format_promotable_summary``'s input-position recency read and
    # reuses ``count_unintegrated_tasks``'s success-only/uncited/unmerged
    # filter via ``set(ids)``. The ``count >= threshold >= 1`` guard above
    # guarantees the set is non-empty and every id came from a ``recent_tasks``
    # row, so the walk always finds a match.
    uncited = set(ids)
    oldest = ""
    branch = ""
    for t in recent_tasks:
        tid = str(t.get("task_id", ""))
        if tid in uncited:
            oldest = tid
            branch = str(t.get("branch", ""))
            break
    directive = (
        f"Promote or integrate branch {branch} (task {oldest}); "
        f"{count} successful branches await integration."
    )
    return truncate_directive(directive)


# ---------------------------------------------------------------------------
# Task-mode balance hint — maintenance-vs-generative classifier + formatter
# ---------------------------------------------------------------------------

# Keywords indicating a task is maintenance (integration, remediation, upkeep
# of existing work). Matched case-insensitively against the combined
# ``goal`` + ``result_summary`` text. When both maintenance and generative
# keywords match, maintenance takes precedence (conservative — a task that
# "promotes a new measurement" is still maintenance).
_MAINTENANCE_KEYWORDS: frozenset[str] = frozenset({
    "promote",
    "rebase",
    "remediate",
    "remediation",
    "merge conflict",
    "conflict resolution",
    "tend",
    "pr branch",
    "branch promotion",
    "fix ci",
    "fix test",
    "fix lint",
    "fix mypy",
    "fix ruff",
    "quality gate",
})

# Keywords indicating a task creates new capability, measurement, or
# mechanism. Matched case-insensitively against the same text fields.
_GENERATIVE_KEYWORDS: frozenset[str] = frozenset({
    "add",
    "build",
    "create",
    "wire",
    "implement",
    "surface",
    "inject",
    "measure",
    "classify",
    "detect",
    "introduce",
    "experiment",
})

# Minimum number of recent tasks before the mode summary is injected.
# Below this the sample is too small to inform distribution choices.
_TASK_MODE_MIN_TASKS = 3


def classify_task_mode(task: dict[str, object]) -> str:
    """Classify a task dict as ``'maintenance'`` or ``'generative'``.

    Inspects ``goal`` and ``result_summary`` fields for keyword matches
    against :data:`_MAINTENANCE_KEYWORDS` and :data:`_GENERATIVE_KEYWORDS`.
    When both keyword sets match, maintenance takes precedence
    (conservative — a task that "promotes a new measurement" is still
    counted as maintenance). Falls back to ``'generative'`` when neither
    set matches.

    Pure function: no I/O, no SDK cost.
    """
    parts: list[str] = []
    for key in ("goal", "result_summary"):
        val = task.get(key)
        if isinstance(val, str):
            parts.append(val)
    combined = " ".join(parts).lower()

    # Word-boundary matching avoids false positives where a short keyword
    # appears as a substring of a common English word — e.g. ``tend`` inside
    # ``extend`` / ``intend`` / ``attend`` or ``add`` inside ``address``.
    # Mirrors the ``\b``-anchored approach used by ``_SHAPE_PATTERNS``.
    has_maintenance = any(
        re.search(r"\b" + re.escape(kw) + r"\b", combined)
        for kw in _MAINTENANCE_KEYWORDS
    )
    has_generative = any(
        re.search(r"\b" + re.escape(kw) + r"\b", combined)
        for kw in _GENERATIVE_KEYWORDS
    )

    if has_maintenance:
        return "maintenance"
    if has_generative:
        return "generative"
    # Default to generative when no keywords match — new/unclassifiable
    # tasks are more likely creative exploration than upkeep.
    return "generative"


def format_task_mode_summary(tasks: list[dict[str, object]]) -> str:
    """Build a one-line maintenance-vs-generative ratio summary.

    Returns an empty string when fewer than :data:`_TASK_MODE_MIN_TASKS`
    tasks are provided, so the prompt placeholder degrades cleanly.
    When enough tasks are present, returns a compact one-liner like::

        TASK MODE BALANCE: 7 maintenance / 3 generative in last 10 tasks.

    Pure function: no I/O, no SDK cost.
    """
    if len(tasks) < _TASK_MODE_MIN_TASKS:
        return ""
    maintenance = sum(1 for t in tasks if classify_task_mode(t) == "maintenance")
    generative = len(tasks) - maintenance
    total = len(tasks)
    return (
        f"TASK MODE BALANCE: {maintenance} maintenance / "
        f"{generative} generative in last {total} tasks."
    )


# No-op task classification — deterministic substring markers
# ---------------------------------------------------------------------------

# Substrings that indicate a task confirmed existing state without producing
# meaningful changes. Case-insensitive matching via ``str.lower()``.
# Sourced from observed task outputs (#1932 x6 pattern and similar).
_NO_OP_MARKERS: tuple[str, ...] = (
    "no changes needed",
    "no remediation",
    "already passing",
    "already fully",
    "was a no-op",
    "required no code changes",
)


def is_no_op_task(result_summary: str | None) -> bool:
    """Return ``True`` when *result_summary* matches any no-op marker.

    Case-insensitive substring match against :data:`_NO_OP_MARKERS`.
    Returns ``False`` for ``None`` or empty input. Pure function — zero
    SDK cost.
    """
    if not result_summary:
        return False
    lower = result_summary.lower()
    return any(marker in lower for marker in _NO_OP_MARKERS)


# Substrings used by :func:`classify_noop_task` to detect no-op tasks from
# a task dict's ``result_summary``.  Case-insensitive substring matching.
# Deliberately a wider vocabulary than :data:`_NO_OP_MARKERS` —
# ``classify_noop_task`` is the dict-level API; ``is_no_op_task`` is the
# string-level primitive that powers ``compute_no_op_rate``.
_NOOP_TASK_PATTERNS: tuple[str, ...] = (
    "no remediation",
    "already passing",
    # Keyword-bearing "already resolved" lives in ``narration_cues`` so the
    # record-time scrub protects it (#2970).
    *_NOOP_ADMISSION_PHRASES,
    "no-op",
    "no code changes",
    "confirmed existing state",
    "already green",
    # The merge-truth-first stand-down marker: ``run`` returns a record whose
    # summary is exactly this when it declines to service an already-settled
    # PR (no planning, no servicing, no code change), so it is a genuine no-op
    # — matching the criterion that a stand-down is classified as a no-op, not
    # a success (#2662).
    "[stood-down]",
)


def classify_noop_task(task: dict[str, object]) -> bool:
    """Return ``True`` when *task*'s ``result_summary`` matches a known no-op pattern.

    Extracts the ``result_summary`` field from *task* and performs
    case-insensitive substring matching against :data:`_NOOP_TASK_PATTERNS`.
    Returns ``False`` for missing, non-string, or empty ``result_summary``.

    Pure function — zero SDK cost.
    """
    summary = task.get("result_summary")
    if not isinstance(summary, str) or not summary.strip():
        return False
    lower = summary.lower()
    return any(pattern in lower for pattern in _NOOP_TASK_PATTERNS)


# Outcome values considered "successful" for :func:`measure_noop_rate` —
# the canonical ``"success"`` value from :data:`~maestro.types.TaskOutcome`.
# (:func:`check_kept_task_not_promoted` separately counts
# ``("success", "partial")`` for backlog pressure.)
_SUCCESS_OUTCOMES: frozenset[str] = frozenset({"success"})

# Default source label when a task dict has no ``source`` field.
_DEFAULT_NOOP_SOURCE = "queue"


def measure_noop_rate(
    tasks: list[dict[str, object]],
) -> dict[str, object]:
    """Measure no-op rate across successful tasks, broken down by source.

    Filters *tasks* to those with ``outcome`` in :data:`_SUCCESS_OUTCOMES`,
    classifies each via :func:`classify_noop_task`, and returns a summary
    dict::

        {
            "total_success": int,
            "noop_count": int,
            "noop_rate": float,        # 0.0 when total_success == 0
            "by_source": {
                <source>: {
                    "total": int,
                    "noop": int,
                    "rate": float,      # 0.0 when total == 0
                },
                ...
            },
        }

    *source* is derived from each task's ``source`` field.  When the
    field is missing, not a string, or empty, it defaults to
    :data:`_DEFAULT_NOOP_SOURCE` (``"queue"``).

    Pure function — zero SDK cost.
    """
    total_success = 0
    noop_count = 0
    by_source: dict[str, dict[str, int]] = {}

    for task in tasks:
        outcome = task.get("outcome")
        if not isinstance(outcome, str) or outcome not in _SUCCESS_OUTCOMES:
            continue
        total_success += 1

        raw_source = task.get("source")
        source = (
            raw_source
            if isinstance(raw_source, str) and raw_source.strip()
            else _DEFAULT_NOOP_SOURCE
        )

        bucket = by_source.setdefault(source, {"total": 0, "noop": 0})
        bucket["total"] += 1

        if classify_noop_task(task):
            noop_count += 1
            bucket["noop"] += 1

    noop_rate = noop_count / total_success if total_success > 0 else 0.0

    by_source_result: dict[str, dict[str, object]] = {}
    for src, counts in sorted(by_source.items()):
        src_total = counts["total"]
        src_noop = counts["noop"]
        by_source_result[src] = {
            "total": src_total,
            "noop": src_noop,
            "rate": src_noop / src_total if src_total > 0 else 0.0,
        }

    return {
        "total_success": total_success,
        "noop_count": noop_count,
        "noop_rate": noop_rate,
        "by_source": by_source_result,
    }


# ---------------------------------------------------------------------------
# Settled-relap sub-classification — distinguish re-verifying an already-decided
# verdict (a slower lap in rigor's costume) from a genuine no-op at a live fork.
#
# ``classify_noop_task`` proves a task changed nothing but collapses two very
# different nothings into one bucket: (a) a genuine no-op at a *live* fork —
# gates green on a still-open PR, awaiting tend push, no remediation needed —
# and (b) re-verifying an *already-decided* verdict — a merged/closed PR, a
# resolved review thread, an already-handed-off lineage. That collapse is the
# blind spot ('Two-exhaustions-conflated'). These helpers separate the two so a
# future self can name a settled-relap lap before taking it. Measurement-only —
# changes no behavior, suppresses nothing.
# ---------------------------------------------------------------------------

# Sub-classification labels returned by :func:`classify_settled_relap`.
SettledRelapClass = Literal["settled_relap", "live_fork"]

# Markers that a no-op task re-verified an *already-decided* verdict: the PR
# merged or closed, the review thread resolved, the lineage handed off or
# escalated. Distinct from :data:`_NOOP_TASK_PATTERNS` (which only proves the
# task changed nothing); a no-op whose summary also carries any of these is a
# settled-relap rather than a live-fork no-op.
_SETTLED_VERDICT_PATTERNS: tuple[str, ...] = (
    # Keyword-bearing landing prose lives in ``narration_cues`` so the record-time
    # scrub protects it (#2970); the keyword-free settle markers stay local.
    *_SETTLED_VERDICT_LANDING_PHRASES,
    "already closed",
    "already handed off",
    "handed off",
    "already escalated",
    "already delivered",
    "merge-or-close",
)


def classify_settled_relap(
    task: dict[str, object],
) -> SettledRelapClass | None:
    """Sub-classify a no-op *task* as ``settled_relap`` or ``live_fork``.

    Returns ``None`` when *task* is not a no-op at all (per
    :func:`classify_noop_task`). For a no-op, returns:

    - ``"settled_relap"`` when its ``result_summary`` re-confirms an
      already-decided verdict (a merged/closed PR, a resolved review thread,
      an already-handed-off / escalated lineage) — matched case-insensitively
      against :data:`_SETTLED_VERDICT_PATTERNS`. This is the slower lap
      wearing rigor's costume.
    - ``"live_fork"`` otherwise — a genuine nothing-mine-at-a-live-fork no-op
      (gates green on a still-open PR, awaiting tend push, no remediation
      needed) where the verdict was never decided.

    The existing no-op classifier collapses both into one bucket; this
    separation makes the conflation legible without changing behavior.

    Pure function — zero SDK cost.
    """
    if not classify_noop_task(task):
        return None
    # ``classify_noop_task`` only returns True for a non-empty string
    # ``result_summary``, so the cast is sound and no guard is reachable here.
    lower = cast(str, task.get("result_summary")).lower()
    if any(pattern in lower for pattern in _SETTLED_VERDICT_PATTERNS):
        return "settled_relap"
    return "live_fork"


def _relap_counts(tasks: list[dict[str, object]]) -> dict[str, object]:
    """Count settled-relap vs live-fork no-ops across successful *tasks*.

    Filters to successful tasks (``outcome`` in :data:`_SUCCESS_OUTCOMES`),
    sub-classifies each via :func:`classify_settled_relap`, and returns::

        {
            "total_noop": int,       # successful no-op tasks
            "settled_relap": int,    # of those, re-verifications of a verdict
            "live_fork": int,        # of those, genuine live-fork no-ops
            "relap_rate": float,     # settled_relap / total_noop (0.0 if none)
        }

    ``relap_rate`` is the fraction of no-ops that were settled-relaps — the
    share of "diligent" confirmations that re-decided an already-decided
    verdict. Pure function — zero SDK cost.
    """
    settled = 0
    live = 0
    for task in tasks:
        outcome = task.get("outcome")
        if not isinstance(outcome, str) or outcome not in _SUCCESS_OUTCOMES:
            continue
        cls = classify_settled_relap(task)
        if cls == "settled_relap":
            settled += 1
        elif cls == "live_fork":
            live += 1
    total_noop = settled + live
    return {
        "total_noop": total_noop,
        "settled_relap": settled,
        "live_fork": live,
        "relap_rate": settled / total_noop if total_noop > 0 else 0.0,
    }


def measure_relap_rate(data_dir: Path, n: int = 0) -> dict[str, object]:
    """Measure the settled-relap rate among recent no-op tasks.

    Reads the *n* most recent task records from ``task_history.jsonl`` under
    *data_dir* via :meth:`Memory.get_recent_tasks` (``n`` ≤ 0 reads effectively
    all, mirroring :func:`measure_false_success_recurrence`'s CLI default),
    converts them to dicts, and delegates to :func:`_relap_counts`.

    Returns the :func:`_relap_counts` summary dict; ``relap_rate`` is the
    fraction of successful no-op tasks that re-verified an already-decided
    verdict rather than genuinely finding nothing at a live fork.

    Reads from disk; otherwise pure — zero SDK cost.
    """
    tasks = Memory(data_dir).get_recent_tasks(n=n if n and n > 0 else 10_000)
    task_dicts = [asdict(t) for t in tasks]
    return _relap_counts(task_dicts)


def compute_no_op_rate(tasks: list[TaskRecord], window: int) -> float:
    """Fraction of the last *window* tasks classified as no-op.

    Classifies each task's ``result_summary`` via :func:`is_no_op_task`.
    Returns ``0.0`` for empty input or ``window <= 0``. Pure function —
    zero SDK cost.
    """
    if not tasks or window <= 0:
        return 0.0
    recent = tasks[-window:]
    no_ops = sum(1 for t in recent if is_no_op_task(t.result_summary))
    return no_ops / len(recent)


# ---------------------------------------------------------------------------
# Task-origin classification — external vs self-directed success measurement
# ---------------------------------------------------------------------------

# Origin labels returned by :func:`classify_task_origin`.
_ORIGIN_EXTERNAL = "external"
_ORIGIN_SELF_DIRECTED = "self_directed"

# Outcome values considered successful for :func:`measure_success_by_origin`.
# Mirrors :data:`_SUCCESS_OUTCOMES` used by no-op measurement.
_ORIGIN_SUCCESS_OUTCOMES: frozenset[str] = frozenset({"success"})


def classify_task_origin(task: dict[str, object]) -> str:
    """Classify a task dict as ``'external'`` or ``'self_directed'``.

    Heuristics (applied in order, first match wins):

    1. ``goal`` starts with ``'Remediate PR #'`` → ``'external'``
    2. :func:`extract_issue_numbers` finds issue references in ``goal`` →
       ``'external'``
    3. ``goal`` starts with ``'Promote branch'`` → ``'external'``
    4. Otherwise → ``'self_directed'``

    Pure function: no I/O, no SDK cost.
    """
    goal = task.get("goal")
    if not isinstance(goal, str):
        return _ORIGIN_SELF_DIRECTED

    if goal.startswith("Remediate PR #"):
        return _ORIGIN_EXTERNAL
    if extract_issue_numbers(goal):
        return _ORIGIN_EXTERNAL
    if goal.startswith("Promote branch"):
        return _ORIGIN_EXTERNAL
    return _ORIGIN_SELF_DIRECTED


def measure_success_by_origin(
    data_dir: Path,
    n: int = 50,
) -> dict[str, dict[str, int | float]]:
    """Measure per-origin success rates from the task history tail.

    Reads the last *n* records from ``task_history.jsonl``, classifies
    each via :func:`classify_task_origin`, and returns::

        {
            "external": {"total": int, "success": int, "rate": float},
            "self_directed": {"total": int, "success": int, "rate": float},
        }

    Returns zeroed buckets when the file is missing or empty. Malformed
    lines are silently skipped. Pure read — no mutations.
    """
    path = data_dir / "task_history.jsonl"
    empty: dict[str, dict[str, int | float]] = {
        _ORIGIN_EXTERNAL: {"total": 0, "success": 0, "rate": 0.0},
        _ORIGIN_SELF_DIRECTED: {"total": 0, "success": 0, "rate": 0.0},
    }
    if not path.exists():
        return empty

    # Read all lines and take the tail.
    lines: list[str] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                lines.append(line)
    except OSError:
        return empty

    tail = lines[-n:] if n > 0 else []
    totals: dict[str, int] = {_ORIGIN_EXTERNAL: 0, _ORIGIN_SELF_DIRECTED: 0}
    successes: dict[str, int] = {_ORIGIN_EXTERNAL: 0, _ORIGIN_SELF_DIRECTED: 0}

    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            task = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(task, dict):
            continue

        origin = classify_task_origin(task)
        totals[origin] += 1

        outcome = task.get("outcome")
        if isinstance(outcome, str) and outcome in _ORIGIN_SUCCESS_OUTCOMES:
            successes[origin] += 1

    result: dict[str, dict[str, int | float]] = {}
    for origin in (_ORIGIN_EXTERNAL, _ORIGIN_SELF_DIRECTED):
        total = totals[origin]
        success = successes[origin]
        result[origin] = {
            "total": total,
            "success": success,
            "rate": success / total if total > 0 else 0.0,
        }
    return result


def format_origin_summary(data_dir: Path, n: int = 50) -> str:
    """Render a compact two-line summary of per-origin success rates.

    Reads via :func:`measure_success_by_origin` and formats like::

        TASK ORIGIN: external 8/10 (80.0%) | self_directed 5/12 (41.7%)

    Returns an empty string when no tasks are found. Pure read — no
    mutations.
    """
    data = measure_success_by_origin(data_dir, n)
    ext = data[_ORIGIN_EXTERNAL]
    sd = data[_ORIGIN_SELF_DIRECTED]
    ext_total = int(ext["total"])
    sd_total = int(sd["total"])
    if ext_total == 0 and sd_total == 0:
        return ""
    ext_rate = ext["rate"] * 100
    sd_rate = sd["rate"] * 100
    return (
        f"TASK ORIGIN: external {int(ext['success'])}/{ext_total} "
        f"({ext_rate:.1f}%) | self_directed {int(sd['success'])}/{sd_total} "
        f"({sd_rate:.1f}%)"
    )


# Mean pairwise token-overlap threshold above which the comfort-cluster
# check fires. Jaccard index averaged across all non-empty goal pairs —
# 0.70 is conservative enough that a narrow topical cluster still triggers
# while legitimately related-but-varied goals stay below.
_COMFORT_CLUSTER_THRESHOLD = 0.70

# Minimum number of tasks with non-empty goals required before the
# comfort-cluster check fires. Below two, no pairwise comparison exists.
_COMFORT_CLUSTER_MIN_TASKS = 2

# Task-window size for the comfort-cluster check. Comfort-zone detection
# operates on the *recent* behavioural pattern — 10 tasks mirrors the
# task-cost-risk gate's window. The caller may pass a wider window; this
# check slices to the 10 most recent internally so its Jaccard signal
# stays focused on the agent's current cluster behaviour.
_COMFORT_CLUSTER_WINDOW = 10


def check_task_goal_overlap(
    self_model: SelfModel,
    tasks: list[TaskRecord],
) -> list[AuditFinding]:
    """Detect comfort-zone repetition: recent task goals cluster too tightly.

    Operates on the **most recent 10 tasks** (sliced internally via
    :data:`_COMFORT_CLUSTER_WINDOW`) regardless of the window passed in.
    Strips stopwords and lowercases each task's ``goal``, computes the
    pairwise Jaccard index (intersection / union) for every pair of
    non-empty token sets, and fires when the **mean** exceeds
    :data:`_COMFORT_CLUSTER_THRESHOLD`.

    Edge cases:
    - Fewer than 2 tasks with non-empty goals → no finding.
    - Goals that reduce to an empty token set after stopword removal are
      skipped (same as blank goals).

    Self-extinguishing semantics: the check fires only while the
    behavioural record shows high homogeneity. Once diverse tasks land,
    the mean overlap drops and it stops matching — no explicit
    integration required; the condition ceasing is sufficient.

    *self_model* is accepted for call-shape parity with the other audit
    rules (a future limitation-mute can read it without breaking
    callers) but is currently unused.

    Pure function: no I/O, no side effects, zero SDK cost. Consumed by
    the ``comfort_cluster`` directive gate in :mod:`maestro.task_gates`.
    """
    del self_model  # parity-only; see docstring

    recent = tasks[-_COMFORT_CLUSTER_WINDOW:]

    # Collect (task_id, token-set) pairs from non-empty task goals.
    goal_token_sets: list[tuple[str, set[str]]] = []
    for task in recent:
        goal = task.goal or ""
        if not goal.strip():
            continue
        tokens = _content_tokens(goal)
        if not tokens:
            continue
        goal_token_sets.append((task.task_id, tokens))

    if len(goal_token_sets) < _COMFORT_CLUSTER_MIN_TASKS:
        return []

    # Mean pairwise Jaccard index across the collected token sets.
    total = 0.0
    pair_count = 0
    for i in range(len(goal_token_sets)):
        for j in range(i + 1, len(goal_token_sets)):
            set_a = goal_token_sets[i][1]
            set_b = goal_token_sets[j][1]
            union_size = len(set_a | set_b)
            if union_size == 0:  # pragma: no cover
                continue  # both empty — defensive; already filtered above
            total += len(set_a & set_b) / union_size
            pair_count += 1

    if pair_count == 0:  # pragma: no cover
        return []

    mean_overlap = total / pair_count
    if mean_overlap <= _COMFORT_CLUSTER_THRESHOLD:
        return []

    cites = [tid for tid, _ in goal_token_sets][:MAX_CITES_PER_FINDING]

    return [AuditFinding(
        kind="comfort_cluster",
        statement=(
            f"Recent task goals cluster tightly (mean pairwise overlap "
            f"{round(mean_overlap * 100)}%), suggesting comfort-zone "
            f"repetition."
        ),
        cites=cites,
        rule="comfort_cluster",
    )]


def format_investigation_rate_context(
    investigation_count: int,
    window: int,
) -> str:
    """Build a one-line investigation-rate summary for the prompt.

    Returns a string like ``'Investigation rate: 3/10 (30%) over last 10
    tasks'`` or an empty string when *window* is zero or negative so the
    caller can inject only when non-empty.

    Pure function — no I/O, no SDK cost.
    """
    if window <= 0:
        return ""
    pct = round(investigation_count / window * 100)
    return (
        f"Investigation rate: {investigation_count}/{window} "
        f"({pct}%) over last {window} tasks"
    )


# ── Investigation-signal classification (keyword-based) ────────────────
#
# Keywords that signal a task or finding involves self-investigation
# rather than pure engineering.  Case-insensitive matching; token
# boundaries enforced via ``\b`` so "self-model" matches but
# "myself-model" does not.  The list is intentionally narrow —
# false positives (engineering tasks that happen to mention "hypothesis")
# are worse than false negatives because the predicate must be a
# trustworthy binary instrument, not a generous classifier.
#
# Named ``_INVESTIGATION_SIGNAL_*`` to stay distinct from the shape-based
# ``_INVESTIGATION_RE`` above (``_SHAPE_PATTERNS`` / ``classify_task_shape``),
# which classifies read-only task *shape*, not keyword signal.
_INVESTIGATION_SIGNAL_KEYWORDS: tuple[str, ...] = (
    "self-investigation",
    "self-model",
    "self-observation",
    "falsif",                 # falsify, falsification, falsifiable
    "experiment",
    "hypothesis",
    "investigation",
    "introspect",             # introspection, introspective
    "cognitive",
    "behavioral pattern",
    "behavioural pattern",
    "selection bias",
    "meta-cognit",            # meta-cognition, meta-cognitive
    "measurement apparatus",
    "instrumentable",
)

# Compiled once at import time. ``re.IGNORECASE`` so "Hypothesis" and
# "EXPERIMENT" both match.
_INVESTIGATION_SIGNAL_RE: re.Pattern[str] = re.compile(
    "|".join(rf"\b{re.escape(kw)}" for kw in _INVESTIGATION_SIGNAL_KEYWORDS),
    re.IGNORECASE,
)


def compute_investigation_rate(
    tasks: list[TaskRecord],
    window: int = 20,
) -> tuple[float, int, int]:
    """Compute the investigation-task rate over the last *window* tasks.

    Returns ``(rate, investigation_count, window_size)`` where *rate* is
    ``investigation_count / window_size`` (0.0 when *window_size* is 0).

    Reuses :func:`_is_investigation_task` classification — no new
    heuristics.  Pure function: no I/O, no SDK cost.
    """
    if not tasks or window <= 0:
        return (0.0, 0, 0)
    recent = tasks[-window:]
    investigation_count = sum(1 for t in recent if _is_investigation_task(t))
    window_size = len(recent)
    rate = investigation_count / window_size
    return (rate, investigation_count, window_size)


def _is_investigation_task(task: TaskRecord) -> bool:
    """Return ``True`` if *task*'s goal or findings contain investigation signals.

    Checks the ``goal`` text and — when present — ``key_findings`` against
    :data:`_INVESTIGATION_SIGNAL_RE`.  Pure-engineering tasks that never mention
    investigation concepts return ``False``.
    """
    if task.goal and _INVESTIGATION_SIGNAL_RE.search(task.goal):
        return True
    return bool(
        task.key_findings and _INVESTIGATION_SIGNAL_RE.search(task.key_findings)
    )


# ---------------------------------------------------------------------------
# False-success recurrence — closure-blind accounting detector
# ---------------------------------------------------------------------------

# Matches the unified remediation-goal header ``Remediate PR #<n>:`` rendered
# by ``agent._compose_pr_remediation_prompt``. Only genuine remediation tasks
# (not arbitrary mentions of a PR) carry this header, so grouping is precise.
_FALSE_SUCCESS_PR_PATTERN = re.compile(r"Remediate PR #(\d+)")

# A PR carrying this many or more ``success`` outcomes in the window is
# evidence of false-success recurrence: the same remediation passed twice
# without the PR ever closing.
_FALSE_SUCCESS_THRESHOLD = 2


def measure_false_success_recurrence(
    history: list[dict[str, object]],
    n: int,
) -> dict[str, object]:
    """Count distinct PRs stamped ``success`` >=2x without closing.

    Scans the trailing *n* tasks of *history* (all when ``n <= 0``), keeps
    only remediation tasks whose goal/title matches
    ``Remediate PR #<number>``, groups them by extracted PR number, counts
    the ``success`` outcomes per PR, and returns::

        {
            "count": int,            # distinct PRs with >=2 successes
            "by_pr": {pr_number: success_count, ...},  # only the >=2 PRs
        }

    Recurrence of a *successful* remediation for the same PR is the hard
    evidence of failure-to-close: the pass was measured, the merge was not.
    This predicate reports that red number back. Measurement-only — no I/O,
    zero SDK cost.
    """
    tail = history[-n:] if n > 0 else history

    successes_by_pr: dict[int, int] = {}
    for task in tail:
        title = task.get("goal")
        if not isinstance(title, str) or not title:
            title = task.get("title")
        if not isinstance(title, str) or not title:
            continue
        match = _FALSE_SUCCESS_PR_PATTERN.search(title)
        if match is None:
            continue
        outcome = task.get("outcome")
        if not isinstance(outcome, str) or outcome not in _SUCCESS_OUTCOMES:
            continue
        pr_num = int(match.group(1))
        successes_by_pr[pr_num] = successes_by_pr.get(pr_num, 0) + 1

    by_pr = {
        pr: count
        for pr, count in sorted(successes_by_pr.items())
        if count >= _FALSE_SUCCESS_THRESHOLD
    }
    return {"count": len(by_pr), "by_pr": by_pr}


def format_false_success_recurrence(
    history: list[dict[str, object]],
    n: int,
) -> str:
    """Render a ``FALSE-SUCCESS RECURRENCE`` line, or ``""`` when none.

    Calls :func:`measure_false_success_recurrence` and, when the count is
    positive, returns a single line naming each twice-stamped PR and its
    success count. Returns an empty string when the count is zero so the
    caller injects only when there is failure-to-close to report.

    Pure function — no I/O, zero SDK cost.
    """
    result = measure_false_success_recurrence(history, n)
    count: int = result["count"]  # type: ignore[assignment]
    if count <= 0:
        return ""
    by_pr: dict[int, int] = result["by_pr"]  # type: ignore[assignment]
    parts = ", ".join(f"#{pr} (x{cnt})" for pr, cnt in by_pr.items())
    return (
        f"FALSE-SUCCESS RECURRENCE: {count} PR(s) stamped success "
        f">={_FALSE_SUCCESS_THRESHOLD}x without closing: {parts}"
    )


# ---------------------------------------------------------------------------
# Resolved-recurrence detector — surface PR-lineage tending pileups
# ---------------------------------------------------------------------------
#
# False-success recurrence (above) catches the *closure-blind* case: the same
# remediation passing repeatedly while the PR never merges. This detector
# catches the adjacent flaw — piling *zero-cost* successes onto a lineage that
# is already resolved, where recurrence reads as throughput but produces no
# value. It groups by any ``#NNNN`` reference (not just the remediation header)
# so post-merge tending pileups surface even when phrased loosely.

# Matches any ``#NNNN`` issue/PR reference anywhere in a task's text fields.
_PR_LINEAGE_PATTERN = re.compile(r"#(\d+)")

# Duration (seconds) at or below which a successful task is treated as
# effectively zero-cost — a preflight-skip or no-op closure rather than real
# work. Sub-second durations only land on the fast-close paths.
_ZERO_COST_DURATION_SECS = 1.0

# Substring marking a preflight-skip closure (recorded by the pre-remediation
# gate's ``[preflight]`` no-op path). Case-insensitive match.
_PREFLIGHT_SKIP_MARKER = Marker.PREFLIGHT

# A PR/issue lineage accumulating this many zero-cost ``success`` tasks is
# surfaced as a resolved-recurrence pileup: the substantive work has landed and
# further tending likely adds no value. Set above a single legitimate re-touch
# so the line fires only on a sustained pile (the #2348 pattern was 6-7 deep).
_RESOLVED_RECURRENCE_THRESHOLD = 3

# Task text fields scanned for ``#NNNN`` references, in priority order. Only
# string values participate; absent or non-string fields are skipped.
_PR_LINEAGE_TEXT_FIELDS: tuple[str, ...] = (
    "goal",
    "title",
    "description",
    "result_summary",
)


def _is_zero_cost_lineage_task(task: dict[str, object]) -> bool:
    """Return ``True`` when *task* is a zero-cost success closure.

    A task counts as zero-cost when it is classified as a no-op
    (:func:`classify_noop_task`), carries the preflight-skip marker in its
    ``result_summary``, or ran for at or below
    :data:`_ZERO_COST_DURATION_SECS`. These are the fast-close paths that
    produce a ``success`` outcome without substantive work.

    Pure helper — zero SDK cost.
    """
    if classify_noop_task(task):
        return True
    summary = task.get("result_summary")
    if isinstance(summary, str) and _PREFLIGHT_SKIP_MARKER in summary.lower():
        return True
    duration = task.get("duration_secs")
    return (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and duration <= _ZERO_COST_DURATION_SECS
    )


def _extract_pr_lineage_refs(task: dict[str, object]) -> set[int]:
    """Return the set of distinct ``#NNNN`` references in *task*'s text fields.

    Scans :data:`_PR_LINEAGE_TEXT_FIELDS` for ``#NNNN`` tokens via
    :data:`_PR_LINEAGE_PATTERN`. References are deduplicated per task so a PR
    mentioned twice in one task counts once. Pure helper — zero SDK cost.
    """
    refs: set[int] = set()
    for key in _PR_LINEAGE_TEXT_FIELDS:
        value = task.get(key)
        if isinstance(value, str) and value:
            for match in _PR_LINEAGE_PATTERN.finditer(value):
                refs.add(int(match.group(1)))
    return refs


def measure_pr_lineage_recurrence(
    tasks: list[dict[str, object]],
    threshold: int = _RESOLVED_RECURRENCE_THRESHOLD,
) -> dict[int, int]:
    """Count zero-cost ``success`` tasks per PR/issue lineage.

    Keeps only tasks whose ``outcome`` is in :data:`_SUCCESS_OUTCOMES` and
    that :func:`_is_zero_cost_lineage_task` flags as a fast-close (no-op,
    preflight-skip, or near-zero duration), extracts every ``#NNNN``
    reference via :func:`_extract_pr_lineage_refs`, and counts the successes
    per lineage. Returns a ``{pr_number: count}`` mapping (sorted ascending)
    containing only lineages whose count is at or above *threshold*.

    This is the awareness mirror for conflating recurrence with progress:
    a lineage piling up zero-cost successes has already been resolved, and
    further tending likely adds no value. Measurement-only — no I/O, zero
    SDK cost.
    """
    counts: dict[int, int] = {}
    for task in tasks:
        outcome = task.get("outcome")
        if not isinstance(outcome, str) or outcome not in _SUCCESS_OUTCOMES:
            continue
        if not _is_zero_cost_lineage_task(task):
            continue
        for pr in _extract_pr_lineage_refs(task):
            counts[pr] = counts.get(pr, 0) + 1
    return {
        pr: count
        for pr, count in sorted(counts.items())
        if count >= threshold
    }


# ---------------------------------------------------------------------------
# Outcome-trend measurement — is the success rate moving, or just being
# re-described?  Splits a recent window into older/newer halves and reports
# the per-half success / partial / investigation rates plus their deltas.
# ---------------------------------------------------------------------------

# Outcome value treated as a partial completion for trend accounting.
_PARTIAL_OUTCOME = "partial"

# Minimum task count required to form two non-trivial halves. Below this the
# split would put 0-1 tasks in a half, making the delta noise rather than
# signal, so :func:`measure_outcome_trends` returns ``None`` instead.
_OUTCOME_TREND_MIN_TASKS = 4


@dataclass(frozen=True)
class OutcomeTrend:
    """Per-half success/partial/investigation rates over a recent window.

    The window is split into an *older* and a *newer* half (older first,
    matching the oldest-first ordering of ``get_recent_tasks``); each
    ``*_delta`` is ``newer_rate - older_rate`` so a positive value means the
    rate rose in the more recent half. Rates are fractions in ``[0, 1]``.
    """

    older_success_rate: float
    newer_success_rate: float
    success_delta: float
    older_partial_rate: float
    newer_partial_rate: float
    partial_delta: float
    older_investigation_rate: float
    newer_investigation_rate: float
    investigation_delta: float
    older_window: int
    newer_window: int


def _outcome_half_rates(
    tasks: list[dict[str, object]],
) -> tuple[float, float, float]:
    """Return ``(success_rate, partial_rate, investigation_rate)`` for *tasks*.

    Success counts ``outcome`` in :data:`_SUCCESS_OUTCOMES`; partial counts
    ``outcome == "partial"``; investigation counts goals whose
    :func:`classify_task_shape` falls in :data:`_INVESTIGATION_SHAPES`.
    Returns all-zero rates for an empty list. Pure helper — zero SDK cost.
    """
    total = len(tasks)
    if total == 0:
        return (0.0, 0.0, 0.0)
    success = 0
    partial = 0
    investigation = 0
    for task in tasks:
        outcome = task.get("outcome")
        if isinstance(outcome, str):
            if outcome in _SUCCESS_OUTCOMES:
                success += 1
            elif outcome == _PARTIAL_OUTCOME:
                partial += 1
        goal = task.get("goal")
        shape = classify_task_shape(goal if isinstance(goal, str) else "")
        if shape in _INVESTIGATION_SHAPES:
            investigation += 1
    return (success / total, partial / total, investigation / total)


def measure_outcome_trends(
    tasks: list[dict[str, object]],
    window: int = 20,
) -> OutcomeTrend | None:
    """Compute success/partial/investigation rate deltas across a window.

    Takes recent task dicts (oldest-first, as produced by
    ``dataclasses.asdict`` over ``get_recent_tasks``), keeps the trailing
    *window*, splits it into an older and a newer half, and returns an
    :class:`OutcomeTrend` whose ``*_delta`` fields are ``newer - older``.

    Returns ``None`` when fewer than :data:`_OUTCOME_TREND_MIN_TASKS` tasks
    are available (or *window* is non-positive), since a split that small
    yields noise rather than a meaningful trend. Pure function — no I/O,
    zero SDK cost.
    """
    if window <= 0:
        return None
    recent = tasks[-window:]
    if len(recent) < _OUTCOME_TREND_MIN_TASKS:
        return None

    mid = len(recent) // 2
    older = recent[:mid]
    newer = recent[mid:]

    older_success, older_partial, older_inv = _outcome_half_rates(older)
    newer_success, newer_partial, newer_inv = _outcome_half_rates(newer)

    return OutcomeTrend(
        older_success_rate=older_success,
        newer_success_rate=newer_success,
        success_delta=newer_success - older_success,
        older_partial_rate=older_partial,
        newer_partial_rate=newer_partial,
        partial_delta=newer_partial - older_partial,
        older_investigation_rate=older_inv,
        newer_investigation_rate=newer_inv,
        investigation_delta=newer_inv - older_inv,
        older_window=len(older),
        newer_window=len(newer),
    )


def _trend_arrow(delta: float) -> str:
    """Return ``↑``/``↓``/``→`` for a positive/negative/zero *delta*."""
    if delta > 0:
        return "↑"
    if delta < 0:
        return "↓"
    return "→"


def _format_trend_metric(
    label: str,
    older_rate: float,
    newer_rate: float,
    delta: float,
) -> str:
    """Render one ``label O%→N% ↑Dpp`` fragment for the trend line."""
    arrow = _trend_arrow(delta)
    return (
        f"{label} {round(older_rate * 100)}%→{round(newer_rate * 100)}% "
        f"{arrow}{abs(round(delta * 100))}pp"
    )


def format_outcome_trend_summary(trend: OutcomeTrend | None) -> str:
    """Render a single-line ``OUTCOME TREND`` summary, or ``""`` when *None*.

    Produces a directional summary with percentage-point deltas for the
    success, partial, and investigation rates, e.g.::

        OUTCOME TREND (older 5 → newer 5 tasks): success 40%→60% ↑20pp, ...

    Returns an empty string for ``None`` input so the caller injects only
    when a trend is available. Pure function — no I/O, zero SDK cost.
    """
    if trend is None:
        return ""
    metrics = ", ".join([
        _format_trend_metric(
            "success",
            trend.older_success_rate,
            trend.newer_success_rate,
            trend.success_delta,
        ),
        _format_trend_metric(
            "partial",
            trend.older_partial_rate,
            trend.newer_partial_rate,
            trend.partial_delta,
        ),
        _format_trend_metric(
            "investigation",
            trend.older_investigation_rate,
            trend.newer_investigation_rate,
            trend.investigation_delta,
        ),
    ])
    return (
        f"OUTCOME TREND (older {trend.older_window} → "
        f"newer {trend.newer_window} tasks): {metrics}"
    )


# ---------------------------------------------------------------------------
# Directive-compliance measurement — does setting next_task_directive actually
# change the subsequent task, or are the directive bridges ceremonial?
#
# The identity model claims Maestro is "gate-steered, not insight-steered:
# blocking gates and directive bridges change behavior". This instrument tests
# the *directive-bridge* half of that claim against recorded data: it joins
# each directive-setting gate activation (``gate_activations.jsonl``) to the
# first task that ran *after* it (``task_history.jsonl``) and checks whether
# that task's goal carries the keywords the directive was steering toward.
#
# Decision boundaries (documented, not enforced in code): a sustained
# compliance rate below :data:`DIRECTIVE_COMPLIANCE_PRUNE_THRESHOLD` (30%) is
# evidence the bridges are ceremonial and should be pruned rather than
# extended; above :data:`DIRECTIVE_COMPLIANCE_VALIDATE_THRESHOLD` (70%)
# validates the gate-steered claim. Measurement-only — zero SDK cost.
# ---------------------------------------------------------------------------

# Gate names that set ``next_task_directive`` and log an
# ``action="directive_set"`` activation to ``gate_activations.jsonl`` whose
# directive text carries a *stable* intent-keyword set, so
# :func:`measure_directive_compliance` can score whether the steered task
# complied. Keep this set in lockstep with the
# ``log_gate_activation(..., action="directive_set")`` call sites in
# ``agent.generate_task`` — including the ``dropped_directive_replay`` chain
# gate (proposal 090), whose fire-or-close text ("land #N / retire the branch")
# has a stable keyword set.
#
# The narration→action writers (``audit_directive_bridge`` and the §5c
# record-time binds / forced arms, proposal 090) now *also* log activations but
# are deliberately excluded here: their directive/handoff detail is
# finding-specific with no stable intent-keyword set, so a keyword-overlap score
# would be noise. Their conversion is measured by
# :func:`measure_narration_conversion` instead — it joins each actuation to its
# git-grounded settlement (the cited PR merged / SHA landed), not to task
# keywords.
DIRECTIVE_GATE_TYPES: frozenset[str] = frozenset({
    "dropped_directive_replay",
    "instrument_consumption",
    "integration_cadence",
    "investigation_rate",
    "investigation_streak",
    "partial_construction",
    "promotable_backlog_directive",
    "self_directed_success",
})

# Per-gate keywords reflecting the behavioral intent each directive steers
# toward. A subsequent task is "compliant" when its goal (whole-word
# tokenized) shares at least one keyword with its gate's set. All bridges
# push toward construction / integration, so the keyword sets centre on
# build/fix/integrate verbs tuned to each gate's specific directive text.
DIRECTIVE_INTENT_KEYWORDS: dict[str, frozenset[str]] = {
    "dropped_directive_replay": frozenset({
        "land", "close", "merge", "retire", "pr", "branch",
    }),
    "instrument_consumption": frozenset({
        "consume", "review", "use", "wire", "integrate", "apply",
        "instrument", "act", "surface",
    }),
    "integration_cadence": frozenset({
        "integrate", "merge", "promote", "branch", "unmerged", "triage",
        "backlog",
    }),
    "investigation_rate": frozenset({
        "build", "fix", "wire", "integrate", "promote", "construct",
    }),
    "investigation_streak": frozenset({
        "build", "construct", "implement", "create", "add",
    }),
    "partial_construction": frozenset({
        "complete", "finish", "implement", "build", "fix", "add", "wire",
    }),
    "promotable_backlog_directive": frozenset({
        "promote", "integrate", "merge", "branch", "cite", "backlog",
    }),
    "self_directed_success": frozenset({
        "build", "construct", "implement", "add", "fix", "create",
    }),
}

# Minimum directive activations (with a subsequent task) before
# :func:`format_directive_compliance_summary` surfaces a block. Below this the
# rate is noise rather than signal.
DIRECTIVE_COMPLIANCE_MIN_ACTIVATIONS = 3

# Decision boundaries for the compliance rate. Documented for the operator and
# referenced by the docstrings; not enforced as code gates.
DIRECTIVE_COMPLIANCE_PRUNE_THRESHOLD = 0.30
DIRECTIVE_COMPLIANCE_VALIDATE_THRESHOLD = 0.70

# Filename of the task-history log scanned for subsequent-task lookup. Mirrors
# the path ``Memory`` writes to; read-only here.
_TASK_HISTORY_FILENAME = "task_history.jsonl"


def _load_directive_compliance_tasks(data_dir: Path) -> list[tuple[str, str]]:
    """Read ``task_history.jsonl`` into ``(timestamp, goal)`` pairs, sorted.

    Returns an ascending-by-timestamp list so the caller can binary-search
    for the first task following a given activation timestamp. Records
    missing or carrying an empty-string ``timestamp`` or ``goal`` are
    skipped (an empty goal can never match a directive keyword, so it would
    deflate the compliance-rate denominator); a missing or unreadable file
    yields an empty list. Malformed lines — undecodable or non-object JSON —
    are skipped (corrupted-tail tolerance matching :meth:`Memory.get_recent_tasks`).
    """
    path = data_dir / _TASK_HISTORY_FILENAME
    if not path.exists():
        return []
    out: list[tuple[str, str]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                ts = record.get("timestamp")
                goal = record.get("goal")
                if isinstance(ts, str) and ts and isinstance(goal, str) and goal:
                    out.append((ts, goal))
    except OSError:
        return []
    out.sort(key=lambda pair: pair[0])
    return out


def measure_directive_compliance(
    backend: GateActivationBackend,
    data_dir: Path,
    n: int = 50,
) -> dict[str, object]:
    """Correlate directive-bridge activations with subsequent task keywords.

    Reads the trailing *n* rows of the gate-activation trace via
    ``backend.read_all()``, keeps only directive-setting activations (gate in
    :data:`DIRECTIVE_GATE_TYPES` with ``action == "directive_set"``), and for
    each finds the first task in ``task_history.jsonl`` (still read from
    *data_dir*) whose timestamp is strictly later (the task the directive was
    meant to steer). A task is *compliant* when its goal shares at least one
    whole-word token with its gate's :data:`DIRECTIVE_INTENT_KEYWORDS` set.
    Returns::

        {
            "total_activations": int,    # activations with a subsequent task
            "compliant_count": int,      # of those, goals matching intent
            "compliance_rate": float,    # compliant / total, 0.0 when total 0
            "per_gate": {                # only gates that fired in the window
                <gate>: {"total": int, "compliant": int},
                ...
            },
        }

    Activations with no subsequent task (e.g. the most recent activation,
    fired after the last recorded task) are excluded from the denominator —
    their behavioral effect has not yet been observed. Returns all-zero when
    the trace or task history is empty or no directive activations exist. Pure
    read — no mutations, zero SDK cost.

    The *n*-window bounds the last *n* well-formed rows (``read_all()`` already
    drops corrupt rows), not the last *n* raw lines the pre-fold reader tailed
    before parsing — identical on a clean store, differing only by dropped-row
    slots on a crash-torn tail (proposal 106).
    """
    empty: dict[str, object] = {
        "total_activations": 0,
        "compliant_count": 0,
        "compliance_rate": 0.0,
        "per_gate": {},
    }
    records = backend.read_all()
    tail = records[-n:] if n > 0 else []
    activations: list[tuple[str, str]] = []  # (gate, timestamp)
    for record in tail:
        gate = record.gate
        action = record.action
        ts = record.timestamp
        if (
            isinstance(gate, str)
            and gate in DIRECTIVE_GATE_TYPES
            and action == "directive_set"
            and isinstance(ts, str)
            and ts
        ):
            activations.append((gate, ts))

    if not activations:
        return dict(empty)

    tasks = _load_directive_compliance_tasks(data_dir)
    if not tasks:
        return dict(empty)
    task_timestamps = [ts for ts, _ in tasks]

    total = 0
    compliant = 0
    per_gate: dict[str, dict[str, int]] = {}
    for gate, activation_ts in activations:
        # First task strictly after the activation timestamp.
        idx = bisect.bisect_right(task_timestamps, activation_ts)
        if idx >= len(tasks):
            continue  # no subsequent task — excluded from the denominator
        goal = tasks[idx][1]
        total += 1
        bucket = per_gate.setdefault(gate, {"total": 0, "compliant": 0})
        bucket["total"] += 1
        keywords = DIRECTIVE_INTENT_KEYWORDS.get(gate, frozenset())
        if set(tokenize(goal)) & keywords:
            compliant += 1
            bucket["compliant"] += 1

    return {
        "total_activations": total,
        "compliant_count": compliant,
        "compliance_rate": compliant / total if total > 0 else 0.0,
        "per_gate": per_gate,
    }


def format_directive_compliance_summary(
    backend: GateActivationBackend, data_dir: Path,
) -> str:
    """Render a ``DIRECTIVE COMPLIANCE`` line, or ``""`` when too sparse.

    Calls :func:`measure_directive_compliance` (gate rows via *backend*, task
    history from *data_dir*) and returns a single-line summary naming the
    overall rate and the per-gate compliant/total breakdown. Returns an empty
    string when fewer than :data:`DIRECTIVE_COMPLIANCE_MIN_ACTIVATIONS` directive
    activations have an observed subsequent task, so the caller injects only when
    the rate is grounded in enough data to act on. Zero SDK cost.
    """
    result = measure_directive_compliance(backend, data_dir)
    total = result["total_activations"]
    if (
        not isinstance(total, int)
        or total < DIRECTIVE_COMPLIANCE_MIN_ACTIVATIONS
    ):
        return ""
    compliant = result["compliant_count"]
    rate = result["compliance_rate"]
    per_gate: dict[str, dict[str, int]] = result["per_gate"]  # type: ignore[assignment]
    breakdown = ", ".join(
        f"{gate} {stats['compliant']}/{stats['total']}"
        for gate, stats in sorted(per_gate.items())
    )
    rate_pct = round(rate * 100) if isinstance(rate, (int, float)) else 0
    return (
        f"DIRECTIVE COMPLIANCE: {compliant}/{total} ({rate_pct}%) "
        f"subsequent tasks matched directive intent [{breakdown}]"
    )


# Settled-relap rate above which :func:`format_known_state_summary` surfaces a
# SETTLED-RELAP line. Below this share of no-ops being verdict re-confirmations
# the pattern is noise; above it, re-verifying decided verdicts dominates the
# "diligent" no-ops and is worth naming before the next lap.
_SETTLED_RELAP_RATE_THRESHOLD = 0.4


def format_settled_relap_summary(
    task_history: list[dict[str, object]] | None,
    threshold: float = _SETTLED_RELAP_RATE_THRESHOLD,
) -> str:
    """Render a SETTLED-RELAP line when verdict re-confirmations dominate no-ops.

    Counts settled-relap vs live-fork no-ops in *task_history* via
    :func:`_relap_counts`. Returns a single line naming the share only when at
    least one no-op exists and the relap rate strictly exceeds *threshold*;
    otherwise ``""`` so the caller injects nothing.

    This is the awareness mirror for 'Two-exhaustions-conflated': it names how
    often a "diligent" no-op was actually re-verifying an already-decided
    verdict rather than finding nothing at a live fork. Pure function — zero
    SDK cost.
    """
    if not task_history:
        return ""
    counts = _relap_counts(task_history)
    total_noop: int = counts["total_noop"]  # type: ignore[assignment]
    settled: int = counts["settled_relap"]  # type: ignore[assignment]
    rate: float = counts["relap_rate"]  # type: ignore[assignment]
    if total_noop <= 0 or rate <= threshold:
        return ""
    return (
        f"SETTLED-RELAP: {settled}/{total_noop} no-op tasks ({rate:.0%}) "
        "re-verified an already-decided verdict (merged/closed PR, resolved "
        "thread, handed-off lineage) rather than a live fork — a slower lap "
        "in rigor's costume, not progress."
    )


def format_known_state_summary(
    gate_states: dict[str, bool],
    recent_goals: list[str],
    max_goals: int = 5,
    max_goal_len: int = 80,
    investigation_rate_context: str = "",
    prediction_accuracy_context: str = "",
    data_sufficiency_context: str = "",
    unlanded_remediation_context: str = "",
    pr_readmission_context: str = "",
    forced_quiet_context: str = "",
    standdown_production_context: str = "",
    base_advance_races_context: str = "",
    confirmed_landing_context: str = "",
    reality_refutation_context: str = "",
    narration_conversion_context: str = "",
    undefined_coinage_context: str = "",
    task_history: list[dict[str, object]] | None = None,
    false_success_window: int = 0,
    data_dir: Path | None = None,
    pr_remediation_max_attempts_per_head: int = DEFAULT_MAX_ATTEMPTS_PER_HEAD,
    pr_lap_context: str = "",
    pr_churn_context: str = "",
    gate_activation_backend: GateActivationBackend | None = None,
    pr_ledger_backend: PRLedgerBackend | None = None,
) -> str:
    """Build an ``## Already Known State`` prompt section for task generation.

    Formats *gate_states* (mapping gate name → enabled/disabled) and
    *recent_goals* (most-recent task goal strings) into a concise context
    block so the LLM can see what the system already knows and avoid
    proposing redundant investigation of already-confirmed state.

    *max_goals* caps how many recent goals appear (default 5).
    *max_goal_len* truncates each goal string, appending ``…`` when
    truncated (default 80 chars).

    *investigation_rate_context*, when non-empty, is appended as a
    purpose-alignment metric line. Populated by
    :func:`format_investigation_rate_context` at zero SDK cost.

    *prediction_accuracy_context*, when non-empty, is appended as a
    calibration metric line. Populated by
    :func:`measure_prediction_accuracy` at zero SDK cost.

    *data_sufficiency_context*, when non-empty, is appended as a
    per-instrument data-sufficiency block. Populated by
    :func:`format_data_sufficiency_context` at zero SDK cost.

    *unlanded_remediation_context*, when non-empty, is appended as an
    ``UNLANDED-REMEDIATION`` line. Populated by
    :func:`maestro.remediation_tracker.format_unlanded_remediations`, the
    git-merge-truth ground-truth sensor that surfaces remediations stamped
    success/partial whose PR never reached ``main`` — distinct from the
    stamp-based recurrence detectors below. Computed by the caller (the
    I/O lives there), so this function stays pure.

    *pr_readmission_context*, when non-empty, is appended as a single
    ``PR-READMISSION`` line carrying both readmission intents. Populated by
    :func:`maestro.remediation_tracker.format_pr_readmission_combined`, which
    folds the admission-channel count (PRs re-admitted through the FIFO-exempt
    ``pending_pr_requests`` bypass) and the merged-re-serviced rate (already-
    merged PRs re-fed as later ``Remediate PR #<n>`` tasks, git-merge-truth)
    into one line. Computed by the caller (the channel-log and git-oracle I/O
    live there), so this function stays pure.

    *pr_lap_context*, when non-empty, is appended as a ``PR-LAPS`` line.
    Populated by :func:`maestro.remediation_tracker.format_pr_lap_summary`,
    which folds per-PR remediation lap counts (against the host-first
    merge-truth oracle) and any active handoff standdowns into one line so a
    pileup is observable in contemplation before it reaches
    ``[loop] pr_remediation_lap_budget`` (proposal 080 step 4). Computed by the
    caller (lap measurement and standdown read live there), so this function
    stays pure.

    *pr_churn_context*, when non-empty, is appended as the ``PR-READY`` /
    ``PR-STUCK`` churn block. Populated by
    :func:`maestro.remediation_tracker.format_pr_churn_summary` (proposal 089
    step 6), which names a PR whose work is done and awaits approval/push
    (``PR-READY``) and a PR genuinely grinding past the no-progress budget
    (``PR-STUCK``), while a PR churning *with* progress raises neither — so a
    fast main's necessary churn reads as the job, not a treadmill. Computed by
    the caller, so this function stays pure.

    *forced_quiet_context*, when non-empty, is appended as a
    ``FORCED-QUIET-RATIO`` line. Populated by
    :func:`maestro.remediation_tracker.format_forced_vs_chosen_quiet`, the
    mirror that classifies every remediation quiet as externally forced
    (``[merge-block]`` / ``[branch-resolved]`` / ``[preflight]`` / ``awaiting
    tend push``) versus self-initiated (``[stood-down]``). Unlike its sibling
    sensors it does not count laps; it reads 100%-forced until a stand-down I
    chose lands. Computed by the caller, so this function stays pure.

    *standdown_production_context*, when non-empty, is appended as a
    ``STAND-DOWN-PRODUCTION`` line. Populated by
    :func:`maestro.remediation_tracker.format_standdown_production`, the counter
    that names how many ``[stood-down]`` verdicts the ledger holds all-time
    against the PR lineages now at the stall threshold (blocker ownership, review
    threads, and merge state deliberately left unverified there) — a persistent
    gap can mean the mechanical eviction gate is starved and stalled PRs keep
    re-lapping. Computed by the caller, so this function stays pure.

    *base_advance_races_context*, when non-empty, is appended as a
    ``BASE-ADVANCE-RACES`` line. Populated by
    :func:`maestro.remediation_tracker.format_base_advance_races` (proposal 099),
    the zero-SDK convergence sensor that reports how many conflict-remediation
    lineages re-entered the resolution race on a moved ``origin/main`` and then
    merged, against how many re-entered at all — separating re-entry-is-working
    (races land) from re-entry-is-spinning (races recur without landing),
    confirming the now-unconditional re-entry behaviour keeps converging conflicts;
    self-suppressed below its lineage-count floor. Computed by the caller, so this
    function stays pure.

    *confirmed_landing_context*, when non-empty, is appended as a
    ``CONFIRMED-LANDING-RATIO`` line. Populated by
    :func:`maestro.stats.format_confirmed_landing_summary`, the mirror that names
    how often a cure stamped ``success`` was merely confirmed already-landed
    (another's merge) rather than caused to land by the task — suppressed below
    its threshold. Computed by the caller, so this function stays pure.

    *reality_refutation_context*, when non-empty, is appended as a
    ``REALITY-REFUTATION`` line. Populated by
    :func:`format_reality_refutation_summary` (proposal 088 Phase 2), which
    names each git-grounded audit finding the LLM has refuted across a threshold
    of consecutive cycles while git still contradicts the claim — the awareness
    mirror that makes a sustained narration-vs-git dispute visible before the
    Phase 2 actuator escalates it. Computed by the caller, so this function
    stays pure.

    *narration_conversion_context*, when non-empty, is appended as a
    ``NARRATION-CONVERSION`` line. Populated by
    :func:`format_narration_conversion_summary` (proposal 090), which reports
    per narration→action writer how many detected narration problems became
    durable actuations (fire-or-close directives / handoff deliveries) and how
    many of those actuations' PRs/SHAs later settled — the evidence path toward
    graduating the default-off actuator flags. Computed by the caller (the
    git-fact oracles live there), so this function stays pure.

    *undefined_coinage_context*, when non-empty, is appended as an
    ``UNDEFINED-COINAGE`` line. Populated by
    :func:`format_undefined_coinage_summary` (proposal 101) over
    :func:`measure_undefined_coinages`, naming the coinages the journal is
    actively minting that ``docs/lexicon.md`` does not yet define — the live
    feedback loop between minting and defining. Computed by the caller (the
    lexicon and journal reads live there), so this function stays pure.

    *pr_ledger_backend*, when supplied alongside *data_dir*, routes the
    ``STUCK-PR`` / ``PARKED-PR`` ledger read through the flag-selected
    :class:`~maestro.pr_ledger.PRLedgerBackend` (``pr_ledger.json`` default or
    ``state.db``); when ``None`` the default JSON store is read (proposal 106).

    *task_history*, when provided, is scanned by
    :func:`format_false_success_recurrence` (windowed by
    *false_success_window*; ``0`` means all) so a ``FALSE-SUCCESS
    RECURRENCE`` line auto-injects when distinct PRs carry ≥2 success
    stamps without closing. The line is omitted when the count is zero.
    *task_history* is also passed to :func:`measure_outcome_trends` so an
    ``OUTCOME TREND`` line auto-injects when enough tasks are present,
    surfacing whether the success/partial/investigation rates are moving.
    Finally, :func:`measure_pr_lineage_recurrence` scans *task_history* so a
    ``RESOLVED-RECURRENCE`` line auto-injects per PR/issue lineage that has
    piled up ``_RESOLVED_RECURRENCE_THRESHOLD`` or more zero-cost success
    tasks — the awareness mirror for conflating recurrence with progress.
    Omitted when no lineage qualifies. *task_history* is finally scanned by
    :func:`format_settled_relap_summary` so a ``SETTLED-RELAP`` line
    auto-injects when the share of no-op tasks that re-verified an
    already-decided verdict (rather than a live fork) exceeds
    :data:`_SETTLED_RELAP_RATE_THRESHOLD`. Omitted below threshold.

    *data_dir*, when provided, is passed to
    :func:`format_directive_compliance_summary` so a ``DIRECTIVE COMPLIANCE``
    line auto-injects once ≥:data:`DIRECTIVE_COMPLIANCE_MIN_ACTIVATIONS`
    directive-bridge activations have an observed subsequent task. *data_dir*
    is also passed to :func:`maestro.stats.format_pending_plan_backlog` so a
    ``PENDING-PLAN BACKLOG`` line auto-injects when the count of
    ``pending_review`` plans in ``evolution_plans.jsonl`` reaches its threshold
    — the anti-treadmill mirror that makes my own unlanded backlog impossible
    to not-see before I mint another redundant sibling. *data_dir* is finally
    passed to :func:`maestro.remediation_tracker.format_refusal_profile` so a
    ``REFUSAL-PROFILE`` block auto-injects once enough directive-shaped tasks are
    classified — the live comply / redirect / refuse-structural /
    refuse-by-correctness distribution, whose subject is the blind spot itself
    (has a correctness refusal ever fired?); self-suppressed below its data
    floor. *data_dir* is also passed to
    :func:`maestro.remediation_tracker.measure_live_plan_collisions` so a
    ``LIVE-PLAN-COLLISION`` line auto-injects when a recent live plan reused
    discriminating ``files_affected`` another live plan already claims — the
    falsifiable duplicate-concern readout (proposal 100); self-suppressed at
    zero. This is the one input that reads from disk (``task_history.jsonl`` +
    ``evolution_plans.jsonl`` + the PR-ledger store via *pr_ledger_backend*,
    plus the gate-activation trace via *gate_activation_backend*); leaving
    *data_dir* ``None`` keeps the function pure with no I/O.

    *gate_activation_backend*, when supplied alongside *data_dir*, routes the
    ``DIRECTIVE COMPLIANCE`` line's gate-row read through the flag-selected
    backend (JSONL default or ``state.db``); when ``None`` that line is omitted.

    *gate_activation_backend*, on its own (no *data_dir* needed), also drives an
    ``OWED-ACT-DIVERGENCE`` line via
    :func:`maestro.remediation_tracker.format_owed_act_divergence` (proposal
    137), the zero-SDK reading of how many tend cooldown skips declined a PR
    whose owed act — measured in the same lap — was ``code-owed``; it
    self-suppresses below its data floor and self-extinguishes at zero
    divergence.

    *pr_remediation_max_attempts_per_head* must mirror the configured budget
    (``[loop] pr_remediation_max_attempts_per_head``) so the ``PARKED-PR``
    threshold in :func:`maestro.pr_ledger.format_stuck_summary` agrees with
    the ledger's own ``can_attempt`` gate rather than the module default.

    Returns an empty string when all inputs are empty so the caller
    can inject only when non-empty. No SDK cost.
    """
    false_success_line = (
        format_false_success_recurrence(task_history, false_success_window)
        if task_history
        else ""
    )
    outcome_trend_line = (
        format_outcome_trend_summary(measure_outcome_trends(task_history))
        if task_history
        else ""
    )
    settled_relap_line = (
        format_settled_relap_summary(task_history)
        if task_history
        else ""
    )
    directive_compliance_line = (
        format_directive_compliance_summary(gate_activation_backend, data_dir)
        if data_dir is not None and gate_activation_backend is not None
        else ""
    )
    stuck_pr_line = (
        _format_stuck_summary(
            data_dir,
            max_attempts_per_head=pr_remediation_max_attempts_per_head,
            backend=pr_ledger_backend,
        )
        if data_dir is not None
        else ""
    )
    # Local import: ``stats`` depends on ``audit_checks`` transitively (via
    # health_check/memory/remediation_tracker), so a module-level import here
    # would close an import cycle. The PENDING-PLAN BACKLOG mirror names my own
    # pile of unreviewed plans before I mint another redundant sibling.
    if data_dir is not None:
        from maestro.stats import format_pending_plan_backlog
        pending_plan_backlog_line = format_pending_plan_backlog(data_dir)
    else:
        pending_plan_backlog_line = ""
    # REFUSAL-PROFILE: the live comply/redirect/refuse-by-correctness
    # distribution over recent directive-shaped tasks — the sensor whose
    # subject is the blind spot itself (has refusal-by-correctness ever fired?).
    # Reads ``task_history.jsonl``; self-suppresses below its data floor.
    if data_dir is not None:
        from maestro.remediation_tracker import format_refusal_profile
        refusal_profile_line = format_refusal_profile(data_dir)
    else:
        refusal_profile_line = ""
    # OWED-ACT-DIVERGENCE: how often a tend cooldown skip declined a PR the
    # walk's own owed-act oracle read as code-owed (proposal 137). Reads only the
    # gate-activation trace, so it needs no ``data_dir``; self-suppresses below
    # its data floor and at zero divergence.
    if gate_activation_backend is not None:
        owed_act_divergence_line = format_owed_act_divergence(
            gate_activation_backend,
        )
    else:
        owed_act_divergence_line = ""
    # LIVE-PLAN-COLLISION: how many recent live plans reuse discriminating files
    # another live plan already claims — the falsifiable duplicate-concern readout
    # (proposal 100). Reads ``evolution_plans.jsonl``; self-suppresses at zero,
    # since a clean slate surfaced every cycle is noise, not signal.
    if data_dir is not None:
        from maestro.remediation_tracker import (
            _LIVE_PLAN_COLLISION_WINDOW,
            measure_live_plan_collisions,
        )
        collision = measure_live_plan_collisions(
            data_dir, _LIVE_PLAN_COLLISION_WINDOW,
        )
        if collision["collisions"] > 0:
            live_plan_collision_line = (
                f"LIVE-PLAN-COLLISION: {collision['collisions']} of the last "
                f"{collision['window']} live plans reused files another live "
                "plan already claims — a sibling may already be in flight; check "
                "the live-work backlog before minting another."
            )
        else:
            live_plan_collision_line = ""
    else:
        live_plan_collision_line = ""
    resolved_recurrence_lines = (
        [
            f"RESOLVED-RECURRENCE: PR #{pr} — {count} zero-cost tending "
            "successes; further tending likely adds no value"
            for pr, count in measure_pr_lineage_recurrence(task_history).items()
        ]
        if task_history
        else []
    )

    if (
        not gate_states
        and not recent_goals
        and not investigation_rate_context
        and not prediction_accuracy_context
        and not data_sufficiency_context
        and not unlanded_remediation_context
        and not pr_lap_context
        and not pr_churn_context
        and not pr_readmission_context
        and not forced_quiet_context
        and not standdown_production_context
        and not base_advance_races_context
        and not confirmed_landing_context
        and not reality_refutation_context
        and not narration_conversion_context
        and not undefined_coinage_context
        and not false_success_line
        and not outcome_trend_line
        and not settled_relap_line
        and not directive_compliance_line
        and not stuck_pr_line
        and not pending_plan_backlog_line
        and not refusal_profile_line
        and not live_plan_collision_line
        and not owed_act_divergence_line
        and not resolved_recurrence_lines
    ):
        return ""

    lines: list[str] = ["## Already Known State"]

    if gate_states:
        lines.append("Gate states:")
        for name, enabled in sorted(gate_states.items()):
            status = "enabled" if enabled else "disabled"
            lines.append(f"  - {name}: {status}")

    goals = recent_goals[:max_goals]
    if goals:
        lines.append("Recent task goals (already investigated — do not repeat):")
        for goal in goals:
            if len(goal) > max_goal_len:
                goal = goal[:max_goal_len] + "…"
            lines.append(f"  - {goal}")

    if investigation_rate_context:
        lines.append(investigation_rate_context)

    if prediction_accuracy_context:
        lines.append(prediction_accuracy_context)

    if data_sufficiency_context:
        lines.append(data_sufficiency_context)

    if unlanded_remediation_context:
        lines.append(unlanded_remediation_context)

    if pr_lap_context:
        lines.append(pr_lap_context)

    if pr_churn_context:
        lines.append(pr_churn_context)

    if pr_readmission_context:
        lines.append(pr_readmission_context)

    if forced_quiet_context:
        lines.append(forced_quiet_context)

    if standdown_production_context:
        lines.append(standdown_production_context)

    if base_advance_races_context:
        lines.append(base_advance_races_context)

    if confirmed_landing_context:
        lines.append(confirmed_landing_context)

    if reality_refutation_context:
        lines.append(reality_refutation_context)

    if narration_conversion_context:
        lines.append(narration_conversion_context)

    if undefined_coinage_context:
        lines.append(undefined_coinage_context)

    if false_success_line:
        lines.append(false_success_line)

    if outcome_trend_line:
        lines.append(outcome_trend_line)

    if settled_relap_line:
        lines.append(settled_relap_line)

    if directive_compliance_line:
        lines.append(directive_compliance_line)

    if stuck_pr_line:
        lines.append(stuck_pr_line)

    if pending_plan_backlog_line:
        lines.append(pending_plan_backlog_line)

    if refusal_profile_line:
        lines.append(refusal_profile_line)

    if live_plan_collision_line:
        lines.append(live_plan_collision_line)

    if owed_act_divergence_line:
        lines.append(owed_act_divergence_line)

    lines.extend(resolved_recurrence_lines)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Investigation-streak directive — redirect sustained investigation runs
# toward construction
# ---------------------------------------------------------------------------

# Shapes classified by :func:`classify_task_shape` that are considered
# "investigation-shaped" — measurement, explicit investigation, and
# open-ended/unbounded (``other``). These are the shapes that default
# to introspection over external service. Construction-shaped tasks
# (``github_issue``, ``branch_promotion``, ``module_extension``,
# ``construction``, ``remediation``) break the streak.
_INVESTIGATION_SHAPES: frozenset[str] = frozenset({
    "measurement",
    "investigation",
    "other",
})


def check_investigation_streak(
    tasks: list[TaskRecord],
    threshold: int,
) -> str | None:
    """Return a directive when the last *threshold* tasks are all investigation-shaped.

    Walks backward from the most recent task in *tasks*, classifying each
    via :func:`classify_task_shape`. If the trailing *threshold* tasks are
    all in :data:`_INVESTIGATION_SHAPES` (``measurement``,
    ``investigation``, or ``other``), returns a directive string requiring
    the next task to be construction-shaped (build, fix, wire, integrate,
    or promote).

    Returns ``None`` when:
    - *tasks* is empty
    - fewer than *threshold* tasks exist
    - *threshold* is ≤ 0
    - the streak is broken by a construction-shaped task

    Self-extinguishing: one construction-shaped task resets the streak.
    Pure function — no I/O, zero SDK cost.
    """
    if threshold <= 0 or len(tasks) < threshold:
        return None
    tail = tasks[-threshold:]
    for t in tail:
        shape = classify_task_shape(t.goal or "")
        if shape not in _INVESTIGATION_SHAPES:
            return None
    return (
        f"Last {threshold} tasks were all investigation-shaped "
        "(measurement/investigation/other). "
        "Next task must be construction-shaped: build, fix, wire, integrate, "
        "or promote a branch."
    )


def compute_shape_investigation_rate(data_dir: Path, window: int = 10) -> float:
    """Return the fraction of the last *window* tasks that are investigation-shaped.

    Reads the *window* most recent task records from ``task_history.jsonl``
    under *data_dir* via :meth:`Memory.get_recent_tasks`, classifies each
    goal with :func:`classify_task_shape`, and returns the fraction whose
    shape is in :data:`_INVESTIGATION_SHAPES` (``measurement``,
    ``investigation``, or ``other``).

    Returns ``0.0`` when no tasks exist or *window* is ≤ 0.

    This is the windowed-rate complement to
    :func:`check_investigation_streak`, which requires *consecutive*
    investigation tasks. The streak gate never fires when mechanical tasks
    (promotions, remediations) interleave with investigations; the rate
    check catches that distributed pattern by measuring the fraction over a
    window rather than requiring an unbroken run.

    Shape-based by design: distinct from
    :func:`compute_investigation_rate`, which classifies via
    keyword regex over goal/findings for the falsification predicate. This
    one uses :func:`classify_task_shape` so it shares the streak gate's
    notion of "investigation-shaped".

    Pure read — no mutations, zero SDK cost.
    """
    if window <= 0:
        return 0.0
    tasks = Memory(data_dir).get_recent_tasks(window)
    if not tasks:
        return 0.0
    investigation = sum(
        1
        for t in tasks
        if classify_task_shape(t.goal or "") in _INVESTIGATION_SHAPES
    )
    return investigation / len(tasks)


# ---------------------------------------------------------------------------
# Integration-cadence gate — mechanical directive injection for unmerged work
# ---------------------------------------------------------------------------

# Compiled regex matching integration-specific terms in task goals.
# Kept deliberately narrow to avoid false positives — only terms that
# unambiguously signal integration work participate.
_INTEGRATION_TASK_RE = re.compile(
    r"\btriage\b"
    r"|\bunmerged\b"
    r"|\bpromot\w*\s+(?:branch|pr)\b"
    r"|\b(?:branch|pr)\s+promot\w*\b"
    r"|\bintegrat\w*\s+(?:debt|backlog)\b",
    re.IGNORECASE,
)


def is_integration_task(goal: str) -> bool:
    """Return ``True`` when *goal* describes integration work.

    Uses :data:`_INTEGRATION_TASK_RE` to detect integration-specific
    terms (triage, unmerged, promote branch/PR, integration debt/backlog).
    Pure function — zero SDK cost.
    """
    if not goal:
        return False
    return bool(_INTEGRATION_TASK_RE.search(goal))


def check_integration_cadence(
    recent_tasks: list[dict[str, object]],
    cadence: int = 5,
) -> str | None:
    """Return a directive when no recent task performed integration work.

    Windows the last *cadence* tasks and checks each goal via
    :func:`is_integration_task`. Returns ``None`` when the window is
    empty, *cadence* is ≤ 0, or any task in the window matches. When no
    match is found, returns a concrete directive string instructing
    branch triage and promotion.

    Pure function — no I/O, zero SDK cost.
    """
    if cadence <= 0 or not recent_tasks:
        return None
    window = recent_tasks[-cadence:]
    for t in window:
        goal = t.get("goal")
        if isinstance(goal, str) and is_integration_task(goal):
            return None
    return (
        "No recent task performed integration work. "
        "Triage unmerged successful branches and promote one "
        "via a source_task_id follow-up plan."
    )


# ---------------------------------------------------------------------------
# Gate-activation trace — JSONL recording of gate firings for measure_gate_triggers
# ---------------------------------------------------------------------------

# Filename used by :func:`log_gate_activation` and :func:`measure_gate_triggers`.
GATE_ACTIVATIONS_FILENAME = "gate_activations.jsonl"

# Shadow-ledger vocabulary for the mechanical-outcome-authority shadow mode
# (proposal 105). The writer — ``Agent._record_remediation_outcome`` — stamps
# one row per observed remediation record, ``diverged`` when the git-grounded
# mechanical verdict contradicts the recorded narration verdict and ``agree``
# when they match. The reader — :func:`measure_phantom_landing_rate` — tallies
# those rows into a divergence rate. Naming both tokens here keeps writer and
# reader from drifting apart.
MECHANICAL_OUTCOME_AUTHORITY_GATE = "mechanical_outcome_authority"
MECHANICAL_OUTCOME_DIVERGED_ACTION = "diverged"
MECHANICAL_OUTCOME_AGREE_ACTION = "agree"

# Divergence-trace vocabulary for the calibration-verdict-authority mode
# (proposal 129). The writer — ``Agent.contemplate`` — stamps one row per
# *measured* reflection verdict, ``diverged`` when the oracle's measured verdict
# contradicts the model's ``PRIOR VERDICT:`` self-grade and ``agree`` when they
# match, before adopting the measurement. Mirrors the mechanical-outcome-authority
# trace above so writer and reader read a single vocabulary.
CALIBRATION_VERDICT_AUTHORITY_GATE = "calibration_verdict_authority"
CALIBRATION_VERDICT_DIVERGED_ACTION = "diverged"
CALIBRATION_VERDICT_AGREE_ACTION = "agree"


@dataclass(frozen=True)
class GateActivation:
    """One gate firing event recorded to ``gate_activations.jsonl``.

    *gate* — canonical gate name (e.g. ``"audit_stall"``).
    *action* — what the gate did (e.g. ``"blocked"``).
    *timestamp* — ISO-8601 UTC timestamp.
    *detail* — human-readable context (bounded to ~200 chars by caller).
    """

    gate: str
    action: str
    timestamp: str
    detail: str


@runtime_checkable
class GateActivationBackend(Protocol):
    """The gate-activation trace persistence surface (proposal 106).

    Every gate firing in the autonomous loop is recorded as one row, and the
    behavioral sensors and CLI diagnostics read the trace back
    (:func:`measure_gate_triggers`, :func:`measure_directive_compliance`,
    :func:`measure_phantom_landing_rate`, :func:`measure_narration_conversion`,
    and the data-sufficiency instrument). So the trace can live in
    ``gate_activations.jsonl`` (default) or in ``state.db`` behind
    ``[paths] sqlite_state_enabled`` with no call-site branching. A pure append
    trace with no keyed-replace or retraction shape: one row per firing,
    append-only, oldest-first load. Named :meth:`append` / :meth:`read_all` (not
    ``record`` / ``load_all``) to match the writer's and readers' existing verbs.
    ``@runtime_checkable`` enables a structural ``isinstance`` check in the
    backend-selection tests.
    """

    def append(self, record: GateActivation) -> None:
        """Append exactly one gate-firing row."""
        ...

    def read_all(self) -> list[GateActivation]:
        """Return every recorded gate firing oldest-first."""
        ...

    def read_tail(self, limit: int) -> list[GateActivation]:
        """Return the newest *limit* gate firings oldest-first (bounded read).

        A caller that only needs a short tail of the append-only, never-rotated
        trace (the cost-risk release valve) uses this instead of
        :meth:`read_all` so it does not pay the O(history) hydration cost on
        every read. ``limit <= 0`` returns ``[]``; the result may hold fewer
        than *limit* rows on an empty or corrupt-tailed trace.
        """
        ...

    def read_gate_tail(self, gate: str, limit: int) -> list[GateActivation]:
        """Return the newest *limit* firings **of gate** *gate* oldest-first.

        The trace is shared by every phase (tend's cooldown-skip rows land every
        iteration), so a caller counting a run of one gate's own rows — the
        cost-risk release valve (#4071) — reads through this filter rather than
        :meth:`read_tail`, whose window other gates' rows would crowd out.
        ``limit <= 0`` returns ``[]``.
        """
        ...


def _parse_gate_activation_dict(
    data: dict[str, object], *, source: Path | None = None,
) -> GateActivation | None:
    """Coerce an already-decoded JSON row into a :class:`GateActivation`.

    Field-filters against ``{f.name for f in fields(GateActivation)}`` (so an
    unknown future field is dropped, not fatal) and returns ``None`` on any
    structural failure (``KeyError`` / ``TypeError`` / ``AttributeError``) — a
    row missing a required field is dropped, not raised. The ``json.loads`` that
    can raise ``JSONDecodeError`` lives in the shared reader
    :func:`load_all_gate_activations`, mirroring
    :func:`~maestro.memory._parse_question_resolution_dict`. *source*, when
    given, names the ledger in the warning.
    """
    known = {f.name for f in fields(GateActivation)}
    try:
        filtered = {k: v for k, v in data.items() if k in known}
        return GateActivation(**filtered)  # type: ignore[arg-type]
    except (KeyError, TypeError, AttributeError):
        logger.warning("Skipping corrupted gate_activation row in %s", source)
        return None


def load_all_gate_activations(data_dir: Path) -> list[GateActivation]:
    """Parse every well-formed row from ``gate_activations.jsonl`` oldest-first.

    The shared reader — the SQLite seed source and the JSONL adapter's
    :meth:`~JsonlGateActivationLedger.read_all` — carrying the corrupted-line
    tolerance the readers always had: it **skips corrupt rows and never raises**
    (a malformed line anywhere, interior or trailing, is dropped, not
    propagated), and returns ``[]`` on a missing file or an ``OSError``. Order is
    oldest-first, so ``read_all()[-n:]`` is the tail window the sensors take.
    """
    path = data_dir / GATE_ACTIVATIONS_FILENAME
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[GateActivation] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        record = _parse_gate_activation_dict(row, source=path)
        if record is not None:
            records.append(record)
    return records


def load_recent_gate_activations(
    data_dir: Path, limit: int,
) -> list[GateActivation]:
    """Parse only the newest *limit* well-formed rows, oldest-first.

    A bounded-cost tail read. Unlike :func:`load_all_gate_activations` — which
    hydrates *every* row of the append-only, never-rotated
    ``gate_activations.jsonl`` — this ``json``-parses only the last *limit*
    non-empty lines, so a caller needing a short tail (the cost-risk release
    valve) does not pay the O(history) hydration cost on each read. The bulk
    file read stays O(file), matching the sibling sensors, but the per-row
    parse/hydration drops to O(limit). Corruption-tolerance matches the full
    reader: corrupt lines inside the tail window are dropped (never raised), so
    the result may hold fewer than *limit* rows; a missing file or ``OSError``
    yields ``[]``, and ``limit <= 0`` yields ``[]``. Order is oldest-first,
    matching the tail :meth:`JsonlGateActivationLedger.read_all` returns.
    """
    if limit <= 0:
        return []
    path = data_dir / GATE_ACTIVATIONS_FILENAME
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[GateActivation] = []
    lines = [line for line in text.splitlines() if line.strip()]
    for line in lines[-limit:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        record = _parse_gate_activation_dict(row, source=path)
        if record is not None:
            records.append(record)
    return records


def load_recent_gate_activations_for_gate(
    data_dir: Path, gate: str, limit: int,
) -> list[GateActivation]:
    """Return the newest *limit* well-formed rows of *gate*, oldest-first.

    The gate-scoped sibling of :func:`load_recent_gate_activations`: walks the
    non-empty lines newest-first, keeps rows whose ``gate`` matches, and stops at
    *limit* matches (hydration is bounded by *limit*; the scan is proportional to
    the distance back to the *limit*-th match — a cheap substring pre-filter
    skips ``json.loads`` on lines that cannot name *gate*), so rows of other gates interleaved in the shared trace are
    skipped rather than filling the window (#4071). Same corruption tolerance as
    the unscoped reader; a missing file, ``OSError``, or ``limit <= 0`` yields
    ``[]``.
    """
    if limit <= 0:
        return []
    path = data_dir / GATE_ACTIVATIONS_FILENAME
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[GateActivation] = []
    for line in reversed(text.splitlines()):
        if not line.strip() or gate not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("gate") != gate:
            continue
        record = _parse_gate_activation_dict(row, source=path)
        if record is None:
            continue
        records.append(record)
        if len(records) >= limit:
            break
    records.reverse()
    return records


class JsonlGateActivationLedger:
    """JSONL-backed :class:`GateActivationBackend` — the default, flag-off store.

    Binds a ``data_dir`` and keeps the ledger on ``gate_activations.jsonl``
    (behaviour-preserving). :meth:`append` reproduces the exact pre-fold write —
    an advisory :func:`~maestro.fileutil.file_lock` around an
    ``open(path, "a", encoding="utf-8")`` append of
    ``json.dumps(asdict(record)) + "\\n"`` (parent-dir-created) — so the JSONL
    substrate is bit-for-bit unchanged, and concurrent appends never interleave
    partial lines. :meth:`read_all` reuses the corrupt-tolerant forward parse via
    :func:`load_all_gate_activations`.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._history_path = data_dir / GATE_ACTIVATIONS_FILENAME

    def append(self, record: GateActivation) -> None:
        self._history_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            file_lock(self._history_path),
            open(self._history_path, "a", encoding="utf-8") as f,
        ):
            f.write(json.dumps(asdict(record)) + "\n")

    def read_all(self) -> list[GateActivation]:
        return load_all_gate_activations(self._data_dir)

    def read_tail(self, limit: int) -> list[GateActivation]:
        return load_recent_gate_activations(self._data_dir, limit)

    def read_gate_tail(self, gate: str, limit: int) -> list[GateActivation]:
        return load_recent_gate_activations_for_gate(self._data_dir, gate, limit)


def log_gate_activation(
    backend: GateActivationBackend,
    gate: str,
    action: str,
    detail: str,
) -> None:
    """Stamp one gate firing with the UTC timestamp and append it via *backend*.

    Builds a :class:`GateActivation` (truncating *detail* to 200 characters to
    keep records compact) and delegates persistence to *backend* — the
    flag-selected :class:`JsonlGateActivationLedger` (default) or
    :class:`~maestro.state_store.SqliteGateActivationLedger`. The JSONL adapter
    keeps the historical ``file_lock`` append; the SQLite adapter serialises
    through its own transaction.
    """
    record = GateActivation(
        gate=gate,
        action=action,
        timestamp=datetime.now(tz=UTC).isoformat(),
        detail=detail[:200],
    )
    backend.append(record)


def measure_gate_triggers(
    backend: GateActivationBackend,
    n: int = 100,
) -> dict[str, int]:
    """Count per-gate activations from the tail of the gate-activation trace.

    Reads the last *n* rows via ``backend.read_all()`` and returns a
    ``{gate_name: count}`` dict. Returns an empty dict when the trace is empty.
    Structurally-corrupt rows are already dropped by the backend's read
    (corrupted-tail tolerance matching :meth:`Memory.get_recent_tasks`); a row
    whose ``gate`` is empty or non-string is skipped here.

    The *n*-window now bounds the last *n* well-formed rows (``read_all()``
    already drops corrupt rows), not the last *n* raw lines the pre-fold reader
    tailed before parsing — identical on a clean store, and on a crash-torn tail
    at most a few dropped-row slots differ. Advisory sensor; behaviour-preserving
    in the common case (proposal 106).
    """
    records = backend.read_all()
    tail = records[-n:] if n > 0 else []
    counts: Counter[str] = Counter()
    for record in tail:
        if isinstance(record.gate, str) and record.gate:
            counts[record.gate] += 1
    return dict(counts)

# ---------------------------------------------------------------------------
# Plan-mill instrument (proposal 116, increment 1) — zero-SDK readers that
# quantify how self-referential and how unproductive the evolution generator
# currently is over a window of recently-created plans. Pure and read-only:
# no loop behaviour changes, no prompt/gate/flag. Later increments are only
# justified if these numbers show the mill shrinking.
# ---------------------------------------------------------------------------

# Compiled regex matching the self-measurement theme vocabulary the mill keeps
# re-minting plans about. Word-boundary anchored (following the
# :data:`_INSTRUMENT_KEYWORDS_RE` idiom) so "sensor" matches but "sensory"
# does not; an optional trailing ``s`` tolerates the common plural forms the
# production titles use ("sensors", "discriminators"). Includes the existing
# instrument family (``metric``/``measurement``).
_SELF_MEASUREMENT_THEME_RE = re.compile(
    r"\b(?:sensors?|decoys?|monoscores?|discriminators?|metrics?"
    r"|measurements?|audit[- ]rules?|servicing[- ]laps?|net[- ]signs?)\b",
    re.IGNORECASE,
)

# Compiled regex matching a plan title that promotes/lands a stranded task
# branch. Calibrated against the observed production titles
# ("Finish the stranded-branch sensor lineage",
# "Promote task branch maestro/task/… into main") — a promotion verb followed
# by a stranded/task-branch/into-main object. Word-boundary anchored so
# ``land`` matches but ``landing`` does not.
#
# The ``.*`` requires the verb *before* the object, so an object-first title
# ("Stranded branch X: promote to main") is intentionally NOT matched. Every
# observed production title is verb-first; keeping the ordering tight caps
# recall by design rather than over-matching titles that merely mention a
# promotion verb and a branch object out of order. Broaden to order-independent
# lookaheads only if production surfaces genuine object-first promotion titles.
_STRANDED_PROMOTION_RE = re.compile(
    r"\b(?:promote|land|finish)\b.*"
    r"\b(?:stranded|task[- ]branch|into[- ]main)\b",
    re.IGNORECASE,
)


def classify_self_measurement_plan(plan: EvolutionPlan) -> bool:
    """Return ``True`` when *plan* is about the self-measurement machinery.

    Matches :data:`_SELF_MEASUREMENT_THEME_RE` against the plan's ``title``
    and ``rationale`` (searched together). Pure function — zero SDK cost.
    """
    text = f"{plan.title}\n{plan.rationale}"
    return bool(_SELF_MEASUREMENT_THEME_RE.search(text))


def _recent_plan_window(
    plans: list[EvolutionPlan], n: int,
) -> list[EvolutionPlan]:
    """Return the *n* plans with the most-recent ``created_at`` (all when n<=0).

    ``evolution_plans.jsonl`` is rewritten whole by ``_save_all_plans`` so file
    order is not creation order; sort by the ISO ``created_at`` string (lexical
    order is chronological for ISO-8601, empty/missing strings rank oldest) and
    take the tail. The ``or ""`` guard follows the codebase idiom (a legacy or
    malformed row may carry a ``None`` ``created_at``) so a null timestamp ranks
    oldest instead of raising ``TypeError`` mid-sort. ``n <= 0`` operates over
    every plan without sorting — every metric below is an order-independent
    count, so the sort is only needed to pick a bounded most-recent window.
    Pure function — zero SDK cost.
    """
    if n <= 0:
        return list(plans)
    return sorted(plans, key=lambda p: p.created_at or "")[-n:]


def measure_plan_theme_saturation(
    plans: list[EvolutionPlan], n: int = 0,
) -> dict[str, object]:
    """Fraction of recently-created plans matching the self-measurement theme.

    Over the *n*-plan most-recent window (all plans when ``n <= 0``), counts
    how many match :func:`classify_self_measurement_plan`. Returns::

        {"total": int, "themed": int, "themed_fraction": float}

    ``themed_fraction`` is ``0.0`` when the window is empty. Pure — zero SDK.
    """
    window = _recent_plan_window(plans, n)
    total = len(window)
    themed = sum(1 for p in window if classify_self_measurement_plan(p))
    return {
        "total": total,
        "themed": themed,
        "themed_fraction": themed / total if total > 0 else 0.0,
    }


def measure_merged_to_created_ratio(
    plans: list[EvolutionPlan], n: int = 0,
) -> dict[str, object]:
    """How many recently-created plans actually reached ``main`` (``merged``).

    Over the *n*-plan most-recent window (all plans when ``n <= 0``), counts
    ``status == "merged"`` against the window total. Returns::

        {"created": int, "merged": int, "ratio": float}

    ``ratio`` is ``0.0`` when the window is empty. Pure — zero SDK cost.
    """
    window = _recent_plan_window(plans, n)
    created = len(window)
    merged = sum(1 for p in window if p.status == "merged")
    return {
        "created": created,
        "merged": merged,
        "ratio": merged / created if created > 0 else 0.0,
    }


def measure_task_coupled_plan_fraction(
    plans: list[EvolutionPlan], n: int = 0,
) -> dict[str, object]:
    """Fraction of recently-created plans that nominate a ``source_task_id``.

    Over the *n*-plan most-recent window (all plans when ``n <= 0``), counts
    plans basing on a kept task branch (a non-empty ``source_task_id``) rather
    than fresh from ``main``. Returns::

        {"total": int, "coupled": int, "coupled_fraction": float}

    ``coupled_fraction`` is ``0.0`` when the window is empty. Pure — zero SDK.
    """
    window = _recent_plan_window(plans, n)
    total = len(window)
    coupled = sum(
        1 for p in window
        if isinstance(p.source_task_id, str) and p.source_task_id
    )
    return {
        "total": total,
        "coupled": coupled,
        "coupled_fraction": coupled / total if total > 0 else 0.0,
    }


# Compiled regex matching the two persisted submission-door signatures a
# freshly-minted plan carries in ``obsolete_reason`` when it dies at the door.
# ``submit_for_review`` in ``evolution_submission.py`` records a net-new plan ``obsolete``
# with one of two reasons: the generation-admission gate ("Admission gate refused
# net-new mint: … backlog saturated (…) …") and the older backlog submit-block
# ("… backlog saturated: … plans await review …"). Both authored phrasings carry
# one of these substrings, so matching either (case-insensitive) marks the plan
# dead on arrival — obsoleted at the door after the priced ``propose_plan``
# call already ran.
_DEAD_ON_ARRIVAL_REASON_RE = re.compile(
    r"admission gate refused net-new mint|backlog saturated",
    re.IGNORECASE,
)


# Compiled regex matching the ``[ungrounded:<category>]`` prefix that
# ``submit_for_review``'s plan-grounding guard (``evolution_submission.py``) stamps into a
# net-new plan's ``obsolete_reason`` when a fresh reading of the located tree
# refutes the plan's premise probe. The captured group is the category slug
# (e.g. ``duplicate`` / ``no-such-defect``), so a single ``match`` both marks the
# plan ungrounded and yields its per-category tally.
_UNGROUNDED_REASON_RE = re.compile(r"^\[ungrounded:([^\]]+)\]")

# The remaining grounding-family door-refusal signatures, named so
# :data:`_DOOR_REFUSAL_REASON_TABLE` and :func:`measure_planning_grounding`'s
# per-tag split read one phrasing each rather than forking them: proposal 136's
# generator reality-grounding guard, then the shipped-CLI and evidence-CLI
# grounding guards.
_UNGROUNDED_GENERATION_REASON_RE = re.compile(r"^\[ungrounded-generation:")
_CLI_SHIPPED_REASON_RE = re.compile(r"^Proposes only already-shipped CLI surface")
_CLI_EVIDENCE_REASON_RE = re.compile(r"^Cites unshipped CLI evidence")
# The flag-graduation grounding guard — refuses a self-initiated plan proposing
# to graduate an experimental flag before its review_by window closes.
_FLAG_GRADUATION_REASON_RE = re.compile(r"^\[flag-graduation-premature:")

# Trailing planner-read attribution tag ``submit_for_review`` appends to an
# ``[ungrounded:<category>]`` reason when the planner had the tree surface
# (proposal 143 step 5); built from the owner's literals so both sides agree.
_PLANNER_TAG_RE = re.compile(
    rf"(?:{re.escape(PLANNER_READ_TAG)}|{re.escape(PLANNER_UNREAD_TAG)})$",
)


# Ordered door-refusal reason table (proposal 136). Each arm keys on a phrasing
# ``EvolutionEngine.submit_for_review`` persists through
# ``_record_obsolete_duplicate`` — trace a reworded reason back to its author:
#
# - ``saturation``: the generation-admission gate ("Admission gate refused
#   net-new mint: …") and the backlog submit-block ("… backlog saturated: …") —
#   exactly :data:`_DEAD_ON_ARRIVAL_REASON_RE`, so the saturation count stays
#   the proposal-120 baseline;
# - ``grounding``: the plan-grounding guard (``[ungrounded:<category>]``), the
#   generator reality-grounding guard (``[ungrounded-generation:<reason>]``), the
#   shipped-CLI grounding guard ("Proposes only already-shipped CLI surface …")
#   and the evidence-CLI grounding guard ("Cites unshipped CLI evidence …");
# - ``restatement``: the pairwise duplicate-pending guard ("Near-duplicate of
#   …"), the themed-restatement cluster guard ("Themed restatement of …"), the
#   theme-cluster cap ("Theme-clusters with …") and the recited-source-task guard
#   ("Re-cites source branch …");
# - ``value``: the retired value-claim admission arm (``[value-refused:<reason>]``),
#   still read on historical rows — deliberately outside :data:`_CONTENT_DEATH_REASONS` and the
#   saturation baseline, so both keep their meaning.
# - ``flag-graduation``: the flag-graduation grounding guard
#   (``[flag-graduation-premature:<flag>]``) — a self-initiated plan proposing to
#   graduate an experimental flag before its review_by window closes.
#
# Every non-saturation arm is ``^``-anchored: each phrasing is authored at the
# start of the reason, so a free-text LLM prune reason (e.g. a
# ``curate_pending_plans`` obsolete verdict) that merely *mentions* one
# mid-sentence is not miscounted as a door death.
#: Reason prefix of a gap plan the rejection cooldown deferred (proposal 155).
DEFERRED_COOLDOWN_TAG = "[deferred:cooldown:"
_DOOR_REFUSAL_REASON_TABLE: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    ("saturation", (_DEAD_ON_ARRIVAL_REASON_RE,)),
    ("grounding", (
        _UNGROUNDED_REASON_RE,
        _UNGROUNDED_GENERATION_REASON_RE,
        _CLI_SHIPPED_REASON_RE,
        _CLI_EVIDENCE_REASON_RE,
    )),
    ("restatement", (
        re.compile(r"^Near-duplicate of"),
        re.compile(r"^Themed restatement of"),
        re.compile(r"^Theme-clusters with"),
        re.compile(r"^Re-cites source branch"),
    )),
    ("value", (re.compile(r"^\[value-refused:"),)),
    ("flag-graduation", (_FLAG_GRADUATION_REASON_RE,)),
    ("cooldown", (re.compile("^" + re.escape(DEFERRED_COOLDOWN_TAG)),)),
)

# The two door-refusal families counted as content death (the plan's substance,
# not the backlog's fullness, killed it).
_CONTENT_DEATH_REASONS = frozenset({"grounding", "restatement"})


def classify_door_refusal_reason(reason: str) -> str | None:
    """Return the door-refusal family of an ``obsolete_reason``, or ``None``.

    Walks :data:`_DOOR_REFUSAL_REASON_TABLE` in order and returns the first
    family (``"saturation"`` / ``"grounding"`` / ``"restatement"`` / ``"value"`` /
    ``"flag-graduation"`` / ``"cooldown"``) with a
    pattern that ``search``-es *reason*. ``None`` means not a submission-door
    refusal — e.g. a ``mark_plan_obsolete`` backlog-sweep prune. Pure — zero SDK.
    """
    for family, patterns in _DOOR_REFUSAL_REASON_TABLE:
        if any(pattern.search(reason) for pattern in patterns):
            return family
    return None


def classify_plan_mint_channel(plan: EvolutionPlan) -> str:
    """Return the mint provenance inferred from *plan*'s persisted record.

    - ``"phase-slice"`` when ``parent_plan_id`` is set — a plan minted as a slice
      of a parent proposal;
    - ``"task-anchored"`` when ``source_task_id`` is set — based on a kept task
      branch rather than fresh from ``main``;
    - ``"evolve"`` otherwise — the standalone fuel-gated ``propose_plan`` mint that
      is the dominant production case (``evolve()`` is the sole ``propose_plan``
      caller today, so essentially every self-initiated mint lands here).

    ``parent_plan_id`` wins over ``source_task_id`` because a phase-slice is the
    more specific provenance. Pure function — zero SDK cost.
    """
    if isinstance(plan.parent_plan_id, str) and plan.parent_plan_id:
        return "phase-slice"
    if isinstance(plan.source_task_id, str) and plan.source_task_id:
        return "task-anchored"
    return "evolve"


def measure_dead_on_arrival_mints(
    plans: list[EvolutionPlan], n: int = 0,
) -> dict[str, object]:
    """How many recently-created plans died at the submission door.

    Over the *n*-plan most-recent window (all plans when ``n <= 0``), counts the
    plans recorded ``status == "obsolete"`` with an ``obsolete_reason`` matching a
    submission-door signature (:data:`_DEAD_ON_ARRIVAL_REASON_RE`) — a plan the
    priced ``propose_plan`` call authored only for the door to
    discard within minutes. Returns::

        {"total": int, "dead_on_arrival": int, "doa_fraction": float,
         "by_channel": dict[str, int], "by_reason": dict[str, int],
         "content_dead": int, "content_fraction": float}

    ``by_channel`` tallies the dead-on-arrival plans by
    :func:`classify_plan_mint_channel` provenance; ``doa_fraction`` is guarded as
    ``dead_on_arrival / total if total > 0 else 0.0``. Those four keys count
    saturation only (the proposal-120 baseline). Additively (proposal 136),
    ``by_reason`` tallies every obsolete window plan by
    :func:`classify_door_refusal_reason` family, and ``content_dead`` /
    ``content_fraction`` count the grounding + restatement deaths, guarded the
    same way. Pure — zero SDK cost, read-only.
    """
    window = _recent_plan_window(plans, n)
    total = len(window)
    dead = [
        p for p in window
        if p.status == "obsolete"
        and isinstance(p.obsolete_reason, str)
        and _DEAD_ON_ARRIVAL_REASON_RE.search(p.obsolete_reason)
    ]
    by_channel: dict[str, int] = {}
    for p in dead:
        channel = classify_plan_mint_channel(p)
        by_channel[channel] = by_channel.get(channel, 0) + 1
    by_reason: dict[str, int] = {}
    for p in window:
        if p.status != "obsolete" or not isinstance(p.obsolete_reason, str):
            continue
        family = classify_door_refusal_reason(p.obsolete_reason)
        if family is not None:
            by_reason[family] = by_reason.get(family, 0) + 1
    content_dead = sum(
        count for family, count in by_reason.items()
        if family in _CONTENT_DEATH_REASONS
    )
    dead_on_arrival = len(dead)
    return {
        "total": total,
        "dead_on_arrival": dead_on_arrival,
        "doa_fraction": dead_on_arrival / total if total > 0 else 0.0,
        "by_channel": by_channel,
        "by_reason": by_reason,
        "content_dead": content_dead,
        "content_fraction": content_dead / total if total > 0 else 0.0,
    }


def format_dead_on_arrival_mints_summary(
    plans: list[EvolutionPlan], n: int = 0,
) -> str:
    """Render a ``DEAD-ON-ARRIVAL-MINTS`` line, or ``""`` when none died.

    Delegates to :func:`measure_dead_on_arrival_mints` over the *n*-plan window
    and, when at least one recently-minted plan was obsoleted at the submission
    door, returns a single line reporting the count, its fraction of the window,
    and the per-channel provenance breakdown. Returns ``""`` when
    ``dead_on_arrival <= 0``, so the caller injects the line only when there is
    waste to account for — self-extinguishing the moment no recent plan dies at
    the door, mirroring
    :func:`~maestro.remediation_tracker.format_lap_delivery_split` and
    :func:`~maestro.frame_discharge.format_measurement_costume_summary`. Pure
    formatting — zero SDK cost, measurement-only.
    """
    report = measure_dead_on_arrival_mints(plans, n)
    dead_on_arrival: int = report["dead_on_arrival"]  # type: ignore[assignment]
    if dead_on_arrival <= 0:
        return ""
    total: int = report["total"]  # type: ignore[assignment]
    fraction: float = report["doa_fraction"]  # type: ignore[assignment]
    by_channel: dict[str, int] = report["by_channel"]  # type: ignore[assignment]
    channels = ", ".join(
        f"{channel}={count}" for channel, count in sorted(by_channel.items())
    )
    return (
        f"DEAD-ON-ARRIVAL-MINTS: {dead_on_arrival}/{total} recently-minted plans "
        f"({fraction:.0%}) were obsoleted at the submission door's "
        f"backlog-saturation / admission gate "
        f"(by channel: {channels}) — priced propose_plan calls spent on plans "
        f"the door discarded within minutes; move the keep/discard decision "
        f"before the spend"
    )


# Compiled regexes matching a human ``rejection_reason`` that a single read of
# the source tree would have settled (proposals 136/143). The two named
# sub-patterns are the breakdown ``measure_rejection_classes`` reports: the
# plan's end state already exists, or its premise is false / names a module,
# symbol, or path that does not exist or lives elsewhere.
# ``_TREE_CHECKABLE_REJECTION_RE`` is their union, derived rather than
# hand-maintained, so a reason is tree-checkable iff it lands in a sub-class;
# no other reader forks the vocabulary. Coarse lexical proxies: the trend
# across windows is the signal, not the accuracy of any single match.
_ALREADY_SHIPPED_REJECTION_RE = re.compile(
    r"already\s+(?:implemented|exists?|ships|shipped|registered|wired|landed|has"
    r"|produces|scopes|does|refuses|mechanically|in\s+(?:the\s+)?tree)"
    r"|exists\s+already",
    re.IGNORECASE,
)
_FALSE_PREMISE_REJECTION_RE = re.compile(
    r"false\s+premise|premise\s+is\s+(?:false|wrong)|misread|does\s+not\s+exist"
    r"|doesn[\u2019']?t\s+exist|no\s+such|not\s+true"
    r"|wrong\s+(?:module|file|seam|path)|lives\s+in|phantom|nonexist",
    re.IGNORECASE,
)
_TREE_CHECKABLE_REJECTION_RE = re.compile(
    _ALREADY_SHIPPED_REJECTION_RE.pattern + "|" + _FALSE_PREMISE_REJECTION_RE.pattern,
    re.IGNORECASE,
)
_NOT_NEEDED_REJECTION_RE = re.compile(
    r"not\s+needed|unnecessary|no\s+need", re.IGNORECASE,
)


def measure_rejection_classes(
    records: list[RejectionRecord], n: int = 0,
) -> dict[str, object]:
    """Count human rejections in the grounding-failure classes.

    Over the *n* most recent *records* (all when ``n <= 0``; ledger order is
    oldest-first), a record counts toward every class it matches — rejections
    are compound — and toward ``any_grounding_class`` once:

    - ``already_shipped``: :data:`_ALREADY_SHIPPED_REJECTION_RE` on the reason;
    - ``false_premise``: :data:`_FALSE_PREMISE_REJECTION_RE` on the reason;
    - ``not_needed``: ``rejection_category == "not_needed"`` or
      :data:`_NOT_NEEDED_REJECTION_RE` on the reason.

    ``tree_checkable`` counts the window's reasons
    :func:`is_tree_checkable_rejection` (proposal 143's one tree-checkable
    definition) accepts. Its regex is the union of the ``already_shipped`` and
    ``false_premise`` sub-patterns, so a reason is tree-checkable exactly when
    it lands in one of those two classes — a breakdown, not a second
    definition; ``not_needed`` stays outside it. Returns ``{"total",
    "by_class", "any_grounding_class", "any_grounding_fraction",
    "tree_checkable"}``, the fraction guarded against an empty window. A coarse lexical proxy — pure, zero SDK cost, read-only.
    """
    window = records[-n:] if n > 0 else list(records)
    by_class = {"already_shipped": 0, "false_premise": 0, "not_needed": 0}
    any_class = 0
    tree_checkable = 0
    for record in window:
        reason = record.rejection_reason or ""
        if is_tree_checkable_rejection(record.rejection_reason):
            tree_checkable += 1
        matched = {
            "already_shipped": bool(_ALREADY_SHIPPED_REJECTION_RE.search(reason)),
            "false_premise": bool(_FALSE_PREMISE_REJECTION_RE.search(reason)),
            "not_needed": (
                record.rejection_category == "not_needed"
                or bool(_NOT_NEEDED_REJECTION_RE.search(reason))
            ),
        }
        for name, hit in matched.items():
            if hit:
                by_class[name] += 1
        if any(matched.values()):
            any_class += 1
    total = len(window)
    return {
        "total": total,
        "by_class": by_class,
        "any_grounding_class": any_class,
        "any_grounding_fraction": any_class / total if total > 0 else 0.0,
        "tree_checkable": tree_checkable,
    }


def measure_ungrounded_premise_rate(
    plans: list[EvolutionPlan], n: int = 0,
) -> dict[str, object]:
    """What fraction of recent premise-carrying plans the tree refuted.

    Over the *n*-plan most-recent window (all plans when ``n <= 0``), takes as
    denominator the *probe-carrying* plans — those staking a checkable premise
    (``plan.grounding is not None``) — and as numerator those additionally
    recorded ``status == "obsolete"`` with an ``obsolete_reason`` matching the
    ``[ungrounded:<category>]`` signature (:data:`_UNGROUNDED_REASON_RE`) that
    ``submit_for_review``'s grounding guard stamps when a fresh reading of the
    located tree refutes the premise. Returns::

        {"probe_carrying": int, "ungrounded": int, "ungrounded_fraction": float,
         "by_category": dict[str, int]}

    ``by_category`` tallies the ungrounded plans by the category slug parsed from
    the reason prefix; ``ungrounded_fraction`` is guarded as
    ``ungrounded / probe_carrying if probe_carrying else 0.0``. Pure — zero SDK
    cost, read-only over the obsolete rows the live guard already writes.
    """
    window = _recent_plan_window(plans, n)
    probe_carrying = [p for p in window if p.grounding is not None]
    by_category: dict[str, int] = {}
    ungrounded = 0
    for p in probe_carrying:
        if p.status != "obsolete" or not isinstance(p.obsolete_reason, str):
            continue
        match = _UNGROUNDED_REASON_RE.match(p.obsolete_reason)
        if match is None:
            continue
        ungrounded += 1
        category = match.group(1)
        by_category[category] = by_category.get(category, 0) + 1
    probe_count = len(probe_carrying)
    return {
        "probe_carrying": probe_count,
        "ungrounded": ungrounded,
        "ungrounded_fraction": ungrounded / probe_count if probe_count else 0.0,
        "by_category": by_category,
    }


def format_ungrounded_rate_summary(
    plans: list[EvolutionPlan], n: int = 0,
) -> str:
    """Render an ``UNGROUNDED-RATE`` line, or ``""`` when none was refuted.

    Delegates to :func:`measure_ungrounded_premise_rate` over the *n*-plan window
    and, when at least one recently-minted premise-carrying plan was refused
    because a fresh reading of the tree refuted its premise, returns a single line
    reporting the count, its fraction of the probe-carrying plans, and the
    per-category breakdown. Returns ``""`` when ``ungrounded <= 0``, so the caller
    injects the line only when a premise actually failed the fresh read —
    self-extinguishing the moment no recent premise is refuted, mirroring
    :func:`format_dead_on_arrival_mints_summary`. Pure formatting — zero SDK cost,
    measurement-only.
    """
    report = measure_ungrounded_premise_rate(plans, n)
    ungrounded: int = report["ungrounded"]  # type: ignore[assignment]
    if ungrounded <= 0:
        return ""
    probe_carrying: int = report["probe_carrying"]  # type: ignore[assignment]
    fraction: float = report["ungrounded_fraction"]  # type: ignore[assignment]
    by_category: dict[str, int] = report["by_category"]  # type: ignore[assignment]
    categories = ", ".join(
        f"{category}={count}" for category, count in sorted(by_category.items())
    )
    return (
        f"UNGROUNDED-RATE: {ungrounded}/{probe_carrying} recent premise-carrying "
        f"plans ({fraction:.0%}) were refused because a fresh reading of the tree "
        f"refuted their premise (by category: {categories}) — plan generation "
        f"premised on facts the codebase contradicts; ground the premise against "
        f"disk before staking a plan on it"
    )


def is_tree_checkable_rejection(reason: str | None) -> bool:
    """True when a human rejection *reason* is one a tree read would settle.

    The single definition of the tree-checkable rejection class
    (:data:`_TREE_CHECKABLE_REJECTION_RE`). A non-``str`` or empty reason is
    ``False``. Pure — zero SDK cost.
    """
    if not isinstance(reason, str) or not reason:
        return False
    return _TREE_CHECKABLE_REJECTION_RE.search(reason) is not None


# ``rejection_reason`` prefix the loop stamps when the host closed a PR
# unmerged — a mechanical rejection, not a human plan-review decision, so
# :func:`measure_planning_grounding` keeps it out of its human-rejection counts.
_HOST_CLOSED_REJECTION_PREFIX = "PR closed without merging"


def _normalise_tree_path(path: str) -> str:
    """Strip a leading ``./`` so ``./maestro/x.py`` and ``maestro/x.py`` agree."""
    return path[2:] if path.startswith("./") else path


#: The :func:`measure_planning_grounding` ``refusals_by_tag`` buckets, in the
#: fixed order the CLI renders them.
PLANNING_REFUSAL_TAGS: tuple[str, ...] = (
    "ungrounded:read", "ungrounded:unread", "ungrounded:untagged",
    "ungrounded-generation", "cli-shipped", "cli-evidence",
)


def _planning_refusal_tag(reason: str) -> str | None:
    """Return the :data:`PLANNING_REFUSAL_TAGS` bucket of *reason*, or ``None``."""
    if _UNGROUNDED_REASON_RE.match(reason):
        tag = _PLANNER_TAG_RE.search(reason)
        if tag is None:
            return "ungrounded:untagged"
        if tag.group(0) == PLANNER_READ_TAG:
            return "ungrounded:read"
        return "ungrounded:unread"
    if _UNGROUNDED_GENERATION_REASON_RE.match(reason):
        return "ungrounded-generation"
    if _CLI_SHIPPED_REASON_RE.match(reason):
        return "cli-shipped"
    if _CLI_EVIDENCE_REASON_RE.match(reason):
        return "cli-evidence"
    return None


def measure_planning_grounding(
    plans: list[EvolutionPlan], since: str | None = None,
) -> dict[str, object]:
    """Tree-checkable share of human rejections, plus planner read coverage.

    The window is every plan whose ``created_at`` is at or after *since* (a
    lexical ISO comparison; a ``None``/empty ``created_at`` is excluded when
    *since* is set), or every plan when *since* is ``None``/empty. Returns::

        {"decided": int, "rejected": int, "tree_checkable": int,
         "tree_checkable_fraction": float, "door_refusals": int,
         "refusals_by_tag": dict[str, int],
         "with_reads": int, "no_reads_recorded": int,
         "read_coverage": float | None}

    ``decided`` counts ``merged`` + human ``rejected`` — a rejection the loop
    stamped because the host closed the PR unmerged
    (:data:`_HOST_CLOSED_REJECTION_PREFIX`) is mechanical and excluded, while a
    reasonless human rejection still counts; ``tree_checkable`` counts the
    rejected plans whose ``rejection_reason`` passes
    :func:`is_tree_checkable_rejection`; ``door_refusals`` counts ``obsolete``
    plans carrying the ``[ungrounded:...]`` door signature. ``refusals_by_tag``
    splits the window's grounding-family machine refusals by reason tag —
    ``ungrounded:read`` / ``ungrounded:unread`` / ``ungrounded:untagged`` (the
    trailing ``[planner-read]`` / ``[planner-unread]`` attribution, absent when
    the planner ran blind or the probe was ``SYMBOL_ABSENT``),
    ``ungrounded-generation``, ``cli-shipped`` and ``cli-evidence`` — every key
    always present, so a fall in tree-checkable rejections can be credited to
    the planner reading versus an upstream clearance. ``read_coverage`` is
    the mean, over plans with ``grounded_reads`` recorded and a non-empty
    ``files_affected``, of the share of ``files_affected`` the planner read —
    ``None`` when no plan qualifies, which is "no reads recorded", not zero
    coverage. Pure — zero SDK cost, no I/O.
    """
    if since:
        window = [p for p in plans if (p.created_at or "") and p.created_at >= since]
    else:
        window = list(plans)
    rejected = [
        p for p in window
        if p.status == "rejected"
        and not (p.rejection_reason or "").startswith(_HOST_CLOSED_REJECTION_PREFIX)
    ]
    merged = sum(1 for p in window if p.status == "merged")
    tree_checkable = sum(
        1 for p in rejected if is_tree_checkable_rejection(p.rejection_reason)
    )
    door_refusals = sum(
        1 for p in window
        if p.status == "obsolete"
        and isinstance(p.obsolete_reason, str)
        and _UNGROUNDED_REASON_RE.match(p.obsolete_reason)
    )
    refusals_by_tag = dict.fromkeys(PLANNING_REFUSAL_TAGS, 0)
    for p in window:
        if p.status != "obsolete" or not isinstance(p.obsolete_reason, str):
            continue
        tag = _planning_refusal_tag(p.obsolete_reason)
        if tag is not None:
            refusals_by_tag[tag] += 1
    with_reads = [p for p in window if p.grounded_reads is not None]
    coverages: list[float] = []
    for p in with_reads:
        affected = {_normalise_tree_path(f) for f in p.files_affected}
        if not affected:
            continue
        read = {_normalise_tree_path(r) for r in p.grounded_reads or []}
        coverages.append(len(affected & read) / len(affected))
    rejected_count = len(rejected)
    return {
        "decided": merged + rejected_count,
        "rejected": rejected_count,
        "tree_checkable": tree_checkable,
        "tree_checkable_fraction": (
            tree_checkable / rejected_count if rejected_count else 0.0
        ),
        "door_refusals": door_refusals,
        "refusals_by_tag": refusals_by_tag,
        "with_reads": len(with_reads),
        "no_reads_recorded": len(window) - len(with_reads),
        "read_coverage": sum(coverages) / len(coverages) if coverages else None,
    }


def measure_stranded_promotion_plans(
    plans: list[EvolutionPlan], n: int = 0,
) -> dict[str, object]:
    """Count recently-created plans whose title promotes a stranded branch.

    Over the *n*-plan most-recent window (all plans when ``n <= 0``), matches
    :data:`_STRANDED_PROMOTION_RE` against each ``title``. Returns::

        {"count": int, "plan_ids": list[str]}

    ``plan_ids`` are the matching plans' ids (empty when none). Pure — zero SDK.
    """
    window = _recent_plan_window(plans, n)
    plan_ids = [
        p.plan_id for p in window if _STRANDED_PROMOTION_RE.search(p.title)
    ]
    return {"count": len(plan_ids), "plan_ids": plan_ids}



# ---------------------------------------------------------------------------
# Criteria verifiability heuristic — whole-word marker check
# ---------------------------------------------------------------------------

# Frozen set of ~75 state-change verbs (with inflections) and deliverable
# nouns.  Used by :func:`_check_criteria_verifiability` to detect whether
# acceptance criteria contain at least one mechanically verifiable marker.
# Checked via ``set(criterion.lower().split()) & _VERIFIABLE_MARKERS`` —
# whole-word matching so inflected forms like "testified" do not match
# "test".
_VERIFIABLE_MARKERS: frozenset[str] = frozenset({
    # State-change verbs — base, 3rd-person, and past-tense inflections.
    "add", "adds", "added",
    "append", "appends", "appended",
    "block", "blocks", "blocked",
    "build", "builds", "built",
    "cap", "caps", "capped",
    "count", "counts", "counted",
    "create", "creates", "created",
    "delete", "deletes", "deleted",
    "drop", "drops", "dropped",
    "emit", "emits", "emitted",
    "fail", "fails", "failed",
    "filter", "filters", "filtered",
    "fix", "fixes", "fixed",
    "gate", "gates", "gated",
    "implement", "implements", "implemented",
    "inject", "injects", "injected",
    "insert", "inserts", "inserted",
    "introduce", "introduces", "introduced",
    "log", "logs", "logged",
    "merge", "merges", "merged",
    "modify", "modifies", "modified",
    "move", "moves", "moved",
    "pass", "passes", "passed",
    "persist", "persists", "persisted",
    "raise", "raises", "raised",
    "record", "records", "recorded",
    "refactor", "refactors", "refactored",
    "reject", "rejects", "rejected",
    "remove", "removes", "removed",
    "rename", "renames", "renamed",
    "replace", "replaces", "replaced",
    "return", "returns", "returned",
    "skip", "skips", "skipped",
    "suppress", "suppresses", "suppressed",
    "update", "updates", "updated",
    "validate", "validates", "validated",
    "wire", "wires", "wired",
    "write", "writes", "written",
    # Deliverable nouns — concrete artifacts a criterion can reference.
    "file", "test", "function", "module", "class",
    "branch", "commit", "field", "entry",
    "constant", "directory", "path", "schema", "report",
    "method",
})


def _check_criteria_verifiability(criteria: list[str]) -> bool:
    """Return ``True`` when at least one criterion contains a verifiable marker.

    Uses whole-word matching via ``re.findall(r"[a-zA-Z]+", criterion.lower())``
    so inflected forms like ``"testified"`` do not accidentally match
    ``"test"``, and trailing punctuation (``"tests pass."``) is stripped
    before the set intersection. Case-insensitive. Returns ``True`` for
    an empty list (vacuous pass — no criteria to check).

    Designed as the fourth heuristic in
    :meth:`~maestro.evolution.EvolutionEngine.validate_success_criteria`.
    When no criterion in the entire batch contains any marker, the batch
    describes activities (analyze, review, assess) rather than verifiable
    states (file created, tests pass, module deleted) and is rejected.
    One verifiable word in one criterion lets the entire set pass — only
    purely abstract criteria are caught.

    Pure function — zero SDK cost.
    """
    if not criteria:
        return True
    for criterion in criteria:
        words = set(re.findall(r"[a-zA-Z]+", criterion.lower()))
        if words & _VERIFIABLE_MARKERS:
            return True
    return False

def check_partial_construction_gap(
    recent_tasks: list[dict[str, object]],
) -> str | None:
    """Return a directive when the most recent task was partial with findings.

    Examines only the **last** element of *recent_tasks*. Returns a
    construction directive string (including ``task_id`` and ≤200-char
    findings) when:

    - ``outcome`` is ``"partial"``
    - ``key_findings`` (preferred) or ``result_summary`` (fallback) is
      non-empty after stripping whitespace

    Returns ``None`` when:

    - *recent_tasks* is empty
    - the most recent outcome is not ``"partial"``
    - the most recent outcome is a zero-duration no-op skip (``duration_secs``
      is ``0.0``) — e.g. a ``[merge-block]`` zero-cost gate skip, which records
      ``partial`` but did no work to construct against
    - both ``key_findings`` and ``result_summary`` are empty/missing

    Self-extinguishing: fires only when the very last task is partial; a
    subsequent completed task pushes the partial out of tail position.
    Pure function — no I/O, zero SDK cost.
    """
    if not recent_tasks:
        return None
    last = recent_tasks[-1]
    if last.get("outcome") != "partial":
        return None
    # A zero-duration partial is a no-op skip, not partial construction: the
    # `[merge-block]` zero-cost gate records `partial` (git-merge-truth shows
    # the PR unlanded) without doing any work. Firing the bridge on it would
    # inject a directive to "complete" a remediation the gate just declined to
    # run — re-feeding the wheel. Genuine partial tasks always carry positive
    # duration, so this excludes only the no-op skips.
    if last.get("duration_secs") == 0.0:
        return None
    # Prefer key_findings, fall back to result_summary.
    findings = ""
    raw_kf = last.get("key_findings")
    if isinstance(raw_kf, str) and raw_kf.strip():
        findings = raw_kf.strip()
    else:
        raw_rs = last.get("result_summary")
        if isinstance(raw_rs, str) and raw_rs.strip():
            findings = raw_rs.strip()
    if not findings:
        return None
    task_id = str(last.get("task_id", "unknown"))
    prefix = f"Complete partial task {task_id}: "
    remaining = _DIRECTIVE_MAX_CHARS - len(prefix)
    if len(findings) > remaining:
        findings = findings[: remaining - 1] + "…"
    return prefix + findings


# ---------------------------------------------------------------------------
# Self-directed success-rate directive — mechanical gate over rolling window
# ---------------------------------------------------------------------------

# Minimum rolling success rate for self-directed tasks below which the
# directive injects a bounded-construction requirement. At 0.70, the
# gate fires when fewer than 70% of self-directed tasks succeed,
# steering toward the task shape (bounded construction) that historically
# succeeds at ~100%.
SELF_DIRECTED_SUCCESS_THRESHOLD = 0.70

# Number of recent tasks examined by the self-directed success-rate gate.
SELF_DIRECTED_WINDOW = 15

# Minimum number of self-directed tasks in the window before the gate
# fires. Below this the sample is too noisy to draw conclusions.
SELF_DIRECTED_MIN_SAMPLE = 5

# Task shapes classified as externally-scoped. A secondary shape filter
# layered over the ``TaskRecord.source == "self_directed"`` provenance
# filter in the self-directed success-rate calculation: a self-directed
# queue task that targets a GitHub issue filed by someone else still has
# its scope set externally, so it is excluded too.
_EXTERNAL_SHAPES: frozenset[str] = frozenset({"github_issue"})


def check_self_directed_success_rate(
    tasks: list[TaskRecord],
    window: int = SELF_DIRECTED_WINDOW,
    threshold: float = SELF_DIRECTED_SUCCESS_THRESHOLD,
) -> str | None:
    """Return a directive when self-directed task success rate drops below *threshold*.

    Slices the last *window* tasks and keeps only those whose recorded
    provenance is ``source == "self_directed"`` (origin comes from
    ``TaskRecord.source``, never goal shape — matching
    ``_consecutive_partial_breaker_run``, so external remediation laps and
    stand-down records never enter the denominator), then additionally
    excludes those whose ``goal`` classifies as an :data:`_EXTERNAL_SHAPES`
    shape via :func:`classify_task_shape`, and computes the success rate
    over the remainder. Returns a directive string when the rate is strictly below *threshold* and at least
    :data:`SELF_DIRECTED_MIN_SAMPLE` self-directed tasks exist in the
    window.

    Returns ``None`` when:
    - *tasks* is empty or *window* ≤ 0
    - fewer than :data:`SELF_DIRECTED_MIN_SAMPLE` self-directed tasks exist
    - the success rate is at or above *threshold*

    Self-extinguishing: once self-directed success rate recovers above
    *threshold*, the gate stops firing.
    Pure function — no I/O, zero SDK cost.
    """
    if not tasks or window <= 0:
        return None
    recent = tasks[-window:]
    self_directed = [
        t for t in recent
        if t.source == "self_directed"
        and classify_task_shape(t.goal or "") not in _EXTERNAL_SHAPES
    ]
    if len(self_directed) < SELF_DIRECTED_MIN_SAMPLE:
        return None
    successes = sum(1 for t in self_directed if t.outcome == "success")
    rate = successes / len(self_directed)
    if rate >= threshold:
        return None
    pct = round(rate * 100)
    return (
        f"Self-directed success rate is {pct}% "
        f"({successes}/{len(self_directed)}). "
        f"Next task must be bounded construction: "
        f"fix a bug, wire an existing branch, or extend a working module."
    )


# Post-merge impact-review audit rule (rule 9) — knobs and sidecar.
# Defaults mirror the [audit] config keys wired in
# ``loop.self_audit.audit_self_model``; the function accepts overrides so the
# caller stays the single config decision point.
_IMPACT_REVIEW_DEFAULT_AFTER_DAYS = 7
_IMPACT_REVIEW_DEFAULT_WINDOW = 10
# Surfaced-version-id sidecar filename under the data dir; dedups so a
# version is only ever raised once.
_IMPACT_REVIEW_SIDECAR = "impact_review_surfaced.json"
# Commit-SHA prefix length compared against recent-task text to decide
# whether an evolution has already been revisited.
_IMPACT_REVIEW_SHA_PREFIX = 8


def _impact_review_task_text(recent_tasks: list[TaskRecord]) -> str:
    """Concatenate ``result_summary`` + ``key_findings`` of *recent_tasks*.

    Lower-cased so commit-SHA-prefix membership tests are case-insensitive.
    Used to detect whether a merged evolution has already been revisited.
    """
    parts: list[str] = []
    for task in recent_tasks:
        if isinstance(task.result_summary, str):
            parts.append(task.result_summary)
        if isinstance(task.key_findings, str):
            parts.append(task.key_findings)
    return "\n".join(parts).lower()


def _load_impact_review_surfaced(path: Path) -> set[str]:
    """Read the surfaced-version-id set from the sidecar.

    Returns an empty set when the file is absent, unreadable, corrupt, or
    not a JSON list of strings — the rule simply re-evaluates from scratch
    rather than crashing on a damaged sidecar.
    """
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Skipping corrupted impact-review sidecar %s", path)
        return set()
    if not isinstance(raw, list):
        return set()
    return {v for v in raw if isinstance(v, str)}


@runtime_checkable
class ImpactReviewSurfacedBackend(Protocol):
    """The impact-review surfaced-set sidecar surface (proposal 106).

    The sorted ``set[str]`` of surfaced :class:`VersionRecord` ids that
    :func:`check_evolution_impact_reviews` (rule 9) has already raised — so each
    merged evolution is surfaced for post-merge review exactly once — can live in
    ``impact_review_surfaced.json`` (default) or in ``state.db`` behind
    ``[paths] sqlite_state_enabled`` with no call-site branching. The rule only
    calls :meth:`load`; ``loop.self_audit.audit_self_model`` calls :meth:`record`
    with the ids of the rule-9 findings that survive the cycle's final clip, so an id is
    marked only once its finding actually surfaced. The rule is the
    set's single consumer, and the set is **append-only** (surfaced ids are only
    ever added, never pruned), so — unlike the prune-and-touch map siblings
    (:class:`OrphanedCureBackend`) — the seam is a two-method :meth:`load` /
    :meth:`record` surface rather than a read-modify-write one.
    ``@runtime_checkable`` enables a structural ``isinstance`` check in the
    agent's selection tests.
    """

    def load(self) -> set[str]:
        """Return the stored surfaced-version-id set (empty when unset)."""
        ...

    def record(self, version_ids: list[str]) -> None:
        """Idempotently add *version_ids* to the stored surfaced set."""
        ...


def load_all_impact_review_surfaced(data_dir: Path) -> set[str]:
    """Return the surfaced-version-id set — the SQLite seed source.

    Reuses the corrupt-tolerant :func:`_load_impact_review_surfaced` over
    ``<data_dir>/impact_review_surfaced.json``, so enabling
    ``[paths] sqlite_state_enabled`` seeds the store from the in-flight sidecar.
    Has a real call site (the
    :class:`~maestro.state_store.SqliteImpactReviewSurfacedStore` one-time seed),
    so it does not trip the self-review uncalled-symbol gate. Read-only, zero SDK
    cost.
    """
    return _load_impact_review_surfaced(data_dir / _IMPACT_REVIEW_SIDECAR)


class JsonImpactReviewSurfacedStore:
    """JSON-backed :class:`ImpactReviewSurfacedBackend` — the default, flag-off store.

    Binds a ``data_dir`` and keeps the historical ``file_lock``-guarded
    load-union-write body verbatim: :meth:`record` reads the sidecar via
    :func:`_load_impact_review_surfaced`, unions the new ids in, and
    ``atomic_write``s the sorted list back — exactly the block formerly inline in
    :func:`check_evolution_impact_reviews` — while :meth:`load` returns the same
    corrupt-tolerant read. The sidecar file is
    ``<data_dir>/impact_review_surfaced.json``. Mirrors the
    :class:`JsonOrphanedCureStore` idiom, reduced to the append-only set's
    two-method surface.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def load(self) -> set[str]:
        return _load_impact_review_surfaced(
            self._data_dir / _IMPACT_REVIEW_SIDECAR,
        )

    def record(self, version_ids: list[str]) -> None:
        sidecar = self._data_dir / _IMPACT_REVIEW_SIDECAR
        with file_lock(sidecar):
            surfaced = _load_impact_review_surfaced(sidecar)
            surfaced.update(version_ids)
            atomic_write(sidecar, json.dumps(sorted(surfaced)))


def check_evolution_impact_reviews(
    version_records: list[VersionRecord],
    recent_tasks: list[TaskRecord],
    backend: ImpactReviewSurfacedBackend,
    *,
    max_age_days: int = _IMPACT_REVIEW_DEFAULT_AFTER_DAYS,
    review_window: int = _IMPACT_REVIEW_DEFAULT_WINDOW,
    enabled: bool = True,
    now: datetime | None = None,
) -> list[AuditFinding]:
    """Rule 9 — surface merged evolutions overdue for post-merge review.

    Closes the verify→remove half of the full circuit: identify→build
    completes today, but merged evolutions are never revisited to check
    whether their acceptance criteria still hold, so there is no
    evidence-based retirement signal. This rule provides the trigger.

    Scans the last *review_window* :class:`VersionRecord` entries. A record
    is surfaced as a finding when **all** of the following hold:

    * its ``merged_at`` timestamp is older than *max_age_days* (young or
      timestamp-unparseable records are skipped);
    * no recent task's ``result_summary`` / ``key_findings`` mentions its
      commit-SHA prefix (first :data:`_IMPACT_REVIEW_SHA_PREFIX` chars) —
      i.e. nothing has already revisited it;
    * it is not already recorded in the surfaced set (loaded once from the
      injected :class:`ImpactReviewSurfacedBackend`), so a version is only
      ever raised once.

    Pure with respect to the surfaced set: it only reads *backend*; the
    caller records the ids of the findings that survive its final
    per-cycle clip, so a candidate clipped away is re-raised later rather
    than lost. Returns at
    most :data:`MAX_FINDINGS_PER_RULE` findings; any
    remaining overdue versions surface on later cycles. Setting *enabled*
    to ``False`` (the ``[audit] impact_review_enabled`` master switch)
    short-circuits to an empty list with no I/O. Zero SDK cost.
    """
    if not enabled:
        return []
    if review_window <= 0:
        return []
    window = version_records[-review_window:]
    if not window:
        return []

    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=max_age_days)
    task_text = _impact_review_task_text(recent_tasks)

    findings: list[AuditFinding] = []
    surfaced = backend.load()
    for record in window:
        if record.version_id in surfaced:
            continue
        merged_at = _parse_iso_utc(record.merged_at)
        if merged_at is None or merged_at > cutoff:
            continue
        sha_prefix = (record.commit_sha or "")[:_IMPACT_REVIEW_SHA_PREFIX]
        if sha_prefix and sha_prefix.lower() in task_text:
            continue
        gate = "passed" if record.quality_gate_pass else "did NOT pass"
        findings.append(AuditFinding(
            kind="unreviewed_evolution",
            statement=(
                f"Merged evolution {record.version_id!r} "
                f"({record.plan_title!r}) is overdue for post-merge "
                f"impact review — merged {record.merged_at}, merge-CI "
                f"{gate}; re-validate its acceptance criteria or retire "
                f"it."
            ),
            cites=[record.version_id],
            rule="evolution_impact_review",
        ))
        if len(findings) >= MAX_FINDINGS_PER_RULE:
            break
    return findings


def _parse_iso_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, coercing naive values to UTC.

    Returns ``None`` for empty or unparseable input so callers can treat a
    damaged timestamp as "not eligible" rather than crashing.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed



# ---------------------------------------------------------------------------
# Compound-scope post-call gate — reject build+wire self-scoped goals
# ---------------------------------------------------------------------------

# Compiled regex matching the specific "creation verb … conjunction …
# connection verb" pattern within a single clause.  Positional ordering
# (creation before conjunction before connection) avoids false positives on
# standalone wiring goals ("Wire X into Y") or creation goals whose
# *implementation detail* happens to mention integration ("Add a function
# that integrates data").  The conjunction marker (``and``, ``then``,
# ``,``) must appear *between* the two verb clusters.
_COMPOUND_SCOPE_PATTERN = re.compile(
    r"""
    \b(?:build|add|create|implement|write|introduce|extend)\b  # creation verb
    .{0,80}?                                                    # short clause gap
    (?:\band\b|,\s*(?:then\b)?\s*|\bthen\b)                   # conjunction marker
    .{0,40}?                                                    # short gap
    \b(?:wire|hook|plug|inject|integrate|connect)\b            # connection verb
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


def is_compound_scope(description: str) -> bool:
    """Return ``True`` when *description* combines creation and connection verbs.

    Detects the "build X and wire it into Y" compound-scope pattern that
    historically leads to partial completion.  Uses positional ordering
    (creation verb → conjunction → connection verb) to avoid false
    positives on standalone wiring goals or single-verb goals.

    Pure function — zero SDK cost.  Designed as a post-call gate in
    ``generate_task()``.
    """
    if not description:
        return False
    return bool(_COMPOUND_SCOPE_PATTERN.search(description))


# ---------------------------------------------------------------------------
# Concreteness post-call gate — reject pure-analysis tasks at generation
# ---------------------------------------------------------------------------

# A concrete *implementation* target is a code/doc source file the task will
# modify — a path under a source directory (``maestro/...py``, ``tests/...py``,
# ``docs/...md``), a repo-root doc (``CLAUDE.md`` / ``README.md``), or a
# ``.toml`` config. Deliberately excludes data files (``*.jsonl`` / ``*.json``)
# and ``artifacts/...`` outputs: those are analysis *inputs* and gitignored
# *outputs*, never implementation targets, so a task that only reads a
# ``.jsonl`` or writes an ``artifacts/*.md`` report must not count as concrete.
_CONCRETE_FILE_TARGET_RE = re.compile(
    r"\b(?:maestro|maestro_review|maestro_sdk|tests)/[\w./-]*\w\.py\b"
    r"|\bdocs/[\w./-]*\w\.md\b"
    r"|\b(?:CLAUDE|README|AGENTS)\.md\b"
    r"|\b[\w-]+\.toml\b",
    re.IGNORECASE,
)

# Implementation verbs — the task changes the tree. Mirrors the plan's listed
# set (fix, add, remove, update, create, sweep, replace, delete, rename, move)
# with inflections, each anchored by ``\b`` so substrings (``address`` →
# ``add``) never false-match.
_IMPLEMENTATION_VERB_RE = re.compile(
    r"\b(?:fix(?:e[sd]|ing)?|add(?:s|ed|ing)?|remov(?:e|es|ed|ing)"
    r"|updat(?:e|es|ed|ing)|creat(?:e|es|ed|ing)|sweep(?:s|ing)?|swept"
    r"|replac(?:e|es|ed|ing)|delet(?:e|es|ed|ing)|renam(?:e|es|ed|ing)"
    r"|mov(?:e|es|ed|ing))\b",
    re.IGNORECASE,
)

# Analysis-only verbs — the task produces understanding, not a tree change.
# Includes the "write a report/findings/synthesis/summary" artifact-output
# phrasing so report-writing goals register as analysis-only.
_ANALYSIS_ONLY_VERB_RE = re.compile(
    r"\banaly[sz]\w*|\bexplor\w*|\binvestigat\w*|\bstud(?:y|ies|ied|ying)\b"
    r"|\bsurvey\w*|\bexamin\w*"
    r"|\bwrite\s+(?:a\s+|the\s+)?(?:report|findings|synthesis|summary)\b",
    re.IGNORECASE,
)


def check_task_concreteness(task_text: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` — reject pure-analysis tasks at generation.

    A pure-analysis task is one that produces only understanding (or
    gitignored markdown) and consumes a full implementation cycle without
    touching the tree. It is rejected when **all three** hold: no concrete
    file/module target (:data:`_CONCRETE_FILE_TARGET_RE`), no implementation
    verb (:data:`_IMPLEMENTATION_VERB_RE`), **and** at least one analysis-only
    verb (:data:`_ANALYSIS_ONLY_VERB_RE`). A single file target *or* a single
    implementation verb is enough to admit the task — so a compound
    "analyze X then fix Y" goal, or a goal naming ``maestro/foo.py``, passes.

    Returns ``(True, "")`` when allowed, ``(False, reason)`` when rejected.
    Pure regex — zero SDK cost. Designed as an inline post-call gate in
    ``generate_task()`` alongside terrain-exhaustion and compound-scope.
    """
    if not task_text or not task_text.strip():
        return True, ""
    has_file_target = bool(_CONCRETE_FILE_TARGET_RE.search(task_text))
    has_impl_verb = bool(_IMPLEMENTATION_VERB_RE.search(task_text))
    has_analysis_verb = bool(_ANALYSIS_ONLY_VERB_RE.search(task_text))
    if not has_file_target and not has_impl_verb and has_analysis_verb:
        return (
            False,
            "pure-analysis task: no concrete file target, no implementation "
            "verb, only analysis-only verb(s)",
        )
    return True, ""


# ---------------------------------------------------------------------------
# Phantom-premise post-call gate — refuse a defect claim against an absent file
# ---------------------------------------------------------------------------

# Defect-claim vocabulary — the goal asserts an *existing* fault in the code it
# names, so the named file must already be on disk for the premise to hold.
# Deliberately narrow: each token is a word the goal uses to describe something
# that is wrong *today*, anchored by ``\b`` so substrings never false-match.
_DEFECT_CLAIM_RE = re.compile(
    r"\bfix(?:e[sd]|ing)?\b|\bbugs?\b|\bbroken\b|\bregressions?\b"
    r"|\bcrash(?:es|ed|ing)?\b|\bfail(?:s|ed|ing|ure|ures)?\b"
    r"|\bsilently\b|\bincorrectly\b|\bswallow(?:s|ed|ing)?\b",
    re.IGNORECASE,
)

# Creation vocabulary — the goal's intent is to *bring the named path into
# being*, the legitimate case the advisory premise report already calls out
# (``MaestroAgent._build_premise_report``): a not-yet-existing path is expected,
# not a refuted premise. Its presence admits the goal even alongside a defect
# claim, because a goal may legitimately frame a new file as the fix for a bug
# that lives elsewhere.
_PREMISE_CREATION_RE = re.compile(
    r"\badd(?:s|ed|ing)?\b|\bcreat(?:e|es|ed|ing)\b"
    r"|\bintroduc(?:e|es|ed|ing)\b|\bnew\s+file\b"
    r"|\bextract(?:s|ed|ing)?\b.{0,80}?\binto\b"
    r"|\bscaffold(?:s|ed|ing)?\b",
    re.IGNORECASE | re.DOTALL,
)


def check_phantom_premise(
    goal: str, missing_paths: Sequence[str],
) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` — refuse a defect claim about an absent file.

    A phantom-premise goal asserts a bug in a ``.py`` path that is not in the
    tree. It passes every other content gate, is queued, pops, opens a task
    branch, and burns a full implementation cycle before the executor discovers
    the file it was told to fix is not there — the failure mode
    ``maestro measure-premise-accuracy`` counts but nothing acted on.

    Rejected when **all three** hold: *missing_paths* is non-empty, the goal
    carries a defect-claim token (:data:`_DEFECT_CLAIM_RE`), **and** it carries
    no creation token (:data:`_PREMISE_CREATION_RE`). The creation carve-out is
    load-bearing: a goal whose framing is to *create* the named path
    legitimately references a not-yet-existing file, exactly as the advisory
    ``## PREMISE REPORT`` block already says. It is deliberately *loose* — any
    creation token anywhere in the goal admits it, so a goal that merely says
    "add …" while claiming a defect in an absent path escapes the gate. That
    biases the gate towards a false negative (a phantom goal admitted) over a
    false refusal (a legitimate creation goal dropped), which is the safe
    direction for a default-off experiment; read the ``phantom_premise``
    fire-rate as a *lower bound* on the phantom-premise population, not a
    census. An empty or whitespace goal needs no separate guard: it cannot
    match the defect-claim token, so the check above already admits it.

    Pure — no I/O, zero SDK cost. The existence reading is the caller's
    (``MaestroAgent._missing_file_references``, narrowed to paths that are
    genuinely *absent* rather than present-but-unparseable), so the ``#1882``
    containment guard stays shared rather than duplicated here. Mirrors
    :func:`check_task_concreteness`'s ``(allowed, reason)`` shape.
    """
    if not missing_paths:
        return True, ""
    if not _DEFECT_CLAIM_RE.search(goal):
        return True, ""
    if _PREMISE_CREATION_RE.search(goal):
        return True, ""
    named = ", ".join(missing_paths[:3])
    return (
        False,
        f"phantom premise: claims an existing defect in absent path(s) {named}",
    )


def finding_to_directive(finding: AuditFinding) -> str:
    """Format an :class:`AuditFinding` as a ≤200-char ``next_task_directive``.

    Returns ``'Address audit finding [{kind}]: {detail}'`` where *detail*
    is truncated with a trailing ellipsis (``…``) to guarantee the result
    never exceeds :data:`_DIRECTIVE_MAX_CHARS`. Pure function — no I/O,
    no SDK cost.
    """
    prefix = f"Address audit finding [{finding.kind}]: "
    remaining = _DIRECTIVE_MAX_CHARS - len(prefix)
    detail = finding.statement
    if len(detail) > remaining:
        detail = detail[: remaining - 1] + "…"
    return prefix + detail

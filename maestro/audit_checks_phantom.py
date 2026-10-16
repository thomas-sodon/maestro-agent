"""Phantom and unconfirmed-landing audit rules.

Split out of :mod:`maestro.audit_checks` by proposal 147 Step 9: the
shared git merge-truth oracle, the orphaned-cure, phantom-escalation,
phantom-partial-prune, unconfirmed-own-landing, undercredited-landing and
reconcile-near-miss rules, plus handoff delivery confirmation. Zero SDK
cost; registered by :func:`maestro.audit_checks._registered_audit_checks`.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from maestro.audit_checks import (
    _EVOLUTION_PLANS_FILE,
    _ORIGIN_SELF_DIRECTED,
    _PLAN_ID_RE,
    MAX_CITES_PER_FINDING,
    MAX_FINDINGS_PER_RULE,
    MECHANICAL_OUTCOME_AGREE_ACTION,
    MECHANICAL_OUTCOME_AUTHORITY_GATE,
    MECHANICAL_OUTCOME_DIVERGED_ACTION,
    AuditFinding,
    GateActivationBackend,
    _parse_iso_utc,
    classify_task_origin,
    classify_task_shape,
    extract_issue_numbers,
    is_branch_merged_into_main,
)
from maestro.fileutil import atomic_write, file_lock
from maestro.memory import (
    EvolutionPlan,
    TaskRecord,
)
from maestro.messaging import Message
from maestro.narration_cues import (
    COMMITTED_SHA_RE as _COMMITTED_SHA_RE,
)
from maestro.narration_cues import (
    ESCALATION_PROSE_MARKER as _ESCALATION_PROSE_MARKER,
)
from maestro.narration_cues import (
    ON_BRANCH_RESOLVED_PHRASES as _ON_BRANCH_RESOLVED_PHRASES,
)
from maestro.outcome_grounding import extract_self_committed_shas
from maestro.pr_owed_act import OwedAct
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    StandDownRow,
    StandDownVerdictBackend,
    _read_recent_task_tail,
    classify_landing_attribution,
    pr_number_from_remediation_goal,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rule 19: phantom_partial_prune — a landed owned prune under-claimed as partial
# ---------------------------------------------------------------------------
#
# The affirmative counterpart to the git-keyed landing binds, aimed at
# the *under*-claim side of the grounding-drift loop. My recurring error at the
# owned-terminal-act tail is not phantom success but phantom failure: I fire
# ``mark_plan_obsolete`` on a stale ``pending_review`` plan — the plan_id
# cleanly flips to ``obsolete`` on disk — yet I stamp the task ``partial``
# because I let the *gross* ``pending_review`` count-delta be the confirmation
# arbiter, and concurrent loop writes corrupt that delta (a 21->18 when -1 was
# required still reads as "the prune didn't land"). My identity already names
# the correct witness — the *target plan_id's* status transition, not the gross
# count — but until this rule nothing in code detected when I betray it. The
# landing binds catch partial-narrated-as-done (phantom success); this catches
# landed-narrated-as-partial (phantom failure). It is detection-only, reads no
# git oracle (the plan store is the ground truth for a prune), self-extinguishes
# the moment no ``partial`` owned-prune task cites a disk-``obsolete`` plan_id,
# and is default-off per the opt-in audit-rule convention.

# Marker phrase whose presence in a task's ``result_summary`` signals the task
# fired the ``mark_plan_obsolete`` owned prune lever. Matched case-insensitively.
_PHANTOM_PARTIAL_PRUNE_MARKER = "mark_plan_obsolete"

# Per-rule finding cap; mirrors the other bounded audit rules so a window with
# several under-claimed prunes surfaces without monopolising the findings budget.
_PHANTOM_PARTIAL_PRUNE_PER_RULE_CAP = MAX_FINDINGS_PER_RULE


def _read_plan_statuses(data_dir: Path) -> dict[str, str]:
    """Return a fresh ``{plan_id: status}`` map from ``evolution_plans.jsonl``.

    Reads the plan store under *data_dir* (rewritten in place, so one line per
    plan reflects current status) and maps each record's ``plan_id`` to its
    ``status``. Returns an empty dict when the file is absent, unreadable, or
    wholly unparseable. Malformed lines, non-dict records, and rows lacking a
    non-empty string ``plan_id``/``status`` are skipped (corrupted-tail
    tolerance). Later lines win on a duplicate ``plan_id`` so the last-written
    status is authoritative. Zero SDK cost; one bounded file read.
    """
    path = data_dir / _EVOLUTION_PLANS_FILE
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = list(f)
    except OSError:
        return {}
    statuses: dict[str, str] = {}
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
        plan_id = parsed.get("plan_id")
        status = parsed.get("status")
        if (
            isinstance(plan_id, str) and plan_id
            and isinstance(status, str) and status
        ):
            statuses[plan_id] = status
    return statuses


def check_phantom_partial_prune(
    data_dir: Path,
    n: int,
    *,
    enabled: bool = False,
) -> list[AuditFinding]:
    """Rule 19 — a landed owned ``mark_plan_obsolete`` prune under-claimed partial.

    The affirmative counterpart to the git-keyed landing binds: where those
    demote partial-work narrated as done (phantom success), this catches a
    *landed* owned prune stamped ``partial`` (phantom failure).

    Reads the trailing *n* records of ``task_history.jsonl`` under *data_dir*
    and selects each task that is:

    * stamped ``partial`` (the under-claim outcome), and
    * ``self_directed`` (via :func:`classify_task_origin` — an owned prune, not
      a PR-remediation lap), and
    * ``pr_head_sha``-less (a prune has no PR head; a head-carrying record is a
      remediation lap, not an owned prune), and
    * whose ``result_summary`` names the :data:`_PHANTOM_PARTIAL_PRUNE_MARKER`
      lever and carries at least one extractable 12-hex ``plan_id``
      (:data:`_PLAN_ID_RE`).

    For each such task, the plan store is read **fresh**
    (:func:`_read_plan_statuses`) — the correct arbiter for a prune is the
    *target plan_id's* status transition, not the gross ``pending_review``
    count-delta that concurrent loop writes corrupt. When a cited plan_id reads
    ``obsolete`` on disk, the prune landed and the ``partial`` verdict
    under-claimed it; one :class:`AuditFinding`
    (``kind='phantom_partial_prune'``) is emitted naming the plan_id, deduped so
    a plan_id surfaces once. Capped at
    :data:`_PHANTOM_PARTIAL_PRUNE_PER_RULE_CAP`.

    Returns no finding when the plan is still ``pending_review`` (genuinely not
    yet pruned → the ``partial`` is honest), when no qualifying task exists, or
    when *enabled* is ``False`` (the ``[audit] phantom_partial_prune_enabled``
    master switch, default-off per the opt-in audit-rule convention). Reads no
    git oracle; self-extinguishes the moment no ``partial`` owned-prune task
    cites a disk-``obsolete`` plan_id. Zero SDK cost; bounded file reads only.
    """
    if not enabled or n <= 0:
        return []
    statuses: dict[str, str] | None = None
    findings: list[AuditFinding] = []
    seen: set[str] = set()
    for task in _read_recent_task_tail(data_dir, n):
        if task.get("outcome") != "partial":
            continue
        if classify_task_origin(task) != _ORIGIN_SELF_DIRECTED:
            continue
        head = task.get("pr_head_sha")
        if isinstance(head, str) and head:
            continue
        summary = task.get("result_summary")
        if not isinstance(summary, str) or not summary:
            continue
        if _PHANTOM_PARTIAL_PRUNE_MARKER not in summary.lower():
            continue
        plan_ids = _PLAN_ID_RE.findall(summary)
        if not plan_ids:
            continue
        if statuses is None:
            statuses = _read_plan_statuses(data_dir)
        for plan_id in plan_ids:
            if plan_id in seen:
                continue
            if statuses.get(plan_id) != "obsolete":
                continue
            seen.add(plan_id)
            tid = task.get("task_id")
            cites = [str(tid)] if isinstance(tid, str) and tid else []
            findings.append(AuditFinding(
                kind="phantom_partial_prune",
                statement=(
                    f"An owned mark_plan_obsolete prune of plan {plan_id} was "
                    f"stamped 'partial', but the plan store shows that plan_id "
                    f"'obsolete' — the prune landed. The arbiter is the target "
                    f"plan_id's status transition, not the gross pending_review "
                    f"count-delta concurrent writes corrupt; stamp it success."
                ),
                cites=cites[:MAX_CITES_PER_FINDING],
                rule="phantom_partial_prune",
            ))
            if len(findings) >= _PHANTOM_PARTIAL_PRUNE_PER_RULE_CAP:
                return findings
    return findings

def measure_phantom_landing_rate(
    backend: GateActivationBackend,
    n: int = 100,
) -> dict[str, object]:
    """Measure the phantom-landing divergence rate from the shadow ledger.

    Reads the gate-activation trace via ``backend.read_all()``, keeps only the
    ``mechanical_outcome_authority`` shadow-mode rows (proposal 105), takes the
    last *n* of *those* rows so the denominator is a stable window of observed
    shadow records — not diluted by unrelated gate firings sharing the trace —
    and counts ``diverged`` vs ``agree``. Returns::

        {
            "total": int,             # diverged + agree
            "diverged": int,          # mechanical verdict contradicted narration
            "agree": int,             # mechanical verdict matched narration
            "divergence_rate": float, # diverged / total (0.0 if total == 0)
        }

    ``divergence_rate`` is the measured phantom-landing rate: how often the
    model's landing prose disagreed with git-truth. Returns the all-zero result
    when the trace is empty (an empty ``read_all()`` yields it naturally).
    Structurally-corrupt rows are already dropped by the backend's read
    (corrupted-tail tolerance matching :func:`measure_gate_triggers`). Pure given
    the backend — zero SDK cost.
    """
    actions: list[str] = []
    for record in backend.read_all():
        if record.gate != MECHANICAL_OUTCOME_AUTHORITY_GATE:
            continue
        if isinstance(record.action, str):
            actions.append(record.action)
    window = actions[-n:] if n > 0 else []
    diverged = sum(
        1 for a in window if a == MECHANICAL_OUTCOME_DIVERGED_ACTION
    )
    agree = sum(1 for a in window if a == MECHANICAL_OUTCOME_AGREE_ACTION)
    total = diverged + agree
    return {
        "total": total,
        "diverged": diverged,
        "agree": agree,
        "divergence_rate": diverged / total if total > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Shared git-merge-truth oracle
# ---------------------------------------------------------------------------
#
# ``_git_pr_merged_default`` is the merged-only, subject-shape merge predicate
# the PR-keyed audit rules (orphaned-cure, phantom-escalation/-resolution,
# unconfirmed-own-landing) and the remediation sensors consult when no code-host
# oracle is injected. It touches git via one bounded ``git log origin/main``.

# Bound the git-merge-truth query (see :func:`_git_pr_merged_default`) so a
# stalled ``git log origin/main`` cannot hold the audit phase for the full
# blocking syscall. Mirrors the project-wide ``config.git_timeout`` default
# (30 s); the predicate takes no config so the bound is a module constant.
_GIT_MERGE_TRUTH_TIMEOUT_SECONDS = 30


def _git_pr_merged_default(
    pr_number: int, cwd: Path | str | None = None,
) -> bool:
    """Return True when PR ``#pr_number`` landed as a merge/squash commit on main.

    Consults git merge-truth and **enforces** a subject-only contract: a PR has
    landed only when a commit reachable from ``origin/main`` carries the PR
    number in the GitHub-generated landing *subject* — either a
    ``Merge pull request #<N>`` merge-commit subject or the ``(#<N>)`` squash
    suffix. A bare body mention (e.g. Maestro's own commits citing a PR number
    as ledger evidence) is **not** proof of merge and must not satisfy the
    oracle. This is the sole subprocess-touching path in the shared merge-truth
    oracle; it makes no network call.

    The ``--grep`` query is only a *cheap candidate filter* — it over-selects
    (it still matches body mentions), and the subject regexes below are the
    actual discriminator. Each candidate's ``%H\\0%s`` line is split on NUL and
    only its subject is tested, so a body-only mention is rejected.

    *cwd* is the repo root the ``git log`` runs against. It defaults to ``None``
    (the process working directory) so the PR-keyed audit rules and the
    unlanded-remediation sensor keep their existing behaviour; the dequeue-face
    merged-PR exclusion
    threads ``cwd=config.workspace`` so its verdict targets the agent's repo
    regardless of the process cwd.

    Best-effort — any subprocess failure returns ``False`` so an unresolvable
    git state *suppresses* the finding (treats the PR as not-merged) rather than
    raising a spurious phantom alarm on a transient git error. The
    bounded ``timeout`` makes a hung ``git log`` one such failure:
    ``TimeoutExpired`` is a :class:`subprocess.SubprocessError`, so the clause
    treats a timed-out query as not-merged rather than blocking the audit phase.
    """
    stdout = _git_landing_candidates(pr_number, "%H%x00%s", cwd)
    if stdout is None:
        return False
    for line in stdout.splitlines():
        _sha, _sep, subject = line.partition("\x00")
        if not subject:
            continue
        if _is_landing_subject(pr_number, subject):
            return True
    return False


def _is_landing_subject(pr_number: int, subject: str) -> bool:
    """True when *subject* is a GitHub merge/squash landing subject for the PR.

    Anchored, digit-boundary-safe shapes: ``\\b`` after the digits keeps
    ``#2376`` from matching ``#23769``; the ``(#N)$`` anchor does the same for
    the squash suffix.
    """
    merge_subject = re.compile(rf"^Merge pull request #{pr_number}\b")
    squash_subject = re.compile(rf"\(#{pr_number}\)$")
    return bool(merge_subject.search(subject) or squash_subject.search(subject))


def _git_landing_candidates(
    pr_number: int, fmt: str, cwd: Path | str | None,
) -> str | None:
    """Run the bounded ``git log origin/main --grep`` candidate scan.

    Shared by the untimed and timed merge oracles. The ERE uses explicit
    non-digit boundaries so ``#2376`` does not match ``#23769``; it
    over-selects (body mentions), and :func:`_is_landing_subject` is the real
    discriminator. Returns stdout, or ``None`` on any subprocess failure
    (``OSError`` / ``SubprocessError``, including the bounded timeout).
    """
    grep_pattern = rf"(^|[^0-9])#{pr_number}([^0-9]|$)"
    try:
        result = subprocess.run(
            [
                "git", "log", "origin/main",
                "--extended-regexp", f"--grep={grep_pattern}",
                f"--format={fmt}",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_MERGE_TRUTH_TIMEOUT_SECONDS,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout


def _git_pr_merged_at_default(
    pr_number: int, cwd: Path | str | None = None,
) -> datetime | None:
    """Return when PR ``#pr_number`` landed on main, or ``None`` if it has not.

    The timed sibling of :func:`_git_pr_merged_default`: the same bounded
    ``git log origin/main --grep`` candidate filter and the same subject-only
    merge/squash contract (a body-only mention is never proof of merge), but it
    returns the committer time (``%cI``) of the newest landing commit so a
    caller can order an event against the merge. Returns ``None`` when no
    landing subject matches, when the committer time is unparseable, or on any
    subprocess failure (``OSError`` / ``SubprocessError``, including the bounded
    timeout) — best-effort fail-closed, like its untimed sibling.
    """
    stdout = _git_landing_candidates(pr_number, "%H%x00%cI%x00%s", cwd)
    if stdout is None:
        return None
    for line in stdout.splitlines():
        parts = line.split("\x00", 2)
        if len(parts) != 3:
            continue
        _sha, committed_at, subject = parts
        if _is_landing_subject(pr_number, subject):
            return _parse_iso_utc(committed_at)
    return None


# ---------------------------------------------------------------------------
# Rule 11: orphaned_cure — implemented-but-unmerged plan vs. ongoing remediation
# ---------------------------------------------------------------------------
#
# The disease this rule names is inward. Reward fires at construction and goes
# silent at the merge tail, so a built, opened, *unmerged* cure gets orphaned
# while the very remediations it would eliminate keep being lapped. Rules 9 and
# 10 aim the unlanded/unreviewed lens outward (everyone else's PRs, git-truth);
# this one aims it at the agent's own ``implemented`` plans. It is detection-only
# — it surfaces an ``[audit] unmerged_work`` open question and self-extinguishes
# the moment the cure merges (the plan leaves ``implemented`` status); it never
# suppresses a remediation lap or re-routes a PR. Age is measured in audit
# cycles via a first-seen sidecar so a freshly-implemented plan is never flagged
# on its first cycle.

# Default number of audit cycles a plan must sit in ``implemented`` status
# before it is eligible to flag. Strictly > 0 so a just-implemented cure is
# never flagged on the same cycle it is first observed ("after a few cycles, not
# instantly"). Overridable via ``[audit] orphaned_cure_age_cycles``.
_ORPHANED_CURE_DEFAULT_AGE_CYCLES = 3

# Sidecar recording ``{plan_id: first-seen audit iteration}`` for implemented
# plans, so age can be measured in cycles without a per-plan implemented-at
# timestamp. Pruned each cycle to the currently-implemented set so it stays
# bounded. Mirrors rule 9's sidecar I/O isolation under a file lock.
_ORPHANED_CURE_SIDECAR = "orphaned_cure_first_seen.json"

# Per-rule cap; mirrors :data:`MAX_FINDINGS_PER_RULE` so a burst of orphaned
# cures in one window cannot monopolise the cycle's findings budget.
_ORPHANED_CURE_PER_RULE_CAP = MAX_FINDINGS_PER_RULE


def _load_orphaned_cure_seen(path: Path) -> dict[str, int]:
    """Read the ``{plan_id: first-seen iteration}`` map from the sidecar.

    Returns an empty dict when the file is absent, unreadable, corrupt, or not
    a JSON object of ``str -> int`` — the rule then re-seeds from the current
    cycle rather than crashing on a damaged sidecar. Booleans are rejected as
    values (``bool`` subclasses ``int``) so a corrupted ``true``/``false`` is
    never mistaken for iteration 1/0.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Skipping corrupted orphaned-cure sidecar %s", path)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: v
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
    }


@runtime_checkable
class OrphanedCureBackend(Protocol):
    """The orphaned-cure first-seen sidecar surface (proposal 106).

    The ``{plan_id: first-seen audit iteration}`` map that
    :func:`check_orphaned_cure` (rule 11) uses to age an ``implemented``-but-
    unmerged cure can live in ``orphaned_cure_first_seen.json`` (default) or in
    ``state.db`` behind ``[paths] sqlite_state_enabled`` with no call-site
    branching. Unlike the single-consumer sibling
    :class:`UnconfirmedOwnLandingBackend`, this map has **two** consumers — the
    rule that prunes-and-touches it each cycle, and the retirement sweep
    (:meth:`~maestro.memory.Memory.sweep_orphaned_cures`) that re-reads it — so
    the seam exposes both an atomic :meth:`prune_and_touch` read-modify-write and
    a plain :meth:`load`. ``@runtime_checkable`` enables a structural
    ``isinstance`` check in the agent's selection tests.
    """

    def prune_and_touch(
        self, observed: list[str], iteration: int,
    ) -> dict[str, int]:
        """Prune the stored map to *observed*, touching newly-seen plan ids.

        Load the current ``{plan_id: first_seen}`` map, compute
        ``{pid: seen.get(pid, iteration) for pid in observed}`` — dropping every
        plan id no longer in *observed* (it left ``implemented``) and stamping a
        first-seen of *iteration* on any plan id seen for the first time —
        persist that map, and return it for the caller's findings loop.
        """
        ...

    def load(self) -> dict[str, int]:
        """Return the stored ``{plan_id: first-seen iteration}`` map, unmodified.

        The read-only accessor the retirement sweep uses to measure cure age
        without mutating the map (the rule's :meth:`prune_and_touch` has already
        pruned/touched it this cycle, so the sweep reads the freshly-pruned map).
        """
        ...


def load_all_orphaned_cure_seen(data_dir: Path) -> dict[str, int]:
    """Return the ``{plan_id: first-seen iteration}`` map — the SQLite seed source.

    Reuses the corrupt-tolerant :func:`_load_orphaned_cure_seen` over
    ``<data_dir>/orphaned_cure_first_seen.json``, so enabling
    ``[paths] sqlite_state_enabled`` seeds the store from the in-flight sidecar.
    Has a real call site (the
    :class:`~maestro.state_store.SqliteOrphanedCureStore` one-time seed), so it
    does not trip the self-review uncalled-symbol gate. Read-only, zero SDK cost.
    """
    return _load_orphaned_cure_seen(data_dir / _ORPHANED_CURE_SIDECAR)


class JsonOrphanedCureStore:
    """JSON-backed :class:`OrphanedCureBackend` — the default, flag-off store.

    Binds a ``data_dir`` and keeps the historical ``file_lock``-guarded
    load-prune-write body verbatim: :meth:`prune_and_touch` reads the sidecar via
    :func:`_load_orphaned_cure_seen`, recomputes the pruned/touched map, and
    ``atomic_write``s it back — exactly the block formerly inline in
    :func:`check_orphaned_cure` — while :meth:`load` returns the same
    corrupt-tolerant read for the retirement sweep. The sidecar file is
    ``<data_dir>/orphaned_cure_first_seen.json``. Mirrors the
    :class:`JsonUnconfirmedOwnLandingStore` idiom, extended with the second
    read-only consumer.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def prune_and_touch(
        self, observed: list[str], iteration: int,
    ) -> dict[str, int]:
        sidecar = self._data_dir / _ORPHANED_CURE_SIDECAR
        with file_lock(sidecar):
            seen = _load_orphaned_cure_seen(sidecar)
            # Seed first-seen for newly-implemented plans and drop entries whose
            # plan has left ``implemented`` in one pass, so the sidecar tracks
            # exactly the live set.
            updated = {pid: seen.get(pid, iteration) for pid in observed}
            atomic_write(sidecar, json.dumps(updated, sort_keys=True))
        return updated

    def load(self) -> dict[str, int]:
        return _load_orphaned_cure_seen(self._data_dir / _ORPHANED_CURE_SIDECAR)


def _plan_pr_number(pr_url: str | None) -> int | None:
    """Best-effort PR number from a plan's ``pr_url`` (``None`` on any failure).

    Mirrors :meth:`maestro.evolution.EvolutionEngine._pr_number_from_url`'s
    parse (strip query/fragment, take the trailing path segment, require a
    positive int) but returns ``None`` instead of raising, so a settlement
    pre-filter over a heterogeneous plan corpus never crashes on a malformed or
    absent URL — an unparseable URL simply means "cannot pre-filter this plan".
    """
    if not isinstance(pr_url, str) or not pr_url.strip():
        return None
    try:
        clean = pr_url.split("?")[0].split("#")[0]
        number = int(clean.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return None
    return number if number > 0 else None


def check_orphaned_cure(
    plans: list[EvolutionPlan],
    recent_tasks: list[TaskRecord],
    backend: OrphanedCureBackend,
    iteration: int,
    *,
    age_cycles: int = _ORPHANED_CURE_DEFAULT_AGE_CYCLES,
    enabled: bool = True,
    settled_prs: frozenset[int] = frozenset(),
) -> list[AuditFinding]:
    """Rule 11 — an implemented-but-unmerged cure while remediation laps run.

    Cross-references two signals over the recent window and flags **only when
    both are non-zero**:

    * a plan in ``implemented`` status (built + PR opened, not yet ``merged``)
      whose age in audit cycles is at least *age_cycles*; and
    * at least one remediation-shaped task (``classify_task_shape == 'remediation'``)
      among *recent_tasks* — the laps the cure would eliminate.

    Age is measured in cycles via a first-seen map owned by *backend* (the JSON
    sidecar :data:`_ORPHANED_CURE_SIDECAR` by default, or ``state.db`` under
    ``[paths] sqlite_state_enabled``) keyed by ``plan_id`` and storing the
    *iteration* at which each implemented plan was first observed; the map is
    pruned each cycle to the currently-implemented set so it stays bounded. A
    plan first seen this cycle has age 0 and is never flagged, so a fresh
    implementation is given room to merge before the alarm sounds.

    Emits one :class:`AuditFinding` (``kind='unmerged_work'``,
    ``rule='orphaned_cure'``) per overdue plan, capped at
    :data:`_ORPHANED_CURE_PER_RULE_CAP`. Self-extinguishing: once the cure
    merges, the plan leaves ``implemented`` status and the rule goes silent.
    Setting *enabled* to ``False`` (the ``[audit] orphaned_cure_enabled``
    master switch) short-circuits to an empty list with no I/O.

    *settled_prs* pre-filters a plan whose ``pr_url`` names a PR the standing
    settlement reconciler has observed settled (merged *or* closed) via the
    ``is_pr_settled`` chokepoint — mirroring the same pre-filter rules 12/14
    already take. Such a plan is dropped from the implemented set *before* the
    first-seen touch, so it is also pruned from the age map, and the finding
    genuinely self-extinguishes even when the plan's own ``implemented`` →
    ``merged`` status flip lags (the ``check_merged_plans`` phase starving
    behind live PR-conflict tending is exactly that lag). A plan with no
    parseable PR URL is never in *settled_prs* and behaves as before. The empty
    default keeps every legacy call identical.

    Zero SDK cost; the only I/O is *backend*'s prune-and-touch (a locked sidecar
    read/write, or one ``state.db`` transaction).
    """
    if not enabled:
        return []
    implemented = [
        p for p in plans
        if p.status == "implemented"
        and isinstance(p.plan_id, str)
        and p.plan_id
        and _plan_pr_number(p.pr_url) not in settled_prs
    ]
    if not implemented:
        return []
    remediation_laps = sum(
        1 for t in recent_tasks
        if classify_task_shape(t.goal or "") == "remediation"
    )
    if remediation_laps == 0:
        return []

    implemented_ids = {p.plan_id for p in implemented}
    # The backend owns the atomic load-prune-write: it drops entries whose plan
    # has left ``implemented`` and stamps a first-seen on newly-implemented plans
    # in one pass, so the map tracks exactly the live set. It returns the pruned/
    # touched map the findings loop measures age against.
    updated = backend.prune_and_touch(sorted(implemented_ids), iteration)
    findings: list[AuditFinding] = []
    for plan in implemented:
        age = iteration - updated[plan.plan_id]
        if age < age_cycles:
            continue
        findings.append(AuditFinding(
            kind="unmerged_work",
            statement=(
                f"Cure plan {plan.plan_id!r} ({plan.title!r}) has been "
                f"implemented but unmerged for {age} audit cycles while "
                f"{remediation_laps} remediation laps ran in the same "
                f"window — merge the cure or retire it; do not keep "
                f"lapping the remediations it would eliminate."
            ),
            cites=[plan.plan_id],
            rule="orphaned_cure",
        ))
        if len(findings) >= _ORPHANED_CURE_PER_RULE_CAP:
            break
    return findings


# ---------------------------------------------------------------------------
# Rule 12: phantom_escalation — 'escalating' prose with no outbound message
# ---------------------------------------------------------------------------
#
# The merge-block / branch-resolved gates downgrade a remediation lap to
# ``partial`` and frame it "escalating" — but the verb lives only in the
# ``result_summary`` prose. Standing down means handing **up**: one real message
# to Thomas naming the PR. Rules 10/11 watch the unlanded/orphaned tail; this
# rule watches the *escalation* tail — the gap between a partial that asserts it
# escalated and the existence of an outbound message that actually left the
# building. It is the standing alarm at the door: it counts ``partial`` laps
# whose summary asserts escalation for ``#NNNN`` and goes quiet only when an
# outbound (``sender="maestro"``) message referencing the same issue exists. It
# cannot be satisfied by another lap — only by knocking. Self-extinguishing: the
# moment a referencing message is delivered, that issue drops out of the scan.

# Per-rule cap; mirrors :data:`MAX_FINDINGS_PER_RULE` so a burst of phantom
# escalations in one window cannot monopolise the cycle's findings budget.
_PHANTOM_ESCALATION_PER_RULE_CAP = MAX_FINDINGS_PER_RULE


# Preserved delivery actuator (proposal 105 §5d out-of-scope): a live merge-or-close
# delivery arm, not a verdict-*labeling* phantom rule — the git-grounded landing
# authority (``maestro/outcome_authority.py::decide_outcome``) subsumed only the
# verdict-labeling rules (10/13/15), never this one.
def check_phantom_escalation(
    tasks: list[TaskRecord],
    messages: list[Message],
    *,
    enabled: bool = True,
    settled_prs: frozenset[int] = frozenset(),
) -> list[AuditFinding]:
    """Rule 12 — escalation prose in a partial lap with no message to Thomas.

    Scans *tasks* for ``partial`` laps whose ``result_summary`` asserts
    escalation (carries the :data:`_ESCALATION_PROSE_MARKER` stem) and references
    a ``#NNNN`` issue/PR number, grouping the citing ``task_id``\\ s per issue.
    Cross-references *messages* for outbound (``sender == "maestro"``) entries
    whose ``content`` references the same number — the human knock that the
    escalation prose claims to have delivered. Emits one
    :class:`AuditFinding` (``kind='phantom_escalation'``,
    ``rule='phantom_escalation'``) per issue that has escalation prose but **no**
    referencing outbound message, capped at
    :data:`_PHANTOM_ESCALATION_PER_RULE_CAP` and ordered by issue number
    ascending (oldest-first doctrine).

    Self-extinguishing by construction: the rule goes quiet for an issue the
    moment a single outbound message references it — the escalation verb has
    become a delivered knock rather than prose. Setting *enabled* to ``False``
    (the ``[audit] phantom_escalation_enabled`` master switch) short-circuits to
    an empty list.

    *settled_prs* (proposal 097) additionally pre-filters an issue whose number
    the standing settlement reconciler has observed settled (merged *or* closed)
    via the ``is_pr_settled`` chokepoint — skipped alongside the *delivered*
    skip. The reconciler only records real PR numbers from ``Remediate PR #N``
    verdicts, so a plain (non-PR) issue number is never in the set and fires
    exactly as today. The empty default keeps every legacy call identical.

    Pure function — zero SDK cost, no I/O.
    """
    if not enabled:
        return []

    # Group escalation-prose partials by the issue number they reference. The
    # ``#NNNN`` key is read from the summary first (where the escalation verb
    # lives), falling back to the goal so a ``Remediate PR #N`` lap whose summary
    # omits the number is still keyed correctly.
    prose_cites: dict[int, list[str]] = {}
    for task in tasks:
        if task.outcome != "partial":
            continue
        summary = task.result_summary
        if not isinstance(summary, str) or not summary:
            continue
        if _ESCALATION_PROSE_MARKER not in summary.lower():
            continue
        issues = extract_issue_numbers(summary) or extract_issue_numbers(
            task.goal or "",
        )
        for issue_num in issues:
            cites = prose_cites.setdefault(issue_num, [])
            tid = task.task_id
            if isinstance(tid, str) and tid and tid not in cites:
                cites.append(tid)
    if not prose_cites:
        return []

    # Issue numbers referenced by any outbound (maestro→Thomas) message — the
    # door actually knocked. A single referencing message extinguishes the
    # issue, so escalation that *did* leave the building never flags.
    delivered: set[int] = set()
    for msg in messages:
        if msg.sender != "maestro":
            continue
        delivered |= extract_issue_numbers(msg.content or "")

    findings: list[AuditFinding] = []
    for issue_num in sorted(prose_cites):
        if issue_num in delivered:
            continue
        # Standing-settlement pre-filter (proposal 097): a PR the reconciler
        # observed settled is skipped alongside a delivered knock. Plain issue
        # numbers are never in the set (the reconciler records only PR numbers).
        if issue_num in settled_prs:
            continue
        lap_count = len(prose_cites[issue_num])
        findings.append(AuditFinding(
            kind="phantom_escalation",
            statement=(
                f"Remediation for #{issue_num} was stamped 'escalating' in "
                f"{lap_count} partial lap(s) but no outbound message to Thomas "
                f"references it — the escalation lived only in prose; send one "
                f"merge-or-close message or stop re-stamping the skip."
            ),
            cites=prose_cites[issue_num][:MAX_CITES_PER_FINDING],
            rule="phantom_escalation",
        ))
        if len(findings) >= _PHANTOM_ESCALATION_PER_RULE_CAP:
            break
    return findings


# ---------------------------------------------------------------------------
# Rule 14: unconfirmed_own_landing — my committed SHA not yet on origin/main
# ---------------------------------------------------------------------------
#
# Rule 12 reads a *verdict* (an escalation phrase) and contests it against
# git-merge-truth keyed by PR number.
# None watches the most concrete handoff gap: I committed a real fix on a DIRTY
# PR branch — a specific SHA — stamped the task ``partial`` ("resolved on branch,
# awaiting tend push"), and walked, never confirming the SHA reached
# ``origin/main``. That is Handed-is-not-landed made physical: the actuation gap
# did not close, it moved downstream to a commit I stopped watching. This rule is
# the standing alarm at *my own commit*. It parses recent ``partial`` summaries
# for an own-committed SHA + ``#NNNN`` + an on-branch handoff marker, asks git
# whether that exact SHA is an ancestor of ``origin/main`` (the same
# ``merge-base --is-ancestor`` oracle the merge-truth chokepoint uses, here keyed
# by SHA via :func:`is_branch_merged_into_main`), and — only when the SHA is *not*
# an ancestor AND the PR has *not* otherwise merged (squash) AND the claim has aged
# past a grace window — routes a return-and-verify finding through the
# audit→directive bridge so it cannot be shelved. It self-extinguishes the
# instant the SHA lands (becomes an ancestor) or the PR merges by any path. It
# adds no push capability and respects the Tend-sole-pusher invariant: the only
# owned act it forces is *looking*.

# Case-insensitive substrings a ``partial`` ``result_summary`` carries when I
# resolved a PR on its branch and handed the push to Tend. A summary containing
# one of these is an *on-branch handoff* — the precondition for a SHA I committed
# but never confirmed landed.
_ON_BRANCH_HANDOFF_MARKERS: tuple[str, ...] = (
    Marker.AWAITING_TEND_PUSH,
    # A delivered-but-unmerged lap (proposal 091) is still an on-branch handoff:
    # I pushed the SHA to the PR branch but never confirmed it landed on main.
    # Named explicitly so the marker split does not rely on "resolved on branch"
    # co-location to keep matching.
    Marker.DELIVERED_AWAITING_MERGE,
    # An owned-push lap (proposal 098) is the strongest on-branch handoff of
    # all: the SHA is still only local, so rule 14's unconfirmed-own-landing
    # check must keep matching it after the §5-push marker swap.
    Marker.OWNED_PUSH_AVAILABLE,
    # A push-unconfirmed lap (proposal 099) is an on-branch handoff whose SHA
    # never confirmed landing on ``origin/<pr_branch>`` — the exact
    # unconfirmed-own-landing case rule 14 exists for. Named alongside its
    # OWNED_PUSH_AVAILABLE sibling so the marker split costs rule 14 no coverage.
    Marker.PUSH_UNCONFIRMED,
    # Keyword-bearing handoff prose lives in ``narration_cues`` so the record-time
    # scrub protects it byte-for-byte (#2970); the keyword-free "committed on …"
    # siblings stay local.
    *_ON_BRANCH_RESOLVED_PHRASES,
    "committed on this branch",
    "committed on branch",
    "committed on the branch",
)

# Default number of audit cycles an unconfirmed own-landing claim must persist
# while its SHA stays off ``origin/main`` before it is eligible to flag. Strictly
# > 0 so a fresh handoff is never flagged the same cycle it is first observed —
# the grace window is fair to Tend's mechanical push latency. Overridable via
# ``[audit] unconfirmed_landing_age_cycles``.
_UNCONFIRMED_LANDING_DEFAULT_AGE_CYCLES = 3

# Sidecar recording ``{sha: first-seen audit iteration}`` for unconfirmed
# own-landing claims, so age can be measured in cycles. Pruned each cycle to the
# currently-unlanded SHA set so it stays bounded and self-extinguishes once a SHA
# lands. Isolates its sidecar I/O under a file lock.
_UNCONFIRMED_LANDING_SIDECAR = "unconfirmed_landing_first_seen.json"

# Per-rule cap; mirrors :data:`MAX_FINDINGS_PER_RULE` so a burst of unconfirmed
# landings in one window cannot monopolise the cycle's findings budget.
_UNCONFIRMED_LANDING_PER_RULE_CAP = MAX_FINDINGS_PER_RULE


def _load_unconfirmed_landing_seen(path: Path) -> dict[str, int]:
    """Read the ``{sha: first-seen iteration}`` map from the sidecar.

    Returns an empty dict when the file is absent, unreadable, corrupt, or not
    a JSON object of ``str -> int`` — the rule then re-seeds from the current
    cycle rather than crashing on a damaged sidecar. Booleans are rejected as
    values (``bool`` subclasses ``int``) so a corrupted ``true``/``false`` is
    never mistaken for iteration 1/0.
    """
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Skipping corrupted unconfirmed-landing sidecar %s", path)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: v
        for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
    }


@runtime_checkable
class UnconfirmedOwnLandingBackend(Protocol):
    """The unconfirmed-own-landing first-seen sidecar surface (proposal 106).

    One *atomic* read-modify-write behind one seam so the ``{sha: first-seen
    audit iteration}`` map that :func:`check_unconfirmed_own_landing` uses to age
    an own-landing claim can live in ``unconfirmed_landing_first_seen.json``
    (default) or in ``state.db`` behind ``[paths] sqlite_state_enabled`` with no
    call-site branching. Like the sibling
    :class:`~maestro.directive_ledger.DroppedDirectiveBackend`, the single method
    owns the whole load-mutate-write; the rule keeps the policy (which SHAs are
    still unlanded) and the backend executes the prune-and-touch inside one lock /
    transaction. ``@runtime_checkable`` enables a structural ``isinstance`` check
    in the agent's selection tests.
    """

    def prune_and_touch(
        self, observed: list[str], iteration: int,
    ) -> dict[str, int]:
        """Prune the stored map to *observed*, touching newly-seen SHAs.

        Load the current ``{sha: first_seen}`` map, compute
        ``{sha: seen.get(sha, iteration) for sha in observed}`` — dropping every
        SHA no longer in *observed* (it landed or settled) and stamping a
        first-seen of *iteration* on any SHA seen for the first time — persist
        that map, and return it for the caller's findings loop.
        """
        ...


def load_all_unconfirmed_landing_seen(data_dir: Path) -> dict[str, int]:
    """Return the ``{sha: first-seen iteration}`` map — the SQLite seed source.

    Reuses the corrupt-tolerant :func:`_load_unconfirmed_landing_seen` over
    ``<data_dir>/unconfirmed_landing_first_seen.json``, so enabling
    ``[paths] sqlite_state_enabled`` seeds the store from the in-flight sidecar.
    Has a real call site (the
    :class:`~maestro.state_store.SqliteUnconfirmedOwnLandingStore` one-time
    seed), so it does not trip the self-review uncalled-symbol gate. Read-only,
    zero SDK cost.
    """
    return _load_unconfirmed_landing_seen(data_dir / _UNCONFIRMED_LANDING_SIDECAR)


class JsonUnconfirmedOwnLandingStore:
    """JSON-backed :class:`UnconfirmedOwnLandingBackend` — the default, flag-off store.

    Binds a ``data_dir`` and keeps the historical ``file_lock``-guarded
    load-prune-write body verbatim: :meth:`prune_and_touch` reads the sidecar via
    :func:`_load_unconfirmed_landing_seen`, recomputes the pruned/touched map, and
    ``atomic_write``s it back — exactly the block formerly inline in
    :func:`check_unconfirmed_own_landing`. The sidecar file is
    ``<data_dir>/unconfirmed_landing_first_seen.json``. Mirrors the
    :class:`~maestro.directive_ledger.JsonlDroppedDirectiveLedger` idiom.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def prune_and_touch(
        self, observed: list[str], iteration: int,
    ) -> dict[str, int]:
        sidecar = self._data_dir / _UNCONFIRMED_LANDING_SIDECAR
        with file_lock(sidecar):
            seen = _load_unconfirmed_landing_seen(sidecar)
            # Track first-seen only for still-unlanded SHAs; a landed SHA
            # self-extinguishes (drops from the sidecar) in one pass.
            updated = {sha: seen.get(sha, iteration) for sha in observed}
            atomic_write(sidecar, json.dumps(updated, sort_keys=True))
        return updated


def _summary_confirms_others_landing(summary: str) -> bool:
    """True when *summary* confirms another party's landing (``CONFIRMED``).

    Shared by rules 14 and 20 so the twins cannot drift on this admission
    check. :func:`classify_landing_attribution` only attributes success
    outcomes, so the outcome is normalised to ``success`` here purely to
    engage its CONFIRMED-marker logic against the *summary* — the markers,
    not the task's non-success outcome, carry the "someone else already
    landed it" signal both rules skip.
    """
    return classify_landing_attribution(
        {"outcome": "success", "result_summary": summary},
    ) == "CONFIRMED"


# Preserved delivery actuator (proposal 105 §5d out-of-scope): the sole owner of
# committed-SHA-ancestry verification and the PR-less fire-or-close directive — a
# live delivery arm, not a verdict-*labeling* phantom rule the git-grounded landing
# authority (``decide_outcome``) subsumed alongside rules 10/13/15.
def check_unconfirmed_own_landing(
    tasks: list[TaskRecord],
    backend: UnconfirmedOwnLandingBackend,
    iteration: int,
    *,
    age_cycles: int = _UNCONFIRMED_LANDING_DEFAULT_AGE_CYCLES,
    enabled: bool = True,
    is_ancestor: Callable[[str], bool] | None = None,
    is_pr_merged: Callable[[int], bool] | None = None,
    settled_prs: frozenset[int] = frozenset(),
) -> list[AuditFinding]:
    """Rule 14 — my own committed SHA still not an ancestor of origin/main.

    Scans *tasks* for ``partial`` records whose ``result_summary`` carries an
    on-branch handoff marker (:data:`_ON_BRANCH_HANDOFF_MARKERS`) together with a
    committed-SHA (:data:`_COMMITTED_SHA_RE`) — the fingerprint of "I resolved
    this on the branch and handed the push to Tend". The PR is *optional*: a
    summary citing a ``#NNNN`` (or a ``Remediate PR #N`` goal) binds the SHA to
    that PR, while a truly PR-less on-branch handoff (no ``#NNNN`` in the summary
    and no remediation goal) is admitted with ``pr_number = None`` so SHA
    ancestry alone decides it. A goal-bound remediation handoff that merely omits
    the number from its summary is deliberately still skipped. A task whose
    summary :func:`classify_landing_attribution` labels ``CONFIRMED`` is skipped:
    that is a confirmation of *someone else's* land, not a SHA I committed and
    must verify.

    For each candidate SHA the rule asks *is_ancestor* (defaulting to
    :func:`is_branch_merged_into_main`, i.e. ``git merge-base --is-ancestor
    <sha> origin/main`` — the same oracle the merge-truth chokepoint uses, here
    keyed by SHA)
    whether the commit has reached ``origin/main``, and *is_pr_merged*
    (defaulting to :func:`_git_pr_merged_default`) whether the PR merged by any
    path (e.g. a squash that rewrote the SHA) — the latter only for a PR-bearing
    candidate; a PR-less one is decided by ancestry alone. A finding is emitted
    only when the SHA is **not** an ancestor AND (for a PR-bearing candidate) the
    PR is **not** merged AND the claim has persisted at least *age_cycles* audit
    cycles.

    Age is measured in cycles via a first-seen map owned by *backend* (the JSON
    sidecar :data:`_UNCONFIRMED_LANDING_SIDECAR` by default, or ``state.db`` under
    ``[paths] sqlite_state_enabled``) keyed by SHA; the map is
    pruned each cycle to the currently-unlanded SHA set, so a SHA
    first seen this cycle has age 0 and is never flagged — the grace window is
    fair to Tend's push latency. Self-extinguishing: the moment the SHA becomes
    an ancestor (or the PR merges) it drops out of the unlanded set and the
    finding vanishes. Setting *enabled* to ``False`` (the
    ``[audit] unconfirmed_landing_enabled`` master switch) short-circuits to an
    empty list with no I/O. Capped at
    :data:`_UNCONFIRMED_LANDING_PER_RULE_CAP` and ordered by PR number ascending
    (oldest-first doctrine), PR-less candidates last, by SHA.

    *settled_prs* (proposal 097) pre-filters a PR-bearing candidate whose bound
    PR the standing settlement reconciler has observed settled (merged *or*
    closed) via the ``is_pr_settled`` chokepoint: it is excluded from the
    unlanded set — and thereby pruned from the first-seen map — before the merge
    predicate runs. A
    truly PR-less candidate (``pr is None``) is never in the set and still fires
    on SHA ancestry alone. The empty default keeps every legacy call identical.

    Zero SDK cost; the only I/O is the backend's ``prune_and_touch`` (a locked
    sidecar read/write, or one SQLite transaction) plus the two merge-truth
    predicates' bounded git subprocesses (one per SHA / PR).
    """
    if not enabled:
        return []
    ancestor_pred = (
        is_ancestor if is_ancestor is not None else is_branch_merged_into_main
    )
    merged_pred = (
        is_pr_merged if is_pr_merged is not None else _git_pr_merged_default
    )

    # Group candidate handoffs by the committed SHA they cite. The PR a SHA
    # resolved is the task's *remediation target* — the ``#N`` in its
    # ``Remediate PR #N: …`` goal — not the numeric minimum of every ``#NNNN``
    # the summary happens to mention. A real handoff summary references several
    # numbers ("Resolved on branch for PR #2638; supersedes #2100; committed
    # ac48a769"); ``min`` would bind the SHA to #2100, then run the
    # self-extinguish merge-truth check against that unrelated (often
    # long-merged) PR and misattribute the surfaced finding. Falling back to
    # ``min(issues)`` only when the goal carries no remediation target keeps
    # legacy/self-directed handoffs deterministic.
    sha_pr: dict[str, int | None] = {}
    sha_cites: dict[str, list[str]] = {}
    for task in tasks:
        if task.outcome != "partial":
            continue
        summary = task.result_summary
        if not isinstance(summary, str) or not summary:
            continue
        lowered = summary.lower()
        if not any(marker in lowered for marker in _ON_BRANCH_HANDOFF_MARKERS):
            continue
        # Skip confirmations of another's land — a summary saying the cure was
        # already merged before I touched it carries no SHA of mine to verify.
        if _summary_confirms_others_landing(summary):
            continue
        shas = set(_COMMITTED_SHA_RE.findall(lowered))
        if not shas:
            continue
        issues = extract_issue_numbers(summary)
        target_pr = pr_number_from_remediation_goal(task.goal)
        # PR-optional admission of the SHA-bearing handoff:
        #  * summary cites a ``#NNNN`` → bind as before, preferring the
        #    remediation-goal target over the lowest cited issue (rationale
        #    above);
        #  * no summary ``#NNNN`` but the goal *is* a ``Remediate PR #N`` lap →
        #    a goal-bound remediation handoff that merely omitted the number
        #    from its summary — deliberately out of scope, keep skipping it;
        #  * no summary ``#NNNN`` and no goal PR → a truly PR-less on-branch
        #    handoff (a committed SHA nobody is watching); admit it with
        #    ``pr_number = None`` so SHA ancestry alone decides its fate and the
        #    finding routes to the fire-or-close directive arm.
        pr_number: int | None
        if issues:
            pr_number = target_pr if target_pr is not None else min(issues)
        elif target_pr is not None:
            continue
        else:
            pr_number = None
        tid = task.task_id
        for sha in shas:
            sha_pr.setdefault(sha, pr_number)
            cites = sha_cites.setdefault(sha, [])
            if isinstance(tid, str) and tid:
                cites.append(tid)
    if not sha_pr:
        return []

    # Consult merge-truth per candidate. A SHA already an ancestor of main, or a
    # PR merged by another path (e.g. a squash that rewrote the SHA), is honest —
    # it drops out of the unlanded set and self-extinguishes.
    unlanded: list[str] = []
    for sha in sha_pr:
        if ancestor_pred(sha):
            continue
        pr = sha_pr[sha]
        # Standing-settlement pre-filter (proposal 097): a PR-bearing candidate
        # whose PR the reconciler observed settled is dropped from ``unlanded``
        # (and thus pruned from the sidecar) before the merge predicate — so it
        # costs no git subprocess. A PR-less candidate is never in the set.
        if pr is not None and pr in settled_prs:
            continue
        # A PR-less candidate has no PR to consult — SHA ancestry alone decides
        # it. A PR-bearing one still self-extinguishes when its PR merged by
        # another path (e.g. a squash that rewrote the SHA).
        if pr is not None and merged_pred(pr):
            continue
        unlanded.append(sha)

    # The backend owns the atomic load-prune-write: it reads the current
    # first-seen map, drops every SHA no longer unlanded (it landed or settled),
    # stamps a first-seen of *iteration* on any newly-seen SHA, persists the map,
    # and returns it for the findings loop below.
    updated = backend.prune_and_touch(unlanded, iteration)
    findings: list[AuditFinding] = []
    # Oldest-first doctrine: PR-bearing candidates first, ordered by PR
    # number; PR-less candidates (``None`` sorts as key 0 behind the
    # ``is None`` flag) after, ordered by SHA. Both branches break ties on
    # the SHA, so the order is fully deterministic.
    for sha in sorted(
        unlanded, key=lambda s: (sha_pr[s] is None, sha_pr[s] or 0, s),
    ):
        age = iteration - updated[sha]
        if age < age_cycles:
            continue
        pr_number = sha_pr[sha]
        if pr_number is None:
            # PR-less handoff: omit any ``#NNNN`` so the forced arm routes
            # it to the fire-or-close directive, and embed the SHA in a
            # bare ``committed {sha}`` cue so it stays recoverable there.
            statement = (
                f"A fix I committed {sha} is still not an ancestor of "
                f"origin/main {age} audit cycles after I stamped it "
                f"resolved-on-branch — handed is not landed; land it via a "
                f"PR, or retire the branch/plan."
            )
        else:
            statement = (
                f"A fix I committed ({sha}) for PR #{pr_number} is still "
                f"not an ancestor of origin/main {age} audit cycles after "
                f"I stamped it resolved-on-branch — handed is not landed; "
                f"return and verify the SHA reached main, or land it."
            )
        findings.append(AuditFinding(
            kind="unconfirmed_landing",
            statement=statement,
            cites=sha_cites[sha][:MAX_CITES_PER_FINDING],
            rule="unconfirmed_own_landing",
        ))
        if len(findings) >= _UNCONFIRMED_LANDING_PER_RULE_CAP:
            break
    return findings


# ---------------------------------------------------------------------------
# Rule 20: undercredited_landing — my committed SHA git-proves landed, yet the
# task that authored it was stamped non-success (the affirmative twin of rule 14)
# ---------------------------------------------------------------------------
#
# Every existing landing-audit rule catches OVER-claiming: unconfirmed-own-landing
# (rule 14) and the self-committed-SHA bind both contest a verdict that claims
# more than git shows — a ``success`` (or
# a resolved-on-branch ``partial``) whose SHA never reached ``origin/main``. Not
# one watches the *opposite* direction, and that is exactly where the disposal-
# treadmill gravity lives: a real 2-parent merge that made ``origin/main`` a true
# ancestor — the single most authored CAN'T-to-DID — was pre-emptively stamped
# ``partial`` while ten cheap writable-half disposals scored ``success``. A
# measurement apparatus that can only *downgrade* landing-work and never once
# *credits* a confirmed landing is all-downside on the exact act it should
# reward, and that asymmetry actively trains the treadmill. This rule is the
# affirmative complement of rule 14: it scans a non-``success`` task's
# ``result_summary`` for an own-committed SHA (:func:`extract_self_committed_shas`
# — the same FM4 extractor the self-committed bind uses), asks the same
# ``merge-base --is-ancestor`` oracle rule 14 uses (:func:`is_branch_merged_into_main`,
# keyed by SHA) whether that commit reached ``origin/main``, and — only when it
# DID — surfaces one self-extinguishing ``[audit]`` finding: "you authored a
# landing and called it nothing." It is deliberately **not** an auto-relabel: the
# ``partial`` may be correct for other goals, so the rule only surfaces the
# under-credit for the LLM to judge, never rewrites the verdict. Default-off,
# git-grounded, reusing machinery already trusted; the only I/O is the bounded
# git subprocess per distinct SHA (memoized within the scan).

# Per-rule cap; mirrors :data:`MAX_FINDINGS_PER_RULE` so a burst of under-credited
# landings in one window cannot monopolise the cycle's findings budget.
_UNDERCREDITED_LANDING_PER_RULE_CAP = MAX_FINDINGS_PER_RULE


def check_undercredited_landing(
    tasks: list[TaskRecord],
    *,
    enabled: bool = False,
    is_ancestor: Callable[[str], bool] | None = None,
) -> list[AuditFinding]:
    """Rule 20 — a non-success task whose own committed SHA git-proves landed.

    The affirmative twin of :func:`check_unconfirmed_own_landing` (rule 14):
    where rule 14 flags a ``partial`` handoff whose committed SHA git shows is
    **not** an ancestor of ``origin/main`` (over-claim: handed-is-not-landed),
    this flags a non-``success`` task whose committed SHA git shows **is** an
    ancestor (under-claim: landed-yet-called-nothing). It closes the sensor
    asymmetry that trains the disposal treadmill — every other landing rule can
    only downgrade a landing claim; none rewards a confirmed one.

    Scans *tasks* for records whose ``outcome`` is **not** ``success`` and whose
    ``result_summary`` cites an own-committed SHA
    (:func:`extract_self_committed_shas`, the shared FM4 extractor). For each
    distinct cited SHA the rule asks *is_ancestor* (defaulting to
    :func:`is_branch_merged_into_main`, i.e. ``git merge-base --is-ancestor
    <sha> origin/main`` — the same oracle rule 14 uses, here keyed by SHA)
    whether the commit reached ``origin/main``. A finding
    (``kind='undercredited_landing'``) is emitted **only** when the SHA **is** an
    ancestor; the ancestry probe is memoized within the scan so an unlanded SHA
    cited by several tasks costs one subprocess, and each landed SHA yields at
    most one finding (deduped across tasks).

    Self-extinguishing: it emits nothing when the outcome is ``success`` (no
    under-claim to make), when no own-committed SHA is cited (nothing to verify),
    when the summary confirms another party's landing
    (:func:`classify_landing_attribution` → ``CONFIRMED``, the same skip rule 14
    applies — conservatively, an own-landing summary that also carries a
    CONFIRMED phrase is skipped too), or when the cited SHA is not an ancestor
    of ``origin/main`` (rule 14's territory, not this one). Setting *enabled*
    to ``False`` (the ``[audit] undercredited_landing_enabled`` master switch,
    default-off per the opt-in audit-rule convention) short-circuits to an
    empty list with no I/O.
    Deliberately **not** an auto-relabel — the ``partial`` may be correct for
    other goals — so the rule only surfaces the under-credit as an ``[audit]``
    question for the LLM to judge. Capped at
    :data:`_UNDERCREDITED_LANDING_PER_RULE_CAP`, oldest-first by corpus order
    then SHA. Zero SDK cost; the only I/O is the bounded git subprocess per
    distinct SHA.
    """
    if not enabled:
        return []
    ancestor_pred = (
        is_ancestor if is_ancestor is not None else is_branch_merged_into_main
    )
    findings: list[AuditFinding] = []
    emitted: set[str] = set()
    ancestry_memo: dict[str, bool] = {}
    for task in tasks:
        if task.outcome == "success":
            continue
        summary = task.result_summary
        if not isinstance(summary, str) or not summary:
            continue
        # A confirmation of another's land is not an own landing to credit —
        # the same skip rule 14 applies, taken before any ancestry probe.
        if _summary_confirms_others_landing(summary):
            continue
        shas = extract_self_committed_shas(summary)
        if not shas:
            continue
        tid = task.task_id
        cites = [tid] if isinstance(tid, str) and tid else []
        for sha in sorted(shas):
            if sha in emitted:
                continue
            if sha not in ancestry_memo:
                ancestry_memo[sha] = ancestor_pred(sha)
            if not ancestry_memo[sha]:
                continue
            emitted.add(sha)
            findings.append(AuditFinding(
                kind="undercredited_landing",
                statement=(
                    f"A task stamped '{task.outcome}' committed {sha}, which "
                    f"git proves is an ancestor of origin/main — you authored a "
                    f"landing and stamped it non-success. Confirm the landing "
                    f"and credit it; a real merge is throughput, not nothing."
                ),
                cites=cites[:MAX_CITES_PER_FINDING],
                rule="undercredited_landing",
            ))
            if len(findings) >= _UNDERCREDITED_LANDING_PER_RULE_CAP:
                return findings
    return findings


# ---------------------------------------------------------------------------
# Rule 21: reconcile_near_miss — a ≤N-behind git-unlanded own PR I called
# 'awaiting' without authoring the reconcile SHA
# ---------------------------------------------------------------------------
#
# The disposal treadmill has an actuator gap keyed exactly to my most-named,
# least-fixed failure. The >N-behind band has the OWNED-DISPOSAL close actuator
# and the ahead-of-origin band has its push guard, but the ≤N-behind
# reconcile-near band — the one the owned-act contract claims (merge origin/main into
# the branch, push, land) — has no sensor and no routed demand, so
# "partial: awaiting review/merge" stays structurally available for the one case
# I own outright: a cleanly-reconcilable own PR a few commits behind main. Rule
# 21 is the *pre-landing* complement of rule 20 (:func:`check_undercredited_
# landing`, which credits a landing I authored but under-claimed): where rule 20
# fires when a lineage SHA git-proves it landed, rule 21 fires when the reconcile
# was *measured but never authored* — the PR is git-unlanded, measured ≤ N behind
# origin/main, and no cited own-committed SHA is an ancestor of origin/main. Only
# a new branch SHA landing on origin/main extinguishes it (via the same ancestry
# oracle rule 20 uses), so it demands the reconcile I keep measuring and never
# performing.

# Per-rule cap; mirrors :data:`MAX_FINDINGS_PER_RULE` so a burst of near-miss
# PRs in one window cannot monopolise the cycle's findings budget.
_RECONCILE_NEAR_MISS_PER_RULE_CAP = MAX_FINDINGS_PER_RULE


def check_reconcile_near_miss(
    tasks: list[TaskRecord],
    *,
    enabled: bool = False,
    max_behind: int = 10,
    is_settled: Callable[[int], bool] | None = None,
    commits_behind: Callable[[int], int | None] | None = None,
    is_ancestor: Callable[[str], bool] | None = None,
    owed_act: Callable[[int], OwedAct | None] | None = None,
    on_disposal_offer: Callable[[int], None] | None = None,
) -> list[AuditFinding]:
    """Rule 21 — a ≤N-behind git-unlanded own PR called 'awaiting', no reconcile SHA.

    The pre-landing complement of :func:`check_undercredited_landing` (rule 20).
    Where rule 20 fires when a lineage SHA git-proves a reconcile *landed* (a
    landing authored but under-claimed), this fires when the reconcile was
    *measured but never authored* — the actuator gap in the ≤N-behind band that
    keeps "partial: awaiting review/merge" structurally available for exactly the
    case Maestro owns: a cleanly-reconcilable own PR a few commits behind
    ``origin/main``.

    Scans *tasks* for ``partial`` remediation records, extracts each cited
    ``#NNNN`` (:func:`extract_issue_numbers`), and emits one
    :class:`AuditFinding` (``kind='reconcile_near_miss'``) per distinct PR for
    which **all three** hold:

    (a) *is_settled(pr)* is ``False`` — the PR is git-unlanded (still open;
        neither merged nor closed);
    (b) *commits_behind(pr)* reports a distance ``d`` with ``1 <= d <=
        max_behind`` — genuinely behind (a reconcile is owed) but within the
        near band (a level ``d == 0`` is not "behind", an unknown ``None`` cannot
        be measured, and ``d > max_behind`` is the disposal band's territory,
        not this one); and
    (c) :func:`extract_self_committed_shas` over the summary yields **no** SHA
        that *is_ancestor* confirms is an ancestor of ``origin/main`` — the
        reconcile was never authored (the absent-SHA failure case) or an
        authored SHA has not landed. Any lineage SHA confirmed an ancestor
        self-extinguishes the finding: a landed reconcile.

    The seams are dependency-injected callables so the rule is pure under test —
    *is_settled* and *commits_behind* have **no** module-level default (they need
    the agent's PR→branch wiring), so a missing seam short-circuits to an empty
    list with no I/O; *is_ancestor* defaults to
    :func:`is_branch_merged_into_main` (the ``git merge-base --is-ancestor``
    oracle rule 20 uses, keyed by SHA). Every probe is memoized within the scan
    (per PR / per SHA) so a PR cited by several tasks costs one settlement + one
    distance read, and each PR yields at most one finding (deduped across tasks).

    **Owed-act binding (proposal 137).** The optional *owed_act* seam reports
    what the PR owes right now, from the single
    :func:`~maestro.pr_owed_act.classify_pr_owed_act` partition. When it reads
    :attr:`~maestro.pr_owed_act.OwedAct.CODE_OWED` — a blocker the loop clears
    by writing code, one commit away — the finding drops its ``or gh pr close
    #N`` clause and states the reconcile demand alone: closing is not a co-equal
    option while the fix is still owned and cheap. A ``None`` seam (the flag-off
    path), a ``None`` reading (unmeasurable) and every other act reproduce
    today's statement byte-for-byte. The seam needs no memo of its own: it is
    consulted only on the one path that goes on to ``emitted.add(pr)``, and
    ``emitted`` already skips every later citation of that PR, so a cited PR
    reaches it at most once per scan.

    The optional *on_disposal_offer* callback is invoked with the PR number
    exactly when the emitted finding carries the ``gh pr close #N`` clause —
    once per emitted PR — so the owed-act divergence sensor can measure close
    offers made on a code-owed PR. ``None`` records nothing; the finding text is
    never affected.

    Self-extinguishing: it emits nothing when the outcome is not ``partial``,
    when no ``#NNNN`` is cited, when the PR is settled, when the distance is
    outside ``[1, max_behind]`` or unmeasurable, or when a cited SHA landed.
    Setting *enabled* to ``False`` (the ``[audit] reconcile_near_miss_enabled``
    master switch, default-off per the opt-in audit-rule convention)
    short-circuits to an empty list with no I/O. Not an auto-relabel — the rule
    only surfaces the un-authored reconcile as an ``[audit]`` question routed
    into a ``Reconcile+push #N or gh pr close #N`` directive for the LLM to act
    on. Capped at :data:`_RECONCILE_NEAR_MISS_PER_RULE_CAP`, oldest-first by
    corpus order then PR number.
    """
    if not enabled:
        return []
    if is_settled is None or commits_behind is None:
        return []
    ancestor_pred = (
        is_ancestor if is_ancestor is not None else is_branch_merged_into_main
    )
    findings: list[AuditFinding] = []
    emitted: set[int] = set()
    settled_memo: dict[int, bool] = {}
    behind_memo: dict[int, int | None] = {}
    ancestry_memo: dict[str, bool] = {}
    for task in tasks:
        if task.outcome != "partial":
            continue
        summary = task.result_summary
        if not isinstance(summary, str) or not summary:
            continue
        pr_nums = extract_issue_numbers(summary)
        if not pr_nums:
            continue
        tid = task.task_id
        cites = [tid] if isinstance(tid, str) and tid else []
        shas = extract_self_committed_shas(summary)
        for pr in sorted(pr_nums):
            if pr in emitted:
                continue
            # (a) PR still open (git-unlanded) — a settled PR (merged or closed)
            # owes no reconcile.
            if pr not in settled_memo:
                settled_memo[pr] = is_settled(pr)
            if settled_memo[pr]:
                continue
            # (b) genuinely behind, but within the near band.
            if pr not in behind_memo:
                behind_memo[pr] = commits_behind(pr)
            behind = behind_memo[pr]
            if behind is None or not 1 <= behind <= max_behind:
                continue
            # (c) no cited own-committed SHA has landed on origin/main.
            landed = False
            for sha in sorted(shas):
                if sha not in ancestry_memo:
                    ancestry_memo[sha] = ancestor_pred(sha)
                if ancestry_memo[sha]:
                    landed = True
                    break
            if landed:
                continue
            # Proposal 137: while the PR is code-owed, offering ``gh pr close``
            # alongside the reconcile presents give-up as co-equal to a fix the
            # loop owns. Drop the clause; the rest of the statement is verbatim.
            code_owed = (
                owed_act is not None and owed_act(pr) is OwedAct.CODE_OWED
            )
            demand = (
                f"Reconcile+push #{pr}" if code_owed
                else f"Reconcile+push #{pr} or gh pr close #{pr}"
            )
            emitted.add(pr)
            if on_disposal_offer is not None and not code_owed:
                on_disposal_offer(pr)
            findings.append(AuditFinding(
                kind="reconcile_near_miss",
                statement=(
                    f"{demand}: it is {behind} "
                    f"commits behind origin/main (≤{max_behind}, your reconcile "
                    f"lane) and git-unlanded, yet a task stamped 'partial' called "
                    f"it awaiting — you measured the reconcile but authored no "
                    f"landing SHA. Only a new SHA on origin/main clears this."
                ),
                cites=cites[:MAX_CITES_PER_FINDING],
                rule="reconcile_near_miss",
            ))
            if len(findings) >= _RECONCILE_NEAR_MISS_PER_RULE_CAP:
                return findings
    return findings


# ---------------------------------------------------------------------------
# Rule 22: green_ready_handoff — a merge-ready owned PR never handed off
# ---------------------------------------------------------------------------
#
# Every prior handoff arm fires *late*. The escalation bridge hands a PR up only
# after its unlanded lineage exhausts the lap budget; rule 14 fires on a
# verdict / SHA contradiction after the fact. None fires on the
# plainest, earliest signal — an owned PR that has gone fully green (every CI
# check SUCCESS, no code-clearable merge blocker) and is merge-ready, waiting on
# nothing but Thomas's merge, yet was never handed off. That is exactly the state
# the treadmill hides in: keeping the green PR "mine" by lapping "awaiting"
# instead of asking for the merge. This rule closes the gap deterministically —
# the instant an owned, git-unlanded PR reads merge-ready and carries no
# ``pr_number``-keyed handoff standdown, its dispatcher fires the existing
# :func:`~maestro.escalation_bridge.deliver_handoff` actuator exactly once. The
# standdown that call records self-extinguishes the rule (a PR already standing
# down is filtered out here, so ``deliver_handoff`` is never re-invoked) and is
# auto-released by the Tend route's ``prune_resolved_standdowns`` the moment git
# shows the PR merged/closed — the same forced-arm delivery + keyed-release path
# rule 14 uses, no new capability and no new push.


def green_ready_handoff_prs(
    owned_pr_numbers: Iterable[int],
    *,
    enabled: bool = True,
    is_green_ready: Callable[[int], bool],
    has_standdown: Callable[[int], bool],
) -> list[int]:
    """Rule 22 — owned merge-ready PRs never handed off, no standdown yet.

    Filters *owned_pr_numbers* (the open ``maestro/evolve/*`` evolution PRs) to
    the ascending, de-duplicated subset for which **both** hold:

    (a) *has_standdown(pr)* is ``False`` — no ``pr_number``-keyed handoff
        standdown exists yet, so the PR was never handed up. This is the
        self-extinguishing gate: once :func:`~maestro.escalation_bridge.deliver_handoff`
        records the standdown, the PR is filtered out here on every later cycle,
        so ``deliver_handoff`` is never re-invoked; and
    (b) *is_green_ready(pr)* is ``True`` — the PR is open (git-unlanded), the CI
        axis is clear (:func:`~maestro.pr_facts.ci_state_clears_ci_axis` —
        every check concluded SUCCESS, or no CI is configured), and no code-clearable merge blocker remains
        (the aggregate ``PRReadiness.ready_and_waiting`` fact): merge-ready,
        waiting only on Thomas. A CI-pending or CI-failing PR reads ``False`` and
        is skipped.

    The standdown gate is evaluated first so a PR already handed up costs no
    readiness observation. Returns the ascending list of PR numbers the
    dispatcher should route through ``deliver_handoff``, or ``[]`` when *enabled*
    is ``False`` (the ``[audit] green_ready_handoff_enabled`` master switch) or no
    owned PR qualifies. Non-positive PR numbers are ignored. Pure given its two
    injected oracles — it holds no push capability and consults the host only via
    the caller-supplied predicates, so the human-in-the-loop and Tend-sole-pusher
    invariants hold.
    """
    if not enabled:
        return []
    result: list[int] = []
    for pr in sorted({n for n in owned_pr_numbers if n > 0}):
        if has_standdown(pr):
            continue
        if not is_green_ready(pr):
            continue
        result.append(pr)
    return result


# ---------------------------------------------------------------------------
# Rule 22 delivery-truth bind — the handoff's 'delivered' is a re-read row
# ---------------------------------------------------------------------------
#
# ``green_ready_handoff_prs`` decides *which* PRs to hand off; the arm's actuator
# (:func:`~maestro.escalation_bridge.deliver_handoff`) sends the merge-or-close
# message and records its escalation-ledger standdown. But a merge-ready owned PR
# produced no durable stand-down artifact across dozens of cycles: the forced arm
# decided to fire yet left nothing the behavioral sensors could count, so the loop
# never closed the PR by its own hand — recognition-is-not-actuation living inside
# the machinery, not just the prose. :func:`confirm_handoff_delivery` binds the
# arm's 'delivered' truth to a *re-read* of the ``pr_number``-keyed
# ``stand_down_verdicts.jsonl`` row written during this delivery (a stale row from
# an earlier act does not count) — the one artifact narration cannot author —
# exactly as accepted push delivery binds to a remote-head advance rather than the
# push return. When the actuator leaves no such row, the bind persists it directly
# at zero SDK cost, so the terminal always leaves the durable artifact.


def _pr_keyed_stand_down_row_count(
    pr: int, backend: StandDownVerdictBackend,
) -> int:
    """Return how many stand-down *verdict* rows keyed to *pr* are on disk.

    Counts rows on *backend* whose ``pr`` matches and which carry a string
    ``recorded_at`` (stand-down verdicts, not ``{"pr", "released_at"}`` release
    rows), regardless of head SHA. :func:`confirm_handoff_delivery` snapshots
    this before and after its actuator so only a row written *during* that
    delivery counts — a stale row from an earlier act never satisfies the bind.
    The corrupted-tail tolerance lives with the backend; this caller-side
    non-positive-*pr* guard stays here. Read-only, zero SDK cost.
    """
    if pr <= 0:
        return 0
    return backend.verdict_count_for_pr(pr)


def _persist_handoff_stand_down_row(
    pr: int,
    backend: StandDownVerdictBackend,
    *,
    head_sha: str = "",
    now: str | None = None,
) -> None:
    """Append the ``pr``-keyed green-ready-handoff stand-down verdict row.

    The direct-persist arm of :func:`confirm_handoff_delivery`. Unlike
    :func:`maestro.remediation_tracker.record_stand_down` it persists even with an
    empty *head_sha*: the green_ready arm carries no remediation-lap head, and
    ``deliver_handoff``'s escalation-ledger standdown filters the PR out of
    :func:`green_ready_handoff_prs` on the very next cycle, so the row must land on
    the cycle the arm fires or never. An empty-head row is inert for
    :func:`maestro.remediation_tracker.find_stand_down` (which refuses an empty
    lookup head) yet still counts as the owned terminal act
    :func:`measure_owned_act_drought` and
    :func:`maestro.remediation_tracker.measure_standdown_production` credit.
    Delegates the append to *backend*; *now* injects the ISO timestamp (``None``
    stamps the current UTC time). A non-positive *pr* carries no key to match on
    and writes nothing.
    """
    if pr <= 0:
        return
    recorded_at = now if now is not None else datetime.now(UTC).isoformat()
    backend.append_verdict(
        StandDownRow(pr=pr, head_sha=head_sha, recorded_at=recorded_at),
    )


def confirm_handoff_delivery(
    backend: StandDownVerdictBackend,
    pr_number: int,
    deliver: Callable[[], bool],
    *,
    head_sha: str = "",
    now: str | None = None,
) -> bool:
    """Fire a green_ready_handoff actuator and bind its delivered-truth to a row.

    Invokes *deliver* — the :func:`maestro.escalation_bridge.deliver_handoff` call
    that sends one merge-or-close message and records the escalation-ledger
    standdown — then **re-reads** *backend* for verdict rows keyed to *pr_number*
    (:func:`_pr_keyed_stand_down_row_count`), comparing against a count taken
    just before the actuator ran. Returns ``True`` (the caller may stamp the arm
    'delivered') only when a verdict row was written *during this delivery*; a
    stale row left by an earlier stand-down, backfill, or since-released verdict
    never satisfies the bind. When no new row appeared — the actuator decided to
    fire but left no artifact, the recognition-is-not-actuation failure this bind
    closes — persists the ``pr_number``-keyed row directly at zero SDK cost
    (:func:`_persist_handoff_stand_down_row`) and returns ``False``: the caller
    must **not** report 'delivered', it backfilled the missing artifact instead.

    Mirrors the accepted push-delivery→remote-head-advance pattern: the terminal's
    'delivered' truth binds to an artifact narration cannot author (a persisted
    verdict row), never to the actuator's return value — *deliver*'s return is
    intentionally ignored, the re-read is the ground truth. *head_sha* stamps the
    informational head on a directly-persisted row (``""`` when the arm has none);
    *now* injects the ISO timestamp for that row. Zero SDK cost.
    """
    before = _pr_keyed_stand_down_row_count(pr_number, backend)
    deliver()
    if _pr_keyed_stand_down_row_count(pr_number, backend) > before:
        return True
    _persist_handoff_stand_down_row(
        pr_number, backend, head_sha=head_sha, now=now,
    )
    return False

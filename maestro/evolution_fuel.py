"""Gap issues: the evolution-fuel owner (proposal 155 step 1).

Self-initiated evolution spends only on closing a *gap*: something Maestro's
own health or a missing capability is blocked on. Each gap is a GitHub issue
labelled with one of :data:`GAP_LABELS`, and this module is the only place that
names those labels or the ``gap_opens`` ledger (the ``gap_issue_owner`` guard in
``tests/structural_guards.py``).

* :func:`open_gap_issue` is the one opener door: a closed owner-id grammar
  (:func:`parse_owner_id`), an opener admitted per prefix (:data:`GAP_PREFIXES`),
  idempotent per owner id, and never reopening an issue an operator closed by
  hand.
* :func:`close_gap_issue` is the one closer.
* :func:`classify_gap_trust` / :func:`admit_gap_issues` decide which labelled
  issues are trusted. A task session can run ``gh issue create --label
  gap:maestro`` as the bot, so a label alone proves nothing: an issue is trusted
  only when a D8 operator login applied the label, or a ``gap_opens`` row shows
  this door opened that exact issue for that owner id from an admitted opener.

The zero-SDK self-health detectors (proposal 155 step 2) are the door's first
callers, run by :func:`run_self_health_detectors`:

* :func:`observe_red_main` / :func:`detect_red_main` — a ``maestro:gate`` issue
  opens only once origin/main has read red on the same failing checks for
  ``[evolution_fuel] red_main_grace_hours``; the first-red clock is the one
  ``red_main_watch`` row in gaps.db, owned here. It closes only on a green read
  no live inherited hold is pinned to.
* :func:`open_gate_for_inherited_hold` — a proposal-124 inherited hold opens or
  reuses that same ``maestro:gate`` issue.
* :func:`detect_source_budget` — at most one ``maestro:source_budget:<path>``
  issue is open at a time, for the worst file at or over the split budget.

Step 4 makes a gap evolve's only fuel: :func:`select_evolution_fuel` picks one
trusted gap issue at zero SDK cost (lowest :func:`fuel_class_of`, oldest
first, past live-plan, attempt and cooldown skips) and :func:`render_gap_block`
renders it, with the loop state that bears on it, under
:data:`GAP_CONTEXT_CAPS` for ``propose_plan``.

Step 5's stuck-gap tripwire parks a gap that spent ``[evolution_fuel]
max_plans_per_gap`` under :data:`NEEDS_DECISION_LABEL` and hands Thomas one
build/buy/drop choice (:func:`format_gap_decision_ask`, :func:`apply_gap_decision`); clearing the
label restarts the attempt count from that ``unlabeled`` event.

No SDK call, no subprocess outside the :class:`~maestro.vcs.CodeHost` reads.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, get_args

from maestro.ci_diagnosis import (
    classify_ci_conclusions,
    failing_check_names,
    is_failing_conclusion,
    is_success_conclusion,
)
from maestro.evolution_submission import (
    count_gap_attempts,
    gap_cooldown_until,
    gap_has_live_plan,
)
from maestro.gap_store import parse_utc_stamp, utc_stamp
from maestro.source_budget import (
    SPLIT_BUDGET,
    SourceBand,
    SourceBudgetReading,
    split_due_readings,
)
from maestro.taint import (
    UNTRUSTED_DATA_RULE,
    UntrustedText,
    mark_untrusted,
    render_untrusted,
)

if TYPE_CHECKING:
    from maestro.ci_diagnosis import CheckRun
    from maestro.config import Config
    from maestro.gap_store import GapStore
    from maestro.issues import Issue, IssueTracker
    from maestro.journal import Journal
    from maestro.memory import EvolutionPlan
    from maestro.vcs import IssueClosure, IssueMeta

logger = logging.getLogger("maestro")

GapLabel = Literal["gap:maestro", "gap:capability"]
GAP_LABELS: tuple[GapLabel, ...] = get_args(GapLabel)

GapOpener = Literal["detector", "refusal_caller"]
GAP_OPENERS: tuple[GapOpener, ...] = get_args(GapOpener)

GapTrust = Literal["operator", "rederived", "unverified", "not_gap"]
GapOpenOutcome = Literal["opened", "reused", "reopened", "refused"]

_GAP_LABEL_PREFIX = "gap:"
_GAP_OPENS = "gap_opens"
#: The longest ``decision_ref`` the door stores.
MAX_DECISION_REF_CHARS = 200
#: The longest rendered evidence the door writes into an issue body; GitHub
#: rejects bodies over 65536 chars and gap bodies later feed prompts.
MAX_EVIDENCE_CHARS = 8000

#: Owner-id prefix -> (its one label, the openers admitted to open it).
GAP_PREFIXES: dict[str, tuple[GapLabel, frozenset[GapOpener]]] = {
    "gate": ("gap:maestro", frozenset({"detector"})),
    "source_budget": ("gap:maestro", frozenset({"detector"})),
    "toolchain": ("gap:maestro", frozenset({"refusal_caller"})),
    "capability": ("gap:capability", frozenset({"refusal_caller"})),
}

#: The fixed title summary per prefix: no free text ever reaches a title.
_TITLE_SUMMARY: dict[str, str] = {
    "gate": "the main quality gate is red",
    "source_budget": "a source file is at its split-due size",
    "toolchain": "a stream toolchain is missing",
    "capability": "a capability a stream needs is missing",
}
_TITLE_SEP = " — "

_SLUG = r"[a-z0-9][a-z0-9._-]{0,63}"
_PY_PATH = r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*/){0,15}[A-Za-z0-9_][A-Za-z0-9_.-]*\.py"
_OWNER_ID_FORMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gate", re.compile(r"maestro:gate")),
    ("source_budget", re.compile(rf"maestro:source_budget:{_PY_PATH}")),
    ("toolchain", re.compile(rf"maestro:toolchain:{_SLUG}")),
    (
        "capability",
        re.compile(rf"capability:(?:act_kind|completion_check):{_SLUG}"),
    ),
)
_MAX_OWNER_ID_CHARS = 200


@dataclass(frozen=True)
class GapOwnerId:
    """A parsed owner id: the text, its prefix, and the label it carries."""

    owner_id: str
    prefix: str

    @property
    def label(self) -> GapLabel:
        return GAP_PREFIXES[self.prefix][0]

    @property
    def openers(self) -> frozenset[GapOpener]:
        return GAP_PREFIXES[self.prefix][1]


@dataclass(frozen=True)
class GapOpenResult:
    outcome: GapOpenOutcome
    issue_number: int | None = None
    reason: str = ""


def is_gap_label(label: object) -> bool:
    """True when *label* is (or poses as) a ``gap:*`` label."""
    return isinstance(label, str) and label.strip().casefold().startswith(_GAP_LABEL_PREFIX)


def parse_owner_id(text: object) -> GapOwnerId | None:
    """Return the parsed owner id when *text* full-matches the closed grammar."""
    if not isinstance(text, str) or len(text) > _MAX_OWNER_ID_CHARS or ".." in text:
        return None
    for prefix, pattern in _OWNER_ID_FORMS:
        if pattern.fullmatch(text):
            return GapOwnerId(owner_id=text, prefix=prefix)
    return None


def gap_title(owner: GapOwnerId) -> str:
    return f"{owner.owner_id}{_TITLE_SEP}{_TITLE_SUMMARY[owner.prefix]}"


def owner_id_of_title(title: str) -> str | None:
    """Read the owner id back from a door-shaped title; ``None`` otherwise."""
    key, sep, summary = title.partition(_TITLE_SEP)
    owner = parse_owner_id(key)
    if not sep or owner is None or summary != _TITLE_SUMMARY[owner.prefix]:
        return None
    return owner.owner_id


def _render_evidence(evidence: str | UntrustedText) -> str:
    return render_untrusted(evidence) if isinstance(evidence, UntrustedText) else evidence


def _body(owner: GapOwnerId, rendered: str, decision_ref: str) -> str:
    return (
        f"Gap `{owner.owner_id}` ({owner.label}), opened by Maestro's evolution-fuel "
        f"door.\nDecision: {decision_ref}\n\n{rendered}\n"
    )


def _door_rows(store: GapStore, owner_id: str) -> dict[int, bool]:
    """Issue numbers this door opened for *owner_id* -> whether a row is still open."""
    rows = store.connection.execute(
        f"SELECT issue_number, closed_at FROM {_GAP_OPENS} WHERE owner_id = ?",
        (owner_id,),
    ).fetchall()
    out: dict[int, bool] = {}
    for number, closed_at in rows:
        out[int(number)] = out.get(int(number), False) or closed_at is None
    return out


def _door_states(
    tracker: IssueTracker, rows: dict[int, bool],
) -> dict[int, IssueMeta] | None:
    """Host read of every issue this door opened; ``None`` if any is unreadable.

    Driven by the ``gap_opens`` rows, not a label listing, so no listing page
    cap can hide the door's own issue (A1) or its operator close (A3). An
    issue the host reports gone (deleted) is dropped, so it
    neither blocks the owner id nor masquerades as a transient host failure.
    """
    states: dict[int, IssueMeta] = {}
    for number in sorted(rows):
        meta = tracker.meta(number)
        if meta is None:
            return None
        if meta.state != "gone":
            states[number] = meta
    return states


def _tampered(owner: GapOwnerId, meta: IssueMeta) -> bool:
    """Whether a door issue's gap label or door-shaped title was edited away."""
    return owner.label not in meta.labels or meta.title != gap_title(owner)


def _operator_hand_close(closure: IssueClosure, operators: set[str]) -> bool:
    """Whether a D8 operator closed the issue by hand (not via a commit)."""
    return closure.actor.casefold() in operators and not closure.via_commit


def _abandoned(closure: IssueClosure, operators: set[str]) -> bool:
    """Whether an operator hand close stands with no later D8 reopen.

    Walks the whole close/reopen history, so a bot ``close; reopen`` cycle
    after the operator's close cannot hide it behind a newer bot close.
    """
    if not closure.history:
        return (
            closure.closed and _operator_hand_close(closure, operators)
            and closure.reopened_by.casefold() not in operators
        )
    abandoned = False
    for event, actor, via_commit in closure.history:
        is_operator = actor.casefold() in operators
        if event == "closed" and is_operator and not via_commit:
            abandoned = True
        elif event == "reopened" and is_operator:
            abandoned = False
    return abandoned


def _record_open(
    store: GapStore, owner: GapOwnerId, opener: GapOpener, number: int,
    decision_ref: str, untrusted: bool, now: datetime,
) -> None:
    with store.transaction() as conn:
        conn.execute(
            f"INSERT INTO {_GAP_OPENS} (owner_id, label, opener, issue_number, "
            "decision_ref, untrusted_input, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (owner.owner_id, owner.label, opener, number, decision_ref,
             int(untrusted), utc_stamp(now)),
        )


def _comment_evidence(
    tracker: IssueTracker, owner: GapOwnerId, number: int, rendered: str,
    decision_ref: str,
) -> bool:
    """Post the current evidence on a reused or reopened door issue."""
    posted = tracker.comment(number, _body(owner, rendered, decision_ref))
    if not posted:
        logger.warning(
            "Gap issue #%d evidence comment failed (%s); retried next tick",
            number, decision_ref,
        )
    return posted


def _note_new_evidence(
    store: GapStore, owner: GapOwnerId, opener: GapOpener, number: int,
    rendered: str, decision_ref: str, untrusted: bool, *,
    tracker: IssueTracker, now: datetime,
) -> None:
    """Comment a reused issue's evidence once per new ``decision_ref``.

    The row is written only after the comment posts, so a failed comment is
    retried on the next tick rather than lost.
    """
    seen = store.connection.execute(
        f"SELECT 1 FROM {_GAP_OPENS} WHERE owner_id = ? AND issue_number = ? "
        "AND decision_ref = ? AND closed_at IS NULL LIMIT 1",
        (owner.owner_id, number, decision_ref),
    ).fetchone()
    if seen is None and _comment_evidence(tracker, owner, number, rendered, decision_ref):
        _record_open(store, owner, opener, number, decision_ref, untrusted, now)


def _record_reopen(
    store: GapStore, owner: GapOwnerId, opener: GapOpener, number: int,
    rendered: str, decision_ref: str, untrusted: bool, *,
    tracker: IssueTracker, now: datetime, labels: Iterable[str] = (),
) -> None:
    """Record a reopen, then post its evidence through the retrying path.

    A ``needs-decision`` label still on the issue (its removal at close failed,
    or a non-door close skipped it) is cleared first: a reopen is a fresh
    occurrence and must not stay parked behind an old ask's fence.

    The reopen row carries a ``reopen:``-prefixed ref, so the episode's own
    ``decision_ref`` is written only once its evidence comment posts; a failed
    comment is retried by :func:`_note_new_evidence` on the next reuse.
    """
    if NEEDS_DECISION_LABEL in labels:
        tracker.remove_label(number, NEEDS_DECISION_LABEL)
    reopen_ref = f"reopen:{decision_ref}"[:MAX_DECISION_REF_CHARS]
    _record_open(store, owner, opener, number, reopen_ref, untrusted, now)
    _note_new_evidence(store, owner, opener, number, rendered, decision_ref,
                       untrusted, tracker=tracker, now=now)


def open_gap_issue(
    owner_id: str,
    opener: GapOpener,
    evidence: str | UntrustedText,
    decision_ref: str,
    *,
    untrusted_input: bool,
    tracker: IssueTracker,
    store: GapStore,
    operator_logins: Iterable[str],
    now: datetime,
) -> GapOpenResult:
    """Open (or reuse, or reopen) the gap issue for *owner_id*.

    Refusals before any host call: ``owner-id-invalid``, ``opener-not-admitted``,
    ``decision-ref-invalid``, ``untrusted-evidence-unfenced``,
    ``evidence-too-long``. Only issues this door opened (a ``gap_opens`` row)
    are reused or reopened, each read by number (never a listing), and one
    whose gap label or title was edited away is ``gap-tampered``; a host-gone
    door issue (deleted) is skipped; a
    door-shaped twin without one is ignored, and a door issue anyone
    reopened is adopted with a fresh row (``reopened``). A
    door-opened issue an operator closed by hand is ``operator-abandoned``; an unreadable closure is
    ``closure-unknown`` (fails closed); a failed host read or write is
    ``host-failed``. ``opened`` / ``reopened`` write a ``gap_opens`` row; a
    ``reopened`` issue gets the current evidence as a comment, and a ``reused``
    one too (plus a row recording its ``decision_ref``) the first time a
    ``decision_ref`` new to that issue reaches it, so a later episode's
    evidence is never lost behind the first one's body.
    """
    owner = parse_owner_id(owner_id)
    if owner is None:
        return GapOpenResult("refused", reason="owner-id-invalid")
    if opener not in owner.openers:
        return GapOpenResult("refused", reason="opener-not-admitted")
    if not decision_ref.strip() or len(decision_ref) > MAX_DECISION_REF_CHARS:
        return GapOpenResult("refused", reason="decision-ref-invalid")
    fenced = isinstance(evidence, UntrustedText)
    if untrusted_input and not fenced:
        return GapOpenResult("refused", reason="untrusted-evidence-unfenced")
    untrusted = untrusted_input or fenced
    rendered = _render_evidence(evidence)
    if len(rendered) > MAX_EVIDENCE_CHARS:
        return GapOpenResult("refused", reason="evidence-too-long")

    rows = _door_rows(store, owner.owner_id)
    states = _door_states(tracker, rows)
    if states is None:
        return GapOpenResult("refused", reason="host-failed")
    open_door = [n for n, st in states.items() if st.state == "open"]
    operators = {login.casefold() for login in operator_logins}
    if open_door:
        live = min(open_door)
        if _tampered(owner, states[live]):
            # Its label or door-shaped title was edited away: reusing it would
            # silence the gap, so the door fails closed under its own reason.
            return GapOpenResult("refused", live, "gap-tampered")
        closure = tracker.closure(live)
        if closure is None:
            return GapOpenResult("refused", live, "closure-unknown")
        if _abandoned(closure, operators):
            # An operator abandoned it and a non-operator reopened it; only a
            # D8 login resumes an abandoned gap.
            return GapOpenResult("refused", live, "operator-abandoned")
        if rows[live]:
            _note_new_evidence(store, owner, opener, live, rendered, decision_ref,
                               untrusted, tracker=tracker, now=now)
            return GapOpenResult("reused", issue_number=live)
        # Anyone (operator or bot) reopened a door issue after the loop closed
        # all its rows; the door would reopen it itself, so adopt it.
        _record_reopen(store, owner, opener, live, rendered, decision_ref,
                       untrusted, tracker=tracker, now=now,
                       labels=states[live].labels)
        return GapOpenResult("reopened", issue_number=live)

    door_closed = sorted(n for n, st in states.items() if st.state == "closed")
    if door_closed:
        for candidate in door_closed:
            closure = tracker.closure(candidate)
            if closure is None or not closure.closed:
                return GapOpenResult("refused", candidate, "closure-unknown")
            if _operator_hand_close(closure, operators) or _abandoned(closure, operators):
                return GapOpenResult("refused", candidate, "operator-abandoned")
        newest = door_closed[-1]
        if _tampered(owner, states[newest]):
            return GapOpenResult("refused", newest, "gap-tampered")
        if not tracker.reopen(newest):
            return GapOpenResult("refused", newest, "host-failed")
        _record_reopen(store, owner, opener, newest, rendered, decision_ref,
                       untrusted, tracker=tracker, now=now,
                       labels=states[newest].labels)
        return GapOpenResult("reopened", issue_number=newest)

    number = tracker.create(
        gap_title(owner), _body(owner, rendered, decision_ref), labels=[owner.label],
    )
    if number is None:
        return GapOpenResult("refused", reason="host-failed")
    _record_open(store, owner, opener, number, decision_ref, untrusted, now)
    return GapOpenResult("opened", issue_number=number)


def close_gap_issue(
    owner_id: str,
    close_ref: str,
    *,
    tracker: IssueTracker,
    store: GapStore,
    now: datetime,
) -> int | None:
    """Close *owner_id*'s open gap issue and stamp its rows.

    Returns the closed issue number, ``None`` if none closed.
    """
    owner = parse_owner_id(owner_id)
    if owner is None:
        return None
    states = _door_states(tracker, _door_rows(store, owner.owner_id))
    open_door = [n for n, st in (states or {}).items() if st.state == "open"]
    if not open_door:
        return None
    number = min(open_door)
    if not tracker.close(number):
        return None
    if states is not None and NEEDS_DECISION_LABEL in states[number].labels:
        # A closed gap is resolved: a later reopen is a fresh occurrence, so it
        # must not stay parked behind the at-most-once fence of an old ask.
        # The ``unlabeled`` event this writes also restarts the attempt count.
        tracker.remove_label(number, NEEDS_DECISION_LABEL)
    with store.transaction() as conn:
        conn.execute(
            f"UPDATE {_GAP_OPENS} SET closed_at = ?, close_ref = ? "
            "WHERE owner_id = ? AND issue_number = ? AND closed_at IS NULL",
            (utc_stamp(now), close_ref, owner.owner_id, number),
        )
    return number


def classify_gap_trust(
    issue: Issue,
    *,
    tracker: IssueTracker,
    store: GapStore,
    operator_logins: Iterable[str],
) -> GapTrust:
    """Return how far a ``gap:*``-labelled *issue* can be trusted."""
    gap_labels = [lbl for lbl in issue.labels if is_gap_label(lbl)]
    if not gap_labels:
        return "not_gap"
    # The free local re-derivation runs first; a re-derived issue then costs
    # one host events read (its closure, for the abandonment check), and an
    # abandoned or unreadable one also reads each gap label's actor.
    operators = {login.casefold() for login in operator_logins}
    owner = parse_owner_id(owner_id_of_title(issue.title))
    if owner is not None and owner.label in issue.labels:
        openers = sorted(owner.openers)
        row = store.connection.execute(
            f"SELECT 1 FROM {_GAP_OPENS} WHERE owner_id = ? AND issue_number = ? "
            "AND closed_at IS NULL AND opener IN "
            f"({', '.join('?' for _ in openers)}) LIMIT 1",
            (owner.owner_id, issue.number, *openers),
        ).fetchone()
        if row is not None:
            # An operator's standing hand close (no later D8 reopen) means the
            # gap was abandoned; a bot reopen cannot revive the row's trust.
            closure = tracker.closure(issue.number)
            if closure is not None and not _abandoned(closure, operators):
                return "rederived"
    for label in gap_labels:
        actor = tracker.label_actor(issue.number, label)
        if actor and actor.casefold() in operators:
            return "operator"
    return "unverified"


def _admit_with_trust(
    issues: Iterable[Issue],
    *,
    tracker: IssueTracker,
    store: GapStore,
    operator_logins: Iterable[str],
    journal: Journal,
) -> list[tuple[Issue, GapTrust]]:
    """The trusted gap issues oldest first with their trust; record each unverified one once."""
    operators = tuple(operator_logins)
    # Read lazily: events.jsonl is never archived, so the full walk runs only
    # when an unverified issue actually needs the dedupe check.
    recorded: set[str] | None = None
    admitted: list[tuple[Issue, GapTrust]] = []
    for issue in issues:
        trust = classify_gap_trust(
            issue, tracker=tracker, store=store, operator_logins=operators,
        )
        if trust in ("operator", "rederived"):
            admitted.append((issue, trust))
        elif trust == "unverified":
            if recorded is None:
                recorded = {
                    entry.content.split(":", 1)[0]
                    for entry in journal.read_events_by_type("audit_observation")
                    if entry.content.startswith("gap-unverified #")
                }
            if f"gap-unverified #{issue.number}" in recorded:
                continue
            recorded.add(f"gap-unverified #{issue.number}")
            journal.record_event(
                f"gap-unverified #{issue.number}: no operator label and no "
                "gap_opens row re-derives it",
                entry_type="audit_observation",
                triggered_by="evolution_fuel",
            )
    admitted.sort(key=lambda pair: (pair[0].created_at or "9999", pair[0].number))
    return admitted


def admit_gap_issues(
    issues: Iterable[Issue],
    *,
    tracker: IssueTracker,
    store: GapStore,
    operator_logins: Iterable[str],
    journal: Journal,
) -> list[Issue]:
    """Return the trusted gap issues oldest first; record each unverified one once."""
    return [
        issue for issue, _ in _admit_with_trust(
            issues, tracker=tracker, store=store,
            operator_logins=operator_logins, journal=journal,
        )
    ]


def operator_login_refusal(logins: Iterable[str], own_login: str) -> str | None:
    """Return why a D8 list naming the loop's own login is refused, else ``None``."""
    if own_login and own_login.casefold() in {lg.casefold() for lg in logins}:
        return (
            f"[evolution_fuel] operator_github_logins names the loop's own login "
            f"{own_login!r}; an operator login must be a human's (D8)"
        )
    return None


def effective_operator_logins(config: Config, own_login: str) -> frozenset[str]:
    """D8 casefolded, with the loop's own login re-excluded at runtime."""
    own = own_login.casefold()
    return frozenset(
        lg.casefold() for lg in config.evolution_fuel_operator_github_logins
        if lg.casefold() != own or not own
    )


# ── Self-health detectors (proposal 155 step 2) ──────────────────────────────

_RED_MAIN = "red_main_watch"
#: The one ref the red-main watch tracks (its row's primary key).
RED_MAIN_REF = "origin/main"
GATE_OWNER_ID = "maestro:gate"
_SOURCE_BUDGET_HEAD = "maestro:source_budget:"
#: Worst-first candidates the source-budget detector tries per tick when the
#: door refuses one (an operator-abandoned file, say).
SOURCE_BUDGET_MAX_OPEN_ATTEMPTS = 3
#: Door refusals the source-budget detector steps past to the next candidate;
#: any other refusal (``host-failed``, ``closure-unknown``) stops the tick.
_SKIPPABLE_REFUSALS = frozenset({"operator-abandoned", "gap-tampered"})
_OVER_BUDGET_BANDS = frozenset({SourceBand.OVER_BUDGET, SourceBand.OVER_CEILING})
RedMainState = Literal["green", "red_pending", "red_due", "running", "unread"]
DetectorAction = Literal["opened", "reused", "reopened", "refused", "closed"]


@dataclass(frozen=True)
class RedMainReading:
    """One origin/main read folded against the ``red_main_watch`` row."""

    state: RedMainState
    sha: str = ""
    first_red_sha: str = ""
    first_red_at: str = ""
    failing_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class DetectorOutcome:
    """What one detector did to one owner id's gap issue."""

    owner_id: str
    action: DetectorAction
    issue_number: int | None = None
    reason: str = ""


@dataclass(frozen=True)
class SelfHealthReport:
    red_main: RedMainReading
    outcomes: tuple[DetectorOutcome, ...]


def _red_reading(
    state: RedMainState, sha: str, first: tuple[str, str, str],
) -> RedMainReading:
    first_sha, first_at, checks = first
    return RedMainReading(state, sha, first_sha, first_at, tuple(json.loads(checks)))


def observe_red_main(
    store: GapStore,
    *,
    sha: str,
    runs: Sequence[CheckRun] | None,
    now: datetime,
    grace_hours: float,
) -> RedMainReading:
    """Fold one origin/main check-run read into the ``red_main_watch`` row.

    ``runs`` ``None`` (no read) is ``unread`` and writes nothing. A FAILING read
    inserts the row, restarts its clock when the sorted set of failing check
    names changed (a read with checks still unconcluded whose failures are a
    subset of the row's keeps the clock), else stamps the read; it is ``red_due`` once the first red is
    *grace_hours* old. GREEN deletes the row; RUNNING keeps it and stays
    ``red_due`` while a past-grace row stands.
    """
    if runs is None or not sha:
        return RedMainReading("unread", sha)
    status = classify_ci_conclusions(runs)
    row = store.connection.execute(
        f"SELECT first_red_sha, first_red_at, failing_checks FROM {_RED_MAIN} "
        "WHERE ref = ?",
        (RED_MAIN_REF,),
    ).fetchone()
    stamp = utc_stamp(now)
    due_before = now - timedelta(hours=grace_hours)
    if status == "GREEN":
        if row is not None:
            with store.transaction() as conn:
                conn.execute(f"DELETE FROM {_RED_MAIN} WHERE ref = ?", (RED_MAIN_REF,))
        return RedMainReading("green", sha)
    if status == "RUNNING":
        if row is not None and parse_utc_stamp(str(row[1])) <= due_before:
            return _red_reading("red_due", sha, (str(row[0]), str(row[1]), str(row[2])))
        return RedMainReading("running", sha)
    failing = set(failing_check_names(runs))
    checks = json.dumps(sorted(failing))
    partial = any(
        not (is_failing_conclusion(r.conclusion) or is_success_conclusion(r.conclusion))
        for r in runs
    )
    if row is not None and partial and failing <= set(json.loads(str(row[2]))):
        # A mid-run read whose failures are all already on the row cannot tell
        # whether the set changed: stamp the read, keep the clock.
        checks = str(row[2])
    with store.transaction() as conn:
        if row is None or str(row[2]) != checks:
            conn.execute(
                f"INSERT OR REPLACE INTO {_RED_MAIN} (ref, first_red_sha, "
                "first_red_at, failing_checks, last_read_sha, last_read_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (RED_MAIN_REF, sha, stamp, checks, sha, stamp),
            )
            first = (sha, stamp, checks)
        else:
            conn.execute(
                f"UPDATE {_RED_MAIN} SET last_read_sha = ?, last_read_at = ? "
                "WHERE ref = ?",
                (sha, stamp, RED_MAIN_REF),
            )
            first = (str(row[0]), str(row[1]), checks)
    state: RedMainState = (
        "red_due" if parse_utc_stamp(first[1]) <= due_before else "red_pending"
    )
    return _red_reading(state, sha, first)


def _from_open(owner_id: str, result: GapOpenResult) -> DetectorOutcome:
    return DetectorOutcome(owner_id, result.outcome, result.issue_number, result.reason)


def detect_red_main(
    reading: RedMainReading,
    *,
    inherited_hold_live: bool,
    tracker: IssueTracker,
    store: GapStore,
    operator_logins: Iterable[str],
    now: datetime,
) -> DetectorOutcome | None:
    """Open the gate issue on ``red_due``; close it only on a ``green`` read.

    The close needs origin/main read green and no live plan on an inherited
    hold against the head just read, so a hold-opened issue stays open until
    the base moves. ``red_pending`` (a changed failing set restarted the clock)
    and ``running`` never close: origin/main may still be red, and the close
    reference names a green head.
    """
    if reading.state == "red_due":
        evidence = (
            f"origin/main has read red on the same checks since "
            f"{reading.first_red_at} (first red at {reading.first_red_sha}, last "
            f"read at {reading.sha}). Failing checks: "
            f"{', '.join(reading.failing_checks) or '(unnamed)'}."
        )[:MAX_EVIDENCE_CHARS]
        ref = (
            f"red-main:{reading.first_red_sha[:12]}:"
            f"{len(reading.failing_checks)}-checks"
        )
        result = open_gap_issue(
            GATE_OWNER_ID, "detector", evidence, ref, untrusted_input=False,
            tracker=tracker, store=store, operator_logins=operator_logins, now=now,
        )
        return _from_open(GATE_OWNER_ID, result)
    if reading.state != "green" or inherited_hold_live:
        return None
    closed = close_gap_issue(
        GATE_OWNER_ID, f"main-green:{reading.sha[:12]}",
        tracker=tracker, store=store, now=now,
    )
    if closed:
        return DetectorOutcome(GATE_OWNER_ID, "closed", closed, reading.state)
    return None


def open_gate_for_inherited_hold(
    plan_id: str,
    held_sha: str | None,
    identities: str,
    *,
    tracker: IssueTracker,
    store: GapStore,
    operator_logins: Iterable[str],
    now: datetime,
) -> DetectorOutcome:
    """Open or reuse the ``maestro:gate`` issue for a proposal-124 inherited hold."""
    head = held_sha or "unknown"
    evidence = (
        f"Plan {plan_id} is held: its quality-gate red is inherited from "
        f"origin/main at {head}, not its diff. Failing: {identities}"
    )[:MAX_EVIDENCE_CHARS]
    ref = f"inherited-hold:{plan_id}@{head[:12]}"[:MAX_DECISION_REF_CHARS]
    result = open_gap_issue(
        GATE_OWNER_ID, "detector", evidence, ref, untrusted_input=False,
        tracker=tracker, store=store, operator_logins=operator_logins, now=now,
    )
    return _from_open(GATE_OWNER_ID, result)


def _live_source_budget_owners(
    tracker: IssueTracker, store: GapStore,
) -> set[str] | None:
    """Source-budget owner ids with an unclosed row whose door issue is open.

    ``None`` when any host read fails, so the caller opens nothing that could
    become a second open issue.
    """
    owners = [
        str(r[0]) for r in store.connection.execute(
            f"SELECT DISTINCT owner_id FROM {_GAP_OPENS} WHERE closed_at IS NULL "
            "AND substr(owner_id, 1, ?) = ?",
            (len(_SOURCE_BUDGET_HEAD), _SOURCE_BUDGET_HEAD),
        ).fetchall()
    ]
    live: set[str] = set()
    for owner_id in owners:
        states = _door_states(tracker, _door_rows(store, owner_id))
        if states is None:
            return None
        if any(meta.state == "open" for meta in states.values()):
            live.add(owner_id)
    return live


def detect_source_budget(
    readings: Sequence[SourceBudgetReading],
    *,
    tracker: IssueTracker,
    store: GapStore,
    operator_logins: Iterable[str],
    now: datetime,
) -> list[DetectorOutcome]:
    """Keep at most one ``maestro:source_budget`` issue open, for the worst file.

    Candidates are files at or over the split budget, worst first. A live issue
    for a file no longer a candidate is closed (``under-budget``); a live
    candidate issue is reused and nothing else opens; otherwise up to
    :data:`SOURCE_BUDGET_MAX_OPEN_ATTEMPTS` candidates are tried worst first,
    stepping past an ``operator-abandoned`` or ``gap-tampered`` refusal and
    stopping on any other.
    """
    candidates: dict[str, SourceBudgetReading] = {}
    for reading in split_due_readings(list(readings)):
        owner_id = f"{_SOURCE_BUDGET_HEAD}{reading.relative}"
        if reading.band in _OVER_BUDGET_BANDS and parse_owner_id(owner_id):
            candidates.setdefault(owner_id, reading)
    if not readings:
        # An empty scan (missing root or path atoms) is unread, not under-budget.
        return []
    live = _live_source_budget_owners(tracker, store)
    if live is None:
        return [DetectorOutcome(_SOURCE_BUDGET_HEAD, "refused", reason="host-failed")]
    outcomes: list[DetectorOutcome] = []
    for owner_id in sorted(live - candidates.keys()):
        closed = close_gap_issue(
            owner_id, "under-budget", tracker=tracker, store=store, now=now,
        )
        if not closed:
            outcomes.append(DetectorOutcome(owner_id, "refused", reason="host-failed"))
            return outcomes
        outcomes.append(DetectorOutcome(owner_id, "closed", closed, "under-budget"))
    ordered = list(candidates)
    standing = [o for o in ordered if o in live]
    tries = standing[:1] or ordered[:SOURCE_BUDGET_MAX_OPEN_ATTEMPTS]
    for owner_id in tries:
        reading = candidates[owner_id]
        evidence = (
            f"{reading.relative} is {reading.size} bytes ({reading.band.value}), "
            f"{reading.size - SPLIT_BUDGET} bytes over the split budget of "
            f"{SPLIT_BUDGET}; the cure is a concern split, never a trim."
        )
        result = open_gap_issue(
            owner_id, "detector", evidence,
            f"source-budget:{reading.band.value}",
            untrusted_input=False, tracker=tracker, store=store,
            operator_logins=operator_logins, now=now,
        )
        outcomes.append(_from_open(owner_id, result))
        if result.outcome != "refused" or result.reason not in _SKIPPABLE_REFUSALS:
            # A host failure or unknown closure may hide a live issue for this
            # file; trying the next file could open a second one.
            break
    return outcomes


#: Gate-abandon observations already surfaced (or found journalled) by this
#: process, so the full ``events.jsonl`` walk runs at most once per issue.
_SURFACED_GATE_ABANDONS: set[str] = set()


def _record_gate_abandoned_once(journal: Journal, outcome: DetectorOutcome) -> None:
    """Surface, once per issue, that a hand close silenced the gate detectors.

    An operator hand close of the ``maestro:gate`` issue stands until an
    operator reopens it, so every red-main and inherited-hold open refuses.
    """
    number = f" #{outcome.issue_number}" if outcome.issue_number else ""
    content = (
        f"gap-abandoned {GATE_OWNER_ID}{number}: an operator closed it by hand; "
        "the red-main and inherited-hold detectors stay silent until an "
        "operator reopens it"
    )
    if content in _SURFACED_GATE_ABANDONS:
        return
    _SURFACED_GATE_ABANDONS.add(content)
    if any(
        entry.content == content
        for entry in journal.read_events_by_type("audit_observation")
    ):
        return
    logger.warning("Self-health: %s", content)
    journal.record_event(
        content, entry_type="audit_observation", triggered_by="evolution_fuel",
    )


def record_detector_outcome(journal: Journal, outcome: DetectorOutcome) -> None:
    """Journal an opened/reopened/closed outcome; log a refusal; skip a reuse."""
    if outcome.action == "refused":
        if outcome.reason == "operator-abandoned" and outcome.owner_id == GATE_OWNER_ID:
            _record_gate_abandoned_once(journal, outcome)
        level = logging.WARNING if outcome.reason == "host-failed" else logging.INFO
        logger.log(
            level, "Self-health detector refused %s: %s",
            outcome.owner_id, outcome.reason,
        )
        return
    if outcome.action == "reused":
        return
    number = f" #{outcome.issue_number}" if outcome.issue_number else ""
    journal.record_event(
        f"gap-{outcome.action} {outcome.owner_id}{number}"
        + (f" ({outcome.reason})" if outcome.reason else ""),
        entry_type="audit_observation",
        triggered_by="evolution_fuel",
    )


def run_self_health_detectors(
    *,
    sha: str,
    check_runs: Sequence[CheckRun] | None,
    inherited_hold_live: bool,
    readings: Sequence[SourceBudgetReading],
    grace_hours: float,
    tracker: IssueTracker,
    store: GapStore,
    operator_logins: Iterable[str],
    journal: Journal,
    now: datetime,
) -> SelfHealthReport:
    """Run the red-main and source-budget detectors; journal each state change."""
    logins = tuple(operator_logins)
    reading = observe_red_main(
        store, sha=sha, runs=check_runs, now=now, grace_hours=grace_hours,
    )
    outcomes: list[DetectorOutcome] = []
    gate = detect_red_main(
        reading, inherited_hold_live=inherited_hold_live, tracker=tracker,
        store=store, operator_logins=logins, now=now,
    )
    if gate is not None:
        outcomes.append(gate)
    outcomes.extend(detect_source_budget(
        readings, tracker=tracker, store=store, operator_logins=logins, now=now,
    ))
    for outcome in outcomes:
        record_detector_outcome(journal, outcome)
    return SelfHealthReport(reading, tuple(outcomes))


# ── Fuel selection (proposal 155 step 4) ─────────────────────────────────────

#: The label the stuck-gap tripwire (step 5) parks a capped gap under; an issue
#: carrying it is never fuel until Thomas clears it.
NEEDS_DECISION_LABEL = "needs-decision"

#: How many open issues each per-label fuel read lists. ``gh`` ANDs repeated
#: ``--label`` flags, so the fuel read makes one labelled call per gap label;
#: an unfiltered page would cut off the oldest gaps behind newer non-gap issues.
FUEL_PAGE_SIZE = 200
#: Fuel classes, lowest first: a red gate, then a capability or toolchain gap,
#: then any other Maestro-health gap.
FuelClass = Literal[1, 2, 3]


@dataclass(frozen=True)
class GapCap:
    """A prompt-input cap: at most *items* entries of at most *chars* each."""

    items: int
    chars: int


#: The load-bearing cap of every input :func:`render_gap_block` interpolates.
GAP_CONTEXT_CAPS: Mapping[str, GapCap] = MappingProxyType({
    "issue_title": GapCap(1, 120),
    "evidence": GapCap(1, 2000),
    "issue_body": GapCap(1, 4000),
    "failed_excerpt": GapCap(1, 3000),
    "citing_reasons": GapCap(5, 300),
    "recent_rejections": GapCap(5, 300),
    "source_task_candidates": GapCap(5, 120),
    "version_tail": GapCap(5, 120),
    "live_targets": GapCap(20, 40),
})


@dataclass(frozen=True)
class EvolutionFuel:
    """One admitted gap issue selected to fuel this evolve tick."""

    issue: Issue
    owner: GapOwnerId | None
    label: GapLabel
    fuel_class: FuelClass
    trust: GapTrust
    failed_excerpt: str = ""

    @property
    def bypasses_gates(self) -> bool:
        """A red gate is planned past WIP primacy and the pre-mint gate."""
        return self.owner is not None and self.owner.owner_id == GATE_OWNER_ID


@dataclass(frozen=True)
class TrippedGap:
    """A gap the stuck-gap tripwire labelled ``needs-decision`` this read."""

    issue_number: int
    attempts: int


@dataclass(frozen=True)
class FuelSelection:
    """The fuel read: the pick (or ``None``), each skip, each trip."""

    selected: EvolutionFuel | None = None
    skipped: tuple[tuple[int, str], ...] = ()
    tripped: tuple[TrippedGap, ...] = ()


@dataclass(frozen=True)
class GapContextInputs:
    """The loop-state reads :func:`render_gap_block` renders, each newest last."""

    citing_reasons: Sequence[str] = ()
    recent_rejections: Sequence[str] = ()
    source_task_candidates: Sequence[str] = ()
    version_tail: Sequence[str] = ()
    live_targets: Sequence[str] = ()


def _gap_label_of(issue: Issue, owner: GapOwnerId | None) -> GapLabel:
    if owner is not None and owner.label in issue.labels:
        return owner.label
    for label in GAP_LABELS:
        if label in issue.labels:
            return label
    return "gap:maestro"


def fuel_class_of(owner: GapOwnerId | None, label: GapLabel) -> FuelClass:
    """Class 1 a red gate; 2 a capability or toolchain gap; 3 any other gap."""
    if owner is not None and owner.owner_id == GATE_OWNER_ID:
        return 1
    if label == "gap:capability":
        return 2
    if owner is not None and owner.prefix == "toolchain":
        return 2
    return 3


def _episode_start(store: GapStore, number: int) -> datetime | None:
    """When this door last reopened *number* (``None``: a first episode)."""
    row = store.connection.execute(
        f"SELECT MAX(opened_at) FROM {_GAP_OPENS} WHERE issue_number = ? "
        "AND decision_ref LIKE 'reopen:%'",
        (number,),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return parse_utc_stamp(str(row[0]))


def _current_episode_plans(
    store: GapStore, number: int, plans: Sequence[EvolutionPlan],
) -> list[EvolutionPlan]:
    """The plans citing *number* minted since this door's newest reopen of it.

    ``open_gap_issue`` reuses an owner's issue number across red episodes, so
    attempts spent before the latest ``reopen:`` row are a past episode's and
    must not exhaust the current one. No reopen row (a first episode, or an
    operator-filed gap) keeps every plan. An unparseable ``created_at`` stays
    counted (fail toward exhaustion, not toward a re-mint loop).
    """
    cited = [p for p in plans if p.issue_number == number]
    since = _episode_start(store, number)
    if since is None:
        return cited
    kept: list[EvolutionPlan] = []
    for p in cited:
        try:
            created = datetime.fromisoformat(p.created_at.replace("Z", "+00:00"))
        except ValueError:
            kept.append(p)
            continue
        if created.tzinfo is None or created >= since:
            kept.append(p)
    return kept


def _record_exhausted_once(
    journal: Journal, number: int, attempts: int, recorded: set[str] | None,
) -> set[str]:
    """Record gap *number* exhausted unless already recorded; return the heads.

    *recorded* is the per-selection cache of recorded heads (``None`` until the
    first exhausted gap reads the events ledger), so one selection reads
    ``events.jsonl`` at most once however many gaps are exhausted.
    """
    if recorded is None:
        recorded = {
            entry.content.split(":", 1)[0]
            for entry in journal.read_events_by_type("audit_observation")
        }
    head = f"gap-exhausted #{number}"
    if head in recorded:
        return recorded
    recorded.add(head)
    journal.record_event(
        f"{head}: {attempts} plan attempt(s) spent; no longer evolution fuel",
        entry_type="audit_observation",
        triggered_by="evolution_fuel",
    )
    return recorded


#: How far before a host ``unlabeled`` stamp Maestro's own record of the act
#: may sit and still corroborate it: the record is written just after the host
#: write, so only clock skew between the host and this machine separates them.
_CORROBORATION_SKEW = timedelta(minutes=10)


def _parse_stamp(text: str) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def _own_removal_corroborated(
    number: int, removed: datetime, *, journal: Journal, store: GapStore,
) -> bool:
    """Whether Maestro's own record names the act behind an own-login removal.

    Maestro's login writes ``needs-decision`` removals only for a 'build'
    reply (:func:`apply_gap_decision`, which journals ``gap-decision #N:
    build``) or a gap close (:func:`close_gap_issue`, which stamps
    ``closed_at``). Any other own-login removal — a task session running
    ``gh issue edit --remove-label`` under the bot token — has no such record.
    """
    floor = removed - _CORROBORATION_SKEW
    for entry in journal.read_events_by_type("audit_observation"):
        if not is_build_removal_row(entry.content, number):
            continue
        at = _parse_stamp(entry.timestamp)
        if at is not None and at >= floor:
            return True
    with store.transaction() as conn:
        rows = conn.execute(
            f"SELECT closed_at FROM {_GAP_OPENS} "
            "WHERE issue_number = ? AND closed_at IS NOT NULL",
            (number,),
        ).fetchall()
    return any(parse_utc_stamp(row[0]) >= floor for row in rows)


def _reset_point(
    tracker: IssueTracker, number: int, *, operators: frozenset[str],
    own_login: str, journal: Journal, store: GapStore,
) -> datetime | Literal["unread"] | None:
    """When Thomas last cleared *number*'s ``needs-decision`` (build differently).

    A removal by a D8 operator resets the count. One by Maestro's own login
    resets it only when Maestro's own record corroborates it (a 'build' reply
    applied, or the gap closed): the bot token is shared with task sessions,
    whose ``gh issue edit`` is not action-gated. Any other removal buys no
    attempts and is logged. ``"unread"`` when the label events could not be
    read: the removal is unknown, so the caller must not re-label on it.
    """
    removal = tracker.label_removed_at(number, NEEDS_DECISION_LABEL)
    if removal is None:
        return None
    if not removal.readable:
        return "unread"
    stamp = _parse_stamp(removal.at)
    if stamp is None:
        return None
    actor = removal.actor.casefold()
    if actor in operators:
        return stamp
    if own_login and actor == own_login and _own_removal_corroborated(
        number, stamp, journal=journal, store=store,
    ):
        return stamp
    logger.info(
        "Gap #%d %s removal by %r is not a recorded decision; attempt count not reset",
        number, NEEDS_DECISION_LABEL, removal.actor,
    )
    return None


_TRIP_COMMENT = (
    "Maestro has spent {attempts} plan attempt(s) on this gap without closing "
    "it, so it is labelled `needs-decision` and is no longer evolution fuel. "
    "{operator}: this is your decision, asked separately — build differently "
    "(reply 'build', or remove the label yourself), buy (reply 'buy'; the gap "
    "stays parked), or drop (close this issue yourself). A label removal by "
    "anyone else is not a decision and re-trips the gap."
)


def _already_tripped(journal: Journal, number: int, *, store: GapStore) -> bool:
    """Whether *number* was tripped (and so asked about) in its current episode.

    A trip row older than the latest reopen belongs to a past episode: a
    reopened gap is fresh fuel and earns a fresh ask once it is spent again.
    An unparseable stamp counts as current (fail toward no duplicate ask).
    """
    head = f"gap-tripwire #{number}:"
    start = _episode_start(store, number)
    for e in journal.read_events_by_type("audit_observation"):
        if not e.content.startswith(head):
            continue
        at = _parse_stamp(e.timestamp)
        if start is None or at is None or at >= start:
            return True
    return False


def _trip_or_reset(
    issue_number: int,
    attempts: int,
    plans: Sequence[EvolutionPlan],
    *,
    retry_budget: int,
    max_attempts: int,
    tracker: IssueTracker,
    journal: Journal,
    store: GapStore,
    operators: frozenset[str],
    own_login: str,
    operator: str,
) -> tuple[int, bool | Literal["relabelled", "unread"] | None]:
    """Recount a capped gap from its reset, else trip it (proposal 155 step 5).

    Returns ``(attempts, tripped)``: ``tripped`` is ``None`` when the recount
    from Thomas's last ``needs-decision`` removal leaves it under the cap
    (ordinary fuel), ``True`` once the label is written (the at-most-once fence
    for the ask), ``False`` when the label write failed (retried next tick),
    ``"relabelled"`` when an unrecognised removal of an already-asked gap's
    label is undone without a fresh comment or ask, ``"unread"`` when the
    label events could not be read (no label write; retried next tick, since
    re-labelling blind could silently undo Thomas's removal).
    """
    since = _reset_point(
        tracker, issue_number, operators=operators, own_login=own_login,
        journal=journal, store=store,
    )
    if since == "unread":
        return attempts, "unread"
    if since is not None:
        attempts = count_gap_attempts(plans, issue_number, retry_budget, since=since)
        if attempts < max_attempts:
            return attempts, None
    if not tracker.add_label(issue_number, NEEDS_DECISION_LABEL):
        return attempts, False
    if since is None and _already_tripped(journal, issue_number, store=store):
        # An unrecognised removal re-parks the gap; Thomas was already asked
        # about it, so neither another comment nor another ask is sent.
        return attempts, "relabelled"
    if not tracker.comment(
        issue_number, _TRIP_COMMENT.format(attempts=attempts, operator=operator),
    ):
        logger.warning("Gap #%d tripwire comment not posted", issue_number)
        journal.record_event(
            f"gap-tripwire-comment-failed #{issue_number}: the issue carries no "
            "decision comment; the ask is the only channel",
            entry_type="audit_observation",
            triggered_by="evolution_fuel",
        )
    journal.record_event(
        f"gap-tripwire #{issue_number}: {attempts} plan attempt(s) spent; "
        f"labelled {NEEDS_DECISION_LABEL} for Thomas's build/buy/drop decision",
        entry_type="audit_observation",
        triggered_by="evolution_fuel",
    )
    return attempts, True


def _list_open_gap_issues(tracker: IssueTracker) -> list[Issue] | None:
    """One labelled open-issue read per gap label, merged by number.

    Filtering on the server keeps an old gap reachable however many non-gap
    issues are open. Any unreadable label makes the whole read unreadable
    (fail closed), and a full page is logged so truncation is never silent.
    """
    merged: dict[int, Issue] = {}
    for label in GAP_LABELS:
        page = tracker.list_open_or_none(labels=[label], page_size=FUEL_PAGE_SIZE)
        if page is None:
            return None
        if len(page) >= FUEL_PAGE_SIZE:
            logger.warning(
                "Fuel read for %s returned a full page (%d); older gaps may be cut off",
                label, FUEL_PAGE_SIZE,
            )
        for issue in page:
            merged.setdefault(issue.number, issue)
    return list(merged.values())


def select_evolution_fuel(
    now: datetime,
    *,
    tracker: IssueTracker,
    store: GapStore,
    operator_logins: Iterable[str],
    journal: Journal,
    plans: Sequence[EvolutionPlan],
    max_attempts: int,
    failed_excerpt: str = "",
    retry_budget: int = 0,
    own_login: str = "",
) -> FuelSelection:
    """Pick this tick's evolution fuel at zero SDK cost.

    One open-issue read, trust admission, then: skip a gap labelled
    ``needs-decision``, one with a live plan, one that has spent
    *max_attempts* since its last reset (tripped: labelled ``needs-decision``
    and returned in ``tripped``; recorded once as exhausted while the label
    write fails), or one still in a
    rejection cooldown; while a gap-citing plan is ``pending_review`` only a
    class-1 gap is fuel. The lowest class wins, oldest first. *failed_excerpt*
    is stamped on a ``maestro:gate`` pick. *retry_budget* is
    ``max_implementation_attempts``: a ``failed`` plan under it is still live.
    *own_login* is Maestro's login: besides an operator's, only its removal of
    ``needs-decision`` restarts the attempt count, and only when Maestro's own
    record of a 'build' reply or a close corroborates it.
    """
    operator_logins = tuple(operator_logins)
    operators = frozenset(login.casefold() for login in operator_logins if login)
    # Deterministic and never a guessed handle: with no D8 login configured
    # the comment mentions nobody rather than an unrelated GitHub user.
    named = sorted(login for login in operator_logins if login)
    operator = f"@{named[0]}" if named else "The operator"
    listed = _list_open_gap_issues(tracker)
    if listed is None:
        return FuelSelection()
    candidates = [i for i in listed if any(is_gap_label(lbl) for lbl in i.labels)]
    admitted = _admit_with_trust(
        candidates, tracker=tracker, store=store,
        operator_logins=operator_logins, journal=journal,
    )
    open_numbers = frozenset(issue.number for issue, _ in admitted)
    pending_gap_plan = any(
        p.status == "pending_review" and p.issue_number in open_numbers for p in plans
    )
    skipped: list[tuple[int, str]] = []
    eligible: list[EvolutionFuel] = []
    tripped: list[TrippedGap] = []
    exhausted_heads: set[str] | None = None
    for issue, trust in admitted:
        if NEEDS_DECISION_LABEL in issue.labels:
            skipped.append((issue.number, "needs-decision"))
            continue
        owner = parse_owner_id(owner_id_of_title(issue.title))
        label = _gap_label_of(issue, owner)
        fuel_class = fuel_class_of(owner, label)
        if gap_has_live_plan(plans, issue.number, retry_budget):
            skipped.append((issue.number, "live-plan"))
            continue
        attempts = count_gap_attempts(
            _current_episode_plans(store, issue.number, plans),
            issue.number, retry_budget,
        )
        if attempts >= max_attempts and not own_login:
            # Without Maestro's login its own corroborated 'build' removal
            # cannot be told apart from a drive-by, so re-labelling now could
            # silently undo Thomas's decision; retry once the login resolves.
            logger.info("Gap #%d capped but own login unresolved; tripwire deferred", issue.number)
            skipped.append((issue.number, "owner-unresolved"))
            continue
        if attempts >= max_attempts:
            attempts, trip = _trip_or_reset(
                issue.number, attempts,
                _current_episode_plans(store, issue.number, plans),
                retry_budget=retry_budget,
                max_attempts=max_attempts, tracker=tracker, journal=journal,
                store=store, operators=operators,
                own_login=own_login.casefold(), operator=operator,
            )
            if trip == "relabelled":
                skipped.append((issue.number, "needs-decision"))
                continue
            if trip == "unread":
                logger.info("Gap #%d capped but label events unreadable; tripwire deferred", issue.number)
                skipped.append((issue.number, "reset-unread"))
                continue
            if trip is True:
                tripped.append(TrippedGap(issue.number, attempts))
                skipped.append((issue.number, "tripped"))
                continue
            if trip is False:
                exhausted_heads = _record_exhausted_once(
                    journal, issue.number, attempts, exhausted_heads,
                )
                skipped.append((issue.number, "exhausted"))
                continue
        until = gap_cooldown_until(plans, issue.number)
        if until is not None and now < until:
            skipped.append((issue.number, "cooldown"))
            continue
        if pending_gap_plan and fuel_class != 1:
            skipped.append((issue.number, "pending-review"))
            continue
        eligible.append(EvolutionFuel(
            issue=issue, owner=owner, label=label, fuel_class=fuel_class,
            trust=trust, failed_excerpt=failed_excerpt if fuel_class == 1 else "",
        ))
    # ``admitted`` is oldest first, so a stable sort keeps age within a class.
    eligible.sort(key=lambda fuel: fuel.fuel_class)
    return FuelSelection(
        selected=eligible[0] if eligible else None,
        skipped=tuple(skipped),
        tripped=tuple(tripped),
    )


def _cap_text(text: str, chars: int) -> str:
    """Keep the head of *text*, marking how many chars were dropped."""
    if len(text) <= chars:
        return text
    return f"{text[:chars]}[truncated: {len(text) - chars} chars]"


def _cap_list(items: Sequence[str], cap: GapCap) -> list[str]:
    """Keep the newest *cap.items* entries (each char-capped), marking the rest."""
    kept = [_cap_text(item, cap.chars) for item in items[-cap.items:]] if cap.items else []
    dropped = len(items) - len(kept)
    if dropped > 0:
        kept.append(f"[+{dropped} more]")
    return kept


def _section(title: str, lines: Sequence[str]) -> str:
    body = "\n".join(f"- {line}" for line in lines) if lines else "(none)"
    return f"{title}:\n{body}"


def render_gap_block(fuel: EvolutionFuel, inputs: GapContextInputs) -> str:
    """The deterministic, capped gap block ``propose_plan`` plans from. Pure.

    Trust only admits the issue as fuel; its live title and body stay
    host-editable text (an operator-filed title is the filer's words, and a
    door-opened body can be edited after the door wrote it), so both are
    fenced as untrusted data. A re-derived title is the door's own owner id.
    """
    caps = GAP_CONTEXT_CAPS
    owner = fuel.owner.owner_id if fuel.owner is not None else "(operator-filed)"
    ref = f"issue:{fuel.issue.number}"
    title = _cap_text(fuel.issue.title, caps["issue_title"].chars)
    if fuel.trust != "rederived":
        title = render_untrusted(mark_untrusted("gap_issue", ref, title))
    parts = [f"Gap issue #{fuel.issue.number} [{fuel.label}] owner {owner}: {title}"]
    if fuel.trust == "rederived":
        heading, body_cap = "Evidence", caps["evidence"].chars
    else:
        heading, body_cap = "Issue body", caps["issue_body"].chars
    fenced = render_untrusted(mark_untrusted(
        "gap_issue", ref, _cap_text(fuel.issue.body, body_cap),
    ))
    parts.append(f"{heading}:\n{fenced}\n{UNTRUSTED_DATA_RULE}")
    if fuel.failed_excerpt:
        parts.append(
            "Failing gate:\n" + _cap_text(fuel.failed_excerpt, caps["failed_excerpt"].chars),
        )
    for key, title in (
        ("citing_reasons", "Earlier plans on this gap ended because"),
        ("recent_rejections", "Recent human rejections"),
        ("source_task_candidates", "Kept task branches no plan cites (source_task_id)"),
        ("version_tail", "Recently merged"),
        ("live_targets", "Live targets"),
    ):
        parts.append(_section(title, _cap_list(getattr(inputs, key), caps[key])))
    return "\n\n".join(parts)


# ── The stuck-gap tripwire's decision (proposal 155 step 5) ──────────────────

GapDecision = Literal["build", "buy", "drop"]
GAP_DECISIONS: tuple[GapDecision, ...] = get_args(GapDecision)
#: The decisions a reply word applies; drop is never a reply word: it takes
#: effect only when an operator login closes the issue.
_REPLY_DECISIONS: tuple[GapDecision, ...] = ("build", "buy")
_GAP_DECISION_MARKER = "[gap-decision #{n}]"
_GAP_DECISION_MARKER_RE = re.compile(r"^\[gap-decision #([1-9]\d*)\]")
_DECISION_WORD_RE = re.compile(r"[a-z]+")
_DECISION_GLOSS: Mapping[GapDecision, str] = MappingProxyType({
    "build": "clear needs-decision and plan this gap differently",
    "buy": "keep the gap parked while it is bought",
    "drop": "close the issue yourself",
})


def gap_issue_url(repo: str, issue_number: int) -> str:
    """The github.com URL of gap issue *issue_number* in *repo*."""
    return f"https://github.com/{repo}/issues/{issue_number}"


def format_gap_decision_ask(issue_number: int, issue_url: str) -> str:
    """The question text that asks Thomas to decide a tripped gap. Pure.

    Carries no issue free text (the title and body are host-editable). Build
    and buy render as reply words; drop renders only as a link to the issue,
    where closing it is the drop act.
    """
    lines = [
        f"{_GAP_DECISION_MARKER.format(n=issue_number)} Decide gap issue {issue_number}",
        (
            f"Gap issue {issue_number} spent its plan attempts without closing; "
            "evolution no longer spends on it until you decide."
        ),
        "Reply to this message with one word:",
    ]
    lines.extend(
        f"  {decision} — {_DECISION_GLOSS[decision]}" for decision in _REPLY_DECISIONS
    )
    lines.append(f"To drop it, {_DECISION_GLOSS['drop']}: {issue_url}")
    return "\n".join(lines)


def format_gap_decision_followup(issue_number: int, text: str) -> str:
    """A follow-up question on gap *issue_number*, carrying its marker. Pure.

    The marker routes a reply to the follow-up back through
    :func:`parse_gap_decision_question`, so answering it applies.
    """
    return f"{_GAP_DECISION_MARKER.format(n=issue_number)} {text}"


def parse_gap_decision_question(text: str) -> int | None:
    """The gap issue number a gap-decision question names, else ``None``."""
    match = _GAP_DECISION_MARKER_RE.match(text.strip())
    return int(match.group(1)) if match else None


def parse_gap_decision_reply(text: str) -> GapDecision | None:
    """The decision a reply's first word names, else ``None``."""
    match = _DECISION_WORD_RE.match(text.strip().casefold())
    if match is None:
        return None
    for decision in GAP_DECISIONS:
        if match.group(0) == decision:
            return decision
    return None


#: The cap on the guidance text kept after a reply's decision word.
MAX_DECISION_NOTE_CHARS = 300


def gap_decision_note(text: str) -> str:
    """The guidance after a reply's first word, char-capped (``''`` when none)."""
    parts = text.strip().split(maxsplit=1)
    note = parts[1].strip(" \t-—:,.") if len(parts) > 1 else ""
    return _cap_text(note, MAX_DECISION_NOTE_CHARS)


def gap_decision_reply_stamp(reply_id: str) -> str:
    """The token a ``gap-decision`` journal row carries for its reply. Pure."""
    return f"(reply {reply_id})"


#: The outcome a 'build' row journals when its label removal landed.
_BUILD_REMOVED_OUTCOME = f"{NEEDS_DECISION_LABEL} removed; the attempt count restarts"


def is_build_removal_row(content: str, issue_number: int) -> bool:
    """Whether *content* is the row :func:`apply_gap_decision` journals for a landed build.

    The one reader of the writer's format: the row may carry a
    :func:`gap_decision_reply_stamp` between the decision and its outcome.
    """
    head = f"gap-decision #{issue_number}: build"
    if not content.startswith(head):
        return False
    rest = content[len(head):]
    if rest.startswith(" (reply "):
        rest = rest.partition(")")[2]
    return rest.startswith(f" — {_BUILD_REMOVED_OUTCOME}")


def apply_gap_decision(
    tracker: IssueTracker, journal: Journal, issue_number: int, decision: GapDecision,
    *, note: str = "", reply_id: str = "",
) -> bool:
    """Apply Thomas's choice on a tripped gap; ``False`` only when a write failed.

    A non-empty *reply_id* is stamped into the journalled row as
    :func:`gap_decision_reply_stamp`, so a re-drained reply is not re-applied.

    *build* removes ``needs-decision`` (the attempt count restarts at that
    ``unlabeled`` event); *buy* keeps the gap parked; *drop* makes no host
    write — it takes effect only when an operator login closes the issue.
    """
    applied = True
    if decision == "build":
        applied = tracker.remove_label(issue_number, NEEDS_DECISION_LABEL)
        outcome = (
            _BUILD_REMOVED_OUTCOME
            if applied else f"{NEEDS_DECISION_LABEL} removal failed"
        )
    elif decision == "buy":
        outcome = "stays parked until its opener's closer closes it"
    else:
        outcome = "no host write; takes effect only when an operator closes the issue"
    if not applied:
        logger.warning("Gap #%d build decision could not clear its label", issue_number)
    guidance = f"; Thomas's note: {note}" if note else ""
    stamp = f" {gap_decision_reply_stamp(reply_id)}" if reply_id else ""
    journal.record_event(
        f"gap-decision #{issue_number}: {decision}{stamp} — {outcome}{guidance}",
        entry_type="audit_observation",
        triggered_by="evolution_fuel",
    )
    return applied

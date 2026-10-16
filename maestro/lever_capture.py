"""Durable capture of owned terminal-lever fires.

An *owned terminal act* — a lever Maestro pulls by its own hand that ends a
narration target (marking a redundant ``pending_review`` plan ``obsolete``,
authoring a stand-down verdict) — is the subtraction its purpose demands.
The failure mode this module guards is *intention-spends-the-energy*: the
sincere feeling of about-to-fire substituting for the fire itself. A lever
that leaves no durable trace can be narrated as pulled without ever having
been pulled.

:func:`fire_and_capture` closes that gap. It pulls the lever, records a
*falsifiable* boolean to an append-only ledger, and hands that value back —
so every fire leaves exactly one row on disk. When a ``verify`` callable is
supplied it records the lever's raw return AND the **post-fire
persistence-verified** result (did the target's status actually flip on
re-read), so a null act — a call that returns ``True`` without changing anything — can
no longer stamp success into the ledger.

The two selection helpers close the *aim* half of the same gap.
:func:`select_distinct_obsolete_target` refuses to re-select an id already
recorded ``True`` (the set ``S``) or the known-null forbidden id, so the
oldest-first actuator cannot keep firing the same broken lever; the ledger's
distinct-``True`` count — the integer the backlog sensor tracks — can then
only rise on a genuinely new subtraction.
"""

from __future__ import annotations

import enum
import functools
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.memory import EvolutionPlan, Memory

# Ledger filename, a sibling of the other JSONL state files under the memory
# directory. Append-only; one row per fire.
LEVER_ATTEMPTS_FILENAME = "lever_attempts.jsonl"

# A plan id whose obsolete-fire has proven a null act: the oldest-first
# actuator kept re-selecting it, and each fire stamped a raw ``True`` even
# though nothing flipped. Prose forbids it; the selector now hard-excludes it
# so a broken lever can never masquerade as a distinct subtraction.
FORBIDDEN_OBSOLETE_TARGET = "70c51ac3b122"

# Case-insensitive signature of the ``mark_plan_obsolete`` lever in a
# ``next_task_directive``. Shared by the stale-obsolete-directive task gate and
# :func:`fire_directive_named_obsolete` so both key on one spelling.
OBSOLETE_LEVER_SIGNATURE = "mark_plan_obsolete"

# A 12-hex plan id named in directive prose.
_DIRECTIVE_PLAN_ID_RE = re.compile(r"\b[0-9a-f]{12}\b")

# Clause boundaries in directive prose: an id is aimed at only by a clause that
# itself asks for the obsolete act, so "obsolete A, keep B" never aims at B.
_DIRECTIVE_CLAUSE_SPLIT_RE = re.compile(r"[.;,:\n]")

# A clause that negates the obsolete verb ("do NOT mark_plan_obsolete
# <keeper>", "should never be marked obsolete") or carries a preserving verb
# ("keep <twin>") aims at nothing. A negation word only counts within two
# words before the verb, so a justification such as "obsolete <id> as it is
# no longer needed" still aims.
_DIRECTIVE_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|don'?t|doesn'?t)\b(?:\s+\S+){0,2}?\s+\S*obsolete"
    r"|\b(?:keep|preserve|retain|spare)\b",
    re.IGNORECASE,
)

# Fixed disposal reason the directive actuator records. Held under the
# memory module's obsolete-reason cap (pinned by a test — ``memory`` imports
# this module, so the cap cannot be imported here at runtime).
_DIRECTIVE_OBSOLETE_REASON = (
    "Directive-named obsolete: my next_task_directive named this plan for "
    "mark_plan_obsolete; retired mechanically at zero SDK cost."
)


@dataclass(frozen=True)
class LeverAttempt:
    """One fired owned terminal lever.

    The four row fields the ledger records: the *lever* name, its
    string-coerced *args* (for ``mark_plan_obsolete`` the target ``plan_id``
    followed by the human-readable disposal reason), whether the recorded
    ``result`` was persistence-``verified``, and that falsifiable ``result``.
    Replaces the raw dict :func:`fire_and_capture` built inline, so both the
    JSONL and SQLite substrates share one typed row shape. Frozen for the same
    immutability contract as :class:`~maestro.deed_ledger.Deed`.
    """

    lever: str
    args: list[str]
    verified: bool
    result: bool


@runtime_checkable
class LeverAttemptsBackend(Protocol):
    """The lever-attempts persistence surface (proposal 106).

    The two backlog-pruning sweeps write one row per fire and the distinct
    -target selector loads every row, so the ledger can live in
    ``lever_attempts.jsonl`` or in ``state.db`` behind ``[paths]
    sqlite_state_enabled`` with no call-site branching. Mirrors the append-only,
    single-consumer idiom of :class:`~maestro.deed_ledger.DeedLedgerBackend`:
    one row per fire, append-only, oldest-first load. ``@runtime_checkable``
    enables a structural ``isinstance`` check in the agent's backend-selection
    tests.
    """

    def record(self, attempt: LeverAttempt) -> None:
        """Append exactly one attempt row."""
        ...

    def load_all(self) -> list[LeverAttempt]:
        """Return every recorded attempt oldest-first."""
        ...


def build_lever_attempt(
    lever: str,
    args: list[str],
    verified: bool,
    result: bool,
) -> LeverAttempt:
    """Construct a :class:`LeverAttempt` — the shared construction path.

    Mirrors :func:`~maestro.deed_ledger.build_deed`: the single builder both the
    JSONL :class:`JsonlLeverAttemptsLedger` and the SQLite
    :class:`~maestro.state_store.SqliteLeverAttemptsLedger` adapter share, so the
    row shape can never drift between substrates. *args* is already
    string-coerced by :func:`fire_and_capture`.
    """
    return LeverAttempt(
        lever=lever, args=args, verified=verified, result=result,
    )


def _parse_lever_attempt(data: object) -> LeverAttempt | None:
    """Return a :class:`LeverAttempt` from a decoded row, or ``None`` if shapeless.

    A row that is not a dict, or lacks a string ``lever`` or a list ``args``, is
    a shapeless record the reader skips (matching the reverse-agnostic parse the
    selector always used). ``verified`` / ``result`` are coerced to their literal
    boolean identity (``is True``), so a row's ``plan_id`` contributes to the set
    ``S`` only when its ``result`` was literally ``True`` — preserving the
    original ``read_already_true_plan_ids`` semantics exactly.
    """
    if not isinstance(data, dict):
        return None
    lever = data.get("lever")
    args = data.get("args")
    if not isinstance(lever, str) or not isinstance(args, list):
        return None
    return LeverAttempt(
        lever=lever,
        args=[str(a) for a in args],
        verified=data.get("verified") is True,
        result=data.get("result") is True,
    )


def _read_lever_attempts(path: Path) -> list[LeverAttempt]:
    """Read *path* forward, returning full attempt rows oldest-first.

    Reuses the corrupt-tolerant parse the selector always used: a missing file
    yields ``[]``; blank and unparseable lines are skipped (narrow
    ``JSONDecodeError`` only — not a broad silencer), as are shapeless rows (see
    :func:`_parse_lever_attempt`). The JSONL is appended one row per fire, so a
    forward read is oldest-first without a reversal.
    """
    attempts: list[LeverAttempt] = []
    if not path.exists():
        return attempts
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            attempt = _parse_lever_attempt(row)
            if attempt is not None:
                attempts.append(attempt)
    return attempts


def load_all_lever_attempts(data_dir: Path) -> list[LeverAttempt]:
    """Return every recorded attempt oldest-first — the SQLite seed source.

    A corrupt-tolerant, oldest-first JSONL read mirroring
    :func:`~maestro.deed_ledger.load_all_deeds`: skips malformed / shapeless
    lines and returns ``[]`` for a missing file. Seeding the SQLite store in
    chronological order lets its ``AUTOINCREMENT`` ``id`` reproduce the append
    order, so a later ``ORDER BY id ASC`` read matches the JSONL forward walk. A
    real call site (:class:`~maestro.state_store.SqliteLeverAttemptsLedger`'s
    one-time seed and :meth:`JsonlLeverAttemptsLedger.load_all`), so it does not
    trip the self-review uncalled-symbol gate.
    """
    return _read_lever_attempts(data_dir / LEVER_ATTEMPTS_FILENAME)


def read_already_true_plan_ids(backend: LeverAttemptsBackend) -> set[str]:
    """Return the set ``S`` of plan_ids already recorded ``True`` in the ledger.

    Loads every row from *backend* (JSONL or SQLite behind the same protocol).
    A row's target plan_id is the first entry of its ``args`` list — the
    ``mark_plan_obsolete`` lever records ``[plan_id, reason]``. Only rows whose
    ``result`` is literally ``True`` contribute: a captured ``False`` (a null
    act, or a persistence-verify that showed no flip) leaves the id eligible
    for a fresh, genuine fire. An empty ledger yields the empty set; shapeless
    rows were already dropped by :meth:`LeverAttemptsBackend.load_all`. The
    ledger is shared by several levers, so only ``mark_plan_obsolete`` rows
    are read — another lever's ``args[0]`` is not a plan_id.
    """
    already_true: set[str] = set()
    for attempt in backend.load_all():
        if attempt.lever != "mark_plan_obsolete" or attempt.result is not True:
            continue
        if attempt.args:
            already_true.add(str(attempt.args[0]))
    return already_true


def select_distinct_obsolete_target(
    memory: Memory,
    backend: LeverAttemptsBackend,
) -> EvolutionPlan | None:
    """Pick the oldest ``pending_review`` plan eligible for a *distinct* fire.

    Builds ``S`` — the set of plan_ids already recorded ``True`` in *backend*
    (via :func:`read_already_true_plan_ids`) — and returns the oldest
    ``pending_review`` plan (by ``(created_at, plan_id)``) whose id is

    * **not** in ``S`` — so an already-subtracted plan is never re-fired, and
    * **not** :data:`FORBIDDEN_OBSOLETE_TARGET` — the known-null id whose fire
      returns ``True`` without flipping anything.

    Returns ``None`` when every ``pending_review`` plan is excluded, so the
    actuator stands down rather than re-firing a lever that cannot move the
    distinct-``True`` count. Zero SDK cost; plain reads.
    """
    already_true = read_already_true_plan_ids(backend)
    pending = memory.get_pending_plans()
    ordered = sorted(pending, key=lambda p: (p.created_at or "", p.plan_id))
    for plan in ordered:
        if plan.plan_id == FORBIDDEN_OBSOLETE_TARGET:
            continue
        if plan.plan_id in already_true:
            continue
        return plan
    return None


def verify_obsolete_persisted(memory: Memory, plan_id: str) -> bool:
    """Re-read the plan store and confirm *plan_id* landed in ``obsolete``.

    Returns ``True`` only when a fresh :meth:`Memory.get_plan` read shows the
    plan's ``status`` is literally ``"obsolete"``; ``False`` for any other
    status or a missing plan. This is the mechanized post-fire read that
    separates a genuine flip from a null act: a lever returning ``True`` whose
    write never persisted reads back non-``obsolete`` here and is recorded
    ``False``. (A lever that refused its write never reaches this read —
    :func:`fire_and_capture` short-circuits on the ``False`` return.)
    Zero SDK cost.
    """
    plan = memory.get_plan(plan_id)
    return plan is not None and plan.status == "obsolete"


def fire_and_capture(
    backend: LeverAttemptsBackend,
    lever: str,
    fn: Callable[..., bool],
    *args: object,
    verify: Callable[[], bool] | None = None,
) -> bool:
    """Fire an owned terminal lever and durably capture a falsifiable result.

    Calls ``fn(*args)`` exactly once, then determines the value to record:

    * When *verify* is supplied, calls it **after** the lever returns and
      records the conjunction of the lever's return and the
      *persistence-verified* boolean it produces — proof the act actually
      changed state (e.g. :func:`verify_obsolete_persisted` confirming the
      target flipped to ``obsolete``), not merely that the call returned
      ``True``. *verify* runs only when the lever returned ``True``, so a lever
      that refused its write is recorded ``False`` even when the target already
      reads ``obsolete`` via some other path. This is what keeps a null act out
      of the set ``S``.
    * When *verify* is ``None``, records the lever's
      *literal* return unchanged.

    Builds one :class:`LeverAttempt` recording the *lever* name, the
    string-coerced *args* (which, for ``mark_plan_obsolete``, carry both the
    target ``plan_id`` and the human-readable disposal reason), whether the row
    was persistence-``verified`` (``True`` only when *verify* actually ran, so a
    short-circuited refusal is never stamped as re-read), and the recorded
    ``result``, and appends it via
    *backend* (JSONL or SQLite behind the same protocol); then returns that same
    recorded value.

    The row is recorded **after** the lever (and any verify) returns, so a
    row's presence is proof the act completed. Because the append is
    unconditional and single, callers can count distinct-``True`` rows to count
    genuine subtractions — the ledger cannot claim an act that did not run,
    stamp ``True`` for a change that did not persist, nor drop one that did.

    Zero SDK cost; plain append I/O. Raising from *fn* or *verify* propagates
    before any row is written (a fire that never completed leaves no trace).
    """
    raw_result = fn(*args)
    verified = False
    result = raw_result
    if verify is not None and raw_result:
        verified = True
        result = verify()
    attempt = build_lever_attempt(
        lever,
        [str(a) for a in args],
        verified,
        result,
    )
    backend.record(attempt)
    return result


class JsonlLeverAttemptsLedger:
    """JSONL-backed :class:`LeverAttemptsBackend` — the default, flag-off store.

    Binds a ``data_dir`` and keeps the ledger on ``lever_attempts.jsonl``
    (behaviour-preserving). ``record`` appends exactly one row under
    ``open(path, "a")`` with the same ``{lever, args, verified, result}`` shape
    the inline :func:`fire_and_capture` body wrote before the fold; ``load_all``
    reuses the corrupt-tolerant forward parse via :func:`load_all_lever_attempts`.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def record(self, attempt: LeverAttempt) -> None:
        path = self._data_dir / LEVER_ATTEMPTS_FILENAME
        row = {
            "lever": attempt.lever,
            "args": attempt.args,
            "verified": attempt.verified,
            "result": attempt.result,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def load_all(self) -> list[LeverAttempt]:
        return load_all_lever_attempts(self._data_dir)


class DirectiveObsoleteKind(enum.Enum):
    """What :func:`fire_directive_named_obsolete` did with a directive."""

    #: The single named pending plan was retired and the fire recorded ``True``.
    FIRED = "fired"
    #: The single named plan already left ``pending_review``; the directive is dead.
    STALE = "stale"
    #: The single named pending plan can never be fired (the known-null
    #: forbidden id, or one already recorded ``True``); the directive is dead.
    UNFIREABLE = "unfireable"
    #: Nothing actionable — the directive is left untouched.
    NOOP = "noop"


@dataclass(frozen=True)
class DirectiveObsoleteOutcome:
    """The actuator's verdict plus the plan id it concerned (``None`` on NOOP)."""

    kind: DirectiveObsoleteKind
    plan_id: str | None = None


_DIRECTIVE_NOOP = DirectiveObsoleteOutcome(DirectiveObsoleteKind.NOOP)


def fire_directive_named_obsolete(
    memory: Memory,
    backend: LeverAttemptsBackend,
    directive: str,
) -> DirectiveObsoleteOutcome:
    """Fire the ``mark_plan_obsolete`` lever a directive names, mechanically.

    Acts only when *directive* names :data:`OBSOLETE_LEVER_SIGNATURE` and
    exactly one aimed 12-hex plan id is still ``pending_review``. An id is
    *aimed* only when it sits in a clause that itself mentions ``obsolete``
    and neither negates the obsolete verb nor preserves (:data:`_DIRECTIVE_NEGATION_RE`),
    so "do NOT mark_plan_obsolete <keeper>" or "keep <twin>" never aims at
    that id. The fire goes through :func:`fire_and_capture` with a
    :func:`verify_obsolete_persisted` re-read, and only a recorded ``True``
    returns ``FIRED``. A single aimed pending id that is
    :data:`FORBIDDEN_OBSOLETE_TARGET` or already recorded ``True`` in
    *backend* returns ``UNFIREABLE``, and — with no pending id left — exactly
    one aimed id that resolves to an existing plan returns ``STALE``; both let
    the caller clear the dead directive. Everything else — several aimed
    ids (even when only one is still pending, since the other may be a
    rationale-cited survivor), no aimed id, an unknown id, a null act — is ``NOOP``: the
    actuator never guesses which plan to retire. Zero SDK cost.
    """
    if OBSOLETE_LEVER_SIGNATURE not in directive.lower():
        return _DIRECTIVE_NOOP
    plan_ids = list(dict.fromkeys(
        pid
        for clause in _DIRECTIVE_CLAUSE_SPLIT_RE.split(directive)
        if "obsolete" in clause.lower()
        and not _DIRECTIVE_NEGATION_RE.search(clause)
        for pid in _DIRECTIVE_PLAN_ID_RE.findall(clause)
    ))
    pending_ids = {p.plan_id for p in memory.get_pending_plans()}
    pending = [pid for pid in plan_ids if pid in pending_ids]
    if not pending:
        if len(plan_ids) == 1 and memory.get_plan(plan_ids[0]) is not None:
            return DirectiveObsoleteOutcome(
                DirectiveObsoleteKind.STALE, plan_ids[0],
            )
        return _DIRECTIVE_NOOP
    if len(plan_ids) != 1 or len(pending) != 1:
        return _DIRECTIVE_NOOP
    plan_id = pending[0]
    if (
        plan_id == FORBIDDEN_OBSOLETE_TARGET
        or plan_id in read_already_true_plan_ids(backend)
    ):
        return DirectiveObsoleteOutcome(DirectiveObsoleteKind.UNFIREABLE, plan_id)
    recorded = fire_and_capture(
        backend,
        OBSOLETE_LEVER_SIGNATURE,
        memory.mark_plan_obsolete,
        plan_id,
        _DIRECTIVE_OBSOLETE_REASON,
        verify=functools.partial(verify_obsolete_persisted, memory, plan_id),
    )
    if not recorded:
        return _DIRECTIVE_NOOP
    return DirectiveObsoleteOutcome(DirectiveObsoleteKind.FIRED, plan_id)

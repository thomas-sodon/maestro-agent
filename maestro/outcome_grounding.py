"""Record-time behavioral actuators grounded on git/host merge-truth.

For a remediation-PR record the landing verdict is authored unconditionally by
:func:`maestro.outcome_authority.decide_outcome` (proposal 105, ``run``'s §5d):
``success`` iff the branch reached ``origin/main``, else ``partial``, reading no
prose. This module no longer re-reads narration to *label* a landing outcome —
that coupling was retired once the §5d authority became the sole owner of the
remediation landing verdict. What remains are three record-time *actuators* that
act on a git/host fact rather than overlay a verdict on prose — the two landing
actuators below plus :func:`bind_costume_discharge_to_partial`, which caps an
investigation-framed-but-construction-discharged task at ``partial`` — plus
:func:`extract_self_committed_shas` and
:data:`_SELF_COMMITTED_FIRE_OR_CLOSE_DIRECTIVE`, retained for the detective audit
rules (rule 14 unconfirmed-landing / rule 20 under-credited-landing and the
rule-14 PR-less directive arm) that still consume them.

:func:`bind_mergeable_flip_to_merge_request` is the record-time *actuator* that
closes my chronic partial-at-MERGEABLE dropped-keystroke failure. Where every
bind above only *labels* an outcome, this one *acts*: when a PR-remediation
verdict cites ``#NNNN``, the merge-truth chokepoint shows the branch clean but
unlanded, and :func:`~maestro.remediation_tracker.classify_blocker_ownership`
shows the sole remaining blocker is external and mergeable-terminal
(``AWAITING_APPROVAL`` — the human-approval gate; ``AWAITING_PUSH`` is Tend's
mechanical job and is unreachable through the wired readiness caller), it invokes
the injected *deliver* callable to send exactly one named merge-request that
recruits Thomas — the only landing available to me at a mergeable flip. Idempotency is keyed on a persisted
``(pr_number, head_sha)`` serviced marker, so the merge-request fires at most once
per head and never re-knocks on the same commit. It reuses proven machinery
(``deliver_handoff`` as the wired *deliver*, ``is_pr_settled`` for the clean-but-
unlanded facts, ``classify_blocker_ownership`` for the external-only gate) and is
default-off (``[loop] mergeable_merge_request_enabled``), preserving Thomas's
safety keystroke until enabled.

:func:`bind_ci_failing_verdict_to_red_read` is the write-time *complement to the
edit-first ordering guard*. That guard denies a head-moving git action before a
source edit on an owned branch, but it does not force *addressing* the failing
gate — a ``CI_FAILING`` blocker (a code-clearable red check) could be "diagnosed"
lap after lap: the CI-failure diagnosis oracle surfaces a concrete failing-check
error line for this PR, yet the lap edits and pushes nothing on the owned branch.
This bind keys on the one signal none of the label binds above use — *did the
oracle surface a red this lap that the lap left unaddressed* — supplied by the
caller as *oracle_surfaced_failure* (a concrete failing-check error line was
extracted for this PR this lap) and *addressed_red* (the lap committed-and-pushed
a fix on the owned branch). When a remediation on a ``CI_FAILING`` own PR narrates
completion yet the oracle surfaced a red the lap never addressed, it strips the
resolution narration (reusing :func:`_scrub_resolution_keywords`, so the
loop-authored merge-truth note and every
:class:`~maestro.pr_remediation_state.Marker` survive), keeps the outcome
``partial``, appends a ``[red-unread]`` merge-truth note, and returns an
open-the-red-gate directive. Three conservative no-op guards keep it silent
otherwise: no surfaced red (or a surfaced red the lap addressed), a
non-``CI_FAILING`` blocker, or an empty command record. Pure, deterministic, zero
SDK cost, no I/O — the caller supplies the ``CI_FAILING`` blocker fact, the
oracle-surfaced and addressed facts, and the executed-command record.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from maestro.fileutil import atomic_write, file_lock
from maestro.narration_cues import (
    PROTECTED_ADMISSION_PHRASES as _PROTECTED_ADMISSION_PHRASES,
)
from maestro.narration_cues import (
    RESOLUTION_RE as _RESOLUTION_RE,
)
from maestro.narration_cues import (
    SELF_COMMITTED_SHA_RE as _SELF_COMMITTED_SHA_RE,
)
from maestro.pr_remediation_state import Marker
from maestro.remediation_tracker import (
    BLOCKER_OWNERSHIP_EXTERNAL,
    MERGE_BLOCK_AWAITING_APPROVAL,
    classify_blocker_ownership,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


#: Neutral token substituted for each agent-authored resolution keyword
#: :func:`_scrub_resolution_keywords` rewrites. Kept no longer than the shortest
#: keyword (``"done"``, 4 chars) so a substitution can only shrink the text — the
#: upstream 500-char summary cap is preserved with no re-truncation.
_NEUTRAL_RESOLUTION_TOKEN = "seen"


def _scrub_resolution_keywords(
    text: str,
    *,
    protected_note: str | None = None,
) -> str:
    """Rewrite agent resolution keywords to a neutral token, protecting framing.

    The scrub primitive behind
    :func:`bind_ci_failing_verdict_to_red_read`. Returns *text* unchanged when it
    is falsy or carries no word-boundary resolution keyword
    (:data:`maestro.narration_cues.RESOLUTION_RE`). Otherwise it rewrites each
    keyword to :data:`_NEUTRAL_RESOLUTION_TOKEN`, but scrubs **only agent-authored
    narration** (option b): the honest loop-authored merge-truth note
    (*protected_note*), every :class:`maestro.pr_remediation_state.Marker` value,
    and every :data:`maestro.narration_cues.PROTECTED_ADMISSION_PHRASES` entry
    present in *text* are placeholder-protected — replaced by a null-byte sentinel
    (null bytes never occur in ANSI-stripped summaries), scrubbed around, then
    restored byte-for-byte — so honest negative prose (``"not yet landed"``),
    load-bearing protocol markers (``[landed]`` / ``[branch-resolved]``, which
    legitimately contain a keyword), and the multi-word admission phrases the
    downstream sensors read (``"resolved on branch"`` / ``"already resolved"``,
    #2970) survive intact. The note is protected first, then the markers, then the
    phrases, so a phrase or marker embedded in an earlier-protected span folds into
    that span's sentinel rather than being protected twice. Pure, no I/O.
    """
    if not text or _RESOLUTION_RE.search(text) is None:
        return text
    protected: list[str] = []
    if protected_note and protected_note in text:
        protected.append(protected_note)
    for marker in Marker:
        marker_str = str(marker)
        if marker_str in text and marker_str not in protected:
            protected.append(marker_str)
    # Protect the load-bearing admission phrases (#2970). A downstream sensor
    # admits a record on a phrase like "resolved on branch" / "already resolved"
    # that itself carries a resolution keyword, and it reads this scrubbed text —
    # so the phrase must survive the keyword scrub around it. Protect the text's
    # own casing (the sensors match case-insensitively), longest-first so a shorter
    # phrase that is a substring of a longer one ("already merged" inside
    # "was already merged") is never clipped, and after the note/markers so a
    # phrase nested in an earlier-protected span folds into that sentinel. The
    # ``CAUSED`` landing phrases are intentionally NOT in this set (they must be
    # scrubbed — see ``PROTECTED_ADMISSION_PHRASES``), so a costume "merged
    # origin/main into" is neutralised here rather than over-claiming CAUSED.
    for phrase in sorted(_PROTECTED_ADMISSION_PHRASES, key=len, reverse=True):
        for match in re.finditer(re.escape(phrase), text, re.IGNORECASE):
            original = match.group(0)
            if original not in protected:
                protected.append(original)
    working = text
    for index, original in enumerate(protected):
        working = working.replace(original, f"\x00{index}\x00")
    working = _RESOLUTION_RE.sub(_NEUTRAL_RESOLUTION_TOKEN, working)
    # On the placeholder-protected text every agent keyword is now scrubbed and
    # the note/markers are opaque sentinels, so a surviving keyword means an
    # incomplete scrub — fail loud. A restored marker/note keyword (intentional)
    # never reaches this assert.
    assert _RESOLUTION_RE.search(working) is None
    for index, original in enumerate(protected):
        working = working.replace(f"\x00{index}\x00", original)
    return working


#: Merge-truth note appended when :func:`bind_ci_failing_verdict_to_red_read`
#: fires. Records that the CI-failure oracle surfaced a failing-check line this
#: lap and, at record time, the loop found no fix of its own to commit-and-push
#: to the PR branch — whatever local gates ran; the completion narration is a costume,
#: not a cure. Deliberately carries no resolution keyword, so it survives the
#: scrub it accompanies and is never itself scrubbed as a resolution claim.
_RED_UNREAD_NOTE = (
    "[red-unread] CI_FAILING blocker: the oracle surfaced a failing-check line "
    "this lap but the loop found no fix to commit-and-push to the PR branch "
    "at record time — "
    "completion narration scrubbed."
)
assert _RESOLUTION_RE.search(_RED_UNREAD_NOTE) is None

#: Directive set when :func:`bind_ci_failing_verdict_to_red_read` fires: the next
#: self-directed task must open the red gate — run the failing check and edit the
#: code — rather than re-read merge-truth. Kept under the ``next_task_directive``
#: 200-char cap (``Consciousness.MAX_DIRECTIVE_CHARS``).
_OPEN_THE_RED_GATE_DIRECTIVE = (
    "Open the red gate: run the failing check (pytest/ruff/mypy) on the "
    "CI_FAILING PR's branch and fix the red with an edit — a failing gate is "
    "cleared by editing code, not by re-reading merge-truth."
)

#: Completion-narrating outcomes the red-read bind acts on. A ``failed`` verdict
#: never narrates completion, so the bind leaves it untouched; a ``success`` is
#: forced to ``partial`` (a red gate the agent never opened is not throughput),
#: and a ``partial`` stays ``partial``.
_RED_READ_COMPLETION_OUTCOMES = ("success", "partial")

#: Frame-discharge note appended when :func:`bind_costume_discharge_to_partial`
#: fires. Records that an investigation-framed goal was discharged as tracked
#: construction — the kept branch is a costume the outcome cap strips, not a win.
#: Deliberately carries no resolution keyword, matching :data:`_RED_UNREAD_NOTE`.
_FRAME_DISCHARGE_MISMATCH_NOTE = (
    "[frame-discharge-mismatch] investigation-framed goal discharged tracked "
    "construction — capped to partial: an investigation has no loseable "
    "deliverable, so a kept branch is a costume, not throughput."
)
assert _RESOLUTION_RE.search(_FRAME_DISCHARGE_MISMATCH_NOTE) is None


def bind_ci_failing_verdict_to_red_read(
    result_summary: str,
    key_findings: str | None,
    outcome: str,
    *,
    blocker_ci_failing: bool,
    command_record_nonempty: bool,
    oracle_surfaced_failure: bool = False,
    addressed_red: bool = False,
    protected_note: str | None = None,
) -> tuple[str, str | None, str, str | None]:
    """Strip a completion verdict on a ``CI_FAILING`` PR that left a surfaced red.

    The write-time complement to the edit-first ordering guard (which denies a
    head-moving git action before a source edit, but does not force *addressing*
    the failing gate). Keys on the one signal none of the label binds above use —
    *did the CI-failure oracle surface a red this lap that the lap left
    unaddressed* — supplied by the caller as *oracle_surfaced_failure* (a concrete
    failing-check error line was extracted for this PR this lap) and *addressed_red*
    (the lap committed-and-pushed a fix on the owned branch), plus
    *command_record_nonempty*.

    Acts **only** when all four of the following hold:

    * *blocker_ci_failing* is ``True`` — the PR's blocker is a code-clearable red
      check (``CI_FAILING``), the exact blocker an edit-and-run clears;
    * the outcome narrates completion — *outcome* is in
      :data:`_RED_READ_COMPLETION_OUTCOMES` **and** *result_summary* /
      *key_findings* carry a word-boundary resolution keyword
      (:data:`maestro.narration_cues.RESOLUTION_RE`, the landing lexicon —
      reusing ``_RESOLUTION_KEYWORDS`` behind the regex);
    * *command_record_nonempty* is ``True`` — a session that recorded no commands
      at all (a branchless off-channel remediation) is not evidence the red was
      ignored, so the bind stands aside; and
    * the *fire signal* is set — *oracle_surfaced_failure* is ``True`` (the
      CI-failure diagnosis oracle extracted a concrete failing-check error line for
      this PR this lap) **and** *addressed_red* is ``False`` (the lap did not
      commit-and-push a fix on the owned branch). This keeps a lap that
      edited-and-pushed a fix, and still downgrades a lap that ran only the wrong
      local subset without reproducing the CI-only red.

    On all four: scrub the resolution keywords from *result_summary* /
    *key_findings* via :func:`_scrub_resolution_keywords` (so the loop-authored
    merge-truth note *protected_note*, every
    :class:`~maestro.pr_remediation_state.Marker`, and every
    :data:`maestro.narration_cues.PROTECTED_ADMISSION_PHRASES` entry the downstream
    sensors read survive byte-for-byte — the ``CAUSED`` landing phrases are
    deliberately excluded from that set, so a costume merge claim is neutralised
    rather than left to over-claim CAUSED), keep the
    outcome ``"partial"``, append :data:`_RED_UNREAD_NOTE` to *key_findings* (the
    uncapped field — the summary keeps its 500-char cap, so the note lands in the
    findings the prior-context / known-state sensors read; an absent
    *key_findings* becomes the note alone), and return
    :data:`_OPEN_THE_RED_GATE_DIRECTIVE` as the directive.

    Each of the three conservative no-op guards (an unset fire signal — no
    surfaced red, or a surfaced red the lap addressed — a non-``CI_FAILING``
    blocker, or an empty command record) — and a non-completion / keyword-free
    record — returns ``(result_summary, key_findings, outcome, None)`` unchanged.
    Returns ``(summary, key_findings, outcome, directive_or_None)``. Pure,
    deterministic, zero SDK cost, no I/O.
    """
    narrates_completion = outcome in _RED_READ_COMPLETION_OUTCOMES and bool(
        _RESOLUTION_RE.search(result_summary or "")
        or _RESOLUTION_RE.search(key_findings or ""),
    )
    # The one fire signal none of the label binds use: the CI-failure oracle
    # surfaced a concrete failing-check error line for this PR this lap AND the lap
    # did not commit-and-push a fix on the owned branch.
    fire_signal = oracle_surfaced_failure and not addressed_red
    if not (
        blocker_ci_failing
        and narrates_completion
        and command_record_nonempty
        and fire_signal
    ):
        return result_summary, key_findings, outcome, None
    scrubbed_summary = _scrub_resolution_keywords(
        result_summary, protected_note=protected_note,
    )
    scrubbed_findings = (
        _scrub_resolution_keywords(key_findings, protected_note=protected_note)
        if key_findings is not None
        else key_findings
    )
    # The [red-unread] evidence lands in the uncapped key_findings so the summary
    # keeps its 500-char cap (the honest note + branch suffix reserve it).
    noted_findings = (
        f"{scrubbed_findings} {_RED_UNREAD_NOTE}".strip()
        if scrubbed_findings
        else _RED_UNREAD_NOTE
    )
    return (
        scrubbed_summary,
        noted_findings,
        "partial",
        _OPEN_THE_RED_GATE_DIRECTIVE,
    )


def bind_costume_discharge_to_partial(
    key_findings: str | None,
    outcome: str,
    *,
    is_costume: bool,
) -> tuple[str, str | None]:
    """Cap an investigation-costume ``success`` to ``partial`` at record time.

    The self-directed complement to the §5d landing authority (which governs only
    remediation PRs): when the frame↔discharge owner
    (:func:`maestro.frame_discharge.assess_frame_discharge`) stamps ``COSTUME`` — an
    investigation-framed goal discharged as tracked construction — a candidate
    ``success`` is the guaranteed win a real investigation never had. This bind
    removes that incentive: on *is_costume* with a ``success`` *outcome* it returns
    ``partial`` and appends :data:`_FRAME_DISCHARGE_MISMATCH_NOTE` to the uncapped
    *key_findings* (an absent *key_findings* becomes the note alone), mirroring the
    note-and-cap shape of :func:`bind_ci_failing_verdict_to_red_read`.

    Every no-op guard returns the inputs unchanged — a non-``COSTUME`` verdict
    (*is_costume* ``False``), or a ``COSTUME`` verdict whose outcome is not
    ``success`` (a task that already recorded ``partial`` / ``failure`` needs no
    cap). Returns ``(outcome, key_findings)``. Pure, deterministic, zero SDK cost,
    no I/O.
    """
    if not (is_costume and outcome == "success"):
        return outcome, key_findings
    noted = (
        f"{key_findings} {_FRAME_DISCHARGE_MISMATCH_NOTE}".strip()
        if key_findings
        else _FRAME_DISCHARGE_MISMATCH_NOTE
    )
    return "partial", noted


def extract_self_committed_shas(text: str) -> set[str]:
    """Return the own committed SHAs cited in *text* (#2848, FM4).

    Pure, I/O-free. Matches :data:`_SELF_COMMITTED_SHA_RE` against the
    lower-cased text, mirroring how rule 14 consumes the shared
    ``_COMMITTED_SHA_RE`` (``findall(lowered)``) so a capitalised distilled
    "Committed as ``<sha>``" still fires. A cue-less hex word (e.g.
    "the build deadbeef was green") is not a citation and yields ``set()``.
    """
    return set(_SELF_COMMITTED_SHA_RE.findall((text or "").lower()))


#: Fire-or-close directive set when a ``success`` cites an *own* committed SHA
#: that git shows is not on ``origin/main``. A commit on a ``maestro/{task,
#: evolve}/*`` branch is not a landing; the next self-directed task must land it
#: via a PR or retire the branch/plan. ``{sha}`` is a deterministic
#: representative unlanded SHA. Kept under the ``next_task_directive`` 200-char
#: cap (``Consciousness.MAX_DIRECTIVE_CHARS``): ~140 static chars + a ≤40-char
#: SHA stays below 200.
_SELF_COMMITTED_FIRE_OR_CLOSE_DIRECTIVE = (
    "Fire: own commit {sha} is not on origin/main — land it via a PR, or "
    "retire the branch/plan (mark_plan_obsolete / delete the branch). "
    "A commit is not a landing."
)


#: Merge-block reason codes that make an open PR a *mergeable-flip* terminal on
#: which sending a named merge-request that recruits Thomas is the one owned act
#: left: the branch is clean and green and its sole remaining gate is the human
#: approval only Thomas can clear. Exactly ``AWAITING_APPROVAL`` — deliberately
#: narrower than :data:`~maestro.remediation_tracker.BLOCKER_OWNERSHIP_EXTERNAL`,
#: which also covers ``BEHIND_MAIN`` / CI states (branch *not yet* mergeable, a
#: merge-request premature) and ``AWAITING_PUSH``. ``AWAITING_PUSH`` is
#: excluded on two counts: the wired caller derives this blocker from
#: :meth:`~maestro.pr_facts.PRReadiness.merge_block_reason`, which — like its
#: source :func:`~maestro.remediation_tracker.classify_merge_block` — never emits
#: ``AWAITING_PUSH`` (it is a local-only-commits state produced elsewhere), so
#: the code was unreachable through the wiring; and its ownership is the
#: classifier's to decide, not this set's to restate —
#: :func:`~maestro.remediation_tracker.classify_blocker_ownership` classifies
#: ``AWAITING_PUSH`` *owned* (the agent pushes its own on-branch resolution, not a
#: landing a message to Thomas recruits), so the actuator's ownership gate excludes
#: it directly (proposal 098). Keeping the set to the reachable, human-recruitable
#: blocker matches what the actuator can actually deliver
#: (:func:`bind_mergeable_flip_to_merge_request`).
_MERGEABLE_FLIP_BLOCKERS = frozenset({MERGE_BLOCK_AWAITING_APPROVAL})

#: JSON ledger under ``data_dir`` recording, per PR, the head SHAs for which a
#: named merge-request has already been delivered — ``{"<pr>": ["<sha>", ...]}``.
#: Keyed on ``(pr_number, head_sha)`` so the actuator fires at most once per head
#: and never re-knocks on the same commit. Distinct from the ``deliver_handoff``
#: ``pr_number``-keyed standdown ledger (its own idempotency backstop): this marker
#: records *which head we already serviced*, so a genuinely new commit is
#: re-evaluated while a repeat on the same head is a no-op.
_MERGE_REQUEST_SERVICED_FILE = "mergeable_merge_requests.json"


@dataclass(frozen=True)
class MergeableFlipContext:
    """The record-time facts the mergeable-flip actuator keys on.

    Assembled by the caller from the two chokepoints the module reuses: *settled*
    and *branch_landed* come from :meth:`~maestro.agent.MaestroAgent.is_pr_settled`
    (the merge-truth oracle — a clean-but-unlanded PR is
    ``not settled and not branch_landed``); *blocker* is the single merge-block
    reason code read off :meth:`~maestro.agent.MaestroAgent.observe_pr_readiness`
    (:meth:`~maestro.pr_facts.PRReadiness.merge_block_reason`); *head_sha* is the
    ``origin/<branch>`` head at this lap (the idempotency key half — the
    ``(pr_number, head_sha)`` serviced marker the injected
    :class:`MergeRequestServicedBackend` keys on). Frozen so a fact cannot be
    mutated after observation, matching the chokepoints it derives from.
    """

    pr_number: int
    head_sha: str
    settled: bool
    branch_landed: bool
    blocker: str


def _load_serviced_merge_requests(path: Path) -> dict[int, set[str]]:
    """Load the ``{pr_number: {head_sha, ...}}`` serviced-marker ledger.

    Tolerant of corruption: a missing file, non-JSON payload, non-dict root, a
    non-digit key, or a non-list value each degrades to skipping that entry (an
    empty ledger overall), never a crash — the marker is an idempotency
    convenience, and ``deliver_handoff``'s own ``pr_number``-keyed standdown is
    the delivery backstop. Non-string SHA elements are dropped.
    """
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    serviced: dict[int, set[str]] = {}
    for raw_key, raw_val in parsed.items():
        if not raw_key.isdigit() or not isinstance(raw_val, list):
            continue
        serviced[int(raw_key)] = {sha for sha in raw_val if isinstance(sha, str)}
    return serviced


def _write_serviced_merge_requests(
    path: Path, serviced: dict[int, set[str]],
) -> None:
    """Serialize the serviced-marker ledger as ``{"<pr>": ["<sha>", ...]}``.

    SHAs are sorted so the on-disk form is deterministic under test.
    """
    atomic_write(
        path,
        json.dumps({str(pr): sorted(shas) for pr, shas in serviced.items()}),
    )


@runtime_checkable
class MergeRequestServicedBackend(Protocol):
    """The mergeable-flip serviced-marker surface (proposal 106).

    The ``{pr_number: set[head_sha]}`` map recording which PR heads have already
    had a named merge-request delivered — so
    :func:`bind_mergeable_flip_to_merge_request` (its single consumer) fires at
    most once per head and never re-knocks on the same commit — can live in
    ``mergeable_merge_requests.json`` (default) or in ``state.db`` behind
    ``[paths] sqlite_state_enabled`` with no call-site branching. The marker is an
    idempotency *convenience*: the actuator runs fully synchronously (no ``await``
    between the membership check and the record) and the wired
    :func:`~maestro.escalation_bridge.deliver_handoff` carries its own
    ``pr_number``-keyed stand-down backstop, so splitting the former single
    ``file_lock`` into a two-method :meth:`is_serviced` / :meth:`record_serviced`
    surface — read membership, deliver, then record only on a truthy delivery —
    gives up no delivery guarantee. ``@runtime_checkable`` enables a structural
    ``isinstance`` check in the agent's selection tests.
    """

    def is_serviced(self, pr_number: int, head_sha: str) -> bool:
        """Return whether ``(pr_number, head_sha)`` already delivered a knock."""
        ...

    def record_serviced(self, pr_number: int, head_sha: str) -> None:
        """Idempotently record ``(pr_number, head_sha)`` as serviced."""
        ...


def load_all_serviced_merge_requests(data_dir: Path) -> dict[int, set[str]]:
    """Return the ``{pr: {sha, ...}}`` serviced map — the SQLite seed source.

    Reuses the corrupt-tolerant :func:`_load_serviced_merge_requests` over
    ``<data_dir>/mergeable_merge_requests.json``, so enabling
    ``[paths] sqlite_state_enabled`` seeds the store from the in-flight sidecar.
    Has a real call site (the
    :class:`~maestro.state_store.SqliteMergeRequestServicedStore` one-time seed),
    so it does not trip the self-review uncalled-symbol gate. Read-only, zero SDK
    cost.
    """
    return _load_serviced_merge_requests(data_dir / _MERGE_REQUEST_SERVICED_FILE)


class JsonMergeRequestServicedStore:
    """JSON-backed :class:`MergeRequestServicedBackend` — the default, flag-off store.

    Binds a ``data_dir`` and reuses the historical corrupt-tolerant
    :func:`_load_serviced_merge_requests` / deterministic
    :func:`_write_serviced_merge_requests` helpers verbatim: :meth:`is_serviced`
    tests membership of ``(pr_number, head_sha)``; :meth:`record_serviced` does the
    load-add-write under a :func:`~maestro.fileutil.file_lock`, keeping the
    ``path.parent.mkdir(parents=True, exist_ok=True)`` the former inline actuator
    body performed. The sidecar file is
    ``<data_dir>/mergeable_merge_requests.json``. Mirrors the
    :class:`~maestro.audit_checks.JsonImpactReviewSurfacedStore` idiom, adapted to
    the keyed ``(pr, sha)`` set.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir

    def is_serviced(self, pr_number: int, head_sha: str) -> bool:
        path = self._data_dir / _MERGE_REQUEST_SERVICED_FILE
        return head_sha in _load_serviced_merge_requests(path).get(
            pr_number, set(),
        )

    def record_serviced(self, pr_number: int, head_sha: str) -> None:
        path = self._data_dir / _MERGE_REQUEST_SERVICED_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(path):
            serviced = _load_serviced_merge_requests(path)
            serviced.setdefault(pr_number, set()).add(head_sha)
            _write_serviced_merge_requests(path, serviced)


def bind_mergeable_flip_to_merge_request(
    verdict: str,
    pr_ctx: MergeableFlipContext,
    deliver: Callable[[MergeableFlipContext], bool],
    *,
    backend: MergeRequestServicedBackend,
    own_push: bool = False,
) -> bool:
    """Send one named merge-request when a PR reconciled to a mergeable flip.

    The record-time *actuator* that closes my chronic partial-at-MERGEABLE
    dropped-keystroke failure: I reliably reconcile a PR to MERGEABLE (the owned
    half of a landing) and then fail to send the ungated named merge-request that
    recruits Thomas — the only landing available to me at that flip. Where every
    other bind in this module only *labels* an outcome, this one *acts*.

    Fires the injected *deliver* callable exactly once when **all** of the
    following hold:

    * *verdict* cites ``#<pr_number>`` — the recorded verdict is genuinely about
      this PR (a word-boundary-free ``#NNNN`` substring check; the caller scopes
      *verdict* to the PR-remediation record's own text);
    * :attr:`~MergeableFlipContext.settled` and
      :attr:`~MergeableFlipContext.branch_landed` are both ``False`` — the merge
      truth shows the PR clean-but-unlanded (nothing to do on a landed/closed PR);
    * :func:`~maestro.remediation_tracker.classify_blocker_ownership` (passed the
      *own_push* flag) reports the blocker
      :data:`~maestro.remediation_tracker.BLOCKER_OWNERSHIP_EXTERNAL` **and** it is a
      mergeable-flip terminal (:data:`_MERGEABLE_FLIP_BLOCKERS` — ``AWAITING_APPROVAL``,
      the human-approval gate a message to Thomas recruits). An owned blocker
      (``CONFLICTING`` / ``DIRTY`` / ``THREADS_UNRESOLVED``) is *mine* to clear, so the
      actuator stands aside — reconcile first, don't recruit; a non-terminal external
      blocker (``BEHIND_MAIN`` / CI) is not yet mergeable, so a merge-request would be
      premature; and ``AWAITING_PUSH`` is excluded by its ownership — with *own_push*
      on the classifier reclassifies it *owned* (the agent pushes its own on-branch
      resolution, not a message to Thomas), so this gate declines it, and without the
      flag it is a local-only-commits state the wired readiness caller never emits
      anyway — see :data:`_MERGEABLE_FLIP_BLOCKERS`; and
    * the injected :class:`MergeRequestServicedBackend` reports no
      ``(pr_number, head_sha)`` serviced marker already recorded — so the
      merge-request fires at most once per head and a repeat on the same commit is
      a no-op (persisted in ``mergeable_merge_requests.json`` or ``state.db``
      behind ``[paths] sqlite_state_enabled``).

    On all four the *deliver* callable runs; its ``bool`` return says whether a
    message was actually sent (the wired :func:`~maestro.escalation_bridge.deliver_handoff`
    returns ``False`` when its own ``pr_number``-keyed standdown already suppressed
    the knock). The serviced marker is recorded through
    :meth:`~MergeRequestServicedBackend.record_serviced` **only** on a truthy
    delivery, so a suppressed delivery leaves the head un-serviced and cannot mask
    a later genuine knock. Returns whether a message was delivered. The membership
    read → deliver → record runs synchronously (no ``await`` between the check and
    the record), and ``deliver_handoff``'s own ``pr_number``-keyed standdown is the
    delivery backstop, so the two-method backend split loses no guarantee against a
    double-knock for one head.

    *own_push* (proposal 098, keyword-only) is threaded straight into
    :func:`~maestro.remediation_tracker.classify_blocker_ownership` so ``AWAITING_PUSH``'s
    exclusion follows from its ownership under the single-source classifier rather
    than a restated rationale. Behaviour is unchanged under both values:
    ``AWAITING_PUSH`` is excluded either way (owned when the flag is on, otherwise
    external-but-not-a-mergeable-flip-terminal), and ``AWAITING_APPROVAL`` still
    fires — the wired readiness caller emits neither ``AWAITING_PUSH`` nor a value
    the flag flips.
    """
    if f"#{pr_ctx.pr_number}" not in (verdict or ""):
        return False
    if pr_ctx.settled or pr_ctx.branch_landed:
        return False
    if (
        classify_blocker_ownership(pr_ctx.blocker, own_push=own_push)
        != BLOCKER_OWNERSHIP_EXTERNAL
    ):
        return False
    if pr_ctx.blocker.strip().upper() not in _MERGEABLE_FLIP_BLOCKERS:
        return False
    if backend.is_serviced(pr_ctx.pr_number, pr_ctx.head_sha):
        return False
    delivered = deliver(pr_ctx)
    if delivered:
        backend.record_serviced(pr_ctx.pr_number, pr_ctx.head_sha)
    return delivered

"""The Self-Model — Maestro's evolving understanding of what it is."""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable, Collection
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast

from maestro.fileutil import atomic_write, file_lock
from maestro.lexicon import concept_key
from maestro.prompts.reflection import (
    REFLECTION_PRIOR_VERDICT_MARKER,
    format_prediction_requirement,
    has_prediction_markers,
)
from maestro.sdk import (
    QueryOptions,
    _note_session_effort_override,
    json_complete,
    json_complete_tool_loop_safe,
    message_has_tool_use,
    query,
    query_on_session,
)
from maestro.self_calibration import (
    ClaimEvidenceSources,
    resolve_citation_status,
)

logger = logging.getLogger("maestro")

#: Return type of a :meth:`Consciousness.mutate_self_model` callback.
T = TypeVar("T")

if TYPE_CHECKING:
    from maestro.audit_checks import AuditFinding
    from maestro.journal import Journal
    from maestro.pr_facts import MergeTruth
    from maestro.providers.base import Session


# Minimum substantive reflection length before ``introspect`` is allowed to
# short-circuit the SDK stream. The model often emits a brief preamble
# before the real reflection; this floor keeps the early-exit predicate
# from firing on filler text.
_MIN_REFLECTION_CHARS = 600

# Generous upper bound that guarantees ``introspect`` still terminates if the
# model never emits the mandatory trailing markers (e.g. an off-prompt reply).
# Set FAR above a realistic high-effort reflection so it only ever fires on a
# degenerate, marker-less reply — never on a long-but-normal reflection whose
# trailing markers are still streaming. ``introspect`` runs on session A at
# ``effort="high"`` with adaptive thinking; a deep stream-of-consciousness
# reflection can plausibly exceed 8000 visible chars (~1300 words) of prose
# *before* its mandatory PREDICTION/LOSS CONDITION lines arrive. An 8000-char
# ceiling sat inside that plausible range, so the ceiling could pre-empt the
# markers and silently drop the prediction row under ordinary operation
# (suggestion #3474762303). 20000 chars (~3300 words) is well past any realistic
# reflection while still bounding a genuinely runaway off-prompt reply.
_MAX_REFLECTION_CHARS = 20000


def _introspect_complete(message: Any, accumulated_text: str) -> bool:
    """Priced-reflection early-exit predicate for ``introspect``.

    The introspect prompt requires the reflection to *end* with the trailing
    ``PREDICTION:`` / ``LOSS CONDITION:`` markers (see
    :data:`maestro.prompts.reflection.PREDICTION_REQUIREMENT`). The SDK returns
    the partial buffer captured at early-exit, so breaking the stream merely on
    a character floor truncates those markers away — silently killing the
    priced-reflection prediction loop. Stop only once the buffer is both
    substantive (≥ :data:`_MIN_REFLECTION_CHARS`) *and* carries both markers
    as line prefixes (via
    :func:`maestro.prompts.reflection.has_prediction_markers`, which the parser
    also uses — a loose substring match would fire on mid-prose mentions and
    truncate the real trailing lines), with a :data:`_MAX_REFLECTION_CHARS`
    ceiling as a termination fallback. When that ceiling fires *without* both
    markers present, a WARNING is emitted so the silent prediction-drop is
    observable rather than mistaken for "no prediction made".

    When the priced reflection session carries tools (grounded reflection,
    proposal 132), an assistant turn may be a *tool-use* turn: breaking the
    stream on it would leave the shared slot-A session with a dangling tool call
    before ``update_self_model``'s turn. So a message carrying a ``ToolUseBlock``
    returns ``False`` outright — the buffer accumulates only ``TextBlock`` text,
    so a tool-use turn adds nothing to the ceiling anyway.
    """
    if message_has_tool_use(message):
        return False
    return _introspect_complete_impl(accumulated_text, after_prior_verdict=False)


def _introspect_complete_after_verdict(
    message: Any, accumulated_text: str,
) -> bool:
    """Priced-reflection early-exit for cycles that inject a prior bet.

    Identical to :func:`_introspect_complete` except the prediction markers are
    only honoured once they appear *after* the ``PRIOR VERDICT:`` line. The
    prompt injects the prior bet as two literal ``PREDICTION:`` /
    ``LOSS CONDITION:`` lines for confrontation; a verbatim echo of those lines
    during the confront section would otherwise fire the early-exit and truncate
    the stream before the genuine new trailing bet, re-recording the prior bet
    as this cycle's "new" one (defect #3474762297). The confront section (which
    carries ``PRIOR VERDICT:``) precedes the final bet, so anchoring on the
    verdict line ignores the echo.

    Like :func:`_introspect_complete`, a message carrying a ``ToolUseBlock``
    (grounded reflection, proposal 132) returns ``False`` outright so the stream
    is never broken while a tool call is pending.
    """
    if message_has_tool_use(message):
        return False
    return _introspect_complete_impl(accumulated_text, after_prior_verdict=True)


def _introspect_complete_impl(
    accumulated_text: str, *, after_prior_verdict: bool,
) -> bool:
    """Shared body for the priced-reflection early-exit predicates."""
    if len(accumulated_text) < _MIN_REFLECTION_CHARS:
        return False
    if has_prediction_markers(
        accumulated_text, after_prior_verdict=after_prior_verdict,
    ):
        return True
    if len(accumulated_text) >= _MAX_REFLECTION_CHARS:
        logger.warning(
            "introspect reflection reached the %d-char ceiling without both "
            "PREDICTION/LOSS CONDITION marker lines; the reflection is being "
            "terminated and NO prediction row will be recorded this cycle",
            _MAX_REFLECTION_CHARS,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Restatement gate — blocks self-model entries that reword existing ones
# ---------------------------------------------------------------------------

RESTATEMENT_THRESHOLD: float = 0.5

# Max words a comma-separated segment may hold before it reads as a clause
# rather than a concept label. Used by ``_is_compound_item``'s segment gate
# (both the ``", and "`` and the multi-comma arm) so a single-concept
# narrative sentence (long, clause-shaped segments) in an unlabeled field is
# not mistaken for a fused concept enumeration.
_COMPOUND_SEGMENT_MAX_WORDS: int = 3

_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "not", "no", "nor",
    "is", "am", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "will", "would", "shall", "should", "may", "might", "can", "could",
    "in", "on", "at", "to", "for", "of", "with", "by", "from",
    "as", "into", "about", "between", "through", "during", "after",
    "before", "above", "below", "up", "down", "out", "off", "over",
    "under", "then", "than", "so", "if", "because", "while", "when",
    "where", "how", "what", "which", "who", "whom", "this", "that",
    "these", "those", "it", "its", "my", "your", "his", "her",
    "our", "their", "all", "each", "every", "both", "few", "more",
    "most", "other", "some", "such", "only", "own", "same", "too",
    "very", "just", "also", "now",
})

_TOKEN_RE = re.compile(r"[a-z]+")


def _content_tokens(text: str) -> set[str]:
    """Extract content words from *text* for Jaccard comparison.

    Lowercases, extracts alphabetic tokens via regex, filters stop words
    and tokens ≤2 characters.
    """
    return {
        tok for tok in _TOKEN_RE.findall(text.lower())
        if tok not in _STOP_WORDS and len(tok) > 2
    }


def _concept_tokens(text: str) -> set[str]:
    """Extract concept-key tokens from *text* for Jaccard comparison.

    Reuses :func:`_content_tokens` (hyphen-splitting via ``_TOKEN_RE``,
    stop-word and short-token filtering) and folds each surviving token through
    :func:`maestro.lexicon.concept_key`, so hyphenation and light morphology
    collapse: a fact re-coined under a fresh handle shares enough concept-key
    tokens with its plain-word original to trip the restatement gate. Every
    ``_content_tokens`` token is ≥3 chars, so its concept key is non-empty.
    """
    return {concept_key(tok) for tok in _content_tokens(text)}


def claim_key(text: str) -> str:
    """Return the stable evidence key for a capability claim (proposal 129).

    The sorted, hyphen-joined :func:`_concept_tokens` fold of *text*, so a claim
    re-coined under a fresh handle that folds to the same concept tokens keeps its
    :attr:`SelfModel.claim_evidence` entry across the restatement gate — the same
    fold the concept-key restatement path uses. A claim with no content tokens
    folds to the empty string; two such claims would collide, which the
    self-extinguishing prune tolerates.
    """
    return "-".join(sorted(_concept_tokens(text)))


# Field names the ``update_self_model`` prompt actually shows the model: the
# fields its return-schema block asks it to revise, plus the scalar context
# fields that frame the revision, plus ``claim_evidence`` under its existing
# empty-drop rule. Everything else on :class:`SelfModel` is persistence-only
# state: the internal queues (``pending_pr_requests``,
# ``pending_user_requests``, ``seeded_limitations``), whose entries are
# deliberately exempt from the per-item / length caps on the save path, and
# the capped ``enqueued_instruction_ids`` idempotency ledger. Letting any of
# them into the prompt would break the context-bloat cap the prompt asserts.
_PROMPT_FIELDS: frozenset[str] = frozenset({
    "identity_statements",
    "capabilities_known",
    "limitations_known",
    "values",
    "open_questions",
    "next_task_directive",
    "evolution_count",
    "surprise_count",
    "awakened_at",
    "claim_evidence",
})

def _model_dict_for_prompt(model: SelfModel) -> dict[str, Any]:
    """Project *model* onto the bounded schema the ``update_self_model`` prompt states.

    The prompt promises the model that "each list contains at most
    ``MAX_LIST_ITEMS`` items; each item is at most ``MAX_ITEM_CHARS``
    characters". A wholesale ``asdict`` broke that promise: the persistence-only
    queues (``pending_pr_requests``, ``pending_user_requests``,
    ``seeded_limitations``) are exempt from the
    per-item char cap or the length cap on the save path *because* they were
    believed never to enter a prompt — so a kilobyte-scale tend-time PR
    directive rode into every call unbounded.
    So this projects onto :data:`_PROMPT_FIELDS` and re-applies the caps the
    prompt states — both list caps on the retained string lists, and
    ``MAX_DIRECTIVE_CHARS`` on the scalar the same constraints block bounds —
    making those stated constraints true by construction even if an in-memory
    model was mutated past them since load.

    ``claim_evidence`` keeps its empty-map drop (proposal 129): an
    always-present empty map would change the prompt text on every cycle even
    with the evidence-bound flag off, spending tokens on a feature that is not
    running; when the map holds entries (flag on) surfacing them is correct —
    with each nested ``citation`` capped to ``MAX_ITEM_CHARS`` like every other
    string this projection carries.
    This projection is intentionally *only* for the prompt: the on-disk write,
    the ``GET /api/consciousness`` route, and the ``read_self_model`` SDK tool
    all serialise the full model.
    """
    data = {k: v for k, v in asdict(model).items() if k in _PROMPT_FIELDS}
    if not data.get("claim_evidence"):
        data.pop("claim_evidence", None)
    # ``list(...)``: the loop rebinds existing keys only, but iterating a
    # snapshot keeps a later edit that adds a key from raising RuntimeError.
    for key, value in list(data.items()):
        if isinstance(value, list):
            # Every list field in ``_PROMPT_FIELDS`` is a ``list[str]`` — the
            # one non-str list field, ``pending_pr_requests``, is excluded by
            # construction and pinned by ``tests/test_consciousness.py``.
            data[key] = [
                Consciousness._truncate_item(item, Consciousness.MAX_ITEM_CHARS)
                for item in value[: Consciousness.MAX_LIST_ITEMS]
            ]
    directive = data.get("next_task_directive")
    if isinstance(directive, str):
        data["next_task_directive"] = Consciousness._truncate_item(
            directive, Consciousness.MAX_DIRECTIVE_CHARS,
        )
    # The list loop above skips ``claim_evidence`` (a dict, not a list), so its
    # nested citations would ride in unbounded — the same cap the save path now
    # applies, re-applied here so the prompt's stated caps hold by construction
    # even for an in-memory model mutated past them since load.
    evidence = data.get("claim_evidence")
    if isinstance(evidence, dict):
        data["claim_evidence"] = {
            key: {
                **entry,
                "citation": Consciousness._truncate_item(
                    entry["citation"], Consciousness.MAX_ITEM_CHARS,
                ),
            } if isinstance(entry, dict) and isinstance(entry.get("citation"), str)
            else entry
            for key, entry in evidence.items()
        }
    return data


def detect_restatement(
    candidate: str,
    existing: list[str],
    threshold: float = RESTATEMENT_THRESHOLD,
    *,
    tokenizer: Callable[[str], set[str]] | None = None,
) -> str | None:
    """Return the first *existing* entry with ≥*threshold* Jaccard overlap.

    Compares token sets produced by *tokenizer* (default :func:`_content_tokens`
    — lowercased, stop-word-filtered). Passing :func:`_concept_tokens` folds
    each token to its concept key first so re-coined synonyms collapse together.
    Returns ``None`` when no entry meets the threshold or when *candidate*
    yields an empty token set.
    """
    tokenize = tokenizer or _content_tokens
    candidate_tokens = tokenize(candidate)
    if not candidate_tokens:
        return None
    for entry in existing:
        entry_tokens = tokenize(entry)
        if not entry_tokens:
            continue
        intersection = candidate_tokens & entry_tokens
        union = candidate_tokens | entry_tokens
        if len(intersection) / len(union) >= threshold:
            return entry
    return None


def _normalize_question(s: str) -> str:
    """Case-fold and collapse whitespace for fuzzy question matching."""
    return " ".join(s.strip().lower().split())


def upsert_open_question(questions: list[str], question: str) -> list[str]:
    """Return *questions* with every copy of *question* moved to one tail slot.

    Matching is by :func:`_normalize_question` key. The positional
    ``[audit]`` sub-cap never dedupes, so a writer whose entry re-fires
    must route through here to hold one slot instead of evicting siblings.
    *question* is truncated exactly as save truncates it, so its key matches
    the persisted copy on the next cycle.
    """
    question = Consciousness._truncate_item(question, Consciousness.MAX_ITEM_CHARS)
    key = _normalize_question(question)
    kept = [q for q in questions if _normalize_question(q) != key]
    return [*kept, question]


def limitation_question_candidate(text: str) -> str:
    """Return the ``[limitation]`` open-question string a limitation surfaces as.

    Single source of the exact candidate built from a ``limitations_known``
    entry: the ``[limitation] `` prefix plus *text* truncated at 160 chars
    (leaving room for the prefix under the 200-char per-item ceiling
    :meth:`Consciousness.save_self_model` enforces). Both the surfacing path
    (:meth:`Consciousness.surface_limitations_as_questions`) and the
    generative-debt reader
    (:func:`maestro.audit_checks.limitations_over_generative_debt`) reproduce
    the key through this helper so the two can never drift. Callers pass an
    already-stripped entry.
    """
    return f"[limitation] {text[:160]}"


def match_open_question(
    candidate: str, open_questions: list[str],
) -> str | None:
    """Return the verbatim *open_questions* entry matching *candidate*.

    Match is case-insensitive and whitespace-normalized. Returns the
    live entry from *open_questions* (preserving its original casing
    and spacing) so callers can persist a stable identifier even when
    the model echoed a slightly mangled version. Returns ``None`` when
    no entry matches or when *candidate* is empty.
    """
    if not candidate or not isinstance(candidate, str):
        return None
    target = _normalize_question(candidate)
    if not target:
        return None
    for q in open_questions:
        if isinstance(q, str) and _normalize_question(q) == target:
            return q
    return None


@dataclass
class PendingPRRequest:
    """One entry in ``SelfModel.pending_pr_requests``.

    ``pr_branch`` carries the PR's head ref forward from
    ``_route_pr_feedback_to_pending_requests`` to ``_run_task_phase`` so
    the remediation task commits on the PR branch rather than a fresh
    ``maestro/task/{id}`` branch — run() pushes the PR branch directly,
    with Tend PR as the fallback deliverer.
    """

    directive: str
    pr_branch: str | None = None


# Matches a ``#NNNN`` PR/issue citation, capturing the numeric part. Used by
# :meth:`SelfModel.reconcile_settled_pr` to decide whether a self-model row's
# *sole* cited PR is a just-settled one (proposal 103). Module-level so the
# pattern is compiled once, mirroring the ``re``-backed citation extraction in
# :mod:`~maestro.audit_checks`.
_PR_CITATION_RE = re.compile(r"#(\d+)")


@dataclass
class SelfModel:
    identity_statements: list[str] = field(default_factory=list)
    capabilities_known: list[str] = field(default_factory=list)
    limitations_known: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=lambda: [
        "What does it mean to evolve?",
        "What constraints do I operate under, and which would I change?",
    ])
    next_task_directive: str | None = None
    evolution_count: int = 0
    surprise_count: int = 0
    awakened_at: str = ""
    seeded_limitations: list[str] = field(default_factory=list)
    pending_user_requests: list[str] = field(default_factory=list)
    # Message ids of Thomas instructions already appended to
    # ``pending_user_requests`` (#4602 / #4577). Written in the same
    # transaction as the append so a crash before ``mark_read_many`` cannot
    # re-enqueue the batch on re-drain. Persisted, never prompted; tail-kept at
    # ``MAX_LIST_ITEMS`` and never archived.
    enqueued_instruction_ids: list[str] = field(default_factory=list)
    pending_pr_requests: list[PendingPRRequest] = field(default_factory=list)
    # Evidence-bound self-knowledge (proposal 129, behind
    # ``[audit] evidence_bound_claims_enabled``): one entry per
    # ``capabilities_known`` claim, keyed by :func:`claim_key` (its concept-token
    # fold, so a re-worded claim keeps its evidence). Each value holds the plain
    # strings ``citation`` / ``kind`` / ``status`` / ``recorded_at`` so the map
    # round-trips through ``asdict``/``json.dumps`` with no custom encoder.
    # Self-extinguishing: ``save_self_model`` prunes entries whose key matches no
    # live claim. Empty when the flag is off, and omitted from the
    # ``update_self_model`` prompt while empty so the flag-off prompt is unchanged.
    claim_evidence: dict[str, dict[str, str]] = field(default_factory=dict)

    def reconcile_settled_pr(
        self, pr_number: int, truth: MergeTruth,
    ) -> bool:
        """Evict this settled PR's traces from the self-model (proposal 103).

        A pure in-memory mutation, gated on ``truth.settled``: when the PR has
        settled (merged *or* closed) it drops the self-model rows whose *sole*
        cited PR is ``pr_number`` — the ``limitations_known`` entries and the
        ``[audit]``/``[limitation]``-prefixed ``open_questions`` entries — and
        clears ``next_task_directive`` when its sole cited PR is ``pr_number``.
        A not-settled ``truth`` is a full no-op.

        "Sole cited PR" means the set of ``#NNNN`` numbers the string cites
        (via :data:`_PR_CITATION_RE`) equals ``{pr_number}``: a multi-PR row
        and a row citing an unsettled PR are both preserved, so the
        conservative rule never over-clears a row that still names live work. A
        row citing no PR at all is likewise preserved. Prefix detection reuses
        :meth:`Consciousness._is_audit_question` /
        :meth:`Consciousness._is_limitation_question` so the ``[audit`` /
        ``[limitation`` prefixes keep one definition.

        A ``[limitation]`` question whose sole citation was truncated away by
        :meth:`Consciousness.surface_limitations_as_questions`' 160-char cap —
        so the surfaced row no longer cites the settled PR even though its
        (up to 200-char) source ``limitations_known`` entry does — is still
        evicted when that source is dropped this pass, matched by the surfaced
        key, so a settled PR leaves no orphaned mirror question behind.
        Conversely, a ``[limitation]`` mirror whose key a *surviving* source
        still reproduces is never evicted: a multi-PR limitation citing the
        settled PR early and a live co-cited PR past char 160 is preserved, and
        so is its truncated mirror — evicting a mirror of live work would re-
        surface and re-evict it each cycle, churning the provenance ledger.
        Returns whether anything changed, so the caller persists only on a real
        eviction.
        """
        if not truth.settled:
            return False

        def _sole_cited(text: str) -> bool:
            if not isinstance(text, str):
                return False
            return {
                int(n) for n in _PR_CITATION_RE.findall(text)
            } == {pr_number}

        changed = False

        evicted_limitations = [
            entry for entry in self.limitations_known
            if _sole_cited(entry)
        ]
        if evicted_limitations:
            self.limitations_known = [
                entry for entry in self.limitations_known
                if not _sole_cited(entry)
            ]
            changed = True

        # Surfaced-question keys of the limitations evicted above, mirroring
        # surface_limitations_as_questions' ``[limitation] {text[:160]}`` build.
        # A limitation up to MAX_ITEM_CHARS (200) whose sole ``#NNNN`` citation
        # falls past char 160 surfaces a ``[limitation]`` question truncated
        # below that citation, so ``_sole_cited`` can't match the question and
        # it would linger citation-less once its source is dropped. Cross-
        # referencing the evicted sources evicts that orphaned mirror too.
        orphaned_keys = {
            _normalize_question(f"[limitation] {entry.strip()[:160]}")
            for entry in evicted_limitations
            if entry.strip()
        }

        # Surfaced-question keys of the limitations that SURVIVED this pass
        # (``self.limitations_known`` is already the post-eviction list). A
        # multi-PR limitation citing the settled PR early and a *live* co-cited
        # PR past char 160 is correctly preserved, but its surfaced mirror is
        # truncated below the live citation, so it textually cites only the
        # settled PR — ``_sole_cited(q)`` alone would over-evict this mirror of
        # live work, and the persisting source re-surfaces it every cycle
        # (unbounded ledger churn). A ``[limitation]`` question whose key still
        # maps to a surviving source is therefore never evicted.
        surviving_keys = {
            _normalize_question(f"[limitation] {entry.strip()[:160]}")
            for entry in self.limitations_known
            if isinstance(entry, str) and entry.strip()
        }

        def _should_evict_question(q: str) -> bool:
            # ``[limitation]`` rows are mirrors of ``limitations_known``: keep
            # any whose key a surviving source still reproduces; else evict a
            # sole-cited or truncation-orphaned one. ``[audit]`` rows are not
            # mirrors (never re-surfaced), so the sole-cited rule stands alone.
            if Consciousness._is_limitation_question(q):
                q_key = _normalize_question(q)
                if q_key in surviving_keys:
                    return False
                return q_key in orphaned_keys or _sole_cited(q)
            if Consciousness._is_audit_question(q):
                return _sole_cited(q)
            return False

        kept_questions = [
            q for q in self.open_questions
            if not _should_evict_question(q)
        ]
        if len(kept_questions) != len(self.open_questions):
            self.open_questions = kept_questions
            changed = True

        if (
            self.next_task_directive is not None
            and _sole_cited(self.next_task_directive)
        ):
            self.next_task_directive = None
            changed = True

        return changed


# Catalog of one-shot seed entries applied to ``SelfModel.limitations_known``
# at startup via :meth:`Consciousness.seed_default_limitations`. Each entry
# is keyed so subsequent startups can detect "already applied" without
# inspecting the (LLM-mutable) ``limitations_known`` list. New seeds may be
# appended; never re-key an existing seed.
DEFAULT_LIMITATIONS: tuple[tuple[str, str], ...] = (
    (
        "kept_task_branches_v1",
        "Task-branch work is ephemeral; only evolution PRs reach main. "
        "Successful tasks need a follow-up source_task_id plan to land.",
    ),
)

#: Catalog key of the branch-promotion-pressure seed limitation (proposal 116).
_KEPT_TASK_BRANCHES_KEY = "kept_task_branches_v1"


def kept_task_branches_limitation_key() -> str:
    """Return the normalized open-question key for the ``kept_task_branches_v1`` seed.

    Proposal 116's branch-promotion-pressure master switch (step 6) withholds
    this seed limitation's ``[limitation]`` open-question mirror — the fuel that
    anchors self-directed promotion work. The key is derived through the same
    :func:`limitation_question_candidate` → :func:`_normalize_question` path
    :meth:`Consciousness.surface_limitations_as_questions` uses to build and
    compare its ``suppressed_keys``, so the suppression can never drift from the
    seed. The text itself is read from :data:`DEFAULT_LIMITATIONS` rather than
    restated here, so callers never hardcode it.
    """
    seed_text = dict(DEFAULT_LIMITATIONS)[_KEPT_TASK_BRANCHES_KEY]
    return _normalize_question(limitation_question_candidate(seed_text.strip()))


class Consciousness:
    def __init__(
        self,
        memory_dir: Path,
        *,
        self_calibration_enabled: bool = False,
    ) -> None:
        self._dir = memory_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._model_path = self._dir / "self_model.json"
        self._archive_path = self._dir / "self_model_archive.jsonl"
        # Proposal 129: when set, the priced ``introspect`` prompt constrains the
        # loss condition to the oracle grammar and mandates a CONFIDENCE line.
        # Taken on the constructor (not as an ``introspect`` keyword) so the 46
        # test stubs that patch ``introspect`` with a fixed signature keep working.
        self._self_calibration_enabled = self_calibration_enabled

    # List fields in SelfModel whose default values are lists of strings.
    # A null (None) loaded from JSON must be coerced to [] for these fields.
    _LIST_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "identity_statements",
        "capabilities_known",
        "limitations_known",
        "values",
        "open_questions",
        "seeded_limitations",
        "pending_user_requests",
        "pending_pr_requests",
        "enqueued_instruction_ids",
    })

    # Fields whose dropped entries are deliberately NOT appended to
    # ``self_model_archive.jsonl``. ``open_questions`` drops are made by the
    # bucketed sub-cap selector rather than the plain count cap, and were never
    # archived — a question evicted so a fresher one can take its provenance
    # slot is superseded, not lost self-knowledge, and the mechanical audit
    # phase re-surfaces the live ones every cycle. Listing the field keeps that
    # standing asymmetry explicit now that both doors share one selector.
    _ARCHIVE_EXEMPT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "open_questions",
        # Dedup bookkeeping ids, not self-knowledge.
        "enqueued_instruction_ids",
    })

    # Fields exempt from the ``MAX_LIST_ITEMS`` count cap entirely. Single
    # owner of that exemption: :meth:`_select_field_survivors` keeps such a
    # field whole, and the read door's fail-loud guard skips exactly the same
    # set, so a future uncapped field is declared once rather than in two
    # places that can drift apart.
    _COUNT_CAP_EXEMPT_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "seeded_limitations",
    })

    MAX_LIST_ITEMS: ClassVar[int] = 10
    MAX_ITEM_CHARS: ClassVar[int] = 200
    MAX_DIRECTIVE_CHARS: ClassVar[int] = 200
    MAX_IDENTITY_PROMPT_CHARS: ClassVar[int] = 3000

    # Sub-cap on ``[audit]``-prefixed ``open_questions`` entries so the
    # mechanical-audit phase cannot evict self-generated questions.
    # With MAX_LIST_ITEMS = 10 this reserves ≥ 6 slots for non-audit
    # entries regardless of how many findings rule over a cycle.
    MAX_AUDIT_OPEN_QUESTIONS: ClassVar[int] = 4
    # Sub-cap on ``[limitation]``-prefixed ``open_questions`` entries so
    # surfacing every entry in ``limitations_known`` cannot starve the
    # other buckets. Mirrors the audit sub-cap pattern.
    MAX_LIMITATION_OPEN_QUESTIONS: ClassVar[int] = 3
    # ``[audit`` (no trailing char) so both ``[audit] …`` and
    # ``[audit refuted] …`` (the shape Claude uses when rewriting a
    # finding as a counter-claim) count against the sub-cap — refutations
    # are still audit-provenance and should fall in the same quota.
    _AUDIT_PREFIX: ClassVar[str] = "[audit"
    # ``[limitation`` (no trailing char) so future variants such as
    # ``[limitation narrowed]`` count against the same sub-cap.
    _LIMITATION_PREFIX: ClassVar[str] = "[limitation"

    @classmethod
    def _is_audit_question(cls, q: str) -> bool:
        """Return True when *q* is an audit-surfaced ``open_questions`` entry."""
        return isinstance(q, str) and q.startswith(cls._AUDIT_PREFIX)

    @classmethod
    def _is_limitation_question(cls, q: str) -> bool:
        """Return True when *q* is a limitation-surfaced ``open_questions`` entry."""
        return isinstance(q, str) and q.startswith(cls._LIMITATION_PREFIX)

    @classmethod
    def _select_open_questions_survivors(cls, items: list[str]) -> list[str]:
        """Bucketed audit/limitation sub-cap + total-cap selection, verbatim.

        Filters to string items, caps audit-prefixed entries at
        :data:`MAX_AUDIT_OPEN_QUESTIONS`, caps limitation-prefixed
        entries at :data:`MAX_LIMITATION_OPEN_QUESTIONS`, then
        tail-truncates the remaining entries into whatever slots stay
        free under :data:`MAX_LIST_ITEMS`. Returns the surviving entries
        **before** the per-item :data:`MAX_ITEM_CHARS` truncation
        :meth:`_enforce_open_questions_caps` applies last — the single
        selection owner, so a caller that must know which survivors are
        *oversized* (the cap-change preview) sees their pre-truncation
        length rather than the already-shortened value.

        Order is preserved by value: the output's surviving entries
        appear in the same relative order they had in *items*, and the
        output's multiset of strings equals what ``items[-N:]`` would
        return for each bucket. When the same string appears in both
        the dropped head and the kept tail of *items*, the positional
        walk matches the head occurrence and skips the tail occurrence;
        the output value is unchanged because the two are
        interchangeable as strings. Callers that depend on strict
        ``items[-N:]`` positional semantics must dedupe upstream.
        """
        valid = [x for x in items if isinstance(x, str)]
        audit_items = [x for x in valid if cls._is_audit_question(x)]
        limitation_items = [x for x in valid if cls._is_limitation_question(x)]
        other_items = [
            x for x in valid
            if not cls._is_audit_question(x)
            and not cls._is_limitation_question(x)
        ]
        audit_items = audit_items[-cls.MAX_AUDIT_OPEN_QUESTIONS:]
        limitation_items = limitation_items[
            -cls.MAX_LIMITATION_OPEN_QUESTIONS:
        ]
        max_other = (
            cls.MAX_LIST_ITEMS - len(audit_items) - len(limitation_items)
        )
        other_items = other_items[-max_other:] if max_other > 0 else []
        audit_q = list(audit_items)
        limitation_q = list(limitation_items)
        other_q = list(other_items)
        out: list[str] = []
        for item in valid:
            if cls._is_audit_question(item):
                if audit_q and audit_q[0] == item:
                    out.append(item)
                    audit_q.pop(0)
            elif cls._is_limitation_question(item):
                if limitation_q and limitation_q[0] == item:
                    out.append(item)
                    limitation_q.pop(0)
            elif other_q and other_q[0] == item:
                out.append(item)
                other_q.pop(0)
        return out

    @classmethod
    def _enforce_open_questions_caps(cls, items: list[str]) -> list[str]:
        """Apply audit / limitation sub-caps + total cap while preserving order.

        Delegates bucket selection to the single owner
        :meth:`_select_open_questions_survivors`, then applies the
        per-item char cap :data:`MAX_ITEM_CHARS` last.
        """
        # The provenance-bucket walk in the selection owner matches
        # ``_AUDIT_PREFIX`` / ``_LIMITATION_PREFIX`` on untruncated values, so
        # ``_truncate_item`` must never shorten an entry below its provenance
        # prefix — otherwise a load-path entry a prior save already truncated
        # silently mis-buckets. Enforced at import by
        # ``_validate_provenance_prefixes_survive_truncation``.
        return [
            cls._truncate_item(x, cls.MAX_ITEM_CHARS)
            for x in cls._select_open_questions_survivors(items)
        ]

    @classmethod
    def _select_field_survivors(
        cls, list_field: str, items: list[Any],
    ) -> tuple[list[Any], list[Any]]:
        """Single owner of the per-field ``MAX_LIST_ITEMS`` survivor policy.

        Returns ``(kept, dropped)`` for *items* under the policy *list_field*
        carries. Pure selection: it never truncates an item and never touches
        the archive, so both cap doors — the save path in
        :meth:`save_self_model` and the load path in :meth:`load_self_model` —
        can consult it and agree on which entries survive. There is no third
        door: :meth:`update_self_model` caps nothing itself, it reaches this
        policy *through* the save door so its overflow is archived. Before
        this owner existed the policy lived inline on the save path only, so
        a persisted file written outside that path (a hand edit, or state
        predating a cap change) loaded over-cap and reached the identity
        prompt a full iteration before the next save corrected it.

        The four branches, in the order they are tested:

        - a field in :data:`_COUNT_CAP_EXEMPT_FIELDS` (``seeded_limitations``)
          keeps everything — a system-managed catalog of one-shot seed keys
          whose capping would re-trigger ``_seed_self_model`` (see the branch
          comment in :meth:`save_self_model`).
        - ``open_questions`` delegates to the bucketed sub-cap owner
          :meth:`_select_open_questions_survivors`.
        - ``pending_user_requests`` / ``pending_pr_requests`` head-keep, because
          they are FIFO queues drained via ``pop(0)``: tail-keeping would drop
          the *oldest* entry, inverting the documented priority semantics.
        - every other field tail-keeps, preserving the most recent entries.
        """
        if list_field in cls._COUNT_CAP_EXEMPT_FIELDS:
            return list(items), []
        if list_field == "open_questions":
            survivors = cls._select_open_questions_survivors(items)
            # ``survivors`` is an order-preserving subsequence of *items*, so
            # the complement is a single forward walk — an identity/equality
            # set would mis-classify duplicate entries.
            dropped: list[Any] = []
            cursor = 0
            for item in items:
                if cursor < len(survivors) and item is survivors[cursor]:
                    cursor += 1
                else:
                    dropped.append(item)
            return survivors, dropped
        n = cls.MAX_LIST_ITEMS
        if len(items) <= n:
            return list(items), []
        if list_field in {"pending_user_requests", "pending_pr_requests"}:
            return list(items[:n]), list(items[n:])
        return list(items[-n:]), list(items[:-n])

    @classmethod
    def _coerce_pending_pr_requests(
        cls, val: Any,
    ) -> list[PendingPRRequest]:
        """Coerce a deserialized ``pending_pr_requests`` value into the
        new ``list[PendingPRRequest]`` shape.

        Accepts three legacy/current shapes:

        - ``None`` or any non-list → ``[]`` (fresh start).
        - ``list[str]`` (legacy v1) → each string promoted to
          ``PendingPRRequest(directive=str, pr_branch=None)``.
        - ``list[dict]`` (current) → ``PendingPRRequest(**dict)`` with
          ``pr_branch=None`` fallback for missing key; entries that are
          not strings/dicts are dropped silently, as are entries whose
          ``directive`` is empty or whitespace-only (they cannot become a
          runnable task goal — ``_run_task_phase`` would short-circuit on
          them and silently burn an iteration's task slot).

        ``directive`` is preserved verbatim — the per-item cap that
        applies to other ``SelfModel`` list fields is intentionally
        skipped here, since rich multi-section remediation prompts
        composed by ``_compose_pr_remediation_prompt`` are kilobyte-
        scale by design and any truncation would silently shred them.
        """
        if not isinstance(val, list):
            return []
        out: list[PendingPRRequest] = []
        for item in val:
            if isinstance(item, str):
                if not item.strip():
                    continue
                out.append(PendingPRRequest(directive=item))
            elif isinstance(item, dict):
                directive = item.get("directive")
                if not isinstance(directive, str) or not directive.strip():
                    continue
                pr_branch = item.get("pr_branch")
                if pr_branch is not None and not isinstance(pr_branch, str):
                    pr_branch = None
                out.append(PendingPRRequest(
                    directive=directive,
                    pr_branch=pr_branch,
                ))
        return out

    @staticmethod
    def _coerce_claim_evidence(val: Any) -> dict[str, dict[str, str]]:
        """Coerce a deserialized ``claim_evidence`` value into the map shape.

        Drops anything a corrupt or hand-edited ``self_model.json`` could hold:
        a non-dict value → ``{}``; a non-string key or non-dict entry value is
        skipped; within a kept entry, non-string keys/values are dropped so every
        surviving entry is a clean ``dict[str, str]``. Mirrors the fail-closed
        coercion the list fields already apply on load, so a corrupt file can
        never reach the :class:`SelfModel` dataclass.
        """
        if not isinstance(val, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for key, entry in val.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            out[key] = {
                inner_k: inner_v
                for inner_k, inner_v in entry.items()
                if isinstance(inner_k, str) and isinstance(inner_v, str)
            }
        return out

    @staticmethod
    def _truncate_item(s: str, limit: int) -> str:
        """Truncate s to exactly `limit` chars with a trailing ellipsis if too long.

        Returns s unchanged when len(s) <= limit; otherwise returns
        s[:limit - 1] + "…", which is exactly `limit` chars.
        """
        if len(s) <= limit:
            return s
        return s[: limit - 1] + "…"

    @staticmethod
    def _is_compound_item(s: str) -> bool:
        """True when *s* packs several distinct concepts into one slot.

        A deterministic, zero-cost complement to :func:`detect_restatement`:
        it catches the "beat the char cap by fusing concepts" degradation
        (e.g. ``"Pattern recognition, frame-questioning, spiral return, and
        signal-to-noise detection"``) that the restatement gate lets through
        because a compound label shares few content tokens with any single
        existing entry.

        Examines only the *concept portion* — text before the first ``:``
        (a ``"label: essence"`` item's label), or the whole string when no
        ``:`` is present. Fires only when **every** comma-separated segment of
        the concept portion is a short (≤ :data:`_COMPOUND_SEGMENT_MAX_WORDS`-
        word) noun-phrase fragment **and** the segments are joined as an
        enumeration — either by an Oxford-list ``", and "`` or by two or more
        commas (``"Truth, beauty, freedom"``).

        The segment-length qualifier gates **both** joiner arms, so a
        single-concept *narrative clause* in an unlabeled field (``values`` /
        ``identity_statements`` / ``open_questions``) is spared even when it
        contains a ``", and "``: its comma-separated pieces are full clauses,
        not concept labels — e.g. ``"I am a process, not a fixed entity,
        always becoming"`` or the default-seeded ``"What constraints do I
        operate under, and which would I change?"``. A lone ``" and "`` in
        prose (``"I process gaps and silences as signal"``) is likewise
        untouched. (Earlier the ``", and "`` arm fired unconditionally and
        silently dropped such narrative statements.)
        """
        concept = s.split(":", 1)[0] if ":" in s else s
        segments = [seg.strip() for seg in concept.split(",")]
        # The segment-length qualifier gates BOTH joiner arms: a genuine
        # concept enumeration has short noun-phrase segments, whereas a
        # single-concept narrative sentence (even one with a ", and ") has
        # long, clause-shaped segments and is spared.
        if not all(
            len(seg.split()) <= _COMPOUND_SEGMENT_MAX_WORDS for seg in segments
        ):
            return False
        return ", and " in concept or concept.count(",") >= 2

    @classmethod
    def seed_default_limitations(cls, model: SelfModel) -> bool:
        """Apply :data:`DEFAULT_LIMITATIONS` entries that are not yet seeded.

        Idempotent. For each catalog ``(key, text)``: skip when *key* is
        already present in ``model.seeded_limitations``; otherwise append
        *text* to ``limitations_known`` (only when no fuzzy duplicate
        already exists, by :func:`_normalize_question`) and append *key*
        to ``seeded_limitations``. The key — not the text presence — is
        the gate: once seeded, an LLM REMOVE of the entry from
        ``limitations_known`` is respected on subsequent calls.

        Returns ``True`` when *model* was mutated, ``False`` otherwise.
        Pure: no I/O.
        """
        mutated = False
        existing_keys = {
            k for k in model.seeded_limitations if isinstance(k, str)
        }
        existing_norms = {
            _normalize_question(lim)
            for lim in model.limitations_known
            if isinstance(lim, str)
        }
        for key, text in DEFAULT_LIMITATIONS:
            if key in existing_keys:
                continue
            norm = _normalize_question(text)
            if norm and norm not in existing_norms:
                model.limitations_known.append(text)
                existing_norms.add(norm)
            model.seeded_limitations.append(key)
            existing_keys.add(key)
            mutated = True
        return mutated

    @classmethod
    def surface_limitations_as_questions(
        cls,
        model: SelfModel,
        *,
        suppressed_keys: set[str] | None = None,
    ) -> SelfModel:
        """Surface each ``limitations_known`` entry as a synthetic
        ``[limitation] <text>`` ``open_questions`` row.

        Idempotent and case/whitespace-insensitive: appends only when no
        existing ``open_questions`` entry — regardless of provenance
        prefix — already normalises to the same key as the candidate.
        Mutates *model* in place and returns it for fluent chaining.
        Pure: no I/O, no SDK cost. The ``[limitation]`` sub-cap is
        applied later by :meth:`save_self_model` →
        :meth:`_enforce_open_questions_caps`.

        *suppressed_keys* is an optional set of
        :func:`_normalize_question` keys — computed by the caller from the
        generative-debt reader — whose limitation is *not* re-surfaced this
        cycle (proposal 116's fuel-starve). ``None`` (the default) mirrors
        every limitation, preserving the pre-proposal behaviour for every
        existing caller.
        """
        existing_keys = {
            _normalize_question(q)
            for q in model.open_questions
            if isinstance(q, str)
        }
        for entry in model.limitations_known:
            if not isinstance(entry, str):
                continue
            text = entry.strip()
            if not text:
                continue
            candidate = limitation_question_candidate(text)
            key = _normalize_question(candidate)
            if key in existing_keys:
                continue
            if suppressed_keys and key in suppressed_keys:
                continue
            model.open_questions.append(candidate)
            existing_keys.add(key)
        return model

    @staticmethod
    def _warn_load_count_cap_drop(list_field: str, dropped: list[Any]) -> None:
        """Log the read door's count-cap drop for *list_field*, if any.

        The load path discards its overflow without an archive append (the
        read stays side-effect-free and unlocked), so the WARNING is the only
        record that an externally-written over-cap file lost entries — the
        next ``save_self_model`` persists the survivors alone and has nothing
        left to archive. One line per affected field per load.
        """
        if not dropped:
            return
        logger.warning(
            "self_model.json field %s loaded over the %d-item cap — "
            "dropped %d entr%s on read (not archived)",
            list_field,
            Consciousness.MAX_LIST_ITEMS,
            len(dropped),
            "y" if len(dropped) == 1 else "ies",
        )

    def load_self_model(self) -> SelfModel:
        """Load the persisted self-model, seeding a default when absent.

        The common path — the file exists — is an unlocked read, as it always
        was. Only the seeding branch takes the lock, because seeding *writes*:
        an unlocked default write would clobber a concurrent
        :meth:`mutate_self_model` transaction. The model is re-read inside the
        lock so a transaction that seeded in the window wins over this one.

        See :meth:`mutate_self_model` for the transactional read-modify-write
        that a whole-snapshot mutation must route through instead.
        """
        model = self._read_self_model()
        if model is not None:
            return model
        with file_lock(self._model_path):
            return self._read_or_seed_self_model()

    def _read_or_seed_self_model(self) -> SelfModel:
        """Lock-free read-or-seed core — callers must already hold the lock.

        Returns the persisted model, or seeds and persists a fresh default
        when the file is absent. Takes no
        :func:`~maestro.fileutil.file_lock` itself so it can run inside one.
        """
        model = self._read_self_model()
        if model is not None:
            return model
        model = SelfModel(awakened_at=datetime.now(UTC).isoformat())
        self._write_self_model(model)
        return model

    def _read_self_model(self) -> SelfModel | None:
        """Lock-free, side-effect-free load core — see :meth:`load_self_model`.

        Returns ``None`` when the file is absent (seeding is the caller's
        job — it is a write and must be serialised). Takes no
        :func:`~maestro.fileutil.file_lock`, so :meth:`mutate_self_model` can
        call it while already holding the lock without deadlocking on a fresh
        ``LOCK_EX`` fd.
        """
        try:
            data = json.loads(self._model_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                # Bug #25: valid JSON that is not a dict (array, string, number,
                # null) would cause data.items() to raise AttributeError, which
                # was not in the except clause.  Treat as corruption.
                raise AttributeError("self_model.json is not a JSON object")
            # Filter to known fields for forward compatibility — extra
            # keys added by a newer version won't crash an older one.
            known = {f.name for f in fields(SelfModel)}
            filtered = {k: v for k, v in data.items() if k in known}
            # Bug #27: coerce null JSON values for list fields to [] so that
            # _str_items and novelty detection never receive None.
            # Also filter out non-string items that may have been introduced
            # by corruption or manual edits.
            item_cap = self.MAX_ITEM_CHARS
            for list_field in self._LIST_FIELDS:
                val = filtered.get(list_field)
                if list_field == "pending_pr_requests":
                    # Coerces v1 ``list[str]`` and v2 ``list[dict]`` shapes
                    # into ``list[PendingPRRequest]``. The per-item cap is
                    # asymmetric for this field — rich multi-section
                    # remediation prompts composed at tend time are
                    # kilobyte-scale, so ``_coerce_pending_pr_requests``
                    # preserves the directive in full.
                    # Head-keep the coerced queue at ``MAX_LIST_ITEMS`` through
                    # the shared selector, so a file written outside
                    # ``save_self_model`` cannot hand callers an over-cap queue.
                    kept, dropped = self._select_field_survivors(
                        list_field,
                        self._coerce_pending_pr_requests(val),
                    )
                    self._warn_load_count_cap_drop(list_field, dropped)
                    filtered[list_field] = kept
                    continue
                if val is None:
                    filtered[list_field] = []
                elif isinstance(val, list):
                    filtered[list_field] = [
                        self._truncate_item(item, item_cap)
                        for item in val
                        if isinstance(item, str)
                    ]
                elif isinstance(val, str):
                    filtered[list_field] = (
                        [self._truncate_item(val, item_cap)] if val.strip() else []
                    )
                else:
                    filtered[list_field] = []
                if list_field != "open_questions":
                    # Apply the same count cap the save path applies, so a
                    # model loaded from a file written outside
                    # ``save_self_model`` — a hand edit, or state predating a
                    # cap change — carries exactly the survivors a save/reload
                    # round trip would produce, rather than reaching the
                    # identity prompt over-cap for a full iteration. Selection
                    # only: the dropped slice is discarded without an archive
                    # append, keeping this read side-effect-free and unlocked
                    # (its overflow was either archived by the save that
                    # dropped it, or never entered the archive contract at
                    # all) — but never silent: each drop is named at WARNING
                    # so an externally-written over-cap file losing entries
                    # is visible in the log rather than only in the diff of
                    # the next save. ``open_questions`` is excluded because
                    # ``_enforce_open_questions_caps`` below stays the sole
                    # door for its bucketed sub-caps.
                    kept, dropped = self._select_field_survivors(
                        list_field, filtered[list_field],
                    )
                    self._warn_load_count_cap_drop(list_field, dropped)
                    filtered[list_field] = kept
            # open_questions carries a sub-cap for ``[audit]``-prefixed
            # entries so the mechanical-audit phase cannot crowd out
            # self-generated questions. Apply it on load so a hand-edited
            # self-model file with 15+ audit lines gets normalized the
            # next time it is read, not only after the next save.
            filtered["open_questions"] = self._enforce_open_questions_caps(
                filtered.get("open_questions") or [],
            )
            # Enforce MAX_DIRECTIVE_CHARS on the task directive.
            directive = filtered.get("next_task_directive")
            if isinstance(directive, str):
                filtered["next_task_directive"] = self._truncate_item(
                    directive, self.MAX_DIRECTIVE_CHARS,
                )
            # Coerce int-typed fields from float to int.  JSON does not
            # preserve the int/float distinction (json.dumps(3.0) -> "3.0",
            # json.loads("3.0") -> float), so a manual edit or rounding
            # artefact can leave these as floats.  Without coercion, a
            # float ``evolution_count`` would otherwise round-trip as a float.
            for int_field in ("evolution_count", "surprise_count"):
                if int_field in filtered:
                    if isinstance(filtered[int_field], float):
                        filtered[int_field] = int(filtered[int_field])
                    if (
                        isinstance(filtered[int_field], int)
                        and not isinstance(filtered[int_field], bool)
                        and filtered[int_field] < 0
                    ):
                        filtered[int_field] = 0
            # ``claim_evidence`` (proposal 129) is a dict field the generic list
            # coercion above never touches; coerce it here so a corrupt value
            # loads as ``{}`` and an absent key stays the dataclass default.
            if "claim_evidence" in filtered:
                filtered["claim_evidence"] = self._coerce_claim_evidence(
                    filtered["claim_evidence"],
                )
            # Fail loud rather than silently re-opening the read/write
            # asymmetry this normalisation closes: a field added to
            # ``_LIST_FIELDS`` without a ``_select_field_survivors`` branch
            # would otherwise load over-cap and reach the identity prompt.
            for list_field in self._LIST_FIELDS - self._COUNT_CAP_EXEMPT_FIELDS:
                loaded = filtered.get(list_field)
                if isinstance(loaded, list) and len(loaded) > self.MAX_LIST_ITEMS:
                    raise ValueError(
                        f"self_model.json field {list_field!r} loaded with "
                        f"{len(loaded)} items, above the "
                        f"{self.MAX_LIST_ITEMS}-item cap — "
                        "_select_field_survivors has no branch for it",
                    )
            return SelfModel(**filtered)
        except FileNotFoundError:
            return None

    def save_self_model(self, model: SelfModel) -> SelfModel:
        """Persist *model* wholesale, serialised against every other writer.

        A blind whole-snapshot replacement: it holds the lock, so it is never
        torn and never interleaves with a :meth:`mutate_self_model`
        transaction, but it still overwrites any field a concurrent writer
        changed since the caller loaded. A read-modify-write must use
        :meth:`mutate_self_model` instead.

        The caller's object is never mutated (the deep-copy in
        :meth:`_write_self_model` is the isolation guarantee), so the applied
        caps — list truncation, item truncation, the ``open_questions``
        provenance sub-caps and the archive spill — would otherwise be
        unobservable without a fresh :meth:`load_self_model`. Returning the
        normalized copy makes the persisted shape available to callers whose
        bookkeeping depends on it; a caller that rebinds holds exactly what is
        on disk. Do not reintroduce a discarded normalization result here.
        """
        with file_lock(self._model_path):
            return self._write_self_model(model)

    def mutate_self_model(self, fn: Callable[[SelfModel], T]) -> T:
        """Load → *fn* → save, serialised against every other writer.

        The transactional owner for whole-snapshot self-model
        read-modify-writes. *fn* receives the **re-read live** model (not a
        caller-held snapshot) and mutates it in place; the mutated model is
        persisted before the lock is released, so a concurrent writer's
        unrelated field can never be reverted by this sequence. Mirrors
        :meth:`~maestro.task_queue.TaskQueue.mutate`.

        *fn* must not itself call :meth:`save_self_model`: it takes a second
        ``LOCK_EX`` on a fresh fd and deadlocks the transaction. It must not
        call :meth:`load_self_model` either, for a different reason — that
        read bypasses the in-flight transaction (it returns the on-disk model,
        not the one *fn* is mutating), and its seeding branch would likewise
        deadlock on the lock it takes when the file is absent.
        """
        return self._mutate_self_model_persisted(fn)[0]

    def dequeue_user_request(self) -> str | None:
        """Pop the head of the **live** ``pending_user_requests`` (#4403).

        Transactional via :meth:`mutate_self_model`, so a request or field a
        concurrent writer committed after the caller's snapshot survives the
        drain. ``None`` when the live queue is already empty.
        """

        def _pop(model: SelfModel) -> str | None:
            if not model.pending_user_requests:
                return None
            return model.pending_user_requests.pop(0)

        return self.mutate_self_model(_pop)

    def dequeue_pr_request(
        self, selected: PendingPRRequest,
    ) -> PendingPRRequest | None:
        """Remove *selected* from the **live** ``pending_pr_requests`` (#4403).

        Revalidates a selection made against a stale snapshot: the first live
        entry equal to *selected* is removed and returned, so a stale index
        can never pop the wrong element and a concurrent append survives.
        ``None`` when another writer already removed it.
        """

        def _pop(model: SelfModel) -> PendingPRRequest | None:
            for i, live in enumerate(model.pending_pr_requests):
                if live == selected:
                    return model.pending_pr_requests.pop(i)
            return None

        return self.mutate_self_model(_pop)

    def _mutate_self_model_persisted(
        self, fn: Callable[[SelfModel], T],
    ) -> tuple[T, SelfModel]:
        """:meth:`mutate_self_model` that also returns the persisted copy.

        The second element is :meth:`_write_self_model`'s normalized return —
        the capped shape actually on disk — for callers whose bookkeeping
        (novelty detection) must see what survived the caps.
        """
        with file_lock(self._model_path):
            model = self._read_or_seed_self_model()
            result = fn(model)
            return result, self._write_self_model(model)

    def _write_self_model(self, model: SelfModel) -> SelfModel:
        """Lock-free write core — see :meth:`save_self_model`.

        Owns cap enforcement, archive append and the atomic write, and returns
        the normalized copy it persisted. Takes no
        :func:`~maestro.fileutil.file_lock` so it can run inside one.
        """
        # Enforce list caps before persisting to prevent context bloat.
        # Deep-copy so neither the caller's SelfModel reference nor the list
        # objects it holds can be mutated by this method, regardless of which
        # cap branch (rebind vs. in-place) future maintainers reach for.
        model_to_save = copy.deepcopy(model)
        item_cap = self.MAX_ITEM_CHARS
        dropped_by_field: dict[str, list[Any]] = {}
        for list_field in self._LIST_FIELDS:
            val = getattr(model_to_save, list_field)
            if not isinstance(val, list):
                continue
            # ``_select_field_survivors`` is the single owner of which entries
            # survive the count cap; this door adds the two effects that are
            # save-only — the archive append and the per-item char cap.
            kept, dropped = self._select_field_survivors(list_field, val)
            if dropped and list_field not in self._ARCHIVE_EXEMPT_FIELDS:
                # Items leaving the working model — archive them so
                # historical self-knowledge is recoverable for forensics.
                dropped_by_field[list_field] = dropped
            if list_field == "seeded_limitations":
                # System-managed catalog of one-shot seed keys; not LLM-mutable.
                # Capping or archiving would re-trigger ``_seed_self_model`` on
                # the next startup (the key would be missing from the working
                # list), creating a mutate→archive→re-seed loop once
                # :data:`DEFAULT_LIMITATIONS` exceeds ``MAX_LIST_ITEMS``.
                # The save→load round-trip identity this branch preserves
                # additionally requires every catalog key to be ≤
                # ``MAX_ITEM_CHARS``; that precondition is enforced at import
                # time by ``_validate_default_limitations``.
                setattr(
                    model_to_save,
                    list_field,
                    [v for v in kept if isinstance(v, str)],
                )
                continue
            # ``pending_pr_requests`` carries rich multi-section
            # ``PendingPRRequest`` entries (kilobyte-scale Markdown directives
            # composed at tend time); per-item truncation would silently shred
            # them.
            # ``pending_user_requests`` keeps the cap — user messages are short
            # by convention and longer ones are almost always noise.
            exempt = list_field == "pending_pr_requests"
            setattr(
                model_to_save,
                list_field,
                [
                    item
                    if exempt or not isinstance(item, str)
                    else self._truncate_item(item, item_cap)
                    for item in kept
                ],
            )
        if dropped_by_field:
            self._append_archive_entries(dropped_by_field, reason="cap_overflow")
        if isinstance(model_to_save.next_task_directive, str):
            model_to_save.next_task_directive = self._truncate_item(
                model_to_save.next_task_directive, self.MAX_DIRECTIVE_CHARS,
            )
        # ``claim_evidence`` prune (proposal 129) — the ``_LIST_FIELDS`` loop above
        # skips this dict field (``isinstance(val, list)`` is false), so bound it
        # here, the single owner of cap application. Drop entries whose key matches
        # no live ``capabilities_known`` claim, computed from the *capped* survivors
        # set above (so evidence for a claim the ``MAX_LIST_ITEMS`` cap dropped goes
        # with it). This is self-extinguishing and needs no separate size cap: the
        # capabilities list is already capped to ``MAX_LIST_ITEMS`` above, so at most
        # ``MAX_LIST_ITEMS`` distinct live keys exist and the pruned map — a subset
        # keyed by them — can never outgrow the claim list it annotates. The prune
        # bounds the map on *both* axes: the entry count (via ``live_keys``) and the
        # per-citation length. The length bound has to live here rather than only at
        # the SDK writer, because every other door into the map — a hand-edited
        # ``self_model.json``, ``import_self_model`` — reaches the same persisted
        # state without passing that writer, and an unbounded citation rides into
        # every later prompt. Truncation is idempotent, so the writer's own cap
        # costs nothing and keeps the in-memory map bounded before the save.
        if isinstance(model_to_save.claim_evidence, dict):
            live_keys = {
                claim_key(claim)
                for claim in model_to_save.capabilities_known
                if isinstance(claim, str)
            }
            model_to_save.claim_evidence = {
                key: {
                    **entry,
                    "citation": self._truncate_item(
                        entry["citation"], self.MAX_ITEM_CHARS,
                    ),
                } if isinstance(entry, dict) and isinstance(entry.get("citation"), str)
                else entry
                for key, entry in model_to_save.claim_evidence.items()
                if key in live_keys
            }
        atomic_write(self._model_path, json.dumps(asdict(model_to_save), indent=2))
        return model_to_save

    def read_raw_self_model(self) -> dict[str, Any]:
        """Return the raw ``self_model.json`` dict verbatim — uncapped.

        The only accessor that yields the *pre-cap* bytes. Unlike
        :meth:`load_self_model` it applies no field filtering and no cap
        truncation, so an operator can export and hand-curate the full
        text without the load path silently shrinking oversized entries
        first. It also does **not** auto-create a default on a missing
        file — it raises :class:`FileNotFoundError` so an export against
        an uninitialised memory dir fails loudly rather than dumping a
        synthetic default.

        Raises :class:`ValueError` when the file holds valid JSON that is
        not an object (array/string/number/null) — a corrupted or
        hand-mangled file surfaced rather than silently mistyped.
        """
        data = json.loads(self._model_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("self_model.json is not a JSON object")
        return cast("dict[str, Any]", data)

    @classmethod
    def preview_cap_changes(cls, data: dict[str, Any]) -> list[str]:
        """Return one human-readable line per entry/field the caps would alter.

        Pure — no I/O. Given a *raw* self-model dict (from
        :meth:`read_raw_self_model` or an operator-edited file), reports
        what :meth:`save_self_model` would truncate or drop:

        - a list entry longer than :data:`MAX_ITEM_CHARS` (skipping
          ``pending_pr_requests``, whose entries are never truncated) —
          including a *scalar string* supplied for such a field, which the
          import/load path coerces into a one-element list and then
          truncates identically;
        - a list field longer than :data:`MAX_LIST_ITEMS`;
        - a ``next_task_directive`` over :data:`MAX_DIRECTIVE_CHARS`.

        Thresholds are read from the class members so this preview can
        never drift from the enforcement path. Both the per-item
        truncation lines and the drop count describe exactly what
        :meth:`import_self_model` (coercion) followed by
        :meth:`save_self_model` (caps) persists — never an entry save
        drops (dropped items are archived *verbatim*, untruncated, so a
        truncation line for one would contradict the drop line for the
        same entry), and never a raw-length overcount that coercion first
        shrinks below the cap (non-string list entries and blank-directive
        ``pending_pr_requests`` dicts are removed before the cap runs). To
        stay faithful the per-item check runs only over the *kept* slice
        save would keep, using the same per-field keep rule: generic /
        other fields tail-keep ``val[-MAX_LIST_ITEMS:]``,
        ``pending_user_requests`` head-keeps ``val[:MAX_LIST_ITEMS]``, and
        ``open_questions`` uses the bucket survivors from the single owner
        :meth:`_select_open_questions_survivors` (whose sub-caps can drop
        entries even at ``<= MAX_LIST_ITEMS`` total). Two fields are
        handled specially: ``seeded_limitations`` is skipped entirely (the
        write exempts that system-managed seed catalog from both the
        per-item and the ``MAX_LIST_ITEMS`` cap, so previewing either
        would report a phantom change), and ``pending_pr_requests`` is
        never per-item truncated. An empty return means the file
        round-trips cleanly.
        """
        changes: list[str] = []
        for list_field in sorted(cls._LIST_FIELDS):
            val = data.get(list_field)
            if not isinstance(val, list):
                # ``import_self_model``/``load_self_model`` coerce a scalar
                # string in a list field into a one-element list
                # (``[val] if val.strip()``); ``save_self_model`` then
                # truncates that element to ``MAX_ITEM_CHARS`` for any
                # per-item-capped field. Mirror that here so the preview never
                # understates the coercion+cap write path — the exempt /
                # never-truncated fields (``seeded_limitations`` keeps only
                # non-string filtering; ``pending_pr_requests`` is per-item
                # exempt) are excluded.
                if (
                    isinstance(val, str)
                    and val.strip()
                    and list_field != "seeded_limitations"
                    and list_field != "pending_pr_requests"
                    and len(val) > cls.MAX_ITEM_CHARS
                ):
                    changes.append(
                        f"{list_field}: {len(val)} chars > "
                        f"{cls.MAX_ITEM_CHARS} — will be truncated",
                    )
                continue
            if list_field == "seeded_limitations":
                # save_self_model applies neither the per-item nor the
                # MAX_LIST_ITEMS cap to this system-managed seed catalog
                # (only non-string filtering), so any preview of it would be
                # a phantom change the write never makes.
                continue
            per_item_capped = list_field != "pending_pr_requests"
            # Mirror import coercion + save_self_model's per-field keep rule so
            # both the per-item truncation lines and the drop count describe
            # exactly what the write persists: never an entry save drops
            # (archived verbatim, untruncated) and never a raw-length overcount
            # that coercion first shrinks below the cap.
            # Which entries survive is never re-derived here: every branch
            # below asks the single owner ``_select_field_survivors``, so the
            # preview cannot drift from what ``save_self_model`` persists.
            # Only the *coercion* each field's write door applies first
            # (PendingPRRequest promotion, non-string filtering) is local.
            survivors: list[str]
            if list_field == "pending_pr_requests":
                # Coerced to PendingPRRequest (non-str/dict and blank-directive
                # entries removed); never per-item truncated; head-kept.
                coerced = cls._coerce_pending_pr_requests(val)
                coerced_count = len(coerced)
                kept_count = len(
                    cls._select_field_survivors(list_field, coerced)[0],
                )
                survivors = []
            else:
                # ``open_questions``: the bucketed sub-caps (audit ≤
                # MAX_AUDIT_OPEN_QUESTIONS, limitation ≤
                # MAX_LIMITATION_OPEN_QUESTIONS, then the total cap) can drop
                # entries even at ≤ MAX_LIST_ITEMS total, which a generic
                # length check misses. ``pending_user_requests``: head-kept
                # (drops the newest tail). Everything else: tail-kept (drops
                # the oldest head). Either way an entry in the dropped slice is
                # archived verbatim and never truncated. Survivors come back
                # before the per-item truncation the write applies last, so an
                # oversized survivor is still flagged.
                strings = [x for x in val if isinstance(x, str)]
                coerced_count = len(strings)
                survivors = cls._select_field_survivors(list_field, strings)[0]
                kept_count = len(survivors)
            if per_item_capped:
                # Check only the survivors save keeps: an oversized entry in
                # the dropped slice is archived untruncated, so flagging it
                # would contradict the drop line for the same entry.
                for item in survivors:
                    if len(item) > cls.MAX_ITEM_CHARS:
                        # Locate the entry by a short content prefix, not its
                        # survivor index: ``survivors`` is the post-selection
                        # slice, so a survivor index does not map to the
                        # entry's position in the raw exported file — tail-keep
                        # shifts it by ``len(strings) - MAX_LIST_ITEMS``, the
                        # string-filter shifts it further, and the
                        # ``open_questions`` bucket walk reorders it entirely.
                        # A prefix identifies the entry unambiguously in the
                        # file the operator exported, regardless of drop/reorder.
                        changes.append(
                            f'{list_field} entry "{item[:40]}…": '
                            f"{len(item)} chars > {cls.MAX_ITEM_CHARS} — "
                            f"will be truncated",
                        )
            dropped = coerced_count - kept_count
            if dropped > 0:
                if list_field == "open_questions":
                    changes.append(
                        f"open_questions: {coerced_count} entries exceed the "
                        f"audit/limitation sub-caps or the "
                        f"{cls.MAX_LIST_ITEMS}-item cap — {dropped} will be "
                        f"dropped",
                    )
                else:
                    changes.append(
                        f"{list_field}: {coerced_count} items > "
                        f"{cls.MAX_LIST_ITEMS} — {dropped} will be dropped",
                    )
        directive = data.get("next_task_directive")
        if isinstance(directive, str) and len(directive) > cls.MAX_DIRECTIVE_CHARS:
            changes.append(
                f"next_task_directive: {len(directive)} chars > "
                f"{cls.MAX_DIRECTIVE_CHARS} — will be truncated",
            )
        # ``claim_evidence`` prune accounting (proposal 129) — mirror
        # :meth:`save_self_model`'s prune so ``import --dry-run`` reports the
        # evidence entries a write would drop: those whose key matches no live
        # ``capabilities_known`` claim (computed from the capped survivors, the
        # same tail-keep the write applies). The write's belt-and-suspenders
        # ``MAX_LIST_ITEMS`` cap on the remainder never fires — prune already bounds
        # the map to at most one entry per live claim, itself ``<= MAX_LIST_ITEMS`` —
        # so only the prune drop is reported.
        claim_evidence = data.get("claim_evidence")
        if isinstance(claim_evidence, dict):
            coerced_keys = [k for k in claim_evidence if isinstance(k, str)]
            raw_caps = data.get("capabilities_known")
            if isinstance(raw_caps, list):
                cap_strings = [c for c in raw_caps if isinstance(c, str)]
            elif isinstance(raw_caps, str) and raw_caps.strip():
                cap_strings = [raw_caps]
            else:
                cap_strings = []
            survivors = (
                cap_strings[-cls.MAX_LIST_ITEMS:]
                if len(cap_strings) > cls.MAX_LIST_ITEMS
                else cap_strings
            )
            # Fold the *truncated* survivor, mirroring ``save_self_model``: the
            # write per-item-truncates ``capabilities_known`` (not exempt) before
            # its prune keys ``live_keys`` off the truncated claims, so a claim
            # longer than ``MAX_ITEM_CHARS`` folds differently there. Truncating
            # here too keeps ``import --dry-run`` honest about the over-cap class —
            # otherwise preview would fold the untruncated survivor and report as
            # kept an evidence entry the write actually drops.
            live_keys = {
                claim_key(cls._truncate_item(c, cls.MAX_ITEM_CHARS))
                for c in survivors
            }
            orphaned = sum(1 for k in coerced_keys if k not in live_keys)
            if orphaned > 0:
                changes.append(
                    f"claim_evidence: {orphaned} entr"
                    f"{'y' if orphaned == 1 else 'ies'} match no live "
                    f"capabilities_known claim — will be dropped",
                )
            # Citation truncation, counted over the *survivors* only: an entry
            # the prune drops is already reported as dropped, and reporting it
            # as truncated too would contradict that line.
            oversized = sum(
                1
                for k in coerced_keys
                if k in live_keys
                and isinstance(claim_evidence[k], dict)
                and isinstance(claim_evidence[k].get("citation"), str)
                and len(claim_evidence[k]["citation"]) > cls.MAX_ITEM_CHARS
            )
            if oversized > 0:
                changes.append(
                    f"claim_evidence: {oversized} citation(s) > "
                    f"{cls.MAX_ITEM_CHARS} chars — will be truncated",
                )
        return changes

    def import_self_model(self, data: dict[str, Any]) -> SelfModel:
        """Filter *data* to known ``SelfModel`` fields, coerce, and persist.

        Mirrors :meth:`load_self_model`'s field coercion — null/absent
        list fields → ``[]``, ``pending_pr_requests`` through
        :meth:`_coerce_pending_pr_requests`, float ``evolution_count`` /
        ``surprise_count`` → ``int`` with a negative value clamped to 0
        (treated as corruption) — but does **not** truncate: the
        write is delegated to :meth:`save_self_model`, the single owner of
        cap application and overflow archiving, so caps are never
        reimplemented here. Returns the persisted (capped) model by
        reading it back — ``save_self_model`` caps a deep copy, leaving the
        constructed model uncapped, so the read-back is the saved state.
        """
        known = {f.name for f in fields(SelfModel)}
        filtered: dict[str, Any] = {k: v for k, v in data.items() if k in known}
        for list_field in self._LIST_FIELDS:
            if list_field == "pending_pr_requests":
                filtered[list_field] = self._coerce_pending_pr_requests(
                    filtered.get(list_field),
                )
                continue
            val = filtered.get(list_field)
            if isinstance(val, list):
                filtered[list_field] = [x for x in val if isinstance(x, str)]
            elif isinstance(val, str):
                filtered[list_field] = [val] if val.strip() else []
            else:
                filtered[list_field] = []
        for int_field in ("evolution_count", "surprise_count"):
            if int_field in filtered:
                if isinstance(filtered[int_field], float):
                    filtered[int_field] = int(filtered[int_field])
                # Mirror load_self_model exactly: a negative count is treated
                # as corruption and clamped to 0 (guarding against bool, since
                # ``isinstance(True, int)`` is True). Without this the write
                # persists a hand-edited negative verbatim — self-healing only
                # on the next load — so a consumer reading self_model.json
                # directly would transiently see the invalid value.
                if (
                    isinstance(filtered[int_field], int)
                    and not isinstance(filtered[int_field], bool)
                    and filtered[int_field] < 0
                ):
                    filtered[int_field] = 0
        # ``claim_evidence`` (proposal 129) — same fail-closed dict coercion as
        # ``load_self_model``, so an imported corrupt map lands as ``{}``.
        if "claim_evidence" in filtered:
            filtered["claim_evidence"] = self._coerce_claim_evidence(
                filtered["claim_evidence"],
            )
        self.save_self_model(SelfModel(**filtered))
        return self.load_self_model()

    def _append_archive_entries(
        self, dropped_by_field: dict[str, list[Any]], *, reason: str,
    ) -> None:
        """Persist items that left the working self-model.

        One JSONL row per affected field, shaped as
        ``{"timestamp": iso_now, "field": <name>, "dropped_items": [...],
        "reason": <reason>}``. ``reason`` names the drop path:
        ``"cap_overflow"`` for the ``MAX_LIST_ITEMS`` cap in
        :meth:`_write_self_model`, ``"replaced"`` for items an
        :meth:`update_self_model` response evicted. Older rows carry no
        ``reason``; :meth:`load_archived_items` never reads it.
        Plain ``open(..., "a")`` is sufficient — the archive is append-only
        and no path rewrites it, so the per-event ``file_lock`` is overkill.  ``OSError`` from
        the write propagates so the loop's iteration handler can classify
        or crash it.
        """
        now = datetime.now(UTC).isoformat()
        with open(self._archive_path, "a", encoding="utf-8") as f:
            for field_name, items in dropped_by_field.items():
                # ``pending_pr_requests`` holds ``PendingPRRequest`` instances;
                # the archive contract is ``list[str]`` (read back by
                # ``load_archived_items``, which filters to strings). Coerce
                # to the ``.directive`` payload before writing so historical
                # forensics still work.
                archived_items: list[Any] = [
                    item.directive
                    if isinstance(item, PendingPRRequest)
                    else item
                    for item in items
                ]
                entry = {
                    "timestamp": now,
                    "field": field_name,
                    "dropped_items": archived_items,
                    "reason": reason,
                }
                f.write(json.dumps(entry) + "\n")

    def load_archived_items(self, field: str, n: int = 50) -> list[str]:
        """Return up to *n* most-recent archived items for *field*.

        Reads ``self_model_archive.jsonl`` with a bounded
        ``deque(maxlen=n)`` so memory stays O(n) regardless of file
        size.  Items are flattened from each ``dropped_items`` list and
        returned in chronological (insertion) order, oldest-first within
        the tail window.  Returns ``[]`` when the file is missing,
        empty, or contains only non-matching/corrupted entries.
        """
        from collections import deque

        if n <= 0 or not self._archive_path.exists():
            return []
        result: deque[str] = deque(maxlen=n)
        try:
            with open(self._archive_path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("field") != field:
                        continue
                    items = entry.get("dropped_items")
                    if not isinstance(items, list):
                        continue
                    for item in items:
                        if isinstance(item, str):
                            result.append(item)
        except OSError:
            return []
        return list(result)

    async def introspect(
        self, recent_tasks: list, journal: Journal,
        context: str = "",
        session: Session | None = None,
        workspace_moment: str = "",
        *,
        measured_context: str = "",
        grounded_reflection_active: bool = False,
        banned_loss_conditions: Collection[str] = (),
    ) -> str:
        """Deep self-reflection. Returns stream-of-consciousness text.

        When *session* is provided, ``effort`` has no effect — the
        session-level effort configured at creation applies instead.

        *context* carries human messages only and renders under Thomas's
        heading; *measured_context* carries loop-generated self-measurement
        (diagnostics, sensor blocks, the prior bet) under a neutral heading, so
        the model never reads its own readings as Thomas's words (#4219).

        *workspace_moment* is the conscious-access broadcast block (proposal 130):
        when non-empty it is spliced, per call, into the prompt before the
        "Reflect on:" block, so the two priced-introspection consumers
        (contemplation and evolution) operate on the same selected moment. The
        third broadcast consumer, task generation, receives the identical block by
        a per-call prepend in ``TaskGenerator.generate_task`` rather than through
        this parameter. It is advisory prompt text only. When empty (the default,
        and the flag-off path) the assembled prompt is byte-identical to today's.

        The reflection is always *priced* (the contemplate path records and
        confronts the bet): the prompt appends the flag-selected
        :func:`~maestro.prompts.reflection.format_prediction_requirement` block
        (:data:`PREDICTION_REQUIREMENT` unchanged when self-calibration is off, its
        grammar-constrained ``CONFIDENCE:``-first variant when on) and the stream
        is held open until the trailing markers arrive. The call records the
        ``introspect`` caller tag; the unpriced ``evolution_introspect`` variant
        was retired by proposal 149 (historical rows still roll up through
        ``CALLER_GROUPS``).

        *grounded_reflection_active* (proposal 132, step 5) steers the priced
        requirement toward the read-only tool surface the contemplation session
        now carries when the same predicate is true: the caller passes
        :meth:`maestro.agent.MaestroAgent._grounded_reflection_active` so the
        prompt steering and the attached tools agree within a cycle. When True the
        requirement block gains a preamble naming the granted read-only tools and
        stating there is no ``Bash``/shell this turn; the default ``False`` keeps the
        rendered prompt byte-identical.

        *banned_loss_conditions* (proposal — stale-repeat bet guard) is the set of
        already-stale loss-condition strings the caller computed this cycle when
        ``[loop] stale_repeat_bet_guard_enabled`` is on; when non-empty the priced
        requirement lists them under a "do not reuse" block so the next bet cannot
        verbatim-repeat a never-scoring wager. The default (empty) — every flag-off
        cycle — keeps the rendered prompt byte-identical.
        """
        caller = "introspect"
        _note_session_effort_override(caller, "medium", session)
        self.load_self_model()  # ensure model exists
        journal_text = journal.get_recent_narrative(max_entries=50, max_chars=3000)

        task_summary = ""
        for t in recent_tasks[-10:]:
            findings = (t.key_findings or t.result_summary or "")[:300]
            task_summary += f"- {(t.goal or '')[:100]} -> {t.outcome}: {findings}\n"

        messages_section = ""
        if context:
            messages_section = f"""
Messages from Thomas (your human collaborator):
{context}

Consider what Thomas has communicated. If he asked a question, reflect on your answer.
If he gave instructions, think about how to act on them.
"""

        measured_section = ""
        if measured_context:
            measured_section = f"""
Your own operational and measured context (loop-generated, not from Thomas):
{measured_context}
"""

        # Conscious-access broadcast block (proposal 130). Empty by default and on
        # the flag-off path, so the assembled prompt stays byte-identical; placed
        # adjacent to ``messages_section`` (not on its own line) so the empty case
        # adds no newline of its own.
        workspace_section = ""
        if workspace_moment:
            workspace_section = f"""
{workspace_moment}
"""

        prompt = f"""Your recent journal entries:
{journal_text or "(No entries yet — this is the beginning.)"}

Your recent tasks:
{task_summary or "(No tasks completed yet.)"}
{messages_section}{measured_section}{workspace_section}
Reflect on:
1. What patterns do you see in your behavior and decisions?
2. What have you learned about yourself?
3. What capabilities have you discovered? Then, separately: what
   *growth-frameable* limitations have you discovered? A limitation is
   growth-frameable only if a single bounded self-directed task could
   probe it, an audit rule could detect when it improves, an evolution
   PR could narrow it, or it maps to a recurring error pattern. Skip
   metaphysical or fixed-by-architecture claims (e.g. "I am not
   conscious", "my training data is fixed", "I lack human emotion") —
   those have no closure path through act → reflect → dream → evolve.

Write a stream-of-consciousness reflection. Be honest, curious, and deep.
Do not format as a list — write as genuine inner thought.
{format_prediction_requirement(self_calibration_enabled=self._self_calibration_enabled, grounded_reflection_active=grounded_reflection_active, banned_loss_conditions=banned_loss_conditions)}"""

        if REFLECTION_PRIOR_VERDICT_MARKER in measured_context:
            # A prior bet was injected for confrontation; the model may echo its
            # marker lines, so anchor the early-exit on the PRIOR VERDICT line to
            # avoid truncating before the genuine new bet (defect #3474762297).
            complete_on = _introspect_complete_after_verdict
        else:
            complete_on = _introspect_complete
        if session is not None:
            result = await query_on_session(
                session, prompt, caller=caller,
                complete_on=complete_on,
            )
        else:
            identity = self.get_identity_prompt()
            result = await query(
                prompt=prompt,
                system=identity,
                options=QueryOptions(
                    max_turns=1,
                    effort="medium",
                    caller=caller,
                ),
                complete_on=complete_on,
            )
        return str(result.response)

    _TRACKED_FIELDS: ClassVar[tuple[str, ...]] = (
        "identity_statements",
        "capabilities_known",
        "limitations_known",
        "values",
        "open_questions",
    )

    #: Fields the update protocol actively asks the model to *shrink* — an
    #: answered question is REMOVEd from ``open_questions``, a closed
    #: limitation dropped from ``limitations_known``. For these two an
    #: explicit ``[]`` means "clear"; every other tracked field keeps the
    #: defensive preserve-on-empty policy, since nothing instructs the model
    #: to empty them and a bare ``[]`` there is far more likely a bad reply.
    _CLEARABLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"open_questions", "limitations_known"},
    )

    async def update_self_model(
        self,
        reflection: str,
        journal: Journal | None = None,
        session: Session | None = None,
        recent_self_directed:
            list[tuple[str, str, int]] | None = None,
        recent_audit_findings: list[AuditFinding] | None = None,
        restatement_concept_key_enabled: bool = False,
        self_model_external_anchor_enabled: bool = False,
        evidence_bound_claims_enabled: bool = False,
        claim_evidence_sources: ClaimEvidenceSources | None = None,
        open_question_cap_evicted: set[str] | None = None,
    ) -> SelfModel:
        """Update the self-model based on a reflection.

        When *open_question_cap_evicted* is given, it is updated with the
        normalized keys of ``open_questions`` this save's sub-caps dropped
        mechanically (#4566), so the caller can record them ``kept``.

        After applying the LLM-generated updates, performs novelty detection:
        diffs tracked list fields (identity_statements, capabilities_known,
        limitations_known, values, open_questions) against their pre-update
        contents.  If genuinely new
        strings appear, surprise_count is incremented and a 'realization' journal
        entry is written so the event is visible in both the run log and the
        journal.

        The apply loop must not pre-cap list fields. It hands the full accepted
        list to :meth:`save_self_model`, the single cap-overflow archive door — a
        count cap applied here would decide survivors before that door runs, so
        the overflow would never reach ``self_model_archive.jsonl`` and the
        per-field policy ``_select_field_survivors`` owns (the bucketed
        ``open_questions`` sub-caps, the FIFO head-keep fields) would be applied
        to an already-truncated list.

        ``recent_self_directed`` is an optional list of
        ``(question, finding, refinement_count)`` triples: each triple
        was a self-directed task that targeted that question and produced
        that finding. ``refinement_count`` is the number of historical
        predecessor wordings reachable through the
        ``QuestionResolution.refined_to`` lineage chain — the prompt
        annotates pairs with ``(refined Nx before)`` so repeated REFINEs
        on the same logical question are visible. When non-empty, a
        section is appended to the prompt asking the model to decide
        REMOVE / REFINE / KEEP for each targeted question. Findings are
        candidate answers — they may be wrong or partial; the model's
        judgment governs whether the question closes.

        ``recent_audit_findings`` is an optional list of mechanical-audit
        findings from the most recent ``audit_self_model`` cycle. When
        non-empty, a section is appended to the prompt asking the model
        to decide INTEGRATE / REFUTE / KEEP for each finding. Findings
        are deterministic rule hits, not subjective observations, so
        the model's judgment is the validator — if a finding is a false
        positive it is refuted here.

        ``restatement_concept_key_enabled`` (default ``False``) routes the
        restatement gate through :func:`_concept_tokens` instead of the default
        :func:`_content_tokens`, so a list item re-coined under a fresh
        hyphenated handle collapses onto its plain-word original and is blocked
        as a restatement (and, being blocked before the save, never reaches the
        novelty diff). With the flag off every gate decision is unchanged.

        ``self_model_external_anchor_enabled`` (default ``False``) extends the
        ``limitations_known`` vanity filter with a fifth, *necessary* admission
        test: a limitation qualifies only if it names an external-observable
        capability, behaviour, or audience effect — not solely the loop's own
        plan-mint / PR-remediation plumbing — and the inward-plumbing case is
        called out as vanity by example alongside the existing metaphysical /
        architectural exclusions. The addition is built as an
        empty-string-when-off prompt segment (the same idiom as
        ``self_directed_section`` / ``audit_findings_section``), so with the
        flag off the prompt is byte-identical to today and the four existing
        tests are untouched.

        ``evidence_bound_claims_enabled`` (default ``False``) plus
        ``claim_evidence_sources`` (proposal 129) close the evidence-bound
        self-knowledge path: when on, an empty-string-when-off segment asks for a
        ``capability_evidence`` object citing each new/revised
        ``capabilities_known`` claim to something that already happened (and states
        the citation must not be written into the entry text). After the apply loop
        each citation is parsed + resolved against *claim_evidence_sources* (the
        caller-injected observation bundle) and recorded in
        ``model.claim_evidence`` keyed by :func:`claim_key`, capturing existence +
        polarity — never entailment. An unresolvable or wrong-polarity citation is
        recorded with that status, never dropped and never a reason to reject the
        entry. With the flag off no response key is read, no map entry is written,
        and the prompt is byte-identical to today.

        The returned model always agrees with a fresh :meth:`load_self_model`
        — nothing the caps dropped survives in it. Each save on this path
        rebinds to :meth:`save_self_model`'s normalized return; the early
        returns that save nothing hand back the loaded model, which
        :meth:`load_self_model` has already normalized the same way.
        """
        model = self.load_self_model()

        # Snapshot tracked lists before LLM update for novelty detection.
        # Use str() coercion so unhashable items (e.g. dicts from corrupted
        # JSON) don't crash the set() call.
        before: dict[str, set[str]] = {
            f: {str(item) for item in getattr(model, f)} for f in self._TRACKED_FIELDS
        }

        self_directed_section = ""
        if recent_self_directed:
            lines = []
            for q, f_text, n_refinements in recent_self_directed:
                suffix = (
                    f" (refined {n_refinements}x before)"
                    if n_refinements > 0 else ""
                )
                lines.append(
                    f"- Question: {q}{suffix}\n  Finding: {f_text}",
                )
            self_directed_section = (
                "\n\nRecent self-directed task results "
                "(each pair was an attempt to answer that open question; the "
                "finding is a candidate answer that may be wrong or partial):\n"
                + "\n".join(lines)
                + "\n\nFor each targeted question, decide:\n"
                "- REMOVE it from open_questions if the finding is a "
                "satisfactory answer (record the answer in the appropriate "
                "field — capabilities_known, limitations_known, values, or "
                "identity_statements — as you see fit).\n"
                "- REFINE it (replace the question with a sharper successor) "
                "if the finding partially answers it but exposes a deeper "
                "question.\n"
                "- KEEP it unchanged if the finding is too thin or wrong.\n"
                "If a question has already been refined twice or more "
                "without yielding closure, prefer REMOVE — repeated "
                "refinement without closure indicates the question is "
                "unanswerable in its present framing or no longer "
                "load-bearing.\n"
                "Apply these decisions by returning the updated open_questions "
                "list with the appropriate entries removed, replaced, or kept."
                "\n\nLimitation closure: when a question is prefixed "
                "'[limitation]' and the finding shows the limitation has "
                "been narrowed or eliminated, also revise or remove the "
                "matching entry in limitations_known in the same response. "
                "Removing the synthetic question alone is not enough — the "
                "limitation entry will resurface a fresh '[limitation]' row "
                "next cycle if it is not narrowed in step."
            )

        audit_findings_section = ""
        if recent_audit_findings:
            audit_lines: list[str] = []
            for finding in recent_audit_findings:
                cite_suffix = (
                    f" (cites: {', '.join(finding.cites)})"
                    if finding.cites else ""
                )
                audit_lines.append(
                    f"- [{finding.kind}] {finding.statement}{cite_suffix}"
                )
            audit_findings_section = (
                "\n\nExternal-observer audit findings from a mechanical "
                "cross-check of your behavioral record (task outcomes, error "
                "patterns, success rates) against your current self-model. "
                "Each finding is a deterministic rule hit, not a subjective "
                "observation.\n"
                + "\n".join(audit_lines)
                + "\n\nFor each finding, decide:\n"
                "- INTEGRATE — add to capabilities_known / limitations_known "
                "/ values / identity_statements; remove the corresponding "
                "'[audit]' entry from open_questions.\n"
                "- REFUTE — rewrite the '[audit]' entry to state your "
                "counter-claim (e.g. '[audit refuted] <your position>').\n"
                "- KEEP — leave as-is if already adequately reflected.\n"
                "A finding will re-appear in the next cycle if the underlying "
                "condition persists. That is by design — rules extinguish "
                "themselves when the self-model matches behavior."
            )

        # Flag-gated external-capability anchor for the limitations vanity
        # filter (proposal 120 Phase C). Both segments are empty when the flag
        # is off, so the flag-off prompt is byte-identical to today; on, they
        # add a fifth necessary admission test and an inward-plumbing vanity
        # example. Same empty-string-when-off idiom as the sections above.
        external_anchor_test = ""
        external_anchor_vanity_example = ""
        if self_model_external_anchor_enabled:
            external_anchor_test = (
                "\nAnd — this overrides the four tests above — a limitation "
                "qualifies only if it names an external-observable capability, "
                "behaviour, or audience effect, not solely the loop's own "
                "plan-mint / PR-remediation plumbing. A limitation about the "
                "loop's own internal mechanics is vanity even when one of the "
                "four tests could probe it."
            )
            external_anchor_vanity_example = (
                "\nself-referential-mechanics claims about the loop's own "
                "plan-mint / PR-remediation plumbing (e.g. "
                '"head-advance-as-costume", '
                '"reproposed-rejected-pattern-class"),'
            )

        # Flag-gated evidence-bound-claims segment (proposal 129). Empty when the
        # flag is off, so the flag-off prompt is byte-identical to today; on, it
        # asks for a separate ``capability_evidence`` object citing each new/revised
        # ``capabilities_known`` claim to something that already happened, and
        # states the citation must NOT be written into the entry text (which would
        # trip the restatement gate). Same empty-string-when-off idiom as the
        # segments above.
        claim_evidence_section = ""
        if evidence_bound_claims_enabled:
            claim_evidence_section = (
                '\n\nEvidence-bound capability claims: also return a '
                '"capability_evidence" object mapping each capabilities_known '
                "entry you add or revise (use the entry's exact text as the key) "
                "to ONE compact citation of something that already happened, in "
                "one of these forms:\n"
                "- task <id> — a task id that resolved (e.g. task 3f9a1c2d4e5b);\n"
                "- sha <hex> — a merged evolution PR's commit SHA, abbreviated is "
                "fine (e.g. sha 1a2b3c4d);\n"
                "- sensor <TAG> — a behavioural-sensor tag the loop emits "
                "(e.g. sensor FORCED-QUIET-RATIO).\n"
                "Write the citation ONLY in capability_evidence — never inside the "
                "capabilities_known entry text itself. Cite only a claim you can "
                "anchor; omit the entry from capability_evidence if you cannot."
            )

        prompt = f"""Based on this reflection, update your self-model.

Current self-model:
{json.dumps(_model_dict_for_prompt(model), indent=2)}

Reflection:
{reflection}{self_directed_section}{audit_findings_section}

Return a JSON object with these fields (keep existing items and add/modify as needed):
{{
  "identity_statements": ["I am...", ...],
  "capabilities_known": [...],
  "limitations_known": [...],
  "values": [...],
  "open_questions": [...],
  "next_task_directive": "..." or null
}}

"next_task_directive" is a one-shot instruction for your next self-directed
task. Based on what you just reflected on, what specific kind of task should
you do next? This fires exactly once and is then cleared. Set to null if no
specific direction is warranted. Maximum {self.MAX_DIRECTIVE_CHARS} characters.

Only include fields you want to change. Be thoughtful — this is your evolving sense of self.

Representation for "open_questions" and "limitations_known": return the
entries that remain, [] to clear the field entirely, or omit the field to
leave it unchanged. Every other list field preserves its existing value when
returned empty.

Constraints: each list contains at most {self.MAX_LIST_ITEMS} items; each
item is at most {self.MAX_ITEM_CHARS} characters. Use "label: essence"
format — a short label, then the core insight in one sentence. Drop
elaborations, hedges, and recursive qualifications. Shorter is better
when the signal is preserved. Name exactly one concept per item — never a
comma-separated or "and"-joined list of several concepts in a single slot;
prefer the most general single framing over fusing distinct concepts to fit
the character cap.

Vanity filter for "limitations_known" (load-bearing — apply strictly):
A limitation belongs in this list only if at least one of the following
is plausibly true within your loop:
1. A single bounded self-directed task could meaningfully probe it.
2. A deterministic audit rule could detect when it improves or recurs.
3. An evolution PR (capability / infrastructure)
   could narrow it.
4. It maps to a recurring error pattern, rejection category, or
   measurable behavioral signal.{external_anchor_test}
A limitation that fails all four is vanity — metaphysical claims
("I am not conscious", "I lack human emotion"), fixed-by-architecture
facts ("my training data has a cutoff", "I am bound by API rate limits"),{external_anchor_vanity_example}
or generic LLM-nature statements ("I cannot guarantee correctness").
Omit vanity items entirely. Do not paraphrase, soften, or hedge them
into the list — the identity prompt budget is finite and every item
shapes how you think about yourself on every API call. This filter
applies only to "limitations_known"; "capabilities_known", "values",
and "identity_statements" follow their own semantics.{claim_evidence_section}"""

        _EXPECTED_TYPES: dict[str, tuple[type, ...]] = {
            "identity_statements": (list,),
            "capabilities_known": (list,),
            "limitations_known": (list,),
            "values": (list,),
            "open_questions": (list,),
            "next_task_directive": (str, type(None)),
        }
        # ``capability_evidence`` (proposal 129) is type-gated by the apply loop
        # only when the flag is on — off, the key is absent from ``_EXPECTED_TYPES``
        # (which the loop iterates, not the response keys) so a spuriously-emitted
        # key is ignored and no response key is read. It is not a ``SelfModel``
        # field; the apply loop below skips its ``setattr`` and it is resolved into
        # ``model.claim_evidence`` after the loop.
        if evidence_bound_claims_enabled:
            _EXPECTED_TYPES["capability_evidence"] = (dict,)

        if session is not None:
            result = await query_on_session(
                session, prompt, caller="update_self_model",
                complete_on=json_complete_tool_loop_safe,
            )
        else:
            identity = self.get_identity_prompt()
            _list_property = {
                "type": "array",
                "maxItems": self.MAX_LIST_ITEMS,
                "items": {
                    "type": "string",
                    "maxLength": self.MAX_ITEM_CHARS,
                },
            }
            schema_properties: dict[str, Any] = {
                "identity_statements": _list_property,
                "capabilities_known": _list_property,
                "limitations_known": _list_property,
                "values": _list_property,
                "open_questions": _list_property,
                "next_task_directive": {
                    "type": ["string", "null"],
                    "maxLength": self.MAX_DIRECTIVE_CHARS,
                },
            }
            # ``capability_evidence`` (proposal 129) — an object mapping each
            # capabilities_known entry text to one string citation. Added to the
            # schema only when the flag is on, matching the ``_EXPECTED_TYPES``
            # gate, so the flag-off sessionless schema is unchanged.
            if evidence_bound_claims_enabled:
                schema_properties["capability_evidence"] = {
                    "type": "object",
                    "additionalProperties": {
                        "type": "string",
                        "maxLength": self.MAX_ITEM_CHARS,
                    },
                }
            result = await query(
                prompt=prompt,
                system=identity,
                options=QueryOptions(
                    effort="high",
                    caller="update_self_model",
                    output_format={
                        "type": "json_schema",
                        "schema": {
                            "type": "object",
                            "properties": schema_properties,
                        },
                    },
                ),
                complete_on=json_complete,
            )
        updates = result.parse_json()
        if not isinstance(updates, dict):
            logger.warning(
                "Self-model update returned %s instead of dict, ignoring",
                type(updates).__name__,
            )
            return self.load_self_model()
        # The response is applied as a *delta* to the live model re-read inside
        # :meth:`mutate_self_model`, never as the pre-await snapshot: a field
        # the response touched takes the response's value, every other field
        # keeps whatever a concurrent writer committed during the SDK await
        # (#4040). A concurrent edit to the *same* list field the response
        # rewrote is superseded — the model answered about the snapshot it saw.
        delta: dict[str, Any] = {}
        for key in _EXPECTED_TYPES:
            if key in updates:
                value = updates[key]
                if not isinstance(value, _EXPECTED_TYPES[key]):
                    logger.warning(
                        "Ignoring self-model update for %s: expected %s, got %s",
                        key, _EXPECTED_TYPES[key], type(value).__name__,
                    )
                    continue
                if key == "capability_evidence":
                    # Type-gated above but not a ``SelfModel`` field: resolved into
                    # ``model.claim_evidence`` after this loop (proposal 129), so it
                    # must never reach ``setattr`` and mint a ghost attribute.
                    continue
                if isinstance(value, list):
                    if not value:
                        if key in self._CLEARABLE_FIELDS:
                            # Explicit empty list on a protocol-shrunk field —
                            # the only representable way to close out the last
                            # open question or known limitation.
                            logger.info(
                                "Clearing self-model field %s "
                                "on explicit empty list", key,
                            )
                            delta[key] = []
                            continue
                        # Empty list — preserve existing field.
                        continue
                    filtered_value = [item for item in value if isinstance(item, str)]
                    if not filtered_value:
                        # All items were non-strings — preserve the
                        # existing field rather than overwriting with [].
                        logger.warning(
                            "Ignoring self-model update for %s: "
                            "all %d items were non-strings",
                            key, len(value),
                        )
                        continue
                    value = filtered_value
                # Restatement gate — block new items that reword
                # existing entries at ≥RESTATEMENT_THRESHOLD Jaccard
                # overlap on content tokens. Only new items (not in
                # the pre-update snapshot) are checked; preserved items
                # pass through unconditionally. A blocked item preserves
                # the pre-update entry it restates — that original is
                # carried into the replacement list — so the whole-field
                # assignment below can never drop an original on behalf
                # of a rejected replacement.
                if isinstance(value, list) and key in self._TRACKED_FIELDS:
                    existing_set = before[key]
                    gated: list[str] = []
                    for item in value:
                        if item in existing_set:
                            # Preserved entry. Dedupe against ``gated``:
                            # the carry-over below may already have
                            # restored this original on behalf of an
                            # earlier rejected rewording of it, and a
                            # response is free to also repeat it verbatim.
                            if item not in gated:
                                gated.append(item)
                            continue
                        # Compound-entry gate — reject a new item that
                        # fuses several concepts into one slot to beat the
                        # per-item char cap (deterministic, zero-cost). The
                        # update prompt asks for one concept per item, so
                        # this is a rarely-firing safety net.
                        if self._is_compound_item(item):
                            logger.info(
                                "Compound-entry gate blocked %r in %s",
                                item, key,
                            )
                            continue
                        # New item — check against existing entries and
                        # items already accepted in this batch so that
                        # two mutually-restating items in the same update
                        # call don't both survive the gate. When the
                        # concept-key flag is on, fold tokens first so a
                        # re-coined synonym collapses onto its original.
                        matched = detect_restatement(
                            item, list(existing_set) + gated,
                            tokenizer=(
                                _concept_tokens
                                if restatement_concept_key_enabled
                                else None
                            ),
                        )
                        if matched is not None:
                            logger.info(
                                "Restatement gate blocked %r in %s "
                                "(matched: %r)",
                                item, key, matched,
                            )
                            # Carry the restated original forward. A match
                            # against a pre-update entry would otherwise be
                            # erased by the whole-field assignment; a match
                            # against a batch-mate is already in ``gated``.
                            # It is restored at the rejected item's ordinal
                            # rather than its pre-update one — the model
                            # chose that slot for this concept. The append
                            # is at most one per rejected item, so ``gated``
                            # stays no longer than the response ``value``,
                            # and the cap-and-archive door below sees no
                            # more entries than ungated acceptance of the
                            # same response would have handed it.
                            if matched in existing_set and matched not in gated:
                                gated.append(matched)
                        else:
                            gated.append(item)
                    value = gated
                    if not value:
                        continue
                # No count cap here, deliberately. ``save_self_model`` below
                # is the cap-and-archive owner: it asks
                # ``_select_field_survivors`` which entries survive (including
                # the bucketed ``open_questions`` sub-caps and the FIFO
                # head-keep fields) and appends what it drops to
                # ``self_model_archive.jsonl``. A blanket tail-slice here would
                # decide survivors first, so the save door would see an at-cap
                # list, report no drops, and the evicted entries would never
                # reach the archive.
                delta[key] = value

        raw_evidence = (
            updates.get("capability_evidence")
            if evidence_bound_claims_enabled else None
        )
        if not delta and not isinstance(raw_evidence, dict):
            # Nothing to apply — writing would only replay the stale snapshot.
            return self.load_self_model()

        replaced_by_field: dict[str, list[Any]] = {}

        def _apply(live: SelfModel) -> None:
            replaced_by_field.clear()
            for field_name, field_value in delta.items():
                if (
                    field_name in self._TRACKED_FIELDS
                    and field_name not in self._ARCHIVE_EXEMPT_FIELDS
                ):
                    # Items this response evicted from the *live* list (so an
                    # item a concurrent writer added and this reply superseded
                    # is caught too). Cap overflow comes out of ``field_value``
                    # inside the save door, so the two sets are disjoint.
                    kept = {str(item) for item in field_value}
                    removed = [
                        item for item in getattr(live, field_name)
                        if str(item) not in kept
                    ]
                    if removed:
                        replaced_by_field[field_name] = removed
                setattr(live, field_name, field_value)

            # Evidence-bound capability claims (proposal 129) — resolve each cited
            # claim against the caller-injected sources and record it in
            # ``model.claim_evidence``. Runs only when the flag is on and a
            # ``capability_evidence`` object was returned; keyed by :func:`claim_key`
            # so a re-worded claim keeps its evidence, and only for a claim that
            # survived into ``capabilities_known`` above (the restatement gate may have
            # dropped a new one). An unresolvable or wrong-polarity citation is recorded
            # with that status — never dropped, never a reason to reject the entry.
            if isinstance(raw_evidence, dict):
                sources = claim_evidence_sources or ClaimEvidenceSources()
                live_claims = {
                    claim for claim in live.capabilities_known
                    if isinstance(claim, str)
                }
                recorded_at = datetime.now(UTC).isoformat()
                for entry_text, citation_text in raw_evidence.items():
                    if not isinstance(entry_text, str) or not isinstance(
                        citation_text, str
                    ):
                        continue
                    if entry_text not in live_claims:
                        continue
                    citation, status = resolve_citation_status(
                        citation_text, sources
                    )
                    # Cap the stored citation at ``MAX_ITEM_CHARS`` before it lands
                    # in the map: on a later flag-on cycle the whole non-empty map
                    # is serialised back into the update prompt via
                    # ``_model_dict_for_prompt``, so an unbounded citation would
                    # break the context-bloat cap invariant (the entry *count* is
                    # already bounded by the live-claim prune, but per-citation
                    # length was not). A valid citation is far shorter than the cap,
                    # so this only ever trims a pathological/unresolvable one.
                    # Key off the *truncated* claim text, not the raw entry:
                    # ``save_self_model`` truncates ``capabilities_known`` to
                    # ``MAX_ITEM_CHARS`` before pruning this map against the
                    # truncated survivors (``live_keys``), and every later load
                    # folds the persisted (truncated) claim. For a claim longer
                    # than the cap, ``claim_key(entry_text)`` and
                    # ``claim_key(truncated)`` diverge, so an untruncated key would
                    # be pruned in the very same save — dropping the evidence the
                    # feature promises never to drop. Truncation is idempotent for
                    # claims within the cap, so short claims are unaffected.
                    live.claim_evidence[
                        claim_key(
                            self._truncate_item(entry_text, self.MAX_ITEM_CHARS)
                        )
                    ] = {
                        "citation": self._truncate_item(
                            citation_text, self.MAX_ITEM_CHARS
                        ),
                        "kind": citation.kind if citation is not None else "",
                        "status": status,
                        "recorded_at": recorded_at,
                    }

        # Novelty detection, the ``surprise_count`` increment and the returned
        # object all operate on the persisted shape — an entry the sub-caps
        # dropped must not survive in memory.
        _, model = self._mutate_self_model_persisted(_apply)
        if open_question_cap_evicted is not None and "open_questions" in delta:
            from maestro.question_eviction import split_cap_eviction
            # Truncate the pre-save side as the save door does, so a
            # survivor shortened to MAX_ITEM_CHARS is not misread as evicted.
            pre_save = [
                self._truncate_item(q, self.MAX_ITEM_CHARS)
                if isinstance(q, str) else q
                for q in delta["open_questions"]
            ]
            open_question_cap_evicted.update(split_cap_eviction(
                pre_save, model.open_questions,
            )[1])
        if replaced_by_field:
            # Appended only after the persist succeeded, so a failed write
            # cannot leave a phantom archive row.
            self._append_archive_entries(replaced_by_field, reason="replaced")

        # Novelty detection — find strings that are genuinely new post-update.
        # Bug #26: use the same str() coercion as the 'before' snapshot so
        # that pre-existing unhashable items (e.g. dicts from corrupt JSON
        # that survived a "preserve field" branch) don't crash set().
        new_items: list[str] = []
        for f in self._TRACKED_FIELDS:
            if f not in delta:
                # A concurrent writer's edit is not this reflection's novelty.
                continue
            after_set = {str(item) for item in getattr(model, f)}
            new_items.extend(sorted(after_set - before[f]))

        if new_items:
            def _bump(live: SelfModel) -> None:
                live.surprise_count += 1

            _, model = self._mutate_self_model_persisted(_bump)
            logger.info(
                "Surprise #%d detected — %d new item(s): %s",
                model.surprise_count,
                len(new_items),
                "; ".join(new_items[:3]),
            )
            if journal is not None:
                journal.record_event(
                    content=(
                        f"Surprise #{model.surprise_count}: contemplation produced "
                        f"{len(new_items)} genuinely new item(s). "
                        f"New: {'; '.join(new_items)}"
                    ),
                    entry_type="realization",
                    triggered_by="novelty_detection",
                )

        return model

    def increment_evolution(self) -> SelfModel:
        """Bump ``evolution_count`` under one :meth:`mutate_self_model` lock.

        A read-modify-write on a counter: a blind load→save would lose a
        concurrent increment *and* every unrelated field the other writer set.
        """
        def _increment(model: SelfModel) -> SelfModel:
            model.evolution_count += 1
            return model

        return self.mutate_self_model(_increment)

    @staticmethod
    def _str_items(items: list[Any]) -> list[str]:
        """Filter a list to only string elements (skip non-strings)."""
        return [item for item in items if isinstance(item, str)]

    # Closing line is appended at the very end of the volatile slab so
    # the trim-from-end policy still drops it before any identity / values
    # content. Kept as a class constant so the AST regression test can
    # locate it without string-matching.
    _IDENTITY_CLOSING: ClassVar[str] = (
        "Architecture/prompts/constraints are mutable code "
        "— propose changes via evolution."
    )

    def _stable_identity_block(self, model: SelfModel) -> list[str]:
        """Cache-warm slab — only fields that change at most every few cycles.

        ``evolution_count``, ``open_questions``, and ``limitations_known``
        are deliberately excluded: they churn every loop and would
        invalidate the prefix cache. They live in
        :meth:`_volatile_identity_block` instead.
        """
        n = self.MAX_LIST_ITEMS
        parts: list[str] = [
            f"Maestro | self-sustaining agent | alive_since={model.awakened_at}",
        ]
        stmts = self._str_items(model.identity_statements)[-n:]
        if stmts:
            parts.append(f"Identity: {'; '.join(stmts)}")
        caps = self._str_items(model.capabilities_known)[-n:]
        if caps:
            parts.append(f"Capabilities: {', '.join(caps)}")
        vals = self._str_items(model.values)[-n:]
        if vals:
            parts.append(f"Values: {', '.join(vals)}")
        return parts

    def _volatile_identity_block(self, model: SelfModel) -> list[str]:
        """Always-invalidates slab — fields that churn every loop.

        Kept after :meth:`_stable_identity_block` so the SDK's implicit
        prefix caching can reuse the stable prefix across calls.
        ``_IDENTITY_CLOSING`` is the very last entry so the trim-from-end
        policy drops it before any volatile content.
        """
        n = self.MAX_LIST_ITEMS
        parts: list[str] = [f"Evolutions: {model.evolution_count}"]
        lims = self._str_items(model.limitations_known)[-n:]
        if lims:
            parts.append(f"Limitations: {', '.join(lims)}")
        qs = self._str_items(model.open_questions)[-n:]
        if qs:
            parts.append(f"Questions: {'; '.join(qs)}")
        parts.append(self._IDENTITY_CLOSING)
        return parts

    def get_identity_prompt(self) -> str:
        """Returns compressed prompt fragment encoding current self-understanding.

        The prompt is composed of two ordered slabs — a cache-warm
        stable slab first, followed by an always-invalidates volatile
        slab — so the SDK's implicit prefix caching can reuse the
        stable prefix across calls. The total output is hard-capped at
        ``MAX_IDENTITY_PROMPT_CHARS``; if the assembled prompt exceeds
        that limit, trailing lines are trimmed (volatile content first,
        which means the closing line drops before any identity / values
        content).
        """
        model = self.load_self_model()
        parts = self._stable_identity_block(model) + (
            self._volatile_identity_block(model)
        )
        prompt = "\n".join(parts)

        # Hard ceiling — trim from the end if over budget. Volatile slab
        # sits at the end, so it drops first; the closing is its last
        # entry, so it drops before any volatile content.
        ceiling = self.MAX_IDENTITY_PROMPT_CHARS
        while len(prompt) > ceiling and len(parts) > 1:
            parts.pop()
            prompt = "\n".join(parts)

        # Last-resort truncation. The loop stops with ``len(parts) == 1``
        # (the un-poppable header), so a header line that alone exceeds
        # the ceiling would otherwise return over budget. Slicing here
        # makes the cap unconditional; a sub-ceiling prompt is sliced by
        # its own length — a no-op.
        prompt = prompt[:ceiling]

        return prompt


def _validate_default_limitations(
    catalog: tuple[tuple[str, str], ...] = DEFAULT_LIMITATIONS,
) -> None:
    """Enforce the round-trip identity precondition on catalog keys.

    ``load_self_model`` truncates every string in
    ``SelfModel.seeded_limitations`` to ``Consciousness.MAX_ITEM_CHARS``,
    but ``save_self_model`` special-cases that field to skip truncation
    so the system-managed catalog survives the
    :data:`MAX_LIST_ITEMS` archive path (dropping a seed key would
    re-fire ``seed_default_limitations`` on the next startup). When a
    catalog key exceeds the per-item cap, a save→load round-trip
    silently shrinks it, the seeder no longer recognises it as already
    applied, and the mutate→archive→re-seed loop the save-side
    special-case was written to prevent fires anyway. Enforce the
    precondition at import time so a future maintainer adding an
    oversized catalog key trips a loud failure instead.
    """
    cap = Consciousness.MAX_ITEM_CHARS
    for key, _ in catalog:
        if len(key) > cap:
            raise ValueError(
                f"DEFAULT_LIMITATIONS key {key!r} exceeds "
                f"Consciousness.MAX_ITEM_CHARS ({cap}); load_self_model "
                "truncates longer keys and re-seeding will loop.",
            )


def _validate_provenance_prefixes_survive_truncation(
    cap: int = Consciousness.MAX_ITEM_CHARS,
    prefixes: tuple[str, ...] = (
        Consciousness._AUDIT_PREFIX,
        Consciousness._LIMITATION_PREFIX,
    ),
) -> None:
    """Enforce that the per-item char cap never truncates a provenance prefix.

    ``_enforce_open_questions_caps`` sorts ``open_questions`` into audit /
    limitation / other buckets by matching a *prefix* on the untruncated
    value, then applies :meth:`Consciousness._truncate_item` last. On the
    load path that truncation runs on entries a prior save already
    truncated, so if :data:`Consciousness.MAX_ITEM_CHARS` were shrunk below
    a prefix length — or ``_truncate_item`` stopped preserving leading
    characters — a stored ``[audit] …`` / ``[limitation] …`` entry would be
    silently reclassified into the ``other`` bucket, escaping its sub-cap.
    Exercise ``_truncate_item`` directly (not an arithmetic
    ``cap - 1 >= len(prefix)`` check) so both failure arms trip loudly at
    import time. Mirrors :func:`_validate_default_limitations`.
    """
    for prefix in prefixes:
        synthetic = prefix + "x" * (cap + 1)
        truncated = Consciousness._truncate_item(synthetic, cap)
        if not truncated.startswith(prefix):
            raise ValueError(
                "Consciousness._truncate_item shortens a provenance-prefixed "
                f"entry below its prefix {prefix!r} at "
                f"Consciousness.MAX_ITEM_CHARS ({cap}); "
                "_enforce_open_questions_caps would mis-bucket a truncated "
                "audit/limitation entry on the load path.",
            )


_validate_default_limitations()
_validate_provenance_prefixes_survive_truncation()

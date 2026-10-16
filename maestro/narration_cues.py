"""Shared narration-cue vocabulary — one definition site per prose cue.

Maestro's grounding pipeline reads its own task narration — resolution keywords
in a ``result_summary``, "escalating #NNNN" prose, and cited commit SHAs — and
converts what it senses into record-time downgrades, directives, and handoffs.
The prose-matching primitives that drive this sensing are collected here so each
cue has exactly one definition site: a keyword change moves every sensor together
instead of forcing a parallel edit in :mod:`maestro.outcome_grounding` and
:mod:`maestro.audit_checks`, which previously each carried their own copy.

This is a leaf module: it imports only :mod:`re` and nothing from the ``maestro``
package, so any consumer can import it without risking an import cycle.

Two committed-SHA regexes live here side by side. :data:`COMMITTED_SHA_RE`
(narrow) requires the SHA to follow its commit cue immediately;
:data:`SELF_COMMITTED_SHA_RE` (wide) additionally accepts a literal ``as``
connector and an optional backtick before the SHA (the ``committed as `<sha>```
evidence shape). The wide regex is a strict superset of the narrow one: every
string the narrow regex matches, the wide regex also matches, because the extra
tokens the wide one accepts are all optional. Keeping both here co-locates that
relationship, and ``tests/test_narration_cues.py`` locks the narrow⊆wide
containment down so an independent edit to either can no longer split their
coverage silently.
"""

from __future__ import annotations

import re

#: Word-boundary resolution keywords whose presence in a ``success`` summary is a
#: landing claim. Matched case-insensitively via :data:`RESOLUTION_RE` against the
#: recorded ``result_summary``. Single source for the record-time prose scrub
#: (:func:`maestro.outcome_grounding.bind_ci_failing_verdict_to_red_read`) so it
#: keys on identical prose. The word boundary is what keeps ``fixed`` firing while
#: ``prefixed`` / ``unfixed`` do not, and ``done`` firing while ``abandoned`` /
#: ``condone`` do not — an honest "not yet fixed" is never mis-read (the negation
#: lives in adjacent words, not inside the keyword token).
RESOLUTION_KEYWORDS = ("resolved", "landed", "merged", "fixed", "done")

#: Whole-word, case-insensitive alternation over :data:`RESOLUTION_KEYWORDS`.
RESOLUTION_RE = re.compile(
    r"\b(?:" + "|".join(RESOLUTION_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# ── Protected admission phrases ──────────────────────────────────────────
# Several downstream sensors admit a task record on a case-insensitive prose
# *substring* that itself contains a :data:`RESOLUTION_KEYWORDS` word — e.g.
# rule 14's on-branch handoff match ("resolved on branch") and the repetition
# gate's no-op match ("already resolved"). Those sensors read the *scrubbed*
# summary that :func:`maestro.outcome_grounding._scrub_resolution_keywords`
# persisted, and the scrub rewrites every bare resolution keyword to a neutral
# token — so a phrase like "resolved on branch" becomes "seen on branch" and the
# sensor goes blind (#2970). The phrases live here, at the one leaf definition
# site both the scrub and its consumers can import without a cycle, so the scrub
# placeholder-protects exactly what the sensors admit on. Each group below is the
# keyword-bearing subset of one consumer vocabulary; the consumer composes its
# tuple from the group (keeping its keyword-free entries and Marker members
# local), and :data:`PROTECTED_ADMISSION_PHRASES` is the deduplicated union the
# scrub and the ``tests/test_narration_cues.py`` structural guard key on. One
# group — :data:`CAUSED_LANDING_PHRASES` — is deliberately left OUT of that union:
# for its sole consumer, keeping the phrase readable through the scrub would be an
# over-claim, not a rescue (see its note below). The structural guard exempts that
# group so the invariant "every keyword-bearing sensor phrase is registered" holds
# for exactly the phrases whose survival keeps a sensor honest.

#: The "already resolved" no-op admission, shared by the no-op and settled
#: sensors — defined once so both compose from the same literal.
_ALREADY_RESOLVED = "already resolved"

#: The "already merged" landing admission, shared by the settled-verdict and
#: confirmed-landing sensors — defined once so both compose from the same literal.
_ALREADY_MERGED = "already merged"

#: Keyword-bearing subset of
#: :data:`maestro.audit_checks_phantom._ON_BRANCH_HANDOFF_MARKERS` (rule 14): an own
#: commit narrated as resolved on its branch, not yet confirmed on ``origin/main``.
ON_BRANCH_RESOLVED_PHRASES: tuple[str, ...] = (
    "resolved on branch",
    "resolved on this branch",
)

#: Keyword-bearing subset of the no-op vocabularies
#: (:data:`maestro.audit_checks._NOOP_TASK_PATTERNS`,
#: :data:`maestro.remediation_tracker.NOOP_INDICATORS`).
NOOP_ADMISSION_PHRASES: tuple[str, ...] = (_ALREADY_RESOLVED,)

#: Keyword-bearing subset of
#: :data:`maestro.audit_checks._SETTLED_VERDICT_PATTERNS`.
SETTLED_VERDICT_LANDING_PHRASES: tuple[str, ...] = (
    _ALREADY_MERGED,
    "merged into main",
    "merged to main",
    "pr merged",
    _ALREADY_RESOLVED,
)

#: Keyword-bearing subset of
#: :data:`maestro.remediation_tracker._CONFIRMED_LANDING_MARKERS` (another party
#: already landed the cure).
CONFIRMED_LANDING_PHRASES: tuple[str, ...] = (
    _ALREADY_MERGED,
    "already landed",
    "was already merged",
    "merged before i",
    "merged by thomas",
    "merged by someone",
    "confirmed merged",
    "verified merged",
    "git shows merged",
    "git-merge-truth shows merged",
)

#: Keyword-bearing subset of
#: :data:`maestro.remediation_tracker._CAUSED_LANDING_MARKERS` (this lap moved
#: the cure). **Deliberately excluded from** :data:`PROTECTED_ADMISSION_PHRASES`
#: (#2970 review): its sole consumer,
#: :func:`~maestro.remediation_tracker.classify_landing_attribution`, is only ever
#: read on *scrubbed* text in the one place the scrub runs — the red-read-bind
#: costume slice, where the lap committed-and-pushed nothing
#: (:func:`~maestro.outcome_grounding.bind_ci_failing_verdict_to_red_read` fires
#: with ``addressed_red`` ``False``). Were these phrases protected, that costume
#: would read ``CAUSED`` the moment §5d's landing authority
#: (:func:`~maestro.outcome_authority.decide_outcome`) flips it ``partial`` →
#: ``success`` on a raced merge — inflating the very confirmed-isn't-caused
#: over-claim :func:`~maestro.remediation_tracker.measure_confirmed_landing_rate`
#: exists to expose. Left scrubbed, the costume reads ``NA`` — the honest,
#: pre-#2970 reading. A *legitimate* CAUSED record is never scrubbed (the bind
#: does not fire when the lap addressed the red), so its phrase survives on the
#: un-scrubbed path untouched. Kept here (not inlined) so
#: ``_CAUSED_LANDING_MARKERS`` still composes from one definition site and the
#: guard's exemption names the group.
CAUSED_LANDING_PHRASES: tuple[str, ...] = (
    "merged origin/main",
    "merged main into",
    "merged origin/main into",
    "resolved the conflict",
    "resolved conflicts",
    "resolved merge conflict",
    "resolved the merge conflict",
    "fixed the failing gate",
    "fixed the red gate",
)

#: Deduplicated union of every keyword-bearing admission phrase a downstream
#: sensor matches on **whose survival keeps that sensor honest**.
#: :func:`maestro.outcome_grounding._scrub_resolution_keywords` placeholder-protects
#: each entry present in the text (case-insensitively) so the scrub can neutralise a
#: free-floating resolution keyword without stripping the multi-word phrase the
#: sensors read. ``dict.fromkeys`` dedups (``already merged`` / ``already resolved``
#: each appear in two groups) while preserving first-seen order.
#: :data:`CAUSED_LANDING_PHRASES` is intentionally NOT unioned in — protecting it
#: would let a red-read-bind costume read ``CAUSED`` (see that group's note), so
#: leaving it scrubbed is the correct, conservative reading rather than a blinded
#: sensor.
PROTECTED_ADMISSION_PHRASES: tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            *ON_BRANCH_RESOLVED_PHRASES,
            *NOOP_ADMISSION_PHRASES,
            *SETTLED_VERDICT_LANDING_PHRASES,
            *CONFIRMED_LANDING_PHRASES,
        ),
    ),
)

#: Case-insensitive substring matched against a ``partial`` ``result_summary`` to
#: detect escalation prose. The ``escalat`` stem catches both ``escalating`` and
#: ``escalation`` while never matching unrelated words.
ESCALATION_PROSE_MARKER = "escalat"

#: Captures a committed git SHA only when it sits in an explicit commit context.
#: The single capture group is a 7-40 hex run with at least one hex *letter*
#: (``a`` to ``f``) — the letter requirement (positive lookahead) keeps it from
#: matching a bare decimal run (a 7+ digit issue/PR number or timestamp), and the
#: trailing ``\b`` stops it slicing a longer alphanumeric token. Crucially the SHA
#: must be preceded by a commit cue (``commit``/``committed``/``commits``/``sha``
#: then whitespace, or ``@`` with optional whitespace): without this anchor the old
#: whole-summary scan mis-bound any stray hex-only word or identifier in free-form
#: prose (e.g. ``defaced``, ``feedface``, ``deadbeef`` in ``1a2b3c4-deadbeef``) as a
#: committed SHA. A phantom SHA can never become an ancestor of ``origin/main``, so
#: it would re-fire every cycle — the exact self-perpetuating false alarm rule 14
#: exists to prevent. One *bounded* interjection is allowed between a word cue and
#: the SHA — the on-branch phrasing ``on (this|the) branch`` — so the handoff shapes
#: :data:`maestro.audit_checks_phantom._ON_BRANCH_HANDOFF_MARKERS` actually names ("committed
#: on this branch ac48a769") capture instead of slipping through as a false
#: negative; the phrase is fixed-vocabulary, so it widens the cue without
#: re-admitting arbitrary hex words (a bare ``on this branch deadbeef`` with no cue
#: still matches nothing). The SHA is the only capture group, so ``findall`` returns
#: the SHA strings directly and the call site is unchanged; the cue forms match
#: Maestro's authored handoff style ("committed ac48a769", "committed on this branch
#: ac48a769", "@ac48a769").
COMMITTED_SHA_RE = re.compile(
    r"(?:(?:\bcommit(?:ted|s)?\b|\bsha\b)\s+(?:on\s+(?:this\s+|the\s+)?branch\s+)?|@\s*)"
    r"((?=[0-9a-f]*[a-f])[0-9a-f]{7,40})\b"
)

#: Cue family for an *own* committed SHA (FM4, #2848). A **dedicated** widening of
#: the co-located narrow :data:`COMMITTED_SHA_RE` by exactly one literal ``as``
#: connector and an optional backtick before the SHA. The canonical FM4 evidence
#: (task ``9e77f0f2b36f``) records its SHA as ``committed as `5971202d` ``; the
#: narrow regex requires the SHA to follow the cue immediately, so the ``as``
#: between ``committed`` and the SHA breaks its match
#: (``COMMITTED_SHA_RE.findall("committed as `5971202d`") == []``) — reusing it
#: would silently never fire on the real evidence. Kept *dedicated* rather than
#: widening the narrow regex so rule 14's documented anti-false-alarm posture is
#: untouched; the only new false-positive surface is ``committed as <hex-word>``, a
#: strictly bounded widening of the narrow regex's already-accepted
#: ``committed <hex-word>``. The lookahead ``(?=[0-9a-f]*[a-f])`` requires at least
#: one hex letter (``a`` to ``f``) so a pure-decimal run (e.g. an issue number) is
#: never read as a SHA.
SELF_COMMITTED_SHA_RE = re.compile(
    r"(?:(?:\bcommit(?:ted|s)?\b|\bsha\b)"
    r"\s+(?:as\s+)?(?:on\s+(?:this\s+|the\s+)?branch\s+)?|@\s*)"
    r"`?((?=[0-9a-f]*[a-f])[0-9a-f]{7,40})\b"
)

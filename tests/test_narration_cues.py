"""Tests for the shared narration-cue vocabulary (``maestro.narration_cues``).

This leaf module is the single definition site for the prose cues Maestro's
grounding pipeline reads back from its own task narration. The tests are pure
regex/constant characterization plus two structural invariants: the narrow⊆wide
committed-SHA containment, and the import-site identity that proves both consumers
(``outcome_grounding`` and ``audit_checks``) key on the one shared object. No
``mem_fs`` or mocked oracles are needed — these cues touch no filesystem, network,
or subprocess.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import ModuleType

import pytest

from maestro import (
    audit_checks,
    audit_checks_calibration,
    audit_checks_phantom,
    audit_checks_settlement,
    narration_cues,
    outcome_grounding,
    remediation_tracker,
)
from maestro.pr_remediation_state import Marker

# ── RESOLUTION_RE: word-boundary resolution keywords ────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "resolved",
        "landed",
        "merged",
        "fixed",
        "done",
        "merged #42",
        "RESOLVED the conflict",  # case-insensitive
    ],
)
def test_resolution_re_matches_whole_word_keywords(text: str) -> None:
    """Each keyword fires as a whole word, case-insensitively."""
    assert narration_cues.RESOLUTION_RE.search(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "undone",  # 'done' only as a suffix
        "prefixed",  # 'fixed' only mid-token
        "unfixed",  # 'fixed' only as a suffix
        "abandoned",  # not a whole-word 'done'
        "condone",  # not a whole-word 'done'
    ],
)
def test_resolution_re_rejects_sub_token_matches(text: str) -> None:
    """A keyword embedded inside a longer token is not a match — the word
    boundary keeps ``prefixed`` / ``unfixed`` / ``undone`` from firing."""
    assert narration_cues.RESOLUTION_RE.search(text) is None


def test_resolution_re_matches_whole_word_even_under_negation() -> None:
    """The lexical matcher fires on a whole-word ``fixed`` even when the prose
    negates it: the negation lives in adjacent words, not inside the keyword
    token, so this is a documented characterization, not a defect."""
    assert narration_cues.RESOLUTION_RE.search("not yet fixed") is not None


# ── ESCALATION_PROSE_MARKER: substring stem ─────────────────────────────


@pytest.mark.parametrize("text", ["escalating", "escalation", "Escalating now", "escalated"])
def test_escalation_marker_matches_escalation_prose(text: str) -> None:
    """The ``escalat`` stem catches the escalation word family."""
    assert narration_cues.ESCALATION_PROSE_MARKER in text.lower()


@pytest.mark.parametrize("text", ["calibrate", "scalar", "the plan progressed"])
def test_escalation_marker_ignores_unrelated_words(text: str) -> None:
    """The stem never matches unrelated words."""
    assert narration_cues.ESCALATION_PROSE_MARKER not in text.lower()


# ── committed-SHA cues: narrow, wide, and narrow⊆wide containment ────────

#: Corpus of commit-cue phrasings and non-cue hex words exercised by both
#: regexes. Kept as one list so the containment test sees every interesting case.
_SHA_CUE_CORPUS = [
    "committed ac48a769",
    "committed on this branch ac48a769",
    "@ac48a769",
    "sha ac48a769",
    "committed as `5971202d`",
    "committed as deadbeef",
    "the build deadbeef was green",  # cue-less hex word — no SHA
    "commit 1234567",  # pure decimal — lookahead rejects
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("committed ac48a769", {"ac48a769"}),
        ("committed on this branch ac48a769", {"ac48a769"}),
        ("@ac48a769", {"ac48a769"}),
        ("sha ac48a769", {"ac48a769"}),
        ("committed as `5971202d`", set()),  # narrow rejects the ``as`` connector
        ("the build deadbeef was green", set()),  # no commit cue
        ("commit 1234567", set()),  # no hex letter — the lookahead rejects it
    ],
)
def test_narrow_committed_sha_re(text: str, expected: set[str]) -> None:
    """The narrow regex binds a SHA only on an immediate commit cue."""
    assert set(narration_cues.COMMITTED_SHA_RE.findall(text.lower())) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("committed ac48a769", {"ac48a769"}),
        ("committed as `5971202d`", {"5971202d"}),  # wide accepts the ``as`` connector
        ("committed as deadbeef", {"deadbeef"}),
        ("the build deadbeef was green", set()),
        ("commit 1234567", set()),
    ],
)
def test_wide_self_committed_sha_re(text: str, expected: set[str]) -> None:
    """The wide regex additionally accepts the ``committed as `<sha>``` shape."""
    assert set(narration_cues.SELF_COMMITTED_SHA_RE.findall(text.lower())) == expected


@pytest.mark.parametrize("text", _SHA_CUE_CORPUS)
def test_narrow_is_subset_of_wide(text: str) -> None:
    """Every narrow committed-SHA match is also a wide match: the wide regex is a
    strict superset, so an independent edit can never split their coverage."""
    lowered = text.lower()
    narrow = set(narration_cues.COMMITTED_SHA_RE.findall(lowered))
    wide = set(narration_cues.SELF_COMMITTED_SHA_RE.findall(lowered))
    assert narrow <= wide


# ── import-site identity: both consumers key on the one shared object ────


def test_outcome_grounding_keys_on_shared_cues() -> None:
    """``outcome_grounding`` binds the shared resolution and self-committed-SHA
    objects, not private copies."""
    assert outcome_grounding._RESOLUTION_RE is narration_cues.RESOLUTION_RE
    assert outcome_grounding._SELF_COMMITTED_SHA_RE is narration_cues.SELF_COMMITTED_SHA_RE


def test_audit_checks_keys_on_shared_cues() -> None:
    """``audit_checks_phantom`` (the phantom rules split out of ``audit_checks``)
    binds the shared narrow committed-SHA regex and escalation stem."""
    assert audit_checks_phantom._COMMITTED_SHA_RE is narration_cues.COMMITTED_SHA_RE
    assert (
        audit_checks_phantom._ESCALATION_PROSE_MARKER
        is narration_cues.ESCALATION_PROSE_MARKER
    )


# ── #2970 structural guard: keyword-bearing sensor prose must be registered ──


#: The one keyword-bearing vocabulary the scrub is *meant* to neutralise (#2970
#: review r1). ``classify_landing_attribution``'s CAUSED markers are read on
#: scrubbed text only in the red-read-bind costume slice, where a survived phrase
#: would over-claim CAUSED once §5d flips the record to ``success`` — so leaving
#: them scrubbed is correct, and the registration guard below exempts exactly them.
_DELIBERATELY_SCRUBBED: frozenset[str] = frozenset(
    narration_cues.CAUSED_LANDING_PHRASES,
)


def _unregistered_keyword_phrases(
    namespace: Mapping[str, object],
) -> list[tuple[str, str]]:
    """Return ``(const_name, phrase)`` for every keyword-bearing prose entry in a
    module ``namespace`` that is absent from
    :data:`~maestro.narration_cues.PROTECTED_ADMISSION_PHRASES` — the #2970
    registration guard's core walk, factored out so the live-module guard and the
    Marker+string mixed-tuple canary drive identical logic.

    Only non-empty ``tuple`` constants whose entries are all ``str`` or ``Marker``
    are inspected. ``Marker`` is a ``StrEnum``, so admitting ``(str, Marker)``
    (rather than plain ``str``) is what keeps a Marker-bearing vocabulary such as
    :data:`maestro.audit_checks_phantom._ON_BRANCH_HANDOFF_MARKERS` from being skipped
    wholesale — and it makes the inner ``Marker`` pass-over below manifestly
    reachable rather than reliant on that ``StrEnum`` coincidence. Within an
    inspected tuple, ``Marker`` members are protected by the scrub's own marker loop
    and the :data:`_DELIBERATELY_SCRUBBED` CAUSED phrases are exempt (r1), so both
    are passed over rather than flagged.
    """
    offenders: list[tuple[str, str]] = []
    for name, value in namespace.items():
        if not isinstance(value, tuple) or not value:
            continue
        if not all(isinstance(entry, (str, Marker)) for entry in value):
            continue
        for entry in value:
            if isinstance(entry, Marker):
                continue  # protected via the scrub's Marker loop, not the phrase set
            if entry in _DELIBERATELY_SCRUBBED:
                continue  # CAUSED phrases are meant to be scrubbed (r1)
            if narration_cues.RESOLUTION_RE.search(entry) and (
                entry not in narration_cues.PROTECTED_ADMISSION_PHRASES
            ):
                offenders.append((name, entry))
    return offenders


@pytest.mark.parametrize(
    "module",
    [
        audit_checks,
        audit_checks_calibration,
        audit_checks_phantom,
        audit_checks_settlement,
        remediation_tracker,
    ],
    ids=[
        "audit_checks",
        "audit_checks_calibration",
        "audit_checks_phantom",
        "audit_checks_settlement",
        "remediation_tracker",
    ],
)
def test_keyword_bearing_vocab_entries_are_registered(module: ModuleType) -> None:
    """Every keyword-bearing prose entry in a sensor vocabulary is registered (#2970).

    Walks each ``tuple`` module constant in the ``audit_checks*`` family /
    ``remediation_tracker``
    and, for every plain-string entry carrying a resolution keyword (``Marker``
    members are protected separately by the scrub's marker loop, so they are skipped;
    the :data:`_DELIBERATELY_SCRUBBED` CAUSED phrases are exempt — see r1), asserts it
    is a member of :data:`~maestro.narration_cues.PROTECTED_ADMISSION_PHRASES`. An
    unregistered keyword-bearing phrase would be silently rewritten to the neutral
    token by ``_scrub_resolution_keywords`` before the sensor that admits on it reads
    the persisted text — the exact blindness this shared vocabulary fixes — so a new
    such phrase fails this guard loudly instead of losing its input unnoticed.
    """
    offenders = _unregistered_keyword_phrases(vars(module))
    assert offenders == [], (
        f"{module.__name__} contains keyword-bearing prose absent from "
        f"PROTECTED_ADMISSION_PHRASES: {offenders!r} — the record-time scrub would "
        f"blind the sensor admitting on it. Register each in the matching group in "
        f"maestro/narration_cues.py."
    )


def test_registration_guard_inspects_marker_bearing_vocab() -> None:
    """The #2970 guard reaches *into* Marker+string mixed vocabularies (review r1).

    :data:`maestro.audit_checks_phantom._ON_BRANCH_HANDOFF_MARKERS` — the rule-14 on-branch
    handoff vocabulary this PR exists to protect — deliberately mixes ``Marker``
    members with the keyword-bearing string phrases the scrub must preserve. The
    guard's ``isinstance(entry, (str, Marker))`` admission is what lets it inspect
    such a tuple instead of skipping it wholesale, so a keyword-bearing phrase dropped
    straight into a Marker-bearing tuple — rather than registered via
    ``narration_cues`` — is still reported. This locks that reach: were the admission
    narrowed to plain ``str`` while ``Marker`` stopped subclassing ``str``, the
    synthetic tuple below would be skipped and the raw phrase would slip through,
    reopening #2970.
    """
    # The real rule-14 tuple genuinely mixes Marker members with plain-string phrases.
    real = audit_checks_phantom._ON_BRANCH_HANDOFF_MARKERS
    assert any(isinstance(entry, Marker) for entry in real)
    assert any(not isinstance(entry, Marker) for entry in real)
    # A Marker-bearing tuple carrying an unregistered keyword phrase is reported; its
    # keyword-free sibling and its Marker member are correctly passed over.
    synthetic: dict[str, object] = {
        "_FAKE_HANDOFF_MARKERS": (
            Marker.PUSH_UNCONFIRMED,
            "committed on this branch",
            "resolved on the feature branch",
        ),
    }
    assert _unregistered_keyword_phrases(synthetic) == [
        ("_FAKE_HANDOFF_MARKERS", "resolved on the feature branch"),
    ]


def test_caused_landing_phrases_are_deliberately_unprotected() -> None:
    """CAUSED landing phrases must stay OUT of the protected set (#2970 r1).

    Protecting them would let a red-read-bind costume record — the lap
    committed-and-pushed nothing — read ``CAUSED`` once §5d's landing authority
    flips it ``partial`` → ``success``, inflating the confirmed-isn't-caused
    over-claim ``measure_confirmed_landing_rate`` exists to expose. This locks the
    deliberate exclusion so a future edit cannot silently re-protect them; the
    non-empty check keeps the assertion from passing vacuously.
    """
    assert narration_cues.CAUSED_LANDING_PHRASES
    for phrase in narration_cues.CAUSED_LANDING_PHRASES:
        assert phrase not in narration_cues.PROTECTED_ADMISSION_PHRASES

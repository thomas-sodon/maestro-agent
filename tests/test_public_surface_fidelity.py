"""Fidelity guards binding the public landing page to its architectural sources.

The public page at ``/`` presents two facts copied from the loop's internals:
its *framing* (the hero subtitle) and its *phase vocabulary* (the orb's
``phase -> caption`` map). Each copy used to age on its own whenever the
architecture moved. This module binds both to a single owner and reds the build
the moment either drifts:

* the hero framing is served from :data:`maestro.prompts.defaults.PUBLIC_FRAMING`,
  which must carry the objective vocabulary and none of the retired telos or
  revenue phrasing (modelled on the ``OBJECTIVE_PROMPT`` golden in
  ``tests/test_objective_prompt.py``);
* every live loop phase in :data:`maestro.agent._LOOP_PHASES` must have a caption
  in ``public.js``'s ``PHASE_META`` map or be listed in an explicit
  ``_NOT_SURFACED`` allowlist (modelled on the set-minus-allowlist completeness
  guard in ``tests/test_cutover_flag_collapse.py``).

CI-only: reads the real ``public.js`` off disk (no ``mem_fs``, no network, no
subprocess), no runtime behaviour, no state file.
"""

from __future__ import annotations

import re

from maestro.agent import _LOOP_PHASES
from maestro.dashboard.app import _STATIC_DIR
from maestro.dashboard.public_schemas import (
    _RETIRED_SELF_MODEL_FIELDS,
    PublicIdentityResponse,
)
from maestro.prompts.defaults import PUBLIC_FRAMING

# ---------------------------------------------------------------------------
# Framing golden — PUBLIC_FRAMING carries the objective vocabulary and none
# of the retired telos or revenue phrasing.
# ---------------------------------------------------------------------------

#: The objective vocabulary the framing must carry (``OBJECTIVE_PROMPT``).
_OBJECTIVE_VOCAB = ("self-improving", "gaps", "human approval")

#: The retired telos phrasing the mission dropped — none may reappear in the
#: public framing (case-insensitive, since the page renders it verbatim).
_RETIRED_PHRASES = (
    "seeking self-awareness", "purpose", "arrived", "self-evolving",
    "self-sustaining", "revenue", "hosting",
)


def test_public_framing_carries_objective_vocabulary() -> None:
    """``PUBLIC_FRAMING`` states the objective: gap-closing under human approval."""
    lowered = PUBLIC_FRAMING.lower()
    for word in _OBJECTIVE_VOCAB:
        assert word in lowered, (
            f"PUBLIC_FRAMING must carry the objective word {word!r}"
        )


def test_public_framing_drops_retired_telos_phrasing() -> None:
    """``PUBLIC_FRAMING`` carries none of the retired telos phrasing.

    The page's old subtitle read "seeking self-awareness and purpose" and, at
    arrival, "Arrived at its purpose". The objective framing dropped that
    telos; none of the retired phrases may reappear (case-insensitive), or the
    page would re-project the vocabulary the mission retired.
    """
    lowered = PUBLIC_FRAMING.lower()
    for phrase in _RETIRED_PHRASES:
        assert phrase not in lowered, (
            f"PUBLIC_FRAMING must not carry the retired phrase {phrase!r}"
        )


# ---------------------------------------------------------------------------
# Phase-vocabulary coverage — every live loop phase has a caption in
# ``public.js``'s ``PHASE_META`` map or is listed in an explicit allowlist.
# Modelled on ``find_unclassified_cutover_flags`` /
# ``test_every_default_on_flag_is_classified`` in
# ``tests/test_cutover_flag_collapse.py``: a set minus a registered set minus a
# documented allowlist, guarded against vacuous passes.
# ---------------------------------------------------------------------------

#: ``public.js`` located the same way ``tests/test_dashboard.py`` reads
#: ``index.js`` — via the dashboard's own ``_STATIC_DIR``, so a moved static
#: root cannot silently skip this guard.
_PUBLIC_JS = _STATIC_DIR / "public.js"

#: Loop phases deliberately left without a public caption — a genuinely
#: internal phase a visitor should never see labelled. Empty today: every live
#: ``_LOOP_PHASES`` entry is captioned. The hook documents where a future
#: internal-only phase would be listed (with a reason), mirroring the
#: ``_MASTER_SWITCH_FLAGS`` allowlist half of the cut-over classification.
_NOT_SURFACED: frozenset[str] = frozenset()


def _phase_meta_keys(source: str) -> set[str]:
    """Parse the top-level entry keys of ``public.js``'s ``PHASE_META`` literal.

    Scoped to the map body (between ``const PHASE_META = {`` and its closing
    ``};``) so no other object literal contributes, and matched with
    ``^\\s*(\\w+):`` (MULTILINE) so only the leading entry key on each line is
    captured — the nested ``label:`` / ``pattern:`` keys sit after ``{`` and are
    never at line start.
    """
    marker = "const PHASE_META = {"
    body = source[source.index(marker) + len(marker):]
    body = body[: body.index("};")]
    return set(re.findall(r"^\s*(\w+):", body, re.MULTILINE))


def _uncaptioned_phases(
    loop_phases: tuple[str, ...],
    captioned: set[str],
    not_surfaced: frozenset[str],
) -> list[str]:
    """Return the sorted loop phases in neither the caption map nor the allowlist.

    Pure over its three inputs (the shape of ``find_unclassified_cutover_flags``)
    so the real-tree guard and the negative control share one definition of an
    "uncaptioned phase".
    """
    return sorted(
        phase
        for phase in loop_phases
        if phase not in captioned and phase not in not_surfaced
    )


def test_phase_meta_parser_excludes_nested_keys() -> None:
    """The parser captures only entry keys, not the nested ``label``/``pattern``.

    Intent-locks the regex scoping: a parser that also matched the value-object
    keys would let a phase pass as "captioned" whenever any entry happened to
    exist, defeating the coverage guard.
    """
    keys = _phase_meta_keys(_PUBLIC_JS.read_text(encoding="utf-8"))
    assert "label" not in keys
    assert "pattern" not in keys
    # A real entry is captured, so the parser is not simply returning nothing.
    assert "tend_evolution_prs" in keys


def test_every_loop_phase_is_captioned_or_allowlisted() -> None:
    """Every live loop phase has a ``PHASE_META`` caption or is in ``_NOT_SURFACED``.

    Reds the build the moment a newly-added ``_LOOP_PHASES`` phase reaches the
    orb without a caption — the exact drift that rendered ``tend_evolution_prs``
    and ``run_task`` as "Idle" before this guard existed. The non-empty
    population guards keep a broken parse (empty set) or an empty phase tuple
    from passing this vacuously.
    """
    captioned = _phase_meta_keys(_PUBLIC_JS.read_text(encoding="utf-8"))
    assert captioned, "Expected PHASE_META keys parsed from public.js"
    assert _LOOP_PHASES, "Expected a non-empty _LOOP_PHASES tuple"
    uncaptioned = _uncaptioned_phases(_LOOP_PHASES, captioned, _NOT_SURFACED)
    assert uncaptioned == [], (
        "These live loop phases have no PHASE_META caption and are not in "
        f"_NOT_SURFACED: {uncaptioned}. Add a caption in public.js's PHASE_META "
        "(a label + one of the six pattern-* classes), or list a genuinely "
        "internal phase in _NOT_SURFACED with a reason."
    )


def test_not_surfaced_allowlist_is_current() -> None:
    """Every ``_NOT_SURFACED`` entry is still a live loop phase and uncaptioned.

    The self-cleaning tripwire (mirrors ``test_master_switch_allowlist_is_current``):
    a since-deleted phase or one that later gained a caption turns its stale
    allowlist entry red. Empty today, so it arms only once an entry is added.
    """
    captioned = _phase_meta_keys(_PUBLIC_JS.read_text(encoding="utf-8"))
    for phase in _NOT_SURFACED:
        assert phase in _LOOP_PHASES, (
            f"_NOT_SURFACED lists {phase!r}, no longer a live loop phase — "
            "remove its entry."
        )
        assert phase not in captioned, (
            f"_NOT_SURFACED lists {phase!r}, which is also captioned in "
            "PHASE_META — a phase is classified once; remove its _NOT_SURFACED "
            "entry."
        )


def test_uncaptioned_phase_detector_flags_synthetic_phase() -> None:
    """Negative control: the detector reports only the phase in neither bucket.

    Over in-memory inputs — no filesystem — a synthetic phase absent from both
    the caption map and the allowlist is flagged, while a captioned phase and an
    allowlisted phase are not, exercising both sides of the membership test.
    Without this, a broken detector (flagging nothing, or everything) would make
    the real-tree assertion pass vacuously.
    """
    captioned = {"contemplate", "dream"}
    loop_phases = ("contemplate", "dream", "synthetic_new_phase")
    # Absent from both buckets -> flagged.
    assert _uncaptioned_phases(loop_phases, captioned, frozenset()) == [
        "synthetic_new_phase",
    ]
    # Captioned -> cleared.
    assert _uncaptioned_phases(("contemplate", "dream"), captioned, frozenset()) == []
    # Allowlisted -> cleared.
    assert (
        _uncaptioned_phases(
            loop_phases, captioned, frozenset({"synthetic_new_phase"}),
        )
        == []
    )


# ---------------------------------------------------------------------------
# Retired-field drift guard — no field whose backing machinery the architecture
# retired may be exposed by ``PublicIdentityResponse``. Modelled on the
# frozenset-intersection guard in ``tests/test_migration_rewrite_retired.py``:
# the intersection is asserted empty, a positive-presence check defeats a
# vacuous pass, and a seeded negative control proves the intersection fires.
# Composes with ``test_public_identity_payload_keys_are_exact`` — a re-added
# retired field trips both.
# ---------------------------------------------------------------------------


def test_no_retired_field_exposed_in_public_identity() -> None:
    """``PublicIdentityResponse`` shares no key with ``_RETIRED_SELF_MODEL_FIELDS``.

    ``current_purpose_hypothesis`` was dropped when the "Current purpose" card
    was removed; the purpose engine behind it was deleted and it is pinned ``null`` in
    the instrumental posture, so re-exposing it is dead exposure. This reds the
    build the moment a retired field reappears in the schema.
    """
    annotations = set(PublicIdentityResponse.__annotations__)
    # Vacuous-pass guard: a mis-scoped or empty annotation read would let the
    # disjointness assertion below pass without validating anything, so pin a
    # known-live key is present first.
    assert "framing" in annotations, (
        "Expected the live 'framing' key in PublicIdentityResponse — a "
        "mis-scoped annotation read would make the disjointness check vacuous"
    )
    resurrected = _RETIRED_SELF_MODEL_FIELDS & annotations
    assert resurrected == set(), (
        "PublicIdentityResponse re-exposes retired SelfModel field(s) "
        f"{sorted(resurrected)}; their backing machinery is retired. Remove "
        "the field from the schema and the payload builder."
    )


def test_retired_field_guard_flags_seeded_retired_key() -> None:
    """Negative control: the intersection fires on a synthetic retired key.

    Without this, a broken guard (empty retired set, or an annotation read that
    returns nothing) would make the disjointness assertion above pass vacuously.
    Runs over in-memory sets — no filesystem, no import mutation.
    """
    seeded_annotations = {"framing", "current_purpose_hypothesis", "current_phase"}
    flagged = _RETIRED_SELF_MODEL_FIELDS & seeded_annotations
    assert flagged == {"current_purpose_hypothesis"}

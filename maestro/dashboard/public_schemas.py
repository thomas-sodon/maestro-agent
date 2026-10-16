"""Response schemas for ``/api/public/*`` endpoints.

These :class:`TypedDict` shapes are the single source of truth for what
fields leak to anonymous visitors.  Each endpoint constructs its payload
as one of these shapes and a test asserts
``set(handler_result) == set(TypedDict.__annotations__)`` — so adding a
new key without widening the schema fails the test.

Every future public endpoint gets its own :class:`TypedDict` here.
"""

from __future__ import annotations

from typing import TypedDict


class PublicIdentityResponse(TypedDict):
    """Curated SelfModel subset exposed at ``GET /api/public/identity``."""

    framing: str
    awakened_at: str | None
    evolution_count: int
    identity_statements: list[str]
    values: list[str]
    capabilities_known: list[str]
    limitations_known: list[str]
    current_phase: str | None


#: SelfModel fields whose backing machinery the architecture retired and that
#: must never be re-exposed to anonymous visitors.  ``current_purpose_hypothesis``
#: was dropped from :class:`PublicIdentityResponse` when the consciousness-era
#: "Current purpose" card was removed; the engine behind it was deleted
#: and the field was deleted from ``SelfModel`` (proposal 149), so surfacing it
#: is dead exposure.  The former ``stage`` field is deleted from ``SelfModel``;
#: its retirement is owned by ``RETIRED_CONCEPTS``' ``evolutionary_stage`` entry
#: in ``tests/structural_guards.py`` (proposal 148).  ``surprise_count`` was dropped when the
#: "Journey so far" line collapsed to the single merge-backed capability stat; it
#: counted narrative churn — increments whenever new items appear in the
#: introspection narrative — not anything the agent shipped, a leftover of the
#: consciousness-era self-concept, so projecting it as a co-equal headline stat is
#: dead exposure as well.  ``tests/test_public_surface_fidelity.py``
#: asserts this set shares no key with
#: :attr:`PublicIdentityResponse.__annotations__`, reddening the build the moment
#: a retired field is re-added — a drift guard that composes with the exact-keys
#: guard in ``tests/test_dashboard_public_api.py``.
_RETIRED_SELF_MODEL_FIELDS: frozenset[str] = frozenset(
    {"current_purpose_hypothesis", "surprise_count"}
)

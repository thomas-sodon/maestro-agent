"""Model profile registry — the single owner of facts about Claude models.

Each :class:`ModelProfile` records a model's id, its default and supported
per-call efforts, and its published pricing (USD per million tokens, keyed by
the same ``input`` / ``output`` / ``cache_creation`` / ``cache_read`` rate
vocabulary ``config_settings._PRICING_RATE_KEYS`` accepts).
``Config.sdk_pricing``'s default is a projection of this registry
(:func:`pricing_table`), and config load refuses any ``[models]`` selection
that has no profile (proposal 140, step 1).

Admission rule: a model gets a profile only if it takes adaptive thinking
controlled by per-call effort, because Maestro steers thinking depth through
effort alone. Budget-only models (``claude-haiku-4-5-20251001``, and anything
before the 4.6 generation) deliberately get none — selecting one would load
and then silently run without thinking.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

#: Maestro-internal pricing label for the 1M-context tier — *not* an
#: Anthropic model id. ``maestro.sdk._label_long_context_pricing`` appends it
#: to ``QueryUsage.model`` when the long-context beta is active.
LONG_CONTEXT_PRICING_SUFFIX = "[1m]"

#: The model Maestro runs when no ``[models] default`` is configured.
DEFAULT_MODEL = "claude-opus-4-8"


@dataclass(frozen=True)
class ModelProfile:
    """Facts about one admitted model."""

    model_id: str
    default_effort: str
    efforts: frozenset[str]
    pricing: Mapping[str, float]
    long_context_pricing: Mapping[str, float] | None = None


def _rates(
    input_rate: float, output_rate: float, cache_read: float | None = None,
) -> Mapping[str, float]:
    """Build a rate row: cache write is 1.25x input, cache read 0.1x unless given."""
    return MappingProxyType({
        "input": input_rate,
        "output": output_rate,
        "cache_creation": input_rate * 1.25,
        "cache_read": input_rate * 0.1 if cache_read is None else cache_read,
    })


# The 1M-context tier bills above the 200K-input threshold at the published
# premium: 2x input (so 2x cache write/read) and 1.5x output.


_EFFORTS_4_6 = frozenset({"low", "medium", "high", "max"})
_EFFORTS_XHIGH = frozenset({"low", "medium", "high", "xhigh", "max"})

MODEL_PROFILES: Mapping[str, ModelProfile] = MappingProxyType({
    "claude-opus-5-5": ModelProfile(
        "claude-opus-5-5", "medium", _EFFORTS_XHIGH, _rates(4.0, 20.0, 0.2),
    ),
    "claude-opus-4-8": ModelProfile(
        "claude-opus-4-8", "high", _EFFORTS_XHIGH, _rates(5.0, 25.0),
        long_context_pricing=_rates(10.0, 37.5),
    ),
    "claude-opus-4-7": ModelProfile(
        "claude-opus-4-7", "high", _EFFORTS_XHIGH, _rates(5.0, 25.0),
        long_context_pricing=_rates(10.0, 37.5),
    ),
    "claude-opus-4-6": ModelProfile(
        "claude-opus-4-6", "high", _EFFORTS_4_6, _rates(5.0, 25.0),
    ),
    "claude-sonnet-4-6": ModelProfile(
        "claude-sonnet-4-6", "high", _EFFORTS_4_6, _rates(3.0, 15.0),
    ),
})


def get_profile(model_id: str) -> ModelProfile | None:
    """Return the profile for ``model_id``, or ``None`` when it is not admitted."""
    return MODEL_PROFILES.get(model_id)


def pricing_table() -> dict[str, dict[str, float]]:
    """Project the registry into a fresh ``sdk_pricing``-shaped table.

    One row per profile, plus a ``<id>[1m]`` row for each profile carrying
    ``long_context_pricing``.
    """
    table: dict[str, dict[str, float]] = {}
    for model_id, profile in MODEL_PROFILES.items():
        table[model_id] = dict(profile.pricing)
        if profile.long_context_pricing is not None:
            table[f"{model_id}{LONG_CONTEXT_PRICING_SUFFIX}"] = dict(
                profile.long_context_pricing
            )
    return table

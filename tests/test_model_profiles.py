"""Tests for the model profile registry and its config-load admission guard."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from maestro import model_profiles
from maestro.config import Config
from maestro.config_settings import _PRICING_RATE_KEYS
from maestro.toml_config import _deep_merge

_ROOT = Path(__file__).resolve().parent.parent
_BASE_TOML = (_ROOT / "maestro.toml").read_text(encoding="utf-8")
_HAIKU = "claude-haiku-4-5-20251001"


# =====================================================================
# Registry facts
# =====================================================================


@pytest.mark.parametrize("profile", list(model_profiles.MODEL_PROFILES.values()))
def test_profile_pricing_keys_match_rate_vocabulary(
    profile: model_profiles.ModelProfile,
) -> None:
    assert set(profile.pricing) == set(_PRICING_RATE_KEYS)
    if profile.long_context_pricing is not None:
        assert set(profile.long_context_pricing) == set(_PRICING_RATE_KEYS)


@pytest.mark.parametrize("profile", list(model_profiles.MODEL_PROFILES.values()))
def test_profile_default_effort_is_supported(
    profile: model_profiles.ModelProfile,
) -> None:
    assert profile.default_effort in profile.efforts


def test_profiles_keyed_by_their_own_id() -> None:
    for model_id, profile in model_profiles.MODEL_PROFILES.items():
        assert profile.model_id == model_id


def test_default_model_has_profile() -> None:
    assert model_profiles.get_profile(model_profiles.DEFAULT_MODEL) is not None


def test_budget_only_model_has_no_profile() -> None:
    assert model_profiles.get_profile(_HAIKU) is None


def test_opus_5_5_defaults_to_medium_effort() -> None:
    profile = model_profiles.get_profile("claude-opus-5-5")
    assert profile is not None
    assert profile.default_effort == "medium"


def test_pricing_table_emits_long_context_rows_exactly_for_profiles_with_them() -> None:
    table = model_profiles.pricing_table()
    suffix = model_profiles.LONG_CONTEXT_PRICING_SUFFIX
    expected_1m = {
        f"{model_id}{suffix}"
        for model_id, profile in model_profiles.MODEL_PROFILES.items()
        if profile.long_context_pricing is not None
    }
    assert {key for key in table if key.endswith(suffix)} == expected_1m
    assert {key for key in table if not key.endswith(suffix)} == set(
        model_profiles.MODEL_PROFILES
    )


def test_long_context_pricing_is_the_premium_tier() -> None:
    # The [1m] row exists to price the 1M-context premium; a copy of the
    # standard row would undercount every long-context call.
    for profile in model_profiles.MODEL_PROFILES.values():
        if profile.long_context_pricing is None:
            continue
        standard = profile.pricing
        premium = profile.long_context_pricing
        assert premium["input"] == standard["input"] * 2
        assert premium["cache_creation"] == standard["cache_creation"] * 2
        assert premium["cache_read"] == standard["cache_read"] * 2
        assert premium["output"] == standard["output"] * 1.5


def test_pricing_table_returns_fresh_object() -> None:
    first = model_profiles.pricing_table()
    first[model_profiles.DEFAULT_MODEL]["input"] = -1.0
    second = model_profiles.pricing_table()
    assert first is not second
    assert second[model_profiles.DEFAULT_MODEL]["input"] != -1.0


def test_sdk_long_context_suffix_owned_by_registry() -> None:
    from maestro import sdk

    assert sdk._LONG_CONTEXT_PRICING_SUFFIX == model_profiles.LONG_CONTEXT_PRICING_SUFFIX


def test_config_default_pricing_is_registry_projection() -> None:
    assert Config().sdk_pricing == model_profiles.pricing_table()


def test_config_default_model_is_registry_default() -> None:
    assert Config().sdk_model == model_profiles.DEFAULT_MODEL


@pytest.mark.parametrize(
    "toml_path", sorted(_ROOT.glob("maestro*.toml")), ids=lambda p: p.name,
)
def test_committed_models_have_profiles(toml_path: Path) -> None:
    """Every non-empty ``[models]`` value in a committed config is admitted."""
    models = tomllib.loads(toml_path.read_text(encoding="utf-8")).get("models", {})
    for key in ("default", "fallback", "long_context"):
        value = models.get(key, "")
        if value:
            assert model_profiles.get_profile(value) is not None, (key, value)


@pytest.mark.parametrize(
    "toml_path", sorted(_ROOT.glob("maestro*.toml")), ids=lambda p: p.name,
)
def test_committed_configs_carry_no_pricing_mirror(toml_path: Path) -> None:
    """No committed (overlay-merged) config restates the registry's pricing.

    A non-empty ``[sdk.pricing.*]`` group replaces the registry projection
    wholesale, so a committed mirror would drift silently (#4254); pricing
    lives only in ``maestro/model_profiles.py``.
    """
    base = tomllib.loads(_BASE_TOML)
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    if toml_path.name != "maestro.toml":
        data = _deep_merge(base, data)
    assert "pricing" not in data.get("sdk", {})


def test_shipped_default_model_is_registry_default() -> None:
    """``[models] default`` and ``DEFAULT_MODEL`` move together or CI fails."""
    models = tomllib.loads(_BASE_TOML)["models"]
    assert models["default"] == model_profiles.DEFAULT_MODEL


def test_shipped_config_pricing_is_registry_projection(mem_fs: dict[str, str]) -> None:
    """Loading the shipped ``maestro.toml`` yields exactly the registry pricing."""
    config = _load(mem_fs, _BASE_TOML)
    assert config.sdk_pricing == model_profiles.pricing_table()


# =====================================================================
# Fail-loud admission at config load
# =====================================================================


def _load(mem_fs: dict[str, str], toml: str) -> Config:
    mem_fs["/fake/toml/maestro.toml"] = toml
    with patch("dotenv.load_dotenv"):
        return Config.from_env(config_dir=Path("/fake/toml"))


def test_budget_only_fallback_refused_at_load(mem_fs: dict[str, str]) -> None:
    toml = _BASE_TOML.replace('fallback = ""', f'fallback = "{_HAIKU}"', 1)
    with pytest.raises(ValueError, match="sdk_fallback_model") as excinfo:
        _load(mem_fs, toml)
    assert "budget-only" in str(excinfo.value)


def test_unknown_default_refused_at_load(mem_fs: dict[str, str]) -> None:
    toml = _BASE_TOML.replace(
        f'default = "{model_profiles.DEFAULT_MODEL}"', 'default = "claude-imaginary-9"', 1,
    )
    with pytest.raises(ValueError, match="sdk_model names 'claude-imaginary-9'"):
        _load(mem_fs, toml)


def test_long_context_pricing_label_refused_at_load(mem_fs: dict[str, str]) -> None:
    label = model_profiles.DEFAULT_MODEL + model_profiles.LONG_CONTEXT_PRICING_SUFFIX
    toml = _BASE_TOML.replace('long_context = ""', f'long_context = "{label}"', 1)
    with pytest.raises(ValueError, match="sdk_model_long_context"):
        _load(mem_fs, toml)


def test_empty_model_values_load(mem_fs: dict[str, str]) -> None:
    toml = _BASE_TOML.replace(
        f'default = "{model_profiles.DEFAULT_MODEL}"', 'default = ""', 1,
    )
    config = _load(mem_fs, toml)
    assert config.sdk_model == ""
    assert config.sdk_fallback_model == ""
    assert config.sdk_model_long_context == ""


def test_admitted_models_load(mem_fs: dict[str, str]) -> None:
    toml = (
        _BASE_TOML
        .replace('fallback = ""', 'fallback = "claude-sonnet-4-6"', 1)
        .replace('long_context = ""', 'long_context = "claude-opus-4-7"', 1)
    )
    config = _load(mem_fs, toml)
    assert config.sdk_fallback_model == "claude-sonnet-4-6"
    assert config.sdk_model_long_context == "claude-opus-4-7"


def test_openai_only_routing_exempt_from_admission() -> None:
    config = Config(sdk_provider="openai", sdk_model="gpt-imaginary")
    config.validate()
    assert config.sdk_model == "gpt-imaginary"


def test_cli_paced_phase_override_enforces_admission() -> None:
    config = Config(
        sdk_provider="openai",
        sdk_phase_providers={"run_task": "cli_paced"},
        sdk_model=_HAIKU,
    )
    with pytest.raises(ValueError, match="no model profile"):
        config.validate()

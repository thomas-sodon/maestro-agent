"""Bind the ``[production: …]`` tags in base ``maestro.toml`` to the overlay manifests.

``maestro.toml`` holds base defaults only, so a key production overrides reads as
its base value to anyone scanning the file — a base ``false`` looks like "off in
production". Every key in :data:`~maestro.flag_lifecycle.PROD_ONLY_CANARIES`,
:data:`~maestro.flag_lifecycle.PROD_ONLY_DISABLED` or
:data:`~maestro.flag_lifecycle.PROD_ONLY_OPERATIONAL` therefore carries an inline
tag rendered by :func:`~maestro.flag_lifecycle.overlay_tag`. These tests bind the
tags to the manifests in both directions (a missing tag and a stale tag on a
graduated key both red) and to the merged production config's actual values.
Static reads of committed files — no writes.
"""

from __future__ import annotations

import re
from pathlib import Path

from maestro.config_settings import _SECTION_MODELS, build_config_via_pydantic
from maestro.flag_lifecycle import expected_overlay_tags, overlay_tag

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAESTRO_TOML = _REPO_ROOT / "maestro.toml"

_SECTION_RE = re.compile(r"^\[([^\]]+)\]")
_KEY_RE = re.compile(r"^(\w+)\s*=")
_TAG_RE = re.compile(r"# (\[production: (?:true|false)(?: — [^\]]+)?\])")


def scan_overlay_tags(toml_text: str) -> dict[tuple[str, str], str]:
    """Return ``(section, key) -> tag`` for every key line carrying a production tag."""
    tags: dict[tuple[str, str], str] = {}
    section = ""
    for line in toml_text.splitlines():
        header = _SECTION_RE.match(line)
        if header:
            section = header.group(1)
            continue
        key = _KEY_RE.match(line)
        tag = _TAG_RE.search(line)
        if key and tag:
            tags[(section, key.group(1))] = tag.group(1)
    return tags


def _field_locations() -> dict[str, tuple[str, str]]:
    """Map each ``Config`` field to its ``(section, TOML key)`` via ``Field.alias``."""
    return {
        name: (section, info.alias or name)
        for section, model in _SECTION_MODELS.items()
        for name, info in model.model_fields.items()
    }


def _expected_by_location() -> dict[tuple[str, str], str]:
    locations = _field_locations()
    return {locations[flag]: tag for flag, tag in expected_overlay_tags().items()}


def _tag_mismatches(
    scanned: dict[tuple[str, str], str], expected: dict[tuple[str, str], str],
) -> dict[tuple[str, str], tuple[str | None, str | None]]:
    """Return ``location -> (scanned, expected)`` for every disagreeing key."""
    return {
        loc: (scanned.get(loc), expected.get(loc))
        for loc in scanned.keys() | expected.keys()
        if scanned.get(loc) != expected.get(loc)
    }


def test_every_diverged_key_carries_its_tag() -> None:
    """The tags in ``maestro.toml`` equal the manifest-derived set exactly."""
    scanned = scan_overlay_tags(_MAESTRO_TOML.read_text(encoding="utf-8"))
    mismatches = _tag_mismatches(scanned, _expected_by_location())
    assert mismatches == {}, (
        "maestro.toml [production: …] tags drifted from the flag_lifecycle "
        f"manifests as (found, expected): {mismatches}. Render the tag with "
        "flag_lifecycle.overlay_tag, or drop it when the divergence graduates."
    )


def test_tag_value_matches_merged_production_config() -> None:
    """Each tag's value is the production value and differs from base."""
    base = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    scanned = scan_overlay_tags(_MAESTRO_TOML.read_text(encoding="utf-8"))
    by_location = {loc: field for field, loc in _field_locations().items()}
    for location, tag in scanned.items():
        field = by_location[location]
        tagged_value = tag.startswith("[production: true")
        assert getattr(prod, field) is tagged_value, f"{location}: {tag}"
        assert getattr(base, field) is not tagged_value, f"{location}: {tag}"


def test_overlay_tag_renders_proposal_and_omits_when_absent() -> None:
    assert overlay_tag("canary", True, "proposal 114 grounding; later proposal 120") == (
        "[production: true — canary, proposal 114]"
    )
    assert overlay_tag("prod-disabled", False, "rule-20 canary, no proposal") == (
        "[production: false — prod-disabled]"
    )


def test_scan_is_not_vacuous() -> None:
    scanned = scan_overlay_tags(_MAESTRO_TOML.read_text(encoding="utf-8"))
    assert len(scanned) >= 1
    assert len(expected_overlay_tags()) >= 1


_SEEDED_EXPECTED = {("loop", "a_enabled"): "[production: true — canary, proposal 114]"}


def test_seeded_missing_tag_is_detected() -> None:
    scanned = scan_overlay_tags("[loop]\na_enabled = false  # Master switch\n")
    assert _tag_mismatches(scanned, _SEEDED_EXPECTED) == {
        ("loop", "a_enabled"): (None, "[production: true — canary, proposal 114]"),
    }


def test_seeded_stale_tag_is_detected() -> None:
    text = (
        "[loop]\na_enabled = false  # [production: true — canary, proposal 114]\n"
        "[audit]\nb_enabled = true  # [production: false — prod-disabled] old\n"
    )
    mismatches = _tag_mismatches(scan_overlay_tags(text), _SEEDED_EXPECTED)
    assert mismatches == {
        ("audit", "b_enabled"): ("[production: false — prod-disabled]", None),
    }


def test_seeded_wrong_value_is_detected() -> None:
    text = "[loop]\na_enabled = false  # [production: false — canary, proposal 114]\n"
    mismatches = _tag_mismatches(scan_overlay_tags(text), _SEEDED_EXPECTED)
    assert mismatches == {
        ("loop", "a_enabled"): (
            "[production: false — canary, proposal 114]",
            "[production: true — canary, proposal 114]",
        ),
    }

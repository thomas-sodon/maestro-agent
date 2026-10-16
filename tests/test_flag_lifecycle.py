"""Tests for the flag-graduation grounding door in ``maestro.flag_lifecycle``.

The door refuses a self-initiated plan that proposes to flip/graduate an
``EXPERIMENTAL_FLAGS`` flag before its ``review_by`` horizon closes — a
human-owned cutover the loop must not pre-empt. These tests exercise the pure
verdict helper in isolation; the submission-door wiring (self_initiated gate,
obsolete persistence, no notification) is covered in ``tests/test_evolution.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pytest

from maestro.flag_lifecycle import (
    _CLAUSE_SPLIT_RE,
    EXPERIMENTAL_FLAGS,
    _flag_token_forms,
    flag_graduation_grounding_verdict,
    parse_review_by,
)

# A real EXPERIMENTAL_FLAGS key whose review_by is comfortably future-dated.
_FUTURE_FLAG = "sdk_single_use_evolution_calls_enabled"
# A ``today`` before every manifest horizon — so a future-review_by flag is live.
_BEFORE_HORIZONS = date(2026, 1, 1)
# A ``today`` past every manifest horizon — so every flag's window has closed.
_AFTER_HORIZONS = date(2099, 1, 1)


@dataclass
class _PlanStub:
    """Minimal structural stand-in for the plan fields the door reads."""

    title: str = ""
    proposed_changes: list[str] = field(default_factory=list)
    files_affected: list[str] = field(default_factory=list)


def test_future_flag_is_future_relative_to_before_horizons() -> None:
    """Guard the fixture: the chosen flag really is future at _BEFORE_HORIZONS."""
    entry = next(f for f in EXPERIMENTAL_FLAGS if f.flag == _FUTURE_FLAG)
    assert parse_review_by(entry.review_by) >= _BEFORE_HORIZONS


def test_flag_token_forms_renders_code_and_toml_shapes() -> None:
    """Both the code form and the ``[section] key`` TOML form are produced."""
    forms = _flag_token_forms(_FUTURE_FLAG)
    assert _FUTURE_FLAG in forms
    assert "[sdk] single_use_evolution_calls_enabled" in forms
    assert "single_use_evolution_calls_enabled" in forms


def test_flag_token_forms_no_underscore_has_single_form() -> None:
    """A key with no underscore yields only its verbatim form."""
    assert _flag_token_forms("solo") == ("solo",)


def test_refuses_future_flag_with_flip_intent_and_config_touched() -> None:
    """Criterion 1/4/5: future review_by + flip intent + cutover path => key."""
    plan = _PlanStub(
        title=f"Graduate {_FUTURE_FLAG} to default-on",
        proposed_changes=["flip the flag"],
        files_affected=["maestro/config.py"],
    )
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) == _FUTURE_FLAG


def test_matches_toml_form_of_the_flag_key() -> None:
    """Criterion 5: the ``[section] key`` TOML form is matched too."""
    plan = _PlanStub(
        title="housekeeping",
        proposed_changes=[
            "Promote [sdk] single_use_evolution_calls_enabled in maestro.toml",
        ],
        files_affected=["maestro.toml"],
    )
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) == _FUTURE_FLAG


def test_admits_past_review_by_flag() -> None:
    """Criterion 3: a flag whose window has closed is the human's to graduate."""
    plan = _PlanStub(
        title=f"Graduate {_FUTURE_FLAG} to default-on",
        proposed_changes=["flip the flag"],
        files_affected=["maestro/config.py"],
    )
    assert flag_graduation_grounding_verdict(plan, _AFTER_HORIZONS) is None


def test_admits_bare_mention_without_flip_intent() -> None:
    """Criterion 4: a mention lacking flip/graduate/promote/default-on passes."""
    plan = _PlanStub(
        title=f"Add a reader for {_FUTURE_FLAG}",
        proposed_changes=[f"document {_FUTURE_FLAG} in a comment"],
        files_affected=["maestro/config.py"],
    )
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) is None


def test_admits_when_no_flag_token_named() -> None:
    """A flip-intent clause naming no EXPERIMENTAL_FLAGS key passes."""
    plan = _PlanStub(
        title="Flip some unrelated default on",
        proposed_changes=["graduate the thing"],
        files_affected=["maestro/config.py"],
    )
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) is None


def test_admits_when_cutover_path_not_touched() -> None:
    """Criterion 1: naming+flipping a flag but touching no cutover path passes."""
    plan = _PlanStub(
        title=f"Graduate {_FUTURE_FLAG} to default-on",
        proposed_changes=["flip the flag"],
        files_affected=["maestro/agent.py"],
    )
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) is None


def test_refuses_flag_then_dotted_toml_path_then_intent() -> None:
    """A dot inside ``maestro.toml`` does not split the flag from its intent."""
    plan = _PlanStub(
        title="housekeeping",
        proposed_changes=[f"Set {_FUTURE_FLAG} = true in maestro.toml (promote to default-on)"],
        files_affected=["maestro.toml"],
    )
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) == _FUTURE_FLAG


def test_refuses_flag_then_dotted_config_path_then_intent() -> None:
    """A dot inside ``maestro/config.py`` does not split the clause."""
    plan = _PlanStub(
        title="housekeeping",
        proposed_changes=[f"Set {_FUTURE_FLAG} default to True in maestro/config.py and graduate it"],
        files_affected=["maestro/config.py"],
    )
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) == _FUTURE_FLAG


def test_sentence_end_dot_still_separates_clauses() -> None:
    """A sentence-ending dot remains a clause boundary."""
    plan = _PlanStub(
        title="housekeeping",
        proposed_changes=[f"Read {_FUTURE_FLAG} in maestro.toml. Later promote the backlog cleanup"],
        files_affected=["maestro.toml"],
    )
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) is None


def test_trailing_dot_at_end_of_part_does_not_break_match() -> None:
    """A final dot at end of string does not break the match."""
    plan = _PlanStub(
        title="housekeeping",
        proposed_changes=[f"Promote {_FUTURE_FLAG} in maestro/config.py."],
        files_affected=["maestro/config.py"],
    )
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) == _FUTURE_FLAG


def test_clause_split_keeps_path_dot() -> None:
    """The splitter leaves a path dot inside one clause."""
    parts = _CLAUSE_SPLIT_RE.split("a in maestro.toml (promote x)")
    assert parts == ["a in maestro.toml (promote x)"]


def test_clause_split_cuts_at_end_of_string_dot() -> None:
    """A dot at end of string is a boundary, distinct from a path dot."""
    parts = _CLAUSE_SPLIT_RE.split("Edit maestro/config.py.")
    assert parts == ["Edit maestro/config.py", ""]


def test_admits_when_flip_intent_in_different_clause_than_key() -> None:
    """Flip intent and the key must co-occur in one clause, not scattered."""
    plan = _PlanStub(
        title="Promote the backlog cleanup",
        proposed_changes=[f"Later we may read {_FUTURE_FLAG} somewhere"],
        files_affected=["maestro/config.py"],
    )
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) is None


def test_grounded_planning_rejection_tool_is_future_dated_experimental_flag() -> None:
    """The new flag resolves in EXPERIMENTAL_FLAGS with a future review_by."""
    entry = next(
        f for f in EXPERIMENTAL_FLAGS
        if f.flag == "grounded_planning_rejection_tool_enabled"
    )
    assert parse_review_by(entry.review_by) >= _BEFORE_HORIZONS


def test_grounded_planning_rejection_tool_prod_canary_carries_justification() -> None:
    """The prod canary is declared with a non-empty justification and no secret."""
    from maestro.flag_lifecycle import PROD_ONLY_CANARIES
    from maestro.secret_scan import has_hard_block

    canary = next(
        c for c in PROD_ONLY_CANARIES
        if c.flag == "grounded_planning_rejection_tool_enabled"
    )
    assert canary.env == "production"
    assert canary.justification.strip()
    assert parse_review_by(canary.review_by) >= _BEFORE_HORIZONS
    assert has_hard_block(canary.note) is False
    assert has_hard_block(canary.justification) is False


# --- #4607: bare TOML keys, plain flip wording, identifier boundaries ---------

# The live-incident flag (plan 34088ff029d6): a ``[loop]`` key whose bare TOML
# key ``curate_skip_serviced_enabled`` drops the ``loop_`` field prefix.
_LIVE_FLAG = "loop_curate_skip_serviced_enabled"
# The ``today`` the issue's reproduction table was run at.
_INCIDENT_DAY = date(2026, 10, 10)
_TOML_ONLY = ["maestro.toml"]


def test_live_flag_is_future_relative_to_incident_day() -> None:
    """Guard the fixture: the live flag's window is still open at _INCIDENT_DAY."""
    entry = next(f for f in EXPERIMENTAL_FLAGS if f.flag == _LIVE_FLAG)
    assert parse_review_by(entry.review_by) >= _INCIDENT_DAY


def test_flag_token_forms_includes_bare_toml_key() -> None:
    """The bare ``maestro.toml`` key (field prefix dropped) is a token form."""
    assert "curate_skip_serviced_enabled" in _flag_token_forms(_LIVE_FLAG)


def test_refuses_bare_toml_key_with_promote() -> None:
    """A title naming the bare TOML key with ``Promote`` is a cutover attempt."""
    plan = _PlanStub(
        title="Promote curate_skip_serviced_enabled to default",
        files_affected=_TOML_ONLY,
    )
    assert flag_graduation_grounding_verdict(plan, _INCIDENT_DAY) == _LIVE_FLAG


@pytest.mark.parametrize(
    "clause",
    [
        "Change loop_curate_skip_serviced_enabled = false to true",
        "Set loop_curate_skip_serviced_enabled to true",
        "Enable loop_curate_skip_serviced_enabled by default",
        "Turn on curate_skip_serviced_enabled",
        "curate_skip_serviced_enabled = True",
        "Promoted curate_skip_serviced_enabled",
        "Make loop_curate_skip_serviced_enabled enabled by default",
        "Default curate_skip_serviced_enabled to enabled",
        "Switch curate_skip_serviced_enabled on",
        "Activate curate_skip_serviced_enabled",
        "curate_skip_serviced_enabled: false -> true",
        "Set curate_skip_serviced_enabled: true",
        "Make curate_skip_serviced_enabled default true",
        "Switch on curate_skip_serviced_enabled",
        "set curate_skip_serviced_enabled true",
        "Turn curate_skip_serviced_enabled on",
    ],
)
def test_refuses_plain_flip_wording(clause: str) -> None:
    """change/set/enable/turn-on/assign-true/inflected wording is flip intent."""
    plan = _PlanStub(proposed_changes=[clause], files_affected=_TOML_ONLY)
    assert flag_graduation_grounding_verdict(plan, _INCIDENT_DAY) == _LIVE_FLAG


def test_refuses_live_incident_plan_34088ff029d6() -> None:
    """The incident plan is refused through its change clause, not its title."""
    plan = _PlanStub(
        title="Promote curate_skip_serviced default to on to cap curate re-spend",
        proposed_changes=[
            "In maestro.toml under [loop], change "
            "`curate_skip_serviced_enabled = false` to `true`",
        ],
        files_affected=_TOML_ONLY,
    )
    assert flag_graduation_grounding_verdict(plan, _INCIDENT_DAY) == _LIVE_FLAG


def test_incident_title_alone_is_not_matched() -> None:
    """The shortened title alone names no flag form, so the change clause binds."""
    plan = _PlanStub(
        title="Promote curate_skip_serviced default to on to cap curate re-spend",
        files_affected=_TOML_ONLY,
    )
    assert flag_graduation_grounding_verdict(plan, _INCIDENT_DAY) is None


@pytest.mark.parametrize(
    "clause",
    [
        "read `curate_skip_serviced_enabled` to decide whether to skip",
        "when curate_skip_serviced_enabled is enabled, log the skip",
        "leave curate_skip_serviced_enabled = false unchanged",
        "when curate_skip_serviced_enabled evaluates to true, skip the curate call",
        "skip when curate_skip_serviced_enabled == true",
        "log when curate_skip_serviced_enabled != true",
        "Switch the curate pass to read curate_skip_serviced_enabled based on queue depth",
        "keep curate_skip_serviced_enabled at its default and point the sensor to on-call",
        "when curate_skip_serviced_enabled is activated, log the skip",
        "Gate the new sensor on curate_skip_serviced_enabled so it only runs when the flag turns on",
        "Document that curate_skip_serviced_enabled enables the serviced-skip path",
        "curate_skip_serviced_enabled activates the hold",
        "curate_skip_serviced_enabled flips the curate pass to skip mode",
        "Note: true only when curate_skip_serviced_enabled is set in the overlay",
    ],
)
def test_admits_bare_toml_key_mention_without_flip_intent(clause: str) -> None:
    """A bare-key mention with no flip intent (descriptive 'enabled') admits."""
    plan = _PlanStub(proposed_changes=[clause], files_affected=_TOML_ONLY)
    assert flag_graduation_grounding_verdict(plan, _INCIDENT_DAY) is None


def test_token_match_respects_identifier_boundaries() -> None:
    """The bare form inside a longer identifier is not a hit."""
    plan = _PlanStub(
        title="Promote other_curate_skip_serviced_enabled_v2",
        files_affected=_TOML_ONLY,
    )
    assert flag_graduation_grounding_verdict(plan, _INCIDENT_DAY) is None


def test_code_form_match_respects_identifier_boundaries() -> None:
    """The code form inside a longer identifier is not a hit."""
    plan = _PlanStub(
        title="Promote loop_curate_skip_serviced_enabled_v2",
        files_affected=_TOML_ONLY,
    )
    assert flag_graduation_grounding_verdict(plan, _INCIDENT_DAY) is None


def _config_field_locations() -> dict[str, tuple[str, str]]:
    """Map each ``Config`` field to its ``(section, TOML key)`` via ``Field.alias``.

    Mirrors ``tests/test_base_toml_overlay_tags.py::_field_locations``; the
    runtime ``_toml_location_by_field`` now reads the same ``_SECTION_MODELS``
    (lazily), so this is an independent recomputation, not a separate source.
    """
    from maestro.config_settings import _SECTION_MODELS

    return {
        name: (section, info.alias or name)
        for section, model in _SECTION_MODELS.items()
        for name, info in model.model_fields.items()
    }


def _location_mismatches() -> dict[str, list[str]]:
    """Return ``flag -> [failed probe, ...]`` for every unmatched TOML location."""
    locations = _config_field_locations()
    failures: dict[str, list[str]] = {}
    for entry in EXPERIMENTAL_FLAGS:
        flag = entry.flag
        if flag not in locations:
            failures[flag] = ["no Config location"]
            continue
        section, alias = locations[flag]
        probes = [f"Promote [{section}] {alias}"]
        if "_" in alias:
            probes.append(f"Set {alias} = true")
        missed = [
            probe for probe in probes
            if flag_graduation_grounding_verdict(
                _PlanStub(title=probe, files_affected=_TOML_ONLY),
                _BEFORE_HORIZONS,
            ) != flag
        ]
        if "_" in alias and alias not in _flag_token_forms(flag):
            missed.append(f"{alias!r} not a token form")
        if missed:
            failures[flag] = missed
    return failures


def test_every_experimental_flag_toml_location_is_matched() -> None:
    """Every flag's authoritative ``[section] alias`` and bare alias resolve to it."""
    assert _location_mismatches() == {}


def test_flag_token_forms_subset_of_authoritative_locations() -> None:
    """No token form is spurious: each is the code form, bare key or header."""
    locations = _config_field_locations()
    spurious = {
        entry.flag: sorted(set(forms) - allowed)
        for entry in EXPERIMENTAL_FLAGS
        if entry.flag in locations
        for section, alias in [locations[entry.flag]]
        for allowed in [{entry.flag, alias, f"[{section}] {alias}"}]
        for forms in [_flag_token_forms(entry.flag)]
        if not set(forms) <= allowed
    }
    assert spurious == {}


def test_single_segment_bare_key_is_not_a_token_form() -> None:
    """``publication_enabled`` keeps its header form but not bare ``enabled``."""
    assert _flag_token_forms("publication_enabled") == (
        "publication_enabled", "[publication] enabled",
    )


@pytest.mark.parametrize(
    "clause",
    [
        "Set foo = true when the reserve is enabled",
        "Add [loop] new_thing_enabled = true to maestro.toml so the sensor is enabled",
        "Turn on verbose logging when debugging is enabled",
    ],
)
def test_plain_word_enabled_names_no_flag(clause: str) -> None:
    """The ordinary word ``enabled`` names no flag."""
    plan = _PlanStub(proposed_changes=[clause], files_affected=_TOML_ONLY)
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) is None



_HANDOFF_FLAG = "ci_pending_green_handoff_enabled"


def test_flag_token_forms_header_from_real_section() -> None:
    """The header form names the flag's real section, not its first segment."""
    assert _flag_token_forms(_HANDOFF_FLAG) == (
        _HANDOFF_FLAG, f"[loop] {_HANDOFF_FLAG}",
    )


def test_refuses_real_section_header_form() -> None:
    """``[loop] ci_pending_green_handoff_enabled`` binds the flag."""
    entry = next(e for e in EXPERIMENTAL_FLAGS if e.flag == _HANDOFF_FLAG)
    assert parse_review_by(entry.review_by) > _BEFORE_HORIZONS
    plan = _PlanStub(
        proposed_changes=[f"Set [loop] {_HANDOFF_FLAG} = true"],
        files_affected=_TOML_ONLY,
    )
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) == (
        _HANDOFF_FLAG
    )


def test_generic_bare_alias_is_not_a_form() -> None:
    """``publication_enabled``'s alias ``enabled`` is too generic to be a form."""
    forms = _flag_token_forms("publication_enabled")
    assert "enabled" not in forms
    assert "[publication] enabled" in forms


@pytest.mark.parametrize(
    "clause",
    [
        "set curate_skip_serviced_enabled to false",
        "In maestro.toml under [loop], change "
        "`curate_skip_serviced_enabled = true` to `false`",
        "Disable curate_skip_serviced_enabled (currently = true)",
        "turn off curate_skip_serviced_enabled = true",
    ],
)
def test_admits_turn_off_wording(clause: str) -> None:
    """Turning a flag off is no cutover, even beside its current ``= true``."""
    plan = _PlanStub(proposed_changes=[clause], files_affected=_TOML_ONLY)
    assert flag_graduation_grounding_verdict(plan, _BEFORE_HORIZONS) is None

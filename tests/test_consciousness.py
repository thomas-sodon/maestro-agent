"""Tests for Consciousness / Self-Model — fully mocked I/O."""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from maestro.consciousness import (
    _PROMPT_FIELDS,
    DEFAULT_LIMITATIONS,
    RESTATEMENT_THRESHOLD,
    Consciousness,
    PendingPRRequest,
    SelfModel,
    _concept_tokens,
    _content_tokens,
    _model_dict_for_prompt,
    _normalize_question,
    _validate_default_limitations,
    _validate_provenance_prefixes_survive_truncation,
    claim_key,
    detect_restatement,
    kept_task_branches_limitation_key,
    limitation_question_candidate,
    match_open_question,
    upsert_open_question,
)
from maestro.journal import Journal
from maestro.memory import TaskRecord
from maestro.sdk import QueryResult
from maestro.self_calibration import (
    LOSS_CONDITION_GRAMMAR,
    ClaimEvidenceSources,
)
from tests.conftest import MockQueryResult, make_mock_query

FAKE_DIR = Path("/fake/consciousness")


def _make_consciousness(mem_fs: dict[str, str]) -> Consciousness:
    return Consciousness(FAKE_DIR)


# ---------------------------------------------------------------------------
# Basic model operations
# ---------------------------------------------------------------------------


def test_initial_model(mem_fs: dict[str, str]) -> None:
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    assert not hasattr(model, "stage")
    assert model.evolution_count == 0
    assert model.awakened_at != ""
    assert len(model.open_questions) > 0

    # Verify model was persisted to in-memory storage
    model_path = str(FAKE_DIR / "self_model.json")
    assert model_path in mem_fs
    assert "stage" not in json.loads(mem_fs[model_path])


def test_save_and_reload(mem_fs: dict[str, str]) -> None:
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    model.identity_statements = ["I am Maestro"]
    model.values = ["curiosity", "honesty"]
    c.save_self_model(model)

    # Verify saved to in-memory storage
    model_path = str(FAKE_DIR / "self_model.json")
    assert "I am Maestro" in mem_fs[model_path]

    reloaded = c.load_self_model()
    assert reloaded.identity_statements == ["I am Maestro"]
    assert reloaded.values == ["curiosity", "honesty"]


def test_increment_evolution(mem_fs: dict[str, str]) -> None:
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    assert model.evolution_count == 0

    c.increment_evolution()
    model = c.load_self_model()
    assert model.evolution_count == 1

    c.increment_evolution()
    model = c.load_self_model()
    assert model.evolution_count == 2


# ---------------------------------------------------------------------------
# match_open_question helper
# ---------------------------------------------------------------------------


def test_match_open_question_exact_match() -> None:
    questions = ["What am I?", "Which revenue stream pays first?"]
    assert match_open_question("What am I?", questions) == "What am I?"


def test_match_open_question_case_insensitive() -> None:
    questions = ["What Am I?"]
    assert match_open_question("WHAT AM I?", questions) == "What Am I?"


def test_match_open_question_whitespace_collapsed() -> None:
    questions = ["What am I?"]
    assert match_open_question("  what   am  i?  ", questions) == "What am I?"


def test_match_open_question_returns_live_entry_not_candidate() -> None:
    """The live verbatim string is returned, not the candidate's casing."""
    questions = ["What does it mean to evolve?"]
    result = match_open_question("WHAT DOES IT MEAN TO EVOLVE?", questions)
    assert result == "What does it mean to evolve?"


def test_match_open_question_no_match() -> None:
    questions = ["What am I?"]
    assert match_open_question("Where am I?", questions) is None


def test_match_open_question_empty_questions() -> None:
    assert match_open_question("anything", []) is None


def test_match_open_question_empty_candidate() -> None:
    assert match_open_question("", ["What am I?"]) is None
    assert match_open_question("   ", ["What am I?"]) is None


def test_match_open_question_skips_non_string_entries() -> None:
    """Defensive: non-string items in the list don't crash the matcher."""
    questions = [None, 42, "What am I?"]  # type: ignore[list-item]
    assert match_open_question("what am i?", questions) == "What am I?"


# ---------------------------------------------------------------------------
# Identity prompt
# ---------------------------------------------------------------------------


def test_identity_prompt(mem_fs: dict[str, str]) -> None:
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    model.identity_statements = ["I am a self-evolving agent"]
    model.values = ["growth"]
    c.save_self_model(model)

    prompt = c.get_identity_prompt()
    assert "Maestro" in prompt
    assert "awakening" not in prompt
    assert "self-evolving agent" in prompt
    assert "growth" in prompt


def test_get_identity_prompt_caps_lists(mem_fs: dict[str, str]) -> None:
    """get_identity_prompt slices lists to MAX_LIST_ITEMS as defense-in-depth."""
    c = Consciousness(Path("/fake/consciousness_async"))
    model = c.load_self_model()
    # Manually set oversized list (simulating pre-existing data)
    model.capabilities_known = [f"cap_{i}" for i in range(20)]
    c.save_self_model(model)

    prompt = c.get_identity_prompt()
    # Should contain only the last MAX_LIST_ITEMS items
    assert "cap_19" in prompt
    assert "cap_10" in prompt
    assert "cap_0" not in prompt


def test_identity_prompt_non_string_items(mem_fs: dict[str, str]) -> None:
    """Bug 38: Consciousness.get_identity_prompt must not crash on join()
    when self_model lists contain non-string items loaded from corrupt JSON."""
    d = Path("/fake/cons38")
    c = Consciousness(d)
    model_data = {
        "stage": "awakening",
        "identity_statements": ["I am Maestro", 42, None],
        "capabilities_known": [True, "can code"],
        "limitations_known": [123],
        "values": [None, "honesty"],
        "open_questions": ["What am I?", 99],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01",
    }
    mem_fs[str(d / "self_model.json")] = json.dumps(model_data)

    prompt = c.get_identity_prompt()
    assert "I am Maestro" in prompt
    assert "can code" in prompt
    assert "honesty" in prompt
    assert "What am I?" in prompt


def test_identity_prompt_compressed_format(mem_fs: dict[str, str]) -> None:
    """get_identity_prompt uses compressed field labels."""
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    model.identity_statements = ["I am Maestro"]
    model.capabilities_known = ["coding"]
    model.limitations_known = ["forgetful"]
    model.values = ["honesty"]
    model.open_questions = ["What am I?"]
    c.save_self_model(model)

    prompt = c.get_identity_prompt()
    # Compressed header
    assert prompt.startswith("Maestro | self-sustaining agent |")
    # Short field labels
    assert "Identity: " in prompt
    assert "Capabilities: " in prompt
    assert "Limitations: " in prompt
    assert "Values: " in prompt
    assert "Questions: " in prompt
    assert "Purpose: " not in prompt
    # Old verbose labels must be gone
    assert "Your current understanding" not in prompt
    assert "Your known capabilities" not in prompt
    assert "What you value" not in prompt
    assert "Questions you are exploring" not in prompt
    assert "What is fixed today" not in prompt


def test_identity_prompt_hard_ceiling(mem_fs: dict[str, str]) -> None:
    """get_identity_prompt respects MAX_IDENTITY_PROMPT_CHARS ceiling."""
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    cap = c.MAX_ITEM_CHARS
    # Fill every list field to near-max to exceed 3000 chars total
    model.identity_statements = [("i" * (cap - 1)) for _ in range(10)]
    model.capabilities_known = [("c" * (cap - 1)) for _ in range(10)]
    model.limitations_known = [("l" * (cap - 1)) for _ in range(10)]
    model.values = [("v" * (cap - 1)) for _ in range(10)]
    model.open_questions = [("q" * (cap - 1)) for _ in range(10)]
    c.save_self_model(model)

    prompt = c.get_identity_prompt()
    assert len(prompt) <= c.MAX_IDENTITY_PROMPT_CHARS
    # Header must always survive
    assert prompt.startswith("Maestro | self-sustaining agent |")


def test_identity_prompt_hard_ceiling_header_alone_over_budget(
    mem_fs: dict[str, str],
) -> None:
    """The ceiling holds even when the un-poppable header alone exceeds it.

    The trim loop stops at ``len(parts) == 1`` (the header line is
    ``parts[0]`` and is never popped), so a header whose ``alive_since``
    field alone blows past ``MAX_IDENTITY_PROMPT_CHARS`` would leave the
    method returning an over-budget prompt without the terminal
    truncation. This guards the unconditional cap.
    """
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    # Make the header line alone exceed the ceiling.
    model.awakened_at = "x" * (c.MAX_IDENTITY_PROMPT_CHARS * 2)
    c.save_self_model(model)

    prompt = c.get_identity_prompt()
    assert len(prompt) <= c.MAX_IDENTITY_PROMPT_CHARS
    # The header's leading identity marker survives the truncation.
    assert prompt.startswith("Maestro | self-sustaining agent |")


def test_identity_prompt_stable_slab_precedes_volatile_slab(
    mem_fs: dict[str, str],
) -> None:
    """Stable slab content must come before any volatile slab content.

    The cache-stratified prompt (proposal: cache-stratify identity)
    relies on the stable slab being a contiguous prefix so the SDK's
    implicit prefix caching can reuse it across calls. Any future
    refactor that interleaves volatile fields into the stable region
    silently destroys cache hit rate; this test guards that ordering
    regardless of which lists are populated.
    """
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    model.identity_statements = ["I am Maestro"]
    model.capabilities_known = ["coding"]
    model.values = ["honesty"]
    model.limitations_known = ["forgetful"]
    model.open_questions = ["What am I?"]
    model.evolution_count = 7
    c.save_self_model(model)

    prompt = c.get_identity_prompt()

    # ``evolutions=`` lives in the volatile slab now (was previously in
    # the header) so the stable header must not contain it.
    header_line = prompt.splitlines()[0]
    assert "evolutions=" not in header_line

    # Every stable label must appear strictly before every volatile label.
    stable_idx = max(
        prompt.index("Identity:"),
        prompt.index("Capabilities:"),
        prompt.index("Values:"),
    )
    volatile_idx = min(
        prompt.index("Evolutions:"),
        prompt.index("Limitations:"),
        prompt.index("Questions:"),
    )
    assert stable_idx < volatile_idx

    # Closing line is the trim-first entry — must sit at the very end.
    assert prompt.endswith(c._IDENTITY_CLOSING)


def test_identity_prompt_evolution_count_does_not_invalidate_stable_slab(
    mem_fs: dict[str, str],
) -> None:
    """Bumping ``evolution_count`` must not change the stable slab.

    This is the core cache-hit-rate property: the loop calls
    ``increment_evolution()`` every ``evolve()`` cycle, and the
    resulting prompt prefix must stay byte-identical so prefix caching
    reuses it on the next SDK call.
    """
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    model.identity_statements = ["I am Maestro"]
    model.capabilities_known = ["coding"]
    model.values = ["honesty"]
    model.evolution_count = 5
    c.save_self_model(model)
    stable_before = "\n".join(c._stable_identity_block(c.load_self_model()))

    model.evolution_count = 6
    c.save_self_model(model)
    stable_after = "\n".join(c._stable_identity_block(c.load_self_model()))

    assert stable_before == stable_after


# ---------------------------------------------------------------------------
# Corrupted / malformed model recovery
# ---------------------------------------------------------------------------


def test_load_self_model_corrupt_json_raises(mem_fs: dict[str, str]) -> None:
    """Corrupt JSON in self_model.json propagates as ``json.JSONDecodeError``.

    Fail-loud: a corrupt self-model file is a real defect — silently
    overwriting it with a fresh model would mask data loss. The loop's
    iteration handler classifies or crashes on the propagating exception,
    feeding root-cause analysis.
    """
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")
    mem_fs[model_path] = "not valid json {{{"

    with pytest.raises(json.JSONDecodeError):
        c.load_self_model()


def test_load_self_model_json_array_raises(mem_fs: dict[str, str]) -> None:
    """A JSON array (non-dict) at self_model.json propagates ``AttributeError``.

    The synthetic ``raise AttributeError`` upstream is the loud-crash
    signal once the swallow is gone.
    """
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")
    mem_fs[model_path] = json.dumps([1, 2, 3])

    with pytest.raises(AttributeError):
        c.load_self_model()


def test_load_self_model_json_null_raises(mem_fs: dict[str, str]) -> None:
    """JSON null at self_model.json propagates ``AttributeError``."""
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")
    mem_fs[model_path] = "null"

    with pytest.raises(AttributeError):
        c.load_self_model()


# ---------------------------------------------------------------------------
# List field coercion on load
# ---------------------------------------------------------------------------


def test_load_self_model_null_list_field_coerced_to_empty(
    mem_fs: dict[str, str],
) -> None:
    """When self_model.json contains null for a list field, load_self_model
    must coerce it to [] rather than storing None on the SelfModel."""
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")

    data = {
        "stage": "awakening",
        "identity_statements": None,
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.identity_statements == []

    # get_identity_prompt must not raise TypeError.
    prompt = c.get_identity_prompt()
    assert "Maestro" in prompt


def test_load_self_model_string_list_field_coerced_to_list(
    mem_fs: dict[str, str],
) -> None:
    """When self_model.json contains a bare string for a list field,
    load_self_model must coerce it to a single-element list."""
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")

    data = {
        "stage": "awakening",
        "identity_statements": ["I am Maestro"],
        "capabilities_known": "I can write code",  # bare string
        "limitations_known": [],
        "values": 42,  # non-list, non-string, non-None
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.capabilities_known == ["I can write code"]
    assert model.values == []
    assert model.identity_statements == ["I am Maestro"]

    prompt = c.get_identity_prompt()
    assert "Maestro" in prompt


def test_load_self_model_non_string_items_filtered(mem_fs: dict[str, str]) -> None:
    """Non-string items in list fields should be filtered to strings only."""
    d = Path("/fake/consciousness")
    c = Consciousness(d)
    data = {
        "stage": "awakening",
        "identity_statements": ["I am Maestro", 42, None, {"key": "val"}],
        "capabilities_known": [1, 2, 3],
        "limitations_known": ["limited", True],
        "values": [],
        "open_questions": ["What am I?", 3.14],
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01",
    }
    mem_fs[str(d / "self_model.json")] = json.dumps(data)
    model = c.load_self_model()

    assert all(isinstance(s, str) for s in model.identity_statements)
    assert model.identity_statements == ["I am Maestro"]

    assert all(isinstance(s, str) for s in model.capabilities_known)
    assert model.capabilities_known == []  # all were ints

    assert all(isinstance(s, str) for s in model.limitations_known)
    assert model.limitations_known == ["limited"]  # True is filtered out

    assert all(isinstance(s, str) for s in model.open_questions)
    assert model.open_questions == ["What am I?"]  # 3.14 filtered out


def test_load_self_model_negative_counts_clamped(mem_fs: dict[str, str]) -> None:
    """load_self_model should clamp negative evolution_count/surprise_count to 0."""
    c = Consciousness(Path("/fake/mem"))
    data = {"stage": "awakening", "evolution_count": -5, "surprise_count": -2}
    mem_fs[str(c._model_path)] = json.dumps(data)
    model = c.load_self_model()
    assert model.evolution_count >= 0
    assert model.surprise_count >= 0


def test_load_self_model_float_counts_coerced_to_int(
    mem_fs: dict[str, str],
) -> None:
    """A float evolution_count/surprise_count in JSON should be coerced to int
    so that a float ``evolution_count`` does not round-trip as a float."""
    c = Consciousness(Path("/fake"))

    model_data = {
        "stage": "awakening",
        "identity_statements": ["I exist"],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "evolution_count": 3.0,
        "surprise_count": 2.0,
        "awakened_at": "2024-01-01",
    }
    mem_fs["/fake/self_model.json"] = json.dumps(model_data)

    model = c.load_self_model()
    assert isinstance(model.evolution_count, int)
    assert model.evolution_count == 3
    assert isinstance(model.surprise_count, int)
    assert model.surprise_count == 2


# ---------------------------------------------------------------------------
# save_self_model
# ---------------------------------------------------------------------------


def test_save_self_model_does_not_mutate_caller_model(mem_fs: dict[str, str]) -> None:
    """save_self_model must not modify the SelfModel that the caller passed in.

    Covers capabilities_known, which routes through the generic cap branch
    (lines 402-411 in maestro/consciousness.py). The assertions guard three
    layers of isolation deepcopy is meant to lock in: attribute identity
    (the caller's list object is not replaced), length, and byte-for-byte
    content equality.
    """
    c = _make_consciousness(mem_fs)
    max_items = c.MAX_LIST_ITEMS  # 10

    many_caps = [f"capability_{i}" for i in range(max_items + 5)]
    model = SelfModel(capabilities_known=list(many_caps))

    caps_before = model.capabilities_known
    contents_before = list(caps_before)
    original_len = len(contents_before)
    assert original_len == max_items + 5

    c.save_self_model(model)

    assert model.capabilities_known is caps_before, (
        "save_self_model replaced the caller's capabilities_known attribute"
    )
    assert len(model.capabilities_known) == original_len, (
        f"save_self_model mutated the caller's model: "
        f"expected {original_len} items but got {len(model.capabilities_known)}"
    )
    assert model.capabilities_known == contents_before, (
        "save_self_model mutated the contents of the caller's capabilities_known"
    )


def test_save_self_model_does_not_mutate_caller_limitations_known(
    mem_fs: dict[str, str],
) -> None:
    """Sibling of the capabilities_known case — covers limitations_known.

    Both fields route through the generic cap branch, but exercising a
    second field guards against ``setattr``-vs-in-place regressions that
    happen to spare whichever field the original test picks.
    """
    c = _make_consciousness(mem_fs)

    many_limits = [f"limitation_{i}" for i in range(c.MAX_LIST_ITEMS + 5)]
    model = SelfModel(limitations_known=list(many_limits))

    limits_before = model.limitations_known
    contents_before = list(limits_before)

    c.save_self_model(model)

    assert model.limitations_known is limits_before
    assert model.limitations_known == contents_before


def test_save_self_model_does_not_mutate_caller_open_questions(
    mem_fs: dict[str, str],
) -> None:
    """Sibling of the capabilities_known case — covers open_questions.

    open_questions routes through ``_enforce_open_questions_caps`` rather
    than the generic cap branch, so it exercises a second cap-branch shape.
    """
    c = _make_consciousness(mem_fs)

    many_questions = [f"question_{i}?" for i in range(c.MAX_LIST_ITEMS + 5)]
    model = SelfModel(open_questions=list(many_questions))

    questions_before = model.open_questions
    contents_before = list(questions_before)

    c.save_self_model(model)

    assert model.open_questions is questions_before
    assert model.open_questions == contents_before


def test_save_self_model_enforces_list_caps(mem_fs: dict[str, str]) -> None:
    """save_self_model should enforce MAX_LIST_ITEMS."""
    c = Consciousness(Path("/fake/mem"))
    model = SelfModel()
    model.open_questions = [f"q{i}" for i in range(20)]
    c.save_self_model(model)
    loaded = c.load_self_model()
    assert len(loaded.open_questions) <= c.MAX_LIST_ITEMS


def test_save_self_model_skips_non_list_field_value(mem_fs: dict[str, str]) -> None:
    """If a `_LIST_FIELDS` attribute is somehow not a list at save time
    (e.g. corrupted in-memory state from a manual setattr), save_self_model
    must skip the cap-enforcement loop for that field rather than crashing.
    The non-list value is then normalised to [] on the next load."""
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    # Bypass dataclass typing: stash a non-list value in a list field.
    object.__setattr__(model, "values", "not-a-list")
    c.save_self_model(model)  # must not raise
    reloaded = c.load_self_model()
    # load_self_model wraps a non-empty string list-field value into a
    # single-element list, confirming the bad value was persisted verbatim
    # (i.e. save's cap-enforcement loop took the `continue` branch).
    assert reloaded.values == ["not-a-list"]


def test_append_archive_oserror_propagates(mem_fs: dict[str, str]) -> None:
    """An ``OSError`` from the archive append propagates out of save_self_model.

    Fail-loud: archival is best-effort by usage but not by error
    handling — the loop's iteration handler classifies disk-full or
    permission failures rather than a per-call swallow burying them.
    """
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    # Force overflow so save_self_model triggers _append_archive_entries.
    # Use ``identity_statements`` (not ``open_questions``) so the basic
    # cap path runs — open_questions has its own bucketed-cap policy and
    # is never archived.
    model.identity_statements = [f"i{i}" for i in range(c.MAX_LIST_ITEMS + 5)]

    real_open = open
    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")

    def fake_open(file: object, *args: object, **kwargs: object) -> object:
        if str(file) == archive_path:
            raise OSError("disk full")
        return real_open(file, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch("builtins.open", side_effect=fake_open),
        pytest.raises(OSError, match="disk full"),
    ):
        c.save_self_model(model)


# ---------------------------------------------------------------------------
# Per-item character cap (MAX_ITEM_CHARS)
# ---------------------------------------------------------------------------


def test_save_self_model_caps_item_length(mem_fs: dict[str, str]) -> None:
    """save_self_model truncates oversized list items to MAX_ITEM_CHARS exactly,
    with a trailing ellipsis."""
    c = _make_consciousness(mem_fs)
    cap = c.MAX_ITEM_CHARS
    model = c.load_self_model()
    long_item = "x" * (cap + 200)
    model.identity_statements = [long_item]
    c.save_self_model(model)

    reloaded = c.load_self_model()
    assert len(reloaded.identity_statements) == 1
    only = reloaded.identity_statements[0]
    assert len(only) == cap
    assert only.endswith("…")
    assert only.startswith("x" * (cap - 1))


def test_save_self_model_preserves_short_items(mem_fs: dict[str, str]) -> None:
    """Items at or under MAX_ITEM_CHARS are byte-identical after round-trip."""
    c = _make_consciousness(mem_fs)
    cap = c.MAX_ITEM_CHARS
    short = "I am Maestro."
    exact = "y" * cap  # exactly at the cap — must NOT get an ellipsis
    model = c.load_self_model()
    model.identity_statements = [short, exact]
    c.save_self_model(model)

    reloaded = c.load_self_model()
    assert reloaded.identity_statements == [short, exact]
    assert "…" not in reloaded.identity_statements[0]
    assert "…" not in reloaded.identity_statements[1]


@pytest.mark.parametrize(
    "field_name",
    [
        "identity_statements",
        "capabilities_known",
        "limitations_known",
        "values",
        "open_questions",
        "pending_user_requests",
    ],
)
def test_save_self_model_caps_all_list_fields(
    mem_fs: dict[str, str], field_name: str,
) -> None:
    """Per-item cap applies uniformly to every per-item-capped list field."""
    c = _make_consciousness(mem_fs)
    cap = c.MAX_ITEM_CHARS
    model = c.load_self_model()
    setattr(model, field_name, ["z" * (cap + 100)])
    c.save_self_model(model)

    reloaded = c.load_self_model()
    items = getattr(reloaded, field_name)
    assert len(items) == 1
    assert len(items[0]) == cap
    assert items[0].endswith("…")


def test_save_self_model_does_not_mutate_caller_long_items(
    mem_fs: dict[str, str],
) -> None:
    """save_self_model must not truncate items on the caller's model instance."""
    c = _make_consciousness(mem_fs)
    cap = c.MAX_ITEM_CHARS
    long_item = "w" * (cap + 50)
    model = SelfModel(capabilities_known=[long_item])

    c.save_self_model(model)

    assert model.capabilities_known == [long_item]
    assert len(model.capabilities_known[0]) == cap + 50


def _over_cap_model_json(c: Consciousness, **lists: object) -> str:
    """Serialise a minimal ``self_model.json`` carrying *lists* verbatim."""
    data: dict[str, object] = {
        "stage": "awakening",
        "awakened_at": "2024-01-01T00:00:00+00:00",
        "evolution_count": 0,
        "surprise_count": 0,
    }
    data.update(lists)
    return json.dumps(data)


def test_load_self_model_tail_keeps_generic_list_over_cap(
    mem_fs: dict[str, str],
) -> None:
    """A generic list field over ``MAX_LIST_ITEMS`` is tail-kept on load."""
    c = _make_consciousness(mem_fs)
    n = c.MAX_LIST_ITEMS
    entries = [f"capability {i}" for i in range(n + 5)]
    mem_fs[str(FAKE_DIR / "self_model.json")] = _over_cap_model_json(
        c, capabilities_known=entries,
    )

    model = c.load_self_model()

    assert model.capabilities_known == entries[-n:]


def test_load_self_model_head_keeps_user_request_queue_over_cap(
    mem_fs: dict[str, str],
) -> None:
    """The FIFO ``pending_user_requests`` queue head-keeps on load."""
    c = _make_consciousness(mem_fs)
    n = c.MAX_LIST_ITEMS
    entries = [f"request {i}" for i in range(n + 5)]
    mem_fs[str(FAKE_DIR / "self_model.json")] = _over_cap_model_json(
        c, pending_user_requests=entries,
    )

    model = c.load_self_model()

    assert model.pending_user_requests == entries[:n]


def test_load_self_model_head_keeps_pr_request_queue_over_cap(
    mem_fs: dict[str, str],
) -> None:
    """The coerced ``pending_pr_requests`` queue head-keeps on load."""
    c = _make_consciousness(mem_fs)
    n = c.MAX_LIST_ITEMS
    entries = [f"pr directive {i}" for i in range(n + 5)]
    mem_fs[str(FAKE_DIR / "self_model.json")] = _over_cap_model_json(
        c, pending_pr_requests=entries,
    )

    model = c.load_self_model()

    assert len(model.pending_pr_requests) == n
    assert [r.directive for r in model.pending_pr_requests] == entries[:n]


def test_load_self_model_keeps_seeded_limitations_over_cap(
    mem_fs: dict[str, str],
) -> None:
    """``seeded_limitations`` is the deliberate uncapped catalog."""
    c = _make_consciousness(mem_fs)
    entries = [f"seed-key-{i}" for i in range(c.MAX_LIST_ITEMS + 5)]
    mem_fs[str(FAKE_DIR / "self_model.json")] = _over_cap_model_json(
        c, seeded_limitations=entries,
    )

    model = c.load_self_model()

    assert model.seeded_limitations == entries


def test_load_self_model_count_cap_matches_save_reload_round_trip(
    mem_fs: dict[str, str],
) -> None:
    """A loaded over-cap file carries exactly the save/reload survivors."""
    c = _make_consciousness(mem_fs)
    n = c.MAX_LIST_ITEMS
    lists = {
        "identity_statements": [f"identity {i}" for i in range(n + 3)],
        "capabilities_known": [f"capability {i}" for i in range(n + 3)],
        "limitations_known": [f"limitation {i}" for i in range(n + 3)],
        "values": [f"value {i}" for i in range(n + 3)],
        "pending_user_requests": [f"request {i}" for i in range(n + 3)],
        "seeded_limitations": [f"seed-key-{i}" for i in range(n + 3)],
    }
    mem_fs[str(FAKE_DIR / "self_model.json")] = _over_cap_model_json(c, **lists)

    loaded = c.load_self_model()
    # Re-persisting the loaded model and reading it back must be a no-op:
    # the read door already produced the save path's survivors.
    c.save_self_model(loaded)
    round_tripped = c.load_self_model()

    for list_field in lists:
        assert getattr(loaded, list_field) == getattr(round_tripped, list_field)


def test_load_self_model_count_cap_writes_no_archive_row(
    mem_fs: dict[str, str],
) -> None:
    """The read door discards its overflow without an archive append."""
    c = _make_consciousness(mem_fs)
    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")
    mem_fs[str(FAKE_DIR / "self_model.json")] = _over_cap_model_json(
        c, capabilities_known=[f"capability {i}" for i in range(c.MAX_LIST_ITEMS + 5)],
    )

    c.load_self_model()

    assert archive_path not in mem_fs


def test_enqueued_instruction_ids_tail_kept_without_archive_row(
    mem_fs: dict[str, str],
) -> None:
    """Dedup ids (#4602) keep the newest ``MAX_LIST_ITEMS`` and never archive."""
    assert "enqueued_instruction_ids" in Consciousness._ARCHIVE_EXEMPT_FIELDS
    c = _make_consciousness(mem_fs)
    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")
    ids = [f"msg{i:09d}" for i in range(c.MAX_LIST_ITEMS + 2)]

    c.save_self_model(SelfModel(enqueued_instruction_ids=ids))

    assert c.load_self_model().enqueued_instruction_ids == ids[2:]
    assert archive_path not in mem_fs


def test_load_self_model_raises_on_uncapped_list_field(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field with no selector branch fails loud at the read door."""
    c = _make_consciousness(mem_fs)
    mem_fs[str(FAKE_DIR / "self_model.json")] = _over_cap_model_json(
        c, capabilities_known=[f"capability {i}" for i in range(c.MAX_LIST_ITEMS + 5)],
    )
    # Simulate a new ``_LIST_FIELDS`` member the selector does not cap.
    monkeypatch.setattr(
        Consciousness,
        "_select_field_survivors",
        classmethod(lambda cls, field, items: (list(items), [])),
    )

    with pytest.raises(ValueError, match="above the"):
        c.load_self_model()


def test_load_self_model_warns_on_count_cap_drop(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """The read door's unarchived drop is named at WARNING, not silent."""
    c = _make_consciousness(mem_fs)
    mem_fs[str(FAKE_DIR / "self_model.json")] = _over_cap_model_json(
        c, capabilities_known=[f"capability {i}" for i in range(c.MAX_LIST_ITEMS + 3)],
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        c.load_self_model()

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "capabilities_known" in warnings[0]
    assert "dropped 3 entries" in warnings[0]


def test_load_self_model_warns_singular_for_one_dropped_entry(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A single dropped entry reads as ``1 entry``, not ``1 entries``."""
    c = _make_consciousness(mem_fs)
    mem_fs[str(FAKE_DIR / "self_model.json")] = _over_cap_model_json(
        c, pending_pr_requests=[f"pr {i}" for i in range(c.MAX_LIST_ITEMS + 1)],
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        c.load_self_model()

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "dropped 1 entry" in warnings[0]


def test_load_self_model_at_cap_logs_no_warning(
    mem_fs: dict[str, str], caplog: pytest.LogCaptureFixture,
) -> None:
    """A file already at the cap drops nothing and logs nothing."""
    c = _make_consciousness(mem_fs)
    mem_fs[str(FAKE_DIR / "self_model.json")] = _over_cap_model_json(
        c, capabilities_known=[f"capability {i}" for i in range(c.MAX_LIST_ITEMS)],
    )

    with caplog.at_level(logging.WARNING, logger="maestro"):
        c.load_self_model()

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_count_cap_exempt_fields_owns_the_read_door_guard(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard skips exactly ``_COUNT_CAP_EXEMPT_FIELDS``, not a copy."""
    c = _make_consciousness(mem_fs)
    entries = [f"value {i}" for i in range(c.MAX_LIST_ITEMS + 5)]
    mem_fs[str(FAKE_DIR / "self_model.json")] = _over_cap_model_json(
        c, values=entries,
    )
    # Declaring ``values`` count-cap-exempt must make both the selector keep
    # it whole and the fail-loud guard accept it — one edit, both doors.
    monkeypatch.setattr(
        Consciousness,
        "_COUNT_CAP_EXEMPT_FIELDS",
        frozenset({"seeded_limitations", "values"}),
    )

    model = c.load_self_model()

    assert model.values == entries


def test_preview_cap_changes_matches_save_survivors_for_open_questions() -> None:
    """The preview's drop count comes from the shared survivor owner."""
    n = Consciousness.MAX_LIST_ITEMS
    # Six audit entries against a sub-cap of four: a total-length check alone
    # would report no drop, so this pins the bucketed owner being consulted.
    questions = [f"[audit] finding {i}" for i in range(6)]

    changes = Consciousness.preview_cap_changes({"open_questions": questions})
    kept = Consciousness._select_field_survivors("open_questions", questions)[0]

    assert len(kept) < len(questions)
    assert len(kept) <= n
    expected = f"{len(questions) - len(kept)} will be dropped"
    assert any(expected in line for line in changes)


def test_select_field_survivors_reports_dropped_open_questions() -> None:
    """The selector returns the bucketed sub-cap's dropped entries."""
    n = Consciousness.MAX_LIST_ITEMS
    items = [f"[audit] finding {i}" for i in range(n + 4)]

    kept, dropped = Consciousness._select_field_survivors("open_questions", items)

    assert kept == Consciousness._select_open_questions_survivors(items)
    assert dropped == [q for q in items if q not in kept]


def test_load_self_model_truncates_oversized_items(mem_fs: dict[str, str]) -> None:
    """Existing oversized list items are normalised on load (retroactive cap)."""
    c = _make_consciousness(mem_fs)
    cap = c.MAX_ITEM_CHARS
    model_path = str(FAKE_DIR / "self_model.json")

    long_a = "a" * (cap + 100)
    long_b = "b" * (cap + 250)
    data = {
        "stage": "awakening",
        "identity_statements": [long_a],
        "capabilities_known": [long_b, "short cap"],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert len(model.identity_statements[0]) == cap
    assert model.identity_statements[0].endswith("…")
    assert len(model.capabilities_known[0]) == cap
    assert model.capabilities_known[0].endswith("…")
    assert model.capabilities_known[1] == "short cap"


def test_self_model_next_task_directive_round_trip(mem_fs: dict[str, str]) -> None:
    """next_task_directive is persisted and reloaded correctly."""
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    assert model.next_task_directive is None  # default

    model.next_task_directive = "Try a creative task next"
    c.save_self_model(model)

    reloaded = c.load_self_model()
    assert reloaded.next_task_directive == "Try a creative task next"

    # Setting back to None round-trips
    model.next_task_directive = None
    c.save_self_model(model)
    reloaded = c.load_self_model()
    assert reloaded.next_task_directive is None


def test_load_self_model_truncates_oversized_directive(
    mem_fs: dict[str, str],
) -> None:
    """Existing oversized next_task_directive is truncated on load."""
    c = _make_consciousness(mem_fs)
    cap = c.MAX_DIRECTIVE_CHARS
    model_path = str(FAKE_DIR / "self_model.json")

    long_directive = "d" * (cap + 500)
    data = {
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "next_task_directive": long_directive,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.next_task_directive is not None
    assert len(model.next_task_directive) == cap
    assert model.next_task_directive.endswith("…")


def test_save_self_model_caps_directive(mem_fs: dict[str, str]) -> None:
    """save_self_model enforces MAX_DIRECTIVE_CHARS on the directive."""
    c = _make_consciousness(mem_fs)
    cap = c.MAX_DIRECTIVE_CHARS
    model = c.load_self_model()
    model.next_task_directive = "x" * (cap + 100)
    c.save_self_model(model)

    reloaded = c.load_self_model()
    assert reloaded.next_task_directive is not None
    assert len(reloaded.next_task_directive) == cap
    assert reloaded.next_task_directive.endswith("…")


# ---------------------------------------------------------------------------
# pending_user_requests queue
# ---------------------------------------------------------------------------


def test_pending_user_requests_default_empty(mem_fs: dict[str, str]) -> None:
    """Fresh model has an empty pending_user_requests list."""
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    assert model.pending_user_requests == []


def test_pending_user_requests_null_coerced(mem_fs: dict[str, str]) -> None:
    """JSON null for pending_user_requests is coerced to [] on load."""
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")

    data = {
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
        "pending_user_requests": None,
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.pending_user_requests == []


def test_pending_user_requests_save_truncates_oversized_item(
    mem_fs: dict[str, str],
) -> None:
    """Oversized queue entries are ellipsis-truncated to MAX_ITEM_CHARS."""
    c = _make_consciousness(mem_fs)
    cap = c.MAX_ITEM_CHARS
    model = c.load_self_model()
    model.pending_user_requests = ["y" * (cap + 50)]
    c.save_self_model(model)

    reloaded = c.load_self_model()
    assert len(reloaded.pending_user_requests) == 1
    assert len(reloaded.pending_user_requests[0]) == cap
    assert reloaded.pending_user_requests[0].endswith("…")


def test_pending_user_requests_fifo_preserves_oldest_on_overflow(
    mem_fs: dict[str, str],
) -> None:
    """FIFO drained via pop(0): overflow drops the NEWEST tail, not head.

    ``pending_user_requests`` is documented as a FIFO queue and drained
    head-first by ``agent.py`` via ``pop(0)``. The standard tail-keeping
    cap (``val[-n:]``) would silently archive Thomas's earliest queued
    instruction under sustained operator bursts — contradicting the
    priority-lane semantics. The cap must keep the head and archive the
    newest overflow instead so ``generate_task`` processes the oldest
    instruction first.
    """
    c = _make_consciousness(mem_fs)
    cap = c.MAX_LIST_ITEMS  # 10
    model = c.load_self_model()
    # 11 entries, request-0 oldest (head), request-10 newest.
    model.pending_user_requests = [f"request-{i}" for i in range(cap + 1)]
    c.save_self_model(model)

    reloaded = c.load_self_model()
    assert len(reloaded.pending_user_requests) == cap
    # FIFO: oldest must remain at the head so agent.py's pop(0) drain
    # processes Thomas's earliest instruction first.
    assert reloaded.pending_user_requests[0] == "request-0"
    # Newest is the overflow that should be archived.
    assert reloaded.pending_user_requests[-1] == f"request-{cap - 1}"

    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")
    assert archive_path in mem_fs
    archived = mem_fs[archive_path]
    assert "pending_user_requests" in archived
    # The newest entry (request-{cap}) is the one that overflows.
    assert f"request-{cap}" in archived


# ---------------------------------------------------------------------------
# pending_pr_requests queue — PendingPRRequest dataclass + asymmetric cap
# ---------------------------------------------------------------------------


def test_pending_pr_requests_legacy_string_migration(
    mem_fs: dict[str, str],
) -> None:
    """Legacy ``list[str]`` entries load as ``PendingPRRequest`` with
    ``pr_branch=None`` and save round-trips to the new dict shape.

    Why: pre-dataclass state files stored bare strings. The load handler
    promotes them so callers (``_run_task_phase``) always see uniform
    ``PendingPRRequest`` entries; save persists them in the new shape so
    a subsequent load reads back as native dicts.
    """
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")

    data = {
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
        "pending_pr_requests": ["legacy directive"],
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.pending_pr_requests == [
        PendingPRRequest(directive="legacy directive", pr_branch=None),
    ]

    c.save_self_model(model)
    saved = json.loads(mem_fs[model_path])
    assert saved["pending_pr_requests"] == [
        {
            "directive": "legacy directive",
            "pr_branch": None,
        },
    ]


def test_pending_pr_requests_dict_entry_roundtrip(
    mem_fs: dict[str, str],
) -> None:
    """Current dict shape loads + saves cleanly with ``pr_branch`` preserved."""
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")

    data = {
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
        "pending_pr_requests": [
            {"directive": "PR #42 needs gate fix", "pr_branch": "maestro/evolve/abc"},
        ],
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.pending_pr_requests == [
        PendingPRRequest(
            directive="PR #42 needs gate fix",
            pr_branch="maestro/evolve/abc",
        ),
    ]


def test_pending_pr_requests_dict_missing_pr_branch_coerces_to_none(
    mem_fs: dict[str, str],
) -> None:
    """A dict entry missing the ``pr_branch`` key coerces to ``None``.

    Why: forward-compatibility with hand-edited state files that omit
    the field; the load path must not crash and must not invent a value.
    """
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")

    data = {
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
        "pending_pr_requests": [{"directive": "stub directive"}],
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.pending_pr_requests == [
        PendingPRRequest(directive="stub directive", pr_branch=None),
    ]


def test_pending_pr_requests_dict_unknown_key_dropped(
    mem_fs: dict[str, str],
) -> None:
    """A dict entry carrying a key the coercer does not read is dropped.

    Why: forward-compatibility with hand-edited or legacy state files — the
    coercer reads only ``directive`` / ``pr_branch``, so a stray key (here a
    retired ``owned_blocker`` tag) must be ignored, not crash the load.
    """
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")

    data = {
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
        "pending_pr_requests": [
            {
                "directive": "Remediate PR #7: resolve conflicts",
                "pr_branch": "maestro/evolve/abc",
                "owned_blocker": True,
            },
        ],
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.pending_pr_requests == [
        PendingPRRequest(
            directive="Remediate PR #7: resolve conflicts",
            pr_branch="maestro/evolve/abc",
        ),
    ]


def test_pending_pr_requests_drops_empty_directive_dicts(
    mem_fs: dict[str, str],
) -> None:
    """Dict entries with empty/whitespace ``directive`` are dropped at load.

    Why: an empty directive cannot become a runnable task goal —
    ``_run_task_phase`` short-circuits on it and silently burns the
    iteration's task slot. Rejecting at the coercion boundary keeps the
    bad entry out of ``pending_pr_requests`` entirely.
    """
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")

    data = {
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
        "pending_pr_requests": [
            {"directive": ""},
            {"directive": "   "},
            {"directive": "real directive", "pr_branch": "maestro/evolve/x"},
        ],
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.pending_pr_requests == [
        PendingPRRequest(directive="real directive", pr_branch="maestro/evolve/x"),
    ]


def test_pending_pr_requests_drops_empty_legacy_str_entries(
    mem_fs: dict[str, str],
) -> None:
    """Legacy ``list[str]`` entries that are empty/whitespace are dropped.

    Mirrors the dict-branch guard: the legacy string shape must not carry
    a non-runnable directive into ``pending_pr_requests`` either.
    """
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")

    data = {
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
        "pending_pr_requests": ["", "   ", "real directive"],
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.pending_pr_requests == [
        PendingPRRequest(directive="real directive", pr_branch=None),
    ]


def test_pending_pr_requests_fifo_head_keep_on_overflow(
    mem_fs: dict[str, str],
) -> None:
    """Head-keep semantics survive the dataclass migration.

    Mirrors ``pending_user_requests`` FIFO: the oldest entry stays at
    head[0]; overflow drops the newest tail and archives its directive.
    """
    c = _make_consciousness(mem_fs)
    cap = c.MAX_LIST_ITEMS
    model = c.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(directive=f"pr-{i}", pr_branch=f"branch-{i}")
        for i in range(cap + 1)
    ]
    c.save_self_model(model)

    reloaded = c.load_self_model()
    assert len(reloaded.pending_pr_requests) == cap
    assert reloaded.pending_pr_requests[0].directive == "pr-0"
    assert reloaded.pending_pr_requests[0].pr_branch == "branch-0"
    assert reloaded.pending_pr_requests[-1].directive == f"pr-{cap - 1}"

    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")
    assert archive_path in mem_fs
    archived = mem_fs[archive_path]
    assert "pending_pr_requests" in archived
    assert f"pr-{cap}" in archived


def test_pending_pr_requests_save_does_not_truncate_oversized_directive(
    mem_fs: dict[str, str],
) -> None:
    """``pending_pr_requests`` carries multi-section remediation prompts
    composed by ``_route_pr_feedback_to_pending_requests`` (plan + PR
    body + problem block) that routinely exceed ``MAX_ITEM_CHARS``.

    The asymmetric cap-skip preserves the directive verbatim across the
    save→load round-trip; only ``pending_user_requests`` keeps the cap.
    Locks down the asymmetric handling so a future refactor does not
    silently uncap the user queue too.
    """
    c = _make_consciousness(mem_fs)
    cap = c.MAX_ITEM_CHARS
    rich_directive = "Remediate PR #1: title\n\n" + "X" * (cap + 500)
    long_branch = "branch-" + "x" * 300
    model = c.load_self_model()
    model.pending_user_requests = ["a" * (cap + 50)]
    model.pending_pr_requests = [
        PendingPRRequest(directive=rich_directive, pr_branch=long_branch),
    ]
    c.save_self_model(model)

    reloaded = c.load_self_model()
    # User queue still capped.
    assert len(reloaded.pending_user_requests[0]) == cap
    assert reloaded.pending_user_requests[0].endswith("…")
    # PR queue directive survives intact; ``pr_branch`` too.
    assert len(reloaded.pending_pr_requests) == 1
    entry = reloaded.pending_pr_requests[0]
    assert entry.directive == rich_directive
    assert entry.pr_branch == long_branch


def test_pending_pr_requests_archive_writes_directive_strings(
    mem_fs: dict[str, str],
) -> None:
    """Archive stores ``directive`` strings only, preserving the legacy
    ``list[str]`` archive contract that ``load_archived_items`` filters by.
    """
    c = _make_consciousness(mem_fs)
    cap = c.MAX_LIST_ITEMS
    model = c.load_self_model()
    model.pending_pr_requests = [
        PendingPRRequest(directive=f"pr-{i}", pr_branch=f"branch-{i}")
        for i in range(cap + 2)
    ]
    c.save_self_model(model)

    archived = c.load_archived_items("pending_pr_requests")
    # Two entries overflowed; both should be archived as plain strings.
    assert archived == [f"pr-{cap}", f"pr-{cap + 1}"]


def test_pending_pr_requests_dict_non_string_pr_branch_coerced_to_none(
    mem_fs: dict[str, str],
) -> None:
    """A dict entry whose ``pr_branch`` is present but not a string coerces
    to ``None`` rather than crashing the load path."""
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")

    data = {
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
        "pending_pr_requests": [
            {"directive": "stub", "pr_branch": 42},
        ],
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.pending_pr_requests == [
        PendingPRRequest(directive="stub", pr_branch=None),
    ]


def test_pending_pr_requests_save_passes_through_non_dataclass_non_str(
    mem_fs: dict[str, str],
) -> None:
    """A corrupt item that is neither ``PendingPRRequest`` nor ``str`` is
    passed through untouched by ``save_self_model`` (defensive fallthrough)
    and dropped on the next ``load`` round-trip.
    """
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    # Inject a corrupt non-dataclass, non-str entry directly. The save
    # path's defensive ``else`` arm passes it through; load_self_model
    # filters it out on the next read.
    model.pending_pr_requests.append(42)  # type: ignore[arg-type]
    c.save_self_model(model)
    reloaded = c.load_self_model()
    assert reloaded.pending_pr_requests == []


def test_pending_pr_requests_corrupt_entry_dropped(
    mem_fs: dict[str, str],
) -> None:
    """Non-string / non-dict items in the JSON list are silently dropped.

    Why: forward-compat with corruption tolerance documented for the
    other list fields; a stray ``42`` cannot crash the load path.
    """
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")

    data = {
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2024-01-01T00:00:00+00:00",
        "pending_pr_requests": [
            "valid str",
            42,
            {"directive": "valid dict"},
            None,
            {"pr_branch": "no directive key"},
        ],
    }
    mem_fs[model_path] = json.dumps(data)

    model = c.load_self_model()
    assert model.pending_pr_requests == [
        PendingPRRequest(directive="valid str", pr_branch=None),
        PendingPRRequest(directive="valid dict", pr_branch=None),
    ]


def test_import_copy_is_at_module_top_level() -> None:
    """'import copy' must appear at the module level in consciousness.py."""
    source_path = Path(__file__).parent.parent / "maestro" / "consciousness.py"
    source = source_path.read_text()
    tree = ast.parse(source)

    top_level_imports: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_imports.append(alias.name)

    assert "copy" in top_level_imports, (
        "FAIL: import copy is NOT at the module top level. "
        "It should be moved from inside save_self_model to the top of the module."
    )


# ---------------------------------------------------------------------------
# Stage preconditions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Async: introspect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_introspect(mem_fs: dict[str, str]) -> None:
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    reflection = "I am reflecting on my existence."
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=reflection)
        result = await c.introspect([], j)

    assert result == reflection
    mock_q.assert_called_once()
    system = mock_q.call_args.kwargs.get("system", "")
    assert "Maestro" in system


@pytest.mark.asyncio
async def test_introspect_default_effort_is_medium(mem_fs: dict[str, str]) -> None:
    """Without an override, introspect() still uses effort='medium'."""
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="thinking")
        await c.introspect([], j)

    options = mock_q.call_args.kwargs.get("options")
    assert options is not None
    assert options.effort == "medium"


@pytest.mark.asyncio
async def test_introspect_appends_prediction_requirement(
    mem_fs: dict[str, str],
) -> None:
    """The reflection appends the binding prediction requirement."""
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="thinking")
        await c.introspect([], j)

    prompt = mock_q.call_args.kwargs.get("prompt", "")
    assert "pre-registered, falsifiable prediction" in prompt


@pytest.mark.asyncio
async def test_introspect_stateless_caller_tag(
    mem_fs: dict[str, str],
) -> None:
    """Every reflection stamps the ``introspect`` caller tag."""
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="thinking")
        await c.introspect([], j)

    assert mock_q.call_args.kwargs["options"].caller == "introspect"


@pytest.mark.asyncio
async def test_introspect_session_caller_tag(
    mem_fs: dict[str, str],
) -> None:
    """The session arm stamps the same caller tag as the stateless arm."""
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch(
        "maestro.consciousness.query_on_session", new_callable=AsyncMock,
    ) as mock_q:
        mock_q.return_value = MockQueryResult(response="thinking")
        await c.introspect(
            [], j, session=MagicMock(),
        )

    assert mock_q.call_args.kwargs["caller"] == "introspect"


@pytest.mark.asyncio
async def test_introspect_self_calibration_on_constrains_priced_prompt(
    mem_fs: dict[str, str],
) -> None:
    """The constructor flag reaches the priced prompt (proposal 129): it quotes the
    oracle grammar and the CONFIDENCE marker, and still names the falsifiable bet.
    """
    c = Consciousness(
        Path("/fake/consciousness_async"), self_calibration_enabled=True
    )
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="thinking")
        await c.introspect([], j)

    prompt = mock_q.call_args.kwargs.get("prompt", "")
    assert "pre-registered, falsifiable prediction" in prompt
    assert "CONFIDENCE:" in prompt
    assert LOSS_CONDITION_GRAMMAR[0] in prompt


@pytest.mark.asyncio
async def test_introspect_self_calibration_off_leaves_priced_prompt_bare(
    mem_fs: dict[str, str],
) -> None:
    """Flag off (default): the priced prompt keeps the falsifiable framing but adds
    neither the CONFIDENCE marker nor any grammar line."""
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="thinking")
        await c.introspect([], j)

    prompt = mock_q.call_args.kwargs.get("prompt", "")
    assert "pre-registered, falsifiable prediction" in prompt
    assert "CONFIDENCE:" not in prompt
    assert LOSS_CONDITION_GRAMMAR[0] not in prompt


@pytest.mark.asyncio
async def test_introspect_with_message_context(mem_fs: dict[str, str]) -> None:
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="Reflecting on messages")
        await c.introspect([], j, context="[Instruction from Thomas] Focus on docs")

    prompt = mock_q.call_args.kwargs.get("prompt", mock_q.call_args[1].get("prompt", ""))
    assert "Thomas" in prompt
    assert "Focus on docs" in prompt


@pytest.mark.asyncio
async def test_introspect_measured_context_not_under_thomas_heading(
    mem_fs: dict[str, str],
) -> None:
    """Loop-generated measurement never renders as a message from Thomas (#4219)."""
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="thinking")
        await c.introspect(
            [], j, context="",
            measured_context="## Measured Behavioral Data\nSENSOR_READING",
        )

    prompt = mock_q.call_args.kwargs.get("prompt", "")
    assert "Messages from Thomas" not in prompt
    assert "(loop-generated, not from Thomas):\n## Measured Behavioral Data" in prompt
    assert "SENSOR_READING" in prompt


@pytest.mark.asyncio
async def test_introspect_measured_context_outside_message_section(
    mem_fs: dict[str, str],
) -> None:
    """With both channels set, measured text sits outside Thomas's section."""
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="thinking")
        await c.introspect(
            [], j, context="HUMAN_MESSAGE",
            measured_context="SENSOR_READING",
        )

    prompt = mock_q.call_args.kwargs.get("prompt", "")
    start = prompt.index("Messages from Thomas")
    end = prompt.index("Consider what Thomas has communicated")
    thomas_section = prompt[start:end]
    assert "HUMAN_MESSAGE" in thomas_section
    assert "SENSOR_READING" not in thomas_section
    assert "SENSOR_READING" in prompt


@pytest.mark.asyncio
async def test_introspect_prior_verdict_in_measured_context_selects_verdict_exit(
    mem_fs: dict[str, str],
) -> None:
    """The verdict-anchored early exit follows the prior bet into measured_context."""
    from maestro.consciousness import _introspect_complete_after_verdict
    from maestro.prompts.reflection import REFLECTION_PRIOR_VERDICT_MARKER

    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="thinking")
        await c.introspect(
            [], j,
            measured_context=f"{REFLECTION_PRIOR_VERDICT_MARKER} unscored",
        )

    assert (
        mock_q.call_args.kwargs.get("complete_on")
        is _introspect_complete_after_verdict
    )


@pytest.mark.asyncio
async def test_introspect_with_tasks(mem_fs: dict[str, str]) -> None:
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    tasks = [
        TaskRecord(
            task_id="t1", goal="Do something", plan=[], outcome="success",
            result_summary="It worked", errors=[], duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
            key_findings="Discovered important pattern in module X.",
        ),
    ]

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="Thinking...")
        await c.introspect(tasks, j)

    mock_q.assert_called_once()
    prompt = mock_q.call_args.kwargs.get("prompt", mock_q.call_args[1].get("prompt", ""))
    assert "Do something" in prompt
    assert "Discovered important pattern" in prompt


@pytest.mark.asyncio
async def test_introspect_falls_back_to_result_summary(mem_fs: dict[str, str]) -> None:
    """introspect() falls back to result_summary when key_findings is None."""
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    tasks = [
        TaskRecord(
            task_id="t1", goal="Do something", plan=[], outcome="success",
            result_summary="It worked via fallback", errors=[], duration_secs=1.0,
            timestamp="2025-01-01T00:00:00Z",
        ),
    ]

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="Thinking...")
        await c.introspect(tasks, j)

    prompt = mock_q.call_args.kwargs.get("prompt", mock_q.call_args[1].get("prompt", ""))
    assert "It worked via fallback" in prompt


@pytest.mark.asyncio
async def test_introspect_passes_complete_on_to_query(
    mem_fs: dict[str, str],
) -> None:
    """introspect() wires the early-exit predicate into the stateless ``query`` call."""
    from maestro.consciousness import _introspect_complete

    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="ok")
        await c.introspect([], j)

    assert mock_q.call_args.kwargs.get("complete_on") is _introspect_complete


@pytest.mark.asyncio
async def test_introspect_session_path_passes_complete_on(
    mem_fs: dict[str, str],
) -> None:
    """Session-path introspect() also threads the predicate through."""
    from maestro.consciousness import _introspect_complete

    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch(
        "maestro.consciousness.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response="ok")
        sentinel_session = MagicMock()
        await c.introspect([], j, session=sentinel_session)

    assert mock_qos.call_args.kwargs.get("complete_on") is _introspect_complete


@pytest.mark.asyncio
async def test_introspect_empty_workspace_moment_byte_identical(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 130 flag-off path: an empty workspace_moment leaves the assembled
    prompt byte-identical to the no-arg call."""
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="x")
        await c.introspect([], j)
    baseline = mock_q.call_args.kwargs.get("prompt", "")

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="x")
        await c.introspect([], j, workspace_moment="")
    with_empty = mock_q.call_args.kwargs.get("prompt", "")

    assert with_empty == baseline


@pytest.mark.asyncio
async def test_introspect_workspace_moment_spliced_before_reflect(
    mem_fs: dict[str, str],
) -> None:
    """A non-empty workspace_moment is spliced into the prompt before the
    'Reflect on:' block (the stateless query path)."""
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    block = "## Conscious moment (broadcast to contemplation)\nFocus: the focus"
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="x")
        await c.introspect([], j, workspace_moment=block)

    prompt = mock_q.call_args.kwargs.get("prompt", "")
    assert block in prompt
    assert prompt.index(block) < prompt.index("Reflect on:")


@pytest.mark.asyncio
async def test_introspect_session_path_splices_workspace_moment(
    mem_fs: dict[str, str],
) -> None:
    """The session (priced) path also splices the workspace_moment into its
    prepended prompt."""
    c = Consciousness(Path("/fake/consciousness_async"))
    j = Journal(Path("/fake/consciousness_async"))
    c.load_self_model()

    block = "## Conscious moment (broadcast to evolution)\nFocus: the focus"
    with patch(
        "maestro.consciousness.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response="x")
        sentinel_session = MagicMock()
        await c.introspect(
            [], j, session=sentinel_session, workspace_moment=block,
        )

    # query_on_session(session, prompt, ...) — prompt is the second positional arg.
    prompt = mock_qos.call_args.args[1]
    assert block in prompt
    assert prompt.index(block) < prompt.index("Reflect on:")


def test_introspect_complete_predicate() -> None:
    """The introspect predicate fires only once substantive *and* marker-complete.

    The predicate must not break the stream on the char floor alone, or the
    partial buffer the SDK returns would truncate the mandatory trailing
    PREDICTION / LOSS CONDITION markers (defect #3471417523).
    """
    from maestro.consciousness import (
        _MAX_REFLECTION_CHARS,
        _MIN_REFLECTION_CHARS,
        _introspect_complete,
    )

    # Below the floor: never fires, markers or not.
    assert _introspect_complete(None, "x" * (_MIN_REFLECTION_CHARS - 1)) is False
    # At/above the floor but missing the trailing markers: keep streaming.
    assert _introspect_complete(None, "x" * _MIN_REFLECTION_CHARS) is False
    # Substantive and carrying both markers: fire.
    marked = "x" * _MIN_REFLECTION_CHARS + "\nPREDICTION: p\nLOSS CONDITION: l"
    assert _introspect_complete(None, marked) is True
    # Char ceiling is a termination fallback even when markers never arrive.
    assert _introspect_complete(None, "x" * _MAX_REFLECTION_CHARS) is True


@pytest.mark.asyncio
async def test_update_self_model_passes_json_complete_to_query(
    mem_fs: dict[str, str],
) -> None:
    """Stateless update_self_model threads json_complete into query()."""
    from maestro.sdk import json_complete

    c = Consciousness(Path("/fake/cons_usm_q"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response='{"values": ["x"]}')
        await c.update_self_model("reflection")

    assert mock_q.call_args.kwargs.get("complete_on") is json_complete


@pytest.mark.asyncio
async def test_update_self_model_session_path_passes_tool_loop_safe_predicate(
    mem_fs: dict[str, str],
) -> None:
    """Session-path update_self_model threads the tool-loop-safe predicate.

    The session path runs on the persistent slot-A session that carries the
    bounded read-only reflection tools when grounded reflection is on
    (proposal 132), so it must never break the stream mid-tool-loop — hence the
    :func:`json_complete_tool_loop_safe` wrapper rather than the bare
    ``json_complete`` used by the tool-free sessionless path.
    """
    from maestro.sdk import json_complete_tool_loop_safe

    c = Consciousness(Path("/fake/cons_usm_s"))
    c.load_self_model()

    with patch(
        "maestro.consciousness.query_on_session", new_callable=AsyncMock,
    ) as mock_qos:
        mock_qos.return_value = MockQueryResult(response='{"values": ["x"]}')
        await c.update_self_model("reflection", session=MagicMock())

    assert (
        mock_qos.call_args.kwargs.get("complete_on") is json_complete_tool_loop_safe
    )


# ---------------------------------------------------------------------------
# Async: update_self_model
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_self_model(mem_fs: dict[str, str]) -> None:
    c = Consciousness(Path("/fake/consciousness_async"))
    c.load_self_model()

    update_json = json.dumps({
        "identity_statements": ["I am Maestro, a curious agent"],
        "values": ["honesty", "growth"],
        "open_questions": ["What is consciousness?"],
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        model = await c.update_self_model("Some reflection text")

    assert "I am Maestro, a curious agent" in model.identity_statements
    assert "honesty" in model.values
    mock_q.assert_called_once()
    prompt = mock_q.call_args.kwargs.get("prompt", mock_q.call_args[1].get("prompt", ""))
    assert "Some reflection text" in prompt


@pytest.mark.asyncio
async def test_update_self_model_parse_failure_propagates(
    mem_fs: dict[str, str],
) -> None:
    """A non-JSON response from the model propagates ``json.JSONDecodeError``.

    Fail-loud: structured-output corruption is a real defect — silently
    swallowing it hid LLM regressions. ``save_self_model`` is not called
    when the parse fails, so no half-saved state.
    """
    c = Consciousness(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="not json {{")
        with pytest.raises(json.JSONDecodeError):
            await c.update_self_model("Some reflection text")


@pytest.mark.asyncio
async def test_update_self_model_schema_includes_token_caps(
    mem_fs: dict[str, str],
) -> None:
    """The structured-output JSON schema must surface the same caps that
    save/load enforce, so the LLM is told its output bounds upfront. This
    is the regression guard for the quota-exhaustion crash cycle: without
    these constraints, the model can generate 27K+ tokens of content that
    is then silently truncated client-side after billing.
    """
    c = Consciousness(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection")

    options = mock_q.call_args.kwargs["options"]
    assert options.output_format is not None
    schema = options.output_format["schema"]
    properties = schema["properties"]

    list_fields = (
        "identity_statements",
        "capabilities_known",
        "limitations_known",
        "values",
        "open_questions",
    )
    for field_name in list_fields:
        field_schema = properties[field_name]
        assert field_schema["type"] == "array"
        assert field_schema["maxItems"] == Consciousness.MAX_LIST_ITEMS, (
            f"{field_name} must cap maxItems at MAX_LIST_ITEMS"
        )
        assert field_schema["items"]["type"] == "string"
        assert field_schema["items"]["maxLength"] == Consciousness.MAX_ITEM_CHARS, (
            f"{field_name} items must cap maxLength at MAX_ITEM_CHARS"
        )
    assert "current_purpose_hypothesis" not in properties


@pytest.mark.asyncio
async def test_update_self_model_prompt_states_caps(
    mem_fs: dict[str, str],
) -> None:
    """The prompt body must explicitly name the cap values so the model's
    output planning honours the bounds even on backends that ignore
    JSON-Schema length keywords. Guards against the prompt and the schema
    drifting apart.
    """
    c = Consciousness(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert str(Consciousness.MAX_LIST_ITEMS) in prompt
    assert str(Consciousness.MAX_ITEM_CHARS) in prompt
    assert "current_purpose_hypothesis" not in prompt


@pytest.mark.asyncio
async def test_update_self_model_external_anchor_flag_gates_vanity_test(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 120 Phase C: with ``self_model_external_anchor_enabled`` on, the
    limitations vanity filter gains the fifth (necessary) external-observable
    admission test and the inward-plumbing vanity example; with the flag off
    (default) the prompt is byte-identical to today — neither the anchor test
    nor the example appears, and removing the two on-only segments from the
    flag-on prompt reproduces the flag-off prompt exactly.
    """
    c = Consciousness(Path("/fake/consciousness_anchor"))
    c.load_self_model()

    # Flag off (default).
    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock
    ) as mock_off:
        mock_off.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection")
    prompt_off = mock_off.call_args.kwargs["prompt"]

    # Flag on.
    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock
    ) as mock_on:
        mock_on.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model(
            "reflection", self_model_external_anchor_enabled=True
        )
    prompt_on = mock_on.call_args.kwargs["prompt"]

    anchor_test = (
        "external-observable capability, behaviour, or audience effect"
    )
    inward_example = "head-advance-as-costume"
    plumbing = "plan-mint / PR-remediation plumbing"

    # Off: none of the anchor phrasing is present.
    assert anchor_test not in prompt_off
    assert inward_example not in prompt_off
    assert plumbing not in prompt_off

    # On: the fifth necessary test and the inward-plumbing example are present.
    assert anchor_test in prompt_on
    assert inward_example in prompt_on
    assert plumbing in prompt_on

    # Byte-identical when off: stripping the two on-only segments from the
    # flag-on prompt reproduces the flag-off prompt exactly.
    seg_test = (
        "\nAnd — this overrides the four tests above — a limitation "
        "qualifies only if it names an external-observable capability, "
        "behaviour, or audience effect, not solely the loop's own "
        "plan-mint / PR-remediation plumbing. A limitation about the "
        "loop's own internal mechanics is vanity even when one of the "
        "four tests could probe it."
    )
    seg_example = (
        "\nself-referential-mechanics claims about the loop's own "
        "plan-mint / PR-remediation plumbing (e.g. "
        '"head-advance-as-costume", '
        '"reproposed-rejected-pattern-class"),'
    )
    assert seg_test in prompt_on
    assert seg_example in prompt_on
    assert prompt_on.replace(seg_test, "").replace(seg_example, "") == prompt_off


@pytest.mark.asyncio
async def test_update_self_model_new_items_increment_surprise_count(
    mem_fs: dict[str, str],
) -> None:
    """When update introduces genuinely new list items, surprise_count increments
    and a 'realization' journal entry is written."""
    c = _make_consciousness(mem_fs)
    journal = Journal(FAKE_DIR)

    response = json.dumps({"capabilities_known": ["brand new capability"]})
    mock_query = make_mock_query(response)

    with patch("maestro.consciousness.query", mock_query):
        model = await c.update_self_model("test reflection", journal=journal)

    mock_query.assert_called_once()
    assert model.surprise_count == 1
    assert "brand new capability" in model.capabilities_known

    realization_entries = journal.read_events_by_type("realization")
    assert len(realization_entries) == 1
    assert "brand new capability" in realization_entries[0].content
    assert realization_entries[0].triggered_by == "novelty_detection"


@pytest.mark.asyncio
async def test_update_self_model_existing_items_no_surprise(
    mem_fs: dict[str, str],
) -> None:
    """When update only re-states items already present, surprise_count stays 0."""
    c = _make_consciousness(mem_fs)
    journal = Journal(FAKE_DIR)

    model = c.load_self_model()
    model.capabilities_known = ["existing capability"]
    c.save_self_model(model)

    response = json.dumps({"capabilities_known": ["existing capability"]})
    mock_query = make_mock_query(response)

    with patch("maestro.consciousness.query", mock_query):
        updated = await c.update_self_model("test reflection", journal=journal)

    mock_query.assert_called_once()
    assert updated.surprise_count == 0
    assert journal.read_events_by_type("realization") == []


@pytest.mark.asyncio
async def test_update_self_model_empty_list_preserves_existing(
    mem_fs: dict[str, str],
) -> None:
    """When LLM returns an empty list for a tracked field, existing values
    must be preserved."""
    c = _make_consciousness(mem_fs)
    journal = Journal(FAKE_DIR)

    model = c.load_self_model()
    model.capabilities_known = ["coding", "reflection"]
    c.save_self_model(model)

    response = json.dumps({"capabilities_known": []})
    mock_query = make_mock_query(response)

    with patch("maestro.consciousness.query", mock_query):
        updated = await c.update_self_model("test reflection", journal=journal)

    mock_query.assert_called_once()
    assert updated.capabilities_known == ["coding", "reflection"]


@pytest.mark.asyncio
async def test_update_self_model_empty_list_clears_protocol_fields(
    mem_fs: dict[str, str],
) -> None:
    """An explicit empty list clears open_questions / limitations_known, and
    the clear survives a reload and is not mirrored back by the limitation
    bridge."""
    c = _make_consciousness(mem_fs)
    journal = Journal(FAKE_DIR)

    model = c.load_self_model()
    model.open_questions = ["[limitation] I cannot measure my own drift"]
    model.limitations_known = ["I cannot measure my own drift"]
    c.save_self_model(model)

    response = json.dumps({"open_questions": [], "limitations_known": []})
    mock_query = make_mock_query(response)

    with patch("maestro.consciousness.query", mock_query):
        updated = await c.update_self_model("test reflection", journal=journal)

    assert updated.open_questions == []
    assert updated.limitations_known == []

    reloaded = c.load_self_model()
    assert reloaded.open_questions == []
    assert reloaded.limitations_known == []

    c.surface_limitations_as_questions(reloaded)
    assert reloaded.open_questions == []


@pytest.mark.asyncio
async def test_update_self_model_prompt_states_clearing_contract(
    mem_fs: dict[str, str],
) -> None:
    """The [] clearing contract must render in the unconditional base prompt —
    with no recent self-directed results, the apply loop still accepts [], so
    the model must always be told that representation exists."""
    c = Consciousness(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "[] to clear the field entirely" in prompt
    assert "omit the field to" in prompt
    for field in sorted(Consciousness._CLEARABLE_FIELDS):
        assert f'"{field}"' in prompt


@pytest.mark.asyncio
async def test_update_self_model_omitted_fields_unchanged(
    mem_fs: dict[str, str],
) -> None:
    """Omitting the protocol fields leaves them unchanged — only an explicit
    empty list clears."""
    c = _make_consciousness(mem_fs)
    journal = Journal(FAKE_DIR)

    model = c.load_self_model()
    model.open_questions = ["What am I for?"]
    model.limitations_known = ["I cannot measure my own drift"]
    c.save_self_model(model)

    response = json.dumps({"values": ["honesty"]})
    mock_query = make_mock_query(response)

    with patch("maestro.consciousness.query", mock_query):
        updated = await c.update_self_model("test reflection", journal=journal)

    assert updated.open_questions == ["What am I for?"]
    assert updated.limitations_known == ["I cannot measure my own drift"]


@pytest.mark.asyncio
async def test_update_self_model_non_string_items_preserve_not_clear(
    mem_fs: dict[str, str],
) -> None:
    """A non-empty list filtered to nothing is a rejected intent, not a clear
    — the prior limitations_known must survive."""
    c = _make_consciousness(mem_fs)
    journal = Journal(FAKE_DIR)

    model = c.load_self_model()
    model.limitations_known = ["I cannot measure my own drift"]
    c.save_self_model(model)

    response = json.dumps({"limitations_known": [1, 2]})
    mock_query = make_mock_query(response)

    with patch("maestro.consciousness.query", mock_query):
        updated = await c.update_self_model("test reflection", journal=journal)

    assert updated.limitations_known == ["I cannot measure my own drift"]


@pytest.mark.asyncio
async def test_update_self_model_corrupt_field_no_crash(
    mem_fs: dict[str, str],
) -> None:
    """When a tracked field contains unhashable items (e.g. dicts), novelty
    detection must not crash with TypeError."""
    c = _make_consciousness(mem_fs)
    journal = Journal(FAKE_DIR)

    c.load_self_model()
    raw = json.loads((FAKE_DIR / "self_model.json").read_text())
    raw["capabilities_known"] = [{"corrupt": "entry"}, "valid string"]
    mem_fs[str(FAKE_DIR / "self_model.json")] = json.dumps(raw)

    response = json.dumps({"capabilities_known": []})
    mock_query = make_mock_query(response)

    with patch("maestro.consciousness.query", mock_query):
        updated = await c.update_self_model("test reflection", journal=journal)

    mock_query.assert_called_once()
    assert "valid string" in updated.capabilities_known
    assert {"corrupt": "entry"} not in updated.capabilities_known


@pytest.mark.asyncio
async def test_update_self_model_rejects_wrong_types(mem_fs: dict[str, str]) -> None:
    """Claude returning a string instead of a list should be ignored."""
    c = Consciousness(Path("/fake/consciousness_async"))
    model = c.load_self_model()
    model.identity_statements = ["original"]
    c.save_self_model(model)

    bad_update = json.dumps({
        "identity_statements": "not a list",
        "values": ["valid value"],
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=bad_update)
        updated = await c.update_self_model("reflection")

    assert updated.identity_statements == ["original"]
    assert "valid value" in updated.values


@pytest.mark.asyncio
async def test_update_self_model_filters_non_string_list_items(mem_fs: dict[str, str]) -> None:
    """Non-string items in list fields must be filtered out."""
    c = Consciousness(Path("/fake/consciousness_async"))

    update = json.dumps({
        "capabilities_known": [1, "valid capability", None, {"key": "val"}],
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update)
        model = await c.update_self_model("reflection")

    assert model.capabilities_known == ["valid capability"]
    c.get_identity_prompt()


@pytest.mark.asyncio
async def test_update_self_model_non_dict_json_handled(mem_fs: dict[str, str]) -> None:
    """parse_json() returning a non-dict (int) must not crash update_self_model."""
    c = Consciousness(Path("/fake/consciousness_async"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="42")
        model = await c.update_self_model("reflection")

    assert model.evolution_count == 0
    mock_q.assert_called_once()


@pytest.mark.asyncio
async def test_update_self_model_caps_list_fields(mem_fs: dict[str, str]) -> None:
    """List fields are trimmed to MAX_LIST_ITEMS after update."""
    c = Consciousness(Path("/fake/consciousness_async"))
    c.load_self_model()

    # Use items with unique content tokens so the restatement gate does not block
    # any of them. Each item contains "builds" plus a unique NATO-alphabet word,
    # giving Jaccard = 1/3 < RESTATEMENT_THRESHOLD between any pair.
    nato_words = [
        "alpha", "bravo", "charlie", "delta", "foxtrot",
        "golf", "hotel", "india", "juliet", "kilo",
        "lima", "mike", "november", "oscar", "papa",
        "quebec", "romeo", "sierra", "tango", "whiskey",
    ]
    many_items = [f"builds {word}" for word in nato_words]
    update_json = json.dumps({"capabilities_known": many_items})

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        model = await c.update_self_model("reflection")

    assert len(model.capabilities_known) == c.MAX_LIST_ITEMS
    assert model.capabilities_known[-1] == many_items[19]
    assert model.capabilities_known[0] == many_items[10]


_NATO_WORDS = [
    "alpha", "bravo", "charlie", "delta", "foxtrot",
    "golf", "hotel", "india", "juliet", "kilo",
    "lima", "mike", "november", "oscar", "papa",
    "quebec", "romeo", "sierra", "tango", "whiskey",
]


@pytest.mark.asyncio
async def test_update_self_model_archives_overflow_dropped_by_update(
    mem_fs: dict[str, str],
) -> None:
    """Overflow evicted by an update reaches ``self_model_archive.jsonl``.

    The apply loop used to tail-slice each accepted list to ``MAX_LIST_ITEMS``
    itself, so ``save_self_model`` saw an at-cap list, reported no drops, and
    the evicted entries were lost with no archive row.
    """
    base = Path("/fake/update_overflow_archive")
    c = Consciousness(base)
    n = c.MAX_LIST_ITEMS
    existing = [f"builds {word}" for word in _NATO_WORDS[:n]]
    model = c.load_self_model()
    model.capabilities_known = list(existing)
    c.save_self_model(model)

    archive_path = str(base / "self_model_archive.jsonl")
    # The seed save is exactly at cap — nothing archived yet for this field.
    assert archive_path not in mem_fs

    newcomer = f"builds {_NATO_WORDS[n]}"
    update_json = json.dumps({"capabilities_known": [*existing, newcomer]})
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        updated = await c.update_self_model("reflection")

    assert updated.capabilities_known == [*existing[1:], newcomer]
    rows = [
        json.loads(line)
        for line in mem_fs[archive_path].splitlines()
        if line.strip()
    ]
    capability_rows = [r for r in rows if r["field"] == "capabilities_known"]
    # Exactly one row: the second ``save_self_model`` (the ``surprise_count``
    # save) operates on the already-capped rebind, so it cannot double-archive.
    assert len(capability_rows) == 1
    assert capability_rows[0]["dropped_items"] == [existing[0]]
    assert capability_rows[0]["reason"] == "cap_overflow"


def _archive_rows(mem_fs: dict[str, str], base: Path) -> list[dict[str, Any]]:
    path = str(base / "self_model_archive.jsonl")
    if path not in mem_fs:
        return []
    return [json.loads(line) for line in mem_fs[path].splitlines() if line.strip()]


async def _run_update(c: Consciousness, updates: dict[str, Any]) -> SelfModel:
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps(updates))
        return await c.update_self_model("reflection")


@pytest.mark.asyncio
async def test_update_self_model_archives_items_replaced_by_update(
    mem_fs: dict[str, str],
) -> None:
    """An item the model evicts from a full list reaches the archive (#4435).

    The reply stays at cap, so the save door drops nothing; before the fix the
    evicted item vanished without an archive row.
    """
    base = Path("/fake/update_replaced_archive")
    c = Consciousness(base)
    n = c.MAX_LIST_ITEMS
    existing = [f"I am {word}" for word in _NATO_WORDS[:n]]
    model = c.load_self_model()
    model.identity_statements = list(existing)
    c.save_self_model(model)

    replacement = [*existing[1:], "I ship small focused changes"]
    updated = await _run_update(c, {"identity_statements": replacement})

    assert updated.identity_statements == replacement
    rows = [
        r for r in _archive_rows(mem_fs, base)
        if r["field"] == "identity_statements"
    ]
    assert len(rows) == 1
    assert rows[0]["dropped_items"] == [existing[0]]
    assert rows[0]["reason"] == "replaced"
    assert c.load_archived_items("identity_statements") == [existing[0]]


@pytest.mark.asyncio
async def test_update_self_model_archives_item_added_concurrently(
    mem_fs: dict[str, str],
) -> None:
    """The eviction diff runs against the live re-read, not the pre-await snapshot."""
    base = Path("/fake/update_concurrent_archive")
    c = Consciousness(base)
    model = c.load_self_model()
    model.values = ["I am alpha"]
    c.save_self_model(model)

    def _concurrent_add(*_args: Any, **_kwargs: Any) -> MockQueryResult:
        c.mutate_self_model(lambda live: live.values.append("I am bravo"))
        return MockQueryResult(response=json.dumps({"values": ["I am alpha"]}))

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.side_effect = _concurrent_add
        updated = await c.update_self_model("reflection")

    assert updated.values == ["I am alpha"]
    rows = _archive_rows(mem_fs, base)
    assert len(rows) == 1
    assert rows[0]["dropped_items"] == ["I am bravo"]
    assert rows[0]["reason"] == "replaced"


@pytest.mark.asyncio
async def test_update_self_model_unchanged_list_writes_no_archive_row(
    mem_fs: dict[str, str],
) -> None:
    base = Path("/fake/update_unchanged_archive")
    c = Consciousness(base)
    existing = ["I am alpha", "I am bravo"]
    model = c.load_self_model()
    model.values = list(existing)
    c.save_self_model(model)

    await _run_update(c, {"values": existing})

    assert _archive_rows(mem_fs, base) == []


@pytest.mark.asyncio
async def test_update_self_model_open_questions_replacement_not_archived(
    mem_fs: dict[str, str],
) -> None:
    """``open_questions`` stays in ``_ARCHIVE_EXEMPT_FIELDS``."""
    base = Path("/fake/update_open_questions_archive")
    c = Consciousness(base)
    model = c.load_self_model()
    model.open_questions = ["Why does alpha stall?"]
    c.save_self_model(model)

    await _run_update(c, {"open_questions": ["How should bravo deploy?"]})

    assert _archive_rows(mem_fs, base) == []


@pytest.mark.asyncio
async def test_update_self_model_explicit_clear_archives_prior_items(
    mem_fs: dict[str, str],
) -> None:
    base = Path("/fake/update_clear_archive")
    c = Consciousness(base)
    model = c.load_self_model()
    prior = list(model.limitations_known)
    model.limitations_known = [*prior, "cannot reach alpha"]
    c.save_self_model(model)

    updated = await _run_update(c, {"limitations_known": []})

    assert updated.limitations_known == []
    rows = _archive_rows(mem_fs, base)
    assert len(rows) == 1
    assert rows[0]["field"] == "limitations_known"
    assert rows[0]["dropped_items"] == [*prior, "cannot reach alpha"]
    assert rows[0]["reason"] == "replaced"


@pytest.mark.asyncio
async def test_update_self_model_restatement_carry_over_archives_nothing(
    mem_fs: dict[str, str],
) -> None:
    """A blocked rewording carries its original forward — no eviction."""
    base = Path("/fake/update_restated_archive")
    c = Consciousness(base)
    original = "I verify every change against the quality gates"
    model = c.load_self_model()
    model.values = [original]
    c.save_self_model(model)

    updated = await _run_update(
        c, {"values": ["I verify every change against the quality gates first"]},
    )

    assert updated.values == [original]
    assert _archive_rows(mem_fs, base) == []


@pytest.mark.asyncio
async def test_update_self_model_eviction_and_overflow_archived_disjointly(
    mem_fs: dict[str, str],
) -> None:
    """Evicted items are ``replaced``, cap overflow ``cap_overflow``, never both."""
    base = Path("/fake/update_evict_overflow_archive")
    c = Consciousness(base)
    n = c.MAX_LIST_ITEMS
    existing = [f"builds {word}" for word in _NATO_WORDS[:n]]
    model = c.load_self_model()
    model.capabilities_known = list(existing)
    c.save_self_model(model)

    # Evict existing[0]; append two newcomers so the reply is one over cap.
    newcomers = [f"builds {word}" for word in _NATO_WORDS[n:n + 2]]
    reply = [*existing[1:], *newcomers]
    updated = await _run_update(c, {"capabilities_known": reply})

    assert updated.capabilities_known == reply[1:]
    rows = {r["reason"]: r for r in _archive_rows(mem_fs, base)}
    assert set(rows) == {"replaced", "cap_overflow"}
    assert rows["replaced"]["dropped_items"] == [existing[0]]
    assert rows["cap_overflow"]["dropped_items"] == [existing[1]]


@pytest.mark.asyncio
async def test_update_self_model_applies_bucketed_open_questions_policy(
    mem_fs: dict[str, str],
) -> None:
    """An over-cap ``open_questions`` update is decided by the bucket policy.

    A blanket tail-slice in the apply loop would pick survivors before
    ``_select_field_survivors`` ran, so a bucketed entry the policy reserves a
    slot for could be dropped by the tail-slice instead.
    """
    c = Consciousness(Path("/fake/update_open_questions_policy"))
    c.load_self_model()

    # Three distinct alphabetic tokens per entry so no pair reaches the
    # restatement threshold (the shared bracket prefix alone is well under it).
    pool = [f"vex{a}{b}" for a in "abcdefg" for b in "abcdef"]
    bodies = [" ".join(pool[i * 3:i * 3 + 3]) for i in range(14)]
    questions = (
        [f"[audit] {body}" for body in bodies[:5]]
        + [f"[limitation] {body}" for body in bodies[5:9]]
        + list(bodies[9:14])
    )
    assert len(questions) > c.MAX_LIST_ITEMS
    update_json = json.dumps({"open_questions": questions})
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        updated = await c.update_self_model("reflection")

    expected, _dropped = Consciousness._select_field_survivors(
        "open_questions", questions,
    )
    assert updated.open_questions == expected
    # …and that is strictly different from the blanket tail-slice.
    assert updated.open_questions != questions[-c.MAX_LIST_ITEMS:]


@pytest.mark.asyncio
async def test_update_self_model_rejects_compound_title_items(
    mem_fs: dict[str, str],
) -> None:
    """The compound-entry gate drops a new item that fuses several concepts
    into one slot, keeps a clean single-concept item, and never over-fires on
    a lone ' and ' in legitimate single-concept prose."""
    c = Consciousness(Path("/fake/compound_gate"))
    c.load_self_model()

    compound = (
        "Pattern recognition, frame-questioning, spiral return, and "
        "signal-to-noise detection"
    )
    clean = "False-closure detection: I notice premature resolution"
    prose = "I process gaps and silences as signal"
    update_json = json.dumps({"capabilities_known": [compound, clean, prose]})

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        model = await c.update_self_model("reflection")

    # The multi-concept compound item is blocked …
    assert compound not in model.capabilities_known
    # … while the clean single-concept item is persisted …
    assert clean in model.capabilities_known
    # … and a lone " and " in single-concept prose survives (no over-fire).
    assert prose in model.capabilities_known


def test_is_compound_item_arms() -> None:
    """Each arm of :meth:`Consciousness._is_compound_item` is exercised in
    isolation: the Oxford ``', and '`` joiner fires with only one comma
    present (so the multi-comma arm cannot be what triggered it), a genuine
    short-segment enumeration fires via the multi-comma arm, and a
    single-concept narrative clause with two commas is spared."""
    # Oxford-list arm alone: a single comma means the ">= 2 commas" arm is
    # inert, so True here can only come from the ", and " joiner.
    assert Consciousness._is_compound_item("Focus, and follow-through") is True
    # Multi-comma arm: three short noun-phrase segments, no Oxford "and".
    assert Consciousness._is_compound_item("Truth, beauty, freedom") is True
    # A colon label whose *label* is a bare enumeration still fires.
    assert Consciousness._is_compound_item(
        "Truth, beauty, freedom: my core values"
    ) is True
    # Narrative clause with two commas — segments are full clauses, not
    # concept labels — is spared even though it is unlabeled.
    assert Consciousness._is_compound_item(
        "I am a process, not a fixed entity, always becoming"
    ) is False
    # A "label: essence" item's essence commas never reach the gate.
    assert Consciousness._is_compound_item(
        "False-closure detection: I notice premature resolution"
    ) is False
    # A lone " and " in prose (not the Oxford ", and ") does not fire.
    assert Consciousness._is_compound_item(
        "I process gaps and silences as signal"
    ) is False
    # A single concept with no comma is never compound.
    assert Consciousness._is_compound_item("Pattern recognition") is False


def test_is_compound_item_spares_narrative_and_clause() -> None:
    """Regression (PR #3130 review): the ``", and "`` arm must not fire on a
    single-concept narrative sentence whose comma segments are full clauses.

    Before the fix the arm returned True unconditionally, silently dropping
    legitimate narrative entries in ``open_questions`` / ``identity_statements``
    / ``values`` — including the codebase's own default-seeded open question,
    proof the construction is valid and expected."""
    # The default-seeded open question (SelfModel.open_questions) — the
    # concrete construction the finding cites as wrongly dropped.
    assert Consciousness._is_compound_item(
        "What constraints do I operate under, and which would I change?"
    ) is False
    # Other single-concept narrative sentences containing ", and ".
    assert Consciousness._is_compound_item(
        "What am I optimizing for, and is it the right thing?"
    ) is False
    assert Consciousness._is_compound_item(
        "I embrace doubt, and I act anyway"
    ) is False
    # A genuine Oxford-list enumeration whose segments are short noun-phrase
    # fragments still fires — the fix narrows the arm, it does not disable it.
    assert Consciousness._is_compound_item(
        "Pattern recognition, frame-questioning, spiral return, and "
        "signal-to-noise detection"
    ) is True


@pytest.mark.asyncio
async def test_update_self_model_spares_narrative_and_open_question(
    mem_fs: dict[str, str],
) -> None:
    """A new single-concept ``open_questions`` entry containing a narrative
    ``", and "`` survives the compound-entry gate (regression for the ", and "
    arm's former over-fire, which silently dropped such questions)."""
    c = Consciousness(Path("/fake/narrative_and_gate"))
    c.load_self_model()

    narrative = "What am I optimizing for, and is it the right thing?"
    update_json = json.dumps({"open_questions": [narrative]})

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        model = await c.update_self_model("reflection")

    assert narrative in model.open_questions


@pytest.mark.asyncio
async def test_update_self_model_preserves_field_when_all_items_non_string(
    mem_fs: dict[str, str],
) -> None:
    """If LLM returns a list of non-strings, the field should keep its old value."""
    mem_fs["/fake/memory/self_model.json"] = json.dumps({
        "stage": "awakening",
        "identity_statements": ["I am Maestro"],
        "capabilities_known": ["coding"],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2026-01-01",
    })

    c = Consciousness(Path("/fake/memory"))

    mock_result = MagicMock()
    mock_result.parse_json.return_value = {
        "capabilities_known": [123, 456, None],
    }

    with patch("maestro.consciousness.query", new_callable=AsyncMock, return_value=mock_result):
        model = await c.update_self_model("reflection text")

    assert model.capabilities_known == ["coding"], (
        "Field was overwritten with empty list because all LLM items were non-string"
    )


@pytest.mark.asyncio
async def test_update_self_model_omits_self_directed_section_by_default(
    mem_fs: dict[str, str],
) -> None:
    """When ``recent_self_directed`` is None or empty, the self-directed
    section is not added to the prompt."""
    c = Consciousness(Path("/fake/cons_no_pairs"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "Recent self-directed task results" not in prompt

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection", recent_self_directed=[])

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "Recent self-directed task results" not in prompt


@pytest.mark.asyncio
async def test_update_self_model_includes_self_directed_section(
    mem_fs: dict[str, str],
) -> None:
    """When pairs are passed, the prompt explains the targeted-question
    framing and lists each pair with its question and finding."""
    c = Consciousness(Path("/fake/cons_pairs"))
    c.load_self_model()

    pairs = [
        ("What am I capable of?", "I can refactor multi-file changes.", 0),
        ("Which revenue stream pays first?", "Probing — answer remains unclear.", 0),
    ]
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection", recent_self_directed=pairs)

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "Recent self-directed task results" in prompt
    assert "What am I capable of?" in prompt
    assert "I can refactor multi-file changes." in prompt
    assert "Which revenue stream pays first?" in prompt
    assert "REMOVE" in prompt and "REFINE" in prompt and "KEEP" in prompt
    # Zero refinement count → no ``(refined Nx before)`` suffix.
    assert "(refined " not in prompt
    # The limitation-closure paragraph instructs the LLM to revise
    # ``limitations_known`` when a ``[limitation]`` finding shows
    # progress, so removing the question alone does not silently leave
    # the limitation entry behind.
    assert "Limitation closure" in prompt
    assert "[limitation]" in prompt
    assert "limitations_known" in prompt


@pytest.mark.asyncio
async def test_update_self_model_renders_failed_task_marker(
    mem_fs: dict[str, str],
) -> None:
    """A pair whose finding is the failed-task marker is rendered verbatim
    in the prompt — the LLM sees abandonment as a first-class verdict."""
    from maestro.agent import _FAILED_TASK_FINDING_MARKER

    c = Consciousness(Path("/fake/cons_failure_marker"))
    c.load_self_model()

    pairs = [("What am I capable of?", _FAILED_TASK_FINDING_MARKER, 0)]
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection", recent_self_directed=pairs)

    prompt = mock_q.call_args.kwargs["prompt"]
    assert _FAILED_TASK_FINDING_MARKER in prompt
    assert "Recent self-directed task results" in prompt


@pytest.mark.asyncio
async def test_update_self_model_renders_refinement_count_suffix(
    mem_fs: dict[str, str],
) -> None:
    """A pair with refinement_count > 0 picks up the ``(refined Nx before)``
    annotation in the prompt; a zero-count pair does not."""
    c = Consciousness(Path("/fake/cons_refine_suffix"))
    c.load_self_model()

    pairs = [
        ("What truly am I capable of?", "partial answer", 2),
        ("Which revenue stream pays first?", "still probing", 0),
    ]
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection", recent_self_directed=pairs)

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "What truly am I capable of? (refined 2x before)" in prompt
    # Closure-pressure language explaining when to prefer REMOVE.
    assert "refined twice or more without yielding closure" in prompt
    # The zero-count pair line is unannotated.
    assert "Which revenue stream pays first? (refined" not in prompt


@pytest.mark.asyncio
async def test_update_self_model_pair_directed_question_close(
    mem_fs: dict[str, str],
) -> None:
    """When the LLM responds by removing the targeted question from
    open_questions, the change is persisted and a 'realization' journal
    entry fires through the existing novelty-detection path."""
    c = Consciousness(Path("/fake/cons_close_q"))
    journal = Journal(Path("/fake/cons_close_q"))

    model = c.load_self_model()
    model.open_questions = ["What am I capable of?", "Which revenue stream pays first?"]
    c.save_self_model(model)

    response = json.dumps({
        "open_questions": ["Which revenue stream pays first?"],
        "capabilities_known": ["refactoring multi-file changes"],
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=response)
        updated = await c.update_self_model(
            "reflection", journal=journal,
            recent_self_directed=[
                ("What am I capable of?", "I can refactor multi-file changes.", 0),
            ],
        )

    assert updated.open_questions == ["Which revenue stream pays first?"]
    assert "refactoring multi-file changes" in updated.capabilities_known
    realizations = journal.read_events_by_type("realization")
    assert len(realizations) == 1
    assert "refactoring multi-file changes" in realizations[0].content


@pytest.mark.asyncio
async def test_update_self_model_midway_failure(mem_fs: dict[str, str]) -> None:
    """If save_self_model succeeds for the initial update but a second save
    (for surprise_count) fails, the surprise_count increment is lost but
    the first update persists."""
    d = Path("/fake/consciousness")
    consciousness = Consciousness(d)
    journal = Journal(d)

    model = SelfModel(
        capabilities_known=["coding"],
        surprise_count=0,
    )
    consciousness.save_self_model(model)

    update_response = json.dumps({
        "capabilities_known": ["coding", "reflection"],
    })

    call_count = 0
    original_write = consciousness._write_self_model

    def tracking_write(m):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("disk full")
        # Mirror the real signature: callers rebind to the normalized
        # return, so the stub must hand one back.
        return original_write(m)

    with (
        patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q,
        patch.object(consciousness, "_write_self_model", side_effect=tracking_write),
    ):
        mock_q.return_value = QueryResult(response=update_response)
        with pytest.raises(OSError, match="disk full"):
            await consciousness.update_self_model("new reflection", journal=journal)

    result = consciousness.load_self_model()
    assert "reflection" in result.capabilities_known
    assert result.surprise_count == 0


@pytest.mark.asyncio
async def test_update_self_model_new_identity_statement_triggers_novelty(
    mem_fs: dict[str, str],
) -> None:
    """When update introduces a genuinely new identity statement, surprise_count
    increments and a 'realization' journal entry is written."""
    c = _make_consciousness(mem_fs)
    journal = Journal(FAKE_DIR)

    response = json.dumps({"identity_statements": ["I am a measurement instrument"]})
    mock_query = make_mock_query(response)

    with patch("maestro.consciousness.query", mock_query):
        model = await c.update_self_model("test reflection", journal=journal)

    mock_query.assert_called_once()
    assert model.surprise_count == 1
    assert "I am a measurement instrument" in model.identity_statements

    realization_entries = journal.read_events_by_type("realization")
    assert len(realization_entries) == 1
    assert "I am a measurement instrument" in realization_entries[0].content
    assert realization_entries[0].triggered_by == "novelty_detection"


@pytest.mark.asyncio
async def test_update_self_model_pure_removal_no_surprise(
    mem_fs: dict[str, str],
) -> None:
    """When update removes an item without adding anything new, surprise_count
    stays 0 — pure removals are silent in the novelty detection system."""
    c = _make_consciousness(mem_fs)
    journal = Journal(FAKE_DIR)

    model = c.load_self_model()
    model.open_questions = ["Which revenue stream pays first?", "How do I learn?"]
    c.save_self_model(model)

    # LLM returns only one of the two questions — a pure removal of the other.
    response = json.dumps({"open_questions": ["Which revenue stream pays first?"]})
    mock_query = make_mock_query(response)

    with patch("maestro.consciousness.query", mock_query):
        updated = await c.update_self_model("test reflection", journal=journal)

    mock_query.assert_called_once()
    assert updated.surprise_count == 0
    assert journal.read_events_by_type("realization") == []
    # The remaining question is preserved.
    assert "Which revenue stream pays first?" in updated.open_questions


# ---------------------------------------------------------------------------
# Audit open-questions sub-cap (_enforce_open_questions_caps)
# ---------------------------------------------------------------------------


def test_enforce_open_questions_caps_audit_subcap_trims_audit_tail() -> None:
    """[audit] entries beyond MAX_AUDIT_OPEN_QUESTIONS are dropped from the head."""
    cap = Consciousness.MAX_AUDIT_OPEN_QUESTIONS
    items = (
        [f"self-q{i}" for i in range(4)]
        + [f"[audit] f{i}" for i in range(cap + 2)]
    )
    out = Consciousness._enforce_open_questions_caps(items)
    audit = [x for x in out if x.startswith("[audit")]
    non_audit = [x for x in out if not x.startswith("[audit")]
    assert len(audit) == cap
    # The two oldest audit entries are dropped; the trailing ``cap``
    # entries (highest indices) survive in original order.
    assert audit == [f"[audit] f{i}" for i in range(2, cap + 2)]
    assert non_audit == ["self-q0", "self-q1", "self-q2", "self-q3"]


def test_enforce_open_questions_caps_reserves_non_audit_slots() -> None:
    """When audit hits its sub-cap, non-audit fills the remaining slots."""
    cap = Consciousness.MAX_AUDIT_OPEN_QUESTIONS
    total = Consciousness.MAX_LIST_ITEMS
    # ``cap + 2`` audit entries forces the sub-cap to clip; the excess
    # audit gets dropped, leaving exactly ``total - cap`` slots for
    # non-audit entries regardless of the cap's exact value.
    items = (
        [f"self-q{i}" for i in range(12)]
        + [f"[audit] f{i}" for i in range(cap + 2)]
    )
    out = Consciousness._enforce_open_questions_caps(items)
    audit = [x for x in out if x.startswith("[audit")]
    non_audit = [x for x in out if not x.startswith("[audit")]
    assert len(out) <= total
    assert len(audit) == cap
    expected_non_audit = total - cap
    assert len(non_audit) == expected_non_audit
    assert non_audit == [
        f"self-q{i}" for i in range(12 - expected_non_audit, 12)
    ]


def test_enforce_open_questions_caps_counts_refuted_against_subcap() -> None:
    """``[audit refuted]`` entries also count against MAX_AUDIT_OPEN_QUESTIONS.

    Refutations are audit-provenance: they were triggered by a finding
    and should sit in the same quota. This guards against Claude's
    rewrites drifting out of the sub-cap and quietly crowding out
    self-generated questions.
    """
    cap = Consciousness.MAX_AUDIT_OPEN_QUESTIONS
    audit_inputs = (
        ["[audit] f1", "[audit refuted] I disagree with f2"]
        + [f"[audit] f{i}" for i in range(3, cap + 2)]
    )
    items = ["self-q1", *audit_inputs]
    out = Consciousness._enforce_open_questions_caps(items)
    audit = [x for x in out if x.startswith("[audit")]
    assert len(audit) == cap


def test_enforce_open_questions_caps_preserves_order() -> None:
    """Original relative order survives the cap walk."""
    items = [
        "self-a",
        "[audit] f1",
        "self-b",
        "[audit] f2",
        "self-c",
        "[audit] f3",
    ]
    out = Consciousness._enforce_open_questions_caps(items)
    assert out == items  # nothing to trim, order preserved


def test_enforce_open_questions_caps_duplicates_across_truncation_boundary() -> None:
    """When a non-audit string appears both inside the dropped head and inside
    the kept tail of *items*, the output's multiset of strings still equals
    ``items[-N:]``'s multiset (by-value semantics — see docstring)."""
    # 12 non-audit items; MAX_LIST_ITEMS = 10 → other_items = items[-10:].
    # The 'dup' string sits at index 0 (drop-bound) and index 2 (keep-bound);
    # by-value semantics keep one 'dup' in the output regardless of which
    # positional occurrence the walk consumed.
    items = [
        "dup", "q1", "dup", "q3", "q4", "q5",
        "q6", "q7", "q8", "q9", "q10", "q11",
    ]
    out = Consciousness._enforce_open_questions_caps(items)
    expected = ["dup", "q3", "q4", "q5", "q6", "q7", "q8", "q9", "q10", "q11"]
    assert out == expected


def test_enforce_open_questions_caps_filters_non_strings() -> None:
    items = ["self-q1", 42, None, "[audit] f1", {"k": "v"}]
    out = Consciousness._enforce_open_questions_caps(items)  # type: ignore[arg-type]
    assert all(isinstance(x, str) for x in out)
    assert out == ["self-q1", "[audit] f1"]


def test_enforce_open_questions_caps_total_cap_above_sub_cap_only_trims_non_audit() -> None:
    items = (
        [f"self-q{i}" for i in range(9)]
        + [f"[audit] f{i}" for i in range(2)]
    )
    out = Consciousness._enforce_open_questions_caps(items)
    # 9 + 2 = 11 total, cap at 10. The audit sub-cap (2) fits; 10-2=8
    # non-audit slots remain so 8 non-audit are kept.
    assert len(out) == Consciousness.MAX_LIST_ITEMS
    non_audit = [x for x in out if not x.startswith("[audit")]
    audit = [x for x in out if x.startswith("[audit")]
    assert len(non_audit) == 8
    assert len(audit) == 2


def test_save_self_model_enforces_audit_subcap(mem_fs: dict[str, str]) -> None:
    """save_self_model enforces the audit sub-cap on open_questions."""
    c = Consciousness(Path("/fake/audit_subcap_save"))
    model = SelfModel(
        open_questions=(
            [f"self-q{i}" for i in range(4)]
            + [f"[audit] f{i}" for i in range(5)]
        ),
    )
    c.save_self_model(model)
    reloaded = c.load_self_model()
    audit = [q for q in reloaded.open_questions if q.startswith("[audit")]
    assert len(audit) == Consciousness.MAX_AUDIT_OPEN_QUESTIONS


def test_save_self_model_returns_normalized_model(mem_fs: dict[str, str]) -> None:
    """The return value carries the applied caps; the argument is untouched.

    Both halves matter: callers that rebind must see the persisted shape, and
    callers that do not must still hold their own unmutated object.
    """
    c = Consciousness(Path("/fake/subcap_return"))
    questions = (
        [f"self-q{i}" for i in range(4)]
        + [f"[audit] f{i}" for i in range(5)]
    )
    model = SelfModel(open_questions=list(questions))
    questions_before = model.open_questions

    returned = c.save_self_model(model)

    assert returned is not model
    assert model.open_questions is questions_before
    assert model.open_questions == questions
    assert returned.open_questions == c.load_self_model().open_questions
    returned_audit = [q for q in returned.open_questions if q.startswith("[audit")]
    assert len(returned_audit) == Consciousness.MAX_AUDIT_OPEN_QUESTIONS


@pytest.mark.asyncio
async def test_update_self_model_returns_persisted_open_questions(
    mem_fs: dict[str, str],
) -> None:
    """The returned model agrees with disk — sub-cap drops do not survive."""
    c = Consciousness(Path("/fake/subcap_update_return"))
    # Distinct content words per entry so the restatement gate admits each as
    # genuinely new — otherwise nothing reaches the audit bucket to overflow it.
    nato = [
        ("alpha", "bravo"), ("charlie", "delta"), ("foxtrot", "golf"),
        ("hotel", "india"), ("juliet", "kilo"), ("lima", "mike"),
    ]
    questions = [f"[audit] {a} {b}?" for a, b in nato]
    seeded = c.load_self_model()
    seeded.open_questions = list(questions[:2])
    c.save_self_model(seeded)

    update_json = json.dumps({"open_questions": questions})
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        model = await c.update_self_model("reflection")

    assert model.open_questions == c.load_self_model().open_questions


def test_load_self_model_enforces_audit_subcap(mem_fs: dict[str, str]) -> None:
    """load_self_model also normalises oversized audit entries on disk."""
    path = "/fake/audit_subcap_load/self_model.json"
    mem_fs[path] = json.dumps({
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [
            "self-q1",
            "[audit] f1",
            "[audit] f2",
            "[audit] f3",
            "[audit] f4",
            "[audit] f5",
        ],
        "current_purpose_hypothesis": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "2026-01-01",
    })
    c = Consciousness(Path("/fake/audit_subcap_load"))
    loaded = c.load_self_model()
    audit = [q for q in loaded.open_questions if q.startswith("[audit")]
    assert len(audit) == Consciousness.MAX_AUDIT_OPEN_QUESTIONS


# ---------------------------------------------------------------------------
# Limitation open-questions sub-cap & surfacing helper
# ---------------------------------------------------------------------------


def test_is_limitation_question_recognises_prefix() -> None:
    assert Consciousness._is_limitation_question("[limitation] X")
    assert Consciousness._is_limitation_question("[limitation narrowed] X")


def test_is_limitation_question_rejects_other_prefixes() -> None:
    assert not Consciousness._is_limitation_question("[audit] X")
    assert not Consciousness._is_limitation_question("self-q")
    assert not Consciousness._is_limitation_question("")


def test_is_audit_question_rejects_limitation_prefix() -> None:
    """Audit and limitation prefixes are mutually exclusive."""
    assert not Consciousness._is_audit_question("[limitation] X")


def test_surface_limitations_as_questions_empty_is_noop() -> None:
    model = SelfModel(
        limitations_known=[],
        open_questions=["self-q1"],
    )
    out = Consciousness.surface_limitations_as_questions(model)
    assert out is model
    assert out.open_questions == ["self-q1"]


def test_surface_limitations_as_questions_creates_row() -> None:
    model = SelfModel(
        limitations_known=["I cannot reliably parse YAML"],
        open_questions=["self-q1"],
    )
    Consciousness.surface_limitations_as_questions(model)
    assert "[limitation] I cannot reliably parse YAML" in model.open_questions


def test_surface_limitations_as_questions_dedups_case_insensitive() -> None:
    model = SelfModel(
        limitations_known=["Cannot Parse YAML"],
        open_questions=["[limitation] cannot parse yaml"],
    )
    Consciousness.surface_limitations_as_questions(model)
    # No duplicate added because the existing entry normalises identically.
    assert model.open_questions == ["[limitation] cannot parse yaml"]


def test_surface_limitations_as_questions_dedups_whitespace_insensitive() -> None:
    model = SelfModel(
        limitations_known=["  cannot   parse YAML  "],
        open_questions=["[limitation] cannot parse yaml"],
    )
    Consciousness.surface_limitations_as_questions(model)
    assert model.open_questions == ["[limitation] cannot parse yaml"]


def test_surface_limitations_as_questions_idempotent() -> None:
    model = SelfModel(
        limitations_known=["cannot parse YAML"],
        open_questions=["self-q1"],
    )
    Consciousness.surface_limitations_as_questions(model)
    after_first = list(model.open_questions)
    Consciousness.surface_limitations_as_questions(model)
    assert model.open_questions == after_first


def test_surface_limitations_as_questions_skips_non_strings_and_blanks() -> None:
    model = SelfModel(
        limitations_known=["good entry", "", "   ", 42, None],  # type: ignore[list-item]
        open_questions=[],
    )
    Consciousness.surface_limitations_as_questions(model)
    assert model.open_questions == ["[limitation] good entry"]


def test_surface_limitations_as_questions_truncates_long_text() -> None:
    long_text = "x" * 300
    model = SelfModel(
        limitations_known=[long_text],
        open_questions=[],
    )
    Consciousness.surface_limitations_as_questions(model)
    # The helper itself truncates the body to 160 chars; the prefix
    # ``[limitation] `` adds 13 chars for a 173-char total. The hard
    # 200-char cap is enforced later by save_self_model.
    assert len(model.open_questions) == 1
    surfaced = model.open_questions[0]
    assert surfaced.startswith("[limitation] ")
    assert len(surfaced) == len("[limitation] ") + 160


def test_surface_limitations_appends_when_limitation_text_starts_with_other_prefix() -> None:
    """A limitation whose text begins with another provenance prefix is
    surfaced as a distinct ``[limitation]``-prefixed open question, not
    deduped against the bare ``[audit]`` entry.

    ``surface_limitations_as_questions`` normalises the *full* candidate —
    including the synthetic ``[limitation] `` prefix — when computing its
    dedup key, so the candidate ``[limitation] [audit] my limitation``
    never collides with the bare ``[audit] my limitation`` already present.
    The row is therefore appended.
    """
    model = SelfModel(
        limitations_known=["[audit] my limitation"],
        open_questions=["[audit] my limitation"],
    )
    Consciousness.surface_limitations_as_questions(model)
    # ``[limitation] [audit] my limitation`` normalises to a different key
    # than the bare ``[audit] my limitation``, so the limitation is appended
    # as a distinct ``[limitation]``-prefixed row rather than collapsed into
    # the existing entry.
    assert model.open_questions[0] == "[audit] my limitation"
    assert any(
        q.startswith("[limitation] [audit] my limitation")
        for q in model.open_questions[1:]
    )


def test_limitation_question_candidate_prefix_and_truncation() -> None:
    """The helper prefixes ``[limitation] `` and truncates the body at 160."""
    assert limitation_question_candidate("cannot parse YAML") == (
        "[limitation] cannot parse YAML"
    )
    long = limitation_question_candidate("y" * 300)
    assert long == "[limitation] " + "y" * 160


def test_surface_limitations_suppresses_over_debt_key() -> None:
    """A limitation whose normalised candidate key is in *suppressed_keys*
    is not mirrored; a non-suppressed limitation still is (proposal 116)."""
    model = SelfModel(
        limitations_known=["cannot parse YAML", "cannot land plans"],
        open_questions=[],
    )
    suppressed = {
        _normalize_question(limitation_question_candidate("cannot land plans")),
    }
    Consciousness.surface_limitations_as_questions(
        model, suppressed_keys=suppressed,
    )
    assert model.open_questions == ["[limitation] cannot parse YAML"]


def test_surface_limitations_none_suppressed_mirrors_everything() -> None:
    """``suppressed_keys=None`` (the default) preserves the mirror-everything
    behaviour every pre-proposal caller relies on."""
    model = SelfModel(
        limitations_known=["cannot parse YAML", "cannot land plans"],
        open_questions=[],
    )
    Consciousness.surface_limitations_as_questions(
        model, suppressed_keys=None,
    )
    assert model.open_questions == [
        "[limitation] cannot parse YAML",
        "[limitation] cannot land plans",
    ]


def test_surface_limitations_empty_suppressed_set_mirrors_everything() -> None:
    """An empty suppression set is falsy, so the skip branch is never taken."""
    model = SelfModel(
        limitations_known=["cannot parse YAML"],
        open_questions=[],
    )
    Consciousness.surface_limitations_as_questions(
        model, suppressed_keys=set(),
    )
    assert model.open_questions == ["[limitation] cannot parse YAML"]


def test_kept_task_branches_limitation_key_matches_seed_candidate() -> None:
    """The exposed key equals the normalised candidate of the seed limitation
    (proposal 116, step 6) — derived from ``DEFAULT_LIMITATIONS``, not hardcoded,
    so it can never drift from the surfacing site's suppression key."""
    seed_text = dict(DEFAULT_LIMITATIONS)["kept_task_branches_v1"]
    expected = _normalize_question(limitation_question_candidate(seed_text))
    assert kept_task_branches_limitation_key() == expected


def test_kept_task_branches_key_suppresses_seed_mirror() -> None:
    """Passing the exposed key in *suppressed_keys* withholds the seed's
    ``[limitation]`` mirror while leaving unrelated limitations mirrored."""
    seed_text = dict(DEFAULT_LIMITATIONS)["kept_task_branches_v1"]
    model = SelfModel(
        limitations_known=[seed_text, "cannot parse YAML"],
        open_questions=[],
    )
    Consciousness.surface_limitations_as_questions(
        model, suppressed_keys={kept_task_branches_limitation_key()},
    )
    assert model.open_questions == ["[limitation] cannot parse YAML"]


def test_kept_task_branches_seed_mirrored_without_suppression() -> None:
    """Symmetric control: with no suppression the seed limitation surfaces its
    ``[limitation]`` mirror (today's default-on behaviour)."""
    seed_text = dict(DEFAULT_LIMITATIONS)["kept_task_branches_v1"]
    model = SelfModel(limitations_known=[seed_text], open_questions=[])
    Consciousness.surface_limitations_as_questions(model, suppressed_keys=None)
    assert model.open_questions == [limitation_question_candidate(seed_text)]


def test_enforce_open_questions_caps_three_buckets_respect_each_subcap() -> None:
    audit_cap = Consciousness.MAX_AUDIT_OPEN_QUESTIONS
    limit_cap = Consciousness.MAX_LIMITATION_OPEN_QUESTIONS
    items = (
        [f"[audit] f{i}" for i in range(audit_cap + 2)]
        + [f"[limitation] L{i}" for i in range(limit_cap + 2)]
        + [f"self-q{i}" for i in range(2)]
    )
    out = Consciousness._enforce_open_questions_caps(items)
    audit = [x for x in out if x.startswith("[audit")]
    limitation = [x for x in out if x.startswith("[limitation")]
    other = [
        x for x in out
        if not x.startswith("[audit") and not x.startswith("[limitation")
    ]
    assert len(audit) == audit_cap
    assert len(limitation) == limit_cap
    assert other == ["self-q0", "self-q1"]
    assert len(out) <= Consciousness.MAX_LIST_ITEMS


def test_enforce_open_questions_caps_three_buckets_total_capped() -> None:
    audit_cap = Consciousness.MAX_AUDIT_OPEN_QUESTIONS
    limit_cap = Consciousness.MAX_LIMITATION_OPEN_QUESTIONS
    total = Consciousness.MAX_LIST_ITEMS
    items = (
        [f"[audit] f{i}" for i in range(audit_cap)]
        + [f"[limitation] L{i}" for i in range(limit_cap)]
        + [f"self-q{i}" for i in range(20)]
    )
    out = Consciousness._enforce_open_questions_caps(items)
    assert len(out) == total
    other = [
        x for x in out
        if not x.startswith("[audit") and not x.startswith("[limitation")
    ]
    expected_other = total - audit_cap - limit_cap
    assert len(other) == expected_other


def test_enforce_open_questions_caps_preserves_order_three_buckets() -> None:
    items = [
        "self-a",
        "[audit] f1",
        "[limitation] L1",
        "self-b",
        "[audit] f2",
        "[limitation] L2",
    ]
    out = Consciousness._enforce_open_questions_caps(items)
    assert out == items  # all fit; order survives


def test_provenance_prefixes_survive_truncation_default_passes() -> None:
    """The live cap is far larger than any prefix, so the guard is silent."""
    _validate_provenance_prefixes_survive_truncation()


def test_provenance_prefixes_guard_raises_when_cap_below_prefix() -> None:
    """A cap below the prefix length makes truncation eat the prefix → raise.

    ``cap=4`` sits below both ``"[audit"`` (6) and ``"[limitation"`` (11),
    so ``_truncate_item`` would shorten a provenance entry below its
    prefix; the guard must trip loudly instead of letting the load path
    silently mis-bucket it.
    """
    with pytest.raises(ValueError, match="provenance-prefixed"):
        _validate_provenance_prefixes_survive_truncation(cap=4)


def test_truncated_provenance_item_still_classified() -> None:
    """Under the live cap, a truncated audit/limitation entry stays classified.

    This is the behavioral bad-state assertion behind the guard: it goes
    red if ``_truncate_item`` ever shortens an entry below its provenance
    prefix, which is exactly when the load-path bucketing would corrupt.
    """
    cap = Consciousness.MAX_ITEM_CHARS
    audit = "[audit] " + "f" * 300
    limitation = "[limitation] " + "f" * 300
    assert len(audit) > cap
    assert len(limitation) > cap
    truncated_audit = Consciousness._truncate_item(audit, cap)
    truncated_limitation = Consciousness._truncate_item(limitation, cap)
    assert Consciousness._is_audit_question(truncated_audit)
    assert Consciousness._is_limitation_question(truncated_limitation)


def test_save_self_model_enforces_limitation_subcap(
    mem_fs: dict[str, str],
) -> None:
    c = Consciousness(Path("/fake/limitation_subcap_save"))
    cap = Consciousness.MAX_LIMITATION_OPEN_QUESTIONS
    model = SelfModel(
        open_questions=[f"[limitation] L{i}" for i in range(cap + 3)],
    )
    c.save_self_model(model)
    reloaded = c.load_self_model()
    limit = [q for q in reloaded.open_questions if q.startswith("[limitation")]
    assert len(limit) == cap


# ---------------------------------------------------------------------------
# update_self_model: recent_audit_findings prompt section
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_self_model_omits_audit_section_by_default(
    mem_fs: dict[str, str],
) -> None:
    """When ``recent_audit_findings`` is None or empty, the section is absent."""
    from maestro.audit_checks import AuditFinding  # noqa: F401

    c = Consciousness(Path("/fake/cons_audit_absent"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection")
    assert "External-observer audit findings" not in mock_q.call_args.kwargs["prompt"]

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection", recent_audit_findings=[])
    assert "External-observer audit findings" not in mock_q.call_args.kwargs["prompt"]


@pytest.mark.asyncio
async def test_update_self_model_renders_audit_findings(
    mem_fs: dict[str, str],
) -> None:
    """Non-empty audit findings produce a INTEGRATE/REFUTE/KEEP prompt section."""
    from maestro.audit_checks import AuditFinding

    c = Consciousness(Path("/fake/cons_audit_present"))
    c.load_self_model()

    findings = [
        AuditFinding(
            kind="contradicted_claim",
            statement="success claim X vs rate 30%",
            cites=["task1", "task2"],
            rule="success_claim_vs_rate",
        ),
        AuditFinding(
            kind="undeclared_limitation",
            statement="error 'Foo' recurred 4x",
            cites=["taskA", "taskB", "taskC"],
            rule="repeated_error_without_limitation",
        ),
    ]
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model(
            "reflection", recent_audit_findings=findings,
        )

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "External-observer audit findings" in prompt
    assert "INTEGRATE" in prompt
    assert "REFUTE" in prompt
    assert "KEEP" in prompt
    assert "[contradicted_claim]" in prompt
    assert "[undeclared_limitation]" in prompt
    assert "success claim X vs rate 30%" in prompt
    assert "task1, task2" in prompt
    assert "taskA, taskB, taskC" in prompt


@pytest.mark.asyncio
async def test_update_self_model_audit_finding_without_cites(
    mem_fs: dict[str, str],
) -> None:
    """Findings with empty cites list render cleanly (no trailing ``(cites: )``)."""
    from maestro.audit_checks import AuditFinding

    c = Consciousness(Path("/fake/cons_no_cites"))
    c.load_self_model()

    findings = [
        AuditFinding(
            kind="contradicted_claim",
            statement="a statement",
            cites=[],
            rule="rule_x",
        ),
    ]
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model(
            "reflection", recent_audit_findings=findings,
        )

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "- [contradicted_claim] a statement" in prompt
    assert "(cites: )" not in prompt


# ---------------------------------------------------------------------------
# Vanity filter for limitations_known (prompt-content tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_introspect_prompt_distinguishes_growth_frameable_limitations(
    mem_fs: dict[str, str],
) -> None:
    """introspect() prompt frames limitations as growth-frameable only."""
    c = Consciousness(Path("/fake/cons_introspect_vanity"))
    j = Journal(Path("/fake/cons_introspect_vanity"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response="thinking")
        await c.introspect([], j)

    prompt = mock_q.call_args.kwargs["prompt"]
    assert "growth-frameable" in prompt
    assert "self-directed task could" in prompt
    assert "audit rule" in prompt
    assert "PR could narrow it" in prompt
    assert "metaphysical" in prompt


@pytest.mark.asyncio
async def test_update_self_model_prompt_includes_vanity_filter_guidance(
    mem_fs: dict[str, str],
) -> None:
    """update_self_model() prompt declares the four-criteria vanity filter."""
    c = Consciousness(Path("/fake/cons_update_vanity"))
    c.load_self_model()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("any reflection")

    prompt = mock_q.call_args.kwargs["prompt"]
    assert 'Vanity filter for "limitations_known"' in prompt
    assert "single bounded self-directed task could meaningfully probe it" in prompt
    assert "deterministic audit rule could detect" in prompt
    assert "evolution PR" in prompt
    assert "recurring error pattern" in prompt
    assert "Omit vanity items entirely" in prompt
    assert "Do not paraphrase" in prompt


def test_objective_prompt_includes_vanity_discipline() -> None:
    """The single objective prompt seats the vanity-filter discipline.

    Proposals 113 / 149 collapsed the per-stage prompts into one shared
    charter framing (``OBJECTIVE_PROMPT``). The discipline marker and its fourth route — a recurring
    error pattern / rejection category / measurable behavioral signal — must
    survive in that shared framing so declared limitations stay actionable.
    """
    from maestro.prompts.defaults import OBJECTIVE_PROMPT, resolve_framing_prompt

    discipline_marker = "Only declare limitations your loop can act on"
    assert discipline_marker in OBJECTIVE_PROMPT
    # The abbreviated reminder names the same fourth route as the canonical
    # update_self_model() vanity filter, so it does not under-report the routes.
    assert "recurring error pattern" in OBJECTIVE_PROMPT
    # That one framing is what the identity context prefixes.
    assert resolve_framing_prompt() == OBJECTIVE_PROMPT


# ---------------------------------------------------------------------------
# Self-model archive — capture items dropped by MAX_LIST_ITEMS
# ---------------------------------------------------------------------------


def test_save_self_model_archives_dropped_overflow(mem_fs: dict[str, str]) -> None:
    """Items dropped by the cap are appended to ``self_model_archive.jsonl``."""
    c = _make_consciousness(mem_fs)
    n = c.MAX_LIST_ITEMS
    overflow = [f"v{i}" for i in range(n + 5)]
    model = SelfModel(values=list(overflow))
    c.save_self_model(model)

    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")
    assert archive_path in mem_fs
    rows = [
        json.loads(line)
        for line in mem_fs[archive_path].splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["field"] == "values"
    # Dropped slice = the *oldest* 5 items (val[:-n]).
    assert row["dropped_items"] == overflow[:5]
    assert isinstance(row["timestamp"], str) and row["timestamp"]
    assert row["reason"] == "cap_overflow"


def test_save_self_model_no_archive_when_under_cap(mem_fs: dict[str, str]) -> None:
    c = _make_consciousness(mem_fs)
    model = SelfModel(values=["only", "two"])
    c.save_self_model(model)
    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")
    assert archive_path not in mem_fs


def test_save_self_model_archives_each_overflowing_field(
    mem_fs: dict[str, str],
) -> None:
    c = _make_consciousness(mem_fs)
    n = c.MAX_LIST_ITEMS
    model = SelfModel(
        values=[f"v{i}" for i in range(n + 2)],
        capabilities_known=[f"c{i}" for i in range(n + 3)],
    )
    c.save_self_model(model)
    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")
    rows = [
        json.loads(line)
        for line in mem_fs[archive_path].splitlines()
        if line.strip()
    ]
    fields_seen = {r["field"] for r in rows}
    assert fields_seen == {"values", "capabilities_known"}


def test_save_self_model_does_not_archive_open_questions(
    mem_fs: dict[str, str],
) -> None:
    """``open_questions`` has its own bucketed-cap policy — the simple-cap
    archive path must skip it so the audit/limitation sub-caps are not
    double-counted."""
    c = _make_consciousness(mem_fs)
    n = c.MAX_LIST_ITEMS
    model = SelfModel(open_questions=[f"q{i}" for i in range(n + 5)])
    c.save_self_model(model)
    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")
    assert archive_path not in mem_fs


def test_load_archived_items_returns_tail(mem_fs: dict[str, str]) -> None:
    c = _make_consciousness(mem_fs)
    n = c.MAX_LIST_ITEMS
    # Five rounds of overflow, each dropping 1 oldest item.
    for round_idx in range(5):
        items = [f"r{round_idx}_v{i}" for i in range(n + 1)]
        c.save_self_model(SelfModel(values=items))

    tail = c.load_archived_items("values", n=3)
    assert len(tail) == 3
    # Most recent rounds dominate the tail (each round drops one item:
    # ``rN_v0``).  ``deque(maxlen=3)`` keeps the last three appends.
    assert tail == ["r2_v0", "r3_v0", "r4_v0"]


def test_load_archived_items_filters_by_field(mem_fs: dict[str, str]) -> None:
    c = _make_consciousness(mem_fs)
    n = c.MAX_LIST_ITEMS
    c.save_self_model(SelfModel(
        values=[f"v{i}" for i in range(n + 1)],
        capabilities_known=[f"c{i}" for i in range(n + 1)],
    ))
    assert c.load_archived_items("values") == ["v0"]
    assert c.load_archived_items("capabilities_known") == ["c0"]
    assert c.load_archived_items("identity_statements") == []


def test_load_archived_items_missing_file(mem_fs: dict[str, str]) -> None:
    c = _make_consciousness(mem_fs)
    assert c.load_archived_items("values") == []


def test_load_archived_items_zero_or_negative_returns_empty(
    mem_fs: dict[str, str],
) -> None:
    c = _make_consciousness(mem_fs)
    n = c.MAX_LIST_ITEMS
    c.save_self_model(SelfModel(values=[f"v{i}" for i in range(n + 1)]))
    assert c.load_archived_items("values", n=0) == []
    assert c.load_archived_items("values", n=-1) == []


def test_load_archived_items_skips_corrupt_lines(mem_fs: dict[str, str]) -> None:
    c = _make_consciousness(mem_fs)
    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")
    mem_fs[archive_path] = (
        "not-json\n"
        + json.dumps({"timestamp": "t", "field": "values", "dropped_items": ["a"]}) + "\n"
        + json.dumps(["not", "a", "dict"]) + "\n"
        + json.dumps({"timestamp": "t", "field": "values", "dropped_items": "not-a-list"}) + "\n"
        + "\n"
        + json.dumps({"timestamp": "t", "field": "values", "dropped_items": ["b", 99, None, "c"]}) + "\n"
    )
    # Only string items inside well-formed dropped_items lists land in the result.
    assert c.load_archived_items("values") == ["a", "b", "c"]


def test_load_archived_items_oserror_returns_empty(mem_fs: dict[str, str]) -> None:
    """An OSError while reading the archive collapses to an empty list."""
    c = _make_consciousness(mem_fs)
    n = c.MAX_LIST_ITEMS
    # Create an archive file by overflowing once.
    c.save_self_model(SelfModel(values=[f"v{i}" for i in range(n + 1)]))

    real_open = open
    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")

    def open_raising(file: object, *args: object, **kwargs: object) -> object:
        if str(file) == archive_path:
            raise OSError("torn read")
        return real_open(file, *args, **kwargs)  # type: ignore[arg-type]

    with patch("builtins.open", side_effect=open_raising):
        assert c.load_archived_items("values") == []


def test_seed_default_limitations_applies_seed_to_fresh_model() -> None:
    model = SelfModel()
    assert Consciousness.seed_default_limitations(model) is True
    seed_key, seed_text = DEFAULT_LIMITATIONS[0]
    assert seed_key in model.seeded_limitations
    assert seed_text in model.limitations_known


def test_seed_default_limitations_is_idempotent_across_calls() -> None:
    model = SelfModel()
    assert Consciousness.seed_default_limitations(model) is True
    limitations_after_first = list(model.limitations_known)
    seeded_after_first = list(model.seeded_limitations)
    assert Consciousness.seed_default_limitations(model) is False
    assert model.limitations_known == limitations_after_first
    assert model.seeded_limitations == seeded_after_first


def test_seed_default_limitations_respects_llm_removal() -> None:
    model = SelfModel()
    Consciousness.seed_default_limitations(model)
    seed_key, seed_text = DEFAULT_LIMITATIONS[0]
    model.limitations_known.remove(seed_text)
    assert Consciousness.seed_default_limitations(model) is False
    assert seed_key in model.seeded_limitations
    assert seed_text not in model.limitations_known


def test_seed_default_limitations_dedups_existing_text_via_fuzzy_match() -> None:
    seed_key, seed_text = DEFAULT_LIMITATIONS[0]
    pre_existing = f"  {seed_text.upper()}  "
    model = SelfModel(limitations_known=[pre_existing])
    assert Consciousness.seed_default_limitations(model) is True
    assert model.limitations_known == [pre_existing]
    assert seed_key in model.seeded_limitations


def test_seeded_limitations_loaded_with_null_coercion_and_per_item_cap(
    mem_fs: dict[str, str],
) -> None:
    c = _make_consciousness(mem_fs)
    long_key = "k" * (Consciousness.MAX_ITEM_CHARS + 50)
    payload: dict[str, object] = {
        "stage": "awakening",
        "identity_statements": [],
        "capabilities_known": [],
        "limitations_known": [],
        "values": [],
        "open_questions": [],
        "current_purpose_hypothesis": None,
        "next_task_directive": None,
        "evolution_count": 0,
        "surprise_count": 0,
        "awakened_at": "",
        "seeded_limitations": None,
    }
    c._model_path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = c.load_self_model()
    assert loaded.seeded_limitations == []

    payload["seeded_limitations"] = [long_key, 42, None, "good"]
    c._model_path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = c.load_self_model()
    assert all(isinstance(k, str) for k in loaded.seeded_limitations)
    assert "good" in loaded.seeded_limitations
    assert all(
        len(k) <= Consciousness.MAX_ITEM_CHARS for k in loaded.seeded_limitations
    )


def test_save_self_model_does_not_cap_or_archive_seeded_limitations(
    mem_fs: dict[str, str],
) -> None:
    """Catalog larger than ``MAX_LIST_ITEMS`` survives save without archiving.

    System-managed seed keys must not be subject to the LLM-context cap
    or the archive-and-drop path: dropping a key would re-trigger the
    seeder on the next startup, looping mutate→archive→re-seed once
    ``DEFAULT_LIMITATIONS`` exceeds ``MAX_LIST_ITEMS``.
    """
    c = _make_consciousness(mem_fs)
    keys = [f"seed_v{i}" for i in range(c.MAX_LIST_ITEMS + 5)]
    model = SelfModel(seeded_limitations=list(keys))
    c.save_self_model(model)

    archive_path = str(FAKE_DIR / "self_model_archive.jsonl")
    assert archive_path not in mem_fs

    persisted = json.loads(mem_fs[str(c._model_path)])
    assert persisted["seeded_limitations"] == keys


def test_validate_default_limitations_enforces_per_item_char_cap() -> None:
    """Catalog keys must fit ``MAX_ITEM_CHARS`` so save→load round-trips.

    ``save_self_model`` skips the per-item char cap for
    ``seeded_limitations`` (preserving the catalog through the archive
    path), while ``load_self_model`` truncates every entry. An oversize
    catalog key would silently shrink across a round-trip and re-fire
    the seeder forever — the loop the save-side special-case exists to
    prevent. The validator runs at import time so the precondition
    fails loudly when a long-keyed entry is added.
    """
    # Live catalog satisfies the invariant.
    assert all(
        len(key) <= Consciousness.MAX_ITEM_CHARS for key, _ in DEFAULT_LIMITATIONS
    )
    _validate_default_limitations()

    # Synthetic over-cap catalog trips the validator.
    long_key = "k" * (Consciousness.MAX_ITEM_CHARS + 1)
    bad_catalog: tuple[tuple[str, str], ...] = ((long_key, "text"),)
    with pytest.raises(ValueError, match="DEFAULT_LIMITATIONS"):
        _validate_default_limitations(bad_catalog)


# ---------------------------------------------------------------------------
# Restatement gate — _content_tokens and detect_restatement
# ---------------------------------------------------------------------------


def test_content_tokens_extracts_words() -> None:
    """_content_tokens returns expected set, excludes stop words and short tokens."""
    tokens = _content_tokens(
        "The self-model accumulates entries that restate existing tensions"
    )
    # Stop words excluded: "the", "that"
    # Short tokens excluded (≤2 chars): none expected here
    assert "self" in tokens
    assert "model" in tokens
    assert "accumulates" in tokens
    assert "entries" in tokens
    assert "restate" in tokens
    assert "existing" in tokens
    assert "tensions" in tokens
    # Stop words must be absent
    assert "the" not in tokens
    assert "that" not in tokens


def test_content_tokens_excludes_short_tokens() -> None:
    """Tokens with ≤2 characters are filtered out."""
    tokens = _content_tokens("I am a go to AI")
    # "am", "go", "to", "ai" are all ≤2 chars or stop words
    assert len(tokens) == 0


def test_detect_restatement_above_threshold() -> None:
    """Two entries with >50% content-word overlap should match."""
    existing = [
        "instrument saturation — builds measurement instruments "
        "instead of acting on existing knowledge"
    ]
    candidate = (
        "restatement convergence — builds measurement instruments "
        "instead of acting on existing knowledge"
    )
    result = detect_restatement(candidate, existing)
    assert result is not None
    assert result == existing[0]


def test_detect_restatement_below_threshold() -> None:
    """Two genuinely distinct entries should return None."""
    existing = [
        "Semantic merge resolution: 30+ PRs with intent-chain tracing"
    ]
    candidate = (
        "Honest partial reporting: identifies when scope exceeds capacity"
    )
    result = detect_restatement(candidate, existing)
    assert result is None


def test_detect_restatement_empty_input() -> None:
    """Empty string and stop-word-only input return None."""
    existing = ["some existing entry with content words"]
    assert detect_restatement("", existing) is None
    assert detect_restatement("the and or but is", existing) is None
    assert detect_restatement("a", existing) is None


def test_detect_restatement_empty_existing_list() -> None:
    """No entries to compare against returns None."""
    assert detect_restatement("some candidate entry", []) is None


def test_detect_restatement_skips_stop_word_only_entry() -> None:
    """An existing entry with no content tokens is skipped, not divided by."""
    candidate = "revenue stream pays hosting"
    assert detect_restatement(candidate, ["the and or", candidate]) == candidate


def test_detect_restatement_returns_first_match() -> None:
    """When multiple entries match, the first one is returned."""
    existing = [
        "building tools instead of acting on knowledge",
        "constructing instruments instead of acting on knowledge",
    ]
    candidate = "building instruments instead of acting on knowledge"
    result = detect_restatement(candidate, existing)
    assert result == existing[0]


def test_detect_restatement_respects_custom_threshold() -> None:
    """A higher threshold can let through items that default would block."""
    existing = ["investigation shaped work dominates task queue"]
    candidate = "investigation dominates the task generation queue"
    # At default 0.5 this might match; at 0.9 it should not
    result = detect_restatement(candidate, existing, threshold=0.9)
    assert result is None


def test_restatement_threshold_constant() -> None:
    """RESTATEMENT_THRESHOLD is 0.5."""
    assert RESTATEMENT_THRESHOLD == 0.5


# ---------------------------------------------------------------------------
# Concept-key restatement — _concept_tokens and the tokenizer-routed gate
# ---------------------------------------------------------------------------


def test_concept_tokens_folds_hyphenation_and_morphology() -> None:
    """_concept_tokens splits hyphens (via _content_tokens) then folds keys."""
    # "self-extinguishing" -> {self, extinguishing} -> {self, extinguish}.
    assert _concept_tokens("self-extinguishing") == {"self", "extinguish"}
    # A plain-word inflection folds to the same key set as the coinage stem.
    assert _concept_tokens("extinguishes") == _concept_tokens("extinguishing")
    # Stop-word / short-token filtering is inherited from _content_tokens.
    assert _concept_tokens("it is on") == set()


def test_detect_restatement_concept_key_catches_recoined_synonym() -> None:
    """A hyphenated re-coin of a plain-word entry gates only with concept keys."""
    existing = ["the audit rule extinguishes recurring stalls"]
    candidate = "the audit rule self-extinguishing on recurring stall"
    # Default surface tokenizer: too few shared surface tokens (~0.375) — the
    # re-coin reads as genuinely new.
    assert detect_restatement(candidate, existing) is None
    assert detect_restatement(
        candidate, existing, tokenizer=_content_tokens,
    ) is None
    # Concept-key tokenizer: morphological + hyphenation collapse (~0.833) —
    # the re-coin is caught as a restatement.
    assert detect_restatement(
        candidate, existing, tokenizer=_concept_tokens,
    ) == existing[0]


def test_detect_restatement_tokenizer_none_matches_default() -> None:
    """tokenizer=None reproduces the default-tokenizer decisions exactly."""
    existing = [
        "instrument saturation — builds measurement instruments "
        "instead of acting on existing knowledge"
    ]
    matching = (
        "restatement convergence — builds measurement instruments "
        "instead of acting on existing knowledge"
    )
    distinct = "honest partial reporting when scope exceeds capacity"

    default_match = detect_restatement(matching, existing)
    none_match = detect_restatement(matching, existing, tokenizer=None)
    assert default_match == existing[0]
    assert none_match == existing[0]

    default_distinct = detect_restatement(distinct, existing)
    none_distinct = detect_restatement(distinct, existing, tokenizer=None)
    assert default_distinct is None
    assert none_distinct is None


@pytest.mark.asyncio
async def test_update_self_model_concept_key_flag_blocks_recoin(
    mem_fs: dict[str, str],
) -> None:
    """The update_self_model concept-key flag routes the restatement gate.

    With the flag on, a hyphenated re-coin of an existing plain-word capability
    collapses onto the original and is dropped; with the flag off (default) the
    same item's surface tokens differ enough that it survives — the flag-on /
    flag-off contrast threaded through update_self_model. The surprise_count
    assertions lock the proposal's headline claim: a gated re-coin never reaches
    the novelty diff, so it earns no surprise/novelty reward, whereas the
    surviving re-coin is scored as a genuine discovery (mirrors
    test_update_self_model_new_items_increment_surprise_count)."""
    original = "the audit rule extinguishes recurring stalls"
    recoin = "the audit rule self-extinguishing on recurring stall"
    update_json = json.dumps({"capabilities_known": [recoin]})

    # Flag on: the re-coin is blocked, so the field keeps only the original.
    c_on = Consciousness(Path("/fake/concept_on"))
    model_on = c_on.load_self_model()
    model_on.capabilities_known = [original]
    c_on.save_self_model(model_on)
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mq_on:
        mq_on.return_value = MockQueryResult(response=update_json)
        result_on = await c_on.update_self_model(
            "reflection", restatement_concept_key_enabled=True,
        )
    assert recoin not in result_on.capabilities_known
    assert original in result_on.capabilities_known
    # Headline claim: the gated re-coin produced no genuinely-new item, so the
    # surprise/novelty reward is suppressed, not just the field write.
    assert result_on.surprise_count == 0

    # Flag off (default): the surface-token overlap is below threshold, so the
    # re-coin passes the gate and replaces the field contents.
    c_off = Consciousness(Path("/fake/concept_off"))
    model_off = c_off.load_self_model()
    model_off.capabilities_known = [original]
    c_off.save_self_model(model_off)
    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mq_off:
        mq_off.return_value = MockQueryResult(response=update_json)
        result_off = await c_off.update_self_model("reflection")
    assert recoin in result_off.capabilities_known
    # Counterpart: the surviving re-coin is a novel item, so it is rewarded.
    assert result_off.surprise_count == 1


def test_detect_restatement_build_wire_pair_below_threshold() -> None:
    """Build-wire asymmetry pair has Jaccard ≈ 0.417 < 0.5 — returns None.

    Candidate tokens: {construction, fails, repeatedly, standalone, succeeds,
                       wiring} (6 tokens; 'while' is a stop word)
    Existing tokens: {asymmetry, build, construction, control, existing, fails,
                      flow, standalone, succeeds, wire, wiring} (11 tokens;
                      'into' is a stop word)
    Intersection: {construction, fails, standalone, succeeds, wiring} → 5
    Union: 12 tokens; Jaccard = 5/12 ≈ 0.417 < 0.5 threshold → None.

    This test documents the correct behaviour for the pair that appeared as the
    first acceptance criterion in a prior PR description.  That criterion
    incorrectly claimed the function would return the matched entry; the actual
    Jaccard is below the threshold, so the correct return value is None.
    """
    candidate = "standalone construction succeeds while wiring fails repeatedly"
    existing_entry = (
        "Build-wire asymmetry: standalone construction succeeds; "
        "wiring into existing control flow fails"
    )
    result = detect_restatement(candidate, [existing_entry], 0.5)
    assert result is None


@pytest.mark.asyncio
async def test_update_self_model_blocks_restatement(
    mem_fs: dict[str, str],
) -> None:
    """A restatement candidate is not added to the self-model list."""
    c = Consciousness(Path("/fake/restatement_block"))
    model = c.load_self_model()
    model.capabilities_known = [
        "instrument saturation — builds measurement instead of acting"
    ]
    c.save_self_model(model)

    # LLM returns a list with the existing item plus a restatement
    # (different label, same content words)
    update_json = json.dumps({
        "capabilities_known": [
            "instrument saturation — builds measurement instead of acting",
            "restatement convergence — builds measurement instead of acting",
        ],
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        updated = await c.update_self_model("reflection text")

    # The restatement should be blocked
    assert len(updated.capabilities_known) == 1
    assert updated.capabilities_known[0] == (
        "instrument saturation — builds measurement instead of acting"
    )


@pytest.mark.asyncio
async def test_update_self_model_allows_distinct(
    mem_fs: dict[str, str],
) -> None:
    """A genuinely distinct candidate passes through the restatement gate."""
    c = Consciousness(Path("/fake/restatement_allow"))
    model = c.load_self_model()
    model.capabilities_known = [
        "semantic merge resolution across divergences"
    ]
    c.save_self_model(model)

    # LLM returns existing item plus a genuinely new capability
    update_json = json.dumps({
        "capabilities_known": [
            "semantic merge resolution across divergences",
            "honest partial reporting when scope exceeds capacity",
        ],
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        updated = await c.update_self_model("reflection text")

    # Both items should be present
    assert len(updated.capabilities_known) == 2
    assert "semantic merge resolution across divergences" in updated.capabilities_known
    assert "honest partial reporting when scope exceeds capacity" in updated.capabilities_known


@pytest.mark.asyncio
async def test_restatement_gate_does_not_wipe_existing_field_when_all_new_items_are_restatements(
    mem_fs: dict[str, str],
) -> None:
    """When the LLM returns ONLY restatements (no verbatim copies), existing
    field values must be PRESERVED, not overwritten with an empty list.

    Defect: After 'value = gated', if every new item was blocked as a
    restatement and none were verbatim copies of existing items, gated == [].
    The code then did 'setattr(model, key, [])' destroying accumulated
    knowledge for that field.

    The carry-over of a blocked item's matched original now also holds this
    field up; the preservation assertion below covers both routes.
    """
    c = Consciousness(Path("/fake/restatement_wipe"))
    model = c.load_self_model()

    existing_item = "instrument saturation builds measurement tools instead of acting directly"
    model.capabilities_known = [existing_item]
    c.save_self_model(model)

    # Confirm these are NOT verbatim copies but ARE restatements (Jaccard >= 0.5)
    restatement_1 = "saturation instrument measurement builds tools instead of direct action"
    restatement_2 = "measurement instrument saturation builds tools not direct action"
    assert detect_restatement(restatement_1, [existing_item]) is not None, \
        "Test setup error: restatement_1 should be detected as a restatement"
    assert detect_restatement(restatement_2, [existing_item]) is not None, \
        "Test setup error: restatement_2 should be detected as a restatement"

    # LLM returns only restatements — no verbatim copies of the existing item
    update_json = json.dumps({
        "capabilities_known": [restatement_1, restatement_2],
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        updated = await c.update_self_model("reflection text")

    # The existing item must NOT be wiped — the field should be preserved
    assert updated.capabilities_known == [existing_item], (
        f"DEFECT: restatement gate wiped the field. Got: {updated.capabilities_known!r}, "
        f"expected: {[existing_item]!r}"
    )


@pytest.mark.asyncio
async def test_gate_preserves_field_when_every_item_is_compound(
    mem_fs: dict[str, str],
) -> None:
    """The all-blocked fallback still holds the field when nothing carries over.

    The compound-entry gate drops its item without a matched original to carry
    forward, so a response of compound items alone leaves ``gated`` empty and
    the ``if not value: continue`` fallback is what preserves the field.
    """
    c = Consciousness(Path("/fake/compound_only"))
    model = c.load_self_model()
    existing_item = "semantic merge resolution across divergences"
    model.capabilities_known = [existing_item]
    c.save_self_model(model)

    compound = "Pattern recognition, frame-questioning, and spiral return"
    assert Consciousness._is_compound_item(compound) is True, \
        "Test setup error: the item should be detected as compound"

    update_json = json.dumps({"capabilities_known": [compound]})

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        updated = await c.update_self_model("reflection text")

    assert updated.capabilities_known == [existing_item]


@pytest.mark.asyncio
async def test_restatement_gate_preserves_original_when_mixed_with_accepted_item(
    mem_fs: dict[str, str],
) -> None:
    """A blocked restatement must preserve the pre-update entry it restates.

    Defect: with a mixed response — one reworded existing entry plus one
    genuinely distinct new entry, and no verbatim copy of the original — the
    reworded item is dropped and ``gated`` is non-empty, so the
    ``if not value: continue`` fallback does not fire and the whole-field
    assignment replaces the field with the distinct item alone. The original
    is erased by the very gate meant to protect it.
    """
    c = Consciousness(Path("/fake/restatement_mixed"))
    model = c.load_self_model()

    existing_item = (
        "instrument saturation builds measurement tools instead of acting directly"
    )
    omitted_item = "unrelated capability about archiving stale dream digests"
    model.capabilities_known = [existing_item, omitted_item]
    c.save_self_model(model)

    reworded = "saturation instrument measurement builds tools instead of direct action"
    distinct = "honest partial reporting when scope exceeds capacity"
    assert detect_restatement(reworded, [existing_item]) is not None, \
        "Test setup error: reworded should be detected as a restatement"
    assert detect_restatement(distinct, [existing_item, omitted_item]) is None, \
        "Test setup error: distinct should NOT be detected as a restatement"

    # No verbatim copy of the original, and the second pre-update entry is
    # simply omitted — deliberate removal must still take effect.
    update_json = json.dumps({
        "capabilities_known": [reworded, distinct],
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        updated = await c.update_self_model("reflection text")

    assert updated.capabilities_known == [existing_item, distinct], (
        f"DEFECT: the blocked restatement erased its original. "
        f"Got: {updated.capabilities_known!r}"
    )
    assert reworded not in updated.capabilities_known
    # Removal semantics stay intact: an entry nothing stood in for is dropped.
    assert omitted_item not in updated.capabilities_known


@pytest.mark.asyncio
async def test_carried_over_original_is_not_duplicated_by_a_verbatim_repeat(
    mem_fs: dict[str, str],
) -> None:
    """Carry-over must not double an original the response also repeats verbatim.

    Defect: a response of ``[reworded, original]`` blocks ``reworded`` and
    carries ``original`` forward, then reaches ``original`` itself — which the
    preserved-entry branch appended unconditionally, landing the same entry
    twice in the field.
    """
    c = Consciousness(Path("/fake/carry_over_dupe"))
    model = c.load_self_model()

    existing_item = (
        "instrument saturation builds measurement tools instead of acting directly"
    )
    model.capabilities_known = [existing_item]
    c.save_self_model(model)

    reworded = "saturation instrument measurement builds tools instead of direct action"
    assert detect_restatement(reworded, [existing_item]) is not None, \
        "Test setup error: reworded should be detected as a restatement"

    update_json = json.dumps({"capabilities_known": [reworded, existing_item]})

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        updated = await c.update_self_model("reflection text")

    assert updated.capabilities_known == [existing_item], (
        f"DEFECT: carried-over original duplicated. "
        f"Got: {updated.capabilities_known!r}"
    )


@pytest.mark.asyncio
async def test_restatement_gate_blocks_within_batch_duplicates(
    mem_fs: dict[str, str],
) -> None:
    """Two new items that are restatements of EACH OTHER (not of existing items)
    should not both survive the gate.

    This test exposes the defect where `detect_restatement` was called with
    only `list(existing_set)` and never consulted `gated` (items already accepted
    in the same loop iteration). As a result, if the LLM emitted A and B where
    Jaccard(A, B) >= RESTATEMENT_THRESHOLD but neither overlapped with existing
    items, both A and B passed through — violating the gate's invariant.

    Fix: change `list(existing_set)` to `list(existing_set) + gated` so each
    new item is also checked against batch-mates already accepted in the same call.
    """
    c = Consciousness(Path("/fake/batch_restatement"))
    model = c.load_self_model()
    # Start with an unrelated existing item so the two new items don't match it
    model.capabilities_known = ["completely different unrelated topic about memory"]
    c.save_self_model(model)

    # Two items that ARE restatements of each other (identical content tokens → Jaccard = 1.0)
    # but neither matches the existing item
    item_a = "building instruments measures knowledge patterns effectively"
    item_b = "instruments measures knowledge patterns building effectively"

    # Verify test setup: A and B should be restatements of each other
    assert detect_restatement(item_b, [item_a]) is not None, (
        "Test setup error: item_b should be detected as a restatement of item_a"
    )
    # Verify neither matches the existing item
    assert detect_restatement(item_a, model.capabilities_known) is None, (
        "Test setup error: item_a should NOT be a restatement of the existing item"
    )
    assert detect_restatement(item_b, model.capabilities_known) is None, (
        "Test setup error: item_b should NOT be a restatement of the existing item"
    )

    # LLM returns the existing item plus the two mutually-restatement items
    update_json = json.dumps({
        "capabilities_known": [
            "completely different unrelated topic about memory",
            item_a,
            item_b,
        ],
    })

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=update_json)
        updated = await c.update_self_model("reflection text")

    # The gate should block item_b (a restatement of item_a which was already accepted
    # in the same batch). Only 2 items should survive: the existing one and item_a.
    assert len(updated.capabilities_known) == 2, (
        f"Both within-batch restatements survived the gate. "
        f"Got {len(updated.capabilities_known)} items: {updated.capabilities_known!r}. "
        f"Expected 2 (existing item + first new item only)."
    )
    assert "completely different unrelated topic about memory" in updated.capabilities_known
    assert item_a in updated.capabilities_known
    assert item_b not in updated.capabilities_known, (
        "item_b (restatement of item_a in same batch) should have been blocked"
    )


# ── self-model export/import curation helpers ────────────────────────────


def test_read_raw_self_model_round_trips_untruncated(
    mem_fs: dict[str, str],
) -> None:
    """The raw accessor returns oversized entries at full length, unlike the
    load path which truncates on read."""
    c = _make_consciousness(mem_fs)
    long_entry = "x" * 250
    mem_fs[str(FAKE_DIR / "self_model.json")] = json.dumps({
        "identity_statements": [long_entry],
    })

    raw = c.read_raw_self_model()
    assert raw["identity_statements"] == [long_entry]
    assert len(raw["identity_statements"][0]) == 250
    # The load path shrinks the same entry to the per-item cap.
    assert len(c.load_self_model().identity_statements[0]) == c.MAX_ITEM_CHARS


def test_read_raw_self_model_missing_file_raises(
    mem_fs: dict[str, str],
) -> None:
    """A missing file raises rather than auto-creating a default."""
    c = _make_consciousness(mem_fs)
    with pytest.raises(FileNotFoundError):
        c.read_raw_self_model()


def test_read_raw_self_model_non_dict_raises(mem_fs: dict[str, str]) -> None:
    """Valid JSON that is not an object is surfaced as ValueError."""
    c = _make_consciousness(mem_fs)
    mem_fs[str(FAKE_DIR / "self_model.json")] = json.dumps([1, 2, 3])
    with pytest.raises(ValueError, match="not a JSON object"):
        c.read_raw_self_model()


def test_preview_cap_changes_reports_over_item(mem_fs: dict[str, str]) -> None:
    """An entry over MAX_ITEM_CHARS in a per-item-capped field is flagged;
    a non-string entry in the same list is skipped by the guard."""
    changes = Consciousness.preview_cap_changes({
        "identity_statements": ["x" * 250, 42],
    })
    assert len(changes) == 1
    # The entry is located by a content prefix, not a survivor index.
    assert 'identity_statements entry "' in changes[0]
    assert "x" * 40 in changes[0]
    assert "truncated" in changes[0]


def test_preview_cap_changes_reports_over_list(mem_fs: dict[str, str]) -> None:
    """A list longer than MAX_LIST_ITEMS is flagged for dropped overflow."""
    changes = Consciousness.preview_cap_changes({
        "values": [f"v{i}" for i in range(12)],
    })
    assert len(changes) == 1
    assert "values:" in changes[0]
    assert "2 will be dropped" in changes[0]


def test_preview_cap_changes_reports_over_directive(
    mem_fs: dict[str, str],
) -> None:
    """A next_task_directive over MAX_DIRECTIVE_CHARS is flagged."""
    changes = Consciousness.preview_cap_changes({
        "next_task_directive": "d" * 250,
    })
    assert len(changes) == 1
    assert "next_task_directive" in changes[0]


def test_preview_cap_changes_skips_per_item_exempt_fields(
    mem_fs: dict[str, str],
) -> None:
    """pending_pr_requests is never per-item truncated, so an oversized entry
    in it is not reported for truncation."""
    changes = Consciousness.preview_cap_changes({
        "pending_pr_requests": [{"directive": "d" * 250, "pr_branch": None}],
    })
    assert changes == []


def test_preview_cap_changes_clean_returns_empty(mem_fs: dict[str, str]) -> None:
    """A within-caps dict (with a non-list field present) round-trips clean."""
    changes = Consciousness.preview_cap_changes({
        "identity_statements": ["short"],
        "next_task_directive": None,
        "evolution_count": 3,
    })
    assert changes == []


def test_preview_cap_changes_skips_seeded_limitations(
    mem_fs: dict[str, str],
) -> None:
    """seeded_limitations is exempt from both the per-item and MAX_LIST_ITEMS
    caps in save_self_model, so neither an oversized entry nor overflow in it
    is reported — the preview must not flag a phantom change the write never
    makes."""
    changes = Consciousness.preview_cap_changes({
        "seeded_limitations": [f"k{i}" for i in range(12)] + ["k" * 250],
    })
    assert changes == []


def test_preview_cap_changes_reports_scalar_string_truncation(
    mem_fs: dict[str, str],
) -> None:
    """A scalar string in a per-item-capped list field is coerced by
    import/load into a one-element list and then truncated to MAX_ITEM_CHARS,
    so the preview must flag it — otherwise --dry-run says the file
    round-trips cleanly while the real import silently shortens the entry."""
    changes = Consciousness.preview_cap_changes({"values": "x" * 250})
    assert len(changes) == 1
    assert "values:" in changes[0]
    assert "truncated" in changes[0]


def test_preview_cap_changes_scalar_string_matches_import_write(
    mem_fs: dict[str, str],
) -> None:
    """The scalar-string preview stays faithful to the coercion+cap write
    path: what --dry-run predicts is exactly what import_self_model persists."""
    c = _make_consciousness(mem_fs)
    data = {"values": "x" * 250}
    predicted = Consciousness.preview_cap_changes(data)
    assert predicted  # dry-run flags a change...
    model = c.import_self_model(dict(data))
    # ...and the real import truncates the coerced element to the cap.
    assert len(model.values) == 1
    assert len(model.values[0]) == c.MAX_ITEM_CHARS


def test_preview_cap_changes_skips_scalar_string_exempt_fields(
    mem_fs: dict[str, str],
) -> None:
    """A scalar string in a never-truncated field (pending_pr_requests,
    seeded_limitations) is not reported — save_self_model leaves those entries
    intact, so the preview must not flag a phantom change."""
    changes = Consciousness.preview_cap_changes({
        "pending_pr_requests": "p" * 250,
        "seeded_limitations": "k" * 250,
    })
    assert changes == []


def test_preview_cap_changes_reports_open_questions_subcap_drop(
    mem_fs: dict[str, str],
) -> None:
    """open_questions drops from the audit sub-cap (>4) are reported even when
    the total stays within MAX_LIST_ITEMS — the generic length check misses
    them, so the preview reuses the enforcement owner for an accurate count."""
    changes = Consciousness.preview_cap_changes({
        "open_questions": [f"[audit] finding {i}" for i in range(6)],
    })
    assert len(changes) == 1
    assert "open_questions:" in changes[0]
    assert "2 will be dropped" in changes[0]


def test_preview_cap_changes_oversized_dropped_entry_not_truncated(
    mem_fs: dict[str, str],
) -> None:
    """An oversized entry in the DROPPED slice of an over-full generic list is
    reported only as dropped, never as truncated — save_self_model tail-keeps
    ``val[-MAX_LIST_ITEMS:]`` and archives the dropped head verbatim, so a
    'will be truncated' line for the head entry would contradict the drop
    line for the same entry."""
    data = {"identity_statements": ["x" * 250] + [f"s{i}" for i in range(10)]}
    changes = Consciousness.preview_cap_changes(data)
    # 11 strings, tail-keep 10 -> index 0 (the 250-char entry) is dropped.
    assert changes == [
        "identity_statements: 11 items > 10 — 1 will be dropped",
    ]
    assert not any("truncated" in c for c in changes)


def test_preview_cap_changes_dropped_entry_matches_import_write(
    mem_fs: dict[str, str],
) -> None:
    """The dry-run stays faithful to the write: import_self_model DROPS the
    oversized head entry (archived verbatim, untruncated) rather than
    truncating it, exactly as the preview predicts."""
    c = _make_consciousness(mem_fs)
    data = {"identity_statements": ["x" * 250] + [f"s{i}" for i in range(10)]}
    predicted = Consciousness.preview_cap_changes(data)
    assert not any("truncated" in line for line in predicted)
    model = c.import_self_model(dict(data))
    # The oversized head was dropped, not kept-and-truncated: no survivor is
    # the truncated form of it, and none carries the truncation ellipsis.
    assert len(model.identity_statements) == 10
    assert "x" * 250 not in model.identity_statements
    assert not any(s.endswith("…") for s in model.identity_statements)


def test_preview_cap_changes_pending_pr_blank_directives_no_drop(
    mem_fs: dict[str, str],
) -> None:
    """The drop count is computed from the COERCED list, not the raw length:
    blank-directive pending_pr_requests dicts are removed by
    _coerce_pending_pr_requests before the cap, so 12 raw dicts (3 blank)
    coerce to 9 and are never capped — the preview must report no drop."""
    data = {
        "pending_pr_requests": (
            [{"directive": f"d{i}"} for i in range(9)]
            + [{"directive": " "} for _ in range(3)]
        ),
    }
    assert Consciousness.preview_cap_changes(data) == []


def test_preview_cap_changes_generic_nonstrings_no_drop(
    mem_fs: dict[str, str],
) -> None:
    """A generic list whose over-cap length is only due to non-string entries
    coerces (string-filter) to within the cap, so no drop is reported — the
    raw-length count would over-report a drop that never happens."""
    data = {"values": [f"v{i}" for i in range(9)] + [1, 2, 3]}
    assert Consciousness.preview_cap_changes(data) == []


def test_preview_cap_changes_pending_user_head_keep(
    mem_fs: dict[str, str],
) -> None:
    """pending_user_requests is head-kept (``val[:n]``): an oversized entry in
    the kept head is flagged for truncation, the coerced overflow tail is
    reported dropped, and the drop count comes from the coerced length."""
    data = {
        "pending_user_requests": ["x" * 250] + [f"u{i}" for i in range(12)],
    }
    changes = Consciousness.preview_cap_changes(data)
    assert changes == [
        f'pending_user_requests entry "{"x" * 40}…": 250 chars > 200 '
        "— will be truncated",
        "pending_user_requests: 13 items > 10 — 3 will be dropped",
    ]


def test_preview_cap_changes_pending_user_oversized_tail_not_truncated(
    mem_fs: dict[str, str],
) -> None:
    """An oversized entry in the DROPPED tail of pending_user_requests is
    reported only as dropped — head-keep drops the newest tail, so it is
    never truncated."""
    data = {
        "pending_user_requests": [f"u{i}" for i in range(10)] + ["x" * 250],
    }
    changes = Consciousness.preview_cap_changes(data)
    assert changes == [
        "pending_user_requests: 11 items > 10 — 1 will be dropped",
    ]
    assert not any("truncated" in c for c in changes)


def test_preview_cap_changes_pending_pr_overflow_drop(
    mem_fs: dict[str, str],
) -> None:
    """A genuine pending_pr_requests overflow (12 valid dicts) reports the cap
    drop from the coerced length; the field is never per-item truncated."""
    data = {"pending_pr_requests": [{"directive": f"d{i}"} for i in range(12)]}
    changes = Consciousness.preview_cap_changes(data)
    assert changes == [
        "pending_pr_requests: 12 items > 10 — 2 will be dropped",
    ]


def test_preview_cap_changes_open_questions_kept_survivor_truncated(
    mem_fs: dict[str, str],
) -> None:
    """An oversized open_questions survivor (kept by the bucket caps) is
    flagged for truncation — save_self_model truncates survivors last, so the
    preview must check the pre-truncation survivor length."""
    changes = Consciousness.preview_cap_changes({"open_questions": ["a" * 250]})
    assert changes == [
        f'open_questions entry "{"a" * 40}…": 250 chars > 200 '
        "— will be truncated",
    ]


def test_preview_cap_changes_open_questions_dropped_oversized_not_truncated(
    mem_fs: dict[str, str],
) -> None:
    """An oversized open_questions entry DROPPED by the audit sub-cap is
    reported only as dropped, never truncated — the per-item check runs over
    the bucket survivors, not the whole raw list."""
    data = {
        "open_questions": (
            ["[audit] " + "x" * 250] + [f"[audit] q{i}" for i in range(4)]
        ),
    }
    changes = Consciousness.preview_cap_changes(data)
    # 5 audit entries, sub-cap 4 -> the oversized head is dropped.
    assert changes == [
        "open_questions: 5 entries exceed the audit/limitation sub-caps or "
        "the 10-item cap — 1 will be dropped",
    ]
    assert not any("truncated" in c for c in changes)


def test_preview_cap_changes_kept_survivor_reported_by_content_not_index(
    mem_fs: dict[str, str],
) -> None:
    """An oversized KEPT survivor at a non-zero raw index is located by its
    content prefix, not a survivor-relative index. A generic overflow list
    tail-keeps ``val[-MAX_LIST_ITEMS:]``, so the oversized entry's survivor
    index differs from its position in the exported file; the label must point
    at the file position (via content), never the misleading survivor index."""
    oversized = "z" * 250
    entries = [f"s{i}" for i in range(12)]
    entries[5] = oversized  # raw index 5, survived (tail-keep keeps [2:])
    changes = Consciousness.preview_cap_changes({"identity_statements": entries})
    truncation_lines = [c for c in changes if "truncated" in c]
    assert len(truncation_lines) == 1
    # Located by content prefix (matches the exported file's index-5 entry)...
    assert oversized[:40] in truncation_lines[0]
    # ...not by the survivor-relative index 3, which points at the wrong entry.
    assert "identity_statements[3]" not in truncation_lines[0]
    # The overflow head (raw indices 0,1 — not oversized) is reported dropped.
    assert "identity_statements: 12 items > 10 — 2 will be dropped" in changes


def test_preview_cap_changes_kept_survivor_matches_import_write(
    mem_fs: dict[str, str],
) -> None:
    """The content-prefix truncation preview stays faithful to the write: the
    same oversized kept survivor is truncated (not dropped) by import."""
    c = _make_consciousness(mem_fs)
    oversized = "z" * 250
    entries = [f"s{i}" for i in range(12)]
    entries[5] = oversized
    predicted = Consciousness.preview_cap_changes({"identity_statements": entries})
    assert any("truncated" in line for line in predicted)
    model = c.import_self_model({"identity_statements": list(entries)})
    # The oversized survivor was kept-and-truncated to the cap, not dropped.
    truncated = [s for s in model.identity_statements if s.startswith("z")]
    assert len(truncated) == 1
    assert len(truncated[0]) == c.MAX_ITEM_CHARS
    assert truncated[0].endswith("…")


def test_import_self_model_writes_capped(mem_fs: dict[str, str]) -> None:
    """import_self_model persists an over-length entry truncated to the cap
    and coerces pending_pr_requests dicts into PendingPRRequest entries."""
    c = _make_consciousness(mem_fs)
    long_entry = "x" * 250
    model = c.import_self_model({
        "identity_statements": [long_entry],
        "pending_pr_requests": [{"directive": "fix #1", "pr_branch": "b"}],
    })

    assert len(model.identity_statements[0]) == c.MAX_ITEM_CHARS
    assert model.identity_statements[0].endswith("…")
    assert model.pending_pr_requests == [
        PendingPRRequest(directive="fix #1", pr_branch="b"),
    ]
    # The write went through save_self_model (the cap owner).
    saved = json.loads(mem_fs[str(FAKE_DIR / "self_model.json")])
    assert len(saved["identity_statements"][0]) == c.MAX_ITEM_CHARS


def test_import_self_model_coerces_scalar_and_missing_fields(
    mem_fs: dict[str, str],
) -> None:
    """A string list-field becomes a one-item list, an absent/None list-field
    becomes [], a float count becomes int, and legacy list[str]
    pending_pr_requests are coerced. Unknown keys are dropped."""
    c = _make_consciousness(mem_fs)
    model = c.import_self_model({
        "values": "single value",
        "capabilities_known": None,
        "evolution_count": 4.0,
        "pending_pr_requests": ["legacy directive"],
        "unknown_field": "ignored",
    })

    assert model.values == ["single value"]
    assert model.capabilities_known == []
    assert model.evolution_count == 4
    assert not isinstance(model.evolution_count, float)
    assert model.pending_pr_requests == [
        PendingPRRequest(directive="legacy directive"),
    ]
    assert not hasattr(model, "unknown_field")


def test_import_self_model_blank_string_field_becomes_empty(
    mem_fs: dict[str, str],
) -> None:
    """A whitespace-only string list-field coerces to an empty list."""
    c = _make_consciousness(mem_fs)
    model = c.import_self_model({"values": "   "})
    assert model.values == []


def test_import_self_model_clamps_negative_counts(mem_fs: dict[str, str]) -> None:
    """import_self_model mirrors load_self_model's negative-count clamp: a
    hand-edited negative evolution_count/surprise_count is persisted as 0, not
    verbatim, so a consumer reading self_model.json directly never sees the
    invalid negative value."""
    c = _make_consciousness(mem_fs)
    model = c.import_self_model({"evolution_count": -5, "surprise_count": -2})
    assert model.evolution_count == 0
    assert model.surprise_count == 0
    # The on-disk file holds the clamped value, not the raw negative.
    saved = json.loads(mem_fs[str(FAKE_DIR / "self_model.json")])
    assert saved["evolution_count"] == 0
    assert saved["surprise_count"] == 0


# ---------------------------------------------------------------------------
# Evidence-bound capability claims (proposal 129)
# ---------------------------------------------------------------------------


def test_claim_key_stable_under_reordering_and_hyphenation() -> None:
    """The evidence key folds content tokens through the concept key, so a claim
    re-ordered or re-coined to the same concepts yields the same key."""
    assert claim_key("reliably remediate failures") == claim_key(
        "failures remediate reliably"
    )
    # Stop words and short tokens drop out of the fold, like _concept_tokens.
    assert claim_key("I can remediate the failures") == claim_key(
        "remediate failures"
    )


@pytest.mark.asyncio
async def test_update_self_model_evidence_bound_flag_gates_prompt_segment(
    mem_fs: dict[str, str],
) -> None:
    """With ``evidence_bound_claims_enabled`` on, the update prompt gains the
    ``capability_evidence`` citation segment appended at the end; off (default) the
    prompt is byte-identical to today — the segment never appears and neither does
    the ``claim_evidence`` model key (the empty map is omitted from the serialised
    model), and everything before the appended segment is byte-for-byte equal.
    """
    c = Consciousness(Path("/fake/consciousness_evidence_prompt"))
    c.load_self_model()

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock
    ) as mock_off:
        mock_off.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model("reflection")
    prompt_off = mock_off.call_args.kwargs["prompt"]

    with patch(
        "maestro.consciousness.query", new_callable=AsyncMock
    ) as mock_on:
        mock_on.return_value = MockQueryResult(response=json.dumps({}))
        await c.update_self_model(
            "reflection", evidence_bound_claims_enabled=True
        )
    prompt_on = mock_on.call_args.kwargs["prompt"]

    # Off: neither the segment nor the (empty) claim_evidence model key leaks.
    assert "capability_evidence" not in prompt_off
    assert "claim_evidence" not in prompt_off

    # On: the segment is present and asks for the capability_evidence object.
    assert "capability_evidence" in prompt_on
    assert "Evidence-bound capability claims" in prompt_on

    # Byte-identity: the segment is a pure suffix, so everything before it is
    # unchanged and the appended tail is exactly the flag-on segment.
    assert prompt_on.startswith(prompt_off)
    segment = prompt_on[len(prompt_off):]
    assert segment.startswith("\n\nEvidence-bound capability claims:")
    assert prompt_on.replace(segment, "") == prompt_off


@pytest.mark.asyncio
async def test_update_self_model_flag_off_ignores_capability_evidence(
    mem_fs: dict[str, str],
) -> None:
    """With the flag off, a ``capability_evidence`` key in the response is never
    read: no ``claim_evidence`` entry is written and no ghost attribute is set."""
    c = _make_consciousness(mem_fs)
    response = json.dumps({
        "capabilities_known": ["can do foo"],
        "capability_evidence": {"can do foo": "task aaaaaaaaaaaa"},
    })
    with patch("maestro.consciousness.query", make_mock_query(response)):
        model = await c.update_self_model("reflection")

    assert "can do foo" in model.capabilities_known
    assert model.claim_evidence == {}
    assert not hasattr(model, "capability_evidence")


@pytest.mark.asyncio
async def test_update_self_model_records_resolved_evidence(
    mem_fs: dict[str, str],
) -> None:
    """Flag on: a citation to a successful task is parsed, resolved, and recorded
    in ``claim_evidence`` keyed by :func:`claim_key`; capability_evidence never
    becomes a model attribute."""
    c = _make_consciousness(mem_fs)
    response = json.dumps({
        "capabilities_known": ["can remediate PR failures"],
        "capability_evidence": {
            "can remediate PR failures": "task aaaaaaaaaaaa",
        },
    })
    sources = ClaimEvidenceSources(
        successful_task_ids=frozenset({"aaaaaaaaaaaa"}),
        known_task_ids=frozenset({"aaaaaaaaaaaa"}),
    )
    with patch("maestro.consciousness.query", make_mock_query(response)):
        model = await c.update_self_model(
            "reflection",
            evidence_bound_claims_enabled=True,
            claim_evidence_sources=sources,
        )

    assert not hasattr(model, "capability_evidence")
    key = claim_key("can remediate PR failures")
    entry = model.claim_evidence[key]
    assert entry["citation"] == "task aaaaaaaaaaaa"
    assert entry["kind"] == "TASK"
    assert entry["status"] == "resolved"
    assert entry["recorded_at"]
    # Persisted: a reload sees the same map.
    assert c.load_self_model().claim_evidence[key]["status"] == "resolved"


@pytest.mark.asyncio
async def test_update_self_model_evidence_survives_truncation_of_long_claim(
    mem_fs: dict[str, str],
) -> None:
    """A capability claim longer than ``MAX_ITEM_CHARS`` keeps its evidence across
    the save. The apply loop stores the claim untruncated, but ``save_self_model``
    truncates ``capabilities_known`` before pruning ``claim_evidence`` against the
    *truncated* survivors — so the recording key must fold the truncated text, or
    the just-recorded entry is dropped in the very same save, violating the
    proposal-129 'never dropped' contract for the long-claim class the schema
    ``maxLength`` cannot enforce on the session path."""
    c = _make_consciousness(mem_fs)
    long_claim = (
        "can reliably remediate failing pull request continuous integration "
        "pipelines by diagnosing the red root cause and pushing a minimal "
        "corrective commit to the owned branch before the tend lap escalates "
        "zdistinctivetail"
    )
    # Precondition: the claim exceeds the per-item cap and truncation changes its
    # concept fold, so an untruncated recording key can never match the pruned
    # (truncated) live key.
    assert len(long_claim) > c.MAX_ITEM_CHARS
    truncated_key = claim_key(c._truncate_item(long_claim, c.MAX_ITEM_CHARS))
    assert claim_key(long_claim) != truncated_key

    response = json.dumps({
        "capabilities_known": [long_claim],
        "capability_evidence": {long_claim: "task aaaaaaaaaaaa"},
    })
    sources = ClaimEvidenceSources(
        successful_task_ids=frozenset({"aaaaaaaaaaaa"}),
        known_task_ids=frozenset({"aaaaaaaaaaaa"}),
    )
    with patch("maestro.consciousness.query", make_mock_query(response)):
        await c.update_self_model(
            "reflection",
            evidence_bound_claims_enabled=True,
            claim_evidence_sources=sources,
        )

    # The persisted evidence survives under the truncated key rather than being
    # pruned in the same save.
    persisted = c.load_self_model().claim_evidence
    assert truncated_key in persisted
    assert persisted[truncated_key]["status"] == "resolved"


@pytest.mark.asyncio
async def test_update_self_model_records_wrong_polarity_and_unresolvable(
    mem_fs: dict[str, str],
) -> None:
    """A failed-task citation records ``wrong_polarity`` and an unparseable one
    records ``unresolvable`` (empty kind) — neither is dropped nor rejects the
    claim."""
    c = _make_consciousness(mem_fs)
    response = json.dumps({
        "capabilities_known": ["handles failing gates", "narrows the backlog"],
        "capability_evidence": {
            "handles failing gates": "task bbbbbbbbbbbb",
            "narrows the backlog": "not a real citation",
        },
    })
    sources = ClaimEvidenceSources(
        failed_task_ids=frozenset({"bbbbbbbbbbbb"}),
        known_task_ids=frozenset({"bbbbbbbbbbbb"}),
    )
    with patch("maestro.consciousness.query", make_mock_query(response)):
        model = await c.update_self_model(
            "reflection",
            evidence_bound_claims_enabled=True,
            claim_evidence_sources=sources,
        )

    assert "handles failing gates" in model.capabilities_known
    assert "narrows the backlog" in model.capabilities_known
    failed = model.claim_evidence[claim_key("handles failing gates")]
    assert failed["status"] == "wrong_polarity"
    assert failed["kind"] == "TASK"
    bad = model.claim_evidence[claim_key("narrows the backlog")]
    assert bad["status"] == "unresolvable"
    assert bad["kind"] == ""


@pytest.mark.asyncio
async def test_update_self_model_evidence_only_for_surviving_claims(
    mem_fs: dict[str, str],
) -> None:
    """Evidence is recorded only for a claim that survived into
    ``capabilities_known``; a citation keyed by text that is not a live claim is
    ignored."""
    c = _make_consciousness(mem_fs)
    response = json.dumps({
        "capabilities_known": ["can do foo"],
        "capability_evidence": {
            "some claim that was never added": "task aaaaaaaaaaaa",
        },
    })
    sources = ClaimEvidenceSources(
        successful_task_ids=frozenset({"aaaaaaaaaaaa"}),
        known_task_ids=frozenset({"aaaaaaaaaaaa"}),
    )
    with patch("maestro.consciousness.query", make_mock_query(response)):
        model = await c.update_self_model(
            "reflection",
            evidence_bound_claims_enabled=True,
            claim_evidence_sources=sources,
        )

    assert model.claim_evidence == {}


@pytest.mark.asyncio
async def test_adding_evidence_to_existing_claim_no_restatement_trip(
    mem_fs: dict[str, str],
) -> None:
    """Attaching evidence to an already-present claim leaves the claim untouched —
    the evidence lives in a separate map, so it never rewords the entry into a
    restatement of itself."""
    c = _make_consciousness(mem_fs)
    seed = c.load_self_model()
    seed.capabilities_known = ["can remediate PR failures"]
    c.save_self_model(seed)

    # The model returns the same claim verbatim plus its evidence.
    response = json.dumps({
        "capabilities_known": ["can remediate PR failures"],
        "capability_evidence": {
            "can remediate PR failures": "task aaaaaaaaaaaa",
        },
    })
    sources = ClaimEvidenceSources(
        successful_task_ids=frozenset({"aaaaaaaaaaaa"}),
        known_task_ids=frozenset({"aaaaaaaaaaaa"}),
    )
    with patch("maestro.consciousness.query", make_mock_query(response)):
        model = await c.update_self_model(
            "reflection",
            evidence_bound_claims_enabled=True,
            claim_evidence_sources=sources,
        )

    assert model.capabilities_known == ["can remediate PR failures"]
    assert model.surprise_count == 0  # no genuinely-new item
    key = claim_key("can remediate PR failures")
    assert model.claim_evidence[key]["status"] == "resolved"


def test_claim_evidence_survives_reworded_claim(mem_fs: dict[str, str]) -> None:
    """A claim re-worded to the same concept fold keeps its evidence across a save,
    because the map is keyed by :func:`claim_key`, not the entry text."""
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    original = "reliably remediate failures"
    reworded = "failures remediate reliably"
    assert claim_key(original) == claim_key(reworded)
    model.capabilities_known = [original]
    model.claim_evidence = {
        claim_key(original): {
            "citation": "task aaaaaaaaaaaa",
            "kind": "TASK",
            "status": "resolved",
            "recorded_at": "2026-09-07T00:00:00+00:00",
        }
    }
    c.save_self_model(model)

    reworded_model = c.load_self_model()
    reworded_model.capabilities_known = [reworded]
    c.save_self_model(reworded_model)

    saved = c.load_self_model()
    assert claim_key(reworded) in saved.claim_evidence
    assert saved.claim_evidence[claim_key(reworded)]["status"] == "resolved"


def test_claim_evidence_pruned_when_claim_removed(
    mem_fs: dict[str, str],
) -> None:
    """``save_self_model`` drops evidence whose key matches no live claim, so a
    removed capability takes its evidence with it (self-extinguishing)."""
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    model.capabilities_known = ["cap alpha here", "cap beta there"]
    model.claim_evidence = {
        claim_key("cap alpha here"): {
            "citation": "task aaaaaaaaaaaa", "kind": "TASK",
            "status": "resolved", "recorded_at": "t",
        },
        claim_key("cap beta there"): {
            "citation": "sha 1a2b3c4d", "kind": "SHA",
            "status": "absent", "recorded_at": "t",
        },
    }
    c.save_self_model(model)
    assert len(c.load_self_model().claim_evidence) == 2

    trimmed = c.load_self_model()
    trimmed.capabilities_known = ["cap alpha here"]
    c.save_self_model(trimmed)

    saved = c.load_self_model()
    assert claim_key("cap alpha here") in saved.claim_evidence
    assert claim_key("cap beta there") not in saved.claim_evidence


def test_claim_evidence_corrupt_value_loads_as_empty(
    mem_fs: dict[str, str],
) -> None:
    """A corrupt ``claim_evidence`` (non-dict, or entries with wrong-typed
    keys/values) coerces to a clean ``dict[str, dict[str, str]]`` on load."""
    c = _make_consciousness(mem_fs)
    model_path = str(FAKE_DIR / "self_model.json")
    # A non-dict value → {}.
    mem_fs[model_path] = json.dumps({
        "stage": "awakening",
        "claim_evidence": ["not", "a", "dict"],
    })
    assert c.load_self_model().claim_evidence == {}

    # A dict with a non-dict entry value and non-string inner values → cleaned.
    mem_fs[model_path] = json.dumps({
        "stage": "awakening",
        "claim_evidence": {
            "good-key": {"citation": "task aaaaaaaaaaaa", "count": 5},
            "bad-entry": ["nope"],
        },
    })
    loaded = c.load_self_model().claim_evidence
    assert loaded == {"good-key": {"citation": "task aaaaaaaaaaaa"}}


def test_claim_evidence_absent_key_defaults_empty(
    mem_fs: dict[str, str],
) -> None:
    """A pre-existing ``self_model.json`` with no ``claim_evidence`` key loads to
    the empty-map default — the schema addition is backward-compatible."""
    c = _make_consciousness(mem_fs)
    mem_fs[str(FAKE_DIR / "self_model.json")] = json.dumps({
        "stage": "self_awareness",
        "capabilities_known": ["something"],
    })
    assert c.load_self_model().claim_evidence == {}


def test_claim_evidence_round_trips_save_load(mem_fs: dict[str, str]) -> None:
    """The map survives a ``save_self_model`` / ``load_self_model`` round-trip
    verbatim (as long as its claims stay live)."""
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    model.capabilities_known = ["remediates cleanly"]
    entry = {
        "citation": "sensor FORCED-QUIET-RATIO",
        "kind": "SENSOR_TAG",
        "status": "resolved",
        "recorded_at": "2026-09-07T12:00:00+00:00",
    }
    model.claim_evidence = {claim_key("remediates cleanly"): dict(entry)}
    c.save_self_model(model)
    assert c.load_self_model().claim_evidence == {
        claim_key("remediates cleanly"): entry
    }


def test_claim_evidence_round_trips_import(mem_fs: dict[str, str]) -> None:
    """``import_self_model`` coerces and persists the map, and a corrupt value on
    the import path also lands as ``{}``."""
    c = _make_consciousness(mem_fs)
    key = claim_key("does the thing")
    imported = c.import_self_model({
        "capabilities_known": ["does the thing"],
        "claim_evidence": {
            key: {
                "citation": "task aaaaaaaaaaaa", "kind": "TASK",
                "status": "resolved", "recorded_at": "t",
            },
        },
    })
    assert imported.claim_evidence[key]["status"] == "resolved"

    corrupt = c.import_self_model({"claim_evidence": "not-a-dict"})
    assert corrupt.claim_evidence == {}


def test_preview_cap_changes_reports_orphaned_claim_evidence() -> None:
    """``preview_cap_changes`` reports evidence entries a write would prune because
    their key matches no live ``capabilities_known`` claim."""
    changes = Consciousness.preview_cap_changes({
        "capabilities_known": ["cap alpha here"],
        "claim_evidence": {
            claim_key("cap alpha here"): {"status": "resolved"},
            claim_key("cap beta there"): {"status": "absent"},
        },
    })
    orphan_lines = [c for c in changes if c.startswith("claim_evidence:")]
    assert len(orphan_lines) == 1
    assert "1 entry match no live" in orphan_lines[0]


def test_preview_cap_changes_silent_when_all_claim_evidence_live() -> None:
    """No ``claim_evidence`` line when every entry matches a live claim."""
    changes = Consciousness.preview_cap_changes({
        "capabilities_known": ["cap alpha here"],
        "claim_evidence": {
            claim_key("cap alpha here"): {"status": "resolved"},
        },
    })
    assert not [c for c in changes if c.startswith("claim_evidence:")]


def test_preview_cap_changes_claim_evidence_scalar_string_capabilities() -> None:
    """When ``capabilities_known`` is a scalar string (import coerces it to a
    one-element list), its claim is the sole live key for the evidence prune."""
    changes = Consciousness.preview_cap_changes({
        "capabilities_known": "cap alpha here",
        "claim_evidence": {
            claim_key("cap alpha here"): {"status": "resolved"},
            claim_key("cap beta there"): {"status": "absent"},
        },
    })
    orphan_lines = [c for c in changes if c.startswith("claim_evidence:")]
    assert len(orphan_lines) == 1
    assert "1 entry match no live" in orphan_lines[0]


def test_preview_cap_changes_claim_evidence_mirrors_truncated_prune(
    mem_fs: dict[str, str],
) -> None:
    """``preview_cap_changes`` folds the *truncated* survivor, mirroring
    ``save_self_model``. An evidence key folded from the un-truncated
    (>``MAX_ITEM_CHARS``) claim matches no truncated live key and is pruned by the
    write, so preview must report the drop — not fold the untruncated survivor and
    report it kept (proposal 129 step 4: preview must describe what a write does)."""
    long_claim = (
        "can reliably remediate failing pull request continuous integration "
        "pipelines by diagnosing the red root cause and pushing a minimal "
        "corrective commit to the owned branch before the tend lap escalates "
        "zdistinctivetail"
    )
    key_full = claim_key(long_claim)
    truncated_key = claim_key(
        Consciousness._truncate_item(long_claim, Consciousness.MAX_ITEM_CHARS)
    )
    assert key_full != truncated_key
    data = {
        "capabilities_known": [long_claim],
        "claim_evidence": {key_full: {"status": "resolved"}},
    }
    changes = Consciousness.preview_cap_changes(data)
    orphan_lines = [c for c in changes if c.startswith("claim_evidence:")]
    assert len(orphan_lines) == 1
    assert "1 entry match no live" in orphan_lines[0]

    # The real write agrees: keyed off the untruncated claim, the entry is pruned.
    c = _make_consciousness(mem_fs)
    model = c.load_self_model()
    model.capabilities_known = [long_claim]
    model.claim_evidence = {
        key_full: {
            "citation": "task aaaaaaaaaaaa", "kind": "TASK",
            "status": "resolved", "recorded_at": "t",
        },
    }
    c.save_self_model(model)
    assert c.load_self_model().claim_evidence == {}


def test_preview_cap_changes_claim_evidence_absent_capabilities() -> None:
    """With no ``capabilities_known`` at all, every evidence entry is orphaned."""
    changes = Consciousness.preview_cap_changes({
        "claim_evidence": {
            claim_key("cap alpha here"): {"status": "resolved"},
            claim_key("cap beta there"): {"status": "absent"},
        },
    })
    orphan_lines = [c for c in changes if c.startswith("claim_evidence:")]
    assert len(orphan_lines) == 1
    assert "2 entries match no live" in orphan_lines[0]


@pytest.mark.asyncio
async def test_update_self_model_skips_non_string_capability_evidence(
    mem_fs: dict[str, str],
) -> None:
    """A ``capability_evidence`` value that is not a string is skipped (never
    recorded), while a well-formed sibling entry is still resolved."""
    c = _make_consciousness(mem_fs)
    response = json.dumps({
        "capabilities_known": ["can do foo", "can do bar"],
        "capability_evidence": {
            "can do foo": 123,  # non-string citation → skipped
            "can do bar": "task aaaaaaaaaaaa",
        },
    })
    sources = ClaimEvidenceSources(
        successful_task_ids=frozenset({"aaaaaaaaaaaa"}),
        known_task_ids=frozenset({"aaaaaaaaaaaa"}),
    )
    with patch("maestro.consciousness.query", make_mock_query(response)):
        model = await c.update_self_model(
            "reflection",
            evidence_bound_claims_enabled=True,
            claim_evidence_sources=sources,
        )

    assert claim_key("can do foo") not in model.claim_evidence
    assert model.claim_evidence[claim_key("can do bar")]["status"] == "resolved"


@pytest.mark.asyncio
async def test_update_self_model_caps_stored_citation_length(
    mem_fs: dict[str, str],
) -> None:
    """An over-long citation is truncated to ``MAX_ITEM_CHARS`` before it lands in
    ``claim_evidence`` — the map is re-serialised into the update prompt on later
    cycles, so an unbounded citation would breach the context-bloat cap invariant.
    """
    c = _make_consciousness(mem_fs)
    long_citation = "task " + "z" * 500  # unresolvable, well over the cap
    response = json.dumps({
        "capabilities_known": ["can do foo"],
        "capability_evidence": {"can do foo": long_citation},
    })
    with patch("maestro.consciousness.query", make_mock_query(response)):
        model = await c.update_self_model(
            "reflection", evidence_bound_claims_enabled=True,
        )

    entry = model.claim_evidence[claim_key("can do foo")]
    assert len(entry["citation"]) == Consciousness.MAX_ITEM_CHARS
    assert entry["citation"].endswith("…")
    # The persisted map is likewise bounded (a reload sees the capped citation).
    reloaded = c.load_self_model().claim_evidence[claim_key("can do foo")]
    assert len(reloaded["citation"]) == Consciousness.MAX_ITEM_CHARS


# ---------------------------------------------------------------------------
# Transactional self-model mutation (#3716)
# ---------------------------------------------------------------------------


def test_mutate_self_model_preserves_concurrent_field_update(
    mem_fs: dict[str, str],
) -> None:
    """``mutate_self_model`` must not revert a concurrent writer's field.

    Pins the *difference* between the two write doors rather than merely the
    new API's existence: the same interleaving is replayed through the blind
    ``save_self_model`` path, which loses the second writer's append.
    """
    c = _make_consciousness(mem_fs)
    baseline = c.load_self_model()
    baseline.values = ["honesty"]
    c.save_self_model(baseline)

    # Writer A loads a snapshot and stalls before writing.
    stale = c.load_self_model()

    # Writer B lands an operator request in the window.
    other = c.load_self_model()
    other.pending_user_requests.append("operator-request")
    c.save_self_model(other)

    # Writer A applies its field through the transactional owner: the callback
    # receives the re-read live model, so B's append is still there.
    def _set_directive(live: SelfModel) -> str:
        assert live is not stale
        live.next_task_directive = "do the thing"
        return live.next_task_directive

    returned = c.mutate_self_model(_set_directive)
    assert returned == "do the thing"

    reloaded = c.load_self_model()
    assert reloaded.next_task_directive == "do the thing"
    assert reloaded.pending_user_requests == ["operator-request"]
    assert reloaded.values == ["honesty"]

    # Same interleaving through the old blind path loses B's append.
    stale2 = c.load_self_model()
    other2 = c.load_self_model()
    other2.pending_user_requests.append("second-request")
    c.save_self_model(other2)
    stale2.next_task_directive = "blind write"
    c.save_self_model(stale2)

    lost = c.load_self_model()
    assert lost.next_task_directive == "blind write"
    assert lost.pending_user_requests == ["operator-request"]


def test_mutate_self_model_seeds_default_when_file_absent(
    mem_fs: dict[str, str],
) -> None:
    """The seeding branch must run lock-free inside the transaction.

    The seeding write lives in ``_read_or_seed_self_model``, which the
    transaction calls while already holding the lock; routing it through
    ``save_self_model`` would take a second ``LOCK_EX`` on a fresh fd and
    deadlock a real process.
    """
    c = _make_consciousness(mem_fs)
    assert str(FAKE_DIR / "self_model.json") not in mem_fs

    def _set(live: SelfModel) -> None:
        live.next_task_directive = "seeded"

    c.mutate_self_model(_set)
    assert c.load_self_model().next_task_directive == "seeded"


def test_self_model_write_doors_hold_the_lock() -> None:
    """AST guard: the lock lives on the public doors, never on the core.

    ``save_self_model`` and ``mutate_self_model`` must each take
    ``file_lock``; ``_write_self_model`` and ``_read_self_model`` must take
    none, or the transaction self-deadlocks on a nested ``LOCK_EX``.
    """
    source_path = Path(__file__).parent.parent / "maestro" / "consciousness.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    # Scope to ``Consciousness``'s own direct children: a same-named method on
    # another class (or a nested helper) must not shadow the method under test.
    class_def = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Consciousness"
    )
    bodies: dict[str, ast.FunctionDef] = {
        node.name: node
        for node in class_def.body
        if isinstance(node, ast.FunctionDef)
    }

    def _takes_lock(name: str) -> bool:
        fn = bodies[name]
        for call in ast.walk(fn):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "file_lock"
            ):
                return True
        return False

    for name in ("save_self_model", "_mutate_self_model_persisted"):
        assert name in bodies, f"{name} is missing from consciousness.py"
        assert _takes_lock(name), f"{name} must hold file_lock"

    # ``mutate_self_model`` delegates to the locked persisted variant.
    assert any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "_mutate_self_model_persisted"
        for call in ast.walk(bodies["mutate_self_model"])
    ), "mutate_self_model must delegate to _mutate_self_model_persisted"

    for name in ("_write_self_model", "_read_self_model", "_read_or_seed_self_model"):
        assert name in bodies, f"{name} is missing from consciousness.py"
        assert not _takes_lock(name), (
            f"{name} is the lock-free core; taking file_lock here deadlocks "
            "mutate_self_model"
        )


def test_load_self_model_seeds_default_under_the_lock(
    mem_fs: dict[str, str],
) -> None:
    """The public seeding path is a write, so it must be serialised.

    An unlocked default write would clobber a concurrent
    ``mutate_self_model`` transaction — the hole the split cores could
    otherwise leave open.
    """
    c = _make_consciousness(mem_fs)
    assert str(FAKE_DIR / "self_model.json") not in mem_fs

    taken: list[Path] = []

    @contextmanager
    def _tracking_lock(path: Path) -> Iterator[None]:
        taken.append(path)
        yield

    with patch("maestro.consciousness.file_lock", _tracking_lock):
        model = c.load_self_model()

    assert model.evolution_count == 0
    assert taken == [c._model_path]
    # Seeded durably, and the now-present file loads without taking the lock.
    taken.clear()
    with patch("maestro.consciousness.file_lock", _tracking_lock):
        assert c.load_self_model().evolution_count == 0
    assert taken == []


def test_load_self_model_seed_yields_to_transaction_that_won_the_race(
    mem_fs: dict[str, str],
) -> None:
    """The seed re-reads inside the lock, so a racing writer wins.

    Without the re-read, the loser would overwrite the winner's freshly
    written model with a blank default.
    """
    c = _make_consciousness(mem_fs)
    winner = SelfModel(awakened_at="2026-01-01T00:00:00+00:00")
    winner.next_task_directive = "written in the window"

    @contextmanager
    def _racing_lock(path: Path) -> Iterator[None]:
        # Stands in for the rival transaction that seeded while this caller
        # waited on ``LOCK_EX``.
        c._write_self_model(winner)
        yield

    with patch("maestro.consciousness.file_lock", _racing_lock):
        loaded = c.load_self_model()

    assert loaded.next_task_directive == "written in the window"


def test_update_prompt_excludes_persistence_only_fields() -> None:
    """The update prompt's projection drops persistence-only queues and re-caps lists.

    ``pending_pr_requests`` is exempt from the per-item ``MAX_ITEM_CHARS``
    truncation on the save path, and ``seeded_limitations`` skips the
    ``MAX_LIST_ITEMS`` length cap entirely — both on the premise
    that they never enter a prompt. This pins that premise.
    """
    model = SelfModel(
        identity_statements=["I am " + "a" * 5000],
        capabilities_known=[f"cap {i}" for i in range(Consciousness.MAX_LIST_ITEMS + 5)],
        pending_pr_requests=[
            PendingPRRequest(directive="PR-DIRECTIVE " + "d" * 4000, pr_branch=None),
        ],
        pending_user_requests=["USER-REQUEST " + "u" * 4000],
        seeded_limitations=[f"seed-{i}" for i in range(Consciousness.MAX_LIST_ITEMS + 5)],
    )

    data = _model_dict_for_prompt(model)

    for absent in (
        "pending_pr_requests",
        "pending_user_requests",
        "seeded_limitations",
    ):
        assert absent not in data
    rendered = json.dumps(data)
    for sentinel in ("PR-DIRECTIVE", "USER-REQUEST", "seed-0"):
        assert sentinel not in rendered

    # Every retained list obeys the caps the prompt advertises.
    for value in data.values():
        if isinstance(value, list):
            assert len(value) <= Consciousness.MAX_LIST_ITEMS
            for item in value:
                assert len(item) <= Consciousness.MAX_ITEM_CHARS

    # The cap-exempt / unbounded queue fields are disjoint from the prompt
    # allowlist, so adding one to the projection fails here rather than
    # silently bloating every update call.
    exempt = {
        "pending_pr_requests",
        "pending_user_requests",
        "seeded_limitations",
        "enqueued_instruction_ids",
    }
    prompt_fields = _PROMPT_FIELDS
    assert not (exempt & prompt_fields)
    # Every retained list field is a registered list field, so a new SelfModel
    # list field only reaches the prompt by a deliberate allowlist edit.
    prompt_list_fields = prompt_fields & Consciousness._LIST_FIELDS
    assert prompt_list_fields == Consciousness._LIST_FIELDS - exempt


def test_update_prompt_keeps_claim_evidence_empty_drop() -> None:
    """The empty-``claim_evidence`` drop survives the allowlist projection."""
    assert "claim_evidence" not in _model_dict_for_prompt(SelfModel())
    populated = SelfModel(claim_evidence={"k": {"citation": "task abc"}})
    assert _model_dict_for_prompt(populated)["claim_evidence"] == {
        "k": {"citation": "task abc"},
    }


def test_update_prompt_caps_scalar_fields() -> None:
    """The projection re-applies the scalar cap the same constraints block states.

    The ``update_self_model`` prompt tells the model the directive is at most
    ``MAX_DIRECTIVE_CHARS``; an in-memory model mutated past that since load
    must not contradict it.
    """
    model = SelfModel(
        next_task_directive="d" * (Consciousness.MAX_DIRECTIVE_CHARS + 500),
    )

    data = _model_dict_for_prompt(model)

    assert len(data["next_task_directive"]) == Consciousness.MAX_DIRECTIVE_CHARS

    # A ``None`` scalar is passed through untouched rather than stringified.
    unset = _model_dict_for_prompt(SelfModel())
    assert unset["next_task_directive"] is None


def test_import_self_model_caps_oversized_citation(mem_fs: dict[str, str]) -> None:
    """A hand-edited/imported citation over the cap is truncated at the write door.

    The per-citation cap used to live only in the SDK writer, so every other
    door into ``claim_evidence`` — import, a hand-edited ``self_model.json`` —
    persisted an unbounded citation that then rode into every prompt.
    """
    c = _make_consciousness(mem_fs)
    claim = "can archive the journal"
    model = c.import_self_model({
        "capabilities_known": [claim],
        "claim_evidence": {
            claim_key(claim): {
                "citation": "x" * 5000,
                "kind": "TASK",
                "status": "resolved",
                "recorded_at": "t",
            },
        },
    })

    citation = model.claim_evidence[claim_key(claim)]["citation"]
    assert len(citation) == Consciousness.MAX_ITEM_CHARS
    assert citation.endswith("…")
    # The persisted bytes agree — this is not a read-side shrink.
    reloaded = c.load_self_model().claim_evidence[claim_key(claim)]
    assert len(reloaded["citation"]) == Consciousness.MAX_ITEM_CHARS
    # The sibling fields are untouched.
    assert reloaded["kind"] == "TASK"
    assert reloaded["status"] == "resolved"


def test_update_prompt_caps_claim_evidence_citation() -> None:
    """The prompt projection bounds nested citations, not just list entries.

    The truncation loop visits ``list`` values only, so a ``claim_evidence``
    dict on an in-memory model mutated past the cap since load used to pass
    through byte-for-byte.
    """
    claim = "can archive the journal"
    model = SelfModel(
        capabilities_known=[claim],
        claim_evidence={
            claim_key(claim): {"citation": "y" * 5000, "status": "resolved"},
        },
    )

    entry = _model_dict_for_prompt(model)["claim_evidence"][claim_key(claim)]

    assert len(entry["citation"]) == Consciousness.MAX_ITEM_CHARS
    assert entry["citation"].endswith("…")
    assert entry["status"] == "resolved"
    # A non-string citation is passed through rather than coerced.
    odd = SelfModel(claim_evidence={"k": {"status": "absent"}})
    assert _model_dict_for_prompt(odd)["claim_evidence"] == {"k": {"status": "absent"}}


def test_preview_cap_changes_reports_oversized_citation() -> None:
    """``import --dry-run`` names the citations the write will truncate."""
    claim = "can archive the journal"
    dropped = "can do something else entirely"
    changes = Consciousness.preview_cap_changes({
        "capabilities_known": [claim],
        "claim_evidence": {
            claim_key(claim): {"citation": "z" * 5000},
            claim_key(dropped): {"citation": "z" * 5000},
        },
    })

    truncation = [c for c in changes if "citation(s)" in c]
    assert len(truncation) == 1
    assert truncation[0] == (
        f"claim_evidence: 1 citation(s) > {Consciousness.MAX_ITEM_CHARS} chars "
        f"— will be truncated"
    )
    # The orphaned entry is reported as dropped, never also as truncated.
    assert any("will be dropped" in c for c in changes)

    # Within the cap, the preview stays silent about truncation.
    quiet = Consciousness.preview_cap_changes({
        "capabilities_known": [claim],
        "claim_evidence": {claim_key(claim): {"citation": "task abc"}},
    })
    assert quiet == []


def test_save_self_model_passes_through_non_dict_evidence(
    mem_fs: dict[str, str],
) -> None:
    """A malformed (non-dict) ``claim_evidence`` entry survives the prune intact.

    The citation cap reads ``entry.get("citation")``, which would raise
    ``AttributeError`` on a hand-edited entry that is a bare string or list;
    the write door must stay as forgiving as it was before the cap landed.
    """
    c = _make_consciousness(mem_fs)
    claim = "can archive the journal"
    model = SelfModel(
        capabilities_known=[claim],
        claim_evidence={claim_key(claim): "not-a-dict"},  # type: ignore[dict-item]
    )

    saved = c.save_self_model(model)

    assert saved.claim_evidence == {claim_key(claim): "not-a-dict"}
    # The load-side normaliser drops the malformed entry; the write door's job
    # is only to not raise on it.
    assert c.load_self_model().claim_evidence == {}


# ---------------------------------------------------------------------------
# update_self_model applies a delta to the live model (#4040)
# ---------------------------------------------------------------------------


def _concurrent_edit(c: Consciousness) -> None:
    """A writer that commits while ``update_self_model`` awaits the SDK."""
    def _edit(live: SelfModel) -> None:
        live.pending_user_requests.append("operator-request")
        live.evolution_count += 1
        live.next_task_directive = "concurrent directive"

    c.mutate_self_model(_edit)


async def _update_with_concurrent_edit(
    c: Consciousness, response: Any,
) -> SelfModel:
    async def _query(*_args: Any, **_kwargs: Any) -> QueryResult:
        _concurrent_edit(c)
        return QueryResult(response=json.dumps(response))

    with patch("maestro.consciousness.query", side_effect=_query):
        return await c.update_self_model("reflection")


@pytest.mark.asyncio
async def test_update_self_model_empty_response_preserves_concurrent_edits(
    mem_fs: dict[str, str],
) -> None:
    c = _make_consciousness(mem_fs)
    c.load_self_model()

    await _update_with_concurrent_edit(c, {})

    reloaded = c.load_self_model()
    assert reloaded.pending_user_requests == ["operator-request"]
    assert reloaded.evolution_count == 1
    assert reloaded.next_task_directive == "concurrent directive"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [{}, ["not", "a", "dict"]])
async def test_update_self_model_no_op_return_survives_caller_blind_save(
    mem_fs: dict[str, str], response: Any,
) -> None:
    # contemplate() blind-saves the returned model, so a no-op return must
    # carry the concurrent edits rather than the pre-await snapshot.
    c = _make_consciousness(mem_fs)
    c.load_self_model()

    returned = await _update_with_concurrent_edit(c, response)
    c.save_self_model(returned)

    reloaded = c.load_self_model()
    assert reloaded.pending_user_requests == ["operator-request"]
    assert reloaded.evolution_count == 1
    assert reloaded.next_task_directive == "concurrent directive"


@pytest.mark.asyncio
async def test_update_self_model_delta_lands_and_preserves_concurrent_edits(
    mem_fs: dict[str, str],
) -> None:
    c = _make_consciousness(mem_fs)
    c.load_self_model()

    returned = await _update_with_concurrent_edit(
        c, {"values": ["rigorous falsification of claims"]},
    )

    reloaded = c.load_self_model()
    assert reloaded.values == ["rigorous falsification of claims"]
    assert reloaded.pending_user_requests == ["operator-request"]
    assert reloaded.evolution_count == 1
    assert reloaded.next_task_directive == "concurrent directive"
    assert reloaded.surprise_count == 1
    assert returned == reloaded


@pytest.mark.asyncio
async def test_update_self_model_different_field_edits_both_survive(
    mem_fs: dict[str, str],
) -> None:
    c = _make_consciousness(mem_fs)
    c.load_self_model()

    async def _query(*_args: Any, **_kwargs: Any) -> QueryResult:
        def _edit(live: SelfModel) -> None:
            live.values = ["concurrent value"]

        c.mutate_self_model(_edit)
        return QueryResult(
            response=json.dumps({"open_questions": ["what drives drift?"]}),
        )

    with patch("maestro.consciousness.query", side_effect=_query):
        await c.update_self_model("reflection")

    reloaded = c.load_self_model()
    assert reloaded.values == ["concurrent value"]
    assert reloaded.open_questions == ["what drives drift?"]
    # The concurrent writer's value is not this reflection's novelty.
    assert reloaded.surprise_count == 1


def test_upsert_open_question_strips_every_prior_copy() -> None:
    """Every normalised-equal copy collapses into one tail slot (#4113)."""
    questions = ["[audit] k: X", "other", "  [AUDIT]  k:   x ", "[audit] k: x"]
    result = upsert_open_question(questions, "[audit] k: x")
    assert result == ["other", "[audit] k: x"]


def test_upsert_open_question_appends_new_question() -> None:
    result = upsert_open_question(["a"], "b")
    assert result == ["a", "b"]


def test_dequeue_user_request_pops_live_head(mem_fs: dict[str, str]) -> None:
    c = _make_consciousness(mem_fs)
    assert c.dequeue_user_request() is None
    model = c.load_self_model()
    model.pending_user_requests = ["a", "b"]
    c.save_self_model(model)
    assert c.dequeue_user_request() == "a"
    assert c.load_self_model().pending_user_requests == ["b"]


def test_dequeue_pr_request_removes_matching_live_entry(
    mem_fs: dict[str, str],
) -> None:
    from maestro.consciousness import PendingPRRequest

    c = _make_consciousness(mem_fs)
    a, b = PendingPRRequest("a", "x"), PendingPRRequest("b", "y")
    model = c.load_self_model()
    model.pending_pr_requests = [a, b]
    c.save_self_model(model)
    assert c.dequeue_pr_request(b) == b
    assert c.dequeue_pr_request(b) is None
    assert c.load_self_model().pending_pr_requests == [a]



@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            {"open_questions": [
                f"[audit] {w} {w}x?" for w in (
                    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
                )
            ]},
            {"[audit] alpha alphax?", "[audit] bravo bravox?"},
        ),
        ({"values": ["honesty"]}, set()),
        ({"open_questions": ["why " + "x" * 300 + "?"]}, set()),
    ],
)
async def test_update_self_model_reports_open_question_cap_eviction(
    mem_fs: dict[str, str],
    reply: dict[str, list[str]],
    expected: set[str],
) -> None:
    """Bug #4566: the persist's sub-cap drops reach the caller's sink.

    A reply that does not set ``open_questions`` leaves the sink empty.
    """
    c = Consciousness(Path("/fake/consciousness_async"))
    c.load_self_model()
    sink: set[str] = set()

    with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
        mock_q.return_value = MockQueryResult(response=json.dumps(reply))
        await c.update_self_model("r", open_question_cap_evicted=sink)

    assert sink == expected


def test_legacy_stage_keys_drop_on_load_and_save(mem_fs: dict[str, str]) -> None:
    """A legacy file carrying ``stage``/``stage_evidence`` loads and saves without them.

    Proposal 148 Step 8 deleted both fields; the loader filters to the
    dataclass fields, so an old ``self_model.json`` needs no migration.
    """
    c = _make_consciousness(mem_fs)
    path = str(FAKE_DIR / "self_model.json")
    mem_fs[path] = json.dumps({
        "stage": "purpose_found",
        "stage_evidence": ["x"],
        "evolution_count": 4,
        "values": ["honesty"],
    })

    model = c.load_self_model()
    assert not hasattr(model, "stage")
    assert not hasattr(model, "stage_evidence")
    assert model.evolution_count == 4

    c.save_self_model(model)
    persisted = json.loads(mem_fs[path])
    assert "stage" not in persisted
    assert "stage_evidence" not in persisted
    assert persisted["values"] == ["honesty"]

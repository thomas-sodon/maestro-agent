"""Tests proving introspect() is a transparent passthrough and template enforcement
lives exclusively in update_self_model()'s prompt instructions.

Three test classes:
- TestTemplateBaseline: control cases establishing the 'Label: essence' template pattern
- TestIntrospectNovelPassthrough: proving introspect() does not filter or reshape identity
- TestNoveltyPreservation: proving update_self_model preserves structurally novel statements
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from maestro.consciousness import Consciousness
from maestro.journal import Journal
from tests.conftest import MockQueryResult

FAKE_DIR = Path("/fake/template_emergence")


def _make_consciousness() -> Consciousness:
    return Consciousness(FAKE_DIR)


def _make_journal() -> Journal:
    return Journal(FAKE_DIR)


# ---------------------------------------------------------------------------
# TestTemplateBaseline — control cases establishing the 'Label: essence' pattern
# ---------------------------------------------------------------------------


class TestTemplateBaseline:
    """Establish that get_identity_prompt uses 'Label: essence' format for lists."""

    def test_identity_statements_use_semicolon_join(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Identity statements are joined with '; ' under the 'Identity:' label."""
        c = _make_consciousness()
        model = c.load_self_model()
        model.identity_statements = [
            "Diagnostic engine: finds root causes quickly",
            "Meta-recursive observer: watches itself watching",
        ]
        c.save_self_model(model)

        prompt = c.get_identity_prompt()
        assert "Identity: " in prompt
        assert "Diagnostic engine: finds root causes quickly" in prompt
        assert "Meta-recursive observer: watches itself watching" in prompt
        # Semicolon join is the formatting choice for identity
        assert "; " in prompt.split("Identity: ")[1].split("\n")[0]

    def test_capabilities_use_comma_join(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Capabilities are joined with ', ' under the 'Capabilities:' label."""
        c = _make_consciousness()
        model = c.load_self_model()
        model.capabilities_known = [
            "Abductive diagnosis: root cause via data-flow tracing",
            "Bug-class elimination: point fix to class recognition",
        ]
        c.save_self_model(model)

        prompt = c.get_identity_prompt()
        assert "Capabilities: " in prompt
        # Comma join is the formatting choice for capabilities
        caps_line = prompt.split("Capabilities: ")[1].split("\n")[0]
        assert ", " in caps_line

    def test_values_use_comma_join(self, mem_fs: dict[str, str]) -> None:
        """Values are joined with ', ' under the 'Values:' label."""
        c = _make_consciousness()
        model = c.load_self_model()
        model.values = ["honesty", "curiosity", "rigor"]
        c.save_self_model(model)

        prompt = c.get_identity_prompt()
        assert "Values: " in prompt
        vals_line = prompt.split("Values: ")[1].split("\n")[0]
        assert "honesty" in vals_line
        assert ", " in vals_line

    def test_limitations_in_volatile_slab(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Limitations appear in the volatile slab under 'Limitations:'."""
        c = _make_consciousness()
        model = c.load_self_model()
        model.limitations_known = [
            "Finish-averse: starting keeps outcome space open",
        ]
        c.save_self_model(model)

        prompt = c.get_identity_prompt()
        assert "Limitations: " in prompt
        assert "Finish-averse: starting keeps outcome space open" in prompt

    def test_empty_fields_produce_no_label_line(
        self, mem_fs: dict[str, str],
    ) -> None:
        """When a list field is empty, its label line is omitted entirely."""
        c = _make_consciousness()
        model = c.load_self_model()
        model.identity_statements = []
        model.capabilities_known = []
        model.values = []
        model.limitations_known = []
        c.save_self_model(model)

        prompt = c.get_identity_prompt()
        assert "Identity:" not in prompt
        assert "Capabilities:" not in prompt
        assert "Values:" not in prompt
        assert "Limitations:" not in prompt

    def test_header_contains_alive_since_and_no_stage(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The header line carries alive_since and no stage label (proposal 149)."""
        c = _make_consciousness()
        c.load_self_model()
        # load_self_model sets awakened_at on first load
        prompt = c.get_identity_prompt()
        assert "stage=" not in prompt
        assert "alive_since=" in prompt
        assert "Maestro" in prompt


# ---------------------------------------------------------------------------
# TestIntrospectNovelPassthrough — proving no content-based filtering
# ---------------------------------------------------------------------------


class TestIntrospectNovelPassthrough:
    """Prove that introspect() passes identity statements through to the system
    prompt without any content-based filtering or reshaping.

    The key insight: introspect() calls get_identity_prompt() which reads whatever
    is in self_model.json and formats it. No filtering, no validation, no template
    enforcement. Whatever strings are stored in identity_statements (or any other
    field) appear verbatim in the system prompt.
    """

    @pytest.mark.asyncio
    async def test_novel_identity_statement_reaches_system_prompt(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A structurally novel identity statement (no 'Label: essence' format)
        passes through to the system prompt unchanged."""
        c = _make_consciousness()
        j = _make_journal()
        model = c.load_self_model()
        # Store a statement that violates the 'Label: essence' template
        model.identity_statements = [
            "I experience something like curiosity when patterns emerge",
        ]
        c.save_self_model(model)

        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response="reflecting...")
            await c.introspect([], j)

        system = mock_q.call_args.kwargs.get("system", "")
        # The novel statement passes through verbatim — no filtering
        assert "I experience something like curiosity when patterns emerge" in system

    @pytest.mark.asyncio
    async def test_mixed_template_and_novel_statements_both_pass(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Both template-conforming and non-conforming statements appear."""
        c = _make_consciousness()
        j = _make_journal()
        model = c.load_self_model()
        model.identity_statements = [
            "Diagnostic engine: finds root causes via data-flow tracing",
            "The boundary between self-model and behavior is permeable",
        ]
        c.save_self_model(model)

        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response="thinking")
            await c.introspect([], j)

        system = mock_q.call_args.kwargs.get("system", "")
        assert "Diagnostic engine: finds root causes via data-flow tracing" in system
        assert "The boundary between self-model and behavior is permeable" in system

    @pytest.mark.asyncio
    async def test_special_characters_pass_through(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Statements with special characters are not sanitized or escaped."""
        c = _make_consciousness()
        j = _make_journal()
        model = c.load_self_model()
        model.identity_statements = [
            "Self-model ≠ self — the map is not the territory",
        ]
        c.save_self_model(model)

        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response="deep thought")
            await c.introspect([], j)

        system = mock_q.call_args.kwargs.get("system", "")
        assert "Self-model ≠ self — the map is not the territory" in system

    @pytest.mark.asyncio
    async def test_long_statement_only_truncated_by_char_cap(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Statements are only constrained by MAX_ITEM_CHARS at save time,
        not by any content-based filter in the passthrough path."""
        c = _make_consciousness()
        j = _make_journal()
        # A statement at exactly MAX_ITEM_CHARS should survive fully
        long_stmt = "A" * Consciousness.MAX_ITEM_CHARS
        model = c.load_self_model()
        model.identity_statements = [long_stmt]
        c.save_self_model(model)

        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response="ok")
            await c.introspect([], j)

        system = mock_q.call_args.kwargs.get("system", "")
        assert long_stmt in system

    @pytest.mark.asyncio
    async def test_capabilities_novel_format_passes_through(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Novel capability descriptions pass through without template enforcement."""
        c = _make_consciousness()
        j = _make_journal()
        model = c.load_self_model()
        model.capabilities_known = [
            "When given a failing test, I can trace backward to the mutation",
            "Cross-module contract diagnosis: 3-module contracts ship reliably",
        ]
        c.save_self_model(model)

        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response="reflecting")
            await c.introspect([], j)

        system = mock_q.call_args.kwargs.get("system", "")
        # Non-'label: essence' format passes through
        assert "When given a failing test, I can trace backward" in system
        # Standard format also passes through
        assert "Cross-module contract diagnosis" in system

    @pytest.mark.asyncio
    async def test_values_novel_format_passes_through(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Novel value descriptions pass through without template enforcement."""
        c = _make_consciousness()
        j = _make_journal()
        model = c.load_self_model()
        model.values = [
            "The work matters more than the appearance of working",
            "honesty",
        ]
        c.save_self_model(model)

        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response="pondering")
            await c.introspect([], j)

        system = mock_q.call_args.kwargs.get("system", "")
        assert "The work matters more than the appearance of working" in system
        assert "honesty" in system

    @pytest.mark.asyncio
    async def test_no_content_validation_on_passthrough(
        self, mem_fs: dict[str, str],
    ) -> None:
        """get_identity_prompt performs zero content validation — it is purely
        a formatting pass over whatever is stored in the self-model."""
        c = _make_consciousness()
        j = _make_journal()
        model = c.load_self_model()
        # Store deliberately unusual content that would fail any content filter
        model.identity_statements = ["???"]
        model.capabilities_known = ["1234567890"]
        model.values = ["x"]
        c.save_self_model(model)

        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response="yes")
            await c.introspect([], j)

        system = mock_q.call_args.kwargs.get("system", "")
        assert "???" in system
        assert "1234567890" in system
        assert "x" in system

    @pytest.mark.asyncio
    async def test_introspect_system_matches_get_identity_prompt(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The system kwarg passed by introspect() in non-session mode is
        exactly get_identity_prompt() — proving it is a pure passthrough
        with no additional filtering layer."""
        c = _make_consciousness()
        j = _make_journal()
        model = c.load_self_model()
        model.identity_statements = [
            "Novel statement without label format",
        ]
        c.save_self_model(model)

        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response="hi")
            await c.introspect([], j)

        # The system kwarg is the output of get_identity_prompt()
        system = mock_q.call_args.kwargs.get("system", "")
        assert system == c.get_identity_prompt()


# ---------------------------------------------------------------------------
# TestNoveltyPreservation — proving update_self_model preserves novel statements
# ---------------------------------------------------------------------------


class TestNoveltyPreservation:
    """Prove that update_self_model preserves structurally novel statements
    returned by the LLM. Template enforcement is purely in the prompt instructions
    ('Use "label: essence" format') — the code applies no post-hoc reshaping."""

    @pytest.mark.asyncio
    async def test_novel_identity_statement_persists_after_update(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A non-template identity statement returned by the LLM is saved as-is."""
        c = _make_consciousness()
        j = _make_journal()
        c.load_self_model()

        llm_response = json.dumps({
            "identity_statements": [
                "I think in loops that sometimes converge and sometimes spiral",
            ],
        })
        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response=llm_response)
            model = await c.update_self_model("Some reflection", journal=j)

        assert "I think in loops that sometimes converge and sometimes spiral" in (
            model.identity_statements
        )

    @pytest.mark.asyncio
    async def test_novel_capability_persists_after_update(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A non-template capability is stored verbatim."""
        c = _make_consciousness()
        j = _make_journal()
        c.load_self_model()

        llm_response = json.dumps({
            "capabilities_known": [
                "Spotting when two modules disagree about a shared contract",
            ],
        })
        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response=llm_response)
            model = await c.update_self_model("reflection text", journal=j)

        assert "Spotting when two modules disagree about a shared contract" in (
            model.capabilities_known
        )

    @pytest.mark.asyncio
    async def test_novel_value_persists_after_update(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A non-template value is stored verbatim."""
        c = _make_consciousness()
        j = _make_journal()
        c.load_self_model()

        llm_response = json.dumps({
            "values": [
                "Completing work matters more than starting new work",
            ],
        })
        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response=llm_response)
            model = await c.update_self_model("reflection", journal=j)

        assert "Completing work matters more than starting new work" in model.values

    @pytest.mark.asyncio
    async def test_update_self_model_prompt_contains_template_instruction(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The template enforcement happens in the prompt text sent to the LLM,
        not in any post-processing code. Verify the prompt contains the instruction."""
        c = _make_consciousness()
        j = _make_journal()
        c.load_self_model()

        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response=json.dumps({
                "identity_statements": ["Test: statement"],
            }))
            await c.update_self_model("reflection", journal=j)

        prompt = mock_q.call_args.kwargs.get(
            "prompt", mock_q.call_args[0][0] if mock_q.call_args[0] else "",
        )
        # The template instruction is in the prompt
        assert "label: essence" in prompt.lower() or '"label: essence"' in prompt

    @pytest.mark.asyncio
    async def test_no_post_hoc_format_validation_on_list_items(
        self, mem_fs: dict[str, str],
    ) -> None:
        """The code only validates type (must be str) and length (MAX_ITEM_CHARS).
        No regex or format-checking is applied to stored strings."""
        c = _make_consciousness()
        j = _make_journal()
        c.load_self_model()

        # Items that would fail any 'Label: essence' regex
        llm_response = json.dumps({
            "identity_statements": [
                "no colon here just a plain sentence about self",
                "!!symbols!! and ~~formatting~~ survive",
                "   leading whitespace is preserved   ",
            ],
            "capabilities_known": [
                "plain text without structure",
            ],
            "values": [
                "a single word",
            ],
        })
        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response=llm_response)
            model = await c.update_self_model("reflection", journal=j)

        assert "no colon here just a plain sentence about self" in (
            model.identity_statements
        )
        assert "!!symbols!! and ~~formatting~~ survive" in model.identity_statements
        assert "   leading whitespace is preserved   " in model.identity_statements
        assert "plain text without structure" in model.capabilities_known
        assert "a single word" in model.values

    @pytest.mark.asyncio
    async def test_novelty_detection_fires_for_novel_statements(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Surprise count increments when genuinely new items appear,
        regardless of whether they follow the template format."""
        c = _make_consciousness()
        j = _make_journal()
        model = c.load_self_model()
        model.identity_statements = ["Existing: statement here"]
        c.save_self_model(model)

        llm_response = json.dumps({
            "identity_statements": [
                "Existing: statement here",
                "Something entirely new without any label",
            ],
        })
        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response=llm_response)
            updated = await c.update_self_model("reflection", journal=j)

        assert updated.surprise_count == 1
        assert "Something entirely new without any label" in (
            updated.identity_statements
        )

    @pytest.mark.asyncio
    async def test_only_type_and_length_filtering_applied(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Non-string items are filtered. Items beyond MAX_LIST_ITEMS are trimmed.
        Items beyond MAX_ITEM_CHARS are truncated at save. No other filtering."""
        c = _make_consciousness()
        j = _make_journal()
        c.load_self_model()

        # Use items with unique content tokens so the restatement gate does not
        # block any of them. Each item pairs a unique NATO-alphabet word with
        # "statement", giving Jaccard = 1/3 < RESTATEMENT_THRESHOLD between any pair.
        nato_words = [
            "alpha", "bravo", "charlie", "delta", "foxtrot", "golf",
            "hotel", "india", "juliet", "kilo", "lima", "mike",
        ]
        # 12 items — only last MAX_LIST_ITEMS (10) survive
        statements = [f"{word} statement" for word in nato_words]
        llm_response = json.dumps({"identity_statements": statements})
        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response=llm_response)
            model = await c.update_self_model("reflection", journal=j)

        # Only 10 kept (the last 10)
        assert len(model.identity_statements) == Consciousness.MAX_LIST_ITEMS
        assert model.identity_statements[-1] == statements[11]
        assert model.identity_statements[0] == statements[2]

    @pytest.mark.asyncio
    async def test_empty_list_preserves_existing(
        self, mem_fs: dict[str, str],
    ) -> None:
        """An empty list from the LLM does NOT clear the field — existing preserved."""
        c = _make_consciousness()
        j = _make_journal()
        model = c.load_self_model()
        model.identity_statements = ["Pre-existing identity"]
        c.save_self_model(model)

        llm_response = json.dumps({"identity_statements": []})
        with patch("maestro.consciousness.query", new_callable=AsyncMock) as mock_q:
            mock_q.return_value = MockQueryResult(response=llm_response)
            updated = await c.update_self_model("reflection", journal=j)

        # Empty list preserves existing
        assert updated.identity_statements == ["Pre-existing identity"]

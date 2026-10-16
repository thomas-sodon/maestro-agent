"""Tests for _note_session_effort_override (sdk.py session-effort observability)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from maestro.sdk import _note_session_effort_override


class TestNoteSessionEffortOverride:
    """Verify the helper emits exactly the right log traces."""

    def test_emits_debug_when_both_present(self, caplog: pytest.LogCaptureFixture) -> None:
        """When both effort and session are non-None, emit one debug line."""
        session = MagicMock(name="fake_session")
        with caplog.at_level(logging.DEBUG, logger="maestro"):
            _note_session_effort_override("introspect", "medium", session)

        debug_messages = [
            r.message for r in caplog.records if r.levelno == logging.DEBUG
        ]
        assert len(debug_messages) == 1
        assert "effort=medium" in debug_messages[0]
        assert "caller=introspect" in debug_messages[0]

    def test_silent_when_session_is_none(self, caplog: pytest.LogCaptureFixture) -> None:
        """No log when session is None — the effort will take effect normally."""
        with caplog.at_level(logging.DEBUG, logger="maestro"):
            _note_session_effort_override("reflect", "high", None)

        assert len(caplog.records) == 0

    def test_silent_when_effort_is_none(self, caplog: pytest.LogCaptureFixture) -> None:
        """No log when effort is None — nothing to override."""
        session = MagicMock(name="fake_session")
        with caplog.at_level(logging.DEBUG, logger="maestro"):
            _note_session_effort_override("dream", None, session)

        assert len(caplog.records) == 0

    def test_does_not_mutate_session(self) -> None:
        """The helper is observability-only — session must not be touched."""
        session = MagicMock(name="fake_session")
        _note_session_effort_override("propose_plan", "high", session)

        # No attributes should have been set or methods called on the session
        assert session.method_calls == []
        assert session.call_args_list == []

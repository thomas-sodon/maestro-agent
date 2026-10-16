"""Tests for maestro.logs — log reading, SDK session listing, CLI handler."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from maestro import transcript
from maestro.cli import cmd_logs
from maestro.config import Config
from maestro.logs import (
    _format_transcript_turns,
    iter_transcript_records,
    list_sdk_sessions,
    parse_app_log_tail,
    read_sdk_session_file,
    read_transcript_detail,
    read_transcript_summaries,
    show_sdk_session,
    tail_file,
)
from tests.conftest import capture_stdout

FAKE_DIR = Path("/fake/logs")
SDK_DIR = FAKE_DIR / "sdk"


# ---------------------------------------------------------------------------
# tail_file
# ---------------------------------------------------------------------------


class TestTailFile:
    def test_missing_file(self, mem_fs: dict[str, str]) -> None:
        output = capture_stdout(tail_file, FAKE_DIR / "missing.log")
        assert "No log file found" in output

    def test_reads_last_n_lines(self, mem_fs: dict[str, str]) -> None:
        content = "\n".join(f"line {i}" for i in range(20))
        mem_fs[str(FAKE_DIR / "app.log")] = content
        output = capture_stdout(tail_file, FAKE_DIR / "app.log", lines=5)
        lines = [x for x in output.strip().splitlines() if x]
        assert len(lines) == 5
        assert "line 19" in lines[-1]

    def test_reads_all_when_fewer_than_n(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(FAKE_DIR / "small.log")] = "a\nb\nc"
        output = capture_stdout(tail_file, FAKE_DIR / "small.log", lines=50)
        assert output.strip().splitlines() == ["a", "b", "c"]

    def test_follow_calls_subprocess(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(FAKE_DIR / "f.log")] = "hello"
        with patch("maestro.logs.subprocess.run") as mock_run:
            tail_file(FAKE_DIR / "f.log", lines=10, follow=True)
        mock_run.assert_called_once_with(
            ["tail", "-n", "10", "-f", str(FAKE_DIR / "f.log")],
            check=False,
        )

    def test_follow_handles_keyboard_interrupt(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(FAKE_DIR / "f.log")] = "hello"
        with patch("maestro.logs.subprocess.run", side_effect=KeyboardInterrupt):
            tail_file(FAKE_DIR / "f.log", follow=True)

    def test_lines_zero_returns_early(self, mem_fs: dict[str, str]) -> None:
        """Line 19: lines <= 0 returns immediately."""
        mem_fs[str(FAKE_DIR / "app.log")] = "line1\nline2\n"
        output = capture_stdout(tail_file, FAKE_DIR / "app.log", lines=0)
        assert output == ""

    def test_oserror_reading_file(self, mem_fs: dict[str, str]) -> None:
        """OSError during open() in tail_file (non-follow path)."""
        mem_fs[str(FAKE_DIR / "app.log")] = "content"
        with patch("builtins.open", side_effect=OSError("permission denied")):
            output = capture_stdout(tail_file, FAKE_DIR / "app.log")
        assert "Cannot read" in output
        assert "permission denied" in output

    def test_unicode_decode_error(self, mem_fs: dict[str, str]) -> None:
        """UnicodeDecodeError during open() in tail_file (non-follow path)."""
        mem_fs[str(FAKE_DIR / "binary.log")] = "irrelevant"
        with patch("builtins.open", side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "reason")):
            output = capture_stdout(tail_file, FAKE_DIR / "binary.log")
        assert "binary/non-UTF-8" in output

    def test_negative_lines_returns_early(self, mem_fs: dict[str, str]) -> None:
        """Negative lines should produce no output."""
        mem_fs[str(FAKE_DIR / "app.log")] = "line1\nline2\nline3\n"
        output = capture_stdout(tail_file, FAKE_DIR / "app.log", lines=-5)
        assert output == ""

    def test_empty_file(self, mem_fs: dict[str, str]) -> None:
        """Empty file should produce no output."""
        mem_fs[str(FAKE_DIR / "empty.log")] = ""
        output = capture_stdout(tail_file, FAKE_DIR / "empty.log", lines=10)
        assert output == ""

    def test_follow_negative_lines_no_subprocess(self, mem_fs: dict[str, str]) -> None:
        """Follow mode with negative lines should not call subprocess."""
        mem_fs[str(FAKE_DIR / "f.log")] = "content\n"
        with patch("maestro.logs.subprocess.run") as mock_run:
            tail_file(FAKE_DIR / "f.log", lines=-1, follow=True)
            mock_run.assert_not_called()

    def test_follow_zero_lines_no_subprocess(self, mem_fs: dict[str, str]) -> None:
        """Follow mode with zero lines should not call subprocess."""
        mem_fs[str(FAKE_DIR / "f.log")] = "content\n"
        with patch("maestro.logs.subprocess.run") as mock_run:
            tail_file(FAKE_DIR / "f.log", lines=0, follow=True)
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# list_sdk_sessions
# ---------------------------------------------------------------------------


class TestListSdkSessions:
    """The listing renders from ``read_transcript_summaries`` (proposal 104).

    Sessions are built through the summaries reader's ``.jsonl`` / ``.log``
    date-dir shape (the hand-built ``_mock_date_dir`` / ``_mock_file`` idiom):
    a stamped sidecar surfaces its phase, a legacy ``.log`` still shows
    ``filename (bytes)``.
    """

    @staticmethod
    def _sdk(date_dirs: list[MagicMock]) -> MagicMock:
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = date_dirs
        return mock_sdk

    def test_no_dir(self, mem_fs: dict[str, str]) -> None:
        output = capture_stdout(list_sdk_sessions, SDK_DIR)
        assert "No SDK logs found" in output

    def test_max_dates_zero_returns_early(self, mem_fs: dict[str, str]) -> None:
        """max_dates <= 0 returns immediately without touching the directory."""
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        output = capture_stdout(list_sdk_sessions, mock_sdk, max_dates=0)
        assert output == ""
        mock_sdk.iterdir.assert_not_called()

    def test_negative_max_dates_shows_nothing(self, mem_fs: dict[str, str]) -> None:
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        output = capture_stdout(list_sdk_sessions, mock_sdk, max_dates=-1)
        assert output == ""

    def test_stamped_row_shows_phase(self, mem_fs: dict[str, str]) -> None:
        """The increment's core acceptance: a stamped row surfaces its phase."""
        record = _record(
            turns=[transcript.TranscriptTurn("text", "assistant", "t", "hi")],
        )
        sidecar = _mock_file(
            "14-30-05_contemplate_abc123.jsonl",
            size=321,
            text=transcript.to_jsonl_line(record),
        )
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar])

        output = capture_stdout(list_sdk_sessions, self._sdk([date_dir]))
        assert "--- 2026-03-22 (1 sessions) ---" in output
        assert "14-30-05_contemplate_abc123.jsonl" in output
        assert "phase=contemplate" in output
        assert "session=session-A" in output
        assert "iter=7" in output
        assert "task=task-9" in output
        assert "turns=1" in output
        assert "321 bytes" in output

    def test_legacy_row_shows_filename_and_bytes(self, mem_fs: dict[str, str]) -> None:
        """A legacy ``.log``-only session still prints ``filename (bytes)``."""
        log = _mock_file("14-30-05_abc123.log", size=1024)
        date_dir = _mock_date_dir("2026-03-22", log=[log])

        output = capture_stdout(list_sdk_sessions, self._sdk([date_dir]))
        assert "14-30-05_abc123.log  (1,024 bytes)" in output
        assert "phase=" not in output

    def test_null_provenance_fields_show_dash(self, mem_fs: dict[str, str]) -> None:
        """A stamped row with null session/iter/task renders the ``-`` sentinel."""
        record = _record(
            session_label=None, iteration=None, task_id=None, turns=[],
        )
        sidecar = _mock_file(
            "14-30-05_contemplate_abc123.jsonl",
            text=transcript.to_jsonl_line(record),
        )
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar])

        output = capture_stdout(list_sdk_sessions, self._sdk([date_dir]))
        assert "phase=contemplate" in output
        assert "session=- iter=- task=- turns=0" in output

    def test_mixed_rows_grouped_under_one_date_header(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A stamped and a legacy session group under one ``(N sessions)`` header."""
        sidecar = _mock_file(
            "14-30-05_contemplate_abc123.jsonl",
            text=transcript.to_jsonl_line(_record(turns=[])),
        )
        log = _mock_file("15-00-00_def456.log", size=200)
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar], log=[log])

        output = capture_stdout(list_sdk_sessions, self._sdk([date_dir]))
        assert "--- 2026-03-22 (2 sessions) ---" in output
        assert "phase=contemplate" in output
        assert "15-00-00_def456.log  (200 bytes)" in output

    def test_date_filter_no_match(self, mem_fs: dict[str, str]) -> None:
        sidecar = _mock_file(
            "14-30-05_contemplate_abc123.jsonl",
            text=transcript.to_jsonl_line(_record()),
        )
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar])

        output = capture_stdout(
            list_sdk_sessions, self._sdk([date_dir]), date="2026-01-01",
        )
        assert "No SDK logs for date 2026-01-01." in output

    def test_date_filter_match(self, mem_fs: dict[str, str]) -> None:
        sidecar = _mock_file(
            "14-30-05_contemplate_abc123.jsonl",
            text=transcript.to_jsonl_line(_record(turns=[])),
        )
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar])

        output = capture_stdout(
            list_sdk_sessions, self._sdk([date_dir]), date="2026-03-22",
        )
        assert "--- 2026-03-22 (1 sessions) ---" in output
        assert "phase=contemplate" in output

    def test_date_filter_finds_dir_older_than_max_dates(
        self, mem_fs: dict[str, str],
    ) -> None:
        """``--date`` resolves a dir beyond the ``max_dates`` recency window.

        Regression: the listing sliced to the most-recent ``max_dates`` dirs
        *before* applying the date filter, so ``--list --date <old>`` wrongly
        reported 'No SDK logs for date' for a date older than the window while
        ``show_sdk_session``'s unbounded date path still found it. The reader now
        filters by date before the slice.
        """
        sidecar = _mock_file(
            "14-30-05_contemplate_abc123.jsonl",
            text=transcript.to_jsonl_line(_record(turns=[])),
        )
        older = _mock_date_dir("2026-03-22", jsonl=[sidecar])
        older.__lt__ = lambda s, o: s.name < o.name
        newest = _mock_date_dir("2026-03-23")  # empty, sorts ahead of older
        newest.__lt__ = lambda s, o: s.name < o.name

        output = capture_stdout(
            list_sdk_sessions,
            self._sdk([newest, older]),
            date="2026-03-22",
            max_dates=1,
        )
        assert "--- 2026-03-22 (1 sessions) ---" in output
        assert "phase=contemplate" in output

    def test_no_sessions_prints_empty_message(self, mem_fs: dict[str, str]) -> None:
        date_dir = _mock_date_dir("2026-03-22")  # no sidecars, no logs
        output = capture_stdout(list_sdk_sessions, self._sdk([date_dir]))
        assert "No SDK session logs found" in output

    def test_permission_error_reports_denied(self, mem_fs: dict[str, str]) -> None:
        """A directory PermissionError surfaces the diagnostic, not 'no logs'.

        read_transcript_summaries swallows the error to [] to keep its
        dashboard contract; list_sdk_sessions re-probes readability and reports
        the actionable permission error, matching show_sdk_session rather than
        masking a genuine permissions problem as 'No SDK session logs found.'.
        """
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.side_effect = PermissionError("denied")
        output = capture_stdout(list_sdk_sessions, mock_sdk)
        assert "Permission denied reading SDK log directory." in output
        assert "No SDK session logs found" not in output


# ---------------------------------------------------------------------------
# show_sdk_session
# ---------------------------------------------------------------------------


_TYPED_DETAIL = {
    "caller": "contemplate",
    "session_label": "session-A",
    "iteration": 7,
    "task_id": "task-9",
    "started_at": "2026-03-22T14:30:05Z",
    "turns": [
        {"kind": "prompt", "role": "user", "ts": "t", "content": "do the thing"},
        {"kind": "text", "role": "assistant", "ts": "t", "content": "did it"},
    ],
    "legacy": False,
}


class TestShowSdkSession:
    """The detail view renders typed turns for a stamped session (proposal 104).

    A session is resolved as a stem group (an optional ``.log`` + optional
    ``.jsonl`` sidecar). With a sidecar and no ``--follow`` the view calls
    ``read_transcript_detail`` and prints typed turns; a legacy / malformed /
    ``--follow`` path tails the raw ``.log`` as before. ``read_transcript_detail``
    is mocked (it is covered by its own tests) so these isolate dispatch.
    """

    @staticmethod
    def _sdk(date_dirs: list[MagicMock]) -> MagicMock:
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = date_dirs
        return mock_sdk

    def test_no_dir(self, mem_fs: dict[str, str]) -> None:
        output = capture_stdout(show_sdk_session, SDK_DIR)
        assert "No SDK logs found" in output

    def test_stamped_session_renders_typed_turns(self, mem_fs: dict[str, str]) -> None:
        """A stamped session renders distinct turn blocks, no ``Text delta:``."""
        log = _mock_file("14-30-05_abc123.log")
        sidecar = _mock_file("14-30-05_contemplate_abc123.jsonl")
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar], log=[log])

        with patch(
            "maestro.logs.read_transcript_detail", return_value=dict(_TYPED_DETAIL),
        ) as m_detail:
            output = capture_stdout(show_sdk_session, self._sdk([date_dir]))

        assert m_detail.call_args.args[1:] == (
            "2026-03-22", "14-30-05_contemplate_abc123.jsonl",
        )
        assert "=== SDK Session: 2026-03-22/14-30-05_contemplate_abc123.jsonl ===" in output
        assert "phase=contemplate" in output
        assert "--- prompt (user) ---" in output
        assert "--- text (assistant) ---" in output
        assert "do the thing" in output
        assert "did it" in output
        assert "Text delta:" not in output

    def test_session_search_by_hex_renders_typed(self, mem_fs: dict[str, str]) -> None:
        """A shared-hex search resolves the stamped group and renders typed turns."""
        log = _mock_file("14-30-05_abc123.log")
        sidecar = _mock_file("14-30-05_contemplate_abc123.jsonl")
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar], log=[log])

        with patch(
            "maestro.logs.read_transcript_detail", return_value=dict(_TYPED_DETAIL),
        ):
            output = capture_stdout(
                show_sdk_session, self._sdk([date_dir]), session="abc123",
            )
        assert "14-30-05_contemplate_abc123.jsonl" in output
        assert "--- text (assistant) ---" in output

    def test_session_search_by_jsonl_filename_renders_typed(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A stamped ``.jsonl`` filename copied from the listing resolves to typed view."""
        log = _mock_file("14-30-05_abc123.log")
        sidecar = _mock_file("14-30-05_contemplate_abc123.jsonl")
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar], log=[log])

        with patch(
            "maestro.logs.read_transcript_detail", return_value=dict(_TYPED_DETAIL),
        ):
            output = capture_stdout(
                show_sdk_session,
                self._sdk([date_dir]),
                session="14-30-05_contemplate_abc123.jsonl",
            )
        assert "--- prompt (user) ---" in output

    def test_legacy_session_tails_raw_log(self, mem_fs: dict[str, str]) -> None:
        """A legacy ``.log``-only session tails the raw log (no sidecar → no typed)."""
        log_path = SDK_DIR / "2026-03-22" / "14-30-05_abc123.log"
        mem_fs[str(log_path)] = "raw human-readable log content"
        date_dir = _mock_date_dir("2026-03-22", log=[log_path])

        with patch("maestro.logs.read_transcript_detail") as m_detail:
            output = capture_stdout(show_sdk_session, self._sdk([date_dir]))
        m_detail.assert_not_called()
        assert "=== SDK Session: 2026-03-22/14-30-05_abc123.log ===" in output
        assert "raw human-readable log content" in output

    def test_full_log_filename_still_resolves(self, mem_fs: dict[str, str]) -> None:
        """A full ``.log`` filename copied from the listing still tails."""
        log_path = SDK_DIR / "2026-03-22" / "14-30-05_abc123.log"
        mem_fs[str(log_path)] = "readable log content"
        date_dir = _mock_date_dir("2026-03-22", log=[log_path])

        output = capture_stdout(
            show_sdk_session, self._sdk([date_dir]), session="14-30-05_abc123.log",
        )
        assert "No SDK session matching" not in output
        assert "readable log content" in output

    def test_malformed_sidecar_falls_back_to_tail(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A sidecar that degrades to ``legacy=True`` tails the raw ``.log``."""
        log_path = SDK_DIR / "2026-03-22" / "14-30-05_abc123.log"
        mem_fs[str(log_path)] = "raw log fallback"
        sidecar = _mock_file("14-30-05_contemplate_abc123.jsonl")
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar], log=[log_path])

        with patch(
            "maestro.logs.read_transcript_detail",
            return_value={"legacy": True, "content": "blob"},
        ):
            output = capture_stdout(show_sdk_session, self._sdk([date_dir]))
        assert "=== SDK Session: 2026-03-22/14-30-05_abc123.log ===" in output
        assert "raw log fallback" in output

    def test_unreadable_sidecar_falls_back_to_tail(
        self, mem_fs: dict[str, str],
    ) -> None:
        """``read_transcript_detail`` returning ``None`` tails the raw ``.log``."""
        log_path = SDK_DIR / "2026-03-22" / "14-30-05_abc123.log"
        mem_fs[str(log_path)] = "raw log after none"
        sidecar = _mock_file("14-30-05_contemplate_abc123.jsonl")
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar], log=[log_path])

        with patch("maestro.logs.read_transcript_detail", return_value=None):
            output = capture_stdout(show_sdk_session, self._sdk([date_dir]))
        assert "14-30-05_abc123.log" in output
        assert "raw log after none" in output

    def test_follow_streams_raw_log_not_typed(self, mem_fs: dict[str, str]) -> None:
        """``--follow`` streams the live ``.log`` even for a stamped session."""
        log = _mock_file("14-30-05_abc123.log")
        sidecar = _mock_file("14-30-05_contemplate_abc123.jsonl")
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar], log=[log])

        with patch("maestro.logs.read_transcript_detail") as m_detail, \
             patch("maestro.logs.tail_file") as m_tail:
            output = capture_stdout(
                show_sdk_session, self._sdk([date_dir]), follow=True,
            )
        m_detail.assert_not_called()
        m_tail.assert_called_once_with(log, lines=50, follow=True)
        assert "=== SDK Session: 2026-03-22/14-30-05_abc123.log ===" in output

    def test_sidecar_only_renders_typed(self, mem_fs: dict[str, str]) -> None:
        """A sidecar with no sibling ``.log`` is still viewable as typed turns."""
        sidecar = _mock_file("14-30-05_contemplate_abc123.jsonl")
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar])

        with patch(
            "maestro.logs.read_transcript_detail", return_value=dict(_TYPED_DETAIL),
        ):
            output = capture_stdout(show_sdk_session, self._sdk([date_dir]))
        assert "=== SDK Session: 2026-03-22/14-30-05_contemplate_abc123.jsonl ===" in output
        assert "--- text (assistant) ---" in output

    def test_sidecar_only_malformed_tails_sidecar(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A sidecar-only group whose sidecar degrades tails the sidecar itself."""
        sidecar_path = SDK_DIR / "2026-03-22" / "14-30-05_contemplate_abc123.jsonl"
        mem_fs[str(sidecar_path)] = "raw sidecar blob"
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar_path])

        with patch(
            "maestro.logs.read_transcript_detail",
            return_value={"legacy": True, "content": "blob"},
        ):
            output = capture_stdout(show_sdk_session, self._sdk([date_dir]))
        assert "2026-03-22/14-30-05_contemplate_abc123.jsonl" in output
        assert "raw sidecar blob" in output

    def test_permission_error_with_session(self, mem_fs: dict[str, str]) -> None:
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.side_effect = PermissionError("denied")
        output = capture_stdout(show_sdk_session, mock_sdk, session="abc123")
        assert "Permission denied" in output

    def test_permission_error_no_session(self, mem_fs: dict[str, str]) -> None:
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.side_effect = PermissionError("denied")
        output = capture_stdout(show_sdk_session, mock_sdk)
        assert "Permission denied" in output

    def test_no_match_by_session(self, mem_fs: dict[str, str]) -> None:
        """A present session whose name lacks the search term does not match."""
        log = _mock_file("14-30-05_abc123.log")
        date_dir = _mock_date_dir("2026-03-22", log=[log])
        output = capture_stdout(
            show_sdk_session, self._sdk([date_dir]), session="zzz999",
        )
        assert "No SDK session matching 'zzz999'" in output

    def test_session_search_skips_non_dir_entries(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A non-directory entry in the SDK log dir is skipped during a search."""
        non_dir = MagicMock()
        non_dir.is_dir.return_value = False
        output = capture_stdout(
            show_sdk_session, self._sdk([non_dir]), session="abc123",
        )
        assert "No SDK session matching" in output

    def test_glob_metacharacters_treated_literally(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A ``*`` session term is a literal substring, not a wildcard."""
        log = _mock_file("14-30-05_abc123.log")
        date_dir = _mock_date_dir("2026-03-22", log=[log])
        output = capture_stdout(show_sdk_session, self._sdk([date_dir]), session="*")
        assert "No SDK session matching '*'" in output

    def test_no_date_dirs_no_session(self, mem_fs: dict[str, str]) -> None:
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = []
        output = capture_stdout(show_sdk_session, mock_sdk)
        assert "No SDK session logs found" in output

    def test_date_filter_no_match_no_session(self, mem_fs: dict[str, str]) -> None:
        date_dir = _mock_date_dir("2026-03-22")
        output = capture_stdout(
            show_sdk_session, self._sdk([date_dir]), date="2026-01-01",
        )
        assert "No SDK sessions found for date" in output

    def test_date_filter_match_no_session(self, mem_fs: dict[str, str]) -> None:
        log_path = SDK_DIR / "2026-03-22" / "14-30-05_abc123.log"
        mem_fs[str(log_path)] = "dated session content"
        date_dir = _mock_date_dir("2026-03-22", log=[log_path])
        output = capture_stdout(
            show_sdk_session, self._sdk([date_dir]), date="2026-03-22",
        )
        assert "2026-03-22/14-30-05_abc123.log" in output
        assert "dated session content" in output

    def test_no_sessions_in_search_dirs(self, mem_fs: dict[str, str]) -> None:
        date_dir = _mock_date_dir("2026-03-22")  # empty
        output = capture_stdout(show_sdk_session, self._sdk([date_dir]))
        assert "No SDK session logs found" in output

    def test_session_date_filter_skips_non_matching_dirs(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A session search honours ``--date``, skipping other date dirs."""
        log = _mock_file("14-30-05_abc123.log")
        wrong_date_dir = _mock_date_dir("2026-03-20", log=[log])
        output = capture_stdout(
            show_sdk_session,
            self._sdk([wrong_date_dir]),
            session="abc123",
            date="2026-03-22",
        )
        assert "No SDK session matching" in output

    def test_empty_recent_dir_skips_to_next(self, mem_fs: dict[str, str]) -> None:
        """An empty newest date dir falls through to an older populated one."""
        log_path = SDK_DIR / "2026-03-22" / "14-30-05_abc123.log"
        mem_fs[str(log_path)] = "older session"

        recent_dir = _mock_date_dir("2026-03-23")  # empty
        recent_dir.__lt__ = lambda s, o: s.name < o.name
        older_dir = _mock_date_dir("2026-03-22", log=[log_path])
        older_dir.__lt__ = lambda s, o: s.name < o.name

        output = capture_stdout(
            show_sdk_session, self._sdk([recent_dir, older_dir]),
        )
        assert "2026-03-22/14-30-05_abc123.log" in output
        assert "older session" in output


# ---------------------------------------------------------------------------
# _format_transcript_turns
# ---------------------------------------------------------------------------


class TestFormatTranscriptTurns:
    def test_full_provenance_and_typed_turns(self) -> None:
        detail = {
            "caller": "contemplate",
            "session_label": "session-A",
            "iteration": 7,
            "task_id": "task-9",
            "started_at": "2026-03-22T14:30:05Z",
            "turns": [
                {"kind": "thinking", "role": "assistant", "ts": "t", "content": "hmm"},
                {"kind": "text", "role": "assistant", "ts": "t", "content": "answer"},
            ],
            "legacy": False,
        }
        out = _format_transcript_turns(detail)
        assert "phase=contemplate" in out
        assert "session=session-A" in out
        assert "iter=7" in out
        assert "task=task-9" in out
        assert "started=2026-03-22T14:30:05Z" in out
        assert "--- thinking (assistant) ---" in out
        assert "hmm" in out
        assert "--- text (assistant) ---" in out
        assert "answer" in out

    def test_unknown_kind_falls_back_to_generic_label(self) -> None:
        """An unknown ``kind`` renders under the generic ``turn`` label; the
        null header fields are omitted."""
        detail = {
            "caller": "act",
            "session_label": None,
            "iteration": None,
            "task_id": None,
            "started_at": None,
            "turns": [{"kind": "bogus", "role": "system", "ts": "t", "content": "x"}],
            "legacy": False,
        }
        out = _format_transcript_turns(detail)
        assert "phase=act" in out
        assert "session=" not in out
        assert "iter=" not in out
        assert "task=" not in out
        assert "started=" not in out
        assert "--- turn (system) ---" in out
        assert "x" in out

    def test_empty_header_and_no_turns(self) -> None:
        """All-null provenance and no turns render as an empty string."""
        detail = {
            "caller": None,
            "session_label": None,
            "iteration": None,
            "task_id": None,
            "started_at": None,
            "turns": [],
            "legacy": False,
        }
        assert _format_transcript_turns(detail) == ""


# ---------------------------------------------------------------------------
# read_sdk_session_file
# ---------------------------------------------------------------------------


class TestReadSdkSessionFile:
    def test_file_not_found(self, mem_fs: dict[str, str]) -> None:
        assert read_sdk_session_file(SDK_DIR, "2026-03-22", "nope.log") is None

    def test_reads_file(self, mem_fs: dict[str, str]) -> None:
        path = str(SDK_DIR / "2026-03-22" / "session.log")
        mem_fs[path] = "log content"
        # Need to mock resolve/relative_to for path traversal check
        with patch.object(Path, "resolve", return_value=Path(path)):
            content = read_sdk_session_file(SDK_DIR, "2026-03-22", "session.log")
        assert content == "log content"

    def test_path_traversal_blocked(self, mem_fs: dict[str, str]) -> None:
        content = read_sdk_session_file(SDK_DIR, "2026-03-22", "../../etc/passwd")
        assert content is None

    def test_date_traversal_blocked(self, mem_fs: dict[str, str]) -> None:
        content = read_sdk_session_file(SDK_DIR, "../../../etc", "passwd")
        assert content is None

    def test_slash_in_date_blocked(self, mem_fs: dict[str, str]) -> None:
        content = read_sdk_session_file(SDK_DIR, "foo/bar", "session.log")
        assert content is None

    def test_slash_in_filename_blocked(self, mem_fs: dict[str, str]) -> None:
        content = read_sdk_session_file(SDK_DIR, "2026-03-28", "foo/bar.log")
        assert content is None

    def test_backslash_in_date_blocked(self, mem_fs: dict[str, str]) -> None:
        content = read_sdk_session_file(SDK_DIR, "foo\\bar", "session.log")
        assert content is None

    def test_backslash_in_filename_blocked(self, mem_fs: dict[str, str]) -> None:
        content = read_sdk_session_file(SDK_DIR, "2026-03-28", "foo\\bar.log")
        assert content is None

    def test_resolve_relative_to_value_error(self, mem_fs: dict[str, str]) -> None:
        """Lines 195-196: resolve().relative_to() ValueError in read_sdk_session_file (path traversal)."""
        path = str(SDK_DIR / "2026-03-22" / "session.log")
        mem_fs[path] = "content"

        # Make resolve return paths such that relative_to raises ValueError
        def fake_resolve(self: Path, strict: bool = False) -> Path:
            if "session.log" in str(self):
                return Path("/elsewhere/session.log")
            return Path("/fake/logs/sdk")

        with patch.object(Path, "is_symlink", return_value=False), \
             patch.object(Path, "resolve", fake_resolve):
            content = read_sdk_session_file(SDK_DIR, "2026-03-22", "session.log")
        assert content is None

    def test_read_text_unicode_error(self, mem_fs: dict[str, str]) -> None:
        """Lines 199-200: UnicodeDecodeError in read_sdk_session_file.read_text()."""
        path = str(SDK_DIR / "2026-03-22" / "session.log")
        mem_fs[path] = "content"

        original_resolve = Path.resolve

        def fake_resolve(self: Path, strict: bool = False) -> Path:
            return original_resolve(self, strict=strict)

        with patch.object(Path, "resolve", fake_resolve), \
             patch.object(Path, "read_text", side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad")):
            content = read_sdk_session_file(SDK_DIR, "2026-03-22", "session.log")
        assert content is None

    def test_read_text_permission_error(self, mem_fs: dict[str, str]) -> None:
        """PermissionError on read_text should return None, not crash."""
        path = str(SDK_DIR / "2026-03-22" / "session.log")
        mem_fs[path] = "content"

        original_resolve = Path.resolve

        def fake_resolve(self: Path, strict: bool = False) -> Path:
            return original_resolve(self, strict=strict)

        with patch.object(Path, "resolve", fake_resolve), \
             patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            content = read_sdk_session_file(SDK_DIR, "2026-03-22", "session.log")
        assert content is None


# ---------------------------------------------------------------------------
# read_transcript_summaries / read_transcript_detail (proposal 104)
# ---------------------------------------------------------------------------


def _record(
    caller: str = "contemplate",
    session_id: str = "abc123",
    started_at: str = "2026-03-22T14:30:05Z",
    session_label: str | None = "session-A",
    iteration: int | None = 7,
    task_id: str | None = "task-9",
    turns: list[transcript.TranscriptTurn] | None = None,
) -> transcript.TranscriptRecord:
    """Build a provenance-stamped record for a sidecar's single JSONL line."""
    return transcript.TranscriptRecord(
        caller=caller,
        session_id=session_id,
        started_at=started_at,
        session_label=session_label,
        iteration=iteration,
        task_id=task_id,
        turns=turns if turns is not None else [],
    )


def _mock_file(name: str, *, size: int = 100, text: str | None = None) -> MagicMock:
    """A glob-returned file mock exposing ``.name``/``.stat()``/``.read_text()``."""
    f = MagicMock()
    f.name = name
    f.stat.return_value = MagicMock(st_size=size)
    if text is not None:
        f.read_text.return_value = text
    return f


def _mock_date_dir(
    name: str,
    *,
    jsonl: list[MagicMock] | None = None,
    log: list[MagicMock] | None = None,
) -> MagicMock:
    """A date-dir mock whose ``.glob`` routes ``*.jsonl`` / ``*.log`` patterns.

    ``mem_fs`` does not patch ``glob``/``iterdir``/``stat``, so the tree is
    hand-built from ``MagicMock``s (the hand-built date-dir idiom).
    """
    mapping = {"*.jsonl": jsonl or [], "*.log": log or []}
    date_dir = MagicMock()
    date_dir.name = name
    date_dir.is_dir.return_value = True
    date_dir.glob.side_effect = lambda pattern: mapping[pattern]
    return date_dir


class TestReadTranscriptSummaries:
    def test_no_dir(self, mem_fs: dict[str, str]) -> None:
        assert read_transcript_summaries(SDK_DIR) == []

    def test_max_dates_zero_returns_empty(self, mem_fs: dict[str, str]) -> None:
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        assert read_transcript_summaries(mock_sdk, max_dates=0) == []

    def test_permission_error(self, mem_fs: dict[str, str]) -> None:
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.side_effect = PermissionError("denied")
        assert read_transcript_summaries(mock_sdk) == []

    def test_stamped_sidecar_surfaces_provenance(self, mem_fs: dict[str, str]) -> None:
        record = _record(
            turns=[
                transcript.TranscriptTurn("thinking", "assistant", "t", "hmm"),
                transcript.TranscriptTurn("text", "assistant", "t", "hi"),
            ],
        )
        sidecar = _mock_file(
            "14-30-05_contemplate_abc123.jsonl",
            size=321,
            text=transcript.to_jsonl_line(record),
        )
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar])

        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = [date_dir]

        result = read_transcript_summaries(mock_sdk)
        assert len(result) == 1
        row = result[0]
        assert row["date"] == "2026-03-22"
        assert row["filename"] == "14-30-05_contemplate_abc123.jsonl"
        assert row["size_bytes"] == 321
        assert row["phase"] == "contemplate"
        assert row["session"] == "session-A"
        assert row["iteration"] == 7
        assert row["task"] == "task-9"
        assert row["turn_count"] == 2
        assert row["started_at"] == "2026-03-22T14:30:05Z"
        assert row["session_id"] == "abc123"

    def test_legacy_log_has_null_provenance(self, mem_fs: dict[str, str]) -> None:
        log = _mock_file("14-30-05_abc123.log", size=555)
        date_dir = _mock_date_dir("2026-03-22", log=[log])

        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = [date_dir]

        result = read_transcript_summaries(mock_sdk)
        assert len(result) == 1
        row = result[0]
        assert row["filename"] == "14-30-05_abc123.log"
        assert row["size_bytes"] == 555
        assert row["phase"] is None
        assert row["session"] is None
        assert row["iteration"] is None
        assert row["task"] is None
        assert row["turn_count"] is None
        assert row["started_at"] is None
        assert row["session_id"] is None

    def test_stamped_session_listed_once(self, mem_fs: dict[str, str]) -> None:
        """A session with both a sidecar and a ``.log`` appears once (the sidecar).

        The ``.log`` shares the ``HH-MM-SS`` + ``<hex>`` stem with its sidecar, so
        it is deduped by :func:`_session_stem_key` and not re-listed as legacy.
        """
        sidecar = _mock_file(
            "14-30-05_contemplate_abc123.jsonl",
            text=transcript.to_jsonl_line(_record()),
        )
        log = _mock_file("14-30-05_abc123.log")
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar], log=[log])

        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = [date_dir]

        result = read_transcript_summaries(mock_sdk)
        assert len(result) == 1
        assert result[0]["filename"] == "14-30-05_contemplate_abc123.jsonl"
        assert result[0]["phase"] == "contemplate"

    def test_sidecar_stat_oserror_skipped(self, mem_fs: dict[str, str]) -> None:
        sidecar = _mock_file(
            "14-30-05_contemplate_abc123.jsonl",
            text=transcript.to_jsonl_line(_record()),
        )
        sidecar.stat.side_effect = OSError("gone")
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar])

        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = [date_dir]

        assert read_transcript_summaries(mock_sdk) == []

    def test_sidecar_read_error_skipped(self, mem_fs: dict[str, str]) -> None:
        sidecar = _mock_file("14-30-05_contemplate_abc123.jsonl")
        sidecar.read_text.side_effect = UnicodeDecodeError("utf-8", b"", 0, 1, "bad")
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar])

        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = [date_dir]

        assert read_transcript_summaries(mock_sdk) == []

    def test_malformed_sidecar_skipped(self, mem_fs: dict[str, str]) -> None:
        sidecar = _mock_file("14-30-05_contemplate_abc123.jsonl", text="not json {")
        date_dir = _mock_date_dir("2026-03-22", jsonl=[sidecar])

        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = [date_dir]

        assert read_transcript_summaries(mock_sdk) == []

    def test_log_stat_oserror_skipped(self, mem_fs: dict[str, str]) -> None:
        log = _mock_file("14-30-05_abc123.log")
        log.stat.side_effect = OSError("gone")
        date_dir = _mock_date_dir("2026-03-22", log=[log])

        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = [date_dir]

        assert read_transcript_summaries(mock_sdk) == []


class TestIterTranscriptRecords:
    def test_no_dir(self, mem_fs: dict[str, str]) -> None:
        assert list(iter_transcript_records(SDK_DIR)) == []

    def test_max_dates_zero_yields_nothing(self, mem_fs: dict[str, str]) -> None:
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        assert list(iter_transcript_records(mock_sdk, max_dates=0)) == []

    def test_permission_error(self, mem_fs: dict[str, str]) -> None:
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.side_effect = PermissionError("denied")
        assert list(iter_transcript_records(mock_sdk)) == []

    def test_parsed_malformed_unreadable_and_log_only(
        self, mem_fs: dict[str, str],
    ) -> None:
        good = _mock_file(
            "14-30-05_contemplate_abc123.jsonl",
            text=transcript.to_jsonl_line(_record()),
        )
        malformed = _mock_file("14-30-06_contemplate_def456.jsonl", text="{nope")
        unreadable = _mock_file("14-30-07_contemplate_aaa111.jsonl")
        unreadable.read_text.side_effect = OSError("gone")
        log_only = _mock_file("14-30-08_bbb222.log")
        for f in (good, malformed, unreadable):
            f.__lt__ = lambda s, o: s.name < o.name
        date_dir = _mock_date_dir(
            "2026-03-22", jsonl=[good, malformed, unreadable], log=[log_only],
        )
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = [date_dir]

        result = list(iter_transcript_records(mock_sdk))
        assert len(result) == 1
        assert result[0][0] == "2026-03-22"
        assert result[0][1].session_id == "abc123"

    def test_max_dates_bounds_to_newest(self, mem_fs: dict[str, str]) -> None:
        old = _mock_date_dir("2026-03-21", jsonl=[_mock_file(
            "a_x_old111.jsonl",
            text=transcript.to_jsonl_line(_record(session_id="old111")),
        )])
        new = _mock_date_dir("2026-03-22", jsonl=[_mock_file(
            "a_x_new111.jsonl",
            text=transcript.to_jsonl_line(_record(session_id="new111")),
        )])
        old.__lt__ = lambda self, other: self.name < other.name
        new.__lt__ = lambda self, other: self.name < other.name
        mock_sdk = MagicMock()
        mock_sdk.exists.return_value = True
        mock_sdk.iterdir.return_value = [old, new]

        result = list(iter_transcript_records(mock_sdk, max_dates=1))
        assert [date for date, _ in result] == ["2026-03-22"]


class TestReadTranscriptDetail:
    def test_missing_file_returns_none(self, mem_fs: dict[str, str]) -> None:
        assert read_transcript_detail(SDK_DIR, "2026-03-22", "nope.jsonl") is None

    def test_path_traversal_returns_none(self, mem_fs: dict[str, str]) -> None:
        """The delegated ``read_sdk_session_file`` guard rejects traversal."""
        assert (
            read_transcript_detail(SDK_DIR, "2026-03-22", "../../etc/passwd") is None
        )

    def test_jsonl_returns_typed_turns(self, mem_fs: dict[str, str]) -> None:
        record = _record(
            turns=[transcript.TranscriptTurn("text", "assistant", "t", "hi")],
        )
        filename = "14-30-05_contemplate_abc123.jsonl"
        path = str(SDK_DIR / "2026-03-22" / filename)
        mem_fs[path] = transcript.to_jsonl_line(record)

        with patch.object(Path, "resolve", return_value=Path(path)):
            detail = read_transcript_detail(SDK_DIR, "2026-03-22", filename)

        assert detail is not None
        assert detail["legacy"] is False
        assert detail["caller"] == "contemplate"
        assert detail["session_label"] == "session-A"
        assert detail["iteration"] == 7
        assert detail["task_id"] == "task-9"
        assert detail["session_id"] == "abc123"
        assert detail["turns"] == [
            {"kind": "text", "role": "assistant", "ts": "t", "content": "hi"},
        ]
        # A sidecar written before proposal 142 carries no advertised surface.
        assert detail["tools_advertised"] is None

    def test_jsonl_surfaces_tools_advertised(self, mem_fs: dict[str, str]) -> None:
        record = _record()
        record.tools_advertised = ["Read", "Monitor"]
        filename = "14-30-05_contemplate_abc123.jsonl"
        path = str(SDK_DIR / "2026-03-22" / filename)
        mem_fs[path] = transcript.to_jsonl_line(record)

        with patch.object(Path, "resolve", return_value=Path(path)):
            detail = read_transcript_detail(SDK_DIR, "2026-03-22", filename)

        assert detail is not None
        assert detail["tools_advertised"] == ["Read", "Monitor"]

    def test_jsonl_surfaces_mcp_servers_advertised(
        self, mem_fs: dict[str, str],
    ) -> None:
        record = _record()
        record.mcp_servers_advertised = ["journal"]
        filename = "14-30-05_contemplate_abc123.jsonl"
        path = str(SDK_DIR / "2026-03-22" / filename)
        mem_fs[path] = transcript.to_jsonl_line(record)

        with patch.object(Path, "resolve", return_value=Path(path)):
            detail = read_transcript_detail(SDK_DIR, "2026-03-22", filename)

        assert detail is not None
        assert detail["mcp_servers_advertised"] == ["journal"]

    def test_log_returns_blob_fallback(self, mem_fs: dict[str, str]) -> None:
        filename = "14-30-05_abc123.log"
        path = str(SDK_DIR / "2026-03-22" / filename)
        mem_fs[path] = "raw human-readable log content"

        with patch.object(Path, "resolve", return_value=Path(path)):
            detail = read_transcript_detail(SDK_DIR, "2026-03-22", filename)

        assert detail is not None
        assert detail["legacy"] is True
        assert detail["content"] == "raw human-readable log content"
        assert detail["caller"] is None
        assert detail["session_id"] is None
        assert detail["started_at"] is None
        assert detail["session_label"] is None
        assert detail["iteration"] is None
        assert detail["task_id"] is None
        assert detail["tools_advertised"] is None
        assert detail["mcp_servers_advertised"] is None

    def test_malformed_sidecar_returns_blob_fallback(
        self, mem_fs: dict[str, str],
    ) -> None:
        filename = "14-30-05_contemplate_abc123.jsonl"
        path = str(SDK_DIR / "2026-03-22" / filename)
        mem_fs[path] = "not valid json {"

        with patch.object(Path, "resolve", return_value=Path(path)):
            detail = read_transcript_detail(SDK_DIR, "2026-03-22", filename)

        assert detail is not None
        assert detail["legacy"] is True
        assert detail["content"] == "not valid json {"
        assert detail["caller"] is None


# ---------------------------------------------------------------------------
# cmd_logs CLI handler
# ---------------------------------------------------------------------------


class TestCmdLogs:
    def test_app_logs_default(self, mem_fs: dict[str, str]) -> None:
        config = Config(memory_dir=FAKE_DIR)
        mem_fs[str(config.app_log_path)] = "app log line 1\napp log line 2\n"
        args = argparse.Namespace(
            sdk=False, follow=False, lines=50, date=None,
            session=None, list_sessions=False,
        )
        output = capture_stdout(cmd_logs, args, config)
        assert "app log line 1" in output
        assert "app log line 2" in output

    def test_sdk_list(self, mem_fs: dict[str, str]) -> None:
        config = Config(memory_dir=FAKE_DIR)
        args = argparse.Namespace(
            sdk=True, follow=False, lines=50, date=None,
            session=None, list_sessions=True,
        )
        output = capture_stdout(cmd_logs, args, config)
        assert "No SDK logs found" in output

    def test_sdk_latest(self, mem_fs: dict[str, str]) -> None:
        config = Config(memory_dir=FAKE_DIR)
        args = argparse.Namespace(
            sdk=True, follow=False, lines=50, date=None,
            session=None, list_sessions=False,
        )
        output = capture_stdout(cmd_logs, args, config)
        assert "No SDK logs found" in output


# ---------------------------------------------------------------------------
# parse_app_log_tail
# ---------------------------------------------------------------------------

LOG_PATH = FAKE_DIR / "logs" / "maestro.log"


class TestParseAppLogTail:
    def test_missing_file_returns_empty(self, mem_fs: dict[str, str]) -> None:
        assert parse_app_log_tail(LOG_PATH) == []

    def test_empty_file_returns_empty(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(LOG_PATH)] = ""
        assert parse_app_log_tail(LOG_PATH) == []

    def test_parses_single_line(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(LOG_PATH)] = (
            "2026-04-05 10:00:00,000 [INFO] maestro.agent: Loop iteration 1\n"
        )
        result = parse_app_log_tail(LOG_PATH)
        assert len(result) == 1
        entry = result[0]
        assert entry["level"] == "INFO"
        assert entry["message"] == "Loop iteration 1"
        assert entry["logger"] == "maestro.agent"
        assert entry["source"] == "app"
        assert "2026-04-05" in entry["timestamp"]

    def test_parses_multiple_lines(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(LOG_PATH)] = (
            "2026-04-05 10:00:00,000 [INFO] maestro: msg one\n"
            "2026-04-05 10:00:01,000 [WARNING] maestro: msg two\n"
            "2026-04-05 10:00:02,000 [ERROR] maestro: msg three\n"
        )
        result = parse_app_log_tail(LOG_PATH)
        assert len(result) == 3
        assert result[0]["message"] == "msg one"
        assert result[2]["level"] == "ERROR"

    def test_multiline_traceback(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(LOG_PATH)] = (
            "2026-04-05 10:00:00,000 [ERROR] maestro: Something failed\n"
            "Traceback (most recent call last):\n"
            '  File "test.py", line 1\n'
            "ValueError: bad value\n"
            "2026-04-05 10:00:01,000 [INFO] maestro: Next entry\n"
        )
        result = parse_app_log_tail(LOG_PATH)
        assert len(result) == 2
        assert "Traceback" in result[0]["message"]
        assert "ValueError" in result[0]["message"]
        assert result[1]["message"] == "Next entry"

    def test_orphan_continuation_line_skipped(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(LOG_PATH)] = (
            "orphan line with no header\n"
            "2026-04-05 10:00:00,000 [INFO] maestro: real entry\n"
        )
        result = parse_app_log_tail(LOG_PATH)
        assert len(result) == 1
        assert result[0]["message"] == "real entry"

    def test_level_filter(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(LOG_PATH)] = (
            "2026-04-05 10:00:00,000 [INFO] maestro: info msg\n"
            "2026-04-05 10:00:01,000 [WARNING] maestro: warn msg\n"
        )
        result = parse_app_log_tail(LOG_PATH, level="WARNING")
        assert len(result) == 1
        assert result[0]["level"] == "WARNING"

    def test_source_filter_sdk(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(LOG_PATH)] = (
            "2026-04-05 10:00:00,000 [INFO] maestro: app msg\n"
            "2026-04-05 10:00:01,000 [INFO] maestro: SDK tool call: Bash\n"
        )
        result = parse_app_log_tail(LOG_PATH, source="sdk")
        assert len(result) == 1
        assert result[0]["source"] == "sdk"

    def test_source_filter_app(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(LOG_PATH)] = (
            "2026-04-05 10:00:00,000 [INFO] maestro: app msg\n"
            "2026-04-05 10:00:01,000 [INFO] maestro: SDK tool call: Bash\n"
        )
        result = parse_app_log_tail(LOG_PATH, source="app")
        assert len(result) == 1
        assert result[0]["source"] == "app"

    def test_n_limits_results(self, mem_fs: dict[str, str]) -> None:
        lines = "".join(
            f"2026-04-05 10:00:{i:02d},000 [INFO] maestro: msg {i}\n"
            for i in range(10)
        )
        mem_fs[str(LOG_PATH)] = lines
        result = parse_app_log_tail(LOG_PATH, n=3)
        assert len(result) == 3
        assert result[0]["message"] == "msg 7"
        assert result[2]["message"] == "msg 9"

    def test_oserror_returns_empty(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(LOG_PATH)] = "content"
        with patch("builtins.open", side_effect=OSError("denied")):
            assert parse_app_log_tail(LOG_PATH) == []

    def test_unicode_decode_error_returns_empty(self, mem_fs: dict[str, str]) -> None:
        mem_fs[str(LOG_PATH)] = "content"
        with patch(
            "builtins.open",
            side_effect=UnicodeDecodeError("utf-8", b"", 0, 1, "bad"),
        ):
            assert parse_app_log_tail(LOG_PATH) == []


class _ReadTracker:
    """Wraps a BytesIO-shaped file object and records read sizes."""

    def __init__(self, inner: Any, sink: list[int]) -> None:
        self._inner = inner
        self._sink = sink

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        data = self._inner.read(*args, **kwargs)
        self._sink.append(len(data))
        return data

    def seek(self, *args: Any, **kwargs: Any) -> int:
        return int(self._inner.seek(*args, **kwargs))

    def tell(self) -> int:
        return int(self._inner.tell())

    def __enter__(self) -> _ReadTracker:
        return self

    def __exit__(self, *exc: Any) -> None:
        self._inner.close()


class TestParseAppLogTailSeekBack:
    """Cover the seek-from-end window strategy in parse_app_log_tail."""

    def _wrap_open(
        self, real_open: Any, sink: list[int],
    ) -> Any:
        """Return an open() shim that wraps binary returns with a read tracker."""
        def wrapped(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            handle = real_open(file, mode, *args, **kwargs)
            if "b" in mode:
                return _ReadTracker(handle, sink)
            return handle
        return wrapped

    def test_large_file_reads_only_window(self, mem_fs: dict[str, str]) -> None:
        """A large log file is read via a bounded tail window, not from byte 0."""
        lines = "".join(
            f"2026-04-05 10:00:00,000 [INFO] maestro: msg {i:05d}\n"
            for i in range(10_000)
        )
        mem_fs[str(LOG_PATH)] = lines
        full_size = len(lines.encode("utf-8"))

        bytes_read: list[int] = []
        import builtins
        real_open = builtins.open
        with patch("builtins.open", side_effect=self._wrap_open(real_open, bytes_read)):
            result = parse_app_log_tail(LOG_PATH, n=50)

        assert len(result) == 50
        assert result[-1]["message"] == "msg 09999"
        assert result[0]["message"] == "msg 09950"
        # Total bytes pulled from the file must be far below the file size —
        # the initial 256 KiB window covers 50 entries comfortably.
        assert sum(bytes_read) < 512 * 1024
        assert sum(bytes_read) < full_size

    def test_multiline_traceback_at_chunk_boundary(
        self, mem_fs: dict[str, str],
    ) -> None:
        """A traceback straddling the seek-back boundary returns complete entries.

        Pad a leading entry with enough continuations that even the
        ``max(initial, n*512)`` tail window cannot reach byte 0; the seek-back
        therefore lands inside that leader and the parser must (a) drop the
        truncated leader, (b) return the two complete trailing entries
        (one of which is itself a multi-line traceback).
        """
        # Pad the leader with continuation lines so the file is much larger
        # than ``n=2`` * 512 = 1024 bytes — forcing a real seek-back.
        padding = "\n".join(f"prefix continuation line {i}" for i in range(200))
        body = (
            "2026-04-05 10:00:00,000 [INFO] maestro: prefix-leader\n"
            f"{padding}\n"
            "2026-04-05 10:00:01,000 [ERROR] maestro: real failure\n"
            "Traceback (most recent call last):\n"
            '  File "x.py", line 1\n'
            "ValueError: bad\n"
            "2026-04-05 10:00:02,000 [INFO] maestro: tail entry\n"
        )
        mem_fs[str(LOG_PATH)] = body
        # 256-byte initial window + n=2*512 = 1024 floor; the file is ~6 KB.
        with patch("maestro.logs._TAIL_READ_INITIAL_BYTES", 256):
            result = parse_app_log_tail(LOG_PATH, n=2)

        # The truncated leader must not be returned (its header is past the
        # window's start), but the two complete trailing entries must be.
        messages = [e["message"] for e in result]
        assert all("prefix-leader" not in m for m in messages)
        assert any("real failure" in m and "Traceback" in m for m in messages)
        assert any(m == "tail entry" for m in messages)

    def test_window_expands_when_filter_short(
        self, mem_fs: dict[str, str],
    ) -> None:
        """When the initial window holds fewer matches than requested, expand."""
        # Mostly INFO entries with ERRORs scattered far apart — a tiny initial
        # window will not contain enough ERRORs and the loop must re-read.
        lines: list[str] = []
        for i in range(2000):
            level = "ERROR" if i % 100 == 0 else "INFO"
            lines.append(
                f"2026-04-05 10:00:00,000 [{level}] maestro: entry {i:04d}\n"
            )
        mem_fs[str(LOG_PATH)] = "".join(lines)

        # 4 KiB initial window holds maybe 60 entries, well under 20 ERRORs;
        # expansion must kick in to satisfy n=20 with level=ERROR.
        with patch("maestro.logs._TAIL_READ_INITIAL_BYTES", 4096):
            result = parse_app_log_tail(LOG_PATH, n=20, level="ERROR")

        assert len(result) == 20
        assert all(e["level"] == "ERROR" for e in result)

    def test_window_expansion_capped_by_max_bytes(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Expansion stops at ``_TAIL_READ_MAX_BYTES`` even if n is unmet.

        Strategy: place one ERROR entry at the very start of a file filled
        with INFO entries.  Patch the initial window and max-bytes cap so that
        ``tail_bytes`` starts below the cap, doubles once to *reach* the cap,
        but the cap fires before the window is large enough to cover the
        ERROR entry at byte 0.  Filtering for ``level='ERROR'`` then yields
        zero results — proving the cap clause blocked further expansion.
        """
        # ~80 INFO entries ≈ 3920 bytes; ERROR at position 0 is unreachable
        # within a 1024-byte window.
        error_line = "2026-04-05 09:59:59,000 [ERROR] maestro: hidden error\n"
        info_lines = "".join(
            f"2026-04-05 10:00:00,000 [INFO] maestro: pad {i:03d}\n"
            for i in range(80)
        )
        mem_fs[str(LOG_PATH)] = error_line + info_lines

        # tail_bytes = max(512, 1*512) = 512.  First read: last 512 bytes
        # (all INFO, no ERROR match).  Double → 1024.  Second read: last
        # 1024 bytes (still all INFO).  1024 >= _TAIL_READ_MAX_BYTES → cap
        # fires; the ERROR at byte 0 is never reached.
        with patch("maestro.logs._TAIL_READ_INITIAL_BYTES", 512), \
             patch("maestro.logs._TAIL_READ_MAX_BYTES", 1024):
            result = parse_app_log_tail(LOG_PATH, n=1, level="ERROR")

        assert len(result) == 0

    def test_window_expansion_finds_entry_when_cap_raised(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Complementary to the cap test: raising the cap lets the entry be found.

        Same file layout, but ``_TAIL_READ_MAX_BYTES`` is large enough to
        cover the entire file.  The ERROR entry at byte 0 IS returned,
        proving the cap was the only factor blocking the previous test.
        """
        error_line = "2026-04-05 09:59:59,000 [ERROR] maestro: hidden error\n"
        info_lines = "".join(
            f"2026-04-05 10:00:00,000 [INFO] maestro: pad {i:03d}\n"
            for i in range(80)
        )
        mem_fs[str(LOG_PATH)] = error_line + info_lines

        # Same initial window, but the cap is large enough for the full file.
        with patch("maestro.logs._TAIL_READ_INITIAL_BYTES", 512), \
             patch("maestro.logs._TAIL_READ_MAX_BYTES", 8192):
            result = parse_app_log_tail(LOG_PATH, n=1, level="ERROR")

        assert len(result) == 1
        assert result[0]["message"] == "hidden error"

    def test_window_with_no_header_drops_lines_and_expands(
        self, mem_fs: dict[str, str],
    ) -> None:
        """Seek-back landing in a header-less continuation block expands until found.

        Build one log entry whose continuation tail is longer than the initial
        window. The first read pulls only continuations (no header), the
        for-else branch must drop them all (``lines = []``), and the loop must
        expand until ``offset == 0`` lets the header be read.
        """
        body = (
            "2026-04-05 10:00:00,000 [INFO] maestro: header\n"
            + "\n".join(f"continuation x{i:04d}" for i in range(2000))
            + "\n"
        )
        mem_fs[str(LOG_PATH)] = body
        # 256-byte initial window cannot reach the leading header.
        with patch("maestro.logs._TAIL_READ_INITIAL_BYTES", 256):
            result = parse_app_log_tail(LOG_PATH, n=1)
        assert len(result) == 1
        assert result[0]["message"].startswith("header")
        assert "continuation x1999" in result[0]["message"]

    def test_offset_zero_short_circuits(self, mem_fs: dict[str, str]) -> None:
        """A file smaller than the initial window is parsed in a single read."""
        mem_fs[str(LOG_PATH)] = (
            "2026-04-05 10:00:00,000 [INFO] maestro: only entry\n"
        )
        bytes_read: list[int] = []
        import builtins
        real_open = builtins.open
        with patch("builtins.open", side_effect=self._wrap_open(real_open, bytes_read)):
            result = parse_app_log_tail(LOG_PATH, n=10)
        assert len(result) == 1
        # Exactly one read pulled the whole file — no expansion loop ran.
        assert len(bytes_read) == 1

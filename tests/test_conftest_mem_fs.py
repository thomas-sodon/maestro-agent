"""Tests for the mem_fs fixture's binary and text mode dispatch."""

from __future__ import annotations

import pytest


def test_wb_accepts_bytes_and_persists(mem_fs: dict[str, str]) -> None:
    """Writing bytes via 'wb' mode should persist decoded UTF-8 content."""
    with open("/fake/file.bin", "wb") as f:
        f.write(b"hello bytes")
    assert mem_fs["/fake/file.bin"] == "hello bytes"


def test_wb_overwrites_existing(mem_fs: dict[str, str]) -> None:
    """'wb' mode should overwrite any existing content."""
    mem_fs["/fake/file.bin"] = "old content"
    with open("/fake/file.bin", "wb") as f:
        f.write(b"new content")
    assert mem_fs["/fake/file.bin"] == "new content"


def test_ab_appends_bytes_to_existing(mem_fs: dict[str, str]) -> None:
    """Appending bytes via 'ab' mode should concatenate with existing content."""
    mem_fs["/fake/log"] = "line1\n"
    with open("/fake/log", "ab") as f:
        f.write(b"line2\n")
    assert mem_fs["/fake/log"] == "line1\nline2\n"


def test_ab_creates_new_file(mem_fs: dict[str, str]) -> None:
    """'ab' mode on a non-existent file should create it."""
    with open("/fake/new.log", "ab") as f:
        f.write(b"first")
    assert mem_fs["/fake/new.log"] == "first"


def test_rb_reads_back_as_bytes(mem_fs: dict[str, str]) -> None:
    """Reading via 'rb' mode should return bytes."""
    mem_fs["/fake/data"] = "content"
    with open("/fake/data", "rb") as f:
        data = f.read()
    assert data == b"content"
    assert isinstance(data, bytes)


def test_text_write_mode_still_works(mem_fs: dict[str, str]) -> None:
    """Text 'w' mode regression guard."""
    with open("/fake/text.txt", "w") as f:
        f.write("hello text")
    assert mem_fs["/fake/text.txt"] == "hello text"


def test_text_append_mode_still_works(mem_fs: dict[str, str]) -> None:
    """Text 'a' mode regression guard."""
    mem_fs["/fake/text.txt"] = "start "
    with open("/fake/text.txt", "a") as f:
        f.write("end")
    assert mem_fs["/fake/text.txt"] == "start end"


def test_text_read_mode_still_works(mem_fs: dict[str, str]) -> None:
    """Text default-read mode regression guard (implicit 'r')."""
    mem_fs["/fake/text.txt"] = "read me"
    with open("/fake/text.txt") as f:
        data = f.read()
    assert data == "read me"
    assert isinstance(data, str)


def test_wb_with_utf8_content(mem_fs: dict[str, str]) -> None:
    """Binary write with multi-byte UTF-8 should round-trip correctly."""
    content = "café résumé"
    with open("/fake/utf8.bin", "wb") as f:
        f.write(content.encode("utf-8"))
    assert mem_fs["/fake/utf8.bin"] == content
    # Verify round-trip through rb
    with open("/fake/utf8.bin", "rb") as f:
        assert f.read() == content.encode("utf-8")


def test_wb_non_utf8_raises_value_error(mem_fs: dict[str, str]) -> None:
    """'wb' mode with non-UTF-8 bytes should raise ValueError, not UnicodeDecodeError."""
    with pytest.raises(ValueError, match="mem_fs binary mode requires UTF-8"), open(
        "/fake/bin.dat", "wb"
    ) as f:
        f.write(b"\xff\xfe")


def test_ab_non_utf8_raises_value_error(mem_fs: dict[str, str]) -> None:
    """'ab' mode appending non-UTF-8 bytes should raise ValueError, not UnicodeDecodeError."""
    mem_fs["/fake/log"] = "existing text\n"
    with pytest.raises(ValueError, match="mem_fs binary mode requires UTF-8"), open(
        "/fake/log", "ab"
    ) as f:
        f.write(b"\xff\xfe")

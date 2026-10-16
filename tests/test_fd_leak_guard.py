"""The per-test fd-leak guard reports a seeded leak and stays quiet otherwise (#4213)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._fd_leak_guard import fd_leak_message, open_fd_count


def test_open_fd_count_sees_a_seeded_pipe() -> None:
    before = open_fd_count()
    read_fd, write_fd = os.pipe()
    try:
        during = open_fd_count()
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert before is not None
    assert during == before + 2
    assert fd_leak_message(before, during) is not None
    assert fd_leak_message(before, open_fd_count()) is None


def test_open_fd_count_is_none_without_proc() -> None:
    assert open_fd_count("/nonexistent/proc/self/fd") is None


def test_fd_leak_message_is_silent_when_unmeasurable_or_shrinking() -> None:
    assert fd_leak_message(None, 5) is None
    assert fd_leak_message(5, None) is None
    assert fd_leak_message(5, 5) is None
    assert fd_leak_message(6, 5) is None


def test_fd_leak_message_names_the_growth() -> None:
    assert fd_leak_message(5, 7) == (
        "file-descriptor leak: 2 descriptor(s) left open by this test (5 -> 7)"
    )


def test_mem_fs_still_closes_real_descriptors(mem_fs: dict[str, str]) -> None:
    """``mem_fs`` swallows only its fake fds; a real ``os.close`` still closes."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    os.close(write_fd)
    assert not Path(f"/proc/self/fd/{read_fd}").is_symlink()
    assert not Path(f"/proc/self/fd/{write_fd}").is_symlink()
    # The fake lock fd is swallowed; a real close of it would raise EBADF.
    os.close(os.open("/fake/lock", os.O_RDWR))


def test_guard_fixture_is_autouse(request: pytest.FixtureRequest) -> None:
    """The conftest guard is wired into every test, so a leak cannot slip past."""
    assert "_measure_fd_leak" in request.fixturenames


def _drive_guard(marker: str | None, leak: bool) -> None:
    """Run the conftest guard's generator around a (possibly leaking) body."""
    from tests.conftest import _measure_fd_leak

    node = type("Node", (), {"get_closest_marker": lambda self, name: marker})()
    request = type("Req", (), {"node": node})()
    gen = _measure_fd_leak.__wrapped__(request)  # type: ignore[attr-defined]
    next(gen)
    fds = os.pipe() if leak else ()
    try:
        with pytest.raises(StopIteration):
            next(gen)
    finally:
        for fd in fds:
            os.close(fd)


def test_guard_fixture_fails_a_leaking_test() -> None:
    with pytest.raises(pytest.fail.Exception, match="file-descriptor leak: 2"):
        _drive_guard(None, leak=True)


def test_guard_fixture_passes_a_clean_test() -> None:
    _drive_guard(None, leak=False)


def test_guard_fixture_exempts_browser_tests() -> None:
    _drive_guard("browser", leak=True)

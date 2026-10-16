"""Tests for the enforcing live-state fence (proposal 138)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests import _live_state_fence as fence_mod
from tests import conftest
from tests._live_state_fence import (
    LiveStateFence,
    Violation,
    classify_audit_event,
    format_test_failure,
    format_violation_report,
    install_fence,
    merge_worker_violations,
    resolve_live_state_roots,
    serialise_violations,
    session_failed,
    violations_for,
)

ROOTS = ("/x/data",)


def _roots_for(mem_fs: dict[str, str], files: dict[str, str]) -> tuple[str, ...]:
    mem_fs.update(files)
    tomls = [Path(p) for p in files]
    with (
        patch.object(Path, "glob", return_value=iter(tomls)),
        patch.object(Path, "home", return_value=Path("/home/u")),
        patch("os.path.realpath", side_effect=os.path.abspath),
    ):
        return resolve_live_state_roots(Path("/repo"))


def test_roots_relative_memory_dir(mem_fs: dict[str, str]) -> None:
    roots = _roots_for(mem_fs, {"/repo/maestro.toml": '[paths]\nmemory_dir = "data"\n'})
    assert roots == ("/repo/data", "/home/u/.maestro")


def test_roots_absolute_override(mem_fs: dict[str, str]) -> None:
    roots = _roots_for(mem_fs, {"/repo/maestro.prod.toml": '[paths]\nmemory_dir = "/srv/state"\n'})
    assert roots == ("/srv/state", "/home/u/.maestro")


def test_roots_overlay_without_paths(mem_fs: dict[str, str]) -> None:
    roots = _roots_for(mem_fs, {"/repo/maestro.dev.toml": "[loop]\ntask_every = 1\n"})
    assert roots == ("/home/u/.maestro",)


def test_roots_include_realpath_form(mem_fs: dict[str, str]) -> None:
    mem_fs["/repo/maestro.toml"] = '[paths]\nmemory_dir = "data"\n'
    with (
        patch.object(Path, "glob", return_value=iter([Path("/repo/maestro.toml")])),
        patch.object(Path, "home", return_value=Path("/home/u")),
        patch("os.path.realpath", side_effect=lambda p: f"/real{os.path.abspath(p)}"),
    ):
        roots = resolve_live_state_roots(Path("/repo"))
    assert roots == ("/repo/data", "/real/repo/data", "/home/u/.maestro", "/real/home/u/.maestro")


def test_open_read_is_ignored() -> None:
    assert classify_audit_event("open", ("/x/data/a", "r", os.O_RDONLY), ROOTS) is None


def test_open_append_is_flagged() -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    assert classify_audit_event("open", ("/x/data/a", "a", flags), ROOTS) == "/x/data/a"


def test_os_open_write_is_flagged() -> None:
    flags = os.O_WRONLY | os.O_CREAT
    assert classify_audit_event("open", ("/x/data/a", None, flags), ROOTS) == "/x/data/a"


def test_os_open_read_is_ignored() -> None:
    assert classify_audit_event("open", ("/x/data/a", None, os.O_RDONLY), ROOTS) is None


def test_open_non_int_flags_ignored() -> None:
    assert classify_audit_event("open", ("/x/data/a", "w", None), ROOTS) is None


def test_single_path_events() -> None:
    for event in ("os.mkdir", "os.rmdir", "os.remove", "os.truncate", "shutil.rmtree"):
        assert classify_audit_event(event, ("/x/data/d", 0o777, -1), ROOTS) == "/x/data/d"


def test_rename_checks_both_sides() -> None:
    assert classify_audit_event("os.rename", ("/x/data/a", "/tmp/b", -1, -1), ROOTS) == "/x/data/a"
    assert classify_audit_event("os.rename", ("/tmp/a", "/x/data/b", -1, -1), ROOTS) == "/x/data/b"
    assert classify_audit_event("os.rename", ("/tmp/a", "/tmp/b", -1, -1), ROOTS) is None


def test_link_events_check_destination() -> None:
    for event in ("os.link", "os.symlink"):
        assert classify_audit_event(event, ("/tmp/a", "/x/data/l", -1), ROOTS) == "/x/data/l"


def test_link_source_under_root_is_not_a_write() -> None:
    for event in ("os.link", "os.symlink"):
        assert classify_audit_event(event, ("/x/data/src", "/tmp/l", -1), ROOTS) is None


def test_sqlite_connect() -> None:
    assert classify_audit_event("sqlite3.connect", ("/x/data/state.db",), ROOTS) == "/x/data/state.db"
    assert classify_audit_event("sqlite3.connect", (":memory:",), ROOTS) is None
    assert classify_audit_event("sqlite3.connect", ("",), ROOTS) is None


def test_fd_and_outside_and_sibling_prefix() -> None:
    flags = os.O_WRONLY
    assert classify_audit_event("open", (3, None, flags), ROOTS) is None
    assert classify_audit_event("open", ("/y/other", "w", flags), ROOTS) is None
    assert classify_audit_event("open", ("/x/data2/a", "w", flags), ROOTS) is None
    assert classify_audit_event("open", (b"/x/data", "w", flags), ROOTS) == "/x/data"


def test_unwatched_event_ignored() -> None:
    assert classify_audit_event("os.listdir", ("/x/data",), ROOTS) is None


def test_armed_fence_records_violation() -> None:
    fence = LiveStateFence(roots=ROOTS, armed=True, current_nodeid="t::a")
    fence.on_audit("os.remove", ("/x/data/f", -1))
    assert len(fence.violations) == 1
    violation = fence.violations[0]
    assert violation.nodeid == "t::a"
    assert violation.path == "/x/data/f"
    assert violation.event == "os.remove"
    assert len(violation.stack) > 0


def test_repeat_hits_fold_into_one_row() -> None:
    fence = LiveStateFence(roots=ROOTS, armed=True, current_nodeid="t::a")
    for _ in range(3):
        fence.on_audit("os.remove", ("/x/data/f", -1))
    fence.on_audit("os.remove", ("/x/data/g", -1))
    assert len(fence.violations) == 2
    assert fence.violations[0].count == 3
    assert fence.violations[1].count == 1


def test_merge_folds_across_workers() -> None:
    row = Violation("open", "/x/data/a", "t::a", ("f.py:fn",), 2)
    controller = LiveStateFence(roots=ROOTS)
    for _ in range(2):
        merge_worker_violations(
            controller, {"live_state_violations": serialise_violations([row])},
        )
    assert controller.violations == [Violation("open", "/x/data/a", "t::a", ("f.py:fn",), 4)]


def test_unarmed_fence_records_nothing() -> None:
    fence = LiveStateFence(roots=ROOTS)
    fence.on_audit("os.remove", ("/x/data/f", -1))
    assert fence.violations == []


def test_unmatched_event_records_nothing() -> None:
    fence = LiveStateFence(roots=ROOTS, armed=True)
    fence.on_audit("os.remove", ("/elsewhere", -1))
    assert fence.violations == []


def test_nodeid_none_outside_a_test() -> None:
    fence = LiveStateFence(roots=ROOTS, armed=True)
    fence.on_audit("os.mkdir", ("/x/data/d", 0o777, -1))
    assert fence.violations[0].nodeid is None


def test_reentrant_event_is_noop() -> None:
    fence = LiveStateFence(roots=ROOTS, armed=True)
    fence._recording = True
    fence.on_audit("os.mkdir", ("/x/data/d", 0o777, -1))
    assert fence.violations == []


def test_install_fence_once_only() -> None:
    first = LiveStateFence(roots=ROOTS)
    second = LiveStateFence(roots=ROOTS)
    with (
        patch.object(fence_mod, "_installed", []),
        patch("sys.addaudithook") as addaudithook,
    ):
        assert install_fence(first) is True
        assert install_fence(second) is False
    addaudithook.assert_called_once_with(first.on_audit)


def test_relay_round_trip() -> None:
    violations = [Violation("open", "/x/data/a", "t::a", ("f.py:fn",))]
    controller = LiveStateFence(roots=ROOTS)
    merge_worker_violations(
        controller, {"live_state_violations": serialise_violations(violations)},
    )
    assert controller.violations == violations


def test_merge_without_relay_key() -> None:
    controller = LiveStateFence(roots=ROOTS)
    merge_worker_violations(controller, {})
    assert controller.violations == []


def test_report_empty() -> None:
    assert format_violation_report([]) == []


def test_report_groups_by_nodeid_and_path() -> None:
    violations = [
        Violation("open", "/x/data/a", "t::a", ("f.py:first",)),
        Violation("open", "/x/data/a", "t::a", ("f.py:second",), 4),
        Violation("os.remove", "/x/data/b", None, ()),
    ]
    assert format_violation_report(violations) == [
        "t::a: /x/data/a (5x open)",
        "    f.py:first",
        "<outside any test>: /x/data/b (1x os.remove)",
    ]


_WRITE = os.O_WRONLY | os.O_CREAT


def _enforcing() -> LiveStateFence:
    return LiveStateFence(roots=ROOTS, armed=True, enforcing=True, current_nodeid="t::a")


def test_enforcing_refuses_first_hit() -> None:
    fence = _enforcing()
    with pytest.raises(PermissionError, match="live-state fence: write refused"):
        fence.on_audit("open", ("/x/data/f", "w", _WRITE))
    assert len(fence.violations) == 1
    assert fence.violations[0].path == "/x/data/f"
    assert fence.violations[0].nodeid == "t::a"
    assert len(fence.violations[0].stack) > 0


def test_enforcing_refuses_folded_hit() -> None:
    fence = _enforcing()
    for _ in range(2):
        with pytest.raises(PermissionError):
            fence.on_audit("open", ("/x/data/f", "w", _WRITE))
    assert len(fence.violations) == 1
    assert fence.violations[0].count == 2


def test_enforcing_refuses_two_path_event() -> None:
    fence = _enforcing()
    with pytest.raises(PermissionError):
        fence.on_audit("os.rename", ("/tmp/a", "/x/data/b", -1, -1))
    assert fence.violations[0].path == "/x/data/b"


def test_enforcing_passes_non_matching_events() -> None:
    fence = _enforcing()
    fence.on_audit("open", ("/x/data/f", "r", os.O_RDONLY))
    fence.on_audit("open", ("/y/other", "w", _WRITE))
    fence.armed = False
    fence.on_audit("open", ("/x/data/f", "w", _WRITE))
    assert fence.violations == []


def test_report_mode_records_without_raising() -> None:
    fence = LiveStateFence(roots=ROOTS, armed=True, current_nodeid="t::a")
    fence.on_audit("open", ("/x/data/f", "w", _WRITE))
    assert len(fence.violations) == 1


def test_violations_for_filters_by_nodeid() -> None:
    fence = LiveStateFence(roots=ROOTS)
    mine = Violation("open", "/x/data/a", "t::a", ())
    fence.record(mine)
    fence.record(Violation("open", "/x/data/a", "t::b", ()))
    assert violations_for(fence, "t::a") == [mine]


def test_format_test_failure_names_path_and_stack() -> None:
    message = format_test_failure([Violation("open", "/x/data/a", "t::a", ("f.py:fn",))])
    assert "/x/data/a" in message
    assert "f.py:fn" in message
    assert "write refused" in message


def test_session_failed() -> None:
    assert session_failed([]) is False
    assert session_failed([Violation("open", "/x/data/a", None, ())]) is True


def _session(fence: LiveStateFence, **config: object) -> SimpleNamespace:
    stash = {conftest._LIVE_STATE_FENCE: fence}
    return SimpleNamespace(
        config=SimpleNamespace(stash=stash, **config), exitstatus=pytest.ExitCode.OK,
    )


def test_sessionfinish_fails_on_outside_test_write() -> None:
    fence = LiveStateFence(roots=ROOTS)
    fence.record(Violation("open", "/x/data/a", None, ()))
    session = _session(fence)
    conftest.pytest_sessionfinish(session)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED


def test_sessionfinish_keeps_non_ok_status() -> None:
    fence = LiveStateFence(roots=ROOTS)
    fence.record(Violation("open", "/x/data/a", None, ()))
    session = _session(fence)
    session.exitstatus = pytest.ExitCode.INTERRUPTED
    conftest.pytest_sessionfinish(session)
    assert session.exitstatus == pytest.ExitCode.INTERRUPTED


def test_sessionfinish_clean_leaves_ok() -> None:
    session = _session(LiveStateFence(roots=ROOTS))
    conftest.pytest_sessionfinish(session)
    assert session.exitstatus == pytest.ExitCode.OK


def test_sessionfinish_worker_relays_only() -> None:
    fence = LiveStateFence(roots=ROOTS)
    fence.record(Violation("open", "/x/data/a", None, ()))
    workeroutput: dict[str, object] = {}
    session = _session(fence, workeroutput=workeroutput)
    conftest.pytest_sessionfinish(session)
    assert session.exitstatus == pytest.ExitCode.OK
    assert workeroutput["live_state_violations"] == serialise_violations(fence.violations)


def _drive_verdict(fence: LiveStateFence) -> None:
    request = SimpleNamespace(
        config=SimpleNamespace(stash={conftest._LIVE_STATE_FENCE: fence}),
        node=SimpleNamespace(nodeid="t::a"),
    )
    verdict = conftest._live_state_verdict.__wrapped__(request)
    next(verdict)
    with pytest.raises(StopIteration):
        next(verdict)


def test_verdict_fails_test_with_violation() -> None:
    fence = LiveStateFence(roots=ROOTS)
    fence.record(Violation("open", "/x/data/a", "t::a", ("f.py:fn",)))
    with pytest.raises(pytest.fail.Exception, match="/x/data/a"):
        _drive_verdict(fence)


def test_verdict_passes_clean_test() -> None:
    fence = LiveStateFence(roots=ROOTS)
    fence.record(Violation("open", "/x/data/a", "t::other", ()))
    _drive_verdict(fence)

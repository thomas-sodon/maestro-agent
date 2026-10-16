"""Per-test file-descriptor leak guard (issue #4213).

A leaked descriptor per test is invisible on a host with a large ``nofile``
limit, but ~9k tests on one xdist worker exhaust a container's default soft
limit of 1024, after which unrelated late tests fail with ``EMFILE``. The
autouse fixture in ``tests/conftest.py`` counts open descriptors around each
test through these helpers and fails the test that grew the count.
"""

from __future__ import annotations

import os

FD_DIR = "/proc/self/fd"


def open_fd_count(fd_dir: str = FD_DIR) -> int | None:
    """Return the number of open descriptors, or ``None`` where it is unmeasurable."""
    if not os.path.isdir(fd_dir):
        return None
    return len(os.listdir(fd_dir))


def fd_leak_message(before: int | None, after: int | None) -> str | None:
    """Return a failure message when the count grew across a test, else ``None``."""
    if before is None or after is None or after <= before:
        return None
    return f"file-descriptor leak: {after - before} descriptor(s) left open by this test ({before} -> {after})"

"""Tests for the _block_external_commands fixture in conftest.py."""

from __future__ import annotations

import subprocess
import tempfile

import pytest

# =====================================================================
# Thread #1: Unguarded subprocess functions (Popen/call/check_call/check_output)
# =====================================================================


class TestGuardBlocksGh:
    """All subprocess entry points must block bare gh calls."""

    def test_via_popen(self) -> None:
        with pytest.raises(RuntimeError, match="gh without mocking"):
            subprocess.Popen(["gh", "pr", "list"])

    def test_via_call(self) -> None:
        with pytest.raises(RuntimeError, match="gh without mocking"):
            subprocess.call(["gh", "pr", "list"])

    def test_via_check_call(self) -> None:
        with pytest.raises(RuntimeError, match="gh without mocking"):
            subprocess.check_call(["gh", "pr", "list"])

    def test_via_check_output(self) -> None:
        with pytest.raises(RuntimeError, match="gh without mocking"):
            subprocess.check_output(["gh", "pr", "list"])


class TestGuardBlocksGitOutsideTmp:
    """All subprocess entry points must block git calls outside tmp dirs."""

    def test_via_popen(self) -> None:
        with pytest.raises(RuntimeError, match="git outside tmp dir"):
            subprocess.Popen(["git", "status"], cwd="/home/user/repo")

    def test_via_call(self) -> None:
        with pytest.raises(RuntimeError, match="git outside tmp dir"):
            subprocess.call(["git", "status"], cwd="/home/user/repo")

    def test_via_check_call(self) -> None:
        with pytest.raises(RuntimeError, match="git outside tmp dir"):
            subprocess.check_call(["git", "status"], cwd="/home/user/repo")

    def test_via_check_output(self) -> None:
        with pytest.raises(RuntimeError, match="git outside tmp dir"):
            subprocess.check_output(["git", "status"], cwd="/home/user/repo")


# =====================================================================
# Thread #2: Hardcoded /tmp prefixes miss tempfile.gettempdir()
# =====================================================================


class TestGuardAllowsTempdir:
    """git calls under the system temp directory must be allowed."""

    def test_allows_git_in_tmp_path(self, tmp_path: object) -> None:
        """tmp_path on macOS resolves to /var/folders/... — guard must allow it."""
        try:
            subprocess.run(
                ["git", "status"], cwd=str(tmp_path), capture_output=True,
            )
        except RuntimeError:
            pytest.fail(
                f"Guard rejected git in tmp_path ({tmp_path}). "
                f"tempfile.gettempdir() = {tempfile.gettempdir()}"
            )

    def test_allows_git_in_realpath_tempdir(self) -> None:
        """git in os.path.realpath(tempfile.gettempdir()) must be allowed."""
        import os

        real_tmp = os.path.realpath(tempfile.gettempdir())
        fake_cwd = real_tmp + "/pytest-123/test_xyz"
        try:
            subprocess.run(
                ["git", "status"], cwd=fake_cwd, capture_output=True,
            )
        except RuntimeError:
            pytest.fail(
                f"Guard rejected git in realpath tempdir ({fake_cwd}). "
                f"Expected allowed prefix: {real_tmp}"
            )
        except (FileNotFoundError, OSError):
            pass  # Expected — directory doesn't exist, but guard didn't block

"""Tests for scripts/docker-entrypoint.sh install-gate behaviour.

The key invariant: the ``pip install -e /workspace[dev,email]`` dance (which includes
``rm -rf /workspace/maestro.egg-info``) must be gated on ``[ -w /workspace ]``,
because ``docker-compose.yml`` mounts ``/workspace`` read-only on the dashboard
container and the rm fails with EROFS there. On the read-only path the
entrypoint must fall back to the baked-in image install + ``PYTHONPATH=/workspace``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_ENTRYPOINT = Path(__file__).resolve().parents[1] / "scripts" / "docker-entrypoint.sh"


def _script_text() -> str:
    return _ENTRYPOINT.read_text()


class TestEntrypointStructure:
    """Structural checks on the entrypoint script itself."""

    def test_entrypoint_file_exists(self) -> None:
        assert _ENTRYPOINT.is_file(), f"expected {_ENTRYPOINT} to exist"

    def test_pip_install_is_gated_on_workspace_writable(self) -> None:
        """``pip install -e /workspace[dev,email]`` must live inside ``[ -w /workspace ]``.

        Otherwise the dashboard container (``.:/workspace:ro`` in
        docker-compose.yml) crashes on EROFS during install.
        """
        text = _script_text()
        lines = text.splitlines()

        install_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if not line.lstrip().startswith("#")
                and 'pip install --quiet --no-cache-dir -e "/workspace[dev,email]"' in line
            ),
            None,
        )
        assert install_idx is not None, (
            "expected the editable pip install line in docker-entrypoint.sh"
        )

        # Walk backwards through enclosing `if` blocks until we find the
        # writability gate, stopping at `fi` that would close it.
        depth = 0
        found_gate = False
        for line in reversed(lines[:install_idx]):
            stripped = line.strip()
            if re.match(r"fi\b", stripped):
                depth += 1
            elif stripped.startswith("if ") or stripped.startswith("elif "):
                if depth == 0 and "[ -w /workspace ]" in stripped:
                    found_gate = True
                    break
                if depth > 0:
                    depth -= 1

        assert found_gate, (
            "pip install -e /workspace[dev,email] must be nested inside "
            "`if [ -w /workspace ]; then ...` so the dashboard's read-only "
            "bind mount skips the editable reinstall instead of crashing on "
            "EROFS. See the dashboard service's `.:/workspace:ro` mount in "
            "docker-compose.yml."
        )

    def test_rm_egg_info_is_gated_on_workspace_writable(self) -> None:
        """``rm -rf /workspace/maestro.egg-info`` must also live inside the gate.

        On a read-only bind mount the rm fails with
        ``cannot remove '/workspace/maestro.egg-info/...': Read-only file system``.
        """
        text = _script_text()
        lines = text.splitlines()

        rm_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if not line.lstrip().startswith("#")
                and "rm -rf /workspace/maestro.egg-info" in line
                and "maestro_review.egg-info" in line
            ),
            None,
        )
        assert rm_idx is not None, (
            "expected the stale egg-info cleanup line in docker-entrypoint.sh"
        )

        depth = 0
        found_gate = False
        for line in reversed(lines[:rm_idx]):
            stripped = line.strip()
            if re.match(r"fi\b", stripped):
                depth += 1
            elif stripped.startswith("if ") or stripped.startswith("elif "):
                if depth == 0 and "[ -w /workspace ]" in stripped:
                    found_gate = True
                    break
                if depth > 0:
                    depth -= 1

        assert found_gate, (
            "rm -rf /workspace/maestro.egg-info must be nested inside "
            "`if [ -w /workspace ]; then ...`"
        )

    def test_pip_uninstall_is_gated_on_workspace_writable(self) -> None:
        """``pip uninstall -y maestro`` must live inside the writable branch.

        On the read-only path we rely on the baked-in image install for the
        ``maestro`` console script; uninstalling it there would leave the
        container with no maestro at all.
        """
        text = _script_text()
        lines = text.splitlines()

        uninstall_idx = next(
            (
                i
                for i, line in enumerate(lines)
                # Skip comment lines that mention the command in prose.
                if not line.lstrip().startswith("#")
                and "pip uninstall -y maestro" in line
            ),
            None,
        )
        assert uninstall_idx is not None, (
            "expected the pip uninstall line in docker-entrypoint.sh"
        )

        depth = 0
        found_gate = False
        for line in reversed(lines[:uninstall_idx]):
            stripped = line.strip()
            if re.match(r"fi\b", stripped):
                depth += 1
            elif stripped.startswith("if ") or stripped.startswith("elif "):
                if depth == 0 and "[ -w /workspace ]" in stripped:
                    found_gate = True
                    break
                if depth > 0:
                    depth -= 1

        assert found_gate, (
            "pip uninstall -y maestro must be nested inside "
            "`if [ -w /workspace ]; then ...` so the read-only dashboard path "
            "keeps the baked-in maestro install (and its console script) intact."
        )

    def test_readonly_branch_logs_fallback_message(self) -> None:
        """The read-only else branch must log that it is falling back.

        Otherwise silent fallback would be hard to diagnose from
        ``docker compose logs dashboard``.
        """
        text = _script_text()
        assert "/workspace is read-only" in text, (
            "expected the read-only fallback to emit a diagnostic message"
        )

    def test_sanity_check_asserts_workspace_prefix(self) -> None:
        """The post-install sanity check must *verify* the load path, not just print it.

        Printing ``maestro.__file__`` alone lets the container start
        successfully while running stale code from an unexpected install
        location — the only signal being a buried log line. The check must
        instead programmatically assert ``maestro.__file__`` lives under
        ``/workspace/`` so a wrong-path import exits non-zero into the
        existing ``FATAL`` guard, failing loudly and early.
        """
        text = _script_text()
        assert "maestro.__file__.startswith('/workspace/')" in text, (
            "the post-install sanity check must programmatically verify "
            "maestro.__file__ resolves under /workspace/ (not merely print it), "
            "so a wrong-location import fails the container start instead of "
            "silently running stale code"
        )
        # The failure path must raise (non-zero exit) rather than only print,
        # and must use SystemExit (not `assert`, which -O / PYTHONOPTIMIZE strips).
        # Match the code-specific message string, not the bare `raise SystemExit`
        # token: the latter also appears in the script's explanatory comment, so
        # it would pass on comment text alone even if the code branch were removed.
        assert "raise SystemExit('loaded from wrong path:" in text, (
            "the wrong-path branch must `raise SystemExit('loaded from wrong path: "
            "...')` so it survives Python running under -O / PYTHONOPTIMIZE (which "
            "strips `assert`); matching the message string guards the code branch, "
            "not the prose comment that also mentions `raise SystemExit`"
        )

    def test_entrypoint_seeds_writable_claude_json(self) -> None:
        """The entrypoint must seed a writable ``~/.claude.json`` as root.

        The Claude CLI (driven by the Agent SDK under every provider) aborts its
        "save config with lock" step with ``ENOENT: no such file or directory,
        lstat '/home/maestro/.claude.json'`` when that file does not exist. The
        root-privileged startup block must therefore create the file — guarded by
        a ``[ ! -f ]`` existence check so a persisted config is never clobbered —
        and hand it to the maestro user (UID 1002) before ``exec gosu maestro``.
        """
        text = _script_text()
        lines = text.splitlines()

        # (a) The seed targets /home/maestro/.claude.json, guarded so an existing
        #     (possibly live-session) config is never clobbered.
        assert 'CLAUDE_CONFIG="/home/maestro/.claude.json"' in text, (
            "expected the entrypoint to seed /home/maestro/.claude.json"
        )
        assert 'if [ ! -f "$CLAUDE_CONFIG" ]' in text, (
            "the .claude.json seed must be guarded by a `[ ! -f ]` existence "
            "check so it never clobbers a persisted config"
        )
        # The guarded branch creates the file by redirecting `echo '{}'` into
        # $CLAUDE_CONFIG. Assert the write target rather than the exact byte
        # sequence so the stderr-ordering (`2>/dev/null` before `>`) covered by
        # test_claude_json_seeds_redirect_stderr_before_stdout stays free to move.
        assert '> "$CLAUDE_CONFIG"' in text, (
            "the guarded branch must redirect stdout into the .claude.json file"
        )
        assert "echo '{}'" in text, (
            "the guarded branch must seed the .claude.json file with `{}`"
        )

        # (b) The seeded file is chowned to the maestro user (UID 1002).
        chown_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if not line.lstrip().startswith("#")
                and 'chown 1002:1002 "$CLAUDE_CONFIG"' in line
            ),
            None,
        )
        assert chown_idx is not None, (
            'expected `chown 1002:1002 "$CLAUDE_CONFIG"` so the maestro user '
            "owns the seeded config"
        )

        # (c) The seed lives inside the root-privileged `if [ "$(id -u)" = "0" ]`
        #     block, before `exec gosu maestro`. Walk backwards from the chown
        #     through enclosing `if` blocks until the root gate is found.
        depth = 0
        found_root_gate = False
        for line in reversed(lines[:chown_idx]):
            stripped = line.strip()
            if re.match(r"fi\b", stripped):
                depth += 1
            elif stripped.startswith("if ") or stripped.startswith("elif "):
                if depth == 0 and '[ "$(id -u)" = "0" ]' in stripped:
                    found_root_gate = True
                    break
                if depth > 0:
                    depth -= 1

        assert found_root_gate, (
            "the .claude.json seed must be nested inside the root-privileged "
            '`if [ "$(id -u)" = "0" ]; then ...` block so it runs before the '
            "gosu privilege drop."
        )

        gosu_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if not line.lstrip().startswith("#") and "exec gosu maestro" in line
            ),
            None,
        )
        assert gosu_idx is not None, (
            "expected `exec gosu maestro` to close the root-privileged block"
        )
        assert chown_idx < gosu_idx, (
            "the .claude.json seed must run before `exec gosu maestro` drops "
            "privileges to the maestro user"
        )

    def test_claude_json_root_seed_is_best_effort(self) -> None:
        """The root-block ``.claude.json`` seed must not abort startup on failure.

        The script runs under ``set -e``, so a bare ``echo '{}' > "$CLAUDE_CONFIG"``
        would propagate a write failure (edge-case read-only ``/home`` mount, full
        disk) and abort the whole container startup — turning the cosmetic runtime
        ENOENT the seed is meant to mitigate into a hard boot-blocking failure. The
        seed must be best-effort (``2>/dev/null || true``), matching the sibling
        ``chown``, the non-root fallback seed, and the "never block startup"
        invariant in ``docs/deployment.md``.
        """
        text = _script_text()
        lines = text.splitlines()

        # The root-block seed writes to "$CLAUDE_CONFIG" (the non-root fallback uses
        # the literal /home/maestro/.claude.json path, and the sibling chown has no
        # `echo '{}'`), so keying on the variable + the echo uniquely identifies the
        # root-block seed line regardless of the `2>/dev/null` / `>` ordering.
        seed_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if not line.lstrip().startswith("#")
                and "echo '{}'" in line
                and '"$CLAUDE_CONFIG"' in line
            ),
            None,
        )
        assert seed_idx is not None, (
            "expected the root-block `.claude.json` seed line "
            "`echo '{}' > \"$CLAUDE_CONFIG\"`"
        )

        seed_line = lines[seed_idx]
        assert "|| true" in seed_line, (
            "the root-block `.claude.json` seed must be best-effort (`|| true`) so "
            "a write failure under `set -e` cannot abort container startup"
        )
        assert "2>/dev/null" in seed_line, (
            "the root-block `.claude.json` seed must suppress stderr "
            "(`2>/dev/null`) so a write failure does not leak a cryptic redirection "
            "error at startup"
        )

    def test_claude_json_seeds_redirect_stderr_before_stdout(self) -> None:
        """Both ``.claude.json`` seeds must place ``2>/dev/null`` before the ``>`` open.

        POSIX shells (the image runs dash via ``#!/bin/sh``) apply redirections
        left-to-right. With ``echo '{}' > <file> 2>/dev/null`` the stdout open is
        attempted *first*; if it fails (read-only ``/home``, missing
        ``/home/maestro``, full inode table) the shell writes its
        ``cannot create ...`` diagnostic to fd 2 while fd 2 is still the terminal
        — the trailing ``2>/dev/null`` never suppresses it, so the error leaks in
        exactly the failure case the seed's ``2>/dev/null`` targets. Ordering
        ``2>/dev/null`` before the ``>`` redirect points fd 2 at ``/dev/null``
        before the open, suppressing both the open failure and any mid-write
        failure. ``|| true`` still guards the ``set -e`` abort in either order, so
        this is the stderr-suppression half of best-effort, not the abort half.
        """
        text = _script_text()
        seeds = [
            line
            for line in text.splitlines()
            if not line.lstrip().startswith("#") and "echo '{}'" in line
        ]
        # The root-block seed (writes "$CLAUDE_CONFIG") and the non-root fallback
        # seed (writes the literal /home/maestro/.claude.json) — exactly two.
        assert len(seeds) == 2, (
            "expected exactly two `.claude.json` seed lines (root block + "
            f"non-root fallback); found {len(seeds)}: {seeds!r}"
        )
        for line in seeds:
            stdout_target = (
                '> "$CLAUDE_CONFIG"'
                if '"$CLAUDE_CONFIG"' in line
                else "> /home/maestro/.claude.json"
            )
            assert stdout_target in line, (
                f"seed line must redirect stdout to the config file: {line!r}"
            )
            assert "2>/dev/null" in line, (
                f"seed line must suppress stderr with `2>/dev/null`: {line!r}"
            )
            assert line.index("2>/dev/null") < line.index(stdout_target), (
                "the stderr redirect `2>/dev/null` must precede the `>` file "
                "redirect: POSIX shells open redirections left-to-right, so a "
                "`2>/dev/null` placed *after* `>` never suppresses the "
                "`cannot create ...` diagnostic from a failed open — fd 2 is "
                f"redirected too late: {line!r}"
            )

    def test_nonroot_fallback_seed_hardcodes_maestro_home(self) -> None:
        """The non-root fallback seed must hard-code /home/maestro/.claude.json.

        gosu does not rewrite ``HOME`` on the privilege drop, and the image sets
        no ``USER`` / ``ENV HOME``, so ``$HOME`` is ``/root`` (or empty) even in
        the fallback's target scenario (container started directly as the maestro
        user via a compose ``USER`` override). The Claude CLI still resolves its
        config to ``/home/maestro/.claude.json``, so a ``"$HOME/.claude.json"``
        seed would write the wrong, unwritable file and fail to mitigate the very
        ENOENT it targets. The fallback must therefore hard-code the path, exactly
        as the root-block seed and the sibling ``.gitconfig`` copy do.
        """
        text = _script_text()
        lines = text.splitlines()

        # The fallback seed lives *after* the root-privileged block (after
        # `exec gosu maestro`), so find it below that line and confirm it targets
        # the hard-coded path rather than $HOME.
        gosu_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if not line.lstrip().startswith("#") and "exec gosu maestro" in line
            ),
            None,
        )
        assert gosu_idx is not None, "expected `exec gosu maestro` in the entrypoint"

        fallback_seed_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if i > gosu_idx
                and not line.lstrip().startswith("#")
                and ".claude.json" in line
                and "echo '{}'" in line
            ),
            None,
        )
        assert fallback_seed_idx is not None, (
            "expected a non-root fallback `.claude.json` seed after the "
            "`exec gosu maestro` privilege-drop line"
        )

        fallback_line = lines[fallback_seed_idx]
        assert "/home/maestro/.claude.json" in fallback_line, (
            "the non-root fallback seed must hard-code /home/maestro/.claude.json "
            "(as the root block and the .gitconfig copy do), because $HOME is not "
            "reliably /home/maestro after the gosu privilege drop"
        )
        assert "$HOME" not in fallback_line, (
            'the non-root fallback seed must not use "$HOME/.claude.json" — gosu '
            "does not rewrite HOME and the image sets no USER/ENV HOME, so $HOME "
            "seeds the wrong, unwritable file and fails to mitigate the ENOENT"
        )

    def test_nonroot_fallback_seed_guarded_against_clobber(self) -> None:
        """The non-root fallback seed must keep its ``[ ! -f ]`` clobber guard.

        The root block ends with ``exec gosu maestro "$0" "$@"``, which
        re-executes the entrypoint as the maestro user — so the non-root
        fallback seed runs on *every* normal startup, not just the compose
        ``USER``-override edge case. Its ``[ ! -f /home/maestro/.claude.json ]``
        existence guard is therefore load-bearing: drop it and
        ``echo '{}' > /home/maestro/.claude.json`` would overwrite the persisted
        config with ``{}`` on every container restart, wiping the auth/session
        this PR exists to preserve. The hard-code test above only checks the
        path, and the root-block tests key on the variable-form guard
        (``[ ! -f "$CLAUDE_CONFIG" ]``), so without this assertion a regression
        dropping the fallback guard would pass every other test.
        """
        text = _script_text()
        lines = text.splitlines()

        gosu_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if not line.lstrip().startswith("#") and "exec gosu maestro" in line
            ),
            None,
        )
        assert gosu_idx is not None, "expected `exec gosu maestro` in the entrypoint"

        fallback_seed_idx = next(
            (
                i
                for i, line in enumerate(lines)
                if i > gosu_idx
                and not line.lstrip().startswith("#")
                and ".claude.json" in line
                and "echo '{}'" in line
            ),
            None,
        )
        assert fallback_seed_idx is not None, (
            "expected a non-root fallback `.claude.json` seed after the "
            "`exec gosu maestro` privilege-drop line"
        )

        fallback_line = lines[fallback_seed_idx]
        assert "[ ! -f /home/maestro/.claude.json ]" in fallback_line, (
            "the non-root fallback `.claude.json` seed must keep its "
            "`[ ! -f /home/maestro/.claude.json ]` existence guard: the gosu "
            "re-exec runs this line on every normal startup, so without the "
            "guard `echo '{}' > /home/maestro/.claude.json` would clobber the "
            "persisted config with `{}` on every container restart."
        )


class TestWritabilityCheckSemantics:
    """``[ -w PATH ]`` must correctly detect unwritable directories.

    We can't reproduce a real read-only bind mount in a unit test (it needs
    real mount privileges), but ``access(2)`` returns the same result — no
    write permission — for both ``chmod 555`` and ``EROFS``, so a chmod-based
    fixture validates the shell idiom that the entrypoint depends on.
    """

    def test_writable_directory_passes_minus_w(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "pyproject.toml").write_text("[project]\n")

        result = subprocess.run(
            [
                "sh",
                "-c",
                f'if [ -w "{workspace}" ]; then echo writable; else echo readonly; fi',
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "writable"

    def test_chmod_555_directory_fails_minus_w(self, tmp_path: Path) -> None:
        """``chmod 555`` — the closest single-user-namespace analogue of a
        read-only bind mount — must make ``[ -w ]`` return false."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "pyproject.toml").write_text("[project]\n")
        # Pre-create an egg-info dir so the gate decision isn't just about
        # the pyproject.toml line — it must also cover stale egg-info cleanup.
        egg_info = workspace / "maestro.egg-info"
        egg_info.mkdir()
        (egg_info / "PKG-INFO").write_text("Name: maestro\n")

        # Remove write permission from the workspace directory itself.
        if os.geteuid() == 0:
            pytest.skip(
                "running as root — kernel ignores mode bits for access() "
                "checks, so chmod 555 cannot simulate EROFS under root"
            )
        workspace.chmod(0o555)
        try:
            result = subprocess.run(
                [
                    "sh",
                    "-c",
                    f'if [ -w "{workspace}" ]; then echo writable; else echo readonly; fi',
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            assert result.stdout.strip() == "readonly"
        finally:
            workspace.chmod(0o755)

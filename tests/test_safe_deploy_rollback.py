"""Tests for scripts/safe-deploy.sh rollback behaviour.

The key invariant: after a rollback, HEAD must point at the old commit so that
subsequent deploys correctly detect dependency changes via
``git diff ROLLBACK_HEAD HEAD``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Return a temporary git repository with two commits (v1 and v2)."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    run("git", "init", "-q")
    run("git", "config", "user.email", "test@test.com")
    run("git", "config", "user.name", "Test")
    run("git", "config", "commit.gpgsign", "false")

    (repo / "app.py").write_text("v1\n")
    run("git", "add", ".")
    run("git", "commit", "-qm", "v1")

    (repo / "app.py").write_text("v2\n")
    run("git", "add", ".")
    run("git", "commit", "-qm", "v2")

    return repo


def _head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


class TestRollbackMovesHead:
    """``git reset --hard`` must move HEAD; ``git checkout -- .`` must NOT be used."""

    def test_git_reset_hard_moves_head_to_old_commit(self, git_repo: Path) -> None:
        """After ``git reset --hard OLD``, HEAD equals OLD."""
        new_head = _head(git_repo)

        # Remember the parent commit (v1)
        old_head = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Simulate safe-deploy.sh rollback (the fixed version)
        _run(git_repo, "git", "reset", "--hard", old_head)

        assert _head(git_repo) == old_head, (
            "HEAD must point at the old commit after rollback"
        )
        assert _head(git_repo) != new_head, (
            "HEAD must NOT remain at the new commit after rollback"
        )

    def test_git_reset_hard_restores_file_contents(self, git_repo: Path) -> None:
        """After ``git reset --hard OLD``, working-tree files match the old commit."""
        old_head = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        _run(git_repo, "git", "reset", "--hard", old_head)

        assert (git_repo / "app.py").read_text() == "v1\n"

    def test_next_deploy_diff_detects_changes_after_reset(self, git_repo: Path) -> None:
        """After rollback via reset --hard, the next deploy can detect file changes.

        This is the cascade described in the defect: if HEAD is wrong, then
        ``git diff ROLLBACK_HEAD HEAD`` compares a commit to itself → zero
        changed files → Docker rebuild is incorrectly skipped.
        """
        new_head = _head(git_repo)

        old_head = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Rollback using the fix
        _run(git_repo, "git", "reset", "--hard", old_head)

        # Simulate what the next deploy does:
        #   ROLLBACK_HEAD=$(git rev-parse HEAD)   → should be old_head now
        rollback_head_next = _head(git_repo)
        assert rollback_head_next == old_head, (
            "Next deploy's ROLLBACK_HEAD must be the old commit, not the new one"
        )

        # Simulate git pull (no-op in this test; we manually set HEAD to new_head)
        _run(git_repo, "git", "reset", "--hard", new_head)

        # git diff ROLLBACK_HEAD HEAD must show changed files
        diff_result = subprocess.run(
            ["git", "diff", rollback_head_next, "HEAD", "--name-only"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        )
        changed_files = [f for f in diff_result.stdout.strip().splitlines() if f]
        assert len(changed_files) > 0, (
            "git diff must show changed files so Docker rebuild is triggered"
        )

    def test_bug_checkout_does_not_move_head(self, git_repo: Path) -> None:
        """Demonstrate that the OLD behaviour (git checkout -- .) left HEAD unchanged.

        This is kept as documentation of the original defect; it passes
        precisely because ``git checkout OLD -- .`` does NOT move HEAD.
        """
        new_head = _head(git_repo)
        old_head = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Buggy rollback: restores files but HEAD stays at new_head
        _run(git_repo, "git", "checkout", old_head, "--", ".")

        assert _head(git_repo) == new_head, (
            "git checkout -- . must NOT move HEAD (documents the original bug)"
        )


class TestStopBeforeReset:
    """The loop must be stopped before the source tree is reset.

    Issue #1795: ``safe-deploy.sh`` reset the working tree to ``origin/main``
    before stopping the loop. While the loop was still running between the
    reset and the SIGKILL grace expiry, an in-flight Executor finished
    ``Edit``-tool writes against the freshly-reset tree, leaving uncommitted
    source on the deploy host. The next container start imported the dirty
    source and crashed with ``AttributeError`` on a half-applied call site.

    The fix is to move the ``notify-pre-deploy`` and ``docker compose stop``
    blocks above the ``git reset --hard`` so no Maestro process is alive when
    the destructive reset runs. This test asserts the source-order invariant
    statically against ``scripts/safe-deploy.sh``.
    """

    @pytest.fixture()
    def script_lines(self) -> list[str]:
        script = Path(__file__).resolve().parents[1] / "scripts" / "safe-deploy.sh"
        return script.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def _first_line(lines: list[str], needle: str) -> int:
        for index, line in enumerate(lines, start=1):
            if needle in line:
                return index
        raise AssertionError(
            f"Expected to find {needle!r} in scripts/safe-deploy.sh — "
            "either the script was renamed or the relevant block was removed"
        )

    @staticmethod
    def _first_stop_loop_line(lines: list[str]) -> int:
        # Tolerate optional ``-t <seconds>`` between ``stop`` and ``loop`` so
        # the test does not have to be re-written every time the grace window
        # is tuned. The invariant under test is ordering, not flag shape.
        pattern = re.compile(r"docker compose stop(\s+-t\s+\d+)?\s+loop")
        for index, line in enumerate(lines, start=1):
            if pattern.search(line):
                return index
        raise AssertionError(
            "Expected to find 'docker compose stop ... loop' in "
            "scripts/safe-deploy.sh — either the script was renamed or the "
            "relevant block was removed"
        )

    @staticmethod
    def _deploy_reset_line(lines: list[str]) -> int:
        # The deploy-flow reset writes the *new* code from upstream. It may
        # target ``origin/main``, the bare ``FETCH_HEAD`` ref, or — since the
        # #2814 fix — the ``"$DEPLOY_TARGET"`` SHA pinned at fetch time. The
        # rollback function's ``git reset --hard "$ROLLBACK_HEAD"`` is
        # intentionally not matched — it runs only after services are already
        # restarting from the rollback image and is not subject to the
        # in-flight-Executor race.
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("git reset --hard") and (
                "origin/main" in stripped
                or "FETCH_HEAD" in stripped
                or "DEPLOY_TARGET" in stripped
            ):
                return index
        raise AssertionError(
            "Expected a deploy-flow 'git reset --hard origin/main', "
            "'git reset --hard FETCH_HEAD', or 'git reset --hard \"$DEPLOY_TARGET\"' "
            "in scripts/safe-deploy.sh"
        )

    def test_stop_loop_precedes_reset(self, script_lines: list[str]) -> None:
        stop_line = self._first_stop_loop_line(script_lines)
        reset_line = self._deploy_reset_line(script_lines)
        assert stop_line < reset_line, (
            "docker compose stop ... loop must run before the deploy-flow "
            "git reset --hard so an in-flight Executor cannot write to a "
            "freshly-reset tree (#1795)"
        )

    def test_notify_pre_deploy_precedes_reset(self, script_lines: list[str]) -> None:
        notify_line = self._first_line(script_lines, "maestro notify-pre-deploy")
        reset_line = self._deploy_reset_line(script_lines)
        assert notify_line < reset_line, (
            "notify-pre-deploy must run before the deploy-flow git reset --hard "
            "— the notify depends on the loop being alive in the container (#1795)"
        )


class TestDeployResetPinned:
    """The deploy reset must target a SHA pinned at fetch time, not FETCH_HEAD.

    Issue #2814: ``safe-deploy.sh`` fetched ``origin/main`` into ``.git/FETCH_HEAD``,
    waited up to 30 min for the still-running loop to drain its current phase, then
    reset the working tree to the **mutable** ``FETCH_HEAD`` ref. During the stop
    window the loop issues its own ``git fetch origin …`` calls (merge-truth,
    fast-forward), each of which overwrites the single shared ``.git/FETCH_HEAD``.
    The reset therefore landed ``main`` on whatever the loop last fetched — possibly
    an unmerged PR-branch commit — diverging local ``main`` ahead of ``origin/main``
    and tripping the ``tend_evolution_prs()`` tamper check on every later cycle, so
    no PR was tended.

    The fix pins the fetched SHA into ``DEPLOY_TARGET`` immediately after the fetch
    (before the stop window opens) and resets to that SHA, making the reset
    deterministic. These tests assert the source-order and target invariants
    statically against ``scripts/safe-deploy.sh`` (pure content scan — no
    subprocess / real-FS / network), mirroring the ``TestStopBeforeReset`` style.
    """

    @pytest.fixture()
    def script_lines(self) -> list[str]:
        script = Path(__file__).resolve().parents[1] / "scripts" / "safe-deploy.sh"
        return script.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def _pin_capture_line(lines: list[str]) -> int:
        # The pin captures the fetched SHA into DEPLOY_TARGET from FETCH_HEAD,
        # while FETCH_HEAD still holds the deploy's target (before the stop).
        for index, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("DEPLOY_TARGET=") and "FETCH_HEAD" in stripped:
                return index
        raise AssertionError(
            "Expected a 'DEPLOY_TARGET=\"$(git rev-parse FETCH_HEAD)\"' pin in "
            "scripts/safe-deploy.sh — the deploy target must be captured before "
            "the stop window so a concurrent loop fetch cannot redirect the reset "
            "(#2814)"
        )

    def test_deploy_reset_is_not_bare_fetch_head(
        self, script_lines: list[str]
    ) -> None:
        # The mutable shared ref is exactly what #2814 banned: a concurrent loop
        # fetch rewrites FETCH_HEAD during the stop window, so the deploy reset
        # must not read it bare.
        reset_line = TestStopBeforeReset._deploy_reset_line(script_lines)
        stripped = script_lines[reset_line - 1].strip()
        assert stripped != "git reset --hard FETCH_HEAD", (
            "deploy-flow reset must not target the bare mutable FETCH_HEAD ref — "
            "a concurrent loop fetch can rewrite it during the stop window (#2814)"
        )
        assert "DEPLOY_TARGET" in stripped or "origin/main" in stripped, (
            "deploy-flow reset must target the pinned $DEPLOY_TARGET SHA (or "
            "origin/main), not the mutable FETCH_HEAD ref (#2814)"
        )

    def test_pin_precedes_stop(self, script_lines: list[str]) -> None:
        pin_line = self._pin_capture_line(script_lines)
        stop_line = TestStopBeforeReset._first_stop_loop_line(script_lines)
        assert pin_line < stop_line, (
            "DEPLOY_TARGET must be pinned before 'docker compose stop ... loop' — "
            "while FETCH_HEAD still holds the deploy's target, before any "
            "concurrent-fetch window opens (#2814)"
        )

    def test_pin_precedes_and_feeds_reset(self, script_lines: list[str]) -> None:
        pin_line = self._pin_capture_line(script_lines)
        reset_line = TestStopBeforeReset._deploy_reset_line(script_lines)
        assert pin_line < reset_line, (
            "DEPLOY_TARGET must be pinned before the deploy-flow reset (#2814)"
        )
        assert "DEPLOY_TARGET" in script_lines[reset_line - 1], (
            "the deploy-flow reset must reference the same pinned $DEPLOY_TARGET "
            "captured before the stop window (#2814)"
        )

    def test_reset_to_pinned_sha_survives_fetch_head_rewrite(
        self, git_repo: Path
    ) -> None:
        """Resetting to a captured SHA ignores a concurrent FETCH_HEAD rewrite.

        Simulates the #2814 race: the deploy pins its target SHA, then the live
        loop's concurrent ``git fetch`` overwrites ``.git/FETCH_HEAD`` to a
        different commit. A reset to the pinned SHA must land the captured target,
        whereas a reset to the bare ``FETCH_HEAD`` would land the loop's commit.
        """
        target = subprocess.run(
            ["git", "rev-parse", "HEAD~1"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        other = _head(git_repo)  # stands in for the loop's concurrent fetch

        # Loop overwrites the shared FETCH_HEAD ref mid-deploy.
        (git_repo / ".git" / "FETCH_HEAD").write_text(
            f"{other}\t\tbranch 'main' of origin\n", encoding="utf-8"
        )
        assert (
            subprocess.run(
                ["git", "rev-parse", "FETCH_HEAD"],
                cwd=git_repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            == other
        ), "precondition: FETCH_HEAD now points at the loop's commit"

        # The fix resets to the pinned SHA, not the rewritten FETCH_HEAD.
        _run(git_repo, "git", "reset", "--hard", target)

        assert _head(git_repo) == target, (
            "reset to the pinned deploy target must land the captured commit, "
            "not whatever a concurrent loop fetch wrote into FETCH_HEAD (#2814)"
        )
        assert _head(git_repo) != other, (
            "reset must NOT land the loop's concurrently-fetched commit (#2814)"
        )


class TestErrTrapRollback:
    """An ``ERR`` trap must make rollback the default for unhandled failures.

    Issue #174: ``safe-deploy.sh`` runs under ``set -euo pipefail`` but installs
    no ``trap ... ERR``. Any command that is not explicitly guarded with
    ``|| { fail ...; rollback; }`` trips ``set -e`` and exits immediately,
    skipping ``rollback`` — leaving the deploy dirty with services changed and
    no restore. Several unguarded command substitutions already sit on the happy
    path (``git rev-parse HEAD``, ``git rev-parse FETCH_HEAD``, ``sha256sum``).

    The fix installs ``trap 'rollback' ERR`` after the function definitions and
    the existing ``EXIT`` trap, and enables ``errtrace`` (``set -Eeuo pipefail``)
    so the trap propagates into command substitutions and subshells. This test
    asserts those invariants statically against ``scripts/safe-deploy.sh``.
    """

    @pytest.fixture()
    def script_lines(self) -> list[str]:
        script = Path(__file__).resolve().parents[1] / "scripts" / "safe-deploy.sh"
        return script.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def _first_line(lines: list[str], needle: str) -> int:
        for index, line in enumerate(lines, start=1):
            if needle in line:
                return index
        raise AssertionError(
            f"Expected to find {needle!r} in scripts/safe-deploy.sh — "
            "either the script was renamed or the relevant block was removed"
        )

    def test_err_trap_installs_rollback(self, script_lines: list[str]) -> None:
        # The trap must exist at all — this is the literal gap #174 flags.
        self._first_line(script_lines, "trap 'rollback' ERR")

    def test_err_trap_defined_after_rollback_function(
        self, script_lines: list[str]
    ) -> None:
        rollback_def_line = self._first_line(script_lines, "rollback() {")
        trap_line = self._first_line(script_lines, "trap 'rollback' ERR")
        assert rollback_def_line < trap_line, (
            "trap 'rollback' ERR must be installed after rollback() is defined "
            "so the trap references a defined function (#174)"
        )

    def test_errtrace_enabled(self, script_lines: list[str]) -> None:
        # errtrace (-E) is what propagates the ERR trap into command
        # substitutions and subshells, closing the $(...) gap that already
        # exists today. Accept either the combined ``set -Eeuo pipefail`` form
        # or a standalone ``set -o errtrace``.
        has_errtrace = any(
            "set -Eeuo pipefail" in line or "set -o errtrace" in line
            for line in script_lines
        )
        assert has_errtrace is True, (
            "scripts/safe-deploy.sh must enable errtrace (set -Eeuo pipefail or "
            "set -o errtrace) so the ERR trap propagates into command "
            "substitutions and subshells (#174)"
        )


class TestDeployGitCleanFailureLogged:
    """A deploy-flow ``git clean`` failure must be logged, not silently discarded.

    Issue #227: after the destructive ``git reset --hard`` brings the tree to the
    pinned deploy target, ``git clean -fd || true`` discards every failure with no
    log line and no exit-code propagation. If untracked files are undeletable (for
    example root-owned artifacts left in a bind mount), ``git clean`` exits
    non-zero and the deploy continues as if cleanup succeeded — leftover untracked
    files can then leak into the subsequent Docker build context. Every other
    tolerated failure in the script announces itself via ``log``; this one was
    invisible.

    The fix keeps the failure non-fatal — a guarded ``|| log ...`` so the ERR trap
    does not fire — but routes it through the ``log`` helper so an incomplete
    cleanup is visible in the deploy log. This test asserts that invariant
    statically against ``scripts/safe-deploy.sh``.
    """

    @pytest.fixture()
    def script_lines(self) -> list[str]:
        script = Path(__file__).resolve().parents[1] / "scripts" / "safe-deploy.sh"
        return script.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def _deploy_git_clean_line(lines: list[str]) -> str:
        # The deploy-flow cleanup runs ``git clean -fd`` right after the pinned
        # ``git reset --hard`` to drop untracked files from the freshly-reset
        # tree. Match on ``git clean`` so the test does not have to track the
        # exact flag shape.
        for line in lines:
            if "git clean" in line:
                return line.strip()
        raise AssertionError(
            "Expected a deploy-flow 'git clean' line in scripts/safe-deploy.sh — "
            "either the script was renamed or the cleanup block was removed"
        )

    def test_git_clean_failure_is_logged_not_discarded(
        self, script_lines: list[str]
    ) -> None:
        clean_line = self._deploy_git_clean_line(script_lines)
        assert "|| true" not in clean_line, (
            "deploy-flow 'git clean' must not discard failures with '|| true' — "
            "an undeletable untracked file would then leak into the build context "
            "with no operator-visible log line (#227)"
        )
        assert "|| log" in clean_line, (
            "deploy-flow 'git clean' failure must route through the 'log' helper "
            "(|| log ...) so an incomplete cleanup is visible in the deploy log, "
            "while staying guarded so the ERR trap does not escalate it to a full "
            "rollback (#227)"
        )


class TestHeartbeatDiagnosticReadsHost:
    """The heartbeat diagnostic must read the host bind-mount, not exec the container.

    Issue #491: when the post-recreate health check fails, ``safe-deploy.sh`` dumps
    the loop container's heartbeat file so the operator can tell whether the loop
    never wrote it, wrote it and went stale, or wrote it recently. The dump was done
    by ``docker compose exec -T loop sh -c '...'`` — but the single most common reason
    the health check fails is that the loop container itself has *exited* (for
    example ``_build_agent`` crashed on startup, so the container never reached a
    healthy state). ``docker compose exec`` then has no running container to attach
    to and errors out; the trailing ``|| true`` swallows the docker error and the
    diagnostic prints nothing useful — broken for its primary use case.

    The loop service bind-mounts the repo root at ``/workspace``
    (``docker-compose.yml``), so ``/workspace/data/loop.heartbeat`` inside the
    container is the very same file as ``./data/loop.heartbeat`` on the deploy host.
    The fix reads the heartbeat straight from the host ``./data`` bind-mount with an
    inline ``python3 -c`` (mirroring the host-side health-check invocation), so it
    reports ``exists``/``mtime``/``age_seconds`` reliably whether or not the loop
    container is still running, and routes any genuine failure through the ``log``
    helper instead of a bare ``|| true``. This test asserts that invariant
    statically against ``scripts/safe-deploy.sh`` (pure content scan — no subprocess
    / real-FS / network), mirroring the ``TestDeployGitCleanFailureLogged`` style.
    """

    @pytest.fixture()
    def script_lines(self) -> list[str]:
        script = Path(__file__).resolve().parents[1] / "scripts" / "safe-deploy.sh"
        return script.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def _heartbeat_diagnostic_block(lines: list[str]) -> list[str]:
        # The heartbeat diagnostic runs after a failed health check: it prints a
        # fixed banner, then stats the loop's heartbeat file with an inline
        # ``python3 -c`` whose ``|| ...`` failure guard closes the block. Anchor on
        # the banner — stable across #491's host-read rewrite and #492's
        # single-sourced ``${LOOP_HEARTBEAT_PATH#/workspace/}`` derivation — and
        # return the stripped block through that guard, so the matcher tracks
        # neither the exact command shape nor whether the path is a literal or a
        # variable expansion.
        start = next(
            (
                i
                for i, line in enumerate(lines)
                if "Loop heartbeat file status:" in line
            ),
            None,
        )
        if start is None:
            raise AssertionError(
                "Expected the 'Loop heartbeat file status:' diagnostic banner in "
                "scripts/safe-deploy.sh — either the script was renamed or the "
                "health-check failure diagnostic block was removed"
            )
        for end in range(start + 1, len(lines)):
            if "||" in lines[end]:
                return [line.strip() for line in lines[start : end + 1]]
        raise AssertionError(
            "Expected the heartbeat diagnostic to carry a '|| ...' failure guard "
            "after its banner, so a genuine read failure is handled instead of "
            "tripping the ERR trap (#491)"
        )

    @staticmethod
    def _heartbeat_diagnostic_guard(lines: list[str]) -> str:
        # The block's failure guard sits on its closing line (the '|| ...' route).
        block = TestHeartbeatDiagnosticReadsHost._heartbeat_diagnostic_block(lines)
        return block[-1]

    def test_heartbeat_diagnostic_reads_host_volume_not_container_exec(
        self, script_lines: list[str]
    ) -> None:
        block = self._heartbeat_diagnostic_block(script_lines)
        for line in block:
            assert "docker compose exec" not in line, (
                "heartbeat diagnostic must not dump the heartbeat via "
                "'docker compose exec' — the health check most often fails because "
                "the loop container has exited, leaving exec no running container to "
                "attach to, so the dump prints nothing useful (#491)"
            )
        joined = "\n".join(block)
        assert "data/loop.heartbeat" in joined or "#/workspace/" in joined, (
            "heartbeat diagnostic must read the host bind-mount path directly — "
            "either the literal 'data/loop.heartbeat' or the host-relative "
            "derivation '${LOOP_HEARTBEAT_PATH#/workspace/}' (#492) — so it reports "
            "exists/mtime/age_seconds reliably even when the loop container has "
            "exited (#491)"
        )

    def test_heartbeat_diagnostic_failure_routes_through_log_not_true(
        self, script_lines: list[str]
    ) -> None:
        # Regression guard for the '|| log' hardening: the bare '|| true' on the
        # old container-exec form is exactly what swallowed the broken diagnostic,
        # so a silent revert to '|| true' must fail this test rather than pass it.
        guard = self._heartbeat_diagnostic_guard(script_lines)
        assert "|| true" not in guard, (
            "heartbeat diagnostic must not swallow read failures with '|| true' — "
            "a bare '|| true' is exactly what hid the broken container-exec "
            "diagnostic; route genuine read failures through the 'log' helper so "
            "they stay visible in the deploy log (#491)"
        )
        assert "|| log" in guard, (
            "heartbeat diagnostic read failure must route through the 'log' helper "
            "(|| log ...) so it is visible in the deploy log while staying "
            "non-fatal (the rollback below still runs), mirroring the deploy-flow "
            "'git clean' guard (#491)"
        )


class TestRollbackCleansTags:
    """A successful rollback must drop the ``:rollback`` image tags.

    Issue #175: ``cleanup()`` removes the ``maestro-loop:rollback`` /
    ``maestro-dashboard:rollback`` tags, but it was only ever invoked on the
    success path. The successful-rollback path (``exit 1``) never ran it, so the
    rollback tags accumulated across every failed deploy. The fix adds a single
    ``cleanup`` call on the ``exit 1`` path — *after* the services have been
    restarted from the rollback image, so ``:latest`` already points at the
    recovered image and dropping the ``:rollback`` tag is safe. The
    failed-rollback path (``exit 2``) is deliberately left untouched: there the
    ``:rollback`` tag may be the operator's only handle on the last-good image.

    These tests assert the ordering invariant statically against
    ``scripts/safe-deploy.sh`` (pure content scan — no subprocess / real-FS /
    network), mirroring the ``TestStopBeforeReset`` style.
    """

    @pytest.fixture()
    def script_lines(self) -> list[str]:
        script = Path(__file__).resolve().parents[1] / "scripts" / "safe-deploy.sh"
        return script.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def _first_line_from(lines: list[str], needle: str, start: int) -> int:
        for index in range(start, len(lines) + 1):
            if needle in lines[index - 1]:
                return index
        raise AssertionError(
            f"Expected to find {needle!r} in scripts/safe-deploy.sh at or after "
            f"line {start} — either the script was renamed or the relevant block "
            "was removed"
        )

    @staticmethod
    def _bare_cleanup_call(lines: list[str], start: int, end: int) -> int:
        # A standalone ``cleanup`` invocation on its own line — not the
        # ``cleanup() {`` definition (has parens) and not ``smoke_snapshot_cleanup``
        # (a different identifier). Scoped to ``[start, end)`` so we only match a
        # call inside the rollback() body, never the success-path call or the def.
        for index in range(start, end):
            if lines[index - 1].strip() == "cleanup":
                return index
        raise AssertionError(
            "Expected a bare 'cleanup' call inside rollback() before 'exit 1' in "
            "scripts/safe-deploy.sh — the successful-rollback path must drop the "
            "stale ':rollback' image tags (#175)"
        )

    def test_cleanup_runs_before_exit_1_in_rollback(
        self, script_lines: list[str]
    ) -> None:
        rollback_def = self._first_line_from(script_lines, "rollback() {", 1)
        exit1_line = self._first_line_from(script_lines, "exit 1", rollback_def)
        cleanup_call = self._bare_cleanup_call(
            script_lines, rollback_def + 1, exit1_line
        )
        assert rollback_def < cleanup_call < exit1_line, (
            "rollback() must call cleanup after the restore but before 'exit 1' "
            "so the stale ':rollback' tags are dropped on a successful rollback "
            "(#175)"
        )

    def test_cleanup_runs_after_service_restart_in_rollback(
        self, script_lines: list[str]
    ) -> None:
        rollback_def = self._first_line_from(script_lines, "rollback() {", 1)
        exit1_line = self._first_line_from(script_lines, "exit 1", rollback_def)
        restart_line = self._first_line_from(
            script_lines, "docker compose up", rollback_def
        )
        cleanup_call = self._bare_cleanup_call(
            script_lines, rollback_def + 1, exit1_line
        )
        assert restart_line < cleanup_call, (
            "cleanup must run only after 'docker compose up' has restarted "
            "services from the rollback image, so dropping the ':rollback' tag "
            "cannot precede the restore (#175)"
        )

"""Track external contributions — detect commits by Thomas between loop iterations."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from maestro.fileutil import atomic_write, file_lock
from maestro.vcs import CommitInfo, SubprocessVCS

if TYPE_CHECKING:
    from maestro.journal import Journal
    from maestro.vcs import VCSClient

logger = logging.getLogger("maestro")

# Backward-compatible alias.
Commit = CommitInfo


class ContributionTracker:
    """Detects non-Maestro commits and records them as journal entries."""

    def __init__(
        self,
        workspace: Path,
        memory_dir: Path,
        git_timeout: int = 10,
        vcs: VCSClient | None = None,
    ) -> None:
        self._dir = memory_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._commit_path = self._dir / "last_known_commit.txt"
        self._own_commits_path = self._dir / "own_commits.txt"
        self._own_commits_cache: set[str] | None = None
        # In-memory cooldown: the file size at which the last compaction
        # attempt returned 0. Suppresses re-running the O(N) is_ancestor
        # sweep on every record_own_commit when nothing is droppable.
        self._compaction_attempted_at_size: int | None = None
        self._vcs = vcs or SubprocessVCS(
            workspace, timeout=git_timeout, quick_timeout=git_timeout,
        )

    # ------------------------------------------------------------------
    # Git helpers (delegate to VCSClient)
    # ------------------------------------------------------------------

    def get_current_head(self) -> str | None:
        """Return the current HEAD commit hash, or None on failure."""
        return self._vcs.get_head_hash()

    def get_new_commits(
        self, old_hash: str, new_hash: str,
    ) -> list[CommitInfo]:
        """Return commits between *old_hash* and *new_hash* (exclusive..inclusive)."""
        return self._vcs.get_commits_between(old_hash, new_hash)

    def get_diff_stat(self, old_hash: str, new_hash: str) -> str:
        """Return ``git diff --stat`` output between two commits."""
        return self._vcs.get_diff_stat(old_hash, new_hash)

    # ------------------------------------------------------------------
    # Persistent state — last known commit
    # ------------------------------------------------------------------

    def load_last_known_commit(self) -> str | None:
        """Load the previously recorded HEAD hash, or None if not yet stored."""
        try:
            text = self._commit_path.read_text(encoding="utf-8").strip()
            return text or None
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("Failed to read last known commit", exc_info=True)
            return None

    def save_last_known_commit(self, commit_hash: str) -> None:
        try:
            atomic_write(self._commit_path, commit_hash)
        except OSError:
            logger.warning("Failed to save last known commit", exc_info=True)

    # ------------------------------------------------------------------
    # Persistent state — own commits
    # ------------------------------------------------------------------

    def load_own_commits(self) -> set[str]:
        """Return the set of commit hashes Maestro has created.

        Returns a defensive copy so callers cannot corrupt the internal cache.
        """
        if self._own_commits_cache is not None:
            return set(self._own_commits_cache)
        try:
            text = self._own_commits_path.read_text(encoding="utf-8")
            self._own_commits_cache = {
                line.strip() for line in text.splitlines() if line.strip()
            }
        except FileNotFoundError:
            self._own_commits_cache = set()
        except OSError:
            logger.warning("Failed to read own commits", exc_info=True)
            self._own_commits_cache = set()
        return set(self._own_commits_cache)

    # Compact own_commits.txt once it crosses this many recorded entries.
    # Chosen as the smallest power-of-ten that bounds growth without adding a
    # config knob: at typical loop cadence this triggers compaction roughly
    # every few weeks of operation, keeping per-record cost negligible.
    _OWN_COMMITS_COMPACT_THRESHOLD = 1000

    _HEX_HASH_RE = re.compile(r"[0-9a-fA-F]{4,64}")

    @staticmethod
    def _is_valid_hex_hash(h: str) -> bool:
        """Return True if *h* looks like a valid (abbreviated or full) hex commit hash."""
        return bool(ContributionTracker._HEX_HASH_RE.fullmatch(h))

    def record_own_commit(self, commit_hash: str) -> None:
        """Append a commit hash that Maestro authored."""
        commit_hash = commit_hash.strip()
        if not commit_hash or not self._is_valid_hex_hash(commit_hash):
            logger.warning("Ignoring malformed commit hash: %r", commit_hash)
            return
        try:
            with file_lock(self._own_commits_path), open(
                self._own_commits_path, "a", encoding="utf-8",
            ) as f:
                f.write(commit_hash + "\n")
        except OSError:
            logger.warning("Failed to record own commit %s", commit_hash[:8], exc_info=True)
            return
        # Invalidate cache so next load picks up the new hash.
        if self._own_commits_cache is not None:
            self._own_commits_cache.add(commit_hash)
        self._maybe_compact_own_commits()

    def _maybe_compact_own_commits(self) -> None:
        """Trigger compaction only when the file is large enough to bother."""
        # 41 = 40-char hash + newline; exact for SHA-1, a lower bound for
        # longer hashes the regex permits (compaction then fires earlier,
        # which is the safe direction). Used as a cheap fast path that skips
        # git for almost every record.
        try:
            size = self._own_commits_path.stat().st_size
        except OSError:
            return
        threshold_bytes = self._OWN_COMMITS_COMPACT_THRESHOLD * 41
        if size < threshold_bytes:
            return
        # Cooldown: a prior attempt at this size class found nothing to drop.
        # Skip re-running the O(N) is_ancestor sweep until the file has grown
        # by at least another threshold-sized chunk past that observation.
        if (
            self._compaction_attempted_at_size is not None
            and size < self._compaction_attempted_at_size + threshold_bytes
        ):
            return
        dropped = self.compact_own_commits()
        if dropped == 0:
            self._compaction_attempted_at_size = size
        else:
            self._compaction_attempted_at_size = None

    def compact_own_commits(self) -> int:
        """Drop entries that are ancestors of last_known_commit.

        Returns the number of dropped entries.  An entry that is an ancestor
        of ``last_known_commit`` cannot resurface in any future
        ``get_commits_between(last_known_commit, current_head)`` window, so
        dropping it is a behavior-preserving operation.

        Best-effort — read or write failures log and return 0 without
        raising so compaction never blocks recording or contribution
        detection.
        """
        last_known = self.load_last_known_commit()
        if last_known is None:
            return 0

        try:
            with file_lock(self._own_commits_path):
                try:
                    text = self._own_commits_path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    return 0
                except OSError:
                    logger.warning(
                        "Failed to read own commits for compaction", exc_info=True,
                    )
                    return 0

                before: set[str] = {
                    line.strip() for line in text.splitlines() if line.strip()
                }
                if not before:
                    return 0

                kept: set[str] = set()
                for h in before:
                    if not self._vcs.is_ancestor(h, last_known):
                        kept.add(h)

                if kept == before:
                    logger.debug(
                        "own_commits compaction: no ancestors of %s — file unchanged",
                        last_known[:8],
                    )
                    return 0

                try:
                    atomic_write(
                        self._own_commits_path,
                        "\n".join(sorted(kept)) + "\n" if kept else "",
                    )
                except OSError:
                    logger.warning(
                        "Failed to rewrite own commits during compaction", exc_info=True,
                    )
                    return 0

                dropped = len(before) - len(kept)
                kept_count = len(kept)
        except OSError:
            logger.warning(
                "Failed to acquire own_commits lock for compaction", exc_info=True,
            )
            return 0

        # Force the next load_own_commits to re-read from disk.
        self._own_commits_cache = None
        logger.info(
            "Compacted own_commits.txt: dropped %d ancestor entries, kept %d",
            dropped, kept_count,
        )
        return dropped

    # ------------------------------------------------------------------
    # Main orchestration
    # ------------------------------------------------------------------

    def check_for_contributions(self, journal: Journal) -> None:
        """Detect external commits since last check and journal about them."""
        current = self.get_current_head()
        if current is None:
            return

        last = self.load_last_known_commit()
        if last is None:
            # First run — just record the baseline.
            self.save_last_known_commit(current)
            return

        if current == last:
            return

        commits = self.get_new_commits(last, current)

        if not commits:
            # Empty commit list is ambiguous — could be a VCS error or a
            # force-push/rebase with no new commits.  Do NOT advance the
            # baseline so real commits aren't lost.  The next iteration
            # will retry.
            logger.debug(
                "No commits found between %s and %s — not advancing baseline",
                last[:8], current[:8],
            )
            return

        # Filter out Maestro's own commits by hash.
        own = self.load_own_commits()
        external = [c for c in commits if c.hash not in own]

        if not external:
            # All commits are Maestro's own — safe to update the baseline.
            self.save_last_known_commit(current)
            return

        # Only fetch diff stat when ALL commits in the range are external,
        # so the stat accurately reflects collaborator changes.
        diff_stat = ""
        if len(external) == len(commits):
            try:
                diff_stat = self.get_diff_stat(last, current)
            except Exception:
                logger.warning(
                    "Failed to get diff stat for %s..%s, continuing without it",
                    last[:8], current[:8], exc_info=True,
                )

        # Build journal content.
        lines = [
            f"Detected {len(external)} new commit(s) from collaborators:",
        ]
        for c in external:
            lines.append(f"  - {c.hash[:8]} {c.subject} ({c.author})")
        if diff_stat:
            lines.append("")
            lines.append(diff_stat)

        try:
            journal.record_event(
                content="\n".join(lines),
                entry_type="contribution",
                triggered_by="contribution_tracking",
            )
        except Exception:
            # Journal write failed — do NOT update last_known_commit so
            # contributions are retried on the next run.
            logger.warning("Failed to journal contributions", exc_info=True)
            return

        # Only update the baseline AFTER successful journaling to avoid
        # losing contribution data if the journal write fails.
        self.save_last_known_commit(current)
        logger.info(
            "Recorded %d external contribution(s) since %s",
            len(external),
            last[:8],
        )


def check_merge_clean(
    branch: str, target: str = "origin/main", *, cwd: str | None = None,
) -> bool | None:
    """Return whether *branch* merges cleanly into *target*.

    Uses ``git merge-tree --write-tree`` — a real three-way merge computed
    entirely in the object store, touching neither the working tree, the
    index, nor any ref. That zero-side-effect property is what makes it
    safe to call at task completion, when the branch is freshest and
    least likely to have rotted against ``main``.

    *cwd* scopes the git invocation to the caller's configured workspace,
    mirroring every other git call in the codebase (``SubprocessVCS._run``
    passes ``cwd=self._cwd``). Without it git would run in the process's
    launch directory; whenever that differs from ``config.workspace`` — a
    supported TOML configuration — the probe would run against the wrong
    directory (or a non-repo), silently always returning ``None`` and
    no-op'ing the whole promotion feature. ``None`` (the default) preserves
    the process-cwd behaviour for callers that genuinely want it.

    Return contract:

    - exit ``0`` → the merge produced a tree with no conflicts → ``True``
    - exit ``1`` → the merge hit conflicts → ``False``
    - ``TimeoutExpired`` / ``OSError`` / ``UnicodeDecodeError`` / any other
      exit code → ``None`` (unknown — git unavailable, a missing ref, a
      malformed invocation, or undecodable output)

    ``None`` is a first-class "could not determine" verdict, never an
    error to swallow: callers treat it the same as a conflict (no
    promotion), so an indeterminate probe never fabricates a clean-merge
    claim.
    """
    try:
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", target, branch],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=cwd,
        )
    except (subprocess.TimeoutExpired, OSError, UnicodeDecodeError):
        logger.warning(
            "check_merge_clean: git merge-tree failed for %s into %s",
            branch, target, exc_info=True,
        )
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    logger.warning(
        "check_merge_clean: unexpected git merge-tree exit %d for %s into %s",
        result.returncode, branch, target,
    )
    return None

"""Version control and CI/CD platform abstractions.

Provides :class:`VCSClient` and :class:`CodeHost` protocols that decouple
core logic from specific VCS/CI tooling.  Concrete subprocess-based
implementations (:class:`SubprocessVCS`, :class:`SubprocessCodeHost`)
wrap the ``git`` and ``gh`` CLIs.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from maestro import secret_scan
from maestro.ci_diagnosis import (
    CheckRun,
    is_failing_conclusion,
    is_success_conclusion,
    rollup_item_name,
)

logger = logging.getLogger("maestro")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class VCSError(Exception):
    """Raised when a version control or CI/CD platform operation fails."""


class DirtyWorkingTreeError(VCSError):
    """Raised when a mutating phase refuses to run on an unclean working tree.

    A named subclass so callers can distinguish "the tree carries work this
    phase did not author" from every other VCS failure without matching on
    message text. Carrying unrelated edits onto a freshly cut branch folds
    them into the phase's commit (#3711); the refusal is the fix — nothing
    is discarded.
    """


# ---------------------------------------------------------------------------
# Status-check rollup classification
# ---------------------------------------------------------------------------

# Status-check names that are advisory, not CI quality gates, and must never
# block the tend-phase main-merge / conflict path. An AI code-review verdict
# (a ``review`` job running ``maestro_review`` in an operator's own workflow)
# reports a red check for review *opinions*, not a broken build, so a
# conflicting PR whose only red check is ``review`` must still reach conflict
# resolution (#2440). The ``reply-coverage`` entry names the deterministic
# reply-discipline audit (``maestro-review --require-comment-replies``), which
# by design shows red whenever a reviewer finding still lacks a reply — a
# signal, never a merge blocker — so it cannot gate the tend-phase merge path
# either. Neither workflow ships in this repository's own CI.
#
# Matching is exact-name membership (case-insensitively), never substring: a
# substring match on the advisory ``review`` token would wrongly sweep any real
# quality gate whose name merely contains it into the advisory set. The
# maestro_review lint/type/test now runs inside the ``package-quality`` job, so
# no live quality gate shares a token with ``review`` — but exact membership is
# the rule so a future ``*review*``-named gate cannot be silently muted.
ADVISORY_CHECK_NAMES: frozenset[str] = frozenset({"review", "reply-coverage"})

# Failure/success rollup vocabulary and the item-name reader live in the single
# CI-classification owner :mod:`maestro.ci_diagnosis` (imported above). Advisory
# gating stays here — it is a VCS policy, not a classification — and reads the
# item name through the relocated :func:`~maestro.ci_diagnosis.rollup_item_name`.


def _is_advisory_check(item: dict[str, Any]) -> bool:
    """Return True when *item* is an advisory (non-gating) status check."""
    return rollup_item_name(item).strip().lower() in ADVISORY_CHECK_NAMES


#: Check runs a main-ref read also drops: post-merge jobs that are not quality
#: gates (a ``deploy`` job only publishes a deployment record), so
#: their failure must not read main as red.
REF_NON_GATE_CHECK_NAMES: frozenset[str] = frozenset({"deploy"})

#: A commit SHA a ref check-run read accepts (abbreviated or full).
_REF_SHA_RE = re.compile(r"[0-9a-fA-F]{7,40}")


@dataclass(frozen=True)
class RefCheckRuns:
    """The non-advisory check-runs one commit carries (latest per check)."""

    sha: str
    runs: tuple[CheckRun, ...]


# ---------------------------------------------------------------------------
# Failing-run log reading (read_run_log_failed)
# ---------------------------------------------------------------------------

#: Hard char cap on a failing-run log excerpt returned by
#: :meth:`SubprocessCodeHost.read_run_log_failed` — bounds the text before it can
#: reach an artifact or a prompt (context-bloat cap). The excerpt keeps the log's
#: *tail*, where a failing step's concrete error summary lands (see the method).
#: Redaction runs *before* slicing so a secret straddling the cut can never leak.
_RUN_LOG_MAX_CHARS = 4000

#: The workflow run id lives in a failing rollup item's ``detailsUrl`` /
#: ``targetUrl`` as ``…/runs/<id>`` (with an optional ``/job/<id>`` suffix).
_RUN_ID_RE = re.compile(r"/runs/(\d+)")

#: A GitHub pull-request URL as ``gh pr create`` prints it on stdout. Used to
#: recognise a degraded-success ``gh pr create`` (PR created, a follow-up
#: mutation such as the ``--reviewer`` request failed, non-zero exit) (#4553).
_PR_URL_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/pull/\d+")

#: Upper bound on the name-only diff :meth:`SubprocessVCS.changed_paths`
#: returns, so a pathologically large diff cannot hand an unbounded list to the
#: proposal-124 attributor. A diff-attribution decision only needs the set of
#: files a branch touched; a diff larger than this is not a normal evolution
#: branch and the bound is the fail-safe.
_CHANGED_PATHS_MAX = 1000


def _first_failing_run_id(rollup: list[Any]) -> str | None:
    """Return the workflow run id of the first non-advisory failing rollup item.

    Walks *rollup*, skips non-dict and advisory items, and for the first item
    whose conclusion/state is a
    :func:`~maestro.ci_diagnosis.is_failing_conclusion` resolves its workflow run
    id from the ``detailsUrl`` / ``targetUrl`` via :data:`_RUN_ID_RE`. Returns
    ``None`` when no failing item carries a parseable run id — best-effort, so the
    caller degrades to ``""``. The ``detailsUrl`` → run-id parse stays local — it
    is the exempt fetch seam, not rollup classification.
    """
    for item in rollup:
        if not isinstance(item, dict) or _is_advisory_check(item):
            continue
        value = str(item.get("conclusion") or item.get("state") or "")
        if not is_failing_conclusion(value):
            continue
        url = str(item.get("detailsUrl") or item.get("targetUrl") or "")
        match = _RUN_ID_RE.search(url)
        if match:
            return match.group(1)
    return None


def _redact_run_log(text: str) -> str:
    """Substitute credential material in a CI run log with ``<redacted>``.

    A thin alias for :func:`maestro.secret_scan.redact_credentials`, which
    owns the credential regexes shared with the crash-milestone stderr head.
    Pure.
    """
    return secret_scan.redact_credentials(text)


def _is_label_related_failure(stderr: str) -> bool:
    """Return True when a ``gh issue create`` failure looks label-related.

    A case-insensitive substring match on ``"label"`` covers both observed
    phrasings — ``"could not add label: '<x>' not found"`` and
    ``"label '<x>' not found"`` — so the missing-label retry fires only for
    failures that ensuring labels could actually cure.
    """
    return "label" in stderr.lower()


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class CommitInfo:
    """A single git commit with hash, subject line, and author name."""

    hash: str
    subject: str
    author: str


@dataclass
class MergeResult:
    """Outcome of a local merge attempt.

    ``success`` is ``True`` iff the merge completed cleanly with no
    conflicts.  On conflict, ``conflicted_files`` lists the unmerged
    paths so callers can decide whether to attempt automated resolution
    or abort.  The caller is responsible for invoking ``abort_merge()``
    on conflict if it does not intend to resolve and commit.
    """

    success: bool
    conflicted_files: list[str]


@dataclass
class PrMergeState:
    """Snapshot of a pull request's mergeability and review state.

    Populated from ``gh pr list --json`` fields.  ``mergeable`` is
    GitHub's computed mergeability (``MERGEABLE``/``CONFLICTING``/
    ``UNKNOWN``); ``merge_state_status`` is the richer state signal
    (``CLEAN``/``BEHIND``/``DIRTY``/``BLOCKED``/...).  ``review_decision``
    is one of ``APPROVED``/``CHANGES_REQUESTED``/``REVIEW_REQUIRED`` or
    the empty string when unset.
    """

    number: int
    head_ref: str
    title: str
    body: str
    state: str
    mergeable: str
    merge_state_status: str
    review_decision: str
    created_at: str
    updated_at: str
    url: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def task_branch(task_id: str) -> str:
    """Return the canonical branch name for a task."""
    return f"maestro/task/{task_id}"


def evolution_branch(plan_id: str) -> str:
    """Return the canonical branch name for an evolution plan.

    Sanitisation is applied at plan-ID *creation* time (see the plan_id
    generator in evolution.py which uses ``uuid4().hex[:12]``).  Branch
    lookup must use the stored plan_id verbatim so that it always resolves
    to the branch that was actually created.
    """
    return f"maestro/evolve/{plan_id}"


def open_evolution_plan_ids(
    list_open: Callable[[], Mapping[str, object]],
) -> set[str] | None:
    """Plan ids behind the open ``maestro/evolve/*`` PRs *list_open* returns.

    Inverts :func:`evolution_branch` over the listing's head refs. A
    :class:`VCSError` from the listing reads as *unknown* and returns ``None``,
    so a caller that holds rows on open-PR state never mistakes a failed
    ``gh pr list`` for "no open PRs". The listing is capped repo-wide
    (``get_open_evolution_pr_states`` passes ``--limit 100``), so past that
    many open PRs an open evolution PR can be absent from the set.
    """
    try:
        heads = list_open()
    except VCSError:
        return None
    prefix = evolution_branch("")
    return {head[len(prefix):] for head in heads if head.startswith(prefix)}


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class VCSClient(Protocol):
    """Abstraction over local version control operations.

    Methods that *must succeed* raise :class:`VCSError` on failure.
    Methods documented as *best-effort* handle errors internally and return
    a fallback value (``None``, empty list, or empty string).
    """

    def get_current_branch(self) -> str:
        """Return the name of the currently checked-out branch.

        Raises :class:`VCSError` on failure.
        """
        ...

    def get_head_hash(self) -> str | None:
        """Return the current HEAD commit hash, or ``None`` on failure.

        Best-effort — returns ``None`` if the hash cannot be determined.
        """
        ...

    def checkout_new_branch(self, branch: str) -> None:
        """Create and switch to a new branch.

        Raises :class:`VCSError` if the branch already exists or on failure.
        """
        ...

    def checkout(self, branch: str) -> None:
        """Switch to an existing branch.

        Raises :class:`VCSError` on failure.
        """
        ...

    def delete_branch(self, branch: str) -> None:
        """Delete a local branch.

        Raises :class:`VCSError` on failure.
        """
        ...

    def has_changes(self) -> bool:
        """Return ``True`` if the working tree has uncommitted changes.

        Raises :class:`VCSError` on failure.
        """
        ...

    def porcelain_status(self) -> str:
        """Return the sorted ``git status --porcelain`` output as one string.

        Companion to :meth:`has_changes` for callers that need to compare
        the working-tree state across two points in time (e.g. detect
        whether an SDK call actually added or removed edits, when the
        prior round's edits are still uncommitted).

        Raises :class:`VCSError` on failure.
        """
        ...

    def discard_changes(self) -> None:
        """Discard all uncommitted changes and untracked files.

        Best-effort — logs warnings on failure, does not raise.
        """
        ...

    def stage_all(self) -> None:
        """Stage all changes for commit.

        Raises :class:`VCSError` on failure.
        """
        ...

    def commit(self, message: str) -> None:
        """Create a commit with the given message.

        Raises :class:`VCSError` on failure.
        """
        ...

    def amend_commit(self) -> None:
        """Amend the last commit with currently staged changes.

        Raises :class:`VCSError` on failure.
        """
        ...

    def push(self, branch: str, *, set_upstream: bool = False) -> None:
        """Push a branch to the remote.

        Raises :class:`VCSError` on failure.
        """
        ...

    def delete_remote_branch(self, branch: str) -> None:
        """Delete a branch from the ``origin`` remote.

        Scoped to reversible disposal of a PR-less ``maestro/evolve/*``
        orphan (analogous to the permitted ``gh pr close``): implementations
        MUST refuse any branch outside that prefix by raising, so a caller
        mistake can never delete ``main`` or a non-evolution ref. Never a
        force-push.

        Raises :class:`VCSError` on a refused branch or on failure.
        """
        ...

    def get_commits_between(
        self, old_hash: str, new_hash: str,
    ) -> list[CommitInfo]:
        """Return commits between *old_hash* (exclusive) and *new_hash* (inclusive).

        Results are returned in chronological (oldest-first) order, which is the
        reverse of ``git log``'s natural newest-first output.

        Best-effort — returns an empty list on failure.
        """
        ...

    def get_diff_stat(self, old_hash: str, new_hash: str) -> str:
        """Return diff stat output between two commits.

        Best-effort — returns an empty string on failure.
        """
        ...

    def get_diff(
        self, old_hash: str, new_hash: str, *, max_chars: int = 50_000,
    ) -> str:
        """Return unified diff between two commits.

        When ``max_chars > 0`` and the diff exceeds it, the diff is
        head+tail truncated with an accurate omitted-count marker; ``0``
        disables the cap so a downstream consumer can be the sole
        truncation authority.  Best-effort — returns an empty string on
        failure.
        """
        ...

    def get_file_log_p(
        self, rev_range: str, file_path: str, *, max_chars: int = 8000,
    ) -> str:
        """Return ``git log -p <rev_range> -- <file_path>`` output.

        Used by long-context conflict resolution to give the LLM the
        commit history that introduced the conflicting changes for a
        single file.  Output is truncated head-only at *max_chars*.
        Best-effort — returns an empty string on failure.
        """
        ...

    def fetch(
        self, remote: str = "origin", ref: str | None = None,
        *, prune: bool = False,
    ) -> None:
        """Fetch refs from a remote.

        When *prune* is true, pass ``--prune`` so remote-tracking refs for
        branches deleted on the remote are removed.  A caller that then reads
        ``git branch -r --contains`` (e.g. the tend reconcile via
        :meth:`remote_refs_containing`) must not treat a stale ref for a
        since-deleted branch as "still pushed."  Raises :class:`VCSError` on
        failure.
        """
        ...

    def merge_from(self, source_ref: str, *, message: str | None = None) -> MergeResult:
        """Merge ``source_ref`` into the current branch (no fast-forward disabled).

        On clean merge returns ``MergeResult(success=True, conflicted_files=[])``.
        On conflict leaves the working tree in the conflicted state and returns
        ``MergeResult(success=False, conflicted_files=[...])`` — the caller must
        call :meth:`abort_merge` if it does not intend to resolve.  Any other
        failure raises :class:`VCSError`.
        """
        ...

    def abort_merge(self) -> None:
        """Abort an in-progress merge and restore the pre-merge working tree.

        Best-effort — logs a warning on failure, does not raise.
        """
        ...

    def render_conflict_diff3(self, path: str) -> None:
        """Re-render *path*'s in-progress conflict markers in diff3 style.

        Runs ``git checkout --conflict=diff3 -- <path>`` so the on-disk conflict
        carries the ``|||||||`` merge-base section (from the retained index
        stages) — a general utility for a caller that needs the merge-base view
        of a conflict, not only ``<<<<<<<``/``=======``/``>>>>>>>``. Best-effort —
        logs a warning on failure, does not raise.
        """
        ...

    def merge_tree_conflicts(self, ref_a: str, ref_b: str) -> bool:
        """Return ``True`` if a three-way merge of *ref_a* and *ref_b* conflicts.

        Performs an in-memory ``git merge-tree`` (the merge result is written to
        the object DB only) — it never touches the working tree, index, or HEAD,
        so it is safe to call mid-task against a dirty worktree, unlike a
        checkout + ``git merge`` probe. Deterministic and immune to the
        code-host's asynchronously-recomputed ``mergeable`` field.

        Best-effort — returns ``False`` (no conflict detected) on any failure,
        including an unresolvable ref, so callers keep their prior verdict.
        """
        ...

    def merge_tree_text(self, ours_ref: str, theirs_ref: str) -> str:
        """Return the legacy ``git merge-tree`` diff of merging the two refs.

        Runs ``git merge-tree <merge-base> <ours_ref> <theirs_ref>`` — the
        legacy three-argument form that emits a per-file diff with inline
        conflict markers (``changed in both`` sections, ``<<<<<<<``/``=======``/
        ``>>>>>>>``). Entirely in-memory: never touches the working tree, index,
        or HEAD, so it is safe to call mid-merge to classify an in-progress
        conflict.

        Best-effort — returns ``""`` on any failure (unresolvable ref, no
        merge-base), so a caller reads it as "nothing deterministic to do".
        """
        ...

    def reset_hard(self, ref: str) -> None:
        """Reset the current branch to ``ref`` discarding local changes.

        Raises :class:`VCSError` on failure.
        """
        ...

    def reset_branch_to(self, branch: str, ref: str) -> None:
        """Move the named ``branch`` to ``ref``, never any other branch.

        A ``git reset --hard <ref>`` when ``branch`` is checked out, else a
        ``git branch -f <branch> <ref>`` that leaves the current branch and
        working tree untouched — so a rewind addressed to one branch can never
        land on whichever branch happens to be checked out.

        Raises :class:`VCSError` on failure.
        """
        ...

    def list_remote_branches(self, remote: str = "origin") -> list[str]:
        """List branch names on ``remote`` (without the ``<remote>/`` prefix).

        Best-effort — returns an empty list on failure.
        """
        ...

    def commits_ahead_behind(self, a: str, b: str) -> tuple[int, int]:
        """Return ``(ahead, behind)`` — commits in ``a`` not in ``b`` and vice versa.

        Best-effort — returns ``(0, 0)`` on failure.
        """
        ...

    def commits_not_on_remote(self, ref: str, remote: str = "origin") -> int:
        """Return the count of ``ref`` commits absent from every ``remote`` ref.

        Runs ``git rev-list --count <ref> --not --remotes=<remote>`` — the
        number of commits reachable from *ref* that are on no ``<remote>/*``
        remote-tracking branch, i.e. local commits not yet pushed anywhere on
        *remote*. A positive count means the branch is an owned, pushable act.
        Distinct from :meth:`commits_ahead_behind` against ``<remote>/main``: a
        branch fully pushed to ``<remote>/<branch>`` but ahead of ``main`` counts
        zero here, since its commits *are* on an ``origin`` ref.

        Best-effort — returns ``0`` on failure (unresolvable ref, garbled output,
        subprocess error), the safe direction: a failed probe never fabricates an
        owned-ahead reading that would refuse a legitimate stand-down.
        """
        ...

    def non_merge_commits_ahead(self, ref: str, base: str) -> int | None:
        """Return the count of non-merge commits in ``base..ref``.

        Runs ``git rev-list --count --no-merges <base>..<ref>``. Distinct from
        the ``ahead`` element of :meth:`commits_ahead_behind`, which counts
        merge commits too: a branch whose only new commit is a merge of *base*
        into itself is ahead without carrying any work of its own.

        Best-effort — returns ``None`` when the count is *unknown* (unresolvable
        ref, garbled output, subprocess error) rather than ``0``, so a failed
        probe is distinguishable from a measured "no commits" and callers can
        degrade to their own fallback instead of reading it as no work.
        """
        ...

    def lap_commits_since(self, ref: str, bases: tuple[str, ...]) -> int | None:
        """Return the count of first-parent, non-merge commits on *ref* not
        reachable from any of *bases*.

        Runs ``git rev-list --count --no-merges --first-parent <ref> ^<base>...``.
        Excluding every base (e.g. both the local and ``origin/`` heads stamped
        at checkout) keeps a fast-forward of a stale local ref onto upstream
        commits from counting as lap-authored.
        ``--first-parent`` keeps upstream commits brought in by a merge of
        ``origin/main`` out of the count and ``--no-merges`` drops that merge
        commit itself, so the count is the commits authored on *ref* since the
        *bases* watermarks.

        Best-effort — returns ``None`` when the count is unknown (unresolvable
        ref, garbled output, subprocess error), distinguishable from a measured
        zero.
        """
        ...

    def rev_parse(self, ref: str) -> str | None:
        """Resolve a ref to its full commit hash, or ``None`` if it does not exist.

        Best-effort — returns ``None`` on any failure (invalid ref, missing, etc.).
        """
        ...

    def is_ancestor(self, candidate: str, descendant: str) -> bool:
        """Return ``True`` if ``candidate`` is an ancestor of ``descendant``.

        Best-effort — returns ``False`` on any failure (missing commit,
        invalid hash, subprocess error). A hash that is no longer in tree
        (e.g. force-pushed away) must NOT be reported as an ancestor, so
        callers using this for safe-to-drop decisions never drop it.
        """
        ...

    def merge_base(self, ref_a: str, ref_b: str) -> str | None:
        """Return the best common ancestor of *ref_a* and *ref_b*, or ``None``.

        Runs ``git merge-base <ref_a> <ref_b>``.  The base SHA proposal 124's
        record-time landing arms key their gate-observation baseline on:
        ``merge_base(HEAD, "origin/main")`` is the commit an owned branch was
        cut from, so a red the base already carried can be told apart from one
        the diff introduced.  Best-effort — returns ``None`` on any failure
        (unresolvable ref, no common ancestor, subprocess error), mirroring
        :meth:`rev_parse`'s ``None``-on-failure posture; :meth:`is_ancestor`
        (which only returns a bool) is the closest existing shape, so this is
        its SHA-returning sibling.
        """
        ...

    def changed_paths(self, base: str, head: str) -> tuple[str, ...]:
        """Return the repo-relative paths differing between *base* and *head*.

        Runs ``git diff --name-only <base>...<head>`` — the symmetric-difference
        form, so the diff is *head* against the merge-base of the two, matching
        what a PR shows.  Feeds proposal 124's
        :func:`~maestro.gate_observation.attribute`, whose diff side is exactly
        this file set.  Best-effort — returns an empty tuple on any failure, and
        the result is bounded to :data:`_CHANGED_PATHS_MAX` paths.
        """
        ...

    def tracked_paths(self) -> frozenset[str]:
        """Return every git-tracked repo-relative POSIX path in the workspace.

        Runs ``git ls-files -z``.  The caller-supplied tracked set proposal
        143's :class:`~maestro.tree_retrieval.TreeView` admits reads against,
        so ``tree_retrieval`` itself stays subprocess-free.  Best-effort —
        returns an empty set on any failure, which the caller reads as "run
        tool-free".
        """
        ...

    def local_main_ahead_commits(self, remote: str = "origin") -> list[str]:
        """Return the SHAs on local ``main`` not on ``<remote>/main``, newest-first.

        Runs ``git rev-list <remote>/main..main``.  **Fail-closed:** raises
        :class:`VCSError` on subprocess failure.  An empty result is what
        authorizes a hard reset of ``main`` to ``<remote>/main``, so a swallowed
        failure must never masquerade as "no drift."
        """
        ...

    def remote_refs_containing(
        self, commit: str, remote: str = "origin",
    ) -> list[str]:
        """Return the ``<remote>/*`` branch names that contain ``commit``.

        Runs ``git branch -r --contains <commit>`` and filters/cleans the
        output exactly like :meth:`list_remote_branches` (strip the
        ``<remote>/`` prefix, drop ``HEAD``/symbolic lines).  Best-effort —
        returns ``[]`` on failure; the caller reads empty as "not provably
        pushed," which is the safe direction (escalate, never reset).
        """
        ...


@dataclass(frozen=True)
class LabelRemoval:
    """The newest ``unlabeled`` event for a label: its ``created_at`` and actor login.

    ``readable`` is ``False`` (with an empty ``at``) when the events read
    failed, so a caller can tell "unknown" apart from "no removal".
    """

    at: str
    actor: str = ""
    readable: bool = True


@dataclass(frozen=True)
class IssueClosure:
    """Who closed an issue last, whether a commit (or merged PR) did it, and who reopened it.

    ``closed`` is ``False`` when the events read cleanly but hold no
    ``closed`` event (never closed). ``reopened_by`` is the actor of the
    latest ``reopened`` event after that close, ``""`` when none followed.
    ``history`` is every ``closed`` / ``reopened`` event oldest first as
    ``(event, actor, via_commit)``, so a reader can see an earlier close the
    latest one hides; empty when the reader built the closure by hand.
    """

    actor: str
    via_commit: bool
    closed: bool = True
    reopened_by: str = ""
    history: tuple[tuple[str, str, bool], ...] = ()


@dataclass(frozen=True)
class IssueMeta:
    """An issue read by number: ``state`` is ``open`` / ``closed`` / ``gone``.

    ``gone`` means the host answered 404 or 410 (deleted), as
    distinct from an unreadable issue, which the read reports as ``None``.
    """

    state: str
    title: str = ""
    labels: tuple[str, ...] = ()


class CodeHost(Protocol):
    """Abstraction over CI/CD platform operations (PRs, issues, CI checks).

    Methods that *must succeed* raise :class:`VCSError` on failure.
    Methods documented as *best-effort* handle errors internally.
    """

    def create_pr(
        self, title: str, body: str, *, draft: bool = False,
    ) -> str:
        """Create a pull request. Returns the PR URL.

        ``draft=True`` opens the PR in draft state (``gh pr create --draft``).
        Used by the docs-from-tasks pipeline so reviewers see a clear "agent
        proposal — review before merging" signal.

        Raises :class:`VCSError` on failure.
        """
        ...

    def is_pr_merged(self, pr_number: int) -> bool:
        """Check whether a PR has been merged.

        Best-effort — returns False on failure.
        """
        ...

    def is_pr_merged_strict(self, pr_number: int) -> bool:
        """Check whether a PR has been merged — **fail-loud** (raises on failure).

        The same merge question as the best-effort :meth:`is_pr_merged` directly
        above, with the opposite failure contract: an unreachable or unparseable
        host read raises :class:`VCSError` (or ``json.JSONDecodeError``) instead
        of degrading to ``False``. Callers that *fall back to another oracle when
        the host cannot answer* must use this variant — a swallowed failure would
        otherwise be indistinguishable from a genuine "not merged" host reply and
        would silently strand the fallback tier as dead code (#2954).

        Wired at exactly the two sites whose fallback a raise unlocks:
        :meth:`maestro.agent.MaestroAgent.is_pr_settled` (the merge-truth
        chokepoint, whose ``branch_oracle`` runs only on a raise) and
        :meth:`maestro.agent.MaestroAgent._host_or_git_pr_merged`. Every other
        caller has no fallback to reach and wants the best-effort sibling.
        """
        ...

    def has_merged_at(self, pr_number: int) -> bool:
        """Return ``True`` when the host records a non-null ``mergedAt`` for a PR.

        The squash-merge-aware landing signal the ``is_pr_settled`` chokepoint
        consults as an override: a squash merge sets ``mergedAt`` yet is not the
        branch head's linear ancestor, so a ``merge-base --is-ancestor``-shaped
        read reports not-landed while this returns ``True``.

        Best-effort — returns False on failure.
        """
        ...

    def is_pr_closed(self, pr_number: int) -> bool:
        """Check whether a PR has been closed without merging.

        Best-effort — returns False on failure.
        """
        ...

    def get_issue_state(self, number: int) -> str | None:
        """Return ``"open"`` / ``"closed"`` for issue-or-PR *number*.

        GitHub shares one number space between issues and PRs, and the
        ``issues/{n}`` endpoint answers for both. Best-effort — returns
        ``None`` on failure or unrecognised output.
        """
        ...

    def get_pr_base_branch(self, pr_number: int) -> str:
        """Fetch the base (target) branch name of a pull request.

        Best-effort — returns an empty string on failure.
        """
        ...

    def get_commit_sha_of_pr(self, pr_number: int) -> str:
        """Fetch the merge commit sha of a merged pull request.

        Best-effort — returns an empty string on failure or when the PR has
        no merge commit (e.g. not yet merged).
        """
        ...

    def get_pr_check_conclusion(self, pr_number: int) -> str:
        """Fetch the CI status-check rollup conclusion for a PR's merge commit.

        Returns ``"success"``, ``"failure"``, ``"none"`` (no checks configured
        — proceed), or ``""`` (pending / unparseable / fetch failure — defer).
        ``"none"`` is distinct from ``""`` so a no-CI deployment is not
        deferred forever (#2109).  Advisory checks
        (:data:`ADVISORY_CHECK_NAMES`) are excluded.  Best-effort.
        """
        ...

    def get_ref_check_runs(self, sha: str) -> RefCheckRuns | None:
        """Return the latest non-advisory check-runs on commit *sha*.

        Post-merge non-gate jobs (:data:`REF_NON_GATE_CHECK_NAMES`) are also
        excluded.

        Best-effort — ``None`` on a host or parse failure, an unset repo, or a
        *sha* that is not 7 to 40 hex characters (refused before any call).
        """
        ...

    def get_pr_failing_check_names(self, pr_number: int) -> list[str]:
        """Return the names of non-advisory status checks that failed.

        Names the real failing CI gate(s) for a remediation directive so it
        does not misattribute the failure (#2440).  Advisory checks
        (:data:`ADVISORY_CHECK_NAMES`) are excluded.  Best-effort — returns
        ``[]`` on fetch failure or when no named failing check is present.
        """
        ...

    def read_run_log_failed(self, pr_number: int) -> str:
        """Return a bounded, secret-redacted excerpt of the PR's failing CI log.

        Resolves the first non-advisory failing status-check rollup item to its
        workflow run id and returns ``gh run view <id> --log-failed`` output,
        redacted of any credential material and truncated (keeping the tail,
        where a failed step's error summary lands) to a hard char cap.
        Best-effort — returns ``""`` on any fetch/parse failure or when no
        failing run id can be resolved, never raising into the caller.
        """
        ...

    def get_pr_diff(self, pr_number: int) -> str:
        """Fetch the diff for a pull request.

        Best-effort — returns an empty string on failure.
        """
        ...

    def get_pr_info(self, pr_number: int) -> dict[str, str]:
        """Fetch PR title and body.

        Best-effort — returns ``{"title": "", "body": ""}`` on failure.
        """
        ...

    def post_pr_review(
        self, pr_number: int, payload: dict[str, Any],
    ) -> None:
        """Post an inline review on a PR.

        Raises :class:`VCSError` on failure.
        """
        ...

    def get_pr_review_threads(
        self, pr_number: int,
    ) -> list[dict[str, Any]]:
        """Fetch review threads with resolution state via GraphQL.

        Best-effort — returns an empty list on failure.
        """
        ...

    def get_pr_review_bodies(self, pr_number: int) -> list[str]:
        """Fetch the bodies of a PR's submitted reviews, oldest first.

        The tend phase scans these for the AI reviewer's overflow-findings
        block (proposal 064).  Best-effort — returns an empty list on failure.
        """
        ...

    def get_pr_comment_bodies(self, pr_number: int) -> list[str]:
        """Fetch the bodies of a PR's issue comments, oldest first.

        Used to locate the roll-up overflow acknowledgement comment
        (proposal 064).  Best-effort — returns an empty list on failure.
        """
        ...

    def get_pr_review_threads_strict(
        self, pr_number: int,
    ) -> list[dict[str, Any]]:
        """:meth:`get_pr_review_threads` — **fail-loud** (raises on failure).

        An unreachable host or unparseable payload raises :class:`VCSError`
        instead of degrading to ``[]``, so a caller that must tell a failed
        scan from an empty one (the comment-completeness gate, #4065) can.
        """
        ...

    def get_pr_review_bodies_strict(self, pr_number: int) -> list[str]:
        """:meth:`get_pr_review_bodies` — **fail-loud** (raises :class:`VCSError`)."""
        ...

    def get_pr_comment_bodies_strict(self, pr_number: int) -> list[str]:
        """:meth:`get_pr_comment_bodies` — **fail-loud** (raises :class:`VCSError`)."""
        ...

    def reply_to_review_comment(
        self, pr_number: int, comment_id: int, body: str,
    ) -> None:
        """Reply to an existing review comment thread.

        Best-effort — logs a warning on failure, does not raise.
        """
        ...

    def resolve_review_thread(self, thread_id: str) -> None:
        """Resolve a review thread by its GraphQL node id.

        Best-effort — logs a warning on failure, does not raise.
        """
        ...

    def list_issues(
        self,
        *,
        state: str = "open",
        labels: list[str] | None = None,
        exclude_labels: list[str] | None = None,
        page_size: int | None = None,
        title_contains: str | None = None,
    ) -> list[dict[str, Any]] | None:
        """List issues. Returns ``None`` on failure (vs empty list for no issues).

        *title_contains* narrows to issues whose title contains that text. It
        goes through the host's eventually consistent search index, so callers
        must re-check the title exactly and must never back an idempotency
        check with it. Best-effort.
        """
        ...

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> int | None:
        """Create an issue. Returns the issue number, or ``None`` on failure.

        Best-effort.
        """
        ...

    def close_issue(self, number: int) -> bool:
        """Close an issue; ``True`` on success.

        Best-effort — logs a warning on failure, does not raise.
        """
        ...

    def reopen_issue(self, number: int) -> bool:
        """Reopen a closed issue; ``True`` on success.

        Best-effort — logs a warning on failure, does not raise.
        """
        ...

    def get_issue_label_actor(self, number: int, label: str) -> str | None:
        """Return the login that last applied *label* to issue *number*.

        ``None`` when the latest event for that label is an ``unlabeled``, when
        no event names it, or on failure. Best-effort.
        """
        ...

    def add_issue_label(self, number: int, label: str) -> bool:
        """Add *label* to issue *number*; ``True`` on success.

        Best-effort — logs a warning on failure, does not raise.
        """
        ...

    def remove_issue_label(self, number: int, label: str) -> bool:
        """Remove *label* from issue *number*; ``True`` on success.

        Best-effort — logs a warning on failure, does not raise.
        """
        ...

    def get_issue_label_removed_at(self, number: int, label: str) -> LabelRemoval | None:
        """Return the newest ``unlabeled`` event for *label* (stamp and actor).

        ``None`` when that label's latest event is a ``labeled`` one or when no
        ``unlabeled`` event names it; ``LabelRemoval("", readable=False)``
        when the events read failed. Best-effort.
        """
        ...

    def get_issue_closure(self, number: int) -> IssueClosure | None:
        """Return who closed issue *number* last, whether a commit did it, and who reopened it.

        An issue never closed reads ``IssueClosure("", False, closed=False)``;
        ``None`` only on failure, so a caller can fail closed. Best-effort.
        """
        ...

    def get_issue_meta(self, number: int) -> IssueMeta | None:
        """Return issue *number*'s state, title and labels in one read.

        A 404/410 answer is ``IssueMeta("gone")``; any other failure ``None``.
        Best-effort.
        """
        ...

    def comment_issue(self, number: int, body: str) -> bool:
        """Post a comment on an issue.

        Returns ``True`` on success, ``False`` on failure. Best-effort —
        logs a warning on failure, does not raise.
        """
        ...

    def get_open_evolution_pr_states(
        self, branch_prefix: str, *, strict: bool = False,
    ) -> dict[str, PrMergeState]:
        """Return ``{head_ref: PrMergeState}`` for all open PRs whose branch starts with ``branch_prefix``.

        Used by the evolution PR tender to reconcile local branches with
        upstream PR state in a single API call.  Best-effort by default —
        returns an empty dict on failure.

        Pass ``strict=True`` when an empty result must mean 'no such PR' and
        never 'the listing failed': it raises :class:`VCSError` on any host
        failure (command error, malformed JSON, or unset repo) instead of
        swallowing it, so a caller that reads absence as positive
        confirmation cannot be defeated by a transient ``gh`` failure (#2813).
        """
        ...

    def find_evolution_pr_for_branch(self, branch: str) -> PrMergeState | None:
        """Return the newest open or merged PR whose head is ``branch``.

        A closed-unmerged PR was disposed, so it is ignored. Fail loud: raises
        :class:`VCSError` on command error or malformed JSON, so
        ``None`` always means 'no such PR', never 'the listing failed'. An
        unset repo lets ``gh`` infer it from the workspace, as ``create_pr``
        does (#4553).
        """
        ...

    def resolve_owner(self) -> str:
        """Resolve the current ``gh`` user's login.

        Best-effort — returns an empty string on failure.
        """
        ...

    def repo_exists(self, owner: str, name: str) -> bool:
        """Check whether ``{owner}/{name}`` exists on the code host.

        Best-effort — returns ``False`` on 404, ``False`` with a warning
        on other non-zero exits (fail-open: real collisions surface at
        repo-create time in proposal 033).
        """
        ...

    def create_public_repo(
        self, name: str, description: str, *, owner: str = "",
    ) -> str:
        """Create a public repository under *owner* (or the resolved login).

        Deliberately split from any working-copy push step: the publication
        pipeline checkpoints between repo creation and push so a crash
        cannot leave an orphan repo whose origin we forgot. Returns the
        canonical clone URL (``https://github.com/<owner>/<name>``).

        Raises :class:`VCSError` on failure.
        """
        ...


# ---------------------------------------------------------------------------
# Subprocess implementations
# ---------------------------------------------------------------------------

# ASCII SOH (Start of Heading) — cannot appear in commit messages
# and is not treated as whitespace by Python's str.strip().
_COMMIT_SEP = "\x01"



class SubprocessVCS:
    """Git CLI implementation of :class:`VCSClient`."""

    def __init__(
        self,
        workspace: Path,
        *,
        timeout: int = 30,
        quick_timeout: int = 10,
        push_timeout: int = 120,
    ) -> None:
        self._cwd = str(workspace)
        self._timeout = timeout
        self._quick_timeout = quick_timeout
        self._push_timeout = push_timeout

    def _run(
        self,
        cmd: list[str],
        *,
        timeout: int | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run a git command, raising :class:`VCSError` on failure.

        When *check* is ``False`` a non-zero exit is returned as a normal
        :class:`~subprocess.CompletedProcess` (so the caller can inspect
        ``returncode``) instead of raising; a timeout or OS-level error still
        raises :class:`VCSError`.
        """
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=check,
                timeout=self._timeout if timeout is None else timeout,
                cwd=self._cwd,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            parts = [str(exc)]
            stderr = getattr(exc, "stderr", None)
            if stderr:
                rendered = stderr.strip() if isinstance(stderr, str) else repr(stderr)
                parts.append(f"stderr: {rendered}")
            stdout = getattr(exc, "stdout", None)
            if stdout:
                rendered = stdout.strip() if isinstance(stdout, str) else repr(stdout)
                parts.append(f"stdout: {rendered}")
            raise VCSError(" | ".join(parts)) from exc

    # -- Must-succeed methods -----------------------------------------------

    def get_current_branch(self) -> str:
        result = self._run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=self._quick_timeout,
        )
        branch = result.stdout.strip()
        if branch == "HEAD":
            raise VCSError("HEAD is detached — not on any branch")
        return branch

    def checkout_new_branch(self, branch: str) -> None:
        self._run(["git", "checkout", "-b", branch])

    def checkout(self, branch: str) -> None:
        self._run(["git", "checkout", branch])

    def delete_branch(self, branch: str) -> None:
        self._run(["git", "branch", "-D", branch])

    def has_changes(self) -> bool:
        result = self._run(
            ["git", "status", "--porcelain"],
            timeout=self._quick_timeout,
        )
        return bool(result.stdout.strip())

    def porcelain_status(self) -> str:
        result = self._run(
            ["git", "status", "--porcelain"],
            timeout=self._quick_timeout,
        )
        return "\n".join(sorted(result.stdout.splitlines()))

    def discard_changes(self) -> None:
        def _best_effort(cmd: list[str]) -> bool:
            """Run *cmd*; return True on success, False (logged) on any failure.

            Best-effort by contract — never raises, so cleanup callers always
            get a return regardless of git state.
            """
            try:
                self._run(cmd, timeout=self._quick_timeout)
                return True
            except Exception:
                logger.warning(
                    "discard_changes step failed: %s", cmd, exc_info=True,
                )
                return False

        reset_ok = _best_effort(["git", "reset", "HEAD"])
        _best_effort(["git", "checkout", "--", "."])
        if not reset_ok:
            # ``git reset HEAD`` failed (e.g. an unborn branch with no
            # resolvable HEAD), so the index was never unstaged. ``checkout
            # -- .`` only restores tracked paths and ``clean -fd`` (below)
            # skips index-tracked files, so a staged-but-uncommitted new file
            # would survive the whole cleanup. Clear the index
            # HEAD-independently first so ``clean`` sweeps the now-untracked
            # files. ``--cached`` leaves the working tree untouched and
            # ``--ignore-unmatch`` keeps an empty index from erroring.
            _best_effort(
                ["git", "rm", "-r", "--cached", "--ignore-unmatch", "--", "."],
            )
        _best_effort(["git", "clean", "-fd"])

    def stage_all(self) -> None:
        self._run(["git", "add", "-A"])

    def commit(self, message: str) -> None:
        self._run(["git", "commit", "-m", message])

    def amend_commit(self) -> None:
        self._run(["git", "commit", "--amend", "--no-edit"])

    def push(self, branch: str, *, set_upstream: bool = False) -> None:
        cmd = ["git", "push"]
        if set_upstream:
            cmd.extend(["-u", "origin", branch])
        else:
            cmd.extend(["origin", branch])
        self._run(cmd, timeout=self._push_timeout)

    def delete_remote_branch(self, branch: str) -> None:
        # Refuse anything outside the evolution-branch prefix: the delete is
        # only ever a reversible disposal of a PR-less ``maestro/evolve/*``
        # orphan, so a caller mistake must fail loudly here rather than delete
        # ``main`` or another ref.
        if not branch.startswith("maestro/evolve/"):
            raise VCSError(
                f"refusing to delete remote branch {branch!r}: only "
                "maestro/evolve/* orphans may be removed",
            )
        self._run(
            ["git", "push", "origin", "--delete", branch],
            timeout=self._push_timeout,
        )

    # -- Best-effort methods ------------------------------------------------

    def get_head_hash(self) -> str | None:
        try:
            result = self._run(
                ["git", "rev-parse", "HEAD"],
                timeout=self._quick_timeout,
            )
            return result.stdout.strip() or None
        except VCSError:
            logger.warning("Failed to get current HEAD", exc_info=True)
            return None

    def get_commits_between(
        self, old_hash: str, new_hash: str,
    ) -> list[CommitInfo]:
        if not old_hash or not new_hash:
            return []
        try:
            result = self._run([
                "git", "log",
                f"--format=%H%n%s%n%an%n{_COMMIT_SEP}",
                f"{old_hash}..{new_hash}",
            ])
        except VCSError:
            logger.warning("Failed to get new commits", exc_info=True)
            return []

        commits: list[CommitInfo] = []
        for block in result.stdout.strip().split(_COMMIT_SEP):
            block = block.strip("\n")
            if not block:
                continue
            lines = block.splitlines()
            if len(lines) >= 3:
                commits.append(CommitInfo(
                    hash=lines[0].strip(),
                    subject=lines[1].strip(),
                    author=lines[2].strip(),
                ))
        # git log returns newest-first; reverse to chronological order.
        commits.reverse()
        return commits

    def get_diff_stat(self, old_hash: str, new_hash: str) -> str:
        if not old_hash or not new_hash:
            return ""
        try:
            result = self._run(
                ["git", "diff", "--stat", f"{old_hash}..{new_hash}"],
            )
            return result.stdout.strip()
        except VCSError:
            logger.warning("Failed to get diff stat", exc_info=True)
            return ""

    def get_diff(
        self, old_hash: str, new_hash: str, *, max_chars: int = 50_000,
    ) -> str:
        if not old_hash or not new_hash:
            return ""
        try:
            result = self._run(
                ["git", "diff", f"{old_hash}..{new_hash}"],
            )
            diff = result.stdout.strip()
            if max_chars > 0 and len(diff) > max_chars:
                # Head+tail truncate with an accurate marker rather than a
                # silent head-slice: the tail of a large refactor is the
                # riskiest part to review, and a lone head-slice hid both it
                # and the true omitted count.  Mirrors ``_truncate_diff``'s
                # shape (kept local — importing from ``maestro.evolution``
                # would invert the dependency direction).
                half = max_chars // 2
                omitted = len(diff) - (2 * half)
                marker = (
                    f"\n\n[... {omitted} chars truncated by get_diff cap ...]\n\n"
                )
                return diff[:half] + marker + diff[-half:]
            return diff
        except VCSError:
            logger.warning("Failed to get diff", exc_info=True)
            return ""

    def get_file_log_p(
        self, rev_range: str, file_path: str, *, max_chars: int = 8000,
    ) -> str:
        if not rev_range or not file_path:
            return ""
        try:
            result = self._run(
                ["git", "log", "-p", rev_range, "--", file_path],
            )
            log = result.stdout.strip()
            if max_chars > 0 and len(log) > max_chars:
                return log[:max_chars] + "\n... [truncated]"
            return log
        except VCSError:
            logger.warning(
                "Failed to get file log for %s", file_path, exc_info=True,
            )
            return ""

    def fetch(
        self, remote: str = "origin", ref: str | None = None,
        *, prune: bool = False,
    ) -> None:
        cmd = ["git", "fetch", remote]
        if prune:
            cmd.append("--prune")
        if ref:
            cmd.append(ref)
        self._run(cmd, timeout=self._push_timeout)

    def merge_from(
        self, source_ref: str, *, message: str | None = None,
    ) -> MergeResult:
        cmd = ["git", "merge", "--no-ff", "--no-edit"]
        if message is not None:
            cmd.extend(["-m", message])
        cmd.append(source_ref)
        try:
            self._run(cmd)
            return MergeResult(success=True, conflicted_files=[])
        except VCSError:
            try:
                status = self._run(
                    ["git", "diff", "--name-only", "--diff-filter=U"],
                    timeout=self._quick_timeout,
                )
                conflicted = [
                    line.strip()
                    for line in status.stdout.splitlines()
                    if line.strip()
                ]
            except VCSError:
                conflicted = []
            if not conflicted:
                # Not a conflict — propagate the underlying failure.
                raise
            return MergeResult(success=False, conflicted_files=conflicted)

    def abort_merge(self) -> None:
        try:
            self._run(
                ["git", "merge", "--abort"], timeout=self._quick_timeout,
            )
        except VCSError:
            logger.warning("merge --abort failed", exc_info=True)

    def render_conflict_diff3(self, path: str) -> None:
        try:
            self._run(
                ["git", "checkout", "--conflict=diff3", "--", path],
                timeout=self._quick_timeout,
            )
        except VCSError:
            logger.warning(
                "checkout --conflict=diff3 failed for %s", path, exc_info=True,
            )

    def merge_tree_conflicts(self, ref_a: str, ref_b: str) -> bool:
        # ``git merge-tree --write-tree`` (git >= 2.38) runs a real three-way
        # merge entirely in the object DB: exit 0 == clean, exit 1 == conflicts,
        # any other status == a problem (e.g. an unresolvable ref). ``--name-only``
        # bounds stdout to conflicted paths, but only the exit code is consulted.
        # ``check=False`` keeps the conflict exit (1) from raising.
        try:
            result = self._run(
                [
                    "git", "merge-tree", "--write-tree", "--name-only",
                    ref_a, ref_b,
                ],
                check=False,
            )
        except VCSError:
            logger.warning(
                "merge-tree probe failed for %s + %s", ref_a, ref_b,
                exc_info=True,
            )
            return False
        return result.returncode == 1

    def merge_tree_text(self, ours_ref: str, theirs_ref: str) -> str:
        # Legacy three-argument ``git merge-tree`` needs an explicit merge-base;
        # ``git merge-base`` supplies it. Any failure (unresolvable ref, no
        # common ancestor) fails safe to ``""`` so the caller keeps its prior
        # routing.
        try:
            base_result = self._run(
                ["git", "merge-base", ours_ref, theirs_ref],
                timeout=self._quick_timeout,
            )
        except VCSError:
            logger.warning(
                "merge-base failed for %s + %s", ours_ref, theirs_ref,
                exc_info=True,
            )
            return ""
        base = base_result.stdout.strip()
        if not base:
            return ""
        try:
            result = self._run(
                ["git", "merge-tree", base, ours_ref, theirs_ref],
                check=False,
            )
        except VCSError:
            logger.warning(
                "merge-tree text failed for %s + %s", ours_ref, theirs_ref,
                exc_info=True,
            )
            return ""
        return result.stdout

    def reset_hard(self, ref: str) -> None:
        self._run(["git", "reset", "--hard", ref])

    def reset_branch_to(self, branch: str, ref: str) -> None:
        if self.get_current_branch() == branch:
            self._run(["git", "reset", "--hard", ref])
        else:
            self._run(["git", "branch", "-f", branch, ref])

    def list_remote_branches(self, remote: str = "origin") -> list[str]:
        try:
            result = self._run(
                ["git", "branch", "-r", "--format=%(refname:short)"],
                timeout=self._timeout,
            )
        except VCSError:
            logger.warning("Failed to list remote branches", exc_info=True)
            return []
        prefix = f"{remote}/"
        names: list[str] = []
        for line in result.stdout.splitlines():
            name = line.strip()
            if not name or name.endswith("/HEAD") or " -> " in name:
                continue
            if name.startswith(prefix):
                names.append(name[len(prefix):])
        return names

    def commits_ahead_behind(self, a: str, b: str) -> tuple[int, int]:
        try:
            result = self._run(
                ["git", "rev-list", "--left-right", "--count", f"{a}...{b}"],
                timeout=self._quick_timeout,
            )
        except VCSError:
            logger.warning(
                "Failed to compute ahead/behind for %s...%s", a, b, exc_info=True,
            )
            return (0, 0)
        parts = result.stdout.split()
        if len(parts) != 2:
            return (0, 0)
        try:
            return (int(parts[0]), int(parts[1]))
        except ValueError:
            return (0, 0)

    def commits_not_on_remote(self, ref: str, remote: str = "origin") -> int:
        try:
            result = self._run(
                [
                    "git", "rev-list", "--count", ref,
                    "--not", f"--remotes={remote}",
                ],
                timeout=self._quick_timeout,
            )
        except VCSError:
            logger.warning(
                "Failed to count commits on %s not on remote %s",
                ref, remote, exc_info=True,
            )
            return 0
        stripped = result.stdout.strip()
        try:
            return int(stripped)
        except ValueError:
            return 0

    def non_merge_commits_ahead(self, ref: str, base: str) -> int | None:
        try:
            result = self._run(
                [
                    "git", "rev-list", "--count", "--no-merges",
                    f"{base}..{ref}",
                ],
                timeout=self._quick_timeout,
            )
        except VCSError:
            logger.warning(
                "Failed to count non-merge commits for %s..%s",
                base, ref, exc_info=True,
            )
            return None
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None

    def lap_commits_since(self, ref: str, bases: tuple[str, ...]) -> int | None:
        try:
            result = self._run(
                [
                    "git", "rev-list", "--count", "--no-merges",
                    "--first-parent", ref, *(f"^{b}" for b in bases),
                ],
                timeout=self._quick_timeout,
            )
        except VCSError:
            logger.warning(
                "Failed to count lap commits for %s since %s",
                ref, bases, exc_info=True,
            )
            return None
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None

    def rev_parse(self, ref: str) -> str | None:
        try:
            result = self._run(
                ["git", "rev-parse", "--verify", ref],
                timeout=self._quick_timeout,
            )
        except VCSError:
            return None
        value = result.stdout.strip()
        return value or None

    def is_ancestor(self, candidate: str, descendant: str) -> bool:
        if not candidate or not descendant:
            return False
        try:
            self._run(
                ["git", "merge-base", "--is-ancestor", candidate, descendant],
                timeout=self._quick_timeout,
            )
            return True
        except VCSError as exc:
            cause = exc.__cause__
            if (
                isinstance(cause, subprocess.CalledProcessError)
                and cause.returncode == 1
            ):
                # Exit 1 is git's documented "not an ancestor" signal.
                return False
            logger.warning(
                "is_ancestor failed for %s..%s",
                candidate[:8] if candidate else "?",
                descendant[:8] if descendant else "?",
                exc_info=True,
            )
            return False

    def merge_base(self, ref_a: str, ref_b: str) -> str | None:
        try:
            result = self._run(
                ["git", "merge-base", ref_a, ref_b],
                timeout=self._quick_timeout,
            )
        except VCSError:
            return None
        value = result.stdout.strip()
        return value or None

    def changed_paths(self, base: str, head: str) -> tuple[str, ...]:
        try:
            result = self._run(
                ["git", "diff", "--name-only", f"{base}...{head}"],
                timeout=self._quick_timeout,
            )
        except VCSError:
            return ()
        paths = [
            line.strip() for line in result.stdout.splitlines() if line.strip()
        ]
        return tuple(paths[:_CHANGED_PATHS_MAX])

    def tracked_paths(self) -> frozenset[str]:
        try:
            result = self._run(
                ["git", "ls-files", "-z"], timeout=self._quick_timeout,
            )
        except VCSError:
            return frozenset()
        return frozenset(path for path in result.stdout.split("\0") if path)

    def local_main_ahead_commits(self, remote: str = "origin") -> list[str]:
        # Fail-closed: an empty return authorizes resetting main to
        # origin/main, so a subprocess failure must raise, never masquerade as
        # "no drift." rev-list emits newest-first already.
        result = self._run(
            ["git", "rev-list", f"{remote}/main..main"],
            timeout=self._quick_timeout,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def remote_refs_containing(
        self, commit: str, remote: str = "origin",
    ) -> list[str]:
        try:
            result = self._run(
                ["git", "branch", "-r", "--contains", commit],
                timeout=self._timeout,
            )
        except VCSError:
            logger.warning(
                "Failed to list remote refs containing %s",
                commit[:8] if commit else "?",
                exc_info=True,
            )
            return []
        prefix = f"{remote}/"
        names: list[str] = []
        for line in result.stdout.splitlines():
            name = line.strip()
            if not name or name.endswith("/HEAD") or " -> " in name:
                continue
            if name.startswith(prefix):
                names.append(name[len(prefix):])
        return names


class SubprocessCodeHost:
    """GitHub CLI implementation of :class:`CodeHost`."""

    def __init__(
        self,
        repo: str | None = None,
        *,
        workspace: Path | None = None,
        timeout: int = 30,
        pr_creation_timeout: int = 60,
        reviewer: str | None = None,
    ) -> None:
        self._repo = repo
        self._cwd = str(workspace) if workspace else None
        self._timeout = timeout
        self._pr_creation_timeout = pr_creation_timeout
        self._reviewer = reviewer
        self._cached_owner: str | None = None

    def _repo_args(self) -> list[str]:
        """Return ``['--repo', repo]`` when a repo is configured, else ``[]``."""
        return ["--repo", self._repo] if self._repo else []

    def _run(
        self,
        cmd: list[str],
        *,
        timeout: int | None = None,
        input_data: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a gh command, raising :class:`VCSError` on failure."""
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=self._timeout if timeout is None else timeout,
                cwd=self._cwd,
                input=input_data,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as exc:
            parts = [str(exc)]
            stderr = getattr(exc, "stderr", None)
            if stderr:
                rendered = stderr.strip() if isinstance(stderr, str) else repr(stderr)
                parts.append(f"stderr: {rendered}")
            stdout = getattr(exc, "stdout", None)
            if stdout:
                rendered = stdout.strip() if isinstance(stdout, str) else repr(stdout)
                parts.append(f"stdout: {rendered}")
            raise VCSError(" | ".join(parts)) from exc

    # -- Must-succeed methods -----------------------------------------------

    def create_pr(
        self, title: str, body: str, *, draft: bool = False,
    ) -> str:
        cmd = [
            "gh", "pr", "create",
            "--title", title,
            "--body", body,
            *self._repo_args(),
        ]
        if draft:
            cmd.append("--draft")
        if self._reviewer:
            cmd.extend(["--reviewer", self._reviewer])
        try:
            result = self._run(cmd, timeout=self._pr_creation_timeout)
        except VCSError as exc:
            url = self._created_pr_url(exc)
            if url is None:
                raise
            stderr = getattr(exc.__cause__, "stderr", "") or ""
            logger.warning(
                "gh pr create exited non-zero after creating %s (degraded "
                "success, e.g. reviewer request failed): %s",
                url, str(stderr).strip(),
            )
            return url
        return result.stdout.strip()

    def _created_pr_url(self, exc: VCSError) -> str | None:
        """Return the PR URL a failed ``gh pr create`` already printed, if any.

        Only a :class:`subprocess.CalledProcessError` carries usable stdout;
        the URL must belong to the configured repo (any repo when unset).
        """
        cause = exc.__cause__
        if not isinstance(cause, subprocess.CalledProcessError):
            return None
        stdout = cause.stdout if isinstance(cause.stdout, str) else ""
        for match in _PR_URL_RE.finditer(stdout):
            if (
                not self._repo
                or match.group(1).casefold() == self._repo.casefold()
            ):
                return match.group(0)
        return None

    def resolve_owner(self) -> str:
        if self._cached_owner is not None:
            return self._cached_owner
        try:
            result = self._run(["gh", "api", "user", "--jq", ".login"])
        except VCSError:
            logger.warning("resolve_owner failed", exc_info=True)
            return ""
        # ``gh api --jq`` returns a raw string; strip whitespace and any
        # surrounding quotes to tolerate both unquoted and JSON-quoted output.
        owner = result.stdout.strip().strip('"')
        if owner:
            self._cached_owner = owner
        return owner

    def repo_exists(self, owner: str, name: str) -> bool:
        try:
            self._run(
                ["gh", "api", f"repos/{owner}/{name}", "--silent"],
            )
            return True
        except VCSError as exc:
            msg = str(exc)
            if "exit status 1" in msg and (
                "Not Found" in msg or "HTTP 404" in msg
            ):
                return False
            logger.warning(
                "repo_exists inconclusive for %s/%s", owner, name,
                exc_info=True,
            )
            return False

    def create_public_repo(
        self, name: str, description: str, *, owner: str = "",
    ) -> str:
        resolved_owner = owner or self.resolve_owner()
        if not resolved_owner:
            raise VCSError(
                "create_public_repo: owner not supplied and resolve_owner "
                "returned empty",
            )
        cmd = [
            "gh", "repo", "create",
            f"{resolved_owner}/{name}",
            "--public",
            "--description", description,
        ]
        self._run(cmd, timeout=self._pr_creation_timeout)
        return f"https://github.com/{resolved_owner}/{name}"

    @staticmethod
    def _validate_repo(repo: str | None) -> tuple[str, str]:
        """Validate and split a repo string into (owner, name).

        Raises :class:`VCSError` if the repo is missing or malformed.
        """
        if not repo:
            raise VCSError("No repo configured")
        parts = repo.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise VCSError(f"Invalid repo format: {repo!r} (expected 'owner/name')")
        return parts[0], parts[1]

    def post_pr_review(
        self, pr_number: int, payload: dict[str, Any],
    ) -> None:
        owner, name = self._validate_repo(self._repo)
        self._run(
            [
                "gh", "api",
                f"repos/{owner}/{name}/pulls/{pr_number}/reviews",
                "--method", "POST",
                "--input", "-",
            ],
            input_data=json.dumps(payload),
        )

    # -- Best-effort methods ------------------------------------------------

    # Each best-effort review read is a thin wrapper over its ``*_strict``
    # sibling, so the ``gh`` command and parse live in one place and only the
    # failure contract differs (the ``is_pr_merged`` pairing, #4065).

    def get_pr_review_threads(
        self, pr_number: int,
    ) -> list[dict[str, Any]]:
        try:
            self._validate_repo(self._repo)
        except VCSError:
            # No repo configured: silently empty, as before #4065.
            return []
        try:
            return self.get_pr_review_threads_strict(pr_number)
        except VCSError:
            logger.warning(
                "Failed to fetch PR review threads", exc_info=True,
            )
            return []

    def get_pr_review_threads_strict(
        self, pr_number: int,
    ) -> list[dict[str, Any]]:
        owner, repo = self._validate_repo(self._repo)
        query = (
            "query($owner:String!,$repo:String!,$pr:Int!){"
            "repository(owner:$owner,name:$repo){"
            "pullRequest(number:$pr){"
            "reviewThreads(first:100){nodes{"
            "id isResolved isOutdated "
            "comments(first:100){nodes{databaseId path body author{login}}}"
            "}}"
            "}}}"
        )
        result = self._run(
            ["gh", "api", "graphql", "--input", "-"],
            input_data=json.dumps({
                "query": query,
                "variables": {"owner": owner, "repo": repo, "pr": pr_number},
            }),
        )
        try:
            data = json.loads(result.stdout)
            nodes: list[dict[str, Any]] = (
                data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise VCSError(f"Unparseable PR review threads: {exc}") from exc
        return nodes

    def get_pr_review_bodies(self, pr_number: int) -> list[str]:
        try:
            return self.get_pr_review_bodies_strict(pr_number)
        except VCSError:
            logger.warning("Failed to fetch PR review bodies", exc_info=True)
            return []

    def get_pr_review_bodies_strict(self, pr_number: int) -> list[str]:
        return self._pr_view_bodies(pr_number, "reviews")

    def get_pr_comment_bodies(self, pr_number: int) -> list[str]:
        try:
            return self.get_pr_comment_bodies_strict(pr_number)
        except VCSError:
            logger.warning("Failed to fetch PR comment bodies", exc_info=True)
            return []

    def get_pr_comment_bodies_strict(self, pr_number: int) -> list[str]:
        return self._pr_view_bodies(pr_number, "comments")

    def _pr_view_bodies(self, pr_number: int, field: str) -> list[str]:
        """The ``body`` of each ``field`` entry of ``gh pr view`` — raises on failure."""
        result = self._run([
            "gh", "pr", "view", str(pr_number),
            *self._repo_args(),
            "--json", field,
        ])
        try:
            data = json.loads(result.stdout)
            entries = data.get(field) or []
            return [
                str(entry.get("body") or "")
                for entry in entries
                if isinstance(entry, dict)
            ]
        except (json.JSONDecodeError, AttributeError, TypeError) as exc:
            raise VCSError(f"Unparseable PR {field}: {exc}") from exc

    def reply_to_review_comment(
        self, pr_number: int, comment_id: int, body: str,
    ) -> None:
        if not self._repo:
            return
        try:
            self._run(
                [
                    "gh", "api",
                    f"repos/{self._repo}/pulls/{pr_number}/comments",
                    "--method", "POST",
                    "--input", "-",
                ],
                input_data=json.dumps({
                    "body": body,
                    "in_reply_to": comment_id,
                }),
            )
        except VCSError:
            logger.warning(
                "Failed to reply to review comment %d", comment_id,
                exc_info=True,
            )

    def resolve_review_thread(self, thread_id: str) -> None:
        if not self._repo:
            return
        mutation = (
            "mutation($threadId:ID!){"
            "resolveReviewThread(input:{threadId:$threadId}){thread{id}}}"
        )
        try:
            self._run(
                ["gh", "api", "graphql", "--input", "-"],
                input_data=json.dumps({
                    "query": mutation,
                    "variables": {"threadId": thread_id},
                }),
            )
        except VCSError:
            logger.warning(
                "Failed to resolve review thread %s", thread_id,
                exc_info=True,
            )

    def is_pr_merged(self, pr_number: int) -> bool:
        """The fail-loud read below, degraded to ``False`` on host failure.

        A thin wrapper over :meth:`is_pr_merged_strict` rather than a second
        copy of the same ``gh`` invocation: the command and the ``state ==
        "MERGED"`` parse live in exactly one place, so the two variants cannot
        drift apart — only their failure contracts differ.
        """
        try:
            return self.is_pr_merged_strict(pr_number)
        except (VCSError, json.JSONDecodeError):
            logger.warning("Failed to check PR merge status", exc_info=True)
            return False

    def is_pr_merged_strict(self, pr_number: int) -> bool:
        """The merge read itself — handler-free, so failures propagate.

        The best-effort :meth:`is_pr_merged` above wraps this method and
        swallows the failure; here :class:`VCSError` from :meth:`_run` and
        ``json.JSONDecodeError`` from an unparseable payload both escape, so a
        caller wired to fall back on a raise (the merge-truth chokepoint's
        git-subject oracle) actually reaches its fallback rather than reading a
        laundered ``False`` as a confident host verdict.
        """
        result = self._run([
            "gh", "pr", "view", str(pr_number),
            *self._repo_args(),
            "--json", "state",
        ])
        data: dict[str, str] = json.loads(result.stdout)
        return data.get("state") == "MERGED"

    def has_merged_at(self, pr_number: int) -> bool:
        try:
            result = self._run([
                "gh", "pr", "view", str(pr_number),
                *self._repo_args(),
                "--json", "mergedAt",
            ])
            data: dict[str, str | None] = json.loads(result.stdout)
            return data.get("mergedAt") is not None
        except (VCSError, json.JSONDecodeError):
            logger.warning("Failed to read PR mergedAt", exc_info=True)
            return False

    def is_pr_closed(self, pr_number: int) -> bool:
        try:
            result = self._run([
                "gh", "pr", "view", str(pr_number),
                *self._repo_args(),
                "--json", "state",
            ])
            data: dict[str, str] = json.loads(result.stdout)
            return data.get("state") == "CLOSED"
        except (VCSError, json.JSONDecodeError):
            logger.warning("Failed to check PR closed status", exc_info=True)
            return False

    def get_issue_state(self, number: int) -> str | None:
        try:
            owner, name = self._validate_repo(self._repo)
            result = self._run([
                "gh", "api", f"repos/{owner}/{name}/issues/{number}",
                "--jq", ".state",
            ])
        except VCSError:
            logger.warning("Failed to fetch issue state", exc_info=True)
            return None
        state = result.stdout.strip()
        return state if state in ("open", "closed") else None

    def get_pr_base_branch(self, pr_number: int) -> str:
        try:
            result = self._run([
                "gh", "pr", "view", str(pr_number),
                *self._repo_args(),
                "--json", "baseRefName",
            ])
            data = json.loads(result.stdout)
            return str(data.get("baseRefName", ""))
        except (VCSError, json.JSONDecodeError):
            logger.warning("Failed to fetch PR base branch", exc_info=True)
            return ""

    def get_commit_sha_of_pr(self, pr_number: int) -> str:
        try:
            result = self._run([
                "gh", "pr", "view", str(pr_number),
                *self._repo_args(),
                "--json", "mergeCommit",
            ])
            data = json.loads(result.stdout)
            merge_commit = data.get("mergeCommit") or {}
            if not isinstance(merge_commit, dict):
                return ""
            return str(merge_commit.get("oid", ""))
        except (VCSError, json.JSONDecodeError):
            logger.warning("Failed to fetch PR merge commit", exc_info=True)
            return ""

    def get_pr_check_conclusion(self, pr_number: int) -> str:
        """Fetch the CI status-check rollup conclusion for a PR.

        Returns one of four values: ``"success"`` (all checks green),
        ``"failure"`` (at least one check failed), ``"none"`` (no checks
        configured for this repo/branch — proceed, do not wait for CI), or
        ``""`` (genuinely pending, unparseable, or a fetch error — defer and
        reprobe). ``"none"`` is held distinct from ``""`` so a no-CI
        deployment is not deferred forever (#2109). Best-effort.
        """
        try:
            result = self._run([
                "gh", "pr", "view", str(pr_number),
                *self._repo_args(),
                "--json", "statusCheckRollup",
            ])
            data = json.loads(result.stdout)
            rollup = data.get("statusCheckRollup")
            if not isinstance(rollup, list) or not rollup:
                # No checks defined for this repo/branch. Distinct from the
                # pending/error ``""`` sentinel so the caller can proceed
                # rather than defer forever (see #2109).
                return "none"
            values: list[str] = []
            for item in rollup:
                if not isinstance(item, dict):
                    continue
                # Advisory checks (e.g. the AI ``review`` verdict) are not CI
                # quality gates — skip them so a red review alone cannot block
                # the main-merge / conflict path (#2440).
                if _is_advisory_check(item):
                    continue
                values.append(str(item.get("conclusion") or item.get("state") or ""))
            if not values:
                return ""
            if any(is_failing_conclusion(v) for v in values):
                return "failure"
            if all(is_success_conclusion(v) for v in values):
                return "success"
            return ""
        except (VCSError, json.JSONDecodeError):
            logger.warning("Failed to fetch PR check conclusion", exc_info=True)
            return ""

    def get_ref_check_runs(self, sha: str) -> RefCheckRuns | None:
        if not self._repo or not _REF_SHA_RE.fullmatch(sha):
            return None
        try:
            owner, name = self._validate_repo(self._repo)
            result = self._run([
                "gh", "api",
                f"repos/{owner}/{name}/commits/{sha}/check-runs"
                "?filter=latest&per_page=100",
                "--paginate",
                "--jq", ".check_runs[] | {name,status,conclusion}",
            ])
            runs: list[CheckRun] = []
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict) or _is_advisory_check(item):
                    continue
                if rollup_item_name(item).strip().lower() in REF_NON_GATE_CHECK_NAMES:
                    continue
                value = str(item.get("conclusion") or item.get("status") or "")
                runs.append(CheckRun(str(item.get("name") or ""), value.lower()))
        except (VCSError, json.JSONDecodeError):
            logger.warning("Failed to read check runs for %s", sha, exc_info=True)
            return None
        return RefCheckRuns(sha=sha, runs=tuple(runs))

    def get_pr_failing_check_names(self, pr_number: int) -> list[str]:
        try:
            result = self._run([
                "gh", "pr", "view", str(pr_number),
                *self._repo_args(),
                "--json", "statusCheckRollup",
            ])
            data = json.loads(result.stdout)
            rollup = data.get("statusCheckRollup")
            if not isinstance(rollup, list):
                return []
            failing: list[str] = []
            for item in rollup:
                if not isinstance(item, dict) or _is_advisory_check(item):
                    continue
                value = str(item.get("conclusion") or item.get("state") or "")
                if is_failing_conclusion(value):
                    name = rollup_item_name(item).strip()
                    if name:
                        failing.append(name)
            return failing
        except (VCSError, json.JSONDecodeError):
            logger.warning(
                "Failed to fetch PR failing check names", exc_info=True,
            )
            return []

    def read_run_log_failed(self, pr_number: int) -> str:
        try:
            rollup_result = self._run([
                "gh", "pr", "view", str(pr_number),
                *self._repo_args(),
                "--json", "statusCheckRollup",
            ])
            rollup = json.loads(rollup_result.stdout).get("statusCheckRollup")
            if not isinstance(rollup, list):
                return ""
            run_id = _first_failing_run_id(rollup)
            if run_id is None:
                return ""
            log_result = self._run([
                "gh", "run", "view", run_id,
                *self._repo_args(),
                "--log-failed",
            ])
            # Keep the log's TAIL, not its head: gh run view --log-failed emits
            # the failing step's full output and the concrete failure summary (a
            # pytest FAILED/assertion block, a step's "Error: Process completed
            # …" line) lands at the END, so a head window would capture only
            # collection/progress noise and drop the real error. Redact BEFORE
            # slicing so a secret straddling the cut cannot leak a fragment; the
            # char cap then bounds what reaches the caller.
            return _redact_run_log(log_result.stdout)[-_RUN_LOG_MAX_CHARS:]
        except (VCSError, json.JSONDecodeError):
            logger.warning(
                "Failed to read PR failing run log", exc_info=True,
            )
            return ""

    def get_pr_diff(self, pr_number: int) -> str:
        try:
            result = self._run([
                "gh", "pr", "diff", str(pr_number),
                *self._repo_args(),
            ])
            return result.stdout
        except VCSError:
            logger.warning("Failed to fetch PR diff", exc_info=True)
            return ""

    def get_pr_info(self, pr_number: int) -> dict[str, str]:
        try:
            result = self._run([
                "gh", "pr", "view", str(pr_number),
                *self._repo_args(),
                "--json", "title,body",
            ])
            data = json.loads(result.stdout)
            return {
                "title": data.get("title", ""),
                "body": data.get("body", ""),
            }
        except (VCSError, json.JSONDecodeError):
            logger.warning("Failed to fetch PR info", exc_info=True)
            return {"title": "", "body": ""}

    def list_issues(
        self,
        *,
        state: str = "open",
        labels: list[str] | None = None,
        exclude_labels: list[str] | None = None,
        page_size: int | None = None,
        title_contains: str | None = None,
    ) -> list[dict[str, Any]] | None:
        if not self._repo:
            return None

        cmd: list[str] = [
            "gh", "issue", "list",
            "--repo", self._repo,
            "--state", state,
            "--json", "number,title,labels,body,createdAt",
        ]
        if labels:
            for label in labels:
                cmd.extend(["--label", label])
        terms: list[str] = []
        if title_contains:
            escaped = title_contains.replace('"', "")
            terms.append(f'"{escaped}" in:title')
        if exclude_labels:
            terms.extend(f'-label:"{lbl}"' for lbl in exclude_labels)
        if terms:
            cmd.extend(["--search", " ".join(terms)])
        if page_size is not None and page_size > 0:
            cmd.extend(["--limit", str(page_size)])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=self._timeout,
                cwd=self._cwd,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Failed to list issues: %s\nstderr: %s",
                exc,
                (exc.stderr or "").strip(),
            )
            return None
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Failed to list issues", exc_info=True)
            return None

        try:
            raw: list[dict[str, Any]] = json.loads(result.stdout)
            return raw
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from gh issue list")
            return None

    def _ensure_labels(self, labels: list[str]) -> None:
        """Best-effort: ensure each label exists in the repo. Never raises.

        No-ops when ``self._repo`` is unset, so the "never raises" contract
        holds regardless of optimization mode (including ``python -O``).
        """
        if not self._repo:
            return
        for label in labels:
            try:
                subprocess.run(
                    [
                        "gh", "label", "create", label,
                        "--repo", self._repo,
                        "--force",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=self._timeout,
                    cwd=self._cwd,
                )
            except subprocess.CalledProcessError as exc:
                logger.warning(
                    "Failed to ensure label %r: %s\nstderr: %s",
                    label,
                    exc,
                    (exc.stderr or "").strip(),
                )
            except (subprocess.TimeoutExpired, OSError):
                logger.warning(
                    "Failed to ensure label %r", label, exc_info=True,
                )

    def _run_create_issue(
        self, cmd: list[str], body: str,
    ) -> tuple[str | None, str | None]:
        """Run a `gh issue create` invocation with body on stdin.

        Returns ``(url, None)`` on success, ``(None, stderr)`` when the
        subprocess exits non-zero (surfacing the captured stderr so the caller
        can tell a missing-label failure from an unrelated one), and
        ``(None, None)`` for timeouts / OS errors, which are definitely not
        label-related.
        """
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=self._timeout,
                cwd=self._cwd,
                input=body,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            logger.warning(
                "Failed to create issue: %s\nstderr: %s",
                exc,
                stderr,
            )
            return None, stderr
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Failed to create issue", exc_info=True)
            return None, None
        return result.stdout.strip(), None

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> int | None:
        if not self._repo:
            return None

        cmd: list[str] = [
            "gh", "issue", "create",
            "--repo", self._repo,
            "--title", title,
            "--body-file", "-",
        ]
        if labels:
            for label in labels:
                cmd.extend(["--label", label])

        url, stderr = self._run_create_issue(cmd, body)
        if (
            url is None
            and labels
            and stderr is not None
            and _is_label_related_failure(stderr)
        ):
            logger.warning(
                "Retrying gh issue create after ensuring labels %s exist",
                labels,
            )
            self._ensure_labels(labels)
            url, _ = self._run_create_issue(cmd, body)
        if url is None:
            return None

        try:
            return int(url.rstrip("/").rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            logger.warning("Could not parse issue number from: %s", url)
            return None

    def _issue_state_change(self, number: int, verb: str) -> bool:
        if not self._repo:
            return False
        try:
            subprocess.run(
                [
                    "gh", "issue", verb, str(number),
                    "--repo", self._repo,
                ],
                check=True,
                timeout=self._timeout,
                capture_output=True,
                text=True,
                cwd=self._cwd,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Failed to %s issue #%d: %s\nstderr: %s",
                verb,
                number,
                exc,
                (exc.stderr or "").strip(),
            )
            return False
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Failed to %s issue #%d", verb, number, exc_info=True)
            return False
        return True

    def close_issue(self, number: int) -> bool:
        return self._issue_state_change(number, "close")

    def reopen_issue(self, number: int) -> bool:
        return self._issue_state_change(number, "reopen")

    def _issue_events(self, number: int) -> list[dict[str, Any]] | None:
        """Return issue *number*'s labeled/unlabeled/closed/reopened events, oldest first."""
        try:
            owner, name = self._validate_repo(self._repo)
            result = self._run([
                "gh", "api", f"repos/{owner}/{name}/issues/{number}/events",
                "--paginate",
                "--jq",
                '.[] | select(.event == "labeled" or .event == "unlabeled" '
                'or .event == "closed" or .event == "reopened") | {event, label: .label.name, '
                "actor: .actor.login, commit_id, created_at}",
            ])
            events: list[dict[str, Any]] = []
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if isinstance(row, dict):
                    events.append(row)
            return events
        except (VCSError, json.JSONDecodeError):
            logger.warning("Failed to read issue events", exc_info=True)
            return None

    def get_issue_label_actor(self, number: int, label: str) -> str | None:
        events = self._issue_events(number)
        if events is None:
            return None
        latest: dict[str, Any] | None = None
        for row in events:
            if row.get("event") in ("labeled", "unlabeled") and row.get("label") == label:
                latest = row
        if latest is None or latest.get("event") != "labeled":
            return None
        actor = latest.get("actor")
        return actor if isinstance(actor, str) and actor else None

    def get_issue_label_removed_at(self, number: int, label: str) -> LabelRemoval | None:
        events = self._issue_events(number)
        if events is None:
            return LabelRemoval("", readable=False)
        latest: dict[str, Any] | None = None
        for row in events:
            if row.get("event") in ("labeled", "unlabeled") and row.get("label") == label:
                latest = row
        if latest is None or latest.get("event") != "unlabeled":
            return None
        stamp = latest.get("created_at")
        if not isinstance(stamp, str) or not stamp:
            return None
        actor = latest.get("actor")
        return LabelRemoval(stamp, actor if isinstance(actor, str) else "")

    def _run_issue_label_edit(self, number: int, flag: str, label: str) -> str | None:
        """Run ``gh issue edit --<flag> <label>``; ``None`` on success, else stderr."""
        try:
            subprocess.run(
                [
                    "gh", "issue", "edit", str(number),
                    "--repo", str(self._repo),
                    flag, label,
                ],
                check=True,
                timeout=self._timeout,
                capture_output=True,
                text=True,
                cwd=self._cwd,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            logger.warning(
                "Failed to edit label %r on issue #%d: %s\nstderr: %s",
                label, number, exc, stderr,
            )
            return stderr
        except (subprocess.TimeoutExpired, OSError):
            logger.warning(
                "Failed to edit label %r on issue #%d", label, number, exc_info=True,
            )
            return ""
        return None

    def add_issue_label(self, number: int, label: str) -> bool:
        if not self._repo:
            return False
        stderr = self._run_issue_label_edit(number, "--add-label", label)
        if stderr and _is_label_related_failure(stderr):
            logger.warning("Retrying label add after ensuring %r exists", label)
            self._ensure_labels([label])
            stderr = self._run_issue_label_edit(number, "--add-label", label)
        return stderr is None

    def remove_issue_label(self, number: int, label: str) -> bool:
        if not self._repo:
            return False
        return self._run_issue_label_edit(number, "--remove-label", label) is None

    def get_issue_meta(self, number: int) -> IssueMeta | None:
        try:
            owner, name = self._validate_repo(self._repo)
            result = self._run([
                "gh", "api", f"repos/{owner}/{name}/issues/{number}",
                "--jq", "{state, title, labels: [.labels[].name]}",
            ])
            data = json.loads(result.stdout)
        except VCSError as exc:
            if re.search(r"HTTP (404|410)", str(exc)):
                return IssueMeta("gone")
            logger.warning("Failed to fetch issue #%d", number, exc_info=True)
            return None
        except json.JSONDecodeError:
            logger.warning("Failed to parse issue #%d", number, exc_info=True)
            return None
        if not isinstance(data, dict) or data.get("state") not in ("open", "closed"):
            return None
        labels = data.get("labels")
        return IssueMeta(
            state=str(data["state"]),
            title=str(data.get("title") or ""),
            labels=tuple(str(lbl) for lbl in labels) if isinstance(labels, list) else (),
        )

    def get_issue_closure(self, number: int) -> IssueClosure | None:
        events = self._issue_events(number)
        if events is None:
            return None
        history = tuple(
            (
                str(row["event"]),
                row["actor"] if isinstance(row.get("actor"), str) else "",
                bool(row.get("commit_id")),
            )
            for row in events
            if row.get("event") in ("closed", "reopened")
        )
        last_close = max(
            (i for i, row in enumerate(events) if row.get("event") == "closed"),
            default=None,
        )
        if last_close is None:
            return IssueClosure("", via_commit=False, closed=False)
        actor = events[last_close].get("actor")
        reopened_by = ""
        for row in events[last_close + 1:]:
            if row.get("event") == "reopened":
                who = row.get("actor")
                reopened_by = who if isinstance(who, str) else ""
        return IssueClosure(
            actor=actor if isinstance(actor, str) else "",
            via_commit=bool(events[last_close].get("commit_id")),
            reopened_by=reopened_by,
            history=history,
        )

    def comment_issue(self, number: int, body: str) -> bool:
        if not self._repo:
            return False
        try:
            subprocess.run(
                [
                    "gh", "issue", "comment", str(number),
                    "--repo", self._repo,
                    "--body-file", "-",
                ],
                check=True,
                timeout=self._timeout,
                capture_output=True,
                text=True,
                cwd=self._cwd,
                input=body,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Failed to comment on issue #%d: %s\nstderr: %s",
                number,
                exc,
                (exc.stderr or "").strip(),
            )
            return False
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("Failed to comment on issue #%d", number, exc_info=True)
            return False
        return True

    def get_open_evolution_pr_states(
        self, branch_prefix: str, *, strict: bool = False,
    ) -> dict[str, PrMergeState]:
        if not self._repo:
            if strict:
                raise VCSError(
                    "Cannot confirm open evolution PR state: no repo configured",
                )
            return {}
        cmd = [
            "gh", "pr", "list",
            "--repo", self._repo,
            "--state", "open",
            "--limit", "100",
            "--json", (
                "number,headRefName,title,body,state,mergeable,"
                "mergeStateStatus,reviewDecision,createdAt,updatedAt,url"
            ),
        ]
        try:
            result = self._run(cmd, timeout=self._timeout)
        except VCSError:
            if strict:
                raise
            logger.warning(
                "Failed to list evolution PRs for prefix %s", branch_prefix,
                exc_info=True,
            )
            return {}
        try:
            raw: list[dict[str, Any]] = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            if strict:
                raise VCSError("Invalid JSON from gh pr list") from exc
            logger.warning("Invalid JSON from gh pr list")
            return {}
        out: dict[str, PrMergeState] = {}
        for row in raw:
            head = str(row.get("headRefName", ""))
            if not head or not head.startswith(branch_prefix):
                continue
            out[head] = PrMergeState(
                number=int(row.get("number", 0)),
                head_ref=head,
                title=str(row.get("title", "")),
                body=str(row.get("body", "")),
                state=str(row.get("state", "")),
                mergeable=str(row.get("mergeable", "")),
                merge_state_status=str(row.get("mergeStateStatus", "")),
                review_decision=str(row.get("reviewDecision", "")),
                created_at=str(row.get("createdAt", "")),
                updated_at=str(row.get("updatedAt", "")),
                url=str(row.get("url", "")),
            )
        return out

    def find_evolution_pr_for_branch(self, branch: str) -> PrMergeState | None:
        cmd = [
            "gh", "pr", "list",
            *self._repo_args(),
            "--head", branch,
            "--state", "all",
            "--limit", "10",
            "--json", (
                "number,headRefName,title,body,state,mergeable,"
                "mergeStateStatus,reviewDecision,createdAt,updatedAt,url"
            ),
        ]
        result = self._run(cmd, timeout=self._timeout)
        try:
            raw: list[dict[str, Any]] = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise VCSError("Invalid JSON from gh pr list") from exc
        live = [
            row for row in raw
            if str(row.get("state", "")) in ("OPEN", "MERGED")
            and str(row.get("headRefName", "")) == branch
        ]
        if not live:
            return None
        row = max(live, key=lambda r: int(r.get("number", 0)))
        return PrMergeState(
            number=int(row.get("number", 0)),
            head_ref=branch,
            title=str(row.get("title", "")),
            body=str(row.get("body", "")),
            state=str(row.get("state", "")),
            mergeable=str(row.get("mergeable", "")),
            merge_state_status=str(row.get("mergeStateStatus", "")),
            review_decision=str(row.get("reviewDecision", "")),
            created_at=str(row.get("createdAt", "")),
            updated_at=str(row.get("updatedAt", "")),
            url=str(row.get("url", "")),
        )

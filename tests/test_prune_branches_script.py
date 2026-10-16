"""Static-text pins for ``scripts/prune-branches.sh`` (proposal 065 step 4).

``scripts/prune-branches.sh`` centralises the candidate-detection-plus-safe-delete
plumbing the ``prune-branches`` slash command previously restated in prose:
capture the protected set (current branch, ``main``/``master``/``develop``,
worktree-pinned branches), ``git fetch --all --prune``, enumerate
``[gone]``-upstream branches via ``%(upstream:track)``, subtract the protected
set, print the plan, and run the safe ``git branch -d`` pass — collecting the
``-d``-refused (unmerged) remainder separately for the command to drive the
force-delete judgment, which stays in prose.

Following the shell-script testing shape established by
``tests/test_quality_gate_hook_changed_files.py``, ``tests/test_preflight_script.py``,
``tests/test_git_push_tracked_script.py``, and ``tests/test_pr_url_script.py``,
these are deliberately *static-text* pins, not behavioral tests: exercising the
script would require running it as a real subprocess against a real git tree,
which the unit suite forbids (see "Testing Principles" in ``CLAUDE.md``). The
pins catch the silent-drop / dangerous-form regression class — a future edit
dropping a protected name, the ``[gone]`` detector, the fetch or safe-delete
command, the ``--dry-run`` early exit, or (worst) sneaking a ``-D`` force-delete
onto a ``git branch`` line — and leave behavioral correctness (safe-deleting the
merged branch, listing the unmerged one as refused, never touching a
protected/worktree branch, deleting nothing under ``--dry-run``) to manual
review.

The ``-d``-vs-``-D`` pin uses **line-level enumeration**, not a bare substring
check: a ``"git branch -d" in content`` assertion stays true even when a
``git branch -D`` line is added alongside the safe ``-d`` pass, so it cannot
catch a ``-d``→``-D`` escalation. Collecting every non-comment line containing
``git branch`` and asserting none carries ``-D`` verifies *exclusivity*, and the
comment-line filter keeps the self-documenting safety comment (which mentions a
force delete in prose) from interfering.
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPT_FILE = (
    Path(__file__).resolve().parent.parent / "scripts" / "prune-branches.sh"
)

# The protected long-lived branches that are never deletion candidates
# regardless of ``[gone]`` status. Pinned as the exact ``for name in …`` token so
# a future edit cannot silently drop one.
_PROTECTED_LONG_LIVED = "main master develop"

# The current-branch probe and the entry that adds it to the protected set, so
# the checked-out branch can never be pruned.
_CURRENT_BRANCH_PROBE = "git rev-parse --abbrev-ref HEAD"
_CURRENT_BRANCH_PROTECT = 'protected["$current_branch"]'

# Worktree-pinned-branch detection: enumerate worktrees and protect every branch
# one has checked out. Pinned together so dropping either leaves a worktree
# branch prunable.
_WORKTREE_LIST = "git worktree list --porcelain"
_WORKTREE_BRANCH_PREFIX = "branch refs/heads/"

# The ``[gone]``-upstream detector: the ``%(upstream:track)`` for-each-ref format
# and the exact ``[gone]`` comparison that selects candidates.
_UPSTREAM_TRACK_FORMAT = "%(upstream:track)"
_GONE_MARKER = "[gone]"

# The fetch that clears local remote-tracking refs whose origin branches are gone
# — the ``[gone]`` detection is invalid without it.
_FETCH_COMMAND = "git fetch --all --prune"

# The soft-token opt-in flag the prune fetch must pass to ``with-env.sh`` (fix
# #1536): the prune path is a purely-local cleanup whose only network step is
# this fetch, so a missing GITHUB_TOKEN must not block an origin fetchable without
# one (SSH, public HTTPS, a credential helper). Pinned so a future edit cannot
# silently re-harden the prune path by dropping the flag.
_OPTIONAL_TOKEN_FLAG = "--optional-token"

# The ``--dry-run`` flag and the guard that stops before the safe-delete pass.
_DRY_RUN_FLAG = "--dry-run"
_DRY_RUN_GUARD = '"$dry_run" == true'

# The safe-delete form. ``-D`` (force-delete) must never appear on a
# ``git branch`` line.
_SAFE_DELETE_FORM = "git branch -d"
_FORCE_DELETE_TOKEN = "-D"

# Protected ``[gone]`` branches are surfaced, not silently dropped: collected into
# ``protected_gone`` and printed under a ``protected (excluded …)`` plan line so
# the command's report can attribute them as skipped (current / main-master-develop
# / worktree).
_PROTECTED_GONE_COLLECTION = "protected_gone"
_PROTECTED_EXCLUDED_LABEL = "protected (excluded"

# The user-facing merged status is measured against the resolved *trunk*, never
# the checked-out ``HEAD``. Issue #1535: run from a feature branch,
# ``git branch --merged HEAD`` reports a candidate already merged into the trunk
# (but not into the current branch) as "NOT merged", nudging the user toward an
# unnecessary force-delete. The probe and the displayed labels must name the
# trunk, so the status answers "is this work on the trunk?" — not "is it on the
# branch I happen to have checked out?".
_TRUNK_MERGED_PROBE = 'git branch --merged "$trunk"'
_HEAD_MERGED_PROBE = "git branch --merged HEAD"
_TRUNK_STATUS_MERGED = "merged into $trunk"
_TRUNK_STATUS_UNMERGED = "NOT merged into $trunk"
_HEAD_STATUS_LABEL = "merged into HEAD"

# The fail-closed trunk-resolution chain: the recorded ``origin/HEAD`` symbolic
# ref, then the first existing of ``origin/{main,master,develop}``, and only in a
# trunk-less checkout the literal ``HEAD`` — surfaced in the plan so the degrade
# is visible rather than silently reverting to the old ambiguous wording.
_TRUNK_ORIGIN_HEAD_PROBE = "refs/remotes/origin/HEAD"
_TRUNK_LESS_FALLBACK_LABEL = "no trunk ref resolved"

# ``git symbolic-ref`` reads only the symref's *content* — it succeeds even when
# the branch it names is gone. The ``origin/HEAD`` resolution must therefore
# existence-validate its target with the same ``git rev-parse --verify --quiet``
# guard the ``origin/{main,master,develop}`` fallbacks use, or a dangling
# ``origin/HEAD`` (left after a renamed remote default is pruned) sets ``trunk``
# to a missing ref, skips the fallback chain, and mislabels every candidate.
_TRUNK_ORIGIN_HEAD_EXISTENCE_GUARD = 'git rev-parse --verify --quiet "$trunk"'


def _read() -> str:
    return _SCRIPT_FILE.read_text(encoding="utf-8")


def _git_branch_lines(content: str) -> list[str]:
    """Every non-comment line that runs a ``git branch`` command.

    The ``^(?!\\s*#)`` filter drops comment lines so the header's safety prose
    (which describes the force-delete it refuses) cannot trip the ``-D`` pin, and
    ``\\bgit branch\\b`` matches the command rather than the word in prose.
    """
    return re.findall(r"(?m)^(?!\s*#).*\bgit branch\b.*$", content)


def test_protected_set_is_pinned() -> None:
    """The protected set covers current branch, main/master/develop, worktrees.

    Pinned together so a future edit cannot drop a protected name and start
    treating a long-lived or checked-out branch as a deletion candidate.
    """
    content = _read()
    assert _CURRENT_BRANCH_PROBE in content, (
        f"scripts/prune-branches.sh must determine the current branch via "
        f"{_CURRENT_BRANCH_PROBE!r} so it is never pruned."
    )
    assert _CURRENT_BRANCH_PROTECT in content, (
        f"scripts/prune-branches.sh must add the current branch to the protected "
        f"set ({_CURRENT_BRANCH_PROTECT}) so the checked-out branch is never a "
        f"deletion candidate."
    )
    assert _PROTECTED_LONG_LIVED in content, (
        f"scripts/prune-branches.sh must protect the long-lived branches "
        f"{_PROTECTED_LONG_LIVED!r}; dropping one would make it prunable when its "
        f"upstream is gone."
    )
    assert _WORKTREE_LIST in content, (
        f"scripts/prune-branches.sh must enumerate worktrees via "
        f"{_WORKTREE_LIST!r} so a branch checked out in a worktree is protected."
    )
    assert _WORKTREE_BRANCH_PREFIX in content, (
        f"scripts/prune-branches.sh must parse worktree branch refs "
        f"({_WORKTREE_BRANCH_PREFIX!r}) so worktree-pinned branches are added to "
        f"the protected set."
    )


def test_gone_upstream_detector_is_pinned() -> None:
    """Candidates are detected by the ``%(upstream:track)`` ``[gone]`` marker.

    Pinned so a future edit cannot change the for-each-ref format or the
    ``[gone]`` comparison and silently start selecting (or missing) the wrong
    branches.
    """
    content = _read()
    assert _UPSTREAM_TRACK_FORMAT in content, (
        f"scripts/prune-branches.sh must enumerate upstream-track state via "
        f"{_UPSTREAM_TRACK_FORMAT!r}; the [gone] detection keys on it."
    )
    assert _GONE_MARKER in content, (
        f"scripts/prune-branches.sh must select candidates by the {_GONE_MARKER!r} "
        f"upstream-track marker."
    )


def test_fetch_prune_is_present_and_routed_through_with_env() -> None:
    """The fetch runs ``git fetch --all --prune`` through ``with-env.sh``.

    Pinned so a future edit cannot drop the prune (leaving ``[gone]`` detection
    stale) or drop the token-loading wrapper (leaving the fetch to fail on auth in
    a fresh shell). The prune path is fetch-only, so the wrapper is invoked in
    SOFT-token mode (``--optional-token``, fix #1536): the flag must appear
    *between* ``with-env.sh`` and the fetch on that one line, so a missing
    GITHUB_TOKEN no longer blocks an origin fetchable without one. Enumerates the
    actual fetch line rather than scanning the whole file so the header's mention
    of ``with-env.sh`` cannot stand in for the real routing.
    """
    content = _read()
    # The line that actually invokes the fetch must route it through with-env.sh
    # in soft-token mode. Requiring --optional-token between the wrapper and the
    # fetch on one non-comment line ignores the diagnostic `echo` that names the
    # command in an error string and the header's prose mention, so neither can
    # stand in for the real, soft-token invocation — and captures the issue's bad
    # state (the flag absent) so the prune path cannot be silently re-hardened.
    routed = re.findall(
        r"(?m)^(?!\s*#).*with-env\.sh.*"
        + re.escape(_OPTIONAL_TOKEN_FLAG)
        + r".*\bgit fetch --all --prune\b.*$",
        content,
    )
    assert routed, (
        f"scripts/prune-branches.sh must invoke {_FETCH_COMMAND!r} through "
        f"scripts/with-env.sh in soft-token mode ({_OPTIONAL_TOKEN_FLAG!r}) so the "
        f"fetch-only prune runs on a fresh prune fetch without hard-requiring a "
        f"token; dropping the prune leaves detection stale, dropping the wrapper "
        f"leaves the fetch to fail on auth in a fresh shell, and dropping "
        f"{_OPTIONAL_TOKEN_FLAG!r} re-hardens the prune path against tokenless "
        f"origins (SSH, public HTTPS, a credential helper)."
    )


def test_dry_run_early_exit_precedes_the_delete_pass() -> None:
    """``--dry-run`` stops before the safe-delete pass.

    Pin the flag, the guard, and the *ordering*: the ``--dry-run`` exit must come
    before the ``git branch -d`` pass, so a dry run can never delete a branch.
    """
    content = _read()
    assert _DRY_RUN_FLAG in content, (
        f"scripts/prune-branches.sh must recognise the {_DRY_RUN_FLAG!r} flag."
    )
    guard_pos = content.find(_DRY_RUN_GUARD)
    assert guard_pos != -1, (
        f"scripts/prune-branches.sh must guard the early exit on {_DRY_RUN_GUARD!r}."
    )
    # Locate the actual (non-comment) safe-delete command, not the header's prose
    # mention of it — the comment appears first in the file and would invert the
    # ordering check.
    delete_match = re.search(r"(?m)^(?!\s*#).*\bgit branch -d\b", content)
    assert delete_match, (
        f"scripts/prune-branches.sh must run the safe-delete pass "
        f"({_SAFE_DELETE_FORM!r})."
    )
    delete_pos = delete_match.start()
    assert guard_pos < delete_pos, (
        "scripts/prune-branches.sh must place the --dry-run guard BEFORE the "
        "`git branch -d` pass so a dry run never deletes a branch."
    )
    # An `exit` must sit between the dry-run guard and the delete pass, so the
    # guarded branch actually short-circuits rather than falling through.
    between = content[guard_pos:delete_pos]
    assert re.search(r"(?m)^(?!\s*#).*\bexit\b", between), (
        "scripts/prune-branches.sh --dry-run guard must `exit` before the "
        "safe-delete pass rather than falling through into it."
    )


def test_safe_delete_form_is_present() -> None:
    """At least one ``git branch`` line runs the safe ``-d`` delete.

    Pinned so a future edit cannot drop the delete pass entirely and turn the
    helper into a no-op that only ever prints the plan.
    """
    content = _read()
    branch_lines = _git_branch_lines(content)
    assert branch_lines, (
        "scripts/prune-branches.sh has no `git branch` line — the helper must "
        "actually run the safe-delete pass."
    )
    assert any(_SAFE_DELETE_FORM in line for line in branch_lines), (
        f"scripts/prune-branches.sh must run the safe-delete pass "
        f"({_SAFE_DELETE_FORM!r}); without it the helper never prunes."
    )


def test_no_force_delete_on_any_git_branch_line() -> None:
    """No ``git branch`` line carries ``-D`` (force delete).

    The exclusivity safety pin. ``git branch -d`` refuses unmerged branches by
    design; force-deletion is the command's judgment, never this script's. A bare
    ``"git branch -d" in content`` check stays true even with a ``git branch -D``
    line added alongside it, so it cannot catch a ``-d``→``-D`` escalation —
    line-level enumeration verifies that *no* ``git branch`` line force-deletes.
    """
    content = _read()
    for line in _git_branch_lines(content):
        assert _FORCE_DELETE_TOKEN not in line, (
            f"scripts/prune-branches.sh git branch line {line.strip()!r} carries "
            f"{_FORCE_DELETE_TOKEN!r}; the helper must never force-delete — the "
            f"unmerged remainder is handed to the command for its judgment."
        )


def test_protected_gone_branches_are_surfaced_not_dropped() -> None:
    """``[gone]`` branches kept off the candidate list are reported, not dropped.

    A ``[gone]``-upstream branch that is protected (current / main-master-develop
    / worktree) is collected into ``protected_gone`` and printed under a
    ``protected (excluded …)`` plan line, so the command's report can attribute it
    as skipped. Pinned so a future edit cannot revert to silently discarding
    protected ``[gone]`` branches, which would leave the command unable to source
    its skipped-branch report from the helper's output.
    """
    content = _read()
    assert _PROTECTED_GONE_COLLECTION in content, (
        f"scripts/prune-branches.sh must collect protected [gone] branches into "
        f"{_PROTECTED_GONE_COLLECTION!r} rather than dropping them, so the command "
        f"can surface them as skipped."
    )
    assert _PROTECTED_EXCLUDED_LABEL in content, (
        f"scripts/prune-branches.sh must print a {_PROTECTED_EXCLUDED_LABEL!r}…) "
        f"plan line naming the protected [gone] branches it excluded, which the "
        f"command's report step sources for its skipped-branch list."
    )


def test_merged_status_is_measured_against_the_trunk_not_head() -> None:
    """The merged set and status labels name the resolved trunk, never ``HEAD``.

    Regression pin for #1535: run from a feature branch, ``git branch --merged
    HEAD`` marks a candidate already merged into the trunk (but not into the
    checked-out branch) as "NOT merged", nudging the user toward an unnecessary
    force-delete of work that is in fact preserved on the trunk. The probe must
    query the trunk ref and the displayed labels must name it, so the merged
    status answers "is this work on the trunk?" rather than "is it on the branch
    I happen to have checked out?". Against the pre-fix script this fails (the
    text is ``git branch --merged HEAD`` and ``merged into HEAD``).
    """
    content = _read()
    assert _TRUNK_MERGED_PROBE in content, (
        f"scripts/prune-branches.sh must compute the merged set against the "
        f"resolved trunk ref ({_TRUNK_MERGED_PROBE!r}), not the checked-out HEAD."
    )
    assert _HEAD_MERGED_PROBE not in content, (
        f"scripts/prune-branches.sh must not probe {_HEAD_MERGED_PROBE!r}: run "
        f"from a feature branch it falsely reports a trunk-merged branch as "
        f"unmerged (issue #1535)."
    )
    assert _TRUNK_STATUS_MERGED in content, (
        f"scripts/prune-branches.sh must label a merged candidate "
        f"{_TRUNK_STATUS_MERGED!r} so the displayed status names the trunk it was "
        f"measured against."
    )
    assert _TRUNK_STATUS_UNMERGED in content, (
        f"scripts/prune-branches.sh must label an unmerged candidate "
        f"{_TRUNK_STATUS_UNMERGED!r} so the displayed status names the trunk it "
        f"was measured against."
    )
    assert _HEAD_STATUS_LABEL not in content, (
        f"scripts/prune-branches.sh must not display {_HEAD_STATUS_LABEL!r}: the "
        f"user-facing merged status is trunk-relative, not HEAD-relative (#1535)."
    )


def test_trunk_ref_resolution_is_fail_closed_and_surfaced() -> None:
    """The trunk ref resolves from origin, falling back to ``HEAD`` only if none.

    Pins the fail-closed fallback chain: the recorded ``origin/HEAD`` symbolic
    ref resolved first, and — only in a trunk-less checkout — the literal
    ``HEAD``, whose degrade is surfaced in the plan output rather than silently
    reverting to the old ambiguous HEAD-relative wording.
    """
    content = _read()
    assert _TRUNK_ORIGIN_HEAD_PROBE in content, (
        f"scripts/prune-branches.sh must resolve the trunk from "
        f"{_TRUNK_ORIGIN_HEAD_PROBE!r} (the recorded default branch) before "
        f"falling back to the origin/{{main,master,develop}} probes."
    )
    assert _TRUNK_LESS_FALLBACK_LABEL in content, (
        f"scripts/prune-branches.sh must surface the trunk-less HEAD fallback in "
        f"the plan output ({_TRUNK_LESS_FALLBACK_LABEL!r}) rather than silently "
        f"reverting to HEAD-relative wording when no trunk ref resolves."
    )


def test_origin_head_trunk_probe_is_existence_validated() -> None:
    """The ``origin/HEAD`` symbolic-ref resolution validates its target exists.

    Regression pin for the dangling-``origin/HEAD`` mislabelling. ``git
    symbolic-ref --quiet --short refs/remotes/origin/HEAD`` reads only the
    symref's *content* and succeeds even when the branch it names is gone. Right
    after the script's own ``git fetch --all --prune``, a renamed remote default
    leaves ``refs/remotes/origin/HEAD`` dangling — the prune drops the old
    ``origin/<name>`` but does not repoint ``origin/HEAD``. Unless the probe
    existence-validates its target with the same ``git rev-parse --verify
    --quiet`` guard the ``origin/{main,master,develop}`` fallbacks use, ``trunk``
    is set to a missing ref, the fallback chain is skipped, ``git branch --merged
    "$trunk"`` fails, and every candidate is mislabelled "NOT merged" — the exact
    false-unmerged bug (#1535) this resolution exists to prevent. The guard must
    sit on the same non-comment line as the ``origin/HEAD`` probe so a dangling
    symref falls through to the fallback chain rather than being accepted; against
    the pre-fix script that line lacks the guard and this fails.
    """
    content = _read()
    origin_head_lines = [
        line
        for line in re.findall(r"(?m)^(?!\s*#).*$", content)
        if _TRUNK_ORIGIN_HEAD_PROBE in line
    ]
    assert origin_head_lines, (
        f"scripts/prune-branches.sh must resolve the trunk from "
        f"{_TRUNK_ORIGIN_HEAD_PROBE!r} on a non-comment line."
    )
    for line in origin_head_lines:
        assert _TRUNK_ORIGIN_HEAD_EXISTENCE_GUARD in line, (
            f"scripts/prune-branches.sh origin/HEAD resolution line "
            f"{line.strip()!r} must existence-validate its target with "
            f"{_TRUNK_ORIGIN_HEAD_EXISTENCE_GUARD!r} on the SAME line; "
            f"git symbolic-ref succeeds on a dangling origin/HEAD, so without the "
            f"guard a renamed remote default sets trunk to a missing ref, skips "
            f"the fallback chain, and mislabels every candidate 'NOT merged' "
            f"(#1535)."
        )

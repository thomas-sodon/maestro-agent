"""Regression: the Stop hook must route gate output to stderr (issue #164).

Claude Code's Stop hook reads diagnostic feedback off stderr, so if gate output
lands on stdout the agent never sees the failure detail and exits as if the gate
passed. Issue #164 documents the bug.

The hook (``.claude/quality-gate-hook.sh``) now delegates the gates to
``scripts/quality-gates.sh changed`` and folds that runner's stdout onto stderr
with a single ``>&2``. Two checks keep #164 pinned:

* The reversed redirect pair ``2>&1 >&2`` must not appear anywhere in the hook.
  Bash processes redirects strictly left-to-right, so ``cmd 2>&1 >&2`` lands both
  streams on the *original stdout* (the buggy form): step 1 dups fd1 onto fd2,
  then step 2 dups fd2 onto fd1, so both end up on stdout. ``cmd >&2`` instead
  dups stderr onto fd1, routing the stream to stderr.
* The ``quality-gates.sh changed`` runner invocation must carry ``>&2`` so the
  script's stdout is folded onto stderr where the agent can read it. A
  hand-rolled edit that drops the redirect would silently swallow gate output.

Scope note: like the sibling ``test_quality_gate_hook_changed_files`` module,
these are static-text pins on the hook's redirect wiring, not behavioral tests
of the shell runner — exercising the runner's stdout/stderr routing at runtime
would need a real subprocess, which the unit suite forbids (see "Testing
Principles" in ``CLAUDE.md``).
"""

from __future__ import annotations

import re
from pathlib import Path

_HOOK_FILE = (
    Path(__file__).resolve().parent.parent / ".claude" / "quality-gate-hook.sh"
)

_BUGGY = "2>&1 >&2"

# The hook delegates the gates to this runner; its output must be redirected to
# stderr. Matches e.g. ``./scripts/quality-gates.sh changed >&2``.
_RUNNER_TO_STDERR = re.compile(r"quality-gates\.sh\s+changed\b[^\n]*>&2")


def test_buggy_redirect_pair_absent_anywhere_in_hook() -> None:
    """The reversed redirect order must not appear in the hook file at all.

    Whole-file scan rather than a per-line check: a future invocation whose
    shape we don't anticipate would slip past a per-line assertion and silently
    re-introduce the #164 defect.
    """
    content = _HOOK_FILE.read_text(encoding="utf-8")
    assert _BUGGY not in content, (
        f"Buggy redirect order {_BUGGY!r} found in {_HOOK_FILE.name}. "
        f"Bash processes redirects left-to-right, so this lands gate output on "
        f"stdout; Claude Code's Stop hook reads stderr for feedback, so the "
        f"agent would never see the failure detail."
    )


def test_runner_invocation_redirects_to_stderr() -> None:
    """The ``quality-gates.sh changed`` call must fold its output onto stderr.

    This is the mechanism that replaced the old per-line ``>&2 2>&1`` redirects:
    the hook now runs the shared runner and redirects the whole invocation with
    ``>&2``. Dropping that redirect would route gate diagnostics to stdout,
    re-introducing #164.
    """
    content = _HOOK_FILE.read_text(encoding="utf-8")
    assert _RUNNER_TO_STDERR.search(content), (
        "Hook does not redirect the 'quality-gates.sh changed' runner to "
        "stderr (expected '>&2' on the invocation). Without it, gate output "
        "lands on stdout and the Stop hook (#164) never surfaces failures to "
        "the agent."
    )

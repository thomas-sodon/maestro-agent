"""Static-text pins for ``scripts/pr-url.sh`` (proposal 065 step 3).

``scripts/pr-url.sh`` centralises the PR-URL lookup the ``commit-push`` slash
command previously restated in prose: after pushing, run
``gh pr view --json url --jq .url`` and treat a non-zero exit ("no pull requests
found for this branch") as "no PR to link" — empty stdout, exit 0 — rather than a
surfaced error.

Following the shell-script testing shape established by
``tests/test_quality_gate_hook_changed_files.py``,
``tests/test_preflight_script.py``, and ``tests/test_git_push_tracked_script.py``,
these are deliberately *static-text* pins, not behavioral tests: exercising the
script would require running it as a real subprocess against a real git tree and
a live ``gh``, which the unit suite forbids (see "Testing Principles" in
``CLAUDE.md``). The pins catch the silent-drop / contract-break regression class —
a future edit changing the lookup query, dropping the ``with-env.sh`` token
routing, surfacing gh's "no pull requests found" message instead of swallowing
it, or collapsing the failure handling back to a blanket ``2>/dev/null`` +
unconditional ``exit 0`` that masks a genuine gh/auth/network failure as "no PR"
— and leave behavioral correctness (the captured URL on success, empty stdout on
no PR, a surfaced error on genuine failure) to manual review.

The exit-code pins use **line-level enumeration**, not a bare substring check, so
the header's contract description (which spells out both ``exit 0`` and a
non-zero exit) cannot stand in for a real ``exit``. The contract has two halves:
the success and no-PR paths ``exit 0`` (neither is a surfaced failure), while a
genuine gh/auth/network failure exits non-zero and re-emits gh's stderr so a real
error is surfaced rather than masked as "no PR". The no-PR swallow is keyed on
gh's specific message, not on discarding all stderr — discarding stderr
unconditionally is exactly what would re-mask genuine failures.
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPT_FILE = Path(__file__).resolve().parent.parent / "scripts" / "pr-url.sh"

# The exact PR-URL lookup the helper centralises (``commit-push`` restated it
# inline). Pinned so a future edit cannot change the query shape and silently
# start returning a different field or output format.
_PR_VIEW_INVOCATION = "gh pr view --json url --jq .url"

# The lookup is captured in an ``if`` condition so gh's non-zero "no PR" exit is
# branched on (empty stdout) rather than aborting; ``re.DOTALL`` lets the match
# span the command substitution. Group 1 is the captured variable name, reused to
# pin that the same variable is the one printed on success.
_CONDITIONAL_LOOKUP_RE = re.compile(
    r"if\s+(\w+)=\"?\$\(.*?" + re.escape(_PR_VIEW_INVOCATION) + r".*?\)",
    re.DOTALL,
)


def _read() -> str:
    return _SCRIPT_FILE.read_text(encoding="utf-8")


def _gh_lookup_lines(content: str) -> list[str]:
    """Every non-comment line that runs the ``gh pr view`` lookup.

    The ``^(?!\\s*#)`` filter drops comment lines so the header's description of
    the lookup cannot stand in for the real invocation.
    """
    return re.findall(r"(?m)^(?!\s*#).*\bgh pr view\b.*$", content)


def _exit_lines(content: str) -> list[str]:
    """Every non-comment line containing an ``exit`` statement.

    The comment-line filter keeps the header's contract description (which spells
    out ``exit 0``) from being counted as a real exit.
    """
    return re.findall(r"(?m)^(?!\s*#).*\bexit\b.*$", content)


def test_pr_view_invocation_is_present() -> None:
    """The script runs exactly ``gh pr view --json url --jq .url``.

    Pinned so a future edit cannot change the query (e.g. drop ``--jq .url`` and
    start emitting raw JSON, or look up a different field) and silently break the
    URL the caller captures.
    """
    content = _read()
    lines = _gh_lookup_lines(content)
    assert lines, (
        "scripts/pr-url.sh has no `gh pr view` line — the helper must actually "
        "look up the PR."
    )
    assert any(_PR_VIEW_INVOCATION in line for line in lines), (
        f"scripts/pr-url.sh must run {_PR_VIEW_INVOCATION!r}; the captured URL "
        f"contract depends on the `--json url --jq .url` query shape."
    )


def test_lookup_is_routed_through_with_env() -> None:
    """The lookup runs through ``with-env.sh`` so GITHUB_TOKEN is loaded.

    Pinned so a future edit cannot drop the token-loading wrapper and leave
    ``gh`` to fail on auth in a fresh shell (which the no-PR fallback would then
    misreport as "no PR").

    Enumerates the real ``gh pr view`` invocation line(s) rather than scanning
    the whole file: the header comment mentions ``scripts/with-env.sh``, so a
    bare ``"with-env.sh" in content`` check would still pass if a future edit
    dropped the wrapper from the *actual* invocation — the exact silent-drop
    this test exists to catch. The same comment-line filter the other pins use
    keeps the header description from standing in for the real routing.
    """
    content = _read()
    lines = _gh_lookup_lines(content)
    assert lines, (
        "scripts/pr-url.sh has no `gh pr view` line — the helper must actually "
        "look up the PR."
    )
    for line in lines:
        assert "with-env.sh" in line, (
            f"scripts/pr-url.sh gh lookup line {line.strip()!r} must route "
            f"through scripts/with-env.sh so GITHUB_TOKEN is loaded before "
            f"calling gh."
        )


def test_no_pr_case_is_swallowed_by_message_match() -> None:
    """The no-PR case is swallowed by matching gh's message, not by discarding
    all stderr.

    ``gh pr view`` exits non-zero and prints "no pull requests found ..." when the
    branch has no PR. The lookup is captured in an ``if`` condition (so the
    non-zero exit is branched on, not fatal); gh's stderr is captured rather than
    discarded with ``2>/dev/null``, and *only* the "no pull requests found"
    message is swallowed (empty stdout, exit 0). A blanket ``2>/dev/null`` would
    re-mask a genuine gh/auth/network failure as "no PR".
    """
    content = _read()
    assert _CONDITIONAL_LOOKUP_RE.search(content), (
        "scripts/pr-url.sh must capture the gh lookup in an `if VAR=$(...)` "
        "condition so a non-zero 'no PR' exit is branched on (empty stdout) "
        "rather than aborting the script."
    )
    for line in _gh_lookup_lines(content):
        assert "2>/dev/null" not in line, (
            f"scripts/pr-url.sh gh lookup line {line.strip()!r} must NOT blanket-"
            f"discard stderr with 2>/dev/null: that masks a genuine gh/auth/"
            f"network failure as 'no PR'. Capture stderr and swallow only the "
            f"'no pull requests found' message instead."
        )
    assert re.search(r"(?m)^(?!\s*#).*\bgrep\b.*no pull requests found", content), (
        "scripts/pr-url.sh must grep gh's captured stderr for the "
        "'no pull requests found' message so only that expected condition is "
        "swallowed as 'no PR' and genuine failures stay visible."
    )


def test_url_is_printed_only_on_lookup_success() -> None:
    """On success the captured URL is printed; on failure nothing is.

    The print references the same variable the ``if`` condition captures, so it
    runs only when the lookup succeeded — giving the captured-URL-on-success half
    of the contract while the failure branch leaves stdout empty.
    """
    content = _read()
    match = _CONDITIONAL_LOOKUP_RE.search(content)
    assert match, (
        "scripts/pr-url.sh must capture the gh lookup into a variable in an `if` "
        "condition (see test_no_pr_error_is_not_surfaced)."
    )
    var = match.group(1)
    assert re.search(rf'(?:printf|echo)[^\n]*"\${var}"', content), (
        f"scripts/pr-url.sh must print the captured URL variable (${var}) on the "
        f"success branch so an existing PR's URL reaches stdout."
    )


def test_success_and_no_pr_paths_exit_zero() -> None:
    """The success and no-PR paths ``exit 0`` — neither is a surfaced failure.

    A found PR prints its URL and exits 0; the swallowed no-PR case prints
    nothing and exits 0. At least one non-comment ``exit 0`` must remain so those
    two expected paths never surface as a failure. (The comment-line filter keeps
    the header's contract description, which spells out ``exit 0``, from standing
    in for a real exit.)
    """
    content = _read()
    lines = _exit_lines(content)
    assert any(line.strip() == "exit 0" for line in lines), (
        "scripts/pr-url.sh must `exit 0` on the success and no-PR paths so "
        "neither is surfaced as a failure."
    )


def test_genuine_failure_exits_nonzero_and_reemits_stderr() -> None:
    """A genuine failure is surfaced: non-zero exit, stderr re-emitted.

    Unlike the no-PR case, a gh/auth/network failure (or with-env.sh's own
    ".env not found" / "GITHUB_TOKEN is empty" diagnostics) must NOT be swallowed:
    the script re-emits the captured stderr to fd 2 and exits non-zero so the
    caller can surface a real error rather than misreport it as "no PR". This is
    the fail-loud half of the contract that a blanket ``2>/dev/null`` +
    unconditional ``exit 0`` would erase. Line-level enumeration keeps the
    header's contract description from standing in for the real exit/re-emit.
    """
    content = _read()
    assert re.search(r"(?m)^(?!\s*#).*\bexit\s+[1-9]", content), (
        "scripts/pr-url.sh must exit non-zero on a genuine failure; a script "
        "that only ever `exit 0` masks gh/auth/network errors as 'no PR'."
    )
    assert re.search(r"(?m)^(?!\s*#).*>&2", content), (
        "scripts/pr-url.sh must re-emit the captured stderr (>&2) on a genuine "
        "failure so the caller sees the real error rather than empty 'no PR'."
    )

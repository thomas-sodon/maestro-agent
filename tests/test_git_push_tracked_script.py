"""Static-text pins for ``scripts/git-push-tracked.sh`` (proposal 065 step 2).

``scripts/git-push-tracked.sh`` centralises the push plumbing the ``commit-push``,
``create-pr``, and ``merge-main`` slash commands previously restated in prose: it
picks ``git push`` when the branch already tracks a remote and
``git push -u origin HEAD`` to set tracking on the first push, and signals a
non-fast-forward rejection with a distinct reserved exit code so the caller
integrates (fetch + merge) and retries instead of force-pushing.

Following the shell-script testing shape established by
``tests/test_quality_gate_hook_changed_files.py`` and
``tests/test_preflight_script.py``, these are deliberately *static-text* pins,
not behavioral tests: exercising the script would require running it as a real
subprocess against a real git tree, which the unit suite forbids (see "Testing
Principles" in ``CLAUDE.md``). The pins catch the silent-drop / dangerous-form
regression class — a future edit dropping the upstream-detection branch, the
reserved exit code, an NFF message pattern, or (worst) sneaking a ``--force``
onto a push line — and leave behavioral correctness (the actual push fallback and
exit-3 signalling) to manual review.

The safe-form pin uses **line-level enumeration**, not a bare substring check: a
``"git push" in content`` assertion is a substring of ``"git push --force"`` and
so silently passes the exact regression it must catch. Collecting every
non-comment line containing ``git push`` and asserting each is exactly one of the
two safe forms verifies *exclusivity*, and the comment-line filter keeps a
self-documenting safety comment (``# never pass --force``) from interfering.
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPT_FILE = (
    Path(__file__).resolve().parent.parent / "scripts" / "git-push-tracked.sh"
)

# The two — and only two — push forms the script may run: plain ``git push`` when
# an upstream is already configured, ``git push -u origin HEAD`` to set tracking
# on the first push. Pinned as an exclusive set so a future edit cannot add a
# third (e.g. force-pushing) form.
_ALLOWED_PUSH_FORMS = frozenset({"git push", "git push -u origin HEAD"})

# Force flags that must never appear on a push line. A non-fast-forward rejection
# is signalled (exit 3) for the caller to integrate, never overridden here.
_FORCE_TOKENS = ("--force", "--force-with-lease")

# The upstream-detection probe that selects between the two push forms.
_UPSTREAM_PROBE = "git rev-parse --abbrev-ref --symbolic-full-name '@{u}'"

# The reserved non-fast-forward exit code, kept distinct from git's generic
# failure (1) so the caller branches on "integration required" without parsing
# output. Pinned both as the definition and as the routed ``exit`` so neither can
# drift without the other.
_RESERVED_NFF_EXIT_CODE = "3"

# The three non-fast-forward rejection-message patterns, matching the set
# ``merge-main.md`` step 7 keys on. Pinned so a future edit cannot drop one and
# silently stop recognising a rejection (collapsing the exit-3 signal into a
# generic exit-1 the caller would surface as an unrecoverable error).
_NFF_MESSAGE_PATTERNS = (
    "Updates were rejected because the remote contains work that you do not "
    "have locally",
    "fetch first",
    "non-fast-forward",
)


def _read() -> str:
    return _SCRIPT_FILE.read_text(encoding="utf-8")


def _push_lines(content: str) -> list[str]:
    """Every non-comment line that runs a ``git push``.

    Mirrors the Testing Strategy regex: ``^(?!\\s*#)`` drops comment lines so a
    safety comment mentioning ``--force`` cannot trip the force-token pin, and
    ``\\bgit push\\b`` matches the command (not, say, ``git push``-in-prose).
    """
    return re.findall(r"(?m)^(?!\s*#).*\bgit push\b.*$", content)


def _push_form(line: str) -> str:
    """Extract the ``git push …`` invocation from a push line.

    The script wraps the form in a bash array (``push_cmd=(git push -u origin
    HEAD)``); capture from ``git push`` up to the closing paren / quote so the
    surrounding array syntax does not pollute the exclusivity comparison.
    """
    match = re.search(r"git push[^)\"'\n]*", line)
    return match.group(0).strip() if match else ""


def test_upstream_detection_branch_is_present() -> None:
    """The push form is selected on the upstream-detection probe.

    Pin the ``@{u}`` probe and both push forms so a future edit cannot collapse
    the branch into a single unconditional form (losing the first-push
    ``-u origin HEAD`` tracking, or pushing with ``-u`` every time).
    """
    content = _read()
    assert _UPSTREAM_PROBE in content, (
        f"scripts/git-push-tracked.sh must select the push form on the upstream "
        f"probe {_UPSTREAM_PROBE!r}; without it the `git push` vs "
        f"`git push -u origin HEAD` branch cannot be keyed on real upstream "
        f"state."
    )
    forms = {_push_form(line) for line in _push_lines(content)}
    for safe in _ALLOWED_PUSH_FORMS:
        assert safe in forms, (
            f"scripts/git-push-tracked.sh is missing the safe push form "
            f"{safe!r}; both {sorted(_ALLOWED_PUSH_FORMS)!r} must be present so "
            f"the upstream-detection branch keeps both arms."
        )


def test_push_lines_use_only_the_two_safe_forms() -> None:
    """Every ``git push`` line is exactly one of the two safe forms.

    Line-level enumeration verifies *exclusivity*: a bare ``"git push" in
    content`` check is a substring of ``"git push --force"`` and would pass even
    with a force-push present. Asserting each push line's extracted form is in
    the allowed set catches any third (dangerous) form being added.
    """
    content = _read()
    lines = _push_lines(content)
    assert lines, (
        "scripts/git-push-tracked.sh has no `git push` line — the helper must "
        "actually push."
    )
    for line in lines:
        form = _push_form(line)
        assert form in _ALLOWED_PUSH_FORMS, (
            f"scripts/git-push-tracked.sh push line {line.strip()!r} resolves to "
            f"{form!r}, which is not one of the safe forms "
            f"{sorted(_ALLOWED_PUSH_FORMS)!r}. No third push form is allowed."
        )


def test_no_force_push_token_on_any_push_line() -> None:
    """No push line carries ``--force`` / ``--force-with-lease``.

    The exclusivity safety pin. A non-fast-forward rejection is signalled (exit
    3) for the caller to integrate; the helper must never override it by
    force-pushing.
    """
    content = _read()
    for line in _push_lines(content):
        for token in _FORCE_TOKENS:
            assert token not in line, (
                f"scripts/git-push-tracked.sh push line {line.strip()!r} carries "
                f"{token!r}; the helper must never force-push — a "
                f"non-fast-forward rejection is signalled (exit "
                f"{_RESERVED_NFF_EXIT_CODE!r}) for the caller to integrate."
            )


def test_reserved_nff_exit_code_is_defined_and_routed() -> None:
    """The NFF exit code is defined as 3 and is what the NFF branch exits with.

    Pin the constant definition and its use so a future edit cannot silently
    change the reserved code (breaking every caller branching on exit 3) or route
    the NFF branch to a different code.
    """
    content = _read()
    assert f"NFF_EXIT_CODE={_RESERVED_NFF_EXIT_CODE}" in content, (
        f"scripts/git-push-tracked.sh must define the reserved non-fast-forward "
        f"exit code as NFF_EXIT_CODE={_RESERVED_NFF_EXIT_CODE}; callers "
        f"(merge-main, commit-push, create-pr) branch on exit "
        f"{_RESERVED_NFF_EXIT_CODE}."
    )
    assert 'exit "$NFF_EXIT_CODE"' in content, (
        "scripts/git-push-tracked.sh must exit with $NFF_EXIT_CODE on the "
        "non-fast-forward branch so the reserved code is actually emitted."
    )


def test_all_three_nff_rejection_patterns_are_pinned() -> None:
    """Every non-fast-forward rejection message merge-main keys on is matched.

    Pinned together so dropping one (e.g. ``"fetch first"``) cannot silently
    narrow the detection and let a real rejection fall through to the generic
    exit-1 path the caller surfaces as unrecoverable.
    """
    content = _read()
    for pattern in _NFF_MESSAGE_PATTERNS:
        assert pattern in content, (
            f"scripts/git-push-tracked.sh is missing the non-fast-forward "
            f"rejection pattern {pattern!r}; all of {_NFF_MESSAGE_PATTERNS!r} "
            f"(matching merge-main.md step 7) must be matched."
        )


def test_push_is_routed_through_with_env() -> None:
    """The push runs through ``with-env.sh`` so GITHUB_TOKEN is loaded.

    Pinned so a future edit cannot drop the token-loading wrapper and leave the
    push to fail on auth in a fresh shell.
    """
    content = _read()
    assert "with-env.sh" in content, (
        "scripts/git-push-tracked.sh must route the push through "
        "scripts/with-env.sh so GITHUB_TOKEN is loaded before pushing."
    )

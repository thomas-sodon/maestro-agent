"""Regression: backlog.md Trigger A requires a bounded issue-number match.

`/backlog`'s **Close trigger A — already fixed** auto-closes a GitHub issue
when a fix-shaped commit appears to have resolved it. A close is publicly
visible and notifies every subscriber, so the issue-number match must be
precise. The original wording — "or directly references the issue number" —
permitted a bare-substring reading: a commit subject containing `#1234`
contains the substring `234`, so it could be read as fixing issue 234 too.

This test guards the tightened wording: the loose phrase is gone, the
issue-number reference is pinned to a boundary, and bare-substring matching
of the digits alone is documented as insufficient.
"""

from __future__ import annotations

import re
from pathlib import Path

_BACKLOG_DOC = (
    Path(__file__).resolve().parent.parent / ".claude" / "commands" / "backlog.md"
)


def _section_between(text: str, start_header: str, end_header: str) -> str:
    start = text.index(start_header)
    end = text.index(end_header, start + len(start_header))
    return text[start:end]


def test_trigger_a_requires_bounded_issue_number_match() -> None:
    doc = _BACKLOG_DOC.read_text(encoding="utf-8")

    trigger_a = _section_between(
        doc, "### Close trigger A", "### Close trigger B"
    )
    # Prose may capitalize a term at a sentence start; match case-insensitively.
    trigger_a_lower = trigger_a.lower()

    assert "directly references the issue number" not in trigger_a_lower, (
        "Trigger A must no longer permit the loose 'directly references the "
        "issue number' bare-substring reading — a commit subject containing "
        f"`#1234` could match issue 234. Section:\n{trigger_a}"
    )
    assert "word boundary" in trigger_a_lower, (
        "Trigger A must pin the issue-number reference to a word boundary so "
        f"a coincidental digit substring does not match. Section:\n{trigger_a}"
    )
    assert "non-digit" in trigger_a_lower, (
        "Trigger A must require the bare `#<NNN>` token to be bounded by "
        f"non-digits so `#1234` does not match issue 234. Section:\n{trigger_a}"
    )
    assert "bare-substring" in trigger_a_lower and "insufficient" in trigger_a_lower, (
        "Trigger A must explicitly state that bare-substring matching of the "
        f"digits alone is insufficient. Section:\n{trigger_a}"
    )


def test_open_pr_skip_covers_all_closing_keywords() -> None:
    """Regression (#1683): the open-PR skip must recognize every GitHub
    closing keyword, not only `Fixes`/`Closes`.

    The oldest-first picker skips an issue when an open PR is already linked
    to it (an implementer is on it). GitHub links a PR via any of its
    closing keywords — `close`/`closes`/`closed`, `fix`/`fixes`/`fixed`,
    `resolve`/`resolves`/`resolved` — so a body saying `Resolves #<NNN>` is a
    genuine in-flight fix. The original two-keyword union
    (`Fixes`/`Closes` only) missed it, so the skip failed open and re-picked
    an issue that already had an implementer PR. This test pins the widened
    detection on the isolated skip line so a regression to two keywords is
    caught. Trigger A at line 66 already lists all three keywords and is a
    different check, so the assertion isolates the skip bullet, not the doc.
    """
    doc = _BACKLOG_DOC.read_text(encoding="utf-8")

    skip_lines = [
        line
        for line in doc.splitlines()
        if "an open pr is linked to the issue" in line.lower()
    ]
    assert len(skip_lines) == 1, (
        "Expected exactly one 'open PR is linked' skip bullet in the "
        f"oldest-first picker, found {len(skip_lines)}."
    )
    skip = skip_lines[0].lower()

    assert "resolve" in skip, (
        "The open-PR skip detection must recognize GitHub's `resolve` closing-"
        "keyword family (`resolve`/`resolves`/`resolved`), not only "
        f"`fixes`/`closes`. Line:\n{skip_lines[0]}"
    )
    assert "closing keyword" in skip, (
        "The open-PR skip detection must state it covers GitHub's closing-"
        f"keyword set, not just two keywords. Line:\n{skip_lines[0]}"
    )
    assert "colon" in skip, (
        "The open-PR skip detection must accept GitHub's colon-separated "
        "closing form (`Fixes: #<NNN>`) via the optional `:?` — GitHub "
        "auto-closes it, so the whitespace-only regex failed open. "
        f"Line:\n{skip_lines[0]}"
    )


def test_open_pr_skip_regex_bounds_issue_number() -> None:
    """Regression (#1683 follow-up): the open-PR skip regex must be bounded.

    The widened regex `(close[sd]?|fix(es|ed)?|resolve[sd]?)\\s+#<NNN>` had no
    boundaries. Its trailing side is unbounded on the issue number, so
    triaging `#12` matches a PR body's `Fixes #123` — a false-positive skip of
    a real, actionable issue, the exact class Trigger A guards. Its leading
    side omits `\\b`, so `discloses #<NNN>` reads as `closes #<NNN>`. The fix
    wraps the regex in `\\b...\\b`; this pins both boundaries so a regression
    to the unbounded literal fails. The bounded literal also carries the
    optional `:?` so GitHub's colon-separated closing form (`Fixes: #<NNN>`,
    which GitHub still auto-closes) is matched; a regression that drops the
    colon fails here too.
    """
    doc = _BACKLOG_DOC.read_text(encoding="utf-8")

    unbounded = "`(close[sd]?|fix(es|ed)?|resolve[sd]?):?\\s+#<NNN>`"
    bounded = "`\\b(close[sd]?|fix(es|ed)?|resolve[sd]?):?\\s+#<NNN>\\b`"
    assert unbounded not in doc, (
        "The open-PR skip regex must not use the unbounded literal "
        f"{unbounded} — it false-matches `#123` when triaging `#12`. Use the "
        f"boundary-wrapped {bounded} instead."
    )
    assert bounded in doc, (
        "The open-PR skip regex must be wrapped in `\\b...\\b` so the keyword "
        "stays whole and the issue number is bounded on a non-digit. "
        f"Expected {bounded} in the doc."
    )

    skip = next(
        line
        for line in doc.splitlines()
        if "an open pr is linked to the issue" in line.lower()
    ).lower()
    assert "non-digit" in skip, (
        "The skip bullet must explain the trailing boundary bounds the issue "
        f"number on a non-digit. Line:\n{skip}"
    )
    assert "#123" in skip, (
        "The skip bullet must give the concrete false-match it now prevents "
        f"(`#12` must not match `#123`). Line:\n{skip}"
    )


def test_backlog_close_provenance_is_branch_agnostic() -> None:
    """The auto-close provenance line must not hardcode the default branch name.

    Every `/backlog` auto-close posts a publicly visible, subscriber-notifying
    comment ending in a provenance line. Baking the literal default-branch name
    `main` into that permanent record couples it to a mutable repository setting:
    rename the default branch and every already-posted comment silently misstates
    what was verified. The line must instead be phrased in terms of "the current
    default branch" so it stays accurate regardless of the branch's name.
    """
    doc = _BACKLOG_DOC.read_text(encoding="utf-8")

    close_path = _section_between(doc, "## Close path", "## Plan path")

    assert "verifying against current main" not in close_path, (
        "The close provenance line must not hardcode the default-branch name "
        "`main` — a rename would make every already-posted auto-close comment "
        f"factually wrong. Section:\n{close_path}"
    )
    assert "current default branch" in close_path, (
        "The close provenance line must be branch-agnostic, phrased in terms of "
        f"'the current default branch' rather than a literal name. Section:\n{close_path}"
    )


def test_picking_list_fetches_whole_open_backlog() -> None:
    """The oldest-first picking list must not cap the fetch below the backlog.

    `/backlog` walks the open backlog oldest-first and skips issues that already
    have a plan, an open linked PR, or a skip-class label. A `--limit 100` fetch
    ceiling (issue #1684) drops the youngest issues, so once the repository has
    more than the cap's worth of open issues an actionable issue beyond the
    window is invisible forever — the walk can stop with
    `error=no_actionable_issue` while actionable work exists unfetched. This
    test locks the ceiling above the realistic open-issue count and requires the
    spec to acknowledge the residual fetch-window bound honestly.
    """
    doc = _BACKLOG_DOC.read_text(encoding="utf-8")

    picking = _section_between(
        doc,
        "## Pick the oldest actionable issue",
        "## Read and parse the picked issue",
    )
    picking_lower = picking.lower()

    # `--limit 1000` legitimately contains the substring `--limit 100`, so a
    # bare substring check would false-positive; pin the cap to a word boundary
    # (not followed by another digit) to catch only the truncating `100` cap.
    assert not re.search(r"--limit 100(?!\d)", picking), (
        "The picking list command must not cap the open-issue fetch at "
        "`--limit 100` — with more open issues than the cap the youngest are "
        "never fetched, so an actionable issue beyond the window is masked "
        f"forever. Section:\n{picking}"
    )
    assert "must exceed" in picking_lower and "open-issue count" in picking_lower, (
        "The picking section must acknowledge that the `--limit` ceiling must "
        "exceed the current open-issue count (gh pages internally up to "
        f"`--limit`). Section:\n{picking}"
    )
    assert "whole open backlog" in picking_lower, (
        "The picking section must state that the walk fetches the whole open "
        f"backlog so no actionable issue is masked by the fetch window. "
        f"Section:\n{picking}"
    )
    # The residual-bound acknowledgment must stay honest: raising the cap does
    # not eliminate #1684's failure mode (open-issue count above the ceiling
    # AND every fetched issue skipped), so the section must not overclaim that
    # the first actionable issue is *always* within the fetched set, and must
    # acknowledge that a youngest issue can still be masked.
    assert not re.search(r"always[^.]*within the fetched", picking_lower), (
        "The picking section must not overclaim that the first actionable "
        "issue is *always* within the fetched set — the fetch-window bound is "
        f"raised, not eliminated. Section:\n{picking}"
    )
    assert "can still be masked" in picking_lower, (
        "The picking section must honestly acknowledge that an actionable "
        "youngest issue can still be masked when the open-issue count exceeds "
        f"the ceiling and every fetched issue is skipped. Section:\n{picking}"
    )


def test_no_code_fence_carries_trailing_prose() -> None:
    """Regression (#2307): a closing code fence must not carry trailing prose.

    CommonMark permits an info string only on an *opening* fence; a closing
    fence may be followed only by whitespace. The `gh issue view` example's
    closer once read ```` ``` If the lock fetch returns `true` … ````, so the
    closing fence never closed — the block opened at the ```` ``` ```` above it
    stayed open and swallowed the whole **Read and parse the picked issue**
    recovery guidance (and cascaded into the downstream `gh issue close` fences)
    as code content. This walk over the checked-in doc pins the invariant: every
    opened fence closes, and no closing fence carries trailing non-whitespace.
    """
    doc = _BACKLOG_DOC.read_text(encoding="utf-8")

    open_marker_len: int | None = None
    open_lineno: int | None = None
    for lineno, line in enumerate(doc.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("```"):
            continue
        # The fence marker is the leading run of backtick characters.
        marker_len = len(stripped) - len(stripped.lstrip("`"))
        after = stripped[marker_len:]
        if open_marker_len is None:
            # Opening fence: an info string after the marker is permitted.
            open_marker_len = marker_len
            open_lineno = lineno
        else:
            # Closing fence: only whitespace may follow the marker, and it must
            # be a bare run (no info string) at least as long as the opener.
            assert after.strip() == "", (
                "A closing code fence must be followed only by whitespace "
                "(CommonMark forbids an info string on a closer). The trailing "
                "prose leaves the block opened at line "
                f"{open_lineno} unclosed, swallowing the following guidance as "
                f"code. Offending closer at line {lineno}: {line!r}"
            )
            assert marker_len >= open_marker_len, (
                "A closing code fence must be at least as long as its opener "
                f"(opener line {open_lineno} used {open_marker_len} backticks; "
                f"closer at line {lineno} used {marker_len})."
            )
            open_marker_len = None
            open_lineno = None

    assert open_marker_len is None, (
        "Every opened code fence must close; the fence opened at line "
        f"{open_lineno} never closes (a trailing-prose closer does not count)."
    )

    # The specific bad state #2307 described: the recovery sentence must render
    # as prose, not be swallowed as code content of an unclosed block.
    section = _section_between(
        doc,
        "## Read and parse the picked issue",
        "## Verify against today's tree",
    )
    lock_line = next(
        line
        for line in section.splitlines()
        if "If the lock fetch returns `true`" in line
    )
    assert not lock_line.lstrip().startswith("```"), (
        "The recovery sentence 'If the lock fetch returns `true`' must be a "
        "standalone paragraph, not glued onto a closing code fence — gluing it "
        f"onto the fence hides the recovery guidance in a code block. Line:\n{lock_line}"
    )

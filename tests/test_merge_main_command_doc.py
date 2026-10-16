"""Regression: merge-main.md's branch-derived-plan strip stays uid-shape-constrained.

`.claude/commands/merge-main.md`'s *Originating plan* step-1 (Plan-file-derived
bullet) resolves the branch's plan file and, when the exact `<slug>.md` is
missing, strips a trailing `-<token>` to undo the `-<uid>` that `/impl` appends.
Stripping *any* trailing token is over-broad: it can drop a real slug word and
false-match a shorter, unrelated plan. `/create-pr` shares this strip and got
`test_branch_plan_strip_constrained_to_uid_shape` to lock its regex to the uid
shape; merge-main.md carries the identical hand-authored regex but had no test,
so a future broadening here would go undetected.

This test mirrors the create-pr regression so the merge-main strip can only fire
on a token matching the documented `/impl` uid shape.
"""

from __future__ import annotations

from pathlib import Path

_MERGE_MAIN_DOC = (
    Path(__file__).resolve().parent.parent / ".claude" / "commands" / "merge-main.md"
)


def _line_containing(text: str, needle: str) -> str:
    matches = [line for line in text.splitlines() if needle in line]
    assert len(matches) == 1, (
        f"Expected exactly one line containing {needle!r}, found {len(matches)}."
    )
    return matches[0]


def test_branch_plan_strip_constrained_to_uid_shape() -> None:
    doc = _MERGE_MAIN_DOC.read_text(encoding="utf-8")

    bullet = _line_containing(doc, "strip that one uid-shaped trailing segment")

    assert "^[0-9]{12}[0-9a-f]*$" in bullet, (
        "The Plan-file-derived bullet must constrain the trailing token to the "
        "`/impl` uid shape via the anchored regex `^[0-9]{12}[0-9a-f]*$`, not "
        f"strip any trailing segment. Bullet:\n{bullet}"
    )
    assert "uid shape" in bullet, (
        "The bullet must name the `/impl` uid shape as the strip condition. "
        f"Bullet:\n{bullet}"
    )
    assert "ordinary trailing slug word is left intact" in bullet, (
        "The bullet must state that an ordinary trailing slug word is left "
        "intact so the strip can never resolve a shorter, unrelated plan. "
        f"Bullet:\n{bullet}"
    )
    # The old unconstrained wording — strip *any* trailing segment — must be gone.
    assert "strip that one trailing segment and retry the lookup once" not in doc, (
        "The unconditional 'strip that one trailing segment' wording must be "
        "replaced by the uid-shape-constrained strip."
    )


def test_auth_section_omits_unused_gh() -> None:
    """The Authentication section enumerates only the verbs the command runs.

    `/merge-main` shells out to `git fetch` and `git push` (via the wrappers) but
    never invokes `gh`; the auth line must not prime the reader to expect a `gh`
    invocation that never comes (issue #1194).
    """
    doc = _MERGE_MAIN_DOC.read_text(encoding="utf-8")

    auth_line = _line_containing(doc, "GitHub access needs")

    assert "`gh`" not in auth_line, (
        "The auth section must not enumerate `gh` — `/merge-main` invokes no `gh` "
        f"command. Auth line:\n{auth_line}"
    )
    assert "`git fetch`" in auth_line, (
        "The auth section must name `git fetch` — the command does fetch from "
        f"origin. Auth line:\n{auth_line}"
    )
    assert "`git push`" in auth_line, (
        "The auth section must name `git push` — the command does push the merged "
        f"commits. Auth line:\n{auth_line}"
    )

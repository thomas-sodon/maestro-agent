"""Regression: rca.md defers the bug-mode module-scoped git log to step 8b.

Step 8 ("Map to recent code changes") used to instruct bug mode to run
`git log --oneline -15 -- maestro/<module>.py` — but the implicated
`<module>` is only identified later, in step 8b's Grep/Glob
investigation. A model following the document in order would either have
to guess the module before step 8b located it (risking the wrong scope)
or defer the query, contradicting the document's step order. The
skip-when-no-data-dir preamble compounded it: it sent bug mode "to step
8b", silently omitting step 8's data-dir-free `git log` even though that
history query legitimately applies to bug mode.

This test guards two things:

* The bug-mode module-scoped `git log` lives in step 8b (after the
  module is identified), and step 8 explicitly defers it there rather
  than naming `<module>.py` before it is known.
* The no-data-dir skip path lands on step 8 (whose repo-root `git log`
  needs no data dir), not step 8b, so that history query still runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_RCA_DOC = Path(__file__).resolve().parent.parent / ".claude" / "commands" / "rca.md"

# A shell heredoc operator: `<<` (optionally `<<-`), optional whitespace,
# then a delimiter word that may be bare (`<<EOF`), single/double-quoted
# (`<<'EOF'`, `<<"EOF"`), or backslash-quoted (`<<\EOF`). Every form
# quotes the RCA body and carries the same early-termination/truncation
# risk #1639 fixes, so all must be caught. The `(?<!<)` lookbehind keeps a
# `<<<` here-string from matching: without it, an unanchored `re.search`
# would bind `<<` to the 2nd/3rd `<` of `<<<WORD` and flag a harmless
# here-string (which quotes nothing and carries no truncation risk).
_HEREDOC_RE = re.compile(r"(?<!<)<<-?\s*['\"\\]?\w")


def _section_between(text: str, start_header: str, end_header: str) -> str:
    if start_header not in text:
        raise AssertionError(f"Start marker not found in document: {start_header!r}")
    start = text.index(start_header)
    if end_header not in text[start + len(start_header) :]:
        raise AssertionError(f"End marker not found after start in document: {end_header!r}")
    end = text.index(end_header, start + len(start_header))
    return text[start:end]


def test_step_8_defers_bug_mode_module_git_log_to_8b() -> None:
    doc = _RCA_DOC.read_text(encoding="utf-8")

    step_8 = _section_between(
        doc,
        "8. **Map to recent code changes.**",
        "8b. **Investigate the implicated code",
    )
    assert "deferred to step 8b" in step_8, (
        "Step 8 must explicitly defer the bug-mode module-scoped git log "
        f"to step 8b rather than running it before the module is known. "
        f"Step 8:\n{step_8}"
    )
    assert "do not guess a module here" in step_8, (
        "Step 8 must forbid guessing the implicated module before step 8b "
        f"identifies it. Step 8:\n{step_8}"
    )
    assert "git log --oneline -15" not in step_8, (
        "The module-scoped `git log --oneline -15` must not live in step 8 "
        "— it belongs in step 8b, after the module is identified. "
        f"Step 8:\n{step_8}"
    )


def test_step_8b_runs_module_scoped_git_log_after_identifying_module() -> None:
    doc = _RCA_DOC.read_text(encoding="utf-8")

    step_8b = _section_between(
        doc,
        "8b. **Investigate the implicated code",
        "9. **Walk to the root cause.**",
    )
    assert "git log --oneline -15 -- maestro/<module>.py" in step_8b, (
        "Step 8b must run the deferred module-scoped git log once the "
        f"implicated module(s) are known. Step 8b:\n{step_8b}"
    )


def test_step_2_fallback_stop_is_mode_qualified_not_unconditional() -> None:
    doc = _RCA_DOC.read_text(encoding="utf-8")

    step_2 = _section_between(
        doc,
        "2. **Locate the live data directory.**",
        "3. **Read the",
    )
    assert "in **incident mode**, stop" in step_2, (
        "Step 2's final fallback bullet must qualify the hard stop to "
        "incident mode — bug mode is best-effort and must demote to the "
        f"code steps (8/8b), not abort. Step 2:\n{step_2}"
    )
    assert "If none of the above work, stop" not in step_2, (
        "Step 2 must not carry an unconditional 'If none of the above work, "
        "stop' — that aborts bug mode, contradicting the best-effort "
        f"semantics declared in the step 2 preamble. Step 2:\n{step_2}"
    )


def test_docker_exec_fallback_states_in_container_data_dir() -> None:
    doc = _RCA_DOC.read_text(encoding="utf-8")

    step_2 = _section_between(
        doc,
        "2. **Locate the live data directory.**",
        "3. **Read the",
    )
    # Anchor the assertion to the `docker exec` fallback bullet itself, not
    # anywhere in step 2 — otherwise a future edit could relocate the literal
    # to an unrelated bullet and the guard would still pass.
    fallback_bullet = _section_between(
        step_2,
        "Else, fall back to `docker exec`",
        "If none of the above work",
    )
    assert "/workspace/data" in fallback_bullet, (
        "Step 2's `docker exec` fallback branch must name the concrete "
        "in-container data dir (`/workspace/data`, where the loop container "
        "mounts the repo and writes its state) so `$DATA_DIR` resolves to a "
        "real path instead of being left to inference. Fallback bullet:\n"
        f"{fallback_bullet}"
    )


def test_no_data_dir_skip_lands_on_step_8_not_8b() -> None:
    doc = _RCA_DOC.read_text(encoding="utf-8")

    # Anchor on a substring that omits the en-dash in the "Steps 3-7"
    # heading so the literal avoids ruff's ambiguous-character lint
    # (RUF001) while still pinning the preamble that carries the skip.
    preamble = _section_between(
        doc, "gather live runtime evidence.**", "3. **Read the"
    )
    assert "skip to step 8 " in preamble, (
        "The no-data-dir bug-mode skip must land on step 8 (its repo-root "
        "`git log` needs no data dir), not jump past it to step 8b. "
        f"Preamble:\n{preamble}"
    )
    assert "skip to step 8b" not in preamble, (
        "The skip must no longer jump straight to step 8b, which would "
        "silently omit step 8's data-dir-free git log in bug mode. "
        f"Preamble:\n{preamble}"
    )


def test_invalid_data_dir_bug_mode_includes_step_8b() -> None:
    doc = _RCA_DOC.read_text(encoding="utf-8")

    data_dir_bullet = _section_between(
        doc,
        "If `--data-dir` is set",
        "Else, try the host bind-mount",
    )
    assert "proceed to steps 8 and 8b" in data_dir_bullet, (
        "The invalid `--data-dir` bug-mode bullet must demote to both step 8 "
        "and step 8b (the core of bug mode), matching every other no-data-dir "
        f"path in the document. Bullet:\n{data_dir_bullet}"
    )
    assert "proceed to step 8." not in data_dir_bullet, (
        "The invalid `--data-dir` bug-mode bullet must not stop at step 8 — "
        "that omits step 8b's Grep/Glob investigation, code trace, and test "
        f"check that constitute the actual bug-mode analysis. Bullet:\n{data_dir_bullet}"
    )


def test_post_issue_recipe_uses_body_file_not_heredoc() -> None:
    """The "Post the issue" recipe must pass the body via `--body-file`.

    Step 14 used to embed the RCA report — which quotes runtime log
    excerpts, SDK transcripts, and Python reprs verbatim — through a
    quoted shell heredoc, `--body "$(cat <<'EOF' … EOF )"`. Any body line
    that is exactly `EOF` terminates the heredoc early: the posted issue
    is silently truncated at that line and the trailing `)"` leaks into
    the shell as stray tokens. `--body-file` reads the body from a file,
    so no body line can be mistaken for a shell terminator.

    This test converts the previously silent fragility into a loud CI
    failure the moment any future edit reintroduces a shell heredoc — of
    any delimiter, not just the original `<<'EOF'` — into the posting
    recipe.
    """
    doc = _RCA_DOC.read_text(encoding="utf-8")

    step_14 = _section_between(
        doc,
        "14. **Post the issue.**",
        "15. **Report the issue URL.**",
    )

    # Reject any heredoc operator — bare, quoted, `<<-`, or backslash-
    # quoted (`<<\EOF`) — not just the original `<<'EOF'`, so a
    # reintroduction under a different delimiter cannot slip past this
    # guard. `<<<` here-strings carry no truncation risk and are excluded
    # by `_HEREDOC_RE`'s lookbehind (see its definition above).
    assert _HEREDOC_RE.search(step_14) is None, (
        "The RCA posting recipe must not embed the issue body through a "
        "shell heredoc — a body line equal to the heredoc delimiter would "
        "terminate it early and silently truncate the posted issue. Use "
        f"`--body-file`. Step 14:\n{step_14}"
    )

    assert "--body-file" in step_14, (
        "The 'Post the issue' recipe must post the composed body via "
        "`--body-file` so no body line can be mistaken for a shell "
        f"terminator. Step 14:\n{step_14}"
    )


def test_heredoc_guard_matches_all_delimiter_forms_but_not_herestrings() -> None:
    """`_HEREDOC_RE` catches every heredoc delimiter form, not here-strings.

    The recipe guard promises detection "of any delimiter". A
    backslash-quoted heredoc (`cat <<\\EOF … EOF`) quotes the body exactly
    like `<<'EOF'` and carries the same truncation risk, so it must be
    caught — the earlier regex missed it because `\\` is neither a quote
    nor a word character. A `<<<` here-string quotes nothing and must not
    trip the guard; without the `(?<!<)` lookbehind an unanchored search
    would bind `<<` to the 2nd/3rd `<` of `<<<WORD` and flag it.
    """
    # Every heredoc form — including the backslash-quoted delimiter the
    # earlier regex missed — must be detected.
    for heredoc in (
        "cat <<EOF",
        "cat << EOF",
        "cat <<-EOF",
        "cat <<'EOF'",
        'cat <<"EOF"',
        "cat <<\\EOF",
    ):
        assert _HEREDOC_RE.search(heredoc) is not None, (
            f"Heredoc form must be detected by the guard: {heredoc!r}"
        )

    # `<<<` here-strings quote nothing and must not trip the guard.
    for herestring in ("cat <<<WORD", 'cat <<< "$body"'):
        assert _HEREDOC_RE.search(herestring) is None, (
            f"Here-string must not trip the heredoc guard: {herestring!r}"
        )


def test_title_format_examples_obey_char_cap() -> None:
    """Every worked example in the "Title format:" section obeys the cap the
    section itself states.

    The section lists three example titles and then rules "Keep titles under
    80 characters." An example that breaks that rule teaches contributors —
    and the `/rca` command copying it as a template — an over-length title
    and undercuts the guidance (issue #1641: the middle example was 85 chars).
    This guards every bulleted example against the cap parsed from the rule
    sentence itself, so the test and the rule can't drift: edit the number in
    the doc and this guard moves with it.
    """
    doc = _RCA_DOC.read_text(encoding="utf-8")

    # Parse the cap out of the rule sentence itself, rather than hard-coding a
    # second copy of the number, so the two can never drift apart.
    cap_match = re.search(r"Keep titles under (\d+) characters", doc)
    assert cap_match is not None, (
        "rca.md must state the title cap as 'Keep titles under <N> characters' "
        "so this guard can parse the number it enforces."
    )
    cap = int(cap_match.group(1))

    # Isolate the worked-example bullets: from the "Title format:" line up to
    # (but excluding) the "Keep titles under ..." rule sentence.
    section = _section_between(doc, "Title format:", "Keep titles under")

    # Each example is a bulleted line whose title sits in backticks; any
    # trailing annotation like ` (bug mode)` lives outside the backticks and is
    # naturally excluded. The `Title format:` template line is not a bullet, so
    # it is skipped. The em-dash `—` counts as one character (len on the str).
    titles = []
    for line in section.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            continue
        match = re.search(r"`([^`]+)`", stripped)
        if match is not None:
            titles.append(match.group(1))

    assert titles, (
        "Expected at least one backtick-quoted example title in the "
        f"'Title format:' section. Section:\n{section}"
    )
    for title in titles:
        assert len(title) < cap, (
            f"Example title exceeds the doc's own {cap}-char cap "
            f"({len(title)} chars): {title!r}"
        )


def test_section_between_raises_assertion_error_on_missing_marker() -> None:
    text = "alpha beta gamma"

    with pytest.raises(AssertionError):
        _section_between(text, "ZZZ_MISSING_START", "beta")
    with pytest.raises(AssertionError):
        _section_between(text, "alpha", "ZZZ_MISSING_END")

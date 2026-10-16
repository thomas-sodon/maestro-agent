"""Static-text pins for ``scripts/with-env.sh`` (fix #1189).

``scripts/with-env.sh`` loads the project's ``.env`` and ``exec``s the wrapped
``gh`` / ``git`` command. It previously sourced the whole file under a bare
``set -a`` / ``set +a`` pair, which marks *every* variable in ``.env`` for
export — so ``exec "$@"`` leaked unrelated secrets (``GEMINI_API_KEY``,
``ANTHROPIC_API_KEY``, ``TELEGRAM_BOT_TOKEN``, …) into the subprocess even though
the wrapped tools need only ``GITHUB_TOKEN``. Fix #1189 confined the export-all
semantics to a command substitution and lifted out only ``GITHUB_TOKEN``; fix
#1636 then dropped the ``source`` entirely — the wrapper now reads the literal
``GITHUB_TOKEN=`` line so it never evaluates ``.env`` as shell (matching Docker
Compose's ``env_file`` parse rather than bash's).

Following the shell-script testing shape established by
``tests/test_preflight_script.py`` and ``tests/test_git_push_tracked_script.py``,
these are deliberately *static-text* pins, not behavioral tests: exercising the
script would require running it as a real subprocess (which the unit suite
forbids — see "Testing Principles" in ``CLAUDE.md``), so the least-privilege
export is left to manual review. The pins catch the over-broad-export regression
class — a future edit re-introducing the blanket ``set -a`` whole-file export, or
lifting a second variable out of ``.env``.

Like ``test_git_push_tracked_script.py``'s push-form pin, the export pin uses
**line-level enumeration**, not a bare substring check: a standalone ``set -a``
statement is the blanket-export signature, distinct from a ``set -a`` nested
inside a command substitution (the now-removed pre-#1636
``GITHUB_TOKEN="$(set -a; source …)"`` capture), so the pin must reason about
whole (non-comment) lines rather than a substring. A sibling pin
(``test_with_env_reads_github_token_without_sourcing_env``) complements it,
requiring that no ``source "$env_file"`` survives at all and that the token is
read literally.

A later pin (fix #1536) covers the opt-in ``--optional-token`` SOFT-token mode:
the fetch-only ``prune-branches`` path passes the flag so a checkout whose ``.env``
lacks ``GITHUB_TOKEN`` but whose origin is fetchable without one (SSH, public
HTTPS, a credential helper) still runs. It pins that the flag is recognised, that
the missing-``.env`` / empty-token abort is gated on the flag being off (never an
unconditional ``exit 1`` that would defeat soft mode), and that the soft branch
does not widen the least-privilege export. Two follow-up pins guard the soft-mode
edges: default mode's missing-``.env`` abort must not be conjoined with the
empty-token test (r1 — an ambient token must not relax the fail-closed default),
and an empty ``.env`` capture must be dropped rather than exported as an empty
``GITHUB_TOKEN`` that clobbers soft mode's ambient auth (r2). A third pin (r3, fix
#1636) guards the ambient-token regression the literal read introduced: the read /
export / unset is gated on the ``GITHUB_TOKEN=`` line *existing*, so a present
``.env`` that omits the line leaves an ambient token untouched (matching the
absent-``.env`` path) rather than ``unset``-ing it — the pre-#1636 ``source``
capture preserved it by inheriting the exported ambient value in its subshell.
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPT_FILE = (
    Path(__file__).resolve().parent.parent / "scripts" / "with-env.sh"
)

# The one — and only one — variable the wrapper may lift out of .env and export
# into the exec'd subprocess. Pinned as an exclusive set so a future edit cannot
# widen the export back to a second secret.
_ALLOWED_EXPORTS = frozenset({"GITHUB_TOKEN"})

# A standalone export-all directive: a bare ``set -a`` marks every subsequently
# set variable for export, so a following ``source "$env_file"`` would export all
# of .env. Fix #1636 removed the source entirely (GITHUB_TOKEN is now read
# literally), so this is a forward-looking guard — its ``set +a`` partner is
# pinned too so neither half can reappear.
_BLANKET_EXPORT_DIRECTIVES = ("set -a", "set +a")

# The opt-in soft-token flag (fix #1536) and the guard token that gates the
# fail-closed abort on it. In soft mode the missing-.env / empty-token abort is
# skipped so a fetch-only caller can proceed with ambient auth.
_OPTIONAL_TOKEN_FLAG = "--optional-token"
_SOFT_MODE_GUARD = '"$optional_token" == false'
# The two fail-closed diagnostics that must live only inside the soft-mode-gated
# block, never on an ungated path that would exit 1 before the guard.
_ABORT_DIAGNOSTICS = ("not found", "empty after reading")

# The empty-token test (fix #1536, r1): it must NOT sit on the soft-mode gate
# line. The pre-fix guard conjoined ``"$optional_token" == false`` with this test,
# so a missing .env aborted only when the token was *also* empty — letting an
# ambient GITHUB_TOKEN relax the fail-closed default. Keeping it off the gate line
# makes the .env-presence abort fire unconditionally in default mode.
_EMPTY_TOKEN_TEST = '-z "${GITHUB_TOKEN:-}"'

# The non-empty export guard and empty-capture drop (fix #1536, r2). An empty
# .env capture must be dropped, never exported: exporting (or leaving exported)
# GITHUB_TOKEN="" would clobber the ambient auth (a credential helper / SSH) soft
# mode relies on. The export is guarded on a non-empty capture and the empty
# branch ``unset``s the token.
_NONEMPTY_TOKEN_GUARD = '-n "$GITHUB_TOKEN"'
_EMPTY_CAPTURE_DROP = "unset GITHUB_TOKEN"

# The line-presence gate (fix #1636, ambient-token regression). The value read /
# export / unset is nested inside a test that the ``GITHUB_TOKEN=`` *line* exists
# in .env — distinct from the ``-n "$GITHUB_TOKEN"`` value test — so a present .env
# that omits the line skips the whole extraction and leaves an ambient token
# untouched, matching the absent-.env path rather than ``unset``-ing it.
_LINE_PRESENT_GATE = '-n "$_github_token_line"'
_VALUE_FROM_LINE = 'GITHUB_TOKEN="${_github_token_line#*=}"'

# The Compose-matching value normalization (fix #1636 follow-up). Docker Compose's
# env_file parser trims surrounding whitespace and strips a matched pair of
# surrounding quotes — and the pre-#1636 `source` capture did the same via shell
# quote-removal — but the literal `grep`/prefix-strip read does neither on its own.
# So `GITHUB_TOKEN="ghp_…"` (a common .env habit) or a padded value would export the
# literal quotes/whitespace to `gh` / `git` and fail auth, re-introducing the very
# host-vs-Compose divergence fix #1636 removes. These markers pin that the extracted
# value is trimmed of surrounding whitespace (the `[![:space:]]` parameter-expansion
# idiom) and stripped of one matched single/double quote pair (a `case` on the
# value). Escape / `$`-expansion inside quotes are intentionally NOT replicated —
# tokens are `[A-Za-z0-9_]`, so the pair-strip suffices, not a full dotenv re-parse.
_NORMALIZE_MARKERS = (
    "%%[![:space:]]*",  # leading-whitespace trim
    "##*[![:space:]]",  # trailing-whitespace trim
    'case "$GITHUB_TOKEN" in',  # matched-quote-pair dispatch
    r'\"*\"',  # double-quoted branch label
    r"\'*\'",  # single-quoted branch label
)


def _read() -> str:
    return _SCRIPT_FILE.read_text(encoding="utf-8")


def _noncomment_lines(content: str) -> list[str]:
    """Every non-blank, non-comment line, stripped of surrounding whitespace."""
    lines: list[str] = []
    for raw in content.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def _exported_vars(lines: list[str]) -> set[str]:
    """Names lifted out of .env via an ``export NAME`` statement."""
    names: set[str] = set()
    for line in lines:
        for match in re.finditer(r"\bexport\s+([A-Za-z_][A-Za-z0-9_]*)", line):
            names.add(match.group(1))
    return names


def test_with_env_exports_only_github_token() -> None:
    """The wrapper lifts GITHUB_TOKEN — and nothing else — out of .env.

    Two failure modes are pinned together (the plan's "no blanket export" AND
    "only GITHUB_TOKEN"):

    * No standalone ``set -a`` / ``set +a`` directive. A bare ``set -a`` marks
      every variable a following ``source "$env_file"`` sets for export, leaking
      all of .env into the exec'd subprocess. Fix #1636 removed the ``source``
      entirely (GITHUB_TOKEN is now read literally, pinned by
      ``test_with_env_reads_github_token_without_sourcing_env``), so this is a
      forward-looking guard against a standalone ``set -a`` reappearing.
    * ``export`` lifts out exactly {GITHUB_TOKEN}. With the blanket ``set -a``
      ruled out, enumerating ``export NAME`` statements proves the exec'd
      subprocess inherits GITHUB_TOKEN alone.
    """
    lines = _noncomment_lines(_read())
    for line in lines:
        assert line not in _BLANKET_EXPORT_DIRECTIVES, (
            f"scripts/with-env.sh line {line!r} is a standalone export-all "
            f"directive; sourcing .env under it leaks every variable into the "
            f"exec'd subprocess. The wrapper reads the literal GITHUB_TOKEN= line "
            f"instead — keep the blanket `set -a` out so only GITHUB_TOKEN is "
            f"exported."
        )
    exported = _exported_vars(lines)
    assert exported == set(_ALLOWED_EXPORTS), (
        f"scripts/with-env.sh must export exactly {sorted(_ALLOWED_EXPORTS)!r}, "
        f"but exports {sorted(exported)!r}. Exporting any other variable leaks a "
        f".env secret into the exec'd `gh` / `git` subprocess."
    )


def test_with_env_reads_github_token_without_sourcing_env() -> None:
    """GITHUB_TOKEN is read *literally* from .env — never sourced or evaluated.

    Sourcing .env (the pre-fix form, ``GITHUB_TOKEN="$(set -a; source …)"``)
    evaluates the file as shell: a ``$…`` expansion mangles a value Docker
    Compose would accept literally (``GITHUB_TOKEN=ab$cd`` yields ``ab``), and a
    ``$(…)`` / backtick line *executes* during the load. Docker Compose's
    ``env_file`` parse is a literal ``KEY=VALUE`` read (fix #1636). Pin that the
    wrapper matches it — no ``source "$env_file"``, no ``eval`` of the file — and
    that GITHUB_TOKEN is instead extracted with a non-evaluating line read of the
    ``GITHUB_TOKEN=`` entry. (The standalone ``set -a`` export-all directive is
    already banned by ``test_with_env_exports_only_github_token``.) The guard also
    rejects the POSIX dot-source form ``. "$env_file"``, which evaluates .env
    identically to ``source`` — same ``$(…)``/backtick execution and value-mangling
    fix #1636 removes — despite containing neither ``source`` nor ``eval``.
    """
    lines = _noncomment_lines(_read())
    for line in lines:
        assert 'source "$env_file"' not in line, (
            f"scripts/with-env.sh line {line!r} still sources .env; sourcing "
            f"evaluates the file as shell (expanding $…, running $(…)), diverging "
            f"from Docker Compose's literal KEY=VALUE env_file parse. Read "
            f"GITHUB_TOKEN literally instead."
        )
        assert not re.search(r'(^|\s)\.\s+"\$env_file"', line), (
            f"scripts/with-env.sh line {line!r} dot-sources .env; the POSIX "
            f"`. \"$env_file\"` form evaluates the file exactly like `source`, "
            f"re-introducing the $(…)/backtick execution and value-mangling fix "
            f"#1636 removes. Read GITHUB_TOKEN literally instead."
        )
        assert not re.search(r"\beval\b.*env_file", line), (
            f"scripts/with-env.sh line {line!r} eval's the .env file; read "
            f"GITHUB_TOKEN literally rather than evaluating .env as shell."
        )
    read_lines = [
        line
        for line in lines
        if "GITHUB_TOKEN=" in line
        and '"$env_file"' in line
        and re.search(r"\b(grep|awk|sed|cut|read)\b", line)
    ]
    assert read_lines, (
        "scripts/with-env.sh must extract GITHUB_TOKEN with a non-evaluating line "
        "read of the 'GITHUB_TOKEN=' entry in \"$env_file\" (e.g. grep, awk, or "
        "sed — any text extractor that does not evaluate .env), matching Docker "
        "Compose's literal env_file parse."
    )


def test_with_env_optional_token_makes_the_token_soft() -> None:
    """``--optional-token`` gates the fail-closed abort so soft mode never exit 1s.

    The fetch-only ``prune-branches`` path (fix #1536) passes ``--optional-token``
    so a checkout whose ``.env`` lacks ``GITHUB_TOKEN`` but whose origin is
    fetchable without one (SSH, public HTTPS, a credential helper) still fetches.
    Three facts are pinned together:

    * The flag is recognised.
    * The missing-``.env`` / empty-token abort is conditioned on the soft-mode
      flag being **off** — never an unconditional ``exit 1`` that would defeat
      soft mode — and both abort diagnostics live *after* (inside) that guard.
    * The soft branch does not widen the export: the wrapper still lifts out
      exactly {GITHUB_TOKEN}, so soft mode cannot leak a second ``.env`` secret.
    """
    content = _read()
    lines = _noncomment_lines(content)
    assert _OPTIONAL_TOKEN_FLAG in content, (
        f"scripts/with-env.sh must recognise the {_OPTIONAL_TOKEN_FLAG!r} opt-in "
        f"flag so fetch-only callers (prune-branches) can proceed without a token."
    )
    guard_lines = [line for line in lines if _SOFT_MODE_GUARD in line]
    assert guard_lines, (
        f"scripts/with-env.sh must gate its missing-.env / empty-token abort on "
        f"{_SOFT_MODE_GUARD!r} so --optional-token proceeds instead of exiting 1; "
        f"an unconditional abort would defeat soft-token mode."
    )
    guard_pos = content.find(_SOFT_MODE_GUARD)
    for diagnostic in _ABORT_DIAGNOSTICS:
        diag_pos = content.find(diagnostic)
        assert diag_pos != -1, (
            f"scripts/with-env.sh must still emit the {diagnostic!r} fail-closed "
            f"diagnostic in the default (no-flag) mode."
        )
        assert diag_pos > guard_pos, (
            f"scripts/with-env.sh must emit the {diagnostic!r} abort only inside "
            f"the {_SOFT_MODE_GUARD!r} block, never on an ungated path that would "
            f"exit 1 before --optional-token is consulted."
        )
    exported = _exported_vars(lines)
    assert exported == set(_ALLOWED_EXPORTS), (
        f"scripts/with-env.sh soft-token branch must not widen the export: it "
        f"must still export exactly {sorted(_ALLOWED_EXPORTS)!r}, but exports "
        f"{sorted(exported)!r}."
    )


def test_with_env_default_mode_missing_env_aborts_regardless_of_ambient_token() -> None:
    """Default mode's missing-.env abort is not gated on the token (fix #1536, r1).

    The first cut of soft mode conjoined the gate with the empty-token test —
    ``[[ "$optional_token" == false && -z "${GITHUB_TOKEN:-}" ]]`` — so with .env
    absent but an ambient GITHUB_TOKEN set, default mode fell through instead of
    aborting, silently relaxing the fail-closed contract commit-push / create-pr /
    merge-main depend on. Pin that the soft-mode gate line does **not** also carry
    the empty-token test, so the .env-presence abort inside it fires unconditionally
    in default mode regardless of any ambient token.
    """
    gate_lines = [line for line in _noncomment_lines(_read()) if _SOFT_MODE_GUARD in line]
    assert gate_lines, (
        f"scripts/with-env.sh must gate the fail-closed abort on {_SOFT_MODE_GUARD!r}."
    )
    for line in gate_lines:
        assert _EMPTY_TOKEN_TEST not in line, (
            f"scripts/with-env.sh gate line {line!r} conjoins the soft-mode gate "
            f"with {_EMPTY_TOKEN_TEST!r}; a missing .env would then abort only when "
            f"the token is also empty, letting an ambient GITHUB_TOKEN relax the "
            f"fail-closed default. Keep the .env-presence abort separate so default "
            f"mode aborts on a missing .env regardless of any ambient token."
        )


def test_with_env_never_exports_an_empty_github_token() -> None:
    """An empty .env capture is dropped, never exported (fix #1536, r2).

    When .env is present but yields no GITHUB_TOKEN, the first cut still exported
    GITHUB_TOKEN="" — clobbering the ambient auth (a credential helper / SSH) that
    soft mode relies on, and contradicting "only when .env provides it". Pin that
    the ``export`` is guarded by a non-empty test and that the empty branch
    ``unset``s the token, so soft mode never runs the wrapped command with an empty
    exported GITHUB_TOKEN.
    """
    joined = "\n".join(_noncomment_lines(_read()))
    guard_pos = joined.find(_NONEMPTY_TOKEN_GUARD)
    export_pos = joined.find("export GITHUB_TOKEN")
    unset_pos = joined.find(_EMPTY_CAPTURE_DROP)
    assert guard_pos != -1 and export_pos != -1 and guard_pos < export_pos, (
        f"scripts/with-env.sh must guard `export GITHUB_TOKEN` on {_NONEMPTY_TOKEN_GUARD!r} "
        f"so an empty .env capture is never exported over ambient auth."
    )
    assert unset_pos > guard_pos, (
        f"scripts/with-env.sh must `{_EMPTY_CAPTURE_DROP}` when .env yields no token "
        f"so an empty capture cannot remain an exported empty value that clobbers "
        f"ambient auth in soft mode."
    )


def test_with_env_preserves_ambient_token_when_env_omits_github_token() -> None:
    """A present .env that omits GITHUB_TOKEN leaves ambient auth untouched (fix #1636).

    The pre-#1636 ``source`` capture ran in a subshell that inherited an exported
    ambient GITHUB_TOKEN, so a .env present-but-missing the token still yielded the
    ambient value — the ``export`` branch preserved it into ``exec``. The literal
    ``grep`` read sees only the file, so an absent ``GITHUB_TOKEN=`` line yields an
    empty capture that an *unconditional* empty-drop would then ``unset`` —
    stripping an ambient token a soft-mode caller (``prune-branches
    --optional-token``) relied on, and contradicting the absent-.env path (which
    skips the whole block, letting an ambient token survive). Pin that the value
    read / export / unset is nested inside the ``GITHUB_TOKEN=`` *line-presence*
    gate (``-n "$_github_token_line"``, distinct from the ``-n "$GITHUB_TOKEN"``
    value test), so a .env that omits the line skips the extraction and preserves
    any ambient token instead of dropping it.
    """
    joined = "\n".join(_noncomment_lines(_read()))
    gate_pos = joined.find(_LINE_PRESENT_GATE)
    assert gate_pos != -1, (
        f"scripts/with-env.sh must gate the GITHUB_TOKEN extraction on the "
        f"{_LINE_PRESENT_GATE!r} line-presence test so a present .env that omits "
        f"GITHUB_TOKEN preserves any ambient token rather than unset-ing it."
    )
    # Both the value assignment and the empty-drop must sit *after* (inside) the
    # line-presence gate, so neither runs when .env has no GITHUB_TOKEN line.
    value_pos = joined.find(_VALUE_FROM_LINE)
    unset_pos = joined.find(_EMPTY_CAPTURE_DROP)
    assert value_pos > gate_pos, (
        f"scripts/with-env.sh must assign GITHUB_TOKEN from the captured line only "
        f"inside the {_LINE_PRESENT_GATE!r} gate, so a missing line is a no-op that "
        f"leaves the ambient token in place."
    )
    assert unset_pos > gate_pos, (
        f"scripts/with-env.sh must reach `{_EMPTY_CAPTURE_DROP}` only inside the "
        f"{_LINE_PRESENT_GATE!r} gate, so a .env without a GITHUB_TOKEN line leaves "
        f"an ambient token untouched rather than unset-ing it."
    )


def test_with_env_normalizes_quoted_or_padded_github_token() -> None:
    """The literal read trims whitespace and strips one matched quote pair (fix #1636).

    Docker Compose's env_file parser trims surrounding whitespace and strips a
    matched pair of surrounding quotes, and the pre-#1636 ``source`` capture did the
    same via shell quote-removal — but the literal ``grep``/prefix-strip read does
    neither on its own. A common ``.env`` authoring style — ``GITHUB_TOKEN="ghp_…"``
    or a padded value — would then export the literal quotes/whitespace to ``gh`` /
    ``git`` and fail auth, re-introducing the very host-vs-Compose divergence fix
    #1636 set out to remove. Pin that the wrapper normalizes the extracted value:
    it trims surrounding whitespace (the ``[![:space:]]`` parameter-expansion idiom)
    and strips one matched pair of surrounding single/double quotes (a ``case`` on
    the value). Escape / ``$``-expansion inside quotes are intentionally NOT
    replicated — GitHub tokens are ``[A-Za-z0-9_]``, so the pair-strip suffices — so
    this pins that the normalization exists, not a full compose-go dotenv re-parse.
    """
    content = _read()
    for marker in _NORMALIZE_MARKERS:
        assert marker in content, (
            f"scripts/with-env.sh must normalize the extracted GITHUB_TOKEN the way "
            f"Docker Compose's env_file parse does (trim surrounding whitespace, "
            f"strip one matched quote pair) but is missing {marker!r}; a quoted or "
            f"padded GITHUB_TOKEN in .env would otherwise reach gh/git with literal "
            f"quotes/whitespace and fail auth."
        )

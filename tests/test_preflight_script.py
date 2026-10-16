"""Static-text pins for ``scripts/preflight.sh`` (proposal 065 step 1).

``scripts/preflight.sh`` centralises the git preconditions that the
``commit-push``, ``create-pr``, ``merge-main``, and ``prune-branches`` slash
commands previously restated in prose. It takes named check tokens, runs each in
order, and exits non-zero with a one-line diagnostic on the first failed check.

Following the shell-script testing shape established by
``tests/test_quality_gate_hook_changed_files.py``, these are deliberately
*static-text* pins, not behavioral tests: exercising the script would require
running it as a real subprocess against a real git tree, which the unit suite
forbids (see "Testing Principles" in ``CLAUDE.md``). The pins catch the
silent-drop regression class — a future edit dropping a check token, its git
command, or its non-zero-exit-on-failure routing — and leave behavioral
correctness (e.g. ``not-main`` actually refusing on ``main`` and passing on a
feature branch) to manual review.
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPT_FILE = (
    Path(__file__).resolve().parent.parent / "scripts" / "preflight.sh"
)

# Every check token the script documents, mapped to the git command (or, for
# ``github-token``, the env var) that implements it. Pinned so a future edit
# cannot silently drop a check or the command that backs it.
_CHECK_COMMANDS = {
    "work-tree": "git rev-parse --is-inside-work-tree",
    "not-main": "git rev-parse --abbrev-ref HEAD",
    "attached-head": "git symbolic-ref --quiet HEAD",
    "clean-tree": "git status --porcelain",
    "dirty-tree": "git status --porcelain",
    "github-token": "GITHUB_TOKEN",
}

# ``clean-tree`` and ``dirty-tree`` both capture ``git status --porcelain``
# into ``status`` (guarded — see the fail-closed pins below) and test it with
# opposite polarity; pin both predicates so a future edit cannot collapse them
# into one check.
_CLEAN_TREE_PREDICATE = '-n "$status"'
_DIRTY_TREE_PREDICATE = '-z "$status"'

# ``work-tree`` must evaluate the probe *output*, not just its exit code:
# ``git rev-parse --is-inside-work-tree`` also exits 0 from inside the ``.git/``
# directory, where it prints ``false``. An exit-code-only check (stdout sent to
# ``/dev/null``) passes that case, letting a command run from inside ``.git/``
# slip through the work-tree precondition. Pin the comparison against ``"true"``
# so the ``false`` case is refused.
_WORK_TREE_OUTPUT_PREDICATE = '"$inside" != "true"'

# ``attached-head`` rejects a detached HEAD. It uses ``git symbolic-ref --quiet
# HEAD`` rather than ``not-main``'s ``git rev-parse --abbrev-ref HEAD``: the
# latter prints the literal string ``HEAD`` when detached, so it cannot tell "on
# a branch named HEAD" from "detached", whereas ``symbolic-ref`` exits non-zero in
# detached state (and on any probe failure), making the check fail closed without
# a capture-into-variable guard. Pin its stderr diagnostic and ``return 1`` so a
# future edit cannot turn the detached-HEAD refusal into a silent pass.
_ATTACHED_HEAD_REFUSAL = re.compile(
    r'echo "preflight: HEAD is detached[^"\n]*" >&2\s*\n\s*return 1'
)

# ``check_github_token`` must lift GITHUB_TOKEN alone out of .env — not blanket-
# export every variable (fix #1189). A bare ``set -a`` / ``set +a`` pair around a
# ``source "$env_file"`` would export all of .env into the preflight process and
# any later checks in the same invocation. Fix #1636 dropped the ``source``
# altogether — the check now reads the literal ``GITHUB_TOKEN=`` line — so pinning
# the standalone directives as forbidden is a forward-looking guard: it keeps a
# future edit from re-introducing a whole-file source under ``set -a``, and keeps
# GITHUB_TOKEN the sole export, so this sibling of scripts/with-env.sh cannot drift
# back to the blanket export.
_BLANKET_EXPORT_DIRECTIVES = ("set -a", "set +a")
_GITHUB_TOKEN_ONLY_EXPORT = frozenset({"GITHUB_TOKEN"})

# The Compose-matching value normalization (fix #1636 follow-up), mirrored from
# ``scripts/with-env.sh``. Docker Compose's env_file parser trims surrounding
# whitespace and strips a matched pair of surrounding quotes; the literal
# ``grep``/prefix-strip read does neither on its own, so a quoted/padded
# ``GITHUB_TOKEN="ghp_…"`` in .env would pass ``check_github_token``'s non-empty
# check yet carry literal quotes/whitespace that later ``gh`` / ``git`` calls (via
# with-env.sh) reject. The two sibling scripts must normalize identically or drift.
# These markers pin that the value is trimmed (the ``[![:space:]]`` idiom) and
# stripped of one matched single/double quote pair (a ``case`` on the value).
_NORMALIZE_MARKERS = (
    "%%[![:space:]]*",  # leading-whitespace trim
    "##*[![:space:]]",  # trailing-whitespace trim
    'case "$GITHUB_TOKEN" in',  # matched-quote-pair dispatch
    r'\"*\"',  # double-quoted branch label
    r"\'*\'",  # single-quoted branch label
)

# Every git probe whose output feeds a check predicate must be guarded so a
# failed probe (not a work tree, unborn HEAD) fails the check instead of
# reading as empty output — empty output would pass ``not-main`` and
# ``clean-tree``, the exact fail-open path these guards close.
_FAIL_CLOSED_GUARDS = (
    'if ! inside="$(git rev-parse --is-inside-work-tree 2>/dev/null)"; then',
    'if ! branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"; then',
    'if ! status="$(git status --porcelain)"; then',
    'if ! root="$(git rev-parse --show-toplevel 2>/dev/null)"; then',
)


def _read() -> str:
    return _SCRIPT_FILE.read_text(encoding="utf-8")


def _case_line(content: str, token: str) -> str:
    """Return the ``case`` branch line that routes ``token``, or "" if absent."""
    match = re.search(rf"(?m)^\s*{re.escape(token)}\)\s.*$", content)
    return match.group(0) if match else ""


def test_every_check_token_is_routed_and_exits_nonzero_on_failure() -> None:
    """Each check token has a ``case`` branch that exits non-zero on failure.

    A dropped branch would route the token to the ``*)`` catch-all (a hard
    error) or, worse, silently disable the check. Pinning the per-token
    ``|| exit 1`` routing guards against a future edit dropping a check's
    failure signal.
    """
    content = _read()
    for token in _CHECK_COMMANDS:
        line = _case_line(content, token)
        assert line, (
            f"scripts/preflight.sh is missing a 'case' branch for check token "
            f"{token!r} (expected a line beginning '{token})'). All of "
            f"{tuple(_CHECK_COMMANDS)!r} must be routed."
        )
        assert "exit 1" in line, (
            f"scripts/preflight.sh check token {token!r} must exit non-zero on "
            f"failure (expected '|| exit 1' on its 'case' branch line). Without "
            f"it a failed precondition could pass silently."
        )


def test_every_check_token_maps_to_its_git_command() -> None:
    """Each check token's underlying git command (or env var) is present.

    Pinned together so dropping the command that implements a check — leaving the
    token routed but inert — fails the test.
    """
    content = _read()
    for token, command in _CHECK_COMMANDS.items():
        assert command in content, (
            f"scripts/preflight.sh check token {token!r} is missing its "
            f"implementing command {command!r}. The token would be routed but "
            f"perform no actual check."
        )


def test_clean_and_dirty_tree_keep_opposite_polarity() -> None:
    """``clean-tree`` and ``dirty-tree`` share a command but opposite predicates.

    Both capture ``git status --porcelain`` into ``status``; ``clean-tree``
    refuses when it is non-empty (``-n``), ``dirty-tree`` when it is empty
    (``-z``). Pinning both predicates keeps a future edit from collapsing the
    two checks into one polarity.
    """
    content = _read()
    assert _CLEAN_TREE_PREDICATE in content, (
        f"scripts/preflight.sh clean-tree must refuse a dirty tree via "
        f"{_CLEAN_TREE_PREDICATE!r}."
    )
    assert _DIRTY_TREE_PREDICATE in content, (
        f"scripts/preflight.sh dirty-tree must refuse a clean tree via "
        f"{_DIRTY_TREE_PREDICATE!r}."
    )


def test_git_probes_fail_closed_on_command_failure() -> None:
    """Every git probe feeding a predicate is guarded against probe failure.

    Unguarded command substitution turns a failed probe into empty output: an
    unborn-HEAD ``main`` passes ``not-main`` (empty branch is not ``"main"``)
    and a failed ``git status`` passes ``clean-tree`` (empty output reads as
    clean). Pinning the ``if ! var="$(...)"`` guard on each probe keeps the
    checks failing closed.
    """
    content = _read()
    for guard in _FAIL_CLOSED_GUARDS:
        assert guard in content, (
            f"scripts/preflight.sh must guard its git probe with {guard!r} so a "
            f"failed probe fails the check instead of passing on empty output."
        )


def test_work_tree_evaluates_probe_output_against_true() -> None:
    """``check_work_tree`` must reject the ``.git/``-subdirectory fail-open case.

    ``git rev-parse --is-inside-work-tree`` exits 0 both inside the work tree
    (printing ``true``) and inside the ``.git/`` directory itself (printing
    ``false``). Testing only the exit code — redirecting stdout to ``/dev/null`` —
    accepts both, letting a command run from inside ``.git/`` pass the work-tree
    precondition the script's header promises fails closed. Pin that the check
    captures the probe output and compares it against ``"true"`` so the
    ``false`` case is refused (the fail-closed capture guard itself is pinned in
    ``test_git_probes_fail_closed_on_command_failure``).
    """
    content = _read()
    assert _WORK_TREE_OUTPUT_PREDICATE in content, (
        f"scripts/preflight.sh check_work_tree must evaluate the probe output "
        f"via {_WORK_TREE_OUTPUT_PREDICATE!r}, not just its exit code — "
        f"'git rev-parse --is-inside-work-tree' exits 0 inside '.git/' too "
        f"(printing 'false'), so an exit-code-only check fails open there."
    )


def test_attached_head_refuses_detached_head_on_stderr() -> None:
    """``check_attached_head`` must refuse a detached HEAD loudly.

    ``merge-main`` requires ``attached-head`` so its later
    ``git rev-parse --abbrev-ref HEAD`` uses (step 7's non-fast-forward recovery
    cycle and the *Originating plan* derivation) resolve to a real branch name; in
    detached HEAD that probe returns the literal ``HEAD`` and the recovery cycle
    would silently target origin's default branch. Pin that the check emits its
    detached-HEAD diagnostic to stderr (``>&2``) and ``return 1`` so the refusal
    cannot regress into a silent pass. The token's ``case``-branch routing and its
    ``git symbolic-ref --quiet HEAD`` probe are pinned by the ``_CHECK_COMMANDS``
    tests above; the attached-branch pass (default ``return 0``) is left to manual
    review, matching this module's static-text discipline.
    """
    content = _read()
    assert _ATTACHED_HEAD_REFUSAL.search(content), (
        "scripts/preflight.sh check_attached_head must route its detached-HEAD "
        "diagnostic to stderr ('>&2') and 'return 1' so the check fails closed "
        "instead of letting a detached HEAD pass silently."
    )


def test_unknown_check_token_fails_loud() -> None:
    """An unrecognised check token must hit a non-zero ``*)`` catch-all.

    A silently-ignored unknown token would let a typo'd precondition pass as a
    no-op; the fallthrough must fail loud instead.
    """
    content = _read()
    assert re.search(r"(?m)^\s*\*\)", content), (
        "scripts/preflight.sh is missing the '*)' catch-all branch for unknown "
        "check tokens."
    )
    assert "unknown check" in content, (
        "scripts/preflight.sh '*)' catch-all must emit an 'unknown check' "
        "diagnostic and exit non-zero rather than silently ignoring a typo."
    )


def test_github_token_check_exports_only_github_token() -> None:
    """``check_github_token`` lifts GITHUB_TOKEN alone — no blanket .env export.

    Like ``scripts/with-env.sh`` (fix #1189), the check previously sourced the
    whole file under a bare ``set -a`` / ``set +a`` pair, exporting every .env
    variable into the preflight process and any later checks in the same
    invocation. Fix #1636 dropped the ``source`` entirely — the check now reads
    the literal ``GITHUB_TOKEN=`` line (pinned by
    ``test_github_token_check_reads_token_without_sourcing_env``) — so this pin is
    now a forward-looking guard against the blanket export creeping back. Pin
    (a) that no standalone export-all directive survives and (b) that ``export``
    lifts out exactly {GITHUB_TOKEN}, so the two sibling scripts cannot drift back
    to the blanket export. The ``github-token -> GITHUB_TOKEN`` mapping in
    ``_CHECK_COMMANDS`` still holds because ``GITHUB_TOKEN`` remains referenced.
    """
    content = _read()
    exported: set[str] = set()
    for raw in content.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        assert stripped not in _BLANKET_EXPORT_DIRECTIVES, (
            f"scripts/preflight.sh line {stripped!r} is a standalone export-all "
            f"directive; sourcing .env under it exports every variable into the "
            f"preflight process. check_github_token reads the literal "
            f"GITHUB_TOKEN= line instead — keep the blanket `set -a` out so only "
            f"GITHUB_TOKEN is lifted."
        )
        for match in re.finditer(r"\bexport\s+([A-Za-z_][A-Za-z0-9_]*)", stripped):
            exported.add(match.group(1))
    assert exported == set(_GITHUB_TOKEN_ONLY_EXPORT), (
        f"scripts/preflight.sh must export exactly "
        f"{sorted(_GITHUB_TOKEN_ONLY_EXPORT)!r}, but exports {sorted(exported)!r}. "
        f"Exporting any other .env variable widens check_github_token's blast "
        f"radius beyond the one token it needs."
    )


def test_github_token_check_reads_token_without_sourcing_env() -> None:
    """``check_github_token`` reads GITHUB_TOKEN literally — never sources .env.

    Like ``scripts/with-env.sh`` (fix #1636), sourcing .env evaluates the file as
    shell — expanding ``$…`` (mangling a value Docker Compose accepts literally)
    and running any ``$(…)`` / backtick line — diverging from Compose's literal
    ``env_file`` parse. Pin that the check no longer sources or evaluates .env (no
    ``source "$env_file"``, no ``eval`` of the file) and instead extracts
    GITHUB_TOKEN with a non-evaluating line read of the ``GITHUB_TOKEN=`` entry, so
    the two sibling scripts stay aligned on the literal read. (The standalone
    ``set -a`` export-all directive is already banned by
    ``test_github_token_check_exports_only_github_token``.) The guard also rejects
    the POSIX dot-source form ``. "$env_file"``, which evaluates .env identically to
    ``source`` — same code-execution/value-mangling risk — despite containing
    neither ``source`` nor ``eval``, mirroring the with-env.sh sibling.
    """
    lines = [
        stripped
        for raw in _read().splitlines()
        if (stripped := raw.strip()) and not stripped.startswith("#")
    ]
    for line in lines:
        assert 'source "$env_file"' not in line, (
            f"scripts/preflight.sh line {line!r} still sources .env in "
            f"check_github_token; sourcing evaluates the file as shell. Read "
            f"GITHUB_TOKEN literally to match Docker Compose's env_file parse."
        )
        assert not re.search(r'(^|\s)\.\s+"\$env_file"', line), (
            f"scripts/preflight.sh line {line!r} dot-sources .env; the POSIX "
            f"`. \"$env_file\"` form evaluates the file exactly like `source`, "
            f"re-introducing the code-execution/value-mangling fix #1636 removes. "
            f"Read GITHUB_TOKEN literally instead."
        )
        assert not re.search(r"\beval\b.*env_file", line), (
            f"scripts/preflight.sh line {line!r} eval's the .env file; read "
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
        "scripts/preflight.sh check_github_token must extract GITHUB_TOKEN with a "
        "non-evaluating line read of the 'GITHUB_TOKEN=' entry in \"$env_file\" "
        "(e.g. grep, awk, or sed — any text extractor that does not evaluate "
        ".env), matching Docker Compose's literal env_file parse."
    )


def test_github_token_check_normalizes_quoted_or_padded_token() -> None:
    """``check_github_token`` trims whitespace and strips one matched quote pair.

    Like ``scripts/with-env.sh`` (fix #1636), the literal read must normalize the
    value the way Docker Compose's env_file parser does — trim surrounding
    whitespace and strip a matched pair of surrounding quotes. Without it a
    quoted/padded ``GITHUB_TOKEN="ghp_…"`` in .env passes the non-empty check here
    (so preflight reports success) yet carries literal quotes/whitespace that later
    ``gh`` / ``git`` calls (via with-env.sh) reject — and the two sibling scripts,
    which are required not to drift, would normalize differently. Pin that the check
    trims surrounding whitespace (the ``[![:space:]]`` parameter-expansion idiom) and
    strips one matched single/double quote pair (a ``case`` on the value). Escape /
    ``$``-expansion inside quotes are intentionally NOT replicated (tokens are
    ``[A-Za-z0-9_]``), matching the with-env.sh pin rather than a full dotenv re-parse.
    """
    content = _read()
    for marker in _NORMALIZE_MARKERS:
        assert marker in content, (
            f"scripts/preflight.sh check_github_token must normalize the extracted "
            f"GITHUB_TOKEN the way Docker Compose's env_file parse does (trim "
            f"surrounding whitespace, strip one matched quote pair) but is missing "
            f"{marker!r}; a quoted/padded value would otherwise pass the non-empty "
            f"check yet fail later gh/git auth, and the two sibling scripts would "
            f"drift."
        )

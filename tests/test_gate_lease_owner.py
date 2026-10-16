r"""AST/regex regression: no gate site may hardcode a worker count.

Proposal 126, step 6. "How wide may a gate run be?" is a single fact owned by
:mod:`maestro.gate_capacity` (``plan_lease`` + the ``measure_host_capacity`` /
``count_in_flight`` readers), reachable through the ``maestro gate-lease`` exec
wrapper behind ``[loop] gate_lease_enabled``. The sibling
``tests/test_gate_command_owner.py`` already forbids re-hardcoding a gate
*path*; this guard is its worker-count twin, forbidding a gate site from naming
``-n auto`` / ``-n <int>`` instead of taking the width from the lease.

It scans the same eight gate sites the shared owner names
(:mod:`tests.support.gate_sites`), in two halves:

* **Python half** — AST-walk the four in-process readers
  (:data:`~tests.support.gate_sites.PYTHON_GATE_READERS`) for a ``list``/``tuple``
  literal whose *first* element is the string constant ``"pytest"`` and which
  names a concrete xdist worker count (``-n auto`` / ``-n 4``, adjacent or
  combined). Gating on the first element being ``"pytest"`` mirrors the sibling
  guard, so the unrelated ``["git", "grep", "-n", "--fixed-strings"]`` list in
  ``maestro/evolution.py`` is not a false positive, and a routed ``{worker_args}``
  placeholder is not a concrete count so it never trips.
* **Text half** — line-enumerate the shell runner and the three hand-run docs,
  dropping comment lines (the ``(?m)^(?!\s*#)`` technique from
  ``tests/test_git_push_tracked_script.py``, so the script's own justification
  comments cannot trip the scan) and matching each hit against an exclusive
  frozenset carrying **one** carve-out: the exact flag-off rollback line in
  ``gate_maestro`` (``-n auto`` survives there as the deliberate rollback path).
  The scanner is pure over a ``dict[str, str]`` of ``{relpath: text}`` (the
  shape of ``tests/test_rank_bm25_retired.py``), so the seeded offender stays an
  in-memory dict.

Kept a bespoke module rather than a ``tests/structural_guards.py`` entry: that
harness resolves paths only under ``maestro/`` and carries a population budget,
whereas this guard's sites span ``scripts/`` and ``.claude/commands/`` too.
"""

from __future__ import annotations

import ast
import re

import pytest

from tests.support.gate_argv import gate_tool
from tests.support.gate_argv import string_constant as _string_constant
from tests.support.gate_sites import (
    COMMAND_DOC_GATE_SITES,
    MAESTRO_ROOT,
    PYTHON_GATE_READERS,
    REPO_ROOT,
    SHELL_GATE_RUNNER,
)

# ---------------------------------------------------------------------------
# Python half — no reader may bake a worker count into a pytest command literal.
# ---------------------------------------------------------------------------


def _is_count_value(value: str | None) -> bool:
    """True iff *value* is a concrete xdist worker count (``auto`` or an int).

    A routed ``{worker_args}`` placeholder is deliberately *not* a concrete count,
    so a reader that later routes through the wrapper is never a false positive.
    """
    return value == "auto" or (value is not None and value.isdigit())


def _is_hardcoded_worker_count(node: ast.AST) -> bool:
    """True iff *node* is a ``pytest`` command literal naming a concrete count.

    The signature: a ``list``/``tuple`` literal that invokes ``"pytest"``
    (tool-first or ``[sys.executable, "-m", "pytest", ...]``, located by
    ``gate_tool``) and which names an xdist worker count either as
    the two adjacent tokens ``"-n"``, ``"auto"`` / ``"4"`` or as one combined
    token ``"-nauto"`` / ``"-n4"`` / ``"-n=auto"``. The tool gate keeps
    the ``git grep -n`` list from tripping, and ``{worker_args}`` — not a concrete
    count — never matches.
    """
    if gate_tool(node) != "pytest":
        return False
    assert isinstance(node, (ast.List, ast.Tuple))
    consts = [_string_constant(elt) for elt in node.elts]
    for index, value in enumerate(consts):
        # `-n auto` / `-n 4` as two adjacent tokens.
        if (
            value == "-n"
            and index + 1 < len(consts)
            and _is_count_value(consts[index + 1])
        ):
            return True
        # `-nauto` / `-n4` / `-n=auto` as one combined token.
        if value is not None and re.fullmatch(r"-n=?(auto|\d+)", value):
            return True
    return False


def _py_violators(source: str, rel: str) -> list[str]:
    """Return ``rel:lineno`` for every hardcoded worker-count literal in *source*."""
    tree = ast.parse(source, filename=rel)
    return [
        f"{rel}:{node.lineno}"
        for node in ast.walk(tree)
        if _is_hardcoded_worker_count(node)
    ]


# ---------------------------------------------------------------------------
# Text half — the shell runner and the hand-run docs may name no worker count,
# save the one carved-out flag-off rollback line.
# ---------------------------------------------------------------------------

# A concrete worker count as spelled in shell/prose: `-n auto` / `-n <int>` as two
# tokens, *or* the combined `-nauto` / `-n4` / `-n=auto` forms the Python half's
# `re.fullmatch(r"-n=?(auto|\d+)")` also catches — the whitespace is optional so the
# two halves stay at parity and a combined token cannot slip past the text scan. A
# routed `{worker_args}` placeholder still does not match — it carries no `-n`, so
# the width is spliced by `maestro gate-lease` from `gate_capacity.worker_args`.
_WORKER_COUNT_RE = re.compile(r"-n\s*=?(auto|\d)")

# Non-comment content lines only, so a `# ... -n auto ...` justification comment
# cannot trip the scan (mirrors tests/test_git_push_tracked_script.py).
_NON_COMMENT_LINE_RE = re.compile(r"(?m)^(?!\s*#).*$")

# The single carve-out: gate_maestro's flag-off branch keeps `-n auto` verbatim
# as the deliberate rollback path. Compared stripped, so indentation is ignored.
_FLAG_OFF_ROLLBACK_LINE = "$PY -m pytest tests/ -v -n auto --dist loadfile && return 0"
_TEXT_CARVE_OUTS: frozenset[str] = frozenset({_FLAG_OFF_ROLLBACK_LINE})


def _text_violators(sources: dict[str, str]) -> list[str]:
    """Return ``relpath:lineno`` for every worker-count line outside the carve-out.

    Pure over an in-memory ``{relpath: text}`` map so a seeded offender need not
    touch disk. Comment lines are dropped; a hit whose stripped text is the
    carved-out rollback line is allowed.
    """
    violators: list[str] = []
    for rel, text in sorted(sources.items()):
        for match in _NON_COMMENT_LINE_RE.finditer(text):
            line = match.group(0)
            if not _WORKER_COUNT_RE.search(line):
                continue
            if line.strip() in _TEXT_CARVE_OUTS:
                continue
            lineno = text.count("\n", 0, match.start()) + 1
            violators.append(f"{rel}:{lineno}")
    return violators


def _text_sources() -> dict[str, str]:
    """The real shell runner + hand-run docs as a ``{relpath: text}`` map."""
    sites = [SHELL_GATE_RUNNER, *COMMAND_DOC_GATE_SITES]
    return {
        str(path.relative_to(REPO_ROOT)): path.read_text(encoding="utf-8")
        for path in sites
    }


# ---------------------------------------------------------------------------
# Guards.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(15)
def test_no_gate_site_names_a_worker_count() -> None:
    """No gate site may hardcode a worker count instead of using the lease.

    Both halves over the real tree: the four readers must build their pytest
    argv without a baked ``-n auto`` / ``-n <int>``, and the shell runner + the
    three hand-run docs must take their width from ``maestro gate-lease`` — save
    the single carved-out flag-off rollback line in ``gate_maestro``.
    """
    violators: list[str] = []
    for rel in PYTHON_GATE_READERS:
        path = MAESTRO_ROOT / rel
        violators.extend(_py_violators(path.read_text(encoding="utf-8"), rel))
    violators.extend(_text_violators(_text_sources()))

    assert not violators, (
        "Hardcoded quality-gate worker count re-introduced (proposal 126):\n  "
        + "\n  ".join(violators)
        + "\n\nTake the worker width from the gate execution lease "
        "(`maestro gate-lease --` splices `{worker_args}`) rather than naming "
        "`-n auto` / `-n <int>` at a gate site."
    )


#: The one call site whose rows ``gate_capacity.derive_deadline`` reads — the
#: loop's own budgeted suite run, which keeps the default ``source="in_process"``.
_BUDGETED_GATE_CALLER = "evolution_quality.py"


def _lease_sites_missing_source(text: str, rel: str) -> list[str]:
    """Rows for every ``lease_for_gate_run(...)`` call that names no ``source=``."""
    missing: list[str] = []
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(
            func, "id", "",
        )
        if name != "lease_for_gate_run":
            continue
        if not any(kw.arg == "source" for kw in node.keywords):
            missing.append(f"{rel}:{node.lineno}")
    return missing


@pytest.mark.timeout(15)
def test_only_the_budgeted_caller_keeps_the_default_lease_source() -> None:
    """Every lease call site but the loop's own suite run must name its ``source``.

    ``gate_capacity.derive_deadline`` reads only ``source="in_process"`` rows at
    the granted width — the runs a lease-granted deadline was actually enforced
    on. A sibling reader (``preflight``, ``gate_check``) or the hand-run wrapper
    spends the flat ``selfcheck`` constant, so a row of theirs landing as
    ``"in_process"`` between two budgeted suite runs resets a converging
    escalation to the floor and the budget never reaches a fit. Naming the source
    at each of those sites is what keeps them out of the derivation's input, and
    the default must stay reserved for the one caller it describes.
    """
    violators: list[str] = []
    for rel in (*PYTHON_GATE_READERS, "cli.py"):
        if rel == _BUDGETED_GATE_CALLER:
            continue
        path = MAESTRO_ROOT / rel
        violators.extend(
            _lease_sites_missing_source(path.read_text(encoding="utf-8"), rel),
        )

    assert not violators, (
        "lease_for_gate_run call site left on the default source (proposal 134):"
        "\n  " + "\n  ".join(violators)
        + "\n\nPass an explicit `source=` naming this caller; only "
        f"`{_BUDGETED_GATE_CALLER}`'s budgeted suite run may record "
        "`in_process`, the rows `derive_deadline` reads."
    )


def test_lease_source_guard_flags_a_synthetic_default_call() -> None:
    """Drift in the helper would silently pass real violators (non-vacuity)."""
    module = (
        "gate_capacity.lease_for_gate_run(d, runs_path=p)\n"
        "gate_capacity.lease_for_gate_run(d, source=\"wrapper\")\n"
        "unrelated_call(d)\n"
    )
    assert _lease_sites_missing_source(module, "synthetic.py") == ["synthetic.py:1"]


def test_python_guard_flags_a_synthetic_worker_count() -> None:
    """A hand-written baked worker count is detected (Python-half non-vacuity).

    Drift in the AST helper would silently make the guard above pass on real
    violators. Parse a synthetic module and assert the helper flags exactly the
    baked-count rows — and that the count-less pytest row, the ``git grep -n``
    list, and the routed ``{worker_args}`` placeholder are *not* flagged.
    """
    source = (
        "GATES = [\n"
        '    ["pytest", "-n", "auto", "-v"],\n'
        '    ["pytest", "-n", "4"],\n'
        '    ["pytest", "-nauto"],\n'
        '    [sys.executable, "-m", "pytest", t, "-n", "auto"],\n'
        "]\n"
        "source = wt.gate_source_path\n"
        "CLEAN = [\n"
        '    ["pytest", "-v", source],\n'
        '    [sys.executable, "-m", "pytest", t, "-v"],\n'
        '    ["git", "grep", "-n", "--fixed-strings"],\n'
        '    ["pytest", "{worker_args}"],\n'
        "]\n"
    )
    tree = ast.parse(source)
    flagged = [
        node.lineno
        for node in ast.walk(tree)
        if _is_hardcoded_worker_count(node)
    ]
    # The four baked-count rows on lines 2-5 (including the interpreter-first
    # row) are flagged; the variable-based pytest rows, the git-grep list, and
    # the {worker_args} placeholder are not.
    assert flagged == [2, 3, 4, 5]


def test_text_guard_flags_a_seeded_worker_count() -> None:
    """A baked worker count in the carve-out file itself is detected.

    Positive control: the clean real tree yields no text violators. Then seed an
    offender into the carve-out file's *own* text, on a line other than the
    carved-out rollback line, so the carve-out cannot be widened into a hole that
    whitelists the whole file. The seeded literal is split (``"-n " + "4"``) so
    this test file never carries a contiguous match for its own scan.
    """
    sources = _text_sources()
    # Positive control — the clean tree is silent.
    assert _text_violators(sources) == []

    shell_rel = str(SHELL_GATE_RUNNER.relative_to(REPO_ROOT))
    seeded_line = "  $PY -m pytest tests/ -v " + "-n " + "4" + " --dist loadfile"
    seeded = dict(sources)
    seeded[shell_rel] = sources[shell_rel] + "\n" + seeded_line + "\n"

    violators = _text_violators(seeded)
    assert violators, "seeded worker-count line was not detected"
    assert all(v.startswith(shell_rel + ":") for v in violators)


def test_text_guard_flags_a_combined_worker_count() -> None:
    """A combined `-n4` / `-nauto` / `-n=auto` token is caught, not just a spaced one.

    Parity with the Python half's ``re.fullmatch(r"-n=?(auto|\\d+)")``: a gate site
    that spells the count as one token (``-nauto``) rather than two (``-n auto``) is
    an equally valid xdist form, so the text scan must catch it too or a re-hardcoded
    combined count slips past the guard. Each seeded token is split so this test file
    carries no contiguous match of its own.
    """
    sources = _text_sources()
    shell_rel = str(SHELL_GATE_RUNNER.relative_to(REPO_ROOT))
    for token in ("-n" + "4", "-n" + "auto", "-n=" + "auto"):
        seeded_line = "  $PY -m pytest tests/ -v " + token + " --dist loadfile"
        seeded = dict(sources)
        seeded[shell_rel] = sources[shell_rel] + "\n" + seeded_line + "\n"
        violators = _text_violators(seeded)
        assert violators, f"combined worker-count token {token!r} was not detected"
        assert all(v.startswith(shell_rel + ":") for v in violators)


def test_gate_sites_exist() -> None:
    """Every scanned gate site resolves to a real file (non-vacuity floor).

    Guards against a rename silently emptying a scan set and hiding a
    re-hardcoded worker count (mirrors the sibling guard's existence check).
    """
    for rel in PYTHON_GATE_READERS:
        assert (MAESTRO_ROOT / rel).is_file(), f"missing reader module: {rel}"
    assert SHELL_GATE_RUNNER.is_file(), f"missing shell runner: {SHELL_GATE_RUNNER}"
    for doc in COMMAND_DOC_GATE_SITES:
        assert doc.is_file(), f"missing command doc: {doc}"


# ---------------------------------------------------------------------------
# Deadline half — no gate reader may name a literal budget, and exactly one
# site derives one from the lease (proposal 134).
# ---------------------------------------------------------------------------


def _literal_timeout_sites(source: str, rel: str) -> list[str]:
    """Rows for every call in *source* passing a numeric-literal ``timeout=``.

    "How long may a gate run take?" is the lease's fact, served through
    ``gate_capacity.deadline_or_default``. A call that spells its budget as a
    bare number instead re-creates the mismatch the derived deadline exists to
    remove — a green suite killed by a ceiling calibrated for another machine.
    ``bool`` is excluded explicitly because it is an ``int`` subclass, so a
    ``timeout=True`` would otherwise read as a literal budget.

    The scan is deliberately **module-wide**, not scoped to the gate-command
    call sites: deciding which ``subprocess.run`` in an 8.6k-line
    ``evolution.py`` is "the gate one" is exactly the judgement that drifts, so
    the rule instead holds these four readers to naming no bare numeric budget
    at all. An unrelated literal arriving later is a real hit, and its fix is
    the cheap one the failure message spells out — hoist the number to a
    ``Config`` field or a module constant, which is where a tunable budget
    belongs regardless of this guard.
    """
    rows: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "timeout":
                continue
            value = kw.value
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, int | float)
                and not isinstance(value.value, bool)
            ):
                rows.append(f"{rel}:{node.lineno}")
    return rows


@pytest.mark.timeout(15)
def test_no_gate_reader_names_a_literal_timeout() -> None:
    """No in-process gate reader may bake a numeric subprocess budget.

    Scoped to the four readers rather than the tree: ``contributions.py``,
    ``audit_checks.py``, ``config.py`` and ``notifications/telegram.py`` carry
    legitimate literal timeouts on non-gate calls. None of the four readers has
    a literal today, so the live scan starts clean with no carve-out — every
    budget there flows from a config attribute or from the lease.
    """
    violators: list[str] = []
    for rel in PYTHON_GATE_READERS:
        path = MAESTRO_ROOT / rel
        violators.extend(
            _literal_timeout_sites(path.read_text(encoding="utf-8"), rel),
        )

    assert not violators, (
        "gate reader names a literal subprocess timeout (proposal 134):"
        "\n  " + "\n  ".join(violators)
        + "\n\nThis rule is module-wide: these four readers may name no bare "
        "numeric `timeout=` anywhere, gate-related or not, because which call "
        "is 'the gate one' is the judgement that drifts. For a gate run, take "
        "the budget from `gate_capacity.deadline_or_default(lease, …)`; for any "
        "other call, hoist the number to a `Config` field or a module constant "
        "— a bare number is a ceiling calibrated for one machine."
    )


def _callee_name(node: ast.Call) -> str:
    """The called symbol's own name, whether spelled bare or as an attribute.

    ``self.run_quality_gates(...)`` and a bare ``run_quality_gates(...)`` are the
    same call for every rule here, so resolving the name once keeps a rule from
    seeing only one of the two spellings.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _deadline_reader_sites(source: str, rel: str) -> list[str]:
    """Rows for every ``deadline_or_default(...)`` call in *source*."""
    return [
        f"{rel}:{node.lineno}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and _callee_name(node) == "deadline_or_default"
    ]


@pytest.mark.timeout(15)
def test_exactly_one_reader_derives_the_lease_deadline() -> None:
    """The positive twin of the literal-timeout rule: one derivation, one reader.

    ``test_no_gate_reader_names_a_literal_timeout`` forbids the wrong source of
    a budget; this pins the right one to a single site. A second reader deriving
    its own budget beside the lease would not name a literal — it would quietly
    diverge from the one run whose rows ``derive_deadline`` reads.
    """
    sites: list[str] = []
    for rel in PYTHON_GATE_READERS:
        path = MAESTRO_ROOT / rel
        sites.extend(
            _deadline_reader_sites(path.read_text(encoding="utf-8"), rel),
        )

    assert len(sites) == 1, (
        "`gate_capacity.deadline_or_default` must be called at exactly one gate "
        f"reader site (proposal 134); found {len(sites)}:\n  "
        + "\n  ".join(sites)
    )
    assert sites[0].startswith(_BUDGETED_GATE_CALLER + ":"), (
        "the lease deadline must be derived in "
        f"`{_BUDGETED_GATE_CALLER}` — the budgeted suite run — not at "
        f"{sites[0]}."
    )


@pytest.mark.timeout(15)
def test_deadline_helpers_flag_synthetic_sources() -> None:
    """Drift in either deadline helper would silently pass real violators."""
    literal_module = (
        "run(cmd, timeout=300)\n"
        "run(cmd, timeout=1.5)\n"
        "run(cmd, timeout=config.git_timeout)\n"
        "run(cmd, timeout=deadline)\n"
        "run(cmd, check=True)\n"
        "run(cmd, timeout=True)\n"
    )
    assert _literal_timeout_sites(literal_module, "synthetic.py") == [
        "synthetic.py:1", "synthetic.py:2",
    ]

    reader_module = (
        "gate_capacity.deadline_or_default(lease, selfcheck)\n"
        "deadline_or_default(None, 300)\n"
        "other_call(lease, 300)\n"
    )
    assert _deadline_reader_sites(reader_module, "synthetic.py") == [
        "synthetic.py:1", "synthetic.py:2",
    ]


# ---------------------------------------------------------------------------
# Scope half — exactly one narrowed gate call, and the certifying reads are
# provably whole (proposal 134).
# ---------------------------------------------------------------------------


def _scope_passing_calls(source: str, rel: str) -> list[tuple[str, ast.expr]]:
    """``(row, value)`` for every ``run_quality_gates`` call naming ``scope=``."""
    found: list[tuple[str, ast.expr]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if _callee_name(node) != "run_quality_gates":
            continue
        for kw in node.keywords:
            if kw.arg == "scope":
                found.append((f"{rel}:{node.lineno}", kw.value))
    return found


@pytest.mark.timeout(15)
def test_only_the_fix_round_narrows_the_gate_scope() -> None:
    """Exactly one ``run_quality_gates`` call narrows, and it names a variable.

    The impacted-scope fix loop rests on two reads staying whole: the fresh
    full-scope, with-coverage **certification read** that is the only path to a
    ``green`` terminal, and ``_convergence_terminal``'s **re-read** that
    re-derives the outcome before a plan is demoted. Narrow either and a
    subset-green is promoted to a terminal verdict, or a plan is demoted on a
    partial read — both silent, both shipping green. One narrowed call, whose
    value is the round's ``impacted`` selection rather than a constant, is what
    keeps that arithmetic honest.
    """
    found: list[tuple[str, ast.expr]] = []
    for path in sorted(MAESTRO_ROOT.rglob("*.py")):
        rel = str(path.relative_to(MAESTRO_ROOT))
        found.extend(
            _scope_passing_calls(path.read_text(encoding="utf-8"), rel),
        )

    rows = [row for row, _ in found]
    assert len(rows) == 1, (
        "exactly one `run_quality_gates(scope=…)` call may exist (proposal "
        f"134); found {len(rows)}:\n  " + "\n  ".join(rows)
    )
    assert rows[0].startswith(_BUDGETED_GATE_CALLER + ":"), (
        "the one narrowed gate call belongs to the fix loop's rounds driver in "
        f"`{_BUDGETED_GATE_CALLER}`, not {rows[0]}."
    )
    assert isinstance(found[0][1], ast.Name), (
        f"the narrowed scope at {rows[0]} must be the round's selection "
        "variable, not a hardcoded literal."
    )


#: The fix loop's rounds driver, and the local name it binds its certifying
#: full-scope read to — the two anchors the by-name scope rules resolve.
_FIX_ROUNDS_DRIVER = "_converge_rounds"
_CERTIFICATION_NAME = "certification"


def _certification_reads(scope: ast.AST) -> list[ast.Call]:
    """Every ``certification = run_quality_gates(...)`` call under *scope*."""
    calls: list[ast.Call] = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if _CERTIFICATION_NAME not in names:
            continue
        if (
            isinstance(node.value, ast.Call)
            and _callee_name(node.value) == "run_quality_gates"
        ):
            calls.append(node.value)
    return calls


def _find_method(
    tree: ast.Module, name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Return the function named *name* anywhere in *tree*, or raise.

    Both function flavours are matched: the fix loop's rounds driver is ``async
    def`` while the convergence terminal is a plain ``def``, and a lookup that
    silently missed one would make its rule vacuous.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"function not found: {name}")


@pytest.mark.timeout(15)
def test_convergence_terminal_reread_is_never_narrowed() -> None:
    """``_convergence_terminal``'s re-read passes no keywords at all.

    The count rule above already catches a seeded narrowing arithmetically; this
    states the invariant by name at the site that carries it, so the failure
    points at the re-read rather than at a count that no longer balances.
    """
    source = (MAESTRO_ROOT / _BUDGETED_GATE_CALLER).read_text(encoding="utf-8")
    method = _find_method(ast.parse(source), "_convergence_terminal")
    offenders = [
        f"{_BUDGETED_GATE_CALLER}:{node.lineno}"
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and _callee_name(node) == "run_quality_gates"
        and (node.keywords or node.args)
    ]
    assert not offenders, (
        "`_convergence_terminal`'s re-read must be a whole, unnarrowed gate run "
        "(proposal 134) — a plan demoted on a partial read is a silent wrong "
        "verdict:\n  " + "\n  ".join(offenders)
    )
    reads = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and _callee_name(node) == "run_quality_gates"
    ]
    assert reads, (
        "`_convergence_terminal` no longer calls `run_quality_gates` — this "
        "rule would pass vacuously; re-point it at the re-read's new home."
    )


@pytest.mark.timeout(15)
def test_certification_read_is_never_narrowed() -> None:
    """The fix loop's certification read passes no keywords either.

    Its twin above pins ``_convergence_terminal``'s re-read; this pins the other
    read the scope machinery rests on — the fresh full-scope, with-coverage run
    that is the only path from a narrowed green to a ``green`` terminal. Both
    live in a file the count rule already balances, but a count says only that
    *some* call was narrowed, not *which*: narrowing the certification read
    while un-narrowing a round keeps the count at one and promotes a
    subset-green to a terminal verdict in silence. Naming the read makes that
    trade impossible, and points the failure at the site that carries it.
    """
    source = (MAESTRO_ROOT / _BUDGETED_GATE_CALLER).read_text(encoding="utf-8")
    method = _find_method(ast.parse(source), _FIX_ROUNDS_DRIVER)
    certifications = _certification_reads(method)
    assert len(certifications) == 1, (
        f"expected exactly one `{_CERTIFICATION_NAME} = run_quality_gates(...)` "
        f"in `{_FIX_ROUNDS_DRIVER}` (proposal 134); found "
        f"{len(certifications)} — this rule may not pass vacuously."
    )
    call = certifications[0]
    assert not call.args and not call.keywords, (
        f"the certification read at {_BUDGETED_GATE_CALLER}:{call.lineno} must "
        "be a whole, unnarrowed gate run (proposal 134) — it is the only thing "
        "that may promote a narrowed green to a terminal verdict, so a subset "
        "read there ships a suite-red plan as green."
    )


@pytest.mark.timeout(15)
def test_scope_helpers_flag_synthetic_sources() -> None:
    """Drift in the scope helper would silently pass real violators."""
    module = (
        "self.run_quality_gates(scope=impacted)\n"
        'self.run_quality_gates(scope="impacted")\n'
        "self.run_quality_gates()\n"
        "run_quality_gates(scope=selected)\n"
        "other_call(scope=impacted)\n"
    )
    found = _scope_passing_calls(module, "synthetic.py")
    assert [row for row, _ in found] == [
        "synthetic.py:1", "synthetic.py:2", "synthetic.py:4",
    ]
    assert isinstance(found[0][1], ast.Name)
    assert isinstance(found[1][1], ast.Constant)


@pytest.mark.timeout(15)
def test_certification_reader_flags_synthetic_sources() -> None:
    """Drift in the certification helper would silently pass a narrowed read."""
    module = (
        "certification = self.run_quality_gates()\n"
        "certification = self.run_quality_gates(scope=impacted)\n"
        "certification = other_call()\n"
        "review = self.run_quality_gates()\n"
    )
    calls = _certification_reads(ast.parse(module))
    assert [call.lineno for call in calls] == [1, 2]
    assert not calls[0].keywords
    assert calls[1].keywords[0].arg == "scope"


@pytest.mark.timeout(15)
def test_find_method_locates_both_function_flavours() -> None:
    """The lookup resolves ``async def`` too — the rounds driver is one."""
    tree = ast.parse("async def rounds():\n    pass\ndef plain():\n    pass\n")
    assert _find_method(tree, "rounds").name == "rounds"
    assert _find_method(tree, "plain").name == "plain"


@pytest.mark.timeout(15)
def test_find_method_raises_on_missing_name() -> None:
    """The function-scoped check fails loud rather than vacuously passing."""
    with pytest.raises(AssertionError, match="function not found"):
        _find_method(ast.parse("x = 1\n"), "absent")


# ---------------------------------------------------------------------------
# Provenance half — the deadline and cap reaching the runnability oracle can
# only come from the lease, never from a literal (proposal 134).
# ---------------------------------------------------------------------------

#: The two sites that assemble the runnability oracle's evidence — the loop-side
#: selector hold and the CLI-side ``gate-check`` refresh. Named locally rather
#: than folded into :data:`~tests.support.gate_sites.PYTHON_GATE_READERS`
#: because ``loop/plan_holds.py`` builds no pytest argv: it is an evidence
#: assembler, not a gate reader, and adding it to the shared list would put it
#: under the width and literal-timeout rules that are about running a gate.
_ORACLE_ASSEMBLY_SITES: tuple[str, ...] = ("loop/plan_holds.py", "gate_check.py")

#: The oracle's two public doors. Both take ``deadline_seconds`` and
#: ``cap_seconds`` injected, which is what keeps ``gate_environment.py`` a leaf.
_ORACLE_DOORS: frozenset[str] = frozenset({"refresh_verdict", "probe_runnability"})


def _oracle_budget_sites(
    source: str, rel: str,
) -> list[tuple[str, str, ast.expr | None]]:
    """``(row, keyword, value)`` for the budget arguments of each oracle call.

    One row per ``deadline_seconds`` / ``cap_seconds`` keyword at every
    :data:`_ORACLE_DOORS` call in *source*; a door call that omits one entirely
    yields a row whose value is ``None``, so a silently-dropped argument is a
    violation rather than an absence the scan cannot see.
    """
    rows: list[tuple[str, str, ast.expr | None]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if _callee_name(node) not in _ORACLE_DOORS:
            continue
        row = f"{rel}:{node.lineno}"
        named = {kw.arg: kw.value for kw in node.keywords}
        for arg in ("deadline_seconds", "cap_seconds"):
            rows.append((row, arg, named.get(arg)))
    return rows


def _budget_violation(
    value: ast.expr | None,
    *,
    arg: str,
    bindings: dict[str, list[ast.expr]],
    params: frozenset[str],
) -> str | None:
    """Why *value* is not lease-sourced evidence for *arg*, or ``None`` if it is.

    Both live sites spell the budget as a local name assigned in a flag branch
    (``deadline = ...`` / ``cap = ...``), so a rule that stopped at "the
    argument is a ``Name``" would accept ``deadline = 300`` — the very literal
    the rule exists to forbid. The check therefore resolves a local name one
    level, through *bindings* (every assignment to that name in the enclosing
    scope) and *params* (the scope's own arguments, which are injected evidence
    the caller already sourced). A name it cannot resolve is a violation rather
    than an absence the scan cannot see.

    ``cap_seconds=0`` is the single admitted literal: it is the oracle's
    documented "no cap known" sentinel — the *absence* of a ceiling, not a bare
    number standing in for one — and it is what the flag-off arm must pass.
    """
    if value is None:
        return "missing"
    if isinstance(value, ast.Constant):
        if arg == "cap_seconds" and value.value == 0:
            return None
        return "is a literal"
    if isinstance(value, ast.Attribute | ast.Call):
        return None
    if not isinstance(value, ast.Name):
        return "is not lease-sourced"
    if value.id in params:
        return None
    bound = bindings.get(value.id)
    if not bound:
        return f"binds `{value.id}`, which this scope never assigns"
    for inner in bound:
        why = _budget_violation(inner, arg=arg, bindings={}, params=params)
        if why is not None:
            return f"binds `{value.id}`, which {why}"
    return None


def _scope_of_calls(source: str) -> list[tuple[ast.Call, ast.AST]]:
    """Every call in *source* paired with its innermost enclosing scope.

    ``ast.walk`` alone cannot say which function a call sits in, and the
    provenance rule needs that: a name is resolved against the assignments and
    parameters of *its own* scope, never a sibling function's.
    """
    found: list[tuple[ast.Call, ast.AST]] = []

    def visit(node: ast.AST, scope: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                found.append((child, scope))
            inner = (
                child
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else scope
            )
            visit(child, inner)

    tree = ast.parse(source)
    visit(tree, tree)
    return found


def _scope_bindings(scope: ast.AST) -> dict[str, list[ast.expr]]:
    """Every value assigned to a bare name anywhere in *scope*."""
    bindings: dict[str, list[ast.expr]] = {}
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings.setdefault(target.id, []).append(value)
    return bindings


def _scope_params(scope: ast.AST) -> frozenset[str]:
    """The parameter names *scope* binds, empty for a module scope."""
    if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
        return frozenset()
    args = scope.args
    return frozenset(
        a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
    )


def _oracle_budget_violations(source: str, rel: str) -> list[str]:
    """Every budget argument at an oracle door in *source* that is not lease-sourced.

    The provenance rule proper: one row per offending ``deadline_seconds`` /
    ``cap_seconds`` at every :data:`_ORACLE_DOORS` call, with local names
    resolved against their own scope by :func:`_budget_violation`.
    """
    violations: list[str] = []
    for call, scope in _scope_of_calls(source):
        if _callee_name(call) not in _ORACLE_DOORS:
            continue
        bindings = _scope_bindings(scope)
        params = _scope_params(scope)
        named = {kw.arg: kw.value for kw in call.keywords}
        for arg in ("deadline_seconds", "cap_seconds"):
            why = _budget_violation(
                named.get(arg), arg=arg, bindings=bindings, params=params,
            )
            if why is not None:
                violations.append(f"{rel}:{call.lineno}: `{arg}=` {why}")
    return violations


def _derives_lease_deadline(source: str) -> bool:
    """Whether *source* sources a deadline from ``gate_capacity.derive_deadline``."""
    return any(
        isinstance(node, ast.Call) and _callee_name(node) == "derive_deadline"
        for node in ast.walk(ast.parse(source))
    )


@pytest.mark.timeout(15)
def test_oracle_budget_evidence_comes_from_the_lease() -> None:
    """The runnability oracle may only be handed a lease-sourced budget.

    ``probe_fit`` reads ``UNFIT`` against the budget it is given, and that
    verdict parks every eligible approved plan on an environment hold. A budget
    spelled as a bare number at either assembly site would therefore stop
    implementation against a ceiling no run was ever killed at — exactly the
    mismatch the lease exists to remove. So both budget arguments must be
    present and must resolve — through a local name's own assignments, since
    that is how both live sites spell their flag branches — to a config field or
    a call result such as ``derive_deadline``'s own return. The one admitted
    literal is ``cap_seconds=0``, the oracle's "no cap known" sentinel. Each
    site must also have at least one branch that sources its deadline from the
    lease derivation.
    """
    violators: list[str] = []
    for rel in _ORACLE_ASSEMBLY_SITES:
        path = MAESTRO_ROOT / rel
        source = path.read_text(encoding="utf-8")
        violators.extend(_oracle_budget_violations(source, rel))
        assert _derives_lease_deadline(source), (
            f"{rel} assembles the runnability oracle's evidence but never calls "
            "`gate_capacity.derive_deadline` — the deadline it judges the suite "
            "against must come from the lease (proposal 134)."
        )

    assert not violators, (
        "runnability-oracle budget evidence must come from the lease "
        "(proposal 134):\n  " + "\n  ".join(violators)
        + "\n\nPass `deadline_seconds=` and `cap_seconds=` as a config field or "
        "a `gate_capacity.derive_deadline` result, never a bare number: an "
        "`UNFIT` verdict read against an unenforced ceiling holds every "
        "approved plan on an environment that is not broken."
    )


@pytest.mark.timeout(15)
def test_oracle_budget_helpers_flag_synthetic_sources() -> None:
    """Drift in either provenance helper would silently pass real violators."""
    module = (
        "refresh_verdict(p, deadline_seconds=deadline, cap_seconds=cap)\n"
        "probe_runnability(deadline_seconds=300, cap_seconds=config.max)\n"
        "refresh_verdict(p, deadline_seconds=deadline)\n"
        "other_call(deadline_seconds=300)\n"
    )
    rows = _oracle_budget_sites(module, "synthetic.py")
    assert [(row, arg, value is None) for row, arg, value in rows] == [
        ("synthetic.py:1", "deadline_seconds", False),
        ("synthetic.py:1", "cap_seconds", False),
        ("synthetic.py:2", "deadline_seconds", False),
        ("synthetic.py:2", "cap_seconds", False),
        ("synthetic.py:3", "deadline_seconds", False),
        ("synthetic.py:3", "cap_seconds", True),
    ]
    assert isinstance(rows[2][2], ast.Constant)

    assert _derives_lease_deadline("d = gate_capacity.derive_deadline(rows)\n")
    assert not _derives_lease_deadline("d = config.selfcheck_timeout\n")


@pytest.mark.timeout(15)
def test_oracle_budget_resolution_flags_synthetic_sources() -> None:
    """Drift in the name-resolution helper would re-open the literal hole."""
    lease_sourced = (
        "def assemble(selfcheck):\n"
        "    deadline = gate_capacity.derive_deadline(rows)\n"
        "    cap = 0\n"
        "    other = selfcheck\n"
        "    refresh_verdict(p, deadline_seconds=deadline, cap_seconds=cap)\n"
        "    probe_runnability(deadline_seconds=other, cap_seconds=config.max)\n"
    )
    assert _oracle_budget_violations(lease_sourced, "ok.py") == []

    seeded = (
        "def assemble():\n"
        "    deadline = 300\n"
        "    cap = 900\n"
        "    refresh_verdict(p, deadline_seconds=deadline, cap_seconds=cap)\n"
    )
    assert _oracle_budget_violations(seeded, "bad.py") == [
        "bad.py:4: `deadline_seconds=` binds `deadline`, which is a literal",
        "bad.py:4: `cap_seconds=` binds `cap`, which is a literal",
    ]


@pytest.mark.timeout(15)
def test_oracle_budget_resolution_rejects_an_unresolvable_name() -> None:
    """A name the scope never binds is a violation, not a silent pass."""
    module = "def assemble():\n    refresh_verdict(p, deadline_seconds=mystery)\n"
    violations = _oracle_budget_violations(module, "bad.py")

    assert violations == [
        "bad.py:2: `deadline_seconds=` binds `mystery`, which this scope never "
        "assigns",
        "bad.py:2: `cap_seconds=` missing",
    ]


@pytest.mark.timeout(15)
def test_oracle_budget_resolution_is_scope_local() -> None:
    """A sibling function's lease-sourced binding must not launder a literal."""
    module = (
        "def good():\n"
        "    deadline = gate_capacity.derive_deadline(rows)\n"
        "def bad():\n"
        "    deadline = 300\n"
        "    refresh_verdict(p, deadline_seconds=deadline, cap_seconds=0)\n"
    )
    assert _oracle_budget_violations(module, "bad.py") == [
        "bad.py:5: `deadline_seconds=` binds `deadline`, which is a literal",
    ]


@pytest.mark.timeout(15)
def test_oracle_budget_admits_only_zero_as_a_cap_literal() -> None:
    """``cap_seconds=0`` is the no-cap sentinel; any other number is a ceiling."""
    zero = "refresh_verdict(p, deadline_seconds=config.x, cap_seconds=0)\n"
    nonzero = "refresh_verdict(p, deadline_seconds=config.x, cap_seconds=900)\n"
    deadline_zero = "refresh_verdict(p, deadline_seconds=0, cap_seconds=0)\n"

    assert _oracle_budget_violations(zero, "ok.py") == []
    assert _oracle_budget_violations(nonzero, "bad.py") == [
        "bad.py:1: `cap_seconds=` is a literal",
    ]
    assert _oracle_budget_violations(deadline_zero, "bad.py") == [
        "bad.py:1: `deadline_seconds=` is a literal",
    ]


@pytest.mark.timeout(15)
def test_oracle_budget_guard_flags_a_seeded_local_literal() -> None:
    """The seeded-fault control for the resolution path (proposal 134, r1).

    The live sites bind their budget to a local name, so the literal a caller
    would realistically introduce is ``deadline = 300`` one line above the door
    call — invisible to a rule that only types the argument expression.
    """
    rel = "gate_check.py"
    source = (MAESTRO_ROOT / rel).read_text(encoding="utf-8")
    assert "        deadline = selfcheck\n" in source
    seeded = source.replace("        deadline = selfcheck\n", "        deadline = 300\n")

    violations = _oracle_budget_violations(seeded, rel)

    assert len(violations) == 1
    assert violations[0].endswith("`deadline_seconds=` binds `deadline`, which is a literal")
    assert _oracle_budget_violations(source, rel) == []


@pytest.mark.timeout(15)
def test_oracle_budget_guard_flags_a_seeded_literal() -> None:
    """The seeded-fault control: a literal budget at a live site must fail.

    Mutation is in-memory (the ``tests/test_fault_seeding.py`` idiom) — the live
    source is read, its lease-sourced deadline replaced with a bare number, and
    the scan re-run over the copy. Without this, a helper that stopped matching
    the real call shape would pass vacuously forever.
    """
    rel = "gate_check.py"
    source = (MAESTRO_ROOT / rel).read_text(encoding="utf-8")
    assert "deadline_seconds=deadline," in source
    seeded = source.replace("deadline_seconds=deadline,", "deadline_seconds=300,")
    literals = [
        f"{row}:{arg}"
        for row, arg, value in _oracle_budget_sites(seeded, rel)
        if isinstance(value, ast.Constant)
    ]
    assert len(literals) == 1
    assert literals[0].endswith(":deadline_seconds")

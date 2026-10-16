"""Post-implementation quality pipeline — split out of :mod:`maestro.evolution` (proposal 147).

Runs and observes the quality gates, drives the ``converge_to_green`` fix
loop (``auto_fix`` plus scope narrowing and progress attribution), runs the
``self_review`` reviewer, and scans a diff for uncalled symbols. The moved
``EvolutionEngine`` methods live here as ``fn(engine, ...)`` functions; the
engine keeps one-line delegators so callers are unchanged.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from maestro import (
    gate_capacity,
    gate_environment,
    gate_scope,
)
from maestro.audit_checks import (
    log_gate_activation,
)
from maestro.call_policy import single_use_call
from maestro.gate_convergence import (
    ConvergenceDecision,
    ConvergenceResult,
    plan_next_review_round,
    plan_next_round,
)
from maestro.gate_observation import (
    _MAX_OUTPUT_CHARS,
    MAX_FAILURES,
    Attribution,
    GateFailure,
    GateNonAnswer,
    GateObservation,
    GateObservationKind,
    GateRunObservation,
    GateTool,
    attribute,
    combined_output,
    parse_gate_output,
    parse_non_answer,
    render_failure_report,
    truncated_by_first_failure,
)
from maestro.memory import (
    EvolutionPlan,
)
from maestro.prompts.acceptance import format_acceptance_criteria_block
from maestro.sdk import (
    LONG_CONTEXT_BETA,
    QueryOptions,
    _note_session_effort_override,
    query,
    query_on_session,
)
from maestro.uncalled_scope import UNCALLED_GREP_EXCLUDES, counts_for_uncalled_scan
from maestro.vcs import (
    VCSError,
)

if TYPE_CHECKING:
    from maestro.evolution import EvolutionEngine
    from maestro.providers.base import Session

logger = logging.getLogger("maestro")


# Bound on the warning bullets a full-diff parse may feed into a prompt: the
# parsers read the uncapped diff (#4217), so the list no longer inherits the
# diff cap's implicit bound.
_MAX_WARNING_BULLETS = 10


def _format_warning_bullets(warnings: list[str]) -> str:
    """Render *warnings* as ``- `` bullets, capped at ``_MAX_WARNING_BULLETS``."""
    lines = [f"- {w}" for w in warnings[:_MAX_WARNING_BULLETS]]
    overflow = len(warnings) - _MAX_WARNING_BULLETS
    if overflow > 0:
        lines.append(f"- ... and {overflow} more")
    return "\n".join(lines)


_LGTM_PATTERN = re.compile(r"^\s*LGTM[\s.!]*$", re.IGNORECASE)


#: Maximum diff size (in characters) injected into the self-review prompt.
#: Diffs larger than this are head-and-tail truncated with an explicit
#: ``[... N chars truncated ...]`` marker so the reviewer sees both the
#: beginning and end of the change instead of running out of context.
_SELF_REVIEW_DIFF_MAX_CHARS = 50_000


#: Diff cap when the long-context model variant is active (~125K input
#: tokens, well under the 1M-token window).  Ten times the standard cap
#: so the tail of a large refactor — the riskiest part to review — is
#: no longer dropped.  Used only at the three opt-in call sites; the
#: standard cap remains the load-bearing default everywhere else.
_SELF_REVIEW_DIFF_MAX_CHARS_LONG_CONTEXT = 500_000


#: How many verify-only review rounds one ``self_review`` call may buy against a
#: provably-green branch before a no-``LGTM``, no-commit round is finally a stall
#: (proposal 124). Seeded at loop entry and decremented by whatever
#: :meth:`EvolutionEngine._review_terminal` reports consuming; never replenished
#: within one call, so a reviewer that twice produces non-``LGTM`` prose against
#: an unchanged tree still terminates and one ``self_review`` pays at most one
#: extra gate run.
_VERIFY_ONLY_REVIEW_ROUND_BUDGET = 1


#: Standard tail-cap for the committed / cumulative diff slices injected
#: into the ``converge_to_green`` auto-fix prompt (``_run_auto_fix_query``).
#: The load-bearing default: the flag-off legacy capped loop and every
#: non-long-context call keep this byte-for-byte.
_AUTO_FIX_DIFF_MAX_CHARS = 3000


#: Standard tail-cap for the raw quality-gate output slice injected into
#: the auto-fix prompt.  Same load-bearing default as
#: ``_AUTO_FIX_DIFF_MAX_CHARS`` — the errors the fix round must resolve.
_AUTO_FIX_GATE_OUTPUT_MAX_CHARS = 2000


#: Auto-fix diff cap when the long-context seam is active — ten times the
#: standard cap, mirroring the ``_SELF_REVIEW_DIFF_MAX_CHARS`` to
#: ``_SELF_REVIEW_DIFF_MAX_CHARS_LONG_CONTEXT`` ratio so the fix step
#: sees the full failing diff instead of a truncated tail.  Used only
#: when the long-context seam is active (``_long_context_active``); the
#: standard cap remains the default everywhere else.
_AUTO_FIX_DIFF_MAX_CHARS_LONG_CONTEXT = 30_000


#: Auto-fix gate-output cap when the long-context seam is active — ten times
#: the standard cap so the model ingests the complete ruff / mypy /
#: pytest output it must resolve.  Used only when the long-context seam
#: is active (``_long_context_active``); the standard cap remains the
#: default everywhere else.
_AUTO_FIX_GATE_OUTPUT_MAX_CHARS_LONG_CONTEXT = 20_000


#: Max enumerated failure rows the flag-on auto-fix diagnosis renders
#: (proposal 124), passed as ``render_failure_report``'s ``limit``. The joined
#: block is still bounded by the ``gate_output_cap`` above (2000 standard /
#: 20 000 long-context), so this only caps the *row count* before that char
#: cap — generous enough that a realistic multi-gate red enumerates in full.
_AUTO_FIX_FAILURE_ROW_LIMIT = 50


#: Max base-commit gate baselines the engine keeps in ``_gate_baselines``
#: (proposal 124). Small on purpose: the only bases a live process attributes
#: against are ``origin/main`` and the handful of ``source_task_id`` branches an
#: iteration builds on, so four spans them with room to spare while a stale
#: entry costs nothing but memory. Bounded by dropping the oldest *inserted*
#: key — the dict analogue of ``directive_ledger._MAX_ENTRIES``' drop-oldest
#: list trim, spelled plainly because the package uses neither ``OrderedDict``
#: nor ``lru_cache`` anywhere.
_MAX_GATE_BASELINES = 4


def _truncate_diff(diff: str, max_chars: int = _SELF_REVIEW_DIFF_MAX_CHARS) -> str:
    """Head-and-tail truncate a diff so it fits within ``max_chars``.

    Returns the diff unchanged when it is already within budget.  When the
    diff is over budget, returns roughly the first half + a marker line +
    the last half so the reviewer sees both ends of the change.
    """
    if len(diff) <= max_chars:
        return diff
    half = max_chars // 2
    omitted = len(diff) - (2 * half)
    head = diff[:half]
    tail = diff[-half:]
    marker = f"\n\n[... {omitted} chars truncated by self-review diff cap ...]\n\n"
    return head + marker + tail


def _is_lgtm(response_text: str) -> bool:
    """Return True if the reviewer's response is an unambiguous LGTM verdict.

    The reviewer is instructed to respond with exactly ``LGTM`` when the
    implementation is approved.  Earlier code used a substring match
    (``"LGTM" in response_text``) which falsely accepted responses like
    ``"This is not LGTM yet because ..."`` — i.e. an explicit rejection.
    To avoid that, we look at the **last non-empty line** of the response and
    accept it only if that line is exactly ``LGTM`` (case-insensitive, with
    optional trailing punctuation/whitespace).
    """
    if not response_text:
        return False
    for line in reversed(response_text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        return bool(_LGTM_PATTERN.match(stripped))
    return False


def _decoded_partial_stream(raw: str | bytes | None) -> str:
    """Normalise the partial stream a killed gate runner left on its exception.

    ``subprocess.TimeoutExpired`` carries what the runner had already written when
    ``subprocess.run`` killed it — but not through ``text=True``'s decoder: on
    POSIX the exception is raised from inside ``Popen._communicate`` *before* the
    newline translation runs, so the attribute is ``bytes``; Windows re-reads the
    pipes after the kill and hands back ``str``; and either platform leaves it
    ``None`` when nothing had been written yet. All three shapes normalise to
    ``str`` here so :func:`~maestro.gate_observation.parse_non_answer` can be given
    text without knowing any of that.

    Decoded as UTF-8 with ``errors="replace"`` (CLAUDE.md → all text I/O is UTF-8):
    a stream cut off mid-character must not raise, because salvaging what the
    runner did manage to say is the entire point of reading it.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _join_long_report(parts: list[tuple[str, str | None]]) -> str:
    """Join the long-context gate report so it fits the long-context cap whole.

    Every header and separator is kept; the remaining budget of
    :data:`_AUTO_FIX_GATE_OUTPUT_MAX_CHARS_LONG_CONTEXT` is shared evenly among
    the red gates' output tails (each also bounded at
    :data:`~maestro.gate_observation._MAX_OUTPUT_CHARS`), so the consumer's
    ``[-cap:]`` slice never cuts a header or drops an earlier red gate.
    """
    fixed = sum(len(header) for header, _ in parts) + 2 * max(len(parts) - 1, 0)
    red = sum(1 for _, output in parts if output is not None)
    share = min(
        _MAX_OUTPUT_CHARS,
        max(_AUTO_FIX_GATE_OUTPUT_MAX_CHARS_LONG_CONTEXT - fixed, 0) // max(red, 1),
    )
    return "\n\n".join(
        header if output is None else header + (output[-share:] if share else "")
        for header, output in parts
    )


@dataclass
class QualityGateResult:
    """Structured result of running quality gates (ruff, mypy, pytest)."""

    passed: bool
    ruff_ok: bool
    mypy_ok: bool
    tests_ok: bool
    raw_output: str
    #: The structured, enumerated gate fact (proposal 124) — one
    #: :class:`~maestro.gate_observation.GateObservation` per gate — or ``None``
    #: when ``[loop] gate_observation_enabled`` is off. Trailing and defaulted so
    #: no existing construction site changes shape; ``converge_to_green``'s
    #: terminal and progress predicate read it (proposal 124 steps 6-7), and the
    #: five fields above keep their exact prior meaning when it is ``None``.
    observation: GateRunObservation | None = None
    #: True only on the synthesized result of a gate run the execution lease
    #: *declined* (proposal 133) — the cached runnability verdict held that the
    #: environment cannot answer, so no subprocess ran and every gate reads
    #: ``False`` without the tree having been measured at all. Callers that bank
    #: a result as a *fact about a commit* (``_observe_baseline``) must refuse a
    #: declined one: it is a reading about the environment, not the tree, and
    #: banking it would let a later genuine red be excused as ``INHERITED``.
    #: Trailing and defaulted, so every real run keeps its exact shape.
    declined: bool = False
    #: The same report as ``raw_output`` but with each red gate's tail bounded
    #: at :data:`~maestro.gate_observation._MAX_OUTPUT_CHARS` rather than 1000,
    #: so the flag-off long-context auto-fix seam has output to widen into.
    #: Empty on a synthesized result; readers fall back to ``raw_output``.
    long_raw_output: str = ""

    def failure_signature(self) -> frozenset[str]:
        """Return the identifiers of the *failing* gates only.

        A passing result yields the empty frozenset; resolving one gate
        yields a strict subset of the prior signature. Pure — no I/O. This is
        the *flag-off* progress signal :meth:`EvolutionEngine._progress_signals`
        hands :func:`maestro.gate_convergence.plan_next_round` across fix rounds;
        flag-on that seam supplies the finer per-failure
        :meth:`~maestro.gate_observation.GateRunObservation.signature` instead.
        """
        return frozenset(
            name
            for name, ok in (
                ("ruff", self.ruff_ok),
                ("mypy", self.mypy_ok),
                ("tests", self.tests_ok),
            )
            if not ok
        )


def _is_symbol_definition(content: str, name: str) -> bool:
    """Return ``True`` when *content* is a ``def``/``class`` statement for *name*.

    *content* is a raw source line (e.g. the ``content`` field of a
    ``git grep -n`` ``path:lineno:content`` row).  Used to recognise — and
    discount — a symbol's own definition line so that the only thing left to
    count is a genuine call site.
    """
    return (
        re.match(
            rf"^\s*(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b",
            content,
        )
        is not None
    )


def _is_method_definition(content: str, name: str) -> bool:
    """Return ``True`` when *content* is an *indented* ``def`` for *name*.

    Indentation is what distinguishes a method declaration on a class from a
    module-level function declaration; :func:`_symbol_has_call_site` uses it to
    tell an interface/override declaration apart from a duplicate dead
    module-level definition.
    """
    return (
        re.match(rf"^\s+(?:async\s+)?def\s+{re.escape(name)}\b", content)
        is not None
    )


def _symbol_has_call_site(grep_output: str, name: str, file_path: str) -> bool:
    """Return ``True`` when *grep_output* references *name* off a declaration.

    *grep_output* is the stdout of ``git grep -n --fixed-strings -e <name>``,
    whose rows have the form ``path:lineno:content``.  A row counts as a real
    call site unless it is a ``def``/``class`` declaration for *name*, **no
    matter which file it lives in** — a same-named definition elsewhere in the
    tree is another declaration, not a use, so duplicate dead module-level
    definitions cannot vouch for each other (#3797).  Intra-module call sites
    (a private helper used only within its defining module) still count, while
    a symbol whose only rows are declarations does not.

    One declaration shape is exempt from that discount: an *indented* ``def``
    for *name* in a file other than *file_path* is a method declaration on
    another type, and a new method added in *file_path* that matches it is an
    override or protocol implementation — conformance to an existing interface,
    which is a genuine use even with no explicit call site.  The exemption is
    file-qualified because the symbol's own definition line lives in
    *file_path*; without that qualifier every new method would vouch for
    itself.
    """
    for line in grep_output.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            # Not a ``path:lineno:content`` row (e.g. a binary-match notice);
            # cannot be a call site we can reason about, so skip it.
            continue
        path, content = parts[0], parts[2]
        if _is_symbol_definition(content, name):
            if path != file_path and _is_method_definition(content, name):
                # Interface conformance in another file — counts as a use.
                return True
            continue
        return True
    return False




def _extract_added_source(diff: str) -> str:
    """Reconstruct added Python source from a unified *diff* (best-effort).

    Walks the diff, tracking the current ``+++ b/<path>`` target, and collects
    the added lines (``+`` prefix, excluding the ``+++`` header) from non-test
    ``.py`` files only (``counts_for_uncalled_scan``) — test helpers and
    fixtures legitimately return literals.

    The reconstructed text is an approximation: a hunk that edits the middle of
    an existing function yields indented fragments that may not parse as a
    complete module.  The sole caller, :func:`_find_literal_only_functions`,
    tolerates that by returning no warnings when the text does not parse.
    """
    include = False
    added: list[str] = []
    for line in diff.splitlines():
        match = _DIFF_FILE_RE.match(line)
        if match:
            path = match.group(1)
            include = counts_for_uncalled_scan(path)
            continue
        if include and line.startswith("+"):
            added.append(line[1:])
    return "\n".join(added)


def _is_literal_constant(value: ast.expr | None) -> bool:
    """Return ``True`` when *value* is a frozen literal/constant expression.

    A constant scalar (``ast.Constant`` — number, string, bytes, ``True``,
    ``False``, ``None``) is literal, as is a list/tuple/set/dict whose every
    element (and dict key) is itself literal — e.g. ``[1, 2]`` or
    ``{"a": 1}``.  A unary-signed literal (``-1``, ``+3.5``) is also literal:
    in the AST these parse as ``UnaryOp(USub/UAdd, …)`` over the operand, not
    as a signed ``Constant``, so the sign is unwrapped here.  Anything that
    references a name, calls a function, formats an f-string, comprehends, or
    applies any other operator is *not* literal because it computes its
    result.  A bare ``return`` (``value is None``) is not a literal value.
    """
    if value is None:
        return False
    if isinstance(value, ast.Constant):
        return True
    if isinstance(value, ast.UnaryOp) and isinstance(value.op, (ast.USub, ast.UAdd)):
        return _is_literal_constant(value.operand)
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_literal_constant(elt) for elt in value.elts)
    if isinstance(value, ast.Dict):
        return all(
            key is not None
            and _is_literal_constant(key)
            and _is_literal_constant(val)
            for key, val in zip(value.keys, value.values, strict=True)
        )
    return False


def _find_literal_only_functions(changed_source: str) -> list[str]:
    """Flag newly-added functions whose body is just ``return <literal>``.

    Parses *changed_source* with :mod:`ast` and returns a human-readable
    warning for every ``def`` / ``async def`` whose body reduces to a single
    ``return`` of a literal/constant value — a leading docstring is permitted,
    but nothing else.  Such a function names work it does not do (the
    "noun-not-verb" / hollow-artifact failure mode): it echoes a frozen value
    instead of computing one.

    A function is *not* flagged when it returns a non-constant expression
    (a call, name, attribute, f-string, comprehension, operator, or a
    container holding any of those) — including the common case of returning
    a value derived from its parameters — or when it contains any statement
    beyond the optional docstring and the single ``return``.

    The check is best-effort and advisory: if *changed_source* does not parse
    as a complete Python module the function returns an empty list rather than
    raising, mirroring the advisory contract of
    :meth:`EvolutionEngine._find_uncalled_symbols`.
    """
    try:
        tree = ast.parse(changed_source)
    except SyntaxError:
        return []

    warnings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        # Permit (and skip) a single leading docstring.
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        # The remaining body must be exactly one ``return`` of a literal.
        if len(body) != 1 or not isinstance(body[0], ast.Return):
            continue
        if not _is_literal_constant(body[0].value):
            continue
        warnings.append(
            f"{node.name}: body only returns a constant literal — it names "
            "work it does not perform (hollow artifact). Make it compute its "
            "result or remove it."
        )
    return warnings


#: The largest share of the suite a *widened* impacted selection may name before
#: narrowing stops paying for itself (proposal 134). Listing most of the suite as
#: an explicit pytest argv is slower than running the suite, so
#: :meth:`EvolutionEngine._widen_scope` degrades to full scope past this fraction
#: rather than growing without bound as a broad red names module after module.
_MAX_IMPACTED_FRACTION = 0.5


@dataclass
class _ScopeTrace:
    """The mutable record one ``gate_scope`` activation row is rendered from.

    ``action`` is ``"narrowed"`` only once a narrowed baseline actually survived
    the restriction; every other path — flag off, nothing selected, a red outside
    the selection — leaves it ``"full"``, so the row states what the convergence
    really ran rather than what was attempted. ``detail`` accumulates the
    selection's size against the suite, and any widening or certification that
    followed.
    """

    action: str = "full"
    detail: str = ""


def _declined_tests_identities(
    verdict: gate_environment.GateRunnabilityVerdict,
) -> frozenset[str]:
    """The ``tests:``-scoped identities a run declined on *verdict* reports under.

    :func:`~maestro.gate_environment.declined_gate_identities` answers *which
    identities the environment cannot answer for*; this narrows that to the ones
    the synthesized observation can honestly claim. The observation is built with
    ``tool="tests"`` — one gate, one tool — so an identity carried under any other
    tool prefix would be silently re-labelled into the ``tests:`` namespace and
    compare equal to a failure that never existed. Those are dropped, and if the
    verdict names none the run *can* be reported under, the whole-run collection
    abort stands in, exactly as it does for a verdict that named nothing at all.

    Pure; returns a non-empty set by construction.
    """
    identities = gate_environment.declined_gate_identities(verdict)
    scoped = frozenset(i for i in identities if i.startswith("tests:"))
    return scoped or frozenset({gate_environment.DECLINED_IDENTITY})


#: Matches unified-diff ``+++ b/<path>`` headers to extract the file.
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")


#: Matches added ``def`` or ``class`` lines (``+`` prefix, not ``++``).
_ADDED_SYMBOL_RE = re.compile(
    r"^\+(?!\+)\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)"
)


def _run_gate_command(
    engine: EvolutionEngine,
    tool: GateTool,
    cmd: list[str],
    workspace: str,
    *,
    timeout_seconds: int | None = None,
) -> subprocess.CompletedProcess[str] | GateObservation:
    """Run one quality-gate command, splitting a finished run from a non-answer.

    Returns the ``CompletedProcess`` for a run that *finished* — whatever its
    exit code — so the caller keeps the whole ``(returncode, stdout, stderr)``
    triple rather than only a truncated tail of it. Returns the **named
    non-answer** as an already-built
    :class:`~maestro.gate_observation.GateObservation` otherwise: ``TIMEOUT``
    when the runner exceeded its budget, ``UNAVAILABLE`` when it
    could not be executed at all (missing binary, no exec permission). The
    pre-124 runner collapsed both into the single ``"could not run"`` string,
    which then became the fix step's entire diagnosis; splitting them here is
    what lets the gate observation name the non-answer instead of stringifying
    it. The two arms are returned as one ``isinstance``-discriminated union
    rather than an ``Optional`` pair so the caller narrows without a second
    ``None`` check, and the non-answer arm is built by
    :func:`~maestro.gate_observation.parse_non_answer`, whose ``kind``
    parameter is typed to the two-member
    :data:`~maestro.gate_observation.GateNonAnswer` rather than the whole
    vocabulary, so a gate that never ran cannot be built as ``CLEAN``.

    A timed-out runner is *not* mute: ``subprocess.TimeoutExpired`` carries
    whatever ``subprocess.run`` had read when it killed the process, so that
    partial text is salvaged and enumerated rather than thrown away —
    see :func:`_decoded_partial_stream`. An ``OSError`` runner never started,
    so it has none.

    *timeout_seconds* is the budget this command runs under; ``None`` — every
    caller but the ``tests`` gate under ``[loop]
    gate_budget_lease_owned_enabled`` — means the flat ``selfcheck_timeout``
    ceiling, so the enforced value is unchanged from today. A budgeted run that
    is killed still yields the same ``TIMEOUT`` non-answer with its salvaged
    partial output, so only *when* the kill happens moves, never the recorded
    vocabulary.

    Neither handler silences anything (CLAUDE.md → "Fail loud"): both exception
    types are specific, and each becomes a named outcome the caller reports
    rather than a swallowed warning.
    """
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=(
                engine._config.selfcheck_timeout
                if timeout_seconds is None
                else timeout_seconds
            ),
            cwd=workspace,
        )
    except subprocess.TimeoutExpired as exc:
        return parse_non_answer(
            tool, "TIMEOUT",
            _decoded_partial_stream(exc.stdout),
            _decoded_partial_stream(exc.stderr),
        )
    except OSError:
        return parse_non_answer(tool, "UNAVAILABLE", "", "")


def _observe_gate(
    engine: EvolutionEngine,
    tool: GateTool,
    cmd: list[str],
    proc: subprocess.CompletedProcess[str],
    workspace: str,
    *,
    timeout_seconds: int | None = None,
) -> GateObservation:
    """Parse one *finished* gate run into a :class:`GateObservation`.

    A ``pytest -x`` probe whose own output shows it was **cut short** — it
    carries a failing test or a collection abort, the two kinds ``-x`` stops at
    (``gate_observation.truncated_by_first_failure``) — is re-run once without
    ``-x`` and the observation is parsed from *that* output, because the probe
    cannot enumerate a failing set it refused to finish computing. Every other
    gate is parsed from the probe alone, so the re-run costs a second suite
    only where it buys enumeration: a green probe, a ruff/mypy gate, and — the
    common red on a ``--cov-fail-under`` suite — a coverage-only shortfall,
    whose run *completed* with every test passing and whose own output already
    carries the threshold line, are all read once.

    The re-run's argv is derived from the probe's *own* argv with ``-x``
    filtered out, so the ``WorkTarget`` gate-path atoms stay the single source
    of the paths (``tests/test_gate_command_owner.py``) instead of being
    re-hardcoded in a second command literal. That the probe carries ``-x`` at
    all is pinned by ``test_run_quality_gates_flag_off_is_byte_identical``, so
    the filter is never a no-op that would re-run an identical suite twice.

    The **probe** stays the authority on whether the gate is red — its
    ``returncode`` is what ``tests_ok`` already reflects — while the re-run
    supplies only the material to enumerate. So a re-run that itself does not
    finish, or that comes back green (flaky ordering), still yields a red
    observation: at worst an empty ``failures`` tuple, which
    ``signature()`` reports as the synthetic ``tests:FAILED`` rather than as a
    clean run.

    A re-run that did **not** answer is the one case where the returned
    observation would otherwise lie about its own completeness: the probe's
    rows are whatever ``-x`` stopped at, and the reading that would have
    enumerated the rest never came back. That probe is therefore returned
    stamped ``truncated``, which is what stops
    ``gate_observation.attribute`` from calling a first-failure-only set
    wholly ``INHERITED`` while the failures past the cut were never compared
    to the base. The re-run is the likeliest place for this to happen — the
    whole suite runs under the same *timeout_seconds* the fast probe finished
    well inside of. That budget is passed through rather than re-derived, so
    the enumerate re-run is never budgeted more tightly than the probe it
    replaces; ``None`` keeps the flat ``selfcheck_timeout``.
    """
    probe = parse_gate_output(tool, proc.stdout, proc.stderr, proc.returncode)
    if tool != "tests" or not truncated_by_first_failure(probe):
        return probe
    enumerated = engine._run_gate_command(
        tool, [arg for arg in cmd if arg != "-x"], workspace,
        timeout_seconds=timeout_seconds,
    )
    if isinstance(enumerated, GateObservation):
        return replace(probe, truncated=True)
    return parse_gate_output(
        tool, enumerated.stdout, enumerated.stderr, proc.returncode,
    )


def collect_suite(engine: EvolutionEngine) -> tuple[int, str, str] | GateNonAnswer:
    """Run ``pytest --collect-only`` and report it in the oracle's vocabulary.

    The collector :func:`maestro.gate_environment.probe_collection` asks for:
    either a *finished* run as its ``(returncode, stdout, stderr)`` triple, or
    the bare ``"TIMEOUT"`` / ``"UNAVAILABLE"`` literal naming a non-answer. The
    split is already owned by :meth:`_run_gate_command`, so this method is only
    the adapter — it builds the argv, hands it to that one runner, and narrows
    the union: a ``CompletedProcess`` becomes the triple, a ``GateObservation``
    becomes its bare kind (which, from that runner, is one of exactly those two).

    The argv comes from the :meth:`~maestro.config.Config.work_target` path
    atoms rather than a literal, like every other gate reader.
    ``-p no:cacheprovider`` keeps a collection probe from writing a
    ``.pytest_cache`` into the workspace, and ``-q`` keeps the output bounded.
    No deadline is derived here — the flat ``selfcheck_timeout`` applies, since
    collection is orders of magnitude cheaper than a run and the fit question
    is the *next* probe's, not this one's.
    """
    wt = engine._config.work_target()
    tests = wt.gate_tests_path
    cmd = ["pytest", tests, "--collect-only", "-q", "-p", "no:cacheprovider"]
    result = engine._run_gate_command(
        "tests", cmd, str(wt.repo_workspace),
    )
    if isinstance(result, GateObservation):
        # ``_run_gate_command``'s non-answer arm builds only these two kinds,
        # both members of ``GateNonAnswer``; the cast records that narrowing
        # rather than re-deriving it from the observation.
        return cast(GateNonAnswer, result.kind)
    return result.returncode, result.stdout, result.stderr


def run_quality_gates(engine: EvolutionEngine, *, scope: Sequence[str] = ()) -> QualityGateResult:
    """Run quality gates on own code and return a structured result.

    A non-empty *scope* (proposal 134) narrows **only** the pytest argv to the
    named test modules, drops the two ``--cov`` atoms in favour of
    ``--no-cov``, and stamps the telemetry row ``scope="impacted"`` so the
    narrowed reading never enters ``derive_deadline``'s budget derivation or
    ``measure-gate-contention``. Ruff and mypy still run whole either way. The
    empty default is the "do not narrow" sentinel: today's argv, today's
    ``"full"`` row, and every existing no-argument call site unchanged.

    Under ``[loop] gate_observation_enabled`` (proposal 124) each gate's
    *retained* output is additionally parsed into a
    :class:`~maestro.gate_observation.GateObservation`, the two non-answers are
    named rather than stringified, and a pytest probe that ``-x`` cut short is
    enumerated by one ``-x``-less re-run (see :meth:`_observe_gate`). Flag-off
    the argv, the
    ``raw_output`` report, and the three booleans are byte-identical to the
    pre-124 runner and ``observation`` is ``None`` — nothing reads the
    observation yet, by design.
    """
    report_parts: list[str] = []
    # ``(header, output)`` pairs; red gates carry their combined output,
    # sliced after the loop so the joined long report fits the auto-fix cap.
    long_report_parts: list[tuple[str, str | None]] = []
    workspace = str(engine._config.workspace)
    gate_results: dict[str, bool] = {}
    observe = engine._config.gate_observation_enabled
    observations: list[GateObservation] = []
    # The non-answer kinds observed this run, collected independently of
    # *observe* so the coarse telemetry outcome can still name a killed runner.
    non_answer_kinds: list[GateObservationKind] = []

    # The gate source/tests paths come from the WorkTarget owner (proposal
    # 107) rather than hardcoded literals; both default to ``maestro/`` /
    # ``tests/`` (plus the lint-only ``scripts/`` extra) so a default build
    # matches CI's ``quality`` job. ``--cov`` is derived
    # from the source atom (``--cov=maestro`` by default). The middle element
    # is the observation's tool vocabulary (``tests``, not ``pytest``), kept
    # beside each command so the two can never drift apart.
    wt = engine._config.work_target()
    source, tests = wt.gate_source_path, wt.gate_tests_path
    extra = wt.gate_lint_extra_paths
    # One slot covers the whole ruff -> mypy -> pytest sequence; the granted
    # width is spliced into the ``Tests`` argv beside the ``--cov`` atoms and
    # flows unchanged into ``_observe_gate``'s ``-x``-less enumerate re-run
    # (which derives its argv from the probe's own), so the re-run stays inside
    # the same held slot. Flag off, ``worker_args`` returns ``()`` and the argv
    # is byte-identical.
    with gate_capacity.lease_for_gate_run(
        engine._config.gate_lease_dir,
        runs_path=engine._config.gate_runs_path,
        cgroup_reserve=engine._config.gate_lease_cgroup_reserve_enabled,
        admission_cap=engine._config.gate_lease_admission_cap_enabled,
        budget_lease_owned=engine._config.gate_budget_lease_owned_enabled,
        scope="impacted" if scope else "full",
        selfcheck=engine._config.selfcheck_timeout,
        selfcheck_max=engine._config.selfcheck_max_timeout,
        verdict_path=(
            engine._config.gate_environment_path
            if engine._config.gate_environment_oracle_enabled
            else None
        ),
    ) as run:
        if run.runnability is not None:
            # The seam declined this run: the cached runnability verdict
            # already holds that the environment cannot answer, so no
            # subprocess is spawned and the failing result is synthesized
            # from the verdict itself. It is reported in the ordinary
            # observation vocabulary — one COLLECTION_ERROR gate carrying one
            # failure per identity the verdict names — so its ``signature()``
            # equals the verdict the
            # convergence terminal attributes against, and every downstream
            # reader behaves exactly as it does for a real environment red,
            # only seconds sooner and for free.
            run.outcome = "COLLECTION_ERROR"
            return engine._declined_gate_result(run.runnability)
        # The lease owns the budget as well as the width (proposal 134), and
        # only the suite spends it: ruff and mypy pass ``None`` for the flat
        # ``selfcheck_timeout`` ceiling, while the tests gate runs under the
        # granted deadline. ``deadline_or_default`` is the single reader for
        # that fallback, so flag off (or lease off) every gate is enforced at
        # ``selfcheck_timeout`` exactly as today.
        tests_deadline = gate_capacity.deadline_or_default(
            run.lease, engine._config.selfcheck_timeout,
        )
        # A narrowed round runs the selected modules with ``--no-cov``: the
        # coverage floor is not observable over a subset of the suite, so
        # asking for it would report a shortfall the diff did not cause. CI at
        # ``--cov-fail-under=100`` stays the coverage authority either way, and
        # the loop's own certification read (``converge_to_green``) is
        # full-scope and with coverage.
        tests_argv = (
            ["pytest", *scope, "-x", "--tb=short", "-q", "--no-cov"]
            if scope
            else ["pytest", tests, "-x", "--tb=short", "-q",
                  f"--cov={source.rstrip('/')}",
                  f"--cov-fail-under={engine._config.cov_fail_under}"]
        )
        gates: list[tuple[str, GateTool, list[str], int | None]] = [
            ("Ruff", "ruff", ["ruff", "check", source, tests, *extra], None),
            ("Mypy", "mypy", ["mypy", source, *extra], None),
            ("Tests", "tests", [*tests_argv,
                                *gate_capacity.worker_args(run.lease)],
             tests_deadline),
        ]
        for label, tool, cmd, budget in gates:
            # Only the suite is timed: ``tests_seconds`` is the quantity the
            # granted deadline is enforced against, and the completed half of
            # ``derive_deadline`` reads it as the suite's measured cost. Ruff
            # and mypy take ``nullcontext`` so their argv and control flow are
            # untouched and contribute nothing to the figure. A timed-out suite
            # is stamped too — the timer folds in a ``finally`` — because a
            # censored reading is evidence; the derivation is what decides to
            # exclude it from the completed p50.
            timer = (
                run.time_tests_command()
                if tool == "tests"
                else contextlib.nullcontext()
            )
            with timer:
                outcome = engine._run_gate_command(
                    tool, cmd, workspace, timeout_seconds=budget,
                )
            if isinstance(outcome, GateObservation):
                report_parts.append(f"{label}: could not run")
                long_report_parts.append((f"{label}: could not run", None))
                gate_results[label] = False
                # Recorded regardless of *observe*: the telemetry outcome is
                # what ``derive_deadline`` reads, and a killed suite must land
                # as ``TIMEOUT`` there even with observation off — the coarse
                # CLEAN/FAILED branch below would otherwise call it ``FAILED``
                # and the budget escalation could never fire.
                non_answer_kinds.append(outcome.kind)
                if observe:
                    # Already built by ``_run_gate_command``: the *kind* is the
                    # headline fact, plus whatever failures a killed runner's
                    # partial output still named. ``signature()`` keeps the
                    # non-empty ``"<tool>:<kind>"`` identity either way, so a
                    # non-answer can never read as green — nor as progress.
                    observations.append(outcome)
                continue
            proc = outcome
            if proc.returncode != 0:
                # The stdout/stderr join has one owner: this report tail, the
                # ``long_raw_output`` tail and ``GateObservation.output`` are
                # slices of the same combined text, so the rule cannot be changed in one spelling and left
                # behind in the other. The ``[-1000:]`` tail is the pre-124
                # report's own bound, unchanged.
                output = combined_output(proc.stdout, proc.stderr)
                report_parts.append(f"{label} issues:\n{output[-1000:]}")
                long_report_parts.append((f"{label} issues:\n", output))
                gate_results[label] = False
            else:
                report_parts.append(f"{label}: clean")
                long_report_parts.append((f"{label}: clean", None))
                gate_results[label] = True
            if observe:
                # Re-entered for the tests gate so ``_observe_gate``'s
                # ``-x``-less enumerate re-run — a second full pytest command
                # under the same per-command budget — feeds the same field; the
                # ``max`` fold keeps the longer of the two.
                with (
                    run.time_tests_command()
                    if tool == "tests"
                    else contextlib.nullcontext()
                ):
                    observations.append(engine._observe_gate(
                        tool, cmd, proc, workspace, timeout_seconds=budget,
                    ))
        # Stamp how the run ended for the telemetry row before the lease
        # context exits (the seam records on exit): the worst observed gate
        # kind when observation is on, else a coarse outcome from the pass/fail
        # booleans already computed into ``gate_results``. A non-answer wins
        # over that coarse reading either way — ``derive_deadline`` keys on a
        # recorded ``TIMEOUT``, so a killed suite must not read as a mere
        # ``FAILED`` just because ``[loop] gate_observation_enabled`` is off;
        # that dependency would make the budget flag silently inert.
        if observe and observations:
            run.outcome = gate_capacity.observation_outcome(
                obs.kind for obs in observations
            )
        elif non_answer_kinds:
            run.outcome = gate_capacity.observation_outcome(non_answer_kinds)
        else:
            run.outcome = (
                "CLEAN"
                if all(gate_results.get(name, False)
                       for name in ("Ruff", "Mypy", "Tests"))
                else "FAILED"
            )

    ruff_ok = gate_results.get("Ruff", False)
    mypy_ok = gate_results.get("Mypy", False)
    tests_ok = gate_results.get("Tests", False)
    return QualityGateResult(
        passed=ruff_ok and mypy_ok and tests_ok,
        ruff_ok=ruff_ok,
        mypy_ok=mypy_ok,
        tests_ok=tests_ok,
        raw_output="\n\n".join(report_parts),
        observation=(
            GateRunObservation(gates=tuple(observations)) if observe else None
        ),
        long_raw_output=_join_long_report(long_report_parts),
    )


def _declined_gate_result(
    engine: EvolutionEngine, verdict: gate_environment.GateRunnabilityVerdict,
) -> QualityGateResult:
    """Synthesize the failing result of a gate run the seam refused.

    Reached only when the lease seam declined the run, which it does only on a
    cached verdict saying the local gate cannot reach a verdict at all. Every
    gate reads ``False``: the environment answered for none of them, and a red
    no diff can clear must never read as a pass. ``raw_output`` is the
    verdict's own bounded ``detail``, so the report says *why* in the oracle's
    words rather than in a manufactured sentence.

    The observation is built under the same ``gate_observation_enabled``
    condition the real path uses, so flag composition is unchanged and a
    caller reading ``observation`` gets ``None`` exactly where it does today.
    The verdict already carries its identities in the
    :meth:`~maestro.gate_observation.GateObservation.signature` spelling, so
    each ``tests:``-scoped one (see :func:`_declined_tests_identities`, which
    drops any other tool's namespace rather than re-labelling it) becomes one
    ``COLLECTION_ERROR`` failure whose
    :meth:`~maestro.gate_observation.GateFailure.identity` is that very
    string — the tool prefix stripped back off, since ``identity`` re-adds it.
    The observation's
    :meth:`~maestro.gate_observation.GateRunObservation.signature` is
    therefore *equal* to the verdict's identities rather than merely
    compatible with them, which is what lets the convergence terminal
    attribute the red to the environment with no translation layer. The
    tuple is bounded by :data:`~maestro.gate_observation.MAX_FAILURES`, the
    same cap a parsed observation carries, so a verdict naming an enormous
    identity set cannot produce an unbounded observation. The
    coarse ``failure_signature`` is unaffected: it names all three gates, as
    it does for any run where none of them passed.
    """
    identities = _declined_tests_identities(verdict)
    observation = GateObservation(
        tool="tests",
        kind="COLLECTION_ERROR",
        failures=tuple(
            GateFailure(
                tool="tests",
                kind="COLLECTION_ERROR",
                location="",
                identifier=identity.split(":", 1)[1],
                message=verdict.detail,
            )
            for identity in sorted(identities)[:MAX_FAILURES]
        ),
        output=verdict.detail,
    )
    return QualityGateResult(
        passed=False,
        ruff_ok=False,
        mypy_ok=False,
        tests_ok=False,
        raw_output=verdict.detail,
        observation=(
            GateRunObservation(gates=(observation,))
            if engine._config.gate_observation_enabled
            else None
        ),
        declined=True,
    )


def _observe_baseline(engine: EvolutionEngine, sha: str) -> GateRunObservation | None:
    """Observe the local gate on base commit *sha* — never moving the tree.

    The other half of proposal 124's attribution:
    :func:`~maestro.gate_observation.attribute` can only call a red
    ``INHERITED`` when it holds a baseline of the branch's *own base*, and
    this is the only thing that produces one.

    The seam **never checks anything out**. It reads
    :meth:`~maestro.vcs.VCS.get_head_hash` and runs the gate only when HEAD
    already equals *sha*, so no caller can move a mid-flight tree out from
    under an implementation session — the property that keeps the action
    gate's branch scoping and the hold-the-tree contract intact. A HEAD that
    does not match yields ``None`` and records nothing; callers place the
    capture at the two moments where the match already holds
    (``tend_evolution_prs`` after ``main`` is reconciled to ``origin/main``
    and the open-PR worklist is known — and only when that worklist is
    non-empty — and ``implement`` on the fresh, commit-free evolution branch
    right after it reads its own ``base_hash``).

    A **dirty tree is not that commit.** ``run_quality_gates`` observes the
    working tree and the installed environment, not the object named by
    *sha*, so an uncommitted edit sitting on top of the base would be banked
    as the base's own result — and a red it caused would later excuse a
    branch's identical red as ``INHERITED``. ``ensure_main_branch`` only
    discards changes when HEAD is *off* ``main``, so dirty-on-``main`` is
    reachable; a working tree with uncommitted changes therefore yields
    ``None`` and banks nothing. What the cache key still cannot capture is the
    *environment* — an uninstalled dependency reds every commit alike, and a
    baseline banked while the container was broken keeps that red for the
    process's life. That is the benign direction (the red really was
    inherited, from the environment rather than the diff) and a restart
    re-reads it, which is the honest bound on the immutability claim below.

    Returns ``None`` — and issues no subprocess at all — when
    ``[loop] gate_observation_enabled`` is off or *sha* is empty (the
    ``or ""`` coercion at both call sites can hand it one). A cached entry is
    served without re-running; a fresh one is banked and the map trimmed to
    :data:`_MAX_GATE_BASELINES` oldest-first. The result is returned for the
    caller that wants it and safely ignored by the ones that only want the
    cache warmed.

    Blocking: the gate run is a full ruff + mypy + pytest sweep, so both
    callers hand this sync seam to ``asyncio.to_thread`` rather than stalling
    their event loop for the duration.
    """
    if not engine._config.gate_observation_enabled or not sha:
        return None
    cached = engine._gate_baselines.get(sha)
    if cached is not None:
        return cached
    if (engine._vcs.get_head_hash() or "") != sha:
        return None
    try:
        dirty = engine._vcs.has_changes()
    except VCSError:
        # Narrow, and a *named* outcome rather than a swallowed warning: an
        # unreadable working-tree state is indistinguishable from a dirty one
        # for this purpose, and "no baseline" is the same fail-safe
        # ``UNATTRIBUTABLE`` a cache miss already yields. The seam is a
        # convenience; refusing to bank costs a less-informed verdict, while
        # raising would abort a mechanical tend lap over a warm-up.
        logger.warning(
            "Could not read working-tree state for gate baseline %s",
            sha[:8], exc_info=True,
        )
        return None
    if dirty:
        return None
    result = engine.run_quality_gates()
    if result.declined:
        # The execution lease refused the run on a cached runnability
        # verdict, so nothing measured this commit: the synthesized
        # COLLECTION_ERROR is a fact about the environment. Banking it would
        # outlive the repair — the map is keyed by *sha* alone and served
        # for the process's life — and would then excuse a later genuine
        # collection red on this branch as ``INHERITED``. No baseline is the
        # fail-safe: attribution falls back to ``UNATTRIBUTABLE``.
        return None
    observation = result.observation
    if observation is None:
        # Unreachable with the flag on — ``run_quality_gates`` attaches the
        # observation on exactly that condition — but the field is typed
        # optional, and banking a ``None`` would poison the cache for a base
        # a later call could still observe properly.
        return None
    engine._gate_baselines[sha] = observation
    while len(engine._gate_baselines) > _MAX_GATE_BASELINES:
        del engine._gate_baselines[next(iter(engine._gate_baselines))]
    return observation


def _auto_fix_diagnosis(
    review: QualityGateResult,
    attribution: Attribution | None,
    cap: int,
) -> str:
    """Select the auto-fix prompt's diagnosis section (proposal 124).

    The seam that decides whether the fix round reads the pre-124
    front-truncated ``raw_output`` tail or the enumerated per-gate failure
    block. When *review* carries no
    :class:`~maestro.gate_observation.GateRunObservation` (flag-off, or any
    caller that did not attach one), it returns ``review.raw_output[-cap:]``
    **verbatim**, so the flag-off prompt stays byte-identical to its pre-124
    shape — except that a long-context *cap* reads ``long_raw_output``
    instead, since ``raw_output``'s 1000-char gate tails would leave the
    wider cap nothing to widen into. Otherwise it renders
    :func:`~maestro.gate_observation.render_failure_report` — capped at
    :data:`_AUTO_FIX_FAILURE_ROW_LIMIT` rows and *cap* characters (the same
    ``gate_output_cap`` the tail used, so the long-context seam keeps its
    meaning) — followed by an ``Attribution: {verdict}`` line, and on
    ``INHERITED`` one sentence telling the fixer the failures belong to the
    branch's base so the round spends itself on the diff rather than on
    inherited breakage. ``ENVIRONMENT`` (proposal 133) gets the same
    treatment for the same reason: a bare attribution word the prompt never
    explains is a word the model will read as "try harder", and this one means
    the *opposite* — the gate reached no verdict, so no diff can clear the
    red and rewriting working code to chase it is the failure mode. The round
    is not short-circuited here: the terminal, not the prompt, owns whether an
    environment red ends the loop, and a fixer that can see a real defect in
    the diff is still free to fix it. Pure — reads only the already-computed
    inputs.
    """
    observation = review.observation
    if observation is None:
        if cap > _AUTO_FIX_GATE_OUTPUT_MAX_CHARS and review.long_raw_output:
            return review.long_raw_output[-cap:]
        return review.raw_output[-cap:]
    lines = [
        render_failure_report(
            observation,
            limit=_AUTO_FIX_FAILURE_ROW_LIMIT,
            max_chars=cap,
        ),
        f"Attribution: {attribution}",
    ]
    if attribution == "INHERITED":
        lines.append(
            "These failures are already present on this branch's base and "
            "are not this plan's to fix — repair only the diff, not the "
            "inherited breakage."
        )
    if attribution == "ENVIRONMENT":
        lines.append(
            "The local quality gate could not reach a verdict at all — this "
            "red is the environment's (a missing dependency, a suite that "
            "will not collect, a run that cannot finish in its budget), not "
            "this diff's. No code change can clear it: do not rewrite working "
            "code to chase it. Make no change unless you can see a concrete "
            "defect in the diff itself."
        )
    return "\n".join(lines)


async def _run_auto_fix_query(
    engine: EvolutionEngine,
    plan: EvolutionPlan,
    review: QualityGateResult,
    session: Session | None = None,
    *,
    round_n: int,
    max_rounds: int,
    long_context: bool = False,
    bypass_warning_throttle: bool = False,
    attribution: Attribution | None = None,
) -> bool:
    """Run one LLM-assisted quality-gate fix without committing.

    Returns ``True`` when the SDK call left changes in the working tree
    (commit shape is the caller's responsibility).

    ``round_n`` / ``max_rounds`` are required so the prompt can surface
    the round budget to the LLM and, on round ≥ 2, include the cumulative
    ``git diff HEAD`` so the LLM sees what previous rounds already
    changed instead of re-issuing the same edits. The cumulative-diff
    section is suppressed when ``git diff HEAD`` is empty — the
    ``auto_fix()`` caller amends each round into HEAD between calls,
    so the working tree is clean on round ≥ 2 and an empty section
    would just be misleading noise.

    ``long_context`` widens the three tail-sliced context slices (the
    committed diff, the cumulative diff, and the raw gate output) to
    their long-context caps so the fix step sees the full failing diff
    and complete gate output instead of a truncated tail. It defaults
    to ``False`` — the flag-off legacy capped loop and every
    non-convergence caller keep the standard caps byte-for-byte.
    ``converge_to_green`` passes ``engine._long_context_active(session)``.

    ``bypass_warning_throttle`` forwards to the SDK query so the
    gate-convergence fix loop's must-finish grind skips the soft per-call
    warning-throttle sleep. It defaults to ``False`` — the flag-off legacy
    capped loop and every non-convergence caller keep the throttle pacing
    byte-for-byte; ``converge_to_green`` passes ``True``.

    ``attribution`` is the proposal-124 verdict :meth:`_attribute_review`
    computes for the current red. It selects the prompt's diagnosis section
    via :meth:`_auto_fix_diagnosis`: ``None`` (flag-off, no observation, or
    every non-convergence caller) keeps the pre-124 ``raw_output`` tail at the
    standard cap (a long-context cap reads ``long_raw_output``)
    byte-identical; a verdict swaps in the enumerated per-gate failure block
    plus an ``Attribution:`` line, and on ``INHERITED`` a sentence telling the
    fixer the failures are the base's and not this plan's to fix.
    """
    _note_session_effort_override("auto_fix", "high", session)
    diff_cap = (
        _AUTO_FIX_DIFF_MAX_CHARS_LONG_CONTEXT
        if long_context
        else _AUTO_FIX_DIFF_MAX_CHARS
    )
    gate_output_cap = (
        _AUTO_FIX_GATE_OUTPUT_MAX_CHARS_LONG_CONTEXT
        if long_context
        else _AUTO_FIX_GATE_OUTPUT_MAX_CHARS
    )
    workspace = str(engine._config.workspace)
    try:
        diff_proc = subprocess.run(
            ["git", "diff", "origin/main...HEAD"],
            capture_output=True, text=True,
            timeout=engine._config.git_timeout, cwd=workspace,
        )
        if diff_proc.returncode != 0:
            # A non-zero git exit (e.g. an unresolvable ref) is a genuine
            # failure, not an empty change set. ``origin/main...HEAD`` diffs
            # from the merge-base regardless of commit count, so it stays
            # correct on a single-commit branch where ``HEAD~1`` cannot be
            # resolved; distinguishing the failure keeps a real error from
            # being handed to the model as an empty ``(no diff)`` change set.
            diff_text = "(could not retrieve diff)"
        else:
            diff_text = (
                diff_proc.stdout[-diff_cap:]
                if diff_proc.stdout else "(no diff)"
            )
    except (subprocess.TimeoutExpired, OSError):
        diff_text = "(could not retrieve diff)"

    cumulative_section = ""
    cumulative_text = ""
    cumulative_retrieved = False
    if round_n > 1:
        try:
            cum_proc = subprocess.run(
                ["git", "diff", "HEAD"],
                capture_output=True, text=True,
                timeout=engine._config.git_timeout, cwd=workspace,
            )
            cumulative_text = (
                cum_proc.stdout[-diff_cap:] if cum_proc.stdout else ""
            )
            cumulative_retrieved = True
        except (subprocess.TimeoutExpired, OSError):
            cumulative_text = "(could not retrieve cumulative diff)"
        if cumulative_text:
            cumulative_section = (
                "\n## Working-tree edits since the failed commit\n"
                f"```\n{cumulative_text}\n```\n"
            )

    if round_n > 1 and cumulative_section and cumulative_retrieved:
        preamble = (
            f"Round {round_n} of {max_rounds}. Previous rounds may have"
            f" left edits in the working tree — see the cumulative diff"
            f" below."
        )
    elif round_n > 1 and cumulative_section:
        preamble = (
            f"Round {round_n} of {max_rounds}. Previous rounds may have"
            f" left edits in the working tree, but the cumulative diff"
            f" could not be retrieved — read the relevant files before"
            f" re-issuing edits."
        )
    elif round_n > 1:
        preamble = (
            f"Round {round_n} of {max_rounds}. Earlier rounds were already"
            f" folded into the failed commit; the diff above is the"
            f" cumulative change set."
        )
    else:
        preamble = (
            f"Round {round_n} of {max_rounds}. This is the first attempt."
        )

    prompt = f"""{preamble}

You are fixing quality gate failures in a Maestro evolution.

Plan: {plan.title}

The following diff was committed but failed quality checks:
```
{diff_text}
```
{cumulative_section}
Quality gate output:
{engine._auto_fix_diagnosis(review, attribution, gate_output_cap)}

Fix ALL issues reported above. Do not change the intent of the code — only fix
linting errors, type errors, and test failures. Be precise and minimal.

If you run the test suite to verify a fix, run it through the container-safe gate
lease — `scripts/quality-gates.sh`, or `maestro gate-lease -- python3.14 -m pytest
tests/ -v` — so pytest-xdist is sized to this container's CPU/memory rather than
the whole host. A host-sized parallel run can oversubscribe the loop's
memory-limited cgroup and stall the whole run (#3760)."""

    # Keep the binding acceptance criteria in view during a fix round so
    # the writer satisfies the quality gates without drifting from a
    # criterion. Inert when the flag is off or the plan has no criteria.
    if (
        engine._config.criteria_alignment_enabled
        and plan.acceptance_criteria
    ):
        prompt += format_acceptance_criteria_block(
            plan.acceptance_criteria, style="writing",
        )

    if session is not None:
        await query_on_session(
            session, prompt, caller="auto_fix",
            stream_idle_timeout_seconds=(
                engine._config.sdk_tool_stream_idle_timeout_seconds
            ),
            bypass_warning_throttle=bypass_warning_throttle,
        )
    else:
        identity = engine._consciousness.get_identity_prompt()
        await query(
            prompt=prompt,
            system=identity,
            options=QueryOptions(
                allowed_tools=[
                    "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                ],
                mcp_servers=engine._mcp_servers,
                disallowed_tools=(
                    engine._config.external_mcp_disallowed_tools()
                ),
                effort="high",
                caller="auto_fix",
                cwd=workspace,
                bypass_warning_throttle=bypass_warning_throttle,
                hooks=engine.action_gate_hooks(plan),
            ),
            stream_idle_timeout_seconds=(
                engine._config.sdk_tool_stream_idle_timeout_seconds
            ),
            phase="implementation",
        )

    return engine._vcs.has_changes()


async def auto_fix(
    engine: EvolutionEngine,
    plan: EvolutionPlan,
    review: QualityGateResult,
    session: Session | None = None,
    *,
    round_n: int,
    max_rounds: int,
    long_context: bool = False,
    bypass_warning_throttle: bool = False,
    attribution: Attribution | None = None,
) -> bool:
    """Attempt one LLM-assisted fix of quality gate failures.

    Used by the local-implementation loop, where the auto-fix is
    amended onto the in-progress evolution commit before any push.
    Returns True if changes were made (caller should re-run
    run_quality_gates).

    When *session* is provided, ``effort`` has no effect — the
    session-level effort configured at creation applies instead.

    ``round_n`` / ``max_rounds`` thread through to the prompt so the
    LLM sees the round budget and the cumulative diff of edits it has
    already made on prior rounds.

    ``long_context`` forwards to ``_run_auto_fix_query`` to widen the
    prompt's context slices when the long-context seam is active. It
    defaults to ``False`` so the flag-off legacy caller
    (``agent.implement_plan``) keeps the standard caps unchanged;
    ``converge_to_green`` passes the seam decision.

    ``bypass_warning_throttle`` forwards to ``_run_auto_fix_query`` so the
    gate-convergence fix loop skips the soft per-call warning-throttle
    sleep. It defaults to ``False`` — the legacy ``implement_plan`` caller
    keeps the throttle pacing byte-for-byte; ``converge_to_green`` passes
    ``True``.

    ``attribution`` forwards to ``_run_auto_fix_query`` so the fix prompt can
    state whether the red is the diff's or the base's (proposal 124). It
    defaults to ``None`` — every caller and prompt test other than
    ``converge_to_green`` keeps the diagnosis byte-identical;
    ``converge_to_green`` passes :meth:`_attribute_review`'s verdict.
    """
    if not await engine._run_auto_fix_query(
        plan, review, session, round_n=round_n, max_rounds=max_rounds,
        long_context=long_context,
        bypass_warning_throttle=bypass_warning_throttle,
        attribution=attribution,
    ):
        return False
    engine._vcs.stage_all()
    engine._vcs.amend_commit()
    return True


async def converge_to_green(
    engine: EvolutionEngine,
    plan: EvolutionPlan,
    session: Session | None = None,
    *,
    ceiling: int,
    base_hash: str | None = None,
) -> ConvergenceResult:
    """Grind this branch to green or stop honestly (proposal 110).

    The single owner of the post-implementation fix loop's terminal
    decision. Delegates to :meth:`_converge_rounds`, which runs
    :meth:`run_quality_gates`; if it already passes,
    returns ``"green"`` with ``rounds_used=0``. Otherwise it drives the
    trusted ``auto_fix`` actuator through
    :func:`maestro.gate_convergence.plan_next_round`, which after each
    round decides — from the progress signals :meth:`_progress_signals`
    picks (the *failure-identity* set flag-on, the three coarse gate labels
    flag-off), not merely whether files changed — whether every observed
    gate went CLEAN (``"green"``), the failing set strictly shrank
    (``"continue"``), or it stalled (``"stuck"``). The ``ceiling`` is a
    generous backstop that guarantees termination even while progress
    continues.

    Reuses ``auto_fix``'s amend-commit behavior unchanged, so a
    ``"green"`` result leaves the in-progress evolution commit carrying
    the accumulated fixes.

    When the long-context seam is active (``_long_context_active``), each
    fix round widens its prompt's context slices so the model grinds
    against the full failing diff and complete gate output rather than a
    truncated tail. Computed once here — the seam does not change between
    rounds — and passed to every ``auto_fix`` call.

    Flag-on (``[loop] gate_observation_enabled``), each round also classifies
    the current red via :meth:`_attribute_review` and threads that
    :class:`~maestro.gate_observation.Attribution` verdict through
    ``auto_fix`` into the fix prompt, so on ``INHERITED`` the fixer is told
    the failures belong to the branch's base and are not this plan's to fix.
    Flag-off (or no observation) the verdict is ``None`` and the prompt is
    byte-identical to the pre-124 diagnosis tail.

    Flag-on (``[loop] gate_scope_impacted_enabled``, proposal 134), the
    *inner* rounds are narrowed to the test modules the plan's diff can reach
    (see :meth:`_impacted_scope`) and run ``--no-cov``, so a round costs what
    the diff costs rather than what the suite costs. Two properties keep the
    narrowing from manufacturing a signal: every comparison is made between
    signatures **restricted to the same narrowed suite**
    (:func:`maestro.gate_scope.restrict_signature`), and a narrowed round is
    never a terminal on its own reading — green *or* stuck, it is certified by
    one fresh full-scope, with-coverage read, and only that read may return
    ``"green"`` or be the review the terminal attributes. Exactly one
    ``gate_scope`` activation row records whether the narrow was taken. Off,
    every round, every argv and every terminal are byte-identical to today.

    Every round is issued with ``bypass_warning_throttle=True`` so the soft
    per-call SDK warning-throttle sleep never paces this must-finish grind;
    the hard rate-limit protection and the throttle's own decay counter are
    untouched (see :attr:`maestro.sdk.QueryOptions.bypass_warning_throttle`).
    """
    trace = _ScopeTrace()
    try:
        return await engine._converge_rounds(
            plan, session, ceiling=ceiling, base_hash=base_hash, trace=trace,
        )
    finally:
        # One row per convergence, matching the ``gate_convergence_terminal``
        # and ``self_review_terminal`` convention. Only written flag-on: off,
        # the loop must leave no trace it did not leave before.
        if engine._config.gate_scope_impacted_enabled:
            log_gate_activation(
                engine._gate_activations,
                gate="gate_scope",
                action=trace.action,
                detail=trace.detail,
            )


def _impacted_scope(engine: EvolutionEngine, plan: EvolutionPlan) -> tuple[tuple[str, ...], int]:
    """Select the test modules *plan*'s diff can reach, and the suite's size.

    Returns ``((), 0)`` — the "do not narrow" sentinel plus a denominator the
    activation row will not use — when ``[loop] gate_scope_impacted_enabled``
    is off, so the whole seam costs one flag read on the default path. Flag-on
    it reads the tests tree once through
    :func:`maestro.gate_scope.read_test_sources` and hands that one mapping to
    both :func:`~maestro.gate_scope.select_impacted_tests` (the selection) and
    :func:`~maestro.gate_scope.count_test_modules` (the denominator the row's
    fraction is measured against), rather than paying two walks of the tree.
    Both gate path atoms come from :meth:`maestro.config.Config.work_target`,
    never a literal.
    """
    if not engine._config.gate_scope_impacted_enabled:
        return (), 0
    wt = engine._config.work_target()
    sources = gate_scope.read_test_sources(
        engine._config.workspace, wt.gate_tests_path,
    )
    selected = gate_scope.select_impacted_tests(
        plan.files_affected,
        sources,
        source_path=wt.gate_source_path,
        tests_path=wt.gate_tests_path,
    )
    return selected, gate_scope.count_test_modules(
        sources, tests_path=wt.gate_tests_path,
    )


def _restricted_signature(
    review: QualityGateResult,
    impacted: Sequence[str],
    *,
    own_run: bool = False,
) -> frozenset[str]:
    """Project *review*'s failure signature onto the narrowed *impacted* suite.

    The one reader of :func:`maestro.gate_scope.restrict_signature` in the fix
    loop, so the baseline, each round and the re-baselining after a widening
    cannot drift apart on how a full red is projected. A *review* carrying no
    observation — ``[loop] gate_observation_enabled`` off, whose coarse
    three-label signature has no per-failure locations to restrict — yields the
    empty set, which the baseline path reads as "do not narrow".

    *own_run* distinguishes the two directions the projection serves. False —
    the default — *review* is a full-scope read being projected onto the
    narrowed terms as a baseline, and its "did not answer" fact is about a
    suite the narrowed probe will not run. True — *review* is the narrowed
    round's **own** result, so a non-answer is about that probe and its
    synthetic identity is retained; otherwise a narrowed round killed at the
    deadline with no in-scope survivors would restrict to the empty set and be
    read as maximal progress by a gate that never finished.
    """
    observation = review.observation
    if observation is None:
        return frozenset()
    return gate_scope.restrict_signature(
        observation, impacted, retain_non_answer=own_run,
    )


def _widen_scope(
    review: QualityGateResult, impacted: Sequence[str], suite_size: int,
) -> tuple[str, ...]:
    """Widen *impacted* with the test modules *review*'s own failures name.

    Called after a red full-scope certification that still shows progress: the
    surviving red sits (at least partly) outside the modules the diff selected,
    so the next narrowed rounds must be able to see it. Reads only the already
    enumerated failure locations — no disk, no re-selection.

    The widening is **bounded**: a broad red names many modules, and a
    selection past :data:`_MAX_IMPACTED_FRACTION` of *suite_size* buys nothing
    — an explicit argv listing most of the suite is slower than running the
    suite, so the narrowing would cost more than it saves. Past that fraction
    (and whenever *suite_size* is unknown, i.e. zero) this returns the empty
    "do not narrow" sentinel and the remaining rounds run full-scope.
    """
    widened = set(impacted)
    observation = review.observation
    if observation is not None:
        for gate in observation.gates:
            if gate.tool != "tests":
                continue
            for failure in gate.failures:
                if failure.location:
                    widened.add(failure.location)
    if suite_size <= 0 or len(widened) > suite_size * _MAX_IMPACTED_FRACTION:
        return ()
    return tuple(sorted(widened))


async def _converge_rounds(
    engine: EvolutionEngine,
    plan: EvolutionPlan,
    session: Session | None,
    *,
    ceiling: int,
    base_hash: str | None,
    trace: _ScopeTrace,
) -> ConvergenceResult:
    """Drive the fix loop's rounds — :meth:`converge_to_green`'s whole body.

    Split out so the single ``gate_scope`` activation row can be written at
    exactly one place (the caller's ``finally``) no matter which terminal the
    loop takes, rather than being restated at every ``return``. *trace* is the
    mutable record that row is rendered from.
    """
    review = engine.run_quality_gates()
    prev_failures, all_clean = engine._progress_signals(review)
    if all_clean:
        return engine._convergence_terminal(
            "green", review, rounds_used=0, plan=plan, base_hash=base_hash,
        )
    impacted, suite_size = engine._impacted_scope(plan)
    if impacted:
        # The baseline the first narrowed round is compared against must be the
        # pre-loop red *as the narrowed suite would have seen it*. An empty
        # restriction means the red lies wholly outside the selection (a lone
        # ``tests:TIMEOUT``, a coverage-only shortfall, or a failure in a module
        # the diff cannot reach), so there is nothing to converge against
        # narrowly and the loop degrades to today's whole-suite rounds.
        restricted = engine._restricted_signature(review, impacted)
        if restricted:
            prev_failures = restricted
            trace.action = "narrowed"
            trace.detail = (
                f"modules={len(impacted)}/{suite_size} "
                f"baseline={len(restricted)}"
            )
        else:
            trace.detail = (
                f"discarded: modules={len(impacted)}/{suite_size} "
                "red outside the impacted set"
                if review.observation is not None
                else "discarded: no observation (gate_observation_enabled off)"
            )
            impacted = ()
    elif engine._config.gate_scope_impacted_enabled:
        trace.detail = f"no impacted modules ({suite_size} in suite)"
    prev_full = engine._progress_signals(review)[0]
    prev_full_count, prev_full_truncated = engine._progress_counts(review)
    # The count fallback compares whole-run counts, which are only comparable
    # across two full-scope reads; a narrowed round's restricted set is not a
    # whole-run count, so it keeps the subset compare. Known limitation: ruff
    # and mypy still run whole in a narrowed round, so a >_MAX_FAILURES window
    # of theirs can still churn there; the certification read that follows
    # every narrowed terminal uses the counts, so it cannot end the loop.
    prev_count, prev_truncated = (
        (prev_full_count, prev_full_truncated) if not impacted else (0, False)
    )
    long_context = engine._long_context_active(session)
    rounds_used = 0
    while True:
        failing = sorted(prev_failures)
        logger.warning(
            "Gate convergence round %d (ceiling %d) — failing set: %d "
            "(sample: %s)",
            rounds_used + 1, ceiling, len(failing), failing[:5],
        )
        attribution, _ = engine._attribute_review(review, base_hash)
        fixed = await engine.auto_fix(
            plan, review, session,
            round_n=rounds_used + 1, max_rounds=ceiling,
            long_context=long_context,
            bypass_warning_throttle=True,
            attribution=attribution,
        )
        if not fixed:
            # No files changed — no further progress is possible.
            logger.info(
                "Gate convergence: auto_fix made no changes on round %d "
                "— ending the fix loop", rounds_used + 1,
            )
            return engine._convergence_terminal(
                "stuck", review, rounds_used=rounds_used,
                plan=plan, base_hash=base_hash, reread=True,
            )
        rounds_used += 1
        # The keyword is passed only when there is something to narrow, so a
        # flag-off round is the same no-argument call it has always been —
        # including at every test seam that asserts on it.
        review = (
            engine.run_quality_gates(scope=impacted)
            if impacted
            else engine.run_quality_gates()
        )
        curr_failures, all_clean = engine._progress_signals(review)
        curr_count, curr_truncated = engine._progress_counts(review)
        if impacted:
            curr_failures = engine._restricted_signature(
                review, impacted, own_run=True,
            )
            curr_count, curr_truncated = 0, False
        decision = plan_next_round(
            prev_failures, curr_failures,
            all_clean=all_clean,
            rounds_used=rounds_used, ceiling=ceiling,
            prev_count=prev_count, curr_count=curr_count,
            truncated=prev_truncated or curr_truncated,
        )
        if decision == "green" and impacted:
            # A narrowed green is a claim about a subset, never a terminal.
            # One fresh full-scope, with-coverage read is the only thing that
            # may certify it — and its cost is charged as a round, so a loop
            # that keeps clearing the subset while the suite stays red still
            # terminates.
            certification = engine.run_quality_gates()
            cert_failures, cert_clean = engine._progress_signals(certification)
            cert_count, cert_truncated = engine._progress_counts(certification)
            review = certification
            if cert_clean:
                trace.detail += f" certified={rounds_used}"
                return engine._convergence_terminal(
                    "green", review, rounds_used=rounds_used,
                    plan=plan, base_hash=base_hash,
                )
            rounds_used += 1
            decision = plan_next_round(
                prev_full, cert_failures,
                all_clean=False,
                rounds_used=rounds_used, ceiling=ceiling,
                prev_count=prev_full_count, curr_count=cert_count,
                truncated=prev_full_truncated or cert_truncated,
            )
            if decision == "stuck":
                return engine._convergence_terminal(
                    "stuck", review, rounds_used=rounds_used,
                    plan=plan, base_hash=base_hash,
                )
            # A strict subset: real progress the narrowed suite could not see.
            # Re-baseline both signatures on it and widen the selection to the
            # modules the survivors name, so the next rounds can act on them.
            prev_full = cert_failures
            prev_full_count, prev_full_truncated = cert_count, cert_truncated
            impacted = engine._widen_scope(certification, impacted, suite_size)
            restricted = (
                engine._restricted_signature(certification, impacted)
                if impacted
                else frozenset()
            )
            if restricted:
                prev_failures = restricted
                trace.detail += f" widened={len(impacted)}"
            else:
                impacted = ()
                prev_failures = cert_failures
                prev_count, prev_truncated = cert_count, cert_truncated
                trace.detail += " widened=full"
            continue
        if decision == "green":
            return engine._convergence_terminal(
                "green", review, rounds_used=rounds_used,
                plan=plan, base_hash=base_hash,
            )
        if decision == "stuck":
            if impacted:
                # A narrowed observation enumerates only the selected subset,
                # and the terminal attributes *its* review: fewer enumerated
                # identities makes ``INHERITED`` strictly easier to reach, so a
                # diff-caused red in an unselected module could be held as the
                # base's. The stuck terminal is certified on one fresh
                # full-scope read, exactly as the green terminal is.
                review = engine.run_quality_gates()
                trace.detail += " stuck-certified"
            return engine._convergence_terminal(
                "stuck", review, rounds_used=rounds_used,
                plan=plan, base_hash=base_hash,
            )
        prev_failures = curr_failures
        prev_count, prev_truncated = curr_count, curr_truncated


def _progress_signals(review: QualityGateResult) -> tuple[frozenset[str], bool]:
    """Return the (failure signature, all-clean) pair the fix loop compares.

    The single seam that picks the convergence loop's progress vocabulary from
    the flag (proposal 124). Flag *on* — ``review.observation`` is a
    :class:`~maestro.gate_observation.GateRunObservation` — it returns the
    *failure-identity* signature (:meth:`GateRunObservation.progress_signature`,
    one position-free identity per failing test / diagnostic, so a line shift
    cannot rename a surviving one — #4309) and per-gate CLEAN status
    (:meth:`GateRunObservation.all_clean`), so clearing eleven of twelve
    failing tests is a strictly shrinking set and the ceiling becomes the real
    budget it advertises. Flag *off* — ``observation is None`` — it returns the
    pre-124 pair ``(review.failure_signature(), review.passed)``, the three
    coarse gate labels and the aggregate pass, so the loop is byte-identical to
    its pre-124 behaviour (``passed`` is true iff ``failure_signature()`` is
    empty, so binding green to ``all_clean`` reproduces the old empty-set rule
    exactly). Pure — reads only the already-computed *review*.
    """
    if review.observation is not None:
        return (
            review.observation.progress_signature(),
            review.observation.all_clean(),
        )
    return review.failure_signature(), review.passed


def _progress_counts(review: QualityGateResult) -> tuple[int, bool]:
    """Return the (full-enumeration count, any-truncated) pair for *review*.

    The companion of :meth:`_progress_signals` that
    :func:`~maestro.gate_convergence.plan_next_round` falls back to when an
    enumeration hit the parser cap (#4309): two capped windows are not
    comparable as sets, the counts are. Flag off — no observation — the
    coarse labels are never truncated, so it returns ``(len, False)``. Pure.
    """
    observation = review.observation
    if observation is None:
        return len(review.failure_signature()), False
    return observation.enumerated_count(), observation.any_truncated()


def _attribute_review(
    engine: EvolutionEngine,
    review: QualityGateResult,
    base_key: str | None,
) -> tuple[Attribution | None, gate_environment.GateRunnabilityVerdict | None]:
    """Classify *review*'s red against the branch's diff and its base.

    The single attribution reader the convergence loop shares (proposal 124),
    so the fix loop (:meth:`converge_to_green`, which threads the verdict into
    each :meth:`auto_fix` round) and the terminal
    (:meth:`_convergence_terminal`) cannot drift apart on *how* a red is
    classified. Returns ``None`` — the byte-identical pre-124 path — when
    ``[loop] gate_observation_enabled`` is off or *review* carries no
    :class:`~maestro.gate_observation.GateRunObservation`. Otherwise it hands
    :func:`~maestro.gate_observation.attribute` the observation, the
    ``base_key``-to-HEAD diff read from :meth:`VCS.changed_paths` — the same
    VCS-derived boundary the sibling reader
    (:meth:`~maestro.agent.MaestroAgent._attribute_owned_gate_red`) measures,
    never the model-authored ``files_affected`` metadata, which can
    under- or over-declare what the implementation actually touched — and the
    cached base-commit baseline served from :meth:`_observe_baseline` (which
    itself yields ``None`` — and issues no subprocess — off the flag or on an
    empty *base_key*). A missing head or a missing *base_key* is
    ``UNATTRIBUTABLE``, mirroring that sibling's fail-safe: without a base
    there is no boundary to measure the red against — but that case is still
    routed through :func:`attribute` with an empty diff and no baseline
    rather than returned directly, so an environment-owned red keeps its
    ``ENVIRONMENT`` precedence (that arm reads only *runnability*, never the
    boundary). Zero SDK cost.

    It also carries the proposal-133 runnability verdict into the attributor,
    so a red the environment owns is named ``ENVIRONMENT`` rather than blamed
    on the diff. The verdict is *read*, never probed:
    :func:`maestro.gate_environment.read_verdict` is the zero-subprocess
    reader of the row the selector
    (:func:`maestro.loop.plan_holds.gate_environment_verdict`) already
    refreshed once this iteration — the probe is the selector's to own, never
    this reader's. With ``[loop] gate_environment_oracle_enabled`` off, or
    with no cached row, ``None`` is passed and :func:`attribute` behaves
    exactly as it did pre-133.

    Returns the ``(attribution, runnability)`` pair rather than the
    attribution alone, so the verdict this call already read is *carried* to
    the terminal instead of re-read there. That is what keeps
    :attr:`~maestro.gate_convergence.ConvergenceResult.runnability`
    non-``None`` whenever the attribution is ``ENVIRONMENT``: a second
    independent read could return ``None`` if the cached row moved in between,
    and the hold that consumes it treats a ``None`` fingerprint as *clearing*
    the stamp. Round callers that want only the verdict discard the second
    element.
    """
    if not engine._config.gate_observation_enabled:
        return None, None
    observation = review.observation
    if observation is None:
        return None, None
    runnability = (
        gate_environment.read_verdict(engine._config.gate_environment_path)
        if engine._config.gate_environment_oracle_enabled
        else None
    )
    head = engine._vcs.get_head_hash()
    if head and base_key:
        changed: Sequence[str] = engine._vcs.changed_paths(base_key, head)
        baseline = engine._observe_baseline(base_key)
    else:
        # No boundary to measure against: an empty diff and no baseline is
        # ``UNATTRIBUTABLE`` by construction, and reading neither seam costs
        # no subprocess. Still routed through the one :func:`attribute` call
        # rather than returned early, so the proposal-133 ``ENVIRONMENT``
        # arm — which outranks every boundary-derived verdict and needs no
        # boundary at all — keeps its precedence here too.
        changed, baseline = (), None
    return attribute(
        observation, changed, baseline, runnability=runnability,
    ), runnability


def _convergence_terminal(
    engine: EvolutionEngine,
    outcome: ConvergenceDecision,
    review: QualityGateResult,
    *,
    rounds_used: int,
    plan: EvolutionPlan,
    base_hash: str | None,
    reread: bool = False,
) -> ConvergenceResult:
    """Build the single terminal :class:`ConvergenceResult` for the fix loop.

    The sole constructor of :class:`ConvergenceResult` in
    :meth:`converge_to_green` (proposal 124). Flag-off it is byte-identical to
    the pre-124 return — no re-read, no attribution, no ``gate_activations``
    row — so the loop's every terminal is unchanged when
    ``[loop] gate_observation_enabled`` is off.

    Flag-on it does two things the coarse pre-124 exit could not. When
    *reread* is set — the "auto_fix made no changes" path, whose *review* is
    the **pre-actuator** gate result captured before the fix ran — it re-runs
    :meth:`run_quality_gates` and re-derives ``outcome`` from that fresh read,
    so a branch the environment repair (a dependency install, a cache purge)
    turned green is never demoted on a red that stopped being true minutes
    earlier. It then classifies the (possibly re-read) observation against the
    branch's base→HEAD diff and the cached base-commit baseline via
    :func:`~maestro.gate_observation.attribute` — ``INHERITED`` when the red is
    the base's, not the diff's — records that verdict on the result, and
    appends exactly one ``gate_convergence_terminal`` activation row. The
    baseline is served from :meth:`_observe_baseline`'s cache and, on a branch
    head (``HEAD != base_hash``), never re-runs the gate or checks anything out.

    On an ``ENVIRONMENT`` attribution (proposal 133) it also carries the
    runnability verdict that produced it onto
    :attr:`~maestro.gate_convergence.ConvergenceResult.runnability`, so the
    hold that consumes it never re-reads the cached row — see that field for
    why a second read is not merely wasteful but unsafe. Every other
    attribution carries ``None``, so nothing downstream can read a verdict the
    attribution did not rest on.
    """
    if not engine._config.gate_observation_enabled:
        return ConvergenceResult(
            outcome, review, rounds_used=rounds_used, attribution=None,
        )
    if reread:
        review = engine.run_quality_gates()
        outcome = "green" if engine._progress_signals(review)[1] else "stuck"
    attribution, runnability = engine._attribute_review(review, base_hash)
    log_gate_activation(
        engine._gate_activations,
        gate="gate_convergence_terminal",
        action=outcome,
        detail=f"[{attribution}] rounds={rounds_used} "
        f"base={(base_hash or '')[:8]}",
    )
    return ConvergenceResult(
        outcome, review, rounds_used=rounds_used, attribution=attribution,
        runnability=runnability if attribution == "ENVIRONMENT" else None,
    )


def _review_terminal(
    engine: EvolutionEngine,
    *,
    approved: bool,
    made_changes: bool,
    rounds_used: int,
    ceiling: int,
    verify_grace_remaining: int,
) -> tuple[ConvergenceDecision, int]:
    """Decide one self-review round's terminal, reading the gate when needed.

    The self-review sibling of :meth:`_convergence_terminal` (proposal 124),
    and the single owner of the review loop's terminal decision. Returns the
    :class:`~maestro.gate_convergence.ConvergenceDecision` plus how much
    verify-only grace this round consumed (``1`` iff it converted a
    would-be demotion into a further round, else ``0``).

    Flag-off (``[loop] gate_observation_enabled`` unset) it is a pure
    pass-through to :func:`~maestro.gate_convergence.plan_next_review_round`
    with ``gate_clean=False, verify_grace_remaining=0`` and consumes no
    grace, so the loop is byte-identical to its pre-124 behaviour and issues
    **no** :meth:`run_quality_gates` call at all.

    Flag-on it re-reads the gate — via the same
    :meth:`_progress_signals` seam :meth:`converge_to_green` already picks
    its ``all_clean`` flag off — but **only** on the path that would
    otherwise be ``"stuck"`` for lack of an undirtied tree: not approved, no
    committed change, still under the ceiling, and grace still unspent. It
    never fires on an approval, a fix-committing round, or a ceiling exit, so
    a green branch is never demoted on a stale-clean-tree proxy while the
    ``LGTM`` requirement (no unapproved head proceeds) is untouched, and one
    ``self_review`` pays at most one extra gate run. It appends exactly one
    ``self_review_terminal`` activation row per terminal exit (``"green"`` /
    ``"stuck"``), whose ``detail`` carries the rounds used and the grace
    spent — recorded in ``gate_activations.jsonl`` for direct inspection.
    (``maestro measure-gate-triggers`` reports only the per-gate firing
    *count*, not the ``action`` or ``detail``, so how often the grace
    converted a would-be demotion into a further round is read from the
    trace's own rows, not that CLI.) This reader accepts no head toward a
    PR — it only decides whether the review loop keeps going.

    Blocking: flag-on the gate re-read is a full ruff + mypy + pytest sweep,
    so :meth:`self_review` hands this sync seam to ``asyncio.to_thread``
    rather than stalling its event loop for the duration — the same
    convention :meth:`_observe_baseline` documents. It therefore stays a
    plain (non-``async``) method whose lone impurity is that one gate read.
    """
    if not engine._config.gate_observation_enabled:
        decision = plan_next_review_round(
            approved=approved,
            made_changes=made_changes,
            rounds_used=rounds_used,
            ceiling=ceiling,
            gate_clean=False,
            verify_grace_remaining=0,
        )
        return decision, 0

    would_be_stuck = (
        not approved
        and not made_changes
        and rounds_used < ceiling
        and verify_grace_remaining > 0
    )
    gate_clean = False
    if would_be_stuck:
        # The re-read is deterministic and zero-SDK, and fires only here — on
        # the path that today ends in a demote. ``_progress_signals`` returns
        # ``(signature, all_clean)``; the second element is the CLEAN verdict.
        gate_clean = engine._progress_signals(engine.run_quality_gates())[1]

    decision = plan_next_review_round(
        approved=approved,
        made_changes=made_changes,
        rounds_used=rounds_used,
        ceiling=ceiling,
        gate_clean=gate_clean,
        verify_grace_remaining=verify_grace_remaining,
    )
    # A grace-bought continuation is the only round that consumes grace; a
    # committed-fix continuation (``made_changes``) does not.
    grace_consumed = 1 if would_be_stuck and gate_clean else 0
    if decision != "continue":
        grace_spent = _VERIFY_ONLY_REVIEW_ROUND_BUDGET - verify_grace_remaining
        log_gate_activation(
            engine._gate_activations,
            gate="self_review_terminal",
            action=decision,
            detail=f"rounds={rounds_used} grace_spent={grace_spent}",
        )
    return decision, grace_consumed


async def self_review(
    engine: EvolutionEngine,
    plan: EvolutionPlan,
    base_hash: str,
    session: Session | None = None,
) -> bool:
    """Review implementation against the plan.

    A progress-gated review-fix loop (proposal 110) mirroring the sibling
    :meth:`converge_to_green` gate grind: each round gets the diff from
    *base_hash* to HEAD and asks the LLM to review the changes against the
    plan.  If the reviewer finds issues it fixes them (full tool access)
    and commits; if everything looks good it responds with ``LGTM``.  The
    loop keeps going while each round makes progress (a fix committed) and
    terminates on the same two honest outcomes the gate loop uses —
    ``LGTM`` (proceed) or a no-progress stall / ceiling backstop.
    ``self_review_max_rounds`` is no longer a hard terminal: it only raises
    the convergence ceiling floor (``max`` with ``gate_convergence_ceiling``),
    exactly as ``auto_fix_max_rounds`` does for the gate loop, so a review
    that is still resolving real issues is never cut off mid-progress.

    Three arms run each round.  When *session* is provided, the review
    runs on the same session as the implementation — the reviewer sees
    the full implementation conversation context and the session-level
    effort configured at creation applies.  With no *session* and
    ``[loop] self_review_own_session_enabled`` on, each round opens a
    fresh session via :func:`single_use_call` at the registry's ``high``
    effort, carrying the plan's action-gate hooks.  Otherwise the round
    is a stateless ``query`` at ``high`` effort.

    Returns ``True`` when the review reached ``LGTM`` (or was benignly
    skipped: unobtainable HEAD, empty ``base_hash``, or empty diff — the
    anomalous-VCS cases that continue to proceed as before), and ``False``
    on a no-progress stall or the ceiling backstop without approval.  The
    caller in ``agent.implement_plan`` routes a ``False`` return to the
    loud fail-loud terminal (ERROR + branch cleanup + demote to
    ``failed``), so no unapproved head ever proceeds to the gate loop or
    ``create_pr``.
    """
    _note_session_effort_override("self_review", "high", session)
    # ``self_review_max_rounds`` is now only a ceiling-floor input, not a
    # hard cap: it folds into the generous convergence ceiling exactly as
    # ``auto_fix_max_rounds`` does in ``converge_to_green``.
    ceiling = max(
        engine._config.gate_convergence_ceiling,
        engine._config.self_review_max_rounds,
    )
    rounds_used = 0
    # Seed the verify-only grace once at loop entry and never replenish it
    # within this call (proposal 124): a would-be stall against a green
    # branch may buy at most ``_VERIFY_ONLY_REVIEW_ROUND_BUDGET`` further
    # rounds, so one ``self_review`` pays at most one extra gate run. That
    # single gate read is deliberately decision-local and is *not* threaded
    # forward: on the ``LGTM``-after-grace happy path the caller
    # (``agent.implement_plan``) next runs ``converge_to_green``, whose own
    # pre-loop ``run_quality_gates`` re-reads the same tree — an accepted,
    # documented double sweep, since ``converge_to_green`` owns its gate
    # acceptance and must read the tree it is about to grind, not inherit a
    # stale bool from a sibling terminal.
    verify_grace_remaining = _VERIFY_ONLY_REVIEW_ROUND_BUDGET
    # Set once a round buys a verify-only continuation, so the *next* round's
    # prompt can state the measured-green gate fact the grace read paid for
    # (proposal 124) — otherwise the grace round re-sends a byte-identical
    # prompt and the reviewer is likely to repeat the same non-``LGTM``
    # prose. Flag-off no grace is ever consumed, so this stays ``False`` and
    # the prompt is byte-identical.
    verify_round_ahead = False
    # ``_long_context_active`` owns the seam decision — the diff cap
    # expands to match the larger window the long-context session (or
    # fresh-context fallback) already holds; see the helper's docstring.
    # The model string is still needed below to attach model+betas on the
    # fresh-context query path.
    long_context_model = engine._config.sdk_model_long_context
    long_context_active = engine._long_context_active(session)
    diff_cap = (
        _SELF_REVIEW_DIFF_MAX_CHARS_LONG_CONTEXT
        if long_context_active
        else _SELF_REVIEW_DIFF_MAX_CHARS
    )

    while True:
        round_n = rounds_used + 1
        current_head = engine._vcs.get_head_hash() or ""
        if not current_head:
            # Anomalous VCS state: HEAD could not be resolved.  Loud
            # WARNING per the "fail loud" invariant — this is a real
            # problem, distinct from a benign empty diff below.
            logger.warning(
                "Self-review round %d (ceiling %d): could not obtain HEAD,"
                " skipping review",
                round_n, ceiling,
            )
            return True
        if not base_hash:
            # Anomalous VCS state produced upstream: base_hash is coerced
            # from ``get_head_hash() or ""`` in implement(), so an empty
            # value means HEAD was unobtainable at implement time.  With a
            # valid current_head, ``get_diff("", current_head)`` returns ""
            # *silently* (empty-arg guard, no WARNING), which would misroute
            # this into the benign no-changes branch below and downgrade a
            # real problem to INFO.  Loud WARNING per the "fail loud"
            # invariant, matching the no-HEAD case above.
            logger.warning(
                "Self-review round %d (ceiling %d): no base commit to diff"
                " against, skipping review",
                round_n, ceiling,
            )
            return True
        # Request the uncapped diff so ``_truncate_diff`` below is the
        # single truncation authority: it head+tail truncates at
        # ``diff_cap`` with an accurate marker and fires the WARNING.
        # (A ``get_diff`` pre-cap at the same 50K would silently hide
        # the tail and make the long-context cap unreachable.)
        diff = engine._vcs.get_diff(base_hash, current_head, max_chars=0)
        if not diff:
            # Empty diff between two valid commits: nothing changed
            # this round (e.g. base_hash == current_head).  Benign
            # no-op — INFO, not WARNING.  A genuine ``git diff``
            # failure is already surfaced by ``get_diff``'s own
            # WARNING, which the caller cannot re-derive.
            logger.info(
                "Self-review round %d (ceiling %d): no changes to review,"
                " skipping",
                round_n, ceiling,
            )
            return True

        # The uncalled/literal parsers read the complete diff: the
        # truncated copy below is for the prompt only, and parsing
        # across its splice would misattribute files (#4217).
        full_diff = diff
        original_diff_len = len(diff)
        diff = _truncate_diff(diff, max_chars=diff_cap)
        if len(diff) != original_diff_len:
            logger.warning(
                "Self-review round %d (ceiling %d): diff truncated from %d"
                " to %d chars (cap=%d) — large changes may not be fully"
                " reviewed",
                round_n, ceiling, original_diff_len, len(diff),
                diff_cap,
            )

        uncalled_warnings = engine._find_uncalled_symbols(full_diff)
        uncalled_block = ""
        if uncalled_warnings:
            bullet_list = _format_warning_bullets(uncalled_warnings)
            uncalled_block = (
                "\n## Uncalled Symbol Warnings\n"
                "The following new symbols have no external call sites"
                " outside their definition file and tests/:\n"
                f"{bullet_list}\n"
            )

        literal_warnings = _find_literal_only_functions(
            _extract_added_source(full_diff)
        )
        literal_block = ""
        if literal_warnings:
            bullet_list = _format_warning_bullets(literal_warnings)
            literal_block = (
                "\n## Hollow Artifact Warnings\n"
                "The following newly-added functions only return a"
                " constant literal — they name work they do not perform:\n"
                f"{bullet_list}\n"
            )

        # On a grace-bought verify round, surface the measured-green gate
        # fact the previous round's terminal read paid for, so the reviewer
        # decides its LGTM against new information rather than re-reading a
        # byte-identical prompt (proposal 124). Reset per round: the fact is
        # only true for the one round the grace bought.
        gate_status_block = ""
        if verify_round_ahead:
            gate_status_block = (
                "\n## Gate Status\n"
                "The full quality-gate suite (ruff, mypy, and pytest) was"
                " just run against this branch and every gate is GREEN — no"
                " gate failure remains to fix. If the implementation"
                " satisfies the plan, respond with exactly: LGTM.\n"
            )

        criteria_block = format_acceptance_criteria_block(
            plan.acceptance_criteria, style="binding",
        )
        # Build the review checklist with computed (not hard-coded)
        # numbering: the first four items and the trailing minimalism item
        # are always present; the middle items are appended only when their
        # trigger fires (proposal 108 made the CLAUDE.md item opt-in).
        # Numbering the surviving items in sequence keeps the list gap-free
        # when an optional item is dropped.
        checklist_items = [
            "**Completeness** — are all proposed changes implemented?",
            "**Correctness** — does the code do what the plan describes?",
            "**Quality** — no debug code, no missing error handling at"
            " boundaries, clean style",
            "**Tests** — are new/changed behaviors covered by tests?",
        ]
        if engine._config.require_claude_md_update:
            checklist_items.append(
                "**CLAUDE.md** — is it updated if architecture changed?"
            )
        if plan.acceptance_criteria:
            checklist_items.append(
                "**Acceptance criteria** — every binding criterion above"
                " is implemented AND has a test that would fail if it were"
                " violated."
            )
        if uncalled_warnings:
            checklist_items.append(
                "**Uncalled symbols** — every new public function/class is"
                " called from production code or has a documented reason"
                " for being unused."
            )
        if literal_warnings:
            checklist_items.append(
                "**Hollow artifacts** — every new function computes its"
                " result or has a documented reason for returning a"
                " constant literal."
            )
        checklist_items.append(
            "**Minimalism** — the change reuses existing codebase,"
            " standard-library, or installed-dependency behavior rather"
            " than reinventing it, adds no single-caller abstraction (no"
            " interface with one implementation, no factory for one"
            " product), and is the smallest diff that satisfies the plan;"
            " a net-subtractive diff that removes more than it adds"
            " satisfies this item rather than raising suspicion."
        )
        review_checklist = "\n".join(
            f"{pos}. {item}"
            for pos, item in enumerate(checklist_items, 1)
        )

        prompt = f"""You are reviewing an evolution implementation in a fresh context.

## Evolution Plan
**Title:** {plan.title}
**Rationale:** {plan.rationale}
**Proposed changes:**
{chr(10).join(f"- {c}" for c in plan.proposed_changes)}

**Files expected to be modified:** {plan.files_affected}
{criteria_block}
## Implementation Diff
```diff
{diff}
```
{uncalled_block}{literal_block}{gate_status_block}
## Review Instructions
Review the implementation against the plan above. Check for:
{review_checklist}

If you find issues, fix them using the available tools (Read, Write, Edit, Bash,
Glob, Grep). After fixing, the changes will be committed automatically.

If everything looks good, respond with exactly: LGTM

Review round {round_n} (ceiling {ceiling})."""

        if session is not None:
            fence = (
                f"=== SELF-REVIEW ROUND {round_n} (CEILING {ceiling}) ===\n"
                "You are now reviewing the implementation you just completed."
                " Focus on the diff below.\n\n"
            )
            if long_context_active:
                logger.info(
                    "long-context call: caller=self_review session=reuse "
                    "model=%s prompt_chars=%d",
                    long_context_model, len(fence) + len(prompt),
                )
            result = await query_on_session(
                session, fence + prompt, caller="self_review",
                stream_idle_timeout_seconds=(
                    engine._config.sdk_tool_stream_idle_timeout_seconds
                ),
            )
        else:
            identity = engine._consciousness.get_identity_prompt()
            if long_context_active:
                logger.info(
                    "long-context call: caller=self_review model=%s "
                    "prompt_chars=%d",
                    long_context_model, len(prompt),
                )
            if engine._config.self_review_own_session_enabled:
                result = await single_use_call(
                    "self_review",
                    prompt,
                    system=identity,
                    options=QueryOptions(
                        allowed_tools=[
                            "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                        ],
                        mcp_servers=engine._mcp_servers,
                        disallowed_tools=(
                            engine._config.external_mcp_disallowed_tools()
                        ),
                        cwd=str(engine._config.workspace),
                        model=(
                            long_context_model if long_context_active
                            else None
                        ),
                        betas=(
                            [LONG_CONTEXT_BETA] if long_context_active
                            else None
                        ),
                        hooks=engine.action_gate_hooks(plan),
                    ),
                    stream_idle_timeout_seconds=(
                        engine._config.sdk_tool_stream_idle_timeout_seconds
                    ),
                )
            else:
                result = await query(
                    prompt=prompt,
                    system=identity,
                    options=QueryOptions(
                        allowed_tools=[
                            "Read", "Write", "Edit", "Bash", "Glob", "Grep",
                        ],
                        mcp_servers=engine._mcp_servers,
                        disallowed_tools=(
                            engine._config.external_mcp_disallowed_tools()
                        ),
                        effort="high",
                        caller="self_review",
                        cwd=str(engine._config.workspace),
                        model=long_context_model if long_context_active else None,
                        betas=(
                            [LONG_CONTEXT_BETA] if long_context_active else None
                        ),
                        hooks=engine.action_gate_hooks(plan),
                    ),
                    stream_idle_timeout_seconds=(
                        engine._config.sdk_tool_stream_idle_timeout_seconds
                    ),
                    phase="implementation",
                )

        rounds_used += 1
        response_text = result.response.strip()
        approved = _is_lgtm(response_text)
        made_changes = False
        if engine._vcs.has_changes():
            # Reviewer resolved something this round — commit the fixes
            # whether or not it also approved: a "Fixed X.\n\nLGTM" reply
            # still left edits in the tree, and no later step commits them
            # on the green path, so the pushed PR would miss them (#4067).
            made_changes = True
            engine._vcs.stage_all()
            engine._vcs.commit(
                f"maestro: self-review fixes (round {round_n})",
            )
            logger.info(
                "Self-review round %d (ceiling %d): fixes committed%s",
                round_n, ceiling, "; LGTM" if approved else "",
            )
        elif approved:
            logger.info(
                "Self-review round %d (ceiling %d): LGTM",
                round_n, ceiling,
            )
        elif (
            engine._config.gate_observation_enabled
            and (engine._vcs.get_head_hash() or "") != current_head
        ):
            # Retire the tree-dirtiness proxy for the self-committing
            # reviewer (proposal 124's second motivating case): the action
            # gate scopes the review agent's ``Bash`` to this evolve branch,
            # so an agent that stages and commits its own fix hands back a
            # clean tree while HEAD has advanced past this round's start.
            # ``has_changes()`` cannot see that fix; the moved HEAD can.
            # Flag-on, count it as real progress — a continuation, not a
            # stall — so a productively self-committing reviewer is never
            # demoted on an undirtied tree, and no verify-only grace is
            # spent on a round that already made progress. Flag-off the
            # short-circuit keeps this branch dead and the loop
            # byte-identical to its pre-124 behaviour.
            made_changes = True
            logger.info(
                "Self-review round %d (ceiling %d): reviewer committed its"
                " own fix (HEAD advanced past round start)",
                round_n, ceiling,
            )
        else:
            logger.info(
                "Self-review round %d (ceiling %d): no fixes needed but"
                " no LGTM",
                round_n, ceiling,
            )

        # One owner decides every terminal, exactly as ``converge_to_green``
        # routes through ``_convergence_terminal``: flag-off it is a pure
        # pass-through to ``plan_next_review_round``; flag-on it reads the
        # gate on the would-otherwise-be-stuck path so a green branch is
        # never demoted on the strength of an undirtied tree, and reports
        # how much verify-only grace the round consumed. Its flag-on gate
        # re-read is a full ruff+mypy+pytest sweep, so it is handed to
        # ``asyncio.to_thread`` rather than stalled on the event loop while
        # the implementation session may still be live — the same
        # convention ``_observe_baseline``'s callers already use.
        decision, grace_consumed = await asyncio.to_thread(
            engine._review_terminal,
            approved=approved,
            made_changes=made_changes,
            rounds_used=rounds_used,
            ceiling=ceiling,
            verify_grace_remaining=verify_grace_remaining,
        )
        verify_grace_remaining -= grace_consumed
        # A grace-bought continuation means the *next* round runs against a
        # branch this terminal just measured green — flag that round's prompt
        # to say so. Any other continuation (a committed fix) leaves the gate
        # state unknown for the next round, so the note is not carried.
        verify_round_ahead = grace_consumed == 1
        if decision == "green":
            return True
        if decision == "stuck":
            # No-progress stall or ceiling backstop.  The caller in
            # ``agent.implement_plan`` routes this ``False`` return to the
            # loud fail-loud terminal (ERROR + branch cleanup + demote to
            # ``failed``); no unapproved head proceeds.
            return False


def _find_uncalled_symbols(engine: EvolutionEngine, diff: str) -> list[str]:
    """Find new symbols in *diff* that have no call site off their def line.

    Parses the unified diff for added ``def <name>`` / ``class <name>``
    lines, determines the definition file from the diff header, then issues
    a *single* batched ``git grep -n`` (one ``-e <name>`` per symbol) to
    look for usages across non-test code.  The
    batched rows are attributed back to each symbol in-process by the same
    ``--fixed-strings`` substring test git would apply per symbol.  A
    reference anywhere in non-test code counts as a call site — including
    the symbol's own module — and only the symbol's own definition line is
    discounted.  Intra-module helpers (the normal shape of a decomposed
    feature) therefore pass.

    Returns a list of human-readable warning strings, one per symbol whose
    sole appearance is its own definition line.  Dunder methods (``__foo__``)
    and private test helpers (names starting with ``_test``) are
    skipped since they are invoked implicitly or only within the test
    suite by convention.  Test code and non-Python files are skipped
    (``counts_for_uncalled_scan``).
    """
    # 1. Parse the diff to collect (file, symbol) pairs.
    current_file: str | None = None
    symbols: list[tuple[str, str]] = []  # (file_path, symbol_name)

    for line in diff.splitlines():
        file_match = _DIFF_FILE_RE.match(line)
        if file_match:
            current_file = file_match.group(1)
            continue

        sym_match = _ADDED_SYMBOL_RE.match(line)
        if sym_match and current_file is not None:
            name = sym_match.group(1)
            # Skip dunder methods — they are invoked implicitly by
            # the runtime or protocols rather than by explicit call.
            if name.startswith("__") and name.endswith("__"):
                continue
            # Skip private test helpers (``_test_foo``).
            if name.startswith("_test"):
                continue
            if not counts_for_uncalled_scan(current_file):
                continue
            symbols.append((current_file, name))

    # 2. Batched: one ``git grep`` for all symbols, attribute rows per
    # symbol.  ``git grep`` accepts multiple ``-e <pat>`` flags in a single
    # invocation, so all symbols are matched in one fork rather than one
    # subprocess per symbol (#1856).
    workspace = str(engine._config.workspace)
    warnings: list[str] = []

    # Empty-symbol guard: ``git grep`` with no ``-e`` is an error / scans
    # nothing usefully, so skip the fork entirely.
    if not symbols:
        return warnings

    grep_args = ["git", "grep", "-n", "--fixed-strings"]
    for _file_path, name in symbols:
        grep_args += ["-e", name]
    grep_args += ["--", ".", *UNCALLED_GREP_EXCLUDES]

    try:
        proc = subprocess.run(
            grep_args,
            capture_output=True,
            text=True,
            timeout=engine._config.git_timeout,
            cwd=workspace,
        )
    except (subprocess.TimeoutExpired, OSError):
        # If the batched grep fails, skip *all* symbols rather than
        # blocking — the gate is advisory.  Note the blast radius so the
        # debug log makes clear every symbol was skipped, not just one.
        logger.debug(
            "_find_uncalled_symbols: batched git grep failed for %d "
            "symbol(s); skipping uncalled-symbol gate this round",
            len(symbols),
        )
        return warnings

    # Pre-split the batched output once into (path, content) rows so each
    # symbol's attribution is a cheap in-memory filter rather than a fork.
    rows: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        rows.append((parts[0], parts[2]))

    for file_path, name in symbols:
        # Filter to rows whose content contains this symbol as a substring
        # — the exact ``--fixed-strings -e name`` semantics a per-symbol
        # grep would apply, so behaviour is byte-for-byte identical (e.g.
        # ``foo`` still matches a line containing ``foobar``).  Reconstruct
        # a ``path:lineno:content`` blob (the lineno is irrelevant to the
        # helper) and reuse the existing call-site check; pre-filtering
        # keeps ``_symbol_has_call_site``'s single-symbol contract intact so
        # one symbol's rows cannot count as a call site for another.
        relevant = "\n".join(
            f"{path}:0:{content}" for path, content in rows if name in content
        )
        # A symbol is "called" iff git grep finds a reference on some line
        # that is not a declaration of it.  Intra-module call sites count;
        # every declaration row — including a same-named one in another
        # file — is discounted, bar an other-file method declaration the
        # new symbol implements.
        if not _symbol_has_call_site(relevant, name, file_path):
            warnings.append(
                f"{file_path}: symbol '{name}' has no non-test "
                f"call sites outside its own definition line"
            )

    return warnings

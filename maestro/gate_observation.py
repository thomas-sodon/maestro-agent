"""Local gate observation — turn a quality-gate runner's output into an enumerated fact.

The local quality gate is the instrument the implementation lineage uses to make
its most destructive decisions: demote an approved plan to ``failed``, delete its
branch, discard a working tree, rewind a committed head. Today that instrument
reports three booleans plus one opaque string — a front-truncated 1000-character
tail of a ``-x``-aborted run — so every downstream consumer substitutes a weaker
locally-derived proxy. The sharpest case is progress:
``QualityGateResult.failure_signature()`` is a frozenset drawn from exactly three
labels (``ruff`` / ``mypy`` / ``tests``), so when one gate is red that set can
never strictly shrink without going fully green, and a fix round that clears
eleven of twelve failing tests is indistinguishable from one that changed nothing.

This module is the missing fact, and the sole owner of the *local* gate-failure
vocabulary — the local-gate sibling of :mod:`maestro.ci_diagnosis`, which already
does exactly this for remote check logs. It defines the closed status vocabulary
(:data:`GateObservationKind`), the closed tool vocabulary (:data:`GateTool`), a
frozen :class:`GateFailure` per enumerated failure, a frozen
:class:`GateObservation` per gate, a frozen :class:`GateRunObservation` for the
whole run, the pure :func:`combined_output` that joins a runner's two captured
streams (the single spelling of that rule, shared with the gate report), the pure
:func:`parse_gate_output` that reads one runner's streams into an observation, the
pure :func:`parse_non_answer` that does the same for a gate that never answered
(salvaging the partial output a killed runner still wrote), and
the pure :func:`truncated_by_first_failure` that names whether a ``-x`` run stopped
short of its failing set. Layered over that fact it also owns the closed
attribution vocabulary (:data:`Attribution`) and the pure :func:`attribute` that
answers the one question the observation alone cannot: did *this diff* cause the
red, or did the branch inherit it from its own base? It names the non-answers
(``TIMEOUT`` / ``UNAVAILABLE``)
instead of stringifying them, and exposes :meth:`GateObservation.signature` at
*failure*-identity rather than gate-label granularity, so a twelve-to-one
improvement becomes representable as progress.

Pure functions, zero SDK cost, no I/O: the caller supplies the captured streams,
so this owner never runs a subprocess and never touches the filesystem. Mirrors
the :mod:`maestro.plan_grounding` / :mod:`maestro.frame_discharge` skeleton — a
``Literal`` closed vocabulary (nothing here crosses a JSONL boundary, so no
``StrEnum``), ``@dataclasses.dataclass(frozen=True)`` provenance-carrying results
as :mod:`maestro.ci_diagnosis` uses, module-level compiled regexes, and plain
module-level pure ``def``s.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Literal, NamedTuple, get_args

if TYPE_CHECKING:
    from maestro.gate_environment import GateRunnabilityVerdict

# ---------------------------------------------------------------------------
# The closed vocabularies
# ---------------------------------------------------------------------------

#: The two *non-answers* — the runner exceeded its budget, or could not be
#: executed at all (or, for pytest, ran but failed only on host-resource
#: exhaustion, reaching no verdict on the code) — which the pre-124 runner collapsed into the single literal
#: string ``"could not run"``. Spelled as its own alias, and *unioned into*
#: :data:`GateObservationKind` below so the two can never drift, because the
#: runner's "did this gate answer at all?" arm returns exactly these two: a
#: wider return type there would let a gate that never ran be constructed as
#: ``CLEAN`` — precisely what the fail-closed contract forbids — with no mypy
#: objection.
GateNonAnswer = Literal["TIMEOUT", "UNAVAILABLE"]

#: The closed per-gate status vocabulary. ``CLEAN`` is the only non-failing
#: member. ``FAILED`` is an ordinary red (ruff diagnostics, mypy errors, failing
#: tests); ``COLLECTION_ERROR`` is a pytest ``ERROR`` row — an import/collection
#: abort, or a fixture setup/teardown error, the outcomes where the *test body* is
#: not what reported the failure (pytest spells all of them the same way in its
#: summary, and this owner reads only that summary, so the two are one kind here);
#: ``COVERAGE`` is a pytest-cov
#: threshold shortfall, which reds the gate while emitting no failure line at all;
#: the remaining two are the :data:`GateNonAnswer` pair. Kept a ``Literal`` so a
#: new kind is a one-line vocabulary edit.
GateObservationKind = Literal[
    "CLEAN",
    "FAILED",
    "COLLECTION_ERROR",
    "COVERAGE",
] | GateNonAnswer

#: The :data:`GateNonAnswer` pair as a membership set, derived from the alias via
#: ``get_args`` so the two members stay spelled exactly once. Read by
#: :meth:`GateObservation.signature`, which keeps a non-answer's synthetic identity
#: even when its partial output enumerated some failures.
_NON_ANSWER_KINDS: frozenset[GateObservationKind] = frozenset(get_args(GateNonAnswer))

#: The kinds whose synthetic ``<tool>:<kind>`` member the *progress* vocabulary
#: always carries (:meth:`GateObservation.progress_signature`): the non-answers
#: plus ``COLLECTION_ERROR``, the outcomes where the gate did not judge the tests
#: it would otherwise have run. A truncated comparison falls back to counts, and a
#: round that newly carries one of these markers can shed rows only by hiding them,
#: so :func:`regression_markers` lets :func:`maestro.gate_convergence.plan_next_round`
#: read it as ``"stuck"`` rather than as a falling count (#4309).
_PROGRESS_MARKER_KINDS: frozenset[GateObservationKind] = _NON_ANSWER_KINDS | {
    "COLLECTION_ERROR",
}

#: The closed local-gate tool vocabulary, deliberately spelled with the *same*
#: three labels ``QualityGateResult.failure_signature()`` already uses (``tests``,
#: not ``pytest``), so :meth:`GateObservation.signature` is drop-in compatible
#: with :func:`maestro.gate_convergence.plan_next_round`'s ``frozenset[str]``
#: parameter shape when a later increment retargets it.
GateTool = Literal["ruff", "mypy", "tests"]

#: The closed attribution vocabulary — whose fault a red gate is. ``ATTRIBUTED``
#: is a failure the diff itself must answer for; ``INHERITED`` is one the branch's
#: own base was already carrying; ``UNATTRIBUTABLE`` is the honest "no evidence
#: either way", which is exactly the pre-124 posture every terminal already takes,
#: so a consumer that treats it as today's behaviour is never made worse.
#: A bare module-level ``Literal`` alias rather than a provenance-carrying frozen
#: dataclass, matching the two closest siblings
#: (:func:`maestro.remediation_tracker.classify_landing_attribution` returning
#: ``LandingAttribution``, and
#: :func:`maestro.ci_diagnosis.classify_ci_conclusions` returning ``CIStatus``):
#: every consumer that later wants the failing identities themselves already holds
#: the :class:`GateRunObservation` they are derivable from. A ``Literal`` and not a
#: ``StrEnum`` for the same reason as the vocabularies above — nothing here crosses
#: a JSONL boundary.
#: ``ENVIRONMENT`` (proposal 133) is the fourth answer, and the only one this
#: module does not derive itself: it is *sourced* from
#: :class:`~maestro.gate_environment.GateRunnabilityVerdict` — the oracle that
#: already measured, at zero SDK cost, whether the local gate can reach a verdict
#: at all. A red whose every failing identity that verdict names is the
#: environment's, not the diff's and not the base's, so no attempt may be charged
#: for it. It is never inferred from output text, which is exactly what keeps this
#: module from forking the runnability owner's judgement.
Attribution = Literal[
    "ATTRIBUTED", "INHERITED", "UNATTRIBUTABLE", "ENVIRONMENT",
]

# ---------------------------------------------------------------------------
# Bounds (context-bloat caps — this text reaches a prompt and a journal entry)
# ---------------------------------------------------------------------------

#: Hard cap on enumerated failures per gate. A catastrophically red suite can
#: report hundreds of failures; the observation is a decision input, not a
#: transcript, so the head is enough. Mirrors :data:`ci_diagnosis._MAX_ERROR_LINES`
#: in intent, sized larger because enumeration is the whole point of this owner —
#: a twelve-to-one improvement must stay visible as progress.
_MAX_FAILURES = 50

#: Public spelling of the cap above, for the one consumer that must build a
#: :class:`GateObservation` without going through :func:`parse_gate_output` —
#: ``EvolutionEngine._declined_gate_result``, which synthesizes the failing
#: result of a gate run the execution lease refused. A synthesized observation
#: is bounded by exactly the same number a parsed one is, so a declining verdict
#: naming thousands of identities cannot smuggle an unbounded tuple past the cap
#: this owner applies everywhere else.
MAX_FAILURES: int = _MAX_FAILURES

#: Hard per-message char cap. A single failure message (a long assertion repr, a
#: mypy type spelled out in full) can be huge, so each is truncated before it can
#: bloat whatever prompt or artifact it lands in. Mirrors
#: :data:`ci_diagnosis._MAX_LINE_CHARS`.
_MAX_MESSAGE_CHARS = 200

#: Hard cap on the retained gate output. Deliberately generous — twenty times the
#: 1000-character tail the pre-124 runner kept, so the whole failure section of a
#: realistic red run survives — but a cap nonetheless: maestro's own suite emits
#: hundreds of kilobytes, and CLAUDE.md's context-bloat invariant admits no
#: unbounded string on a path that reaches a prompt or an artifact. The *tail* is
#: what is kept, matching the pre-124 report's ``output[-1000:]``, and
#: :func:`parse_gate_output` enumerates :attr:`GateObservation.failures` from the
#: **full** text before truncating, so trimming never costs an enumerated failure.
_MAX_OUTPUT_CHARS = 20_000

# ---------------------------------------------------------------------------
# The three tools' real output formats (ruff emits two of them)
# ---------------------------------------------------------------------------

#: pytest's short-test-summary lines (default ``reportchars`` is ``fE``, so both
#: appear under the ``-q`` the gate runs). ``FAILED`` names a test that ran and
#: failed; ``ERROR`` names a collection/import abort (whose node is a bare *path*)
#: or a fixture setup/teardown error (whose node is a full nodeid) — the node
#: pattern accepts both, and :attr:`GateFailure.location` splits either back to its
#: file. Anchored at line start so the ``==== ERRORS ====`` section banner (which
#: starts with ``=``) and the run-summary line never match.
#:
#: The *section* scope :func:`_pytest_failures` applies — only lines between
#: pytest's ``short test summary info`` banner and the run-summary line that ends
#: it — is what keeps non-summary lines out; this pattern alone was never
#: sufficient. Under the ``--tb=short`` the gate runs, pytest prints a ``Captured
#: log call`` section per failing test, formatted
#: ``%(levelname)-8s %(name)s:%(filename)s:%(lineno)d`` — so an ``ERROR``-level
#: record satisfies the status word *and* the mandatory whitespace after it by
#: construction, as does any captured stdout line beginning with ``ERROR ``. The
#: whitespace still excludes pytest-cov's ``ERROR: Coverage failure:`` line, but
#: it excludes nothing about captured output.
#:
#: The tail is captured *whole* — ``.+``, not a node pattern plus an in-pattern
#: `` - `` alternative — and split by :func:`_split_pytest_node_and_message`. A
#: node pattern of ``\S+`` cannot match a string-parametrized id at all (it keeps
#: its spaces: ``test_x[a b]``), dropping the row from the enumeration entirely,
#: while a ``.+?`` node splitting at the *first* `` - `` cuts a parametrized id
#: that contains the separator (``test_x[a - b]``) in half. Splitting outside the
#: pattern is what lets the split be bracket-aware.
_PYTEST_SUMMARY_RE = re.compile(r"^(?P<status>FAILED|ERROR)\s+(?P<tail>.+)$")

#: pytest's summary-section banner, matched as a *substring* because pytest pads it
#: with ``=`` runs whose width tracks the terminal. Opening this section is what
#: makes a line eligible for :data:`_PYTEST_SUMMARY_RE`.
_PYTEST_SUMMARY_BANNER = "short test summary info"

#: The lines that close the summary section: pytest's run-summary line, the ``-x``
#: interrupt notice, and the collection-interrupt notice. Closing the section is
#: what keeps pytest-cov's trailing shortfall block — printed *after* the run
#: summary — from being read as more summary rows.
#:
#: The run summary is matched with its ``=`` padding *optional*, because the gate
#: runs pytest under ``-q``, which prints it undecorated (``4 failed, 118 passed
#: in 8.31s``); requiring the padding would leave the section open to EOF on every
#: real gate run. The outcome word is enumerated rather than left as ``.+`` so an
#: arbitrary digit-leading captured line cannot close the section early; the
#: singular spellings also cover their plurals, which they prefix.
_PYTEST_SUMMARY_END_RE = re.compile(
    r"^(?:=+\s*)?\d+ (?:failed|passed|error|skipped|deselected|xfailed|xpassed|warning)"
    r"|^(?:=+\s*)?no tests ran\b"
    r"|^!+\s*(?:stopping after|Interrupted)"
)

#: pytest's per-test captured-stream section headers (``--- Captured log call ---``,
#: ``--- Captured stdout call ---``). These close summary-row eligibility too, which
#: is what carries the section scope onto the *banner-less* fallback: a runner
#: killed before the ``short test summary info`` banner printed still emits captured
#: sections, and their ``ERROR``-padded log records are exactly the phantom
#: identities the scope exists to exclude. Only a captured header closes the
#: fallback — the ``FAILURES``/``ERRORS`` banners must not, since the salvaged rows
#: of a partial report sit under them.
_PYTEST_CAPTURED_SECTION_RE = re.compile(r"^-+\s*Captured\b")

#: The `` - `` joining a summary row's node to its message. Every occurrence is a
#: *candidate*: :func:`_split_pytest_node_and_message` ranks them by two
#: independent balance tests — a node half that closes every ``[`` it opened, and
#: a message half carrying no surplus ``]`` — preferring the candidate both agree
#: on. Spelled ``\s+-\s+`` rather than the literal ``" - "`` so a row whose runner
#: padded the separator is split identically.
_PYTEST_MESSAGE_SEP_RE = re.compile(r"\s+-\s+")

#: pytest-cov's threshold shortfall. This is the one red that emits *no* failure
#: line, so without an explicit alternative a coverage-only red would parse as an
#: empty failure set — the exact "unparsed red reads as green" hazard
#: :meth:`GateObservation.signature` exists to make impossible.
#: Nothing downstream reads the threshold or the achieved percentage — the whole
#: matched line becomes the row's ``message`` — so both halves are non-capturing.
_PYTEST_COVERAGE_RE = re.compile(
    r"^FAIL Required test coverage of [\d.]+% not reached\.?\s*.*$",
)

#: ruff's ``full`` diagnostic — the **default** output format since ruff 0.9, and
#: therefore what this gate actually gets, since ``run_quality_gates`` passes no
#: ``--output-format`` and ``pyproject.toml`` sets none. It spans *two* lines: a
#: header carrying the rule code, the optional ``[*]`` fixable marker and the
#: message, then an indented ``--> path:line:col`` location line (followed by a
#: source snippet and an optional ``help:`` line, neither of which matches
#: anything here). :func:`_ruff_enumeration` joins the pair into one failure.
#:
#: The code group is ruff's rule-code shape — letters then digits (``E501``,
#: ``RUF100``, ``PLR0913``) — which is what keeps the header pattern off the
#: snippet lines (``4 | x = 1``), the ``help:`` hint, and the ``Found N errors.`` /
#: ``[*] N fixable`` summaries. A ruff *syntax* error carries no rule code and so
#: does not match here; :data:`_RUFF_FULL_SYNTAX_HEADER_RE` is its header, and it
#: is enumerated the same way, with the literal :data:`_RUFF_SYNTAX_CODE` standing
#: in for the rule code.
_RUFF_FULL_HEADER_RE = re.compile(
    r"^(?P<code>[A-Z]+[0-9]+)(?: \[\*\])? (?P<message>.*)$",
)

#: The location half of the ``full`` shape, matched after ``str.strip()`` has
#: removed ruff's leading indent. Anchored at ``-->`` so a snippet line that
#: happens to contain an arrow (snippet lines are prefixed ``N | ``) cannot match.
_RUFF_FULL_LOCATION_RE = re.compile(
    r"^-->\s*(?P<path>[^\s:]+):(?P<line>\d+):(?P<col>\d+)$",
)

#: ruff's ``concise`` diagnostic — ``path:line:col: CODE message``, the pre-0.9
#: default. Still parsed, on one line, so an operator who sets
#: ``output-format = "concise"`` (or a pinned older ruff) is enumerated too.
_RUFF_CONCISE_RE = re.compile(
    r"^(?P<path>[^\s:]+):(?P<line>\d+):(?P<col>\d+): (?P<code>[A-Z]+[0-9]+)"
    r"(?: \[\*\])? (?P<message>.*)$",
)

#: The code token a ruff *syntax* diagnostic is enumerated under. ruff spells it
#: this way in both output formats, and reusing its own spelling — rather than
#: inventing one — keeps the identity legible against raw ruff output.
_RUFF_SYNTAX_CODE = "invalid-syntax"

#: A syntax diagnostic's ``full``-format header. Same two-line shape as a rule
#: diagnostic's (a ``--> path:line:col`` location line follows), so it feeds the
#: same *pending* join in :func:`_ruff_enumeration`. Anchored on the literal so the
#: snippet lines (``1 | def f(:``) and the ``Found N errors.`` summary cannot
#: match.
_RUFF_FULL_SYNTAX_HEADER_RE = re.compile(
    rf"^{_RUFF_SYNTAX_CODE}: (?P<message>.*)$",
)

#: A syntax diagnostic's ``concise``-format row — complete on one line, like
#: :data:`_RUFF_CONCISE_RE`, whose rule-code group it cannot match.
_RUFF_CONCISE_SYNTAX_RE = re.compile(
    rf"^(?P<path>[^\s:]+):(?P<line>\d+):(?P<col>\d+): {_RUFF_SYNTAX_CODE}: "
    r"(?P<message>.*)$",
)

#: mypy's error line: ``path:line[:col]: error: message  [code]``. The ``error:``
#: literal is what excludes interleaved ``note:`` lines and the ``Found N errors``
#: summary — the same shape :data:`maestro.red_root._MYPY_ERROR_LINE_RE` already
#: proved. The column is optional (mypy omits it without ``--show-column-numbers``,
#: which this gate does not pass) and is matched non-capturing because it cannot
#: sharpen the identifier the way ruff's does — see :func:`_mypy_enumeration`. The
#: trailing ``[code]`` bracket is optional (omitted under ``--hide-error-codes``);
#: the message group is non-greedy so the bracket, when present, is captured as the
#: code rather than swallowed into the message.
_MYPY_ERROR_RE = re.compile(
    r"^(?P<path>[^\s:]+):(?P<line>\d+)(?::\d+)?: error: "
    r"(?P<message>.*?)(?:\s+\[(?P<code>[A-Za-z0-9-]+)\])?$",
)

#: A ``line N`` reference inside a diagnostic's message (mypy's ``already defined
#: on line 12``). Blanked by :meth:`GateFailure.progress_key` so the progress
#: identity survives an edit that moves the referenced line.
_LINE_REFERENCE_RE = re.compile(r"\bline \d+\b")


# ---------------------------------------------------------------------------
# The frozen facts
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class GateFailure:
    """One enumerated failure from one local quality gate.

    :attr:`location` is the *bare file path* the failure sits in (``""`` when the
    failure names no file — a coverage shortfall), kept bare rather than
    ``path:line`` so a later increment can intersect it with a plan's changed
    paths without re-splitting it. :attr:`identifier` is the token that uniquely
    identifies this failure across the whole run: a pytest nodeid, an ``ERROR``
    row's node (a collection-abort path or a fixture-error nodeid), ruff's
    ``path:line:col:CODE``, mypy's
    ``path:line:CODE``, or the literal ``coverage``. It is *path-qualified* for
    every tool — a bare ``line:col:CODE`` would collide between two files
    reporting the same diagnostic on the same line, silently understating how
    many failures a red gate has. :attr:`message` is the bounded human-readable detail,
    truncated to :data:`_MAX_MESSAGE_CHARS`, and may be ``""`` when the runner
    printed none. :attr:`code` is the ruff rule / mypy error code on its own
    (``""`` for every other tool, and for mypy under ``--hide-error-codes``), the
    position-free half :meth:`progress_key` is built from.
    """

    tool: GateTool
    kind: GateObservationKind
    location: str
    identifier: str
    message: str
    code: str = ""

    def identity(self) -> str:
        """Return this failure's stable signature identity.

        ``"{tool}:{identifier}"``, falling back to the location and then the kind
        so the identity is never a bare tool name. Because :attr:`identifier` is
        path-qualified for every tool, the result is unique per failure across the
        whole run. This is the member :meth:`GateObservation.signature`
        contributes, and the granularity that makes clearing eleven of twelve
        failing tests representable as progress. Pure.
        """
        return f"{self.tool}:{self.identifier or self.location or self.kind}"

    def progress_key(self) -> str:
        """Return this failure's *position-independent* progress identity.

        :meth:`identity` locates a failure; this one survives an edit. A ruff or
        mypy identity embeds the line (and ruff's column), so a fix round that
        removes a line above a surviving diagnostic renames it and a round that
        cleared one error reads as "one gone, one new" — not progress (#4309). The
        progress key drops the position: ``"{tool}:{path}:{code}"``. The message is
        deliberately left out when a code is present, because ruff/mypy messages
        carry measurements (E501's ``(120 > 88)``, C901's complexity) that a partial
        fix changes; only when the code is empty (mypy ``--hide-error-codes``) is
        the message, whitespace-collapsed with any ``line N`` reference blanked,
        the key's discriminating half: ``"{tool}:{path}::{message}"``. Two
        diagnostics that share a key are counted, not collapsed, by
        :meth:`GateObservation.progress_signature`. A pytest / coverage row's
        :meth:`identity` is already position-free (a nodeid), so it is returned
        unchanged. Pure.
        """
        if self.tool not in ("ruff", "mypy"):
            return self.identity()
        if self.code:
            return f"{self.tool}:{self.location}:{self.code}"
        message = _LINE_REFERENCE_RE.sub("line N", " ".join(self.message.split()))
        return f"{self.tool}:{self.location}::{message}"


@dataclasses.dataclass(frozen=True)
class GateObservation:
    """One local quality gate's status and its enumerated failures.

    :attr:`kind` is the gate's status; :attr:`failures` enumerates what went
    wrong, and is empty both for a ``CLEAN`` gate and for a red the runner never
    produced parseable failures for (a non-answer, or output this owner does not
    recognise). :attr:`output` retains the gate's captured streams — the material
    the pre-124 runner threw away by keeping only a 1000-character tail — bounded
    by :func:`parse_gate_output` to the trailing :data:`_MAX_OUTPUT_CHARS`, which
    is twenty times that tail and cannot cost an enumerated failure because
    :attr:`failures` is parsed from the untruncated text.

    :attr:`truncated` is the honest "and there may be more": ``True`` when this
    gate's failure enumeration is known to be *incomplete*, so no reader may treat
    :attr:`failures` as the whole failing set. Two producers set it — the
    :data:`_MAX_FAILURES` cap here in :func:`parse_gate_output`, and
    ``evolution.EvolutionEngine._observe_gate`` when the ``-x``-less enumerate
    re-run it needed did not itself answer, leaving the ``-x``-truncated probe as
    the only reading. It is deliberately *not* folded into :meth:`signature`: a
    truncated set is still a set of real failures, and inventing a synthetic
    identity for it would make a fix round that cleared eleven of twelve read as
    no progress. :func:`attribute` is what reads it — an incomplete set can never
    justify ``INHERITED``, because the failures beyond the cut were never compared
    to the base at all. Its progress-path reader is the convergence loop
    (``EvolutionEngine._progress_counts`` feeding
    :func:`maestro.gate_convergence.plan_next_round`'s ``truncated``): two
    windows of a capped enumeration are not comparable as sets — a partial fix
    pulls unseen rows into the window, and xdist completion order reshuffles it —
    so a truncated comparison falls back to :attr:`enumerated_count`.

    :attr:`enumerated_count` is how many failures the parser *saw* before the
    :data:`_MAX_FAILURES` cap sliced :attr:`failures`; ``None`` (every
    hand-built observation) reads as ``len(failures)`` through
    :meth:`progress_count`.
    """

    tool: GateTool
    kind: GateObservationKind
    failures: tuple[GateFailure, ...]
    output: str
    truncated: bool = False
    enumerated_count: int | None = None

    def signature(self) -> frozenset[str]:
        """Return one identity per failure — empty iff this gate is ``CLEAN``.

        A non-``CLEAN`` gate whose :attr:`failures` is empty contributes one
        *synthetic* ``"{tool}:{kind}"`` identity (``"tests:TIMEOUT"``) rather than
        nothing, so a non-answer or an unrecognised red can never be mistaken for
        a clean run by a downstream progress or greenness reader.

        A **non-answer** keeps that synthetic identity *in addition to* whatever
        its partial output enumerated (:func:`parse_non_answer`). Dropping it
        would let a run that timed out with three salvaged rows read as a strictly
        smaller failure set than a completed run with four — progress, from a gate
        that never finished. The salvaged rows are still contributed, because they
        are real failures a fixer can act on; only the "and it did not answer"
        fact is what must not be droppable.

        Returns ``frozenset[str]``, matching
        :func:`maestro.gate_convergence.plan_next_round`'s parameter shape. Pure.
        """
        if self.kind == "CLEAN":
            return frozenset()
        identities = {failure.identity() for failure in self.failures}
        if not identities or self.kind in _NON_ANSWER_KINDS:
            identities.add(f"{self.tool}:{self.kind}")
        return frozenset(identities)

    def progress_signature(self) -> frozenset[str]:
        """Return the progress vocabulary — :meth:`signature` with stable members.

        Same ``CLEAN`` / synthetic non-answer rules as :meth:`signature`, but each
        member is a :meth:`GateFailure.progress_key` so a line shift cannot rename
        a surviving diagnostic. Because the key drops the position, two failures
        can share one; they are kept as a multiset by suffixing every repeat with
        its ordinal (``key#2``, ``key#3``), so fixing one of two identical errors
        still shrinks the set. Unlike :meth:`signature`, a ``COLLECTION_ERROR``
        gate also keeps its synthetic member alongside its rows (see
        :data:`_PROGRESS_MARKER_KINDS`). The locating :meth:`signature` is
        untouched — the attributor and the red-root rendering need the line. Pure.
        """
        if self.kind == "CLEAN":
            return frozenset()
        seen: dict[str, int] = {}
        members: set[str] = set()
        for failure in self.failures:
            key = failure.progress_key()
            seen[key] = seen.get(key, 0) + 1
            members.add(key if seen[key] == 1 else f"{key}#{seen[key]}")
        if not members or self.kind in _PROGRESS_MARKER_KINDS:
            members.add(f"{self.tool}:{self.kind}")
        return frozenset(members)

    def progress_count(self) -> int:
        """Return the full-enumeration failure count the truncated compare reads.

        ``0`` for a ``CLEAN`` gate. Otherwise :attr:`enumerated_count` (or
        ``len(failures)`` when unset), plus one for the synthetic member
        :meth:`progress_signature` adds. The count alone does not keep a
        non-answer from reading as progress — a killed run salvaging two rows
        counts lower than sixty answered failures — which is why a truncated
        comparison also consults :func:`regression_markers`. Pure.
        """
        if self.kind == "CLEAN":
            return 0
        count = (
            len(self.failures)
            if self.enumerated_count is None
            else self.enumerated_count
        )
        if not self.failures or self.kind in _PROGRESS_MARKER_KINDS:
            count += 1
        return count


@dataclasses.dataclass(frozen=True)
class GateRunObservation:
    """One whole quality-gate run — every gate's observation, per-gate.

    Kept a tuple of :class:`GateObservation` rather than a flattened failure list
    so the run-level fact retains *which gate* said what: a consumer can ask
    "is mypy still red?" without re-deriving it from failure locations.
    """

    gates: tuple[GateObservation, ...]

    def signature(self) -> frozenset[str]:
        """Return the union of every gate's :meth:`GateObservation.signature`.

        Empty iff every gate is ``CLEAN``, inheriting the per-gate guarantee. Pure.
        """
        identities: set[str] = set()
        for gate in self.gates:
            identities |= gate.signature()
        return frozenset(identities)

    def progress_signature(self) -> frozenset[str]:
        """Return the union of every gate's :meth:`GateObservation.progress_signature`.

        The convergence loop's and :func:`maestro.attempt_disposition.classify_attempt`'s
        progress vocabulary; members are tool-prefixed, so the union never merges
        two gates' keys. Pure.
        """
        members: set[str] = set()
        for gate in self.gates:
            members |= gate.progress_signature()
        return frozenset(members)

    def any_truncated(self) -> bool:
        """Whether any gate's enumeration is known incomplete. Pure."""
        return any(gate.truncated for gate in self.gates)

    def enumerated_count(self) -> int:
        """Return the sum of every gate's :meth:`GateObservation.progress_count`. Pure."""
        return sum(gate.progress_count() for gate in self.gates)

    def all_clean(self) -> bool:
        """Whether every observed gate is ``CLEAN``.

        A run with *no* gates is not clean: nothing was observed, so there is no
        evidence of greenness — the same fail-closed posture
        :meth:`GateObservation.signature` takes on an unparsed red. Pure.
        """
        return bool(self.gates) and all(gate.kind == "CLEAN" for gate in self.gates)


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------


def regression_markers(
    prev: frozenset[str], curr: frozenset[str],
) -> frozenset[str]:
    """Return the synthetic non-judgement markers *curr* carries and *prev* lacks.

    A member ``<tool>:<kind>`` with ``kind`` in :data:`_PROGRESS_MARKER_KINDS`
    (``TIMEOUT`` / ``UNAVAILABLE`` / ``COLLECTION_ERROR``) is the synthetic one
    :meth:`GateObservation.progress_signature` adds; a real failure key always
    carries a path or nodeid after the tool, so it never matches. Non-empty means
    the current round stopped judging something the previous one judged, which
    no falling count may read as progress (#4309). Pure.
    """
    markers = {
        f"{tool}:{kind}"
        for tool in get_args(GateTool)
        for kind in _PROGRESS_MARKER_KINDS
    }
    return frozenset((curr - prev) & markers)


def combined_output(stdout: str, stderr: str) -> str:
    """Join one gate runner's two captured streams into the text everything reads.

    Newline-joined, dropping an empty stream, so a runner that writes diagnostics
    to ``stderr`` (ruff on a syntax error) is parsed identically to one that
    writes to ``stdout``.

    Public because this owner is the *single* home of the join rule: both
    :attr:`GateObservation.output` and ``evolution.run_quality_gates``' red-gate
    report lines are slices of this one text (``raw_output`` keeps a
    1000-character tail; ``long_raw_output`` and the observation the trailing
    :data:`_MAX_OUTPUT_CHARS`), so the rule is
    spelled once and a change to it cannot leave one of the two behind. Pure.
    """
    return "\n".join(part for part in (stdout, stderr) if part)


def _pytest_failures(output: str) -> tuple[GateFailure, ...]:
    """Enumerate *every* pytest failure in *output*, unbounded.

    Three shapes, in the order they can appear: a ``FAILED <nodeid>`` summary line
    (a test that ran and failed), an ``ERROR <node>`` row (a collection abort or a
    fixture setup/teardown error), and
    pytest-cov's threshold shortfall — which emits no failure line, so it is
    matched explicitly. Walks the whole output — the caller slices to
    :data:`_MAX_FAILURES`, so the exhaustion reading sees every row. Pure.

    Summary rows are admitted only from inside pytest's own ``short test summary
    info`` section. The gate runs ``--tb=short``, which prints a ``Captured log
    call`` section per failing test, and pytest's capture format pads the level
    name — so a captured ``ERROR`` record satisfies
    :data:`_PYTEST_SUMMARY_RE` by construction and would otherwise be enumerated
    as a phantom ``COLLECTION_ERROR`` identity. Coverage shortfall lines stay
    eligible everywhere, because pytest-cov prints them *outside* the section.
    When no banner is present at all — the partial text of a runner killed at its
    timeout, which :func:`parse_non_answer` salvages through this same walk —
    lines start out eligible, so the salvage keeps its rows; a captured-stream
    section header (:data:`_PYTEST_CAPTURED_SECTION_RE`) still closes eligibility
    there, so the same phantom records are excluded from the salvage too.
    """
    lines = output.splitlines()
    in_summary = not any(_PYTEST_SUMMARY_BANNER in raw for raw in lines)
    failures: list[GateFailure] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            continue
        if _PYTEST_SUMMARY_BANNER in stripped:
            in_summary = True
            continue
        if in_summary and (
            _PYTEST_SUMMARY_END_RE.match(stripped)
            or _PYTEST_CAPTURED_SECTION_RE.match(stripped)
        ):
            in_summary = False
            continue
        failure = _pytest_failure_from_line(stripped, summary_eligible=in_summary)
        if failure is None:
            continue
        failures.append(failure)
    return tuple(failures)


#: Messages naming a *host resource* the run ran out of — descriptors
#: (``EMFILE``), disk (``ENOSPC``), memory — rather than a defect in the code.
#: Anchored to the message's leading exception type, so a genuine red that merely
#: *quotes* one (``DID NOT RAISE <class 'MemoryError'>``, an assertion on the text
#: ``No space left on device``) is not mistaken for exhaustion. In a container
#: the cgroup OOM killer SIGKILLs a pytest-xdist worker, so no ``MemoryError`` is
#: ever raised: the only trace is xdist's ``worker 'gwN' crashed while running``
#: report on the in-flight item, matched likewise at the message start (#4315).
#: An OOM-killed worker can instead leave only ``[gwN] node down: …`` followed
#: at once by that worker's ``[gwN] [ NN%] FAILED <nodeid>`` progress line (and
#: sometimes a loadscope ``KeyError: <WorkerController gwN>`` INTERNALERROR that
#: aborts before any summary); :func:`_xdist_worker_crashes` binds that shape.
#: xdist renders the nodeid with ``!r``, so it is double-quoted when it carries
#: an apostrophe.
_ENVIRONMENTAL_FAILURE_RE = re.compile(
    r"(?:[\w.]+\.)?\w*(?:Error|Exception): \[Errno (?:24|28)\]"
    r"|(?:[\w.]+\.)?MemoryError\b"
    r"|worker '[^']+' crashed while running ['\"]",
)

#: xdist's crash report as a whole line of the ``FAILURES`` section. Outside CI
#: (no ``CI``/``-vv``) pytest trims or drops a summary row's message to fit the
#: terminal width, so the crash text often never reaches the ``FAILED`` row; this
#: line always carries it in full, and binds to that row by nodeid (#4315).
_XDIST_CRASH_LINE_RE = re.compile(
    r"worker '[^']+' crashed while running (['\"])(?P<nodeid>.+)\1$",
)


#: pytest-timeout's kill evidence, matched against one *stripped* line, every
#: form anchored as the whole line: the thread method's ``+++… Timeout +++…``
#: banner, or the signal method's failure text — ``Failed: Timeout (>N.Ns) from
#: pytest-timeout.`` in current releases, ``Failed: Timeout >N.Ns`` in older — in the
#: two places pytest prints it — the ``E``-prefixed traceback line and the
#: ``FAILED <nodeid> - …`` short-summary row. Anchoring matters: a ``-v``
#: progress line for a test whose parametrized id *quotes* that text
#: (``…[E   Failed: Timeout >5.0s-True] PASSED``) must not read as a kill.
_PYTEST_TIMEOUT_SIGNAL_TEXT = (
    r"Failed: Timeout (?:>\d+(?:\.\d+)?s|\(>\d+(?:\.\d+)?s\) from pytest-timeout\.)"
)
_PYTEST_TIMEOUT_LINE_RE = re.compile(
    r"\++ Timeout \++"
    rf"|E\s+{_PYTEST_TIMEOUT_SIGNAL_TEXT}"
    rf"|FAILED \S+ - {_PYTEST_TIMEOUT_SIGNAL_TEXT}",
)


def is_pytest_timeout_line(line: str) -> bool:
    """Whether *line* is pytest-timeout reporting that it killed a test. Pure."""
    return _PYTEST_TIMEOUT_LINE_RE.fullmatch(line.strip()) is not None


#: xdist's report that a worker process went away, as a whole stripped line.
_XDIST_NODE_DOWN_RE = re.compile(r"\[(?P<worker>gw\d+)\] node down: .*")

#: An xdist ``-v`` progress line reporting a failed or errored test.
_XDIST_PROGRESS_RE = re.compile(
    r"\[(?P<worker>gw\d+)\] \[\s*\d+%\] (?P<status>FAILED|ERROR) (?P<nodeid>.+)",
)

#: The loadscope scheduler's INTERNALERROR when it is told about a worker it
#: already dropped — the trace a crashed worker leaves, not a defect.
_XDIST_CONTROLLER_KEYERROR_RE = re.compile(
    r"INTERNALERROR>.*KeyError: <WorkerController (?P<worker>gw\d+)>",
)


class _WorkerCrashes(NamedTuple):
    """What the xdist progress stream says about crashed workers. Private."""

    down_workers: frozenset[str]
    crashed_nodeids: frozenset[str]
    progress_failures: frozenset[str]
    internal_error_ok: bool


def _xdist_worker_crashes(output: str) -> _WorkerCrashes:
    """Read crashed-worker evidence from the xdist progress stream of *output*.

    One walk over non-blank stripped lines: a ``[gwN] node down:`` line arms
    that worker, and the *very next* line, when it is the same worker's
    ``FAILED`` progress line, binds its nodeid as crashed; any other line
    disarms. Every ``FAILED``/``ERROR`` progress nodeid is collected so a
    genuine failure the session never summarised still keeps the run red.
    ``internal_error_ok`` is true when no ``INTERNALERROR>`` line appears, or
    when a controller ``KeyError`` names a downed worker. Pure.
    """
    down: set[str] = set()
    crashed: set[str] = set()
    progress: set[str] = set()
    internal_lines: list[str] = []
    armed: str | None = None
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("INTERNALERROR>"):
            internal_lines.append(line)
        if (down_match := _XDIST_NODE_DOWN_RE.fullmatch(line)) is not None:
            armed = down_match.group("worker")
            down.add(armed)
            continue
        progress_match = _XDIST_PROGRESS_RE.fullmatch(line)
        if progress_match is not None:
            progress.add(progress_match.group("nodeid"))
            if armed == progress_match.group("worker") and progress_match.group("status") == "FAILED":
                crashed.add(progress_match.group("nodeid"))
        armed = None
    internal_ok = not internal_lines or any(
        (match := _XDIST_CONTROLLER_KEYERROR_RE.match(line)) is not None
        and match.group("worker") in down
        for line in internal_lines
    )
    return _WorkerCrashes(
        down_workers=frozenset(down),
        crashed_nodeids=frozenset(crashed),
        progress_failures=frozenset(progress),
        internal_error_ok=internal_ok,
    )


def _xdist_crashed_nodeids(output: str) -> frozenset[str]:
    """Return the nodeids xdist reported a worker crashing on, anywhere in *output*.

    Matched only as a whole stripped line, so an assertion or traceback that
    merely quotes the text (prefixed ``E``, embedded mid-line) contributes
    nothing. Pure.
    """
    return frozenset(
        match.group("nodeid")
        for raw in output.splitlines()
        if (match := _XDIST_CRASH_LINE_RE.match(raw.strip()))
    )


def _environmentally_exhausted(
    rows: Sequence[GateFailure],
    crashed: frozenset[str] = frozenset(),
    *,
    progress: frozenset[str] = frozenset(),
) -> bool:
    """Whether every failure in a red pytest run is host-resource exhaustion.

    *rows* is the *whole* enumeration (no :data:`_MAX_FAILURES` cap), so a
    genuine regression past the cap still keeps the run red. A coverage shortfall is
    ignored — it follows from the tests that could not run — but at least one
    exhausted failure is required, and a single other failure keeps the red
    (issue #4213). A row whose nodeid is in *crashed* — an xdist worker crash
    reported in full outside the (possibly trimmed) summary row — counts as
    exhausted too (#4315). *progress* holds the nodeids xdist's progress lines
    reported ``FAILED``/``ERROR``; one with no summary row (a session aborted
    before printing it) must itself be in *crashed*, and counts as the required
    exhausted failure when it is. Pure.
    """
    real = [f for f in rows if f.kind != "COVERAGE"]
    row_ids = {f.identifier for f in real}
    extra = progress - row_ids
    return (
        bool(real or extra)
        and extra <= crashed
        and all(
            _ENVIRONMENTAL_FAILURE_RE.match(f.message) or f.identifier in crashed
            for f in real
        )
    )


def _split_pytest_node_and_message(tail: str) -> tuple[str, str]:
    """Split a summary row's tail into its node half and its message half.

    pytest joins the two with `` - ``, but a string-parametrized nodeid keeps its
    spaces *and* may contain that very separator inside its ``[...]`` brackets —
    ``test_x[a - b] - <detail>`` — so splitting at the first `` - `` would cut the
    nodeid in half and mis-name both halves.

    Two independent balance tests each name the real separator, and each is blind
    on its own. The *node* test — the head closes every ``[`` it opened — misreads
    the shapes pytest renders with a literal unmatched bracket: a parameter value
    ``[`` (``test_x[[] - <detail>``, where no head ever balances, swallowing the
    whole message into the identity) and a parameter value carrying ``] - ``
    (``test_x[a] - left]``, where the head balances early and two distinct failures
    collapse onto one identifier). The *message* test — the message half carries no
    surplus ``]``, since every ``]`` of the parametrization sits on the node side —
    reads both of those correctly but misreads a parametrized id whose message
    carries a stray ``]`` (``test_x[a - b] - AssertionError: ']'``), where no
    message half is clean and the id is cut in half.

    So the candidates are ranked rather than filtered: the first both tests accept
    wins, then the first either one accepts, message test first — it is the one
    that reads pytest's literal-bracket shapes. A tail with no separator at all is
    all node and no message: exactly what pytest prints when the failure carries no
    one-line representation. When separators exist but neither test accepts any,
    the whole tail decides: a tail that is itself surplus-``]``-free is a
    message-less parametrized id (``test_x[a - b]``) and stays whole, while a
    surplus-``]`` tail is a *message* carrying its own stray ``]`` and splits at the
    first candidate rather than swallowing itself into the identity. Pure.
    """
    separators = tuple(_PYTEST_MESSAGE_SEP_RE.finditer(tail))
    ranked: list[list[re.Match[str]]] = [[], [], []]
    for separator in separators:
        head, message = tail[: separator.start()], tail[separator.end() :]
        node_ok = head.count("[") <= head.count("]")
        message_ok = message.count("]") <= message.count("[")
        if node_ok and message_ok:
            ranked[0].append(separator)
        elif message_ok:
            ranked[1].append(separator)
        elif node_ok:
            ranked[2].append(separator)
    for tier in ranked:
        if tier:
            chosen = tier[0]
            return tail[: chosen.start()], tail[chosen.end() :]
    if separators and tail.count("]") > tail.count("["):
        fallback = separators[0]
        return tail[: fallback.start()], tail[fallback.end() :]
    return tail, ""


def _pytest_failure_from_line(
    line: str, *, summary_eligible: bool,
) -> GateFailure | None:
    """Return the :class:`GateFailure` *line* carries, or ``None``.

    Split out of :func:`_pytest_failures` so the per-line shape decision reads as
    one expression per format rather than a nested loop body. *summary_eligible*
    is the caller's section verdict: a line outside pytest's ``short test summary
    info`` section is offered to the coverage matcher only, since that is the one
    shape pytest prints outside the section. Pure.
    """
    summary = _PYTEST_SUMMARY_RE.match(line) if summary_eligible else None
    if summary is not None:
        node, message = _split_pytest_node_and_message(summary.group("tail"))
        return GateFailure(
            tool="tests",
            # An ``ERROR`` row is pytest's "the test body did not report this"
            # outcome — a collection/import abort (no test ran at all) or a
            # fixture setup/teardown error (the fixture, not the assertion,
            # failed). Both are a different fact from a test that ran and failed,
            # and pytest's summary spells them identically, so both take the one
            # kind; both also stop a ``-x`` run, which is what
            # :data:`_ABORTING_KINDS` cares about.
            kind="COLLECTION_ERROR" if summary.group("status") == "ERROR" else "FAILED",
            location=node.split("::", 1)[0],
            identifier=node,
            message=message[:_MAX_MESSAGE_CHARS],
        )
    coverage = _PYTEST_COVERAGE_RE.match(line)
    if coverage is not None:
        return GateFailure(
            tool="tests",
            kind="COVERAGE",
            # A threshold shortfall is a property of the run, not of any one
            # file, so it names no location.
            location="",
            identifier="coverage",
            message=line[:_MAX_MESSAGE_CHARS],
        )
    return None


def _ruff_failure(path: str, line: str, col: str, code: str, message: str) -> GateFailure:
    """Build one ruff row from the five fields both output formats carry.

    Shared by the ``full`` and ``concise`` arms so the *identity* spelling —
    path-qualified ``path:line:col:CODE`` — is written once and the two formats
    cannot produce different identities for the same diagnostic. Pure.
    """
    return GateFailure(
        tool="ruff",
        kind="FAILED",
        location=path,
        identifier=f"{path}:{line}:{col}:{code}",
        message=message[:_MAX_MESSAGE_CHARS],
        code=code,
    )


def _ruff_failure_from_line(
    line: str,
    pending: tuple[str, str] | None,
) -> GateFailure | None:
    """Return the ruff failure *line* completes, or ``None``.

    *pending* is the ``(code, message)`` of an unconsumed ``full``-format header —
    a rule diagnostic's or a syntax diagnostic's, which share the shape — that
    this line's ``--> path:line:col`` half completes. Split out of
    :func:`_ruff_enumeration` so the per-line format decision reads as one expression
    per format, mirroring :func:`_pytest_failure_from_line`. Pure.
    """
    concise = _RUFF_CONCISE_RE.match(line)
    if concise is not None:
        return _ruff_failure(
            concise.group("path"), concise.group("line"), concise.group("col"),
            concise.group("code"), concise.group("message"),
        )
    concise_syntax = _RUFF_CONCISE_SYNTAX_RE.match(line)
    if concise_syntax is not None:
        return _ruff_failure(
            concise_syntax.group("path"), concise_syntax.group("line"),
            concise_syntax.group("col"), _RUFF_SYNTAX_CODE,
            concise_syntax.group("message"),
        )
    location = _RUFF_FULL_LOCATION_RE.match(line)
    if location is None or pending is None:
        return None
    code, message = pending
    return _ruff_failure(
        location.group("path"), location.group("line"), location.group("col"),
        code, message,
    )


def _ruff_header_from_line(line: str) -> tuple[str, str] | None:
    """Return the ``(code, message)`` *line* opens as a ``full``-format header.

    One expression per header shape — a rule diagnostic's, then a syntax
    diagnostic's, whose :data:`_RUFF_SYNTAX_CODE` stands in for the rule code it
    has none of — so :func:`_ruff_enumeration`'s loop reads the two the same way.
    ``None`` for every other line. Pure.
    """
    rule = _RUFF_FULL_HEADER_RE.match(line)
    if rule is not None:
        return rule.group("code"), rule.group("message")
    syntax = _RUFF_FULL_SYNTAX_HEADER_RE.match(line)
    if syntax is not None:
        return _RUFF_SYNTAX_CODE, syntax.group("message")
    return None


class _RuffEnumeration(NamedTuple):
    """What one walk of ruff's output found: its rows, and whether any was lost.

    *unjoined_header* records that a ``full``-format header and its
    ``--> path:line:col`` line were not joined — a recognised header (rule or
    syntax) never completed, or a location line arriving with no recognised
    header before it — so a diagnostic ruff reported is missing from *failures*. It is the input to
    :attr:`GateObservation.truncated`, whose documented meaning ("this enumeration
    is known incomplete") is exactly that.
    """

    failures: tuple[GateFailure, ...]
    unjoined_header: bool
    enumerated: int


def _ruff_enumeration(output: str) -> _RuffEnumeration:
    """Enumerate ruff's diagnostics from *output*, bounded, with completeness.

    Handles both output formats. In the ``full`` format ruff actually emits (see
    :data:`_RUFF_FULL_HEADER_RE`), a diagnostic spans two lines — the code and
    message, then ``--> path:line:col`` — so a matched header is held as *pending*
    and joined with the location line that follows it. A *syntax* diagnostic has
    the same two-line shape with :data:`_RUFF_SYNTAX_CODE` in place of the rule
    code (:data:`_RUFF_FULL_SYNTAX_HEADER_RE`), and rides the same join, so a red
    that is only a syntax error is enumerated with a real path-qualified identity
    instead of collapsing to the synthetic one — which is what lets the attributor
    see the newly broken file in a run that *also* carries ordinary diagnostics.
    A ``concise`` line of either shape is complete on its own. Everything else —
    the source snippet, the ``help:`` hint, the ``Found N errors.`` / ``[*] N
    fixable`` summaries — matches nothing and is skipped.

    A join that does not happen in either direction — a header left unjoined at
    the end of the walk or displaced by a second header, and a location line that
    arrives with no header to complete it — is a diagnostic this walk could not
    represent, and is reported as *unjoined_header* rather than dropped in
    silence. Keeps at most :data:`_MAX_FAILURES` rows but walks to the end, so
    *enumerated* counts every diagnostic it joined. Pure.
    """
    failures: list[GateFailure] = []
    pending: tuple[str, str] | None = None
    unjoined = False
    enumerated = 0
    for raw in output.splitlines():
        line = raw.strip()
        header = _ruff_header_from_line(line)
        if header is not None:
            # A header arriving while one is still pending means the first was
            # never completed — the same loss as one left pending at the end.
            unjoined = unjoined or pending is not None
            pending = header
            continue
        failure = _ruff_failure_from_line(line, pending)
        if failure is None:
            # The mirror of the displaced-header case: a location line reaching
            # here had no pending header to complete (one that did would have
            # returned a failure), so ruff reported a diagnostic under a header
            # shape this walk does not recognise. Same loss, opposite direction.
            unjoined = unjoined or _RUFF_FULL_LOCATION_RE.match(line) is not None
            continue
        pending = None
        enumerated += 1
        if len(failures) < _MAX_FAILURES:
            failures.append(failure)
    return _RuffEnumeration(
        tuple(failures), unjoined or pending is not None, enumerated,
    )


def _mypy_enumeration(output: str) -> tuple[tuple[GateFailure, ...], int]:
    """Enumerate mypy's errors from *output*, bounded, with the full count.

    One failure per ``error:`` line; interleaved ``note:`` lines and the
    ``Found N errors`` summary carry no ``error:`` marker and are skipped, the
    same exclusion :data:`maestro.red_root._MYPY_ERROR_LINE_RE` makes. Keeps at
    most :data:`_MAX_FAILURES` rows, but counts every ``error:`` line. Pure.
    """
    failures: list[GateFailure] = []
    enumerated = 0
    for raw in output.splitlines():
        match = _MYPY_ERROR_RE.match(raw.strip())
        if match is None:
            continue
        enumerated += 1
        if len(failures) >= _MAX_FAILURES:
            continue
        # The error code is mypy's stable name for the *class* of error, so it is
        # the identifying half; the line number distinguishes two of a kind in one
        # file. The code is absent under ``--hide-error-codes``, so it may be "".
        # There is no column to add: the gate passes no ``--show-column-numbers``,
        # so two same-code errors on one line share an identity. ``failures``
        # still carries both rows — only ``signature()``, a set, collapses them.
        code = match.group("code") or ""
        located = f"{match.group('path')}:{match.group('line')}"
        failures.append(
            GateFailure(
                tool="mypy",
                kind="FAILED",
                location=match.group("path"),
                identifier=f"{located}:{code}" if code else located,
                message=match.group("message")[:_MAX_MESSAGE_CHARS],
                code=code,
            ),
        )
    return tuple(failures), enumerated


#: Worst-first precedence for deriving a red gate's status from its failures. A
#: run that both failed tests *and* missed the coverage threshold is ``FAILED``,
#: because the failing tests are the fact worth fixing. Spelled as a typed tuple
#: so the vocabulary is named once and the members stay narrowed to the alias.
_RED_KIND_PRECEDENCE: tuple[GateObservationKind, ...] = (
    "FAILED",
    "COLLECTION_ERROR",
    "COVERAGE",
)


def _red_kind(failures: tuple[GateFailure, ...]) -> GateObservationKind:
    """Derive a red gate's status from the failures parsed out of it.

    Walks :data:`_RED_KIND_PRECEDENCE` and returns the worst kind present. A red
    whose output produced no parseable failure is ``FAILED`` — honest about being
    red, and :meth:`GateObservation.signature` still gives it a non-empty
    synthetic identity so it cannot read as green. Pure.
    """
    kinds = {failure.kind for failure in failures}
    for candidate in _RED_KIND_PRECEDENCE:
        if candidate in kinds:
            return candidate
    return "FAILED"


#: The failure kinds a ``pytest -x`` run *stops* at. Both mean the run was cut
#: short at the first occurrence, so its output cannot enumerate the rest of the
#: failing set. A ``COVERAGE`` shortfall is deliberately absent: that red comes
#: from a run that *completed* with every test passing, so its own output already
#: carries the whole fact and re-running the suite would only reproduce it.
_ABORTING_KINDS: frozenset[GateObservationKind] = frozenset(
    {"FAILED", "COLLECTION_ERROR"},
)


def truncated_by_first_failure(observation: GateObservation) -> bool:
    """Whether *observation* came from a run ``-x`` cut short of the failing set.

    The predicate a caller that ran ``pytest -x`` uses to decide whether a second,
    ``-x``-less run would tell it anything new: true when the observation carries a
    failure of an :data:`_ABORTING_KINDS` kind. False for a coverage-only red, for
    a non-answer, and for an unparsed red — in each of those the re-run would burn
    a whole suite to reproduce output already in hand. Pure.
    """
    return any(failure.kind in _ABORTING_KINDS for failure in observation.failures)


def parse_gate_output(
    tool: GateTool,
    stdout: str,
    stderr: str,
    returncode: int,
) -> GateObservation:
    """Parse one completed gate run into a :class:`GateObservation`.

    A zero *returncode* is ``CLEAN`` with no failures, regardless of what the
    streams contain — a green run's warnings are not failures. A non-zero
    *returncode* is enumerated with the parser for *tool* and its status derived
    from what was found (:func:`_red_kind`) — except a pytest red whose every
    failure is host-resource exhaustion (:func:`_environmentally_exhausted`), which
    is the ``UNAVAILABLE`` non-answer with its rows kept; a crashed xdist worker's
    test (its ``[gwN] [ NN%] FAILED`` line straight after ``[gwN] node down:``)
    counts as such exhaustion, summary or not. The combined output is retained on the
    observation either way, bounded to the trailing :data:`_MAX_OUTPUT_CHARS` — the
    failures are enumerated from the *untruncated* text, so the bound never costs a
    row. An enumeration that stopped at :data:`_MAX_FAILURES` — or, for ruff, one
    that could not join a diagnostic's header and location line — sets
    :attr:`GateObservation.truncated`, which is how a row set announces that it is
    not the whole failing set.

    Pure: takes the already-captured streams, runs no subprocess, touches no
    filesystem. The two *non-answers* — a runner that timed out or could not be
    executed — do not come through this door (the exhausted-pytest ``UNAVAILABLE``
    above is the one non-answer read from a finished run's returncode), because a returncode is exactly what
    they lack; :func:`parse_non_answer` is theirs, and reuses this enumeration on
    whatever partial output the killed runner had already written.
    """
    output = combined_output(stdout, stderr)
    if returncode == 0:
        return GateObservation(
            tool=tool, kind="CLEAN", failures=(), output=output[-_MAX_OUTPUT_CHARS:],
        )
    # ruff is the one parser that can *know* it lost a row it recognised — a
    # ``full``-format header with no location line after it — so its walk is read
    # through the richer door, and that fact joins the cap below.
    exhausted = False
    if tool == "ruff":
        failures, unjoined_header, enumerated = _ruff_enumeration(output)
    elif tool == "tests":
        # One unbounded walk, sliced to the cap, so the exhaustion reading and
        # the kept rows can never diverge. A run whose every failure is
        # host-resource exhaustion (EMFILE past a container's ``nofile``,
        # ENOSPC, OOM — which arrives as an xdist worker crash, not a
        # ``MemoryError``, read from its full ``FAILURES`` line since the summary
        # row's message may be trimmed) reached no verdict on the code: it is the
        # ``UNAVAILABLE`` non-answer, rows kept, not a red to clear (#4213, #4315).
        every_row = _pytest_failures(output)
        failures = every_row[:_MAX_FAILURES]
        enumerated = len(every_row)
        crashes = _xdist_worker_crashes(output)
        # The INTERNALERROR veto guards only the progress-bound crash evidence:
        # a shape that reads exhausted without it (ENOSPC/EMFILE/MemoryError
        # rows, the #4315 full crash line) keeps its pre-existing verdict.
        legacy_crashed = _xdist_crashed_nodeids(output)
        exhausted = _environmentally_exhausted(every_row, legacy_crashed) or (
            crashes.internal_error_ok
            and _environmentally_exhausted(
                every_row,
                legacy_crashed | crashes.crashed_nodeids,
                progress=crashes.progress_failures,
            )
        )
        unjoined_header = False
    else:
        failures, enumerated = _mypy_enumeration(output)
        unjoined_header = False
    return GateObservation(
        tool=tool,
        kind="UNAVAILABLE" if exhausted else _red_kind(failures),
        failures=failures,
        output=output[-_MAX_OUTPUT_CHARS:],
        # A parser that stopped exactly *at* the cap may have had more to read,
        # so the row set is known-incomplete. Stamped rather than inferred by the
        # reader, because ``_MAX_FAILURES`` is this owner's private bound. A ruff
        # header left unjoined is the same known-incompleteness, differently
        # caused.
        truncated=len(failures) >= _MAX_FAILURES or unjoined_header,
        enumerated_count=enumerated,
    )


def parse_non_answer(
    tool: GateTool,
    kind: GateNonAnswer,
    stdout: str,
    stderr: str,
) -> GateObservation:
    """Build the observation for a gate that *never answered*, keeping its output.

    A runner killed at its timeout has usually already written some of what it
    had to say — ``subprocess.TimeoutExpired`` carries whatever was read before
    the kill — and on a slow suite that partial text may already name real
    failures. Discarding it leaves a fixer with the kind and nothing else, which
    is the pre-124 ``"could not run"`` string wearing a better name.

    So the partial streams are enumerated exactly as a finished red run's are
    (same parsers, same :data:`_MAX_FAILURES` / :data:`_MAX_OUTPUT_CHARS`
    bounds), and only the *kind* is overridden to the non-answer: the gate did
    not answer, whatever it managed to print, and no salvaged row may relabel it
    as an ordinary red. :meth:`GateObservation.signature` keeps the synthetic
    ``"{tool}:{kind}"`` identity alongside the salvaged ones for the same reason.

    An ``UNAVAILABLE`` runner that never started has no output at all, so this is
    in practice the ``TIMEOUT`` path (a pytest run that finished but only on
    host-resource exhaustion is labelled ``UNAVAILABLE`` by
    :func:`parse_gate_output` itself, not here); it is written for both because the caller
    should not have to know which non-answer carries partial streams. Pure — the
    caller supplies the already-decoded text.
    """
    salvaged = parse_gate_output(tool, stdout, stderr, returncode=1)
    return dataclasses.replace(salvaged, kind=kind)


# ---------------------------------------------------------------------------
# The attributor
# ---------------------------------------------------------------------------


def _normalised_paths(paths: Iterable[str]) -> frozenset[str]:
    """Return *paths* in the one spelling both sides of the comparison can share.

    Whitespace-stripped, backslash separators folded to ``/``, and a single
    leading ``./`` dropped, so a runner's repo-relative ``./maestro/x.py`` and a
    plan's ``files_affected`` entry ``maestro/x.py`` (which is only ever
    ``.strip()``ed where it is written) compare equal. Empty results are dropped,
    which is what keeps a failure that names *no* file — a coverage shortfall,
    whose :attr:`GateFailure.location` is ``""`` — from matching anything.

    Exact normalised-path equality is the whole comparison: deliberately **no**
    basename fallback of the kind :func:`maestro.planner._referenced_canonical_files`
    uses for prose (even unique-only), because two same-named files in different directories must not
    cross-match. A false ``ATTRIBUTED`` blames the diff for a failure it did not
    cause, which is the destructive direction. Pure.
    """
    normalised: set[str] = set()
    for path in paths:
        cleaned = path.strip().replace("\\", "/")
        if cleaned.startswith("./"):
            cleaned = cleaned[2:]
        if cleaned:
            normalised.add(cleaned)
    return frozenset(normalised)


def _environment_owns(
    observation: GateRunObservation,
    runnability: GateRunnabilityVerdict | None,
) -> bool:
    """Answer whether *runnability* claims every failing identity in *observation*.

    The sourced half of :func:`attribute` — the only place this module consults
    :mod:`maestro.gate_environment`, and it reads the verdict's own fields rather
    than re-deriving the fact from output text.

    Two scopes, and the difference is the whole point of the helper. A verdict
    carrying :attr:`~maestro.gate_environment.GateRunnabilityVerdict.identities`
    (``UNCOLLECTABLE`` is the only kind that does today) is a claim about *those*
    gate identities, so it owns the red only when it names every one of them —
    a collection abort alongside a genuine regression the diff introduced is not
    the environment's to excuse. A verdict with **empty** identities
    (``UNPROVISIONED``, ``UNFIT``) is not a narrower claim but a **wider** one:
    a sibling distribution that will not import, or a suite measurably unable to
    finish inside its deadline, says the run as a whole reached no verdict, so
    every identity it produced is the environment's. Reading empty as "names
    nothing" — which a bare ``signature() <= identities`` subset test does — makes
    the arm dead for exactly the two kinds the proposal names as motivating.

    ``SOUND`` and ``None`` (the flag-off answer, and what every non-convergence
    caller passes) are both ``False``, so the arm cannot fire off the flag. Pure.
    """
    if runnability is None or runnability.kind == "SOUND":
        return False
    if not runnability.identities:
        return True
    return observation.signature() <= runnability.identities


def attribute(
    observation: GateRunObservation,
    changed_paths: Sequence[str],
    baseline: GateRunObservation | None,
    *,
    runnability: GateRunnabilityVerdict | None = None,
) -> Attribution:
    """Classify a red *observation* against the diff and the branch's base.

    The question every destructive terminal needs answered and cannot ask today:
    *did this diff cause the failure, or did the branch inherit it from its base?*
    An inherited breakage — an uninstalled dependency, a collection abort in a file
    the diff never touched, a base that was already red when the branch was cut —
    is today indistinguishable from a genuine regression, so every terminal treats
    it as the plan's fault and spends a human approval, an attempt, or a working
    tree on it.

    *changed_paths* is the diff's file list. The two convergence/landing readers
    (:meth:`~maestro.evolution.EvolutionEngine._attribute_review` and
    :meth:`~maestro.agent.MaestroAgent._attribute_owned_gate_red`) derive it from
    the base-to-HEAD :meth:`~maestro.vcs.VCS.changed_paths`, never from declared
    plan metadata, which can under- or over-declare what a branch actually
    touched; the Tend post-merge arm still passes a plan's ``files_affected``
    and carries that weakness;
    *baseline* is a :func:`~maestro.evolution.EvolutionEngine._observe_baseline`
    run of the branch's own base commit, or ``None`` when none was captured. The
    decision, in strict precedence order:

    * a clean run — an :meth:`GateRunObservation.all_clean` observation — is
      ``UNATTRIBUTABLE``; there is nothing to attribute;
    * ``ENVIRONMENT`` (proposal 133) when *runnability* — the
      :mod:`maestro.gate_environment` verdict, passed by the convergence reader
      and by nobody else — owns the red per :func:`_environment_owns`: non-
      ``SOUND``, and either naming every identity in
      :meth:`GateRunObservation.signature` (enumerated and synthetic alike) or
      carrying no identities at all, which is the *whole-run* claim
      ``UNPROVISIONED`` and ``UNFIT`` make. This outranks ``ATTRIBUTED``: a gate
      that measurably cannot reach a verdict has not judged the diff at all, so a
      failing location inside *changed_paths* is no evidence against it. It also
      sits **above** the "enumerated nothing" arm below, because a bare non-answer
      — a lone ``tests:TIMEOUT`` with zero enumerated rows — is precisely the
      shape an ``UNFIT`` environment produces, and letting ``UNATTRIBUTABLE``
      claim it first would make the arm unreachable for that class. The fact is
      *sourced* from the verdict, never re-derived from output text; a
      ``runnability`` of ``None`` — the default, and what every non-convergence
      caller passes — can never satisfy it, so those call sites are byte-identical
      to their pre-133 behaviour;
    * whatever remains that enumerated no failures at all (a bare non-answer, or a
      red this owner's parsers did not recognise) is ``UNATTRIBUTABLE``;
    * ``ATTRIBUTED`` when any enumerated :attr:`GateFailure.location` is in the
      normalised *changed_paths*. This **wins over** ``INHERITED``: a failure in a
      file the diff touched is the diff's to answer for even when the base was
      also red there. An empty *changed_paths* can never reach this arm;
    * ``INHERITED`` only when the observation's failing set is *complete* and
      *every* enumerated :meth:`GateFailure.identity` already appears in
      ``baseline.signature()``. Three things can withhold it. A *baseline* of
      ``None`` skips the arm entirely. A **synthetic identity** — one
      :meth:`GateObservation.signature` added for a non-answer or an unparsed red,
      computed here as the run signature minus the enumerated identities — forces
      ``UNATTRIBUTABLE``, because a gate that timed out or printed output the
      parser could not read proves nothing about the diff; that is the fail-safe
      direction for parse drift. And a :attr:`GateObservation.truncated` gate does
      the same, because a set the producer already knows is short cannot be shown
      to be wholly inherited: the failures past the ``-x`` cut or the
      :data:`_MAX_FAILURES` cap were never compared to the base at all, and
      calling that ``INHERITED`` is exactly the false excuse this owner exists to
      refuse. Those last two read the *observation* side only. The **baseline**
      side is deliberately unguarded: a baseline that timed out or
      capped has a *smaller* signature, which makes the subset test harder, never
      easier, so its rows are honest evidence that the base already carried those
      identities and are read as such;
    * ``UNATTRIBUTABLE`` otherwise. One inherited failure alongside one the
      baseline does not confirm is ``UNATTRIBUTABLE``, not ``INHERITED`` —
      conservative by construction, since ``UNATTRIBUTABLE`` is exactly the
      pre-124 behaviour and ``INHERITED`` is the verdict that will later hold a
      destructive arm off.

    Pure: no VCS, no filesystem, no config — the two observations and the path
    list are the whole input.
    """
    if observation.all_clean():
        return "UNATTRIBUTABLE"
    if _environment_owns(observation, runnability):
        return "ENVIRONMENT"
    enumerated = tuple(
        failure for gate in observation.gates for failure in gate.failures
    )
    if not enumerated:
        return "UNATTRIBUTABLE"

    changed = _normalised_paths(changed_paths)
    if changed & _normalised_paths(failure.location for failure in enumerated):
        return "ATTRIBUTED"

    if baseline is None:
        return "UNATTRIBUTABLE"
    if any(gate.truncated for gate in observation.gates):
        return "UNATTRIBUTABLE"
    identities = {failure.identity() for failure in enumerated}
    if observation.signature() - identities:
        return "UNATTRIBUTABLE"
    if identities <= baseline.signature():
        return "INHERITED"
    return "UNATTRIBUTABLE"


def render_identities(
    observation: GateRunObservation,
    *,
    limit: int,
    max_chars: int,
) -> str:
    """Render *observation*'s failure identities as one bounded, sorted string.

    The proposal-124 inherited-hold arm names the base failures that held a plan
    in two places — the base-repair ``next_task_directive`` and the eventual
    terminal-demote ``failure_reason`` — and both are slices of this one string,
    so the rendering rule (:meth:`GateRunObservation.signature` sorted for a
    deterministic order, capped at *limit* identities, then the join capped at
    *max_chars* characters) lives with the vocabulary owner rather than being
    spelled twice.  An all-``CLEAN`` observation — an empty signature — renders
    the empty string.  Pure, zero-I/O — matching the module's bounded-output
    posture.
    """
    identities = sorted(observation.signature())
    if not identities:
        return ""
    return ", ".join(identities[:limit])[:max_chars]


def render_failure_report(
    observation: GateRunObservation,
    *,
    limit: int,
    max_chars: int,
) -> str:
    """Render *observation*'s failures as a bounded ``## Quality gate failures`` block.

    The multi-line sibling of :func:`render_identities`, for the one caller that
    wants the *whole* picture rather than a one-line join: the auto-fix prompt
    (proposal 124). It follows :func:`maestro.ci_diagnosis.render_ci_failure_section`'s
    shape exactly — a ``## Quality gate failures`` heading, one ``- {tool}
    ({kind}):`` bullet per non-``CLEAN`` gate, and one ``  - {identity} —
    {message}`` sub-bullet per enumerated :class:`GateFailure` — so the fixer reads
    an enumerated fact instead of the front-truncated ``raw_output`` tail.

    A red gate that enumerated *nothing* (a :data:`GateNonAnswer`, or a
    :data:`~maestro.gate_observation.GateObservationKind` such as ``COVERAGE`` that
    reds while printing no failure line) gets the explicit ``  - (no failure line
    parsed — kind is {kind})`` placeholder rather than silence, so a
    ``TIMEOUT`` or coverage-only red is visibly not-green. A gate whose failures
    are *not fully shown* gets a trailing ``  - (enumeration incomplete — more
    failures may exist)`` line — whether the runner stopped short
    (:attr:`GateObservation.truncated`) or the run-wide *limit* was spent before
    this gate's failures rendered, so an earlier gate exhausting the row cap can
    never leave a later red gate as a bare header that reads clean. An
    :meth:`GateRunObservation.all_clean` observation returns ``""``, matching this
    module's append-unconditionally convention and
    :func:`render_ci_failure_section`'s empty-input contract.

    Bounds are caller-supplied and keyword-only, exactly as :func:`render_identities`:
    *limit* caps the total enumerated failure sub-bullets across the whole run
    (the structural heading, gate bullets, and the placeholder/incomplete lines
    above are never counted against it, so a red gate is never silently rendered
    green), and *max_chars* caps the joined block — a block over the cap keeps its
    head (the heading and earliest failures) and drops the tail with a trailing
    ``[truncated]`` marker so the drop is visible rather than a silently amputated
    report (unlike :func:`render_identities`' one-line, marker-less join, whose
    dashboard/Telegram consumers would read a marker as noise). Pure, zero-I/O.
    """
    if observation.all_clean():
        return ""
    lines = ["## Quality gate failures"]
    rows_left = limit
    for gate in observation.gates:
        if gate.kind == "CLEAN":
            continue
        lines.append(f"- {gate.tool} ({gate.kind}):")
        rendered = 0
        if gate.failures:
            for failure in gate.failures:
                if rows_left <= 0:
                    break
                lines.append(f"  - {failure.identity()} — {failure.message}")
                rows_left -= 1
                rendered += 1
        else:
            lines.append(f"  - (no failure line parsed — kind is {gate.kind})")
        # Mark the gate incomplete when its failures are not fully shown — the
        # runner stopped short (``truncated``) *or* the run-wide row cap was spent
        # before this gate's failures rendered. Without the second arm an earlier
        # gate that exhausts ``limit`` leaves a later red gate as a bare
        # ``- {tool} ({kind}):`` header with no sub-bullets, reading as if clean.
        if gate.truncated or rendered < len(gate.failures):
            lines.append(
                "  - (enumeration incomplete — more failures may exist)"
            )
    joined = "\n".join(lines)
    if len(joined) <= max_chars:
        return joined
    # Keep the head (heading + earliest failures) and drop the tail, leaving a
    # marker so the cut is visible to the fixer rather than a silently amputated
    # block. The result still satisfies ``len(result) <= max_chars``.
    marker = "\n[truncated]"
    if max_chars <= len(marker):
        return joined[:max_chars]
    return joined[: max_chars - len(marker)] + marker

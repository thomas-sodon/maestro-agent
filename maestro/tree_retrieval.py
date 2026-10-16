"""Confined, read-only source-tree reads for grounded planning (proposal 143).

The evolution planner states facts about the source tree — ``files_affected``,
symbol names, "this does not exist yet" claims, and a grounding probe's
``locator`` / ``expectation`` — while running tool-free, so every such claim is
recalled or guessed. Closing that class means eventually handing the planner a
bounded, structurally read-only view of the tree. This module is the single
owner of *confined* tree reads, in two tiers:

* the **door reader** (:func:`is_confined_locator`, :func:`read_door_text`,
  :func:`read_door_corpus`) keeps the submission-door grounding probe's
  confinement exactly — an absolute locator or one carrying a ``..`` part is
  refused unread — so ``EvolutionEngine._read_grounding_tree_text`` routes
  through it with byte-identical results;
* the stricter **tool reader** (:class:`TreeView`, :func:`resolve_tool_path`)
  admits only git-tracked files under the closed :data:`TOOL_READ_ALLOWED_DIRS`
  allowlist (plus root-level ``*.md`` / ``maestro*.toml``), refuses anything
  matching :data:`TOOL_READ_DENIED_PARTS` as a second layer, and refuses a
  symlink whose target resolves out of the tree.

Four pure tool functions sit on the tool reader — :func:`read_source`,
:func:`grep_source`, :func:`find_definition`, :func:`list_cli_surface` — each
returning tool-result text, with a refusal rendered as a short explanatory string
rather than an exception. A fifth tool, ``read_reality_inventory``, renders the
reality inventory through a callable the caller injects (``reality_inventory``
imports this module, so this leaf never imports it back).
:func:`build_bounded_tree_tool_set` wraps them as one
bounded, read-only in-process MCP server (:data:`TREE_SERVER_NAME`) under a
shared call budget and a per-result cap, recording what each call read in a
:class:`TreeReadReceipt`; it is built per call and deliberately kept out of
``cli._build_agent_tools``. The planner callers (``propose_plan`` first)
carry it behind ``[loop] grounded_planning_enabled``. The
pure CLI-surface AST walker (:func:`shipped_cli_surface`) lives here too, so the
door's shipped-CLI guard and :func:`list_cli_surface` share one parser.

Reads text only: no SDK import, no ``subprocess``, no write API
(:mod:`maestro.inproc_mcp` depends only on ``mcp`` and is imported only inside
:func:`build_bounded_tree_tool_set`, so the door reader stays import-light). The
tracked-file
set is supplied by the caller, never derived here.
"""

from __future__ import annotations

import ast
import re
import re._constants as sre_constants
import re._parser as sre_parser
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from maestro.plan_grounding import _has_definition

if TYPE_CHECKING:
    from maestro.inproc_mcp import Handler
    from maestro.providers.base import ToolRegistry

#: Top-level directories the tool reader admits (a tracked file must sit under one).
TOOL_READ_ALLOWED_DIRS: tuple[str, ...] = (
    "maestro",
    "maestro_review",
    "maestro_sdk",
    "tests",
    "docs",
    "scripts",
)

#: Path-part patterns the tool reader refuses even when tracked and allowlisted —
#: a second layer so a stray tracked secret or cache file is still refused.
TOOL_READ_DENIED_PARTS: tuple[str, ...] = (
    "data",
    ".git",
    ".env*",
    "__pycache__",
    ".mypy_cache",
    ".coverage",
    "*.egg-info",
)

#: Default and maximum number of lines one :func:`read_source` call returns.
READ_SOURCE_MAX_LINES = 400
#: Longest pattern :func:`grep_source` admits in regex mode.
GREP_MAX_PATTERN_CHARS = 200
#: Byte budget one :func:`grep_source` scan reads before truncating.
GREP_MAX_BYTES_SCANNED = 4_000_000
#: Match budget one :func:`grep_source` / :func:`find_definition` call returns.
GREP_MAX_MATCHES = 100
#: Longest line prefix a regex-mode :func:`grep_source` searches: an admitted
#: pattern costs O(len(pattern) * len(line)**2), so one long tracked line must
#: not stall the loop.
GREP_MAX_REGEX_LINE_CHARS = 1000
#: Estimated regex work (choice paths x pattern chars x searched line chars
#: squared, summed over lines) one regex-mode :func:`grep_source` call spends before truncating.
GREP_MAX_REGEX_WORK = 1_000_000_000

_CLI_SOURCE_PATH = "maestro/cli.py"

#: The in-process MCP server name the bounded tree surface is served under.
TREE_SERVER_NAME = "tree"

#: The closed allowlist of tools the bounded tree surface exposes. Inclusion by
#: allowlist: a tool not named here is never served. ``search_rejections`` is
#: served only when the caller supplies a rejected-plan corpus (proposal:
#: grounded planner rejection-ledger search), gated behind
#: ``[loop] grounded_planning_rejection_tool_enabled``.
READ_ONLY_TREE_TOOLS: frozenset[str] = frozenset(
    {
        "read_source",
        "grep_source",
        "find_definition",
        "list_cli_surface",
        "read_reality_inventory",
        "search_rejections",
    },
)

#: The longest rationale snippet one :func:`search_rejections` hit renders.
REJECTION_SNIPPET_CHARS = 300
#: Match budget one :func:`search_rejections` call returns.
REJECTION_MAX_MATCHES = 20

#: The hard per-session tree-read budget — twice the reflection budget, because a
#: plan typically needs one ``find_definition`` / ``grep_source`` plus a read of
#: each file in ``files_affected``.
MAX_TREE_TOOL_CALLS = 8

#: The per-tool-result char cap — large enough that a ``read_source`` window
#: stays useful; the truncation marker tells the model to page with ``offset``.
TREE_TOOL_RESULT_CHAR_CAP = 6000

#: The session ``max_turns`` fail-loud backstop. The primary budget is the
#: graceful in-process counter; like ``REFLECTION_SESSION_MAX_TURNS`` this sits
#: above it — every budgeted read, an error turn per over-budget attempt, the
#: trailing text turn, and one spare.
TREE_SESSION_MAX_TURNS = 2 * MAX_TREE_TOOL_CALLS + 2
# Repeat opcodes in a parsed pattern. ``re`` has no timeout, so the regex guard is
# an allowlist: every repeat must sit over one of the single-character matchers
# below, and at most one may be variable-length (a range with maximum above 1).
_REPEAT_OPCODES = frozenset(
    {sre_constants.MAX_REPEAT, sre_constants.MIN_REPEAT, sre_constants.POSSESSIVE_REPEAT},
)
_SINGLE_CHAR_OPCODES = frozenset(
    {
        sre_constants.LITERAL, sre_constants.NOT_LITERAL, sre_constants.ANY,
        sre_constants.IN, sre_constants.RANGE, sre_constants.CATEGORY,
    },
)
# Cap on the product of backtracking choice points (``?`` and alternation arms):
# each one multiplies the paths a failing line is re-tried along.
_MAX_CHOICE_PATHS = 8
_TRUNCATED = "[truncated]"


# ── Door reader ──────────────────────────────────────────────────────────────


def is_confined_locator(locator: str) -> bool:
    """Return ``True`` when *locator* is neither absolute nor carries a ``..`` part."""
    candidate = Path(locator)
    return not (candidate.is_absolute() or ".." in candidate.parts)


def read_door_text(root: Path, locator: str) -> str | None:
    """Read the file at *locator* under *root*, or ``None`` when it cannot be read.

    ``None`` covers an unconfinable locator and an ``(OSError, ValueError)`` read
    failure — absent, unreadable, non-UTF-8 (``UnicodeDecodeError`` ⊂
    ``ValueError``), or an ``embedded null byte`` a locator can smuggle past the
    confinement check. Each caller maps ``None`` to its own fail value.
    """
    if not is_confined_locator(locator):
        return None
    try:
        return (root / locator).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def read_door_corpus(
    root: Path, subdir: str = "maestro", pattern: str = "*.py",
) -> str:
    """Return every *pattern* file under ``root/subdir``, sorted, joined by newlines.

    An unreadable or non-UTF-8 file is skipped, so the corpus degrades rather
    than raising.
    """
    parts: list[str] = []
    for path in sorted((root / subdir).rglob(pattern)):
        try:
            parts.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(parts)


# ── Tool reader ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TreeView:
    """A tree root plus its git-tracked repo-relative POSIX paths (caller-supplied)."""

    root: Path
    tracked: frozenset[str]


def _is_denied(parts: tuple[str, ...]) -> bool:
    return any(
        fnmatch(part, pattern) for part in parts for pattern in TOOL_READ_DENIED_PARTS
    )


def _is_allowlisted(parts: tuple[str, ...]) -> bool:
    if len(parts) == 1:
        name = parts[0]
        return name.endswith(".md") or fnmatch(name, "maestro*.toml")
    return parts[0] in TOOL_READ_ALLOWED_DIRS


def resolve_tool_path(view: TreeView, path: str) -> Path | None:
    """Return the on-disk path for *path* when the tool reader admits it, else ``None``.

    Admitted only when *path* is confined, tracked, allowlisted, not denied, and
    its resolved target stays under the resolved root (a symlink pointing out of
    the tree is refused).
    """
    if not is_confined_locator(path):
        return None
    posix = PurePosixPath(path).as_posix()
    if posix not in view.tracked:
        return None
    parts = PurePosixPath(posix).parts
    if not _is_allowlisted(parts) or _is_denied(parts):
        return None
    candidate = view.root / posix
    if not candidate.resolve().is_relative_to(view.root.resolve()):
        return None
    return candidate


def _read_admitted(view: TreeView, path: str) -> str | None:
    resolved = resolve_tool_path(view, path)
    if resolved is None:
        return None
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _admitted_matching(view: TreeView, path_glob: str) -> list[str]:
    return [
        path
        for path in sorted(view.tracked)
        if PurePosixPath(path).full_match(path_glob)
        and resolve_tool_path(view, path) is not None
    ]


def _refused(path: str) -> str:
    return f"refused: {path!r} is not a readable tracked source file"


def read_source(
    view: TreeView, path: str, offset: int = 0, limit: int = READ_SOURCE_MAX_LINES,
) -> str:
    """Return up to *limit* numbered lines of *path* starting at line ``offset + 1``."""
    text = _read_admitted(view, path)
    if text is None:
        return _refused(path)
    lines = text.splitlines()
    start = max(offset, 0)
    count = min(max(limit, 0), READ_SOURCE_MAX_LINES)
    window = lines[start:start + count]
    if not window:
        return f"[no lines] {len(lines)} lines total"
    rendered = "\n".join(
        f"{number}\t{line}" for number, line in enumerate(window, start=start + 1)
    )
    if start + count < len(lines):
        rendered += f"\n{_TRUNCATED} {len(lines)} lines total"
    return rendered


def _sub_patterns(value: object) -> list[sre_parser.SubPattern]:
    """Return every :class:`SubPattern` directly held in an opcode argument."""
    if isinstance(value, sre_parser.SubPattern):
        return [value]
    if isinstance(value, (list, tuple)):
        return [sub for item in value for sub in _sub_patterns(item)]
    return []


def _backtrack_shape(
    pattern: sre_parser.SubPattern,
) -> tuple[int, int, int] | None:
    """Return ``(unbounded repeats, choice-path product, exact-count width)``.

    ``None`` when refused. The exact-count width sums every ``{n}`` count, so
    the work charge sees ``a{250}`` as the 250-char tail it is.

    A repeat whose operand is anything but one single-character matcher (a
    group, alternation, back-reference, lookaround) is refused outright, as is
    any back-reference or conditional anywhere in the pattern. A repeat
    with a maximum above 1 and a range (not an exact ``{n}`` count) counts as
    unbounded; a bounded optional (``?``,
    ``{0,1}``) multiplies the path product by ``max - min + 1``; a ``BRANCH``
    contributes the sum of its arms' products. Every choice point is a
    backtracking fork, so the product bounds how often a line is re-scanned.
    The walk descends into every group, branch, lookaround and conditional arm.
    """
    unbounded = 0
    product = 1
    width = 0
    for opcode, argument in pattern:
        if opcode in (sre_constants.GROUPREF, sre_constants.GROUPREF_EXISTS):
            # A back-reference re-compares O(len(line)) chars per tried length,
            # breaking the O(len(pattern) * len(line)**2) bound the work estimate charges.
            return None
        if opcode in _REPEAT_OPCODES:
            minimum, maximum, operand = argument
            if len(operand) != 1 or operand[0][0] not in _SINGLE_CHAR_OPCODES:
                return None
            if minimum == maximum:
                # An exact count over one character offers no backtracking
                # choice, but the tail it adds is ``minimum`` chars wide.
                width += minimum
            elif maximum > 1:
                unbounded += 1
            else:
                product *= maximum - minimum + 1
            continue
        arms = []
        for sub in _sub_patterns(argument):
            shape = _backtrack_shape(sub)
            if shape is None:
                return None
            arms.append(shape)
        unbounded += sum(arm[0] for arm in arms)
        width += sum(arm[2] for arm in arms)
        if opcode == sre_constants.BRANCH:
            product *= sum(arm[1] for arm in arms)
        else:
            for arm in arms:
                product *= arm[1]
    return unbounded, product, width


def _regex_choice_paths(pattern: str) -> tuple[int, int] | None:
    """Return ``(choice paths, tail weight)``, or ``None`` if the allowlist refuses.

    The tail weight is ``len(pattern)`` plus every exact ``{n}`` count.
    """
    shape = _backtrack_shape(sre_parser.parse(pattern))
    if shape is None:
        return None
    unbounded, product, width = shape
    if unbounded > 1 or product > _MAX_CHOICE_PATHS:
        return None
    return product, len(pattern) + width


def _is_unsafe_regex(pattern: str) -> bool:
    """Return ``True`` unless *pattern* fits the bounded-backtracking allowlist.

    Admitted: at most one variable-length repeat (a range with maximum above 1),
    every repeat (``?`` included) over exactly one single-character matcher, and
    at most :data:`_MAX_CHOICE_PATHS` paths through its ``?``/alternation choices.
    That rules out nested quantifiers, quantified alternation and chains of
    overlapping repeats. At each start position the one repeat backs off through
    every length, re-running the bounded tail each time, so a line costs at most
    O(len(pattern) * len(line)**2). Walks the
    parsed pattern structurally; call only on a pattern that already compiles.
    """
    return _regex_choice_paths(pattern) is None


def grep_source(
    view: TreeView, pattern: str, path_glob: str = "maestro/**/*.py", *,
    regex: bool = False,
) -> str:
    """Return ``path:line: text`` hits for *pattern* across admitted tracked files.

    Literal substring match by default. With *regex*, the pattern must be at most
    :data:`GREP_MAX_PATTERN_CHARS` long and fit the bounded-backtracking
    allowlist: one variable-length repeat at most, every repeat over a single
    character, and at most
    :data:`_MAX_CHOICE_PATHS` paths through its ``?``/alternation choices
    (``re`` has no timeout). A regex searches only the first
    :data:`GREP_MAX_REGEX_LINE_CHARS` of each line (noting how many lines it
    clipped, and suffixing a hit on a clipped line ``[clipped]`` since ``$`` and
    ``\\b`` are evaluated at the cut) and stops once its estimated
    work reaches :data:`GREP_MAX_REGEX_WORK`. The scan stops at
    :data:`GREP_MAX_BYTES_SCANNED` or :data:`GREP_MAX_MATCHES`, noting the
    truncation.
    """
    if not pattern:
        return "refused: empty pattern"
    compiled: re.Pattern[str] | None = None
    choice_paths = 1
    tail_weight = len(pattern)
    if regex:
        if len(pattern) > GREP_MAX_PATTERN_CHARS:
            return f"refused: regex longer than {GREP_MAX_PATTERN_CHARS} characters"
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"refused: invalid regex ({exc})"
        # The only time bound per call: an admitted pattern costs at most
        # O(len(pattern) * len(line)**2) per line, and the scan runs on the
        # event loop.
        paths = _regex_choice_paths(pattern)
        if paths is None:
            return (
                "refused: unbounded-backtracking regex (one variable-length repeat "
                f"max, over a single character; at most {_MAX_CHOICE_PATHS} paths through "
                "?/alternation choices)"
            )
        choice_paths, tail_weight = paths
    hits: list[str] = []
    scanned = 0
    work = 0
    clipped = 0

    def _finish() -> str:
        rendered = list(hits) if hits else ["no matches"]
        if clipped:
            rendered.append(
                f"{_TRUNCATED} {clipped} line(s) searched only to the first "
                f"{GREP_MAX_REGEX_LINE_CHARS} chars"
            )
        return "\n".join(rendered)

    for path in _admitted_matching(view, path_glob):
        text = _read_admitted(view, path)
        if text is None:
            continue
        scanned += len(text.encode("utf-8"))
        if scanned > GREP_MAX_BYTES_SCANNED:
            hits.append(f"{_TRUNCATED} byte budget reached")
            break
        for number, line in enumerate(text.splitlines(), start=1):
            if compiled is not None:
                searched = line[:GREP_MAX_REGEX_LINE_CHARS]
                if len(line) > GREP_MAX_REGEX_LINE_CHARS:
                    clipped += 1
                work += choice_paths * tail_weight * max(len(searched), 1) ** 2
                if work > GREP_MAX_REGEX_WORK:
                    hits.append(f"{_TRUNCATED} regex work budget reached")
                    return _finish()
                matched = compiled.search(searched) is not None
            else:
                matched = pattern in line
            if matched:
                # ``$`` / ``\b`` can match at the clip, so flag hits on cut lines.
                suffix = (
                    " [clipped]" if compiled is not None
                    and len(line) > GREP_MAX_REGEX_LINE_CHARS else ""
                )
                hits.append(f"{path}:{number}: {line}{suffix}")
                if len(hits) >= GREP_MAX_MATCHES:
                    hits.append(f"{_TRUNCATED} match budget reached")
                    return _finish()
    return _finish()


def find_definition(view: TreeView, symbol: str) -> str:
    """Return ``path:line`` for each definition-shaped occurrence of *symbol*.

    Scans admitted tracked ``.py`` files line by line with
    :func:`maestro.plan_grounding._has_definition`, so a mention in a comment or
    string never counts.
    """
    if not symbol.strip():
        return "refused: empty symbol"
    hits: list[str] = []
    for path in _admitted_matching(view, "**/*.py"):
        text = _read_admitted(view, path)
        if text is None or symbol not in text:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if _has_definition(line, symbol):
                hits.append(f"{path}:{number}")
                if len(hits) >= GREP_MAX_MATCHES:
                    hits.append(f"{_TRUNCATED} match budget reached")
                    return "\n".join(hits)
    return "\n".join(hits) if hits else f"no definition of {symbol!r}"


def list_cli_surface(view: TreeView) -> str:
    """Render the shipped ``maestro`` CLI surface as compact text."""
    source = _read_admitted(view, _CLI_SOURCE_PATH)
    if source is None:
        return _refused(_CLI_SOURCE_PATH)
    subcommands, flags_by_sub, global_flags = shipped_cli_surface(source)
    lines = [f"global flags: {' '.join(sorted(global_flags)) or '-'}"]
    for name in sorted(subcommands):
        flags = " ".join(sorted(flags_by_sub.get(name, set()))) or "-"
        lines.append(f"{name}: {flags}")
    return "\n".join(lines)


# ── Rejection-ledger reader ──────────────────────────────────────────────────


@dataclass(frozen=True)
class RejectionEntry:
    """A rejected plan projected for :func:`search_rejections` (caller-supplied).

    Mirrors the :class:`TreeView` tracked-set invariant: the leaf reads no
    filesystem, so the caller materialises the corpus and hands it in. Only the
    fields the search returns live here — title, lifecycle ``status`` (the leaf
    admits only ``"rejected"``), the human rejection reason, and the plan
    rationale a snippet is drawn from.
    """

    title: str
    status: str
    reason: str
    rationale: str


_REJECTION_TOKEN_RE = re.compile(r"[a-z]+")


def _rejection_tokens(text: str) -> set[str]:
    """Lower-cased alphabetic tokens of length > 2 in *text* (content words)."""
    return {tok for tok in _REJECTION_TOKEN_RE.findall(text.lower()) if len(tok) > 2}


def search_rejections(corpus: Sequence[RejectionEntry], query: str) -> str:
    """Return title/reason/rationale-snippet for rejected entries matching *query*.

    Operates only on the caller-supplied *corpus* (reads no filesystem), admits
    only entries whose ``status == "rejected"``, and keeps those whose title or
    rationale shares a content word with *query*. The result is bounded by
    :data:`REJECTION_MAX_MATCHES` hits, each rationale clipped to
    :data:`REJECTION_SNIPPET_CHARS`; a malformed or empty query returns a
    ``refused:`` text rather than raising.
    """
    if not isinstance(query, str) or not query.strip():
        return "refused: empty query"
    terms = _rejection_tokens(query)
    if not terms:
        return "refused: query has no content words"
    hits: list[str] = []
    for entry in corpus:
        if entry.status != "rejected":
            continue
        if not (_rejection_tokens(f"{entry.title} {entry.rationale}") & terms):
            continue
        snippet = entry.rationale[:REJECTION_SNIPPET_CHARS]
        hits.append(
            f"title: {entry.title}\nreason: {entry.reason or '-'}\nrationale: {snippet}",
        )
        if len(hits) >= REJECTION_MAX_MATCHES:
            hits.append(f"{_TRUNCATED} match budget reached")
            break
    return "\n\n".join(hits) if hits else "no matching rejected plans"


# ── Bounded tool surface ─────────────────────────────────────────────────────


@dataclass
class TreeReadReceipt:
    """Ordered ``(tool, target, admitted)`` record of the tree reads one surface served."""

    entries: list[tuple[str, str, bool]] = field(default_factory=list)

    def read_paths(self) -> list[str]:
        """Return the de-duplicated, ordered paths of admitted file reads."""
        paths: list[str] = []
        for tool_name, target, admitted in self.entries:
            if (
                admitted
                and tool_name in {"read_source", "list_cli_surface"}
                and target not in paths
            ):
                paths.append(target)
        return paths


_NUMBERED_LINE_RE = re.compile(r"^(\d+)\t")


def _cap_tree_result(text: str) -> str:
    """Cap *text* at :data:`TREE_TOOL_RESULT_CHAR_CAP` on a line boundary.

    When the last kept line is a numbered ``read_source`` line, the marker names
    the ``offset`` that resumes the read after it. A single line that overflows
    the cap is cut mid-line; ``read_source`` has no character offset, so
    re-reading it would return the same truncated text forever — the marker
    instead says the line cannot be shown in full and names the offset past it.
    """
    if len(text) <= TREE_TOOL_RESULT_CHAR_CAP:
        return text
    cut = text.rfind("\n", 0, TREE_TOOL_RESULT_CHAR_CAP + 1)
    mid_line = cut <= 0
    if mid_line:
        cut = TREE_TOOL_RESULT_CHAR_CAP
    kept = text[:cut]
    extra = len(text) - cut
    marker = f"[truncated — {extra} more chars"
    last = _NUMBERED_LINE_RE.match(kept.rsplit("\n", 1)[-1])
    if last is not None:
        line_no = int(last.group(1))
        if mid_line:
            marker += f"; line {line_no} exceeds the cap and cannot be shown in full"
        marker += f"; page with offset={line_no}"
    return f"{kept}\n\n{marker}]"


class _BadArgument(Exception):
    """A tool argument of the wrong type; rendered as a ``refused:`` text."""


def _str_arg(tool_input: dict[str, Any], key: str, default: str) -> str:
    value: object = tool_input.get(key, default)
    if not isinstance(value, str):
        raise _BadArgument(key)
    return value


def _int_arg(tool_input: dict[str, Any], key: str, default: int) -> int:
    value: object = tool_input.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _BadArgument(key)
    return value


def _bool_arg(tool_input: dict[str, Any], key: str) -> bool:
    value: object = tool_input.get(key, False)
    if not isinstance(value, bool):
        raise _BadArgument(key)
    return value


_TreeCall = Callable[[dict[str, Any]], tuple[str, str]]


#: The injected ``read_reality_inventory`` renderer: ``kind`` (``None`` for the
#: overview) to tool-result text.
InventoryReader = Callable[[str | None], str]


def _tree_tool_specs(
    view: TreeView,
    inventory: InventoryReader | None = None,
    rejections: Sequence[RejectionEntry] | None = None,
) -> list[tuple[str, str, dict[str, Any], _TreeCall]]:
    """Return ``(name, description, input_schema, call)`` per tree tool.

    ``call(tool_input)`` returns ``(target, text)``, raising :class:`_BadArgument`
    on a wrongly-typed argument. ``search_rejections`` is appended only when
    *rejections* is supplied, so the tool set is otherwise unchanged.
    """

    def _read(args: dict[str, Any]) -> tuple[str, str]:
        path = _str_arg(args, "path", "")
        offset = _int_arg(args, "offset", 0)
        limit = _int_arg(args, "limit", READ_SOURCE_MAX_LINES)
        return path, read_source(view, path, offset, limit)

    def _grep(args: dict[str, Any]) -> tuple[str, str]:
        pattern = _str_arg(args, "pattern", "")
        path_glob = _str_arg(args, "path_glob", "maestro/**/*.py")
        regex = _bool_arg(args, "regex")
        return pattern, grep_source(view, pattern, path_glob, regex=regex)

    def _find(args: dict[str, Any]) -> tuple[str, str]:
        symbol = _str_arg(args, "symbol", "")
        return symbol, find_definition(view, symbol)

    def _cli(_args: dict[str, Any]) -> tuple[str, str]:
        return _CLI_SOURCE_PATH, list_cli_surface(view)

    def _inventory(args: dict[str, Any]) -> tuple[str, str]:
        # An explicit JSON null for the optional ``kind`` means the overview.
        kind = None if args.get("kind") is None else _str_arg(args, "kind", "") or None
        target = f"inventory:{kind or '*'}"
        if inventory is None:
            return target, "refused: reality inventory unavailable"
        return target, inventory(kind)

    def _search_rejections(args: dict[str, Any]) -> tuple[str, str]:
        query = _str_arg(args, "query", "")
        return query, search_rejections(rejections or (), query)

    specs: list[tuple[str, str, dict[str, Any], _TreeCall]] = [
        (
            "read_source",
            "Read numbered lines of a tracked source file (page with offset).",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
            _read,
        ),
        (
            "grep_source",
            "Search tracked source files for a literal (or safe regex: one "
            "variable-length repeat max, over a single character, few "
            "?/alternation choices, "
            "no back-references; searched only to each line's first "
            f"{GREP_MAX_REGEX_LINE_CHARS} chars) pattern.",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path_glob": {"type": "string"},
                    "regex": {"type": "boolean"},
                },
                "required": ["pattern"],
            },
            _grep,
        ),
        (
            "find_definition",
            "Locate the definition(s) of a Python symbol in tracked files.",
            {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
            _find,
        ),
        (
            "list_cli_surface",
            "List the shipped maestro CLI subcommands and their flags.",
            {"type": "object", "properties": {}},
            _cli,
        ),
        (
            "read_reality_inventory",
            "List measured problem ids and shipped-capability counts; pass kind "
            "(cli, gate, audit-rule, sensor-tag, flag) to list that kind's names.",
            {"type": "object", "properties": {"kind": {"type": "string"}}},
            _inventory,
        ),
    ]
    if rejections is not None:
        specs.append(
            (
                "search_rejections",
                "Search previously rejected plans by theme; returns each match's "
                "title, human rejection reason, and a rationale snippet.",
                {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
                _search_rejections,
            ),
        )
    return specs


def build_bounded_tree_tool_set(
    view: TreeView,
    *,
    inventory: InventoryReader | None = None,
    rejections: Sequence[RejectionEntry] | None = None,
) -> tuple[ToolRegistry, TreeReadReceipt]:
    """Return the bounded read-only ``tree`` server over *view* plus its receipt.

    Every tool shares one call counter per invocation: once it reaches
    :data:`MAX_TREE_TOOL_CALLS` a call returns a graceful ``is_error`` result
    without reading. An admitted call's text is capped at
    :data:`TREE_TOOL_RESULT_CHAR_CAP` and recorded in the receipt, with a
    ``refused:`` result recorded as not admitted. A wrongly-typed argument is a
    ``refused:`` text, never an exception, and still spends a budget slot (so a
    model cannot retry malformed calls without bound). Each invocation gets a fresh counter
    and receipt. *inventory* backs ``read_reality_inventory``; without it that
    tool returns a ``refused:`` text. *rejections* backs ``search_rejections``;
    only when it is supplied is that sixth tool served at all (otherwise the
    five-tool set is unchanged).
    """
    from maestro.inproc_mcp import InprocTool

    receipt = TreeReadReceipt()
    calls_made = [0]

    def _wrap(name: str, call: _TreeCall) -> Handler:
        async def _handler(tool_input: Any) -> dict[str, Any]:
            if calls_made[0] >= MAX_TREE_TOOL_CALLS:
                return {
                    "type": "text",
                    "text": "<tree read budget exhausted; finish your answer now>",
                    "is_error": True,
                }
            calls_made[0] += 1
            args = tool_input if isinstance(tool_input, dict) else {}
            try:
                target, text = call(args)
            except _BadArgument as exc:
                return {"type": "text", "text": f"refused: bad argument {exc.args[0]!r}"}
            receipt.entries.append((name, target, not text.startswith("refused:")))
            return {"type": "text", "text": _cap_tree_result(text)}

        return _handler

    tools = [
        InprocTool(name=name, description=desc, input_schema=schema,
                   handler=_wrap(name, call))
        for name, desc, schema, call in _tree_tool_specs(view, inventory, rejections)
        if name in READ_ONLY_TREE_TOOLS
    ]
    return {TREE_SERVER_NAME: tools}, receipt


# ── CLI surface parser ───────────────────────────────────────────────────────


def _first_str_arg(call: ast.Call) -> str | None:
    """Return *call*'s first positional string-literal argument, or ``None``."""
    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


class _Nested:
    """Owner marker for a parser registered under a sub-subcommand."""


_NESTED = _Nested()
_GROUP_FACTORIES = frozenset({"add_mutually_exclusive_group", "add_argument_group"})


def _parser_owner_of_add_parser(
    call: ast.Call, subparsers_owner: dict[str, str | _Nested | None],
) -> str | _Nested | None:
    """Return the owner a ``<s>.add_parser("name")`` call's parser gets.

    The name is top-level when ``<s>`` is a subparsers action owned by the root
    parser — or when ``<s>`` is unknown, failing open to treating it as
    top-level; it is nested when ``<s>`` belongs to a subcommand's parser.
    ``None`` here means the call carries no literal name (an unnamed parser),
    *not* the root owner — callers must not record it as root.
    """
    name = _first_str_arg(call)
    receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
    if (
        isinstance(receiver, ast.Name)
        and subparsers_owner.get(receiver.id) is not None
    ):
        return _NESTED
    return name


def shipped_cli_surface(
    source: str,
) -> tuple[set[str], dict[str, set[str]], set[str]]:
    """Return the CLI surface in *source* with flags bound to their owning subcommand.

    Returns ``(subcommands, flags_by_subcommand, global_flags)``:

    * ``subcommands`` — the top-level ``add_parser`` name literals: those made
      by a subparsers action the root parser owns (or by an unknown receiver,
      failing open). Nested sub-subcommands and their flags are deliberately
      absent from the triple.
    * ``flags_by_subcommand`` — for each subcommand, the flag/positional name
      literals registered on *that subcommand's* parser, tracked through the
      variable each ``add_parser`` result is assigned to; an argument group
      (``add_mutually_exclusive_group`` / ``add_argument_group``) aliases to
      the parser that owns it.
    * ``global_flags`` — flag literals registered on the root
      ``ArgumentParser`` (directly or through one of its groups).

    Keyword-argument string values (``help`` / ``metavar`` / ``dest`` /
    ``choices``) are excluded. An unparseable source yields empty containers.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set(), {}, set()

    # Pass 1 — assignment and expression statements in source order: resolve
    # which parser owns each parser, group, and subparsers variable (root
    # ``None``, a top-level subcommand name, or ``_NESTED``).
    parser_owner: dict[str, str | _Nested | None] = {}
    subparsers_owner: dict[str, str | _Nested | None] = {}
    subcommands: set[str] = set()
    resolved_calls: set[int] = set()
    statements = sorted(
        (n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.Expr))),
        key=lambda n: (n.lineno, n.col_offset),
    )
    for stmt in statements:
        value = stmt.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        stmt_targets = stmt.targets if isinstance(stmt, ast.Assign) else []
        targets = [t.id for t in stmt_targets if isinstance(t, ast.Name)]
        if (isinstance(func, ast.Attribute) and func.attr == "ArgumentParser") or (
            isinstance(func, ast.Name) and func.id == "ArgumentParser"
        ):
            parser_owner.update(dict.fromkeys(targets))
            continue
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr == "add_parser":
            # Resolve name and owner against the in-order state, so a later
            # rebinding of the subparsers variable cannot split the triple.
            resolved_calls.add(id(value))
            owner = _parser_owner_of_add_parser(value, subparsers_owner)
            if isinstance(owner, str):
                subcommands.add(owner)
            if owner is not None:
                parser_owner.update(dict.fromkeys(targets, owner))
            continue
        receiver = func.value
        if not isinstance(receiver, ast.Name) or receiver.id not in parser_owner:
            continue
        if func.attr == "add_subparsers":
            subparsers_owner.update(dict.fromkeys(targets, parser_owner[receiver.id]))
        elif func.attr in _GROUP_FACTORIES:
            parser_owner.update(dict.fromkeys(targets, parser_owner[receiver.id]))

    # Pass 2 — ``add_parser`` calls not seen as a statement in Pass 1 (e.g.
    # nested inside another expression) also count as subcommand surface.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and id(node) not in resolved_calls
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
        ):
            owner = _parser_owner_of_add_parser(node, subparsers_owner)
            if isinstance(owner, str):
                subcommands.add(owner)

    # Pass 3 — attribute each ``add_argument``'s positional name literals to the
    # subcommand (or root) its receiver variable's owner names; nested-owned
    # receivers are skipped.
    flags_by_sub: dict[str, set[str]] = {}
    global_flags: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "add_argument":
            continue
        receiver = func.value
        if not isinstance(receiver, ast.Name) or receiver.id not in parser_owner:
            continue
        literals = {
            arg.value
            for arg in node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        }
        if not literals:
            continue
        owner = parser_owner[receiver.id]
        if owner is None:
            global_flags.update(literals)
        elif isinstance(owner, str):
            flags_by_sub.setdefault(owner, set()).update(literals)

    return subcommands, flags_by_sub, global_flags

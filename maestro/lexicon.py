"""Parser for Maestro's coined-term lexicon (``docs/lexicon.md``).

Maestro coins hyphenated compounds — ``merge-truth``, ``self-extinguishing``,
``wip-primacy`` — to fuse a recurring behaviour into a single handle. Their
definitions were historically scattered across docstrings, proposal bodies, tag
maps, and inline CLAUDE.md prose. :data:`docs/lexicon.md` is the single checked-in
definitional source of truth; this module turns that Markdown table into a typed,
frozen structure other modules can consume, plus the two spelling accessors that
later coverage and lint work will key on.

This is a **leaf module**: it imports only the standard library and nothing from
the ``maestro`` package (mirroring :mod:`maestro.narration_cues`), so any consumer
can import it without risking an import cycle.

The table is located by its exact six-column header row; a file with no such table
returns ``()`` (no lexicon, not an error), while a data row with the wrong column
count raises :class:`ValueError` — fail loud, never a silent skip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: The lexicon table's column order. The parser locates the header by matching
#: this tuple exactly, and every data row must carry exactly this many cells.
COLUMNS: tuple[str, ...] = (
    "term",
    "definition",
    "names",
    "anchor",
    "variants",
    "minted",
)


@dataclass(frozen=True)
class LexiconEntry:
    """One coined term and everything the lexicon records about it.

    ``variants`` is the (possibly empty) tuple of alternate spellings the
    canonical ``term`` collapses; the remaining fields are the raw cell text.
    """

    term: str
    definition: str
    names: str
    anchor: str
    variants: tuple[str, ...]
    minted: str


def _parse_row(line: str) -> list[str] | None:
    """Split one Markdown table row into stripped cells, or ``None``.

    Returns ``None`` for any line that is not a pipe-delimited table row (so the
    header scan skips prose and the data scan stops at the first non-row line).
    A genuine row ``| a | b |`` yields ``["a", "b"]`` with the leading/trailing
    pipes and surrounding whitespace removed.
    """
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def load_lexicon(path: Path) -> tuple[LexiconEntry, ...]:
    """Parse ``path`` into the frozen tuple of :class:`LexiconEntry` rows.

    Reads the file as UTF-8, locates the six-column header row, skips the
    separator row beneath it, and parses each subsequent pipe-delimited row until
    the table ends. A file containing no matching header returns ``()``; a data
    row whose column count differs from :data:`COLUMNS` raises :class:`ValueError`.
    """
    lines = path.read_text(encoding="utf-8").splitlines()

    header_idx: int | None = None
    for idx, line in enumerate(lines):
        cells = _parse_row(line)
        if cells is not None and tuple(cells) == COLUMNS:
            header_idx = idx
            break
    if header_idx is None:
        return ()

    entries: list[LexiconEntry] = []
    # Skip the header row and the separator row directly beneath it.
    for line in lines[header_idx + 2:]:
        cells = _parse_row(line)
        if cells is None:
            break
        if len(cells) != len(COLUMNS):
            raise ValueError(
                f"lexicon row has {len(cells)} columns, "
                f"expected {len(COLUMNS)}: {line!r}"
            )
        term, definition, names, anchor, variants_cell, minted = cells
        variants = tuple(
            part for part in (p.strip() for p in variants_cell.split(",")) if part
        )
        entries.append(
            LexiconEntry(
                term=term,
                definition=definition,
                names=names,
                anchor=anchor,
                variants=variants,
                minted=minted,
            )
        )
    return tuple(entries)


def canonical_terms(entries: tuple[LexiconEntry, ...]) -> frozenset[str]:
    """The set of canonical ``term`` spellings across ``entries``."""
    return frozenset(entry.term for entry in entries)


def all_spellings(entries: tuple[LexiconEntry, ...]) -> frozenset[str]:
    """Every spelling the lexicon knows — each ``term`` plus its ``variants``."""
    spellings: set[str] = set()
    for entry in entries:
        spellings.add(entry.term)
        spellings.update(entry.variants)
    return frozenset(spellings)


#: Matches one inline-code span inside a tag-map cell — a single
#: backtick-delimited run wrapping one tag token. The tag extractor pulls each
#: span's inner text, so the capture group holds the bare token. ``+`` (not ``*``)
#: ignores an empty ```` `` ```` pair, which is never a real tag.
_TAG_SPAN_RE = re.compile(r"`([^`]+)`")


def extract_sensor_tags(markdown_text: str) -> list[str]:
    """Pull the behavioural-sensor tag tokens out of a ``| Tag | Sensor |`` table.

    Locates the tag map by its exact two-column header (a row whose stripped
    cells equal ``("Tag", "Sensor")``), skips the separator row beneath it, and
    reads each subsequent pipe-delimited row until the first non-row line. From
    every data row's first (Tag) column it extracts each backtick-delimited span
    (:data:`_TAG_SPAN_RE`) as one token, in document order:

    * a slash-composite cell (``PR-READY`` / ``PR-STUCK`` / ``PR-ROTATING``)
      yields one token per span;
    * a space-containing tag (``PENDING-PLAN BACKLOG``) is preserved verbatim
      as a single token;
    * a Tag cell with no backtick span — the ``(task shape / origin)``
      placeholder — contributes nothing.

    Reuses the row-splitting shape of :func:`_parse_row` (leading/trailing pipe
    stripped, cells ``.strip()``-ed) to locate the header and read the rows. A
    *markdown_text* with no such table (or a non-string / empty one) returns
    ``[]``. Pure, stdlib-only, no I/O — a leaf function like
    :func:`extract_coinages` below it.
    """
    if not isinstance(markdown_text, str) or not markdown_text:
        return []
    lines = markdown_text.splitlines()

    header_idx: int | None = None
    for idx, line in enumerate(lines):
        cells = _parse_row(line)
        if cells is not None and tuple(cells) == ("Tag", "Sensor"):
            header_idx = idx
            break
    if header_idx is None:
        return []

    tags: list[str] = []
    # Skip the header row and the separator row directly beneath it.
    for line in lines[header_idx + 2:]:
        cells = _parse_row(line)
        if cells is None:
            break
        tags.extend(_TAG_SPAN_RE.findall(cells[0]))
    return tags


#: Provenance value marking a :class:`LexiconEntry` as one behavioural-sensor
#: tag-map row. :func:`render_sensor_tag_map` selects exactly the entries whose
#: ``minted`` equals this string — the ``minted`` cell every tag-map entry in
#: ``docs/lexicon.md`` carries — so the renderer stays a pure view over the
#: lexicon's own provenance rather than a hand-kept list of tag terms.
_SENSOR_TAG_MINTED = "behavioral-sensor tag map"

#: The one tag-map row that names no tag and therefore has no lexicon entry: the
#: leading ``(task shape / origin)`` row whose sensors feed the task-shape and
#: task-origin classifiers. Held verbatim — byte-for-byte as it appears in the
#: committed ``docs/self-evolution.md`` table — so the render can reproduce the
#: placeholder the lexicon cannot supply. Emitted once, right after the
#: header/separator, ahead of the per-group rows.
_SENSOR_TAG_PLACEHOLDER_ROW = (
    "| (task shape / origin) | `classify_task_shape`, "
    "`classify_task_origin`, `measure_success_by_origin` |"
)


def render_sensor_tag_map(entries: tuple[LexiconEntry, ...]) -> str:
    """Render the ``| Tag | Sensor |`` block from the lexicon's sensor-tag entries.

    Selects the sensor-tag entries (those whose ``minted`` equals
    :data:`_SENSOR_TAG_MINTED`), walks them in lexicon-file order, and coalesces a
    maximal run of *consecutive* entries sharing an identical ``anchor`` into one
    row — the ``PR-READY`` / ``PR-STUCK`` / ``PR-ROTATING`` churn triple, all three
    carrying the same three-symbol anchor, collapses to a single slash-composite
    row. Emits the full table block: the ``| Tag | Sensor |`` header, the
    ``|---|---|`` separator, the one leading placeholder row
    (:data:`_SENSOR_TAG_PLACEHOLDER_ROW`, the ``(task shape / origin)`` row that
    names no tag and so has no lexicon entry), then one row per sensor group.

    Each group's Tag cell is its members' backtick-wrapped variants joined by
    `` / `` — ``variants[0]`` per entry, falling back to ``term.upper()`` when an
    entry carries no variant. Each group's Sensor cell re-renders the shared
    anchor's backtick spans (:data:`_TAG_SPAN_RE`) joined by ``, ``, so the
    lexicon's `` / ``-separated anchor prints in the table's comma style. Pure,
    stdlib-only, no I/O — the write side the golden test and ``maestro lexicon
    --tag-map`` compare against the committed table.
    """
    sensors = [entry for entry in entries if entry.minted == _SENSOR_TAG_MINTED]

    lines = ["| Tag | Sensor |", "|---|---|", _SENSOR_TAG_PLACEHOLDER_ROW]

    idx = 0
    while idx < len(sensors):
        anchor = sensors[idx].anchor
        group = [sensors[idx]]
        idx += 1
        # Coalesce the maximal run of consecutive same-anchor entries.
        while idx < len(sensors) and sensors[idx].anchor == anchor:
            group.append(sensors[idx])
            idx += 1
        tags = " / ".join(
            f"`{entry.variants[0] if entry.variants else entry.term.upper()}`"
            for entry in group
        )
        sensor_cell = ", ".join(f"`{span}`" for span in _TAG_SPAN_RE.findall(anchor))
        lines.append(f"| {tags} | {sensor_cell} |")

    return "\n".join(lines)


def extract_sensor_tag_map_table(markdown_text: str) -> str:
    """Return the committed ``| Tag | Sensor |`` table block from *markdown_text*.

    Locates the tag map by its exact two-column header (a row whose stripped cells
    equal ``("Tag", "Sensor")`` — the same header scan :func:`extract_sensor_tags`
    uses), then returns that header line through the last consecutive
    pipe-delimited row: header, separator, and every data row, joined by ``\\n``.
    Reading stops at the first line that is not a table row (:func:`_parse_row`
    returns ``None``), so trailing prose is excluded. A *markdown_text* with no
    such table (or a non-string / empty one) returns ``""``. This is the read side
    the golden equality test compares the render against. Pure, stdlib-only, no
    I/O.
    """
    if not isinstance(markdown_text, str) or not markdown_text:
        return ""
    lines = markdown_text.splitlines()

    header_idx: int | None = None
    for idx, line in enumerate(lines):
        cells = _parse_row(line)
        if cells is not None and tuple(cells) == ("Tag", "Sensor"):
            header_idx = idx
            break
    if header_idx is None:
        return ""

    block = [lines[header_idx]]
    for line in lines[header_idx + 1:]:
        if _parse_row(line) is None:
            break
        block.append(line)
    return "\n".join(block)


#: Matches a lowercase hyphenated compound — one or more ``a-z`` runs joined by
#: single hyphens (``merge-truth``, ``self-extinguishing``, ``owned-act-drought``).
#: Requires at least one hyphen, so plain words never match; digits and other
#: characters break a run, so ``§5-push`` and ``e2e`` are not picked up.
_COINAGE_RE = re.compile(r"[a-z]+(?:-[a-z]+)+")

#: Ordinary English (and workaday technical) hyphenations that read as a coinage
#: to the regex above but are *not* opaque Maestro-minted vocabulary. Subtracting
#: them keeps plain prose from surfacing as an undefined coinage. It holds only
#: compounds a reader reconstructs from their parts — ordinary English
#: (``read-only``), generic technical (``content-hash``), and Maestro-domain but
#: still-compositional (``evolution-pr``, ``tend-eligible``). It deliberately
#: excludes the opaque minted handles Maestro coins — every ``self-*`` term and
#: terms of art like ``owned-act`` / ``stood-down`` / ``costume-signature`` —
#: which the lexicon (not this stoplist) is responsible for defining. The second
#: block was populated by proposal 101's CLAUDE.md coverage guard
#: (:func:`extract_prose_coinages`), which surfaces every prose coinage the
#: lexicon must otherwise define.
_COMMON_ENGLISH_STOPLIST: frozenset[str] = frozenset({
    "read-only",
    "opt-in",
    "opt-out",
    "zero-cost",
    "fine-grained",
    "coarse-grained",
    "long-term",
    "short-term",
    "well-formed",
    "well-known",
    "built-in",
    "follow-up",
    "follow-on",
    "end-to-end",
    "first-class",
    "high-level",
    "low-level",
    "real-time",
    "non-empty",
    "non-negative",
    "non-zero",
    "case-insensitive",
    "case-sensitive",
    "pre-existing",
    "up-to-date",
    "so-called",
    "by-product",
    "trade-off",
    "round-trip",
    "best-effort",
    "off-by-one",
    "out-of-scope",
    "in-progress",
    "day-to-day",
    "side-effect",
    "in-memory",
    "on-disk",
    "one-shot",
    "one-line",
    "top-level",
    "per-call",
    "per-item",
    "per-task",
    "per-iteration",
    "per-cycle",
    "per-file",
    "double-check",
    "cross-check",
    "hard-coded",
    "fall-through",
    "up-front",
    "no-op",
    # --- Proposal 101 CLAUDE.md coverage-guard additions ---
    # Compositional compounds CLAUDE.md's prose uses that read from their parts;
    # the opaque minted handles among the guard's output went to docs/lexicon.md.
    "action-gated",
    "addressed-thread",
    "agent-self-pruned",
    "ahead-of-origin",
    "ai-finding",
    "all-met",
    "already-landed",
    "already-pushed",
    "anthropic-only",
    "append-only",
    "append-only-keyed",
    "ast-enforced",
    "at-a-glance",
    "auth-gated",
    "auto-deleted",
    "auto-directs",
    "auto-released",
    "auto-releases",
    "auto-resolution",
    "auto-resolved",
    "batch-reviews",
    "behind-but-clean",
    "bind-mount",
    "branch-head",
    "branch-writes",
    "budget-gated",
    "bug-fix",
    "burn-rate",
    "carve-out",
    "catch-up",
    "check-log",
    "ci-failing",
    "ci-gated",
    "ci-pending-only",
    "ci-red",
    "ci-running",
    "classifier-derived",
    "clean-branch",
    "cli-driving",
    "code-clearable",
    "conflict-cooldown",
    "conflict-route",
    "consecutive-remediation",
    "content-hash",
    "content-word",
    "corrupted-record",
    "creation-time",
    "criteria-anchored",
    "deep-dive",
    "default-off",
    "default-on",
    "doc-only",
    "doc-proposals",
    "early-exit",
    "empty-response",
    "eof-only",
    "error-extraction",
    "escalation-budget",
    "evolution-pr",
    "experimental-flag",
    "extended-idle",
    "externally-blocked",
    "fail-loud",
    "failing-gate",
    "fault-seeding",
    "fetch-first",
    "fire-or-close",
    "flag-gated",
    "flag-off",
    "forced-read",
    "fresh-from",
    "full-file",
    "gate-activation",
    "gate-clean",
    "gate-failure",
    "genuinely-new",
    "git-grounded",
    "git-unlanded",
    "give-up",
    "glyph-stripped",
    "guard-less",
    "hand-run",
    "hard-capped",
    "hard-reset",
    "head-of-chain",
    "human-in-the-loop",
    "human-paced",
    "iana-tz",
    "in-loop",
    "in-process",
    "is-ancestor",
    "literal-only-function",
    "llm-judgment",
    "llm-narrated",
    "load-bearing",
    "logs-and-falls-through",
    "long-context",
    "long-lived",
    "loop-authored",
    "lowest-issue-first",
    "lowest-precedence",
    "main-merge",
    "main-phase",
    "marker-less",
    "measured-data",
    "merge-commit",
    "merge-or-close",
    "merge-oracle",
    "module-by-module",
    "multi-concept",
    "multi-file",
    "multi-pr",
    "mutating-phase",
    "near-restated",
    "no-ops",
    "non-disposable",
    "non-green",
    "non-null",
    "non-sdk",
    "non-stalled",
    "non-test",
    "not-ancestor",
    "not-landed",
    "obligation-free",
    "observed-ready",
    "oldest-bug-first",
    "oldest-first",
    "on-demand",
    "one-liner",
    "operator-managed",
    "oracle-gated",
    "os-level",
    "own-pr",
    "ownership-fork",
    "pack-named",
    "partial-construction",
    "path-checked",
    "pending-plan",
    "per-bullet",
    "per-criterion",
    "per-gate",
    "per-iteration-memoized",
    "per-slice",
    "per-test",
    "phase-aware",
    "placeholder-protected",
    "plan-scope",
    "post-call",
    "post-hoc",
    "post-merge",
    "post-push",
    "post-query",
    "post-self-review",
    "post-session",
    "pr-keyed",
    "pr-servicing",
    "pre-call",
    "pre-implementation",
    "pre-merge",
    "pre-pop",
    "pre-push",
    "pre-registers",
    "prior-task",
    "progress-gated",
    "promotable-branch",
    "promote-or-delete",
    "provenance-stamped",
    "provider-attributed",
    "public-api",
    "pytest-cov",
    "pytest-timeout",
    "quality-gate",
    "quick-start",
    "rate-limit",
    "rationale-blind",
    "re-appear",
    "re-exports",
    "re-exposed",
    "re-fire",
    "re-mute",
    "re-offered",
    "re-push",
    "re-read",
    "re-running",
    "re-sent",
    "re-used",
    "read-modify-write",
    "ready-for-review",
    "record-time",
    "regex-enforced",
    "remote-ci",
    "resolution-keyword",
    "restatement-chain",
    "resume-in-place",
    "retired-machinery",
    "retrieval-only",
    "review-thread",
    "rewind-on-red",
    "round-capped",
    "same-turn",
    "session-attached",
    "session-aware",
    "short-circuit",
    "specialized-prompt",
    "squash-merge-aware",
    "stage-specific",
    "startup-swept",
    "status-check",
    "step-group",
    "stream-idle",
    "sub-cap",
    "sub-caps",
    "task-anchored",
    "task-execution",
    "task-gate",
    "tend-clearable",
    "tend-eligible",
    "theme-cluster",
    "thread-clear",
    "timed-out",
    "toctou-safe",
    "top-priority",
    "topo-groups",
    "twenty-one",
    "twenty-two",
    "un-replied",
    "uncalled-symbol",
    "warning-silencing",
    "zero-cost-evicts",
    "zero-sdk",
    "zero-sdk-cost",
})


def find_canonical_drift(
    text: str, entries: tuple[LexiconEntry, ...]
) -> list[tuple[str, str, int]]:
    """Find every lexicon variant spelling ``text`` uses in place of the canonical.

    For each :class:`LexiconEntry`, scans *text* for each of the entry's
    ``variants`` and returns ``(variant, entry.term, count)`` for every variant
    that appears at least once. The match is case-insensitive, word-boundary
    anchored, *and* hyphen-guarded (``(?<!-)\\b…\\b(?!-)``): the ``\\b`` keeps a
    variant that is a prefix of its canonical (``self-extinguish`` inside
    ``self-extinguishing``) from registering, and the hyphen lookarounds keep a
    variant that is a hyphen-adjacent segment of a longer coinage (``stand-down``
    inside ``stand-down-foo`` / ``foo-stand-down``) from registering — since ``-``
    is a non-word char, a bare ``\\b`` sits between it and the segment and would
    false-positive.

    A variant equal case-insensitively to its own ``entry.term`` is skipped, so a
    term that lists a tag form of itself (``settled-relap`` with variant
    ``SETTLED-RELAP``) never self-matches as drift.

    This is the single definition of "what counts as canonical-spelling drift" —
    the ``maestro lexicon --lint`` CLI and the coverage golden test both call it,
    so the two can never disagree. Pure, stdlib-only.
    """
    drift: list[tuple[str, str, int]] = []
    for entry in entries:
        for variant in entry.variants:
            if variant.lower() == entry.term.lower():
                continue
            count = len(re.findall(
                rf"(?<!-)\b{re.escape(variant)}\b(?!-)", text, re.IGNORECASE,
            ))
            if count:
                drift.append((variant, entry.term, count))
    return drift


def extract_coinages(text: str) -> list[str]:
    """Pull the hyphenated coinages out of ``text``, in order, with repeats kept.

    Lowercases *text*, matches every :data:`_COINAGE_RE` hyphenated compound, and
    drops any that is an ordinary English hyphenation
    (:data:`_COMMON_ENGLISH_STOPLIST`). Repetition is preserved so a caller can
    count frequency; order follows first appearance in the text. A non-string or
    empty *text* yields ``[]``.

    This is the shared normaliser the undefined-coinage contemplation sensor and
    any later CLI/coverage tooling both consume — a single definition of "what
    counts as a coinage token" so scans never drift apart. Pure, stdlib-only.
    """
    if not isinstance(text, str) or not text:
        return []
    return [
        token
        for token in _COINAGE_RE.findall(text.lower())
        if token not in _COMMON_ENGLISH_STOPLIST
    ]


#: Matches an inline code span: a single-backtick-delimited run such as the
#: source spans around ``gate-check`` or ``pr-lap-ledger``. CLI/flag identifiers
#: inside these are not prose coinage, so the prose extractor strips them first.
_INLINE_CODE_RE = re.compile(r"`[^`]*`")

#: Matches a Markdown inline link ``[text](target)`` — the visible text *and* the
#: parenthesised target together. Stripping the whole construct (not merely the
#: ``](target)`` half) is deliberate: CLAUDE.md self-links its deep-dives as
#: ``[docs/loop-lifecycle.md](docs/loop-lifecycle.md)``, so removing only the
#: target would leave the doc-filename slug sitting in the link *text* and surface
#: it as a bogus coinage — exactly the "doc-filename slugs ... not prose coinage"
#: the prose extractor exists to exclude. A genuine coinage that also appears in
#: real prose (e.g. ``self-evolution`` in the ``**Self-evolution**`` heading) still
#: surfaces from that prose occurrence.
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")


def extract_prose_coinages(markdown_text: str) -> list[str]:
    """Pull the hyphenated coinages out of *markdown_text*'s **prose only**.

    Strips the three Markdown constructs that carry non-prose hyphenated tokens —
    fenced code blocks (```` ``` ````-delimited), inline code spans
    (:data:`_INLINE_CODE_RE`), and inline links (:data:`_MARKDOWN_LINK_RE`) — then
    delegates the surviving prose to :func:`extract_coinages`, so the single
    definition of "what counts as a coinage token" (the regex plus the
    common-English stoplist) stays single-sourced and the prose and journal scans
    can never disagree.

    The fenced-block pass toggles an ``in_code_block`` flag: a line whose
    ``lstrip()``-ed form starts with ```` ``` ```` flips the flag and is itself
    dropped, and every line between markers is dropped. Inline code spans and
    links are stripped *per surviving line*, never across the joined document, so
    an unbalanced backtick (or bracket) in prose can swallow at most the rest of
    its own line rather than real coinages many lines away; within a line the code
    span is stripped before the link so a span holding bracket/paren characters
    cannot leave a dangling fragment for the link pass. A non-string or empty
    *markdown_text* yields ``[]``. Pure, stdlib-only, no I/O.
    """
    if not isinstance(markdown_text, str) or not markdown_text:
        return []
    prose_lines: list[str] = []
    in_code_block = False
    for line in markdown_text.splitlines():
        if line.lstrip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        without_code = _INLINE_CODE_RE.sub(" ", line)
        prose_lines.append(_MARKDOWN_LINK_RE.sub(" ", without_code))
    return extract_coinages("\n".join(prose_lines))


#: Inflectional suffixes the concept-key fold peels, longest-first so a word
#: ending in ``ings`` sheds the whole tail before the shorter ``ing``/``s`` rules
#: can see it. Kept deliberately small — this is a light morphological fold for
#: restatement matching, not a full stemmer.
_CONCEPT_SUFFIXES: tuple[str, ...] = ("ings", "ing", "es", "ed", "s")

#: A suffix is peeled only when at least this many characters would remain, so a
#: short word (``gas`` -> ``gas``, never ``ga``) is not mangled. Mirrors the
#: minimum-stem intuition behind ``maestro.agent._MIN_STEM_PREFIX``.
_MIN_CONCEPT_STEM = 4


def concept_key(term: str) -> str:
    """Fold *term* to a canonical concept key for restatement matching.

    Lowercases *term*, strips every hyphen, then repeatedly peels a light
    inflectional suffix (:data:`_CONCEPT_SUFFIXES`) while at least
    :data:`_MIN_CONCEPT_STEM` characters would remain. Hyphenation and morphology
    thus collapse together: ``self-extinguishing``, ``self-extinguish`` and
    ``self-extinguishes`` all fold to one key, as do ``stand-down`` and
    ``stand-downs``.

    Peeling iterates to a fixed point — the loop exits only when no suffix passes
    both the ``endswith`` and minimum-stem tests, so the result never itself ends
    in a peelable suffix and the function is idempotent
    (``concept_key(concept_key(x)) == concept_key(x)``). The minimum-stem guard
    keeps short words from being over-stripped. A non-string or empty *term*
    yields ``""``. Pure, stdlib-only, no I/O — a leaf function like
    :func:`extract_coinages` beside it.
    """
    if not isinstance(term, str) or not term:
        return ""
    key = term.lower().replace("-", "")
    changed = True
    while changed:
        changed = False
        for suffix in _CONCEPT_SUFFIXES:
            if (
                key.endswith(suffix)
                and len(key) - len(suffix) >= _MIN_CONCEPT_STEM
            ):
                key = key[: -len(suffix)]
                changed = True
                break
    return key

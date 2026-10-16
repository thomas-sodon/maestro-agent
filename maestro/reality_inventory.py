"""Reality inventory — what ships today and which problems are measured.

The single owner that derives the plan generator's grounding input
(proposal 136): the set of *shipped capabilities* and the set of *measured
problems*, composed from the registries and ledgers that already own each fact
rather than from a new registry or sensor. Zero SDK cost.

:func:`build_reality_inventory` is pure; :func:`collect_inventory_sources` is
the one I/O seam; :func:`load_reality_inventory` composes the two. The module
is import-light so :mod:`maestro.evolution` can import it: it never imports
``evolution``, and reaches :mod:`maestro.task_gates` (which pulls in
``maestro.loop``) only through a function-local import.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from maestro import tree_retrieval
from maestro.audit_checks import RULE_AUTHORITY
from maestro.flag_lifecycle import CUTOVER_FLAGS, EXPERIMENTAL_FLAGS

# Re-exported (redundant alias) so the generation site (``evolution.py``) can
# consume the flag-graduation grounding door through the inventory module it is
# already bound to, rather than importing ``maestro.flag_lifecycle`` directly —
# the ``evolution_off_maestro_flag_lifecycle`` structural guard forbids the
# generation site re-walking a registry the inventory composes (proposal 136).
from maestro.flag_lifecycle import (
    flag_graduation_grounding_verdict as flag_graduation_grounding_verdict,
)
from maestro.lexicon import extract_sensor_tags, load_lexicon, render_sensor_tag_map
from maestro.memory import AuditResult, RejectionRecord, SensorReading

if TYPE_CHECKING:
    from maestro.config import Config
    from maestro.memory import Memory

logger = logging.getLogger(__name__)

ProblemSource = Literal["audit", "sensor", "rejection"]
ClearanceReason = Literal["duplicate", "unmeasured-problem"]

# Load-bearing caps (context-bloat invariant): a later increment injects this
# inventory into a prompt, so every problem list is bounded here, at its owner.
# The shipped-capability set is deliberately uncapped: it answers "does this
# already ship?" membership checks, which a truncated set would answer wrongly;
# a prompt renderer bounds it at render time instead.
MAX_PROBLEMS_PER_SOURCE = 20
MAX_PROBLEM_SUMMARY_CHARS = 160

# Sensor tags whose favourable move is negative (a shrinking owned backlog), so
# only a positive reading is a problem; every other crossed tag is a problem.
_NEGATIVE_IS_FAVOURABLE: frozenset[str] = frozenset({"OWNED-NET-DELTA"})

# Clearance caps: only the first ``MAX_CLEARANCE_NAMES`` declared names are
# tested, and the echoed ``offending`` value is bounded so the downstream
# obsolete reason stays well under ``mark_plan_obsolete``'s 280-char ceiling.
MAX_CLEARANCE_NAMES = 10
#: Render cap on one kind's shipped names in :func:`render_reality_inventory` —
#: the set itself stays uncapped for membership checks.
MAX_RENDERED_CAPABILITIES = 200
MAX_OFFENDING_CHARS = 120

# The kind prefixes ``build_reality_inventory`` stamps on shipped capabilities.
_CAPABILITY_PREFIXES: tuple[str, ...] = (
    "cli:", "gate:", "audit-rule:", "sensor-tag:", "flag:",
)

# Ordering of problem sources in the built inventory.
_SOURCE_ORDER: tuple[ProblemSource, ...] = ("audit", "sensor", "rejection")


@dataclass(frozen=True)
class Problem:
    """One measured problem, keyed ``<source>:<name>``."""

    id: str
    source: ProblemSource
    summary: str
    weight: int


@dataclass(frozen=True)
class RealityInventory:
    """Prefixed shipped-capability ids plus the measured problems."""

    shipped_capabilities: frozenset[str]
    measured_problems: tuple[Problem, ...]
    #: Every measured problem id, uncapped — :func:`clear_mint` clears against
    #: this so a problem ranked past :data:`MAX_PROBLEMS_PER_SOURCE` (a prompt
    #: cap) is still measured. Empty falls back to ``measured_problems``' ids.
    measured_problem_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MintClearance:
    """The door verdict on a plan's declared problem and new capability names.

    ``reason`` is ``None`` when cleared; otherwise ``"duplicate"`` (a declared
    name already ships) or ``"unmeasured-problem"`` (the declared problem is not
    measured). ``offending`` is the bounded name or problem id that refused.
    """

    cleared: bool
    reason: ClearanceReason | None
    offending: str | None


@dataclass(frozen=True)
class InventorySources:
    """The raw inputs :func:`build_reality_inventory` composes."""

    cli_subcommands: frozenset[str] = frozenset()
    gate_names: frozenset[str] = frozenset()
    audit_rules: frozenset[str] = frozenset()
    sensor_tags: frozenset[str] = frozenset()
    flag_names: frozenset[str] = frozenset()
    latest_audit: AuditResult | None = None
    sensor_readings: tuple[SensorReading, ...] = ()
    rejections: tuple[RejectionRecord, ...] = ()


def _read_own_cli_source() -> str | None:
    """Return the on-disk source of ``maestro/cli.py`` (sibling of this module).

    Read through :func:`maestro.tree_retrieval.read_door_text`, the tree-read
    owner (proposal 143). Best-effort: an unreadable file yields ``None``
    rather than raising, so the submit-time shipped-CLI guard fails open
    (never wrongly obsoletes a plan).
    """
    return tree_retrieval.read_door_text(
        Path(__file__).resolve().parent.parent, "maestro/cli.py",
    )


def read_shipped_cli_surface(
    source: str | None = None,
) -> tuple[set[str], dict[str, set[str]], set[str]]:
    """Return the shipped CLI surface with flags bound to their owning subcommand.

    Parses *source* (defaulting to the on-disk ``maestro/cli.py``) through
    :func:`maestro.tree_retrieval.shipped_cli_surface`, which owns the AST walk
    and documents the returned ``(subcommands, flags_by_subcommand,
    global_flags)`` triple. Best-effort: an unreadable source yields empty
    containers rather than raising, so the submit-time guard fails open.
    """
    if source is None:
        source = _read_own_cli_source()
    if source is None:
        return set(), {}, set()
    return tree_retrieval.shipped_cli_surface(source)


def _truncate(text: str) -> str:
    """Collapse whitespace to one line and bound to :data:`MAX_PROBLEM_SUMMARY_CHARS`."""
    text = " ".join(text.split())
    if len(text) <= MAX_PROBLEM_SUMMARY_CHARS:
        return text
    return text[: MAX_PROBLEM_SUMMARY_CHARS - 1] + "…"


def _audit_problems(audit: AuditResult | None) -> list[Problem]:
    """One ``audit:<rule>`` problem per distinct finding rule, weight = count."""
    if audit is None:
        return []
    counts: dict[str, int] = {}
    first_statement: dict[str, str] = {}
    for finding in audit.findings:
        if not finding.rule:
            continue
        counts[finding.rule] = counts.get(finding.rule, 0) + 1
        first_statement.setdefault(finding.rule, finding.statement)
    return [
        Problem(
            id=f"audit:{rule}",
            source="audit",
            summary=_truncate(first_statement[rule]),
            weight=count,
        )
        for rule, count in counts.items()
    ]


def _sensor_problems(readings: tuple[SensorReading, ...]) -> list[Problem]:
    """One ``sensor:<TAG>`` per tag crossed unfavourably at the latest timestamp.

    A tag in :data:`_NEGATIVE_IS_FAVOURABLE` crossed with a non-positive value
    moved the good way and is not a problem.
    """
    if not readings:
        return []
    latest = max(reading.timestamp for reading in readings)
    crossed: dict[str, float] = {}
    for reading in readings:
        if reading.timestamp != latest or not reading.crossed:
            continue
        if reading.tag in _NEGATIVE_IS_FAVOURABLE and reading.value <= 0:
            continue
        crossed[reading.tag] = reading.value
    return [
        Problem(
            id=f"sensor:{tag}",
            source="sensor",
            summary=_truncate(f"{tag} reads {value:g} at {latest}"),
            weight=max(1, int(value)),
        )
        for tag, value in crossed.items()
    ]


def _rejection_problems(rejections: tuple[RejectionRecord, ...]) -> list[Problem]:
    """One ``rejection:<category>`` per non-``other`` category, weight = count."""
    counts: dict[str, int] = {}
    for record in rejections:
        category = record.rejection_category
        if category == "other":
            continue
        counts[category] = counts.get(category, 0) + 1
    return [
        Problem(
            id=f"rejection:{category}",
            source="rejection",
            summary=f"{count} recent plan rejection(s) categorised {category}",
            weight=count,
        )
        for category, count in counts.items()
    ]


def build_reality_inventory(sources: InventorySources) -> RealityInventory:
    """Compose *sources* into a :class:`RealityInventory`. Pure, zero SDK.

    Capability ids carry a kind prefix (``cli:``, ``gate:``, ``audit-rule:``,
    ``sensor-tag:``, ``flag:``) so kinds cannot collide. Problems are ordered by
    source, then weight descending, then id — deterministic — and each source
    is capped at :data:`MAX_PROBLEMS_PER_SOURCE`; the capability set is kept
    whole so membership checks stay exact.
    """
    capabilities = (
        [f"cli:{name}" for name in sources.cli_subcommands]
        + [f"gate:{name}" for name in sources.gate_names]
        + [f"audit-rule:{name}" for name in sources.audit_rules]
        + [f"sensor-tag:{name}" for name in sources.sensor_tags]
        + [f"flag:{name}" for name in sources.flag_names]
    )
    by_source: dict[ProblemSource, list[Problem]] = {
        "audit": _audit_problems(sources.latest_audit),
        "sensor": _sensor_problems(sources.sensor_readings),
        "rejection": _rejection_problems(sources.rejections),
    }
    problems: list[Problem] = []
    all_ids = frozenset(
        problem.id for source_problems in by_source.values()
        for problem in source_problems
    )
    for source in _SOURCE_ORDER:
        ranked = sorted(by_source[source], key=lambda p: (-p.weight, p.id))
        problems.extend(ranked[:MAX_PROBLEMS_PER_SOURCE])
    return RealityInventory(
        shipped_capabilities=frozenset(capabilities),
        measured_problems=tuple(problems),
        measured_problem_ids=all_ids,
    )


def collect_inventory_sources(
    config: Config,
    memory: Memory,
    *,
    rejection_window: int,
    sensor_window: int,
) -> InventorySources:
    """Gather :class:`InventorySources` from the owning registries and ledgers.

    The one I/O seam. Read-only: it writes no state and reaches no SDK. A
    missing ``docs/lexicon.md`` yields an empty sensor-tag set.
    """
    from maestro.task_gates import gate_order

    subcommands, _flags_by_sub, _global_flags = read_shipped_cli_surface()
    lexicon_path = config.workspace / "docs" / "lexicon.md"
    sensor_tags: frozenset[str] = frozenset()
    if lexicon_path.exists():
        entries = load_lexicon(lexicon_path)
        sensor_tags = frozenset(extract_sensor_tags(render_sensor_tag_map(entries)))
    return InventorySources(
        cli_subcommands=frozenset(subcommands),
        gate_names=frozenset(name for name, _kind in gate_order()),
        audit_rules=frozenset(RULE_AUTHORITY),
        sensor_tags=sensor_tags,
        flag_names=frozenset(
            [flag.flag for flag in EXPERIMENTAL_FLAGS]
            + [flag.flag for flag in CUTOVER_FLAGS]
        ),
        latest_audit=memory.get_most_recent_audit(),
        sensor_readings=tuple(memory.get_recent_sensor_readings(sensor_window)),
        rejections=tuple(memory.get_recent_rejections(rejection_window)),
    )


#: Default ledger windows for :func:`load_reality_inventory`.
DEFAULT_REJECTION_WINDOW = 50
DEFAULT_SENSOR_WINDOW = 500


def load_reality_inventory(config: Config, memory: Memory) -> RealityInventory:
    """Collect sources and build the inventory — the entry point for callers."""
    return build_reality_inventory(
        collect_inventory_sources(
            config,
            memory,
            rejection_window=DEFAULT_REJECTION_WINDOW,
            sensor_window=DEFAULT_SENSOR_WINDOW,
        )
    )


def _ships(inventory: RealityInventory, name: str, corpus_text: str) -> bool:
    """Return ``True`` when the declared capability *name* already ships.

    A kind-prefixed name is an exact membership test; a bare name ships when a
    shipped id's suffix equals it, or when the corpus already defines it at
    module level (:func:`_defined_at_module_level`). Dotted and dunder names
    (``Cls.method``, ``__init__``) are never module-level capabilities, so they
    are not tested — a new class's methods must not collide with every other
    class's same-named method.
    """
    if name.startswith(_CAPABILITY_PREFIXES):
        return name in inventory.shipped_capabilities
    if any(
        cap.split(":", 1)[1] == name for cap in inventory.shipped_capabilities
    ):
        return True
    if "." in name or (name.startswith("__") and name.endswith("__")):
        return False
    return _defined_at_module_level(corpus_text, name)


def _defined_at_module_level(text: str, name: str) -> bool:
    """Return ``True`` when *text* defines *name* at column zero.

    A narrower sibling of :func:`maestro.plan_grounding._has_definition`: only
    an unindented ``def`` / ``async def`` / ``class`` or a module-level
    ``<name> =`` / ``<name>:`` counts, so a method or keyword argument of the
    same name elsewhere in ``maestro/**`` never refuses a declared capability.
    """
    escaped = re.escape(name)
    patterns = (
        rf"^(?:async[ \t]+)?def[ \t]+{escaped}\b",
        rf"^class[ \t]+{escaped}\b",
        rf"^{escaped}[ \t]*[:=]",
    )
    return any(re.search(p, text, re.MULTILINE) for p in patterns)


def declares_cli_capability(names: list[str] | None) -> bool:
    """True iff a name :func:`clear_mint` tests declares a ``cli:`` capability.

    Applies the same strip-and-cap rule as :func:`clear_mint`, so a ``cli:``
    entry past :data:`MAX_CLEARANCE_NAMES` (never cleared) does not count. A
    degenerate ``cli:`` whose suffix is empty or holds whitespace names no
    subcommand, so it does not count either.
    """
    return any(
        re.fullmatch(r"cli:\S+", raw.strip()) is not None
        for raw in (names or [])[:MAX_CLEARANCE_NAMES]
    )


def clear_mint(
    inventory: RealityInventory,
    *,
    problem_ref: str | None,
    new_capability_names: list[str] | None,
    corpus_text: str,
) -> MintClearance:
    """Clear a plan's declarations against *inventory* — pure, zero I/O.

    Rules, in order: a declared name that already ships refuses ``duplicate``;
    a non-``None`` *problem_ref* absent from the measured problems refuses
    ``unmeasured-problem``; otherwise cleared. Neither field declared clears
    unconditionally (the open-ended escape hatch). Only the first
    :data:`MAX_CLEARANCE_NAMES` names are tested.
    """
    for raw in (new_capability_names or [])[:MAX_CLEARANCE_NAMES]:
        name = raw.strip()
        if name and _ships(inventory, name, corpus_text):
            return MintClearance(
                cleared=False,
                reason="duplicate",
                offending=name[:MAX_OFFENDING_CHARS],
            )
    measured = inventory.measured_problem_ids | {
        problem.id for problem in inventory.measured_problems
    }
    if problem_ref is not None and problem_ref not in measured:
        return MintClearance(
            cleared=False,
            reason="unmeasured-problem",
            offending=problem_ref[:MAX_OFFENDING_CHARS],
        )
    return MintClearance(cleared=True, reason=None, offending=None)


def _capability_kinds() -> list[str]:
    """Return the bare capability kinds (``cli``, ``gate``, …) in prefix order."""
    return [prefix.rstrip(":") for prefix in _CAPABILITY_PREFIXES]


def render_reality_inventory(inventory: RealityInventory, kind: str | None) -> str:
    """Render *inventory* as bounded tool-result text — pure, zero I/O.

    With *kind* ``None`` it lists the shipped-capability count per kind and the
    valid *kind* values first, then the measured problems (already capped per
    source at :data:`MAX_PROBLEMS_PER_SOURCE`). With a known kind it lists that
    kind's sorted bare names, capped at :data:`MAX_RENDERED_CAPABILITIES` with a
    truncation note. An unknown kind is a ``refused:`` line naming the kinds.
    """
    kinds = _capability_kinds()
    if kind is None:
        # Counts and the kind vocabulary lead, so a per-result cap that
        # truncates a long problem list never drops the navigation lines.
        lines = ["shipped capabilities by kind:"]
        for name in kinds:
            prefix = f"{name}:"
            count = sum(
                1 for cap in inventory.shipped_capabilities if cap.startswith(prefix)
            )
            lines.append(f"{name}: {count}")
        lines.append(f"list one kind's names with kind = one of: {', '.join(kinds)}")
        lines.append("measured problems (cite one id as problem_ref):")
        lines.extend(
            f"{problem.id} (weight {problem.weight}): {problem.summary}"
            for problem in inventory.measured_problems
        )
        if not inventory.measured_problems:
            lines.append("-")
        return "\n".join(lines)
    if kind not in kinds:
        return f"refused: unknown kind {kind!r}; valid kinds: {', '.join(kinds)}"
    prefix = f"{kind}:"
    names = sorted(
        cap[len(prefix):] for cap in inventory.shipped_capabilities
        if cap.startswith(prefix)
    )
    lines = [f"shipped {kind} ({len(names)}):"]
    lines.extend(names[:MAX_RENDERED_CAPABILITIES])
    if len(names) > MAX_RENDERED_CAPABILITIES:
        lines.append(
            f"[truncated — {len(names) - MAX_RENDERED_CAPABILITIES} more names]"
        )
    return "\n".join(lines)


def make_inventory_reader(
    config: Config, memory: Memory,
) -> Callable[[str | None], str]:
    """Return the ``read_reality_inventory`` callable for one tree session.

    The inventory loads lazily on the first call and is memoised for the
    reader's lifetime (one session), so repeated calls pay one load. An
    ``OSError`` from the load renders as a ``refused:`` text (logged at INFO)
    and is retried on the next call, mirroring the door's fail-open read.
    """
    loaded: list[RealityInventory] = []

    def _reader(kind: str | None) -> str:
        if not loaded:
            try:
                loaded.append(load_reality_inventory(config, memory))
            except OSError as exc:
                logger.info("Reality inventory read failed (%s) — tool refused", exc)
                return "refused: reality inventory unavailable"
        return render_reality_inventory(loaded[0], kind)

    return _reader

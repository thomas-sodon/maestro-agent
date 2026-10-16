"""Plan-premise grounding — the pure owner deciding whether an evolution plan's
premise still holds against the tree it names.

Every evolution plan the agent authors is grounded in what the model *believes*
about its own code, never in a fresh reading of the tree the plan names. Nothing
between contemplation and the human approval prompt confronts the two, so the
pipeline can surface plans premised on facts the codebase refutes — most often a
gate, sensor, or actuator the plan proposes to *build* that already exists and is
live (a ``duplicate``), or a defect the plan proposes to *fix* that is not there
(``no-such-defect``). Today the only grounding oracle is the human reviewer, who
re-runs a ``grep`` by hand and spends a scarce approval to reject a false premise.

This module is the closed, provenance-stamped computer of "is this plan's premise
true against the tree?". It defines the closed probe vocabulary
(:data:`GroundingKind`), a frozen :class:`GroundingProbe` describing the single
check a plan must clear, a frozen :class:`GroundingVerdict` carrying the outcome
and — when ungrounded — a category slug, and a pure :func:`confirm` that decides
groundedness from *injected* text. ``SYMBOL_ABSENT`` scans a corpus for a
*definition-shaped* occurrence of the locator (found ⇒ the symbol already exists
⇒ ``ungrounded("duplicate")``); ``BEHAVIOR_ABSENT`` looks for a witness substring
in the text of the file the behavior would live in (found ⇒ the behavior already
ships, under whatever name ⇒ ``ungrounded("duplicate")``) — the behavioral
counterpart that closes ``SYMBOL_ABSENT``'s name-novelty hole, since the name a
build-plan proposes is absent by construction; ``DEFECT_PRESENT`` looks for the
expectation
needle in a file's text (absent ⇒ ``ungrounded("no-such-defect")``);
``LINT_PRESENT`` reads the injected ruff findings for a named rule against a
named file (empty ⇒ ruff runs clean ⇒ the claimed lint error is hallucinated ⇒
``ungrounded("no-such-lint")``) — the ruff read happens upstream behind an
injectable reader, so this owner still touches no filesystem.

Pure functions, zero SDK cost, no I/O: the caller supplies the text to read, so
this owner never touches the filesystem. Mirrors the
:mod:`maestro.frame_discharge` skeleton — a ``Literal`` closed vocabulary, a
``@dataclass(frozen=True)`` provenance-carrying verdict, and plain module-level
pure ``def``s.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

#: The closed probe vocabulary. ``SYMBOL_ABSENT`` asserts that a symbol the plan
#: proposes to build is *not yet* defined in the tree (a plan whose premise is
#: "this gate/sensor/actuator does not exist yet" — sound only when the plan
#: reuses the exact identifier an existing mechanism may already hold);
#: ``BEHAVIOR_ABSENT`` asserts that the *behavior* a plan proposes to build is
#: not yet present at the file it names, regardless of what the new mechanism
#: would be called; ``DEFECT_PRESENT`` asserts
#: that a defect the plan proposes to fix *is* present in a named file;
#: ``LINT_PRESENT`` asserts that a lint rule the plan proposes to fix *is*
#: reported by ruff on a named file (the injected reader runs the exact rule
#: against the exact file upstream, so this owner reads only text). Kept a
#: ``Literal`` so a new kind is a one-line vocabulary edit.
GroundingKind = Literal[
    "SYMBOL_ABSENT", "BEHAVIOR_ABSENT", "DEFECT_PRESENT", "LINT_PRESENT",
]

#: The ungrounded-category slugs, paired one-to-one with the probe kinds:
#: ``"duplicate"`` when a ``SYMBOL_ABSENT`` locator is already defined or a
#: ``BEHAVIOR_ABSENT`` witness is already present (both name the same failure —
#: the mechanism already ships — so no new category slug is minted and the
#: ``[ungrounded:*]`` reason vocabulary and ``UNGROUNDED-RATE`` sensor are
#: untouched),
#: ``"no-such-defect"`` when a ``DEFECT_PRESENT`` needle is missing, and
#: ``"no-such-lint"`` when a ``LINT_PRESENT`` rule is not reported by ruff on the
#: named file. ``None`` on a grounded verdict.
GroundingCategory = Literal["duplicate", "no-such-defect", "no-such-lint"]


@dataclass(frozen=True)
class GroundingProbe:
    """The single tree-reading check a plan must clear before it spends an approval.

    ``kind`` selects the check. For ``SYMBOL_ABSENT`` the ``locator`` is the symbol
    name expected to be *absent* from the tree and ``expectation`` is unused. For
    ``BEHAVIOR_ABSENT`` the ``locator`` is the file path the behavior would live
    in and ``expectation`` is a witness substring expected to be *absent* from it.
    For ``DEFECT_PRESENT`` the ``locator`` is the file path whose text the caller reads
    and ``expectation`` is the needle substring expected to be *present*. For
    ``LINT_PRESENT`` the ``locator`` is the file path and ``expectation`` is the
    ruff rule code (e.g. ``RUF036``) expected to be *reported* on it. Frozen so it
    is a stable premise-verification provenance record persisted on the plan.
    """

    kind: GroundingKind
    locator: str
    expectation: str = ""


@dataclass(frozen=True)
class GroundingVerdict:
    """The grounding outcome plus the provenance it was computed from.

    ``grounded`` is ``True`` when the plan's premise holds against the injected
    text. ``category`` is ``None`` on a grounded verdict and otherwise the slug
    naming *why* the premise is false (``"duplicate"`` / ``"no-such-defect"`` /
    ``"no-such-lint"``).
    A ``BEHAVIOR_ABSENT`` refutation reuses ``"duplicate"``.
    ``kind`` carries the probe kind the verdict was derived from, so a caller (or a
    test) can see which check ran without re-deriving it — mirroring the
    provenance-stamped verdict style used across the tracker.
    """

    grounded: bool
    category: GroundingCategory | None
    kind: GroundingKind


def _has_definition(text: str, symbol: str) -> bool:
    """Return ``True`` when *text* contains a *definition-shaped* occurrence of *symbol*.

    Matches only anchored definition shapes — ``def`` / ``async def`` / ``class``
    ``<symbol>``, a module-level ``<symbol> =`` / ``<symbol>:`` assignment, or a
    ``GATE_CHAIN`` registration ``name="<symbol>"`` or the module-level gate-name
    constant it may register through (``_X_GATE = "<symbol>"``) — so a bare
    mention of the name in a comment or string never false-fires. *symbol* is regex-escaped, so a name
    containing regex metacharacters is matched literally. Pure, no I/O.
    """
    name = re.escape(symbol)
    patterns = (
        rf"^[ \t]*(?:async[ \t]+)?def[ \t]+{name}\b",  # def / async def <symbol>
        rf"^[ \t]*class[ \t]+{name}\b",  # class <symbol>
        rf"^{name}[ \t]*[:=]",  # module-level <symbol> = / <symbol>: ...
        rf"""\bname[ \t]*=[ \t]*["']{name}["']""",  # GATE_CHAIN name="<symbol>"
        rf"""^\w+_GATE[ \t]*(?::[^=\n]*)?=[ \t]*["']{name}["']""",  # _X_GATE = "<symbol>"
    )
    return any(re.search(pattern, text, re.MULTILINE) for pattern in patterns)


def confirm(probe: GroundingProbe, read_text: str) -> GroundingVerdict:
    """Decide whether *probe*'s premise holds against the injected *read_text*.

    ``SYMBOL_ABSENT``: *read_text* is the corpus scanned for a definition-shaped
    occurrence of ``probe.locator`` (:func:`_has_definition`) — found means the
    symbol already exists, so the plan's "build this new thing" premise is false
    ⇒ ``ungrounded("duplicate")``; absent ⇒ grounded. ``BEHAVIOR_ABSENT``:
    *read_text* is the named file's text, scanned for the witness substring
    ``probe.expectation`` — present means the behavior already ships at that site
    (whatever it is called) so the "nothing here already does this" premise is
    false ⇒ ``ungrounded("duplicate")``; absent ⇒ grounded. ``LINT_PRESENT``:
    *read_text* is the injected reader's normalized ruff findings for the rule the
    plan names against the file it names — non-empty means ruff reports the rule
    (the "fix this lint error" premise holds ⇒ grounded), empty means ruff runs
    clean (the claimed lint error is hallucinated ⇒
    ``ungrounded("no-such-lint")``); the ruff read happens upstream in the
    injectable seam, so this branch reads only the injected text. ``DEFECT_PRESENT``:
    *read_text* is the named file's text, grounded when ``probe.expectation`` (the
    needle) is present and ``ungrounded("no-such-defect")`` when it is absent. An
    empty ``expectation`` needle is never "present" — it fails safe to
    ``no-such-defect`` rather than trivially matching every file (``"" in text`` is
    always true), so a malformed probe blocks rather than waves a plan through.
    ``BEHAVIOR_ABSENT``'s empty-needle direction is the deliberate *opposite*: its
    refutation is the witness being found, so an empty ``expectation`` — with no
    witness there is nothing to refute the premise with — must ground and fall
    through to the existing guards rather than trivially matching every file and
    false-refusing a real to-build plan. This owner is the last line of that
    defence, not the only one: the probe parser upstream declines a witness-less
    ``BEHAVIOR_ABSENT`` probe outright, so such a probe normally never reaches
    here. A *grounded* ``BEHAVIOR_ABSENT`` verdict whose witness the plan itself
    introduces is self-certifying (:func:`plan_introduces_witness`, #4581): the
    submission guard discounts it to unevaluated, while a present witness still
    refutes.
    Pure, deterministic, no I/O — the caller supplies the text to read.
    """
    if probe.kind == "SYMBOL_ABSENT":
        if _has_definition(read_text, probe.locator):
            return GroundingVerdict(
                grounded=False, category="duplicate", kind="SYMBOL_ABSENT",
            )
        return GroundingVerdict(grounded=True, category=None, kind="SYMBOL_ABSENT")
    if probe.kind == "BEHAVIOR_ABSENT":
        # The witness found in the named file means the behavior already ships
        # there. Empty ``expectation`` fails *safe* by grounding (see the
        # docstring's asymmetry note). Placed beside ``LINT_PRESENT``, before the
        # ``DEFECT_PRESENT`` fallthrough, so an unknown kind still cannot reach
        # here.
        if probe.expectation and probe.expectation in read_text:
            return GroundingVerdict(
                grounded=False, category="duplicate", kind="BEHAVIOR_ABSENT",
            )
        return GroundingVerdict(grounded=True, category=None, kind="BEHAVIOR_ABSENT")
    if probe.kind == "LINT_PRESENT":
        # Non-empty injected findings ⇒ ruff reports the named rule on the named
        # file ⇒ the "fix this lint error" premise holds. Empty ⇒ ruff ran clean
        # ⇒ the claimed lint error is hallucinated. Placed before the
        # DEFECT_PRESENT fallthrough so an unknown kind can never reach here.
        if read_text:
            return GroundingVerdict(grounded=True, category=None, kind="LINT_PRESENT")
        return GroundingVerdict(
            grounded=False, category="no-such-lint", kind="LINT_PRESENT",
        )
    if probe.expectation and probe.expectation in read_text:
        return GroundingVerdict(grounded=True, category=None, kind="DEFECT_PRESENT")
    return GroundingVerdict(
        grounded=False, category="no-such-defect", kind="DEFECT_PRESENT",
    )


#: Trailing tag on an ``[ungrounded:<category>]`` door refusal whose probe
#: locator is among the paths the planner read (proposal 143 step 5) — the
#: planner looked and still misclaimed, which points at reasoning, not access.
PLANNER_READ_TAG = "[planner-read]"

#: Trailing tag on an ``[ungrounded:<category>]`` door refusal whose planner had
#: the tree surface but never read the probed locator — it never looked.
PLANNER_UNREAD_TAG = "[planner-unread]"


def plan_introduces_witness(expectation: str, texts: list[str]) -> bool:
    """Return True when the trimmed *expectation* appears in the plan's *texts*.

    *texts* are the plan's own ``proposed_changes`` plus ``acceptance_criteria``;
    matching is case-insensitive. A witness the plan proposes to add may be
    absent from the tree only because it is not built yet, so a grounded
    ``BEHAVIOR_ABSENT`` verdict over it certifies nothing (#4581). An empty
    witness is never "introduced".
    """
    needle = expectation.strip().casefold()
    if not needle:
        return False
    return needle in "\n".join(texts).casefold()


def _normalise_path(path: str) -> str:
    """Strip a leading ``./`` — mirrors ``audit_checks._normalise_tree_path``."""
    return path[2:] if path.startswith("./") else path


def planner_read_tag(
    probe: GroundingProbe, grounded_reads: list[str] | None,
) -> str:
    """Return the ``" [planner-read]"`` / ``" [planner-unread]"`` reason suffix.

    ``""`` when *grounded_reads* is ``None`` (the planner ran blind, so the
    refusal reason stays byte-identical to the untagged form) or when the probe
    is ``SYMBOL_ABSENT`` (its locator is a symbol, while *grounded_reads* records
    paths only). Otherwise the ``./``-normalised locator is looked up among the
    ``./``-normalised reads. Pure, no I/O.
    """
    if grounded_reads is None or probe.kind == "SYMBOL_ABSENT":
        return ""
    read = {_normalise_path(r) for r in grounded_reads}
    if _normalise_path(probe.locator) in read:
        return f" {PLANNER_READ_TAG}"
    return f" {PLANNER_UNREAD_TAG}"

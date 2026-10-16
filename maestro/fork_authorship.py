"""Fork-authorship classifier — self-authored vs externally-grounded forks.

A *fork* is a pre-registered prediction plus its loss condition. The loss
condition is what would have to happen for the prediction to be refuted. This
module measures one axis of that loss condition's *quality*: whether I authored
it (and thus control whether it fires) or whether it is grounded in world-state
I do not control.

- ``self_authored`` — the loss condition references my own intended action
  ("I will create exactly zero new files"). I control whether the trigger
  fires, so the fork cannot truly refute me; satisfaction here is suspect.
- ``externally_grounded`` — the loss condition references world-state I do not
  author (a git commit count, a pre-existing file count, a measured rate). The
  number is the world's, so the fork can lose; this is the live kind.
- ``unknown`` — no qualifying signal, or empty input.

The heuristic is a zero-cost keyword match — no SDK call. ``self_authored``
signals take precedence: an intended first-person action ("I will create zero
new files") is self-authored even when it mentions files, because the trigger
is my choice, not the world's count.
"""

from __future__ import annotations

import re
from typing import Literal

ForkClass = Literal["self_authored", "externally_grounded", "unknown"]

# First-person intended action — "I will create/build/write/add ..." and the
# negated-own-action form "I will not ...". These mark a loss condition whose
# trigger I author.
_SELF_AUTHORED_RE = re.compile(
    r"\bi\s+will\s+(?:not\s+|only\s+|never\s+)?"
    r"(?:create|build|write|add|make|produce|implement|land|"
    r"commit|push|open|submit|change|modify|delete|remove)\b",
    re.IGNORECASE,
)

# The bare negated-own-action form "I will not" / "I won't" — a self-authored
# restraint pledge even when no action verb follows on the same span.
_SELF_NEGATED_RE = re.compile(r"\bi\s+(?:will\s+not|won't)\b", re.IGNORECASE)

# The word "git" as a world-state signal, matched on word boundaries so common
# words that merely contain the substring ("legit", "digit") do not qualify.
# A bare ``"git "`` substring would match inside "legit " / "digit ".
_GIT_WORD_RE = re.compile(r"\bgit\b", re.IGNORECASE)

# World-state signals: git commit counts, file counts, measured pre-existing
# state, landing/merge facts, exit codes. These mark a loss condition grounded
# in something I do not author. The "git" word is matched separately via
# :data:`_GIT_WORD_RE` to avoid bare-substring false positives.
_EXTERNAL_SIGNALS: tuple[str, ...] = (
    "commit count",
    "commits",
    "file count",
    "number of files",
    "files exist",
    "pre-existing",
    "pre-existed",
    "already exist",
    "already on main",
    "measured",
    "success rate",
    "merge-base",
    "is-ancestor",
    "ancestor of",
    "landed on main",
    "exit code",
    "exit-0",
    "ci pass",
    "ci fail",
)


def classify_fork_authorship(text: str) -> ForkClass:
    """Classify a loss condition as self-authored, externally-grounded, or unknown.

    Returns ``"self_authored"`` when *text* describes a first-person intended
    action (or restraint), ``"externally_grounded"`` when it references
    world-state I do not control, and ``"unknown"`` for empty input or text
    with no qualifying signal. Self-authored signals take precedence — an
    intended action wins even when world-state words also appear, because the
    trigger is my choice. Pure function — no I/O.
    """
    if not text:
        return "unknown"
    lowered = text.lower()
    if _SELF_AUTHORED_RE.search(lowered) or _SELF_NEGATED_RE.search(lowered):
        return "self_authored"
    if _GIT_WORD_RE.search(lowered) or any(
        signal in lowered for signal in _EXTERNAL_SIGNALS
    ):
        return "externally_grounded"
    return "unknown"


def _record_text(record: dict[str, object]) -> str:
    """Extract the loss-condition text from a prediction record.

    Prefers the explicit ``loss_condition`` field (reflection self-predictions),
    falling back to ``prediction`` then ``text``. Returns ``""`` when no string
    field is present.
    """
    for key in ("loss_condition", "prediction", "text"):
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def measure_fork_authorship(
    records: list[dict[str, object]],
) -> dict[str, object]:
    """Count fork-authorship classes across prediction *records*.

    Each record's loss-condition text (see :func:`_record_text`) is classified.
    Returns::

        {
            "self_authored": int,
            "externally_grounded": int,
            "unknown": int,
            "total_classified": int,   # self_authored + externally_grounded
            "externally_grounded_ratio": float,
        }

    ``total_classified`` excludes ``unknown`` rows — only forks that produced a
    definite signal. ``externally_grounded_ratio`` is
    ``externally_grounded / total_classified``, or ``0.0`` when no record
    classifies. Pure function — no I/O.
    """
    counts = {"self_authored": 0, "externally_grounded": 0, "unknown": 0}
    for record in records:
        klass = classify_fork_authorship(_record_text(record))
        counts[klass] += 1
    total_classified = counts["self_authored"] + counts["externally_grounded"]
    ratio = (
        counts["externally_grounded"] / total_classified
        if total_classified
        else 0.0
    )
    return {
        "self_authored": counts["self_authored"],
        "externally_grounded": counts["externally_grounded"],
        "unknown": counts["unknown"],
        "total_classified": total_classified,
        "externally_grounded_ratio": ratio,
    }


def format_fork_authorship_summary(stats: dict[str, object]) -> str:
    """Render :func:`measure_fork_authorship` output as a human summary.

    Always returns a non-empty multi-line string, including when no forks
    classified (the absence is itself the finding).
    """
    self_authored = stats.get("self_authored", 0)
    externally_grounded = stats.get("externally_grounded", 0)
    unknown = stats.get("unknown", 0)
    total_classified = stats.get("total_classified", 0)
    ratio = stats.get("externally_grounded_ratio", 0.0)
    ratio_pct = f"{float(ratio) * 100:.0f}%" if isinstance(ratio, (int, float)) else "0%"
    lines = [
        "Fork-Authorship Breakdown",
        "=" * 25,
        f"  self_authored:        {self_authored}",
        f"  externally_grounded:  {externally_grounded}",
        f"  unknown:              {unknown}",
        f"  total classified:     {total_classified}",
        f"  externally-grounded:  {ratio_pct} of classified forks",
    ]
    return "\n".join(lines)

"""Generate a per-phase cost report from the cost-sample backend.

Each cost sample (see :class:`maestro.cost_tracking.CostSample`) records one SDK
call with its caller label, token counts, and dollar cost. This module reads the
most recent N samples from a :class:`~maestro.cost_tracking.CostSampleBackend`
(the JSONL sidecar or ``state.db``, per ``[paths] sqlite_state_enabled``), rolls
them up by phase via :data:`maestro.cost_tracking.CALLER_GROUPS`, and writes a
ranked Markdown table.

Sourcing from per-call samples (rather than iteration-level ``usage.jsonl``
aggregates) gives exact token-weighted attribution: a single 50k-token
``implement_plan`` call no longer gets the same share as a 500-token
``introspect`` call in the same iteration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from maestro.cost_tracking import CALLER_GROUPS

if TYPE_CHECKING:
    from maestro.cost_tracking import CostSample, CostSampleBackend

logger = logging.getLogger("maestro")

_OTHER_GROUP = "Other"

# Phase-aware reduction targets.  When a phase lands in the top-3 by cost
# we pick a target that matches the dominant cost driver for that group.
_PHASE_REDUCTION_TARGETS: dict[str, str] = {
    "Contemplation": "increase cadence interval",
    "Evolution": "reduce context size",
    "Implementation": "reduce context size",
    "Planning & Execution": "reduce context size",
    "Dreaming": "increase cadence interval",
    "Task Generation": "increase cadence interval",
    "Probes": "lower effort level",
    "Artifacts & Docs": "increase cadence interval",
    # Retired phases: their callers no longer run; rows are history only.
    "Scouting": "none (retired phase)",
    "Engagements": "none (retired phase)",
    _OTHER_GROUP: "lower effort level",
}


@dataclass
class PhaseAggregate:
    """Accumulated cost data for a single phase group."""

    phase: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0


def _distribute_to_phases(
    samples: list[CostSample],
) -> dict[str, PhaseAggregate]:
    """Roll per-call samples up into phase-group aggregates.

    Each sample contributes its full ``cost_usd``, ``input_tokens``, and
    ``output_tokens`` to the phase identified by ``CALLER_GROUPS[caller]``;
    callers not in the map land in ``"Other"``.  Because each sample is
    one call with its own exact cost, attribution is token-weighted —
    a 50k-token ``implement_plan`` call dominates a 500-token
    ``introspect`` call in the same window, fixing the previous
    count-weighted misattribution. Fields are read straight off the typed
    :class:`~maestro.cost_tracking.CostSample`, so no per-field coercion or
    malformed-value guarding is needed (the backend already validated them).
    """
    phases: dict[str, PhaseAggregate] = {}

    for sample in samples:
        phase = CALLER_GROUPS.get(sample.caller, _OTHER_GROUP)
        agg = phases.setdefault(phase, PhaseAggregate(phase=phase))
        agg.input_tokens += sample.input_tokens
        agg.output_tokens += sample.output_tokens
        agg.cost_usd += sample.cost_usd
        agg.calls += 1

    return phases


def _reduction_target(phase: str, rank: int) -> str:
    """Return a reduction-target string for a top-3 phase."""
    if rank > 3:
        return ""
    return _PHASE_REDUCTION_TARGETS.get(phase, "lower effort level")


def _format_table(
    phases: dict[str, PhaseAggregate],
) -> str:
    """Render a Markdown table ranked descending by cost_usd."""
    ranked = sorted(phases.values(), key=lambda p: p.cost_usd, reverse=True)
    total_cost = sum(p.cost_usd for p in ranked)

    lines: list[str] = [
        "# Phase Cost Report",
        "",
        "| Phase | Input Tokens | Output Tokens | Calls | Total USD | % of Total | Reduction Target |",
        "|---|---|---|---|---|---|---|",
    ]

    for idx, p in enumerate(ranked, start=1):
        pct = (p.cost_usd / total_cost * 100) if total_cost > 0 else 0.0
        target = _reduction_target(p.phase, idx)
        lines.append(
            f"| {p.phase} "
            f"| {p.input_tokens:,} "
            f"| {p.output_tokens:,} "
            f"| {p.calls:,} "
            f"| ${p.cost_usd:.4f} "
            f"| {pct:.1f}% "
            f"| {target} |"
        )

    lines.append("")
    lines.append(
        f"**Total across all phases:** ${total_cost:.4f}"
    )

    if ranked:
        lines.append("")
        lines.append("## Top-3 Reduction Targets")
        lines.append("")
        for idx, p in enumerate(ranked[:3], start=1):
            target = _reduction_target(p.phase, idx)
            lines.append(
                f"{idx}. **{p.phase}** (${p.cost_usd:.4f}) — {target}"
            )

    lines.append("")
    return "\n".join(lines)


def generate_phase_cost_report(
    backend: CostSampleBackend,
    output_path: Path,
    n_recent: int = 2000,
) -> str:
    """Read recent cost samples from *backend*, aggregate by phase, write it.

    Parameters
    ----------
    backend:
        The :class:`~maestro.cost_tracking.CostSampleBackend` to read the most
        recent samples from (JSONL sidecar or ``state.db``, per the flag).
    output_path:
        Destination for the Markdown report file.
    n_recent:
        Number of most-recent samples to include (bounded by the backend).
        Default is larger than the old per-iteration default (200) because
        each iteration produces many call samples.

    Returns
    -------
    str
        The generated Markdown content (also written to *output_path*).
    """
    samples = backend.recent_samples(n_recent)
    if not samples:
        content = (
            "# Phase Cost Report\n\n"
            "No usage records found.\n"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return content

    phases = _distribute_to_phases(samples)
    content = _format_table(phases)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return content


def refresh_phase_cost_report(
    cost_samples: CostSampleBackend | None, output_path: Path,
) -> None:
    """Refresh the phase-cost report (zero SDK cost — pure file I/O).

    Called by the ``dream`` phase on every cadence pass (proposal 149).
    Best-effort: an I/O or data error here must never crash the dream phase,
    so the failure is logged and swallowed.
    """
    if cost_samples is None:
        return
    try:
        generate_phase_cost_report(cost_samples, output_path)
    except Exception:
        logger.warning(
            "Phase cost report refresh failed during dream; skipping",
            exc_info=True,
        )

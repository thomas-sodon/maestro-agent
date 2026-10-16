"""Tests for the quality-gate + self-review section of docs/self-evolution.md.

Issue #4317: the section still described ``auto_fix()`` as a fixed loop of
``[loop] auto_fix_max_rounds`` rounds (default 3) long after proposal 110 moved
it under ``converge_to_green``'s progress-gated ceiling (20 by default), and
claimed ``self_review()`` always opens a fresh context although by default it
runs on the implementer's session. These guards keep both sentences bound to
the code and the shipped ``maestro.toml`` default.

Like ``test_self_evolution_archive_docs.py``, these are content assertions over
committed repo source (no subprocess, network, or sleep), so the
``mem_fs``/mock-externals rule does not apply.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SELF_EVOLUTION_DOC = _ROOT / "docs" / "self-evolution.md"
_HEADING = "## Post-Implementation Quality Gate + Self-Review"


def _quality_gate_section() -> str:
    text = _SELF_EVOLUTION_DOC.read_text(encoding="utf-8")
    assert text.count(_HEADING) == 1
    body = text.split(_HEADING, 1)[1]
    return body.split("\n#", 1)[0]


def test_quality_gate_section_does_not_claim_fixed_auto_fix_cap() -> None:
    section = _quality_gate_section()
    assert "default 3" not in section
    assert "up to `[loop] auto_fix_max_rounds` times" not in section


def test_quality_gate_section_names_convergence_owner() -> None:
    section = _quality_gate_section()
    assert "converge_to_green" in section
    assert "gate_convergence_ceiling" in section


def test_quality_gate_section_does_not_claim_unconditional_fresh_review_context() -> None:
    section = _quality_gate_section()
    assert "opens a fresh LLM context" not in section
    assert "self_review_own_session_enabled" in section


def test_self_review_own_session_default_matches_doc() -> None:
    # The doc says the review runs on the implementation session by default;
    # flipping this default must force a doc update.
    config = tomllib.loads((_ROOT / "maestro.toml").read_text(encoding="utf-8"))
    assert config["loop"]["self_review_own_session_enabled"] is False


def test_convergence_ceiling_default_matches_doc() -> None:
    # The doc says the ceiling backstop is 20 rounds by default:
    # max(gate_convergence_ceiling, auto_fix_max_rounds).
    loop = tomllib.loads((_ROOT / "maestro.toml").read_text(encoding="utf-8"))["loop"]
    assert loop["gate_convergence_ceiling"] == 20
    assert loop["auto_fix_max_rounds"] <= loop["gate_convergence_ceiling"]
    assert "20 rounds by default" in _quality_gate_section()

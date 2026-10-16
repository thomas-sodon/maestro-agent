"""Tests for the gate-lease slot-directory paragraph in docs/deployment.md.

Issue #4054: the paragraph still said ``[loop] gate_lease_enabled`` was
*default off* ("With the flag off (the default) no slot files are created") long
after the lease graduated to default-on, so it told operators no mount change was
needed for the wrong reason. These guards bind the paragraph's stated default to
``Config`` and the shipped ``maestro.toml`` so the next default flip cannot drift
the doc silently.

Like ``test_deployment_restart_policy_docs.py``, these are content assertions over
committed repo source (no subprocess, network, or sleep), so the
``mem_fs``/mock-externals rule does not apply.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from maestro.config import Config

_ROOT = Path(__file__).resolve().parent.parent
_DEPLOYMENT_DOC = _ROOT / "docs" / "deployment.md"
_MAESTRO_TOML = _ROOT / "maestro.toml"


def _gate_lease_paragraph() -> str:
    text = _DEPLOYMENT_DOC.read_text(encoding="utf-8")
    match = re.search(r"\*\*Gate execution lease slot directory.*?(?=\n\s*\n|\Z)", text, re.DOTALL)
    assert match is not None
    return match.group(0)


def _toml_gate_lease_enabled() -> bool:
    with _MAESTRO_TOML.open("rb") as fh:
        data = tomllib.load(fh)
    value = data["loop"]["gate_lease_enabled"]
    assert isinstance(value, bool)
    return value


def test_config_and_toml_gate_lease_defaults_agree() -> None:
    assert Config().gate_lease_enabled == _toml_gate_lease_enabled()


def test_gate_lease_paragraph_states_code_default() -> None:
    paragraph = _gate_lease_paragraph()
    if Config().gate_lease_enabled:
        assert "default on" in paragraph
        assert "default off" not in paragraph
        assert "flag off (the default)" not in paragraph
    else:
        assert "default off" in paragraph
        assert "default on" not in paragraph

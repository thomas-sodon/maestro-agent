"""Regression: the hand-run gate docs must invoke ``maestro`` via ``-m maestro.cli``.

The three command docs in :data:`COMMAND_DOC_GATE_SITES` tell the interactive
agent to run the core suite through ``maestro gate-lease`` and to classify a
failed run with ``maestro gate-classify``. Written as the bare ``maestro``
console script, those resolve off ``PATH`` to whichever single
``~/.local/bin/maestro`` a ``pip install -e`` last bound. On a host running
several sibling checkouts that install belongs to *one* checkout while the agent
works in *another*: the bare invocation loads *this* checkout's ``maestro.toml``
but validates it against the *other* checkout's config models, and a ``[loop]``
key present here but absent from the stale install's ``_LoopSettings`` raises a
pydantic ``extra_forbidden`` (``extra="forbid"`` is a deliberate proposal-106
fail-loud invariant) — a confusing "real config bug" mask over what is only a
CLI-vs-checkout mismatch.

The fix mirrors ``scripts/quality-gates.sh``'s ``maestro()`` shell function
(``$PY -m maestro.cli``): spell each subcommand as ``python3.14 -m maestro.cli
<sub>`` so ``sys.path[0]`` self-resolves the CLI to the checkout under test. This
guard fails against a bare ``maestro gate-lease`` / ``maestro gate-classify``
occurrence — capturing today's bad state — and passes only once every occurrence
carries the module prefix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.gate_sites import COMMAND_DOC_GATE_SITES

#: Every ``gate-lease`` / ``gate-classify`` in a hand-run gate doc must be spelled
#: through this module prefix, not the bare ``maestro`` console script, so the CLI
#: self-resolves to the checkout under test rather than a stale sibling install.
_CLI_PREFIX = "python3.14 -m maestro.cli "

#: The gate subcommands whose spelling this guard pins.
_GATE_SUBCOMMANDS = ("gate-lease", "gate-classify")


def _occurrences(text: str, needle: str) -> list[int]:
    """Return the start index of every ``needle`` in ``text`` (overlapping-safe)."""
    positions: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            return positions
        positions.append(idx)
        start = idx + 1


@pytest.mark.parametrize("doc", COMMAND_DOC_GATE_SITES, ids=lambda path: path.name)
def test_gate_subcommands_resolve_from_checkout(doc: Path) -> None:
    """Every gate subcommand in the doc is invoked via ``python3.14 -m maestro.cli``.

    Non-vacuous: each doc must actually invoke both subcommands, so a doc that
    silently dropped its gate lines cannot pass this guard by having nothing to
    scan.
    """
    content = doc.read_text(encoding="utf-8")
    for subcommand in _GATE_SUBCOMMANDS:
        positions = _occurrences(content, subcommand)
        assert positions, (
            f"{doc.name}: expected at least one '{subcommand}' invocation to pin, "
            "found none — the hand-run gate doc must still prescribe it."
        )
        for pos in positions:
            preceding = content[pos - len(_CLI_PREFIX) : pos]
            assert preceding == _CLI_PREFIX, (
                f"{doc.name}: '{subcommand}' at offset {pos} is not spelled "
                f"'{_CLI_PREFIX}{subcommand}'. A bare 'maestro {subcommand}' "
                "resolves off PATH to a sibling checkout's editable install, which "
                "loads this checkout's maestro.toml against its own config models "
                "and raises a spurious pydantic 'extra_forbidden'. Spell it "
                f"'{_CLI_PREFIX}{subcommand}' so sys.path[0] self-resolves the CLI "
                "to the checkout under test (mirrors scripts/quality-gates.sh's "
                "maestro() shell function)."
            )

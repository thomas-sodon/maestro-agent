"""Static-text guard for the 'Directive slot' invariant line in ``CLAUDE.md``.

The line is an orientation pointer for the ``next_task_directive`` one-shot
clear contract. It previously said the directive is "cleared on accepted task"
— which is false: the directive is cleared inside ``generate_task``'s curation
pass (via ``mutate_self_model``) once a response parses, *independent of whether
any new enqueue entry survived the post-call gates*. That stale line was acted
on as if true (#4166). This guard pins the corrected wording so it cannot drift
back, and cross-checks it against the tested contract in
``test_generate_task_clears_directive_even_when_gate_drops_new_entry`` (the clear
fires even when every enqueue entry is gated out) and forbids reintroducing a
phantom symbol like ``_consume_next_task_directive`` that got a prior attempt
rejected.

Static-text only: reads ``CLAUDE.md``, no ``mem_fs``/subprocess/network/sleep.
"""

from __future__ import annotations

from pathlib import Path

_CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"


def _directive_slot_line() -> str:
    text = _CLAUDE_MD.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "**Directive slot**" in line:
            return line
    raise AssertionError("No '**Directive slot**' invariant line in CLAUDE.md")


def test_directive_slot_line_states_curation_pass_clear_contract() -> None:
    """The line must state the parse-time, gate-independent clear contract.

    Criterion 1: cleared inside ``generate_task``'s curation pass once a
    response parses, even when every enqueue entry is gated out, and survives
    SDK/parse failures.
    """
    line = _directive_slot_line()
    assert "generate_task" in line, (
        "The line must name `generate_task` as the pass that clears the "
        f"directive. Line:\n{line}"
    )
    assert "curation pass" in line, (
        "The line must say the directive is cleared inside the curation pass, "
        f"not 'on accepted task'. Line:\n{line}"
    )
    assert "parses" in line, (
        "The line must condition the clear on a parseable response. "
        f"Line:\n{line}"
    )
    lowered = line.lower()
    assert "gated out" in lowered or "gate" in lowered, (
        "The line must state the clear is independent of whether every enqueue "
        f"entry is gated out. Line:\n{line}"
    )
    assert "survives" in lowered and (
        "sdk" in lowered and "parse" in lowered
    ), (
        "The line must state the directive survives SDK/parse failures. "
        f"Line:\n{line}"
    )
    assert "cleared on accepted task" not in lowered, (
        "The stale 'cleared on accepted task' claim (false, #4166) must be gone."
    )


def test_directive_slot_line_cites_only_real_symbols() -> None:
    """Criterion 2: only shipped symbols, no phantom symbol."""
    line = _directive_slot_line()
    assert "mutate_self_model" in line, (
        "The line must cite `mutate_self_model` as the clear's write seam. "
        f"Line:\n{line}"
    )
    assert "_consume_next_task_directive" not in line, (
        "The line must not cite the phantom symbol `_consume_next_task_directive` "
        "that got a prior attempt rejected."
    )


def test_directive_slot_wording_agrees_with_tested_contract() -> None:
    """Criterion 3: consistent with the gate-drops-new-entry test's assertions.

    The test proves the directive clears to ``None`` even when the queue stays
    empty because every new entry was gated out. The doc line must carry that
    same gate-independence, so the two never drift.
    """
    test_file = (
        Path(__file__).resolve().parent / "test_agent_task_generation.py"
    ).read_text(encoding="utf-8")
    marker = "async def test_generate_task_clears_directive_even_when_gate_drops_new_entry"
    assert marker in test_file, (
        "The anchoring test must exist; the doc line's contract is grounded in "
        "it."
    )
    start = test_file.index(marker)
    end = test_file.index("\nasync def ", start + len(marker))
    body = test_file[start:end]
    assert "reloaded.next_task_directive is None" in body, (
        "The anchoring test must assert the directive clears to None."
    )
    assert "agent.task_queue.load() == []" in body, (
        "The anchoring test must assert the queue stays empty (every entry "
        "gated out) — the exact case the doc line's 'even if every enqueue "
        "entry is gated out' clause describes."
    )

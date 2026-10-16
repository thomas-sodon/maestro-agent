"""Tests for the extracted ``maestro.loop.lifecycle.begin_task_run``.

This is ``run``'s entry-time run-initialization block lifted into one named,
independently-testable stage (proposal 106). After the pre-flight stand-down
check and before the resume/plan/branch stages, ``run`` flips ``self._running``,
records the current task goal, resets the four ``_last_remediation_*``
observation fields, installs the loop's signal handlers, records the start
timestamp, mints the task id when the caller did not supply one, stamps that id
onto the per-iteration usage accumulator (under the ``is not None`` guard), and
builds the identity/system-prompt context.

The extraction is behaviour-preserving, so these tests pin the moved behaviour
directly on a bare ``make_agent()`` agent — no ``agent.run`` and no module-level
SDK patching, which is what the extraction bought (the same direct-call shape as
``tests/test_distill_task_findings.py``, which covers the
``maestro.loop.task_record`` sibling). ``_install_signal_handlers`` is stubbed
with a ``MagicMock`` (the fixture deliberately does not mock it), and
``_build_identity_context`` is stubbed to a sentinel string so the returned
triple's threading is unambiguous.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from maestro.loop.lifecycle import begin_task_run
from maestro.sdk import IterationUsage


def test_begin_task_run_preserves_supplied_task_id(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(a) A supplied ``task_id`` is returned unchanged."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._build_identity_context = MagicMock(  # type: ignore[method-assign]
        return_value="identity",
    )

    task_id, _start, _identity = begin_task_run(
        agent,
        goal="do the thing", task_id="feedfacecafe",
    )

    assert task_id == "feedfacecafe"


def test_begin_task_run_mints_task_id_when_absent(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(b) A ``None`` ``task_id`` is minted to a 12-char hex id."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._build_identity_context = MagicMock(  # type: ignore[method-assign]
        return_value="identity",
    )

    minted = MagicMock()
    minted.hex = "0123456789abcdef0123"
    with patch("maestro.agent.uuid.uuid4", return_value=minted):
        task_id, _start, _identity = begin_task_run(
            agent,
            goal="goal", task_id=None,
        )

    assert task_id == "0123456789ab"
    assert len(task_id) == 12


def test_begin_task_run_resets_remediation_fields(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(c) The four ``_last_remediation_*`` fields reset to ``None``."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._build_identity_context = MagicMock(  # type: ignore[method-assign]
        return_value="identity",
    )
    # Pre-seed all four with sentinels so the reset is observable.
    agent._last_remediation_merge_block = "MERGE_BLOCK_SENTINEL"
    agent._last_remediation_merge_source = "host"
    agent._last_remediation_merge_observed_at = 123.0
    agent._last_remediation_items_remaining = 3

    begin_task_run(agent, goal="goal", task_id="feedfacecafe")

    assert agent._last_remediation_merge_block is None
    assert agent._last_remediation_merge_source is None
    assert agent._last_remediation_merge_observed_at is None
    assert agent._last_remediation_items_remaining is None


def test_begin_task_run_sets_running_and_installs_handlers(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(d) ``_running``/``_current_task`` are set and handlers installed once."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._build_identity_context = MagicMock(  # type: ignore[method-assign]
        return_value="identity",
    )

    begin_task_run(agent, goal="my goal", task_id="feedfacecafe")

    assert agent._running is True
    assert agent._current_task == "my goal"
    agent._install_signal_handlers.assert_called_once_with()


def test_begin_task_run_stamps_iteration_usage(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(e) With an accumulator, ``.task_id`` is stamped with the returned id."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._build_identity_context = MagicMock(  # type: ignore[method-assign]
        return_value="identity",
    )
    agent._iteration_usage = IterationUsage(
        iteration=1, started_at="2026-01-01T00:00:00Z",
    )
    assert agent._iteration_usage.task_id is None

    task_id, _start, _identity = begin_task_run(
        agent,
        goal="goal", task_id="feedfacecafe",
    )

    assert task_id == "feedfacecafe"
    assert agent._iteration_usage.task_id == "feedfacecafe"


def test_begin_task_run_without_iteration_usage_does_not_crash(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(e) With ``_iteration_usage`` ``None``, no crash occurs."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._build_identity_context = MagicMock(  # type: ignore[method-assign]
        return_value="identity",
    )
    agent._iteration_usage = None

    task_id, _start, _identity = begin_task_run(
        agent,
        goal="goal", task_id="feedfacecafe",
    )

    assert task_id == "feedfacecafe"


def test_begin_task_run_returns_start_and_identity(
    make_agent,  # type: ignore[no-untyped-def]
) -> None:
    """(f) ``start`` is a float and ``identity_context`` the builder's string."""
    agent = make_agent()
    agent._install_signal_handlers = MagicMock()  # type: ignore[method-assign]
    agent._build_identity_context = MagicMock(  # type: ignore[method-assign]
        return_value="identity-prompt",
    )

    _task_id, start, identity_context = begin_task_run(
        agent,
        goal="goal", task_id="feedfacecafe",
    )

    assert isinstance(start, float)
    assert identity_context == "identity-prompt"
    agent._build_identity_context.assert_called_once_with()

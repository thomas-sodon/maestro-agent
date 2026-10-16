"""Step execution — runs individual plan steps via the Claude Agent SDK."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from maestro.email_tools import (
    SERVER_KEY as EMAIL_SERVER_KEY,
)
from maestro.email_tools import (
    TOOL_NAMES as EMAIL_TOOL_NAMES,
)
from maestro.memory import PlanStep
from maestro.providers.anthropic_tool_adapter import AnthropicToolAdapter
from maestro.sdk import (
    LONG_CONTEXT_BETA,
    ErrorCategory,
    QueryOptions,
    QueryResult,
    SDKError,
    ToolCall,
    create_session,
    disconnect_session,
    query,
    query_on_session,
)
from maestro.telegram_tools import (
    SERVER_KEY as TELEGRAM_SERVER_KEY,
)
from maestro.telegram_tools import (
    TOOL_NAMES as TELEGRAM_TOOL_NAMES,
)
from maestro.tool_gating import make_task_tool_hooks
from maestro.tools import create_messaging_server

if TYPE_CHECKING:
    from maestro.messaging import MessageStore
    from maestro.notifications.base import Notifier
    from maestro.providers.base import Session

logger = logging.getLogger("maestro")


@dataclass
class StepResult:
    success: bool
    output: str
    error: str | None = None
    loop_detected: bool = False
    error_category: ErrorCategory | None = None
    exit_code: int | None = None
    sdk_error: SDKError | None = None
    #: Command strings actually executed via the ``Bash`` tool this step, read
    #: from ``QueryResult.tool_calls`` inputs — NOT the assistant's prose. This
    #: is the executed-command record the CI-failing red-read bind
    #: (:func:`~maestro.outcome_grounding.bind_ci_failing_verdict_to_red_read`)
    #: reads for its non-empty-command-record guard: ``output`` carries only
    #: ``result.response`` (the model's final text), so a signal sourced from it
    #: would measure narration, not execution. Empty for a step that ran no Bash
    #: calls; populated only on the productive success return.
    bash_commands: list[str] = field(default_factory=list)

    @classmethod
    def from_sdk_error(cls, exc: SDKError) -> StepResult:
        return cls(
            success=False,
            output="",
            error=str(exc),
            error_category=exc.category,
            exit_code=exc.exit_code,
            sdk_error=exc,
        )


def _is_productive(result: QueryResult) -> bool:
    """Whether a step result is productive enough to count as a real reply.

    Productive means the model emitted assistant text **or** ran at least one
    tool call that did not error. Tool-only completions (text empty but tools
    succeeded) are productive — late steps often skip the wrap-up sentence
    after a long tool-execution run, and treating those as failures wastes
    the replan budget on work the model actually completed.
    """
    if result.response:
        return True
    return any(not tc.is_error for tc in result.tool_calls)


def _detect_tool_loop(
    tool_calls: list[ToolCall],
    threshold: int = 3,
) -> tuple[bool, str | None]:
    """Detect consecutive identical tool-call fingerprints.

    Returns ``(True, description)`` when *threshold* or more consecutive
    calls share the same fingerprint (name + input hash + result hash).
    """
    if len(tool_calls) < threshold:
        return False, None

    def _fp(tc: ToolCall) -> str:
        inp = hashlib.sha1(
            json.dumps(tc.input, sort_keys=True).encode(),
        ).hexdigest()[:12]
        res = hashlib.sha1(
            (tc.result or "").encode(),
        ).hexdigest()[:12]
        return f"{tc.name}:{inp}:{res}"

    streak = 1
    prev = _fp(tool_calls[0])
    streak_name = tool_calls[0].name
    for tc in tool_calls[1:]:
        fp = _fp(tc)
        if fp == prev:
            streak += 1
            if streak >= threshold:
                return True, f"{streak_name} repeated {streak} times"
        else:
            streak = 1
            prev = fp
            streak_name = tc.name
    return False, None


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
_PROGRESS_GLYPHS_RE = re.compile(r"[█░▓▒]+")
_LONG_REPEATS_RE = re.compile(r"([=+])\1{9,}")
_PKG_MANAGER_RE = re.compile(r"^(pip install|npm install|apt-get)", re.IGNORECASE)
_ERROR_LINE_RE = re.compile(r"(error|warn|fail|exception)", re.IGNORECASE)

_BASH_HARD_CAP = 2000
_BASH_HEAD = 800
_BASH_TAIL = 800

#: Long-context bash output caps — applied when the executor runs under
#: the 1M-token model variant.  Keeps verbose pip/npm/pytest output
#: intact through the head/tail elision so the stored tool-call record
#: still carries the failing assertion.  A 50KB hard
#: cap with 20KB head + 20KB tail leaves a single-step bash result
#: comfortably within the long-context window even after several steps.
_BASH_HARD_CAP_LONG_CONTEXT = 50_000
_BASH_HEAD_LONG_CONTEXT = 20_000
_BASH_TAIL_LONG_CONTEXT = 20_000


def _compress_bash_output(
    output: str,
    *,
    hard_cap: int = _BASH_HARD_CAP,
    head: int = _BASH_HEAD,
    tail: int = _BASH_TAIL,
) -> str:
    """Compress verbose Bash tool output for journal/log storage.

    The default ``hard_cap``/``head``/``tail`` (2KB / 800 / 800) is the
    standard executor budget.  When the executor runs under the
    long-context model variant the caller passes the larger
    ``_BASH_HARD_CAP_LONG_CONTEXT``/``_BASH_HEAD_LONG_CONTEXT``/
    ``_BASH_TAIL_LONG_CONTEXT`` triple so the stored tool-call record
    keeps more of verbose build/test output.
    """
    if not output:
        return output

    # 1. Strip ANSI escape codes.
    text = _ANSI_RE.sub("", output)

    # 2. Strip progress-bar glyphs; collapse long runs of = or +.
    text = _PROGRESS_GLYPHS_RE.sub("", text)
    text = _LONG_REPEATS_RE.sub(r"\1...\1", text)

    # 3. Collapse consecutive identical lines.
    raw_lines = text.split("\n")
    collapsed: list[str] = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        count = 1
        while i + count < len(raw_lines) and raw_lines[i + count] == line:
            count += 1
        collapsed.append(line)
        if count > 1:
            more = count - 1
            unit = "time" if more == 1 else "times"
            collapsed.append(f"[line repeated {more} more {unit}]")
        i += count

    # 4. Package-manager detection — keep first, errors, last 3.
    if collapsed and _PKG_MANAGER_RE.match(collapsed[0]) and len(collapsed) > 4:
        first = collapsed[0]
        last_3 = collapsed[-3:]
        error_lines = [
            ln for ln in collapsed[1:-3] if _ERROR_LINE_RE.search(ln)
        ]
        collapsed = [first, *error_lines, *last_3]

    text = "\n".join(collapsed)

    # 5. Hard cap: ``head`` head + ``tail`` tail with elision marker.
    if len(text) > hard_cap:
        elided = len(text) - head - tail
        text = (
            text[:head]
            + f"\n... [{elided} chars elided] ...\n"
            + text[-tail:]
        )

    return text


class Executor:
    def __init__(
        self,
        notifier: Notifier | None = None,
        message_store: MessageStore | None = None,
        mcp_servers: dict[str, Any] | None = None,
        agent_tools: dict[str, Any] | None = None,
        loop_detect_repeats: int = 3,
        tool_stream_idle_timeout_seconds: float = 1800.0,
        empty_response_retries: int = 1,
        long_context_model: str | None = None,
        disallowed_tools: list[str] | None = None,
        gate_strip_redirections: bool = False,
    ) -> None:
        self._notifier = notifier
        self._message_store = message_store
        self._mcp_servers = mcp_servers
        self._agent_tools = agent_tools
        self._loop_detect_repeats = loop_detect_repeats
        # Mirrors ``Config.sdk_tool_stream_idle_timeout_seconds``' default so an
        # Executor built without the ``cli.py`` wiring does not silently keep a
        # net tighter than the largest budget one gate run may spend; pinned in
        # ``tests/test_executor.py``.
        self._tool_stream_idle_timeout_seconds = tool_stream_idle_timeout_seconds
        self._empty_response_retries = empty_response_retries
        # When set, the task action-gate strips shell redirections before
        # reading a push's refspecs (proposal 096). Read once here and carried
        # onto every gate hook the executor builds, so the config value rides
        # the hook instance to both enforcement paths.
        self._gate_strip_redirections = gate_strip_redirections
        # Per-session tool-denial list (e.g. an external read-only MCP server's
        # mutating tools). ``None`` keeps the SDK's default ``["Agent"]`` denial,
        # so the run_task step session is byte-identical to today while off.
        self._disallowed_tools = disallowed_tools
        # Long-context implementation session — when ``long_context_model``
        # is non-empty the executor session opens on that model with the
        # ``context-1m-2025-08-07`` beta enabled, and bash output capture
        # uses the larger ``_BASH_*_LONG_CONTEXT`` triple so the stored
        # tool-call records keep more of verbose build/test logs.
        self._long_context_model = long_context_model or None
        self._session_client: Session | None = None
        self._session_step_count: int = 0
        self._cached_tool_names: list[str] = []
        self._cached_mcp_servers: dict[str, Any] | None = None
        self._cached_agent_tool_names: list[str] = []
        self._cached_messaging_tool_names: list[str] = []
        self._tool_cache_built: bool = False

    @property
    def _long_context_enabled(self) -> bool:
        return bool(self._long_context_model)

    async def start_session(
        self,
        workspace: str,
        identity_context: str,
        allowed_branches: frozenset[str] | None = None,
        current_branch: str | None = None,
        allowed_push_remotes: frozenset[str] | None = None,
    ) -> None:
        """Create and connect a session for session-reuse mode.

        The session is opened without an initial prompt — the first
        ``execute()`` call sends the real step prompt via
        ``query_on_session``.  *current_branch* (proposal 099) is the run's own
        checked-out branch, forwarded to the action gate so a bare ``HEAD`` /
        refspec-less push resolves to it; ``None`` keeps the gate fail-closed.
        """
        await self.end_session()
        long_context = self._long_context_enabled
        if long_context:
            logger.info(
                "long-context call: caller=execute_step model=%s",
                self._long_context_model,
            )
        self._session_client = await create_session(
            system=identity_context,
            options=QueryOptions(
                allowed_tools=self._all_tool_names(),
                cwd=workspace,
                mcp_servers=self._merged_mcp_servers(),
                disallowed_tools=self._disallowed_tools,
                caller="execute_step",
                model=self._long_context_model if long_context else None,
                betas=[LONG_CONTEXT_BETA] if long_context else None,
                hooks=make_task_tool_hooks(
                    allowed_branches,
                    strip_redirections=self._gate_strip_redirections,
                    current_branch=current_branch,
                    allowed_push_remotes=allowed_push_remotes,
                ),
            ),
            # The executor backs the run_task phase, so its step session honours a
            # ``[sdk] run_task_provider`` override (proposal 058 Step 6 runtime).
            phase="run_task",
        )
        self._session_step_count = 0

    async def end_session(self) -> None:
        """Disconnect the current session client (safe no-op if none)."""
        if self._session_client is not None:
            try:
                await disconnect_session(self._session_client)
            except Exception:
                logger.warning("Failed to disconnect session client", exc_info=True)
            self._session_client = None
            self._session_step_count = 0

    def _ensure_tool_cache(self) -> None:
        """Lazily compute and cache tool names and MCP server configs."""
        if self._tool_cache_built:
            return
        extra_tools, extra_mcp = self._build_messaging_options()
        email_tools = self._build_email_tool_names()
        telegram_tools = self._build_telegram_tool_names()
        agent_extra_tools, agent_extra_mcp = self._build_agent_tools_options()
        base_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
        self._cached_tool_names = (
            base_tools + extra_tools + email_tools
            + telegram_tools + agent_extra_tools
        )
        # Cache the messaging tool names so prompt-building can decide whether to
        # add the messaging hint without re-running create_messaging_server (which
        # allocates a throwaway MCP server object per call).
        self._cached_messaging_tool_names = extra_tools
        # Cache the agent tool names so prompt-building can reuse them without
        # re-running the adapter (which rebuilds MCP Server objects per call).
        self._cached_agent_tool_names = agent_extra_tools
        merged = {**(self._mcp_servers or {}), **extra_mcp, **agent_extra_mcp}
        self._cached_mcp_servers = merged if merged else None
        self._tool_cache_built = True

    def _all_tool_names(self) -> list[str]:
        """Collect all tool names the executor makes available to steps."""
        self._ensure_tool_cache()
        return list(self._cached_tool_names)

    def _merged_mcp_servers(self) -> dict[str, Any] | None:
        """Merge all MCP server configs into a single dict."""
        self._ensure_tool_cache()
        return self._cached_mcp_servers

    def _build_messaging_options(self) -> tuple[list[str], dict[str, Any]]:
        """Build extra allowed_tools and mcp_servers for messaging if available.

        Returns (extra_tools, extra_mcp_servers) — both empty when messaging
        is not configured.
        """
        if self._notifier is None or self._message_store is None:
            return [], {}

        server = create_messaging_server(self._notifier, self._message_store)
        extra_tools = [
            "mcp__messaging__send_message_to_thomas",
            "mcp__messaging__read_messages_from_thomas",
        ]
        return extra_tools, {"messaging": server}

    def _build_email_tool_names(self) -> list[str]:
        """Return qualified tool names for the email MCP server, if registered."""
        if not self._mcp_servers:
            return []
        if EMAIL_SERVER_KEY in self._mcp_servers:
            return list(EMAIL_TOOL_NAMES)
        return []

    def _build_telegram_tool_names(self) -> list[str]:
        """Return qualified tool names for the Telegram MCP server, if registered."""
        if not self._mcp_servers:
            return []
        if TELEGRAM_SERVER_KEY in self._mcp_servers:
            return list(TELEGRAM_TOOL_NAMES)
        return []

    def _build_agent_tools_options(self) -> tuple[list[str], dict[str, Any]]:
        """Materialise the registry into qualified names + MCP servers.

        Reads the ``"registry"`` entry of ``agent_tools`` and materialises it
        into ``(qualified_tool_names, mcp_servers_dict)`` via the
        provider-specific :class:`~maestro.providers.base.ToolAdapter`. Today
        the only adapter is
        :class:`~maestro.providers.anthropic_tool_adapter.AnthropicToolAdapter`,
        which reproduces the prior ``{"type": "sdk", "name", "instance"}`` MCP
        wire shape and ``mcp__<server>__<tool>`` qualified names. Returns
        ``([], {})`` when no agent tools are configured.
        """
        if not self._agent_tools:
            return [], {}
        registry = self._agent_tools["registry"]
        return AnthropicToolAdapter().materialize(registry)

    async def execute(
        self,
        step: PlanStep,
        workspace: str,
        identity_context: str,
        artifacts_dir: str | None = None,
        prior_steps: list[PlanStep] | None = None,
        use_session: bool = False,
        allowed_branches: frozenset[str] | None = None,
        current_branch: str | None = None,
        allowed_push_remotes: frozenset[str] | None = None,
    ) -> StepResult:
        """Execute a single plan step using Claude Agent SDK.

        *current_branch* (proposal 099) is the run's own checked-out branch,
        forwarded to the action gate so a bare ``HEAD`` / refspec-less push
        resolves to it; ``None`` keeps the gate fail-closed.
        *allowed_push_remotes* scopes a push's remote;
        ``None`` leaves it unchecked.
        """
        if not step.description or not step.description.strip():
            return StepResult(
                success=False,
                output="",
                error="Step has empty description — skipping API call",
            )
        prompt = self._build_step_prompt(
            step, workspace, artifacts_dir, prior_steps,
            is_session_followup=use_session and self._session_step_count > 0,
        )

        retries = max(0, self._empty_response_retries)
        attempt = 0
        try:
            while True:
                attempt += 1
                on_session = use_session and self._session_client is not None
                try:
                    if use_session and self._session_client is not None:
                        result = await query_on_session(
                            self._session_client,
                            prompt,
                            caller="execute_step",
                            stream_idle_timeout_seconds=(
                                self._tool_stream_idle_timeout_seconds
                            ),
                        )
                        self._session_step_count += 1
                    else:
                        if use_session and self._session_client is None:
                            logger.warning(
                                "use_session=True but no active session "
                                "client — falling back to stateless query",
                            )
                        result = await query(
                            prompt=prompt,
                            system=identity_context,
                            options=self._step_query_options(
                                workspace, allowed_branches, current_branch,
                                allowed_push_remotes,
                            ),
                            stream_idle_timeout_seconds=(
                                self._tool_stream_idle_timeout_seconds
                            ),
                            # Stateless steps back the run_task phase just like
                            # the step session does — without the tag a
                            # ``[sdk] run_task_provider`` override would be
                            # silently ignored for single-step/all-parallel
                            # plans (the use_session=False paths).
                            phase="run_task",
                        )
                except SDKError as exc:
                    # Cheap same-session retry on STREAM_STALL only —
                    # ``sdk.py`` raises this when a completion was
                    # unproductive (no text and no successful tool call)
                    # so the executor can swallow the first occurrence
                    # cheaply before the iteration-level resume-in-place
                    # path in ``agent.py`` cycles to a fresh session.
                    # A stall that poisoned the session (idle timeout, or
                    # a stream that ended with no ResultMessage because
                    # the subprocess exited) carries
                    # ``session_reusable=False``: another turn on it would
                    # duplicate a live prompt or die as NETWORK, so it
                    # propagates for resume-in-place to recreate the
                    # session. A stateless ``query()`` opens a fresh
                    # session per attempt, so it stays retryable. Other categories (``RATE_LIMIT``,
                    # ``CLI_CRASH``, ``STALE_SESSION``) skip the retry and
                    # propagate to their own handlers.
                    if (
                        exc.category != ErrorCategory.STREAM_STALL
                        or (on_session and not exc.session_reusable)
                        or attempt > retries
                    ):
                        raise
                    logger.warning(
                        "STREAM_STALL on attempt %d/%d — same-session "
                        "retry: %s",
                        attempt, retries + 1, step.description,
                    )
                    continue
                return self._process_step_result(result, step)
        except SDKError:
            raise
        except Exception as e:
            logger.exception("Step execution failed: %s", step.description)
            return StepResult(success=False, output="", error=str(e))

    def _step_query_options(
        self,
        workspace: str,
        allowed_branches: frozenset[str] | None = None,
        current_branch: str | None = None,
        allowed_push_remotes: frozenset[str] | None = None,
    ) -> QueryOptions:
        """Build QueryOptions for a stateless step execution."""
        long_context = self._long_context_enabled
        return QueryOptions(
            allowed_tools=self._all_tool_names(),
            cwd=workspace,
            mcp_servers=self._merged_mcp_servers(),
            disallowed_tools=self._disallowed_tools,
            caller="execute_step",
            model=self._long_context_model if long_context else None,
            betas=[LONG_CONTEXT_BETA] if long_context else None,
            hooks=make_task_tool_hooks(
                allowed_branches,
                strip_redirections=self._gate_strip_redirections,
                current_branch=current_branch,
                allowed_push_remotes=allowed_push_remotes,
            ),
        )

    def _build_step_prompt(
        self,
        step: PlanStep,
        workspace: str,
        artifacts_dir: str | None,
        prior_steps: list[PlanStep] | None,
        *,
        is_session_followup: bool = False,
    ) -> str:
        """Build the full prompt for a plan step."""
        artifacts_line = (
            f"Artifacts directory: {artifacts_dir} — write any generated "
            "artifacts (reports, logs, outputs) here.\n\n"
            if artifacts_dir else ""
        )

        prior_context = ""
        if prior_steps:
            parts: list[str] = []
            budget = 2000
            for ps in prior_steps:
                desc = (ps.description or "")[:200]
                snippet = (ps.result or "No result")[:200]
                entry = f"- {desc}: {snippet}"
                if budget - len(entry) < 0:
                    continue
                parts.append(entry)
                budget -= len(entry)
            if parts:
                prior_context = (
                    "Prior steps completed:\n"
                    + "\n".join(parts)
                    + "\n\n"
                )

        self._ensure_tool_cache()
        extra_tools = self._cached_messaging_tool_names
        email_tools = self._build_email_tool_names()
        telegram_tools = self._build_telegram_tool_names()
        agent_extra_tools = self._cached_agent_tool_names
        base_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

        # On a session-attached followup step, the full tool-capability
        # descriptions were already delivered on the first step's turn; the
        # session still carries them, so re-sending the prose is pure
        # redundant input. Suppress it (the tools remain available —
        # allowed_tools is unchanged) and keep only the NEW STEP fence.
        describe_tools = not is_session_followup

        messaging_line = ""
        if extra_tools and describe_tools:
            messaging_line = (
                "\nYou can also communicate with Thomas using "
                "send_message_to_thomas and read_messages_from_thomas. This is "
                "the durable, recorded channel: send_message_to_thomas persists "
                "your message to the message history — it is threaded and "
                "visible in the dashboard — so prefer it for anything Thomas "
                "should be able to see later (questions, decisions, progress "
                "updates). read_messages_from_thomas returns his unread replies "
                "and instructions.\n"
            )

        email_line = ""
        if email_tools and describe_tools:
            email_line = (
                "\nYou have email tools available: email_send (send an email "
                "to any recipient), email_inbox (list inbox emails), and "
                "email_read (read a specific email by UID).\n"
            )

        telegram_line = ""
        if telegram_tools and describe_tools:
            telegram_line = (
                "\nYou also have Telegram tools: telegram_send and "
                "telegram_read_messages. This is a transient Telegram channel "
                "that is NOT recorded in the message history — use it only for "
                "throwaway pings; telegram_read_messages can pull recent "
                "history with a limit. When a message should be recorded, or "
                "when you are unsure, prefer send_message_to_thomas.\n"
            )

        introspection_line = ""
        if agent_extra_tools and describe_tools:
            introspection_line = (
                "\nYou have access to introspection tools: "
                "write_journal_entry (record insights), "
                "read_journal/search_journal (recall past reflections), "
                "read_self_model (your identity), "
                "read_task_history (past tasks), "
                "read_evolution_plans (evolution history), "
                "list_open_issues/create_issue/close_issue (work tracking), "
                "get_diagnostic_snapshot/get_recent_errors/"
                "is_diagnostics_noteworthy (operational health). "
                "Use these to inform your decisions and record learnings.\n"
            )

        fence = ""
        if is_session_followup:
            fence = (
                "=== NEW STEP ===\n"
                "The previous conversation was a different task step. Do not "
                "reference tool results from the previous step unless "
                "explicitly provided below.\n\n"
            )

        return f"""{fence}Execute this task in the workspace: {workspace}

{artifacts_line}{prior_context}Task: {step.description}

Use the tools available to you ({', '.join(base_tools)})
to complete this task. Be precise and thorough.
{messaging_line}{email_line}{telegram_line}{introspection_line}
When done, summarize what you did and the result."""

    def _process_step_result(
        self,
        result: QueryResult,
        step: PlanStep,
    ) -> StepResult:
        """Post-process a QueryResult into a StepResult."""
        if not _is_productive(result):
            logger.warning("Step returned empty response: %s", step.description)
            return StepResult(
                success=False, output="", error="Empty response from SDK",
            )
        if result.tool_calls and self._loop_detect_repeats >= 2:
            detected, loop_msg = _detect_tool_loop(
                result.tool_calls, threshold=self._loop_detect_repeats,
            )
            if detected:
                return StepResult(
                    success=False,
                    output=result.response,
                    error=f"loop detected: {loop_msg}",
                    loop_detected=True,
                )
        long_context = self._long_context_enabled
        bash_hard_cap = (
            _BASH_HARD_CAP_LONG_CONTEXT if long_context else _BASH_HARD_CAP
        )
        bash_head = (
            _BASH_HEAD_LONG_CONTEXT if long_context else _BASH_HEAD
        )
        bash_tail = (
            _BASH_TAIL_LONG_CONTEXT if long_context else _BASH_TAIL
        )
        for tc in result.tool_calls:
            if tc.name == "Bash" and tc.result:
                tc.result = _compress_bash_output(
                    tc.result,
                    hard_cap=bash_hard_cap,
                    head=bash_head,
                    tail=bash_tail,
                )
        output = result.response
        if not output:
            successful = sorted(
                tc.name for tc in result.tool_calls if not tc.is_error
            )
            output = (
                f"(no text output; completed {len(successful)} tool call(s): "
                f"{', '.join(successful)})"
            )
        # Capture the commands actually run via Bash this step from the tool-call
        # inputs — the executed-command record red-read keys on, distinct from the
        # assistant prose in ``output``. A ``Bash`` call always carries a
        # ``command`` string input; guard the get so a malformed call can't inject
        # a non-string.
        bash_commands = [
            cmd
            for tc in result.tool_calls
            if tc.name == "Bash"
            and isinstance(cmd := tc.input.get("command"), str)
            and cmd
        ]
        return StepResult(
            success=True, output=output, bash_commands=bash_commands,
        )

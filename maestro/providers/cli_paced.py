"""CLI-paced reference :class:`~maestro.providers.base.Provider` (proposal 056 Step 11 / 058 Phase 4).

Re-homes proposal 056's standalone ``[sdk] backend = "cli_paced"`` adapter as a
registered :class:`~maestro.providers.base.Provider`, so the human-paced
``claude``-CLI driver is selected through the same ``[sdk] provider`` registry as
the Anthropic and OpenAI providers. The retired ``maestro.sdk_backends`` package
delegated to this same adapter body during the transition; Step 11 folds the
adapter *into* this provider verbatim and deletes that package, so this module is
now the single home of the CLI-paced logic.

The provider does three jobs (the adapter's original contract, unchanged):

1. **Re-pack types.** :mod:`maestro_sdk` mirrors :mod:`maestro.sdk`'s shapes, so a
   :class:`maestro_sdk.QueryResult` becomes a :class:`maestro.sdk.QueryResult`
   field-for-field. The CLI surfaces no token counts *in-band*, but it writes a
   per-session transcript JSONL sidecar; proposal 061 Step 10 reads it for real
   metering (see the **Usage metering** note below). When the sidecar is missing,
   unreadable, or ambiguous the row falls back to a
   :class:`~maestro.sdk.QueryUsage` whose token counts are zero, whose
   ``total_cost_usd`` is ``None``, and which carries
   ``unmetered=True`` (proposal 058 Step 13) so ``cost_tracking`` and the dashboard
   treat the cost as *unknown* rather than measured-as-zero. The zeros are a
   dataclass-shape concession, not a faked measurement — the same shape
   :func:`maestro.sdk.query` returns on its no-``ResultMessage`` early-exit path.
2. **Translate errors.** A :class:`maestro_sdk.MaestroSDKError` carries a
   ``category`` *string* mirroring :class:`maestro.providers.base.ErrorCategory` by
   name; the provider re-packs it into a real :class:`~maestro.providers.base.SDKError`
   so resume-in-place keeps switching on the same taxonomy.
3. **Fail loud on what the CLI path cannot do.** Two categories of unsupported
   features:

   *Behaviour-shaping, raised as* :class:`NotImplementedError`: un-mappable SDK
   betas (every beta *except* long-context — see Step 7 below),
   ``output_format`` *on a session* (see Step 9 below — the one-shot ``query``
   path enforces it client-side), and SDK lifecycle ``hooks`` — except the one
   hooks payload the provider can replicate subprocess-natively: the task
   action-gate (``tool_gating.make_task_tool_hooks``) is recognised and
   re-expressed as the spawned CLI's own ``PreToolUse`` hook via an injected
   ``--settings`` payload plus a branch env var (proposal 066; see
   :mod:`maestro.providers.cli_paced_gate`). Silently honouring a JSON schema
   a follow-up turn can't enforce, or dropping a hook it can't replicate,
   would mislead the caller. Each raises with a remediation in
   :data:`_…_UNSUPPORTED` rather than being dropped.

   *Delegated to the CLI's own config* (silent drop is the contract, not a bug):
   ``model``, ``fallback_model``, ``task_budget_tokens``, and ``effort``. The ``claude`` CLI
   reads these from its own ``~/.claude.json`` / startup flags, so a Maestro-side
   override would either be inert (the CLI re-resolves them) or fight the operator's
   CLI config. Documented here so the silent drop is explicit, not implicit.
   Maestro's declared ``effort`` never reaches the child: effort and thinking follow
   the CLI's own effort/thinking settings, and no thinking budget is injected
   (proposal 140) — so the former 128K-budget callers no longer force thinking on
   under this provider.

   *Inert under this provider*: ``bypass_warning_throttle`` is a flag for the
   agent-SDK warning-throttle subsystem, which does not run in the CLI path — there
   is no throttle here to bypass — so the field is silently dropped on purpose.

**Transcript continuity via the persistent pty session (proposal 061 Steps 1-2).**
:class:`maestro_sdk.Session` keeps one pty runner alive across turns — reusing the
child on the :attr:`maestro_sdk.ClientMarkers.idle` redraw, respawning on EOF —
and a respawn reconnects via ``claude --resume <id>`` whenever a CLI-reported
session id has been captured, so a session handle is a continuing conversation
rather than single-turn-equivalent. The provider forwards ``resume_session_id`` /
``initial_prompt`` straight into :meth:`maestro_sdk.AsyncClient.create_session`
(the seed prompt is composed with the session's system prompt, mirroring the
per-turn prepend). Live continuity depends on the configured
:class:`maestro_sdk.ClientMarkers` — ``idle`` / ``session_id`` are per-CLI-version
layout policy, like the parser markers: under the default empty markers every
turn ends on EOF and no id is captured, so a respawn cannot ``--resume``.
Marker families are configured as a versioned :class:`maestro_sdk.MarkerPack`
via ``[sdk.cli_paced] marker_pack`` (proposal 075): :func:`configure_cli_paced`
stores the resolved pack and the client factory threads its families into every
:class:`maestro_sdk.AsyncClient`; no pack keeps the empty defaults above.

**In-stream early-exit via the incremental parser (proposal 061 Step 3).**
``complete_on`` no longer raises: :func:`_adapt_complete_on` re-shapes Maestro's
two-argument predicate ``(message, accumulated_text)`` onto
:attr:`maestro_sdk.QueryOptions.complete_on`'s text-only form by passing ``None``
for the message — the repainting TUI streams rendered text only, so no message
object exists on this path (every shipped predicate ignores the message argument
already). The driver evaluates it against ``parser.response`` after each snapshot
and tears the child down early on the first truthy result. Because a snapshot can
end mid-row, the adapter shows the predicate only complete rows (up to the last
``\n``) and trims an early-exit response to the prefix it fired on (#4112).

**External MCP forwarding (proposal 061 Step 5).** The ``claude`` CLI speaks MCP
natively, so ``options.mcp_servers`` no longer raises: the *external*,
stdio-shaped entries (``{command, args, env}``, as
:meth:`maestro.config.Config.mcp_servers_config` produces for Context7 /
Serena) are folded into a CLI ``--mcp-config`` payload — built by
:mod:`maestro_sdk.mcp_config` and passed inline as string-encoded JSON on the
spawn command, so no temp file is written. Maestro's *in-process* introspection /
messaging servers (SDK-instance specs a separate ``claude`` process cannot host)
are not forwardable that way — their cross-process launcher is proposal 061's
deferred rollout — so they are selected out; an in-process-only mapping yields no
``--mcp-config``. This is config generation, not transport; the ``remote_mcp``
capability flag vouches for it (proposal 061 Step 11).

**Tool-policy + ``max_turns`` forwarding (proposal 061 Step 6).** Tool policy and
``max_turns`` no longer raise — each reaches a mechanism the ``claude`` CLI already
exposes:

* *Tool policy* folds onto the CLI's own flags on the spawn command (built by
  :func:`_build_cli_command` alongside ``--mcp-config``): ``tools`` →
  ``--tools`` (an empty list emits ``--tools ""`` to disable all tools, the CLI's
  documented "no tools" form), ``allowed_tools`` → ``--allowedTools``, and
  ``disallowed_tools`` → ``--disallowedTools``. All three map (not just the
  allow/deny pair) so removing the old blanket rejection never *silently* drops a
  base-set ``tools`` request — a silent drop there could widen or narrow tool
  access without notice, the one thing the fail-loud doctrine forbids most here.
* ``max_turns`` has no CLI flag (the ``claude`` CLI exposes none), so it is
  enforced as a per-session **prompt-turn** cap: each
  :meth:`CliPacedProvider.query_on_session` call is one prompt-turn, and the
  ``N+1``-th turn on a ``max_turns=N`` session raises rather than running. This is
  a *different* notion of "turn" from the Anthropic path's in-query agentic-turn
  cap (the pty drives the CLI's internal agentic loop opaquely, so Maestro can
  only bound the turns *it* drives) — documented here so the divergence is
  explicit, not hidden. A one-shot :meth:`CliPacedProvider.query` is a single
  prompt-turn, so any ``max_turns >= 1`` is satisfied by construction and the
  one-shot path needs no counter.

Like external MCP forwarding, tool-policy forwarding is vouched for by the
capability flags (proposal 061 Step 11); ``max_turns`` is not a capability flag at all.

**Long-context beta via the CLI child environment (proposal 061 Step 7).** The
``claude`` CLI has no SDK-style ``betas`` argument, but it forwards arbitrary
request headers from its ``ANTHROPIC_CUSTOM_HEADERS`` environment variable. So a
requested :data:`maestro.sdk.LONG_CONTEXT_BETA` no longer raises: :func:`_beta_env`
maps it onto an ``anthropic-beta`` header set on the spawned child's environment
(merged over ``os.environ`` in :func:`_default_client_factory` so the child keeps
its ``PATH`` / API key). Any *other* requested beta has no CLI-side mechanism and
still raises — a silently dropped beta could change the model's behaviour. The env
override bakes into a session's spawn too, so every turn carries the same header.
The ``long_context`` capability flag vouches for it (proposal 061 Step 11).

**Thinking follows the CLI's own config (proposal 140).** Maestro supplies no
thinking-token budget: the spawned child follows the ``claude`` CLI's own thinking
config and its own effort setting (Maestro's declared ``effort`` is not
forwarded), so nothing
thinking-related enters the child environment. The fixed-budget env mirror proposal
061 Step 8 added was retired with the budget itself.

**Structured output via client-side validate-and-reask (proposal 061 Step 9).**
The ``claude`` CLI returns free-form text and cannot enforce a schema server-side,
so the one-shot :meth:`CliPacedProvider.query` path enforces ``options.output_format``
*client-side*: :func:`_augment_prompt_for_schema` appends the requested JSON Schema to
the prompt, :func:`_parse_and_validate` extracts the answer's JSON (reusing
:meth:`maestro.sdk.QueryResult.parse_json`) and validates it against the schema, and
:func:`_query_structured` re-asks **exactly once** on a parse/validation miss before
failing loud with :data:`_OUTPUT_FORMAT_VALIDATION_FAILED`. The validated object rides
back on :attr:`maestro.sdk.QueryResult.structured_output` so a caller's ``parse_json``
short-circuits to it. A *session* still raises :data:`_OUTPUT_FORMAT_SESSION_UNSUPPORTED`
(``query_on_session`` does no validation, so honouring a schema there would silently
yield ``None`` on follow-up turns). Enforcement is client-side — a documented caveat;
a malformed *schema* fails loud as a caller bug (it never triggers the re-ask, which
only ever fixes a bad *answer*), and operators needing a server-side guarantee use
``anthropic``. The ``structured_output`` capability flag vouches for it
(proposal 061 Step 11).

**Usage metering via the CLI transcript sidecar (proposal 061 Step 10).** The CLI
prints no token counts on its TUI, but it persists every session's transcript as a
JSONL sidecar at ``~/.claude/projects/<slug>/<session-id>.jsonl`` (``<slug>`` is the
launch cwd with every non-alphanumeric character replaced by ``-``). After a turn,
:func:`_read_sidecar_usage` locates that file via the CLI-reported
``QueryResult.session_id`` (captured in Step 2) and sums the per-iteration token
counts from each assistant ``message.usage`` block into a real
:class:`~maestro.sdk.QueryUsage` with ``unmetered=False``. This on-disk layout is the
CLI's own, **not** a stable public contract — so the path degrades to today's honest
``unmetered=True`` zero-fill (never a fabricated count) whenever the sidecar is
missing, unreadable, malformed, or carries no usage rows: a layout drift reads as
*unknown*, exactly today's behaviour, rather than a wrong number that would pollute
the burn-rate gate. The dollar figure (``total_cost_usd``) stays ``None`` — the
sidecar carries token counts, not a metered cost, and cost is derived downstream
from the counts. The ``usage_metering`` capability flag vouches for it
(proposal 061 Step 11).

**Stream-idle timeout: not inherited from `[sdk] stream_idle_timeout_seconds`.**
The agent path uses the operator's configured idle deadline (180s in the shipped
``maestro.toml``). The CLI path does **not** read that global. Instead it has its
own ``[sdk.cli_paced] stream_idle_timeout_seconds`` deadline (proposal 056 Step
12), applied at startup via :func:`configure_cli_paced`: when a caller passes no
per-call override, a CLI session falls back to that configured deadline, and —
absent the ``[sdk.cli_paced]`` table — to :class:`maestro_sdk.QueryOptions`'s
built-in 10-second default (18x shorter than the agent global). A per-call
``stream_idle_timeout_seconds`` on ``query`` / ``query_on_session`` still wins over
both. Do **not** silently inherit the agent-side global here — diverging defaults
are why this is configured separately rather than papered over.

**Cadence overrides.** The human-pacing rhythm — per-key delay, word/sentence
pauses, post-answer heartbeat, pty winsize jitter — comes from a
:class:`maestro_sdk.Cadence`. Operators tune individual knobs through the
``[sdk.cli_paced]`` table (keys in
:data:`maestro.config.VALID_CLI_PACED_CADENCE_KEYS`); :func:`configure_cli_paced`
stores them and the default client factory applies them over ``Cadence()``'s
defaults. Absent the table, the provider uses the documented ``Cadence()``
defaults unchanged.

The *system* prompt **is** honoured: :mod:`maestro_sdk` does not thread it into the
CLI invocation yet, so the provider prepends it to the user prompt — the same
discipline ``maestro/sdk.py`` documents for session callers.

**Lazy import posture.** This module imports :mod:`maestro_sdk` at module top. The
registry registers this provider behind a *lazy* factory
(:func:`maestro.providers.registry._cli_paced_factory`), so this module — and
therefore ``maestro_sdk`` — loads only when an operator selects or per-phase-routes
``cli_paced``; the default ``anthropic`` path never pays for it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import jsonschema
from maestro_sdk.mcp_config import build_mcp_config, serialize_mcp_config

from maestro.cost_tracking import PlanHeadroom, log_metering_divergence
from maestro.diagnostics import ContextWindowSnapshot
from maestro.providers.base import (
    Capabilities,
    ErrorCategory,
    QueryOptions,
    QueryResult,
    QueryUsage,
    SDKError,
    Session,
    ToolCall,
)
from maestro.providers.cli_paced_gate import (
    allowed_branches_to_env,
    current_branch_to_env,
    push_remotes_to_env,
    serialize_gate_settings,
    strip_redirections_to_env,
)
from maestro.sdk import (
    LONG_CONTEXT_BETA,
    _coerce_int,
    _truncate,
    open_session_logger,
)
from maestro.tool_gating import task_gate_from_hooks
from maestro.tool_surface import deny_ungated_shell
from maestro.types import TranscriptTurnKind
from maestro_sdk import (
    DEFAULT_PROBE_MARKERS,
    AsyncClient,
    Cadence,
    ContextReport,
    CostReport,
    MaestroSDKError,
    MarkerPack,
    SlashProbeMarkers,
    UsageReport,
    detect_cli_version,
    get_marker_pack,
    parse_context,
    parse_cost,
    parse_usage,
    select_marker_pack,
)
from maestro_sdk import QueryOptions as CLIQueryOptions
from maestro_sdk import QueryResult as CLIQueryResult
from maestro_sdk import Session as CLISession

_log = logging.getLogger("maestro.providers.cli_paced")

# Remediation messages for the features the CLI path cannot honour in this
# slice. Each is raised (never logged-and-dropped) so the gap is loud.
_BETAS_UNSUPPORTED = (
    "CLI-paced provider can only map the long-context SDK beta to the claude "
    "CLI child environment; other SDK betas have no CLI-side mechanism and are "
    "agent-only"
)
_OUTPUT_FORMAT_SESSION_UNSUPPORTED = (
    "CLI-paced provider enforces a structured output_format only on the one-shot "
    "query path (client-side validate-and-reask); query_on_session does no "
    "validation, so a session's structured_output would silently be None — use "
    "query() for structured output"
)
_OUTPUT_FORMAT_VALIDATION_FAILED = (
    "CLI-paced provider could not obtain output conforming to the requested "
    "JSON schema after one re-ask; the CLI enforces structured output "
    "client-side, so a model that fails twice fails loud — use the anthropic "
    "provider for a server-side schema guarantee"
)
_MAX_TURNS_EXCEEDED = (
    "CLI-paced session reached its max_turns cap ({n}); the session stops "
    "serving turns — open a new session to continue"
)
_HOOKS_UNSUPPORTED = (
    "CLI-paced provider cannot enforce SDK lifecycle hooks "
    "(in-process callbacks cannot reach a separate claude CLI subprocess). "
    "The one exception is the task action-gate built by "
    "tool_gating.make_task_tool_hooks, which the provider replicates onto "
    "the spawned CLI via an injected --settings PreToolUse hook (proposal "
    "066); any other hooks payload fails loud so a silent drop cannot "
    "widen tool access"
)

# String → enum lookup for error translation. Every maestro_sdk category name
# is a valid ErrorCategory value; an unknown one maps to UNKNOWN rather than
# raising, so an opaque failure stays opaque (never mis-bucketed as retryable).
_CATEGORY_BY_NAME: dict[str, ErrorCategory] = {c.value: c for c in ErrorCategory}

# Base spawn command, mirroring maestro_sdk's AsyncClient default. External MCP
# forwarding (proposal 061 Step 5) appends ``--mcp-config <payload>`` to it, and
# the always-present ungated-shell ``--disallowedTools`` denial (issue #4381)
# means the provider always passes an explicit command built on this base.
_BASE_COMMAND: tuple[str, ...] = ("claude",)

# The CLI flag that loads MCP servers. The ``claude`` CLI accepts a string-encoded
# JSON payload here (not only a file path), so the provider passes the serialised
# ``{"mcpServers": {...}}`` inline — no temp file, which keeps the path within the
# no-real-IO unit-test discipline.
_MCP_CONFIG_FLAG = "--mcp-config"

# Tool-policy flags the CLI exposes (proposal 061 Step 6). ``tools`` is the base
# set (``--tools ""`` disables all tools), ``allowed_tools`` / ``disallowed_tools``
# filter it. The provider folds each onto its CLI flag on the spawn command,
# alongside ``--mcp-config``, rather than into the projected ``CLIQueryOptions``.
_TOOLS_FLAG = "--tools"
_ALLOWED_TOOLS_FLAG = "--allowedTools"
_DISALLOWED_TOOLS_FLAG = "--disallowedTools"

# Action-gate replication (proposal 066). When ``options.hooks`` is exactly the
# task action-gate the executor wires in-process
# (``tool_gating.make_task_tool_hooks``), the provider injects an equivalent
# gate into the spawned ``claude`` itself: an inline string-encoded settings
# payload on ``--settings`` (the CLI accepts inline JSON here, like
# ``--mcp-config`` — no on-disk settings file) registering the hidden
# ``maestro _gate-bash-hook`` subcommand as the CLI's own ``PreToolUse`` Bash
# hook, plus the run's owned-branch set on the child env (JSON-encoded in
# ``GATE_BRANCH_ENV``). Both paths delegate to the same
# ``tool_gating.evaluate_bash_command`` core, so they cannot drift. Any
# *other* hooks payload still fails loud (:data:`_HOOKS_UNSUPPORTED`).
_SETTINGS_FLAG = "--settings"

# Long-context beta via the CLI child's environment (proposal 061 Step 7). The
# ``claude`` CLI has no SDK-style ``betas`` argument, but it forwards arbitrary
# request headers from ``ANTHROPIC_CUSTOM_HEADERS`` (``Name: Value`` form,
# https://code.claude.com/docs/en/env-vars). Maestro maps the one beta it knows
# how to reach that way — the 1M-context window
# (:data:`maestro.sdk.LONG_CONTEXT_BETA`) — onto the ``anthropic-beta`` header.
# Any *other* requested beta has no CLI-side mechanism and fails loud rather than
# being silently dropped (a dropped beta could silently change the model's
# behaviour). The ``long_context`` capability flag vouches for this mechanism.
_CUSTOM_HEADERS_ENV = "ANTHROPIC_CUSTOM_HEADERS"
_ANTHROPIC_BETA_HEADER = "anthropic-beta"
_SUPPORTED_BETAS: frozenset[str] = frozenset({LONG_CONTEXT_BETA})

# Slash-command metrics probe (proposal 062 Step 3). The ``claude`` TUI answers
# ``/cost`` / ``/usage`` / ``/context`` with live session metrics that live in a
# repainting TUI region, not the answer region; :meth:`CLISession.run_command`
# scrapes the redraw and :mod:`maestro_sdk.slash_probe` parses it. The provider
# issues the whole command set on a cadence hit and attaches the parsed reports
# to a provider-held slot on the session handle (:attr:`_CLISession.last_probe`),
# never to the shared :class:`~maestro.sdk.QueryResult` — folding ``maestro_sdk``
# report types into core ``sdk.py`` would break this module's lazy-import posture
# (the default ``anthropic`` path must never pay for ``maestro_sdk``). The two
# operator knobs land as module globals here (mirroring the ``_client_factory`` /
# ``_stream_idle_timeout_seconds`` seams), wired from the validated
# ``[sdk.cli_paced]`` config via :func:`configure_cli_paced` (proposal 062
# Step 6). Default-off, so today's behaviour is byte-for-byte unchanged until an
# operator opts in.
#
# Resident-runner caveat: ``run_command`` probes the ``claude`` child the session
# keeps resident, but that child only survives between turns under an *idle
# marker*. Without a configured ``[sdk.cli_paced] marker_pack`` (proposal 075)
# carrying a non-empty ``ClientMarkers.idle``, ``cli_paced`` builds its
# ``AsyncClient`` with the empty ``DEFAULT_CLIENT_MARKERS`` (EOF is the only
# done-signal), so ``maestro_sdk`` reaps the child at the end of every turn and
# the next ``query`` respawns. With no resident child, ``run_command`` raises a
# plain ``RuntimeError`` ("no live runner") — so an *enabled* probe fails loud
# every cadence hit until a pack with a working idle marker is registered and
# selected (the registry currently ships none — see docs/sdk-internals.md on the
# claude 2.1.x layout findings). Default-off keeps it inert in the meantime.
_COST_COMMAND = "/cost"
_USAGE_COMMAND = "/usage"
_CONTEXT_COMMAND = "/context"


@dataclass
class ProbeReports:
    """Parsed slash-command metrics from one probe pass (proposal 062 Step 3).

    Holds the three reports a probe scrapes off the live session — ``/cost``,
    ``/usage``, ``/context`` — each ``| None`` because a given CLI layout may
    surface some lines and not others, and an unconfigured / non-matching marker
    pack honestly degrades each to ``None`` rather than a fabricated zero (the
    same fail-soft discipline :mod:`maestro_sdk.slash_probe` enforces). An
    all-``None`` ``ProbeReports`` is the honest "probed, read nothing" state, not
    "measured zero."
    """

    cost: CostReport | None = None
    usage: UsageReport | None = None
    context: ContextReport | None = None


# Usage metering via the CLI transcript sidecar (proposal 061 Step 10). The
# ``claude`` CLI persists each session's transcript as JSONL under
# ``~/.claude/projects/<slug>/<session-id>.jsonl``, where ``<slug>`` is the launch
# cwd with every non-alphanumeric character replaced by ``-`` (so
# ``/home/u/p`` → ``-home-u-p``). This on-disk layout is the CLI's own and not a
# stable public contract, so the reader degrades to honest ``unmetered=True``
# (never a fabricated count) whenever the path is missing/unreadable/ambiguous.
_PROJECTS_SUBPATH: tuple[str, ...] = (".claude", "projects")
_TRANSCRIPT_SLUG_RE = re.compile(r"[^a-zA-Z0-9]")
# Assistant ``message.usage`` keys summed into the metered QueryUsage, mapped to
# the QueryUsage field they land in. The CLI mirrors the Anthropic token-count
# names, so this is the same vocabulary :func:`maestro.sdk._finalize_usage` reads.
_SIDECAR_TOKEN_FIELDS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_creation_input_tokens", "cache_creation_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
)


@dataclass(frozen=True)
class _SidecarUsage:
    """Token counts (and the reported model) recovered from a transcript sidecar.

    Summed across the turn's assistant ``message.usage`` blocks by
    :func:`_read_sidecar_usage`; ``model`` is the last assistant row's reported
    model (``None`` when no row carried one). Consumed by :func:`_to_query_result`
    to build a metered :class:`~maestro.sdk.QueryUsage`.
    """

    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    model: str | None


def _transcript_path(session_id: str, cwd: str) -> Path:
    """Locate the CLI transcript sidecar for *session_id* launched in *cwd*."""
    slug = _TRANSCRIPT_SLUG_RE.sub("-", cwd)
    return Path.home().joinpath(*_PROJECTS_SUBPATH, slug, f"{session_id}.jsonl")


def _load_sidecar_rows(session_id: str | None, cwd: str) -> list[dict[str, Any]] | None:
    """Load the CLI transcript sidecar for *session_id* (launched in *cwd*).

    Returns its JSON-object rows in file order, or ``None`` for every ambiguous
    case: no session id, an unreadable or undecodable file, or any malformed
    JSON line. The narrow ``OSError`` / ``UnicodeDecodeError`` /
    ``json.JSONDecodeError`` returns surface the failure structurally (a
    ``None`` the caller branches on), not a silenced warning — an unreadable
    sidecar is *unknown*, never an empty transcript. Non-object rows are dropped.
    """
    if not session_id:
        return None
    try:
        raw = _transcript_path(session_id, cwd).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _sidecar_usage_from_rows(
    rows: Sequence[Mapping[str, Any]] | None,
) -> _SidecarUsage | None:
    """Sum assistant ``message.usage`` blocks over loaded sidecar *rows*.

    ``None`` when *rows* is ``None`` (unreadable sidecar) or no assistant row
    carries usage — never a fabricated zero count.
    """
    if rows is None:
        return None
    totals: dict[str, int] = {field: 0 for _, field in _SIDECAR_TOKEN_FIELDS}
    model: str | None = None
    saw_usage = False
    for row in rows:
        if row.get("type") != "assistant":
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        saw_usage = True
        for src_key, field in _SIDECAR_TOKEN_FIELDS:
            totals[field] += _coerce_int(usage.get(src_key))
        row_model = message.get("model")
        if isinstance(row_model, str):
            model = row_model
    if not saw_usage:
        return None
    return _SidecarUsage(
        input_tokens=totals["input_tokens"],
        output_tokens=totals["output_tokens"],
        cache_creation_tokens=totals["cache_creation_tokens"],
        cache_read_tokens=totals["cache_read_tokens"],
        model=model,
    )


def _read_sidecar_usage(session_id: str | None, cwd: str) -> _SidecarUsage | None:
    """Sum per-turn token usage from the CLI transcript sidecar (Step 10).

    Returns a :class:`_SidecarUsage` whose token counts are summed across every
    assistant ``message.usage`` block in the sidecar for *session_id* (launched in
    *cwd*), or ``None`` for every ambiguous case the caller degrades to honest
    ``unmetered=True`` on: no session id, no readable file, a malformed JSON line,
    or a file with no assistant usage rows (see :func:`_load_sidecar_rows`). It
    never fabricates a count — an unreadable sidecar is *unknown*, not
    measured-as-zero.

    A one-shot ``query`` spawns a fresh ``claude`` process whose transcript is this
    one turn's agentic loop, so summing the per-iteration usage blocks yields the
    turn's total token consumption (the cost basis).
    """
    return _sidecar_usage_from_rows(_load_sidecar_rows(session_id, cwd))


def _tool_result_text(content: Any) -> str | None:
    """Flatten a ``tool_result`` block's content to text, or ``None`` if unshaped."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return None


def _sidecar_tool_turns(
    rows: Sequence[Mapping[str, Any]],
) -> list[tuple[TranscriptTurnKind, str, str]]:
    """Recover ``tool_call`` / ``tool_result`` turns from CLI transcript *rows*.

    Emits exactly the content shapes ``sdk._process_content_blocks`` writes —
    ``"<name> input=<input>"`` for a call, an ``"ERROR "`` prefix on an errored
    result — so :mod:`maestro.tool_surface` parses both providers' sidecars the
    same way (proposal 142). A block without a string ``name``, or a message
    whose ``content`` is not a list, is skipped rather than guessed at.
    """
    turns: list[tuple[TranscriptTurnKind, str, str]] = []
    for row in rows:
        row_type = row.get("type")
        if row_type not in ("assistant", "user"):
            continue
        message = row.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if row_type == "assistant" and block.get("type") == "tool_use":
                name = block.get("name")
                if isinstance(name, str):
                    turns.append(
                        ("tool_call", "assistant", f"{name} input={block.get('input')}")
                    )
            elif row_type == "user" and block.get("type") == "tool_result":
                text = _tool_result_text(block.get("content"))
                if text is not None:
                    prefix = "ERROR " if block.get("is_error") else ""
                    turns.append(("tool_result", "tool", f"{prefix}{text}"))
    return turns


def _record_transcript(
    caller: str | None,
    prompt: str,
    response: str,
    rows: Sequence[Mapping[str, Any]] | None,
) -> None:
    """Write a Maestro transcript sidecar for one completed ``cli_paced`` turn.

    Makes ``cli_paced`` sessions visible to ``maestro tool-surface-audit``
    (proposal 142): the caller's own *prompt* (not the system-prefixed form,
    matching the ``anthropic`` path), the tool turns recovered from this turn's
    CLI transcript *rows* (none when unreadable), then the final text. The
    advertised surface stays ``None`` — the interactive CLI emits no init
    message — so the sensor counts it advertised-unknown. A no-op unless
    :func:`maestro.sdk.configure_sdk_logging` ran; a turn that raised records
    nothing, since no session id means no CLI transcript to recover. The
    turn has already completed, so a log directory that cannot be opened
    (``OSError``) drops the sidecar at debug rather than masking the result.
    """
    try:
        session_log = open_session_logger(caller)
    except OSError as exc:
        _log.debug("cli_paced transcript sidecar not written: %s", exc)
        return
    if session_log is None:
        return
    try:
        session_log.log(f"Prompt: {_truncate(prompt, 1000)}")
        session_log.add_turn("prompt", "user", prompt)
        for kind, role, content in _sidecar_tool_turns(rows or []):
            session_log.add_turn(kind, role, content)
        session_log.add_turn("text", "assistant", response)
    finally:
        session_log.close()


@dataclass
class _CLISession:
    """Opaque session handle for the CLI-paced provider.

    Wraps a :class:`maestro_sdk.Session` together with the options and system
    prompt captured at :meth:`CliPacedProvider.create_session` time.
    :class:`maestro_sdk.Session` *replaces* (does not merge) per-call options, so
    the handle carries the create-time ``options`` to let a follow-up apply a
    ``stream_idle_timeout`` override without dropping ``cwd``, and carries
    ``system`` so every follow-up turn re-prepends the session's baseline system
    prompt.

    ``turn_count`` tracks how many :meth:`CliPacedProvider.query_on_session` calls
    this handle has served, driving the ``max_turns`` cap and the slash-probe
    cadence. The counter is incremented *before* the underlying ``Session.query``
    await, so a failed turn still counts as a served prompt-turn.

    ``max_turns`` is the per-session prompt-turn cap (proposal 061 Step 6),
    captured from ``options.max_turns`` at create time. ``None`` (the default)
    leaves the session uncapped; a set cap makes the ``N+1``-th
    :meth:`CliPacedProvider.query_on_session` raise rather than run, so the
    session serves exactly ``N`` turns. Checked *before* ``turn_count`` is
    incremented, so an over-cap call has no side effect (no counter bump, no
    query).
    """

    session: CLISession
    options: CLIQueryOptions
    system: str | None = None
    turn_count: int = 0
    max_turns: int | None = None
    #: Most recent slash-command metrics probe (proposal 062 Step 3), or ``None``
    #: until a probe runs. :meth:`CliPacedProvider.query_on_session` populates it
    #: on a cadence hit when ``_probe_metrics_enabled``; a default-off session
    #: leaves it ``None`` and issues no ``run_command``.
    last_probe: ProbeReports | None = None
    #: Whether any turn on this handle returned a CLI-reported session id
    #: (proposal 075 Step 5). Once ``True`` the session-id drift sentinel stays
    #: quiet for this session — the configured marker demonstrably matches.
    session_id_seen: bool = False
    #: Tool-turn cursor into the CLI transcript (proposal 142): the session id
    #: whose transcript was last read, and how many of its rows earlier turns
    #: already recorded, so each turn's Maestro sidecar carries only its own
    #: tool rows. A changed id (respawn without resume) restarts from row 0.
    transcript_session_id: str | None = None
    transcript_rows_seen: int = 0


# cli_paced provider config (proposal 056 Step 12), set once at startup from the
# ``[sdk.cli_paced]`` table via :func:`configure_cli_paced`. Empty / ``None`` until
# configured, so an unconfigured provider — tests, and any deployment that did not
# select cli_paced — uses ``maestro_sdk``'s documented defaults: ``Cadence()`` and
# ``QueryOptions``'s built-in 10-second idle deadline.
_cadence_overrides: dict[str, float] = {}
_stream_idle_timeout_seconds: float | None = None

#: Freshest realized context-window composition (proposal 062 Step 4), or ``None``
#: until a cadence-hit probe with a parseable ``/context`` reading runs. Held as a
#: process-global — not provider-instance state — so :meth:`CliPacedProvider`
#: keeps its documented stateless contract while
#: :meth:`CliPacedProvider.latest_context_window` still exposes the latest reading
#: across sessions. :func:`configure_cli_paced` clears it so a reconfigured process
#: starts from an honest "unmeasured".
_last_context_window: ContextWindowSnapshot | None = None

#: Freshest subscription plan rate-limit headroom (proposal 062 Step 5), or
#: ``None`` until a cadence-hit probe with a parseable ``/usage`` reading runs.
#: The same process-global posture as :data:`_last_context_window`:
#: :meth:`CliPacedProvider.latest_plan_headroom` exposes it read-only, and
#: :func:`configure_cli_paced` clears it on reconfiguration.
_last_plan_headroom: PlanHeadroom | None = None

# Slash-command metrics probe knobs (proposal 062 Step 3). Default-off, every-turn
# cadence, and a matches-nothing marker pack — the honest-degenerate defaults, so a
# provider that no operator opted in issues no probe and an opted-in one with no
# version-specific marker pack parses every redraw to ``None`` rather than a
# fabricated count. The validated ``[sdk.cli_paced]`` ``probe_metrics_enabled`` /
# ``probe_metrics_every`` config wires into the first two via
# :func:`configure_cli_paced` (proposal 062 Step 6); ``_probe_markers`` stays a
# module-level seam (the layout pack is per-CLI-version, not a TOML knob) that
# tests and a future real pack override.
_probe_metrics_enabled: bool = False
_probe_metrics_every: int = 1
_probe_markers: SlashProbeMarkers = DEFAULT_PROBE_MARKERS

#: Configured per-CLI-version marker pack (proposal 075 Step 3), or ``None`` for
#: the empty defaults (EOF-only turns, no id capture — today's behaviour). Set
#: by :func:`configure_cli_paced` from the resolved ``[sdk.cli_paced]``
#: ``marker_pack`` config; ``_default_client_factory`` threads its four marker
#: families into :class:`maestro_sdk.AsyncClient`, and its probe family replaces
#: the :data:`_probe_markers` seam.
_marker_pack: MarkerPack | None = None

# Layout-drift sentinel latches (proposal 075 Step 5): one-shot per process so
# a mismatched pack reads as one loud, pack-named WARNING per drift category,
# not a per-turn log storm. Reset by configure_cli_paced so a reconfigured
# process re-arms the sentinels for its (possibly different) pack.
_idle_drift_warned: bool = False
_session_id_drift_warned: bool = False

# How many session turns a configured session_id marker may go unmatched before
# the drift sentinel fires: long enough that an id printed late in the first
# exchanges still counts, short enough that a whole session rarely runs blind.
_SESSION_ID_DRIFT_TURNS = 3


def configure_cli_paced(
    *,
    cadence_overrides: dict[str, float] | None = None,
    stream_idle_timeout_seconds: float | None = None,
    probe_metrics_enabled: bool = False,
    probe_metrics_every: int = 1,
    marker_pack: MarkerPack | None = None,
) -> None:
    """Apply ``[sdk.cli_paced]`` overrides to the provider (startup-time).

    *cadence_overrides* maps :class:`maestro_sdk.Cadence` field names to numeric
    overrides; an absent key keeps its ``Cadence`` default. *stream_idle_timeout_seconds*
    becomes the CLI-path idle deadline a caller inherits when it passes no per-call
    override; ``None`` falls back to ``maestro_sdk``'s built-in ``QueryOptions``
    default. *probe_metrics_enabled* / *probe_metrics_every* arm the slash-command
    metrics probe and its turn cadence (proposal 062 Step 6); the defaults keep
    probing off, so a caller omitting them — and every deployment whose
    ``[sdk.cli_paced]`` table lacks the keys — runs today's no-probe behaviour.
    *marker_pack* is the resolved per-CLI-version layout pack (proposal 075):
    when set, the client factory builds every :class:`maestro_sdk.AsyncClient`
    with the pack's client/parser/approval marker families and the probe parses
    redraws through the pack's :class:`maestro_sdk.SlashProbeMarkers`; ``None``
    (the default, and the resolution of ``marker_pack = "none"`` or an ``"auto"``
    that covered nothing) keeps every empty-marker default byte-for-byte.
    Validation lives in :meth:`maestro.config.Config.validate`; this seam
    only stores. Called from ``cli.main`` *only* when cli_paced is the selected (or a
    per-phase) provider, so the default ``anthropic`` path never reaches it.
    """
    global _cadence_overrides, _stream_idle_timeout_seconds, _last_context_window
    global _last_plan_headroom, _probe_metrics_enabled, _probe_metrics_every
    global _probe_markers, _marker_pack, _idle_drift_warned, _session_id_drift_warned
    _cadence_overrides = dict(cadence_overrides) if cadence_overrides else {}
    _stream_idle_timeout_seconds = stream_idle_timeout_seconds
    _probe_metrics_enabled = probe_metrics_enabled
    _probe_metrics_every = probe_metrics_every
    _marker_pack = marker_pack
    _probe_markers = marker_pack.probe if marker_pack is not None else DEFAULT_PROBE_MARKERS
    # Startup also clears any cached probe readings so a reconfigured process
    # reports an honest "unmeasured" until its next probe, and re-arms the
    # one-shot layout-drift sentinels for the (possibly different) pack.
    _last_context_window = None
    _last_plan_headroom = None
    _idle_drift_warned = False
    _session_id_drift_warned = False


def resolve_marker_pack(setting: str) -> MarkerPack | None:
    """Resolve the validated ``[sdk.cli_paced] marker_pack`` setting to a pack.

    ``"none"`` (the default) resolves to ``None`` — today's empty-marker
    behaviour. ``"auto"`` probes ``claude --version``
    (:func:`maestro_sdk.detect_cli_version`) and selects the newest registered
    pack covering the detected version; when nothing covers it (or the probe
    failed), it logs one WARNING naming the detected version and resolves to
    ``None``, so an unknown CLI layout reads as a loud, attributable
    degradation rather than a silent return to EOF-only turns. A *named*
    setting resolves through the registry and raises on an unknown name —
    :meth:`maestro.config.Config.validate` already gates this, so the raise is
    the backstop for a registry/config drift between validation and startup,
    never a path that silently means "no markers". Called from ``cli.main``
    inside the cli_paced-selected branch only.
    """
    if setting == "none":
        return None
    if setting == "auto":
        version = detect_cli_version()
        pack = select_marker_pack(version)
        if pack is None:
            _log.warning(
                '[sdk.cli_paced] marker_pack = "auto": no registered marker '
                "pack covers claude CLI version %s; proceeding with the empty "
                "default markers (EOF-only turns, no session-id capture)",
                version if version is not None else "<undetected>",
            )
        return pack
    pack = get_marker_pack(setting)
    if pack is None:
        raise ValueError(
            f"[sdk.cli_paced] marker_pack names unknown pack {setting!r} "
            "(registry changed since config validation?)"
        )
    return pack


def _default_client_factory(
    options: CLIQueryOptions,
    command: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> AsyncClient:
    """Build a default human-paced client. Overridable in tests via
    :data:`_client_factory`.

    Applies the configured ``[sdk.cli_paced]`` Cadence overrides over
    ``Cadence()``'s defaults (:func:`dataclasses.replace`, which re-runs
    ``__post_init__`` so the new instance gets a fresh RNG). An empty override map
    leaves the documented defaults untouched. ``command`` overrides the client's
    default spawn command — the external-MCP path (proposal 061 Step 5) passes
    ``["claude", "--mcp-config", <payload>]``; ``None`` keeps ``AsyncClient``'s
    own default command.

    ``env`` is the long-context-beta override (proposal 061 Step 7): a non-``None``
    mapping is **merged over** ``os.environ`` before reaching the child, because a
    bare ``env`` dict *replaces* the subprocess environment wholesale (so passing
    only the override would strip ``PATH`` / ``ANTHROPIC_API_KEY`` from the spawned
    ``claude``). ``None`` leaves ``AsyncClient``'s default (the child inherits this
    process's environment unchanged), so the non-beta path is untouched.

    Precedence: the override wins key-by-key, so a beta-carrying
    ``ANTHROPIC_CUSTOM_HEADERS`` *replaces* any operator-set value of the same var
    for the spawned child (the merge is last-wins, not header-appending). That is
    intentional — Maestro reaches the long-context beta through exactly this var,
    and an operator who also needs custom headers on the CLI child would set them
    via the same mechanism. The non-beta path passes ``env=None`` and so never
    touches an inherited ``ANTHROPIC_CUSTOM_HEADERS``.
    """
    # ``replace`` type-checks ``**kwargs`` against every Cadence field (incl. the
    # tuple/int/RNG ones), so the float-only override dict needs a cast. The keys
    # are runtime-safe: Config.validate() gates them to VALID_CLI_PACED_CADENCE_KEYS,
    # which a guard test pins ⊆ Cadence's fields, and every value is a finite number.
    if _cadence_overrides:
        cadence = replace(Cadence(), **cast("dict[str, Any]", _cadence_overrides))
    else:
        cadence = Cadence()
    kwargs: dict[str, Any] = {"options": options}
    if command is not None:
        kwargs["command"] = command
    if env is not None:
        kwargs["env"] = {**os.environ, **env}
    # Marker-pack threading (proposal 075 Step 3): a configured pack supplies
    # the client/parser/approval layout families; with none configured the
    # kwargs stay byte-for-byte what they were before this seam existed, so the
    # empty-marker defaults (EOF-only turns) keep applying by omission.
    if _marker_pack is not None:
        kwargs["client_markers"] = _marker_pack.client
        kwargs["parser_markers"] = _marker_pack.parser
        kwargs["approval_markers"] = _marker_pack.approvals
    return AsyncClient(cadence, **kwargs)


#: Seam for tests to inject a fake client without spawning a pty.
_client_factory: Callable[
    [CLIQueryOptions, Sequence[str] | None, Mapping[str, str] | None], AsyncClient
] = _default_client_factory


def _reject_unsupported_options(options: QueryOptions | None) -> None:
    """Raise for option fields the CLI path cannot honour.

    Covers the *behaviour-shaping* features listed in the module docstring.
    Fields listed there as "delegated to the CLI's own config" (``model``,
    ``fallback_model``, ``task_budget_tokens``) are intentionally **not** rejected
    here — the CLI re-resolves them from its own config — so this function should not
    grow a branch for them. ``effort`` is likewise not rejected but dropped: effort and
    thinking follow the CLI's own settings, and no Maestro-supplied budget is injected.
    Tool policy (``tools`` / ``allowed_tools`` /
    ``disallowed_tools``) and ``max_turns`` are also **not** rejected: tool
    policy folds onto CLI flags in :func:`_build_cli_command` and ``max_turns``
    is enforced as a per-session prompt-turn cap (proposal 061 Step 6).
    ``betas`` is no longer rejected here either: the long-context beta maps onto
    the CLI child's environment in :func:`_beta_env` (proposal 061 Step 7), which
    raises for any *un-mappable* beta — so this function leaves ``betas`` to it.
    ``output_format`` is no longer rejected universally either: the one-shot
    ``query`` path enforces it client-side via :func:`_query_structured` (proposal
    061 Step 9), so the *session* path rejects it separately (in
    :meth:`CliPacedProvider.create_session`) rather than here. ``hooks`` stays the
    one field this shared check still rejects — with a single carve-out
    (proposal 066): the task action-gate mapping recognised by
    :func:`maestro.tool_gating.task_gate_from_hooks` is *consumed* (replicated
    onto the spawned CLI via :func:`_gate_settings_flags` / :func:`_gate_env`)
    rather than rejected. Anything else under ``hooks`` — including a mapping
    that merely contains the gate — still fails loud.
    """
    if options is None:
        return
    if options.hooks is not None and task_gate_from_hooks(options.hooks) is None:
        raise NotImplementedError(_HOOKS_UNSUPPORTED)


def _beta_env(options: QueryOptions | None) -> dict[str, str]:
    """Map requested SDK betas onto the CLI child's environment (Step 7).

    The ``claude`` CLI has no ``betas`` argument, so the only beta Maestro can
    reach is one expressible as a forwarded request header via
    ``ANTHROPIC_CUSTOM_HEADERS``. Returns ``{_CUSTOM_HEADERS_ENV: "anthropic-beta:
    <csv>"}`` when *options* requests the long-context beta (the lone member of
    :data:`_SUPPORTED_BETAS`), or ``{}`` when no betas are requested. A requested
    beta outside :data:`_SUPPORTED_BETAS` has no CLI-side mechanism and raises
    :class:`NotImplementedError` rather than being silently dropped — a dropped
    beta could quietly change the model's behaviour, the one thing the fail-loud
    doctrine forbids most. The header value joins the requested betas with
    ``", "`` (the API's multi-beta form) so the mechanism generalises if a second
    mappable beta is ever added.
    """
    if options is None or not options.betas:
        return {}
    if any(beta not in _SUPPORTED_BETAS for beta in options.betas):
        raise NotImplementedError(_BETAS_UNSUPPORTED)
    header = f"{_ANTHROPIC_BETA_HEADER}: {', '.join(options.betas)}"
    return {_CUSTOM_HEADERS_ENV: header}


def _child_env(options: QueryOptions | None) -> dict[str, str]:
    """Merge every CLI-child environment override into one dict (Step 7, 066).

    Unions the long-context-beta header (:func:`_beta_env`) and the action-gate
    branch (:func:`_gate_env`, proposal 066); the two contribute disjoint keys
    (``ANTHROPIC_CUSTOM_HEADERS`` / ``MAESTRO_GATE_ALLOWED_BRANCH``), so neither
    clobbers the other. No thinking budget is injected (proposal 140).
    :func:`_beta_env` raises for an un-mappable beta, so that fail-loud path
    propagates through here unchanged. Returns ``{}`` when no override applies, so the
    callers' ``_child_env(options) or None`` keeps the non-override path at ``env=None``
    (the child inherits this process's environment untouched).
    """
    env = _beta_env(options)
    env.update(_gate_env(options))
    return env


class _AdaptedCompleteOn:
    """Maestro's two-arg predicate re-shaped onto the CLI driver's text-only form.

    ``predicate`` is what the driver runs; ``fired_prefix`` records the
    complete-row prefix the predicate last fired on (``None`` until it fires), so
    the provider can trim the early-exit response to the text actually judged.
    """

    def __init__(self, complete_on: Callable[[Any, str], bool]) -> None:
        self._complete_on = complete_on
        self.fired_prefix: str | None = None

    def predicate(self, accumulated_text: str) -> bool:
        prefix = accumulated_text[: accumulated_text.rfind("\n") + 1]
        assert prefix == "" or prefix.endswith("\n")
        fired = bool(self._complete_on(None, prefix))
        if fired:
            self.fired_prefix = prefix
        return fired

    def trim(self, result: CLIQueryResult) -> CLIQueryResult:
        """Cut an early-exit *result* back to the prefix the predicate fired on.

        A predicate that never fired, or a response that no longer starts with
        the judged prefix, leaves *result* untouched.
        """
        prefix = self.fired_prefix
        if prefix is None or not result.response.startswith(prefix):
            return result
        return replace(result, response=prefix)


def _adapt_complete_on(
    complete_on: Callable[[Any, str], bool] | None,
) -> _AdaptedCompleteOn | None:
    """Re-shape Maestro's two-arg predicate onto the CLI driver's text-only form.

    Maestro's ``complete_on`` receives ``(message, accumulated_text)``; the CLI
    driver streams rendered text with no message object, so the adapted predicate
    passes ``None`` for the message (proposal 061 Step 3). Every shipped predicate
    (``json_complete``, ``chars_complete``, callers' local ones) already ignores
    the message argument.

    Complete-row contract (#4112): the Anthropic path evaluates predicates once
    per whole message, so every line they see is finished; the driver instead
    runs a forwarded predicate after every pty snapshot, against partial,
    mid-render text. The adapter therefore hides the row still being drawn —
    the predicate sees only the prefix up to and including the last ``\n`` —
    so a marker check like ``LOSS CONDITION:`` cannot fire on a bare prefix.
    Because the parser strips trailing whitespace, the *final* row never carries
    a ``\n`` either, so a predicate that needs the last row (markers-last
    reflection, a single-line JSON answer) never early-exits on this path — the
    turn simply runs to completion. That latency cost is deliberate; do not
    widen the prefix to regain it.
    ``None`` passes through so the no-predicate path stays inert.
    """
    if complete_on is None:
        return None
    return _AdaptedCompleteOn(complete_on)


def _to_cli_options(
    options: QueryOptions | None, stream_idle_timeout_seconds: float | None
) -> CLIQueryOptions:
    """Project the maestro-side options onto the subset the CLI path honours."""
    kwargs: dict[str, Any] = {}
    if options is not None and options.cwd is not None:
        kwargs["cwd"] = options.cwd
    # ``> 0`` (not ``is not None``) is deliberate: in ``maestro.sdk`` a 0 idle
    # timeout *disables* the deadline, but the CLI driver has no "disable" — its
    # stream read is always bounded. So a 0 here means "fall back to
    # maestro_sdk's default deadline," not "run unbounded." This diverges from
    # the agent path's 0-semantics by necessity, not by accident.
    if stream_idle_timeout_seconds is not None and stream_idle_timeout_seconds > 0:
        kwargs["stream_idle_timeout_seconds"] = stream_idle_timeout_seconds
    return CLIQueryOptions(**kwargs)


def _external_mcp_servers(options: QueryOptions | None) -> dict[str, dict[str, Any]]:
    """Select the command-shaped MCP entries forwardable to the CLI.

    ``options.mcp_servers`` is the merged dict the loop hands every provider:
    forwardable servers carry a ``{command, args, env}`` shape — both
    :meth:`maestro.config.Config.mcp_servers_config`'s external stdio entries
    (Context7 / Serena) and, since proposal 076 Step 2, the
    :class:`~maestro.providers.cli_paced_tool_adapter.CliPacedToolAdapter`'s
    ``maestro _mcp-serve`` launcher entries for Maestro's own in-process
    introspection / messaging servers. Only command-shaped entries serialise
    into a ``--mcp-config`` payload, so this keeps exactly those; a ``None`` /
    empty / wholly-unforwardable mapping yields ``{}`` (no ``--mcp-config`` is
    emitted).

    Any *remaining* non-command-shaped entry is a spec this provider cannot host
    in a separate ``claude`` process and the Step 2 adapter did not cover (e.g. a
    future SDK-instance server). Selecting it out narrows the spawned child's
    tool surface, so — per proposal 076 Step 3 — that drop is announced as a
    WARNING naming the dropped servers rather than happening silently (a silent
    tool-surface narrowing is exactly the hazard the fail-loud doctrine forbids
    most). WARNING, not a raise: a missing introspection server degrades a phase
    (the model runs without those tools) rather than corrupting it. The signal
    recurs per spawn — not once per process — because every degraded spawn
    genuinely runs without those tools until the spec is made forwardable.
    """
    if options is None or not options.mcp_servers:
        return {}
    forwardable: dict[str, dict[str, Any]] = {}
    dropped: list[str] = []
    for name, spec in options.mcp_servers.items():
        if isinstance(spec, dict) and "command" in spec:
            forwardable[name] = spec
        else:
            dropped.append(name)
    if dropped:
        _log.warning(
            "cli_paced provider cannot forward %d MCP server(s) to the claude "
            "CLI — not command-shaped launcher entries, so the spawned child "
            "runs without their tools: %s",
            len(dropped),
            ", ".join(sorted(dropped)),
        )
    return forwardable


def _tool_policy_flags(options: QueryOptions | None) -> list[str]:
    """Fold tool policy onto the CLI's own flags (proposal 061 Step 6).

    Maps ``tools`` → ``--tools`` (an empty list emits ``--tools ""``, the CLI's
    "disable all tools" form — so ``tools=[]`` stays materially distinct from
    ``tools=None``, which omits the flag), ``allowed_tools`` → ``--allowedTools``,
    and ``disallowed_tools`` → ``--disallowedTools``. Each tool name is its own
    argv element (the CLI flags are variadic), so a tool spec containing spaces —
    e.g. ``Bash(git *)`` — is one unambiguous token, not shell-split. An empty
    ``allowed_tools`` is a no-op (the CLI ignores an empty filter), so only a
    non-empty list emits its flag. ``--disallowedTools`` is
    always emitted: the caller's list (``None`` → ``Agent``) unioned with
    :func:`~maestro.tool_surface.deny_ungated_shell`'s ungated shell built-ins,
    so a ``cli_paced`` subprocess never exposes a shell the gate does not see.
    """
    flags: list[str] = []
    if options is not None and options.tools is not None:
        flags.append(_TOOLS_FLAG)
        flags.extend(options.tools or [""])
    if options is not None and options.allowed_tools:
        flags.append(_ALLOWED_TOOLS_FLAG)
        flags.extend(options.allowed_tools)
    flags.append(_DISALLOWED_TOOLS_FLAG)
    flags.extend(
        deny_ungated_shell(options.disallowed_tools if options is not None else None)
    )
    return flags


def _gate_settings_flags(options: QueryOptions | None) -> list[str]:
    """Fold the action-gate onto the CLI's ``--settings`` flag (proposal 066).

    When ``options.hooks`` is the recognised task action-gate, returns
    ``["--settings", <inline JSON>]`` registering the ``maestro
    _gate-bash-hook`` subcommand as the spawned CLI's own ``PreToolUse`` Bash
    hook, branch-scoped to the gate's owned-branch set
    (``allowed_branches``).  Returns ``[]`` when no hooks ride the options; an
    *unrecognised* hooks payload also returns ``[]`` here only defensively —
    :func:`_reject_unsupported_options` has already failed loud on it before
    any spawn command is built.
    """
    if options is None or options.hooks is None:
        return []
    gate = task_gate_from_hooks(options.hooks)
    if gate is None:
        return []
    return [
        _SETTINGS_FLAG,
        serialize_gate_settings(
            gate.allowed_branches, gate.strip_redirections, gate.current_branch,
            gate.allowed_push_remotes,
        ),
    ]


def _gate_env(options: QueryOptions | None) -> dict[str, str]:
    """Map the action-gate's branch set onto the CLI child's environment (066).

    Returns the JSON-encoded owned-branch env
    (:func:`allowed_branches_to_env`) when ``options.hooks`` is the recognised
    task action-gate and it carries a non-empty set, ``{}`` otherwise.  A
    branch-less gate sets nothing: the hook subprocess then reads no branch and
    the gate core denies every push fail-closed, mirroring the in-process
    ``TaskGateHook(None)`` posture.  Using the *same* encoder as the settings
    ``env`` block means the child env and the settings payload cannot drift;
    setting it on the child too means the hook subprocess sees it regardless of
    how the CLI scopes settings env.
    """
    if options is None or options.hooks is None:
        return {}
    gate = task_gate_from_hooks(options.hooks)
    if gate is None:
        return {}
    return {
        **allowed_branches_to_env(gate.allowed_branches),
        **strip_redirections_to_env(gate.strip_redirections),
        **current_branch_to_env(gate.current_branch),
        **push_remotes_to_env(gate.allowed_push_remotes),
    }


def _build_cli_command(options: QueryOptions | None) -> list[str]:
    """Build the spawn command carrying external-MCP, tool-policy, and gate flags.

    Returns the base command plus an inline string-encoded
    ``--mcp-config`` payload (``{"mcpServers": {...}}``) for the external servers
    in *options* (Step 5), the ``--tools`` / ``--allowedTools`` /
    ``--disallowedTools`` tool-policy flags (Step 6), and the action-gate
    ``--settings`` payload when *options* carries the task gate (proposal 066).
    The ungated-shell ``--disallowedTools`` denial is always present (issue
    #4381), so the client never falls back to its default command. Inline JSON
    (not a temp file) keeps the path free of real I/O. Each CLI flag
    is variadic; every value here (a JSON payload, tool names) is
    non-``--``-prefixed, so a following ``--flag`` cleanly terminates the prior
    variadic group regardless of ordering.
    """
    extra: list[str] = []
    external = _external_mcp_servers(options)
    if external:
        payload = serialize_mcp_config(build_mcp_config(external))
        extra += [_MCP_CONFIG_FLAG, payload]
    extra += _tool_policy_flags(options)
    extra += _gate_settings_flags(options)
    return [*_BASE_COMMAND, *extra]


def _compose_prompt(prompt: str, system: str | None) -> str:
    """Fold the system prompt into the user prompt (the CLI driver does not
    thread a separate system prompt)."""
    if system is not None:
        return f"{system}\n\n{prompt}"
    return prompt


def _augment_prompt_for_schema(
    prompt: str, output_format: dict[str, Any], *, reask: bool
) -> str:
    """Append the requested JSON Schema to the prompt (proposal 061 Step 9).

    The CLI returns free-form text, so structured output is *steered* by appending
    the schema and an instruction to emit only conforming JSON. *reask* prepends a
    short correction note used for the single retry after a first-attempt
    parse/validation miss; because the one-shot ``query`` path spawns a fresh
    ``claude`` process per attempt (no transcript memory of the bad answer), the
    re-ask still carries the full original prompt + schema, not a bare "try again".
    A missing ``schema`` key degrades to ``{}`` (any JSON conforms), so the prompt
    still asks for JSON rather than emitting a malformed instruction.
    """
    schema = output_format.get("schema")
    schema_text = json.dumps(schema, indent=2) if schema is not None else "{}"
    correction = (
        "Your previous reply was not valid JSON conforming to the schema. "
        if reask
        else ""
    )
    return (
        f"{prompt}\n\n"
        f"{correction}Respond with a single JSON value that strictly conforms to "
        f"this JSON Schema. Output only the JSON — no prose, no code fences:\n"
        f"{schema_text}"
    )


def _parse_and_validate(result: QueryResult, schema: dict[str, Any] | None) -> Any:
    """Parse *result*'s response as JSON and validate it against *schema*.

    Raises :class:`json.JSONDecodeError` when no JSON can be extracted (reusing
    :meth:`maestro.sdk.QueryResult.parse_json`'s fenced-block / bracket recovery)
    and :class:`jsonschema.ValidationError` when the parsed value violates the
    schema — both signals :func:`_query_structured` catches to trigger the single
    re-ask. The *schema* itself is assumed well-formed here: :func:`_query_structured`
    pre-checks it before the first turn, so a malformed schema (a caller bug) has
    already failed loud and never reaches this parse-then-re-ask path.
    """
    obj = result.parse_json()
    if schema is not None:
        jsonschema.validate(obj, schema)
    return obj


async def _query_structured(
    run_turn: Callable[[str], Awaitable[QueryResult]],
    prompt: str,
    output_format: dict[str, Any],
    caller: str | None = None,
) -> QueryResult:
    """Client-side structured-output validate-and-reask (proposal 061 Step 9).

    The CLI cannot enforce a schema server-side, so the provider enforces it here:
    append the schema (:func:`_augment_prompt_for_schema`), parse + validate the
    answer (:func:`_parse_and_validate`), and on a first-attempt parse/validation
    miss re-ask **exactly once** before failing loud with
    :data:`_OUTPUT_FORMAT_VALIDATION_FAILED`. The validated object rides back on
    :attr:`maestro.sdk.QueryResult.structured_output` so a caller's ``parse_json``
    short-circuits to it. *run_turn* runs one CLI turn for a (possibly augmented)
    prompt and returns the re-packed :class:`~maestro.sdk.QueryResult`.

    The *schema* itself is well-formedness-checked **up front**, before the first
    CLI turn. A malformed schema is a caller bug, not a bad answer: validating it
    only inside :func:`_parse_and_validate` (after ``parse_json``) would let a
    non-JSON first answer raise :class:`json.JSONDecodeError` first — masking the
    :class:`jsonschema.SchemaError` behind a spurious re-ask, or losing it entirely
    when the re-ask also fails to parse. The pre-check fails loud with no CLI turn.
    """
    schema = output_format.get("schema")
    if schema is not None:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    result = await run_turn(_augment_prompt_for_schema(prompt, output_format, reask=False))
    try:
        result.structured_output = _parse_and_validate(result, schema)
        return result
    except (json.JSONDecodeError, jsonschema.ValidationError) as first_exc:
        _log.warning(
            "cli_paced.query: structured output failed validation on the first "
            "answer (%s); re-asking once",
            first_exc,
        )
    result = await run_turn(_augment_prompt_for_schema(prompt, output_format, reask=True))
    try:
        result.structured_output = _parse_and_validate(result, schema)
    except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
        # Typed like the anthropic provider's schema exhaustion so a caller's
        # unparseable-reply routing sees the same category (#4535).
        raise SDKError(
            _OUTPUT_FORMAT_VALIDATION_FAILED,
            original=exc,
            category=ErrorCategory.STRUCTURED_OUTPUT_INVALID,
            caller=caller,
        ) from exc
    return result


def _translate_error(exc: MaestroSDKError) -> SDKError:
    """Re-pack a maestro_sdk error into the maestro resume taxonomy."""
    category = _CATEGORY_BY_NAME.get(exc.category, ErrorCategory.UNKNOWN)
    return SDKError(str(exc), original=exc, category=category)


def _warn_idle_drift(result: CLIQueryResult) -> None:
    """One-shot, pack-named WARNING when the configured idle marker missed (075 Step 5).

    The driver records the miss on :attr:`maestro_sdk.QueryResult.idle_marker_missed`
    (and logs its own marker-level one-shot); this provider-side sentinel adds the
    *attribution* — which configured pack no longer matches the CLI layout — so an
    operator reading the loop log knows what to re-pin after a CLI upgrade.
    Behaviour still degrades to the honest EOF-only path; the pack is
    observability config, not a correctness gate.
    """
    global _idle_drift_warned
    if _marker_pack is None or not result.idle_marker_missed or _idle_drift_warned:
        return
    _idle_drift_warned = True
    _log.warning(
        "configured marker pack %r did not match the CLI layout (idle marker "
        "never fired before EOF) — possible CLI upgrade; session continuity "
        "degrades to EOF-only turns until the pack is re-pinned",
        _marker_pack.name,
    )


def _warn_session_id_drift(session: _CLISession) -> None:
    """One-shot, pack-named WARNING when no session id ever matched (075 Step 5).

    Fires once per process when a pack configuring
    :attr:`maestro_sdk.ClientMarkers.session_id` has served a session
    :data:`_SESSION_ID_DRIFT_TURNS` turns without one CLI-reported id: without
    the id a respawn cannot ``--resume`` and sidecar metering stays
    ``unmetered=True``, so the blind spot must be loud and attributable rather
    than read as "metering just happens to be unknown".
    """
    global _session_id_drift_warned
    if (
        _marker_pack is None
        or not _marker_pack.client.session_id
        or session.session_id_seen
        or session.turn_count < _SESSION_ID_DRIFT_TURNS
        or _session_id_drift_warned
    ):
        return
    _session_id_drift_warned = True
    _log.warning(
        "configured marker pack %r captured no CLI session id across a "
        "session's first %d turns — possible CLI upgrade; respawns cannot "
        "--resume and sidecar metering stays unmetered until the pack is "
        "re-pinned",
        _marker_pack.name,
        _SESSION_ID_DRIFT_TURNS,
    )


def _to_query_result(
    result: CLIQueryResult,
    caller: str | None,
    rows: Sequence[Mapping[str, Any]] | None,
) -> QueryResult:
    """Re-pack a CLI QueryResult into the maestro shape with honest usage.

    Usage metering (proposal 061 Step 10): when the CLI transcript sidecar for
    ``result.session_id``, pre-loaded as *rows* by :func:`_load_sidecar_rows`, is readable and carries token counts,
    the row carries those real, summed counts with ``unmetered=False`` and the
    reported model. Otherwise it falls back to the zero-filled ``unmetered=True``
    shape — the CLI surfaces no token counts in-band, so an unreadable/ambiguous
    sidecar is *unknown*, not measured-as-zero (mirrors the no-``ResultMessage``
    early-exit path; cost aggregation skips it and the dashboard renders ``—``,
    proposal 058 Step 13). ``total_cost_usd`` stays ``None`` on both branches: the
    sidecar carries token counts, not a metered dollar figure, and cost is derived
    downstream from the counts. Never fabricates a count.
    """
    # Layout-drift sentinel (075 Step 5): attribute an idle-marker miss to the
    # configured pack, once, before the usage re-pack — both query paths fold
    # through here, so neither needs its own check.
    _warn_idle_drift(result)
    metered = _sidecar_usage_from_rows(rows)
    # ``effort`` is deliberately left ``None`` on both rows: no effort flag is
    # passed to the CLI child, so echoing ``options.effort`` would record an
    # effort that never reached the model (proposal 140).
    if metered is not None:
        usage = QueryUsage(
            caller=caller,
            session_id=result.session_id,
            model=metered.model,
            input_tokens=metered.input_tokens,
            output_tokens=metered.output_tokens,
            cache_creation_tokens=metered.cache_creation_tokens,
            cache_read_tokens=metered.cache_read_tokens,
            total_tokens=(
                metered.input_tokens
                + metered.output_tokens
                + metered.cache_creation_tokens
                + metered.cache_read_tokens
            ),
            unmetered=False,
            provider="cli_paced",
        )
    else:
        usage = QueryUsage(
            caller=caller,
            session_id=result.session_id,
            unmetered=True,
            provider="cli_paced",
        )
    return QueryResult(
        response=result.response,
        tool_calls=[ToolCall(name=tc.name, input=dict(tc.args)) for tc in result.tool_calls],
        usage=usage,
        session_id=result.session_id,
    )


async def _run_probe(session: CLISession) -> ProbeReports:
    """Issue the slash-command metrics probe on a session runner, parse the redraws.

    Runs ``/cost`` / ``/usage`` / ``/context`` as read-only probes via
    :meth:`maestro_sdk.Session.run_command` (which leaves turn/answer state
    untouched), parsing each rendered redraw through :mod:`maestro_sdk.slash_probe`
    with the configured :data:`_probe_markers`. Each parser fail-softs to ``None``
    on a non-matching snapshot, so an unconfigured / skewed marker pack yields an
    all-``None`` :class:`ProbeReports` (honest "read nothing") rather than a
    fabricated count.

    Requires a resident runner. ``run_command`` can only probe a ``claude`` child
    that survives between turns, which needs an idle marker; ``cli_paced`` has none
    today (see the module-level resident-runner caveat), so on the live provider
    this call raises a plain ``RuntimeError`` ("no live runner") rather than reading
    metrics — by design, until the proposal 056 continuity slice keeps the child
    resident. Probe failures are **not** swallowed: a ``MaestroSDKError``
    (rate-limit / expired session) propagates to
    :meth:`CliPacedProvider.query_on_session` for resume-taxonomy translation; a
    plain ``RuntimeError`` (no live runner / closed session) propagates uncaught and
    crashes loud — both honour the fail-loud doctrine over logging-and-falling-through.
    """
    cost = parse_cost(await session.run_command(_COST_COMMAND), _probe_markers)
    usage = parse_usage(await session.run_command(_USAGE_COMMAND), _probe_markers)
    context = parse_context(await session.run_command(_CONTEXT_COMMAND), _probe_markers)
    return ProbeReports(cost=cost, usage=usage, context=context)


def context_window_from_reports(
    reports: ProbeReports | None,
) -> ContextWindowSnapshot | None:
    """Fold a probe pass's ``/context`` reading into a core context snapshot.

    The bridge from the provider-held :class:`ProbeReports` (proposal 062 Step 3)
    to :class:`maestro.diagnostics.ContextWindowSnapshot` (Step 4): translating
    ``maestro_sdk``'s ``ContextReport`` here keeps that report type from crossing
    into ``diagnostics`` / ``sdk.py`` and paying the ``maestro_sdk`` import on the
    default ``anthropic`` path. Returns ``None`` when no probe ran
    (``reports is None``) or its ``/context`` parse missed
    (``reports.context is None``), so a :class:`~maestro.diagnostics.DiagnosticSnapshot`
    built without a reading names the context-window gap in ``blind_spots()``
    rather than carrying a fabricated all-empty composition.
    """
    if reports is None or reports.context is None:
        return None
    ctx = reports.context
    return ContextWindowSnapshot(
        used_tokens=ctx.used_tokens,
        total_tokens=ctx.total_tokens,
        buckets=tuple((bucket.name, bucket.tokens) for bucket in ctx.buckets),
    )


def _record_probe_context_window(reports: ProbeReports | None) -> None:
    """Cache the freshest probe's ``/context`` reading for diagnostics (Step 4).

    Called right after a cadence-hit probe attaches to ``session.last_probe``;
    folds it through :func:`context_window_from_reports` and stores the result on
    the ``_last_context_window`` process-global, where
    :func:`maestro.sdk.latest_context_window` reads it. Freshest-wins, including
    ``None``: a probe whose ``/context`` did not parse honestly clears any stale
    composition rather than leaving an older reading to masquerade as current.
    """
    global _last_context_window
    _last_context_window = context_window_from_reports(reports)


def plan_headroom_from_reports(
    reports: ProbeReports | None,
) -> PlanHeadroom | None:
    """Fold a probe pass's ``/usage`` reading into a core plan-headroom signal.

    The Step 5 sibling of :func:`context_window_from_reports`: translating
    ``maestro_sdk``'s ``UsageReport`` here keeps that report type from crossing
    into ``cost_tracking`` / ``sdk.py`` and paying the ``maestro_sdk`` import on
    the default ``anthropic`` path. Returns ``None`` when no probe ran
    (``reports is None``) or its ``/usage`` parse missed (``reports.usage is
    None``) — a non-subscription login or a layout skew reads as "no headroom
    signal", never a fabricated percentage.
    """
    if reports is None or reports.usage is None:
        return None
    usage = reports.usage
    return PlanHeadroom(
        percent_used=usage.plan_percent_used, label=usage.plan_label,
    )


def _record_probe_plan_headroom(reports: ProbeReports | None) -> None:
    """Cache the freshest probe's ``/usage`` plan headroom (Step 5).

    The Step 5 sibling of :func:`_record_probe_context_window`: stores the
    translated reading on the ``_last_plan_headroom`` process-global, where
    :func:`maestro.sdk.latest_plan_headroom` reads it for the burn-rate
    throttle's awareness. Freshest-wins, including ``None`` — a probe whose
    ``/usage`` did not parse honestly clears any stale headroom rather than
    leaving an older reading to masquerade as current.
    """
    global _last_plan_headroom
    _last_plan_headroom = plan_headroom_from_reports(reports)


def _effective_cwd(cwd: str | None) -> str:
    """Resolve the cwd the CLI child was (or will be) launched in.

    The transcript sidecar's project-dir slug is keyed on the CLI child's launch
    cwd: an explicit ``options.cwd`` when set, else the spawned ``claude`` inherits
    this process's cwd. :func:`_read_sidecar_usage` needs that same value to locate
    the file, so both query paths resolve it the same way here.
    """
    return cwd if cwd is not None else os.getcwd()


class CliPacedProvider:
    """CLI-paced :class:`~maestro.providers.base.Provider` backed by :mod:`maestro_sdk`.

    Stateless: every method drives the human-paced ``maestro_sdk.AsyncClient``
    through the module-level helpers above (type re-pack, error translation, and
    fail-loud rejection of unsupported option fields).
    """

    #: Fully qualified (proposal 061 Step 11): every flag is ``True``, each
    #: vouched for by a CLI-side mechanism rather than by abandoning the pty:
    #:
    #: * ``sessions`` — :class:`maestro_sdk.Session` keeps one pty runner alive
    #:   across turns and respawns with ``claude --resume <id>`` (Steps 1-2);
    #:   ``resume_session_id`` / ``initial_prompt`` forward into it. Live
    #:   continuity needs a configured marker pack (see the module docstring).
    #: * ``in_process_tools`` — :class:`~maestro.providers.cli_paced_tool_adapter.CliPacedToolAdapter`
    #:   materialises the registry into a ``--mcp-config`` + allow-list (Step 4);
    #:   the cross-process launcher entrypoint ships with the rollout proposal.
    #: * ``remote_mcp`` — external stdio servers fold into ``--mcp-config`` on
    #:   the spawn command (Step 5).
    #: * ``extended_thinking`` — the child follows the CLI's own thinking
    #:   effort/thinking config; Maestro's effort and budget are not forwarded (140).
    #: * ``long_context`` — the long-context beta maps onto the child's
    #:   ``ANTHROPIC_CUSTOM_HEADERS`` env (Step 7).
    #: * ``structured_output`` — one-shot ``output_format`` is enforced
    #:   client-side via validate-and-reask (Step 9; sessions still reject it).
    #: * ``usage_metering`` — transcript-sidecar token counts re-pack into a
    #:   real ``QueryUsage(unmetered=False)``, honest ``unmetered=True``
    #:   fallback when unreadable (Step 10).
    #: * ``streaming`` — ``complete_on`` early-exits the read loop against the
    #:   parser's accumulated answer text (Step 3).
    #:
    #: Capability parity is not a production rollout. The in-process
    #: ``PreToolUse`` action-gate cannot reach a separate ``claude`` process,
    #: so the provider replicates it into the spawned CLI's own permission
    #: settings (proposal 066; :mod:`maestro.providers.cli_paced_gate`);
    #: actually turning ``cli_paced`` on awaits the rollout proposal (067).
    capabilities = Capabilities(
        sessions=True,
        in_process_tools=True,
        remote_mcp=True,
        extended_thinking=True,
        long_context=True,
        structured_output=True,
        usage_metering=True,
        streaming=True,
    )

    async def query(
        self,
        prompt: str,
        *,
        options: QueryOptions | None = None,
        system: str | None = None,
        complete_on: Callable[[Any, str], bool] | None = None,
        stream_idle_timeout_seconds: float | None = None,
    ) -> QueryResult:
        """Run a one-shot query through the CLI-paced driver.

        ``max_turns`` is honoured only as ``>= 1`` here: a one-shot query is a
        single prompt-turn, so any ``max_turns >= 1`` is satisfied by construction
        and no counter is needed (the per-session cap lives in
        :meth:`query_on_session`). A sub-1 ``max_turns`` (``0`` / negative) is *not*
        enforced on this path — the single turn still runs — whereas a ``max_turns=0``
        *session* raises on its first turn. Sub-1 caps are not a real caller shape
        (Anthropic ``max_turns`` is always ``>= 1``); this divergence is documented
        rather than guarded so the one-shot path stays counter-free.
        """
        _reject_unsupported_options(options)
        # A positive per-call override wins; a 0 or absent override falls back
        # to the configured [sdk.cli_paced] deadline (None → maestro_sdk's
        # built-in default). The ``> 0`` test mirrors query_on_session and
        # _to_cli_options: a 0 means "use the baseline deadline," not "disable"
        # (the CLI driver has no disable). Keeping the same ``> 0`` gate here
        # makes an explicit 0 behave identically on the one-shot and session
        # paths — both fall back to the configured baseline rather than diverging.
        effective_idle = (
            stream_idle_timeout_seconds
            if stream_idle_timeout_seconds is not None
            and stream_idle_timeout_seconds > 0
            else _stream_idle_timeout_seconds
        )
        cli_opts = _to_cli_options(options, effective_idle)
        # In-stream early-exit (Step 3): the adapted predicate runs against the
        # parser's accumulated answer text inside the driver's read loop.
        adapted = _adapt_complete_on(complete_on)
        if adapted is not None:
            cli_opts = replace(cli_opts, complete_on=adapted.predicate)
        caller = options.caller if options is not None else None
        # The transcript sidecar (Step 10) is keyed on the CLI child's launch cwd
        # — the same ``cwd`` projected onto ``cli_opts`` above.
        cwd = _effective_cwd(cli_opts.cwd)
        # ``_child_env`` unions the long-context-beta header (Step 7) and the
        # thinking-budget env (Step 8); it raises for an un-mappable beta, and
        # ``{} or None`` keeps the no-override path at ``env=None`` (child inherits
        # this process's env).
        client = _client_factory(
            cli_opts, _build_cli_command(options), _child_env(options) or None
        )

        async def _run_turn(turn_prompt: str) -> QueryResult:
            if adapted is not None:
                # A structured re-ask is a fresh turn: forget the prior fire.
                adapted.fired_prefix = None
            try:
                result = await client.query(_compose_prompt(turn_prompt, system))
            except MaestroSDKError as exc:
                raise _translate_error(exc) from exc
            if adapted is not None:
                result = adapted.trim(result)
            # One read of the CLI transcript serves both metering and the
            # tool-surface sidecar; a fresh process means every row is this turn's.
            rows = _load_sidecar_rows(result.session_id, cwd)
            _record_transcript(caller, turn_prompt, result.response, rows)
            return _to_query_result(result, caller, rows)

        # Structured output (proposal 061 Step 9): when a schema is requested the
        # CLI cannot enforce it server-side, so the provider validates client-side
        # and re-asks once before failing loud. Absent a schema the turn runs once
        # verbatim — behaviour-identical to before this slice.
        output_format = options.output_format if options is not None else None
        if output_format is not None:
            return await _query_structured(_run_turn, prompt, output_format, caller)
        return await _run_turn(prompt)

    async def create_session(
        self,
        initial_prompt: str | None = None,
        *,
        options: QueryOptions | None = None,
        system: str | None = None,
        resume_session_id: str | None = None,
    ) -> Session:
        """Open a CLI-paced session, returning an opaque :class:`_CLISession`.

        ``resume_session_id`` and ``initial_prompt`` forward into
        :meth:`maestro_sdk.AsyncClient.create_session` (proposal 061 Step 2): the
        driver spawns ``claude --resume <id>`` and delivers the seed prompt as
        the session's first turn, ahead of the caller's first
        :meth:`query_on_session` prompt. The seed is composed with *system* here
        — the driver threads no separate system prompt, and every follow-up turn
        re-prepends the same baseline (see :meth:`query_on_session`).
        """
        _reject_unsupported_options(options)
        # Structured output is enforced only on the one-shot ``query`` path
        # (proposal 061 Step 9); ``query_on_session`` does no validation, so a
        # session carrying ``output_format`` would silently drop it. Reject rather
        # than mislead — the rejection rides here (not in the shared
        # ``_reject_unsupported_options``) so the one-shot path stays supported.
        if options is not None and options.output_format is not None:
            raise NotImplementedError(_OUTPUT_FORMAT_SESSION_UNSUPPORTED)
        # Bake the configured [sdk.cli_paced] deadline into the session's stored
        # options so every follow-up turn inherits it (a per-call override on
        # query_on_session still wins); None → maestro_sdk's built-in default.
        cli_opts = _to_cli_options(options, _stream_idle_timeout_seconds)
        # External MCP servers bake into the session's spawn command, so every
        # turn the session drives inherits the same ``--mcp-config`` (Step 5); the
        # long-context-beta header (Step 7) likewise bakes into the session's
        # spawn (via :func:`_child_env`) so every turn carries the same
        # ``anthropic-beta`` env.
        client = _client_factory(
            cli_opts, _build_cli_command(options), _child_env(options) or None
        )
        seed = (
            _compose_prompt(initial_prompt, system)
            if initial_prompt is not None
            else None
        )
        return _CLISession(
            session=client.create_session(seed, resume=resume_session_id),
            options=cli_opts,
            system=system,
            max_turns=options.max_turns if options is not None else None,
        )

    async def query_on_session(
        self,
        session: Session,
        prompt: str,
        *,
        caller: str | None = None,
        complete_on: Callable[[Any, str], bool] | None = None,
        stream_idle_timeout_seconds: float | None = None,
        bypass_warning_throttle: bool = False,
    ) -> QueryResult:
        """Follow-up turn on a CLI-paced session handle.

        ``bypass_warning_throttle`` is accepted for protocol parity and ignored:
        the CLI-paced driver does not drive the agent-SDK per-call warning
        throttle, so there is no soft pre-call sleep to skip here.

        Transcript continuity rides on :class:`maestro_sdk.Session` (proposal 061
        Steps 1-2): the driver reuses the live child between turns under an idle
        marker and respawns with ``--resume`` from the captured session id
        otherwise. Under the default empty :class:`maestro_sdk.ClientMarkers`
        no id is captured, so continuity degrades to independent turns until a
        marker pack is configured (see the module docstring); the session's
        baseline ``system`` prompt is re-prepended on every turn either way.
        """
        # Per-session prompt-turn cap (proposal 061 Step 6). Checked before the
        # turn_count bump so the N+1-th turn raises with no side effect — the
        # session has served its N turns and stops here. This is a ``RuntimeError``
        # (not a translated ``SDKError`` or a ``NotImplementedError`` like the
        # unsupported-feature rejections) on purpose: the cap is a caller-set
        # terminal control-flow stop, not a transient SDK failure to resume nor an
        # unsupported feature to remediate — so it is deliberately *outside* the
        # resume taxonomy and the caller is expected to open a new session.
        if session.max_turns is not None and session.turn_count >= session.max_turns:
            raise RuntimeError(_MAX_TURNS_EXCEEDED.format(n=session.max_turns))
        opts = session.options
        # ``> 0`` mirrors _to_cli_options: a 0 override means "use the driver's
        # default deadline," not "disable it" (the CLI driver has no disable).
        if stream_idle_timeout_seconds is not None and stream_idle_timeout_seconds > 0:
            opts = replace(opts, stream_idle_timeout_seconds=stream_idle_timeout_seconds)
        # In-stream early-exit (Step 3): a per-call predicate applies to this turn
        # only — it is set on the local ``opts`` copy, never baked into the
        # session's stored options, so a later predicate-free turn runs unbounded.
        adapted = _adapt_complete_on(complete_on)
        if adapted is not None:
            opts = replace(opts, complete_on=adapted.predicate)
        session.turn_count += 1
        try:
            result = await session.session.query(
                _compose_prompt(prompt, session.system), options=opts
            )
        except MaestroSDKError as exc:
            raise _translate_error(exc) from exc
        if adapted is not None:
            result = adapted.trim(result)
        # Slash-command metrics probe (proposal 062 Step 3). After the turn returns,
        # issue the probe when enabled and this turn hits the cadence (``turn_count``
        # was bumped above, so ``every == 1`` probes every turn and ``every == N``
        # probes turns N, 2N, …). Default-off skips this entirely: no ``run_command``,
        # ``last_probe`` stays ``None``, the result is identical to today's. The probe
        # attaches to the provider-held session slot, never to the returned
        # ``QueryResult``.
        #
        # Error handling is fail-loud, not fail-soft. A probe-side ``MaestroSDKError``
        # (rate-limit / expired session) is translated into the resume taxonomy
        # exactly like the turn above; it discards this turn's result, but a
        # probe-time SDK failure signals a genuine session problem worth resuming. A
        # plain ``RuntimeError`` from ``run_command`` is deliberately *not* caught
        # here: it means the session has no resident child to probe (the EOF-mode
        # reality on ``cli_paced`` today — see the module-level resident-runner
        # caveat) or is closed, which is a wiring fault to crash on, not a transient
        # SDK failure to resume. Translating it into the resume taxonomy would mask
        # the missing-runner bug behind an endless retry, so it propagates uncaught.
        #
        # A non-positive ``every`` is read as "every turn," not a divisor: this gate
        # runs ahead of the Step 6 config validation, so a ``0`` (or negative)
        # cadence reaching here — from a config path that forgets the bound, or a
        # direct test override — degrades to probe-every-turn rather than crashing
        # the live turn with ``ZeroDivisionError``.
        fresh_probe: ProbeReports | None = None
        if _probe_metrics_enabled and (
            _probe_metrics_every < 1 or session.turn_count % _probe_metrics_every == 0
        ):
            try:
                fresh_probe = await _run_probe(session.session)
            except MaestroSDKError as exc:
                raise _translate_error(exc) from exc
            session.last_probe = fresh_probe
            # Fold this probe's /context reading into the diagnostics seam so a
            # later ``build_diagnostic_snapshot`` exposes realized context-window
            # composition (proposal 062 Step 4), and its /usage plan headroom
            # into the throttle-awareness seam (Step 5).
            _record_probe_context_window(fresh_probe)
            _record_probe_plan_headroom(fresh_probe)
        # Metering (Step 10) reads the sidecar keyed on the session's launch cwd —
        # the ``cwd`` baked into the stored ``options`` at create_session time.
        rows = _load_sidecar_rows(
            result.session_id, _effective_cwd(session.options.cwd)
        )
        # Record only this turn's tool rows (proposal 142): slice from the
        # session's cursor, restarting at row 0 when the CLI session id changed
        # or its transcript shrank below the cursor (rewritten in place). A turn
        # that captured no id records nothing and leaves the cursor untouched, so
        # a captured -> None -> captured sequence never re-records earlier rows.
        if result.session_id is not None and (
            result.session_id != session.transcript_session_id
            or (rows is not None and len(rows) < session.transcript_rows_seen)
        ):
            session.transcript_session_id = result.session_id
            session.transcript_rows_seen = 0
        turn_rows = rows[session.transcript_rows_seen:] if rows is not None else None
        if rows is not None:
            session.transcript_rows_seen = len(rows)
        _record_transcript(caller, prompt, result.response, turn_rows)
        query_result = _to_query_result(result, caller, rows)
        # Session-id drift sentinel (075 Step 5): record a captured id, and
        # once the session has served _SESSION_ID_DRIFT_TURNS turns with none
        # under a session_id-configured pack, warn once naming the pack.
        if query_result.session_id is not None:
            session.session_id_seen = True
        _warn_session_id_drift(session)
        # /cost cross-check (proposal 062 Step 5): when this turn both probed a
        # ``/cost`` reading and metered a real sidecar count, compare the two
        # session-cumulative token totals and WARN past the threshold. Only a
        # *this-turn* probe is compared (a stale ``session.last_probe`` against a
        # newer sidecar count would manufacture divergence), and only against a
        # metered row (an ``unmetered=True`` zero-fill is *unknown*, not a count
        # to diverge from). The helper logs only — the sidecar ``QueryUsage`` is
        # returned unchanged either way.
        if fresh_probe is not None and fresh_probe.cost is not None and (
            not query_result.usage.unmetered
        ):
            log_metering_divergence(
                fresh_probe.cost.total_tokens,
                query_result.usage.total_tokens,
                caller=caller,
            )
        return query_result

    async def disconnect_session(self, session: Session) -> None:
        """Close a CLI-paced session handle."""
        await session.session.aclose()

    def latest_context_window(self) -> ContextWindowSnapshot | None:
        """Return the freshest probe-derived context-window composition (Step 4).

        Reads the ``_last_context_window`` process-global populated on each
        cadence-hit probe (see :func:`_record_probe_context_window`). ``None``
        when probing is disabled, no probe has run yet, or the latest
        ``/context`` parse missed — the diagnostics layer then names the gap in
        ``blind_spots()``. Read-only and instance-state-free: the provider keeps
        its documented stateless contract (the cache is module-global). This is
        the one provider exposing the accessor; :func:`maestro.sdk.latest_context_window`
        reaches it by ``getattr`` so no other provider grows a stub.
        """
        return _last_context_window

    def latest_plan_headroom(self) -> PlanHeadroom | None:
        """Return the freshest probe-derived plan rate-limit headroom (Step 5).

        Reads the ``_last_plan_headroom`` process-global populated on each
        cadence-hit probe (see :func:`_record_probe_plan_headroom`). ``None``
        when probing is disabled, no probe has run yet, the login is not a
        subscription, or the latest ``/usage`` parse missed. Read-only — the
        burn-rate throttle reports it on transitions but never gates on it
        (deferred by the proposal). Same ``getattr`` posture as
        :meth:`latest_context_window`: :func:`maestro.sdk.latest_plan_headroom`
        reaches it without any other provider growing a stub.
        """
        return _last_plan_headroom


__all__ = [
    "CliPacedProvider",
    "ProbeReports",
    "configure_cli_paced",
    "context_window_from_reports",
    "plan_headroom_from_reports",
    "resolve_marker_pack",
]

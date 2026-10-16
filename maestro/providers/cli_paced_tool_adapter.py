"""CLI-paced materialiser for the vendor-neutral tool registry (proposal 061 Step 4, 076 Step 2).

Maps a :data:`~maestro.providers.base.ToolRegistry` into the two halves the
``cli_paced`` provider needs to host Maestro's in-process introspection /
messaging tools through a *separate* ``claude`` CLI process:

* :meth:`CliPacedToolAdapter.materialize` — the registry → wire-shape transform
  the :class:`~maestro.providers.base.ToolAdapter` protocol requires. Where the
  Anthropic adapter emits ``{"type": "sdk", ...}`` in-process server instances
  (consumed inside the same process by the Claude Agent SDK), the CLI-paced
  adapter emits a CLI ``--mcp-config`` JSON payload — the only shape a *separate*
  ``claude`` subprocess can consume — alongside the identical
  ``mcp__<server>__<tool>`` qualified-name allow-list the Anthropic convention
  uses. The ``--mcp-config`` is built by :mod:`maestro_sdk.mcp_config` (pure
  JSON, no transport).
* :meth:`CliPacedToolAdapter.build_servers` — the in-process hosts. Each registry
  server is wrapped by :func:`maestro.inproc_mcp.create_inproc_mcp_server` so the
  unchanged Python handlers in :mod:`maestro.tools` run **in Maestro's process**;
  a dispatched tool call reaches the same handler the Anthropic path would. This
  is the in-process side, kept for callers that host the registry in-process.

**Launcher-shaped config (proposal 076 Step 2).** Proposal 061 Step 4 emitted a
coherent-but-deferred ``--mcp-config`` naming a launcher that did not yet exist;
proposal 076 Step 1 shipped that launcher as the hidden ``maestro _mcp-serve
<server>`` subcommand (the pattern 066 established with ``maestro
_gate-bash-hook``). :meth:`materialize` now points each per-server entry at it —
``{command: "maestro", args: ["_mcp-serve", <server>], env}`` — so the entries
are *command-shaped* and the provider's
:func:`maestro.providers.cli_paced._external_mcp_servers` selector forwards them
onto the spawn argv with no provider change. The ``env`` block threads the
config env (:func:`launcher_config_env`) so the sibling ``maestro _mcp-serve``
process resolves the *same* state files as the loop. The semantic shift this
makes explicit: "in-process" becomes "in a sibling Maestro process" on this
provider — the handlers are unchanged Python, but they run against the data
directory from a second process. The loud interim signal for any
``options.mcp_servers`` spec the launcher cannot cover is proposal 076's next
slice (Step 3); selecting *this* adapter for a ``cli_paced`` phase (the executor
today hard-codes the Anthropic adapter) is a separate concern this step does not
take on.

**Lazy import posture.** This module imports only ``maestro_sdk.mcp_config`` and
the neutral tool shape at top; the subcommand name is imported from
:mod:`maestro.cli` *lazily inside* :meth:`materialize`, so importing the adapter
does not drag ``maestro.cli``'s heavy graph onto the default ``anthropic`` path —
the import only happens when a ``cli_paced`` phase actually materialises tools
(at which point ``cli`` is already loaded as the process entry point).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Any

from maestro_sdk.mcp_config import build_mcp_config, stdio_server

from maestro.inproc_mcp import create_inproc_mcp_server
from maestro.providers.base import ToolRegistry

#: Module the launcher-shaped ``--mcp-config`` entries run under the loop's own
#: interpreter: the spawned ``claude`` runs ``<sys.executable> -P -m maestro.cli
#: _mcp-serve <server>`` (``-P`` keeps the workspace cwd off ``sys.path``) to host one logical server over stdio MCP, so the
#: sibling is the same installation as the loop regardless of ``PATH`` —
#: mirroring the action-gate hook's binding (issue #4160).
_LAUNCHER_MODULE = "maestro.cli"

#: Config env vars forwarded into each launcher child's MCP ``env`` block so the
#: sibling ``maestro _mcp-serve`` process resolves the *same* state files as the
#: loop. ``MAESTRO_ENV`` selects the ``maestro.{env}.toml`` overlay
#: :meth:`maestro.config.Config.from_env` reads — and therefore the data
#: directory — so a mismatch here would point the sibling at different state. The
#: ``claude`` CLI *merges* this block over the child's inherited environment (the
#: external GitHub server forwards only ``GITHUB_TOKEN`` the same way), so PATH /
#: credentials stay intact and only the overlay selection is pinned.
_FORWARDED_CONFIG_ENV: tuple[str, ...] = ("MAESTRO_ENV",)


def launcher_config_env() -> dict[str, str]:
    """Capture the config env the launcher child needs to match the loop's state.

    Returns the subset of :data:`_FORWARDED_CONFIG_ENV` present in this process's
    environment, in declaration order. It is the default ``env`` an adapter
    threads into every launcher-shaped ``--mcp-config`` entry, so a sibling
    ``maestro _mcp-serve`` reads the same ``maestro.{env}.toml`` overlay (hence
    the same data directory) as the loop that spawned it. An absent var is
    *omitted* rather than blanked, so the child falls back to the loop's own
    default instead of being forced onto the base ``maestro.toml``.
    """
    return {name: os.environ[name] for name in _FORWARDED_CONFIG_ENV if name in os.environ}


class CliPacedToolAdapter:
    """Materialise a tool registry into a CLI ``--mcp-config`` plus in-process hosts."""

    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        """Capture the env threaded into every launcher-shaped ``--mcp-config`` entry.

        *env* is the environment block each emitted launcher entry carries so the
        sibling ``maestro _mcp-serve`` child resolves the same state as the loop.
        ``None`` (the default) captures :func:`launcher_config_env` from this
        process — the ergonomic default for a loop-side spawn, which needs no
        extra plumbing — while an explicit mapping (including ``{}``) overrides it
        for tests and callers that thread a specific environment.
        """
        self._env: dict[str, str] = (
            dict(env) if env is not None else launcher_config_env()
        )

    def materialize(
        self, registry: ToolRegistry,
    ) -> tuple[list[str], dict[str, Any]]:
        """Return ``(qualified_tool_names, mcp_config)`` for *registry*.

        The first element is the ``mcp__<server>__<tool>`` allow-list in
        registry-insertion order (server-first, then tool order within the
        server) — byte-identical to the Anthropic adapter's, so a phase's
        allow-list is provider-independent. The second element is the CLI
        ``--mcp-config`` payload (``{"mcpServers": {<server>: <launcher spec>}}``):
        each server entry is a *command-shaped* launcher invocation —
        ``{command: "maestro", args: ["_mcp-serve", <server>], env}`` (the ``env``
        block omitted when empty) — pointing at the hidden subcommand (proposal
        076 Step 1) that hosts that one logical server over stdio MCP in a sibling
        Maestro process. Because the entries are command-shaped, the provider's
        :func:`maestro.providers.cli_paced._external_mcp_servers` selector
        forwards them onto the spawn argv with no provider change.
        """
        # Lazy: the subcommand name lives in maestro.cli (where its argparse
        # parser is wired); importing it here rather than at module top keeps the
        # default anthropic path off cli's heavy import graph.
        from maestro.cli import MCP_SERVE_SUBCOMMAND

        qualified_names: list[str] = []
        servers: dict[str, dict[str, Any]] = {}
        for server_name, tools in registry.items():
            servers[server_name] = stdio_server(
                sys.executable,
                args=["-P", "-m", _LAUNCHER_MODULE, MCP_SERVE_SUBCOMMAND, server_name],
                env=self._env or None,
            )
            for t in tools:
                qualified_names.append(f"mcp__{server_name}__{t.name}")
        return qualified_names, build_mcp_config(servers)

    def build_servers(self, registry: ToolRegistry) -> dict[str, dict[str, Any]]:
        """Build the in-process MCP host per registry server.

        Each value is the ``{"type": "sdk", "name", "instance"}`` config
        :func:`maestro.inproc_mcp.create_inproc_mcp_server` produces — the same
        in-process server the Anthropic adapter hosts, so the unchanged Python
        handlers run in Maestro's process and a dispatched call reaches the same
        handler regardless of provider. Keyed by server name in registry order.
        """
        return {
            server_name: create_inproc_mcp_server(name=server_name, tools=list(tools))
            for server_name, tools in registry.items()
        }


__all__ = ["CliPacedToolAdapter", "launcher_config_env"]

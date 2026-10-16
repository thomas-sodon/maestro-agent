"""Provider abstraction for Maestro's LLM backend (proposal 058).

The :mod:`maestro.providers.base` module declares the vendor-neutral
``Provider`` protocol, a ``Capabilities`` descriptor, an opaque ``Session``
handle, the ``ToolRegistry`` / ``ToolAdapter`` tool-materialisation seam, and
re-exports the contract types (``QueryOptions`` / ``QueryResult`` /
``QueryUsage`` / ``ToolCall`` / ``ErrorCategory`` / ``SDKError``) so providers
and call sites import the contract from one neutral module rather than
reaching into :mod:`maestro.sdk`.

The registry/adapter seam is wired: ``cli._build_agent_tools`` assembles the
neutral ``ToolRegistry`` and the executor materialises it via the
``AnthropicToolAdapter`` (see :mod:`maestro.providers.anthropic_tool_adapter`).
The first concrete ``Provider`` has landed:
:class:`maestro.providers.anthropic.AnthropicProvider`, a stateless wrapper
whose four operations delegate to :mod:`maestro.sdk` (Phase 1 Step 3). Wiring
``sdk.py`` to select a provider (``get_active_provider``) remains Phase 2.
"""

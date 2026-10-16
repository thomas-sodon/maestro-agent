"""Structured mock for ``maestro.sdk.query`` in component tests.

Provides prompt-based routing, ordered fallback responses, response
builders for common shapes, auto-patching of all modules that import
``query``, and verification helpers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

from maestro.sdk import QueryResult, QueryUsage

# Every module that does ``from maestro.sdk import query``.
ALL_QUERY_MODULES = [
    "maestro.agent",
    "maestro.loop.task_generation",
    "maestro.consciousness",
    "maestro.evolution",
    "maestro.evolution_quality",
    "maestro.executor",
    "maestro.planner",
    "maestro_review.review",
]

QueryHandler = Callable[..., Awaitable[QueryResult]]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class _Route:
    """A prompt-matching rule that produces a QueryResult."""

    pattern: str | re.Pattern[str]
    response: QueryResult | QueryHandler
    times: int | None  # None = unlimited
    label: str | None


@dataclass
class CallRecord:
    """Record of a single SDK call for verification."""

    kwargs: dict[str, Any]
    result: QueryResult | None
    matched_label: str | None


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def evolution_plan(
    title: str,
    category: str = "capability",
    priority: str = "feature",
    rationale: str = "test",
    proposed_changes: list[str] | None = None,
    files_affected: list[str] | None = None,
    expected_impact: str = "test impact",
    issue_number: int | None = None,
) -> QueryResult:
    """Evolution plan response with sensible defaults."""
    return QueryResult(response=json.dumps({
        "title": title,
        "category": category,
        "priority": priority,
        "rationale": rationale,
        "proposed_changes": proposed_changes or ["change 1"],
        "files_affected": files_affected or ["test.py"],
        "expected_impact": expected_impact,
        "issue_number": issue_number,
    }))


def self_model_update(**fields: Any) -> QueryResult:
    """Self-model update response.  Only includes the fields you pass.

    Example::

        self_model_update(
            identity_statements=["I am Maestro"],
            capabilities_known=["task planning"],
        )
    """
    return QueryResult(response=json.dumps(fields))


def review_result(
    findings: list[dict[str, object]],
    summary: str = "ok",
) -> QueryResult:
    """PR review result response."""
    return QueryResult(
        response=json.dumps({"summary": summary, "findings": findings}),
        usage=QueryUsage(total_tokens=500, total_cost_usd=0.02),
    )


# ---------------------------------------------------------------------------
# Core mock
# ---------------------------------------------------------------------------


class SDKMock:
    """Mock for ``maestro.sdk.query`` with prompt routing and verification."""

    def __init__(self) -> None:
        self._routes: list[_Route] = []
        self._fallback_queue: list[QueryResult] = []
        self._calls: list[CallRecord] = []
        self._async_mock = AsyncMock(side_effect=self._dispatch)

    # --- Routing API -------------------------------------------------------

    def on(
        self,
        pattern: str,
        response: str | dict[str, Any] | QueryResult | QueryHandler,
        *,
        times: int | None = None,
        label: str | None = None,
        regex: bool = False,
    ) -> SDKMock:
        """Register a prompt-matched response.

        *pattern* is matched as a substring (or regex when *regex=True*)
        against the ``prompt`` keyword argument of each ``query()`` call.
        """
        compiled: str | re.Pattern[str] = re.compile(pattern) if regex else pattern
        qr = self._normalize_response(response)
        self._routes.append(_Route(
            pattern=compiled, response=qr, times=times, label=label,
        ))
        return self

    def on_any(
        self,
        response: str | dict[str, Any] | QueryResult | QueryHandler,
        *,
        times: int | None = None,
        label: str | None = None,
    ) -> SDKMock:
        """Register a catch-all response (unlimited by default)."""
        return self.on("", response, times=times, label=label)

    def then(
        self,
        *responses: str | dict[str, Any] | QueryResult,
    ) -> SDKMock:
        """Append ordered fallback responses consumed when no route matches."""
        for r in responses:
            nr = self._normalize_response(r)
            if not isinstance(nr, QueryResult):
                raise TypeError(
                    f"SDKMock.then() only accepts static responses (str, dict, QueryResult), "
                    f"got {type(nr).__name__!r}. Use sdk.on(pattern, handler) for callables."
                )
            self._fallback_queue.append(nr)
        return self

    # --- Context manager for auto-patching ---------------------------------

    def patch(
        self,
        *modules: str,
        extras: dict[str, Any] | None = None,
    ) -> _SDKMockContext:
        """Context manager that patches ``query`` in the given modules.

        If no modules are specified, all :data:`ALL_QUERY_MODULES` are
        patched.  *extras* maps additional dotted patch targets to their
        replacement (e.g. subprocess side-effects).
        """
        targets = list(modules) if modules else list(ALL_QUERY_MODULES)
        return _SDKMockContext(self, targets, extras or {})

    # --- Verification API --------------------------------------------------

    @property
    def calls(self) -> list[CallRecord]:
        return list(self._calls)

    @property
    def call_count(self) -> int:
        return len(self._calls)

    @property
    def prompts(self) -> list[str]:
        """All prompts sent, in order."""
        return [c.kwargs.get("prompt", "") for c in self._calls]

    @property
    def systems(self) -> list[str]:
        """All system prompts sent, in order."""
        return [c.kwargs.get("system", "") or "" for c in self._calls]

    def assert_called(self, *, times: int | None = None) -> None:
        if times is not None and len(self._calls) != times:
            raise AssertionError(
                f"Expected {times} SDK calls, got {len(self._calls)}"
            )
        if times is None and not self._calls:
            raise AssertionError("Expected at least one SDK call, got none")

    def assert_not_called(self) -> None:
        if self._calls:
            snippets = [c.kwargs.get("prompt", "")[:80] for c in self._calls]
            raise AssertionError(
                f"Expected no SDK calls, got {len(self._calls)}: {snippets}"
            )

    def assert_all_consumed(self) -> None:
        """Assert that all expected responses were consumed.

        Checks both the ordered fallback queue (from ``.then()``) and
        times-limited routes (from ``.on(..., times=N)``) that still
        have remaining invocations.
        """
        errors: list[str] = []
        if self._fallback_queue:
            errors.append(
                f"{len(self._fallback_queue)} unconsumed fallback response(s) remain"
            )
        for route in self._routes:
            if route.times is not None and route.times > 0:
                pat = (
                    route.pattern.pattern
                    if isinstance(route.pattern, re.Pattern)
                    else repr(route.pattern)
                )
                label = route.label or pat
                errors.append(
                    f"route {label}: {route.times} unconsumed invocation(s) remain"
                )
        if errors:
            raise AssertionError("\n".join(errors))

    def assert_prompt_contained(
        self, substring: str, *, call_index: int = -1,
    ) -> None:
        if not self._calls:
            raise AssertionError(
                f"assert_prompt_contained({substring!r}): no SDK calls recorded"
            )
        prompt = self._calls[call_index].kwargs.get("prompt", "")
        if substring not in prompt:
            raise AssertionError(
                f"Expected prompt to contain {substring!r}, "
                f"got: {prompt[:200]}..."
            )

    # --- Raw mock access ---------------------------------------------------

    @property
    def mock(self) -> AsyncMock:
        """The underlying ``AsyncMock``."""
        return self._async_mock

    # --- Internal ----------------------------------------------------------

    @staticmethod
    def _normalize_response(
        response: str | dict[str, Any] | QueryResult | QueryHandler,
    ) -> QueryResult | QueryHandler:
        if isinstance(response, QueryResult):
            return response
        if callable(response):
            return response
        if isinstance(response, dict):
            return QueryResult(response=json.dumps(response))
        return QueryResult(response=response)

    def _match_route(self, prompt: str) -> _Route | None:
        for route in self._routes:
            if route.times is not None and route.times <= 0:
                continue
            if isinstance(route.pattern, re.Pattern):
                if route.pattern.search(prompt):
                    return route
            else:
                if route.pattern in prompt:
                    return route
        return None

    async def _dispatch(self, **kwargs: Any) -> QueryResult:
        prompt = kwargs.get("prompt", "")

        # Try routes first
        route = self._match_route(prompt)
        if route is not None:
            response = route.response
            result: QueryResult | None = None
            try:
                if callable(response):
                    result = await response(**kwargs)
                else:
                    result = response
                if route.times is not None:
                    route.times -= 1
                return result
            finally:
                self._calls.append(CallRecord(
                    kwargs=kwargs, result=result, matched_label=route.label,
                ))

        # Try ordered fallback queue
        if self._fallback_queue:
            result = self._fallback_queue.pop(0)
            self._calls.append(CallRecord(
                kwargs=kwargs, result=result, matched_label=None,
            ))
            return result

        # Nothing matched
        route_descriptions: list[str] = []
        for r in self._routes:
            pat = r.pattern.pattern if isinstance(r.pattern, re.Pattern) else repr(r.pattern)
            route_descriptions.append(
                f"  - {r.label or 'unnamed'}: pattern={pat}, remaining={r.times}"
            )
        raise AssertionError(
            f"SDKMock: No matching response for prompt:\n"
            f"  {prompt[:200]}{'...' if len(prompt) > 200 else ''}\n"
            f"Registered routes:\n"
            + "\n".join(route_descriptions or ["  (none)"])
            + "\nFallback queue: empty"
            + f"\nTotal calls so far: {len(self._calls)}"
        )


class _SDKMockContext:
    """Context manager returned by :meth:`SDKMock.patch`."""

    def __init__(
        self,
        sdk_mock: SDKMock,
        modules: list[str],
        extras: dict[str, Any],
    ) -> None:
        self._sdk_mock = sdk_mock
        self._modules = modules
        self._extras = extras
        self._patchers: list[Any] = []

    def __enter__(self) -> SDKMock:
        try:
            for mod in self._modules:
                p = patch(f"{mod}.query", self._sdk_mock.mock)
                p.start()
                self._patchers.append(p)
            for target, value in self._extras.items():
                p = patch(target, value)
                p.start()
                self._patchers.append(p)
        except Exception:
            for p in reversed(self._patchers):
                p.stop()
            self._patchers.clear()
            raise
        return self._sdk_mock

    def __exit__(self, *args: object) -> None:
        errors: list[BaseException] = []
        for p in reversed(self._patchers):
            try:
                p.stop()
            except Exception as exc:
                errors.append(exc)
        if errors and args[0] is not None:
            # Attach teardown errors as notes on the original exception so
            # they appear in the traceback without masking the real failure.
            exc_instance = args[1]
            if isinstance(exc_instance, BaseException):
                for err in errors:
                    exc_instance.add_note(
                        f"patcher stop failed during teardown: {err}"
                    )
        elif errors:
            if len(errors) == 1:
                raise errors[0]
            raise ExceptionGroup("patcher stop failures", errors)

"""Unit tests for the SDKMock test utility."""

from __future__ import annotations

import json

import pytest

from maestro.sdk import QueryResult
from tests.component.sdk_mock import (
    SDKMock,
    evolution_plan,
    review_result,
    self_model_update,
)

# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


class TestResponseBuilders:
    def test_evolution_plan_defaults(self) -> None:
        qr = evolution_plan(title="Test plan")
        data = json.loads(qr.response)
        assert data["title"] == "Test plan"
        assert data["category"] == "capability"
        assert data["priority"] == "feature"
        assert data["proposed_changes"] == ["change 1"]
        assert data["files_affected"] == ["test.py"]

    def test_evolution_plan_custom(self) -> None:
        qr = evolution_plan(
            title="Fix bug",
            priority="resilience",
            proposed_changes=["Fix it"],
            files_affected=["a.py"],
        )
        data = json.loads(qr.response)
        assert data["title"] == "Fix bug"
        assert data["priority"] == "resilience"

    def test_self_model_update(self) -> None:
        qr = self_model_update(
            identity_statements=["I am Maestro"],
            capabilities_known=["planning"],
        )
        data = json.loads(qr.response)
        assert data["identity_statements"] == ["I am Maestro"]
        assert data["capabilities_known"] == ["planning"]
        assert "values" not in data  # only includes passed fields

    def test_review_result(self) -> None:
        qr = review_result([{"file": "a.py", "severity": "bug"}], summary="Found issues")
        data = json.loads(qr.response)
        assert data["summary"] == "Found issues"
        assert len(data["findings"]) == 1
        assert qr.usage.total_cost_usd == 0.02


# ---------------------------------------------------------------------------
# SDKMock routing
# ---------------------------------------------------------------------------


class TestSDKMockRouting:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_substring_match(self) -> None:
        sdk = SDKMock()
        sdk.on("hello", "world")
        result = await sdk._dispatch(prompt="say hello to me")
        assert result.response == "world"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_regex_match(self) -> None:
        sdk = SDKMock()
        sdk.on(r"step \d+", "matched", regex=True)
        result = await sdk._dispatch(prompt="execute step 42")
        assert result.response == "matched"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_dict_response_serialized(self) -> None:
        sdk = SDKMock()
        sdk.on("test", {"key": "value"})
        result = await sdk._dispatch(prompt="test")
        assert json.loads(result.response) == {"key": "value"}

    @pytest.mark.asyncio(loop_scope="function")
    async def test_query_result_passthrough(self) -> None:
        qr = QueryResult(response="custom")
        sdk = SDKMock()
        sdk.on("test", qr)
        result = await sdk._dispatch(prompt="test")
        assert result is qr

    @pytest.mark.asyncio(loop_scope="function")
    async def test_callable_handler(self) -> None:
        captured: list[dict[str, object]] = []

        async def handler(**kwargs: object) -> QueryResult:
            captured.append(kwargs)  # type: ignore[arg-type]
            return QueryResult(response="handled")

        sdk = SDKMock()
        sdk.on("test", handler)
        result = await sdk._dispatch(prompt="test prompt", extra="data")
        assert result.response == "handled"
        assert captured[0]["prompt"] == "test prompt"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_callable_handler_exception_still_records_call(self) -> None:
        async def failing_handler(**kwargs: object) -> QueryResult:
            raise RuntimeError("handler failed")

        sdk = SDKMock()
        sdk.on("test", failing_handler)
        with pytest.raises(RuntimeError, match="handler failed"):
            await sdk._dispatch(prompt="test prompt")
        assert sdk.call_count == 1
        assert "test prompt" in sdk.prompts

    @pytest.mark.asyncio(loop_scope="function")
    async def test_callable_exception_does_not_consume_times(self) -> None:
        """Failed callable does NOT consume a times slot so assert_all_consumed flags it."""
        async def failing_handler(**kwargs: object) -> QueryResult:
            raise RuntimeError("transient failure")

        sdk = SDKMock()
        sdk.on("test", failing_handler, times=1)
        with pytest.raises(RuntimeError, match="transient failure"):
            await sdk._dispatch(prompt="test")
        # times=1 should NOT be consumed when the handler raised
        assert sdk._routes[0].times == 1
        # The call is still recorded
        assert sdk.call_count == 1

    @pytest.mark.asyncio(loop_scope="function")
    async def test_callable_exception_assert_all_consumed_flags_unconsumed(self) -> None:
        """assert_all_consumed flags unconsumed slot after callable with times=1 raises."""
        async def failing_handler(**kwargs: object) -> QueryResult:
            raise RuntimeError("sdk error")

        sdk = SDKMock()
        sdk.on("test", failing_handler, times=1)
        with pytest.raises(RuntimeError, match="sdk error"):
            await sdk._dispatch(prompt="test")
        # Route raised — times was NOT consumed, so assert_all_consumed should fail
        with pytest.raises(AssertionError, match="unconsumed invocation"):
            sdk.assert_all_consumed()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_times_decremented_after_handler(self) -> None:
        """route.times is decremented after handler succeeds."""
        times_during_handler: int | None = None

        async def check_handler(**kwargs: object) -> QueryResult:
            nonlocal times_during_handler
            times_during_handler = route.times
            return QueryResult(response="ok")

        sdk = SDKMock()
        sdk.on("test", check_handler, times=1)
        route = sdk._routes[0]
        await sdk._dispatch(prompt="test")
        assert times_during_handler == 1, "times should not be decremented during handler"
        assert route.times == 0, "times should be 0 after handler completes"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_first_match_wins(self) -> None:
        sdk = SDKMock()
        sdk.on("test", "first")
        sdk.on("test", "second")
        result = await sdk._dispatch(prompt="test")
        assert result.response == "first"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_times_limiting(self) -> None:
        sdk = SDKMock()
        sdk.on("test", "limited", times=1)
        sdk.on("test", "unlimited")
        r1 = await sdk._dispatch(prompt="test")
        r2 = await sdk._dispatch(prompt="test")
        assert r1.response == "limited"
        assert r2.response == "unlimited"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_on_any_matches_everything(self) -> None:
        sdk = SDKMock()
        sdk.on_any("catch-all")
        result = await sdk._dispatch(prompt="anything at all")
        assert result.response == "catch-all"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_on_any_unlimited_by_default(self) -> None:
        sdk = SDKMock()
        sdk.on_any("always")
        r1 = await sdk._dispatch(prompt="first")
        r2 = await sdk._dispatch(prompt="second")
        r3 = await sdk._dispatch(prompt="third")
        assert r1.response == "always"
        assert r2.response == "always"
        assert r3.response == "always"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_on_any_with_times_limit(self) -> None:
        sdk = SDKMock()
        sdk.on_any("once", times=1)
        await sdk._dispatch(prompt="first")
        with pytest.raises(AssertionError, match="No matching response"):
            await sdk._dispatch(prompt="second")


# ---------------------------------------------------------------------------
# SDKMock fallback queue
# ---------------------------------------------------------------------------


class TestSDKMockFallback:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_fallback_queue_consumed_in_order(self) -> None:
        sdk = SDKMock()
        sdk.then("first", "second", "third")
        r1 = await sdk._dispatch(prompt="a")
        r2 = await sdk._dispatch(prompt="b")
        r3 = await sdk._dispatch(prompt="c")
        assert r1.response == "first"
        assert r2.response == "second"
        assert r3.response == "third"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_routes_take_priority_over_fallback(self) -> None:
        sdk = SDKMock()
        sdk.on("special", "routed")
        sdk.then("fallback1", "fallback2")
        r1 = await sdk._dispatch(prompt="special request")
        r2 = await sdk._dispatch(prompt="generic request")
        assert r1.response == "routed"
        assert r2.response == "fallback1"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_error_when_nothing_matches(self) -> None:
        sdk = SDKMock()
        sdk.on("specific", "response", label="my-route")
        with pytest.raises(AssertionError, match="No matching response"):
            await sdk._dispatch(prompt="something else entirely")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_error_message_no_ellipsis_on_short_prompt(self) -> None:
        """Short prompts should not show a trailing '...' implying truncation."""
        sdk = SDKMock()
        short_prompt = "short"
        with pytest.raises(AssertionError, match="short\n") as exc_info:
            await sdk._dispatch(prompt=short_prompt)
        # The prompt line should NOT end with '...'
        assert f"  {short_prompt}..." not in str(exc_info.value)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_error_message_ellipsis_on_long_prompt(self) -> None:
        """Prompts longer than 200 chars should show '...' to indicate truncation."""
        sdk = SDKMock()
        long_prompt = "x" * 250
        with pytest.raises(AssertionError) as exc_info:
            await sdk._dispatch(prompt=long_prompt)
        assert "..." in str(exc_info.value)

    def test_then_rejects_callable(self) -> None:
        async def handler(**kwargs: object) -> QueryResult:
            return QueryResult(response="x")

        sdk = SDKMock()
        with pytest.raises(TypeError, match="only accepts static responses"):
            sdk.then(handler)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# SDKMock verification
# ---------------------------------------------------------------------------


class TestSDKMockVerification:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_calls_recorded(self) -> None:
        sdk = SDKMock()
        sdk.then("r1", "r2")
        await sdk._dispatch(prompt="p1")
        await sdk._dispatch(prompt="p2")
        assert sdk.call_count == 2
        assert sdk.prompts == ["p1", "p2"]
        assert sdk.calls[0].result.response == "r1"
        assert sdk.calls[1].matched_label is None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_label_recorded(self) -> None:
        sdk = SDKMock()
        sdk.on("test", "response", label="my-label")
        await sdk._dispatch(prompt="test")
        assert sdk.calls[0].matched_label == "my-label"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_assert_called_times(self) -> None:
        sdk = SDKMock()
        sdk.then("r")
        await sdk._dispatch(prompt="p")
        sdk.assert_called(times=1)
        with pytest.raises(AssertionError, match="Expected 2"):
            sdk.assert_called(times=2)

    def test_assert_called_no_calls(self) -> None:
        sdk = SDKMock()
        with pytest.raises(AssertionError, match="Expected at least one"):
            sdk.assert_called()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_assert_not_called(self) -> None:
        sdk = SDKMock()
        sdk.assert_not_called()  # should pass
        sdk.then("r")
        await sdk._dispatch(prompt="p")
        with pytest.raises(AssertionError, match="Expected no SDK calls"):
            sdk.assert_not_called()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_assert_all_consumed(self) -> None:
        sdk = SDKMock()
        sdk.then("r1", "r2")
        await sdk._dispatch(prompt="p1")
        with pytest.raises(AssertionError, match="1 unconsumed"):
            sdk.assert_all_consumed()
        await sdk._dispatch(prompt="p2")
        sdk.assert_all_consumed()  # should pass

    @pytest.mark.asyncio(loop_scope="function")
    async def test_assert_all_consumed_checks_times_limited_routes(self) -> None:
        """assert_all_consumed also checks times-limited routes."""
        sdk = SDKMock()
        sdk.on("test", "response", times=2)
        await sdk._dispatch(prompt="test")
        # 1 remaining invocation on the route
        with pytest.raises(AssertionError, match="1 unconsumed invocation"):
            sdk.assert_all_consumed()
        await sdk._dispatch(prompt="test")
        sdk.assert_all_consumed()  # should pass

    def test_assert_all_consumed_ignores_unlimited_routes(self) -> None:
        """Unlimited routes (times=None) are not checked."""
        sdk = SDKMock()
        sdk.on("test", "response")  # times=None (unlimited)
        sdk.assert_all_consumed()  # should pass

    @pytest.mark.asyncio(loop_scope="function")
    async def test_assert_prompt_contained(self) -> None:
        sdk = SDKMock()
        sdk.then("r")
        await sdk._dispatch(prompt="hello world")
        sdk.assert_prompt_contained("hello")
        with pytest.raises(AssertionError, match="Expected prompt to contain"):
            sdk.assert_prompt_contained("goodbye")

    def test_assert_prompt_contained_no_calls(self) -> None:
        sdk = SDKMock()
        with pytest.raises(AssertionError, match="no SDK calls recorded"):
            sdk.assert_prompt_contained("anything")


# ---------------------------------------------------------------------------
# SDKMock fluent chaining
# ---------------------------------------------------------------------------


class TestSDKMockChaining:
    def test_on_returns_self(self) -> None:
        sdk = SDKMock()
        result = sdk.on("a", "b")
        assert result is sdk

    def test_then_returns_self(self) -> None:
        sdk = SDKMock()
        result = sdk.then("a", "b")
        assert result is sdk

    def test_on_any_returns_self(self) -> None:
        sdk = SDKMock()
        result = sdk.on_any("a")
        assert result is sdk


# ---------------------------------------------------------------------------
# SDKMock.patch() context manager
# ---------------------------------------------------------------------------


class TestSDKMockPatch:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_patch_context_manager(self) -> None:
        sdk = SDKMock()
        sdk.then("patched response")
        with sdk.patch("maestro.consciousness"):
            from maestro.consciousness import query
            result = await query(prompt="test")  # type: ignore[call-arg]
            assert result.response == "patched response"

    def test_patch_cleans_up_on_enter_failure(self) -> None:
        """If a module name is invalid, already-started patchers must be stopped."""
        sdk = SDKMock()
        sdk.then("resp")
        # First module is valid, second is bogus — should raise and clean up
        with pytest.raises((ModuleNotFoundError, AttributeError)), \
             sdk.patch("maestro.consciousness", "maestro.nonexistent_module_xyz"):
            pass  # pragma: no cover
        # Verify maestro.consciousness.query is NOT left patched
        from maestro.consciousness import query as q
        assert q is not sdk.mock, "Patcher leaked: maestro.consciousness.query still patched"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_patch_extras(self) -> None:
        sdk = SDKMock()
        sdk.then("response")
        captured: list[str] = []

        def side_effect(cmd: list[str], **kwargs: object) -> None:
            captured.append(str(cmd))

        with sdk.patch(
            "maestro.consciousness",
            extras={"maestro.evolution.subprocess.run": side_effect},
        ):
            import maestro.evolution as evo_mod
            evo_mod.subprocess.run(["test"])  # type: ignore[arg-type]
            assert len(captured) == 1

    def test_patch_extras_with_dict_value(self) -> None:
        """Extras dict values are used as replacement objects, not patch kwargs."""
        sdk = SDKMock()
        replacement = {"key": "value"}
        with sdk.patch(
            extras={"maestro.evolution.subprocess": replacement},
        ):
            import maestro.evolution as evo_mod
            assert evo_mod.subprocess == replacement  # type: ignore[comparison-overlap]

    def test_exit_surfaces_all_patcher_errors_as_exception_group(self) -> None:
        """When multiple p.stop() calls fail, all errors are surfaced."""
        from tests.component.sdk_mock import _SDKMockContext

        sdk = SDKMock()
        ctx = _SDKMockContext(sdk, [], {})

        # Manually inject fake patchers that raise on stop()
        class _BadPatcher:
            def __init__(self, msg: str) -> None:
                self._msg = msg

            def stop(self) -> None:
                raise RuntimeError(self._msg)

        ctx._patchers = [_BadPatcher("err1"), _BadPatcher("err2")]  # type: ignore[list-item]
        with pytest.raises(ExceptionGroup) as exc_info:
            ctx.__exit__(None, None, None)
        assert len(exc_info.value.exceptions) == 2

    def test_exit_single_patcher_error_raises_directly(self) -> None:
        """A single stop() failure raises the error directly (not ExceptionGroup)."""
        from tests.component.sdk_mock import _SDKMockContext

        sdk = SDKMock()
        ctx = _SDKMockContext(sdk, [], {})

        class _BadPatcher:
            def stop(self) -> None:
                raise RuntimeError("single error")

        ctx._patchers = [_BadPatcher()]  # type: ignore[list-item]
        with pytest.raises(RuntimeError, match="single error"):
            ctx.__exit__(None, None, None)

    def test_exit_attaches_patcher_errors_as_notes_when_body_raised(self) -> None:
        """Patcher stop errors are attached as __notes__ on the original exception."""
        from tests.component.sdk_mock import _SDKMockContext

        sdk = SDKMock()
        ctx = _SDKMockContext(sdk, [], {})

        class _BadPatcher:
            def stop(self) -> None:
                raise RuntimeError("stop failed")

        ctx._patchers = [_BadPatcher()]  # type: ignore[list-item]
        original = ValueError("original")
        # Simulate __exit__ called with an active exception (args[0] is not None)
        ctx.__exit__(ValueError, original, None)
        # No error raised — stop errors attached as notes on the original exception
        assert hasattr(original, "__notes__")
        assert any("patcher stop failed" in n for n in original.__notes__)

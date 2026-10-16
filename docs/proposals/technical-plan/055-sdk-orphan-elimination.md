# 055: SDK Orphan-Task Elimination at the Source

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/sdk.py`, `maestro/agent.py`, `maestro/executor.py`, `tests/test_sdk.py`, `tests/test_executor.py`, `tests/test_session_reuse_phases.py`, `docs/proposals/README.md` |
| **Derived from** | Issue #1719 auto-RCA; prior PRs #1648, #1643, #1656, #1696, #1704 |

## Description

> Status updated to shipped on 2026-06-01 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Issue #1719 is the latest in a recurring bug class: residual
`async_generator_athrow` (and adjacent) tasks leak from completed Claude
Agent SDK calls and fire `CancelledError` into later non-SDK awaits,
crashing maestro's autonomous loop. Five prior PRs each shipped
*containment* — drains and `CancelledError` catches at specific call
sites — but never addressed the producer. The auto-RCA on #1719 proposed
a sixth containment guard at `agent.py`; that fix is forbidden by
the user feedback rule "no new smoothing layers; let crashes propagate
so triage fires." This proposal addresses the producer instead.

Mechanical root cause (firsthand-verified against
`claude-agent-sdk==0.1.0`): `claude_agent_sdk/_internal/query.py`
— `Query.close()` cancels every entry in `self._child_tasks` and **does
not await any of them**. Those tasks (spawned by `Query.spawn_task` at
`_internal/query.py` with coroutines `stream_input`,
`wait_for_result_and_end_input`, `_handle_control_request`) get a
`task.cancel()` call which only schedules a `CancelledError` for the
cancelled task's next `await`. The asyncio scheduler then resumes that
task whenever it next gets the loop — which can be inside an arbitrary
later `await` in maestro. Issue #1719's 92-second delay between
`gen.aclose()` and the crash is consistent with this scheduling-deferred
mechanism.

End state once this lands: every SDK call boundary in `maestro/sdk.py`
drains residual SDK child tasks the same way it drains athrow tasks
today, so the existing six vulnerable awaits in `agent.py` stop seeing leaked cancels — without
adding any new `CancelledError` handlers in `agent.py`.

## Relevant Findings

- `claude_agent_sdk/_internal/query.py` — `Query.close()` calls
  `task.cancel()` on each entry in `self._child_tasks` and returns
  without awaiting. This is the producer.
- `claude_agent_sdk/_internal/query.py` — `Query.spawn_task`
  registers child tasks; `add_done_callback` removes them on completion.
- `claude_agent_sdk/_internal/query.py` — control-request handler
  spawn (`_handle_control_request` coroutine).
- `claude_agent_sdk/_internal/client.py` —
  `wait_for_result_and_end_input` and `stream_input` coroutine spawns.
- `claude_agent_sdk/client.py` — `ClaudeSDKClient.disconnect()`
  calls `Query.close()`. Every maestro disconnect site is therefore a
  potential leak point.
- `maestro/sdk.py` — `_collect_athrow_tasks()`, the template the
  new `_collect_sdk_child_tasks()` mirrors. Same task-introspection
  shape, different name set.
- `maestro/sdk.py` — `_drain_athrow_tasks()`, the template for
  the new drain. Same active-cancel-then-`asyncio.wait` shape (#1704),
  same finally-safe never-raise discipline, longer default timeout
  (2.0 s vs 0.5 s) because SDK child tasks may be mid-stdin-write to
  the subprocess.
- `maestro/sdk.py` — `query()`'s finally block already calls
  `_drain_athrow_tasks` after `gen.aclose()`. The new drain lands
  immediately above the athrow drain.
- `maestro/sdk.py` — `create_session()`'s two error-fallback
  branches each call `client.disconnect()` inside `contextlib.suppress`.
  These are existing leak sites today.
- `maestro/agent.py` — five existing
  disconnect call sites in `agent.py`, all already wrapped in
  `contextlib.suppress(Exception)` or `try/except Exception:`. Each is
  a potential producer of the leak.
- `maestro/executor.py` — `Executor.end_session()` disconnects the
  per-task implementation session opened at `executor.py` via
  `create_session()`. Reached from `agent.run()` at `agent.py`,
  so a leaked orphan here fires into the same
  `agent.run()` awaits the agent.py call sites cover. Wrapped in
  `try/except Exception:` already.
- `maestro/agent.py` — the six
  vulnerable `await asyncio.wait_for(self._ensure_shutdown_event().wait(),
  ...)` awaits where leaked cancels have crashed the loop in production.
- `tests/test_sdk.py` (`TestSnapshotAthrowTasks`) and
  `tests/test_sdk.py` (`TestDrainAthrowTasks`) — the test
  patterns the new tests mirror.

## Implementation Plan

1. **`_SDK_CHILD_TASK_NAMES` constant + collector.** `maestro/sdk.py` —
   add a `frozenset` of the three SDK child-task coroutine names near
   the athrow helpers. Add `_collect_sdk_child_tasks()` next to
   `_collect_athrow_tasks()`, differing only in the name-set predicate.
2. **`_drain_sdk_child_tasks(label, *, timeout_seconds=2.0)`.**
   `maestro/sdk.py` — sibling of `_drain_athrow_tasks`. Same
   active-cancel-then-`asyncio.wait` shape, same return contract, same
   finally-safe never-raise discipline.
3. **`disconnect_session(client)` wrapper.** `maestro/sdk.py` (end of
   file) — `contextlib.suppress(Exception)` around `await
   client.disconnect()`, then the new SDK-child drain, then the existing
   athrow drain. Drain order matters: child-task cancel scopes can
   spawn athrow tasks for inner generators, so the second drain must
   run after the first.
4. **Wire the drain into `query()`'s finally.** `maestro/sdk.py` — one
   call to `_drain_sdk_child_tasks("sdk.query post-aclose")` between
   `await gen.aclose()` and the existing `_drain_athrow_tasks` call.
5. **Substitute `disconnect_session()` for the eight existing disconnect
   sites.** Two in `maestro/sdk.py` (`create_session` error fallbacks),
   five in `maestro/agent.py` (`agent.run`, `implement_plan`,
   `implement_approved_plans` rate-limit cleanup, task generation
   session D, end-of-iteration sessions A/B), and one in
   `maestro/executor.py` (`Executor.end_session`, the per-task
   implementation session reached from `agent.run`). All sites already
   wrap the disconnect in `contextlib.suppress` / `try/except
   Exception:`, which the substitution preserves verbatim.
6. **Tests.** `tests/test_sdk.py` — `TestCollectSdkChildTasks`,
   `TestDrainSdkChildTasks`, and `TestDisconnectSession` mirror the
   athrow-helper test classes. Synthetic tasks named after the three
   SDK child-task coroutines, parked on `asyncio.Event.wait()`, prove
   collection and drain coverage. The disconnect-session test pins the
   ordering invariant (disconnect → child drain → athrow drain) and
   the never-raise contract.
7. **Documentation.** This proposal file.

## Testing Strategy

- `TestCollectSdkChildTasks` — three synthetic tasks named
  `stream_input` / `wait_for_result_and_end_input` /
  `_handle_control_request` are all picked up; an unrelated task is
  not; introspection failures from `asyncio.all_tasks` and individual
  `get_coro()` calls are swallowed at DEBUG.
- `TestDrainSdkChildTasks` — drain is silent on idle, drains a
  completing child task, actively cancels a stuck child task,
  WARNINGs on shielded-orphan timeout, swallows unexpected
  `asyncio.wait` failures.
- `TestDisconnectSession` — pins the SDK-child-drain → athrow-drain
  ordering, the swallow-on-disconnect-failure contract, and the
  drains-run-even-when-disconnect-raises invariant.
- `TestDrainAthrowTasks` — must continue to pass unchanged; the new
  drain runs alongside, not instead of, the athrow drain.

All tests honour the project rules: `mem_fs` is unused (no I/O), no
real network, no real `sleep` longer than `await asyncio.sleep(0)`,
externals mocked. Per-test runtime stays well under 5 s.

## Risk Assessment

The new helpers are additions (no existing helper is modified). The
`disconnect_session` substitutions are pure aliases at the call sites:
each caller already wraps the disconnect in `contextlib.suppress` or
`try/except Exception:`, so the new helper's `contextlib.suppress`
redundancy is a no-op. The `_drain_athrow_tasks` shape it imitates has
been live in production for ten weeks across PRs #1648 / #1696 / #1704
without a single drain-side regression.

Rollback shape: revert the two new helpers + the `disconnect_session`
wrapper in `maestro/sdk.py`, and the seven `disconnect_session` →
`client.disconnect()` substitutions. Pure deletions; no schema
migration; no behavioural drift inside the existing helpers.

The architectural invariants this honours:

- **Crashes must stay apparent** — no new `CancelledError` handlers are
  added in `agent.py`. If the SDK-boundary drain ever misses an orphan,
  the loop still crashes loudly and crash-detection still fires.
- **Journal over logs** — the new drain emits one WARNING per shielded
  orphan, the same telemetry shape the athrow drain established.
- **No bypass of quality gates** — the substitution is mechanical; no
  `# noqa`, no `# type: ignore`.

## Out of scope

Anything that requires upstream SDK changes. The companion deliverable
is a separate-track GitHub issue against `anthropics/claude-agent-sdk`
proposing the upstream fix:

```python
# In Query.close():
for task in list(self._child_tasks):
    task.cancel()
await asyncio.gather(*self._child_tasks, return_exceptions=True)
```

Once upstream lands, the local drain becomes a near-no-op (cheap to
leave in place as defence-in-depth).

Also out of scope: `gc.collect()` calls, `set_asyncgen_finalizer`
overrides, forking the SDK package, and any rewrite of the six
vulnerable awaits in `agent.py`. The fix shape is deliberately the
smallest mechanical change that addresses the producer.

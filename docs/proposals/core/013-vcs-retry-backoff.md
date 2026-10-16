# 013: VCS Operation Retry with Backoff

| Field | Value |
|-------|-------|
| **Impact** | LOWER |
| **Status** | proposed |
| **Affects** | `maestro/vcs.py`, `maestro/config.py`, `maestro/toml_config.py` |

## Description

Network-facing VCS and code host operations (`push()`, `create_pr()`, `is_pr_merged()`, `is_pr_closed()`) have no retry logic. A single transient failure — network timeout, GitHub API rate limit, connection reset — fails the entire operation and can waste a full evolution cycle (reflect, propose, implement, then fail on push). The SDK/loop layers already have robust retry and rate-limit backoff; the VCS layer has **not** been wrapped and still fails single-shot.

## Relevant Findings

### Recent context: rate-limit backoff landed at the SDK/loop layer, not VCS
Commit `e58f32e` ("Add rate limit handling with smart backoff and proactive throttling") introduced a `RateLimitError` carrying a `resets_at` timestamp in `maestro/sdk.py` and taught `run_loop()` in `maestro/agent.py` to sleep until that timestamp on hit, apply exponential backoff on other failures (capped at 30 min), and proactively double the sleep interval on `allowed_warning` events. That commit touched `maestro/agent.py`, `maestro/sdk.py`, and their tests — **not `maestro/vcs.py`**. `SubprocessVCS.push()` and every method on `SubprocessCodeHost` still invoke `self._run(...)` exactly once and raise `VCSError` on the first failure. This proposal covers the VCS layer only and is complementary to the already-landed SDK/loop retry infrastructure.

### push() is single-attempt (`vcs.py`)
```python
def push(self, branch: str, *, set_upstream: bool = False) -> None:
    cmd = ["git", "push"]
    if set_upstream:
        cmd.extend(["-u", "origin", branch])
    else:
        cmd.extend(["origin", branch])
    self._run(cmd, timeout=self._push_timeout)
```
Single subprocess call. If it times out or gets a connection reset, `VCSError` is raised and the caller's evolution cycle is over.

### create_pr() is single-attempt (`vcs.py`)
```python
def create_pr(self, title: str, body: str) -> str:
    cmd = [
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        *self._repo_args(),
    ]
    if self._reviewer:
        cmd.extend(["--reviewer", self._reviewer])
    result = self._run(cmd, timeout=self._pr_creation_timeout)
    return result.stdout.strip()
```
Single `gh` CLI invocation. GitHub API rate limits or network issues cause immediate failure — and unlike the SDK layer, there is no `RateLimitError` to short-circuit the retry decision, so the loop's rate-limit-aware sleep cannot distinguish a transient VCS blip from a fatal one.

### is_pr_merged() / is_pr_closed() are single-attempt (`vcs.py`)
Both call `gh pr view` once and swallow errors to return `False`. They're used in `check_merged_plans()`, which only runs on the first loop iteration after a restart — a transient failure here means a merged PR is silently classified as "not merged" and will not be retried until the next restart. Worse, a transient failure can flip a merged PR into the **closed-without-merging** rejection-learning path, polluting `learn_from_rejection()` with spurious realizations.

### sdk.py retry pattern can be reused (`sdk.py`)
```python
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.0
_RETRY_BACKOFF_FACTOR = 2.0

def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, RateLimitError):
        ...
```
The SDK retry helper detects transients, honours `RateLimitError.resets_at`, sleeps `_RETRY_BASE_DELAY * _RETRY_BACKOFF_FACTOR ** (attempt - 1)` between attempts, and logs every retry. The VCS layer needs its own string-based transient detector because `gh`/`git` failures surface through `VCSError` containing the subprocess stderr rather than typed exceptions — but the overall shape (detect → backoff → log → cap) transplants cleanly.

### Evolution cycle waste is high
A typical evolution cycle: `reflect()` (LLM) → `propose_plan()` (LLM) → `implement()` (multi-turn LLM, tool-using, `evolve_max_turns`) → `run_quality_gates()` → `auto_fix()` if needed → `push()` → `create_pr()`. If `push()` or `create_pr()` fails transiently, all the expensive LLM + quality-gate work is wasted. The branch cleanup path discards the implementation and the loop learns nothing from the failure.

## Implementation Plan

### Step 1: Add retry config option

In `maestro/config.py`, add:
```python
vcs_max_retries: int = 3
```

In `maestro/toml_config.py`:
```python
("timeouts", "vcs_max_retries"): ("vcs_max_retries", int),
```

In `maestro.toml`, under `[timeouts]`:
```toml
vcs_max_retries = 3
```

### Step 2: Add transient error detection for VCS

In `maestro/vcs.py`, add:
```python
import time

_TRANSIENT_PATTERNS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "rate limit",
    "502",
    "503",
    "504",
    "could not resolve host",
    "network is unreachable",
    "temporary failure",
    "ssl",
)

_FATAL_PATTERNS = (
    "permission denied",
    "authentication failed",
    "not found",
    "invalid",
)


def _is_transient_vcs_error(exc: Exception) -> bool:
    """Return True if the VCS error is likely transient and worth retrying."""
    msg = str(exc).lower()
    if any(p in msg for p in _FATAL_PATTERNS):
        return False
    return any(p in msg for p in _TRANSIENT_PATTERNS)
```

### Step 3: Add retry helper

In `maestro/vcs.py`, add:
```python
def _retry_vcs(
    fn: Callable[..., T],
    *args: Any,
    max_retries: int = 3,
    backoff_base: float = 2.0,
    **kwargs: Any,
) -> T:
    """Retry a VCS operation with exponential backoff on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt >= max_retries or not _is_transient_vcs_error(exc):
                raise
            delay = backoff_base ** attempt
            logger.warning(
                "VCS operation failed (attempt %d/%d), retrying in %.1fs: %s",
                attempt + 1, max_retries + 1, delay, exc,
            )
            time.sleep(delay)
    raise last_exc  # unreachable, but satisfies type checker
```

### Step 4: Wrap network-facing operations in SubprocessVCS

Update `push()` (`vcs.py`):
```python
def push(self, branch: str, *, set_upstream: bool = False) -> None:
    cmd = ["git", "push"]
    if set_upstream:
        cmd.extend(["-u", "origin", branch])
    else:
        cmd.extend(["origin", branch])
    _retry_vcs(
        self._run, cmd,
        timeout=self._push_timeout,
        max_retries=self._max_retries,
    )
```

Pass `max_retries` through the constructor:
```python
def __init__(self, workspace, *, timeout=30, quick_timeout=10, push_timeout=120, max_retries=3):
    ...
    self._max_retries = max_retries
```

### Step 5: Wrap network-facing operations in SubprocessCodeHost

Update `create_pr()` (`vcs.py`), `is_pr_merged()` (`vcs.py`), `is_pr_closed()` (`vcs.py`), plus the issue operations that stream bodies via stdin (`_run_create_issue`, `comment_issue`, `close_issue`, `list_issues`). The best target is the shared `self._run(...)` call, which is the only place network I/O leaves the class. Wrap that one method:
```python
def _run(self, cmd: list[str], *, timeout: float | None = None,
         input_data: str | None = None) -> subprocess.CompletedProcess[str]:
    def _once() -> subprocess.CompletedProcess[str]:
        return self._run_once(cmd, timeout=timeout, input_data=input_data)
    return _retry_vcs(_once, max_retries=self._max_retries)
```
Rename the current `_run` body to `_run_once` so the retry wrapper composes cleanly, then every `gh` invocation inherits the retry for free. **Do not** retry `create_issue()`'s lazy `gh label create --force` fallback (it is already a "try once, then retry with fresh labels" path — stacking retries on top risks label-creation thundering herds). Gate the wrapper on an explicit `retryable=True` kwarg if necessary.

Pass `max_retries` through the `SubprocessCodeHost` constructor.

### Step 6: Wire config to constructors

In `maestro/cli.py` and `maestro/evolution.py` where VCS/CodeHost are instantiated:
```python
SubprocessVCS(config.workspace, ..., max_retries=config.vcs_max_retries)
SubprocessCodeHost(config.github_repo, ..., max_retries=config.vcs_max_retries)
```

## Testing Strategy

1. **Retry on transient error** — Mock subprocess to fail with "connection reset" twice then succeed, verify 3 attempts made
2. **No retry on fatal error** — Mock "permission denied", verify single attempt and raise
3. **Backoff timing** — Mock `time.sleep`, verify exponential delays (1s, 2s, 4s)
4. **Max retries respected** — Mock all attempts failing, verify exactly `max_retries + 1` attempts
5. **Config wired through** — Verify `vcs_max_retries` from config reaches constructor
6. **Transient detection** — Test `_is_transient_vcs_error()` with various error messages
7. **push() retries** — Verify push uses retry wrapper
8. **create_pr() retries** — Verify PR creation uses retry wrapper

Run: `python3.12 -m pytest tests/ -v -k "retry or transient_vcs"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Retry delays slow down operations | Low | Only on failures; backoff is 2-4-8 seconds; fast for transient issues |
| Retry masks persistent errors | Low | Fatal pattern detection; max_retries cap; warning logs on each retry |
| time.sleep() blocks event loop | Medium | VCS operations are already synchronous (subprocess); sleep is fine here |
| Retry creates duplicate PRs | Low | `create_pr()` is idempotent-ish; GitHub rejects duplicate head branches |
| Test complexity | Low | Same pattern as sdk.py retry tests; well-understood approach |
| Interaction with loop-level backoff (`e58f32e`) | Low | SDK errors surface as `SDKError` / `RateLimitError` and are handled in `run_loop()`; VCS errors surface as `VCSError` and never reach that path. The two retry layers are mutually exclusive by exception type. |
| Double-retry on `create_issue()` label fallback | Low | The fallback already retries once after `gh label create --force`; wrap via `retryable=False` kwarg on the fallback path so only the outer invocation retries. |

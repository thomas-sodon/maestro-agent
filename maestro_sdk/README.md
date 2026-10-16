# maestro_sdk

A Python SDK that drives the `claude` Claude Code CLI **as if it were a human user**: paced keystrokes, realistic terminal allocation, post-response heartbeat, strictly sequential flow.

> **Not to be confused with `maestro/sdk.py`.** That module is Maestro's wrapper around the official `claude-agent-sdk` and bulk-writes inputs at machine speed. This package (`maestro_sdk`) is a separate, standalone library that drives the `claude` CLI through a pty at human cadence. The data-contract shapes intentionally mirror `maestro/sdk.py` so a future adapter is straightforward, but no code is shared and the two are not interchangeable.

This is not "another way to call the Claude API". It is a way to exercise the CLI at human cadence — useful when:

- You want to surface TUI/UX issues that bulk-write SDKs hide.
- You're building automation on a `claude` CLI + OAuth login with no `ANTHROPIC_API_KEY`.
- Downstream rate-limiters / UI state machines assume between-keystroke time.

## Status

Early. The foundation layer (`cadence`, `errors`, `types`, `_logging`), the `screen` emulator, the `pty_runner`, the output `parser`, the `approvals` handler, the public `AsyncClient`, and the persistent `Session` (one pty runner kept alive across turns, respawning with the CLI's `--resume <id>` from the captured session id so continuity survives an EOF or a crash) have landed. The Maestro-side `CliPacedProvider` (`[sdk] provider = "cli_paced"` or a per-phase `[sdk] <phase>_provider` override, in `maestro/providers/cli_paced.py`) backs the four SDK seam functions through Maestro's provider registry at full capability parity (proposal 061): resume/seed-prompt forwarding, `complete_on` early-exit, `[sdk.cli_paced]` cadence and stream-idle overrides, the `claude`-logged-in credential precondition, and transcript-sidecar usage metering with an honest `unmetered` fallback — though no production phase routes through it by default. Per-CLI-version layout configuration ships as versioned **marker packs** (`marker_packs.py`, proposal 075): a frozen `MarkerPack` bundles all four marker families (`ClientMarkers`/`ParserMarkers`/`ApprovalMarkers`/`SlashProbeMarkers`) under an inclusive CLI-version range, `detect_cli_version()` probes `claude --version`, and `select_marker_pack()` returns the newest covering pack or honestly `None` — wired Maestro-side via `[sdk.cli_paced] marker_pack = "auto" | "<name>" | "none"`, with a layout-drift sentinel (`QueryResult.idle_marker_missed` + one-shot WARNINGs) so a CLI upgrade reads as a loud degradation rather than a silent return to EOF-only turns. The registry currently ships no packs: the claude 2.1.x TUI paints no in-turn session id and its idle hint renders unstably through the deliberate vt100-subset `Screen` emulator, so authoring the first real pack awaits emulator-fidelity work (see `docs/sdk-internals.md`). What remains of the rollout track is the cross-process in-process-tools launcher entrypoint (CLI-side action-gate replication shipped with proposal 066).

## Cadence — the heart of the package

```python
from maestro_sdk import Cadence

c = Cadence(seed=42)
c.delay_for("h", prev=None)        # seconds to wait before pressing 'h'
c.heartbeat_for(answer_chars=350)  # seconds to wait after a 350-char response
c.winsize()                        # (cols, rows) — realistic, jittered
```

The `Cadence` object owns every timing constant in the package. There is no `sleep(...)` call anywhere else — if you want different rhythm, pass a different `Cadence`.

For tests, `InstantCadence` returns zero for every delay:

```python
from maestro_sdk import InstantCadence
c = InstantCadence()  # all delays are zero; winsize is deterministic
```

## Screen — turning a repaint into text

The `claude` CLI repaints its terminal rather than appending text. `Screen` is a
minimal vt100-subset emulator (cursor moves, ED/EL erasure, scroll region, SGR
discarded) that turns a raw byte stream into the text a terminal would show, so
a later parser can tell incremental streaming apart from a full re-render.

```python
from maestro_sdk import Screen

s = Screen(cols=80, rows=24)
s.feed(b"\x1b[2Jhello\r\nworld")  # clear screen, then two lines
s.display()                       # "hello\nworld"
s.cursor_row, s.cursor_col        # (1, 5)
```

It is pure (no I/O) and stateful across `feed` calls — split escape sequences and
truncated UTF-8 lead bytes carry into the next feed, mirroring pty delivery.

## PtyRunner — the bytes in and out

`PtyRunner` spawns the CLI in a pseudo-terminal sized from `Cadence.winsize()`,
types one keystroke at a time at the cadence-prescribed delay, and drains the
master fd into a queue you read chunk by chunk.

```python
from maestro_sdk import Cadence, PtyRunner

async with PtyRunner(["claude"], Cadence(seed=1)) as runner:
    await runner.send_text("hello\r")        # paced, char by char
    chunk = await runner.aread_chunk()        # b"" signals end of stream
```

It owns the child and the master fd: `aclose` (or leaving the `async with`)
walks the child through a `SIGTERM` → `SIGKILL` ladder, tears the reader task
down, and closes the fd — idempotently — so a cancelled query leaks neither a
process nor a descriptor.

## Parser — snapshots into deltas, a response, and tool calls

`Parser` sits above `Screen`. Feed it the stream of `display()` snapshots; each
`feed` returns only the text revealed since the last one (so a repaint emits
nothing), and across the stream it accumulates the answer into `response` and
the recognised tool invocations into `tool_calls`.

```python
from maestro_sdk import Parser, ParserMarkers

# Markers are layout policy, tuned per CLI version — not baked into the parser.
# (Illustrative glyphs: discover the real ones by recording a session.)
markers = ParserMarkers(
    thinking_open=("✻ Thinking",),       # opens the reasoning region
    thinking_close=("✦ Answer",),        # ends it; the answer follows
    tool_call=r"^[⏺●]\s*(?P<name>[A-Za-z][\w-]*)\((?P<args>.*)\)$",
    chrome=("│", "╭", "╰", "? for shortcuts"),
)
parser = Parser(markers)
for snapshot in snapshots:               # each is a Screen.display() string
    for delta in parser.feed(snapshot):
        print(delta.kind, repr(delta.text))   # "thinking" | "answer"
parser.response                          # accumulated answer text
parser.tool_calls                        # [ToolCall(name="Bash", args={"raw": "ls"}), ...]
```

The parser ships the **mechanism**; the **markers** are configuration, the same
way `Cadence` owns timing. The default `ParserMarkers()` matches nothing, so the
parser degrades to "every line is answer text, no tool calls" rather than
guessing at one CLI version's glyphs. It is pure (no I/O) and diffs incrementally
by longest common prefix, so append-only streaming is exact; output that scrolls
off the top of the pty is a documented gap — size the window for the expected
answer length.

## Approvals — deny by default, opt in to approve

When the CLI wants to run a tool it pauses on an approval modal. `ApprovalHandler`
recognises that modal in a screen snapshot and applies a **deny-by-default**
policy: with no policy (or a policy that returns `False`), a detected modal raises
`ApprovalRequiredError` instead of guessing. Approving is opt-in — pass a policy.

```python
from maestro_sdk import ApprovalHandler, ApprovalMarkers, approve_all

# Layout policy, tuned per CLI version — like ParserMarkers, not baked in.
markers = ApprovalMarkers(
    prompt=("Do you want to proceed",),                       # detection gate
    option=r"^[❯>]?\s*(?P<key>\d+)\.\s*(?P<label>.+)$",       # numbered choices
    tool=r"^[⏺●]?\s*(?P<name>[A-Za-z][\w-]*) command$",       # the tool being run
    approve_keys=("1",),                                      # which option approves
)

# No policy: a modal surfaces to the operator by raising.
ApprovalHandler(markers).resolve(snapshot)                    # -> ApprovalRequiredError

# Opt in to approve everything; resolve returns the keystroke to type.
ApprovalHandler(markers, policy=approve_all).resolve(snapshot)  # -> "1"
```

Like the parser, it ships the **mechanism** and leaves the **markers** to
configuration; the default `ApprovalMarkers()` matches nothing, so no snapshot is
ever read as a modal. It is pure: `detect` is a function of one snapshot and
`resolve` a function of the request and the policy. The client slice sends the
returned keystrokes through the pty and waits for the modal to clear. A
sophisticated per-tool policy (rather than the blanket `approve_all`) is left to a
later approval-policy proposal so the trust chain stays explicit.

## AsyncClient — putting it together

`AsyncClient` composes the runner, screen, parser, and approval handler into one
operation: type a prompt at human cadence, watch the CLI answer, hand back a
`QueryResult`. A query is a strict sequence — spawn, type the prompt + Enter,
stream the reply, then wait `Cadence.heartbeat_for(answer_length)` before
returning — and each phase is timed in `QueryResult.timing` so a caller can prove
the pacing happened.

```python
from maestro_sdk import AsyncClient, Cadence, ClientMarkers, ParserMarkers

client = AsyncClient(
    Cadence(seed=1),
    command=("claude",),
    parser_markers=ParserMarkers(thinking_open=("✻ Thinking",), thinking_close=("✦ Answer",)),
    # Whole-screen conditions, tuned per CLI version like the other markers.
    client_markers=ClientMarkers(
        idle=("│ > ",),                       # input prompt redrew → turn complete
        rate_limit=("Rate limit",),           # → RateLimitError
        session_expired=("Session expired",), # → SessionExpiredError
    ),
)

result = await client.query("summarise this repo")
result.response          # the accumulated answer text
result.tool_calls        # tool invocations the parser recognised
result.usage             # None — the TUI surfaces no token counts (never zero-filled)
result.timing.total_ms   # send + stream + heartbeat, in milliseconds

session = client.create_session()
first = await session.query("hi")
await session.aclose()   # a closed session refuses further queries
```

**Two timeouts guard the stream**, both `QueryOptions` fields. The first read
after sending is bounded by `startup_timeout_seconds` (no output → a
`"timeout"`-category `CLITimeoutError`); every later read by
`stream_idle_timeout_seconds` (a silent, incomplete turn → `"stream_stall"`).
Either way the child and the pty fd are reclaimed in a `finally`, so a timed-out,
cancelled, or rejected query leaks neither.

**Done-detection is EOF by default.** With no `ClientMarkers.idle`, end-of-stream
(the CLI process exiting) is the only turn-complete signal — right for a one-shot
invocation. A persistent interactive CLI never EOFs between turns, so configure an
`idle` marker (its input prompt redrawing) or every query stalls. Every error
carries a `category` string mirroring `maestro.sdk.ErrorCategory` by name, so the
Maestro selection seam can re-pack failures into the resume-in-place taxonomy
without this package depending on `maestro`.

## Install

```bash
python3.14 -m pip install -e .[dev]
```

## Quality gates

```bash
python3.14 -m ruff check src/ tests/
python3.14 -m mypy src/
python3.14 -m pytest tests/ -v
```

Most of this package is pure and mock-tested, but `pty_runner` is the
deliberate exception: a pty layer cannot be meaningfully exercised without a
real pseudo-terminal, real signals, and a real child process. Its tests
therefore spawn small hermetic Python fixtures (never the network, never the
installed `claude` CLI) over an actual pty, and the heavier spawn-and-wait
tests carry a relaxed `@pytest.mark.timeout` so a cold interpreter start on a
loaded CI runner does not trip the default per-test budget.

Integration tests against the real `claude` CLI are opt-in:

```bash
RUN_CLAUDE_TESTS=1 python3.14 -m pytest tests/test_integration.py -v
```

# 081: Layout-Independent Idle and Session-ID for the CLI-Paced Driver — Output-Quiescence Turn Completion and Spawn-Pinned `--session-id`

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | partial |
| **Affects** | `maestro_sdk/src/maestro_sdk/{client,session,types,marker_packs}.py`, `maestro/providers/cli_paced.py`, `docs/sdk-internals.md`, `docs/deployment.md`, tests |
| **Depends on** | 061 (shipped), 062 (shipped) |
| **What landed** | Step 1 — output-quiescence turn completion: `QueryOptions.idle_quiescence_seconds` (`maestro_sdk/src/maestro_sdk/types.py`) and the quiescence completion outcome in `AsyncClient._stream` (`maestro_sdk/src/maestro_sdk/client.py`). Step 2 — spawn-pinned session id: `QueryOptions.session_id` (`maestro_sdk/src/maestro_sdk/types.py`), `AsyncClient.create_session(session_id=…)` and the `--session-id` flag (`_SESSION_ID_FLAG`) in `AsyncClient._spawn` (`maestro_sdk/src/maestro_sdk/client.py`), the pinned id floored onto `QueryResult.session_id` in `AsyncClient._drive_turn` with no on-screen marker and reused for the `--resume` respawn, and `Session`'s pinned-id carry (`maestro_sdk/src/maestro_sdk/session.py`). |
| **What is missing** | Step 3 — `MarkerPack.idle_quiescence_seconds` / `pin_session_id` knobs, provider UUID pinning, and the symmetric sidecar drift sentinel; Step 4 — documentation. |

## Description

Proposal 075 shipped the marker-pack *mechanism* (Steps 1–5, 7) but left its
keystone — Step 6, the first real pack — unimplemented, and its own
documentation records why: recorded-transcript analysis of the target CLI
(claude 2.1.172) found the two markers that carry 075's core value are
**unmatchable against that layout** (`docs/sdk-internals.md`, "Registry status
(claude 2.1.x)"). The TUI paints **no session id during a turn** — the id lives
only inside the `/status` dialog, which the driver never opens mid-turn — so
`ClientMarkers.session_id` has nothing to extract; and the idle hint line
(`? for shortcuts …`) renders through the package's deliberate vt100-subset
`Screen` emulator with unstable spacing and stale-cell residue (no wide-glyph
width handling, no OSC consumption), so no stripped-line prefix matches it
reliably. The same paragraph names the fix explicitly: *"Authoring a first real
pack therefore needs `Screen` fidelity work (or an output-quiescence idle
mechanism) first."* That prerequisite is owned by no proposal today.

The blast radius of the two missing signals is exactly the blast radius 075
documented for the empty defaults. Without a turn-complete signal for a
persistent interactive child, every `maestro_sdk.Session` turn ends on EOF and
the child is reaped — session continuity degrades to one-shot-equivalent turns.
Without a session id, no respawn can `claude --resume`, **and**
`_read_sidecar_usage` has no id to locate the transcript sidecar, so every
`cli_paced` row falls back to `unmetered=True` — making the `usage_metering`
capability flag vacuously true. These are the very capabilities 075's pack was
meant to make live; on the 2.1.x layout the pack alone cannot, because the
signals it would encode are not paintable.

This proposal supplies the two **layout-independent** mechanisms that close that
gap without betting on a fragile TUI prefix — the exact fragility 075 fights:

- **Output-quiescence turn completion.** A turn is complete when the rendered
  screen stops changing for a bounded window while the child is still alive,
  not when a hand-authored idle prefix matches. This is the "output-quiescence
  idle mechanism" 075's own analysis names as the alternative to `Screen`
  fidelity work, and it is robust to layout churn by construction.
- **Spawn-pinned session id.** The driver generates a UUID and pins it at spawn
  with `claude --session-id <uuid>` (a real flag on the target CLI — verified on
  2.1.172), so the id is known *a priori* rather than scraped off a screen that
  never prints it. The known id flows onto `QueryResult.session_id`, into the
  `--resume` respawn, and into `_read_sidecar_usage`'s transcript lookup — making
  metering and continuity work with zero layout knowledge.

End state: once both land, 075 Step 6 can author a real 2.1.x pack that *selects*
these mechanisms (a quiescence window plus pinned-id, both carried as per-version
pack policy) and pins only the markers that *are* fixture-recordable
(rate-limit / expired notice text, the slash-probe redraws), instead of being
blocked on idle/session-id prefixes that the layout never paints. Everything
here is inert under the default `[sdk] provider = "anthropic"` and under
`[sdk.cli_paced] marker_pack = "none"`: no pack, no activation.

## Relevant Findings

- `docs/sdk-internals.md` ("Registry status (claude 2.1.x)") — the project's own
  recorded-transcript verdict: no session id is painted during a turn, the idle
  hint line has no reliable stripped-line prefix, and "authoring a first real
  pack therefore needs `Screen` fidelity work (or an output-quiescence idle
  mechanism) first." This proposal is that prerequisite.
- `maestro_sdk/src/maestro_sdk/client.py` (`AsyncClient._stream`) — the turn
  loop already distinguishes three completion outcomes: a matched
  `ClientMarkers.idle` prefix (child clean, resident), a `complete_on` early
  exit (`exited_early`, child mid-generation), and EOF (`ended_on_eof`). A
  quiescence path is a fourth completion outcome of the same shape as the idle
  break — the seam exists; only a new predicate is needed.
- `maestro_sdk/src/maestro_sdk/client.py` (`AsyncClient._extract_session_id`,
  `ClientMarkers.session_id`) — the *only* path that populates
  `QueryResult.session_id` today is scraping a configured on-screen prefix; with
  the 2.1.x layout painting no such line, this path is structurally dead, so a
  spawn-pinned id is the only way to obtain one.
- `maestro_sdk/src/maestro_sdk/client.py` (`AsyncClient._spawn`, `_RESUME_FLAG`)
  — the spawn command already grows `--resume <id>` from a captured id; a
  pinned id reuses exactly this seam, and the same id pins the *first* spawn via
  a new `--session-id` argument.
- `maestro/providers/cli_paced.py` (`_read_sidecar_usage`, `_transcript_path`)
  — metering locates `~/.claude/projects/<slug>/<session-id>.jsonl` from the
  CLI-reported `session_id` and returns `None` (→ honest `unmetered=True`) when
  the id is falsy. A known pinned id is the missing input that flips
  `usage_metering` from vacuous to live.
- `maestro/providers/cli_paced.py` (`configure_cli_paced`,
  `_default_client_factory`, `_marker_pack`) — 075's threading seam already
  resolves a `MarkerPack` at startup and threads its four marker families into
  every `AsyncClient`; two new per-version knobs ride the same seam with no new
  plumbing.
- `maestro/providers/cli_paced.py` (`_warn_session_id_drift`,
  `_warn_idle_drift`) — 075's drift sentinels key off non-empty
  `ClientMarkers.idle` / `ClientMarkers.session_id`; a pack that selects
  quiescence and pinned-id leaves those families empty, so the existing
  sentinels stay correctly inert (there is no on-screen marker to drift). This
  proposal adds the symmetric sentinel for the new path: a pinned id that yields
  no readable sidecar.
- `maestro_sdk/src/maestro_sdk/types.py` (`QueryOptions.startup_timeout_seconds`
  / `stream_idle_timeout_seconds`) — the established home for per-call stream
  timing knobs, where `idle_quiescence_seconds` belongs; the existing
  `stream_idle_timeout_seconds` is the hard outer bound a quiescence window
  always sits inside.
- `claude --version` / `claude --help` on the target binary — confirm both the
  `2.1.172` version the analysis targeted and the `--session-id <uuid>` flag
  ("Use a specific session ID for the session") this proposal pins; mocked in
  unit tests, exercised live under `RUN_CLAUDE_TESTS=1`.
- `maestro_sdk/src/maestro_sdk/screen.py` (module docstring) — the emulator is a
  deliberate vt100 *subset* with `dependencies = []`; adding a wide-glyph width
  table and OSC consumption to chase the idle prefix is the heavier, still
  layout-coupled alternative this proposal rejects in favour of quiescence (see
  Risk Assessment).

## Implementation Plan

Steps land the two mechanisms inert (opt-in via `QueryOptions` / constructor),
then teach the `MarkerPack` to select them and the provider to thread them, then
document. Each step is behaviour-preserving under the default provider and under
`marker_pack = "none"`; an interrupted run leaves the tree working.

1. [x] **Output-quiescence turn completion.**
   `maestro_sdk/src/maestro_sdk/{types,client}.py` — add
   `QueryOptions.idle_quiescence_seconds: float | None` (default `None`,
   today's behaviour). In `AsyncClient._stream`, when it is set and no
   `ClientMarkers.idle` is configured, complete the turn once the rendered
   `Screen.display()` snapshot has been **unchanged for the window** while the
   child is alive — a fourth completion outcome of the same shape as the idle
   break (`ended_on_eof=False`, `exited_early=False`, child clean and resident).
   Quiescence is measured on the *rendered snapshot*, not raw bytes, so a
   cursor blink or an identical repaint counts as quiet. Precedence is
   unchanged: a configured idle marker still wins, a `complete_on` predicate
   still wins, EOF still wins, and the window always sits inside
   `stream_idle_timeout_seconds` as the hard outer bound.
   - *Depends on:* none.
   - *Acceptance:* with `idle_quiescence_seconds` set and no idle marker, a
     fake-runner transcript that stops changing completes the turn as a resident
     child (not EOF, not `exited_early`); a transcript still changing within the
     window does not complete early; a configured idle marker still takes
     precedence over quiescence; `None` leaves the stream loop byte-identical to
     today.
2. [x] **Spawn-pinned session id.**
   `maestro_sdk/src/maestro_sdk/{client,session}.py` — let a caller pin a
   session id: `AsyncClient.create_session(..., session_id=…)` (and a one-shot
   `query` option) pass it to spawn as `--session-id <id>`, report it on
   `QueryResult.session_id` **without** an on-screen match, and reuse it for the
   `--resume` respawn. An on-screen `ClientMarkers.session_id` capture, when one
   ever matches, still overrides for that turn (the pinned id is the floor, not
   a ceiling). An unset id leaves the spawn command exactly as today.
   - *Depends on:* none.
   - *Acceptance:* a session created with a pinned id spawns with
     `--session-id <id>`, every turn's `QueryResult.session_id` equals it with no
     marker configured, and the respawn argv carries `--resume <id>`; an unset
     pinned id leaves both the spawn argv and `QueryResult.session_id` (`None`)
     unchanged.
3. [ ] **Per-version selection + provider threading.**
   `maestro_sdk/src/maestro_sdk/marker_packs.py` + `maestro/providers/cli_paced.py`
   — extend `MarkerPack` with two optional per-CLI-version knobs:
   `idle_quiescence_seconds: float | None = None` and `pin_session_id: bool =
   False` (a pack declares "this version's idle line is unpaintable — complete
   on quiet" and "pin the id at spawn"). `configure_cli_paced` /
   `_default_client_factory` thread the resolved pack's quiescence value into the
   per-call/session `QueryOptions`, and when `pin_session_id` is set the provider
   generates a fresh UUID per session (and per metered one-shot), pins it, and
   feeds it to `_read_sidecar_usage` so the row goes `unmetered=False`. Add the
   symmetric drift sentinel: a pinned id that produces no readable sidecar across
   a session's first N turns logs one pack-named WARNING (metering silently
   degrading to `unmetered=True` is the honest fallback, never a fabricated
   count). No pack / `"none"` → neither knob set → today's behaviour byte-for-byte.
   - *Depends on:* 1, 2.
   - *Acceptance:* a pack declaring a quiescence value and `pin_session_id=True`,
     selected via `"auto"`, makes a `cli_paced` session spawn with `--session-id`
     and complete turns on quiescence; a fake transcript sidecar under the pinned
     id makes the usage row `unmetered=False`; a pinned id with no sidecar logs
     exactly one WARNING across repeated turns; a pack with neither knob (and the
     `"none"` path) leaves spawn argv, completion, and metering unchanged.
4. [ ] **Documentation.** `docs/sdk-internals.md` + `docs/deployment.md` —
   replace the "Registry status (claude 2.1.x)" *blocked* note with the two
   mechanisms and the new per-version pack knobs; state that a 2.1.x pack now
   becomes authorable (075 Step 6) by selecting quiescence + pinned-id and
   recording only the fixture-recordable markers; document the new drift sentinel
   and the quiescence-window operator caveat (tune above the model's
   inter-repaint cadence, below `stream_idle_timeout_seconds`). Note that
   `usage_metering` and `sessions` continuity are now reachable on the 2.1.x
   layout via configuration, not vacuous.
   - *Depends on:* 3.
   - *Acceptance:* the "Registry status" note no longer reads as blocked-on-an-
     unowned-prerequisite; the capability matrix's caveat column cites the
     quiescence/pinned-id knobs; 075 Step 6's acceptance is reachable as written
     against the documented mechanisms.

## Testing Strategy

Unit tests honour `mem_fs` + no-real-I/O + no-real-network + no-real-`sleep`;
the runner is the fake-runner seam, cadence is `InstantCadence`, the version and
`--session-id` probes are mocked, and the transcript sidecar is a `mem_fs`
fixture. Live validation is opt-in only.

- `maestro_sdk/tests/test_client.py` (extend) — quiescence completes a turn as a
  resident child under a still-alive runner; a transcript that keeps changing
  within the window does not complete; idle-marker and `complete_on` precedence
  over quiescence; `idle_quiescence_seconds=None` is byte-identical to today.
- `maestro_sdk/tests/test_session.py` (extend) — a pinned id keeps the runner
  resident across two `Session.query` turns under quiescence (one spawn), the
  respawn argv carries `--resume <id>`, and `QueryResult.session_id` is the
  pinned id with no marker configured.
- `maestro_sdk/tests/test_marker_packs.py` (extend) — `MarkerPack` carries the
  two new knobs, defaults leave them off, and the dataclass stays frozen.
- `tests/test_provider_cli_paced.py` (extend) — a pack with `pin_session_id`
  reaches the spawn argv and `_read_sidecar_usage`; the row goes
  `unmetered=False` once a fixture sidecar under the pinned id exists; the
  quiescence value reaches the client options; the no-sidecar WARNING fires once;
  the `"none"`/unconfigured path is byte-identical.
- `maestro_sdk/tests/` opt-in `RUN_CLAUDE_TESTS=1` — a real one-shot/session turn
  under a quiescence+pinned-id pack captures the pinned id and completes on
  quiescence (not EOF) against the pinned CLI version; this is the live check
  075 Step 6 then reuses.

## Risk Assessment

- **Quiescence false-completes mid-turn.** A long *silent* model pause with a
  static screen could read as "turn done." Mitigation: quiescence is measured on
  the rendered snapshot, so any spinner/"Running…"/token-tick repaint during
  work keeps the turn live; the window is tuned above the model's inter-repaint
  cadence and always sits inside `stream_idle_timeout_seconds` as the hard
  bound; it is opt-in per-version pack policy (off by default, off entirely
  under `"none"`); and the opt-in live test validates it against the real
  layout. A genuinely-static long pause remains the documented residual — the
  same class of bound the existing stream-idle deadline already accepts.
- **`--session-id` collision or semantics drift.** Reusing an id across spawns
  would collide; a CLI change could alter the flag. Mitigation: a fresh UUID is
  generated per session (reused only for that session's own `--resume`); the
  flag is verified on the pinned version and exercised under
  `RUN_CLAUDE_TESTS=1`; a pinned id that yields no readable sidecar degrades to
  the honest `unmetered=True` with a one-shot WARNING, never a wrong count.
- **`Screen`-fidelity alternative rejected.** Chasing the idle prefix instead
  would mean a wide-glyph width table and OSC consumption in a deliberately
  `dependencies = []` emulator — more surface, and still coupled to one
  version's exact glyph spacing (the residue problem the analysis flagged is
  beyond width alone). Quiescence is layout-free, which is the property 075's
  whole design optimises for; pinned-id removes the on-screen id dependency
  entirely. Both are strictly less fragile than a hand-authored prefix.

**Rollback posture.** Default `[sdk] provider = "anthropic"` and
`[sdk.cli_paced] marker_pack = "none"` keep every step inert: no pack means
neither knob is set, so completion stays EOF-only and no id is pinned — exactly
today's behaviour. Rollback is reverting the pack selection (or the provider),
with no state migration.

**Data migration.** None. `usage.jsonl` already carries both metered and
`unmetered` rows; a pinned id flipping rows to metered is forward-compatible,
identical to the 075 metering note.

## Out of scope

- **Authoring the 2.1.x pack itself** — that is 075 Step 6, which this proposal
  unblocks. Once 081 lands, 075 Step 6 selects these mechanisms and records the
  fixture-recordable markers (rate-limit / expired notices, slash-probe
  redraws); this proposal ships only the two enabling mechanisms, not a pack.
- **`Screen`-emulator fidelity upgrades** (wide-glyph width, OSC consumption) —
  the rejected alternative above; not needed once quiescence replaces the idle
  prefix, and a larger change to a zero-dependency module best justified on its
  own terms if ever wanted.
- **Mid-turn token-budget or rate-limit parsing** — unchanged from 061/077; the
  rate-limit/expired *markers* remain layout policy authored in 075's pack, not
  a mechanism this proposal touches.
- **Turning `cli_paced` on in production** — 067 owns the canary/runbook; this
  proposal, like 075, supplies a configuration-layer prerequisite its
  observability silently assumed.

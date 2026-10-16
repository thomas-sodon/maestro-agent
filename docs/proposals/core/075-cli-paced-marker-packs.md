# 075: Per-CLI-Version Marker Packs and Layout-Drift Detection for the CLI-Paced Provider

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | partial |
| **Affects** | new `maestro_sdk/src/maestro_sdk/marker_packs.py`, `maestro_sdk/src/maestro_sdk/{client,parser,approvals,slash_probe}.py` (consumed types only), `maestro/providers/cli_paced.py`, `maestro/config.py`, `maestro/cli.py`, `docs/sdk-internals.md`, `docs/deployment.md`, tests |
| **Depends on** | 061 (shipped), 062 (shipped) |
| **What landed** | Steps 1–5 + 7. `MarkerPack` (frozen), the `MARKER_PACKS` registry, `get_marker_pack`, and `select_marker_pack` in `maestro_sdk/src/maestro_sdk/marker_packs.py`; the `detect_cli_version` `claude --version` probe (every failure mode → `None`); the provider threading seam — `configure_cli_paced(marker_pack=…)` → `_default_client_factory` threads the pack's `ClientMarkers` / `ParserMarkers` / `ApprovalMarkers` and replaces the `_probe_markers` seam; the config surface — `Config.sdk_cli_paced_marker_pack` (`"auto"` / named / `"none"`, default `"none"`), fail-loud validation at `Config.validate`, and `resolve_marker_pack` at the `cli.py` `configure_cli_paced` call site; the layout-drift sentinel — `QueryResult.idle_marker_missed` plus the one-shot `_warn_idle_drift` / `_warn_session_id_drift` WARNINGs; and the `docs/sdk-internals.md` / `docs/deployment.md` documentation (PR #2617). |
| **What is missing** | Step 6 — the first real marker pack. `MARKER_PACKS` is still the empty tuple `()`, so `select_marker_pack` and `marker_pack = "auto"` find no covering pack and every operator gets today's EOF-only degradation (no live session continuity, sidecar metering, error taxonomy, or working probe); the `RUN_CLAUDE_TESTS=1` opt-in live-validation test against the deploy image's pinned CLI version is not yet present. The parity is exercised only against test doubles until a pack is registered. |

## Description

> Status updated to partial on 2026-06-14 by /update-proposals — see [README.md](../README.md#open-proposals) for the landing record.

Proposal 061 gave `cli_paced` capability parity, but four of its eight
capabilities are *mechanism-complete and configuration-empty*: every marker
family the driver acts on — `ClientMarkers` (idle / rate-limit /
session-expired / session-id), `ParserMarkers`, `ApprovalMarkers`,
`SlashProbeMarkers` — defaults to "matches nothing," because the on-screen
layout is per-CLI-version policy that 061 deliberately deferred to "the
production rollout." Neither rollout proposal picked it up: 066 owns the
action-gate, 067 owns login preconditions, attribution, and runbooks. No
proposal currently owns shipping a real marker pack, so on a live `claude`
binary the parity is exercised end-to-end only against test doubles.

The blast radius of the empty defaults is much larger than "no slash-command
metrics." Without an `idle` marker, every session turn ends on EOF and the
child is reaped — `maestro_sdk.Session` continuity degrades to independent
turns. Without a `session_id` marker, no CLI session id is ever captured, so a
respawn cannot `claude --resume` **and** `_read_sidecar_usage` has no id to
locate the transcript sidecar — every `cli_paced` row falls back to
`unmetered=True`, making the `usage_metering=True` capability flag vacuously
true in production. Without `rate_limit` / `session_expired` markers, a
rate-limited or expired session presents as a generic stream-idle timeout
(`stream_stall`) or EOF (`cli_crash`), so the loop's resume-in-place taxonomy
mis-buckets exactly the failures it has dedicated handling for. And the
proposal 062 probe fails loud on every cadence hit because `run_command`
requires a resident child that only an idle marker provides.

This proposal ships the missing layer: a versioned **marker pack** — one
frozen bundle of all four marker families keyed to a CLI version range — a
selection mechanism that probes `claude --version` at startup, a
`[sdk.cli_paced] marker_pack` config surface, and a layout-drift sentinel so a
CLI upgrade that silently changes the TUI reads as a loud, attributable
degradation instead of a quiet return to EOF-only turns. End state: an
operator on a supported CLI version gets live session continuity, real
sidecar metering, correct error taxonomy, and a working metrics probe; an
operator on an unknown version gets today's honest EOF-only behaviour plus a
WARNING naming the unmatched version.

## Relevant Findings

- `maestro_sdk/src/maestro_sdk/client.py` (`ClientMarkers`,
  `DEFAULT_CLIENT_MARKERS`) — all four fields default to the empty tuple;
  the docstring states "an unconfigured client detects none of these
  conditions and treats EOF as the only turn-complete signal." The
  `session_id` field notes the id glyph is "CLI-version specific — the same
  mechanism-plus-configuration split as the other fields."
- `maestro/providers/cli_paced.py` (`_default_client_factory`) — builds
  `AsyncClient(cadence, options=…, command=…, env=…)` and threads **no marker
  arguments at all**: even a hand-built pack has no provider-side seam to
  reach the driver today. `configure_cli_paced` accepts cadence / idle-timeout
  / probe knobs but nothing marker-shaped.
- `maestro/providers/cli_paced.py` (module docstring, "Transcript continuity"
  note) — "under the default empty markers every turn ends on EOF and no id is
  captured, so a respawn cannot `--resume`. Shipping a real marker pack
  belongs to the production rollout proposal, not this module."
- `maestro/providers/cli_paced.py` (`_read_sidecar_usage`) — returns `None`
  (→ honest `unmetered=True`) when `session_id` is falsy; the id is only ever
  populated via `ClientMarkers.session_id` extraction, so metering is
  structurally dead until a pack configures that marker.
- `maestro/providers/cli_paced.py` (resident-runner caveat above
  `_COST_COMMAND`) — an *enabled* probe "fails loud every cadence hit until a
  marker pack configuring `ClientMarkers.idle` lands."
- `maestro_sdk/src/maestro_sdk/errors.py` (`RateLimitError`,
  `SessionExpiredError`) — carry resume-taxonomy categories `"rate_limit"` /
  `"stale_session"`, but are raised only when the matching `ClientMarkers`
  prefixes fire; with empty markers those categories are unreachable on the
  CLI path.
- `maestro_sdk/src/maestro_sdk/slash_probe.py` (`SlashProbeMarkers`,
  `DEFAULT_PROBE_MARKERS`) — same honest-degenerate default ("parses every
  redraw to `None`"); `maestro/providers/cli_paced.py` holds it as the
  `_probe_markers` module seam awaiting "a future real pack."
- `docs/sdk-internals.md` (CLI-paced provider section) — names "the
  per-CLI-version `ClientMarkers`/`ParserMarkers` packs" as part of "a
  separate proposal (067)", but `067-cli-paced-rollout-canary-observability.md`
  contains no marker-pack step — the work is currently unowned.
- `maestro/config.py` (`_claude_cli_logged_in`) — precedent for a read-only
  startup probe of the `claude` binary (`claude auth status --json`), the same
  shape a `claude --version` probe needs.
- `maestro_sdk/README.md` — real-CLI integration tests are opt-in behind
  `RUN_CLAUDE_TESTS=1`; the only place a shipped pack can be validated against
  a live binary.

## Implementation Plan

Steps are ordered so the registry and selection logic land inert (no pack is
applied until the config surface in Step 4 wires one), and the drift sentinel
lands before the first real pack so the pack ships with its own regression
alarm. Everything is inert under the default `anthropic` provider.

1. [x] **Marker-pack type and registry.** New
   `maestro_sdk/src/maestro_sdk/marker_packs.py` — a frozen `MarkerPack`
   dataclass bundling `ClientMarkers`, `ParserMarkers`, `ApprovalMarkers`,
   `SlashProbeMarkers`, a pack `name`, and an inclusive CLI-version range
   (`min_version` / `max_version`, compared as dotted-numeric tuples), plus a
   module-level registry and `select_marker_pack(version) -> MarkerPack | None`
   that returns the newest pack whose range covers *version* and `None`
   otherwise (the honest "unknown layout" answer, never a best-guess pack).
   - *Depends on:* none.
   - *Acceptance:* `select_marker_pack` returns the covering pack for an
     in-range version, prefers the newest of two overlapping packs, and
     returns `None` for an out-of-range version; the dataclass is frozen.
2. [x] **CLI version probe.** `maestro_sdk/src/maestro_sdk/marker_packs.py` —
   `detect_cli_version(command)` runs `claude --version` (subprocess mocked in
   tests, mirroring the `_claude_cli_logged_in` probe discipline) and parses
   the dotted version; returns `None` on a missing binary, non-zero exit, or
   unparseable output — selection then degrades to no-pack rather than
   guessing.
   - *Depends on:* none.
   - *Acceptance:* a mocked probe returning a version string yields the parsed
     version; missing-binary / garbage-output cases return `None` without
     raising.
3. [x] **Provider threading seam.** `maestro/providers/cli_paced.py` —
   `configure_cli_paced` gains a `marker_pack` parameter; `_default_client_factory`
   passes the configured pack's `ClientMarkers` / `ParserMarkers` /
   `ApprovalMarkers` into `AsyncClient`, and the pack's `SlashProbeMarkers`
   replaces the `_probe_markers` module seam. An unconfigured pack keeps every
   current default byte-for-byte (`DEFAULT_CLIENT_MARKERS` et al.), so this
   step is behaviour-preserving until Step 4 wires config.
   - *Depends on:* 1.
   - *Acceptance:* with a pack configured, the fake-runner client is
     constructed with that pack's four marker families; with none configured,
     construction arguments are unchanged from today.
4. [x] **Config surface.** `maestro/config.py` + `maestro/cli.py` —
   `[sdk.cli_paced] marker_pack = "auto" | "<pack-name>" | "none"` (default
   `"none"`, today's behaviour). `"auto"` runs Step 2's probe and Step 1's
   selection at the same startup point that already calls
   `configure_cli_paced`; an `"auto"` resolution that finds no covering pack
   logs a WARNING naming the detected version and proceeds pack-less; a named
   pack that does not exist fails loud at config validation (a typo must not
   silently mean "no markers").
   - *Depends on:* 2, 3.
   - *Acceptance:* `"auto"` on a covered mocked version applies the pack;
     `"auto"` on an uncovered version warns and applies none; an unknown pack
     name raises at `Config.validate`; default config changes nothing.
5. [x] **Layout-drift sentinel.** `maestro_sdk/src/maestro_sdk/client.py` +
   `maestro/providers/cli_paced.py` — when a non-empty `ClientMarkers.idle` is
   configured but a turn ends on EOF without the idle marker ever matching,
   record `idle_marker_missed` on the turn result and log a one-shot WARNING
   ("configured marker pack did not match the CLI layout — possible CLI
   upgrade") naming the pack; same one-shot WARNING when a configured
   `session_id` marker never matches across a session's first N turns. Drift
   thus reads as a loud, attributable degradation while behaviour still
   degrades to the honest EOF-only path (no crash — the pack is observability
   config, not a correctness gate).
   - *Depends on:* 3.
   - *Acceptance:* a fake-runner transcript with no idle line under a
     configured pack sets the miss flag and logs exactly one WARNING across
     repeated turns; an unconfigured client logs nothing.
6. [ ] **First real pack + live validation.** `maestro_sdk/src/maestro_sdk/marker_packs.py`
   + `maestro_sdk/tests/` — author the pack for the CLI version pinned in the
   deploy image (idle prompt redraw, rate-limit notice, expired-session
   notice, session-id line, probe layouts), and add an opt-in
   `RUN_CLAUDE_TESTS=1` integration test asserting a real one-shot turn under
   `"auto"` captures a session id and ends on the idle marker (not EOF). Unit
   tests pin the pack's prefixes against recorded transcript fixtures.
   - *Depends on:* 4, 5.
   - *Acceptance:* fixture-driven unit tests show the pack matching idle /
     session-id / rate-limit lines from a recorded transcript; the opt-in live
     test passes against the pinned CLI version.
7. [x] **Documentation.** `docs/sdk-internals.md`, `docs/deployment.md` —
   replace the "marker packs belong to the rollout proposal" deferral with the
   pack mechanism, the `"auto"`/named/`"none"` config contract, the drift
   sentinel semantics, and the operator procedure for a CLI upgrade (re-pin
   pack or wait for a covering pack). Note which capabilities are
   configuration-dependent (sessions continuity, usage metering, error
   taxonomy, probe) so 067's canary runbook can cite the pack as a
   prerequisite for meaningful observability.
   - *Depends on:* 6.
   - *Acceptance:* the capability matrix's caveat column points at the pack
     mechanism instead of an unowned deferral; 067's runbook prerequisites can
     cite this section.

## Testing Strategy

Unit tests honour `mem_fs` + no-real-I/O + no-real-network + no-real-`sleep`;
the `claude` binary, pty, and version probe are mocked (fake-runner seam,
`InstantCadence`, patched subprocess). Live validation is opt-in only.

- `maestro_sdk/tests/test_marker_packs.py` (new) — registry selection (covered
  / uncovered / overlapping ranges), frozen-ness, version-probe parsing happy
  path and all degrade branches.
- `maestro_sdk/tests/test_client.py` + `test_session.py` (extend) — a
  configured idle marker keeps the runner resident across two `Session.query`
  turns (one spawn); a configured `session_id` marker round-trips into
  `QueryResult.session_id` and the respawn argv carries `--resume`; the drift
  sentinel's miss flag + one-shot WARNING.
- `tests/test_provider_cli_paced.py` (extend) — `configure_cli_paced(marker_pack=…)`
  reaches the client factory's marker arguments and `_probe_markers`; sidecar
  metering goes `unmetered=False` end-to-end once the fake transcript carries
  the session id the marker extracted; unconfigured path byte-identical.
- `tests/test_config.py` (extend) — `marker_pack` validation: `"auto"` /
  `"none"` / known name accepted, unknown name fails loud at `Config.validate`.
- `maestro_sdk/tests/` opt-in `RUN_CLAUDE_TESTS=1` — the Step 6 live check.

## Risk Assessment

- **Marker rot on CLI upgrade.** The pack encodes a third-party TUI layout
  that changes without notice; a silent mismatch would quietly disable
  continuity, metering, and the probe while flags still read `True`.
  Mitigation: the Step 5 drift sentinel makes a mismatch loud and named, the
  version-range selection refuses to apply a pack outside its tested range,
  and degraded behaviour is exactly today's EOF-only path — never a wrong
  parse. The opt-in live test pins the pack to the deploy image's CLI version.
- **Wrong-marker false positives.** An over-broad prefix (e.g. a rate-limit
  marker matching ordinary prose) would mis-raise `RateLimitError` on healthy
  turns. Mitigation: packs are authored against recorded transcript fixtures
  with unit tests pinning both matching and non-matching lines; prefixes
  follow the existing stripped-line-prefix discipline rather than substring
  search.
- **Startup probe fragility.** `claude --version` could hang or change shape.
  Mitigation: the probe is bounded and mocked-in-tests like
  `_claude_cli_logged_in`; every failure path resolves to "no pack" plus a
  WARNING, never a blocked startup.

**Rollback posture.** Default `[sdk.cli_paced] marker_pack = "none"` (and
default provider `anthropic`) keeps every step inert; rollback is reverting
the config line. No step changes behaviour for an operator who has not opted
in.

**Data migration.** None. `usage.jsonl` already carries both metered and
`unmetered` rows; a pack flipping rows to metered is forward-compatible.

## Out of scope

- **Turning `cli_paced` on in production** — 067 owns the canary/runbook; this
  proposal supplies the configuration layer 067's observability silently
  assumed.
- **Auto-generating packs from live transcripts** — packs are hand-authored
  against fixtures; a self-calibrating layout learner is a research idea, not
  this slice.
- **Marker packs for non-`claude` CLIs** — the driver is `claude`-specific by
  design (proposal 056).

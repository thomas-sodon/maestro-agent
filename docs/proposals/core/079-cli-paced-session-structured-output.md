# 079: Session-Path Structured Output for the CLI-Paced Provider

| Field | Value |
|-------|-------|
| **Impact** | LOWER |
| **Status** | proposed |
| **Affects** | `maestro/providers/cli_paced.py`, `docs/sdk-internals.md`, tests |
| **Depends on** | 061 (shipped) |

## Description

After proposal 061, the `cli_paced` provider's option surface has exactly two
remaining `NotImplementedError` rejections: `hooks` (closed for the one
production payload by proposal 066's action-gate replication) and
`output_format` on the *session* path — `create_session` raises
`_OUTPUT_FORMAT_SESSION_UNSUPPORTED` because `query_on_session` performs no
validation, and silently honouring a schema that follow-up turns ignore would
hand callers a `structured_output` of `None`. On the Anthropic provider there
is no such split: `_build_sdk_options` forwards `output_format` for sessions
exactly as for one-shot queries, so a session created with a schema enforces
it on every turn.

The gap is currently latent — every loop call site that requests
`output_format` does so on the one-shot `query()` fallback branch, while
session-attached turns steer JSON via `complete_on=json_complete` plus
`parse_json` — but it is the last asymmetry in the capability surface, and it
constrains future callers: the session-attachment invariant pushes phases
toward `query_on_session`, and a phase that wants schema-validated output is
today forced onto the one-shot path (losing session context) or onto manual
parsing. Closing it is cheap because 061 Step 9 already built the whole
mechanism — `_augment_prompt_for_schema`, `_parse_and_validate`, and the
single-re-ask loop in `_query_structured` — for the one-shot path; this
proposal points the same machinery at session turns.

One session-specific improvement falls out naturally: on a live session the
re-ask happens inside a transcript that remembers the bad answer, so the
correction turn does not need to repeat the full original prompt the way the
fresh-process one-shot re-ask must. End state: `create_session` accepts
`output_format`, every turn on that session validates against the schema with
one re-ask before failing loud, and the provider's only remaining rejection
is the non-gate `hooks` payload — the documented, deliberate boundary.

## Relevant Findings

- `maestro/providers/cli_paced.py` (`create_session`,
  `_OUTPUT_FORMAT_SESSION_UNSUPPORTED`) — the session path rejects
  `output_format` at create time with the remediation "use `query()` for
  structured output"; the rejection exists precisely because honouring the
  field without per-turn validation would be a silent `None`.
- `maestro/providers/cli_paced.py` (`_query_structured`,
  `_augment_prompt_for_schema`, `_parse_and_validate`) — the complete
  validate-and-reask mechanism from 061 Step 9: up-front `check_schema`
  fail-loud, schema appended to the prompt, `parse_json` +
  `jsonschema.validate`, exactly one re-ask, then
  `_OUTPUT_FORMAT_VALIDATION_FAILED`. It is parameterised over a `run_turn`
  callable, so a session turn can back it without restructuring.
- `maestro/providers/cli_paced.py` (`_augment_prompt_for_schema`, *reask*
  note) — the one-shot re-ask "still carries the full original prompt +
  schema, not a bare 'try again'" *because* the fresh process has "no
  transcript memory of the bad answer"; a session re-ask explicitly does have
  that memory, enabling the shorter correction turn.
- `maestro/providers/cli_paced.py` (`_CLISession`) — already carries
  create-time `options`, `system`, `turn_count`, and `max_turns`; the natural
  slot for a captured `output_format`.
- `maestro/providers/cli_paced.py` (`query_on_session`, `max_turns` cap) — a
  re-ask is a driven prompt-turn; its interaction with the per-session
  `max_turns` cap must be defined (this proposal counts the re-ask against
  the cap, since the cap bounds turns Maestro drives).
- `maestro/sdk.py` (`_build_sdk_options` used by `_create_session_impl`) —
  the Anthropic parity baseline: sessions and one-shot queries share one
  options pipeline, so `output_format` on a session is enforced server-side
  every turn.
- `maestro/consciousness.py` (`assess_stage_readiness`) — the representative
  call-site shape: the session branch uses `complete_on=json_complete` with
  no schema, the one-shot fallback branch passes `output_format`; evidence
  the gap is latent today and that closing it lets the two branches converge.

## Implementation Plan

1. [ ] **Capture the schema on the session handle.**
   `maestro/providers/cli_paced.py` — `create_session` stops raising
   `_OUTPUT_FORMAT_SESSION_UNSUPPORTED`; it runs the same up-front
   `check_schema` well-formedness gate `_query_structured` uses (a malformed
   schema fails loud with no spawn), then stores `output_format` on
   `_CLISession`. No turn behaviour changes yet — the field is captured but
   unread, so this step is safe to merge alone only together with the
   docstring note that enforcement lands in Step 2; land both in one PR if
   interrupted-run safety is in doubt.
   - *Depends on:* none.
   - *Acceptance:* `create_session` with a valid schema returns a handle
     carrying it; a malformed schema raises `jsonschema.SchemaError` with no
     client spawn; `None` stays `None`.
2. [ ] **Per-turn validate-and-reask on sessions.**
   `maestro/providers/cli_paced.py` — when the handle carries a schema,
   `query_on_session` routes the turn through `_query_structured` with a
   session-backed `run_turn` (the existing compose-prepend + driver call),
   passing a session-aware re-ask prompt: the correction note plus the schema,
   without repeating the full original prompt (the transcript already holds
   it). The re-ask consumes a prompt-turn against `max_turns` (checked the
   same way as a caller turn, so a session at its cap fails the re-ask loudly
   rather than exceeding the cap silently). The validated object rides
   `QueryResult.structured_output`; a second miss raises
   `_OUTPUT_FORMAT_VALIDATION_FAILED` exactly as on the one-shot path.
   - *Depends on:* 1.
   - *Acceptance:* a conforming first answer returns the validated object; a
     non-conforming first answer triggers exactly one session re-ask whose
     prompt contains the correction note and schema but not the original
     prompt; two misses raise; a schema-less session is byte-identical to
     today; the re-ask increments `turn_count`.
3. [ ] **Documentation.** `docs/sdk-internals.md` +
   `maestro/providers/cli_paced.py` module docstring — the structured-output
   caveat drops its "one-shot only" qualifier; the remaining rejection
   surface is stated as exactly non-gate `hooks`. Note the client-side
   (vs Anthropic server-side) enforcement caveat unchanged.
   - *Depends on:* 2.
   - *Acceptance:* docs and docstring list `hooks` as the sole remaining
     rejection; the capability matrix's `structured_output` caveat covers
     both paths.

## Testing Strategy

Unit tests honour `mem_fs` + no-real-I/O + no-real-network + no-real-`sleep`;
session turns run against the fake-runner seam with `InstantCadence`.

- `tests/test_provider_cli_paced.py` (extend) — the full session matrix:
  conforming / one-miss-then-conforming / two-miss-raises; session-aware
  re-ask prompt shape (correction + schema, no original prompt); malformed
  schema fails at create with no spawn; schema-less sessions unchanged;
  re-ask vs `max_turns` interaction (cap reached between first answer and
  re-ask fails loud); `structured_output` populated on the returned result.
- `tests/test_capability_gating.py` (unchanged) — `structured_output` was
  already `True`; this proposal removes a path-level caveat, not a flag.

## Risk Assessment

- **Re-ask burns session turns.** A flaky model on a tightly capped session
  could spend its last turn on a correction. Mitigation: the cap check is
  explicit and loud (the existing `max_turns` `RuntimeError`), and counting
  the re-ask is the honest accounting — it *is* a driven turn; the docstring
  states it.
- **Transcript-dependent re-ask under empty markers.** Without a proposal 075
  marker pack, session continuity degrades to independent turns, so the
  shortened re-ask prompt would reach a child with no memory of the bad
  answer. Mitigation: detect the degraded case the driver already exposes
  (no captured session id / respawn without resume) and fall back to the
  full-prompt re-ask form the one-shot path uses — never a context-free
  "try again."
- **Behavioural drift between the two enforcement paths.** Mitigation: both
  paths share `_query_structured` / `_augment_prompt_for_schema` /
  `_parse_and_validate`; the only divergence (re-ask prompt form) is a
  parameter, pinned by tests on both paths.

**Rollback posture.** Default `[sdk] provider = "anthropic"` keeps the change
inert; reverting restores the create-time rejection with no state impact.

**Data migration.** None.

## Out of scope

- **Forwarding arbitrary `hooks`** — the deliberate, documented boundary:
  in-process callbacks cannot reach a subprocess; the task action-gate carve-
  out (066) is the only mapping.
- **Server-side schema enforcement** — impossible from the TUI; the
  client-side caveat stands for both paths, and hard-guarantee callers use
  `anthropic`.

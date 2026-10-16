# 020: Approved Guest Telegram Conversations

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | proposed |
| **Affects** | `maestro/types.py`, `maestro/messaging.py`, `maestro/agent.py`, `maestro/telegram_tools.py`, `maestro/notifications/base.py`, `maestro/notifications/telegram.py`, `maestro/prompts/defaults.py`, `maestro/config.py`, `maestro/cli.py`, `maestro/dashboard/routes.py`, `maestro/dashboard/templates/index.html`, `maestro.toml`, `CLAUDE.md`; new `maestro/contacts.py`, `maestro/observations.py`, `maestro/observation_tools.py` |

## Description

Today every inbound Telegram message is hard-coded as `sender="thomas"` in `_on_message` (`maestro/agent.py`) and the polling loop does no `chat_id` filtering (`maestro/notifications/telegram.py`). Anyone who finds the bot can reach Maestro, and their words flow straight through `_process_messages → contemplate → introspect → update_self_model` — poisoning the self-model and burning unbounded budget.

Thomas wants strangers to be greeted politely but held in a quarantine bucket until he approves them. Approved guests may chat within cost caps, but their words must never directly shape Maestro's identity. Anything Maestro finds noteworthy is surfaced to Thomas for explicit confirmation; only on acceptance does it enter the self-model — and then through the normal Thomas-message pipeline, not a side door.

**Trust model.** Thomas is the only fully-trusted counterparty. Guests are nice to talk with; their content is conversational, not identity-forming. This proposal enforces that asymmetry with code-level invariants and unit tests, not prompt discipline.

## Relevant Findings

### The current "single seam" claim is false in three places

Identity-forming calls (`update_self_model`, `introspect`, `reflect`, `generate_hypothesis`, `dream`) read Thomas's messages via the loop's `_process_messages()` path at `maestro/agent.py`. But the codebase has **three other readers** of `MessageStore` that must be accounted for before any guest sender can exist:

- `maestro/tools.py` — `read_messages_from_thomas` MCP tool already filters to `sender=="thomas"` via `get_unread()`. Safe.
- `maestro/telegram_tools.py` — `_handle_read()` uses `get_recent()` **without sender filtering**. An agent task that calls `telegram_read_messages` during task execution would capture guest text in `step.result`, which flows into `_distill_findings` (`maestro/agent.py`) → `key_findings` → `recent_self_directed` pairs → `update_self_model`. **This is the biggest non-obvious leak path.**
- `maestro/journal.py` — `get_recent_narrative()` does **not** filter by `entry_type` or `triggered_by`. Anything journaled anywhere feeds `introspect`, `reflect`, `generate_hypothesis`, `dream`. So guest content must not be journaled at all.

### Existing patterns to reuse (do not reinvent)

- **JSONL + `file_lock()` + atomic rewrite.** `MessageStore.mark_read` (`maestro/messaging.py`) and `Memory.update_plan_status` (`maestro/memory.py`) are the canonical templates. Use them for `ContactStore` and `ObservationStore`.
- **Forward-compatible message loading.** `_load_all()` already filters `fields(Message)` (`maestro/messaging.py`), so adding a `contact_id` field loads legacy rows cleanly without migration.
- **Inline-button callback dispatch.** `_handle_callback` (`maestro/notifications/telegram.py`) already dispatches `action:id`-shaped callback data. Extend with four new prefixes.
- **Approval polling call site.** `await self.notifier.start_approval_polling(_on_approve, _on_reject, on_message=on_message)` at `maestro/agent.py`. Signature at `maestro/notifications/telegram.py`. Add optional kwargs; existing call site keeps working.
- **Per-call MCP server attachment.** `QueryOptions.mcp_servers: dict[str, Any]` at `maestro/sdk.py` — passed through in `_build_sdk_options()` (`maestro/sdk.py`). Lets us attach one bespoke MCP server (with `contact_id` captured in the handler's closure) to the guest reply `query()` alone.
- **`_respond_to_messages()` structural shape.** `maestro/agent.py` — fetch unread → build prompt → stateless `query()` with `effort="low"` + `chars_complete(200)` → save → `notifier.notify()`. The new `_respond_to_guests` mirrors this shape with isolation constraints added.
- **Dashboard review pattern.** `approve_plan_action` (`maestro/dashboard/routes.py`) calling `memory.update_plan_status` (`maestro/memory.py`) is the template for contact/observation status endpoints.
- **Plans tab markup.** `maestro/dashboard/templates/index.html` (panel) and `renderPendingPlans()` at `maestro/dashboard/static/index.js` are the template for a new **People** tab.

### `latest_inbound_message_id()` is already Thomas-only

`maestro/messaging.py` explicitly checks `m.sender == "thomas"`. The burn-rate standby gate that depends on it is unaffected by the `MessageSender` literal widening.

## Implementation Plan

### Step 1: Data model

**`maestro/types.py`:** extend `MessageSender` to `Literal["maestro", "thomas", "guest"]`; regenerate `VALID_MESSAGE_SENDERS`. Add `ContactStatus = Literal["pending", "approved", "revoked"]`.

**`maestro/messaging.py`:**
- Add optional `contact_id: str | None = None` to `Message`. `fields(Message)` filtering handles legacy rows with no migration.
- Rename `get_unread()` → `get_unread_from_thomas()` and migrate the two in-tree callers (`maestro/tools.py`, `maestro/agent.py:_process_messages`).
- Add `get_unread_from_guest(contact_id: str | None = None)` that filters `sender == "guest"` and optionally by `contact_id`.
- Keep `get_unread()` as a deprecated thin alias for one release cycle, then delete.

**`maestro/contacts.py` (new):**

```python
@dataclass
class Contact:
    chat_id: str
    status: ContactStatus
    display_name: str
    first_seen: str
    last_message_at: str
    approved_at: str | None
    revoked_at: str | None
    first_message_preview: str   # <=300 chars
    notes: str = ""

class ContactStore:
    # file_lock + atomic rewrite, mirroring MessageStore
    def get(self, chat_id) -> Contact | None: ...
    def upsert_pending(self, chat_id, display_name, preview) -> Contact: ...
    def list_all(self) -> list[Contact]: ...
    def list_by_status(self, status: ContactStatus) -> list[Contact]: ...
    def set_status(self, chat_id, new_status: ContactStatus) -> bool: ...
    def set_notes(self, chat_id, notes: str) -> bool: ...
    def touch_last_message(self, chat_id) -> None: ...
```

Thomas's `telegram_chat_id` is never stored — he's implicitly trusted.

**`maestro/observations.py` (new):**

```python
@dataclass
class NoteworthyObservation:
    obs_id: str
    created_at: str
    contact_id: str
    contact_display_name: str
    observation: str           # <=500 chars
    reasoning: str             # <=300 chars
    source_message_ids: list[str]
    status: Literal["pending", "accepted", "dismissed"]
    reviewed_at: str | None
```

`ObservationStore` with the same JSONL+lock shape.

### Step 2: Fix the `telegram_read_messages` leak

In `maestro/telegram_tools.py`, add an explicit sender filter so the tool never returns non-Thomas messages to the agent, regardless of what senders exist in `messages.jsonl`:

```python
messages = [m for m in messages if m.sender == "thomas"]
```

This closes the `telegram_read_messages → key_findings → update_self_model` leak path identified above. The tool is already named for Thomas; the filter now matches the name.

### Step 3: Inbound dispatch

Rewrite `_on_message` in `maestro/agent.py`:

```python
async def _on_message(text: str, chat_id: str, display_name: str) -> None:
    if chat_id == self.config.telegram_chat_id:
        save as sender="thomas", contact_id=None
        return

    if not self.config.guest_enabled:
        return  # silent drop — no record, no reply

    contact = contact_store.get(chat_id)
    if contact is None:
        contact = contact_store.upsert_pending(chat_id, display_name, preview=text[:300])
        save as sender="guest", contact_id=chat_id, message_type="instruction"
        if not _holding_reply_rate_limited(chat_id):
            await notifier.notify(HOLDING_REPLY, chat_id=chat_id)
        await notifier.notify_contact_request(contact)
        return

    contact_store.touch_last_message(chat_id)
    save as sender="guest", contact_id=chat_id

    if contact.status == "pending":
        if not _holding_reply_rate_limited(chat_id):
            await notifier.notify(HOLDING_REPLY, chat_id=chat_id)
        return

    if contact.status == "revoked":
        return  # silent drop — history preserved, no reply

    # status == "approved" — real reply happens in the respond_to_guests phase
```

Holding-reply rate limit: ephemeral `self._last_holding_reply_at: dict[str, datetime]` keyed on `chat_id`, gated on `config.guest_holding_reply_cooldown_seconds` (default 24h). Acceptable to reset on restart — this only gates auto-replies, not message persistence.

Widen `notifications/telegram._on_message` signature to `(text, chat_id, display_name)`; extract those from `update.message.chat.id` and `update.message.from_user` in `_poll_loop`.

### Step 4: Guest response phase

Insert new phase `"respond_to_guests"` into `_LOOP_PHASES` (`maestro/agent.py`) immediately after `respond_to_messages`. Disabled when `config.guest_enabled = false`.

`agent._respond_to_guests()` mirrors the shape of `_respond_to_messages` (`maestro/agent.py`) with three hard isolation constraints:

1. **Stateless.** Always `query()`, never `query_on_session()`. Sessions A/B/C/D are never used for guest replies.
2. **Sanitized system prompt.** `system=GUEST_IDENTITY_PROMPT` — a stripped-down prompt with role, tone, boundaries, and the "raise anything noteworthy" rule. Omits `awakened_at`, `evolution_count`, open questions, purpose hypothesis, and the architecture-mutability closing line. Cap at ~1500 chars.
3. **Tool allowlist of exactly one.** `allowed_tools=["mcp__maestro_observations__raise_to_thomas"]`. Nothing else — no Read/Write/Bash/Grep/Edit, no journal/self-awareness/memory/issues/telegram MCP servers.

The MCP server carrying `raise_to_thomas` is built **fresh per guest reply** with `contact_id` captured in the tool handler's **closure**. No thread-locals, no `contextvars` — the closure is the simplest safe pattern for propagating guest identity into the tool.

```python
for contact in self.contact_store.list_by_status("approved"):
    unread = self.message_store.get_unread_from_guest(contact.chat_id)
    if not unread:
        continue
    if self._guest_replies_today(contact.chat_id) >= self.config.guest_daily_message_cap:
        for m in unread: self.message_store.mark_read(m.message_id)
        self._notify_daily_cap_once_per_day(contact)
        continue

    obs_mcp = build_observation_mcp_server(
        self.observation_store, self.notifier, contact,
    )
    result = await query(
        prompt=GUEST_RESPONSE_PROMPT.format(
            display_name=contact.display_name,
            messages="\n".join(f"[guest] {m.content[:200]}" for m in unread[-10:]),
        ),
        system=GUEST_IDENTITY_PROMPT,
        options=QueryOptions(
            effort=self.config.guest_effort,       # default "low"
            caller="respond_to_guest",
            mcp_servers={"maestro_observations": obs_mcp},
            allowed_tools=["mcp__maestro_observations__raise_to_thomas"],
        ),
        complete_on=chars_complete(200),
    )
    reply_text = str(result.response).strip()[: self.config.message_max_length]
    if reply_text:
        self.message_store.save_message(Message(
            message_id=uuid.uuid4().hex[:12],
            timestamp=datetime.now(UTC).isoformat(),
            sender="maestro", content=reply_text, message_type="info",
            reply_to=unread[-1].message_id,
            contact_id=contact.chat_id,
        ))
        await self.notifier.notify(reply_text, chat_id=contact.chat_id)
        for m in unread: self.message_store.mark_read(m.message_id)
        self._guest_empty_streak.pop(contact.chat_id, None)  # reset streak on success
    else:
        # Empty reply (early-exit before text, budget throttle, a stream-idle
        # swallow, or a model refusal): do NOT mark the guest's messages read on
        # a transient miss -- leave them unread so the next respond_to_guests
        # iteration retries. But BOUND the retries. A durably-empty guest (a
        # persistent model refusal) records no sender=="maestro" outbound, so
        # _guest_replies_today stays 0 and the daily-cap gate above never fires
        # for it -- without a separate bound this would burn one query() per loop
        # iteration forever, the very "unbounded budget" this proposal exists to
        # stop. Track an ephemeral per-guest empty streak (self._guest_empty_streak,
        # resets on restart and on any successful reply, mirroring the
        # holding-reply rate limit) and give up once it reaches
        # max(guest_daily_message_cap, 2) -- a floor of 2 guarantees at least one
        # retry even when the cap is 1: mark the batch read so the loop stops
        # retrying it, and re-arm for the guest's next burst. Log chat_id and the
        # streak/unread COUNTS only -- never guest content -- so the "guest
        # messages are never journaled/logged" isolation invariant holds.
        streak = self._guest_empty_streak.get(contact.chat_id, 0) + 1
        self._guest_empty_streak[contact.chat_id] = streak
        if streak >= max(self.config.guest_daily_message_cap, 2):
            for m in unread: self.message_store.mark_read(m.message_id)
            self._guest_empty_streak.pop(contact.chat_id, None)  # re-arm next burst
            # An empty reply can come from a transient, system-wide cause (a
            # budget-throttle skip, a stream-idle swallow) as readily as a durable
            # per-guest refusal, and the cause-blind streak cannot tell them apart
            # -- so giving up may discard a legitimate guest's never-answered
            # messages. Do NOT drop silently: surface it to Thomas, mirroring the
            # daily-cap path (_notify_daily_cap_once_per_day), deduped once per day
            # per contact so a persistent system hiccup cannot spam him. The
            # notification carries chat_id + counts ONLY -- never guest content --
            # so the "guest messages are never journaled/logged" invariant holds.
            self._notify_guest_giveup_once_per_day(contact)
            logger.warning(
                "Giving up on chat_id=%s after %d empty guest replies; "
                "%d message(s) marked read",
                contact.chat_id, streak, len(unread),
            )
        else:
            logger.warning(
                "Empty guest reply for chat_id=%s (attempt %d/%d); "
                "%d message(s) left unread for retry",
                contact.chat_id, streak, max(self.config.guest_daily_message_cap, 2),
                len(unread),
            )
```

**Empty-reply contract.** On a non-empty reply the batch is marked read. On an empty reply — when `query()` yields no text (an early-exit before any text, a budget-throttle skip, a stream-idle swallow, or a model refusal) — the guest's messages are left **unread** so the next `respond_to_guests` iteration retries them, *until* the empty streak reaches `max(guest_daily_message_cap, 2)`, at which point the batch **is** marked read to halt the retry loop (see below). Each still-retrying empty emits one event-only WARNING carrying the `chat_id` and counts; the give-up drop additionally **notifies Thomas** (deduped once per day per contact, mirroring the daily-cap path's `_notify_daily_cap_once_per_day`), because an empty reply can be a transient, system-wide hiccup (a budget-throttle skip, a stream-idle swallow) as readily as a durable per-guest refusal, so a give-up may discard a legitimate guest's messages and must never be silent. Both channels carry the `chat_id` and counts — **never** the message content, so the "guest messages are never journaled/logged" isolation invariant holds. Nothing is silently swallowed. The daily-cap counter stays consistent because it counts only recorded `sender=="maestro"` outbounds; the give-up and daily-cap paths mark inbounds read without writing an outbound, which never corrupts that count and never fabricates a phantom outbound.

**Bounded empty-reply retry.** Because an empty reply records no `sender=="maestro"` outbound, `_guest_replies_today` never counts it, so the daily-cap gate alone does **not** bound a *durably*-empty guest — a persistent model refusal would otherwise burn one `query()` per loop iteration indefinitely, the exact "unbounded budget" this proposal exists to close. The `else` branch therefore keeps an ephemeral per-guest counter, `self._guest_empty_streak: dict[str, int]` (keyed on `chat_id`, reset on restart and cleared on the guest's next successful reply — mirroring `self._last_holding_reply_at`), and gives up once the streak reaches `max(guest_daily_message_cap, 2)`: the batch is marked read (halting the retry loop) and the counter is cleared to re-arm for the guest's next burst. Because the streak is cause-blind — it cannot distinguish a transient, system-wide empty (a budget-throttle skip or a stream-idle swallow) from a durable per-guest refusal — a give-up may discard a legitimate guest's never-answered messages, so the give-up path **notifies Thomas** via `_notify_guest_giveup_once_per_day(contact)` (deduped once per day per contact to bound spam during a system-wide hiccup, mirroring the daily-cap drop's notification) rather than dropping on an event-only WARNING, bringing the give-up drop to parity with the daily-cap drop's visibility; the notification carries the `chat_id` and counts only, never guest content. So empty-reply attempts are capped at `max(guest_daily_message_cap, 2)` low-effort attempts per burst — a floor of 2 (one guaranteed retry) so a single transient miss is **always** retried on the very next iteration, even when an operator sets `guest_daily_message_cap = 1`, while still reusing the reply cap rather than adding a config knob. The streak counts attempts only, holds no guest content, and writes no message, preserving both the journaling-isolation and no-phantom-outbound invariants.

Daily cap helper counts `sender=="maestro"` messages in the last 24h with `contact_id == chat_id`. With `Message.contact_id` set on both directions, this is a straight filter — no `reply_to` join.

**Guest messages are never journaled.** `messages.jsonl` is the only durable record of guest content. No `Journal.log_*` calls anywhere in the guest code path. This closes the `get_recent_narrative()` leak cleanly — there is nothing to filter because nothing is written.

### Step 5: Noteworthy observation path

New MCP server in `maestro/observation_tools.py` exposing exactly one tool:

```
raise_to_thomas(observation: str, reasoning: str)
```

Factory `build_observation_mcp_server(store, notifier, contact)` returns an `McpSdkServerConfig` whose handler is a closure over `store`, `notifier`, and `contact`. The handler validates lengths, creates a pending `NoteworthyObservation`, calls `notifier.notify_noteworthy_observation(obs)` (Telegram to Thomas with Accept/Dismiss buttons), and returns `"Noted — I'll share that with Thomas."` so the guest reply flows naturally.

**Acceptance** — the only door by which guest-origin content can shape identity:

1. Thomas clicks Accept (dashboard or Telegram button).
2. `observation_store.set_status(obs_id, "accepted", reviewed_at=now)`.
3. Append a **synthetic Thomas message** to `messages.jsonl`:
   ```
   sender="thomas", contact_id=None, message_type="info",
   content=f"[Confirmed observation from {contact.display_name}]: {observation}\n\nReasoning: {reasoning}"
   ```
4. Next contemplation picks it up via `get_unread_from_thomas()` → existing `_process_messages()` → `contemplate(context=...)` → `update_self_model`. No new plumbing in `consciousness.py`. The invariant "only Thomas-authored bytes reach identity" holds: his click authorizes the content.

Dismiss just sets status.

### Step 6: Telegram polling callbacks

In `notifications/telegram.py`:
- Add four new callback prefixes to `_handle_callback`: `approve_contact:<chat_id>`, `reject_contact:<chat_id>`, `accept_observation:<obs_id>`, `dismiss_observation:<obs_id>`.
- Extend `start_approval_polling` with four additive optional kwargs: `on_contact_approve`, `on_contact_reject`, `on_observation_accept`, `on_observation_dismiss`. Existing call site at `maestro/agent.py` keeps working unchanged.
- In `notifications/base.py`, add optional `chat_id` kwarg to `Notifier.notify()` (`TelegramNotifier` uses `chat_id or self._chat_id`; non-Telegram notifiers ignore). Add two typed helpers: `notify_contact_request(contact)` and `notify_noteworthy_observation(obs)`, each sending a message with the right inline buttons.

### Step 7: Dashboard

`maestro/dashboard/routes.py` — seven new authenticated endpoints (`_PUBLIC_PATHS` unchanged; `BearerAuthMiddleware` protects them automatically):

| Method | Path | Action |
|---|---|---|
| GET | `/api/contacts` | list all |
| POST | `/api/contacts/{chat_id}/approve` | status → `"approved"` |
| POST | `/api/contacts/{chat_id}/revoke` | status → `"revoked"` (also handles reject-pending and re-approve-to-revoked flows) |
| POST | `/api/contacts/{chat_id}/notes` | update notes |
| GET | `/api/observations` | list (filter by status) |
| POST | `/api/observations/{obs_id}/accept` | status → `"accepted"` + append synthetic Thomas message |
| POST | `/api/observations/{obs_id}/dismiss` | status → `"dismissed"` |

All mirror `approve_plan_action` / `update_plan_status`.

`maestro/dashboard/templates/index.html` — new **People** tab mirroring the Plans tab (`index.html` panel, `maestro/dashboard/static/index.js` renderers). Sections top-down by actionability:
1. **Noteworthy Observations** (`#pending-observations`) — contact, observation, reasoning, source-message previews, Accept/Dismiss.
2. **Pending Contact Requests** (`#pending-contacts`) — name, chat_id, first-message preview, first_seen, Approve/Reject.
3. **Approved Contacts** (`#approved-contacts`) — name, last_message_at, today's reply count / cap, editable notes, Revoke.
4. **Revoked Contacts** (`#revoked-contacts`) — name, revoked_at, Re-approve.

JS: `renderPendingObservations`, `renderPendingContacts`, `renderApprovedContacts`, `renderRevokedContacts`. Each uses `setHtmlIfChanged()`. Auto-refresh already runs every 10s.

### Step 8: Config

New `[telegram]` keys in `maestro/config.py` and `maestro.toml`:

```toml
[telegram]
guest_enabled = true
guest_daily_message_cap = 20
guest_holding_reply_cooldown_seconds = 86400
guest_effort = "low"
```

When `guest_enabled = false` or `TELEGRAM_CHAT_ID` is absent, every non-Thomas inbound is silently dropped (no record, no reply).

### Step 9: Tests

All use `mem_fs`; no real network, no real sleep.

- `tests/test_contacts.py` — `ContactStore` CRUD, status transitions.
- `tests/test_observations.py` — `ObservationStore` CRUD; acceptance helper appends a `sender="thomas"` message with expected content.
- `tests/test_messaging.py` — `get_unread_from_thomas` / `get_unread_from_guest` filters; legacy rows without `contact_id` load cleanly.
- `tests/test_telegram_tools_guest_filter.py` — `_handle_read()` drops `sender="guest"` in both `unread_only` branches.
- `tests/test_notifications_telegram.py` — `_poll_loop` dispatches to `_on_message` with correct `chat_id`/`display_name`; four new callback prefixes route correctly; holding-reply rate limit.
- `tests/test_agent_guest.py` — **seven isolation rules**, one test each:
  1. A pending-guest message never appears in `_process_messages()` output.
  2. An approved-guest message never appears in `_process_messages()` output.
  3. `update_self_model` is never called with reflection text containing guest content in any `contemplate()` code path.
  4. `Journal.log_*` is never called from the guest code path (assert by mock spy).
  5. `telegram_read_messages._handle_read()` drops `sender != "thomas"` in both branches.
  6. Guest `query()` call uses `system=GUEST_IDENTITY_PROMPT` and `allowed_tools=["mcp__maestro_observations__raise_to_thomas"]`.
  7. Observation acceptance writes a `sender="thomas"` message with the observation content; dismiss writes none.
- `tests/test_agent_observations.py` — `raise_to_thomas` tool creates a pending observation and sends Telegram; accept route appends a synthetic Thomas message and the next `_process_messages()` surfaces it as Thomas content.
- `tests/browser/test_people_page.py` — authenticated People page renders pending/approved/revoked/observations; buttons POST to the right endpoints (production `create_router()`).

Run:
```bash
python3.12 -m ruff check maestro/ tests/ && \
  python3.12 -m mypy maestro/ && \
  python3.12 -m pytest tests/ -v
python3.12 -m pytest tests/browser/ -v -m browser --no-cov --browser chromium
```

### Step 10: CLAUDE.md

New rows in the core-modules table: `contacts.py`, `observations.py`, `observation_tools.py`. New design-decision bullets:

- **Approved guest messaging** — trust model, three-state contact lifecycle, holding-reply rate limit, daily cap, stateless + sanitized-prompt + single-tool guest replies.
- **Guest isolation invariants** — the seven rules above, plus a file/line anchor to each enforcement point.
- **Noteworthy observation → synthetic Thomas acceptance** — the single-door design, why it preserves the "only Thomas bytes reach identity" invariant.

Add `"respond_to_guests"` to the phase list in the lifecycle section. Document the four new `[telegram]` keys.

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Guest text leaks into identity via an unindexed path we missed | High | Seven isolation rules are unit-tested; `telegram_read_messages` is sender-filtered; guests are never journaled; stateless `query()` with single-tool allowlist. New readers of `MessageStore` added in future PRs must opt into one of the sender-filtered helpers. |
| Guest manipulates Maestro into writing a malicious "observation" that Thomas clicks through | Medium | Dashboard observation card and Telegram notification both render `observation` + `reasoning` + source-message previews in full so Thomas can't approve blind. Observation content is explicitly guest-originated text in the stored record. |
| Inbound storage growth via any guest (pending, approved, or revoked) flooding | Low | Cap is checked per-iteration for each contact at the start of `_respond_to_guests`; any bursts land in `messages.jsonl` and `mark_read` happens only after cap-check + reply, so the per-contact per-iteration outbound overshoot is zero — the cap gates the reply before it is sent. The genuine residual risk is **inbound storage growth, not outbound bypass**: inbound is never rate-limited (see "does **not** rate-limit guest inbound messages" below), and crucially `_on_message` saves the message to `messages.jsonl` *before* the status branch, so pending, approved, *and* revoked contacts can all grow the file unboundedly. Revoking a guest stops replies but does not stop storage writes (`# silent drop — history preserved`). Future mitigations could add a per-contact-per-day inbound ceiling, an auto-revoke threshold, or a max-stored-preview-bytes limit. |
| Holding-reply storm if a guest sends many messages while pending | Low | Rate limit is 24h per `chat_id`. First reply goes out; subsequent pending-state messages within 24h get none. Ephemeral in-memory state resets on restart; acceptable since the rate limit is a courtesy, not a gate. |
| `contextvars` / thread-local misuse leaks guest `contact_id` across queries | Low | Avoided entirely: MCP server is rebuilt per guest with `contact_id` captured in the tool handler's closure. No shared state. |
| Existing `get_unread()` callers break during the rename | Low | Deprecated thin alias during one release cycle. `latest_inbound_message_id()` is already sender-filtered and unaffected. Quality gates (mypy, tests) catch any missed migration. |
| Config misreading: `guest_enabled = true` before `TELEGRAM_CHAT_ID` is set | Low | `_on_message` checks `chat_id == config.telegram_chat_id` first; when unset, Thomas's branch is never taken, and the `guest_enabled` check runs — but with no `telegram_chat_id`, polling itself doesn't start. Defensive no-op. |
| Dashboard endpoints exposed without auth | Low | `BearerAuthMiddleware` protects everything outside `_PUBLIC_PATHS`; new routes are not added to public list. Covered by existing middleware test. |

## What this proposal does NOT do

- It does **not** add multi-channel contacts (email etc.). Telegram-only for v1. `Contact.chat_id` can be generalised later by renaming to `handle` + adding `platform`.
- It does **not** let Maestro initiate messages to guests. Reply-only.
- It does **not** offer per-contact tiered effort levels or per-contact caps. Single `guest_effort` + single `guest_daily_message_cap`.
- It does **not** auto-expire approvals. All revocations are manual.
- It does **not** rate-limit guest **inbound** messages. Only Maestro's replies are capped; inbound is always saved so Thomas has full history. This is the residual risk flagged in the "Inbound storage growth via any guest (pending, approved, or revoked) flooding" Risk Assessment row above: `messages.jsonl` can grow unboundedly under any hostile guest, regardless of approval status.
- It does **not** journal guest content under any `entry_type` or `triggered_by`. `messages.jsonl` is the only durable record.

# 032: Publication Approval Surface

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/artifact_review.py`, `maestro/agent.py`, `maestro/notifications/telegram.py`, `maestro/notifications/base.py`, `maestro/dashboard/routes.py`, `maestro/dashboard/templates/index.html`, `maestro/cli.py`, `maestro.toml`, tests |
| **Depends on** | 030, 031 |

## Description

> Status updated to shipped on 2026-04-27 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Third of the 030 → 033 series. Adds the human-approval UX for publication proposals: Telegram inline buttons and a dashboard Publications tab, both writing to the same `publication_proposals.jsonl` under a shared `file_lock` so a click on either surface cancels the still-pending view of the other. This proposal does **not** include the publisher itself — 033 handles the actual `gh repo create`. Approved proposals queue in `status=approved` until the publisher (033) picks them up; a dashboard status badge makes the queue visible.

Thomas flips `[publication] enabled = true` (from 030's `false` default) as part of this proposal's landing PR. `ArtifactReviewer._emit_publication_if_eligible` now creates a `PublicationProposal` for every `publish` verdict, subject to the name-collision and README-scrub pre-flight checks. End state: a `publish` verdict surfaces as an approvable proposal on both surfaces, and an approval on either flips the shared row.

## Relevant Findings

- `maestro/agent.py` — plan-approval callback wiring in `run_loop()` defines `_on_approve` / `_on_reject` inline and wires them via `await self.notifier.start_approval_polling(...)`.
- `maestro/notifications/telegram.py` — `TelegramNotifier.start_approval_polling` accepts three callbacks and starts a single `_poll_loop`; extending its signature with keyword-only kwargs is backwards-compatible.
- `maestro/notifications/telegram.py` — `_handle_callback` parses `action, entity_id = data.split(":", 1)`; current prefixes are `approve:` / `reject:`. Adding `pub_approve:` / `pub_reject:` avoids any migration — old plan buttons keep working.
- `maestro/notifications/base.py` — the `Notifier` protocol provides default no-op `notify_plan_for_review` / `resolve_plan`; new publication methods follow the same pattern.
- `maestro/dashboard/routes.py` — error types, action functions, HTTP handlers. `approve_plan_action` is the exact template (including the TOCTOU recheck when `update_..._status` returns `False`); auth is automatic via `BearerAuthMiddleware` (`dashboard/auth.py`).
- `maestro/dashboard/templates/index.html` (tab), badge, click delegation, `Promise.all` refresh — the scaffolding duplicated for publications.
- `SubprocessCodeHost.repo_exists` / `.resolve_owner` from 030 are used at emission time for the name-collision pre-check.

## Implementation Plan

All steps have landed (`[x]`). Emission (1) and the notifier protocol/Telegram plumbing (2–3) precede the agent callbacks (4), then the dashboard routes/template (5–6), CLI (7), and default flip (8).

1. [x] **Emission gate.** `maestro/artifact_review.py` (`_emit_publication_if_eligible`, newly added in this proposal — 031 logs INFO for a `publish` verdict without emitting) — when `publication_enabled` and verdict `publish`: `scrub_readme` hard-block → demote to `improve` (`[scrub_blocked:…]`); else `repo_exists(owner, proposed_name)` → demote to `improve` (`[name taken:…]`); else create `PublicationProposal(status="pending_approval", proposed_readme=<scrubbed>, publishable_files=bundle_file_names, scrub_findings=<redact-level>)`, persist, and `await notifier.notify_publication_for_review(...)`.
   - *Depends on:* none.
   - *Acceptance:* clean README + free name → proposal persisted and `notify_publication_for_review` invoked with a `pub_approve:{id}` prefix; a secret or name collision demotes to `improve` with no proposal.
   - *SDK cost:* emission runs inside `review()` after the single LLM query, so it is skipped whole under `budget_throttle_active`; the `scrub_readme` and `repo_exists` pre-checks are themselves zero-SDK-cost.
2. [x] **Notifier protocol.** `maestro/notifications/base.py` — extend `Notifier` with default no-op `notify_publication_for_review(proposal_id, message)` and `resolve_publication(proposal_id, label)`.
   - *Depends on:* none.
   - *Acceptance:* the protocol exposes both methods with no-op defaults so non-Telegram notifiers stay valid.
3. [x] **Telegram routing.** `maestro/notifications/telegram.py` — add `notify_publication_for_review` (mirroring `notify_plan_for_review`, tracking ids in `_publication_messages`), extend `start_approval_polling` with keyword-only `on_publication_approve` / `on_publication_reject`, extend `_handle_callback` to route `pub_approve` / `pub_reject` (legacy `approve`/`reject` preserved, unknown prefix → WARNING), and clean up the publication-messages dict in `_update_message_after_action` / `resolve_publication`.
   - *Depends on:* 2.
   - *Acceptance:* a `pub_approve:{id}` callback invokes `on_publication_approve` and not `on_approve`; legacy `approve:{id}` still routes to `on_approve`; unknown prefix logs a WARNING.
4. [x] **Agent callbacks.** `maestro/agent.py` (`run_loop`) — add `_on_publication_approve` / `_on_publication_reject` (guard `status == "pending_approval"`, flip via `update_proposal_status`, journal a milestone, return `"approved"`/`"ignored"`) and wire them into `start_approval_polling(...)`.
   - *Depends on:* 3.
   - *Acceptance:* `_on_publication_approve` flips to `approved` and journals a milestone; double-approve and a TOCTOU race both return `"ignored"`.
5. [x] **Dashboard routes.** `maestro/dashboard/routes.py` — add `PublicationNotFoundError` / `PublicationNotPendingError`, `approve_publication_action` / `reject_publication_action` (mirroring the plan actions verbatim, including the TOCTOU recheck), and the HTTP handlers `GET /api/publications`, `GET /api/publications/{id}`, `POST .../approve`, `POST .../reject`, `GET /api/artifact-reviews`, with 404/400 mapping identical to the plan handlers.
   - *Depends on:* 1.
   - *Acceptance:* the action functions cover success / not-found / not-pending / TOCTOU; the endpoints return 400 on already-approved and 401 unauthenticated.
6. [x] **Dashboard template.** `maestro/dashboard/templates/index.html` — duplicate the Plans scaffolding: a `publications` section (`#pending-publications` + `#all-publications`), `nav-badge-publications`, `pub-approve` / `pub-reject` click delegation, a `refreshPublications()` in the `Promise.all`, a pending card (name, description, task link, reasoning, `publishable_files` count, `scrub_findings` with `narrative_redactions:*` highlight, `<details>` README in `<pre>` with Copy), and an all-publications card (status badge, filter, `published_url` link, `error` text).
   - *Depends on:* 5.
   - *Acceptance:* browser tests render the pending card, POST approve/reject, filter by status, show the narrative-flag warning, and render an unknown status as a generic badge.
7. [x] **CLI.** `maestro/cli.py` — `maestro publications [-n 20] [--status …] [--json]` tailing `publication_proposals.jsonl`, mirroring `maestro reviews`.
   - *Depends on:* 1.
   - *Acceptance:* the command tails the JSONL and `--status` filters correctly.
8. [x] **Default flip.** `maestro.toml` — flip `[publication] enabled = true`.
   - *Depends on:* 4, 6.
   - *Acceptance:* the shipped `maestro.toml` has `publication.enabled = true`.

## Testing Strategy

All tests use `mem_fs` and mock externals; browser tests use `create_router()`.

- **Emission** (`tests/test_artifact_review.py`): publish + clean README + free name → proposal + `notify_publication_for_review` with `pub_approve:{id}`; secret → demote; name collision → demote.
- **Telegram routing** (`tests/test_telegram_notifier.py`): `pub_approve:{id}` → `on_publication_approve` not `on_approve`; legacy `approve:{id}` preserved; unknown prefix → WARNING.
- **Agent callback** (`tests/component/test_publication_approval.py`): flip to `approved` + milestone; double-approve → `"ignored"`; TOCTOU → `"ignored"`.
- **Dashboard actions** (`tests/test_dashboard_api.py`): success / not-found / not-pending / TOCTOU for both approve and reject.
- **Dashboard HTTP** (`tests/test_dashboard_api.py`): already-approved → 400; reject rechecks status after await; not-found → 404; non-dict body → 400; unauthenticated → 401/redirect.
- **Browser** (`tests/browser/test_publications.py`): pending card with README `<details>` + Approve/Reject; Approve POSTs + refreshes; Reject captures reason; status filter; narrative-flag warning; unknown-status fallback badge.
- **Two-surface idempotency**: Telegram approve → subsequent dashboard `POST /approve` → 400 (`PublicationNotPendingError`), and vice-versa.

Run: `python3.12 -m pytest tests/test_artifact_review.py tests/test_telegram_notifier.py tests/component/test_publication_approval.py tests/test_dashboard_api.py -v && python3.12 -m pytest tests/browser/test_publications.py -v -m browser --no-cov --browser chromium`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Approved proposals pile up with no executor (until 033) | Medium | Visible in the dashboard "approved" column; documented in CLAUDE.md that 032 is inert without 033. 032 + 033 can land in the same deploy if the gap feels awkward. |
| Telegram signature change breaks existing plan approvals | Low | Additions are keyword-only; positional signature preserved; the router accepts legacy `approve:` / `reject:` indefinitely. Regression-tested. |
| Race between Telegram + dashboard approve | Low | Both write via `update_proposal_status` under `file_lock`; the second write sees `status != "pending_approval"` and returns `"ignored"` / raises `PublicationNotPendingError`. Exact parity with plans. |
| Name-collision check gives false confidence | Medium | `repo_exists` returns `False` on ambiguous exit codes; the real collision surfaces at `gh repo create` in 033, which handles it gracefully. |
| Dashboard renders raw README markdown as HTML | Medium | README renders inside `<pre>` (literal text, not parsed). Thomas sees exactly what will be committed. Explicitly tested. |
| `scrub_findings` leak internal details in the dashboard | Low | Findings are `kind:count` (e.g. `redact_path:3`), not raw matches; hard-block findings carry a redacted preview, never the full secret. |

## What landed

`Memory.update_proposal_status(..., expect_status=...)` + `notifications/telegram.py` `pub_approve:` / `pub_reject:` callbacks + `dashboard/routes.py` Publications endpoints + `dashboard/templates/index.html` Publications tab + `maestro publications` CLI (PR #1505). `[publication] enabled` flipped to `true`.

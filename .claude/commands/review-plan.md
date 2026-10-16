Review the single **oldest** Maestro evolution plan currently pending human review against live dashboard data — decide whether it is a needed, valid improvement versus dead weight, duplicative, or overly complex — and prepare (and, only on explicit request, submit) either a rejection for the dashboard reject API or a change request for the dashboard feedback API.

This command reads the **live** pending-review queue (the same `EvolutionPlan` records the dashboard approval surface shows), selects exactly the one plan that has waited longest — the minimum `created_at` — and judges it on its merits against today's tree, then hands you a ready-to-POST rejection payload if it should be declined, or a ready-to-POST change request if the idea is sound but its body needs a fixable edit. Approvals are deliberately out of scope — this command only ever recommends *keep*, prepares a *change request*, or prepares a *rejection* (see `## Safety`).

`$ARGUMENTS` selects mode (any unrecognized token: warn the user, do not abort):
- *(empty)* — review the oldest pending plan and **prepare** a rejection or change-request payload if its verdict calls for one. Do **not** submit anything (dry-run is the default).
- `--submit` — after presenting the verdict and getting the user's explicit go-ahead, POST the prepared rejection or change request to the dashboard API. Without this flag, never POST.
- `--base <url>` — override the dashboard base URL (default is resolved from config, `http://127.0.0.1:8420`).
- Stop codes: `error=no_pending_plans` (queue empty — report and stop); `error=dashboard_auth` (the dashboard returned 401/403 — it is running but the token is missing/wrong; check `MAESTRO_DASHBOARD_TOKEN` and stop); `error=dashboard_unreachable_submit` (`--submit` given but the API cannot be reached — you cannot mutate state, so stop after preparing).

## Endpoint and authentication

The reject and feedback APIs are served by the dashboard (`maestro/dashboard/routes.py`, `reject_plan` and `plan_feedback`). It authenticates with a **Bearer** token; a valid `Authorization: Bearer` header also exempts the request from CSRF (`maestro/dashboard/auth.py`, `CSRFMiddleware`), so no cookie or `X-CSRF-Token` is needed. When `MAESTRO_DASHBOARD_TOKEN` is unset the dashboard runs unauthenticated (loopback dev) and the header is simply omitted.

Resolve the base URL, the on-disk `memory_dir`, and the token once, at the top:

```bash
read -r BASE MEMDIR < <(python3.14 - <<'PY'
import os, tomllib
from pathlib import Path
paths, dash = {}, {}
def merge(fn):
    p = Path(fn)
    if not p.exists():
        return
    with open(p, "rb") as f:
        d = tomllib.load(f)
    paths.update(d.get("paths", {}))
    dash.update(d.get("dashboard", {}))
merge("maestro.toml")
env = os.environ.get("MAESTRO_ENV")
if env:
    merge(f"maestro.{env}.toml")
port = int(dash.get("port", 8420))
print(f"http://127.0.0.1:{port} {paths.get('memory_dir', 'data')}")
PY
)
# --base <url> overrides BASE. Token: env first, then a literal read of .env
# (never source .env — read the KEY=VALUE line verbatim, matching scripts/with-env.sh).
TOKEN="${MAESTRO_DASHBOARD_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f .env ]; then
  TOKEN="$(grep -E '^[[:space:]]*MAESTRO_DASHBOARD_TOKEN=' .env | tail -n1 | sed -e 's/^[[:space:]]*MAESTRO_DASHBOARD_TOKEN=//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^["'\'']//' -e 's/["'\'']$//')"
fi
AUTH=(); [ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")
```

## Steps

1. **Fetch the live pending queue, then select the single oldest plan.** Prefer the dashboard API — it is the source of truth and returns each plan's `plan_content_hash` verbatim, which is exactly what the reject endpoint re-verifies. Paginate `GET /api/plans/pending` **fully**: the endpoint (`maestro/dashboard/routes.py`, `pending_plans`) returns the queue **newest-first** (`sorted(..., key=created_at, reverse=True)`), so the oldest plan is on the *last* page — reading only the first page would hand you the newest-of-first-page, not the true oldest. The bootstrap request omits `n` — the client cannot learn `max_page_size` until a response arrives, and a hardcoded `n` would 422 the very first request whenever an operator lowers the cap below it — so it relies on the server's clamped default, then clamps `n` to the returned `max_page_size` on subsequent pages, walking forward while `offset + count < total` so the oldest plan is never silently below the cut.

   ```bash
   # Capture the HTTP status separately: without it curl (no -f) exits 0 on
   # 401/403/5xx, so the auth-vs-unreachable branching below could not be applied
   # reliably. A connection refusal or --max-time timeout leaves curl exit≠0 and
   # CODE=000, which is the "unreachable" case.
   CODE="$(curl -sS --max-time 10 -o /tmp/pending.json -w '%{http_code}' \
     "${AUTH[@]}" "$BASE/api/plans/pending?offset=0")"
   ```

   On `200`, the body written to `/tmp/pending.json` is `{ "plans": [ …asdict(plan)… ], "total", "offset", "count", "page_size", "max_page_size" }`. Collect every plan dict across all pages, keyed by `plan_id`, retaining its `plan_content_hash`. Branch on `$CODE`:

   - **If `$CODE` is 401 or 403** (bad/missing Bearer token, or CSRF): the dashboard *is* running and reachable — this is an auth problem, not a down server, so do **not** fall back to the JSONL as if it were down (that would mask a real token misconfiguration and produce a payload that then fails to submit). Stop with `error=dashboard_auth` and tell the operator to check `MAESTRO_DASHBOARD_TOKEN` — consistent with step 7, which also treats 401/403 as an auth/CSRF problem.
   - **If the API is unreachable** (`$CODE` is `000` from a connection refusal / `--max-time` timeout, or a 5xx): the dashboard is not running. Fall back to reading the live JSONL directly for **review only**, recomputing the content hash so the payload stays valid for later submission. You cannot submit without a running dashboard — if `--submit` was passed, stop with `error=dashboard_unreachable_submit` after preparing.

     ```bash
     python3.14 - "$MEMDIR" <<'PY'
     import json, sys
     from dataclasses import asdict
     from pathlib import Path
     from maestro.memory import Memory, compute_plan_content_hash
     mem = Memory(Path(sys.argv[1]))
     for p in mem.get_pending_plans():
         print(json.dumps({"plan_id": p.plan_id, "title": p.title,
             "content_hash": compute_plan_content_hash(p),
             "category": p.category, "priority": p.priority, "stage": p.stage,
             "rationale": p.rationale, "proposed_changes": p.proposed_changes,
             "files_affected": p.files_affected,
             "expected_stage_impact": p.expected_stage_impact,
             "acceptance_criteria": p.acceptance_criteria,
             "source_task_id": p.source_task_id, "created_at": p.created_at,
             "reviewer_feedback": [asdict(f) for f in p.reviewer_feedback]}))
     PY
     ```

   - If the queue is empty, report `error=no_pending_plans` and stop.
   - **Select the oldest.** Once the full pending set is collected (from the API across every page, or from the JSONL fallback), pick exactly **one** plan: the one with the **minimum `created_at`**. `created_at` is ISO 8601, so it sorts lexicographically; break a tie deterministically by ascending `plan_id`. Everything below — review, verdict, and payload — covers that single oldest plan only; ignore the rest of the queue.

2. **Gather review context.** Before judging, ground yourself so "duplicative" and "already-done" are evidence-based, not guesses. Read `CLAUDE.md` (the load-bearing invariants) and, for the selected oldest plan, pull the concrete comparanda:
   - **The current tree** — open the files it names in `files_affected` and the symbols its `proposed_changes` describe. A plan proposing something the code *already does* is dead weight (the self-referential proposal-mill failure mode).
   - **The other pending plans** — cross-compare its title, `rationale`, and `files_affected` against the rest of the queue to catch a theme-clustered / restated duplicate that slipped past `detect_restatement` / the saturation guards.
   - **Its reviewer feedback** — the plan's `reviewer_feedback` ledger (returned by the API via `asdict(plan)`, and by the JSONL fallback above): the entries still unaddressed (`addressed_at` is null) and the curator's latest `response` on addressed ones, so you neither re-request a change the curator already made nor ignore its explanation for declining one. A `200 queued` change request is only a `plan_feedback` message until the loop folds it into the ledger on a later iteration, so also check `messages.jsonl` for a still-unread queued request on this plan and count it as unaddressed feedback:

     ```bash
     python3.14 - "$MEMDIR" "<plan_id>" <<'PY'
     import sys
     from pathlib import Path
     from maestro.messaging import MessageStore
     prefix = sys.argv[2] + "@"
     for m in MessageStore(Path(sys.argv[1])).get_unread():
         if m.message_type == "plan_feedback" and (m.reply_to or "").startswith(prefix):
             print(m.message_id, m.timestamp, m.content[:200])
     PY
     ```
   - **What already landed or was already rejected** — recently merged work and prior rejections, so a re-proposal of a shipped or already-declined idea is caught. Sources: `git log --oneline -30`, `data/version_archive.jsonl` (merged plans) and `data/rejection_history.jsonl` if present, and `python3.14 -m maestro list-versions`.

3. **Evaluate the plan against the rubric** in `## Review rubric`. Reach one grounded verdict: **KEEP**, **REQUEST_CHANGES**, or **REJECT**. Cite the specific evidence — a file/symbol that already implements it, the sibling plan it duplicates, the invariant it violates, or the concrete over-engineering — never a vibe. If you are genuinely unsure, default to **KEEP** and flag it for a human (do not reject on speculation). If the plan already carries unaddressed reviewer feedback (including a queued, not-yet-folded request), the curator still owes it a pass: do not stack another request — report the pending feedback and treat the plan as **KEEP, pending curation** (the endpoint refuses a further entry once `MAX_UNADDRESSED_FEEDBACK` are unaddressed anyway).

4. **Compose the rejection reason** if the verdict is REJECT. This is not throwaway text: a rejection feeds `learn_from_rejection()` (`maestro/agent.py`), whose `RejectionCategory` aggregates into `reflect()`, so a wrong or vague reason poisons the agent's future planning. Write it specific, actionable, and self-contained — name the concrete redundancy/flaw and (where useful) what *would* make the idea worth proposing. Keep it under the `rejection_reason_max` cap (2000 chars).

4b. **Compose the change request** if the verdict is REQUEST_CHANGES. Address the text to the curator — it enters the curate prompt via `render_feedback_section` (`maestro/plan_feedback.py`), and `curate_pending_plans` refines the plan body and answers it. Name each concrete edit wanted ("drop `maestro/foo.py` from scope", "replace criterion 2 with a test that asserts X") and cite the evidence for it. Keep it within `FEEDBACK_TEXT_MAX_CHARS` (1000 chars) — much tighter than the 2000-char rejection cap; the endpoint returns 400 over it. No `RejectionCategory` learning happens: the plan stays `pending_review`, and the refined plan resurfaces as the oldest pending plan for re-review on a later run.

5. **Prepare the rejection payload.** For a REJECT, assemble the exact body the reject endpoint expects — the `content_hash` **must** be the `plan_content_hash` from the *same* fetch in step 1 (content-hash binding: a stale hash is a 409):

   ```json
   { "content_hash": "<plan_content_hash>", "reason": "<your reason>" }
   ```

   and the ready-to-run call. **Never** interpolate the LLM-composed reason into a single-quoted `-d '{…}'` literal: a rejection reason is prose (naming symbols, citing code, up to 2000 chars) and almost always contains an apostrophe (`plan's`, `doesn't`) or a double quote — a literal `'` terminates the single-quoted shell string and a literal `"` yields invalid JSON the endpoint rejects with `400 "Invalid JSON body"`, and because the reason feeds `learn_from_rejection()` a garbled reason also corrupts rejection learning. Build the body with a real encoder and stream it via `--data @-`, and capture the HTTP status so the step-7 branching below applies:

   ```bash
   # Encode the reason with a real JSON encoder so apostrophes, "quotes", and
   # newlines are always safe. The quoted heredoc (<<'REASON') passes the reason
   # body verbatim — no shell expansion or word-splitting touches it.
   python3.14 -c 'import json,sys; print(json.dumps({"content_hash": sys.argv[1], "reason": sys.stdin.read().rstrip("\n")}))' \
     "<plan_content_hash>" > /tmp/reject-body.json <<'REASON'
   <your full rejection reason, verbatim>
   REASON

   CODE="$(curl -sS -X POST "${AUTH[@]}" -H "Content-Type: application/json" \
     --data @/tmp/reject-body.json -o /tmp/reject.json -w '%{http_code}' \
     "$BASE/api/plans/<plan_id>/reject")"
   # Branch on $CODE per step 7: 200 rejected; 409 stale hash (re-fetch/re-review);
   # 400/404 skip; 401/403 stop (auth); 429 back off.
   ```

   **Prepare the change-request payload.** For a REQUEST_CHANGES, the feedback endpoint takes `{"content_hash", "text"}` (`PlanFeedbackBody`, `extra="forbid"` — any other key is a 400). Send the `plan_content_hash` from the same step-1 fetch. The text is prose for the same reasons as a rejection reason, so the same rule holds — never a single-quoted `-d '{…}'` literal; encode it and stream it:

   ```bash
   python3.14 -c 'import json,sys; print(json.dumps({"content_hash": sys.argv[1], "text": sys.stdin.read().rstrip("\n")}))' \
     "<plan_content_hash>" > /tmp/feedback-body.json <<'FEEDBACK'
   <your full change request, verbatim>
   FEEDBACK

   CODE="$(curl -sS -X POST "${AUTH[@]}" -H "Content-Type: application/json" \
     --data @/tmp/feedback-body.json -o /tmp/feedback.json -w '%{http_code}' \
     "$BASE/api/plans/<plan_id>/feedback")"
   # Branch on $CODE per step 7: 200 queued; 400/404 skip; 409 read the error;
   # 501 stop (messaging); 401/403 stop (auth); 429 back off.
   ```

6. **Present the verdict.** Show one row for the selected oldest plan: `plan_id` (short), title, category/priority/stage, its `created_at`, verdict (KEEP / REQUEST_CHANGES / REJECT), the one-line reason, and — for a KEEP — a one-line note on why it is worth approving. For a REQUEST_CHANGES or a REJECT, follow it with the prepared payload as a copy-pasteable block so it can be fed into the API by hand. This is the deliverable in the default (no-`--submit`) mode — stop here.

7. **Submit (only with `--submit`).** Show the verdict from step 6, then **ask the user for explicit confirmation** to POST (name the exact plan id *and* the action — reject, or request changes). Only on a clear yes, POST the prepared payload:
   - Pace the submission under the approval rate limit (**30/min** per IP; `MutationRateLimitMiddleware`) — a single request here, so pacing rarely bites.
   - Handle a **reject** response: `200 {"status":"rejected"}` → done; `409` → the plan changed since you read it — **re-fetch that plan, re-review it, and only re-submit with the fresh hash if the verdict still holds** (never blind-retry a stale hash); `400 "not pending review"` → it was approved/rejected concurrently — skip; `404` → gone — skip; `401`/`403` → auth/CSRF problem, stop and surface (check the token); `429` → back off and retry once.
   - Handle a **feedback** response (`plan_feedback`): `200 {"status":"queued"}` → done; the loop folds it into the plan's `reviewer_feedback` and the curator refines the plan; `400` → not pending, empty or over-long text, or a body validation error — skip and surface; `404` → gone — skip; `409` → read the `error` body: "not being serviced" means `feedback_serviceable` is false in this environment (`[loop] plan_feedback_enabled` off, curation off, or the `external_delivery` focus) — do **not** retry; tell the operator and offer to fall back to KEEP or to re-review for REJECT; "already holds … unaddressed" means a curation pass is pending — skip; `501` → messaging not configured — stop; `401`/`403` → auth — stop; `429` → back off and retry once. Unlike reject, a stale `content_hash` is accepted (recorded so the curator can flag it), so there is no re-fetch loop — still always send the hash from the same fetch.
   - Report a final summary: rejected (id), change requested (id, queued), or skipped with reason, or the KEEP left untouched in the queue for a human to approve.

## Review rubric

Judge the plan on the three questions the way a careful maintainer would, grounded in the evidence from step 2. A plan earns **KEEP** only when it is needed **and** valid **and** an improvement; any of the reject signals is sufficient to reject.

**Needed / valid / improvement (→ lean KEEP):**
- **Needed** — addresses a real gap, bug, or capability that the tree does *not* already cover. The `rationale` points at a concrete problem, not a restatement of existing behavior.
- **Valid** — technically sound and consistent with the load-bearing invariants (human-in-the-loop evolution, the chokepoint sole-owners, fail-loud / no silenced exceptions, context-bloat caps, the stage model). Cites files/symbols that actually exist. `acceptance_criteria` are concrete and testable (not vacuous).
- **Improvement** — a net gain in capability or clarity that outweighs its cost, and it is small and focused ("one thing well"), respecting the stage model.

**Fixable body defect (→ REQUEST_CHANGES):** the plan passes *Needed* and *Improvement* but fails part of *Valid* or *Overly complex* in a way an edit to its body can fix — narrow an over-broad scope, replace a vacuous or untestable acceptance criterion, correct a cited file or symbol, split off a bundled change, add the missing per-file verification. A change request keeps the sound idea and teaches the curator the edit, where a rejection would discard it and feed `learn_from_rejection()` the wrong lesson.

**Dead weight / duplicative / overly complex / invalid (→ REJECT, with the category named in the reason)** — signals no body edit can fix:
- **Already done** — the described change already exists in the tree or is already an invariant. The clearest dead weight; cite the file/symbol that implements it.
- **Duplicative** — overlaps another pending plan (theme cluster / restatement), or re-proposes a recently merged or already-rejected idea. Cite the sibling plan id or the version/rejection record.
- **Overly complex** — adds a flag, knob, module, or abstraction disproportionate to the benefit; sprawls across many files without per-file verification; introduces config nothing consumes; or bundles several changes where one focused change was asked for.
- **Not needed / churn** — solves a non-problem, or only touches its own measurement/scaffolding (a plan to measure the measuring), with no measurable capability or clarity gain.
- **Invalid** — conflicts with a load-bearing invariant or an AST-enforced guard, cites files/symbols that do not exist, skips or rushes an evolutionary stage, or ships vacuous/untestable acceptance criteria.

When in doubt between a marginal improvement and churn, prefer KEEP and flag the doubt for the human — a wrong rejection both loses a real improvement and teaches the agent the wrong lesson. When the idea is sound and only the body is wrong, prefer REQUEST_CHANGES over REJECT.

## Safety

- **Never approve through this command.** Approval is a human decision made on the dashboard; this command only recommends *keep* or prepares/sends a *change request* or a *rejection*. Do not call the approve endpoint.
- **Default is read-only.** Without `--submit`, prepare and present only — POST nothing. `--submit` mutates state and always requires an explicit user confirmation naming the plan id first.
- **Content-hash binding.** Only ever POST the `plan_content_hash` returned by the same fetch that produced the plan you reviewed. On a reject `409`, re-fetch and re-review before considering a retry — never restamp a stale hash to force it through. A feedback `409` is not a hash conflict (the feedback endpoint accepts a stale hash) — it means feedback is not serviced or the ledger is full; handle it per step 7 and do not retry.
- **Ground the rejection or change request.** A rejection reason or change request must cite concrete evidence from the live code or the queue. Never fabricate a reason; a wrong reason corrupts the rejection-learning cycle.
- **Respect the rate limit.** On `429`, back off before retrying.
- **Never edit `evolution_plans.jsonl` (or the plan state) directly** — the only sanctioned mutation paths are the dashboard reject API (which re-verifies existence, pending status, and the content hash) and the dashboard feedback API (which re-verifies existence, pending status, and serviceability). Never call `Memory.add_plan_feedback` directly; the loop's fold is its only writer. This command runs no quality gates and touches no git state.

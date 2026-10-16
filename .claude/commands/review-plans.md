Review the Maestro evolution plans currently pending human review against live dashboard data — decide which are needed, valid improvements versus dead weight, duplicative, or overly complex — and prepare (and, only on explicit request, submit) rejection responses for the dashboard reject API.

This command reads the **live** pending-review queue (the same `EvolutionPlan` records the dashboard approval surface shows) and judges each one on its merits against today's tree, then hands you ready-to-POST rejection payloads. Approvals are deliberately out of scope — this command only ever recommends *keep* or prepares a *rejection* (see `## Safety`).

`$ARGUMENTS` selects scope and mode (any unrecognized token: warn the user, do not abort):
- *(empty)* — review **every** pending plan and **prepare** rejection payloads for the ones that should be rejected. Do **not** submit anything (dry-run is the default).
- `--submit` — after presenting the full verdict table and getting the user's explicit go-ahead, POST the prepared rejections to the dashboard API. Without this flag, never POST.
- `<plan_id> [<plan_id> …]` — restrict the review to the named plan id(s) instead of the whole queue.
- `--base <url>` — override the dashboard base URL (default is resolved from config, `http://127.0.0.1:8420`).
- Stop codes: `error=no_pending_plans` (queue empty — report and stop); `error=dashboard_auth` (the dashboard returned 401/403 — it is running but the token is missing/wrong; check `MAESTRO_DASHBOARD_TOKEN` and stop); `error=dashboard_unreachable_submit` (`--submit` given but the API cannot be reached — you cannot mutate state, so stop after preparing).

## Endpoint and authentication

The reject API is served by the dashboard (`maestro/dashboard/routes.py`, `reject_plan`). It authenticates with a **Bearer** token; a valid `Authorization: Bearer` header also exempts the request from CSRF (`maestro/dashboard/auth.py`, `CSRFMiddleware`), so no cookie or `X-CSRF-Token` is needed. When `MAESTRO_DASHBOARD_TOKEN` is unset the dashboard runs unauthenticated (loopback dev) and the header is simply omitted.

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

1. **Fetch the live pending queue.** Prefer the dashboard API — it is the source of truth and returns each plan's `plan_content_hash` verbatim, which is exactly what the reject endpoint re-verifies. Paginate `GET /api/plans/pending` fully: the bootstrap request omits `n` — the client cannot learn `max_page_size` until a response arrives, and a hardcoded `n` would 422 the very first request whenever an operator lowers the cap below it — so it relies on the server's clamped default, then clamps `n` to the returned `max_page_size` on subsequent pages, walking forward while `offset + count < total` so no approvable plan is silently below the cut.

   ```bash
   # Capture the HTTP status separately: without it curl (no -f) exits 0 on
   # 401/403/5xx, so the auth-vs-unreachable branching below could not be applied
   # reliably. A connection refusal or --max-time timeout leaves curl exit≠0 and
   # CODE=000, which is the "unreachable" case.
   CODE="$(curl -sS --max-time 10 -o /tmp/pending.json -w '%{http_code}' \
     "${AUTH[@]}" "$BASE/api/plans/pending?offset=0")"
   ```

   On `200`, the body written to `/tmp/pending.json` is `{ "plans": [ …asdict(plan)… ], "total", "offset", "count", "page_size", "max_page_size" }`. Collect every plan dict, keyed by `plan_id`, retaining its `plan_content_hash`. Branch on `$CODE`:

   - **If `$CODE` is 401 or 403** (bad/missing Bearer token, or CSRF): the dashboard *is* running and reachable — this is an auth problem, not a down server, so do **not** fall back to the JSONL as if it were down (that would mask a real token misconfiguration and produce payloads that then fail to submit). Stop with `error=dashboard_auth` and tell the operator to check `MAESTRO_DASHBOARD_TOKEN` — consistent with step 7, which also treats 401/403 as an auth/CSRF problem.
   - **If the API is unreachable** (`$CODE` is `000` from a connection refusal / `--max-time` timeout, or a 5xx): the dashboard is not running. Fall back to reading the live JSONL directly for **review only**, recomputing the content hash so payloads stay valid for later submission. You cannot submit without a running dashboard — if `--submit` was passed, stop with `error=dashboard_unreachable_submit` after preparing.

     ```bash
     python3.14 - "$MEMDIR" <<'PY'
     import json, sys
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
             "source_task_id": p.source_task_id, "created_at": p.created_at}))
     PY
     ```

   - If the queue is empty, report `error=no_pending_plans` and stop.
   - If `$ARGUMENTS` named specific plan ids, drop everything else. Warn about any named id that is not in the pending set (already approved/rejected/obsolete, or unknown) and continue with the rest.

2. **Gather review context.** Before judging, ground yourself so "duplicative" and "already-done" are evidence-based, not guesses. Read `CLAUDE.md` (the load-bearing invariants) and, for the plans in front of you, pull the concrete comparanda:
   - **The current tree** — for each plan, open the files it names in `files_affected` and the symbols its `proposed_changes` describe. A plan proposing something the code *already does* is dead weight (the self-referential proposal-mill failure mode).
   - **The other pending plans** — cross-compare titles, `rationale`, and `files_affected` to catch theme-clustered / restated duplicates that slipped past `detect_restatement` / the saturation guards.
   - **What already landed or was already rejected** — recently merged work and prior rejections, so a re-proposal of a shipped or already-declined idea is caught. Sources: `git log --oneline -30`, `data/version_archive.jsonl` (merged plans) and `data/rejection_history.jsonl` if present, and `python3.14 -m maestro list-versions`.

3. **Evaluate each plan against the rubric** in `## Review rubric`. For every plan, reach one grounded verdict: **KEEP** or **REJECT**. Cite the specific evidence — a file/symbol that already implements it, the sibling plan it duplicates, the invariant it violates, or the concrete over-engineering — never a vibe. If you are genuinely unsure, default to **KEEP** and flag it for a human (do not reject on speculation).

4. **Compose the rejection reason** for each REJECT. This is not throwaway text: a rejection feeds `learn_from_rejection()` (`maestro/agent.py`), whose `RejectionCategory` aggregates into `reflect()`, so a wrong or vague reason poisons the agent's future planning. Write it specific, actionable, and self-contained — name the concrete redundancy/flaw and (where useful) what *would* make the idea worth proposing. Keep it under the `rejection_reason_max` cap (2000 chars).

5. **Prepare the rejection payloads.** For each REJECT, assemble the exact body the reject endpoint expects — the `content_hash` **must** be the `plan_content_hash` from the *same* fetch in step 1 (content-hash binding: a stale hash is a 409):

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

6. **Present the verdict table.** For the whole queue, show one row per plan: `plan_id` (short), title, category/priority/stage, verdict (KEEP / REJECT), the one-line reason, and — for KEEPs — a one-line note on why it is worth approving. Follow it with the prepared REJECT payloads as a copy-pasteable block so they can be fed into the API by hand. This is the deliverable in the default (no-`--submit`) mode — stop here.

7. **Submit (only with `--submit`).** Show the table from step 6, then **ask the user for explicit confirmation** to POST the rejections (list exactly which plan ids will be rejected). Only on a clear yes, POST each prepared payload:
   - Pace submissions under the approval rate limit (**30/min** per IP; `MutationRateLimitMiddleware`) — for large batches, one request roughly every ~2s.
   - Handle each response: `200 {"status":"rejected"}` → done; `409` → the plan changed since you read it — **re-fetch that plan, re-review it, and only re-submit with the fresh hash if the verdict still holds** (never blind-retry a stale hash); `400 "not pending review"` → it was approved/rejected concurrently — skip; `404` → gone — skip; `401`/`403` → auth/CSRF problem, stop and surface (check the token); `429` → back off and continue.
   - Report a final summary: rejected (ids), skipped with reason, and any KEEPs left untouched in the queue for a human to approve.

## Review rubric

Judge each plan on the three questions the way a careful maintainer would, grounded in the evidence from step 2. A plan earns **KEEP** only when it is needed **and** valid **and** an improvement; any of the reject signals is sufficient to reject.

**Needed / valid / improvement (→ lean KEEP):**
- **Needed** — addresses a real gap, bug, or capability that the tree does *not* already cover. The `rationale` points at a concrete problem, not a restatement of existing behavior.
- **Valid** — technically sound and consistent with the load-bearing invariants (human-in-the-loop evolution, the chokepoint sole-owners, fail-loud / no silenced exceptions, context-bloat caps, the stage model). Cites files/symbols that actually exist. `acceptance_criteria` are concrete and testable (not vacuous).
- **Improvement** — a net gain in capability or clarity that outweighs its cost, and it is small and focused ("one thing well"), respecting the stage model.

**Dead weight / duplicative / overly complex / invalid (→ REJECT, with the category named in the reason):**
- **Already done** — the described change already exists in the tree or is already an invariant. The clearest dead weight; cite the file/symbol that implements it.
- **Duplicative** — overlaps another pending plan (theme cluster / restatement), or re-proposes a recently merged or already-rejected idea. Cite the sibling plan id or the version/rejection record.
- **Overly complex** — adds a flag, knob, module, or abstraction disproportionate to the benefit; sprawls across many files without per-file verification; introduces config nothing consumes; or bundles several changes where one focused change was asked for.
- **Not needed / churn** — solves a non-problem, or only touches its own measurement/scaffolding (a plan to measure the measuring), with no measurable capability or clarity gain.
- **Invalid** — conflicts with a load-bearing invariant or an AST-enforced guard, cites files/symbols that do not exist, skips or rushes an evolutionary stage, or ships vacuous/untestable acceptance criteria.

When in doubt between a marginal improvement and churn, prefer KEEP and flag the doubt for the human — a wrong rejection both loses a real improvement and teaches the agent the wrong lesson.

## Safety

- **Never approve through this command.** Approval is a human decision made on the dashboard; this command only recommends *keep* or prepares/sends *rejections*. Do not call the approve endpoint.
- **Default is read-only.** Without `--submit`, prepare and present only — POST nothing. `--submit` mutates state and always requires an explicit user confirmation naming the plan ids first.
- **Content-hash binding.** Only ever POST the `plan_content_hash` returned by the same fetch that produced the plan you reviewed. On a `409`, re-fetch and re-review before considering a retry — never restamp a stale hash to force it through.
- **Ground every rejection.** A rejection reason must cite concrete evidence from the live code or the queue. Never fabricate a reason; a wrong reason corrupts the rejection-learning cycle.
- **Respect the rate limit.** Pace submissions under 30/min; on `429`, back off.
- **Never edit `evolution_plans.jsonl` (or the plan state) directly** — the only sanctioned mutation path is the dashboard reject API, which re-verifies existence, pending status, and the content hash. This command runs no quality gates and touches no git state.

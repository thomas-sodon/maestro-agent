Run a root-cause analysis on a Maestro bug or failure and post it as a GitHub issue.

This command is for **diagnosis**, not remediation. The output explains *why* something broke, not how to paper over it. The desired runtime posture is **fail hard, fail loud, do not restart the loop** — `docker-compose.yml` already sets `restart: "no"` on the `loop` service, so the container stays down after a crash and the logs survive intact for triage. Suggestions in the issue must preserve that posture.

`$ARGUMENTS` is a space-separated list of tokens. Recognized flag tokens are listed below; **everything else is joined into a free-text bug description** — a prompt describing where a bug appears to be occurring. The presence or absence of a bug description selects the mode (see **Modes**).

Recognized tokens:
- `--data-dir=PATH` — explicit path to the live `data/` directory (overrides auto-detection)
- `--container=NAME` — read logs via `docker exec` on this container instead of the host bind-mount (default: `maestro-loop` if `--data-dir` is not given and host bind-mount lookup fails)
- `--iteration=N` — anchor analysis to a specific loop iteration
- `--phase=NAME` — anchor analysis to a specific loop phase (e.g. `evolution`, `audit_self_model`)
- `--since=ISO8601` — only consider events at or after this timestamp
- `--dry-run` — produce the RCA report on stdout but do not create the GitHub issue
- *(free text)* — a bug description / suspected location. Selects **bug mode**: the analysis must root-cause the described behavior, treating the description as the symptom to explain.
- *(no free text)* — selects **incident mode**: auto-anchor to the most recent non-transient failure in the live data.

## Modes

The command runs in one of two modes, chosen by whether `$ARGUMENTS` contains a free-text bug description:

- **Incident mode** (no free text) — the failure is *discovered* from live runtime evidence. Locate the live `data/` dir, anchor on the most recent non-transient failure, and walk outward. This is the original behavior; the live data dir is **required** and the command stops if it cannot be found.

- **Bug mode** (free text present) — the failure is *described by the user*, not discovered from logs. The bug description is the anchor symptom. The investigation is **code-first**: read the implicated modules, trace the data/control flow, and identify the defect that produces the described behavior. Live logs are **optional corroborating evidence** — pull them if a data dir is available and they bear on the described bug, but their absence is not a blocker. `--iteration` / `--phase` / `--since`, if given, narrow the supporting log evidence.

In both modes the root-cause walk, the forbidden-suggestion rules, the report template, and the posting/safety steps are identical. The only difference is how the anchor symptom and evidence are gathered.

## Authentication

GitHub access needs `GITHUB_TOKEN` from `.env` in the project root. Run every `gh`, `git push`, and `git fetch` command through the wrapper, which loads `.env` and then execs the command; the examples below already use it:

```
scripts/with-env.sh <command>
```

If `.env` does not exist or the token is empty, stop before posting and tell the user to populate it. The analysis itself does not need the token — only the final post step does.

## Steps

1. **Parse $ARGUMENTS and pick the mode.** Apply the flag tokens above. Join every remaining (non-flag) token into the free-text **bug description**. If it is non-empty, you are in **bug mode**; otherwise **incident mode**. If the user passed both `--data-dir` and `--container`, prefer `--data-dir` (host filesystem reads are cheaper than `docker exec`) and warn that `--container` is ignored. In bug mode, surface the bug description verbatim in the report and make the whole analysis explain it; in incident mode there is no description and the anchor comes from the logs.

2. **Locate the live data directory.** In **incident mode** this is required; in **bug mode** it is best-effort — if no data dir is found, skip the log/SDK/journal evidence steps (3–7) and go straight to the code-history and code-investigation steps (8 and 8b), noting in the report that no live data was available. In order:
   - If `--data-dir` is set, use it. Verify it contains `lifecycle.json` and `logs/maestro.log`; in incident mode stop if not; in bug mode warn that the path is invalid, skip steps 3–7, and proceed to steps 8 and 8b.
   - Else, try the host bind-mount of the loop container:
     ```
     docker inspect maestro-loop --format '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}'
     ```
     If the result is non-empty and `<source>/data/lifecycle.json` exists, use `<source>/data`.
   - Else, fall back to `docker exec` against `--container` (default `maestro-loop`). Inside that container the repo is mounted at `/workspace` (see `docker-compose.yml`: `working_dir: /workspace`, and the loop healthcheck reads `/workspace/data/loop.heartbeat`), so the live data dir is `$DATA_DIR=/workspace/data`. All later reads in this branch must use `docker exec <container> cat $DATA_DIR/<file>` / `docker exec <container> ls $DATA_DIR/<dir>` (e.g. `docker exec <container> cat /workspace/data/lifecycle.json`). Mention the fallback in the report so the reader knows the evidence came through `docker exec`.
   - If none of the above work: in **incident mode**, stop and tell the user there is no live Maestro data to analyze; in **bug mode**, note in the report that no live data was available and proceed to the code-history and code-investigation steps (8 and 8b).

   Record the resolved data dir as `$DATA_DIR` and use it for every later read. Never read from `/opt/maestro-dev/data` — that path is a stale dev-tree directory that the live loop does not write to.

**Steps 3–7 gather live runtime evidence.** They always run in incident mode. In bug mode they run only if a data dir was found in step 2, and only to corroborate the described bug — treat their output as supporting evidence, not as the anchor. If no data dir was found in bug mode, skip to step 8 (its repo-root `git log` needs no data dir) and then step 8b.

3. **Read the runtime state.** From `$DATA_DIR/lifecycle.json` extract: `iteration`, `current_phase`, `next_phase`, `consecutive_rate_limits`, `consecutive_cli_crashes`, `consecutive_stream_stalls`, `budget_throttle_entered_at`, `rate_limit_idle_entered_at`. This is the single most important snapshot — it tells you whether the loop is *currently* mid-failure, mid-throttle, or healthy. Quote it verbatim in the report.

4. **Pull the app-log tail.** Tail the last **400** lines of `$DATA_DIR/logs/maestro.log` (or `docker exec` equivalent), then parse them in Python using the same regex as `maestro.logs.parse_app_log_tail` (header format: `^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \[(\w+)\] ([\w.]+): (.*)`). Continuation lines (tracebacks, multi-line tool output) attach to their preceding header. Filter to entries with `level in {"WARNING", "ERROR", "CRITICAL"}` *and* keep all entries within ±60 s of any such entry, so surrounding context is preserved. If `--since` is set, drop everything earlier. If `--iteration=N` or `--phase=NAME` is set, narrow further to entries that mention them.

5. **Pick the anchor failure.** In **bug mode** the anchor is the user's bug description, not a log event — skip the rest of this step except to note any log entries that match the described symptom (use them as corroboration). In **incident mode**, from the filtered tail, identify the earliest event that is **not** in the transient set. Transient (do *not* anchor on these — the loop already handles them):
   - `RATE_LIMIT` / `rate_limit_idle` / `Entering rate-limit idle`
   - `STALE_SESSION` / session-resume retries
   - `CLI_CRASH` / SDK CLI restart-in-place
   - `STREAM_STALL` / `stream_idle_timeout` retries
   - `allowed_warning` throttle / `Entering budget throttle`

   Anchor on a non-transient event: an unhandled exception traceback, a `CRITICAL` line, a `Failed to …` ERROR that is not paired with a successful retry shortly after, a quality-gate failure that wasn't auto-fixed, a crash milestone written by `maestro.crash.handle_crash`, or an invariant violation (e.g. `AssertionError`, `KeyError`, `ValidationError`). If every recent ERROR is transient, say so explicitly in the report and stop before posting — there is nothing to RCA.

6. **Pull SDK session logs around the anchor.** From `$DATA_DIR/logs/sdk/<date>/` covering the date(s) spanning ±5 minutes of the anchor timestamp, list session files and read the 1–3 sessions whose mtime brackets the anchor. SDK session logs contain the prompt, tool calls, and tool results — they are the only place where the *content* of a Claude turn is visible. Quote only the directly relevant span; never paste an entire session into the issue. Redact obvious tokens / `Bearer ` headers / API keys before quoting.

7. **Cross-check journal and audit signals.** Read the tail of `$DATA_DIR/journal.jsonl` (last 30 entries) and `$DATA_DIR/audit_history.jsonl` (last 20). Look for:
   - A `crash` milestone (`triggered_by == "crash"`) at or near the anchor — `maestro.crash.handle_crash` writes one when the previous run died without clean shutdown.
   - An `audit_observation` entry naming the same file/phase/symptom — Maestro's own audit may already have flagged the issue.
   - A self-directed task that was running at the anchor iteration — its `linked_question` and `Targeting:` line often contain the proximate trigger.

   If any of these exist, name them in the report; they shorten triage massively.

8. **Map to recent code changes.** Run `git log --oneline -20` from the repo root — this runs in both modes and needs no data dir. In incident mode also run `git log --since="<anchor_timestamp - 7d>" --pretty=format:'%h %ad %s' --date=iso`; if the anchor symptom involves a specific module, also `git log --oneline -10 -- maestro/<module>.py`. In bug mode the implicated module(s) are not known yet — step 8b identifies them — so the module-scoped `git log` is **deferred to step 8b**; do not guess a module here. The goal is to identify whether a recent merged plan plausibly introduced the regression. Mention the suspect commit(s) by SHA in the report; do not assert causation without evidence.

8b. **Investigate the implicated code (bug mode — the core of this mode).** Starting from the bug description, locate the relevant code and trace it to the defect. This is where bug mode does its real work; incident mode may also use it when the anchor points at specific code.
   - **Find the surface.** Use Grep/Glob to map the description's nouns and behaviors to functions, files, and call sites. Read the implicated functions in full, plus their direct callers and callees — don't reason from a single excerpt.
   - **Scope the history (bug-mode deferral from step 8).** Now that the implicated module(s) are known, run `git log --oneline -15 -- maestro/<module>.py` for each to see whether a recent merged change plausibly introduced the defect; cite suspect commit(s) by SHA in the report without asserting causation.
   - **Trace the data/control flow.** Follow the values and branches that produce the described behavior: where does the bad state originate, where is the wrong branch taken, which precondition is missing or which contract between two modules is violated. Check the relevant CLAUDE.md "Architecture invariants" — many bugs are an invariant being broken.
   - **Check the tests.** Find the tests covering this code (`tests/test_<module>.py`). A missing test for the described case, or a test that asserts the buggy behavior, is itself evidence and pins the defect to `file:line`.
   - **Confirm against evidence.** If live logs were available (steps 4–7), check that they are consistent with the code-level finding. State explicitly when the code path can produce the described symptom and when it cannot (which would mean the description is inaccurate or the bug is elsewhere — say so).
   - Quote the decisive code spans by `file:line` in the report, tight (5–15 lines), the same way log excerpts are quoted.

9. **Walk to the root cause.** With the evidence in hand, separate the layers:
   - **Symptom** — the externally visible failure (loop stopped, container exited, error logged, healthcheck failed), or in bug mode the behavior described in the bug prompt.
   - **Proximate cause** — the immediate exception or invariant violation, with `file:line`.
   - **Root cause** — the upstream defect that produced the bad state: a missing precondition, a misclassified error category, an unbounded data flow, a broken invariant from CLAUDE.md's "Architecture invariants" list, a contract mismatch between two modules, or a recent change that introduced the defect.

   You do not have to invent a root cause. If the evidence only goes as far as the proximate cause, say so honestly: `Root cause unidentified — evidence supports proximate cause only. Next investigation step: <specific log/file/test to inspect>`. A truthful "we don't know yet" is more useful than a confident guess.

10. **Decide whether to include a suggested fix.** Only include one if all of the following hold:
    - The root cause was identified with evidence in step 9 (not just a proximate cause).
    - The fix is a **root-cause** change at the identified `file:line`, not a workaround.
    - The fix does not violate the forbidden-suggestion rules below.

    If any condition fails, omit the "Suggested fix" section entirely. The issue is allowed — and often expected — to ship without a fix suggestion.

11. **Validate the suggestion against the forbidden rules** (see "Forbidden suggestion patterns" below). If your candidate fix matches any forbidden pattern, drop it and replace with `Suggested fix: none — see Forbidden patterns. Investigation pointer: <…>`.

12. **Build the report.** Use the template in "Issue body template" below. Keep quoted log spans tight — 5–15 lines each, with a leading `-- log excerpt --` marker. Include exact timestamps. Reference all evidence files by path so a future reviewer can reopen them.

13. **Verify container restart posture.** Read `docker-compose.yml` and confirm the `loop` service still has `restart: "no"`. If it has been changed to anything else (`always`, `unless-stopped`, `on-failure`), call that out as a `Posture regression` in the report — restoring `restart: "no"` is a precondition for clean RCA logs in the future. Do **not** modify `docker-compose.yml` from this command.

14. **Post the issue.** In `--dry-run` mode, print the title and body to stdout and stop. Otherwise, derive `<repo>` from `scripts/with-env.sh gh repo view --json nameWithOwner -q .nameWithOwner`. Write the composed `<body>` to a temporary file (e.g. via the Write tool to a path such as `<tmp>/rca-issue-body.md`) so no line of the quoted evidence — log excerpts, SDK transcripts, Python reprs — can be mistaken for a shell heredoc terminator, then post it with `--body-file`:

    ```
    scripts/with-env.sh gh issue create \
      --repo "<repo>" \
      --title "<title>" \
      --label "rca" --label "bug" \
      --body-file "<tmp>/rca-issue-body.md"
    ```

    The `rca` label may not exist in the repo. If `gh` returns `could not add label`, retry once without `--label "rca"`; do not create the label automatically.

15. **Report the issue URL.** Print the URL `gh` returned so the user can open it.

## Forbidden suggestion patterns

A "Suggested fix" section MUST NOT contain any of these. If your candidate matches, drop it.

- **Silencing a non-transient error.** No `try/except: pass`, no broad `except Exception:` that logs-and-continues, no `# type: ignore` on a real type bug, no demotion of a `CRITICAL`/`ERROR` log to `WARNING`/`DEBUG`. The transient-error categories (`RATE_LIMIT`, `STALE_SESSION`, `CLI_CRASH`, `STREAM_STALL`) already have categorized handling in `maestro.sdk` — do not propose extending that list to cover a new symptom.
- **Adding auto-recovery to the loop.** No retries on non-transient errors. No "maybe wrap `run_loop` in a top-level catch and continue". No suggestions to set `restart: always` / `unless-stopped` / `on-failure` on the `loop` service in `docker-compose.yml`. The loop is supposed to die hard and stay down so the next operator sees the same evidence we are looking at right now.
- **Symptomatic patches.** No "increase timeout", "raise the limit", "bump the cap", "add a retry" *unless* the evidence in step 9 proves the limit itself is the root cause and not a guard against a deeper defect.
- **Bypassing quality gates.** No `pytest.skip` / `xfail`, no `# pragma: no cover`, no lowering of `cov_fail_under`, no project-wide ruff / mypy disables.
- **Removing the offending code without replacement.** Deleting the line that raised is not a fix unless the code was provably dead.

Allowed and encouraged:
- Fix the upstream defect at `file:line`.
- Add an `assert` / type narrowing / explicit `raise` that converts a silent bad state into a louder, earlier failure.
- Add a regression test (per CLAUDE.md rule 11) that captures the bad state — name the test file and a one-line description; do not write it from this command.
- Tighten an existing invariant (e.g. shrink an unbounded list cap) when the evidence shows it was the source of unbounded growth.

## Issue body template

```markdown
## Summary

<one-line root-cause statement, ≤120 chars; if root cause unidentified, state that explicitly>

**Mode:** <incident | bug>
**Bug description (bug mode):** <verbatim user prompt, or "n/a — incident mode">

## Symptom

- **What failed:** <external failure: container exited, healthcheck failed, ERROR logged, etc.; in bug mode, the described behavior>
- **When:** <ISO timestamp of anchor, or "n/a — described, not observed in logs" in bug mode>
- **Iteration / phase:** <iteration N, phase X, or "n/a">
- **Detected via:** <log line / lifecycle.json field / journal milestone / docker ps, or "user report" in bug mode>

## Lifecycle snapshot

```
<verbatim quote of relevant fields from lifecycle.json; in bug mode write "n/a — no live data dir" if none was found>
```

## Timeline / code trace

<incident mode: chronological log excerpts, each with timestamp, level, logger, and source path; ±60s around the anchor. bug mode: the code trace from step 8b — the implicated `file:line` spans in flow order, with log excerpts only if they corroborate.>

```
-- $DATA_DIR/logs/maestro.log --
<excerpt>
```

```
-- $DATA_DIR/logs/sdk/<date>/<session>.log --
<excerpt, redacted>
```

## Proximate cause

<the immediate exception / invariant violation, with file:line>

## Root cause

<the upstream defect, OR explicit "Root cause unidentified — evidence supports proximate cause only" with the next-step pointer>

## Cross-checks

- **Journal milestone:** <yes/no — quote if yes>
- **Audit observation:** <yes/no — quote if yes>
- **Self-directed task in flight:** <yes/no — name task and linked_question if yes>
- **Recent suspect commits:** <SHA list with one-line summaries, or "none">
- **Bug description / hypothesis (from $ARGUMENTS):** <quote and verdict: confirmed / contradicted / inconclusive — in bug mode this is whether the code investigation reproduced the described defect; if contradicted, state where the bug actually is or that it could not be located>

## Posture check

- `docker-compose.yml` `loop.restart`: `<actual value>` (expected `"no"`)
- `consecutive_rate_limits` / `consecutive_cli_crashes` / `consecutive_stream_stalls` at anchor: `<n / n / n>`

## Suggested fix

<root-cause fix at file:line, OR omit this section entirely if step 10 conditions are not all met>

## Reproduction

<steps or input that triggered the failure, if known; else "Not yet reproducible — observed once in production">

## Evidence index

- `$DATA_DIR/lifecycle.json`
- `$DATA_DIR/logs/maestro.log` (lines ~<start>–<end>)
- `$DATA_DIR/logs/sdk/<date>/<session>.log`
- `$DATA_DIR/journal.jsonl` (last <N> entries inspected)
- `$DATA_DIR/audit_history.jsonl` (last <N> entries inspected)
- <bug mode: each source file inspected, with the decisive `file:line` ranges, and the relevant `tests/test_<module>.py`>
- Generated by `/rca` on <ISO timestamp>
```

Title format: `[rca] <phase or module>: <one-line root cause or proximate symptom>`. Examples:
- `[rca] evolution: PR conflict resolver dereferences None plan_id`
- `[rca] audit_self_model: KeyError on missing 'finding' — cause unidentified`
- `[rca] tend_pr: pre-merge reset_hard discards local commits when behind==0` (bug mode)

Keep titles under 80 characters. Do not include emoji. Do not include the iteration number in the title (it is in the body) — titles need to remain meaningful as state advances.

## Safety

- This command **reads** logs and **writes one GitHub issue**. It must not modify `data/`, `docker-compose.yml`, source files, or any other repo state.
- It must not push branches, open PRs, or close existing issues.
- It must not run `docker stop` / `docker restart` / `docker compose` against the loop or dashboard. The whole point of the failure posture is that the operator gets to inspect the dead container before anything touches it.
- Redact tokens, OAuth headers, and obvious secrets from any quoted log span before posting. Telegram bot tokens (`bot\d+:[A-Za-z0-9_-]{30,}`) appear in `httpx` request lines — strip them.
- If the evidence is insufficient to write a defensible RCA, post a `[rca] insufficient evidence` issue that describes what would be needed (which logs to capture, which dashboards to enable) rather than guessing at a cause.
- The whole RCA pipeline is read-only against the live loop and the repo — bug mode reads source files but **must not edit them** (diagnosis only; fixing is `/bug-fix`'s job).
- **Incident mode only:** if the loop is *currently* up and healthy (lifecycle.json mtime within last 60 s, no recent non-transient ERRORs), tell the user there is nothing to analyze and stop without posting. This check does **not** apply in bug mode — a described bug is worth root-causing regardless of loop health.

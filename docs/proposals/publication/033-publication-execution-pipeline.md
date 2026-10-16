# 033: Publication Execution Pipeline

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/agent.py`, `maestro/vcs.py`, `maestro/memory.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `CLAUDE.md`, new `maestro/publication.py`, tests |
| **Depends on** | 030, 031, 032 |

## Description

> Status updated to shipped on 2026-04-27 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Fourth and final of the 030 → 033 series. Adds the `execute_approved_publications` loop phase and the `Publisher` module that ships an approved proposal to a public GitHub repo under Thomas's personal namespace. This is the first proposal in the series with an externally-visible side effect (a live repo on `github.com/{owner}/{name}`), so its safety design is the critical part — every control is motivated by one of: *no orphan repos if we crash*, *no secret leakage ever*, *no surprise content beyond what the LLM reviewed*, *no token-scope surprise*.

The phase is gated behind `[publication] enabled = true` (032 ships the flag). It runs every iteration, is cheap when the queue is empty (one JSONL read), and processes at most one fresh proposal per `tick()` so the blast radius of any single bug is bounded to one repo per iteration. Publisher uses a TOCTOU-safe `approved → executing` status flip plus a durable `published_url` checkpoint so a crash between `gh repo create` and `git push` is recoverable without orphaning a repo. Token-scope and container credential-helper requirements are split off to **039** as deployment prerequisites. End state: an approved proposal becomes a live public repo, idempotently and secret-safe, with `copyright_holder` enforced as required-when-enabled at startup.

## Relevant Findings

- `maestro/vcs.py` — `SubprocessCodeHost.create_pr` is the template for a new `gh`-based mutation helper: subprocess, error-on-non-zero, return the parsed URL.
- `maestro/agent.py` — `_implement_approved_plans` is the template for a "pick one eligible item per iteration, sort approved-first" phase method.
- Resume-in-place already handles `STREAM_STALL`, `RATE_LIMIT`, `CLI_CRASH`, `STALE_SESSION` (CLAUDE.md). The publisher makes no LLM calls, so only generic process crashes need coverage — handled by the two-step checkpoint.
- `PublicationProposal.executing_started_at` and `.published_url` (from 030) are the two checkpoint fields that make resume-safe publishing possible.
- `secret_scan.scan_contents` (from 030) is the hard-block scanner for full file contents at publish time — runs once per file, not just on the LLM's bundle.
- `Memory.update_proposal_status(..., expect_status=...)` (030/032) re-verifies the current status under the JSONL `file_lock` before mutating, returning `False` on mismatch — the TOCTOU primitive used to flip `approved → executing` exactly once across concurrent ticks.
- 032's `[publication] enabled` flag gates this phase identically to how 031's `[artifacts] review_every` gates `review_artifacts`.

## Implementation Plan

All steps have landed (`[x]`). The dataclass field (1) and config (2) are substrate; the VCS helper (3) and `Publisher` module (4) are the core; phase wiring (5) and docs (6) finish.

1. [x] **Retry-limit setting.** Shipped as the `[publication] push_retry_limit` config key (`Config.publication_push_retry_limit: int = 3`) rather than a `PublicationProposal` dataclass field.
   - *Depends on:* none.
   - *Acceptance:* the resume path retries `git push` up to the configured limit.
2. [x] **Config keys + validation.** `maestro/config.py` + `maestro/toml_config.py` + `maestro.toml` — add `executing_stale_timeout_seconds=600`, `push_retry_limit=3`, `copyright_holder="Thomas Sodon"` (required non-empty). Validation raises `ConfigError` when `copyright_holder` is empty/whitespace; smoke mode does not bypass it. (`homepage` was not shipped.)
   - *Depends on:* none.
   - *Acceptance:* `publication_enabled=True` + `copyright_holder=""` raises `ConfigError`; setting either side independently passes.
3. [x] **Public-repo VCS helper.** `maestro/vcs.py` — add `SubprocessCodeHost.create_public_repo(name, description, *, owner="") -> str` (`gh repo create {owner}/{name} --public --description …`, no `--source`/`--push`, returns the HTTPS URL, propagates non-zero as `VCSError`) and the same signature on the `CodeHost` Protocol.
   - *Depends on:* none.
   - *Acceptance:* mocked `subprocess.run` returns the URL on success and raises `VCSError` on non-zero exit; the Protocol carries the method so test doubles type-check.
4. [x] **Publisher module.** new `maestro/publication.py` — `PublicationBlockedError` / `PublicationFailedError` and a `Publisher` with `tick()`, `_publish`, `_resume_publish`, `_stage`, `_git_init_commit` (see the pipeline below). `_stage` is pure file-IO callable with `host=None`/`runner=None` so 040's dry-run can reuse it; `_git_init_commit` uses `git -c user.email=… -c user.name=…` (identity not persisted) and does not use `SubprocessVCS`.
   - *Depends on:* 1, 3.
   - *Acceptance:* the full test matrix below passes — happy publish, both secret gates, the publishable-files bound, the binary guard, stale downgrade, resume + retry-exhaustion + staging-missing, the one-per-tick budget, and the `approved → executing` TOCTOU.
5. [x] **Phase wiring.** `maestro/agent.py` — insert `"execute_approved_publications"` into `_LOOP_PHASES` directly after `"implement_approved_plans"`; it is **NOT** in `_BUDGET_GATED_PHASES` (zero SDK cost, must keep running under burn-rate throttle). Guard on `not smoke and publication_enabled`, build a `Publisher`, and `await publisher.tick()`; `_phase_will_run()` is consulted once so the iteration header stays in lock-step.
   - *Depends on:* 4.
   - *Acceptance:* the phase runs every iteration when enabled, short-circuits under smoke or when disabled, and stays out of the budget-gated set (runs under throttle).
6. [x] **Docs.** `CLAUDE.md` — add `publication.py` to the modules table, `execute_approved_publications` to the phase list, a design bullet on the three safety seams (TOCTOU flip + `published_url` checkpoint; `publishable_files` whitelist; full-file secret scan at publish time), and a note that 039 covers `GITHUB_TOKEN` scope + container `gh auth setup-git` as deployment prerequisites.
   - *Depends on:* 5.
   - *Acceptance:* CLAUDE.md names the module, the phase, and the 039 prerequisite.

### Publisher internals (step 4 detail)

`tick()` runs an ordered pass per iteration:

```
A) Reconcile stale-executing: status=='executing' AND executing_started_at older
   than executing_stale_timeout_seconds AND published_url is None →
   status=failed, error='stale_executing_pre_create', notify, journal milestone.
B) Resume in-flight: status=='executing' AND published_url is not None →
   _resume_publish() (idempotent retry of the push tail).
C) Fresh publish: at most ONE per tick, FIFO by reviewed_at over status=='approved'
   → _publish().
Stale-reconcile runs before fresh publish so the per-tick budget cannot be starved.
```

`_publish` pipeline (10 steps, single durable checkpoint at step 7):

```
1. update_proposal_status(id, "executing", expect_status="approved",
                          executing_started_at=now)
   → False ⇒ another tick won the race; return without side effect.
2. task = memory.get_task(p.task_id); task.artifacts_dir missing → fail 'artifacts_missing'.
3. staging = memory_dir/publications/{proposal_id}/
4. redact_findings = _stage(p, artifacts_dir, staging, host, runner)
5. _git_init_commit(staging, task_id)
6. url = host.create_public_repo(p.proposed_name, p.proposed_description)
7. update_proposal_status(id, "executing", published_url=url,
                          scrub_findings=existing + redact_findings)
   ← CHECKPOINT: the public repo exists from this point onward.
8. runner(['git', 'remote', 'add', 'origin', url], cwd=staging)
9. runner(['git', 'push', '-u', 'origin', 'main'], cwd=staging)
10. update_proposal_status(id, "published", expect_status="executing")
    → False ⇒ a racing surface flipped the row out of executing; log a
      warning and return without notifying (the winning surface owns it).
    → True  ⇒ journal milestone; notifier.notify(... url).
```

`_stage` reads each `publishable_files` entry full (UTF-8; `UnicodeDecodeError` → `PublicationBlockedError('binary_in_publishable_files:<rel>')`), runs `scan_contents` (hard-block → `PublicationBlockedError('secret_scan_blocked:<preview>')`), writes scrubbed content into staging, then writes `LICENSE` (MIT, `{year} {copyright_holder}`), `README.md` (verbatim from the 032-scrubbed `proposed_readme`), and `.gitignore` only when a `.py` file is present; returns the redact findings.

Any exception in steps 1–6 → `status=failed`, `error=<detail>`, notify, milestone (staging kept for post-mortem). Steps 8–10 are the resumable tail. `_resume_publish` (entered when `status=="executing"` AND `published_url is not None`) verifies staging is present (missing → `status=failed, error='staging_missing_after_repo_create…'`, `published_url` kept), runs `git remote set-url origin` (idempotent), retries `git push` up to `push_retry_limit` (counter transient-only), and flips to `published` — under the same `expect_status="executing"` guard as the fresh path's terminal write, since the tail is shared — on success or `failed` (`repo_created_but_push_failed:<detail>`) on exhaustion.

## Testing Strategy

All tests use `mem_fs` and mock `subprocess.run` / the host; no real network or fs writes outside the temp staging.

- **Happy publish** (`tests/component/test_publication_phase.py`): approved + mocked `create_public_repo` + mocked push → `status=published`, `published_url` set, milestone, Telegram sent, staging holds `README.md` + `LICENSE` + only `publishable_files`.
- **Hard-block at publish** (`tests/test_publication.py`): staged `sk-ant-…` → `status=failed`, `error="secret_scan_blocked:…"`, no `create_public_repo`.
- **Redact-only**: `/home/operator/…` redacted in staging, `scrub_findings` populated, proceeds to `published`.
- **Publishable-files bound**: 20 files in artifacts, only the 10 in `publishable_files` reach staging + commit.
- **Binary file**: UTF-8 decode failure → `PublicationBlockedError("binary_in_publishable_files:…")`, `status=failed`, no `create_public_repo`.
- **Stale-executing downgrade**: `executing` + `now-15m` + `published_url=None` → `status=failed, error="stale_executing_pre_create"`, no `create_public_repo`.
- **Resume after checkpoint**: `executing` + `published_url` set + staging present → skip `create_public_repo`, `git remote set-url` + push, → `published`.
- **Resume — retry exhaustion**: `push_retry_limit=2`, both pushes fail → `status=failed, error="repo_created_but_push_failed:…"`, `published_url` preserved.
- **Resume — staging missing**: `published_url` set, staging deleted → `status=failed, error="staging_missing_after_repo_create"`, `published_url` preserved.
- **`git remote set-url` idempotency**: already-configured remote → succeeds, push proceeds.
- **One-per-tick budget**: 3 approved → exactly 1 transitions per `tick()`.
- **TOCTOU on flip**: `update_proposal_status` returns `False` on step 1 → returns without `create_public_repo`; status unchanged.
- **`copyright_holder` validation** (`tests/test_config.py`): `enabled=True` + empty holder → `ConfigError`.
- **Forward-compat `push_retry_limit`**: legacy row loads with `3`.
- **Smoke mode** (`tests/component/test_loop_smoke.py`): `--smoke --max-iters 1` short-circuits regardless of queue.
- **No duplicate repo across crash window**: two consecutive ticks across a simulated crash between steps 6 and 7 → `create_public_repo` invoked exactly once; second tick enters resume.

Run: `python3.12 -m pytest tests/test_publication.py tests/component/test_publication_phase.py tests/component/test_loop_smoke.py tests/test_config.py -v`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Crash between `create_public_repo` and `git push` orphans a public repo | High | Two-step pipeline with the durable `published_url` checkpoint at step 7; resume detects `executing` + `published_url` and skips re-creation; the stale downgrade fires only when `published_url` is still `None`. Tested across all three crash windows. |
| Concurrent ticks double-create a repo, or a racing surface stomped at finalize | High | Step 1 uses `update_proposal_status(..., expect_status="approved")` under `file_lock`; only one tick wins. The one-per-tick budget also caps blast radius. The terminal `executing → published` write (shared by the fresh and resume tails) is likewise guarded with `expect_status="executing"`, so a surface that legitimately flipped the row to `failed`/`rejected` between the Step 7 checkpoint and a successful push is not stomped back to `published` and fires no phantom success notification. |
| Secret leaks to a public repo | High | Layered: 031 pre-classification scan on the bundle, 032 README-scrub at emission, and a full-file hard-block scan at publish time in 033 — a hit flips to `failed` before `create_public_repo`. |
| Content surprises Thomas after approval | Medium | `publishable_files` is the 031 bundle — the publisher can only ship files the LLM saw. A head-truncated large file is an acknowledged blind spot for narrative leaks, mitigated by the 032 README preview gate. |
| `git push` needs a credential helper the container lacks | Medium | Deferred to **039**: `gh auth setup-git` at container entrypoint. |
| GitHub token scope insufficient for `gh repo create` | Medium | Deferred to **039**: `GITHUB_TOKEN` rotation to include `repo`. A real scope failure surfaces at `gh repo create` with clear stderr that `_publish` flips to `failed`. |
| `copyright_holder` unset at first publish | Low | `validate_when_enabled()` raises `ConfigError` at startup. Tested. |
| Duplicate proposed name slipped past 032 | Low | `gh repo create` fails loudly; `_publish` flips to `failed` with the gh error text. |
| Staging dir growth unbounded | Low | One dir per proposal; periodic cleanup is an explicit v1 non-goal. A future proposal can sweep `status ∈ {published, failed}` older than N days. |
| Resume-in-place during `tick()` retries the same proposal | Low | Retry re-enters the phase; `tick()` sees `executing` + `published_url=None` (stale-reconciled after the window) or `+ published_url` (resumed). Neither double-creates. |

## What landed

`maestro/publication.py:Publisher` + the `execute_approved_publications` loop phase + the TOCTOU-safe `approved → executing` flip + the `published_url` checkpoint + `_resume_publish()` + the `publishable_files` whitelist + the per-file publish-time secret-scan re-check (PR #1570). `[publication]` config extended with `executing_stale_timeout_seconds`, `push_retry_limit`, and `copyright_holder` (required non-empty, default `"Thomas Sodon"`).

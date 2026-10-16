# 030: Artifact Persistence Foundation and Secret Scanner

| Field | Value |
|-------|-------|
| **Impact** | LOW (no behaviour change on its own) |
| **Status** | shipped |
| **Affects** | `maestro/types.py`, `maestro/memory.py`, `maestro/migration.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro/vcs.py`, `maestro.toml`, new `maestro/secret_scan.py`, tests |

## Description

> Status updated to shipped on 2026-04-27 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

First of four proposals (**030 → 031 → 032 → 033**) that together add an artifact-review → open-source publication pipeline. This proposal is pure infrastructure: it lands the persistence layer, configuration, the shared secret-scanning utility, and the two read-only GitHub CLI helpers that the later phases depend on. Nothing in the loop changes — the new JSONL files are never written and no new phase is dispatched.

The artifact-review pipeline classifies accumulated `artifacts/{task_id}/` contents as **publish**, **improve**, **reuse**, or **skip**, and for `publish` verdicts produces a human-approved publication of the artifacts as a public GitHub repo under Thomas's personal namespace (MIT, `Copyright (c) <year> Thomas Sodon`). This proposal builds the substrate. End state: the dataclasses, helpers, config, scanner, and VCS read helpers all exist and are unit-tested, but inert — the subsequent proposals merge one-at-a-time behind feature flags.

## Relevant Findings

- `maestro/memory.py` — the `EvolutionPlan` dataclass and its append-JSONL-with-last-wins-load pattern. New publication dataclasses follow it.
- `maestro/memory.py` — `update_plan_status` is the canonical mutation-helper pattern: `_UNSET` sentinel kwargs, full-file rewrite under `file_lock`, returns `bool` for TOCTOU detection. New helpers mirror it exactly.
- `maestro/memory.py` (`_load_all_tasks`) — iterates `tasks.jsonl` without bounding; `TaskRecord.artifacts_dir` is already persisted per task (optional, `None` for legacy rows). No existing consumer ever reads it back.
- `maestro/migration.py` — `ensure_schema()` computes a SHA-256 fingerprint over `fields()` of every class in `_TRACKED_CLASSES` and re-writes state files on mismatch. Adding a dataclass triggers a migration even if no existing JSONL is modified.
- `maestro/vcs.py` — `SubprocessCodeHost` has no `gh api user` or `gh api repos/{owner}/{name}` helper today. The `_run()` subprocess pattern in the sibling `SubprocessVCS` is the template for new methods.
- Secret-scanning logic exists nowhere in the codebase. Evolution `_self_review()` reads diffs but does not scan for leaked credentials.

## Implementation Plan

All steps have landed (`[x]`). They were ordered so the persistence and scanner substrate (1–4, 7) could ship before the config and VCS read helpers (5–6) that later phases consume; every step was independently mergeable and inert until 031.

1. [x] **Literal types.** `maestro/types.py` — add `Verdict = Literal["publish", "improve", "reuse", "skip"]` and `ProposalStatus = Literal["pending_approval", "approved", "rejected", "executing", "published", "failed"]`. These name the verdict and proposal-status axes the dataclasses key on.
   - *Depends on:* none.
   - *Acceptance:* both literals import from `maestro.types` and the dataclasses below annotate against them.
2. [x] **Dataclasses.** `maestro/memory.py` — add `ArtifactReview(review_id, task_id, verdict, title, reasoning, bundle_file_names, proposed_name, proposed_description, timestamp, proposed_readme)` and `PublicationProposal(proposal_id, task_id, review_id, proposed_name, description, proposed_readme, publishable_files, status, created_at, reviewed_at, rejection_reason, scrub_findings, executing_started_at, published_url, error)` alongside `EvolutionPlan`. `publishable_files` is sourced from `ArtifactReview.bundle_file_names` and is the **only** set of files the publisher may ship.
   - *Depends on:* 1.
   - *Acceptance:* both dataclasses round-trip through JSONL write+load, last-wins on duplicate id, and skip unknown legacy fields.
3. [x] **Memory helpers.** `maestro/memory.py` — add `write_artifact_review`, `get_artifact_reviews(limit=None)`, `get_reviewed_task_ids() -> set[str]`, `write_publication_proposal`, `get_publication_proposals(limit, status_filter=None)`, `get_proposal(proposal_id)`, and `update_proposal_status(...) -> bool` (the `update_plan_status` skeleton verbatim with the dataclass type swapped). JSONL paths `{memory_dir}/artifact_reviews.jsonl` and `{memory_dir}/publication_proposals.jsonl`.
   - *Depends on:* 2.
   - *Acceptance:* `update_proposal_status` returns `False` on an unknown id and acquires `file_lock` for writes; the getters tail the correct JSONL.
4. [x] **Migration registration.** `maestro/migration.py` — extend `_TRACKED_CLASSES` with `ArtifactReview, PublicationProposal`; extend `_STATE_FILES` for the two new JSONL files. No data transformation — the files start empty.
   - *Depends on:* 2.
   - *Acceptance:* `compute_schema_fingerprint` changes when the two dataclasses are added, and `ensure_schema()` does not crash when the two JSONL files are absent.
5. [x] **Config sections.** `maestro.toml` + `maestro/config.py` + `maestro/toml_config.py` — add `[artifacts]` (`review_enabled=false`, `review_every=20`, `review_batch_size=3`, `review_max_file_bytes=4096`, `review_max_files_per_task=10`, `review_max_bundle_bytes=12288`) and `[publication]` (`enabled=false`, `owner=""`, `copyright_holder="Thomas Sodon"`, `license="MIT"`, `secret_scan_enabled=true`, `executing_stale_timeout_seconds=600`, `push_retry_limit=3`), validated like `[loop]`.
   - *Depends on:* none.
   - *Acceptance:* new keys load with defaults when absent, explicit values override, and validation rejects negative caps and non-positive `review_every`.
6. [x] **VCS read helpers.** `maestro/vcs.py` — add `SubprocessCodeHost.resolve_owner() -> str` (`gh api user --jq .login`, cached) and `repo_exists(owner, name) -> bool` (`gh api repos/{owner}/{name}`; `True` on 200, `False` on 404, `False` + warning otherwise — fail-open at this gate, real collisions surface at `gh repo create` in 033).
   - *Depends on:* none.
   - *Acceptance:* mocked `subprocess.run` proves `resolve_owner` parses the login and `repo_exists` maps 200/404/other to `True`/`False`/`False`+warning.
7. [x] **Secret scanner.** new `maestro/secret_scan.py` — pure functions over a `ScanResult(hard_block_findings, redact_findings, scrubbed_content)`: `scan_contents`, `scrub_readme` (alias), `scrub_file(path)`, `has_hard_block(text)`. Hard-block patterns cover Anthropic / OpenAI / GitHub classic + fine-grained `github_pat_*` / AWS / private keys / keyword-proximate base64; redact-inline covers workspace paths, the operator emails + host, and the narrative tokens `maestro` / `self-(evolving|…)` / `autonomous (ai )?agent`. Compiled regex tuples are module-level and importable by tests.
   - *Depends on:* none.
   - *Acceptance:* one positive + one false-positive guard per hard-block family pass, all redaction kinds scrub, and `scrub_file` swallows `UnicodeDecodeError` while propagating `FileNotFoundError`/`PermissionError`.

## Testing Strategy

All tests use `mem_fs` and mock externals per CLAUDE.md.

- **Secret scanner** (`tests/test_secret_scan.py`, 34 tests): one positive + one false-positive guard per hard-block family (benign base64, `sk-test` alone, short `ghp_`); every redaction kind; dedupe by `(kind, 12-char preview)` and `kind: count` aggregation; the three `scrub_file` contracts; 100% line coverage on the new module.
- **Memory helpers** (extend `tests/test_memory.py`): round-trip write+load per dataclass; last-wins on duplicate `review_id`/`proposal_id`; legacy-row load skips unknown fields; `update_proposal_status` TOCTOU returns `False` on unknown id; `file_lock` asserted via the existing mock.
- **Migration** (extend `tests/test_migration.py`): fingerprint changes when the two dataclasses are added; `ensure_schema()` survives absent JSONL.
- **Config** (extend `tests/test_toml_config.py`): defaults, overrides, and rejection of negative caps / non-positive `review_every`.
- **VCS helpers** (extend `tests/test_vcs.py`): mocked `subprocess.run` for `resolve_owner` (quoted/unquoted) and `repo_exists` (exit 0 / 404 / other-exit-warning).

Run: `python3.12 -m pytest tests/test_secret_scan.py tests/test_memory.py tests/test_migration.py tests/test_toml_config.py tests/test_vcs.py -v`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Regex false positives silently mangle future READMEs | Medium | Scanner ships in 030 but no consumer calls it until 031/032. Pattern set is unit-tested in isolation with a false-positive guard per family before any prod use. |
| Fingerprint bump forces a migration for all running instances | Low | `ensure_schema()` is designed to re-write on mismatch; the only effect is a one-time rewrite of existing files into canonical shape. |
| New JSONL files grow unbounded | Low | Out of scope here; growth is bounded by `review_batch_size × 1/review_every` once 031 lands. Proposal 002 (tiered memory) covers these files eventually. |
| New config keys drift between `maestro.toml` and `Config` | Low | `test_toml_config.py` asserts every TOML key maps to a field and vice versa. |

## What landed

`maestro/secret_scan.py` (PR #1440) — `ScanResult` dataclass, `scan_contents`, `scrub_readme`, `scrub_file`, `has_hard_block`, with module-level `HARD_BLOCK_PATTERNS` / `REDACT_PATTERNS` tuples importable for tests. Hard-block patterns cover Anthropic / OpenAI / GitHub classic / AWS / private keys / keyword-proximate base64 plus fine-grained `github_pat_*` PATs; the OpenAI pattern adds `\b` anchors and a `(?!ant-)` negative lookahead to avoid claiming Anthropic prefixes. Inline redactions cover `/home/operator/`, `/workspace/`, `/tmp/` paths, the operator emails + host, and the narrative tokens. Hard-block findings dedupe by `(kind, 12-char preview)`; redact findings aggregate as `"kind: count"`. `scrub_file` swallows `UnicodeDecodeError` but propagates `FileNotFoundError` / `PermissionError`. 34 tests, 100% line coverage. The module had no call sites in 030.

Persistence layer (PR #1451): `Verdict` / `ProposalStatus` literals (`maestro/types.py`); `ArtifactReview` / `PublicationProposal` dataclasses + `DuplicateProposalError` + the seven memory helpers (`maestro/memory.py`); migration `_TRACKED_CLASSES` / `_STATE_FILES` extension (`maestro/migration.py`). The fingerprint bump triggers a one-time `ensure_schema()` rewrite on first restart — the same pattern used for `version_archive` / `rejection_history`.

Config + VCS (PR #1463): `[artifacts]` / `[publication]` TOML sections wired through `Config` and `toml_config.py`; `SubprocessCodeHost.resolve_owner()` and `repo_exists()` in `maestro/vcs.py`.

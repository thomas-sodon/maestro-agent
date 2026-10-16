# 019: OWASP Agentic Risk Self-Audit

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Derived from** | [Microsoft Agent Governance Toolkit](https://opensource.microsoft.com/blog/2026/04/02/introducing-the-agent-governance-toolkit-open-source-runtime-security-for-ai-agents/), via [`docs/proposals/research/related-projects-survey.md`](../research/related-projects-survey.md) |
| **Affects** | new `docs/owasp-agentic-audit.md`, `maestro/evolution.py`, `CLAUDE.md`, tests |

## Description

Maestro already has many mitigations for agentic AI risks:

- HITL approval on every structural self-modification (`EvolutionEngine`
  flow).
- `check_secrets()` at startup validating all secrets are present and
  well-formed (`maestro/config.py`).
- Path-traversal check in `read_task_artifact` MCP tool
  (`maestro/tools.py`).
- Caps on LLM output shape via JSON schema `maxItems`/`maxLength` in
  `Consciousness.update_self_model` (`maestro/consciousness.py`).
- 7-day cooldown on resubmission of rejected plans with the same
  normalized title (`maestro/evolution.py`).
- Crash-triage durable queue with retry/attempt limits (`maestro/crash.py`).
- Rate-limit classification and containment in `sdk.query()`
  (`maestro/sdk.py`).
- …and many more, scattered across the codebase and documented only in
  `CLAUDE.md` prose.

The problem isn't missing mitigations — it's that no single document
answers "does Maestro handle OWASP agentic AI risk #N?" for all N. The
Microsoft Agent Governance Toolkit (April 2026) addresses all 10 OWASP
agentic AI risks with runtime policy enforcement. We are not proposing
to adopt the toolkit itself — runtime policy enforcement is orthogonal
to Maestro's HITL approval model, and there's no integration that pays
for itself. We are proposing to adopt **the taxonomy**: a one-time
self-audit cycle that produces a living document mapping each OWASP
agentic AI risk to Maestro's current mitigation(s), with file/line
anchors, residual risk, and a freshness check so it can't silently drift
out of date.

This is the lowest-cost proposal of the three. Most of the work is
reading code Maestro already has and writing down what's there. Where
mitigations are missing or weak, the audit produces a prioritised
backlog that feeds future evolution cycles through the normal flow.

## Relevant Findings

### Mitigations exist but aren't indexed

A direct grep across `maestro/` for security-relevant patterns
(`check_secrets`, `path_traversal`, `read_task_artifact`) hits
`tools.py`, `config.py`, `cli.py` — three files, different layers, no
cross-reference. Cooldown, rate limiting, crash containment, and PR
gating are each in their own modules with their own docs. The
consolidated story only lives in `CLAUDE.md` prose and only for someone
who reads it end-to-end.

### `_self_review()` already reads the diff against the plan

`EvolutionEngine._self_review()` (`maestro/evolution.py`) already
runs after every `implement()` commit, reviews the diff against the
plan, and can fix issues in-place. That's the exact right hook for an
audit-freshness check: if a modified file is referenced by the audit
document and the audit has not been updated in the same commit range,
emit a warning.

### No new runtime code is needed

The audit document itself is markdown. The only code change is a small
helper in `_self_review()` that checks the diff against a parseable
section of the audit doc. Everything else — the risk enumeration, the
mitigation prose, the file anchors — is content, not code.

## Implementation Plan

### Step 1: Write `docs/owasp-agentic-audit.md`

A new living document. Structure:

```markdown
# Maestro — OWASP Agentic AI Risk Self-Audit

| Field | Value |
|-------|-------|
| **Last updated** | YYYY-MM-DD |
| **Audit owner** | whoever last updated this doc |
| **Freshness check** | enforced in `EvolutionEngine._self_review()` |

## Methodology

... (short: "one row per OWASP agentic AI risk; each row has the
current mitigation with file/line anchors and a residual-risk
assessment") ...

## Risks

### AAI01: ... (risk name from OWASP list)

**Description.** One-paragraph restatement of the risk in Maestro's
context.

**Current mitigation.**
- `maestro/evolution.py` — `EvolutionEngine.reflect()` caps inputs
  at 2000 chars to prevent context-window blow-up.
- `maestro/consciousness.py` — JSON schema enforces `maxItems` /
  `maxLength` on `update_self_model` output.

**Residual risk.** Medium / Low / None. One sentence.

**Tracked files.**
- `maestro/evolution.py`
- `maestro/consciousness.py`

### AAI02: ...

...
```

The "Tracked files" field at the bottom of each risk is machine-
parseable (the freshness check in Step 2 reads it). We do **not** use
YAML frontmatter — staying all-markdown keeps the doc diffable by
reviewers.

Maestro's implementing agent writes the **first draft** of the audit by
reading the codebase against the 10 OWASP agentic AI risks. The concrete
list of risks is whatever the current OWASP agentic AI top-10 says at
the time of implementation — the proposal intentionally doesn't pin a
specific version because the list evolves; the implementing agent fetches
it fresh.

### Step 2: Add a freshness check to `_self_review()`

In `maestro/evolution.py`, inside `_self_review()`, after the
diff is computed and before the LLM review call:

```python
stale_files = self._audit_freshness_check(diff_text)
if stale_files:
    # Inject into the review prompt as an extra check item.
    audit_warning = (
        "\n## OWASP audit freshness\n\n"
        "The following modified files are referenced in "
        "docs/owasp-agentic-audit.md but the audit was not updated in "
        "this PR. Review whether the audit needs to be updated:\n"
        + "\n".join(f"- {f}" for f in stale_files)
    )
    review_prompt += audit_warning
```

`_audit_freshness_check(diff_text: str) -> list[str]`:

1. Parse `docs/owasp-agentic-audit.md`. Extract all "Tracked files" list
   items into a set.
2. Parse the unified diff — extract the set of modified files
   (`+++ b/path`) and also whether `docs/owasp-agentic-audit.md` itself
   is in the diff.
3. If the audit *is* in the diff → return `[]` (someone already updated
   it).
4. Otherwise → return the intersection of "modified files" ∩ "tracked
   files".
5. On any parse error → return `[]` with a warning log (fail-open:
   freshness is a nudge, not a gate).

The check never *blocks* the review — it only asks the LLM reviewer to
look at the audit alongside the diff. The reviewer has full tool access
and can update the audit in the same commit if appropriate.

### Step 3: Update CLAUDE.md

Add a new bullet under "Key Design Decisions":

> **OWASP agentic risk self-audit**: `docs/owasp-agentic-audit.md` maps
> each OWASP agentic AI risk to Maestro's current mitigation with
> file/line anchors and residual-risk assessment.
> `EvolutionEngine._self_review()` runs an `_audit_freshness_check` over
> the diff: if a modified file is referenced by the audit but the audit
> itself wasn't updated in the PR, the reviewer prompt includes a
> warning listing the stale files. The check is fail-open — audit parse
> errors never block review.

### Step 4: Seed the "Security posture" section in README

Short paragraph in README.md linking to `docs/owasp-agentic-audit.md` so
external readers (and GitHub rendering) can find it.

### Step 5: Tests

- `test_audit_freshness_check_parses_tracked_files` — build a fake
  audit doc with 3 known tracked files, assert the parser returns the
  expected set.
- `test_audit_freshness_check_no_modified_files` — empty diff → returns
  `[]`.
- `test_audit_freshness_check_audit_itself_in_diff` — diff touches
  `docs/owasp-agentic-audit.md` → returns `[]`.
- `test_audit_freshness_check_stale_file` — diff modifies a tracked
  file, audit not in diff → returns that file.
- `test_audit_freshness_check_handles_parse_error` — malformed audit
  doc → returns `[]` and logs a warning.
- `test_self_review_injects_audit_warning_into_prompt` — integration:
  mock `_audit_freshness_check` to return a non-empty list, verify the
  review prompt contains the warning block.

All using `mem_fs`; no real file I/O.

Run: `python3.12 -m pytest tests/ -v -k "audit_freshness or self_review"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Audit document becomes stale — written once, never updated | Medium | The `_self_review()` freshness check is the structural answer: every relevant PR gets a nudge |
| False positives: freshness check flags a file whose mitigation didn't actually change | Low | Check is a warning to the reviewer, not a gate; reviewer judges |
| False negatives: a relevant change touches a file not listed in any "Tracked files" block | Medium | The audit is a living doc — each time the warning fires and the reviewer updates the audit, the tracked-files list grows |
| Audit misrepresents actual mitigation coverage | Medium | The audit carries file/line anchors that can be grep-verified by hand; the first-draft implementing agent is expected to run the anchors through ripgrep before writing each row |
| Audit doc contains sensitive security details that shouldn't be public | Low | Public repo, but every file/line anchor just points at existing public source; no new secrets are added |

## What this proposal does NOT do

- It does **not** adopt the Microsoft Agent Governance Toolkit or any
  other runtime policy enforcement system. Runtime policy enforcement
  is orthogonal to Maestro's HITL model.
- It does **not** introduce new runtime mitigations for gaps the audit
  finds. Gaps → normal evolution plans → normal approval flow. This
  proposal is visibility + freshness, nothing more.
- It does **not** automate the audit first-draft. The implementing
  agent writes it once, and `_self_review()`'s freshness check keeps it
  honest afterwards.
- It does **not** change any runtime code path other than
  `_self_review()` prompt assembly. No new process, no new blocking
  gate, no new config.

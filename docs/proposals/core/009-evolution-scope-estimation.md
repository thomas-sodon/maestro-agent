# 009: Evolution Plan Scope Estimation

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `maestro/evolution.py`, `maestro/memory.py`, `maestro/types.py`, `maestro/migration.py` |

## Description

Evolution plans have no complexity or scope estimate. A "feature" plan could be a one-line change or a multi-file refactor, and the human reviewer has no signal for expected effort. Large plans tend to get rejected, but the system doesn't learn to prefer smaller plans because there's no scope data to correlate with outcomes. This proposal adds complexity estimation to plans, surfaces it in PRs and notifications, and feeds it back into the reflection process.

## Relevant Findings

### EvolutionPlan has no size signal (`memory.py`)
```python
@dataclass
class EvolutionPlan:
    plan_id: str
    title: str
    stage: str
    category: Category
    rationale: str
    proposed_changes: list[str]
    files_affected: list[str]
    expected_stage_impact: str
    priority: Priority = "feature"
    issue_number: int | None = None
    status: PlanStatus = "pending_review"
    created_at: str = ""
    reviewed_at: str | None = None
    rejection_reason: str | None = None
    pr_url: str | None = None
    rejection_learned: bool = False
```
`proposed_changes` and `files_affected` give some indication but no explicit complexity rating.

### propose_plan() JSON schema doesn't request complexity (`evolution.py`)
The `output_format` schema requires `title`, `category`, `priority`, `rationale`, `proposed_changes`, `files_affected`, `expected_stage_impact`, and optional `issue_number`. No complexity field.

### create_pr() body has no scope indicator (`evolution.py`)
```python
body = (
    f"## Evolution Plan: {plan.title}\n\n"
    f"**Stage:** {plan.stage}\n"
    f"**Category:** {plan.category}\n"
    f"**Priority:** {plan.priority}\n\n"
    f"### Rationale\n{plan.rationale}\n\n"
    f"### Expected Impact\n{plan.expected_stage_impact}\n\n"
    f"### Changes\n" +
    "\n".join(f"- {c}" for c in plan.proposed_changes) +
    closes_line
)
```
No complexity badge or estimated size.

### Telegram notifications show title only
`submit_for_review()` sends the plan title to Telegram. No scope information for quick reviewer assessment.

### reflect() doesn't consider plan size patterns
The reflection prompt includes past rejections but doesn't analyze whether rejected plans were consistently large or complex.

## Implementation Plan

### Step 1: Add Complexity type

In `maestro/types.py`, add:
```python
Complexity = Literal["trivial", "small", "medium", "large"]
VALID_COMPLEXITIES: frozenset[str] = frozenset(get_args(Complexity))
```

### Step 2: Add fields to EvolutionPlan

In `maestro/memory.py`, add to `EvolutionPlan`:
```python
estimated_complexity: str = "small"  # Complexity literal
estimated_lines: int = 0
```

### Step 3: Update propose_plan() JSON schema

In `maestro/evolution.py`, add to the JSON schema in `propose_plan()`:
```python
"estimated_complexity": {
    "type": "string",
    "enum": ["trivial", "small", "medium", "large"],
    "description": "Expected implementation complexity"
},
"estimated_lines": {
    "type": "integer",
    "description": "Estimated total lines of code changed (added + modified + deleted)"
}
```

Add to the prompt guidance:
```
Prefer small, focused changes. Most evolutions should be "trivial" or "small".
"medium" is acceptable for multi-file refactors. "large" should be rare and well-justified.
```

### Step 4: Validate complexity in propose_plan()

After parsing the plan JSON:
```python
complexity = raw.get("estimated_complexity", "small")
if complexity not in VALID_COMPLEXITIES:
    complexity = "small"
plan.estimated_complexity = complexity
plan.estimated_lines = max(0, int(raw.get("estimated_lines", 0)))
```

### Step 5: Include complexity in PR body

In `create_pr()`, add to the body:
```python
body += f"\n**Complexity:** {plan.estimated_complexity}"
body += f" (~{plan.estimated_lines} lines)" if plan.estimated_lines else ""
```

### Step 6: Include complexity in Telegram notification

In `submit_for_review()`, add complexity to the notification:
```python
message = f"Evolution proposed: {plan.title}\n"
message += f"Priority: {plan.priority} | Complexity: {plan.estimated_complexity}\n"
message += f"Category: {plan.category}"
```

### Step 7: Feed complexity into reflection

In `reflect()`, when building the past plans section, include complexity:
```python
plan_lines.append(
    f"- [{p.status}] {p.title} (complexity: {p.estimated_complexity}, "
    f"priority: {p.priority})"
)
```

Add guidance if large plans have been rejected:
```python
large_rejected = [p for p in rejected_plans if p.estimated_complexity in ("medium", "large")]
if large_rejected:
    sections.append("Note: Large/medium complexity plans have been rejected recently. Prefer smaller, focused changes.")
```

### Step 8: Update migration

In `maestro/migration.py`, handle new `EvolutionPlan` fields with defaults (`estimated_complexity="small"`, `estimated_lines=0`).

## Testing Strategy

1. **Complexity field in plan** — Verify `propose_plan()` output includes `estimated_complexity`
2. **Invalid complexity defaults** — LLM returns "huge", verify normalized to "small"
3. **PR body includes complexity** — Verify complexity badge in PR body text
4. **Telegram includes complexity** — Verify notification message includes complexity
5. **Reflection includes complexity** — Verify past plans in reflect prompt show complexity
6. **Large rejection guidance** — Verify extra guidance when large plans were rejected
7. **Migration** — Old plans load with `estimated_complexity="small"`, `estimated_lines=0`

Run: `python3.12 -m pytest tests/ -v -k "complexity or scope"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| LLM estimates inaccurate | Low | Used as signal, not enforcement; directionally useful |
| Complexity steering limits ambition | Low | Only steers toward smaller plans when large ones are getting rejected |
| Schema change needs migration | Low | Additive fields with defaults |
| Telegram message length increase | Low | Adds ~30 chars; well within 4096 limit |

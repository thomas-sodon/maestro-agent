# 005: Structured Evolution Feedback Loop

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/evolution.py`, `maestro/memory.py`, `maestro/types.py`, `maestro/consciousness.py`, `maestro/migration.py` |

## Description

> Status updated to shipped on 2026-04-27 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

When an evolution PR is rejected, the system writes a journal entry and updates the self-model with free-text learning. The `reflect()` prompt includes past rejections as raw text. But there's no structured categorization, no pattern detection across rejections, and no way to determine whether the same type of mistake is being repeated. This proposal adds structured rejection records, categorical classification, and pattern analysis to transform rejection from a single learning event into systematic pattern recognition.

## Relevant Findings

### learn_from_rejection() is unstructured (`maestro/evolution.py`)
```python
async def learn_from_rejection(
    self, plan: EvolutionPlan, reason: str,
) -> None:
    """Record rejection in journal and update self-model."""
    model = self._consciousness.load_self_model()
    self._journal.write_thought(
        content=(
            f"Evolution plan rejected: {plan.title}\n"
            f"Reason: {reason}\n"
            f"I need to learn from this. What was too aggressive, "
            f"misguided, or premature about this proposal?"
        ),
        stage=model.stage,
        entry_type="realization",
        triggered_by=f"rejection:{plan.plan_id}",
    )

    # Update self-model with this limitation
    reflection = (
        f"My evolution proposal '{plan.title}' was rejected: {reason}. "
        f"This tells me something about my limitations or misjudgments."
    )
    await self._consciousness.update_self_model(reflection)
```
Both outputs are free-form text. The `triggered_by="rejection:{plan_id}"` tag is the only structured metadata captured, and it's consumed by nothing downstream.

### reflect() includes rejections as text (`maestro/evolution.py`)
The reflection prompt builds a "Past rejections" section inline before calling `query()`:
```python
rejected_plans = [
    p for p in past_plans if p.status == "rejected" and p.rejection_reason
]
rejection_text = ""
for p in rejected_plans[-5:]:
    rejection_text += f"- \"{p.title}\" was rejected: {p.rejection_reason}\n"
...
prompt = f"""...
Past rejections (learn from these — do NOT repeat similar proposals):
{rejection_text or "(None)"}
...
Avoid repeating approaches that were previously rejected."""
```
Only the last 5 rejections are included, as raw strings. There is no categorical summary and no count of how often a given rejection category has recurred — the LLM is left to extract patterns from the prose itself every time it reflects.

### rejection_reason is a capped string (`maestro/memory.py`)
```python
rejection_reason: str | None = None
```
Set by `update_plan_status()` (`maestro/memory.py`) with the reason capped at `rejection_reason_max` (2000 chars). No categorical metadata.

### _process_rejected_plans() delegates without analysis (`maestro/agent.py`)
Iterates rejected plans whose `rejection_learned` flag is false, calls `learn_from_rejection()` + `mark_rejection_learned()`, and clears Telegram approval buttons via `notifier.resolve_plan(..., "Rejected")`. Every error path is swallowed with a warning log. No pattern analysis happens here or anywhere else in the loop.

### No rejection history separate from evolution plans
Rejection data is embedded in `EvolutionPlan.rejection_reason` within `evolution_plans.jsonl`. To analyze patterns, you'd need to scan all plans and filter.

## Implementation Plan

### Step 1: Add RejectionCategory type

In `maestro/types.py`, add:
```python
RejectionCategory = Literal[
    "scope_too_large",
    "wrong_approach",
    "not_needed",
    "quality_issue",
    "duplicate",
    "other",
]
VALID_REJECTION_CATEGORIES: frozenset[str] = frozenset(get_args(RejectionCategory))
```

### Step 2: Add RejectionRecord dataclass

In `maestro/memory.py`, add:
```python
@dataclass
class RejectionRecord:
    plan_id: str
    title: str
    category: Category        # evolution category (capability/awareness/purpose/infrastructure)
    priority: Priority        # evolution priority (bug_fix/performance/security/feature)
    rejection_reason: str
    rejection_category: str   # from RejectionCategory
    timestamp: str
```

### Step 3: Add rejection history persistence

In `maestro/memory.py`, add to `Memory`:
```python
def __init__(self, memory_dir: Path) -> None:
    ...
    self._rejections_path = self._dir / "rejection_history.jsonl"

def record_rejection(self, record: RejectionRecord) -> None:
    """Append a structured rejection record."""
    with open(self._rejections_path, "a") as f:
        f.write(json.dumps(asdict(record)) + "\n")

def get_recent_rejections(self, n: int = 50) -> list[RejectionRecord]:
    """Return the last N rejection records."""
    ...
```

### Step 4: Add rejection classification to learn_from_rejection()

In `maestro/evolution.py`, modify `learn_from_rejection()`:
1. After writing the journal entry, classify the rejection reason:
```python
classification = await query(
    prompt=f"Classify this rejection reason into one category:\n"
           f"Reason: {reason}\n\n"
           f"Categories: scope_too_large, wrong_approach, not_needed, quality_issue, duplicate, other\n\n"
           f"Reply with just the category name.",
    system="You classify evolution plan rejection reasons.",
    options=QueryOptions(max_turns=1, effort="low"),
)
rejection_category = classification.response.strip().lower()
if rejection_category not in VALID_REJECTION_CATEGORIES:
    rejection_category = "other"
```

2. Create and persist a `RejectionRecord`:
```python
record = RejectionRecord(
    plan_id=plan.plan_id,
    title=plan.title,
    category=plan.category,
    priority=plan.priority,
    rejection_reason=reason,
    rejection_category=rejection_category,
    timestamp=datetime.now(UTC).isoformat(),
)
self._memory.record_rejection(record)
```

### Step 5: Add pattern analysis method

In `maestro/evolution.py`, add:
```python
def analyze_rejection_patterns(self) -> str:
    """Analyze rejection history for recurring patterns."""
    records = self._memory.get_recent_rejections(50)
    if not records:
        return "No rejection history."

    # Count by rejection category
    by_category: dict[str, int] = {}
    for r in records:
        by_category[r.rejection_category] = by_category.get(r.rejection_category, 0) + 1

    # Count by evolution category
    by_evo_category: dict[str, int] = {}
    for r in records:
        by_evo_category[r.category] = by_evo_category.get(r.category, 0) + 1

    total = len(records)
    lines = [f"Total rejections: {total}"]
    lines.append("By rejection reason:")
    for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {count} ({count*100//total}%)")
    lines.append("By evolution category:")
    for cat, count in sorted(by_evo_category.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {count} ({count*100//total}%)")

    return "\n".join(lines)
```

### Step 6: Include patterns in reflect()

In `maestro/evolution.py`, the prompt is built inline as an f-string — there are no "sections" to append to. Insert the pattern analysis next to the existing "Past rejections" block:
```python
pattern_analysis = self.analyze_rejection_patterns()
pattern_section = (
    f"\n\nRejection pattern analysis:\n{pattern_analysis}"
    if pattern_analysis and pattern_analysis != "No rejection history."
    else ""
)

prompt = f"""...
Past rejections (learn from these — do NOT repeat similar proposals):
{rejection_text or "(None)"}{pattern_section}
...
"""
```
Keep the existing `rejection_text` loop intact — the pattern analysis is an addition, not a replacement.

### Step 7: Update migration

In `maestro/migration.py`, add `RejectionRecord` to tracked dataclasses. Add `_migrate_rejection_history()` handler (no migration needed initially — new file).

## Testing Strategy

1. **RejectionRecord persistence** — Write records, read back, verify fields
2. **Rejection classification** — Mock `query()` to return known categories, verify classification
3. **Invalid category fallback** — Mock `query()` returning garbage, verify falls back to "other"
4. **Pattern analysis** — Write 10 records with mixed categories, verify analysis output
5. **Pattern analysis empty** — Verify "No rejection history" for empty file
6. **Integration with reflect()** — Verify reflect prompt includes pattern analysis section
7. **Integration with learn_from_rejection()** — Verify rejection record created during learning

Run: `python3.12 -m pytest tests/ -v -k "rejection"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Classification LLM call adds cost | Low | Uses effort="low", max_turns=1; one call per rejection (rare event) |
| Classification may be inaccurate | Low | Categories are broad; "other" catches ambiguous cases |
| Pattern analysis in prompt adds tokens | Low | Summary is short (~200 chars); far less than raw rejection text |
| New JSONL file to maintain | Low | Simple append-only, same pattern as other JSONL files |
| Rejection records duplicate plan data | Low | Separate file enables fast pattern queries without scanning all plans |

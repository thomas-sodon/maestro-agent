# 008: Dream Quality Validation

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/dreaming.py`, `maestro/config.py`, `maestro/toml_config.py` |

## Description

> Status updated to shipped on 2026-06-19 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

The dreaming system compresses accumulated journal entries, task history, and purpose hypotheses into a 500-char-per-field digest via a single LLM call. The output is trusted without validation — a bad dream can silently erase important context with no detection or recovery. All four digest fields share the same 500-char cap regardless of content importance. There are no compression quality metrics. This proposal adds a validation pass, configurable per-field limits, and quality tracking.

## Relevant Findings

### dream() trusts LLM output blindly (`dreaming.py`)
The LLM response is parsed through `output_format` with a JSON schema, then each field is inlined into the `DreamDigest` constructor with a single `[:500]` slice:
```python
digest = DreamDigest(
    timestamp=datetime.now(UTC).isoformat(),
    journal_digest=str(data.get("journal_digest", ""))[:500],
    task_digest=str(data.get("task_digest", ""))[:500],
    purpose_digest=str(data.get("purpose_digest", ""))[:500],
    identity_evolution=str(data.get("identity_evolution", ""))[:500],
    iteration_count=iteration,
    entries_digested=entry_count,
    tasks_digested=task_count,
)
```
No quality check, no comparison to source data. The digest could be empty, repetitive, or miss critical information, and it is immediately persisted via `self.save_digest(digest)`.

### Uniform 500-char clamp is rigid (`dreaming.py`)
All four output fields get the same 500-character slice. Journal entries (Maestro's primary reflective medium) may warrant more space than task summaries; purpose and identity digests are typically shorter in practice.

### No compression metrics tracked (`dreaming.py`)
```python
@dataclass
class DreamDigest:
    timestamp: str = ""
    journal_digest: str = ""
    task_digest: str = ""
    purpose_digest: str = ""
    identity_evolution: str = ""
    iteration_count: int = 0
    entries_digested: int = 0
    tasks_digested: int = 0
```
`entries_digested` / `tasks_digested` count the *source* entries, not compression. There is no `chars_in`, `chars_out`, or quality indicator.

### get_context_for_prompt() propagates bad digests
If `load_digest()` returns a digest, `get_context_for_prompt()` stamps it into the `"## Compressed memories (dream #N):"` header used by every downstream prompt builder. A low-quality digest silently poisons every LLM call until the next dream cycle overwrites it — and the next cycle may well make it worse.

### Source data is pre-truncated before dreaming (`dreaming.py`)
`journal_text` is sliced to the last 5000 characters (`journal_text[-5000:]`), per-task `key_findings` are truncated to 150 chars and per-goal text to 100 chars, and the cumulative task block is capped at 5000 chars (`task_text = task_text[:5000]`). Evidence lists are sliced to 5 per hypothesis (`h.evidence_for[:5]`, `h.evidence_against[:5]`). The LLM never sees the raw corpus — validation therefore needs to compare the digest against this already-truncated `source_summary`, not the full history.

## Implementation Plan

### Step 1: Add configurable per-field limits

In `maestro/config.py`, add:
```python
dream_journal_max: int = 600
dream_task_max: int = 400
dream_purpose_max: int = 400
dream_identity_max: int = 400
dream_max_retries: int = 1
```

In `maestro/toml_config.py`, add mappings in `_TOML_TO_CONFIG`:
```python
("loop", "dream_journal_max"): ("dream_journal_max", int),
("loop", "dream_task_max"): ("dream_task_max", int),
("loop", "dream_purpose_max"): ("dream_purpose_max", int),
("loop", "dream_identity_max"): ("dream_identity_max", int),
("loop", "dream_max_retries"): ("dream_max_retries", int),
```

### Step 2: Add quality metrics to DreamDigest

In `maestro/dreaming.py`, add fields to `DreamDigest`:
```python
chars_in: int = 0        # total chars of source data sent to LLM
chars_out: int = 0       # total chars of digest output
quality_score: float = 1.0  # 0.0-1.0, from validation pass
```

### Step 3: Apply per-field limits in dream()

Replace the four hard-coded `[:500]` slices in the `DreamDigest` constructor (`dreaming.py`) with config-driven limits. Because the fields are inlined directly into the constructor, the cleanest change is to extract the slicing first:
```python
limits = {
    "journal_digest": self._journal_max,
    "task_digest": self._task_max,
    "purpose_digest": self._purpose_max,
    "identity_evolution": self._identity_max,
}
clipped = {
    key: str(data.get(key, ""))[: limits[key]] for key in limits
}
digest = DreamDigest(
    timestamp=datetime.now(UTC).isoformat(),
    **clipped,
    iteration_count=iteration,
    entries_digested=entry_count,
    tasks_digested=task_count,
)
```

Note: `Dreamer.__init__()` (`dreaming.py`) currently takes `memory_dir`, `journal`, `memory`, `consciousness`, `purpose`, plus keyword-only `cost_samples_path` and `report_output_path` — no config. Add the four limits and `dream_max_retries` as constructor arguments and wire them from `cli._build_agent()` where the `Dreamer` is instantiated.

### Step 4: Add validation pass

In `maestro/dreaming.py`, add:
```python
async def _validate_digest(
    self, digest: DreamDigest, source_summary: str,
) -> tuple[float, list[str]]:
    """Validate dream quality. Returns (score, lost_items)."""
    prompt = f"""Compare this compressed digest against its source data.

Source data summary (truncated):
{source_summary[:2000]}

Compressed digest:
- Journal: {digest.journal_digest}
- Tasks: {digest.task_digest}
- Purpose: {digest.purpose_digest}
- Identity: {digest.identity_evolution}

Rate the compression quality from 0.0 to 1.0 and list any critical information
that was lost. Reply with JSON:
{{"quality_score": 0.8, "lost_items": ["item1", "item2"]}}
"""
    result = await query(
        prompt=prompt,
        system="You evaluate the quality of memory compression.",
        options=QueryOptions(max_turns=1, effort="low"),
    )
    parsed = result.parse_json()
    score = float(parsed.get("quality_score", 0.5))
    lost = parsed.get("lost_items", [])
    return (max(0.0, min(1.0, score)), lost)
```

### Step 5: Integrate validation into dream()

After generating the initial digest:
```python
source_summary = f"Journal ({entry_count} entries):\n{journal_text[:500]}\n\nTasks ({task_count}):\n{task_text[:500]}"
score, lost_items = await self._validate_digest(digest, source_summary)
digest.quality_score = score

if score < 0.6 and lost_items and retries < self._config.dream_max_retries:
    # Re-dream with preservation instructions
    preserve_prompt = f"\n\nCRITICAL: You must preserve these items that were lost in the previous attempt:\n"
    preserve_prompt += "\n".join(f"- {item}" for item in lost_items[:5])
    # Re-run dream with augmented prompt
    ...
    retries += 1
```

### Step 6: Track compression metrics

After dream completion:
```python
digest.chars_in = len(journal_text) + len(task_text) + len(purpose_text)
digest.chars_out = sum(len(getattr(digest, f)) for f in ("journal_digest", "task_digest", "purpose_digest", "identity_evolution"))
logger.info(
    "Dream quality: %.2f, compression: %d -> %d chars (%.1fx)",
    digest.quality_score, digest.chars_in, digest.chars_out,
    digest.chars_in / max(1, digest.chars_out),
)
```

## Testing Strategy

1. **Per-field limits applied** — Configure different limits per field, verify each respected
2. **Validation catches bad digest** — Mock LLM returning low quality_score and lost_items, verify re-dream triggered
3. **Re-dream includes preservation instructions** — Verify prompt includes lost items
4. **Max retries respected** — Verify no more than `dream_max_retries` re-dream attempts
5. **Quality metrics tracked** — Verify `chars_in`, `chars_out`, `quality_score` in saved digest
6. **Good digest not re-dreamed** — Score >= 0.6 and no lost items, verify no retry
7. **Config integration** — Verify all dream_* config options load from TOML

Run: `python3.12 -m pytest tests/ -v -k "dream"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Validation LLM call adds cost | Low | effort="low", max_turns=1; one call per dream cycle (rare) |
| Re-dream doubles dream cost | Low | `dream_max_retries=1` caps total; rare event (only on bad quality) |
| Validation itself could be wrong | Low | Used as heuristic, not gate; bad validation just means one extra dream call |
| New DreamDigest fields need migration | Low | Additive fields with defaults; existing digests load fine |
| Config complexity increases | Low | Sensible defaults; most users won't need to tune |

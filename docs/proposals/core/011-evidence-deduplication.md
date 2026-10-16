# 011: Evidence Deduplication and Weighting

| Field | Value |
|-------|-------|
| **Impact** | LOWER |
| **Status** | shipped |
| **Affects** | `maestro/purpose.py`, `maestro/tools.py`, `maestro/migration.py` |
| **What landed** | All eight steps — the `EvidenceItem` dataclass, the `PurposeHypothesis` field switch to `list[EvidenceItem]`, `_is_duplicate` / `_accumulate_evidence` / `_prune_evidence`, dual-format coercion in `_load_hypotheses` (`_coerce_evidence`), startup migration in `_migrate_hypotheses`, and `add_purpose_evidence` routed through `PurposeEngine.add_evidence` |
| **What is missing** | Nothing — all eight steps landed |

## Description

> Status updated to shipped on 2026-06-16 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Purpose hypotheses accumulate evidence as `list[str]` with no deduplication, no weighting, and no metadata. The same observation can appear multiple times. All evidence is treated equally regardless of recency or significance. When evidence lists hit the 10-item cap, the oldest items are dropped first — even if they're the most important. This proposal adds structured evidence items with deduplication, weighting, and smart pruning.

## Relevant Findings

### Evidence is raw list[str] (`maestro/purpose.py`)
```python
@dataclass
class PurposeHypothesis:
    ...
    evidence_for: list[str] = field(default_factory=list)
    evidence_against: list[str] = field(default_factory=list)
    confidence: float = 0.5
```
No metadata per evidence item.

### evaluate_hypothesis() appends without dedup (`maestro/purpose.py`)
```python
hypothesis.evidence_for.extend(
    item for item in for_items if isinstance(item, str)
)
hypothesis.evidence_against.extend(
    item for item in against_items if isinstance(item, str)
)
hypothesis.evidence_for = hypothesis.evidence_for[-self.MAX_EVIDENCE_ITEMS:]
hypothesis.evidence_against = hypothesis.evidence_against[-self.MAX_EVIDENCE_ITEMS:]
```
No duplicate check. Items appended then truncated FIFO.

### add_purpose_evidence tool also appends (`tools.py`)
The MCP tool `add_purpose_evidence` delegates to `PurposeEngine` methods that append without dedup.

### MAX_EVIDENCE_ITEMS = 10 with FIFO truncation (`maestro/purpose.py`)
```python
MAX_EVIDENCE_ITEMS: int = 10
```
Applied in `maestro/purpose.py` (`evaluate_hypothesis`) after evaluation accumulates evidence and in `maestro/purpose.py` (`refine_or_abandon`) when a refined hypothesis is created. Oldest items dropped regardless of importance.

## Implementation Plan

Steps are ordered so an interrupted run leaves the tree working: the
`EvidenceItem` type and its field switch land first (1–2), then the
dedup / accumulation / pruning logic (3–5), then the read, migration, and tool
paths that depend on the new schema (6–8). Switching the evidence fields is a
breaking schema change; loading (step 6) and startup migration (step 7) coerce
the legacy `list[str]` form so no stored hypothesis is lost.

1. [x] **Define the `EvidenceItem` dataclass.** `maestro/purpose.py` — add a
   dataclass so each piece of evidence carries metadata (weight, first-observed
   timestamp, source) instead of being a bare string:
   ```python
   @dataclass
   class EvidenceItem:
       text: str
       weight: float = 1.0    # importance weight, increases on re-observation
       timestamp: str = ""     # ISO format, when first observed
       source: str = ""        # e.g., "task_evaluation", "manual", "reflection"
   ```
   - *Depends on:* none.
   - *Acceptance:* `EvidenceItem` exists in `maestro/purpose.py` with the four
     fields and their defaults, and round-trips through `dataclasses.asdict()`
     and reconstruction.
2. [x] **Switch the evidence fields to `list[EvidenceItem]`.** `maestro/purpose.py`
   — change `PurposeHypothesis.evidence_for` / `evidence_against` from
   `list[str]` to `list[EvidenceItem]`:
   ```python
   evidence_for: list[EvidenceItem] = field(default_factory=list)
   evidence_against: list[EvidenceItem] = field(default_factory=list)
   ```
   This is a breaking schema change; existing `list[str]` data is coerced on load
   (step 6) and migrated on startup (step 7).
   - *Depends on:* 1.
   - *Acceptance:* both fields are typed `list[EvidenceItem]` and `mypy` passes
     across `maestro/`.
3. [x] **Add the deduplication helper.** `maestro/purpose.py` — add
   `_is_duplicate`, returning the matching existing item (or `None`) via a
   normalized exact-or-substring comparison that adds no dependency:
   ```python
   def _is_duplicate(self, new_text: str, existing: list[EvidenceItem], threshold: float = 0.85) -> EvidenceItem | None:
       """Check if new evidence is a near-duplicate of existing evidence.
       Returns the matching existing item, or None."""
       normalized_new = new_text.lower().strip()
       for item in existing:
           normalized_existing = item.text.lower().strip()
           # Exact match
           if normalized_new == normalized_existing:
               return item
           # Substring containment (one contains the other)
           if normalized_new in normalized_existing or normalized_existing in normalized_new:
               return item
       return None
   ```
   A simple normalized comparison is preferred over fuzzy matching to avoid
   adding dependencies (e.g. `difflib`); `difflib.SequenceMatcher` (stdlib) could
   be adopted later if needed.
   - *Depends on:* 1.
   - *Acceptance:* `_is_duplicate` returns the matching item for an exact match
     and for substring containment in either direction, and `None` for genuinely
     distinct text.
4. [x] **Dedup-aware evidence accumulation.** `maestro/purpose.py` — in
   `evaluate_hypothesis()`, replace the direct append with a dedup pass that
   reinforces an existing item's weight (capped at 5.0) or appends a new
   `EvidenceItem`:
   ```python
   for text in new_for:
       if not isinstance(text, str) or not text.strip():
           continue
       existing = self._is_duplicate(text, hypothesis.evidence_for)
       if existing:
           # Reinforce existing evidence
           existing.weight = min(5.0, existing.weight + 0.5)
       else:
           hypothesis.evidence_for.append(EvidenceItem(
               text=text.strip(),
               weight=1.0,
               timestamp=datetime.now(UTC).isoformat(),
               source="evaluation",
           ))
   ```
   - *Depends on:* 2, 3.
   - *Acceptance:* re-observing existing evidence increments its weight (bounded
     at 5.0) rather than appending a duplicate; novel evidence is appended as a
     new `EvidenceItem` with `weight=1.0` and a populated `timestamp`.
5. [x] **Weight-based pruning.** `maestro/purpose.py` — replace the FIFO
   `[-MAX_EVIDENCE_ITEMS:]` truncation in `evaluate_hypothesis()` and
   `refine_or_abandon()` with `_prune_evidence`, keeping the highest
   `weight * recency` items (30-day recency half-life):
   ```python
   def _prune_evidence(self, items: list[EvidenceItem], max_items: int) -> list[EvidenceItem]:
       """Keep the highest-scoring items."""
       if len(items) <= max_items:
           return items
       # Score = weight * recency_factor
       now = datetime.now(UTC)
       def score(item: EvidenceItem) -> float:
           try:
               age_days = (now - datetime.fromisoformat(item.timestamp)).days
           except (ValueError, TypeError):
               age_days = 365
           recency = 1.0 / (1.0 + age_days / 30.0)  # half-life of 30 days
           return item.weight * recency
       sorted_items = sorted(items, key=score, reverse=True)
       return sorted_items[:max_items]
   ```
   - *Depends on:* 2.
   - *Acceptance:* with more than `MAX_EVIDENCE_ITEMS` items, `_prune_evidence`
     keeps the `MAX_EVIDENCE_ITEMS` highest-scored by `weight * recency`; a
     high-weight old item outranks a low-weight new one; no FIFO slice remains.
6. [x] **Coerce both formats on load.** `maestro/purpose.py` — in
   `_load_hypotheses()`, accept both the legacy `list[str]` and the new
   dict / `EvidenceItem` forms, coercing each into `EvidenceItem`:
   ```python
   # Evidence field coercion
   for field_name in ("evidence_for", "evidence_against"):
       val = filtered.get(field_name, [])
       if isinstance(val, list):
           coerced = []
           for item in val:
               if isinstance(item, str):
                   # Legacy format: convert to EvidenceItem
                   coerced.append(EvidenceItem(text=item))
               elif isinstance(item, dict):
                   coerced.append(EvidenceItem(**{k: v for k, v in item.items() if k in evidence_fields}))
           filtered[field_name] = coerced
   ```
   - *Depends on:* 2.
   - *Acceptance:* loading a hypothesis whose evidence is legacy `list[str]`
     yields `list[EvidenceItem]` (text preserved, default weight/timestamp), the
     new dict form preserves every field, and serialization round-trips.
7. [x] **Migrate stored hypotheses on startup.** `maestro/migration.py` — update
   `_migrate_hypotheses()` to convert any `list[str]` evidence to
   `list[EvidenceItem]` with default `weight=1.0` and empty `timestamp`, so the
   on-disk schema matches the new dataclass after `ensure_schema()`.
   - *Depends on:* 2.
   - *Acceptance:* an existing hypotheses file with `list[str]` evidence is
     rewritten to the `EvidenceItem` dict form (default weight 1.0, empty
     timestamp) and its schema fingerprint updates.
8. [x] **Route the MCP tool through the new path.** `maestro/tools.py` — in
   `create_self_awareness_server()`, update the `add_purpose_evidence` tool to
   construct `EvidenceItem` objects (with `source` set), so manually added
   evidence flows through the same dedup / weighting path as evaluation-sourced
   evidence.
   - *Depends on:* 1, 4.
   - *Acceptance:* `add_purpose_evidence` produces `EvidenceItem` instances and
     re-adding the same text reinforces weight instead of creating a duplicate.

## Testing Strategy

1. **Deduplication: exact match** — Add same text twice, verify weight increases instead of duplicate
2. **Deduplication: substring** — Add "X" then "X is important", verify treated as duplicate
3. **Deduplication: no match** — Add genuinely different evidence, verify both kept
4. **Weight-based pruning** — Add 15 items, verify 10 highest-scored kept
5. **Recency factor** — Old high-weight item vs new low-weight, verify scoring correct
6. **Legacy format loading** — Load `list[str]` evidence, verify converted to `list[EvidenceItem]`
7. **Migration** — Existing hypotheses file migrated with default weights
8. **Serialization round-trip** — Save and load EvidenceItem, verify all fields preserved

Run: `python3.12 -m pytest tests/ -v -k "evidence or dedup"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Schema change breaks existing data | Medium | Migration handles old format; loading coerces both formats |
| Deduplication too aggressive | Low | Only exact/substring match; threshold-based fuzzy match available if needed |
| Weight accumulation unbounded | Low | Capped at 5.0 max weight |
| Recency factor penalizes old but important evidence | Low | Weight compensates; high-weight old evidence still ranks above low-weight new |
| Serialization complexity | Low | Dataclass with simple fields; asdict/from_dict pattern |

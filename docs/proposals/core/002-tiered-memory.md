# 002: Tiered Memory with Archival

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **Affects** | `maestro/memory.py`, `maestro/dreaming.py`, `maestro/consciousness.py`, `maestro/agent.py`, `maestro/config.py`, `maestro/migration.py` |
| **What landed** | Archive rotation via `Memory.archive(threshold_days)` splits `task_history.jsonl` / `evolution_plans.jsonl` into hot + cold siblings, gated by the `[loop] archive_threshold_days` config (0 = disabled). The in-memory plan index (`_plan_index` / `_build_plan_index` / `_invalidate_plan_index`) backs `get_plan()` and is invalidated on every write. Dream digests append to `dream_history.jsonl` with `load_dream_history(n: int \| None = None)` for replay; legacy `dream_digest.json` migrates in-place. Consciousness archival appends dropped list items to `self_model_archive.jsonl` via `_append_archive_entries`, retrievable via `load_archived_items(field, n)`. `run_loop` invokes `self.memory.archive(self.config.archive_threshold_days)` on dream cycles. Shape drift vs the original plan: Step 3 ships as `dream_history.jsonl` / `load_dream_history(n: int \| None = None)` rather than `dream_digests.jsonl` / `load_digest_history(n: int = 10)`. |

## Description

All persistent state files (task history, evolution plans, journal) grow unbounded as JSONL. Every read operation scans the entire file. The consciousness self-model silently drops the oldest items when lists exceed 10 entries, permanently losing historical knowledge. Dream digests overwrite a single file, erasing the history of how Maestro's understanding evolved. This proposal introduces tiered storage (hot/archive), indexed lookups, dream digest history, and consciousness list archival.

## Relevant Findings

### Linear scans on every read (`maestro/memory.py`)
```python
def get_recent_tasks(self, n: int = 30) -> list[TaskRecord]:
    if n <= 0:
        return []
    if not self._tasks_path.exists():
        return []
    lines = self._tasks_path.read_text(encoding="utf-8").strip().splitlines()
    entries = []
    for line in lines[-n:]:
        ...
```
Reads the entire file into memory, then takes the last N lines. Same pattern for `get_all_plans()` at `maestro/memory.py`.

### `get_all_plans()` used in hot paths
Called by `_implement_approved_plans()` (`maestro/agent.py`), `_process_rejected_plans()` (`maestro/agent.py`), and the `has_pending_rejection` precheck inside `run_loop`'s `process_rejected_plans` phase (`maestro/agent.py`). Each call scans the full plans file.

### Silent list truncation (`maestro/consciousness.py`, `save_self_model`)
```python
MAX_LIST_ITEMS: ClassVar[int] = 10
MAX_ITEM_CHARS: ClassVar[int] = 280
MAX_PURPOSE_CHARS: ClassVar[int] = 1200
```
`save_self_model()` truncates all tracked list fields to `MAX_LIST_ITEMS` and each item to `MAX_ITEM_CHARS`. Dropped items and truncated strings are lost forever — no archival. The caps are enforced symmetrically on load so oversized pre-existing data is normalised on first read.

### Single dream digest (`dreaming.py`)
`save_digest()` overwrites `dream_digest.json`. Previous digest content is gone. There's no way to see how Maestro's compressed understanding evolved over time.

### Journal also grows unbounded (`journal.py`)
`_load_all()` reads the entire `journal.jsonl` on every call. Used by `get_full_narrative()` and `dream()`.

## Implementation Plan

### Step 1: Add archive rotation to Memory

In `maestro/memory.py`, add:
```python
def archive(self, threshold_days: int = 30) -> None:
    """Move JSONL entries older than threshold to archive files."""
```

For each JSONL file (`task_history.jsonl`, `evolution_plans.jsonl`):
1. Read all lines, parse timestamps
2. Split into hot (recent) and cold (older than threshold)
3. Write hot entries back to original file via `atomic_write()`
4. Append cold entries to `{filename}.archive.jsonl`
5. Use `file_lock()` during the operation

Add `archive_threshold_days: int = 30` to `Config` in `[loop]` TOML section.

### Step 2: Add in-memory plan index

In `maestro/memory.py`, add to `Memory`:
```python
_plan_index: dict[str, int] | None = None  # plan_id -> line number

def _build_plan_index(self) -> dict[str, int]:
    """Build index mapping plan_id to line number for fast lookup."""
    ...

def _invalidate_plan_index(self) -> None:
    self._plan_index = None
```

Call `_invalidate_plan_index()` after any write to `evolution_plans.jsonl`. Use the index in `get_plan()` to seek directly to the right line instead of scanning all entries.

### Step 3: Change dream digest to append-only history

In `maestro/dreaming.py`:
1. Change storage from `dream_digest.json` to `dream_history.jsonl` <!-- shipped under this filename, not `dream_digests.jsonl` -->
2. `save_digest()` appends a new line instead of overwriting
3. `load_digest()` reads the last line (latest digest)
4. Add `load_dream_history(n: int | None = None) -> list[DreamDigest]` for accessing older digests <!-- shipped under this name and signature, not `load_digest_history(n: int = 10)` -->
5. `get_context_for_prompt()` continues using latest digest only (no behavior change for consumers)

### Step 4: Add consciousness list archival

In `maestro/consciousness.py`, modify `save_self_model()`:
1. Before truncating list fields to `MAX_LIST_ITEMS`, identify items that will be dropped
2. If any items are dropped, append them to `self_model_archive.jsonl` with:
   ```json
   {"timestamp": "...", "field": "values", "dropped_items": ["item1", "item2"]}
   ```
3. Add `load_archived_items(field: str, n: int = 50) -> list[str]` method for retrieval

### Step 5: Integrate archival into run_loop

In `agent.py:run_loop()`, call `memory.archive()` on dream cycles (when `dream_every > 0` and iteration triggers dreaming). This keeps archival frequency tied to the dreaming schedule.

### Step 6: Migration

In `maestro/migration.py`:
- Handle `dream_digest.json` → `dream_digests.jsonl` conversion: if old file exists, read it, write as single JSONL entry, delete old file
- No migration needed for archive files (they're new)
- No migration needed for plan index (ephemeral, in-memory)

### Step 7: Update CLAUDE.md

Update descriptions for Memory, Dreaming, and Consciousness modules to reflect archival, digest history, and indexing.

## Testing Strategy

1. **Archive rotation** — Write 50 task records with varying timestamps, call `archive(threshold_days=7)`, verify hot file has only recent records, archive has older ones
2. **Plan index** — Write 20 plans, call `get_plan()` for specific IDs, verify correct plan returned; write new plan, verify index invalidated and rebuilt
3. **Dream digest history** — Save 5 digests, verify `load_digest()` returns latest, `load_digest_history(3)` returns last 3
4. **Consciousness archival** — Set MAX_LIST_ITEMS=3 for test, save model with 5 items, verify archive contains dropped 2 items
5. **Migration** — Create `dream_digest.json`, run migration, verify `dream_digests.jsonl` exists with correct content
6. **File locking** — Verify archive operation uses `file_lock()`

Run: `python3.12 -m pytest tests/ -v -k "archive or digest_history or plan_index"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Migration breaks existing dream digest | Medium | Migration converts old format; fallback to fresh state on error |
| Archive files also grow unbounded | Low | Archives are cold storage, rarely read; can be manually pruned |
| Plan index memory usage | Low | Index is just `{str: int}` dict; negligible even for thousands of plans |
| Race condition during archive rotation | Medium | Uses `file_lock()` for atomicity |
| Consciousness archival adds I/O per save | Low | Only writes when items are actually dropped (infrequent after stabilization) |

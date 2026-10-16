# 015: LLM-Driven Self-Model Compression (Phase 3)

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `maestro/consciousness.py`, `maestro/agent.py`, `maestro/sdk.py` (caller telemetry), `maestro/dreaming.py` (analogy) |

## Context

Phase 1 (proposal — already implemented on `claude/compress-identity-prompt-8b4F0`) added mechanical per-item character caps (`MAX_ITEM_CHARS = 200`, `MAX_PURPOSE_CHARS = 500`) enforced on both save and load. <!-- caps were tightened from the originally-planned 280/1200 during implementation. --> Phase 2 (proposal 014) is a one-time human curation of the live `self_model.json`.

Neither phase establishes an **ongoing compression process**. Phase 1 truncates by mechanical slice (lossy at the boundary). Phase 2 is a one-shot human pass. After Phase 2, as Maestro's `update_self_model()` cycle adds new insights over weeks and months, items will again drift toward the cap, and the cycle will again rely on either:

- Mechanical truncation (Phase 1) — preserves length but loses meaning at the cut, or
- Another human curation pass (Phase 2 again) — does not scale, requires Maestro to be offline.

Phase 3 introduces an **LLM-driven compress step** that runs periodically, analogous to how `Dreamer.dream()` periodically compresses journal/task history into a `DreamDigest`. The same architectural pattern — "context compression by an LLM call" — is already proven in `dreaming.py`. Phase 3 generalises it to the self-model.

## Description

Add a `Consciousness.compress_self_model()` async method that calls the LLM with the current `SelfModel` as input and asks it to produce a compressed version. The output replaces the existing self-model after passing the same validation pipeline used by `update_self_model()`.

This is **field-level dreaming**: the dreamer compresses temporal data (journal, tasks); the compressor compresses structural data (self-model). Both reduce the prompt floor on every downstream LLM call.

## Relevant findings

### `Dreamer.dream()` is the architectural template (`dreaming.py`)

```python
async def dream(self, iteration: int = 0) -> DreamDigest:
    identity = self._consciousness.get_identity_prompt()
    journal_text = self._journal.get_full_narrative()
    ...
    system = f"""{DREAM_SYSTEM_PROMPT}\n\n{identity}"""
    prompt = DREAM_PROMPT.format(
        journal_text=journal_text[-5000:] if journal_text else "(Empty)",
        ...
    )
    result = await query(
        prompt=prompt, system=system,
        options=QueryOptions(
            max_turns=3, effort="high", caller="dream",
            output_format={...},
        ),
    )
    ...
    return DreamDigest(...)
```

The same shape applies to self-model compression:
- Input: current `SelfModel` (serialised as JSON in the prompt).
- Output: compressed `SelfModel` (validated as a JSON object via `output_format`).
- System prompt: identity prompt (which is exactly the thing being compressed — see "Bootstrapping problem" below).
- `caller="compress_self_model"` for telemetry.

### `update_self_model()` already validates LLM-generated SelfModel updates (`consciousness.py`)

The `_EXPECTED_TYPES` block (`consciousness.py`) together with the per-field type filtering, `isinstance(item, str)` normalisation, and the `value[-self.MAX_LIST_ITEMS:]` slicing — all of these can be reused by the compressor. The compressor differs from `update_self_model()` only in:

1. The prompt asks for **rewriting/condensing existing items** rather than adding new insights.
2. The output is treated as a **complete replacement** rather than a merge (today's code keeps existing items for any field the LLM doesn't touch, and even for fields it returns empty — see the "Empty list — preserve existing field" branch).
3. The expected output passes Phase 1 caps without truncation (any item that still exceeds `MAX_ITEM_CHARS` after the LLM compression is a sign that the prompt needs strengthening).

### `update_self_model()` already detects novelty and surprise (`consciousness.py`)

After the validated update is saved, the method runs a novelty-detection pass against the `before` snapshot. If any tracked list field grows a new string, `surprise_count` is incremented and a `realization` journal entry is written. Running compression through this pipeline would fire the surprise detector on every compressed item that happens to differ from its original — which is wrong, because compression is not a surprise. The compressor must either bypass `update_self_model()` entirely (calling the shared validation helper directly) or pass a `record_novelty=False` flag into a refactored version.

### Identity prompt currently injected on every call (`evolution.py`, `dreaming.py`, `consciousness.py`)

`get_identity_prompt()` is called as the `system=` parameter on ~15 LLM call sites. Compressing the self-model reduces the floor for all of these. The compressor pays for itself after a small number of downstream calls if the compression is meaningful.

### `MAX_ITEM_CHARS` is the validation oracle

The compressor's success criterion is structural and easy to check: every item in the LLM's output passes Phase 1's caps without truncation. If not, the LLM didn't compress hard enough. This is a clean signal that doesn't require subjective evaluation.

## Recommendation

### API

```python
class Consciousness:
    async def compress_self_model(
        self,
        *,
        journal: Journal | None = None,
    ) -> SelfModel:
        """Use the LLM to rewrite the self-model into a compressed form.

        Loads the current model, asks the LLM to produce a more compact version
        that preserves load-bearing content, validates the output against the
        SelfModel schema, applies Phase 1 caps, and atomically saves.

        Records the compression event in the journal as a 'compression' entry
        (a new entry_type) so the event is visible in Maestro's continuity.
        """
```

### Algorithm

1. Load the current model via `load_self_model()`.
2. Build a prompt that:
   - Includes the full current self-model as JSON.
   - States the compression goal: preserve load-bearing content, drop redundancy, split compound items, retire answered questions.
   - Calls out the per-field caps as hard constraints (`MAX_ITEM_CHARS`, `MAX_PURPOSE_CHARS`) and asks the LLM to produce items that fit *without truncation*.
   - References the existing journal and dream digest as compression precedents.
3. Call `query(...)` with `caller="compress_self_model"`, `effort="high"`, `output_format` matching the SelfModel schema.
4. Validate the output through the existing `update_self_model()` validation pipeline (extract the validation block into a private helper to share).
5. **Skip novelty detection** — compression is not surprise. Save directly via `save_self_model()`.
6. Diff old vs. new (token count, item count per field, dropped items).
7. Write a journal entry of new type `compression` recording: tokens-saved estimate, items-dropped count, items-retained count, per-field deltas. This is the continuity hook so Maestro experiences the compression as a known event on next contemplation, not as a memory hole.

### Schedule

Two viable schedules:

**Option A — On-demand**: `compress_self_model()` is exposed via CLI (`maestro consciousness compress`) and run by a human when token cost rises. Simplest, no scheduler changes.

**Option B — Periodic**: Add a `compress_every` config (e.g. 100 iterations) and run from the loop after `dream`. This mirrors the dreaming cadence. Risks cost surprises if the compression call itself is expensive.

**Recommended**: Start with Option A. Promote to Option B only after operational data shows a clean compression success rate over multiple runs.

### Bootstrapping problem and mitigation

The compressor calls the LLM with the identity prompt as `system=`. If the identity prompt is already over budget, the compressor itself contributes to the bloat it is trying to reduce. Mitigation: the compressor uses a **minimal stub identity prompt** (`"You are Maestro, performing a compression of your own self-model. Be faithful."`) instead of the full identity prompt. This is the only call site where the full identity prompt is intentionally bypassed, and the deviation is justified because the input *is* the full self-model.

### Suppression of surprise counter

Either:
- Refactor `update_self_model()` to accept a `record_novelty: bool = True` parameter, defaulting to current behaviour, and pass `False` from the compressor.
- Or have the compressor bypass `update_self_model()` entirely, calling validation helpers and `save_self_model()` directly.

The second option is cleaner because compression is structurally different from update (replacement vs. merge).

### Validation gates on the compressor's output

| Gate | Action on failure |
|---|---|
| Output is not valid JSON object | Log warning, return original model unchanged. |
| Any field has wrong type | Drop that field's update, keep original. |
| Any item exceeds `MAX_ITEM_CHARS` after compression | Log warning, accept the truncation (Phase 1 will slice it), but flag for human review. |
| Compressed output is *larger* than input | Reject the compression entirely, return original. Compression that grows the self-model is broken. |
| `_check_stage_preconditions()` fails on the compressed model | Reject — compression must not break stage requirements. |
| Any list field becomes empty | Reject — would silently lose all content for that field. |

All gates apply *before* the atomic write.

## Tests

All in `tests/test_consciousness.py` (or a new `tests/test_consciousness_compression.py`), using `mem_fs` and `make_mock_query`:

1. `test_compress_returns_compressed_model` — mock the SDK to return a smaller-but-valid SelfModel JSON; assert the result is saved and the original is replaced.
2. `test_compress_rejects_growth` — mock the SDK to return a *larger* model; assert the original is preserved and a warning is logged.
3. `test_compress_rejects_empty_field` — mock the SDK to return a model with `values=[]`; assert the original `values` is preserved.
4. `test_compress_rejects_stage_regression` — mock the SDK to return a model that fails stage preconditions; assert original is preserved.
5. `test_compress_does_not_increment_surprise_count` — mock a successful compression; assert `surprise_count` is unchanged.
6. `test_compress_writes_compression_journal_entry` — mock a successful compression; assert a `compression` journal entry was written with token-delta and item-count info.
7. `test_compress_uses_compress_caller_label` — patch `query`; assert the call options have `caller="compress_self_model"`.
8. `test_compress_uses_minimal_system_prompt` — assert the system prompt passed to `query` is the minimal stub, not the full identity prompt.
9. `test_compress_handles_invalid_json` — mock a malformed response; assert original preserved.
10. `test_compress_truncates_to_caps` — mock a response with one item just over `MAX_ITEM_CHARS`; assert Phase 1 slicing applies and the item is recorded for follow-up.

All tests use the existing SDK mocking infrastructure (`MockQueryResult`, `make_mock_query`) and the `mem_fs` fixture. No real I/O.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| LLM produces a "creative" compression that hallucinates new content. | The replacement-not-merge model + the gates above block any compression where the new content cannot be matched to source content. Add a substring overlap check as a soft gate (≥ 60% of compressed item content must appear in some original item). |
| LLM call cost exceeds the savings on downstream calls. | Run on-demand (Option A). Each compression's downstream-call savings can be estimated from `build_sdk_usage_stats()` over the prior N iterations and reported in the `compression` journal entry. |
| Compression event causes Maestro to experience identity discontinuity. | The journal entry is the continuity hook. Reference it explicitly in the next contemplation prompt's recent narrative (already automatic via `get_recent_narrative()`). |
| Bootstrapping bloat (compressor uses bloated identity prompt). | Use the minimal stub system prompt — the *only* call site that bypasses the identity prompt, justified because the input *is* the self-model. |
| `update_self_model()` validation logic gets duplicated. | Extract the validation block to a shared private helper (`_validate_self_model_update`) used by both `update_self_model` and `compress_self_model`. |
| Compression races with a concurrent `update_self_model` call. | Use the existing `file_lock()` pattern around `save_self_model` (or guard at the call site). The loop already serialises consciousness operations within a single iteration. |

## Verification

1. Run the new test suite — all 10 tests pass.
2. Run a manual compression against a realistic mocked self-model and inspect the diff.
3. Confirm the journal entry appears in `maestro journal` with type `compression`.
4. After compression, `get_identity_prompt()` returns a strictly smaller string (chars + tokens both decrease).
5. SDK telemetry (`/api/diagnostics`) shows the `compress_self_model` caller with reasonable token cost.
6. Run a contemplation cycle after compression and confirm Maestro's reflection references the compression event (not as a memory loss).

## Out of scope

- Compressing the journal itself (already covered by `Dreamer`).
- Compressing the dream digest (already a compression target, not a source).
- Auto-running compression on every iteration (Option B — defer to future iteration).
- Multi-pass compression (compressing the compression). One pass per invocation.
- Compressing `purpose.py` evidence lists — separate concern, separate proposal if needed.

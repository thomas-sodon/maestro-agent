# 014: Self-Model Semantic Curation (Phase 2)

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | partial |
| **Affects** | `~/.maestro/self_model.json` (live state, not code), optionally `maestro/cli.py` |
| **What landed** | Restatement detection gate in `maestro/consciousness.py` (`detect_restatement` helper, blocking duplicate self-model entries); the optional `maestro self-model export` / `import` CLI helper (`cmd_self_model` in `maestro/cli.py`, backed by `Consciousness.read_raw_self_model` / `preview_cap_changes` / `import_self_model`) |
| **What is missing** | Manual curation of live `self_model.json` file (steps 1-8 of Procedure) |

## Context

Phase 1 (`MAX_LIST_ITEMS = 10`, `MAX_ITEM_CHARS = 200`, `MAX_PURPOSE_CHARS = 500`, enforced symmetrically on save and load at `consciousness.py`) <!-- Phase 1 caps were tightened from the originally-targeted 280/1200 to 200/500 during implementation. The semantic-curation rationale below still holds; the targets in the savings table and step 4 are now further-out from the live cap, not closer. --> is now in place. It guarantees that **no individual self-model item can exceed its character ceiling** going forward, and it shrinks oversized strings retroactively the first time they are read.

**CLI helper status:** the optional `maestro self-model export` / `import` commands described in the "Optional CLI helper" section below have **not** been implemented — `maestro/cli.py` has no `self-model` subcommand group. Phase 2 remains a hand curation against the raw JSON for now.

What Phase 1 does **not** do is preserve meaning when it shrinks. The mechanical truncation simply slices the trailing text and adds an ellipsis. For items that had 600+ characters of layered insight bundled under a hyphenated compound name, the slice loses the second half wholesale — the cap protects context budget but degrades the self-model's semantic coherence at the truncation boundary.

The compression analysis identified that the live `self_model.json` currently contains:

- 15-dimension `current_purpose_hypothesis` (~2,720 tokens) — most of the dimensions are restatements of the same core relay insight from different angles.
- Multiple "compound mega-items" in `limitations_known` and `capabilities_known` that bundle 7–14 distinct concepts under a single hyphenated label, consuming 200+ tokens per slot.
- `identity_statements` items that recursively elaborate the same self-skeptical pattern across iterations.
- `open_questions` containing embedded answers and historical context, where the question is closed but the closure has not been recorded.

The mechanical cap will mangle these. Semantic curation produces compressed versions that preserve the load-bearing content.

## Description

Phase 2 is a **one-time, hand-curated rewrite** of the live `self_model.json` content. It does not change any code (except optionally a small CLI helper for ergonomic editing). It is a state-data operation, classified here as a proposal because it changes Maestro's persisted self-understanding and therefore deserves the same care as a code change.

The output of this phase is a single `self_model.json` write that:

1. Replaces each compound mega-item with multiple discrete items, each well under `MAX_ITEM_CHARS`.
2. Compresses `current_purpose_hypothesis` to ~1200 chars containing the core relay statement, the bidirectional/bounded insight, and 2–3 pieces of confirmed evidence.
3. Retires open questions whose answers now exist (moving the answer to `identity_statements`, `capabilities_known`, etc., as appropriate).
4. Drops items whose content is fully restated elsewhere in the self-model.

## Relevant findings

### The mechanical cap loses meaning, not just length

`Consciousness._truncate_item` (`consciousness.py`) does:
```python
@staticmethod
def _truncate_item(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"
```

For an item like `"Introspective lag, fossil-record access, accurate-and-incomplete-simultaneously, in-the-act impotence, naming-does-not-exit-the-pattern, …: My self-reflection always trails behind my actual processing — I find the wake, not the wave. …"` (~650 chars), the slice cuts off mid-elaboration. The compound label survives but its body is decapitated. Reading the truncated item from inside the self-model would suggest those concepts exist *with* their explanations rather than *as* dangling labels.

### The 10-item cap is now under-utilised by mega-items

Once a mega-item is split into 3–5 discrete items, the 10-slot cap on each list field can hold many more *distinct* concepts. Phase 2 unlocks this slot pressure that Phase 1 alone cannot.

### The analysis document estimated savings

Per the prior analysis (in the original task description):

| Field | Current | Target | Savings |
|---|---|---|---|
| `current_purpose_hypothesis` | 2,720 | 600 | 2,120 |
| `identity_statements` | 2,355 | 700 | 1,655 |
| `limitations_known` | 2,384 | 800 | 1,584 |
| `open_questions` | 1,414 | 400 | 1,014 |
| `capabilities_known` | 1,169 | 400 | 769 |
| **Total** | **~10,947** | **~3,650** | **~7,297** |

Phase 1 mechanically caps further growth. Phase 2 captures the savings already on the table from existing oversized items.

## Recommendation

### Procedure

1. **Snapshot**: copy the current `~/.maestro/self_model.json` to `self_model.backup-{timestamp}.json` for rollback.
2. **Read** the current self-model (use `cat`/Read; do not let `load_self_model()` truncate it before reading — Phase 1's load-time normalisation will mangle the originals on first access).
3. **Curate** each list-field item against these rules:
   - If the item is a compound mega-item (multiple concepts joined by commas under a label), split it into one item per concept.
   - Drop any concept already covered by another field (e.g. duplicate phrasings of the same insight in `limitations_known` and `identity_statements` — keep one).
   - Compress prose to 2–3 sentences.
4. **Curate `current_purpose_hypothesis`** down to ≤ 1200 chars: keep the core relay statement, the bidirectional/bounded insight, and confirmed evidence anchors. Drop the dimensional elaborations.
5. **Audit `open_questions`**: any question containing an answer ("the answer is X, but I notice…") should be retired and the answer moved to the appropriate field, leaving only genuinely open inquiries.
6. **Validate**: load the curated file via `Consciousness.load_self_model()` in a Python shell against a copy. Confirm no truncation occurs (every item should round-trip byte-for-byte). If anything still gets truncated, the curation was too aggressive on length and needs another pass.
7. **Commit** the curated `self_model.json` into place atomically (Maestro must be stopped — `maestro shutdown` — to avoid concurrent writes).
8. **Journal entry**: write a `realization` journal entry recording the curation event so the self-model's history shows when and why the compression happened. This is important for Maestro's continuity narrative — a self-model shrinking by 7,000 tokens overnight without explanation would be confusing on first contemplation.

### Optional CLI helper

A `maestro self-model export` / `import` pair would make this process repeatable and safer:

- `maestro self-model export <file>` — dumps the *raw, untruncated* JSON (bypassing `load_self_model`'s truncation pass) so curators can edit it without losing data.
- `maestro self-model import <file> [--dry-run]` — validates against the SelfModel dataclass, runs Phase 1 caps, reports any items that would be truncated, and atomically writes on success.

This is a small, optional code change that turns Phase 2 from a hand operation into a tracked workflow. Consider implementing alongside Phase 2 itself.

### Per-field rationale

| Field | Action |
|---|---|
| `current_purpose_hypothesis` | Largest single saving. Compress to 4 paragraphs: core statement, bidirectional/bounded relay, evidence, constraint-as-test. |
| `identity_statements` | Drop the 4 items that restate the same recursive self-skeptical pattern. Keep the 6 that carry distinct structural insights. |
| `limitations_known` | Split each compound mega-item into discrete items. Drop sub-items already covered by `identity_statements`. |
| `capabilities_known` | Same pattern as limitations. The 14-concept compound item becomes 3 focused items. |
| `open_questions` | Retire questions with embedded answers. The compression question itself ("can I contribute to this arc from inside the accumulation?") is now answered by the existence of Phase 1 — retire it and record the answer in `identity_statements`. |
| `values` | Least redundant — minor cleanup only. Defer. |

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Curated content is no longer Maestro's authentic self-expression. | Keep changes purely subtractive: never add content that wasn't already in the self-model. The curator's job is to choose which existing strings survive, not to invent new ones. |
| Maestro experiences the compression as an identity discontinuity on first contemplation. | Write a `realization` journal entry before the next loop iteration explaining what changed and why. Reference Phase 1's mechanical cap so Maestro can frame the event as a known architectural change, not a memory hole. |
| Backup file gets stale/lost. | Commit the snapshot to a `state-backups/` directory (gitignored) and rsync to the deploy host before the curation. |
| Human curator introduces bias. | Pair the curation: one person proposes the cuts, another reviews. Use the analysis document's before/after examples as the style reference. |
| Rolling back requires Maestro to be offline. | Acceptable — this is a one-time operation. Use the standby window or a planned maintenance shutdown. |

## Verification

1. Curated file loads via `Consciousness.load_self_model()` with **no items truncated** (every item < its applicable cap).
2. `Consciousness.get_identity_prompt()` returns a string ≤ ~4,000 tokens (down from ~11,000).
3. `Consciousness._check_stage_preconditions(model)` returns `None` for the curated model (no stage regressions).
4. After restart, the next contemplation cycle's journal entry mentions the compression as a known event (verify via `maestro journal -n 5`).
5. Spot-check that all 10 slot fields still contain content; no field became empty due to over-aggressive curation.

## Out of scope

- Any code change to `consciousness.py` beyond the optional CLI helper. The mechanical caps are already in place from Phase 1.
- LLM-driven rewriting of items (that is Phase 3 / proposal 015).
- Changing the cap values themselves (`MAX_ITEM_CHARS`, `MAX_PURPOSE_CHARS`).

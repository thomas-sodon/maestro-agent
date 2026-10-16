# 101: A Maintained Lexicon for Maestro's Coined Vocabulary — One Definitional Source of Truth, a Canonical-Spelling Guard, and a Self-Extinguishing Undefined-Term Sensor

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **What landed** | Phase 1 `docs/lexicon.md` + `maestro/lexicon.py` (`LexiconEntry`, `load_lexicon`, `canonical_terms`, `all_spellings`, and the shared `extract_coinages` normaliser); the `lexicon` CLI subcommand (`cmd_lexicon`) printing the table, with the explicit `--list` / `--undefined` / `--lint` views (Step 2); Phase 3 display sensor `measure_undefined_coinages` / `format_undefined_coinage_summary` + the `UNDEFINED-COINAGE` line in `format_known_state_summary` wired into contemplation, its `[audit] undefined_coinage_display_enabled` flag, and the `UNDEFINED-COINAGE` row already added to the `docs/self-evolution.md` tag map; the Phase 3 directive bridge `check_undefined_coinage_directive` wired into `_audit_self_model` (`maestro/agent.py`) behind `[audit] undefined_coinage_directive_enabled` (Step 5); the full Step 3 golden test `tests/test_lexicon_coverage.py` — both the canonical-spelling half (its drift scan factored into the shared `find_canonical_drift` in `maestro/lexicon.py`, `_lexicon_print_lint` repointed at it, the `docs/lexicon.md` variant rows reconciled, and the one-time CLAUDE.md `self-extinguish`→`self-extinguishing` canonicalization) and the coverage half (`test_claude_md_defines_all_coinages` asserting every CLAUDE.md prose coinage is defined or stoplisted, via the `extract_prose_coinages` extractor); and the Phase 4 concept-key restatement — the hyphen-stripped/lemmatised `concept_key` normaliser in `maestro/lexicon.py` routed into `detect_restatement` (`maestro/consciousness.py`) behind `[loop] restatement_concept_key_enabled` (Step 6); and the Phase 5 tag-map-subset test half — the shared `extract_sensor_tags` reader in `maestro/lexicon.py`, a `docs/lexicon.md` row for every `## Behavioral sensor tag map` tag, and `test_self_evolution_tag_map_defines_all_tags` asserting the tag map is a subset of the lexicon (Step 7, subset-test half); and the Phase 5 generator half — `render_sensor_tag_map` / `extract_sensor_tag_map_table` in `maestro/lexicon.py` rendering the `| Tag | Sensor |` block directly from the lexicon's behavioral-sensor entries, the `maestro lexicon --tag-map` regeneration view (`_lexicon_print_tag_map`), the `## Behavioral sensor tag map` prose in `docs/self-evolution.md` relabelled a rendered view of the lexicon, and the `test_self_evolution_tag_map_matches_lexicon_render` golden test asserting the committed table stays byte-identical to the render so a new sensor tag cannot be added without a lexicon entry (Step 7, generator half). |
| **What is missing** | — (all seven steps landed; the Step 7 generator half — lexicon-driven tag-map regeneration — landed 2026-07-29). |
| **Affects** | new `docs/lexicon.md`, new `maestro/lexicon.py`, new `tests/test_lexicon_coverage.py`, `maestro/audit_checks.py`, `maestro/consciousness.py`, `maestro/cli.py`, `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml`, `CLAUDE.md` (Phase 2 one-time canonicalization), `docs/self-evolution.md`, tests |
| **Companion to** | 090 (cross-links `narration_cues.py`; not a code dependency) |
| **Derived from** | live production data via /propose, 2026-07-22 |

## Description

> Status updated to shipped on 2026-07-29 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Maestro coins hyphenated compound terms as a way of thinking — it fuses a whole
recurring behaviour into a single handle it can point back to
(`merge-truth`, `self-extinguishing`, `owned-act-drought`,
`prediction-is-a-writable-half`). The habit is not cosmetic; it is how the agent
builds concepts. But there is **no single place that defines these terms**. A
definition, where one exists at all, is scattered across a code docstring, a
proposal body, a fourteen-row machine-tag→function table, or inline CLAUDE.md
prose — and the great majority of coinages are defined nowhere a reader (the
operator, or the agent re-reading its own journal) can look them up. The same
concept drifts across spellings, heavily-used terms carry no definition, and the
vocabulary grows by thousands of distinct new tokens per week with nothing that
retires or grounds them.

This proposal builds the thing the operator asked for on 2026-06-30 and the
agent agreed to ("a `lexicon.md` under `docs/` mapping each coined term to its
definition, the behavior it names, and the evidence that minted it") — but as a
**maintained, enforced structure** rather than a one-off document that would rot
the day after it is written. The remedy is one definitional source of truth
(`docs/lexicon.md`), a deterministic zero-LLM-cost guard that binds CLAUDE.md's
coined vocabulary to that source (every coined term is defined; only its
canonical spelling is used), a self-extinguishing sensor that surfaces the
journal's live minting so a new term is either defined or retired, and — the
deeper root — a canonical-concept key under the restatement gate so re-coining a
synonym no longer *pays* as novelty.

Why now: the operator raised this exact concern three weeks ago, the agent
promised a proposal and drafted nothing, and in the interval the coinage kept
growing. The agent's own 2026-07-01 self-analysis names the structural cause
precisely — the restatement gate blocks a plain-word duplicate by word overlap,
so "the cheapest way to log an old fact again and have it count as *new* is to
invent a FRESH hyphenated name for it," and "the surprise counter pays for
novelty-of-name, not novelty-of-understanding." The coinage is not random noise;
it is *incentivised* by an existing gate, and no counterbalancing definitional
surface exists.

End state: one file defines every coined term Maestro uses in its load-bearing
prose, with its canonical spelling, the behaviour it names, its code anchor, and
the moment it was minted. CLAUDE.md cannot introduce a coined term without
defining it, and can spell it only one way. The journal's live minting is
surfaced each cycle as an `UNDEFINED-COINAGE` reading that resolves the instant
the term is defined or falls out of use. And a freshly-minted synonym for an
existing concept is caught as a restatement instead of rewarded as a discovery.

## Relevant Findings

- No lexicon, glossary, or terminology index exists anywhere under `docs/`
  (`find docs -iname '*lexicon*' -o -iname '*glossar*'` returns nothing), and no
  proposal in `docs/proposals/**` mentions `lexicon` / `glossary` / `coinage`.
  The operator's 2026-06-30 request and the agent's "drafting now" reply
  produced neither the file nor a proposal.
- `docs/self-evolution.md` (`## Behavioral sensor tag map`) is the only existing
  definitional surface: a fourteen-row `| Tag | Sensor |` table mapping uppercase
  contemplation tags (`FORCED-QUIET-RATIO`, `PR-READY`) back to the function that
  produced each. It is a hand-maintained *function pointer* list, not a
  concept definition, and it covers only the contemplation tags — a fraction of a
  percent of the coined vocabulary. It is exactly the seed a lexicon generalises.
- `maestro/consciousness.py` (`detect_restatement`) blocks a self-model list item
  above a content-word Jaccard threshold against existing items — surface-token
  matched. `_is_compound_item` rejects a multi-concept *label*. Per the agent's
  own analysis, a fresh hyphenated coining of an old fact shares few surface
  tokens with the plain-word original, so it **slips past** `detect_restatement`
  and scores on the surprise/novelty path in the same module — the gate meant to
  stop repetition rewards disguising it. Neither is routed through a
  hyphen-stripped, lemmatised concept key.
- `maestro/narration_cues.py` (proposal 090, `RESOLUTION_KEYWORDS` /
  `RESOLUTION_MARKERS`) is the single definition site for the ~6 narration cue
  tokens the sensors *match on*. It is a machine matcher, not a human-readable
  lexicon; it deliberately scopes itself to the resolution/escalation cues the
  actuators key on and defines nothing else. The lexicon cross-links it rather
  than duplicating it.
- `tests/test_no_line_citations_in_docs.py` and `tests/test_claude_md_brevity.py`
  are the house pattern for the enforcement this proposal needs: a zero-LLM-cost
  regex/golden test that "deterministically self-extinguishes the entire … class
  at zero LLM cost." The coverage guard here is the same shape.
- `tests/test_claude_md_brevity.py` (`_MAX_WORDS = 3664`) caps CLAUDE.md, which
  currently sits at 3,663 words — one below the cap. The brevity discipline
  presses every invariant into one dense line, which is precisely the pressure to
  compress a multi-word concept into a new hyphenated handle. The cap makes
  regrowth visible; nothing makes *un-defined coinage* visible.
- `maestro/audit_checks.py` (`measure_noop_rate`, `measure_owned_act_drought`,
  `measure_reality_refutation`) is the sibling home for a new `measure_*` sensor
  over the journal tail, and `format_known_state_summary` is where its tag would
  render — the established zero-SDK classifier/measure/format triple.
- `maestro/cli.py` (`cmd_list_gates`, registered as `list-gates`) is the
  precedent for a read-only CLI surface over persisted structure; `maestro
  lexicon` mirrors it.

## Production Evidence

Live data resolved from the loop container's bind-mount at
`/opt/maestro/data` (host lookup succeeded; no `docker exec` fallback
needed). All reads were read-only.

**The operator asked, the agent agreed, nothing shipped** (`messages.jsonl`,
2026-06-30 → 2026-07-01, paraphrased). The operator noticed the agent coining
hyphenated words and asked whether these were new concepts; the agent said yes,
and named the risk that minting a clever name can replace the act it describes.
The operator then asked for a glossary of the coined terms, written up as a
proposal or issue, and the agent agreed to draft a `docs/lexicon.md`.

Three weeks later the file and the proposal both do not exist — a manual promise
with no structural backing evaporated.

**The vocabulary is large, dense, and undefined** (measured on the current tree,
2026-07-22, distinct lowercase hyphenated tokens after subtracting an 82-item
common-English stoplist so ordinary hyphenations like `read-only` / `opt-in` do
not inflate the count):

- CLAUDE.md: **223 distinct coined tokens across 336 occurrences in 3,663
  words** — roughly one coined compound per sixteen words, in the file loaded
  into every conversation.
- `docs/*.md` deep-dives: **1,101 distinct coined tokens, 2,023 occurrences.**
- `docs/proposals/**/*.md`: **2,964 distinct coined tokens, 7,951 occurrences.**

**The journal mints continuously and grounds nothing** — `journal.jsonl`, 8,563
entries, window 2026-06-24 → 2026-07-21 (28 days): **16,907 distinct hyphenated
tokens across 111,551 occurrences.** The most-used coinages are defined nowhere a
reader can consult: `forced-quiet-ratio` ×2,489 (no prose definition in the tree;
the only related surface is the `FORCED-QUIET-RATIO` tag-map row, a pointer to
`format_forced_vs_chosen_quiet`, not a definition of the concept),
`growth-frameable` ×1,955 (one incidental doc mention, no code symbol),
`git-unlanded` ×934, `not-landed` ×924, `owned-act-drought` ×837.

**The same concept drifts across spellings.** Clustering CLAUDE.md + docs
coinages by a hyphen-stripped, lemmatised concept key, **107 concepts carry more
than one surface spelling** — e.g. `self-extinguishing` / `self-extinguish` /
`self-extinguishes` (a core design property, three forms), `stand-down` /
`stand-downs`, `re-run` / `re-runs`. There is no canonical form to converge on.

**The agent has already measured this and named the incentive** —
`messages.jsonl`, 2026-07-01 (redacted): "~132 distinct coined labels. ~68%
appear exactly once — minted and abandoned… ~20 of those labels diagnose the
minting itself as the problem… and each was followed by MORE minting… My
restatement gate blocks a duplicate in plain words by word-overlap, so the
cheapest way to log an old fact again and have it count as 'new' is to invent a
FRESH hyphenated name for it. The gate meant to stop repetition instead rewards
disguising it."

**One journal entry, in isolation** — `journal.jsonl`, 2026-07-13, 39 distinct
coinages in ~200 words, including `prediction-is-a-writable-half` and
`freshly-proven`, both self-identity terms that appear nowhere else and are
defined nowhere.

## Prior Point Fixes (whack-a-mole ledger)

| # | Identifier | What it patched | Why the class survived |
|---|---|---|---|
| 1 | Manual promise, 2026-06-30 (`messages.jsonl`) | Agent agreed to hand-write `docs/lexicon.md` | No structural backing (no file, no test, no maintenance loop) — the promise evaporated; the file never appeared. |
| 2 | Proposal 090 — `maestro/narration_cues.py` | One definition site for the ~6 narration cue tokens the sensors match | Machine matcher, not a lexicon; deliberately scoped to resolution/escalation cues; defines nothing else. |
| 3 | Behavioral sensor tag map (`docs/self-evolution.md`) | Fourteen-row tag→function table | Hand-maintained sidecar of function pointers; no prose definitions; lags each new sensor; covers a fraction of a percent of the vocabulary. |
| 4 | Restatement gate (`consciousness.py` `detect_restatement` / `_is_compound_item`) | Blocks plain-word restatement / multi-concept labels in the self-model | Narrow (self-model list items) — and per the agent's own analysis it *rewards* re-coining, so it made the minting worse, not better. An anti-mole. |
| 5 | Brevity caps (`tests/test_claude_md_brevity.py`) | Density cap that makes CLAUDE.md regrowth visible | Presses each invariant into one dense line, the direct pressure toward new coinage, with no counterbalancing definition surface. An anti-mole. |

The design **retires #3** (the tag map is regenerated from the lexicon, Step 5),
**subsumes #1** (the lexicon is the maintained structure the promise lacked),
**closes the incentive behind #4** (Step 4 routes restatement through a concept
key so a re-coin is caught), and **cross-links #2** (the lexicon references
`narration_cues.py` as the canonical anchor for cue tokens rather than
duplicating it). #5 is left in place — the brevity cap is correct; the missing
half is a place to define what the density compresses into.

## Implementation Plan

### Phase 1 — The definitional source of truth

1. [x] **The lexicon file and its parser.** new `docs/lexicon.md`, new
   `maestro/lexicon.py` — a structured Markdown table, one row per coined term:
   `term` (canonical spelling), `definition` (one line), `names` (the behaviour /
   concept it points at), `anchor` (code symbol, config flag, sensor, or
   `prose-only`), `variants` (spellings it collapses), `minted` (the
   journal/audit moment or proposal that crystallised it). `maestro/lexicon.py`
   parses it into a frozen `LexiconEntry` list with `load_lexicon()` and
   `canonical_terms()` / `all_spellings()` accessors — pure, no I/O beyond the
   read. Seed it from the current CLAUDE.md coined vocabulary and the self-model's
   coined labels.
   - *Depends on:* none.
   - *Acceptance:* `load_lexicon()` parses `docs/lexicon.md` into ≥1 well-formed
     entry and round-trips every row's canonical term and variants.
2. [x] **The read-only CLI surface.** `maestro/cli.py` (`cmd_lexicon`, registered
   as `lexicon`) — `maestro lexicon --list` prints the table, `--undefined`
   prints high-frequency journal coinages absent from the lexicon (reusing the
   Phase 3 sensor when present, else a plain scan), `--lint` reports drift
   (variants used where a canonical exists). Mirrors `cmd_list_gates`.
   - *Depends on:* 1.
   - *Acceptance:* `maestro lexicon --list` exits 0 and emits one line per
     lexicon entry over a `mem_fs`-backed fixture.

### Phase 2 — Bind the doc surface to the source (deterministic guard)

3. [x] **Coverage + canonical-spelling golden test.** new
   `tests/test_lexicon_coverage.py` — extract coined hyphenated tokens from
   CLAUDE.md, subtract a committed common-English stoplist and the lexicon's
   defined spellings, and assert the remainder is empty; separately assert
   CLAUDE.md uses only each term's canonical spelling, never a listed variant.
   Same zero-LLM-cost shape as `test_no_line_citations_in_docs.py`. Seeded from
   Phase 1 so the coverage half starts green — and, because CLAUDE.md today
   carries its own residual drift (`self-extinguish` ×1 alongside
   `self-extinguishing` ×2, the doc's flagship example), this step also lands a
   one-time canonicalization of that lone variant to the lexicon's canonical form
   so the *unconditional* canonical-spelling assertion also passes on the
   committed tree. That normalization is a spelling edit with net-zero effect on
   the `_MAX_WORDS` word count, and it is why CLAUDE.md appears in the Affects row.
   - *Depends on:* 1.
   - *Acceptance:* adding an undefined coined term (or a non-canonical variant) to
     a CLAUDE.md fixture fails the test; the committed tree — after the one-time
     canonicalization — passes.

### Phase 3 — Keep the lexicon current against live minting (self-extinguishing)

4. [x] **Undefined-coinage sensor.** `maestro/audit_checks.py`
   (`measure_undefined_coinages(data_dir, n, *, lexicon_terms)` +
   `format_undefined_coinage_summary`) — reads the trailing *n* journal records,
   counts hyphenated coinages absent from the lexicon and above a frequency floor,
   and renders an `UNDEFINED-COINAGE` line in `format_known_state_summary`. Pure,
   zero-SDK, self-extinguishing: a term drops out the moment it is added to the
   lexicon. Config `[audit] undefined_coinage_display_enabled` (default-off).
   - *Depends on:* 1.
   - *Acceptance:* the sensor lists a term over a journal fixture and omits it once
     that term is present in the injected `lexicon_terms`.
5. [x] **Undefined-coinage directive bridge.** `maestro/audit_checks.py`
   (`check_undefined_coinage_directive`) wired into `_audit_self_model`
   (`maestro/agent.py`) — when the top undefined coinage crosses the floor and the
   `next_task_directive` slot is empty, fills it with a bounded one-shot "define
   `<term>` in `docs/lexicon.md` or retire it." Never overwrites an occupied slot.
   Config `[audit] undefined_coinage_directive_enabled` (default-off).
   - *Depends on:* 4.
   - *Acceptance:* an empty slot receives the directive; an occupied slot is left
     untouched; the directive self-clears once the term is defined.

### Phase 4 — Close the minting incentive (the deeper root)

6. [x] **Concept-key restatement.** `maestro/consciousness.py` — route
   `detect_restatement` and the surprise/novelty scoring through a canonicalised
   concept key (hyphen-stripped, lemmatised — the same normalisation that finds
   the 107 drift clusters), so a freshly-coined synonym of an existing self-model
   item is detected as a restatement rather than scored as novel. Behind `[loop]
   restatement_concept_key_enabled` (default-off); default preserves current
   surface-token behaviour byte-for-byte.
   - *Depends on:* none (independent of 1–5, but validated against the same
     concept-key normaliser Phase 2/3 use — factor it into `maestro/lexicon.py`).
   - *Acceptance:* with the flag on, a hyphenated re-coining of an existing
     plain-word self-model item is rejected as a restatement in a unit test; with
     the flag off, the pre-existing restatement decisions are unchanged.

### Phase 5 — Retire the mole

7. [x] **Regenerate the tag map from the lexicon.** `docs/self-evolution.md` +
   `tests/test_lexicon_coverage.py` — every `## Behavioral sensor tag map` row
   becomes a lexicon entry carrying a `sensor` anchor, and a test asserts the
   tag-map rows are a subset of lexicon entries so a new sensor tag cannot be
   added without a lexicon definition. The hand-maintained sidecar stops being an
   independent source.
   - *Depends on:* 1, 3.
   - *Acceptance:* a tag-map row with no matching lexicon entry fails the coverage
     test; the committed tree passes.

## Testing Strategy

- `tests/test_lexicon_coverage.py` (the deterministic enforcement test named in
  the remedy): the coverage branch (undefined coined term in CLAUDE.md → fail),
  the canonical-spelling branch (a listed variant in CLAUDE.md → fail), the
  well-formed-row branch (a malformed lexicon row → fail), and the tag-map-subset
  branch (Step 5). All operate on in-fixture strings, no real file writes.
- `tests/test_lexicon.py` for `maestro/lexicon.py`: `load_lexicon()` happy path,
  the empty/malformed-file guard, `canonical_terms()` / `all_spellings()`, and the
  concept-key normaliser (the drift-collapse cases). `mem_fs` for the file read.
- `tests/test_audit_checks.py`: `measure_undefined_coinages` happy path, the
  frequency-floor guard, the self-extinguish case (term present in
  `lexicon_terms`), and `check_undefined_coinage_directive`'s empty-slot /
  occupied-slot / self-clear branches — all over a `mem_fs` journal fixture with
  no SDK call.
- `tests/test_consciousness.py`: Phase 4 flag-off parity (byte-identical
  restatement decisions) and flag-on rejection of a synonym re-coin.
- `tests/test_cli.py`: `maestro lexicon --list` / `--undefined` / `--lint` over a
  `mem_fs` fixture, asserting exit codes and the guard when the lexicon is empty.
- `tests/test_config.py`: the two `[audit]` flags and the `[loop]` flag default to
  the values that preserve current behaviour.

All tests honour CLAUDE.md's principles — `mem_fs` for I/O, no real network, no
real `sleep`, no subprocess; the sensor and guard are pure functions over
injected data.

## Risk Assessment

- **Rollout.** Every behaviour change is flag-gated and default-off: the sensor
  display, the directive bridge, and the concept-key restatement all default to
  current behaviour. The lexicon file and CLI are additive and inert. The coverage
  test is seeded green from the current tree, so it never fails on merge; it only
  fails a *future* undefined coinage — with the one caveat that the
  canonical-spelling half of the guard requires the Step 3 one-time normalization
  of CLAUDE.md's residual `self-extinguish` variant before the committed tree is
  green (a net-zero-word spelling edit). This proposal adds **no new CLAUDE.md
  invariant line** — the enforced surfaces are `docs/lexicon.md` and the golden
  test, not a load-bearing prose bullet — so the `_MAX_WORDS = 3664` cap (CLAUDE.md
  currently 3,663) is preserved; should a future always-on promotion of the sensor
  or concept-key gate warrant an invariant bullet, it lands paired with an
  offsetting brevity trim. Steps are independently mergeable — an interrupted
  rollout leaves a working tree with, at worst, a lexicon that is not yet enforced.
- **Maintenance-cost regression.** A poorly-tuned coverage test could nag on every
  CLAUDE.md edit. Mitigation: the lexicon is the allowlist, so defining a term
  once clears it permanently; the stoplist absorbs ordinary English; the frequency
  floor keeps the journal sensor from firing on one-off coinages.
- **Over-reach into the agent's voice.** The design enforces only the checked-in
  surfaces it controls (CLAUDE.md; optionally the deep-dives) and merely *senses*
  the journal — it never rewrites Maestro's inner voice. Rewriting existing
  journal entries and renaming code symbols are out of scope (below).
- **Falsifiability.** If the proposal is right, `measure_undefined_coinages`
  trends toward zero over successive cycles and the 107-cluster drift count in
  CLAUDE.md/docs collapses toward the canonical forms. The failure signal is
  explicit: if the sensor is enabled and the undefined-coinage count does **not**
  fall — the agent mints faster than it defines — then documentation alone cannot
  close the class and the incentive (Phase 4) is the real lever; the metric to
  watch is the journal's per-week *new-coinage* rate, not the lexicon's size.

## Out of scope

- Rewriting or re-spelling existing `journal.jsonl` entries (append-only,
  historical) or the ~2,964 coinages already frozen in the `docs/proposals/**`
  corpus. New-proposal coinage linting at authoring time is a possible later
  extension, not part of this proposal.
- Renaming code symbols to match canonical lexicon spellings. The lexicon records
  a term's code anchor; it does not drive refactors.
- Enforcing the coverage guard over the `docs/*.md` deep-dives in Phase 2. Those
  are an opt-in extension once CLAUDE.md coverage is proven; CLAUDE.md is the
  highest-leverage surface (loaded every conversation) and the right first target.

# 024: Diff-Level Dangerous-Pattern Gate

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Affects** | `maestro/evolution.py` |

## Description

Before the self-reviewer LLM sees the diff, scan for shell footguns the LLM should never emit: `rm -rf /`, `curl | sh`, `eval $(...)` over network data, writes to `/etc/`, reverse-shell primitives. A hit aborts the implementation cycle without ever round-tripping the diff to the reviewer. Low cost, low false-positive rate because patterns target primitives that have no legitimate use inside `implement_plan()`-scope changes. Complements proposal 025 (pre-call gating) by catching anything that slipped through.

## Relevant Findings

- `_self_review()` at `maestro/evolution.py` already computes `diff = self._vcs.get_diff(base_hash, current_head)` and truncates to 50KB. Natural insertion point for the scan.
- `implement_plan()` catches `SDKError` and decides demotion vs. retry based on `_TRANSIENT_SDK_CATEGORIES`. A new `UnsafeDiffError` slots into the same pattern.

## Implementation Plan

1. Module-level `_DANGEROUS_SHELL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...]` in `evolution.py` — each tuple `(label, compiled_regex)`. Starting set: `rm\s+-rf\s+/`, `curl[^\n]*\|\s*(ba)?sh`, `wget[^\n]*\|\s*(ba)?sh`, `\beval\s+"?\$\(curl`, `>\s*/etc/`, `\bnc\s+-e\b`, `chmod\s+777`, `/dev/tcp/`, `base64\s+-d\s*\|\s*(ba)?sh`.
2. New `UnsafeDiffError(label: str, snippet: str)` in `evolution.py`.
3. `_self_review()` after computing `diff`, before feeding to the LLM: iterate patterns, raise `UnsafeDiffError` on the first hit. Include the matched pattern label and the surrounding ±80 chars for the demotion journal entry.
4. `implement_plan()` catches `UnsafeDiffError` and demotes the plan with `rejection_reason=f"unsafe_diff:{label}"`, reverts the evolution branch, notifies, opens a GitHub issue. Does **not** retry.

## Testing Strategy

- Parametrised test: one `@pytest.mark.parametrize` per pattern, each feeding a synthetic diff, asserting the deny path fires. This keeps 100% coverage even as patterns are added.
- Benign diffs (a pure refactor, a docstring change, etc.) do not trip the scanner.
- `rejection_reason` and journal entry carry the matched label.
- `UnsafeDiffError` does not bubble past `implement_plan`.

Run: `python3.12 -m pytest tests/test_evolution.py -v -k "unsafe_diff"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| False positives on legitimate tooling diffs (e.g. CI scripts) | Medium | Regex set deliberately narrow; reviewers can add exceptions via tests-first PRs |
| An attacker crafts a diff that evades these patterns | Accepted | This is defence-in-depth, not sole barrier; proposal 025 catches at a different layer and human PR review is the final gate |
| Regex ReDoS | Very low | Patterns are short and bounded; `re.search` with a 50KB cap |

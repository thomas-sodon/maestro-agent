Search the Maestro project for documentation/code drift and fix what is safely fixable.

This command targets a fixed set of drift categories where CLAUDE.md, the docs, the TOML config, and the code are expected to agree. It does not attempt open-ended "code review" — that is what `/review-changes` is for.

`$ARGUMENTS` selects scope:
- *(empty)* — run all categories below.
- One or more category names from the list (`modules`, `cli`, `config`, `caps`, `links`, `literals`) — run only those.
- `--dry-run` — report findings only; do not edit any file even for trivial fixes.

`--dry-run` may appear alongside any number of category tokens; treat it as a flag, not a category.

## Categories

For each category, the steps are: (a) collect the two sides, (b) compute the drift, (c) classify each finding as **trivial-fix** (safe to apply immediately) or **needs-confirmation** (asks user first), (d) act per the rule for that category. Never invent a "side" — read it from the source.

### 1. `modules` — CLAUDE.md module table ↔ filesystem

- Left side: every `maestro/*.py` module (skip `__init__.py`, `__main__.py`, anything under `maestro/prompts/` is its own subsection in CLAUDE.md).
- Right side: rows in the `### Core Modules` table in CLAUDE.md, plus the Core Modules subsection rows for `maestro/prompts/*` and `maestro/notifications/`, `maestro/dashboard/`.
- Drift cases:
  - File exists but is missing from the table → **needs-confirmation** (a one-line purpose summary requires user judgment).
  - Table row points to a path that does not exist → **needs-confirmation** (a rename target requires user judgment; "grep-able from purpose" is a fuzzy match and the agent should never silently rewrite a CLAUDE.md path).
  - Table row purpose mentions a function/class that no longer exists → flag as **needs-confirmation**.

### 2. `cli` — CLI command coverage

- Left side: every command registered on the `cli` Click group in `maestro/cli.py` (search for `@cli.command(` and similar decorators; capture the canonical name from the decorator, not the function name).
- Right side: the rows under `## CLI Usage` in CLAUDE.md and the corresponding section in `README.md`.
- Drift cases:
  - Command exists in `cli.py` but is missing from CLAUDE.md or README → **needs-confirmation**.
  - CLAUDE.md / README references a `maestro <subcmd>` that does not exist in `cli.py` → **needs-confirmation** (could be a rename — propose the closest match).
  - Argument names or flag names differ between `cli.py` (`@click.option("--foo")`) and the CLAUDE.md row → **trivial-fix** in CLAUDE.md to match the code (code is the source of truth).

### 3. `config` — TOML keys ↔ Config dataclass ↔ docs

- Sources: `maestro/config.py` (`Config` dataclass fields and `[section]` mapping), `maestro/toml_config.py` (loader / overlays), `maestro.toml` (defaults), and the `## CLI, configuration, and secrets` section of CLAUDE.md.
- Drift cases:
  - Key in `Config` dataclass but missing from `maestro.toml` defaults → **needs-confirmation** (we need a real default value).
  - Key in `maestro.toml` but unknown to `Config` / `toml_config` → **needs-confirmation** (could be a typo or dead key).
  - Section listed in CLAUDE.md (`Sections: …`) but not present in `maestro.toml` or `Config` → **trivial-fix** in CLAUDE.md to drop or correct the section name.
  - `maestro.{env}.toml` overlay file contains a value that looks like a real secret — assigned to `*token*`, `*password*`, `*key*`, or `*chat_id*`, and the right-hand-side value is **all of**: ≥ 16 characters, mixed case or mixed alphanumeric (long opaque string), **and** does not match any obvious-placeholder shape. Treat as obvious-placeholder if any of these hold: the value is uppercase-only (e.g., `REPLACE_ME_WITH_A_LONG_PLACEHOLDER_VALUE`); the value contains any of the substrings `EXAMPLE`, `PLACEHOLDER`, `YOUR_`, `REPLACE`, `CHANGEME`, `SAMPLE`, `DUMMY`, `FAKE`, or `TODO` (case-insensitive); the value is a single repeated character or `xxxxxxxx`-style filler; the value parses as a literal English word or phrase. Obvious placeholders are not flagged at all. A value that survives the obvious-placeholder filter is flagged as **needs-confirmation**, not a bug — surface the file:line and the masked value, and let the user disposition it. Do not edit overlays automatically.

### 4. `caps` — documented caps vs enforced constants

CLAUDE.md states many concrete caps (e.g., "Identity lists capped at 10 × 200 chars", "`key_findings` ≤ 1000 chars", "Prior step results: 200 chars/step, 2000 total", "GitHub issues page (default 5)", "Self-directed `recent_self_directed` (5 pairs)").

- For each cap stated in CLAUDE.md, grep the code for the relevant module + numeric literal. The fingerprint is `(module, semantic-name, value)`.
- Drift cases:
  - Cap stated in CLAUDE.md but no matching constant or check found in code → **needs-confirmation** (either the cap was removed or never enforced — user must decide which is the bug).
  - Constant in code with a different value than CLAUDE.md states → **needs-confirmation** (do not silently rewrite either side; ask which is correct).
  - Cap enforced by an inline magic number rather than a named constant → report only as **suggestion**; do not rewrite — that is a refactor outside this command's scope.

### 5. `links` — broken cross-doc references

- Scope: `CLAUDE.md`, `README.md`, every file under `docs/`, every file under `docs/proposals/`, and `maestro_review/CLAUDE.md`.
- For every Markdown link `[text](path)` and every inline `path:line` style reference (e.g., `maestro/agent.py:2540`):
  - Resolve the path relative to the containing file. If the target does not exist, this is drift.
  - For `path:line` references where the file exists, do not validate the line number — that is too noisy and changes constantly.
- Drift cases:
  - Link target file is missing → **trivial-fix** if a single obvious rename candidate exists (one file in the repo with the same basename and a closely matching path); otherwise **needs-confirmation**.
  - Link points to a heading anchor (`docs/foo.md#bar`) that is not present in the target file → **needs-confirmation**.

### 6. `literals` — `Literal` type aliases ↔ usage

- Source of truth: `maestro/types.py` (`StageName`, priority/category/status/entry-type/message-type aliases).
- Drift cases:
  - A string literal used in a `==` / `in` comparison against one of these typed fields, where the literal is not in the alias's union → **needs-confirmation** (could be a typo or a missing value).
  - A value present in the alias's union that is not produced or consumed anywhere in `maestro/` (search both production sites and tests) → report as **suggestion** only. Dead enum values are common and removing them is a separate decision.

## Steps

1. **Parse `$ARGUMENTS`.** Determine the active categories and whether `--dry-run` is set. An unknown token: warn the user and proceed with the recognized ones.

2. **Collect findings.** For each active category, perform the search exactly as specified above. Use Grep for textual searches and Read for full-file context before classifying anything as a drift. Do not classify a row as drift on the basis of a fuzzy match alone — confirm with a direct read.

3. **Group and report.** Print findings grouped by category, then by severity:
   - **Drift (trivial-fix)** — what will be auto-applied if `--dry-run` is not set.
   - **Drift (needs-confirmation)** — what requires user approval before editing.
   - **Suggestions** — non-blocking, non-fix observations. Never auto-apply.

   Each finding lists: file `path:line` (or section heading for unanchored facts), the two sides of the drift, and the proposed fix.

4. **Apply trivial fixes.** Unless `--dry-run` is set, apply the **trivial-fix** edits with the Edit tool one at a time. For each edit, the `old_string` must contain enough surrounding context that the change is unambiguous. After each fix, restate it in one line: `fixed: <file>:<rough-loc>`.

5. **Walk the user through `needs-confirmation` items.** For each one, show the two sides and ask the user which is correct. Apply the user's chosen direction immediately, or skip if they decline. Do not ask in batches — one at a time, so the user can answer concretely.

6. **Verify what you changed.** After all edits, re-run the searches for any category whose source files were edited, to confirm the drift is gone and you did not introduce a new inconsistency. Read each edited file once at the end.

7. **Run the cheap quality gates.** If you edited any `.py` file, run `python3.14 -m ruff check <files>` on just the edited paths. If you edited only `.md` / `.toml`, skip — those gates do not apply. Do not run mypy or pytest — this command's edits should be too small to need them, and surfacing unrelated pre-existing failures hurts more than it helps.

## What to skip

- Open-ended code-quality issues — that is `/review-changes`.
- Refactor suggestions (extract constants, rename for clarity, etc.) — out of scope.
- Test-coverage gaps — out of scope.
- Comment / docstring drift inside function bodies — too noisy and rarely actionable.
- Anything in `artifacts/`, `.git/`, `node_modules/`, or generated files.

## Output format

End with a one-section summary:

- `findings: { trivial: N, needs-confirmation: N, suggestions: N }`
- `applied: N` (auto-applied trivial fixes)
- `accepted: N` / `declined: N` (user decisions on needs-confirmation items)
- `skipped categories: …` (any category skipped by `$ARGUMENTS`)
- `--dry-run` if set

If no findings at all, say so plainly in one line and stop.

## Safety

- Never delete a doc file, a code module, or a TOML key as part of this command. The fixes here are textual edits within existing files.
- Never edit `maestro.{env}.toml` overlay files. Real-secret-shaped values in overlays surface as **needs-confirmation** findings and require the user to handle manually; the command never edits overlay files.
- Never run `git commit`, `git push`, or any branch-mutating operation. This command edits the working tree only.
- Never escalate from a **needs-confirmation** finding to an auto-fix without an explicit user approval for that specific finding.
- If a search would touch more than ~50 files (e.g., the `links` scan in a large `docs/` tree), batch-read in groups of ~10 and continue rather than reading them all at once.
- If `$ARGUMENTS` includes `--dry-run`, no Edit tool call may run for any reason.

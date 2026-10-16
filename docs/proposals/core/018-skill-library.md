# 018: Skill Library Distilled from Successful Tasks

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | proposed |
| **Derived from** | Voyager (Minecraft), via [`docs/proposals/research/related-projects-survey.md`](../research/related-projects-survey.md) |
| **Affects** | new `maestro/skills.py`, `maestro/memory.py`, `maestro/agent.py`, `maestro/executor.py`, `maestro/tools.py`, `maestro/config.py`, `CLAUDE.md`, tests |

## Description

Maestro keeps task artifacts (`artifacts/{task_id}/`) and exposes them via
`read_task_history` and `read_task_artifact` MCP tools
(`maestro/tools.py`). That's enough for the agent to go back
and read what happened on a *specific* past task, but nothing distils
cross-task patterns into a named, reusable form. Every new task starts
fresh; the LLM has to re-discover recurring patterns (common shell
invocations, effective prompt shapes, typical file layouts) from raw
artifact text each time.

Voyager's core innovation in the Minecraft agent literature was a
*growing library of reusable skills* — each skill a named, callable
piece of behaviour the agent can reach for by name. Maestro doesn't have
code-level callable skills (the LLM drives tools ad-hoc), but it has the
equivalent abstraction one level up: **a named, searchable library of
"when I had to do X, this worked"**, fed into the Executor's prompt for
new tasks that match.

This is a small, contained change. It reuses the existing distillation
pipeline (`_distill_findings` in `maestro/agent.py`), the existing MCP memory
server scaffolding (`maestro/tools.py`), and the existing JSONL + caps
storage conventions. It does not require embeddings, a vector store, or
any new infrastructure.

## Relevant Findings

### Distillation already happens once per task

`MaestroAgent._distill_findings()` (`maestro/agent.py`) already calls the LLM
at `effort="low"` after each task to produce `TaskRecord.key_findings`.
That's the ideal hook point for a sibling `_distill_skill()` step that
runs only on successful tasks and only when the distiller judges there
*was* a reusable pattern (it's allowed to return "none").

### Nothing reads across task artifacts today

The Executor prompt builder (in `maestro/executor.py`) takes the current
goal and current step and generates a Claude call. Task history is
surfaced only via the MCP `read_task_history` tool on demand — the LLM
has to *know to ask* for it. There is no passive injection of
"historically relevant context for this goal" into the prompt.

### `TaskRecord.key_findings` is bounded

`key_findings` is already capped (LLM-distilled, ≤1000 chars, further
truncated to 150–300 chars per consumer per the CLAUDE.md caps list). A
`Skill` record should use the same shape — bounded, self-contained,
readable at a glance.

### The `memory` MCP server pattern is already established

`create_memory_server()` (`maestro/tools.py`) already wires read-only tools
over `Memory`. Adding a `read_skill_library` tool follows the same
shape — no new server, just one more `@tool` inside the existing server.

## Implementation Plan

### Step 1: Add `Skill` dataclass and storage

In `maestro/memory.py`:

```python
@dataclass
class Skill:
    skill_id: str            # "skill-<slug>"
    name: str                # short noun phrase, ≤60 chars
    when_to_use: str         # single sentence, ≤200 chars
    snippet: str             # code / shell / prompt fragment, ≤500 chars
    tags: list[str]          # keyword tags for matching, ≤8 tags, ≤30 chars each
    source_task_id: str
    created_at: str
    usage_count: int = 0     # incremented when selected into an Executor prompt
```

New paired methods on `Memory`:

```python
def add_skill(self, skill: Skill) -> None: ...
def get_all_skills(self) -> list[Skill]: ...
def increment_skill_usage(self, skill_id: str) -> None: ...
```

Stored at `memory_dir/skill_library.jsonl`. Deduplication on `skill_id`
(if the LLM produces the same slug twice, the second one is dropped with
a warning — simplest correct behaviour).

Caps via module-level constants mirroring `consciousness.py`:

```python
SKILL_MAX_LIBRARY_SIZE: Final = 100
SKILL_NAME_MAX_CHARS: Final = 60
SKILL_WHEN_TO_USE_MAX_CHARS: Final = 200
SKILL_SNIPPET_MAX_CHARS: Final = 500
```

When the library exceeds `SKILL_MAX_LIBRARY_SIZE`, drop the lowest-
`usage_count` skill (ties broken by oldest `created_at`). Logged as a
warning.

### Step 2: New module `maestro/skills.py` for distillation + ranking

```python
class SkillDistiller:
    def __init__(self, config: Config) -> None: ...

    async def distill(
        self, task: TaskRecord, agent_tools_context: str | None = None
    ) -> Skill | None:
        """LLM-distill a Skill from a successful task, or None if no
        reusable pattern is visible."""

class SkillRanker:
    def __init__(self, memory: Memory) -> None: ...

    def top_k_for_goal(self, goal: str, k: int = 3) -> list[Skill]:
        """Keyword-tag overlap ranking. No embeddings."""
```

`SkillDistiller.distill()`:
- `effort="low"`, `max_turns=3`, structured JSON output schema:
  `{name, when_to_use, snippet, tags, is_reusable}`.
- Prompt gives the distiller the task goal, outcome, key_findings, and
  (if available) the first few lines of any artifacts. It explicitly
  says "return `is_reusable: false` if the task was one-off or too
  specific to generalise".
- If `is_reusable: false` → returns `None`, nothing stored.
- If `is_reusable: true` → build `Skill`, return it.
- `caller="distill_skill"` for telemetry.

`SkillRanker.top_k_for_goal()`:
- Tokenise goal (lowercase, split on non-alphanumerics, drop stop-words
  from a small built-in list).
- For each skill, count tag overlaps with goal tokens.
- Sort by (overlap_count desc, usage_count desc, created_at desc).
- Return top `k`, skipping any with zero overlap.

Keyword-tag matching is deliberately dumb. It's good enough for a first
pass, doesn't need an embedding model, and the only way to tell if we
need something smarter is to ship this and see.

### Step 3: Hook distillation into the task post-success path

In `maestro/agent.py`, in the same block that calls `_distill_findings`, add:

```python
if outcome == "success" and self.config.skill_library_enabled:
    try:
        skill = await self._skill_distiller.distill(task_record)
        if skill is not None:
            self.memory.add_skill(skill)
            logger.info("Distilled new skill: %s", skill.name)
    except Exception as exc:
        logger.warning("Skill distillation failed: %s", exc)
```

One `try/except` — distillation failure must never break task completion.

### Step 4: Hook ranking into the Executor prompt

In `maestro/executor.py`, where the prompt is assembled, add a new
section after task history context and before the step body:

```
## Relevant skills from past tasks

{top_k_skills_rendered_here}
```

Populated by `SkillRanker.top_k_for_goal(goal, k=3)`. Each skill rendered
as:

```
• {name}
  When to use: {when_to_use}
  Snippet:
    {snippet}
```

Cap the whole block at 1800 chars (3 × ~600 chars). If empty, omit the
header entirely. For each skill selected, call
`memory.increment_skill_usage(skill_id)` — gives us signal for the
eviction policy and for measuring whether the feature is actually used.

### Step 5: New MCP tool `read_skill_library`

In `maestro/tools.py`, inside `create_memory_server()`:

```python
@tool(
    name="read_skill_library",
    description=(
        "Read your library of distilled skills — patterns and snippets "
        "that worked well on past tasks. Optionally filter by keyword."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "optional filter"},
            "count": {"type": "integer", "default": 10},
        },
    },
)
async def read_skill_library(input: Any) -> dict[str, Any]: ...
```

Returns a rendered text block identical in style to `read_task_history`.

Add to the tool list returned at the bottom of `create_memory_server`.

### Step 6: Config

Add to `[loop]` section of `maestro.toml`:

```toml
skill_library_enabled = true
skill_library_top_k = 3
skill_library_max_size = 100
```

Matching fields on `Config` with defaults. Disabling the flag makes both
the distillation hook (Step 3) and the Executor injection (Step 4)
no-ops.

### Step 7: Update CLAUDE.md

Add a new bullet under "Key Design Decisions":

> **Skill library distilled from successful tasks**: After each
> successful task, `SkillDistiller.distill()` runs at `effort="low"` and,
> if the task produced a reusable pattern, writes a `Skill` record
> (`name`, `when_to_use`, `snippet`, tags) to
> `memory_dir/skill_library.jsonl`. The Executor prompt builder injects
> the top-3 keyword-tag-matched skills for the current goal. Capped at
> 100 skills with lowest-usage-eviction. New `read_skill_library` MCP
> tool on the `memory` server lets tool-using queries browse it
> explicitly. Inspired by Voyager's growing skill library.

Add `Skill` to the `memory.py` and `tools.py` row in the Core Modules
table and add a new `skills.py` row.

## Testing Strategy

1. **`Skill` round-trip** — write 5 skills, read back, verify caps
   enforced on oversized input.
2. **`SkillDistiller.distill()` with `is_reusable: false`** — mock SDK
   to return a rejection JSON; verify `distill()` returns `None` and
   nothing is written.
3. **`SkillDistiller.distill()` happy path** — mock SDK to return a
   valid skill JSON; verify `Skill` object matches and caps are applied.
4. **`SkillRanker.top_k_for_goal()`** — library with 10 skills with
   known tags, query with known goal, verify top-k ordering matches
   hand-computed expected result. Include a zero-overlap case.
5. **Library size cap eviction** — fill library to
   `SKILL_MAX_LIBRARY_SIZE + 1`, verify the lowest-usage skill is
   evicted.
6. **Executor injection** — mock ranker to return 2 skills, build
   executor prompt, assert both appear with the expected format.
7. **MCP tool `read_skill_library`** — standard MCP tool test pattern,
   with and without keyword filter.
8. **Distillation failure is isolated** — make `distill()` raise; verify
   the surrounding task-record write still happens.
9. **Feature flag disabled** — set `skill_library_enabled = false`;
   verify neither distillation nor injection runs.

Run: `python3.12 -m pytest tests/ -v -k "skill"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Distilled skills are low-quality / misleading, polluting Executor context | Medium | Distiller has an explicit `is_reusable: false` escape hatch; cap of 100 skills with usage-based eviction means bad ones drop out over time if never selected |
| Per-task SDK cost increases (extra `effort="low"` call) | Low | `effort="low"` is the cheapest tier; runs only on successful tasks; gated by feature flag |
| Keyword-tag ranking misses relevant skills | Low | It's v1 — easy to replace with embeddings later if/when warranted; meanwhile tool-using queries can still browse the library via `read_skill_library` |
| Library file grows unbounded | Low | Hard cap at `SKILL_MAX_LIBRARY_SIZE`; same JSONL discipline as other state |
| Skills leak sensitive data (paths, tokens) from source tasks | Medium | Distiller prompt explicitly says "redact secrets and absolute paths"; snippet cap at 500 chars limits blast radius; same as existing `key_findings` risk profile |
| Skill `snippet` contains prompt-injection content that hijacks the Executor | Medium | Snippet is injected as *context*, not as an instruction; Executor prompt wraps it in a clearly-marked "reference material" block. Same trust boundary as existing `read_task_artifact` |

## What this proposal does NOT do

- It does **not** introduce embeddings or a vector store. Keyword-tag
  matching is v1 on purpose.
- It does **not** make skills *executable* (no "call skill X" primitive).
  Skills are reference material for the Executor prompt, not callable
  functions. That's a much bigger change and we should ship the
  reference-material version first.
- It does **not** distil skills from *failed* tasks. Failed tasks are
  already learned from via the existing rejection-learning and journal
  paths; adding another learning hook here would double-count.
- It does **not** unify with the existing `read_task_artifact` path.
  Artifacts are raw evidence; skills are distilled patterns. Keeping
  them separate keeps the trust boundaries clean.

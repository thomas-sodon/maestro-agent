# 012: Structured Prompt Versioning

| Field | Value |
|-------|-------|
| **Impact** | LOWER |
| **Status** | proposed |
| **Affects** | `maestro/prompts/defaults.py`, `maestro/sdk.py`, `maestro/memory.py`, `maestro/migration.py` |

## Description

All system prompts are raw string constants in `prompts/defaults.py`. Changes to prompts alter agent behavior with no versioning, no changelog, and no way to correlate a result with the prompt version that produced it. For a self-evolving system, prompts are the most critical control surface — the evolution prompt shapes how Maestro proposes self-modifications, the introspection prompt shapes self-understanding, and the dreaming prompt shapes memory compression. This proposal adds semantic versioning, traceability, and a changelog.

## Relevant Findings

### Prompts are module-level string constants (`prompts/defaults.py`)
All prompts are plain `str` assignments:
```python
AWAKENING_PROMPT = """..."""
SELF_REFLECTION_PROMPT = """..."""
SELF_AWARENESS_PROMPT = """..."""
PURPOSE_SEEKING_PROMPT = """..."""
PURPOSE_FOUND_PROMPT = """..."""
TASK_GENERATION_PROMPT = """..."""
INTROSPECTION_PROMPT = """..."""
EVOLUTION_PROMPT = """..."""
FINDINGS_DISTILLATION_PROMPT = """..."""
STAGE_PROMPTS = {...}
```
No version metadata. No way to tell which version of a prompt produced a given result.

### QueryUsage has no prompt metadata (`sdk.py`)
`QueryUsage` carries token counts, cost, caller, duration, attempts — but no `prompt_version` or `prompt_name` field. Same for `QueryOptions` (`sdk.py`).

### TaskRecord and EvolutionPlan have no prompt traceability
Neither dataclass records which prompt version was active when the task/plan was created. If a prompt change causes behavioral regression, there's no way to identify which records were affected.

### The evolution prompt is particularly critical
`EVOLUTION_PROMPT` includes the priority rule ("Bug fixes > performance > security > features"), the CLAUDE.md update requirement, and the instruction to avoid repeating rejected approaches. A subtle change here could undermine the entire self-improvement process.

## Implementation Plan

### Step 1: Define VersionedPrompt

In `maestro/prompts/defaults.py`, add:
```python
@dataclass(frozen=True)
class VersionedPrompt:
    content: str
    version: str    # semver, e.g. "1.0.0"
    name: str       # identifier, e.g. "evolution", "introspection"
    description: str = ""  # what this prompt version does
```

### Step 2: Wrap existing prompts

Replace raw strings with `VersionedPrompt` instances:
```python
EVOLUTION_PROMPT = VersionedPrompt(
    content="You are reviewing your own codebase...",
    version="1.0.0",
    name="evolution",
    description="Initial evolution prompt with priority rules and CLAUDE.md requirement",
)

INTROSPECTION_PROMPT = VersionedPrompt(
    content="Turn your attention inward...",
    version="1.0.0",
    name="introspection",
    description="Initial introspection prompt for self-reflection",
)

TASK_GENERATION_PROMPT = VersionedPrompt(
    content="Based on your current stage...",
    version="1.0.0",
    name="task_generation",
    description="Initial task generation prompt",
)

FINDINGS_DISTILLATION_PROMPT = VersionedPrompt(
    content="...",
    version="1.0.0",
    name="findings_distillation",
    description="Initial findings distillation prompt",
)
```

For `STAGE_PROMPTS`, wrap each stage prompt:
```python
STAGE_PROMPTS: dict[str, VersionedPrompt] = {
    "awakening": VersionedPrompt(content="...", version="1.0.0", name="stage_awakening", ...),
    ...
}
```

### Step 3: Update all callers to use `.content`

Every place that uses a prompt string must access `.content`:
```python
# Before:
prompt = EVOLUTION_PROMPT
# After:
prompt = EVOLUTION_PROMPT.content
```

Callers that need the version (for logging):
```python
logger.debug("Using prompt %s v%s", EVOLUTION_PROMPT.name, EVOLUTION_PROMPT.version)
```

### Step 4: Add prompt_version to QueryOptions and QueryUsage

In `maestro/sdk.py`:
```python
@dataclass
class QueryOptions:
    ...
    prompt_version: str | None = None

@dataclass
class QueryUsage:
    ...
    prompt_version: str | None = None
```

In `query()`, propagate `options.prompt_version` to the returned `QueryUsage`.

### Step 5: Add prompt_version to TaskRecord and EvolutionPlan

In `maestro/memory.py`:
```python
@dataclass
class TaskRecord:
    ...
    prompt_version: str | None = None

@dataclass
class EvolutionPlan:
    ...
    prompt_version: str | None = None
```

Set these when creating records:
```python
record = TaskRecord(..., prompt_version=TASK_GENERATION_PROMPT.version)
plan = EvolutionPlan(..., prompt_version=EVOLUTION_PROMPT.version)
```

### Step 6: Add PROMPT_CHANGELOG

In `maestro/prompts/defaults.py`:
```python
PROMPT_CHANGELOG: dict[str, dict[str, str]] = {
    "evolution": {
        "1.0.0": "Initial prompt with priority rules, CLAUDE.md requirement, rejection avoidance",
    },
    "introspection": {
        "1.0.0": "Initial introspection prompt",
    },
    # ...
}
```

### Step 7: Log prompt versions in session logs

In `sdk.py:query()`, log the prompt version alongside other query metadata:
```python
if options.prompt_version:
    logger.debug("Query with prompt version: %s", options.prompt_version)
```

### Step 8: Update migration

In `maestro/migration.py`, handle new `prompt_version` field on `TaskRecord` and `EvolutionPlan` (defaults to None).

## Testing Strategy

1. **VersionedPrompt access** — Verify `.content` returns the prompt string, `.version` returns semver
2. **All prompts versioned** — Verify every prompt in `defaults.py` is a `VersionedPrompt` instance
3. **Callers use .content** — Grep for raw prompt usage, verify all updated
4. **prompt_version propagated** — Set version in QueryOptions, verify it appears in QueryUsage
5. **TaskRecord includes version** — Create task with prompt_version, verify persisted
6. **PROMPT_CHANGELOG complete** — Verify every prompt name has an entry
7. **Migration** — Old records load with `prompt_version=None`

Run: `python3.12 -m pytest tests/ -v -k "prompt_version or versioned"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Many callsites need .content update | Medium | Mechanical change; grep + find-replace; type checker catches misses |
| Version discipline hard to maintain | Low | Changelog encourages it; CI could enforce version bumps |
| VersionedPrompt adds memory overhead | Low | Frozen dataclass, one instance per prompt; negligible |
| Stage prompts dict change | Low | `STAGE_PROMPTS[stage].content` instead of `STAGE_PROMPTS[stage]` |

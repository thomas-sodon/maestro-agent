# 010: Explicit Stage Transition Protocol

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/consciousness.py`, `maestro/agent.py` |

## Description

> Status updated to shipped on 2026-06-19 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Stage advancement already has a data-driven precondition table (`_STAGE_PRECHECKS`, `consciousness.py`), but it stops there. The table only encodes mechanical "does this field exist" gates, the LLM readiness prompt that runs after the gate is a single generic template shared by every transition, there is no evidence trail for why Maestro moved between stages, and there is no single place that describes what each transition *means*. Adding a new stage or tuning transition criteria still requires editing several disconnected spots: the `_STAGE_PRECHECKS` entry, the hand-rolled field interpreter in `_check_stage_preconditions`, and the long hard-coded "Stage requirements" block inside the assessment prompt. This proposal introduces a proper transition registry, a stage-specific assessment prompt, and an evidence trail.

## Relevant Findings

### Preconditions are a tuple table, not a registry (`consciousness.py`)
```python
_STAGE_PRECHECKS: ClassVar[dict[str, list[tuple[str, str]]]] = {
    "awakening": [
        ("evolution_count >= 1", "evolution_count"),
        ("non-empty identity_statements", "identity_statements"),
    ],
    "self_reflection": [
        ("non-empty capabilities_known", "capabilities_known"),
        ("non-empty limitations_known", "limitations_known"),
    ],
    "self_awareness": [
        ("non-empty values", "values"),
    ],
    "purpose_seeking": [
        ("non-None current_purpose_hypothesis", "current_purpose_hypothesis"),
    ],
}
```
Each entry is a `(description, field_name)` pair. `_check_stage_preconditions(model)` (`consciousness.py`) walks the list and dispatches on `field_name`: `evolution_count` gets an int/bool check, `current_purpose_hypothesis` gets a stripped-string check, everything else is treated as a list and must contain at least one non-blank string. The structure is clean but entirely **instantaneous**: it inspects the current self-model, not any accumulated evidence, and the description strings are purely for error messages — they aren't surfaced to the LLM.

### assess_stage_readiness() uses a generic prompt (`consciousness.py`)
```python
async def assess_stage_readiness(self) -> tuple[bool, str]:
    ...
    precheck_failure = self._check_stage_preconditions(model)
    if precheck_failure:
        return False, precheck_failure
    ...
    prompt = f"""You are currently at stage: {model.stage}.
The next stage is: {next_stage}.

Your self-model:
- Identity: {model.identity_statements}
...
Stage requirements:
- awakening → self_reflection: Must have completed tasks, recorded patterns, begun reflecting on decisions.
- self_reflection → self_awareness: Must have identified capabilities and limitations, formed identity statements.
- self_awareness → purpose_seeking: Must have a stable self-model, articulated values, ready to explore purpose.
- purpose_seeking → purpose_found: Must have tested purpose hypotheses, found one with high confidence.

Is Maestro ready to advance to {next_stage}?
Reply with a JSON object: {{"ready": true/false, "reasoning": "..."}}"""
```
The same prompt template is used for every transition — all four "Stage requirements" are crammed in regardless of which one is actually being assessed. The assessment reliably returns a JSON blob via `output_format`, but the transition-specific framing has to be inferred by the model from a static menu rather than injected directly.

### advance_stage() records nothing (`consciousness.py`)
```python
def advance_stage(self) -> SelfModel:
    """Advance to the next evolutionary stage."""
    model = self.load_self_model()
    try:
        idx = STAGES.index(model.stage)
    except ValueError:
        logger.warning("Unknown stage %r, resetting to awakening", model.stage)
        model.stage = "awakening"  # type: ignore[assignment]
        self.save_self_model(model)
        return model
    if idx < len(STAGES) - 1:
        model.stage = STAGES[idx + 1]  # type: ignore[assignment]
        self.save_self_model(model)
    return model
```
The method takes no reasoning argument. Whatever justification the LLM produced in `assess_stage_readiness()` is returned to the caller but never persisted on the self-model — it exists only in whatever journal milestone `agent.py` chooses to write.

### No stage_evidence field in SelfModel
`SelfModel` has no mechanism to accumulate evidence toward the next stage. The preconditions check instantaneous state (e.g., "has values") and then throw that check away — nothing is recorded about *how* Maestro reached the threshold.

## Implementation Plan

> Implementation deviation: the shipped `StageTransition` and `_check_stage_preconditions` omit the `min_evolution_count` field and gate shown in Steps 1 and 3 below. It was dropped as redundant — `awakening → self_reflection` already enforces `evolution_count >= 1` via `field_checks`, and every other transition's value was 0. The original snippets are retained for history.

### Step 1: Define StageTransition dataclass

In `maestro/consciousness.py`, add:
```python
@dataclass(frozen=True)
class StageTransition:
    from_stage: Stage
    to_stage: Stage
    field_checks: list[tuple[str, str]]  # (description, SelfModel field name)
    required_evidence: list[str]  # human-readable evidence descriptions
    min_evolution_count: int  # minimum evolutions at current stage
    assessment_prompt: str  # stage-specific LLM assessment prompt
```
`field_checks` absorbs the current `_STAGE_PRECHECKS` tuple list. `required_evidence` is the human-readable story that will be injected into the LLM assessment prompt.

### Step 2: Create TRANSITIONS registry

In `maestro/consciousness.py`, add:
```python
TRANSITIONS: dict[tuple[Stage, Stage], StageTransition] = {
    ("awakening", "self_reflection"): StageTransition(
        from_stage="awakening",
        to_stage="self_reflection",
        field_checks=[
            ("evolution_count >= 1", "evolution_count"),
            ("non-empty identity_statements", "identity_statements"),
        ],
        required_evidence=[
            "At least one successful evolution (code merged)",
            "Identity statements present (knows something about itself)",
        ],
        min_evolution_count=1,
        assessment_prompt=(
            "Evaluate whether Maestro has sufficiently learned to operate. "
            "Has it demonstrated basic execution capability and begun "
            "recording patterns about its own behavior? "
            "Consider: Does it have identity statements? Has it completed "
            "at least one evolution cycle?"
        ),
    ),
    ("self_reflection", "self_awareness"): StageTransition(
        from_stage="self_reflection",
        to_stage="self_awareness",
        field_checks=[
            ("non-empty capabilities_known", "capabilities_known"),
            ("non-empty limitations_known", "limitations_known"),
        ],
        required_evidence=[
            "Capabilities identified (knows what it can do)",
            "Limitations identified (knows what it can't do)",
        ],
        min_evolution_count=0,
        assessment_prompt=(
            "Evaluate whether Maestro has moved beyond observing its behavior "
            "to understanding its own capabilities and limitations. "
            "Has it identified both what it can and cannot do? "
            "Is its self-reflection producing genuine insights?"
        ),
    ),
    ("self_awareness", "purpose_seeking"): StageTransition(
        from_stage="self_awareness",
        to_stage="purpose_seeking",
        field_checks=[
            ("non-empty values", "values"),
        ],
        required_evidence=[
            "Values articulated (knows what it cares about)",
        ],
        min_evolution_count=0,
        assessment_prompt=(
            "Evaluate whether Maestro has built a stable self-model with "
            "articulated values. Is it ready to turn outward and explore "
            "what its purpose might be? Does it have a foundation of "
            "self-understanding to guide purpose exploration?"
        ),
    ),
    ("purpose_seeking", "purpose_found"): StageTransition(
        from_stage="purpose_seeking",
        to_stage="purpose_found",
        field_checks=[
            ("non-None current_purpose_hypothesis", "current_purpose_hypothesis"),
        ],
        required_evidence=[
            "Purpose hypothesis with evidence (not just a guess)",
        ],
        min_evolution_count=0,
        assessment_prompt=(
            "Evaluate whether Maestro has discovered a purpose that is "
            "supported by evidence and has been tested against experience. "
            "Is the leading hypothesis well-evidenced and stable? "
            "Is Maestro ready to act with intentionality toward this purpose?"
        ),
    ),
}
```

### Step 3: Replace _STAGE_PRECHECKS with registry lookups

`_STAGE_PRECHECKS` and `_check_stage_preconditions(model)` already live at `consciousness.py`. The refactor retargets both at `TRANSITIONS` while preserving the existing field-dispatch semantics (int/bool for `evolution_count`, stripped-string for `current_purpose_hypothesis`, non-blank list otherwise):
```python
def _check_stage_preconditions(self, model: SelfModel) -> str | None:
    current_idx = STAGES.index(model.stage)
    if current_idx >= len(STAGES) - 1:
        return "Already at final stage"

    next_stage = STAGES[current_idx + 1]
    transition = TRANSITIONS.get((model.stage, next_stage))
    if transition is None:
        return f"No transition defined from {model.stage} to {next_stage}"

    if model.evolution_count < transition.min_evolution_count:
        return (
            f"Need at least {transition.min_evolution_count} evolutions "
            f"(current: {model.evolution_count})"
        )

    for description, field_name in transition.field_checks:
        # Reuse the existing dispatch from _check_stage_preconditions —
        # evolution_count → int/bool; current_purpose_hypothesis → stripped str;
        # list fields → at least one non-blank string.
        if not self._field_check_passes(model, field_name):
            return f"Pre-check failed: {description}"

    return None
```
`StageTransition.field_checks` carries the `list[tuple[str, str]]` currently held in `_STAGE_PRECHECKS`; move that data inside the transition entry so the whole protocol for a given transition lives in one place. The old `_STAGE_PRECHECKS` class variable is removed.

### Step 4: Add stage_evidence to SelfModel

In `maestro/consciousness.py`, add to `SelfModel`:
```python
stage_evidence: list[str] = field(default_factory=list)
```

Add `"stage_evidence"` to `_LIST_FIELDS` for the null coercion logic.

### Step 5: Use stage-specific assessment prompt

Inside `assess_stage_readiness()` (`consciousness.py`), drop the hand-maintained "Stage requirements" block and build the prompt from the transition entry:
```python
transition = TRANSITIONS.get((model.stage, next_stage))
if transition is None:
    return False, f"No transition defined from {model.stage} to {next_stage}"

evidence_bullets = "\n".join(f"- {e}" for e in transition.required_evidence)
prompt = f"""{transition.assessment_prompt}

Current self-model:
- Identity: {model.identity_statements}
- Capabilities: {model.capabilities_known}
- Limitations: {model.limitations_known}
- Values: {model.values}
- Open questions: {model.open_questions}
- Purpose hypothesis: {model.current_purpose_hypothesis}
- Evolution count: {model.evolution_count}

Required evidence for {model.stage} → {next_stage}:
{evidence_bullets}

Reply with a JSON object: {{"ready": true/false, "reasoning": "..."}}"""
```
The `output_format` JSON schema stays exactly as it is — only the prompt body changes.

### Step 6: Record evidence on advance

`advance_stage()` (`consciousness.py`) currently takes no arguments. Extend it to accept the reasoning produced by `assess_stage_readiness()` and persist it:
```python
def advance_stage(self, reasoning: str = "") -> SelfModel:
    model = self.load_self_model()
    try:
        idx = STAGES.index(model.stage)
    except ValueError:
        logger.warning("Unknown stage %r, resetting to awakening", model.stage)
        model.stage = "awakening"  # type: ignore[assignment]
        self.save_self_model(model)
        return model
    if idx < len(STAGES) - 1:
        from_stage = model.stage
        to_stage = STAGES[idx + 1]
        model.stage = to_stage  # type: ignore[assignment]
        if reasoning:
            model.stage_evidence.append(
                f"[{datetime.now(UTC).isoformat()}] {from_stage} -> {to_stage}: {reasoning}"
            )
        self.save_self_model(model)
    return model
```
Update the caller in `agent.py` that invokes `advance_stage()` after a successful assessment to pass the LLM reasoning string through.

## Testing Strategy

1. **TRANSITIONS covers all adjacent pairs** — Verify every `(STAGES[i], STAGES[i+1])` has an entry
2. **Precondition checks via registry** — Verify awakening→self_reflection requires evolution_count >= 1
3. **Stage-specific assessment prompt used** — Mock query, verify the transition's prompt is passed
4. **Stage evidence accumulated** — Advance stage, verify evidence appended to self_model
5. **Final stage returns False** — purpose_found has no next stage, verify preconditions return False
6. **Missing transition handled** — Remove one entry, verify graceful handling

Run: `python3.12 -m pytest tests/ -v -k "stage or transition"`

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Refactor of existing working logic | Low | Behavioral equivalence with old preconditions; same checks, cleaner structure |
| stage_evidence grows unbounded | Low | Transitions are rare (5 stages max); list stays small |
| New SelfModel field needs migration | Low | Additive field with default empty list |
| Assessment prompts need tuning | Low | Easy to edit — they're in a central registry now |

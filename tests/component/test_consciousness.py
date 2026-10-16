"""Component tests for consciousness."""

from __future__ import annotations

import json

import pytest

from maestro.consciousness import Consciousness
from maestro.journal import Journal
from maestro.memory import Memory
from tests.component.conftest import write_self_model
from tests.component.sdk_mock import SDKMock

# ---- Novelty detection ----


@pytest.mark.asyncio
async def test_update_self_model_detects_novelty(memory_dir):
    write_self_model(memory_dir,
                     capabilities_known=["task planning"],
                     limitations_known=["slow"])
    consciousness = Consciousness(memory_dir)
    journal = Journal(memory_dir)
    sdk = SDKMock()
    sdk.then(json.dumps({
        "capabilities_known": ["task planning", "code generation"],
        "limitations_known": ["slow", "sometimes imprecise"],
    }))
    with sdk.patch("maestro.consciousness"):
        model = await consciousness.update_self_model(
            "I realized I can generate code", journal=journal,
        )
    sdk.assert_all_consumed()
    assert model.surprise_count >= 1
    entries = [json.loads(line) for line in
               (memory_dir / "events.jsonl").read_text().strip().splitlines()]
    realization_entries = [e for e in entries if e["entry_type"] == "realization"]
    assert len(realization_entries) >= 1
    assert "genuinely new" in realization_entries[0]["content"].lower()


# ---- Growth tests ----


@pytest.mark.asyncio
async def test_self_model_persists_across_instances(memory_dir):
    write_self_model(memory_dir, values=["honesty"])
    c1 = Consciousness(memory_dir)
    model = c1.load_self_model()
    model.values.append("curiosity")
    c1.save_self_model(model)
    c2 = Consciousness(memory_dir)
    model2 = c2.load_self_model()
    assert "curiosity" in model2.values
    assert "honesty" in model2.values


# ---- Audit self-extinguishing round-trip ----


@pytest.mark.asyncio
async def test_audit_self_extinguishes_after_integration(memory_dir):
    """The mechanical audit rule stops firing once Claude integrates the finding.

    This is the end-to-end validation that rules are self-extinguishing:

    1. Seed tasks so rule 1 (``success_claim_vs_rate``) fires — effectiveness
       claim + low success rate.
    2. Run the audit → confirm a ``contradicted_claim`` finding appears in
       the event record as ``audit_observation`` (not ``realization``), an
       ``[audit]`` entry is added to ``open_questions``, and an
       ``AuditResult`` row lands on disk.
    3. Simulate Claude's next contemplation integrating the finding: drop
       the effective-planning capability, add the corresponding limitation,
       and remove the ``[audit]`` open-question.
    4. Re-run the audit → the rule no longer fires (zero findings,
       zero new ``audit_observation`` entries).
    """
    from maestro.audit_checks import run_all_checks
    from maestro.memory import AuditResult, PlanStep, TaskRecord

    consciousness = Consciousness(memory_dir)
    memory = Memory(memory_dir)
    journal = Journal(memory_dir)

    # Step 1 — seed a self-model that claims effectiveness + failing tasks.
    model = consciousness.load_self_model()
    model.capabilities_known = ["effective planning"]
    consciousness.save_self_model(model)

    def _seed_task(i: int, outcome: str) -> None:
        memory.record_task(TaskRecord(
            task_id=f"t{i}",
            # Use a goal referencing a self-module so the prediction audit
            # (self_referential) stays silent and doesn't interfere with the
            # rule-1 test.
            goal="measure maestro/consciousness.py coverage",
            plan=[PlanStep(step_id="s1", description="x", status="done")],
            outcome=outcome,  # type: ignore[arg-type]
            result_summary="",
            errors=[],
            duration_secs=0.5,
            timestamp="2025-01-01T00:00:00Z",
        ))

    for i in range(8):
        _seed_task(i, "failure")
    for i in range(8, 10):
        _seed_task(i, "success")

    # Step 2 — manual audit cycle (equivalent to _audit_self_model body).
    import datetime as _dt

    tasks = memory.get_recent_tasks(20)
    rate = memory.get_success_rate(20)
    findings = run_all_checks(consciousness.load_self_model(), tasks, rate)
    assert len(findings) == 1
    assert findings[0].rule == "success_claim_vs_rate"

    loaded = consciousness.load_self_model()
    for finding in findings:
        cite_suffix = (
            f" — cites: {', '.join(finding.cites)}" if finding.cites else ""
        )
        journal.record_event(
            content=f"[{finding.kind}] {finding.statement}{cite_suffix}",
            entry_type="audit_observation",
            triggered_by="audit_self_model",
        )
        loaded.open_questions.append(
            f"[audit] {finding.kind}: {finding.statement[:160]}"
        )
    consciousness.save_self_model(loaded)
    memory.append_audit_result(AuditResult(
        iteration=1,
        timestamp=_dt.datetime.now(_dt.UTC).isoformat(),
        findings=list(findings),
        rules_fired=sorted({f.rule for f in findings}),
    ))

    # Provenance is an ``audit_observation`` entry (NOT ``realization``).
    audit_entries = journal.read_events_by_type("audit_observation")
    realizations = journal.read_events_by_type("realization")
    assert len(audit_entries) == 1
    assert realizations == []

    model_after = consciousness.load_self_model()
    audit_qs = [q for q in model_after.open_questions if q.startswith("[audit")]
    assert len(audit_qs) == 1

    persisted = memory.get_most_recent_audit()
    assert persisted is not None
    assert len(persisted.findings) == 1

    # Step 3 — simulate INTEGRATE: Claude moves the finding into
    # limitations_known, drops the faulty capability, and removes the
    # ``[audit]`` open-question.
    model_after.capabilities_known = []
    model_after.limitations_known = [
        "over-claims effectiveness given current success rate",
    ]
    model_after.open_questions = [
        q for q in model_after.open_questions if not q.startswith("[audit")
    ]
    consciousness.save_self_model(model_after)

    # Step 4 — re-run the audit. Rule 1's condition (effectiveness claim)
    # no longer holds, so no finding is produced.
    tasks = memory.get_recent_tasks(20)
    rate = memory.get_success_rate(20)
    findings2 = run_all_checks(consciousness.load_self_model(), tasks, rate)
    assert findings2 == []

    # No new audit_observation entry, no new [audit] question.
    audit_entries2 = journal.read_events_by_type("audit_observation")
    assert len(audit_entries2) == 1  # unchanged from step 2
    model_final = consciousness.load_self_model()
    assert not any(
        q.startswith("[audit") for q in model_final.open_questions
    )

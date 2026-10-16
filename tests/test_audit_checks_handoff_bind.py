"""Regression tests for #4078 — the green-handoff delivery-truth bind.

:func:`maestro.audit_checks_phantom.confirm_handoff_delivery` must report 'delivered'
only when a ``stand_down_verdicts.jsonl`` verdict row appeared *during* this
fire; a stale row left by an earlier act for the same PR must not satisfy it.
Split out of ``tests/test_audit_checks.py``, whose size is pinned shrink-only.
"""

from __future__ import annotations

import json
from pathlib import Path

from maestro import audit_checks_phantom
from maestro.remediation_tracker import (
    JsonlStandDownVerdictLedger,
    release_stand_downs,
)
from maestro.standdown_validity import StanddownClaim

_GREEN_READY_DIR = Path("/fake/green_ready_handoff_bind")


def _stand_down_rows(mem_fs: dict[str, str], data_dir: Path) -> list[dict]:
    """Parse every JSONL row in *data_dir*'s ``stand_down_verdicts.jsonl``."""
    raw = mem_fs.get(str(data_dir / "stand_down_verdicts.jsonl"))
    if raw is None:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def test_confirm_handoff_delivery_backfills_despite_stale_prior_row(
    mem_fs: dict[str, str],
) -> None:
    """#4078 — a verdict row left by an *earlier* act for the same PR must not
    satisfy the bind: an actuator that writes nothing this fire still triggers a
    backfill, and the bind reports False rather than 'delivered'."""
    backend = JsonlStandDownVerdictLedger(_GREEN_READY_DIR)
    audit_checks_phantom._persist_handoff_stand_down_row(3181, backend, now="t-old")

    delivered = audit_checks_phantom.confirm_handoff_delivery(
        backend, 3181, lambda: True, now="t-new",
    )

    assert delivered is False
    rows = [r for r in _stand_down_rows(mem_fs, _GREEN_READY_DIR) if r["pr"] == 3181]
    assert len(rows) == 2
    assert rows[-1]["recorded_at"] == "t-new"


def test_confirm_handoff_delivery_backfills_despite_released_prior_row(
    mem_fs: dict[str, str],
) -> None:
    """#4078 — a prior verdict since *released* is equally stale: this fire still
    backfills its own row and reports False."""
    backend = JsonlStandDownVerdictLedger(_GREEN_READY_DIR)
    audit_checks_phantom._persist_handoff_stand_down_row(3181, backend, now="t-old")
    release_stand_downs(3181, backend, now="t-release")

    delivered = audit_checks_phantom.confirm_handoff_delivery(
        backend, 3181, lambda: True, now="t-new",
    )

    assert delivered is False
    verdicts = [
        r for r in _stand_down_rows(mem_fs, _GREEN_READY_DIR)
        if r["pr"] == 3181 and "recorded_at" in r
    ]
    assert len(verdicts) == 2
    assert verdicts[-1]["recorded_at"] == "t-new"


def test_confirm_handoff_delivery_true_when_actuator_writes_despite_stale_row(
    mem_fs: dict[str, str],
) -> None:
    """#4078 — with a stale prior row present, an actuator that writes its own row
    this fire still reads as 'delivered' (count grew 1 → 2) and no backfill runs."""
    backend = JsonlStandDownVerdictLedger(_GREEN_READY_DIR)
    audit_checks_phantom._persist_handoff_stand_down_row(3181, backend, now="t-old")

    def _deliver() -> bool:
        audit_checks_phantom._persist_handoff_stand_down_row(3181, backend, now="t-act")
        return True

    delivered = audit_checks_phantom.confirm_handoff_delivery(
        backend, 3181, _deliver, now="t-new",
    )

    assert delivered is True
    rows = [r for r in _stand_down_rows(mem_fs, _GREEN_READY_DIR) if r["pr"] == 3181]
    assert len(rows) == 2
    assert rows[-1]["recorded_at"] == "t-act"


def test_green_ready_handoff_deliver_then_release(
    mem_fs: dict[str, str],
) -> None:
    """Criterion 5 — a green-ready handoff records the durable pr_number-keyed
    standdown via the existing ``deliver_handoff`` actuator (idempotent: a
    second call is a no-op), and it is released on merge/close by the existing
    ``prune_resolved_standdowns`` path — no new release code."""
    from maestro.escalation_bridge import (
        JsonEscalationLedger,
        UnlandedCureHandoff,
        deliver_handoff,
        has_standdown,
        prune_resolved_standdowns,
    )
    from maestro.messaging import MessageStore

    store = MessageStore(_GREEN_READY_DIR)
    backend = JsonEscalationLedger(_GREEN_READY_DIR)
    handoff = UnlandedCureHandoff(pr_number=3181, head_sha="", stamp_count=1)

    # First dispatch delivers exactly one message + records the standdown.
    assert deliver_handoff(
        _GREEN_READY_DIR, handoff, backend=backend, message_store=store,
        claim=StanddownClaim.HANDOFF_READY,
    ) is True
    assert has_standdown(backend, 3181) is True
    # Self-extinguishing: the rule now filters #3181 out (has_standdown True),
    # and even a direct re-dispatch is an idempotent no-op (no second message).
    assert audit_checks_phantom.green_ready_handoff_prs(
        [3181],
        enabled=True,
        is_green_ready=lambda n: True,
        has_standdown=lambda n: has_standdown(backend, n),
    ) == []
    assert deliver_handoff(
        _GREEN_READY_DIR, handoff, backend=backend, message_store=store,
        claim=StanddownClaim.HANDOFF_READY,
    ) is False
    assert len(store.get_recent(10)) == 1

    # Merge/close releases the standdown via the existing pruner (no new code).
    cleared = prune_resolved_standdowns(backend, lambda n: n == 3181)
    assert cleared == [3181]
    assert has_standdown(backend, 3181) is False

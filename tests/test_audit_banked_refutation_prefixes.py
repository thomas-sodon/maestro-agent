"""Banked-owned-act recognition of the refuted-release message families.

Proposal 135 registers one of the two new message-id shapes in
``audit_checks._HANDOFF_MESSAGE_ID_PREFIXES`` and deliberately leaves the other
out; this module pins that asymmetry. It lives beside ``test_audit_checks.py``
rather than inside it because that module sits near the SPLIT_BUDGET held by
``tests/test_source_file_size.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from maestro import audit_checks
from maestro.audit_checks import check_banked_owned_act_reperformance
from maestro.remediation_tracker import JsonlStandDownVerdictLedger

_BANKED_DIR = Path("/fake/banked-refutation")


def _seed_banked_messages(
    mem_fs: dict[str, str], records: list[dict[str, object]],
) -> None:
    """Seed ``messages.jsonl`` with raw message *records* (one line each)."""
    mem_fs[str(_BANKED_DIR / "messages.jsonl")] = "".join(
        json.dumps(r) + "\n" for r in records
    )


def test_banked_reperformance_fires_on_refutation_capped_message_id(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 135: the refutation release-cap terminal notice is a genuine
    durable merge-or-close escalation, so the banked reader recognises it and a
    directive re-performing it fires. Guards the registration."""
    capped_id = "standdown-refutation-capped-3112"
    _seed_banked_messages(mem_fs, [
        {"message_id": capped_id, "sender": "maestro",
         "content": "stand-down is now permanent", "message_type": "question"},
    ])
    assert capped_id in audit_checks._banked_handoff_message_ids(_BANKED_DIR)
    findings = check_banked_owned_act_reperformance(
        _BANKED_DIR, f"re-deliver {capped_id}", [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    )
    assert len(findings) == 1
    assert capped_id in findings[0].statement


def test_banked_reperformance_ignores_refutation_withdrawal_message_id(
    mem_fs: dict[str, str],
) -> None:
    """Proposal 135: the withdrawal prefix is deliberately **not** registered.

    A withdrawal is the *un*-banking of an owned act — the loop taking back a
    stand-down it measured to be false — so banking it would invert this rule's
    meaning, letting a retracted handoff suppress the very remediation the
    retraction promised to resume. Pinned so the distinction cannot drift.
    """
    withdrawal_id = "standdown-withdrawn-3112-1"
    _seed_banked_messages(mem_fs, [
        {"message_id": withdrawal_id, "sender": "maestro",
         "content": "withdrawing my stand-down", "message_type": "question"},
    ])
    assert withdrawal_id not in audit_checks._banked_handoff_message_ids(
        _BANKED_DIR,
    )
    assert check_banked_owned_act_reperformance(
        _BANKED_DIR, f"re-deliver {withdrawal_id}", [], enabled=True,
        stand_down_backend=JsonlStandDownVerdictLedger(_BANKED_DIR),
    ) == []

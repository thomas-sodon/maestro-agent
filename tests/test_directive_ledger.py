"""Tests for the dropped fire-or-close directive ledger (proposal 090).

All I/O runs through the ``mem_fs`` fixture — no real filesystem — and the git
liveness oracles are injected as plain callables, so no subprocess/network/sleep
is ever touched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from maestro.consciousness import SelfModel
from maestro.directive_ledger import (
    _MAX_ENTRIES,
    DirectiveGrounding,
    DroppedDirective,
    DroppedDirectiveBackend,
    JsonlDroppedDirectiveLedger,
    load_all_dropped_directives,
    offer_directive,
    replay_dropped_directive,
)
from maestro.state_store import SqliteDroppedDirectiveLedger, StateStore

_DIR = Path("/fake/dl")
_LEDGER = "/fake/dl/dropped_directives.jsonl"

# A far-future ``now`` never trips the age cap for freshly-stamped rows.
_NOW = "2026-07-09T00:00:00+00:00"


def _rows(mem_fs: dict[str, str]) -> list[dict[str, object]]:
    """Parse the ledger rows currently in *mem_fs* (oldest-first)."""
    raw = mem_fs.get(_LEDGER)
    if not raw:
        return []
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _seed(mem_fs: dict[str, str], *rows: dict[str, object]) -> None:
    """Write *rows* verbatim into the ledger file in *mem_fs*."""
    mem_fs[_LEDGER] = "".join(json.dumps(r) + "\n" for r in rows)


def _entry(
    text: str,
    *,
    pr_numbers: list[int] | None = None,
    shas: list[str] | None = None,
    recorded_at: str = _NOW,
    source: str = "test",
) -> dict[str, object]:
    return {
        "text": text,
        "source": source,
        "pr_numbers": pr_numbers or [],
        "shas": shas or [],
        "recorded_at": recorded_at,
    }


def _be() -> JsonlDroppedDirectiveLedger:
    """The default JSONL backend bound to the fake data dir (over ``mem_fs``)."""
    return JsonlDroppedDirectiveLedger(_DIR)


# ---------------------------------------------------------------------------
# offer_directive — fill semantics
# ---------------------------------------------------------------------------


def test_offer_fills_empty_slot_and_writes_nothing_flag_off(
    mem_fs: dict[str, str],
) -> None:
    model = SelfModel()
    filled = offer_directive(
        model,
        "land #7 or close it",
        source="diagnostic",
        grounding=DirectiveGrounding(pr_numbers=(7,)),
        backend=_be(),
        enabled=False,
    )
    assert filled is True
    assert model.next_task_directive == "land #7 or close it"
    assert mem_fs.get(_LEDGER) is None


def test_offer_fills_empty_slot_and_writes_nothing_flag_on(
    mem_fs: dict[str, str],
) -> None:
    """Even with the flag on, an empty slot is filled — never ledgered."""
    model = SelfModel()
    filled = offer_directive(
        model,
        "land #7 or close it",
        source="diagnostic",
        grounding=DirectiveGrounding(pr_numbers=(7,)),
        backend=_be(),
        enabled=True,
    )
    assert filled is True
    assert model.next_task_directive == "land #7 or close it"
    assert mem_fs.get(_LEDGER) is None


def test_offer_does_not_save_model(mem_fs: dict[str, str]) -> None:
    """The caller persists; offer_directive only mutates the in-memory field."""
    model = SelfModel()
    offer_directive(
        model,
        "land #7 or close it",
        source="diagnostic",
        grounding=DirectiveGrounding(pr_numbers=(7,)),
        backend=_be(),
        enabled=True,
    )
    # No self_model.json was written by the ledger.
    assert not any(k.endswith("self_model.json") for k in mem_fs)


# ---------------------------------------------------------------------------
# offer_directive — occupied slot
# ---------------------------------------------------------------------------


def test_offer_occupied_slot_flag_off_is_noop(mem_fs: dict[str, str]) -> None:
    model = SelfModel(next_task_directive="pre-set")
    filled = offer_directive(
        model,
        "land #7 or close it",
        source="diagnostic",
        grounding=DirectiveGrounding(pr_numbers=(7,)),
        backend=_be(),
        enabled=False,
    )
    assert filled is False
    assert model.next_task_directive == "pre-set"
    assert mem_fs.get(_LEDGER) is None


def test_offer_occupied_slot_flag_on_appends_one_row(
    mem_fs: dict[str, str],
) -> None:
    model = SelfModel(next_task_directive="pre-set")
    filled = offer_directive(
        model,
        "land #7 or close it",
        source="diagnostic",
        grounding=DirectiveGrounding(pr_numbers=(7,), shas=("abc123",)),
        backend=_be(),
        enabled=True,
    )
    assert filled is False
    assert model.next_task_directive == "pre-set"
    rows = _rows(mem_fs)
    assert len(rows) == 1
    assert rows[0]["text"] == "land #7 or close it"
    assert rows[0]["source"] == "diagnostic"
    assert rows[0]["pr_numbers"] == [7]
    assert rows[0]["shas"] == ["abc123"]
    assert isinstance(rows[0]["recorded_at"], str)
    assert rows[0]["recorded_at"] != ""


def test_offer_enforces_size_cap_dropping_oldest(
    mem_fs: dict[str, str],
) -> None:
    model = SelfModel(next_task_directive="pre-set")
    for i in range(_MAX_ENTRIES + 5):
        offer_directive(
            model,
            f"directive {i}",
            source="diagnostic",
            grounding=DirectiveGrounding(pr_numbers=(i,)),
            backend=_be(),
            enabled=True,
        )
    rows = _rows(mem_fs)
    assert len(rows) == _MAX_ENTRIES
    # Oldest five dropped; the newest _MAX_ENTRIES survive in order.
    assert rows[0]["text"] == "directive 5"
    assert rows[-1]["text"] == f"directive {_MAX_ENTRIES + 4}"


def test_offer_of_slot_occupant_writes_nothing_flag_on(
    mem_fs: dict[str, str],
) -> None:
    """Re-offering the slot's own occupant is not a drop — nothing is banked."""
    model = SelfModel(next_task_directive="D")
    filled = offer_directive(
        model,
        "D",
        source="diagnostic",
        grounding=DirectiveGrounding(pr_numbers=(7,)),
        backend=_be(),
        enabled=True,
    )
    assert filled is False
    assert model.next_task_directive == "D"
    assert mem_fs.get(_LEDGER) is None


def test_offer_identical_reoffer_is_idempotent(
    mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated identical offer keeps one row and its first ``recorded_at``."""
    _seed(mem_fs, _entry("land #42 or close it", pr_numbers=[42]))
    ticks = iter(range(55))

    class _Clock:
        @staticmethod
        def now(tz: object) -> datetime:
            return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=next(ticks))

    monkeypatch.setattr("maestro.directive_ledger.datetime", _Clock)
    model = SelfModel(next_task_directive="occupant")
    first_recorded_at: object = None
    for _ in range(55):
        offer_directive(
            model,
            "land #7 or close it",
            source="diagnostic",
            grounding=DirectiveGrounding(pr_numbers=(7,), shas=("abc",)),
            backend=_be(),
            enabled=True,
        )
        if first_recorded_at is None:
            first_recorded_at = _rows(mem_fs)[1]["recorded_at"]
    rows = _rows(mem_fs)
    assert len(rows) == 2
    assert rows[0]["text"] == "land #42 or close it"
    assert rows[1]["text"] == "land #7 or close it"
    assert rows[1]["recorded_at"] == first_recorded_at


def test_offer_same_text_different_grounding_is_distinct(
    mem_fs: dict[str, str],
) -> None:
    """Dedupe keys on grounding too — same text citing another PR is banked."""
    backend = _be()
    backend.offer(DroppedDirective("fire", "s", (1,), (), _NOW))
    backend.offer(DroppedDirective("fire", "s", (2,), (), _NOW))
    backend.offer(DroppedDirective("fire", "s", (1,), ("abc",), _NOW))
    assert len(_rows(mem_fs)) == 3


# ---------------------------------------------------------------------------
# replay_dropped_directive
# ---------------------------------------------------------------------------


def _never_settled(_pr: int) -> bool:
    return False


def _never_landed(_sha: str) -> bool:
    return False


def test_replay_missing_ledger_returns_none(mem_fs: dict[str, str]) -> None:
    assert (
        replay_dropped_directive(
            _be(),
            is_pr_settled=_never_settled,
            is_sha_landed=_never_landed,
            now=_NOW,
        )
        is None
    )


def test_replay_reoffers_oldest_live_and_consumes_it(
    mem_fs: dict[str, str],
) -> None:
    _seed(
        mem_fs,
        _entry("fire #100", pr_numbers=[100]),
        _entry("fire sha abc", shas=["abc"]),
    )
    text = replay_dropped_directive(
        _be(),
        is_pr_settled=_never_settled,
        is_sha_landed=_never_landed,
        now=_NOW,
    )
    assert text == "fire #100"
    # The offered entry is consumed; only the second live entry remains.
    rows = _rows(mem_fs)
    assert len(rows) == 1
    assert rows[0]["text"] == "fire sha abc"
    # A second replay emits the remaining entry, then the ledger is empty.
    text2 = replay_dropped_directive(
        _be(),
        is_pr_settled=_never_settled,
        is_sha_landed=_never_landed,
        now=_NOW,
    )
    assert text2 == "fire sha abc"
    assert _rows(mem_fs) == []


def test_replay_prunes_settled_pr_without_reoffering(
    mem_fs: dict[str, str],
) -> None:
    _seed(
        mem_fs,
        _entry("fire #100", pr_numbers=[100]),  # settled → dead
        _entry("fire #200", pr_numbers=[200]),  # unsettled → live
    )
    text = replay_dropped_directive(
        _be(),
        is_pr_settled=lambda pr: pr == 100,
        is_sha_landed=_never_landed,
        now=_NOW,
    )
    assert text == "fire #200"
    # The dead entry is pruned and the offered live entry consumed → empty.
    assert _rows(mem_fs) == []


def test_replay_prunes_landed_sha_without_reoffering(
    mem_fs: dict[str, str],
) -> None:
    _seed(
        mem_fs,
        _entry("fire sha abc", shas=["abc"]),  # landed → dead
        _entry("fire sha def", shas=["def"]),  # unlanded → live
    )
    text = replay_dropped_directive(
        _be(),
        is_pr_settled=_never_settled,
        is_sha_landed=lambda sha: sha == "abc",
        now=_NOW,
    )
    assert text == "fire sha def"
    assert _rows(mem_fs) == []


def test_replay_dead_only_ledger_rewrites_empty_returns_none(
    mem_fs: dict[str, str],
) -> None:
    _seed(
        mem_fs,
        _entry("fire #100", pr_numbers=[100]),
        _entry("fire #200", pr_numbers=[200]),
    )
    text = replay_dropped_directive(
        _be(),
        is_pr_settled=lambda _pr: True,
        is_sha_landed=_never_landed,
        now=_NOW,
    )
    assert text is None
    assert _rows(mem_fs) == []


def test_replay_ages_out_stale_entry(mem_fs: dict[str, str]) -> None:
    _seed(
        mem_fs,
        _entry(
            "fire #100",
            pr_numbers=[100],
            recorded_at="2000-01-01T00:00:00+00:00",
        ),
    )
    # PR 100 never settles, but the entry is far past the age cap → dead.
    text = replay_dropped_directive(
        _be(),
        is_pr_settled=_never_settled,
        is_sha_landed=_never_landed,
        now=_NOW,
    )
    assert text is None
    assert _rows(mem_fs) == []


def test_replay_pr_and_sha_grounding_live_until_both_resolve(
    mem_fs: dict[str, str],
) -> None:
    """An entry citing a PR and a SHA stays live while either is unresolved."""
    _seed(mem_fs, _entry("fire both", pr_numbers=[100], shas=["abc"]))
    # PR settled but SHA unlanded → still live, re-offered.
    text = replay_dropped_directive(
        _be(),
        is_pr_settled=lambda _pr: True,
        is_sha_landed=_never_landed,
        now=_NOW,
    )
    assert text == "fire both"


def test_replay_ungrounded_entry_is_dead(mem_fs: dict[str, str]) -> None:
    """An entry with no PR/SHA grounding is vacuously dead — never replayed."""
    _seed(mem_fs, _entry("ungrounded"))
    text = replay_dropped_directive(
        _be(),
        is_pr_settled=_never_settled,
        is_sha_landed=_never_landed,
        now=_NOW,
    )
    assert text is None
    assert _rows(mem_fs) == []


def test_replay_tolerates_corrupt_line(mem_fs: dict[str, str]) -> None:
    mem_fs[_LEDGER] = (
        "not json at all\n"
        + json.dumps(_entry("fire #100", pr_numbers=[100]))
        + "\n"
    )
    text = replay_dropped_directive(
        _be(),
        is_pr_settled=_never_settled,
        is_sha_landed=_never_landed,
        now=_NOW,
    )
    assert text == "fire #100"


def test_replay_skips_blank_nondict_and_textless_rows(
    mem_fs: dict[str, str],
) -> None:
    """Blank lines, non-object rows and rows missing ``text`` are all skipped."""
    mem_fs[_LEDGER] = (
        "\n"  # blank line
        + json.dumps([1, 2, 3]) + "\n"  # non-dict row
        + json.dumps({"pr_numbers": [7]}) + "\n"  # missing ``text``
        + json.dumps(_entry("fire #100", pr_numbers=[100])) + "\n"
    )
    text = replay_dropped_directive(
        _be(),
        is_pr_settled=_never_settled,
        is_sha_landed=_never_landed,
        now=_NOW,
    )
    assert text == "fire #100"


def test_replay_empty_existing_ledger_returns_none(
    mem_fs: dict[str, str],
) -> None:
    """A ledger file that exists but holds no valid rows yields ``None``."""
    mem_fs[_LEDGER] = "   \n\n"
    text = replay_dropped_directive(
        _be(),
        is_pr_settled=_never_settled,
        is_sha_landed=_never_landed,
        now=_NOW,
    )
    assert text is None


def test_replay_tolerates_unparseable_now(mem_fs: dict[str, str]) -> None:
    """An unparseable ``now`` skips the age cap; grounding still governs."""
    _seed(
        mem_fs,
        _entry(
            "fire #100",
            pr_numbers=[100],
            recorded_at="2000-01-01T00:00:00+00:00",
        ),
    )
    text = replay_dropped_directive(
        _be(),
        is_pr_settled=_never_settled,
        is_sha_landed=_never_landed,
        now="garbage",
    )
    # Age cap skipped (now unparseable), PR unsettled → entry stays live.
    assert text == "fire #100"


# ---------------------------------------------------------------------------
# Cross-substrate parity: JSONL vs SQLite backend (proposal 106)
# ---------------------------------------------------------------------------


def _replay_script(backend: DroppedDirectiveBackend) -> list[str | None]:
    """Replay three times over a [#100, sha abc, #200] ledger.

    Settle #100 (dead → pruned, oldest live ``sha abc`` re-offered), then drain
    ``#200``, then observe the empty ``None`` — the observable sequence any
    substrate must reproduce.
    """
    return [
        replay_dropped_directive(
            backend,
            is_pr_settled=lambda pr: pr == 100,
            is_sha_landed=_never_landed,
            now=_NOW,
        ),
        replay_dropped_directive(
            backend,
            is_pr_settled=_never_settled,
            is_sha_landed=_never_landed,
            now=_NOW,
        ),
        replay_dropped_directive(
            backend,
            is_pr_settled=_never_settled,
            is_sha_landed=_never_landed,
            now=_NOW,
        ),
    ]


def _offer_then_replay(backend: DroppedDirectiveBackend) -> list[str | None]:
    """Offer three entries, then run :func:`_replay_script` over them."""
    backend.offer(DroppedDirective("fire #100", "s", (100,), (), _NOW))
    backend.offer(DroppedDirective("fire sha abc", "s", (), ("abc",), _NOW))
    backend.offer(DroppedDirective("fire #200", "s", (200,), (), _NOW))
    return _replay_script(backend)


def test_jsonl_and_sqlite_dropped_directives_agree(
    mem_fs: dict[str, str],
) -> None:
    """Both backends produce identical offer/replay results and are structural."""
    jsonl = JsonlDroppedDirectiveLedger(_DIR)
    sqlite = SqliteDroppedDirectiveLedger(StateStore(":memory:"))
    assert isinstance(jsonl, DroppedDirectiveBackend)
    assert isinstance(sqlite, DroppedDirectiveBackend)
    jsonl_results = _offer_then_replay(jsonl)
    sqlite_results = _offer_then_replay(sqlite)
    assert jsonl_results == sqlite_results
    assert jsonl_results == ["fire sha abc", "fire #200", None]


def test_jsonl_and_sqlite_dropped_directives_agree_via_legacy_seed(
    mem_fs: dict[str, str],
) -> None:
    """A SQLite store seeded from a legacy JSONL matches a JSONL adapter over it.

    The SQLite adapter is driven first so it seeds from the pristine legacy file
    before the JSONL adapter's own replays mutate that file in place.
    """
    _seed(
        mem_fs,
        _entry("fire #100", pr_numbers=[100]),
        _entry("fire sha abc", shas=["abc"]),
        _entry("fire #200", pr_numbers=[200]),
    )
    sqlite = SqliteDroppedDirectiveLedger(
        StateStore(":memory:"), legacy_jsonl_dir=_DIR,
    )
    sqlite_results = _replay_script(sqlite)
    jsonl = JsonlDroppedDirectiveLedger(_DIR)
    jsonl_results = _replay_script(jsonl)
    assert sqlite_results == jsonl_results
    assert sqlite_results == ["fire sha abc", "fire #200", None]


def test_load_all_dropped_directives_round_trips_seed_source(
    mem_fs: dict[str, str],
) -> None:
    """The seed source returns every row oldest-first as ``DroppedDirective``s."""
    _seed(
        mem_fs,
        _entry("fire #100", pr_numbers=[100]),
        _entry("fire sha abc", shas=["abc"]),
    )
    records = load_all_dropped_directives(_DIR)
    assert all(isinstance(r, DroppedDirective) for r in records)
    assert [r.text for r in records] == ["fire #100", "fire sha abc"]
    assert records[0].pr_numbers == (100,)
    assert records[1].shas == ("abc",)


def _offer_duplicates(backend: DroppedDirectiveBackend) -> list[str | None]:
    """Offer duplicate and distinct entries, then run :func:`_replay_script`."""
    for _ in range(3):
        backend.offer(DroppedDirective("fire #100", "s", (100,), (), _NOW))
    backend.offer(DroppedDirective("fire sha abc", "s", (), ("abc",), _NOW))
    backend.offer(DroppedDirective("fire #100", "other", (100,), (), "later"))
    backend.offer(DroppedDirective("fire #200", "s", (200,), (), _NOW))
    backend.offer(DroppedDirective("fire #200", "s", (201,), (), _NOW))  # distinct grounding
    return _replay_script(backend)


def test_jsonl_and_sqlite_dropped_directives_agree_on_duplicate_offers(
    mem_fs: dict[str, str],
) -> None:
    """Both backends dedupe identical re-offers and agree on the replay order."""
    jsonl = JsonlDroppedDirectiveLedger(_DIR)
    store = StateStore(":memory:")
    sqlite = SqliteDroppedDirectiveLedger(store)
    for _ in range(3):
        jsonl.offer(DroppedDirective("fire #100", "s", (100,), (), _NOW))
        sqlite.offer(DroppedDirective("fire #100", "s", (100,), (), _NOW))
    sqlite_count = store._conn.execute(
        "SELECT COUNT(*) FROM dropped_directives",
    ).fetchone()[0]
    assert len(_rows(mem_fs)) == 1
    assert sqlite_count == 1
    jsonl_results = _offer_duplicates(jsonl)
    sqlite_results = _offer_duplicates(sqlite)
    assert jsonl_results == sqlite_results
    assert jsonl_results == ["fire sha abc", "fire #200", "fire #200"]

"""The ``billing`` stamp on cost samples (proposal 152 step 1).

Every row is ``subscription`` or ``metered``; a legacy row (no key / NULL
column) reads as ``subscription`` on both backends. SQLite runs in-memory.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from maestro import sdk
from maestro.config import Config
from maestro.cost_tracking import CostSample, _cost_sample_from_dict, resolve_billing
from maestro.sdk import QueryUsage
from maestro.state_store import StateStore

_ROW = {
    "timestamp": "2026-10-01T00:00:00+00:00",
    "cost_usd": 0.5,
    "input_tokens": 1,
    "output_tokens": 2,
    "cache_read_tokens": 0,
    "cache_creation_tokens": 0,
    "caller": "reflect",
    "duration_ms": 10,
}


def _sample() -> CostSample:
    sample = _cost_sample_from_dict(dict(_ROW), Path("x"))
    assert sample is not None
    return sample


@pytest.mark.parametrize(
    ("provider", "auth", "expected"),
    [
        ("anthropic", "oauth", "subscription"),
        ("anthropic", "api_key", "metered"),
        ("anthropic", None, "subscription"),
        ("cli_paced", "oauth", "subscription"),
        ("cli_paced", "api_key", "metered"),
        ("cli_paced", None, "subscription"),
        ("openai", "oauth", "metered"),
        ("openai", None, "metered"),
        (None, None, "subscription"),
    ],
)
def test_resolve_billing_truth_table(
    provider: str | None, auth: str | None, expected: str,
) -> None:
    assert resolve_billing(provider, auth) == expected


def test_from_dict_without_key_reads_subscription() -> None:
    sample = _cost_sample_from_dict(dict(_ROW), Path("x"))
    assert sample is not None
    assert sample.billing == "subscription"


def test_from_dict_metered_round_trips() -> None:
    sample = _cost_sample_from_dict({**_ROW, "billing": "metered"}, Path("x"))
    assert sample is not None
    assert sample.billing == "metered"


def test_from_dict_garbage_billing_reads_subscription() -> None:
    sample = _cost_sample_from_dict({**_ROW, "billing": 7}, Path("x"))
    assert sample is not None
    assert sample.billing == "subscription"


@pytest.mark.parametrize(
    ("auth", "expected"), [("api_key", "metered"), ("oauth", "subscription")],
)
def test_log_sdk_call_stamps_billing(
    monkeypatch: pytest.MonkeyPatch, auth: str, expected: str,
) -> None:
    backend = MagicMock()
    monkeypatch.setattr(sdk, "_usage_sink", None)
    monkeypatch.setattr(sdk, "_cost_sample_backend", None)
    monkeypatch.setattr(sdk, "_cost_sample_auth_method", None)
    sdk.configure_cost_tracking(backend, auth_method=auth)
    sdk._log_sdk_call(QueryUsage(caller="c", provider="anthropic"))
    sample = backend.append.call_args.args[0]
    assert sample.billing == expected


def test_configure_cost_tracking_defaults_auth_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sdk, "_cost_sample_backend", None)
    monkeypatch.setattr(sdk, "_cost_sample_auth_method", "api_key")
    sdk.configure_cost_tracking(MagicMock())
    assert sdk._cost_sample_auth_method is None


_OLD_DDL = """
CREATE TABLE cost_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    ts_epoch REAL NOT NULL,
    cost_usd REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL,
    caller TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    estimated INTEGER NOT NULL,
    unmetered INTEGER NOT NULL,
    provider TEXT,
    effort TEXT,
    model TEXT
)
"""


def test_sqlite_old_table_gains_billing_and_legacy_row_reads_subscription() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(_OLD_DDL)
    conn.execute(
        "INSERT INTO cost_samples (timestamp, ts_epoch, cost_usd, input_tokens, "
        "output_tokens, cache_read_tokens, cache_creation_tokens, caller, "
        "duration_ms, estimated, unmetered, provider) "
        "VALUES ('2026-10-01T00:00:00+00:00', 1.0, 0.5, 1, 2, 0, 0, 'reflect', "
        "10, 0, 0, 'anthropic')",
    )
    conn.commit()
    store = StateStore(connection=conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(cost_samples)")}
    assert "billing" in cols
    [sample] = store.recent_cost_samples(5)
    assert sample.billing == "subscription"


def test_sqlite_metered_row_round_trips() -> None:
    store = StateStore(":memory:")
    store.append_cost_sample(dataclasses.replace(_sample(), billing="metered"))
    [sample] = store.load_cost_samples_since(0.0)
    assert sample.billing == "metered"


def test_sqlite_unknown_billing_reads_subscription() -> None:
    conn = sqlite3.connect(":memory:")
    store = StateStore(connection=conn)
    store.append_cost_sample(_sample())
    conn.execute("UPDATE cost_samples SET billing = 'bogus'")
    [sample] = store.recent_cost_samples(1)
    assert sample.billing == "subscription"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}, "oauth"),
        ({"ANTHROPIC_API_KEY": "k"}, "api_key"),
        ({}, None),
    ],
)
def test_detect_auth(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str | None,
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert Config.detect_auth() == expected


def test_check_auth_still_raises_same_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit, match=r"^No Claude authentication found\."):
        Config.check_auth()


def test_check_auth_returns_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert Config.check_auth() == "api_key"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"CLAUDE_CODE_OAUTH_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}, "api_key"),
        ({"ANTHROPIC_API_KEY": "k"}, "api_key"),
        ({"CLAUDE_CODE_OAUTH_TOKEN": "t"}, "oauth"),
        ({}, None),
    ],
)
def test_detect_billing_auth(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str | None,
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert Config.detect_billing_auth() == expected


def test_detect_billing_auth_warns_when_both_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with caplog.at_level("WARNING", logger="maestro.config"):
        Config.detect_billing_auth()
    assert "stamped metered" in caplog.text

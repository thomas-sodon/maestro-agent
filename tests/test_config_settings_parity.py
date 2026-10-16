"""Round-trip + constraint coverage for the pydantic-settings declarative parser.

Proposal 106. Mirrors the seed-then-assert idiom of
``tests/test_deed_ledger.py::TestParity`` / ``tests/test_cost_tracking.py`` over
the ``mem_fs`` fixture: a representative ``maestro.toml`` (+ a ``{env}`` overlay +
an env-var secret set) is loaded through both ``Config.from_env`` (the public
entry point, which delegates to :func:`build_config_authoritative`) and
``maestro.config_settings.build_config_via_pydantic`` (the underlying declarative
build), and the two ``Config`` objects are asserted equal field-for-field — so
``from_env`` is pinned to the builder and the builder's real-file parse is
exercised end to end.

The committed ``maestro.toml`` is the representative base — reading it (rather
than an inline subset) keeps the fixture complete: a future ``maestro.toml`` key
the pydantic models forget to mirror surfaces here as a build failure, not a
silent gap. It is read once at import (before ``mem_fs`` patches file I/O) exactly
as ``tests/test_config.py`` reads it.
"""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import ValidationError

from maestro import config_settings as cs
from maestro.config import Config
from maestro.config_settings import (
    _parse_cli_paced,
    _parse_phase_providers,
    _parse_pricing,
    _SdkSettings,
    _strip_sdk_nonflat_keys,
    build_config_authoritative,
    build_config_via_pydantic,
)

#: The committed ``maestro.toml`` text — read at import, before ``mem_fs`` patches
#: file I/O, so it reflects the real default surface under test.
_BASE_TOML = (Path(__file__).resolve().parent.parent / "maestro.toml").read_text(
    encoding="utf-8"
)

#: The committed ``maestro.toml`` parsed into its section tables — reused as the
#: valid base for the per-section constraint rejects below (proposal 106). Each
#: non-``[sdk]`` section table is exactly what ``build_config_via_pydantic`` feeds
#: its section model (``model_cls(**merged[section])``), so a table that builds
#: here is the real load-path input plus one deliberately out-of-range override.
_BASE_TABLES = tomllib.loads(_BASE_TOML)

_CONFIG_DIR = Path("/fake/cfg")
_BASE_PATH = str(_CONFIG_DIR / "maestro.toml")

#: A ``maestro.toml`` needle that isolates ``[loop] interval`` for the bool-reject
#: load-path tests. The bare ``interval = 60`` substring also matches
#: ``warning_hold_probe_interval = 60`` (both are ``_NumericInt``), so a
#: ``.replace("interval = 60", ...)`` would rewrite two fields — hiding a
#: ``loop_interval`` retype regression and mis-firing a future sub-10 control on
#: the collided ``[sdk]`` field. Anchoring on the ``[loop]`` header rewrites only
#: ``loop_interval``; each test asserts ``_BASE_TOML.count(...) == 1`` — the guard
#: ``tests/test_config.py`` uses — so a future collision fails loud, not silent.
_LOOP_INTERVAL_NEEDLE = "[loop]\ninterval = 60"


def _seed_base(mem_fs: dict[str, str]) -> None:
    mem_fs[_BASE_PATH] = _BASE_TOML


class TestParity:
    """``build_config_via_pydantic`` reproduces ``Config.from_env`` field-for-field.

    The section-model ``(section, key)`` completeness against the committed
    ``maestro.toml`` scalar keys is guarded in
    ``tests/test_config_flag_lifecycle.py::test_config_toml_mapping_matches_maestro_toml_keys``;
    here the full real-file round-trip is what proves the models parse it.
    """

    def test_base_only_parity(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        _seed_base(mem_fs)
        legacy = Config.from_env(env_file=None, config_dir=_CONFIG_DIR)
        shadow = build_config_via_pydantic(_CONFIG_DIR, env_name=None)
        assert legacy == shadow

    def test_overlay_and_secret_parity(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``{env}`` overlay plus env secrets round-trips identically.

        Exercises the deep-merged overlay, the empty-string ``->`` ``None``
        coercions (``repo`` set, ``telegram_chat_id`` empty), the env-var secrets,
        and every non-flat ``[sdk]`` sub-structure (a ``<phase>_provider`` override,
        a ``[sdk.cli_paced]`` table, and a ``[sdk.pricing.*]`` entry).
        """
        monkeypatch.setenv("MAESTRO_ENV", "parity")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "")  # empty -> None
        monkeypatch.setenv("MAESTRO_DASHBOARD_TOKEN", "dash")
        monkeypatch.setenv("MAESTRO_EMAIL_ADDRESS", "me@example.com")
        monkeypatch.delenv("MAESTRO_EMAIL_PASSWORD", raising=False)
        _seed_base(mem_fs)
        mem_fs[str(_CONFIG_DIR / "maestro.parity.toml")] = (
            '[github]\nrepo = "owner/repo"\nreviewer = "rev"\n'
            "[loop]\ninterval = 120\nbudget_rate_window_hours = 2\n"
            '[sdk]\nartifact_review_provider = "openai"\n'
            "[sdk.cli_paced]\nstream_idle_timeout_seconds = 180\nmean_key_ms = 90\n"
            'cols = 100\nprobe_metrics_enabled = true\nprobe_metrics_every = 3\n'
            'marker_pack = "none"\n'
            '[sdk.pricing."custom-model"]\ninput = 2.0\noutput = 4.0\n'
        )
        legacy = Config.from_env(env_file=None, config_dir=_CONFIG_DIR)
        shadow = build_config_via_pydantic(_CONFIG_DIR, env_name="parity")
        assert legacy == shadow
        # Spot-check the non-flat + coercion outcomes the parity assertion covers.
        assert shadow.sdk_phase_providers == {"artifact_review": "openai"}
        assert shadow.telegram_chat_id is None
        assert shadow.telegram_bot_token == "bot-token"
        assert shadow.github_repo == "owner/repo"
        assert shadow.sdk_cli_paced_cadence == {"mean_key_ms": 90.0, "cols": 100}
        assert shadow.sdk_cli_paced_stream_idle_timeout_seconds == 180.0
        assert shadow.sdk_pricing["custom-model"] == {"input": 2.0, "output": 4.0}

    def test_previously_unwired_fields_now_honour_toml(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The thirteen formerly-unwired keys now honour a non-default overlay.

        Before proposal 106's wiring, ``Config.from_env`` read these keys into
        ``toml_vals`` and dropped them, so both paths kept the dataclass default.
        Now the constructor consumes them and the declarative model mirrors them,
        so a non-default overlay reaches the built ``Config`` — in both the legacy
        and shadow parses, which stay equal field-for-field.
        """
        monkeypatch.setenv("MAESTRO_ENV", "wired")
        _seed_base(mem_fs)
        mem_fs[str(_CONFIG_DIR / "maestro.wired.toml")] = (
            "[loop]\n"
            "duplicate_pending_plan_threshold = 0.9\n"
            "pending_plan_cluster_threshold = 7\n"
            "pending_cluster_threshold = 0.99\n"
            "pending_cluster_max = 9\n"
            "pending_cluster_sweep_enabled = false\n"
            "restatement_sweep_enabled = false\n"
            "restatement_concept_key_enabled = true\n"
            "pending_plan_submit_block_threshold = 11\n"
            "quiet_skip_enabled = false\n"
            "executor_loop_detect_repeats = 9\n"
            "plan_review_expiry_days = 45\n"
            "[sdk]\n"
            "warning_hold_threshold = 8\n"
            "warning_hold_probe_interval = 120\n"
        )
        legacy = Config.from_env(env_file=None, config_dir=_CONFIG_DIR)
        shadow = build_config_via_pydantic(_CONFIG_DIR, env_name="wired")
        assert legacy == shadow

        defaults = {f.name: f.default for f in dataclasses.fields(Config)}
        # ``Config`` field name -> the non-default value the overlay set for it.
        overlaid: dict[str, object] = {
            "loop_duplicate_pending_plan_threshold": 0.9,
            "loop_pending_plan_cluster_threshold": 7,
            "loop_pending_cluster_threshold": 0.99,
            "loop_pending_cluster_max": 9,
            "loop_pending_cluster_sweep_enabled": False,
            "loop_restatement_sweep_enabled": False,
            "loop_restatement_concept_key_enabled": True,
            "loop_pending_plan_submit_block_threshold": 11,
            "quiet_skip_enabled": False,
            "executor_loop_detect_repeats": 9,
            "loop_plan_review_expiry_days": 45,
            "sdk_warning_hold_threshold": 8,
            "sdk_warning_hold_probe_interval": 120,
        }
        for field_name, overlay_value in overlaid.items():
            # A meaningful overlay: each value genuinely differs from the default.
            assert defaults[field_name] != overlay_value
            assert getattr(legacy, field_name) == overlay_value
            assert getattr(shadow, field_name) == overlay_value

    def test_malformed_pricing_rate_raises_on_both_paths(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-numeric ``[sdk.pricing.*]`` rate fails loud on both entry points.

        Neither coerces the numeric-looking string nor drops the entry (which
        would silently price the model at ``0.0``).
        """
        monkeypatch.setenv("MAESTRO_ENV", "badprice")
        _seed_base(mem_fs)
        mem_fs[str(_CONFIG_DIR / "maestro.badprice.toml")] = (
            '[sdk.pricing."stringy"]\ninput = "2.0"\n'
        )
        with pytest.raises(ValueError, match="'input' must be a number"):
            Config.from_env(env_file=None, config_dir=_CONFIG_DIR)
        with pytest.raises(ValueError, match="'input' must be a number"):
            build_config_via_pydantic(_CONFIG_DIR, env_name="badprice")


class TestSdkSubtableTyposFailLoud:
    """A typo'd or mistyped ``[sdk]`` sub-table key fails at load (#4053)."""

    @pytest.mark.parametrize(
        ("overlay", "needle"),
        [
            ('[sdk]\nbogus_provider = "anthropic"\n', "'bogus'"),
            ("[sdk.cli_paced]\nmin_key_msx = 40\n", "'min_key_msx'"),
            ('[sdk.cli_paced]\nmin_key_ms = "40"\n', "'min_key_ms'"),
            ('[sdk.cli_paced]\nprobe_metrics_enabled = "yes"\n', "probe_metrics_enabled"),
            ("[sdk.cli_paced]\nmarker_pack = 5\n", "marker_pack"),
            ('[sdk.pricing."m"]\ninputt = 1.0\n', "'inputt'"),
            ('[sdk.pricing."m"]\ninput = "2.0"\n', "'input'"),
            ('[sdk.pricing]\nm = "not-a-table"\n', "'m'"),
        ],
    )
    def test_typo_raises_naming_key(
        self,
        mem_fs: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        overlay: str,
        needle: str,
    ) -> None:
        monkeypatch.setenv("MAESTRO_ENV", "typo")
        _seed_base(mem_fs)
        mem_fs[str(_CONFIG_DIR / "maestro.typo.toml")] = overlay
        with pytest.raises(ValueError) as excinfo:
            build_config_authoritative(_CONFIG_DIR, env_name="typo")
        assert needle in str(excinfo.value)


class TestBuildEdgeCases:
    """The builder's non-happy-path branches."""

    def test_missing_base_raises(self, mem_fs: dict[str, str]) -> None:
        with pytest.raises(FileNotFoundError, match=r"maestro\.toml"):
            build_config_via_pydantic(_CONFIG_DIR, env_name=None)

    def test_invalid_env_name_raises(self, mem_fs: dict[str, str]) -> None:
        _seed_base(mem_fs)
        with pytest.raises(ValueError, match="Invalid MAESTRO_ENV"):
            build_config_via_pydantic(_CONFIG_DIR, env_name="bad/env")

    def test_absent_overlay_falls_back_to_base(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A valid env whose overlay file is absent parses the base alone."""
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        _seed_base(mem_fs)
        shadow = build_config_via_pydantic(_CONFIG_DIR, env_name="absent")
        base = build_config_via_pydantic(_CONFIG_DIR, env_name=None)
        assert shadow == base


class TestPhaseProviderParsing:
    def test_collects_every_phase_key_skipping_base(self) -> None:
        result = _parse_phase_providers(
            {
                "provider": "anthropic",  # base key -> skipped
                "max_concurrent_queries": 2,  # non-provider -> skipped
                "artifact_review_provider": "openai",
                "bogus_provider": "x",  # kept: the cross-field check rejects it
            }
        )
        assert result == {"artifact_review": "openai", "bogus": "x"}

    def test_no_providers_returns_empty(self) -> None:
        assert _parse_phase_providers({"provider": "anthropic"}) == {}


class TestPricingParsing:
    def test_absent_pricing_returns_empty(self) -> None:
        assert _parse_pricing({}) == {}

    def test_non_dict_pricing_raises(self) -> None:
        with pytest.raises(ValueError, match="pricing must be a table"):
            _parse_pricing({"pricing": "nope"})

    def test_present_keys_only(self) -> None:
        result = _parse_pricing(
            {"pricing": {"m1": {"input": 1.0, "output": 2.0}, "m2": {}}}
        )
        assert result == {"m1": {"input": 1.0, "output": 2.0}}

    def test_non_table_model_entry_raises(self) -> None:
        with pytest.raises(ValueError, match="'m2' must be a table"):
            _parse_pricing({"pricing": {"m2": "not-a-table"}})

    def test_unknown_rate_key_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown rate key 'unknown_rate'"):
            _parse_pricing({"pricing": {"m3": {"unknown_rate": 5.0}}})

    def test_int_rate_coerced_to_float(self) -> None:
        result = _parse_pricing({"pricing": {"m": {"input": 3}}})
        assert result == {"m": {"input": 3.0}}

    @pytest.mark.parametrize("bad", ["2.0", "abc", True])
    def test_non_numeric_rate_raises(self, bad: object) -> None:
        # Never coerced (``"2.0"``) nor silently dropped.
        with pytest.raises(ValueError, match="'input' must be a number"):
            _parse_pricing({"pricing": {"m": {"input": bad}}})


class TestCliPacedParsing:
    def test_absent_returns_empty(self) -> None:
        assert _parse_cli_paced({}) == {}

    def test_non_dict_raises(self) -> None:
        with pytest.raises(ValueError, match="cli_paced must be a table"):
            _parse_cli_paced({"cli_paced": "nope"})

    def test_full_table_parsed(self) -> None:
        result = _parse_cli_paced(
            {
                "cli_paced": {
                    "probe_metrics_enabled": True,
                    "marker_pack": "none",
                    "stream_idle_timeout_seconds": 180,
                    "probe_metrics_every": 3,
                    "mean_key_ms": 90,
                    "cols": 100,
                }
            }
        )
        assert result == {
            "sdk_cli_paced_probe_metrics_enabled": True,
            "sdk_cli_paced_marker_pack": "none",
            "sdk_cli_paced_stream_idle_timeout_seconds": 180.0,
            "sdk_cli_paced_probe_metrics_every": 3,
            "sdk_cli_paced_cadence": {"mean_key_ms": 90.0, "cols": 100},
        }

    def test_non_number_value_raises(self) -> None:
        with pytest.raises(ValueError, match="'bad_value' must be a number"):
            _parse_cli_paced({"cli_paced": {"bad_value": "x"}})

    def test_unknown_numeric_key_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown key 'unknown_number'"):
            _parse_cli_paced({"cli_paced": {"unknown_number": 5}})

    @pytest.mark.parametrize(
        ("key", "bad"),
        [("cols", 100.5), ("probe_metrics_every", 2.7), ("cols", float("inf"))],
    )
    def test_fractional_or_infinite_int_key_raises(
        self, key: str, bad: float
    ) -> None:
        with pytest.raises(ValueError, match=f"'{key}' must be a whole number"):
            _parse_cli_paced({"cli_paced": {key: bad}})

    def test_whole_float_int_key_accepted(self) -> None:
        result = _parse_cli_paced({"cli_paced": {"cols": 100.0}})
        assert result == {"sdk_cli_paced_cadence": {"cols": 100}}

    def test_non_bool_probe_flag_raises(self) -> None:
        with pytest.raises(ValueError, match="probe_metrics_enabled must be a bool"):
            _parse_cli_paced({"cli_paced": {"probe_metrics_enabled": "yes"}})

    def test_non_str_marker_raises(self) -> None:
        with pytest.raises(ValueError, match="marker_pack must be a string"):
            _parse_cli_paced({"cli_paced": {"marker_pack": 5}})


class TestConstraintRejects:
    """``Field`` constraints and custom validators reject bad values."""

    _SDK_OK: ClassVar[dict[str, object]] = {
        "warning_throttle_base_seconds": 10,
        "warning_throttle_hysteresis": 3,
        "rate_limit_idle_probe_interval": 300,
        "rate_limit_idle_max_probe_interval": 3600,
        "max_concurrent_queries": 2,
        "stream_idle_timeout_seconds": 180.0,
        "tool_stream_idle_timeout_seconds": 1800.0,
        "task_budget_tokens": 0,
        "provider": "anthropic",
        "enable_session_reuse": True,
    }

    def test_provider_must_be_registered(self) -> None:
        with pytest.raises(ValidationError, match="sdk_provider must be one of"):
            cs._SdkSettings(**{**self._SDK_OK, "provider": "bogus"})

    _PUBLICATION_OK: ClassVar[dict[str, object]] = {
        "enabled": True,
        "owner": "o",
        "copyright_holder": "h",
        "license": "MIT",
        "secret_scan_enabled": True,
        "executing_stale_timeout_seconds": 600,
        "push_retry_limit": 3,
    }


    def test_work_framing_must_be_valid(self) -> None:
        with pytest.raises(ValidationError, match="must be one of"):
            cs._WorkSettings(
                focus_profile="bad",
                task_framing="self_evolution",
                repo_workspace="",
                github_repo="",
                gate_source_path="maestro/",
                gate_tests_path="tests/",
                gate_lint_extra_paths=["scripts/"],
            )

    def test_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            cs._TimeoutsSettings(
                git=0,
                git_quick=10,
                selfcheck=300,
                selfcheck_max=900,
                implement=120,
                pr_creation=60,
                gh=30,
                shutdown=10.0,
            )

    def test_bool_on_int_field_rejected(self) -> None:
        # pydantic treats ``bool`` as a subclass of ``int``, so without the
        # ``_NumericInt`` before-validator ``git=True`` would coerce to ``1``.
        with pytest.raises(ValidationError, match="must be a number, not a boolean"):
            cs._TimeoutsSettings(
                git=True,
                git_quick=10,
                selfcheck=300,
                selfcheck_max=900,
                implement=120,
                pr_creation=60,
                gh=30,
                shutdown=10.0,
            )

    def test_bool_on_float_field_rejected(self) -> None:
        # The float ``_NumericFloat`` field rejects a boolean the same way its int
        # sibling does (``shutdown=False`` would otherwise coerce to ``0.0``).
        with pytest.raises(ValidationError, match="must be a number, not a boolean"):
            cs._TimeoutsSettings(
                git=30,
                git_quick=10,
                selfcheck=300,
                selfcheck_max=900,
                implement=120,
                pr_creation=60,
                gh=30,
                shutdown=False,
            )

    def test_genuine_int_for_float_field_still_coerces(self) -> None:
        # Negative control: a real int supplied for a float field passes the
        # bool-reject before-validator untouched and coerces to float, so the
        # change did not over-tighten legitimate int-for-float TOML values.
        model = cs._TimeoutsSettings(
            git=30,
            git_quick=10,
            selfcheck=300,
            selfcheck_max=900,
            implement=120,
            pr_creation=60,
            gh=30,
            shutdown=30,
        )
        assert model.shutdown_timeout == 30.0
        assert isinstance(model.shutdown_timeout, float)

    def test_non_finite_float_rejected(self) -> None:
        with pytest.raises(ValidationError):
            cs._SdkSettings(
                **{**self._SDK_OK, "stream_idle_timeout_seconds": float("inf")}
            )

    def test_empty_required_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            cs._DashboardSettings(
                port=8420,
                host="",
                rejection_reason_max=2000,
                max_page_size=500,
                log_buffer_size=500,
                trust_forwarded_proto=False,
                trust_forwarded_for=False,
                public_api_rate_limit_per_minute=60,
                public_api_cache_max_age_seconds=30,
                security_headers_enabled=True,
                login_rate_limit_per_minute=10,
                approval_rate_limit_per_minute=30,
            )

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            cs._CiSettings()  # cov_fail_under required

    # ── Per-section backfill (proposal 106) ────────────────────────────────
    # TestConstraintRejects above pins only 5 of the 19 section models. The
    # deleted ``Config.validate`` flat-scalar ladder held the numeric/length
    # guards for the rest; those guards now live declaratively on the section
    # models, so one representative reject per remaining constrained model keeps
    # that enforcement pinned. Each constructs the model from its committed
    # ``maestro.toml`` table (the real load-path input, which must itself build)
    # plus one out-of-range override. ``_LoopSettings`` / ``_AuditSettings``
    # owned the bulk of the retired numeric and bool fields.
    @pytest.mark.parametrize(
        ("model_name", "section", "bad_key", "bad_value"),
        [
            ("_LoopSettings", "loop", "interval", 0),
            # Folded here from tests/test_replanning.py and
            # tests/test_agent_lifecycle.py (proposal 106 step 4): their
            # direct-construction ``.validate()`` negative rejects moved onto the
            # owning ``_LoopSettings`` ``Field(ge=0)`` guards.
            ("_LoopSettings", "loop", "max_replans", -1),
            ("_LoopSettings", "loop", "uncalled_symbol_max_allowed", -1),
            ("_AuditSettings", "audit", "stall_threshold", 0),
            ("_GithubSettings", "github", "issues_page_size", 0),
            ("_CliSettings", "cli", "journal_default_count", 0),
            ("_TelegramSettings", "telegram", "max_length", 0),
            ("_LogsSettings", "logs", "dates_shown", 0),
            ("_MessagingSettings", "messaging", "max_length", 0),
            ("_EmailSettings", "email", "smtp_port", 70000),
            ("_ArtifactsSettings", "artifacts", "review_every", 0),
            ("_PublicationSettings", "publication", "push_retry_limit", 0),
            ("_McpSettings", "mcp", "context7_command", ""),
            ("_EvolutionFuelSettings", "evolution_fuel", "max_plans_per_gap", 0),
        ],
    )
    def test_section_model_out_of_range_rejected(
        self, model_name: str, section: str, bad_key: str, bad_value: object,
    ) -> None:
        model = getattr(cs, model_name)
        base = _BASE_TABLES[section]
        model(**base)  # the committed table is a valid base (guards a stale key)
        with pytest.raises(ValidationError):
            model(**{**base, bad_key: bad_value})

    @pytest.mark.parametrize(
        ("model_name", "section"),
        [("_PathsSettings", "paths"), ("_ModelsSettings", "models")],
    )
    def test_constraintless_section_model_rejects_unknown_key(
        self, model_name: str, section: str,
    ) -> None:
        """``[paths]`` and ``[models]`` carry no per-field numeric/length bound
        (the retired ladder held none for them either), so ``extra="forbid"`` is
        their rejection surface: a genuinely-unknown key must still fail loud."""
        model = getattr(cs, model_name)
        base = _BASE_TABLES[section]
        model(**base)
        with pytest.raises(ValidationError):
            model(**{**base, "bogus_unknown_key": 1})


class TestBuildAuthoritative:
    """``build_config_authoritative`` builds, validates, and normalises errors."""

    def test_returns_validated_declarative_config(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        _seed_base(mem_fs)
        result = build_config_authoritative(_CONFIG_DIR, None)
        # Same object the declarative builder produces, having also passed validate().
        assert result == build_config_via_pydantic(_CONFIG_DIR, None)

    def test_validation_error_reraised_as_plain_value_error(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A section-model ``ValidationError`` surfaces as a plain ``ValueError``.

        ``pydantic.ValidationError`` subclasses ``ValueError``, so the assertion
        that it is *not* a ``ValidationError`` proves the wrapping actually ran.
        """
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "[loop]\n", "[loop]\nnonexistent_loop_key = 1\n", 1
        )
        with pytest.raises(ValueError) as exc_info:
            build_config_authoritative(_CONFIG_DIR, None)
        assert not isinstance(exc_info.value, ValidationError)

    def test_cross_field_guard_enforced_by_declarative_build_not_validate(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cross-field violation is rejected by the declarative build itself.

        ``build_config_authoritative`` no longer runs the legacy ``Config.validate``
        raise-ladder on the load path (proposal 106): the cross-field/sub-table
        guards live in :meth:`Config._check_cross_field_and_subtable`, which
        :func:`build_config_via_pydantic` invokes. Equal standby hours pass every
        per-field ``Field(...)`` constraint but violate the cross-field guard, so
        the authoritative build must raise — and a spy that would blow up if
        ``Config.validate`` were called proves the rejection comes from the
        declarative build, not from a separate ``validate()`` pass.
        """
        monkeypatch.delenv("MAESTRO_ENV", raising=False)

        def _boom(self: Config) -> None:
            raise AssertionError("Config.validate must not run on the load path")

        monkeypatch.setattr(Config, "validate", _boom)
        # standby_start_hour_utc defaults to 13; make the end hour equal it.
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "standby_end_hour_utc = 19", "standby_end_hour_utc = 13"
        )
        with pytest.raises(
            ValueError,
            match="standby_start_hour_utc and standby_end_hour_utc must differ",
        ):
            build_config_authoritative(_CONFIG_DIR, None)


class TestFromEnvUsesDeclarativeBuild:
    """``from_env`` unconditionally returns the declarative Config (proposal 106)."""

    def test_from_env_returns_declarative_object(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``from_env`` returns the object ``build_config_via_pydantic`` built.

        A distinguishing (valid) marker injected onto the declarative build proves
        ``from_env`` delegates to and returns the builder's Config, not one
        constructed any other way.
        """
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        _seed_base(mem_fs)

        def _marked(config_dir: Path, env_name: str | None = None) -> Config:
            cfg = build_config_via_pydantic(config_dir, env_name)
            cfg.github_issues_page_size = 99  # valid (ge=1) marker on the build
            return cfg

        monkeypatch.setattr(cs, "build_config_via_pydantic", _marked)
        result = Config.from_env(env_file=None, config_dir=_CONFIG_DIR)
        assert result.github_issues_page_size == 99

    def test_from_env_unknown_key_raises_value_error(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown key in a modelled section makes ``from_env`` raise a plain
        ``ValueError`` (the section-model ``ValidationError`` normalised)."""
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "[loop]\n", "[loop]\nnonexistent_loop_key = 1\n", 1
        )
        with pytest.raises(ValueError) as exc_info:
            Config.from_env(env_file=None, config_dir=_CONFIG_DIR)
        assert not isinstance(exc_info.value, ValidationError)


class TestForbidUnknownKeys:
    """``extra="forbid"`` rejects an unknown key in a modelled section."""

    def test_unknown_key_in_modelled_section_fails_loud(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        # Negative control: the committed config (only known keys) builds clean.
        _seed_base(mem_fs)
        build_config_via_pydantic(_CONFIG_DIR, None)
        # An unknown key in a modelled section now fails loud at load.
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "[loop]\n", "[loop]\nnonexistent_loop_key = 1\n", 1
        )
        with pytest.raises(ValidationError):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_retired_github_mcp_key_fails_loud(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#4132: an overlay still setting the retired ``[mcp] github_enabled``
        is rejected as an unknown key, not silently ignored."""
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "[mcp]\n", "[mcp]\ngithub_enabled = true\n", 1
        )
        with pytest.raises(ValidationError, match="github_enabled"):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_retired_fetch_mcp_key_fails_loud(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proposal 146 Step 11: an overlay still setting the retired
        ``[mcp] fetch_enabled`` is rejected as an unknown key."""
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "[mcp]\n", "[mcp]\nfetch_enabled = true\n", 1
        )
        with pytest.raises(ValidationError, match="fetch_enabled"):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_unknown_top_level_section_fails_loud(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown top-level section (a typo like ``[looop]``) fails loud as a
        ``ValueError`` — the section-level analogue of the per-key forbid guard,
        replacing the retired loader's silent drop-and-warn. The committed
        ``[review]`` external section builds clean in every parity test above, so
        the ``_KNOWN_EXTERNAL_SECTIONS`` exemption is already exercised."""
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML + "\n[looop]\nfoo = 1\n"
        with pytest.raises(ValueError, match="Unknown top-level TOML section"):
            build_config_via_pydantic(_CONFIG_DIR, None)


class TestCrossFieldRejects:
    """The cross-field / semantic / ``[sdk]`` sub-table guards reject on the load path.

    Proposal 106 folded the guards a per-field ``Field(...)`` constraint cannot
    express into :meth:`Config._check_cross_field_and_subtable`, which
    :func:`build_config_via_pydantic` runs on the assembled ``Config``. Each case
    seeds an otherwise-valid ``maestro.toml`` (a ``_BASE_TOML`` mutation or a
    ``{env}`` overlay) whose only fault is a value that passes every section-model
    ``Field(...)`` constraint but violates one of these guards, and asserts the
    declarative build raises ``ValueError`` — mirroring ``TestForbidUnknownKeys``.
    """

    def test_selfcheck_max_below_selfcheck_rejected(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lease's budget cap may not sit below the floor it escalates from.

        Both values pass ``gt=0`` alone, but a cap under the floor would make a
        derived deadline clamp *down* below the flat ceiling every gate already
        gets — tightening the suite's budget instead of widening it.
        """
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "selfcheck_max = 1350", "selfcheck_max = 200"
        )
        with pytest.raises(
            ValueError, match="selfcheck_max_timeout must be an int >= selfcheck_timeout"
        ):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_unsafe_archive_threshold_rejected(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An enabled rotation threshold inside the widest hot-read window rejects.

        ``7`` passes ``Field(ge=0)`` alone, but 168 hours does not *exceed* the
        dashboard's 168-hour ``window_hours`` cap over ``cost_samples.jsonl``,
        so the rotation would shed in-window rows to cold storage.
        """
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "archive_threshold_days = 0", "archive_threshold_days = 7"
        )
        with pytest.raises(
            ValueError, match=r"archive_threshold_days must be 0 \(disabled\)"
        ):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_safe_archive_threshold_accepted(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A threshold above the floor builds clean on the declarative path."""
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "archive_threshold_days = 0", "archive_threshold_days = 32"
        )
        cfg = build_config_via_pydantic(_CONFIG_DIR, None)
        assert cfg.archive_threshold_days == 32

    def test_selfcheck_max_equal_to_selfcheck_accepted(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Equality is the degenerate-but-valid "no escalation" configuration."""
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "selfcheck_max = 1350", "selfcheck_max = 900"
        )
        config = build_config_via_pydantic(_CONFIG_DIR, None)
        assert config.selfcheck_max_timeout == 900

    def test_tool_stream_idle_below_selfcheck_max_rejected(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The idle net may not sit at or below the largest gate budget.

        A gate run permitted to spend ``selfcheck_max`` seconds inside one Bash
        call whose stream is declared stalled at the same figure can never
        answer: the suite is not failing, it is being cut off. Equality is
        rejected too — the net must strictly outlast the budget.
        """
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "tool_stream_idle_timeout_seconds = 1800",
            "tool_stream_idle_timeout_seconds = 1350",
        )
        with pytest.raises(
            ValueError,
            match=(
                "sdk_tool_stream_idle_timeout_seconds must be strictly greater "
                "than selfcheck_max_timeout"
            ),
        ):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_tool_stream_idle_disabled_skips_ordering(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disabled net has no ordering to violate.

        ``<= 0`` is the documented "no idle timeout" semantics; the guard must
        not turn that into a startup failure just because ``0 < selfcheck_max``.
        The load path's own ``ge=0`` bound keeps a negative out of the TOML, so
        the negative half of the disable semantics is covered against the
        dataclass in ``tests/test_config.py``.
        """
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "tool_stream_idle_timeout_seconds = 1800",
            "tool_stream_idle_timeout_seconds = 0",
        )
        config = build_config_via_pydantic(_CONFIG_DIR, None)
        assert config.sdk_tool_stream_idle_timeout_seconds == 0.0

    def test_shipped_toml_satisfies_timeout_ordering(self) -> None:
        """The committed constants must satisfy the ordering they are pinned by.

        Reads the real ``maestro.toml`` text so a future edit to either knob
        cannot drift the shipped values away from the validator.
        """
        timeouts = _BASE_TABLES["timeouts"]
        idle = _BASE_TABLES["sdk"]["tool_stream_idle_timeout_seconds"]
        assert timeouts["selfcheck"] <= timeouts["selfcheck_max"]
        assert timeouts["selfcheck_max"] < idle

    def test_standby_hours_equal_rejected(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        # start defaults to 13; equal hours pass ge=0/le=23 but mean always on/off.
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "standby_end_hour_utc = 19", "standby_end_hour_utc = 13"
        )
        with pytest.raises(
            ValueError,
            match="standby_start_hour_utc and standby_end_hour_utc must differ",
        ):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_peak_throttle_hours_equal_rejected(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        # start defaults to 5; equal hours pass ge=0/le=23 but mean always on/off.
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "peak_throttle_end_hour = 11", "peak_throttle_end_hour = 5"
        )
        with pytest.raises(
            ValueError,
            match="peak_throttle_start_hour and peak_throttle_end_hour",
        ):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_invalid_iana_timezone_rejected(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        # A non-empty string passes ``Field(min_length=1)`` but is not a real zone.
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            'peak_throttle_timezone = "America/Los_Angeles"',
            'peak_throttle_timezone = "Not/AZone"',
        )
        with pytest.raises(
            ValueError, match="peak_throttle_timezone must be a valid IANA zone name"
        ):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_orphaned_cure_retire_not_greater_rejected(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        # age defaults to 3; retire == age passes ge=1 on both but is not strictly >.
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "orphaned_cure_retire_age_cycles = 30",
            "orphaned_cure_retire_age_cycles = 3",
        )
        with pytest.raises(
            ValueError, match="orphaned_cure_retire_age_cycles must be an int strictly"
        ):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_sdk_idle_max_below_probe_rejected(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        # probe defaults to 300; a max of 100 passes ge=10 but is below probe.
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "rate_limit_idle_max_probe_interval = 3600",
            "rate_limit_idle_max_probe_interval = 100",
        )
        with pytest.raises(
            ValueError, match="sdk_rate_limit_idle_max_probe_interval must be"
        ):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_task_budget_in_disabled_gap_rejected(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        # 500 passes ge=0 but falls in the disallowed (0, 1024) gap.
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            "task_budget_tokens = 0", "task_budget_tokens = 500"
        )
        with pytest.raises(ValueError, match="sdk_task_budget_tokens must be 0"):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_negative_cli_paced_cadence_rejected(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        # A negative pacing knob is a real number _parse_cli_paced keeps, so the
        # sub-table guard (not the tolerant parser) is what rejects it.
        mem_fs[_BASE_PATH] = _BASE_TOML + "\n[sdk.cli_paced]\nmean_key_ms = -5\n"
        with pytest.raises(ValueError, match="sdk_cli_paced_cadence"):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_phase_providers_without_session_reuse_rejected(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        # A valid per-phase override plus session-reuse off: each value passes its
        # own membership/bool check, but the override cannot take effect stateless.
        _seed_base(mem_fs)
        mem_fs[str(_CONFIG_DIR / "maestro.phase.toml")] = (
            '[sdk]\nenable_session_reuse = false\n'
            'contemplation_provider = "anthropic"\n'
        )
        with pytest.raises(ValueError, match="enable_session_reuse=true"):
            build_config_via_pydantic(_CONFIG_DIR, env_name="phase")


class TestNumericBoolRejects:
    """A boolean supplied for a numeric setting fails loud on the load path.

    pydantic treats ``bool`` as a subclass of ``int``, so it coerces a boolean to
    ``1``/``0`` for an ``int``/``float`` field by default — a mistyped
    ``maestro.toml`` line (``interval = true``) would otherwise load as
    ``loop_interval == 1`` instead of failing at startup. The
    ``_NumericInt``/``_NumericFloat`` before-validators reject it. Follows the
    seeded-``maestro.toml`` load-path idiom of ``TestForbidUnknownKeys`` /
    ``TestCrossFieldRejects``.
    """

    def test_bool_numeric_via_pydantic_raises(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        # ``interval = true`` parses as a TOML boolean but the section model
        # rejects it before it can coerce to ``1``.
        assert _BASE_TOML.count(_LOOP_INTERVAL_NEEDLE) == 1
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            _LOOP_INTERVAL_NEEDLE, "[loop]\ninterval = true"
        )
        with pytest.raises(ValidationError, match="must be a number, not a boolean"):
            build_config_via_pydantic(_CONFIG_DIR, None)

    def test_bool_numeric_via_authoritative_raises_plain_value_error(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The authoritative entry normalises the reject to a plain ``ValueError``.

        ``pydantic.ValidationError`` subclasses ``ValueError``, so asserting the
        raised error is *not* a ``ValidationError`` proves
        ``build_config_authoritative``'s wrapping ran on the bool-reject path too.
        """
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        assert _BASE_TOML.count(_LOOP_INTERVAL_NEEDLE) == 1
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            _LOOP_INTERVAL_NEEDLE, "[loop]\ninterval = true"
        )
        with pytest.raises(ValueError) as exc_info:
            build_config_authoritative(_CONFIG_DIR, None)
        assert not isinstance(exc_info.value, ValidationError)

    def test_genuine_int_builds_clean(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: a real integer passes the before-validator untouched."""
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        assert _BASE_TOML.count(_LOOP_INTERVAL_NEEDLE) == 1
        mem_fs[_BASE_PATH] = _BASE_TOML.replace(
            _LOOP_INTERVAL_NEEDLE, "[loop]\ninterval = 45"
        )
        config = build_config_via_pydantic(_CONFIG_DIR, None)
        assert config.loop_interval == 45

    def test_int_for_float_field_coerces_and_builds(
        self, mem_fs: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control: a TOML int for a float field still coerces on load."""
        monkeypatch.delenv("MAESTRO_ENV", raising=False)
        mem_fs[_BASE_PATH] = _BASE_TOML.replace("shutdown = 10.0", "shutdown = 30")
        config = build_config_via_pydantic(_CONFIG_DIR, None)
        assert config.shutdown_timeout == 30.0
        assert isinstance(config.shutdown_timeout, float)


class TestSdkNonFlatStripping:
    """The non-flat ``[sdk]`` keys are stripped ahead of ``extra="forbid"``."""

    #: The committed ``[sdk]`` section — all 13 flat keys — plus an operator
    #: ``pricing`` override table (``maestro.toml`` ships none), a realistic,
    #: complete dict the strip must reduce to flat keys before the forbid'd
    #: ``_SdkSettings`` sees it.
    _BASE_SDK: ClassVar[dict[str, object]] = {
        **tomllib.loads(_BASE_TOML)["sdk"],
        "pricing": {"custom-model": {"input": 2.0, "output": 4.0}},
    }

    def test_strip_removes_nonflat_keeps_flat(self) -> None:
        sdk_raw = {
            "provider": "anthropic",  # base flat key -> kept
            "max_concurrent_queries": 2,  # flat key -> kept
            "artifact_review_provider": "openai",  # <phase>_provider -> stripped
            "pricing": {"m": {"input": 1.0}},  # nested table -> stripped
            "cli_paced": {"cols": 80},  # nested table -> stripped
        }
        stripped = _strip_sdk_nonflat_keys(sdk_raw)
        assert stripped == {"provider": "anthropic", "max_concurrent_queries": 2}
        # The caller's dict is left intact for the sub-table parsers.
        assert "pricing" in sdk_raw

    def test_forbid_rejects_nonflat_pricing_when_not_stripped(self) -> None:
        """Load-bearing proof: the committed ``[sdk]`` plus ``pricing`` trips forbid.

        The 13 flat keys are all present, so the only reason the raw dict is
        rejected is the undeclared ``pricing`` table — exactly what the strip
        removes.
        """
        with pytest.raises(ValidationError):
            _SdkSettings(**self._BASE_SDK)

    def test_stripped_committed_sdk_builds(self) -> None:
        stripped = _strip_sdk_nonflat_keys(self._BASE_SDK)
        assert "pricing" not in stripped
        model = _SdkSettings(**stripped)  # builds once the non-flat key is gone
        assert model.sdk_provider == self._BASE_SDK["provider"]



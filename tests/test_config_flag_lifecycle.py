"""Config-flag lifecycle guard: every ``Config`` field must have a consumer.

``Config`` in ``maestro/config.py`` has grown to hundreds of dataclass fields,
each threaded by hand through several disconnected edit sites — the dataclass
declaration, the ``config_settings`` section model, the ``maestro.toml``
defaults, and the consumer that actually reads the value. Nothing binds those
sites together, so when a refactor deletes the last reader of a flag the field
silently survives: it still parses, still validates, still ships in
``maestro.toml``, still costs boilerplate — but changes no behaviour.

This deterministic, zero-LLM-cost source scan — the same ``pathlib``-over-
``maestro/`` guard family as ``tests/test_removed_dead_pr_fact_functions.py`` —
enumerates every ``Config`` field and fails when a field has no reader anywhere
under ``maestro/``. It reads the real source files, so it touches no ``mem_fs``,
network, or subprocess. A field counts as *read* when an ``ast.Attribute`` access
of ``.<field>`` appears in some scanned source's syntax tree at a location that is
**not** (a) ``toml_config.py`` — the pure mirror layer that names every field by
construction — and **not** (b) a line inside ``config.py``'s ``validate()`` /
``from_env()`` spans, the two plumbing/validation regions whose ``self.<field>``
references are not genuine consumers. Matching on the parsed syntax tree rather
than raw text is what keeps a field named only in a comment, docstring, or string
literal from masquerading as a live reader.

The attribute match is deliberately *receiver-blind*: it records the ``.attr`` name
of every ``ast.Attribute`` regardless of the object it is read from, so a ``Config``
field is treated as read whenever *any* object under ``maestro/`` exposes an
attribute of the same name (``some_pr.enabled`` would mask a dead ``enabled`` flag).
That keeps the scan zero-cost and free of type inference, at a known efficacy
ceiling: it reliably surfaces dead flags whose names are *distinct* (as all seven
allowlisted fields are) but cannot catch one whose name collides with an unrelated
attribute elsewhere in the tree. Narrowing the match to receiver-verified ``Config``
accesses is a deliberate non-goal of this coarse guard.

New flags can then only enter the surface with a consumer wired in. The eight
fields that are dead on today's tree are recorded in a documented, self-cleaning
allowlist (:data:`_KNOWN_READERLESS_FIELDS`); the day someone wires a consumer or
deletes one, its stale allowlist entry turns the build red and must be removed.

Beyond that field -> reader direction, this file also guards the field ->
manifest *completeness* direction: :func:`test_every_default_off_flag_is_managed_or_allowlisted`
requires every default-*off* boolean ``Config`` field to be either
lifecycle-tracked in a manifest (``EXPERIMENTAL_FLAGS`` / ``PROD_ONLY_CANARIES`` /
``PROD_ONLY_OPERATIONAL``) or grandfathered in :data:`_UNMANIFESTED_DEFAULT_OFF`
with a reason — the default-off mirror of ``tests/test_cutover_flag_collapse.py``'s
default-on classification guard, so a new opt-in switch can no longer ship without
a review horizon. This completes the trio: field -> reader (the dead-flag scan),
manifest -> field (``test_every_experimental_flag_names_a_real_config_field`` /
``test_every_toml_mapping_names_a_real_config_field``), and now field -> manifest.

This module is CI-only: no runtime behaviour, no state file, no trust-chain
change. The config<->TOML symmetry guard (``maestro_toml_scalar_pairs`` and the
``test_config_toml_mapping_*`` tests) now lives here alongside the reader scan;
deleting the dead flags surfaced above, and the remaining experimental-flag
review-horizon manifest, are left for later focused PRs.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import tomllib
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from maestro.config import SDK_PHASE_NAMES, Config
from maestro.config_settings import _SECTION_MODELS, build_config_via_pydantic
from maestro.flag_lifecycle import (
    CUTOVER_FLAGS,
    EXPERIMENTAL_FLAGS,
    FREEZE_DATE,
    PROD_ONLY_CANARIES,
    PROD_ONLY_CANARY_BASELINE,
    PROD_ONLY_DISABLED,
    PROD_ONLY_OPERATIONAL,
    CutoverFlag,
    EnvOverlayCanary,
    ExperimentalFlag,
    audit_flags_report,
    disabled_only_in_overlay,
    enabled_only_in_overlay,
    find_horizons_within,
    find_overdue_flags,
    find_unjustified_canaries,
    format_flag_horizon_summary,
    parse_review_by,
)
from maestro.toml_config import _KNOWN_EXTERNAL_SECTIONS

_MAESTRO_ROOT = Path(__file__).resolve().parent.parent / "maestro"
_CONFIG_PY = _MAESTRO_ROOT / "config.py"

#: The committed ``maestro.toml`` — the real default surface ("Config field
#: defaults are for tests only"), read for the Config<->TOML symmetry guard.
_MAESTRO_TOML = Path(__file__).resolve().parent.parent / "maestro.toml"

#: The project root (parent of ``maestro.toml``) — the ``config_dir`` argument
#: ``build_config_via_pydantic`` resolves ``maestro.toml`` and ``maestro.{env}.toml``
#: under, mirroring ``test_dockerfile_structure``'s ``repo_root`` resolution.
_REPO_ROOT = _MAESTRO_TOML.parent

#: Relative path (under ``maestro/``) of the ``Config`` dataclass module. A
#: ``self.<field>`` reference here counts as a reader only when it sits *outside*
#: the plumbing/validation spans discovered by :func:`_config_plumbing_spans`.
_CONFIG_RELPATH = "config.py"

#: Relative path (under ``maestro/``) of the TOML mirror layer. Every field is
#: named here by construction (parse + overlay), so a match here is never a
#: genuine behavioural consumer — the whole file is excluded from the reader scan.
_TOML_RELPATH = "toml_config.py"

#: Relative path (under ``maestro/``) of the experimental-flag manifest module.
#: Its own ``gate="<name>"`` declarations are the manifest under test, not emitting
#: call sites, so the gate-name scan excludes it — otherwise a typo'd manifest gate
#: would validate against its own declaration (the same self-reference the reader
#: scan excludes ``toml_config.py`` to avoid), rotting the guard into a tautology.
_FLAG_LIFECYCLE_RELPATH = "flag_lifecycle.py"

#: The ``Config`` regions whose ``self.<field>`` references are pure
#: plumbing/validation, not real consumers: ``validate()`` (type/range checks),
#: ``_check_cross_field_and_subtable()`` (the cross-field / ``[sdk]`` sub-table
#: guards ``validate`` and the declarative load path share, proposal 106), and the
#: ``from_env()`` classmethod (env-var construction).
_PLUMBING_METHODS = frozenset(
    {"validate", "_check_cross_field_and_subtable", "from_env"}
)


#: Fields declared and validated on ``Config`` today but consumed by no reader
#: anywhere under ``maestro/`` — each mapped to why it is dead. Feeding this set
#: into :func:`find_readerless_fields` lets the guard pass green on today's tree
#: while :func:`test_known_readerless_allowlist_is_current` keeps the list honest:
#: the moment a consumer is wired in (or the flag deleted), the stale entry turns
#: the build red. Wiring or deleting each flag is deliberately left to a later
#: human-reviewed PR; this increment only makes them visible.
_KNOWN_READERLESS_FIELDS: dict[str, str] = {
    "email_thomas": (
        "populated from MAESTRO_EMAIL_THOMAS in from_env() but read by no "
        "consumer — the email surfaces never reference it"
    ),
    "shutdown_timeout": (
        "validated only; the loop's shutdown waits go through "
        "_safe_shutdown_wait with sleeps/cooldowns, never this field"
    ),
    "construction_dominance_enabled": (
        "check_construction_dominance sits in the audit registry list but "
        "run_all_checks never dispatches it, so the flag never reaches it"
    ),
    "construction_dominance_threshold": (
        "consumed only by the undispatched check_construction_dominance rule"
    ),
    "construction_dominance_window": (
        "consumed only by the undispatched check_construction_dominance rule"
    ),
    "publication_license": (
        "the publisher hardcodes MIT via _format_license_mit, never reading "
        "this field"
    ),
    "publication_secret_scan_enabled": (
        "the pre-push secret scan runs unconditionally, never gating on this flag"
    ),
}


def _iter_py_files() -> list[Path]:
    """Every ``*.py`` under ``maestro/`` (excluding ``__pycache__``)."""
    return [p for p in _MAESTRO_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _read_maestro_sources() -> dict[str, str]:
    """Build the ``{relpath: text}`` source map the reader scan walks.

    Keyed by POSIX path relative to ``maestro/`` (so ``config.py`` and
    ``toml_config.py`` match the exclusion constants above), mirroring the
    sibling golden tests' source-map idiom.
    """
    return {
        p.relative_to(_MAESTRO_ROOT).as_posix(): p.read_text(encoding="utf-8")
        for p in _iter_py_files()
    }


def _config_field_names() -> set[str]:
    """Every declared ``Config`` dataclass field name."""
    return {f.name for f in dataclasses.fields(Config)}


def _config_plumbing_spans() -> list[tuple[int, int]]:
    """Return the ``(lineno, end_lineno)`` spans of ``Config.validate`` / ``from_env``.

    AST-parses ``config.py``, locates the ``Config`` ``ClassDef``, and records the
    line span of each plumbing method in its direct body. Fails loud if either
    plumbing region cannot be located — a rename would otherwise silently let
    their ``self.<field>`` references count as genuine readers, masking dead flags.
    """
    tree = ast.parse(_CONFIG_PY.read_text(encoding="utf-8"))
    config_cls = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "Config"
    )
    spans: dict[str, tuple[int, int]] = {}
    for item in config_cls.body:
        if (
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name in _PLUMBING_METHODS
        ):
            end = item.end_lineno
            assert end is not None, f"Config.{item.name} has no end_lineno"
            spans[item.name] = (item.lineno, end)
    missing = _PLUMBING_METHODS - spans.keys()
    assert not missing, (
        f"Config plumbing methods not found in config.py: {sorted(missing)}. "
        "The reader scan relies on these spans to exclude validation/construction "
        "references; update _PLUMBING_METHODS if they were renamed."
    )
    return sorted(spans.values())


def _in_spans(lineno: int, spans: list[tuple[int, int]]) -> bool:
    """Whether *lineno* falls inside any ``(start, end)`` plumbing span."""
    return any(start <= lineno <= end for start, end in spans)


def _collect_reader_field_names(
    sources: dict[str, str], plumbing_spans: list[tuple[int, int]]
) -> set[str]:
    """Every attribute name reached by a genuine ``.<name>`` read across *sources*.

    Parses each scanned source once and records every ``ast.Attribute`` access's
    ``.attr`` name — an exact syntax-tree read that, unlike a raw-text regex,
    never counts a name appearing only in a comment, docstring, or string
    literal. The ``toml_config.py`` mirror layer is skipped wholesale; a
    ``config.py`` attribute access is skipped when it sits on a line inside a
    plumbing span (``validate`` / ``from_env``), whose ``self.<field>`` references
    are construction/validation plumbing, not genuine consumers. Only
    ``config.py`` needs line-level granularity to honour the span exclusion.

    The match is receiver-blind: it counts a ``.<name>`` read on *any* object, not
    only on a ``Config`` instance, so a field whose name collides with an unrelated
    attribute elsewhere under ``maestro/`` is treated as read. See the module
    docstring for the resulting efficacy ceiling — narrowing this to a verified
    ``Config`` receiver is a deliberate non-goal of the coarse, zero-cost guard.
    """
    readers: set[str] = set()
    for rel, text in sources.items():
        if rel == _TOML_RELPATH:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if rel == _CONFIG_RELPATH and _in_spans(node.lineno, plumbing_spans):
                continue
            readers.add(node.attr)
    return readers


def find_readerless_fields(
    field_names: set[str],
    sources: dict[str, str],
    plumbing_spans: list[tuple[int, int]],
    allowlist: Iterable[str] = (),
) -> list[str]:
    """Return the sorted field names with no genuine reader in *sources*.

    Pure over its *sources* mapping (relpath -> file text) and *plumbing_spans*,
    so the real-file guard, the allowlist tripwire, and the seeded negative
    controls share one definition of "readerless". Fields in *allowlist* (any
    iterable of names) are skipped.
    """
    allowset = set(allowlist)
    readers = _collect_reader_field_names(sources, plumbing_spans)
    return sorted(
        name
        for name in field_names
        if name not in allowset and name not in readers
    )


def maestro_toml_scalar_pairs(
    data: dict[str, Any],
    *,
    external_sections: Iterable[str],
    phase_names: Iterable[str],
) -> set[tuple[str, str]]:
    """Return the scalar ``(section, key)`` pairs a ``maestro.toml`` dict flat-maps.

    Pure over an already-parsed TOML *data* dict — mirroring
    :func:`find_readerless_fields` above — so the committed-file guard and the
    seeded negative controls share one definition of "scalar-mapped pair". It
    replicates the declarative parser's flat-scalar carve-outs so the result is
    directly comparable to :func:`_section_model_pairs`:

    * a top-level section in *external_sections* is skipped wholesale — the
      ``[review]`` table is consumed by the sibling ``maestro_review`` package and
      never mirrored into ``Config``;
    * a key whose value is a nested table (``dict``) is skipped — this drops the
      hand-parsed ``[sdk.pricing.*]`` and ``[sdk.cli_paced]`` sub-tables the parser
      pulls out with dedicated helpers rather than a section-model field;
    * an ``[sdk]`` ``<phase>_provider`` key for a *phase_names* phase is skipped —
      those per-phase overrides fold into ``sdk_phase_providers`` rather than
      flat-mapping (the base ``[sdk] provider`` key stays, matching its
      ``_SdkSettings`` field).

    One deliberate divergence from the parser: ``_parse_phase_providers`` collects
    *every* ``[sdk]`` ``*_provider`` key, so a key naming a phase *not* in
    ``SDK_PHASE_NAMES`` (a typo like ``artifcat_review_provider``) reaches the
    cross-field unknown-phase raise and fails at load. This helper only carves out
    *valid*-phase provider keys, so such a typo'd key also stays in the returned
    pairs and surfaces as an unmapped key in the symmetry guard — a stricter
    carve-out kept as defence in depth.

    A top-level key whose value is not a table is not a section at all, so it is
    skipped too.
    """
    phase_provider_keys = {f"{phase}_provider" for phase in phase_names}
    external = set(external_sections)
    pairs: set[tuple[str, str]] = set()
    for section, section_data in data.items():
        if section in external:
            continue
        if not isinstance(section_data, dict):
            continue
        for key, value in section_data.items():
            if isinstance(value, dict):
                continue
            if section == "sdk" and key in phase_provider_keys:
                continue
            pairs.add((section, key))
    return pairs


def test_every_config_field_has_a_reader() -> None:
    """Every ``Config`` field has a consumer, save the documented dead-flag set.

    The dead-flag guard: a field with no ``.<field>`` reader anywhere under
    ``maestro/`` (excluding the TOML mirror and ``config.py``'s plumbing spans)
    that is not on :data:`_KNOWN_READERLESS_FIELDS` fails the build. Wire a
    consumer, delete the flag, or add a documented allowlist entry.
    """
    sources = _read_maestro_sources()
    # Vacuous-pass guard: an empty scan would let the assertion pass without
    # validating anything (mirrors the sibling golden tests).
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    orphans = find_readerless_fields(
        _config_field_names(),
        sources,
        _config_plumbing_spans(),
        allowlist=_KNOWN_READERLESS_FIELDS,
    )
    assert orphans == [], (
        "These Config fields are declared but read by no consumer under "
        f"maestro/: {orphans}. Each is dead boilerplate. Pick one of three "
        "honest dispositions: wire a consumer that reads the flag, delete the "
        "field (and its maestro.toml default + tests), or — if it is "
        "intentionally unread — add it to _KNOWN_READERLESS_FIELDS with a "
        "documented reason."
    )


def test_known_readerless_allowlist_is_current() -> None:
    """Every allowlisted dead flag still exists and still has no reader.

    The self-cleaning tripwire: the day someone wires a consumer for an
    allowlisted field, or deletes the field outright, its stale entry must turn
    the build red so the allowlist never silently misrepresents the tree.
    """
    field_names = _config_field_names()
    sources = _read_maestro_sources()
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    readerless = set(
        find_readerless_fields(field_names, sources, _config_plumbing_spans())
    )
    for field in _KNOWN_READERLESS_FIELDS:
        assert field in field_names, (
            f"Allowlisted dead flag {field!r} no longer exists on Config — "
            "remove its _KNOWN_READERLESS_FIELDS entry."
        )
        assert field in readerless, (
            f"Allowlisted dead flag {field!r} now has a reader under maestro/ — "
            "it is no longer dead. Remove its _KNOWN_READERLESS_FIELDS entry so "
            "the guard protects it going forward."
        )


def test_guard_detects_a_seeded_readerless_field() -> None:
    """Negative control: the detector fires on a dead flag, not on a real read.

    Without this, a broken scan (matching nothing, or matching everything) would
    make the clean-tree assertions above pass vacuously. Hands in-memory sources
    to the pure helper, so it touches no filesystem — and exercises both a
    genuinely-absent field (reported) and a genuinely-read field (not reported).
    """
    field_names = {"seeded_dead_flag", "seeded_live_flag"}
    sources = {
        # A module that reads only the live flag via a dotted access.
        "consumer.py": "def use(cfg):\n    return cfg.seeded_live_flag + 1\n",
        # A module that mentions neither flag as a reader.
        "unrelated.py": "def noop():\n    return 0\n",
    }
    orphans = find_readerless_fields(field_names, sources, plumbing_spans=[])
    assert orphans == ["seeded_dead_flag"]


def test_scan_excludes_mirror_and_plumbing_locations() -> None:
    """Negative control: mirror-layer and plumbing-span reads do not count.

    A ``.<field>`` access in ``toml_config.py`` (the mirror layer) or on a
    ``config.py`` line inside a plumbing span must NOT count as a genuine reader —
    otherwise every field would look live and the guard would rot into a vacuous
    pass. This exercises both exclusion branches, plus a ``config.py`` reference
    *outside* the span, which must count.
    """
    field_names = {"mirror_only", "plumbing_only", "config_reader"}
    config_text = (
        "class Config:\n"  # line 1
        "    mirror_only: int = 0\n"  # line 2 (declaration, not a dotted read)
        "    plumbing_only: int = 0\n"  # line 3
        "    def validate(self):\n"  # line 4
        "        return self.plumbing_only\n"  # line 5 (inside the plumbing span)
        "    def use(self):\n"  # line 6
        "        return self.config_reader\n"  # line 7 (outside any span)
    )
    sources = {
        _CONFIG_RELPATH: config_text,
        _TOML_RELPATH: "def parse():\n    return cfg.mirror_only\n",
    }
    # validate() spans lines 4-5, so self.plumbing_only on line 5 is excluded.
    orphans = find_readerless_fields(
        field_names, sources, plumbing_spans=[(4, 5)]
    )
    # mirror_only: only read in the mirror layer -> readerless.
    # plumbing_only: only read inside the plumbing span -> readerless.
    # config_reader: read on a config.py line outside any span -> has a reader.
    assert orphans == ["mirror_only", "plumbing_only"]


# --- Config<->TOML symmetry guard -------------------------------------------
# The dead-flag guard above pins Config fields to their consumers. This second
# guard pins the declarative section models (``_SECTION_MODELS`` in
# config_settings.py — the source of truth since proposal 106 retired the
# ``_TOML_TO_CONFIG`` mapping table) to the committed ``maestro.toml`` defaults, so
# a TOML-backed flag can only enter or leave the surface with its model field and
# default in agreement — the "defaults are for tests only" split can no longer
# silently rot. CI-only: no runtime behaviour, no state file.


def _section_model_pairs() -> set[tuple[str, str]]:
    """The ``(section, TOML key)`` pairs the declarative models mirror.

    For each ``_SECTION_MODELS`` model, every field contributes ``(section,
    Field(alias=...) or field-name)`` — the ``pydantic-settings`` analogue of the
    retired ``set(_TOML_TO_CONFIG)`` key set. Directly comparable to
    :func:`maestro_toml_scalar_pairs` (both key on the TOML key).
    """
    pairs: set[tuple[str, str]] = set()
    for section, model_cls in _SECTION_MODELS.items():
        for name, field in model_cls.model_fields.items():
            pairs.add((section, field.alias or name))
    return pairs


def test_every_section_model_field_names_a_real_config_field() -> None:
    """Every ``_SECTION_MODELS`` field name is a declared ``Config`` field.

    A model field whose name is misspelled, or references a since-deleted field,
    otherwise surfaces only late — as a ``TypeError`` at ``Config(**...)``
    construction when the real file is loaded. This pure check over two code
    constants catches it deterministically in CI. Replaces the retired
    ``_TOML_TO_CONFIG``-row guard.
    """
    config_fields = _config_field_names()
    # Vacuous-pass guard: an empty field set would let the diff pass without
    # validating anything (mirrors the sibling guards' non-emptiness checks).
    assert config_fields, "Expected declared Config dataclass fields"
    mapped_fields = {
        name for model_cls in _SECTION_MODELS.values() for name in model_cls.model_fields
    }
    assert mapped_fields, "Expected declarative section-model fields"
    orphans = sorted(mapped_fields - config_fields)
    assert orphans == [], (
        "These _SECTION_MODELS fields are not declared on Config: "
        f"{orphans}. Fix the model's field name, or add the Config field — "
        "otherwise the parsed value fails to reach Config at construction."
    )


def test_config_toml_mapping_matches_maestro_toml_keys() -> None:
    """``_SECTION_MODELS`` and the committed ``maestro.toml`` scalar keys agree.

    Bidirectional symmetry over the real committed file: every modelled
    ``(section, key)`` has a scalar ``maestro.toml`` default, and every scalar
    ``maestro.toml`` key (minus the parser's hand-parsed carve-outs) has a section-
    model field. The two directions are split so each failure mode gets an
    actionable message — a model field whose default was deleted (runtime
    ``ValidationError`` on the required field), and the silently-unmodelled case of
    a TOML key with no field.
    """
    data = tomllib.loads(_MAESTRO_TOML.read_text(encoding="utf-8"))
    pairs = maestro_toml_scalar_pairs(
        data,
        external_sections=_KNOWN_EXTERNAL_SECTIONS,
        phase_names=SDK_PHASE_NAMES,
    )
    # Vacuous-pass guard: an empty parse would let both diffs pass without
    # validating anything (mirrors the sibling real-file guards).
    assert pairs, f"Expected scalar (section, key) pairs in {_MAESTRO_TOML}"
    mapped = _section_model_pairs()
    assert mapped, "Expected declarative section-model (section, key) pairs"
    mapped_but_absent = sorted(mapped - pairs)
    assert mapped_but_absent == [], (
        "These _SECTION_MODELS fields have no scalar default in maestro.toml (a "
        f"deleted or renamed key raises ValidationError at load): {mapped_but_absent}. "
        "Add the maestro.toml default, or drop the model field."
    )
    present_but_unmapped = sorted(pairs - mapped)
    assert present_but_unmapped == [], (
        "These maestro.toml scalar keys have no _SECTION_MODELS field, so the parser "
        f"rejects them via extra=\"forbid\": {present_but_unmapped}. Add a model "
        "field (and Config field), or remove the stray key."
    )


def test_symmetry_diff_detects_seeded_asymmetric_pairs() -> None:
    """Negative control: the directional diffs fire on a seeded mismatch.

    Without this, a helper that returned the empty set (or the whole mapping)
    would make the committed-file assertions above pass vacuously. Over synthetic
    in-memory data — no filesystem — a key present in the TOML but absent from the
    mapping, and a mapping row absent from the TOML, must each surface on the
    correct side of the diff.
    """
    data: dict[str, Any] = {"loop": {"shared_knob": 1, "toml_only_knob": 2}}
    pairs = maestro_toml_scalar_pairs(
        data, external_sections=frozenset(), phase_names=frozenset()
    )
    mapping = {("loop", "shared_knob"), ("loop", "mapping_only_knob")}
    # Present in the synthetic TOML but unmapped -> surfaced by pairs - mapping.
    assert sorted(pairs - mapping) == [("loop", "toml_only_knob")]
    # Mapped but absent from the synthetic TOML -> surfaced by mapping - pairs.
    assert sorted(mapping - pairs) == [("loop", "mapping_only_knob")]


def test_scalar_pairs_excludes_external_nested_and_phase_provider_keys() -> None:
    """Negative control: the loader's carve-outs are all honoured.

    A ``[review]`` external-section key, a top-level non-table value, a nested-table
    value (the ``[sdk.pricing.*]`` / ``[sdk.cli_paced]`` shape), and an ``[sdk]``
    ``<phase>_provider`` override must all be excluded, while an ordinary scalar and
    the base ``[sdk] provider`` key are kept — otherwise the symmetry test would
    demand mapping rows for keys the loader deliberately hand-parses.
    """
    data: dict[str, Any] = {
        "review": {"model": "x"},                 # external section -> dropped
        "stray_scalar": 7,                        # top-level non-table -> dropped
        "sdk": {
            "provider": "anthropic",              # base scalar -> kept
            "implementation_provider": "openai",  # per-phase override -> dropped
            "pricing": {"m": {"input": 1.0}},     # nested table -> dropped
        },
        "loop": {"task_every": 5},                # ordinary scalar -> kept
    }
    pairs = maestro_toml_scalar_pairs(
        data,
        external_sections=frozenset({"review"}),
        phase_names=frozenset({"implementation"}),
    )
    assert pairs == {("sdk", "provider"), ("loop", "task_every")}


# ---------------------------------------------------------------------------
# Experimental-flag manifest guards (maestro/flag_lifecycle.py)
# ---------------------------------------------------------------------------


def _collect_emitted_gate_names(sources: dict[str, str]) -> set[str]:
    """Every ``gate="<name>"`` keyword-argument string literal across *sources*.

    AST-walks each scanned source and records the string value of every
    ``ast.keyword`` named ``gate`` — the ``record_gate_activation(gate=...)`` call
    sites that put a gate into ``gate_activations.jsonl``. The manifest module
    (:data:`_FLAG_LIFECYCLE_RELPATH`) is skipped wholesale: its own ``gate=``
    declarations are the manifest under test, not emitting sites, so counting them
    would let a typo'd gate validate against itself. Matching parsed keyword nodes
    rather than raw text keeps a name in a comment, docstring, or string literal
    from masquerading as an emission, and naturally excludes a plain ``gate =
    "pass"`` assignment (an ``ast.Assign``, not a keyword).
    """
    emitted: set[str] = set()
    for rel, text in sources.items():
        if rel == _FLAG_LIFECYCLE_RELPATH:
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.keyword) or node.arg != "gate":
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                emitted.add(value.value)
    return emitted


def find_orphan_manifest_gates(
    flags: Iterable[ExperimentalFlag | CutoverFlag],
    sources: dict[str, str],
) -> list[str]:
    """Return the sorted manifest ``gate`` names with no emitting call site.

    Pure over a ``{relpath: text}`` source map (mirroring
    :func:`find_readerless_fields`), so the real-file guard and the seeded negative
    control share one definition of "orphan gate". Every manifest ``gate`` that is
    not ``None`` and does not appear as a ``gate="<name>"`` keyword literal in
    *sources* (excluding the manifest module) is reported. Accepts either manifest
    record — :class:`ExperimentalFlag` or :class:`CutoverFlag`, both of which expose
    a ``.gate`` attribute — so the experimental and cut-over gate guards share one
    definition.
    """
    emitted = _collect_emitted_gate_names(sources)
    manifest_gates = {f.gate for f in flags if f.gate is not None}
    return sorted(manifest_gates - emitted)


def test_manifest_is_non_empty() -> None:
    """Vacuous-pass guard: an empty manifest would let the scans below pass
    without validating anything (mirrors the sibling guards' non-emptiness
    checks)."""
    assert EXPERIMENTAL_FLAGS, "Expected a non-empty experimental-flag manifest"


_NUM_WORDS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    # The manifest crossed twenty gate-less flags at proposal 137, so the table
    # continues through the hyphenated twenties (the regexes below capture the
    # hyphen for the same reason).
    "twenty-one": 21, "twenty-two": 22, "twenty-three": 23, "twenty-four": 24,
    "twenty-five": 25, "twenty-six": 26, "twenty-seven": 27,
    "twenty-eight": 28, "twenty-nine": 29, "thirty": 30, "thirty-one": 31,
    "thirty-two": 32,
}


def test_experimental_flags_gate_trace_count_matches_comment() -> None:
    """The ``EXPERIMENTAL_FLAGS`` attribute comment's spelled-out 'N carry a gate
    trace; M do not' counts must match the live manifest.

    The counts live in prose, not a derived value, so appending a gate-less flag
    (as ``generation_admission_enabled`` did) silently drifts the '... do not'
    tally. Line-based coverage never exercises a comment, so nothing else catches
    it — this guard ties the comment to the data so the count stays honest.
    """
    source = (_MAESTRO_ROOT / "flag_lifecycle.py").read_text(encoding="utf-8")
    # Collect the contiguous ``#:`` attribute-comment block directly above the
    # assignment (Sphinx doc-comments are not runtime-reachable via ``__doc__``).
    idx = source.index("\nEXPERIMENTAL_FLAGS:")
    comment_lines: list[str] = []
    for line in reversed(source[:idx].splitlines()):
        stripped = line.strip()
        if stripped.startswith("#:"):
            comment_lines.append(stripped[2:].strip())
        else:
            break
    comment = " ".join(reversed(comment_lines))
    trace_match = re.search(r"([\w-]+) carry a gate trace", comment)
    without_match = re.search(r"([\w-]+) do not", comment)
    assert trace_match is not None and without_match is not None, (
        f"Could not parse the gate-trace counts from the comment: {comment!r}"
    )
    stated_with = _NUM_WORDS[trace_match.group(1).lower()]
    stated_without = _NUM_WORDS[without_match.group(1).lower()]
    actual_with = sum(1 for f in EXPERIMENTAL_FLAGS if f.gate is not None)
    actual_without = sum(1 for f in EXPERIMENTAL_FLAGS if f.gate is None)
    assert stated_with == actual_with, (
        f"Comment says {stated_with} flags carry a gate trace but the manifest "
        f"has {actual_with}. Update the EXPERIMENTAL_FLAGS attribute comment."
    )
    assert stated_without == actual_without, (
        f"Comment says {stated_without} flags carry no gate trace but the "
        f"manifest has {actual_without}. Update the EXPERIMENTAL_FLAGS comment."
    )


def test_every_experimental_flag_names_a_real_config_field() -> None:
    """Every manifest ``flag`` names a declared ``Config`` field.

    The manifest keys on the ``Config`` field name (not the TOML key — the two
    diverge for some flags). A misspelled or since-deleted field would otherwise
    surface only when someone read the audit report and found a phantom row.
    Mirrors :func:`test_every_toml_mapping_names_a_real_config_field`.
    """
    config_fields = _config_field_names()
    assert config_fields, "Expected declared Config dataclass fields"
    named = {entry.flag for entry in EXPERIMENTAL_FLAGS}
    orphans = sorted(named - config_fields)
    assert orphans == [], (
        "These EXPERIMENTAL_FLAGS entries name fields that are not declared on "
        f"Config: {orphans}. Fix the manifest's flag name, or add the Config "
        "field — the manifest tracks real opt-in flags."
    )


def test_frame_discharge_bind_flag_manifested_default_off() -> None:
    """Proposal 112's ``frame_discharge_bind_enabled`` is a manifested default-off
    experiment with no activation gate.

    It ships ``false`` (:func:`test_config_frame_discharge_bind_default_off`), has a
    live reader in ``run`` (so :func:`test_every_config_field_has_a_reader` passes
    without an allowlist entry), and carries ``gate=None`` because the record-time
    cap emits no ``gate_activations.jsonl`` row — so it is correctly excluded from
    the orphan-gate scan rather than rendering a permanent false '0 fires'.
    """
    entry = next(
        (f for f in EXPERIMENTAL_FLAGS if f.flag == "frame_discharge_bind_enabled"),
        None,
    )
    assert entry is not None, (
        "frame_discharge_bind_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().frame_discharge_bind_enabled is False


def test_pre_mint_generation_gate_flag_manifested_default_off() -> None:
    """Proposal 120's ``pre_mint_generation_gate_enabled`` is a manifested
    default-off experiment with no activation gate.

    It ships ``false``, has a live reader in ``evolve`` (so
    :func:`test_every_config_field_has_a_reader` passes without an allowlist
    entry), and carries ``gate=None`` because the pre-mint gate emits no
    ``gate_activations.jsonl`` row — so it is correctly excluded from the
    orphan-gate scan. Mirrors
    :func:`test_frame_discharge_bind_flag_manifested_default_off`.
    """
    entry = next(
        (
            f
            for f in EXPERIMENTAL_FLAGS
            if f.flag == "pre_mint_generation_gate_enabled"
        ),
        None,
    )
    assert entry is not None, (
        "pre_mint_generation_gate_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().pre_mint_generation_gate_enabled is False


def test_self_model_external_anchor_flag_manifested_default_off() -> None:
    """Proposal 120 Phase C's ``self_model_external_anchor_enabled`` is a
    manifested default-off experiment with no activation gate.

    It ships ``false``, has live readers in ``contemplate``'s
    ``update_self_model`` call (``agent.py``) and the rejection-learning path
    (``evolution.py``) — so :func:`test_every_config_field_has_a_reader` passes
    without an allowlist entry — and carries ``gate=None`` because the
    prompt-only anchor emits no ``gate_activations.jsonl`` row, so it is
    correctly excluded from the orphan-gate scan. Mirrors
    :func:`test_pre_mint_generation_gate_flag_manifested_default_off`.
    """
    entry = next(
        (
            f
            for f in EXPERIMENTAL_FLAGS
            if f.flag == "self_model_external_anchor_enabled"
        ),
        None,
    )
    assert entry is not None, (
        "self_model_external_anchor_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().self_model_external_anchor_enabled is False


def test_retired_net_burn_objective_flag_absent_from_registries() -> None:
    """Proposal 155 Step 3 deleted the planning-objective flag outright, so it
    is in neither the experimental manifest nor the prod-only canaries."""
    flag = "work_net_burn_objective_enabled"
    assert flag not in {f.flag for f in EXPERIMENTAL_FLAGS}
    assert flag not in {c.flag for c in PROD_ONLY_CANARIES}


def test_host_capacity_sensor_flag_manifested_default_off() -> None:
    """Proposal 125's ``host_capacity_sensor_enabled`` is a manifested default-off
    experiment with no activation gate.

    It ships ``false``, has live readers in ``contemplate``'s fallback snapshot
    build and the loop's hoisted status snapshot (``agent.py``) — so
    :func:`test_every_config_field_has_a_reader` passes without an allowlist entry
    — and carries ``gate=None`` because folding the reading emits no
    ``gate_activations.jsonl`` row, so it is correctly excluded from the
    orphan-gate scan. Mirrors
    :func:`test_self_model_external_anchor_flag_manifested_default_off`.
    """
    entry = next(
        (f for f in EXPERIMENTAL_FLAGS if f.flag == "host_capacity_sensor_enabled"),
        None,
    )
    assert entry is not None, (
        "host_capacity_sensor_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().host_capacity_sensor_enabled is False


def test_runtime_contract_enforced_flag_manifested_default_off() -> None:
    """Proposal 141 step 8's ``runtime_contract_enforced`` is a manifested
    default-off experiment with no activation gate (read in ``cmd_preflight`` and
    ``_build_agent``). Mirrors
    :func:`test_host_capacity_sensor_flag_manifested_default_off`.
    """
    entry = next(
        (f for f in EXPERIMENTAL_FLAGS if f.flag == "runtime_contract_enforced"),
        None,
    )
    assert entry is not None, (
        "runtime_contract_enforced must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().runtime_contract_enforced is False


def test_attempt_disposition_flag_manifested_default_off() -> None:
    """Proposal 131's ``attempt_disposition_enabled`` is a manifested default-off
    experiment with no activation gate.

    It ships ``false``, has live readers in ``implement_plan``'s non-inherited
    stuck terminal (``agent.py``) and ``implement``'s resume branch path
    (``evolution.py``) — so :func:`test_every_config_field_has_a_reader` passes
    without an allowlist entry — and carries ``gate=None`` because the RESUME arm
    emits no ``gate_activations.jsonl`` row, so it is correctly excluded from the
    orphan-gate scan. Mirrors
    :func:`test_host_capacity_sensor_flag_manifested_default_off`.
    """
    entry = next(
        (f for f in EXPERIMENTAL_FLAGS if f.flag == "attempt_disposition_enabled"),
        None,
    )
    assert entry is not None, (
        "attempt_disposition_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().attempt_disposition_enabled is False


def test_grounded_reflection_flag_manifested_default_off() -> None:
    """Proposal 132's ``grounded_reflection_enabled`` is a manifested default-off
    experiment with no activation gate.

    It ships ``false`` and carries ``gate=None`` because the read-only reflection
    retrieval surface emits no ``gate_activations.jsonl`` row, so it is correctly
    excluded from the orphan-gate scan. The session-wiring increment has landed:
    the flag now has real consumers (``MaestroAgent._grounded_reflection_active``
    / ``_build_contemplation_session_options``), so it is **no longer** on
    :data:`_KNOWN_READERLESS_FIELDS` and
    :func:`test_every_config_field_has_a_reader` finds its reader directly.
    Mirrors :func:`test_attempt_disposition_flag_manifested_default_off`.
    """
    entry = next(
        (f for f in EXPERIMENTAL_FLAGS if f.flag == "grounded_reflection_enabled"),
        None,
    )
    assert entry is not None, (
        "grounded_reflection_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().grounded_reflection_enabled is False


def test_cooldown_signature_keying_flag_manifested_default_off() -> None:
    """Proposal 137's ``cooldown_signature_keying_enabled`` is a manifested
    default-off experiment with no activation gate.

    It ships ``false`` and carries ``gate=None`` because keying a cooldown to
    the signature it was stamped against writes no ``gate_activations.jsonl``
    row — it only changes whether an existing predicate returns early — so it is
    correctly excluded from the orphan-gate scan. Its readers
    (``evolution_tend._signature_released``, reached from
    ``_in_conflict_cooldown`` / ``_in_quality_fix_cooldown``) are real, so it is
    not on :data:`_KNOWN_READERLESS_FIELDS`. Mirrors
    :func:`test_grounded_reflection_flag_manifested_default_off`.
    """
    entry = next(
        (
            f
            for f in EXPERIMENTAL_FLAGS
            if f.flag == "cooldown_signature_keying_enabled"
        ),
        None,
    )
    assert entry is not None, (
        "cooldown_signature_keying_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().cooldown_signature_keying_enabled is False


def test_owed_act_binding_flag_manifested_default_off() -> None:
    """Proposal 137's ``owed_act_binding_enabled`` is a manifested default-off
    experiment with no activation gate.

    It ships ``false`` and carries ``gate=None`` because binding the two give-up
    decisions to the owed act writes no ``gate_activations.jsonl`` row — it only
    changes what an existing predicate returns (rule 21's finding text, the
    salvage verdict) — so it is correctly excluded from the orphan-gate scan.
    Its readers (``MaestroAgent._assess_pr_salvage`` and the rule-21 block in
    ``_audit_self_model``, both via ``loop.owed_act_binding``) are real, so it is
    not on :data:`_KNOWN_READERLESS_FIELDS`. Mirrors
    :func:`test_cooldown_signature_keying_flag_manifested_default_off`.
    """
    entry = next(
        (f for f in EXPERIMENTAL_FLAGS if f.flag == "owed_act_binding_enabled"),
        None,
    )
    assert entry is not None, (
        "owed_act_binding_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().owed_act_binding_enabled is False


def test_stale_repeat_bet_guard_flag_manifested_default_off() -> None:
    """The stale-repeat bet guard's ``stale_repeat_bet_guard_enabled`` is a
    manifested default-off experiment with a dated ``review_by`` and no gate.

    It ships ``false`` (criterion 4), has a live reader in ``contemplate``
    (``agent.py``) — so :func:`test_every_config_field_has_a_reader` passes without
    an allowlist entry — and carries ``gate=None`` because the prompt-only ban
    block emits no ``gate_activations.jsonl`` row, so it stays out of the
    orphan-gate scan. Mirrors
    :func:`test_grounded_reflection_flag_manifested_default_off`.
    """
    entry = next(
        (
            f
            for f in EXPERIMENTAL_FLAGS
            if f.flag == "stale_repeat_bet_guard_enabled"
        ),
        None,
    )
    assert entry is not None, (
        "stale_repeat_bet_guard_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    # A dated, valid ISO review_by (criterion 4).
    assert isinstance(parse_review_by(entry.review_by), date)
    assert Config().stale_repeat_bet_guard_enabled is False


def test_gate_lease_cgroup_reserve_flag_manifested_default_off() -> None:
    """Proposal 134's ``gate_lease_cgroup_reserve_enabled`` is a manifested
    default-off experiment with no activation gate.

    It ships ``false`` and carries ``gate=None`` because ``plan_lease`` is a pure
    policy that emits no ``gate_activations.jsonl`` row — its effect is read from
    the ``available_mib`` / ``bounded_by`` fields stamped unconditionally on every
    ``gate_runs.jsonl`` row instead — so it is correctly excluded from the
    orphan-gate scan. Mirrors
    :func:`test_grounded_reflection_flag_manifested_default_off`.
    """
    entry = next(
        (
            f
            for f in EXPERIMENTAL_FLAGS
            if f.flag == "gate_lease_cgroup_reserve_enabled"
        ),
        None,
    )
    assert entry is not None, (
        "gate_lease_cgroup_reserve_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().gate_lease_cgroup_reserve_enabled is False


def test_account_ui_v2_flag_manifested_cutover_default_on() -> None:
    """Proposal 122's ``account_ui_v2`` graduated onto the cut-over axis.

    It shipped as a default-off ``EXPERIMENTAL_FLAGS`` dark launch and was
    promoted 2026-09-03 to the base default, which moves it onto the *opposite*
    manifest: a default-on flag whose flag-off branch is live old logic (the flat
    pill nav) is a :class:`CutoverFlag`, not an experiment, so it must leave
    ``EXPERIMENTAL_FLAGS`` in the same diff — a default-on entry left on the
    default-off axis would misreport a shipped default as a pending
    promote-or-delete decision. ``stage="cutover"`` marks it as still burning in
    rather than collapse-eligible, and ``gate=None`` because the presentation
    shell emits no ``gate_activations.jsonl`` row, so it stays out of the
    orphan-gate scan on this axis too. Mirrors the sibling default-on graduation
    guards; the collapse-side structure (``off_path_symbol`` binding, growth
    budget) is held by ``tests/test_cutover_flag_collapse.py``.
    """
    assert all(f.flag != "account_ui_v2" for f in EXPERIMENTAL_FLAGS), (
        "account_ui_v2 is default-on and must not remain in EXPERIMENTAL_FLAGS"
    )
    entry = next(
        (f for f in CUTOVER_FLAGS if f.flag == "account_ui_v2"),
        None,
    )
    assert entry is not None, "account_ui_v2 must be tracked in CUTOVER_FLAGS"
    assert entry.stage == "cutover"
    assert entry.gate is None
    assert Config().account_ui_v2 is True


def test_generation_admission_flag_manifested_cutover_default_on() -> None:
    """Proposal 116's ``generation_admission_enabled`` graduated onto the cut-over axis.

    Promoted 2026-10-07 from a prod-only canary to the base default, it must
    leave ``EXPERIMENTAL_FLAGS`` and be tracked in ``CUTOVER_FLAGS`` as still
    burning in, with no ``gate_activations.jsonl`` row.
    """
    assert all(f.flag != "generation_admission_enabled" for f in EXPERIMENTAL_FLAGS)
    entry = next((f for f in CUTOVER_FLAGS if f.flag == "generation_admission_enabled"), None)
    assert entry is not None
    assert entry.stage == "cutover"
    assert entry.gate is None
    assert Config().generation_admission_enabled is True


def test_retire_unlanded_limitations_flag_manifested_cutover_default_on() -> None:
    """Proposal 116's ``retire_unlanded_limitations_enabled`` graduated onto the cut-over axis.

    Promoted 2026-10-07 from a prod-only canary to the base default, it must
    leave ``EXPERIMENTAL_FLAGS`` and be tracked in ``CUTOVER_FLAGS`` as still
    burning in, with no ``gate_activations.jsonl`` row.
    """
    assert all(f.flag != "retire_unlanded_limitations_enabled" for f in EXPERIMENTAL_FLAGS)
    entry = next((f for f in CUTOVER_FLAGS if f.flag == "retire_unlanded_limitations_enabled"), None)
    assert entry is not None
    assert entry.stage == "cutover"
    assert entry.gate is None
    assert Config().retire_unlanded_limitations_enabled is True


def test_gate_scope_impacted_flag_manifested_with_its_gate() -> None:
    """Proposal 134's ``gate_scope_impacted_enabled`` is a manifested default-off
    experiment that *does* carry an activation gate.

    Unlike its two sibling gate-fit flags, the scope seam is not a pure policy:
    ``converge_to_green`` writes exactly one ``gate_scope`` row per convergence
    naming whether the narrow was taken, so the flag's fire rate is measurable and
    the manifest must claim that gate. Mirrors
    :func:`test_gate_lease_cgroup_reserve_flag_manifested_default_off`.
    """
    entry = next(
        (f for f in EXPERIMENTAL_FLAGS if f.flag == "gate_scope_impacted_enabled"),
        None,
    )
    assert entry is not None, (
        "gate_scope_impacted_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate == "gate_scope"
    assert Config().gate_scope_impacted_enabled is False


def test_gate_environment_oracle_flag_manifested_with_its_gate() -> None:
    """Proposal 133's ``gate_environment_oracle_enabled`` is a manifested
    default-off experiment carrying an activation gate.

    ``_implement_approved_plans`` writes exactly one ``gate_environment_hold`` row
    per held iteration, so the flag's fire rate is measurable and the manifest must
    claim that gate. Mirrors
    :func:`test_gate_scope_impacted_flag_manifested_with_its_gate`.
    """
    entry = next(
        (
            f for f in EXPERIMENTAL_FLAGS
            if f.flag == "gate_environment_oracle_enabled"
        ),
        None,
    )
    assert entry is not None, (
        "gate_environment_oracle_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate == "gate_environment_hold"
    assert Config().gate_environment_oracle_enabled is False


def test_every_experimental_flag_gate_names_a_real_gate() -> None:
    """Every manifest ``gate`` names a real emitted gate string — the symmetric
    guard the ``flag``-name check lacks for the ``gate`` half.

    ``audit_flags_report`` renders ``activation_counts.get(gate, 0)``: a gate name
    that never appears at a ``record_gate_activation(gate=...)`` site under
    ``maestro/`` (a typo, or a gate renamed at its call site while the manifest kept
    the old name) collapses to a permanent ``0``, which the report defines as a live
    'zero-fire, promote-or-delete' signal. That would falsely advertise 'this
    experiment never fires, delete it' even while the gate fires under its real
    name — the exact misleading state the module exists to eliminate. There is no
    central gate registry, so this scans the real emitting call sites (excluding the
    manifest module itself), mirroring
    :func:`test_every_experimental_flag_names_a_real_config_field`.
    """
    sources = _read_maestro_sources()
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    orphans = find_orphan_manifest_gates(EXPERIMENTAL_FLAGS, sources)
    assert orphans == [], (
        "These EXPERIMENTAL_FLAGS gate names have no record_gate_activation("
        f"gate=...) call site under maestro/: {orphans}. Each would render a "
        "permanent false '0 fires' in audit-flags. Fix the manifest's gate name to "
        "match the emitting call site, or rename the call site to match."
    )


def test_gate_scan_detects_seeded_orphan_gates() -> None:
    """Negative control: the gate scan reports a manifest gate with no emitting
    call site, and never counts the manifest module's own ``gate=`` declaration
    (which would let a typo validate against itself) or a plain ``gate =``
    assignment as an emission, while a real ``record_gate_activation(gate=...)``
    site is recognised.

    Without this, a broken scan (matching nothing, or matching the manifest module)
    would make the clean-tree assertion above pass vacuously. Hands in-memory
    sources to the pure helper, so it touches no filesystem.
    """
    flags = (
        ExperimentalFlag(
            flag="live", review_by="2027-01-01", gate="emitted_gate", note="live",
        ),
        ExperimentalFlag(
            flag="self_named", review_by="2027-01-01", gate="self_gate", note="s",
        ),
        ExperimentalFlag(
            flag="assigned", review_by="2027-01-01", gate="assigned_gate", note="a",
        ),
        ExperimentalFlag(
            flag="untraced", review_by="2027-01-01", gate=None, note="untraced",
        ),
    )
    sources = {
        # A real emitting call site -> recognised.
        "emitter.py": 'record_gate_activation(gate="emitted_gate")\n',
        # The manifest module's own declaration must NOT count as an emission,
        # else a typo'd gate would validate against itself.
        _FLAG_LIFECYCLE_RELPATH: 'ExperimentalFlag(gate="self_gate")\n',
        # A plain assignment is an ast.Assign, not a keyword -> not an emission.
        "assign.py": 'gate = "assigned_gate"\n',
        # A non-"gate" keyword and a non-string gate value are both ignored.
        "other.py": 'record_gate_activation(name="x", gate=computed())\n',
    }
    orphans = find_orphan_manifest_gates(flags, sources)
    assert orphans == ["assigned_gate", "self_gate"]


def test_every_cutover_flag_gate_names_a_real_gate() -> None:
    """Every non-``None`` CUTOVER_FLAGS gate names a real emitted gate string — the
    symmetric guard the cut-over manifest lacked, mirroring
    :func:`test_every_experimental_flag_gate_names_a_real_gate` for the retirement
    half.

    ``cutover_burn_in_report`` renders ``activation_counts.get(gate, 0)`` exactly as
    ``audit_flags_report`` does, but — unlike the experimental case, which only says
    'delete it' — a past-floor, zero-count gate actively RECOMMENDS
    ``collapse_eligible``, and a collapse deletes the off-path/fallback code. A gate
    name that never appears at a ``record_gate_activation(gate=...)`` site under
    ``maestro/`` (a typo, or a gate renamed at its call site while the manifest kept
    the old name) collapses to a permanent ``0`` divergences, falsely recommending
    the collapse of a flag whose off-path may still be diverging under the real gate
    name. Every cut-over gate is ``None`` today, so this passes trivially now; the
    guard bites the moment a divergence gate is wired to an entry.
    """
    sources = _read_maestro_sources()
    assert sources, f"Expected Python sources under {_MAESTRO_ROOT}"
    orphans = find_orphan_manifest_gates(CUTOVER_FLAGS, sources)
    assert orphans == [], (
        "These CUTOVER_FLAGS gate names have no record_gate_activation(gate=...) "
        f"call site under maestro/: {orphans}. Each would render a permanent false "
        "'0 divergences' in audit-flags and could falsely recommend collapse. Fix "
        "the manifest's gate name to match the emitting call site, or rename the "
        "call site to match."
    )


def test_cutover_gate_scan_detects_seeded_orphan_gates() -> None:
    """Negative control: the shared gate scan reports a :class:`CutoverFlag`
    manifest gate with no emitting call site, recognises a real
    ``record_gate_activation(gate=...)`` site, and never counts the manifest
    module's own ``gate=`` declaration (which would let a typo validate against
    itself) or a plain ``gate =`` assignment as an emission.

    Proves :func:`find_orphan_manifest_gates` walks ``CutoverFlag.gate`` too, not
    only ``ExperimentalFlag.gate`` — without it,
    :func:`test_every_cutover_flag_gate_names_a_real_gate` (vacuous today, since
    every cut-over gate is ``None``) could pass even with a scan blind to
    ``CutoverFlag``. Hands in-memory sources to the pure helper, so it touches no
    filesystem. Mirrors :func:`test_gate_scan_detects_seeded_orphan_gates`.
    """
    flags = (
        CutoverFlag(
            flag="live", proposal=None, cutover_cycle="2026-01-01",
            off_path_symbol="live_sym", review_by="2027-01-01", stage="cutover",
            note="live", gate="emitted_gate",
        ),
        CutoverFlag(
            flag="self_named", proposal=None, cutover_cycle="2026-01-01",
            off_path_symbol="self_sym", review_by="2027-01-01", stage="cutover",
            note="s", gate="self_gate",
        ),
        CutoverFlag(
            flag="assigned", proposal=None, cutover_cycle="2026-01-01",
            off_path_symbol="assigned_sym", review_by="2027-01-01",
            stage="cutover", note="a", gate="assigned_gate",
        ),
        CutoverFlag(
            flag="untraced", proposal=None, cutover_cycle="2026-01-01",
            off_path_symbol="untraced_sym", review_by="2027-01-01",
            stage="cutover", note="untraced", gate=None,
        ),
    )
    sources = {
        # A real emitting call site -> recognised.
        "emitter.py": 'record_gate_activation(gate="emitted_gate")\n',
        # The manifest module's own declaration must NOT count as an emission,
        # else a typo'd gate would validate against itself.
        _FLAG_LIFECYCLE_RELPATH: 'CutoverFlag(gate="self_gate")\n',
        # A plain assignment is an ast.Assign, not a keyword -> not an emission.
        "assign.py": 'gate = "assigned_gate"\n',
        # A non-"gate" keyword and a non-string gate value are both ignored.
        "other.py": 'record_gate_activation(name="x", gate=computed())\n',
    }
    orphans = find_orphan_manifest_gates(flags, sources)
    assert orphans == ["assigned_gate", "self_gate"]


def test_every_experimental_flag_review_by_is_valid_iso() -> None:
    """``parse_review_by`` accepts every manifest ``review_by`` — a typo tripwire.

    A malformed date would otherwise sort as never-overdue and silently disable
    the staleness guard for that flag.
    """
    for entry in EXPERIMENTAL_FLAGS:
        parsed = parse_review_by(entry.review_by)
        assert isinstance(parsed, date)


def test_parse_review_by_rejects_malformed_date() -> None:
    """``parse_review_by`` fails loud on a malformed date rather than silently
    sorting it as never-overdue (matching the fail-loud invariant)."""
    with pytest.raises(ValueError, match="Invalid review_by date"):
        parse_review_by("not-a-date")


def test_no_experimental_flag_is_overdue() -> None:
    """CI staleness guard: no manifest entry has crossed its review horizon.

    This is the forcing function — the day a ``review_by`` date passes, the build
    goes red until a human promotes the flag (delete the switch, keep the
    behaviour), deletes it (drop both), or re-dates the entry with a rationale.
    """
    overdue = find_overdue_flags(EXPERIMENTAL_FLAGS, FREEZE_DATE)
    overdue_names = sorted(entry.flag for entry in overdue)
    assert overdue_names == [], (
        "These experimental flags have passed their review_by horizon: "
        f"{overdue_names}. Promote (delete the switch), delete (drop the flag), "
        "or re-date the manifest entry with a rationale."
    )


def test_overdue_detector_fires_on_seeded_past_horizon() -> None:
    """Negative control: the overdue detector flags a past date and not a future
    one, over synthetic entries and a *fixed* reference date — so the detection
    logic is exercised independent of the wall clock."""
    reference = date(2026, 6, 15)
    past = ExperimentalFlag(
        flag="past_flag", review_by="2026-01-01", gate=None, note="past",
    )
    future = ExperimentalFlag(
        flag="future_flag", review_by="2027-01-01", gate=None, note="future",
    )
    overdue = find_overdue_flags([past, future], reference)
    assert [entry.flag for entry in overdue] == ["past_flag"]


def test_audit_flags_report_marks_overdue_row_and_joins_activations() -> None:
    """``audit_flags_report`` sets ``overdue`` True for a crossed-horizon entry
    and False for a future one, over a *fixed* reference date, while joining the
    gate fire-rate.

    Every CLI test drives the report through the (patched) real clock against the seeded
    all-future manifest, so the ``overdue: True`` row is otherwise never
    exercised. coverage.py does not split the one-line ``f.flag in overdue``
    conditional into branches, so the 100% gate would pass with that true side
    dead — inverting the horizon comparison, or a wrong marker, would ship
    silently. This drives the True side deterministically, no wall-clock read.
    """
    reference = date(2026, 6, 15)
    past = ExperimentalFlag(
        flag="past_flag", review_by="2026-01-01", gate="fired_gate", note="past",
    )
    future = ExperimentalFlag(
        flag="future_flag", review_by="2027-01-01", gate=None, note="future",
    )
    rows = audit_flags_report([past, future], {"fired_gate": 3}, reference)
    by_flag = {row["flag"]: row for row in rows}
    assert by_flag["past_flag"]["overdue"] is True
    assert by_flag["past_flag"]["activations"] == 3
    assert by_flag["future_flag"]["overdue"] is False
    assert by_flag["future_flag"]["activations"] is None


def test_find_horizons_within_selects_approaching_and_overdue() -> None:
    """The push-side selector (proposal 117) returns entries within the window —
    including already-overdue ones (negative delta) — and excludes a comfortably
    distant horizon, over synthetic entries and a *fixed* reference date.

    Mirrors :func:`test_overdue_detector_fires_on_seeded_past_horizon`: deterministic
    (caller-supplied *today*), so the window arithmetic is exercised independent of
    the wall clock. Uses the default 30-day window.
    """
    reference = date(2026, 6, 15)
    overdue = ExperimentalFlag(
        flag="overdue_flag", review_by="2026-06-01", gate=None, note="past",
    )
    approaching = ExperimentalFlag(
        flag="approaching_flag", review_by="2026-07-01", gate=None, note="near",
    )
    distant = ExperimentalFlag(
        flag="distant_flag", review_by="2027-01-01", gate=None, note="far",
    )
    due = find_horizons_within([overdue, approaching, distant], reference)
    assert sorted(f.flag for f in due) == ["approaching_flag", "overdue_flag"]


def test_find_horizons_within_respects_custom_window() -> None:
    """A tighter *within_days* excludes a horizon the default window would include —
    exercising the parameterized window, not just the default."""
    reference = date(2026, 6, 15)
    approaching = ExperimentalFlag(
        flag="approaching_flag", review_by="2026-07-01", gate=None, note="near",
    )
    due = find_horizons_within([approaching], reference, within_days=5)
    assert due == []


def test_format_flag_horizon_summary_renders_marks_and_self_suppresses() -> None:
    """The formatter (proposal 117) renders a ``FLAG-HORIZON`` line naming an
    approaching flag with its remaining days and marking an overdue one ``OVERDUE``,
    and returns ``""`` when no flag is near or past its horizon.

    Fixed reference date so the day-count and the empty-window suppression are both
    exercised deterministically, no wall-clock read.
    """
    reference = date(2026, 6, 15)
    overdue = ExperimentalFlag(
        flag="overdue_flag", review_by="2026-06-01", gate=None, note="past",
    )
    approaching = ExperimentalFlag(
        flag="approaching_flag", review_by="2026-07-01", gate=None, note="near",
    )
    distant = ExperimentalFlag(
        flag="distant_flag", review_by="2027-01-01", gate=None, note="far",
    )
    line = format_flag_horizon_summary([overdue, approaching, distant], reference)
    assert line.startswith("FLAG-HORIZON: ")
    assert "overdue_flag OVERDUE (was 2026-06-01)" in line
    assert "approaching_flag due 2026-07-01 (16d)" in line
    assert "distant_flag" not in line
    # Self-suppression: a window holding no due flag renders nothing.
    assert format_flag_horizon_summary([distant], reference) == ""


def test_format_flag_horizon_summary_real_manifest_shape() -> None:
    """Smoke over the live ``EXPERIMENTAL_FLAGS`` with the real clock: the sensor
    either self-suppresses or renders a well-formed ``FLAG-HORIZON`` line.

    Asserts shape, not content — the manifest's real dates move over time, so the
    exact due-set is not pinned; what must hold is that the sensor never emits a
    malformed line.
    """
    line = format_flag_horizon_summary(EXPERIMENTAL_FLAGS, FREEZE_DATE)
    assert line == "" or line.startswith("FLAG-HORIZON: ")


def test_flag_horizon_display_flag_manifested_default_off() -> None:
    """Proposal 117's ``audit_flag_horizon_display_enabled`` is a manifested
    default-off experiment with no activation gate.

    It ships ``false``, has a live reader in ``contemplate`` (so
    :func:`test_every_config_field_has_a_reader` passes without an allowlist entry),
    and carries ``gate=None`` because the pure display sensor emits no
    ``gate_activations.jsonl`` row. Mirrors
    :func:`test_frame_discharge_bind_flag_manifested_default_off`.
    """
    entry = next(
        (
            f
            for f in EXPERIMENTAL_FLAGS
            if f.flag == "audit_flag_horizon_display_enabled"
        ),
        None,
    )
    assert entry is not None, (
        "audit_flag_horizon_display_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().audit_flag_horizon_display_enabled is False


def test_split_due_sensor_flag_manifested_default_off() -> None:
    """Proposal 147's ``audit_split_due_sensor_enabled`` is a manifested
    default-off experiment with no activation gate — the read-only display
    sensor emits no ``gate_activations.jsonl`` row. Mirrors
    :func:`test_flag_horizon_display_flag_manifested_default_off`.
    """
    entry = next(
        (f for f in EXPERIMENTAL_FLAGS if f.flag == "audit_split_due_sensor_enabled"),
        None,
    )
    assert entry is not None, (
        "audit_split_due_sensor_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().audit_split_due_sensor_enabled is False


def test_split_due_directive_flag_manifested_default_off() -> None:
    """Proposal 147's split-due directive bridge is a manifested default-off
    experiment whose fills are traced under the ``split_due_directive`` gate.
    """
    entry = next(
        (f for f in EXPERIMENTAL_FLAGS if f.flag == "audit_split_due_directive_enabled"),
        None,
    )
    assert entry is not None
    assert entry.gate == "split_due_directive"
    assert Config().audit_split_due_directive_enabled is False


def test_self_calibration_flag_manifested_default_off() -> None:
    """Proposal 129's ``audit_self_calibration_enabled`` is a manifested default-off
    experiment with no activation gate.

    It ships ``false``, has live readers in ``cli._build_agent`` and
    ``agent.contemplate`` (so :func:`test_every_config_field_has_a_reader` passes
    without an allowlist entry), and carries ``gate=None`` because the prompt-shape
    change plus ledger-field write emits no ``gate_activations.jsonl`` row. Mirrors
    :func:`test_flag_horizon_display_flag_manifested_default_off`.
    """
    entry = next(
        (
            f
            for f in EXPERIMENTAL_FLAGS
            if f.flag == "audit_self_calibration_enabled"
        ),
        None,
    )
    assert entry is not None, (
        "audit_self_calibration_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().audit_self_calibration_enabled is False


def test_calibration_verdict_authority_flag_manifested_default_off() -> None:
    """Proposal 129's ``audit_calibration_verdict_authority_enabled`` is a manifested
    default-off experiment that carries a gate trace.

    It ships ``false``, has a live reader in ``agent.contemplate`` (so
    :func:`test_every_config_field_has_a_reader` passes without an allowlist entry),
    and carries ``gate="calibration_verdict_authority"`` because each measured
    verdict stamps a ``gate_activations.jsonl`` row. Distinct from the
    input-capture flag ``audit_self_calibration_enabled`` — the two roll out
    independently.
    """
    entry = next(
        (
            f
            for f in EXPERIMENTAL_FLAGS
            if f.flag == "audit_calibration_verdict_authority_enabled"
        ),
        None,
    )
    assert entry is not None, (
        "audit_calibration_verdict_authority_enabled must be tracked in "
        "EXPERIMENTAL_FLAGS"
    )
    assert entry.gate == "calibration_verdict_authority"
    assert Config().audit_calibration_verdict_authority_enabled is False


def test_evidence_bound_claims_flag_manifested_default_off() -> None:
    """Proposal 129's ``audit_evidence_bound_claims_enabled`` is a manifested
    default-off experiment with no activation gate.

    It ships ``false``, has a live reader in ``agent.contemplate`` (so
    :func:`test_every_config_field_has_a_reader` passes without an allowlist entry),
    and carries ``gate=None`` because the prompt-shape change plus the
    ``claim_evidence`` map write emits no ``gate_activations.jsonl`` row. Mirrors
    :func:`test_self_calibration_flag_manifested_default_off`.
    """
    entry = next(
        (
            f
            for f in EXPERIMENTAL_FLAGS
            if f.flag == "audit_evidence_bound_claims_enabled"
        ),
        None,
    )
    assert entry is not None, (
        "audit_evidence_bound_claims_enabled must be tracked in EXPERIMENTAL_FLAGS"
    )
    assert entry.gate is None
    assert Config().audit_evidence_bound_claims_enabled is False


def test_refutation_accountability_flag_manifested_default_off() -> None:
    """Proposal 129 Phase C's ``audit_refutation_accountability_enabled`` is a
    manifested default-off experiment that carries a gate trace.

    It ships ``false``, has a live reader in ``agent._audit_self_model`` (so
    :func:`test_every_config_field_has_a_reader` passes without an allowlist entry),
    and carries ``gate="refutation_accountability"`` because each mechanically
    overturned refutation stamps a ``gate_activations.jsonl`` row. Mirrors
    :func:`test_calibration_verdict_authority_flag_manifested_default_off`.
    """
    entry = next(
        (
            f
            for f in EXPERIMENTAL_FLAGS
            if f.flag == "audit_refutation_accountability_enabled"
        ),
        None,
    )
    assert entry is not None, (
        "audit_refutation_accountability_enabled must be tracked in "
        "EXPERIMENTAL_FLAGS"
    )
    assert entry.gate == "refutation_accountability"
    assert Config().audit_refutation_accountability_enabled is False


def test_prod_only_enabled_flags_are_all_declared() -> None:
    """Every flag enabled in ``maestro.production.toml`` but off in base is declared.

    The environment-overlay analogue of the base-flag guards: computes the real
    prod-only-enabled set over the committed base ``maestro.toml`` and the
    base+production merge (building each ``Config`` via
    ``build_config_via_pydantic`` — the same real-file parse the runtime uses) and
    asserts it equals the **union** of the two declared manifests. An undeclared
    prod-only enable reds the build as accidental drift; a manifest entry no longer
    prod-only-enabled reds as a stale entry — self-cleaning both ways, like
    ``test_known_readerless_allowlist_is_current``. Also asserts the two manifests are
    disjoint (a flag is either a canary or a permanent divergence, never both).
    """
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    base_flat = {f.name: getattr(base_config, f.name) for f in dataclasses.fields(Config)}
    overlay_flat = {
        f.name: getattr(prod_config, f.name) for f in dataclasses.fields(Config)
    }
    prod_only = enabled_only_in_overlay(base_flat, overlay_flat)
    # Vacuous-pass guard: an empty parse would let the set-equality pass without
    # validating anything (mirrors the sibling real-file guards).
    assert prod_only, (
        "Expected a non-empty prod-only-enabled set from the base vs. "
        "base+production merge — an empty set means the overlay load broke."
    )
    canaries = {c.flag for c in PROD_ONLY_CANARIES}
    operational = {d.flag for d in PROD_ONLY_OPERATIONAL}
    assert canaries.isdisjoint(operational), (
        "A flag appears in both PROD_ONLY_CANARIES and PROD_ONLY_OPERATIONAL: "
        f"{sorted(canaries & operational)}. Each prod-only enable is either a "
        "time-boxed canary or a permanent divergence, never both."
    )
    declared = canaries | operational
    undeclared = sorted(prod_only - declared)
    assert undeclared == [], (
        "These flags are enabled in maestro.production.toml but off in base "
        f"maestro.toml, yet declared in neither manifest: {undeclared}. Accidental "
        "prod-only drift — either revert the enable, or declare it as an "
        "EnvOverlayCanary (with a dated review_by) or an EnvOverlayDivergence (a "
        "permanent operational divergence)."
    )
    stale = sorted(declared - prod_only)
    assert stale == [], (
        "These manifest entries declare a prod-only enable that no longer exists "
        f"(the flag is off in production, or now on in base too): {stale}. Remove "
        "the stale PROD_ONLY_CANARIES / PROD_ONLY_OPERATIONAL entry."
    )


def test_prod_only_disabled_flags_are_all_declared() -> None:
    """Every flag on in base but off in ``maestro.production.toml`` is declared.

    The subtractive mirror of
    :func:`test_prod_only_enabled_flags_are_all_declared`, closing that guard's
    disable-direction blind spot: it computes the real
    prod-only-*disabled* set over the committed base ``maestro.toml`` and the
    base+production merge (both built with ``build_config_via_pydantic``, the
    real-file parse the runtime uses) and asserts it equals
    :data:`PROD_ONLY_DISABLED`. An undeclared prod-disable reds the build as
    accidental drift; an entry whose flag is no longer prod-only-disabled reds as
    stale — self-cleaning both ways. Also asserts the subtractive manifest is
    disjoint from both additive manifests: a flag cannot be prod-enabled and
    prod-disabled at once.
    """
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    base_flat = {
        f.name: getattr(base_config, f.name) for f in dataclasses.fields(Config)
    }
    overlay_flat = {
        f.name: getattr(prod_config, f.name) for f in dataclasses.fields(Config)
    }
    prod_disabled = disabled_only_in_overlay(base_flat, overlay_flat)
    # Vacuous-pass guard: an empty parse would let the set-equality pass without
    # validating anything (mirrors the additive sibling).
    assert prod_disabled, (
        "Expected a non-empty prod-only-disabled set from the base vs. "
        "base+production merge — an empty set means the overlay load broke."
    )
    declared = {c.flag for c in PROD_ONLY_DISABLED}
    additive = {c.flag for c in PROD_ONLY_CANARIES} | {
        d.flag for d in PROD_ONLY_OPERATIONAL
    }
    assert declared.isdisjoint(additive), (
        "A flag appears in both PROD_ONLY_DISABLED and an additive overlay "
        f"manifest: {sorted(declared & additive)}. A prod-only override is either "
        "an enable or a disable, never both."
    )
    undeclared = sorted(prod_disabled - declared)
    assert undeclared == [], (
        "These flags are on by default in base maestro.toml but turned off in "
        f"maestro.production.toml, yet declared nowhere: {undeclared}. Accidental "
        "prod-only drift — either revert the override, or declare it in "
        "PROD_ONLY_DISABLED with a dated review_by and a justification."
    )
    stale = sorted(declared - prod_disabled)
    assert stale == [], (
        "These PROD_ONLY_DISABLED entries declare a prod-only disable that no "
        "longer exists (the flag is on in production, or now off in base too): "
        f"{stale}. Remove the stale entry."
    )


def test_no_prod_only_disabled_flag_is_overdue() -> None:
    """CI staleness guard: no prod-only *disabled* flag has crossed its horizon.

    The subtractive half of :func:`test_no_prod_only_canary_is_overdue`, reusing
    ``find_overdue_flags`` (generic over any ``review_by``-carrying record, so the
    new manifest gets horizon enforcement for free). The day a dormancy lever's or
    kill-switch's horizon passes, the build reds until a human graduates it (lands
    the retirement/collapse and drops the override), reverts the override, or
    re-dates the entry with a rationale — the forcing function a burn-in canary left
    diverged past its observation window otherwise lacks entirely.
    """
    overdue = find_overdue_flags(PROD_ONLY_DISABLED, FREEZE_DATE)
    overdue_names = sorted(entry.flag for entry in overdue)
    assert overdue_names == [], (
        "These prod-only disabled flags have passed their review_by horizon: "
        f"{overdue_names}. Land the graduation/retirement that removes the "
        "override, revert the production disable, or re-date the "
        "PROD_ONLY_DISABLED entry with a rationale."
    )


def test_every_prod_only_disabled_flag_carries_justification() -> None:
    """Every prod-only disabled entry names why the divergence is production-scoped.

    Reuses ``find_unjustified_canaries`` with an **empty** baseline: unlike
    :data:`PROD_ONLY_CANARIES`, this manifest was authored after the justification
    discipline existed, so nothing is grandfathered and every entry — seeded or new
    — must carry a non-empty ``justification``. The non-empty manifest assertion
    keeps an emptied :data:`PROD_ONLY_DISABLED` from passing this vacuously.
    """
    assert PROD_ONLY_DISABLED, (
        "Expected a non-empty PROD_ONLY_DISABLED manifest — an empty manifest "
        "would pass the justification guard vacuously."
    )
    unjustified = find_unjustified_canaries(PROD_ONLY_DISABLED, frozenset())
    assert unjustified == [], (
        "These prod-only disabled flags carry no justification: "
        f"{unjustified}. A production override must name why the divergence is "
        "scoped to production rather than flipped in base maestro.toml."
    )


#: The issue #3280 landing-credit grounding canary's flag set: Tier 1's two
#: affirmative audit rules plus Tier 2's write-time downgrade. Tier 3's mechanical
#: actuators (``session_landing_enabled``, ``mergeable_merge_request_enabled``,
#: ``obligation_fork_floor_enabled``) are deliberately **not** here — they change
#: outward behaviour and are held for a separate PR landed only after this canary
#: has been observed.
_LANDING_CREDIT_CANARY_FLAGS: frozenset[str] = frozenset(
    {
        "undercredited_landing_enabled",
        "reconcile_near_miss_enabled",
        "prestamp_merge_truth_gate_enabled",
    },
)


def test_landing_credit_canaries_declared() -> None:
    """Issue #3280's landing-credit flags are prod-only enables with justified horizons.

    Pins all three axes the canary rests on, so deleting any one reds the build:
    each flag is ``True`` in the base+production merge, still ``False`` in base
    ``maestro.toml`` (every non-production environment unchanged), and declared as a
    :class:`EnvOverlayCanary` in :data:`PROD_ONLY_CANARIES` with a parseable dated
    ``review_by`` and a non-empty ``justification`` (required for any post-baseline
    entry — see :func:`test_new_prod_only_canary_carries_justification`). Both
    ``Config`` objects are built with ``build_config_via_pydantic``, the same
    real-file parse the runtime uses, so this asserts resolved values rather than
    file text. The disable-direction blind spot of
    :func:`test_prod_only_enabled_flags_are_all_declared` is why this direct pin
    exists: that guard classifies only prod-only *enables*, so it would not notice
    these three lines being dropped from the overlay.
    """
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    canaries = {c.flag: c for c in PROD_ONLY_CANARIES}
    for flag in sorted(_LANDING_CREDIT_CANARY_FLAGS):
        assert getattr(prod_config, flag) is True, (
            f"{flag} must be enabled in maestro.production.toml — it is one of "
            "issue #3280's landing-credit grounding canary flags."
        )
        assert getattr(base_config, flag) is False, (
            f"{flag} must stay false in base maestro.toml — the canary is a "
            "production-only observation, not a base default flip."
        )
        entry = canaries.get(flag)
        assert entry is not None, (
            f"{flag} is enabled prod-only but carries no PROD_ONLY_CANARIES entry."
        )
        assert entry.env == "production"
        assert parse_review_by(entry.review_by) > date(2026, 9, 16)
        assert entry.justification != "", (
            f"{flag} is a post-baseline prod-only canary and must name an explicit "
            "observability/operational justification."
        )
        assert flag not in PROD_ONLY_CANARY_BASELINE, (
            f"{flag} is a new canary and must not be grandfathered into the "
            "2026-08-24 baseline freeze."
        )


def test_single_use_evolution_calls_canary_declared() -> None:
    """Proposal 139's single-use evolution calls flag is a justified prod-only canary.

    Pins the three axes the canary rests on, mirroring
    :func:`test_landing_credit_canaries_declared`: the flag resolves ``True`` in
    the base+production merge, stays ``False`` in base ``maestro.toml``, and is
    declared as a post-baseline :class:`EnvOverlayCanary` with a dated
    ``review_by`` and a non-empty ``justification``. Dropping the overlay line
    reds here even though the enable-direction guard would stay silent.
    """
    flag = "sdk_single_use_evolution_calls_enabled"
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    canaries = {c.flag: c for c in PROD_ONLY_CANARIES}
    assert prod_config.sdk_single_use_evolution_calls_enabled is True
    assert base_config.sdk_single_use_evolution_calls_enabled is False
    entry = canaries.get(flag)
    assert entry is not None
    assert entry.env == "production"
    assert parse_review_by(entry.review_by) > date(2026, 9, 27)
    assert entry.justification != ""
    assert flag not in PROD_ONLY_CANARY_BASELINE


def test_owed_act_chokepoint_canaries_declared() -> None:
    """Proposal 137's two owed-act flags are justified prod-only canaries.

    Mirrors :func:`test_single_use_evolution_calls_canary_declared` for
    ``cooldown_signature_keying_enabled`` and ``owed_act_binding_enabled``: each
    resolves ``True`` in the base+production merge, ``False`` in base, and is a
    post-baseline :class:`EnvOverlayCanary` with a dated ``review_by`` and a
    non-empty ``justification``. Also pins that the canary does not retune
    ``cooldown_backoff_base_hours`` in production.
    """
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    canaries = {c.flag: c for c in PROD_ONLY_CANARIES}
    for flag in ("cooldown_signature_keying_enabled", "owed_act_binding_enabled"):
        assert getattr(prod_config, flag) is True
        assert getattr(base_config, flag) is False
        entry = canaries.get(flag)
        assert entry is not None
        assert entry.env == "production"
        assert parse_review_by(entry.review_by) > date(2026, 9, 30)
        assert entry.justification != ""
        assert flag not in PROD_ONLY_CANARY_BASELINE
    assert (
        prod_config.cooldown_backoff_base_hours
        == base_config.cooldown_backoff_base_hours
    )


def test_gate_environment_oracle_canary_declared() -> None:
    """Proposal 133's gate-runnability oracle flag is a justified prod-only canary.

    Mirrors :func:`test_single_use_evolution_calls_canary_declared`: the flag
    resolves ``True`` in the base+production merge, stays ``False`` in base
    ``maestro.toml``, and is declared as a post-baseline :class:`EnvOverlayCanary`
    with a dated ``review_by`` and a non-empty ``justification``.
    """
    flag = "gate_environment_oracle_enabled"
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    canaries = {c.flag: c for c in PROD_ONLY_CANARIES}
    assert prod_config.gate_environment_oracle_enabled is True
    assert base_config.gate_environment_oracle_enabled is False
    entry = canaries.get(flag)
    assert entry is not None
    assert entry.env == "production"
    assert parse_review_by(entry.review_by) > date(2026, 9, 30)
    assert entry.justification != ""
    assert flag not in PROD_ONLY_CANARY_BASELINE


def test_gate_budget_lease_owned_canary_declared() -> None:
    """Proposal 134's first-stage deadline flag is a justified prod-only canary.

    Mirrors :func:`test_gate_environment_oracle_canary_declared`: the flag
    resolves ``True`` in the base+production merge, stays ``False`` in base
    ``maestro.toml``, and is declared as a post-baseline :class:`EnvOverlayCanary`
    with a dated ``review_by`` and a non-empty ``justification``.
    """
    flag = "gate_budget_lease_owned_enabled"
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    canaries = {c.flag: c for c in PROD_ONLY_CANARIES}
    assert prod_config.gate_budget_lease_owned_enabled is True
    assert base_config.gate_budget_lease_owned_enabled is False
    entry = canaries.get(flag)
    assert entry is not None
    assert entry.env == "production"
    assert parse_review_by(entry.review_by) > date(2026, 9, 30)
    assert entry.justification != ""
    assert flag not in PROD_ONLY_CANARY_BASELINE


def test_declared_effort_canary_declared() -> None:
    """Proposal 140's two declared-effort flags are justified prod-only canaries.

    Mirrors :func:`test_single_use_evolution_calls_canary_declared`: both flags
    resolve ``True`` in the base+production merge, stay ``False`` in base
    ``maestro.toml``, and are declared as post-baseline
    :class:`EnvOverlayCanary` entries with a dated ``review_by`` and a non-empty
    ``justification``.
    """
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    canaries = {c.flag: c for c in PROD_ONLY_CANARIES}
    assert prod_config.self_review_own_session_enabled is True
    assert prod_config.sdk_call_policy_resolution_enabled is True
    assert base_config.self_review_own_session_enabled is False
    assert base_config.sdk_call_policy_resolution_enabled is False
    for flag in (
        "self_review_own_session_enabled",
        "sdk_call_policy_resolution_enabled",
    ):
        entry = canaries.get(flag)
        assert entry is not None
        assert entry.env == "production"
        assert parse_review_by(entry.review_by) > date(2026, 9, 30)
        assert entry.justification != ""
        assert flag not in PROD_ONLY_CANARY_BASELINE


def test_enforced_tool_surface_canary_declared() -> None:
    """Proposal 142's enforced tool-surface flag is a justified prod-only canary.

    Mirrors :func:`test_declared_effort_canary_declared`: the flag resolves
    ``True`` in the base+production merge, stays ``False`` in base
    ``maestro.toml``, and is a post-baseline :class:`EnvOverlayCanary` entry
    with a dated ``review_by`` and a non-empty ``justification``.
    """
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    canaries = {c.flag: c for c in PROD_ONLY_CANARIES}
    assert prod_config.sdk_enforced_tool_surface_enabled is True
    assert base_config.sdk_enforced_tool_surface_enabled is False
    entry = canaries.get("sdk_enforced_tool_surface_enabled")
    assert entry is not None
    assert entry.env == "production"
    assert parse_review_by(entry.review_by) > date(2026, 9, 30)
    assert entry.justification != ""
    assert "sdk_enforced_tool_surface_enabled" not in PROD_ONLY_CANARY_BASELINE


def test_grounded_planning_canary_declared() -> None:
    """Proposal 143's grounded planning flag is a justified prod-only canary.

    Mirrors :func:`test_enforced_tool_surface_canary_declared`: the flag resolves
    ``True`` in the base+production merge, stays ``False`` in base
    ``maestro.toml``, and is a post-baseline :class:`EnvOverlayCanary` entry
    with a dated ``review_by`` and a non-empty ``justification``. The caller
    list stays at its base ``propose_plan``-only default, so the later-stage callers stay
    dark.
    """
    flag = "grounded_planning_enabled"
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    canaries = {c.flag: c for c in PROD_ONLY_CANARIES}
    assert prod_config.grounded_planning_enabled is True
    assert base_config.grounded_planning_enabled is False
    assert prod_config.grounded_planning_callers == ["propose_plan"]
    entry = canaries.get(flag)
    assert entry is not None
    assert entry.env == "production"
    assert parse_review_by(entry.review_by) > date(2026, 10, 3)
    assert entry.justification != ""
    assert flag not in PROD_ONLY_CANARY_BASELINE


def test_plan_feedback_canary_declared() -> None:
    """Proposal 145's reviewer feedback flag is a justified prod-only canary.

    Mirrors :func:`test_grounded_planning_canary_declared`: the flag resolves
    ``True`` in the base+production merge, stays ``False`` in base
    ``maestro.toml``, and is a post-baseline :class:`EnvOverlayCanary` entry
    with a dated ``review_by`` and a non-empty ``justification``.
    """
    flag = "loop_plan_feedback_enabled"
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    canaries = {c.flag: c for c in PROD_ONLY_CANARIES}
    assert prod_config.loop_plan_feedback_enabled is True
    assert base_config.loop_plan_feedback_enabled is False
    entry = canaries.get(flag)
    assert entry is not None
    assert entry.env == "production"
    assert parse_review_by(entry.review_by) > date(2026, 10, 5)
    assert entry.justification != ""
    assert flag not in PROD_ONLY_CANARY_BASELINE


def test_generator_reality_grounding_canary_declared() -> None:
    """Proposal 136's generator reality grounding flag is a justified prod-only canary.

    Mirrors :func:`test_grounded_planning_canary_declared`: the flag resolves
    ``True`` in the base+production merge, stays ``False`` in base
    ``maestro.toml``, and is a post-baseline :class:`EnvOverlayCanary` entry
    with a dated ``review_by`` and a non-empty ``justification``.
    """
    flag = "generator_reality_grounding_enabled"
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    canaries = {c.flag: c for c in PROD_ONLY_CANARIES}
    assert prod_config.generator_reality_grounding_enabled is True
    assert base_config.generator_reality_grounding_enabled is False
    entry = canaries.get(flag)
    assert entry is not None
    assert entry.env == "production"
    assert parse_review_by(entry.review_by) > date(2026, 10, 7)
    assert entry.justification != ""
    assert flag not in PROD_ONLY_CANARY_BASELINE


def test_landing_credit_tier_three_actuators_stay_off() -> None:
    """Tier 3's mechanical landing actuators are not flipped by this canary.

    The staging is the point: Tier 1/2 are sensor and write-time-grading changes
    with no outward act, whereas ``session_landing_enabled``,
    ``mergeable_merge_request_enabled`` and ``obligation_fork_floor_enabled`` push
    commits, message Thomas, and tighten the verdict floor. They land in a separate
    human-reviewed PR only after this canary has been observed, so an accidental
    same-PR flip — which would collapse the observation window and put outward
    behaviour live unobserved — reds the build here.
    """
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    for flag in (
        "session_landing_enabled",
        "mergeable_merge_request_enabled",
        "obligation_fork_floor_enabled",
    ):
        assert getattr(prod_config, flag) is False, (
            f"{flag} is a Tier 3 actuator — it must stay off until the Tier 1/2 "
            "landing-credit canary has been observed (see the 'Landing-credit "
            "grounding canary (issue #3280)' subsection of docs/self-evolution.md)."
        )
        assert getattr(base_config, flag) is False


def test_prod_overlay_pins_tend_main_merge_off() -> None:
    """The production overlay pins ``tend_main_merge_enabled`` off; base stays on.

    Proposal 128's merge-queue cutover retires the tend phase's eager
    ``origin/main`` merge-and-push in ``MAESTRO_ENV=production`` only, via a
    ``tend_main_merge_enabled = false`` line in ``maestro.production.toml``'s
    ``[loop]`` section (both configs built with ``build_config_via_pydantic`` —
    the same real-file parse the runtime uses). This pins that override so an
    accidental deletion of the overlay line reds the build. The enable-only
    overlay drift guard (:func:`test_prod_only_enabled_flags_are_all_declared`)
    cannot catch a removed ``= false`` — it classifies only ``False``->``True``
    prod-only *enables* — so this direct pin is the guard for the disable
    direction. The companion base assertion confirms the committed default stays
    ``True`` as the instant, code-free rollback.
    """
    prod_config = build_config_via_pydantic(_REPO_ROOT, env_name="production")
    assert prod_config.tend_main_merge_enabled is False
    base_config = build_config_via_pydantic(_REPO_ROOT, env_name=None)
    assert base_config.tend_main_merge_enabled is True


def test_no_prod_only_canary_is_overdue() -> None:
    """CI staleness guard: no prod-only canary has crossed its graduation horizon.

    The overlay-axis mirror of ``test_no_experimental_flag_is_overdue``, over
    ``PROD_ONLY_CANARIES`` **only** — ``PROD_ONLY_OPERATIONAL`` carries no
    ``review_by`` and is a permanent divergence, never overdue-checked. The day a
    canary's horizon passes, the build reds until a human graduates it (promote to
    base), reverts it, or re-dates the entry with a rationale.
    """
    overdue = find_overdue_flags(PROD_ONLY_CANARIES, FREEZE_DATE)
    overdue_names = sorted(entry.flag for entry in overdue)
    assert overdue_names == [], (
        "These prod-only canaries have passed their review_by horizon: "
        f"{overdue_names}. Graduate (promote to base maestro.toml), revert the "
        "production enable, or re-date the PROD_ONLY_CANARIES entry with a "
        "rationale."
    )


def test_enabled_only_in_overlay_detects_seeded_pairs() -> None:
    """Negative control: the prod-vs-base diff fires on the right synthetic keys.

    Without this, a helper that returned the empty set (or every key) would make the
    real-file assertion above pass vacuously. Over synthetic in-memory flat dicts —
    no filesystem — a flag ``True`` in overlay but ``False``/absent in base is
    reported, a flag ``True`` in both is not, and a numeric override (a non-boolean
    ``True`` value) is not, exercising the ``is True`` boolean check.
    """
    base_flat: dict[str, object] = {
        "shared_on": True,       # on in both -> not prod-only
        "base_off": False,       # off in base, on in overlay -> prod-only
        "numeric_knob": 3,       # numeric in both -> never a flag enable
    }
    overlay_flat: dict[str, object] = {
        "shared_on": True,
        "base_off": True,
        "overlay_new": True,     # absent from base, on in overlay -> prod-only
        "numeric_knob": 5,       # numeric override, not a boolean True -> excluded
    }
    prod_only = enabled_only_in_overlay(base_flat, overlay_flat)
    assert prod_only == {"base_off", "overlay_new"}


def test_disabled_only_in_overlay_detects_seeded_pairs() -> None:
    """Negative control: the subtractive prod-vs-base diff fires on the right keys.

    The mirror of :func:`test_enabled_only_in_overlay_detects_seeded_pairs`, and the
    same reason it exists: without it a helper that returned the empty set (or every
    key) would make ``test_prod_only_disabled_flags_are_all_declared``'s real-file
    set-equality pass vacuously. Over synthetic in-memory flat dicts — no filesystem
    — a flag ``True`` in base but ``False`` in the overlay is reported, one ``True``
    in base but absent from the overlay is reported, one ``True`` in both is not, and
    a numeric override is not, exercising the ``is True`` boolean check on both
    sides.
    """
    base_flat: dict[str, object] = {
        "shared_on": True,        # on in both -> not prod-only-disabled
        "base_on": True,          # on in base, off in overlay -> prod-only-disabled
        "base_only_on": True,     # on in base, absent from overlay -> reported
        "base_off": False,        # off in base -> never a disable
        "numeric_knob": 3,        # numeric in both -> never a flag disable
    }
    overlay_flat: dict[str, object] = {
        "shared_on": True,
        "base_on": False,
        "base_off": True,         # an *enable*, the other polarity -> excluded
        "numeric_knob": 5,        # numeric override, not a boolean True -> excluded
    }
    prod_disabled = disabled_only_in_overlay(base_flat, overlay_flat)
    assert prod_disabled == {"base_on", "base_only_on"}


def test_new_prod_only_canary_carries_justification() -> None:
    """Every non-baseline prod-only canary carries an explicit justification.

    The overlay-axis feature-gating-discipline guard: a canary added after the
    2026-08-24 baseline freeze (:data:`PROD_ONLY_CANARY_BASELINE`) must name an
    observability/operational reason it is a prod-only enable rather than a
    dark-launched off-everywhere feature. A new bare ``EnvOverlayCanary`` reds the
    build until it either carries a ``justification`` or the author reaches for the
    disciplined default (``EXPERIMENTAL_FLAGS``). The non-empty manifest guard keeps
    an empty ``PROD_ONLY_CANARIES`` from passing this vacuously.
    """
    assert PROD_ONLY_CANARIES, (
        "Expected a non-empty PROD_ONLY_CANARIES manifest — an empty manifest would "
        "pass the justification guard vacuously."
    )
    unjustified = find_unjustified_canaries(
        PROD_ONLY_CANARIES, PROD_ONLY_CANARY_BASELINE,
    )
    assert unjustified == [], (
        "These prod-only canaries were added after the baseline freeze but carry no "
        f"justification: {unjustified}. A new prod-only enable must name an explicit "
        "observability/operational reason (EnvOverlayCanary.justification), or "
        "dark-launch off-everywhere via EXPERIMENTAL_FLAGS instead."
    )


def test_prod_only_canary_baseline_is_current() -> None:
    """Every grandfathered baseline flag still appears in ``PROD_ONLY_CANARIES``.

    The self-cleaning tripwire (mirrors
    :func:`test_unmanifested_default_off_allowlist_is_current` and
    :func:`test_base_advance_re_entry_graduated_default_on`): a baseline flag that
    graduates to base, reverts, or is otherwise dropped from
    :data:`PROD_ONLY_CANARIES` turns its stale :data:`PROD_ONLY_CANARY_BASELINE`
    entry red, so the frozen baseline shrinks with the manifest and can never
    silently grandfather a flag that is gone (which would let a re-added canary of
    the same name masquerade as pre-freeze).
    """
    canaries = {c.flag for c in PROD_ONLY_CANARIES}
    stale = sorted(PROD_ONLY_CANARY_BASELINE - canaries)
    assert stale == [], (
        "These PROD_ONLY_CANARY_BASELINE flags are no longer in PROD_ONLY_CANARIES "
        f"(graduated, reverted, or removed): {stale}. Drop them from the baseline so "
        "it never grandfathers a flag that is gone."
    )


def test_unjustified_canary_detector_fires_on_seeded_entry() -> None:
    """Negative control: the detector reports only the unjustified non-baseline canary.

    Over synthetic in-memory ``EnvOverlayCanary`` records — no filesystem — three
    cases pin both branches of :func:`find_unjustified_canaries`: a non-baseline
    entry with an empty justification is reported, a non-baseline entry carrying a
    justification is not, and a baseline entry with an empty justification is not
    (grandfathered). Without this, a detector that returned nothing (or everything)
    would make the real-tree guard pass vacuously. Mirrors
    :func:`test_enabled_only_in_overlay_detects_seeded_pairs`.
    """
    baseline = frozenset({"grandfathered_flag"})
    canaries = (
        EnvOverlayCanary(
            flag="new_unjustified_flag",
            env="production",
            review_by="2027-12-31",
            note="seeded canary with no justification",
        ),
        EnvOverlayCanary(
            flag="new_justified_flag",
            env="production",
            review_by="2027-12-31",
            note="seeded canary",
            justification="measures the widget divergence under production load",
        ),
        EnvOverlayCanary(
            flag="grandfathered_flag",
            env="production",
            review_by="2027-12-31",
            note="baseline entry — an empty justification is exempt",
        ),
    )
    unjustified = find_unjustified_canaries(canaries, baseline)
    assert unjustified == ["new_unjustified_flag"]


# ---------------------------------------------------------------------------
# Default-off completeness guard (field -> manifest)
# ---------------------------------------------------------------------------
# The reader scan above pins every Config field to a consumer, and the
# manifest -> field guards pin every manifest entry to a real field — but nothing
# forces a newly-added default-*off* feature switch to be lifecycle-tracked at
# all: it has a reader (the dead-flag guard passes), is registered in no manifest,
# and sails past every existing guard while carrying no review_by horizon. This
# guard closes that gap by partitioning every default-off boolean Config field
# into exactly one of two documented dispositions — managed in a lifecycle
# manifest (EXPERIMENTAL_FLAGS / PROD_ONLY_CANARIES / PROD_ONLY_OPERATIONAL), or
# grandfathered in the allowlist below with an honest reason — and failing on any
# flag in neither. Adding a default-off flag then becomes a conscious
# classification decision, exactly as the default-on sibling
# (tests/test_cutover_flag_collapse.py) forces for the retirement half. The
# helpers, allowlist, and detector mirror _KNOWN_READERLESS_FIELDS /
# find_readerless_fields member-for-member.


def _default_off_bool_field_names(cls: type[Any] = Config) -> set[str]:
    """Return the *cls* dataclass field names whose default is boolean ``False``.

    Reads the dataclass default directly (``{f.name for f in
    dataclasses.fields(cls) if f.default is False}``), *not* the committed
    ``maestro.toml`` via the section models as the default-on sibling
    (``tests/test_cutover_flag_collapse.py::default_on_boolean_fields``) does. That
    divergence is deliberate: it catches nested-table flags such as
    ``sdk_cli_paced_probe_metrics_enabled`` that carry no section-model field
    and would otherwise escape the guard entirely. The ``is False`` identity test
    uniquely selects boolean-``False`` defaults — ``0 is False`` is ``False``, so
    ``int`` / ``str`` / ``default_factory`` fields are naturally excluded. *cls*
    defaults to :class:`Config` (the real population) and is parameterised only so
    the negative control can feed a synthetic dataclass, mirroring the defaulted
    parameter on the default-on sibling's population helper.
    """
    return {f.name for f in dataclasses.fields(cls) if f.default is False}


def _lifecycle_managed_flags() -> set[str]:
    """Return every flag already tracked on some lifecycle axis.

    The union of the three manifests' flag names: the base-axis review horizon
    (:data:`EXPERIMENTAL_FLAGS`), the overlay graduation horizon
    (:data:`PROD_ONLY_CANARIES`), and the declared permanent overlay divergence
    (:data:`PROD_ONLY_OPERATIONAL`). Counting overlay membership as "managed" (not
    only the base ``EXPERIMENTAL_FLAGS`` axis) keeps the flags that carry an
    overlay horizon from having to be double-declared in the allowlist — a flag
    already bound to a horizon on any axis is not escaping the forcing function.
    """
    return (
        {f.flag for f in EXPERIMENTAL_FLAGS}
        | {c.flag for c in PROD_ONLY_CANARIES}
        | {d.flag for d in PROD_ONLY_OPERATIONAL}
    )


#: Every default-off boolean ``Config`` field not yet tracked on any lifecycle
#: axis, mapped to an honest one-line reason (the :data:`_KNOWN_READERLESS_FIELDS`
#: shape). This grandfathers the flags unmanaged on today's tree so the
#: completeness guard passes green while making them visible; a follow-on
#: increment migrates the genuine time-boxed experiments into
#: ``EXPERIMENTAL_FLAGS`` with real ``review_by`` horizons, shrinking this list
#: toward only the permanently-non-experimental toggles.
#: :func:`test_unmanifested_default_off_allowlist_is_current` keeps it honest: a
#: promoted, deleted, or default-flipped flag turns its stale entry red.
_UNMANIFESTED_DEFAULT_OFF: dict[str, str] = {
    # --- Non-experimental toggles (never need a review horizon) ---
    "audit_costume_display_enabled": (
        "observability opt-in — surfaces the audit costume-signature display; a "
        "diagnostic toggle, never a time-boxed experiment"
    ),
    "audit_undefined_coinage_display_enabled": (
        "observability opt-in — surfaces undefined-coinage findings in the audit "
        "display; a diagnostic toggle, not an experiment"
    ),
    "long_context_implementation_enabled": (
        "model-selection mode — extends the long-context cap to Session C / "
        "Executor; a capability toggle, not an experiment"
    ),
    "peak_throttle_enabled": (
        "operating-mode toggle — enables peak-hour LLM-phase throttling; a "
        "permanent mode switch, not an experiment"
    ),
    "conscious_workspace_observe_enabled": (
        "cut-over flag tracked in CUTOVER_FLAGS, off in base (proposal 149); "
        "deleted with the workspace decommission"
    ),
    "require_claude_md_update": (
        "prompt-policy toggle — requires a CLAUDE.md update in the implementation "
        "prompt; a policy switch, not an experiment"
    ),
    "sdk_cli_paced_probe_metrics_enabled": (
        "observability opt-in — scrapes cli_paced /cost, /usage, /context probe "
        "metrics; a diagnostic toggle, not an experiment"
    ),
    "serena_mcp_enabled": (
        "MCP-server wiring — enables the read-only Serena MCP launcher; a "
        "permanent integration toggle, never a horizon-bound experiment"
    ),
    "sqlite_state_enabled": (
        "storage-backend selection — routes state through the SQLite backend; a "
        "permanent backend toggle, not an experiment"
    ),
    "standby_enabled": (
        "operating-mode toggle — enables standby-hour iteration skipping; a "
        "permanent mode switch, not an experiment"
    ),
    "trust_forwarded_proto": (
        "reverse-proxy trust — honours X-Forwarded-Proto behind a trusted proxy; a "
        "deployment toggle, not an experiment"
    ),
    # --- Behavioral experiments grandfathered pending an EXPERIMENTAL_FLAGS
    # horizon (the follow-on increment) ---
    "audit_stall_block_enabled": (
        "opt-in audit-rule experiment (stall block) awaiting an EXPERIMENTAL_FLAGS "
        "review_by horizon (the follow-on increment)"
    ),
    "audit_undefined_coinage_directive_enabled": (
        "opt-in audit-rule experiment (undefined-coinage directive) awaiting an "
        "EXPERIMENTAL_FLAGS review_by horizon (the follow-on)"
    ),
    "audit_ungrounded_closure_enabled": (
        "opt-in audit-rule experiment (ungrounded closure) awaiting an "
        "EXPERIMENTAL_FLAGS review_by horizon (the follow-on)"
    ),
    "banked_owned_act_enabled": (
        "opt-in audit-rule experiment (banked-owned-act reperformance) awaiting an "
        "EXPERIMENTAL_FLAGS review_by horizon (the follow-on)"
    ),
    "dropped_directive_replay_enabled": (
        "opt-in behavioral experiment (dropped-directive replay gate) awaiting an "
        "EXPERIMENTAL_FLAGS review_by horizon (the follow-on)"
    ),
    "loop_restatement_concept_key_enabled": (
        "opt-in behavioral experiment (restatement concept-key) awaiting an "
        "EXPERIMENTAL_FLAGS review_by horizon (the follow-on)"
    ),
    "phantom_partial_prune_enabled": (
        "opt-in behavioral experiment (phantom-partial prune) awaiting an "
        "EXPERIMENTAL_FLAGS review_by horizon (the follow-on)"
    ),
    "progress_aware_lap_budget_enabled": (
        "opt-in behavioral experiment (progress-aware lap budget) awaiting an "
        "EXPERIMENTAL_FLAGS review_by horizon (the follow-on)"
    ),
    "readmission_construction_directive_enabled": (
        "opt-in behavioral experiment (readmission construction directive) "
        "awaiting an EXPERIMENTAL_FLAGS review_by horizon (the follow-on)"
    ),
    # --- Dead flags (readerless; disposal is a separate cleanup) ---
    "construction_dominance_enabled": (
        "dead flag — gates the undispatched check_construction_dominance rule "
        "(run_all_checks never dispatches it), so it changes no behaviour; also in "
        "_KNOWN_READERLESS_FIELDS, disposal is a separate cleanup"
    ),
    "verbose": (
        "dead flag — readerless, masked from the reader guard only by the "
        "args.verbose name collision (cli.py); this guard newly forces its "
        "disposition, a separate cleanup"
    ),
}


def find_unmanifested_default_off_flags(
    default_off: set[str],
    managed: set[str],
    allowlisted: Iterable[str],
) -> list[str]:
    """Return the sorted default-off flags in neither a manifest nor the allowlist.

    Pure over its three sets — the exact shape of :func:`find_readerless_fields` /
    ``tests/test_cutover_flag_collapse.py::find_unclassified_cutover_flags`` — so
    the real-tree guard and the seeded negative control share one definition of
    "unmanifested": a default-off boolean that is neither lifecycle-managed nor
    grandfathered in :data:`_UNMANIFESTED_DEFAULT_OFF`.
    """
    return sorted(default_off - managed - set(allowlisted))


def test_every_default_off_flag_is_managed_or_allowlisted() -> None:
    """Every default-off boolean flag is lifecycle-managed or documented.

    The completeness guard: build the default-off boolean population off the
    ``Config`` dataclass defaults and assert every field is either tracked in a
    lifecycle manifest (:func:`_lifecycle_managed_flags`) or grandfathered in
    :data:`_UNMANIFESTED_DEFAULT_OFF` with a reason. A newly-added default-off flag
    that is neither reds the build. The non-empty population guard keeps a broken
    read (empty set) from passing this vacuously.
    """
    population = _default_off_bool_field_names()
    assert population, "Expected default-off boolean fields on Config"
    unmanifested = find_unmanifested_default_off_flags(
        population,
        _lifecycle_managed_flags(),
        _UNMANIFESTED_DEFAULT_OFF,
    )
    assert unmanifested == [], (
        "These default-off boolean flags are neither lifecycle-managed nor "
        f"allowlisted: {unmanifested}. Pick one of three honest dispositions: "
        "register the flag in EXPERIMENTAL_FLAGS with a review_by horizon, declare "
        "it as a PROD_ONLY_CANARIES / PROD_ONLY_OPERATIONAL overlay enable, or "
        "document it in _UNMANIFESTED_DEFAULT_OFF with a reason."
    )


def test_unmanifested_default_off_allowlist_is_current() -> None:
    """Every allowlisted flag still exists, still defaults off, and stays unmanaged.

    The self-cleaning tripwire (mirrors
    :func:`test_known_readerless_allowlist_is_current`): promoting a grandfathered
    flag into a lifecycle manifest, deleting it, or flipping its default to
    ``True`` each turns its stale :data:`_UNMANIFESTED_DEFAULT_OFF` entry red, so
    the allowlist never silently misrepresents the tree.
    """
    default_off = _default_off_bool_field_names()
    managed = _lifecycle_managed_flags()
    for flag in _UNMANIFESTED_DEFAULT_OFF:
        assert flag in default_off, (
            f"Allowlisted flag {flag!r} is no longer a default-off boolean on "
            "Config (deleted, or its default flipped) — remove its "
            "_UNMANIFESTED_DEFAULT_OFF entry."
        )
        assert flag not in managed, (
            f"Allowlisted flag {flag!r} is now tracked in a lifecycle manifest — "
            "it carries a horizon now. Remove its _UNMANIFESTED_DEFAULT_OFF entry."
        )


def test_unmanifested_detector_reports_only_unmanaged_flag() -> None:
    """Negative control: the detector reports only the unmanaged, unallowlisted flag.

    Over in-memory sets — no filesystem — a managed flag and an allowlisted flag
    are not reported, while a flag in neither is, exercising both subtractions.
    Without this, a broken detector (reporting nothing, or everything) would make
    the real-tree assertion pass vacuously.
    """
    default_off = {"managed_flag", "allowlisted_flag", "unmanaged_flag"}
    managed = {"managed_flag"}
    allowlisted = {"allowlisted_flag"}
    unmanifested = find_unmanifested_default_off_flags(
        default_off, managed, allowlisted,
    )
    assert unmanifested == ["unmanaged_flag"]


@dataclasses.dataclass
class _PopulationProbe:
    """Synthetic dataclass exercising the ``is False`` population semantics."""

    off_flag: bool = False
    on_flag: bool = True
    zero_int: int = 0


def test_default_off_population_matches_false_default_not_falsy() -> None:
    """Negative control: the population helper matches ``is False``, not falsiness.

    Over a synthetic dataclass — no filesystem — only the ``bool = False`` field is
    selected; an ``int = 0`` field (falsy, but ``0 is False`` is ``False``) and a
    ``bool = True`` field are both excluded, proving the ``is False`` identity
    semantics a truthiness test would get wrong.
    """
    assert _default_off_bool_field_names(_PopulationProbe) == {"off_flag"}


def test_enforced_tool_surface_flag_is_experimental() -> None:
    """Proposal 142's seam enforcement flag carries a dated review horizon."""
    entry = next(
        f for f in EXPERIMENTAL_FLAGS
        if f.flag == "sdk_enforced_tool_surface_enabled"
    )
    assert entry.gate is None
    assert entry.review_by == "2027-09-30"


def test_freeze_date_precedes_every_manifest_horizon() -> None:
    """``FREEZE_DATE`` must not post-date any dated horizon, else a frozen CI is red."""
    entries = [*EXPERIMENTAL_FLAGS, *PROD_ONLY_DISABLED, *PROD_ONLY_CANARIES]
    stale = [e.flag for e in entries if parse_review_by(e.review_by) < FREEZE_DATE]
    assert stale == [], f"entries dated before FREEZE_DATE: {stale}"

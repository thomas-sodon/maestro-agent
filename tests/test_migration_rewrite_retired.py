"""Regression guard: the eager schema-migration rewrite path stays retired (proposal 106).

Proposal 106 collapsed ``ensure_schema`` to schema-on-read only: on a fingerprint
mismatch it advances the stored fingerprint and lets the dataclass loaders normalise
each row on read, deleting the eager ``migrate()`` rewrite sweep and its twelve
``_migrate_*`` normalization helpers. This deterministic, zero-LLM-cost AST scan pins
that ``maestro/migration.py`` no longer defines ``migrate`` or any deleted helper —
while the two genuine data-moving startup steps survive — and that the transitional
``schema_on_read`` flag token is gone from every site it touched. AST (not a
``\\bmigrate\\b`` text scan) is used deliberately: the bare word "migrate" legitimately
appears in prose and comments elsewhere under ``maestro/``. It reads the real source
files, so it touches no ``mem_fs``, network, or subprocess.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MIGRATION = _REPO_ROOT / "maestro" / "migration.py"

#: Module-level functions proposal 106 deleted: the eager rewrite entry point and
#: its per-file normalization helpers.
_RETIRED_FUNCTIONS = frozenset({
    "migrate",
    "_migrate_self_model",
    "_migrate_journal",
    "_migrate_tasks",
    "_migrate_plans",
    "_migrate_version_archive",
    "_migrate_rejection_history",
    "_migrate_artifact_reviews",
    "_migrate_question_resolutions",
    "_migrate_document_records",
    "_migrate_hypotheses",
    "_migrate_messages",
    "_migrate_task_queue",
    # Proposal 149 Step 5: nothing reads the dream history any more.
    "_migrate_dream_digest",
})

#: The two genuine data-moving startup steps that must survive the collapse.
_GENUINE_STEPS = frozenset({
    "migrate_backfill_hashes",
    "_seed_self_model",
})

#: The transitional flag token and every site it touched — it must be gone from all.
_FLAG_TOKEN = "schema_on_read"
_FLAG_SITES = (
    "maestro/config.py",
    "maestro/toml_config.py",
    "maestro/cli.py",
    "maestro/flag_lifecycle.py",
    "maestro.toml",
)


def _module_level_functions(source: str) -> set[str]:
    """Return the names of every module-level ``def`` in *source*.

    Pure over its *source* string so the real-file scan and the seeded negative
    control below share one definition of "defines a module-level function".
    Nested defs (methods, closures) are ignored — only the module body counts.
    """
    tree = ast.parse(source)
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_eager_rewrite_functions_retired_from_migration() -> None:
    """``migration.py`` defines none of the retired rewrite functions, all genuine steps."""
    defined = _module_level_functions(_MIGRATION.read_text(encoding="utf-8"))
    # Vacuous-pass guard: a mis-scoped path or empty parse would let the absence
    # assertion below pass without validating anything.
    assert defined >= _GENUINE_STEPS, (
        f"Expected the genuine startup steps {sorted(_GENUINE_STEPS)} to remain "
        f"defined in {_MIGRATION}; found {sorted(defined)}"
    )
    resurrected = _RETIRED_FUNCTIONS & defined
    assert resurrected == set(), (
        "Proposal 106 collapsed ensure_schema to schema-on-read only and deleted "
        f"the eager rewrite path; {sorted(resurrected)} reappeared in "
        f"{_MIGRATION}. Delete it — the loaders normalise on read."
    )


def test_scan_flags_seeded_retired_functions() -> None:
    """Negative control: a seeded ``migrate`` / ``_migrate_*`` def is detected.

    Without this, a broken scan (returning nothing) would make the retirement
    assertion above pass vacuously. Runs over an in-memory source string, so it
    touches no filesystem — and exercises a retired entry point, a retired
    helper, and a genuine step that must not be flagged.
    """
    seeded = (
        "def migrate(memory_dir):\n"
        "    return True\n"
        "def _migrate_journal(memory_dir):\n"
        "    return True\n"
        "def _seed_self_model(memory_dir):\n"
        "    return True\n"
    )
    defined = _module_level_functions(seeded)
    flagged = _RETIRED_FUNCTIONS & defined
    assert flagged == {"migrate", "_migrate_journal"}
    assert "_seed_self_model" in defined


def test_schema_on_read_flag_token_absent_from_every_site() -> None:
    """The transitional ``schema_on_read`` token is gone from every flag site."""
    for rel in _FLAG_SITES:
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        # Vacuous-pass guard: an unreadable/empty file would pass hollowly.
        assert text, f"Expected a non-empty {rel}"
        assert _FLAG_TOKEN not in text, (
            f"Proposal 106 retired the {_FLAG_TOKEN}_enabled flag; the token "
            f"reappeared in {rel}. Remove it — schema-on-read is unconditional."
        )

"""Regression: docs/vps-setup.md §9 must keep the backup cron functional.

The cron-spawned shell opens the stdout/stderr redirect target **before**
exec'ing the script. If the parent directory of
``/opt/maestro/logs/backup.log`` does not exist on the freshly
provisioned VPS, ``open(2)`` returns ``ENOENT``, the shell exits non-zero,
and ``backup-maestro.sh`` never runs — symptomatically identical to the
root-owned ``/var/log/`` bug the §9 rewrite claims to fix.

This test guards against that regression by enforcing two layered fixes:

1. ``docs/vps-setup.md`` §9 must teach the operator to create
   ``/opt/maestro/logs`` before the redirect ever fires (either
   as a one-off prerequisite, an inline ``mkdir -p`` in the crontab
   entry, or both).
2. ``scripts/backup-maestro.sh`` must be self-bootstrapping — i.e. it
   must ``mkdir -p`` the conventional log directory itself — so that an
   operator who pastes the crontab line verbatim is not one ``rm -rf``
   away from a silent backup outage.

The grep regex mirrors the one in the originating validation evidence so
the test fails today if either fix is reverted.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_VPS_DOC = _REPO / "docs" / "vps-setup.md"
_BACKUP_SCRIPT = _REPO / "scripts" / "backup-maestro.sh"

# Matches ``mkdir … /logs<non-slash>`` so trailing-slash or quoted forms
# count, but the unrelated docker-entrypoint ``$MEMORY_DIR/logs/sdk``
# pattern (which has another path component after ``/logs/``) does not.
_MKDIR_LOGS_RE = re.compile(r"mkdir.*(/opt/maestro)?/logs([^/]|$)")


def _matches(path: Path) -> bool:
    return any(_MKDIR_LOGS_RE.search(line) for line in path.read_text().splitlines())


def test_vps_setup_documents_logs_mkdir_prerequisite() -> None:
    """§9 must instruct the operator to create the redirect's parent dir.

    Without this, a freshly-provisioned VPS that follows the guide
    verbatim will have its first cron firing abort at the ``>>``
    redirect with ``No such file or directory``, exactly mirroring the
    ``/var/log/`` bug §9 was rewritten to fix.
    """
    assert _matches(_VPS_DOC), (
        "expected docs/vps-setup.md to contain a `mkdir … /logs` step "
        "(either a standalone prerequisite or an inline mkdir in the "
        "crontab entry) so the redirect target exists before cron fires"
    )


def test_backup_script_is_self_bootstrapping() -> None:
    """The script must self-create its conventional log directory.

    Even with the §9 prerequisite, an operator who later prunes the
    directory (e.g. via tmpfiles cleanup, repo wipe, or fresh clone)
    must not be one missing ``mkdir`` away from a silent backup outage.
    """
    assert _matches(_BACKUP_SCRIPT), (
        "expected scripts/backup-maestro.sh to `mkdir -p …/logs` itself "
        "as defense-in-depth so the conventional cron-log directory "
        "auto-recovers after any out-of-band deletion"
    )


def test_backup_script_comment_does_not_overstate_self_recovery() -> None:
    """The script's defense-in-depth comment must not mislead maintainers.

    Cron's shell opens ``>>`` redirects *before* exec'ing the script, so
    on a fresh VPS with no log dir the script body — including its own
    ``mkdir -p`` — never runs. A future maintainer who reads a comment
    claiming "subsequent runs append even if the docs prerequisite was
    skipped" may delete the inline ``mkdir -p &&`` from the crontab
    entry, silently breaking backups again. Guard both halves:

    1. The comment must NOT claim that subsequent/later cron runs
       self-recover when the docs prerequisite was skipped.
    2. The comment must call the crontab inline ``mkdir -p`` load-bearing
       (or equivalent wording) so its role is unmistakable.
    """
    text = _BACKUP_SCRIPT.read_text()

    # (1) Forbid the overstatement that triggered the review finding.
    forbidden = re.compile(
        r"(second run onward|subsequent run.*even if|even if the docs prerequisite was skipped)",
        re.IGNORECASE,
    )
    assert not forbidden.search(text), (
        "scripts/backup-maestro.sh comment must not claim the script "
        "self-recovers on subsequent cron firings when the docs "
        "prerequisite was skipped — cron opens the `>>` redirect before "
        "exec'ing the script, so without the inline crontab `mkdir -p` "
        "every firing fails at ENOENT and the script body never runs"
    )

    # (2) Require explicit language flagging the inline mkdir as required.
    required = re.compile(r"load-bearing|must not be removed|required", re.IGNORECASE)
    assert required.search(text), (
        "scripts/backup-maestro.sh comment must explicitly mark the "
        "inline `mkdir -p` in the crontab entry as load-bearing / "
        "required, so future maintainers do not delete it thinking the "
        "script's own mkdir substitutes for it"
    )


def test_no_real_bash_subprocess_in_cron_doc_tests() -> None:
    """CLAUDE.md forbids real subprocesses + real-fs writes in unit tests.

    The two empirical bash-redirect tests this file used to contain
    spawned a real ``bash`` subprocess and wrote to the real filesystem
    via ``tmp_path``, in direct violation of the testing principles.
    They also exercised bash's own ``>>`` redirect semantics rather than
    any Maestro artefact, so a flip would signal a system bash
    regression — not a Maestro regression. This test guards against
    their reintroduction.
    """
    target = Path(__file__)
    tree = ast.parse(target.read_text())

    violators = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            uses_subprocess = False
            uses_tmp_path = any(arg.arg == "tmp_path" for arg in node.args.args)
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "run"
                    and isinstance(sub.func.value, ast.Name)
                    and sub.func.value.id == "subprocess"
                ):
                    uses_subprocess = True
            if uses_subprocess and uses_tmp_path:
                violators.append(node.name)

    assert not violators, (
        f"Tests violating CLAUDE.md (real subprocess + real fs): {violators}. "
        "Mock subprocess.run and use mem_fs instead, or delete these tests "
        "if they only assert bash semantics rather than Maestro behaviour."
    )

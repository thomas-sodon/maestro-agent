"""CLI-side replication of the task action-gate (proposal 066).

Maestro's in-process ``PreToolUse`` action-gate (:mod:`maestro.tool_gating`)
blocks PR-mutating and protected-ref-push Bash commands during task execution,
but it runs *inside Maestro's process* and cannot intercept the separate
``claude`` subprocess the CLI-paced provider drives.  This module closes that
gap by re-expressing the same gate in the ``claude`` CLI's own permission
system:

* :func:`run_gate_bash_hook` backs the hidden ``maestro _gate-bash-hook``
  subcommand (``maestro/cli.py``).  The ``claude`` CLI invokes it as a
  ``PreToolUse`` hook command, piping the hook payload to stdin; it delegates
  the decision to the **same** :class:`~maestro.tool_gating.TaskGateHook`
  callback the in-process path runs (which itself wraps
  :func:`~maestro.tool_gating.evaluate_bash_command`), so there is exactly one
  gate implementation and the two enforcement paths cannot drift.  The run's
  owned-writable branch *set* arrives JSON-encoded via
  :data:`GATE_BRANCH_ENV`; an absent/empty/malformed variable decodes to
  ``None``, which the gate core treats fail-closed (every push denied).
* :func:`build_gate_settings` materialises the ``claude`` CLI settings payload
  that registers that subcommand as the CLI's own ``PreToolUse`` ``Bash``
  hook.  The CLI-paced provider serialises it inline onto the spawn argv via
  ``--settings`` (the same inline-JSON pattern as ``--mcp-config`` — no
  on-disk settings file), and carries the branch set both in the settings
  ``env`` block and on the spawned child's environment, so the hook subprocess
  reads it either way.

The threat model matches :mod:`maestro.tool_gating`: a redirected,
non-adversarial model, not deliberate evasion.  This module deliberately
imports neither :mod:`maestro_sdk` nor :mod:`maestro.providers.cli_paced`, so
``maestro/cli.py`` can import it unconditionally without paying the lazy
``maestro_sdk`` import the default ``anthropic`` path avoids.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from collections.abc import Mapping
from typing import Any, TextIO

from maestro.tool_gating import TaskGateHook
from maestro.tool_surface import gate_hook_matcher

#: Hidden ``maestro`` subcommand the ``claude`` CLI invokes as its hook.
GATE_SUBCOMMAND = "_gate-bash-hook"


def gate_hook_argv() -> list[str]:
    """Return the hook argv, bound to the running interpreter (issue #4160).

    ``<sys.executable> -P -m maestro.cli _gate-bash-hook`` launches exactly the
    installation — and gate code — the loop runs, independent of ``PATH``;
    ``-P`` keeps the hook's cwd (the agent-edited workspace) off ``sys.path``
    so a workspace ``maestro/`` cannot supply the gate code.  A
    bare ``maestro`` absent from the child's ``PATH`` exits 127, which the
    ``claude`` CLI treats as a non-blocking hook error: fail-open.
    """
    return [sys.executable, "-P", "-m", "maestro.cli", GATE_SUBCOMMAND]


def gate_hook_command() -> str:
    """Return :func:`gate_hook_argv` shell-quoted as the settings command line."""
    return shlex.join(gate_hook_argv())


#: Environment variable carrying the run's owned-writable branch *set* into
#: the hook subprocess, JSON-encoded as a list (``json.dumps(sorted(...))``).
#: Unset, empty, or malformed reads as ``None``, which
#: ``evaluate_bash_command`` treats fail-closed: every push is denied.  The
#: name is kept singular for compatibility; only its encoded value widened
#: from one branch string to the owned-branch list (proposal 093).
GATE_BRANCH_ENV = "MAESTRO_GATE_ALLOWED_BRANCH"

#: Environment variable carrying the proposal-096 redirection-stripping flag
#: into the hook subprocess.  Emitted (value ``"1"``) **only** when the flag is
#: true; an unset/empty value decodes ``False`` — so a flag-off settings/env
#: payload is byte-identical to the pre-096 one and the two enforcement paths
#: cannot drift on the default.
GATE_STRIP_REDIRECTIONS_ENV = "MAESTRO_GATE_STRIP_REDIRECTIONS"

#: Environment variable carrying the run's current branch (proposal 099) into
#: the hook subprocess, so a bare ``HEAD`` / refspec-less push resolves to it
#: across the seam exactly as in-process.  Emitted **only** for a truthy branch;
#: an unset/empty value decodes ``None`` — the fail-closed default posture,
#: byte-identical to the pre-099 payload.
GATE_CURRENT_BRANCH_ENV = "MAESTRO_GATE_CURRENT_BRANCH"

#: Environment variable carrying the run's allowed push remotes as a JSON
#: list.  Absent decodes ``None`` (remotes unchecked, today's
#: posture); a present but malformed value decodes the empty set, which denies
#: every push's remote — fail-closed.
GATE_PUSH_REMOTES_ENV = "MAESTRO_GATE_PUSH_REMOTES"

#: Deny verdict for a hook payload that is not parseable JSON.  The in-process
#: path can never see this case (the SDK hands the hook a parsed dict), so it
#: is outside the parity surface; emitting a deny rather than crashing keeps
#: the subprocess gate fail-closed — a non-zero hook exit would read to the
#: ``claude`` CLI as a *non-blocking* hook error, i.e. fail-open.
_UNPARSEABLE_DENY: dict[str, Any] = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "Blocked (fail-closed): the action-gate hook received an "
            "unparseable payload and denies for safety."
        ),
    },
}


def allowed_branches_to_env(
    allowed_branches: frozenset[str] | None,
) -> dict[str, str]:
    """Encode *allowed_branches* into the hook subprocess's env block.

    Returns ``{}`` for a falsy set (``None`` or empty) — the caller then sets
    no :data:`GATE_BRANCH_ENV`, and the decoder reads ``None``, so the gate
    core fails closed on every push.  Otherwise returns
    ``{GATE_BRANCH_ENV: json.dumps(sorted(allowed_branches))}``.  Sorting makes
    the encoding deterministic (a ``frozenset`` has no stable iteration order),
    mirroring the ``json.dumps(sorted(...))`` convention used elsewhere
    (:mod:`maestro.audit_checks`).  A single function produces *both* the
    settings ``env`` block and the spawned child's env so the two cannot drift.
    """
    if not allowed_branches:
        return {}
    return {GATE_BRANCH_ENV: json.dumps(sorted(allowed_branches))}


def strip_redirections_to_env(strip_redirections: bool) -> dict[str, str]:
    """Encode the proposal-096 redirection-stripping flag into an env block.

    Returns ``{GATE_STRIP_REDIRECTIONS_ENV: "1"}`` when the flag is true and
    ``{}`` when it is false — so a flag-off payload sets no variable and
    :func:`strip_redirections_from_env` reads ``False``, keeping the
    flag-off env block byte-identical to the pre-096 one.  Mirrors the
    absent-is-fail-safe-default convention of :func:`allowed_branches_to_env`.
    """
    if not strip_redirections:
        return {}
    return {GATE_STRIP_REDIRECTIONS_ENV: "1"}


def strip_redirections_from_env(environ: Mapping[str, str]) -> bool:
    """Decode the redirection-stripping flag from *environ*, defaulting off.

    ``True`` only when :data:`GATE_STRIP_REDIRECTIONS_ENV` is present and
    non-empty (:func:`strip_redirections_to_env` writes ``"1"``); a missing or
    empty value reads ``False`` — the byte-identical-to-today default posture.
    """
    return bool(environ.get(GATE_STRIP_REDIRECTIONS_ENV))


def current_branch_to_env(current_branch: str | None) -> dict[str, str]:
    """Encode the run's current branch (proposal 099) into an env block.

    Returns ``{GATE_CURRENT_BRANCH_ENV: current_branch}`` for a truthy branch
    and ``{}`` for a falsy one (``None`` or empty) — so a branch-less payload
    sets no variable and :func:`current_branch_from_env` reads ``None``, keeping
    the fail-closed default env block byte-identical to the pre-099 one.
    Mirrors the absent-is-fail-safe-default convention of
    :func:`strip_redirections_to_env`.
    """
    if not current_branch:
        return {}
    return {GATE_CURRENT_BRANCH_ENV: current_branch}


def current_branch_from_env(environ: Mapping[str, str]) -> str | None:
    """Decode the run's current branch from *environ*, defaulting ``None``.

    Returns the :data:`GATE_CURRENT_BRANCH_ENV` value when present and non-empty
    (:func:`current_branch_to_env` writes the branch name); a missing or empty
    value reads ``None`` — the fail-closed posture, byte-identical to a hook
    built with no current branch.
    """
    return environ.get(GATE_CURRENT_BRANCH_ENV) or None


def push_remotes_to_env(
    allowed_push_remotes: frozenset[str] | None,
) -> dict[str, str]:
    """Encode the allowed push remotes; ``None`` sets no variable."""
    if allowed_push_remotes is None:
        return {}
    return {GATE_PUSH_REMOTES_ENV: json.dumps(sorted(allowed_push_remotes))}


def push_remotes_from_env(environ: Mapping[str, str]) -> frozenset[str] | None:
    """Decode the allowed push remotes; absent → ``None``, malformed → empty set."""
    raw = environ.get(GATE_PUSH_REMOTES_ENV)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return frozenset()
    if not isinstance(payload, list) or not all(isinstance(i, str) for i in payload):
        return frozenset()
    return frozenset(payload)


def build_gate_settings(
    allowed_branches: frozenset[str] | None,
    strip_redirections: bool = False,
    current_branch: str | None = None,
    allowed_push_remotes: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Build the ``claude`` CLI settings payload registering the gate hook.

    The structure is the CLI's own settings schema: a ``PreToolUse`` matcher
    scoped to ``Bash`` whose hook is the :func:`gate_hook_command` subcommand,
    plus an ``env`` block carrying the JSON-encoded *allowed_branches* set
    (:func:`allowed_branches_to_env`) so the hook subprocess is branch-scoped
    exactly like the in-process gate, — only when *strip_redirections* is
    true — the :data:`GATE_STRIP_REDIRECTIONS_ENV` flag
    (:func:`strip_redirections_to_env`), and — only for a truthy
    *current_branch* — the :data:`GATE_CURRENT_BRANCH_ENV` value
    (:func:`current_branch_to_env`, proposal 099).  When the branch set is
    falsy, the flag is off, and no current branch is supplied the ``env`` block
    is omitted entirely — the hook then reads no branch and fails closed on
    every push, mirroring ``TaskGateHook(None)``; such a payload is
    byte-identical to the pre-099 one.  *allowed_push_remotes*
    rides as :data:`GATE_PUSH_REMOTES_ENV` only when not ``None``.
    """
    settings: dict[str, Any] = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": gate_hook_matcher(),
                    "hooks": [
                        {"type": "command", "command": gate_hook_command()},
                    ],
                },
            ],
        },
    }
    env = {
        **allowed_branches_to_env(allowed_branches),
        **strip_redirections_to_env(strip_redirections),
        **current_branch_to_env(current_branch),
        **push_remotes_to_env(allowed_push_remotes),
    }
    if env:
        settings["env"] = env
    return settings


def serialize_gate_settings(
    allowed_branches: frozenset[str] | None,
    strip_redirections: bool = False,
    current_branch: str | None = None,
    allowed_push_remotes: frozenset[str] | None = None,
) -> str:
    """Serialise :func:`build_gate_settings` for the inline ``--settings`` flag.

    Inline string-encoded JSON (not a temp file) keeps the spawn path free of
    real I/O, mirroring the ``--mcp-config`` pattern.
    """
    return json.dumps(
        build_gate_settings(
            allowed_branches, strip_redirections, current_branch, allowed_push_remotes,
        ),
    )


def allowed_branches_from_env(
    environ: Mapping[str, str],
) -> frozenset[str] | None:
    """Decode the run's owned-writable branch set from *environ*, fail-closed.

    Reads the JSON list :func:`allowed_branches_to_env` wrote to
    :data:`GATE_BRANCH_ENV` and returns a ``frozenset`` **only** when the
    payload is a non-empty list of strings.  A missing, empty, non-list, or
    unparseable value returns ``None`` — the gate core then denies every push,
    the same fail-closed posture as an in-process hook built with no branch.
    """
    raw = environ.get(GATE_BRANCH_ENV)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    if not all(isinstance(item, str) for item in payload):
        return None
    return frozenset(payload) or None


def run_gate_bash_hook(
    stdin: TextIO, stdout: TextIO, environ: Mapping[str, str],
) -> int:
    """Answer one ``claude`` CLI ``PreToolUse`` hook invocation.

    Reads the CLI's hook payload (JSON on *stdin* — the same
    ``{"tool_name": …, "tool_input": {"command": …}}`` shape the in-process
    hook receives), runs the **same** :class:`~maestro.tool_gating.TaskGateHook`
    callback the executor wires in-process, and writes its verdict JSON to
    *stdout*: the deny shape with the gate's reason, or ``{}`` for
    no-opinion (the CLI's normal flow then proceeds).  Always returns ``0`` —
    the verdict rides the JSON, and a non-zero exit would read to the CLI as a
    non-blocking hook *error* rather than a decision.

    Unparseable stdin emits the fail-closed :data:`_UNPARSEABLE_DENY` rather
    than crashing: a crashed hook is a fail-open hook.
    """
    try:
        payload = json.loads(stdin.read())
    except json.JSONDecodeError:
        json.dump(_UNPARSEABLE_DENY, stdout)
        return 0
    hook = TaskGateHook(
        allowed_branches_from_env(environ),
        strip_redirections=strip_redirections_from_env(environ),
        current_branch=current_branch_from_env(environ),
        allowed_push_remotes=push_remotes_from_env(environ),
    )
    verdict = asyncio.run(hook(payload, None, None))
    json.dump(verdict, stdout)
    return 0


__all__ = [
    "GATE_BRANCH_ENV",
    "GATE_CURRENT_BRANCH_ENV",
    "GATE_PUSH_REMOTES_ENV",
    "GATE_STRIP_REDIRECTIONS_ENV",
    "GATE_SUBCOMMAND",
    "allowed_branches_from_env",
    "allowed_branches_to_env",
    "build_gate_settings",
    "current_branch_from_env",
    "current_branch_to_env",
    "gate_hook_argv",
    "gate_hook_command",
    "push_remotes_from_env",
    "push_remotes_to_env",
    "run_gate_bash_hook",
    "serialize_gate_settings",
    "strip_redirections_from_env",
    "strip_redirections_to_env",
]

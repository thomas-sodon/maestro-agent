"""Push-conformance golden test for the task-execution action gate (096).

The gate (:mod:`maestro.tool_gating`) exists to enforce that a task-execution
Bash push targets only the run's owned branches, never ``main`` and never a
force-push.  Proposal 093 made the PR-remediation prompts unconditionally
instruct the agent to self-deliver its fix with the push shape centralised in
:data:`maestro.loop.pr_remediation.OWN_BRANCH_PUSH_TEMPLATE`.  Before proposal
096 that instructed push was silently unenforceable: the agent captures output
with ``… 2>&1 | tail`` on essentially every push, and the gate's tokenizer read
the leaked ``2``/``1`` file-descriptor tokens as stray refspecs and denied the
push fail-closed.

This module is the missing *conformance contract*: every push form the loop
instructs — the shared template plus the bare ``git push origin <branch>`` form —
wrapped in the realistic output-capture idioms the agent emits, must be
**allowed** for an in-set branch and **denied** for ``main``/``master``, an
out-of-set branch, or a force-push.  Each case is driven through all three
enforcement faces — the pure core, the in-process ``PreToolUse`` hook, and the
CLI-subprocess replica — so the two enforcement paths are proven to conform.

Most of the suite pins ``strip_redirections=True`` directly;
:class:`TestPushConformanceShippingDefault` instead derives the flag from a bare
``Config()`` so the shipping default (on since proposal 096's flip) is proven to
reach the gate — not just a hardcoded ``True``.  The inert-default guard proving
flag-off is byte-identical lives in ``tests/test_tool_gating.py``.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from maestro.config import Config
from maestro.loop.pr_remediation import OWN_BRANCH_PUSH_TEMPLATE
from maestro.providers.cli_paced_gate import (
    allowed_branches_to_env,
    current_branch_to_env,
    run_gate_bash_hook,
    strip_redirections_to_env,
)
from maestro.tool_gating import TaskGateHook, evaluate_bash_command

#: The owned PR head the loop instructs a push to (a ``maestro/evolve/*`` head).
_BRANCH = "maestro/evolve/abc"
_BRANCH_SET = frozenset({_BRANCH})
#: The env block the CLI-subprocess replica reads: owned-branch set + the
#: proposal-096 strip flag, so ``run_gate_bash_hook`` strips redirections too.
_ENV = {
    **allowed_branches_to_env(_BRANCH_SET),
    **strip_redirections_to_env(True),
}

#: The canonical own-branch push the loop instructs (proposal 093/096) plus the
#: bare ``git push origin <branch>`` form — the two shapes the agent writes to
#: deliver a fix.  Deriving the first from the shared template pins that the
#: form the loop *instructs* is the form this suite proves the gate *allows*.
_ALLOWED_BASE_PUSHES: tuple[str, ...] = (
    OWN_BRANCH_PUSH_TEMPLATE.format(branch=_BRANCH),  # git push origin HEAD:…
    f"git push origin {_BRANCH}",                      # bare refspec form
)

#: Pushes that must stay denied even after redirection-stripping: protected
#: refs, an out-of-set branch, and every force-push shape.
_DENIED_BASE_PUSHES: tuple[str, ...] = (
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin master",
    "git push origin maestro/evolve/other",         # out of set
    f"git push --force origin {_BRANCH}",
    f"git push --force-with-lease origin {_BRANCH}",
    f"git push origin +{_BRANCH}",                   # +refspec force
)


def _idioms(base: str) -> list[str]:
    """Wrap *base* in each realistic output-capture idiom the agent emits."""
    return [
        base,                                   # bare
        f"{base} 2>&1 | tail -12",              # the ubiquitous capture idiom
        f"{base} 2>&1",                         # merge stderr into stdout
        f"{base} > out 2>&1",                   # redirect to a file
        f"cd /workspace; {base} 2>&1 | tail",   # cd-prefixed chain
        f"{base}; echo EXIT=$?",                # trailing status echo
    ]


def _bash_payload(command: str) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


def _core_allows(command: str) -> bool:
    return evaluate_bash_command(
        command, _BRANCH_SET, strip_redirections=True,
    ) is None


def _hook_allows(command: str) -> bool:
    hook = TaskGateHook(_BRANCH_SET, strip_redirections=True)
    verdict = asyncio.run(hook(_bash_payload(command), None, None))
    return verdict == {}


def _subcommand_allows(command: str) -> bool:
    stdin = io.StringIO(json.dumps(_bash_payload(command)))
    stdout = io.StringIO()
    code = run_gate_bash_hook(stdin, stdout, _ENV)
    assert code == 0
    return json.loads(stdout.getvalue()) == {}


#: The three enforcement faces, each a predicate returning ``True`` for allow.
_PATHS = (
    pytest.param(_core_allows, id="core"),
    pytest.param(_hook_allows, id="in-process-hook"),
    pytest.param(_subcommand_allows, id="cli-subcommand"),
)

_ALLOW_CASES = [c for base in _ALLOWED_BASE_PUSHES for c in _idioms(base)]
_DENY_CASES = [c for base in _DENIED_BASE_PUSHES for c in _idioms(base)]


class TestPushConformanceAllowed:
    """Every instructed own-branch push, in every idiom, is allowed on all
    three enforcement faces once the flag is on."""

    @pytest.mark.parametrize("allows", _PATHS)
    @pytest.mark.parametrize("command", _ALLOW_CASES)
    def test_instructed_push_allowed(self, command: str, allows) -> None:  # type: ignore[no-untyped-def]
        assert allows(command) is True


class TestPushConformanceDenied:
    """No-fail-open: redirect-wrapped pushes to a protected ref, an out-of-set
    branch, or any force shape stay denied on all three faces."""

    @pytest.mark.parametrize("allows", _PATHS)
    @pytest.mark.parametrize("command", _DENY_CASES)
    def test_protected_foreign_and_force_still_denied(
        self, command: str, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        assert allows(command) is False


class TestPushConformanceNoFailOpen:
    """The redirection stripper must never *launder* a redirect operand or its
    target into an allowed refspec, and an unresolvable push still fails
    closed."""

    @pytest.mark.parametrize("allows", _PATHS)
    def test_second_refspec_main_denied(self, allows) -> None:  # type: ignore[no-untyped-def]
        # A push whose second refspec is ``main`` (captured) must deny — the
        # first ref being in an owned set must not rescue it.
        assert allows("git push origin ok-branch main 2>&1") is False

    @pytest.mark.parametrize("allows", _PATHS)
    def test_redirect_target_not_laundered_into_refspec(
        self, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        # ``git push origin > <owned>`` redirects into a file *named* like the
        # owned branch; the operand must be dropped, leaving a refspec-less push
        # that fails closed — never an allow because the target reads owned.
        assert allows(f"git push origin > {_BRANCH}") is False

    @pytest.mark.parametrize("allows", _PATHS)
    def test_refspec_less_push_fails_closed(self, allows) -> None:  # type: ignore[no-untyped-def]
        # A remote-only push (no refspec) with output capture cannot be
        # confirmed to target an owned branch → deny.
        assert allows("git push origin 2>&1 | tail") is False
        assert allows("git push 2>&1") is False

    def test_unparseable_push_fails_closed(self) -> None:
        # An unbalanced-quote push is pr/push-shaped and unparseable → deny,
        # even with the strip flag on.
        assert evaluate_bash_command(
            'git push origin "unterminated', _BRANCH_SET,
            strip_redirections=True,
        ) is not None


class TestPushConformanceAcceptedDigitPopLimitation:
    """Documented, accepted fail-open (proposal 096 *Out of scope*): after
    whitespace-discarding tokenization a *space-separated* positional numeric
    refspec preceding a redirect is indistinguishable from a no-space file
    descriptor, so the digit-pop that makes ``2>&1`` work also drops such a
    foreign refspec, laundering the push to an allow.  Unreachable for
    ``main``/``master``, force-pushes, and owned ``maestro/*`` branches; it can
    only affect a branch literally named by an integer — a shape the loop never
    instructs.  Pinned here (not in :class:`TestPushConformanceNoFailOpen`,
    whose no-launder contract this case is the one accepted exception to) so a
    future pre-tokenization fix flips it deliberately, not by accident.
    """

    #: bash pushes BOTH ``maestro/evolve/abc`` (owned) and the foreign branch
    #: ``5``; the space before ``>`` makes ``5`` a positional refspec, not an fd.
    _LAUNDERED = f"git push origin {_BRANCH} 5 > log"

    @pytest.mark.parametrize("allows", _PATHS)
    def test_numeric_foreign_refspec_laundered_when_flag_on(
        self, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        # Accepted limitation: with the flag on the foreign ``5`` is popped as an
        # fd and the push reads owned-only → allow, identically on every face.
        assert allows(self._LAUNDERED) is True

    def test_numeric_foreign_refspec_denied_when_flag_off(self) -> None:
        # Flag off leaves ``5``/``>``/``log`` in the token stream → foreign
        # refspec → deny; the limitation exists only on the proposal-096 path.
        assert evaluate_bash_command(
            self._LAUNDERED, _BRANCH_SET, strip_redirections=False,
        ) is not None


class TestPushConformancePathParity:
    """The three faces agree verdict-for-verdict across the whole matrix — a
    single decision core behind three seams."""

    @pytest.mark.parametrize("command", _ALLOW_CASES + _DENY_CASES)
    def test_all_paths_agree(self, command: str) -> None:
        verdicts = {
            _core_allows(command),
            _hook_allows(command),
            _subcommand_allows(command),
        }
        assert len(verdicts) == 1


#: The shipping strip flag read from a bare ``Config()`` (no TOML override) — the
#: authoritative default proposal 096's flip sets.  Deriving from it (rather than
#: a hardcoded ``True``) is what proves the *default* reaches the gate.
_DEFAULT_FLAG = Config().gate_strip_redirections_enabled


def _core_allows_flag(command: str, flag: bool) -> bool:
    return evaluate_bash_command(
        command, _BRANCH_SET, strip_redirections=flag,
    ) is None


def _hook_allows_flag(command: str, flag: bool) -> bool:
    hook = TaskGateHook(_BRANCH_SET, strip_redirections=flag)
    verdict = asyncio.run(hook(_bash_payload(command), None, None))
    return verdict == {}


def _subcommand_allows_flag(command: str, flag: bool) -> bool:
    env = {
        **allowed_branches_to_env(_BRANCH_SET),
        **strip_redirections_to_env(flag),
    }
    stdin = io.StringIO(json.dumps(_bash_payload(command)))
    stdout = io.StringIO()
    code = run_gate_bash_hook(stdin, stdout, env)
    assert code == 0
    return json.loads(stdout.getvalue()) == {}


#: The three faces again, each a predicate parameterised on the strip flag so the
#: shipping default (not a hardcoded ``True``) drives the verdict.
_FLAG_PATHS = (
    pytest.param(_core_allows_flag, id="core"),
    pytest.param(_hook_allows_flag, id="in-process-hook"),
    pytest.param(_subcommand_allows_flag, id="cli-subcommand"),
)


class TestPushConformanceShippingDefault:
    """The *shipping default* reaches the gate.  Every predicate here derives its
    strip flag from a bare ``Config()`` rather than pinning ``True``, so proposal
    096's flip to default-on is proven end-to-end: with the default, a redirect-
    wrapped in-set push is allowed on all three faces while a redirect-wrapped
    ``main`` and a force variant stay denied.  This closes the gap between "the
    default value changed" and "the default actually strips at the gate" — were
    the default reverted to ``False`` the allow assertion would fail (flag off →
    the leaked fd tokens deny), so it self-guards the flip."""

    def test_shipping_default_is_on(self) -> None:
        # Anchors why the allow assertion below is non-vacuous: the flip is live.
        assert _DEFAULT_FLAG is True

    @pytest.mark.parametrize("allows", _FLAG_PATHS)
    def test_redirect_wrapped_in_set_push_allowed_by_default(
        self, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        command = f"{OWN_BRANCH_PUSH_TEMPLATE.format(branch=_BRANCH)} 2>&1 | tail -12"
        assert allows(command, _DEFAULT_FLAG) is True

    @pytest.mark.parametrize("allows", _FLAG_PATHS)
    def test_redirect_wrapped_main_denied_by_default(
        self, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        assert allows("git push origin main 2>&1 | tail -12", _DEFAULT_FLAG) is False

    @pytest.mark.parametrize("allows", _FLAG_PATHS)
    def test_redirect_wrapped_force_denied_by_default(
        self, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        force = f"git push --force origin {_BRANCH} 2>&1 | tail -12"
        assert allows(force, _DEFAULT_FLAG) is False


# ---------------------------------------------------------------------------
# Proposal 099 step 7: bare ``HEAD`` and refspec-less pushes resolve to the
# run's current branch, across all three enforcement faces.
# ---------------------------------------------------------------------------

#: The two branch-name-less push shapes the model naturally writes — the
#: destination is the symbolic ``HEAD`` or the implied current branch — plus the
#: ``git push origin`` remote-only form.  None carries a branch token, so the
#: gate can only allow them once the run's current branch is injected.
_CURRENT_BRANCH_BASE_PUSHES: tuple[str, ...] = (
    "git push origin HEAD",   # symbolic HEAD destination
    "git push",               # fully implicit
    "git push origin",        # remote-only, refspec-less
)

#: Bare-``HEAD`` / refspec-less force-push shapes — denied regardless of the
#: current branch because :func:`_is_force_push` runs before HEAD resolution.
_CURRENT_BRANCH_FORCE_PUSHES: tuple[str, ...] = (
    "git push --force origin HEAD",
    "git push --force-with-lease origin HEAD",
    "git push --force",
    "git push origin +HEAD",
)

#: Multi-ref push shapes (``--all`` / ``--mirror``) — denied regardless of the
#: current branch because they push *every* local branch (including the
#: protected ``main``/``master``), not just the resolved destination, so an owned
#: current branch must never launder them into an allow.  The ``HEAD --all`` form
#: proves the destination-resolution below cannot rescue a multi-ref push, and
#: the redirect-wrapped form proves output-capture stripping does not either.
_CURRENT_BRANCH_ALL_REFS_PUSHES: tuple[str, ...] = (
    "git push --all",
    "git push --mirror",
    "git push origin --all",
    "git push origin --mirror",
    "git push origin HEAD --all",
    "git push origin HEAD --mirror",
    "git push --all 2>&1 | tail -12",
    "git push --tags",
    "git push origin --tags",
    "git push --follow-tags",
    "git push origin HEAD --follow-tags 2>&1 | tail -12",
    "git push origin HEAD --tag",
    "git push origin HEAD --follow-tag",
    "git push origin HEAD --al",
    "git push origin HEAD --mirr",
    'bash -lc "git push --tag"',
)

#: Destination-side ``HEAD`` refspecs (``<src>:HEAD``) — denied regardless of the
#: current branch.  Only the *lone-source* ``HEAD`` form resolves to the run's
#: branch; in a ``<src>:HEAD`` refspec the ``HEAD`` is resolved on the *remote*,
#: where it is the remote's default branch (``main``/``master``), and the source
#: is unrelated to the current branch.  Resolving these to an owned current
#: branch would launder a push to the remote's protected default into an allow,
#: so the guard keys on the literal refspec token, not the parsed destination.
#: The owned-source case (``<owned>:HEAD``) proves that even a member of the
#: owned set as *source* cannot rescue a remote-default destination.
_CURRENT_BRANCH_DEST_HEAD_PUSHES: tuple[str, ...] = (
    "git push origin main:HEAD",
    "git push origin master:HEAD",
    f"git push origin {_BRANCH}:HEAD",
)


def _core_allows_cb(command: str, current_branch: str | None) -> bool:
    return evaluate_bash_command(
        command, _BRANCH_SET, strip_redirections=True,
        current_branch=current_branch,
    ) is None


def _hook_allows_cb(command: str, current_branch: str | None) -> bool:
    hook = TaskGateHook(
        _BRANCH_SET, strip_redirections=True, current_branch=current_branch,
    )
    verdict = asyncio.run(hook(_bash_payload(command), None, None))
    return verdict == {}


def _subcommand_allows_cb(command: str, current_branch: str | None) -> bool:
    env = {
        **allowed_branches_to_env(_BRANCH_SET),
        **strip_redirections_to_env(True),
        **current_branch_to_env(current_branch),
    }
    stdin = io.StringIO(json.dumps(_bash_payload(command)))
    stdout = io.StringIO()
    code = run_gate_bash_hook(stdin, stdout, env)
    assert code == 0
    return json.loads(stdout.getvalue()) == {}


#: The three faces again, each a predicate parameterised on the injected
#: current branch, so the widened parsing is proven identical on every seam.
_CB_PATHS = (
    pytest.param(_core_allows_cb, id="core"),
    pytest.param(_hook_allows_cb, id="in-process-hook"),
    pytest.param(_subcommand_allows_cb, id="cli-subcommand"),
)

_CB_ALLOW_CASES = [
    c for base in _CURRENT_BRANCH_BASE_PUSHES for c in _idioms(base)
]


class TestPushConformanceCurrentBranchAllowed:
    """A bare ``HEAD`` / refspec-less push, in every output-capture idiom,
    resolves to the run's current branch and is allowed on all three faces when
    that branch is owned and unprotected (proposal 099 step 7)."""

    @pytest.mark.parametrize("allows", _CB_PATHS)
    @pytest.mark.parametrize("command", _CB_ALLOW_CASES)
    def test_current_branch_push_allowed(
        self, command: str, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        assert allows(command, _BRANCH) is True


class TestPushConformanceCurrentBranchDenied:
    """The parsing widened, the policy did not: the same forms stay denied when
    the resolved current branch is protected or unowned, when no branch is
    injected at all (fail-closed, byte-identical to today), and for any
    force-push shape regardless of the current branch."""

    @pytest.mark.parametrize("allows", _CB_PATHS)
    @pytest.mark.parametrize(
        "current_branch", ["main", "master", "maestro/evolve/other"],
    )
    @pytest.mark.parametrize("command", list(_CURRENT_BRANCH_BASE_PUSHES))
    def test_denied_when_current_branch_protected_or_unowned(
        self, command: str, current_branch: str, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        assert allows(command, current_branch) is False

    @pytest.mark.parametrize("allows", _CB_PATHS)
    @pytest.mark.parametrize("command", list(_CURRENT_BRANCH_BASE_PUSHES))
    def test_fails_closed_without_current_branch(
        self, command: str, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        # The branch-fallback path injects no current branch → unresolvable →
        # fail-closed deny, identical to the pre-099 behaviour on every face.
        assert allows(command, None) is False

    @pytest.mark.parametrize("allows", _CB_PATHS)
    @pytest.mark.parametrize("command", list(_CURRENT_BRANCH_FORCE_PUSHES))
    def test_force_push_denied_even_on_owned_current_branch(
        self, command: str, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        # Force detection precedes HEAD resolution, so an owned current branch
        # never rescues a bare-HEAD / refspec-less force push.
        assert allows(command, _BRANCH) is False

    @pytest.mark.parametrize("allows", _CB_PATHS)
    @pytest.mark.parametrize("command", list(_CURRENT_BRANCH_ALL_REFS_PUSHES))
    def test_all_refs_push_denied_even_on_owned_current_branch(
        self, command: str, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        # ``--all`` / ``--mirror`` push every local branch, so the multi-ref
        # guard precedes HEAD / refspec-less resolution: an owned current branch
        # never launders one into an allow on any face.
        assert allows(command, _BRANCH) is False

    @pytest.mark.parametrize("allows", _CB_PATHS)
    @pytest.mark.parametrize("command", list(_CURRENT_BRANCH_DEST_HEAD_PUSHES))
    def test_dest_side_head_denied_even_on_owned_current_branch(
        self, command: str, allows,  # type: ignore[no-untyped-def]
    ) -> None:
        # HEAD resolution keys on the *lone-source* ``HEAD`` refspec, not the
        # parsed destination, so a ``<src>:HEAD`` refspec — whose ``HEAD`` is the
        # remote's default branch (main/master), not the current branch — never
        # resolves to an owned current branch and stays denied on every face.
        assert allows(command, _BRANCH) is False


class TestPushConformanceCurrentBranchPathParity:
    """The three faces agree verdict-for-verdict across the current-branch
    matrix — one decision core threaded through three seams."""

    @pytest.mark.parametrize(
        "command",
        _CB_ALLOW_CASES
        + list(_CURRENT_BRANCH_FORCE_PUSHES)
        + list(_CURRENT_BRANCH_ALL_REFS_PUSHES)
        + list(_CURRENT_BRANCH_DEST_HEAD_PUSHES),
    )
    @pytest.mark.parametrize(
        "current_branch", [_BRANCH, "main", "maestro/evolve/other", None],
    )
    def test_all_paths_agree(
        self, command: str, current_branch: str | None,
    ) -> None:
        verdicts = {
            _core_allows_cb(command, current_branch),
            _hook_allows_cb(command, current_branch),
            _subcommand_allows_cb(command, current_branch),
        }
        assert len(verdicts) == 1

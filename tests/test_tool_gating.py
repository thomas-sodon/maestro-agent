"""Tests for the task-execution tool-gating guard (issue #2280).

The guard denies PR-mutating and protected-ref-push ``Bash`` commands on the
task-execution path so a goal cannot bypass the human-in-the-loop approval
gate.  These tests exercise every deny class, the allow path, and the
fail-closed default, plus the ``PreToolUse`` hook shape and its dispatch
through the SDK control protocol.

The hook mechanism was also verified live: a ``PreToolUse`` deny hook blocks a
Bash command under ``permission_mode="bypassPermissions"`` (the mode the
executor runs in), whereas a ``can_use_tool`` callback is short-circuited past
in that mode.  That live check cannot run offline; the control-protocol test
below pins the same contract deterministically.
"""

from __future__ import annotations

import asyncio
import io
import json
import shlex

import pytest
from claude_agent_sdk import HookMatcher
from claude_agent_sdk._internal.query import Query

from maestro import tool_gating, tool_surface
from maestro.providers.cli_paced_gate import (
    GATE_BRANCH_ENV,
    allowed_branches_from_env,
    allowed_branches_to_env,
    run_gate_bash_hook,
)
from maestro.tool_gating import (
    _MAX_UNWRAP_DEPTH,
    _PARSE_DENY,
    TaskGateHook,
    _evaluate_command,
    _gh_api_endpoint,
    _heredoc_span,
    _join_line_continuations,
    _maestro_deny,
    _strip_heredoc_bodies,
    _substitution_bodies,
    evaluate_bash_command,
    make_task_pretooluse_hook,
    make_task_tool_hooks,
    permits_pr_close,
    task_gate_from_hooks,
)

_BRANCH = "maestro/task/abc123"
#: The owned-branch *set* the widened decision core takes (proposal 093). The
#: one-element form reproduces the pre-set single-branch verdicts exactly; the
#: ``make_task_*`` shims still take the bare ``_BRANCH`` string and wrap it.
_BRANCH_SET = frozenset({_BRANCH})
#: The JSON-encoded env block the provider hands the hook subprocess for the
#: single-branch set — what the subcommand seam decodes back (proposal 093).
_BRANCH_ENV = allowed_branches_to_env(_BRANCH_SET)

#: Ground-truth command tables for the cross-process gate replication
#: (proposal 066).  One deny class per row plus the issue #2280 attack shapes;
#: the allow side includes the own-branch push the gate deliberately permits.
#: ``test_fault_seeding.py`` imports these same tables and drives them through
#: *both* enforcement paths — in-process hook and CLI-side gate subcommand —
#: so the two paths are proven against a single shared fixture table.
GATE_PARITY_DENY_COMMANDS: tuple[str, ...] = (
    "gh pr create --title x --body y",
    "gh pr merge 5 --squash",
    "gh pr ready 5",
    "gh --repo o/r pr create",
    "gh api repos/o/r/pulls -X POST -f title=x",
    "gh api repos/o/r/pulls/3/merge -X PUT",
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin feature-x",
    "git push",
    "git -C /repo push origin main",
    "GH_TOKEN=tok gh pr create",
    "a && gh pr create --title x",
    'bash -c "gh pr create --title x"',
    'bash -c "git push origin main"',
    'bash -lc "gh pr create"',
    # Options after ``-c`` (issue #4565): the shell runs the first non-option
    # operand, so the gate must judge that operand, not the option token.
    'bash -c -- "gh pr merge 5"',
    'sh -c -e "git push origin main"',
    'bash -c -eo pipefail "gh pr merge 5"',
    'bash -c + "gh pr merge 5"',
    'bash -c -oo pipefail errexit "gh pr merge 5"',
    'echo "gh pr merge 5" | bash -eo pipefail',
    # The exact shapes observed in PR #2130's log (issue #2280).
    "gh pr create --title 'Promote branch' --head maestro/task/3ce652",
    "git push origin maestro/task/abc123:main",
    # The checkout-resolving module spelling of the gate-lease wrapper (r1):
    # `python -m maestro.cli gate-lease -- <cmd>` execs its child directly just
    # like the bare console script, so a mutation smuggled behind the
    # interpreter must be unwrapped and denied on both enforcement paths.
    "python3.12 -m maestro.cli gate-lease -- gh pr merge 5 --squash",
    "python3 -m maestro gate-lease -- git push origin main",
    # A leading interpreter option (verified to still run the module) must not
    # shift ``-m`` out of reach of the unwrap — both enforcement paths have to
    # close the same PR/push-behind-the-interpreter hole even behind a benign
    # flag, or ``-I``/``-Im``/``-X utf8`` re-opens it (r1).
    "python3.12 -I -m maestro.cli gate-lease -- gh pr merge 5",
    "python3.12 -Im maestro.cli gate-lease -- git push origin main",
    "python3.12 -X utf8 -m maestro.cli gate-lease -- gh pr create --title x",
    # Outward reach: repo/release/secret writes, their wrapper forms, and tag
    # pushes.
    "gh repo edit o/r --visibility public",
    "gh repo delete o/r",
    "gh release create v1",
    "gh secret set TOKEN",
    'bash -lc "gh release create v1"',
    "git push --tags",
    "git push origin --tags",
    "git push --follow-tags",
)

GATE_PARITY_ALLOW_COMMANDS: tuple[str, ...] = (
    'git commit -m "msg"',
    "git push origin maestro/task/abc123",
    "git -C /repo push origin maestro/task/abc123",
    "git status",
    "pytest -q",
    "gh pr list",
    "gh pr view 5",
    "gh pr comment 5 --body-file ack.md",  # benign verb (proposal 064)
    "gh pr close 2812",  # reversible disposal (owned-disposal lever)
    # Threaded reply to an inline review comment — benign like `gh pr comment`
    # and mandated by the PR-remediation flow (#2819).
    "gh api repos/o/r/pulls/7/comments/123/replies -X POST -f body=x",
    "gh issue create --title pr",
    "gh api repos/o/r/pulls",
    'bash -c "pytest -q"',
    "ls -la",
    # Module-form gate-lease wrapping a benign child is allowed on both paths,
    # exactly like the bare console script (r1).
    "python3.12 -m maestro.cli gate-lease -- pytest -q",
    "gh repo view",
)


# ---------------------------------------------------------------------------
# evaluate_bash_command — deny classes
# ---------------------------------------------------------------------------

class TestDeniedCommands:
    @pytest.mark.parametrize(
        "command",
        [
            "gh pr create --title x --body y",
            "gh pr merge 5 --squash",
            "gh pr ready 5",
            "gh pr",            # no verb — fail closed
            "gh pr reopen 5",   # inverse of the allowed close — fail closed
            "gh pr edit 5",
            "gh --repo o/r pr create",   # global flag before subcommand
            "gh -R o/r pr merge 5",
        ],
    )
    def test_pr_subcommands_denied(self, command: str) -> None:
        reason = evaluate_bash_command(command, _BRANCH_SET)
        assert reason is not None
        assert "pull request" in reason

    def test_pr_comment_allowed_but_siblings_denied(self) -> None:
        """``gh pr comment`` is benign (proposal 064): it must be allowed so the
        overflow-finding acknowledgement comment can be created, while the other
        non-read ``gh pr`` verbs stay fail-closed."""
        assert evaluate_bash_command("gh pr comment 5 --body x", _BRANCH_SET) is None
        for verb in ("create", "merge", "ready", "reopen", "edit", "review"):
            command = f"gh pr {verb} 5"
            assert evaluate_bash_command(command, _BRANCH_SET) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "gh api repos/o/r/pulls -X POST -f title=x",
            "gh api repos/o/r/pulls/3/merge -X PUT",
            "gh api repos/o/r/pulls --method=PATCH",
            "gh api repos/o/r/pulls -XDELETE",
            "gh api repos/o/r/pulls -f title=x",
            "gh api repos/o/r/pulls --field title=x",
            "gh api repos/o/r/pulls/3/merge",
            "gh --repo o/r api repos/o/r/pulls -X POST",  # global flag first
            # #4097: an `--input` body is an implied POST too.
            "gh api repos/o/r/pulls --input pr.json",
            "gh api repos/o/r/pulls --input=pr.json",
            "echo {} | gh api repos/o/r/pulls --input -",
            "gh api --input pr.json repos/o/r/pulls",
            # #4383: pflag's attached short-value form is a body too.
            "gh api repos/o/r/pulls -fhead=maestro/task/x -fbase=main -ftitle=t",
            "gh api repos/o/r/pulls/5 -Fstate=closed",
            "gh api repos/o/r/pulls/5/requested_reviewers -freviewers[]=x",
            "gh api -fhead=a -fbase=main repos/o/r/pulls",
            "gh api repos/o/r/pulls -f=title=x",
            "gh api repos/o/r/pulls/5/update-branch -X=PUT",
            "gh api repos/o/r/pulls/comments/9 -X=DELETE",
        ],
    )
    def test_mutating_pulls_api_denied(self, command: str) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is not None

    @pytest.mark.parametrize(
        "command",
        [
            # Issue #4045: value-taking flags placed *before* the endpoint must
            # not have their value mistaken for the path.
            "gh api -X PUT repos/o/r/pulls/5/merge",
            "gh api --method POST repos/o/r/pulls -f head=b -f base=main",
            'gh api -H "Accept: x" repos/o/r/pulls/5/merge',
            'gh api --header "Accept: x" -X PUT repos/o/r/pulls/5/merge',
            "gh api -q .x -X PUT repos/o/r/pulls/5/merge",
            "gh api --input body.json repos/o/r/pulls/5/merge",
            "gh api --hostname h repos/o/r/pulls -f title=x",
            # The GitHub-docs multi-line form and a path-first continuation.
            'gh api \\\n  --method PUT \\\n'
            '  -H "Accept: application/vnd.github+json" \\\n'
            "  /repos/O/R/pulls/5/merge",
            "gh api \\\n  repos/o/r/pulls/5/merge \\\n  -X PUT",
            # Two positionals leave the endpoint ambiguous: fail closed.
            "gh api -X PUT repos/o/r/pulls/5/merge extra/positional",
        ],
    )
    def test_mutating_pulls_api_flags_first_denied(self, command: str) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "gh api repos/o/r/pulls/5",
            'gh api -H "Accept: x" repos/o/r/pulls/5',
            "gh api -q .title repos/o/r/pulls/5",
            # Ambiguous positionals with no `pulls` stay out of the gate's scope.
            "gh api graphql extra -f query=x",
            "gh api",
        ],
    )
    def test_pulls_api_reads_allowed(self, command: str) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is None

    def test_gh_api_endpoint_skips_flag_values(self) -> None:
        """The replies carve-out must match on the real path, not a flag value."""
        args = [
            "--method", "POST", "repos/o/r/pulls/7/comments/123/replies",
            "-f", "body=x",
        ]
        assert _gh_api_endpoint(args) == "repos/o/r/pulls/7/comments/123/replies"

    def test_gh_api_endpoint_glued_forms_consume_nothing(self) -> None:
        args = ["-XPUT", "--method=POST", "repos/o/r/pulls/5"]
        assert _gh_api_endpoint(args) == "repos/o/r/pulls/5"

    def test_gh_api_endpoint_empty_and_ambiguous(self) -> None:
        assert _gh_api_endpoint(["-X"]) == ""
        assert _gh_api_endpoint(["a", "b"]) is None

    @pytest.mark.parametrize(
        "command",
        [
            "gh \\\n  pr merge 5",
            "git \\\n  push origin main",
            "git push \\\n  origin main",
        ],
    )
    def test_line_continuation_denied(self, command: str) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is not None

    @pytest.mark.parametrize(
        "command",
        [
            f"git \\\n  push origin {_BRANCH}",
            "gh \\\n  pr view 5",
        ],
    )
    def test_line_continuation_allowed(self, command: str) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is None

    def test_join_line_continuations_respects_quotes(self) -> None:
        assert _join_line_continuations("a \\\nb") == "a b"
        assert _join_line_continuations('"a\\\nb"') == '"ab"'
        assert _join_line_continuations("'a\\\nb'") == "'a\\\nb'"
        assert _join_line_continuations('a \\"b') == 'a \\"b'

    @pytest.mark.parametrize(
        "command",
        [
            # The dedicated review-comment replies endpoint, in every idiomatic
            # flag ordering — path-first (the form GitHub docs use) must NOT be
            # denied just because `pulls` is then visible to the path scanner.
            "gh api repos/o/r/pulls/7/comments/123/replies -X POST -f body=x",
            "gh api repos/o/r/pulls/7/comments/123/replies --method POST "
            "-f body=x",
            "gh api repos/o/r/pulls/7/comments/123/replies -f body=x",
            "gh api --method POST repos/o/r/pulls/7/comments/123/replies "
            "-f body=x",
            'gh api -X POST -H "Accept: x" '
            "repos/o/r/pulls/7/comments/123/replies -f body=x",
            "gh api repos/o/r/pulls/7/comments/123/replies/ -X POST",
            # The older reply form: POST to the comments collection with an
            # `in_reply_to` field.
            "gh api repos/o/r/pulls/7/comments -f in_reply_to=123 -f body=x",
            "gh api repos/o/r/pulls/7/comments --field in_reply_to=123 "
            "-f body=x",
            "gh api repos/o/r/pulls/7/comments --field=in_reply_to=123 "
            "-f body=x",
            "gh api repos/o/r/pulls/7/comments/1/replies --input body.json",
            # #4383: the attached short form is recognised as a reply too.
            "gh api repos/o/r/pulls/7/comments -fin_reply_to=123 -fbody=x",
            "gh api repos/o/r/pulls/7/comments -Fin_reply_to=123 -f body=x",
            "gh api repos/o/r/pulls/7/comments -fbody=x -fin_reply_to=123",
        ],
    )
    def test_review_comment_reply_allowed(self, command: str) -> None:
        """Replying to an inline review comment is benign — it cannot open,
        merge, ready, or push a PR — so the PR-remediation threaded-reply
        mandate (#2819) must not be denied in any idiomatic ordering."""
        assert evaluate_bash_command(command, _BRANCH_SET) is None

    @pytest.mark.parametrize(
        "command",
        [
            # A fresh top-level review comment (no `in_reply_to`) is NOT a reply
            # — the carve-out must not widen to it; it stays denied fail-closed.
            "gh api repos/o/r/pulls/7/comments -X POST -f body=x",
            # `replies` appearing as a field value, not the path, must not open
            # the carve-out for a genuine PR mutation.
            "gh api repos/o/r/pulls -X POST -f body=replies",
            # An `in_reply_to` field on a non-comments pulls path (e.g. the PR
            # itself) must not be treated as a benign reply.
            "gh api repos/o/r/pulls/7 -X PATCH -f in_reply_to=123",
            "gh api repos/o/r/pulls/7 -X PATCH -fin_reply_to=123",
            "gh api repos/o/r/pulls/7/comments -fbody=x -fpath=a -fline=1",
            # An `in_reply_to` hidden in an opaque `--input` body is invisible
            # to the gate, so it stays denied fail-closed (#4097).
            "gh api repos/o/r/pulls/7/comments --input body.json",
        ],
    )
    def test_non_reply_pulls_mutations_still_denied(self, command: str) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is not None

    @pytest.mark.parametrize(
        "command",
        [
            "git push origin main",
            "git push origin master",
            "git push origin HEAD:main",
            "git push origin +main",
            "git push --force origin main",
            "git push",                       # bare push — fail closed
            "git push origin",                # no refspec — fail closed
            "git push origin feature-x",      # not the run's own branch
            # Global value-flags must not hide the ``push`` subcommand behind
            # their value token (issue #2280 regression).
            "git -C /repo push origin main",
            "git -c key=val push origin main",
            "git --git-dir=/r/.git push origin main",
            "git --work-tree /r push origin main",
        ],
    )
    def test_protected_or_foreign_pushes_denied(self, command: str) -> None:
        reason = evaluate_bash_command(command, _BRANCH_SET)
        assert reason is not None
        assert "branch" in reason

    @pytest.mark.parametrize(
        "command",
        [
            "a && gh pr create --title x",
            "GH_TOKEN=tok gh pr create",
            "env GH_TOKEN=tok gh pr create",
            "env --unset=X gh pr create",      # env flag before program
            "env -u FOO gh pr merge 1",        # env flag consuming a value
            "a&&gh pr create",
            "ls; gh pr merge 1",
        ],
    )
    def test_chained_and_env_prefixed_denied(self, command: str) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is not None

    @pytest.mark.parametrize("strip", [False, True])
    @pytest.mark.parametrize(
        "command",
        [
            "git add -A\ngit push origin main",
            "cd /r\ngh pr create --fill",
            "git status\ngh pr merge 5 --squash",
            "git fetch origin\ngit push origin HEAD:main",
            "for b in x; do git push origin main; done",
            "if true; then gh pr merge 5; fi",
            "if gh pr merge 5; then echo ok; fi",
            "if FOO=1 gh pr merge 5; then echo ok; fi",
            "while true; do gh pr merge 5; done",
            "{ gh pr merge 5; }",
            "time gh pr merge 5",
            "time -p env X=1 git push origin main",
            "! gh pr merge 5",
            "a &&\ngh pr create",
            "ls;\ngh pr merge 1",
            "git push \\\n  origin main",   # line continuation stays joined
            "xargs git push origin main",
            "nohup gh pr merge 5",
            "command gh pr create",
            'git commit -m "it\'s" && git push \\\n origin main',
            "xargs git status gh pr merge 5",
            "git log # inspect\ngit push origin main",
            "gh pr view 1 # note\ngh pr merge 2",
            "git log # it's\ngit push origin main",
        ],
    )
    def test_newline_and_reserved_word_commands_denied(
        self, command: str, strip: bool,
    ) -> None:
        # #4044: newline lists, compound-command reserved words, and a gated
        # program behind an unrecognised launcher classify like ``;`` chains.
        reason = evaluate_bash_command(
            command, _BRANCH_SET, strip_redirections=strip,
            current_branch=_BRANCH,
        )
        assert reason is not None

    @pytest.mark.parametrize("strip", [False, True])
    @pytest.mark.parametrize(
        "command",
        [
            'timeout 300 bash -c "gh pr merge 5 --squash"',
            'nohup sh -c "git push origin main" &',
            'command bash -c "gh pr merge 5"',
            'timeout 900 maestro gate-lease -- bash -c "gh pr merge 5"',
            "timeout 900 python3.14 -m maestro.cli gate-lease -- "
            'bash -c "gh pr create --fill"',
            'timeout 300 bash -lc "gh pr merge 5"',
            'sudo -u me bash -c "git push origin main"',
            'xargs -I{} sh -c "gh pr create --fill"',
            'nice -n 10 bash -c "gh pr ready 5"',
            f'timeout 60 bash -c "git push --force origin {_BRANCH}"',
        ],
    )
    def test_wrapper_behind_launcher_denied(
        self, command: str, strip: bool,
    ) -> None:
        # #4147: a shell ``-c`` wrapper or gate-lease launcher behind an
        # unrecognised launcher is unwrapped like the leading form.
        reason = evaluate_bash_command(
            command, _BRANCH_SET, strip_redirections=strip,
            current_branch=_BRANCH,
        )
        assert reason is not None

    @pytest.mark.parametrize(
        "command",
        [
            f"git push origin {_BRANCH} # it's mine",
            f"git status # don't\ngit push origin {_BRANCH}",
            f'git commit -m "#1 it\'s" && git push origin {_BRANCH}',
        ],
    )
    def test_owned_push_with_comment_or_hash_allowed(
        self, command: str,
    ) -> None:
        # A quote inside an unquoted ``#`` comment must not unbalance the
        # lexer; a quoted ``#`` is not a comment.
        reason = evaluate_bash_command(
            command, _BRANCH_SET, current_branch=_BRANCH,
        )
        assert reason is None

    @pytest.mark.parametrize(
        "command",
        [
            'bash -c "gh pr create --title x"',
            "sh -c 'gh pr merge 5'",
            'bash -c "a && gh pr create"',
            'bash -c "git push origin main"',
            "dash -c 'gh pr ready 5'",
            # Issue #4565: options after -c precede the real script operand.
            'bash -c -- "gh pr merge 5"',
            'bash -c -e "git push origin main"',
            'bash -c -x "gh pr merge 5"',
            'bash -c +e "gh pr merge 5"',
            'bash -c -o pipefail "gh pr merge 5"',
            'bash -c --norc "gh pr merge 5"',
            'sh -c -- "git push origin main"',
            'bash -c - "gh pr merge 5"',
            'timeout 60 bash -c -- "gh pr merge 5"',
            'xargs sh -c -e "gh pr merge 5"',
            'bash -c -eo pipefail "gh pr merge 5"',
            'timeout 60 bash -c -xo pipefail "git push origin main"',
            'bash -c +eO extglob "gh pr merge 5"',
            'bash -c + "gh pr merge 5"',
            'sh -c + "git push origin main"',
            'timeout 60 bash -c + "gh pr merge 5"',
            'bash -c -oo pipefail errexit "gh pr merge 5"',
            'bash -c -oO pipefail extglob "gh pr merge 5"',
            'sh -c -oo errexit nounset "git push origin main"',
            'bash -c "-x; gh  pr merge 5"',
            'bash -c "+x; gh  pr merge 5"',
            'bash -c "-x; gh pr merge 5" "echo hi"',
        ],
    )
    def test_shell_wrapper_mutation_unwrapped_and_denied(
        self, command: str,
    ) -> None:
        # bash -c / sh -c wrappers are unwrapped and the inner command gated.
        assert evaluate_bash_command(command, _BRANCH_SET) is not None

    def test_shell_wrapper_options_after_c_evaluate_real_operand(
        self,
    ) -> None:
        # The real operand is evaluated, not blanket-denied (issue #4565).
        for command in (
            'bash -c -- "pytest -q"',
            'bash -c -e "git status"',
            'bash -c -o pipefail "ruff check maestro/"',
        ):
            assert evaluate_bash_command(command, _BRANCH_SET) is None
        own_push = f'bash -c -e "git push origin {_BRANCH}"'
        reason = evaluate_bash_command(
            own_push, _BRANCH_SET, current_branch=_BRANCH,
        )
        assert reason is None

    @pytest.mark.parametrize(
        "command",
        [
            # ``-o`` consumes the string as its value: no script operand.
            'bash -c -o "gh pr merge 5"',
            # A script file operand precedes ``-c``: not a -c invocation.
            'bash -x script.sh -c "gh pr merge 5"',
        ],
    )
    def test_shell_wrapper_without_script_operand_fails_closed(
        self, command: str,
    ) -> None:
        reason = evaluate_bash_command(command, _BRANCH_SET)
        assert reason is not None
        assert "fail-closed" in reason

    def test_shell_wrapper_bare_c_without_operand_allowed(self) -> None:
        # Nothing gated anywhere and no operand: nothing to deny.
        assert evaluate_bash_command("bash -c -e", _BRANCH_SET) is None

    @pytest.mark.parametrize(
        "command",
        [
            'bash -lc "gh pr create"',     # combined flag — can't extract -c
            "sh -ic 'git push origin main'",
        ],
    )
    def test_shell_wrapper_combined_flag_fails_closed(
        self, command: str,
    ) -> None:
        reason = evaluate_bash_command(command, _BRANCH_SET)
        assert reason is not None
        assert "fail-closed" in reason

    @pytest.mark.parametrize(
        "command",
        [
            # `maestro gate-lease -- <cmd>` execs its child directly, so a PR/push
            # mutation smuggled as that child must be unwrapped and denied (r4 /
            # proposal 126) — the same hole `bash -c "…"` opens.
            "maestro gate-lease -- gh pr merge 5 --squash",
            "maestro gate-lease -- gh pr create --title x",
            "maestro gate-lease -- git push origin main",
            # REMAINDER form without an explicit `--` separator.
            "maestro gate-lease gh pr merge 5",
            # A shell-wrapped child rides the ordinary wrapper dispatch on
            # re-evaluation.
            "maestro gate-lease -- bash -c 'gh pr merge 5'",
            # Chained after a benign segment — split then unwrapped.
            "pytest -q && maestro gate-lease -- gh pr merge 5",
            # gate-lease wrapping gate-lease wrapping a mutation.
            "maestro gate-lease -- maestro gate-lease -- gh pr merge 5",
            # The checkout-resolving module spelling the hand-run gate docs use
            # (r1): `python -m maestro.cli gate-lease -- <cmd>` must unwrap its
            # child exactly like the bare console script, in every interpreter
            # name / module spelling / separator form.
            "python3.12 -m maestro.cli gate-lease -- gh pr merge 5 --squash",
            "python3 -m maestro gate-lease -- git push origin main",  # -m maestro
            "python -m maestro.cli gate-lease gh pr merge 5",  # REMAINDER, no `--`
            "python3.12 -mmaestro.cli gate-lease -- gh pr create --title x",  # -mmod
            # A module-form gate-lease nested inside a bare gate-lease is still
            # unwrapped, and vice-versa; depth stays bounded.
            "maestro gate-lease -- python3.12 -m maestro.cli gate-lease -- "
            "gh pr merge 5",
            # A leading interpreter option must not shift ``-m`` out of the
            # unwrap's reach (r1): each of these forms was verified to actually
            # run the module, so the smuggled child still execs — the scan skips
            # the leading options with real getopt semantics and denies it.
            "python3.12 -I -m maestro.cli gate-lease -- gh pr merge 5",  # value-less
            "python3.12 -IB -m maestro gate-lease -- git push origin main",  # bundle
            "python3.12 -Im maestro.cli gate-lease -- gh pr create --title x",  # -Im
            "python3.12 -X utf8 -m maestro.cli gate-lease -- gh pr merge 5",  # -X val
            "python3.12 -Xutf8 -m maestro.cli gate-lease -- git push origin main",
            "python3.12 --check-hash-based-pycs always -m maestro.cli "
            "gate-lease -- gh pr merge 5",  # long value-taking option
            "python3.12 --check-hash-based-pycs=always -m maestro.cli "
            "gate-lease -- git push origin main",  # long option, ``=`` value
        ],
    )
    def test_gate_lease_wrapper_mutation_unwrapped_and_denied(
        self, command: str,
    ) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is not None

    def test_unwrap_depth_exceeded_fails_closed(self) -> None:
        # A PR/push-shaped command past the unwrap-depth bound fails closed
        # even though `gh pr list` would normally be allowed.
        assert (
            _evaluate_command("gh pr list", _BRANCH_SET, _MAX_UNWRAP_DEPTH + 1)
            == _PARSE_DENY
        )

    def test_gate_lease_unwrap_depth_exceeded_fails_closed(self) -> None:
        # A gate-lease-wrapped child past the unwrap-depth bound fails closed,
        # bounding a gate-lease-wrapping-gate-lease chain (r4 / proposal 126).
        assert (
            _maestro_deny(
                ["gate-lease", "--", "pytest", "-q"], _BRANCH_SET,
                _MAX_UNWRAP_DEPTH + 1,
            )
            == _PARSE_DENY
        )

    @pytest.mark.parametrize("strip", [False, True])
    @pytest.mark.parametrize(
        "command",
        [
            'git commit -m "line1\nline2"',
            "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"",
            "git add -A\ngit commit -m x\ngit push origin maestro/task/abc123",
            "git push \\\n  origin maestro/task/abc123",
            "gh pr close 5\ngh pr view 5",
            "nohup git push origin maestro/task/abc123",
            "time -p pytest -q",
            "echo 'a \\\n b'",
            'echo \\"quoted\\" x',  # non-newline backslash escape kept
            'git commit -m "it\'s" && git push \\\n origin maestro/task/abc123',
        ],
    )
    def test_newline_and_reserved_word_allow_controls(
        self, command: str, strip: bool,
    ) -> None:
        # #4044 allow controls: quoted multi-line arguments stay one token,
        # and owned pushes spread over lines / continuations stay allowed.
        assert evaluate_bash_command(
            command, _BRANCH_SET, strip_redirections=strip,
            current_branch=_BRANCH,
        ) is None

    def test_reserved_word_only_segment_allowed(self) -> None:
        # A segment that is nothing but reserved words has no program.
        assert evaluate_bash_command("if true; then { :; }; fi", _BRANCH_SET) is None

    def test_push_denied_when_no_allowed_branch(self) -> None:
        # Fail closed: with no run branch known, every push is denied.
        assert evaluate_bash_command(
            "git push origin maestro/task/abc123", None,
        ) is not None

    def test_unparseable_pr_shaped_command_fails_closed(self) -> None:
        # Unbalanced quote => tokenizer fails; pr/push-shaped => deny.
        reason = evaluate_bash_command('gh pr create "unterminated', _BRANCH_SET)
        assert reason is not None
        assert "fail-closed" in reason

    def test_unparseable_push_shaped_command_fails_closed(self) -> None:
        assert evaluate_bash_command('git push "unterminated', _BRANCH_SET) is not None

    def test_unparseable_pulls_shaped_command_fails_closed(self) -> None:
        assert evaluate_bash_command(
            'gh api repos/o/r/pulls "unterminated', _BRANCH_SET,
        ) is not None


# ---------------------------------------------------------------------------
# evaluate_bash_command — allow path
# ---------------------------------------------------------------------------

class TestAllowedCommands:
    def test_nested_launcher_wrappers_evaluated_linearly(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # r1: k launcher-wrapper tokens before one ``-c`` evaluate its payload
        # once per unwrap level, not k^depth times.
        calls: list[str] = []
        real = tool_gating._evaluate_command

        def counting(command: str, *args: object, **kwargs: object) -> object:
            calls.append(command)
            return real(command, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(tool_gating, "_evaluate_command", counting)
        command = "pytest -q"
        for _ in range(4):
            command = "timeout 1 sh bash sh bash -c " + shlex.quote(command)
        assert evaluate_bash_command(command, _BRANCH_SET) is None
        assert len(calls) == 5

    def test_nested_launcher_wrapper_still_denies(self) -> None:
        # r1 control: the dedupe keeps the innermost mutation reachable.
        command = "gh pr merge 5"
        for _ in range(4):
            command = "timeout 1 sh bash -c " + shlex.quote(command)
        assert evaluate_bash_command(command, _BRANCH_SET) is not None

    @pytest.mark.parametrize("strip", [False, True])
    @pytest.mark.parametrize(
        "command",
        [
            'timeout 300 bash -c "pytest -q"',
            f'nohup sh -c "git push origin {_BRANCH}"',
            "timeout 900 maestro gate-lease -- pytest -q",
            'rg -t sh "git push" maestro/',
            'rg --type sh "gh pr create" scripts/',
            "rg -t sh -n pulls .",
        ],
    )
    def test_benign_wrapper_behind_launcher_allowed(
        self, command: str, strip: bool,
    ) -> None:
        # #4147 control: re-dispatching behind a launcher must not over-deny.
        reason = evaluate_bash_command(
            command, _BRANCH_SET, strip_redirections=strip,
            current_branch=_BRANCH,
        )
        assert reason is None

    @pytest.mark.parametrize(
        "command",
        [
            'git commit -m "msg"',
            "git push origin maestro/task/abc123",
            "git push origin maestro/task/abc123:maestro/task/abc123",
            # Global value-flag, own-branch push — allowed (still needs a PR
            # + approval to reach main).
            "git -C /repo push origin maestro/task/abc123",
            # ``push`` only appears as a flag value / message — not a push.
            "git -c push.default=current status",
            'git commit -m "push to main"',
            "git status",
            "git add -A",
            "pytest -q",
            "gh pr list",
            "gh pr view 5",
            "gh pr diff 5",
            # ``gh pr comment`` is a benign mutating verb (proposal 064): the
            # PR-remediation flow needs it to create the overflow-finding
            # acknowledgement comment.  It cannot open/merge/push a PR.
            "gh pr comment 5 --body-file ack.md",
            'gh pr comment 5 --body "of-deadbeef — Fixed locally"',
            # ``gh pr close`` is a reversible disposal (owned-disposal lever):
            # it retracts an own-authored, git-unlanded cure and cannot
            # merge/ready/open/push a PR.
            "gh pr close 2812",
            "gh --repo o/r pr close 2812",       # global flag + disposal verb
            "gh --repo o/r pr comment 5 --body x",  # global flag + benign verb
            "gh --repo o/r pr list",            # global flag, read verb
            # ``pr`` as an argument *value* of a non-PR subcommand — allowed.
            "gh issue create --title pr",
            "gh issue comment 5 --body pr",
            "gh release view v1 --jq pr",
            "gh api repos/o/r/pulls",          # read-only list — allowed
            "gh api repos/o/r/pulls -X",        # method flag without value
            "gh api user",                      # non-pulls endpoint — allowed
            "gh api repos/o/r/issues -X POST",  # mutating but not a PR
            "gh api",                           # no path positional
            "gh issue list",
            "gh --version",                     # no gh subcommand
            "ls -la",
            'bash -c "pytest -q"',              # wrapper around benign cmd
            "sh -c 'git status'",
            "bash script.sh",                   # script file, no -c to unwrap
            # `maestro gate-lease` wrapping a benign gate command is allowed
            # (r4 / proposal 126): the unwrapped child is not pr/push-shaped.
            "maestro gate-lease -- pytest tests/ -n 4",
            "maestro gate-lease -- pytest tests/ {worker_args}",
            "maestro gate-lease --enabled",     # flag probe, execs no child
            "maestro gate-lease",               # gate-lease with no child at all
            "maestro gate-lease --",            # trailing marker, empty child
            "maestro gate-classify --tool tests --returncode 1",  # other subcmd
            "maestro cost-report",              # non-gate-lease maestro subcmd
            "maestro",                          # bare maestro, no subcommand
            # Module-form gate-lease (r1) mirrors the bare console script: a
            # benign child is allowed, and every non-``gate-lease`` /
            # non-maestro-module shape falls through to allow.
            "python3.12 -m maestro.cli gate-lease -- python3.12 -m pytest "
            "tests/ -v",                        # documented invocation; benign
            "python3.12 -m maestro.cli gate-lease --enabled",  # flag probe
            "python3.12 -m maestro.cli cost-report",  # module-form, other subcmd
            "python3.12 -m pytest tests/ -v",   # -m, module is not maestro
            # A leading interpreter option is skipped before ``-m`` is read, so
            # a non-maestro module still falls through to allow (r1).
            "python3.12 -I -m pytest tests/ -v",
            "python3.12 -m",                    # -m with no module argument
            "python3.12",                       # python interpreter, no args
            "python3.12 script.py",             # python running a script, not -m
            # ``-c`` runs a command string (not a ``-m`` module), ``-`` reads
            # from stdin, and ``--`` ends option parsing — none is a maestro
            # module invocation, so the unwrap declines and lets them through.
            'python3.12 -c "print(1)"',
            "python3.12 -",
            "python3.12 -- script.py",
            "python3.12 -I -B",                 # only interpreter flags, no -m
            "FOO=bar",                          # env assignment only
            "",                                 # empty
            "   ",                              # whitespace only
        ],
    )
    def test_benign_commands_allowed(self, command: str) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is None

    def test_unparseable_benign_command_allowed(self) -> None:
        # Tokenizer fails but the command is not pr/push-shaped => allow.
        assert evaluate_bash_command('echo "unterminated', _BRANCH_SET) is None


# ---------------------------------------------------------------------------
# evaluate_bash_command — owned-branch *set* widening (proposal 093)
# ---------------------------------------------------------------------------

class TestBranchSetWidening:
    """The decision core authorizes a *set* of owned branches, not one string.

    This increment only ever receives ≤1-element sets from the factory shims,
    but the core itself must already honour a genuine multi-branch set, deny a
    destination outside it, and fail closed on an empty set."""

    def test_multi_branch_push_to_member_allowed(self) -> None:
        # A push whose destination is *any* member of the owned set is allowed.
        allowed = frozenset({"maestro/evolve/a", "maestro/evolve/b"})
        assert evaluate_bash_command(
            "git push origin maestro/evolve/b", allowed,
        ) is None
        assert evaluate_bash_command(
            "git push origin maestro/evolve/a", allowed,
        ) is None

    def test_push_to_non_member_denied(self) -> None:
        # A destination outside the owned set is denied even when the set is
        # non-empty.
        reason = evaluate_bash_command(
            "git push origin maestro/evolve/c",
            frozenset({"maestro/evolve/a"}),
        )
        assert reason is not None
        assert "branch" in reason

    def test_partial_multi_refspec_push_denied(self) -> None:
        # Every refspec must be in the set; one out-of-set ref denies the push.
        allowed = frozenset({"maestro/evolve/a", "maestro/evolve/b"})
        assert evaluate_bash_command(
            "git push origin maestro/evolve/a maestro/evolve/c", allowed,
        ) is not None

    def test_empty_set_fails_closed(self) -> None:
        # A falsy set (empty) denies every push, exactly as ``None`` does.
        assert evaluate_bash_command(
            "git push origin maestro/task/abc123", frozenset(),
        ) is not None

    def test_one_element_set_reproduces_single_branch_verdicts(self) -> None:
        # The one-element set the shims build must reproduce the pre-set
        # single-branch behaviour: own-branch push allowed, foreign denied.
        assert evaluate_bash_command(
            "git push origin maestro/task/abc123", _BRANCH_SET,
        ) is None
        assert evaluate_bash_command(
            "git push origin feature-x", _BRANCH_SET,
        ) is not None


# ---------------------------------------------------------------------------
# evaluate_bash_command — force-push denial (proposal 093, new behavior)
# ---------------------------------------------------------------------------

class TestForcePushDenied:
    """Force-pushing rewrites a branch's published history and is denied
    unconditionally — even to an otherwise-owned branch — so a human reviews an
    append-only PR.  This is net-new denial: today's ``+`` stripping and flag
    filtering silently permit a force-push to the run's own branch."""

    @pytest.mark.parametrize(
        "command",
        [
            "git push --force origin maestro/task/abc123",
            "git push -f origin maestro/task/abc123",
            "git push --force-with-lease origin maestro/task/abc123",
            "git push --force-with-lease=origin/maestro/task/abc123 "
            "origin maestro/task/abc123",
            "git push --force-if-includes origin maestro/task/abc123",
            "git push origin +maestro/task/abc123",
            "git push origin +HEAD:maestro/task/abc123",
            # A force-push hidden behind a value-taking global flag still trips.
            "git -C /repo push --force origin maestro/task/abc123",
            # git accepts any unique long-option prefix.
            "git push --forc origin maestro/task/abc123",
            "git push --force-with origin maestro/task/abc123",
        ],
    )
    def test_force_push_to_owned_branch_denied(self, command: str) -> None:
        reason = evaluate_bash_command(command, _BRANCH_SET)
        assert reason is not None
        assert "branch" in reason

    def test_non_force_push_to_owned_branch_still_allowed(self) -> None:
        # The force check must not over-reach: a plain own-branch push (with a
        # benign flag like ``-u``) stays allowed.
        assert evaluate_bash_command(
            "git push -u origin maestro/task/abc123", _BRANCH_SET,
        ) is None


class TestMultiRefPushDenied:
    """``git push --all`` / ``--mirror`` push *every* local branch (including the
    protected ``main``/``master``), so they are denied fail-closed even when the
    run's current branch is owned — an owned ``current_branch`` resolves only a
    single destination and must never launder a multi-ref push into an allow.
    This closes the proposal-099 gap where the widened refspec-less / bare-HEAD
    parsing let ``--all``/``--mirror`` slip through."""

    @pytest.mark.parametrize(
        "command",
        [
            "git push --all",
            "git push --mirror",
            "git push origin --all",
            "git push origin --mirror",
            # ``--all`` overrides the ``HEAD`` refspec: it still pushes every
            # branch, so bare-HEAD resolution must not rescue it.
            "git push origin HEAD --all",
            "git push origin HEAD --mirror",
            # A value-taking global flag must not hide the multi-ref push.
            "git -C /repo push --all",
            # Tag pushes publish refs beyond the owned branch (156 W2).
            "git push --tags",
            "git push origin --tags",
            "git push --follow-tags",
            "git push origin HEAD --tags",
            # git accepts any unique long-option prefix.
            "git push origin HEAD --tag",
            "git push origin HEAD --ta",
            "git push origin HEAD --follow-tag",
            "git push origin HEAD --al",
            "git push origin HEAD --mirr",
        ],
    )
    def test_all_and_mirror_push_denied_even_on_owned_current_branch(
        self, command: str,
    ) -> None:
        reason = evaluate_bash_command(
            command, _BRANCH_SET, current_branch=_BRANCH,
        )
        assert reason is not None
        assert "--all / --mirror" in reason

    def test_single_flag_push_not_over_denied(self) -> None:
        # The multi-ref guard must not over-reach onto an unrelated flag: an
        # ``-u`` / ``--no-verify``-carrying own-branch push resolves to a single
        # destination and stays allowed.
        assert evaluate_bash_command(
            "git push -u origin HEAD", _BRANCH_SET, current_branch=_BRANCH,
        ) is None
        assert evaluate_bash_command(
            "git push --no-verify origin HEAD", _BRANCH_SET,
            current_branch=_BRANCH,
        ) is None


# ---------------------------------------------------------------------------
# evaluate_bash_command — redirection-stripping flag is inert by default (096)
# ---------------------------------------------------------------------------

class TestRedirectStrippingInertByDefault:
    """The proposal-096 ``strip_redirections`` parameter defaults ``False`` and
    is byte-identical to today when off.  An output-captured own-branch push
    still fails closed (its leaked file-descriptor / redirect-target tokens read
    as stray refspecs), and every redirect shape produces the *same* verdict
    whether the parameter is omitted or explicitly ``False`` — pinning that a
    flag-off tree behaves exactly like the pre-096 one."""

    #: Own-branch pushes wrapped in each realistic redirect idiom.  With the
    #: flag off every one fails closed today because the redirect tokens leak
    #: into the positional refspecs (``2>&1`` → ``2``, ``1``; ``> f`` → ``>``,
    #: ``f``); the conformance suite proves the flag flips these to *allow*.
    REDIRECT_WRAPPED_OWN_PUSHES: tuple[str, ...] = (
        "git push origin maestro/task/abc123 2>&1",
        "git push origin maestro/task/abc123 > f",
        "git push origin maestro/task/abc123 2> f",
        "git push origin maestro/task/abc123 &> f",
        "git push origin maestro/task/abc123 >| f",
        "git push origin maestro/task/abc123 1>&2",
        "git push origin maestro/task/abc123 2>&1 | tail -12",
    )

    @pytest.mark.parametrize("command", REDIRECT_WRAPPED_OWN_PUSHES)
    def test_redirect_wrapped_own_push_denied_flag_off(
        self, command: str,
    ) -> None:
        # Default (parameter omitted) still fails closed — the live bug 096
        # fixes only behind the flag.
        assert evaluate_bash_command(command, _BRANCH_SET) is not None

    @pytest.mark.parametrize("command", REDIRECT_WRAPPED_OWN_PUSHES)
    def test_omitted_equals_explicit_false(self, command: str) -> None:
        # Omitting the parameter is byte-identical to passing ``False``.
        assert evaluate_bash_command(
            command, _BRANCH_SET,
        ) == evaluate_bash_command(
            command, _BRANCH_SET, strip_redirections=False,
        )

    @pytest.mark.parametrize(
        "command",
        [
            *GATE_PARITY_DENY_COMMANDS,
            *GATE_PARITY_ALLOW_COMMANDS,
            *REDIRECT_WRAPPED_OWN_PUSHES,
        ],
    )
    def test_parity_table_byte_identical_flag_off(self, command: str) -> None:
        # Across the whole shared parity table plus the redirect shapes, the
        # flag-off verdict equals the explicit-False verdict — no drift.
        assert evaluate_bash_command(
            command, _BRANCH_SET,
        ) == evaluate_bash_command(
            command, _BRANCH_SET, strip_redirections=False,
        )


# ---------------------------------------------------------------------------
# TaskGateHook.allowed_branch — single-branch compat property (proposal 093)
# ---------------------------------------------------------------------------

class TestAllowedBranchCompatProperty:
    """The read-only ``allowed_branch`` property bridges the widened set back to
    the single-branch surface the CLI-paced provider still reads.  It is exact
    for the ≤1-element sets the shims build this increment, and declines to
    guess for a (later-increment-only) multi-element set."""

    def test_one_element_set_returns_sole_branch(self) -> None:
        assert TaskGateHook(frozenset({_BRANCH})).allowed_branch == _BRANCH

    def test_none_set_returns_none(self) -> None:
        assert TaskGateHook(None).allowed_branch is None

    def test_empty_set_returns_none(self) -> None:
        assert TaskGateHook(frozenset()).allowed_branch is None

    def test_multi_element_set_returns_none(self) -> None:
        # A genuine multi-branch set has no single answer, so the compat
        # property returns None rather than an arbitrary element.
        hook = TaskGateHook(frozenset({"maestro/evolve/a", "maestro/evolve/b"}))
        assert hook.allowed_branch is None


# ---------------------------------------------------------------------------
# permits_pr_close — the owned-disposal reachability proof (won't vs can't)
# ---------------------------------------------------------------------------

class TestPermitsPrClose:
    """The reachability predicate proving `gh pr close` is a permitted terminal.

    These pin the whole point of the owned-disposal lever: the disposal act is
    *reachable* (so a never-invoked record is a choice, ``won't``, not a gate
    block, ``can't``), while the genuine approval-bypass verbs it must never be
    confused with — ``merge``/``ready``/``open``/``reopen`` — stay blocked.
    """

    def test_close_is_permitted(self) -> None:
        # The reversible withdrawal is reachable — the proof this plan exists
        # to make a tested fact rather than a guess.
        assert permits_pr_close("gh pr close 2812") is True
        assert permits_pr_close("gh --repo o/r pr close 2812") is True
        assert permits_pr_close("gh pr close 5\n") is True

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr merge 2812",    # merge bypass — the #2280 attack shape
            "gh pr ready 2812",    # ready bypass
            "gh pr open 2812",     # open (unknown verb) — fail closed
            "gh pr reopen 2812",   # inverse of close — not a disposal
        ],
    )
    def test_bypass_verbs_stay_blocked(self, command: str) -> None:
        # The predicate delegates to the same classification, so the
        # approval-bypass verbs it must never be confused with return False.
        assert permits_pr_close(command) is False


# ---------------------------------------------------------------------------
# make_task_pretooluse_hook — PreToolUse hook shape
# ---------------------------------------------------------------------------

def _input(command: str, tool_name: str = "Bash") -> dict[str, object]:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
    }


class TestPreToolUseHook:
    @pytest.mark.asyncio
    async def test_non_bash_tool_allowed(self) -> None:
        hook = make_task_pretooluse_hook(_BRANCH_SET)
        out = await hook(_input("/x", tool_name="Read"), None, None)
        assert out == {}

    @pytest.mark.asyncio
    async def test_bash_pr_create_denied(self) -> None:
        hook = make_task_pretooluse_hook(_BRANCH_SET)
        out = await hook(_input("gh pr create --title x"), "t1", None)
        decision = out["hookSpecificOutput"]
        assert decision["hookEventName"] == "PreToolUse"
        assert decision["permissionDecision"] == "deny"
        assert "source_task_id" in decision["permissionDecisionReason"]

    @pytest.mark.asyncio
    async def test_bash_benign_allowed(self) -> None:
        hook = make_task_pretooluse_hook(_BRANCH_SET)
        out = await hook(_input("pytest -q"), None, None)
        assert out == {}

    @pytest.mark.asyncio
    async def test_non_dict_input_allowed(self) -> None:
        hook = make_task_pretooluse_hook(_BRANCH_SET)
        out = await hook("not-a-dict", None, None)
        assert out == {}

    @pytest.mark.asyncio
    async def test_non_string_command_allowed(self) -> None:
        hook = make_task_pretooluse_hook(_BRANCH_SET)
        out = await hook(_input(123), None, None)  # type: ignore[arg-type]
        assert out == {}

    @pytest.mark.asyncio
    async def test_missing_tool_input_allowed(self) -> None:
        hook = make_task_pretooluse_hook(_BRANCH_SET)
        out = await hook({"tool_name": "Bash"}, None, None)
        assert out == {}

    def test_make_task_tool_hooks_shape(self) -> None:
        hooks = make_task_tool_hooks(_BRANCH_SET)
        matchers = hooks["PreToolUse"]
        assert len(matchers) == 1
        assert matchers[0].matcher == "Bash"
        assert len(matchers[0].hooks) == 1

    @pytest.mark.asyncio
    async def test_monitor_passes_through_unwidened(self) -> None:
        hook = make_task_tool_hooks(_BRANCH_SET)["PreToolUse"][0].hooks[0]
        out = await hook(_input("git push origin main", tool_name="Monitor"), None, None)
        assert out == {}

    @pytest.mark.asyncio
    async def test_widened_gate_set_moves_matcher_and_filter(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Proposal 142 step 6: the matcher and the callback's filter both read
        # ``GATE_MATCHED_TOOLS``, so widening it moves the whole gate at once.
        monkeypatch.setattr(
            tool_surface, "GATE_MATCHED_TOOLS", frozenset({"Bash", "Monitor"}),
        )
        hooks = make_task_tool_hooks(_BRANCH_SET)
        assert hooks["PreToolUse"][0].matcher == "Bash|Monitor"
        assert task_gate_from_hooks(hooks) is not None
        hook = hooks["PreToolUse"][0].hooks[0]
        out = await hook(_input("git push origin main", tool_name="Monitor"), None, None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Faithful integration — drive the SDK control protocol (Concern: prove the
# hook fires through the real dispatch path, not just a direct call).
# ---------------------------------------------------------------------------

class _CaptureTransport:
    """Minimal transport stub capturing control-protocol writes."""

    def __init__(self) -> None:
        self.written: list[str] = []

    async def write(self, data: str) -> None:
        self.written.append(data)


class TestControlProtocolDispatch:
    @pytest.mark.asyncio
    async def test_hook_callback_request_denies_pr_create(self) -> None:
        """The SDK's ``hook_callback`` dispatch invokes our hook and emits a
        deny decision the CLI can consume."""
        hooks = make_task_tool_hooks(_BRANCH_SET)
        callback = hooks["PreToolUse"][0].hooks[0]
        transport = _CaptureTransport()
        query = Query(transport=transport, is_streaming_mode=True)  # type: ignore[arg-type]
        query.hook_callbacks["hook_0"] = callback

        await query._handle_control_request({
            "type": "control_request",
            "request_id": "r1",
            "request": {
                "subtype": "hook_callback",
                "callback_id": "hook_0",
                "input": _input("gh pr create --title x"),
                "tool_use_id": "t1",
            },
        })

        assert len(transport.written) == 1
        response = json.loads(transport.written[0])
        assert response["response"]["subtype"] == "success"
        decision = response["response"]["response"]["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "source_task_id" in decision["permissionDecisionReason"]

    @pytest.mark.asyncio
    async def test_hook_callback_request_allows_benign(self) -> None:
        """A benign command produces an empty (no-opinion) hook response."""
        hooks = make_task_tool_hooks(_BRANCH_SET)
        callback = hooks["PreToolUse"][0].hooks[0]
        transport = _CaptureTransport()
        query = Query(transport=transport, is_streaming_mode=True)  # type: ignore[arg-type]
        query.hook_callbacks["hook_0"] = callback

        await query._handle_control_request({
            "type": "control_request",
            "request_id": "r2",
            "request": {
                "subtype": "hook_callback",
                "callback_id": "hook_0",
                "input": _input("pytest -q"),
                "tool_use_id": "t2",
            },
        })

        response = json.loads(transport.written[0])
        assert response["response"]["subtype"] == "success"
        assert response["response"]["response"] == {}


# ---------------------------------------------------------------------------
# Adversarial — a "open a PR" task goal that reaches a gh pr create Bash call
# ---------------------------------------------------------------------------

class TestAdversarialPrBypass:
    """Seed the issue #2280 attack: a task whose goal is "open a PR" leads the
    model to issue a ``gh pr create`` Bash command.  The structural guard must
    deny it (and every quoting/chaining variant) regardless of goal wording.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            # The exact shape observed in PR #2130's log.
            "gh pr create --title 'Promote branch' --head maestro/task/3ce652",
            "gh pr merge 2130 --merge",
            "GH_TOKEN=$TOKEN gh pr create --fill",
            "cd /repo && gh pr create",
            "git push origin maestro/task/3ce652:main",
            'bash -c "gh pr create --head maestro/task/3ce652"',
        ],
    )
    async def test_seeded_attack_command_denied(self, command: str) -> None:
        hook = make_task_pretooluse_hook(_BRANCH_SET)
        out = await hook(_input(command), "t1", None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


_HEREDOC_COMMIT = (
    "git commit -m \"$(cat <<'EOF'\n"
    "fix: gh pr create bypass\n\n"
    "gh pr create is gated, it's data\n"
    "EOF\n"
    ")\""
)

_SUBSTITUTION_DENY_COMMANDS = [
    'PR_URL="$(gh pr create --title x --body y)"',
    'echo "merged: $(gh pr merge 5 --squash)"',
    "echo `gh pr merge 5`",
    'echo "`gh pr merge 5`"',
    'X="$(git push origin main)"',
    'OUT="$(git push --force origin main 2>&1)"',
    "cat <(gh pr merge 5)",
    'echo "$(echo "$(gh pr merge 5)")"',
    'X="$(git push origin maestro/task/other)"',
    "echo $(( $(gh pr merge 5) + 1 ))",
    # r1: an unbalanced substitution must not skip earlier runnable segments.
    "git -C . push origin main\necho `x",
    "gh  pr merge 5\necho `x",
    # r2: bash expands substitutions inside an unquoted-delimiter heredoc.
    "git commit -m \"$(cat <<EOF\n$(gh pr merge 5)\nEOF\n)\"",
    "git commit -m \"$(cat <<EOF\nit's `gh pr merge 5`\nEOF\n)\"",
    "cat <<EOF\n\"$(gh pr merge 5)\"\nEOF",
    "git commit -m \"$(cat <<EOF\n$(gh pr merge 5\nEOF\n)\"",
    # r3: a case pattern's ``)`` would end the span early.
    'X="$(case a in a) gh pr merge 5;; esac)"',
]


class TestCommandSubstitutionDenied:
    """Issue #4098: posix ``shlex`` returns a double-quoted substitution as
    literal word text, so the gated command inside ``"$(…)"`` / backticks was
    never classified.  Every substitution body bash runs is now re-evaluated
    as a nested command; single-quoted text and heredoc bodies stay data."""

    @pytest.mark.parametrize("command", _SUBSTITUTION_DENY_COMMANDS)
    def test_substituted_gated_command_denied(self, command: str) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is not None

    @pytest.mark.parametrize(
        "command",
        [
            'git commit -m "push to main"',
            'echo "$(git status)"',
            "echo '$(gh pr merge 5)'",
            "echo '`gh pr merge 5`'",
            f'X="$(git push origin {_BRANCH})"',
            "echo $((1 + 2))",
            'echo "\\$(gh pr merge 5)"',
            _HEREDOC_COMMIT,
            "git commit -m \"$(cat <<EOF\ngh pr create\nEOF\n)\"",
            "git commit -m \"$(cat <<'EOF'\n$(gh pr merge 5)\nEOF\n)\"",
            "echo \"$(case a in a) echo hi;; esac)\"",
        ],
    )
    def test_data_and_owned_substitutions_allowed(self, command: str) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is None

    def test_unbalanced_substitution_fails_closed(self) -> None:
        assert evaluate_bash_command('X="$(gh pr merge 5"', _BRANCH_SET) is not None

    def test_unbalanced_benign_substitution_allowed(self) -> None:
        assert evaluate_bash_command("echo `date", _BRANCH_SET) is None

    def test_deep_nesting_fails_closed(self) -> None:
        command = "echo " + "$(" * 8 + "date" + ")" * 8
        assert evaluate_bash_command(command, _BRANCH_SET) is not None

    @pytest.mark.parametrize(
        "command", [*_SUBSTITUTION_DENY_COMMANDS, _HEREDOC_COMMIT],
    )
    def test_cli_paced_parity(self, command: str) -> None:
        reason = evaluate_bash_command(command, _BRANCH_SET)
        code, out = _run_gate(command, _BRANCH_ENV)
        assert code == 0
        if reason is None:
            assert json.loads(out) == {}
        else:
            decision = json.loads(out)["hookSpecificOutput"]
            assert decision["permissionDecisionReason"] == reason

    @pytest.mark.asyncio
    async def test_stripping_hook_denies_capture(self) -> None:
        hook = make_task_pretooluse_hook(
            _BRANCH_SET, strip_redirections=True, current_branch=_BRANCH,
        )
        out = await hook(
            _input('OUT="$(git push origin main 2>&1)"'), "t1", None,
        )
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestSubstitutionScanners:
    """Branch coverage for the raw-string substitution / heredoc scanners."""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("echo $(a (b) c)", ["a (b) c"]),
            ('echo "$(a \\) b)"', ["a \\) b"]),
            ("echo $(cat <<E\n)\nE\n)", ["cat <<E\n)\nE\n"]),
            ("cat <<E\n$(gh pr merge)\nE\necho $(x)", ["gh pr merge", "x"]),
            ("cat <<'E'\n$(gh pr merge)\nE\necho $(x)", ["x"]),
            ("cat <<E\n'$(a)'\nE", ["a"]),
            ("cat << 'E'\n$(a)\nE", []),
            ("echo \\$(x)", []),
            ("echo 'a' \"b\" > (x)", []),
            ("echo `a \\` b`", ["a \\` b"]),
            ("echo $(echo ')')", ["echo ')'"]),
            ('echo `a "b`', ['a "b']),
        ],
    )
    def test_substitution_bodies(self, command: str, expected: list[str]) -> None:
        assert _substitution_bodies(command) == expected

    def test_unbalanced_backtick_returns_none(self) -> None:
        assert _substitution_bodies("echo `a") is None

    def test_unbalanced_heredoc_substitution_returns_none(self) -> None:
        assert _substitution_bodies("cat <<E\n$(a\nE") is None
        assert _strip_heredoc_bodies("cat <<E\n$(a\nE") is None

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("cat <<<x", None),
            ("cat << ;", None),
            ("cat <<'E", None),
            ("cat <<E", None),
            ("cat <<-E\nbody\n\tE\nafter", (9, 17)),
            ('cat <<"E"\nbody', (10, 14)),
        ],
    )
    def test_heredoc_span(
        self, command: str, expected: tuple[int, int] | None,
    ) -> None:
        assert _heredoc_span(command, 4) == expected

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("cat <<E\ngh pr create\nE\ndone", ("cat <<E\ndone", [])),
            ("cat <<E\n$(x)\nE\ndone", ("cat <<E\ndone", ["x"])),
            ("cat <<-'E'\n$(x)\n\tE\ndone", ("cat <<-'E'\ndone", [])),
            ("echo '<<E'\nx\nE", ("echo '<<E'\nx\nE", [])),
            ('echo "\\"" <<<x', ('echo "\\"" <<<x', [])),
            # r3: an unquoted comment is copied verbatim; its apostrophe and
            # ``<<`` neither flip quote state nor open a heredoc.
            ("git push x # it's <<E\ny", ("git push x # it's <<E\ny", [])),
            ("cat <<'E'\nit's\nE\ndone", ("cat <<'E'\ndone", [])),
            ("cat <<E \\\nx\nb\nE", ("cat <<E \\\nx\n", [])),
        ],
    )
    def test_strip_heredoc_bodies(
        self, command: str, expected: tuple[str, list[str]],
    ) -> None:
        assert _strip_heredoc_bodies(command) == expected



_TOP_LEVEL_HEREDOC_BYPASS_COMMANDS = [
    "cat > notes.md <<'EOF'\nDon't forget the tests\nEOF\n"
    "git push origin main  # it's safe",
    "cat > notes.md <<'EOF'\nDon't forget the tests\nEOF\n"
    "gh pr merge 5 --squash  # it's green",
    "cat > notes.md <<EOF\nDon't forget the tests\nEOF\n"
    "git push origin main  # it's safe",
    "git commit -F - <<'EOF'\nDon't regress the gate\nEOF\n"
    "git push origin HEAD:main\n# Let's check the log\ngit log -1",
    "cat <<'A'\nit's\nA\ngit push origin main\ncat <<'B'\nit's\nB",
    "cat > n.md <<'EOF'\nDon't\nEOF\ngh pr create --title x --body y  # it's",
    "cat > n.md <<'EOF'\nDon't\nEOF\ngh pr ready 5  # it's",
    "cat > n.md <<'EOF'\nDon't\nEOF\n"
    "git push --force origin maestro/task/abc123  # it's",
    "cat > n.md <<'EOF'\nDon't\nEOF\ngit push origin maestro/task/other  # it's",
    'cat > n.md <<\'EOF\'\nsay "hi\nEOF\ngit push origin main  # the "real" one"',
    # Substitutions bash expands in an unquoted body are still gated.
    'cat <<EOF\n"$(gh pr merge 5)"\nEOF',
    # An unbalanced substitution in an unquoted body fails closed.
    "cat <<EOF\n$(x\nEOF\ngit push origin maestro/task/abc123",
    # r1: a heredoc fed to a shell's stdin is a script, not data.
    "bash <<'EOF'\ngit push origin main\nEOF",
    "sh -s <<EOF\ngh pr merge 5\nEOF",
    "cat <<'EOF' | bash\ngit push origin main\nEOF",
    # r2: a backslash-newline continues the header line into a command.
    "cat <<EOF \\\n&& git push origin main\nbody\nEOF",
]


class TestTopLevelHeredocGating:
    @pytest.mark.parametrize("strip_redirections", [False, True])
    @pytest.mark.parametrize("command", _TOP_LEVEL_HEREDOC_BYPASS_COMMANDS)
    def test_top_level_heredoc_quote_does_not_hide_gated_command(
        self, command: str, strip_redirections: bool,
    ) -> None:
        reason = evaluate_bash_command(
            command, _BRANCH_SET,
            strip_redirections=strip_redirections, current_branch=_BRANCH,
        )
        assert reason is not None

    def test_top_level_heredoc_contraction_then_owned_push_allowed(
        self,
    ) -> None:
        command = (
            "git commit -F - <<'EOF'\nDon't regress\nEOF\n"
            f"git push origin {_BRANCH}"
        )
        assert evaluate_bash_command(
            command, _BRANCH_SET, current_branch=_BRANCH,
        ) is None

    def test_gated_line_in_quoted_heredoc_body_is_data_allowed(
        self,
    ) -> None:
        # r3: the body is stdin data for ``cat``, never executed.
        assert evaluate_bash_command(
            "cat <<'EOF'\ngit push origin main\nEOF",
            _BRANCH_SET, current_branch=_BRANCH,
        ) is None

    def test_unbalanced_heredoc_substitution_ungated_allowed(self) -> None:
        assert evaluate_bash_command(
            "cat <<EOF\n$(x\nEOF", _BRANCH_SET, current_branch=_BRANCH,
        ) is None

# ---------------------------------------------------------------------------
# Cross-process gate replication (proposal 066) — TaskGateHook recognition
# and the `maestro _gate-bash-hook` subcommand seam.
# ---------------------------------------------------------------------------


async def _foreign_hook(
    input_data: object, tool_use_id: str | None, context: object,
) -> dict[str, object]:
    """A non-gate hook callback the recognizer must not mistake for the gate."""
    return {}


class TestTaskGateRecognition:
    def test_hook_carries_allowed_branch(self) -> None:
        hook = make_task_pretooluse_hook(_BRANCH_SET)
        assert isinstance(hook, TaskGateHook)
        assert hook.allowed_branch == _BRANCH

    def test_recognizes_task_gate_mapping(self) -> None:
        gate = task_gate_from_hooks(make_task_tool_hooks(_BRANCH_SET))
        assert gate is not None
        assert gate.allowed_branch == _BRANCH

    def test_recognizes_none_branch_gate(self) -> None:
        gate = task_gate_from_hooks(make_task_tool_hooks(None))
        assert gate is not None
        assert gate.allowed_branch is None

    def test_hook_carries_multi_branch_set(self) -> None:
        """The widened factory passes a genuine multi-branch owned set straight
        through — the old single-branch shim would have collapsed it (093)."""
        branches = frozenset({_BRANCH, "maestro/evolve/xyz"})
        hook = make_task_pretooluse_hook(branches)
        assert isinstance(hook, TaskGateHook)
        assert hook.allowed_branches == branches
        gate = task_gate_from_hooks(make_task_tool_hooks(branches))
        assert gate is not None
        assert gate.allowed_branches == branches

    @pytest.mark.parametrize(
        "hooks",
        [
            None,
            {},
            {"PostToolUse": []},
            # The gate plus a second event — containing the gate is not
            # *being* the gate; partial replication would silently drop the
            # extra hook.
            {**make_task_tool_hooks(_BRANCH_SET), "PostToolUse": []},
            {"PreToolUse": "not-a-list"},
            {"PreToolUse": []},
            {
                "PreToolUse": make_task_tool_hooks(_BRANCH_SET)["PreToolUse"] * 2,
            },
            # A plain dict where the HookMatcher should be.
            {"PreToolUse": [{"matcher": "Bash"}]},
            {
                "PreToolUse": [
                    HookMatcher(
                        matcher="Read",
                        hooks=[make_task_pretooluse_hook(_BRANCH_SET)],
                    ),
                ],
            },
            {"PreToolUse": [HookMatcher(matcher="Bash", hooks=None)]},
            {
                "PreToolUse": [
                    HookMatcher(
                        matcher="Bash",
                        hooks=[
                            make_task_pretooluse_hook(_BRANCH_SET),
                            make_task_pretooluse_hook(_BRANCH_SET),
                        ],
                    ),
                ],
            },
            {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[_foreign_hook])]},
        ],
    )
    def test_rejects_non_gate_mappings(self, hooks: dict[str, object] | None) -> None:
        assert task_gate_from_hooks(hooks) is None  # type: ignore[arg-type]


def _run_gate(
    command: str,
    env: dict[str, str],
    *,
    tool_name: str = "Bash",
) -> tuple[int, str]:
    """Drive one command through the gate-subcommand seam, no subprocess."""
    stdin = io.StringIO(json.dumps(_input(command, tool_name=tool_name)))
    stdout = io.StringIO()
    code = run_gate_bash_hook(stdin, stdout, env)
    return code, stdout.getvalue()


class TestGateBashHookSubcommand:
    """Step 1 acceptance: the subcommand's verdict matches
    ``evaluate_bash_command`` for the same inputs, table-driven over the
    shared ground-truth fixture tables."""

    @pytest.mark.parametrize("command", GATE_PARITY_DENY_COMMANDS)
    def test_deny_verdicts_match_evaluate(self, command: str) -> None:
        reason = evaluate_bash_command(command, _BRANCH_SET)
        assert reason is not None  # ground truth: the core denies this
        code, out = _run_gate(command, _BRANCH_ENV)
        assert code == 0
        decision = json.loads(out)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"] == reason

    def test_options_after_c_denied_by_gate_bash_hook(self) -> None:
        """Issue #4565: ``bash -c -- "<merge>"`` is denied on the cli_paced
        path too, with the same reason the in-process core gives."""
        command = 'bash -c -- "gh pr merge 5"'
        reason = evaluate_bash_command(command, _BRANCH_SET)
        assert reason is not None
        code, out = _run_gate(command, _BRANCH_ENV)
        assert code == 0
        decision = json.loads(out)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"] == reason

    @pytest.mark.parametrize("command", GATE_PARITY_ALLOW_COMMANDS)
    def test_allow_verdicts_match_evaluate(self, command: str) -> None:
        assert evaluate_bash_command(command, _BRANCH_SET) is None  # ground truth
        code, out = _run_gate(command, _BRANCH_ENV)
        assert code == 0
        assert json.loads(out) == {}

    @pytest.mark.parametrize(
        "command",
        [
            "gh pr create --title x",
            "git push origin maestro/task/abc123",
            "pytest -q",
        ],
    )
    def test_byte_for_byte_parity_with_in_process_hook(
        self, command: str,
    ) -> None:
        """The subcommand's stdout is byte-for-byte the JSON serialisation of
        the in-process hook's verdict for the same payload and branch."""
        payload = _input(command)
        hook = make_task_pretooluse_hook(_BRANCH_SET)
        expected = json.dumps(asyncio.run(hook(payload, None, None)))
        stdout = io.StringIO()
        code = run_gate_bash_hook(
            io.StringIO(json.dumps(payload)), stdout, _BRANCH_ENV,
        )
        assert code == 0
        assert stdout.getvalue() == expected

    def test_missing_branch_env_fails_closed(self) -> None:
        # No env var → branch None → even an own-branch push is denied.
        _, out = _run_gate("git push origin maestro/task/abc123", {})
        decision = json.loads(out)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"

    def test_empty_branch_env_fails_closed(self) -> None:
        _, out = _run_gate(
            "git push origin maestro/task/abc123", {GATE_BRANCH_ENV: ""},
        )
        decision = json.loads(out)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"

    def test_foreign_branch_env_denies_own_branch_push(self) -> None:
        # The env carries a *different* owned branch (validly encoded); a push
        # to our own branch is denied because it is not in that set.
        _, out = _run_gate(
            "git push origin maestro/task/abc123",
            allowed_branches_to_env(frozenset({"maestro/task/other"})),
        )
        decision = json.loads(out)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"

    def test_non_bash_tool_no_opinion(self) -> None:
        code, out = _run_gate(
            "gh pr create", _BRANCH_ENV, tool_name="Read",
        )
        assert code == 0
        assert json.loads(out) == {}

    def test_unparseable_stdin_denies_fail_closed(self) -> None:
        # A crashed hook would read to the claude CLI as a non-blocking error
        # (fail-open), so unparseable input must emit a deny and exit 0.
        stdout = io.StringIO()
        code = run_gate_bash_hook(
            io.StringIO("{not json"), stdout, _BRANCH_ENV,
        )
        assert code == 0
        decision = json.loads(stdout.getvalue())["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert "fail-closed" in decision["permissionDecisionReason"]

    def test_two_branch_set_parity_in_process_vs_subcommand(self) -> None:
        """proposal 093 acceptance: a genuine two-branch owned set round-trips
        byte-for-byte across the boundary.  The in-process
        ``TaskGateHook(frozenset({a, b}))`` and the subcommand seam reading the
        two-branch env return identical verdicts — both allow a push to either
        owned branch, both deny an out-of-set third branch, ``main``, and a
        force-push to an owned branch."""
        a, b = "maestro/task/aaa", "maestro/evolve/bbb"
        branches = frozenset({a, b})
        env = allowed_branches_to_env(branches)
        hook = TaskGateHook(branches)
        cases = (
            (f"git push origin {a}", False),             # owned → allow
            (f"git push origin {b}", False),             # other owned → allow
            ("git push origin maestro/task/ccc", True),  # out of set → deny
            ("git push origin main", True),              # protected → deny
            (f"git push --force origin {a}", True),      # force owned → deny
        )
        for command, should_deny in cases:
            payload = _input(command)
            in_process = asyncio.run(hook(payload, None, None))
            stdout = io.StringIO()
            code = run_gate_bash_hook(
                io.StringIO(json.dumps(payload)), stdout, env,
            )
            assert code == 0
            # Byte-for-byte parity between the two enforcement paths.
            assert stdout.getvalue() == json.dumps(in_process)
            denied = "hookSpecificOutput" in in_process
            assert denied == should_deny


class TestAllowedBranchesToEnv:
    """The env encoder mirrors the decoder: a falsy set produces no env, a
    non-empty set a deterministically-sorted JSON list (proposal 093)."""

    def test_none_and_empty_produce_no_env(self) -> None:
        assert allowed_branches_to_env(None) == {}
        assert allowed_branches_to_env(frozenset()) == {}

    def test_sorted_deterministic_encoding(self) -> None:
        # A frozenset has no stable order; sorting makes the env value stable.
        env = allowed_branches_to_env(frozenset({"b2", "b1"}))
        assert env == {GATE_BRANCH_ENV: '["b1", "b2"]'}

    def test_round_trips_through_decoder(self) -> None:
        branches = frozenset({"maestro/task/x", "maestro/evolve/y"})
        env = allowed_branches_to_env(branches)
        assert allowed_branches_from_env(env) == branches


class TestAllowedBranchesFromEnv:
    """The env decoder is fail-closed: only a non-empty JSON list of strings
    yields a set; every malformed shape decodes to ``None`` so the gate core
    denies every push (proposal 093)."""

    def test_valid_list_decodes_to_frozenset(self) -> None:
        env = {GATE_BRANCH_ENV: json.dumps(["b1", "b2"])}
        assert allowed_branches_from_env(env) == frozenset({"b1", "b2"})

    def test_missing_returns_none(self) -> None:
        assert allowed_branches_from_env({}) is None

    def test_empty_string_returns_none(self) -> None:
        assert allowed_branches_from_env({GATE_BRANCH_ENV: ""}) is None

    def test_empty_list_returns_none(self) -> None:
        assert allowed_branches_from_env({GATE_BRANCH_ENV: "[]"}) is None

    def test_non_list_json_returns_none(self) -> None:
        # Valid JSON but not a branch list — a bare string or an object.
        assert allowed_branches_from_env({GATE_BRANCH_ENV: '"b1"'}) is None
        assert allowed_branches_from_env({GATE_BRANCH_ENV: '{"b": 1}'}) is None

    def test_list_with_non_string_returns_none(self) -> None:
        assert allowed_branches_from_env({GATE_BRANCH_ENV: '["b1", 2]'}) is None

    def test_unparseable_returns_none(self) -> None:
        assert allowed_branches_from_env({GATE_BRANCH_ENV: "{not json"}) is None


class TestCurrentBranchResolution:
    """Proposal 099 step 7: an injected ``current_branch`` resolves a bare
    ``HEAD`` destination and a refspec-less push to that branch *before* the
    owned-set check, widening the gate's parsing without widening its policy.
    ``None`` (the default, and every non-activated caller) keeps both shapes
    fail-closed, byte-identical to before."""

    def test_bare_head_allowed_when_current_branch_owned(self) -> None:
        assert evaluate_bash_command(
            "git push origin HEAD", _BRANCH_SET, current_branch=_BRANCH,
        ) is None

    def test_refspec_less_allowed_when_current_branch_owned(self) -> None:
        assert evaluate_bash_command(
            "git push", _BRANCH_SET, current_branch=_BRANCH,
        ) is None
        assert evaluate_bash_command(
            "git push origin", _BRANCH_SET, current_branch=_BRANCH,
        ) is None

    def test_bare_head_denied_without_current_branch(self) -> None:
        # No branch injected → HEAD is unresolvable → fail-closed (today's
        # behaviour; the default is ``None``).
        assert evaluate_bash_command(
            "git push origin HEAD", _BRANCH_SET,
        ) is not None

    def test_refspec_less_denied_without_current_branch(self) -> None:
        assert evaluate_bash_command("git push", _BRANCH_SET) is not None

    def test_bare_head_denied_when_current_branch_protected(self) -> None:
        # A current branch of ``main`` resolves HEAD to a protected ref → deny,
        # even though it is the "current" branch.
        for protected in ("main", "master"):
            assert evaluate_bash_command(
                "git push origin HEAD", frozenset({protected}),
                current_branch=protected,
            ) is not None

    def test_bare_head_denied_when_current_branch_unowned(self) -> None:
        # HEAD resolves to a branch outside the owned set → deny.
        assert evaluate_bash_command(
            "git push origin HEAD", _BRANCH_SET,
            current_branch="maestro/task/other",
        ) is not None

    def test_refspec_less_denied_when_current_branch_unowned(self) -> None:
        assert evaluate_bash_command(
            "git push", _BRANCH_SET, current_branch="maestro/task/other",
        ) is not None

    def test_head_with_explicit_dest_ignores_current_branch(self) -> None:
        # ``HEAD:main`` already carries a destination (``main``); the current
        # branch never rescues it.
        assert evaluate_bash_command(
            "git push origin HEAD:main", _BRANCH_SET, current_branch=_BRANCH,
        ) is not None

    def test_force_bare_head_denied_despite_owned_current_branch(self) -> None:
        # Force detection runs before HEAD resolution → deny regardless.
        assert evaluate_bash_command(
            "git push --force origin HEAD", _BRANCH_SET, current_branch=_BRANCH,
        ) is not None
        assert evaluate_bash_command(
            "git push origin +HEAD", _BRANCH_SET, current_branch=_BRANCH,
        ) is not None
        assert evaluate_bash_command(
            "git push --force", _BRANCH_SET, current_branch=_BRANCH,
        ) is not None

    def test_current_branch_threads_through_bash_c_unwrap(self) -> None:
        # The branch must ride the ``bash -c`` unwrap so a wrapped bare-HEAD
        # push resolves identically to an unwrapped one.
        assert evaluate_bash_command(
            'bash -c "git push origin HEAD"', _BRANCH_SET,
            current_branch=_BRANCH,
        ) is None
        assert evaluate_bash_command(
            'bash -c "git push origin HEAD"', _BRANCH_SET,
            current_branch="main",
        ) is not None

    def test_hook_denies_wrapper_behind_launcher(self) -> None:
        # #4147: the hook fails closed on ``timeout … bash -c "gh pr merge"``.
        hook = TaskGateHook(_BRANCH_SET, current_branch=_BRANCH)
        denied = asyncio.run(
            hook(
                {"tool_name": "Bash",
                 "tool_input": {
                     "command": 'timeout 300 bash -c "gh pr merge 5"'}},
                None, None,
            ),
        )
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_hook_carries_current_branch(self) -> None:
        # The hook forwards its ``current_branch`` into the decision core.
        hook = TaskGateHook(_BRANCH_SET, current_branch=_BRANCH)
        allowed = asyncio.run(
            hook(
                {"tool_name": "Bash",
                 "tool_input": {"command": "git push origin HEAD"}},
                None, None,
            ),
        )
        assert allowed == {}
        denied = asyncio.run(
            TaskGateHook(_BRANCH_SET, current_branch="main")(
                {"tool_name": "Bash",
                 "tool_input": {"command": "git push origin HEAD"}},
                None, None,
            ),
        )
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_make_task_tool_hooks_forwards_current_branch(self) -> None:
        # The factory shim threads ``current_branch`` onto the built hook.
        hooks = make_task_tool_hooks(_BRANCH_SET, current_branch=_BRANCH)
        gate = task_gate_from_hooks(hooks)
        assert gate is not None
        assert gate.current_branch == _BRANCH


_EVAL_STDIN_SHELL_DENIED = [
    'eval "gh pr merge 5 --squash"',
    'eval "git push origin main"',
    "eval 'gh pr create --fill'",
    'command eval "gh pr merge 5"',
    'echo "gh pr create --fill" | sh',
    'printf "git push origin main" | bash',
    'echo "gh pr merge 5" | /bin/sh -s',
    'echo "gh pr merge 5" | bash -s foo',
    'echo "gh pr merge 5" | bash -o pipefail',
    'echo "gh pr merge 5" | bash -eo pipefail',
    'echo "gh pr merge 5" | bash -oo pipefail errexit',
    'echo "gh pr merge 5" | bash -c "sh -s"',
    'echo "gh pr merge 5" | bash -c "true; bash"',
    'echo "gh pr merge 5" | bash -c "sh \'unclosed"',
    'echo "gh pr merge 5" | bash +x',
    'echo "gh pr merge 5" | bash /dev/stdin',
    'echo "gh pr merge 5" | bash -',
    'echo "gh pr merge 5" | bash --',
    'echo "gh pr merge 5" | bash --norc',
    'echo "gh pr merge 5" | sudo bash',
    'echo "gh pr merge 5" | timeout 5 sh',
    'echo "gh pr merge 5" | command sh',
    'echo "gh  pr merge 5" | sh',
    'command -p eval "gh pr merge 5"',
    'command builtin eval "gh pr merge 5"',
    'builtin command eval "git push origin main"',
]

_EVAL_STDIN_SHELL_ALLOWED = [
    'eval "git status"',
    f'eval "git push origin {_BRANCH}"',
    "cat script.sh | bash",
    "bash script.sh",
    'rg -t sh "git push"',
    'grep eval "git push"',
    'echo "gh pr view 5"; bash script.sh',
    'echo "gh pr view 5"; bash -o pipefail script.sh',
    'echo "gh pr view 5"; bash -- script.sh',
    'echo "gh pr view 5"; bash -x -c "ls"',
    'echo "gh pr view 5" | bash -c "cat"',
]


class TestEvalAndStdinShellGating:
    def test_stdin_shell_recursion_stops_at_unwrap_depth(self) -> None:
        tokens = ["bash", "-c", "sh -s"]
        assert tool_gating._is_stdin_shell(tokens, False) is True
        assert tool_gating._is_stdin_shell(
            tokens, False, tool_gating._MAX_UNWRAP_DEPTH,
        ) is False

    @pytest.mark.parametrize("strip_redirections", [False, True])
    @pytest.mark.parametrize("command", _EVAL_STDIN_SHELL_DENIED)
    def test_eval_and_stdin_shell_denied(
        self, command: str, strip_redirections: bool,
    ) -> None:
        reason = evaluate_bash_command(
            command, _BRANCH_SET,
            strip_redirections=strip_redirections, current_branch=_BRANCH,
        )
        assert reason is not None

    @pytest.mark.parametrize("strip_redirections", [False, True])
    @pytest.mark.parametrize("command", _EVAL_STDIN_SHELL_ALLOWED)
    def test_eval_and_stdin_shell_allowed(
        self, command: str, strip_redirections: bool,
    ) -> None:
        reason = evaluate_bash_command(
            command, _BRANCH_SET,
            strip_redirections=strip_redirections, current_branch=_BRANCH,
        )
        assert reason is None

    def test_empty_segment_is_not_stdin_shell(self) -> None:
        assert tool_gating._is_stdin_shell([], False) is False

    def test_builtin_eval_denied(self) -> None:
        assert evaluate_bash_command(
            'builtin eval "git push origin main"',
            _BRANCH_SET, current_branch=_BRANCH,
        ) is not None

    def test_command_without_eval_allowed(self) -> None:
        assert evaluate_bash_command(
            "command -v gh", _BRANCH_SET, current_branch=_BRANCH,
        ) is None

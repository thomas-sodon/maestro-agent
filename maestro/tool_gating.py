"""Action-level tool gating for the task-execution path.

The human-in-the-loop evolution invariant ("All code changes require approval
via dashboard or Telegram") is a *path-level* construct: PRs may only be opened
by ``EvolutionEngine.create_pr()`` after a plan is approved.  The task executor,
however, runs with an unrestricted ``Bash`` tool under ``bypassPermissions`` and
could open or merge a PR — or push to ``main`` — directly, bypassing that gate
(issue #2280; the observed instance was PR #2130, opened by a ``gh pr create``
Bash call inside ``run_task``).

This module supplies a ``PreToolUse`` **hook** that the executor wires onto its
SDK sessions.  It inspects ``Bash`` commands and **denies** PR-mutating
operations (``gh pr create|merge|ready``, mutating ``gh api …/pulls`` calls) and
pushes to protected or non-own-branch refs, while allowing commits, pushes to
the run's own task branch, and all read/edit tooling.  ``gh pr comment`` is
explicitly allowed: posting a PR comment cannot open, merge, ready, or push a
PR, and the PR-remediation flow needs it to upsert the overflow-finding roll-up
acknowledgement comment (proposal 064).  For the same reason a ``gh api`` call
that only *replies to* a review comment (the ``…/pulls/<n>/comments/<id>/replies``
endpoint, or a ``…/pulls/<n>/comments`` POST carrying ``in_reply_to``) is allowed
even though it is a mutating ``…/pulls`` call: the PR-remediation flow mandates
a *threaded* reply to answer each inline review thread (#2819), and a reply can
no more open/merge/ready/push a PR than ``gh pr comment`` can.  ``gh pr close``
is likewise allowed as a **reversible withdrawal**: closing a PR neither merges
it, marks it ready, opens a new one, nor pushes a ref — it retracts an
own-authored change and can be undone by ``gh pr reopen``.  Naming it a
first-class permitted disposal (proposal: owned-disposal lever) makes the
"won't vs can't" question a tested fact — :func:`permits_pr_close` proves the
lever is reachable — so a stuck zero-invocations record is a choice, not a gate
block; ``merge``/``ready``/``open``/``reopen`` stay denied.  The deny *reason*
is the "fail loud"
surface: it names why and redirects the model to the approval route (nominate
the branch via ``EvolutionPlan.source_task_id``).

A push to the run's *own* branch is intentionally permitted: it does not bypass
the approval gate, because that branch still needs a PR and human approval to
reach ``main``.  This is deliberately looser than the operational convention
that the LLM commits but does not push (run() and Tend PR deliver); the guard
enforces the #2280 *approval-bypass* invariant, not that convention.

A ``PreToolUse`` hook is used rather than a ``can_use_tool`` permission callback
because the executor runs under ``permission_mode="bypassPermissions"``, and the
CLI short-circuits the permission flow to "allow" *before* it would consult a
``can_use_tool`` callback — so that callback never fires.  Hooks are a separate
mechanism that runs regardless of permission mode, and (unlike ``can_use_tool``)
impose no streaming-mode requirement on the prompt, so they also work on the
stateless ``query()`` step path.  Verified empirically: a ``PreToolUse`` deny
hook blocks a Bash command under ``bypassPermissions``.

The guard is *fail-closed*: a ``gh pr``- or ``git push``-shaped command that
cannot be parsed cleanly is denied rather than allowed, and a mutation hidden
behind a ``bash -c``/``sh -c`` wrapper — or smuggled as the child of the
``maestro gate-lease`` exec wrapper (proposal 126), or behind ``eval "…"``
— is unwrapped and re-evaluated; a script piped into an argument-less shell
(``echo "…" | sh``) fails closed.  Backslash-newline line continuations are joined as bash joins
them before lexing, and a ``gh api`` endpoint is located by skipping its
value-taking flags with their values, so flags placed before the endpoint
cannot hide a pulls mutation; an ambiguous ``pulls`` endpoint fails closed.
It is
intentionally narrow — it gates the PR-bypass behavior of issue #2280, not
general shell sandboxing (that overlaps proposals 024/025, which can reuse the
``QueryOptions.hooks`` passthrough this module relies on).  It does not attempt
to defeat deliberate evasion (``gh`` invoked by absolute path or a GraphQL
mutation); the threat model is a redirected, non-adversarial model,
backed for defense in depth by the ``generate_task`` goal filter and the
PR-funnel regression test.  Within that model the parser follows the shapes a
model naturally writes: a newline ends a command like ``;`` does, a
compound-command reserved word (``if``/``then``/``do``/``{``/``!``/``time`` …)
is stripped before dispatch, and any gated program the leading dispatch
recognises (``gh``/``git``, a shell ``-c`` wrapper, ``maestro gate-lease``, or
``python -m maestro.cli gate-lease``) at a non-leading position of an otherwise
unrecognised segment (``xargs git push …``, ``timeout 300 bash -c "gh pr
merge …"``) is re-classified from that point, failing closed.
Like ``bash -c`` and ``gate-lease``, a command substitution (``$(…)``,
backticks, ``<(…)``/``>(…)``) outside single quotes is unwrapped and its body
evaluated as a nested command, even inside a double-quoted capture.

Outward reach: the same gate refuses the loop's outward acts that need
Thomas's identity, since publishing or granting access under his name is his
act alone — ``gh repo`` / ``gh release`` / ``gh secret``
verbs other than reads, mutating ``gh api`` calls against repository, release,
secret (repo, org and user), transfer, fork, git-ref/tag-object, contents,
merge, merge-upstream, branch-protection, deploy-key, collaborator, webhook or
repo-creation endpoints (path
or full ``https://<host>/[api/v3/]`` URL form), and tag pushes (``--tags`` /
``--follow-tags``).  Still outside the threat model: GraphQL mutations,
absolute-path ``gh``/``git`` binaries, the read-like ``gh repo`` verbs outside
the read set (``gitignore`` / ``license`` / ``set-default``, denied
fail-closed), ``pnpm dlx`` / ``yarn dlx`` / ``bunx``, and
``git -c push.followTags=true push``.
"""

from __future__ import annotations

import logging
import re
import shlex
from contextvars import ContextVar
from typing import Any

from claude_agent_sdk import HookJSONOutput, HookMatcher

from maestro.tool_surface import gate_hook_matcher, is_gate_matched

logger = logging.getLogger("maestro")

#: Refs the task executor may never push to, regardless of the run's own
#: branch.  ``main``/``master`` are the protected integration branches.
_PROTECTED_REFS: frozenset[str] = frozenset({"main", "master"})

#: Read-only ``gh pr`` subcommands.  Any other (or missing) ``gh pr`` verb —
#: ``create``/``merge``/``ready`` plus any future mutating verb — is denied
#: fail-closed unless it is a benign verb below.
_GH_PR_READ_VERBS: frozenset[str] = frozenset(
    {"list", "view", "status", "checks", "diff"},
)

#: Mutating ``gh pr`` verbs that do *not* bypass the approval gate.  Posting a
#: comment cannot open, merge, ready, or push a PR, and the PR-remediation flow
#: needs ``gh pr comment`` to *create* the overflow-finding roll-up
#: acknowledgement comment when none exists yet (proposal 064) — the update path
#: (``gh api --method PATCH …/issues/comments/…``) is already allowed because it
#: targets an ``issues`` endpoint, not ``pulls``.  Kept separate from the read
#: verbs so the fail-closed default still denies ``create``/``merge``/``ready``
#: and any unknown verb.
_GH_PR_BENIGN_VERBS: frozenset[str] = frozenset({"comment"})

#: Mutating ``gh pr`` verbs that *dispose* of a PR reversibly without bypassing
#: the approval gate.  ``gh pr close`` retracts an own-authored, git-unlanded
#: change: it neither merges, marks ready, opens, nor pushes a ref, and is
#: undoable via ``gh pr reopen``.  It is named as a first-class permitted
#: terminal act (proposal: owned-disposal lever) so the "won't vs can't"
#: question of a never-invoked disposal is a tested fact (:func:`permits_pr_close`),
#: not a guess.  ``reopen`` is intentionally NOT here — it is the inverse and
#: stays fail-closed with ``merge``/``ready``/``open``.
_GH_PR_DISPOSAL_VERBS: frozenset[str] = frozenset({"close"})

#: Read-only ``gh repo`` / ``gh release`` / ``gh secret`` verbs.  Any other (or
#: missing) verb of those subcommands is an outward act — publishing,
#: re-scoping or deleting a repository, cutting a release, or writing a secret —
#: and is denied fail-closed with :data:`_OUTWARD_DENY`.
_GH_REPO_READ_VERBS: frozenset[str] = frozenset({"view", "list", "clone"})
_GH_RELEASE_READ_VERBS: frozenset[str] = frozenset({"list", "view", "download"})
_GH_SECRET_READ_VERBS: frozenset[str] = frozenset({"list"})

#: ``gh api`` endpoints whose mutation is an outward act: the repository
#: resource itself, its releases, its secrets (actions / dependabot /
#: codespaces / environment), transfer, forking and template generation, any
#: git ref write (tag *and* branch refs — the API form of a push, so a
#: ``PATCH …/git/refs/heads/main`` cannot route around the push gate) and
#: annotated-tag creation, the other API routes that write a branch without a
#: push (``contents`` commits, ``merges``, ``merge-upstream``, branch
#: ``protection``), deploy ``keys``, ``collaborators`` and ``hooks``, org and user
#: secrets, and repository creation under the user or an org.  A ``https://<host>/`` URL prefix, an
#: ``api/v3/`` (GHES) prefix, a leading ``/`` and any ``?query`` are stripped
#: before matching.
_OUTWARD_API_PATH_RE = re.compile(
    r"^(?:"
    r"repos/[^/]+/[^/]+"
    r"|repos/[^/]+/[^/]+/releases(?:/.*)?"
    r"|repos/[^/]+/[^/]+/(?:actions|dependabot|codespaces)/secrets(?:/.*)?"
    r"|repos/[^/]+/[^/]+/environments/[^/]+/secrets(?:/.*)?"
    r"|repos/[^/]+/[^/]+/(?:transfer|generate|forks)"
    r"|repos/[^/]+/[^/]+/git/(?:refs|tags)(?:/.*)?"
    r"|repos/[^/]+/[^/]+/(?:contents|merges|merge-upstream)(?:/.*)?"
    r"|repos/[^/]+/[^/]+/(?:keys|collaborators|hooks)(?:/.*)?"
    r"|repos/[^/]+/[^/]+/branches/.+/protection(?:/.*)?"
    r"|orgs/[^/]+/(?:actions|dependabot|codespaces)/secrets(?:/.*)?"
    r"|user/codespaces/secrets(?:/.*)?"
    r"|user/repos"
    r"|orgs/[^/]+/repos"
    r")/?$",
)

#: A full-URL ``gh api`` endpoint prefix (scheme, host, optional GHES
#: ``api/v3``), stripped so the URL form matches like the path form.
_API_URL_PREFIX_RE = re.compile(r"^(?:https?://[^/]+)?/*(?:api/v3/)?", re.IGNORECASE)

#: ``gh api`` method flags whose value implies a mutation.
_MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PATCH", "PUT", "DELETE"})

#: ``gh api`` field flags.  Their mere presence makes ``gh api`` send POST.
_GH_API_FIELD_FLAGS: frozenset[str] = frozenset(
    {"-f", "-F", "--field", "--raw-field"},
)

#: ``gh api`` flags that carry a request body — the field flags plus
#: ``--input <file|->``.  Any body makes ``gh api`` send POST (#4097).
_GH_API_BODY_FLAGS: frozenset[str] = _GH_API_FIELD_FLAGS | frozenset(
    {"--input"},
)

#: The single-dash members of :data:`_GH_API_FIELD_FLAGS`.  pflag also accepts
#: these with the value attached (``-fhead=x`` ≡ ``-f head=x``, #4383).
_GH_API_SHORT_FIELD_FLAGS: frozenset[str] = frozenset(
    flag for flag in _GH_API_FIELD_FLAGS if not flag.startswith("--")
)

#: ``gh api`` flags that take a *separate* value token.  Their value must be
#: consumed when locating the endpoint so it is not mistaken for the path —
#: the ``PUT`` in ``gh api -X PUT repos/o/r/pulls/5/merge`` is not the endpoint.
_GH_API_VALUE_FLAGS: frozenset[str] = _GH_API_BODY_FLAGS | frozenset(
    {
        "-X", "--method", "-H", "--header", "-q", "--jq", "-t", "--template",
        "-p", "--preview", "--cache", "--hostname",
    },
)

#: ``env`` flags that consume the following token as a value (so it is not
#: mistaken for the wrapped program).  ``env -u NAME``/``env --unset NAME``.
_ENV_VALUE_FLAGS: frozenset[str] = frozenset({"-u", "--unset"})

#: ``git`` global flags (before the subcommand) that take a *separate* value
#: token.  Their value must be consumed so it is not mistaken for the
#: subcommand — e.g. the ``/repo`` in ``git -C /repo push`` is not ``push``.
#: The ``--flag=value`` form is a single token and is skipped without a
#: separate consume.  Deliberate flag-stuffing with obscure value-flags is out
#: of scope (see the module-level threat-model note).
_GIT_VALUE_FLAGS: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"},
)

#: ``git push`` flags that rewrite a branch's published history.  Any of these
#: — or a ``+``-prefixed refspec — turns a push into a force-push, denied
#: unconditionally (even to an otherwise-owned branch) so a published
#: evolution-PR branch stays append-only: a human reviews the diff a branch
#: already published, never a silently-rewritten one.  The
#: ``--force-with-lease=<refname>[:<expect>]`` value form is a single token, so
#: it is matched by prefix in :func:`_is_force_push`, not by set membership.
_FORCE_PUSH_FLAGS: frozenset[str] = frozenset(
    {"-f", "--force", "--force-with-lease", "--force-if-includes"},
)

#: ``git push`` flags that push *every* local branch to the remote irrespective
#: of the refspec or current branch: ``--all`` pushes all ``refs/heads/*`` and
#: ``--mirror`` mirrors every ref (force-updating and deleting remote refs).
#: Either flag defeats the protected-branch guard by pushing ``main``/``master``
#: too, so the resolved refspec is *not* the only destination that reaches the
#: remote.  A push carrying one is denied fail-closed — it can never be confirmed
#: to target only owned branches — the same way a protected-ref refspec is.
#: ``--tags`` / ``--follow-tags`` join them: a tag push publishes refs beyond the
#: owned branch (a release tag is an outward act), so it is refused the same way.
_ALL_REFS_PUSH_FLAGS: frozenset[str] = frozenset(
    {"--all", "--mirror", "--tags", "--follow-tags"},
)

#: ``gh`` global flags (before the subcommand) that take a *separate* value
#: token, e.g. ``gh --repo o/r pr create`` — ``o/r`` is the value of
#: ``--repo``, not the subcommand.
_GH_VALUE_FLAGS: frozenset[str] = frozenset(
    {"-R", "--repo", "--hostname"},
)

#: Shell control operators used to split a command line into sub-commands.
_SHELL_OPERATORS: frozenset[str] = frozenset(
    {"&&", "||", ";", "|", "&", "(", ")"},
)

#: Characters :func:`_split_commands` lexes as punctuation.  Newline is among
#: them because it terminates a command in a Bash list exactly like ``;``.
_PUNCTUATION_CHARS = "();<>|&\n"

#: Compound-command reserved words (and the ``!`` / ``time`` pipeline
#: prefixes) that may lead a segment ahead of the real program.  They are
#: stripped before dispatch so ``if true; then gh pr merge 5; fi`` classifies
#: the ``gh`` invocation rather than the keyword.
_SHELL_RESERVED_PREFIXES: frozenset[str] = frozenset(
    {"if", "then", "else", "elif", "while", "until", "do", "{", "!", "time"},
)

#: Shell interpreters that take a ``-c <command-string>`` argument.  A PR/push
#: mutation hidden inside ``bash -c "gh pr create"`` must be unwrapped and
#: re-evaluated rather than waved through (the wrapper program is not
#: ``gh``/``git``).
_SHELL_WRAPPERS: frozenset[str] = frozenset({"bash", "sh", "dash", "zsh"})

#: Module targets that resolve to the maestro CLI under ``python -m``.  The
#: hand-run gate docs invoke ``gate-lease`` as ``python3.14 -m maestro.cli
#: gate-lease -- <cmd>`` so it self-resolves to the checkout under test (a bare
#: ``maestro`` resolves off ``PATH`` to a sibling checkout's editable install);
#: ``maestro`` is accepted alongside ``maestro.cli`` so the unwrap tracks either
#: spelling.  Recognising this module form is what lets :func:`_maestro_deny`
#: unwrap a ``gate-lease`` child smuggled behind the interpreter (r1).
_MAESTRO_CLI_MODULES: frozenset[str] = frozenset({"maestro", "maestro.cli"})

#: Bound on shell-wrapper unwrapping recursion; exceeding it fails closed so a
#: pathologically nested ``bash -c "bash -c …"`` can never blow the stack.
_MAX_UNWRAP_DEPTH = 5

_REDIRECT = (
    "To land a task branch on main, nominate it via "
    "EvolutionPlan.source_task_id so the change goes through the "
    "approval-gated evolution path; the task executor must not open, merge, "
    "or push PRs directly."
)
_PR_DENY = (
    "Blocked: opening or merging a pull request from the task-execution path "
    "bypasses the human-in-the-loop approval gate. " + _REDIRECT
)
_PUSH_DENY = (
    "Blocked: pushing to main or a branch other than this run's own task "
    "branch bypasses the human-in-the-loop approval gate. " + _REDIRECT
)
_PARSE_DENY = (
    "Blocked (fail-closed): a gated (PR, push or outward-reach) command could "
    "not be parsed and is denied for safety. Branch work belongs on this run's "
    "own task branch; outward acts go to Thomas as a message. " + _REDIRECT
)
_FORCE_PUSH_DENY = (
    "Blocked: force-pushing rewrites a branch's published history, which a "
    "human reviews as an append-only PR; the task executor may only "
    "fast-forward its own branch, never force-push it. " + _REDIRECT
)
_OUTWARD_DENY = (
    "Blocked: publishing, re-scoping or deleting a repository, cutting a "
    "release, writing a secret, writing a branch or git ref through the API, "
    "or granting access (deploy keys, collaborators, webhooks) is an "
    "outward act that needs Thomas's "
    "identity; the loop may only read these surfaces. Hand the act to Thomas "
    "as a message instead."
)
_PUSH_REMOTE_FOREIGN_DENY = (
    "Blocked (push-remote-foreign): this run may push only to its own repo's "
    "remote (origin); a push to another remote or URL, a --repo override, a "
    "remote/url config override, or rewriting a remote is refused. "
    + _REDIRECT
)
_ALL_REFS_PUSH_DENY = (
    "Blocked: git push --all / --mirror push every local branch (including "
    "main/master) to the remote, and --tags / --follow-tags publish tags "
    "beyond it, so the push cannot be confirmed to target only this run's "
    "own task branch. " + _REDIRECT
)


def _join_line_continuations(command: str) -> str:
    """Remove backslash-newline line continuations outside single quotes.

    Bash joins ``git push \\<newline> origin main`` into one command; doing
    the same before lexing keeps the newline separator of
    :func:`_split_commands` from splitting it into a refspec-less ``git push``
    (which a set ``current_branch`` would resolve to the owned branch) plus a
    stray ``origin main`` segment.  Inside single quotes a backslash is
    literal, so the pair is kept there; an apostrophe inside double quotes
    is literal too, so it does not open a single-quoted span.
    """
    out: list[str] = []
    in_single = False
    in_double = False
    index = 0
    while index < len(command):
        char = command[index]
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "\\" and not in_single:
            if command.startswith("\n", index + 1):
                index += 2
                continue
            out.append(command[index:index + 2])
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _is_separator(token: str) -> bool:
    """Whether *token* ends a sub-command.

    ``shlex`` merges adjacent punctuation, so a newline following an operator
    comes back as one token (``";\n"``, ``"&&\n"``); any punctuation-only
    token carrying a newline therefore separates, alongside the bare
    :data:`_SHELL_OPERATORS`.
    """
    if token in _SHELL_OPERATORS:
        return True
    return "\n" in token and all(ch in _PUNCTUATION_CHARS for ch in token)


def _strip_comments(command: str) -> str:
    """Drop unquoted ``#`` comments up to (not including) their newline.

    Bash starts a comment only at a word boundary, outside quotes.  Removing
    it before lexing keeps the newline as a separator for
    :func:`_split_commands` and stops a stray quote inside a comment
    (``git push origin <owned> # it's mine``) from unbalancing the lexer.
    """
    out: list[str] = []
    in_single = False
    in_double = False
    index = 0
    while index < len(command):
        char = command[index]
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "\\" and not in_single:
            out.append(command[index:index + 2])
            index += 2
            continue
        elif (
            char == "#" and not in_single and not in_double
            and (not out or out[-1][-1] in " \t\r" + _PUNCTUATION_CHARS)
        ):
            end = command.find("\n", index)
            index = len(command) if end == -1 else end
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _heredoc_span(command: str, index: int) -> tuple[int, int] | None:
    """Locate the heredoc introduced by the ``<<`` at *index*.

    Returns ``(body_start, body_end)`` — the offset just past the header
    line's newline and the offset just past the closing delimiter line (or
    the end of *command* when no delimiter line follows) — or ``None`` when
    the ``<<`` is not a heredoc header (``<<<`` here-string, no delimiter
    word, or no body line).
    """
    pos = index + 2
    if command.startswith("<", pos):
        return None
    strip_tabs = command.startswith("-", pos)
    if strip_tabs:
        pos += 1
    while pos < len(command) and command[pos] in " \t":
        pos += 1
    if pos < len(command) and command[pos] in "'\"":
        close = command.find(command[pos], pos + 1)
        if close == -1:
            return None
        delim = command[pos + 1:close]
    else:
        end = pos
        while end < len(command) and (command[end].isalnum() or command[end] == "_"):
            end += 1
        delim = command[pos:end]
    newline = command.find("\n", pos)
    # A backslash-newline joins the header's next line into it (bash reads
    # the continuation before the body), so the body starts after the first
    # unescaped newline.
    while newline > 0 and command[newline - 1] == "\\":
        newline = command.find("\n", newline + 1)
    if not delim or newline == -1:
        return None
    body_start = newline + 1
    line_start = body_start
    while line_start < len(command):
        line_end = command.find("\n", line_start)
        stop = len(command) if line_end == -1 else line_end
        line = command[line_start:stop]
        if (line.lstrip("\t") if strip_tabs else line) == delim:
            return body_start, min(stop + 1, len(command))
        line_start = stop + 1
    return body_start, len(command)


def _heredoc_quoted(command: str, index: int) -> bool:
    """Whether the heredoc delimiter after the ``<<`` at *index* is quoted.

    Bash expands ``$(…)`` and backticks inside an unquoted-delimiter heredoc
    body, so only a quoted delimiter makes the body pure data.
    """
    pos = index + 2
    if command.startswith("-", pos):
        pos += 1
    while pos < len(command) and command[pos] in " \t":
        pos += 1
    return pos < len(command) and command[pos] in "'\""


def _heredoc_substitutions(
    command: str, index: int, span: tuple[int, int],
) -> list[str] | None:
    """Substitution bodies bash runs inside the heredoc at *index*.

    Empty for a quoted delimiter; otherwise the body is scanned with quotes
    treated as literal text, as bash does inside a heredoc body.
    """
    if _heredoc_quoted(command, index):
        return []
    return _substitution_bodies(command[span[0]:span[1]], quote_blind=True)


def _strip_heredoc_bodies(command: str) -> tuple[str, list[str]] | None:
    """Drop every heredoc body in *command*, keeping its header line.

    A heredoc body is data fed to the command's stdin, so removing it before
    any quote-tracking pass — of the top-level command and of each
    re-evaluated substitution body — keeps a quote inside the body (``Don't
    …``) from regrouping the real commands after the delimiter line.  An
    unquoted ``#`` comment is copied through verbatim without quote tracking,
    so an apostrophe or ``<<`` inside it neither flips quote state nor reads
    as a heredoc header.  Returns the stripped text plus the substitution
    bodies bash still runs inside the dropped unquoted-delimiter bodies, or
    ``None`` when one of those is unbalanced.
    """
    out: list[str] = []
    expanded: list[str] = []
    in_single = False
    in_double = False
    index = 0
    while index < len(command):
        char = command[index]
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "\\" and not in_single:
            out.append(command[index:index + 2])
            index += 2
            continue
        elif (
            char == "#" and not in_single and not in_double
            and (not out or out[-1][-1] in " \t\r" + _PUNCTUATION_CHARS)
        ):
            end = command.find("\n", index)
            stop = len(command) if end == -1 else end
            out.append(command[index:stop])
            index = stop
            continue
        elif command.startswith("<<", index) and not in_single and not in_double:
            span = _heredoc_span(command, index)
            if span is not None:
                inner = _heredoc_substitutions(command, index, span)
                if inner is None:
                    return None
                expanded.extend(inner)
                out.append(command[index:span[0]])
                index = span[1]
                continue
        out.append(char)
        index += 1
    return "".join(out), expanded


def _match_close(command: str, index: int, closer: str) -> int:
    """Index of the *closer* ending the span whose body starts at *index*.

    For ``)`` nested parentheses are counted; quotes, backslash escapes and
    heredoc bodies inside the span are skipped.  Returns ``-1`` when the span
    is unbalanced.
    """
    depth = 0
    in_single = False
    in_double = False
    while index < len(command):
        char = command[index]
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "\\" and not in_single:
            index += 2
            continue
        elif not in_single and not in_double:
            if char == closer and depth == 0:
                return index
            if closer == ")" and char == "(":
                depth += 1
            elif closer == ")" and char == ")":
                depth -= 1
            elif command.startswith("<<", index):
                span = _heredoc_span(command, index)
                if span is not None:
                    index = span[1]
                    continue
        elif in_double and char == closer and closer == "`":
            return index
        index += 1
    return -1


def _substitution_bodies(
    command: str, *, quote_blind: bool = False,
) -> list[str] | None:
    """Bodies of every command substitution in *command* that bash runs.

    Collects ``$(…)`` and backtick spans outside single quotes, plus
    ``<(…)``/``>(…)`` process substitutions outside any quotes, scanning the
    raw string because posix ``shlex`` returns a double-quoted substitution
    as literal word text.  ``$((…))`` arithmetic is not a substitution and is
    skipped (a substitution nested inside it is still found).  A heredoc
    body with a quoted delimiter is data and is skipped; an unquoted one is
    scanned with *quote_blind* set, since bash expands substitutions there
    but treats quotes as literal text.  Only the outermost bodies are
    returned; nested ones are reached when the caller re-evaluates each
    body.  Returns ``None`` when a delimiter is unbalanced.
    """
    bodies: list[str] = []
    in_single = False
    in_double = False
    index = 0
    while index < len(command):
        char = command[index]
        if quote_blind and char in "'\"":
            pass
        elif char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "\\" and not in_single:
            index += 2
            continue
        elif not in_single:
            opener = command[index:index + 2]
            if command.startswith("$((", index):
                index += 3
                continue
            if opener == "$(" or (opener in ("<(", ">(") and not in_double):
                close = _match_close(command, index + 2, ")")
            elif char == "`":
                close = _match_close(command, index + 1, "`")
                opener = "`"
            else:
                close = None
                if (
                    not in_double and not quote_blind
                    and command.startswith("<<", index)
                ):
                    span = _heredoc_span(command, index)
                    if span is not None:
                        inner = _heredoc_substitutions(command, index, span)
                        if inner is None:
                            return None
                        bodies.extend(inner)
                        index = span[1]
                        continue
            if close is not None:
                if close == -1:
                    return None
                bodies.append(command[index + len(opener):close])
                index = close + 1
                continue
        index += 1
    return bodies


def _split_commands(command: str) -> list[list[str]] | None:
    """Tokenize *command* and split it into sub-command token lists.

    Splits on shell control operators (``&&``, ``||``, ``;``, ``|``, ``&``,
    parentheses) and on unquoted newlines, so a chained or multi-line command
    such as ``a && gh pr create`` or ``git add -A<newline>git push origin
    main`` is analyzed segment-by-segment.  Backslash-newline continuations are
    joined first (:func:`_join_line_continuations`).  Quoting is respected via
    ``shlex`` with ``punctuation_chars`` enabled so operators split even
    without surrounding whitespace, and a quoted newline (a multi-line commit
    message) stays inside its token.  Heredoc bodies are not modelled here:
    :func:`_evaluate_command` strips top-level bodies as stdin data
    (:func:`_strip_heredoc_bodies`) before calling it, re-evaluating any
    substitution bash expands inside an unquoted-delimiter body as a nested
    command.  Command-substitution bodies are not tokenised here —
    :func:`_evaluate_command` extracts and re-evaluates them
    (:func:`_substitution_bodies`) with their heredoc bodies kept as data, so
    the quoted ``"$(cat <<'EOF' … EOF)"`` idiom stays allowed.
    Returns ``None`` when the command cannot be tokenized (e.g. unbalanced
    quotes), letting the caller fail closed.
    """
    lexer = shlex.shlex(
        _strip_comments(_join_line_continuations(command)),
        posix=True,
        punctuation_chars=_PUNCTUATION_CHARS,
    )
    lexer.whitespace = " \t\r"
    # Comments are pre-stripped; shlex's own ``#`` handling would
    # ``readline()`` past the newline and merge the next line's segment.
    lexer.commenters = ""
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if _is_separator(token):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _strip_env_prefix(tokens: list[str]) -> list[str]:
    """Drop a leading ``env`` and any ``NAME=value`` assignment prefix.

    ``GH_TOKEN=x gh pr create`` and ``env GH_TOKEN=x gh pr create`` both
    resolve to a ``gh`` program once the prefix is stripped.  Once ``env`` is
    seen, its own option flags (``env -i``, ``env --unset=X``, ``env -u NAME``)
    are also skipped so the real program is not mistaken for one of them.
    """
    index = 0
    seen_env = False
    while index < len(tokens):
        token = tokens[index]
        if token == "env":
            seen_env = True
            index += 1
            continue
        if seen_env and token.startswith("-"):
            # An ``env`` option flag; ``-u NAME``/``--unset NAME`` also
            # consume the following token as the variable name.
            index += 1
            if token in _ENV_VALUE_FLAGS and index < len(tokens):
                index += 1
            continue
        name, sep, _value = token.partition("=")
        if sep and name.isidentifier():
            index += 1
            continue
        break
    return tokens[index:]


def _strip_reserved_prefix(tokens: list[str]) -> list[str]:
    """Drop leading :data:`_SHELL_RESERVED_PREFIXES` (and ``time -p``)."""
    index = 0
    while index < len(tokens) and tokens[index] in _SHELL_RESERVED_PREFIXES:
        if tokens[index] == "time" and tokens[index + 1:index + 2] == ["-p"]:
            index += 1
        index += 1
    return tokens[index:]


def _strip_command_prefix(tokens: list[str]) -> list[str]:
    """Alternate the env and reserved-word strippers until neither applies.

    ``if FOO=1 gh pr merge 5`` and ``time env X=1 git push origin main`` both
    resolve to the real program.
    """
    while True:
        stripped = _strip_reserved_prefix(_strip_env_prefix(tokens))
        if stripped == tokens:
            return tokens
        tokens = stripped


def _strip_leading_global_flags(
    args: list[str], value_flags: frozenset[str],
) -> list[str]:
    """Drop leading global option flags to expose the real subcommand.

    Skips flag tokens (those starting with ``-``); for a flag in *value_flags*
    given in separate-value form (``-C /repo``, ``--repo o/r``) also skips the
    following token as its value, so that value is not mistaken for the
    subcommand.  The ``--flag=value`` form is a single token and is skipped
    without consuming a successor.  Stops at the first non-flag token (the
    subcommand) and returns it plus the remainder.

    This is what lets ``git -C /repo push origin main`` and
    ``gh --repo o/r pr create`` be located by their *subcommand* (``push`` /
    ``pr``) rather than by a brittle positional index, without mistaking an
    argument value such as the ``pr`` in ``gh issue create --title pr`` for the
    subcommand.
    """
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-"):
            break
        index += 1
        if token in value_flags and index < len(args):
            index += 1  # consume the flag's separate value token
    return args[index:]


def _is_redirect_op(token: str) -> bool:
    """Whether *token* is a shell redirection operator.

    True for a token built only from the redirection characters ``<>&|`` that
    carries at least one ``<`` or ``>`` — so ``>``, ``>>``, ``>&``, ``&>``,
    ``&>>``, ``>|``, ``<``, ``<&`` all match, while the bare control operators
    ``&`` and ``|`` (already consumed as segment separators by
    :func:`_split_commands`) do not.  Used by :func:`_strip_redirections` to
    recognise the operator tokens ``shlex`` emits for output-capture idioms
    (``2>&1`` tokenises to ``2``, ``>&``, ``1``).
    """
    return (
        bool(token)
        and all(ch in "<>&|" for ch in token)
        and any(ch in "<>" for ch in token)
    )


def _strip_redirections(tokens: list[str]) -> list[str]:
    """Drop shell redirections from a segment's tokens before refspec reading.

    Walks *tokens* and removes each redirection operator
    (:func:`_is_redirect_op`) together with its immediately-following target
    operand and any immediately-preceding bare-integer file-descriptor token,
    so an output-captured push no longer leaks stray tokens into the positional
    refspecs.  ``2>&1`` (``2``, ``>&``, ``1``) drops all three; ``> out`` drops
    the operator *and* its operand — the operand is never laundered into a
    refspec, so ``git push origin > maestro/evolve/x`` stays a refspec-less,
    fail-closed push rather than one that appears to target an owned branch.  A
    trailing operator with no operand (``… >``) is dropped alone.  Pure and
    order-preserving; mirrors the existing :func:`_strip_env_prefix` /
    :func:`_strip_leading_global_flags` token strippers.

    Known limitation (accepted; see proposal 096 *Out of scope*): because the
    tokenizer discards whitespace, a *space-separated* positional numeric
    refspec preceding a redirect — ``git push origin owned 5 > log``, which bash
    reads as pushing both ``owned`` *and* the foreign branch ``5`` — is
    indistinguishable from the no-space file-descriptor form ``2>&1``, so the
    ``5`` is popped as an fd and the foreign push is laundered to an allow.
    This is a fail-*open* shape outside the increment's fail-*closed*,
    non-adversarial threat model: it is unreachable for ``main``/``master``
    (never numeric), force-pushes (``+``/``--force`` never numeric), and any
    owned ``maestro/*`` branch (never a bare integer), so it can only affect a
    push to a branch literally named by an integer — a shape the loop never
    instructs.  Closing it robustly needs a pre-tokenization raw-string pass and
    is deferred; ``tests/test_push_conformance.py`` pins the current behavior.
    """
    result: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _is_redirect_op(token):
            # Drop a bare integer file-descriptor token immediately preceding
            # the operator (the ``2`` in ``2>&1`` / ``2> log``).
            if result and result[-1].isdigit():
                result.pop()
            # Skip the operator and its target operand (``> out`` → both go).
            index += 2
            continue
        result.append(token)
        index += 1
    return result


def _dest_ref(refspec: str) -> str:
    """Return the destination ref of a ``git push`` refspec.

    ``main`` → ``main``; ``HEAD:main`` → ``main``; ``+main`` → ``main``.
    """
    spec = refspec.lstrip("+")
    if ":" in spec:
        return spec.split(":", 1)[1]
    return spec


def _next_positional(args: list[str], start: int) -> str:
    """First non-flag token in ``args`` at or after index *start*."""
    for token in args[start:]:
        if not token.startswith("-"):
            return token
    return ""


def _gh_api_endpoint(args: list[str]) -> str | None:
    """The endpoint positional of a ``gh api`` call, skipping flag values.

    Mirrors :func:`_strip_leading_global_flags`: ``-``-prefixed tokens are
    skipped, and a flag in :data:`_GH_API_VALUE_FLAGS` given in separate-value
    form also consumes its successor, so ``gh api -X PUT <path>`` yields
    ``<path>`` rather than ``PUT``.  The ``-XPUT`` / ``--flag=value`` forms are a
    single token and consume nothing.  Returns ``""`` when there is no
    positional and ``None`` when there are several (the endpoint is ambiguous).
    """
    positionals: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token.startswith("-"):
            if token in _GH_API_VALUE_FLAGS and index < len(args):
                index += 1  # consume the flag's separate value token
            continue
        positionals.append(token)
    if len(positionals) > 1:
        return None
    return positionals[0] if positionals else ""


def _attached_short_field_value(token: str) -> str | None:
    """The value of an attached short field flag, or ``None``.

    pflag accepts ``-f<value>`` and ``-f=<value>`` for a short flag, so
    ``-fhead=x`` and ``-f=head=x`` both carry ``head=x``.  The one recogniser
    shared by the mutation check and the reply carve-out so they cannot drift
    (#4383).
    """
    if len(token) > 2 and token[:2] in _GH_API_SHORT_FIELD_FLAGS:
        value = token[2:]
        return value[1:] if value.startswith("=") else value
    return None


def _has_in_reply_to_field(args: list[str]) -> bool:
    """Whether ``args`` carries a ``gh api`` ``in_reply_to`` field flag.

    Recognizes the separate-value form (``-f in_reply_to=<id>`` /
    ``--field in_reply_to=<id>``), the single-token ``--field=in_reply_to=<id>``
    form, and the attached short form (``-fin_reply_to=<id>``).  The ``in_reply_to`` field is what turns a ``…/pulls/<n>/comments``
    POST into a *reply* to an existing review comment rather than a fresh
    top-level review comment.
    """
    for i, token in enumerate(args):
        attached = _attached_short_field_value(token)
        if attached is not None:
            if attached.startswith("in_reply_to="):
                return True
        elif token in _GH_API_FIELD_FLAGS:
            value = args[i + 1] if i + 1 < len(args) else ""
            if value.startswith("in_reply_to="):
                return True
        elif any(
            token.startswith(flag + "=") for flag in _GH_API_FIELD_FLAGS
        ) and token.split("=", 1)[1].startswith("in_reply_to="):
            return True
    return False


def _is_review_comment_reply(args: list[str], path: str) -> bool:
    """Whether a ``gh api`` pulls call only *replies to* a review comment.

    Two GitHub forms post a reply to an existing inline review comment, and
    neither can open, merge, ready, or push a PR — they are as benign as the
    already-allowed ``gh pr comment``, and the PR-remediation flow mandates one
    of them to answer inline review threads (#2819):

    * the dedicated replies endpoint ``…/pulls/<n>/comments/<id>/replies``, and
    * the ``…/pulls/<n>/comments`` create endpoint carrying an ``in_reply_to``
      field.

    Anything else under ``pulls`` (opening, merging, editing the PR, requesting
    reviewers, or a fresh top-level review comment) stays subject to the
    mutation check.
    """
    normalized = path.rstrip("/")
    if normalized.endswith("/replies"):
        return True
    return normalized.endswith("/comments") and _has_in_reply_to_field(args)


def _is_mutating_pulls_api(args: list[str], path: str) -> bool:
    """Whether a ``gh api`` call against *path* mutates a pulls endpoint.

    True when *path* references ``pulls`` *and* the call is a mutation — an
    explicit mutating method, a ``/merge`` path, or any body-carrying flag
    (``gh api`` sends POST when a field or ``--input`` body is present),
    including an attached short field flag (``-fhead=x``).  Replying to a review comment is
    carved out as benign (see :func:`_is_review_comment_reply`) so the
    PR-remediation threaded-reply mandate (#2819) is not denied.
    """
    if "pulls" not in path:
        return False
    if _is_review_comment_reply(args, path):
        return False
    if path.rstrip("/").endswith("/merge"):
        return True
    return _is_mutating_api_call(args)


#: ``gh api`` methods that stay reads even when field flags are present.
_READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})


def _is_mutating_api_call(args: list[str]) -> bool:
    """Whether a ``gh api`` call's flags make it a mutation.

    True for an explicit mutating method (``-X``/``--method`` in any of its
    separate, ``=`` or attached forms) or any body-carrying flag — ``gh api``
    sends POST when a field or ``--input`` body is present, including an
    attached short field flag (``-fhead=x``).  An explicit read method
    (:data:`_READ_METHODS`; the last ``-X`` wins, as in pflag) overrides the
    body heuristic: under ``-X GET`` ``gh api`` sends fields as query
    parameters, so ``gh api -X GET …/releases -f per_page=100`` is a read.
    """
    method: str | None = None
    has_body = False
    for i, token in enumerate(args):
        if token in {"-X", "--method"}:
            method = (args[i + 1] if i + 1 < len(args) else "").upper()
        elif token.startswith("--method="):
            method = token.split("=", 1)[1].upper()
        elif token.startswith("-X") and len(token) > 2:
            # pflag accepts ``-XPUT`` and ``-X=PUT`` alike.
            method = token[2:].removeprefix("=").upper()
        elif (
            token in _GH_API_BODY_FLAGS
            or any(token.startswith(flag + "=") for flag in _GH_API_BODY_FLAGS)
            or _attached_short_field_value(token) is not None
        ):
            has_body = True
    if method in _READ_METHODS:
        return False
    return method in _MUTATING_METHODS or has_body


def _is_outward_api_path(path: str) -> bool:
    """Whether a ``gh api`` *path* names an outward-reach endpoint."""
    normalized = _API_URL_PREFIX_RE.sub("", path.split("?", 1)[0]).lstrip("/")
    return _OUTWARD_API_PATH_RE.match(normalized) is not None


def _gh_verb(sub_args: list[str]) -> str:
    """The verb of a ``gh <subcommand>`` call, skipping global value flags."""
    rest = _strip_leading_global_flags(sub_args, _GH_VALUE_FLAGS)
    return rest[0] if rest else ""


def _gh_deny(args: list[str]) -> str | None:
    """Deny reason for a ``gh`` sub-command, or ``None`` to allow.

    The sub-command is located positionally *after* stripping leading global
    flags (``gh --repo o/r pr create`` → subcommand ``pr``), so a global flag
    before it cannot shift it out of view, yet an argument *value* that happens
    to read ``pr``/``api`` (``gh issue create --title pr``) is not mistaken for
    the subcommand.  A ``pr`` sub-command whose verb is neither a known read verb,
    a known benign verb (``comment``), nor a reversible-disposal verb (``close``)
    is denied fail-closed.

    A ``gh api`` endpoint is located by :func:`_gh_api_endpoint`, which skips
    value-taking flags *with* their values, so flags before the endpoint
    (``gh api -X PUT repos/o/r/pulls/5/merge``) cannot hide it.  When the
    endpoint is ambiguous (several positionals) and any positional references
    ``pulls``, the call is denied fail-closed.

    Outward reach: ``gh repo`` / ``gh release`` /
    ``gh secret`` are allowed only for a read verb, and a mutating ``gh api``
    call against an outward endpoint (:func:`_is_outward_api_path`) is denied
    with :data:`_OUTWARD_DENY`; an ambiguous endpoint with an outward
    positional fails closed, mirroring the pulls rule.
    """
    rest = _strip_leading_global_flags(args, _GH_VALUE_FLAGS)
    if not rest:
        return None
    subcommand, sub_args = rest[0], rest[1:]
    if subcommand == "pr":
        verb = _next_positional(sub_args, 0)
        if (
            verb in _GH_PR_READ_VERBS
            or verb in _GH_PR_BENIGN_VERBS
            or verb in _GH_PR_DISPOSAL_VERBS
        ):
            return None
        return _PR_DENY
    read_verbs = {
        "repo": _GH_REPO_READ_VERBS,
        "release": _GH_RELEASE_READ_VERBS,
        "secret": _GH_SECRET_READ_VERBS,
    }.get(subcommand)
    if read_verbs is not None:
        return None if _gh_verb(sub_args) in read_verbs else _OUTWARD_DENY
    if subcommand == "api":
        path = _gh_api_endpoint(sub_args)
        if path is None:
            positionals = [t for t in sub_args if not t.startswith("-")]
            if any("pulls" in token for token in positionals):
                return _PR_DENY
            if any(_is_outward_api_path(token) for token in positionals):
                return _OUTWARD_DENY
            return None
        if _is_mutating_pulls_api(sub_args, path):
            return _PR_DENY
        if _is_outward_api_path(path) and _is_mutating_api_call(sub_args):
            return _OUTWARD_DENY
    return None


def _npm_exec_index(args: list[str]) -> int | None:
    """Index of the ``exec`` / ``x`` subcommand in ``npm`` *args*, or ``None``.

    Every token before ``--`` is checked rather than only the first, so a
    leading npm option (``npm --yes exec …``, ``npm --loglevel silent exec …``)
    cannot hide the subcommand; an option *value* spelled ``exec``/``x`` is
    routed too, a fail-closed cost.
    """
    for index, token in enumerate(args):
        if token == "--":
            return None
        if token in ("exec", "x"):
            return index
    return None


def _npx_deny(
    args: list[str],
    allowed_branches: frozenset[str] | None,
    depth: int,
    strip_redirections: bool,
    *,
    current_branch: str | None,
) -> str | None:
    """Deny reason for an ``npx`` invocation, or ``None`` to allow.

    ``-c``/``--call`` command strings are re-evaluated at ``depth + 1``;
    ``--`` ends the options.  The ``-c``/``--call`` search checks every token
    before ``--`` rather than stopping at the first positional, so an unknown
    value-taking option (``npx --loglevel silent -c …``) cannot hide it;
    ``npx foo -c "…"`` is re-evaluated too, a fail-closed cost.
    ``npm exec`` / ``npm x`` route here too.
    """
    for index, token in enumerate(args):
        if token == "--":
            break
        if token in ("-c", "--call") or token.startswith("--call="):
            if token.startswith("--call="):
                inner = token.split("=", 1)[1]
            else:
                inner = args[index + 1] if index + 1 < len(args) else ""
            inner_reason = _evaluate_command(
                inner, allowed_branches, depth + 1, strip_redirections,
                current_branch=current_branch,
            )
            if inner_reason is not None:
                return inner_reason
    return None


#: Shortest ``--`` token treated as an abbreviation of a gated long flag.
#: git's parse-options accepts any unique prefix of a long option (``--tag``
#: for ``--tags``); ``--`` plus two letters is the floor.
_MIN_LONG_FLAG_ABBREV = 4


def _abbreviates_long_flag(token: str, flags: frozenset[str]) -> bool:
    """Whether *token* is a git-style abbreviation of a long flag in *flags*.

    A ``--`` token (no ``=`` value) of at least
    :data:`_MIN_LONG_FLAG_ABBREV` characters that is a prefix of any member
    counts as that flag.  An ambiguous prefix is an error in git, so treating
    it as the gated flag (fail-closed) costs nothing.
    """
    if "=" in token or len(token) < _MIN_LONG_FLAG_ABBREV:
        return False
    return token.startswith("--") and any(flag.startswith(token) for flag in flags)


def _is_force_push(rest: list[str], refspecs: list[str]) -> bool:
    """Whether a ``git push`` is a force-push.

    *rest* is the push's tokens after leading global flags are stripped (so its
    flag tokens are visible); *refspecs* is its positional refspecs.  True when
    any token is a force flag (:data:`_FORCE_PUSH_FLAGS`, or a
    ``--force-with-lease=<value>`` token matched by prefix) or any refspec is
    ``+``-prefixed (``+main``, ``+HEAD:main``).  A force-push rewrites published
    history and is denied regardless of destination.
    """
    for token in rest:
        if (
            token in _FORCE_PUSH_FLAGS
            or token.startswith("--force-with-lease=")
            or _abbreviates_long_flag(token, _FORCE_PUSH_FLAGS)
        ):
            return True
    return any(spec.startswith("+") for spec in refspecs)


#: The remotes a scoped run may push to, set for the duration
#: of one :func:`evaluate_bash_command` call so every nested unwrap (``bash -c``,
#: ``gate-lease``, substitutions) sees it without re-threading each recursion
#: layer.  ``None`` (the default) leaves every push's remote unchecked.
_ALLOWED_PUSH_REMOTES: ContextVar[frozenset[str] | None] = ContextVar(
    "_ALLOWED_PUSH_REMOTES", default=None,
)

_SCP_REPO_RE = re.compile(r"^[\w.-]+@(?P<host>[\w.-]+):(?P<path>[^/].*)$")
_URL_REPO_RE = re.compile(
    r"^(?:https?|ssh|git)://(?:[^@/#?\\\s]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+)$",
)
_BARE_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")

#: The only host a URL or scp-style repo reference may name: an ``owner/repo``
#: path on any other host is another repo, never the scoped one.
_REPO_HOST = "github.com"


def normalize_repo_ref(ref: str, *, allow_bare: bool = True) -> str | None:
    """Normalize a repo reference to a lower-cased ``owner/repo``, else ``None``.

    Accepts an https/ssh/git URL and an scp-style ``user@host:owner/repo`` on
    the host ``github.com`` only, and (when *allow_bare*) a bare
    ``owner/repo``, each with an optional ``.git`` suffix.  Any other shape (a
    remote *name*, a local path, a deeper path, another host) returns ``None``.
    The push site passes ``allow_bare=False``: git reads a bare ``owner/repo``
    there as a relative local path, not a GitHub repo.
    """
    ref = ref.strip()
    match = _URL_REPO_RE.match(ref) or _SCP_REPO_RE.match(ref)
    if match is not None:
        if match.group("host").lower() != _REPO_HOST:
            return None
        path = match.group("path")
    elif allow_bare:
        path = ref
    else:
        return None
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if not _BARE_REPO_RE.match(path):
        return None
    return path.lower()


def _is_remote_config_key(value: str) -> bool:
    """Whether a git config key/assignment rewrites a remote or URL mapping."""
    lowered = value.lower()
    # ``include.path`` / ``includeIf.*`` pull in a file that can itself set
    # ``remote.*`` / ``url.*``.
    if lowered.startswith(("remote.", "url.", "include.", "includeif.")):
        return True
    # ``branch.<name>.remote`` / ``.pushremote`` pick a bare push's target
    # ahead of ``origin``; the key may carry an ``=value`` assignment.
    key = lowered.split("=", 1)[0]
    return key.startswith("branch.") and key.endswith((".remote", ".pushremote"))


#: ``git config`` flags (and ``get``/``list`` subcommands) that only read.
_CONFIG_READ_FLAGS = frozenset({
    "--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list",
})


def _is_config_read(rest: list[str], positional: list[str]) -> bool:
    """Whether a ``git config`` invocation only reads (never rewrites a key)."""
    if any(a in _CONFIG_READ_FLAGS for a in rest):
        return True
    return len(positional) > 1 and positional[1] in {"get", "list"}


def _remote_scope_deny(
    args: list[str], rest: list[str], positional: list[str], remotes: frozenset[str],
) -> str | None:
    """Deny reason under remote scoping, else ``None``.

    Refuses a ``-c remote.*`` / ``url.*`` global override on any git command,
    ``git remote add|set-url|rename|remove|rm``, ``git config remote.*`` /
    ``url.*``, and a push whose remote (``origin`` when omitted) is neither a
    member of *remotes* nor a github.com URL normalizing to one, that uses
    ``--repo``, or that re-targets the checkout with ``-C`` / ``--git-dir`` /
    ``--work-tree``.

    Accepted limit (the module's non-adversarial threat model): a remote
    *name* in *remotes* (``origin``) is trusted as the one the session's own
    checkout configures.  A ``cd <other checkout> && git push origin …`` or a
    ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_*`` environment prefix that rewrites
    ``remote.origin.url``, or a ``GIT_DIR``/``GIT_WORK_TREE`` environment
    prefix that re-targets the checkout, can still point ``origin`` at another
    repo; that is
    deliberate evasion, out of scope here and closed by the worker container
    (W2), which holds no other checkout.
    """
    lead = args[: len(args) - len(rest)]
    for index, token in enumerate(lead):
        value = ""
        if token == "-c" and index + 1 < len(lead):
            value = lead[index + 1]
        elif token.startswith("-c") and len(token) > 2:
            value = token[2:]
        elif token == "--config-env" and index + 1 < len(lead):
            value = lead[index + 1]
        elif token.startswith("--config-env="):
            value = token.split("=", 1)[-1]
        if value and _is_remote_config_key(value):
            return _PUSH_REMOTE_FOREIGN_DENY
    if not positional:
        return None
    verb = positional[0]
    if verb == "remote" and len(positional) > 1 and positional[1] in {
        "add", "set-url", "rename", "remove", "rm",
    }:
        return _PUSH_REMOTE_FOREIGN_DENY
    if verb == "config" and not _is_config_read(rest, positional) and any(
        _is_remote_config_key(a) for a in positional[1:]
    ):
        return _PUSH_REMOTE_FOREIGN_DENY
    if verb != "push":
        return None
    # A push that re-targets the checkout (``-C`` / ``--git-dir`` /
    # ``--work-tree``) could make ``origin`` name another repo's remote.
    if any(
        token in {"-C", "--git-dir", "--work-tree"}
        or token.startswith(("--git-dir=", "--work-tree="))
        or (token.startswith("-C") and len(token) > 2)
        for token in lead
    ):
        return _PUSH_REMOTE_FOREIGN_DENY
    if any(a == "--repo" or a.startswith("--repo=") for a in rest):
        return _PUSH_REMOTE_FOREIGN_DENY
    remote = positional[1] if len(positional) > 1 else "origin"
    # A member that is a remote *name* matches by name; an ``owner/repo``
    # member matches only a github.com URL normalizing to it (a bare
    # ``owner/repo`` on the command line is a local path to git).
    lowered = {r.lower() for r in remotes}
    names = {r for r in lowered if normalize_repo_ref(r) is None}
    if remote.lower() in names:
        return None
    normalized = normalize_repo_ref(remote, allow_bare=False)
    if normalized is not None and normalized in lowered:
        return None
    return _PUSH_REMOTE_FOREIGN_DENY


def _git_deny(
    args: list[str],
    allowed_branches: frozenset[str] | None,
    *,
    current_branch: str | None = None,
) -> str | None:
    """Deny reason for a ``git`` sub-command, or ``None`` to allow.

    Only ``git push`` is gated.  A push is allowed only when every destination
    ref is in the run's owned ``allowed_branches`` set and none is protected; a
    push that cannot be confirmed to target only owned branches (bare
    ``git push`` / ``git push <remote>`` without a refspec or a bare ``HEAD``
    destination when no ``current_branch`` is injected — see below — or a falsy
    ``allowed_branches`` — ``None`` or the empty set) fails closed.  A
    force-push (:func:`_is_force_push`) is denied unconditionally, even to an
    otherwise-owned branch, so a published branch's history stays append-only.
    Leading git global flags are stripped first so a value-taking flag
    (``git -C /repo push``, ``git -c k=v push``) cannot hide the ``push``
    subcommand behind its value token.

    ``current_branch`` is the run's own checked-out branch, resolved and
    *injected* by the caller — the gate never re-derives it (the
    ``test_gate_module_never_re_derives_ownership`` guard forbids this module
    reaching into the owned-branch oracle).  It lets two branch-name-less push
    shapes the model naturally writes be resolved *before* the owned-set check:
    a bare ``HEAD`` destination (``git push origin HEAD``) and a refspec-less
    push (bare ``git push`` / ``git push <remote>``) both target the current
    branch.  With ``current_branch`` ``None`` (the branch-fallback path, or any
    non-activated caller) both keep today's fail-closed deny — the destination
    is unknowable — so this widens only the gate's *parsing*, never its policy:
    the resolved branch is always already a member of ``allowed_branches``, so
    no push reaches the remote that a ``HEAD:<branch>`` form would not.  The
    resolution binds a *single* destination, so a multi-ref push flag
    (``--all`` / ``--mirror`` / ``--tags`` / ``--follow-tags``,
    :data:`_ALL_REFS_PUSH_FLAGS`) — which pushes every
    local branch regardless of the refspec — is denied *before* it, so an owned
    ``current_branch`` never launders one into an allow.  Accepted
    upstream-config limitation (matching the module's threat model): a
    non-adversarial model's bare push targets the same-named ``origin`` branch;
    a deliberately mis-configured push upstream is out of scope.

    Accepted live-HEAD limitation (same non-adversarial threat model):
    ``current_branch`` is the branch bound once at run start, but git resolves a
    bare ``HEAD`` / refspec-less push against the *live* checked-out branch.
    ``git checkout`` / ``git switch`` is not gated (only ``push`` / PR are) and
    the gate cannot re-derive live HEAD (the
    ``test_gate_module_never_re_derives_ownership`` guard forbids reaching into
    the owned-branch oracle), so a deliberate ``git checkout main`` followed by
    ``git push origin HEAD`` (or a bare ``git push``) is evaluated against the
    owned *injected* branch and allowed while git actually pushes the live HEAD
    to ``origin/main``.  This checkout-then-HEAD-push vector is out of scope: it
    is the module's already-excluded deliberate evasion — a redirected model
    checked out on its own task branch never checks out ``main`` before a HEAD
    push — and the explicit ``HEAD:<branch>`` / ``origin <branch>`` forms bind the
    destination independently of live HEAD as the robust route when it matters.
    """
    rest = _strip_leading_global_flags(args, _GIT_VALUE_FLAGS)
    positional = [a for a in rest if not a.startswith("-")]
    remotes = _ALLOWED_PUSH_REMOTES.get()
    if remotes is not None:
        scoped = _remote_scope_deny(args, rest, positional, remotes)
        if scoped is not None:
            return scoped
    if not positional or positional[0] != "push":
        return None
    if not allowed_branches:  # None or empty set → every push fails closed.
        return _PUSH_DENY
    # positional[0]="push", positional[1]=remote, positional[2:]=refspecs.
    refspecs = positional[2:]
    if _is_force_push(rest, refspecs):
        return _FORCE_PUSH_DENY
    if any(
        token in _ALL_REFS_PUSH_FLAGS
        or _abbreviates_long_flag(token, _ALL_REFS_PUSH_FLAGS)
        for token in rest
    ):
        # ``--all`` / ``--mirror`` push every local branch (including protected
        # refs), and ``--tags`` / ``--follow-tags`` publish tags, regardless
        # of the refspec or the resolved current branch, so the destination
        # is not the only ref that reaches the remote.  This runs before the
        # refspec-less and bare-``HEAD`` resolution below so an owned
        # ``current_branch`` cannot launder ``git push --all`` or
        # ``git push origin HEAD --all`` into an allow.
        return _ALL_REFS_PUSH_DENY
    if not refspecs:
        # Bare ``git push`` / ``git push <remote>``: the destination is implied
        # by the current branch.  ``None`` (unknowable) keeps the fail-closed
        # deny; otherwise apply the same protected/owned check to it.
        if not current_branch:
            return _PUSH_DENY
        if (
            current_branch in _PROTECTED_REFS
            or current_branch not in allowed_branches
        ):
            return _PUSH_DENY
        return None
    for spec in refspecs:
        dest = _dest_ref(spec)
        if spec == "HEAD":
            # Only the *lone-source* ``HEAD`` refspec (``git push origin HEAD``)
            # resolves to the run's current branch; ``None`` leaves it
            # unresolvable so the owned-set check below fails closed.  Keying on
            # the literal refspec token — not the parsed destination — is load
            # bearing: ``_dest_ref`` also returns ``HEAD`` for a ``<src>:HEAD``
            # refspec (``main:HEAD``), whose ``HEAD`` is resolved on the *remote*
            # (its default branch, ``main``/``master``) and whose source is
            # unrelated to the current branch, so resolving that to an owned
            # ``current_branch`` would launder a push to the remote's protected
            # default into an allow.  A ``<src>:HEAD`` spec therefore keeps
            # ``dest == "HEAD"``, which is never in ``allowed_branches`` → denied.
            dest = current_branch or ""
        if dest in _PROTECTED_REFS or dest not in allowed_branches:
            return _PUSH_DENY
    return None


def _wrapper_deny(
    args: list[str],
    allowed_branches: frozenset[str] | None,
    depth: int,
    strip_redirections: bool = False,
    *,
    current_branch: str | None = None,
    non_leading: bool = False,
) -> str | None:
    """Deny reason for a shell-interpreter segment (``bash -c "…"``).

    Unwraps the ``-c`` command string -- the first non-option operand after
    the shell's option list, located by :func:`_shell_script_operand` along
    the shell's short-option grammar (its divergences, e.g. long options after
    ``-c``, only go toward rejecting or denying) -- and re-evaluates it so a
    PR/push mutation hidden behind ``bash -c``/``sh -c`` is caught instead of
    waved through.  When no clean ``-c <string>`` is present but a
    PR/push-shaped command rides a combined flag (e.g. ``bash -lc "gh pr
    create"``) or a bare ``-c`` whose script operand cannot be located (an
    option consumes it, ``bash -c -o "gh pr merge 5"``; a non-letter
    option-shaped token stops the walk, ``bash -c "-x; gh pr merge 5"``; or a
    script file precedes ``-c``), it fails closed.  *strip_redirections*
    rides the recursive re-evaluation so a wrapped
    ``… 2>&1`` is stripped at every unwrap level, not just the outermost;
    *current_branch* rides it identically so a wrapped bare ``HEAD``/refspec-less
    push resolves to the run's branch at every level too.  With *non_leading*
    (the wrapper name sits mid-segment, e.g. ``rg -t sh "git push"``) the
    fail-closed fallback fires only when a combined short-flag cluster carrying
    ``c`` (``-lc``/``-ec``) is present, so a bare ``sh`` argument value is not
    mistaken for an interpreter.
    """
    operand = _shell_script_operand(args)
    if operand is not None:
        return _evaluate_command(
            operand, allowed_branches, depth + 1, strip_redirections,
            current_branch=current_branch,
        )
    if non_leading and not any(
        token.startswith("-") and not token.startswith("--")
        and token[1:].isalpha() and "c" in token[1:]
        for token in args
    ):
        return None
    for token in args:
        if _GATED_TEXT_RE.search(token.lower()) is not None:
            return _PARSE_DENY
    return None


def _maestro_deny(
    args: list[str],
    allowed_branches: frozenset[str] | None,
    depth: int,
    strip_redirections: bool = False,
    *,
    current_branch: str | None = None,
) -> str | None:
    """Deny reason for a ``maestro`` segment whose subcommand execs a child.

    ``maestro gate-lease [--] <cmd> <args…>`` (proposal 126) runs everything
    after its own flags as a child process, so a PR/push mutation smuggled as
    that child (``maestro gate-lease -- gh pr merge 5``) would otherwise sail
    past a gate that only keys on the leading ``maestro`` program — the same
    hole a bare ``bash -c "gh pr merge 5"`` would open, which
    :func:`_wrapper_deny` already closes.  When the segment is a ``gate-lease``
    invocation carrying a child, that child argv is re-evaluated as one segment:
    ``subprocess`` execs it directly with no shell, so it is exactly one command
    (a shell-wrapped child such as ``bash -c "…"`` is caught by the ordinary
    wrapper dispatch on re-evaluation).  ``depth + 1`` bounds a
    ``gate-lease``-wrapping-``gate-lease`` chain exactly as the shell-wrapper
    unwrap is bounded, failing closed past the limit.  The ``--enabled`` flag
    probe and every other ``maestro`` subcommand exec no child and are allowed.
    """
    if depth > _MAX_UNWRAP_DEPTH:
        return _PARSE_DENY
    if not args or args[0] != "gate-lease":
        return None
    child = args[1:]
    # Drop a leading end-of-options marker if the model wrote ``-- <cmd>``.
    if child and child[0] == "--":
        child = child[1:]
    if not child:
        return None
    return _segment_deny(
        child, allowed_branches, depth + 1, strip_redirections,
        current_branch=current_branch,
    )


def _module_maestro_args(program: str, args: list[str]) -> list[str] | None:
    """Return the ``maestro`` subcommand argv behind a ``python -m maestro.cli`` segment.

    The hand-run gate docs invoke the CLI as ``python3.14 -m maestro.cli
    gate-lease -- <cmd>`` so it self-resolves to the checkout under test — a
    bare ``maestro`` would resolve off ``PATH`` to a sibling checkout's editable
    install.  That module spelling must reach the same ``gate-lease`` child
    unwrap as the bare console script (:func:`_maestro_deny`), or a PR/push
    mutation smuggled as ``python3.14 -m maestro.cli gate-lease -- gh pr merge
    5`` would sail past a gate that keys only on a leading ``maestro`` program
    (r1).

    ``-m`` need not be ``args[0]``: any leading interpreter option shifts it
    right while CPython still runs the module, so requiring ``-m`` first would
    re-open the hole behind a benign flag — ``python3.14 -I -m maestro.cli
    gate-lease -- gh pr merge 5`` (verified to execute the module) sails past.
    The scan therefore skips a leading run of interpreter options before
    locating ``-m``, honouring their real ``getopt`` semantics: value-less
    single-letter flags (``-I``/``-B``/``-O``/``-S``/``-E``…) bundle freely
    (``-IB``, even ``-Im`` == ``-I -m``); ``-W``/``-X`` and the long
    ``--check-hash-based-pycs`` consume a value (token remainder or the next
    token); ``-c`` (run a command string) and a non-option script path each
    terminate the search — neither is a ``-m`` module invocation.  Returns the
    subcommand tokens (``["gate-lease", "--", …]``) for ``-m maestro`` / ``-m
    maestro.cli`` (``-m mod`` or combined ``-mmod``), else ``None``.
    """
    base = program.rsplit("/", 1)[-1]
    if base != "python" and not base.startswith("python3"):
        return None
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "-" or not tok.startswith("-"):
            # A bare token (``-`` stdin, or a script path) before ``-m`` — a
            # ``python foo.py`` invocation, not a module selector.
            return None
        if tok.startswith("--"):
            # ``--`` ends option parsing; ``--check-hash-based-pycs`` is the one
            # long option that can precede ``-m`` and, in its separate-value
            # form (no ``=``), consumes the next token.
            if tok == "--":
                return None
            i += 2 if tok == "--check-hash-based-pycs" else 1
            continue
        # Short-option cluster (``-I``, ``-IB``, ``-Xutf8``, ``-Im``,
        # ``-mmaestro.cli``): scan its letters left to right.  The first
        # argument-taking letter (``m``/``c``/``W``/``X``) consumes the token
        # remainder, or the next token when it is the cluster's last letter.
        j = 1
        while j < len(tok):
            letter = tok[j]
            if letter == "m":
                remainder = tok[j + 1:]
                module: str | None
                if remainder:
                    module = remainder
                    rest = args[i + 1:]
                else:
                    module = args[i + 1] if i + 1 < len(args) else None
                    rest = args[i + 2:]
                if module not in _MAESTRO_CLI_MODULES:
                    return None
                return rest
            if letter == "c":
                # ``-c`` runs a command string, not a ``-m`` module.
                return None
            if letter in ("W", "X"):
                # Value-taking: its value is the token remainder or the next
                # token; either way this cluster carries no ``-m``.
                i += 1 if tok[j + 1:] else 2
                break
            j += 1
        else:
            # A cluster of only value-less flags; advance past it.
            i += 1
    return None


def _segment_deny(
    tokens: list[str],
    allowed_branches: frozenset[str] | None,
    depth: int,
    strip_redirections: bool = False,
    *,
    current_branch: str | None = None,
) -> str | None:
    """Deny reason for one sub-command segment, or ``None`` to allow.

    When the leading program is not ``gh``/``git`` and its own dispatch allows,
    every later position is re-dispatched through ``_program_deny``'s table
    (``gh``/``git``, shell ``-c`` wrappers, ``maestro gate-lease``,
    ``python -m maestro.cli gate-lease``) — ``xargs git push …``,
    ``timeout 300 bash -c "gh pr merge …"`` — so a gated program hiding behind
    an unrecognised launcher fails closed.  A leading ``eval`` instead
    re-evaluates its joined arguments and suppresses the tail rescan.
    """
    tokens = _strip_command_prefix(tokens)
    if not tokens:
        return None
    reason = _program_deny(
        tokens, allowed_branches, depth, strip_redirections,
        current_branch=current_branch,
    )
    if (
        reason is not None
        or tokens[0] in ("gh", "git", "eval")
        or _consumes_tail(tokens)
    ):
        return reason
    for index in range(1, len(tokens)):
        tail = tokens[index:]
        reason = _program_deny(
            tail, allowed_branches, depth, strip_redirections,
            current_branch=current_branch, non_leading=True,
        )
        if reason is not None:
            return reason
        if _consumes_tail(tail):
            # Its dispatch already evaluated the whole tail; re-scanning the
            # remaining positions would re-evaluate the same payload once per
            # launcher token, compounding per unwrap level (r1).
            break
    return None


def _consumes_tail(tokens: list[str]) -> bool:
    """Whether dispatching *tokens* already evaluated every executable part.

    A shell wrapper whose ``-c`` script operand was located evaluates that
    command string (later tokens are positional ``$0``/``$1`` …, never
    executed); a ``gate-lease`` child is re-evaluated through
    :func:`_segment_deny`, which scans it end to end.
    """
    program, args = tokens[0], tokens[1:]
    if program in _SHELL_WRAPPERS:
        return _shell_script_operand(args) is not None
    if program == "maestro":
        return bool(args) and args[0] == "gate-lease"
    module_args = _module_maestro_args(program, args)
    return module_args is not None and module_args[:1] == ["gate-lease"]


def _program_deny(
    tokens: list[str],
    allowed_branches: frozenset[str] | None,
    depth: int,
    strip_redirections: bool,
    *,
    current_branch: str | None,
    non_leading: bool = False,
) -> str | None:
    """Dispatch a prefix-stripped segment on its leading program.

    *non_leading* marks a re-dispatch from a later position of a segment
    (:func:`_segment_deny`), where a shell-wrapper name may just be an argument
    value (``rg -t sh "git push"``) — see :func:`_wrapper_deny` — and an
    ``eval`` name is an argument (``grep eval …``), not the builtin.
    """
    program = tokens[0]
    args = tokens[1:]
    if program in ("command", "builtin"):
        # ``command [-p] eval "…"`` / ``builtin command eval "…"`` still run
        # the builtin; peel every launcher layer before checking for eval.
        rest = args
        while rest and rest[0] in ("command", "builtin", "-p"):
            rest = rest[1:]
        if rest[:1] == ["eval"]:
            program, args = rest[0], rest[1:]
    if program == "eval":
        if non_leading:
            return None
        # eval joins its arguments and re-parses them as a command line.
        return _evaluate_command(
            " ".join(args), allowed_branches, depth + 1, strip_redirections,
            current_branch=current_branch,
        )
    if program == "gh":
        return _gh_deny(args)
    if program == "git":
        return _git_deny(args, allowed_branches, current_branch=current_branch)
    if program == "npx":
        return _npx_deny(
            args, allowed_branches, depth, strip_redirections,
            current_branch=current_branch,
        )
    if program == "npm":
        exec_at = _npm_exec_index(args)
        if exec_at is not None:
            return _npx_deny(
                args[exec_at + 1:], allowed_branches, depth, strip_redirections,
                current_branch=current_branch,
            )
    if program in _SHELL_WRAPPERS:
        return _wrapper_deny(
            args, allowed_branches, depth, strip_redirections,
            current_branch=current_branch, non_leading=non_leading,
        )
    if program == "maestro":
        return _maestro_deny(
            args, allowed_branches, depth, strip_redirections,
            current_branch=current_branch,
        )
    module_args = _module_maestro_args(program, args)
    if module_args is not None:
        # ``python3.14 -m maestro.cli gate-lease -- <cmd>`` — the checkout-
        # resolving module spelling of the console script (r1): unwrap its
        # ``gate-lease`` child through the same path as the bare form.
        return _maestro_deny(
            module_args, allowed_branches, depth, strip_redirections,
            current_branch=current_branch,
        )
    return None


#: The one gated-text vocabulary both fail-closed fallbacks share (the
#: unparseable-command arm in :func:`_evaluate_command` and the combined-flag /
#: no-locatable-operand wrapper arm in :func:`_wrapper_deny`); both match
#: lowered text, hence
#: ``-r`` for ``gh -R``.  The tag arm is scoped to ``push`` so
#: ``git describe --tags`` / ``git fetch --tags`` are not swept up.
_GATED_TEXT_RE = re.compile(
    r"\bgh(?:\s+(?:(?:-r|--repo|--hostname)(?:\s+|=)\S+|-r\S+))*\s+(?:pr|repo|release|secret)\b"
    r"|\bgit\s+push\b|pulls"
    r"|\bpush\b[^;&|\n]*--(?:follow-)?tags?\b",
)
_SHELL_VALUE_OPTIONS = frozenset({"-o", "-O", "+o", "+O", "--rcfile", "--init-file"})
_STDIN_OPERANDS = frozenset({"-", "/dev/stdin"})


def _shell_script_operand(args: list[str]) -> str | None:
    """The ``-c`` script a shell invoked with *args* runs, or ``None``.

    ``-c`` is a mode flag: the shell keeps parsing options after it (``-e``,
    ``+x``, ``-o pipefail``, ``--``) and runs the first non-option operand.
    Follows the shell's short-option grammar, as :func:`_shell_reads_stdin`
    does, though not in lockstep (a lone ``-`` ends options here but means
    stdin there, and only this walk applies the letter-cluster check); every
    divergence from the real shell (e.g. long options after ``-c``, which bash
    rejects) only goes toward ``None`` or a deny.  An option-shaped token
    that is not a pure letter cluster (``"-x; gh pr"``) stops the walk with
    ``None`` so the caller fails closed.  Only a bare
    ``-c`` token counts; a combined cluster (``-lc``) yields ``None`` so the
    caller keeps its fail-closed fallback, as does a ``-c`` with no operand.
    """
    saw_c = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _SHELL_VALUE_OPTIONS:
            i += 2
            continue
        if arg in ("--", "-"):
            i += 1
            break
        if arg.startswith("--"):
            i += 1
            continue
        if arg == "+":
            # bash and dash ignore a lone ``+`` while parsing options.
            i += 1
            continue
        if arg[:1] in ("-", "+") and len(arg) > 1:
            if not arg[1:].isalpha():
                return None
            saw_c = saw_c or arg == "-c"
            # Each ``o``/``O`` letter in a cluster (``-eo pipefail``,
            # ``-oo pipefail errexit``) consumes its own value arg, exactly
            # as the shell does.
            i += 1 + sum(ch in "oO" for ch in arg[1:])
            continue
        break
    if saw_c and i < len(args):
        return args[i]
    return None


def _shell_reads_stdin(args: list[str]) -> bool:
    """Whether a shell invoked with *args* takes its script from stdin."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _STDIN_OPERANDS:
            return True
        if arg in _SHELL_VALUE_OPTIONS:
            i += 2
            continue
        if arg == "--":
            return i + 1 >= len(args) or args[i + 1] in _STDIN_OPERANDS
        if arg.startswith("--"):
            i += 1
            continue
        if arg[0] in "-+":
            flags = arg[1:]
            if "c" in flags:
                return False
            if "s" in flags:
                # ``-s``: later words are positional parameters, not a script.
                return True
            # Each ``o``/``O`` letter consumes its own value arg.
            i += 1 + sum(ch in "oO" for ch in flags)
            continue
        return False
    return True


def _is_stdin_shell(
    tokens: list[str], strip_redirections: bool, depth: int = 0,
) -> bool:
    """Whether any shell in a segment is fed its script on stdin.

    Every position is checked, so ``| sudo bash`` / ``| timeout 5 sh`` are
    caught; ``rg -t sh "git push"`` still has an operand after ``sh``.
    """
    if strip_redirections:
        tokens = _strip_redirections(tokens)
    tokens = _strip_command_prefix(tokens)
    for i, tok in enumerate(tokens):
        if tok.rsplit("/", 1)[-1] not in _SHELL_WRAPPERS:
            continue
        args = tokens[i + 1:]
        if _shell_reads_stdin(args):
            return True
        # A ``-c`` script inherits the pipe's stdin, so a shell inside it
        # (``| bash -c "sh -s"``) reads the piped script too.
        operand = _shell_script_operand(args)
        if operand is not None and depth < _MAX_UNWRAP_DEPTH:
            inner = _split_commands(operand)
            if inner is None or any(
                _is_stdin_shell(seg, strip_redirections, depth + 1)
                for seg in inner
            ):
                return True
    return False


def _evaluate_command(
    command: str,
    allowed_branches: frozenset[str] | None,
    depth: int,
    strip_redirections: bool = False,
    *,
    current_branch: str | None = None,
) -> str | None:
    """Depth-tracked core of :func:`evaluate_bash_command`.

    ``depth`` counts ``bash -c``/``sh -c`` unwrap levels; exceeding
    ``_MAX_UNWRAP_DEPTH`` fails closed rather than recursing further.  When
    *strip_redirections* is set each segment is passed through
    :func:`_strip_redirections` before dispatch, so output-capture idioms
    (``2>&1``, ``> out``) cannot leak stray tokens into a push's refspecs;
    default ``False`` leaves tokenization byte-identical to today.
    *current_branch* is threaded to :func:`_git_deny` so a bare ``HEAD`` /
    refspec-less push resolves to the run's branch; ``None`` keeps it
    fail-closed.
    """
    if depth > _MAX_UNWRAP_DEPTH:
        return _PARSE_DENY
    if not command or not command.strip():
        return None
    lowered = command.lower()
    # Whitespace-tolerant, so ``gh  pr`` cannot dodge the fail-closed arms.
    gated_text = _GATED_TEXT_RE.search(lowered) is not None
    # Heredoc bodies are stdin data: strip them before any quote-tracking
    # pass so a quote inside one cannot regroup the commands after it.
    # Substitutions bash expands inside an unquoted-delimiter body still run.
    nested: list[str] = []
    original = command
    top = _strip_heredoc_bodies(command)
    if top is None:
        if gated_text:
            return _PARSE_DENY
    else:
        command = top[0]
        nested.extend(top[1])
    segments = _split_commands(command)
    bodies = _substitution_bodies(
        _strip_comments(_join_line_continuations(command)),
    )
    if segments is None:
        return _PARSE_DENY if gated_text else None
    # A heredoc fed to a shell's stdin (``bash <<EOF``, ``cat <<EOF | sh``)
    # is a script, not data; its dropped body cannot be judged as stdin, so
    # fail closed when gated text is present.
    if gated_text and top is not None and top[0] != original and any(
        tok.rsplit("/", 1)[-1] in _SHELL_WRAPPERS and "-c" not in tokens
        for tokens in segments for tok in tokens
    ):
        return _PARSE_DENY
    # A shell led with no ``-c`` and no script operand (``echo "…" | sh``,
    # ``| bash -s``) reads its script from stdin, which cannot be judged.
    if gated_text and any(
        _is_stdin_shell(tokens, strip_redirections) for tokens in segments
    ):
        return _PARSE_DENY
    # An unbalanced substitution still leaves the earlier segments runnable
    # (bash executes them before the syntax error), so they are evaluated
    # below before the substring fallback applies.
    for body in bodies or []:
        # _match_close counts parens, so a ``case`` pattern's ``)`` can end
        # the span early and hide the rest of the body; fail closed there.
        if gated_text and re.search(r"\bcase\b", body):
            return _PARSE_DENY
        # A substitution body runs as its own command; its quoted-delimiter
        # heredoc bodies stay stdin data, while substitutions inside an
        # unquoted one still run and are evaluated too.
        stripped = _strip_heredoc_bodies(body)
        if stripped is None:
            bodies = None
            break
        nested.append(stripped[0])
        nested.extend(stripped[1])
    for body in nested:
        reason = _evaluate_command(
            body, allowed_branches, depth + 1,
            strip_redirections, current_branch=current_branch,
        )
        if reason is not None:
            return reason
    for tokens in segments:
        if strip_redirections:
            tokens = _strip_redirections(tokens)
        reason = _segment_deny(
            tokens, allowed_branches, depth, strip_redirections,
            current_branch=current_branch,
        )
        if reason is not None:
            return reason
    if bodies is None and gated_text:
        return _PARSE_DENY
    return None


def evaluate_bash_command(
    command: str,
    allowed_branches: frozenset[str] | None,
    *,
    strip_redirections: bool = False,
    current_branch: str | None = None,
    allowed_push_remotes: frozenset[str] | None = None,
) -> str | None:
    """Return a deny reason for *command*, or ``None`` when it is allowed.

    Pure, synchronous core of the guard.  ``allowed_branches`` is the set of
    branches the run may push to (each e.g. ``maestro/task/<id>`` or a
    ``maestro/evolve/*`` PR head); a falsy set — ``None`` or empty — denies
    every push fail-closed.  A push is allowed only when every destination ref
    is in the set and none is protected; a force-push to any branch is denied
    unconditionally.  A ``bash -c``/``sh -c`` wrapper is unwrapped and its inner
    command re-evaluated.  When *strip_redirections* is set (the opt-in proposal
    096 behavior) shell redirections are stripped from each segment before its
    refspecs are read, so an output-captured own-branch push
    (``git push origin maestro/evolve/<id> 2>&1 | tail -12``) is no longer
    mis-parsed and fail-closed; default ``False`` is byte-identical to today.

    ``current_branch`` (proposal 099) is the run's own checked-out branch,
    injected by the caller so the gate can resolve the two branch-name-less push
    shapes the model naturally writes — a bare ``HEAD`` destination and a
    refspec-less push — to that branch before the owned-set check.  Left ``None``
    (the branch-fallback path and every non-activated caller, e.g.
    :func:`permits_pr_close`) both shapes stay denied fail-closed, byte-identical
    to today; a resolved branch is always already in ``allowed_branches``, so
    this widens the gate's parsing, not its policy.  A multi-ref push flag
    (``--all`` / ``--mirror`` / ``--tags`` / ``--follow-tags``) is denied
    before that resolution, since it would push more than the single resolved
    branch.  Because ``current_branch`` is
    bound at run start and the gate cannot re-derive live HEAD, a deliberate
    checkout away from the owned branch before a bare ``HEAD`` / refspec-less
    push (``git checkout main && git push origin HEAD``) is an accepted
    out-of-scope limitation under the module's non-adversarial threat model —
    see :func:`_git_deny`.

    ``allowed_push_remotes`` scopes every push's remote to the run's own repo,
    so a scoped run cannot publish to a repo it was not given: when
    set, a push to a remote name or URL outside it, ``--repo``, a ``-c
    remote.*``/``url.*`` override, or rewriting a remote is refused
    ``push-remote-foreign``; ``None`` leaves remotes unchecked (the one
    unscoped change: ``--config-env`` is now consumed as a value-taking global
    flag, so ``git --config-env k=V push …`` is read as the push it is).
    """
    token = _ALLOWED_PUSH_REMOTES.set(allowed_push_remotes)
    try:
        return _evaluate_command(
            command, allowed_branches, 0, strip_redirections,
            current_branch=current_branch,
        )
    finally:
        _ALLOWED_PUSH_REMOTES.reset(token)


def permits_pr_close(command: str) -> bool:
    """Whether the action gate permits *command* as a reversible PR disposal.

    A pure, side-effect-free predicate over :func:`evaluate_bash_command`:
    ``True`` iff the gate does not deny *command*.  Its intended argument is a
    ``gh pr close <N>`` invocation — the reversible withdrawal of an owned,
    git-unlanded cure.  It exists to make the "won't vs can't" question a
    first-class *tested* fact rather than a guess: if this returns ``True`` for
    ``gh pr close``, then a never-invoked disposal record was always a choice
    (``won't``), never a gate block (``can't``).  ``allowed_branches`` is
    irrelevant to a ``gh pr`` verb (only ``git push`` consults it), so ``None``
    is passed; blocked bypasses (``merge``/``ready``/``open``/``reopen``) return
    ``False`` through the same classification.
    """
    return evaluate_bash_command(command, allowed_branches=None) is None


class TaskGateHook:
    """The ``PreToolUse`` hook callback gating the task-execution path.

    An async-callable class rather than a closure so the hook is *recognizable*
    and *inspectable*: the CLI-paced provider identifies the task action-gate
    inside a ``QueryOptions.hooks`` mapping by ``isinstance`` (via
    :func:`task_gate_from_hooks`) and reads the :attr:`allowed_branches` set
    off the instance to replicate the same gate into the spawned ``claude``
    subprocess (proposal 066/093) — one decision core, two enforcement paths.

    Calling it inspects ``Bash`` tool calls and returns a
    ``permissionDecision="deny"`` for PR-mutating / protected-ref-push commands
    (redirecting the model to the approval route).  Any non-deny case returns an
    empty dict, expressing no opinion so the CLI's normal flow proceeds.
    ``allowed_branches`` is the set of branches the run may push to; a push to
    any ref outside it, or a force-push to any branch, is denied.
    """

    def __init__(
        self,
        allowed_branches: frozenset[str] | None,
        *,
        strip_redirections: bool = False,
        current_branch: str | None = None,
        allowed_push_remotes: frozenset[str] | None = None,
    ) -> None:
        self.allowed_branches = allowed_branches
        #: The remotes a push may target; ``None`` leaves
        #: remotes unchecked.  Replicated across the subprocess seam.
        self.allowed_push_remotes = allowed_push_remotes
        #: When set, the gate strips shell redirections before reading a push's
        #: refspecs (proposal 096) — read off the instance by the CLI-paced
        #: provider to replicate the same behaviour across the subprocess seam,
        #: exactly as it reads :attr:`allowed_branches`.
        self.strip_redirections = strip_redirections
        #: The run's own checked-out branch (proposal 099), injected so the gate
        #: resolves a bare ``HEAD`` / refspec-less push to it before the
        #: owned-set check.  ``None`` keeps both shapes fail-closed.  Read off
        #: the instance by the CLI-paced provider to replicate across the
        #: subprocess seam, exactly as it reads :attr:`allowed_branches`.
        self.current_branch = current_branch

    @property
    def allowed_branch(self) -> str | None:
        """The sole owned branch, for the single-branch compat surface.

        The factory shims (:func:`make_task_pretooluse_hook` /
        :func:`make_task_tool_hooks`) build at most a one-element set this
        increment, so returning that element (or ``None`` for an absent/empty
        set) exactly reproduces the pre-set single-branch field its tests read
        (the CLI-paced provider now reads the full :attr:`allowed_branches` set
        directly).  A multi-element set — only reachable once a later increment
        injects a genuine owned set across the process boundary — returns
        ``None`` rather than an arbitrary element.
        """
        if self.allowed_branches and len(self.allowed_branches) == 1:
            return next(iter(self.allowed_branches))
        return None

    async def __call__(
        self,
        input_data: Any,
        tool_use_id: str | None,
        context: Any,
    ) -> HookJSONOutput:
        tool_name = ""
        command = ""
        if isinstance(input_data, dict):
            raw_name = input_data.get("tool_name", "")
            if isinstance(raw_name, str):
                tool_name = raw_name
            tool_input = input_data.get("tool_input")
            if isinstance(tool_input, dict):
                raw_command = tool_input.get("command", "")
                if isinstance(raw_command, str):
                    command = raw_command
        if not is_gate_matched(tool_name):
            return {}
        reason = evaluate_bash_command(
            command,
            self.allowed_branches,
            strip_redirections=self.strip_redirections,
            current_branch=self.current_branch,
            allowed_push_remotes=self.allowed_push_remotes,
        )
        if reason is None:
            return {}
        logger.warning(
            "tool_gating denied Bash command (branches=%s): %s",
            self.allowed_branches,
            command[:200],
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }


def make_task_pretooluse_hook(
    allowed_branches: frozenset[str] | None,
    *,
    strip_redirections: bool = False,
    current_branch: str | None = None,
    allowed_push_remotes: frozenset[str] | None = None,
) -> TaskGateHook:
    """Build the ``PreToolUse`` hook callback gating the task-execution path.

    Passes the caller's owned-writable *allowed_branches* set straight to a
    :class:`TaskGateHook` — an async callable satisfying the SDK's
    ``HookCallback`` contract carrying the :attr:`~TaskGateHook.allowed_branches`
    set the CLI-paced provider reads to replicate the gate across the subprocess
    boundary (proposal 066/093).  ``None`` (fail-closed) and the empty set both
    deny every push; the producer that computes a genuine multi-branch owned set
    lives in ``run()`` (proposal 093).  *strip_redirections* rides onto the hook
    so the same redirection-stripping posture (proposal 096) is enforced in
    process and replicated across the subprocess seam.  *current_branch*
    (proposal 099) rides on identically so a bare ``HEAD`` / refspec-less push
    resolves to the run's branch on both faces; ``None`` keeps it fail-closed.
    """
    return TaskGateHook(
        allowed_branches,
        strip_redirections=strip_redirections,
        current_branch=current_branch,
        allowed_push_remotes=allowed_push_remotes,
    )


def make_task_tool_hooks(
    allowed_branches: frozenset[str] | None,
    *,
    strip_redirections: bool = False,
    current_branch: str | None = None,
    allowed_push_remotes: frozenset[str] | None = None,
) -> dict[str, list[HookMatcher]]:
    """Build the ``hooks`` mapping for the task executor's ``QueryOptions``.

    Registers the :func:`make_task_pretooluse_hook` callback as a ``PreToolUse``
    hook scoped to the ``Bash`` tool, scoping it to the owned-writable
    *allowed_branches* set, the *strip_redirections* posture (proposal 096), and
    the run's *current_branch* (proposal 099).  Passed through
    ``QueryOptions.hooks`` to ``ClaudeAgentOptions.hooks``; fires regardless of
    ``permission_mode``.
    """
    return {
        "PreToolUse": [
            HookMatcher(
                matcher=gate_hook_matcher(),
                hooks=[
                    make_task_pretooluse_hook(
                        allowed_branches,
                        strip_redirections=strip_redirections,
                        current_branch=current_branch,
                        allowed_push_remotes=allowed_push_remotes,
                    ),
                ],
            ),
        ],
    }


def task_gate_from_hooks(hooks: dict[str, Any] | None) -> TaskGateHook | None:
    """Recognize the exact mapping :func:`make_task_tool_hooks` builds.

    Returns the embedded :class:`TaskGateHook` when *hooks* is precisely the
    task action-gate — one ``PreToolUse`` key, one
    :data:`~maestro.tool_surface.GATE_MATCHED_TOOLS`-scoped matcher, one
    :class:`TaskGateHook` callback — and ``None`` for anything else, including
    a mapping that *contains* the gate alongside other hooks.  The strictness
    is deliberate: the CLI-paced provider replicates a recognized gate into the
    spawned ``claude`` subprocess (proposal 066) and must fail loud on any hook
    payload it cannot replicate, never silently drop part of one.
    """
    if not isinstance(hooks, dict) or set(hooks) != {"PreToolUse"}:
        return None
    matchers = hooks["PreToolUse"]
    if not isinstance(matchers, list) or len(matchers) != 1:
        return None
    matcher = matchers[0]
    if not isinstance(matcher, HookMatcher) or matcher.matcher != gate_hook_matcher():
        return None
    callbacks = matcher.hooks
    if not isinstance(callbacks, list) or len(callbacks) != 1:
        return None
    callback = callbacks[0]
    if not isinstance(callback, TaskGateHook):
        return None
    return callback

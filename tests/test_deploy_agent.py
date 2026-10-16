"""Pin the load-bearing shape of the host-local pull-based deploy agent.

`scripts/deploy-agent.sh` runs on the production host and drives
`scripts/safe-deploy.sh` when a new ``production`` GitHub deployment appears. It
carries safe-deploy.sh's four-outcome exit-code contract (0 ok / 1 rolled back /
2 rollback-also-failed / 3 aborted-before-any-change) to a human through a
GitHub deployment-status stamp and a Telegram headline. Nobody watches the host
when it fires, so a silent regression in any of those guarantees would go
unnoticed — exactly the failure mode ``tests/test_deploy_failure_alerting.py``
guards for the runner-driven path.

These tests pin that shape with the ``_<thing>_report(injected) -> str``
detector house pattern the sibling workflow/deploy-shape modules
(``tests/test_workload_placement``) uses, and
the containment-assertion style of ``tests/test_deploy_failure_alerting``. Each
detector is pure over injected artifact text; a thin caller reads the committed
artifact through a real ``Path`` (like the sibling repo-tree guards), never
executing it — no real subprocess. ``tests/`` and ``deploy/`` are outside the
``maestro/``-only coverage gate, so every detector arm carries a seeded positive
**and** negative control, or an unexercised branch would be caught by nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "deploy-agent.sh"
_UNIT_DIR = _REPO_ROOT / "deploy" / "systemd" / "deploy-agent"
_SERVICE_PATH = _UNIT_DIR / "maestro-deploy-agent.service"
_TIMER_PATH = _UNIT_DIR / "maestro-deploy-agent.timer"

#: The three ``.env`` normalisations copied verbatim from the deploy workflow's
#: ``read_env``: drop a trailing CR, then strip one matched pair of surrounding
#: double / single quotes. Defined once so the detector and the seeded controls
#: cannot drift from each other.
_CR_STRIP = r"tr -d '\r'"
_DQUOTE_STRIP = r's/^"\(.*\)"$/\1/'
_SQUOTE_STRIP = r"s/^'\(.*\)'\$/\1/"


def _read(path: Path) -> str:
    """Read a committed artifact, failing loudly if it is missing or empty.

    The non-empty guard is the vacuity backstop: a truncated-to-zero artifact
    would otherwise let every containment detector below pass having asserted
    nothing.
    """
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{path} is empty"
    return text


# --- Detectors (pure over injected text) ------------------------------------


def _case_headlines(script: str) -> dict[str, str]:
    """Map each ``case "$rc"`` arm's exit code to the ``HEADLINE`` it assigns.

    Isolates the ``case "$rc" in … esac`` block first so stray ``digit)`` text
    elsewhere in the script cannot be mistaken for an arm, then reads each arm's
    ``HEADLINE="…"``. Arms with no ``HEADLINE`` are omitted.
    """
    block = re.search(r'case\s+"\$rc"\s+in(.*?)\besac\b', script, re.S)
    if block is None:
        return {}
    headlines: dict[str, str] = {}
    for arm in re.finditer(r"(?P<code>[0-9*]+)\)(?P<body>.*?);;", block.group(1), re.S):
        found = re.search(r'HEADLINE="(?P<headline>[^"]*)"', arm.group("body"))
        if found is not None:
            headlines[arm.group("code")] = found.group("headline")
    return headlines


def _headlines_report(script: str) -> str:
    """Return why the exit-code headlines are wrong, or ``""`` when they hold.

    Each of exit codes 1/2/3 must map to a distinct headline, with exit 2 the
    manual-intervention case and exit 3 the aborted-before-any-change case (so a
    disk-guard abort is never mislabelled "auto-rolled back").
    """
    headlines = _case_headlines(script)
    missing = [code for code in ("1", "2", "3") if code not in headlines]
    if missing:
        return (
            "deploy-agent exit-code headlines incomplete: "
            + "; ".join(f"exit code {code} has no HEADLINE arm" for code in missing)
        )
    problems: list[str] = []
    if "manual intervention" not in headlines["2"].lower():
        problems.append("exit 2 headline does not say 'manual intervention'")
    if "aborted before any change" not in headlines["3"].lower():
        problems.append("exit 3 headline does not say 'aborted before any change'")
    if len({headlines["1"], headlines["2"], headlines["3"]}) != 3:
        problems.append("exit 1/2/3 headlines are not mutually distinct")
    if problems:
        return "deploy-agent exit-code headlines wrong: " + "; ".join(problems)
    return ""


def _poll_and_compare_report(script: str) -> str:
    """Return why the poll/compare shape is wrong, or ``""`` when it holds.

    The agent must poll the newest ``production`` deployment, read the local head
    with ``git rev-parse HEAD``, and gate on a target-vs-local sha equality test
    (the no-op that makes a short polling interval safe).
    """
    problems: list[str] = []
    if "deployments?environment=production" not in script:
        problems.append("no production deployments poll")
    if "git rev-parse HEAD" not in script:
        problems.append("no `git rev-parse HEAD` read of the local head")
    both_orders = re.search(
        r'\[\s*"\$TARGET_SHA"\s*=\s*"\$LOCAL_SHA"\s*\]', script
    ) or re.search(r'\[\s*"\$LOCAL_SHA"\s*=\s*"\$TARGET_SHA"\s*\]', script)
    if both_orders is None:
        problems.append("no equality test comparing the target sha to the local head")
    if problems:
        return "deploy-agent poll/compare shape wrong: " + "; ".join(problems)
    return ""


def _origin_main_guard_report(script: str) -> str:
    """Return why the origin/main honesty guard is missing or racy, or ``""`` when it holds.

    ``safe-deploy.sh`` lands origin/main's tip, not the deployment record's own
    sha, so the agent must read origin/main's tip and refuse to drive a deploy
    whose target sha is not that tip. Without it a stale record (a dispatch
    redeploy of an older commit, or main racing ahead) makes safe-deploy.sh land
    the wrong commit and — since HEAD can never reach the record sha — redeploy
    forever.

    The tip must be read *race-free*. ``docker-compose.yml`` bind-mounts this repo
    into the still-running loop container (``.:/workspace``), which issues its own
    ``git fetch origin …`` calls; each rewrites the single process-global
    ``FETCH_HEAD`` ref — the exact hazard ``safe-deploy.sh`` documents under issue
    #2814. A loop fetch landing between the agent's own fetch and its ``git
    rev-parse FETCH_HEAD`` would leave the read holding whatever the loop last
    fetched, silently defeating the guard. The agent must instead query the remote
    directly with ``git ls-remote``, which prints the tip to its own stdout and
    never touches the shared ``FETCH_HEAD`` ref.
    """
    # Read the tip check against code only: prose may legitimately name both
    # `git ls-remote` and `FETCH_HEAD` (explaining why the latter is avoided), so a
    # whole-text scan would false-match a comment. Drop full-line ``#`` comments.
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    problems: list[str] = []
    if re.search(r"git ls-remote\b.*\borigin\b.*\bmain\b", code) is None:
        problems.append("no race-free `git ls-remote origin … main` read of origin/main's tip")
    if "FETCH_HEAD" in code:
        problems.append(
            "reads origin/main's tip from the process-global FETCH_HEAD ref, which the "
            "co-located loop container concurrently rewrites (issue #2814) — use `git ls-remote`"
        )
    guard = re.search(
        r'\[\s*"\$TARGET_SHA"\s*!=\s*"\$[A-Z_]*SHA"\s*\]', code
    ) or re.search(r'\[\s*"\$[A-Z_]*SHA"\s*!=\s*"\$TARGET_SHA"\s*\]', code)
    if guard is None:
        problems.append("no target-vs-origin/main inequality skip guard")
    if problems:
        return "deploy-agent origin/main honesty guard wrong: " + "; ".join(problems)
    return ""


def _attempt_memory_report(script: str) -> str:
    """Return why per-deployment attempt memory is missing, or ``""`` when it holds.

    A failed deploy leaves HEAD short of the target, so without memory the naive
    HEAD-vs-target no-op re-drives the same deployment every tick — a rebuild +
    smoke + loop restart + alert storm. The agent must persist the last
    deployment id it drove (to a marker file it reads back) and skip a deployment
    id it has already attempted.
    """
    problems: list[str] = []
    # A marker file, named with ATTEMPT, that the agent writes via redirect.
    if re.search(r'>\s*"\$\{?[A-Za-z_]*ATTEMPT[A-Za-z_]*', script) is None:
        problems.append("no attempt-marker file write")
    # Keyed on the deployment id: a skip when it equals the last-attempted value.
    if re.search(r'\[\s*"\$DEPLOYMENT_ID"\s*=\s*"\$[A-Za-z_]+"\s*\]', script) is None:
        problems.append("no already-attempted deployment-id skip guard")
    if problems:
        return "deploy-agent attempt-memory shape wrong: " + "; ".join(problems)
    return ""


def _exit3_retry_report(script: str) -> str:
    """Return why the exit-3 arm never retries, or ``""`` when it clears the marker.

    The attempt marker is written *before* ``safe-deploy.sh`` runs, so the
    deployment id is recorded as attempted regardless of outcome. For exit 1/2
    (deploy failed / rollback also failed) that non-retry is correct. But exit 3
    is ``safe-deploy.sh``'s *transient* abort-before-any-change (e.g. low disk:
    nothing deployed, no rollback, loop left running, guidance "Free disk and
    re-run"). Unless the exit-3 arm clears the marker, that deployment id stays
    permanently attempted and a gate-passed deploy silently never lands once the
    operator frees the resource — the one retryable outcome wrongly lumped with
    the hard failures. The exit-3 arm must remove the attempt-marker file.
    """
    block = re.search(r'case\s+"\$rc"\s+in(.*?)\besac\b', script, re.S)
    if block is None:
        return 'deploy-agent has no `case "$rc"` block'
    arm_body: str | None = None
    for arm in re.finditer(r"(?P<code>[0-9*]+)\)(?P<body>.*?);;", block.group(1), re.S):
        if arm.group("code") == "3":
            arm_body = arm.group("body")
            break
    if arm_body is None:
        return "deploy-agent has no exit-3 arm to clear the attempt marker"
    if re.search(r"\brm\b[^\n]*ATTEMPT", arm_body) is None:
        return (
            "deploy-agent exit-3 arm does not clear the attempt marker: a transient "
            "abort-before-any-change (safe-deploy.sh: 'Free disk and re-run') stays "
            "permanently attempted and never retries"
        )
    return ""


def _telegram_curl_report(script: str) -> str:
    """Return why the Telegram send is unsafe, or ``""`` when it holds.

    The send must use ``curl`` with ``--fail``/``-f`` against the Telegram
    ``sendMessage`` API, so an HTTP-level rejection (bad token, wrong chat) is a
    non-zero exit the warning fallback can catch rather than a silent success —
    the same rationale as ``test_deploy_failure_alerting``.
    """
    send_line = next(
        (line for line in script.splitlines() if line.strip().startswith("curl ")),
        None,
    )
    if send_line is None:
        return "deploy-agent has no curl invocation for the Telegram send"
    if "sendMessage" not in script:
        return "deploy-agent curl does not target the Telegram sendMessage API"
    has_fail = "--fail" in send_line or bool(re.search(r"(?<!\S)-[a-zA-Z]*f", send_line))
    if not has_fail:
        return f"deploy-agent Telegram curl must use --fail/-f: {send_line!r}"
    return ""


def _env_read_report(script: str) -> str:
    """Return why the ``.env`` reader is wrong, or ``""`` when it holds.

    A quoted or CRLF-terminated ``.env`` must still yield a usable token, so the
    reader strips a trailing CR and one pair of surrounding double/single quotes.
    """
    problems: list[str] = []
    if _CR_STRIP not in script:
        problems.append("no CR strip (tr -d '\\r')")
    if _DQUOTE_STRIP not in script:
        problems.append("no surrounding double-quote strip")
    if _SQUOTE_STRIP not in script:
        problems.append("no surrounding single-quote strip")
    if problems:
        return "deploy-agent .env reader shape wrong: " + "; ".join(problems)
    return ""


def _deployment_status_states_report(script: str) -> str:
    """Return why the deployment-status states are wrong, or ``""`` when they hold.

    The agent posts to the deployment ``…/statuses`` endpoint and assigns all
    three GitHub states it maps exit codes onto: ``success`` / ``failure`` /
    ``error``.
    """
    problems: list[str] = []
    if "/statuses" not in script:
        problems.append("no deployment-status POST endpoint (…/statuses)")
    for state in ("success", "failure", "error"):
        if f'STATE="{state}"' not in script:
            problems.append(f'no STATE="{state}" arm')
    if problems:
        return "deploy-agent deployment-status states wrong: " + "; ".join(problems)
    return ""


def _single_flight_report(script: str) -> str:
    """Return why single-flight is missing, or ``""`` when a ``flock`` guard exists.

    Overlapping timer activations must not race two deploys against one Docker
    daemon; a ``flock`` guard makes the second run a no-op.
    """
    if "flock" not in script:
        return "deploy-agent has no flock single-flight guard"
    return ""


def _multiline_functions(script: str) -> list[tuple[str, int, int]]:
    """Return ``(name, start, end)`` char spans of each multi-line ``name() { … }``.

    House bash style opens a multi-line function with ``name() {`` on its own line
    and closes it with a bare ``}`` at column 0; one-liner ``name() { …; }``
    functions (open and close on the same line, so the line does not *end* in
    ``{``) are not multi-line bodies and are skipped. ``case``/``if`` blocks and
    ``${…}`` expansions never put a ``}`` at line start, so the first such line
    after an opener is that function's close.
    """
    functions: list[tuple[str, int, int]] = []
    opener = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{[ \t]*$", re.M)
    close = re.compile(r"^\}[ \t]*$", re.M)
    for match in opener.finditer(script):
        end = close.search(script, match.end())
        if end is None:
            continue
        functions.append((match.group(1), match.start(), end.end()))
    return functions


def _reexec_safety_report(script: str) -> str:
    """Return why the post-deploy tail is exposed to the running-script mutation
    hazard, or ``""`` when it is pre-parsed into a function.

    ``safe-deploy.sh`` does ``git reset --hard`` + ``git clean -fd``, so a deploy
    that lands a change to ``scripts/deploy-agent.sh`` replaces this file on disk
    while the agent is still running from it. Bash reads scripts incrementally by
    byte offset, so any post-deploy logic re-read from disk *after* ``bash
    scripts/safe-deploy.sh`` returns could be stale or garbled — the identical
    hazard ``safe-deploy.sh`` guards against for itself via its ``DEPLOY_REEXEC``
    re-exec. The agent must therefore run the deploy and map/report its outcome
    from inside a *function*: a bash function body is parsed in full at definition
    time (before any deploy) and thereafter executed from memory, so the exit-code
    capture, headline mapping and final ``exit`` cannot be corrupted mid-run. This
    detector requires the ``bash scripts/safe-deploy.sh`` invocation, the ``case
    "$rc"`` mapping and the final ``exit "$rc"`` to all live inside one function
    body that the top-level flow then calls.
    """
    deploy = re.search(r"bash\s+scripts/safe-deploy\.sh", script)
    if deploy is None:
        return "deploy-agent never invokes `bash scripts/safe-deploy.sh`"
    enclosing = [
        (name, start, end)
        for (name, start, end) in _multiline_functions(script)
        if start <= deploy.start() and deploy.end() <= end
    ]
    if not enclosing:
        return (
            "deploy-agent runs `bash scripts/safe-deploy.sh` at top-level script scope: "
            "safe-deploy.sh's `git reset --hard` + `git clean -fd` rewrites this file on "
            "disk mid-run, so the post-deploy tail read from disk afterward may be garbled "
            "— wrap the run and its outcome reporting in a function so bash pre-parses the "
            "tail into memory (as safe-deploy.sh does with its DEPLOY_REEXEC re-exec)"
        )
    name, start, end = enclosing[0]
    body = script[start:end]
    problems: list[str] = []
    if re.search(r'case\s+"\$rc"\s+in', body) is None:
        problems.append('the `case "$rc"` exit-code mapping is not inside the deploy function')
    if re.search(r'\bexit\s+"?\$rc"?', body) is None:
        problems.append('the final `exit "$rc"` is not inside the deploy function')
    # The wrapping is only load-bearing if the top-level flow actually calls it.
    call = re.search(rf"^{re.escape(name)}[ \t]*$", script, re.M)
    called_outside = call is not None and not (start <= call.start() < end)
    if not called_outside:
        problems.append(f"the deploy function `{name}` is never called from top-level flow")
    if problems:
        return "deploy-agent post-deploy tail is not fully pre-parsed: " + "; ".join(problems)
    return ""


def _service_report(service_text: str) -> str:
    """Return why the ``.service`` unit is wrong, or ``""`` when it holds.

    The unit must be ``Type=oneshot`` and its ``ExecStart`` must run
    ``deploy-agent.sh``.
    """
    problems: list[str] = []
    if "Type=oneshot" not in service_text:
        problems.append("service is not Type=oneshot")
    if re.search(r"ExecStart=.*deploy-agent\.sh", service_text) is None:
        problems.append("ExecStart does not name deploy-agent.sh")
    if problems:
        return "deploy-agent .service shape wrong: " + "; ".join(problems)
    return ""


def _timer_report(timer_text: str) -> str:
    """Return why the ``.timer`` unit is wrong, or ``""`` when it holds.

    The timer must declare a schedule (``OnBootSec`` / ``OnUnitActiveSec`` /
    ``OnCalendar``) and an ``[Install]`` section with a ``WantedBy=`` so it can be
    enabled.
    """
    problems: list[str] = []
    if re.search(r"On(BootSec|UnitActiveSec|Calendar)=", timer_text) is None:
        problems.append("no schedule (OnBootSec/OnUnitActiveSec/OnCalendar)")
    if re.search(r"\[Install\]", timer_text) is None:
        problems.append("no [Install] section")
    if "WantedBy=" not in timer_text:
        problems.append("no [Install] WantedBy=")
    if problems:
        return "deploy-agent .timer shape wrong: " + "; ".join(problems)
    return ""


# --- Real-tree callers ------------------------------------------------------


def test_exit_code_headlines_are_distinct_and_labelled() -> None:
    """Exit codes 1/2/3 each map to a distinct, correctly-labelled headline."""
    assert _headlines_report(_read(_SCRIPT_PATH)) == ""


def test_polls_production_and_compares_local_head() -> None:
    """The agent polls the production deployment and compares against HEAD."""
    assert _poll_and_compare_report(_read(_SCRIPT_PATH)) == ""


def test_only_deploys_when_record_is_origin_main_tip() -> None:
    """The agent refuses to drive a deploy whose sha is not origin/main's tip."""
    assert _origin_main_guard_report(_read(_SCRIPT_PATH)) == ""


def test_remembers_attempted_deployments() -> None:
    """The agent records the deployment it drove and will not re-drive it."""
    assert _attempt_memory_report(_read(_SCRIPT_PATH)) == ""


def test_exit3_clears_attempt_marker_for_retry() -> None:
    """A transient exit-3 abort clears the attempt marker so it can retry."""
    assert _exit3_retry_report(_read(_SCRIPT_PATH)) == ""


def test_telegram_curl_fails_on_http_error() -> None:
    """The Telegram send uses curl --fail so an API rejection is not silent."""
    assert _telegram_curl_report(_read(_SCRIPT_PATH)) == ""


def test_env_read_strips_quotes_and_cr() -> None:
    """The .env reader tolerates quoted values and CRLF line endings."""
    assert _env_read_report(_read(_SCRIPT_PATH)) == ""


def test_deployment_status_posts_all_three_states() -> None:
    """The status update posts success/failure/error to …/statuses."""
    assert _deployment_status_states_report(_read(_SCRIPT_PATH)) == ""


def test_single_flight_guard_present() -> None:
    """A flock single-flight guard prevents overlapping deploys."""
    assert _single_flight_report(_read(_SCRIPT_PATH)) == ""


def test_post_deploy_tail_is_pre_parsed_into_a_function() -> None:
    """The deploy run + outcome report is wrapped in a function, safe from the
    running-script mutation safe-deploy.sh performs (git reset --hard)."""
    assert _reexec_safety_report(_read(_SCRIPT_PATH)) == ""


def test_service_is_oneshot_and_runs_the_script() -> None:
    """The service is Type=oneshot and its ExecStart runs the agent script."""
    assert _service_report(_read(_SERVICE_PATH)) == ""


def test_timer_declares_schedule_and_install() -> None:
    """The timer declares a schedule and an [Install] WantedBy."""
    assert _timer_report(_read(_TIMER_PATH)) == ""


# --- Seeded controls: exit-code headlines -----------------------------------

_GOOD_CASE = """
case "$rc" in
  1)
    STATE="failure"
    HEADLINE="🚨 Deploy failed (auto-rolled back)"
    ;;
  2)
    STATE="error"
    HEADLINE="🚨🚨 Deploy FAILED and ROLLBACK FAILED — manual intervention required"
    ;;
  3)
    STATE="error"
    HEADLINE="⚠️ Deploy aborted before any change (loop left running) — nothing deployed"
    ;;
esac
"""


def test_headlines_report_accepts_a_well_formed_case() -> None:
    """Positive control: distinct, correctly-labelled headlines yield no report."""
    assert _headlines_report(_GOOD_CASE) == ""


def test_headlines_report_fires_on_a_missing_arm() -> None:
    """Negative control: an exit code with no HEADLINE arm is named."""
    bad = _GOOD_CASE.replace(
        '  2)\n    STATE="error"\n'
        '    HEADLINE="🚨🚨 Deploy FAILED and ROLLBACK FAILED — manual intervention required"\n'
        "    ;;\n",
        "",
    )
    report = _headlines_report(bad)
    assert report != ""
    assert "exit code 2" in report


def test_headlines_report_fires_when_exit_2_not_manual_intervention() -> None:
    """Negative control: exit 2 not labelled manual-intervention reds."""
    bad = _GOOD_CASE.replace("manual intervention required", "something else")
    report = _headlines_report(bad)
    assert report != ""
    assert "manual intervention" in report


def test_headlines_report_fires_when_exit_3_not_aborted() -> None:
    """Negative control: exit 3 mislabelled as a rollback reds (and is not distinct)."""
    bad = _GOOD_CASE.replace(
        "⚠️ Deploy aborted before any change (loop left running) — nothing deployed",
        "🚨 Deploy failed (auto-rolled back)",
    )
    report = _headlines_report(bad)
    assert report != ""
    assert "aborted before any change" in report


# --- Seeded controls: poll + compare ----------------------------------------

_GOOD_POLL = """
poll_output="$(gh api "repos/{owner}/{repo}/deployments?environment=production&per_page=1")"
LOCAL_SHA="$(git rev-parse HEAD)"
if [ "$TARGET_SHA" = "$LOCAL_SHA" ]; then
  exit 0
fi
"""


def test_poll_and_compare_report_accepts_a_well_formed_snippet() -> None:
    """Positive control: poll + rev-parse + equality test yield no report."""
    assert _poll_and_compare_report(_GOOD_POLL) == ""


def test_poll_and_compare_report_fires_without_the_poll() -> None:
    """Negative control: a non-production poll is named."""
    bad = _GOOD_POLL.replace("environment=production", "environment=staging")
    report = _poll_and_compare_report(bad)
    assert "production deployments poll" in report


def test_poll_and_compare_report_fires_without_rev_parse() -> None:
    """Negative control: a missing local-head read is named."""
    bad = _GOOD_POLL.replace("git rev-parse HEAD", "echo nope")
    report = _poll_and_compare_report(bad)
    assert "git rev-parse HEAD" in report


def test_poll_and_compare_report_fires_without_the_comparison() -> None:
    """Negative control: a missing target-vs-local equality test is named."""
    bad = _GOOD_POLL.replace('[ "$TARGET_SHA" = "$LOCAL_SHA" ]', '[ -n "$TARGET_SHA" ]')
    report = _poll_and_compare_report(bad)
    assert "target sha to the local head" in report


# --- Seeded controls: origin/main honesty guard -----------------------------

_GOOD_ORIGIN_GUARD = """
ORIGIN_MAIN_SHA="$(git ls-remote origin refs/heads/main | cut -f1)"
if [ "$TARGET_SHA" != "$ORIGIN_MAIN_SHA" ]; then
  exit 0
fi
"""

#: The pre-fix shape: the racy `git fetch` + `git rev-parse FETCH_HEAD` read the
#: co-located loop container can silently defeat by rewriting the shared
#: FETCH_HEAD ref between the fetch and the read (issue #2814). Seeded so the
#: detector proves it reds on the exact defect the ls-remote read removes.
_RACY_ORIGIN_GUARD = """
git fetch --quiet origin main
ORIGIN_MAIN_SHA="$(git rev-parse FETCH_HEAD)"
if [ "$TARGET_SHA" != "$ORIGIN_MAIN_SHA" ]; then
  exit 0
fi
"""


def test_origin_main_guard_report_accepts_a_race_free_snippet() -> None:
    """Positive control: an ls-remote tip read + inequality skip yield no report."""
    assert _origin_main_guard_report(_GOOD_ORIGIN_GUARD) == ""


def test_origin_main_guard_report_fires_without_the_ls_remote_read() -> None:
    """Negative control: a missing race-free `git ls-remote` tip read is named."""
    bad = _GOOD_ORIGIN_GUARD.replace(
        'ORIGIN_MAIN_SHA="$(git ls-remote origin refs/heads/main | cut -f1)"',
        'ORIGIN_MAIN_SHA="$(echo skip)"',
    )
    report = _origin_main_guard_report(bad)
    assert "ls-remote" in report


def test_origin_main_guard_report_fires_on_a_fetch_head_read() -> None:
    """Negative control: the pre-fix shape reading the loop-rewritten FETCH_HEAD reds.

    This seeds the exact concurrency defect the fix removes — reading origin/main's
    tip from the process-global FETCH_HEAD ref the co-located loop container
    rewrites (issue #2814) — so the race-free ls-remote read cannot silently
    regress back to a FETCH_HEAD read.
    """
    report = _origin_main_guard_report(_RACY_ORIGIN_GUARD)
    assert report != ""
    assert "FETCH_HEAD" in report


def test_origin_main_guard_report_fires_without_the_inequality_guard() -> None:
    """Negative control: a missing target-vs-origin/main skip guard is named."""
    bad = _GOOD_ORIGIN_GUARD.replace(
        '[ "$TARGET_SHA" != "$ORIGIN_MAIN_SHA" ]', '[ -n "$TARGET_SHA" ]'
    )
    report = _origin_main_guard_report(bad)
    assert "inequality skip guard" in report


# --- Seeded controls: per-deployment attempt memory -------------------------

_GOOD_ATTEMPT = """
ATTEMPT_FILE="${TMPDIR:-/tmp}/maestro-deploy-agent.last-attempt"
LAST_ATTEMPT=""
if [ -f "$ATTEMPT_FILE" ]; then
  LAST_ATTEMPT="$(tr -d '[:space:]' < "$ATTEMPT_FILE")"
fi
if [ "$DEPLOYMENT_ID" = "$LAST_ATTEMPT" ]; then
  exit 0
fi
printf '%s\\n' "$DEPLOYMENT_ID" > "$ATTEMPT_FILE"
"""


def test_attempt_memory_report_accepts_a_well_formed_snippet() -> None:
    """Positive control: a marker write + deployment-id skip yield no report."""
    assert _attempt_memory_report(_GOOD_ATTEMPT) == ""


def test_attempt_memory_report_fires_without_the_marker_write() -> None:
    """Negative control: a reader with no marker write is named."""
    bad = _GOOD_ATTEMPT.replace('printf \'%s\\n\' "$DEPLOYMENT_ID" > "$ATTEMPT_FILE"', "true")
    report = _attempt_memory_report(bad)
    assert "attempt-marker file write" in report


def test_attempt_memory_report_fires_without_the_deployment_id_guard() -> None:
    """Negative control: a skip not keyed on the deployment id is named."""
    bad = _GOOD_ATTEMPT.replace(
        '[ "$DEPLOYMENT_ID" = "$LAST_ATTEMPT" ]', '[ -n "$LAST_ATTEMPT" ]'
    )
    report = _attempt_memory_report(bad)
    assert "already-attempted deployment-id skip guard" in report


# --- Seeded controls: exit-3 transient-abort retry --------------------------

_GOOD_RETRY_CASE = """
case "$rc" in
  1)
    STATE="failure"
    HEADLINE="🚨 Deploy failed (auto-rolled back)"
    ;;
  3)
    STATE="error"
    HEADLINE="⚠️ Deploy aborted before any change (loop left running)"
    rm -f "$ATTEMPT_FILE"
    ;;
esac
"""


def test_exit3_retry_report_accepts_a_marker_clearing_arm() -> None:
    """Positive control: an exit-3 arm that clears the marker yields no report."""
    assert _exit3_retry_report(_GOOD_RETRY_CASE) == ""


def test_exit3_retry_report_fires_without_the_marker_clear() -> None:
    """Negative control: an exit-3 arm that never clears the marker reds."""
    bad = _GOOD_RETRY_CASE.replace('    rm -f "$ATTEMPT_FILE"\n', "")
    report = _exit3_retry_report(bad)
    assert report != ""
    assert "never retries" in report


def test_exit3_retry_report_fires_without_an_exit3_arm() -> None:
    """Negative control: a case block with no exit-3 arm at all is named."""
    bad = _GOOD_RETRY_CASE.replace(
        '  3)\n    STATE="error"\n'
        '    HEADLINE="⚠️ Deploy aborted before any change (loop left running)"\n'
        '    rm -f "$ATTEMPT_FILE"\n'
        "    ;;\n",
        "",
    )
    report = _exit3_retry_report(bad)
    assert report != ""
    assert "no exit-3 arm" in report


# --- Seeded controls: Telegram curl -----------------------------------------

_GOOD_CURL = (
    "send() {\n"
    "  curl -fsS --max-time 20 \\\n"
    '    "https://api.telegram.org/bot$TOKEN/sendMessage" \\\n'
    '    --data-urlencode "text=$1"\n'
    "}\n"
)


def test_telegram_curl_report_accepts_a_failing_curl() -> None:
    """Positive control: a curl --fail to sendMessage yields no report."""
    assert _telegram_curl_report(_GOOD_CURL) == ""


def test_telegram_curl_report_fires_without_any_curl() -> None:
    """Negative control: no curl invocation at all is named."""
    report = _telegram_curl_report("echo hello\n")
    assert "no curl invocation" in report


def test_telegram_curl_report_fires_without_sendmessage() -> None:
    """Negative control: a curl not targeting sendMessage is named."""
    report = _telegram_curl_report('curl -fsS "https://example.com/x"\n')
    assert "sendMessage" in report


def test_telegram_curl_report_fires_without_fail_flag() -> None:
    """Negative control: a curl lacking --fail/-f reds."""
    bad = _GOOD_CURL.replace("curl -fsS", "curl -sS")
    report = _telegram_curl_report(bad)
    assert "--fail/-f" in report


# --- Seeded controls: .env reader -------------------------------------------

_GOOD_ENV = f"{_CR_STRIP} | sed -e '{_DQUOTE_STRIP}' -e \"{_SQUOTE_STRIP}\"\n"


def test_env_read_report_accepts_all_three_strips() -> None:
    """Positive control: CR + double/single quote strips yield no report."""
    assert _env_read_report(_GOOD_ENV) == ""


def test_env_read_report_fires_without_cr_strip() -> None:
    """Negative control: a reader missing the CR strip reds."""
    report = _env_read_report(f"sed -e '{_DQUOTE_STRIP}' -e \"{_SQUOTE_STRIP}\"\n")
    assert "CR strip" in report


def test_env_read_report_fires_without_quote_strips() -> None:
    """Negative control: a reader missing both quote strips names both."""
    report = _env_read_report(f"{_CR_STRIP}\n")
    assert "double-quote strip" in report
    assert "single-quote strip" in report


# --- Seeded controls: deployment-status states ------------------------------

_GOOD_STATUS = (
    'gh api "repos/{owner}/{repo}/deployments/$ID/statuses" -f "state=$s"\n'
    'STATE="success"\n'
    'STATE="failure"\n'
    'STATE="error"\n'
)


def test_status_states_report_accepts_all_three_states() -> None:
    """Positive control: a /statuses post with all three states yields no report."""
    assert _deployment_status_states_report(_GOOD_STATUS) == ""


def test_status_states_report_fires_without_a_state() -> None:
    """Negative control: a missing state is named."""
    bad = _GOOD_STATUS.replace('STATE="error"', 'STATE="broken"')
    report = _deployment_status_states_report(bad)
    assert 'STATE="error"' in report


def test_status_states_report_fires_without_the_statuses_endpoint() -> None:
    """Negative control: a missing …/statuses endpoint is named."""
    bad = _GOOD_STATUS.replace("/statuses", "/nope")
    report = _deployment_status_states_report(bad)
    assert "statuses" in report


# --- Seeded controls: single-flight guard -----------------------------------


def test_single_flight_report_accepts_a_flock_guard() -> None:
    """Positive control: a flock guard yields no report."""
    assert _single_flight_report('exec 9>"$LOCK"\nflock -n 9 || exit 0\n') == ""


def test_single_flight_report_fires_without_flock() -> None:
    """Negative control: a script with no flock guard reds."""
    report = _single_flight_report("echo no lock here\n")
    assert "flock" in report


# --- Seeded controls: post-deploy re-exec safety ----------------------------

_GOOD_REEXEC = """
post_status() {
  echo "$1"
}

run_deploy_and_report() {
  local rc STATE
  post_status "in_progress" "running"
  set +e
  bash scripts/safe-deploy.sh
  rc=$?
  set -e
  case "$rc" in
    0)
      STATE="success"
      ;;
  esac
  post_status "$STATE"
  exit "$rc"
}

run_deploy_and_report
"""


def test_reexec_safety_report_accepts_a_wrapped_tail() -> None:
    """Positive control: the deploy + report wrapped in a called function passes."""
    assert _reexec_safety_report(_GOOD_REEXEC) == ""


def test_reexec_safety_report_fires_on_a_top_level_tail() -> None:
    """Negative control: running the deploy inline at top-level scope reds."""
    bad = """
post_status() {
  echo "$1"
}

set +e
bash scripts/safe-deploy.sh
rc=$?
set -e
case "$rc" in
  0)
    STATE="success"
    ;;
esac
post_status "$STATE"
exit "$rc"
"""
    report = _reexec_safety_report(bad)
    assert report != ""
    assert "top-level script scope" in report


def test_reexec_safety_report_fires_when_mapping_escapes_the_function() -> None:
    """Negative control: a wrap that leaves the case/exit tail outside it reds."""
    bad = """
run_deploy_and_report() {
  set +e
  bash scripts/safe-deploy.sh
  rc=$?
  set -e
}

run_deploy_and_report
case "$rc" in
  0)
    STATE="success"
    ;;
esac
exit "$rc"
"""
    report = _reexec_safety_report(bad)
    assert report != ""
    assert "case" in report
    assert "exit" in report


def test_reexec_safety_report_fires_when_the_function_is_never_called() -> None:
    """Negative control: a fully-wrapped tail that is never invoked reds."""
    bad = """
run_deploy_and_report() {
  set +e
  bash scripts/safe-deploy.sh
  rc=$?
  set -e
  case "$rc" in
    0)
      STATE="success"
      ;;
  esac
  exit "$rc"
}
"""
    report = _reexec_safety_report(bad)
    assert report != ""
    assert "never called" in report


# --- Seeded controls: systemd units -----------------------------------------

_GOOD_SERVICE = "[Service]\nType=oneshot\nExecStart=/opt/maestro/scripts/deploy-agent.sh\n"
_GOOD_TIMER = "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=2min\n\n[Install]\nWantedBy=timers.target\n"


def test_service_report_accepts_a_well_formed_unit() -> None:
    """Positive control: a oneshot unit running the script yields no report."""
    assert _service_report(_GOOD_SERVICE) == ""


def test_service_report_fires_without_oneshot() -> None:
    """Negative control: a non-oneshot service reds."""
    report = _service_report(_GOOD_SERVICE.replace("Type=oneshot", "Type=simple"))
    assert "oneshot" in report


def test_service_report_fires_without_the_script_execstart() -> None:
    """Negative control: an ExecStart not naming the agent script reds."""
    report = _service_report(
        _GOOD_SERVICE.replace("ExecStart=/opt/maestro/scripts/deploy-agent.sh", "ExecStart=/bin/true")
    )
    assert "ExecStart" in report


def test_timer_report_accepts_a_well_formed_unit() -> None:
    """Positive control: a scheduled timer with [Install] WantedBy yields no report."""
    assert _timer_report(_GOOD_TIMER) == ""


def test_timer_report_fires_without_a_schedule() -> None:
    """Negative control: a timer with no schedule reds."""
    bad = _GOOD_TIMER.replace("OnBootSec=2min\n", "").replace("OnUnitActiveSec=2min\n", "")
    report = _timer_report(bad)
    assert "schedule" in report


def test_timer_report_fires_without_install_wantedby() -> None:
    """Negative control: a timer with no [Install] WantedBy reds."""
    report = _timer_report("[Timer]\nOnBootSec=2min\n")
    assert report != ""
    assert "WantedBy" in report

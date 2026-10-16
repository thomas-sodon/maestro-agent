"""Tests for scripts/safe-deploy.sh disk reclaim + pre-deploy space guard.

Issue #3632: the deploy pipeline built a fresh loop image on every deploy but
never reclaimed what it left behind, so the host root filesystem filled
monotonically until a deploy died on ``No space left on device`` — and because
the deploy gracefully stops the loop *before* the build, a full-disk deploy left
the loop ``Exited (0)`` with no room to bring it back up.

Two independent defects drove it, and these structural assertions (read the
committed script text, no subprocess, no real host state) pin both fixes:

1. ``cleanup()`` untagged the prior rollback image but never reclaimed the
   dangling layers or stale build cache — no ``docker image prune`` /
   ``docker builder prune``.
2. No pre-deploy disk-space guard existed, so a deploy started against an
   already-near-full disk stopped the loop and only then died at the build /
   ``docker compose up -d``, stranding the loop down.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "safe-deploy.sh"

# The graceful loop stop the disk-space preflight must precede.
_LOOP_STOP_LINE = "docker compose stop -t 1800 loop"

# The pre-deploy notify the disk-space preflight must also precede. Anchor on
# the full invocation line, not the bare `maestro notify-pre-deploy` command
# name, so the guard comment's prose mention of the command cannot false-match.
_PRE_DEPLOY_NOTIFY_LINE = "docker compose exec -T loop maestro notify-pre-deploy"


def _cleanup_body(text: str) -> str:
    """Return the body of the ``cleanup()`` function from safe-deploy.sh.

    Anchored on ``cleanup() {`` through the first column-0 ``}`` — cleanup has
    no nested braces, so this isolates exactly the hook that runs on a
    successful deploy and a successful rollback.
    """
    match = re.search(r"\ncleanup\(\) \{\n(.*?)\n\}\n", text, re.DOTALL)
    assert match is not None, (
        "could not isolate the cleanup() function body in safe-deploy.sh"
    )
    return match.group(1)


def _preflight_region(text: str) -> str:
    """Return the script text from the ``df`` free-space read to the loop stop.

    The disk-space preflight (#3632) must read free space with ``df`` and appear
    *before* the graceful loop stop. Anchoring the region on the ``df`` read and
    bounding it at the loop-stop line means every assertion below is confined to
    the preflight — an unrelated ``fail``/``exit`` elsewhere (e.g. rollback())
    cannot satisfy it, and a preflight that moved *after* the stop leaves no
    ``df`` read before it, failing here.
    """
    stop = text.index(_LOOP_STOP_LINE)
    df_read = re.search(r"df\b[^\n]*--output=avail", text[:stop])
    assert df_read is not None, (
        "no df-based free-space read appears before the loop-stop line — the "
        "pre-deploy disk-space guard (#3632) is missing or moved after the stop"
    )
    return text[df_read.start() : stop]


def test_cleanup_reclaims_build_artifacts() -> None:
    """Issue #3632: cleanup() must reclaim dangling images and stale build cache.

    Before the fix cleanup() only ran ``docker rmi ...:rollback`` (a tag drop),
    so the image-before-last stayed dangling and build cache accumulated without
    bound across deploys until the host root filesystem filled. cleanup() must
    now also ``docker image prune`` and ``docker builder prune``.
    """
    body = _cleanup_body(_SCRIPT.read_text(encoding="utf-8"))
    assert "docker image prune" in body, (
        "cleanup() must run `docker image prune` to reclaim dangling images"
    )
    assert "docker builder prune" in body, (
        "cleanup() must run `docker builder prune` to reclaim stale build cache"
    )


def test_disk_preflight_aborts_with_distinct_exit_code() -> None:
    """Issue #3632 (r1): the disk-space abort must exit 3, not exit 1.

    safe-deploy.sh documents exit 1 = "rollback performed" and deploy.yml renders
    exit 1 as "Deploy failed (auto-rolled back)". The disk preflight rolls nothing
    back — it aborts *before* the loop is even stopped — so reusing exit 1 would
    make the deploy alert falsely report an auto-rollback. The abort must use a
    distinct code (3), and the script's exit-code header must document it.
    """
    text = _SCRIPT.read_text(encoding="utf-8")
    region = _preflight_region(text)
    # Match the bare abort *statement*, not a substring: the guard's own comment
    # legibly mentions "exit 1" while explaining why it uses 3 instead, so a
    # substring check would false-positive on that prose.
    statements = [line.strip() for line in region.splitlines()]
    assert "exit 3" in statements, (
        "the disk-space preflight must abort with the distinct exit code 3 (not "
        "exit 1, which the deploy alert renders as 'auto-rolled back')"
    )
    assert "exit 1" not in statements, (
        "the disk-space preflight must not abort with exit 1 — exit 1 means "
        "'rollback performed', but the preflight rolls nothing back"
    )
    # The exit-code header (above the `set -Eeuo pipefail` line) must document
    # code 3 so its distinct meaning is discoverable, not just used inline.
    header = text[: text.index("set -Eeuo pipefail")]
    assert "aborted before any change" in header, (
        "the exit-code header must document exit code 3 (aborted before any "
        "change) so its distinct meaning is legible"
    )


def test_disk_preflight_validates_threshold_override() -> None:
    """A non-numeric MAESTRO_DEPLOY_MIN_FREE_KB must not disarm the guard.

    ``[ "$AVAIL_KB" -lt "$MIN_FREE_KB" ]`` with a non-integer operand errors out
    and the ``elif`` evaluates false, so a typo'd override (e.g. ``5G``) would
    silently skip the abort branch and let the deploy proceed against a full
    disk — the opposite of the guard's fail-loud purpose. The preflight must
    validate the override is all digits and fall back to the default (guard
    stays armed) when it is not.
    """
    text = _SCRIPT.read_text(encoding="utf-8")
    # Anchor on the threshold assignment (it precedes the df read that
    # _preflight_region anchors on), bounded at the loop stop so the assertion
    # stays confined to the preflight.
    assign = text.index('MIN_FREE_KB="${MAESTRO_DEPLOY_MIN_FREE_KB')
    region = text[assign : text.index(_LOOP_STOP_LINE)]
    assert "[!0-9]" in region, (
        "the disk-space preflight must validate MAESTRO_DEPLOY_MIN_FREE_KB is "
        "all digits — a non-numeric value makes the -lt test error and "
        "silently disables the guard"
    )


def test_disk_preflight_precedes_pre_deploy_notify() -> None:
    """PR #3645 review: the disk-space guard must precede the pre-deploy notify.

    ``maestro notify-pre-deploy`` persists a Message into the loop's memory dir
    and writes a journal ``milestone`` (``cmd_notify_pre_deploy`` in
    ``maestro/cli.py`` calls ``store.save_message()`` and
    ``journal.write_thought(triggered_by='pre_deploy')``). If the guard aborts
    (``exit 3``) *after* that notify, the running loop has already been told a
    deploy is imminent — contradicting the guard's own "nothing has been
    deployed" contract and the "keep the journal honest" invariant. The
    ``df``-based free-space read must therefore appear *before* the
    ``maestro notify-pre-deploy`` invocation, so a low-disk abort leaves the
    loop no phantom deploy message or journal milestone.
    """
    text = _SCRIPT.read_text(encoding="utf-8")
    df_read = re.search(r"df\b[^\n]*--output=avail", text)
    assert df_read is not None, (
        "no df-based free-space read found — the disk-space preflight (#3632) "
        "is missing"
    )
    notify = text.find(_PRE_DEPLOY_NOTIFY_LINE)
    assert notify != -1, (
        "no `maestro notify-pre-deploy` invocation found in safe-deploy.sh"
    )
    assert df_read.start() < notify, (
        "the disk-space preflight's df read must precede `maestro "
        "notify-pre-deploy` so an aborted deploy writes no phantom pre-deploy "
        "message or journal milestone (PR #3645 review)"
    )


def test_disk_preflight_aborts_before_loop_stop() -> None:
    """Issue #3632: a disk-space preflight must abort before the loop is stopped.

    The graceful loop stop runs first, so a deploy against a near-full disk used
    to stop the loop and only then die at the build / ``up -d`` — leaving the
    loop down with no room to restart it. A ``df``-based free-space read plus a
    fail-loud abort on an under-threshold branch must now precede the stop, so a
    low-disk deploy leaves the loop running (a louder, earlier failure, not loop
    auto-recovery).
    """
    region = _preflight_region(_SCRIPT.read_text(encoding="utf-8"))
    # An under-threshold comparison guards the abort (not an unrelated exit).
    assert "-lt" in region, (
        "the disk-space preflight must compare free space against a threshold"
    )
    # The under-threshold branch aborts loudly (fail message + exit), before the
    # loop stop, so the loop stays running.
    assert re.search(r"\bfail\b", region), (
        "the disk-space preflight must emit a fail-loud message on abort"
    )
    assert re.search(r"\bexit\b", region), (
        "the disk-space preflight must exit (abort) on the under-threshold branch"
    )

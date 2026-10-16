# 039: GITHUB_TOKEN Scope Elevation and Container `gh auth setup-git`

| Field | Value |
|-------|-------|
| **Impact** | MEDIUM |
| **Status** | shipped |
| **Affects** | `maestro/config.py` (optional soft-check in `check_secrets`), `CLAUDE.md`, `docs/deployment.md`, Dockerfile/entrypoint, deploy environment (PAT rotation) |
| **Depends on** | (none) |
| **Prerequisite for** | **033** (must land before 033 enables in production) |

## Description

> Status updated to shipped on 2026-06-01 by /update-proposals — see [README.md](../README.md#shipped) for the landing record.

Operational/deployment proposal that captures the two pieces of out-of-code work required before 033's `Publisher` can succeed in the production container:

1. **`GITHUB_TOKEN` scope elevation.** The deploy PAT currently carries `contents:write`, `pull_requests:write`, `issues:write`. `gh repo create --public` requires the `repo` scope (broader: full read/write on public + private repos). Thomas rotates the secret once at deploy.
2. **Container credential helper.** `git push` from inside the staging directory needs a credential helper that can satisfy GitHub HTTPS auth using the rotated PAT. `gh auth setup-git` configures one. The container image must run it once (image build or entrypoint) so the helper is in `~/.gitconfig` for the loop process's UID.

Splitting this off from 033 keeps the code-change PR (033) and the secret-rotation+image-change PR (039) independently reviewable. 033's `Publisher` will fail loudly with a clear stderr if either prerequisite is missed; this proposal documents the procedure so the failure mode is "blocked at deploy" rather than "silently degraded in production".

A small optional non-blocking soft-check in `check_secrets()` warns at startup when `publication_enabled` is true and `gh auth status` reports a token without `repo` scope — defence-in-depth so a forgotten rotation surfaces immediately, not on the first publish attempt.

## Relevant Findings

- CLAUDE.md's Environment Variables table currently lists `GITHUB_TOKEN` as `contents:write, pull_requests:write, issues:write`. That row is the single source of truth for required scopes and needs the `repo` addition.
- `check_secrets()` in `maestro/config.py` validates secret *presence* and prefix shape only; it does not call any external service. A scope check would be the first network call from `check_secrets()`, so it must be opt-in (gated on `publication_enabled`) and best-effort (timeout, swallow on failure).
- `gh repo create` exits with code 1 and a clear stderr (`HTTP 403 — Resource not accessible by personal access token`) when the scope is missing. 033's `_publish` flips the proposal to `status=failed` with that stderr in `error`, so a missed rotation is not silently catastrophic — but it does waste an approval cycle.
- `gh auth setup-git` writes a `credential.https://github.com.helper` entry to `~/.gitconfig`. It is idempotent and safe to run on every container start.

## Implementation Plan

1. **`docs/deployment.md`.** New section *Publication track deployment prerequisites* covering, in order:
   - Regenerate the deploy PAT with scopes `contents:write`, `pull_requests:write`, `issues:write`, **`repo`**.
   - Update the Docker secret / `.env` value for `GITHUB_TOKEN`.
   - Restart the loop container (the dashboard does not need restart — it does not call `gh repo create`).
   - Verify: `docker exec maestro-loop gh auth status --show-token` lists `repo` in the scope summary.

2. **Container `gh auth setup-git`.** Add a single line to the Dockerfile entrypoint (or image build, depending on how `gh` is currently authenticated):
   ```bash
   gh auth setup-git
   ```
   This must run after `GITHUB_TOKEN` is in the environment but before the loop process starts. Document the placement in `docs/deployment.md` alongside the rotation procedure.

3. **Optional soft-check in `check_secrets()`** (`maestro/config.py`). Behind `publication_enabled and GITHUB_TOKEN`:
   ```python
   try:
       result = subprocess.run(
           ["gh", "auth", "status", "--show-token"],
           capture_output=True, text=True, timeout=5,
       )
       if "repo" not in (result.stderr + result.stdout):
           logger.warning(
               "publication_enabled=true but GITHUB_TOKEN does not "
               "appear to carry the 'repo' scope. gh repo create will "
               "fail. See proposal 039."
           )
   except Exception:
       pass  # best-effort; the production failure mode is still clear.
   ```
   Non-blocking by design. Smoke mode skips this check entirely.

4. **CLAUDE.md.** Update the `GITHUB_TOKEN` row in the Environment Variables table to list scopes `contents:write, pull_requests:write, issues:write, repo`. Add a sentence pointing at `docs/deployment.md` for the rotation procedure.

## Testing Strategy

1. **`check_secrets()` soft-check** (`tests/test_config.py`): mock `subprocess.run("gh auth status --show-token")` to return stderr without `repo` while `publication_enabled=true` → assert one `WARNING` log line referencing proposal 039. With `repo` present → no warning. With `subprocess.run` raising → no warning, no exception (best-effort swallow). With `publication_enabled=false` → no `subprocess.run` call.
2. **No regression for non-publication deploys**: `publication_enabled=false` runs `check_secrets` without invoking `gh`. Asserted by mocking `subprocess.run` and checking call count == 0.
3. **Manual deploy verification** (documented, not automated):
   - In the loop container after rotation: `gh auth status --show-token` lists `repo`.
   - `gh auth setup-git` exits 0 and writes `credential.https://github.com.helper` to `~/.gitconfig`.
   - One end-to-end test: create a throwaway test repo via `gh repo create thomas-sodon/maestro-publish-039-test --public --description test` from inside the container, then delete it. Confirms the rotated PAT and credential helper both work before any real publication is approved. Targeting the `[publication] owner` account namespace (`thomas-sodon`) is deliberate: this step must exercise the same namespace real publications are created in, and a test under any other namespace would pass on permissions the pipeline never uses.

## Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| PAT carries `repo` but cannot create repositories in the `[publication] owner` namespace | Medium | `gh repo create` needs more than scope: the token's owner must be permitted to create repositories in the `[publication] owner` account namespace (`thomas-sodon`), and if that owner were a SAML-enforcing organization the PAT would also need SSO authorization. Both fail closed with a 403 that `_publish` records in the proposal's `error`. The manual end-to-end step above targets the `[publication] owner` namespace precisely so this surfaces at deploy, not on the first approved publication. |
| Elevated `repo` scope broadens blast radius | Medium | `repo` is the minimum scope `gh repo create` requires (no narrower public-only scope exists for PATs). No other Maestro code path uses scope-elevated operations today; the rotation is single-purpose. The trust-chain hardening track (021–029) constrains what can be requested with the elevated token. |
| Container missing the credential helper after a base-image refresh | Medium | `gh auth setup-git` runs at container entrypoint, not just at image build, so a base-image change cannot silently strip the helper. Documented in `docs/deployment.md` and verified by the manual deploy check. |
| Forgotten rotation lets 033 land in production with the old PAT | Medium | The optional soft-check in `check_secrets()` warns at startup. If missed there, the first `gh repo create` returns exit 1 with a clear stderr that `_publish` records in the proposal's `error` — visible in dashboard and Telegram. Costs one wasted approval cycle, no orphan repo. |
| Rotation introduces a brief window where `GITHUB_TOKEN` lacks one of the older scopes | Low | The new PAT carries the union of old + new scopes (the procedure explicitly lists all four). The container restart is the only window — the loop is paused during restart, so no in-flight evolution PR or issue can hit a half-rotated token. |

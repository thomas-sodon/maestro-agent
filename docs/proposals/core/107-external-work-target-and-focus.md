# 107: External Work Target and Focus Dial — One Owner for *Which Repo, Which Gates, Which Framing, and How Much Self-Focus*, So Effort Can Redirect from Self to a Customer Repo

| Field | Value |
|-------|-------|
| **Impact** | HIGH |
| **Status** | shipped |
| **What landed** | Phase A (step 1), Phase B (steps 2–3), and Phase C's focus dial (step 4). Step 1 — the owner: `maestro/work_target.py` with the frozen `WorkTarget` dataclass and the `WorkFraming` alias, plus the `Config.work_target()` resolver (`maestro/config.py`) assembling all six facets from a new `[work]` section (`maestro.toml`; six `("work", …)` rows in `maestro/toml_config.py`; `work_focus_profile` / `work_task_framing` / `work_repo_workspace` / `work_github_repo` / `work_gate_source_path` / `work_gate_tests_path` fields, the two framings validated against `VALID_WORK_FRAMINGS`), every facet defaulting to today's self values; covered by `tests/test_work_target.py` and `tests/test_config.py`. The gate facet shipped as two redirectable path atoms (`WorkTarget.gate_source_path` / `.gate_tests_path`, default `maestro/` / `tests/`) rather than the single `gate_commands` tuple Step 1 sketched. Step 2 — the gate-command collapse: all four readers now source the two atoms through `Config.work_target()` — `evolution.run_quality_gates`, `loop/pr_remediation.preflight`, `gate_check.build_gate_commands`, and `red_root.build_red_root_gates` (the former `GATE_COMMANDS` / `RED_ROOT_GATES` constants are gone) — with `tests/test_gate_command_owner.py` (an AST guard) asserting no hardcoded `maestro/`+`tests/` gate literal survives in a reader module. Step 3 — the repo-client collapse: the git / GitHub client construction seams now build `SubprocessVCS` / `SubprocessCodeHost` / `IssueTracker` from `Config.work_target()` (`WorkTarget.repo_workspace` / `.github_repo`) instead of the raw `config.workspace` / `config.github_repo` scalars — at `cli._build_agent`, `cli._build_mcp_serve_registry`, `cli.cmd_issues`, and the `MaestroAgent` / `EvolutionEngine` fallback constructions — both facets defaulting to the self scalars so a no-`[work]` build is byte-identical and a `[work]` redirect points the clients at another checkout/repo; covered by new `tests/test_work_target_clients.py` and `tests/test_evolution.py` additions. Step 4 — the focus dial: `phase_will_run` (`maestro/loop/lifecycle.py`) now returns `False` for the introspective phases held in the `_FOCUS_SUPPRESSED_PHASES` frozenset (`evolve`, `dream`, `audit_self_model`, `curate_pending_plans`) when `Config.work_target().focus_profile == "external_delivery"`, so `generate_task` / `run_task` / `tend_evolution_prs` keep their iteration slots; the default `self_evolution` profile is a no-op leaving every cadence exactly as before; covered by `tests/test_loop_lifecycle.py`. Step 5 — the external-delivery framing: `maestro/prompts/defaults.py` adds the `TASK_GENERATION_PROMPT_EXTERNAL` / `EVOLUTION_PROMPT_EXTERNAL` constants and the `select_task_generation_prompt` / `select_evolution_prompt` selectors (modelled on `claude_md_guidance`), wired into `task_generation.generate_task` (curator prompt) and `evolution.reflect` (both session and stateless paths) so that under `task_framing == "external_delivery"` the prompts frame work as delivery against `WorkTarget.github_repo` (a generic phrase when unset) instead of self-modification; the default `self_evolution` framing returns today's constants byte-for-byte. The identity/stage prompts (`agent._build_identity_context`) were intentionally descoped and stay self-framed by design. Covered by `tests/test_prompts.py`, `tests/component/test_task_generation.py`, `tests/test_evolution.py`, and `tests/test_session_reuse_phases.py`. Step 6 — the construction chokepoint: `tests/test_work_target_chokepoint.py` AST-guards the five loop-facing client-construction seams (`cli._build_agent` / `cli._build_mcp_serve_registry` / `cli.cmd_issues` / `MaestroAgent.__init__`'s fallback arms / `EvolutionEngine.__init__`), failing the build when a `SubprocessVCS` / `SubprocessCodeHost` / `IssueTracker` is fed a raw `config.workspace` / `config.github_repo` scalar, with `cli.cmd_notify_pre_deploy` carried in a named `_FUNCTION_ALLOWLIST` as the one deferred seam and the residual direct-`cwd=config.workspace` sites named in its deferred-scatter docstring; together with the gate-literal sibling guard `tests/test_gate_command_owner.py` this completes Phase D, mirroring `tests/test_merge_truth_chokepoint.py`. |
| **What is missing** | _(nothing — all six steps landed. The Phase D chokepoint shipped as two sibling AST guards: `tests/test_gate_command_owner.py` (gate-command literals) and `tests/test_work_target_chokepoint.py` (client constructions). The identity/stage prompts (`agent._build_identity_context`) were intentionally left self-framed and are out of scope for this proposal; the residual direct `cwd=config.workspace` subprocess sites are named in the chokepoint test's deferred-scatter docstring for a later PR.)_ |
| **Affects** | new `maestro/work_target.py`, `maestro/config.py` (`Config.work_target`, `[work]` fields), `maestro/toml_config.py` (`_TOML_TO_CONFIG`), `maestro.toml`, `maestro/evolution.py` (`run_quality_gates`, `EvolutionEngine.__init__`), `maestro/loop/pr_remediation.py` (`preflight`), `maestro/gate_check.py` (`build_gate_commands`), `maestro/red_root.py` (`build_red_root_gates`), `maestro/cli.py` (`_build_agent`), `maestro/agent.py` (`MaestroAgent.__init__`), `maestro/issues.py` (`IssueTracker`), `maestro/loop/lifecycle.py` (`phase_will_run`), `maestro/prompts/defaults.py`, `maestro/loop/task_generation.py`, new `tests/test_work_target_chokepoint.py`, tests |
| **Depends on** | _(none — foundational engine substrate; external delivery targets are the natural consumer)_ |
| **Derived from** | code + git history via `/propose`, 2026-07-30 (no live production-loop data was reachable — the only `data/` on disk was a fresh dev instance at iteration 2, so the evidence is architectural, not behavioural) |

## Description

> Status updated to shipped on 2026-08-02 by /plan-proposal — see [README.md](../README.md#shipped) for the landing record.

Maestro's whole loop is hardwired to work on **itself**: it builds on its own
checkout, opens PRs against its own GitHub repo, runs its quality gates over
`maestro/`+`tests/`, frames every generated task as self-improvement, and
spends a large share of each iteration on introspective phases
(`contemplate` / `evolve` / `dream` / `audit_self_model` / `curate_pending_plans`).
None of these facts has a single owner — each is re-derived independently at a
dozen-plus call sites. There is no knob that says "for now, point your effort at
*that* repo, on behalf of *that* customer, and dial down the navel-gazing." This
proposal adds one.

The structural fix is a single **`WorkTarget`** owner that resolves the four
contested facets of "what is the target of my effort" — repo location
(`workspace` + `github_repo`), gate command, task framing, and introspection
budget — from a new `[work]` config section, with every facet defaulting to the
current self values so the default build is byte-for-byte the behaviour we have
today. Subsystems read their facet through the one owner instead of reaching for
`config.workspace` / `config.github_repo` / a hardcoded `maestro/` gate literal /
a self-framed prompt directly. A `[work] focus_profile = external_delivery`
setting then redirects the repo facets to a customer repo and dampens the
introspective phase cadences so budget flows to `run_task` / `tend_evolution_prs`
against that external repo. This is the same move as proposal 087 (one
provenance-stamped owner behind one chokepoint, replacing scattered per-site
re-derivations of a single fact).

It gives external delivery targets the redirection substrate they need. End state: the operator sets `[work]` to a customer repo and
`focus_profile = external_delivery`; the loop generates and delivers
externally-framed tasks against that repo, runs the customer's gate command,
keeps every push behind the unchanged human-in-the-loop PR gate, and spends its
budget on delivery instead of self-reflection — all revertible by returning
`[work]` to its self defaults.

## Relevant Findings

- `maestro/config.py` (`Config.workspace`, `Config.github_repo`) — the two
  scalar sources of truth for "my repo." `workspace` defaults to
  `Path(os.getcwd())`; `github_repo` defaults to `None`. Single *values*, but
  **no owner object** — they are re-read directly at ~a dozen and ~six sites
  respectively.
- `maestro/evolution.py` (`run_quality_gates`), `maestro/loop/pr_remediation.py`
  (`preflight`), `maestro/gate_check.py` (`GATE_COMMANDS`), `maestro/red_root.py`
  (`RED_ROOT_GATES`) — **four** independent literals of the same
  `["ruff","check","maestro/","tests/"]` / `["mypy","maestro/"]` /
  `["pytest","tests/", …, "--cov=maestro"]` gate triple, plus the
  `Config.cov_fail_under` coupling. No shared constant; the package path is
  hardcoded four times.
- `maestro/prompts/defaults.py` (`TASK_GENERATION_PROMPT`, `EVOLUTION_PROMPT`) —
  the self-framing is concentrated here: *"candidate tasks **for yourself**"* and
  *"proposing **your own evolution** … changes to **your own code** …
  self-modification."* Injected onto every phase via
  `maestro/agent.py` (`_build_identity_context`); the code paths in
  `maestro/loop/task_generation.py` (`TaskGenerator.generate_task`) are thin
  `.format()` shims over these constants.
- `maestro/loop/lifecycle.py` (`phase_will_run`) — the single gate deciding
  whether each phase runs, keyed on the cadence inputs (`task_every`,
  `evolve_every`, `dream_every`, `audit_self_every`, `curate_every`). A focus dial
  tunes exactly these inputs.
- `maestro/loop/lifecycle.py` (`_BUDGET_GATED_PHASES`) — the frozenset of phases
  already dropped first under burn-rate throttle (`contemplate`, `evolve`,
  `dream`, `curate_pending_plans`, `generate_task`, …). This is the existing
  "dampen introspection under pressure" precedent the focus dial reuses.
- `maestro/config.py` (`Config.loop_curate_enabled`, `Config.loop_curate_every`)
  with `maestro/toml_config.py` (`_TOML_TO_CONFIG`) — the master-switch-plus-cadence
  pattern (`curate_enabled and curate_every > 0 and iteration % curate_every == 0`)
  that the `[work]` focus dial imitates. `_TOML_TO_CONFIG` already anchors
  `("paths","workspace")` and `("github","repo")`; a `[work]` section is new rows
  in the same registry, applied generically by its parse loop.
- `maestro/tool_gating.py` (module docstring, `_REDIRECT`, own-branch push allowance)
  — the action gate blocks `gh pr create|merge|ready` and any push to a protected
  or non-own-branch ref **by command shape, not by repo**. It never reads
  `github_repo`. Redirecting the target therefore does **not** weaken the
  human-in-the-loop invariant: a customer PR still opens and merges only by human
  hand; only pushes to the run's own `maestro/task/{id}` branch are permitted.
- `maestro/publication.py` (`staging_dir`, `_run_git`, `_git_push`) — the one
  existing precedent for driving git against a directory that is **not**
  `config.workspace`. It proves the loop can already operate on a non-self repo
  path; `WorkTarget` generalises that from the publish path to the whole loop.
- `maestro/pr_facts.py` (`compute_owned_writable_branches`) — own-branch
  derivation keys on the `maestro/task/` / `maestro/evolve/` prefixes, not on the
  repo. External-repo delivery branches keep those prefixes, so they stay
  gate-permitted without touching the oracle.

## Production Evidence

No live production-loop data was available: the only `data/` on disk was a fresh
dev instance (`lifecycle.json` at `iteration 2`, `self_model.json` stage
`awakening`, `evolution_count 0`, a 3-line `journal.jsonl`). The loop container
was not reachable for a `docker exec` read either. The evidence below is
therefore **architectural scatter measured over the current tree (2026-07-30)**,
not behavioural counts — which is the right evidence for this class anyway: the
concern is a missing chokepoint, not a misbehaving one.

- **Repo-target facet — re-derived, no owner.** `Config.workspace` is read as a
  git/subprocess `cwd=` at ~a dozen sites spanning
  `maestro/cli.py` (`_build_agent` and the `cmd_review` path both build their own
  `SubprocessVCS` / `SubprocessCodeHost`), `maestro/agent.py`
  (`MaestroAgent.__init__` fallback plus direct branch-cleanup / PR-merged / gate
  probes), `maestro/evolution.py`, `maestro/contributions.py`
  (`ContributionTracker`), `maestro/loop/pr_remediation.py`, `maestro/executor.py`,
  `maestro/audit_checks.py`, and `maestro/doc_proposals.py`. `Config.github_repo`
  is read at ~six construction sites (`SubprocessCodeHost`, `IssueTracker`,
  `EvolutionEngine`, and the two `cli.py` build paths). Measured over the current
  tree.
- **Gate-command facet — the same literal four times.** The
  `maestro/`+`tests/` package path is hardcoded in four independent gate lists —
  `run_quality_gates`, `PrRemediationController.preflight`, `GATE_COMMANDS`,
  `RED_ROOT_GATES` — none sharing a constant, plus two `--cov=maestro` /
  `cov_fail_under` couplings. Any redirect to an external package touches all four.
  Measured over the current tree.
- **Framing facet — self-language in the stage ladder.** All five stage prompts
  plus `TASK_GENERATION_PROMPT` and `EVOLUTION_PROMPT` in `prompts/defaults.py`
  frame work as introspection or self-modification; `PREDICTION_REQUIREMENT` in
  `prompts/reflection.py` bets on *"your own next-cycle behaviour."* Zero of the
  seven carry any notion of external delivery. Measured over the current tree.
- **Focus facet — no external-delivery dial.** Of the 18 entries in
  `agent.py` (`_LOOP_PHASES`), the introspective set
  (`contemplate`/`evolve`/`audit_self_model`/`curate_pending_plans`/`dream`) has
  five members, each cadence-gated in `phase_will_run` with **no** setting that
  favours delivery phases. The only existing lever that suppresses them is the
  burn-rate `_BUDGET_GATED_PHASES` frozenset — a pressure valve, not a chosen
  direction. Measured over the current tree.

## Prior Point Fixes (whack-a-mole ledger)

There is **no prior-fix trail** for target redirection —
`git log --all --grep` for *external repo / target repo / work target /
customer repo / repo-agnostic / multi-tenant / focus profile* returns nothing.
This capability has never existed, so the honest evidence for a structural
remedy is not a history of patches but the **scatter** itself: the single fact
"what is the target of my effort" re-derived independently at each site below,
with no owner. This is precisely the 087 shape — a fact with many readers and no
chokepoint. The table lists the independent derivation sites (the "moles" the one
owner subsumes) and which the design retires.

| Derivation site | What it independently assumes | Retired by |
|---|---|---|
| `evolution.py` (`run_quality_gates`) | gate = `maestro/`+`tests/` | Step 2 — reads `WorkTarget.gate_commands` |
| `loop/pr_remediation.py` (`preflight`) | gate = `maestro/`+`tests/` | Step 2 |
| `gate_check.py` (`GATE_COMMANDS`) | gate = `maestro/`+`tests/` | Step 2 |
| `red_root.py` (`RED_ROOT_GATES`) | gate = `maestro/` | Step 2 |
| `cli.py` (`_build_agent`), `agent.py` / `evolution.py` / `issues.py` constructors | repo clients built from `config.workspace` / `config.github_repo` | Step 3 — built via `WorkTarget` |
| `prompts/defaults.py` (`TASK_GENERATION_PROMPT`, `EVOLUTION_PROMPT`) | work = self-improvement | Step 5 — framing selected by `WorkTarget.task_framing` |
| `loop/lifecycle.py` (`phase_will_run` cadences) | budget favours introspection | Step 4 — focus dial gates introspective phases |

The direct `cwd=config.workspace` subprocess sites in `agent.py` /
`executor.py` / `audit_checks.py` that are **not** repo-client constructions are
deliberately left reading the same scalar for now (see Out of scope) — Step 6's
chokepoint test enforces the *constructions* and the *gate literals*, the two
cleanly-enforceable facets, and names the deferred sites so the gap is visible
rather than silent.

## Implementation Plan

### Phase A — the owner (inert)

1. [x] **Add the `WorkTarget` owner and `[work]` config.** new `maestro/work_target.py`,
   `maestro/config.py`, `maestro/toml_config.py`, `maestro.toml` — a `WorkTarget`
   dataclass exposing `repo_workspace: Path`, `github_repo: str | None`,
   `gate_commands: tuple[tuple[str, ...], ...]`, `task_framing: Literal["self_evolution","external_delivery"]`,
   `focus_profile: Literal["self_evolution","external_delivery"]`, plus a
   `Config.work_target()` resolver that reads a new `[work]` section and defaults
   **every** facet to the current self values (`workspace`, `github_repo`, the
   `maestro/`+`tests/` gate triple, `self_evolution`, `self_evolution`). New
   `_TOML_TO_CONFIG` rows follow the `curate_enabled`+`curate_every`
   master-switch-plus-cadence shape. Nothing reads it yet.
   - *Depends on:* none.
   - *Acceptance:* with no `[work]` section, `Config.work_target()` returns a
     `WorkTarget` whose facets equal today's self values; a `[work]` override
     round-trips through `from_env` unchanged.

### Phase B — collapse the facets onto the owner (default behaviour unchanged)

2. [x] **Route the gate command through `WorkTarget.gate_commands`.**
   `maestro/evolution.py` (`run_quality_gates`), `maestro/loop/pr_remediation.py`
   (`preflight`), `maestro/gate_check.py` (`GATE_COMMANDS`), `maestro/red_root.py`
   (`RED_ROOT_GATES`) — replace the four hardcoded `maestro/`+`tests/` literals
   with reads of `WorkTarget.gate_commands`. Default resolves to the identical
   triple, so behaviour is unchanged.
   - *Depends on:* 1.
   - *Acceptance:* a golden test asserts no `["ruff","check","maestro/","tests/"]`-shaped
     literal survives outside `work_target.py`; the four readers produce the same
     commands as today under the default target.
3. [x] **Build the repo clients via `WorkTarget`.** `maestro/cli.py`
   (`_build_agent`), `maestro/agent.py` (`MaestroAgent.__init__`),
   `maestro/evolution.py` (`EvolutionEngine.__init__`), `maestro/issues.py`
   (`IssueTracker`) — construct `SubprocessVCS` / `SubprocessCodeHost` /
   `IssueTracker` from `WorkTarget.repo_workspace` / `.github_repo` at the
   construction seams instead of reaching for `config.workspace` /
   `config.github_repo` directly. Default resolves to the same scalars.
   - *Depends on:* 1.
   - *Acceptance:* under the default target the constructed clients target the
     same workspace and repo as today; a unit test with a `[work]`-overridden
     target constructs clients pointed at the external repo.

### Phase C — redirect (behaviour, flag-gated, default-off)

4. [x] **Add the focus dial.** `maestro/loop/lifecycle.py` (`phase_will_run`),
   `maestro/config.py` — when `WorkTarget.focus_profile == "external_delivery"`,
   `phase_will_run` suppresses the introspective phases
   (`evolve`, `dream`, `audit_self_model`, `curate_pending_plans`, and dampens
   `contemplate`) using the `_BUDGET_GATED_PHASES` membership pattern, so
   `generate_task` / `run_task` / `tend_evolution_prs` keep their slots. Default
   `self_evolution` leaves every cadence exactly as today.
   - *Depends on:* 1.
   - *Acceptance:* at an iteration where the self profile runs `evolve`/`dream`,
     the `external_delivery` profile returns `False` for those phases and `True`
     for `run_task` / `tend_evolution_prs`; the default profile is unchanged.
5. [x] **Select task framing via `WorkTarget.task_framing`.**
   `maestro/prompts/defaults.py`, `maestro/loop/task_generation.py` — add an
   external-delivery variant of `TASK_GENERATION_PROMPT` / `EVOLUTION_PROMPT`
   that frames work as delivering on the target repo's issues/backlog (naming
   `WorkTarget.github_repo`) rather than self-improvement, chosen by
   `task_framing`. Default keeps the current constants verbatim.
   `maestro/agent.py` (`_build_identity_context`) was intentionally descoped —
   the identity/stage prompts stay self-framed by design.
   - *Depends on:* 1.
   - *Acceptance:* with `task_framing="external_delivery"` the generated prompt
     names the external repo and omits self-modification language; with the
     default it is byte-identical to today's constant.

### Phase D — enforce and retire the scatter

6. [x] **Land the chokepoint test.** new `tests/test_work_target_chokepoint.py` —
   an AST/golden test asserting the gate-command literals and the
   `SubprocessVCS`/`SubprocessCodeHost`/`IssueTracker` constructions read their
   target only through `WorkTarget`, and naming the deferred direct-`cwd=` sites
   so the remaining scatter is explicit. Lands after Steps 2–3 prove the
   migration, mirroring `tests/test_merge_truth_chokepoint.py`.
   - *Depends on:* 2, 3.
   - *Acceptance:* the test fails if a new gate literal or a raw
     `config.workspace`/`config.github_repo` client construction is reintroduced
     at an enforced site.

## Testing Strategy

All tests use `mem_fs` and mock every external (`subprocess.run`, `gh`, git),
per CLAUDE.md; no real network, sleep, or subprocess.

- **Owner + config round-trip** (unit) — `Config.work_target()` returns self
  defaults with no `[work]` section; a `[work]` overlay round-trips through
  `from_env`; the `_TOML_TO_CONFIG` rows apply generically (happy path + the
  bool/int type-guard branch). Covers Step 1.
- **Gate-command owner** (unit) — with `subprocess.run` mocked, each of the four
  readers emits `WorkTarget.gate_commands`; asserts the default equals the
  historical triple and an external target swaps the package path. Covers Step 2.
- **Repo-client construction** (unit) — `_build_agent` and the three constructors
  build clients whose workspace/repo come from `WorkTarget`; a self target and an
  external target are both asserted, plus the injected-client fallback branch.
  Covers Step 3.
- **Focus dial** (unit) — `phase_will_run` truth table across both profiles at a
  spread of iteration numbers: introspective phases suppressed under
  `external_delivery`, delivery phases retained, self profile unchanged; guard
  branch for a disabled cadence. Covers Step 4.
- **Framing selection** (unit) — the external variant names the target repo and
  drops self-language; the default is byte-identical to the current constant.
  Covers Step 5.
- **Chokepoint enforcement** — `tests/test_work_target_chokepoint.py` (named in
  Step 6): a fault-seeded reintroduction of a gate literal or a raw
  `config.workspace` client construction makes the AST assertion fail; the clean
  tree passes.
- **Action-gate regression** — assert the existing `tool_gating.py` tests still
  pass unchanged with a redirected target: a `gh pr create|merge|ready` against
  the external repo is still blocked and an own-branch push is still permitted,
  proving redirection does not open a bypass.

## Risk Assessment

- **Rollback posture.** Every facet defaults to the current self values and the
  focus dial defaults to `self_evolution`, so a default build is behaviourally
  identical to today; the whole feature is inert until an operator writes a
  `[work]` section. Steps are independently mergeable and each preserves default
  behaviour, so an interrupted rollout leaves the loop working. Revert = delete
  the `[work]` section (or the flag) — no state migration.
- **Trust chain — unchanged and load-bearing.** `maestro/tool_gating.py` is
  **not** touched: it gates by command shape, not repo, so a redirected target
  still requires a human to open and merge the customer PR, and only own-branch
  (`maestro/task/{id}`) pushes are permitted. The action-gate regression test
  (Testing Strategy) proves the control still fires under redirection. The
  CLAUDE.md "Human-in-the-loop evolution" invariant holds verbatim.
- **Secrets / credentials for the external repo.** Pointing `github_repo` at a
  customer repo means the `gh` token must carry access to it; that stays the
  operator's explicit responsibility. Phase-aware `Config.check_secrets` and the
  `secret_scan` publish gate continue to apply. Per-customer credential isolation
  and sandboxing the external checkout are **out of scope** (below) — a security
  follow-up, not silently assumed here.
- **Scatter not fully closed in one PR.** The chokepoint test enforces the two
  cleanly-enforceable facets (gate literals, client constructions); the residual
  direct `cwd=config.workspace` subprocess sites still read the self scalar. This
  is deliberate and named in Step 6 rather than hidden, so a later PR can migrate
  them without a surprise.
- **Focus dial pauses tiered-memory archival.** Step 4 suppresses `dream`, which
  hosts the best-effort `Memory.archive()` hot/cold rotation and dream-digest
  compression — so `external_delivery` also pauses those. Inert while archival is
  default-off (`archive_threshold_days == 0`), but an operator running
  `external_delivery` *with* archival enabled silently stops hot/cold rotation of
  the JSONL files delivery work appends to; weigh before enabling both together.

## Out of scope

- **Intake, quoting, approval, billing, income reconciliation** — out of scope. This proposal is only the redirection engine
  those consume.
- **Multiple simultaneous targets / multi-tenant operation** — one target at a
  time; a target switch is an operator config change, not a runtime fan-out.
- **Per-customer credential isolation and sandboxing the external checkout** — a
  security-track follow-up; here the operator provisions access explicitly.
- **Rewriting Maestro's identity or self-model** — the self-awareness stage
  ladder is *dampened* under `external_delivery`, not deleted; Maestro keeps its
  identity while its work target redirects.
- **Migrating every direct `cwd=config.workspace` subprocess call** to the owner —
  bounded here to the construction seams and the gate/framing/focus facets; the
  remaining sites are named for a later PR.

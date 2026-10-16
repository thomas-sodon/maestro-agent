export const meta = {
  name: 'plan-to-merged-pr',
  whenToUse: 'Take one concern — or one proposal lane — from plan to an open, review-clean PR, then babysit it. Also: build-only (stacked lanes, no PR), ship-only (open/ready a built branch once its predecessors merged) and monitor-only modes. See .claude/workflows/README.md.',
  description: 'Plan → impl → review/fix until clean → polish-until-clean → PR → monitor comments/conflicts, for one concern or proposal lane',
  phases: [
    { title: 'Plan', detail: '/plan or /plan-proposal at medium effort' },
    { title: 'Implement', detail: '/impl at low effort' },
    { title: 'Review', detail: '/review-changes at high effort, fixes at low, re-reviewed until clean' },
    { title: 'Polish', detail: '/polish at low effort until a clean round' },
    { title: 'PR', detail: 'commit + merge check, then /create-pr (or push only, in build-only mode)' },
    { title: 'Monitor', detail: 'process comments, merge main, until settled' },
  ],
}

// args (a plain string is taken as `concern`):
//   concern: string            the plan / concern text; for planner 'plan-proposal', the steps in scope for this lane
//   planner?: 'plan' | 'plan-proposal'   skill that writes the plan (default 'plan')
//   proposal_number?: number   planner 'plan-proposal': the proposal the skill plans (its only argument)
//   proposal?: string          proposal doc path; frames the review alongside the plan
//   context?: string           scouting hints for the planner
//   plan_text?: string         an already-written plan (e.g. from bug-fix-backlog's triage); skips the planning skill, needs `branch`
//   dir?: string               checkout / worktree to work in (created from the last base when `branch` is given and it is absent)
//   branch?: string            use this branch instead of letting the planner create one
//   bases?: string[]           branches this work is stacked on (merged before planning and at the merge check)
//   ship?: boolean             default true; false = build only: commit, merge check, push the branch, open no PR
//   ship_only?: boolean        skip planning/building: open (or ready `existing_pr`) a PR for `branch`, then monitor
//   existing_pr?: number       ship-only: update + ready this PR instead of creating one
//   predecessor_prs?: number[] PRs that must read MERGED before a PR is opened or readied (Mergify merges any ready PR)
//   title?: string, pr_note?: string     PR title / extra body text when opening; closing keywords in pr_note are the PR's only ones
//   gate?: string              gate semaphore wrapper (default ./scripts/gate-slot.sh; a host may require its own)
//   commit_trailers?: string[] lines the workflow's own commits end with (default: the create-pr skill's trailer)
//   max_review_rounds?: 5, max_polish?: 6, max_monitor_rounds?: 10 (max_rounds is accepted as an alias)
//   efforts?: { plan, impl, review, fix, polish, merge_check, pr, monitor }  (pr unset = the session's effort, as /create-pr always ran)
// Monitor-only mode: { pr: number, branch: string, dir?: string } skips straight to the Monitor phase.
const A = typeof args === 'string' ? { concern: args } : (args || {})
const CONCERN = A.concern
const MONITOR_ONLY = !!A.pr
const SHIP_ONLY = !!A.ship_only
const CLOSES_IN_NOTE = /\b(fix(es|ed)?|close[sd]?|resolve[sd]?):?\s+#\d+\b/i.test(A.pr_note || '')
const BUILD_ONLY = A.ship === false
if (!CONCERN && !MONITOR_ONLY && !SHIP_ONLY) throw new Error('pass args.concern (the plan / concern text), args.ship_only + args.branch, or args.pr + args.branch to only monitor')
if ((MONITOR_ONLY || SHIP_ONLY) && !A.branch) throw new Error('monitor-only and ship-only modes need args.branch')
if (SHIP_ONLY && BUILD_ONLY) throw new Error('ship_only and ship: false are mutually exclusive')
if (MONITOR_ONLY && (SHIP_ONLY || BUILD_ONLY)) throw new Error('args.pr (monitor-only) cannot be combined with ship_only or ship: false')
if (SHIP_ONLY && !A.existing_pr && !A.title) throw new Error('ship-only mode needs args.title (or args.existing_pr)')
if (!MONITOR_ONLY && !SHIP_ONLY && A.planner === 'plan-proposal' && !A.proposal_number) throw new Error("planner 'plan-proposal' needs args.proposal_number")
if (A.plan_text && !A.branch) throw new Error('args.plan_text needs args.branch (the branch the plan is built on)')
const CONTEXT = A.context || ''
const DIR = A.dir || ''
const PLANNER = A.planner || 'plan'
const BASES = (A.bases || []).filter(b => b && b !== 'main')
const PREDS = (A.predecessor_prs || []).map(Number)
// A stacked branch carries unmerged base commits; a ready PR to main would let Mergify merge them early.
if (BASES.length && !A.branch) throw new Error('args.bases needs args.branch (the stacked branch to build)')
if (BASES.length && !BUILD_ONLY && !PREDS.length) throw new Error('a stacked run that opens or readies a PR needs args.predecessor_prs (or ship: false to build only)')
const GATE = A.gate || './scripts/gate-slot.sh'
const TRAILERS = (A.commit_trailers || ['Co-Authored-By: Claude <noreply@anthropic.com>']).join('\n')
const MAX_REVIEW = A.max_review_rounds ?? 5
const EFF = { plan: 'medium', impl: 'low', review: 'high', fix: 'low', polish: 'low', merge_check: 'low', monitor: 'low', ...(A.efforts || {}) }
// Agent options; an unset effort inherits the session's.
const opts = (label, ph, effortKey, schema) => {
  const o = { label, phase: ph }
  if (EFF[effortKey]) o.effort = EFF[effortKey]
  if (schema) o.schema = schema
  return o
}
// Worktrees share one refs/stash: on 2026-10-12 two salvage lanes each popped the other's stash entry.
const NO_STASH = `Never run git stash (push, pop, apply or drop): every worktree of this repo shares one stash list, ` +
  `so another lane can pop your entry or you can pop theirs. For a before/after check, save the diff to a file in ` +
  `your scratch area (git add -N any new fix files first, then git diff --binary HEAD -- <fix paths> > <file>), ` +
  `git checkout HEAD -- <fix paths> and remove any new fix files, leaving the tests in place, run the test, ` +
  `then git apply <file>; or make a WIP commit on your own branch. `
const WHERE = (DIR
  ? `Work ONLY in the git checkout ${DIR} (cd there / use absolute paths); other workflows may be using other checkouts. `
  : '') + NO_STASH
// A denied action must stay denied: in PR #4651 a step blocked six rounds running re-did the edit via Bash/python.
const GUARD = `If a permission check or classifier denies any action, do NOT retry it through another tool or route ` +
  `(Bash, python, sed, heredocs, a different file path). Record it as blocked — for a review comment, post the ` +
  `"Surfaced for human review — out of scope for automated remediation." reply once — and report it; a human decides. `
// scripts/with-env.sh needs a .env, which a git worktree lacks; gh's ambient auth works there.
const AUTH = `For gh/git network commands use scripts/with-env.sh when the checkout has a .env; if it reports .env not found, run gh/git directly (ambient auth works). ` +
  `gh is already authenticated, so a skill's missing .env / GITHUB_TOKEN stop (e.g. error=missing_github_token) does not apply — continue with plain gh/git. ` +
  `For GraphQL owner/name, read them from \`gh repo view --json owner,name\` — never guess the repository name. `
// Every stage that may run a quality gate — directly or through a skill such as /impl, /polish, /merge-main or /process-comments.
const GATES = `GATE RULES (these override any skill instruction to run gates in the foreground with a fixed timeout): run every ruff / mypy / pytest / scripts/quality-gates.sh invocation through the gate semaphore, \`${GATE} <command>\`, from the checkout root. ` +
  `It takes a CPU-disjoint slot sized to this machine's cores and memory and pins the run to it. For a raw test run pass "-n auto", never a literal worker count (leave a skill's {worker_args} placeholder to gate-lease). ` +
  `Exit 75 = every slot busy: re-run the same command. Exit 125 (with a "gate-slot:" line on stderr), or 127 / a missing wrapper script = the wrapper cannot run: report it, never bypass the wrapper. Any other exit code is the gated command's own result. ` +
  `Run long gates with run_in_background and output to a log, monitor the log, and do not end your turn until the run exits — no hard timeouts. ` +
  `Write any commit message or PR body that names those tools to a file and pass it with -F / --body-file. `
const STACK = BASES.length
  ? `This work is stacked on ${BASES.join(', ')}, which may not be merged yet: git fetch and merge the latest origin/<base> of each (skip any already merged into main or deleted) — never rebase, never force-push. Work present on those bases counts as landed, not as a missing dependency. `
  : ''
const PREDS_CHECK = PREDS.length
  ? `First confirm each of ${PREDS.map(n => '#' + n).join(', ')} reads MERGED (gh pr view <n> --json state). If any does not, change nothing and report blocked=true naming it. `
  : ''
const WORK = GUARD + WHERE + GATES
// Review scope: a stacked branch's HEAD carries its bases' commits, so review only this branch's own change.
// `bases` are ordered so the last one contains the others.
const REVIEW_SCOPE = BASES.length ? `all:origin/${BASES[BASES.length - 1]}` : ''
const SCOPE_NOTE = REVIEW_SCOPE
  ? ` If origin/${BASES[BASES.length - 1]} no longer resolves after a git fetch (merged and deleted), use args "all" instead. `
  : ''

const PLAN_SCHEMA = {
  type: 'object',
  properties: {
    branch: { type: 'string', description: 'feature branch created and checked out for this work' },
    plan: { type: 'string', description: 'full plan text: files, changes, tests, verification' },
  },
  required: ['branch', 'plan'],
}
const STEP_SCHEMA = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    gates_green: { type: 'boolean', description: 'ruff+mypy+pytest for touched packages all pass' },
  },
  required: ['summary', 'files_changed', 'gates_green'],
}
const REVIEW_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          file: { type: 'string' },
          severity: { type: 'string', enum: ['bug', 'concern', 'suggestion', 'nit'] },
          description: { type: 'string' },
        },
        required: ['file', 'severity', 'description'],
      },
    },
    completed: { type: 'boolean', description: 'false if the review skill stopped without reviewing' },
  },
  required: ['findings', 'completed'],
}
const POLISH_SCHEMA = {
  type: 'object',
  properties: {
    issues_found_and_fixed: { type: 'integer', description: 'count of bugs/issues fixed this round (0 = clean)' },
    summary: { type: 'string' },
    gates_green: { type: 'boolean' },
  },
  required: ['issues_found_and_fixed', 'summary', 'gates_green'],
}
const OBSERVE_SCHEMA = {
  type: 'object',
  properties: {
    pr_state: { type: 'string', enum: ['OPEN', 'MERGED', 'CLOSED'] },
    head_sha: { type: 'string' },
    has_conflicts: { type: 'boolean', description: 'true if the PR has merge conflicts with main (mergeable CONFLICTING / mergeStateStatus DIRTY)' },
    behind_main: { type: 'boolean', description: 'true if the PR is conflict-free but out of date with main (mergeStateStatus BEHIND)' },
    ai_review_done_on_head: { type: 'boolean', description: 'true once the "AI Code Review" check run for head_sha has completed (any conclusion) (this repo always runs it, so a missing check means not registered yet, not done)' },
    ci_state: { type: 'string', enum: ['green', 'red', 'pending', 'none'], description: 'Quality-gate CI on head_sha, ignoring the AI Code Review check (it fails just by posting findings)' },
    ci_red_caused_by_pr: { type: 'boolean', description: 'true only if ci_state is red and the failure is attributable to this PR\'s diff' },
    actionable_comments: { type: 'integer', description: 'unresolved review threads (incl. outdated) with NO terminal reply (Fixed locally / Could not prove this defect / Could not prove this bug / Declined with / Answered with / Fixed automatically / Tracked in #) and NO out-of-scope reply (Surfaced for human review — out of scope for automated remediation.; those wait on a human), plus unacknowledged overflow findings and unanswered top-level human comments' },
    notes: { type: 'string' },
  },
  required: ['pr_state', 'head_sha', 'has_conflicts', 'behind_main', 'ai_review_done_on_head', 'ci_state', 'ci_red_caused_by_pr', 'actionable_comments', 'notes'],
}
const ACTION_SCHEMA = {
  type: 'object',
  properties: {
    pushed: { type: 'boolean', description: 'true if a commit was pushed to the PR branch this step' },
    handled: { type: 'integer', description: 'items handled (comments replied to / conflicts resolved / CI failures fixed)' },
    remaining: { type: 'integer', description: 'items still needing action after this step' },
    summary: { type: 'string' },
  },
  required: ['pushed', 'handled', 'remaining', 'summary'],
}

const FIX_SCHEMA = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    declined: {
      type: 'array',
      description: 'findings judged wrong and left unfixed, each with the reason',
      items: { type: 'object', properties: { finding: { type: 'string' }, reason: { type: 'string' } }, required: ['finding', 'reason'] },
    },
    gates_green: { type: 'boolean' },
  },
  required: ['summary', 'declined', 'gates_green'],
}
const MERGE_CHECK_SCHEMA = {
  type: 'object',
  properties: {
    blocked: { type: 'boolean', description: 'true if a predecessor is not merged (nothing was changed)' },
    blocked_reason: { type: 'string' },
    merge_check: { type: 'string', description: 'what was committed, what the merge brought in, conflicts resolved, merge-tree result vs origin/main' },
    gates_green: { type: 'boolean' },
    pushed: { type: 'boolean', description: 'true only if the branch was pushed in this step' },
    pushed_sha: { type: 'string' },
  },
  required: ['blocked', 'merge_check', 'gates_green', 'pushed'],
}
const SHIP_PR_SCHEMA = {
  type: 'object',
  properties: {
    pr_number: { type: 'integer' },
    url: { type: 'string' },
    head_branch: { type: 'string', description: 'the PR headRefName' },
    blocked: { type: 'boolean', description: 'true if a predecessor is not merged or the PR could not be opened/readied' },
    blocked_reason: { type: 'string' },
  },
  required: ['blocked'],
}
// Defects block the PR; suggestions and nits go to the fixer but never hold it.
const blocking = list => list.filter(f => f.severity === 'bug' || f.severity === 'concern')

let plan = null, impl = null, pr = null, findings = [], clean = false, reviewClean = SHIP_ONLY ? null : false
let BRANCH = A.branch || null
const done = (status, extra) => ({ branch: BRANCH, plan: plan && plan.plan, impl, review_clean: reviewClean, polish_clean: clean, status, ...extra })
if (MONITOR_ONLY) {
  pr = { pr_number: A.pr, url: `PR #${A.pr}` }
  log(`Monitor-only mode for PR #${A.pr} (${BRANCH})`)
} else {
  if (!SHIP_ONLY) {
    // ---- Plan (the worktree is set up before the skill runs)
    phase('Plan')
    // A given branch (a stacked lane) is set up before the skill runs, and the skill must not switch off it.
    // Otherwise, as #4651 established, the planner runs first and the branch is cut afterwards.
    const tokenRule = `Rewrite any --prefixed token inside the concern text (e.g. "--json" -> "the json flag") so the skill never reads it as one of its own flags. `
    const setup = BRANCH
      ? `First use branch ${BRANCH}${DIR ? ` in ${DIR}` : ''}: if that checkout does not exist, git fetch and create it as a worktree on that branch from origin/${BASES[BASES.length - 1] || 'main'}; if it exists, keep and build on any work already there. ` + STACK
      : ''
    const skillCall = PLANNER === 'plan-proposal'
      ? `${BRANCH ? 'Then i' : 'I'}nvoke the Skill tool with skill "plan-proposal" and args: "${A.proposal_number}". Plan exactly this scope as one increment, whatever the skill would pick by default: ${CONCERN}\n` +
        (BRANCH ? `Stay on branch ${BRANCH}: skip the skill's switch-to-main step. ` : '')
      : BRANCH
        ? `Then invoke the Skill tool with skill "plan" with args "--no-switch " followed by the concern below (keep that leading --no-switch flag; it stops the skill switching off this branch). ${tokenRule}\n${JSON.stringify(CONCERN)}\n`
        : `Invoke the Skill tool with skill "plan" with the concern below as args. ${tokenRule}\n${JSON.stringify(CONCERN)}\n`
    const branchAfter = BRANCH
      ? ''
      : `The planning skill creates no branch; AFTER it finishes, as a separate step, create and check out a feature branch off an up-to-date main (descriptive name, dated). Verify it with git rev-parse --abbrev-ref HEAD (never main). `
    plan = A.plan_text
      ? await agent(
        WORK + setup + `The plan is already written; do NOT invoke a planning skill. Run git rev-parse --abbrev-ref HEAD in that checkout and return its output verbatim as branch (do not switch branches to make it match), leave any uncommitted work there in place, and return plan "given".`,
        opts('setup', 'Plan', 'merge_check', PLAN_SCHEMA),
      ).then(r => {
        if (!r) return null
        // A checkout on another branch must stop the run, never be built on as if it were BRANCH.
        if ((r.branch || '').trim() !== BRANCH) { log(`Setup found ${DIR || 'the checkout'} on ${r.branch || '(unknown)'}, not ${BRANCH}`); return null }
        return { branch: BRANCH, plan: A.plan_text }
      })
      : await agent(
      WORK + setup + skillCall +
      (CONTEXT ? `Scout context already found: ${CONTEXT}\n` : '') +
      `Do NOT stop for interactive approval or plan mode — produce the plan directly. ` + branchAfter +
      `The plan must list each acceptance criterion with its current status, the ordered per-file edits, the tests, and the gate commands. Return the branch and the full plan.`,
      opts('plan', 'Plan', 'plan', PLAN_SCHEMA),
    )
    if (!plan) return done('failed_at_plan')
    BRANCH = plan.branch
    log(`Plan ready on branch ${BRANCH}`)
    const AUTHORITATIVE = `The plan below is authoritative for this branch; ignore any other plan file under ~/.claude/plans.\n\n${plan.plan}\n\n`

    // ---- Implement
    phase('Implement')
    impl = await agent(
      WORK + `You are on branch ${BRANCH}. Invoke the Skill tool with skill "impl", and implement this plan (do not wait for approval). ${AUTHORITATIVE}` +
      `Include/adjust tests. Run the CLAUDE.md quality gates for every package touched. Do not commit.`,
      opts('impl', 'Implement', 'impl', STEP_SCHEMA),
    )
    if (!impl) return done('failed_at_impl')
    log(`Implemented: ${impl.files_changed.join(', ')} (gates ${impl.gates_green ? 'green' : 'RED'})`)

    // ---- Review until no defect remains: every fix is re-reviewed; a defect left at the cap blocks the PR.
    phase('Review')
    const frame = `The intent of the change: ${CONCERN}.` +
      (A.proposal ? ` Judge it against the proposal ${A.proposal} (its steps' text and acceptance criteria) and the plan. ` : ' Judge it against the plan. ') + AUTHORITATIVE
    const history = []
    let fixes = 0, reviewed = false
    for (let round = 1; ; round++) {
      const review = await agent(
        WORK + `On branch ${BRANCH} (changes may be uncommitted), invoke the Skill tool with skill "review-changes"${REVIEW_SCOPE ? ` with args "${REVIEW_SCOPE}"` : ''}.${SCOPE_NOTE} ${frame}` +
        `Report every finding (bug/concern/suggestion/nit), verified against the code. Do not edit files and do not change git state. ` +
        `Set completed=false if the skill stopped without reviewing (an empty findings list must mean a real, clean review).` +
        (history.length ? `\nEarlier rounds and what the fixer did (confirm each is resolved; do not re-report fixed items or ones validly declined below):\n${JSON.stringify(history, null, 1)}` : ''),
        opts(`review-changes-${round}`, 'Review', 'review', REVIEW_SCHEMA),
      )
      if (!review || review.completed === false) { reviewed = false; break }
      reviewed = true
      findings = review.findings
      const defects = blocking(findings)
      log(`Review round ${round}: ${findings.length} findings, ${defects.length} blocking`)
      if (!defects.length && (!findings.length || fixes >= 1 || fixes >= MAX_REVIEW)) { reviewClean = true; break }
      if (fixes >= MAX_REVIEW) break
      const fix = await agent(
        WORK + `On branch ${BRANCH}, validate and fix these review findings; decline any you can verify is wrong and give the reason:\n` +
        JSON.stringify(findings, null, 2) + `\nRe-run the relevant quality gates. Do not commit.`,
        opts(`apply-review-fixes-${round}`, 'Review', 'fix', FIX_SCHEMA),
      )
      if (!fix) { reviewed = false; break }
      fixes++
      history.push({ round, findings, fixed: fix.summary, declined: fix.declined })
    }
    if (!reviewed) {
      log('A review or fix stage failed to report — stopping before any push or PR')
      return done('review_failed', { review_findings: findings })
    }
    if (!reviewClean) {
      log(`${blocking(findings).length} blocking finding(s) left after ${fixes} fix round(s) — stopping before any push or PR`)
      return done('blocked_at_review', { review_findings: findings })
    }

    // ---- Polish until clean
    phase('Polish')
    const MAX_POLISH = A.max_polish ?? 6
    for (let i = 1; i <= MAX_POLISH && !clean; i++) {
      const p = await agent(
        WORK + `On branch ${BRANCH}, invoke the Skill tool with skill "polish". Fix every bug found and run the quality gates. ` +
        `Report how many issues you found and fixed this round (0 if the diff was already clean) and whether gates are green.`,
        opts(`polish-${i}`, 'Polish', 'polish', POLISH_SCHEMA),
      )
      if (!p) continue
      log(`Polish round ${i}: fixed ${p.issues_found_and_fixed}, gates ${p.gates_green ? 'green' : 'RED'}`)
      clean = p.issues_found_and_fixed === 0 && p.gates_green
    }
    if (!clean) log(`Polish did not reach a clean round within ${MAX_POLISH} rounds — proceeding anyway; flag in final report`)
  }

  // ---- Commit, then merge check; push here only in build-only mode
  phase('PR')
  const mc = await agent(
    WORK + AUTH + (SHIP_ONLY ? PREDS_CHECK : '') + `On branch ${BRANCH}:\n` +
    `1. Commit any uncommitted work first (git status must be clean before merging): stage the change's files explicitly, with a message that explains why, ending with:\n${TRAILERS}\n` +
    `2. Merge check: git fetch; merge origin/main${BASES.length ? ' and the latest origin/<base> of each stacked branch' : ''} (never rebase); resolve conflicts preserving both sides' intent and commit the merge. ` + STACK + `\n` +
    (SHIP_ONLY || !clean
      ? `3. Run "./scripts/quality-gates.sh unit" (plus "browser" if dashboard or browser files are in this branch's diff) whatever the merge brought in — ${SHIP_ONLY ? 'this run built nothing, so no earlier green run can be relied on' : 'polish never reached a clean, green round'} — and continue only when green. Confirm git merge-tree against origin/main is clean.\n`
      : `3. If the merge brought in any change, run "./scripts/quality-gates.sh unit" (plus "browser" if dashboard or browser files are in this branch's diff) and continue only when green; if it brought in nothing, polish's last clean round already ran the gates green — say so. Confirm git merge-tree against origin/main is clean.\n`) +
    (BUILD_ONLY
      ? `4. Push the branch (git push -u origin ${BRANCH}; never force, never main) and report the pushed sha. Open no PR.`
      : `4. Do NOT push and do NOT open a PR in this step.`),
    opts('merge-check', 'PR', 'merge_check', MERGE_CHECK_SCHEMA),
  )
  if (!mc) return done('failed_at_merge_check')
  if (mc.blocked) {
    log(`Not shipped: ${mc.blocked_reason || 'a predecessor is not merged'}`)
    return done('blocked_at_predecessors', { merge_check: mc })
  }
  if (!mc.gates_green) {
    log('Merge check did not finish green — stopping before any PR')
    return done('blocked_at_merge_check', { merge_check: mc })
  }
  if (BUILD_ONLY) {
    if (!mc.pushed || !mc.pushed_sha) {
      log('Build finished but the branch was not confirmed pushed')
      return done('failed_at_push', { merge_check: mc })
    }
    log(`Built and pushed ${BRANCH} @${mc.pushed_sha.slice(0, 9)}; no PR opened (build-only)`)
    return done('pushed', { merge_check: mc })
  }
  const opened = await agent(
    GUARD + WHERE + GATES + AUTH + PREDS_CHECK + (A.existing_pr
      ? `PR #${A.existing_pr} should exist for ${BRANCH}: confirm its head is ${BRANCH} and its base is main (else report blocked). Push any unpushed commits with git push -u origin ${BRANCH} (never force). ` +
        `Rewrite its body (gh pr edit --body-file) in the create-pr skill's body format, dropping any stacking/draft wording, and if it is a draft mark it ready (gh pr ready ${A.existing_pr}).`
      : `On branch ${BRANCH}, invoke the Skill tool with skill "create-pr" to open a PR (not a draft) for: ${CONCERN || A.title}. The work is already committed. ` +
        (SHIP_ONLY
          ? `The branch already exists on the remote: push any unpushed commits yourself with git push -u origin ${BRANCH} (never force), skip the skill's steps 1–5 and its confirmation for pushing to an existing remote branch, and run from step 6; if step 6 finds an open draft PR for the branch, mark it ready (gh pr ready) and return it. `
          : `If the checkout has no .env, treat the skill's github-token preflight as satisfied by ambient gh auth, and push with git push -u origin ${BRANCH} when its push helper reports .env not found. `) +
        (A.title ? `Title it "${A.title}". ` : '') + (A.pr_note ? `Include in the body: ${A.pr_note} ` : '') +
        // A caller that names the issues to close owns that list: the skill's branch/plan/commit detection could add an issue the caller dropped.
        (CLOSES_IN_NOTE ? `The closing keywords in that note are the ONLY ones the PR may carry: skip the skill's step 7 issue detection and add no other Fixes/Closes/Resolves line. ` : '')) +
    `Return the PR number, URL and its head branch (gh pr view --json headRefName); set blocked=true if a predecessor is not merged or the PR could not be opened.`,
    opts(A.existing_pr ? 'ready-pr' : 'create-pr', 'PR', 'pr', SHIP_PR_SCHEMA),
  )
  if (!opened || opened.blocked || !opened.pr_number) {
    log(`PR not opened: ${(opened && opened.blocked_reason) || 'stage failed'}`)
    return done('blocked_at_pr', { pr_stage: opened })
  }
  pr = { pr_number: opened.pr_number, url: opened.url }
  BRANCH = opened.head_branch || BRANCH
  log(`PR #${pr.pr_number} open: ${pr.url}`)
}

// ---- Monitor
// Each round: one read-only observe step (waits for CI + the AI review on the current head),
// then only the action steps the observation calls for. A push restarts the cycle on the new head.
phase('Monitor')
const PRN = pr.pr_number
const MAX_ROUNDS = A.max_monitor_rounds ?? A.max_rounds ?? 10
// A conflict-free BEHIND merge restarts CI + AI review (~20 min); on a busy main the PR falls behind
// again meanwhile, so these merges are capped and only taken once nothing else is pending.
const MAX_BEHIND_MERGES = 2
let behindMerges = 0
let quiet = 0
let last = null
const actions = []
for (let r = 1; r <= MAX_ROUNDS; r++) {
  const obs = await agent(
    GUARD + WHERE + AUTH + `Observe PR #${PRN} (branch ${BRANCH}), round ${r}. Read-only: change no files, push nothing, post nothing.\n` +
    `1. Read the head SHA (gh pr view ${PRN} --json headRefOid,state,mergeable,mergeStateStatus). If MERGED/CLOSED, report and stop. If mergeable is CONFLICTING or mergeStateStatus is DIRTY, CI cannot run: skip step 2, report has_conflicts=true, ci_state=none, ai_review_done_on_head=false.\n` +
    `2. Wait for CI and the "AI Code Review" check on that head to complete, WITHOUT foreground sleep, capped at ~20 minutes. ` +
    `Try \`gh pr checks ${PRN} --watch --interval 30\`; if it errors (e.g. 403 on check-runs), poll \`gh run list --branch ${BRANCH} --json name,status,conclusion,headSha\` ` +
    `(or gh pr view --json statusCheckRollup) with the Monitor tool until-loop instead. Review comments land when the AI Code Review run completes, so do not count comments before it has.\n` +
    `3. Count actionable comments per the actionable_comments definition: GraphQL reviewThreads (isResolved, isOutdated, comments with author.login and body), ` +
    `the <!-- maestro:overflow --> block in the latest review body vs the <!-- maestro:overflow-ack --> comment, and top-level human PR comments vs <!-- maestro:human-ack -->. ` +
    `An unresolved thread whose latest reply is a terminal marker is NOT actionable — those stay unresolved for the human.\n` +
    `4. If quality-gate CI is red, read the failing log and judge whether this PR caused it.`,
    opts(`observe-${r}`, 'Monitor', 'monitor', OBSERVE_SCHEMA),
  )
  if (!obs) {
    if (r === MAX_ROUNDS) log(`Monitor hit the ${MAX_ROUNDS}-round cap without settling — check the PR by hand`)
    continue
  }
  last = obs
  log(`Round ${r}: ${last.pr_state} @${last.head_sha.slice(0, 9)}, CI ${last.ci_state}, review ${last.ai_review_done_on_head ? 'done' : 'pending'}, actionable ${last.actionable_comments}, conflicts ${last.has_conflicts}, behind ${last.behind_main}`)
  if (last.pr_state !== 'OPEN') break

  // 'none' right after a push usually means checks have not registered yet, so it never counts as settled.
  const settled = last.ai_review_done_on_head && last.ci_state === 'green'
  // A red CI this PR did not cause may be a since-fixed main; merging main can cure it.
  const redNotOurs = last.ci_state === 'red' && !last.ci_red_caused_by_pr && last.ai_review_done_on_head
  const behindMergeDue = last.behind_main && !last.has_conflicts && (settled || redNotOurs) &&
    last.actionable_comments === 0 && behindMerges < MAX_BEHIND_MERGES
  let pushed = false
  if (last.has_conflicts || behindMergeDue) {
    const m = await agent(
      GUARD + WHERE + GATES + AUTH + `On branch ${BRANCH} (PR #${PRN}), invoke the Skill tool with skill "merge-main". Resolve every conflict, run the quality gates for touched packages, and push to the PR branch (never force-push, never main).`,
      opts(`merge-main-${r}`, 'Monitor', 'monitor', ACTION_SCHEMA),
    )
    if (m) { actions.push({ round: r, step: 'merge-main', ...m }); pushed = pushed || m.pushed }
    // Count every BEHIND attempt: one that pushes nothing (no-op or failed) would otherwise retry every round and never settle.
    if (behindMergeDue) behindMerges++
  }
  if (last.actionable_comments > 0) {
    const c = await agent(
      GUARD + WHERE + GATES + AUTH + `On branch ${BRANCH}, invoke the Skill tool with skill "process-comments" with args "${PRN}". ` +
      `Address EVERY actionable item (AI bug/suggestion/nit threads incl. outdated ones, overflow findings, human comments): fix with tests or decline/answer with reasons, ` +
      `run the gates once, push, then post the terminal-marker replies and verify every item carries one. ` +
      (last.has_conflicts || behindMergeDue ? `A merge-main step just ran; pull the branch first. ` : '') +
      `Do not resolve threads and do not merge the PR.`,
      opts(`process-comments-${r}`, 'Monitor', 'monitor', ACTION_SCHEMA),
    )
    if (c) {
      actions.push({ round: r, step: 'process-comments', ...c })
      pushed = pushed || c.pushed
      if (c.remaining > 0) log(`process-comments left ${c.remaining} item(s) without a reply — will retry next round`)
    }
  }
  if (last.ci_state === 'red' && last.ci_red_caused_by_pr && !pushed) {
    const f = await agent(
      GUARD + WHERE + GATES + AUTH + `CI quality gates are red on PR #${PRN} (branch ${BRANCH}, head ${last.head_sha}) because of this PR. ` +
      `Read the failing run log, fix the root cause (no noqa/type-ignore/skips), run the gates locally, commit and push to the PR branch.`,
      opts(`fix-ci-${r}`, 'Monitor', 'monitor', ACTION_SCHEMA),
    )
    if (f) { actions.push({ round: r, step: 'fix-ci', ...f }); pushed = pushed || f.pushed }
  }

  // Red CI this PR did not cause (flake, broken main) with nothing else to do: no step can change it.
  if (!pushed && last.ci_state === 'red' && !last.ci_red_caused_by_pr && last.ai_review_done_on_head &&
      last.actionable_comments === 0 && !last.has_conflicts && !behindMergeDue) {
    log('CI is red for a reason outside this PR and nothing else is actionable — handing to human (re-run or fix main)')
    break
  }
  // BEHIND alone does not block settling once its capped merges are spent; "Update branch" at merge time covers it.
  const idle = !pushed && !last.has_conflicts && !behindMergeDue && last.actionable_comments === 0 && settled
  quiet = idle ? quiet + 1 : 0
  if (quiet >= 2) {
    log('PR settled with nothing actionable for 2 consecutive rounds — handing to human for merge' +
      (last.behind_main ? ' (still behind main: use "Update branch" when merging)' : ''))
    break
  }
  if (r === MAX_ROUNDS) log(`Monitor hit the ${MAX_ROUNDS}-round cap without settling — check the PR by hand`)
}

return done(last ? last.pr_state : 'monitor_failed', { pr, monitor_actions: actions, final_state: last })

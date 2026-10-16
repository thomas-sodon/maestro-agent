export const meta = {
  name: 'bug-fix-backlog',
  description: 'Triage open bug issues with /bug-fix, adversarially verify each verdict (reproduce + refute), close the confirmed-fixed ones, and take each confirmed bug through plan-to-merged-pr',
  whenToUse: 'Walking the open bug backlog: one lane per issue (or per group of issues that share a code site), each triaged, verified and shipped as its own PR. See .claude/workflows/README.md.',
  phases: [
    { title: 'Triage', detail: '/bug-fix <n> --dry-run per issue, in the lane worktree' },
    { title: 'Verify', detail: 'plan verdicts: failing regression test + skeptic; close verdicts: two refuters' },
    { title: 'Close', detail: 'close only the issues every refuter failed to keep open' },
    { title: 'Ship', detail: 'plan-to-merged-pr with the verified plan (impl → review → polish → PR → monitor)' },
  ],
}

// args: { lanes: [Lane], root?, date?, gate?, commit_trailers?, max_review_rounds?, max_polish?, max_monitor_rounds?, efforts?, ship?, close? }
// Lane: { key, issues: number[], branch?, dir? }
//   issues  — issues fixed together in one PR (group issues that edit the same code site)
//   branch  — default bugfix/<key>-<issues>-<date> (not fix/<NNN>-…, which /create-pr would read as the issue to close);  dir — default <root>/fix-<issues>
//   ship    — default true; false = build only (push, no PR)
//   close   — default true; false = report confirmed-fixed issues instead of closing them
const A = args || {}
if (!A.lanes || !A.lanes.length) throw new Error('bug-fix-backlog: args.lanes is required (see .claude/workflows/README.md)')
const ROOT = A.root || '/opt/maestro-dev/.claude/worktrees'
const DATE = A.date || 'undated'
const GATE = A.gate || './scripts/gate-slot.sh'
const EFF = { triage: 'medium', verify: 'high', close: 'low', ...(A.efforts || {}) }
const SHARED = {}
for (const k of ['gate', 'commit_trailers', 'max_review_rounds', 'max_polish', 'max_monitor_rounds', 'efforts']) if (A[k] !== undefined) SHARED[k] = A[k]
if (A.ship === false) SHARED.ship = false

const GUARD = `If a permission check or classifier denies any action, do NOT retry it through another tool or route (Bash, python, sed, heredocs, a different file path); report it as blocked. `
const AUTH = `For gh/git network commands use scripts/with-env.sh when the checkout has a .env; if it reports .env not found, run gh/git directly (gh is already authenticated, so a skill's missing .env / GITHUB_TOKEN stop does not apply). `
const GATES = `Run every ruff / mypy / pytest invocation through the gate semaphore \`${GATE} <command>\` from the checkout root (exit 75 = every slot busy: re-run; exit 125/127 = report, never bypass). For a raw pytest run pass "-n auto" or, for a single file, "-p no:xdist". Background long runs and wait for them to exit. `
const where = l => `Work ONLY in the git checkout ${l.dir} (cd there / use absolute paths); other agents are using other checkouts, including the main checkout, which you must not touch. `
const nums = l => l.issues.map(n => '#' + n).join(', ')

const TRIAGE_SCHEMA = {
  type: 'object',
  properties: {
    branch: { type: 'string', description: 'branch checked out in the lane worktree' },
    ahead_of_main: { type: 'integer', description: 'git rev-list --count origin/main..HEAD in the lane worktree after git fetch origin, at setup' },
    behind_main: { type: 'integer', description: 'git rev-list --count HEAD..origin/main in the lane worktree after git fetch origin, at setup' },
    baseline_dirty: { type: 'array', items: { type: 'string' }, description: 'git status --porcelain --untracked-files=all lines in the lane worktree at setup, before any skill ran' },
    issues: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          number: { type: 'integer' },
          title: { type: 'string' },
          verdict: { type: 'string', enum: ['plan', 'close', 'skip'], description: 'skip = the skill stopped with an error (closed, claimed by another folder)' },
          linked_open_pr: { type: 'string', description: 'URL of an open PR whose body closes this issue (the skill warns and proceeds); empty when none' },
          evidence: { type: 'string', description: 'the code facts (current path:symbol) behind the verdict' },
          close_reason: { type: 'string', description: "close verdict: the skill's one-line close reason" },
          close_comment: { type: 'string', description: 'close verdict: the full comment body the skill would post' },
          skip_reason: { type: 'string' },
        },
        required: ['number', 'title', 'verdict', 'evidence'],
      },
    },
    plan: { type: 'string', description: 'ONE combined fix plan covering every plan-verdict issue (empty when none): intent, current code locations, ordered per-file edits, the failing regression tests to add first, gate commands, and a "Fixes #N" line per issue' },
  },
  required: ['branch', 'ahead_of_main', 'behind_main', 'baseline_dirty', 'issues', 'plan'],
}
const REPRO_SCHEMA = {
  type: 'object',
  properties: {
    results: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          number: { type: 'integer' },
          reproduced: { type: 'boolean', description: 'true only if a new test fails on today\'s code for exactly the reason the issue states' },
          behaviour_correct: { type: 'boolean', description: 'true only if running today\'s code showed it already behaves as the issue asks (no defect); false when you merely could not build a test that fails for the right reason' },
          tests: { type: 'array', items: { type: 'string' }, description: 'pytest node ids of the regression tests written (left uncommitted in the worktree)' },
          failure_excerpt: { type: 'string' },
          notes: { type: 'string' },
        },
        required: ['number', 'reproduced', 'behaviour_correct', 'tests', 'failure_excerpt', 'notes'],
      },
    },
  },
  required: ['results'],
}
const REFUTE_SCHEMA = {
  type: 'object',
  properties: {
    results: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          number: { type: 'integer' },
          refuted: { type: 'boolean', description: 'true if you found concrete code evidence the verdict under test is wrong' },
          evidence: { type: 'string', description: 'current path:symbol and the reasoning; quote the code' },
        },
        required: ['number', 'refuted', 'evidence'],
      },
    },
  },
  required: ['results'],
}
const CLOSE_SCHEMA = {
  type: 'object',
  properties: {
    closed: { type: 'array', items: { type: 'integer' } },
    not_closed: { type: 'array', items: { type: 'object', properties: { number: { type: 'integer' }, reason: { type: 'string' } }, required: ['number', 'reason'] } },
  },
  required: ['closed', 'not_closed'],
}
const REPLAN_SCHEMA = {
  type: 'object',
  properties: { plan: { type: 'string', description: 'the full combined plan, now also covering the reopened issues' } },
  required: ['plan'],
}

const CLEANUP_SCHEMA = {
  type: 'object',
  properties: {
    dirty: { type: 'array', items: { type: 'string' }, description: 'git status --porcelain lines left in the worktree after the cleanup' },
    unresolved: { type: 'array', items: { type: 'string' }, description: 'test ids or paths that could not be located or safely removed' },
  },
  required: ['dirty', 'unresolved'],
}

const lanes = A.lanes.map(l => {
  if (!l.key || !l.issues || !l.issues.length) throw new Error(`bug-fix-backlog: lane ${l.key || '?'} needs key and issues`)
  const tag = l.issues.join('-')
  return { ...l, branch: l.branch || `bugfix/${l.key}-${tag}-${DATE}`, dir: l.dir || `${ROOT}/fix-${tag}` }
})
// Lanes run concurrently and every per-issue safeguard assumes an issue, a checkout and a branch belong to one lane.
for (const field of ['key', 'dir', 'branch']) {
  const seen = {}
  for (const l of lanes) { if (seen[l[field]]) throw new Error(`bug-fix-backlog: lanes ${seen[l[field]]} and ${l.key} share ${field} ${l[field]}`); seen[l[field]] = l.key }
}
const owner = {}
for (const l of lanes) for (const n of l.issues) { if (owner[n]) throw new Error(`bug-fix-backlog: issue #${n} is in lanes ${owner[n]} and ${l.key}`); owner[n] = l.key }

const results = await pipeline(
  lanes,

  // ---- Triage: the bug-fix skill decides plan or close for each issue, without mutating anything public
  l => agent(
    GUARD + AUTH + where(l) +
    `Set up first: if ${l.dir} does not exist, git fetch origin and create it as a git worktree on a new branch ${l.branch} from origin/main; if it exists, keep it and any work in it. Run git rev-parse --abbrev-ref HEAD there and return its output verbatim as branch (do not switch branches to make it match ${l.branch}; a mismatch stops the lane). Also return git status --porcelain --untracked-files=all there verbatim as baseline_dirty, one line per entry ([] when it prints nothing), and, after git fetch origin, git rev-list --count origin/main..HEAD as ahead_of_main and git rev-list --count HEAD..origin/main as behind_main.\n` +
    `Then, from ${l.dir}, for EACH of ${nums(l)}: invoke the Skill tool with skill "bug-fix" and args "<number> --dry-run". Stay on branch ${l.branch} (skip the skill's switch-to-main step). ` +
    `Dry-run means the skill writes no plan file and closes nothing — keep it that way. Record its verdict per issue (plan / close, or skip if it stopped with an error such as issue_already_closed or issue_claimed_by_other_folder), exactly one entry per issue. ` +
    `If the skill's open-PR precheck warns that an open PR already closes the issue, record that PR's URL as linked_open_pr (it proceeds anyway; this workflow does not).\n` +
    `Then write ONE combined plan for every plan-verdict issue, at today's code locations, in the skill's plan layout: the failing regression test(s) to add first (mem_fs, mocked externals, per CLAUDE.md Testing Principles), the ordered per-file edits, the acceptance criteria, and the gate commands; end it with a "Fixes #N" line per plan-verdict issue. ` +
    (l.issues.length > 1 ? `These issues share a code site: design one fix that resolves all of them rather than stacking patches. ` : '') +
    `Do NOT edit any repo file in this step.`,
    { label: `triage:${l.key}`, phase: 'Triage', effort: EFF.triage, schema: TRIAGE_SCHEMA },
  ),

  // ---- Verify: every verdict faces an independent check before it is acted on
  async (tri, l) => {
    if (!tri) return { lane: l, status: 'failed_at_triage' }
    if ((tri.branch || '').trim() !== l.branch) { log(`${l.key}: ${l.dir} is on ${tri.branch || '(unknown)'}, not ${l.branch}`); return { lane: l, status: 'branch_mismatch' } }
    // Only the lane's own issues count, and every one of them must come out of this lane somewhere.
    const rows = tri.issues.filter(i => l.issues.includes(i.number))
    const missing = l.issues.filter(n => !rows.some(i => i.number === n))
    if (missing.length) log(`${l.key}: triage returned no verdict for ${missing.map(n => '#' + n).join(', ')}`)
    // One row per issue: rows that disagree on the verdict could both close an issue and claim to fix it, so neither is acted on.
    const conflicting = l.issues.filter(n => new Set(rows.filter(i => i.number === n).map(i => i.verdict)).size > 1)
    const triaged = l.issues.filter(n => !conflicting.includes(n)).map(n => {
      const same = rows.filter(i => i.number === n)
      return same.length && { ...same[0], linked_open_pr: same.map(i => i.linked_open_pr).find(Boolean) || '' }
    }).filter(Boolean)
    // An issue an open PR already closes is that PR's to fix or close: shipping it (even after a refuted close) would open a competing PR.
    const linked = triaged.filter(i => i.verdict !== 'skip' && i.linked_open_pr)
    // A plan verdict judged on a tree behind origin/main cannot see fixes that landed since, so shipping it could re-fix a bug main no longer has.
    const planRows = triaged.filter(i => i.verdict === 'plan' && !i.linked_open_pr)
    const stalePlans = tri.behind_main === 0 ? [] : planRows
    const planned = planRows.filter(i => !stalePlans.includes(i))
    // A close is public and claims the fix is on main, so it is judged only in a worktree that is exactly origin/main:
    // a kept worktree holding an earlier run's unlanded fix would show the bug as gone when main still has it.
    tri.baseline_dirty = (tri.baseline_dirty || []).filter(b => b.trim())
    const atMain = tri.ahead_of_main === 0 && tri.behind_main === 0 && !tri.baseline_dirty.length
    const closeRows = triaged.filter(i => i.verdict === 'close' && !i.linked_open_pr)
    const unclosable = closeRows.filter(i => !atMain || !(i.close_comment || '').trim())
    const closing = closeRows.filter(i => !unclosable.includes(i))
    const brief = list => JSON.stringify(list.map(i => ({ number: i.number, title: i.title, evidence: i.evidence, close_reason: i.close_reason })), null, 1)
    const thunks = []
    if (planned.length) {
      // Reproduce lens: the failing test the fix is later held to (CLAUDE.md: bugs need a failing test first).
      thunks.push(() => agent(
        GUARD + where(l) + GATES +
        `On branch ${tri.branch}, for each issue below write the failing regression test(s) the plan calls for, in the right tests/ module (mem_fs fixture, mocked externals, no real sleep, simple asserts). ` +
        `Run them and confirm each fails on today's code for EXACTLY the reason the issue states — a failure for any other reason (import error, wrong fixture, a typo) does not count; fix the test until it fails for the right reason, or report reproduced=false; set behaviour_correct=true only if running the code showed the behaviour is in fact correct. ` +
        `Change NO production code. Leave the tests uncommitted in the worktree — the implementer builds on them.\nIssues:\n${brief(planned)}\nPlan:\n${tri.plan}`,
        { label: `reproduce:${l.key}`, phase: 'Verify', effort: EFF.verify, schema: REPRO_SCHEMA },
      ).then(r => ({ kind: 'repro', r })))
      // Skeptic lens: is the bug real and reachable at all?
      thunks.push(() => agent(
        GUARD + where(l) +
        `Read-only (edit nothing, change no git state). Try to REFUTE that each bug below is real in today's code at ${l.dir}: look for an upstream guard that already prevents it, a caller that never produces the triggering input, a recent commit that fixed it (git fetch origin, then git log -S / -G since the issue date on both HEAD and origin/main, reading origin/main with git show origin/main:<path>), or a misread of the code. ` +
        `Set refuted=true only with concrete quoted code evidence; if the bug is real, refuted=false.\n${brief(planned)}`,
        { label: `skeptic:${l.key}`, phase: 'Verify', effort: EFF.verify, schema: REFUTE_SCHEMA },
      ).then(r => ({ kind: 'skeptic', r })))
    }
    // Two independent refuters per close verdict: a wrong close is public, so it needs both to fail.
    for (const lens of ['trace the issue\'s failure scenario through today\'s call path end to end', 'search for sibling sites and alternative inputs that still exhibit the bug'])
      if (closing.length) thunks.push(() => agent(
        GUARD + where(l) +
        `Read-only (edit nothing, change no git state). The bug-fix triage concluded each issue below is ALREADY FIXED (or its surface removed) and wants to close it. Try to REFUTE that: ${lens}. ` +
        `Fetch each issue body with gh issue view <n> --json body. Set refuted=true if the described behaviour (by intent, not literal line numbers) still occurs anywhere in origin/main (git fetch origin, then read it with git show origin/main:<path> / git grep <pattern> origin/main), quoting the code; a fix present only in the working tree or in commits not on origin/main does not count.\n${brief(closing)}`,
        { label: `refute-close:${l.key}`, phase: 'Verify', effort: EFF.verify, schema: REFUTE_SCHEMA },
      ).then(r => ({ kind: 'refute', r })))
    const checks = (await parallel(thunks)).filter(Boolean)
    const of = kind => checks.filter(c => c.kind === kind && c.r).flatMap(c => c.r.results)
    const repro = of('repro'), skeptic = of('skeptic')

    const confirmedBugs = [], confirmedFixed = [], reopened = []
    const needsHuman = [
      ...missing.map(n => ({ number: n, why: 'absent from triage output' })),
      ...conflicting.map(n => ({ number: n, why: 'conflicting triage verdicts' })),
      ...unclosable.map(i => ({ number: i.number, why: atMain ? 'close verdict missing close comment' : 'close verdict judged in a worktree that differs from origin/main' })),
      ...stalePlans.map(i => ({ number: i.number, why: 'plan verdict judged in a worktree behind origin/main' })),
    ]
    for (const i of planned) {
      const rep = repro.find(x => x.number === i.number)
      const sk = skeptic.find(x => x.number === i.number)
      // A reproduction is decisive; otherwise the bug survives only if the skeptic could not refute it.
      if (rep && rep.reproduced) confirmedBugs.push({ ...i, tests: rep.tests })
      else if (rep && rep.behaviour_correct) needsHuman.push({ number: i.number, why: 'reproducer ran the code and found the behaviour correct', repro: rep, skeptic: sk, tests: rep.tests || [] })
      else if (sk && !sk.refuted) confirmedBugs.push({ ...i, tests: [], unreproduced_tests: (rep && rep.tests) || [], unreported: !rep, note: 'not reproduced by a test; skeptic could not refute' })
      else needsHuman.push({ number: i.number, why: 'plan verdict neither reproduced nor survived the skeptic', repro: rep, skeptic: sk, tests: (rep && rep.tests) || [] })
    }
    // One vote per refuter report, never per result row, so a refuter listing an issue twice cannot stand in for two.
    const refuteReports = checks.filter(c => c.kind === 'refute' && c.r)
    for (const i of closing) {
      const votes = refuteReports.map(c => c.r.results.find(x => x.number === i.number)).filter(Boolean)
      if (votes.length < 2) needsHuman.push({ number: i.number, why: 'close verdict lacked two refuter reports' })
      else if (votes.some(v => v.refuted)) reopened.push({ ...i, refutations: votes.filter(v => v.refuted) })
      else confirmedFixed.push(i)
    }
    log(`${l.key}: ${confirmedBugs.length} confirmed bug(s), ${confirmedFixed.length} confirmed fixed, ${reopened.length} close verdict(s) refuted, ${needsHuman.length} for a human`)
    return { lane: l, tri, planned, missing, conflicting, confirmedBugs, confirmedFixed, reopened, needsHuman, skipped: [...triaged.filter(i => i.verdict === 'skip'), ...linked.map(i => ({ ...i, skip_reason: `linked_open_pr:${i.linked_open_pr}` }))] }
  },

  // ---- Close confirmed-fixed issues (with the race re-check), and fold refuted closes back into the plan
  async v => {
    if (!v.tri) return v
    const l = v.lane
    let plan = v.tri.plan
    if (v.reopened.length) {
      const r = await agent(
        GUARD + where(l) +
        `Read-only. The triage judged these issues fixed, but a refuter showed the bug still occurs:\n${JSON.stringify(v.reopened, null, 1)}\n` +
        `Rewrite the combined plan below so it also fixes them (failing regression test first, per-file edits, gates, a "Fixes #N" line each). Return the full plan.\n\n${plan || '(no plan yet)'}`,
        { label: `replan:${l.key}`, phase: 'Verify', effort: EFF.triage, schema: REPLAN_SCHEMA },
      )
      if (r && r.plan) { plan = r.plan; v.confirmedBugs.push(...v.reopened.map(i => ({ ...i, tests: [], note: 'reopened by a refuter' }))) }
      else v.needsHuman.push(...v.reopened.map(i => ({ number: i.number, why: 'close verdict refuted; re-plan failed' })))
    }
    let closed = { closed: [], not_closed: [] }
    if (v.confirmedFixed.length && A.close !== false) {
      closed = (await agent(
        GUARD + AUTH + where(l) +
        `Close each issue below as already fixed. Immediately before each close, re-check: gh issue view <n> --json state (skip it if no longer OPEN), and gh pr list --state open --search "<n> in:body" --json number,body — skip it if an open PR body has a closing keyword for it. ` +
        `Close with gh issue close <n> --reason completed --comment "$(cat <file>)" (gh issue close has no --comment-file flag), writing the comment (the triage's close comment, which quotes the evidence, ending "Auto-closed by /bug-fix after verifying intent against current main; two independent refuters found no remaining instance.") to a file in your scratchpad first. Change nothing else on the issue.\n` +
        JSON.stringify(v.confirmedFixed.map(i => ({ number: i.number, close_reason: i.close_reason, close_comment: i.close_comment })), null, 1),
        { label: `close:${l.key}`, phase: 'Close', effort: EFF.close, schema: CLOSE_SCHEMA },
      )) || { closed: [], not_closed: v.confirmedFixed.map(i => ({ number: i.number, reason: 'close stage failed' })) }
      if (closed.closed.length) log(`${l.key}: closed ${closed.closed.map(n => '#' + n).join(', ')}`)
    }
    return { ...v, plan, closed }
  },

  // ---- Ship the confirmed bugs through the shared engine
  async v => {
    if (!v.tri) return { lane: v.lane.key, status: v.status || 'failed_at_triage' }
    const l = v.lane
    const summary = {
      lane: l.key, branch: l.branch, dir: l.dir,
      closed: v.closed.closed, not_closed: v.closed.not_closed,
      confirmed_fixed_unclosed: A.close === false ? v.confirmedFixed.map(i => i.number) : [],
      needs_human: v.needsHuman, skipped: v.skipped.map(i => ({ number: i.number, reason: i.skip_reason })),
      bugs: v.confirmedBugs.map(i => ({ number: i.number, tests: i.tests, note: i.note })),
    }
    const humanTests = v.needsHuman.flatMap(h => h.tests || [])
    // Triage edits nothing and the other checks are read-only, so a tests/ file dirty now but not at setup is the reproducer's;
    // one already dirty at setup (a kept worktree) holds earlier work and is never touched.
    const baseline = v.tri.baseline_dirty || []
    const before = `Before the reproducer ran, git status --porcelain --untracked-files=all in the worktree read: ${baseline.length ? JSON.stringify(baseline) : '(clean)'}. `
    const sparing = before + `Never touch a file listed there (or under a directory listed there); report a test in such a file as unresolved instead. `
    // For the implementer the guard limits only its deletions: the plan's own edits may well target a file that was dirty at setup.
    const deleteSparing = baseline.length ? ' ' + before + 'When deleting these tests, do not delete or revert anything in a file listed there or under a directory listed there (it holds work from before this run); leave such a test in place and say so in your summary. This does not restrict the plan\'s edits.' : ''
    if (!v.confirmedBugs.length) {
      // Nothing reaches an implementer, so nobody else removes the reproducer's uncommitted tests, and a re-run keeps the worktree.
      // The reproducer ran whenever a plan verdict existed, even if it reported no test ids (it may have failed after writing them).
      if (!v.planned.length) return { ...summary, status: 'nothing_to_ship' }
      const c = await agent(
        GUARD + where(l) +
        `A reproducer wrote regression tests in this worktree for ${v.planned.map(i => '#' + i.number).join(', ')}, none of which will ship. Remove them. ` +
        `Recorded test ids: ${humanTests.join(', ') || 'none (the reproducer may have failed before reporting)'}. ` + sparing +
        `Every other file under tests/ that git status now shows as changed was changed only by the reproducer: git restore it if tracked, delete it if untracked. ` +
        `Change nothing else and commit nothing. ` +
        `Report each test you could not resolve as unresolved, then return git status --porcelain (run in ${l.dir}) verbatim as dirty, one line per entry.`,
        { label: `cleanup:${l.key}`, phase: 'Ship', effort: EFF.close, schema: CLEANUP_SCHEMA },
      )
      if (!c) { log(`${l.key}: cleanup failed; reproducer tests may remain uncommitted in ${l.dir}`); return { ...summary, status: 'nothing_to_ship', cleanup_failed: true, uncommitted_tests: humanTests } }
      return { ...summary, status: 'nothing_to_ship', dirty: c.dirty, unresolved_tests: c.unresolved }
    }
    // Only the confirmed bugs are claimed fixed; a dropped issue's plan section is removed by the implementer.
    const kept = v.confirmedBugs.map(i => i.number)
    // Every issue the triage plan may cover but this PR will not claim: unconfirmed or already-linked plan verdicts, out-of-lane verdicts, lane issues triage left out or contradicted.
    const dropped = [...new Set([...v.tri.issues.filter(i => i.verdict === 'plan').map(i => i.number), ...v.missing, ...v.conflicting])].filter(n => !kept.includes(n))
    const concern = `Fix ${v.confirmedBugs.map(i => `#${i.number} (${i.title})`).join('; ')}`
    const tests = v.confirmedBugs.flatMap(i => i.tests)
    // Tests the reproducer wrote but could not make fail for the stated reason stay uncommitted; they must not ship.
    const unreproduced = v.confirmedBugs.flatMap(i => i.unreproduced_tests || [])
    // The reproducer may have written tests and then failed to report them; nothing else names those files to the implementer.
    const unreported = v.confirmedBugs.filter(i => i.unreported).map(i => i.number)
    // Their cleanup is scoped by file: the kept tests' files may also hold fixtures and helpers those tests need.
    const keptFiles = [...new Set(tests.map(t => t.split('::')[0]))]
    // Tests the implementer is told to delete but must spare because their file held earlier work; surfaced so a human can tidy them.
    const spared = [...unreproduced, ...(dropped.length ? humanTests : [])].filter(t => baseline.some(b => { const f = b.trim().replace(/^\S{1,2}\s+/, '').split(' -> ').pop().trim(); return t.split('::')[0] === f || (f.endsWith('/') && t.startsWith(f)) }))
    const planText = v.plan +
      (tests.length ? `\n\n## Regression tests already written (uncommitted in the worktree, failing on today's code — keep them; they must pass after the fix)\n${tests.map(t => '- ' + t).join('\n')}\n` : '') +
      (unreproduced.length ? `\n\nDELETE these tests the reproducer wrote but could not make fail for the issue's reason (uncommitted in the worktree; write the plan's regression tests afresh): ${unreproduced.join(', ')}.${deleteSparing}\n` : '') +
      (unreported.length ? `\n\nThe reproducer for ${unreported.map(n => '#' + n).join(', ')} may have left uncommitted tests it never reported: delete any uncommitted change under tests/ outside ${keptFiles.length ? `the files holding the tests listed above as already written (${keptFiles.join(', ')}); inside those files remove only test functions not listed, keeping every fixture, helper and import the listed tests use, and never remove a conftest or helper change a listed test depends on` : 'nothing already written (no tests were listed)'}. Then write the plan's regression tests afresh.${deleteSparing}\n` : '') +
      (dropped.length ? `\n\nOUT OF SCOPE: drop the plan sections for ${dropped.map(n => '#' + n).join(', ')} (not confirmed), delete any regression test written for them (${humanTests.join(', ') || 'none recorded; the reproducer may have failed before reporting, so check tests/ for uncommitted ones written for them'}), and do not claim to fix them.${deleteSparing}\n` : '')
    let r = null
    try {
      r = await workflow('plan-to-merged-pr', {
        ...SHARED,
        concern,
        plan_text: planText,
        dir: l.dir,
        branch: l.branch,
        pr_note: kept.map(n => `Fixes #${n}`).join('\n'),
      })
    } catch (err) {
      log(`${l.key}: ${err && err.message ? err.message : err}`)
    }
    const status = (r && r.status) || 'failed'
    log(`${l.key}: ${status}${r && r.pr ? ` (${r.pr.url})` : ''}`)
    return { ...summary, status, spared_tests: spared, pr: r && r.pr, review_clean: r && r.review_clean, polish_clean: r && r.polish_clean, final_state: r && r.final_state }
  },
)

const dropped = lanes.filter((_, i) => !results[i]).map(l => l.key)
if (dropped.length) log(`Lanes that failed outright: ${dropped.join(', ')}`)
return results.map((r, i) => r || { lane: lanes[i].key, status: 'failed' })

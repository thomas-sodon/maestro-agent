export const meta = {
  name: 'proposal-build-chain',
  description: 'Build a chain of stacked proposal lanes, each through plan-to-merged-pr in build-only mode (/plan-proposal → /impl → review/fix until clean → polish → merge check → push, no PR)',
  whenToUse: 'Implementing proposal steps as stacked lanes; each lane is later shipped with plan-to-merged-pr {ship_only: true} once every PR below it has merged. See .claude/workflows/README.md.',
  phases: [
    { title: 'Lanes', detail: 'one plan-to-merged-pr build per lane, in stack order' },
  ],
}

// args: { lanes: [Lane], gate?, commit_trailers?, max_review_rounds?, max_polish?, efforts? }  (the shared keys pass through to every lane)
// Lane: { key, dir, branch, bases?: string[], proposal, number, concern }
//   number   — the proposal number (the /plan-proposal argument)
//   concern  — the steps in scope for this lane, planned as one increment
//   bases    — branches this lane stacks on (the earlier lanes' branches, still unmerged)
const A = args || {}
if (!A.lanes || !A.lanes.length) throw new Error('proposal-build-chain: args.lanes is required (see .claude/workflows/README.md)')
const SHARED = {}
for (const k of ['gate', 'commit_trailers', 'max_review_rounds', 'max_polish', 'efforts']) if (A[k] !== undefined) SHARED[k] = A[k]

phase('Lanes')
const results = []
for (const l of A.lanes) {
  if (!l.key || !l.dir || !l.branch || !l.concern || !l.number) throw new Error(`proposal-build-chain: lane ${l.key || '?'} needs key, dir, branch, number and concern`)
  log(`Lane ${l.key}: building ${l.branch}${l.bases && l.bases.length ? ` on ${l.bases.join(', ')}` : ''}`)
  let r = null
  try {
    r = await workflow('plan-to-merged-pr', {
    ...SHARED,
    planner: 'plan-proposal',
    proposal_number: l.number,
    concern: l.concern,
    proposal: l.proposal,
    dir: l.dir,
    branch: l.branch,
    bases: l.bases || [],
    ship: false,
    })
  } catch (err) {
    log(`Lane ${l.key}: ${err && err.message ? err.message : err}`)
  }
  const status = (r && r.status) || 'failed'
  results.push({ lane: l.key, branch: l.branch, status, result: r })
  log(`Lane ${l.key}: ${status}`)
  // A later lane stacks on this one; never build it on a branch that did not get pushed.
  if (status !== 'pushed') break
}
return results

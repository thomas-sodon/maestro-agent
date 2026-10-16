Tell me, bluntly, what the fuck Maestro has actually been spending its time and money on.

This is a read-only forensic report over the **running loop's** persisted ledgers — no git mutations, no SDK calls, no code-host calls, no edits. It answers one question: for the requested window, where did the iterations, the dollars, and the wall-clock go, and what — if anything — came out the other end.

`$ARGUMENTS` is optional:
- A window: `24h` (default), `7d`, `90m`, `2w`, or `all`.
- `--data-dir <dir>` to point at a different state directory. The default comes from `$MAESTRO_DATA_DIR` when that is set in the environment, otherwise the live loop's `/opt/maestro/data`, falling back to `./data`. Each ledger is read hot tier plus its `Memory.archive()` cold sibling, so "all time" survives a dream-cycle rotation.
- `--top <n>` to widen the ranked tables (default 10).

The digest reads JSONL only. When `[paths] sqlite_state_enabled` has moved a ledger into `state.db`, the empty sidecar is flagged with a `!!` banner — carry that into the report as *unmeasured*, never as a measured zero.

Anything unrecognized: warn and use the defaults.

## Steps

1. **Run the digest.** `python3.14 scripts/wtf.py $ARGUMENTS` from the repo root. It prints the pulse, cost-by-caller, iterations, tasks, plan fates, gate/audit/deed firings, journal mix, and the current self-model. If it errors (missing data dir, unparseable window), surface the one-line message and stop — do not go rummaging through the ledgers by hand.

2. **Add the two facts the ledgers can't see.** Cheap, read-only, and skip either with a noted reason if it fails.

   A PR is the loop's **own** iff its `headRefName` starts with `maestro/evolve/` — that prefix is the ownership test the loop itself applies (`vcs.py::get_open_evolution_pr_states`, called with that literal at every site). Filter on it, never on `--author @me`: the `GITHUB_TOKEN` is shared with the human-paced sessions, so `@me` resolves to whichever account holds the token and returns the *wrong* set — hand-authored `feat/*` PRs the loop cannot see, while omitting the `maestro/evolve/*` PRs it is actually working. `gh` supplies its own `--jq`; a system `jq` is not needed.

   - Open own PRs — how much of the "output" is still sitting unmerged:
     ```
     scripts/with-env.sh gh pr list --state open --limit 100 \
       --json number,title,headRefName,createdAt,isDraft,mergeStateStatus \
       --jq '[.[] | select(.headRefName | startswith("maestro/evolve/"))]'
     ```
   - Merged in window — filter to the window yourself:
     ```
     scripts/with-env.sh gh pr list --state merged --limit 60 \
       --json number,title,headRefName,mergedAt \
       --jq '[.[] | select(.headRefName | startswith("maestro/evolve/"))]'
     ```

   Report both counts as *owned*. If you also want the human-paced PRs for contrast, list them as a separate line and label them as such — they are not the loop's output and must never be folded into its spend-vs-landed ratio, nor read as loop rot when they go stale.

3. **Report.** Lead with the verdict, not the tables. Structure:
   - **One line up top:** what it spent (dollars, iterations, hours) and what landed (merged PRs / merged plans). If those two numbers are wildly out of proportion, say so in plain words.
   - **Top consumers:** the three or four callers that ate the budget, with the share. Name what each one *is* (e.g. `auto_fix`/`self_review` = fixing and re-reviewing its own implementation attempts; `execute_step` = task work; `introspect`/`contemplate` = thinking about itself).
   - **Output:** plans minted vs. merged vs. killed, with the actual kill reasons quoted. All-time status breakdown for context — a rounding-error merge rate is the headline, not a footnote.
   - **Loops and stalls:** repeated audit findings, gates firing on the same thing, remediation churn, stale directives, any `!!` line from the pulse section. This is where "spending its time" usually turns out to mean "re-litigating the same thing".
   - **What it thinks it's doing:** the current `next_task_directive`, stage, and the open questions — against what the ledgers say it actually did.
   - **Close with a one-line answer** to the literal question, and at most three concrete things worth doing about it.

   Be direct and specific. Quote real numbers and real strings from the digest — no hedging, no filler, and no inventing a charitable explanation the data doesn't support. If the honest answer is that it burned $N re-reviewing its own rejected proposals, say exactly that.

## What this command does NOT do

- Does not modify any file, commit, push, or touch the running loop's state.
- Does not call the SDK, spend budget, or start/stop the loop or dashboard.
- Does not fix anything it finds — it reports. Route follow-ups to `/plan`, `/bug-hunt`, or `/review-plans`.

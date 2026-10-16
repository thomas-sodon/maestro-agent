# Maestro CLI

Run `maestro --help` (and `maestro <command> --help`) for the authoritative list of subcommands; this page groups the common ones.

## Quick Start

```bash
# Check Maestro's current state
maestro status

# Start continuous autonomous mode — Maestro contemplates and evolves on its own
maestro loop

# Or step through manually:
maestro contemplate         # Self-reflection
maestro evolve              # Trigger an evolution cycle (self-review + priority-aware)
maestro issues              # List open GitHub issues

# Optionally give Maestro a specific task
maestro run "Analyze the test suite and suggest improvements"

# Read the last 10 journal entries (inner thoughts)
maestro journal -n 10

# Read operational records (deploys, merges, lifecycle, audit findings)
maestro journal --events -n 20

# View logs
maestro logs                 # Last 50 lines of app log
maestro logs -f              # Follow app log in real-time
maestro logs --sdk           # View most recent SDK session log
maestro logs --sdk --list    # List SDK sessions by date

# Start the web dashboard to monitor and approve evolution plans
maestro serve

# Stop processes gracefully
maestro shutdown             # Stop the loop
maestro shutdown --dashboard # Stop the dashboard
maestro shutdown --all       # Stop both
```

## Read surfaces & operations

Read-only surfaces over Maestro's persisted state, plus operational and email commands. All are additive to the [Quick Start](#quick-start) set above. The list is a curated sample: run `maestro --help` (and `maestro <command> --help`) for every subcommand, including the ledger and `measure-*` surfaces summarised in the CLI bullet of [CLAUDE.md](../CLAUDE.md#cli-configuration-and-secrets).

**Read surfaces over persisted state** — inspect history, reviews, and proposal records:

```bash
maestro history                       # View task history
maestro history -n 20 --outcome success --source self_directed
maestro history --json                # Full TaskRecord data as a JSON array

maestro reviews                       # View artifact review verdicts
maestro reviews --verdict publish --json   # Filter (publish|improve|reuse|skip)

maestro publications                  # View publication proposals (proposal 032)
maestro publications --status pending_approval --json

maestro docs                          # View historical document-proposal records (retired pipeline)
maestro docs --type security --status pr_opened --task <task_id> --json

maestro messages -n 20                # Show recent Maestro↔Thomas messages
```

**Measurement surfaces** — zero-cost metrics over `task_history.jsonl`, `usage.jsonl`, and remediation state:

```bash
maestro measure-noops --last 100          # No-op task rate from history
maestro measure-fork-authorship           # Self-authored vs externally-grounded forks
maestro measure-false-success --last 100  # PRs stamped success >=2x without closing
maestro measure-outcome-trends -n 20      # Success/partial/investigation rate trends
maestro measure-handoff-relapse -n 50     # Tasks re-engaging a handed-off PR lineage
maestro measure-relap --last 100          # Settled-relap rate among no-ops
maestro measure-confirmed-landings --last 100  # Confirmed-vs-caused landing rate
maestro gate-scope maestro/red_root.py     # Test modules a changed diff can reach
maestro gate-scope --plan <plan_id> --json # …the same, from a plan's files_affected
maestro reality-inventory --json          # Shipped capabilities + measured problems
maestro source-budget --json              # Source files at or above the split-due size line
maestro runtime-contract [--json] [--network]  # Check the environment against deploy/runtime_contract.toml
maestro measure-rejection-classes --last 30  # Rejections reading already-shipped / false-premise / not-needed
maestro pr-laps -n 50                      # Per-PR remediation lap counts + standdowns
maestro burn-baseline --since 2026-07-01   # $/task baseline + prompt-cache hit rate
maestro burn-baseline --compare 2026-07-01 # Split the corpus into a before/after/Δ table
```

**Operational** — task resume, config validation, and deploy signalling:

```bash
maestro resume-task <task_id>         # Resume a checkpointed task by id
maestro preflight                     # Validate config, state, and imports
maestro notify-pre-deploy --ref <sha> --from-ref <sha> --pr 42 --run-id <id>
                                      # Notify the running loop a deploy is starting
```

**Email** — send and receive via `MAESTRO_EMAIL_*` (requires the `email` extra):

```bash
maestro email send <to> <subject> <body>   # Send an email
maestro email inbox -n 20 --all             # List inbox (--all includes seen mail)
maestro email read <uid>                    # Read a specific email
```

## Self-model curation

Export the raw self-model, hand-edit it, and import it back with the context-bloat caps applied on write:

```bash
maestro self-model export sm.json           # Dump raw, untruncated self_model.json
maestro self-model import sm.json --dry-run  # Preview what the caps would truncate/drop
maestro self-model import sm.json            # Validate, apply caps, and write it back
```

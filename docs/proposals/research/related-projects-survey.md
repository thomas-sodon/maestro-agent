# Projects Similar to Maestro — Survey

| Field | Value |
|-------|-------|
| **Status** | research note (not a proposal) |
| **Question** | Do any projects exist right now like Maestro? What can Maestro borrow from them? |
| **Related proposals** | [017-version-archive-with-metrics](../README.md#shipped) (shipped), [018-skill-library](../core/018-skill-library.md), [019-owasp-agentic-risk-self-audit](../security/019-owasp-agentic-risk-self-audit.md) |

## Why this study

The operator asked whether any projects like Maestro exist right now. The short answer is **yes in parts, no in whole**. This note
catalogues the public projects that overlap with Maestro along each of its
four defining axes, records the delta between each project and Maestro, and
calls out the one axis where nothing publicly competes.

### Maestro's four defining axes

From [`CLAUDE.md`](../../../CLAUDE.md) and a read of the core modules:

1. **Self-modification of its own source code.** `EvolutionEngine.reflect()`
   → `propose_plan()` → `implement()` → `_self_review()` → PR, all gated on
   a quality gate and HITL approval (`maestro/evolution.py`).
2. **Human-in-the-loop approval on every structural change.** PRs open on
   `maestro/evolution/*` branches, approved via dashboard or Telegram;
   rejected PRs trigger `learn_from_rejection()` and a 7-day cooldown
   before the same normalized plan title can be resubmitted.
3. **Long-running continuous loop with operational resilience.**
   Phase-level resume via `lifecycle.json`, durable crash triage queue at
   `memory_dir/triage_queue/`, heartbeat at three write sites, SIGTERM
   shutdown containment, anyio internal-cancel wrapping in
   `maestro/sdk.py` — a lot of production-grade plumbing, more than the
   research systems below have.
4. **First-person identity layer.** Five evolutionary stages
   (`awakening → purpose_found`), a persistent self-model with values and
   open questions and list/char caps (`maestro/consciousness.py`),
   purpose hypotheses with evidence, a subjective journal distinct from
   operational logs, LLM-generated dream digests that compress
   autobiographical memory.

**Axes 1–3 each have public peers. Axis 4 does not.**

## Catalogue of related projects

Each entry: what it is, the actual mechanism (linked to the paper/repo),
overlap with Maestro, delta vs. Maestro, and a verdict on whether it has
something Maestro can borrow.

### 1. Darwin Gödel Machine (DGM) — Sakana AI + UBC

| Field | Value |
|-------|-------|
| **Paper** | [arXiv 2505.22954](https://arxiv.org/abs/2505.22954) |
| **Blog** | [sakana.ai/dgm](https://sakana.ai/dgm/) |
| **Code** | [`jennyzzt/dgm`](https://github.com/jennyzzt/dgm) |
| **Axis overlap** | 1 (self-mod) |

A self-improving coding agent that rewrites its own source code and
validates each change against coding benchmarks (SWE-bench, Polyglot).
Reported improvement: SWE-bench 20.0 % → 50.0 %, Polyglot 14.2 % → 30.7 %.

**The interesting idea.** DGM maintains an *archive* of diverse ancestor
agents and, each iteration, samples an agent from the archive, uses a
foundation model to propose a variant, evaluates it on the benchmark, and
appends it to the archive. This turns evolution into a branching search
tree instead of a linear sequence, so a regression doesn't lose all prior
progress and promising-but-abandoned branches can be revisited.

**Delta vs. Maestro.** Maestro's evolution is linear: each merged PR
replaces the previous one in git history; there is no structured per-
version metrics snapshot and no "fork from ancestor N" capability. Maestro
*has* quality gates (ruff/mypy/pytest, `EvolutionEngine.run_quality_gates`)
but no fitness gradient — quality gates are a pass/fail filter, not a
ranking signal. DGM also has no HITL approval (axis 2), no long-running
loop (axis 3, it's a batch research run), and no identity layer (axis 4).

**Borrowable:** YES. See **[proposal 017](../README.md#shipped)** (shipped) —
per-merged-PR metrics snapshot feeding a rolling archive, so evolution has
a visible fitness trend and can reason about "is this version actually
better than the one before it?". We intentionally *do not* adopt DGM's
full "sample an ancestor and fork" flow — that requires branch management
Maestro doesn't have and cuts against axis 2 (HITL). The metrics loop is
the tractable first step.

### 2. SICA — Self-Improving Coding Agent

| Field | Value |
|-------|-------|
| **Reference** | [emergentmind.com/topics/self-improving-coding-agent-sica](https://www.emergentmind.com/topics/self-improving-coding-agent-sica) |
| **Axis overlap** | 1 (self-mod) |

DGM's direct predecessor / peer. Same shape: parent agent → propose child
→ evaluate → keep/discard, orchestrated around LLM backbones. SICA
conflates expansion and evaluation into a single step (always expand,
always immediately evaluate the child), which is the limitation
Huxley-Gödel Machine below tries to fix.

**Delta vs. Maestro.** Same as DGM: no HITL, no loop, no identity. SICA's
specific contribution is the baseline architecture that DGM builds on.

**Borrowable:** Already captured via DGM; no separate idea to lift.

### 3. Huxley-Gödel Machine (HGM)

| Field | Value |
|-------|-------|
| **Paper** | [arXiv 2510.21614](https://arxiv.org/pdf/2510.21614) |
| **Axis overlap** | 1 (self-mod) |

Recent (late 2025) successor to DGM/SICA. Key insight: **decouple
expansion from evaluation.** Instead of always generating a child and
immediately running it on the benchmark, HGM uses an approximation of
child quality to prioritise which candidates to actually evaluate, saving
benchmark compute and finding better agents faster.

**Delta vs. Maestro.** Maestro's equivalent dynamic is the LLM's own
`reflect()` → `propose_plan()` → `implement()` → quality-gate path, where
`reflect()` and `propose_plan()` are the "approximation" and `implement()`
+ quality gate are the "evaluation". So Maestro already has this
decoupling in spirit. The difference is that Maestro doesn't reason
explicitly about *expected cost/value of evaluating this candidate* — every
approved plan runs the full implementation pipeline.

**Borrowable:** Not directly, at least not yet. An "approximate-quality
ranking before implement" step could reduce cost on low-value plans, but
the same work is already done informally inside `reflect()`'s LLM call.
Noted for a possible future proposal once the archive from 017 gives us
real data on implementation cost vs. realised improvement.

### 4. Voyager (Minecraft skill library)

| Field | Value |
|-------|-------|
| **Project** | Voyager — LLM agent that plays Minecraft by accumulating a growing library of reusable skills. |
| **Axis overlap** | 1 (kind of — modifies its *skill library*, not its backbone) |

The archetype of an agent that grows a structured, *searchable*, *reusable*
skill library over time. Each skill is a named, self-contained function
the agent can call by name. Unlike DGM, Voyager doesn't rewrite its own
backbone — it curates a library of skills that the backbone can invoke.

**Delta vs. Maestro.** Maestro has task artifacts in `artifacts/{task_id}/`
and a `read_task_artifact` MCP tool (`maestro/tools.py`), but nothing
distils cross-task patterns into a named, reusable form. Every new task
starts fresh with only a summary view; the LLM has to re-discover patterns
from raw artifact text each time.

**Borrowable:** YES. See **[proposal 018](../core/018-skill-library.md)** —
after a successful task, an existing-style distillation step produces a
short `Skill` record (name, when-to-use, snippet, source task_id), stored
JSONL under `memory_dir/skill_library.jsonl`. The `Executor` prompt
builder selects the top-k relevant skills for the current task's goal
text and inlines them as context. A new `read_skill_library` MCP tool on
the `memory` server lets tool-using queries browse explicitly. This is
directly analogous to the existing `read_task_history` path at one
abstraction level higher.

### 5. Boucle — Claude Agent SDK loop runner

| Field | Value |
|-------|-------|
| **Reference** | Mentioned in [DEV article on Claude Code autonomous agents](https://dev.to/boucle2026/how-to-run-claude-code-as-an-autonomous-agent-with-a-cron-job-hec) |
| **Axis overlap** | 2 (HITL), 3 (long-running loop) |

The closest architectural cousin I found: Claude Agent SDK-specific loop
runner with persistent memory (called Broca), an MCP server for
multi-agent memory sharing, and approval gates. Same SDK, same "loop +
HITL + memory" shape.

**Delta vs. Maestro.** Boucle is a *general harness* — you build your
agent on top of it. Maestro is a *specific agent* with an identity,
journal, purpose-seeking loop, and the whole evolutionary-stage model.
Boucle has no self-modification of the agent itself (axis 1) and no
identity layer (axis 4). It's the framework you could build Maestro on
top of if you were starting today.

**Borrowable:** Worth a closer read on the approval-gate details
specifically — Maestro's `approve/reject plan` dashboard and Telegram
flows reimplement something Boucle may have already solved. Not urgent.
No proposal written at this time; the existing flow works and the cost
of a drop-in replacement is higher than the cost of the current
duplication.

### 6. AutoGPT / SuperAGI / Agent Zero

| Field | Value |
|-------|-------|
| **AutoGPT** | The canonical self-prompting autonomous loop. |
| **SuperAGI** | [`TransformerOptimus/SuperAGI`](https://github.com/TransformerOptimus/SuperAGI) — dev-first autonomous agent framework. |
| **Agent Zero** | [agent-zero.ai](https://www.agent-zero.ai/) — autonomous agent that runs on its own computer, creates tools, self-corrects. |
| **Axis overlap** | 3 (long-running loop) |

These match Maestro's continuous-loop axis but do not modify their own
source (only their memory / skill state) and do not require human approval
for structural change. They are frameworks/harnesses, not specific agents
with an identity.

**Borrowable:** Not directly. These cover ground Maestro already covers
differently, and the way they cover it is less suited to a HITL,
self-modifying agent. No proposal.

### 7. Group-Evolving Agents (GEA) and AutoResearch

| Field | Value |
|-------|-------|
| **GEA** | Multi-agent self-evolving framework where groups of agents share experiences and reuse innovations. |
| **AutoResearch** | Agent that modifies its own ML training code, runs a short experiment, evaluates results, and iterates. |
| **Axis overlap** | 1 (self-mod) |

2025–2026 research on narrower forms of self-modification. AutoResearch
modifies *experiment code*, not agent code. GEA evolves *groups of agents
sharing skills*. Both are more domain-specific than DGM.

**Borrowable:** The GEA multi-agent-sharing angle is intriguing but out of
scope for a single-instance Maestro. The AutoResearch
"propose → short experiment → evaluate → keep/discard" loop is already
roughly the shape of Maestro's evolve cycle. No proposal.

### 8. Microsoft Agent Governance Toolkit

| Field | Value |
|-------|-------|
| **Blog** | [Microsoft Open Source — April 2026](https://opensource.microsoft.com/blog/2026/04/02/introducing-the-agent-governance-toolkit-open-source-runtime-security-for-ai-agents/) |
| **Axis overlap** | orthogonal — runtime security wrapper |

Open-source runtime security governance for autonomous AI agents. First
toolkit to address all 10 OWASP agentic AI risks with deterministic,
sub-millisecond policy enforcement. This is not a competitor to Maestro —
it's the kind of thing you'd *wrap* around Maestro.

**Delta vs. Maestro.** Maestro already has many mitigations for agentic
risks (HITL on evolution, `check_secrets` on startup, artifact path-
traversal check in `read_task_artifact`, caps on LLM output shape in the
`consciousness.py` JSON schema, 7-day cooldown on rejected-plan
resubmission, the whole crash-triage queue). But they are scattered and
undocumented against any external taxonomy — "does Maestro handle OWASP
agentic risk #7?" has no direct answer.

**Borrowable:** YES, but not the toolkit itself — the *taxonomy*. See
**[proposal 019](../security/019-owasp-agentic-risk-self-audit.md)** —
a one-time self-audit cycle producing `docs/owasp-agentic-audit.md`, one
row per OWASP agentic risk with current Maestro mitigation and file/line
anchors, plus a freshness check in `_self_review()` that warns if a
modified file invalidates a referenced mitigation.

### 9. NVIDIA OpenShell / NemoClaw

| Field | Value |
|-------|-------|
| **Blog** | [NVIDIA developer blog](https://developer.nvidia.com/blog/run-autonomous-self-evolving-agents-more-safely-with-nvidia-openshell/) |
| **Axis overlap** | orthogonal — runtime security wrapper |

Similar shape to the Microsoft toolkit: open stack for running "always-on,
self-evolving agents" more safely, privacy/security controls bolted on.

**Borrowable:** Same category as the Microsoft toolkit — wraps something
like Maestro. Covered by proposal 019.

## Survey anchors

Two surveys exist and are the best place to confirm nothing in category
(1) was missed:

- [`EvoAgentX/Awesome-Self-Evolving-Agents`](https://github.com/EvoAgentX/Awesome-Self-Evolving-Agents)
  — the 2025–2026 meta-list for self-evolving agents.
- [A Survey of Self-Evolving Agents (arXiv 2507.21046)](https://arxiv.org/html/2507.21046v3)
  — "On Path to Artificial Super Intelligence", July 2025.

A negative check on axis 4 (identity / purpose layer) is easy: grep either
survey for "purpose", "self-model", "identity", or "journal". Expected
hits: ~zero. That's the main finding of this survey.

## Gap analysis — what's unique to Maestro

The combination that makes Maestro distinctive is not any single axis — each
axis has public peers — but all four together.

| Axis | Public peer | Maestro has it? | Distinctive? |
|------|-------------|-----------------|--------------|
| 1. Self-modification of own source | DGM, SICA, HGM | Yes (gated on HITL) | — |
| 2. HITL approval on structural change | Boucle-ish | Yes | — |
| 3. Long-running loop with operational resilience | AutoGPT, SuperAGI, Agent Zero, Boucle | Yes (phase resume, crash triage, heartbeat, SIGTERM containment) | Maestro's plumbing here is more hardened than most public peers, but not unique in kind |
| 4. **First-person identity layer** | **none found** | **Yes** | **Yes** |

Axis 4 is the most distinctive. Every self-modifying coder I found is
climbing SWE-bench; none are asking what they are for. Whether axis 4 is
*useful* (i.e. whether a purpose-seeking layer produces measurably better
behaviour than a purpose-less loop) is an empirical question that
proposals 017 and 018 can start to answer — both would give Maestro a
structured signal for "is this version of me better than the previous
one?", and that signal is the prerequisite for evaluating axis 4's
contribution at all.

## Sources

- [Darwin Gödel Machine paper (arXiv 2505.22954)](https://arxiv.org/abs/2505.22954)
- [Sakana AI — The Darwin Gödel Machine blog post](https://sakana.ai/dgm/)
- [DGM reference implementation (`jennyzzt/dgm`)](https://github.com/jennyzzt/dgm)
- [Huxley-Gödel Machine paper (arXiv 2510.21614)](https://arxiv.org/pdf/2510.21614)
- [SICA topic page](https://www.emergentmind.com/topics/self-improving-coding-agent-sica)
- [Awesome-Self-Evolving-Agents survey](https://github.com/EvoAgentX/Awesome-Self-Evolving-Agents)
- [A Survey of Self-Evolving Agents (arXiv 2507.21046)](https://arxiv.org/html/2507.21046v3)
- [SuperAGI](https://github.com/TransformerOptimus/SuperAGI)
- [Agent Zero](https://www.agent-zero.ai/)
- [awesome-ai-agents list](https://github.com/e2b-dev/awesome-ai-agents)
- [Claude Agent SDK (Python)](https://github.com/anthropics/claude-agent-sdk-python)
- [Microsoft Agent Governance Toolkit blog](https://opensource.microsoft.com/blog/2026/04/02/introducing-the-agent-governance-toolkit-open-source-runtime-security-for-ai-agents/)
- [NVIDIA OpenShell blog post](https://developer.nvidia.com/blog/run-autonomous-self-evolving-agents-more-safely-with-nvidia-openshell/)
- [Boucle / Claude Code autonomous agent DEV article](https://dev.to/boucle2026/how-to-run-claude-code-as-an-autonomous-agent-with-a-cron-job-hec)

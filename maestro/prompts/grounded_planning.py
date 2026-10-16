"""Grounded-planning prompt fragments (proposal 143).

Appended by :class:`maestro.evolution.EvolutionEngine` only when a planner call
carries the bounded ``tree`` surface; kept here so ``evolution.py`` stays
below SPLIT_BUDGET.
"""

#: Prompt fragment appended to :meth:`EvolutionEngine.propose_plan` when the
#: call carries the bounded ``tree`` surface (proposal 143, ``[loop]
#: grounded_planning_enabled``). Appended only on that branch, so every other
#: prompt stays byte-identical.
_GROUNDED_PLANNING_PROMPT = """

READ BEFORE YOU CLAIM: this session carries read-only tools over the source tree
(find_definition, read_source, grep_source, list_cli_surface,
read_reality_inventory) under a small call budget. Before replying, use them to
confirm that every files_affected path and every symbol you cite exists, and that
the capability you propose is not already present; check read_reality_inventory
for shipped capabilities and take any problem id you cite from it. Do not assert a path, symbol, or "does not exist yet" you have not read.
If you attach a "grounding" probe, copy its "expectation" from text you actually
read. Reply with the JSON only once your reads are done."""

#: Chain variant (proposal 143): the chain schema has no ``grounding`` probe.
_GROUNDED_PLANNING_CHAIN_PROMPT = _GROUNDED_PLANNING_PROMPT.replace(
    'If you attach a "grounding" probe, copy its "expectation" from text you'
    " actually\nread. ", "",
)

def reread_affected_prompt(unread_paths: list[str], draft: str) -> str:
    """Build the one re-prompt that asks the planner to read unread cited files.

    Proposal 150: when :meth:`EvolutionEngine.propose_plan`'s grounded arm
    drafts a plan citing an existing ``.py`` in ``files_affected`` it never read
    through the ``tree`` surface, the arm re-sends the full prompt plus this
    suffix — naming those unread paths and the prior draft — so the planner reads
    them before re-emitting, raising ``read_coverage`` and cutting duplicate
    mints. Kept here so ``evolution.py`` stays below SPLIT_BUDGET.
    """
    paths = "\n".join(f"- {path}" for path in unread_paths)
    return f"""

READ BEFORE YOU RE-EMIT: your previous draft cited these existing source files
in files_affected without reading them through the tree tools:
{paths}

Your previous draft was:
{draft}

Use read_source on each unread path above to confirm the capability you propose
is not already present there, then re-emit the full JSON plan — revised if a read
shows the change already shipped. Reply with the JSON only once your reads are done."""


#: Appended to ``curate_pending_plans``'s prompt when that single-use call
#: carries the bounded ``tree`` surface (proposal 143, opt-in caller).
_GROUNDED_CURATE_PROMPT = """

READ BEFORE YOU CLAIM: this session carries read-only tools over the source tree
under a small call budget. Before an "obsolete" verdict that says the capability
already exists or the premise is false, read the module or symbol that shows it.
Every path or symbol in a refined proposed_changes must be one you have read.
Reply with the JSON object only once your reads are done."""

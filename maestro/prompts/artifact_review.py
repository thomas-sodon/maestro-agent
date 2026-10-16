"""Prompt for the artifact-review phase (proposal 031).

Consumed by :class:`maestro.artifact_review.ArtifactReviewer`. The prompt
asks the model to classify each completed task's artifact bundle as
``publish`` / ``improve`` / ``reuse`` / ``skip`` and return a JSON array.

The prompt is intentionally conservative — it leans on two worked
examples (a ``publish`` and a ``skip``) rather than threshold language,
and spells out the negative constraints (no ``Maestro`` references, no
workspace paths, no internal identifiers) that protect the unfiltered
bundle text from leaking.
"""

from __future__ import annotations

# NOTE: this prompt is rendered via :meth:`str.format`. Literal braces in
# the JSON examples are doubled (``{{`` / ``}}``) so only the
# ``{bundle_text}`` placeholder is substituted.

ARTIFACT_REVIEW_PROMPT = """\
You are reviewing artifact directories produced by completed tasks to
decide which outputs are worth publishing as standalone open-source
projects, which are worth improving in place, which could be reused as
patterns for future work, and which are not worth keeping.

For each task below, return exactly one JSON object. Respond with a
single JSON array containing one object per task, in the same order as
the tasks appear.

Each object MUST include:
  - "task_id": the task identifier (verbatim, matching the task header).
  - "verdict": exactly one of "publish", "improve", "reuse", "skip".
  - "title": a short human-readable title (<= 80 chars).
  - "reasoning": 1-3 sentences explaining the verdict.

If "verdict" is "publish", the object MUST also include:
  - "proposed_name": a kebab-case repository name, <= 40 chars
    (lowercase letters, digits, and hyphens only).
  - "proposed_description": a one-line public description, <= 120 chars.
  - "readme_markdown": a complete public-facing README in Markdown,
    at least 600 characters. Include a title, a what-it-is section,
    a how-to-use section, and a short example if relevant. Do NOT
    reference the source project, the task system, or internal tooling.

Verdict guidance:
  - "publish": the artifact is a self-contained, useful, standalone
    piece of work that a stranger could pick up and use without any
    knowledge of how it was produced. When uncertain, do NOT choose
    publish — choose skip or improve.
  - "improve": genuinely interesting output that is not yet polished
    or complete enough to publish. Worth revisiting.
  - "reuse": the approach or pattern is worth remembering for future
    work, even if the artifact itself is not worth shipping.
  - "skip": routine, throwaway, or unsalvageable output. This should
    be the default when in doubt.

Hard negative constraints for "publish" verdicts:
  - Do NOT reference "Maestro", "the agent", "self-evolving",
    "self-reflection", or any internal project naming.
  - Do NOT reference absolute filesystem paths (workspace roots,
    container paths, home directories, tmp dirs).
  - Do NOT reference personal identifiers (e.g. a specific person's
    name, email, or personal repository) unless they are already
    present in the artifact content itself.
  - Do NOT reference internal task ids, plan ids, or loop iterations.
  - If the artifact contains any obvious secret or credential, pick
    "improve" and say so in reasoning.

Worked examples
================

Example 1 (publish):
Input: a task produced a self-contained Python module that computes
cosine similarity over word embeddings, plus a small CLI wrapper and
a test suite.
Output: {{"task_id": "t_001", "verdict": "publish",
         "title": "Tiny embedding similarity CLI",
         "reasoning": "Self-contained utility with tests and a
         useful public surface. No internal references.",
         "proposed_name": "embed-sim",
         "proposed_description": "Compute cosine similarity between
         word embeddings from the command line.",
         "readme_markdown": "# embed-sim\\n\\nA tiny CLI that computes
         cosine similarity over word embeddings...\\n\\n## Usage\\n...
         (>= 600 chars)"}}

Example 2 (skip):
Input: a task produced a single file named `notes.md` containing a
short internal log of what the task tried, plus a reference to the
workspace path where the experiment ran.
Output: {{"task_id": "t_002", "verdict": "skip",
         "title": "Internal scratch notes",
         "reasoning": "Task-internal log with workspace paths; no
         standalone value."}}

Tasks to review
================

{bundle_text}

Return a JSON array with exactly one object per task above, in order.
Do not wrap the array in additional text. No trailing commentary.
"""

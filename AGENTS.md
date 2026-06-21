# Agent entrypoint

This repository uses shared, tool-agnostic agent instructions. `.ai/` is the
canonical source of truth for project context, role contracts, memory, and
reporting discipline.

On session start, read:

1. `.ai/project-constitution.md` — shared project rules, Git workflow, smoke
   tests, migration state, and environment policy.
2. `.ai/README.md` — file classifications, memory/reporting protocol, and
   agent context conventions.
3. The relevant role contract:
   - Mentor: `.ai/agents/ai-mentor.md`
   - Coder: `.ai/agents/coder.md`
   - Operator: `.ai/agents/operator.md`

Role-specific bootstrap:

- **Mentor**: read `.ai/agents/ai-mentor.md`,
  `.ai/agents/ai-mentor.local.md`, `.ai/memory/current-status.md`, and
  `.ai/memory/operator-findings.md`. The Mentor supervises roadmap, Git
  lifecycle, branch scope, merge proposals, and delegation.
- **Coder**: read `.ai/agents/coder.md` and the pointed brief
  `.ai/memory/personal/<slug>-brief.md`. The Coder is stateless and implements
  exactly one brief.
- **Operator**: read `.ai/agents/operator.md`,
  `.ai/agents/operator-memory.md`, `.ai/memory/current-status.md`, and any
  pointed verification brief. The Operator drives execution, traces, Langfuse,
  Application Insights, KQL, and reports.

Do not duplicate long instructions into tool-specific files. Claude-specific
and Codex-specific files should point here and to `.ai/`.

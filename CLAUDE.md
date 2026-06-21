# Claude Code entrypoint

This repository uses tool-agnostic agent instructions. Do not treat this file
as the source of truth.

On session start, read:

1. `.ai/project-constitution.md` — shared project rules, Git workflow, smoke
   tests, migration state, and environment policy.
2. `.ai/README.md` — file classifications, memory/reporting protocol, and
   agent context conventions.
3. The relevant role contract:
   - Mentor: `.ai/agents/ai-mentor.md`
   - Coder: `.ai/agents/coder.md`
   - Operator: `.ai/agents/operator.md`

Claude-specific wrappers under `.claude/agents/` may add launch metadata, but
they inherit the shared `.ai/` contracts. Keep this file thin so Claude Code
and Codex do not drift.

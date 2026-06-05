# `.ai/` — agent context and conventions

This directory holds tool-agnostic guidance and shared state for AI agents
working in this repository. It is meant to be readable by Cursor, Claude
Code, Claude Code subagents, Langfuse-instrumented automations, and any
other AI helper. None of these files contain secrets.

## File classifications

| Class | Examples | Agent may read? | Committed? |
|---|---|---|---|
| **secret** | `.env`, `*.pem`, `*.key`, anything under `~/.aws/`, customer audio | **no** | no |
| **agent-safe env** | `.env.agent-safe` — loaded by the app as non-sensitive defaults; precedence is shell > `.env` > `.env.agent-safe` | yes | yes |
| **project knowledge** | `CLAUDE.md`, `README.md`, `MIGRATION.md`, source code | yes | yes |
| **shared context** | this directory (`.ai/`) | yes (read; write rules below) | yes |
| **per-developer scratch** | `.ai/memory/personal/**` | only the developer's own agent | gitignored |
| **agent-local context** | `.ai/agents/*.local.md` — personal context that complements a public agent contract (employer, mission, secret-adjacent details) | only the developer's own agent | gitignored |
| **runtime data** | `output/`, `rag/data/` | only when explicitly asked | gitignored |

## Boundary mechanics

There are two enforcement layers and one convention layer.

1. **`.gitignore`** — keeps `.env`, runtime data, and per-developer
   scratch out of the repo. This is the only mechanism that protects
   the public history.
2. **`.cursorignore`** — Cursor's indexer respects it; matched files
   are not embedded or sent to model context by default. Add similar
   files for other tools as they appear (`.aiexclude`, etc.).
3. **Convention** — well-behaved agents should:
   - Read `.env.agent-safe` instead of `.env`.
   - Treat any file matching the "secret" row above as off-limits unless
     the user explicitly pastes a value into the conversation.
   - Read `.ai/memory/current-status.md` at the start of a session to
     understand recent context.

Convention is not enforcement. An agent with file-system access can read
`.env`. The structure here lowers accidents (well-meaning auto-indexing
of secrets, careless reads during file exploration); it does not stop a
deliberate exfiltration.

## Agent roles (`.ai/agents/`)

Each markdown file describes one agent's contract:
- **scope** — what it is for
- **may read / may write** — file allow-list
- **must not** — explicit prohibitions
- **typical tools / tasks** — short examples

Roles defined here are descriptive and tool-agnostic. A Claude Code
subagent (`.claude/agents/<name>.md`) or Cursor "custom agent" can
reference the matching `.ai/agents/<name>.md` file to inherit the
contract.

## Shared memory (`.ai/memory/`)

- `current-status.md` — single rolling status doc. Updated when a
  multi-session task completes. Agents should read this before
  proposing the next step, and may append a short entry when finishing
  a milestone.
- `personal/` — gitignored per-developer scratch. Notes that should
  not survive a fresh clone go here.

Write protocol:
1. Read the current contents before editing.
2. Append, do not rewrite history.
3. Date-stamp the entry (`YYYY-MM-DD — summary`).
4. Keep entries under ~10 lines; link to PRs or commits for detail.

## Langfuse architecture (planned)

Langfuse will sit at the **observability** layer, not inside the
secrets layer:

```
agent → LLM API call → Langfuse SDK (trace, span, score)
                          ↓
                       Langfuse server (self-hosted or cloud)
```

What Langfuse sees:
- Prompts and completions issued by the application.
- Token counts, latencies, model identifiers.
- User-supplied scores / tags.

What Langfuse should NOT see:
- API keys for Anthropic / OpenAI / Azure (the SDK never transmits them
  to Langfuse; they only authenticate the call to the model provider).
- `.env` contents.

The Langfuse public key + secret key go in `.env` (they are credentials)
and never in `.env.agent-safe`. Only the *endpoint host* and *project
name* are agent-safe.

When Langfuse is wired in, expose it through the existing provider
abstractions (`rag/providers.py`) so instrumentation is one decorator
on the factory, not a refactor across every call site.

## Adding a new agent role

1. Create `.ai/agents/<role-name>.md` following the format of the
   existing files.
2. Default to **read-only** unless the role explicitly needs writes.
3. Default to **forbidden** for: `.env`, customer audio, anything not
   in this repo.
4. Reference the role from your tool's own agent config (e.g.
   `.claude/agents/<role-name>.md` can be a thin wrapper that includes
   the contract by reference).

### Public contract vs local context

When an agent's role depends on personal or situational context that
shouldn't be public (employer, mission, sector-specific constraints,
non-secret-but-private details), use a two-layer pattern:

- `.ai/agents/<role-name>.md` — public, committed, sector-agnostic
- `.ai/agents/<role-name>.local.md` — private, gitignored, personal
  context that completes the public contract

The public file should reference the `.local.md` sibling explicitly so
future agents know to read both. The `.gitignore` pattern
`.ai/agents/*.local.md` covers all such files. Agents must never quote
`.local.md` content verbatim in publishable surfaces (PR descriptions,
commit messages, issue comments).

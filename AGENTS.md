# AGENTS.md

**Read [`CLAUDE.md`](./CLAUDE.md) first — it is the single source of truth** for
this repository: repository role (submodule under the cookbook repo), branching,
Alembic migrations, architecture, `uv` commands, environment variables, CI,
testing, GBrain (`--source gstack-code-backend`), and behavioral guidelines.

When working inside the cookbook superproject checkout (`Backend/` under
`tasteslikegoodtheangularsvegancookbook`), also follow the superproject's
`../CLAUDE.md` and `../AGENTS.md` for release-train, submodule-pointer, and PM
tooling rules.

This file used to be a parallel copy of `CLAUDE.md`. That drifted behind the
accuracy passes on `CLAUDE.md` (missing agent-tool and Markdown-for-agents env
vars, GBrain `--source gstack-code-backend` rules, and the corrected Gemini
credential notes). Everything shared now lives in `CLAUDE.md` only. What
remains here is the delta for non-Claude agents (Antigravity `agy`, Gemini CLI,
OpenCode, Codex).

## What to skip in CLAUDE.md

- **MCP servers (`.mcp.json`)** — `.mcp.json` auto-spawn is Claude Code
  behaviour. The underlying scripts (`scripts/mcp/run_parent_mcp.sh` proxying to
  the cookbook superproject's `pm-daemon` and `gcp-monitor`) work in any harness
  that configures those MCP servers.

## Installing gstack (`garrytan/gstack`)

`gemstack` is retired. The canonical source for `gstack` skills is
[`garrytan/gstack`](https://github.com/garrytan/gstack):

```bash
# From the cookbook superproject checkout (clones into ~/.claude/skills/gstack):
../scripts/install-gstack.sh --host agy     # Antigravity CLI (agy)
../scripts/install-gstack.sh --host auto    # every detected agent on the machine

# Or from a standalone clone:
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
cd ~/.claude/skills/gstack && ./setup --host agy
./setup --status
```

## Skill loading by harness

- **Antigravity (`agy`):** `./setup --host agy` installs global skills at
  `~/.gemini/antigravity-cli/skills/gstack-*/SKILL.md` (prefixed with `gstack-`
  so `/gstack-plan-*` does not collide with Antigravity's built-in `/plan`).
  Project skills in the superproject live under `.agents/skills/<name>/SKILL.md`.
  Load a skill by reading its `SKILL.md` with `read_file` (read remaining line
  ranges if truncated at 800 lines). Translate Claude tool names in skill docs
  to Antigravity tools:
  - `Bash` → `run_command`
  - `Read` → `read_file`
  - `Write` → `write_file`
  - `Edit` → `edit_file`
  - `Agent` → `invoke_subagent`
  - `AskUserQuestion` → `ask_question`
- **Gemini CLI (`gemini`):** Reads `GEMINI.md` and loads skills from
  `~/.gemini/skills/` and `.agents/skills/`. In GitHub Actions
  (`run-gemini-cli`), `GEMINI_CLI_TRUST_WORKSPACE: 'true'` is required.
- **OpenCode:** Loads skills from `.opencode/skills/` and
  `~/.config/opencode/skills/` via the `skill` tool.

## Skill routing

When the user's request matches an available skill, load it as your FIRST
action (`skill` tool in OpenCode, or `read_file` on the matching `SKILL.md` in
Antigravity / Gemini CLI):

- Product ideas, "is this worth building", brainstorming → `office-hours` (`/gstack-office-hours`)
- Bugs, errors, "why is this broken", 500 errors → `investigate` (`/gstack-investigate`)
- Ship, deploy, push, create PR → `ship` (`/gstack-ship`)
- QA, test the site, find bugs → `qa` (`/gstack-qa`)
- Code review, check my diff → `review` (`/gstack-review`)
- Update docs after shipping → `document-release` (`/gstack-document-release`)
- Weekly retro → `retro` (`/gstack-retro`)
- Architecture review → `plan-eng-review` (`/gstack-plan-eng-review`)

## Editing this file

Add something here only if it is **false or inapplicable for Claude Code**.
Anything true for every agent belongs in [`CLAUDE.md`](./CLAUDE.md).

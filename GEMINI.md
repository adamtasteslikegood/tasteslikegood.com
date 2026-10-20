# GEMINI.md

**Read [`CLAUDE.md`](./CLAUDE.md) and [`AGENTS.md`](./AGENTS.md) first:**

- **[`CLAUDE.md`](./CLAUDE.md)** is the single source of truth for this
  repository (`adamtasteslikegood/tasteslikegood.com` — the Flask backend
  submodule under `Backend/`): branching (`dev` / `main`), Alembic migrations,
  architecture, `uv` commands, environment variables, CI, testing, GBrain code
  search (`--source gstack-code-backend`), and behavioral guidelines.
- **[`AGENTS.md`](./AGENTS.md)** carries the harness delta for **Antigravity
  (`agy`)**, **Gemini CLI (`gemini`)**, and other non-Claude agents — including
  [`garrytan/gstack`](https://github.com/garrytan/gstack) installation
  (`./setup --host agy`), skill loading (`~/.gemini/antigravity-cli/skills/`,
  `~/.gemini/skills/`, `.agents/skills/`), tool-name translation, and skill
  routing.

Antigravity (`agy`) automatically loads both `AGENTS.md` and `GEMINI.md` in
every workspace directory. To avoid duplicating rules across context files or
drifting from `CLAUDE.md`, all repo facts live in [`CLAUDE.md`](./CLAUDE.md) and
all Antigravity / Gemini CLI harness instructions live in
[`AGENTS.md`](./AGENTS.md).

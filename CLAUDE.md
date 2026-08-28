# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository role

This repo (`adamtasteslikegood/tasteslikegood.com`) is the **Flask backend** for the Vegangenius Chef cookbook app. It is checked out as a **git submodule** under `Backend/` inside the cookbook frontend repo (`adamtasteslikegood/tasteslikegoodtheangularsvegancookbook`). Production deploys whichever Backend SHA the cookbook repo points at when a release tag is cut.

A change here ships in two steps:

1. PR into Backend `dev` (this repo).
2. PR into the cookbook repo bumping the submodule pointer to the new Backend SHA.

There is no path that ships Backend changes without a corresponding cookbook PR.

## Branching strategy

- `main` — release branch. Direct commits forbidden.
- `dev` — integration branch. All feature work merges here first.
- `feat/*`, `fix/*`, `chore/*` — short-lived branches off `dev`. PR back into `dev`.

Never commit directly to `main` or `dev`. Always branch off `dev`.

### Starting work: branch or worktree

After fetching (`git fetch origin --prune`) and confirming `dev` is up to date:

```bash
git switch -c feat/my-topic origin/dev   # always base on the remote tip
# ... work, commit ...
git push -u origin feat/my-topic
gh pr create --base dev
```

### Branch protection

`dev` and `main` are protected — **direct pushes are rejected**. All changes reach them via PR. Required status checks: CodeQL `Analyze (python)`, CI jobs (lint, type-check, test, docker build).

### Run checks locally before pushing

```bash
uv run black --check .          # formatting
uv run flake8                   # lint
uv run mypy .                   # type check
uv run pytest                   # tests
```

For docs-only changes, skip the full suite. When in doubt, run everything — it takes ~30 seconds locally.

## Database migrations (Alembic via Flask-Migrate)

Migrations live in `migrations/versions/`. Schema changes ship as auto-generated revisions:

```bash
export FLASK_APP=app.py
uv run flask db migrate -m "describe the change"
uv run flask db upgrade
```

**Multi-PR head conflicts:** Two PRs off the same parent revision create branched heads. Detect with `uv run flask db heads` (must be one line). Unify with:

```bash
uv run flask db merge -m "merge <topic-a> and <topic-b> heads" <revA> <revB>
```

**Production:** Migrations run via Cloud Run Job `flask-backend-migrate` in `cloudbuild.yaml` — never run `flask db upgrade` against prod from a dev machine.

## Architecture

Modular Flask app. Composition wired in `app.py:create_app()`.

```
blueprints/          # route handlers (auth, generation, recipes, collections, worker, public SSR)
services/            # business logic (Gemini, Imagen, GCS, Pub/Sub, stock images)
repositories/        # data access with file locking + DB
models/              # SQLAlchemy: User, Recipe, Cookbook
validators/          # JSON Schema Draft 7 (recipe_schema.json)
utils/               # normalization, slugs, sessions, cache, logging, Valkey auth
migrations/          # Alembic via Flask-Migrate
templates/public/    # SSR templates (base_public.html, recipe.html, browse.html)
tests/               # pytest
```

### Key architectural facts

- **Legacy HTML surface is DEV-ONLY** — the original server-rendered UI (`/`, `/recipe/<filename>`, `/generate_recipe`) is never reachable in production; Express only proxies `/api/*`, `/r/<slug>`, `/browse`, `/sitemap.xml`, `/static/*`. Do not add production features there.
- **Auth:** `get_genai_client(session_credentials)` prefers user OAuth creds, falls back to server `GOOGLE_API_KEY`. Both live generation sites pass `None`, so generation runs on the server key. OAuth flow + PKCE in `blueprints/auth_api_bp.py`.
- **Async generation:** `worker_api_bp.py` handles Pub/Sub push with OIDC verification. Fails closed with 503 when `PUBSUB_INVOKER_SA` unset (bypass with `PUBSUB_AUTH_OPTIONAL=1` for local/test).
- **ProxyFix** trusts `X-Forwarded-*` from Express for correct `url_for(_external=True)`.

## Commands

### Setup (uv)

```bash
uv sync                             # creates .venv, installs deps
cp .env.example .env                # set DATABASE_URL, OAuth secrets, GOOGLE_API_KEY
./init_database.sh                  # initial DB + first upgrade (local SQLite)
```

### Run

```bash
uv run python app.py                # dev server on :5000
```

### Test

```bash
uv run pytest                                                          # all
uv run pytest tests/test_normalization.py                              # one file
uv run pytest tests/test_normalization.py::TestNormalization::test_normalize_unit
```

### Migrations

```bash
uv run flask db heads               # show current head(s) — must be 1 line
uv run flask db migrate -m "..."    # autogenerate
uv run flask db upgrade             # apply
uv run flask db merge -m "..." A B  # unify branched heads
```

## Environment variables

| Var                              | Purpose                                                                                          |
| -------------------------------- | ------------------------------------------------------------------------------------------------ |
| `DATABASE_URL`                   | PostgreSQL in prod, `sqlite:///tasteslikegood.db` in dev. `postgres://` auto-rewritten.          |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | OAuth                                                                                   |
| `GOOGLE_API_KEY`                 | Gemini fallback when user has no OAuth                                                           |
| `GEMINI_DEFAULT_MODEL`           | Overrides the default generation model. Defaults to `gemini-3.7-flash` when unset/empty.         |
| `GEMINI_IMAGE_MODEL`             | Overrides the image generation model. Defaults to `gemini-3-pro-image` when unset/empty.         |
| `FLASK_SECRET_KEY`               | Session signing. **Required in prod** — fails fast if missing.                                   |
| `FLASK_ENV`                      | `production` activates guards + secret-key fail-fast.                                            |
| `GCS_BUCKET_NAME`               | Recipe image storage (`tasteslikegood-recipe-images` in prod)                                    |
| `GCP_PROJECT_ID`                 | Required for Pub/Sub publishing                                                                  |
| `PUBSUB_INVOKER_SA`             | Required for worker push authentication                                                          |
| `VALKEY_HOST`, `VALKEY_AUTH_MODE` | Flask-Caching response cache. Priority: `VALKEY_HOST` > `REDIS_URL` > in-process SimpleCache.    |
| `FRONTEND_URL`, `SESSION_COOKIE_DOMAIN` | OAuth redirect target + cookie scoping across the Express proxy/Flask boundary.            |
| `DD_API_KEY`                     | **Required in prod** for Datadog `serverless-init`. Not needed locally.                          |

All prod secrets from Google Secret Manager via Cloud Run `--set-secrets`.

## CI & Testing

CI (`.github/workflows/ci.yml`) runs on every push/PR to `main` or `dev`: Black+Flake8 lint, mypy type check, pytest, Docker build, pip-audit (advisory). Additional: CodeQL (required), AI reviews.

pytest (`uv run pytest`). Reference test setup pattern: `tests/test_public_ssr.py`.

## Common gotchas

- **Two migration heads** — `flask db upgrade` runs but schema is incomplete; `recipe.status missing` style errors at runtime.
- **`config.py` reads `DATABASE_URL` at import time** — overriding via shell after import does nothing.
- **No `requirements.txt`** — `uv.lock` is the single source of truth; Dockerfile runs `uv export --frozen` at build time.
- **mypy config lives only in `pyproject.toml`** — a `setup.cfg` `[mypy]` section is silently shadowed. `explicit_package_bases = true` required because `scripts/` has no `__init__.py`.
- **`GEMINI_CLI_TRUST_WORKSPACE: 'true'`** — required in every `run-gemini-cli` workflow job; without it gemini-cli refuses to run in an untrusted directory.

## MCP servers (agent sessions)

`.mcp.json` proxies to the cookbook superproject's MCP servers via `scripts/mcp/run_parent_mcp.sh` (locates cookbook checkout, `exec`s the real launchers). Provides `pm-daemon` and `gcp-monitor` — same tools as in the cookbook repo. In a standalone checkout (no superproject), the servers are unavailable.

## GBrain code search (agent sessions)

If gbrain is configured on the machine, this repo is indexed as its own source,
`gstack-code-backend` — separate from the cookbook worktree's source and **not
federated**. Two consequences for anyone working in here:

**1. Every query needs an explicit `--source`.** `code-def`, `code-refs`,
`code-callers`, `code-callees`, `search`, and `query` all default to the
cookbook worktree's pinned source, which contains no Backend Python. Without
the flag they return nothing and give no indication anything was missed:

```bash
gbrain code-def get_genai_client --source gstack-code-backend
gbrain search "OAuth PKCE code verifier" --source gstack-code-backend
```

There is no `.gbrain-source` pin in this directory to make that automatic —
the pin is a cookbook-worktree mechanism, and this repo is a submodule
(a gitlink), so it does not get one.

**2. Never run a bare `/sync-gbrain` from inside `Backend/`.** It does not
no-op here. Because there is no pin, the orchestrator's code stage falls back
to registering the cwd as a _new_ federated source
(`gstack-code-com-<hash> --path .../Backend`), re-indexing the whole repo
alongside the pages already held by `gstack-code-backend`. The nested-path
guard does not catch
it: `gstack-code-backend` lives in gbrain's own managed clone directory rather
than at the `Backend/` path, so there is no path overlap to detect. Verified
via `gstack-gbrain-sync.ts --dry-run` on 2026-07-24.

Refresh this repo's index with the explicit form instead — it fast-forwards
gbrain's managed clone from Backend `dev`:

```bash
gbrain sync --source gstack-code-backend --strategy code
```

If you want the memory + brain-sync stages while sitting in `Backend/`, run
`gstack-gbrain-sync.ts --no-code`. From this directory, always `--dry-run`
first and read the `would:` line before letting the code stage run.

Related: the `/sync-gbrain` skill rewrites its `## GBrain Search Guidance`
block from a fixed template asserting the worktree is pinned via
`.gbrain-source`. That assertion is false here, so do not let the skill write
that block into this file. The cookbook `CLAUDE.md` § GBrain Search Guidance
carries the authoritative cross-repo version.

## Behavioral Guidelines

Follow the four Karpathy principles when writing or modifying code in this project:

1. **Think Before Coding** — understand the problem fully before writing. Read existing code, check for prior art, verify assumptions.
2. **Simplicity First** — prefer the simplest solution that works. Avoid premature abstraction, speculative features, and unnecessary indirection.
3. **Surgical Changes** — make the smallest diff that solves the problem. Don't refactor surrounding code, add unrelated improvements, or "clean up while you're there."
4. **Goal-Driven Execution** — every action should move toward a verifiable success criterion. State what "done" looks like before starting.

## Related docs

- `API.md` — endpoint reference
- `DATABASE_SETUP.md` — migration walkthrough
- `docs/ci/` — CI audit and pipeline docs
- Cookbook repo `CLAUDE.md` — frontend, Express proxy, Cloud Build, release process

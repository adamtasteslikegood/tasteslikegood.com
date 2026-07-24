# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository role

This repo (`adamtasteslikegood/tasteslikegood.com`) is the **Flask backend** for the Vegangenius Chef cookbook app. It is checked out as a **git submodule** under `Backend/` inside the cookbook frontend repo (`adamtasteslikegood/tasteslikegoodtheangularsvegancookbook`). The cookbook repo's `.gitmodules` pins this submodule to a specific SHA on the `dev` branch; production deploys whichever Backend SHA the cookbook repo points at when a release tag is cut.

A change here ships in two steps:

1. PR into Backend `dev` (this repo).
2. PR into the cookbook repo bumping the submodule pointer to the new Backend SHA.

There is no path that ships Backend changes without a corresponding cookbook PR.

## Branching strategy (FINAL)

- `main` — release branch. Stable. The cookbook submodule may track this in the future; today it tracks `dev`. Direct commits forbidden.
- `dev` — integration branch. All feature work merges here first.
- `feat/*`, `fix/*`, `chore/*` — short-lived branches off `dev`. PR back into `dev`.
- Periodic `dev` → `main` PRs catch `main` up after cookbook releases stabilize.

Never commit directly to `main` or `dev`. Always branch off `dev`.

## Database migrations (Alembic via Flask-Migrate)

Migrations live in `migrations/versions/`. Schema changes ship as auto-generated revisions:

```bash
export FLASK_APP=app.py
uv run flask db migrate -m "describe the change"
uv run flask db upgrade
```

### Multi-PR head conflicts

Two PRs that both add a migration off the same parent revision will create **branched heads**. Alembic refuses to upgrade until they're merged. Detect with `uv run flask db heads` — output should be a single line with `(head)`. If two heads appear, generate a merge migration:

```bash
uv run flask db merge -m "merge <topic-a> and <topic-b> heads" <revA> <revB>
```

Commit the resulting `*_merge_*.py` file alongside your PR. The merge migration's `upgrade()`/`downgrade()` are typically empty — it exists only to unify the DAG.

### Production migration runs

In production, migrations are applied by a Cloud Run **Job** (`flask-backend-migrate`) wired into the cookbook repo's `cloudbuild.yaml`. The job runs `flask db upgrade` against Cloud SQL **before** the Flask Cloud Run service is redeployed. If the migration fails, the Flask service is not redeployed and the build aborts. Never run `flask db upgrade` against prod from a developer machine — let the pipeline handle it, or execute the job manually with `gcloud run jobs execute flask-backend-migrate --wait`.

## Architecture

Modular Flask app, not the monolithic `app.py` of older docs. Composition wired in `app.py:create_app()`.

### Layout

```
app.py                          # create_app() — registers blueprints, extensions, ProxyFix
config.py                       # env-driven config (DATABASE_URL, OAuth, secrets)
extensions.py                   # SQLAlchemy, Migrate, Cache singletons
auth.py                         # legacy OAuth helpers (still used by blueprints)

blueprints/
  auth_api_bp.py                # /api/auth/* — OAuth flow, sessions, profile
  generation_bp.py              # legacy HTML generation helpers/routes
  generation_api_bp.py          # /api/generate, /api/generate_image, status/image API
  recipes_api_bp.py             # /api/recipes CRUD
  collections_api_bp.py         # /api/collections (cookbooks) CRUD
  worker_api_bp.py              # Pub/Sub push handlers (OIDC-verified)
  public_bp.py                  # SSR public recipe routes (slug-based)

services/
  gemini_service.py             # Gemini text generation + client factory
  image_service.py              # Imagen image generation
  gcs_service.py                # Google Cloud Storage (recipe images)
  model_service.py              # Gemini model list refresh
  pubsub_service.py             # Pub/Sub message publishing
  stock_image_service.py        # fallback stock image lookup
  migration_service.py          # slug backfill + data migration helpers
  reporting_service.py          # admin reporting

utils/
  normalization.py              # recipe data normalization (units, fractions, fuzzy keys)
  slug_utils.py                 # slug generation with collision handling
  session_utils.py              # Flask session helpers
  cache_utils.py                # cache key builders + decorators
  log_sanitizer.py              # sanitize user input in log output
  valkey_auth.py                # Valkey IAM token refresh for Memorystore
  admin_auth.py                 # admin API token validation
  logging_config.py             # structured logging setup

repositories/                   # data access with file locking + DB
validators/                     # JSON Schema Draft 7 (recipe_schema.json)
models/                         # SQLAlchemy: User, Recipe, Cookbook
migrations/                     # Alembic via Flask-Migrate
templates/public/               # SSR templates (base_public.html, recipe.html, browse.html)
static/                         # CSS tokens, images, JS for SSR pages
tests/                          # pytest (~28 test files)
scripts/                        # one-off migration/debug scripts (backfill_slugs.py, etc.)
recipe_schema.json              # canonical recipe shape
```

### Legacy Flask HTML surface is DEV-ONLY

The original server-rendered HTML UI — `/` and `/recipe/<filename>` (`recipes_bp.py`), `/generate_recipe` (`generation_bp.py`), the `/auth/*` pages, and the templates extending `templates/base.html` (`index.html`, `generate_recipe.html`, `recipe.html`, `profile.html`, `json_viewer.html`) — is **never reachable in production**: the Express proxy only forwards `/api/*`, `/r/<slug>`, `/browse`, `/sitemap.xml`, and `/static/*` to Flask. These pages exist only when running Flask directly on :5000 for local development. The production-facing SSR pages live under `templates/public/` (`base_public.html`); the app-level 404/500 error pages extend the public base for that reason. Do not add production features to the legacy surface, and do not link production pages to its routes. Deleting it entirely is a tracked follow-up (cookbook #3164).

### Authentication

Gemini credential handling lives in `services/gemini_service.py:get_genai_client(session_credentials)`: it prefers caller-supplied user OAuth credentials, falls back to the server `GOOGLE_API_KEY`, and returns `None` if neither works — it never reads the Flask session itself. Today both live generation call sites (`services/image_service.py` and the Pub/Sub worker in `blueprints/worker_api_bp.py`) pass `None`, so generation runs on the server key; only model refresh (`blueprints/api_bp.py` → `refresh_models_from_api`) forwards `session.get("credentials")`. Preserve that preference order.

OAuth flow lives in `blueprints/auth_api_bp.py`. PKCE `code_verifier` is persisted across the redirect (see `fix(auth): persist PKCE code_verifier across OAuth redirect`).

`ProxyFix` middleware trusts `X-Forwarded-*` headers from the Express reverse proxy so `url_for(_external=True)` produces correct external URLs.

### Async generation (Pub/Sub)

`worker_api_bp.py` exposes HTTP push endpoints that Pub/Sub invokes with OIDC tokens. Push authentication fails closed with 503 when `PUBSUB_INVOKER_SA` is unset. `GCP_PROJECT_ID` is separately required when API/worker code publishes Pub/Sub messages. The cookbook's `cloudbuild.yaml` injects both env vars on Flask Cloud Run deploys.

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

| Var | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL in prod, `sqlite:///tasteslikegood.db` in dev. `postgres://` URIs are auto-rewritten to `postgresql://`. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth |
| `GOOGLE_API_KEY` | Gemini fallback when user has no OAuth |
| `FLASK_SECRET_KEY` | Session signing. **Required in prod** — `FLASK_ENV=production` fails fast if missing. |
| `FLASK_ENV` | `production` activates `OAUTHLIB_INSECURE_TRANSPORT` guard + secret-key fail-fast. |
| `GCS_BUCKET_NAME` | Recipe image storage (`tasteslikegood-recipe-images` in prod) |
| `GCP_PROJECT_ID` | Required when API/worker code publishes Pub/Sub messages |
| `PUBSUB_INVOKER_SA` | Required for `worker_api_bp` to authenticate push messages |
| `VALKEY_HOST`, `VALKEY_AUTH_MODE` | Set by cookbook deploy; backs the Flask-Caching response cache (recipe, collection, and image paths). Unset or unavailable = in-process SimpleCache fallback. |
| `FRONTEND_URL`, `SESSION_COOKIE_DOMAIN` | OAuth redirects, cookie scoping |
| `DD_API_KEY` | **Required in prod** for the Docker image's Datadog `serverless-init` entrypoint — without it traces/profiles/logs/AppSec events are dropped (app still serves). Injected via `--set-secrets` in the cookbook `cloudbuild.yaml`; not needed for local `python app.py`. |

In production all secrets come from Google Secret Manager via Cloud Run `--set-secrets`.

## CI pipeline

Backend CI (`.github/workflows/ci.yml`) runs on every push/PR to `main` or `dev`:

| Job | What it checks |
|---|---|
| `Lint (Black + Flake8)` | Code formatting and style |
| `Type Check (mypy)` | Static type analysis |
| `Test (pytest)` | Full test suite with coverage report |
| `Build (docker)` | Validates the production Docker image builds |
| `Security Scan (pip-audit)` | Audits pinned deps for known CVEs (advisory) |

Additional workflows: `codeql.yml` (CodeQL analysis — required status check via branch protection), `claude-review.yml` / `gemini-review.yml` (AI code review on PRs).

## Testing

pytest (`uv run pytest`). ~28 test files in `tests/`. Coverage reported via `pytest-cov`.

Key test patterns:
- SSR routes: `tests/test_public_ssr.py` — the reference for correct app/DB test setup
- Auth: `tests/test_auth_api.py`, `tests/test_auth.py`
- Generation: `tests/test_gemini_service.py`, `tests/test_image_service.py`, `tests/test_async_generation_api.py`
- Data: `tests/test_normalization.py`, `tests/test_recipe_validation.py`
- Worker: `tests/test_worker_oidc.py`, `tests/test_worker_generation_retry.py`

## Common gotchas

- **Two migration heads** — see "Multi-PR head conflicts" above. Symptom: `flask db upgrade` runs but production schema is incomplete; `recipe.status missing` style errors at runtime.
- **`config.py` reads `DATABASE_URL` at import time** — overriding via shell after import does nothing. Set env before invoking Flask.
- **Flask SQLite paths resolve under `instance/`** — `sqlite:///foo.db` writes to `instance/foo.db`, not the cwd.
- **Gemini model names include `models/` prefix** (e.g., `models/gemini-3.1-pro-preview`); filter API responses by `'generateContent' in supported_generation_methods`.
- **Tests must hit the right DB engine** — `tests/test_migration_backfill_slug.py::test_backfill_slugs_retry_loop` set `SQLALCHEMY_DATABASE_URI` after `create_app()` and ran `db.create_all()` against a stale engine. Compare against `tests/test_public_ssr.py` for the working pattern (issue #118).
- **There is no tracked `requirements.txt` — `uv.lock` is the single source of truth** — the Dockerfile's export stage runs `uv export --frozen` from `pyproject.toml` + `uv.lock` at image build time, so the image always installs exactly what's locked. To change dependencies: edit `pyproject.toml`, run `uv lock`, commit both — nothing else to regenerate. (A tracked export artifact used to exist and repeatedly drifted or got corrupted — a Dependabot regeneration once dropped most runtime deps and killed the v0.3.0 deploy; see `docs/ci/CI-AUDIT-REPORT.md` addendum.)
- **mypy config lives only in `pyproject.toml` `[tool.mypy]`** — a `setup.cfg` `[mypy]` section is silently shadowed by it (that shadowing shipped a config that crashed mypy for months; `setup.cfg` was deleted 2026-07-14). `explicit_package_bases = true` is required because `scripts/` has no `__init__.py`.
- **Every `run-gemini-cli` job needs `GEMINI_CLI_TRUST_WORKSPACE: 'true'`** — current gemini-cli refuses to run in an untrusted directory even without a checkout. CI quality gates and the required-check list are documented in `docs/ci/refresh/` (SPEC-01/SPEC-02).

## MCP servers (agent sessions)

`.mcp.json` registers the cookbook superproject's two MCP servers so agent
sessions started inside this repo get the same tooling as sessions started in
the cookbook repo:

- `pm-daemon` — PM tools (`get_project_status`, `sync_pm_documents`,
  `refresh_project_briefing`, `create_epic_from_roadmap`, `log_agent_session`)
  plus the Confluence file watcher (singleton via `flock`; extra daemons are
  expected, one per session).
- `gcp-monitor` — read-only Cloud Monitoring tools (`check_system_health`,
  `list_available_metrics`, `query_metric`) for the production stack.

Both entries go through `scripts/mcp/run_parent_mcp.sh`, which locates the
cookbook checkout (via `git rev-parse --show-superproject-working-tree`, or by
walking up parent directories for linked worktrees) and `exec`s the real
launchers there (`scripts/pm/run_pm_daemon.sh`,
`scripts/monitoring/run_gcp_monitor.sh`). The launchers cd to the cookbook
root, so credentials still come from the cookbook `.env`
(`ATLASSIAN_EMAIL`/`ATLASSIAN_API_TOKEN` for pm-daemon,
`GOOGLE_APPLICATION_CREDENTIALS` for gcp-monitor) — see the cookbook
`CLAUDE.md` § Startup. In a standalone checkout (no cookbook superproject) the
wrapper exits with a clear error and the servers are simply unavailable.

## Behavioral Guidelines

Follow the four Karpathy principles when writing or modifying code in this project:

1. **Think Before Coding** — understand the problem fully before writing. Read existing code, check for prior art, verify assumptions.
2. **Simplicity First** — prefer the simplest solution that works. Avoid premature abstraction, speculative features, and unnecessary indirection.
3. **Surgical Changes** — make the smallest diff that solves the problem. Don't refactor surrounding code, add unrelated improvements, or "clean up while you're there."
4. **Goal-Driven Execution** — every action should move toward a verifiable success criterion. State what "done" looks like before starting.

See the `karpathy-guidelines` skill for the full reference.

## Related docs

- `API.md` — endpoint reference
- `README.md` — getting started
- `DATABASE_SETUP.md` — migration walkthrough
- Cookbook repo `CLAUDE.md` — frontend, Express proxy, Cloud Build flow, release process

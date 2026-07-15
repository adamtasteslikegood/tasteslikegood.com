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
extensions.py                   # SQLAlchemy, Migrate, LoginManager singletons
auth.py                         # legacy OAuth helpers (still used by blueprints)
utils.py                        # recipe data normalization (units, fractions, fuzzy keys)

blueprints/
  auth_api_bp.py                # /api/auth/* — OAuth flow, sessions, profile
  generation_bp.py              # legacy HTML generation helpers/routes
  generation_api_bp.py          # /api/generate, /api/generate_image, status/image API
  recipes_api_bp.py             # /api/recipes CRUD
  collections_api_bp.py         # /api/collections (cookbooks) CRUD
  worker_api_bp.py              # Pub/Sub push handlers (OIDC-verified)
  public_bp.py                  # SSR public recipe routes (slug-based)

services/                       # business logic (gemini_service, image_service, ...)
repositories/                   # data access with file locking + DB
validators/                     # JSON Schema Draft 7 (recipe_schema.json)
models/                         # SQLAlchemy: User, Recipe, Collection
migrations/                     # Alembic via Flask-Migrate
tests/                          # pytest
recipe_schema.json              # canonical recipe shape
```

### Authentication

Dual-credential strategy in services that call Gemini:

1. **Primary** — user OAuth credentials from session.
2. **Fallback** — server `GOOGLE_API_KEY`.

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

## Commit and push cadence

On feature branches, commit and push after every significant work-run so work is recoverable from the remote if the VM/session dies. Stage only intentional files, keep commits scoped, and push immediately after each local commit unless the user explicitly says not to.

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
| `VALKEY_HOST`, `VALKEY_AUTH_MODE` | Optional distributed cache configuration used by recipe, collection, and image paths; helpers fall back when unavailable |
| `FRONTEND_URL`, `SESSION_COOKIE_DOMAIN` | OAuth redirects, cookie scoping |

In production all secrets come from Google Secret Manager via Cloud Run `--set-secrets`.

## Common gotchas

- **Two migration heads** — see "Multi-PR head conflicts" above. Symptom: `flask db upgrade` runs but production schema is incomplete; `recipe.status missing` style errors at runtime.
- **`config.py` reads `DATABASE_URL` at import time** — overriding via shell after import does nothing. Set env before invoking Flask.
- **Flask SQLite paths resolve under `instance/`** — `sqlite:///foo.db` writes to `instance/foo.db`, not the cwd.
- **Gemini model names include `models/` prefix** (e.g., `models/gemini-3.1-pro-preview`); filter API responses by `'generateContent' in supported_generation_methods`.
- **Tests must hit the right DB engine** — `tests/test_migration_backfill_slug.py::test_backfill_slugs_retry_loop` set `SQLALCHEMY_DATABASE_URI` after `create_app()` and ran `db.create_all()` against a stale engine. Compare against `tests/test_public_ssr.py` for the working pattern (issue #118).

## Related docs

- `API.md` — endpoint reference
- `README.md` — getting started
- `DATABASE_SETUP.md` — migration walkthrough
- Cookbook repo `CLAUDE.md` — frontend, Express proxy, Cloud Build flow, release process

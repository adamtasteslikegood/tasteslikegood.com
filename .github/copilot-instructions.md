# Copilot instructions — tasteslikegood.com (Flask backend)

Flask backend for **Vegangenius Chef**, a vegan recipe generator and personal cookbook app. It serves the JSON API and SSR public recipe pages behind an Express reverse proxy that owns all browser traffic. This repo is consumed as the `Backend/` git submodule of the cookbook repo (`adamtasteslikegood/tasteslikegoodtheangularsvegancookbook`); production (Cloud Run) deploys whatever Backend SHA the cookbook pins when a release tag is cut.

`CLAUDE.md` at the repo root is the deeper companion doc; `API.md` is the endpoint reference. Ignore older markdown files describing a monolithic `app.py` — the app has been modular (blueprints + app factory) since the refactor.

## Stack

- Python 3.13 only (`requires-python = ">=3.13,<3.14"`), Flask app factory in `app.py:create_app()`
- SQLAlchemy + Flask-Migrate (Alembic), Flask-Caching (session auth is hand-rolled on Flask sessions — no Flask-Login)
- `google-genai` client for Gemini text and Imagen image generation
- **uv** manages dependencies. Never `pip install` into the project.

## Commands

```bash
uv sync --locked --dev                # install deps (exactly what CI runs)
uv run python app.py                  # dev server on :5000 (Werkzeug debug — dev only)
uv run pytest                         # tests; CI adds --cov=. and dummy FLASK_SECRET_KEY /
                                      # GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars
uv run black --check .                # formatting gate
uv run flake8 . --count --show-source --statistics
uv run mypy . --ignore-missing-imports
uv run flask db heads                 # must print exactly ONE line
```

CI (`.github/workflows/ci.yml`) enforces: Black + Flake8, requirements.txt↔uv.lock sync, mypy, pytest, and a Docker image build. pip-audit also runs but is advisory (`continue-on-error: true`) — treat findings as signal, not build failures.

## Architecture

`create_app()` wires ProxyFix, extensions (`extensions.py`), the response cache, and registers blueprints:

| Blueprint | Routes | Notes |
| --- | --- | --- |
| `auth_api_bp` | `/api/auth/*` | Google OAuth flow, sessions, profile |
| `generation_api_bp` | `/api/generate`, `/api/generate_image`, status/image routes | Gemini + Imagen |
| `recipes_api_bp` | `/api/recipes` | recipe CRUD |
| `collections_api_bp` | `/api/collections` | cookbook list/create/get/delete + add/remove recipes (no update route) |
| `worker_api_bp` | `/api/worker/*` | Pub/Sub push handlers; OIDC-verified; 503s (fails closed) if `PUBSUB_INVOKER_SA` is unset |
| `public_bp` | `/r/<slug>`, `/browse` | SSR public recipe pages (Jinja) |
| `auth_bp`, `recipes_bp`, `generation_bp`, `api_bp` | legacy HTML/JSON routes | |

Supporting layers: `services/` (business logic), `repositories/` (data access), `validators/` (JSON Schema Draft 7, `recipe_schema.json`), `models/` (`User`, `Recipe`, `Cookbook` — the cookbook model class is `Cookbook`, not `Collection`), `migrations/` (Alembic).

### Behind the Express proxy

All browser traffic arrives through the cookbook repo's Express proxy. `ProxyFix` trusts `X-Forwarded-*`, so `url_for(_external=True)` produces correct external URLs — don't hand-build absolute URLs and don't strip or rework forwarded headers.

### Gemini auth is dual-credential

`services/gemini_service.py:get_genai_client()` tries the user's OAuth credentials from the Flask session first, falls back to the server `GOOGLE_API_KEY`, and returns `None` if neither works. Preserve that order. Model IDs from the model-list API carry the `models/` prefix — filter listings by `generateContent` in `supported_generation_methods` — while `config.py:DEFAULT_MODEL` and the generation paths use bare IDs (`gemini-3.1-pro-preview`). Both forms are in active use; don't flag either as wrong.

### Caching

Flask-Caching response cache, selected in `create_app()` with priority `VALKEY_HOST` (prod, IAM or password auth) > `REDIS_URL` (local Docker) > in-process `SimpleCache`. An unreachable Valkey/Redis degrades to `SimpleCache` at startup instead of failing, and per-call cache errors are absorbed by `utils/cache_utils`. Code must behave correctly under any backend.

## Migrations (Alembic via Flask-Migrate)

- `uv run flask db migrate -m "…"` to autogenerate, `uv run flask db upgrade` to apply locally.
- **Single-head rule:** `uv run flask db heads` must print exactly one line. Two PRs adding migrations off the same parent create branched heads and `flask db upgrade` refuses to run; unify with `uv run flask db merge -m "…" <revA> <revB>` and commit the (typically empty) merge migration.
- Production migrations run via the cookbook repo's `flask-backend-migrate` Cloud Run Job before each deploy. Never run `flask db upgrade` against prod from a developer machine.

## Dependencies

`uv.lock` is the source of truth. `requirements.txt` is **generated** for the Docker build — never hand-edit it. Regenerate only with:

```bash
uv export --format requirements-txt --no-dev --extra postgres --no-hashes --no-emit-project -o requirements.txt
```

CI fails when it diverges from `uv.lock`. To change dependencies: edit `pyproject.toml`, run `uv lock`, then regenerate `requirements.txt` with the exact command above.

## Environment variables

| Var | Notes |
| --- | --- |
| `DATABASE_URL` | Postgres in prod, SQLite in dev. `postgres://` URIs are auto-rewritten to `postgresql://` in `config.py`. |
| `FLASK_SECRET_KEY` | `create_app()` raises at startup when `FLASK_ENV=production` and it's missing; dev generates an ephemeral key. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth. |
| `GOOGLE_API_KEY` | Server-side Gemini fallback credential. |
| `GCS_BUCKET_NAME` | Recipe image storage. |
| `GCP_PROJECT_ID` | Required when publishing Pub/Sub messages. |
| `PUBSUB_INVOKER_SA` | Required for worker push auth; endpoints fail closed without it. |
| `VALKEY_HOST`, `VALKEY_PORT`, `VALKEY_AUTH_MODE` | Response-cache backend (`iam` is the prod auth default; legacy `REDIS_URL` only when `VALKEY_HOST` is unset). |
| `DD_API_KEY` | Required in prod: the Docker entrypoint is Datadog `serverless-init`, and without the key telemetry is silently dropped (the app still serves). |

In production all secrets come from Google Secret Manager via Cloud Run `--set-secrets`, wired in the cookbook repo's `cloudbuild.yaml`.

## Review checklist (each of these has actually broken)

- `requirements.txt` edited by hand, or regenerated with different flags → prod deploy breaks. Only the exact `uv export` command above is valid.
- A new migration while another migration PR is in flight → two Alembic heads. Require a merge migration.
- mypy config lives **only** in `pyproject.toml` `[tool.mypy]`. Reject new `setup.cfg`/`mypy.ini` mypy sections — pyproject silently shadows them (that shadowing once shipped a config that crashed mypy). `explicit_package_bases = true` is load-bearing because `scripts/` has no `__init__.py`.
- Every `run-gemini-cli` job in `.github/workflows/` needs `GEMINI_CLI_TRUST_WORKSPACE: 'true'` or the CLI refuses to run.
- `config.py` reads env vars at import time — set them before Flask starts, not after.
- Flask SQLite URIs resolve under `instance/` (`sqlite:///foo.db` → `instance/foo.db`, not the cwd).
- Production serves via `ddtrace-run gunicorn` (Dockerfile `CMD`); `python app.py` is the Werkzeug debug server and must never serve production traffic.

## Git workflow

Branch `feat/*` / `fix/*` / `chore/*` off `dev`; PR back into `dev`. Never commit directly to `main` or `dev`. Shipping to production additionally requires a cookbook-repo PR bumping the `Backend/` submodule pointer — there is no path that deploys Backend code without one.

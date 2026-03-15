# CLAUDE.md

> Last updated: 2026-03-14

This file provides guidance to Claude Code (claude.ai/code) when working with the **Flask backend** in this repository.

For overall project architecture see the root `AGENTS.md` and `.github/copilot-instructions.md`.

## Project Overview

This is the **Flask backend** for **Vegangenius Chef**, a three-tier vegan recipe generator app:

```
Angular 21 SPA → Express reverse-proxy (:8080) → Flask API (:5000) → Cloud SQL (PostgreSQL)
```

Flask handles:
- Google OAuth authentication (server-side sessions)
- AI recipe generation (Gemini `gemini-2.5-flash`)
- AI food photo generation (Imagen `imagen-4.0-generate-001`)
- CRUD for recipes and collections (cookbooks) in Cloud SQL
- Recipe schema validation and normalization

## Development Commands

### Setup
```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Edit .env to add your Google OAuth credentials and API key
```

### Running the Application
```bash
# Development server (auto-reload, port 5000)
python app.py

# The Angular frontend (port 3000) and Express server (port 8080)
# proxy /api/* requests to this Flask server
```

### Testing
```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_normalization.py

# Run with verbose output
pytest -v
```

### Docker
```bash
# Build the Docker image
docker build -t flask-backend .

# Run the container
docker run -p 5000:5000 --env-file .env flask-backend
```

## Architecture

### Modular Structure
```
Backend/
├── app.py              # Flask factory (create_app), CORS, session middleware, blueprint registration
├── auth.py             # Google OAuth blueprint (/auth/*)
├── config.py           # DB URI, environment loading
├── extensions.py       # SQLAlchemy db, Flask-Migrate
├── blueprints/
│   ├── api_bp.py           # /api — base API routes
│   ├── auth_api_bp.py      # /api/auth/* — OAuth login/logout/check (JSON API)
│   ├── recipes_api_bp.py   # /api/recipes/* — CRUD recipes (JSON API)
│   ├── collections_api_bp.py  # /api/collections/* — CRUD cookbooks (JSON API)
│   ├── generation_api_bp.py   # /api/generate, /api/generate_image (JSON API)
│   ├── recipes_bp.py      # HTML recipe views (legacy)
│   └── generation_bp.py   # HTML generation form (legacy)
├── models/             # SQLAlchemy models (User, Recipe, Collection)
├── repositories/       # Data access layer (queries)
├── services/
│   ├── gemini_service.py       # Gemini recipe generation
│   ├── image_service.py        # Imagen food photo generation
│   └── stock_image_service.py  # Unsplash fallback images
├── utils/
│   ├── normalization.py    # Recipe data normalization
│   ├── session_utils.py    # Session ID management
│   └── logging_config.py   # Structured logging setup
├── migrations/         # Alembic/Flask-Migrate schema migrations
├── Dockerfile          # Production container
└── requirements.txt    # Python dependencies
```

### Request Flow

All requests arrive via the Express reverse proxy (`X-Forwarded-Host` headers).
Flask uses `ProxyFix` middleware to trust these headers so `url_for(_external=True)`
generates correct public URLs and session cookies use the right domain.

### Authentication Strategy
- **Google OAuth** via Flask sessions (server-side, not JWT)
- Session middleware (`ensure_session_id`) creates anonymous session IDs for guest tracking
- On OAuth login, guest session data is preserved and merged

### Recipe Generation & Validation

**Generation Flow** (`POST /api/generate`):
1. User submits prompt (1–500 chars)
2. Flask constructs prompt with schema definition and sends to Gemini
3. Parses JSON response (strips markdown code fences if present)
4. Normalizes data (handles typos, unit variations, missing fields)
5. Validates against recipe schema
6. Saves to Cloud SQL via repository layer
7. Returns recipe JSON to client

**Image Generation Flow** (`POST /api/generate_image`):
1. Receives recipe_id
2. Generates food photo via Imagen `imagen-4.0-generate-001`
3. Stores image and returns URL path

### Data Persistence
- **Cloud SQL (PostgreSQL)** — primary store via SQLAlchemy + Flask-Migrate
- Schema migrations managed by Alembic (in `migrations/`)
- For local dev without Cloud SQL, SQLite fallback via `SQLALCHEMY_DATABASE_URI`

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_API_KEY` | ✅ | Gemini API key for recipe/image generation |
| `FLASK_SECRET_KEY` | ✅ (prod) | Session signing key (auto-generated in dev) |
| `SQLALCHEMY_DATABASE_URI` | ✅ (prod) | PostgreSQL connection string |
| `GOOGLE_CLIENT_ID` | For OAuth | OAuth 2.0 client ID |
| `GOOGLE_CLIENT_SECRET` | For OAuth | OAuth 2.0 client secret |
| `PORT` | No | Server port (default `5000`) |

## Key API Endpoints

### JSON API (used by Angular frontend via Express proxy)
- `GET /api/auth/check` — Check if user has active session
- `GET /api/auth/login` — Initiate Google OAuth flow (returns authorization_url)
- `POST /api/auth/logout` — Clear session
- `POST /api/generate` — Generate vegan recipe via Gemini
- `POST /api/generate_image` — Generate food photo via Imagen
- `GET /api/recipes` — List user's saved recipes
- `POST /api/recipes` — Save a recipe
- `DELETE /api/recipes/:id` — Delete a recipe
- `GET /api/collections` — List user's cookbooks
- `POST /api/collections` — Create a cookbook
- `DELETE /api/collections/:id` — Delete a cookbook
- `POST /api/collections/:id/recipes` — Add recipe to cookbook
- `DELETE /api/collections/:id/recipes/:recipe_id` — Remove recipe from cookbook

### Legacy HTML Routes (Flask-rendered templates)
- `GET /` — Homepage with recipe list
- `GET /recipe/<filename>` — View specific recipe
- `GET /generate_recipe` — Recipe generation form

## Important Patterns

### Adding New Routes
- Create a new blueprint in `blueprints/` following existing patterns
- Register in `app.py` via `app.register_blueprint()`
- Use `credentials: 'include'` on the Angular side for session cookies

### Database Migrations
```bash
# Create a migration after model changes
flask db migrate -m "description"

# Apply migrations
flask db upgrade

# Rollback
flask db downgrade
```

### Working with Gemini API
- Use `Client(api_key=...)` for API key auth
- Model names include `models/` prefix (e.g., `models/gemini-2.5-flash`)
- Always wrap generation in try-except (API can return malformed JSON)
- Normalization layer handles common AI output inconsistencies

## Common Issues

| Issue | Fix |
|---|---|
| `GOOGLE_API_KEY` not set | Export env vars before running |
| OAuth redirect URI mismatch | Check `url_for('auth.callback', _external=True)` — `ProxyFix` must be active |
| Cloud SQL connection timeout | Check IAM permissions (`roles/cloudsql.client`) and connection string |
| Schema validation failure | Check `recipe_schema.json`; normalization in `utils/normalization.py` may need update |
| 502 from Express | Flask not running on expected port; check `FLASK_BACKEND_URL` on Express side |

## Related Documentation

- Root `AGENTS.md` — Overall project architecture and conventions
- `.github/copilot-instructions.md` — Copilot-specific project instructions
- `Backend/agents.md` — AI agent configurations (Gemini, Imagen, Unsplash, Validator)
- `Backend/API.md` — Detailed API endpoint documentation
- `docs/ADR-001-auth-and-persistence-routing.md` — Express proxy architecture decision

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Flask-based web application for viewing and generating vegan recipes using Google's Gemini AI models. The app features Google OAuth authentication, AI-powered recipe generation with schema validation, and a recipe browsing interface.

## Development Commands

### Setup
```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Edit .env to add your Google OAuth credentials and API key
```

### Running the Application
```bash
# Development server (includes auto-reload)
python app.py

# Application runs on http://localhost:5000
```

### Testing
```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_normalization.py

# Run with verbose output
pytest -v

# Run specific test function
pytest tests/test_normalization.py::TestNormalization::test_normalize_unit
```

### Docker
```bash
# Build the Docker image
docker build -t tasteslikegood .

# Run the container
docker run -p 5000:5000 --env-file .env tasteslikegood
```

## Architecture

### Core Application Flow
1. **app.py** - Main Flask application with routes, recipe validation, and Gemini API integration
2. **auth.py** - Google OAuth 2.0 authentication blueprint (login, callback, logout, profile)
3. **utils.py** - Recipe data normalization utilities (handles fuzzy matching of units, ingredients, and top-level keys)

### Authentication Strategy
The app uses a dual authentication approach:
1. **Primary**: User OAuth credentials (stored in session) for personalized API access
2. **Fallback**: Server API key when user is not authenticated

Both methods are attempted during recipe generation, with user credentials tried first.

### Recipe Generation & Validation

**Generation Flow** (`/generate_recipe` POST):
1. User submits prompt (10-500 chars) and selects Gemini model
2. App constructs full prompt with schema definition
3. Attempts generation with user credentials first, falls back to API key
4. Parses JSON response (strips markdown code fences if present)
5. **Normalizes** data using `normalize_recipe_data()` (handles typos, unit variations, missing fields)
6. Validates against `recipe_schema.json` using JSON Schema Draft 7
7. Saves to `recipes/` directory with sanitized filename
8. Redirects to new recipe page

**Schema Enforcement** (`recipe_schema.json`):
- Strict structure: `name`, `prepTime`, `cookTime`, `servings`, `ingredients`, `instructions`
- Ingredients grouped into `wet`, `dry`, and optional `other` arrays
- Each ingredient requires `name`, `amount` (number or array for ranges), and `units`
- Instructions can be strings or objects with `step` and `description`

**Normalization** (`utils.py`):
- Fuzzy matches common typos in top-level keys (e.g., "ingredents" → "ingredients")
- Normalizes measurement units using lookup table and difflib (e.g., "tablespoons" → "Tbsp")
- Parses amounts from strings (handles fractions like "1/2", ranges like "1-2")
- Converts flat ingredient lists to categorized structure if needed
- Provides sensible defaults for missing fields

### File Structure
```
app.py              # Main application & routes
auth.py             # OAuth authentication blueprint
utils.py            # Recipe normalization utilities
recipe_schema.json  # JSON Schema for recipe validation
recipes/            # Storage for generated recipes (JSON files)
templates/          # Jinja2 HTML templates
  ├── base.html           # Base template with navigation
  ├── index.html          # Recipe list homepage
  ├── recipe.html         # Individual recipe view
  ├── json_viewer.html    # Raw JSON viewer with copy button
  ├── generate_recipe.html # Recipe generation form with model picker
  └── profile.html        # User profile page
tests/              # Pytest test suite
  ├── test_normalization.py      # Unit tests for utils.py
  ├── test_recipe_validation.py  # Schema validation tests
  ├── test_instruction_parsing.py # Instruction format tests
  ├── test_model_fetching.py     # API model list tests
  └── test_auth.py               # Authentication tests
static/             # CSS, JS, images
```

## Environment Variables

Required variables (see `.env.example`):
- `GOOGLE_CLIENT_ID` - OAuth 2.0 client ID
- `GOOGLE_CLIENT_SECRET` - OAuth 2.0 client secret
- `GOOGLE_API_KEY` - Gemini API key for fallback generation
- `FLASK_SECRET_KEY` - Flask session encryption key (auto-generated if not set)

## Key API Endpoints

### Public Routes
- `GET /` - Homepage with recipe list
- `GET /recipe/<filename>` - View specific recipe
- `GET /recipe/<filename>/json` - View raw JSON (add `?raw=true` for JSON response)
- `GET /generate_recipe` - Recipe generation form
- `POST /generate_recipe` - Generate new recipe (requires prompt & model)
- `GET /api/models` - Fetch available Gemini models (dynamic, filtered to generation-capable)

### Authentication Routes (prefix: `/auth`)
- `GET /auth/login` - Initiate Google OAuth flow
- `GET /auth/callback` - OAuth callback handler
- `GET /auth/profile` - User profile (requires login)
- `GET /auth/logout` - Clear session and logout

## Important Patterns

### Adding New Routes
- Register in `app.py` or create new Blueprint following `auth.py` pattern
- Use `@login_required` decorator for protected routes (defined in `auth.py`)
- Always validate user input and handle JSON parsing errors

### Modifying Recipe Schema
1. Update `recipe_schema.json`
2. Update normalization logic in `utils.py` if needed
3. Add tests to `tests/test_recipe_validation.py`
4. Update `UNIT_MAPPINGS` or `key_mapping` if adding new fields/units

### Working with Gemini API
- Use `Client(api_key=...)` for API key auth
- Use `Client(credentials=...)` for OAuth credentials
- Model names include `models/` prefix (e.g., `models/gemini-2.0-flash-exp`)
- Filter models by `'generateContent' in supported_generation_methods`
- Always wrap generation in try-except (API can return malformed JSON)

### Testing Philosophy
- Tests use `unittest` and `pytest` frameworks
- Mock external API calls (`@patch` decorator)
- Test normalization edge cases (typos, missing data, format variations)
- Validate both positive and negative schema validation cases

## Common Issues

### Recipe Generation Failures
- Check error logs in `recipe_error.txt` and `recipe_error.json`
- Verify API key permissions (needs Gemini API access)
- Ensure prompt is 10-500 characters
- Common causes: malformed JSON from model, schema validation failure, unit typos

### Authentication Issues
- Verify `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set
- Check OAuth redirect URI matches `url_for('auth.callback', _external=True)`
- For local dev, `OAUTHLIB_INSECURE_TRANSPORT=1` is set in `auth.py`
- Session state issues: ensure Flask secret key is consistent

### Docker Deployment
- Ensure `.env` file is properly mounted or variables are passed
- Port 5000 must be exposed
- For production, consider using gunicorn instead of Flask dev server

## Related Documentation

- `API.md` - Detailed API endpoint documentation
- `README.md` - Getting started guide and basic usage
- `agents.md` - Agent configuration template (placeholder)
- `gemini.md` - Gemini agent configuration notes

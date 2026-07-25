# Quick Start Guide

Get Tastes Like Good running in 5 minutes! ⚡

## Prerequisites

- Python 3.13+ installed
- Google Cloud account
- Git installed

## Fast Track Setup

### 1. Clone and Setup (2 minutes)

```bash
# Clone the repository
cd tasteslikegood.com

# Install dependencies with uv (creates .venv automatically)
# Install uv first if needed: https://docs.astral.sh/uv/
uv sync
```

### 2. Get API Credentials (3 minutes)

#### Google Gemini API Key (Required)
1. Go to https://makersuite.google.com/app/apikey
2. Click "Create API key"
3. Copy the key

#### Google OAuth Credentials (Required for login)
1. Go to https://console.cloud.google.com/apis/credentials
2. Click "Create Credentials" → "OAuth 2.0 Client ID"
3. Choose "Web application"
4. Add authorized redirect URI: `http://localhost:5000/auth/callback`
5. Copy Client ID and Client Secret

#### Unsplash API (Optional - for stock images)
1. Go to https://unsplash.com/developers
2. Create a new app
3. Copy your Access Key

### 3. Configure Environment

```bash
# Copy example env file
cp .env.example .env

# Edit .env with your favorite editor
nano .env  # or vim, code, etc.
```

Paste your credentials:
```env
GOOGLE_API_KEY=your_actual_api_key_here
GOOGLE_CLIENT_ID=your_client_id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your_client_secret
FLASK_SECRET_KEY=any_random_string_here
UNSPLASH_ACCESS_KEY=your_unsplash_key  # Optional
```

### 4. Run!

```bash
uv run python app.py
```

Open http://localhost:5000 🎉

## What to Try First

1. **Browse recipes** - Click on any recipe from the homepage
2. **Generate a recipe** - Click "Generate Recipe" and try:
   - "spicy thai coconut curry with tofu"
   - "chocolate chip banana bread"
   - "mediterranean quinoa salad"
3. **View JSON** - Click "View JSON" on any recipe to see the data structure
4. **Login** - Click "Login" to use your own Google credentials for generation

## Common Issues

### "Module not found"
```bash
# Reinstall dependencies from the lockfile
uv sync

# Or run commands through uv so the right environment is used
uv run python app.py
```

### "API key not found"
```bash
# Make sure .env exists and has your key
cat .env  # Check the file contents

# Make sure key name is exactly: GOOGLE_API_KEY
```

### "OAuth redirect URI mismatch"
1. Go to Google Cloud Console → Credentials
2. Edit your OAuth 2.0 Client ID
3. Add exactly: `http://localhost:5000/auth/callback`
4. Save and try again

## Next Steps

- 📖 Read [README.md](README.md) for full documentation
- 🛠️ Check [CONTRIBUTING.md](CONTRIBUTING.md) to start developing
- 🤖 Learn about [AGENTS.md](AGENTS.md) for AI details
- 💻 See [CLAUDE.md](CLAUDE.md) for developer deep-dive

## Development Mode Features

When running with `python app.py`, you get:
- ✅ Auto-reload on code changes
- ✅ Debug mode enabled
- ✅ Detailed error pages
- ✅ OAuth over HTTP (insecure transport allowed)

## Production Deployment

For production, use a WSGI server (gunicorn is already a locked dependency — `uv sync` installs it):

```bash
uv run gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

See [Dockerfile](Dockerfile) for containerized deployment.

## Testing

Run the test suite:

```bash
# All tests
uv run pytest

# With coverage
uv run pytest --cov=.

# Specific test
uv run pytest tests/test_normalization.py -v
```

## Need Help?

- Check existing recipes in `recipes/` folder for examples
- Look at `recipe_schema.json` to understand data structure
- Review error logs in `recipe_error.txt` and `recipe_error.json`
- Open an issue on GitHub

Happy cooking! 🌱👨‍🍳

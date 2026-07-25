# Tastes Like Good - Vegan Recipe Generator

## Description

Tastes Like Good is a Flask-based web application for viewing and generating vegan recipes using Google's Gemini AI models. The app features Google OAuth authentication, AI-powered recipe generation with schema validation, image generation with Imagen 3, and an intuitive recipe browsing interface.

## Features

### Core Features
- **AI Recipe Generation**: Uses Google's Gemini models to create unique vegan recipes
- **OAuth Authentication**: Secure Google OAuth 2.0 login with dual authentication strategy
  - Primary: User OAuth credentials for personalized API access
  - Fallback: Server API key when user is not authenticated
- **Recipe Validation**: Robust JSON Schema validation ensures data consistency
- **Smart Normalization**: Fuzzy matching handles typos, unit variations, and missing fields

### Image Generation
- **Stock Images**: Automatically finds high-quality Unsplash images for recipes
- **AI Images**: Asynchronously generates custom food photography using Imagen 3
- **One-Two Punch**: Displays both stock and AI images side-by-side
- **Image Management**: Regenerate AI images or report issues directly from the UI

### User Experience
- **Code Viewer**: JSON data viewer with copy functionality
- **Metadata Tracking**: Records AI model, prompt, and timestamp for every recipe
- **Simple Navigation**: Easy navigation between recipe list, detail view, and JSON view
- **Session Management**: Anonymous session tracking for personalized experiences
- **Error Handling**: Comprehensive error logging and user-friendly error pages

## Architecture

### Modular Structure
```
app.py              # Main Flask application (factory pattern)
config.py           # Configuration and environment loading
auth.py             # Google OAuth 2.0 authentication blueprint

blueprints/         # Route handlers (modular design)
  ├── api_bp.py           # API endpoints
  ├── generation_bp.py    # Recipe generation routes
  └── recipes_bp.py       # Recipe browsing routes

services/           # Business logic layer
  ├── gemini_service.py   # Gemini API integration
  ├── image_service.py    # Imagen AI image generation
  ├── stock_image_service.py  # Unsplash stock images
  └── models_service.py   # Model listing and management

repositories/       # Data persistence layer
  └── recipe_repository.py  # Recipe CRUD with file locking

validators/         # Data validation
  └── recipe_validator.py  # JSON Schema validation

utils/              # Utility functions
  ├── normalization.py    # Recipe data normalization
  ├── session_utils.py    # Session management
  └── logging_config.py   # Logging configuration

templates/          # Jinja2 HTML templates
static/             # CSS, JS, images
tests/              # Pytest test suite
recipes/            # Storage for generated recipes (JSON files)
```

## API Endpoints

### Public Routes
- `GET /` - Homepage with recipe list
- `GET /recipe/<filename>` - View specific recipe
- `GET /recipe/<filename>/json` - View raw JSON (add `?raw=true` for JSON response)
- `GET /generate_recipe` - Recipe generation form
- `POST /generate_recipe` - Generate new recipe (requires prompt & model)

### API Routes (`/api`)
- `GET /api/models` - Lists available Gemini models
- `POST /api/generate_image/<filename>` - Triggers async AI image generation
- `POST /api/regenerate_image/<filename>` - Forces regeneration of the AI image
- `POST /api/report_recipe/<filename>` - Logs user reports
- `POST /api/migrate` - Migrates old recipe JSONs to the latest schema

### Authentication Routes (`/auth`)
- `GET /auth/login` - Initiate Google OAuth flow
- `GET /auth/callback` - OAuth callback handler
- `GET /auth/profile` - User profile (requires login)
- `GET /auth/logout` - Clear session and logout

## Technologies

### Backend
- **Python 3.13+** - Core language
- **Flask 3.1.2** - Web framework
- **Google Gemini API** - AI recipe generation
- **Google Imagen 3** - AI image generation
- **Google OAuth 2.0** - User authentication

### Frontend
- **HTML5** - Structure
- **CSS3** - Styling
- **JavaScript** - Interactivity
- **Jinja2** - Templating engine

### Data & Validation
- **JSON Schema (Draft 7)** - Recipe validation
- **Pydantic** - Data modeling and validation
- **jsonschema** - Schema validation library

## Getting Started

> 🚀 **Want to get started fast?** Check out our [Quick Start Guide](QUICKSTART.md) for a 5-minute setup!

### Prerequisites
- Python 3.13 or higher
- Google Cloud account with:
  - Gemini API access
  - Imagen API access (optional)
  - OAuth 2.0 credentials configured
- Unsplash API access key (optional, for stock images)

### Installation

1. **Clone the repository**
   ```bash
   cd tasteslikegood.com
   ```

2. **Install [uv](https://docs.astral.sh/uv/)** (if not already installed)
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. **Install dependencies**
   ```bash
   uv sync
   ```
   uv creates and manages the `.venv/` virtual environment automatically. Dependencies are locked in `uv.lock` — there is no `requirements.txt`.

4. **Configure environment variables**
   
   Create a `.env` file in the project root:
   ```bash
   # Google Cloud credentials
   GOOGLE_API_KEY=your_google_api_key_here
   GOOGLE_CLIENT_ID=your_client_id.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=your_client_secret
   
   # Optional: Unsplash integration
   UNSPLASH_ACCESS_KEY=your_unsplash_access_key
   
   # Optional: Flask secret key (auto-generated if not set)
   FLASK_SECRET_KEY=your_secret_key_here
   
   # Optional: Cache TTL in seconds
   RECIPES_CACHE_TTL=60
   ```

5. **Run the application**
   ```bash
   uv run python app.py
   ```

6. **Access the application**
   
   Open your browser to `http://localhost:5000`

### Docker Deployment

1. **Build the Docker image**
   ```bash
   docker build -t tasteslikegood .
   ```

2. **Run the container**
   ```bash
   docker run -p 5000:5000 --env-file .env tasteslikegood
   ```

## Testing

Run the test suite with pytest:

```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -v

# Run specific test file
uv run pytest tests/test_normalization.py

# Run specific test function
uv run pytest tests/test_normalization.py::TestNormalization::test_normalize_unit
```

## CI/CD

This project includes comprehensive CI/CD with GitHub Actions:

- ✅ **Lint** - Black formatting + Flake8 linting
- ✅ **Type Check** - mypy static analysis
- ✅ **Test** - pytest with coverage reporting
- ✅ **Security** - Safety vulnerability scanning

**Pre-commit checks:**
```bash
uv run black .
uv run flake8 .
uv run mypy . --ignore-missing-imports
uv run pytest --cov=.
```

**See:** [`CI_QUICK_REFERENCE.md`](CI_QUICK_REFERENCE.md) for all commands and [`CI_IMPLEMENTATION_COMPLETE.md`](CI_IMPLEMENTATION_COMPLETE.md) for detailed setup.

## Documentation

- **[API.md](API.md)** - Detailed API endpoint documentation
- **[CLAUDE.md](CLAUDE.md)** - Developer guide for working with this codebase
- **[AGENTS.md](AGENTS.md)** - Agent instructions for this repository
- **[gemini.md](gemini.md)** - Gemini agent configuration notes

## Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details on:
- Setting up your development environment
- Code style guidelines
- Testing requirements
- Pull request process

When contributing to this project:
1. Follow the modular architecture pattern (blueprints/services/repositories)
2. Add tests for new features
3. Update schema and normalization logic when modifying recipe structure
4. Use type hints and docstrings
5. Follow PEP 8 style guidelines

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For issues, questions, or contributions, please refer to the documentation files or contact the maintainers.

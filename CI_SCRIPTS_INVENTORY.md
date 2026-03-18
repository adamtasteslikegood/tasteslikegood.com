# Backend CI/CD Scripts Inventory

## Current State (March 3, 2026)

### ✅ **LINT** - Fully Configured

**Scripts:**
- `uv run black .` - Format code (auto-fix)
- `uv run black --check .` - Check formatting (CI mode)
- `uv run flake8 .` - Lint code with Flake8

**Configuration:**
- `.flake8` - Flake8 rules (line length 100, max complexity 10)
- `pyproject.toml` - Black config (line length 100, target Python 3.13)

---

### ✅ **TYPE CHECK** - Fully Configured

**Script:**
- `uv run mypy . --ignore-missing-imports` - Static type checking

**Configuration:**
- `pyproject.toml` - mypy settings (Python 3.13, warn on returns/unused configs)

---

### ✅ **TEST** - Fully Configured

**Scripts:**
- `uv run pytest` - Run all tests
- `uv run pytest --cov=.` - Run with coverage
- `uv run pytest --cov=. --cov-report=term` - Coverage with terminal report
- `uv run pytest --cov=. --cov-report=html` - Coverage with HTML report
- `uv run pytest -v` - Verbose mode
- `uv run pytest tests/test_auth.py` - Run specific test file

**Configuration:**
- `pyproject.toml` - pytest settings (test paths, file patterns, markers)

**Test Files:**
- `tests/test_auth.py` - OAuth authentication
- `tests/test_backend_improvements.py` - Backend improvements
- `tests/test_instruction_parsing.py` - Recipe instructions
- `tests/test_model_fetching.py` - Model discovery
- `tests/test_normalization.py` - Data normalization
- `tests/test_recipe_validation.py` - Recipe schema
- `tests/test_session_utils.py` - Session utilities
- `tests/test_stock_images.py` - Stock images

---

### ✅ **SECURITY** - Configured (Non-blocking)

**Script:**
- `uv pip install safety && uv run safety check` - Vulnerability scanning

---

### ✅ **GITHUB ACTIONS** - Fully Configured

**Workflow:** `.github/workflows/ci.yml`

**Triggers:**
- Push to `main`, `dev/backend_sub222`, `develop`
- Pull requests to these branches

**Jobs:**
1. **lint** - Black + Flake8
2. **type-check** - mypy
3. **test** - pytest with coverage
4. **security** - Safety scan

**Python Version:** 3.13
**Package Manager:** uv (with cache)

---

## Summary Table

| Check | Status | Command | Config File |
|-------|--------|---------|-------------|
| **Format** | ✅ Ready | `uv run black .` | `pyproject.toml` |
| **Lint** | ✅ Ready | `uv run flake8 .` | `.flake8` |
| **Type Check** | ✅ Ready | `uv run mypy . --ignore-missing-imports` | `pyproject.toml` |
| **Test** | ✅ Ready | `uv run pytest --cov=.` | `pyproject.toml` |
| **Security** | ✅ Ready | `uv run safety check` | N/A |
| **GitHub Actions** | ✅ Ready | Automatic on push/PR | `.github/workflows/ci.yml` |

---

## Dev Dependencies Added (Already in pyproject.toml)

```toml
[project.optional-dependencies]
dev = [
    "pytest>=7.0.0",
    "pytest-cov>=4.0.0",
    "black>=23.0.0",
    "flake8>=6.0.0",
    "mypy>=1.0.0",
]
```

These were already present - no changes needed!

---

## Quick Start

### 1. Install Dev Dependencies

```bash
cd Backend
uv sync --dev
```

### 2. Run All Checks

```bash
uv run black . && \
uv run flake8 . && \
uv run mypy . --ignore-missing-imports && \
uv run pytest --cov=. --cov-report=term
```

### 3. Commit and Push

```bash
git add .github .flake8 .gitignore CONTRIBUTING.md CI_QUICK_REFERENCE.md CI_IMPLEMENTATION_COMPLETE.md
git commit -m "ci: Add CI/CD pipeline for Flask backend"
git push origin dev/backend_sub222
```

---

## Files Created

### Configuration
1. `.github/workflows/ci.yml` - GitHub Actions workflow
2. `.flake8` - Flake8 linting rules
3. `.gitignore` - Git ignore patterns (updated)
4. `.github/pull_request_template.md` - PR template

### Documentation
1. `CI_QUICK_REFERENCE.md` - Command reference
2. `CI_IMPLEMENTATION_COMPLETE.md` - Implementation summary
3. `CONTRIBUTING.md` - Contribution guidelines
4. `CI_SCRIPTS_INVENTORY.md` - This file

---

## Comparison: Frontend vs Backend

| Aspect | Frontend | Backend |
|--------|----------|---------|
| Language | TypeScript | Python 3.13 |
| Package Manager | npm | uv |
| Formatter | Prettier | Black |
| Linter | ESLint | Flake8 |
| Type Checker | tsc | mypy |
| Test Framework | Vitest | pytest |
| Runtime | Node 20 | Python 3.13 |

---

## Additional Scripts

### Maintenance

```bash
# Update dependencies
uv sync --upgrade

# Add new dependency
uv add package-name

# Add dev dependency
uv add --dev package-name

# Remove dependency
uv remove package-name

# Check outdated packages
uv pip list --outdated
```

### Database

```bash
# Initialize database
./init_database.sh

# Create migration
uv run flask db migrate -m "Description"

# Apply migration
uv run flask db upgrade

# Rollback migration
uv run flask db downgrade
```

### Development

```bash
# Run Flask app
uv run python app.py

# Run in debug mode
FLASK_DEBUG=1 uv run python app.py

# Check syntax
./check_syntax.sh

# Check prerequisites
./check_prerequisites.sh
```

---

## Environment Setup

Required environment variables (see `.env.example`):

- `FLASK_SECRET_KEY` - Session encryption key
- `GOOGLE_API_KEY` - Gemini API key
- `GOOGLE_CLIENT_ID` - OAuth client ID
- `GOOGLE_CLIENT_SECRET` - OAuth client secret
- `DATABASE_URL` - Database connection string (optional, defaults to SQLite)

---

## Coverage Goals

- **Overall:** >70%
- **Critical paths (auth, recipe generation):** >90%
- **Utilities:** >80%
- **Models:** >60% (simple data classes)

View coverage reports:
```bash
uv run pytest --cov=. --cov-report=html
# Open htmlcov/index.html
```

---

## Status

✅ **All CI/CD components configured and ready**

**Next:** Test locally, commit, push, and verify in GitHub Actions.

---

**Date:** March 3, 2026  
**Repo:** adamtasteslikegood/tasteslikegood.com  
**Branch:** dev/backend_sub222  
**Python:** 3.13  
**Package Manager:** uv

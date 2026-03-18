# Backend CI/CD Quick Reference

## Local Development Commands

### Setup
```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install all dependencies (including dev dependencies)
uv sync
```

**Note:** 
- The `pyproject.toml` uses modern `[dependency-groups] dev = [...]` format (not deprecated `tool.uv.dev-dependencies`)
- `package = false` tells uv and hatchling that this is a Flask app, not a distributable package
- If sync fails, clear cache: `rm -rf .venv uv.lock && uv sync`

### Code Quality Checks

```bash
# Format code with Black
uv run black .

# Check formatting (without changing files)
uv run black --check .

# Lint with Flake8
uv run flake8 .

# Type check with mypy
uv run mypy . --ignore-missing-imports
```

### Testing

```bash
# Run all tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=. --cov-report=term --cov-report=html

# Run specific test file
uv run pytest tests/test_auth.py

# Run tests in verbose mode
uv run pytest -v
```

### All-in-One Pre-Commit Check

```bash
# Format, lint, type-check, and test
uv run black . && \
uv run flake8 . && \
uv run mypy . --ignore-missing-imports && \
uv run pytest --cov=. --cov-report=term
```

### Using the Helper Script

```bash
# Make script executable
chmod +x run_ci_checks.sh

# Run all checks
./run_ci_checks.sh
```

This script will:
1. Sync dependencies
2. Run Black formatter
3. Run Flake8 linter
4. Run mypy type checker
5. Run pytest tests
6. Show summary and next steps

---

## CI/CD Pipeline (GitHub Actions)

The CI pipeline runs automatically on:
- Push to `main`, `dev/backend_sub222`, or `develop` branches
- Pull requests to these branches

### Jobs

1. **Lint** - Black formatting + Flake8 linting
2. **Type Check** - mypy static type checking
3. **Test** - pytest with coverage reporting
4. **Security** - Safety vulnerability scanning

All jobs run in parallel on Python 3.13 using `uv` for fast dependency management.

---

## Configuration Files

| File | Purpose |
|------|---------|
| `.flake8` | Flake8 linting rules |
| `pyproject.toml` | Black, pytest, mypy config + dependencies |
| `.github/workflows/ci.yml` | GitHub Actions workflow |
| `.gitignore` | Git ignore patterns |

---

## Tools & Versions

- **Python:** 3.13+
- **Package Manager:** uv (faster than pip)
- **Formatter:** Black (line length: 100)
- **Linter:** Flake8 (max complexity: 10)
- **Type Checker:** mypy
- **Test Framework:** pytest with pytest-cov
- **Security Scanner:** Safety

---

## Pre-Commit Checklist

Before committing/pushing:

- [ ] `uv run black .` - Format code
- [ ] `uv run flake8 .` - Check linting
- [ ] `uv run mypy . --ignore-missing-imports` - Type check
- [ ] `uv run pytest --cov=.` - Run tests with coverage
- [ ] Check that coverage is reasonable (aim for >70%)

---

## Troubleshooting

### "uv: command not found"
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc  # or ~/.zshrc
```

### Dependencies out of sync
```bash
rm -rf .venv
uv sync --dev
```

### Tests failing locally but passing in CI
- Check Python version: `python --version` (should be 3.13+)
- Ensure environment variables are set (see `.env.example`)
- Clean test artifacts: `rm -rf .pytest_cache __pycache__`

### Flake8 errors
```bash
# See specific errors
uv run flake8 . --show-source

# Auto-fix with Black first
uv run black .
```

### Type checking errors
```bash
# Run mypy with verbose output
uv run mypy . --ignore-missing-imports --show-error-codes
```

---

## Adding New Dependencies

```bash
# Add production dependency
uv add package-name

# Add dev dependency
uv add --dev package-name

# Update all dependencies
uv sync --upgrade
```

Dependencies are tracked in `pyproject.toml` and locked in `uv.lock`.

---

## Coverage Reports

After running tests with coverage:

```bash
# View in terminal
uv run pytest --cov=. --cov-report=term

# Generate HTML report
uv run pytest --cov=. --cov-report=html
# Open htmlcov/index.html in browser
```

---

## Branch Strategy

- **main** - Production-ready code
- **dev/backend_sub222** - Development branch for backend submodule
- **develop** - Integration branch

All branches require CI checks to pass before merging.

---

## Documentation

- `README.md` - Main project documentation
- `CONTRIBUTING.md` - Contribution guidelines
- `UV_QUICK_REFERENCE.md` - uv package manager guide
- `DATABASE_SETUP.md` - Database configuration
- `API.md` - API documentation

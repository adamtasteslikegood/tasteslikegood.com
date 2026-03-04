## Pre-Commit Checklist

Before committing your Python/Flask changes:

```bash
cd Backend

# 1. Format code
uv run black .

# 2. Lint code
uv run flake8 .

# 3. Type check
uv run mypy . --ignore-missing-imports

# 4. Run tests
uv run pytest --cov=. --cov-report=term
```

## All-in-One

```bash
cd Backend && \
uv run black . && \
uv run flake8 . && \
uv run mypy . --ignore-missing-imports && \
uv run pytest --cov=.
```

## CI Status

GitHub Actions will run these same checks automatically on push/PR.

View status: Go to repo → **Actions** tab

---

## Quick Commands

| Task | Command |
|------|---------|
| Format | `uv run black .` |
| Check format | `uv run black --check .` |
| Lint | `uv run flake8 .` |
| Type check | `uv run mypy . --ignore-missing-imports` |
| Test | `uv run pytest` |
| Test + coverage | `uv run pytest --cov=. --cov-report=term` |
| Test verbose | `uv run pytest -v` |
| Test specific | `uv run pytest tests/test_auth.py` |

---

See `CI_QUICK_REFERENCE.md` for full documentation.

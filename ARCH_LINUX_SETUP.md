# Arch Linux / Modern Python Setup Guide (with uv)

This project uses **uv**, a fast Python package manager written in Rust. It's significantly faster than pip and handles virtual environments automatically.

---

## Why uv?

✅ **Fast**: 10-100x faster than pip  
✅ **Automatic**: Manages virtual environments for you  
✅ **Reproducible**: Uses `uv.lock` for exact dependency versions  
✅ **Compatible**: Works with `pyproject.toml` and PEP 668  
✅ **No activation needed**: Use `uv run` for any command

---

## Installation

### On Arch Linux

```bash
# Option 1: From AUR
yay -S uv
# or
paru -S uv

# Option 2: Official installer (recommended)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Option 3: Via pip (if you have pipx)
pipx install uv
```

### Verify Installation

```bash
uv --version
```

---

## Quick Start

```bash
cd Backend

# 1. Sync dependencies (creates .venv automatically)
uv sync

# 2. Run any Python command with 'uv run'
uv run python app.py

# 3. Or use the automated setup script
./init_database.sh
```

That's it! No need to manually activate virtual environments.

---

## Common Commands

### Running Python Scripts

```bash
# Run the Flask backend
uv run python app.py

# Run Flask CLI commands
uv run flask db init
uv run flask db migrate -m "Message"
uv run flask db upgrade

# Run migration script
uv run python scripts/migrate_recipes_to_db.py
```

### Managing Dependencies

```bash
# Sync dependencies (install/update based on uv.lock)
uv sync

# Add a new package
uv add package-name

# Add a dev dependency
uv add --dev package-name

# Update all dependencies
uv sync --upgrade

# Update a specific package
uv add --upgrade package-name
```

### Virtual Environment

```bash
# uv creates .venv automatically on first 'uv sync'
# No need to activate it manually!

# But if you want to activate it traditionally:
source .venv/bin/activate
python app.py  # Now works without 'uv run'
deactivate

# Or use uv shell (interactive shell with venv activated)
uv shell
```

---

## How uv Works

1. **First run of `uv sync`**:
   - Reads `pyproject.toml`
   - Creates `.venv` directory
   - Installs all dependencies from `uv.lock`
   - Takes ~2-5 seconds (vs minutes with pip!)

2. **Subsequent `uv run` commands**:
   - Automatically uses `.venv`
   - No activation needed
   - Just prefix any command with `uv run`

---

## Database Setup with uv

### Option 1: Automated (Recommended)

```bash
cd Backend
./init_database.sh
```

The script uses `uv run` for all commands automatically.

### Option 2: Manual

```bash
cd Backend

# 1. Sync dependencies
uv sync

# 2. Set Flask app
export FLASK_APP=app.py

# 3. Initialize database
uv run flask db init
uv run flask db migrate -m "Initial schema"
uv run flask db upgrade

# 4. Verify
uv run python -c "from app import create_app; from extensions import db; app = create_app(); app.app_context().push(); db.session.execute('SELECT 1'); print('✅ Database works!')"

# 5. Start backend
uv run python app.py
```

---

## Project Structure

```
Backend/
├── pyproject.toml     # Project dependencies and metadata
├── uv.lock           # Locked dependency versions (like package-lock.json)
├── .venv/            # Virtual environment (created by uv)
├── requirements.txt  # Legacy file (optional, for compatibility)
└── app.py           # Main Flask app
```

---

## Advantages over pip + venv

| Task | Traditional | With uv |
|------|-------------|---------|
| Create venv | `python -m venv .venv` | Automatic |
| Activate | `source .venv/bin/activate` | Not needed |
| Install deps | `pip install -r requirements.txt` | `uv sync` |
| Run script | `python app.py` | `uv run python app.py` |
| Speed | Slow (minutes) | Fast (seconds) |
| Lock file | `pip freeze` | `uv.lock` (automatic) |

---

## Common Issues

### "uv: command not found"

```bash
# Install uv first
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or on Arch:
yay -S uv
```

### "error: externally-managed-environment"

This error **doesn't happen with uv**! That's one of the benefits. uv always uses virtual environments, bypassing PEP 668 restrictions.

### "No pyproject.toml found"

```bash
# Make sure you're in the Backend directory
cd Backend
pwd  # Should show: /path/to/Backend
ls pyproject.toml  # Should exist
```

### Dependencies out of sync

```bash
# Resync from uv.lock
uv sync

# Or if lock file is outdated
uv sync --upgrade
```

### Want to use traditional venv activation?

```bash
# Create the venv
uv sync

# Activate it manually
source .venv/bin/activate

# Now you can use python directly
python app.py
flask db upgrade

# Deactivate when done
deactivate
```

---

## Why Not System Packages?

```bash
# DON'T do this (not reproducible, version conflicts)
sudo pacman -S python-flask python-sqlalchemy

# DO this instead (isolated, reproducible)
uv sync
uv run python app.py
```

---

## Updating Dependencies

```bash
# Update a single package
uv add package-name --upgrade

# Update all packages
uv sync --upgrade

# This updates uv.lock with new versions
```

---

## Working with requirements.txt (Legacy)

uv can still use `requirements.txt` for compatibility:

```bash
# Install from requirements.txt
uv pip install -r requirements.txt

# But prefer using pyproject.toml + uv.lock
uv sync
```

---

## Performance Comparison

```
Install 50 packages:
  pip:  120 seconds  ⏳
  uv:   3 seconds    ⚡
```

---

## Documentation & Resources

- **uv Documentation**: https://docs.astral.sh/uv/
- **uv GitHub**: https://github.com/astral-sh/uv
- **PEP 621** (pyproject.toml): https://peps.python.org/pep-0621/

---

## Quick Reference Card

```bash
# Setup (once)
uv sync

# Run Flask
uv run python app.py

# Database migrations
uv run flask db init
uv run flask db migrate -m "Message"
uv run flask db upgrade

# Add package
uv add package-name

# Update all
uv sync --upgrade

# Interactive shell
uv shell
```

---

**Last Updated**: March 1, 2026  
**uv Version**: Latest (check with `uv --version`)


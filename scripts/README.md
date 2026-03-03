# Backend Utility Scripts

This directory contains utility scripts for database maintenance, migrations, and testing.

## Available Scripts

### 📋 Recipe ID Consistency

#### `fix_recipe_ids.py`
**Purpose**: Fixes the dual-ID issue in existing recipes by ensuring `Recipe.data.id` matches `Recipe.id`.

**When to use**:
- After upgrading to the ID-consistency fix
- When migrating data from other sources
- If you suspect ID mismatches in your database

**Usage**:
```bash
cd Backend
python scripts/fix_recipe_ids.py
```

**What it does**:
1. Loads all recipes from the database
2. Checks if each recipe's `data.id` matches its database `id`
3. Updates mismatched recipes to use the database ID
4. Commits changes and reports results

**Output Example**:
```
INFO:__main__:Found 42 recipes to check
INFO:__main__:Fixing recipe 'Test Soup': DB id=ca55f18a..., data.id=test-r-222
INFO:__main__:✓ Updated 3 recipes with corrected IDs
```

**Safety**: 
- Uses transactions (auto-rollback on error)
- Logs all changes before committing
- Idempotent (safe to run multiple times)

---

#### `test_recipe_id_fix.py`
**Purpose**: Automated tests to verify the recipe ID consistency fix works correctly.

**When to use**:
- After applying the ID consistency fix
- Before deploying to production
- When verifying database migrations

**Usage**:
```bash
cd Backend
python scripts/test_recipe_id_fix.py
```

**Tests Performed**:
1. ✓ Recipe with existing ID preserves it
2. ✓ Recipe without ID gets a generated UUID
3. ✓ Updates maintain ID consistency

**Output Example**:
```
=== Running Recipe ID Consistency Tests ===

Test 1: Create recipe with existing ID
✓ Recipe created with consistent ID: test-recipe-123

Test 2: Create recipe without ID
✓ Recipe created with generated UUID: a7b3c4d5-e6f7-8g9h-0i1j-k2l3m4n5o6p7

Test 3: Update recipe maintains ID consistency
✓ Recipe updated with consistent ID: test-recipe-update

=== Test Results ===
Passed: 3
Failed: 0
```

**Exit Codes**:
- `0` - All tests passed
- `1` - One or more tests failed

---

## Adding New Scripts

When adding utility scripts to this directory:

1. **Use a descriptive name** that indicates purpose (e.g., `migrate_user_data.py`)
2. **Include a docstring** at the top explaining:
   - What the script does
   - When to use it
   - How to run it
3. **Use logging** instead of `print()` for output
4. **Handle errors gracefully** with try/except and db rollback
5. **Make it idempotent** when possible (safe to run multiple times)
6. **Add it to this README** with documentation

### Template

```python
#!/usr/bin/env python3
"""
Brief description of what this script does.

Longer explanation of when and why you'd use this script.

Usage:
    python scripts/script_name.py
"""

import sys
import logging
from pathlib import Path

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from extensions import db
from app import app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    """Main script logic."""
    with app.app_context():
        try:
            # Your code here
            db.session.commit()
            logger.info("✓ Success")
        except Exception as e:
            logger.error(f"✗ Error: {e}")
            db.session.rollback()
            return False
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
```

---

## Prerequisites

All scripts require:
- Python 3.8+
- Flask app context (access to database)
- Backend dependencies installed (`pip install -r requirements.txt`)
- Database configured (see `Backend/DATABASE_SETUP.md`)

## Running in Production

**⚠️ Important**: Always:
1. **Backup your database** before running migration scripts
2. **Test in a staging environment** first
3. **Run during low-traffic periods** if possible
4. **Monitor logs** during and after execution

## Related Documentation

- [docs/RECIPE_ID_FIX.md](../../docs/RECIPE_ID_FIX.md) - Details on the recipe ID consistency fix
- [Backend/DATABASE_SETUP.md](../DATABASE_SETUP.md) - Database configuration
- [Backend/README.md](../README.md) - Backend overview

---

**Questions?** See the project documentation in `docs/` or check `CONTRIBUTING.md`.

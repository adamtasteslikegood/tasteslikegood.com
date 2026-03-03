#!/usr/bin/env python3
"""
Verify that recipe IDs are consistent after the migration.

This script queries the API and checks that the outer 'id' matches 'data.id'
for all recipes, showing you the actual consistency.

Usage:
    python scripts/verify_recipe_ids.py
"""

import logging
import sys
from pathlib import Path

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from app import app
from extensions import db
from models import Recipe

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def verify_recipe_ids():
    """
    Check all recipes and verify ID consistency.
    Shows exactly what the API would return.
    """
    with app.app_context():
        try:
            recipes = Recipe.query.all()

            if not recipes:
                logger.info("No recipes found in database")
                return True

            logger.info(f"\n{'=' * 70}")
            logger.info(f"Checking {len(recipes)} recipe(s) for ID consistency")
            logger.info(f"{'=' * 70}\n")

            consistent = 0
            inconsistent = 0

            for i, recipe in enumerate(recipes, 1):
                db_id = recipe.id
                data_id = recipe.data.get("id") if recipe.data else None

                matches = db_id == data_id
                status = "✅ CONSISTENT" if matches else "❌ INCONSISTENT"

                logger.info(f"Recipe {i}: {recipe.name}")
                logger.info(f"  Database ID:  {db_id}")
                logger.info(f"  Data ID:      {data_id}")
                logger.info(f"  Status:       {status}")

                if matches:
                    consistent += 1
                else:
                    inconsistent += 1
                    logger.warning(
                        f"  ⚠️  IDs don't match! Run fix_recipe_ids.py again."
                    )

                # Show what the API returns
                logger.info(f"\n  API Response Structure:")
                logger.info(f"  {{")
                logger.info(f'    "id": "{db_id}",')
                logger.info(f'    "name": "{recipe.name}",')
                logger.info(f'    "data": {{')
                logger.info(f'      "id": "{data_id}",  ← Should match outer id')
                logger.info(f'      "name": "{recipe.name}",')
                logger.info(f"      ...")
                logger.info(f"    }}")
                logger.info(f"  }}\n")
                logger.info(f"{'-' * 70}\n")

            logger.info(f"{'=' * 70}")
            logger.info(f"SUMMARY:")
            logger.info(f"  ✅ Consistent:   {consistent}")
            logger.info(f"  ❌ Inconsistent: {inconsistent}")
            logger.info(f"{'=' * 70}\n")

            if inconsistent > 0:
                logger.error("❌ Some recipes still have inconsistent IDs!")
                logger.error("   Action: Run 'python scripts/fix_recipe_ids.py' again")
                return False
            else:
                logger.info("✅ All recipes have consistent IDs!")
                logger.info(
                    "\n📝 Note: The API response will still show BOTH 'id' and 'data'"
                )
                logger.info(
                    "   fields - this is normal! What matters is they now MATCH."
                )
                return True

        except Exception as e:
            logger.error(f"Error verifying recipe IDs: {e}")
            return False


if __name__ == "__main__":
    logger.info("Verifying recipe ID consistency...\n")
    success = verify_recipe_ids()
    sys.exit(0 if success else 1)

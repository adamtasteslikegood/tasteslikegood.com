#!/usr/bin/env python3
"""
IMPROVED migration script to fix dual-ID issue in existing recipes.

This handles multiple scenarios:
1. data is None
2. data is a dict
3. data is a string (JSON serialized)
4. data.id is missing or wrong

Usage:
    python scripts/fix_recipe_ids_v2.py
"""

import json
import logging
import sys
from pathlib import Path

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from sqlalchemy.orm.attributes import flag_modified

from app import app
from extensions import db
from models import Recipe

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def fix_recipe_ids():
    """
    Update all recipes to ensure data.id matches Recipe.id.
    Handles various data formats and edge cases.
    """
    with app.app_context():
        try:
            recipes = Recipe.query.all()
            updated_count = 0

            logger.info(f"Found {len(recipes)} recipes to check")

            for recipe in recipes:
                needs_update = False

                # Handle None data
                if recipe.data is None:
                    logger.info(f"Recipe '{recipe.name}' has NULL data - initializing")
                    recipe.data = {"id": recipe.id, "name": recipe.name}
                    needs_update = True
                else:
                    # Handle string data (shouldn't happen, but just in case)
                    if isinstance(recipe.data, str):
                        logger.warning(f"Recipe '{recipe.name}' has string data - parsing")
                        try:
                            data = json.loads(recipe.data)
                        except Exception:
                            logger.error(f"Failed to parse data for '{recipe.name}' - skipping")
                            continue
                    else:
                        data = recipe.data.copy() if isinstance(recipe.data, dict) else {}

                    # Check if data has an id field and if it differs from the database id
                    data_id = data.get("id")

                    if data_id != recipe.id:
                        logger.info(
                            f"Fixing recipe '{recipe.name}': "
                            f"DB id={recipe.id}, data.id={data_id}"
                        )

                        # Update data to include the correct id
                        data["id"] = recipe.id
                        recipe.data = data
                        needs_update = True

                if needs_update:
                    # CRITICAL: Tell SQLAlchemy the JSON field was modified
                    flag_modified(recipe, "data")
                    updated_count += 1

                    # Verify the change took effect
                    db.session.flush()
                    logger.debug(f"  Verified: recipe.data['id'] = {recipe.data.get('id')}")

            if updated_count > 0:
                logger.info(f"Committing {updated_count} changes to database...")
                db.session.commit()
                logger.info(f"✓ Successfully committed {updated_count} recipes")

                # Double-check the changes persisted
                logger.info("Verifying changes persisted...")
                for recipe in Recipe.query.all():
                    if recipe.data:
                        data_id = recipe.data.get("id")
                        if data_id != recipe.id:
                            logger.error(
                                f"❌ Verification FAILED for '{recipe.name}': "
                                f"DB id={recipe.id}, data.id={data_id}"
                            )
                            return False

                logger.info("✓ Verification passed - all IDs are consistent")
            else:
                logger.info("✓ All recipes already have consistent IDs")

            return True

        except Exception as e:
            logger.error(f"Error fixing recipe IDs: {e}")
            logger.exception(e)
            db.session.rollback()
            return False


if __name__ == "__main__":
    logger.info("Starting recipe ID migration (v2 - improved)...")
    success = fix_recipe_ids()

    if success:
        logger.info("✓ Migration completed successfully")
        sys.exit(0)
    else:
        logger.error("✗ Migration failed")
        sys.exit(1)

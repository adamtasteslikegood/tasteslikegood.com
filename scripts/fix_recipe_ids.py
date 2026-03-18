#!/usr/bin/env python3
"""
Migration script to fix dual-ID issue in existing recipes.

This script ensures that:
1. The Recipe.data JSON field contains an 'id' that matches the Recipe.id (database primary key)
2. Any recipes with mismatched IDs are updated to use the database ID consistently

Run this after updating the repository code to fix existing data.

Usage:
    python scripts/fix_recipe_ids.py
"""

import json
import logging
import sys
from pathlib import Path

# Add Backend directory to path so we can import from models/extensions
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from app import app
from extensions import db
from models import Recipe

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def fix_recipe_ids():
    """
    Update all recipes to ensure data.id matches Recipe.id.
    """
    with app.app_context():
        try:
            recipes = Recipe.query.all()
            updated_count = 0

            logger.info(f"Found {len(recipes)} recipes to check")

            for recipe in recipes:
                # Normalize JSON payload into a plain dict before updating.
                if recipe.data is None:
                    data = {}
                elif isinstance(recipe.data, dict):
                    data = dict(recipe.data)
                elif isinstance(recipe.data, str):
                    try:
                        parsed = json.loads(recipe.data)
                        data = parsed if isinstance(parsed, dict) else {}
                    except json.JSONDecodeError:
                        data = {}
                else:
                    data = {}

                # Check if data has an id field and if it differs from the database id
                data_id = data.get("id")

                if data_id != recipe.id:
                    logger.info(
                        f"Fixing recipe '{recipe.name}': " f"DB id={recipe.id}, data.id={data_id}"
                    )

                    # Update data to include the correct id
                    data["id"] = recipe.id
                    recipe.data = data

                    updated_count += 1
                    logger.debug(f"  After fix: data={recipe.data}")

            if updated_count > 0:
                logger.info(f"Committing {updated_count} changes to database...")
                db.session.commit()
                logger.info(f"✓ Updated {updated_count} recipes with corrected IDs")
            else:
                logger.info("✓ All recipes already have consistent IDs")

        except Exception as e:
            logger.error(f"Error fixing recipe IDs: {e}")
            db.session.rollback()
            return False

    return True


if __name__ == "__main__":
    logger.info("Starting recipe ID migration...")
    success = fix_recipe_ids()

    if success:
        logger.info("✓ Migration completed successfully")
        sys.exit(0)
    else:
        logger.error("✗ Migration failed")
        sys.exit(1)

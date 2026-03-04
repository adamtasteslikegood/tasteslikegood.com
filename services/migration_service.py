import os
import logging
from typing import List, Dict, Any
from config import RECIPES_DIR
from repositories.recipe_repository import (
    get_recipe,
    save_recipe,
    migrate_recipe_data,
    invalidate_cache,
)

logger = logging.getLogger(__name__)


class MigrationService:
    @staticmethod
    def migrate_all_recipes() -> Dict[str, Any]:
        """
        Migrate all recipes in the recipes directory to the latest schema.

        Returns:
            Dict[str, Any]: Results of the migration including count and files updated.
        """
        count = 0
        updated_files = []

        if not os.path.exists(RECIPES_DIR):
            logger.warning(f"Recipes directory {RECIPES_DIR} does not exist.")
            return {"migrated_count": 0, "files": []}

        for filename in os.listdir(RECIPES_DIR):
            if not filename.endswith(".json"):
                continue

            try:
                # Use repository methods with file locking
                recipe_data = get_recipe(filename)

                # Migrate data using shared logic in repository (or move it here if appropriate)
                recipe_data, changed = migrate_recipe_data(recipe_data, filename)

                if changed:
                    save_recipe(filename, recipe_data)
                    count += 1
                    updated_files.append(filename)
                    logger.info(f"Migrated {filename}")

            except Exception as e:
                logger.error(f"Error migrating {filename}: {e}")

        # Cache is already invalidated by save_recipe, but ensure it's cleared
        if count > 0:
            invalidate_cache()
            logger.info(f"Completed migration of {count} files.")

        return {"migrated_count": count, "files": updated_files}

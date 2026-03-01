"""
Database-backed recipe repository for Phase 3.

Handles recipe CRUD operations with SQLAlchemy ORM:
- Create, read, update, delete recipes
- User-scoped queries (recipes belong to users)
- Support for anonymous recipes (user_id = NULL)
"""
import uuid
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime

from extensions import db
from models import Recipe, User

logger = logging.getLogger(__name__)


def get_user_recipes(user_id: Optional[int]) -> List[Recipe]:
    """
    Get all recipes for a specific user.

    Args:
        user_id: Database ID of the user (None for anonymous recipes)

    Returns:
        List of Recipe objects sorted by creation date (newest first)
    """
    try:
        query = Recipe.query.filter_by(user_id=user_id).order_by(Recipe.created_at.desc())
        return query.all()
    except Exception as e:
        logger.error(f"Error fetching recipes for user {user_id}: {e}")
        return []


def get_recipe_by_id(recipe_id: str, user_id: Optional[int] = None) -> Optional[Recipe]:
    """
    Get a specific recipe by ID.

    Args:
        recipe_id: UUID of the recipe
        user_id: Optional user ID for ownership verification

    Returns:
        Recipe object if found and owned by user (or anonymous), None otherwise
    """
    try:
        query = Recipe.query.filter_by(id=recipe_id)

        # If user_id specified, enforce ownership
        if user_id is not None:
            query = query.filter_by(user_id=user_id)

        return query.first()
    except Exception as e:
        logger.error(f"Error fetching recipe {recipe_id}: {e}")
        return None


def create_recipe(recipe_data: Dict[str, Any], user_id: Optional[int] = None) -> Optional[Recipe]:
    """
    Create a new recipe in the database.

    Args:
        recipe_data: Full recipe JSON data
        user_id: Optional user ID (None for anonymous recipes)

    Returns:
        Created Recipe object, or None if creation failed
    """
    try:
        recipe_id = str(uuid.uuid4())
        recipe_name = recipe_data.get('name', 'Unnamed Recipe')

        recipe = Recipe(
            id=recipe_id,
            user_id=user_id,
            name=recipe_name,
            data=recipe_data
        )

        db.session.add(recipe)
        db.session.commit()

        logger.info(f"Created recipe {recipe_id} for user {user_id}")
        return recipe

    except Exception as e:
        logger.error(f"Error creating recipe: {e}")
        db.session.rollback()
        return None


def update_recipe(recipe_id: str, recipe_data: Dict[str, Any], user_id: Optional[int] = None) -> Optional[Recipe]:
    """
    Update an existing recipe.

    Args:
        recipe_id: UUID of the recipe to update
        recipe_data: New recipe JSON data
        user_id: Optional user ID for ownership verification

    Returns:
        Updated Recipe object, or None if not found or update failed
    """
    try:
        recipe = get_recipe_by_id(recipe_id, user_id)

        if not recipe:
            logger.warning(f"Recipe {recipe_id} not found for user {user_id}")
            return None

        # Update fields
        recipe.name = recipe_data.get('name', recipe.name)
        recipe.data = recipe_data
        recipe.updated_at = datetime.utcnow()

        db.session.commit()

        logger.info(f"Updated recipe {recipe_id}")
        return recipe

    except Exception as e:
        logger.error(f"Error updating recipe {recipe_id}: {e}")
        db.session.rollback()
        return None


def delete_recipe(recipe_id: str, user_id: Optional[int] = None) -> bool:
    """
    Delete a recipe from the database.

    Args:
        recipe_id: UUID of the recipe to delete
        user_id: Optional user ID for ownership verification

    Returns:
        True if deleted successfully, False otherwise
    """
    try:
        recipe = get_recipe_by_id(recipe_id, user_id)

        if not recipe:
            logger.warning(f"Recipe {recipe_id} not found for user {user_id}")
            return False

        db.session.delete(recipe)
        db.session.commit()

        logger.info(f"Deleted recipe {recipe_id}")
        return True

    except Exception as e:
        logger.error(f"Error deleting recipe {recipe_id}: {e}")
        db.session.rollback()
        return False


def get_all_recipes(limit: int = 100) -> List[Recipe]:
    """
    Get all recipes across all users (admin function).

    Args:
        limit: Maximum number of recipes to return

    Returns:
        List of Recipe objects
    """
    try:
        return Recipe.query.order_by(Recipe.created_at.desc()).limit(limit).all()
    except Exception as e:
        logger.error(f"Error fetching all recipes: {e}")
        return []


def count_user_recipes(user_id: Optional[int]) -> int:
    """
    Count the number of recipes for a user.

    Args:
        user_id: Database ID of the user

    Returns:
        Number of recipes
    """
    try:
        return Recipe.query.filter_by(user_id=user_id).count()
    except Exception as e:
        logger.error(f"Error counting recipes for user {user_id}: {e}")
        return 0


def migrate_file_to_db(filename: str, recipe_data: Dict[str, Any], user_id: Optional[int] = None) -> Optional[Recipe]:
    """
    Migrate a file-based recipe to the database.

    Uses the filename (without .json) as the recipe ID to maintain consistency.

    Args:
        filename: Original filename (e.g., "recipe-uuid.json")
        recipe_data: Recipe JSON data
        user_id: Optional user ID to assign ownership

    Returns:
        Created Recipe object, or None if creation failed
    """
    try:
        # Extract UUID from filename
        recipe_id = filename.replace('.json', '')
        recipe_name = recipe_data.get('name', 'Unnamed Recipe')

        # Check if already exists
        existing = Recipe.query.filter_by(id=recipe_id).first()
        if existing:
            logger.warning(f"Recipe {recipe_id} already exists in database, skipping")
            return existing

        recipe = Recipe(
            id=recipe_id,
            user_id=user_id,
            name=recipe_name,
            data=recipe_data
        )

        db.session.add(recipe)
        db.session.commit()

        logger.info(f"Migrated recipe {recipe_id} from file {filename}")
        return recipe

    except Exception as e:
        logger.error(f"Error migrating recipe {filename}: {e}")
        db.session.rollback()
        return None

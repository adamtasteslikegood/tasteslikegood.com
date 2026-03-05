"""
Database-backed recipe repository for Phase 3.

Handles recipe CRUD operations with SQLAlchemy ORM:
- Create, read, update, delete recipes
- User-scoped queries (recipes belong to users)
- Support for anonymous recipes (user_id = NULL)
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from extensions import db
from models import Recipe, User

logger = logging.getLogger(__name__)


def _apply_recipe_scope(query, user_id: Optional[int], guest_session_id: Optional[str]):
    """Scope queries to either authenticated user or anonymous session."""
    if user_id is not None:
        return query.filter_by(user_id=user_id)
    if guest_session_id:
        return query.filter_by(user_id=None, guest_session_id=guest_session_id)
    return query.filter_by(user_id=None, guest_session_id=None)


def get_user_recipes(
    user_id: Optional[int], guest_session_id: Optional[str] = None
) -> List[Recipe]:
    """
    Get all recipes for a specific user.

    Args:
        user_id: Database ID of the user (None for anonymous recipes)

    Returns:
        List of Recipe objects sorted by creation date (newest first)
    """
    try:
        query = _apply_recipe_scope(Recipe.query, user_id, guest_session_id).order_by(
            Recipe.created_at.desc()
        )
        return query.all()  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(f"Error fetching recipes for user {user_id}: {e}")
        return []


def get_recipe_by_id(
    recipe_id: str,
    user_id: Optional[int] = None,
    guest_session_id: Optional[str] = None,
) -> Optional[Recipe]:
    """
    Get a specific recipe by ID.

    Args:
        recipe_id: UUID of the recipe
        user_id: Optional user ID for ownership verification

    Returns:
        Recipe object if found and owned by user (or anonymous), None otherwise
    """
    try:
        query = _apply_recipe_scope(Recipe.query.filter_by(id=recipe_id), user_id, guest_session_id)

        return query.first()  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(f"Error fetching recipe {recipe_id}: {e}")
        return None


def create_recipe(
    recipe_data: Dict[str, Any],
    user_id: Optional[int] = None,
    guest_session_id: Optional[str] = None,
) -> Optional[Recipe]:
    """
    Create a new recipe in the database.

    Args:
        recipe_data: Full recipe JSON data
        user_id: Optional user ID (None for anonymous recipes)

    Returns:
        Created Recipe object, or None if creation failed
    """
    try:
        # Use the id from recipe_data if present, otherwise generate a new UUID
        recipe_id = recipe_data.get("id", str(uuid.uuid4()))
        recipe_name = recipe_data.get("name", "Unnamed Recipe")

        # Ensure the id in recipe_data matches the database record id
        recipe_data_with_id = {**recipe_data, "id": recipe_id}

        existing = Recipe.query.filter_by(id=recipe_id).first()  # type: ignore[no-any-return]
        if existing:
            same_owner = (user_id is not None and existing.user_id == user_id) or (
                user_id is None
                and existing.user_id is None
                and existing.guest_session_id == guest_session_id
            )
            if not same_owner:
                logger.warning(
                    "Recipe ID collision for id=%s (user_id=%s, guest_session_id=%s)",
                    recipe_id,
                    user_id,
                    guest_session_id,
                )
                return None

            existing.name = recipe_name
            existing.data = recipe_data_with_id
            existing.updated_at = datetime.utcnow()
            db.session.commit()
            return existing  # type: ignore[no-any-return]

        recipe = Recipe(
            id=recipe_id,
            user_id=user_id,
            guest_session_id=None if user_id is not None else guest_session_id,
            name=recipe_name,
            data=recipe_data_with_id,
        )

        db.session.add(recipe)
        db.session.commit()

        logger.info(f"Created recipe {recipe_id} for user {user_id}")
        return recipe

    except Exception as e:
        logger.error(f"Error creating recipe: {e}")
        db.session.rollback()
        return None


def update_recipe(
    recipe_id: str,
    recipe_data: Dict[str, Any],
    user_id: Optional[int] = None,
    guest_session_id: Optional[str] = None,
) -> Optional[Recipe]:
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
        recipe = get_recipe_by_id(recipe_id, user_id, guest_session_id)

        if not recipe:
            logger.warning(f"Recipe {recipe_id} not found for user {user_id}")
            return None

        # Ensure the id in recipe_data matches the database record id
        recipe_data_with_id = {**recipe_data, "id": recipe_id}

        # Update fields
        recipe.name = recipe_data.get("name", recipe.name)
        recipe.data = recipe_data_with_id
        recipe.updated_at = datetime.utcnow()

        db.session.commit()

        logger.info(f"Updated recipe {recipe_id}")
        return recipe

    except Exception as e:
        logger.error(f"Error updating recipe {recipe_id}: {e}")
        db.session.rollback()
        return None


def delete_recipe(
    recipe_id: str,
    user_id: Optional[int] = None,
    guest_session_id: Optional[str] = None,
) -> bool:
    """
    Delete a recipe from the database.

    Args:
        recipe_id: UUID of the recipe to delete
        user_id: Optional user ID for ownership verification

    Returns:
        True if deleted successfully, False otherwise
    """
    try:
        recipe = get_recipe_by_id(recipe_id, user_id, guest_session_id)

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
        return Recipe.query.order_by(Recipe.created_at.desc()).limit(limit).all()  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(f"Error fetching all recipes: {e}")
        return []


def count_user_recipes(user_id: Optional[int], guest_session_id: Optional[str] = None) -> int:
    """
    Count the number of recipes for a user.

    Args:
        user_id: Database ID of the user

    Returns:
        Number of recipes
    """
    try:
        return _apply_recipe_scope(Recipe.query, user_id, guest_session_id).count()  # type: ignore[no-any-return]
    except Exception as e:
        logger.error(f"Error counting recipes for user {user_id}: {e}")
        return 0


def migrate_file_to_db(
    filename: str, recipe_data: Dict[str, Any], user_id: Optional[int] = None
) -> Optional[Recipe]:
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
        recipe_id = filename.replace(".json", "")
        recipe_name = recipe_data.get("name", "Unnamed Recipe")

        # Check if already exists
        existing = Recipe.query.filter_by(id=recipe_id).first()  # type: ignore[no-any-return]
        if existing:
            logger.warning(f"Recipe {recipe_id} already exists in database, skipping")
            return existing  # type: ignore[no-any-return]

        recipe = Recipe(id=recipe_id, user_id=user_id, name=recipe_name, data=recipe_data)

        db.session.add(recipe)
        db.session.commit()

        logger.info(f"Migrated recipe {recipe_id} from file {filename}")
        return recipe

    except Exception as e:
        logger.error(f"Error migrating recipe {filename}: {e}")
        db.session.rollback()
        return None

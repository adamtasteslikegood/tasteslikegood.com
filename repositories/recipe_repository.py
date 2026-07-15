"""
Recipe repository for file-based persistence.

Handles:
- Recipe file CRUD operations
- In-memory caching with TTL
- File locking to prevent race conditions
- Filename sanitization and validation
- Recipe data migration to latest schema
"""

import os
import json
import time
import datetime
import fcntl  # Unix file locking
import logging
from typing import List, Dict, Any, Tuple, Generator
from contextlib import contextmanager
from config import RECIPES_DIR, _recipes_cache, _RECIPES_CACHE_TTL

logger = logging.getLogger(__name__)


def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename to prevent path traversal attacks.

    Args:
        filename: Filename to sanitize

    Returns:
        str: Sanitized filename

    Raises:
        ValueError: If filename is invalid or doesn't end with .json
    """
    # Use only the basename to prevent directory traversal
    safe_filename = os.path.basename(filename)
    # Additional validation: ensure it's not empty and ends with .json
    if not safe_filename or not safe_filename.endswith(".json"):
        raise ValueError(f"Invalid filename: {filename}")
    return safe_filename


def validate_recipe_filepath(filename: str) -> str:
    """
    Validate that a recipe filepath is safe and within RECIPES_DIR.

    Args:
        filename: Filename to validate

    Returns:
        str: Full validated filepath

    Raises:
        ValueError: If filename is invalid or path traversal detected
    """
    try:
        safe_filename = sanitize_filename(filename)
        filepath = os.path.join(RECIPES_DIR, safe_filename)
        # Resolve to absolute path and verify it's within RECIPES_DIR
        abs_filepath = os.path.abspath(filepath)
        abs_recipes_dir = os.path.abspath(RECIPES_DIR)
        if not abs_filepath.startswith(abs_recipes_dir + os.sep):
            raise ValueError("Path traversal detected")
        return filepath
    except (ValueError, OSError) as e:
        logger.warning(f"Invalid filename validation attempt: {filename}. Error: {e}")
        raise ValueError(f"Invalid filename: {e}")


@contextmanager
def locked_file(filepath: str, mode: str = "r") -> Generator:
    """
    Context manager for file locking to prevent race conditions.

    Uses fcntl for Unix systems. Acquires an exclusive lock for write operations,
    shared lock for read operations.

    Args:
        filepath: Path to the file to lock
        mode: File open mode ('r' for read, 'w' for write, etc.)

    Yields:
        file object: Opened and locked file
    """
    f = open(filepath, mode)
    try:
        # Exclusive lock for writing, shared lock for reading
        lock_type = fcntl.LOCK_EX if "w" in mode or "a" in mode else fcntl.LOCK_SH
        fcntl.flock(f.fileno(), lock_type)
        yield f
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def get_all_recipes() -> List[Dict[str, str]]:
    """
    Gets a list of all recipes, with in-memory caching to reduce disk I/O.

    Cache TTL is configurable via RECIPES_CACHE_TTL environment variable (default 60s).

    Returns:
        list: List of recipe dicts with 'name' and 'filename' keys, sorted by name
    """
    current_time = time.time()
    # Return cached data if still valid
    cache_age = current_time - _recipes_cache["timestamp"]  # type: ignore[operator]
    if _recipes_cache["data"] is not None and cache_age < _RECIPES_CACHE_TTL:
        return _recipes_cache["data"]  # type: ignore[return-value]

    # Cache miss or expired - read from disk
    recipes = []
    for filename in os.listdir(RECIPES_DIR):
        if filename.endswith(".json"):
            filepath = os.path.join(RECIPES_DIR, filename)
            try:
                # Use locked file reading to prevent reading during writes
                with locked_file(filepath, "r") as f:
                    data = json.load(f)
                    recipes.append(
                        {"name": data.get("name", "Unnamed Recipe"), "filename": filename}
                    )
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Could not read or parse {filename}: {e}")

    sorted_recipes = sorted(recipes, key=lambda r: r["name"])

    # Update cache
    _recipes_cache["data"] = sorted_recipes  # type: ignore[assignment]
    _recipes_cache["timestamp"] = current_time  # type: ignore[assignment]

    return sorted_recipes


def get_recipe(filename: str) -> Dict[str, Any]:
    """
    Load a single recipe from disk with file locking.

    Args:
        filename: Name of the recipe file (e.g., 'recipe.json')

    Returns:
        dict: Recipe data dictionary

    Raises:
        ValueError: If filename is invalid
        FileNotFoundError: If recipe doesn't exist
        json.JSONDecodeError: If recipe file is malformed
    """
    filepath = validate_recipe_filepath(filename)

    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Recipe {filename} not found")

    with locked_file(filepath, "r") as f:
        return json.load(f)  # type: ignore[no-any-return]


def save_recipe(filename: str, recipe_data: Dict[str, Any]) -> None:
    """
    Save a recipe to disk with file locking to prevent race conditions.

    Args:
        filename: Name of the recipe file (e.g., 'recipe.json')
        recipe_data: Recipe dictionary to save

    Raises:
        ValueError: If filename is invalid
        IOError: If file write fails
    """
    filepath = validate_recipe_filepath(filename)

    # Use locked file writing to prevent concurrent modifications
    with locked_file(filepath, "w") as f:
        json.dump(recipe_data, f, indent=2)

    # Invalidate cache after save
    invalidate_cache()


def invalidate_cache():
    """
    Invalidate the recipes cache, forcing a refresh on next request.

    Call this after any recipe create/update/delete operation.
    """
    _recipes_cache["data"] = None
    _recipes_cache["timestamp"] = 0


def migrate_recipe_data(data: Dict[str, Any], filename: str) -> Tuple[Dict[str, Any], bool]:
    """
    Migrates recipe data to the latest schema.

    Handles:
    - Nested 'properties' unwrapping
    - Adding default user_id
    - Adding default ai_metadata
    - Fixing "Untitled Recipe" names

    Args:
        data: Recipe dictionary to migrate
        filename: Filename for deriving recipe name if needed

    Returns:
        tuple: (migrated_data, changed_boolean)
            - migrated_data: Updated recipe dictionary
            - changed_boolean: True if any changes were made
    """
    changed = False

    # 1. Fix nested 'properties'
    if "properties" in data and "name" not in data:
        logger.info(f"Migrating nested JSON in {filename}")
        data = data["properties"]
        changed = True

    # 2. Add user_id
    if "user_id" not in data:
        data["user_id"] = "anonymous"
        changed = True

    # 3. Add ai_metadata
    if "ai_metadata" not in data:
        data["ai_metadata"] = {
            "model": "unknown",
            "timestamp": datetime.datetime.now().isoformat(),
            "prompt": "unknown",
            "images_working": True if data.get("stock_image_url") else False,
        }
        changed = True

    # 4. Fix "Untitled Recipe" if name is generic and filename is specific
    if data.get("name") == "Untitled Recipe":
        # Try to derive from filename
        derived_name = filename.replace("_", " ").replace(".json", "").title()
        data["name"] = derived_name
        changed = True

    return data, changed

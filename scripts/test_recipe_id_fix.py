#!/usr/bin/env python3
"""
Test script to verify the recipe ID consistency fix.

This script creates test recipes and verifies that:
1. Recipe with existing ID preserves that ID
2. Recipe without ID gets a generated UUID
3. The ID in data matches the database ID
4. Updates maintain ID consistency

Usage:
    python scripts/test_recipe_id_fix.py
"""

import logging
import sys
import uuid
from pathlib import Path

# Add Backend directory to path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from app import app
from extensions import db
from repositories import db_recipe_repository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_create_with_existing_id():
    """Test that a recipe with an existing ID preserves it."""
    logger.info("Test 1: Create recipe with existing ID")

    test_id = "test-recipe-123"
    recipe_data = {
        "id": test_id,
        "name": "Test Recipe With ID",
        "ingredients": [],
        "instructions": [],
        "description": "Test",
        "prepTime": 10,
        "cookTime": 20,
        "servings": 4,
    }

    with app.app_context():
        recipe = db_recipe_repository.create_recipe(recipe_data, user_id=None)

        if not recipe:
            logger.error("✗ Failed to create recipe")
            return False

        # Verify database ID matches the provided ID
        if recipe.id != test_id:
            logger.error(
                f"✗ Database ID {recipe.id} doesn't match provided ID {test_id}"
            )
            return False

        # Verify data.id matches database ID
        if recipe.data.get("id") != recipe.id:
            logger.error(
                f"✗ data.id {recipe.data.get('id')} doesn't match DB id {recipe.id}"
            )
            return False

        logger.info(f"✓ Recipe created with consistent ID: {recipe.id}")

        # Clean up
        db_recipe_repository.delete_recipe(recipe.id)
        return True


def test_create_without_id():
    """Test that a recipe without ID gets a generated UUID."""
    logger.info("Test 2: Create recipe without ID")

    recipe_data = {
        "name": "Test Recipe Without ID",
        "ingredients": [],
        "instructions": [],
        "description": "Test",
        "prepTime": 10,
        "cookTime": 20,
        "servings": 4,
    }

    with app.app_context():
        recipe = db_recipe_repository.create_recipe(recipe_data, user_id=None)

        if not recipe:
            logger.error("✗ Failed to create recipe")
            return False

        # Verify a UUID was generated
        try:
            uuid.UUID(recipe.id)
        except ValueError:
            logger.error(f"✗ Generated ID {recipe.id} is not a valid UUID")
            return False

        # Verify data.id matches database ID
        if recipe.data.get("id") != recipe.id:
            logger.error(
                f"✗ data.id {recipe.data.get('id')} doesn't match DB id {recipe.id}"
            )
            return False

        logger.info(f"✓ Recipe created with generated UUID: {recipe.id}")

        # Clean up
        db_recipe_repository.delete_recipe(recipe.id)
        return True


def test_update_maintains_id():
    """Test that updating a recipe maintains ID consistency."""
    logger.info("Test 3: Update recipe maintains ID consistency")

    test_id = "test-recipe-update"
    recipe_data = {
        "id": test_id,
        "name": "Original Name",
        "ingredients": [],
        "instructions": [],
        "description": "Test",
        "prepTime": 10,
        "cookTime": 20,
        "servings": 4,
    }

    with app.app_context():
        recipe = db_recipe_repository.create_recipe(recipe_data, user_id=None)

        if not recipe:
            logger.error("✗ Failed to create recipe")
            return False

        # Update the recipe with data that might have a different/missing ID
        update_data = {
            "name": "Updated Name",
            "ingredients": ["flour"],
            "instructions": ["mix"],
            "description": "Updated",
            "prepTime": 15,
            "cookTime": 25,
            "servings": 6,
        }

        updated = db_recipe_repository.update_recipe(
            recipe.id, update_data, user_id=None
        )

        if not updated:
            logger.error("✗ Failed to update recipe")
            return False

        # Verify ID is still consistent
        if updated.id != test_id:
            logger.error(f"✗ Database ID changed from {test_id} to {updated.id}")
            return False

        if updated.data.get("id") != updated.id:
            logger.error(
                f"✗ data.id {updated.data.get('id')} doesn't match DB id {updated.id}"
            )
            return False

        logger.info(f"✓ Recipe updated with consistent ID: {updated.id}")

        # Clean up
        db_recipe_repository.delete_recipe(recipe.id)
        return True


def run_tests():
    """Run all tests."""
    logger.info("=== Running Recipe ID Consistency Tests ===\n")

    tests = [
        test_create_with_existing_id,
        test_create_without_id,
        test_update_maintains_id,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f"✗ Test {test.__name__} raised exception: {e}")
            failed += 1
        logger.info("")

    logger.info("=== Test Results ===")
    logger.info(f"Passed: {passed}")
    logger.info(f"Failed: {failed}")

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)

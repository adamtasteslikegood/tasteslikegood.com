#!/usr/bin/env python3
"""
Migration script: Import file-based recipes into database.

This script reads all JSON files from the recipes/ directory and imports them
into the database. Recipes are imported as anonymous (user_id = NULL) by default.

Usage:
    python scripts/migrate_recipes_to_db.py [--user-id USER_ID]

Options:
    --user-id USER_ID    Assign all recipes to a specific user ID
    --dry-run            Show what would be migrated without making changes
"""

import argparse
import json
import os
import sys

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from config import RECIPES_DIR
from extensions import db
from repositories import db_recipe_repository


def migrate_recipes(user_id=None, dry_run=False):
    """
    Migrate all file-based recipes to the database.

    Args:
        user_id: Optional user ID to assign ownership
        dry_run: If True, don't actually write to database

    Returns:
        dict with migration statistics
    """
    stats = {"total_files": 0, "successful": 0, "skipped": 0, "failed": 0, "errors": []}

    if not os.path.exists(RECIPES_DIR):
        print(f"Error: Recipes directory not found: {RECIPES_DIR}")
        return stats

    print(f"Scanning recipes directory: {RECIPES_DIR}")
    print(f"User ID: {user_id or 'None (anonymous recipes)'}")
    print(f"Dry run: {dry_run}")
    print("-" * 60)

    for filename in os.listdir(RECIPES_DIR):
        if not filename.endswith(".json"):
            continue

        stats["total_files"] += 1
        filepath = os.path.join(RECIPES_DIR, filename)

        try:
            with open(filepath, "r") as f:
                recipe_data = json.load(f)

            recipe_name = recipe_data.get("name", "Unnamed Recipe")
            print(f"Processing: {filename} - {recipe_name}")

            if dry_run:
                print(f"  [DRY RUN] Would migrate recipe: {filename}")
                stats["successful"] += 1
                continue

            # Attempt migration
            recipe = db_recipe_repository.migrate_file_to_db(
                filename=filename, recipe_data=recipe_data, user_id=user_id
            )

            if recipe:
                print(f"  ✓ Migrated successfully (DB ID: {recipe.id})")
                stats["successful"] += 1
            else:
                print(f"  ⊘ Skipped (already exists or migration failed)")
                stats["skipped"] += 1

        except json.JSONDecodeError as e:
            error_msg = f"Invalid JSON in {filename}: {e}"
            print(f"  ✗ {error_msg}")
            stats["failed"] += 1
            stats["errors"].append(error_msg)

        except Exception as e:
            error_msg = f"Error processing {filename}: {e}"
            print(f"  ✗ {error_msg}")
            stats["failed"] += 1
            stats["errors"].append(error_msg)

    print("-" * 60)
    print("Migration complete!")
    print(f"Total files: {stats['total_files']}")
    print(f"Successful: {stats['successful']}")
    print(f"Skipped: {stats['skipped']}")
    print(f"Failed: {stats['failed']}")

    if stats["errors"]:
        print("\nErrors:")
        for error in stats["errors"]:
            print(f"  - {error}")

    return stats


def main():
    parser = argparse.ArgumentParser(description="Migrate file-based recipes to database")
    parser.add_argument("--user-id", type=int, help="Assign all recipes to this user ID")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without making changes",
    )

    args = parser.parse_args()

    # Create Flask app context
    app = create_app()

    with app.app_context():
        # Verify database is accessible
        try:
            db.session.execute("SELECT 1")
            print("✓ Database connection successful")
        except Exception as e:
            print(f"✗ Database connection failed: {e}")
            print("\nMake sure you have:")
            print("  1. Set DATABASE_URL in .env")
            print("  2. Run 'flask db upgrade' to create tables")
            sys.exit(1)

        # Run migration
        stats = migrate_recipes(user_id=args.user_id, dry_run=args.dry_run)

        # Exit with error code if any failures
        if stats["failed"] > 0:
            sys.exit(1)


if __name__ == "__main__":
    main()

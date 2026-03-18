#!/usr/bin/env python3
"""
Debug script to show raw database content for recipes.

Shows exactly what's stored in the database without any processing.
"""

import json
import sys
from pathlib import Path

backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

from app import app
from extensions import db
from models import Recipe


def show_raw_recipes():
    """Show raw recipe data from the database."""
    with app.app_context():
        recipes = Recipe.query.all()

        print(f"\n{'=' * 80}")
        print(f"RAW DATABASE CONTENT - {len(recipes)} recipe(s)")
        print(f"{'=' * 80}\n")

        for i, recipe in enumerate(recipes, 1):
            print(f"Recipe {i}:")
            print(f"  Database ID (recipe.id): {recipe.id}")
            print(f"  Name (recipe.name):      {recipe.name}")
            print(f"  Data type:               {type(recipe.data)}")
            print(f"  Data content:")
            print(f"    {json.dumps(recipe.data, indent=4)}")
            print(f"\n{'-' * 80}\n")


if __name__ == "__main__":
    show_raw_recipes()

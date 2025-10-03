import copy
import sys
from pathlib import Path

import pytest
from jsonschema import ValidationError

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import validate_recipe_data


@pytest.fixture()
def base_recipe():
    return {
        "name": "Test Recipe",
        "description": "A simple recipe for testing.",
        "prepTime": 10,
        "cookTime": 5,
        "servings": 2,
        "ingredients": {
            "wet": [
                {"name": "Water", "amount": 1, "units": "cup"}
            ],
            "dry": [
                {"name": "Flour", "amount": 2, "units": "cups"}
            ]
        },
        "instructions": ["Mix ingredients."]
    }


def test_validate_accepts_string_instructions(base_recipe):
    validate_recipe_data(copy.deepcopy(base_recipe))


def test_validate_accepts_object_instructions(base_recipe):
    recipe = copy.deepcopy(base_recipe)
    recipe["instructions"] = [
        {"step": 1, "description": "Combine all dry ingredients."},
        {"step": 2, "description": "Bake until golden."}
    ]

    validate_recipe_data(recipe)


def test_validate_rejects_missing_required_field(base_recipe):
    recipe = copy.deepcopy(base_recipe)
    recipe.pop("name")

    with pytest.raises(ValidationError):
        validate_recipe_data(recipe)

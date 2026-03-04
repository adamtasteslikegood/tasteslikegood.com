import copy
import sys
from pathlib import Path
import pytest
from jsonschema import ValidationError

sys.path.append(str(Path(__file__).resolve().parent.parent))

from validators import validate_recipe_data


@pytest.fixture
def base_recipe():
    return {
        "name": "Test Recipe",
        "description": "Testing instruction parsing",
        "prepTime": 5,
        "cookTime": 5,
        "servings": 1,
        "ingredients": {"wet": [], "dry": []},
        "instructions": [],
    }


def test_instructions_as_strings(base_recipe):
    """Test that a simple list of strings is valid."""
    recipe = copy.deepcopy(base_recipe)
    recipe["instructions"] = ["Preheat oven.", "Mix ingredients.", "Bake."]
    # Should not raise
    validate_recipe_data(recipe)


def test_instructions_as_objects(base_recipe):
    """Test that a list of instruction objects is valid."""
    recipe = copy.deepcopy(base_recipe)
    recipe["instructions"] = [
        {"step": 1, "description": "Preheat oven."},
        {"step": 2, "description": "Mix ingredients."},
        {"step": 3, "description": "Bake."},
    ]
    # Should not raise
    validate_recipe_data(recipe)


def test_mixed_instructions(base_recipe):
    """Test that mixing strings and objects is valid (per schema)."""
    recipe = copy.deepcopy(base_recipe)
    recipe["instructions"] = [
        "Preheat oven.",
        {"step": 2, "description": "Mix ingredients."},
        "Bake.",
    ]
    # Should not raise
    validate_recipe_data(recipe)


def test_invalid_instruction_object(base_recipe):
    """Test that an object missing 'description' is invalid."""
    recipe = copy.deepcopy(base_recipe)
    recipe["instructions"] = [{"step": 1}]  # Missing description
    with pytest.raises(ValidationError):
        validate_recipe_data(recipe)


def test_invalid_instruction_type(base_recipe):
    """Test that an invalid type (e.g., number) is invalid."""
    recipe = copy.deepcopy(base_recipe)
    recipe["instructions"] = [12345]
    with pytest.raises(ValidationError):
        validate_recipe_data(recipe)

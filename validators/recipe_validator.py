"""
Recipe validation module.

Provides JSON Schema validation for recipe data to ensure
all recipes conform to the expected structure before saving.
"""

import json
from jsonschema import Draft7Validator, ValidationError
from config import RECIPE_SCHEMA_PATH, RECIPE_VALIDATOR


def load_schema():
    """
    Load the recipe validation schema from recipe_schema.json.

    Returns:
        dict: Recipe schema dictionary, or None if loading fails
    """
    try:
        with open(RECIPE_SCHEMA_PATH, "r") as schema_file:
            return json.load(schema_file)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Warning: Unable to load recipe schema. Error: {exc}")
        return None


def create_validator():
    """
    Create a JSON Schema validator for recipe data.

    Returns:
        Draft7Validator: Validator instance, or None if schema loading failed
    """
    schema = load_schema()
    return Draft7Validator(schema) if schema else None


def validate_recipe_data(recipe_data):
    """
    Validate recipe data against the JSON schema.

    Args:
        recipe_data (dict): Recipe data to validate

    Returns:
        bool: True if validation succeeds

    Raises:
        RuntimeError: If recipe schema is not available
        ValidationError: If recipe data doesn't conform to schema
    """
    # Use the validator from config, or create a new one if needed
    validator = RECIPE_VALIDATOR or create_validator()

    if validator is None:
        raise RuntimeError("Recipe schema is not available for validation.")

    # Get the first error without sorting all errors
    first_error = next(validator.iter_errors(recipe_data), None)
    if first_error:
        location = " -> ".join(str(part) for part in first_error.absolute_path)
        message = first_error.message
        if location:
            message = f"{message} (at {location})"
        raise ValidationError(message)

    return True

"""
Configuration module for Tastes Like Good application.

Handles loading of:
- Application configuration from config.json
- Recipe validation schema
- Environment variables (API keys)
- Directory paths and caching settings
"""

import os
import json
from dotenv import load_dotenv
from typing import Dict, Any, Optional
from jsonschema import Draft7Validator
import logging

from utils.valkey_config import resolve_valkey_config

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Configuration file path
CONFIG_PATH = "config.json"

# Directory and file paths
RECIPES_DIR = "recipes"
RECIPE_SCHEMA_PATH = "recipe_schema.json"

# Ensure the recipes directory exists
os.makedirs(RECIPES_DIR, exist_ok=True)

# API Keys
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME", "")
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "")
GENAI_HTTP_TIMEOUT_MS = int(os.getenv("GENAI_HTTP_TIMEOUT_MS", "540000"))
WORKER_CLAIM_STALE_SECONDS = max(
    1,
    int(os.getenv("WORKER_CLAIM_STALE_SECONDS", "600")),
)

# Valkey/Redis response cache — every VALKEY_*/REDIS_URL env read lives in
# utils/valkey_config (KAN-160); see its docstring for what each var means.
# Resolved once at import time (same semantics as every other setting here);
# these module-level names are the ones tests monkeypatch.
_VALKEY = resolve_valkey_config()
VALKEY_HOST = _VALKEY.host
VALKEY_PORT = _VALKEY.port
VALKEY_AUTH_MODE = _VALKEY.auth_mode
REDIS_URL = _VALKEY.redis_url

# Default Model Configuration
DEFAULT_MODEL = os.getenv("GEMINI_DEFAULT_MODEL") or "gemini-3.1-pro-preview"
IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image")

# Cache settings
_RECIPES_CACHE_TTL = int(os.getenv("RECIPES_CACHE_TTL", "60"))

# Database Configuration
# Fallback to a local SQLite database if no connection string is provided
SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///tasteslikegood.db")
# Fix Heroku/GCP URL format if needed (postgres:// to postgresql://)
if SQLALCHEMY_DATABASE_URI.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace("postgres://", "postgresql://", 1)

SQLALCHEMY_TRACK_MODIFICATIONS = False

# Simple cache for recipe list to avoid reading all files on every request
_recipes_cache = {"data": None, "timestamp": 0}


def load_config() -> Dict[str, Any]:
    """
    Load application configuration from config.json.

    Returns:
        dict: Configuration dictionary, or empty dict if loading fails
    """
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)  # type: ignore[no-any-return]
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning(f"Could not load config.json: {e}")
        return {}


def load_recipe_schema() -> Optional[Dict[str, Any]]:
    """
    Load the recipe validation schema from recipe_schema.json.

    Returns:
        dict: Recipe schema dictionary, or None if loading fails
    """
    try:
        with open(RECIPE_SCHEMA_PATH, "r") as schema_file:
            return json.load(schema_file)  # type: ignore[no-any-return]
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning(f"Unable to load recipe schema. Error: {exc}")
        return None


def get_validator() -> Optional[Draft7Validator]:
    """
    Get a JSON Schema validator for recipe data.

    Returns:
        Draft7Validator: Validator instance, or None if schema loading failed
    """
    schema = load_recipe_schema()
    return Draft7Validator(schema) if schema else None


def get_api_key() -> Optional[str]:
    """
    Get the Google API key from environment variables.

    Returns:
        str: API key or None if not set
    """
    return GOOGLE_API_KEY


# Load configuration at module initialization
CONFIG = load_config()
RECIPE_SCHEMA = load_recipe_schema()
RECIPE_VALIDATOR = get_validator()

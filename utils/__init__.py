"""
Utilities package for the Tastes Like Good application.

Contains:
- session_utils: User identification and session management
- normalization: Recipe data normalization functions (from original utils.py)
"""

# Session utilities
# Normalization utilities (from original utils.py)
from utils.normalization import (
    CANONICAL_UNITS,
    UNIT_MAPPINGS,
    normalize_recipe_data,
    normalize_unit,
    parse_amount,
)
from utils.session_utils import (
    get_or_create_session_id,
    get_user_display_name,
    get_user_id,
    get_user_metadata,
    is_authenticated,
    migrate_session_to_user,
)

__all__ = [
    # Session utilities
    "get_or_create_session_id",
    "get_user_id",
    "get_user_display_name",
    "is_authenticated",
    "get_user_metadata",
    "migrate_session_to_user",
    # Normalization utilities
    "normalize_unit",
    "normalize_recipe_data",
    "parse_amount",
    "UNIT_MAPPINGS",
    "CANONICAL_UNITS",
]

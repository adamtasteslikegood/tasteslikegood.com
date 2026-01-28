"""
Utilities package for the Tastes Like Good application.

Contains:
- session_utils: User identification and session management
- (original utils.py normalization functions are at the root level)
"""

from utils.session_utils import (
    get_or_create_session_id,
    get_user_display_name,
    get_user_id,
    get_user_metadata,
    is_authenticated,
    migrate_session_to_user,
)

__all__ = [
    "get_or_create_session_id",
    "get_user_id",
    "get_user_display_name",
    "is_authenticated",
    "get_user_metadata",
    "migrate_session_to_user",
]

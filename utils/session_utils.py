"""
Session management utilities for user identification and tracking.

Handles:
- Anonymous user session ID generation and persistence
- Authenticated user identification
- Session ID to authenticated user migration
- User metadata extraction for logging and attribution
"""

import uuid
from flask import session


def get_or_create_session_id():
    """
    Get or create a unique session identifier for anonymous users.

    This creates a persistent session ID that survives page refreshes
    until the user clears cookies or the session expires.

    Returns:
        str: Session UUID string (e.g., 'session_abc123...')
    """
    if "session_id" not in session:
        session["session_id"] = f"session_{uuid.uuid4().hex}"
    return session["session_id"]


def get_user_id():
    """
    Get the user identifier for the current session.

    Priority:
    1. Authenticated user email (from Google OAuth)
    2. Authenticated user ID (from Google OAuth)
    3. Anonymous session UUID

    Returns:
        str: User identifier (email, google_id, or session_uuid)
    """
    # Check if user is authenticated via Google OAuth
    if "user_info" in session:
        user_info = session["user_info"]
        # Prefer email as the primary identifier
        if "email" in user_info:
            return user_info["email"]
        # Fallback to Google user ID
        if "id" in user_info:
            return f"google_id_{user_info['id']}"

    # Anonymous user - return session ID
    return get_or_create_session_id()


def get_user_display_name():
    """
    Get a human-readable display name for the current user.

    Returns:
        str: Display name (user's name, email, or 'Anonymous')
    """
    if "user_info" in session:
        user_info = session["user_info"]
        # Try name first, then email, then ID
        if "name" in user_info:
            return user_info["name"]
        if "email" in user_info:
            return user_info["email"]
        if "id" in user_info:
            return f"User {user_info['id']}"

    return "Anonymous"


def is_authenticated():
    """
    Check if the current user is authenticated.

    Returns:
        bool: True if authenticated via Google OAuth, False otherwise
    """
    return "user_id" in session and "user_info" in session


def get_user_metadata():
    """
    Get comprehensive user metadata for logging and attribution.

    Returns:
        dict: User metadata including:
            - user_id: Unique identifier
            - display_name: Human-readable name
            - is_authenticated: Boolean authentication status
            - email: Email address (if authenticated)
            - session_id: Session UUID (always present)
    """
    session_id = get_or_create_session_id()
    user_id = get_user_id()
    display_name = get_user_display_name()
    authenticated = is_authenticated()

    metadata = {
        "user_id": user_id,
        "display_name": display_name,
        "is_authenticated": authenticated,
        "session_id": session_id,
    }

    # Add email if authenticated
    if authenticated and "user_info" in session:
        user_info = session["user_info"]
        if "email" in user_info:
            metadata["email"] = user_info["email"]

    return metadata


def migrate_session_to_user(old_session_id, new_user_id):
    """
    Migrate content from anonymous session to authenticated user.

    This function should be called after successful authentication to
    transfer ownership of any content created during the anonymous session.

    Args:
        old_session_id: The anonymous session UUID
        new_user_id: The authenticated user's identifier

    Returns:
        dict: Migration result with 'migrated_count' and 'errors'

    Note:
        Implementation would query the recipe repository and update
        user_id fields. This is a placeholder for future enhancement.
    """
    # TODO: Implement recipe ownership migration
    # This would involve:
    # 1. Find all recipes with user_id == old_session_id
    # 2. Update their user_id to new_user_id
    # 3. Update ai_metadata to reflect the migration
    # 4. Log the migration for audit purposes

    return {
        "migrated_count": 0,
        "errors": [],
        "message": "Migration not yet implemented - recipes remain under session ID",
        "message": "Migration not yet implemented - recipes remain under session ID",
    }

# User Attribution and Session Tracking

## Overview

This document describes the user identification and attribution system implemented for the Tastes Like Good application. The system provides accountability for AI-generated content through comprehensive user tracking for both authenticated and anonymous users.

## Key Features

### 1. **Dual User Identification**
- **Authenticated Users**: Identified by Google email address or Google ID
- **Anonymous Users**: Tracked via persistent session UUIDs

### 2. **Session-Based Tracking**
- Every user (authenticated or not) receives a unique session ID
- Session IDs persist across page refreshes until cookies are cleared
- Format: `session_<32_hex_characters>` (e.g., `session_b2026ebc74514138bafd3ee749df9424`)

### 3. **Comprehensive Metadata**
All AI-generated content (recipes, images) includes:
- `user_id`: Primary identifier (email or session ID)
- `user_display_name`: Human-readable name or "Anonymous"
- `is_authenticated`: Boolean authentication status
- `session_id`: Session UUID (always present)
- `email`: Email address (authenticated users only)

## Implementation Details

### Core Module: `utils/session_utils.py`

#### Key Functions

**`get_or_create_session_id()`**
- Creates or retrieves session UUID
- Stores in Flask session for persistence
- Called automatically via middleware

**`get_user_id()`**
- Returns authenticated user's email (preferred)
- Falls back to Google ID if no email
- Returns session UUID for anonymous users

**`get_user_metadata()`**
- Returns comprehensive user information dict
- Used throughout the application for attribution

**`is_authenticated()`**
- Checks if user has valid OAuth credentials

**`migrate_session_to_user()`**
- Placeholder for future feature
- Will transfer anonymous content to authenticated user after login

### Middleware: `app.py`

```python
@app.before_request
def ensure_session_id():
    """Ensure all requests have a session ID for anonymous user tracking."""
    if request.endpoint and "static" in request.endpoint:
        return None  # Skip for static files
    get_or_create_session_id()
    return None
```

This runs on every request (except static files) to guarantee session tracking.

### Authentication: `auth.py`

After successful Google OAuth login:
1. User info is stored in session
2. `user_id` is set to email or Google ID
3. Session ID coexists with authenticated user ID

### Recipe Generation: `blueprints/generation_bp.py`

When a recipe is generated:

```python
user_metadata = get_user_metadata()
recipe_data["ai_metadata"]["recipe_generation"] = {
    "model": selected_model,
    "user_id": user_metadata["user_id"],
    "user_display_name": user_metadata["display_name"],
    "is_authenticated": user_metadata["is_authenticated"],
    "session_id": user_metadata["session_id"],
    "prompt": user_prompt,
    "timestamp": generation_timestamp,
    "success": True,
}
```

### Image Generation: `services/image_service.py`

AI image generation includes user metadata:

```python
recipe_data["ai_metadata"]["image_generation"] = {
    "model": model,
    "user_id": user_metadata["user_id"],
    "user_display_name": user_metadata["display_name"],
    "is_authenticated": user_metadata["is_authenticated"],
    "session_id": user_metadata["session_id"],
    "prompt": prompt,
    "timestamp": timestamp,
    "success": True,
    "image_path": image_path,
}
```

### Stock Images: `services/stock_image_service.py`

Unsplash stock image fetching tracks user:

```python
metadata = {
    "source": "unsplash",
    "user_id": user_metadata["user_id"],
    "user_display_name": user_metadata["display_name"],
    "is_authenticated": user_metadata["is_authenticated"],
    "session_id": user_metadata["session_id"],
    "search_query": query,
    "timestamp": timestamp,
    # ...
}
```

## Example Recipe JSON Structure

### Anonymous User Recipe

```json
{
  "name": "Vegan Chocolate Cake",
  "user_id": "session_b2026ebc74514138bafd3ee749df9424",
  "ai_metadata": {
    "recipe_generation": {
      "model": "models/gemini-2.5-flash",
      "user_id": "session_b2026ebc74514138bafd3ee749df9424",
      "user_display_name": "Anonymous",
      "is_authenticated": false,
      "session_id": "session_b2026ebc74514138bafd3ee749df9424",
      "prompt": "chocolate cake",
      "timestamp": "2026-01-28T01:30:00.000000",
      "success": true
    },
    "image_generation": {
      "model": "imagen-4.0-generate-001",
      "user_id": "session_b2026ebc74514138bafd3ee749df9424",
      "user_display_name": "Anonymous",
      "is_authenticated": false,
      "session_id": "session_b2026ebc74514138bafd3ee749df9424",
      "prompt": "A delicious chocolate cake...",
      "timestamp": "2026-01-28T01:30:15.000000",
      "success": true,
      "image_path": "static/images/ai_vegan_chocolate_cake.png"
    },
    "stock_image_generation": {
      "source": "unsplash",
      "user_id": "session_b2026ebc74514138bafd3ee749df9424",
      "user_display_name": "Anonymous",
      "is_authenticated": false,
      "session_id": "session_b2026ebc74514138bafd3ee749df9424",
      "search_query": "vegan chocolate cake",
      "timestamp": "2026-01-28T01:30:10.000000",
      "success": true
    }
  }
}
```

### Authenticated User Recipe

```json
{
  "name": "Vegan Buddha Bowl",
  "user_id": "user@example.com",
  "ai_metadata": {
    "recipe_generation": {
      "model": "models/gemini-pro-latest",
      "user_id": "user@example.com",
      "user_display_name": "John Doe",
      "is_authenticated": true,
      "session_id": "session_a1b2c3d4e5f6...",
      "prompt": "healthy buddha bowl",
      "timestamp": "2026-01-28T02:00:00.000000",
      "success": true
    }
  }
}
```

## Abuse Prevention

The system provides accountability through:

1. **Session Tracking**: All anonymous users have a unique, persistent ID
2. **IP Correlation**: Session IDs can be correlated with IP addresses in server logs
3. **Pattern Detection**: Repeated abuse from same session ID is traceable
4. **Rate Limiting**: Can implement per-session or per-user rate limits
5. **Authentication Incentive**: Authenticated users have better experience/features

## Future Enhancements

### Session Migration (Planned)
When an anonymous user logs in:
1. Find all recipes with `user_id == old_session_id`
2. Update to authenticated `user_id`
3. Preserve `session_id` for audit trail
4. Log the migration event

### Additional Features
- Admin dashboard to view user activity
- Flagging/reporting system for abuse
- Rate limiting based on session ID or user ID
- User profile page showing their generated content
- Content moderation queue

## Testing

Run the manual test suite:

```bash
python3 test_session_manual.py
```

This validates:
- Session ID generation for anonymous users
- User identification for authenticated users
- Session persistence across requests
- Comprehensive metadata extraction

## Security Considerations

1. **Session Security**: Flask secret key must be persistent in production
2. **Cookie Security**: Use secure, httponly cookies in production
3. **PII Protection**: Email addresses are stored only for authenticated users
4. **Audit Trail**: All actions preserve session_id even for authenticated users
5. **No Passwords**: Using OAuth, no password storage required

## Migration Guide

### For Existing Recipes

Old recipes without proper user attribution can be migrated:

```python
from repositories.recipe_repository import migrate_recipe_data

# This function adds:
# - user_id: 'anonymous' (default)
# - ai_metadata structure
# - Fixes nested JSON issues
```

Run migration on recipe load (already implemented in `recipes_bp.py`).

## Best Practices

1. **Always use `get_user_metadata()`** - Don't access session directly
2. **Pass metadata to services** - Services should receive user_metadata dict
3. **Log all actions** - Include user_id and session_id in all logs
4. **Validate on read** - Check recipes have proper user attribution
5. **Monitor patterns** - Watch for abuse from specific session IDs

## Files Modified

- `utils/session_utils.py` - NEW: Core session management
- `utils/__init__.py` - NEW: Package initialization
- `app.py` - Added middleware for session tracking
- `auth.py` - Sets user_id after Google OAuth login
- `blueprints/generation_bp.py` - Uses user metadata in recipe generation
- `blueprints/recipes_bp.py` - Uses user metadata for image fetching
- `services/image_service.py` - Tracks user in AI image generation
- `services/stock_image_service.py` - Tracks user in stock image fetching
- `templates/404.html` - NEW: Error page
- `templates/500.html` - NEW: Error page
- `templates/base.html` - Added favicon

## Summary

This implementation provides comprehensive user tracking for accountability while maintaining privacy and following best practices:

✅ Anonymous users tracked via persistent session UUIDs  
✅ Authenticated users identified by email/Google ID  
✅ All AI content attributed to user/session  
✅ Session IDs persist for abuse tracking  
✅ Ready for future rate limiting and moderation  
✅ Privacy-respecting (minimal PII storage)  
✅ Production-ready with proper security considerations

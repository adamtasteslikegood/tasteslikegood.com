#!/usr/bin/env python3
"""
Manual test script for session utilities.
Run this to verify user identification is working correctly.
"""

import sys

from flask import Flask, session

# Import the session utilities
from utils.session_utils import (
    get_or_create_session_id,
    get_user_display_name,
    get_user_id,
    get_user_metadata,
    is_authenticated,
)


def test_anonymous_user():
    """Test anonymous user session ID generation."""
    print("\n=== Testing Anonymous User ===")

    app = Flask(__name__)
    app.secret_key = "test_secret"

    with app.test_request_context():
        # Get session ID
        session_id = get_or_create_session_id()
        print(f"✓ Session ID created: {session_id}")
        assert session_id.startswith("session_"), (
            "Session ID should start with 'session_'"
        )

        # Get user ID (should be same as session ID for anonymous users)
        user_id = get_user_id()
        print(f"✓ User ID: {user_id}")
        assert user_id == session_id, (
            "User ID should equal session ID for anonymous users"
        )

        # Get display name
        display_name = get_user_display_name()
        print(f"✓ Display name: {display_name}")
        assert display_name == "Anonymous", "Display name should be 'Anonymous'"

        # Check authentication status
        auth_status = is_authenticated()
        print(f"✓ Authenticated: {auth_status}")
        assert not auth_status, "User should not be authenticated"

        # Get comprehensive metadata
        metadata = get_user_metadata()
        print(f"✓ Metadata: {metadata}")
        assert metadata["user_id"] == session_id
        assert metadata["display_name"] == "Anonymous"
        assert not metadata["is_authenticated"]
        assert "session_id" in metadata
        assert "email" not in metadata

        print("✅ All anonymous user tests passed!")


def test_authenticated_user():
    """Test authenticated user identification."""
    print("\n=== Testing Authenticated User ===")

    app = Flask(__name__)
    app.secret_key = "test_secret"

    with app.test_request_context():
        # Simulate Google OAuth login
        session["user_info"] = {
            "email": "testuser@example.com",
            "name": "Test User",
            "id": "123456789",
        }
        session["credentials"] = {"token": "fake_oauth_token"}

        # Get user ID (should be email)
        user_id = get_user_id()
        print(f"✓ User ID: {user_id}")
        assert user_id == "testuser@example.com", (
            "User ID should be email for authenticated users"
        )

        # Get display name
        display_name = get_user_display_name()
        print(f"✓ Display name: {display_name}")
        assert display_name == "Test User", "Display name should be user's name"

        # Check authentication status
        auth_status = is_authenticated()
        print(f"✓ Authenticated: {auth_status}")
        assert auth_status, "User should be authenticated"

        # Get comprehensive metadata
        metadata = get_user_metadata()
        print(f"✓ Metadata: {metadata}")
        assert metadata["user_id"] == "testuser@example.com"
        assert metadata["display_name"] == "Test User"
        assert metadata["is_authenticated"]
        assert metadata["email"] == "testuser@example.com"
        assert "session_id" in metadata

        print("✅ All authenticated user tests passed!")


def test_session_persistence():
    """Test that session IDs persist across calls."""
    print("\n=== Testing Session Persistence ===")

    app = Flask(__name__)
    app.secret_key = "test_secret"

    with app.test_request_context():
        # Create session ID multiple times
        ids = [get_or_create_session_id() for _ in range(5)]

        # All should be the same
        unique_ids = set(ids)
        print(f"✓ Generated {len(ids)} IDs, {len(unique_ids)} unique")
        assert len(unique_ids) == 1, "All session IDs should be the same"

        print("✅ Session persistence test passed!")


def main():
    """Run all tests."""
    print("=" * 60)
    print("SESSION UTILITIES MANUAL TEST SUITE")
    print("=" * 60)

    try:
        test_anonymous_user()
        test_authenticated_user()
        test_session_persistence()

        print("\n" + "=" * 60)
        print("✅ ALL TESTS PASSED!")
        print("=" * 60)
        return 0

    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        return 1

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())

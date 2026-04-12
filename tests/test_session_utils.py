"""
Tests for session management and user identification utilities.

Tests cover:
- Session ID generation for anonymous users
- User ID extraction for authenticated users
- User metadata extraction
- Session persistence across requests
"""

import unittest

from flask import Flask, session

from utils.session_utils import (
    get_or_create_session_id,
    get_user_display_name,
    get_user_id,
    get_user_metadata,
    is_authenticated,
)


class TestSessionUtils(unittest.TestCase):
    """Test cases for session utility functions."""

    def setUp(self):
        """Set up Flask app context for each test."""
        self.app = Flask(__name__)
        self.app.secret_key = "test_secret_key"
        self.ctx = self.app.test_request_context()
        self.ctx.push()

    def tearDown(self):
        """Clean up Flask app context after each test."""
        self.ctx.pop()

    def test_get_or_create_session_id_creates_new(self):
        """Test that a new session ID is created when none exists."""
        session_id = get_or_create_session_id()

        self.assertIsNotNone(session_id)
        self.assertTrue(session_id.startswith("session_"))
        self.assertEqual(len(session_id), 40)  # 'session_' + 32 hex chars

    def test_get_or_create_session_id_returns_existing(self):
        """Test that existing session ID is returned."""
        # Create first session ID
        first_id = get_or_create_session_id()

        # Call again should return same ID
        second_id = get_or_create_session_id()

        self.assertEqual(first_id, second_id)

    def test_get_user_id_anonymous(self):
        """Test user ID for anonymous users returns session ID."""
        user_id = get_user_id()

        self.assertTrue(user_id.startswith("session_"))

    def test_get_user_id_authenticated_with_email(self):
        """Test user ID for authenticated users with email."""
        session["user_info"] = {
            "email": "test@example.com",
            "id": "12345",
            "name": "Test User",
        }
        session["credentials"] = {"token": "fake_token"}

        user_id = get_user_id()

        self.assertEqual(user_id, "test@example.com")

    def test_get_user_id_authenticated_without_email(self):
        """Test user ID for authenticated users without email (fallback to Google ID)."""
        session["user_info"] = {"id": "12345", "name": "Test User"}
        session["credentials"] = {"token": "fake_token"}

        user_id = get_user_id()

        self.assertEqual(user_id, "google_id_12345")

    def test_get_user_display_name_anonymous(self):
        """Test display name for anonymous users."""
        display_name = get_user_display_name()

        self.assertEqual(display_name, "Anonymous")

    def test_get_user_display_name_authenticated(self):
        """Test display name for authenticated users."""
        session["user_info"] = {"name": "Test User", "email": "test@example.com"}

        display_name = get_user_display_name()

        self.assertEqual(display_name, "Test User")

    def test_is_authenticated_false(self):
        """Test authentication check for anonymous users."""
        self.assertFalse(is_authenticated())

    def test_is_authenticated_true(self):
        """Test authentication check for authenticated users."""
        session["user_info"] = {"email": "test@example.com"}
        session["credentials"] = {"token": "fake_token"}

        self.assertTrue(is_authenticated())

    def test_get_user_metadata_anonymous(self):
        """Test comprehensive metadata for anonymous users."""
        metadata = get_user_metadata()

        self.assertIn("user_id", metadata)
        self.assertIn("display_name", metadata)
        self.assertIn("is_authenticated", metadata)
        self.assertIn("session_id", metadata)

        self.assertTrue(metadata["user_id"].startswith("session_"))
        self.assertEqual(metadata["display_name"], "Anonymous")
        self.assertFalse(metadata["is_authenticated"])
        self.assertTrue(metadata["session_id"].startswith("session_"))
        self.assertNotIn("email", metadata)

    def test_get_user_metadata_authenticated(self):
        """Test comprehensive metadata for authenticated users."""
        session["user_info"] = {
            "email": "test@example.com",
            "name": "Test User",
            "id": "12345",
        }
        session["credentials"] = {"token": "fake_token"}

        metadata = get_user_metadata()

        self.assertEqual(metadata["user_id"], "test@example.com")
        self.assertEqual(metadata["display_name"], "Test User")
        self.assertTrue(metadata["is_authenticated"])
        self.assertEqual(metadata["email"], "test@example.com")
        self.assertIn("session_id", metadata)

    def test_session_id_persistence(self):
        """Test that session ID persists across multiple calls."""
        # Simulate multiple requests in same session
        ids = [get_or_create_session_id() for _ in range(5)]

        # All IDs should be the same
        self.assertEqual(len(set(ids)), 1)


if __name__ == "__main__":
    unittest.main()

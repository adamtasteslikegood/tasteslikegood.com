"""Tests for the Gemini AI service."""

import os
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

# Ensure app can be imported
sys.path.append(str(Path(__file__).resolve().parent.parent))

from services.gemini_service import GENAI_HTTP_OPTIONS, attempt_generation, get_genai_client


class TestGetGenaiClient(unittest.TestCase):
    @patch.dict(os.environ, {"GOOGLE_CLIENT_SECRET": "test-secret"})
    @patch("services.gemini_service.Client")
    @patch("services.gemini_service.google.oauth2.credentials.Credentials")
    def test_get_client_with_valid_session_credentials(self, mock_creds, mock_client):
        """Should create a client using session OAuth credentials if valid."""
        # Setup mocks
        mock_creds_instance = MagicMock()
        mock_creds.return_value = mock_creds_instance
        mock_client_instance = MagicMock()
        mock_client.return_value = mock_client_instance

        # Act
        client = get_genai_client({"token": "fake-token"})

        # Assert — client_secret is now passed from GOOGLE_CLIENT_SECRET env var
        mock_creds.assert_called_once_with(token="fake-token", client_secret="test-secret")
        mock_client.assert_called_once_with(
            credentials=mock_creds_instance,
            http_options=GENAI_HTTP_OPTIONS,
        )
        self.assertEqual(client, mock_client_instance)

    @patch("services.gemini_service.Client")
    @patch("services.gemini_service.google.oauth2.credentials.Credentials")
    @patch("services.gemini_service.GOOGLE_API_KEY", "fake-api-key")
    def test_get_client_fallback_to_api_key(self, mock_creds, mock_client):
        """Should fall back to GOOGLE_API_KEY if session credentials fail."""
        mock_creds.side_effect = Exception("Invalid creds")
        mock_client_instance = MagicMock()
        mock_client.return_value = mock_client_instance

        client = get_genai_client({"bad": "creds"})

        # Verify fallback occurred
        mock_client.assert_called_with(
            api_key="fake-api-key",
            http_options=GENAI_HTTP_OPTIONS,
        )
        self.assertEqual(client, mock_client_instance)

    @patch("services.gemini_service.Client")
    @patch("services.gemini_service.GOOGLE_API_KEY", "fake-api-key")
    def test_get_client_api_key_only(self, mock_client):
        """Should use GOOGLE_API_KEY when no session credentials are provided."""
        mock_client_instance = MagicMock()
        mock_client.return_value = mock_client_instance

        client = get_genai_client(None)

        mock_client.assert_called_once_with(
            api_key="fake-api-key",
            http_options=GENAI_HTTP_OPTIONS,
        )
        self.assertEqual(client, mock_client_instance)

    @patch("services.gemini_service.GOOGLE_API_KEY", None)
    def test_get_client_no_auth(self):
        """Should return None if neither session creds nor API key exist."""
        client = get_genai_client(None)
        self.assertIsNone(client)


class TestAttemptGeneration(unittest.TestCase):
    def test_successful_generation(self):
        """Should return generated text when generation succeeds."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Generated recipe text"
        mock_client.models.generate_content.return_value = mock_response

        result = attempt_generation(mock_client, "models/gemini-2.0-flash", "Make a cake")

        self.assertEqual(result, "Generated recipe text")
        mock_client.models.generate_content.assert_called_once_with(
            model="models/gemini-2.0-flash", contents="Make a cake"
        )

    def test_generation_failure(self):
        """Should raise an exception if the API call fails."""
        mock_client = MagicMock()
        mock_client.models.generate_content.side_effect = Exception("API offline")

        with self.assertRaises(Exception) as context:
            attempt_generation(mock_client, "models/gemini-2.0-flash", "Make a cake")

        self.assertIn("API offline", str(context.exception))


if __name__ == "__main__":
    unittest.main()

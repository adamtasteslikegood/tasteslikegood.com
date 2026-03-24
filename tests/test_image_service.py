"""Tests for the AI image generation service."""

import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch, mock_open

# Ensure app can be imported
sys.path.append(str(Path(__file__).resolve().parent.parent))

from services.image_service import (
    generate_ai_image,
    save_image_file,
    update_recipe_with_image,
)

MOCK_USER_METADATA = {
    "user_id": "test_user_123",
    "display_name": "Test User",
    "is_authenticated": True,
    "session_id": "session_abc123",
}


class TestImageService(unittest.TestCase):
    def test_generate_returns_existing_image(self):
        """Should return the existing image URL if force_regenerate is False."""
        recipe_data = {"name": "Test Recipe", "ai_image_url": "/static/images/existing.png"}
        
        url, err = generate_ai_image("dummy_path", recipe_data, "test.json", force_regenerate=False)
        
        self.assertEqual(url, "/static/images/existing.png")
        self.assertIsNone(err)

    @patch("services.image_service.session")
    @patch("services.image_service.get_genai_client")
    def test_generate_fails_without_client(self, mock_get_client, mock_session):
        """Should return an error if no Gemini client can be created (no auth)."""
        mock_get_client.return_value = None
        
        url, err = generate_ai_image("dummy_path", {"name": "Test Recipe"}, "test.json")
        
        self.assertIsNone(url)
        self.assertEqual(err["status"], 500)
        self.assertIn("credentials", err["error"].lower())

    @patch("services.image_service.session")
    @patch("services.image_service.get_genai_client")
    @patch("services.image_service.get_user_metadata", return_value=MOCK_USER_METADATA)
    @patch("services.image_service.save_image_file")
    @patch("services.image_service.update_recipe_with_image")
    @patch("builtins.open", new_callable=mock_open)
    def test_successful_generation(
        self, mock_file_open, mock_update, mock_save, mock_meta, mock_client, mock_session
    ):
        """Should complete the entire generation pipeline and save the JSON."""
        # Setup mock client and response
        mock_gen_client = MagicMock()
        mock_client.return_value = mock_gen_client
        mock_response = MagicMock()
        mock_response.generated_images = [MagicMock()]
        mock_gen_client.models.generate_images.return_value = mock_response
        
        mock_save.return_value = "/static/images/ai_test.png"
        recipe_data = {"name": "Test Recipe"}
        
        url, err = generate_ai_image("path/test.json", recipe_data, "test.json", force_regenerate=True)
        
        # Verify it saved and returned the url
        self.assertEqual(url, "/static/images/ai_test.png")
        self.assertIsNone(err)
        mock_save.assert_called_once()
        mock_update.assert_called_once()
        
        # Verify file write for the JSON was called
        mock_file_open().write.assert_called()

    @patch("services.image_service.session")
    @patch("services.image_service.get_genai_client")
    @patch("services.image_service.get_user_metadata", side_effect=Exception("Simulated API Crash"))
    @patch("builtins.open", new_callable=mock_open)
    def test_generation_exception_handling(
        self, mock_file_open, mock_meta, mock_client, mock_session
    ):
        """Should catch unexpected exceptions, log them, and return a clean 500 error."""
        mock_client.return_value = MagicMock()
        
        url, err = generate_ai_image("path/test.json", {"name": "Test"}, "test.json")
        
        self.assertIsNone(url)
        self.assertEqual(err["status"], 500)
        self.assertIn("Simulated API Crash", err["error"])
        # Should have appended to the recipe_error.txt log file
        mock_file_open.assert_called_with("recipe_error.txt", "a")


class TestImageServiceHelpers(unittest.TestCase):
    def test_update_recipe_with_image(self):
        """Should mutate the recipe dictionary with image generation metadata."""
        recipe_data = {"name": "Test Recipe"}
        
        update_recipe_with_image(
            recipe_data, "/url/img.png", "model-x", MOCK_USER_METADATA, "Prompt text", "2026-01-01", "test.json"
        )
        
        self.assertEqual(recipe_data["ai_image_url"], "/url/img.png")
        self.assertTrue(recipe_data["ai_metadata"]["images_working"])
        self.assertEqual(recipe_data["ai_metadata"]["image_generation"]["user_id"], "test_user_123")
        self.assertEqual(recipe_data["ai_metadata"]["image_generation"]["image_path"], "static/images/ai_test.png")


if __name__ == "__main__":
    unittest.main()

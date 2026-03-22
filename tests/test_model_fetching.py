import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch, mock_open
import json

# Ensure app can be imported
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import app
from services.model_service import filter_and_sort_models


class TestModelFetching(unittest.TestCase):

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    def test_get_models_success_from_cache(self):
        """Test that /api/models returns models from cache file."""
        mock_cache_data = {
            "models": [
                {
                    "name": "models/gemini-2.5-flash",
                    "display_name": "Gemini 2.5 Flash",
                    "supported_generation_methods": ["generateContent"],
                },
                {
                    "name": "models/gemini-2.5-pro",
                    "display_name": "Gemini 2.5 Pro",
                    "supported_generation_methods": ["generateContent"],
                },
                {
                    "name": "models/gemini-2.0-flash",
                    "display_name": "Gemini 2.0 Flash",
                    "supported_generation_methods": ["generateContent"],
                },
            ],
            "updated_at": "2025-01-01T00:00:00Z",
        }

        with patch("builtins.open", mock_open(read_data=json.dumps(mock_cache_data))):
            response = self.app.get("/api/models")

            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)

            # Should return filtered/sorted models
            self.assertGreater(len(data), 0)
            self.assertLessEqual(len(data), 10)  # Max 10 models returned

    def test_get_models_filter_unsupported(self):
        """Test that embedding and other non-generation models are filtered out."""
        mock_cache_data = {
            "models": [
                {
                    "name": "models/gemini-2.5-flash",
                    "display_name": "Gemini 2.5 Flash",
                    "supported_generation_methods": ["generateContent"],
                },
                {
                    "name": "models/embedding-001",
                    "display_name": "Embedding Model",
                    "supported_generation_methods": ["embedContent"],
                },
                {
                    "name": "models/imagen-3",
                    "display_name": "Imagen 3",
                    "supported_generation_methods": ["generateImage"],
                },
                {
                    "name": "models/text-embedding",
                    "display_name": "Text Embedding",
                    "supported_generation_methods": ["embedText"],
                },
            ],
            "updated_at": "2025-01-01T00:00:00Z",
        }

        with patch("builtins.open", mock_open(read_data=json.dumps(mock_cache_data))):
            response = self.app.get("/api/models")

            data = json.loads(response.data)
            # Only gemini models should be included, not embedding or imagen
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["id"], "models/gemini-2.5-flash")

    def test_get_models_no_cache(self):
        """Test that /api/models returns empty list when no cache exists."""
        with patch("builtins.open", side_effect=FileNotFoundError()):
            response = self.app.get("/api/models")

            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            self.assertEqual(data, [])

    def test_get_models_corrupt_cache(self):
        """Test that /api/models handles corrupt cache gracefully."""
        with patch("builtins.open", mock_open(read_data="not valid json")):
            response = self.app.get("/api/models")

            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            self.assertEqual(data, [])


class TestFilterAndSortModels(unittest.TestCase):
    """Test the filter_and_sort_models helper function."""

    def test_filters_embedding_models(self):
        models = [
            {
                "name": "models/gemini-2.5-flash",
                "display_name": "Gemini 2.5 Flash",
                "supported_generation_methods": ["generateContent"],
            },
            {
                "name": "models/embedding-001",
                "display_name": "Embedding",
                "supported_generation_methods": ["embedContent"],
            },
        ]
        result = filter_and_sort_models(models)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "models/gemini-2.5-flash")

    def test_filters_imagen_models(self):
        models = [
            {
                "name": "models/gemini-2.5-flash",
                "display_name": "Gemini 2.5 Flash",
                "supported_generation_methods": ["generateContent"],
            },
            {
                "name": "models/imagen-4",
                "display_name": "Imagen 4",
                "supported_generation_methods": ["generateImage"],
            },
        ]
        result = filter_and_sort_models(models)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "models/gemini-2.5-flash")

    def test_sorts_preferred_models_first(self):
        models = [
            {
                "name": "models/gemini-unknown",
                "display_name": "Gemini Unknown",
                "supported_generation_methods": ["generateContent"],
            },
            {
                "name": "models/gemini-2.5-pro",
                "display_name": "Gemini 2.5 Pro",
                "supported_generation_methods": ["generateContent"],
            },
            {
                "name": "models/gemini-2.5-flash",
                "display_name": "Gemini 2.5 Flash",
                "supported_generation_methods": ["generateContent"],
            },
        ]
        result = filter_and_sort_models(models)
        # Preferred models should come first in defined order
        self.assertEqual(result[0]["id"], "models/gemini-2.5-pro")
        self.assertEqual(result[1]["id"], "models/gemini-2.5-flash")

    def test_limits_to_8_models(self):
        models = [
            {
                "name": f"models/gemini-{i}",
                "display_name": f"Gemini {i}",
                "supported_generation_methods": ["generateContent"],
            }
            for i in range(15)
        ]
        result = filter_and_sort_models(models)
        self.assertEqual(len(result), 10)


class TestRefreshModels(unittest.TestCase):
    """Test the /api/models/refresh endpoint."""

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    @patch("services.model_service.Client")
    def test_refresh_models_success(self, MockClient):
        """Test that refresh fetches models from API and updates cache."""
        mock_client_instance = MockClient.return_value

        mock_model = MagicMock()
        mock_model.name = "models/gemini-2.5-flash"
        mock_model.display_name = "Gemini 2.5 Flash"
        mock_model.supported_generation_methods = ["generateContent"]

        mock_client_instance.models.list.return_value = [mock_model]

        with patch("services.model_service.GOOGLE_API_KEY", "fake-key"):
            with patch("builtins.open", mock_open()):
                response = self.app.post("/api/models/refresh")

                self.assertEqual(response.status_code, 200)
                data = json.loads(response.data)
                self.assertIn("models", data)
                self.assertIn("message", data)

    def test_refresh_models_no_auth(self):
        """Test that refresh fails without API key or credentials."""
        with patch("services.model_service.GOOGLE_API_KEY", None):
            response = self.app.post("/api/models/refresh")

            self.assertEqual(response.status_code, 401)
            data = json.loads(response.data)
            self.assertIn("error", data)


if __name__ == "__main__":
    unittest.main()

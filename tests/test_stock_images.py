"""Tests for stock image URL generation functionality."""
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

# Ensure app can be imported
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import (
    get_smart_stock_image,
    validate_image_url,
    validate_and_refresh_stock_image,
    _get_fallback_image,
    search_unsplash,
    FALLBACK_FOOD_IMAGES,
)


class TestFallbackImages(unittest.TestCase):
    """Test the curated fallback image system."""

    def test_fallback_images_list_exists(self):
        """Verify fallback images list is populated."""
        self.assertGreater(len(FALLBACK_FOOD_IMAGES), 0)

    def test_fallback_images_are_unsplash_urls(self):
        """All fallback images should be valid Unsplash URLs."""
        for url in FALLBACK_FOOD_IMAGES:
            self.assertTrue(url.startswith("https://images.unsplash.com/"))

    def test_fallback_images_have_size_params(self):
        """Fallback images should include sizing parameters."""
        for url in FALLBACK_FOOD_IMAGES:
            self.assertIn("w=800", url)
            self.assertIn("h=600", url)

    def test_get_fallback_image_deterministic(self):
        """Same recipe name should always return the same fallback image."""
        recipe_name = "Chocolate Cake"
        result1 = _get_fallback_image(recipe_name)
        result2 = _get_fallback_image(recipe_name)
        self.assertEqual(result1, result2)

    def test_get_fallback_image_case_insensitive(self):
        """Fallback should be case-insensitive."""
        result1 = _get_fallback_image("Chocolate Cake")
        result2 = _get_fallback_image("chocolate cake")
        self.assertEqual(result1, result2)

    def test_get_fallback_image_different_recipes(self):
        """Different recipe names may return different images."""
        # This tests that we use the hash for variety
        recipes = ["Pasta", "Salad", "Soup", "Bread", "Cake"]
        results = [_get_fallback_image(r) for r in recipes]
        # At least some should be different (not all the same)
        unique_results = set(results)
        self.assertGreater(len(unique_results), 1)

    def test_get_fallback_image_returns_valid_url(self):
        """Fallback image should be from the curated list."""
        result = _get_fallback_image("Test Recipe")
        self.assertIn(result, FALLBACK_FOOD_IMAGES)


class TestValidateImageUrl(unittest.TestCase):
    """Test the URL validation function."""

    @patch('app.requests.head')
    def test_valid_image_url(self, mock_head):
        """Valid image URLs should return True."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {'Content-Type': 'image/jpeg'}
        mock_head.return_value = mock_response

        result = validate_image_url("https://example.com/image.jpg")
        self.assertTrue(result)

    @patch('app.requests.head')
    def test_invalid_status_code(self, mock_head):
        """Non-200 status codes should return False."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_head.return_value = mock_response

        result = validate_image_url("https://example.com/notfound.jpg")
        self.assertFalse(result)

    @patch('app.requests.head')
    def test_non_image_content_type(self, mock_head):
        """Non-image content types should return False."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {'Content-Type': 'text/html'}
        mock_head.return_value = mock_response

        result = validate_image_url("https://example.com/page.html")
        self.assertFalse(result)

    @patch('app.requests.head')
    def test_request_exception(self, mock_head):
        """Request exceptions should return False."""
        mock_head.side_effect = Exception("Connection error")

        result = validate_image_url("https://example.com/image.jpg")
        self.assertFalse(result)

    @patch('app.requests.head')
    def test_accepts_png_content_type(self, mock_head):
        """PNG content type should be valid."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {'Content-Type': 'image/png'}
        mock_head.return_value = mock_response

        result = validate_image_url("https://example.com/image.png")
        self.assertTrue(result)

    @patch('app.requests.head')
    def test_accepts_webp_content_type(self, mock_head):
        """WebP content type should be valid."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {'Content-Type': 'image/webp'}
        mock_head.return_value = mock_response

        result = validate_image_url("https://example.com/image.webp")
        self.assertTrue(result)


class TestGetSmartStockImage(unittest.TestCase):
    """Test the main stock image retrieval function (Unsplash-based)."""

    @patch('app.search_unsplash')
    def test_no_unsplash_key_uses_fallback(self, mock_search):
        """When Unsplash returns None (no key), use fallback image."""
        mock_search.return_value = None

        url, metadata = get_smart_stock_image("Test Recipe")

        self.assertIsNotNone(url)
        self.assertIn(url, FALLBACK_FOOD_IMAGES)
        self.assertTrue(metadata['success'])
        self.assertTrue(metadata['fallback_used'])

    @patch('app.search_unsplash')
    def test_valid_unsplash_response(self, mock_search):
        """When Unsplash returns valid URL, use it."""
        mock_search.return_value = {
            'url': "https://images.unsplash.com/photo-12345",
            'attribution': {
                'photographer_name': 'Test Photographer',
                'photographer_url': 'https://unsplash.com/@test',
                'unsplash_url': 'https://unsplash.com',
                'html': 'Photo by Test Photographer on Unsplash'
            }
        }

        url, metadata = get_smart_stock_image(
            "Chocolate Cake",
            image_keywords=["chocolate", "cake", "dessert"]
        )

        self.assertEqual(url, "https://images.unsplash.com/photo-12345")
        self.assertTrue(metadata['success'])
        self.assertTrue(metadata['url_validated'])
        self.assertFalse(metadata.get('fallback_used', False))
        self.assertIsNotNone(metadata.get('attribution'))

    @patch('app.search_unsplash')
    def test_unsplash_returns_none_uses_fallback(self, mock_search):
        """When Unsplash returns None, use fallback."""
        mock_search.return_value = None

        url, metadata = get_smart_stock_image("Exotic Dish")

        self.assertIn(url, FALLBACK_FOOD_IMAGES)
        self.assertTrue(metadata['fallback_used'])

    @patch('app.search_unsplash')
    def test_uses_image_keywords_first(self, mock_search):
        """Should try image_keywords before description or name."""
        mock_search.return_value = {
            'url': "https://images.unsplash.com/photo-keywords",
            'attribution': {
                'photographer_name': 'Test',
                'photographer_url': 'https://unsplash.com/@test',
                'unsplash_url': 'https://unsplash.com',
                'html': 'Photo by Test on Unsplash'
            }
        }

        url, metadata = get_smart_stock_image(
            "Test Recipe",
            description="A test description",
            image_keywords=["vegan", "bowl", "colorful"]
        )

        # Should use keywords in search
        mock_search.assert_called_once()
        call_args = mock_search.call_args[0][0]
        self.assertEqual(call_args, ["vegan", "bowl", "colorful"])

    @patch('app.search_unsplash')
    def test_falls_back_to_description(self, mock_search):
        """When no keywords, should use description."""
        # First call (description) returns URL
        mock_search.return_value = {
            'url': "https://images.unsplash.com/photo-desc",
            'attribution': {
                'photographer_name': 'Desc Photographer',
                'photographer_url': 'https://unsplash.com/@desc',
                'unsplash_url': 'https://unsplash.com',
                'html': 'Photo by Desc Photographer on Unsplash'
            }
        }

        url, metadata = get_smart_stock_image(
            "Test Recipe",
            description="A delicious vegan curry"
        )

        self.assertEqual(url, "https://images.unsplash.com/photo-desc")
        # Should have called with description keywords
        mock_search.assert_called_once()

    @patch('app.search_unsplash')
    def test_unsplash_exception_uses_fallback(self, mock_search):
        """When Unsplash throws exception, use fallback gracefully."""
        mock_search.side_effect = Exception("API Error")

        url, metadata = get_smart_stock_image("Test Recipe")

        # Should use fallback on any search failure
        self.assertIn(url, FALLBACK_FOOD_IMAGES)
        self.assertTrue(metadata['fallback_used'])
        self.assertTrue(metadata['success'])  # Fallback is still a success

    def test_metadata_includes_user_id(self):
        """Metadata should include the user_id."""
        url, metadata = get_smart_stock_image("Test Recipe", user_id="user123")

        self.assertEqual(metadata['user_id'], "user123")

    def test_metadata_includes_timestamp(self):
        """Metadata should include a timestamp."""
        url, metadata = get_smart_stock_image("Test Recipe")

        self.assertIn('timestamp', metadata)
        self.assertIsNotNone(metadata['timestamp'])

    def test_no_loremflickr_urls(self):
        """Should never return loremflickr URLs."""
        url, metadata = get_smart_stock_image("Any Recipe")

        self.assertNotIn("loremflickr", url)


class TestValidateAndRefreshStockImage(unittest.TestCase):
    """Test the validate and refresh function."""

    @patch('app.validate_image_url')
    def test_valid_existing_url_not_refreshed(self, mock_validate):
        """Valid existing URL should not be refreshed."""
        mock_validate.return_value = True
        recipe_data = {
            'name': 'Test Recipe',
            'stock_image_url': 'https://images.unsplash.com/existing'
        }

        url, metadata, was_refreshed = validate_and_refresh_stock_image(recipe_data)

        self.assertEqual(url, 'https://images.unsplash.com/existing')
        self.assertIsNone(metadata)
        self.assertFalse(was_refreshed)

    @patch('app.get_smart_stock_image')
    @patch('app.validate_image_url')
    def test_invalid_existing_url_refreshed(self, mock_validate, mock_get_smart):
        """Invalid existing URL should trigger refresh."""
        mock_validate.return_value = False
        mock_get_smart.return_value = ('https://new-url.com/image.jpg', {'success': True})
        recipe_data = {
            'name': 'Test Recipe',
            'stock_image_url': 'https://broken-url.com/old.jpg'
        }

        url, metadata, was_refreshed = validate_and_refresh_stock_image(recipe_data)

        self.assertEqual(url, 'https://new-url.com/image.jpg')
        self.assertTrue(was_refreshed)
        mock_get_smart.assert_called_once()

    @patch('app.get_smart_stock_image')
    def test_missing_url_gets_new_one(self, mock_get_smart):
        """Missing stock_image_url should get a new one."""
        mock_get_smart.return_value = ('https://new-url.com/image.jpg', {'success': True})
        recipe_data = {
            'name': 'Test Recipe'
            # No stock_image_url
        }

        url, metadata, was_refreshed = validate_and_refresh_stock_image(recipe_data)

        self.assertEqual(url, 'https://new-url.com/image.jpg')
        self.assertTrue(was_refreshed)


class TestNoRandomBehavior(unittest.TestCase):
    """Ensure no random/lock behavior in stock image generation."""

    @patch('app.search_unsplash')
    def test_fallback_is_deterministic(self, mock_search):
        """Fallback images should be deterministic, not random."""
        mock_search.return_value = None

        # Call multiple times for the same recipe
        results = [get_smart_stock_image("Same Recipe")[0] for _ in range(10)]

        # All results should be identical
        self.assertEqual(len(set(results)), 1)

    def test_no_lock_parameter_in_fallbacks(self):
        """Fallback URLs should not contain lock parameters."""
        for url in FALLBACK_FOOD_IMAGES:
            self.assertNotIn("lock=", url)
            self.assertNotIn("random", url.lower())


if __name__ == '__main__':
    unittest.main()

import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import json

# Ensure app can be imported
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import app

class TestModelFetching(unittest.TestCase):

    def setUp(self):
        self.app = app.test_client()
        self.app.testing = True

    @patch('app.Client')
    def test_get_models_success(self, MockClient):
        # Mock the client instance
        mock_client_instance = MockClient.return_value
        
        # Create dummy models
        mock_models = []
        for i in range(15):
            m = MagicMock()
            m.name = f"models/gemini-{i}"
            m.display_name = f"Gemini {i}"
            # Ensure they are valid content generators
            m.supported_generation_methods = ['generateContent']
            mock_models.append(m)
            
        mock_client_instance.models.list.return_value = mock_models

        with patch('app.GOOGLE_API_KEY', 'fake-key'):
            response = self.app.get('/api/models')
            
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)
            
            # Check limit 10
            self.assertEqual(len(data), 10)
            self.assertEqual(data[0]['id'], "models/gemini-0")
            self.assertEqual(data[0]['name'], "Gemini 0")

    @patch('app.Client')
    def test_get_models_filter_unsupported(self, MockClient):
        mock_client_instance = MockClient.return_value
        
        m1 = MagicMock()
        m1.name = "models/text-only"
        m1.display_name = "Text Only Model"
        m1.supported_generation_methods = ['generateContent']
        
        m2 = MagicMock()
        m2.name = "models/embedding-only"
        m2.display_name = "Embedding Model"
        m2.supported_generation_methods = ['embedContent'] # Not generateContent
        
        mock_client_instance.models.list.return_value = [m1, m2]

        with patch('app.GOOGLE_API_KEY', 'fake-key'):
            response = self.app.get('/api/models')
            
            data = json.loads(response.data)
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]['id'], "models/text-only")

    def test_get_models_no_api_key(self):
        # Patch the global variable in app module
        with patch('app.GOOGLE_API_KEY', None):
            response = self.app.get('/api/models')
            self.assertEqual(response.status_code, 500)
            error_msg = response.get_json()['error']
            self.assertIn('API key not configured', error_msg)

    @patch('app.Client')
    def test_get_models_api_error(self, MockClient):
        mock_client_instance = MockClient.return_value
        mock_client_instance.models.list.side_effect = Exception("API connection failed")

        with patch('app.GOOGLE_API_KEY', 'fake-key'):
            response = self.app.get('/api/models')
            self.assertEqual(response.status_code, 500)
            self.assertIn("API connection failed", response.get_json()['error'])

if __name__ == '__main__':
    unittest.main()

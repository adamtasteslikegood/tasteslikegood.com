import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Add the project root directory to sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import app

class AuthTestCase(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        app.config['SECRET_KEY'] = 'test-secret-key'
        os.environ['GOOGLE_CLIENT_ID'] = 'test-client-id'
        os.environ['GOOGLE_CLIENT_SECRET'] = 'test-client-secret'
        self.client = app.test_client()

    @patch('google_auth_oauthlib.flow.Flow.authorization_url')
    def test_login(self, mock_authorization_url):
        mock_authorization_url.return_value = ('https://accounts.google.com/o/oauth2/auth?prompt=consent', 'test-state')
        response = self.client.get('/auth/login')
        self.assertEqual(response.status_code, 302)
        self.assertIn('https://accounts.google.com/o/oauth2/auth', response.location)

    def test_logout(self):
        with self.client.session_transaction() as sess:
            sess['credentials'] = {'token': 'test-token'}
            sess['user_info'] = {'name': 'Test User'}
        response = self.client.get('/auth/logout', follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Login with Google', response.data)

if __name__ == '__main__':
    unittest.main()

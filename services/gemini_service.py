"""
Gemini AI service for client management and generation.

Handles:
- Creating authenticated Gemini clients (OAuth or API key)
- Dual authentication fallback strategy
- Content generation with error handling
"""
from google.genai import Client
import google.oauth2.credentials
from config import GOOGLE_API_KEY


def get_genai_client(session_credentials=None):
    """
    Get an authenticated GenAI client.

    Uses dual authentication strategy:
    1. Try user credentials from session (if provided)
    2. Fallback to API key

    Args:
        session_credentials: Optional dict of OAuth credentials from Flask session

    Returns:
        Client: Authenticated Gemini client, or None if no auth available
    """
    if session_credentials:
        try:
            creds = google.oauth2.credentials.Credentials(**session_credentials)
            return Client(credentials=creds)
        except Exception as e:
            print(f"Failed to create client from user credentials: {e}")

    if GOOGLE_API_KEY:
        try:
            return Client(api_key=GOOGLE_API_KEY)
        except Exception as e:
            print(f"Failed to create client from API key: {e}")

    return None


def attempt_generation(client, model, prompt):
    """
    Attempt content generation with the Gemini API.

    Args:
        client: Authenticated Gemini client
        model: Model ID string (e.g., 'models/gemini-2.0-flash-exp')
        prompt: Generation prompt string

    Returns:
        str: Generated content text

    Raises:
        Exception: If generation fails
    """
    response = client.models.generate_content(
        model=model,
        contents=prompt
    )
    return response.text

import os
from google.genai import Client
from dotenv import load_dotenv

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if not GOOGLE_API_KEY:
    print("Error: GOOGLE_API_KEY not found.")
    exit(1)

client = Client(api_key=GOOGLE_API_KEY)

print("--- Listing Available Models ---")
try:
    models = client.models.list()
    found_any = False
    for m in models:
        # Check if it supports generateContent
        methods = getattr(m, "supported_generation_methods", [])
        if "generateContent" in methods:
            print(f"Name: {m.name}, Display: {m.display_name}")
            found_any = True
    if not found_any:
        print("No models found with generateContent support.")
except Exception as e:
    print(f"Error listing models: {e}")

print("\n--- Testing Specific Models ---")
test_models = [
    "gemini-2.0-flash-exp",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.5-pro",
    "gemini-1.0-pro",
    "gemini-3-pro-preview",  # Testing the user's requested one
    "gemini-2.5-mini",  # Testing user's request
    "gemini-2.5-nano",  # Testing user's request
]

for model_name in test_models:
    print(f"Testing {model_name}...", end=" ")
    try:
        response = client.models.generate_content(model=model_name, contents="Say 'Hello'")
        print("SUCCESS")
    except Exception as e:
        print(f"FAILED: {e}")

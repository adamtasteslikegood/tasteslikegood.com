import os
from google.genai import Client
from dotenv import load_dotenv

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if not GOOGLE_API_KEY:
    print("Error: GOOGLE_API_KEY not found.")
    exit(1)

client = Client(api_key=GOOGLE_API_KEY)

print("--- Listing All Available Models ---")
try:
    models = client.models.list()
    for m in models:
        # Print everything to find hidden gems
        print(f"Name: {m.name}")
        print(f"  Display: {m.display_name}")
        print(f"  Methods: {getattr(m, 'supported_generation_methods', [])}")
        print("-" * 20)
except Exception as e:
    print(f"Error listing models: {e}")

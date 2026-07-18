import os
import sys

from dotenv import load_dotenv
from google.genai import Client

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if not GOOGLE_API_KEY:
    print("Error: GOOGLE_API_KEY not found.")
    sys.exit(1)

client = Client(api_key=GOOGLE_API_KEY)

print("--- Listing All Available Models ---")
try:
    models = client.models.list()
    count = 0
    for m in models:
        # Print everything to find hidden gems
        print(f"Name: {m.name}")
        print(f"  Display: {m.display_name}")
        print(f"  Methods: {getattr(m, 'supported_generation_methods', [])}")
        count += 1
        print(f"  Raw info for model 'm' number: {count}, in 'client.models.list()':")
        print(f"  raw:  {[{k: v} for (k, v) in m]}")
        print("-" * 20)
except Exception as e:
    print(f"Error listing models: {e}")

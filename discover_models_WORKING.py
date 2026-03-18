import os

import pandas as pd
from dotenv import load_dotenv
from google.genai import Client

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if not GOOGLE_API_KEY:
    print("Error: GOOGLE_API_KEY not found.")
    exit(1)

# Initialize Client (default)
client = Client(api_key=GOOGLE_API_KEY)

# Redirect output to file
with open("model_discovery_output.txt", "w") as f:

    def log(msg):
        print(msg)
        f.write(msg + "\n")

    log(f"--- Client Configuration ---")
    log(f"Client initialized.")

    log("\n--- Listing Models (Standard) ---")
    try:
        models = client.models.list()
        model_dict = {}
        count = 0
        for m in models:
            count += 1
            log(f"\nModel: {m.name}")
            log(f"  Display Name: {m.display_name}")
            log(f"  Description:  {m.description}")
            supported_attrs_list = getattr(m, "supported_actions", [])
            log(f"  Methods: {supported_attrs_list}")
            log(f"  Thinking: {m.thinking}")
            log("-" * 10)
            model_dict[m.name] = {
                "Item Number": count,
                "Display Name": m.display_name,
                "Description": m.description,
                "supported_actions": supported_attrs_list,
                "thinking": m.thinking,
            }
        # Prints model_dict 'as is'
        # log(f"Model Dictionary: {model_dict}")

        log("\n")
        log(("-+-^_-+-$-" * 3) + " Here's that GOOD dict: " + ("-+-^_-+-$-" * 3))

        # Pretty Prints (logs actually) model_dict
        for model_name, model_attribues in model_dict.items():
            log(f"\n  Model Name: {model_name}")
            for k, v in model_attribues.items():
                log(f"     {k} : {v}")
            log("-" * 10)

        if count == 0:
            log("No models returned by client.models.list()")
    except Exception as e:
        log(f"Error listing models: {e}")

"""
    # Old code for testing SPecific Model Names
    log("\n--- Testing Specific Model Names (Existence Check) ---")
    candidates = [
        "gemini-2.0-flash-exp",
        "gemini-2.5-pro-preview-03-25",
        "gemini-2.5-pro-preview",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
        "models/gemini-2.0-flash-exp",
        "models/gemini-1.5-pro",
    ]

    for name in candidates:
        log(f"Testing info for: {name}...")
        try:
            m = client.models.get(model=name)
            log(f"FOUND! Name: {m.name}")
        except Exception as e:
            log(f"Not found or error: {e}")
"""

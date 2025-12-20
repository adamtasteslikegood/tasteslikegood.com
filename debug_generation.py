from app import get_genai_client, DEFAULT_MODEL
from dotenv import load_dotenv

load_dotenv()

print(f"Testing generation with model: {DEFAULT_MODEL}")

try:
    client = get_genai_client()
    if not client:
        print("Error: Could not get GenAI client.")
        exit(1)
    
    print("Client obtained successfully.")
    
    prompt = "Explain why tacos are tasty in one sentence."
    print(f"Sending prompt: {prompt}")
    
    response = client.models.generate_content(
        model=DEFAULT_MODEL,
        contents=prompt
    )
    
    print("Response received:")
    print(response.text)
    print("Success!")

except Exception as e:
    print(f"Generation failed: {e}")
    import traceback
    traceback.print_exc()

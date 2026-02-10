print("Starting debug script...")
import os
from dotenv import load_dotenv
from google.genai import Client

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
print(f"API Key present: {bool(GOOGLE_API_KEY)}")

def test_generation():
    recipe_name = "Refreshing Zesty Rainbow Fruit Salad"
    image_prompt = f"A delicious, high-quality food photography shot of {recipe_name}. Professional lighting, appetizing."
    
    with open("debug_output.txt", "a") as f:
        f.write(f"Attempting generation for: {recipe_name}\n")
    
    try:
        client = Client(api_key=GOOGLE_API_KEY)
        model_to_use = 'imagen-4.0-generate-preview-06-06'
        
        with open("debug_output.txt", "a") as f:
            f.write(f"Calling generate_images with model {model_to_use}...\n")
            
        response = client.models.generate_images(
            model=model_to_use,
            prompt=image_prompt,
            config={'number_of_images': 1}
        )
        
        if response.generated_images:
            with open("debug_output.txt", "a") as f:
                f.write("Success! Image generated.\n")
            image_data = response.generated_images[0].image.image_bytes
            with open("debug_output.txt", "a") as f:
                f.write(f"Image size: {len(image_data)} bytes\n")
        else:
            with open("debug_output.txt", "a") as f:
                f.write("Failed: No images returned.\n")
            
    except Exception as e:
        with open("debug_output.txt", "a") as f:
            f.write(f"Error during generation: {e}\n")
            import traceback
            traceback.print_exc(file=f)

if __name__ == "__main__":
    with open("debug_output.txt", "w") as f:
        f.write("Script started\n")
    try:
        test_generation()
    except Exception as e:
        with open("debug_output.txt", "a") as f:
             f.write(f"Top level error: {e}\n")

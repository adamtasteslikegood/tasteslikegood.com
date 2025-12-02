import os
import json
import time

from dotenv import load_dotenv
from flask import Flask, render_template, abort, request, redirect, url_for, Response, session
from google.genai import Client
import google.oauth2.credentials

from jsonschema import Draft7Validator, ValidationError
from auth import auth_bp

load_dotenv()

# Initialize the Flask application
app = Flask(__name__)
app.secret_key = os.urandom(24)
app.register_blueprint(auth_bp, url_prefix='/auth')


# The folder where the recipe .json files are stored
RECIPES_DIR = 'recipes'
RECIPE_SCHEMA_PATH = 'recipe_schema.json'

# Ensure the recipes directory exists so list/save operations do not fail
os.makedirs(RECIPES_DIR, exist_ok=True)


def _load_recipe_schema():
    try:
        with open(RECIPE_SCHEMA_PATH, 'r') as schema_file:
            return json.load(schema_file)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Warning: Unable to load recipe schema. Error: {exc}")
        return None


RECIPE_SCHEMA = _load_recipe_schema()
RECIPE_VALIDATOR = Draft7Validator(RECIPE_SCHEMA) if RECIPE_SCHEMA else None

# Configure API Key (fallback)
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")


def validate_recipe_data(recipe_data):
    """Validate recipe data against the JSON schema."""
    if RECIPE_VALIDATOR is None:
        raise RuntimeError("Recipe schema is not available for validation.")

    errors = sorted(
        RECIPE_VALIDATOR.iter_errors(recipe_data), key=lambda e: tuple(e.path)
    )
    if errors:
        first_error = errors[0]
        location = " -> ".join(str(part) for part in first_error.absolute_path)
        message = first_error.message
        if location:
            message = f"{message} (at {location})"
        raise ValidationError(message)


def get_all_recipes():
    """Gets a list of all recipes, reading the name from each JSON file."""
    recipes = []
    for filename in os.listdir(RECIPES_DIR):
        if filename.endswith('.json'):
            filepath = os.path.join(RECIPES_DIR, filename)
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                    recipes.append({
                        'name': data.get('name', 'Unnamed Recipe'),
                        'filename': filename
                    })
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Could not read or parse {filename}. Error: {e}")
    return sorted(recipes, key=lambda r: r['name'])


@app.route('/')
def index():
    """The homepage route. Displays a list of all recipes."""
    recipes = get_all_recipes()
    return render_template('index.html', recipes=recipes)


@app.route('/recipe/<filename>')
def show_recipe(filename):
    """The route to display a single recipe."""
    filepath = os.path.join(RECIPES_DIR, filename)
    if not os.path.exists(filepath):
        abort(404)
    try:
        with open(filepath, 'r') as f:
            recipe_data = json.load(f)
        recipe_data['filename'] = filename
        return render_template('recipe.html', recipe=recipe_data)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error processing {filename}. Error: {e}")
        abort(500)


@app.route('/recipe/<filename>/json')
def show_recipe_json(filename):
    """The route to display the raw JSON for a single recipe."""
    filepath = os.path.join(RECIPES_DIR, filename)
    if not os.path.exists(filepath):
        abort(404)
    try:
        with open(filepath, 'r') as f:
            recipe_data = json.load(f)
        
        pretty_json = json.dumps(recipe_data, indent=2)
        
        if request.args.get('raw') == 'true':
            return Response(pretty_json, mimetype='application/json')
            
        recipe_data['filename'] = filename
        return render_template('json_viewer.html', recipe=recipe_data, recipe_json_str=pretty_json)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error processing {filename}. Error: {e}")
        abort(500)


# --- NEW: Route for generating recipes ---
@app.route('/generate_recipe', methods=['GET', 'POST'])
def generate_recipe():
    """Handles both displaying the form and processing the generation request."""
    if request.method == 'POST':
        prompt = request.form.get('prompt', '').strip()
        if not prompt:
            return "A prompt describing the desired recipe is required.", 400
            
        if len(prompt) < 10 or len(prompt) > 500:
            return "Prompt must be between 10 and 500 characters.", 400

        if RECIPE_SCHEMA is None or RECIPE_VALIDATOR is None:
            return "Recipe schema is unavailable; cannot validate generated recipes.", 500

        # The JSON schema to guide the model's output
        with open(RECIPE_SCHEMA_PATH, 'r') as f:
            schema = f.read()

        # Create the full prompt for the model
        full_prompt = (
            f"Generate a Vegan recipe based on the following request: '{prompt}'. "
            f"The output must be a valid JSON object that strictly follows this schema:\n"
            f"{schema}"
            f"Do not include any text before or after the JSON object."
        )

        def attempt_generation(client, source_name):
            """Helper to attempt generation with a specific client."""
            print(f"Attempting generation using {source_name}...")
            response = client.models.generate_content(
                model='gemini-3-pro-preview',
                contents=full_prompt
            )
            # Extract the JSON string from the response
            text_response = response.text.strip().replace('```json', '').replace('```', '').strip()
            # Parse and validate
            data = json.loads(text_response)
            validate_recipe_data(data)
            return data, text_response

        recipe_data = None
        last_error_message = "No generation method available."
        recipe_json_str = "" # Keep for error logging
        response = None # Keep for error logging

        start_time = time.time()

        # 1. Try User Credentials
        if 'credentials' in session:
            try:
                creds = google.oauth2.credentials.Credentials(**session['credentials'])
                user_client = Client(credentials=creds)
                recipe_data, recipe_json_str = attempt_generation(user_client, "User Credentials")
            except Exception as e:
                print(f"User credential generation failed: {e}")
                last_error_message = f"User Auth Error: {e}"
        
        # 2. Fallback to API Key if step 1 failed or wasn't attempted
        if recipe_data is None and GOOGLE_API_KEY:
            try:
                api_client = Client(api_key=GOOGLE_API_KEY)
                recipe_data, recipe_json_str = attempt_generation(api_client, "API Key")
            except Exception as e:
                print(f"API Key generation failed: {e}")
                last_error_message = f"API Key Error: {e}"

        if recipe_data:
            try:
                # Create a filename from the recipe name
                safe_filename = "".join(c for c in recipe_data['name'] if c.isalnum() or c in (' ', '_')).rstrip()
                filename = safe_filename.replace(' ', '_').lower() + '.json'
                filepath = os.path.join(RECIPES_DIR, filename)

                # Save the new recipe to a file
                with open(filepath, 'w') as f:
                    json.dump(recipe_data, f, indent=2)
                    
                end_time = time.time()
                print(f"Recipe generated successfully in {end_time - start_time:.2f} seconds.")

                # Redirect to the new recipe's page
                return redirect(url_for('show_recipe', filename=filename))
            except Exception as e:
                last_error_message = f"File Save Error: {e}"

        # If we reached here, both methods failed or saving failed
        # Log the error details securely
        try:
            with open('recipe_error.json', 'a+') as f:
                f.write(f"{recipe_json_str}\n")
            with open('recipe_error.txt', 'a') as f:
                f.write(
                    f"Full prompt:\n{full_prompt}\n\n"
                    f"Last Error: {last_error_message}\n"
                )
        except Exception as logging_error:
            print(f"Error while logging: {logging_error}")

        # Show the error response to the user
        return f"Sorry, there was an error generating the recipe. Details: {last_error_message}", 500

    # For a GET request, just show the form
    return render_template('generate_recipe.html')



if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
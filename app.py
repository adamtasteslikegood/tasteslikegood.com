import os
import json
import time
import csv

from dotenv import load_dotenv
from flask import Flask, render_template, abort, request, redirect, url_for, Response, session, jsonify
from google.genai import Client
import google.oauth2.credentials

from jsonschema import Draft7Validator, ValidationError
from auth import auth_bp
from utils import normalize_recipe_data

load_dotenv()

# Load configuration
CONFIG_PATH = 'config.json'


def _load_config():
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Warning: Could not load config.json: {e}")
        return {}


CONFIG = _load_config()

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


MODELS_LIST_PATH = 'models_list.json'

# Curated list of preferred Gemini models for recipe generation
PREFERRED_MODELS = [
    'models/gemini-2.5-pro',
    'models/gemini-2.5-flash',
    'models/gemini-2.0-flash',
    'models/gemini-2.0-flash-exp',
    'models/gemini-3-pro-preview',
    'models/gemini-2.0-flash-lite',
    'models/gemini-exp-1206',
    'models/gemini-pro-latest',
    'models/gemini-flash-latest',
]


def load_models_from_cache():
    """Load models from the cached models_list.json file."""
    try:
        with open(MODELS_LIST_PATH, 'r') as f:
            data = json.load(f)
            return data.get('models', [])
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Could not load models cache: {e}")
        return None


def filter_and_sort_models(models_list):
    """Filter and sort models for recipe generation."""
    exclude_patterns = ['embedding', 'imagen', 'veo', 'live', 'tts', 'audio', 'robotics', 'aqa']

    filtered_models = []
    for m in models_list:
        model_name = m.get('name', '').lower()

        # Skip non-generation models
        if any(pattern in model_name for pattern in exclude_patterns):
            continue

        # Skip image generation specific models
        if 'image' in model_name and 'gemini' in model_name:
            continue

        # Only include gemini/gemma models
        if not ('gemini' in model_name or 'gemma' in model_name):
            continue

        filtered_models.append({
            'id': m.get('name'),
            'name': m.get('display_name') or m.get('name')
        })

    # Sort: preferred models first, then alphabetically
    def sort_key(model):
        model_id = model['id']
        if model_id in PREFERRED_MODELS:
            return (0, PREFERRED_MODELS.index(model_id))
        return (1, model['name'])

    filtered_models.sort(key=sort_key)
    return filtered_models[:8]


@app.route('/api/models')
def get_models():
    """Returns a curated list of Gemini models for recipe generation."""
    try:
        cached_models = load_models_from_cache()

        if cached_models:
            return jsonify(filter_and_sort_models(cached_models))

        # Fallback: return empty list if no cache available
        return jsonify([])

    except Exception as e:
        print(f"Error fetching models: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/models/refresh', methods=['POST'])
def refresh_models():
    """Fetches fresh models from Gemini API and updates the cache."""
    client = None
    auth_method = None

    # 1. Try User Credentials first
    if 'credentials' in session:
        try:
            creds = google.oauth2.credentials.Credentials(**session['credentials'])
            client = Client(credentials=creds)
            auth_method = "user_credentials"
        except Exception as e:
            print(f"User credential client failed: {e}")

    # 2. Fallback to API Key
    if client is None and GOOGLE_API_KEY:
        try:
            client = Client(api_key=GOOGLE_API_KEY)
            auth_method = "api_key"
        except Exception as e:
            print(f"API Key client failed: {e}")

    if client is None:
        return jsonify({
            'error': 'No valid authentication method available. Please login or configure API key.'
        }), 401

    try:
        # Fetch models from Gemini API
        models_response = client.models.list()

        # Convert to list of dicts for caching
        models_data = []
        for model in models_response:
            models_data.append({
                'name': model.name,
                'display_name': getattr(model, 'display_name', model.name),
                'supported_generation_methods': getattr(model, 'supported_generation_methods', [])
            })

        # Update the cache file
        cache_data = {
            'models': models_data,
            'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        }
        with open(MODELS_LIST_PATH, 'w') as f:
            json.dump(cache_data, f, indent=2)

        # Return filtered models
        filtered_models = filter_and_sort_models(models_data)

        return jsonify({
            'models': filtered_models,
            'auth_method': auth_method,
            'total_fetched': len(models_data),
            'message': f'Successfully fetched {len(models_data)} models using {auth_method}'
        })

    except Exception as e:
        print(f"Error refreshing models from API: {e}")
        return jsonify({
            'error': f'Failed to fetch models: {str(e)}',
            'auth_method': auth_method
        }), 500


@app.route('/api/jokes')
def get_jokes():
    """Returns additional jokes from CSV file if it exists."""
    joke_file = CONFIG.get('app', {}).get('joke_file', 'computer_jokes.csv')
    if not os.path.exists(joke_file):
        return jsonify([])
    try:
        with open(joke_file, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            return jsonify([row['joke'] for row in reader if row.get('joke')])
    except Exception as e:
        print(f"Error loading jokes from {joke_file}: {e}")
        return jsonify([])


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

        # Get the selected model from the form, default from config
        default_model = CONFIG.get('app', {}).get('default_model', 'models/gemini-2.5-flash')
        selected_model = request.form.get('model', default_model)

        # Create the full prompt for the model
        full_prompt = (
            f"Generate a Vegan recipe based on the following request: '{prompt}'. "
            f"The output must be a valid JSON object that strictly follows this schema:\n"
            f"{schema}"
            f"Do not include any text before or after the JSON object."
        )

        def attempt_generation(client, source_name, model_name):
            """Helper to attempt generation with a specific client."""
            print(f"Attempting generation using {source_name} with model {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents=full_prompt
            )
            # Extract the JSON string from the response
            text_response = response.text.strip().replace('```json', '').replace('```', '').strip()
            # Parse and validate
            data = json.loads(text_response)
            
            # --- NEW: Normalize data before validation ---
            data = normalize_recipe_data(data)
            
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
                recipe_data, recipe_json_str = attempt_generation(user_client, "User Credentials", selected_model)
            except Exception as e:
                print(f"User credential generation failed: {e}")
                last_error_message = f"User Auth Error ({type(e).__name__}): {e}"
        
        # 2. Fallback to API Key if step 1 failed or wasn't attempted
        if recipe_data is None and GOOGLE_API_KEY:
            try:
                api_client = Client(api_key=GOOGLE_API_KEY)
                recipe_data, recipe_json_str = attempt_generation(api_client, "API Key", selected_model)
            except Exception as e:
                print(f"API Key generation failed: {e}")
                last_error_message = f"API Key Error ({type(e).__name__}): {e}"

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

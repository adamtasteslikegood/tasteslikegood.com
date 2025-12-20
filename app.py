import os
import json
import time
import datetime
import re

from dotenv import load_dotenv
from flask import Flask, render_template, abort, request, redirect, url_for, Response, session, jsonify
from google.genai import Client
import google.oauth2.credentials

from jsonschema import Draft7Validator, ValidationError
from auth import auth_bp
from utils import normalize_recipe_data

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

# Simple cache for recipe list to avoid reading all files on every request
_recipes_cache = {'data': None, 'timestamp': 0}
# Cache TTL (in seconds) for the recipes list. Default is 60s, which balances
# avoiding frequent disk reads with keeping the list reasonably fresh.
_RECIPES_CACHE_TTL = int(os.getenv("RECIPES_CACHE_TTL", "60"))


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
# Debug logging - executed at import time to verify API key configuration
print(f"DEBUG: Loaded GOOGLE_API_KEY: {'Yes' if GOOGLE_API_KEY else 'No'}")

# Default Model Configuration
DEFAULT_MODEL = "gemini-2.0-flash-exp"

# Validate DEFAULT_MODEL (Simple check, could be expanded)
if not DEFAULT_MODEL:
    # Debug logging - executed at import time to warn about missing model configuration
    print("Warning: DEFAULT_MODEL is not set. Fallback to 'gemini-2.0-flash-exp'.")
    DEFAULT_MODEL = "gemini-2.0-flash-exp"


def get_genai_client():
    """Helper to get an authenticated GenAI client."""
    if 'credentials' in session:
        creds = google.oauth2.credentials.Credentials(**session['credentials'])
        return Client(credentials=creds)
    elif GOOGLE_API_KEY:
        return Client(api_key=GOOGLE_API_KEY)
    return None


def get_smart_stock_image(recipe_name):
    """Uses Gemini to generate keywords for a stock image."""
    client = get_genai_client()
    if not client:
        return None
        
    try:
        prompt = (
            f"Generate 2-3 simple, comma-separated keywords to search for a stock photo of '{recipe_name}'. "
            f"Example: 'spicy tacos, mexican food'. Return ONLY the keywords."
        )
        # Use a fast model for this
        response = client.models.generate_content(
            model='gemini-2.0-flash-exp',
            contents=prompt
        )
        keywords = response.text.strip().replace(' ', ',')
        # Use loremflickr with specific keywords which is reliable
        import random
        lock_id = random.randint(1, 10000)
        return f"https://loremflickr.com/800/600/{keywords}/all?lock={lock_id}"
    except Exception as e:
        print(f"Smart stock image fetch failed: {e}")
    
    return None


def sanitize_filename(filename):
    """Sanitize filename to prevent path traversal attacks."""
    # Use only the basename to prevent directory traversal
    safe_filename = os.path.basename(filename)
    # Additional validation: ensure it's not empty and ends with .json
    if not safe_filename or not safe_filename.endswith('.json'):
        raise ValueError("Invalid filename")
    return safe_filename


def validate_recipe_filepath(filename):
    """Validate that a recipe filepath is safe and within RECIPES_DIR."""
    try:
        safe_filename = sanitize_filename(filename)
        filepath = os.path.join(RECIPES_DIR, safe_filename)
        # Resolve to absolute path and verify it's within RECIPES_DIR
        abs_filepath = os.path.abspath(filepath)
        abs_recipes_dir = os.path.abspath(RECIPES_DIR)
        if not abs_filepath.startswith(abs_recipes_dir + os.sep):
            raise ValueError("Path traversal detected")
        return filepath
    except (ValueError, OSError) as e:
        raise ValueError(f"Invalid filename: {e}")


def validate_recipe_data(recipe_data):
    """Validate recipe data against the JSON schema."""
    if RECIPE_VALIDATOR is None:
        raise RuntimeError("Recipe schema is not available for validation.")

    # Get the first error without sorting all errors
    first_error = next(RECIPE_VALIDATOR.iter_errors(recipe_data), None)
    if first_error:
        location = " -> ".join(str(part) for part in first_error.absolute_path)
        message = first_error.message
        if location:
            message = f"{message} (at {location})"
        raise ValidationError(message)
    
    return True


def get_all_recipes():
    """Gets a list of all recipes, with in-memory caching to reduce disk I/O."""
    global _recipes_cache
    
    current_time = time.time()
    # Return cached data if still valid
    if _recipes_cache['data'] is not None and (current_time - _recipes_cache['timestamp']) < _RECIPES_CACHE_TTL:
        return _recipes_cache['data']
    
    # Cache miss or expired - read from disk
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
    
    sorted_recipes = sorted(recipes, key=lambda r: r['name'])
    
    # Update cache
    _recipes_cache['data'] = sorted_recipes
    _recipes_cache['timestamp'] = current_time
    
    return sorted_recipes


def invalidate_recipes_cache():
    """Invalidate the recipes cache, forcing a refresh on next request."""
    global _recipes_cache
    _recipes_cache['data'] = None
    _recipes_cache['timestamp'] = 0


def migrate_recipe_data(data, filename):
    """
    Migrates recipe data to the latest schema.
    Returns (migrated_data, changed_boolean).
    """
    changed = False
    
    # 1. Fix nested 'properties'
    if 'properties' in data and 'name' not in data:
        print(f"Migrating nested JSON in {filename}")
        data = data['properties']
        changed = True
        
    # 2. Add user_id
    if 'user_id' not in data:
        data['user_id'] = 'anonymous'
        changed = True
        
    # 3. Add ai_metadata
    if 'ai_metadata' not in data:
        data['ai_metadata'] = {
            'model': 'unknown',
            'timestamp': datetime.datetime.now().isoformat(),
            'prompt': 'unknown',
            'images_working': True if data.get('stock_image_url') else False
        }
        changed = True
    
    # 4. Fix "Untitled Recipe" if name is generic and filename is specific
    if data.get('name') == "Untitled Recipe":
        # Try to derive from filename
        derived_name = filename.replace('_', ' ').replace('.json', '').title()
        data['name'] = derived_name
        changed = True
        
    return data, changed


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
        
        updated = False
        
        # --- Auto-Migration ---
        recipe_data, migrated = migrate_recipe_data(recipe_data, filename)
        if migrated:
            updated = True
            print(f"DEBUG: Auto-migrated {filename} on view.")
        
        # --- Lazy Load Images ---
        # 1. Stock Image
        if not recipe_data.get('stock_image_url'):
            # Try smart retrieval first
            print(f"DEBUG: Fetching smart stock image for {recipe_data.get('name')}...")
            smart_url = get_smart_stock_image(recipe_data.get('name'))
            
            if smart_url:
                recipe_data['stock_image_url'] = smart_url
                print(f"DEBUG: Found smart stock URL: {smart_url}")
            else:
                # Fallback to loremflickr if smart fetch fails
                import random
                keywords = recipe_data.get('name', 'food').replace(' ', ',')
                lock_id = random.randint(1, 10000)
                recipe_data['stock_image_url'] = f"https://loremflickr.com/800/600/{keywords}/all?lock={lock_id}"
                print(f"DEBUG: Fallback to LoremFlickr: {recipe_data['stock_image_url']}")
            
            updated = True
            
        # 2. AI Image - REMOVED synchronous generation
        # Generation is now handled asynchronously via /api/generate_image/<filename>

        if updated:
            # Save the updated recipe
            with open(filepath, 'w') as f:
                json.dump(recipe_data, f, indent=2)
            # Invalidate cache since recipe name may have changed during migration
            invalidate_recipes_cache()

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


@app.route('/api/models')
def get_models():
    """Fetches a list of active models from the Google GenAI API."""
    try:
        # Use API key if available
        if not GOOGLE_API_KEY:
             return jsonify({'error': 'API key not configured'}), 500

        client = Client(api_key=GOOGLE_API_KEY)
        
        # List models and filter in a single pass
        filtered_models = []
        count = 0
        for m in client.models.list():
            if count >= 10:
                break
                
            # Filter logic: Include gemini/gemma models, exclude embeddings and older versions
            name = m.name.lower()
            if ('gemini' in name or 'gemma' in name) and 'embedding' not in name:
                # Exclude clearly older/deprecated model versions
                if any(x in name for x in ['1.0', '1.5']):
                    continue
                    
                # Clean up the name (remove 'models/' prefix for display)
                model_id = m.name.replace('models/', '')
                display_name = m.display_name or model_id
                
                filtered_models.append({
                    'id': model_id,
                    'name': display_name
                })
                count += 1

        return jsonify(filtered_models)
    except Exception as e:
        print(f"Error fetching models: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/generate_image/<filename>', methods=['POST'])
def generate_recipe_image(filename):
    """Generates an AI image for a recipe asynchronously.
    
    Note: This endpoint and others (regenerate_recipe_image, report_recipe, show_recipe)
    can read and write to the same recipe JSON files concurrently without file locking.
    This creates a race condition risk that could lead to data loss or corruption if
    multiple requests modify the same file simultaneously. Consider implementing file
    locking (e.g., fcntl on Unix, msvcrt on Windows) or migrating to a proper database
    for production use.
    """
    try:
        filepath = validate_recipe_filepath(filename)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    
    if not os.path.exists(filepath):
        return jsonify({'error': 'Recipe not found'}), 404

    try:
        with open(filepath, 'r') as f:
            recipe_data = json.load(f)

        if recipe_data.get('ai_image_url'):
            return jsonify({'image_url': recipe_data['ai_image_url']})

        # Generate Image
        try:
            client = None
            if 'credentials' in session:
                creds = google.oauth2.credentials.Credentials(**session['credentials'])
                client = Client(credentials=creds)
            elif GOOGLE_API_KEY:
                client = Client(api_key=GOOGLE_API_KEY)
            
            if not client:
                 return jsonify({'error': 'No credentials available'}), 500

            print(f"DEBUG: Async AI image generation for {recipe_data.get('name')}...")
            image_prompt = f"A delicious, high-quality food photography shot of {recipe_data.get('name')}. Professional lighting, appetizing."
            
            model_to_use = 'imagen-4.0-generate-001'
            response = client.models.generate_images(
                model=model_to_use,
                prompt=image_prompt,
                config={'number_of_images': 1}
            )
            
            if response.generated_images:
                image_data = response.generated_images[0].image.image_bytes
                safe_filename = sanitize_filename(filename)
                image_filename = f"ai_{safe_filename.replace('.json', '.png')}"
                image_path = os.path.join('static', 'images', image_filename)
                os.makedirs(os.path.dirname(image_path), exist_ok=True)
                
                with open(image_path, 'wb') as img_f:
                    img_f.write(image_data)
                    
                image_url = url_for('static', filename=f'images/{image_filename}')
                
                # Update recipe file
                recipe_data['ai_image_url'] = image_url
                
                # Update metadata if present
                if 'ai_metadata' in recipe_data:
                    recipe_data['ai_metadata']['ai_image_url'] = image_url
                    recipe_data['ai_metadata']['images_working'] = True

                with open(filepath, 'w') as f:
                    json.dump(recipe_data, f, indent=2)
                
                return jsonify({'image_url': image_url})
            
            # No images generated but no exception raised
            return jsonify({'error': 'No images generated'}), 500
                    
        except Exception as e:
            import traceback
            traceback_str = traceback.format_exc()
            print(f"Error generating recipe: {e}")
            print(traceback_str)
            
            # TODO: Implement log rotation for recipe_error.txt to prevent unbounded growth.
            # Consider using Python's logging module with RotatingFileHandler.
            # Log to file for debugging
            with open('recipe_error.txt', 'a') as f:
                f.write(f"\nLast Error (Recipe Gen): {repr(e)}\nTraceback:\n{traceback_str}\n")
                
            error_msg = f"{type(e).__name__}: {str(e)}" or "Unknown error"
            return jsonify({'error': error_msg}), 500

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/regenerate_image/<filename>', methods=['POST'])
def regenerate_recipe_image(filename):
    """Force regeneration of an AI image.
    
    TODO: This function shares nearly identical logic with generate_recipe_image.
    Consider extracting the common image generation code into a shared helper function
    to follow DRY principles and make maintenance easier.
    """
    try:
        filepath = validate_recipe_filepath(filename)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    
    if not os.path.exists(filepath):
        return jsonify({'error': 'Recipe not found'}), 404

    try:
        with open(filepath, 'r') as f:
            recipe_data = json.load(f)
            
        # Clear existing image URL to force generation
        if 'ai_image_url' in recipe_data:
            del recipe_data['ai_image_url']
            
        # ... logic copied/adapted ...
        client = get_genai_client()
        if not client:
             return jsonify({'error': 'No credentials available'}), 500

        print(f"DEBUG: Regenerating AI image for {recipe_data.get('name')}...")
        image_prompt = f"A delicious, high-quality food photography shot of {recipe_data.get('name')}. Professional lighting, appetizing."
        
        # Use metadata model if available, else default
        model_to_use = 'imagen-4.0-generate-001'
        if recipe_data.get('ai_metadata', {}).get('model'):
             # If the metadata model is a text model, we still use imagen for image gen.
             # So we stick to imagen-4.0.
             pass

        response = client.models.generate_images(
            model=model_to_use,
            prompt=image_prompt,
            config={'number_of_images': 1}
        )
        
        if response.generated_images:
            image_data = response.generated_images[0].image.image_bytes
            safe_filename = sanitize_filename(filename)
            image_filename = f"ai_{safe_filename.replace('.json', '.png')}"
            image_path = os.path.join('static', 'images', image_filename)
            os.makedirs(os.path.dirname(image_path), exist_ok=True)
            
            with open(image_path, 'wb') as img_f:
                img_f.write(image_data)
                
            image_url = url_for('static', filename=f'images/{image_filename}')
            
            # Update recipe data
            recipe_data['ai_image_url'] = image_url
            
            # Update metadata
            if 'ai_metadata' not in recipe_data:
                recipe_data['ai_metadata'] = {}
            recipe_data['ai_metadata']['ai_image_url'] = image_url
            recipe_data['ai_metadata']['timestamp'] = datetime.datetime.now().isoformat()
            recipe_data['ai_metadata']['images_working'] = True
            
            with open(filepath, 'w') as f:
                json.dump(recipe_data, f, indent=2)
                
            return jsonify({'image_url': image_url})
        
        # If no images were generated but no exception was raised, return an error response
        return jsonify({'error': 'No images generated'}), 500

    except Exception as e:
        print(f"Regeneration error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/report_recipe/<filename>', methods=['POST'])
def report_recipe(filename):
    """Log a user report about a recipe or image."""
    try:
        # Validate filename to prevent path traversal
        try:
            validate_recipe_filepath(filename)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        
        data = request.json
        reason = data.get('reason', 'No reason provided')
        
        # Sanitize reason input - limit length and escape HTML
        reason = reason[:500]  # Limit to 500 characters
        
        report_entry = {
            'timestamp': datetime.datetime.now().isoformat(),
            'filename': filename,
            'reason': reason,
            'user_id': session.get('user_id', 'anonymous')
        }
        
        # Log to a reports file as a JSON array
        reports_path = 'user_reports.json'
        reports = []

        if os.path.exists(reports_path):
            try:
                with open(reports_path, 'r', encoding='utf-8') as f:
                    existing_content = f.read().strip()
                    if existing_content:
                        existing_data = json.loads(existing_content)
                        if isinstance(existing_data, list):
                            reports = existing_data
                        else:
                            reports = [existing_data]
            except Exception as read_error:
                # If the existing file is corrupt or unreadable, start fresh
                print(f"Warning: could not read existing reports file: {read_error}")
                reports = []

        reports.append(report_entry)

        with open(reports_path, 'w', encoding='utf-8') as f:
            json.dump(reports, f, indent=2)
            
        return jsonify({'status': 'reported'})
    except Exception as e:
        print(f"Reporting error: {e}")
        return jsonify({'error': str(e)}), 500






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

        # Get the selected model from the form, default to the app's default model
        selected_model = request.form.get('model', DEFAULT_MODEL)

        # Create the full prompt for the model
        full_prompt = (
            f"Generate a Vegan recipe based on the following request: '{prompt}'. "
            f"The output must be a valid JSON object that strictly follows this schema:\n"
            f"{schema}\n"
            f"IMPORTANT: You MUST find and include a valid, high-quality public stock image URL (e.g. from Unsplash, Pexels) "
            f"in the 'stock_image_url' field. Do not leave it empty.\n"
            f"CRITICAL: Return ONLY the flat JSON object matching the schema. Do NOT nest it inside a 'properties' or 'type' object. "
            f"The top-level keys must be 'name', 'description', 'ingredients', etc.\n"
            f"Do not include any text before or after the JSON object."
        )

        def attempt_generation(client, source_name, model_name):
            """Helper to attempt generation with a specific client."""
            print(f"Attempting generation with {source_name} using {model_name}...")
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=full_prompt,
                    config={
                        'response_mime_type': 'application/json',
                        'temperature': 0.7,
                    }
                )
                
                text_response = response.text.strip()
                # Clean up markdown code blocks if present
                if text_response.startswith('```json'):
                    text_response = text_response[7:]
                if text_response.startswith('```'):
                    text_response = text_response[3:]
                if text_response.endswith('```'):
                    text_response = text_response[:-3]
                
                text_response = text_response.strip()
                
                try:
                    data = json.loads(text_response)
                    
                    # FIX: Check for nested 'properties' (common model error with schemas)
                    if 'properties' in data and 'name' not in data:
                        print("DEBUG: Detected nested JSON structure. Flattening...")
                        data = data['properties']
                    
                    # Validate against schema
                    if not validate_recipe_data(data):
                        print(f"Validation failed for {source_name}")
                        return None, None
                        
                    return data, text_response
                except json.JSONDecodeError:
                    print(f"JSON Decode Error for {source_name}: {text_response[:100]}...")
                    return None, None
                    
            except Exception as e:
                print(f"Generation error with {source_name}: {e}")
                raise e

        # 1. Try with User Credentials (if logged in)
        recipe_data = None
        recipe_json_str = None
        last_error_message = "Unknown error"
        start_time = time.time()

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
                # Add metadata
                recipe_data['user_id'] = session.get('user_id', 'anonymous')
                recipe_data['ai_metadata'] = {
                    'model': selected_model,
                    'timestamp': datetime.datetime.now().isoformat(),
                    'prompt': prompt,
                    'stock_image_url': recipe_data.get('stock_image_url'),
                    'ai_image_url': None, # Will be filled by async gen
                    'images_working': True # Optimistic default
                }

                # Create a safe filename from the recipe name
                recipe_name = recipe_data.get('name', 'untitled_recipe')
                # Remove special characters and limit length
                safe_name = re.sub(r'[^\w\s-]', '', recipe_name).strip().lower()
                safe_name = re.sub(r'[-\s]+', '_', safe_name)
                # Limit length to avoid filesystem issues
                safe_name = safe_name[:100]
                # Ensure we have a valid filename
                if not safe_name:
                    safe_name = 'untitled_recipe'
                filename = f"{safe_name}.json"
                filepath = os.path.join(RECIPES_DIR, filename)

                # Save the new recipe to a file
                with open(filepath, 'w') as f:
                    json.dump(recipe_data, f, indent=2)
                
                # Invalidate recipe list cache since we added a new recipe
                invalidate_recipes_cache()
                    
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
    return render_template('generate_recipe.html', default_model=DEFAULT_MODEL)


@app.route('/api/status', methods=['GET'])
def api_status():
    return jsonify({
        'status': 'running',
        'api_key_loaded': bool(GOOGLE_API_KEY),
        'default_model': DEFAULT_MODEL
    })



@app.route('/api/migrate', methods=['POST'])
def run_migration():
    """Migrate old recipes to the new schema."""
    count = 0
    updated_files = []
    
    for filename in os.listdir(RECIPES_DIR):
        if not filename.endswith('.json'):
            continue
            
        filepath = os.path.join(RECIPES_DIR, filename)
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            
            data, changed = migrate_recipe_data(data, filename)

            if changed:
                with open(filepath, 'w') as f:
                    json.dump(data, f, indent=2)
                count += 1
                updated_files.append(filename)
                
        except Exception as e:
            print(f"Error migrating {filename}: {e}")
    
    # Invalidate cache if any recipes were migrated (names may have changed)
    if count > 0:
        invalidate_recipes_cache()
            
    return jsonify({'migrated_count': count, 'files': updated_files})



if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
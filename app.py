import os
import json
import google.generativeai as genai
from flask import Flask, render_template, abort, request, redirect, url_for

# Import the API key
from api_key import GOOGLE_API_KEY

# Initialize the Flask application
app = Flask(__name__)

# The folder where the recipe .json files are stored
RECIPES_DIR = 'recipes'

# --- NEW: Configure the generative model ---
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')


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
        recipe_data['filename'] = filename
        pretty_json = json.dumps(recipe_data, indent=2)
        return render_template('json_viewer.html', recipe=recipe_data, recipe_json_str=pretty_json)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error processing {filename}. Error: {e}")
        abort(500)


# --- NEW: Route for generating recipes ---
@app.route('/generate_recipe', methods=['GET', 'POST'])
def generate_recipe():
    """Handles both displaying the form and processing the generation request."""
    if request.method == 'POST':
        prompt = request.form['prompt']

        # The JSON schema to guide the model's output
        with open('recipe_schema.json', 'r') as f:
            schema = f.read()

        # Create the full prompt for the model
        full_prompt = (
            f"Generate a recipe based on the following request: '{prompt}'. "
            f"The output must be a valid JSON object that strictly follows this schema:\n"
            f"{schema}"
            f"Do not include any text before or after the JSON object."
        )

        try:
            # Generate the content
            response = model.generate_content(full_prompt)
            # Extract the JSON string from the response
            recipe_json_str = response.text.strip().replace('```json', '').replace('```', '').strip()

            # Parse the JSON string into a Python dictionary
            recipe_data = json.loads(recipe_json_str)

            # Create a filename from the recipe name
            safe_filename = "".join(c for c in recipe_data['name'] if c.isalnum() or c in (' ', '_')).rstrip()
            filename = safe_filename.replace(' ', '_').lower() + '.json'
            filepath = os.path.join(RECIPES_DIR, filename)

            # Save the new recipe to a file
            with open(filepath, 'w') as f:
                json.dump(recipe_data, f, indent=2)

            # Redirect to the new recipe's page
            return redirect(url_for('show_recipe', filename=filename))

        except Exception as e:
            # If anything goes wrong, show an error
            print(f"Error generating recipe: {e}")
            return "Sorry, there was an error generating the recipe. Please try again.", 500

    # For a GET request, just show the form
    return render_template('generate_recipe.html')


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

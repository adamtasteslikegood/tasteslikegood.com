import os
import json

import requests
from google import genai
from flask import Flask, render_template, abort, request, redirect, url_for
from jsonschema import Draft7Validator, ValidationError

# Initialize the Flask application
app = Flask(__name__)

# The folder where the recipe .json files are stored
RECIPES_DIR = 'recipes'
RECIPE_SCHEMA_PATH = 'recipe_schema.json'

# GitHub Actions configuration – override via environment variables
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_DEFAULT_OWNER = os.getenv("GITHUB_OWNER", "adamtasteslikegood")
GITHUB_DEFAULT_REPO = os.getenv("GITHUB_REPO", "tasteslikegood.com")

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

# --- NEW: Configure the generative model ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
client = None

if GOOGLE_API_KEY:
    client = genai.Client(api_key=GOOGLE_API_KEY)


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
        if client is None:
            return (
                "Recipe generation is not configured. Set the GOOGLE_API_KEY environment variable and restart the application.",
                500,
            )

        prompt = request.form.get('prompt', '').strip()
        if not prompt:
            return "A prompt describing the desired recipe is required.", 400

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

        recipe_json_str = ""
        response = None

        try:
            # Generate the content
            response = client.models.generate_content(
                model='gemini-2.5-pro',
                contents=full_prompt,
            )
            # Extract the JSON string from the response
            recipe_json_str = response.text.strip().replace('```json', '').replace('```', '').strip()

            # Parse the JSON string into a Python dictionary
            recipe_data = json.loads(recipe_json_str)

            # Validate the recipe before saving it
            validate_recipe_data(recipe_data)

            # Create a filename from the recipe name
            safe_filename = "".join(c for c in recipe_data['name'] if c.isalnum() or c in (' ', '_')).rstrip()
            filename = safe_filename.replace(' ', '_').lower() + '.json'
            filepath = os.path.join(RECIPES_DIR, filename)

            # Save the new recipe to a file
            with open(filepath, 'w') as f:
                json.dump(recipe_data, f, indent=2)

            # Redirect to the new recipe's page
            return redirect(url_for('show_recipe', filename=filename))

        except json.JSONDecodeError as e:
            # Handle JSON parsing errors
            error_message = f"JSON parsing error: {e}"
        except ValidationError as e:
            error_message = f"Schema validation error: {e.message}"
        except RuntimeError as e:
            error_message = str(e)
        except FileNotFoundError as e:
            # Handle file I/O errors
            error_message = f"File error: {e}"
        except Exception as e:
            # Handle all other unexpected errors
            error_message = f"Unexpected error: {e}"

        # Log the error details securely
        try:
            with open('recipe_error.json', 'a+') as f:
                f.write(f"{recipe_json_str}\n")
            with open('recipe_error.txt', 'a') as f:
                f.write(
                    f"Full prompt:\n{full_prompt}\n\n"
                    f"Response:\n{getattr(response, 'text', 'No response')}\n"
                    f"Error: {error_message}\n"
                )
        except Exception as logging_error:
            print(f"Error while logging: {logging_error}")

        # Show the error response to the user
        return "Sorry, there was an error generating the recipe. Please try again.", 500

    # For a GET request, just show the form
    return render_template('generate_recipe.html')


# Valid conclusion / status values surfaced by the GitHub API
WORKFLOW_CONCLUSION_VALUES = [
    'success', 'failure', 'cancelled', 'skipped',
    'timed_out', 'action_required', 'neutral', 'stale',
]
WORKFLOW_STATUS_VALUES = [
    'completed', 'in_progress', 'queued', 'requested', 'waiting', 'pending',
]
# Combined list shown to users in the filter UI
ALL_FILTER_VALUES = WORKFLOW_CONCLUSION_VALUES + [
    s for s in WORKFLOW_STATUS_VALUES if s not in WORKFLOW_CONCLUSION_VALUES
]


def fetch_workflow_runs(owner, repo, per_page=100):
    """Fetch workflow runs from the GitHub API for a given repository.

    Args:
        owner:    GitHub user or organisation that owns the repository.
        repo:     Repository name.
        per_page: Maximum number of runs to retrieve in a single API call
                  (1–100, default 100).

    Returns a tuple of (runs_list, error_message).  ``error_message`` is None
    on success or a human-readable string when the request fails.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    params = {"per_page": per_page}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 404:
            return [], f"Repository '{owner}/{repo}' not found or not accessible."
        if resp.status_code == 403:
            return [], "GitHub API rate limit exceeded or insufficient permissions. Set the GITHUB_TOKEN environment variable."
        if not resp.ok:
            return [], f"GitHub API error {resp.status_code}: {resp.text[:200]}"
        return resp.json().get("workflow_runs", []), None
    except requests.exceptions.ConnectionError:
        return [], "Could not connect to the GitHub API. Check your network connection."
    except requests.exceptions.Timeout:
        return [], "GitHub API request timed out. Please try again."
    except requests.exceptions.RequestException as exc:
        return [], f"Unexpected error contacting GitHub API: {exc}"


def apply_status_filter(runs, filter_status, filter_mode):
    """Filter *runs* by status/conclusion value.

    Args:
        runs:          List of workflow-run dicts returned by the GitHub API.
        filter_status: A single status/conclusion string to match against, or
                       an empty string to skip filtering.
        filter_mode:   ``'include'`` – keep only runs that match the status.
                       ``'exclude'`` – keep only runs that do NOT match the
                       status (i.e. the inverse set).

    Returns:
        Filtered list of run dicts.
    """
    if not filter_status:
        return runs

    def _matches(run):
        """Return True when the run's status *or* conclusion equals filter_status."""
        return (
            run.get("conclusion") == filter_status
            or run.get("status") == filter_status
        )

    if filter_mode == "exclude":
        return [r for r in runs if not _matches(r)]
    # Default: include mode
    return [r for r in runs if _matches(r)]


@app.route('/workflow_runs')
def workflow_runs():
    """Display GitHub Actions workflow runs with optional status filtering."""
    owner = request.args.get('owner', GITHUB_DEFAULT_OWNER).strip()
    repo = request.args.get('repo', GITHUB_DEFAULT_REPO).strip()
    filter_status = request.args.get('filter_status', '').strip()
    filter_mode = request.args.get('filter_mode', 'include').strip()

    # Sanitise filter_mode to one of the two accepted values
    if filter_mode not in ('include', 'exclude'):
        filter_mode = 'include'

    runs, error = fetch_workflow_runs(owner, repo)

    if error is None:
        runs = apply_status_filter(runs, filter_status, filter_mode)

    return render_template(
        'workflow_runs.html',
        runs=runs,
        error=error,
        owner=owner,
        repo=repo,
        filter_status=filter_status,
        filter_mode=filter_mode,
        all_filter_values=ALL_FILTER_VALUES,
    )


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)

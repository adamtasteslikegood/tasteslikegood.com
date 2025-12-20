# GitHub Copilot Instructions for tasteslikegood.com

## Project Overview

This is a Flask-based web application for viewing and generating vegan recipes. The application uses Google's Gemini AI to generate new recipes based on user prompts and validates them against a JSON schema.

## Technology Stack

- **Backend**: Python 3.x with Flask framework
- **AI Integration**: Google Generative AI (Gemini 2.5 Pro)
- **Validation**: JSON Schema (Draft 7) with jsonschema library
- **Frontend**: HTML templates with Jinja2, CSS, JavaScript

## Development Setup

### Prerequisites
- Python 3.x
- pip (Python package manager)
- Google API key for Gemini AI (set as `GOOGLE_API_KEY` environment variable)

### Installation Steps
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set up Google API key:
   ```bash
   export GOOGLE_API_KEY="your_google_api_key_here"
   ```

3. Run the development server:
   ```bash
   python app.py
   ```

4. Access the application at `http://localhost:5000`

## Code Style and Conventions

### Python
- Follow PEP 8 style guidelines
- Use meaningful variable names (e.g., `recipe_data`, `safe_filename`)
- Keep functions focused and single-purpose
- Document complex logic with clear comments
- Use type hints where appropriate for clarity

### Error Handling
- Catch specific exceptions rather than broad `Exception` catches
- Log errors appropriately (e.g., to `recipe_error.json` and `recipe_error.txt`)
- Provide user-friendly error messages while logging detailed errors separately
- Always validate user input before processing

### File Operations
- Always use `os.path.join()` for cross-platform path compatibility
- Check file existence with `os.path.exists()` before operations
- Handle JSON parsing errors gracefully with try-except blocks
- Use context managers (`with` statements) for file operations

## Project Structure

```
tasteslikegood.com/
├── app.py                    # Main Flask application
├── recipe_schema.json        # JSON schema for recipe validation
├── recipes/                  # Directory for stored recipe JSON files (gitignored)
├── templates/               # Jinja2 HTML templates
├── static/                  # CSS, JavaScript, and other static assets
├── tests/                   # Test files
│   └── test_recipe_validation.py
├── requirements.txt         # Python dependencies
└── README.md               # Project documentation
```

## Testing

### Running Tests
```bash
pytest tests/
```

### Test Conventions
- Use pytest as the testing framework
- Place test files in the `tests/` directory
- Prefix test files with `test_`
- Use fixtures for common test data (e.g., `base_recipe` fixture)
- Test both valid and invalid inputs
- Verify proper error handling with `pytest.raises()`

### Key Test Areas
- Recipe validation against JSON schema
- Both string and object instruction formats are supported
- Required field validation
- Error handling for malformed data

## Key Components

### Recipe Schema (`recipe_schema.json`)
- Defines the structure for recipe JSON files
- Uses JSON Schema Draft 7
- Supports flexible instruction formats (strings or objects with step numbers)
- Validates ingredient structure (wet, dry, other categories)
- Required fields: name, description, prepTime, cookTime, servings, ingredients, instructions

### Main Application (`app.py`)

#### Core Functions
- `get_all_recipes()`: Lists all recipes from the recipes directory
- `validate_recipe_data(recipe_data)`: Validates recipe JSON against schema
- `_load_recipe_schema()`: Loads and caches the recipe schema

#### Routes
- `/`: Homepage with recipe list
- `/recipe/<filename>`: Display individual recipe
- `/recipe/<filename>/json`: View raw JSON for a recipe
- `/generate_recipe`: Form and handler for AI recipe generation

### Recipe Generation
- Uses Gemini 2.5 Pro model
- Validates generated recipes before saving
- Creates safe filenames from recipe names
- Stores generation errors in log files for debugging
- Redirects to the new recipe page on success

## Important Notes

### Security
- Never commit API keys or secrets to the repository
- API keys should be set as environment variables
- The `.gitignore` excludes sensitive files like `.env` and `api_key.py`

### Recipe Storage
- Individual recipes are stored as JSON files in the `recipes/` directory
- The `recipes/` directory is gitignored (not version controlled)
- Filenames are generated from recipe names using safe characters only

### Error Logging
- Generation errors are logged to `recipe_error.json` and `recipe_error.txt`
- These log files are gitignored
- Include full prompt and response for debugging

## Common Workflows

### Adding a New Route
1. Define the route handler function with `@app.route()` decorator
2. Handle potential errors with try-except blocks
3. Return appropriate HTTP status codes
4. Create corresponding template in `templates/` if needed

### Modifying Recipe Schema
1. Update `recipe_schema.json`
2. Run existing tests to ensure backward compatibility
3. Update tests if schema changes affect validation logic
4. Update documentation if schema structure changes significantly

### Adding New Tests
1. Create test file in `tests/` directory (prefix with `test_`)
2. Import necessary modules and functions from `app.py`
3. Use fixtures for reusable test data
4. Test both success and failure cases
5. Run full test suite to ensure no regressions

## Dependencies

Key dependencies in `requirements.txt`:
- `Flask==3.1.1`: Web framework
- `google-genai`: Google Generative AI client
- `jsonschema==4.25.1`: JSON schema validation
- `pytest`: Testing framework

When adding new dependencies:
1. Add them to `requirements.txt` with specific version numbers
2. Ensure they're compatible with existing packages
3. Document any new environment variables or configuration needed

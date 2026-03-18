# Agent Configurations

This file tracks agent information and configurations used in the Tastes Like Good project.

## Agent Name: Gemini Recipe Generator
**Version:** 2.0 (using gemini-2.0-flash-exp as default)
**Description:** AI agent specialized in generating creative vegan recipes using Google's Gemini models. Supports multiple Gemini models dynamically fetched from the API.

### Parameters
| Parameter Name | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `model` | String | gemini-2.0-flash-exp | The specific Gemini model version to use |
| `temperature` | Float | 0.7 | Controls randomness in generation (0.0 to 1.0) |
| `max_output_tokens` | Integer | 8192 | Maximum number of tokens in the response |
| `prompt` | String | (required) | User prompt describing desired recipe (10-500 chars) |

### Dependencies
*   `google-genai` (Python SDK)
*   `google-generativeai` (Legacy SDK support)
*   `jsonschema` (for validation)
*   Recipe schema file (`recipe_schema.json`)

### Usage
```python
from services.gemini_service import generate_recipe_with_gemini

recipe_data = generate_recipe_with_gemini(
    prompt="Create a spicy Thai curry with tofu",
    model="gemini-2.0-flash-exp",
    credentials=user_credentials  # Optional, falls back to API key
)
```

### Testing
1.  Verify API connectivity through `/api/models` endpoint
2.  Test recipe generation with various prompts
3.  Validate schema compliance using `test_recipe_validation.py`
4.  Check normalization with `test_normalization.py`

### Notes
*   Automatically filters models to only those supporting `generateContent`
*   Implements dual authentication (user OAuth + API key fallback)
*   Normalizes output data to handle common AI generation errors

---

## Agent Name: Imagen Food Photographer
**Version:** 3.0 (Imagen 3)
**Description:** AI agent for generating high-quality food photography images based on recipe content using Google's Imagen 3 model.

### Parameters
| Parameter Name | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `recipe_name` | String | (required) | Name of the recipe for image context |
| `aspect_ratio` | String | 1:1 | Image aspect ratio (1:1, 4:3, 16:9, etc.) |
| `number_of_images` | Integer | 1 | Number of images to generate (1-8) |
| `negative_prompt` | String | None | Elements to avoid in the image |
| `person_generation` | String | dont_allow | Control person appearance in images |

### Dependencies
*   `google-genai` (Python SDK)
*   `PIL` (Python Imaging Library) for image processing
*   Async task handling for background generation

### Usage
```python
from services.image_service import generate_recipe_image

image_path = await generate_recipe_image(
    recipe_name="Spicy Thai Tofu Curry",
    recipe_data=recipe_dict
)
```

### Testing
1.  Test image generation with sample recipes
2.  Verify file storage in correct directory
3.  Check async task handling
4.  Validate image format and quality

### Notes
*   Images are generated asynchronously to avoid blocking recipe creation
*   Stored in `static/generated_images/` directory
*   Fallback to Unsplash stock images if generation fails
*   Regeneration available via `/api/regenerate_image/<filename>` endpoint

---

## Agent Name: Unsplash Stock Image Fetcher
**Version:** 1.0
**Description:** Agent for fetching high-quality stock food images from Unsplash as an immediate fallback for AI-generated images.

### Parameters
| Parameter Name | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `query` | String | (required) | Search query based on recipe name |
| `orientation` | String | landscape | Image orientation preference |
| `count` | Integer | 1 | Number of images to fetch |

### Dependencies
*   `requests` (HTTP library)
*   Unsplash API access key

### Usage
```python
from services.stock_image_service import get_stock_image_url

image_url = get_stock_image_url(recipe_name="Vegan Chocolate Cake")
```

### Testing
1.  Verify API key is configured
2.  Test search queries with various recipe names
3.  Check URL validation and fallback handling
4.  Validate image availability

### Notes
*   Provides immediate visual content while AI images generate
*   No storage required, uses direct Unsplash URLs
*   Includes photographer attribution per Unsplash guidelines
*   Gracefully handles API failures

---

## Agent Name: Recipe Validator
**Version:** 1.0 (JSON Schema Draft 7)
**Description:** Validation agent ensuring all recipes conform to the defined schema structure.

### Parameters
| Parameter Name | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `recipe_data` | Dict | (required) | Recipe data to validate |
| `schema_path` | String | recipe_schema.json | Path to schema file |
| `normalize` | Boolean | True | Apply normalization before validation |

### Dependencies
*   `jsonschema` (validation library)
*   `utils.normalization` (data normalization)

### Usage
```python
from validators.recipe_validator import validate_recipe

is_valid, errors = validate_recipe(recipe_data)
if not is_valid:
    print(f"Validation errors: {errors}")
```

### Testing
1.  Test with valid recipe data
2.  Test with missing required fields
3.  Test with invalid data types
4.  Test normalization edge cases

### Notes
*   Normalization handles common typos and variations
*   Provides detailed error messages for debugging
*   Used in both generation and migration workflows

---

## Integration Notes

All agents work together in the recipe generation workflow:

1. **User Request** → Gemini Recipe Generator creates recipe
2. **Recipe Created** → Recipe Validator ensures data quality
3. **Recipe Valid** → Unsplash fetches immediate stock image
4. **Background Task** → Imagen generates custom AI image
5. **Both Images** → Displayed side-by-side in UI

This architecture ensures fast user experience with progressive enhancement.

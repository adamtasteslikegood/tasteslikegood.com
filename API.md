# API Documentation

## Authentication (`/auth`)

The authentication module uses Google OAuth 2.0 to log users in and out.

### GET `/auth/login`
Initiates the OAuth 2.0 flow. Redirects the user to Google's consent screen.

**Response:**
- `302 Found`: Redirects to Google.

### GET `/auth/callback`
Handles the callback from Google. Exchanges the authorization code for an access token and retrieves user information.

**Query Parameters:**
- `code`: The authorization code returned by Google.
- `state`: The state token to prevent CSRF.

**Response:**
- `302 Found`: Redirects to the homepage on success.
- `400 Bad Request`: If the state parameter is missing or invalid.
- `500 Internal Server Error`: If authentication fails.

### GET `/auth/logout`
Logs the user out by clearing the session.

**Response:**
- `302 Found`: Redirects to the homepage.

---

## Recipes

### GET `/`
The homepage. Lists all available recipes.

**Response:**
- `200 OK`: HTML page with a list of recipes.

### GET `/generate_recipe`
Displays the recipe generation form (GET) or processes a generation request (POST).

#### GET
**Response:**
- `200 OK`: HTML form.

#### POST
**Form Data:**
- `prompt` (string): A description of the recipe to generate. Must be between 10 and 500 characters.

**Response:**
- `302 Found`: Redirects to the generated recipe's page on success.
- `400 Bad Request`: If the prompt is missing or invalid.
- `500 Internal Server Error`: If recipe generation fails or the service is unconfigured.

### GET `/recipe/<filename>`
Displays a specific recipe.

**Path Parameters:**
- `filename`: The filename of the recipe JSON file (e.g., `vegan_cookies.json`).

**Response:**
- `200 OK`: HTML page for the recipe.
- `404 Not Found`: If the recipe does not exist.
- `500 Internal Server Error`: If the recipe file is corrupt.

### GET `/recipe/<filename>/json`
Displays or returns the raw JSON for a recipe.

**Path Parameters:**
- `filename`: The filename of the recipe JSON file.

**Query Parameters:**
- `raw` (optional): If set to `true`, returns the raw JSON content.

**Response:**
- `200 OK`: HTML viewer (default) or JSON content (if `raw=true`).
- `404 Not Found`: If the recipe does not exist.
- `500 Internal Server Error`: If the recipe file is corrupt.

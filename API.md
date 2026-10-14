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

---

## Public API description

### GET `/api/public/openapi.json`
An OpenAPI 3.1 description of what a caller with no account may use:
`/api/public/newest-recipes`, `/api/recipes/public/<slug>`, and the agent
tools that read published recipes. The tool entries are built from
`services/agent_tools.py` on each request, so a tool that is switched off or
needs sign-in is not described.

**Response:**
- `200 OK`: the OpenAPI document.

---

## Agent tools (`/api/agent`)

Tools for AI agents. Three switches decide which tools are offered:
`AGENT_TOOLS_READ` (default on), `AGENT_TOOLS_GENERATE_TEXT` and
`AGENT_TOOLS_GENERATE_IMAGE` (both default off). The tool definitions and the
access check are in `services/agent_tools.py`.

### GET `/api/agent/tools`
Lists the tools that are switched on.

**Response:**
- `200 OK`: `{"tools": [{"name", "description", "inputSchema", "requiresSignIn"}], "attribution"}`.

### POST `/api/agent/tools/<name>`
Calls a tool. The body is a JSON object of the tool's arguments.

| Tool | Switch | Sign-in | Arguments |
| ---- | ------ | ------- | --------- |
| `search_recipes` | read | no | `query` (optional, at most 100 characters), `limit` (1 to 20, default 10) |
| `get_recipe` | read | no | `slug` |
| `generate_recipe` | text | yes | `prompt` |
| `generate_recipe_image` | image | yes | `recipe_id` |
| `get_generation_status` | text or image | yes | `recipe_id` |

Generation is asynchronous: the tool returns `202` with a `recipe_id`, and the
caller polls `get_generation_status`. A generated recipe is saved to the
signed-in account. Results carry the recipe's URL and an attribution line.

`get_generation_status` answers for any recipe the signed-in account owns. It
always returns `status` and `image_url`. It returns `recipe` only when the text
switch is on, and then with the fields a published recipe shows, not the
stored row.

`generate_recipe` makes the text only. It does not queue a photo, whatever the
image switch says; a photo is a separate `generate_recipe_image` call.

The Express proxy holds every `generate_*` tool to the same rate limit and
recipe write pause as `/api/generate`.

**Response:**
- `200 OK` or `202 Accepted`: the tool's result.
- `400 Bad Request`: the body is present but is not a JSON object, or an argument is missing or invalid. An empty body means no arguments.
- `401 Unauthorized`: the tool needs a signed-in account.
- `404 Not Found`: the tool does not exist or is switched off, or the recipe was not found.
- `413 Content Too Large`: the body is over 10 KB.
- `415 Unsupported Media Type`: the body is not sent as `application/json`.

---

## MCP endpoint

### POST `/mcp`
A Model Context Protocol server over the published recipes (Streamable HTTP,
protocol versions `2025-06-18` and `2025-03-26`). One JSON-RPC message per
request, one JSON response; no session and no event stream, so `GET` and
`DELETE` answer `405`.

Methods: `initialize`, `ping`, `tools/list`, `tools/call`. Notifications are
accepted with `202`.

The tools are the agent tools above that need no sign-in (`search_recipes`,
`get_recipe`), called through the same access check. The site session is not
read here, so a tool that needs an account is not listed and cannot be called,
whatever the generation switches say. With `AGENT_TOOLS_READ` off the list is
empty.

A request with an `Origin` header from another site is refused with `403`.


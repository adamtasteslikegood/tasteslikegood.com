# Recipe Viewer

## Description

This project is a simple web application designed to display recipes. It provides a user-friendly interface to view recipe details and the underlying JSON data that represents them.

## Features

- **AI Recipe Generation**: Uses Google's Gemini models to create unique vegan recipes.
- **Smart Image Strategy**:
  - **Stock Images**: Automatically finds high-quality Unsplash images for recipes.
  - **AI Images**: Asynchronously generates custom food photography using Imagen 3.
  - **One-Two Punch**: Displays both stock and AI images side-by-side.
- **Metadata & Feedback**:
  - Tracks AI model, prompt, and timestamp for every recipe.
  - Users can **Regenerate** AI images or **Report** issues directly from the UI.
- **Robust JSON Schema**: Ensures consistent data structure for all recipes.

## API Endpoints

- `POST /generate_recipe`: Generates a new recipe.
- `GET /api/models`: Lists available Gemini models.
- `POST /api/generate_image/<filename>`: Triggers async AI image generation.
- `POST /api/regenerate_image/<filename>`: Forces regeneration of the AI image.
- `POST /api/report_recipe/<filename>`: Logs user reports.
- `POST /api/migrate`: Migrates old recipe JSONs to the latest schema.

* **Code Viewer with Copy Functionality:** The JSON data is presented in a clean, readable format with a dedicated "Copy" button to easily copy the data to the clipboard.
- **Simple Navigation:** Users can easily navigate back and forth between the recipe list, the recipe detail view, and the JSON view.

## How to Use

1. Run the application.
2. From the main page, click on a recipe to view its details.
3. From the recipe detail page, you can choose to view the associated JSON data.
4. In the JSON view, you can use the "Copy" button to copy the data.

## Technologies

- **Backend:** Python (likely with a web framework like Flask)
- **Frontend:** HTML, CSS, JavaScript

## Getting Started

1. Create and activate a Python virtual environment (optional but recommended).
2. Install the dependencies:

    ```bash
    pip install -r requirements.txt
    ```

3. Export your Google API key so the Gemini client can authenticate:

    ```bash
    export GOOGLE_API_KEY="your_google_api_key_here"
    ```

4. Start the Flask development server:

    ```bash
    python app.py
    ```

5. Open your browser to `http://localhost:5000` to view the recipes or generate a new one.

---
description: Fix Recipe App Issues (Multi-Agent)
---

This workflow outlines a multi-agent approach to fixing and optimizing the recipe application.

1. **Schema Agent**:
    - Analyze `recipe_schema.json`.
    - Ensure all optional fields allow `null`.
    - Verify `app.py` validation logic.

2. **Migration Agent**:
    - Scan all files in `recipes/`.
    - Identify files matching old schema or "Untitled Recipe".
    - Apply `migrate_recipe_data` to fix structure and add metadata.
    - // turbo
    - Run `curl -X POST http://localhost:5000/api/migrate` to trigger batch migration.

3. **Frontend Agent**:
    - Review `templates/recipe.html` and `static/css/index.css`.
    - Ensure `.recipe-images` grid handles 0, 1, or 2 images gracefully.
    - Verify `onerror` handlers for images.

4. **Testing Agent**:
    - Generate a new recipe to verify the fix.
    - View an old recipe to verify auto-migration.

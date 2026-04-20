## DEV TEST QUICK START 

1. Setup the Environment
  If you haven't already, activate the virtual environment and ensure dependencies are installed:

   1 cd Backend
   2 source .venv/bin/activate
   3 pip install -r requirements.txt

  2. Run the Flask Server
  Start the backend in debug mode:

   1 python app.py
  The server will typically start on http://127.0.0.1:5000.

  3. What to Test in Your Browser
  You can now visit these URLs directly to see the new design and functionality:

   * Landing Page: http://127.0.0.1:5000/ (High-contrast "Zero Fluff" hero)
   * Public Browse: http://127.0.0.1:5000/browse (The new recipe grid)
   * Sitemap: http://127.0.0.1:5000/sitemap.xml (The SEO XML feed)
   * Public Recipe: To view a specific recipe, you'll need its slug. You can find one in the /browse list or check the database. If you have a recipe with slug
     vegan-chili, visit:
      http://127.0.0.1:5000/r/vegan-chili

  4. Note on the "My Kitchen" Links
  The "My Kitchen" and "Save to Cookbook" buttons in the templates are designed to bridge back to the Angular SPA. If the SPA isn't running on port 8080 (or through
  the Express proxy), those specific links will likely show a 404 or connection error, but the SSR content (ingredients, instructions, SEO tags) will all work
  perfectly.

▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
 ## with uv?                                                                                                                                                            
▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄


  1. Initialize and Install Dependencies
  Run this from the Backend directory to create a virtual environment and install everything lightning-fast:

   1 cd Backend
   2 uv sync

  2. Run the Backend
  You can then launch the app using uv run:
   1 uv run python app.py

  3. Verification
  Once running, you can hit the new SSR routes directly:
   * Home: http://127.0.0.1:5000/
   * Browse: http://127.0.0.1:5000/browse
   * Sitemap: http://127.0.0.1:5000/sitemap.xml

  If you need to check which recipes are public to test a specific slug, you can run a quick check with uv run:

   1 uv run python -c "from app import create_app; from models.recipe import Recipe; app=create_app(); [print(r.slug) for r in
     Recipe.query.filter_by(is_public=True).all()]"



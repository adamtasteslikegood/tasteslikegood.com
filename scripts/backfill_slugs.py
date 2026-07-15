import sys
import re
from pathlib import Path
from sqlalchemy.exc import IntegrityError

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app
from extensions import db
from models.recipe import Recipe


def generate_slug(text):
    """Generate a clean URL slug from a title."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = re.sub(r"^-+|-+$", "", text)
    return text or "recipe"


def run_backfill(app):
    with app.app_context():
        recipes = Recipe.query.filter(Recipe.slug.is_(None)).all()
        print(f"Found {len(recipes)} recipes without slugs.")

        success_count = 0
        for recipe in recipes:
            base_slug = generate_slug(recipe.name)
            slug = base_slug
            suffix = 1

            while True:
                recipe.slug = slug
                try:
                    db.session.commit()
                    print(f"Backfilled slug '{slug}' for recipe '{recipe.name}'")
                    success_count += 1
                    break
                except IntegrityError:
                    db.session.rollback()
                    suffix += 1
                    slug = f"{base_slug}-{suffix}"

        print(f"Successfully backfilled {success_count} slugs.")


if __name__ == "__main__":
    app = create_app()
    run_backfill(app)

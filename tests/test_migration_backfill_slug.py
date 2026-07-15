import sys
from pathlib import Path
import pytest
from sqlalchemy.exc import IntegrityError

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import create_app
from extensions import db
from models.recipe import Recipe
from scripts.backfill_slugs import generate_slug, run_backfill


@pytest.fixture
def app():
    app = create_app(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
    )

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


def test_generate_slug():
    assert generate_slug("Thai Peanut Noodles!") == "thai-peanut-noodles"
    assert generate_slug("  Spaces   And-Underscores_  ") == "spaces-and-underscores"
    assert generate_slug("???") == "recipe"
    assert generate_slug("Crème brûlée") == "creme-brulee"


def test_backfill_slugs_retry_loop(app):
    with app.app_context():
        # Create multiple recipes with the same name to force collisions
        r1 = Recipe(id="1", name="Vegan Chili", data={})
        r2 = Recipe(id="2", name="Vegan Chili", data={})
        r3 = Recipe(id="3", name="Vegan Chili", data={})

        db.session.add_all([r1, r2, r3])
        db.session.commit()

        # Run backfill
        run_backfill(app)

        # Verify slugs were generated correctly with suffixes
        recipes = Recipe.query.filter(Recipe.name == "Vegan Chili").order_by(Recipe.slug).all()
        assert len(recipes) == 3

        slugs = [r.slug for r in recipes]
        assert "vegan-chili" in slugs
        assert "vegan-chili-2" in slugs
        assert "vegan-chili-3" in slugs


def test_status_migration_preserves_server_side_sessions():
    migration = (
        Path(__file__).resolve().parent.parent
        / "migrations/versions/03da1e46c9a5_add_status_to_recipe.py"
    ).read_text(encoding="utf-8")

    assert 'drop_table("flask_sessions")' not in migration
    assert 'create_table(\n        "flask_sessions"' not in migration

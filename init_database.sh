#!/bin/bash
# Database initialization script for Phase 3
# Run this to set up the database for the first time

set -e  # Exit on error

echo "🗄️  Tastes Like Good - Database Setup"
echo "===================================="
echo ""

# Check if we're in the Backend directory
if [ ! -f "app.py" ]; then
    echo "❌ Error: Please run this script from the Backend/ directory"
    echo "   cd Backend && ./init_database.sh"
    exit 1
fi

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "❌ Error: uv is not installed"
    echo "   Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "   Or: pip install uv"
    exit 1
fi

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "⚠️  No .env file found. Creating from .env.example..."
    cp .env.example .env
    echo "✅ Created .env file"
    echo "⚠️  Please edit .env and set your DATABASE_URL if needed"
    echo ""
fi

# Sync dependencies with uv (creates venv automatically if needed)
echo "📦 Syncing dependencies with uv..."
# Remove old venv to ensure fresh install with updated dependencies
if [ -d ".venv" ]; then
    echo "   Removing old virtual environment to install updated dependencies..."
    rm -rf .venv
fi
uv sync
echo "✅ Dependencies synced"
echo ""

# Set Flask app
export FLASK_APP=app.py

echo "1️⃣  Initializing Flask-Migrate..."
if [ -d "migrations" ]; then
    echo "   ⚠️  migrations/ directory already exists. Skipping init."
else
    uv run flask db init
    echo "   ✅ Flask-Migrate initialized"
fi
echo ""

echo "2️⃣  Creating database migration..."
uv run flask db migrate -m "Add User and Recipe models with timestamps"
echo "   ✅ Migration created"
echo ""

echo "3️⃣  Applying migration (creating tables)..."
uv run flask db upgrade
echo "   ✅ Tables created"
echo ""

echo "4️⃣  Verifying database connection..."
uv run python -c "
from app import create_app
from extensions import db
from sqlalchemy import text

app = create_app()
with app.app_context():
    try:
        result = db.session.execute(text('SELECT 1')).scalar()
        print('   ✅ Database connection successful!')

        # Count existing records
        from models import User, Recipe
        user_count = User.query.count()
        recipe_count = Recipe.query.count()
        print(f'   📊 Current data: {user_count} users, {recipe_count} recipes')
    except Exception as e:
        print(f'   ❌ Database error: {e}')
        exit(1)
"
echo ""

echo "🎉 Database setup complete!"
echo ""
echo "Next steps:"
echo "  1. Migrate existing recipes: uv run python scripts/migrate_recipes_to_db.py"
echo "  2. Start the backend: uv run python app.py"
echo "  3. Test the API: curl http://localhost:5000/api/recipes"
echo ""
echo "Remember: All Python commands should be prefixed with 'uv run'"
echo "For more info, see: DATABASE_SETUP.md"

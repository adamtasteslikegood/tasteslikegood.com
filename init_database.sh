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

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "⚠️  No .env file found. Creating from .env.example..."
    cp .env.example .env
    echo "✅ Created .env file"
    echo "⚠️  Please edit .env and set your DATABASE_URL if needed"
    echo ""
fi

# Check if Flask is installed
if ! command -v flask &> /dev/null; then
    echo "❌ Flask CLI not found. Installing dependencies..."
    pip install -r requirements.txt
fi

# Set Flask app
export FLASK_APP=app.py

echo "1️⃣  Initializing Flask-Migrate..."
if [ -d "migrations" ]; then
    echo "   ⚠️  migrations/ directory already exists. Skipping init."
else
    flask db init
    echo "   ✅ Flask-Migrate initialized"
fi
echo ""

echo "2️⃣  Creating database migration..."
flask db migrate -m "Add User and Recipe models with timestamps"
echo "   ✅ Migration created"
echo ""

echo "3️⃣  Applying migration (creating tables)..."
flask db upgrade
echo "   ✅ Tables created"
echo ""

echo "4️⃣  Verifying database connection..."
python -c "
from app import create_app
from extensions import db

app = create_app()
with app.app_context():
    try:
        result = db.session.execute('SELECT 1').scalar()
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
echo "  1. Migrate existing recipes: python scripts/migrate_recipes_to_db.py"
echo "  2. Start the backend: python app.py"
echo "  3. Test the API: curl http://localhost:5000/api/recipes"
echo ""
echo "For more info, see: DATABASE_SETUP.md"

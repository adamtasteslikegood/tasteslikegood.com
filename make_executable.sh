#!/bin/bash
# Quick helper to make all scripts executable

cd "$(dirname "$0")"

echo "Making scripts executable..."
chmod +x init_database.sh
chmod +x setup_venv.sh
chmod +x scripts/migrate_recipes_to_db.py

echo "✅ All scripts are now executable"
echo ""
echo "Available scripts:"
echo "  ./setup_venv.sh          - Create virtual environment only"
echo "  ./init_database.sh       - Full database setup (creates venv if needed)"
echo "  scripts/migrate_recipes_to_db.py - Migrate existing recipes to database"

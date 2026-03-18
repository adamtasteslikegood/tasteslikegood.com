#!/bin/bash
# Complete fix procedure for recipe ID inconsistency

echo "========================================================================"
echo "Recipe ID Fix - Complete Procedure"
echo "========================================================================"
echo ""

cd "$(dirname "$0")/.."

echo "Step 1: Show current database state"
echo "------------------------------------------------------------------------"
uv run python scripts/debug_recipes.py
echo ""

read -p "Press Enter to continue to Step 2..."
echo ""

echo "Step 2: Run the migration fix"
echo "------------------------------------------------------------------------"
uv run python scripts/fix_recipe_ids.py
echo ""

read -p "Press Enter to continue to Step 3..."
echo ""

echo "Step 3: Show database state after fix"
echo "------------------------------------------------------------------------"
uv run python scripts/debug_recipes.py
echo ""

read -p "Press Enter to continue to Step 4..."
echo ""

echo "Step 4: Verify the fix"
echo "------------------------------------------------------------------------"
uv run python scripts/verify_recipe_ids.py
echo ""

echo "========================================================================"
echo "Fix procedure complete!"
echo "========================================================================"
echo ""
echo "If recipes are still inconsistent, the issue may be with how the data"
echo "was originally stored. You may need to manually inspect the database."
echo ""

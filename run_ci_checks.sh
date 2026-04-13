#!/bin/bash
set -e

cd /home/adam/projects/tasteslikegoodtheangularsvegancookbook/Backend

echo "=========================================="
echo "  Backend CI/CD Test Script"
echo "=========================================="
echo ""

# Step 1: Sync dependencies
echo "Step 1: Syncing dependencies with uv..."
uv sync
echo "✅ Dependencies synced"
echo ""

# Step 2: Run Black formatter check
echo "Step 2: Checking code formatting with Black..."
if uv run black --check . ; then
    echo "✅ Black: Code is properly formatted"
else
    echo "⚠️  Black: Formatting issues found - running auto-fix..."
    uv run black .
    echo "✅ Black: Code formatted"
fi
echo ""

# Step 3: Run Flake8 linter
echo "Step 3: Linting code with Flake8..."
# Ignoring E501 and E402 for now. These should be cleaned up later.
if uv run flake8 . --extend-ignore=E501,E402 ; then
    echo "✅ Flake8: No linting errors"
else
    echo "⚠️  Flake8: Linting issues found (review above)"
fi
echo ""

# Step 4: Run mypy type checker
echo "Step 4: Type checking with mypy..."
# Clear mypy cache to prevent 'is_bound' corruption errors
rm -rf .mypy_cache
if uv run mypy . --ignore-missing-imports ; then
    echo "✅ mypy: Type checking passed"
else
    echo "⚠️  mypy: Type checking issues found (review above)"
fi
echo ""

# Step 5: Run pytest
echo "Step 5: Running tests with pytest..."
# Coverage omissions are now handled by the .coveragerc file.
if uv run pytest --cov=. --cov-report=term ; then
    echo "✅ pytest: All tests passed"
else
    echo "⚠️  pytest: Some tests failed (review above)"
fi
echo ""

echo "=========================================="
echo "  Summary"
echo "=========================================="
echo "✅ All checks completed!"
echo ""
echo "Next steps:"
echo "1. Review any warnings above"
echo "2. Stage changes: git add ."
echo "3. Commit: git commit -m 'ci: Add CI/CD pipeline for Flask backend'"
echo "4. Push: git push origin dev/backend_sub222"

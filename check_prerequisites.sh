#!/bin/bash
# Quick check if environment is ready for Phase 3 setup

echo "🔍 Checking Phase 3 Prerequisites"
echo "=================================="
echo ""

# Check 1: Are we in Backend directory?
if [ -f "app.py" ]; then
    echo "✅ In Backend directory"
else
    echo "❌ Not in Backend directory"
    echo "   Run: cd Backend"
    exit 1
fi

# Check 2: Is uv installed?
if command -v uv &> /dev/null; then
    UV_VERSION=$(uv --version)
    echo "✅ uv is installed ($UV_VERSION)"
else
    echo "❌ uv is not installed"
    echo "   Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "   Or on Arch: yay -S uv"
    exit 1
fi

# Check 3: Does pyproject.toml exist?
if [ -f "pyproject.toml" ]; then
    echo "✅ pyproject.toml exists"
else
    echo "❌ pyproject.toml not found"
    exit 1
fi

# Check 4: Does .env exist?
if [ -f ".env" ]; then
    echo "✅ .env file exists"
else
    echo "⚠️  .env file not found (will be created from .env.example)"
fi

# Check 5: Is database already initialized?
if [ -d "migrations" ]; then
    echo "⚠️  migrations/ directory exists (database may be already initialized)"
else
    echo "✅ migrations/ not found (ready for fresh setup)"
fi

if [ -f "tasteslikegood.db" ]; then
    echo "⚠️  tasteslikegood.db exists (database already created)"
else
    echo "✅ tasteslikegood.db not found (ready for fresh setup)"
fi

echo ""
echo "=================================="
echo "✅ Prerequisites check complete!"
echo ""
echo "Ready to run:"
echo "  ./init_database.sh"

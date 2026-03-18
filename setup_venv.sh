#!/bin/bash
# Setup environment for Backend using uv
# Run this first before init_database.sh if you prefer

set -e

echo "📦 Setting up Python environment with uv..."
echo ""

cd "$(dirname "$0")"

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    echo "❌ Error: uv is not installed"
    echo ""
    echo "Install uv with one of these methods:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  pip install uv"
    echo "  pacman -S uv  # On Arch Linux"
    echo ""
    exit 1
fi

if [ -d ".venv" ]; then
    echo "⚠️  Virtual environment already exists at .venv"
    read -p "Do you want to recreate it? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "🗑️  Removing existing virtual environment..."
        rm -rf .venv
    else
        echo "✅ Using existing virtual environment"
        echo ""
        echo "To sync dependencies: uv sync"
        exit 0
    fi
fi

echo "🔨 Creating virtual environment and syncing dependencies..."
uv sync

echo "✅ Environment setup complete!"
echo ""
echo "uv automatically manages the virtual environment."
echo "Just use 'uv run' to execute commands:"
echo "  uv run python app.py"
echo "  uv run flask db migrate"
echo ""
echo "Or run the full setup:"
echo "  ./init_database.sh"

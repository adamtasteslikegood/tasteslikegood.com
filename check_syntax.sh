#!/bin/bash
# Quick syntax check before running full setup

echo "🔍 Checking Python syntax..."
echo ""

if uv run python -m py_compile app.py blueprints/*.py models/*.py 2>&1; then
    echo "✅ All Python files have valid syntax"
    exit 0
else
    echo "❌ Syntax error found"
    exit 1
fi

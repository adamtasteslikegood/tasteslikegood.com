#!/bin/sh
set -e

echo "Running database migrations..."
flask db upgrade || echo "Warning: migrations failed (table may already exist)"

echo "Starting application..."
exec python app.py

# Database Setup Guide - Phase 3

This guide walks you through setting up the database layer for persistent recipe storage.

---

## Quick Start (SQLite - Development)

The fastest way to get started is with SQLite (no separate database server needed):

```bash
cd Backend

# 1. Set up environment
cp .env.example .env
# Edit .env - DATABASE_URL should be: sqlite:///tasteslikegood.db

# 2. Install dependencies (if not already done)
pip install -r requirements.txt

# 3. Initialize database migrations
export FLASK_APP=app.py
flask db init

# 4. Create initial migration
flask db migrate -m "Add User and Recipe models with timestamps"

# 5. Apply migration (create tables)
flask db upgrade

# 6. Verify database was created
ls -lh tasteslikegood.db

# 7. Start the Flask backend
python app.py
```

Your database is now ready! The SQLite file `tasteslikegood.db` will be created in the `Backend/` directory.

---

## PostgreSQL Setup (Production)

For production deployment or if you want a more robust database:

### 1. Install PostgreSQL

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install postgresql postgresql-contrib
```

**macOS (Homebrew):**
```bash
brew install postgresql
brew services start postgresql
```

**Docker:**
```bash
docker run --name tasteslikegood-db \
  -e POSTGRES_PASSWORD=yourpassword \
  -e POSTGRES_DB=tasteslikegood \
  -p 5432:5432 \
  -d postgres:15
```

### 2. Create Database

```bash
# Connect to PostgreSQL
psql -U postgres

# Create database and user
CREATE DATABASE tasteslikegood;
CREATE USER tasteslikegood_user WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE tasteslikegood TO tasteslikegood_user;
\q
```

### 3. Configure Environment

```bash
cd Backend
cp .env.example .env
```

Edit `.env` and set:
```bash
DATABASE_URL=postgresql://tasteslikegood_user:your_secure_password@localhost:5432/tasteslikegood
```

### 4. Initialize Database

```bash
export FLASK_APP=app.py
flask db init
flask db migrate -m "Add User and Recipe models with timestamps"
flask db upgrade
```

### 5. Verify Connection

```bash
python -c "
from app import create_app
from extensions import db

app = create_app()
with app.app_context():
    db.session.execute('SELECT 1')
    print('✓ Database connection successful!')
"
```

---

## Migrate Existing Recipes

If you have existing recipes stored as JSON files in `recipes/`, import them:

```bash
cd Backend

# Dry run (preview what will be migrated)
python scripts/migrate_recipes_to_db.py --dry-run

# Actual migration (as anonymous recipes)
python scripts/migrate_recipes_to_db.py

# Or assign to a specific user
python scripts/migrate_recipes_to_db.py --user-id 1
```

---

## Database Schema

### User Table
```sql
CREATE TABLE user (
    id INTEGER PRIMARY KEY,
    email VARCHAR(120) UNIQUE NOT NULL,
    name VARCHAR(100),
    google_id VARCHAR(100) UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Recipe Table
```sql
CREATE TABLE recipe (
    id VARCHAR(36) PRIMARY KEY,
    user_id INTEGER REFERENCES user(id),
    name VARCHAR(200) NOT NULL,
    data JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## Common Commands

### Create a New Migration
After modifying models in `models/`:
```bash
flask db migrate -m "Description of changes"
flask db upgrade
```

### Rollback Migration
```bash
flask db downgrade
```

### View Migration History
```bash
flask db history
```

### Reset Database (Development Only)
```bash
# SQLite
rm tasteslikegood.db
rm -rf migrations/
flask db init
flask db migrate -m "Initial schema"
flask db upgrade

# PostgreSQL
psql -U postgres -d tasteslikegood -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
flask db upgrade
```

---

## Environment Variables

Add to `Backend/.env`:

```bash
# Required
DATABASE_URL=sqlite:///tasteslikegood.db  # or postgresql://...

# Optional
SQLALCHEMY_ECHO=True  # Log all SQL queries (debugging)
```

---

## Testing the API

### Create a Recipe
```bash
curl -X POST http://localhost:5000/api/recipes \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Recipe",
    "ingredients": ["flour", "water"],
    "instructions": ["Mix", "Bake"]
  }'
```

### List Recipes
```bash
curl http://localhost:5000/api/recipes
```

### Get Recipe by ID
```bash
curl http://localhost:5000/api/recipes/<recipe-id>
```

### Update Recipe
```bash
curl -X PUT http://localhost:5000/api/recipes/<recipe-id> \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Updated Recipe",
    "ingredients": ["new", "ingredients"]
  }'
```

### Delete Recipe
```bash
curl -X DELETE http://localhost:5000/api/recipes/<recipe-id>
```

---

## Troubleshooting

### "flask: command not found"
```bash
pip install flask
# OR use:
python -m flask db init
```

### "No module named 'flask_migrate'"
```bash
pip install flask-migrate
```

### "Could not locate a Flask application"
```bash
export FLASK_APP=app.py
# OR add to your .bashrc/.zshrc
```

### "OperationalError: no such table"
Run migrations:
```bash
flask db upgrade
```

### "Can't locate revision identified by"
Reset migrations:
```bash
rm -rf migrations/
flask db init
flask db migrate -m "Initial schema"
flask db upgrade
```

### PostgreSQL Connection Refused
Check if PostgreSQL is running:
```bash
# Ubuntu/Debian
sudo systemctl status postgresql

# macOS
brew services list

# Start if not running
sudo systemctl start postgresql  # Linux
brew services start postgresql   # macOS
```

### Permission Denied on tasteslikegood.db
```bash
chmod 664 tasteslikegood.db
# Make sure your user owns the file
```

---

## Cloud Deployment

### Heroku
```bash
# Heroku automatically provides DATABASE_URL
heroku addons:create heroku-postgresql:mini

# Run migrations
heroku run flask db upgrade
```

### Google Cloud Run
Add Cloud SQL instance and set:
```bash
DATABASE_URL=postgresql://user:pass@/dbname?host=/cloudsql/PROJECT:REGION:INSTANCE
```

### Railway
Railway auto-detects PostgreSQL. Just add the DATABASE_URL from your Railway dashboard.

---

## Next Steps

After database setup:
1. ✅ Backend can persist recipes
2. ⏳ Update Angular frontend to use `/api/recipes` endpoints (see Phase 3 docs)
3. ⏳ Implement recipe sync on login
4. ⏳ Add recipe collections/cookbooks (Phase 4)

---

## Support

- [Flask-SQLAlchemy Docs](https://flask-sqlalchemy.palletsprojects.com/)
- [Flask-Migrate Docs](https://flask-migrate.readthedocs.io/)
- [SQLAlchemy Docs](https://docs.sqlalchemy.org/)
- [PostgreSQL Docs](https://www.postgresql.org/docs/)

---

**Last Updated**: March 1, 2026

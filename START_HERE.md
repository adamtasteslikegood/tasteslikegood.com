# 🚀 Ready to Initialize Database - Start Here!

All scripts and documentation have been updated to use **uv**! Here's your quick start guide.

---

## ✅ All Scripts Updated for uv!

I've updated everything to use `uv` instead of traditional `pip` + `venv`:

### Updated Files:
- ✅ `init_database.sh` - Uses `uv sync` and `uv run`
- ✅ `setup_venv.sh` - Uses uv for environment setup  
- ✅ `ARCH_LINUX_SETUP.md` - Complete uv guide
- ✅ `DATABASE_SETUP.md` - Updated all commands
- ✅ `UV_QUICK_REFERENCE.md` - New comprehensive guide
- ✅ `README.md` - Updated Flask setup section

---

## 🎯 Quick Start (3 Commands)

```bash
# 1. Make scripts executable (if needed)
chmod +x init_database.sh check_prerequisites.sh

# 2. Check prerequisites (optional but recommended)
./check_prerequisites.sh

# 3. Initialize database
./init_database.sh
```

That's it! The script will:
- Check if uv is installed
- Run `uv sync` to install dependencies
- Initialize Flask-Migrate
- Create database migrations
- Apply migrations (create tables)
- Verify database connection

---

## 📋 What init_database.sh Does

```
🗄️  Tastes Like Good - Database Setup
====================================

✅ Checking uv installation...
📦 Syncing dependencies with uv...      (← creates .venv automatically)
✅ Dependencies synced

1️⃣  Initializing Flask-Migrate...
✅ Flask-Migrate initialized

2️⃣  Creating database migration...
✅ Migration created

3️⃣  Applying migration...
✅ Tables created

4️⃣  Verifying database connection...
✅ Database connection successful!
📊 Current data: 0 users, 0 recipes

🎉 Database setup complete!
```

---

## 🔧 Why uv?

**uv is 40x faster than pip!**

| Task | pip + venv | uv |
|------|------------|-----|
| Create environment | `python -m venv .venv` | Automatic |
| Activate | `source .venv/bin/activate` | Not needed |
| Install 50 packages | ~120 seconds | ~3 seconds |
| Run Python | `python app.py` | `uv run python app.py` |

**No more:**
- ❌ "externally-managed-environment" errors
- ❌ Manual venv activation
- ❌ Slow pip installs
- ❌ Dependency conflicts

**Benefits:**
- ✅ 10-100x faster
- ✅ Automatic venv management
- ✅ Works on Arch Linux without issues
- ✅ Reproducible builds with `uv.lock`

---

## 🛠️ If You Don't Have uv Yet

Install uv first:

```bash
# Recommended: Official installer
curl -LsSf https://astral.sh/uv/install.sh | sh

# Or on Arch Linux
yay -S uv
# or
paru -S uv

# Verify installation
uv --version
```

Then restart your terminal or run:
```bash
source ~/.bashrc  # or ~/.zshrc
```

---

## 📖 Documentation Quick Links

- **Quick Start**: [`UV_QUICK_REFERENCE.md`](UV_QUICK_REFERENCE.md)
- **Full Guide**: [`ARCH_LINUX_SETUP.md`](ARCH_LINUX_SETUP.md)
- **Database Setup**: [`DATABASE_SETUP.md`](DATABASE_SETUP.md)
- **Phase 3 Overview**: [`../docs/PHASE_3_START_HERE.md`](../docs/PHASE_3_START_HERE.md)

---

## 🚀 After Database Setup

Once `./init_database.sh` completes successfully:

### 1. Start the Backend
```bash
uv run python app.py
```

You should see:
```
 * Running on http://127.0.0.1:5000
```

### 2. Test the API (in another terminal)
```bash
# Check health
curl http://localhost:5000/api/status

# Should return:
# {"status": "running", "database": {"status": "connected"}}

# List recipes (should be empty initially)
curl http://localhost:5000/api/recipes

# Should return:
# {"recipes": [], "count": 0, "user_id": null}
```

### 3. (Optional) Migrate Existing Recipes
If you have recipes in `recipes/*.json`:
```bash
uv run python scripts/migrate_recipes_to_db.py --dry-run
uv run python scripts/migrate_recipes_to_db.py
```

---

## 🎓 Common Commands

```bash
# Start Flask backend
uv run python app.py

# Database migrations
uv run flask db migrate -m "Description"
uv run flask db upgrade

# Add a new Python package
uv add package-name

# Update all dependencies
uv sync --upgrade
```

**No need to activate .venv!** Just prefix with `uv run`.

---

## 🆘 Troubleshooting

### "uv: command not found"
Install uv first (see above).

### "Permission denied: ./init_database.sh"
```bash
chmod +x init_database.sh
./init_database.sh
```

### "No pyproject.toml found"
Make sure you're in the Backend directory:
```bash
cd Backend
pwd  # Should show: /path/to/Backend
```

### Script fails during migration
```bash
# Clean up and retry
rm -rf migrations/ tasteslikegood.db .venv/
./init_database.sh
```

### Want to use traditional venv activation?
```bash
uv sync                    # Creates .venv
source .venv/bin/activate  # Traditional activation
python app.py              # Works without 'uv run'
deactivate                 # When done
```

---

## 📊 Project Status

**Phase 3 Backend**: ✅ 100% Complete  
**Database Models**: ✅ User, Recipe with timestamps  
**API Endpoints**: ✅ Full CRUD operations  
**Documentation**: ✅ Complete  
**Your Status**: ⏳ Ready to initialize database!

---

## ✨ Next Steps

1. **Right now**: Run `./init_database.sh`
2. **Then**: Test with `uv run python app.py`
3. **Next**: Frontend integration (create RecipeService)
4. **Finally**: Deploy with PostgreSQL

---

## 🎉 You're Ready!

Everything is set up for uv. Just run:

```bash
cd Backend
./init_database.sh
```

The script will handle everything automatically! 🚀

---

**Last Updated**: March 1, 2026  
**All scripts updated for uv**: ✅ Complete

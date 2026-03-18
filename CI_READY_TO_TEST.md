# Backend CI/CD - Ready to Test Checklist

## ✅ Step-by-Step Verification

### 1. Navigate to Backend Directory
```bash
cd /home/adam/projects/tasteslikegoodtheangularsvegancookbook/Backend
```

### 2. Verify Files Were Created

Check that all CI files exist:
```bash
ls -la .github/workflows/ci.yml
ls -la .flake8
ls -la .gitignore
ls -la .github/pull_request_template.md
ls -la CI_QUICK_REFERENCE.md
ls -la CI_IMPLEMENTATION_COMPLETE.md
ls -la CI_SCRIPTS_INVENTORY.md
ls -la CONTRIBUTING.md
ls -la BACKEND_CI_COMPLETE.md
```

**Expected:** All files should exist (no "No such file" errors)

### 3. Install Dev Dependencies

```bash
uv sync --dev
```

**Expected:** Dependencies install successfully

### 5. (Alternative) Run Checks Manually

If you prefer to run checks individually:

#### Run Formatter (Black)

```bash
uv run black .
```

**Expected:** Files formatted (or "All done! ✨ 🍰 ✨")

#### Run Linter (Flake8)

```bash
uv run flake8 .
```

**Expected:** 
- Ideally: No output (no errors)
- Or: Some warnings that need fixing

#### Run Type Checker (mypy)

```bash
uv run mypy . --ignore-missing-imports
```

**Expected:**
- Some type errors are OK initially
- Goal: Fix critical ones before committing

#### Run Tests

```bash
uv run pytest --cov=. --cov-report=term
```

**Expected:**
- Tests pass (or identify which fail)
- Coverage report generated

### 6. All-in-One Manual Check

```bash
uv run black . && \
uv run flake8 . && \
uv run mypy . --ignore-missing-imports && \
uv run pytest --cov=.
```

**Expected:** All checks pass (or you know what needs fixing)

---

## 🔧 If Checks Fail

### Black reformats many files
✅ **This is normal** - Commit the formatted files

### Flake8 shows errors
Options:
1. Fix the errors manually
2. Some can be auto-fixed with Black
3. Update `.flake8` to ignore specific rules (not recommended)

### mypy shows many errors
Options:
1. Fix critical type errors (function signatures, return types)
2. Add `# type: ignore` comments for complex cases
3. This is informational - doesn't block CI initially

### pytest fails
1. Check which tests fail: `uv run pytest -v`
2. Fix failing tests
3. Or skip for now and fix in follow-up PR

---

## 📝 Commit Changes

### Stage CI Files

```bash
git add .github/workflows/ci.yml
git add .flake8
git add .gitignore
git add .github/pull_request_template.md
git add CI_QUICK_REFERENCE.md
git add CI_IMPLEMENTATION_COMPLETE.md
git add CI_SCRIPTS_INVENTORY.md
git add CONTRIBUTING.md
git add BACKEND_CI_COMPLETE.md
git add README.md
```

### Stage Any Formatted Files

```bash
# If Black formatted files, stage them too
git add -u
```

### Commit

```bash
git commit -m "ci: Add CI/CD pipeline for Flask backend

- Add GitHub Actions workflow (4 jobs: lint, type-check, test, security)
- Add Black formatter configuration (line length 100)
- Add Flake8 linter configuration (max complexity 10)
- Add mypy type checker configuration (Python 3.13)
- Add comprehensive .gitignore
- Add CI documentation and quick references
- Add PR template with checklist
- Update README with CI/CD section

Tests: pytest with coverage reporting
Tools: Black, Flake8, mypy, pytest, Safety
Package Manager: uv (10-100x faster than pip)
Python: 3.13
"
```

### Push

```bash
git push origin dev/backend_sub222
```

---

## 🔍 Verify in GitHub

1. **Go to your repo:** https://github.com/adamtasteslikegood/tasteslikegood.com
2. **Switch to branch:** `dev/backend_sub222`
3. **Click "Actions" tab**
4. **Watch the workflow run**

You should see 4 jobs running:
- ✅ lint (Black + Flake8)
- ✅ type-check (mypy)
- ✅ test (pytest)
- ⚠️  security (Safety - non-blocking)

---

## 🎯 Success Criteria

### Minimum (Can commit now):
- [ ] All CI files created
- [ ] Files committed to git
- [ ] Pushed to `dev/backend_sub222`
- [ ] GitHub Actions workflow visible in Actions tab

### Ideal (Fix before merging):
- [ ] Black formatting applied
- [ ] Flake8 passes with 0 errors
- [ ] mypy has minimal errors
- [ ] pytest passes all tests
- [ ] Coverage >70%

---

## ⚡ Quick Commands Summary

```bash
# Navigate
cd Backend

# Install
uv sync --dev

# Check
uv run black .
uv run flake8 .
uv run mypy . --ignore-missing-imports
uv run pytest --cov=.

# All-in-one
uv run black . && uv run flake8 . && uv run mypy . --ignore-missing-imports && uv run pytest --cov=.

# Commit
git add .
git commit -m "ci: Add CI/CD pipeline for Flask backend"
git push origin dev/backend_sub222
```

---

## 📞 Need Help?

**Documentation:**
- `CI_QUICK_REFERENCE.md` - All commands
- `CI_IMPLEMENTATION_COMPLETE.md` - Detailed guide
- `BACKEND_CI_COMPLETE.md` - Summary

**Common Issues:**
- uv not found: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Dependencies fail: `rm -rf .venv && uv sync --dev`
- Tests fail: `uv run pytest -v --tb=short` (see details)

---

## ✅ Final Status Check

Before pushing, verify:
- [ ] I'm in the Backend directory
- [ ] All files were created successfully
- [ ] Dev dependencies installed (`uv sync --dev`)
- [ ] Ran at least one check (Black, Flake8, mypy, or pytest)
- [ ] Committed all new files
- [ ] Ready to push to `dev/backend_sub222`

---

**Ready to go? Run:**

```bash
cd /home/adam/projects/tasteslikegoodtheangularsvegancookbook/Backend
git status
git add .
git commit -m "ci: Add CI/CD pipeline for Flask backend"
git push origin dev/backend_sub222
```

🚀 **Your Flask backend CI/CD is ready!**

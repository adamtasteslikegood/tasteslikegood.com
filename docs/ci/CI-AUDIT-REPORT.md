# CI/CD Pipeline Audit Report

**Repository:** `adamtasteslikegood/tasteslikegood.com`
**Audit Date:** April 2, 2026
**Auditor:** Copilot Coding Agent

---

## Executive Summary

The repository has **14 total workflows** (9 custom + 5 GitHub-managed dynamic workflows). Two critical failures were identified and fixed in this audit:

1. **Redundant broken CI workflow** (`backend-ci.yml`) using `pip` instead of `uv`, causing persistent test and type-check failures.
2. **Incomplete `requirements.txt`** missing packages that `pyproject.toml` declares (breaking Docker builds and the pip-based CI).

After fixes, all CI gates pass: Black ✅, Flake8 ✅, mypy ✅, 88/88 tests ✅.

---

## Visual Documentation

| Diagram | File | Format | Description |
|---------|------|--------|-------------|
| Flow Chart | [`ci-pipeline-flowchart.md`](ci-pipeline-flowchart.md) | Mermaid | Decision-based flow of the entire CI/CD pipeline |
| Process Chart | [`ci-process-chart.md`](ci-process-chart.md) | Mermaid | Step-by-step sequence within each CI stage |
| Workflow Diagram | [`ci-workflow-diagram.svg`](ci-workflow-diagram.svg) | SVG | High-level visual overview of all workflows and merge gates |

---

## Workflow Inventory & Health Assessment

### Custom Workflows (`.github/workflows/`)

| # | Workflow File | Display Name | Trigger | Status Before | Status After | Issues Found |
|---|--------------|-------------|---------|---------------|--------------|--------------|
| 1 | `ci.yml` | Backend CI | push/PR → main, develop, dev/* | ✅ Passing | ✅ Passing (primary) | None — this is the authoritative CI |
| 2 | `backend-ci.yml` | Backend CI (dup) | PR → main only | ❌ **Always failing** | 🗑️ **Removed** | Duplicate name, pip-based, missing deps, broken mypy |
| 3 | `check-non-ascii-filenames.yml` | Check Non-ASCII Filenames | push/PR → all branches | ✅ Passing | ✅ Passing | None |
| 4 | `dependabot-auto-merge.yml` | Dependabot Auto-Merge | pull_request | ✅ Working | ✅ Working | None |
| 5 | `gemini-dispatch.yml` | 🔀 Gemini Dispatch | PR, issues, comments | ✅ Working | ✅ Working | None |
| 6 | `gemini-invoke.yml` | ▶️ Gemini Invoke | workflow_call | ✅ Working | ✅ Working | None |
| 7 | `gemini-review.yml` | 🔎 Gemini Review | workflow_call | ✅ Working | ✅ Working | None |
| 8 | `gemini-scheduled-triage.yml` | 📋 Scheduled Triage | cron (hourly) | ✅ Passing | ✅ Passing | Runs every hour — monitor compute usage |
| 9 | `gemini-triage.yml` | 🔀 Gemini Triage | workflow_call | ✅ Working | ✅ Working | None |

### Dynamic/Platform Workflows

| # | Workflow | Manager | Status | Notes |
|---|---------|---------|--------|-------|
| 10 | Copilot coding agent | GitHub Copilot | ✅ Active | Dynamic agent workflow |
| 11 | Copilot code review | GitHub Copilot | ✅ Active | PR review workflow |
| 12 | Dependabot Updates | Dependabot | ✅ Active | Dependency version bumps |
| 13 | Automatic Dependency Submission | GitHub | ✅ Active | Dependency graph updates |
| 14 | CodeQL | GitHub Advanced Security | ✅ Active | Code scanning for vulnerabilities |

---

## Issues Found & Resolutions

### Issue 1: Redundant Broken CI Workflow ⛔ → ✅ FIXED

**Severity:** Critical
**Workflow:** `backend-ci.yml`
**Root Cause:** The repository had TWO workflows both named "Backend CI":
- `ci.yml` — the correct one, using `uv sync --dev` with pyproject.toml
- `backend-ci.yml` — a broken duplicate using `pip install -r requirements.txt`

**Failure Details:**
- **Lint/mypy job** failed with: `workflow_runs/app.py: error: Duplicate module named "app"` — because the mypy command explicitly targeted both `app.py` and `workflow_runs/app.py` (`mypy app.py workflow_runs/app.py --exclude 'workflow_run'`), ignoring the `exclude` configuration in `pyproject.toml`.
- **Test job** failed with: `ModuleNotFoundError: No module named 'flask_cors'` — because `requirements.txt` was missing packages declared in `pyproject.toml`.

**Run History:** 14+ consecutive failures (runs #27–#53), **never passed once**.

**Resolution:** Removed `backend-ci.yml`. The `ci.yml` workflow is the authoritative CI using `uv` and `pyproject.toml`.

### Issue 2: Incomplete `requirements.txt` ⚠️ → ✅ FIXED

**Severity:** High
**File:** `requirements.txt`
**Root Cause:** When new dependencies were added to `pyproject.toml`, `requirements.txt` was not updated. This caused:
- Docker builds (`Dockerfile`) to produce incomplete environments
- The pip-based CI workflow to fail

**Missing Packages Added:**
| Package | Version | Required By |
|---------|---------|-------------|
| `flask-cors` | 6.0.0 | `app.py` — CORS support |
| `flask-sqlalchemy` | 3.1.1 | `extensions.py` — ORM integration |
| `flask-migrate` | 4.0.5 | Database migrations |
| `sqlalchemy` | 2.0.48 | ORM engine |
| `alembic` | 1.18.4 | Migration tool |
| `pandas` | 3.0.1 | Data processing |
| `python-multipart` | 0.0.22 | Form data parsing |
| `cachetools` | 5.5.2 | Caching utilities |
| `rsa` | 4.9.1 | Cryptographic operations |

**Resolution:** Added all missing packages with pinned versions to `requirements.txt`.

### Issue 3: Duplicate Workflow Names ⚠️ → ✅ FIXED

**Severity:** Medium
**Detail:** Both `ci.yml` and `backend-ci.yml` were named "Backend CI", causing confusion in the GitHub Actions UI. When querying workflow runs, both appeared under the same name making it impossible to distinguish which was which.

**Resolution:** Resolved by removing the duplicate `backend-ci.yml`.

---

## CI Quality Rubric

| Category | Check | Result | Details |
|----------|-------|--------|---------|
| **Code Formatting** | Black `--check` | ✅ Pass | 226 files checked, all compliant |
| **Linting** | Flake8 | ✅ Pass | 0 errors, max-line-length=100 |
| **Type Safety** | mypy | ✅ Pass | 53 source files, `workflow_runs/` excluded |
| **Unit Tests** | pytest | ✅ Pass | 88/88 tests pass in 4.08s |
| **Coverage** | pytest-cov | ✅ 51% | Uploaded as artifact (7-day retention) |
| **Security Scan** | Safety | ⚠️ Advisory | `continue-on-error: true` — reports only |
| **Dependency Security** | CodeQL | ✅ Active | GitHub-managed scanning |
| **AI Code Review** | Gemini + Copilot | ✅ Active | Reviews on every PR |
| **Repo Hygiene** | Non-ASCII check | ✅ Active | All filenames ASCII-safe |
| **Dependency Updates** | Dependabot | ✅ Active | Auto-merge enabled |
| **Branch Protection** | Required checks | ⚠️ Recommend | See recommendations below |

---

## Recommendations

### 1. Enforce Branch Protection Rules
Configure branch protection on `main` to require these status checks before merge:
- `Lint (Black + Flake8)` ✅
- `Type Check (mypy)` ✅
- `Test (pytest)` ✅
- `Check for Non-ASCII Characters in Filenames` ✅

> **Note:** This requires repository admin access via Settings → Branches → Branch protection rules.

### 2. Monitor Scheduled Triage Compute
The `gemini-scheduled-triage.yml` runs **every hour** (24 runs/day). Consider reducing to every 4–6 hours if issue volume is low, to conserve GitHub Actions minutes.

### 3. Keep `requirements.txt` in Sync
When adding new dependencies to `pyproject.toml`, also update `requirements.txt` for Docker compatibility. Consider automating this with `uv pip compile pyproject.toml -o requirements.txt`.

### 4. Consider Adding Frontend CI
The issue title mentions "Angular+Flask" but this project uses Jinja2 server-side templates, not Angular. If a frontend framework is planned for the future, a separate frontend CI workflow will be needed.

---

## Workflow Architecture

```
Event arrives (push, PR, issue, schedule, comment)
       │
       ▼
┌──────────────────────────────────────────────────────┐
│                   GitHub Actions                      │
│                                                       │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────┐ │
│  │ Backend CI   │  │ Gemini AI    │  │ Utilities   │ │
│  │ (ci.yml)     │  │ Dispatch     │  │             │ │
│  │              │  │              │  │ • Non-ASCII  │ │
│  │ • Lint       │  │ • Review     │  │ • Dependabot │ │
│  │ • Type Check │  │ • Triage     │  │   Auto-merge│ │
│  │ • Test       │  │ • Invoke     │  │             │ │
│  │ • Security   │  │ • Scheduled  │  │             │ │
│  └─────────────┘  └──────────────┘  └─────────────┘ │
│                                                       │
│  ┌─────────────────────────────────────────────────┐ │
│  │         Platform Services (Dynamic)              │ │
│  │  CodeQL • Dependabot • Copilot • Dep Graph      │ │
│  └─────────────────────────────────────────────────┘ │
│                                                       │
│                   Merge Gate                          │
│            ┌──────────────────┐                      │
│            │  All checks pass │──→ ✅ Merge allowed  │
│            └──────────────────┘                      │
└──────────────────────────────────────────────────────┘
```

---

## Addendum — 2026-07-14: CI/CD Refresh executed (supersedes the recommendations above)

The April audit's key recommendation — required status checks — was never
implemented, and between April and July the pipeline regressed further:
`ci.yml`'s branch filters stopped matching the integration branch entirely, so
the Lint/Type-Check/Test/Security jobs silently never ran, and the Gemini
dispatch pipeline was deleted (`9ef54fc`) after months of failures. The full
post-mortem and redesign live in [`refresh/`](refresh/) (SPEC-01, SPEC-02,
TODO, PROMPT).

Executed 2026-07-14 via an agent-harness loop; state of the world as of close:

| Area | Change | Landed in |
|---|---|---|
| CI triggers | `on.push`/`on.pull_request` → `[main, dev]`; concurrency group; safety→pip-audit | #169 |
| Tree health | Black sweep (15 files, `.git-blame-ignore-revs`), flake8 0 (E266/E501 now enforced), mypy fixed (`explicit_package_bases` in pyproject; `setup.cfg` deleted — it was shadowed), test suite hermetic (FRONTEND_URL leakage, lazy Pub/Sub client) | #170 #171 #172 #173 |
| Build gate | `Build (docker)` job (no push); red-tested via #175 | #174 |
| Branch protection | 8 required contexts on `dev` **and** `main` via repository rulesets (`rule222`, `protect-main`), incl. per-language `Analyze (…)` CodeQL contexts | rulesets |
| Dependency queue | 10 Dependabot PRs drained: #158 merged; #157 closed (py3.14 vs `requires-python <3.14`; Dockerfile aligned to 3.13 in #178); #159–#166 superseded by consolidated refresh #179 (Dependabot's own `requirements.txt` regeneration drops packages/markers — the sync gate caught all eight) | #178 #179 |
| AI workflows | Scheduled triage: 6h cron + `ci-health` failure-alert job (#180); standalone `gemini-triage.yml` (#181, hardened #183); standalone label-gated `gemini-review.yml` (#184); standalone `gemini-invoke.yml` with deterministic two-phase approval (#185) | #180–#185 |

Correction to SPEC-02 §3.5 discovered during rollout: current gemini-cli
requires `GEMINI_CLI_TRUST_WORKSPACE=true` on **every** `run-gemini-cli` job,
not only those that check out code (evidence: run 29353718769 failed in an
empty workspace; the scheduled workflow's historical greens were vacuous —
its Gemini step skips when there are no untriaged issues).

# CI/CD Refresh — TODO

Execution checklist for [SPEC-01](SPEC-01-ci-quality-gates.md) and
[SPEC-02](SPEC-02-ai-triage-and-review.md). Each item names its **verify** command —
an item is done only when its verify passes. One PR per checkbox group unless noted.
All branches off `dev`, PRs into `dev` (see repo `CLAUDE.md` branching rules).

Baseline measured 2026-07-14 on `dev`: black ❌ 15 files · flake8 ❌ 38 · mypy ❌ crash
· pytest ❌ 4/164 · `ci.yml` triggers never match `dev`.

## Phase 1 — Re-arm CI (informational)

- [ ] `ci.yml`: change `on.push.branches` / `on.pull_request.branches` to `[main, dev]`
      — verify: open a scratch PR into `dev`; Lint/Type-Check/Test/Security jobs appear
- [ ] `ci.yml`: add `concurrency` group with `cancel-in-progress: true`
      — verify: `actionlint .github/workflows/ci.yml` clean; force-push a PR twice, first run cancels
- [ ] `ci.yml`: replace deprecated `safety check` with `pip-audit` (keep
      `continue-on-error: true`)
      — verify: security job runs `pip-audit` and completes
- [ ] Note in scratch-PR description which jobs are red (expected: lint, type-check, test)

## Phase 2 — Fix the tree (one PR per bullet)

- [ ] Run `uv run black .`, commit the 15-file sweep alone; add the commit hash to
      `.git-blame-ignore-revs`
      — verify: `uv run black --check .` exits 0
- [ ] Fix 38 flake8 violations; align flake8 config with Black
      (`max-line-length = 100`, `extend-ignore = E203,W503`); targeted `# noqa` only
      with justification
      — verify: `uv run flake8 . --count` prints 0
- [ ] Fix mypy crash: `explicit_package_bases = true` (or exclude `scripts/`) in
      `[tool.mypy]`; then fix or explicitly `# type: ignore[code]` whatever real errors
      surface, with a tracking issue for deferred ones
      — verify: `uv run mypy . --ignore-missing-imports` exits 0
- [ ] Root-cause the 4 `tests/test_public_ssr.py` failures (canonical link + sitemap).
      **Decide first whether it's test drift or a real SSR/SEO regression**; fix
      accordingly (a real regression is its own `fix/` PR)
      — verify: `FLASK_SECRET_KEY=x GOOGLE_CLIENT_ID=x GOOGLE_CLIENT_SECRET=x uv run pytest` → 164 passed
- [ ] Close the loop: re-run the scratch PR from Phase 1 — all jobs green

## Phase 3 — Build gate

- [ ] Add `build` job to `ci.yml`: `docker build -f Dockerfile .` (no push, no creds)
      — verify: job green on a PR; then red when `requirements.txt` is broken on a
      scratch branch (test once, revert)

## Phase 4 — Branch protection

- [ ] Configure required status checks on `dev` (exact context names — CodeQL is a
      matrix, so its contexts are the per-language job names, never the workflow name):
      `Lint (Black + Flake8)`, `Type Check (mypy)`, `Test (pytest)`, `Build (docker)`,
      `Check for Non-ASCII Characters in Filenames`, `Analyze (actions)`,
      `Analyze (python)`, `Analyze (javascript-typescript)`
      — verify: `gh api repos/adamtasteslikegood/tasteslikegood.com/branches/dev/protection --jq '.required_status_checks.contexts'`
- [ ] Same for `main`
- [ ] Negative test: PR with a deliberately failing test cannot merge; revert
- [ ] Confirm `dependabot-auto-merge.yml` now waits on required checks
      — verify: next Dependabot PR only auto-merges after checks pass

## Phase 5 — Drain the backlog

- [ ] Rebase/recreate the ~10 open Dependabot PRs against the reformatted tree
      (`@dependabot rebase` comment on each)
      — verify: `gh pr list --state open` shows each PR green or intentionally closed
- [ ] Merge or close each through the new gates

## Phase 6 — AI triage & review (SPEC-02, each its own PR)

- [ ] Retune `gemini-scheduled-triage.yml` cron hourly → every 6h; add a dedicated
      alert job (`needs: [triage, label]`, `if: failure()`, own `issues: write`
      permission) that opens/updates a `ci-health` issue — a step inside one job
      can't see the other job's failure, and the `triage` job only has `issues: read`.
      Do NOT add job-level `continue-on-error` to this workflow — it reports failed
      jobs as succeeded and the alert job would never fire
      — verify: green scheduled run; forced failure in either job opens the issue
- [ ] Standalone `gemini-triage.yml` on `issues: opened`, reusing
      `.github/commands/gemini-triage.toml`; guardrails: `timeout-minutes: 10`,
      concurrency, `continue-on-error: true`, pinned action SHA
      — verify: test issue gets labeled
- [ ] Standalone `gemini-review.yml` triggered by `gemini-review` label +
      `workflow_dispatch`; `pull_request` event with same-repo + not-Dependabot guard
      (Dependabot/fork runs get no secrets — skip cleanly, never fail); set
      `GEMINI_CLI_TRUST_WORKSPACE: true` on the checkout job
      — verify: labeled **internal** PR receives review comments; labeling a
      Dependabot PR produces a clean skip; check is not required anywhere
- [ ] Standalone `gemini-invoke.yml` on `@gemini-cli` comments, **author-association
      allowlist (OWNER/MEMBER/COLLABORATOR) enforced deterministically on BOTH the
      trigger comment and the `/approve` approval comment** (the invoke TOML's
      model-side approval poll is not a security boundary); set
      `GEMINI_CLI_TRUST_WORKSPACE: true` on the checkout job
      — verify: collaborator trigger + approval works; non-collaborator trigger is
      ignored; non-collaborator `/approve` on a pending plan leaves the run halted

## Phase 7 — Documentation & close-out

- [ ] Update `docs/ci/CI-AUDIT-REPORT.md` (or supersede it with a dated addendum)
      to reflect the new pipeline
- [ ] Update this repo's `CLAUDE.md` "Commands" section if any dev commands changed
- [ ] Cookbook repo: bump `Backend` submodule pointer via the standard two-step flow
- [ ] Two-week health check: no failing scheduled runs, no red Gemini checks on PRs,
      Dependabot merging cleanly

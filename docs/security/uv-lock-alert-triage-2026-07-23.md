# uv.lock Dependabot alert triage — 2026-07-23

Follow-up to `docs/security/dependabot-alert-triage-2026-07-23.md` (the agent-skill
Dependabot-noise cleanup). Once #229 and #230 merged and GitHub rescanned, all
skill-fixture alerts flipped to `state: fixed`, leaving exactly 4 open alerts —
all on `uv.lock`, the repo's real dependency lockfile. This triages those 4.

## The 4 alerts

| # | Package | Pinned | Patched | Severity | Advisory |
|---|---|---|---|---|---|
| 665 | `cryptography` | 46.0.7 | 48.0.1 | high (CVSS 7.5) | [GHSA-537c-gmf6-5ccf](https://github.com/advisories/GHSA-537c-gmf6-5ccf) — vulnerable OpenSSL vendored in the wheel |
| 583 | `Mako` | 1.3.10 | 1.3.12 | high | [GHSA-2h4p-vjrc-8xpq](https://github.com/advisories/GHSA-2h4p-vjrc-8xpq) — path traversal via backslash URI in `TemplateLookup` (Windows) |
| 507 | `Mako` | 1.3.10 | 1.3.11 | high (CVSS 7.5) | [GHSA-v92g-xgxw-vvmm](https://github.com/advisories/GHSA-v92g-xgxw-vvmm) — path traversal via double-slash URI prefix in `TemplateLookup` |
| 436 | `Pygments` | 2.19.2 | 2.20.0 | low (CVSS 3.3) | [GHSA-5239-wwwm-4pmq](https://github.com/advisories/GHSA-5239-wwwm-4pmq) — ReDoS in GUID-matching lexer regex |

None of these are direct dependencies in `pyproject.toml` — all three are
transitive.

## Reachability (is this actually exploitable here?)

- **`cryptography`** — pulled in by `google-auth==2.56.0`, a **direct runtime
  dependency** used for the Google OAuth flow. The vendored-OpenSSL CVE is
  exercised by any TLS/crypto operation `cryptography` performs, which
  `google-auth` does on every token/cert verification. **Real, reachable,
  ships to production.**
- **`Mako`** — pulled in by `alembic` (via `flask-migrate`, also a direct
  runtime dependency, since Flask-Migrate is imported by the running app even
  though migrations themselves only execute via `flask db upgrade`). Ships to
  production, but the vulnerable code path — `TemplateLookup` resolving a
  template by an **attacker-controlled URI** — is never exercised: Alembic
  only loads its own fixed local `script.py.mako` template to render new
  migration files, never a path derived from user input. **Low practical
  risk, but free to fix and worth doing** since it ships in the image anyway.
- **`Pygments`** — pulled in only by `pytest`, which lives in
  `[dependency-groups].dev` / `[project.optional-dependencies].dev`. The
  Dockerfile's build stage runs `uv export --no-dev`, so **Pygments never
  ships to the production image** — this alert reflects a dev/CI-only
  dependency, not anything running in Cloud Run. **Not reachable in
  production at all**, but still worth bumping for a clean CI environment.

## Fix

None of the three are pinned directly, so bumping them cleanly (without
adding noise to `[project.dependencies]`) means adding floor constraints
under `[tool.uv].constraint-dependencies` in `pyproject.toml`, then
regenerating `uv.lock`:

```toml
[tool.uv]
constraint-dependencies = [
    "cryptography>=48.0.1",  # via google-auth
    "mako>=1.3.12",          # via alembic (flask-migrate)
    "pygments>=2.20.0",      # via pytest (dev group only)
]
```

Checked compatibility against every direct dependent before bumping — none
cap these below the patched version:

- `google-auth==2.56.0` requires `cryptography>=38.0.3` (no ceiling)
- `alembic==1.18.4` requires `Mako` (no version constraint at all)
- `pytest==9.1.1` requires `pygments>=2.7.2` (no ceiling)

`uv lock` resolved cleanly: `cryptography 46.0.7 → 49.0.0`,
`mako 1.3.10 → 1.3.12`, `pygments 2.19.2 → 2.20.0`.

## Bonus findings from local verification

Running `pip-audit` against the exact frozen export the Dockerfile ships
(`uv export --format requirements-txt --no-dev --extra postgres --frozen`,
matching the CI `security` job byte-for-byte) surfaced **two more real
findings that Dependabot alerts hadn't flagged yet**:

| Package | Pinned | Fixed | Advisory |
|---|---|---|---|
| `click` | 8.3.1 | 8.3.3 | PYSEC-2026-2132 |
| `httplib2` | 0.31.2 | 0.32.0 | PYSEC-2026-3444 |

`click` is pulled in by Flask itself; `httplib2` by
`google-api-python-client` (both direct runtime deps, both reachable). Since
this PR was already touching the exact same lockfile for the same class of
fix, both were added to the same `constraint-dependencies` block and bumped
rather than left as a known gap discovered mid-fix. `uv lock` resolved
`click 8.3.1 → 8.4.2` and `httplib2 0.31.2 → 0.32.0` with no conflicts.

## Verification performed

- `uv lock` — resolved 111 packages, no conflicts
- `uv run pytest -q` — 307 passed
- `uv run mypy .` — no issues
- `uv run black --check .` / `uv run flake8 .` — clean
- `uv run flask db heads` — single head (no branched migrations introduced)
- `pip-audit` against the Dockerfile's exact frozen export — **0 known
  vulnerabilities** (down from 2 surfaced before this fix, in addition to
  the 3 GitHub-flagged packages)

## Result

All 4 previously-open Dependabot alerts on `uv.lock`, plus 2 additional
findings surfaced by `pip-audit`, are resolved by this PR. Combined with
#229 and #230, this closes out essentially the entire security-advisory
backlog that motivated this triage — from 228 GitHub-reported vulnerabilities
down to (expected) 0 once this merges and rescans, aside from the 1
pre-existing, unrelated CodeQL alert (`py/unused-global-variable`,
`services/gcs_service.py`), which is out of scope for a dependency fix.

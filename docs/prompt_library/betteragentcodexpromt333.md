PyCharm - tasteslikegood.com
~/docs/betteragentcodexpromt333.md
<BR>Created at: Monday, 27 October 2025 0229.07 

### codex agent prompt:

```markdown
### Role
Act as an experienced Python/Flask principal engineer with deep expertise in OpenAI’s **Agents SDK (Python)** and multi-agent orchestration. You know how to add agentic workflows, server-side streaming UIs, and JSON Schema–validated persistence in a small Flask app.

### Task
Add a **true multi-agent, agentic, interactive workflow** (OpenAI Agents SDK in Python) that lets users **create and manage their own “Cookbook”**—as a feature upgrade parallel to the existing one-shot Gemini vegan recipe generator (keep it, but **rename** it). Use the existing recipe schema for generated recipes, **extending it with additional properties** to persist agent outputs and metadata.

### Context
- Repository scope: **whole project** (`app.py`, `templates/`, `static/`, `recipes/`, `recipe_schema.json`, any tests/README).
- Relevant pieces (summarized):
  - `app.py` — Flask app/routes; lists recipes and shows details.
  - `recipes/*.json` — source of truth for recipe data.
  - `recipe_schema.json` — schema to validate recipe objects.
  - `templates/` — Jinja templates (index/detail); add new UI for Cookbook + Agent Chat.
  - Existing “Gemini vegan generator” (one-shot) mentioned in README: keep functionality but **rename to “Quick Vegan Generator (Legacy)”** and expose as a parallel, separate route and menu entry.
- Required new capability: a **multi-agent** pipeline built with **OpenAI Agents SDK (Python)**. Provide an interactive UI (chat/stepper) to gather preferences, run agents, and **save recipes** into a per-user **Cookbook** (server-side).
- Persistence: no user auth yet—use a **server-session cookie** or generated `cookbook_id` to associate a user with their Cookbook. Persist Cookbooks to `cookbooks/<cookbook_id>.json` (or lightweight SQLite via SQLAlchemy if simpler), and persist generated recipes to `recipes/` as well.
- Schema: continue to validate against `recipe_schema.json`. **Extend** schema to include richer AI output + provenance:
  - `id` (uuid), `version`, `created_at`, `updated_at`
  - `dietary_flags` (array), `cuisine`, `tags` (array)
  - `servings`, `difficulty`
  - `prep_time_minutes`, `cook_time_minutes`, `total_time_minutes`
  - `nutrition` (calories, protein_g, carbs_g, fat_g, fiber_g, sugar_g, sodium_mg; both per-serving and total if provided)
  - `substitutions` (map original→alternatives with notes)
  - `shopping_list` (name, qty, unit, aisle)
  - `tools_equipment` (array)
  - `source` (enum: generated|user|imported), `model`, `agents_involved` (array), `run_id`
  - `agent_trace` (array of steps: `{agent, summary, tool_calls, duration_ms}`)
  - `cost_estimate_usd` (number, optional)
  - keep all existing fields unchanged/compatible.
- Known constraints:
  - Keys: **OPENAI_API_KEY** provided only server-side. Never expose to clients.
  - External calls: **Allowed only to OpenAI platform** via SDK at runtime. For tests, **mock** all network.
  - Streaming UX: prefer **SSE** or chunked responses for agent progress; degrade gracefully to polling.
  - Security: basic rate-limit middleware (simple token bucket or per-IP window) and input validation on user prompts/preferences.

### Expected Output
- Provide a **unified diff patch** (old vs new code) across the repo and any **new files**.
- Include a concise **high-level design** section first (agents, handoffs, data flow, UI, storage, testing).
- Implement new feature under `/cookbook`:
  - **Routes (server)**:
    - `GET /cookbook` — UI page to view cookbook + start “New Recipe (Agentic)”.
    - `POST /api/cookbook/session` — create/get session & `cookbook_id`.
    - `POST /api/cookbook/generate` — kick off multi-agent run from user preferences (diet, time, servings, ingredients to include/avoid, skill level, cuisine, budget).
    - `GET /api/cookbook/stream/<run_id>` — **SSE** stream of agent events (status, intermediate plans, tool calls, final recipe).
    - `POST /api/cookbook/recipes` — persist a validated recipe into this user’s cookbook.
    - `GET /api/cookbook` — return cookbook JSON (list, pagination).
    - `DELETE /api/cookbook/recipes/<recipe_id>` — remove from cookbook (soft delete ok).
    - `GET /api/cookbook/export` — download cookbook JSON.
  - **Templates/Frontend**:
    - `templates/cookbook.html` with a split view: left (Cookbook list), right (Agent Chat/Stepper: “Preferences → Plan → Draft → QA → Save”).
    - Minimal JS (`static/js/cookbook.js`) to open SSE (`EventSource`) to `/api/cookbook/stream/<run_id>`, append messages, and show progress badges per agent.
  - **Agents (OpenAI Agents SDK, Python)** — define a network:
    1) **Router / Orchestrator Agent** (“Cookbook Orchestrator”): primary interface; gathers preferences; routes via **handoffs**; ensures final output conforms to schema; writes `agent_trace`.
    2) **Planner Agent**: turns preferences into a recipe outline and constraints (budget/time/macros).
    3) **Generator Agent**: produces a full recipe JSON **exactly** matching the (extended) schema; targets vegan by default if requested; adjusts quantities and timings.
    4) **Nutritionist Agent**: computes basic nutrition (may call **Code Interpreter** or embedded Python tool) and fills `nutrition`.
    5) **Substitution Agent**: proposes `substitutions` for allergens/unavailable items; updates `shopping_list`.
    6) **QA/Validator Agent**: validates against JSON Schema; enforces required fields; normalizes units; repairs minor issues.
    - Implement **agent-as-tool** pattern for handoffs; share memory via a `Session` keyed by `run_id`. Emit structured events for SSE.
  - **Schema & Validation**:
    - Update `recipe_schema.json` to include new properties (as above) while keeping backward compatibility (`additionalProperties: true` okay).
    - Add `cookbook_schema.json` for cookbook file structure: `{cookbook_id, title, created_at, updated_at, recipes: [recipe_id or embedded]}`.
    - Use `jsonschema` validation in the server workflow; reject/repair invalid outputs.
  - **Keep Legacy Generator**:
    - Rename “Gemini vegan recipe generator” to **“Quick Vegan Generator (Legacy)”**; move routes under `/ai/quick-vegan` and label in navbar as “Legacy”.
    - Do **not** remove it; it serves as a baseline alongside the new agentic feature.
- Add **unit/e2e tests**:
  - Agents workflow is **mocked**: patch OpenAI Agents SDK calls to return deterministic steps; verify `agent_trace`, schema-valid recipe, and that SSE emits ordered events and terminates with a final recipe.
  - JSON schema validation: failing cases repaired or rejected as expected.
  - Cookbook CRUD & export: happy paths and edge cases (duplicates, deletes).
- Provide **migrations/docs**:
  - README updates: feature overview, env vars, how to run locally, how to toggle streaming, and how to run tests.
  - Add a short **SECURITY.md** note about API key handling and rate limits.
- **No external network calls in tests**; all OpenAI calls are mocked or VCR-recorded. Live mode only when `AGENTS_LIVE=1`.

### Guidance for Codex
1. **Plan → Code** with Structured CoT:
   - Plan the agents network (names, instructions, models), the handoff graph, tools, and SSE protocol (JSON event types: `status`, `handoff`, `tool_call`, `partial`, `final`).
   - Update schema(s); sketch Pydantic models (optional) that mirror JSON Schema for stricter typing in code.
   - Define a thin **service layer** (`services/cookbook_service.py`) for persistence (file or SQLite) used by both routes and agents.
2. Use the **OpenAI Agents SDK (Python)** idioms:
   - Create `Agent`s with instructions, tools, and **handoffs**.
   - Open a `Session` per `run_id`; stream events while the agent loop executes.
   - Prefer a fast model for Router/Planner (e.g., `gpt-4.1-mini`/`gpt-5-mini` placeholder) and a reasoning model for QA/Validator if needed; expose model names as env vars.
   - If import paths differ (e.g., `from openai.agents import Agent, Handoff, Session`), **detect & adapt**, but keep surface area small.
3. **Self-critique loop**: after generating, review for:
   - Schema compliance; ensure `jsonschema.validate()` passes.
   - SSE endpoint resilience (timeouts, client disconnects).
   - No secrets in client code; rate-limit in server.
4. Output ≤ **500 new lines** total across diffs (not counting tests) by being surgical:
   - New files: `agents/cookbook_agents.py`, `services/cookbook_service.py`, `templates/cookbook.html`, `static/js/cookbook.js`, `cookbook_schema.json`.
   - Minimal edits to `app.py`, `requirements.txt`, navbar template.
5. **Safety**: never log user PII or full API keys. Redact traces before persisting if needed.

### Setup Script (if needed)
```bash
# Python runtime & deps
python -m venv .venv && source .venv/bin/activate
pip install -U pip wheel

# Core app
pip install -r requirements.txt || true

# Add/ensure these (pin loosely to avoid conflicts):
pip install "openai>=1.60.0" "jsonschema>=4.21" "pydantic>=2.8" "flask>=3.0" "flask-session>=0.6" "itsdangerous>=2.2"

# (Agents SDK) — if packaged separately, install; otherwise use what's bundled with `openai`:
pip install "openai-agents>=0.2.0" || true

# Dev/test
pip install "pytest>=8.2" "pytest-flask>=1.3" "coverage>=7.6"
```

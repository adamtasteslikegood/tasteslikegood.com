PyCharm - tasteslikegood.com
~/docs/agentcodexpromtreal222.md
<BR>Created at: Monday, 27 October 2025 0213.07 

### agentic codex prompy:

```markdown
### Role
Act as an experienced Python + Next.js/TypeScript principal engineer specializing in multi-agent LLM orchestration with the OpenAI SDK, FastAPI, and production-grade web integrations.

### Task
Add a **true multi-agent, agentic, interactive workflow** (Python, OpenAI SDK) that lets users create and manage a personal **“Cookbook”**, delivered as a direct feature upgrade to the existing **recipe generator form** in the GitHub repo `adamtasteslikegood/tasteslikegood.com`.

### Context
- Repository scope: whole project.
- Relevant files / functions:
  - Identify the current “recipe generator form” component/page via a fast code search (e.g., `grep -R "recipe" -n .`), then treat that file as the primary integration point. Leave the path generic in comments if not found; otherwise patch exact files.
  - Add a new **Python microservice** (FastAPI) under `services/cookbook_agent/` implementing the multi-agent system.
  - Add a **Cookbook UI** (list/detail CRUD) and extend the recipe form to stream the multi-agent workflow and save results.
- Known constraints:
  - Must use **OpenAI SDK in Python** (official package). Keep the API key server-side; never expose it to the browser.
  - Provide **streaming** partial outputs to the UI (SSE or WebSocket). Prefer SSE for simplicity.
  - **No external network calls after `setup script`** (other than OpenAI API at runtime). Stub/inline any optional HTTP fetches.
  - Data privacy: store user cookbooks **locally** (default SQLite via SQLAlchemy). Key by a first-party cookie `user_id` (UUID) created server-side.
  - Keep output ≤ 500 new lines (enforced by splitting files and being concise).
  - Add tests (Python unit + minimal frontend tests).
  - Ensure CORS/security basics and zero PII leakage.

### Expected Output
- Provide a **unified diff patch** for all modified files and the full content of any **new files**.
- Include a brief **architecture overview** before code.
- Add **unit/e2e tests**:
  - Python: agents and endpoints with `pytest`.
  - Frontend: critical UI/flow paths with React Testing Library.
- Include a `.env.example` diff.
- Include **README snippets** for running the new service and the app locally.
- Absolutely **no external network calls after `setup script`**.

### Guidance for Codex
1. **Plan (Structured CoT)**: Outline architecture → DB schema → agent roles → API surface → frontend wiring → tests → run scripts.
2. **Agents** (each with a focused system prompt, tool use, and handoffs):
   - **DirectorAgent**: orchestrates the flow and tool routing, maintains session state.
   - **PersonaAgent**: elicits/normalizes user preferences (diet, allergens, difficulty, cuisines, time, servings).
   - **PantryAgent**: parses pantry input, suggests substitutions, validates availability.
   - **PlannerAgent**: drafts recipe outline (title, style cues, sections, ingredients list with quantities).
   - **NutritionAgent**: estimates macros per serving (simple heuristic table embedded locally; no external calls).
   - **InstructionAgent**: writes clear step-by-step instructions with timings and tips.
   - **CriticAgent**: checks constraints (allergens, time, skill) and proposes fixes.
   - **CookbookAgent** (tooling layer): CRUD against DB; de-dupe titles; version recipes.
3. **Agentic Loop**:
   - Maintain a `Session` with shared memory/state (user profile, pantry, prior steps, working recipe).
   - Stream intermediate messages to client as SSE events: `persona`, `pantry`, `plan`, `nutrition`, `instructions`, `critic`, `final`.
   - Allow **interactive turns**: user can nudge (“more protein”, “swap dairy”, etc.), re-enter loop from relevant agent.
4. **API (FastAPI)**:
   - `POST /v1/sessions` → create session; set `user_id` cookie if missing.
   - `POST /v1/sessions/{id}/message` → accept user input; run/continue agent loop; return ack.
   - `GET /v1/sessions/{id}/events` (SSE) → stream agent updates.
   - `POST /v1/cookbook` → save current recipe to cookbook (versioned).
   - `GET /v1/cookbook` → list (filter by tags, text).
   - `GET /v1/cookbook/{rid}` → get detail.
   - `PATCH /v1/cookbook/{rid}` → edit metadata/content.
   - `DELETE /v1/cookbook/{rid}` → delete.
5. **Models (Pydantic/SQLAlchemy)**:
   - `User(id: uuid, created_at)`
   - `Session(id, user_id, created_at, state_json)`
   - `Recipe(id, user_id, title, tags[], servings, total_time, ingredients[], instructions[], nutrition{cal, protein, carbs, fat}, notes, created_at, updated_at, version, parent_id?)`
6. **OpenAI SDK Usage (Python)**:
   - Use the **official `openai` Python package** and a single `OpenAI()` client.
   - Prefer the **Responses API** with tool/function calling if available; otherwise fall back to `chat.completions`.
   - Define internal “tools” for DB CRUD and state reads, exposed to the Director for function-calling.
   - Provide retries, rate limiting backoff, and token budgeting (truncate histories).
7. **Frontend (Next.js/TypeScript)**:
   - **Upgrade recipe generator form**:
     - On submit → `POST /v1/sessions` then `POST /v1/sessions/{id}/message`.
     - Open SSE to `/v1/sessions/{id}/events` and render streaming steps with a timeline UI.
     - Present “Save to Cookbook” → `POST /v1/cookbook`.
   - Add **My Cookbook** page:
     - Grid/list of recipes; search and tag filters; CRUD actions.
     - Detail view with print/export to Markdown.
   - Add a thin client: `src/lib/cookbookClient.ts` (fetch wrappers).
8. **DB & Migrations**:
   - SQLite file `cookbook.db` by default; env-switchable via `DATABASE_URL`.
   - Auto-create tables on boot; include a lightweight migration helper if schema changes (alembic optional; keep code under limit).
9. **Security & Ops**:
   - Keep `OPENAI_API_KEY` server-side only.
   - Same-origin CORS with explicit allowed origin(s).
   - Set/validate `user_id` httpOnly cookie; do not trust client-provided IDs.
   - Input validation for all endpoints; cap payload sizes.
10. **Self-critique loop**: After generating the patch, review for:
    - Build/run errors, import correctness, typings, and exceeding 500 new lines.
    - Secrets in code (forbid).
    - Frontend wiring (actual paths exist).
    - Tests run green locally.

### Setup Script (if needed)
```bash
# Python service
python -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install "openai>=1.40.0" fastapi uvicorn pydantic "SQLAlchemy>=2.0" python-multipart sse-starlette tenacity pytest

# Node deps for frontend (adjust to repo’s manager)
npm install --save isomorphic-fetch
npm install --save-dev @testing-library/react @testing-library/jest-dom jest ts-jest @types/jest

# Env template
cp .env.example .env || true
# Required env (document in README and .env.example):
# OPENAI_API_KEY=sk-...
# DATABASE_URL=sqlite:///./cookbook.db
# COOKBOOK_SERVICE_URL=http://localhost:8001

# Run services (in two terminals)
# 1) Python microservice
uvicorn services.cookbook_agent.main:app --host 0.0.0.0 --port 8001 --reload
# 2) Web app (adjust script as per repo)
npm run dev

```


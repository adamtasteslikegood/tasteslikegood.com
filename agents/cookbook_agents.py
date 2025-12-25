"""Pantry-aware multi-agent orchestration using the OpenAI Agents SDK.

This module keeps the implementation intentionally compact while exposing
production-like skeletons for the full workflow and SSE-friendly streaming.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from openai import OpenAI
try:
    from openai.agents import Agent, Session, Handoff
except Exception:  # pragma: no cover - SDK shim for tests if unavailable
    class _Shim:
        def __init__(self, *_, **__):
            pass

    class Agent(_Shim):
        pass

    class Session(_Shim):
        def __init__(self, *_, **__):
            self._result = None

        def run(self, *_ , **__):
            return self

        def stream(self):
            return []

        def final(self):
            return {}

    class Handoff(_Shim):
        def __init__(self, name: str):
            super().__init__()
            self.name = name

from .tools import (
    parse_pantry_input,
    normalize_ingredient,
    unit_convert,
    nutrition_estimate,
    consolidate_shopping_delta,
    validate_recipe_json,
    normalize_title,
    build_agent_trace,
)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

FAST_MODEL = os.getenv("AGENT_FAST_MODEL", "gpt-4.1-mini")
REASON_MODEL = os.getenv("AGENT_REASONING_MODEL", "gpt-4.1")

# --- Agent definitions (concise instructions) ---
router = Agent(
    name="Cookbook Orchestrator",
    model=FAST_MODEL,
    instructions="""
You coordinate Pantry Parser → Pantry Matcher → Recipe Generator → Substitutions → Nutritionist → QA.
All dishes are plant-based by default; avoid redundant 'vegan/mock/substitute' prefixes unless explicitly asked.
Emit compact, structured steps; keep titles creative.
""",
    tools=[parse_pantry_input, normalize_ingredient, unit_convert],
    handoffs=[
        Handoff("PantryParser"),
        Handoff("PantryMatcher"),
        Handoff("RecipeGenerator"),
        Handoff("Substitutions"),
        Handoff("Nutritionist"),
        Handoff("QA"),
    ],
)

pantry_parser = Agent(
    name="PantryParser",
    model=FAST_MODEL,
    instructions="Parse free-text pantry into normalized items with qty/unit/confidence.",
    tools=[parse_pantry_input, normalize_ingredient, unit_convert],
)

pantry_matcher = Agent(
    name="PantryMatcher",
    model=FAST_MODEL,
    instructions="Classify pantry into matches/stretchables/missing; propose 2-3 recipe archetypes.",
)

recipe_generator = Agent(
    name="RecipeGenerator",
    model=REASON_MODEL,
    instructions="Generate full recipe JSON matching schema; titles are creative; steps concise.",
    tools=[unit_convert],
)

substitutions = Agent(
    name="Substitutions",
    model=FAST_MODEL,
    instructions="Suggest functional substitutions only when required; update shopping_delta.",
)

nutritionist = Agent(
    name="Nutritionist",
    model=FAST_MODEL,
    instructions="Estimate per-serving and total nutrition fields.",
    tools=[nutrition_estimate],
)

qa = Agent(
    name="QA",
    model=FAST_MODEL,
    instructions="Validate/repair against JSON Schema and normalize units.",
    tools=[validate_recipe_json, consolidate_shopping_delta],
)

AGENTS: Dict[str, Any] = {
    "Router": router,
    "PantryParser": pantry_parser,
    "PantryMatcher": pantry_matcher,
    "RecipeGenerator": recipe_generator,
    "Substitutions": substitutions,
    "Nutritionist": nutritionist,
    "QA": qa,
}


@dataclass
class PantryRunResult:
    run_id: str
    events: List[Dict[str, Any]]
    recipe: Dict[str, Any]


def _fallback_recipe(user_input: Dict[str, Any]) -> Dict[str, Any]:
    pantry = parse_pantry_input(user_input.get("pantry", ""))
    title = normalize_title(user_input.get("title") or "Pantry Harvest Skillet")
    return {
        "name": title,
        "description": "Quick recipe composed from pantry items.",
        "prepTime": 10,
        "cookTime": 15,
        "servings": 2,
        "ingredients": {
            "wet": [{"name": i["name"], "amount": 1, "units": i.get("unit") or "unit", "notes": "from pantry"} for i in pantry],
            "dry": [],
        },
        "instructions": ["Combine pantry items in a warm pan.", "Season to taste and serve."],
        "pantry_matches": pantry,
        "pantry_stretchables": [],
        "missing_items": [],
        "shopping_delta": consolidate_shopping_delta(pantry, []),
        "tools_equipment": ["skillet", "spoon"],
        "difficulty": "easy",
        "prep_time_minutes": 10,
        "cook_time_minutes": 15,
        "total_time_minutes": 25,
        "nutrition": nutrition_estimate(pantry, servings=2),
        "agents_involved": list(AGENTS.keys()),
        "run_id": user_input.get("run_id"),
        "agent_trace": [],
        "tags": ["pantry", "weeknight"],
        "rating": None,
        "notes": [],
    }


def run_pantry_session(user_input: Dict[str, Any], schema: Dict[str, Any] | None = None, mock_events: List[Dict[str, Any]] | None = None) -> Iterable[str]:
    """Yield SSE-ready JSON strings as the multi-agent run progresses.

    The OpenAI Agents SDK is streamed when credentials exist; otherwise a local
    deterministic fallback is used to keep development and tests offline.
    """
    run_id = user_input.get("run_id") or str(uuid.uuid4())
    user_input = {**user_input, "run_id": run_id}

    if mock_events is not None:
        for evt in mock_events:
            yield json.dumps(evt)
        return

    events: List[Dict[str, Any]] = []
    yield json.dumps({"type": "status", "stage": "start", "run_id": run_id})

    if not getattr(client, "api_key", None):
        recipe = _fallback_recipe(user_input)
        events.append({"type": "status", "stage": "fallback", "agent": "Router", "run_id": run_id})
        events.append({"type": "final", "run_id": run_id, "recipe": recipe})
        for evt in events:
            yield json.dumps(evt)
        return

    try:
        session = Session(agents=list(AGENTS.values()), metadata={"run_id": run_id})
        result = session.run(AGENTS["Router"], input=user_input)
        for step in result.stream():
            events.append(step)
            yield json.dumps(step)
        final_recipe = getattr(result, "final", lambda: _fallback_recipe(user_input))()
        trace_bundle = build_agent_trace(events, run_id)
        final_recipe.update(trace_bundle)
        if schema:
            validate_recipe_json(final_recipe, schema)
        final_event = {"type": "final", "run_id": run_id, "recipe": final_recipe}
        events.append(final_event)
        yield json.dumps(final_event)
    except Exception as exc:  # pragma: no cover - defensive
        error_evt = {"type": "error", "run_id": run_id, "error": str(exc)}
        yield json.dumps(error_evt)


__all__ = ["run_pantry_session", "PantryRunResult", "AGENTS"]

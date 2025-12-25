import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from openai import OpenAI
try:
    from openai.agents import Agent, Handoff, Session
except ImportError:  # pragma: no cover - fallback for environments without Agents SDK
    class Agent:  # type: ignore
        def __init__(self, *args, **kwargs):
            pass

    class Handoff:  # type: ignore
        def __init__(self, *args, **kwargs):
            pass

    class Session:  # type: ignore
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            raise RuntimeError("OpenAI Agents SDK not available")

from .tools import (
    consolidate_shopping_delta,
    generate_run_id,
    normalize_ingredient,
    nutrition_estimate,
    parse_pantry_input,
    unit_convert,
    validate_recipe_json,
)

OPENAI_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_KEY) if OPENAI_KEY else None

FAST_MODEL = os.getenv("AGENT_FAST_MODEL", "gpt-4.1-mini")
REASON_MODEL = os.getenv("AGENT_REASONING_MODEL", "gpt-4.1")


router = Agent(
    name="Cookbook Orchestrator",
    model=FAST_MODEL,
    instructions="""
You coordinate Pantry Parser → Pantry Matcher → Recipe Generator → Substitutions → Nutritionist → QA.
All dishes are plant-based by default; do not add 'vegan/mock/substitute' prefixes unless requested.
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

AGENTS = {
    "Router": router,
    "PantryParser": pantry_parser,
    "PantryMatcher": pantry_matcher,
    "RecipeGenerator": recipe_generator,
    "Substitutions": substitutions,
    "Nutritionist": nutritionist,
    "QA": qa,
}


@dataclass
class SSEEvent:
    type: str
    run_id: str
    stage: str | None = None
    payload: Dict[str, Any] | None = None

    def to_json(self) -> str:
        data = {"type": self.type, "run_id": self.run_id}
        if self.stage:
            data["stage"] = self.stage
        if self.payload is not None:
            data.update(self.payload)
        return json.dumps(data)


def _local_fallback_recipe(user_input: Dict[str, Any]) -> Dict[str, Any]:
    pantry = parse_pantry_input(user_input.get("pantry", ""))
    name = "Pantry Skillet Supper"
    ingredients = {
        "wet": [{"name": "olive oil", "amount": 2, "units": "tbsp"}],
        "dry": [{"name": "smoked paprika", "amount": 1, "units": "tsp"}],
        "other": [{"name": i["name"], "amount": 1, "units": "cup"} for i in pantry[:2]],
    }
    instructions = [
        "Warm oil in a skillet.",
        "Toss pantry stars with spices.",
        "Simmer until tender and serve hot.",
    ]
    return {
        "name": name,
        "description": "Quick plant-based dinner led by your pantry finds.",
        "prepTime": 10,
        "cookTime": 20,
        "servings": 2,
        "ingredients": ingredients,
        "instructions": instructions,
        "notes": "Auto-generated locally because no OpenAI key was present.",
        "tags": ["pantry", "weeknight"],
    }


def run_pantry_session(user_input: Dict[str, Any], schema: Dict[str, Any] | None = None) -> Iterable[str]:
    """Yield SSE-ready JSON event strings as the multi-agent run progresses."""
    run_id = user_input.get("run_id") or generate_run_id()
    yield SSEEvent(type="status", run_id=run_id, stage="start").to_json()
    if not OPENAI_KEY or not client:
        recipe = _local_fallback_recipe(user_input)
        matches = parse_pantry_input(user_input.get("pantry", ""))
        recipe.update(
            {
                "pantry_matches": matches,
                "pantry_stretchables": [],
                "missing_items": [],
                "shopping_delta": [],
                "tools_equipment": ["skillet", "knife"],
                "difficulty": "easy",
                "prep_time_minutes": 10,
                "cook_time_minutes": 20,
                "total_time_minutes": 30,
                "nutrition": nutrition_estimate([], recipe.get("servings", 2)),
                "agents_involved": list(AGENTS.keys()),
                "run_id": run_id,
                "agent_trace": [{"stage": "local-fallback", "timestamp": time.time()}],
                "rating": None,
                "collections": [],
                "tags": recipe.get("tags", []),
                "variant_of": None,
                "version": "1.0",
            }
        )
        if schema:
            validate_recipe_json(recipe, schema)
        yield SSEEvent(type="status", run_id=run_id, stage="complete").to_json()
        yield SSEEvent(type="final", run_id=run_id, payload={"recipe": recipe}).to_json()
        return

    session = Session(agents=list(AGENTS.values()), metadata={"run_id": run_id})
    orchestrated = session.run(AGENTS["Router"], input=user_input)
    for step in orchestrated.stream():
        yield json.dumps(step)
    final_recipe = orchestrated.final()
    if schema:
        validate_recipe_json(final_recipe, schema)
    yield SSEEvent(type="final", run_id=run_id, payload={"recipe": final_recipe}).to_json()

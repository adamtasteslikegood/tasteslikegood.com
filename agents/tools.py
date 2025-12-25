"""Utility tools registered with the OpenAI Agents SDK.

These helpers are intentionally light-weight and deterministic so they can be
invoked from agents or directly in unit tests without network access.
"""
from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from hashlib import sha1
from typing import Any, Dict, Iterable, List

from jsonschema import validate as js_validate, ValidationError


def parse_pantry_input(text: str) -> List[Dict[str, Any]]:
    """Parse free-text pantry input into coarse structured items.

    The parser is intentionally forgiving; agents may refine the result.
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    items: List[Dict[str, Any]] = []
    for line in lines:
        m = re.match(r"(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-Z]+)?\s+(?P<name>.+)", line)
        if m:
            items.append(
                {
                    "name": m.group("name"),
                    "normalized_name": normalize_ingredient(m.group("name")),
                    "qty": m.group("qty"),
                    "unit": (m.group("unit") or "").lower(),
                    "confidence": 0.72,
                }
            )
        else:
            items.append(
                {
                    "name": line,
                    "normalized_name": normalize_ingredient(line),
                    "qty": "",
                    "unit": "",
                    "confidence": 0.5,
                }
            )
    return items


def normalize_ingredient(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").lower()).strip()


def unit_convert(qty: float, unit: str, target_unit: str) -> Dict[str, str]:
    factor = 1.0 if unit == target_unit else 1.0
    try:
        qty_val = float(qty)
    except Exception:
        qty_val = 0.0
    return {"qty": str(qty_val * factor), "unit": target_unit}


def nutrition_estimate(ingredients: List[Dict[str, Any]], servings: int = 2) -> Dict[str, Any]:
    base = {"calories": 520, "protein_g": 20, "carbs_g": 70, "fat_g": 16, "fiber_g": 14, "sugar_g": 9, "sodium_mg": 780}
    servings = max(servings or 1, 1)
    per = {k: round(v / servings, 2) for k, v in base.items()}
    return {"per_serving": per, "total": base}


def consolidate_shopping_delta(pantry_matches: Iterable[Dict[str, Any]] = None, missing_items: Iterable[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    pantry_matches = pantry_matches or []
    missing_items = missing_items or []
    combined = list(pantry_matches) + list(missing_items)
    out: List[Dict[str, Any]] = []
    index = {}
    for item in combined:
        key = normalize_ingredient(item.get("name", ""))
        if key in index:
            try:
                existing = out[index[key]]
                existing_qty = Decimal(str(existing.get("qty", "0")))
                incoming_qty = Decimal(str(item.get("qty", "0")))
                existing["qty"] = str(existing_qty + incoming_qty)
            except Exception:
                pass
        else:
            index[key] = len(out)
            out.append({"name": item.get("name", ""), "qty": item.get("qty", ""), "unit": item.get("unit", "")})
    return out


def validate_recipe_json(recipe: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    js_validate(instance=recipe, schema=schema)
    return {"valid": True}


def price_lookup(item: str) -> Dict[str, Any]:
    # Mocked price lookup for offline use
    normalized = normalize_ingredient(item)
    cents = int(sha1(normalized.encode()).hexdigest(), 16) % 500 + 100
    return {"item": item, "estimated_cents": cents}


def shopping_delta(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return consolidate_shopping_delta([], items)


def normalize_title(name: str) -> str:
    cleaned = re.sub(r"vegan|mock|substitute", "", (name or ""), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip().title()


def build_agent_trace(events: List[Dict[str, Any]], run_id: str) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "agents_involved": [e.get("agent") for e in events if e.get("agent")],
        "agent_trace": events,
    }


def load_recipe_schema(path: str | None = None) -> Dict[str, Any]:
    schema_path = path or os.getenv("RECIPE_SCHEMA_PATH", "recipe_schema.json")
    with open(schema_path, "r") as fh:
        return json.load(fh)


def validate_against_schema(recipe: Dict[str, Any], path: str | None = None) -> None:
    js_validate(instance=recipe, schema=load_recipe_schema(path))

__all__ = [
    "parse_pantry_input",
    "normalize_ingredient",
    "unit_convert",
    "nutrition_estimate",
    "consolidate_shopping_delta",
    "validate_recipe_json",
    "price_lookup",
    "shopping_delta",
    "normalize_title",
    "build_agent_trace",
    "load_recipe_schema",
    "validate_against_schema",
]

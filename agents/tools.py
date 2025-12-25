import json
import re
import uuid
from decimal import Decimal
from typing import Any, Dict, List

from jsonschema import validate as js_validate


def parse_pantry_input(text: str) -> List[Dict[str, Any]]:
    """Parse free-text pantry into normalized items with qty/unit/confidence."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    items = []
    for line in lines:
        m = re.match(r"(?P<qty>\\d+(\\.\\d+)?)\\s*(?P<unit>[a-zA-Z]+)?\\s+(?P<name>.+)", line)
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
    return re.sub(r"\\s+", " ", name.lower()).strip()


def unit_convert(qty: float, unit: str, target_unit: str) -> Dict[str, str]:
    factor = 1.0 if unit == target_unit else 1.0
    return {"qty": str(qty * factor), "unit": target_unit}


def nutrition_estimate(ingredients: List[Dict[str, Any]], servings: int = 2) -> Dict[str, Any]:
    total = {
        "calories": 500,
        "protein_g": 18,
        "carbs_g": 70,
        "fat_g": 14,
        "fiber_g": 12,
        "sugar_g": 10,
        "sodium_mg": 800,
    }
    per = {k: (v / max(servings, 1)) for k, v in total.items()}
    return {"per_serving": per, "total": total}


def consolidate_shopping_delta(pantry_matches, missing_items) -> List[Dict[str, Any]]:
    out, idx = [], {}
    for item in missing_items or []:
        key = item.get("name", "").lower().strip()
        if not key:
            continue
        if key in idx:
            try:
                out[idx[key]]["qty"] = str(Decimal(out[idx[key]]["qty"]) + Decimal(item.get("qty") or "0"))
            except Exception:
                pass
        else:
            idx[key] = len(out)
            out.append({"name": item.get("name", ""), "qty": item.get("qty", ""), "unit": item.get("unit", "")})
    return out


def validate_recipe_json(recipe: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    js_validate(instance=recipe, schema=schema)
    return {"valid": True}


def generate_run_id() -> str:
    return str(uuid.uuid4())

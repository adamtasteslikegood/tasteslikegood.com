"""Cookbook persistence helpers (JSON-backed).

The module keeps the surface small so it can later be swapped for SQLite. All
IDs are UUID4; slugs can be derived from titles for readability.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

COOKBOOK_DIR = os.getenv("COOKBOOK_DIR", "cookbooks")
RECIPE_DIR = os.getenv("RECIPES_DIR", "recipes")
os.makedirs(COOKBOOK_DIR, exist_ok=True)
os.makedirs(RECIPE_DIR, exist_ok=True)


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def _write_json(path: str, payload: Any):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)


def _slugify(text: str) -> str:
    return "-".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split()) or "cookbook"


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class Cookbook:
    id: str
    title: str
    owner_session_id: str
    collections: List[str]
    tags: List[str]
    default_preferences: Dict[str, Any]
    recipes: List[str]

    @classmethod
    def create(cls, title: str, owner_session_id: str, **kwargs):
        return cls(
            id=new_id(),
            title=title,
            owner_session_id=owner_session_id,
            collections=kwargs.get("collections", []),
            tags=kwargs.get("tags", []),
            default_preferences=kwargs.get("default_preferences", {}),
            recipes=kwargs.get("recipes", []),
        )


@dataclass
class RecipeEnvelope:
    id: str
    data: Dict[str, Any]


def save_recipe(data: Dict[str, Any]) -> RecipeEnvelope:
    recipe_id = data.get("id") or new_id()
    data = {**data, "id": recipe_id}
    path = os.path.join(RECIPE_DIR, f"{recipe_id}.json")
    _write_json(path, data)
    return RecipeEnvelope(id=recipe_id, data=data)


def load_recipe(recipe_id: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(RECIPE_DIR, f"{recipe_id}.json")
    if not os.path.exists(path):
        return None
    return _load_json(path, {})


def save_cookbook(cookbook: Cookbook) -> Cookbook:
    path = os.path.join(COOKBOOK_DIR, f"{cookbook.id}.json")
    _write_json(path, asdict(cookbook))
    return cookbook


def list_cookbooks() -> List[Dict[str, Any]]:
    out = []
    for filename in os.listdir(COOKBOOK_DIR):
        if filename.endswith(".json"):
            out.append(_load_json(os.path.join(COOKBOOK_DIR, filename), {}))
    return out


def attach_recipe_to_cookbook(cookbook_id: str, recipe_id: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(COOKBOOK_DIR, f"{cookbook_id}.json")
    data = _load_json(path, None)
    if data is None:
        return None
    if recipe_id not in data.get("recipes", []):
        data.setdefault("recipes", []).append(recipe_id)
        _write_json(path, data)
    return data


def delete_recipe(recipe_id: str) -> bool:
    path = os.path.join(RECIPE_DIR, f"{recipe_id}.json")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def update_recipe(recipe_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    current = load_recipe(recipe_id)
    if current is None:
        return None
    current.update(updates)
    save_recipe(current)
    return current


def cookbook_schema_stub() -> Dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "Cookbook",
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "owner_session_id": {"type": "string"},
            "collections": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "default_preferences": {"type": "object"},
            "recipes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["id", "title", "owner_session_id", "recipes"],
    }

__all__ = [
    "Cookbook",
    "RecipeEnvelope",
    "save_recipe",
    "load_recipe",
    "save_cookbook",
    "list_cookbooks",
    "attach_recipe_to_cookbook",
    "delete_recipe",
    "update_recipe",
    "cookbook_schema_stub",
]

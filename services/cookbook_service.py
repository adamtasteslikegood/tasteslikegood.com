import json
import os
import uuid
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

COOKBOOK_DIR = "cookbooks"
RECIPES_DIR = "recipes"
os.makedirs(COOKBOOK_DIR, exist_ok=True)
os.makedirs(RECIPES_DIR, exist_ok=True)


def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
        return data


def _write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def generate_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Cookbook:
    id: str
    title: str
    owner_session_id: str = "anonymous"
    collections: List[str] | None = None
    tags: List[str] | None = None
    default_preferences: Dict[str, Any] | None = None
    recipes: List[str] | None = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def load_cookbook(cookbook_id: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(COOKBOOK_DIR, f"{cookbook_id}.json")
    if not os.path.exists(path):
        return None
    return _read_json(path)


def list_cookbooks() -> List[Dict[str, Any]]:
    books = []
    for fname in os.listdir(COOKBOOK_DIR):
        if not fname.endswith(".json"):
            continue
        books.append(_read_json(os.path.join(COOKBOOK_DIR, fname)))
    return books


def save_cookbook(cookbook: Dict[str, Any]) -> Dict[str, Any]:
    cid = cookbook.get("id") or generate_id("cookbook")
    cookbook["id"] = cid
    path = os.path.join(COOKBOOK_DIR, f"{cid}.json")
    _write_json(path, cookbook)
    return cookbook


def save_recipe(recipe: Dict[str, Any]) -> Dict[str, Any]:
    rid = recipe.get("id") or generate_id("recipe")
    recipe["id"] = rid
    path = os.path.join(RECIPES_DIR, f"{rid}.json")
    _write_json(path, recipe)
    return recipe


def delete_recipe(recipe_id: str) -> bool:
    path = os.path.join(RECIPES_DIR, f"{recipe_id}.json")
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def patch_recipe(recipe_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    path = os.path.join(RECIPES_DIR, f"{recipe_id}.json")
    if not os.path.exists(path):
        return None
    data = _read_json(path)
    data.update(updates)
    _write_json(path, data)
    return data


def attach_recipe_to_cookbook(cookbook_id: str, recipe_id: str) -> None:
    cb = load_cookbook(cookbook_id)
    if not cb:
        cb = Cookbook(id=cookbook_id, title="Untitled").to_dict()
    recipes = cb.get("recipes") or []
    if recipe_id not in recipes:
        recipes.append(recipe_id)
    cb["recipes"] = recipes
    save_cookbook(cb)

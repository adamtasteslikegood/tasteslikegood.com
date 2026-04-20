"""
RecipePresenter service for v0.2 Anti-Recipe Site.
Handles transformation of Recipe models into SEO-friendly formats (JSON-LD, Meta tags).
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional


class RecipePresenter:
    @staticmethod
    def to_iso8601_duration(minutes: Optional[int]) -> str:
        """Convert minutes to ISO 8601 duration string (e.g., 30 -> PT30M)."""
        if not minutes or minutes <= 0:
            return "PT0M"
        
        hours = minutes // 60
        mins = minutes % 60
        
        duration = "PT"
        if hours > 0:
            duration += f"{hours}H"
        if mins > 0:
            duration += f"{mins}M"
        
        return duration

    @classmethod
    def get_json_ld(cls, recipe: Any, base_url: str) -> Dict[str, Any]:
        """Generate Schema.org Recipe JSON-LD."""
        data = recipe.data or {}
        
        # Flatten ingredients if they are grouped
        ingredients = []
        if isinstance(data.get("ingredients"), dict):
            for group in data["ingredients"].values():
                for ing in group:
                    amount = ing.get("amount", "")
                    if isinstance(amount, list):
                        amount = f"{amount[0]}-{amount[1]}"
                    ingredients.append(f"{amount} {ing.get('units', '')} {ing.get('name', '')}".strip())
        
        # Flatten instructions
        instructions = []
        for i, step in enumerate(data.get("instructions", [])):
            text = step.get("description", "") if isinstance(step, dict) else str(step)
            instructions.append({
                "@type": "HowToStep",
                "text": text,
                "position": i + 1,
                "url": f"{base_url}/r/{recipe.slug}#step-{i+1}"
            })

        json_ld = {
            "@context": "https://schema.org/",
            "@type": "Recipe",
            "name": recipe.name,
            "image": [
                f"{base_url}/api/recipes/{recipe.id}/image" if data.get("ai_image_url") else data.get("stock_image_url", "")
            ],
            "author": {
                "@type": "Organization",
                "name": "VeganGenius Chef"
            },
            "datePublished": recipe.created_at.strftime('%Y-%m-%d') if recipe.created_at else None,
            "description": data.get("description", ""),
            "prepTime": cls.to_iso8601_duration(data.get("prepTime")),
            "cookTime": cls.to_iso8601_duration(data.get("cookTime")),
            "totalTime": cls.to_iso8601_duration(data.get("prepTime", 0) + data.get("cookTime", 0)),
            "recipeYield": f"{data.get('servings', 1)} servings",
            "recipeCategory": "Main Course", # Default
            "recipeCuisine": "Vegan",
            "keywords": ", ".join(data.get("tags", [])),
            "recipeIngredient": ingredients,
            "recipeInstructions": instructions
        }
        
        return json_ld

    @staticmethod
    def get_meta_tags(recipe: Any, base_url: str) -> Dict[str, str]:
        """Generate Meta tags for OG and Twitter cards."""
        data = recipe.data or {}
        image_url = f"{base_url}/api/recipes/{recipe.id}/image" if data.get("ai_image_url") else data.get("stock_image_url", "")
        
        return {
            "title": f"{recipe.name} — VeganGenius Chef",
            "description": data.get("description", "")[:160],
            "og:title": recipe.name,
            "og:description": data.get("description", "")[:200],
            "og:image": image_url,
            "og:url": f"{base_url}/r/{recipe.slug}",
            "og:type": "website",
            "twitter:card": "summary_large_image",
            "twitter:title": recipe.name,
            "twitter:description": data.get("description", "")[:200],
            "twitter:image": image_url,
            "canonical": f"{base_url}/r/{recipe.slug}"
        }

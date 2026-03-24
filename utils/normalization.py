import difflib

# Canonical units and their common variations
UNIT_MAPPINGS = {
    "cup": ["c", "cp", "cups"],
    "tsp": ["teaspoon", "teaspoons", "t", "ts", "teasp"],
    "Tbsp": ["tablespoon", "tablespoons", "T", "tbl", "tbs"],
    "oz": ["ounce", "ounces"],
    "g": ["gram", "grams"],
    "kg": ["kilogram", "kilograms", "kilo"],
    "ml": ["milliliter", "milliliters", "milli"],
    "l": ["liter", "liters"],
    "lb": ["pound", "pounds"],
    "pinch": ["pinches"],
    "qty": ["quantity", "count", "whole", "pcs", "piece", "pieces"],
    "to taste": ["taste", "to-taste"],
}

# Reverse mapping for faster lookup
CANONICAL_UNITS = {}
for canonical, variations in UNIT_MAPPINGS.items():
    CANONICAL_UNITS[canonical.lower()] = canonical
    for v in variations:
        CANONICAL_UNITS[v.lower()] = canonical


def normalize_unit(unit_str):
    """
    Normalizes a unit string to a canonical form using exact lookup
    and fuzzy matching.
    """
    if not unit_str:
        return "qty"  # Default to quantity/pieces if missing

    unit_lower = unit_str.lower().replace(".", "").strip()

    # 1. Exact match
    if unit_lower in CANONICAL_UNITS:
        return CANONICAL_UNITS[unit_lower]

    # 2. Fuzzy match
    # Get all possible keys
    all_keys = list(CANONICAL_UNITS.keys())
    matches = difflib.get_close_matches(unit_lower, all_keys, n=1, cutoff=0.8)

    if matches:
        return CANONICAL_UNITS[matches[0]]

    # 3. Fallback: Return original cleaned string
    return unit_str.strip()


def parse_amount(amount_val):
    """
    Parses amount which can be number, string (fraction/range), or list.
    Returns a float or a list of two floats (range).
    """
    if isinstance(amount_val, (int, float)):
        return amount_val

    if isinstance(amount_val, list):
        # Ensure all elements are numbers
        try:
            return [float(x) for x in amount_val]
        except (ValueError, TypeError):
            return 0  # Fallback

    if isinstance(amount_val, str):
        val = amount_val.strip()

        # Handle fractions like "1/2"
        if "/" in val:
            try:
                num, den = val.split("/")
                return float(num) / float(den)
            except ValueError:
                pass

        # Handle ranges like "1-2"
        if "-" in val:
            try:
                parts = val.split("-")
                if len(parts) == 2:
                    return [float(parts[0]), float(parts[1])]
            except ValueError:
                pass

        # Try direct float conversion
        try:
            return float(val)
        except ValueError:
            pass

    # Default fallback
    return 0


def normalize_recipe_data(data):
    """
    Normalizes the entire recipe data structure to be more compliant
    with the schema and application expectations.
    """
    if not isinstance(data, dict):
        return data  # Can't do much if it's not a dict

    # 0. Fuzzy Match Top-Level Keys
    # Map common variations to schema keys
    key_mapping = {
        "prepTime": ["prep_time", "preparation_time", "prep", "time_prep"],
        "cookTime": ["cook_time", "cooking_time", "cook", "time_cook"],
        "servings": ["serving", "yield", "yields", "portion", "portions"],
        "ingredients": ["ingredents", "component", "components", "items", "materials"],
        "instructions": ["instruction", "step", "steps", "method", "directions", "procedure"],
        "description": ["desc", "summary", "intro"],
        "notes": ["note", "tips", "variation", "variations"],
    }

    # Iterate over current keys and see if they match a known variation
    # We create a new dict to avoid modification issues during iteration
    new_data = data.copy()

    # Check for missing canonical keys
    for canonical, variations in key_mapping.items():
        if canonical not in new_data:
            # Look for a variation
            found_val = None
            found_key = None

            # 1. Exact variation match
            for v in variations:
                if v in new_data:
                    found_val = new_data[v]
                    found_key = v
                    break

            # 2. If not found, try fuzzy match on all keys
            if not found_key:
                # Get all keys in data
                data_keys = list(new_data.keys())
                # Try to find close match for canonical key itself
                matches = difflib.get_close_matches(
                    canonical.lower(), [k.lower() for k in data_keys], n=1, cutoff=0.85
                )
                if matches:
                    # Find the original case-sensitive key
                    for k in data_keys:
                        if k.lower() == matches[0]:
                            found_key = k
                            found_val = new_data[k]
                            break

            # If we found a value under a different key, move it
            if found_key:
                new_data[canonical] = found_val
                # Optional: Remove old key? Maybe keep it to be safe,
                # but for 'ingredients' we want to process the canonical one.
                # Let's keep it but ensure we use canonical for processing.

    data = new_data

    # 1. Top-level Defaults
    data.setdefault("name", "Untitled Recipe")
    data.setdefault("description", "No description provided.")
    data.setdefault("prepTime", 0)
    data.setdefault("cookTime", 0)
    data.setdefault("servings", 4)
    data.setdefault("instructions", [])

    # 2. Normalize Ingredients
    if "ingredients" not in data:
        data["ingredients"] = {"wet": [], "dry": [], "other": []}
    elif isinstance(data["ingredients"], list):
        # Fix: Some models return a flat list instead of categorized object
        # Move them all to 'other' for safety
        flat_list = data["ingredients"]
        data["ingredients"] = {"wet": [], "dry": [], "other": flat_list}

    # Iterate through ingredient groups
    for group in ["wet", "dry", "other"]:
        if group in data["ingredients"]:
            if not isinstance(data["ingredients"][group], list):
                data["ingredients"][group] = []

            new_list = []
            for item in data["ingredients"][group]:
                if isinstance(item, str):
                    # Handle case where ingredient is just a string
                    new_list.append({"name": item, "amount": 0, "units": "qty"})
                    continue

                if not isinstance(item, dict):
                    continue

                # Normalize Amount
                item["amount"] = parse_amount(item.get("amount", 0))

                # Normalize Unit
                item["units"] = normalize_unit(item.get("units", ""))

                # Ensure Name
                if "name" not in item or not item["name"]:
                    item["name"] = "Unknown Ingredient"

                new_list.append(item)
            data["ingredients"][group] = new_list

    # 3. Normalize Instructions
    if isinstance(data["instructions"], str):
        # Split string into list? Or wrap?
        data["instructions"] = [data["instructions"]]

    return data

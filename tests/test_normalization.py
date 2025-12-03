import unittest
from utils import normalize_unit, parse_amount, normalize_recipe_data

class TestNormalization(unittest.TestCase):
    
    def test_normalize_unit_exact(self):
        self.assertEqual(normalize_unit("tbsp"), "Tbsp")
        self.assertEqual(normalize_unit("Tablespoon"), "Tbsp")
        self.assertEqual(normalize_unit("oz"), "oz")
        
    def test_normalize_unit_fuzzy(self):
        self.assertEqual(normalize_unit("tblsp"), "Tbsp")
        self.assertEqual(normalize_unit("gramme"), "g")
        self.assertEqual(normalize_unit("teasp"), "tsp")
        
    def test_normalize_unit_fallback(self):
        self.assertEqual(normalize_unit("unknown_unit"), "unknown_unit")
        self.assertEqual(normalize_unit(""), "qty")
        
    def test_parse_amount_number(self):
        self.assertEqual(parse_amount(1.5), 1.5)
        self.assertEqual(parse_amount(10), 10)
        
    def test_parse_amount_string_fraction(self):
        self.assertEqual(parse_amount("1/2"), 0.5)
        self.assertEqual(parse_amount("3/4"), 0.75)
        
    def test_parse_amount_string_range(self):
        self.assertEqual(parse_amount("1-2"), [1.0, 2.0])
        self.assertEqual(parse_amount("10 - 12"), [10.0, 12.0])
        
    def test_parse_amount_fallback(self):
        self.assertEqual(parse_amount("invalid"), 0)
        self.assertEqual(parse_amount(None), 0)
        
    def test_normalize_recipe_defaults(self):
        data = {}
        normalized = normalize_recipe_data(data)
        self.assertEqual(normalized['name'], 'Untitled Recipe')
        self.assertEqual(normalized['servings'], 4)
        self.assertIn('ingredients', normalized)
        self.assertEqual(normalized['ingredients']['wet'], [])
        
    def test_normalize_recipe_flat_ingredients(self):
        data = {
            "ingredients": [
                {"name": "flour", "amount": 1, "units": "cup"},
                "salt"
            ]
        }
        normalized = normalize_recipe_data(data)
        self.assertEqual(len(normalized['ingredients']['other']), 2)
        # Check string conversion
        self.assertEqual(normalized['ingredients']['other'][1]['name'], 'salt')
        self.assertEqual(normalized['ingredients']['other'][1]['amount'], 0)
        
    def test_normalize_recipe_nested_ingredients(self):
        data = {
            "ingredients": {
                "wet": [
                    {"name": "water", "amount": "1/2", "units": "c"},
                    {"name": "oil", "amount": "1-2", "units": "Tbsp"}
                ]
            }
        }
        normalized = normalize_recipe_data(data)
        wet = normalized['ingredients']['wet']
        self.assertEqual(wet[0]['amount'], 0.5)
        self.assertEqual(wet[0]['units'], 'cup') # "c" -> "cup"
        self.assertEqual(wet[1]['amount'], [1.0, 2.0])
        self.assertEqual(wet[1]['units'], 'Tbsp')

    def test_normalize_keys_fuzzy(self):
        data = {
            "prep_time": 10,
            "cooking_time": 20,
            "yields": 6,
            "steps": ["Mix", "Bake"]
        }
        normalized = normalize_recipe_data(data)
        self.assertEqual(normalized['prepTime'], 10)
        self.assertEqual(normalized['cookTime'], 20)
        self.assertEqual(normalized['servings'], 6)
        self.assertEqual(normalized['instructions'], ["Mix", "Bake"])

if __name__ == '__main__':
    unittest.main()
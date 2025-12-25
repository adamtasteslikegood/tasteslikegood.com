import json
import unittest

from agents.cookbook_agents import run_pantry_session


class TestPantryAgents(unittest.TestCase):
    def test_local_fallback_stream(self):
        payload = {"pantry": "1 cup oats\n2 tbsp tahini"}
        events = list(run_pantry_session(payload))
        self.assertGreaterEqual(len(events), 2)
        final = json.loads(events[-1])
        self.assertEqual(final["type"], "final")
        self.assertIn("recipe", final)
        self.assertEqual(final["recipe"]["ingredients"]["wet"][0]["name"], "olive oil")

    def test_schema_fields_present(self):
        payload = {"pantry": "can tomatoes"}
        final = json.loads(list(run_pantry_session(payload))[-1])
        recipe = final["recipe"]
        self.assertIn("pantry_matches", recipe)
        self.assertIn("nutrition", recipe)
        self.assertEqual(recipe["difficulty"], "easy")


if __name__ == "__main__":
    unittest.main()

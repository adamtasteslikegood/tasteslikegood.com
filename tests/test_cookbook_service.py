import json
import os
import tempfile
import unittest

from services import cookbook_service as service


class TestCookbookService(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        service.COOKBOOK_DIR = os.path.join(self.tmpdir.name, "cookbooks")
        service.RECIPES_DIR = os.path.join(self.tmpdir.name, "recipes")
        os.makedirs(service.COOKBOOK_DIR, exist_ok=True)
        os.makedirs(service.RECIPES_DIR, exist_ok=True)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_save_and_list_cookbook(self):
        book = {"title": "Weeknight", "owner_session_id": "anon"}
        saved = service.save_cookbook(book)
        self.assertIn("id", saved)
        listed = service.list_cookbooks()
        self.assertEqual(len(listed), 1)

    def test_save_recipe_and_attach(self):
        recipe = {"name": "Soup", "prepTime": 1, "cookTime": 1, "servings": 1, "ingredients": {"wet": [], "dry": []}, "instructions": []}
        saved = service.save_recipe(recipe)
        service.attach_recipe_to_cookbook("default", saved["id"])
        cookbook = service.load_cookbook("default")
        self.assertIn(saved["id"], cookbook["recipes"])

    def test_patch_and_delete(self):
        recipe = {"name": "Stew", "prepTime": 1, "cookTime": 1, "servings": 1, "ingredients": {"wet": [], "dry": []}, "instructions": []}
        saved = service.save_recipe(recipe)
        updated = service.patch_recipe(saved["id"], {"name": "Bright Stew"})
        self.assertEqual(updated["name"], "Bright Stew")
        ok = service.delete_recipe(saved["id"])
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()

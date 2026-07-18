"""Path-containment contract for the file-based recipe repository.

validate_recipe_filepath() is the single barrier between user-supplied
filenames and filesystem access (open/exists in api_bp image routes). It must
return a normalized absolute path inside RECIPES_DIR and raise a constant
message — the original exception text is echoed nowhere, only logged.
"""

import os
import sys
from pathlib import Path

import pytest

# Add the project root directory to sys.path (repo test convention)
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config import RECIPES_DIR  # noqa: E402
from repositories.recipe_repository import validate_recipe_filepath  # noqa: E402


def test_valid_filename_resolves_inside_recipes_dir():
    filepath = validate_recipe_filepath("thai_peanut_noodles.json")
    assert os.path.isabs(filepath)
    assert filepath.startswith(os.path.abspath(RECIPES_DIR) + os.sep)
    assert filepath.endswith("thai_peanut_noodles.json")


@pytest.mark.parametrize(
    "hostile",
    [
        "../secrets.json",
        "..%2F..%2Fetc%2Fpasswd",
        "/etc/passwd",
        "nested/dir/recipe.json",
        "no_json_suffix.txt",
        "",
        ".json",  # basename-only, but empty stem still ends with .json
    ],
)
def test_hostile_filenames_rejected_or_contained(hostile):
    try:
        filepath = validate_recipe_filepath(hostile)
    except ValueError as e:
        # Constant message: no reflection of input or inner exception detail
        assert str(e) == "Invalid filename"
    else:
        # If accepted (basename() may reduce it to a harmless name),
        # the resolved path must still be inside RECIPES_DIR
        assert filepath.startswith(os.path.abspath(RECIPES_DIR) + os.sep)


def test_image_filename_unicode_names_do_not_collide():
    """secure_filename strips non-ASCII; the digest suffix must keep distinct
    Unicode recipe names on distinct image paths (Copilot review on PR #213)."""
    from services.image_service import _image_filename

    sushi = _image_filename("寿司.json")
    curry = _image_filename("咖喱.json")
    assert sushi != curry
    for name in (sushi, curry):
        assert name.startswith("ai_")
        assert name.endswith(".png")
        assert "/" not in name and ".." not in name


def test_image_filename_ascii_names_unchanged():
    from services.image_service import _image_filename

    assert _image_filename("test.json") == "ai_test.png"
    assert _image_filename("thai_peanut_noodles.json") == "ai_thai_peanut_noodles.png"

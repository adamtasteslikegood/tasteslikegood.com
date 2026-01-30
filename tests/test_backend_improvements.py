import pytest
import os
import json
from services.reporting_service import ReportingService
from services.migration_service import MigrationService
from repositories.recipe_repository import save_recipe, get_recipe, RECIPES_DIR

def test_reporting_service_logic(tmp_path):
    # Mock reports file
    ReportingService.REPORTS_FILE = str(tmp_path / "user_reports.json")
    
    # Test report submission
    result = ReportingService.report_recipe("test.json", "Bad instructions", "user123")
    assert result['message'] == 'Report submitted successfully'
    
    # Verify file content
    with open(ReportingService.REPORTS_FILE, 'r') as f:
        reports = json.load(f)
        assert len(reports) == 1
        assert reports[0]['filename'] == "test.json"
        assert reports[0]['reason'] == "Bad instructions"
        assert reports[0]['user_id'] == "user123"

def test_migration_service_logic(tmp_path, monkeypatch):
    # Setup mock recipes dir
    mock_recipes_dir = tmp_path / "recipes"
    mock_recipes_dir.mkdir()
    monkeypatch.setattr("services.migration_service.RECIPES_DIR", str(mock_recipes_dir))
    monkeypatch.setattr("repositories.recipe_repository.RECIPES_DIR", str(mock_recipes_dir))
    
    # Create a legacy recipe
    legacy_recipe = {
        "properties": {
            "name": "Legacy Recipe",
            "ingredients": ["test"]
        }
    }
    recipe_file = "legacy.json"
    with open(mock_recipes_dir / recipe_file, "w") as f:
        json.dump(legacy_recipe, f)
    
    # Run migration
    results = MigrationService.migrate_all_recipes()
    assert results['migrated_count'] == 1
    assert recipe_file in results['files']
    
    # Verify migrated content
    with open(mock_recipes_dir / recipe_file, "r") as f:
        migrated = json.load(f)
        assert "properties" not in migrated
        assert migrated['name'] == "Legacy Recipe"
        assert "user_id" in migrated
        assert "ai_metadata" in migrated

"""Tests for the GitHub Actions workflow-run filtering helpers."""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from app import apply_status_filter, fetch_workflow_runs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def sample_runs():
    """A small set of workflow-run dicts that cover several status/conclusion
    combinations."""
    return [
        {"id": 1, "status": "completed", "conclusion": "success",  "name": "CI"},
        {"id": 2, "status": "completed", "conclusion": "failure",  "name": "CI"},
        {"id": 3, "status": "completed", "conclusion": "cancelled", "name": "CI"},
        {"id": 4, "status": "in_progress", "conclusion": None,     "name": "CI"},
        {"id": 5, "status": "queued",     "conclusion": None,      "name": "CI"},
        {"id": 6, "status": "completed", "conclusion": "success",  "name": "Deploy"},
    ]


# ---------------------------------------------------------------------------
# apply_status_filter tests
# ---------------------------------------------------------------------------

class TestApplyStatusFilter:
    def test_no_filter_returns_all_runs(self, sample_runs):
        result = apply_status_filter(sample_runs, filter_status="", filter_mode="include")
        assert result == sample_runs

    def test_include_success_returns_only_successful_runs(self, sample_runs):
        result = apply_status_filter(sample_runs, filter_status="success", filter_mode="include")
        assert len(result) == 2
        assert all(r["conclusion"] == "success" for r in result)

    def test_exclude_success_returns_all_non_successful_runs(self, sample_runs):
        """Inverse filter: show every run whose status/conclusion is NOT 'success'."""
        result = apply_status_filter(sample_runs, filter_status="success", filter_mode="exclude")
        assert len(result) == 4
        assert all(r["conclusion"] != "success" for r in result)

    def test_include_failure(self, sample_runs):
        result = apply_status_filter(sample_runs, filter_status="failure", filter_mode="include")
        assert len(result) == 1
        assert result[0]["id"] == 2

    def test_exclude_failure(self, sample_runs):
        result = apply_status_filter(sample_runs, filter_status="failure", filter_mode="exclude")
        assert len(result) == 5
        assert not any(r["conclusion"] == "failure" for r in result)

    def test_include_in_progress_matches_on_status_field(self, sample_runs):
        """'in_progress' is a *status* value (not conclusion); should still match."""
        result = apply_status_filter(sample_runs, filter_status="in_progress", filter_mode="include")
        assert len(result) == 1
        assert result[0]["id"] == 4

    def test_exclude_in_progress(self, sample_runs):
        result = apply_status_filter(sample_runs, filter_status="in_progress", filter_mode="exclude")
        assert len(result) == 5
        assert not any(r.get("status") == "in_progress" for r in result)

    def test_unknown_filter_mode_defaults_to_include(self, sample_runs):
        """An unrecognised filter_mode should fall back to include semantics."""
        result = apply_status_filter(sample_runs, filter_status="success", filter_mode="bogus")
        assert len(result) == 2

    def test_filter_status_with_no_matches_returns_empty_list(self, sample_runs):
        result = apply_status_filter(sample_runs, filter_status="timed_out", filter_mode="include")
        assert result == []

    def test_exclude_filter_with_no_matches_returns_all_runs(self, sample_runs):
        result = apply_status_filter(sample_runs, filter_status="timed_out", filter_mode="exclude")
        assert result == sample_runs


# ---------------------------------------------------------------------------
# fetch_workflow_runs tests  (GitHub API is always mocked)
# ---------------------------------------------------------------------------

class TestFetchWorkflowRuns:
    def test_successful_fetch_returns_runs(self):
        mock_payload = {
            "workflow_runs": [
                {"id": 1, "status": "completed", "conclusion": "success"},
            ]
        }
        with patch("app.requests.get") as mock_get:
            mock_get.return_value.ok = True
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = mock_payload

            runs, error = fetch_workflow_runs("owner", "repo")

        assert error is None
        assert len(runs) == 1
        assert runs[0]["conclusion"] == "success"

    def test_404_returns_descriptive_error(self):
        with patch("app.requests.get") as mock_get:
            mock_get.return_value.ok = False
            mock_get.return_value.status_code = 404

            runs, error = fetch_workflow_runs("bad-owner", "bad-repo")

        assert runs == []
        assert "not found" in error.lower()

    def test_403_returns_rate_limit_error(self):
        with patch("app.requests.get") as mock_get:
            mock_get.return_value.ok = False
            mock_get.return_value.status_code = 403

            runs, error = fetch_workflow_runs("owner", "repo")

        assert runs == []
        assert "rate limit" in error.lower() or "permission" in error.lower()

    def test_connection_error_returns_message(self):
        import requests as req_lib
        with patch("app.requests.get", side_effect=req_lib.exceptions.ConnectionError):
            runs, error = fetch_workflow_runs("owner", "repo")

        assert runs == []
        assert "connect" in error.lower()

    def test_timeout_returns_message(self):
        import requests as req_lib
        with patch("app.requests.get", side_effect=req_lib.exceptions.Timeout):
            runs, error = fetch_workflow_runs("owner", "repo")

        assert runs == []
        assert "timed out" in error.lower()

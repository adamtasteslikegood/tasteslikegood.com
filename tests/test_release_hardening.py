from pathlib import Path
import re
import sys
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import pubsub_service


def test_pubsub_publish_fails_closed_without_project_id(monkeypatch):
    get_publisher = Mock()
    monkeypatch.setattr(pubsub_service, "GCP_PROJECT_ID", "")
    monkeypatch.setattr(pubsub_service, "_get_publisher", get_publisher)

    with pytest.raises(RuntimeError, match="GCP_PROJECT_ID"):
        pubsub_service.publish_message("recipe-generation", {"recipe_id": "r-1"})

    get_publisher.assert_not_called()


def test_release_workflows_pin_third_party_actions():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    dependabot = (ROOT / ".github/workflows/dependabot-auto-merge.yml").read_text(encoding="utf-8")

    assert not re.search(r"^\s*uses:\s*actions/checkout@v", ci, re.MULTILINE)
    assert not re.search(r"^\s*uses:\s*astral-sh/setup-uv@v", ci, re.MULTILINE)
    assert not re.search(r"^\s*uses:\s*actions/upload-artifact@v", ci, re.MULTILINE)
    assert not re.search(
        r"^\s*uses:\s*dependabot/fetch-metadata@v",
        dependabot,
        re.MULTILINE,
    )


def test_gemini_execute_is_bound_to_trusted_plan_comment():
    workflow = (ROOT / ".github/workflows/gemini-invoke.yml").read_text(encoding="utf-8")
    execute_prompt = (ROOT / ".github/commands/tlg-invoke-execute.toml").read_text(encoding="utf-8")

    assert "Verify approval references a trusted plan" in workflow
    assert "gemini-plan-link:" in workflow
    assert ".user.login == $bot" in workflow
    assert 'TRUSTED_BOT_LOGIN="${APP_SLUG}[bot]"' in workflow
    assert "steps.mint_identity_token.outputs['app-slug']" in workflow
    assert "permission-contents: 'write'" in workflow
    assert "contents: 'write'" in workflow
    assert '"delete_file"' not in workflow
    assert '"fork_repository"' not in workflow
    assert "Approved Plan Comment ID" in execute_prompt
    assert "Never substitute a newer or similarly titled comment" in execute_prompt


def test_manual_gemini_review_checks_out_resolved_pull_request_head():
    workflow = (ROOT / ".github/workflows/gemini-review.yml").read_text(encoding="utf-8")

    assert "core.setOutput('head_sha', pr.head.sha)" in workflow
    assert "ref: '${{ steps.pr.outputs.head_sha }}'" in workflow

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
    mutable_refs = []
    for pattern in ("*.yml", "*.yaml"):
        for path in (ROOT / ".github/workflows").glob(pattern):
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if re.search(r"^\s*uses:\s*['\"]?[^'\"\s]+@v\d", line):
                    mutable_refs.append(f"{path.name}:{line_number}: {line.strip()}")
    assert mutable_refs == []

    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / ".github/workflows").glob("gemini-*.yml")
    )
    assert "ghcr.io/github/github-mcp-server:v0.18.0" not in workflows
    assert (
        "ghcr.io/github/github-mcp-server@sha256:"
        "5228f9e0b057d419e0e1919729493f75674304181f0ac85ae8385d89dd86affc"
    ) in workflows


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
    assert "github.event.comment.body == '@gemini-cli'" in workflow
    assert "startsWith(github.event.comment.body, '@gemini-cli ')" in workflow
    assert "contains(github.event.comment.body, '@gemini-cli')" not in workflow
    assert "run_shell_command" not in workflow
    assert workflow.count("persist-credentials: 'false'") == 2

    triage_workflow = (ROOT / ".github/workflows/gemini-triage.yml").read_text(encoding="utf-8")
    assert "github.rest.issues.addLabels" in triage_workflow
    assert "github.rest.issues.setLabels" not in triage_workflow

    scheduled_triage = (ROOT / ".github/workflows/gemini-scheduled-triage.yml").read_text(
        encoding="utf-8"
    )
    assert "--search 'no:label'" in scheduled_triage
    assert "--search 'label:\"status/needs-triage\"'" in scheduled_triage
    assert 'no:label label:"status/needs-triage"' not in scheduled_triage
    assert "github.rest.issues.addLabels" in scheduled_triage
    assert "github.rest.issues.setLabels" not in scheduled_triage
    assert "github.rest.issues.removeLabel" in scheduled_triage


def test_manual_gemini_review_uses_sandboxed_github_tools():
    workflow = (ROOT / ".github/workflows/gemini-review.yml").read_text(encoding="utf-8")

    assert "Checkout repository" not in workflow
    assert "run_shell_command" not in workflow
    assert '"core": []' in workflow
    assert '"pull_request_read"' in workflow

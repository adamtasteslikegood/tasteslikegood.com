"""Standalone Flask application for browsing GitHub Actions workflow runs.

Run this app independently from the main recipe server:
    GITHUB_TOKEN=<token> python workflow_runs/app.py

Environment variables:
    GITHUB_TOKEN   – Personal access token for authenticated GitHub API calls
                     (optional but recommended to avoid rate limits).
    GITHUB_OWNER   – Default repository owner pre-filled in the filter form.
    GITHUB_REPO    – Default repository name pre-filled in the filter form.
    PORT           – Port to listen on (default: 5001).
"""
import os

import requests
from flask import Flask, render_template, request

# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------

app = Flask(
    __name__,
    template_folder='templates',
    static_folder='static',
)

# ---------------------------------------------------------------------------
# Configuration (override via environment variables)
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_DEFAULT_OWNER = os.getenv("GITHUB_OWNER", "adamtasteslikegood")
GITHUB_DEFAULT_REPO = os.getenv("GITHUB_REPO", "tasteslikegood.com")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid conclusion values surfaced by the GitHub API
WORKFLOW_CONCLUSION_VALUES = [
    'success', 'failure', 'cancelled', 'skipped',
    'timed_out', 'action_required', 'neutral', 'stale',
]

# Valid status values surfaced by the GitHub API
WORKFLOW_STATUS_VALUES = [
    'completed', 'in_progress', 'queued', 'requested', 'waiting', 'pending',
]

# Combined list shown to users in the filter UI (conclusions first, then
# any status values not already in the conclusions list)
ALL_FILTER_VALUES = WORKFLOW_CONCLUSION_VALUES + [
    s for s in WORKFLOW_STATUS_VALUES if s not in WORKFLOW_CONCLUSION_VALUES
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fetch_workflow_runs(owner, repo, per_page=100):
    """Fetch workflow runs from the GitHub API for a given repository.

    Args:
        owner:    GitHub user or organisation that owns the repository.
        repo:     Repository name.
        per_page: Maximum number of runs to retrieve in a single API call
                  (1–100, default 100).

    Returns:
        A tuple of ``(runs_list, error_message)``.  ``error_message`` is
        ``None`` on success or a human-readable string when the request fails.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    params = {"per_page": per_page}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code == 404:
            return [], f"Repository '{owner}/{repo}' not found or not accessible."
        if resp.status_code == 403:
            return [], (
                "GitHub API rate limit exceeded or insufficient permissions. "
                "Set the GITHUB_TOKEN environment variable."
            )
        if not resp.ok:
            return [], f"GitHub API error {resp.status_code}: {resp.text[:200]}"
        return resp.json().get("workflow_runs", []), None
    except requests.exceptions.ConnectionError:
        return [], "Could not connect to the GitHub API. Check your network connection."
    except requests.exceptions.Timeout:
        return [], "GitHub API request timed out. Please try again."
    except requests.exceptions.RequestException as exc:
        return [], f"Unexpected error contacting GitHub API: {exc}"


def apply_status_filter(runs, filter_status, filter_mode):
    """Filter *runs* by status/conclusion value.

    Args:
        runs:          List of workflow-run dicts returned by the GitHub API.
        filter_status: A single status/conclusion string to match against, or
                       an empty string to skip filtering.
        filter_mode:   ``'include'`` – keep only runs that match the status.
                       ``'exclude'`` – keep only runs that do NOT match the
                       status (i.e. the inverse set).

    Returns:
        Filtered list of run dicts.
    """
    if not filter_status:
        return runs

    def _matches(run):
        """Return True when the run's status *or* conclusion equals filter_status."""
        return (
            run.get("conclusion") == filter_status
            or run.get("status") == filter_status
        )

    if filter_mode == "exclude":
        return [r for r in runs if not _matches(r)]
    # Default: include mode
    return [r for r in runs if _matches(r)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route('/')
def workflow_runs():
    """Display GitHub Actions workflow runs with optional status filtering."""
    owner = request.args.get('owner', GITHUB_DEFAULT_OWNER).strip()
    repo = request.args.get('repo', GITHUB_DEFAULT_REPO).strip()
    filter_status = request.args.get('filter_status', '').strip()
    filter_mode = request.args.get('filter_mode', 'include').strip()

    # Sanitise filter_mode to one of the two accepted values
    if filter_mode not in ('include', 'exclude'):
        filter_mode = 'include'

    runs, error = fetch_workflow_runs(owner, repo)

    if error is None:
        runs = apply_status_filter(runs, filter_status, filter_mode)

    return render_template(
        'workflow_runs.html',
        runs=runs,
        error=error,
        owner=owner,
        repo=repo,
        filter_status=filter_status,
        filter_mode=filter_mode,
        all_filter_values=ALL_FILTER_VALUES,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(os.getenv("PORT", 5001))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, host='0.0.0.0', port=port)

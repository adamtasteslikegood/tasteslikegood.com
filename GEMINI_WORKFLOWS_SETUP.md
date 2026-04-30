# Gemini Workflows Setup Guide

This document provides instructions for configuring the Gemini CLI GitHub Actions workflows in this repository.

## Overview

The Gemini workflows provide AI-powered automation for:
- **PR Reviews** (`gemini-review.yml`) - Automated code review on pull requests
- **Issue Triage** (`gemini-triage.yml`) - Automatic labeling and categorization of issues
- **Scheduled Triage** (`gemini-scheduled-triage.yml`) - Hourly batch processing of unlabeled issues
- **Invoke** (`gemini-invoke.yml`) - On-demand AI assistance via `@gemini-cli` mentions
- **Dispatch** (`gemini-dispatch.yml`) - Router that triggers the appropriate workflow

## Required Configuration

### GitHub Repository Secrets

Add these secrets in **Settings → Secrets and variables → Actions → Repository secrets**:

#### Required (choose one):
- `GEMINI_API_KEY` - Google Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey)
  - OR -
- `GOOGLE_API_KEY` - Alternative name for the Gemini API key

#### Optional (for GitHub App authentication):
- `APP_PRIVATE_KEY` - Private key for GitHub App (if using app-based authentication instead of PAT)

### GitHub Repository Variables

Add these variables in **Settings → Secrets and variables → Actions → Repository variables**:

#### Optional but Recommended:
- `GEMINI_CLI_VERSION` - Version of Gemini CLI to use (e.g., `v0`, `latest`)
  - Default: `v0`

- `GEMINI_MODEL` - Gemini model to use (e.g., `gemini-2.0-flash-exp`, `gemini-pro`)
  - Default: Uses the default model from the action

#### Optional (for GitHub App):
- `APP_ID` - GitHub App ID (if using app-based authentication)

#### Optional (for GCP/Vertex AI):
- `GOOGLE_CLOUD_LOCATION` - GCP region (e.g., `us-central1`)
- `GOOGLE_CLOUD_PROJECT` - GCP project ID
- `SERVICE_ACCOUNT_EMAIL` - Service account email for Workload Identity
- `GCP_WIF_PROVIDER` - Workload Identity Provider path
- `GOOGLE_GENAI_USE_VERTEXAI` - Set to `true` to use Vertex AI instead of AI Studio
- `GOOGLE_GENAI_USE_GCA` - Set to `true` to use Gemini Code Assist

#### Optional (for debugging):
- `DEBUG` - Set to `true` to enable debug logging
- `ACTIONS_STEP_DEBUG` - Alternative debug flag
- `UPLOAD_ARTIFACTS` - Set to `true` to upload workflow artifacts

## Setup Instructions

### Minimum Setup (API Key Only)

1. Get a Gemini API key:
   - Visit [Google AI Studio](https://aistudio.google.com/apikey)
   - Create a new API key
   - Copy the key

2. Add the API key to GitHub:
   - Go to repository **Settings → Secrets and variables → Actions**
   - Click **New repository secret**
   - Name: `GEMINI_API_KEY`
   - Value: Paste your API key
   - Click **Add secret**

3. (Optional) Configure model version:
   - Go to **Repository variables** tab
   - Click **New repository variable**
   - Name: `GEMINI_MODEL`
   - Value: `gemini-2.0-flash-exp` (or your preferred model)
   - Click **Add variable**

### Enterprise Setup (Vertex AI + Workload Identity)

For production use with GCP, configure Workload Identity Federation:

1. Set up Workload Identity Federation in GCP
2. Create a service account with Vertex AI permissions
3. Configure the repository variables listed above under "Optional (for GCP/Vertex AI)"

See [Google's documentation](https://github.com/google-github-actions/auth#workload-identity-federation-through-a-service-account) for detailed setup.

## Usage

### Automatic Triggers

- **Pull Requests**: Gemini automatically reviews PRs when opened
- **Issues**: Gemini triages new issues automatically
- **Scheduled**: Hourly scan for untriaged issues

### Manual Triggers

Comment on issues or PRs:
- `@gemini-cli` - General assistance
- `@gemini-cli /review` - Request code review (on PRs)
- `@gemini-cli /triage` - Request issue triage

## Command Files

The workflows use command files in `.github/commands/`:
- `gemini-invoke.toml` - Prompt and configuration for general invocation
- `gemini-review.toml` - Prompt and rules for code reviews
- `gemini-triage.toml` - Prompt for single issue triage
- `gemini-scheduled-triage.toml` - Prompt for batch issue processing

These files define the AI persona, instructions, and behavior.

## Troubleshooting

### Workflows not running

1. **Check permissions**: Ensure GitHub Actions has write permissions
   - Settings → Actions → General → Workflow permissions
   - Select "Read and write permissions"

2. **Verify secrets**: Make sure `GEMINI_API_KEY` is set correctly
   - It should start with a prefix like `AI...`

3. **Check API quota**: Gemini API has rate limits
   - Free tier: 15 requests per minute
   - Check [AI Studio quotas](https://ai.google.dev/pricing)

### Workflows fail immediately

1. Check the workflow logs for the specific error
2. Common issues:
   - Missing or invalid API key
   - Missing required variables (if using Vertex AI)
   - GitHub Actions permissions issues

### AI responses are poor quality

1. Adjust the model:
   - Try `gemini-2.0-flash-exp` for latest features
   - Try `gemini-pro` for more capable reasoning

2. Edit command files (`.github/commands/*.toml`):
   - Modify the `prompt` section to adjust behavior
   - Add more specific instructions or constraints

## Additional Resources

- [Gemini CLI GitHub Actions](https://github.com/google-github-actions/run-gemini-cli)
- [Google AI Studio](https://aistudio.google.com/)
- [Gemini API Documentation](https://ai.google.dev/docs)
- [GitHub Actions Documentation](https://docs.github.com/en/actions)

## Current Status

The workflows are **installed and configured** in this repository. To verify they're working:

1. Check that secrets/variables are configured (see above)
2. Open a test pull request to trigger the review workflow
3. Create a test issue to trigger triage
4. Check the Actions tab for workflow runs

## Notes for This Repository

- The workflows use `google-github-actions/run-gemini-cli@v0`
- Docker is used to run the GitHub MCP server for repository interactions
- Workflows follow security best practices (least privilege, OIDC, etc.)
- The dispatch workflow routes events to appropriate sub-workflows

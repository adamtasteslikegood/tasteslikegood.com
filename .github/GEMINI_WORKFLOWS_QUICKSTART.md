# Gemini Workflows Quick Start

## Minimum Setup (2 steps)

### 1. Get API Key
Visit: https://aistudio.google.com/apikey

### 2. Add to GitHub
- Settings → Secrets and variables → Actions
- New repository secret
- Name: `GEMINI_API_KEY`
- Value: (paste your key)

## Done!

The workflows will now:
- ✅ Review pull requests automatically
- ✅ Triage new issues
- ✅ Respond to `@gemini-cli` mentions

## Optional: Choose Model

Repository Variables → Add:
- Name: `GEMINI_MODEL`
- Value: `gemini-2.0-flash-exp`

## Full Documentation

See: `/GEMINI_WORKFLOWS_SETUP.md`

## Required Secrets/Variables Summary

### Secrets (Required - choose one):
- `GEMINI_API_KEY` or `GOOGLE_API_KEY`

### Variables (All Optional):
- `GEMINI_MODEL` - Model to use (default: gemini-pro)
- `GEMINI_CLI_VERSION` - CLI version (default: v0)
- `APP_ID` + secret `APP_PRIVATE_KEY` - For GitHub App auth
- GCP variables (for Vertex AI):
  - `GOOGLE_CLOUD_LOCATION`
  - `GOOGLE_CLOUD_PROJECT`
  - `SERVICE_ACCOUNT_EMAIL`
  - `GCP_WIF_PROVIDER`
  - `GOOGLE_GENAI_USE_VERTEXAI`
- `DEBUG` - Enable debug logging
- `UPLOAD_ARTIFACTS` - Save workflow artifacts

## Workflows Included

| Workflow | Trigger | Purpose |
|----------|---------|---------|
| `gemini-dispatch.yml` | PR open, issue open, comments | Routes to appropriate workflow |
| `gemini-review.yml` | Called by dispatch | Automated PR reviews |
| `gemini-triage.yml` | Called by dispatch | Issue labeling |
| `gemini-scheduled-triage.yml` | Hourly | Batch process unlabeled issues |
| `gemini-invoke.yml` | Called by dispatch | General AI assistance |

## Testing

1. Open a test PR → Check Actions tab for review
2. Create test issue → Should get auto-labeled
3. Comment `@gemini-cli help` → Should get response

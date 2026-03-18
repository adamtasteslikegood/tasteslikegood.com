# Gemini Agent Configuration

This file tracks specific configurations and parameters for the Gemini agent.

## Agent Name: Gemini CLI
**Version:** 1.0.0
**Description:** An interactive CLI agent specializing in software engineering tasks, utilizing Google's Gemini models.

### Parameters
| Parameter Name | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `model` | String | gemini-2.0-flash | The specific Gemini model version to use. |
| `temperature` | Float | 0.7 | Controls randomness in generation (0.0 to 1.0). |
| `max_output_tokens` | Integer | 8192 | Maximum number of tokens in the response. |

### Dependencies
*   `google-generativeai` (Python SDK)
*   `ripgrep` (for fast file searching)

### Usage
```bash
# Standard interaction
gemini "Help me fix a bug in app.py"

# With specific context
gemini "Refactor this file" --file app.py
```

### Testing
1.  Verify API connectivity: Run a simple "Hello" prompt.
2.  Check tool availability: Ensure `read_file` and `run_shell_command` are functioning.
3.  Validate context awareness: Ask questions about the current file structure.

### Notes
*   Maintain strict adherence to project conventions.
*   Prioritize safety when executing shell commands.

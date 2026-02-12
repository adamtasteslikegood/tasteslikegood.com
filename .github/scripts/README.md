# Non-ASCII Filename Checker

This tool checks for and optionally fixes non-ASCII characters in file and directory names within a repository.

## Overview

The script scans a repository for files and directories with non-ASCII characters in their names, logs findings, and can optionally rename them to ASCII-safe alternatives.

## Usage

### Basic Check

Check for non-ASCII filenames without making changes:

```bash
python3 .github/scripts/check_non_ascii_filenames.py
```

### Check and Fix

Automatically rename files with non-ASCII characters:

```bash
python3 .github/scripts/check_non_ascii_filenames.py --fix
```

### Dry Run

Preview what changes would be made without actually renaming files:

```bash
python3 .github/scripts/check_non_ascii_filenames.py --dry-run
```

### Fail on Detection

Exit with error status if non-ASCII characters are found (useful for CI/CD):

```bash
python3 .github/scripts/check_non_ascii_filenames.py --fail-on-found
```

## Command-Line Options

- `--root-dir PATH` - Root directory to scan (default: current directory)
- `--log-file PATH` - Path to log file (default: non_ascii_filenames.log)
- `--fix` - Rename files and directories to ASCII-safe names
- `--dry-run` - Simulate renaming without actually changing files
- `--fail-on-found` - Exit with non-zero status if non-ASCII paths are found

## CI/CD Integration

### GitHub Actions

The repository includes a GitHub Actions workflow (`.github/workflows/check-non-ascii-filenames.yml`) that:

1. **Automatic Checks**: Runs on every push and pull request
2. **Fail on Detection**: Workflow fails if non-ASCII filenames are found
3. **Log Artifacts**: Uploads detailed logs as workflow artifacts
4. **Manual Fix**: Can be triggered manually to automatically fix filenames

### Manual Trigger with Auto-Fix

To manually run the workflow and automatically fix non-ASCII filenames:

1. Go to Actions tab in GitHub
2. Select "Check Non-ASCII Filenames" workflow
3. Click "Run workflow"
4. Set "fix_files" to "true"
5. Click "Run workflow"

The workflow will automatically rename files and commit the changes.

## How It Works

### Detection

The script walks through all files and directories, checking if their names can be encoded as ASCII. Files in common excluded directories (`.git`, `node_modules`, `__pycache__`, `.venv`, `venv`) are skipped.

### Normalization

When fixing filenames, the script:

1. Decomposes Unicode characters (NFD normalization)
2. Removes combining marks (accents)
3. Replaces remaining non-ASCII characters with underscores
4. Cleans up multiple consecutive underscores

For example:
- `café.txt` → `cafe.txt`
- `naïve.py` → `naive.py`
- `à_la_mode.png` → `a_la_mode.png`
- `文件.txt` → `_.txt`

### Logging

All operations are logged to a file (default: `non_ascii_filenames.log`) with:
- Timestamp
- Action taken (detected, renamed, dry-run)
- Original filename
- Normalized filename
- List of non-ASCII characters found

## Reusability

This tool is designed to be reusable across different repositories:

1. **Copy the script**: Copy `.github/scripts/check_non_ascii_filenames.py` to your repository
2. **Copy the workflow**: Copy `.github/workflows/check-non-ascii-filenames.yml` to your repository
3. **Update .gitignore**: Add log files to your `.gitignore`:
   ```
   non_ascii_filenames.log
   non_ascii_filenames_fixed.log
   ```

No modifications to the script or workflow are needed - they work out of the box.

## Common Use Cases

### Pre-commit Hook

You can add this as a git pre-commit hook to prevent non-ASCII filenames from being committed:

```bash
#!/bin/bash
# .git/hooks/pre-commit

python3 .github/scripts/check_non_ascii_filenames.py --fail-on-found
```

### CI/CD Pipeline

The included GitHub Actions workflow automatically:
- Checks all commits and pull requests
- Fails if non-ASCII characters are found
- Provides detailed logs
- Allows manual fixing

### Batch Cleanup

To clean up an entire repository:

```bash
# First, do a dry run to see what would change
python3 .github/scripts/check_non_ascii_filenames.py --dry-run

# Review the changes, then apply them
python3 .github/scripts/check_non_ascii_filenames.py --fix
```

## Troubleshooting

### Script doesn't find files

- Ensure you're running from the correct directory
- Check that files aren't in excluded directories (`.git`, `node_modules`, etc.)

### Renaming fails

- Check file permissions
- Ensure target filename doesn't already exist
- Review error messages in the output and log file

### Workflow fails in CI

- Check the workflow logs artifact
- Review the `non_ascii_filenames.log` artifact
- Run the script locally to reproduce the issue

## Examples

### Example 1: Check for Issues

```bash
$ python3 .github/scripts/check_non_ascii_filenames.py
Scanning directory: /path/to/repo
Log file: non_ascii_filenames.log

⚠️  Found 2 path(s) with non-ASCII characters:

📄 File: ./docs/café.txt
   Original: café.txt
   → Would become: cafe.txt

📄 File: ./images/naïve_approach.png
   Original: naïve_approach.png
   → Would become: naive_approach.png
```

### Example 2: Fix Issues

```bash
$ python3 .github/scripts/check_non_ascii_filenames.py --fix
Scanning directory: /path/to/repo
Log file: non_ascii_filenames.log

⚠️  Found 2 path(s) with non-ASCII characters:
...

================================================================================
RENAMING FILES...
================================================================================

✓ ./docs/café.txt
   → ./docs/cafe.txt

✓ ./images/naïve_approach.png
   → ./images/naive_approach.png

Results: 2 succeeded, 0 failed
```

## License

This tool is part of the repository and follows the same license as the repository.

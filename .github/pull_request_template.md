## Description

<!-- Provide a brief description of the changes in this PR -->

## Type of Change

- [ ] Bug fix (non-breaking change which fixes an issue)
- [ ] New feature (non-breaking change which adds functionality)
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected)
- [ ] Documentation update
- [ ] Refactoring (no functional changes)
- [ ] Performance improvement
- [ ] Database migration
- [ ] CI/CD changes

## Testing

<!-- Describe the tests you ran to verify your changes -->

### Pre-Commit Checklist

- [ ] Code formatted with Black (`uv run black .`)
- [ ] Linting passes (`uv run flake8 .`)
- [ ] Type checking passes (`uv run mypy . --ignore-missing-imports`)
- [ ] Tests pass (`uv run pytest`)
- [ ] Coverage is reasonable (aim for >70%)

### Test Details

- [ ] Added new tests for new functionality
- [ ] Updated existing tests as needed
- [ ] All tests pass locally
- [ ] No regressions in existing functionality

## Database Changes

- [ ] No database changes
- [ ] Schema migration included
- [ ] Migration tested locally
- [ ] Rollback plan documented

## Environment Variables

- [ ] No new environment variables
- [ ] New variables documented in `.env.example`
- [ ] Variables added to CI/CD secrets (if needed)

## API Changes

- [ ] No API changes
- [ ] New endpoints documented in `API.md`
- [ ] Breaking changes documented and versioned
- [ ] Backward compatibility maintained

## Dependencies

- [ ] No new dependencies
- [ ] Dependencies added via `uv add package-name`
- [ ] `pyproject.toml` and `uv.lock` updated
- [ ] Security vulnerabilities checked

## Documentation

- [ ] README.md updated (if needed)
- [ ] API.md updated (if API changes)
- [ ] Inline code comments added for complex logic
- [ ] Docstrings added/updated for public functions

## Deployment Notes

<!-- Any special instructions for deployment? -->

- [ ] No special deployment steps
- [ ] Database migration required: <!-- migration command -->
- [ ] Environment variables required: <!-- list -->
- [ ] Service restart required: <!-- reason -->

## Screenshots (if applicable)

<!-- Add screenshots to help explain your changes -->

## Related Issues

<!-- Link to related issues or PRs -->

Closes #
Related to #

## Reviewer Notes

<!-- Any additional context for reviewers -->

---

## Checklist for Reviewers

- [ ] Code follows project style guidelines
- [ ] Changes are well-documented
- [ ] Tests adequately cover new code
- [ ] No obvious security vulnerabilities
- [ ] Performance implications considered
- [ ] Breaking changes are documented

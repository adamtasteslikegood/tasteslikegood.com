# Contributing to Tastes Like Good

Thank you for your interest in contributing to Tastes Like Good! This document provides guidelines and instructions for contributing to the project.

## Table of Contents
- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Architecture Guidelines](#architecture-guidelines)
- [Code Style](#code-style)
- [Testing](#testing)
- [Pull Request Process](#pull-request-process)

## Code of Conduct

Please be respectful and considerate in all interactions. We aim to maintain a welcoming and inclusive environment.

## Getting Started

1. **Fork the repository** and clone your fork locally
2. **Set up your development environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. **Copy `.env.example` to `.env`** and configure your environment variables
4. **Run the application** to ensure everything works:
   ```bash
   python app.py
   ```
5. **Run the test suite:**
   ```bash
   pytest
   ```

## Development Workflow

1. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes** following the architecture guidelines below

3. **Write tests** for your changes in the `tests/` directory

4. **Run tests** to ensure everything passes:
   ```bash
   pytest -v
   ```

5. **Commit your changes** with descriptive commit messages:
   ```bash
   git commit -m "Add: Brief description of your changes"
   ```

6. **Push to your fork** and create a Pull Request

## Architecture Guidelines

### Modular Structure

The project follows a modular architecture. Please maintain this structure:

```
blueprints/      # Route handlers (Flask Blueprints)
services/        # Business logic (AI, images, models)
repositories/    # Data persistence (file operations)
validators/      # Data validation logic
utils/           # Utility functions
templates/       # Jinja2 HTML templates
static/          # CSS, JS, images
tests/           # Test suite
```

### Adding New Features

#### Adding a New Route
1. Create or update a blueprint in `blueprints/`
2. Register the blueprint in `app.py` if new
3. Add corresponding template in `templates/`
4. Add tests in `tests/`

Example:
```python
# blueprints/my_new_bp.py
from flask import Blueprint, render_template

my_new_bp = Blueprint('my_new', __name__)

@my_new_bp.route('/my-route')
def my_route():
    return render_template('my_template.html')
```

#### Adding a New Service
1. Create a new file in `services/` (e.g., `my_service.py`)
2. Implement your service functions with proper error handling
3. Add docstrings explaining parameters and return values
4. Import and use in blueprints as needed

Example:
```python
# services/my_service.py
from typing import Dict, Any
import logging

logger = logging.getLogger(__name__)

def my_service_function(param: str) -> Dict[str, Any]:
    """
    Description of what this function does.
    
    Args:
        param: Description of parameter
        
    Returns:
        Dict containing results
        
    Raises:
        ValueError: If param is invalid
    """
    try:
        # Your logic here
        return {"result": "success"}
    except Exception as e:
        logger.error(f"Error in my_service_function: {e}")
        raise
```

#### Modifying Recipe Schema
1. Update `recipe_schema.json` with your changes
2. Update `utils/normalization.py` if field normalization is needed
3. Update `UNIT_MAPPINGS` if adding new measurement units
4. Add tests to `tests/test_recipe_validation.py`
5. Update documentation in `CLAUDE.md` and `README.md`

### Validation and Normalization

- Always validate user input before processing
- Use `validators/recipe_validator.py` for recipe validation
- Use `utils/normalization.py` for data normalization
- Handle JSON parsing errors gracefully

## Code Style

### Python Style Guidelines
- Follow **PEP 8** style guide
- Use **type hints** for function parameters and return values
- Write **docstrings** for all functions, classes, and modules
- Keep functions focused and single-purpose
- Maximum line length: **100 characters**

### Documentation
- Add docstrings to all public functions:
  ```python
  def my_function(param1: str, param2: int) -> bool:
      """
      Brief description of function.
      
      Args:
          param1: Description of param1
          param2: Description of param2
          
      Returns:
          Description of return value
          
      Raises:
          ValueError: When this exception is raised
      """
      pass
  ```

### Naming Conventions
- **Functions/variables**: `snake_case`
- **Classes**: `PascalCase`
- **Constants**: `UPPER_SNAKE_CASE`
- **Private methods**: `_leading_underscore`

### Error Handling
- Use try-except blocks for external API calls
- Log errors with appropriate severity
- Provide user-friendly error messages
- Never expose sensitive information in error messages

## Testing

### Writing Tests
- Place tests in `tests/` directory
- Name test files `test_*.py`
- Use descriptive test function names: `test_description_of_what_is_tested`
- Mock external API calls using `unittest.mock`

Example:
```python
import pytest
from unittest.mock import patch

def test_my_function_with_valid_input():
    """Test my_function with valid input."""
    result = my_function("valid_input")
    assert result == expected_value

@patch('module.external_api_call')
def test_my_function_with_mocked_api(mock_api):
    """Test my_function with mocked API call."""
    mock_api.return_value = {"data": "test"}
    result = my_function()
    assert result["data"] == "test"
```

### Running Tests
```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_normalization.py

# Run with coverage
pytest --cov=. --cov-report=html

# Run specific test
pytest tests/test_normalization.py::TestNormalization::test_normalize_unit
```

### Test Coverage
- Aim for >80% code coverage
- Test edge cases and error conditions
- Test both positive and negative scenarios

## Pull Request Process

1. **Update documentation** if you've changed functionality
2. **Add tests** for new features or bug fixes
3. **Ensure all tests pass** locally before submitting
4. **Update README.md** if you've added new features
5. **Write a clear PR description** explaining:
   - What changes you made
   - Why you made them
   - How to test them
   - Any related issues

### PR Checklist
- [ ] Tests pass locally
- [ ] New tests added for new features
- [ ] Documentation updated
- [ ] Code follows style guidelines
- [ ] No sensitive data in commits
- [ ] Commit messages are descriptive

## Questions or Need Help?

- Check the documentation in `CLAUDE.md` for detailed technical guidance
- Review `README.md` for setup and usage instructions
- Look at existing code for examples
- Open an issue for questions or clarifications

Thank you for contributing to Tastes Like Good! 🌱

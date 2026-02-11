# Contributing

This project welcomes contributions and suggestions.

## Development Setup

```bash
# Clone the repository
git clone https://github.com/Azure/digital-ops-scale-kit.git
cd digital-ops-scale-kit

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=siteops --cov-report=term-missing
```

## Code Style

- Type hints required for all functions
- Docstrings for public methods
- Follow existing patterns in the codebase

## Testing

- Add tests for new functionality
- Mock `subprocess.run` for executor tests—no real Azure calls
- Use fixtures from `conftest.py` for workspace setup

## Pull Request Process

1. Run `pytest` and ensure all tests pass
2. Update documentation if adding new features
3. Follow the existing code style

## Microsoft Open Source

Most contributions require you to agree to a Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us the rights to use your contribution. For details, visit <https://cla.opensource.microsoft.com>.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/). For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/).

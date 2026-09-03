# Repository Guidelines

## Project Structure & Module Organization

`wlhosted/` is a Django customization layered on Weblate. `integrations/`
contains views, tasks, forms, user-sync logic, and management commands;
`payments/` owns billing models, backends, validation, and migrations. Keep
templates beside their app under `templates/`, translations under
`wlhosted/locale/`, and schema changes in the relevant `migrations/` directory.
Shared models, database routing, and test settings live at the package root.
Utilities belong in `scripts/`; tool configuration is maintained in
`pyproject.toml`, `setup.cfg`, and
`.pre-commit-config.yaml`.

## Build, Test, and Development Commands

- `uv sync --dev` installs locked runtime and development dependencies into
  `.venv`.
- `uv run weblate collectstatic --noinput` prepares static assets as CI does.
- `uv run pytest` runs the Django test suite with branch coverage enabled.
- `uv run pytest wlhosted/payments/tests.py -k decimal` runs a focused test.
- `uv run prek run --all-files` applies the repository's formatting, lint,
  spelling, security, and configuration checks.
- `uv run mypy --show-column-numbers wlhosted` reproduces CI type checking.
- `uv build` creates the source distribution and wheel in `dist/`.

CI tests Python 3.12 through 3.14 with PostgreSQL and Redis.

## Coding Style & Naming Conventions

Follow Weblate's Django conventions. Use four spaces in Python, two in HTML,
YAML, and Markdown, LF endings, and UTF-8. Ruff formats and lints Python; djLint
handles Django templates. Prefer type hints, `snake_case` functions, and
`PascalCase` classes. Mark user-facing Python strings with Django i18n helpers
and template text with `{% translate %}` or `{% blocktranslate %}`. New Python
files must retain the GPL-3.0-or-later copyright and license header pattern.

## Testing Guidelines

Tests use pytest-django with Django `TestCase` classes and live in `tests.py` or
`test_*.py`; test methods start with `test_`. Add tests for fixes and features.
Mock payment providers, HTTP APIs, and other external boundaries. No numeric
coverage threshold is configured, but changes
should not reduce meaningful branch coverage.

## Commit & Pull Request Guidelines

Use Conventional Commits: `<type>(<optional scope>): <description>`, for example
`fix(payments): handle decimal amounts`. Keep commits focused and explain the
motivation in the body; add `Fixes #123` when applicable. Pull requests should
describe behavior and motivation, link issues, include tests, update relevant
documentation, and attach screenshots for visible template changes. Ensure lint,
type checks, packaging checks, and the full test matrix pass.

## Security

Never commit credentials or production payment data. Report vulnerabilities
privately through GitHub's **Report a vulnerability** flow, not a public issue.

# Copilot instructions

Follow [AGENTS.md](../AGENTS.md): the project's only instruction file. If it isn't loaded, read it before
changing anything. The rules that matter most:

- Python with uv: `uv run ruff check`, `uv run mypy`, `uv run pytest`. Never `pip install`.
- Code, tests and the matching `docs/wiki/` page change together.
- Never edit `CHANGELOG.md`, the version, generated wiki blocks or template-owned files. Never weaken a check.

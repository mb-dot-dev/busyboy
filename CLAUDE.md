# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

busyboy is a console application (CLI) that displays various information on BUSYBar. It depends on `busylib`, a
private git package (`ssh://git@github.com/busy-app/busylib-py`). The project is in early/skeleton stage.

Requires Python >=3.14. Dependency management, builds, and packaging all go through `uv`.

## Commands

All commands run through `make` (which wraps `uv run --frozen ...`):

- `make install-dev` — install all dependencies (including dev group) from the lockfile.
- `make install` — install production dependencies only.
- `make unit` — run the test suite (`pytest`).
- `make lint` — run `ruff check`, `ruff format --check`, and `ty check` (type checking).
- `make format` — auto-format code with `ruff format`.
- `make test` — run `lint` then `unit`; this is the full local check before committing.
- `make coverage` — run tests with coverage, writing an XML report to `build/coverage.xml`.
- `make upgrade` — upgrade dependencies and refresh `uv.lock`.

To run a single test file or test: `uv run --frozen pytest tests/test_main.py::test_ok`.

Always run `uv sync` (via `make install-dev`) after pulling changes that touch `pyproject.toml` or `uv.lock`, since
commands use `--frozen` and expect the lockfile to already match.

## Architecture

- `src/busyboy/__init__.py` defines the `main` Click command, exposed as the `busyboy` console script
  (`[project.scripts]` in `pyproject.toml`).
- Tests live in `tests/`, mirroring the package under `src/busyboy/`.
- Coverage config (`[tool.coverage.*]` in `pyproject.toml`) requires 75% coverage and measures branches over
  `src`, excluding `if TYPE_CHECKING:` blocks.
- Ruff is configured with a 120-char line length, double quotes, PEP 257 docstring convention, and isort settings
  that force sorting within sections and split on trailing commas.

## CI

`.github/workflows/main.yaml` runs on every push/PR to `main`: installs deps, lints, runs coverage, then submits
results to SonarQube/SonarCloud (project key `mb-dot-dev_busyboy`) with the quality gate blocking the workflow.
`.github/workflows/release.yaml` publishes to PyPI on `v*.*.*` tags via `uv build` / `uv publish`, using the
tag as the version.

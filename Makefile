.PHONY: help
help:  ## Display this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make <target>\033[36m\033[0m\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

.PHONY: install
install:  ## Install production dependencies.
	uv sync --frozen --no-dev

.PHONY: install-dev
install-dev:  ## Install all dependencies.
	uv sync --frozen

.PHONY: outdated
outdated:  ## Check for outdated dependencies.
	uv tree --depth=1 --outdated

.PHONY: upgrade
upgrade:  ## Upgrade dependencies.
	uv lock --upgrade

.PHONY: unit
unit:  ## Run unit tests.
	uv run --frozen pytest

.PHONY: lint
lint: 		 ## Run linter.
	uv run --frozen ruff check
	uv run --frozen ruff format --check
	uv run --frozen ty check

.PHONY: format
format: 	 ## Format code.
	uv run --frozen ruff format

.PHONY: test
test: lint unit  ## Run all tests.

.PHONY: coverage
coverage:  ## Run tests with coverage report.
	uv run --frozen pytest --cov --cov-report=xml

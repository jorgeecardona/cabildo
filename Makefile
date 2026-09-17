# cabildo -- task runner. uv is the only tool needed; the package is stdlib-only at runtime.
# One-time setup after cloning:  make install-hooks
.DEFAULT_GOAL := help
.PHONY: help install install-hooks format lint typecheck test check bump docs viewer build clean

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## editable install + register the MCP server + hooks (scripts/install.py)
	python scripts/install.py

install-hooks: ## point git at the tracked .githooks/ (pre-commit + pre-push)
	git config core.hooksPath .githooks
	chmod +x .githooks/pre-commit .githooks/pre-push scripts/bump-version scripts/check-version-bump
	@echo "hooks installed: core.hooksPath -> .githooks"

format: ## auto-format + autofix lint (ruff)
	uv run ruff format src tests
	uv run ruff check --fix src tests

lint: ## lint (ruff) -- same as CI
	uv run ruff check src tests

format-check: ## verify formatting (ruff) -- same as CI; run `make format` to fix
	uv run ruff format --check src tests

typecheck: ## type-check (pyright) -- same as CI
	uv run pyright

test: ## run the test suite
	uv run pytest -q

check: lint format-check typecheck test ## the full CI test stage: lint + format-check + typecheck + test

bump: ## bump version in pyproject.toml -- TYPE=patch|minor|major (default patch)
	./scripts/bump-version $(or $(TYPE),patch)

docs: ## build docs strictly (as CI / Pages do)
	uv run --extra docs mkdocs build --strict

viewer: ## run the local web viewer on http://127.0.0.1:8787
	uv run cabildo viewer --open

build: ## build sdist + wheel into dist/
	uv build --out-dir dist

clean: ## remove build + test caches
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache site
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

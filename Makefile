# cabildo -- task runner. uv is the only tool needed; the package is stdlib-only.
.DEFAULT_GOAL := help
.PHONY: help install format lint typecheck test check viewer build clean

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## editable install + register the MCP server + hooks (once, per machine)
	python scripts/install.py

format: ## auto-format + autofix lint (ruff)
	uvx ruff format src tests
	uvx ruff check --fix src tests

lint: ## lint (ruff)
	uvx ruff check src tests

typecheck: ## type-check (pyright)
	uvx pyright src

test: ## run the test suite -- same as CI
	uv run --extra dev pytest -q

check: lint test ## lint + test

viewer: ## run the local web viewer on http://127.0.0.1:8787
	uv run cabildo viewer --open

build: ## build sdist + wheel into dist/
	uv build --out-dir dist

clean: ## remove build + test caches
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

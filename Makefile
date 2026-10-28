# cabildo -- human task verbs. These are thin aliases: the CI-graded tasks
# (lint/format-check/typecheck/test/docs/build) delegate to the matching moon
# task, so the command + its inputs/outputs live in ONE place (moon.yml) and
# `make` runs exactly what CI runs, with moon's caching. Only the verbs moon has
# no task for (format = write, bump, install*, viewer, clean) run directly here.
# One-time setup after cloning:  make install-hooks
.DEFAULT_GOAL := help
.PHONY: help install install-hooks format lint typecheck test check deadcode bump docs viewer build clean

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

lint: ## lint (ruff) -- the CI :lint task
	moon run :lint

format-check: ## verify formatting (ruff) -- the CI :format-check task; run `make format` to fix
	moon run :format-check

typecheck: ## type-check (pyright) -- the CI :typecheck task
	moon run :typecheck

test: ## run the test suite -- the CI :test task
	moon run :test

check: lint format-check typecheck test ## the full CI test stage: lint + format-check + typecheck + test

deadcode: ## report dead-code candidates (vulture); not a gate -- confirm each by hand
	@uv run vulture || true

bump: ## bump __version__ in src/cabildo/__init__.py -- TYPE=patch|minor|major (default patch)
	./scripts/bump-version $(or $(TYPE),patch)

docs: ## build docs strictly (as CI / Pages do) -- the CI :docs task
	moon run :docs

viewer: ## run the local web viewer on http://127.0.0.1:8787
	uv run cabildo viewer --open

build: ## build sdist + wheel into dist/ -- the CI :build task
	moon run :build

clean: ## remove build + test caches
	rm -rf dist build *.egg-info .pytest_cache .ruff_cache site
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

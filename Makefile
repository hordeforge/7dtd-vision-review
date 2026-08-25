.PHONY: help all check lint typecheck test coverage

.DEFAULT_GOAL := help

# uv runs the suite when it is available so contributors share one toolchain;
# the plain interpreter still works, because the core has no dependencies.
PYTHON := $(shell command -v uv >/dev/null 2>&1 && echo "uv run --no-project python3" || echo python3)

help:
	@echo "check     lint + typecheck + compile"
	@echo "test      offline test suite"
	@echo "coverage  test suite with a line-coverage report"
	@echo "all       check + test"

all: check test

check: lint typecheck
	$(PYTHON) -m compileall -q src tests

# Python analysis, mirroring the sibling repos' contract: run when the tool is
# on PATH, hard-fail in CI, and say so plainly when skipped on a dev host.
lint:
	@if command -v ruff >/dev/null 2>&1; then \
		ruff check .; \
		ruff format --check .; \
	elif [ -n "$${CI:-}" ]; then \
		echo "ERROR: CI requires ruff; e.g. uv tool install ruff" >&2; \
		exit 1; \
	else \
		echo "note: ruff not installed; skipped python linting"; \
	fi

typecheck:
	@if command -v mypy >/dev/null 2>&1; then \
		mypy src; \
	elif [ -n "$${CI:-}" ]; then \
		echo "ERROR: CI requires mypy; e.g. uv tool install mypy" >&2; \
		exit 1; \
	else \
		echo "note: mypy not installed; skipped type checking"; \
	fi

test:
	PYTHONPATH=src $(PYTHON) -m pytest -q

# Line coverage feeds the README badge, which CI regenerates on main.
coverage:
	PYTHONPATH=src $(PYTHON) -m coverage run --source=src -m pytest -q
	$(PYTHON) -m coverage report -m

.PHONY: help all check lint typecheck test smoke coverage

.DEFAULT_GOAL := help

# uv routes every tool through the project environment built from the
# committed uv.lock (--frozen never re-resolves), so a contributor runs the
# exact versions CI installs, and a missing or stale .venv heals itself on
# the next make invocation instead of dying inside an import.
# Without uv on PATH, bare tools still work: the core has no dependencies,
# but ruff/mypy/pytest must then come from the host.
UV_PRESENT := $(shell command -v uv >/dev/null 2>&1 && echo yes || echo no)

ifeq ($(UV_PRESENT),yes)
PYTHON := uv run --frozen python3
RUFF := uv run --frozen ruff
MYPY := uv run --frozen mypy
DEADEYE := uv run --frozen deadeye
else
PYTHON := python3
RUFF := $(shell command -v ruff >/dev/null 2>&1 && echo ruff)
MYPY := $(shell command -v mypy >/dev/null 2>&1 && echo mypy)
DEADEYE := env PYTHONPATH=src $(PYTHON) -m deadeye
endif

help:
	@echo "check     lint + typecheck + compile"
	@echo "test      offline test suite"
	@echo "smoke     exercise the CLI entry points CI exercises"
	@echo "coverage  test suite with a line-coverage report"
	@echo "all       check + test + smoke: everything CI's offline job runs"
	@echo
	@echo "single test module:  uv run pytest tests/test_config.py -q"
	@echo "single test by name: uv run pytest -k redact -q"

all: check test smoke

check: lint typecheck
	$(PYTHON) -m compileall -q src tests

lint:
ifdef RUFF
	$(RUFF) check .
	$(RUFF) format --check .
else
	@if [ -n "$${CI:-}" ]; then \
		echo "ERROR: CI requires ruff; run scripts/bootstrap or: uv tool install ruff" >&2; \
		exit 1; \
	else \
		echo "note: ruff not installed; skipped python linting"; \
	fi
endif

typecheck:
ifdef MYPY
	$(MYPY) src scripts
else
	@if [ -n "$${CI:-}" ]; then \
		echo "ERROR: CI requires mypy; run scripts/bootstrap or: uv tool install mypy" >&2; \
		exit 1; \
	else \
		echo "note: mypy not installed; skipped type checking"; \
	fi
endif

test:
	PYTHONPATH=src $(PYTHON) -m pytest -q

# The three CLI entry points CI runs (make smoke) before the suite, so a
# broken console script or capability registry surfaces before push, not after.
smoke:
	$(DEADEYE) --help > /dev/null
	$(DEADEYE) schema > /dev/null
	$(DEADEYE) doctor --json > /dev/null
	@echo "cli entry points ok"

# Line coverage feeds the README badge, which CI regenerates on main.
coverage:
	PYTHONPATH=src $(PYTHON) -m coverage run --source=src -m pytest -q
	$(PYTHON) -m coverage report -m

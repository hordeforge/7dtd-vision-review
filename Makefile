.PHONY: help all check lint lint-shell typecheck test smoke coverage badge \
        dist dist-verify clean

.DEFAULT_GOAL := help

# Recipes run under bash with -e and pipefail, the same failure behavior the
# shell scripts under scripts/ carry: a failing line inside a pipeline must not
# pass because a later command in the pipe exited 0.
SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

# uv routes every tool through the project environment built from the
# committed uv.lock. --locked is what enforces that: it fails when uv.lock no
# longer matches pyproject.toml, where --frozen would install a stale
# resolution and quietly test or ship against dependency versions the
# repository no longer declares. A contributor runs the exact versions CI
# installs, and a missing or stale .venv heals itself on the next make
# invocation instead of dying inside an import.
# Without uv on PATH, bare tools still work: the core has no dependencies,
# but ruff/mypy/pytest must then come from the host.
UV_PRESENT := $(shell command -v uv >/dev/null 2>&1 && echo yes || echo no)

# The shell scripts under scripts/ are a second language in the tree and
# carry the same weight as the Python: e2e.sh is the release gate, bootstrap
# is the environment every contributor runs. shellcheck is not a Python tool,
# so it comes from the host (it ships in the GitHub runner image) rather than
# from the uv project environment.
SHELLCHECK := $(shell command -v shellcheck >/dev/null 2>&1 && echo shellcheck)
SHELL_SOURCES := scripts/bootstrap scripts/e2e.sh

# uv picks the project interpreter from .python-version, and silently rebuilds
# .venv with it when the existing one disagrees. A CI job that synced a
# specific version (uv sync --python 3.11) would then have that venv replaced
# before a single test ran, and every matrix leg would test the same
# interpreter. UV_PYTHON (uv's own env var, also settable on the command line)
# pins it through to uv run. Leave it unset for a normal checkout.
ifdef UV_PYTHON
UV_INTERPRETER := --python $(UV_PYTHON)
else
UV_INTERPRETER :=
endif

ifeq ($(UV_PRESENT),yes)
PYTHON := uv run --locked $(UV_INTERPRETER) python3
RUFF := uv run --locked $(UV_INTERPRETER) ruff
MYPY := uv run --locked $(UV_INTERPRETER) mypy
DEADEYE := uv run --locked $(UV_INTERPRETER) deadeye
else
PYTHON := python3
RUFF := $(shell command -v ruff >/dev/null 2>&1 && echo ruff)
MYPY := $(shell command -v mypy >/dev/null 2>&1 && echo mypy)
DEADEYE := env PYTHONPATH=src $(PYTHON) -m deadeye
endif

help:
	@echo "check     lint + shell lint + typecheck + compile"
	@echo "test      offline test suite"
	@echo "smoke     exercise the CLI entry points CI exercises"
	@echo "coverage  test suite with a line-coverage report"
	@echo "badge     coverage report plus the README badge SVG (BADGE=path)"
	@echo "all       check + test + smoke: everything CI's offline job runs"
	@echo "dist      build the sdist and wheel into dist/ the way a release does"
	@echo "dist-verify   build the same tree twice under a different clock, locale,"
	@echo "           timezone, and hash seed, and diff the artifacts byte for byte"
	@echo "clean     remove the build outputs"
	@echo
	@echo "single test module:  make test TEST=tests/test_config.py"
	@echo "single test by name: make test TEST='-k redact'"

all: check test smoke

check: lint lint-shell typecheck
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
		echo "WARNING: ruff not installed; python linting did NOT run." >&2; \
		echo "         CI installs ruff and fails without it, so a green local" >&2; \
		echo "         run here can still fail the push. Install it:" >&2; \
		echo "           scripts/bootstrap   (or: uv tool install ruff)" >&2; \
	fi
endif

lint-shell:
ifdef SHELLCHECK
	$(SHELLCHECK) -x $(SHELL_SOURCES)
else
	@if [ -n "$${CI:-}" ]; then \
		echo "ERROR: CI requires shellcheck for the shell scripts under scripts/" >&2; \
		exit 1; \
	else \
		echo "WARNING: shellcheck not installed; shell linting did NOT run." >&2; \
		echo "         CI installs shellcheck (it ships in the runner image) and" >&2; \
		echo "         fails without it, so a green local run here can still fail" >&2; \
		echo "         the push. Install it from your system package manager." >&2; \
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
		echo "WARNING: mypy not installed; type checking did NOT run." >&2; \
		echo "         CI installs mypy and fails without it, so a green local run" >&2; \
		echo "         here can still fail the push. Install it:" >&2; \
		echo "           scripts/bootstrap   (or: uv tool install mypy)" >&2; \
	fi
endif

# TEST narrows the run the same way the suite runs: a module, a directory, or
# a bare pytest expression such as `-k redact`. It stays inside this recipe so
# a narrowed run uses the same interpreter, the same PYTHONPATH, and the same
# locked environment as the full one; a pytest invocation typed straight into
# the shell is a different run and can pass where the suite fails.
TEST ?=

test:
	PYTHONPATH=src $(PYTHON) -m pytest -q $(TEST)

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

# The badge render is a project script and needs coverage importable, so it
# runs under $(PYTHON) like every other tool here. CI used to call it with a
# bare `python`, which only worked because the workflow happened to put
# .venv/bin on PATH first; `make badge BADGE=PATH` is the one command.
BADGE ?= .local/coverage.svg

badge: coverage
	@mkdir -p "$(dir $(BADGE))"
	$(PYTHON) scripts/coverage_badge.py "$(BADGE)"

# ------------------------------------------------------------------ release
# One build command for the artifacts that ship, shared with the release
# workflow, so a published build and a local build cannot drift.
#
# BUILD_ENV is the pinned environment every step below runs under. Each name
# is a default rather than an assignment on purpose: dist-verify rebuilds the
# same tree with a different wall clock, locale, timezone, and hash seed, and
# an override there is what makes the comparison mean something. A build that
# honors none of them leaks that host state into the archive and the two runs
# differ.
BUILD_ENV := LC_ALL="$${LC_ALL:-C}" TZ="$${TZ:-UTC}" PYTHONHASHSEED="$${PYTHONHASHSEED:-0}" \
	SOURCE_DATE_EPOCH="$${SOURCE_DATE_EPOCH:-$$(git log -1 --pretty=format:%ct)}"
DIST ?= dist
VERIFY_DIST ?= .local/dist-verify

dist:
ifeq ($(UV_PRESENT),yes)
	# The epoch comes from the tagged commit, and the checkout it is read from
	# is the only place it can come from: no git history and no caller-supplied
	# epoch means an unstampable build, which must stop here rather than hand
	# the canonicalizer an empty value.
	@epoch="$${SOURCE_DATE_EPOCH:-$$(git log -1 --pretty=format:%ct 2>/dev/null || true)}"; \
	if [ -z "$$epoch" ]; then \
		echo "ERROR: no SOURCE_DATE_EPOCH in the environment and no git commit to derive it from" >&2; \
		echo "       (an unpacked sdist has no history; build from a checkout, or export the epoch)" >&2; \
		exit 1; \
	fi; \
	echo "SOURCE_DATE_EPOCH=$$epoch"
	# Wiped first: an artifact left from an earlier version of this tree would
	# otherwise sit in dist/ next to the new one and ship beside it.
	@rm -rf "$(DIST)"
	@mkdir -p "$(DIST)"
	$(BUILD_ENV) uv build --out-dir "$(DIST)"
	# setuptools stamps wheels from SOURCE_DATE_EPOCH but leaves sdists with
	# checkout mtimes and the builder's uid; this canonicalizes them. A bare
	# python3 runs it: the script is stdlib-only, and the release job must not
	# have to install a dev environment to normalize what it just built.
	$(BUILD_ENV) python3 scripts/reproducible_artifacts.py "$(DIST)"
else
	@echo "ERROR: uv is required to build the distribution (scripts/bootstrap); this host has none" >&2
	@exit 1
endif

# The reproducibility claim, checked rather than asserted: the same tree built
# twice, seconds apart, under a different locale, timezone, and hash seed,
# must produce the same bytes. A mismatch names the artifact and, when
# diffoscope is installed, shows what inside it moved.
dist-verify: dist
	@verify_locale=C; \
	if locale -a 2>/dev/null | grep -qi '^C\.utf-\?8$$'; then verify_locale=C.UTF-8; fi; \
	LC_ALL="$$verify_locale" TZ=Asia/Tokyo PYTHONHASHSEED=1 \
		$(MAKE) --no-print-directory dist DIST="$(VERIFY_DIST)"
	@diff <(cd "$(DIST)" && ls -1) <(cd "$(VERIFY_DIST)" && ls -1) \
		|| { echo "ERROR: the two builds produced different artifact sets" >&2; exit 1; }
	@failed=0; \
	for first in "$(DIST)"/*.whl "$(DIST)"/*.tar.gz; do \
		second="$(VERIFY_DIST)/$$(basename "$$first")"; \
		if cmp -s "$$first" "$$second"; then \
			echo "reproducible: $$(basename "$$first")"; \
		else \
			failed=1; \
			echo "NOT reproducible: $$(basename "$$first")" >&2; \
			if command -v diffoscope >/dev/null 2>&1; then diffoscope "$$first" "$$second" || true; fi; \
		fi; \
	done; \
	test "$$failed" -eq 0

clean:
	@rm -rf "$(DIST)" "$(VERIFY_DIST)"
	@find src tests scripts -name __pycache__ -type d -prune -exec rm -rf {} +

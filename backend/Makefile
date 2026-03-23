PYTHON ?= python3
PIP ?= $(PYTHON) -m pip
SRC := mmctl orchestrator evaluation tests
CTX_TOOL ?= ../rlm_context_method_pack/multi_mind_context_method_pack/tools/ctx.py
CTX_ROOT ?= .
CTX_DEPTH ?= 4
CTX_PATTERN ?= TODO
CTX_GLOB ?=
CTX_MAX_RESULTS ?= 200
CTX_FILE ?= README.md
CTX_START ?= 1
CTX_END ?= 200
CTX_FILES ?= README.md
CTX_MAX_LINES ?= 120
CTX_TASK ?= general-workstream
CTX_OWNER ?= operator

.PHONY: help setup smoke test lint typecheck format clean ctx-tree ctx-rg ctx-peek ctx-digest ctx-start

help:
	@echo "Available targets:"
	@echo "  setup      Install project + dev deps"
	@echo "  smoke      CLI/help + syntax compile checks"
	@echo "  test       Run pytest"
	@echo "  lint       Run ruff if installed"
	@echo "  typecheck  Run mypy if installed"
	@echo "  format     Run ruff format if installed"
	@echo "  clean      Remove caches and local build artifacts"
	@echo "  ctx-tree   RLM context tree listing (override CTX_DEPTH, CTX_ROOT)"
	@echo "  ctx-rg     RLM context search (override CTX_PATTERN, CTX_ROOT, CTX_GLOB)"
	@echo "  ctx-peek   RLM context file peek (override CTX_FILE, CTX_START, CTX_END)"
	@echo "  ctx-digest RLM bounded digest (override CTX_FILES, CTX_MAX_LINES)"
	@echo "  ctx-start  Append timestamped context block (override CTX_TASK, CTX_OWNER)"

setup:
	$(PIP) install -e .[dev]

smoke:
	$(PYTHON) -m mmctl --help
	OPENAI_API_KEY=dummy ANTHROPIC_API_KEY=dummy $(PYTHON) -m mmctl config check --config config/config.example.yaml
	$(PYTHON) -m compileall -q $(SRC)

test:
	$(PYTHON) -m pytest -q

lint:
	@if $(PYTHON) -c "import ruff" >/dev/null 2>&1; then \
		$(PYTHON) -m ruff check $(SRC); \
	else \
		echo "ruff not installed; skipping lint"; \
	fi

typecheck:
	@if $(PYTHON) -c "import mypy" >/dev/null 2>&1; then \
		$(PYTHON) -m mypy orchestrator mmctl evaluation; \
	else \
		echo "mypy not installed; skipping typecheck"; \
	fi

format:
	@if $(PYTHON) -c "import ruff" >/dev/null 2>&1; then \
		$(PYTHON) -m ruff format $(SRC); \
	else \
		echo "ruff not installed; skipping format"; \
	fi

clean:
	find . -type d -name '__pycache__' -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info

ctx-tree:
	$(PYTHON) $(CTX_TOOL) tree --depth $(CTX_DEPTH) $(CTX_ROOT)

ctx-rg:
	@if [ -n "$(CTX_GLOB)" ]; then \
		$(PYTHON) $(CTX_TOOL) rg "$(CTX_PATTERN)" "$(CTX_ROOT)" --glob "$(CTX_GLOB)" --max-results $(CTX_MAX_RESULTS); \
	else \
		$(PYTHON) $(CTX_TOOL) rg "$(CTX_PATTERN)" "$(CTX_ROOT)" --max-results $(CTX_MAX_RESULTS); \
	fi

ctx-peek:
	$(PYTHON) $(CTX_TOOL) peek "$(CTX_FILE)" --start $(CTX_START) --end $(CTX_END)

ctx-digest:
	$(PYTHON) $(CTX_TOOL) digest $(CTX_FILES) --max-lines $(CTX_MAX_LINES)

ctx-start:
	@mkdir -p .ctx
	@touch .ctx/context_map.md
	@printf "\n## Workstream: %s (%s)\n- started_at_utc: %s\n- owner: %s\n\n### Verified Facts\n\n### Interfaces\n\n### Assumptions\n\n### Risks\n\n" "$(CTX_TASK)" "$$(date -u +%Y-%m-%d)" "$$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(CTX_OWNER)" >> .ctx/context_map.md
	@echo "Appended context block to .ctx/context_map.md for task: $(CTX_TASK)"

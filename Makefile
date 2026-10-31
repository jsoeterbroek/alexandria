# Alexandria workflow. Run `make` (or `make help`) for the target list.
UV ?= uv
DESCRIPTOR ?= input/input.yaml
PYTHON ?= $(UV) run python

.DEFAULT_GOAL := help
.PHONY: help sync build build_all list library verify test lint fmt fmt-check check clean refresh only

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

sync: ## Create/refresh the uv environment
	$(UV) sync

build: sync ## Convert missing books in the descriptor (skips existing ZIMs)
	$(UV) run alexandria $(DESCRIPTOR)

build_all: sync ## Rebuild every book in the descriptor, overwriting existing ZIMs
	$(UV) run alexandria $(DESCRIPTOR) --force

only: sync ## Build a single book: make only ID=11
	@test -n "$(ID)" || { echo "usage: make only ID=<gutenberg-id>" >&2; exit 2; }
	$(UV) run alexandria $(DESCRIPTOR) --only $(ID)

list: sync ## List the books in the descriptor
	$(UV) run alexandria $(DESCRIPTOR) --list

library: sync ## (Re)write output/library.xml from the built ZIM files
	$(UV) run alexandria $(DESCRIPTOR) --library

verify: sync ## Check the integrity of every built archive
	$(UV) run alexandria --verify output/*.zim

test: sync ## Run the offline test suite
	$(UV) run python -m unittest discover -s tests -v

lint: sync ## Lint with ruff
	$(UV) run ruff check src tests

fmt: sync ## Format with ruff
	$(UV) run ruff format src tests

fmt-check: sync ## Verify formatting without changing files
	$(UV) run ruff format --check src tests

check: lint fmt-check test ## Lint, format check and test

refresh: sync ## Re-download cached Gutenberg resources and rebuild everything
	$(UV) run alexandria $(DESCRIPTOR) --refresh --force

clean: ## Remove generated ZIM files, library.xml and bytecode
	rm -f output/*.zim output/*.zim.part output/library.xml
	find src tests -name __pycache__ -type d -prune -exec rm -rf {} +

distclean: clean ## Also remove the download cache and build metadata
	rm -rf cache .ruff_cache

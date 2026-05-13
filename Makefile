VENV := .venv
EXPAT_LIB := /opt/homebrew/opt/expat/lib
RUN_ENV := env DYLD_LIBRARY_PATH=$(EXPAT_LIB)
PYTHON := $(RUN_ENV) $(VENV)/bin/python
INPUT ?= examples/local-smoke-bookmarks.json
OUTPUT ?= output/result.csv
DB ?= bookmarks_queue.sqlite3

.PHONY: setup install-browsers run smoke test-gemini flush-db clean-cache clean-all

setup:
	./scripts/setup_venv.sh

install-browsers:
	$(PYTHON) -m playwright install chromium

run:
	./bookmarks --input $(INPUT) --output $(OUTPUT) --db $(DB)

smoke:
	./bookmarks --input examples/local-smoke-bookmarks.json --output /private/tmp/bookmarks-smoke.csv --db /private/tmp/bookmarks-smoke.sqlite3 --reset --limit 1 --concurrency 1

test-gemini:
	./bookmarks-python scripts/test_gemini_key.py

flush-db:
	rm -f bookmarks_queue.sqlite3 bookmarks_queue.sqlite3-shm bookmarks_queue.sqlite3-wal

clean-cache: flush-db

clean-all: clean-cache
	mkdir -p output
	find output -mindepth 1 -delete

# Firefox Bookmark Analyzer

This tool reads a Firefox bookmarks JSON export, checks each bookmarked website in a real browser, asks Gemini for a short description, and writes the results to a CSV file.

It is safe to stop and restart. Progress is saved in a SQLite database, so finished URLs are not checked again unless you reset the queue.

## Quick Start

Run these commands from this folder:

```bash
./scripts/setup_venv.sh
./bookmarks-python scripts/test_gemini_key.py
./bookmarks --input bookmark.json --output result.csv
```

That is the normal workflow:

1. Install the local Python environment.
2. Test your Gemini API key.
3. Analyze your Firefox bookmarks file.

## Step 1: Export Bookmarks From Firefox

In Firefox:

1. Open Bookmarks.
2. Choose `Manage Bookmarks`.
3. Click `Import and Backup`.
4. Choose `Backup...`.
5. Save the `.json` file into this project folder.

Example filename:

```text
bookmark.json
```

## Step 2: Set Up the Tool

Run:

```bash
./scripts/setup_venv.sh
```

This creates `.venv`, installs Python packages, and installs the Playwright browser.

You do not need to activate the venv manually. Use the helper commands:

```bash
./bookmarks
./bookmarks-python
```

These wrappers also hide the local Homebrew `DYLD_LIBRARY_PATH` fix needed by this machine's Python install.

## Step 3: Add Your Gemini API Key

Create or edit `.env` in this folder:

```bash
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-3.1-flash-lite-preview
BA_CONCURRENCY=5
BA_BROWSER_TIMEOUT_MS=20000
BA_REQUEST_TIMEOUT_SECONDS=30
BA_RETRY_ATTEMPTS=2
BA_RETRY_BACKOFF_SECONDS=0.7
BA_DOMAIN_MIN_INTERVAL_SECONDS=0.2
BA_GEMINI_RPM=6
BA_GEMINI_TPM=250000
BA_GEMINI_RPD=500
```

The script loads `.env` automatically. You do not need to run `export GEMINI_API_KEY=...`.

`BA_GEMINI_RPM` means Gemini requests per minute.

Examples:

```text
BA_GEMINI_RPM=6   # one Gemini request about every 10 seconds
BA_GEMINI_RPM=12  # one Gemini request about every 5 seconds
BA_GEMINI_RPM=60  # one Gemini request about every 1 second
```

This limit is shared across all workers. For example, if `--concurrency 5` and `BA_GEMINI_RPM=6`, page checks may run in parallel, but Gemini calls still happen about 10 seconds apart.

Other useful `.env` settings:

```text
BA_CONCURRENCY=5
How many bookmark workers run at the same time.

BA_BROWSER_TIMEOUT_MS=20000
How long Playwright waits for a page before calling it a timeout.

BA_REQUEST_TIMEOUT_SECONDS=30
How long the Gemini API request can take before timeout.

BA_RETRY_ATTEMPTS=2
How many retries are allowed. This is used for the persistent queue retry limit and transient browser/Gemini retries.

BA_RETRY_BACKOFF_SECONDS=0.7
How long to wait before retrying. Retries use exponential backoff: 0.7s, 1.4s, 2.8s, etc.

BA_DOMAIN_MIN_INTERVAL_SECONDS=0.2
Minimum delay between browser requests to the same domain.

BA_GEMINI_TPM=250000
Approximate Gemini tokens per minute limit. The tool estimates tokens from prompt length.

BA_GEMINI_RPD=500
Gemini requests per day limit for this run. When reached, AI comments stay blank instead of sending more requests.
```

## Step 4: Test Gemini

Run:

```bash
./bookmarks-python scripts/test_gemini_key.py
```

Expected result:

```text
OK: Gemini API key works. Model '...' answered: 'Ankara'
```

This test asks Gemini:

```text
What is the capital of Turkey?
```

The answer must contain:

```text
Ankara
```

## Step 5: Analyze Bookmarks

Basic run:

```bash
./bookmarks --input bookmark.json --output result.csv
```

This runs in two phases:

```text
Phase 1: deterministic browser checks
Checks live/broken status, HTTP status, title, meta description, and keywords.
Writes the CSV immediately.

Phase 2: AI comments
Adds the Gemini summary into the final `comments` column.
Writes the CSV again with comments filled in.
```

Test with only the first 10 URLs:

```bash
./bookmarks --input bookmark.json --output result.csv --reset --limit 10
```

Run with more parallel workers:

```bash
./bookmarks --input bookmark.json --output result.csv --concurrency 5
```

Run deterministic checks only, with no Gemini calls:

```bash
./bookmarks --input bookmark.json --output result.csv --skip-ai
```

Later, when Gemini is healthy again, fill only the missing AI comments:

```bash
./bookmarks --db bookmarks_queue.sqlite3 --output result.csv --ai-only
```

This is useful when Gemini returns temporary errors like:

```text
503 UNAVAILABLE: model is currently experiencing high demand
```

Override Gemini rate limit from the command line:

```bash
./bookmarks --input bookmark.json --output result.csv --gemini-rpm 6 --gemini-tpm 250000 --gemini-rpd 500
```

Override retry settings from the command line:

```bash
./bookmarks --input bookmark.json --output result.csv --retry-limit 2 --retry-backoff-seconds 0.7
```

## Resume Or Start Over

By default, the tool resumes from the SQLite queue:

```bash
./bookmarks --input bookmark.json --output result.csv
```

Use this when the previous run stopped or crashed. URLs already marked `done` are skipped.

Start from scratch:

```bash
./bookmarks --input bookmark.json --output result.csv --reset
```

Use a custom database file:

```bash
./bookmarks --input bookmark.json --output result.csv --db output/jobs.sqlite3
```

## Output CSV

The CSV contains:

```text
site_url,status,title,description,keywords,comments
```

Status values:

```text
working  # browser loaded the page successfully
broken   # browser could not load the page, or the page returned an error
pending  # not processed yet in a partial run
```

The `comments` column is the Gemini summary.

If Gemini fails or is skipped, `comments` stays blank. You can fill it later with:

```bash
./bookmarks --db bookmarks_queue.sqlite3 --output result.csv --ai-only
```

## Useful Commands

Show help:

```bash
./bookmarks --help
```

Run a tiny smoke test:

```bash
make smoke
```

Test Gemini only:

```bash
make test-gemini
```

Run with Make:

```bash
make run INPUT=bookmark.json OUTPUT=result.csv
```

Remove the default queue database:

```bash
make flush-db
```

`make clean-cache` does the same thing, kept as an older alias:

```bash
make clean-cache
```

Remove queue database and output files:

```bash
make clean-all
```

## What Each File Does

```text
bookmarks                  Friendly command for running the analyzer
bookmarks-python           Friendly command for running helper Python scripts
scripts/setup_venv.sh      Creates .venv and installs dependencies
scripts/test_gemini_key.py Tests GEMINI_API_KEY from .env
main.py                    CLI entrypoint
db.py                      SQLite queue
parser.py                  Extracts URLs from Firefox JSON
browser.py                 Checks pages with Playwright Chromium
ai.py                      Calls Gemini
worker.py                  Async worker pool
exporter.py                Writes CSV output
requirements.txt           Python dependencies
```

## How The Queue Works

The queue is stored in:

```text
bookmarks_queue.sqlite3
```

Each URL becomes one job.

Job statuses:

```text
pending     waiting to be processed
processing  currently being checked
done        completed successfully
failed      failed and may retry
```

If the app stops while a job is `processing`, the next run recovers it and retries it.

## Troubleshooting

If `.venv` is missing:

```bash
./scripts/setup_venv.sh
```

If Gemini test fails:

1. Check `.env`.
2. Make sure `GEMINI_API_KEY` is not empty.
3. Make sure `GEMINI_MODEL` is available for your API key.
4. Run:

```bash
./bookmarks-python scripts/test_gemini_key.py
```

If the browser fails to launch from a restricted environment, run the normal wrapper:

```bash
./bookmarks --input bookmark.json --output result.csv
```

If you want a clean retry:

```bash
./bookmarks --input bookmark.json --output result.csv --reset
```

## Requirements

- Python 3
- Internet access
- Firefox bookmark JSON export
- Gemini API key

The setup script installs the Python packages and Playwright browser for you.

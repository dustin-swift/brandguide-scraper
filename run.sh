#!/bin/sh
# Start the Brief Scraper at http://127.0.0.1:8765
cd "$(dirname "$0")" || exit 1
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
exec .venv/bin/python -m brief_scraper.server

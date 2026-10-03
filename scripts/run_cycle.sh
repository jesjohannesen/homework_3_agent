#!/usr/bin/env bash
# cron entry point. Secrets come from the environment / a 0600 .env, never from this file.
cd "$(dirname "$0")/.." && exec python3 -m agent run >> state/cron.log 2>&1

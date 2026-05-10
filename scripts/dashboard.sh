#!/usr/bin/env bash
cd "/Users/karuthsanker/Documents/Trading BOT"
exec ./.venv/bin/python -m uvicorn trading_bot.dashboard.app:app \
    --host 127.0.0.1 --port 8000

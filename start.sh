#!/bin/bash
set -e

# 1. Create a shared directory for the API server and the bot
mkdir -p /app/tg_data

# 2. Start the Telegram Local Bot API Server in the background
# Redirect its output to a log file AND to stdout so Railway captures it
echo "Starting Telegram Local Bot API Server..."
telegram-bot-api \
    --api-id="${TELEGRAM_API_ID}" \
    --api-hash="${TELEGRAM_API_HASH}" \
    --local \
    -d /app/tg_data \
    --http-port=8081 > /tmp/tgapi.log 2>&1 &

TG_API_PID=$!

# 3. Wait until ready, printing the API server log every 5s so we can debug
echo "Waiting for Local Bot API Server to become ready..."
MAX_WAIT=60
WAITED=0
until curl -sf "http://localhost:8081" > /dev/null 2>&1; do
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "ERROR: Local Bot API Server did not start within ${MAX_WAIT}s."
        echo "=== telegram-bot-api output ==="
        cat /tmp/tgapi.log
        echo "=== end of output ==="
        exit 1
    fi
    # Print the log every 5 seconds so we can see what's happening
    if [ $((WAITED % 5)) -eq 0 ] && [ "$WAITED" -gt 0 ]; then
        echo "--- API server log so far ---"
        cat /tmp/tgapi.log
        echo "---"
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

echo "Local Bot API Server is ready (waited ${WAITED}s). Starting bot..."

# 4. Start the Python bot in the foreground
exec python main.py

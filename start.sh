#!/bin/bash
set -e

# 1. Create a shared directory for the API server and the bot
mkdir -p /app/tg_data

# 2. Start the Telegram Local Bot API Server in the background
echo "Starting Telegram Local Bot API Server..."
telegram-bot-api \
    --api-id="${TELEGRAM_API_ID}" \
    --api-hash="${TELEGRAM_API_HASH}" \
    --local \
    -d /app/tg_data \
    --http-port=8081 &

# 3. Wait until the API server is actually ready (don't rely on a fixed sleep)
echo "Waiting for Local Bot API Server to become ready..."
MAX_WAIT=30
WAITED=0
until curl -sf "http://localhost:8081" > /dev/null 2>&1; do
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "ERROR: Local Bot API Server did not start within ${MAX_WAIT}s. Aborting."
        exit 1
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

echo "Local Bot API Server is ready (waited ${WAITED}s). Starting bot..."

# 4. Start the Python bot in the foreground
exec python main.py

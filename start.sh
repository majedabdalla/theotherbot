#!/bin/bash
set -e

mkdir -p /app/tg_data

echo "Testing telegram-bot-api binary..."
telegram-bot-api --version 2>&1

echo "Starting Telegram Local Bot API Server..."
telegram-bot-api \
    --api-id="${TELEGRAM_API_ID}" \
    --api-hash="${TELEGRAM_API_HASH}" \
    --local \
    -d /app/tg_data \
    --http-port=8081 &

TG_API_PID=$!
echo "API server PID: $TG_API_PID"

echo "Waiting for Local Bot API Server to become ready..."
MAX_WAIT=60
WAITED=0
until curl -sf "http://localhost:8081/health" > /dev/null 2>&1 || \
      curl -sf "http://localhost:8081/bot${BOT_TOKEN}/getMe" > /dev/null 2>&1; do
    if ! kill -0 $TG_API_PID 2>/dev/null; then
        echo "ERROR: API server process died!"
        exit 1
    fi
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "ERROR: API server did not become ready within ${MAX_WAIT}s."
        echo "Trying to connect anyway..."
        break
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

echo "Local Bot API Server ready (waited ${WAITED}s). Starting bot..."
exec python main.py

#!/bin/bash
set -e

mkdir -p /app/tg_data

# Test the binary works at all before trying to start it as a server
echo "Testing telegram-bot-api binary..."
if ! telegram-bot-api --version 2>&1; then
    echo "ERROR: binary failed to run, checking libraries..."
    ldd /usr/local/bin/telegram-bot-api || true
    exit 1
fi

echo "Starting Telegram Local Bot API Server..."
telegram-bot-api \
    --api-id="${TELEGRAM_API_ID}" \
    --api-hash="${TELEGRAM_API_HASH}" \
    --local \
    -d /app/tg_data \
    --http-port=8081 >> /proc/1/fd/1 2>> /proc/1/fd/2 &

TG_API_PID=$!
echo "API server PID: $TG_API_PID"

echo "Waiting for Local Bot API Server to become ready..."
MAX_WAIT=60
WAITED=0
until curl -sf "http://localhost:8081" > /dev/null 2>&1; do
    # Check if the process is still alive
    if ! kill -0 $TG_API_PID 2>/dev/null; then
        echo "ERROR: API server process died (PID $TG_API_PID)!"
        echo "Check the logs above for the error message."
        exit 1
    fi
    if [ "$WAITED" -ge "$MAX_WAIT" ]; then
        echo "ERROR: API server did not become ready within ${MAX_WAIT}s."
        exit 1
    fi
    sleep 1
    WAITED=$((WAITED + 1))
done

echo "Local Bot API Server is ready (waited ${WAITED}s). Starting bot..."
exec python main.py

#!/bin/bash
set -e

mkdir -p /app/tg_data

# Reduce glibc's tendency to hoard freed memory in per-thread arenas.
# telegram-bot-api is multi-threaded; without this, RSS climbs and never
# comes back down even after a file transfer finishes.
export MALLOC_ARENA_MAX=1

echo "Testing telegram-bot-api binary..."
telegram-bot-api --version 2>&1

echo "Starting Telegram Local Bot API Server..."
# --max-connections caps open file descriptors, which bounds how many
# concurrent file buffers TDLib will hold in memory at once. Lower this
# further (e.g. 100) if OOMs continue.
telegram-bot-api \
    --api-id="${TELEGRAM_API_ID}" \
    --api-hash="${TELEGRAM_API_HASH}" \
    --local \
    -d /app/tg_data \
    --http-port=8081 \
    --max-connections="${TG_API_MAX_CONNECTIONS:-200}" \
    --verbosity="${TG_API_VERBOSITY:-1}" &

TG_API_PID=$!
echo "API server PID: $TG_API_PID"

# Periodically clear stale cached files so tg_data doesn't grow unbounded.
# This is disk, not RAM, but on Railway's free tier disk is also limited,
# and a runaway dir can indirectly cause issues during restarts/builds.
(
  while true; do
    sleep 1800
    find /app/tg_data -type f -mmin +60 -delete 2>/dev/null || true
  done
) &

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

# MALLOC_ARENA_MAX is already exported above, so it applies here too —
# Python's own C extensions (Pillow, motor's C driver bits) benefit from
# the same fix.
echo "Local Bot API Server ready (waited ${WAITED}s). Starting bot..."
exec python main.py

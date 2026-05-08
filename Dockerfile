# ── Stage 1: Grab the official pre-compiled Telegram Bot API Server ──────────
FROM aiogram/telegram-bot-api:latest AS api-server

# ── Stage 2: Your actual bot environment ─────────────────────────────────────
FROM python:3.11-slim

# Copy the API server binary from Stage 1
COPY --from=api-server /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

# Install system dependencies (curl needed for health-check in start.sh)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libmagic-dev \
    libmagic1 \
    gcc \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code and the start script
COPY . .

# Make the start script executable
RUN chmod +x start.sh

ENV PYTHONUNBUFFERED=1

# Run the start script which launches both the API server and the bot
CMD ["./start.sh"]

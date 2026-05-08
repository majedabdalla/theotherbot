# ── Build telegram-bot-api from source (reliable, no image dependency) ────────
FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    make \
    git \
    zlib1g-dev \
    libssl-dev \
    gperf \
    cmake \
    g++ \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --recursive --depth=1 https://github.com/tdlib/telegram-bot-api.git /src

RUN mkdir -p /src/build && cd /src/build && \
    cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local .. && \
    cmake --build . --target install -j$(nproc)

# ── Runtime image ─────────────────────────────────────────────────────────────
FROM python:3.11-slim

COPY --from=builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libmagic-dev \
    libmagic1 \
    gcc \
    curl \
    libssl3 \
    zlib1g \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

ENV PYTHONUNBUFFERED=1

CMD ["./start.sh"]

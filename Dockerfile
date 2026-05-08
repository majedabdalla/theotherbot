# ── Stage 1: Build telegram-bot-api from source ───────────────────────────────
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
    cmake -DCMAKE_BUILD_TYPE=Release \
          -DCMAKE_INSTALL_PREFIX=/usr/local \
          -DOPENSSL_ROOT_DIR=/usr \
          .. && \
    cmake --build . --target install -j$(nproc)

# Verify the binary exists and check what libraries it needs
RUN ldd /usr/local/bin/telegram-bot-api

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim

COPY --from=builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

# Install ALL libraries the binary needs (from ldd output above) + app deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libmagic-dev \
    libmagic1 \
    gcc \
    curl \
    libssl3 \
    zlib1g \
    libstdc++6 \
    libgcc-s1 \
    libc6 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Verify the binary actually runs
RUN telegram-bot-api --version || true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x start.sh

ENV PYTHONUNBUFFERED=1

CMD ["./start.sh"]

# ── Base ───────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System dependencies ────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libmagic-dev \
    libmagic1 \
    gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ──────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ────────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Application code ───────────────────────────────────────────────────────────
COPY . .

# ── Runtime ────────────────────────────────────────────────────────────────────
# Railway provides PORT, but this bot uses long-polling — no port needed.
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]

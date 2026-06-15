# ─────────────────────────────────────────────────────────────────
# Stage 1: Builder — install dependencies in isolation
# ─────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

# Prevent .pyc files and enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install build tools needed for asyncpg (C extension)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─────────────────────────────────────────────────────────────────
# Stage 2: Runtime — lean final image
# ─────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Only copy installed packages from builder (no gcc in final image)
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy the bot source code
COPY . .

# Ensure the assets directory exists (for payment QR image)
RUN mkdir -p assets

# ─────────────────────────────────────────────────────────────────
# Health / meta
# ─────────────────────────────────────────────────────────────────
LABEL maintainer="Shein Coupon Bot"
LABEL description="Telegram bot for selling Shein coupon codes (Neon PostgreSQL)"

# Run as a non-root user for security
RUN useradd -m -u 1001 botuser && chown -R botuser:botuser /app
USER botuser

# Start the bot
CMD ["python", "bot.py"]

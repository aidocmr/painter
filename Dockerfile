# syntax=docker/dockerfile:1

# Stage 1: Build virtual environment with uv
FROM python:3.14-slim AS builder

# Install uv from official Astral image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies separately to leverage Docker layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Copy source code and install the project
COPY src/ ./src/
COPY main.py README.md ./
RUN uv sync --frozen --no-dev


# Stage 2: Minimal runtime image
FROM python:3.14-slim AS runner

# Optimize Python execution in container
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DATABASE_PATH="/app/data/canvas_bot.db"

# Create non-root user for security
RUN groupadd -g 10001 appuser && useradd -u 10001 -g appuser -m -d /home/appuser appuser

WORKDIR /app

# Create data directory for persistent SQLite database
RUN mkdir -p /app/data && chown -R appuser:appuser /app

# Copy virtual environment and project files from builder
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/src /app/src
COPY --from=builder --chown=appuser:appuser /app/main.py /app/main.py
COPY --from=builder --chown=appuser:appuser /app/pyproject.toml /app/pyproject.toml
COPY --from=builder --chown=appuser:appuser /app/README.md /app/README.md

# Persistent volume for SQLite database
VOLUME ["/app/data"]

USER appuser

ENTRYPOINT ["python", "main.py"]

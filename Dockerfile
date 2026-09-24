# syntax=docker/dockerfile:1

# ============================================================
# Base Image
# ============================================================

# Official UV Python image with Python 3.12
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS base

# Prevent Python from buffering logs
ENV PYTHONUNBUFFERED=1

# Compile Python files to bytecode during installation
ENV UV_COMPILE_BYTECODE=1

# Hugging Face and Torch cache directories
ENV HF_HOME=/app/.cache/huggingface
ENV TORCH_HOME=/app/.cache/torch

# ============================================================
# Build Stage
# ============================================================

FROM base AS build

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Application directory
WORKDIR /app

# Copy dependency files first
# This improves Docker layer caching
COPY pyproject.toml uv.lock ./

# Create source directory
RUN mkdir -p src

# Install Python dependencies from uv.lock
RUN uv sync --locked

# Download files required by LiveKit plugins
RUN uv run --module livekit.agents download-files

# ============================================================
# Playwright / Chromium
# ============================================================

# Store Playwright browsers in a shared location
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.cache/ms-playwright

# Install Chromium
RUN uv run playwright install chromium

# Copy the rest of the application
COPY . .

# ============================================================
# Production Stage
# ============================================================

FROM base

# Create non-root application user
ARG UID=10001

RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/app" \
    --shell "/sbin/nologin" \
    --uid "${UID}" \
    appuser

# Copy application and virtual environment
# Give ownership to appuser
COPY --from=build --chown=appuser:appuser /app /app

# Application working directory
WORKDIR /app

# ============================================================
# Chromium Runtime Dependencies
# ============================================================

RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libatk1.0-0t64 \
    libatk-bridge2.0-0t64 \
    libcups2t64 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0t64 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0t64 \
    libcairo2 \
    libasound2t64 \
    && rm -rf /var/lib/apt/lists/*

# Playwright browser location
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.cache/ms-playwright

# ============================================================
# Security
# ============================================================

# Run the application as a non-root user
USER appuser

# ============================================================
# Start LiveKit Agent
# ============================================================

# Start the LiveKit AgentServer
CMD ["uv", "run", "src/agent.py", "start"]
# ==========================================
# Stage 1: Builder / Dependency Staging
# ==========================================
ARG PYTHON_BASE=python:3.12.14-slim-bookworm@sha256:9c47360a2a0355e2da18516d0b1c2126ec22c195d2185e97347c9d98398c5bef
ARG GATEKEEPER_VERSION=dev
ARG GATEKEEPER_COMMIT=local
FROM ${PYTHON_BASE} AS builder

WORKDIR /app

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10

# Install system build dependencies required for compiling certain wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install Python requirements into a local prefix
RUN pip install --no-cache-dir --prefix=/install --default-timeout=300 \
    torch==2.13.0+cpu \
    torchvision==0.28.0+cpu \
    --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt ./
RUN pip install --no-cache-dir --prefix=/install --default-timeout=300 -r requirements.txt

# ==========================================
# Stage 2: Lean Production Runtime
# ==========================================
FROM ${PYTHON_BASE} AS runtime

WORKDIR /app

ARG GATEKEEPER_VERSION=dev
ARG GATEKEEPER_COMMIT=local

LABEL org.opencontainers.image.title="gatekeeper" \
            org.opencontainers.image.version="${GATEKEEPER_VERSION}" \
            org.opencontainers.image.revision="${GATEKEEPER_COMMIT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
        PIP_DISABLE_PIP_VERSION_CHECK=1 \
        GATEKEEPER_VERSION=${GATEKEEPER_VERSION} \
        GATEKEEPER_COMMIT=${GATEKEEPER_COMMIT}

# Install minimal runtime system dependencies (OpenCV headless dependencies)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1-mesa-glx \
    libglib2.0-0 \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy installed python packages from builder
COPY --from=builder /install /usr/local

# Create non-root system user for security
RUN useradd --create-home --shell /bin/bash gatekeeper \
    && mkdir -p /app/data \
    && chown -R gatekeeper:gatekeeper /app

# Copy application code
COPY --chown=gatekeeper:gatekeeper gatekeeper/ ./gatekeeper/
COPY --chown=gatekeeper:gatekeeper models/ ./models/
COPY --chown=gatekeeper:gatekeeper scripts/ ./scripts/
COPY --chown=gatekeeper:gatekeeper main.py ./
COPY --chown=gatekeeper:gatekeeper config.json ./

USER gatekeeper

# Expose health probe port
EXPOSE 8080

# Native Docker Healthcheck probing the Sprint 1 readiness endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Default command invoking the GatekeeperRunner bootstrap
CMD ["python", "main.py"]
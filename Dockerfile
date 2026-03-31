# =============================================================================
# QuadriFlow API — Docker Image
#
# Quad-dominant remeshing service wrapping the QuadriFlow C++ binary
# behind a FastAPI endpoint.
#
# Build:
#   docker build -t quadriflow-api:latest .
#
# Run (quick test):
#   docker run -p 8200:8200 quadriflow-api:latest
#
# Swagger UI:
#   http://localhost:8200/docs
# =============================================================================

FROM ubuntu:22.04

# ── Build-time arguments ──────────────────────────────────────────────────────
ARG http_proxy=""
ARG https_proxy=""
ARG no_proxy="localhost,127.0.0.1"

ENV http_proxy=${http_proxy} \
    https_proxy=${https_proxy} \
    HTTP_PROXY=${http_proxy} \
    HTTPS_PROXY=${https_proxy} \
    no_proxy=${no_proxy} \
    NO_PROXY=${no_proxy}

# ── Environment variables ─────────────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ── System packages (C++ build chain + Python) ───────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    wget \
    curl \
    python3.10 \
    python3.10-dev \
    python3-pip \
    # QuadriFlow C++ dependencies
    libboost-all-dev \
    libeigen3-dev \
    && rm -rf /var/lib/apt/lists/*

# ── Python 3.10 as default interpreter ───────────────────────────────────────
RUN update-alternatives --install /usr/bin/python  python  /usr/bin/python3.10 1 \
 && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 \
 && python -m pip install --upgrade --no-cache-dir pip setuptools wheel

WORKDIR /app

# =============================================================================
# STEP 1 — Build QuadriFlow C++ binary
# =============================================================================
COPY CMakeLists.txt /app/CMakeLists.txt
COPY cmake/         /app/cmake/
COPY src/           /app/src/
COPY 3rd/           /app/3rd/

RUN mkdir -p /app/build \
 && cd /app/build \
 && cmake .. -DCMAKE_BUILD_TYPE=release -DBUILD_OPENMP=ON \
 && make -j"$(nproc)" \
 && cp /app/build/quadriflow /usr/local/bin/quadriflow \
 && rm -rf /app/build

# =============================================================================
# STEP 2 — Python API dependencies
# =============================================================================
COPY requirements-api.txt /app/requirements-api.txt
RUN pip install --no-cache-dir -r /app/requirements-api.txt

# =============================================================================
# STEP 3 — Application source
# =============================================================================
COPY quadriflow_api.py /app/quadriflow_api.py

RUN mkdir -p /app/outputs /app/logs

# ── Port ──────────────────────────────────────────────────────────────────────
EXPOSE 8200

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK \
    --interval=30s \
    --timeout=10s \
    --start-period=10s \
    --retries=3 \
    CMD curl -f http://localhost:8200/health || exit 1

# ── Entrypoint ────────────────────────────────────────────────────────────────
CMD ["python", "-m", "uvicorn", "quadriflow_api:app", \
     "--host", "0.0.0.0", \
     "--port", "8200", \
     "--workers", "1", \
     "--log-level", "info"]

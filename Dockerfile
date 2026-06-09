# Dockerfile — PIM Ingestion Agent
# Uses multi-stage build to minimize final image size

# ──────────────────────────────────────────────────────
# STAGE 1: Base — install system deps + Python packages
# ──────────────────────────────────────────────────────
FROM python:3.13-slim AS base

WORKDIR /app

# Install system dependencies (xlrd needs libxml2 for legacy xls support)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifest first (caches this layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ──────────────────────────────────────────────────────
# STAGE 2: Runtime — minimal image with only what's needed
# ──────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

WORKDIR /app

# Copy Python packages from base stage
COPY --from=base /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=base /usr/local/bin /usr/local/bin

# Copy application code
COPY api.py interactive_graph.py interactive_state.py state.py agents.py helpers.py helpers_zip.py learning.py merger.py meta_exporter.py graph.py main.py ./
COPY tools/ ./tools/
COPY chat.html ./

# Create required directories
RUN mkdir -p output cache uploads

# Environment variables
ENV GEMINI_API_KEY=""
ENV PORT=8000

# Expose the application port
EXPOSE 8000

# Start the FastAPI server
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]

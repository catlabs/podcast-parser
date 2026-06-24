# Azure.2a — CPU-only torch + multi-stage build for a slim, amd64-ready image.
#
# The --platform flag is passed at build time, not hardcoded here, so the file
# stays portable across local dev (arm64 Mac) and cloud (linux/amd64 ACR task):
#
#   Local build : docker build --platform linux/amd64 -t podcast-search:azure2a .
#   ACR build   : az acr build --registry $ACR_NAME --platform linux/amd64 \
#                   -t podcast-search:$TAG .  (see deploy/azure-containerapp.sh)
#
# Run   : docker run -d -p 8000:8000 podcast-search:azure2a
# Offline proof: docker run --network none -p 8000:8000 podcast-search:azure2a

# ── Build stage ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# build-essential is needed to compile chromadb / tokenizers wheels on some
# platforms. It stays in this stage only — compiled .so files are copied to the
# final image; build tools are not (smaller attack surface).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install into an isolated venv so we can COPY the whole tree cleanly.
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH" \
    PIP_NO_CACHE_DIR=1

COPY requirements-search-service.txt ./

# CPU-only torch — sentence-transformers needs torch but we do CPU inference.
# Installing it first (from the PyTorch CPU index) keeps the CUDA libraries
# (~7 GB) out of the image. Must precede the requirements install so
# sentence-transformers finds torch already satisfied.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements-search-service.txt

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Shared, world-readable HF cache so the non-root runtime user can read the
    # model baked below (root writes it during build).
    HF_HOME=/opt/hf-cache \
    PATH="/venv/bin:$PATH"

# Copy compiled packages from the builder (no build tools in final image).
COPY --from=builder /venv /venv

WORKDIR /app

# Bake the embedding model into the image (downloads once, at build time).
# `minilm` = all-MiniLM-L6-v2 per EMBED_REGISTRY.
RUN mkdir -p "$HF_HOME" \
    && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# After the bake, force offline mode so a missing/broken cache fails loudly at
# runtime instead of silently re-downloading over the network.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# Application code.
COPY rag/ ./rag/

# Bake the read-only `podcasts` Chroma collection and point CHROMA_DIR at it.
# (metadata.db is excluded by .dockerignore — search does not need it.)
COPY rag/data/chroma/ /app/rag/data/chroma/
ENV CHROMA_DIR=/app/rag/data/chroma

# Run as a non-root user — basic container hygiene.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
CMD ["uvicorn", "rag.service:app", "--host", "0.0.0.0", "--port", "8000"]

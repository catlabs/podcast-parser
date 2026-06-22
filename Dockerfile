# Azure.1 — self-contained image for the Phase-1 SearchAgent HTTP service
# (rag/service.py). Model + Chroma `podcasts` collection are baked at build
# time so the container runs fully offline, with no secrets and no network.
#
# Build : docker build -t podcast-search:azure1 .
# Run   : docker run -d -p 8000:8000 podcast-search:azure1
# Offline proof: docker run --network none -p 8000:8000 podcast-search:azure1

FROM python:3.12-slim

# Toolchain for any wheels that need to compile (chromadb / tokenizers fall
# back to source on some platforms). Removed from the layer in the same RUN to
# keep the image lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Shared, world-readable HF cache so the non-root runtime user can read the
    # model baked below (root writes it during build).
    HF_HOME=/opt/hf-cache

WORKDIR /app

# Dependencies first — separate layer so code/Chroma changes don't bust the
# (expensive) pip + torch download cache.
COPY requirements-search-service.txt ./
RUN pip install --no-cache-dir -r requirements-search-service.txt

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

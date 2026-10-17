# One container: FalkorDB (Redis + the FalkorDB module) and the Repo Brain app.
# Pinned to the exact FalkorDB build the graph snapshot was saved with, so it loads unchanged.
FROM falkordb/falkordb@sha256:b0bdcac2cbaff989e77cb0610439b7a9289e86de033ccbbd4368b66fb680ae7a

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies first, against an empty package, so source edits don't reinstall them.
COPY pyproject.toml ./
RUN mkdir repobrain && touch repobrain/__init__.py \
    && python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -e .
ENV PATH=/opt/venv/bin:$PATH
COPY repobrain ./repobrain
COPY web ./web

# Bake the embedding model in, so the first search needs no download.
# PRELOAD_MODEL=0 skips it for local builds that mount an existing model cache at /app/data/models.
ARG PRELOAD_MODEL=1
RUN if [ "$PRELOAD_MODEL" = "1" ]; then \
      python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/app/data/models')"; \
    fi

COPY snapshot/dump.rdb /app/snapshot/dump.rdb
COPY deploy/start.sh /app/start.sh

# Hugging Face Spaces runs containers as uid 1000.
RUN chmod +x /app/start.sh && chown -R 1000:1000 /app
USER 1000

ENV FALKORDB_HOST=127.0.0.1 \
    FALKORDB_PORT=6379 \
    PORT=7860 \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1
EXPOSE 7860
ENTRYPOINT ["/app/start.sh"]
CMD []

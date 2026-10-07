FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PORT=8080 METRICS_PORT=9100 FASTEMBED_CACHE_PATH=/opt/fastembed

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock .
RUN pip install --prefer-binary -r requirements.lock

# Bake the FlashRank ONNX model into the image (no runtime download, no per-request load)
RUN python -c "from flashrank import Ranker; Ranker(cache_dir='/opt/flashrank')"

# Bake the fastembed BM25 assets (stopwords/stemmer files) so startup never needs the network
RUN python -c "from fastembed import SparseTextEmbedding; SparseTextEmbedding('Qdrant/bm25')"

COPY app ./app
COPY gunicorn.conf.py .

RUN useradd --system --uid 10001 --no-create-home app && mkdir -p /tmp/prom && chown -R app /opt/flashrank /opt/fastembed /tmp/prom
USER app

EXPOSE 8080 9100
HEALTHCHECK --interval=15s --timeout=3s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:${PORT}/healthz || exit 1

# api:    gunicorn -c gunicorn.conf.py app.main:app   (default)
# worker: arq app.worker.WorkerSettings
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.main:app"]

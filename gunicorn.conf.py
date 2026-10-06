"""gunicorn -c gunicorn.conf.py app.main:app"""
import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
worker_class = "uvicorn.workers.UvicornWorker"
workers = int(os.environ.get("WEB_CONCURRENCY", min(multiprocessing.cpu_count() * 2 + 1, 8)))
timeout = 60            # > LLM timeout (30s) + retries budget
graceful_timeout = 30
keepalive = 5
max_requests = 5000     # recycle workers to bound memory growth
max_requests_jitter = 500
accesslog = None
os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/tmp/prom")
os.makedirs(os.environ["PROMETHEUS_MULTIPROC_DIR"], exist_ok=True)


def child_exit(server, worker):  # keep prometheus multiprocess files tidy
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(worker.pid)


def on_starting(server):  # master process: serve aggregated metrics on the internal port
    from app.metrics_server import start

    start(int(os.environ.get("METRICS_PORT", "9100")))

"""Prometheus exposition on a SEPARATE internal port (never on the public API port).

gunicorn master starts it (see gunicorn.conf.py); standalone:  python -m app.metrics_server
Multiprocess-safe when PROMETHEUS_MULTIPROC_DIR is set (aggregates all worker files).
"""
from __future__ import annotations

import os
import sys

from prometheus_client import REGISTRY, CollectorRegistry, multiprocess, start_http_server


def registry() -> CollectorRegistry:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        reg = CollectorRegistry()
        multiprocess.MultiProcessCollector(reg)
        return reg
    return REGISTRY


def start(port: int, addr: str = "0.0.0.0"):  # noqa: S104 (bound inside the pod/network; not exposed by the Service)
    return start_http_server(port, addr=addr, registry=registry())


if __name__ == "__main__":
    import threading

    start(int(os.environ.get("METRICS_PORT", "9100")))
    print("metrics on :" + os.environ.get("METRICS_PORT", "9100"), file=sys.stderr)
    threading.Event().wait()

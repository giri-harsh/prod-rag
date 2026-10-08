"""Static check: every metric and label used by the Prometheus rules and the Grafana dashboards really exists.

    python scripts/check_metric_names.py [alerts.yml | dashboard.json ...]

Why: a rule that names a metric the app does not export is silently dead - the expression evaluates to no data
forever and the alert never fires. promtool checks only the SYNTAX of an expression, never the names in it.
The app's own metrics are discovered by importing the modules that define them (not from a scrape, so the check
needs no running service); metrics that come from another exporter are listed in EXTERNAL below.
Exit status: 0 = every name/label is known, 1 = at least one is not, 2 = bad usage.
"""
from __future__ import annotations

import importlib
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# modules that define the app's own metrics (grep: "= Counter(" / "= Gauge(" under app/)
METRIC_MODULES = [
    "app.observability.metrics",
    "app.ratelimit",
    "app.services.embedding_cache",
    "app.services.semcache",
]

# series the app does not export itself: prometheus-fastapi-instrumentator (http_*), the Prometheus server (up, ALERTS),
# and the python_* / process_* families from prometheus_client. Labels Prometheus adds to every series are always allowed.
EXTERNAL: dict[str, set[str]] = {
    "up": set(),
    "ALERTS": {"alertname", "alertstate", "severity", "component"},
    "http_requests_total": {"method", "status", "handler"},
    "http_request_duration_seconds_bucket": {"method", "handler", "le"},
    "http_request_duration_seconds_count": {"method", "handler"},
    "http_request_duration_seconds_sum": {"method", "handler"},
    "http_request_duration_highr_seconds_bucket": {"le"},
    "http_request_duration_highr_seconds_count": set(),
    "http_request_duration_highr_seconds_sum": set(),
    "http_requests_inprogress": {"method", "handler"},
}
TARGET_LABELS = {"job", "instance", "le", "quantile", "__name__"}

# a PromQL metric selector: NAME or NAME{...}. The lookbehind rejects `by (foo)` / function names by requiring that the
# name is not preceded by an identifier character; PromQL keywords and functions are filtered out by KEYWORDS.
SELECTOR = re.compile(r"(?<![\w:.])([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(\{[^}]*\})?")
LABEL = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)")
KEYWORDS = {
    "and", "or", "unless", "by", "without", "on", "ignoring", "group_left", "group_right", "offset", "bool",
    "rate", "irate", "increase", "sum", "avg", "min", "max", "count", "count_values", "stddev", "stdvar",
    "topk", "bottomk", "quantile", "histogram_quantile", "absent", "absent_over_time", "present_over_time",
    "time", "scalar", "vector", "delta", "idelta", "deriv", "predict_linear", "changes", "resets", "abs",
    "ceil", "floor", "round", "clamp", "clamp_max", "clamp_min", "exp", "ln", "log2", "log10", "sqrt", "sgn",
    "label_replace", "label_join", "sort", "sort_desc", "last_over_time", "avg_over_time", "sum_over_time",
    "min_over_time", "max_over_time", "count_over_time", "quantile_over_time", "stddev_over_time", "timestamp",
    "group", "inf", "nan",
}


def app_metrics() -> dict[str, set[str]]:
    """{series name -> allowed label names} for every metric the app defines, with the suffixes PromQL uses."""
    from prometheus_client.metrics import MetricWrapperBase

    out: dict[str, set[str]] = {}
    for name in METRIC_MODULES:
        mod = importlib.import_module(name)
        for obj in vars(mod).values():
            if not isinstance(obj, MetricWrapperBase):
                continue
            base, labels = obj._name, set(obj._labelnames)
            suffixes = {
                "counter": ("_total", "_created"),
                "gauge": ("",),
                "summary": ("_count", "_sum", "_created"),
                "histogram": ("_bucket", "_count", "_sum", "_created"),
                "info": ("_info",),
            }.get(obj._type, ("",))
            for sfx in suffixes:
                out.setdefault(base + sfx, set()).update(labels)
    return out


def used(expr: str) -> list[tuple[str, set[str]]]:
    """[(metric name, label names)] for every selector in a PromQL expression; strings and durations are skipped."""
    expr = re.sub(r'"[^"]*"|\'[^\']*\'', '""', expr)  # label VALUES may contain anything, including metric-like words
    # aggregation modifiers list LABEL names in parentheses ("sum by (upstream)"): drop them, they are not metrics
    expr = re.sub(r"\b(by|without|on|ignoring|group_left|group_right)\b\s*\([^)]*\)", " ", expr)
    out = []
    for m in SELECTOR.finditer(expr):
        name, block = m.group(1), m.group(2) or ""
        if name.lower() in KEYWORDS or re.fullmatch(r"\d+[smhdwy]", name):
            continue
        out.append((name, {lm.group(1) for lm in LABEL.finditer(block)}))
    return out


def rules_of(path: pathlib.Path):
    """(context, name, expr) for every PromQL expression in a Prometheus rule file or a Grafana dashboard JSON."""
    if path.suffix == ".json":
        import json

        doc = json.loads(path.read_text(encoding="utf-8"))
        for panel in _panels(doc.get("panels", [])):
            title = panel.get("title") or f"panel {panel.get('id')}"
            for t in panel.get("targets", []) or []:
                if t.get("expr"):
                    yield panel.get("type", "?"), title, t["expr"]
        for ann in doc.get("annotations", {}).get("list", []):
            if ann.get("expr"):
                yield "annotation", ann.get("name", "?"), ann["expr"]
        return
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for group in doc.get("groups", []):
        for rule in group.get("rules", []):
            yield group.get("name", "?"), rule.get("alert") or rule.get("record") or "?", rule.get("expr", "")


def _panels(panels: list) -> list:
    """Flatten Grafana rows, whose child panels live in panel["panels"] when the row is collapsed."""
    out = []
    for p in panels:
        out.append(p)
        out.extend(_panels(p.get("panels", []) or []))
    return out


def main(argv: list[str]) -> int:
    files = [pathlib.Path(a) for a in argv] or [ROOT / "deploy/observability/prometheus/alerts.yml"]
    known = {**app_metrics(), **EXTERNAL}
    problems, checked = [], 0
    for f in files:
        if not f.exists():
            print(f"check_metric_names: {f} not found", file=sys.stderr)
            return 2
        for group, alert, expr in rules_of(f):
            for name, labels in used(expr):
                checked += 1
                if name not in known:
                    problems.append(f"{f.name} [{group}/{alert}] unknown metric: {name}")
                    continue
                for lab in labels - known[name] - TARGET_LABELS:
                    problems.append(f"{f.name} [{group}/{alert}] metric {name} has no label {lab!r}")
    for p in problems:
        print("check_metric_names FAILED: " + p, file=sys.stderr)
    if problems:
        return 1
    print(f"check_metric_names OK: {checked} metric references in {len(files)} file(s) exist with the labels used")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

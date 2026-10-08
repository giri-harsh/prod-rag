#!/usr/bin/env bash
# Validate the Prometheus rules in deploy/observability/prometheus.   Usage: bash scripts/check_alerts.sh
#   1. promtool check config / check rules / test rules   (syntax + the rule unit tests in alerts_test.yml)
#   2. scripts/check_metric_names.py                      (every metric/label in the rules is really exported by the app)
# promtool is optional: without it steps 1 degrade to a YAML parse. Install it from a Prometheus release
# (https://github.com/prometheus/prometheus/releases) and put it on PATH, or drop promtool(.exe) in _night/tools/.
set -uo pipefail
cd "$(dirname "$0")/.."
D=deploy/observability/prometheus
status=0
step() { printf '\n== %s\n' "$1"; }

if command -v promtool >/dev/null 2>&1; then PT=promtool
elif [ -x _night/tools/promtool.exe ]; then PT=_night/tools/promtool.exe
elif [ -x _night/tools/promtool ]; then PT=_night/tools/promtool
else PT=""; fi

if [ -n "$PT" ]; then
  step "promtool check config"
  "$PT" check config "$D/prometheus.yml" || status=1
  step "promtool check rules"
  "$PT" check rules "$D/alerts.yml" || status=1
  step "promtool test rules"
  ( cd "$D" && "$(cd "$OLDPWD" && realpath "$PT")" test rules alerts_test.yml ) || status=1   # rule_files in the test file are relative to it
else
  step "promtool not installed - YAML parse only (install it to run the rule unit tests)"
  if [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe; elif [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=${PYTHON:-python}; fi
  "$PY" - "$D" <<'EOF' || status=1
import pathlib, sys, yaml
d = pathlib.Path(sys.argv[1])
for f in sorted(d.glob("*.yml")):
    doc = yaml.safe_load(f.read_text(encoding="utf-8"))
    print(f"  {f.name}: valid YAML, top-level keys {list(doc)}")
rules = yaml.safe_load((d / "alerts.yml").read_text(encoding="utf-8"))
n = 0
for g in rules["groups"]:
    for r in g["rules"]:
        n += 1
        missing = [k for k in ("alert", "expr", "labels", "annotations") if k not in r]
        assert not missing, f"{g['name']}/{r.get('alert')} is missing {missing}"
        assert "runbook_url" in r["annotations"], f"{g['name']}/{r['alert']} has no runbook_url"
        assert r["labels"].get("severity") in ("warning", "critical"), f"{g['name']}/{r['alert']} has no severity"
print(f"  alerts.yml: {n} rules, each with expr/severity/annotations/runbook_url")
EOF
fi

step "check_metric_names.py (alert rules + Grafana dashboards)"
if [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe; elif [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=${PYTHON:-python}; fi
"$PY" scripts/check_metric_names.py "$D/alerts.yml" deploy/observability/grafana/*.json || status=1

step "Grafana dashboards parse and declare a datasource variable"
"$PY" - <<'EOF' || status=1
import json, pathlib
for f in sorted(pathlib.Path("deploy/observability/grafana").glob("*.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    names = {v["name"] for v in d.get("templating", {}).get("list", [])}
    panels = [p for p in d["panels"] if p.get("type") != "row"]
    assert d.get("uid") and d.get("title"), f"{f.name}: needs a stable uid and a title"
    assert "ds" in names, f"{f.name}: no 'ds' datasource variable (the dashboard would hard-code one Prometheus uid)"
    assert all(p.get("description") for p in panels), f"{f.name}: every panel needs a description"
    print(f"  {f.name}: {len(panels)} panels, uid {d['uid']!r}")
EOF

printf '\n'
[ "$status" -eq 0 ] && echo "check_alerts OK" || echo "check_alerts FAILED" >&2
exit "$status"

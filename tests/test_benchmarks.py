"""The landing page's headline numbers must be derived from the repository's own result files, never typed in."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILE = ROOT / "web" / "server" / "benchmarks.json"


def test_committed_benchmarks_match_their_sources():
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_benchmarks.py"), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_every_metric_names_a_source_that_exists_and_says_how_it_was_measured():
    metrics = json.loads(FILE.read_text(encoding="utf-8"))["metrics"]
    assert metrics, "the landing page needs its cards"
    for m in metrics:
        assert m["value"] and m["label"] and m["env"] and m["source"]
        assert (ROOT / m["source"].split(" (")[0]).exists(), m["source"]


def test_the_throughput_card_is_labelled_as_a_simulated_laptop_run():
    """128.6 RPS is real but it is NOT a production number: the LLM and embeddings were simulated on a laptop."""
    m = next(m for m in json.loads(FILE.read_text(encoding="utf-8"))["metrics"] if m["id"] == "throughput")
    assert "SIMULATED" in m["env"] and "laptop" in m["env"]
    assert m["value"] == "128.6 RPS"

"""Structural checks on the Kubernetes manifests.

There is no cluster and no kubectl/kustomize in CI here, so these are parse-and-wire checks, not admission
checks: every manifest must be valid YAML with apiVersion/kind/name, and every path a kustomization lists
must exist. That last one is what caught deploy/k8s/cnpg having no kustomization.yaml while cluster.yaml
documented `kubectl apply -k deploy/k8s/cnpg`.

What these tests deliberately do NOT claim: that the manifests apply cleanly, or that the pgbouncer image
honours a given environment variable. See DOCS/16_POSTGRES_HA.md for what is still unverified.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

K8S = pathlib.Path(__file__).parent.parent / "deploy" / "k8s"
MANIFESTS = sorted(K8S.rglob("*.yaml"))
KUSTOMIZATIONS = sorted(K8S.rglob("kustomization.yaml"))


def test_there_are_manifests_to_check():
    assert MANIFESTS, f"no manifests found under {K8S}"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: str(p.relative_to(K8S)).replace("\\", "/"))
def test_manifest_parses_and_identifies_itself(path: pathlib.Path):
    docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    assert docs, f"{path} has no YAML documents"
    for d in docs:
        assert d.get("apiVersion"), f"{path}: a document has no apiVersion"
        assert d.get("kind"), f"{path}: a document has no kind"
        if d["kind"] != "Kustomization":
            assert d.get("metadata", {}).get("name"), f"{path}: a {d['kind']} has no metadata.name"


@pytest.mark.parametrize("path", KUSTOMIZATIONS, ids=lambda p: str(p.relative_to(K8S)).replace("\\", "/"))
def test_kustomization_resources_all_exist(path: pathlib.Path):
    k = yaml.safe_load(path.read_text(encoding="utf-8"))
    missing = [r for r in (k.get("resources") or []) if not (path.parent / r).exists()]
    assert not missing, f"{path} lists resources that do not exist: {missing}"


def test_every_kustomize_target_referenced_in_a_manifest_comment_exists():
    """A `kubectl apply -k <dir>` written in a comment is an instruction a human will follow verbatim."""
    import re

    bad = []
    for path in MANIFESTS:
        for d in re.findall(r"kubectl apply (?:--server-side )?-k ([^\s`'\"]+)", path.read_text(encoding="utf-8")):
            target = pathlib.Path(__file__).parent.parent / d
            if not (target / "kustomization.yaml").exists():
                bad.append(f"{path.name} -> {d}")
    assert not bad, f"documented kustomize targets with no kustomization.yaml: {bad}"


def test_pgbouncer_runs_more_than_one_replica_and_spreads_them():
    """The whole app's database traffic goes through this Service, so a single pod is a single point of
    failure - and two pods on one node are too."""
    docs = [
        d
        for d in yaml.safe_load_all((K8S / "base" / "pgbouncer.yaml").read_text(encoding="utf-8"))
        if d and d.get("kind") == "Deployment"
    ]
    assert len(docs) == 1
    spec = docs[0]["spec"]
    assert spec["replicas"] >= 2
    anti = spec["template"]["spec"]["affinity"]["podAntiAffinity"]
    terms = anti.get("preferredDuringSchedulingIgnoredDuringExecution") or anti.get(
        "requiredDuringSchedulingIgnoredDuringExecution"
    )
    assert terms, "pgbouncer has no podAntiAffinity: both replicas can share one node"


def test_transaction_pooling_is_matched_by_a_client_that_disables_prepared_statements():
    """pgbouncer transaction mode breaks server-side prepared statements. Both the hand-rolled Deployment and
    the CNPG Pooler declare poolMode/POOL_MODE=transaction, which is only safe because the psycopg pool sets
    prepare_threshold=0. If that ever changes, these manifests silently become wrong."""
    container = (pathlib.Path(__file__).parent.parent / "app" / "container.py").read_text(encoding="utf-8")
    assert '"prepare_threshold": 0' in container, (
        "app/container.py no longer sets prepare_threshold=0, so pgbouncer transaction pooling is unsafe"
    )

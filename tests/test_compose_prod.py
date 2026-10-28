"""Static checks of docker-compose.yml + docker-compose.prod.yml (no Docker needed).

The overlay uses Compose's `!reset` / `!override` tags; they are parsed here and applied the way Compose does
(lists are appended, `!reset` empties, `!override` replaces) so the assertions are about the MERGED result.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
LONG_RUNNING = ["postgres", "redis", "qdrant", "pgbouncer", "api", "worker", "web", "caddy"]
MEM_LIMITS = {"api": "4g", "worker": "1536m", "qdrant": "1g", "postgres": "1g", "redis": "256m"}


class Reset:
    pass


class Override:
    def __init__(self, value):
        self.value = value


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!reset", lambda loader, node: Reset())


def _override(loader, node):
    if isinstance(node, yaml.SequenceNode):
        return Override(loader.construct_sequence(node, deep=True))
    return Override(loader.construct_mapping(node, deep=True))


_Loader.add_constructor("!override", _override)


def _load(name: str) -> dict:
    return yaml.load((ROOT / name).read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506 - SafeLoader subclass


def _merge(base, over):
    if isinstance(over, Reset):
        return None
    if isinstance(over, Override):
        return over.value
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base.get(k), v) if k in base else (None if isinstance(v, Reset) else (v.value if isinstance(v, Override) else v))
        return out
    if isinstance(base, list) and isinstance(over, list):
        return base + over
    return over


@pytest.fixture(scope="module")
def base():
    return _load("docker-compose.yml")


@pytest.fixture(scope="module")
def overlay():
    return _load("docker-compose.prod.yml")


@pytest.fixture(scope="module")
def merged(base, overlay):
    return _merge(base, overlay)


def test_no_published_ports_except_caddy(merged):
    for name, svc in merged["services"].items():
        ports = svc.get("ports") or []
        if name == "caddy":
            assert sorted(str(p) for p in ports) == ["443:443", "80:80"]
        else:
            assert not ports, f"{name} publishes {ports}"
    assert "9100" not in str(merged["services"]["caddy"].get("ports"))


def test_services_present(merged):
    for name in [*LONG_RUNNING, "migrate"]:
        assert name in merged["services"], name


def test_restart_and_logging(merged):
    for name in LONG_RUNNING:
        svc = merged["services"][name]
        assert svc.get("restart") == "unless-stopped", name
        log = svc.get("logging") or {}
        assert log.get("driver") == "json-file", name
        assert log.get("options") == {"max-size": "10m", "max-file": "3"}, name
    assert merged["services"]["migrate"]["restart"] == "no"


def test_memory_limits(merged):
    for name, limit in MEM_LIMITS.items():
        assert merged["services"][name].get("mem_limit") == limit, name


def test_prod_env(merged):
    for name in ("api", "worker", "migrate"):
        env = merged["services"][name]["environment"]
        assert env["APP_ENV"] == "prod"
        assert env["USE_GATEWAY"] == "false"
        assert env["WEB_CONCURRENCY"] == "2"
        assert "${POSTGRES_PASSWORD" in env["DATABASE_URL"]
        assert "${POSTGRES_PASSWORD" in env["DIRECT_DATABASE_URL"]
        assert "@postgres:5432" in env["DIRECT_DATABASE_URL"]  # migrations bypass pgbouncer
        assert "rag:rag@" not in env["DATABASE_URL"] + env["DIRECT_DATABASE_URL"]


def test_no_hardcoded_passwords_in_prod(merged):
    assert "${POSTGRES_PASSWORD" in merged["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"]
    pgb = merged["services"]["pgbouncer"]["environment"]
    assert "${POSTGRES_PASSWORD" in pgb["DB_PASSWORD"]
    assert pgb["AUTH_TYPE"] == "scram-sha-256"
    assert "${QDRANT_API_KEY" in merged["services"]["qdrant"]["environment"]["QDRANT__SERVICE__API_KEY"]


def test_overlay_has_no_hardcoded_rag_credentials():
    text = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "rag:rag" not in text
    assert "DB_PASSWORD: rag" not in text and "POSTGRES_PASSWORD: rag" not in text


def test_every_service_environment_is_fully_resolved_after_merge(merged):
    """The overlay overrides env on top of the base `<<: *app-env` anchor; the MERGED block must be complete."""
    for name in ("api", "worker", "migrate"):
        env = merged["services"][name]["environment"]
        for key in ("APP_ENV", "DATABASE_URL", "DIRECT_DATABASE_URL", "REDIS_URL", "QDRANT_URL", "QDRANT_API_KEY",
                    "GROQ_API_KEY", "GEMINI_API_KEY", "LLM_PRIMARY_MODEL", "TRUSTED_PROXIES"):
            assert key in env, f"{name} lost {key} in the merge"
    assert merged["services"]["api"]["environment"]["LLM_PRIMARY_MODEL"].endswith("openai/gpt-oss-120b}")


def test_trusted_proxies_match_network_subnet(overlay, merged):
    subnet = overlay["networks"]["default"]["ipam"]["config"][0]["subnet"]
    assert merged["services"]["api"]["environment"]["TRUSTED_PROXIES"] == subnet
    assert merged["services"]["web"]["environment"]["BFF_TRUSTED_PROXIES"] == subnet


def test_web_env(merged):
    env = merged["services"]["web"]["environment"]
    assert env["BFF_HOST"] == "0.0.0.0"  # noqa: S104 - container-internal bind
    assert env["RAG_API_URL"] == "http://api:8080"
    assert env["BFF_ALLOWED_ORIGINS"].startswith("https://${SITE_DOMAIN")
    assert "BFF_RAG_API_KEY" in env


def test_caddy_mounts_and_proxy():
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")
    assert "{$SITE_DOMAIN:localhost}" in caddyfile
    assert "reverse_proxy web:8787" in caddyfile
    assert "Strict-Transport-Security" in caddyfile


def test_worker_healthcheck_is_disabled_in_prod(merged):
    """The image HEALTHCHECK curls :8080, which an arq worker does not serve; it would be 'unhealthy' forever."""
    assert merged["services"]["worker"]["healthcheck"] == {"disable": True}


def test_hub_client_cache_is_writable_but_not_offline(merged):
    env = merged["services"]["api"]["environment"]
    assert env["HF_HOME"].startswith("/tmp/")  # writable by the non-root app user (a root-owned token path cost ~100 s)
    # HF_HUB_OFFLINE=1 makes fastembed fail to resolve the baked Qdrant/bm25 model and silently use a worse encoder
    assert "HF_HUB_OFFLINE" not in env


# ── micro profile (docker-compose.micro.yml) ──────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def micro(base, overlay):
    return _merge(_merge(base, overlay), _load("docker-compose.micro.yml"))


def _mem_mib(v) -> int:
    s = str(v).lower()
    return int(s[:-1]) * (1024 if s.endswith("g") else 1) if s[-1] in "gm" else int(s) // (1024 * 1024)


def test_micro_has_no_published_ports_except_caddy(micro):
    for name, svc in micro["services"].items():
        assert bool(svc.get("ports")) == (name == "caddy"), name


def test_micro_single_worker_with_a_cold_start_tolerant_healthcheck(micro):
    api = micro["services"]["api"]
    assert api["environment"]["WEB_CONCURRENCY"] == "1"
    hc = api["healthcheck"]
    assert int(hc["start_period"].rstrip("s")) >= 240
    assert hc["retries"] >= 5


def test_micro_memory_limits_fit_a_small_host(micro):
    limits = {n: _mem_mib(s["mem_limit"]) for n, s in micro["services"].items() if "mem_limit" in s}
    for name in ("postgres", "redis", "qdrant", "pgbouncer", "api", "web", "caddy"):
        assert name in limits, f"{name} has no mem_limit"
    assert limits["api"] <= 600 and limits["postgres"] <= 256 and limits["qdrant"] <= 256 and limits["redis"] <= 128
    # the always-on set must fit the ~900 MiB host together with the OS once usage (not limits) is added up;
    # limits are allowed to oversubscribe a little (swap), but not by an order of magnitude
    assert sum(v for n, v in limits.items() if n != "worker") <= 1400  # the worker is scaled to 0 by default
    for name, svc in micro["services"].items():
        if "mem_reservation" in svc:
            assert _mem_mib(svc["mem_reservation"]) <= limits[name], name


def test_micro_datastores_are_tuned_down(micro):
    pg = micro["services"]["postgres"]["command"]
    assert "max_connections=40" in pg and "shared_buffers=32MB" in pg
    pool = int(micro["services"]["pgbouncer"]["environment"]["DEFAULT_POOL_SIZE"])
    assert pool + 10 <= 40, "pgbouncer pool plus migrate/admin headroom must fit postgres max_connections"
    redis = micro["services"]["redis"]["command"]
    assert "--maxmemory" in redis and redis[redis.index("--maxmemory-policy") + 1] == "allkeys-lru"
    q = micro["services"]["qdrant"]["environment"]
    assert q["QDRANT__STORAGE__ON_DISK_PAYLOAD"] == "true"
    assert "${QDRANT_API_KEY" in q["QDRANT__SERVICE__API_KEY"]  # still authenticated


def test_micro_worker_is_off_by_default_and_configurable(micro):
    assert micro["services"]["worker"]["scale"] == "${WORKER_SCALE:-0}"


def test_deploy_scripts_select_the_micro_overlay():
    helper = (ROOT / "scripts" / "deploy" / "compose_env.sh").read_text(encoding="utf-8")
    assert "docker-compose.micro.yml" in helper and "DEPLOY_PROFILE" in helper
    for name in ("bootstrap_vm.sh", "update.sh", "verify_on_vm.sh"):
        assert "compose_env.sh" in (ROOT / "scripts" / "deploy" / name).read_text(encoding="utf-8"), name


def test_every_compose_var_is_in_env_example(base, overlay):
    text = "".join((ROOT / n).read_text(encoding="utf-8")
                   for n in ("docker-compose.yml", "docker-compose.prod.yml", "docker-compose.micro.yml"))
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", text))
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", example, flags=re.M))
    missing = sorted(used - declared)
    assert not missing, f"add to .env.example: {missing}"

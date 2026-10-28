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


def test_every_compose_var_is_in_env_example(base, overlay):
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8") + (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", text))
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]*)=", example, flags=re.M))
    missing = sorted(used - declared)
    assert not missing, f"add to .env.example: {missing}"

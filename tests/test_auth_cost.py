"""O-2: auth lookup cost - LRU, pub/sub revocation, negative cache, per-IP auth-failure limit, trusted-proxy XFF."""
import asyncio
import os
import random
import time
import uuid

import httpx
import pytest

from app.auth import keys
from app.auth.guard import client_ip, parse_trusted
from tests.conftest import H


def rand_ip():
    return f"198.51.{random.randint(1, 250)}.{random.randint(1, 250)}"


def client_from(e, ip):
    tr = httpx.ASGITransport(app=e.client._transport.app, client=(ip, 5000), raise_app_exceptions=False)
    return httpx.AsyncClient(transport=tr, base_url="http://t")


BODY = {"q": "hello", "session_id": "s1"}


async def test_random_key_flood_costs_one_postgres_lookup_per_unique_key(make_app):
    async with make_app(key_cache_ttl=30, key_neg_ttl=60, auth_fail_per_min=100000) as e1:
        c = client_from(e1, rand_ip())
        bad = f"rag_{uuid.uuid4().hex}"
        codes = [(await c.post("/query", json=BODY, headers=H(bad))).status_code for _ in range(40)]
        assert set(codes) == {401} and e1.svc.keystore.pg_lookups == 1
        for _ in range(5):  # 5 more unique keys => exactly 5 more lookups
            for _ in range(4):
                await c.post("/query", json=BODY, headers=H(f"rag_{uuid.uuid4().hex}"))
        assert e1.svc.keystore.pg_lookups == 1 + 5 * 4  # unique keys: each is looked up once (4 repeats of a NEW key each time here)
        # a second worker process sharing Redis does not hit Postgres for a key the first worker already saw
        async with make_app(key_cache_ttl=30, key_neg_ttl=60, auth_fail_per_min=100000) as e2:
            c2 = client_from(e2, rand_ip())
            assert (await c2.post("/query", json=BODY, headers=H(bad))).status_code == 401
            assert e2.svc.keystore.pg_lookups == 0  # served by the Redis negative cache
        await c.aclose()


async def test_valid_key_hot_path_does_one_lookup(make_app, new_key):
    _, k = new_key()
    async with make_app(key_cache_ttl=30) as e:
        for _ in range(60):
            assert (await e.client.post("/query", json=BODY, headers=H(k))).status_code == 200
        assert e.svc.keystore.pg_lookups == 1


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("REDIS_INTEGRATION_URL"), reason="fakeredis' TCP server does not deliver pub/sub; needs a real Redis")
async def test_revoked_key_is_rejected_within_a_second_via_pubsub(make_app, new_key, pg_url):
    _, k = new_key()
    async with make_app(key_cache_ttl=300) as e:  # TTL far longer than the test: only pub/sub can explain a fast rejection
        assert (await e.client.post("/query", json=BODY, headers=H(k))).status_code == 200  # now cached
        assert (await e.client.post("/query", json=BODY, headers=H(k))).status_code == 200
        keys.revoke_key(pg_url, raw=k, redis_url=e.settings.redis_url)
        t0, code = time.monotonic(), 200
        while time.monotonic() - t0 < 1.0 and code == 200:
            code = (await e.client.post("/query", json=BODY, headers=H(k))).status_code
            await asyncio.sleep(0.02)
        assert code == 401 and time.monotonic() - t0 < 1.0


async def test_without_publish_the_cache_ttl_is_the_only_fallback(make_app, new_key, pg_url):
    _, k = new_key()
    async with make_app(key_cache_ttl=300) as e:
        assert (await e.client.post("/query", json=BODY, headers=H(k))).status_code == 200
        keys.revoke_key(pg_url, raw=k)  # no redis_url => no fan-out
        await asyncio.sleep(0.3)
        assert (await e.client.post("/query", json=BODY, headers=H(k))).status_code == 200  # still cached (control)


async def test_new_key_not_blocked_by_an_earlier_negative_cache_entry(make_app, pg_url):
    raw = None
    async with make_app(key_cache_ttl=30, key_neg_ttl=60, auth_fail_per_min=100000) as e:
        # create the key (publishes eviction), but a negative entry for its hash must have been cleared by that
        tenant = f"tn{uuid.uuid4().hex[:6]}"
        raw = keys.PREFIX + "preknown" + uuid.uuid4().hex
        await e.svc.redis.set(keys.NEG_PREFIX + keys.hash_key(raw), "1", ex=60)  # simulate an earlier failed probe
        import psycopg

        with psycopg.connect(pg_url) as conn:
            conn.execute("INSERT INTO api_keys (key_hash, tenant_id, user_id) VALUES (%s,%s,%s)", (keys.hash_key(raw), tenant, "u"))
        keys.publish_evict(e.settings.redis_url, [keys.hash_key(raw)])
        await asyncio.sleep(0.2)
        assert (await e.client.post("/query", json=BODY, headers=H(raw))).status_code == 200


async def test_auth_failure_ip_limit_returns_429(make_app, new_key):
    _, good = new_key()
    ip, other = rand_ip(), rand_ip()
    async with make_app(key_cache_ttl=30, auth_fail_per_min=5) as e:
        assert (await e.client.post("/query", json=BODY, headers=H(good))).status_code == 200  # warm the LRU for the valid key
        c = client_from(e, ip)
        codes = [(await c.post("/query", json=BODY, headers=H(f"rag_{uuid.uuid4().hex}"))).status_code for _ in range(8)]
        assert codes == [401] * 5 + [429] * 3
        r = await c.post("/query", json=BODY, headers=H(f"rag_{uuid.uuid4().hex}"))
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
        assert (await c.post("/query", json=BODY)).status_code == 429  # missing key counts too
        assert (await c.post("/query", json=BODY, headers=H(good))).status_code == 200  # an already-valid cached key is unaffected
        other_c = client_from(e, other)
        assert (await other_c.post("/query", json=BODY, headers=H("rag_nope"))).status_code == 401  # other IPs unaffected
        await c.aclose()
        await other_c.aclose()


async def test_spoofed_xff_from_untrusted_peer_is_ignored(make_app):
    peer = rand_ip()
    async with make_app(auth_fail_per_min=3) as e:
        c = client_from(e, peer)
        codes = []
        for i in range(6):  # attacker rotates a fake X-Forwarded-For hoping to dodge the per-IP limit
            r = await c.post("/query", json=BODY, headers={**H(f"rag_{uuid.uuid4().hex}"), "X-Forwarded-For": f"203.0.113.{i + 1}"})
            codes.append(r.status_code)
        assert codes == [401, 401, 401, 429, 429, 429]
        await c.aclose()


async def test_xff_honoured_only_from_a_trusted_proxy(make_app):
    proxy, a, b = "10.77.0.9", rand_ip(), rand_ip()
    async with make_app(auth_fail_per_min=3, trusted_proxies=proxy) as e:
        c = client_from(e, proxy)
        for _ in range(3):
            await c.post("/query", json=BODY, headers={**H("rag_x"), "X-Forwarded-For": f"6.6.6.6, {a}"})  # left-most entry is attacker-supplied
        r = await c.post("/query", json=BODY, headers={**H("rag_x"), "X-Forwarded-For": f"1.2.3.4, {a}"})
        assert r.status_code == 429  # counted against the real client `a`, not the spoofable left-most address
        r = await c.post("/query", json=BODY, headers={**H("rag_x"), "X-Forwarded-For": f"6.6.6.6, {b}"})
        assert r.status_code == 401  # a different real client behind the same proxy is not penalised
        await c.aclose()


def test_client_ip_resolution_rules():
    trusted = parse_trusted("10.0.0.0/8, 192.168.1.5")
    assert client_ip("8.8.8.8", "1.1.1.1", trusted) == "8.8.8.8"  # untrusted peer: XFF ignored
    assert client_ip("10.1.2.3", "1.1.1.1, 2.2.2.2", trusted) == "2.2.2.2"  # right-most untrusted hop
    assert client_ip("10.1.2.3", "2.2.2.2, 10.9.9.9", trusted) == "2.2.2.2"  # trusted hops are skipped
    assert client_ip("10.1.2.3", "not-an-ip", trusted) == "10.1.2.3"
    assert client_ip("10.1.2.3", None, trusted) == "10.1.2.3"
    assert client_ip("10.1.2.3", "1.1.1.1", []) == "10.1.2.3"  # no trusted proxies configured


async def test_eviction_listener_drops_the_key_when_a_message_arrives():
    """Offline check of the pub/sub handler itself (the end-to-end test above needs a real Redis)."""
    from app.auth.keys import EVICT_CHANNEL, KeyStore, Principal

    ks = KeyStore(pool=None, cache_ttl_s=300, redis=None)
    h = keys.hash_key("rag_secret")
    ks._remember(h, Principal("id", "t", "u", False, 60, 1000))
    assert ks.peek("rag_secret") is not None

    class FakePubSub:
        def __init__(self):
            self.sent = False

        async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
            if not self.sent:
                self.sent = True
                return {"type": "message", "channel": EVICT_CHANNEL, "data": h.encode()}
            await asyncio.sleep(0.01)
            return None

        async def aclose(self):
            pass

    task = asyncio.create_task(ks._listen(FakePubSub()))
    await asyncio.sleep(0.1)
    task.cancel()
    assert ks.peek("rag_secret") is None

"""Tests for emailfinder.cache — domain fingerprint TTL + sha256 api cache."""
from __future__ import annotations

import time

import pytest

from emailfinder.cache import Cache
from emailfinder.models import DomainFingerprint, Provider


@pytest.fixture
def cache(tmp_path):
    c = Cache(tmp_path / "silo" / "cache.sqlite")
    yield c
    c.close()


def _fp(domain="acme.com", **kw):
    base = dict(
        domain=domain,
        provider=Provider.MICROSOFT365,
        mx=["mx1.acme.com", "mx2.acme.com"],
        is_catch_all=None,
        learned_template="first.last",
        learned_separator=".",
    )
    base.update(kw)
    return DomainFingerprint(**base)


def test_init_creates_file_and_tables(tmp_path):
    path = tmp_path / "nested" / "silo" / "cache.sqlite"
    c = Cache(path)
    assert path.exists()
    # Both tables usable.
    assert c.get_domain("missing.com") is None
    assert c.get_api(Cache.api_key("mv", "x@y.com"), 30) is None
    c.close()


def test_put_then_get_domain_roundtrip(cache):
    cache.put_domain(_fp())
    got = cache.get_domain("acme.com")
    assert got is not None
    assert got.domain == "acme.com"
    assert got.provider is Provider.MICROSOFT365     # enum round-trips via .value
    assert got.mx == ["mx1.acme.com", "mx2.acme.com"]  # list preserved
    assert got.learned_template == "first.last"
    assert got.learned_separator == "."
    assert got.last_probed_at > 0


def test_get_domain_case_insensitive(cache):
    cache.put_domain(_fp(domain="Acme.COM"))
    assert cache.get_domain("acme.com") is not None
    assert cache.get_domain("ACME.com") is not None


def test_catch_all_tristate_roundtrip(cache):
    for value in (None, True, False):
        cache.put_domain(_fp(domain=f"d-{value}.com", is_catch_all=value))
        got = cache.get_domain(f"d-{value}.com")
        assert got.is_catch_all is value  # None distinct from False


def test_get_domain_miss_returns_none(cache):
    assert cache.get_domain("nope.com") is None


def test_get_domain_ttl_expiry(cache):
    cache.put_domain(_fp())
    # Force the stored timestamp far into the past.
    cache._conn.execute(
        "UPDATE domain_fp SET last_probed_at = ? WHERE domain = ?",
        (time.time() - 20 * 86400, "acme.com"),
    )
    cache._conn.commit()
    assert cache.get_domain("acme.com", ttl_days=14) is None   # expired -> miss
    assert cache.get_domain("acme.com", ttl_days=90) is not None  # still fresh


def test_put_domain_upsert_refreshes(cache):
    cache.put_domain(_fp(learned_template="first.last"))
    first = cache.get_domain("acme.com").last_probed_at
    time.sleep(0.01)
    cache.put_domain(_fp(learned_template="flast", learned_separator=""))
    got = cache.get_domain("acme.com")
    assert got.learned_template == "flast"
    assert got.learned_separator == ""
    assert got.last_probed_at >= first
    # Still a single row (upsert, not insert).
    n = cache._conn.execute("SELECT COUNT(*) FROM domain_fp").fetchone()[0]
    assert n == 1


def test_api_key_stable_and_distinct():
    k1 = Cache.api_key("millionverifier", "john@acme.com")
    k2 = Cache.api_key("millionverifier", "john@acme.com")
    k3 = Cache.api_key("hunter", "john@acme.com")
    k4 = Cache.api_key("millionverifier", "jane@acme.com")
    assert k1 == k2                 # deterministic
    assert len(k1) == 64            # sha256 hex
    assert k1 != k3 != k4           # provider + input both participate
    assert k1 != k4


def test_api_cache_roundtrip_prevents_double_charge(cache):
    key = Cache.api_key("millionverifier", "john@acme.com")
    assert cache.get_api(key, 30) is None      # first look: miss -> would call
    cache.put_api(key, {"result": "ok", "credits": 1}, 30)
    hit = cache.get_api(key, 30)               # second look: hit -> no re-call
    assert hit == {"result": "ok", "credits": 1}


def test_api_cache_ttl_expiry(cache):
    key = Cache.api_key("mv", "x@y.com")
    cache.put_api(key, {"result": "ok"}, 30)
    cache._conn.execute(
        "UPDATE api_cache SET stored_at = ? WHERE key = ?",
        (time.time() - 40 * 86400, key),
    )
    cache._conn.commit()
    assert cache.get_api(key, 30) is None       # expired
    assert cache.get_api(key, 90) == {"result": "ok"}  # within longer TTL


def test_api_cache_upsert(cache):
    key = Cache.api_key("mv", "x@y.com")
    cache.put_api(key, {"v": 1}, 30)
    cache.put_api(key, {"v": 2}, 30)
    assert cache.get_api(key, 30) == {"v": 2}
    n = cache._conn.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0]
    assert n == 1


def test_provider_string_accepted_on_put(cache):
    # put_domain tolerates a bare string provider and it reads back as enum.
    fp = _fp(domain="str.com")
    fp.provider = "google_workspace"  # type: ignore[assignment]
    cache.put_domain(fp)
    got = cache.get_domain("str.com")
    assert got.provider is Provider.GOOGLE_WORKSPACE


def test_purge_expired(cache):
    cache.put_domain(_fp(domain="fresh.com"))
    cache.put_domain(_fp(domain="stale.com"))
    cache._conn.execute(
        "UPDATE domain_fp SET last_probed_at = ? WHERE domain = ?",
        (time.time() - 100 * 86400, "stale.com"),
    )
    cache.put_api(Cache.api_key("mv", "old"), {"v": 1}, 30)
    cache._conn.execute(
        "UPDATE api_cache SET stored_at = ? ",
        (time.time() - 100 * 86400,),
    )
    cache._conn.commit()
    purged = cache.purge_expired(domain_ttl_days=14, api_ttl_days=30)
    assert purged == 2
    assert cache.get_domain("fresh.com") is not None
    assert cache.get_domain("stale.com") is None

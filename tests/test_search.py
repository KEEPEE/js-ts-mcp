"""Offline tests for js_ts_mcp.search (parser, ranking, cache logic).

The parsers run against the real downloaded fixtures:
- ``tests/fixtures/mdn_sitemap_en_us.xml.gz`` <- developer.mozilla.org en-US sitemap
- ``tests/fixtures/ts_intro.html``            <- TypeScript handbook intro page

The ranking and get_index tests use a throwaway DocCache pre-populated from
those fixtures plus monkeypatched fetch layers, so nothing here touches the
network.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import time
from pathlib import Path

import pytest

from js_ts_mcp import search as search_mod
from js_ts_mcp.cache import DocCache
from js_ts_mcp.search import (
    INDEX_KEY,
    MDN_SITEMAP_URL,
    TS_HANDBOOK_SOURCE_URL,
    build_index,
    get_index,
    parse_mdn_sitemap_xml,
    parse_ts_handbook_html,
    search_docs,
)

FIXTURES = Path(__file__).parent / "fixtures"
MDN_FIXTURE = FIXTURES / "mdn_sitemap_en_us.xml.gz"
TS_FIXTURE = FIXTURES / "ts_intro.html"


def _mdn_fixture_xml() -> str:
    """The sitemap fixture is stored gzipped (as served); decompress it."""
    return gzip.decompress(MDN_FIXTURE.read_bytes()).decode("utf-8")


def _ts_fixture_html() -> str:
    return TS_FIXTURE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# MDN sitemap parser tests on the real fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mdn_entries() -> list[dict]:
    return parse_mdn_sitemap_xml(_mdn_fixture_xml())


def test_parse_mdn_sitemap_entry_count(mdn_entries):
    assert len(mdn_entries) > 10000


def test_parse_mdn_sitemap_array_entry(mdn_entries):
    by_path = {e["path"]: e for e in mdn_entries}
    entry = by_path.get("Web/JavaScript/Reference/Global_Objects/Array")
    assert entry is not None, "Array missing from sitemap index"
    assert entry["name"] == "Array"
    assert entry["source"] == "mdn"
    assert entry["url"] == (
        "https://developer.mozilla.org/en-US/docs/"
        "Web/JavaScript/Reference/Global_Objects/Array"
    )


def test_parse_mdn_sitemap_only_docs_urls(mdn_entries):
    for entry in mdn_entries:
        assert entry["url"].startswith(
            "https://developer.mozilla.org/en-US/docs/"
        ), entry
        assert entry["path"], entry
        assert entry["name"], entry


def test_parse_mdn_sitemap_entry_shape_and_unique(mdn_entries):
    paths = set()
    for entry in mdn_entries:
        assert set(entry) == {"source", "name", "path", "url"}, entry
        assert entry["source"] == "mdn"
        assert entry["path"] not in paths, f"duplicate path {entry['path']}"
        paths.add(entry["path"])


def test_parse_mdn_sitemap_rejects_garbage():
    with pytest.raises(ValueError):
        parse_mdn_sitemap_xml("<html><body>no sitemap here</body></html>")
    # valid XML but not an MDN docs sitemap
    with pytest.raises(ValueError):
        parse_mdn_sitemap_xml(
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/'
            'schemas/sitemap/0.9"><url><loc>https://example.com/x</loc></url>'
            "</urlset>"
        )


# ---------------------------------------------------------------------------
# TypeScript handbook parser tests on the real fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def ts_entries() -> list[dict]:
    return parse_ts_handbook_html(_ts_fixture_html())


def test_parse_ts_handbook_entry_count(ts_entries):
    assert 50 <= len(ts_entries) <= 200


def test_parse_ts_handbook_basic_types(ts_entries):
    matches = [e for e in ts_entries if e["path"].endswith("basic-types")]
    assert matches, "no entry with a path ending in basic-types"
    entry = matches[0]
    assert entry["name"] == "basic types"
    assert entry["source"] == "typescript"


def test_parse_ts_handbook_urls(ts_entries):
    for entry in ts_entries:
        assert entry["url"].startswith(
            "https://www.typescriptlang.org/docs/handbook/"
        ), entry
        assert entry["url"].endswith(".html"), entry


def test_parse_ts_handbook_entry_shape_and_unique(ts_entries):
    paths = set()
    for entry in ts_entries:
        assert set(entry) == {"source", "name", "path", "url"}, entry
        assert entry["source"] == "typescript"
        assert not entry["path"].endswith(".html"), entry
        assert not entry["path"].startswith("/"), entry
        assert entry["path"] not in paths, f"duplicate path {entry['path']}"
        paths.add(entry["path"])


def test_parse_ts_handbook_rejects_garbage():
    with pytest.raises(ValueError):
        parse_ts_handbook_html("<html><body>no handbook links</body></html>")


# ---------------------------------------------------------------------------
# Ranking tests on the real fixtures (offline, via a pre-populated cache)
# ---------------------------------------------------------------------------

@pytest.fixture()
def fixture_cache(tmp_path, monkeypatch):
    """Point DocCache at a throwaway dir and pre-populate it from the fixtures."""
    monkeypatch.setenv("JS_TS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    cache = DocCache()
    mdn = parse_mdn_sitemap_xml(_mdn_fixture_xml())
    ts = parse_ts_handbook_html(_ts_fixture_html())
    docs = mdn + ts
    index = {
        "docs": docs,
        "count": len(docs),
        "mdn_count": len(mdn),
        "ts_count": len(ts),
        "built_at": "2026-01-01T00:00:00+00:00",
    }
    cache.set(INDEX_KEY, json.dumps(index), 604800)
    return cache


def test_search_array_exact_first(fixture_cache):
    results = search_docs("array")
    assert results, "expected hits for 'array'"
    assert results[0]["name"] == "Array"
    assert results[0]["source"] == "mdn"
    assert all(set(r) == {"source", "name", "path", "score"} for r in results)


def test_search_generics_top_is_typescript(fixture_cache):
    results = search_docs("generics")
    assert results, "expected hits for 'generics'"
    assert results[0]["source"] == "typescript"


def test_search_nonexistent_returns_empty(fixture_cache):
    assert search_docs("zzz_nonexistent_token") == []


def test_search_limit_respected(fixture_cache):
    default = search_docs("array")
    limited = search_docs("array", limit=3)
    assert len(limited) <= 3
    assert [r["name"] for r in limited] == [r["name"] for r in default[:3]]
    assert search_docs("array", limit=0) == []


def test_search_is_case_insensitive(fixture_cache):
    results = search_docs("ARRAY")
    assert results and results[0]["name"] == "Array"


def test_search_multi_token_all_must_match(fixture_cache):
    """Both tokens must match: 'array' exact in the name, 'javascript' in its path."""
    results = search_docs("array javascript")
    assert results, "expected hits for 'array javascript'"
    assert results[0]["name"] == "Array"
    # exact name (10) + substring in path (3)
    assert results[0]["score"] == 13.0


def test_search_scores_descending(fixture_cache):
    results = search_docs("type")
    scores = [r["score"] for r in results]
    assert all(a >= b for a, b in zip(scores, scores[1:]))


# ---------------------------------------------------------------------------
# get_index / build_index cache behaviour (monkeypatched, tmp cache dir)
# ---------------------------------------------------------------------------

def _canned() -> dict:
    return {
        "built_at": "2026-01-01T00:00:00+00:00",
        "count": 1,
        "mdn_count": 1,
        "ts_count": 0,
        "docs": [
            {
                "source": "mdn",
                "name": "Array",
                "path": "Web/JavaScript/Reference/Global_Objects/Array",
                "url": (
                    "https://developer.mozilla.org/en-US/docs/"
                    "Web/JavaScript/Reference/Global_Objects/Array"
                ),
            },
        ],
    }


def _expire_cache_row(db_path: Path) -> None:
    """Rewrite the kv row so its expiry lies in the past (simulate TTL lapse)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE kv SET expires_at = ? WHERE key = ?",
            (time.time() - 10, INDEX_KEY),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def isolated_cache(tmp_path, monkeypatch):
    """Point DocCache at a throwaway directory and stub out build_index."""
    monkeypatch.setenv("JS_TS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    calls = {"count": 0}

    def fake_build():
        calls["count"] += 1
        return _canned()

    monkeypatch.setattr(search_mod, "build_index", fake_build)
    return calls


def test_get_index_fresh_write_then_cache_hit(isolated_cache):
    first = get_index()
    assert first == _canned()
    assert first.get("stale") is not True
    assert isolated_cache["count"] == 1

    # Second call must be served from the cache: no rebuild, same content.
    second = get_index()
    assert second == _canned()
    assert isolated_cache["count"] == 1


def test_get_index_stale_fallback_on_network_failure(tmp_path, monkeypatch):
    """When the network fails and the cached copy is expired, serve it stale."""
    monkeypatch.setenv("JS_TS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    state = {"fail": False}

    def fake_mdn_sitemap():
        if state["fail"]:
            raise RuntimeError("network down")
        return _mdn_fixture_xml()  # real parse of the fixture, no network

    def fake_ts_html():
        if state["fail"]:
            raise RuntimeError("network down")
        return _ts_fixture_html()

    monkeypatch.setattr(search_mod, "_fetch_mdn_sitemap", fake_mdn_sitemap)
    monkeypatch.setattr(search_mod, "_fetch_ts_handbook_html", fake_ts_html)

    # First build succeeds and persists a fresh index.
    fresh = get_index()
    assert fresh.get("stale") is not True
    assert fresh["count"] == len(fresh["docs"])
    assert fresh["mdn_count"] > 10000
    assert 50 <= fresh["ts_count"] <= 200

    # Expire the cached row, then kill the network: the stale copy must come back.
    _expire_cache_row(Path(tmp_path / "cache" / "cache.db"))
    state["fail"] = True
    stale = get_index()
    assert stale.get("stale") is True
    assert stale["count"] == fresh["count"]
    assert stale["docs"] == fresh["docs"]
    assert stale["built_at"] == fresh["built_at"]


def test_build_index_stale_fallback_on_network_failure(tmp_path, monkeypatch):
    """build_index itself falls back: fresh cache first, then stale via peek."""
    monkeypatch.setenv("JS_TS_MCP_CACHE_DIR", str(tmp_path / "cache"))

    def boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(search_mod, "_fetch_mdn_sitemap", boom)
    monkeypatch.setattr(search_mod, "_fetch_ts_handbook_html", boom)

    # No cache at all -> build_index raises (nothing to fall back on).
    with pytest.raises(RuntimeError, match="network down"):
        build_index()

    # Seed an expired cached copy: the stale fallback must return it.
    cache = DocCache()
    cache.set(INDEX_KEY, json.dumps(_canned()), 604800)
    _expire_cache_row(Path(tmp_path / "cache" / "cache.db"))
    result = build_index()
    assert result.get("stale") is True
    assert result["docs"] == _canned()["docs"]


def test_get_index_build_fails_with_no_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("JS_TS_MCP_CACHE_DIR", str(tmp_path / "cache"))

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(search_mod, "_fetch_mdn_sitemap", boom)
    monkeypatch.setattr(search_mod, "_fetch_ts_handbook_html", boom)
    result = get_index()  # must not raise
    assert result["docs"] == []
    assert result["count"] == 0
    assert result["mdn_count"] == 0
    assert result["ts_count"] == 0
    assert result["built_at"] is None
    assert "boom" in result["error"]


def test_get_index_accepts_explicit_cache(tmp_path, monkeypatch):
    """An explicitly passed DocCache is used for both read and write."""
    # build_index() opens its own default cache too; keep that in tmp as well.
    monkeypatch.setenv("JS_TS_MCP_CACHE_DIR", str(tmp_path / "default"))
    monkeypatch.setattr(search_mod, "_fetch_mdn_sitemap", _mdn_fixture_xml)
    monkeypatch.setattr(search_mod, "_fetch_ts_handbook_html", _ts_fixture_html)
    cache = DocCache(str(tmp_path / "explicit" / "cache.db"))

    first = get_index(cache=cache)
    assert first["count"] > 10000
    # The explicit cache must now hold the index (fresh hit, no rebuild).
    cached_raw = cache.get(INDEX_KEY)
    assert json.loads(cached_raw)["docs"] == first["docs"]

    calls = {"n": 0}

    def counting_fetch():
        calls["n"] += 1
        return _mdn_fixture_xml()

    monkeypatch.setattr(search_mod, "_fetch_mdn_sitemap", counting_fetch)
    second = get_index(cache=cache)
    assert second == first
    assert calls["n"] == 0  # served from the explicit cache


def test_search_docs_uses_get_index(monkeypatch):
    """search_docs must rank over whatever get_index returns (no network)."""
    monkeypatch.setattr(search_mod, "get_index", lambda: _canned())
    results = search_docs("array")
    assert [r["name"] for r in results] == ["Array"]
    assert results[0]["score"] == 10.0


def test_constants():
    assert INDEX_KEY == "search-index"
    assert MDN_SITEMAP_URL == (
        "https://developer.mozilla.org/sitemaps/en-us/sitemap.xml.gz"
    )
    assert TS_HANDBOOK_SOURCE_URL == (
        "https://www.typescriptlang.org/docs/handbook/intro.html"
    )

"""Offline tests for the MCP tools in js_ts_mcp.server.

All fetchers, the search index and endpoint probes are monkeypatched — nothing
here touches the network. The cache is exercised for real against a temporary
SQLite file via JS_TS_MCP_CACHE_DIR=tmp_path.
"""

from __future__ import annotations

import re

import pytest

from js_ts_mcp import server as server_mod


MDN_BASE = "https://developer.mozilla.org/en-US/docs"
TS_BASE = "https://www.typescriptlang.org/docs/handbook"


def _mdn(path: str, name: str) -> dict:
    return {"source": "mdn", "name": name, "path": path, "url": f"{MDN_BASE}/{path}"}


def _ts(path: str, name: str) -> dict:
    return {"source": "typescript", "name": name, "path": path, "url": f"{TS_BASE}/{path}.html"}


FAKE_INDEX = {
    "built_at": "2026-01-01T00:00:00+00:00",
    "count": 12,
    "mdn_count": 7,
    "ts_count": 5,
    "docs": [
        _mdn("Web/JavaScript/Reference/Global_Objects/Array", "Array"),
        _mdn("Web/JavaScript/Reference/Global_Objects/Promise", "Promise"),
        _mdn("Web/JavaScript/Reference/Global_Objects/Promise/Promise", "Promise"),
        _mdn("Web/API/PromiseRejectionEvent/promise", "promise"),
        _mdn("Web/API/fetch", "fetch"),
        _mdn("Web/JavaScript/Reference/Global_Objects/JSON", "JSON"),
        _mdn("Web/API/Worker", "Worker"),
        _ts("2/generics", "Generics"),
        _ts("intro", "Introduction"),
        _ts("2/worker", "Worker"),
        _ts("2/mixins", "Mixins"),
        _ts("3/mixins", "Mixins"),
    ],
}

ARRAY_MD = (
    "# Array\n\n"
    "The `Array` global object is used for creating heterogeneous, dense,\n"
    "array-like lists.\n\n"
    "## Methods\n\n"
    "- `push(...items)` — Adds one or more elements to the end of an array.\n"
    "- `pop()` — Removes the last element and returns it.\n\n"
    "## Examples\n\n"
    "```js\nconst a = [1];\na.push(2);\n```\n"
)

INTRO_MD = (
    "# TypeScript\n\n"
    "TypeScript is a strongly typed language that builds on JavaScript, giving\n"
    "you better tooling at any scale.\n\n"
    "## Why TypeScript?\n\n"
    "JavaScript's dynamic typing is flexible but error-prone at scale.\n"
)


@pytest.fixture
def fake_cache_dir(tmp_path, monkeypatch):
    """Point DocCache at a fresh temp dir so tests never share state."""
    monkeypatch.setenv("JS_TS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path


def _fake_search_over(docs, query, limit=8):
    """Emulate js_ts_mcp.search.search_docs over ``docs`` (offline).

    Same per-token tiers as the real implementation: exact name > startswith >
    substring in name > substring in path; every token must match.
    """
    tokens = str(query or "").strip().lower().split()
    if not tokens:
        return []
    results: list[dict] = []
    for entry in docs:
        name_lower = entry["name"].lower()
        path_lower = entry["path"].lower()
        score = 0.0
        matched = True
        for token in tokens:
            if token == name_lower:
                tier = 10.0
            elif name_lower.startswith(token):
                tier = 8.0
            elif token in name_lower:
                tier = 6.0
            elif token in path_lower:
                tier = 3.0
            else:
                matched = False
                break
            score += tier
        if matched and score > 0.0:
            results.append(
                {
                    "source": entry["source"],
                    "name": entry["name"],
                    "path": entry["path"],
                    "score": round(score, 3),
                }
            )
    results.sort(key=lambda r: (-r["score"], r["name"].lower()))
    return results[: max(0, int(limit))]


def _patch_index(monkeypatch, index: dict) -> None:
    docs = list(index.get("docs") or [])

    def fake_get_index(cache=None) -> dict:
        return dict(index)

    def fake_search(query, limit=8):
        return _fake_search_over(docs, query, limit)

    monkeypatch.setattr(server_mod, "get_index", fake_get_index)
    monkeypatch.setattr(server_mod, "search_docs", fake_search)


def _patch_fetchers(monkeypatch, mdn=None, ts=None, npm=None):
    """Install recording fakes for the three fetchers; returns call log."""
    calls = {"mdn": [], "ts": [], "npm": []}

    def fake_mdn(slug):
        calls["mdn"].append({"slug": slug})
        if mdn is not None:
            return mdn(slug)
        return {"ok": False, "error": f"page not found on MDN: {slug}"}

    def fake_ts(page):
        calls["ts"].append({"page": page})
        if ts is not None:
            return ts(page)
        return {"ok": False, "error": f"page not found on typescriptlang.org: {page}"}

    def fake_npm(name, version=None):
        calls["npm"].append({"name": name, "version": version})
        if npm is not None:
            return npm(name, version)
        return {
            "ok": False,
            "error": "package not found on npm",
            "suggestion": "check the package name",
        }

    monkeypatch.setattr(server_mod, "fetch_mdn_doc", fake_mdn)
    monkeypatch.setattr(server_mod, "fetch_ts_page", fake_ts)
    monkeypatch.setattr(server_mod, "fetch_npm_package", fake_npm)
    return calls


def _ok_mdn(path: str, title: str, markdown: str) -> dict:
    return {
        "ok": True,
        "url": f"{MDN_BASE}/{path}",
        "title": title,
        "slug": path,
        "markdown": markdown,
    }


def _ok_ts(page: str, title: str, markdown: str) -> dict:
    return {
        "ok": True,
        "url": f"{TS_BASE}/{page}.html",
        "title": title,
        "page": page,
        "markdown": markdown,
    }


# ---------------------------------------------------------------------------
# js_docs — identifier resolution (mdn:/ts: prefixes)
# ---------------------------------------------------------------------------

def test_mdn_prefix_fetched_directly(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        mdn=lambda s: _ok_mdn(s, "Array - JavaScript | MDN", ARRAY_MD)
        if s == "Web/JavaScript/Reference/Global_Objects/Array"
        else {"ok": False, "error": f"page not found on MDN: {s}"},
    )
    result = server_mod.js_docs("mdn:Web/JavaScript/Reference/Global_Objects/Array")
    assert calls["mdn"] == [{"slug": "Web/JavaScript/Reference/Global_Objects/Array"}]
    assert calls["ts"] == []
    assert result["ok"] is True
    assert result["identifier"] == "mdn:Web/JavaScript/Reference/Global_Objects/Array"
    assert result["source"] == "mdn"
    assert result["url"].endswith("/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array")
    assert "Array" in result["title"]
    assert "push(...items)" in result["markdown"]
    assert result["cached"] is False and result["truncated"] is False


def test_ts_prefix_fetched_directly(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        ts=lambda p: _ok_ts(p, "TypeScript", INTRO_MD)
        if p == "intro"
        else {"ok": False, "error": f"page not found on typescriptlang.org: {p}"},
    )
    result = server_mod.js_docs("ts:intro")
    assert calls["ts"] == [{"page": "intro"}]
    assert calls["mdn"] == []
    assert result["ok"] is True
    assert result["source"] == "typescript"
    assert result["url"].endswith("/docs/handbook/intro.html")
    assert "TypeScript" in result["title"]


def test_ts_prefix_subpage_path(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        ts=lambda p: _ok_ts(p, "Generics", "# Generics\n\ntext\n"),
    )
    result = server_mod.js_docs("ts:2/generics")
    assert calls["ts"] == [{"page": "2/generics"}]
    assert result["ok"] is True
    assert result["url"].endswith("/docs/handbook/2/generics.html")


# ---------------------------------------------------------------------------
# js_docs — plain-name resolution (index + tie-breaking)
# ---------------------------------------------------------------------------

def test_plain_name_resolved_via_index(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    calls = _patch_fetchers(
        monkeypatch,
        mdn=lambda s: _ok_mdn(s, "Array - JavaScript | MDN", ARRAY_MD),
    )
    result = server_mod.js_docs("Array")
    assert calls["mdn"] == [{"slug": "Web/JavaScript/Reference/Global_Objects/Array"}]
    assert result["ok"] is True
    assert result["identifier"] == "Array"
    assert result["source"] == "mdn"
    assert result["url"].endswith("/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array")


def test_plain_name_tie_break_reference_beats_constructor_subpage(fake_cache_dir, monkeypatch):
    # "Promise" has three exact-name matches: two in Web/JavaScript/Reference
    # (the class page + its constructor subpage) and one under Web/API. The
    # winning tier is Web/JavaScript/Reference, where the parent path beats
    # its subpage.
    _patch_index(monkeypatch, FAKE_INDEX)
    calls = _patch_fetchers(
        monkeypatch,
        mdn=lambda s: _ok_mdn(s, "Promise - JavaScript | MDN", "# Promise\n\ntext\n"),
    )
    result = server_mod.js_docs("Promise")
    assert calls["mdn"] == [{"slug": "Web/JavaScript/Reference/Global_Objects/Promise"}]
    assert result["ok"] is True
    assert result["source"] == "mdn"
    assert result["url"].endswith("/en-US/docs/Web/JavaScript/Reference/Global_Objects/Promise")


def test_plain_name_tie_break_mdn_beats_typescript(fake_cache_dir, monkeypatch):
    # "Worker": mdn Web/API (tier 1) vs typescript handbook (tier 3) → mdn wins.
    _patch_index(monkeypatch, FAKE_INDEX)
    calls = _patch_fetchers(
        monkeypatch,
        mdn=lambda s: _ok_mdn(s, "Worker - Web APIs | MDN", "# Worker\n\ntext\n"),
        ts=lambda p: _ok_ts(p, "Worker", "# Worker\n\ntext\n"),
    )
    result = server_mod.js_docs("Worker")
    assert calls["mdn"] == [{"slug": "Web/API/Worker"}]
    assert calls["ts"] == []
    assert result["ok"] is True
    assert result["source"] == "mdn"


def test_plain_name_ambiguous_returns_error(fake_cache_dir, monkeypatch):
    # "Mixins": two unrelated typescript handbook pages tie at the top score
    # in the same tier → genuinely ambiguous.
    _patch_index(monkeypatch, FAKE_INDEX)
    calls = _patch_fetchers(monkeypatch)
    result = server_mod.js_docs("Mixins")
    assert result["ok"] is False
    assert "ambiguous" in result["error"]
    assert "typescript:2/mixins" in result["error"]
    assert "typescript:3/mixins" in result["error"]
    assert "js_search" in result["suggestion"]
    assert "mdn:/ts:" in result["suggestion"]
    assert calls["mdn"] == [] and calls["ts"] == []  # ambiguous → no fetch attempt


def test_plain_name_no_match_returns_error(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)  # no SuchPage entry
    calls = _patch_fetchers(monkeypatch)
    result = server_mod.js_docs("TotallyBogusXYZ")
    assert result["ok"] is False
    assert "no MDN/TypeScript page matching 'TotallyBogusXYZ'" in result["error"]
    assert "js_search" in result["suggestion"]
    assert calls["mdn"] == [] and calls["ts"] == []  # no index match → no fetch


def test_plain_name_with_broken_index_mentions_unavailable(fake_cache_dir, monkeypatch):
    _patch_index(
        monkeypatch,
        {"docs": [], "count": 0, "mdn_count": 0, "ts_count": 0,
         "built_at": None, "error": "index build failed: boom"},
    )
    calls = _patch_fetchers(monkeypatch)
    result = server_mod.js_docs("Array")
    assert result["ok"] is False
    assert "unavailable" in result["error"]
    assert "js_search" in result["suggestion"]
    assert calls["mdn"] == [] and calls["ts"] == []


def test_empty_identifier_returns_error(fake_cache_dir, monkeypatch):
    _patch_fetchers(monkeypatch)
    result = server_mod.js_docs("   ")
    assert result["ok"] is False
    assert "error" in result and "suggestion" in result


def test_mdn_fetch_failure_returns_error_dict_and_is_not_cached(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(monkeypatch)  # every mdn fetch fails
    first = server_mod.js_docs("mdn:Web/JavaScript/Reference/Global_Objects/Array")
    assert first["ok"] is False
    assert "error" in first and "suggestion" in first
    second = server_mod.js_docs("mdn:Web/JavaScript/Reference/Global_Objects/Array")
    assert second["ok"] is False  # failures are never cached → fetcher called again
    assert len(calls["mdn"]) == 2


# ---------------------------------------------------------------------------
# js_docs — topic filter + truncation
# ---------------------------------------------------------------------------

def test_topic_filter_keeps_only_matching_section(fake_cache_dir, monkeypatch):
    _patch_fetchers(
        monkeypatch,
        mdn=lambda s: _ok_mdn(s, "Array - JavaScript | MDN", ARRAY_MD),
    )
    result = server_mod.js_docs("mdn:Web/JavaScript/Reference/Global_Objects/Array", topic="methods")
    assert result["markdown"].startswith("# Array")
    assert "The `Array` global object" in result["markdown"]  # description kept
    assert "## Methods" in result["markdown"]
    assert "push(...items)" in result["markdown"]
    assert "## Examples" not in result["markdown"]
    assert "note" not in result


def test_topic_without_match_returns_full_content_with_note(fake_cache_dir, monkeypatch):
    _patch_fetchers(
        monkeypatch,
        mdn=lambda s: _ok_mdn(s, "Array - JavaScript | MDN", ARRAY_MD),
    )
    result = server_mod.js_docs("mdn:Web/JavaScript/Reference/Global_Objects/Array", topic="classes")
    assert "## Methods" in result["markdown"] and "## Examples" in result["markdown"]
    assert "no section matching 'classes'" in result["note"]
    assert "Methods" in result["note"] and "Examples" in result["note"]


def test_truncation_cuts_at_line_boundary_with_note(fake_cache_dir, monkeypatch):
    long_md = "# Big Page\n\n" + "\n".join(f"line {i} " + "x" * 40 for i in range(600))
    _patch_fetchers(
        monkeypatch,
        mdn=lambda s: _ok_mdn(s, "Big Page", long_md),
    )
    result = server_mod.js_docs("mdn:Web/JavaScript/Reference/Global_Objects/Array", max_tokens=100)
    assert result["truncated"] is True
    last_line = result["markdown"].splitlines()[-1]
    assert last_line.startswith("[truncated: showing ~")
    assert last_line.endswith("estimated tokens]")
    match = re.search(r"showing ~(\d+) of ~(\d+) estimated tokens\]", last_line)
    assert match is not None
    shown, total = int(match.group(1)), int(match.group(2))
    assert shown <= 100 and total > 100
    # cut at a line boundary: no partial "line N xxx..." tail before the note
    body_lines = result["markdown"].splitlines()[:-1]
    assert all(l.startswith("line ") or l.startswith("#") for l in body_lines if l)


def test_no_truncation_when_within_budget(fake_cache_dir, monkeypatch):
    _patch_fetchers(
        monkeypatch,
        mdn=lambda s: _ok_mdn(s, "Array - JavaScript | MDN", ARRAY_MD),
    )
    result = server_mod.js_docs("mdn:Web/JavaScript/Reference/Global_Objects/Array", max_tokens=8000)
    assert result["truncated"] is False
    assert "[truncated:" not in result["markdown"]


# ---------------------------------------------------------------------------
# js_docs — caching (real DocCache against a temp dir)
# ---------------------------------------------------------------------------

def test_mdn_doc_cached_on_second_call(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    calls = _patch_fetchers(
        monkeypatch,
        mdn=lambda s: _ok_mdn(s, "Array - JavaScript | MDN", ARRAY_MD),
    )
    first = server_mod.js_docs("mdn:Web/JavaScript/Reference/Global_Objects/Array")
    second = server_mod.js_docs("mdn:Web/JavaScript/Reference/Global_Objects/Array")
    assert first["cached"] is False and second["cached"] is True
    assert len(calls["mdn"]) == 1
    assert second["markdown"] == first["markdown"]
    assert second["url"] == first["url"]


def test_index_resolved_doc_cached_under_resolved_path(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    calls = _patch_fetchers(
        monkeypatch,
        mdn=lambda s: _ok_mdn(s, "Promise - JavaScript | MDN", "# Promise\n\ntext\n"),
    )
    by_name = server_mod.js_docs("Promise")
    assert by_name["cached"] is False
    # an explicit call with the resolved path hits the same cache entry
    by_path = server_mod.js_docs("mdn:Web/JavaScript/Reference/Global_Objects/Promise")
    assert by_path["cached"] is True
    assert len(calls["mdn"]) == 1


def test_ts_doc_cached_on_second_call(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        ts=lambda p: _ok_ts(p, "TypeScript", INTRO_MD),
    )
    first = server_mod.js_docs("ts:intro")
    second = server_mod.js_docs("ts:intro")
    assert first["cached"] is False and second["cached"] is True
    assert len(calls["ts"]) == 1


# ---------------------------------------------------------------------------
# js_search
# ---------------------------------------------------------------------------

def test_js_search_shape(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    result = server_mod.js_search("generics", limit=3)
    assert result["ok"] is True
    assert result["query"] == "generics"
    assert result["index_count"] == len(FAKE_INDEX["docs"])
    assert result["stale"] is False
    top = result["results"][0]
    assert top["source"] == "typescript"
    assert top["path"] == "2/generics"
    assert set(top) == {"source", "name", "path", "score"}


def test_js_search_stale_index_still_ok(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, dict(FAKE_INDEX, stale=True))
    result = server_mod.js_search("generics")
    assert result["ok"] is True
    assert result["stale"] is True


def test_js_search_reports_index_error(fake_cache_dir, monkeypatch):
    _patch_index(
        monkeypatch,
        {"docs": [], "count": 0, "mdn_count": 0, "ts_count": 0,
         "built_at": None, "error": "boom"},
    )
    result = server_mod.js_search("generics")
    assert result["ok"] is False
    assert "search index unavailable" in result["error"]
    assert "retry" in result["suggestion"].lower()


# ---------------------------------------------------------------------------
# npm_package
# ---------------------------------------------------------------------------

def _ok_npm(name: str, version: str) -> dict:
    return {
        "ok": True,
        "name": name,
        "version": version,
        "description": "A test package",
        "license": "MIT",
        "homepage": f"https://example.com/{name}",
        "repository_url": f"https://github.com/example/{name}.git",
        "keywords": ["test"],
        "engines": {"node": ">=18"},
        "dependencies": {"debug": "^4.0.0"},
        "readme_markdown": f"# {name}\n\nReadme body.\n",
        "url": f"https://www.npmjs.com/package/{name}",
    }


def test_npm_package_success_and_cache(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        npm=lambda n, v=None: _ok_npm(n, "4.21.0" if v is None else v),
    )
    first = server_mod.npm_package("express")
    assert first["ok"] is True
    assert first["cached"] is False
    assert first["name"] == "express"
    assert first["version"] == "4.21.0"
    assert first["readme_markdown"].startswith("# express")

    second = server_mod.npm_package("express")
    assert second["ok"] is True
    assert second["cached"] is True
    assert second["version"] == "4.21.0"
    assert len(calls["npm"]) == 1  # fetcher hit only once


def test_npm_package_version_pin_cached_separately(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(
        monkeypatch,
        npm=lambda n, v=None: _ok_npm(n, v or "4.21.0"),
    )
    pinned = server_mod.npm_package("express", version="4.20.0")
    assert pinned["version"] == "4.20.0" and pinned["cached"] is False
    latest = server_mod.npm_package("express")
    assert latest["version"] == "4.21.0" and latest["cached"] is False
    assert [c["version"] for c in calls["npm"]] == ["4.20.0", None]


def test_npm_package_failure_shape_and_no_cache(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(monkeypatch)  # every npm fetch fails
    first = server_mod.npm_package("nope-xyz")
    assert first["ok"] is False
    assert first["error"] == "npm lookup failed: package not found on npm"
    assert "suggestion" in first
    second = server_mod.npm_package("nope-xyz")
    assert second["ok"] is False  # failures are never cached → fetcher called again
    assert len(calls["npm"]) == 2


def test_npm_package_requires_name(fake_cache_dir, monkeypatch):
    calls = _patch_fetchers(monkeypatch)
    result = server_mod.npm_package("  ")
    assert result["ok"] is False
    assert "error" in result and "suggestion" in result
    assert calls["npm"] == []


# ---------------------------------------------------------------------------
# js_status
# ---------------------------------------------------------------------------

def test_js_status_all_ok(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)
    monkeypatch.setattr(
        server_mod, "_probe_endpoint", lambda url: {"status": "ok", "http_status": 200}
    )
    result = server_mod.js_status()
    assert result["server"] == "js-ts-mcp"
    assert result["overall"] == "ok"
    assert set(result["checks"]) == {
        "search_index",
        "cache",
        "mdn_org",
        "typescriptlang_org",
        "npm_registry",
    }
    si = result["checks"]["search_index"]
    assert si["status"] == "ok" and si["entries"] == len(FAKE_INDEX["docs"])
    assert si["mdn_count"] == FAKE_INDEX["mdn_count"]
    assert si["ts_count"] == FAKE_INDEX["ts_count"]
    assert si["stale"] is False and si["built_at"] == FAKE_INDEX["built_at"]
    assert result["checks"]["cache"]["status"] == "ok"
    for probe in ("mdn_org", "typescriptlang_org", "npm_registry"):
        assert result["checks"][probe] == {"status": "ok", "http_status": 200}


def test_js_status_degraded_when_probe_fails(fake_cache_dir, monkeypatch):
    _patch_index(monkeypatch, FAKE_INDEX)

    def probe(url):
        if "registry.npmjs.org" in url:
            return {"status": "error", "http_status": None, "error": "timeout"}
        return {"status": "ok", "http_status": 200}

    monkeypatch.setattr(server_mod, "_probe_endpoint", probe)
    result = server_mod.js_status()
    assert result["overall"] == "degraded"
    assert result["checks"]["npm_registry"]["status"] == "error"


def test_js_status_error_when_index_broken(fake_cache_dir, monkeypatch):
    _patch_index(
        monkeypatch,
        {"docs": [], "count": 0, "mdn_count": 0, "ts_count": 0,
         "built_at": None, "error": "boom"},
    )
    monkeypatch.setattr(
        server_mod, "_probe_endpoint", lambda url: {"status": "ok", "http_status": 200}
    )
    result = server_mod.js_status()
    assert result["overall"] == "error"
    assert result["checks"]["search_index"]["status"] == "error"
    assert result["checks"]["search_index"]["error"] == "boom"

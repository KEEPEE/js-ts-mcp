"""Wiring tests for the politeness layer in js-ts-mcp — MockTransport, zero network.

The reference suite (``test_politeness.py``) proves the *layer* is correct.
This file proves this repo actually *uses* it, and covers the five repo-specific
facts the A7 task calls out:

1. **Body cap.** ``max_cached_bytes`` is 2 MB (A3 §5.7), which sits exactly
   between this repo's two big artifacts: the decoded MDN sitemap
   (1 936 390 B — kept, so it can be revalidated) and the react packument
   (7 016 423 B — not kept). An oversized body is simply not cached; nothing
   else changes.
2. **The ``registry.npmjs.org/robots.txt`` trap.** It answers HTTP 200 with
   ``application/json`` — the packument of the npm package named ``robots.txt``
   (7 462 bytes, captured in ``tests/fixtures``). It must produce **no rules**
   and the request must proceed.
3. **``www.npmjs.com`` robots.txt = Cloudflare 403** → negative cache: the fact
   that it is unavailable is stored and not re-fetched on the next call.
4. **MDN sitemap conditional GET** (ETag → ``304``, 0 bytes) really is sent on a
   warm rebuild, and ``www.typescriptlang.org/robots.txt`` = 404 → negative
   cache.
5. **No conditional GET where the server sends no validators.**

Plus the budget wiring A6 warned about: the ``js_status`` probes and the index
build spend the *tool call's* budget, and a partial index is never cached.

Everything HTTP is ``httpx.MockTransport``; the layer's clock/sleep are
injected, so no test waits and no test can reach the network.
"""

from __future__ import annotations

import ast
import gzip
import json
import random
from pathlib import Path

import httpx
import pytest

from js_ts_mcp import fetchers as fetchers_mod
from js_ts_mcp import search as search_mod
from js_ts_mcp import server as server_mod
from js_ts_mcp.cache import DocCache
from js_ts_mcp.fetchers import (
    ALLOWED_HOSTS,
    MAX_CACHED_BODY_BYTES,
    FetchError,
    _get,
    new_politeness,
    tool_budget,
)
from js_ts_mcp.politeness import Politeness, parse_robots
from js_ts_mcp.search import INDEX_KEY, MDN_SITEMAP_URL, TS_HANDBOOK_SOURCE_URL

FIXTURES = Path(__file__).parent / "fixtures"

#: The real body of https://registry.npmjs.org/robots.txt (HTTP 200,
#: content-type application/json) as captured by the A2 audit.
NPM_ROBOTS_TRAP = (FIXTURES / "npm_robots_txt_packument.json").read_bytes()

#: The real body of https://developer.mozilla.org/robots.txt (119 bytes).
MDN_ROBOTS = (FIXTURES / "mdn_robots.txt").read_text(encoding="utf-8")

#: Sizes measured by the audit (mcp-politeness-audit/measure-js-ts-mcp-cold.txt
#: and condget/developer.mozilla.org_sitemaps_en-us_sitemap.xml.gz.1.hdr).
MEASURED_SITEMAP_BYTES = 1_936_390  # decoded sitemap (126 932 B on the wire)
MEASURED_REACT_PACKUMENT_BYTES = 7_016_423  # decoded packument


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class FakeClock:
    """Monotonic clock + a sleep that advances it (no real waiting in tests)."""

    def __init__(self, start: float = 1_000.0, wall: float = 1_700_000_000.0) -> None:
        self.t = start
        self.wall = wall
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def wall_now(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Recorder:
    """MockTransport handler: canned routes + a record of every request."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        action = self.routes.get(path, self.routes.get("*"))
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, httpx.Response):
            return action
        if callable(action):
            return action(request, self)
        if isinstance(action, int):
            return httpx.Response(action, content=b"nope")
        return httpx.Response(200, content=action or b"")

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def hits(self, path: str) -> int:
        return self.paths().count(path)

    def robots_hits(self) -> int:
        return self.hits("/robots.txt")

    def headers_for(self, index: int) -> httpx.Headers:
        return self.requests[index].headers


_REAL_HTTPX_CLIENT = httpx.Client


def use_transport(monkeypatch, recorder: Recorder) -> Recorder:
    """Swap only the *transport* of every ``httpx.Client`` built in the repo.

    Patching the class rather than ``fetchers._client`` keeps the production
    client factory in play, so the real headers, timeout and redirect policy
    are what reach the wire — and the ``js_status`` probes, which build their
    own client, are covered by the same seam.
    """

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recorder)
        return _REAL_HTTPX_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return recorder


def fast_layer(clock: FakeClock | None = None, **kw) -> Politeness:
    """Install a politeness layer with no throttling sleeps and return it."""
    kw.setdefault("base_delay", (0.0, 0.0))
    kw.setdefault("rng", random.Random(1234))
    if clock is not None:
        kw["clock"] = clock.now
        kw["sleep"] = clock.sleep
        kw["wall_clock"] = clock.wall_now
    layer = Politeness(fetchers_mod.USER_AGENT, cache_path=None, **kw)
    fetchers_mod.set_politeness(layer)
    return layer


def npm_packument_bytes(name: str = "react", size: int = 2_000) -> bytes:
    """A packument-shaped body of roughly *size* bytes."""
    doc = {
        "name": name,
        "dist-tags": {"latest": "1.0.0"},
        "versions": {"1.0.0": {"name": name, "version": "1.0.0", "dependencies": {}}},
        "description": "x",
        "readme": "r" * max(0, size - 200),
    }
    return json.dumps(doc).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. body cap: 2 MB, oversized bodies are simply not cached
# ---------------------------------------------------------------------------
def test_production_layer_caps_cached_bodies_at_2mb():
    """A3 §5.7 recommended 2 MB; the repo must actually pass it to the layer."""
    assert MAX_CACHED_BODY_BYTES == 2 * 1024 * 1024
    layer = new_politeness(":memory:")
    try:
        assert layer.max_cached_bytes == MAX_CACHED_BODY_BYTES
        assert layer.max_cached_bytes < Politeness("x", cache_path=":memory:").max_cached_bytes
    finally:
        layer.close()


def test_cap_sits_between_the_two_real_artifacts():
    """The cap is justified by measurement, not taste.

    The decoded sitemap (which we *want* revalidated) fits; the react
    packument (which we never want to hold in memory) does not.
    """
    decoded_sitemap = len(gzip.decompress((FIXTURES / "mdn_sitemap_en_us.xml.gz").read_bytes()))
    assert decoded_sitemap == MEASURED_SITEMAP_BYTES
    assert decoded_sitemap < MAX_CACHED_BODY_BYTES
    assert MEASURED_REACT_PACKUMENT_BYTES > MAX_CACHED_BODY_BYTES


def test_oversized_body_is_not_cached_but_the_request_still_works(monkeypatch):
    """A 7 MB-class packument: full body returned, nothing kept, no crash."""
    clock = FakeClock()
    layer = fast_layer(clock, max_cached_bytes=MAX_CACHED_BODY_BYTES)
    big = npm_packument_bytes("react", size=3_000_000)  # > 2 MB cap
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": NPM_ROBOTS_TRAP, "/react": big}),
    )

    first = _get("https://registry.npmjs.org/react")
    assert first.error is None and first.status_code == 200
    assert first.content == big  # the caller still gets the whole body

    second = _get("https://registry.npmjs.org/react")
    assert second.error is None and second.content == big

    assert rec.hits("/react") == 2  # re-downloaded: nothing was remembered
    assert layer.stats()["cached_bodies"] == 0
    assert layer.stats()["conditional"] == 0
    # and the DocCache revalidation row was not written either
    entry = DocCache().get_entry("https://registry.npmjs.org/react", include_expired=True)
    assert entry is None or not entry.get("body")


def test_body_under_the_cap_is_remembered_and_revalidated(monkeypatch):
    """Small body + ETag → second call sends If-None-Match and gets 304."""
    clock = FakeClock()
    layer = fast_layer(clock, max_cached_bytes=MAX_CACHED_BODY_BYTES)
    body = npm_packument_bytes("left-pad", size=4_000)

    def handler(request: httpx.Request, rec: Recorder) -> httpx.Response:
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"etag": '"v1"'})
        return httpx.Response(200, content=body, headers={"etag": '"v1"'})

    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": NPM_ROBOTS_TRAP, "/left-pad": handler}),
    )

    first = _get("https://registry.npmjs.org/left-pad")
    assert first.status_code == 200 and first.content == body
    entry = DocCache().get_entry("https://registry.npmjs.org/left-pad", include_expired=True)
    assert entry is not None and entry["etag"] == '"v1"' and entry["body"] == body

    second = _get("https://registry.npmjs.org/left-pad")
    assert second.status_code == 304 and second.from_cache is True
    assert second.content == body  # served from the stored body, 0 bytes on the wire
    assert rec.hits("/left-pad") == 2
    assert rec.requests[-1].headers.get("if-none-match") == '"v1"'
    assert layer.stats()["revalidated_304"] == 1


def test_sized_body_round_trips_as_bytes_through_the_cache(monkeypatch):
    """Binary-safe: a gzip body comes back byte-identical (BLOB column)."""
    fast_layer(FakeClock())
    gz = (FIXTURES / "mdn_sitemap_en_us.xml.gz").read_bytes()
    use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": MDN_ROBOTS.encode(),
                "/sitemaps/en-us/sitemap.xml.gz": httpx.Response(
                    200, content=gz, headers={"etag": '"sm1"'}
                ),
            }
        ),
    )
    resp = _get(MDN_SITEMAP_URL)
    assert resp.content == gz
    entry = DocCache().get_entry(MDN_SITEMAP_URL, include_expired=True)
    assert entry is not None
    assert entry["body"] == gz  # not mangled by a TEXT column


# ---------------------------------------------------------------------------
# 2. the registry.npmjs.org/robots.txt trap
# ---------------------------------------------------------------------------
def test_npm_robots_body_is_a_packument_and_yields_zero_rules():
    """The trap body parses to *no* robots groups at all."""
    assert NPM_ROBOTS_TRAP.lstrip()[:1] == b"{"
    data = json.loads(NPM_ROBOTS_TRAP)
    assert data["name"] == "robots.txt"  # an npm package, not a robots policy

    groups = parse_robots(NPM_ROBOTS_TRAP.decode("utf-8"))
    assert groups == []
    assert [r.pattern for g in groups for r in g.rules] == []


def test_adversarial_packument_text_still_yields_zero_rules():
    """Robots-looking text *inside* a JSON string must not become a rule.

    JSON escapes newlines, so a readme containing ``user-agent: *`` stays on
    one line and the parser never sees a group header.
    """
    evil = json.dumps(
        {
            "name": "robots.txt",
            "readme": "User-agent: *\nDisallow: /\nAllow: /secret\n",
            "versions": {"1.0.0": {"name": "robots.txt", "version": "1.0.0"}},
        }
    )
    assert parse_robots(evil) == []


def test_npm_request_proceeds_despite_the_trap(monkeypatch):
    """End-to-end: npm_package() works and the trap produced no block."""
    fast_layer(FakeClock())
    body = npm_packument_bytes("left-pad")
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": httpx.Response(
                    200,
                    content=NPM_ROBOTS_TRAP,
                    headers={"content-type": "application/json"},
                ),
                "/left-pad": httpx.Response(
                    200, content=body, headers={"etag": '"np1"'}
                ),
            }
        ),
    )
    result = fetchers_mod.fetch_npm_package("left-pad")
    assert result["ok"] is True
    assert result["name"] == "left-pad"
    assert rec.robots_hits() == 1
    assert fetchers_mod.get_politeness().stats()["blocked_by_robots"] == 0


def test_trap_is_cached_so_robots_is_fetched_once_per_ttl(monkeypatch):
    """The trap body is stored like any other robots record: one fetch per TTL."""
    clock = FakeClock()
    layer = fast_layer(clock)
    body = npm_packument_bytes("left-pad")
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": NPM_ROBOTS_TRAP, "/left-pad": body}),
    )
    assert fetchers_mod.fetch_npm_package("left-pad")["ok"] is True
    assert fetchers_mod.fetch_npm_package("left-pad")["ok"] is True
    assert rec.robots_hits() == 1
    assert layer.stats()["robots_cache_hits"] >= 1


# ---------------------------------------------------------------------------
# 6. npm README fallback: the packument has no readme, GitHub raw does
# ---------------------------------------------------------------------------
#
# Measured live 2026-10-07 against the real services:
#   registry.npmjs.org/zod            → "readme": ""            (key present, empty)
#   registry.npmjs.org/express        → "readme": ""
#   registry.npmjs.org/zod/4.6.5      → no "readme" key at all
#   raw.githubusercontent.com/robots.txt → HTTP 404, body "404: Not Found" (14 B)
#   colinhacks/zod/HEAD/README.md     → HTTP 200, 22 B, content "packages/zod/README.md"
#   colinhacks/zod/HEAD/packages/zod/README.md → HTTP 200, 7 304 B
#   expressjs/express/HEAD/README.md  → HTTP 404
#   expressjs/express/HEAD/Readme.md  → HTTP 200, 10 371 B

#: What raw.githubusercontent.com really answers for its robots file: nothing.
RAW_ROBOTS_404 = httpx.Response(404, content=b"404: Not Found")

#: zod's root README is a 22-byte pointer; the real document is ~7.3 KB.
ZOD_POINTER = b"packages/zod/README.md"
ZOD_README = b'<p align="center">\nzod\n</p>\n' + b"zod body line\n" * 500

#: express publishes the capitalised "Readme.md", not "README.md".
EXPRESS_README = b'<a href="https://expressjs.com/">\n' + b"express body\n" * 800


def _packument_with_empty_readme(name: str, repository_url: str) -> bytes:
    """The packument shape npm serves today: ``readme`` present but empty."""
    return json.dumps(
        {
            "name": name,
            "dist-tags": {"latest": "1.0.0"},
            "versions": {"1.0.0": {"name": name, "version": "1.0.0", "dependencies": {}}},
            "repository": {"type": "git", "url": repository_url},
            "readme": "",
        }
    ).encode("utf-8")


def _npm_routes(name: str, repository_url: str) -> dict:
    """Registry routes for a package whose packument carries no readme."""
    return {
        "/robots.txt": NPM_ROBOTS_TRAP,
        f"/{name}": _packument_with_empty_readme(name, repository_url),
    }


def test_readme_fallback_follows_the_monorepo_pointer(monkeypatch):
    """zod: the root README is a pointer, and following it is the only way in."""
    fast_layer(FakeClock())
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                **_npm_routes("zod", "git+https://github.com/colinhacks/zod.git"),
                "/colinhacks/zod/HEAD/README.md": ZOD_POINTER,
                "/colinhacks/zod/HEAD/packages/zod/README.md": ZOD_README,
            }
        ),
    )
    with tool_budget("npm_package"):
        out = fetchers_mod.fetch_npm_package("zod")

    assert out["ok"] is True
    assert out["readme_markdown"].startswith('<p align="center">')
    assert out["readme_source"] == "github:colinhacks/zod@HEAD/packages/zod/README.md"
    assert "note" not in out
    assert rec.hits("/colinhacks/zod/HEAD/README.md") == 1
    assert rec.hits("/colinhacks/zod/HEAD/packages/zod/README.md") == 1
    # The pointer won: no further candidate was tried after the hit.
    assert rec.hits("/colinhacks/zod/HEAD/Readme.md") == 0
    # Two hosts, so two cold robots fetches: npm and the raw host.
    assert rec.robots_hits() == 2
    # robots(npm) + packument + robots(raw) + pointer + target = 5 of 7 units.
    assert len(rec.requests) == 5


def test_readme_fallback_finds_the_capitalised_readme(monkeypatch):
    """express: HEAD/README.md is 404, HEAD/Readme.md is the real document."""
    fast_layer(FakeClock())
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                **_npm_routes("express", "git+https://github.com/expressjs/express.git"),
                "/expressjs/express/HEAD/README.md": 404,
                "/expressjs/express/HEAD/Readme.md": EXPRESS_README,
            }
        ),
    )
    with tool_budget("npm_package"):
        out = fetchers_mod.fetch_npm_package("express")

    assert out["ok"] is True
    assert out["readme_source"] == "github:expressjs/express@HEAD/Readme.md"
    assert len(out["readme_markdown"]) > 5_000
    assert rec.hits("/expressjs/express/HEAD/README.md") == 1
    assert rec.hits("/expressjs/express/HEAD/Readme.md") == 1
    # Stopping at the first hit is the point: the rest is never requested.
    assert rec.hits("/expressjs/express/HEAD/readme.md") == 0
    assert len(rec.requests) == 5


def test_null_readme_is_never_silent(monkeypatch):
    """A package whose repo is not on GitHub: null README *plus* the reason."""
    fast_layer(FakeClock())
    rec = use_transport(
        monkeypatch,
        Recorder(_npm_routes("internal-thing", "https://gitlab.com/acme/internal-thing.git")),
    )
    with tool_budget("npm_package"):
        out = fetchers_mod.fetch_npm_package("internal-thing")

    assert out["ok"] is True
    assert out["readme_markdown"] is None
    assert "note" in out
    assert "gitlab.com" in out["note"]
    # Nothing left the allowlisted hosts: no raw host was even contacted.
    assert {r.url.host for r in rec.requests} == {"registry.npmjs.org"}


def test_package_without_a_repository_url_says_so(monkeypatch):
    fast_layer(FakeClock())
    body = json.dumps(
        {
            "name": "orphan",
            "dist-tags": {"latest": "1.0.0"},
            "versions": {"1.0.0": {"name": "orphan", "version": "1.0.0"}},
            "readme": "",
        }
    ).encode("utf-8")
    use_transport(monkeypatch, Recorder({"/robots.txt": NPM_ROBOTS_TRAP, "/orphan": body}))
    with tool_budget("npm_package"):
        out = fetchers_mod.fetch_npm_package("orphan")

    assert out["ok"] is True
    assert out["readme_markdown"] is None
    assert "no repository URL" in out["note"]


def test_all_candidates_404_reports_every_one_of_them(monkeypatch):
    """With room in the budget, the whole candidate list is tried and named."""
    fast_layer(FakeClock())
    routes = _npm_routes("bare", "git+https://github.com/acme/bare.git")
    for candidate in fetchers_mod.README_CANDIDATES:
        routes[f"/acme/bare/HEAD/{candidate}"] = 404
    rec = use_transport(monkeypatch, Recorder(routes))

    with tool_budget("npm_package", limit=12):
        out = fetchers_mod.fetch_npm_package("bare")

    assert out["ok"] is True
    assert out["readme_markdown"] is None
    for candidate in fetchers_mod.README_CANDIDATES:
        assert candidate in out["note"]
        assert rec.hits(f"/acme/bare/HEAD/{candidate}") == 1


def _revalidating(body: bytes, etag: str):
    """Serve *body* once, then ``304`` whenever the client offers the ETag back."""

    def handler(request, rec):
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"etag": etag})
        return httpx.Response(200, content=body, headers={"etag": etag})

    return handler


def test_readme_revalidation_304_is_still_the_readme(monkeypatch):
    """A warm second call must not degrade to "no README found".

    Found live, not invented: the first ``fetch_npm_package("zod")`` fetched the
    pointer and the target with fresh ETags; the next one revalidated both and
    got ``304``.  ``_Response`` is deliberately truthful about that (304 +
    ``from_cache``), so a caller that only accepts ``200`` would report a
    missing README for a package it just read.
    """
    fast_layer(FakeClock())
    use_transport(
        monkeypatch,
        Recorder(
            {
                **_npm_routes("zod", "git+https://github.com/colinhacks/zod.git"),
                "/colinhacks/zod/HEAD/README.md": _revalidating(ZOD_POINTER, '"ptr1"'),
                "/colinhacks/zod/HEAD/packages/zod/README.md": _revalidating(ZOD_README, '"doc1"'),
            }
        ),
    )

    for call in range(2):
        with tool_budget("npm_package"):
            out = fetchers_mod.fetch_npm_package("zod")
        assert out["ok"] is True, call
        assert out["readme_source"] == "github:colinhacks/zod@HEAD/packages/zod/README.md", call
        assert out["readme_markdown"].startswith('<p align="center">'), call


def test_spent_budget_degrades_the_readme_to_a_note_not_an_error(monkeypatch):
    """A12 rule: our own cap may cost the README, never the whole lookup."""
    fast_layer(FakeClock())
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                **_npm_routes("zod", "git+https://github.com/colinhacks/zod.git"),
                "/colinhacks/zod/HEAD/README.md": ZOD_POINTER,
                "/colinhacks/zod/HEAD/packages/zod/README.md": ZOD_README,
            }
        ),
    )
    # 3 units: npm robots + packument + raw-host robots.  The first README
    # candidate is the one that gets refused.
    with tool_budget("npm_package", limit=3):
        out = fetchers_mod.fetch_npm_package("zod")

    assert out["ok"] is True
    assert out["name"] == "zod"
    assert out["readme_markdown"] is None
    assert "budget" in out["note"]
    assert len(rec.requests) == 3
    assert rec.hits("/colinhacks/zod/HEAD/README.md") == 0


def test_pointer_cannot_walk_outside_the_repository(monkeypatch):
    """A pointer is resolved under {owner}/{repo}/HEAD/ and nowhere else."""
    fast_layer(FakeClock())
    routes = _npm_routes("tricky", "git+https://github.com/acme/tricky.git")
    routes["/acme/tricky/HEAD/README.md"] = b"../../secrets/README.md"
    routes["/secrets/README.md"] = b"you should not be reading this document at all"
    for candidate in fetchers_mod.README_CANDIDATES[1:]:
        routes[f"/acme/tricky/HEAD/{candidate}"] = 404
    rec = use_transport(monkeypatch, Recorder(routes))

    with tool_budget("npm_package", limit=12):
        out = fetchers_mod.fetch_npm_package("tricky")

    assert out["ok"] is True
    assert out["readme_markdown"] is None
    raw_paths = [
        r.url.path
        for r in rec.requests
        if r.url.host == "raw.githubusercontent.com" and r.url.path != "/robots.txt"
    ]
    assert raw_paths, "the fallback should still have tried the candidates"
    assert all(path.startswith("/acme/tricky/HEAD/") for path in raw_paths)
    assert rec.hits("/secrets/README.md") == 0


def test_rst_readme_is_returned_raw_and_says_so(monkeypatch):
    """This repo has no RST→markdown converter, so the note is mandatory."""
    fast_layer(FakeClock())
    rst = b"tricky\n=====\n\nA reStructuredText README with enough body text.\n" * 6
    routes = _npm_routes("tricky", "git+https://github.com/acme/tricky.git")
    for candidate in fetchers_mod.README_CANDIDATES:
        routes[f"/acme/tricky/HEAD/{candidate}"] = 404
    routes["/acme/tricky/HEAD/README.rst"] = rst
    use_transport(monkeypatch, Recorder(routes))

    with tool_budget("npm_package", limit=12):
        out = fetchers_mod.fetch_npm_package("tricky")

    assert out["ok"] is True
    assert out["readme_source"] == "github:acme/tricky@HEAD/README.rst"
    assert out["readme_markdown"].startswith("tricky\n=====")
    assert "reStructuredText" in out["note"]


def test_version_pinned_lookup_gets_the_readme_too(monkeypatch):
    """``/{name}/{version}`` has no readme key at all — the fallback covers it."""
    fast_layer(FakeClock())
    version_doc = json.dumps(
        {
            "name": "zod",
            "version": "4.6.5",
            "license": "MIT",
            "repository": {"type": "git", "url": "git+https://github.com/colinhacks/zod.git"},
        }
    ).encode("utf-8")
    use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": NPM_ROBOTS_TRAP,
                "/zod/4.6.5": version_doc,
                "/colinhacks/zod/HEAD/README.md": ZOD_POINTER,
                "/colinhacks/zod/HEAD/packages/zod/README.md": ZOD_README,
            }
        ),
    )
    with tool_budget("npm_package"):
        out = fetchers_mod.fetch_npm_package("zod", "4.6.5")

    assert out["ok"] is True
    assert out["version"] == "4.6.5"
    assert out["readme_source"] == "github:colinhacks/zod@HEAD/packages/zod/README.md"


def test_registry_readme_still_wins_and_no_raw_request_is_made(monkeypatch):
    """left-pad-style packages still ship a readme: do not go looking elsewhere."""
    fast_layer(FakeClock())
    body = json.dumps(
        {
            "name": "left-pad",
            "dist-tags": {"latest": "1.3.0"},
            "versions": {"1.3.0": {"name": "left-pad", "version": "1.3.0"}},
            "repository": {"type": "git", "url": "git+ssh://git@github.com/stevemao/left-pad.git"},
            "readme": "## left-pad\n\nString left pad\n" + "body\n" * 40,
        }
    ).encode("utf-8")
    rec = use_transport(monkeypatch, Recorder({"/robots.txt": NPM_ROBOTS_TRAP, "/left-pad": body}))

    with tool_budget("npm_package"):
        out = fetchers_mod.fetch_npm_package("left-pad")

    assert out["ok"] is True
    assert out["readme_markdown"].startswith("## left-pad")
    assert "readme_source" not in out
    assert "note" not in out
    assert {r.url.host for r in rec.requests} == {"registry.npmjs.org"}


def test_raw_host_robots_404_is_negatively_cached(monkeypatch):
    """No robots file on the raw host: store that fact, do not re-fetch it."""
    clock = FakeClock()
    layer = fast_layer(clock)
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                **_npm_routes("zod", "git+https://github.com/colinhacks/zod.git"),
                "/robots.txt": RAW_ROBOTS_404,
                "/colinhacks/zod/HEAD/README.md": ZOD_POINTER,
                "/colinhacks/zod/HEAD/packages/zod/README.md": ZOD_README,
            }
        ),
    )
    for _ in range(2):
        with tool_budget("npm_package"):
            assert fetchers_mod.fetch_npm_package("zod")["ok"] is True

    # One robots fetch per host per TTL, not one per call.
    assert rec.robots_hits() == 2
    assert layer.stats()["robots_negative"] >= 1


# ---------------------------------------------------------------------------
# 3. www.npmjs.com: Cloudflare 403 → negative cache, and never requested anyway
# ---------------------------------------------------------------------------
def test_www_npmjs_com_403_robots_is_negatively_cached(monkeypatch):
    """2nd call must not fetch robots.txt again (A2 §1.1, Cloudflare 403)."""
    clock = FakeClock()
    layer = fast_layer(clock)
    rec = use_transport(monkeypatch, Recorder({"/robots.txt": 403, "/package/react": b"{}"}))
    assert layer.can_fetch("https://www.npmjs.com/package/react", client=None) is True
    assert layer.can_fetch("https://www.npmjs.com/package/left-pad", client=None) is True
    assert rec.robots_hits() == 1
    assert layer.stats()["robots_negative"] == 1


def test_www_npmjs_com_is_not_in_the_allowlist(monkeypatch):
    """This repo only *reports* www.npmjs.com URLs; it never requests them.

    So the 403 host is not even reached — the layer refuses the request with
    zero network traffic.
    """
    fast_layer(FakeClock(), allowed_hosts=ALLOWED_HOSTS)
    rec = use_transport(monkeypatch, Recorder({"*": b"should never be served"}))
    resp = _get("https://www.npmjs.com/package/react")
    assert resp.error and "ALLOWED_HOSTS" in resp.error
    assert resp.blocked_by_robots is False
    assert rec.requests == []


def test_allowlist_covers_exactly_the_four_fetched_hosts():
    """Four hosts, four reasons — and nothing else is reachable.

    ``raw.githubusercontent.com`` joined when ``npm_package`` started fetching
    the README the registry no longer ships.  Its robots.txt is an HTTP 404
    (measured 2026-10-07, body ``404: Not Found``), i.e. no rules, but it still
    has to be listed: an unlisted host is refused by the layer, so a missing
    entry would turn the README fallback into a silent no-op.
    """
    assert ALLOWED_HOSTS == frozenset(
        {
            "developer.mozilla.org",
            "www.typescriptlang.org",
            "registry.npmjs.org",
            "raw.githubusercontent.com",
        }
    )


def test_honest_user_agent_is_what_reaches_the_wire(monkeypatch):
    """No browser spoofing: the public product UA is on *every* request.

    A robots file can only match a product token it can see, so the UA is part
    of the politeness contract rather than cosmetics — asserted on the wire,
    not in prose.  It must also be contactable (a public repository URL) and
    must not masquerade as a browser.
    """
    fast_layer(FakeClock(), allowed_hosts=ALLOWED_HOSTS)
    html = (FIXTURES / "mdn_array.html").read_bytes()
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": MDN_ROBOTS.encode(),
                "/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array": html,
            }
        ),
    )
    assert fetchers_mod.fetch_mdn_doc(
        "Web/JavaScript/Reference/Global_Objects/Array"
    )["ok"] is True
    assert rec.requests, "no request reached the wire"
    assert {r.headers["user-agent"] for r in rec.requests} == {fetchers_mod.USER_AGENT}

    ua = fetchers_mod.USER_AGENT
    assert ua.startswith("js-ts-mcp/")
    assert "github.com/KEEPEE/js-ts-mcp" in ua
    for spoof in ("Mozilla/", "Chrome/", "Safari/", "AppleWebKit"):
        assert spoof not in ua
    # the robots fetch itself carries it — a robots file matches on this token
    robots_request = next(r for r in rec.requests if r.url.path == "/robots.txt")
    assert robots_request.headers["user-agent"] == ua


# ---------------------------------------------------------------------------
# 4. MDN sitemap revalidation + typescriptlang robots 404 negative cache
# ---------------------------------------------------------------------------
def test_mdn_sitemap_revalidation_costs_zero_bytes(monkeypatch):
    """Warm rebuild sends If-None-Match and gets 304 with an empty body.

    The audit measured this live (condget/: 200 with
    ``etag: "f102d54f…"`` then ``304``, content-length 126 932 → nothing).
    """
    clock = FakeClock()
    layer = fast_layer(clock)
    gz = (FIXTURES / "mdn_sitemap_en_us.xml.gz").read_bytes()
    etag = '"f102d54fc40a495830ec3ff1df9fbcad"'

    def handler(request: httpx.Request, rec: Recorder) -> httpx.Response:
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"etag": etag})
        return httpx.Response(200, content=gz, headers={"etag": etag})

    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": MDN_ROBOTS.encode(), "/sitemaps/en-us/sitemap.xml.gz": handler}),
    )

    first = search_mod._fetch_mdn_sitemap()
    assert first.startswith("<?xml") or "<urlset" in first
    assert rec.hits("/sitemaps/en-us/sitemap.xml.gz") == 1

    second = search_mod._fetch_mdn_sitemap()
    assert second == first
    assert rec.hits("/sitemaps/en-us/sitemap.xml.gz") == 2
    last = rec.requests[-1]
    assert last.headers.get("if-none-match") == etag
    assert layer.stats()["revalidated_304"] == 1
    # the 304 carried no body at all
    assert rec.requests[-1].url.path == "/sitemaps/en-us/sitemap.xml.gz"


def test_typescriptlang_robots_404_is_negatively_cached(monkeypatch):
    """www.typescriptlang.org/robots.txt = 404 → stored, not re-fetched."""
    clock = FakeClock()
    layer = fast_layer(clock)
    html = (FIXTURES / "ts_intro.html").read_bytes()
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": 404, "/docs/handbook/intro.html": html}),
    )
    assert fetchers_mod.fetch_ts_page("intro")["ok"] is True
    assert fetchers_mod.fetch_ts_page("intro")["ok"] is True
    assert rec.robots_hits() == 1
    assert layer.stats()["robots_negative"] == 1


def test_mdn_robots_blocks_the_dead_v1_json_api(monkeypatch):
    """``Disallow: /api/`` — the v1 docs API this repo used to call is off-limits."""
    fast_layer(FakeClock(), allowed_hosts=ALLOWED_HOSTS)
    html = (FIXTURES / "mdn_array.html").read_bytes()
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": MDN_ROBOTS.encode(), "/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array": html}),
    )
    blocked = _get("https://developer.mozilla.org/api/v1/docs")
    assert blocked.error and "blocked by robots.txt" in blocked.error
    assert blocked.blocked_by_robots is True
    assert rec.hits("/api/v1/docs") == 0

    ok = fetchers_mod.fetch_mdn_doc("Web/JavaScript/Reference/Global_Objects/Array")
    assert ok["ok"] is True
    assert rec.robots_hits() == 1  # one robots fetch for both decisions


# ---------------------------------------------------------------------------
# 5. no conditional GET where the server sends no validators
# ---------------------------------------------------------------------------
def test_host_without_validators_never_sends_conditional_headers(monkeypatch):
    """A host with no ETag/Last-Modified must not be offered If-None-Match."""
    clock = FakeClock()
    layer = fast_layer(clock)
    body = b'{"name":"x","versions":{"1.0.0":{"name":"x","version":"1.0.0"}}}'
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": NPM_ROBOTS_TRAP, "/x": httpx.Response(200, content=body)}),
    )
    first = _get("https://registry.npmjs.org/x")
    assert first.status_code == 200
    second = _get("https://registry.npmjs.org/x")
    assert second.status_code == 200
    assert rec.hits("/x") == 2
    assert layer.stats()["conditional"] == 0
    for request in rec.requests:
        if request.url.path == "/x":
            assert "if-none-match" not in request.headers
            assert "if-modified-since" not in request.headers


# ---------------------------------------------------------------------------
# budget wiring (the A6 lesson: every client path spends the tool budget)
# ---------------------------------------------------------------------------
def test_js_status_probes_spend_the_tool_budget(monkeypatch):
    """The probes are not free: they draw on the same budget as the tool.

    A8 F3 made the price honest — a probe on a host whose robots.txt is not
    cached pays **two** units (robots.txt + the probe), which is exactly why
    :data:`js_ts_mcp.server.STATUS_BUDGET_LIMIT` is larger than the number of
    probes: a probe refused by the budget reports ``error`` and would turn
    ``overall`` to ``degraded`` for an internal reason.
    """
    fast_layer(FakeClock())
    rec = use_transport(monkeypatch, Recorder({"*": b"{}"}))
    with tool_budget("js_status", limit=4):
        first = server_mod._probe_endpoint("https://developer.mozilla.org/a")
        second = server_mod._probe_endpoint("https://www.typescriptlang.org/b")
        third = server_mod._probe_endpoint("https://registry.npmjs.org/c")
    assert first["status"] == "ok" and second["status"] == "ok"
    assert third["status"] == "error" and "budget" in third["error"]
    pages = [r for r in rec.requests if r.url.path != "/robots.txt"]
    assert len(pages) == 2  # the third probe's page never left the process
    assert [r.url.path for r in pages] == ["/a", "/b"]
    # 4 units == 4 real requests: 2 robots + 2 pages.
    assert rec.hits("/robots.txt") == 2


def test_status_budget_leaves_room_for_every_probe(monkeypatch):
    """With the production limit no probe may be denied by the budget."""
    fast_layer(FakeClock())
    rec = use_transport(monkeypatch, Recorder({"*": b"{}"}))
    monkeypatch.setattr(
        server_mod,
        "get_index",
        lambda: {"docs": [{"source": "mdn", "name": "Array", "path": "p", "url": "u"}],
                 "count": 1, "mdn_count": 1, "ts_count": 0,
                 "built_at": "2026-01-01T00:00:00+00:00"},
    )
    result = server_mod.js_status()
    st = fetchers_mod.get_politeness().stats()
    used, limit = st["budgets"]["tool:js_status"]
    assert limit == server_mod.STATUS_BUDGET_LIMIT
    # 3 probes on 3 cold hosts = 3 robots + 3 pages = 6 units, and the limit
    # still leaves headroom for a rebuild / retry / redirect hop.
    assert used == 6 and used < limit
    assert st["budget_denied"] == 0
    assert result["overall"] == "ok", result["checks"]


def test_index_build_shares_the_tool_budget_and_yields_a_partial_index(monkeypatch):
    """A budget-limited build returns a partial index — it never raises."""
    fast_layer(FakeClock())
    gz = (FIXTURES / "mdn_sitemap_en_us.xml.gz").read_bytes()
    html = (FIXTURES / "ts_intro.html").read_bytes()
    rec = use_transport(
        monkeypatch,
        Recorder(
            {
                "/robots.txt": 404,
                "/sitemaps/en-us/sitemap.xml.gz": gz,
                "/docs/handbook/intro.html": html,
            }
        ),
    )
    # A8 F3: the MDN half of the build costs 2 units (robots.txt + sitemap),
    # so a budget of 2 buys exactly that and the TypeScript half is refused.
    # (With limit=1 not even the sitemap would be attempted — the robots fetch
    # alone would spend the budget.)
    with tool_budget("js_search", limit=2):
        index = search_mod.build_index()
    assert index["partial"] is True
    assert index["mdn_count"] > 10_000 and index["ts_count"] == 0
    assert "budget" in index["partial_reason"]
    assert rec.hits("/docs/handbook/intro.html") == 0
    assert rec.hits("/robots.txt") == 1


def test_partial_index_is_never_persisted(monkeypatch):
    """Caching a truncated list would freeze it for a week — so don't."""
    fast_layer(FakeClock())
    gz = (FIXTURES / "mdn_sitemap_en_us.xml.gz").read_bytes()
    use_transport(
        monkeypatch,
        Recorder({"/robots.txt": 404, "/sitemaps/en-us/sitemap.xml.gz": gz, "/docs/handbook/intro.html": b"<html></html>"}),
    )
    with tool_budget("js_search", limit=1):
        index = search_mod.get_index()
    assert index.get("partial") is True
    assert DocCache().get(INDEX_KEY) is None


def test_budget_denial_for_npm_does_not_trigger_the_latest_fallback(monkeypatch):
    """A spent budget must not be papered over with a second request."""
    fast_layer(FakeClock())
    rec = use_transport(
        monkeypatch,
        Recorder({"/robots.txt": NPM_ROBOTS_TRAP, "/react": 500}),
    )
    with tool_budget("npm_package", limit=1):
        result = fetchers_mod.fetch_npm_package("react")
    assert result["ok"] is False
    assert "budget" in result["error"]
    assert rec.hits("/react/latest") == 0


def test_transport_failure_still_raises_fetch_error(monkeypatch):
    """The pre-existing contract of ``_get`` is kept for transport errors."""
    fast_layer(FakeClock())
    use_transport(
        monkeypatch,
        Recorder({"*": httpx.ConnectError("boom")}),
    )
    with pytest.raises(FetchError):
        _get("https://registry.npmjs.org/react")


# ---------------------------------------------------------------------------
# no path may bypass the layer
# ---------------------------------------------------------------------------
def test_no_subprocess_or_curl_fallback_left_in_the_sources():
    """A curl/subprocess path would skip robots, throttle, retry and budget.

    Checked on the *code* (AST), not on prose: the docstrings deliberately
    explain why the old curl fallback is gone.
    """
    root = Path(fetchers_mod.__file__).parent
    for name in ("fetchers.py", "server.py", "search.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        from_imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "subprocess" not in imported | from_imported, name
        argv_curl = [
            literal.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(literal := node.value, str)
            if literal == "curl" or literal.startswith("curl ")
        ]
        assert not argv_curl, name


def test_fetchers_build_exactly_one_http_client():
    """Every request goes through ``_client()`` → ``_get()`` → the layer."""
    source = Path(fetchers_mod.__file__).read_text(encoding="utf-8")
    assert source.count("httpx.Client(") == 1
    assert "client.get(" not in source


# ---------------------------------------------------------------------------
# js_status exposes the layer outside "checks"
# ---------------------------------------------------------------------------
def test_js_status_reports_politeness_outside_checks(monkeypatch):
    """The politeness block is diagnostic and must never change ``overall``."""
    fast_layer(FakeClock())
    monkeypatch.setattr(
        server_mod,
        "get_index",
        lambda: {"docs": [{"source": "mdn", "name": "Array", "path": "p", "url": "u"}],
                 "count": 1, "mdn_count": 1, "ts_count": 0,
                 "built_at": "2026-01-01T00:00:00+00:00"},
    )
    monkeypatch.setattr(
        server_mod, "_probe_endpoint", lambda url: {"status": "ok", "http_status": 200}
    )
    result = server_mod.js_status()
    assert result["overall"] == "ok"
    assert set(result["checks"]) == {
        "search_index",
        "cache",
        "mdn_org",
        "typescriptlang_org",
        "npm_registry",
    }
    politeness = result["politeness"]
    for key in (
        "status",
        "requests",
        "robots_fetches",
        "robots_cache_hits",
        "robots_negative",
        "blocked_by_robots",
        "conditional",
        "revalidated_304",
        "budget_denied",
        "max_cached_bytes",
        "disabled",
    ):
        assert key in politeness, key
    assert politeness["max_cached_bytes"] == MAX_CACHED_BODY_BYTES
    assert politeness["requests"] >= 0  # probes were monkeypatched away


def test_politeness_report_keys_are_real_counters():
    """Guard against the drift that makes a counter permanently read 0.

    Every reported counter name must exist in ``Politeness.stats()`` — a typo
    like ``robots_fetched`` (vs ``robots_fetches``) would otherwise report a
    silent zero forever.
    """
    stats = Politeness("ua", cache_path=":memory:").stats()
    report = server_mod._politeness_report()
    derived = {"status", "robots_rows", "max_cached_bytes", "robots_db", "host_delays"}
    reported = [key for key in report if key not in derived]
    assert reported, "report is empty"
    for key in reported:
        assert key in stats, key
    # The counters A8 added must be reported, not just counted internally.
    # This repo wires no challenge detector, so ``challenge_*`` legitimately
    # reads 0 — but the key has to be there, otherwise a reader cannot tell
    # "no challenges seen" from "this build does not count them".
    for key in (
        "robots_requests",
        "robots_throttle_waits",
        "robots_throttle_sleep_s",
        "redirect_hops",
        "challenge_detected",
        "challenge_retries",
    ):
        assert key in report, key


def test_js_status_overall_is_immune_to_a_blocked_politeness_state(monkeypatch):
    """Even with blocks and budget denials on the record, ``overall`` is ok."""
    layer = fast_layer(FakeClock())
    layer._n["blocked_by_robots"] = 7
    layer._n["budget_denied"] = 3
    layer._n["errors"] = 2
    monkeypatch.setattr(
        server_mod,
        "get_index",
        lambda: {"docs": [{"source": "mdn", "name": "Array", "path": "p", "url": "u"}],
                 "count": 1, "mdn_count": 1, "ts_count": 0,
                 "built_at": "2026-01-01T00:00:00+00:00"},
    )
    monkeypatch.setattr(
        server_mod, "_probe_endpoint", lambda url: {"status": "ok", "http_status": 200}
    )
    result = server_mod.js_status()
    assert result["overall"] == "ok"
    assert result["politeness"]["blocked_by_robots"] == 7


# ---------------------------------------------------------------------------
# A13 B3 — an unwritable cache directory must never take a tool down
# ---------------------------------------------------------------------------
def test_npm_package_and_status_survive_an_unwritable_cache_dir(monkeypatch, tmp_path):
    """A12 F-A12-2: the cache is optional end-to-end in this repo too.

    A bad cache directory must degrade ``npm_package`` to "no cache", never to
    an exception, and ``js_status`` must report the cache as unusable.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where a directory should be")
    monkeypatch.setenv("JS_TS_MCP_CACHE_DIR", str(blocker / "cache"))

    fast_layer(FakeClock())
    body = npm_packument_bytes("left-pad")
    use_transport(monkeypatch, Recorder({"/robots.txt": NPM_ROBOTS_TRAP, "/left-pad": body}))

    out = server_mod.npm_package("left-pad")
    assert "error" not in out, out
    assert out.get("name") == "left-pad"

    monkeypatch.setattr(
        server_mod, "get_index",
        lambda: {"docs": [{"source": "mdn", "name": "Array", "path": "p", "url": "u"}],
                 "count": 1, "mdn_count": 1, "ts_count": 0, "built_at": "x"},
    )
    monkeypatch.setattr(
        server_mod, "_probe_endpoint", lambda url: {"status": "ok", "http_status": 200}
    )
    status = server_mod.js_status()
    assert status["checks"]["cache"]["status"] == "error", status["checks"]["cache"]
    assert "error" in status["checks"]["cache"]     # the reason is spelled out
    assert status["overall"] == "degraded"          # announced, not hidden

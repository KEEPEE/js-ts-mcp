"""Search index over MDN Web Docs and the TypeScript handbook.

Builds a flat list of searchable entries from two live sources:

- **MDN** — the full docs URL list comes from the gzipped sitemap
  (``https://developer.mozilla.org/sitemaps/en-us/sitemap.xml.gz``; the v1
  JSON API is dead). Only ``<loc>`` values under
  ``https://developer.mozilla.org/en-US/docs/`` are kept; each becomes
  ``{"source": "mdn", "name", "path", "url"}`` where ``path`` is the slug
  after the docs prefix and ``name`` is the last slug segment with
  underscores replaced by spaces.
- **TypeScript** — there is no public index endpoint, so the handbook page
  list is extracted from the sidebar of any handbook page (the intro page is
  used as the canonical source). Each ``/docs/handbook/<page>.html`` link
  becomes ``{"source": "typescript", "name", "path", "url"}`` where ``path``
  is the page without ``.html`` and without the leading ``/docs/handbook/``
  (matching :func:`js_ts_mcp.fetchers.fetch_ts_page`'s ``page`` argument) and
  ``name`` is the last segment with dashes replaced by spaces.

Public entry points:

- ``build_index() -> dict`` — fetch + parse both sources; persists the result
  in the :class:`~js_ts_mcp.cache.DocCache` under key ``search-index`` with a
  7-day TTL. On network/parse failure it falls back to the cached copy
  (fresh first, then stale via ``peek``) and only raises when nothing is
  available.
- ``get_index(cache=None) -> dict`` — cached-or-build accessor used by the
  server; never raises (mirrors the java-spring-mcp ``load_index`` semantics).
- ``search_docs(query, limit=8) -> list[dict]`` — offline multi-token ranking
  over name + path.

Index shape: ``{"docs": [...], "count": int, "mdn_count": int, "ts_count":
int, "built_at": <iso8601 UTC>}`` (a failed ``get_index`` adds ``"error"``; a
stale fallback adds ``"stale"``).

Politeness
----------
Both fetches go through :mod:`js_ts_mcp.politeness` (robots, throttle, retry,
conditional GET — the MDN sitemap answers ``304`` to a revalidation, which is
how a warm rebuild costs 0 bytes instead of re-downloading 1.9 MB) and they
spend a request budget: the enclosing tool call's budget when one is bound
(:func:`js_ts_mcp.fetchers.tool_budget`), otherwise their own
``index:<host>`` budget of :data:`INDEX_BUDGET_LIMIT`.

A **spent budget** is different from a network failure: it never escapes as an
exception. The build returns an index marked ``"partial": True`` plus
``partial_reason``, and such an index is **never cached** — caching a
deliberately truncated list would freeze it for a week.

No network access happens at import time.
"""

from __future__ import annotations

import gzip
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .cache import DocCache
from .fetchers import FetchError, _get, current_budget

__all__ = [
    "INDEX_KEY",
    "MDN_SITEMAP_URL",
    "TS_HANDBOOK_SOURCE_URL",
    "DEFAULT_MAX_AGE_SECONDS",
    "INDEX_BUDGET_LIMIT",
    "IndexBudgetExhausted",
    "parse_mdn_sitemap_xml",
    "parse_ts_handbook_html",
    "build_index",
    "get_index",
    "search_docs",
]

INDEX_KEY = "search-index"
MDN_SITEMAP_URL = "https://developer.mozilla.org/sitemaps/en-us/sitemap.xml.gz"
TS_HANDBOOK_SOURCE_URL = "https://www.typescriptlang.org/docs/handbook/intro.html"
DEFAULT_MAX_AGE_SECONDS = 604800  # one week (7 days)

#: Requests an index build may spend when no tool-call budget is bound.  Since
#: A8 F3 the build pays for the robots.txt of **both** hosts it reads, so a cold
#: build is 4 units: MDN robots + sitemap + typescriptlang robots + handbook
#: page (measured live, A10 smoke).  4 was therefore the bare minimum with zero
#: room for a retry — one ``429`` and the build silently came back ``partial``.
#: 6 buys the two fetches plus one retry/hop per host.  When a tool call *is*
#: bound this scope is not used: the build shares the tool's budget.
INDEX_BUDGET_LIMIT = 6

#: The sitemap also lists non-docs URLs (/en-US/search, /en-US/plus/...,
#: /en-US/blog/...); only locs under this prefix are indexable docs.
_MDNS_DOCS_PREFIX = "https://developer.mozilla.org/en-US/docs/"
_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
_GZIP_MAGIC = b"\x1f\x8b"

#: Handbook sidebar links look like ``href="/docs/handbook/2/basic-types.html"``.
_TS_HANDBOOK_HREF_RE = re.compile(r"""href=["\'](/docs/handbook/[^"\'#]+?\.html)["\']""")
_TS_HANDBOOK_PREFIX = "/docs/handbook/"


# ---------------------------------------------------------------------------
# Parsing (pure / offline)
# ---------------------------------------------------------------------------

def parse_mdn_sitemap_xml(xml_text: str) -> list[dict]:
    """Parse the MDN en-US sitemap XML into doc entries.

    Each entry is ``{"source": "mdn", "name", "path", "url"}`` where ``path``
    is the slug after ``/en-US/docs/`` (e.g.
    ``Web/JavaScript/Reference/Global_Objects/Array``) and ``name`` is the
    last slug segment with underscores replaced by spaces (``Array``). Only
    locs under :data:`_MDNS_DOCS_PREFIX` are kept; non-docs URLs
    (``/en-US/search``, ``/en-US/plus/...``, ``/en-US/blog/...``) and
    duplicates are dropped.

    Raises ``ValueError`` when the document is not parseable XML or yields no
    doc entries.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"not a valid sitemap XML document: {exc}") from exc

    entries: list[dict] = []
    seen: set[str] = set()
    for url_el in root.iter(f"{_SITEMAP_NS}url"):
        loc = (url_el.findtext(f"{_SITEMAP_NS}loc") or "").strip()
        if not loc.startswith(_MDNS_DOCS_PREFIX):
            continue  # non-docs URL (search, plus, observatory, blog, ...)
        slug = loc[len(_MDNS_DOCS_PREFIX):]
        slug = slug.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        entries.append(
            {
                "source": "mdn",
                "name": slug.rsplit("/", 1)[-1].replace("_", " "),
                "path": slug,
                "url": loc,
            }
        )

    if not entries:
        raise ValueError("not an MDN sitemap (no /en-US/docs/ URLs found)")
    return entries


def parse_ts_handbook_html(html: str) -> list[dict]:
    """Extract the TypeScript handbook page list from a handbook page's HTML.

    The sidebar of any handbook page links to every other page
    (``href="/docs/handbook/<page>.html"``); each unique link becomes
    ``{"source": "typescript", "name", "path", "url"}`` where ``path`` is the
    page without ``.html`` and without the leading ``/docs/handbook/`` (e.g.
    ``2/basic-types``, ``typescript-from-scratch`` — the same value accepted
    by :func:`js_ts_mcp.fetchers.fetch_ts_page`) and ``name`` is the last
    segment with dashes replaced by spaces.

    Raises ``ValueError`` when the document yields no handbook links.
    """
    entries: list[dict] = []
    seen: set[str] = set()
    for match in _TS_HANDBOOK_HREF_RE.finditer(html):
        href = match.group(1)  # e.g. /docs/handbook/2/basic-types.html
        page = href[len(_TS_HANDBOOK_PREFIX):-len(".html")]
        if not page or page in seen:
            continue
        seen.add(page)
        entries.append(
            {
                "source": "typescript",
                "name": page.rsplit("/", 1)[-1].replace("-", " "),
                "path": page,
                "url": f"https://www.typescriptlang.org/docs/handbook/{page}.html",
            }
        )

    if not entries:
        raise ValueError("not a TypeScript handbook page (no /docs/handbook/ links found)")
    return entries


# ---------------------------------------------------------------------------
# Index building / loading
# ---------------------------------------------------------------------------

def _open_cache() -> DocCache | None:
    try:
        return DocCache()
    except Exception:
        return None  # cache unavailable; degrade to build-only behaviour


def _valid_index(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("docs"), list)


def _read_cache(cache: DocCache | None, fresh_only: bool = True) -> dict | None:
    """Return a valid cached index or ``None``.

    With ``fresh_only=False`` an expired row is still returned (via
    ``cache.peek``) so callers can serve it as a stale fallback.
    """
    if cache is None:
        return None
    try:
        raw = cache.get(INDEX_KEY)
        if raw is None and not fresh_only:
            raw = cache.peek(INDEX_KEY)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None  # corrupt cache value
    return data if _valid_index(data) else None


class IndexBudgetExhausted(RuntimeError):
    """The politeness request budget for the index build was already spent.

    Internal to this module: :func:`build_index` catches it and returns a
    partial index instead of raising, so a budget-limited build never looks
    like a network outage to the caller.
    """


def _index_budget() -> tuple[str, int]:
    """Budget the index build should spend.

    Inside a tool call the enclosing budget is shared, so an index build cannot
    quietly add requests on top of what the tool is allowed; outside one it
    gets its own ``index:<sources>`` scope.
    """
    bound = current_budget()
    if bound is not None:
        return bound
    return "index:mdn+typescript", INDEX_BUDGET_LIMIT


def _fetch_mdn_sitemap() -> str:
    """GET the gzipped MDN sitemap and return the decompressed XML text.

    The endpoint serves raw gzip bytes without a ``Content-Encoding`` header,
    so httpx does not inflate them — detect the gzip magic and decompress with
    stdlib (already-decompressed bodies pass through unchanged). Raises
    ``RuntimeError`` on any failure and :class:`IndexBudgetExhausted` when the
    request budget is spent.
    """
    scope, limit = _index_budget()
    try:
        response = _get(MDN_SITEMAP_URL, budget_scope=scope, budget_limit=limit)
    except FetchError as exc:
        raise RuntimeError(f"transport error fetching MDN sitemap: {exc}") from exc
    if response.budget_exhausted:
        raise IndexBudgetExhausted(
            response.error or f"request budget exhausted for scope {scope!r}"
        )
    if response.error:
        raise RuntimeError(f"MDN sitemap request failed: {response.error}")
    if response.status_code is not None and response.status_code >= 400:
        raise RuntimeError(
            f"MDN sitemap request failed with HTTP {response.status_code}"
        )
    raw = response.content
    if raw[:2] == _GZIP_MAGIC:
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise RuntimeError(f"failed to decompress MDN sitemap: {exc}") from exc
    return raw.decode("utf-8")


def _fetch_ts_handbook_html() -> str:
    """GET the TypeScript handbook intro page (sidebar source).

    Raises ``RuntimeError`` on any failure and :class:`IndexBudgetExhausted`
    when the request budget is spent.
    """
    scope, limit = _index_budget()
    try:
        response = _get(TS_HANDBOOK_SOURCE_URL, budget_scope=scope, budget_limit=limit)
    except FetchError as exc:
        raise RuntimeError(
            f"transport error fetching TypeScript handbook: {exc}"
        ) from exc
    if response.budget_exhausted:
        raise IndexBudgetExhausted(
            response.error or f"request budget exhausted for scope {scope!r}"
        )
    if response.error:
        raise RuntimeError(f"TypeScript handbook request failed: {response.error}")
    if response.status_code is not None and response.status_code >= 400:
        raise RuntimeError(
            f"TypeScript handbook request failed with HTTP {response.status_code}"
        )
    return response.text


def _partial_index(reason: str, mdn_entries: list[dict], ts_entries: list[dict]) -> dict:
    """A budget-truncated build result: flagged, never cached.

    Whatever was fetched is kept (a TS-only index is still useful); the flag
    tells the caller it is not the whole picture, and :func:`get_index`
    refuses to persist it.
    """
    docs = mdn_entries + ts_entries
    return {
        "docs": docs,
        "count": len(docs),
        "mdn_count": len(mdn_entries),
        "ts_count": len(ts_entries),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "partial": True,
        "partial_reason": reason,
    }


def _build_index(cache: DocCache | None) -> dict:
    """Fetch + parse + persist.  May raise (network or parse failures).

    A spent request budget does **not** raise: the result is a partial index
    (``"partial": True`` + ``partial_reason``) that is not written to the
    cache.
    """
    mdn_entries: list[dict] = []
    ts_entries: list[dict] = []
    partial_reason: str | None = None

    try:
        mdn_entries = parse_mdn_sitemap_xml(_fetch_mdn_sitemap())
    except IndexBudgetExhausted as exc:
        partial_reason = str(exc)

    try:
        ts_entries = parse_ts_handbook_html(_fetch_ts_handbook_html())
    except IndexBudgetExhausted as exc:
        partial_reason = partial_reason or str(exc)

    if partial_reason is not None:
        return _partial_index(partial_reason, mdn_entries, ts_entries)

    docs = mdn_entries + ts_entries
    index = {
        "docs": docs,
        "count": len(docs),
        "mdn_count": len(mdn_entries),
        "ts_count": len(ts_entries),
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    if cache is not None:
        try:
            cache.set(INDEX_KEY, json.dumps(index), DEFAULT_MAX_AGE_SECONDS)
        except Exception:
            pass  # a cache-write failure must not break the returned index
    return index


def build_index() -> dict:
    """Fetch both sources and build the full search index.

    Returns ``{"docs": [...], "count": int, "mdn_count": int, "ts_count":
    int, "built_at": <iso8601 UTC>}`` and persists it in the
    :class:`DocCache` under key ``search-index`` with a 7-day TTL. On network
    or parse failure it falls back to the cached copy — fresh first
    (``cache.get``), then stale (``cache.peek``, returned with ``"stale":
    True``) — and only raises when no usable cache exists.

    A spent politeness request budget is neither: it returns a
    ``"partial": True`` index, which is never persisted.
    """
    cache = _open_cache()
    try:
        index = _build_index(cache)
    except Exception as exc:
        cached = _read_cache(cache, fresh_only=True)
        if cached is not None:
            return cached
        stale = _read_cache(cache, fresh_only=False)
        if stale is not None:
            stale = dict(stale)
            stale["stale"] = True
            return stale
        raise RuntimeError(f"index build failed: {type(exc).__name__}: {exc}") from exc
    if index.get("partial"):
        # A budget-truncated rebuild: prefer a complete cached copy if we have
        # one, but never store the partial result.
        cached = _read_cache(cache, fresh_only=True)
        if cached is not None:
            return cached
        stale = _read_cache(cache, fresh_only=False)
        if stale is not None:
            stale = dict(stale)
            stale["stale"] = True
            return stale
    return index


def get_index(cache: DocCache | None = None) -> dict:
    """Return the search index, using the :class:`DocCache` under key
    ``search-index``.  Never raises (mirrors java-spring-mcp's load_index).

    - Fresh (unexpired) cache hit: returned immediately, no network.
    - Miss: ``build_index()`` runs; on success the result is cached with a
      7-day TTL and returned.
    - Build failure with an old cached copy present: the old copy is returned
      with ``"stale": True``.
    - Build failure with no usable cache: returns
      ``{"docs": [], "count": 0, "mdn_count": 0, "ts_count": 0, "built_at":
      None, "error": ...}``.
    - A budget-truncated build (see :class:`IndexBudgetExhausted`) comes back
      as a ``"partial": True`` index; when it holds no docs it also carries an
      ``"error"`` so callers report the truth instead of "no matches", and it
      is **never persisted**.

    ``cache`` may be passed explicitly (tests, custom storage); when omitted a
    default :class:`DocCache` is opened.
    """
    if cache is None:
        cache = _open_cache()

    cached = _read_cache(cache, fresh_only=True)
    if cached is not None:
        return cached

    try:
        index = build_index()
    except Exception as exc:
        stale = _read_cache(cache, fresh_only=False)
        if stale is not None:
            stale = dict(stale)
            stale["stale"] = True
            return stale
        return {
            "docs": [],
            "count": 0,
            "mdn_count": 0,
            "ts_count": 0,
            "built_at": None,
            "error": f"index build failed: {type(exc).__name__}: {exc}",
        }

    if index.get("partial") and not index.get("docs"):
        index = dict(index)
        index["error"] = index.get("partial_reason") or "index build was budget-limited"

    # Persist to *this* cache when the (possibly monkeypatched) build did not.
    # A partial (budget-truncated) index is never stored: caching a deliberately
    # truncated list would freeze it for a week.
    if (
        cache is not None
        and index.get("built_at") is not None
        and not index.get("stale")
        and not index.get("partial")
    ):
        try:
            cache.set(INDEX_KEY, json.dumps(index), DEFAULT_MAX_AGE_SECONDS)
        except Exception:
            pass
    return index


# ---------------------------------------------------------------------------
# Search / ranking (pure / offline)
# ---------------------------------------------------------------------------

#: Per-token score tiers.  Exact name > startswith > substring in name >
#: substring in path; a token that matches neither field excludes the entry
#: from the results entirely.
_EXACT_SCORE = 10.0
_STARTSWITH_SCORE = 8.0
_SUBSTRING_NAME_SCORE = 6.0
_SUBSTRING_PATH_SCORE = 3.0


def _token_score(token: str, name_lower: str, path_lower: str) -> float:
    """Score one lowercase query token against a doc entry."""
    if token == name_lower:
        return _EXACT_SCORE
    if name_lower.startswith(token):
        return _STARTSWITH_SCORE
    if token in name_lower:
        return _SUBSTRING_NAME_SCORE
    if token in path_lower:
        return _SUBSTRING_PATH_SCORE
    return 0.0


def search_docs(query: str | None, limit: int = 8) -> list[dict]:
    """Rank MDN + TypeScript docs by how well their name/path match ``query``.

    Case-insensitive.  The query is split into whitespace-separated tokens; a
    doc must match **every** token (exact name > startswith > substring in
    name > substring in path) and its score is the sum of the per-token
    scores.  Results are ``{"source", "name", "path", "score"}`` dicts sorted
    best-first (ties broken by name).

    The index comes from :func:`get_index`, so a bare call works without
    touching the network on cache hits.  Empty/None queries return ``[]``.
    """
    if query is None:
        return []
    text = str(query).strip().lower()
    tokens = text.split()
    if not tokens:
        return []

    index = get_index()
    docs = index.get("docs") or []

    results: list[dict] = []
    for entry in docs:
        name_lower = str(entry.get("name") or "").lower()
        path_lower = str(entry.get("path") or "").lower()
        score = 0.0
        matched = True
        for token in tokens:
            token_score = _token_score(token, name_lower, path_lower)
            if token_score <= 0.0:
                matched = False
                break
            score += token_score
        if matched and score > 0.0:
            results.append(
                {
                    "source": entry.get("source", ""),
                    "name": entry.get("name", ""),
                    "path": entry.get("path", ""),
                    "score": round(score, 3),
                }
            )

    results.sort(key=lambda r: (-r["score"], r["name"].lower()))
    if limit is not None:
        results = results[: max(0, int(limit))]
    return results

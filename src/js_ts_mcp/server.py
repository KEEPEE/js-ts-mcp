"""MCP server exposing MDN Web Docs, TypeScript handbook and npm package info tools.

Tool set:

- ``health_check()`` — trivial liveness probe (server name + version).
- ``js_docs(identifier, topic=None, max_tokens=8000)`` — fetch one MDN or
  TypeScript handbook page as clean markdown. Identifier resolution order:

  1. ``mdn:<slug>`` (e.g. ``mdn:Web/JavaScript/Reference/Global_Objects/Array``)
     → fetched directly via the MDN docs path;
  2. ``ts:<page>`` (e.g. ``ts:intro``, ``ts:2/generics``) → fetched directly
     as a TypeScript handbook page;
  3. plain names (e.g. ``Promise``) → resolved through the search index:
     ``search_docs(name, limit=10)`` runs and only the top-score candidates
     are considered; a single distinct path wins outright, ties are broken by
     preferring mdn ``Web/JavaScript/Reference`` pages over mdn ``Web/API``
     pages over other mdn pages over typescript pages (within the winning
     tier a parent page beats its subpages, e.g. ``.../Promise`` over
     ``.../Promise/Promise``); still ambiguous or zero matches return an error
     dict suggesting ``js_search`` / the ``mdn:``/``ts:`` prefixes.

  Optional ``topic`` keeps only the matching heading section (plus title and
  description); ``max_tokens`` truncates the final markdown to a token budget
  (estimated as ``len(text) // 4``) cut at a line boundary.
- ``js_search(query, limit=8)`` — rank MDN + TypeScript handbook docs by
  name/path from the cached search index.
- ``npm_package(name, version=None)`` — npm registry metadata (+ readme when
  resolvable), optionally version-pinned, cached 1 day keyed by name+version.
- ``js_status()`` — real health check over the search index, the local cache
  and light GET probes of developer.mozilla.org, typescriptlang.org and
  registry.npmjs.org.  Reports the politeness layer's counters in a top-level
  ``politeness`` block — deliberately outside ``checks``, so it can never turn
  ``overall`` to ``degraded`` on its own.

Design rules (hard requirements):

- No tool ever calls or delegates to another tool; each one does its own work
  via :mod:`js_ts_mcp.fetchers`, :mod:`js_ts_mcp.search` and
  :class:`~js_ts_mcp.cache.DocCache`.
- Every tool returns a plain dict. On ANY failure the result is
  ``{"ok": False, "error": "<short message>", "suggestion": "<what to try
  instead>"}``; no exception ever escapes a tool and nothing recurses.
- Every network path is politeness-gated (see :mod:`js_ts_mcp.politeness`)
  and every tool binds one request budget with
  :func:`js_ts_mcp.fetchers.tool_budget`, so one tool call cannot make an
  unbounded number of requests (index build included).
- Fetched content is cached in :class:`~js_ts_mcp.cache.DocCache`:
  docs TTL 7 days (key ``doc:{source}:{path}``), npm metadata TTL 1 day
  (key ``npm:{name}[:{version}]``). Failures are never cached. Cache writes
  go through ``DocCache.set_value`` so they never clear the fetcher's
  conditional-GET columns.
"""

from __future__ import annotations

import functools
import json
import os
import re
import sqlite3
import urllib.parse

import httpx
from mcp.server.fastmcp import FastMCP

from . import __version__
from .cache import DocCache
from .fetchers import (
    FETCH_BUDGET_LIMIT,
    MAX_CACHED_BODY_BYTES,
    MDN_BASE_URL,
    NPM_REGISTRY_URL,
    TS_BASE_URL,
    current_budget,
    fetch_mdn_doc,
    fetch_npm_package,
    fetch_ts_page,
    get_politeness,
    tool_budget,
)
from .politeness import default_robots_db_path
from .search import get_index, search_docs

mcp = FastMCP("js-ts")

#: Cache TTLs (seconds). Fetched docs: one week; npm metadata: one day.
DOC_TTL = 604800
NPM_META_TTL = 86400

#: Identifier prefixes for the explicit (non-index) js_docs forms.
MDN_PREFIX = "mdn:"
TS_PREFIX = "ts:"

#: Light GET probes for js_status (name, url).
_ENDPOINT_PROBES = (
    ("mdn_org", f"{MDN_BASE_URL}/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array"),
    ("typescriptlang_org", f"{TS_BASE_URL}/docs/handbook/intro.html"),
    ("npm_registry", f"{NPM_REGISTRY_URL}/left-pad/latest"),
)

#: Request budget one ``js_status`` call may spend.  Since A8 F3 a probe on a
#: host whose robots.txt is not cached costs **two** units (robots.txt + the
#: probe), and the call also rebuilds the search index when it is stale.
#: Measured live (A10 smoke, cold cache): index rebuild 4 (MDN robots +
#: sitemap + TS robots + handbook page) + probes 4 (MDN page 1 + TS page 1 +
#: npm robots 1 + npm page 1 — the rebuild already cached the robots facts of
#: the first two hosts) = **8 units**; warm = **3**.  The limit is 12 so a
#: probe can never come back as ``error: request budget exhausted`` — that
#: would make ``overall`` "degraded" for a purely internal reason (A7 §4.5
#: warning), and the 4 units of reserve cover a ``429`` retry or a redirect
#: hop on top of the cold path.
STATUS_BUDGET_LIMIT = 12

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


# ---------------------------------------------------------------------------
# Small shared helpers (not exposed as tools)
# ---------------------------------------------------------------------------

def _cache() -> DocCache | None:
    """A fresh DocCache, or ``None`` when the cache is unavailable."""
    try:
        return DocCache()
    except Exception:
        return None


def _cache_get(key: str) -> dict | None:
    """Return the cached doc dict for ``key`` (fresh, valid JSON), else None."""
    cache = _cache()
    if cache is None:
        return None
    try:
        raw = cache.get(key)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("markdown"), str):
        return data
    return None


def _cache_set(key: str, value: dict, ttl_seconds: int) -> None:
    """Store a doc dict under ``key``; cache-write failures are ignored.

    ``set_value`` (not ``set``) so a row that also carries the fetcher's
    conditional-GET columns (``etag`` / ``last_modified`` / ``body`` for the
    same URL) keeps them — clearing them would silently disable revalidation.
    """
    cache = _cache()
    if cache is None:
        return
    try:
        cache.set_value(key, json.dumps(value), ttl_seconds)
    except Exception:
        pass


def _doc_url(source: str, path: str) -> str:
    """Source URL for a doc (mirrors the fetchers' URL construction)."""
    quoted = urllib.parse.quote(path, safe="/")
    if source == "mdn":
        return f"{MDN_BASE_URL}/en-US/docs/{quoted}"
    if path.startswith("docs/"):
        return f"{TS_BASE_URL}/{quoted}.html"
    return f"{TS_BASE_URL}/docs/handbook/{quoted}.html"


def _candidate_priority(entry: dict) -> int:
    """Tie-break tier for a top-score search candidate (lower wins).

    mdn ``Web/JavaScript/Reference`` > mdn ``Web/API`` > other mdn > typescript.
    """
    source = str(entry.get("source") or "").lower()
    path = str(entry.get("path") or "")
    if source == "mdn" and path.startswith("Web/JavaScript/Reference"):
        return 0
    if source == "mdn" and path.startswith("Web/API"):
        return 1
    if source == "mdn":
        return 2
    return 3


def _resolve_plain_name(ident: str) -> tuple[list[dict], dict]:
    """Top-score search-index candidates for a plain name, plus the index.

    Runs ``search_docs(ident, limit=10)`` and keeps only the distinct paths
    that share the best score (deduplicated by path). Returns ``([], index)``
    when nothing matches or the index is empty/unavailable.
    """
    index = get_index()  # never raises; may carry "error" + empty docs
    results = search_docs(ident, limit=10)
    if not results:
        return [], index
    top_score = float(results[0].get("score") or 0.0)
    candidates: list[dict] = []
    seen: set[str] = set()
    for entry in results:
        if float(entry.get("score") or 0.0) < top_score:
            break  # sorted best-first; everything below is off the top score
        path = str(entry.get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        candidates.append(entry)
    return candidates, index


def _pick_candidate(candidates: list[dict]) -> dict | None:
    """Pick the single winner among top-score candidates, else ``None``.

    Ties are broken by :func:`_candidate_priority` (mdn reference > mdn API >
    other mdn > typescript). Within the winning tier a parent page — a path
    that is a strict prefix of every other candidate's path — beats its
    subpages (e.g. ``Web/JavaScript/Reference/Global_Objects/Promise`` over
    its ``.../Promise/Promise`` constructor subpage). Anything else is still
    ambiguous and returns ``None`` so the caller can report an error.
    """
    if not candidates:
        return None
    best = min(_candidate_priority(c) for c in candidates)
    winners = [c for c in candidates if _candidate_priority(c) == best]
    if len(winners) == 1:
        return winners[0]
    paths = [str(c.get("path") or "") for c in winners]
    for winner, path in zip(winners, paths):
        if all(
            other.startswith(path + "/")
            for other in paths
            if other != path
        ):
            return winner
    return None


def _extract_topic_section(markdown: str, topic: str) -> tuple[str | None, list[str]]:
    """Extract the first heading section whose text contains ``topic``.

    Returns ``(section_markdown_or_None, all_heading_texts)``.  The section
    runs from the matched heading up to (excluding) the next heading of the
    same or a higher level.
    """
    lines = markdown.splitlines()
    headings: list[str] = []
    target: tuple[int, int] | None = None  # (line index, heading level)
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if not match:
            continue
        text = match.group(2).strip()
        headings.append(text)
        if target is None and topic.lower() in text.lower():
            target = (i, len(match.group(1)))
    if target is None:
        return None, headings
    idx, level = target
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        match = _HEADING_RE.match(lines[j].strip())
        if match and len(match.group(1)) <= level:
            end = j
            break
    return "\n".join(lines[idx:end]).rstrip(), headings


def _title_and_description(markdown: str) -> str:
    """Everything before the first level-2 heading (title + description)."""
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if match and len(match.group(1)) == 2:
            return "\n".join(lines[:i]).strip()
    # No level-2 heading: keep only the title line to avoid duplicating body.
    return lines[0].strip() if lines else ""


def _apply_topic(markdown: str, topic: str | None) -> tuple[str, str | None]:
    """Apply optional topic filtering; returns (content, note_or_None)."""
    if not topic:
        return markdown, None
    section, headings = _extract_topic_section(markdown, str(topic))
    if section is None:
        listing = ", ".join(headings[:25]) or "(none)"
        return markdown, (
            f"no section matching {topic!r}; available headings: {listing}"
        )
    preface = _title_and_description(markdown)
    content = f"{preface}\n\n{section}".strip() if preface else section
    return content, None


def _truncate_markdown(markdown: str, max_tokens: int | None) -> tuple[str, bool]:
    """Truncate to ~max_tokens tokens (len//4 estimate) at a line boundary."""
    if not max_tokens or max_tokens <= 0:
        return markdown, False
    total = len(markdown) // 4
    if total <= int(max_tokens):
        return markdown, False
    budget = int(max_tokens) * 4
    cut = markdown[:budget]
    newline = cut.rfind("\n")
    if newline > 0:
        cut = cut[:newline]
    shown = len(cut) // 4
    note = f"\n\n[truncated: showing ~{shown} of ~{total} estimated tokens]"
    return cut.rstrip() + note, True


def _doc_payload(
    *,
    identifier: str,
    url: str,
    title: str | None,
    source: str,
    markdown: str,
    topic: str | None,
    max_tokens: int,
    cached: bool,
    note: str | None = None,
) -> dict:
    """Assemble the js_docs success shape (topic filter + truncation)."""
    content, topic_note = _apply_topic(markdown, topic)
    if topic_note and note:
        note = f"{note}; {topic_note}"
    elif topic_note:
        note = topic_note
    content, truncated = _truncate_markdown(content, max_tokens)
    payload: dict = {
        "ok": True,
        "identifier": identifier,
        "url": url,
        "source": source,
        "markdown": content,
        "truncated": truncated,
        "cached": cached,
    }
    if title:
        payload["title"] = title
    if note:
        payload["note"] = note
    return payload


def _error_from(fetch_result: dict, context: str) -> dict:
    """Convert a fetcher failure dict into the tool-level error shape."""
    out: dict = {"ok": False, "error": f"{context}: {fetch_result.get('error', 'unknown failure')}"}
    if fetch_result.get("suggestion"):
        out["suggestion"] = fetch_result["suggestion"]
    else:
        out["suggestion"] = "retry, or use js_search() to find the right page"
    return out


def _probe_endpoint(url: str) -> dict:
    """Light GET probe through the politeness layer; never raises.

    The previous version made a bare ``httpx`` call and then a ``curl``
    subprocess fallback.  The curl path bypassed robots.txt, throttle, retry
    and budget entirely, and a health check that hammers a site is not a fix
    for politeness — it is the hole in it.  It is gone: one request per probe,
    through the same layer as every other request.

    The probe spends the *tool call's* request budget when one is bound, so
    ``js_status`` cannot quietly add requests on top of the budget the tool
    already has.
    """
    try:
        budget_kwargs: dict = {}
        bound = current_budget()
        if bound is not None:
            budget_kwargs = {"budget_scope": bound[0], "budget_limit": bound[1]}
        with httpx.Client(
            timeout=httpx.Timeout(8.0, connect=5.0), follow_redirects=True
        ) as client:
            response = get_politeness().get(client, url, **budget_kwargs)
    except Exception as exc:
        return {
            "status": "error",
            "http_status": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if response.error:
        return {
            "status": "error",
            "http_status": response.status_code,
            "error": response.error,
        }
    if response.status_code is not None and response.status_code < 400:
        return {"status": "ok", "http_status": response.status_code}
    return {
        "status": "error",
        "http_status": response.status_code,
        "error": f"HTTP {response.status_code}",
    }


def _robots_rows() -> int | None:
    """How many robots.txt records the politeness SQLite cache holds (read-only).

    ``None`` when the robots DB does not exist yet (or the layer is running on
    its in-memory fallback — politeness still applies, it just forgets across
    restarts); that is not an error.
    """
    path = default_robots_db_path()
    if not os.path.exists(path):
        return None
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM robots").fetchone()[0])
    except Exception:
        return None


def _politeness_report() -> dict:
    """The politeness layer's counters, for ``js_status``.

    Deliberately NOT one of the ``checks``: the layer's own state must never
    flip ``overall`` to ``degraded``/``error``.  A failure here degrades to a
    one-line error, never an exception.
    """
    try:
        stats = get_politeness().stats()
        return {
            "status": "ok",
            "disabled": bool(stats.get("disabled", False)),
            "requests": int(stats.get("requests", 0)),
            # A8 F3: ``requests`` counts content requests only; the robots
            # attempts are the other half of what went on the wire.  F1 gives
            # the robots waits their own counters, F2 the redirect hops, F4 the
            # challenge detections (this repo supplies no detector, so those
            # two stay 0 — they are reported so a future one is visible).
            "robots_requests": int(stats.get("robots_requests", 0)),
            "robots_rows": _robots_rows(),
            "robots_fetches": int(stats.get("robots_fetches", 0)),
            "robots_cache_hits": int(stats.get("robots_cache_hits", 0)),
            "robots_negative": int(stats.get("robots_negative", 0)),
            "robots_refreshed_unchanged": int(stats.get("robots_refreshed_unchanged", 0)),
            "blocked_by_robots": int(stats.get("blocked_by_robots", 0)),
            "throttle_waits": int(stats.get("throttle_waits", 0)),
            "throttle_sleep_s": round(float(stats.get("throttle_sleep_s", 0.0)), 3),
            "robots_throttle_waits": int(stats.get("robots_throttle_waits", 0)),
            "robots_throttle_sleep_s": round(
                float(stats.get("robots_throttle_sleep_s", 0.0)), 3
            ),
            "redirect_hops": int(stats.get("redirect_hops", 0)),
            "challenge_detected": int(stats.get("challenge_detected", 0)),
            "challenge_retries": int(stats.get("challenge_retries", 0)),
            "host_delays": stats.get("hosts", {}),
            "budgets": stats.get("budgets", {}),
            "budget_denied": int(stats.get("budget_denied", 0)),
            "conditional": int(stats.get("conditional", 0)),
            "conditional_skipped": int(stats.get("conditional_skipped", 0)),
            "revalidated_304": int(stats.get("revalidated_304", 0)),
            "retries_429": int(stats.get("retries_429", 0)),
            "retry_after_honoured": int(stats.get("retry_after_honoured", 0)),
            "retries_transport": int(stats.get("retries_transport", 0)),
            "stalls": int(stats.get("stalls", 0)),
            # js-ts-mcp specifics: the in-memory body store and the robots DB
            # location, because this repo pulls multi-megabyte artifacts
            # (MDN sitemap ~1.9 MB decoded, npm packuments up to ~7 MB).
            "cached_bodies": int(stats.get("cached_bodies", 0)),
            "max_cached_bytes": MAX_CACHED_BODY_BYTES,
            "robots_db": default_robots_db_path(),
        }
    except Exception as exc:
        return {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}


def budgeted(name: str, limit: int = FETCH_BUDGET_LIMIT):
    """Bind one request budget to a whole tool call.

    Applied *under* ``@mcp.tool()``; ``functools.wraps`` keeps the tool's
    signature, annotations and docstring, which is what FastMCP advertises.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with tool_budget(name, limit):
                return fn(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def health_check() -> dict:
    """Check that the server is up and report its version."""
    return {"status": "ok", "server": "js-ts-mcp", "version": __version__}


@mcp.tool()
@budgeted("js_docs")
def js_docs(identifier: str, topic: str | None = None, max_tokens: int = 8000) -> dict:
    """Fetch an MDN Web Docs or TypeScript handbook page as markdown.

    ``identifier`` resolution order:
      1. "mdn:<slug>" like "mdn:Web/JavaScript/Reference/Global_Objects/Array"
         → fetched directly from the MDN docs path;
      2. "ts:<page>" like "ts:intro" or "ts:2/generics" → fetched directly as
         a TypeScript handbook page;
      3. plain names like "Promise" → resolved through the search index: only
         the top-score candidates are considered; a single distinct path wins,
         ties prefer mdn Web/JavaScript/Reference pages over mdn Web/API pages
         over other mdn pages over typescript pages (within the winning tier a
         parent page beats its subpages); ambiguous or missing matches return
         an error dict suggesting js_search().

    ``topic`` (a heading like "Methods") keeps only that heading section plus
    title/description; if no such section exists the full content is returned
    with a "note" listing the available headings. ``max_tokens`` truncates the
    final markdown to roughly that many tokens (estimated as len(text)//4),
    cut at a line boundary, and sets "truncated": true when applied.

    On failure returns {"ok": False, "error", "suggestion"} — try js_search()
    to find the exact page, or use the mdn:/ts: prefix.
    """
    try:
        return _js_docs_impl(identifier, topic, max_tokens)
    except Exception as exc:  # last-resort guard: never let an exception escape
        return {
            "ok": False,
            "error": f"unexpected failure: {type(exc).__name__}: {exc}",
            "suggestion": "retry, or use js_search() to find the right page",
        }


def _js_docs_impl(identifier: str, topic: str | None, max_tokens: int) -> dict:
    ident = str(identifier or "").strip()
    if not ident:
        return {
            "ok": False,
            "error": "identifier is required",
            "suggestion": (
                "pass e.g. 'mdn:Web/JavaScript/Reference/Global_Objects/Array', "
                "'ts:intro' or a plain name like 'Promise'"
            ),
        }

    # --- identifier resolution ------------------------------------------------
    if ident.startswith(MDN_PREFIX):
        source, path = "mdn", ident[len(MDN_PREFIX):].strip()
    elif ident.startswith(TS_PREFIX):
        source, path = "typescript", ident[len(TS_PREFIX):].strip()
    else:
        candidates, index = _resolve_plain_name(ident)
        if not candidates:
            detail = ""
            if index.get("error"):
                detail = f" (search index unavailable: {index['error']})"
            return {
                "ok": False,
                "error": f"no MDN/TypeScript page matching '{ident}' found in the search index{detail}",
                "suggestion": "use js_search to find the exact page, or use the mdn:/ts: prefix",
            }
        winner = _pick_candidate(candidates)
        if winner is None:
            alt_list = ", ".join(
                f"{c.get('source')}:{c.get('path')}" for c in candidates[:5]
            )
            return {
                "ok": False,
                "error": f"ambiguous name '{ident}': {len(candidates)} pages match at the top score ({alt_list})",
                "suggestion": "use js_search to find the exact page, or use the mdn:/ts: prefix",
            }
        source = str(winner.get("source") or "")
        path = str(winner.get("path") or "")

    # --- fetch (with DocCache) --------------------------------------------------
    key = f"doc:{source}:{path}"
    cached_doc = _cache_get(key)
    if cached_doc is not None:
        return _doc_payload(
            identifier=ident,
            url=cached_doc.get("url") or _doc_url(source, path),
            title=cached_doc.get("title"),
            source=source,
            markdown=cached_doc["markdown"],
            topic=topic,
            max_tokens=max_tokens,
            cached=True,
        )
    result = fetch_mdn_doc(path) if source == "mdn" else fetch_ts_page(path)
    if not result.get("ok"):
        return _error_from(result, f"'{ident}' fetch failed")
    _cache_set(key, {
        "title": result.get("title"),
        "markdown": result.get("markdown"),
        "url": result.get("url"),
    }, DOC_TTL)
    return _doc_payload(
        identifier=ident,
        url=result.get("url") or _doc_url(source, path),
        title=result.get("title"),
        source=source,
        markdown=result.get("markdown", ""),
        topic=topic,
        max_tokens=max_tokens,
        cached=False,
    )


@mcp.tool()
@budgeted("js_search")
def js_search(query: str, limit: int = 8) -> dict:
    """Search MDN Web Docs and the TypeScript handbook by name/path.

    Ranks entries of the cached search index (every query token must match;
    exact name > startswith > substring in name > substring in path).
    Returns {"ok": True, "query", "results": [{source, name, path, score}],
    "index_count", "stale"}. When the index is unavailable returns
    {"ok": False, "error", "suggestion"} — retry later.
    """
    try:
        index = get_index()
        docs = index.get("docs") or []
        if not docs and index.get("error"):
            return {
                "ok": False,
                "error": f"search index unavailable: {index['error']}",
                "suggestion": "retry js_search() later (the index rebuilds automatically)",
            }
        results = search_docs(query, limit=limit)
        return {
            "ok": True,
            "query": query,
            "results": results,
            "index_count": int(index.get("count") or len(docs)),
            "stale": bool(index.get("stale")),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"search failed: {type(exc).__name__}: {exc}",
            "suggestion": "retry js_search() later",
        }


@mcp.tool()
@budgeted("npm_package")
def npm_package(name: str, version: str | None = None) -> dict:
    """Look up a package on the npm registry (metadata + readme when resolvable).

    Returns name, resolved version, description, license, homepage, repository
    URL, keywords, engines, dependencies and the readme as markdown. When
    ``version`` is given the lookup is pinned to that release; otherwise the
    latest version is used. Metadata is cached for 1 day keyed by name+version;
    failures are never cached.

    On failure returns {"ok": False, "error", "suggestion"}.
    """
    try:
        pkg = str(name or "").strip()
        if not pkg:
            return {
                "ok": False,
                "error": "package name is required",
                "suggestion": "pass e.g. name='express' (optionally version)",
            }
        ver = str(version).strip() if version else None

        key = f"npm:{pkg}" + (f":{ver}" if ver else "")
        cache = _cache()
        if cache is not None:
            try:
                raw = cache.get(key)
            except Exception:
                raw = None
            if raw is not None:
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    data = None
                if isinstance(data, dict):
                    out = dict(data)
                    out["cached"] = True
                    return out

        result = fetch_npm_package(pkg, ver)
        if not result.get("ok"):
            return _error_from(result, "npm lookup failed")

        data: dict = {
            "ok": True,
            "name": result.get("name"),
            "version": result.get("version"),
            "description": result.get("description"),
            "license": result.get("license"),
            "homepage": result.get("homepage"),
            "repository_url": result.get("repository_url"),
            "keywords": result.get("keywords"),
            "engines": result.get("engines"),
            "dependencies": result.get("dependencies"),
            "readme_markdown": result.get("readme_markdown"),
            "url": result.get("url"),
        }
        if result.get("note"):
            data["note"] = result["note"]
        _cache_set(key, data, NPM_META_TTL)
        data["cached"] = False
        return data
    except Exception as exc:
        return {
            "ok": False,
            "error": f"npm lookup failed: {type(exc).__name__}: {exc}",
            "suggestion": "retry, or verify the package name",
        }


@mcp.tool()
@budgeted("js_status", limit=STATUS_BUDGET_LIMIT)
def js_status() -> dict:
    """Real health check: search index, local cache and upstream endpoints.

    The ``cache`` check gains ``read_only: true`` plus ``read_only_reason``
    when the database cannot be written (P5).  It stays an "ok" check on
    purpose: a cache that only reads is a degraded optimisation, not a sick
    server, so ``overall`` does not change.

    Probes developer.mozilla.org, typescriptlang.org and registry.npmjs.org
    with a light GET (8s timeout each, through the politeness layer — no curl
    subprocess). Never raises; returns
    {"server", "version", "checks": {...}, "politeness": {...}, "overall":
    "ok"|"degraded"|"error"} where overall is "error" when the search index is
    unavailable, "degraded" when any check fails, and "ok" otherwise.  The
    "politeness" block holds the layer's counters (robots cache state,
    throttle delays, budget use, conditional-GET hits) and is not a check: it
    never changes "overall".
    """
    try:
        checks: dict = {}

        index = get_index()
        entry_count = len(index.get("docs") or [])
        si: dict = {
            "status": "ok" if entry_count else "error",
            "entries": entry_count,
            "mdn_count": int(index.get("mdn_count") or 0),
            "ts_count": int(index.get("ts_count") or 0),
            "built_at": index.get("built_at"),
            "stale": bool(index.get("stale")),
        }
        if index.get("error"):
            si["error"] = str(index["error"])
        checks["search_index"] = si

        try:
            cache = DocCache()
            stats = cache.stats()
            checks["cache"] = {
                "status": "ok",
                "entries": int(stats.get("entries", 0)),
                "expired": int(stats.get("expired", 0)),
            }
            if cache.read_only:
                # P5: a read-only cache is a degraded optimisation, not a
                # broken server.  It is announced here, and the check keeps
                # ``status: "ok"`` on purpose so ``overall`` is unchanged.
                checks["cache"]["read_only"] = True
                checks["cache"]["read_only_reason"] = cache.read_only_reason
        except Exception as exc:
            checks["cache"] = {
                "status": "error",
                "entries": 0,
                "expired": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }

        for name, url in _ENDPOINT_PROBES:
            checks[name] = _probe_endpoint(url)

        if checks["search_index"]["status"] == "error":
            overall = "error"
        elif any(c.get("status") != "ok" for c in checks.values()):
            overall = "degraded"
        else:
            overall = "ok"
        return {
            "server": "js-ts-mcp",
            "version": __version__,
            "checks": checks,
            # Outside "checks" on purpose: the layer's own counters are
            # diagnostic and must never flip "overall".
            "politeness": _politeness_report(),
            "overall": overall,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"status check failed: {type(exc).__name__}: {exc}",
            "suggestion": "retry js_status()",
        }


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

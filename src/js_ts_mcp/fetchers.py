"""Fetchers for MDN Web Docs, the TypeScript handbook and the npm registry.

Public fetch functions (each returns a dict and NEVER raises):

- :func:`fetch_mdn_doc(slug)` — one MDN page as markdown.
- :func:`fetch_ts_page(page)` — one TypeScript handbook page as markdown.
- :func:`fetch_npm_package(name, version=None)` — npm package metadata, with
  the README filled from GitHub raw when the registry does not carry one (see
  :func:`fetch_github_readme`).
- :func:`fetch_github_readme(owner_repo)` — one repository README as text.

Pure parser functions used by the fetchers after the HTTP layer (and unit
tested offline against ``tests/fixtures``):

- :func:`parse_mdn_html(html, url)`
- :func:`parse_ts_html(html, url)`
- :func:`parse_npm_packument(data)` — full packument shape.
- :func:`parse_npm_version_doc(data)` — single-version doc shape.

Design notes:
- No network access at import time; all fetching happens inside the public
  functions.
- MDN's v1 JSON API (``/api/v1/docs``) is dead; HTML pages are parsed
  directly from ``<main id="content">``.
- Every failure path returns ``{"ok": False, "error": ...}`` (plus a
  ``"suggestion"`` where useful) instead of raising.

Politeness
----------
Every request goes through :mod:`js_ts_mcp.politeness`: robots.txt rules with
an RFC 9309 wildcard matcher, per-host throttle, ``429``/``503``/``504``
handling with ``Retry-After``, stall detection, conditional GET
(``ETag`` / ``Last-Modified`` → ``304``) and a per-tool-call request budget.
The layer never raises and never changes the return shape.  Opt out with
``JS_TS_MCP_POLITENESS_DISABLED=1`` — at the user's own risk.

Three facts of this repo drive the settings:

* ``registry.npmjs.org/robots.txt`` is a **trap** — it answers HTTP 200 with
  ``application/json``, the packument of the npm package literally named
  ``robots.txt`` (7 462 bytes measured by the audit).  It is not a robots
  policy; the layer's lenient RFC 9309 parser yields zero groups for it, so
  the host is treated as "no rules" and the request proceeds.
* A popular packument is huge: ``registry.npmjs.org/react`` is 7 016 423 bytes
  decoded (A2 cold measurement).  Bodies above
  :data:`MAX_CACHED_BODY_BYTES` are therefore not kept in memory and not
  written to the revalidation store — the body is simply not cached.
* ``raw.githubusercontent.com/robots.txt`` answers **HTTP 404** with the 14-byte
  body ``404: Not Found`` (measured 2026-10-07).  No robots file means no
  rules, so the README fallback may fetch raw files from that host — but it is
  still allowlisted explicitly, so a new host can never be reached by accident.
"""

from __future__ import annotations

import re
import threading
import urllib.parse
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

from .cache import DocCache
from .politeness import Politeness, PoliteResponse, default_robots_db_path

__all__ = [
    "fetch_mdn_doc",
    "fetch_ts_page",
    "fetch_npm_package",
    "fetch_github_readme",
    "parse_mdn_html",
    "parse_ts_html",
    "parse_npm_packument",
    "parse_npm_version_doc",
    "get_politeness",
    "set_politeness",
    "new_politeness",
    "tool_budget",
    "current_budget",
]

# ---------------------------------------------------------------------------
# Constants / HTTP layer
# ---------------------------------------------------------------------------

MDN_BASE_URL = "https://developer.mozilla.org"
TS_BASE_URL = "https://www.typescriptlang.org"
NPM_REGISTRY_URL = "https://registry.npmjs.org"
NPM_PACKAGE_URL = "https://www.npmjs.com/package"

#: Raw file host used only by the README fallback (see
#: :func:`fetch_github_readme`).  ``HEAD`` is the mutable ref GitHub accepts in
#: a raw URL, so no default-branch lookup is needed: measured 2026-10-07,
#: ``raw.githubusercontent.com/expressjs/express/HEAD/Readme.md`` → 200,
#: 10 371 bytes.
GITHUB_RAW_BASE_URL = "https://raw.githubusercontent.com"

#: Honest, contactable UA (A1 §6 / A2 §4.3).  This module used to send a
#: Chrome 126 spoof: it buys nothing, it defeats ``User-agent:``-specific
#: robots rules (a robots file can only match a token it can see) and it hides
#: who is knocking.
USER_AGENT = (
    "js-ts-mcp/0.2 (+https://github.com/KEEPEE/js-ts-mcp)"
)

#: connect 5 s / read 20 s / write+pool 60 s.  The module used to carry three
#: per-endpoint timeouts (20 s docs, 60 s packument, 30 s sitemap); the
#: politeness layer sets one timeout policy per request
#: (``client.build_request(timeout=…)`` — ``httpx.Client.send()`` in 0.28 has
#: no ``timeout`` argument), so the knobs collapse into this tuple.  ``read``
#: is per-chunk, which is what a 7 MB packument actually needs.
TIMEOUTS = (5.0, 20.0, 60.0)

#: The only hosts this module ever contacts.  Handed to the politeness layer as
#: an allowlist so an unexpected redirect or a malformed identifier can never
#: turn a docs lookup into a request to somebody else's site.
#: ``www.npmjs.com`` is deliberately absent: it only appears inside the
#: ``url`` field we report, never in a request (and its robots.txt is a
#: Cloudflare 403 challenge anyway).
#: ``raw.githubusercontent.com`` joined with the README fallback: its robots.txt
#: is an HTTP 404 (measured 2026-10-07, body ``404: Not Found``) so the host has
#: no rules, but it is listed explicitly anyway — a host this code fetches must
#: appear here, otherwise the layer refuses it and the fallback silently fails.
ALLOWED_HOSTS = frozenset(
    {
        "developer.mozilla.org",
        "www.typescriptlang.org",
        "registry.npmjs.org",
        "raw.githubusercontent.com",
    }
)

#: Hard cap on requests one MCP tool call may make (A2 §4.3).  Since A8 F3 a
#: unit is **one request on the wire**, so each cold host's robots.txt fetch
#: pays too.  Measured live (A10 smoke): cold ``js_docs("Promise")`` = MDN
#: robots + sitemap + TS robots + handbook page + the MDN page itself = **5**
#: units (the index rebuild is what makes this call expensive); cold
#: ``js_docs("ts:intro")`` = robots + page = **2**.
#: ``npm_package(x)`` with the README fallback, measured 2026-10-07: cold
#: ``zod`` = npm robots + packument + raw-host robots + the 22-byte pointer +
#: the document it names = **5**; the same call warm (ETag revalidation, robots
#: cached) = **3**; a package whose README is nowhere = 2 + 1 + 6 candidates =
#: **9**, which the cap cuts off at 7 and the caller reports as a ``note``
#: rather than an error.  7 keeps every legitimate cold path inside the cap
#: with reserve for a ``429`` retry or a redirect hop; the README fallback is
#: deliberately bounded by the same cap instead of raising it.
FETCH_BUDGET_LIMIT = 7

#: Bodies up to this size are kept for conditional GET (in the layer's memory
#: cache and in the ``DocCache`` revalidation row).  2 MB is the audit's
#: recommendation (A3 §5.7) and it sits exactly between this repo's two big
#: artifacts: the decoded MDN sitemap (1 936 390 B — kept, so its revalidation
#: can answer a ``304``) and the react packument (7 016 423 B — not kept).
MAX_CACHED_BODY_BYTES = 2 * 1024 * 1024

#: TTL for the raw body + validators the fetcher keeps for conditional GET.
#: Matches the server's docs TTL so a revalidation window always exists.
REVALIDATION_TTL_SECONDS = 7 * 24 * 3600

# ---------------------------------------------------------------------------
# README fallback settings (npm packuments no longer carry a usable readme)
# ---------------------------------------------------------------------------

#: Candidate README paths, tried in this order.  The order is not arbitrary:
#: ``README.md`` is the GitHub convention, ``Readme.md`` is what express
#: actually publishes (measured: ``HEAD/README.md`` → 404, ``HEAD/Readme.md`` →
#: 200 / 10 371 B), the case variants cover the rest of the common spellings,
#: ``README.markdown`` and ``README.rst`` are the pre-markdown leftovers, and
#: ``docs/README.md`` catches repos that move the document out of the root.
README_CANDIDATES = (
    "README.md",
    "Readme.md",
    "readme.md",
    "README.markdown",
    "README.rst",
    "docs/README.md",
)

#: A body shorter than this is not a README worth returning — it is a stub, a
#: redirect page (GitHub's raw 404 body is 14 bytes) or a placeholder.
MIN_README_BYTES = 80

#: A body at or below this size, made of exactly one line that looks like a
#: relative document path, is a **pointer**: monorepos park a one-liner at the
#: root and keep the real document in a package directory.  Measured live:
#: ``colinhacks/zod/HEAD/README.md`` is 22 bytes whose entire content is
#: ``packages/zod/README.md``, and the real document is 7 304 bytes at
#: ``colinhacks/zod/HEAD/packages/zod/README.md``.
MAX_POINTER_CHARS = 300

#: How many pointer files we are willing to chase before giving up on this
#: candidate.  Two hops is enough for every layout observed in the wild and it
#: is the loop guard: a pointer that points at itself can never spin.
MAX_POINTER_HOPS = 2

#: Extensions a pointer may name.  Anything else (an image, a script, a URL) is
#: not a document to follow.
_POINTER_EXTENSIONS = ("md", "markdown", "rst")

#: Stable prefixes of the layer's own error messages (``Politeness.get``);
#: used to tell a robots block, an allowlist refusal and a spent budget apart
#: from a transport failure, which keeps raising :class:`FetchError` exactly
#: as before.
_BUDGET_ERROR_PREFIX = "request budget exhausted"
_ALLOWLIST_ERROR_MARK = "is not in ALLOWED_HOSTS"


def _is_policy_refusal(response: PoliteResponse) -> bool:
    """True when the layer *refused* the request (not a network failure).

    A refusal is not exceptional: it is our own rule (robots.txt, the host
    allowlist, or a spent budget), so it is reported verbatim as
    ``_Response.error`` instead of raising.
    """
    error = response.error or ""
    return (
        response.blocked_by_robots
        # A8 F4: a challenge page is our own verdict, not a network failure.
        # This repo ships no detector (MDN / typescriptlang / the npm registry
        # answer 200/302/403, never a JS challenge), so this stays False today —
        # but if a detector is ever wired, its refusal must be reported
        # verbatim instead of raising FetchError.
        or bool(getattr(response, "bot_challenge", False))
        or error.startswith(_BUDGET_ERROR_PREFIX)
        or _ALLOWLIST_ERROR_MARK in error
    )


class FetchError(Exception):
    """Transport-level failure (connection error, timeout, ...) after retries."""


# ---------------------------------------------------------------------------
# Politeness layer (process-wide singleton)
# ---------------------------------------------------------------------------

_politeness: Politeness | None = None
_politeness_lock = threading.Lock()


def new_politeness(cache_path: str | None = None) -> Politeness:
    """Build a politeness layer with this repo's settings.

    Kept separate from :func:`get_politeness` so tests can assert the actual
    production settings (notably :data:`MAX_CACHED_BODY_BYTES`) without
    touching the singleton.
    """
    return Politeness(
        USER_AGENT,
        cache_path=cache_path,
        timeouts=TIMEOUTS,
        allowed_hosts=ALLOWED_HOSTS,
        max_cached_bytes=MAX_CACHED_BODY_BYTES,
    )


def get_politeness() -> Politeness:
    """Process-wide politeness layer (created on first use).

    The robots cache is a SQLite file next to ``cache.db`` so it survives
    restarts (1 robots request per host per 7 days, not per call).  If that
    directory is not writable the layer falls back to a per-process in-memory
    cache — politeness still applies, it just forgets across restarts.  A
    missing cache must never take a tool down.  Tests replace the instance
    through :func:`set_politeness`.
    """
    global _politeness
    if _politeness is None:
        with _politeness_lock:
            if _politeness is None:
                try:
                    _politeness = new_politeness(default_robots_db_path())
                except Exception:
                    _politeness = new_politeness(None)
    return _politeness


def set_politeness(politeness: Politeness | None) -> None:
    """Replace (or with ``None`` reset) the process-wide layer. Test seam."""
    global _politeness
    with _politeness_lock:
        _politeness = politeness


#: Budget bound by the enclosing :func:`tool_budget` (``(scope, limit)``).
#: A ContextVar, not a module global, so a tool call running in a worker
#: thread can never spend another call's budget.
_current_budget: ContextVar[tuple[str, int] | None] = ContextVar(
    "js_ts_mcp_request_budget", default=None
)


@contextmanager
def tool_budget(name: str, limit: int = FETCH_BUDGET_LIMIT):
    """Bind one request budget to everything fetched inside this block.

    A cold ``js_docs("Promise")`` reaches the network three times (sitemap,
    handbook sidebar, then the page itself) and an unresolvable name used to
    be retried candidate after candidate. The budget caps that: one tool call,
    at most ``limit`` requests, index build included. The scope name is stable
    per tool and reset on entry, so ``Politeness._budgets`` cannot grow
    without bound.
    """
    scope = f"tool:{name}"
    try:
        get_politeness().reset_budget(scope)
    except Exception:
        pass  # a broken budget layer must not stop a tool
    token = _current_budget.set((scope, int(limit)))
    try:
        yield scope
    finally:
        _current_budget.reset(token)


def current_budget() -> tuple[str, int] | None:
    """The budget bound by the enclosing :func:`tool_budget`, if any."""
    return _current_budget.get()


# ---------------------------------------------------------------------------
# Revalidation data (validators + raw body) live in DocCache
# ---------------------------------------------------------------------------

def _doc_cache() -> DocCache | None:
    """DocCache for revalidation data, or ``None`` if it cannot be opened.

    A cache problem must never turn into a fetch failure.
    """
    try:
        return DocCache()
    except Exception:
        return None


def _revalidation_for(url: str) -> tuple[dict | None, bytes | None]:
    """``(validators, cached_body)`` stored for ``url`` by an earlier fetch.

    Both are returned together or not at all: a conditional GET without a body
    to serve would throw away the ``304`` (A3 §5.4).
    """
    cache = _doc_cache()
    if cache is None:
        return None, None
    try:
        entry = cache.get_entry(url, include_expired=True)
    except Exception:
        return None, None
    if not entry or not entry.get("body"):
        return None, None
    validators = {
        k: v
        for k, v in (
            ("etag", entry.get("etag")),
            ("last_modified", entry.get("last_modified")),
        )
        if v
    }
    if not validators:
        return None, None
    body = entry["body"]
    if isinstance(body, str):
        body = body.encode("utf-8", "replace")
    return validators, body


def _remember_revalidation(url: str, validators: dict, body: bytes) -> None:
    """Persist validators + raw body so the next fetch can send a conditional GET.

    Bodies above :data:`MAX_CACHED_BODY_BYTES` are **not stored at all** — a
    7 MB packument would bloat the cache DB and buy nothing, because without a
    stored body the layer refuses to send conditional headers anyway.
    """
    if not validators or body is None or len(body) > MAX_CACHED_BODY_BYTES:
        return
    cache = _doc_cache()
    if cache is None:
        return
    try:
        cache.set_validators(
            url,
            etag=validators.get("etag"),
            last_modified=validators.get("last_modified"),
            body=body,
            ttl_seconds=REVALIDATION_TTL_SECONDS,
        )
    except Exception:
        pass  # never break a successful fetch over cache bookkeeping


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

class _Response:
    """Minimal response shim shared by every fetch path in this module.

    It keeps the handful of ``httpx.Response`` attributes the callers use
    (``status_code``, ``content``, ``text``, ``url``, ``json()``) and adds the
    layer's truth: ``from_cache`` / ``blocked_by_robots`` / ``budget_exhausted``
    / ``error`` / ``validators``.  ``status_code`` is *truthful*: ``304`` with
    ``from_cache=True`` when a conditional GET revalidated a stored body.
    """

    def __init__(
        self,
        status_code: int | None,
        content: bytes,
        url: str,
        *,
        error: str | None = None,
        from_cache: bool = False,
        blocked_by_robots: bool = False,
        budget_exhausted: bool = False,
        validators: dict | None = None,
        headers: dict | None = None,
        bot_challenge: bool = False,
    ):
        self.status_code = status_code
        self.content = content or b""
        self.url = url
        self.error = error
        self.from_cache = from_cache
        self.blocked_by_robots = blocked_by_robots
        self.budget_exhausted = budget_exhausted
        self.validators = validators or {}
        self.headers = headers or {}
        #: A8 F4 — set when the layer's optional ``challenge_detector`` judged
        #: the body an anti-bot page.  No detector is wired in this repo today;
        #: the flag exists so the flag never gets lost in translation.
        self.bot_challenge = bot_challenge

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        import json as _json

        return _json.loads(self.text)


def _client() -> httpx.Client:
    """The one place an ``httpx.Client`` is built in this module.

    The politeness layer sets its own honest UA per request; the client header
    is only a default for the robots.txt fetch the layer performs with the
    same client.
    """
    return httpx.Client(
        timeout=httpx.Timeout(TIMEOUTS[2], connect=TIMEOUTS[0], read=TIMEOUTS[1]),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _budget_for(url: str) -> tuple[str, int]:
    """Budget a request made outside any :func:`tool_budget` should spend."""
    bound = current_budget()
    if bound is not None:
        return bound
    host = (urlparse(url).netloc or "").lower()
    scope = f"fetch:{host}"
    try:
        get_politeness().reset_budget(scope)
    except Exception:
        pass
    return scope, FETCH_BUDGET_LIMIT


def _get(
    url: str,
    *,
    budget_scope: str | None = None,
    budget_limit: int = FETCH_BUDGET_LIMIT,
    revalidate: bool = True,
) -> _Response:
    """GET ``url`` through the politeness layer.

    Order inside the layer: robots → budget → throttle → conditional GET →
    send → ``429``/``503``/``504`` with ``Retry-After`` → stall detection.
    The old version of this function built a fresh browser-spoofing client and
    called ``client.get`` twice back-to-back with no delay and no robots check
    at all.

    Raises :class:`FetchError` only when no response could be obtained because
    of the transport (connection error, timeout) — the contract callers
    already rely on.  A robots block or a spent budget is NOT an exception: it
    comes back as ``_Response.error`` so the caller can report it verbatim.

    Unless ``revalidate`` is false, the validators and raw body of an earlier
    response for the same URL are read from :class:`DocCache` and offered as
    ``If-None-Match`` / ``If-Modified-Since``, and a fresh ``200`` writes them
    back.  Both are passed together or not at all (A3 §5.4).
    """
    if budget_scope is None:
        budget_scope, budget_limit = _budget_for(url)

    validators: dict | None = None
    cached_body: bytes | None = None
    if revalidate:
        validators, cached_body = _revalidation_for(url)

    with _client() as client:
        response = get_politeness().get(
            client,
            url,
            validators=validators,
            cached_body=cached_body,
            budget_scope=budget_scope,
            budget_limit=budget_limit,
        )

    final_url = response.url or url
    error = response.error

    if error and not _is_policy_refusal(response):
        # Transport failure (or an internal layer failure): same contract the
        # callers had before this layer existed — raise, do not fabricate a
        # response.
        raise FetchError(f"request to {url} failed: {error}")

    if response.blocked_by_robots or error:
        return _Response(
            response.status_code,
            b"",
            final_url,
            error=error or f"request to {url} failed",
            blocked_by_robots=response.blocked_by_robots,
            budget_exhausted=bool(error and error.startswith(_BUDGET_ERROR_PREFIX)),
            validators=response.validators or {},
            bot_challenge=bool(getattr(response, "bot_challenge", False)),
        )

    if response.from_cache:
        # 304: the body is the one we already had, nothing was re-downloaded.
        # The response carries *fresh* validators, so write them back — keeping
        # the ones we sent would offer a stale ETag on the next revalidation.
        if revalidate:
            _remember_revalidation(url, response.validators or {}, response.content)
        return _Response(
            response.status_code,
            response.content,
            final_url,
            from_cache=True,
            validators=response.validators or {},
            headers=response.headers,
        )

    if response.status_code is None:
        return _Response(None, b"", final_url, error=f"no response from {url}")

    if 200 <= response.status_code < 300 and revalidate:
        _remember_revalidation(url, response.validators or {}, response.content)

    return _Response(
        response.status_code,
        response.content,
        final_url,
        validators=response.validators or {},
        headers=response.headers,
    )


# ---------------------------------------------------------------------------
# Shared markdown helpers
# ---------------------------------------------------------------------------

def _code_language(pre_tag) -> str:
    """Best-effort language tag for a ``<pre>`` block (markdownify callback).

    Handles the two conventions seen in the wild:
    - MDN: ``<pre class="brush: js">`` (highlight.js brush naming)
    - TypeScript handbook / generic: ``<code class="language-ts">``
    """
    try:
        code = pre_tag.find("code") if pre_tag is not None else None
        if code is not None:
            for cls in code.get("class") or []:
                if isinstance(cls, str) and cls.startswith("language-"):
                    return cls[len("language-"):]
        for cls in pre_tag.get("class") or []:
            if isinstance(cls, str) and cls.startswith("brush:"):
                lang = cls[len("brush:"):].strip()
                if lang:
                    return lang
    except Exception:  # pragma: no cover - callback must never break parsing
        pass
    return ""


def _make_converter() -> MarkdownConverter:
    return MarkdownConverter(heading_style="ATX", code_language_callback=_code_language)


def _tidy_markdown(markdown: str) -> str:
    """Strip trailing whitespace per line and collapse 3+ blank lines to one."""
    out: list[str] = []
    blanks = 0
    for line in markdown.splitlines():
        line = line.rstrip()
        if not line:
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        out.append(line)
    return "\n".join(out).strip() + "\n"


def _absolutize_links(root, base_origin: str) -> None:
    """Rewrite relative ``href`` attributes under ``root`` to absolute URLs.

    In-page anchors (``#...``) are left untouched.
    """
    for a in root.find_all("a", href=True):
        href = a.get("href")
        if not isinstance(href, str):
            continue
        if href.startswith("/"):
            a["href"] = base_origin + href
        elif href.startswith("//"):
            a["href"] = "https:" + href


def _fail(error: str) -> dict:
    return {"ok": False, "error": error}


# ---------------------------------------------------------------------------
# MDN Web Docs
# ---------------------------------------------------------------------------

_MDN_TITLE_SUFFIX = re.compile(r"\s*\|\s*MDN\s*$", re.IGNORECASE)


def parse_mdn_html(html: str, url: str) -> dict:
    """Parse a real MDN page (HTML) into ``{"ok", "url", "title", "markdown"}``.

    Title comes from ``<title>`` with the trailing ``" | MDN"`` suffix
    stripped; markdown is rendered from ``<main id="content">`` after
    decomposing nav/TOC/feedback junk. Never raises.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
        main = soup.find("main", id="content") or soup.find("main")
        if main is None:
            return _fail('could not locate <main id="content"> in MDN page')

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag is not None else ""
        title = _MDN_TITLE_SUFFIX.sub("", title).strip()
        if not title:
            return _fail("could not determine page title from <title>")

        # Decompose site chrome: nav, sidebars (incl. the "In this article"
        # TOC box), header/footer, scripts, templates and MDN web components.
        for tag in main.find_all(
            ["nav", "aside", "header", "footer", "script", "style", "template"]
        ):
            tag.decompose()
        for el in main.find_all(True):
            if (
                el.name
                and el.name.startswith("mdn-")
                # only top-level web components (children are removed with them)
                and not any(p.name.startswith("mdn-") for p in el.parents)
            ):
                el.decompose()
        for el in main.find_all("details", class_="baseline-indicator"):
            el.decompose()
        # "Help improve MDN" feedback section + GitHub footer links.
        for el in main.find_all(class_="article-footer"):
            el.decompose()

        _absolutize_links(main, MDN_BASE_URL)
        markdown = _tidy_markdown(_make_converter().convert_soup(main))
        if len(markdown.strip()) < 100:
            return _fail("page content is empty after parsing")
        return {"ok": True, "url": url, "title": title, "markdown": markdown}
    except Exception as exc:
        return _fail(f"failed to parse MDN page: {exc}")


def fetch_mdn_doc(slug: str) -> dict:
    """Fetch one MDN page by docs slug and return it as markdown.

    ``slug`` is the docs path without locale prefix, e.g.
    ``"Web/JavaScript/Reference/Global_Objects/Array"`` (a leading ``/`` is
    accepted). Returns ``{"ok": True, "url", "title", "slug", "markdown"}``
    on success and ``{"ok": False, "error"}`` on failure — never raises.
    """
    try:
        slug = (slug or "").strip().lstrip("/")
        if not slug:
            return _fail("empty MDN slug")
        url = f"{MDN_BASE_URL}/en-US/docs/{urllib.parse.quote(slug, safe='/')}"
        try:
            response = _get(url)
        except FetchError as exc:
            return _fail(f"failed to fetch MDN page: {exc}")
        if response.error:
            return _fail(response.error)
        if response.status_code == 404:
            return _fail(f"page not found on MDN: {slug}")
        if response.status_code >= 400:
            return _fail(f"MDN returned HTTP {response.status_code} for {slug}")
        result = parse_mdn_html(response.text, response.url)
        if result.get("ok"):
            result["slug"] = slug
        return result
    except Exception as exc:
        return _fail(f"failed to fetch MDN page: {exc}")


# ---------------------------------------------------------------------------
# TypeScript handbook
# ---------------------------------------------------------------------------

def parse_ts_html(html: str, url: str) -> dict:
    """Parse a typescriptlang.org handbook page into ``{"ok", "url", "title", "markdown"}``.

    Title comes from ``<h1>`` (fallback ``<title>``); markdown is rendered
    from the article area of ``<main>`` after decomposing sidebar/TOC and
    navigation-card junk. Never raises.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
        main = soup.find("main")
        if main is None:
            return _fail("could not locate <main> in TypeScript page")

        h1 = main.find("h1") or soup.find("h1")
        title = h1.get_text(strip=True) if h1 is not None else ""
        if not title:  # some pages have no <h1>; fall back to <title>
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag is not None else ""
        if not title:
            return _fail("could not determine page title from <h1>/<title>")

        root = main.find("div", id="handbook-content") or main
        # Decompose site chrome: handbook sidebar nav, "On this page" TOC
        # (aside), the "Next: ..." card and open-source banner
        # (div.whitespace-tight), feedback popup, scripts.
        for tag in root.find_all(
            [
                "nav",
                "aside",
                "header",
                "footer",
                "script",
                "style",
                "noscript",
                "button",
                "template",
            ]
        ):
            tag.decompose()
        for el in root.find_all("div", class_="whitespace-tight"):
            el.decompose()
        for el in root.find_all(id="page-helpful-popup"):
            el.decompose()

        _absolutize_links(root, TS_BASE_URL)
        markdown = _tidy_markdown(_make_converter().convert_soup(root))
        if len(markdown.strip()) < 100:
            return _fail("page content is empty after parsing")
        return {"ok": True, "url": url, "title": title, "markdown": markdown}
    except Exception as exc:
        return _fail(f"failed to parse TypeScript page: {exc}")


def fetch_ts_page(page: str) -> dict:
    """Fetch one TypeScript handbook page and return it as markdown.

    ``page`` is the handbook page name without ``.html``, e.g. ``"intro"``
    or ``"typescript-from-scratch"``. A value already starting with
    ``"docs/"`` is treated as a full path under typescriptlang.org. Returns
    ``{"ok": True, "url", "title", "page", "markdown"}`` on success and
    ``{"ok": False, "error"}`` on failure — never raises.
    """
    try:
        page = (page or "").strip().removesuffix(".html")
        if not page:
            return _fail("empty TypeScript page name")
        if page.startswith("docs/"):
            url = f"{TS_BASE_URL}/{urllib.parse.quote(page, safe='/')}.html"
        else:
            url = (
                f"{TS_BASE_URL}/docs/handbook/"
                f"{urllib.parse.quote(page, safe='/')}.html"
            )
        try:
            response = _get(url)
        except FetchError as exc:
            return _fail(f"failed to fetch TypeScript page: {exc}")
        if response.error:
            return _fail(response.error)
        if response.status_code == 404:
            return _fail(f"page not found on typescriptlang.org: {page}")
        if response.status_code >= 400:
            return _fail(
                f"typescriptlang.org returned HTTP {response.status_code} for {page}"
            )
        result = parse_ts_html(response.text, response.url)
        if result.get("ok"):
            result["page"] = page
        return result
    except Exception as exc:
        return _fail(f"failed to fetch TypeScript page: {exc}")


# ---------------------------------------------------------------------------
# GitHub README fallback
# ---------------------------------------------------------------------------

#: ``https://``, ``http://``, ``git+https://``, ``git://``, ``git+ssh://`` (with
#: or without the ``git@`` user) and ``www.``.  ``.git`` and a trailing slash
#: are optional.  Every shape here is a value npm really serves: measured
#: 2026-10-07, ``zod`` → ``git+https://github.com/colinhacks/zod.git``,
#: ``express`` → ``git+https://github.com/expressjs/express.git``,
#: ``left-pad`` → ``git+ssh://git@github.com/stevemao/left-pad.git``.
_GITHUB_URL_RE = re.compile(
    r"^(?:git\+)?(?:(?:https?|git|ssh)://)(?:[^/@\s]+@)?"
    r"(?:www\.)?github\.com[:/]"
    r"(?P<owner>[^/?#\s]+)/(?P<repo>[^/?#\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)

#: The scheme-less shorthand npm's own tooling accepts (``gh:`` is its alias).
_GITHUB_SHORTHAND_RE = re.compile(
    r"^(?:github|gh):(?P<owner>[^/?#\s]+)/(?P<repo>[^/?#\s]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def _github_owner_repo(repository_url: str | None) -> str | None:
    """Turn a normalised npm ``repository.url`` into an ``owner/repo`` pair.

    Returns ``None`` when there is nothing safe to fetch.  That is deliberate
    for non-GitHub hosts: gitlab.com and bitbucket.org have their own raw-file
    layouts *and* their own robots policies, and this module only ever contacts
    hosts whose behaviour has been measured (see :data:`ALLOWED_HOSTS`).  A
    repository URL that is not a plain GitHub project URL is therefore not
    guessed at — the caller reports the miss in a ``note`` instead.
    """
    if not repository_url:
        return None
    url = str(repository_url).strip()
    if not url:
        return None
    # npm likes to store "https://github.com/owner/repo#readme"; the fragment is
    # not part of the path and would otherwise break the match.
    url = url.split("#", 1)[0].split("?", 1)[0]
    match = _GITHUB_URL_RE.match(url) or _GITHUB_SHORTHAND_RE.match(url)
    if match is None:
        return None
    owner, repo = match.group("owner").strip("/"), match.group("repo").strip("/")
    if not owner or not repo:
        return None
    return f"{owner}/{repo}"


def _readme_pointer_target(body: str) -> str | None:
    """Return the relative document path when *body* is a pointer file.

    A pointer is what a monorepo leaves at the repository root: one short line
    naming the real document.  Measured live 2026-10-07 —
    ``colinhacks/zod/HEAD/README.md`` is 22 bytes and its entire content is
    ``packages/zod/README.md``, while the real README is 7 304 bytes at
    ``colinhacks/zod/HEAD/packages/zod/README.md``.  Following it is the only
    way to get a README for the most-downloaded validation package on npm.

    Only a single line that is a relative path to a markdown/rst document is
    followed.  Absolute URLs, rooted paths and any ``..`` segment are refused:
    the fetch is scoped to ``{owner}/{repo}/HEAD/`` and that scope is not a
    suggestion.
    """
    stripped = body.strip()
    if not stripped or len(stripped) > MAX_POINTER_CHARS:
        return None
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    target = lines[0]
    if "://" in target or target.startswith(("/", "\\")):
        return None
    if any(ch.isspace() for ch in target):
        # "see docs/README.md for details" is prose, not a path.  A pointer is
        # the whole file, so it has nothing around the path.
        return None
    # "./docs/README.md" is a legitimate relative path; ".." is not.  The fetch
    # is scoped to {owner}/{repo}/HEAD/ and that scope is not a suggestion.
    while target.startswith("./"):
        target = target[2:]
    if any(segment in ("", "..") for segment in target.split("/")):
        return None
    if not target.lower().endswith(tuple(_POINTER_EXTENSIONS)):
        return None
    return target


def _readme_is_substantive(body: str) -> bool:
    """True when *body* is long enough to plausibly be a README."""
    return len(body.encode("utf-8", errors="replace")) > MIN_README_BYTES


def _fetch_readme_file(base: str, repo: str, path: str, tried: list[str]) -> dict:
    """Fetch one candidate README, following a pointer chain if there is one.

    ``base`` is always ``{GITHUB_RAW_BASE_URL}/{owner}/{repo}/HEAD``; every URL
    this function can build stays under it.

    A ``304`` counts as a hit: :func:`_get` offers the stored ETag and hands
    back the body it already had (``from_cache=True``), which is the same
    document.  Treating ``304`` as a miss would make the *second* call for a
    package fail with "no README found" — measured live: the first
    ``fetch_npm_package("zod")`` fetched the pointer, the next one revalidated
    it.
    """
    current = path
    for _hop in range(MAX_POINTER_HOPS + 1):
        tried.append(current)
        response = _get(f"{base}/{current}")
        if response.budget_exhausted:
            # Our own cap stopped this, not GitHub.  Say so, and let the caller
            # stop trying further candidates.
            return {
                "ok": False,
                "budget_exhausted": True,
                "error": response.error or "request budget exhausted",
            }
        status = response.status_code
        # 2xx is a hit.  So is a 304 that came back with the body :func:`_get`
        # had stored — same document, nothing re-downloaded.
        if not (200 <= (status or 0) < 300 or (status == 304 and response.content)):
            return {"ok": False, "error": f"HTTP {status} for {current}"}
        body = response.text
        pointer = _readme_pointer_target(body)
        if pointer is not None:
            current = pointer
            continue
        if not _readme_is_substantive(body):
            size = len(body.encode("utf-8", errors="replace"))
            return {"ok": False, "error": f"{current} is only {size} bytes"}
        result: dict = {
            "ok": True,
            "markdown": body,
            "source": f"github:{repo}@HEAD/{current}",
        }
        if current.lower().endswith(".rst"):
            # This repo has no RST→markdown converter (python-docs-mcp has
            # ``_rst_to_markdown``; this one never needed one).  Returning the
            # raw body is only honest if the caller is told what it is.
            result["note"] = (
                "the README is reStructuredText and this server has no "
                "RST-to-markdown converter: readme_markdown is the raw .rst body"
            )
        return result
    return {"ok": False, "error": f"pointer chain at {path} exceeded {MAX_POINTER_HOPS} hops"}


def fetch_github_readme(owner_repo: str) -> dict:
    """Fetch a GitHub repository's README as text.

    Why this exists: npm no longer puts the README in the packument.  Measured
    live 2026-10-07, ``registry.npmjs.org/zod`` and ``registry.npmjs.org/express``
    both *do* have a ``readme`` key, but its value is the empty string ``""``
    (``left-pad``, a 2016-era package, still carries 871 bytes).  A
    single-version doc — what a version-pinned lookup fetches — has no
    ``readme`` key at all.  So ``readme_markdown`` came back ``null`` for
    essentially every modern package, silently.  The registry stays the metadata
    source; only the document is taken from GitHub, where the author wrote it.

    Returns ``{"ok": True, "markdown": str, "source": str}`` where ``source``
    names the file that produced the text (e.g.
    ``"github:colinhacks/zod@HEAD/packages/zod/README.md"``), or
    ``{"ok": False, "error": str, "tried": [...]}``.  Never raises.

    Budget: every candidate and every pointer hop is a real request and pays a
    unit of the calling tool's budget (:data:`FETCH_BUDGET_LIMIT`).  When the
    budget is spent the result carries ``"budget_exhausted": True`` and the
    candidate loop stops — a spent budget degrades the README to a ``note``, it
    never turns a working metadata lookup into an error.
    """
    repo = str(owner_repo or "").strip().strip("/")
    parts = repo.split("/")
    if len(parts) != 2 or any(p in ("", ".", "..") for p in parts):
        return {"ok": False, "error": f"not an owner/repo pair: {owner_repo!r}", "tried": []}
    base = f"{GITHUB_RAW_BASE_URL}/{repo}/HEAD"
    tried: list[str] = []
    try:
        for candidate in README_CANDIDATES:
            outcome = _fetch_readme_file(base, repo, candidate, tried)
            if outcome.get("ok"):
                return outcome
            if outcome.get("budget_exhausted"):
                return {
                    "ok": False,
                    "error": outcome["error"],
                    "budget_exhausted": True,
                    "tried": tried,
                }
        return {
            "ok": False,
            "error": f"no README found for {repo} (tried {', '.join(tried)})",
            "tried": tried,
        }
    except Exception as exc:  # transport failure, bad URL, anything else
        return {
            "ok": False,
            "error": f"README fetch failed: {type(exc).__name__}: {exc}",
            "tried": tried,
        }


def _append_note(result: dict, text: str) -> None:
    """Append to ``result["note"]`` instead of overwriting an existing one."""
    existing = result.get("note")
    result["note"] = f"{existing}; {text}" if existing else text


def _no_readme_reason(repository_url: str | None) -> str:
    """Why the fallback could not even try, in one honest sentence."""
    if not repository_url:
        return (
            "npm serves no readme for this package and it declares no repository "
            "URL, so there is nowhere to fetch one from"
        )
    return (
        f"npm serves no readme for this package and its repository "
        f"({repository_url}) is not a GitHub project URL, so no README was fetched "
        "(only github.com raw files are requested)"
    )


def _attach_github_readme(result: dict) -> dict:
    """Fill ``readme_markdown`` from GitHub when the registry carried nothing.

    The contract this enforces: ``readme_markdown: null`` is never silent.  A
    README that was fetched says where it came from (``readme_source``); a
    README that was not fetched says why (``note``).
    """
    if result.get("readme_markdown"):
        return result
    repository_url = result.get("repository_url")
    owner_repo = _github_owner_repo(repository_url)
    if owner_repo is None:
        _append_note(result, _no_readme_reason(repository_url))
        return result
    fetched = fetch_github_readme(owner_repo)
    if not fetched.get("ok"):
        _append_note(result, f"no README available for {owner_repo}: {fetched['error']}")
        return result
    result["readme_markdown"] = fetched["markdown"]
    result["readme_source"] = fetched["source"]
    if fetched.get("note"):
        _append_note(result, fetched["note"])
    return result


# ---------------------------------------------------------------------------
# npm registry
# ---------------------------------------------------------------------------

def _normalize_license(license_value) -> str | None:
    """Normalize an npm ``license`` field (string or ``{"type": ...}`` dict)."""
    if isinstance(license_value, str):
        return license_value.strip() or None
    if isinstance(license_value, dict):
        for key in ("type", "name"):
            value = license_value.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _repo_url(repository) -> str | None:
    """Extract a repository URL from an npm ``repository`` field."""
    if isinstance(repository, dict):
        url = repository.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    elif isinstance(repository, str) and repository.strip():
        return repository.strip()
    return None


def _text(value) -> str | None:
    """Return a non-empty stripped string, else ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse_npm_version_doc(data: dict) -> dict:
    """Parse a single-version npm doc (``/{name}/latest`` or ``/{name}/{version}``).

    Returns the success payload WITHOUT a ``url`` key (the fetcher adds it).
    Single-version docs carry no readme, so ``readme_markdown`` is ``None``.
    Never raises.
    """
    try:
        if not isinstance(data, dict):
            return _fail("npm version doc is not a JSON object")
        name = data.get("name")
        version = data.get("version")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(version, str)
            or not version
        ):
            return _fail("malformed npm version doc (missing name/version)")
        keywords = data.get("keywords")
        engines = data.get("engines")
        dependencies = data.get("dependencies")
        return {
            "ok": True,
            "name": name,
            "version": version,
            "description": _text(data.get("description")),
            "license": _normalize_license(data.get("license")),
            "homepage": _text(data.get("homepage")),
            "repository_url": _repo_url(data.get("repository")),
            "keywords": keywords if isinstance(keywords, list) else None,
            "engines": engines if isinstance(engines, dict) else None,
            "dependencies": dependencies if isinstance(dependencies, dict) else {},
            "readme_markdown": _text(data.get("readme")),
        }
    except Exception as exc:
        return _fail(f"failed to parse npm version doc: {exc}")


def _semver_key(version: str):
    """Coarse semver sort key (numeric major/minor/patch, prerelease ignored)."""
    core = version.lstrip("v").split("-", 1)[0].split("+", 1)[0]
    parts: list[int] = []
    for piece in core.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def parse_npm_packument(data: dict) -> dict:
    """Parse a full npm packument (``GET /{name}``).

    Uses the top-level name/description/license/homepage/repository/keywords/
    readme, resolves the latest version from ``dist-tags.latest`` and takes
    engines/dependencies from that version's doc. Returns the success payload
    WITHOUT a ``url`` key (the fetcher adds it). Never raises.
    """
    try:
        if not isinstance(data, dict):
            return _fail("npm packument is not a JSON object")
        name = data.get("name")
        versions = data.get("versions")
        if not isinstance(name, str) or not name:
            return _fail("malformed npm packument (missing name)")
        if not isinstance(versions, dict) or not versions:
            return _fail("malformed npm packument (missing versions)")

        dist_tags = data.get("dist-tags")
        latest_tag = dist_tags.get("latest") if isinstance(dist_tags, dict) else None
        version_doc = (
            versions.get(latest_tag)
            if isinstance(latest_tag, str) and isinstance(versions.get(latest_tag), dict)
            else None
        )
        if version_doc is None:
            # Defensive fallback: highest semver among all listed versions.
            version_doc = max(
                (v for v in versions.values() if isinstance(v, dict)),
                key=lambda v: _semver_key(str(v.get("version", "0"))),
                default=None,
            )
        if not isinstance(version_doc, dict):
            return _fail("could not resolve a latest version from packument")

        keywords = data.get("keywords")
        engines = version_doc.get("engines")
        dependencies = version_doc.get("dependencies")
        return {
            "ok": True,
            "name": name,
            "version": _text(version_doc.get("version")) or (latest_tag or ""),
            "description": _text(data.get("description")),
            "license": _normalize_license(data.get("license")),
            "homepage": _text(data.get("homepage")),
            "repository_url": _repo_url(data.get("repository")),
            "keywords": keywords if isinstance(keywords, list) else None,
            "engines": engines if isinstance(engines, dict) else None,
            "dependencies": dependencies if isinstance(dependencies, dict) else {},
            "readme_markdown": _text(data.get("readme")),
        }
    except Exception as exc:
        return _fail(f"failed to parse npm packument: {exc}")


def _npm_latest_fallback(name: str, encoded_name: str, reason: str) -> dict:
    """Fall back to the ``/{name}/latest`` endpoint when the packument fails.

    The single-version doc has no readme at all, so the README is taken from
    GitHub (:func:`_attach_github_readme`) and the result still carries an
    explanatory ``note`` about where it came from.
    """
    try:
        response = _get(f"{NPM_REGISTRY_URL}/{encoded_name}/latest")
    except FetchError as exc:
        return _fail(f"failed to fetch npm package: {exc}")
    if response.error:
        # A robots block or a spent budget must not be papered over with a
        # second request to a different endpoint.
        return _fail(response.error)
    if response.status_code == 404:
        return {
            "ok": False,
            "error": "package not found on npm",
            "suggestion": "check the package name",
        }
    if response.status_code >= 400:
        return _fail(f"npm registry returned HTTP {response.status_code}")
    try:
        result = parse_npm_version_doc(response.json())
    except ValueError as exc:
        return _fail(f"failed to fetch npm package: invalid JSON ({exc})")
    if not result.get("ok"):
        return result
    result["readme_markdown"] = None
    result["note"] = f"readme unavailable (packument fetch failed: {reason})"
    result["url"] = f"{NPM_PACKAGE_URL}/{name}"
    return _attach_github_readme(result)


def fetch_npm_package(name: str, version: str | None = None) -> dict:
    """Fetch npm package metadata, plus the README from GitHub when npm has none.

    With ``version``: uses the single-version doc ``/{name}/{version}``.
    Without: fetches the full packument; if that fails but the package exists,
    falls back to ``/{name}/latest`` with a ``note``. Returns
    ``{"ok": False, "error": "package not found on npm", "suggestion": ...}``
    for unknown packages — never raises.

    README: when the registry's ``readme`` is missing or empty (the normal case
    since npm stopped embedding readmes — see :func:`fetch_github_readme`), the
    document is fetched from ``raw.githubusercontent.com`` and the result gains
    ``readme_source``.  When that cannot be done the result keeps
    ``readme_markdown=None`` **and** says why in ``note`` — a silent null is the
    bug this path exists to fix.

    The politeness layer caps how many requests one call may make, so the
    fallback is only taken for an HTTP/parse failure of the packument — never
    for a robots block or a spent budget.  The README fallback shares the same
    budget and stops at the cap instead of failing the lookup.
    """
    try:
        name = (name or "").strip()
        if not name:
            return _fail("empty package name")
        encoded_name = urllib.parse.quote(name, safe="@/")

        if version is not None and str(version).strip():
            version = str(version).strip()
            url = f"{NPM_REGISTRY_URL}/{encoded_name}/{urllib.parse.quote(version, safe='')}"
            try:
                response = _get(url)
            except FetchError as exc:
                return _fail(f"failed to fetch npm package: {exc}")
            if response.error:
                return _fail(response.error)
            if response.status_code == 404:
                return {
                    "ok": False,
                    "error": "package not found on npm",
                    "suggestion": "check the package name and version",
                }
            if response.status_code >= 400:
                return _fail(f"npm registry returned HTTP {response.status_code}")
            try:
                result = parse_npm_version_doc(response.json())
            except ValueError as exc:
                return _fail(f"failed to fetch npm package: invalid JSON ({exc})")
            if result.get("ok"):
                result["url"] = f"{NPM_PACKAGE_URL}/{name}/{version}"
                # A single-version doc never carries a readme, so this is the
                # normal path for a version-pinned lookup.
                result = _attach_github_readme(result)
            return result

        # No version given: full packument. It is the largest artifact this
        # repo pulls (react: 7 MB decoded), so it is deliberately not kept in
        # the revalidation store — see MAX_CACHED_BODY_BYTES.
        try:
            response = _get(f"{NPM_REGISTRY_URL}/{encoded_name}")
        except FetchError as exc:
            return _npm_latest_fallback(name, encoded_name, str(exc))
        if response.error:
            return _fail(response.error)
        if response.status_code == 404:
            return {
                "ok": False,
                "error": "package not found on npm",
                "suggestion": "check the package name",
            }
        if response.status_code >= 400:
            fallback = _npm_latest_fallback(
                name, encoded_name, f"HTTP {response.status_code}"
            )
            if fallback.get("ok"):
                return fallback
            fallback_error = fallback.get("error") or ""
            if fallback_error.startswith(_BUDGET_ERROR_PREFIX) or "robots.txt" in fallback_error:
                # Our own limit stopped the retry — say that instead of
                # blaming the registry for a request that was never made.
                return _fail(fallback_error)
            return _fail(f"npm registry returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            return _npm_latest_fallback(name, encoded_name, f"invalid JSON: {exc}")
        result = parse_npm_packument(data)
        if not result.get("ok"):
            fallback = _npm_latest_fallback(name, encoded_name, result["error"])
            return fallback if fallback.get("ok") else result
        result["url"] = f"{NPM_PACKAGE_URL}/{name}"
        return _attach_github_readme(result)
    except Exception as exc:
        return _fail(f"failed to fetch npm package: {exc}")

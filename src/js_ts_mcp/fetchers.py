"""Fetchers for MDN Web Docs, the TypeScript handbook and the npm registry.

Public fetch functions (each returns a dict and NEVER raises):

- :func:`fetch_mdn_doc(slug)` — one MDN page as markdown.
- :func:`fetch_ts_page(page)` — one TypeScript handbook page as markdown.
- :func:`fetch_npm_package(name, version=None)` — npm package metadata.

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
"""

from __future__ import annotations

import re
import urllib.parse

import httpx
from bs4 import BeautifulSoup
from markdownify import MarkdownConverter

__all__ = [
    "fetch_mdn_doc",
    "fetch_ts_page",
    "fetch_npm_package",
    "parse_mdn_html",
    "parse_ts_html",
    "parse_npm_packument",
    "parse_npm_version_doc",
]

# ---------------------------------------------------------------------------
# Constants / HTTP layer
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

MDN_BASE_URL = "https://developer.mozilla.org"
TS_BASE_URL = "https://www.typescriptlang.org"
NPM_REGISTRY_URL = "https://registry.npmjs.org"
NPM_PACKAGE_URL = "https://www.npmjs.com/package"

DEFAULT_TIMEOUT = 20.0
PACKUMENT_TIMEOUT = 60.0  # popular packages ship multi-MB packuments


class FetchError(Exception):
    """Transport-level failure (connection error, timeout, ...) after retries."""


def _headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _get(url: str, timeout: float = DEFAULT_TIMEOUT) -> httpx.Response:
    """GET ``url`` with a browser-ish client; one retry on transport errors.

    Returns the response for any HTTP status (the caller interprets it).
    Raises :class:`FetchError` only when no response could be obtained at
    all (connection failure, timeout, ...) after two attempts.
    """
    last_error: Exception | None = None
    for _attempt in range(2):  # initial attempt + one retry
        try:
            with httpx.Client(
                timeout=timeout, follow_redirects=True, headers=_headers()
            ) as client:
                return client.get(url)
        except httpx.TransportError as exc:
            last_error = exc
    raise FetchError(f"request to {url} failed: {last_error}") from last_error


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
        response = _get(url)
        if response.status_code == 404:
            return _fail(f"page not found on MDN: {slug}")
        if response.status_code >= 400:
            return _fail(f"MDN returned HTTP {response.status_code} for {slug}")
        result = parse_mdn_html(response.text, str(response.url))
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
        response = _get(url)
        if response.status_code == 404:
            return _fail(f"page not found on typescriptlang.org: {page}")
        if response.status_code >= 400:
            return _fail(
                f"typescriptlang.org returned HTTP {response.status_code} for {page}"
            )
        result = parse_ts_html(response.text, str(response.url))
        if result.get("ok"):
            result["page"] = page
        return result
    except Exception as exc:
        return _fail(f"failed to fetch TypeScript page: {exc}")


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

    The single-version doc has no readme, so the result carries
    ``readme_markdown=None`` plus an explanatory ``note``.
    """
    try:
        response = _get(f"{NPM_REGISTRY_URL}/{encoded_name}/latest")
    except FetchError as exc:
        return _fail(f"failed to fetch npm package: {exc}")
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
    return result


def fetch_npm_package(name: str, version: str | None = None) -> dict:
    """Fetch npm package metadata (and readme when resolvable).

    With ``version``: uses the single-version doc ``/{name}/{version}``.
    Without: fetches the full packument (longer timeout + one retry because
    popular packages are multi-MB); if that fails but the package exists,
    falls back to ``/{name}/latest`` with ``readme_markdown=None`` and a
    ``note``. Returns ``{"ok": False, "error": "package not found on npm",
    "suggestion": ...}`` for unknown packages — never raises.
    """
    try:
        name = (name or "").strip()
        if not name:
            return _fail("empty package name")
        encoded_name = urllib.parse.quote(name, safe="@/")

        if version is not None and str(version).strip():
            version = str(version).strip()
            url = f"{NPM_REGISTRY_URL}/{encoded_name}/{urllib.parse.quote(version, safe='')}"
            response = _get(url)
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
            return result

        # No version given: full packument (longer timeout; one retry in _get).
        try:
            response = _get(f"{NPM_REGISTRY_URL}/{encoded_name}", PACKUMENT_TIMEOUT)
        except FetchError as exc:
            return _npm_latest_fallback(name, encoded_name, str(exc))
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
            return fallback if fallback.get("ok") else _fail(
                f"npm registry returned HTTP {response.status_code}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            return _npm_latest_fallback(name, encoded_name, f"invalid JSON: {exc}")
        result = parse_npm_packument(data)
        if not result.get("ok"):
            fallback = _npm_latest_fallback(name, encoded_name, result["error"])
            return fallback if fallback.get("ok") else result
        result["url"] = f"{NPM_PACKAGE_URL}/{name}"
        return result
    except Exception as exc:
        return _fail(f"failed to fetch npm package: {exc}")

"""Offline tests for the fetcher parsers, using real downloaded fixtures.

Fixtures (see ``tests/fixtures``):
- mdn_array.html        <- MDN Array reference page
- ts_intro.html         <- TypeScript handbook intro page
- npm_react_latest.json <- registry.npmjs.org/react/latest (single-version doc)
- npm_leftpad.json      <- registry.npmjs.org/left-pad (full packument, small)

No network access is required or performed by these tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from js_ts_mcp.fetchers import (
    parse_mdn_html,
    parse_npm_packument,
    parse_npm_version_doc,
    parse_ts_html,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fixture_json(name: str):
    return json.loads(_fixture_text(name))


# ---------------------------------------------------------------------------
# MDN
# ---------------------------------------------------------------------------

def test_parse_mdn_html_array_fixture():
    html = _fixture_text("mdn_array.html")
    url = (
        "https://developer.mozilla.org/en-US/docs/"
        "Web/JavaScript/Reference/Global_Objects/Array"
    )
    result = parse_mdn_html(html, url)

    assert result["ok"] is True
    assert result["url"] == url
    assert result["title"].startswith("Array")
    assert "| MDN" not in result["title"]

    markdown = result["markdown"]
    assert len(markdown) > 5000
    assert "push" in markdown
    assert "map" in markdown


def test_parse_mdn_html_rejects_garbage():
    result = parse_mdn_html("<html><body>no main here</body></html>", "http://x")
    assert result["ok"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# TypeScript handbook
# ---------------------------------------------------------------------------

def test_parse_ts_html_intro_fixture():
    html = _fixture_text("ts_intro.html")
    url = "https://www.typescriptlang.org/docs/handbook/intro.html"
    result = parse_ts_html(html, url)

    assert result["ok"] is True
    assert result["url"] == url
    assert "TypeScript Handbook" in result["title"]

    markdown = result["markdown"]
    assert len(markdown) > 2000
    # sidebar/TOC junk must not leak into the markdown
    assert "On this page" not in markdown


def test_parse_ts_html_rejects_garbage():
    result = parse_ts_html("", "http://x")
    assert result["ok"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# npm — single-version doc
# ---------------------------------------------------------------------------

def test_parse_npm_version_doc_react_fixture():
    data = _fixture_json("npm_react_latest.json")
    result = parse_npm_version_doc(data)

    assert result["ok"] is True
    assert result["name"] == "react"
    assert result["version"] == "19.3.0"
    assert result["license"] == "MIT"
    assert result["homepage"] == "https://react.dev/"
    assert result["description"]
    # single-version docs carry no readme
    assert result["readme_markdown"] is None
    # react has no runtime dependencies -> empty dict, not None
    assert result["dependencies"] == {}


def test_parse_npm_version_doc_license_normalization():
    apache = parse_npm_version_doc(
        {"name": "a", "version": "1.0.0", "license": {"type": "Apache-2.0"}}
    )
    mit = parse_npm_version_doc({"name": "b", "version": "1.0.0", "license": "MIT"})

    assert apache["ok"] is True
    assert isinstance(apache["license"], str)
    assert apache["license"] == "Apache-2.0"
    assert mit["ok"] is True
    assert isinstance(mit["license"], str)
    assert mit["license"] == "MIT"


def test_parse_npm_version_doc_rejects_garbage():
    result = parse_npm_version_doc({})
    assert result["ok"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# npm — full packument
# ---------------------------------------------------------------------------

def test_parse_npm_packument_leftpad_fixture():
    data = _fixture_json("npm_leftpad.json")
    result = parse_npm_packument(data)

    assert result["ok"] is True
    assert result["name"] == "left-pad"
    # latest version resolved from dist-tags
    assert result["version"] == data["dist-tags"]["latest"]
    assert len(result["readme_markdown"]) > 100
    assert isinstance(result["dependencies"], dict)


def test_parse_npm_packument_rejects_garbage():
    result = parse_npm_packument({})
    assert result["ok"] is False
    assert "error" in result

"""Offline tests for the fetcher parsers, using real downloaded fixtures.

Fixtures (see ``tests/fixtures``):
- mdn_array.html        <- MDN Array reference page
- ts_intro.html         <- TypeScript handbook intro page
- npm_react_latest.json <- registry.npmjs.org/react/latest (single-version doc)
- npm_leftpad.json      <- registry.npmjs.org/left-pad (full packument, small)

No network access is required or performed by these tests.  The GitHub README
fallback is covered here for its pure half (repository-URL parsing, pointer
detection) and in ``test_politeness_wiring.py`` for its HTTP half.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from js_ts_mcp.fetchers import (
    _github_owner_repo,
    _readme_is_substantive,
    _readme_pointer_target,
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


# ---------------------------------------------------------------------------
# GitHub README fallback — pure parsing, no HTTP
# ---------------------------------------------------------------------------

# Every one of these is a repository URL npm really serves.  Measured live on
# 2026-10-07 from registry.npmjs.org: zod, express and left-pad are the three
# real-world shapes at the top of the table.
_GITHUB_URL_CASES = {
    "git+https://github.com/colinhacks/zod.git": "colinhacks/zod",
    "git+https://github.com/expressjs/express.git": "expressjs/express",
    "git+ssh://git@github.com/stevemao/left-pad.git": "stevemao/left-pad",
    "https://github.com/owner/repo": "owner/repo",
    "http://github.com/owner/repo": "owner/repo",
    "git://github.com/owner/repo": "owner/repo",
    "git+ssh://github.com/owner/repo.git": "owner/repo",
    "ssh://git@github.com/owner/repo.git": "owner/repo",
    "github:owner/repo": "owner/repo",
    "gh:owner/repo": "owner/repo",
    "github:owner/repo.git": "owner/repo",
    "https://github.com/owner/repo/": "owner/repo",
    "https://github.com/owner/repo.git/": "owner/repo",
    # npm appends "#readme" when it derives the field from the package page.
    "https://github.com/owner/repo#readme": "owner/repo",
    "git+https://github.com/owner/repo.git#readme": "owner/repo",
    "https://www.github.com/owner/repo": "owner/repo",
    "https://GitHub.com/Owner/Repo": "Owner/Repo",
    "  https://github.com/owner/repo  ": "owner/repo",
}


@pytest.mark.parametrize(("url", "expected"), sorted(_GITHUB_URL_CASES.items()))
def test_github_owner_repo_parses_every_shape_npm_serves(url, expected):
    assert _github_owner_repo(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # Other hosts have their own raw-file layouts and their own robots
        # policies; this server does not guess at them.
        "https://gitlab.com/owner/repo.git",
        "git+https://gitlab.com/owner/repo.git",
        "https://bitbucket.org/owner/repo",
        "https://github.example.com/owner/repo",
        "https://notgithub.com/owner/repo",
        # Not a repository URL at all.
        "https://github.com/owner",
        "github:owner",
        "https://example.com/",
        "not a url",
        "",
        "   ",
        None,
    ],
)
def test_github_owner_repo_returns_none_when_there_is_nothing_safe_to_fetch(url):
    assert _github_owner_repo(url) is None


def test_readme_pointer_target_follows_the_zod_monorepo_layout():
    """The real zod root README: 22 bytes, one line, naming the real document."""
    body = "packages/zod/README.md"
    assert len(body.encode("utf-8")) == 22
    assert _readme_pointer_target(body) == "packages/zod/README.md"
    # Trailing newline / surrounding whitespace is normal in a committed file.
    assert _readme_pointer_target("  packages/zod/README.md\n\n") == "packages/zod/README.md"
    # "./" is a relative path too.
    assert _readme_pointer_target("./docs/Readme.rst") == "docs/Readme.rst"


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   \n  ",
        "# A real README\n\nwith several lines of prose in it.",
        "see docs/README.md and packages/x/README.md",   # two candidates, ambiguous
        "https://raw.githubusercontent.com/evil/other/HEAD/README.md",
        "/etc/passwd",
        "../secrets/README.md",
        "docs/../secrets/README.md",
        "docs//README.md",
        "docs/logo.png",            # not a document
        "docs/README",              # no extension
        "x" * 400 + ".md",          # longer than a pointer can plausibly be
    ],
)
def test_readme_pointer_target_refuses_anything_that_is_not_one_relative_document(body):
    assert _readme_pointer_target(body) is None


def test_readme_is_substantive_rejects_githubs_own_404_body():
    """raw.githubusercontent.com answers a miss with ``404: Not Found`` (14 B)."""
    assert _readme_is_substantive("404: Not Found") is False
    assert _readme_is_substantive("x" * 80) is False      # the cap is exclusive
    assert _readme_is_substantive("x" * 81) is True
    assert _readme_is_substantive("# zod\n\n" + "body\n" * 40) is True

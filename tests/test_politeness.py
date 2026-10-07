"""Offline tests for ``js_ts_mcp.politeness`` — no network, no real sleeps.

This is the shared reference suite from ``mcp-politeness-audit/ref/test_politeness.py``.
Keep it in sync with the other three repos; only the import path and the
opt-out env-var name are repo-specific here.

Everything HTTP is simulated with ``httpx.MockTransport``; time and jitter are
injected (``clock`` / ``sleep`` / ``wall_clock`` / ``rng``), so the suite is
deterministic and runs in well under a second.

Run:  .venv/bin/python -m pytest -q      # from the repository root
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.robotparser import RobotFileParser

import httpx
import pytest

from js_ts_mcp import politeness as pol_mod
from js_ts_mcp.politeness import (
    CACHE_DIR_ENV_VAR,
    FALLBACK_CACHE_DIR_NAME,
    Politeness,
    PoliteResponse,
    ROBOTS_DB_NAME,
    _match_rules,
    default_robots_db_path,
    parse_robots,
)

UA = "docs-mcp/0.1 (+https://example.com/docs-mcp)"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
class FakeClock:
    """Monotonic clock + a sleep that advances it (so waits are measurable).

    A11 F5: the two hands are deliberately independent.  ``t`` (monotonic) drives
    throttling / backoff; ``wall`` drives persisted timestamps and TTL.  A test
    that wants a robots.txt entry to expire must move ``wall`` — moving ``t`` is
    exactly the pre-A11 behaviour the fix removed.
    """

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

    def advance_wall(self, seconds: float) -> None:
        """Move only the wall hand (TTL expiry, NTP corrections)."""
        self.wall += seconds


class Recorder:
    """MockTransport handler that records every request (url, headers, clock)."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requests: list[tuple[str, httpx.Headers, float]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((str(request.url), request.headers, 0.0))
        key = request.url.path
        action = self.routes.get(key, self.routes.get("*"))
        if callable(action):
            return action(request, len(self.requests))
        if isinstance(action, int):
            return httpx.Response(action, content=b"nope")
        if isinstance(action, BaseException):
            raise action
        return httpx.Response(200, content=action)

    def paths(self) -> list[str]:
        return [httpx.URL(u).path for u, _h, _t in self.requests]

    def robots_hits(self) -> int:
        return sum(1 for p in self.paths() if p == "/robots.txt")

    def hits(self, path: str) -> int:
        return sum(1 for p in self.paths() if p == path)

    def headers_for(self, index: int) -> httpx.Headers:
        return self.requests[index][1]


def counting(handler):
    """Adapt ``handler(request, n)`` to the httpx MockTransport signature."""
    state = {"n": 0}

    def _h(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return handler(request, state["n"])

    return _h


def make(clock: FakeClock, routes: dict[str, object], **kw) -> tuple[Politeness, httpx.Client, Recorder]:
    rec = Recorder(routes)
    client = httpx.Client(transport=httpx.MockTransport(rec))
    pol = Politeness(
        UA,
        cache_path=kw.pop("cache_path", None),
        base_delay=kw.pop("base_delay", (0.35, 0.9)),
        clock=clock.now,
        sleep=clock.sleep,
        wall_clock=clock.wall_now,
        rng=random.Random(1234),
        **kw,
    )
    return pol, client, rec


# Real robots.txt bodies, copied from the A2 audit fixtures (mcp-politeness-audit/robots/).
PYPI_ROBOTS = """Sitemap: https://pypi.org/sitemap.xml

User-agent: *
Disallow: /simple/
Disallow: /packages/
Disallow: /_includes/authed/
Disallow: /project/*/submit-malware-report/
Disallow: /pypi/*/json
Disallow: /pypi/*/*/json
Disallow: /pypi*?
Disallow: /search*
Disallow: /_/
Disallow: /integrity/
Disallow: /account/
Disallow: /admin/
"""

SPRING_ROBOTS = """User-agent: *
crawl-delay: 1

User-agent: *
Disallow: /autorepo/
"""

FLUTTER_ROBOTS = """# All robots welcome!
"""

NPM_JSON_TRAP = '{"name":"robots.txt","versions":{}}'


# --------------------------------------------------------------------------- #
# 1. the PyPI verdict (the whole point of the audit)
# --------------------------------------------------------------------------- #
def test_pypi_wildcard_blocks_the_json_api_and_allows_the_project_page():
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": PYPI_ROBOTS, "*": b"{}"})
    assert pol.can_fetch("https://pypi.org/pypi/dio/json", client=client) is False
    assert pol.can_fetch("https://pypi.org/pypi/dio/1.0/json", client=client) is False
    assert pol.can_fetch("https://pypi.org/search?q=dio", client=client) is False
    assert pol.can_fetch("https://pypi.org/simple/dio/", client=client) is False
    # the robots-allowed alternative A6 may switch to:
    assert pol.can_fetch("https://pypi.org/project/dio/", client=client) is True
    assert pol.can_fetch("https://pypi.org/project/dio/1.0/", client=client) is True
    assert rec.robots_hits() == 1


@pytest.mark.skipif(sys.version_info >= (3, 13), reason="stdlib gained RFC 9309 wildcard matching in 3.13 (measured: 3.10-3.12 miss it)")
def test_stdlib_robotparser_below_313_misses_the_wildcard():
    """Regression guard: stdlib < 3.13 must NOT be used for this."""
    rp = RobotFileParser()
    rp.parse([line for line in PYPI_ROBOTS.splitlines() if line.strip()])
    assert rp.can_fetch(UA, "https://pypi.org/pypi/dio/json") is True  # the bug
    clock = FakeClock()
    pol, client, _ = make(clock, {"/robots.txt": PYPI_ROBOTS})
    assert pol.can_fetch("https://pypi.org/pypi/dio/json", client=client) is False


def test_get_on_blocked_url_makes_no_request_and_explains_itself():
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": PYPI_ROBOTS, "/pypi/dio/json": b"secret"})
    resp = pol.get(client, "https://pypi.org/pypi/dio/json")
    assert isinstance(resp, PoliteResponse)
    assert resp.blocked_by_robots is True
    assert resp.ok is False
    assert resp.status_code is None
    assert resp.content == b""
    assert "Disallow: /pypi/*/json" in (resp.error or "")
    assert rec.hits("/pypi/dio/json") == 0
    assert rec.robots_hits() == 1


# --------------------------------------------------------------------------- #
# 2. matcher semantics: *, $, longest match, Allow > Disallow, case
# --------------------------------------------------------------------------- #
def test_allow_beats_wildcard_disallow_by_longest_pattern():
    robots = "User-agent: *\nDisallow: /pypi/*/json\nAllow: /pypi/dio/json\n"
    clock = FakeClock()
    pol, client, _ = make(clock, {"/robots.txt": robots})
    assert pol.can_fetch("https://pypi.org/pypi/dio/json", client=client) is True
    assert pol.can_fetch("https://pypi.org/pypi/other/json", client=client) is False


def test_wildcard_and_end_anchor():
    robots = "User-agent: *\nDisallow: /exact$\nAllow: /allow-me$\nDisallow: /search*\n"
    clock = FakeClock()
    pol, client, _ = make(clock, {"/robots.txt": robots})
    assert pol.can_fetch("https://x.test/exact", client=client) is False
    assert pol.can_fetch("https://x.test/exact/extra", client=client) is True  # $ anchors
    assert pol.can_fetch("https://x.test/allow-me", client=client) is True
    assert pol.can_fetch("https://x.test/allow-me/deep", client=client) is True
    assert pol.can_fetch("https://x.test/search?q=1", client=client) is False
    assert pol.can_fetch("https://x.test/searching", client=client) is False  # * spans chars


def test_longest_match_from_rfc9309_section_5_2():
    robots = "User-agent: foobot\nAllow: /example/page/\nDisallow: /example/page/disallowed.gif\n"
    clock = FakeClock()
    pol, client, _ = make(FakeClock(), {"/robots.txt": robots})
    pol = Politeness("foobot/1.0", clock=clock.now, sleep=clock.sleep, rng=random.Random(0))
    rules = [r for g in parse_robots(robots) for r in g.rules]
    assert _match_rules(rules, "/example/page/disallowed.gif")[0] is False
    assert _match_rules(rules, "/example/page/other.gif")[0] is True
    assert pol.can_fetch("https://x.test/example/page/disallowed.gif", client=client) is False


def test_allow_disallow_identical_tie_break_prefers_allow():
    robots = "User-agent: *\nAllow: /twin\nDisallow: /twin\n"
    rules = [r for g in parse_robots(robots) for r in g.rules]
    allowed, reason = _match_rules(rules, "/twin")
    assert allowed is True
    assert reason == "Allow: /twin"


def test_matching_is_case_insensitive():
    robots = "User-agent: *\nDisallow: /PyPI/*/JSON\n"
    rules = [r for g in parse_robots(robots) for r in g.rules]
    assert _match_rules(rules, "/pypi/dio/json")[0] is False


def test_bare_query_rule_does_not_block_the_whole_site():
    robots = "User-agent: *\nDisallow: /*?\n"
    rules = [r for g in parse_robots(robots) for r in g.rules]
    assert _match_rules(rules, "/")[0] is True          # no query -> must stay allowed
    assert _match_rules(rules, "/a/b")[0] is True
    assert _match_rules(rules, "/a/b?x=1")[0] is False  # query present -> blocked


def test_user_agent_group_selection_and_merge():
    robots = (
        "User-agent: Googlebot\nDisallow: /gsp/\n\n"
        "User-agent: *\nDisallow: /star/\n"
    )
    clock = FakeClock()
    pol, client, _ = make(clock, {"/robots.txt": robots})
    assert pol.can_fetch("https://x.test/gsp/x", client=client) is True   # not our group
    assert pol.can_fetch("https://x.test/star/x", client=client) is False
    google = Politeness("Googlebot/2.1 (+http://www.google.com/bot.html)",
                        clock=clock.now, sleep=clock.sleep, rng=random.Random(0))
    assert google.can_fetch("https://x.test/gsp/x", client=client) is False


def test_two_matching_groups_are_merged_spring_style():
    """docs.spring.io ships crawl-delay in one '*' block and Disallow in another."""
    clock = FakeClock()
    pol, client, _ = make(clock, {"/robots.txt": SPRING_ROBOTS, "/autorepo/x": b"x", "/docs/a": b"a"})
    assert pol.can_fetch("https://docs.spring.io/autorepo/docs/x", client=client) is False
    assert pol.can_fetch("https://docs.spring.io/spring-boot/docs/x", client=client) is True
    assert pol._crawl_delay(pol._parsed["docs.spring.io"], "docs.spring.io") == 1.0


def test_no_rules_at_all_means_everything_allowed():
    clock = FakeClock()
    pol, client, _ = make(clock, {"/robots.txt": FLUTTER_ROBOTS, "/flutter/x": b"x"})
    assert pol.can_fetch("https://api.flutter.dev/flutter/material/AppBar-class.html", client=client) is True


# --------------------------------------------------------------------------- #
# 3. throttle + Crawl-delay (measured with the fake clock, never a real sleep)
# --------------------------------------------------------------------------- #
def test_crawl_delay_one_second_is_respected():
    clock = FakeClock()
    stamps: list[float] = []

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=SPRING_ROBOTS.encode())
        stamps.append(clock.t)
        return httpx.Response(200, content=b"page")

    rec = Recorder({})
    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, clock=clock.now, sleep=clock.sleep, rng=random.Random(7))
    for _ in range(3):
        r = pol.get(client, "https://docs.spring.io/spring-boot/docs/current/reference/htmlsingle/using/index.html")
        assert r.ok
    assert len(stamps) == 3
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 1.0 for g in gaps), gaps
    # A8 F1 changed this assertion: the robots fetch now *reserves* a per-host
    # slot, so the page request that follows it waits (bounded by base_delay — the
    # crawl-delay of a file we have not downloaded yet cannot be known).  The
    # robots fetch itself is the first request on the host and never waits
    # (A5 §4.9).  The page-to-page gaps above are what Crawl-delay guarantees.
    s = pol.stats()
    assert s["robots_throttle_waits"] == 0
    assert s["throttle_waits"] == 3            # page1 waits for the robots slot + 2 gaps
    assert clock.slept[0] >= 0.35
    assert all(x >= 1.0 for x in clock.slept[1:]), clock.slept


def test_crawl_delay_gates_the_first_content_request_after_a_cold_robots_fetch():
    """A13 B2 (A12 F-A12-1): a delay learned *now* must apply now, not next time.

    A12 measured the robots→content gap on ``docs.spring.io`` (``crawl-delay:
    1``) at 365 / 711 / 814 / 827 / 857 ms on 5/5 cold runs, while every
    content→content gap was exactly 1000 ms.  The first robots fetch cannot
    know a rule it has not downloaded yet (A8 F1 keeps it throttled with
    ``floor=0``), but the parsed value used to be applied one request too late.
    RFC 9309 §4 counts the interval between *successive* requests, so it is
    anchored on the robots request that is already on the wire.
    """
    clock = FakeClock()
    stamps: list[tuple[str, float]] = []

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        stamps.append((request.url.path, clock.t))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=SPRING_ROBOTS.encode())
        return httpx.Response(200, content=b"page")

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, clock=clock.now, sleep=clock.sleep, rng=random.Random(7))
    assert pol.get(client, "https://docs.spring.io/spring-boot/reference/index.html").ok
    assert [p for p, _ in stamps] == ["/robots.txt", "/spring-boot/reference/index.html"]
    gap = stamps[1][1] - stamps[0][1]
    assert gap >= 1.0, f"first content request was {gap}s after the robots fetch"
    # A8 F1 is untouched: the robots fetch took the first slot of the queue and
    # the content request waited for it, and the delay was pushed exactly once.
    s = pol.stats()
    assert s["crawl_delay_applied"] == 1
    assert s["robots_requests"] == 1 and s["requests"] == 1
    assert s["robots_throttle_waits"] == 0          # first request on the host
    assert clock.slept == [pytest.approx(1.0)], clock.slept

    # and the following requests keep the same 1 s spacing (no double charge);
    # the robots row is cached, so there is no third request
    assert pol.get(client, "https://docs.spring.io/spring-boot/reference/web/index.html").ok
    assert [p for p, _ in stamps] == [
        "/robots.txt",
        "/spring-boot/reference/index.html",
        "/spring-boot/reference/web/index.html",
    ]
    assert stamps[2][1] - stamps[1][1] == pytest.approx(1.0), stamps
    assert pol.stats()["crawl_delay_applied"] == 1  # cached robots row: no re-push


def test_base_delay_jitter_is_the_floor_when_no_crawl_delay():
    clock = FakeClock()
    stamps: list[float] = []

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=FLUTTER_ROBOTS.encode())
        stamps.append(clock.t)
        return httpx.Response(200, content=b"page")

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, base_delay=(0.5, 0.5), clock=clock.now, sleep=clock.sleep, rng=random.Random(3))
    for _ in range(2):
        assert pol.get(client, "https://api.flutter.dev/index.html").ok
    assert stamps[1] - stamps[0] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# 4. 429 / 503 / Retry-After / backoff
# --------------------------------------------------------------------------- #
def test_429_with_retry_after_seconds_waits_exactly_that_and_retries_once():
    clock = FakeClock()

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=FLUTTER_ROBOTS.encode())
        return httpx.Response(429, headers={"retry-after": "3"}, content=b"slow down")

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, clock=clock.now, sleep=clock.sleep, rng=random.Random(1))
    resp = pol.get(client, "https://api.flutter.dev/index.html")
    assert resp.status_code == 429
    assert resp.ok is False
    assert 3.0 in clock.slept
    assert pol.stats()["retry_after_honoured"] == 1
    assert pol.stats()["retries_429"] == 1
    # A8 F1: slept[0] is now the throttle gap that the robots fetch created —
    # the page request has to wait for it.  The Retry-After wait is therefore
    # everything that is not a throttle wait.
    assert clock.slept[-1] == pytest.approx(3.0)
    assert sum(clock.slept) - pol.stats()["throttle_sleep_s"] == pytest.approx(3.0)


def test_429_with_retry_after_http_date_is_parsed():
    clock = FakeClock()
    when = datetime.fromtimestamp(clock.wall, tz=timezone.utc) + timedelta(seconds=5)

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=FLUTTER_ROBOTS.encode())
        return httpx.Response(503, headers={"retry-after": format_datetime(when, usegmt=True)})

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, clock=clock.now, sleep=clock.sleep, wall_clock=clock.wall_now, rng=random.Random(1))
    resp = pol.get(client, "https://api.flutter.dev/index.html")
    assert resp.status_code == 503
    assert clock.slept[-1] == pytest.approx(5.0, abs=0.01)  # last sleep == the retry wait


def test_429_without_header_uses_exponential_backoff_with_jitter():
    clock = FakeClock()

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=FLUTTER_ROBOTS.encode())
        return httpx.Response(429, content=b"")

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, base_delay=(1.0, 1.0), clock=clock.now, sleep=clock.sleep, rng=random.Random(11))
    pol.get(client, "https://api.flutter.dev/index.html")
    wait = clock.slept[-1]  # A8 F1: slept[0] is the robots/page throttle gap
    assert 1.0 * 2 * 0.75 <= wait <= 1.0 * 2 * 1.25, wait


def test_backoff_is_capped_at_backoff_cap():
    clock = FakeClock()

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=FLUTTER_ROBOTS.encode())
        return httpx.Response(429, content=b"")

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, base_delay=(40.0, 40.0), backoff_cap=30.0, clock=clock.now, sleep=clock.sleep, rng=random.Random(5))
    pol.get(client, "https://api.flutter.dev/index.html")
    assert clock.slept[-1] == pytest.approx(30.0)
    # A8 F1 side effect: the 40 s base_delay also shows up as a throttle gap
    # between the robots fetch and the page request.
    assert pol.stats()["throttle_sleep_s"] == pytest.approx(40.0)


def test_503_then_200_succeeds_after_one_retry():
    clock = FakeClock()

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=FLUTTER_ROBOTS.encode())
        if n == 2:
            return httpx.Response(503, content=b"")
        return httpx.Response(200, content=b"page")

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, clock=clock.now, sleep=clock.sleep, rng=random.Random(2))
    resp = pol.get(client, "https://api.flutter.dev/index.html")
    assert resp.ok is True and resp.status_code == 200 and resp.content == b"page"


def test_transport_error_retries_once_then_reports_error():
    clock = FakeClock()

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=FLUTTER_ROBOTS.encode())
        raise httpx.ConnectError("connection refused", request=request)

    rec = Recorder({})
    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, clock=clock.now, sleep=clock.sleep, rng=random.Random(4))
    resp = pol.get(client, "https://api.flutter.dev/index.html")
    assert resp.ok is False and resp.status_code is None
    assert "transport error" in (resp.error or "")
    # 2 attempts total == today's "1 retry" behaviour, now spaced by backoff.
    # A8 F1/F3: one extra sleep exists (the robots→page throttle gap) and both
    # attempts are counted as wire requests.
    assert pol.stats()["requests"] == 2
    assert len(clock.slept) == 2
    assert clock.slept[-1] > 0.0  # the spacing between attempt 1 and attempt 2
    assert pol.stats()["retries_transport"] == 2


def test_stall_escalates_delay_without_extra_retry():
    clock = FakeClock()

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=FLUTTER_ROBOTS.encode())
        clock.advance(12.0)  # search.maven.org style stall
        return httpx.Response(200, content=b"eventually")

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, base_delay=(0.5, 0.5), clock=clock.now, sleep=clock.sleep, rng=random.Random(9))
    resp = pol.get(client, "https://search.maven.org/solrsearch/select?q=guava")
    assert resp.ok is True
    assert pol.stats()["stalls"] == 1
    assert pol.stats()["retries_429"] == 0
    assert pol.stats()["hosts"]["search.maven.org"] > 0.5


# --------------------------------------------------------------------------- #
# 5. conditional GET
# --------------------------------------------------------------------------- #
def test_conditional_get_304_returns_cached_body_without_re_downloading():
    clock = FakeClock()
    bodies: list[bytes] = []

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=b"")
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"etag": '"v2"'})
        bodies.append(request.url.path.encode())
        return httpx.Response(200, content=b"the-page", headers={"etag": '"v1"', "last-modified": "Wed, 21 Oct 2015 07:28:00 GMT"})

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, base_delay=(0.0, 0.0), clock=clock.now, sleep=clock.sleep, rng=random.Random(0))
    url = "https://docs.python.org/3/library/json.html"
    first = pol.get(client, url)
    assert first.ok and first.from_cache is False and first.content == b"the-page"
    assert first.validators["etag"] == '"v1"'

    second = pol.get(client, url, validators=first.validators)
    assert second.from_cache is True
    assert second.status_code == 304
    assert second.content == b"the-page"          # served from the layer's body cache
    assert second.validators["etag"] == '"v2"'    # refreshed validators
    assert second.ok is True
    assert len(bodies) == 1                       # no second download
    assert pol.stats()["revalidated_304"] == 1


def test_conditional_get_uses_caller_cached_body():
    clock = FakeClock()

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=b"")
        if request.headers.get("if-modified-since"):
            return httpx.Response(304)
        return httpx.Response(200, content=b"fresh", headers={"last-modified": "Wed, 21 Oct 2015 07:28:00 GMT"})

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, base_delay=(0.0, 0.0), clock=clock.now, sleep=clock.sleep, rng=random.Random(0))
    url = "https://api.dart.dev/dart-async/Future-class.html"
    resp = pol.get(client, url, validators={"last_modified": "Wed, 21 Oct 2015 07:28:00 GMT"}, cached_body=b"from-sqlite")
    assert resp.from_cache is True and resp.content == b"from-sqlite"


def test_host_without_validators_sends_no_conditional_headers():
    """search.maven.org has neither ETag nor Last-Modified (A2 §3)."""
    clock = FakeClock()

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=b"")
        assert "if-none-match" not in request.headers
        assert "if-modified-since" not in request.headers
        return httpx.Response(200, content=b"{}")

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, base_delay=(0.0, 0.0), clock=clock.now, sleep=clock.sleep, rng=random.Random(0))
    resp = pol.get(client, "https://search.maven.org/solrsearch/select?q=guava")
    assert resp.ok and resp.validators == {}



def test_body_cache_is_byte_bounded():
    """A 7 MB npm packument must not turn the layer into a memory leak."""
    clock = FakeClock()
    seen: list[str] = []

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=b"")
        seen.append(request.url.path)
        assert "if-none-match" not in request.headers  # evicted -> no conditional GET
        return httpx.Response(200, content=b"x" * 80, headers={"etag": '"e"'})

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, base_delay=(0.0, 0.0), max_cached_bytes=100,
                     clock=clock.now, sleep=clock.sleep, rng=random.Random(0))
    a = pol.get(client, "https://registry.npmjs.org/react")
    b = pol.get(client, "https://registry.npmjs.org/lodash")
    assert a.ok and b.ok
    assert pol._body_bytes <= 100
    assert pol.get(client, "https://registry.npmjs.org/react", validators={"etag": '"e"'}).status_code == 200
    assert pol.stats()["conditional_skipped"] == 1


# --------------------------------------------------------------------------- #
# 6. robots cache: negative caching, TTL refresh (the crawl4ai bug)
# --------------------------------------------------------------------------- #
def test_negative_robots_cache_is_not_refetched():
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": 404, "/a": b"a"})
    assert pol.can_fetch("https://api.dart.dev/a", client=client) is True
    assert pol.can_fetch("https://api.dart.dev/a", client=client) is True
    assert rec.robots_hits() == 1
    assert pol.stats()["robots_negative"] == 1


def test_403_robots_is_cached_too():
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": 403})
    assert pol.can_fetch("https://www.npmjs.com/x", client=client) is True
    assert pol.can_fetch("https://www.npmjs.com/y", client=client) is True
    assert rec.robots_hits() == 1


def test_200_json_body_is_not_treated_as_rules():
    """registry.npmjs.org/robots.txt returns an npm packument (A2 §1.1 trap)."""
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": NPM_JSON_TRAP, "/react": b"{}"})
    assert pol.can_fetch("https://registry.npmjs.org/react", client=client) is True
    assert pol.can_fetch("https://registry.npmjs.org/react", client=client) is True
    assert rec.robots_hits() == 1


def test_ttl_refresh_updates_fetched_at_even_when_content_is_unchanged():
    """Regression for crawl4ai bug #1: unchanged body must still refresh fetched_at."""
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": PYPI_ROBOTS, "/project/dio/": b"ok"}, robots_ttl=100.0)
    assert pol.can_fetch("https://pypi.org/project/dio/", client=client) is True
    assert rec.robots_hits() == 1

    clock.advance_wall(101)  # TTL expired (wall clock — A11 F5)
    assert pol.can_fetch("https://pypi.org/project/dio/", client=client) is True
    assert rec.robots_hits() == 2  # one refresh, body identical

    clock.advance_wall(50)  # still inside the *new* TTL
    assert pol.can_fetch("https://pypi.org/project/dio/", client=client) is True
    assert pol.can_fetch("https://pypi.org/project/dio/", client=client) is True
    assert rec.robots_hits() == 2  # with the crawl4ai bug this would be 3, 4, 5, ...
    assert pol.stats()["robots_refreshed_unchanged"] == 1


def test_robots_transport_error_fails_open_and_is_soft_cached():
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": httpx.ConnectError("dns down"), "/a": b"a"})
    assert pol.can_fetch("https://api.dart.dev/a", client=client) is True
    assert pol.can_fetch("https://api.dart.dev/a", client=client) is True
    assert rec.robots_hits() == 1  # soft negative cached for min(ttl, 300 s)
    clock.advance_wall(301)
    assert pol.can_fetch("https://api.dart.dev/a", client=client) is True
    assert rec.robots_hits() == 2


def test_robots_cache_persists_across_instances(tmp_path):
    clock = FakeClock()
    db = str(tmp_path / "robots.db")
    pol1, client, rec = make(clock, {"/robots.txt": PYPI_ROBOTS, "/project/x": b"x"}, cache_path=db)
    assert pol1.can_fetch("https://pypi.org/project/x/", client=client) is True
    assert rec.robots_hits() == 1
    pol2, _client2, _rec2 = make(clock, {"/robots.txt": PYPI_ROBOTS}, cache_path=db)
    assert pol2.can_fetch("https://pypi.org/pypi/dio/json") is False  # served from SQLite
    pol1.close()
    pol2.close()


# --------------------------------------------------------------------------- #
# 7. budget, opt-out, fail-open guarantees, stats
# --------------------------------------------------------------------------- #
def test_budget_cap_stops_after_the_limit():
    clock = FakeClock()
    pol, _client, _rec = make(clock, {})
    assert [pol.budget("index:api.flutter.dev", 3) for _ in range(5)] == [True, True, True, False, False]
    assert pol.stats()["budget_denied"] == 2
    pol.reset_budget("index:api.flutter.dev")
    assert pol.budget("index:api.flutter.dev", 3) is True


def test_budget_scope_also_gates_get():
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": FLUTTER_ROBOTS, "/x": b"x"}, base_delay=(0.0, 0.0))
    # A8 F3 rewrote this test on purpose.  It used to prove "one get() == one
    # budget unit"; that was exactly the defect A5 measured (limit 4 on an error
    # path == 8+ real requests).  A unit is now one *attempt on the wire*, and the
    # robots.txt fetch that serves the call is one of them:
    #   call 1 = robots fetch (1) + page (2);  call 2 = page (3);  call 3 = denied.
    for _ in range(2):
        assert pol.get(client, "https://api.flutter.dev/x", budget_scope="call-1", budget_limit=3).ok
    resp = pol.get(client, "https://api.flutter.dev/x", budget_scope="call-1", budget_limit=3)
    assert resp.ok is False and "budget" in (resp.error or "")
    assert rec.hits("/x") == 2  # the third page request never reached the network
    assert rec.robots_hits() == 1
    assert pol.stats()["budgets"]["call-1"] == [3, 3]
    assert pol.stats()["requests"] == 2
    assert pol.stats()["robots_requests"] == 1


def test_opt_out_env_var_disables_the_layer(monkeypatch):
    monkeypatch.setenv("JS_TS_MCP_POLITENESS_DISABLED", "1")
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": PYPI_ROBOTS, "/pypi/dio/json": b"{}"})
    assert pol.disabled is True
    assert pol.can_fetch("https://pypi.org/pypi/dio/json", client=client) is True
    resp = pol.get(client, "https://pypi.org/pypi/dio/json")
    assert resp.ok is True
    assert rec.robots_hits() == 0
    assert clock.slept == []
    assert pol.stats()["disabled"] is True


def test_politeness_never_raises_on_garbage_input():
    clock = FakeClock()
    pol, client, _rec = make(clock, {})
    resp = pol.get(client, "not a url")
    assert resp.ok is False and resp.error
    assert pol.can_fetch("ftp://x/y") is True
    assert pol.can_fetch("") is True


def test_allowlist_blocks_unexpected_hosts():
    """Optional insurance (A1 §3): the layer never leaves the known domains."""
    clock = FakeClock()
    allowed = frozenset({"api.flutter.dev"})
    pol, client, _rec = make(clock, {"/robots.txt": FLUTTER_ROBOTS, "/x": b"x"}, allowed_hosts=allowed)
    assert pol.can_fetch("https://api.flutter.dev/x", client=client) is True
    assert pol.can_fetch("https://evil.test/x", client=client) is False
    resp = pol.get(client, "https://evil.test/x")
    assert resp.ok is False
    assert resp.blocked_by_robots is False
    assert "ALLOWED_HOSTS" in (resp.error or "")


def test_stats_shape_for_a_status_tool():
    clock = FakeClock()
    pol, client, _ = make(clock, {"/robots.txt": PYPI_ROBOTS, "/project/x": b"x"}, base_delay=(0.0, 0.0))
    pol.get(client, "https://pypi.org/project/x/")
    s = pol.stats()
    for key in (
        "requests", "robots_fetches", "robots_cache_hits", "robots_negative",
        "blocked_by_robots", "throttle_waits", "retries_429", "retries_transport",
        "conditional", "revalidated_304", "stalls", "budget_denied", "errors", "disabled",
    ):
        assert key in s, key
    assert s["requests"] >= 1 and s["robots_fetches"] == 1


# --------------------------------------------------------------------------- #
# 8. A8 regressions — F1 robots throttle, F2 redirects, F3 budget, F4 challenge
# --------------------------------------------------------------------------- #
def test_robots_refresh_waits_its_turn_in_the_per_host_queue():
    """A8 F1.  A7 measured ``min_gap_ms`` falling 388 -> 15 / 434 -> 12 ms because
    ``_fetch_robots()`` jumped the per-host queue.  robots.txt is implicitly
    allowed (RFC 9309 §2.2.2) but it is still a request: it waits its turn."""
    clock = FakeClock()
    stamps: list[tuple[str, float]] = []

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        stamps.append((request.url.path, clock.t))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=FLUTTER_ROBOTS.encode())
        return httpx.Response(200, content=b"page")

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, base_delay=(0.5, 0.5), robots_ttl=0.0,
                     clock=clock.now, sleep=clock.sleep, rng=random.Random(3))
    assert pol.get(client, "https://api.flutter.dev/a").ok
    assert pol.get(client, "https://api.flutter.dev/b").ok
    assert [p for p, _ in stamps] == ["/robots.txt", "/a", "/robots.txt", "/b"]
    gaps = [b - a for (_p1, a), (_p2, b) in zip(stamps, stamps[1:])]
    assert all(g >= 0.5 for g in gaps), gaps
    s = pol.stats()
    assert s["robots_requests"] == 2 and s["requests"] == 2
    assert s["robots_throttle_waits"] == 1            # the 2nd robots fetch waited
    assert s["robots_throttle_sleep_s"] == pytest.approx(0.5)
    assert s["throttle_waits"] == 3                   # robots waits are included
    assert s["throttle_sleep_s"] == pytest.approx(1.5)


def test_crawl_delay_also_gates_the_robots_refresh():
    """A TTL refresh already knows the host's ``Crawl-delay`` — it must honour it."""
    clock = FakeClock()
    stamps: list[tuple[str, float]] = []

    def handler(request: httpx.Request, n: int) -> httpx.Response:
        stamps.append((request.url.path, clock.t))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=SPRING_ROBOTS.encode())
        return httpx.Response(200, content=b"page")

    client = httpx.Client(transport=httpx.MockTransport(counting(handler)))
    pol = Politeness(UA, base_delay=(0.0, 0.0), robots_ttl=0.0,
                     clock=clock.now, sleep=clock.sleep, rng=random.Random(3))
    assert pol.get(client, "https://docs.spring.io/docs/a").ok
    assert pol.get(client, "https://docs.spring.io/docs/b").ok
    gaps = [b - a for (_p1, a), (_p2, b) in zip(stamps, stamps[1:])]
    assert gaps[1] >= 1.0, gaps          # the robots refresh itself waited
    assert gaps[2] >= 1.0, gaps
    assert pol.stats()["robots_throttle_sleep_s"] >= 1.0


def test_redirect_target_is_checked_against_robots_and_its_body_is_never_used():
    """A8 F2 (A5 §4.5): a 302 target used to be fetched without any robots check."""
    clock = FakeClock()
    pol, client, rec = make(
        clock,
        {
            "/robots.txt": "User-agent: *\nDisallow: /private/\n",
            "/start": lambda request, n: httpx.Response(302, headers={"location": "/private/secret"}),
            "/private/secret": b"CROWN-JECTS",
        },
        base_delay=(0.0, 0.0),
    )
    resp = pol.get(client, "https://a.test/start")
    assert resp.blocked_by_robots is True
    assert resp.ok is False
    assert resp.content == b""                        # the body must never leak
    assert resp.url == "https://a.test/private/secret"
    assert "redirect target of https://a.test/start" in (resp.error or "")
    assert rec.hits("/private/secret") == 0           # not one byte fetched
    assert rec.hits("/start") == 1
    assert pol.stats()["redirect_hops"] == 1
    assert pol.stats()["blocked_by_robots"] == 1


def test_cross_host_redirect_is_evaluated_against_the_new_host_robots():
    clock = FakeClock()

    def route(request: httpx.Request, n: int) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if path == "/robots.txt":
            if host == "a.test":
                return httpx.Response(200, content=b"User-agent: *\n")
            return httpx.Response(200, content=b"User-agent: *\nDisallow: /secret\n")
        if host == "a.test" and path == "/start":
            return httpx.Response(302, headers={"location": "https://b.test/secret"})
        return httpx.Response(200, content=b"SECRET PAGE")

    pol, client, rec = make(clock, {"*": route}, base_delay=(0.0, 0.0))
    resp = pol.get(client, "https://a.test/start")
    assert resp.blocked_by_robots is True and resp.content == b""
    assert "blocked by robots.txt for b.test" in (resp.error or "")
    hosts = {httpx.URL(u).host for u, _h, _t in rec.requests}
    assert hosts == {"a.test", "b.test"}              # robots of BOTH hosts consulted
    assert sum(1 for u, _h, _t in rec.requests if httpx.URL(u).path == "/secret") == 0


def test_relative_redirect_chain_returns_the_final_url():
    """Callers (all four fetchers) rely on ``PoliteResponse.url`` being the final URL."""
    clock = FakeClock()
    pol, client, rec = make(
        clock,
        {
            "/robots.txt": FLUTTER_ROBOTS,
            "/a": lambda request, n: httpx.Response(302, headers={"location": "/b"}),
            "/b": lambda request, n: httpx.Response(302, headers={"location": "https://api.flutter.dev/deep/c"}),
            "/deep/c": b"final",
        },
        base_delay=(0.0, 0.0),
    )
    resp = pol.get(client, "https://api.flutter.dev/a")
    assert resp.ok is True and resp.content == b"final"
    assert resp.url == "https://api.flutter.dev/deep/c"
    assert pol.stats()["redirect_hops"] == 2
    assert pol.stats()["requests"] == 3               # every hop is a wire request
    assert rec.robots_hits() == 1


def test_redirect_loop_ends_with_an_error_not_an_exception():
    clock = FakeClock()
    pol, client, rec = make(
        clock,
        {"/robots.txt": FLUTTER_ROBOTS,
         "/loop": lambda request, n: httpx.Response(302, headers={"location": "/loop"})},
        base_delay=(0.0, 0.0),
        max_redirects=5,
    )
    resp = pol.get(client, "https://api.flutter.dev/loop")
    assert resp.ok is False and resp.status_code is None
    assert "too many redirects" in (resp.error or "")
    assert pol.stats()["redirect_hops"] == 5
    assert rec.hits("/loop") == 6                     # initial + 5 hops, then stop


def test_client_with_follow_redirects_true_cannot_bypass_the_layer():
    """A repo may keep ``follow_redirects=True`` on its client; the layer overrides
    it per request, so the redirect check cannot be skipped by configuration."""
    clock = FakeClock()
    rec = Recorder({
        "/robots.txt": "User-agent: *\nDisallow: /private/\n",
        "/start": lambda request, n: httpx.Response(302, headers={"location": "/private/secret"}),
        "/private/secret": b"nope",
    })
    client = httpx.Client(transport=httpx.MockTransport(rec), follow_redirects=True)
    pol = Politeness(UA, base_delay=(0.0, 0.0), clock=clock.now, sleep=clock.sleep, rng=random.Random(0))
    resp = pol.get(client, "https://a.test/start")
    assert resp.blocked_by_robots is True and resp.content == b""
    assert rec.hits("/private/secret") == 0


def test_3xx_without_location_is_reported_as_is():
    clock = FakeClock()
    pol, client, rec = make(
        clock,
        {"/robots.txt": FLUTTER_ROBOTS,
         "/gone": lambda request, n: httpx.Response(301, content=b"moved")},
        base_delay=(0.0, 0.0),
    )
    resp = pol.get(client, "https://api.flutter.dev/gone")
    assert resp.status_code == 301 and resp.ok is False
    assert resp.content == b"moved"
    assert pol.stats()["redirect_hops"] == 0


def test_each_redirect_hop_costs_a_budget_unit():
    """A8 F3: a hop is a request on the wire, so it pays into the scope budget."""
    clock = FakeClock()
    pol, client, rec = make(
        clock,
        {"/robots.txt": FLUTTER_ROBOTS,
         "/a": lambda request, n: httpx.Response(302, headers={"location": "/b"}),
         "/b": lambda request, n: httpx.Response(302, headers={"location": "/c"}),
         "/c": b"final"},
        base_delay=(0.0, 0.0),
    )
    resp = pol.get(client, "https://api.flutter.dev/a", budget_scope="call-1", budget_limit=3)
    assert resp.ok is False and "budget" in (resp.error or "")
    # robots (1) + GET /a (2) + GET /b (3) -> the last hop is refused
    assert rec.hits("/c") == 0
    assert pol.stats()["budgets"]["call-1"] == [3, 3]
    assert pol.stats()["redirect_hops"] == 2


def test_budget_counts_every_retry_attempt():
    """A8 F3 (A5 §4.4): 'limit 4' must mean 4 real requests, not 8."""
    clock = FakeClock()

    def route(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=FLUTTER_ROBOTS.encode())
        if n == 2:
            return httpx.Response(429, content=b"slow down")
        return httpx.Response(200, content=b"page")

    pol, client, rec = make(clock, {"*": route}, base_delay=(0.0, 0.0))
    resp = pol.get(client, "https://api.flutter.dev/x", budget_scope="call-1", budget_limit=3)
    assert resp.ok is True and resp.content == b"page"
    s = pol.stats()
    assert s["requests"] == 2 and s["robots_requests"] == 1
    assert s["budgets"]["call-1"] == [3, 3]           # robots + 429 attempt + retry
    again = pol.get(client, "https://api.flutter.dev/x", budget_scope="call-1", budget_limit=3)
    assert again.ok is False and "budget" in (again.error or "")
    assert rec.hits("/x") == 2


def test_budget_denies_the_robots_fetch_too():
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": FLUTTER_ROBOTS, "/x": b"x"}, base_delay=(0.0, 0.0))
    resp = pol.get(client, "https://api.flutter.dev/x", budget_scope="call-1", budget_limit=0)
    assert resp.ok is False and "(robots fetch)" in (resp.error or "")
    assert resp.blocked_by_robots is False            # a refusal, not a robots verdict
    assert rec.robots_hits() == 0 and rec.hits("/x") == 0
    assert pol.stats()["budget_denied"] == 1


def test_stats_separate_robots_from_content_requests():
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": FLUTTER_ROBOTS, "/x": b"x"}, base_delay=(0.0, 0.0))
    pol.get(client, "https://api.flutter.dev/x")
    s = pol.stats()
    assert s["requests"] == 1 and s["robots_requests"] == 1
    assert s["requests"] + s["robots_requests"] == len(rec.requests)


def test_challenge_detector_retries_once_after_a_delay_on_the_same_url():
    """A8 F4 (A6 §5): PyPI answers HTTP 200 with a ~3 KB JS challenge page.
    The reference only supplies the hook; this test uses a fake detector."""
    clock = FakeClock()
    seen: list[str] = []

    def route(request: httpx.Request, n: int) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=PYPI_ROBOTS.encode())
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(200, content=b"<html>fs-challenge-please-wait</html>")
        return httpx.Response(200, content=b"<html>the real project page</html>")

    pol, client, _rec = make(clock, {"*": route}, base_delay=(0.5, 0.5),
                             challenge_detector=lambda body, headers: b"challenge" in body)
    resp = pol.get(client, "https://pypi.org/project/dio/")
    assert resp.ok is True and resp.content == b"<html>the real project page</html>"
    assert resp.bot_challenge is False
    assert seen == ["https://pypi.org/project/dio/"] * 2   # same URL, never a fallback
    s = pol.stats()
    assert s["challenge_detected"] == 1 and s["challenge_retries"] == 1
    assert s["requests"] == 2
    assert clock.slept[-1] >= 0.75                  # spaced by the escalated delay
    assert pol._bodies["https://pypi.org/project/dio/"][0] == b"<html>the real project page</html>"


def test_persistent_challenge_is_reported_and_never_cached_or_fell_back():
    clock = FakeClock()
    asked: list[str] = []

    def route(request: httpx.Request, n: int) -> httpx.Response:
        asked.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=PYPI_ROBOTS.encode())
        return httpx.Response(200, content=b"<html>fs-challenge</html>")

    pol, client, _rec = make(clock, {"*": route}, base_delay=(0.0, 0.0),
                             challenge_detector=lambda body, headers: b"challenge" in body)
    resp = pol.get(client, "https://pypi.org/project/six/")
    assert resp.ok is False and resp.bot_challenge is True
    assert resp.status_code == 200
    assert "anti-bot challenge" in (resp.error or "")
    assert pol.stats()["challenge_retries"] == 1
    assert "https://pypi.org/project/six/" not in pol._bodies      # never cached
    # the tempting fallback — the robots-disallowed JSON API — was never touched
    assert not [u for u in asked if u.startswith("https://pypi.org/pypi/")]
    assert asked.count("https://pypi.org/project/six/") == 2


def test_broken_challenge_detector_cannot_break_a_tool():
    clock = FakeClock()

    def boom(body: bytes, headers: httpx.Headers) -> bool:
        raise RuntimeError("detector bug")

    pol, client, _rec = make(clock, {"/robots.txt": FLUTTER_ROBOTS, "/x": b"x"},
                             challenge_detector=boom)
    resp = pol.get(client, "https://api.flutter.dev/x")
    assert resp.ok is True and resp.content == b"x"
    assert pol.stats()["challenge_detected"] == 0


def test_redirect_to_a_non_http_scheme_is_never_followed():
    clock = FakeClock()
    pol, client, rec = make(
        clock,
        {"/robots.txt": FLUTTER_ROBOTS,
         "/up": lambda request, n: httpx.Response(302, headers={"location": "file:///etc/passwd"})},
        base_delay=(0.0, 0.0),
    )
    resp = pol.get(client, "https://api.flutter.dev/up")
    assert resp.status_code == 302 and resp.ok is False
    assert pol.stats()["redirect_hops"] == 0
    assert rec.hits("/up") == 1


# --------------------------------------------------------------------------- #
# 9. repo-specific constants (copy-drift guard)
# --------------------------------------------------------------------------- #
def test_repo_specific_names_are_this_repos():
    """The shared module must carry *this* repo's env var and DB name.

    All four repos copy one module; this test is what catches a copy that kept
    another repo's constants.
    """
    from js_ts_mcp import politeness as pol_mod

    assert pol_mod.DEFAULT_DISABLE_ENV_VAR == "JS_TS_MCP_POLITENESS_DISABLED"
    assert pol_mod.ROBOTS_DB_NAME == "robots.db"
    assert pol_mod.default_robots_db_path().endswith("robots.db")

# --------------------------------------------------------------------------- #
# 9. A11 regressions — F5: persisted clock vs. scheduling clock
# --------------------------------------------------------------------------- #
def stored_fetched_at(db: str, host: str = "pypi.org") -> float:
    """Read the raw ``fetched_at`` straight out of SQLite (what a reboot sees)."""
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT fetched_at FROM robots WHERE host = ?", (host,)).fetchone()
    return float(row[0])


def test_robots_fetched_at_is_a_wall_clock_timestamp(tmp_path):
    """F5 core: what is written to SQLite must be epoch seconds, not monotonic."""
    clock = FakeClock(start=1_234.0, wall=1_700_000_000.0)
    db = str(tmp_path / "robots.db")
    pol, client, _rec = make(clock, {"/robots.txt": PYPI_ROBOTS, "/project/dio/": b"ok"},
                             cache_path=db)
    assert pol.can_fetch("https://pypi.org/project/dio/", client=client) is True
    stored = stored_fetched_at(db)
    assert stored == pytest.approx(1_700_000_000.0)   # the wall hand
    assert stored > 1e9                               # never the monotonic 1_234.0
    pol.close()


def test_robots_ttl_survives_a_reboot_that_resets_the_monotonic_clock(tmp_path):
    """Two instances, different monotonic bases, one wall clock = a reboot.

    Pre-A11 the second instance compared its *new* monotonic base with a stamp
    from the *old* boot: the age came out negative, ``age < ttl`` held forever and
    the rules were never refreshed again — stale robots rules for the whole life
    of the DB.  Correct behaviour: the row is still fresh right after the reboot
    **and** it expires once the wall clock crosses the TTL.
    """
    db = str(tmp_path / "robots.db")
    routes = {"/robots.txt": PYPI_ROBOTS, "/project/dio/": b"ok"}

    boot1 = FakeClock(start=90_000.0)      # ~25 h of uptime
    pol1, client1, rec1 = make(boot1, routes, cache_path=db, robots_ttl=100.0)
    assert pol1.can_fetch("https://pypi.org/project/dio/", client=client1) is True
    assert rec1.robots_hits() == 1
    pol1.close()

    boot2 = FakeClock(start=5.0)           # reboot: monotonic restarts, wall does not
    assert boot2.wall == boot1.wall
    pol2, client2, rec2 = make(boot2, routes, cache_path=db, robots_ttl=100.0)
    assert pol2.can_fetch("https://pypi.org/pypi/dio/json", client=client2) is False
    assert rec2.robots_hits() == 0         # still fresh: served from SQLite, correct
    boot2.advance_wall(101)                # the *wall* clock crossed the TTL
    assert pol2.can_fetch("https://pypi.org/pypi/dio/json", client=client2) is False
    assert rec2.robots_hits() == 1         # refreshed — pre-A11 this stayed 0 forever
    assert pol2.stats()["robots_refreshed_unchanged"] == 1
    pol2.close()


def test_robots_row_dated_in_the_future_expires_instead_of_freezing():
    """NTP stepped the wall clock back — the row is now "from the future".

    A negative age must mean *expired*, not *immortal*: the pre-A11 check was only
    ``age < ttl``, so one backwards clock step froze the rules forever.
    """
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": PYPI_ROBOTS, "/project/dio/": b"ok"},
                            robots_ttl=7 * 24 * 3600)
    assert pol.can_fetch("https://pypi.org/project/dio/", client=client) is True
    assert rec.robots_hits() == 1

    clock.advance_wall(-3600.0)            # NTP correction: one hour backwards
    assert pol.can_fetch("https://pypi.org/project/dio/", client=client) is True
    assert rec.robots_hits() == 2          # re-fetched, not trusted forever

    assert pol.can_fetch("https://pypi.org/project/dio/", client=client) is True
    assert rec.robots_hits() == 2          # the new stamp heals it: no refetch storm


def test_legacy_monotonic_row_is_refetched_not_trusted_forever(tmp_path):
    """A DB written by the pre-A11 code migrates itself — no ALTER TABLE needed.

    The legacy row holds ``time.monotonic()`` (seconds since boot, ~1.2e4) while
    the wall clock reads ~1.7e9.  That is "far from now", so the entry expires on
    first sight and the replacement stamp is a real epoch second.
    """
    db = str(tmp_path / "robots.db")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS robots (host TEXT PRIMARY KEY,"
            " status INTEGER NOT NULL, content TEXT NOT NULL, fetched_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO robots (host, status, content, fetched_at) VALUES (?,?,?,?)",
            ("pypi.org", 200, "User-agent: *\nDisallow: /nothing-relevant/\n", 12_345.0),
        )
        conn.commit()

    clock = FakeClock(start=60.0, wall=1_700_000_000.0)
    pol, client, rec = make(clock, {"/robots.txt": PYPI_ROBOTS, "/project/dio/": b"ok"},
                            cache_path=db)
    assert pol.can_fetch("https://pypi.org/project/dio/", client=client) is True
    assert rec.robots_hits() == 1          # the legacy row was not believed
    assert stored_fetched_at(db) == pytest.approx(1_700_000_000.0)

    clock.advance_wall(7 * 24 * 3600 + 1)  # the migrated row expires normally too
    assert pol.can_fetch("https://pypi.org/project/dio/", client=client) is True
    assert rec.robots_hits() == 2
    pol.close()


def test_wall_clock_jump_does_not_disturb_the_throttle():
    """The other half of F5: scheduling stays on the monotonic hand.

    A three-year wall-clock step must not make the per-host queue wait for years,
    and must not let it skip a wait either — ``next_allowed`` is monotonic.
    """
    clock = FakeClock()
    years = 3 * 365 * 24 * 3600
    pol, client, _rec = make(clock, {"/robots.txt": PYPI_ROBOTS, "/a": b"a", "/b": b"b"},
                             base_delay=(0.5, 0.5), robots_ttl=10 * years)
    assert pol.get(client, "https://pypi.org/a").ok is True
    clock.advance_wall(years)              # forward: throttle must ignore it
    assert pol.get(client, "https://pypi.org/b").ok is True
    clock.advance_wall(-years)             # back to where we were: same
    assert pol.get(client, "https://pypi.org/a").ok is True

    assert clock.slept == [pytest.approx(0.5)] * 3, clock.slept
    assert pol.stats()["throttle_waits"] == 3
    assert pol.stats()["throttle_sleep_s"] == pytest.approx(1.5)
    assert pol.stats()["robots_requests"] == 1   # robots stayed fresh the whole time


# --------------------------------------------------------------------------- #
# 10. P6 — the robots store degrades, the call survives
# --------------------------------------------------------------------------- #
#: The exact error SQLite raises when the file exists but the filesystem (or the
#: file's own permission bits) refuses the write.  Measured live on this host:
#: ``~/.cache/<repo>`` is a read-only mount, so every robots-cache write raised
#: it and the tool answered ``error="politeness failure: …"``.
READONLY = "attempt to write a readonly database"

#: root bypasses the permission bits, so the chmod-based tests below only mean
#: something for an unprivileged user (same guard as tests/test_cache.py).
NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")


class ReadOnlyDb:
    """A store that reads fine and refuses every write, like a read-only mount.

    ``execute``/``commit`` raise the production error instead of touching disk,
    so the failure is reproduced without needing a read-only filesystem.
    """

    def __init__(self, rows: dict[str, tuple[int, str, float]] | None = None) -> None:
        self.rows = dict(rows or {})
        self.writes = 0

    def execute(self, sql: str, params=()):
        if sql.lstrip().upper().startswith("SELECT"):
            return _Row(self.rows.get(params[0]) if params else None)
        self.writes += 1
        raise sqlite3.OperationalError(READONLY)

    def commit(self) -> None:
        raise sqlite3.OperationalError(READONLY)

    def close(self) -> None:
        pass


class _Row:
    def __init__(self, row) -> None:
        self._row = row

    def fetchone(self):
        return self._row


def test_readonly_database_never_fails_a_request(monkeypatch, tmp_path):
    """The measured production bug, reproduced offline.

    Pre-P6 the refused INSERT escaped :meth:`Politeness._store_robots_row` →
    ``_robots_for`` → ``_get`` → ``get()`` as
    ``error="politeness failure: attempt to write a readonly database"``, which
    ``fetchers._get`` turned into a ``FetchError`` and the tool into a null
    README — on a host whose network was perfectly reachable.
    """
    clock = FakeClock()
    pol, client, rec = make(
        clock, {"/robots.txt": PYPI_ROBOTS, "/project/dio/": b"ok"},
        cache_path=str(tmp_path / "robots.db"),
    )
    monkeypatch.setattr(pol, "_db", ReadOnlyDb())

    resp = pol.get(client, "https://pypi.org/project/dio/")

    assert resp.ok is True, resp.error
    assert resp.error is None
    assert rec.hits("/project/dio/") == 1          # the request really happened
    assert rec.robots_hits() == 1                  # and so did the robots fetch
    assert pol.stats()["errors"] == 0              # nothing was reported as an error
    assert pol.stats()["robots_write_failures"] == 1
    assert pol.read_only is True                   # and it will not be retried
    assert READONLY in (pol.read_only_reason or "")
    pol.close()


def test_refused_write_is_recorded_once_and_the_store_stops_trying(monkeypatch, tmp_path):
    """Best-effort means *best-effort*: one counted failure, then no more writes."""
    clock = FakeClock()
    pol, client, rec = make(
        clock, {"/robots.txt": PYPI_ROBOTS, "/a": b"a", "/b": b"b", "/c": b"c"},
        cache_path=str(tmp_path / "robots.db"),
    )
    broken = ReadOnlyDb()
    monkeypatch.setattr(pol, "_db", broken)

    for path in ("/a", "/b", "/c"):
        assert pol.get(client, f"https://pypi.org{path}").ok is True

    assert broken.writes == 1, "the store was still being written to after the refusal"
    assert pol.stats()["robots_write_failures"] == 1
    assert pol.stats()["errors"] == 0
    assert rec.robots_hits() == 3                  # every call still got its rules
    pol.close()


@NOT_ROOT
def test_unwritable_cache_dir_falls_back_and_says_so(tmp_path, monkeypatch, capsys):
    """The cache-dir env var is honoured first, and moved only when unwritable."""
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "robots.db").write_bytes(b"-- a database this process may not touch")
    os.chmod(locked, 0o555)
    fallback = tmp_path / "fallback"
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(locked))
    monkeypatch.setattr(pol_mod, "_fallback_cache_dirs", lambda: (str(fallback), str(tmp_path / "temp")))

    chosen = default_robots_db_path()

    assert chosen == str(fallback / ROBOTS_DB_NAME)
    assert os.access(os.path.dirname(chosen), os.W_OK)
    err = capsys.readouterr().err
    assert "robots cache fallback" in err
    assert str(locked) in err
    assert os.path.dirname(chosen) in err

    # …and the layer built on that path works.
    clock = FakeClock()
    pol, client, rec = make(clock, {"/robots.txt": PYPI_ROBOTS, "/project/dio/": b"ok"},
                            cache_path=chosen)
    assert pol.get(client, "https://pypi.org/project/dio/").ok is True
    assert rec.hits("/project/dio/") == 1
    assert pol.stats()["robots_write_failures"] == 0
    pol.close()


@NOT_ROOT
def test_no_writable_cache_dir_anywhere_still_polite(tmp_path, monkeypatch):
    """No file at all is a supported state, not a failure."""
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o555)
    monkeypatch.setenv(CACHE_DIR_ENV_VAR, str(locked))
    monkeypatch.setattr(
        pol_mod, "_fallback_cache_dirs",
        lambda: (str(locked / "nested"), str(locked / "temp")),
    )

    clock = FakeClock()
    pol, client, rec = make(
        clock, {"/robots.txt": PYPI_ROBOTS, "/project/dio/": b"ok"},
        cache_path=default_robots_db_path(),
    )
    assert pol.in_memory is True
    assert pol.cache_path is None
    assert pol.read_only is True
    assert pol.read_only_reason and "no fallback dir is writable" in pol.read_only_reason

    resp = pol.get(client, "https://pypi.org/project/dio/")
    assert resp.ok is True, resp.error
    assert rec.hits("/project/dio/") == 1
    assert pol.stats()["errors"] == 0
    pol.close()


@NOT_ROOT
def test_readonly_store_still_serves_cached_rules(tmp_path):
    """A warm ``robots.db`` that cannot be written is a read-only store, not a miss.

    This is what a read-only mount leaves behind: the rows are readable, the
    journal is not creatable.  Reading them must keep working — that is what
    ``mode=ro`` is for.
    """
    db = str(tmp_path / "robots.db")
    clock = FakeClock()
    pol1, client1, rec1 = make(
        clock, {"/robots.txt": PYPI_ROBOTS, "/project/dio/": b"ok"}, cache_path=db)
    assert pol1.can_fetch("https://pypi.org/project/dio/", client=client1) is True
    assert rec1.robots_hits() == 1
    pol1.close()

    os.chmod(db, 0o444)
    pol2, client2, rec2 = make(
        clock, {"/robots.txt": PYPI_ROBOTS, "/project/dio/": b"ok"}, cache_path=db)
    assert pol2.read_only is True
    assert pol2.cache_path == db                    # the file is still the store
    assert pol2.in_memory is False
    # Disallowed by the cached rules, answered from SQLite without a robots fetch.
    assert pol2.can_fetch("https://pypi.org/pypi/dio/json", client=client2) is False
    assert rec2.robots_hits() == 0
    assert pol2.stats()["robots_cache_hits"] == 1
    assert pol2.stats()["robots_write_failures"] == 0
    pol2.close()
    os.chmod(db, 0o644)


@NOT_ROOT
def test_readonly_store_without_a_table_degrades_to_memory(tmp_path, capsys):
    """A read-only WAL database can open with **no** ``robots`` table at all.

    Measured: a WAL database copied without its ``-wal``/``-shm`` sidecars opens
    ``mode=ro`` and reports ``no such table: robots`` — the schema lived in the
    WAL.  That is a miss, not an error, and the store moves to memory so the same
    failure is not paid for on every call.
    """
    db = str(tmp_path / "robots.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE other (x INTEGER)")   # deliberately not "robots"
    conn.commit()
    conn.close()
    os.chmod(db, 0o444)

    clock = FakeClock()
    pol, client, rec = make(
        clock, {"/robots.txt": PYPI_ROBOTS, "/project/dio/": b"ok"}, cache_path=db)
    resp = pol.get(client, "https://pypi.org/project/dio/")

    assert resp.ok is True, resp.error
    assert rec.hits("/project/dio/") == 1
    assert rec.robots_hits() == 1                     # fetched, not read from the file
    assert pol.stats()["robots_store_errors"] == 1
    assert pol.stats()["errors"] == 0
    assert pol.in_memory is True                      # moved off the unusable file
    assert "store moved to memory" in capsys.readouterr().err
    pol.close()


def test_fallback_chain_is_the_repo_cache_dir_then_the_temp_dir():
    """Where the fallback goes is part of the contract, so it is asserted here."""
    repo_dir, temp_dir = pol_mod._fallback_cache_dirs()
    package_dir = os.path.dirname(os.path.abspath(pol_mod.__file__))
    repo_root = os.path.dirname(os.path.dirname(package_dir))
    assert repo_dir == os.path.join(repo_root, FALLBACK_CACHE_DIR_NAME)
    assert temp_dir == os.path.join(tempfile.gettempdir(), os.path.basename(repo_root))


def test_stats_say_where_the_store_is(tmp_path):
    """``stats()`` reports the store's state, so a degraded cache is visible."""
    clock = FakeClock()
    memory, _client, _rec = make(clock, {})
    stats = memory.stats()
    assert stats["robots_db"] is None
    assert stats["read_only"] is False
    assert stats["read_only_reason"] is None
    assert stats["robots_write_failures"] == 0
    assert stats["robots_store_errors"] == 0
    memory.close()

    db = str(tmp_path / "robots.db")
    on_disk, _c2, _r2 = make(clock, {}, cache_path=db)
    assert on_disk.stats()["robots_db"] == db
    assert on_disk.stats()["read_only"] is False
    on_disk.close()

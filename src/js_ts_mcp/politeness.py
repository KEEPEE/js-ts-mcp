"""Politeness layer for js-ts-mcp (one of four docs-MCP servers).

PORTED COPY — this file is the shared reference module from
``mcp-politeness-audit/ref/politeness.py``.  Its **logic is kept identical** to
the copies in java-spring-mcp / flutter-mcp / python-docs-mcp so that all four
servers behave the same.  Only three things are repo-specific and each is
tagged ``REPO-SPECIFIC`` below: the opt-out env-var name, the default
robots-cache DB path, and this header.  Never "improve" the logic here without
porting the same change to the other three repositories.

One self-contained **synchronous** module: robots.txt cache + RFC 9309 rule matcher,
per-host throttle (incl. ``Crawl-delay``), ``429``/``503``/``504`` handling with
``Retry-After``, conditional GET (ETag / Last-Modified -> 304) and a per-scope request
budget.  Dependencies: **stdlib + httpx** only (no Playwright, no lxml, no new deps).

ATTRIBUTION / LICENCE
---------------------
The *design* of this module — a TTL-cached robots store, a per-host throttle
that honours ``Crawl-delay``, and an escalating backoff for rate-limit
responses — was **inspired by** Crawl4AI,
https://github.com/unclecode/crawl4ai (© Unclecode, Apache License 2.0).

**The implementation below is our own, written from scratch (clean-room).**
Nothing here was transcribed, translated or mechanically adapted from a
crawl4ai source line.  Measured against ``crawl4ai/utils.py`` +
``crawl4ai/cache_validator.py`` (``A12-verification.md`` §4.2): the longest
contiguous span of code shared with crawl4ai is **37 characters** — a plain
stdlib idiom (``PRAGMA journal_mode=WAL``, ``except Exception: return False``) —
and the longest run of identical source lines is **two trivial lines**.  The
shared code is negligible; what was borrowed is the idea and the problem
breakdown, which copyright does not cover.

For reference, the places in *their* tree whose behaviour inspired ours (a
reading pointer to the design — **not** the source of any code here):
``crawl4ai/utils.py:282-419`` (``RobotsParser``), ``crawl4ai/utils.py:54-82``
(wildcard rule translation), ``crawl4ai/utils.py:260-279``
(``_preserve_bare_query``) and ``crawl4ai/async_dispatcher.py:28-85``
(``RateLimiter``).  Those ranges are cited so a reader can compare designs;
they are not ranges that were copied.

Three defects of the original design are fixed here (see
``A1-crawl4ai-reuse.md`` §1):

1. ``fetched_at`` is refreshed on **every** re-fetch, even when the robots.txt body
   is byte-identical — crawl4ai skips the write, so after the TTL expires it
   re-downloads robots.txt on *every* call (request count goes up, not down).
2. Negative results (404 / 403 / 5xx / unparseable body) **are** cached.
3. ``Crawl-delay`` is parsed and honoured (crawl4ai never reads it).

The GPL-3.0 part of crawl4ai — the vendored ``crawl4ai/html2text/`` fork — was
**deliberately excluded**: no code, no data and no dependency from that tree is
used, shipped or installed by these MIT repositories, and none ever should be.
This module never converts HTML to markdown; that is the caller's job
(``markdownify``).

WHAT THIS MODULE GUARANTEES
---------------------------
* ``can_fetch()`` and ``get()`` **never raise** — any internal failure is fail-open
  (robots unknown -> allowed) or reported as ``PoliteResponse.error``.
* An explicit ``Disallow`` match is fail-**closed**: ``blocked_by_robots=True``.
* The public API of the MCP tools is unaffected; the layer is an internal detail.

A8 HARDENING (four gaps measured by A5/A6/A7 — see ``A8-layer-fixes.md``)
------------------------------------------------------------------------
* **F1 — robots.txt is throttled too.** ``_fetch_robots()`` used to hit the wire
  outside the per-host throttle, so a robots fetch sat 0 ms after the previous
  request (A7: ``min_gap_ms`` fell 388→15 / 434→12 ms).  robots.txt is *always
  allowed* (RFC 9309 §2.2.2), so there is no chicken-and-egg problem: the fetch
  simply takes its turn in the same per-host queue.  Its waits are visible in
  ``stats()`` as ``robots_throttle_waits`` / ``robots_throttle_sleep_s`` and its
  attempts as ``robots_requests``.
* **F2 — redirects are resolved by the layer, not by httpx.** The layer sends with
  ``follow_redirects=False`` **even when the caller's client has
  ``follow_redirects=True``**, so every 3xx target is checked against that host's
  robots rules (RFC 9309 §2.3.1.2) before its body is ever used.  Max
  ``max_redirects`` (default 5) hops, then an error response — never an exception.
  A blocked target yields ``blocked_by_robots=True`` with an **empty body**.
  ``PoliteResponse.url`` is the final URL of the chain.
* **F3 — the budget counts wire attempts, not layer calls.** Every attempt pays:
  the robots fetch, the first try, a ``429``/``503`` retry, a transport retry and
  every redirect hop.  ``stats()["requests"]`` stays content-only and
  ``stats()["robots_requests"]`` is the robots counterpart, so the split is visible.
* **F4 — optional anti-bot challenge hook.** ``challenge_detector`` is a caller
  supplied predicate (e.g. PyPI's JS "Client Challenge" page: HTTP 200, ~3 KB).
  When it fires the layer waits (escalated per-host delay, never an immediate
  re-hit) and retries the **same URL** once; a persistent challenge is reported as
  ``PoliteResponse.error`` + ``bot_challenge=True`` and is never cached.  The
  reference deliberately contains no site-specific heuristic and never falls back
  to a robots-disallowed endpoint.

A11 HARDENING (F5 — the clock bug A10 had to work around with ``-1e18``)
-----------------------------------------------------------------------
* **F5 — persisted timestamps and TTL use the wall clock, scheduling stays
  monotonic.**  ``robots.fetched_at`` used to be written from ``clock`` (default
  ``time.monotonic``) and compared against the same clock.  A monotonic value is
  *seconds since boot*, so it is meaningless once persisted to SQLite: after a
  reboot the new base is lower, the stored stamp looks like it is in the future,
  ``now - fetched_at < TTL`` is true forever and robots rules are **never
  refreshed** — stale rules silently defeat politeness for months.  Now:
  ``wall_clock`` (default ``time.time``) stamps every row and answers every TTL
  question, while ``clock`` (monotonic) keeps driving per-host ``next_allowed``,
  backoff sleeps and stall measurement, where a wall-clock NTP jump would either
  make the throttle wait for years or not at all.  Freshness requires the age to
  be in ``[0, TTL)``, so a future-dated row (NTP stepped the clock back) is
  **expired**, not immortal — and a pre-A11 database holding monotonic numbers is
  decades away from the current wall clock, so those rows expire on first sight
  instead of being trusted forever.

A13 HARDENING (F6 — ``Crawl-delay`` arrived one request too late)
-----------------------------------------------------------------
* **F6 — a ``Crawl-delay`` learned from a robots fetch gates the request that
  triggered that fetch.**  The first fetch of a host queues with ``floor = 0.0``
  (the rule is not downloaded yet, so it cannot be honoured in advance) and
  reserves a slot of one jitter base, 0.35–0.9 s.  Before A13 the parsed delay
  then applied only from the *second* content request: ``start = max(now,
  next_allowed)`` was still bounded by that small reservation.  A12 measured
  ``docs.spring.io`` (``crawl-delay: 1``) at **365 / 711 / 814 / 827 / 857 ms**
  for the first gap on 5/5 runs, while every later gap was exactly 1000 ms.
  RFC 9309 §4 counts the delay between successive requests, so it is now
  measured from the robots request itself: :meth:`_apply_crawl_delay` pushes
  ``next_allowed`` to ``robots slot + Crawl-delay`` as soon as the body is
  parsed.  The robots fetch stays throttled (A8 F1 is untouched) and a delay
  that was already known makes this a no-op ``max()``.

PORTING NOTE (A4-A7): read ``A3-politeness-design.md`` before integrating.  Two
deliberate deviations from the naive reading of the spec are documented there:
``PoliteResponse.status_code`` stays truthful (``304`` on revalidation, ``None`` when
blocked/errored) and the politeness ``timeouts`` are applied per request via
``client.build_request(timeout=...)`` because ``httpx.Client.send()`` in 0.28 has no
``timeout`` argument.
"""

from __future__ import annotations

import os
import random
import re
import sqlite3
import threading
from collections import OrderedDict
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import httpx

__all__ = [
    "Politeness",
    "PoliteResponse",
    "DEFAULT_DISABLE_ENV_VAR",
    "RATE_LIMIT_CODES",
    "default_robots_db_path",
]

#: REPO-SPECIFIC (1/3): opt-out switch.  Set this env var to 1/true/yes/on to
#: switch the whole layer off — no robots.txt, no throttle, no retry, no
#: conditional GET — **at the user's own risk** (documented in README.md).
#: The other three repos use the same pattern with their own prefix
#: (``PYTHON_DOCS_…``, ``JAVA_SPRING_…``, ``FLUTTER_DOCS_…``).
DEFAULT_DISABLE_ENV_VAR = "JS_TS_MCP_POLITENESS_DISABLED"

#: REPO-SPECIFIC (2/3): where the robots.txt cache lives.  Same directory the
#: repo already uses for its ``cache.db`` (``cache.default_db_path``), so one
#: env var (``JS_TS_MCP_CACHE_DIR``) relocates both.  The legacy fallbacks
#: mirror ``cache.default_db_path``, which honoured them before this module
#: existed.
CACHE_DIR_ENV_VAR = "JS_TS_MCP_CACHE_DIR"
LEGACY_CACHE_DIR_ENV_VARS = (
    "FLUTTER_DOCS_MCP_CACHE_DIR",
    "JAVA_SPRING_MCP_CACHE_DIR",
    "PYTHON_DOCS_MCP_CACHE_DIR",
)
DEFAULT_CACHE_DIR = "~/.cache/js-ts-mcp"
ROBOTS_DB_NAME = "robots.db"

#: Status codes that mean "slow down" (A2 §4.3).
RATE_LIMIT_CODES = (429, 503, 504)

#: A robots.txt body bigger than this is treated as garbage (the docs.oracle.com
#: file is 158 KB; anything past this is not a robots file we should parse).
_MAX_ROBOTS_BYTES = 512 * 1024

_TRUTHY = {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #


#: REPO-SPECIFIC (3/3): the default robots-cache path.  The reference module
#: has no such helper — its callers pass ``robots_db_path`` explicitly — but
#: this repo has always defaulted its caches to ``$*_CACHE_DIR``, so the layer
#: gets the same treatment as :func:`js_ts_mcp.cache.default_db_path`.
def default_robots_db_path() -> str:
    """Absolute path of the persistent robots.txt cache DB.

    ``$JS_TS_MCP_CACHE_DIR/robots.db``, defaulting to
    ``~/.cache/js-ts-mcp/robots.db``.  Parent dirs are created lazily by
    :meth:`Politeness._open_db`, never here.
    """
    base = os.environ.get(CACHE_DIR_ENV_VAR)
    if not base:
        for legacy in LEGACY_CACHE_DIR_ENV_VARS:
            base = os.environ.get(legacy)
            if base:
                break
    base = base or DEFAULT_CACHE_DIR
    return os.path.join(os.path.expanduser(base), ROBOTS_DB_NAME)
# response container
# --------------------------------------------------------------------------- #
@dataclass
class PoliteResponse:
    """Result of :meth:`Politeness.get`.

    ``status_code`` is truthful: ``304`` when a conditional GET revalidated a
    cached body (``content`` then holds the cached bytes and ``from_cache`` is
    ``True``), and ``None`` when no HTTP response was obtained (blocked by
    robots, budget exhausted, transport failure, redirect loop).

    ``url`` is the **final** URL of the exchange — after a redirect chain resolved
    by the layer (F2) it is the last hop, not the URL that was passed to ``get()``.

    ``bot_challenge`` marks a response the caller-supplied ``challenge_detector``
    judged to be an anti-bot page (F4).  Such a response carries ``error`` set, so
    ``ok`` is ``False``; ``content`` is kept only for diagnostics and is never
    stored in the layer's body cache.
    """

    status_code: int | None
    content: bytes = b""
    url: str = ""
    validators: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False
    blocked_by_robots: bool = False
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    bot_challenge: bool = False

    @property
    def ok(self) -> bool:
        """We ended up with a usable body (fresh 2xx or a revalidated 304)."""
        if self.error is not None or self.blocked_by_robots:
            return False
        return self.status_code == 200 or self.from_cache

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# robots.txt: parsing + RFC 9309 matcher
# --------------------------------------------------------------------------- #
@dataclass
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]

    @property
    def pat_len(self) -> int:
        return len(self.pattern)


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float | None = None


@dataclass
class _Robots:
    groups: list[_Group]
    status: int
    parse_error: bool = False
    budget_denied: bool = False

    @property
    def empty(self) -> bool:
        return not self.groups


def _translate(path: str) -> tuple[str, bool]:
    """Translate one robots path pattern into a regex (``^``-anchored).

    ``*`` -> ``.*`` and a trailing ``$`` -> end-of-string, per RFC 9309 §2.2.3.
    A trailing bare ``?`` means "any URL that carries a query string": without
    the ``*`` appended, the ``?`` would be dropped by naive normalisation and
    ``Disallow: /*?`` would block the whole site (the bug crawl4ai works around
    in ``_preserve_bare_query``).  ``%2A`` / ``%24`` denote a *literal* ``*`` /
    ``$`` in the URI (RFC 9309 §2.2.3, Figure 6).
    """
    anchored = path.endswith("$")
    if anchored:
        path = path[:-1]
    if path.endswith("?"):
        path += "*"
    parts: list[str] = []
    i = 0
    while i < len(path):
        if path.startswith("%2A", i):
            parts.append(re.escape("*"))
            i += 3
        elif path.startswith("%24", i):
            parts.append(re.escape("$"))
            i += 3
        elif path[i] == "*":
            parts.append(".*")
            i += 1
        else:
            parts.append(re.escape(path[i]))
            i += 1
    return "".join(parts), anchored


def _compile_rule(allow: bool, raw: str) -> _Rule | None:
    raw = raw.strip()
    if not raw:  # empty "Disallow:" == allow everything
        return None
    body, anchored = _translate(raw)
    regex = re.compile("^" + body + (r"\Z" if anchored else ""), re.IGNORECASE)
    return _Rule(allow=allow, pattern=raw, regex=regex)


def parse_robots(text: str) -> list[_Group]:
    """Parse robots.txt into user-agent groups (RFC 9309 §2.2).

    Never raises: unparsable lines are skipped (RFC 9309 §2.3.1.5 — "Crawlers
    MUST use the parseable rules").  A JSON/HTML body (the ``registry.npmjs.org``
    trap: ``/robots.txt`` returns HTTP 200 with an npm packument) simply yields
    zero groups.
    """
    groups: list[_Group] = []
    current: _Group | None = None
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            agent = value.lower()
            # RFC 9309 §2.2.4: a Sitemap/other record must not terminate a group;
            # a new user-agent line after rules starts a new group.
            if current is None or current.rules:
                current = _Group()
                groups.append(current)
            current.agents.append(agent)
        elif current is None:
            continue  # rule before the first user-agent line -> ignore
        elif key in ("allow", "disallow"):
            rule = _compile_rule(key == "allow", value)
            if rule is not None:
                current.rules.append(rule)
        elif key in ("crawl-delay", "crawldelay"):
            try:
                delay = float(value)
            except ValueError:
                continue
            if delay >= 0:
                current.crawl_delay = delay
        # sitemap / host / unknown records -> ignored on purpose
    return groups


def _select_groups(groups: list[_Group], user_agent: str) -> list[_Group]:
    """Pick the group(s) that govern us (RFC 9309 §2.2.1).

    The robots ``User-agent:`` value is a *product token* that should appear as a
    substring of our UA header.  We try, in order: exact match against the UA
    product token (``UA.split("/")[0]``), then longest substring match, then
    ``*``.  Every matching group is merged — ``docs.spring.io`` ships two
    ``User-agent: *`` blocks (``crawl-delay`` in one, ``Disallow`` in the other)
    and honouring only the first would drop one of them.
    """
    ua_low = user_agent.lower()
    token = ua_low.split("/")[0].strip()
    exact = [g for g in groups if token and token in g.agents]
    if not exact:
        hits = [(len(a), g) for g in groups for a in g.agents if a not in ("", "*") and a in ua_low]
        if hits:
            longest = max(n for n, _ in hits)
            exact = [g for n, g in hits if n == longest]
    if not exact:
        exact = [g for g in groups if "*" in g.agents]
    return exact


def _match_rules(rules: list[_Rule], target: str) -> tuple[bool, str]:
    """RFC 9309 §2.2.2 + §5.2: most octets wins; on a tie Allow beats Disallow.

    Returns ``(allowed, reason)``.  Pattern length is the secondary key so that
    ``/pypi/*/json`` (12) loses to ``/pypi/dio/json`` (14) even though both match
    all 14 octets of the target.
    """
    best: tuple[int, int, int] | None = None
    winner: _Rule | None = None
    for rule in rules:
        m = rule.regex.match(target)
        if not m:
            continue
        key = (m.end(), rule.pat_len, 1 if rule.allow else 0)
        if best is None or key > best:
            best, winner = key, rule
    if winner is None:
        return True, "no rule matched"
    kind = "Allow" if winner.allow else "Disallow"
    return winner.allow, f"{kind}: {winner.pattern}"


# --------------------------------------------------------------------------- #
# per-host politeness state
# --------------------------------------------------------------------------- #
@dataclass
class _HostState:
    next_allowed: float = 0.0
    current_delay: float = 0.0
    #: Monotonic start of the slot this host last reserved in :meth:`_wait_turn`.
    #: A13 B2: when a robots.txt fetch *learns* a ``Crawl-delay`` it had not known
    #: before, the delay is measured from this instant, so the request that
    #: triggered the fetch is gated by it too — not only the next one.
    last_slot_start: float = 0.0


class Politeness:
    """robots.txt + throttle + Retry-After + conditional GET + request budget.

    >>> p = Politeness("python-docs-mcp/0.1 (+https://github.com/KEEPEE/…)")
    >>> p.can_fetch("https://pypi.org/pypi/dio/json")          # robots-disallowed
    False
    >>> r = p.get(client, "https://docs.python.org/3/library/json.html",
    ...           validators={"etag": '"abc"'})
    >>> r.ok, r.from_cache, r.validators
    (True, False, {'etag': '"…"', 'last_modified': '…'})
    """

    def __init__(
        self,
        user_agent: str,
        cache_path: str | None = None,
        base_delay: tuple[float, float] = (0.35, 0.9),
        robots_ttl: float = 7 * 24 * 3600,
        max_retry_on_429: int = 1,
        backoff_cap: float = 30.0,
        timeouts: tuple[float, float, float] = (5.0, 10.0, 15.0),
        *,
        robots_timeout: float = 2.0,
        max_retry_on_transport: int = 1,
        disable_env_var: str | None = DEFAULT_DISABLE_ENV_VAR,
        allowed_hosts: frozenset[str] | None = None,
        stall_after: float = 10.0,
        max_cached_bodies: int = 64,
        max_cached_bytes: int = 8 * 1024 * 1024,
        max_redirects: int = 5,
        challenge_detector: Callable[[bytes, httpx.Headers], bool] | None = None,
        max_retry_on_challenge: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.base_delay = base_delay
        self.robots_ttl = float(robots_ttl)
        self.max_retry_on_429 = int(max_retry_on_429)
        self.backoff_cap = float(backoff_cap)
        self.timeouts = timeouts
        self.robots_timeout = float(robots_timeout)
        # 1 retry == 2 attempts total: keeps today's "retry once" behaviour of the
        # four repos, but now with backoff instead of an immediate second hit.
        self.max_retry_on_transport = int(max_retry_on_transport)
        self.allowed_hosts = allowed_hosts
        self.stall_after = float(stall_after)
        self.max_cached_bodies = int(max_cached_bodies)
        self.max_cached_bytes = int(max_cached_bytes)
        # F2: the layer walks redirect hops itself (robots check per hop).  5 is the
        # same ceiling the robots fetch uses (RFC 9309 §2.3.1.2).
        self.max_redirects = int(max_redirects)
        # F4: optional, caller-supplied predicate.  None (the default) means the
        # layer behaves exactly as before A8.  A repository supplies its own
        # detector — e.g. python-docs-mcp checks for PyPI's JS "Client Challenge".
        # Contract: ``detector(body, headers) -> bool``; it must be cheap, must not
        # raise (a raising detector is treated as "not a challenge") and must not
        # do network I/O.  The reference ships no site-specific heuristic.
        self.challenge_detector = challenge_detector
        self.max_retry_on_challenge = int(max_retry_on_challenge)
        # A11 F5 — the two clocks have strictly separate jobs:
        #   _clock (monotonic): per-host ``next_allowed``, backoff sleeps, stall
        #     timing.  Never persisted, never compared to anything stored.  A
        #     wall-clock NTP correction must not be able to make the throttle wait
        #     for years (or skip the wait entirely).
        #   _wall (wall clock): every timestamp written to SQLite and every TTL /
        #     freshness decision.  A monotonic number is boot-relative, so it is
        #     meaningless in a database that outlives the boot.
        self._clock = clock
        self._sleep = sleep
        self._wall = wall_clock
        self._rng = rng or random.Random()
        self.disabled = bool(
            disable_env_var and os.environ.get(disable_env_var, "").strip().lower() in _TRUTHY
        )
        self.timeout = httpx.Timeout(timeouts[2], connect=timeouts[0], read=timeouts[1])

        self._lock = threading.RLock()
        self._hosts: dict[str, _HostState] = {}
        self._parsed: dict[str, _Robots] = {}
        self._bodies: "OrderedDict[str, tuple[bytes, dict[str, str]]]" = OrderedDict()
        self._body_bytes = 0
        self._budgets: dict[str, list[int]] = {}
        self._n = {
            # content requests actually put on the wire (robots fetches are NOT
            # included here — see robots_requests; A5 §4.10 / A7 §4.3)
            "requests": 0,
            "robots_fetches": 0,
            "robots_requests": 0,
            "robots_cache_hits": 0,
            "robots_negative": 0,
            "robots_refreshed_unchanged": 0,
            "robots_throttle_waits": 0,
            "robots_throttle_sleep_s": 0.0,
            "blocked_by_robots": 0,
            "throttle_waits": 0,
            "throttle_sleep_s": 0.0,
            # A13 B2: how many times a freshly learned Crawl-delay had to be
            # pushed into the queue after the robots fetch that revealed it.
            "crawl_delay_applied": 0,
            "retries_429": 0,
            "retries_transport": 0,
            "retry_after_honoured": 0,
            "conditional": 0,
            "conditional_skipped": 0,
            "revalidated_304": 0,
            "redirect_hops": 0,
            "challenge_detected": 0,
            "challenge_retries": 0,
            "stalls": 0,
            "budget_denied": 0,
            "errors": 0,
        }
        self._db = self._open_db(cache_path)

    # ------------------------------------------------------------------ #
    # storage
    # ------------------------------------------------------------------ #
    @staticmethod
    def _open_db(cache_path: str | None) -> sqlite3.Connection:
        path = cache_path or ":memory:"
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        db = sqlite3.connect(path, check_same_thread=False)
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:  # pragma: no cover - WAL unsupported on :memory:
            pass
        db.execute(
            "CREATE TABLE IF NOT EXISTS robots ("
            "host TEXT PRIMARY KEY, status INTEGER NOT NULL, "
            "content TEXT NOT NULL, fetched_at REAL NOT NULL)"
        )
        # ``fetched_at`` is epoch seconds from the *wall* clock (A11 F5).  The
        # schema is unchanged from pre-A11 databases on purpose: legacy rows hold
        # a monotonic number, and :meth:`_row_is_fresh` expires them instead of
        # trusting them, so no ALTER TABLE / migration script is needed.
        db.commit()
        return db

    def close(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def can_fetch(self, url: str, *, client: httpx.Client | None = None) -> bool:
        """Robots decision for *url*.  Fail-open, never raises."""
        try:
            allowed, _reason, _kind = self._robots_decision(url, client)
            return allowed
        except Exception:
            return True  # fail-open: a broken politeness layer must not break a tool

    def get(
        self,
        client: httpx.Client,
        url: str,
        *,
        validators: dict | None = None,
        cached_body: bytes | None = None,
        budget_scope: str | None = None,
        budget_limit: int = 4,
    ) -> PoliteResponse:
        """Throttled, robots-aware, retry-aware GET.  Never raises."""
        try:
            return self._get(client, url, validators, cached_body, budget_scope, budget_limit)
        except Exception as exc:  # the layer must never leak an exception
            self._n["errors"] += 1
            return PoliteResponse(None, b"", url, error=f"politeness failure: {exc!r}")

    def budget(self, scope: str, limit: int) -> bool:
        """Consume one unit of *scope*; ``False`` once *limit* is reached.

        Use a fresh scope id per tool call (``f"tool:{call_id}"``) or per index
        build (``"index:api.flutter.dev"``); ``reset_budget()`` clears them.
        """
        with self._lock:
            used = self._budgets.setdefault(scope, [0, int(limit)])
            used[1] = int(limit)
            if used[0] >= used[1]:
                self._n["budget_denied"] += 1
                return False
            used[0] += 1
            return True

    def reset_budget(self, scope: str | None = None) -> None:
        with self._lock:
            if scope is None:
                self._budgets.clear()
            else:
                self._budgets.pop(scope, None)

    def stats(self) -> dict:
        """Counters for a ``*_status``-style tool.

        Reading them (A5 §4.10, A7 §4.3 — and the A8 additions):

        * ``requests`` = **content** requests on the wire; ``robots_requests`` =
          robots.txt attempts (including failed ones).  ``requests +
          robots_requests`` is the true wire total.
        * ``throttle_waits`` / ``throttle_sleep_s`` cover *every* polite wait,
          including the robots ones; ``robots_throttle_waits`` /
          ``robots_throttle_sleep_s`` are the robots subset (F1).
        * ``hosts`` = per-host *escalated* delay (only 429/stall/transport move it;
          ``Crawl-delay`` is a floor applied per wait, not stored here — but a
          delay learned from a robots fetch does push ``next_allowed``, which is
          what ``crawl_delay_applied`` counts, A13 B2).
        * ``budgets`` = ``{scope: [used, limit]}`` where ``used`` counts **every
          attempt** — robots fetch, retries, redirect hops (F3).
        * ``redirect_hops`` = 3xx hops the layer followed itself (F2).
        * ``challenge_detected`` / ``challenge_retries`` = anti-bot pages seen and
          retried through the optional detector (F4).
        """
        with self._lock:
            return {
                "disabled": self.disabled,
                **{k: v for k, v in self._n.items()},
                "hosts": {h: round(s.current_delay, 3) for h, s in self._hosts.items()},
                "budgets": {k: list(v) for k, v in self._budgets.items()},
                "cached_bodies": len(self._bodies),
            }

    # ------------------------------------------------------------------ #
    # robots
    # ------------------------------------------------------------------ #
    @staticmethod
    def _host_of(url: str) -> tuple[str, str, str]:
        u = httpx.URL(url)
        if not u.host:
            raise ValueError(f"not an absolute http(s) URL: {url!r}")
        if u.scheme.lower() not in ("http", "https"):
            raise ValueError(f"unsupported scheme {u.scheme!r}")
        netloc = u.netloc.decode() if isinstance(u.netloc, bytes) else str(u.netloc)
        return netloc.lower(), u.path or "/", (u.query.decode() if isinstance(u.query, bytes) else str(u.query or ""))

    def _robots_url(self, netloc: str, scheme: str = "https") -> str:
        return f"{scheme}://{netloc}/robots.txt"

    def _load_robots_row(self, host: str) -> tuple[int, str, float] | None:
        cur = self._db.execute(
            "SELECT status, content, fetched_at FROM robots WHERE host = ?", (host,)
        )
        row = cur.fetchone()
        return (int(row[0]), row[1], float(row[2])) if row else None

    def _store_robots_row(self, host: str, status: int, content: str, fetched_at: float) -> None:
        # NOTE (crawl4ai bug #1): always write, even when content is unchanged —
        # otherwise fetched_at stays stale and robots.txt is re-fetched forever.
        #
        # A11 F5: ``fetched_at`` is an **epoch-second wall-clock** timestamp (the
        # caller passes ``self._wall()``).  This column outlives the process and
        # often the boot, so a ``time.monotonic()`` value stored here is garbage:
        # after a reboot it reads as "the future" and the row never expires.
        self._db.execute(
            "INSERT OR REPLACE INTO robots (host, status, content, fetched_at) VALUES (?,?,?,?)",
            (host, status, content, fetched_at),
        )
        self._db.commit()

    def _ttl_for(self, status: int) -> float:
        # A transport-level failure is a *soft* negative: keep it short so a
        # transient DNS/connect outage is retried, but not on every call.
        return min(self.robots_ttl, 300.0) if status == 0 else self.robots_ttl

    @staticmethod
    def _row_is_fresh(fetched_at: float, now_wall: float, ttl: float) -> bool:
        """Is a persisted robots row still fresh?  Wall clock only (A11 F5).

        The age must land inside ``[0, ttl)`` — both bounds carry weight:

        * ``age >= ttl`` — normal expiry.  This single comparison is also what
          migrates a pre-A11 database: a ``time.monotonic()`` stamp (seconds since
          boot, 1e0..1e8) sits decades away from a real wall clock (≈1.7e9), so a
          legacy row is "far from now" and expires on first sight instead of
          looking fresh forever.
        * ``age < 0`` — the stamp is from the future: the wall clock stepped
          backwards (NTP correction) or the row holds a monotonic value from a
          *later* boot.  The pre-A11 test was only ``age < ttl``, which made such
          a row immortal and froze the rules.  An untrustworthy age is treated as
          expired: one extra robots.txt request is cheap, a permanently stale rule
          set is not.
        """
        age = now_wall - fetched_at
        return 0.0 <= age < ttl

    def _fetch_robots(self, client: httpx.Client | None, robots_url: str, floor: float) -> tuple[int, str]:
        """Fetch robots.txt **through the per-host throttle** (A8 F1).

        robots.txt is implicitly allowed by RFC 9309 §2.2.2, so there is no
        chicken-and-egg problem in queueing it: it is just another request on the
        same host and must not jump the line.  Before A8 this path bypassed
        ``_wait_turn`` entirely, which is why A7 measured the *minimum* inter-
        request gap falling from 388/434 ms to 15/12 ms once the layer was wired
        in (the robots request sat directly against the page request).

        ``floor`` is the ``Crawl-delay`` already known for that host (0.0 on the
        very first fetch — we cannot know a rule we have not downloaded yet; on a
        TTL refresh the previous value still applies).  That is not a licence to
        ignore the delay we are about to learn: :meth:`_robots_for` calls
        :meth:`_apply_crawl_delay` right after parsing, which pushes the host's
        ``next_allowed`` to ``robots slot + Crawl-delay`` (A13 B2).
        """
        netloc = self._host_of(robots_url)[0]
        if not self.disabled:
            self._wait_turn(netloc, floor, robots=True)
        if client is not None:
            req = client.build_request("GET", robots_url, timeout=httpx.Timeout(self.robots_timeout))
            self._n["robots_requests"] += 1
            # Redirects of robots.txt itself are followed by httpx (RFC 9309
            # §2.3.1.2); they are bounded by the client's max_redirects and are
            # covered by the single budget unit charged for this fetch.
            resp = client.send(req)
            return resp.status_code, resp.text
        with httpx.Client(
            timeout=httpx.Timeout(self.robots_timeout),
            headers={"user-agent": self.user_agent},
            follow_redirects=True,
            max_redirects=self.max_redirects,  # RFC 9309 §2.3.1.2
        ) as own:
            self._n["robots_requests"] += 1
            resp = own.get(robots_url)
            return resp.status_code, resp.text

    def _robots_for(
        self,
        netloc: str,
        client: httpx.Client | None,
        budget: tuple[str, int] | None = None,
    ) -> _Robots:
        """Cached robots for *netloc*; refreshes at most once per TTL.

        The TTL clock is the wall clock (A11 F5) — see :meth:`_row_is_fresh`.

        *budget* ``(scope, limit)`` is supplied by :meth:`get` only: a robots.txt
        fetch is a real request on the wire, so it pays into the same scope budget
        as the content request it serves (A8 F3).  ``can_fetch()`` has no scope and
        therefore stays unbudgeted, exactly as before.
        """
        # A11 F5: freshness is a wall-clock question, so it is answered with the
        # wall clock.  ``self._clock()`` (monotonic) must never appear here — its
        # value is boot-relative and this comparison reads a row that was written
        # in an earlier boot.
        now_wall = self._wall()
        row = self._load_robots_row(netloc)
        if row is not None and self._row_is_fresh(row[2], now_wall, self._ttl_for(row[0])):
            self._n["robots_cache_hits"] += 1
            robots = self._parsed.get(netloc)
            if robots is None:
                robots = _Robots(parse_robots(row[1]), row[0])
                self._parsed[netloc] = robots
            return robots
        if netloc in self._parsed and row is not None:
            pass  # stale: fall through to refresh

        if budget is not None and not self.budget(budget[0], budget[1]):
            # F3: refuse the robots fetch too, instead of silently spending an
            # unbudgeted request and then reporting the content request as the
            # only one over budget.
            return _Robots([], 0, budget_denied=True)

        status, content = 0, ""
        floor = self._crawl_delay(self._parsed.get(netloc, _Robots([], 0)), netloc)
        try:
            status, content = self._fetch_robots(client, self._robots_url(netloc), floor)
            self._n["robots_fetches"] += 1
        except Exception:
            # RFC 9309 §2.3.1.4 would say "complete disallow" for 5xx/unreachable;
            # the contract (and A1/A2) choose fail-open for docs domains.
            self._n["robots_negative"] += 1
            self._store_robots_row(netloc, 0, "", now_wall)
            self._parsed[netloc] = _Robots([], 0, parse_error=True)
            return self._parsed[netloc]

        if len(content) > _MAX_ROBOTS_BYTES:
            content, status = "", 0
            self._n["robots_negative"] += 1
        elif not (200 <= status < 300):
            # 4xx/5xx -> "unavailable" (RFC 9309 §2.3.1.3): no rules, but cached.
            self._n["robots_negative"] += 1
            content = ""
        elif row is not None and row[1] == content:
            self._n["robots_refreshed_unchanged"] += 1

        self._store_robots_row(netloc, status, content, now_wall)
        try:
            groups = parse_robots(content)
            parse_error = False
        except Exception:  # pragma: no cover - parser is defensive already
            groups, parse_error = [], True
            self._n["robots_negative"] += 1
        robots = _Robots(groups, status, parse_error)
        self._parsed[netloc] = robots
        # A13 B2: a Crawl-delay that was *not* known when this fetch queued is
        # now applied retroactively to the slot queue, counted from the robots
        # request itself — so the request that triggered the fetch obeys it too.
        self._apply_crawl_delay(netloc, robots)
        return robots

    def _crawl_delay(self, robots: _Robots, netloc: str) -> float:
        groups = _select_groups(robots.groups, self.user_agent)
        delays = [g.crawl_delay for g in groups if g.crawl_delay is not None]
        return max(delays) if delays else 0.0

    def _robots_decision(
        self, url: str, client: httpx.Client | None, budget: tuple[str, int] | None = None
    ) -> tuple[bool, str, str]:
        if self.disabled:
            return True, "politeness disabled", "disabled"
        netloc, path, query = self._host_of(url)
        if self.allowed_hosts is not None and netloc not in self.allowed_hosts:
            return False, f"host {netloc} is not in ALLOWED_HOSTS", "allowlist"
        if path == "/robots.txt":
            return True, "robots.txt is implicitly allowed", "ok"  # RFC 9309 §2.2.2
        robots = self._robots_for(netloc, client, budget=budget)
        if robots.budget_denied:
            scope = budget[0] if budget else "?"
            return False, f"request budget exhausted for scope {scope!r} (robots fetch)", "budget"
        if robots.empty:
            return True, "no robots rules", "ok"
        groups = _select_groups(robots.groups, self.user_agent)
        rules = [r for g in groups for r in g.rules]
        target = path + (f"?{query}" if query else "")
        allowed, reason = _match_rules(rules, target)
        return allowed, reason, ("robots" if not allowed else "ok")

    # ------------------------------------------------------------------ #
    # throttle
    # ------------------------------------------------------------------ #
    def _jitter_base(self) -> float:
        return self._rng.uniform(self.base_delay[0], self.base_delay[1])

    def _host_state(self, host: str) -> _HostState:
        st = self._hosts.get(host)
        if st is None:
            st = _HostState()
            self._hosts[host] = st
        return st

    def _wait_turn(self, host: str, floor: float, *, robots: bool = False) -> float:
        """Serialise requests per host: sleep until ``next_allowed``.

        The slot is *reserved* under the lock and the sleep happens outside it,
        so the bookkeeping stays correct if a caller ever goes multi-threaded
        (today all four repos are sync, i.e. one in-flight request per host).

        ``robots=True`` only labels the counters (A8 F1): a robots.txt request
        waits in exactly the same queue as a page request.

        Returns the reserved slot's start and records it on the host as
        ``last_slot_start`` — the anchor :meth:`_apply_crawl_delay` needs (A13 B2).
        """
        with self._lock:
            st = self._host_state(host)
            delay = max(self._jitter_base(), floor, st.current_delay)
            now = self._clock()
            start = max(now, st.next_allowed)
            st.next_allowed = start + delay
            st.last_slot_start = start
        wait = start - now
        if wait > 0:
            self._sleep(wait)
            self._n["throttle_waits"] += 1
            self._n["throttle_sleep_s"] += wait
            if robots:
                self._n["robots_throttle_waits"] += 1
                self._n["robots_throttle_sleep_s"] += wait
        return start

    def _apply_crawl_delay(self, host: str, robots: _Robots) -> float:
        """Push ``next_allowed`` out to cover a ``Crawl-delay`` we just learned.

        A13 B2 (A12 F-A12-1).  The first robots.txt fetch of a host cannot know
        the ``Crawl-delay`` it is about to download, so it reserves a slot of
        ``max(jitter, 0)`` only (see :meth:`_fetch_robots`).  Before A13 the
        freshly parsed delay then took effect one request too late: the content
        request that triggered the fetch computed ``floor = 1.0`` but
        ``start = max(now, next_allowed)`` was still bounded by that small
        reservation, so on ``docs.spring.io`` (``crawl-delay: 1``) A12 measured
        the first gap at 365–857 ms while every later gap was exactly 1000 ms.

        RFC 9309 §4 defines ``Crawl-delay`` as the interval between *successive*
        requests, so the interval must be counted from the request that is
        already on the wire — the robots fetch.  Anchoring on
        ``last_slot_start`` (the moment that fetch was allowed to start) makes
        the very next request honour it.  When the delay was already known the
        robots fetch queued with it as its floor, so this is a no-op ``max()``.

        Returns the delay that was applied (0.0 when the host declares none).
        """
        delay = self._crawl_delay(robots, host)
        if delay <= 0.0:
            return 0.0
        with self._lock:
            st = self._host_state(host)
            if st.last_slot_start <= 0.0:
                # Nothing of ours is on the wire yet (robots came from a warm
                # file cache in a fresh process) — there is no earlier request
                # to space the delay from, and the next request will queue with
                # the delay as its floor anyway.
                return 0.0
            target = st.last_slot_start + delay
            if target > st.next_allowed:
                st.next_allowed = target
                self._n["crawl_delay_applied"] += 1
        return delay

    def _escalate(self, st: _HostState) -> float:
        """Exponential backoff with jitter, capped (crawl4ai RateLimiter shape)."""
        base = st.current_delay or self._jitter_base()
        new = min(base * 2.0 * self._rng.uniform(0.75, 1.25), self.backoff_cap)
        st.current_delay = new
        return new

    def _retry_after(self, headers: httpx.Headers) -> float | None:
        raw = headers.get("retry-after")
        if raw is None:
            return None
        raw = raw.strip()
        try:
            seconds = float(raw)
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return None
            if dt is None:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            seconds = (dt.timestamp() - self._wall())
        return max(0.0, min(seconds, self.backoff_cap))

    # ------------------------------------------------------------------ #
    # conditional GET helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validators_of(headers: httpx.Headers) -> dict[str, str]:
        out: dict[str, str] = {}
        etag = headers.get("etag")
        last_modified = headers.get("last-modified")
        if etag:
            out["etag"] = etag
        if last_modified:
            out["last_modified"] = last_modified
        return out

    def _remember_body(self, url: str, content: bytes, validators: dict[str, str]) -> None:
        """Keep the body so a later conditional GET can be answered from memory.

        Bounded by *count* and *bytes*: the js-ts repo pulls 7 MB npm packuments,
        so an unbounded cache would be a memory leak.  Oversized bodies are not
        kept at all — the repo's own SQLite ``DocCache`` stays the source of truth.
        """
        if len(content) > self.max_cached_bytes:
            return
        self._body_bytes += len(content)
        self._bodies[url] = (content, validators)
        while len(self._bodies) > self.max_cached_bodies or self._body_bytes > self.max_cached_bytes:
            if not self._bodies:
                break
            _k, (evicted, _v) = self._bodies.popitem(last=False)
            self._body_bytes -= len(evicted)

    # ------------------------------------------------------------------ #
    # redirect + challenge helpers (A8 F2 / F4)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _redirect_target(current: str, response: httpx.Response) -> str | None:
        """Absolute URL of a 3xx ``Location``, or ``None`` when there is none.

        Relative and absolute-path targets are resolved against the URL that was
        requested (RFC 9110 §10.2.2).  A malformed Location never raises — the
        caller then reports the 3xx as the final answer.
        """
        location = response.headers.get("location")
        if not location or not location.strip():
            return None
        try:
            target = httpx.URL(current).join(location.strip())
            if target.scheme.lower() not in ("http", "https"):
                return None  # never follow a redirect out of the web (no ftp/file/…)
            return str(target)
        except (httpx.InvalidURL, ValueError):
            return None

    def _is_challenge(self, content: bytes, headers: httpx.Headers) -> bool:
        """Run the caller-supplied anti-bot predicate defensively (A8 F4).

        The reference contains **no site-specific heuristic**.  A repository
        detector should combine measurable criteria, e.g.: body shorter than a few
        KB, presence of a JS challenge script, and the *absence* of the marker the
        real page must contain.  A detector that raises is treated as "not a
        challenge" — the layer must never break a tool.
        """
        if self.challenge_detector is None:
            return False
        try:
            return bool(self.challenge_detector(content, headers))
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # the request pipeline
    # ------------------------------------------------------------------ #
    def _get(
        self,
        client: httpx.Client,
        url: str,
        validators: dict | None,
        cached_body: bytes | None,
        budget_scope: str | None,
        budget_limit: int,
    ) -> PoliteResponse:
        netloc, _path, _query = self._host_of(url)
        # A8 F3: the scope is threaded through the robots path too, so the
        # robots.txt fetch that serves this call pays for itself.
        budget: tuple[str, int] | None = (
            (budget_scope, budget_limit) if budget_scope is not None else None
        )

        allowed, reason, kind = self._robots_decision(url, client, budget=budget)
        if not allowed:
            if kind == "robots":
                self._n["blocked_by_robots"] += 1
                return PoliteResponse(
                    None,
                    b"",
                    url,
                    blocked_by_robots=True,
                    error=f"blocked by robots.txt for {netloc} — {reason} matches {url}",
                )
            # allowlist / budget denial: a refusal, but not a robots verdict
            return PoliteResponse(None, b"", url, error=reason)

        stored = self._bodies.get(url)
        body_source: bytes | None = cached_body if cached_body is not None else (
            stored[0] if stored else None
        )
        cond: dict[str, str] = {}
        if validators and body_source is not None:
            etag = validators.get("etag")
            last_modified = validators.get("last_modified")
            if etag:
                cond["If-None-Match"] = etag
            if last_modified:
                cond["If-Modified-Since"] = last_modified
            if cond:
                self._n["conditional"] += 1
        elif validators:
            # No body to serve -> a 304 would be useless; skip conditional GET.
            self._n["conditional_skipped"] += 1

        rate_retries = 0
        transport_retries = 0
        challenge_retries = 0
        hops = 0
        current = url

        while True:
            # A8 F2: ``current`` changes on every hop, and with it the host whose
            # robots rules, crawl-delay and throttle queue apply.
            netloc = self._host_of(current)[0]
            st = self._host_state(netloc)
            floor = self._crawl_delay(self._parsed.get(netloc, _Robots([], 0)), netloc)

            # A8 F3: one budget unit per *attempt on the wire* — the first try, a
            # 429/503 retry, a transport retry and every redirect hop.  Before A8
            # one get() cost exactly one unit, so "limit 4" on an error path meant
            # 8+ real requests (A5 §4.4).
            if budget is not None and not self.budget(budget[0], budget[1]):
                return PoliteResponse(
                    None, b"", current,
                    error=f"request budget exhausted for scope {budget[0]!r}",
                )

            if not self.disabled:
                self._wait_turn(netloc, floor)
            headers = {"user-agent": self.user_agent, **cond}
            request = client.build_request("GET", current, headers=headers, timeout=self.timeout)
            started = self._clock()
            try:
                self._n["requests"] += 1
                # A8 F2: the layer owns redirects.  ``follow_redirects=False``
                # overrides even a client built with follow_redirects=True, so a
                # 3xx target is never fetched before it has been checked against
                # that host's robots rules (A5 §4.5: Oracle answered 302 and the
                # target was never re-examined).
                response = client.send(request, follow_redirects=False)
            except httpx.TransportError as exc:
                transport_retries += 1
                self._n["retries_transport"] += 1
                st.current_delay = min(
                    (st.current_delay or self._jitter_base()) * 2.0 * self._rng.uniform(0.75, 1.25),
                    self.backoff_cap,
                )
                if transport_retries > self.max_retry_on_transport:
                    self._n["errors"] += 1
                    return PoliteResponse(
                        None, b"", current, error=f"transport error after {transport_retries} retries: {exc!r}"
                    )
                self._sleep(st.current_delay)
                continue
            duration = self._clock() - started

            if self.stall_after > 0 and duration > self.stall_after:
                # A2 §4.3: search.maven.org "quietly stalls" (30-40 s answers).
                # Treat it like a soft rate limit: escalate, do not retry.
                self._n["stalls"] += 1
                st.current_delay = min(
                    (st.current_delay or self._jitter_base()) * 2.0 * self._rng.uniform(0.75, 1.25),
                    self.backoff_cap,
                )

            status = response.status_code
            # Validators belong to the URL they were received for.  After a hop the
            # caller's validators for the *original* URL must not be re-attached to
            # the target's response.
            resp_validators = self._validators_of(response.headers) or (
                (validators or {}) if hops == 0 else {}
            )

            if status == 304 and cond:
                self._n["revalidated_304"] += 1
                self._remember_body(current, body_source or b"", resp_validators)
                return PoliteResponse(
                    304,
                    body_source or b"",
                    str(response.url or current),
                    validators=resp_validators,
                    from_cache=True,
                    headers=dict(response.headers),
                )

            # ---- A8 F2: resolve the redirect here, one hop at a time ---------- #
            if 300 <= status < 400:
                target = self._redirect_target(current, response)
                if target is None:
                    # 3xx without a usable Location: nothing to follow, report it.
                    return PoliteResponse(
                        status, response.content, str(response.url or current),
                        validators=resp_validators, headers=dict(response.headers),
                    )
                if hops >= self.max_redirects:
                    # An error, never an exception (and never a silent truncation).
                    self._n["errors"] += 1
                    return PoliteResponse(
                        None, b"", current,
                        error=f"too many redirects (max {self.max_redirects}) while resolving "
                              f"{url!r}; last hop {current!r} -> {target!r}",
                    )
                hops += 1
                self._n["redirect_hops"] += 1
                target_netloc = self._host_of(target)[0]
                allowed, reason, kind = self._robots_decision(target, client, budget=budget)
                if not allowed:
                    if kind == "robots":
                        self._n["blocked_by_robots"] += 1
                        return PoliteResponse(
                            None, b"", target,
                            blocked_by_robots=True,
                            error=f"blocked by robots.txt for {target_netloc} — {reason} matches {target} "
                                  f"(redirect target of {url})",
                        )
                    return PoliteResponse(None, b"", target, error=reason)
                # Validators belong to the URL they came from; a hop gets none.
                cond = {}
                current = target
                continue

            if status in RATE_LIMIT_CODES and rate_retries < self.max_retry_on_429:
                rate_retries += 1
                self._n["retries_429"] += 1
                wait = self._retry_after(response.headers)
                if wait is not None:
                    self._n["retry_after_honoured"] += 1
                else:
                    wait = self._escalate(st)
                st.next_allowed = self._clock() + wait
                self._sleep(wait)
                continue

            content = response.content
            headers_out = dict(response.headers)

            # ---- A8 F4: optional anti-bot / JS-challenge detection ----------- #
            is_challenge = False
            if 200 <= status < 300 and self.challenge_detector is not None:
                is_challenge = self._is_challenge(content, response.headers)
                if is_challenge:
                    self._n["challenge_detected"] += 1
            if is_challenge and challenge_retries < self.max_retry_on_challenge:
                # One decent retry, spaced by the escalated per-host delay — never
                # an immediate re-hit, and never a different (robots-disallowed)
                # endpoint: the retry re-requests exactly this URL.
                challenge_retries += 1
                self._n["challenge_retries"] += 1
                wait = self._escalate(st)
                st.next_allowed = max(st.next_allowed, self._clock() + wait)
                self._sleep(wait)
                continue
            if is_challenge:
                # Readable failure instead of parsing a 3 KB challenge page as if
                # it were the document (A6 §5: "404 vs challenge").  The body is
                # returned for diagnostics but deliberately not cached.
                return PoliteResponse(
                    status, content, str(response.url or current),
                    validators=resp_validators, headers=headers_out, bot_challenge=True,
                    error=f"HTTP {status} from {netloc} looks like an anti-bot challenge page "
                          f"({len(content)} bytes), not the document; retried "
                          f"{challenge_retries}x and it is still a challenge",
                )

            if 200 <= status < 300:
                self._remember_body(current, content, resp_validators)
                if not self.disabled:
                    st.current_delay = max(0.0, st.current_delay * 0.75)  # slow decay
            return PoliteResponse(
                status,
                content,
                str(response.url or current),
                validators=resp_validators,
                headers=headers_out,
            )

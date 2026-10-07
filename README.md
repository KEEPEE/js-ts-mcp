# js-ts-mcp

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that gives AI coding agents **live MDN Web Docs pages, the TypeScript handbook and npm package information** instead of whatever the model happens to remember. Pages are fetched at request time, cached locally, and returned as clean markdown — so generated JavaScript and TypeScript target real, current APIs rather than deprecated or invented ones.

- **MDN Web Docs** — any docs page fetched live by its slug (e.g. `Web/JavaScript/Reference/Global_Objects/Array`) and converted to markdown
- **TypeScript handbook** — all 117 handbook pages (e.g. `intro`, `2/generics`) scraped live from typescriptlang.org
- **npm packages** — registry metadata + README from the npm registry, optionally pinned to a version (see [npm source](#npm-metadata-source-and-its-robotstxt-trap))
- **Local search index** over 14,708 MDN docs (from the en-US sitemap) + 117 TypeScript handbook pages (name + path recorded, so `Promise` resolves to the right page on its own), rebuilt at most every 7 days with a stale fallback when the network fails
- **SQLite TTL cache** so repeated lookups are instant
- **Politeness layer** — robots.txt (RFC 9309), per-host throttle, `Retry-After`, conditional GET, request budgets and a host allowlist ([details](#caching--politeness))

## The five tools

| Tool | What it does |
|---|---|
| `js_docs` | Resolve one identifier (`mdn:…`, `ts:…`, or a plain name like `Promise`) to a single docs page as markdown. |
| `js_search` | Ranked multi-token search over the local index of 14,825 MDN + TypeScript entries (name **and** path). |
| `npm_package` | npm package metadata + README: resolved version, description, license, homepage, repository URL, keywords, engines, dependencies. npm no longer ships a usable README in the packument, so the document is fetched from the package's GitHub repository. |
| `js_status` | Real health check: index size/age, cache stats, live probes of all three hosts, and politeness counters. |
| `health_check` | Server liveness and version. |

Every tool returns a plain dict. Failures come back as `{"ok": false, "error": …, "suggestion": …}` — a bad lookup never raises into the MCP layer, and no tool delegates to another tool.

## Requirements

- Python 3.10 or newer (developed and tested on 3.12)
- [`uv`](https://docs.astral.sh/uv/) for the one-command install below (`uvx` ships with it)
- The MCP Python SDK **1.x** is pinned (`mcp>=1.2.0,<2.0`); this package does not support the mcp 2.x rename of `FastMCP`
- `httpx` is the only HTTP transport. There is no `curl` fallback and no browser user-agent masking — [why](#one-transport-no-curl-fallback-no-browser-user-agent-masking)

## Install & run

### One command — no checkout, no token

```bash
uvx --from git+https://github.com/KEEPEE/js-ts-mcp.git js-ts-docs
```

This builds the package in an isolated environment and starts the stdio MCP server. It prints nothing on purpose: stdout is the protocol channel. Stop it with `Ctrl-C`.

After the repository is updated, force `uv` to re-resolve the commit:

```bash
uvx --refresh --from git+https://github.com/KEEPEE/js-ts-mcp.git js-ts-docs
```

### Local checkout — fastest startup, editable while developing

```bash
git clone https://github.com/KEEPEE/js-ts-mcp.git
cd js-ts-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/js-ts-docs        # stdio MCP server
```

## MCP client configuration

### Generic stdio client (Claude Desktop, Cursor, Cline, …)

```json
{
  "mcpServers": {
    "js-ts": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/KEEPEE/js-ts-mcp.git",
        "js-ts-docs"
      ]
    }
  }
}
```

With an explicit cache directory (any `env` you set is passed straight through to the server):

```json
{
  "mcpServers": {
    "js-ts": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/KEEPEE/js-ts-mcp.git",
        "js-ts-docs"
      ],
      "env": {
        "JS_TS_MCP_CACHE_DIR": "/tmp/js-ts-cache"
      }
    }
  }
}
```

### DeepSeek Harness (`cordis`-style plugin list)

```yaml
- id: mcp-js-ts
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: js-ts
    transport: stdio
    command: uvx
    args:
      [
        '--from',
        'git+https://github.com/KEEPEE/js-ts-mcp.git',
        'js-ts-docs'
      ]
```

To skip the build at client start, point `command` at the console script of a checkout instead — `command: /path/to/js-ts-mcp/.venv/bin/js-ts-docs` with `args: []`.

## Tools reference

### `js_docs(identifier, topic=None, max_tokens=8000)`

Resolves an identifier to one docs page. Identifier forms, tried in this order:

| Form | Meaning |
|---|---|
| `mdn:Web/JavaScript/Reference/Global_Objects/Array` | fetched directly from that MDN slug |
| `ts:intro`, `ts:2/generics` | fetched directly as a TypeScript handbook page |
| `Promise` (any other plain name) | resolved through the local index: only the top-score candidates are considered; a single distinct path wins, ties prefer MDN `Web/JavaScript/Reference` pages over MDN `Web/API` over other MDN pages over TypeScript pages, and within the winning tier a parent page beats its subpages. Ambiguous or missing matches return an error dict pointing at `js_search` |

`topic` keeps only the section whose heading matches it (`Methods`, `Examples`, …) plus the page title and description; if nothing matches, the full page is returned with a `note` listing the available headings. `max_tokens` is a rough budget (1 token ≈ 4 characters): the markdown is cut at a line boundary and `truncated` is set.

Returns `{"ok", "identifier", "url", "source", "title", "markdown", "truncated", "cached"}`, plus an optional `note`.

```jsonc
// js_docs({ "identifier": "mdn:Web/JavaScript/Reference/Global_Objects/Array", "max_tokens": 200 })
{
  "ok": true,
  "identifier": "mdn:Web/JavaScript/Reference/Global_Objects/Array",
  "url": "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array",
  "source": "mdn",
  "markdown": "# Array\n\nThe **`Array`** object, as with arrays in other programming languages, enables [storing a collection of multiple items under a single variable name](…), and has members for [performing common array operations](#examples).  ## [Description](#description)  In JavaScript, arrays aren't [primitives](…) but are instead `Array` objects with the following core characteristics:  … [truncated: showing ~130 of ~16043 estimated tokens]",
  "truncated": true,
  "cached": false,
  "title": "Array - JavaScript"
}
```

A bare name is resolved through the index — no guessing:

```jsonc
// js_docs({ "identifier": "Promise", "max_tokens": 160 })
{
  "ok": true,
  "identifier": "Promise",
  "url": "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Promise",
  "source": "mdn",
  "title": "Promise - JavaScript",
  "markdown": "# Promise\n\nThe **`Promise`** object represents the eventual completion (or failure) of an asynchronous operation and its resulting value. … [truncated: showing ~89 of ~8695 estimated tokens]",
  "truncated": true,
  "cached": false
}
```

A TypeScript handbook page:

```jsonc
// js_docs({ "identifier": "ts:intro", "max_tokens": 160 })
{
  "ok": true,
  "identifier": "ts:intro",
  "url": "https://www.typescriptlang.org/docs/handbook/intro.html",
  "source": "typescript",
  "title": "The TypeScript Handbook",
  "markdown": "# The TypeScript Handbook\n\n## About this Handbook\n … [truncated: showing ~12 of ~1347 estimated tokens]",
  "truncated": true,
  "cached": false
}
```

A `topic` that matches nothing tells you what the page does have:

```jsonc
// js_docs({ "identifier": "ts:intro", "topic": "nonexistent-section-xyz", "max_tokens": 120 })
{
  "ok": true,
  "…": "…",
  "note": "no section matching 'nonexistent-section-xyz'; available headings: The TypeScript Handbook, About this Handbook, How is this Handbook Structured, Non-Goals, Get Started"
}
```

A name that is not in the index is an error dict, not an empty page:

```jsonc
// js_docs({ "identifier": "nope-not-a-real-page-xyz" })
{
  "ok": false,
  "error": "no MDN/TypeScript page matching 'nope-not-a-real-page-xyz' found in the search index",
  "suggestion": "use js_search to find the exact page, or use the mdn:/ts: prefix"
}
```

### `js_search(query, limit=8)`

Multi-token ranked search over the local index: **every** token must match, and both the entry name and its path are searched. Returns `{"ok", "query", "results": [{source, name, path, score}], "index_count", "stale"}`. Score tiers: exact name 10 > name prefix 8 > substring in name 6 > substring in path 3, summed per token.

```jsonc
// js_search({ "query": "regular expression", "limit": 3 })
{
  "ok": true,
  "query": "regular expression",
  "results": [
    { "source": "mdn", "name": "Regular expression",  "path": "Glossary/Regular_expression",                    "score": 14.0 },
    { "source": "mdn", "name": "Regular expressions", "path": "Web/JavaScript/Guide/Regular_expressions",       "score": 14.0 },
    { "source": "mdn", "name": "Regular expressions", "path": "Web/JavaScript/Reference/Regular_expressions",   "score": 14.0 }
  ],
  "index_count": 14825,
  "stale": false
}
```

The index spans both sources, so a TypeScript term comes back from the handbook:

```jsonc
// js_search({ "query": "generics", "limit": 3 })
{ "results": [ { "source": "typescript", "name": "generics", "path": "2/generics", "score": 10.0 } ], "index_count": 14825, "…": "…" }
```

An ambiguous name returns every candidate and lets you pick — this is why the `mdn:`/`ts:` prefix exists:

```jsonc
// js_search({ "query": "promise", "limit": 3 })
{
  "results": [
    { "source": "mdn", "name": "Promise", "path": "Glossary/Promise",                                  "score": 10.0 },
    { "source": "mdn", "name": "promise", "path": "Web/API/PromiseRejectionEvent/promise",             "score": 10.0 },
    { "source": "mdn", "name": "Promise", "path": "Web/JavaScript/Reference/Global_Objects/Promise",   "score": 10.0 }
  ], "…": "…"
}
```

`js_search` costs **zero** requests while the cached index is fresh (measured warm: 0 requests, 0 budget units). If the index is stale the call rebuilds it inside the same budget — measured cold: **4** units.

### `npm_package(name, version=None, max_tokens=6000)`

npm metadata + README. `name` is the package name; `version` pins one release; `max_tokens` caps the README. Returns `{"ok", "name", "version" (resolved), "description", "license", "homepage", "repository_url", "keywords", "engines", "dependencies", "readme_markdown", "url"}` plus `readme_source` when the README came from GitHub. Cached 1 day, keyed by name + version — the cache holds the **uncapped** README, so a later call with a bigger `max_tokens` gets more text without a new request.

**Where the README comes from.** npm used to embed the README in the packument; for modern packages the `readme` field is there but **empty** (`zod`: 4 166 672 B packument, `readme` length 0; `express`: 808 998 B, length 0), and a single-version doc (`/zod/4.6.5`) has no `readme` key at all. So the document is fetched from the package's GitHub repository instead: `https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{candidate}`, candidates `README.md`, `Readme.md`, `readme.md`, `README.markdown`, `README.rst`, `docs/README.md`, first 200 whose body is over 80 bytes wins. A candidate that is a short single line naming another markdown/rst file is a **pointer** and is followed (max 2 hops) — that is how a monorepo works:

```jsonc
// npm_package({ "name": "zod" })   — live output, 2026-10-07
{
  "ok": true,
  "name": "zod",
  "version": "4.6.5",
  "description": "TypeScript-first schema declaration and validation library with static type inference",
  "license": "MIT",
  "homepage": "https://zod.dev",
  "repository_url": "git+https://github.com/colinhacks/zod.git",
  "keywords": ["typescript", "schema", "validation", "type", "inference"],
  "engines": null,
  "dependencies": {},
  "readme_markdown": "<p align=\"center\">\n  <img src=\"logo.svg\" width=\"200px\" …",
  "url": "https://www.npmjs.com/package/zod",
  "readme_source": "github:colinhacks/zod@HEAD/packages/zod/README.md",
  "cached": false,
  "truncated": false
}
```

`colinhacks/zod/HEAD/README.md` is 22 bytes and says exactly `packages/zod/README.md`; following it yields the real 7 304-byte document. `expressjs/express` has no `README.md` at all but a capitalised `Readme.md` (10 371 B), which the candidate list finds. A `.rst` README is returned **raw** with a `note` saying so — this server has no RST→markdown converter.

When there is no README to be had, `readme_markdown` is `null` **and `note` says why** — a silent null is the bug:

```jsonc
// a package whose repository is not on GitHub
"readme_markdown": null,
"note": "npm serves no readme for this package and its repository (https://gitlab.com/acme/thing.git) is not a GitHub project URL"
```

Only `github.com` repository URLs are followed (`git+https://`, `https://`, `git://`, `git+ssh://`, `ssh://`, and the `github:owner/repo` shorthand npm also serves). GitLab, Bitbucket and missing repository URLs get the note, not a guess. A spent request budget costs the README, never the lookup: the result stays `ok: true` with `note` naming the budget.

An unknown name is an error dict:

```jsonc
// npm_package({ "name": "nope-not-a-real-package-xyz" })
{
  "ok": false,
  "error": "npm lookup failed: package not found on npm",
  "suggestion": "check the package name"
}
```

`url` is the human-facing `www.npmjs.com` page. This server **reports** that URL but never requests it — the request goes to `registry.npmjs.org`, which is the only npm host in the allowlist ([why](#two-more-robots-facts-both-negatively-cached)).

### `js_status()`

Probes `developer.mozilla.org`, `www.typescriptlang.org` and `registry.npmjs.org` with a light GET (all on robots-**allowed** paths) and reports the index and cache state. `overall` is `ok`, `degraded` or `error`. The `politeness` block is diagnostics only and never changes `overall`.

```jsonc
// js_status()   — cold cache, live
{
  "server": "js-ts-mcp",
  "version": "0.2.0",
  "checks": {
    "search_index":     { "status": "ok", "entries": 14825, "mdn_count": 14708, "ts_count": 117, "built_at": "2026-10-07T08:24:04.455645+00:00", "stale": false },
    "cache":            { "status": "ok", "entries": 3, "expired": 0 },
    "mdn_org":          { "status": "ok", "http_status": 200 },
    "typescriptlang_org": { "status": "ok", "http_status": 200 },
    "npm_registry":     { "status": "ok", "http_status": 200 }
  },
  "politeness": {
    "status": "ok", "disabled": false,
    "requests": 5, "robots_requests": 3, "robots_rows": 3, "robots_fetches": 3, "robots_cache_hits": 2,
    "robots_negative": 1, "blocked_by_robots": 0,
    "throttle_waits": 4, "throttle_sleep_s": 2.341,
    "host_delays": { "developer.mozilla.org": 0.0, "www.typescriptlang.org": 0.0, "registry.npmjs.org": 0.0 },
    "budgets": { "tool:js_status": [8, 12] }, "budget_denied": 0,
    "conditional": 0, "conditional_skipped": 0, "revalidated_304": 0,
    "retries_429": 0, "retries_transport": 0, "stalls": 0,
    "cached_bodies": 3, "max_cached_bytes": 2097152,
    "robots_db": "/tmp/js-ts-cache/robots.db"
  },
  "overall": "ok"
}
```

`robots_negative: 1` on a cold run is the TypeScript handbook 404 — see [two more robots facts](#two-more-robots-facts-both-negatively-cached).

### `health_check()`

`{"status": "ok", "server": "js-ts-mcp", "version": "0.2.0"}` — no network, no cache; safe as a liveness probe.

## npm metadata source (and its robots.txt trap)

### `registry.npmjs.org/robots.txt` is not a robots file

This is the single strangest thing about crawling npm, and this server is explicit about it. Verified live on 2026-10-07:

```
GET https://registry.npmjs.org/robots.txt
  status=200  content-type=application/json  bytes=7462
  first 80 bytes: {"_id":"robots.txt","_rev":"24-5090ef0ecb2d83c69ed67cf442a0e74d","name":"robots.
```

The registry answers **HTTP 200 with JSON** — the packument of an npm package literally named `robots.txt`. There is no robots policy there at all.

Naive code that fetches `/robots.txt` and feeds whatever comes back into a robots parser is one step away from inventing rules out of package metadata. Worse, a package whose README happens to contain robots-looking lines could make a self-blocking rule appear. The layer runs the real captured body through its RFC 9309 parser and gets **zero groups and zero rules**, so `registry.npmjs.org` is treated as a host with no rules and the request proceeds:

```
parse_robots(<the 7,462-byte body>) -> []
can_fetch("https://registry.npmjs.org/react") -> True
```

How the test suite pins this (`tests/test_politeness_wiring.py`, all four run offline against the captured file `tests/fixtures/npm_robots_txt_packument.json`):

| test | what it asserts |
|---|---|
| `test_npm_robots_body_is_a_packument_and_yields_zero_rules` | the captured body really is JSON (`{` first), really is a package (`data["name"] == "robots.txt"`), and `parse_robots` returns `[]` — no groups, no rules |
| `test_adversarial_packument_text_still_yields_zero_rules` | a synthetic packument whose `readme` is `"User-agent: *\nDisallow: /\nAllow: /secret\n"` still yields `[]`. JSON escapes the newlines, so the parser never sees a line break and never sees a group header — the trap cannot turn into a rule even when a package author tries |
| `test_npm_request_proceeds_despite_the_trap` | end-to-end through `fetch_npm_package`: `ok is True`, `blocked_by_robots == 0`, and the robots file was fetched exactly once |
| `test_trap_is_cached_so_robots_is_fetched_once_per_ttl` | the trap body is stored like any other robots record, so the registry costs one robots request per 7 days, not one per call |

### Three more robots facts, all negatively cached

- **`https://www.npmjs.com/robots.txt` is a Cloudflare 403.** That host is deliberately **not** in the allowlist: this server only *reports* `www.npmjs.com` URLs (the `url` field of `npm_package`), it never requests them. With the production allowlist the layer refuses such a request outright and makes **no** network call at all — measured live, `layer.get("https://www.npmjs.com/robots.txt")` returns `error='host www.npmjs.com is not in ALLOWED_HOSTS'` with zero bytes on the wire. The 403 itself is still negatively cached by the layer (one robots request per host per 7 days, not one per call), which is asserted offline by `test_www_npmjs_com_403_robots_is_negatively_cached` and `test_www_npmjs_com_is_not_in_the_allowlist`.
- **`https://www.typescriptlang.org/robots.txt` is a 404** (GitHub Pages serves an HTML 404 page, measured 9,379 bytes). Also negatively cached: `robots_negative: 1` in `js_status().politeness` on a cold run, and one robots request per week thereafter — asserted by `test_typescriptlang_robots_404_is_negatively_cached`.
- **`https://raw.githubusercontent.com/robots.txt` is a 404 too** — measured 2026-10-07, HTTP 404 with the 14-byte body `404: Not Found`. GitHub serves raw files without publishing robots rules, so the README fallback has **no** rules to obey there and the host is allowlisted explicitly instead. That is also why it must be in `ALLOWED_HOSTS`: an unlisted host is refused by the layer, and a missing entry would turn the fallback into a silent no-op. Asserted by `test_raw_host_robots_404_is_negatively_cached` and `test_allowlist_covers_exactly_the_four_fetched_hosts`.

MDN's `robots.txt` is the only one of the three that publishes real rules — 119 bytes, one `User-agent: *` group, three `Disallow` lines: `/api/`, `/*/files/` and `/media`. `/api/` matters here: this server used to call `developer.mozilla.org/api/v1/docs`, which is both dead and disallowed. The layer refuses it with **no request made** — verified live: `can_fetch("https://developer.mozilla.org/api/v1/docs/Array")` → `False`, `can_fetch("https://developer.mozilla.org/en-US/files/x")` → `False`, while the docs paths this server actually uses return `True`.

## One transport: no `curl` fallback, no browser user-agent masking

Both of these were in earlier versions of this package. Both are gone, deliberately, and a test keeps them gone.

**No `curl` subprocess fallback.** An earlier `js_status` shelled out to `curl` when `httpx` failed. That second transport bypassed robots.txt, the throttle, the request budget and the stored `ETag`/`Last-Modified` validators — so a *fallback* request was by construction an **unpolite** request. It also doubled the worst case, and the premise it was written for (that some CDN edges throttle Python's TLS fingerprint) did not reproduce under measurement. `test_no_subprocess_or_curl_fallback_left_in_the_sources` checks this on the **AST** of `fetchers.py`, `server.py` and `search.py`: no `subprocess` import, no `"curl"` argv string. The docstrings still explain why it is gone; the code does not contain it. `test_fetchers_build_exactly_one_http_client` pins the other half — `fetchers.py` constructs exactly one `httpx.Client` and never calls `client.get()` directly, so every request goes `_client()` → `_get()` → the politeness layer, and there is no second path that could skip it.

**No browser user-agent spoofing.** The module used to send a Chrome 126 UA. It buys nothing: these three sites do not gate on it, and it actively *hurts* politeness, because a robots file can only match a product token it can see — a `User-agent: *`-specific rule, a `Crawl-delay` aimed at a named crawler, or a site's own abuse counter all key off the UA. A spoof also hides who is knocking. The layer now sends one honest, contactable UA on **every** request, robots.txt included:

```
js-ts-mcp/0.2 (+https://github.com/KEEPEE/js-ts-mcp)
```

That string is defined once, in `src/js_ts_mcp/fetchers.py`, and every test refers to it through the constant (`tests/conftest.py`, `tests/test_politeness_wiring.py`). `test_honest_user_agent_is_what_reaches_the_wire` asserts it on the wire: every request the repo makes carries exactly `fetchers_mod.USER_AGENT`, the string starts with `js-ts-mcp/`, contains `github.com/KEEPEE/js-ts-mcp`, contains no `Mozilla/`/`Chrome/`/`Safari/`/`AppleWebKit` token, and the `robots.txt` fetch itself carries it — because that is the token a robots file matches on.

## Caching & politeness

### Cache

Everything lives in one directory: `~/.cache/js-ts-mcp` by default, overridable with `JS_TS_MCP_CACHE_DIR`.

| File | Contents | TTL |
|---|---|---|
| `cache.db` | docs pages (parsed result + raw body + `ETag` / `Last-Modified`), the search index, npm metadata | docs 7 days, search index 7 days, npm metadata 1 day |
| `robots.db` | the politeness layer's robots.txt cache | 7 days per host |

One env var moves both. Delete the files to force a full refresh. A cache problem is never a tool failure: if the directory is unwritable the server runs without a cache and says so in `js_status().checks.cache` — covered by `test_npm_package_and_status_survive_an_unwritable_cache_dir`.

### Politeness

Every outbound request — `js_docs`, `js_search` (its index build), `npm_package` and the `js_status` probes — goes through one small internal module, [`src/js_ts_mcp/politeness.py`](src/js_ts_mcp/politeness.py) (stdlib + `httpx`, no extra dependency):

- **robots.txt is read and obeyed.** Rules are matched per RFC 9309 (`*`, trailing `$`, `%2A`/`%24` literals, merged `User-agent` groups, most-specific match wins, `Allow` beats `Disallow` on a tie). `urllib.robotparser` is deliberately not used: on Python < 3.14 it reads `*` as a literal character, which is exactly how this server once requested `developer.mozilla.org/api/v1/docs` — a path MDN's own `robots.txt` disallows. Files are cached 7 days per host in `robots.db`, **including negative results** (404 / 403 / 5xx / unparseable body), so a host costs one robots request per week — which is what makes the npm and TypeScript handbook quirks above cheap instead of per-call.
- **Per-host throttle, including `Crawl-delay`.** Sequential requests to one host are spaced 0.35–0.9 s by default, or by the site's own `Crawl-delay` when it declares one — **none of the three hosts declares a `Crawl-delay` today** (checked against all three live robots responses), so the default window applies. The `robots.txt` fetch queues in the same per-host line as a page request, and a `Crawl-delay` learned *from* a robots fetch gates the request that triggered the fetch, not only the next one. Measured cold `js_docs("Promise")`: `throttle_sleep_s: 1.382` across 2 waits.
- **`429` / `503` / `504` are handled.** `Retry-After` is honoured to the second; without it the per-host delay is escalated and the request retried **once**. A response that stalls past 10 s escalates the delay instead of being retried blindly.
- **Conditional GET.** `ETag` / `Last-Modified` and the raw body are stored next to the cached entry, so refreshing an expired page costs a `304` instead of re-downloading it. Verified against live headers: **all three hosts send both** `ETag` and `Last-Modified` (MDN `"2f13939d…"`, typescriptlang.org `W/"6ac39afa-2fe88"`, the npm registry `W/"c7ff9479…"`). Measured live: a cold `js_docs("ts:intro")` after the index build had already fetched that same page revalidated it with one `304` and 0 bytes (`conditional: 1`, `revalidated_304: 1`). No conditional headers are ever sent to a host that sent no validators (`test_host_without_validators_never_sends_conditional_headers`).
- **Request budgets — one unit is one request on the wire.** One tool call makes at most **7** requests; the robots.txt fetch of a cold host, a `429` retry, a transport retry and every redirect hop each pay a unit. Measured live with a cold cache: `js_docs("mdn:…Array")` = **2** (robots + page), `js_docs("Promise")` = **5** (MDN robots + sitemap + TypeScript robots + handbook page + the page itself — the index rebuild is what makes a plain name expensive), `js_docs("ts:intro")` = **2**, an unknown identifier = **4** (the index build, then the index answers), `js_search(…)` = **4** cold and **0** warm, `npm_package("zod")` = **5** cold (npm robots + packument + `raw.githubusercontent.com` robots + the 22-byte pointer README + the document it names) and **3** warm (both READMEs revalidated with `304`, robots cached). `js_status` has its own cap of **12** (measured cold **8**, warm **3**) so its probes plus a stale-index rebuild always fit — a probe refused by the budget would report `error` and drag `overall` to `"degraded"` for a purely internal reason. The search-index build shares the tool call's budget; a build that hits its cap stops early and returns a **partial** index (`"partial": true`, `"partial_reason": …`) instead of raising, and a partial index is never cached. A README search that runs out of budget stops early and reports a `note` — the package metadata is still returned.
- **Redirects are resolved by the layer, not by httpx.** Requests go out with `follow_redirects=False`, every 3xx target is re-checked against that host's robots rules before its body is used, the chain is capped at 5 hops and reported as an error (never an exception), and the `url` a tool returns is the **final** URL of the chain.
- **Body-size cap: 2 MiB.** Raw bodies are only kept for revalidation when they are under `MAX_CACHED_BODY_BYTES = 2 * 1024 * 1024`. [Why that number](#why-2-mib-for-the-body-cap).
- **Host allowlist.** Only `developer.mozilla.org`, `www.typescriptlang.org`, `registry.npmjs.org` and `raw.githubusercontent.com` can be contacted, so a malformed identifier, a package's repository URL or an unexpected redirect cannot turn a lookup into a request somewhere else. A non-GitHub `repository` URL is reported, never fetched.

The layer never raises and never changes a tool's return shape; `js_status` reports its counters under the top-level `politeness` key.

### Why 2 MiB for the body cap

The cap is a measurement, not a taste. The politeness layer stores the raw body next to the cached entry so an expired entry can be revalidated with `If-None-Match` and answered with a `304` and zero bytes. That only pays if the body is worth holding in memory and on disk — and the two biggest artifacts this server touches are four orders of magnitude apart in value:

| artifact | measured size | kept? |
|---|---|---|
| MDN en-US sitemap (`developer.mozilla.org/sitemaps/en-us/sitemap.xml.gz`) | 127,027 B on the wire today (126,725 B in the captured fixture), **1,940,079 B decoded today** — 1,936,390 B for the fixture | **yes** — it is the whole search index, and revalidating it with a `304` is the single biggest saving available |
| npm `react` packument (`registry.npmjs.org/react`) | **7,016,423 B** in the audit, 7,021,259 B measured 2026-10-07 | **no** — it is parsed into a few hundred bytes of metadata and then has no further use |
| npm `typescript` packument | **15,762,037 B** measured 2026-10-07 | **no** |

So the cap sits deliberately **between** the two: big enough that the decoded sitemap (1.94 MB) still fits and can be revalidated, small enough that packuments — which grow with every release ever published, and are already 7 MB and 15 MB for two ordinary packages — are never held. `test_cap_sits_between_the_two_real_artifacts` asserts exactly that relationship against the real fixture sizes, so the number cannot drift away from the measurement that justifies it.

Worth saying plainly: the sitemap is the *tight* side of that margin. At 1,940,079 bytes decoded it is within 8% of the 2 MiB cap, so if MDN's en-US index keeps growing the sitemap will eventually stop being revalidated and the index rebuild will re-download it every week. That is a deliberate trade — a weekly re-download is a cost, an unbounded in-memory body is a worse one — and the test is what makes the margin visible rather than folklore.

An oversized body is simply **not remembered**: the request still succeeds, the caller still gets the whole body, and the next call re-downloads it. `test_oversized_body_is_not_cached_but_the_request_still_works` asserts the full body is returned, `cached_bodies == 0`, `conditional == 0`, and that no revalidation row was written. `test_production_layer_caps_cached_bodies_at_2mb` asserts the production layer actually passes the cap (the library default is higher).

### Opt-out

```bash
export JS_TS_MCP_POLITENESS_DISABLED=1
```

This single switch turns off robots.txt, the throttle, retries, conditional GET and the host allowlist at once. **Use it at your own risk:** you take over responsibility for respecting each site's crawling rules, and you are far more likely to be rate-limited or blocked. There is no partial opt-out.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

.venv/bin/python -m pytest -q              # 168 offline tests; fixtures in tests/fixtures/
.venv/bin/python scripts/politeness_smoke.py /tmp/js-ts-smoke-cache
.venv/bin/python scripts/e2e_mcp_test.py
```

- `pytest` is fully offline: HTTP is simulated with `httpx.MockTransport` and time/jitter are injected, so the suite is deterministic and runs in seconds. `tests/fixtures/` holds real captured artifacts: an MDN page (245 KB), the MDN en-US sitemap (127 KB gzipped), MDN's `robots.txt` (119 B), a TypeScript handbook page (196 KB), two npm registry JSON payloads and the 7,462-byte `robots.txt` packument trap.
- `scripts/politeness_smoke.py <cache-dir>` is a **live measurement**, not a test: it drives the real tools against the real docs sites and prints what the politeness layer did (requests, robots, throttle, conditional GET, budgets).
- `scripts/e2e_mcp_test.py` spawns the installed `js-ts-docs` console script and speaks newline-delimited JSON-RPC to it (`initialize` → `tools/list` → every tool → a negative case → `health_check`). Exit code 0 means every check passed. It resolves the server command as `JS_TS_MCP_E2E_CMD` (env override) → this checkout's `.venv/bin/js-ts-docs` → `python -m js_ts_mcp.server`.
- `src/js_ts_mcp/politeness.py` and `tests/test_politeness.py` are shared copies, kept byte-comparable across the four repos in this family; the repo-specific parts are the env-var names, the cache paths and the import path.

## Troubleshooting

**The first `js_docs` call is slower than the rest.** That is the cold search-index build: MDN's `robots.txt` + the en-US sitemap + typescriptlang.org's `robots.txt` + the handbook page, all spaced by the per-host throttle, followed by the page you actually asked for. It happens once every 7 days; later calls hit the cached index. Delete `cache.db` and you pay for it again.

**`request budget exhausted for scope 'tool:js_docs'`.** One tool call hit its cap of 7 requests. `js_search` first — it is offline and free once the index is warm — and then pass the exact `mdn:` or `ts:` path instead of a bare name. `js_status().politeness.budgets` shows how much each scope spent.

**A bare name costs five requests and an explicit `mdn:` slug costs two.** A plain name has to resolve through the index, and on a cold cache that means building it (sitemap + handbook page + two robots fetches) before fetching your page. Once the index is warm the difference is one robots-free page fetch either way.

**`"partial": true`, or fewer search results than expected.** An index build ran out of its budget and stopped early, returning a partial index with a `partial_reason`. A partial index is **not cached**, so the next run rebuilds it. `js_search` still works — it just knows fewer pages — and `js_status().checks.search_index.entries` tells you how many it has (a complete index today is 14,825 entries: 14,708 MDN + 117 TypeScript).

**`"stale": true` in search results.** The rebuild failed (offline, DNS failure, 5xx) and the server fell back to the older cached index instead of failing the lookup.

**Nothing is cached and `overall` is `degraded`.** Read `js_status().checks.cache.error` — an unwritable `JS_TS_MCP_CACHE_DIR` (read-only mount, missing permission) is the usual cause. Tools keep working without a cache; they are just slower and noisier on the network.

**A lookup returns `blocked by robots.txt`.** The site's rules disallow that path for this user agent, and the request was not sent. On MDN that normally means `/api/`, `/*/files/` or `/media`. Fetch the page yourself, or accept the consequences of the opt-out switch above.

**`npm_package` returns `package not found on npm`.** The registry answered 404 for that name. Check the name (npm names are case-sensitive for scoped packages, e.g. `@types/node`) and, if you pinned a version, that the version exists.

**`readme_markdown` is `null` for a pinned version.** Normal: the registry does not always carry a README for a non-latest release. Drop the pin to read the latest README.

**A `www.npmjs.com` lookup is refused.** By design: that host is not in the allowlist, so the layer returns `host www.npmjs.com is not in ALLOWED_HOSTS` and makes no request. Use `npm_package` — it fetches `registry.npmjs.org` and reports the `www.npmjs.com` URL to you.

## Support development

js-ts-mcp is built and maintained by Michal in his spare time. If it saves you time or makes your team's docs easier to work with, a coffee (or more) would mean a lot — every contribution helps keep the project moving. 🙏

**Pay via Revolut:** [revolut.me/michal4zvc](https://revolut.me/michal4zvc)

## License & attribution

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Michal Gaspierik.

The **design** of the politeness layer was inspired by [Crawl4AI](https://github.com/unclecode/crawl4ai) (© Unclecode, Apache-2.0): a TTL-cached robots store, the wildcard rule translation, and a per-domain rate limiter with escalating backoff. The implementation is a clean-room rewrite in this project's own synchronous stdlib-plus-`httpx` style — no line was transcribed, translated or mechanically adapted from Crawl4AI, and Crawl4AI is not a dependency of this package. Three defects of the original design are fixed (robots `fetched_at` refresh, negative-result caching, `Crawl-delay` support), and the RFC 9309 matcher is what makes MDN's wildcard rules actually bind — `robotparser` would have let the dead `/api/` calls through. The GPL-3.0 part of Crawl4AI — its vendored `html2text` fork — is deliberately excluded: no code, data or dependency from that tree is used or shipped here; HTML→markdown stays `markdownify`'s job. The full statement, including the 37-character clean-room measurement and the reading pointers to the design, is in [NOTICE](NOTICE).

Same clean-room architecture as [flutter-mcp](https://github.com/KEEPEE/flutter-mcp), [java-spring-mcp](https://github.com/KEEPEE/java-spring-mcp) and [python-docs-mcp](https://github.com/KEEPEE/python-docs-mcp): fetchers → pure parsers → TTL cache → search index → tools, with no circular delegation, a pinned 1.x MCP SDK, error-dict failures and an offline test suite.

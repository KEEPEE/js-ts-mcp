# js-ts-mcp

Real-time **MDN Web Docs** (developer.mozilla.org), **TypeScript handbook** (typescriptlang.org) and **npm package info** as an MCP (Model Context Protocol) server. Built so AI agents write JavaScript/TypeScript against real, current APIs instead of hallucinated or deprecated ones.

- **MDN Web Docs** — any docs page fetched live by its slug (e.g. `Web/JavaScript/Reference/Global_Objects/Array`) and converted to clean markdown
- **TypeScript handbook** — all 117 handbook pages (e.g. `intro`, `2/generics`) scraped live from typescriptlang.org
- **npm packages** — registry metadata + readme via the official [JSON API](https://registry.npmjs.org/), optionally pinned to a version
- **Local search index** over 14,683 MDN docs (from the en-US sitemap) + 117 TypeScript handbook pages (name + path recorded, so `Promise` resolves to the right page automatically), rebuilt at most every 7 days with stale-fallback
- **SQLite TTL cache** so repeated lookups are instant

## Tools

| Tool | What it does |
|---|---|
| `js_docs` | Fetch an MDN or TypeScript handbook page as markdown. Identifier forms: `mdn:Web/JavaScript/Reference/Global_Objects/Array` (explicit MDN slug), `ts:intro` / `ts:2/generics` (explicit handbook page), or plain names like `Promise` (resolved via the index — ties prefer mdn `Web/JavaScript/Reference` pages over mdn `Web/API` over other mdn over typescript, and a parent page beats its subpages). Optional `topic` filter (a heading section) and `max_tokens` truncation. |
| `js_search` | Multi-token ranked search over the MDN + TypeScript index (name + path; every token must match). Returns `{source, name, path, score}` results — call `js_docs` with the chosen `mdn:`/`ts:` path. |
| `npm_package` | npm package metadata: resolved version, description, license, homepage, repository URL, keywords, engines, dependencies and the readme as markdown. Optional pinned `version`. |
| `js_status` | Real health check: index size (total/mdn/typescript), age/staleness, cache stats, live probes of developer.mozilla.org, typescriptlang.org and registry.npmjs.org (through the politeness layer), plus the politeness counters under a top-level `politeness` key. |
| `health_check` | Server liveness + version. |

All tools return plain dicts; failures come back as `{"ok": false, "error": ..., "suggestion": ...}` — the server never crashes on a bad lookup.

## Requirements

- Python 3.10+ (tested on 3.12)
- The MCP Python SDK **1.x** is pinned (`mcp>=1.2.0,<2.0`) — this package does not support the mcp 2.x rename of FastMCP.
- `httpx` (the only HTTP transport in use — every request goes through the politeness layer; there is no `curl` fallback)

## Run it

### Option A — `uvx` straight from this repo (no manual install)

```bash
# TOKEN = your GitLab personal access token for github.com/KEEPEE
uvx --from "git+https://github.com/KEEPEE/js-ts-mcp.git" js-ts-docs
```

Add `--refresh` to force re-pulling the latest commit after an update.

### Option B — local venv (fastest startup, no token in config)

```bash
git clone https://github.com/KEEPEE/js-ts-mcp.git
cd js-ts-mcp
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e .
.venv/bin/js-ts-docs        # starts the stdio MCP server
```

## MCP client configuration

### DSH (this machine) — in `~/.dsh-home/profiles/web/cordis.patch.yml`

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

### Claude Desktop / any generic MCP client

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

## Cache location

`~/.cache/js-ts-mcp/cache.db` (override with `JS_TS_MCP_CACHE_DIR`). Fetched docs cached 7 days, npm metadata 1 day. The same directory holds `robots.db`, the 7-day robots.txt cache — one env var moves both. Delete the files to force a full refresh.

## Politeness

Every outbound request — `js_docs`, `js_search` (its index build), `npm_package` and the `js_status` probes — goes through one small internal layer, `src/js_ts_mcp/politeness.py` (stdlib + `httpx`, no new dependency):

- **robots.txt is read and obeyed.** Rules are matched per RFC 9309 (`*`, trailing `$`, `%2A`/`%24` literals, merged `User-agent` groups, most-specific match wins, `Allow` beats `Disallow` on a tie). `urllib.robotparser` is deliberately not used: on Python < 3.14 it ignores wildcards, and MDN's rules use them (`Disallow: /*/files/`). Files are cached 7 days per host in `robots.db`, **including negative results** (404/403/5xx), so a host costs one robots request per week.
- **Per-host throttle, including `Crawl-delay`.** Sequential requests to one host are spaced 0.35–0.9 s by default, or the site's own `Crawl-delay` when it declares one. Before this change nothing enforced a gap: the cold-start gaps between same-host page requests were incidental (388 ms on `developer.mozilla.org`, 434 ms on `www.typescriptlang.org`). With the layer the same cold start measures **407 ms** and **464 ms**, with a 350 ms floor. The `robots.txt` fetch is throttled too — it queues in the same per-host line as a page request — so `min_gap_ms` of the measurement harness is a valid politeness metric again (it used to fall to 12–15 ms because a robots fetch sat directly against the page request behind it).
- **`429` / `503` / `504` are handled.** `Retry-After` is honoured to the second; without it the per-host delay is escalated and the request retried once. A response that stalls past 10 s escalates the delay instead of being retried blindly.
- **Bounded time, one transport.** 5 s connect / 20 s read / 60 s pool+write, per request. The `curl` subprocess fallback `js_status` used to fall back to is gone: it bypassed robots, throttle and budget, and doubled the worst case.
- **Conditional GET.** `ETag` / `Last-Modified` and the raw body are stored next to the cached entry, so refreshing an expired page costs a `304` instead of re-downloading it. Measured live after this change: re-fetching the MDN sitemap, the MDN page and two TypeScript handbook pages cost **4 × `304`, 0 bytes** (those four bodies are 2.63 MB when re-downloaded). No conditional headers are sent to a host that sends no validators.
- **Request budgets — one unit is one request on the wire.** One tool call makes at most **7** requests; the robots.txt fetch of a cold host, a `429` retry, a transport retry and every redirect hop each pay a unit. Measured live: a cold `js_docs("Promise")` costs 5 (MDN robots + sitemap + TypeScript robots + handbook page + the page itself — the index rebuild is what makes it expensive), a cold `js_docs("ts:intro")` or `npm_package(x)` costs 2, and an unknown identifier stays at 2–3. `js_status` has its own cap of **12** (cold: index rebuild 4 + probes 4 = 8, because the rebuild already caches the robots facts of MDN and TypeScript; warm: 3) so a probe can never be refused by the budget — a refused probe would report `error` and drag `overall` to `"degraded"` for a purely internal reason. The search-index build shares the tool call's budget. An index build that hits its cap stops early and returns a **partial** index (`"partial": true`, `"partial_reason": …`) instead of raising, and a partial index is never cached.
- **Redirects are resolved by the layer, not by httpx.** Requests go out with `follow_redirects=False`, every 3xx target is re-checked against that host's robots rules before its body is used, the chain is capped at 5 hops and reported as an error (never an exception), and the `url` a tool returns is the **final** URL of the chain.
- **Body-size cap.** Raw bodies are only kept for revalidation when they are under **2 MB**. That number is a measurement, not a guess: the decoded MDN en-US sitemap is 1.94 MB (kept, so it can be revalidated) while the `react` packument is 7.02 MB (never kept). An oversized body is simply not cached — the request still succeeds and the caller gets the whole body.
- **Host allowlist.** Only `developer.mozilla.org`, `www.typescriptlang.org` and `registry.npmjs.org` can be contacted, so a malformed identifier or an unexpected redirect cannot turn a docs lookup into a request somewhere else.

The layer never raises and never changes a tool's return shape; `js_status` reports its counters under the top-level `politeness` key (robots cache rows, throttle waits, blocks, retries, stalls, budgets, body cap) — diagnostics only, never part of `overall`.

**Opt-out** (at your own risk — you become responsible for whatever the site's rules say):

```bash
export JS_TS_MCP_POLITENESS_DISABLED=1
```

That turns off robots, throttle, retry, conditional GET and the allowlist in one switch. There is no partial opt-out.

**Attribution:** the layer's design is inspired by [Crawl4AI](https://github.com/unclecode/crawl4ai) (© Unclecode, Apache-2.0) — the robots store, the wildcard translation and the per-domain rate limiter. It is a clean-room rewrite, not a copy, and Crawl4AI is not a dependency here. See [`NOTICE`](NOTICE).

## npm robots.txt (a trap worth knowing about)

`https://registry.npmjs.org/robots.txt` does **not** return a robots policy. It answers `HTTP 200`, `content-type: application/json` with the **packument of an npm package literally named `robots.txt`** (7,462 bytes — captured in `tests/fixtures/npm_robots_txt_packument.json`). Naive code that fetches `/robots.txt` and parses whatever comes back would read package JSON as rules; a body containing `"readme": "User-agent: *\nDisallow: /"` would even invent a self-blocking rule. The layer treats it as a host with **no rules** (JSON escapes newlines, so the parser never sees a group header) and the request proceeds — asserted by tests against the real captured body.

Two related facts, both measured:

- `https://www.npmjs.com/robots.txt` is a **Cloudflare 403**. That host is not in the allowlist — this server only *reports* `www.npmjs.com` URLs, it never requests them — and the 403 is negatively cached anyway, so it is not re-fetched on the next call.
- `https://www.typescriptlang.org/robots.txt` is a **404** (GitHub Pages). Also negatively cached: one robots request per week, not one per call.

MDN's `robots.txt` disallows `/api/`, `/*/files/` and `/media`. `/api/` matters here: this server used to call `developer.mozilla.org/api/v1/docs`, which is both dead and disallowed — the layer refuses it with no request made.

## Development

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                 # offline unit tests (fixtures in tests/fixtures/)
.venv/bin/python scripts/e2e_mcp_test.py      # live end-to-end: spawns the server, exercises every tool
```

## Design notes

Same clean-room architecture as [python-docs-mcp](https://github.com/KEEPEE/python-docs-mcp) and [java-spring-mcp](https://github.com/KEEPEE/java-spring-mcp): fetchers → pure parsers → TTL cache → search index → tools, with no circular delegation, a pinned 1.x MCP SDK, error-dict failures, and an offline test suite backed by real page fixtures (an MDN page, the en-US sitemap, TypeScript handbook HTML, npm registry JSON).

## License

MIT. The politeness layer's design was inspired by Crawl4AI (Apache-2.0);
the implementation is our own clean-room rewrite — see [`NOTICE`](NOTICE).

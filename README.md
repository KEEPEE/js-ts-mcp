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
| `js_status` | Real health check: index size (total/mdn/typescript), age/staleness, cache stats, live probes of developer.mozilla.org, typescriptlang.org and registry.npmjs.org (with curl fallback for edges that stall Python TLS). |
| `health_check` | Server liveness + version. |

All tools return plain dicts; failures come back as `{"ok": false, "error": ..., "suggestion": ...}` — the server never crashes on a bad lookup.

## Requirements

- Python 3.10+ (tested on 3.12)
- The MCP Python SDK **1.x** is pinned (`mcp>=1.2.0,<2.0`) — this package does not support the mcp 2.x rename of FastMCP.
- `curl` on PATH (fallback transport for hosts that throttle Python TLS clients)

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

`~/.cache/js-ts-mcp/cache.db` (override with `JS_TS_MCP_CACHE_DIR`). Fetched docs cached 7 days, npm metadata 1 day. Delete the file to force a full refresh.

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

MIT

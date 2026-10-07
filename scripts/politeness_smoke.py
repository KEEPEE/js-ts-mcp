"""Live smoke run for js-ts-mcp after the politeness port.

Not a test — a measurement. It drives the real tools against the real docs
sites and prints what the politeness layer did (requests, robots, throttle,
conditional GET, budgets), so the report can quote numbers instead of claims.

Usage: .venv/bin/python scripts/politeness_smoke.py <cache-dir>
"""

from __future__ import annotations

import json
import os
import sys
import time

CACHE_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/js-ts-smoke-cache"
os.environ["JS_TS_MCP_CACHE_DIR"] = CACHE_DIR
os.makedirs(CACHE_DIR, exist_ok=True)

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC)

from js_ts_mcp import fetchers, server  # noqa: E402

INTERESTING = (
    "requests",
    "robots_fetches",
    "robots_cache_hits",
    "blocked_by_robots",
    "throttle_waits",
    "throttle_sleep_s",
    "crawl_delay_applied",
    "conditional",
    "conditional_skipped",
    "revalidated_304",
    "retries_429",
    "retries_transport",
    "redirect_hops",
    "stalls",
    "budget_denied",
    "errors",
)


def snap() -> dict:
    s = fetchers.get_politeness().stats()
    out = {k: s.get(k) for k in INTERESTING}
    out["budgets"] = s.get("budgets")
    out["hosts"] = s.get("hosts")
    return out


def show(label: str, result, t0: float, before: dict) -> None:
    after = snap()
    delta = {
        k: (after[k] or 0) - (before.get(k) or 0)
        for k in INTERESTING
        if isinstance(after.get(k), (int, float)) and isinstance(before.get(k), (int, float))
    }
    keys = sorted(k for k in result if k in {"error", "type", "cached", "truncated", "overall", "partial"})
    summary = {k: (str(result[k])[:90] if k == "error" else result[k]) for k in keys}
    print(f"\n=== {label}  ({time.time() - t0:.2f} s)")
    print(f"    result: {json.dumps(summary)}")
    print(f"    delta : {json.dumps({k: v for k, v in delta.items() if v})}")
    print(f"    total : {json.dumps({k: after[k] for k in INTERESTING})}")


def main() -> int:
    print(f"cache dir: {CACHE_DIR}")
    print(f"robots db: {fetchers.default_robots_db_path()}")
    print(f"UA       : {fetchers.USER_AGENT}")
    print(f"timeouts : {fetchers.TIMEOUTS}  budget/call: {fetchers.FETCH_BUDGET_LIMIT}")

    steps = [
        ("js_docs Array (cold: index build + MDN page)", lambda: server.js_docs("Array")),
        ("js_docs nope_xyz_page (error path)", lambda: server.js_docs("nope_xyz_page")),
        ("js_docs Array (warm)", lambda: server.js_docs("Array")),
        ("js_docs ts:intro (typescriptlang.org)", lambda: server.js_docs("ts:intro")),
        ("js_search text field", lambda: server.js_search("text field", limit=3)),
        ("npm_package left-pad (registry.npmjs.org)", lambda: server.npm_package("left-pad")),
        ("js_docs Array (warm again: server cache answers)", lambda: server.js_docs("Array")),
        ("js_status", lambda: server.js_status()),
    ]
    for label, fn in steps:
        before = snap()
        t0 = time.time()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            result = {"error": f"{exc.__class__.__name__}: {exc}"}
        show(label, result if isinstance(result, dict) else {"type": type(result).__name__}, t0, before)

    status = server.js_status()
    print("\n=== js_status politeness block")
    print(json.dumps(status.get("politeness"), indent=2, sort_keys=True))
    print(f"overall={status.get('overall')} checks={sorted(status.get('checks', {}))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

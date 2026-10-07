"""Shared offline fixtures for the whole test suite.

Two things must be true for every test in this repo, and both are easy to
forget, so they are autouse here rather than repeated per module:

1. **No real cache directory.** ``JS_TS_MCP_CACHE_DIR`` points at a per-test
   temp dir, so tests never read or write the developer's
   ``~/.cache/js-ts-mcp`` (a warm real cache makes tests order-dependent).
   The legacy ``FLUTTER_``/``JAVA_SPRING_``/``PYTHON_DOCS_`` cache-dir
   fallbacks that ``cache.default_db_path`` and ``politeness.default_robots_db_path``
   still honour are cleared for the same reason.
2. **A fresh politeness layer.** The process-wide singleton is replaced before
   each test with one backed by an in-memory robots store, zero base delay and
   a seeded RNG — no real sleeping, no robots state leaking between tests —
   and reset afterwards so production code can never see a test instance.
   It deliberately carries **no** ``allowed_hosts`` and the reference
   ``max_cached_bytes``: production settings (allowlist, 2 MB body cap) are
   asserted explicitly in ``test_politeness_wiring.py`` via
   :func:`js_ts_mcp.fetchers.new_politeness`.

Nothing in this file performs network I/O.
"""

from __future__ import annotations

import random

import pytest

from js_ts_mcp import fetchers as fetchers_mod
from js_ts_mcp.politeness import DEFAULT_DISABLE_ENV_VAR, Politeness


@pytest.fixture(autouse=True)
def isolated_cache_dir(tmp_path, monkeypatch):
    """Keep every test out of the real ``~/.cache/js-ts-mcp``."""
    monkeypatch.setenv("JS_TS_MCP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("FLUTTER_DOCS_MCP_CACHE_DIR", raising=False)
    monkeypatch.delenv("JAVA_SPRING_MCP_CACHE_DIR", raising=False)
    monkeypatch.delenv("PYTHON_DOCS_MCP_CACHE_DIR", raising=False)
    # An opt-out set in a developer's shell must not silently change what the
    # suite proves; tests that want it set it themselves.
    monkeypatch.delenv(DEFAULT_DISABLE_ENV_VAR, raising=False)
    yield


@pytest.fixture(autouse=True)
def politeness_layer():
    """Fresh, fast, deterministic politeness layer for the duration of a test.

    ``cache_path=":memory:"`` keeps the robots cache in-process: a SQLite file
    per test costs seconds across the suite and buys nothing here.
    File-backed persistence is covered explicitly in ``test_politeness.py``
    and ``test_politeness_wiring.py``.
    """
    layer = Politeness(
        fetchers_mod.USER_AGENT,
        cache_path=":memory:",
        base_delay=(0.0, 0.0),  # no throttling sleeps in wiring tests
        rng=random.Random(1234),
    )
    fetchers_mod.set_politeness(layer)
    try:
        yield layer
    finally:
        fetchers_mod.set_politeness(None)
        layer.close()

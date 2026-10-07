"""Offline tests for the DocCache schema migration and the revalidation columns.

The politeness layer added ``etag`` / ``last_modified`` / ``body`` to the ``kv``
table. Real users have a warm ``cache.db`` written by the old three-column
schema, so the upgrade must be additive, idempotent and must never rewrite or
drop a row.
"""

from __future__ import annotations

import gzip
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

import pytest

from js_ts_mcp.cache import DocCache, default_db_path

FIXTURES = Path(__file__).parent / "fixtures"


def _create_old_schema(db_path: Path) -> None:
    """Create exactly the pre-politeness schema and put one row in it."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, expires_at REAL)"
        )
        conn.execute(
            "INSERT INTO kv (key, value, expires_at) VALUES (?, ?, ?)",
            ("search-index", '{"docs": []}', time.time() + 3600),
        )
        conn.commit()
    finally:
        conn.close()


def _columns(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [row[1] for row in conn.execute("PRAGMA table_info(kv)")]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# migration
# ---------------------------------------------------------------------------
def test_old_three_column_database_is_migrated_in_place(tmp_path):
    db = tmp_path / "cache.db"
    _create_old_schema(db)

    cache = DocCache(str(db))

    assert _columns(db) == ["key", "value", "expires_at", "etag", "last_modified", "body"]
    # the pre-existing row survived untouched and is still readable
    assert cache.get("search-index") == '{"docs": []}'
    entry = cache.get_entry("search-index")
    assert entry is not None
    assert entry["etag"] is None and entry["body"] is None and entry["expired"] is False


def test_migration_is_idempotent(tmp_path):
    """Opening the same database repeatedly must not error or duplicate columns."""
    db = tmp_path / "cache.db"
    _create_old_schema(db)
    first = DocCache(str(db))
    second = DocCache(str(db))
    third = DocCache(str(db))
    assert _columns(db) == ["key", "value", "expires_at", "etag", "last_modified", "body"]
    assert first.get("search-index") == second.get("search-index") == third.get("search-index")


def test_new_database_gets_the_full_schema(tmp_path):
    db = tmp_path / "cache.db"
    DocCache(str(db))
    assert _columns(db) == ["key", "value", "expires_at", "etag", "last_modified", "body"]


def test_default_db_path_honours_the_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("JS_TS_MCP_CACHE_DIR", str(tmp_path / "moved"))
    for legacy in ("FLUTTER_DOCS_MCP_CACHE_DIR", "JAVA_SPRING_MCP_CACHE_DIR", "PYTHON_DOCS_MCP_CACHE_DIR"):
        monkeypatch.delenv(legacy, raising=False)
    assert default_db_path() == str(tmp_path / "moved" / "cache.db")


# ---------------------------------------------------------------------------
# revalidation columns
# ---------------------------------------------------------------------------
def test_set_value_does_not_clear_validators(tmp_path):
    """The server caches parsed JSON with set_value; the fetcher's columns stay."""
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set_validators(
        "https://example.test/page",
        etag='"v1"',
        last_modified="Wed, 21 Oct 2015 07:28:00 GMT",
        body=b"<html>body</html>",
        ttl_seconds=60,
    )
    cache.set_value("https://example.test/page", '{"markdown": "x"}', 3600)

    entry = cache.get_entry("https://example.test/page")
    assert entry is not None
    assert entry["value"] == '{"markdown": "x"}'
    assert entry["etag"] == '"v1"'
    assert entry["last_modified"] == "Wed, 21 Oct 2015 07:28:00 GMT"
    assert entry["body"] == b"<html>body</html>"


def test_set_validators_leaves_value_alone(tmp_path):
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set_value("k", "parsed", 3600)
    cache.set_validators("k", etag='"v2"', body=b"raw")
    entry = cache.get_entry("k")
    assert entry is not None
    assert entry["value"] == "parsed"
    assert entry["etag"] == '"v2"'
    assert entry["body"] == b"raw"
    assert entry["expired"] is False  # expiry untouched (no ttl_seconds given)


def test_set_validators_can_extend_expiry_without_touching_value(tmp_path):
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set_value("k", "parsed", 1)
    time.sleep(1.1)
    assert cache.get("k") is None  # expired
    cache.set_validators("k", etag='"v1"', body=b"raw", ttl_seconds=3600)
    assert cache.get("k") == "parsed"  # refreshed by the revalidation write


def test_set_clears_validators_unless_given(tmp_path):
    """A plain ``set`` must not leave a stale ETag behind (wrong 304 risk)."""
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set("k", "v1", 3600, etag='"e1"', body=b"raw")
    cache.set("k", "v2", 3600)
    entry = cache.get_entry("k")
    assert entry is not None
    assert entry["value"] == "v2"
    assert entry["etag"] is None and entry["body"] is None


def test_get_entry_include_expired_returns_the_expired_row(tmp_path):
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set("k", "v", -1, etag='"e1"', body=b"raw")  # already expired
    assert cache.get("k") is None
    assert cache.get_entry("k") is None
    entry = cache.get_entry("k", include_expired=True)
    assert entry is not None
    assert entry["expired"] is True
    assert entry["etag"] == '"e1"'
    assert entry["body"] == b"raw"


def test_body_is_binary_safe(tmp_path):
    """The MDN sitemap can arrive as raw gzip; TEXT would corrupt it, BLOB does not."""
    gz = (FIXTURES / "mdn_sitemap_en_us.xml.gz").read_bytes()
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set("sitemap", '"index"', 3600, etag='"e"', body=gz)
    entry = cache.get_entry("sitemap")
    assert entry is not None
    assert entry["body"] == gz
    assert gzip.decompress(entry["body"]).startswith(b"<?xml")


def test_body_written_as_str_by_an_older_build_is_returned_as_bytes(tmp_path):
    """A row written before the BLOB switch still round-trips usefully."""
    db = tmp_path / "cache.db"
    cache = DocCache(str(db))
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO kv (key, value, expires_at, etag, body) VALUES (?, ?, ?, ?, ?)",
            ("legacy", "v", time.time() + 60, '"e"', "plain text body"),
        )
        conn.commit()
    finally:
        conn.close()
    entry = cache.get_entry("legacy")
    assert entry is not None
    assert entry["body"] == b"plain text body"


def test_stats_counts_expired_rows_only(tmp_path):
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set("fresh", "a", 3600)
    cache.set("stale", "b", -1)
    stats = cache.stats()
    assert stats == {"entries": 2, "expired": 1}


def test_peek_ignores_expiry(tmp_path):
    cache = DocCache(str(tmp_path / "cache.db"))
    cache.set("k", "v", -1)
    assert cache.get("k") is None
    assert cache.peek("k") == "v"
    assert cache.peek("missing") is None


# ---------------------------------------------------------------------------
# A13 B3 — the migration runs once; an unusable cache address fails upstream
# ---------------------------------------------------------------------------

def test_schema_migration_runs_once_per_cache(tmp_path, monkeypatch):
    """A12 F-A12-3: ``_ensure_schema`` used to run on *every* connection.

    Functionally fine, but it put a ``PRAGMA table_info`` and a possible
    ``ALTER TABLE`` in front of every single read — and that was exactly the
    line that blew up (``attempt to write a readonly database``) for a cache
    directory the user cannot write.  It now runs once, at construction.
    """
    calls: list[str] = []
    original = DocCache._ensure_schema.__func__

    def counting(cls, conn):
        calls.append("run")
        return original(cls, conn)

    monkeypatch.setattr(DocCache, "_ensure_schema", classmethod(counting))

    cache = DocCache(db_path=str(tmp_path / "cache.db"))
    for i in range(10):
        cache.set(f"k{i}", "v", ttl_seconds=60)
        cache.get(f"k{i}")
        cache.get_entry(f"k{i}", include_expired=True)
        cache.set_value(f"k{i}", "w", ttl_seconds=60)
        cache.set_validators(f"k{i}", etag='"x"', last_modified=None, body=None, ttl_seconds=60)
        cache.peek(f"k{i}")
        cache.stats()

    assert calls == ["run"], f"_ensure_schema ran {len(calls)} times, expected exactly once"


def test_one_shot_migration_is_still_idempotent(tmp_path):
    """Idempotence is kept on purpose: a re-run must be a harmless no-op."""
    db = str(tmp_path / "cache.db")
    cache = DocCache(db_path=db)
    for _ in range(2):
        conn = sqlite3.connect(db)
        try:
            DocCache._ensure_schema(conn)
        finally:
            conn.close()
    cache.set("k", "v", ttl_seconds=60)
    assert cache.get("k") == "v"


def test_unusable_cache_address_raises_at_construction(tmp_path):
    """The contract all four repos rely on: fail **fast**, in ``__init__``.

    Every repo wraps ``DocCache()`` in try/except and degrades to no cache, so
    the failure has to surface here — not later, inside a tool call, on the
    first read that happened to need a migration (A12 F-A12-2).
    """
    with pytest.raises(sqlite3.OperationalError):
        DocCache(db_path=str(tmp_path))  # a directory is not a writable database


# ---------------------------------------------------------------------------
# P5 — a read-only database degrades the cache, it must never break a tool
# ---------------------------------------------------------------------------
# Why the two ``chmod`` tests below are skipped for root: root bypasses the DAC
# permission bits, so ``chmod 0444`` does not produce a read-only database for
# it and the test would go green without ever entering the read-only code path.
# That masking is exactly what hid this bug — the suite was green locally (DSH
# runs as root) while GitHub's non-root runner failed on the schema migration
# with ``sqlite3.OperationalError: attempt to write a readonly database``.
# ``test_read_only_mode_is_forced_even_for_root`` forces the same path with the
# uid taken out of the equation, so both kinds of runner are covered.


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_read_only_database_is_still_readable(tmp_path):
    """A root-owned or 0444 ``cache.db`` stays readable; writes become no-ops."""
    db = str(tmp_path / "cache.db")
    _create_old_schema(Path(db))
    os.chmod(db, 0o444)
    try:
        cache = DocCache(db_path=db)
        assert cache.read_only is True
        assert cache.read_only_reason  # spelled out for ``*_status()``
        assert cache.get("search-index") == '{"docs": []}'
        # The additive migration was skipped because nothing can be written, so
        # the columns it would have added read as NULL instead of raising
        # ``no such column: etag``.
        entry = cache.get_entry("search-index", include_expired=True)
        assert entry is not None and entry["etag"] is None and entry["body"] is None
        assert set(_columns(Path(db))) == {"key", "value", "expires_at"}  # the file is untouched
        # Writes are a silent no-op returning False — the cache is an
        # optimisation, never a required dependency.
        assert cache.set("k", "v", ttl_seconds=60) is False
        assert cache.set_value("k", "v", ttl_seconds=60) is False
        assert cache.set_validators("k", etag='"e1"') is False
        assert cache.get("k") is None and cache.peek("k") is None
        assert cache.stats() == {"entries": 1, "expired": 0}
    finally:
        os.chmod(db, 0o644)  # let tmp_path cleanup work


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_read_only_directory_is_still_readable(tmp_path):
    """The journal lives next to the DB, so an unwritable dir blocks writes too."""
    cache_dir = tmp_path / "cache-dir"
    cache_dir.mkdir()
    db = str(cache_dir / "cache.db")
    warm = DocCache(db_path=db)
    assert warm.set("k", "v", ttl_seconds=3600) is True

    os.chmod(cache_dir, 0o555)
    try:
        cache = DocCache(db_path=db)
        assert cache.read_only is True
        assert cache.get("k") == "v"
        assert cache.set("k2", "v2", ttl_seconds=60) is False
        assert cache.get("k2") is None
        assert cache.stats() == {"entries": 1, "expired": 0}
    finally:
        os.chmod(cache_dir, 0o755)


def test_read_only_mode_is_forced_even_for_root(tmp_path, monkeypatch):
    """Deterministic version of the chmod test — it must pass whatever the uid.

    ``chmod`` is meaningless for root, so the migration itself is made to raise
    the exact error SQLite raises for a database the process cannot write.
    """
    db = str(tmp_path / "cache.db")
    _create_old_schema(Path(db))

    def refusing_migration(cls, conn):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(DocCache, "_ensure_schema", classmethod(refusing_migration))

    cache = DocCache(db_path=db)
    assert cache.read_only is True
    assert "readonly" in (cache.read_only_reason or "")
    # reads still work …
    assert cache.get("search-index") == '{"docs": []}'
    # … the columns the skipped migration would have added read as NULL …
    entry = cache.get_entry("search-index", include_expired=True)
    assert entry is not None and entry["etag"] is None and entry["body"] is None
    # … and every write is a no-op, never an exception.
    assert cache.set("k", "v", ttl_seconds=60) is False
    assert cache.set_value("k", "v", ttl_seconds=60) is False
    assert cache.set_validators("k", etag='"e1"') is False
    assert cache.get("k") is None and cache.peek("k") is None
    assert cache.stats() == {"entries": 1, "expired": 0}
    # every later connection really is opened through the ``mode=ro`` URI:
    # SQLite itself refuses the write, whatever the uid of the process.
    conn = cache._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO kv (key, value) VALUES ('x', 'y')")
    finally:
        conn.close()


def test_unwritable_directory_is_detected_without_chmod(tmp_path, monkeypatch):
    """Same as the read-only-directory test, forced so that root covers it.

    The up-front ``os.access`` hint is stubbed to report what it reports on a
    non-root runner: the directory cannot hold SQLite's journal.
    """
    db = str(tmp_path / "cache.db")
    warm = DocCache(db_path=db)
    assert warm.read_only is False
    assert warm.set("k", "v", ttl_seconds=3600) is True

    monkeypatch.setattr(DocCache, "_write_possible", lambda self: False)
    cache = DocCache(db_path=db)
    assert cache.read_only is True
    assert cache.get("k") == "v"
    assert cache.set("k2", "v2", ttl_seconds=60) is False
    assert cache.get("k2") is None


def test_refused_write_downgrades_the_cache_to_read_only(tmp_path, monkeypatch):
    """Safety net for what the up-front check cannot see.

    Permissions can change after construction, and ``os.access`` is a lie for
    root.  The first write SQLite refuses flips the cache to read-only instead
    of raising out of the tool that merely wanted to cache something.
    """
    db = str(tmp_path / "cache.db")
    cache = DocCache(db_path=db)
    assert cache.set("k", "v", ttl_seconds=3600) is True

    # A ``mode=ro`` connection refuses writes for every uid, exactly like a
    # database the process has no write permission for.
    monkeypatch.setattr(
        DocCache,
        "_connect",
        lambda self: sqlite3.connect(
            f"file:{urllib.request.pathname2url(self.db_path)}?mode=ro", uri=True
        ),
    )
    assert cache.set("k2", "v2", ttl_seconds=60) is False
    assert cache.read_only is True
    assert "readonly" in (cache.read_only_reason or "")
    assert cache.get("k") == "v"  # reads are unaffected
    # later writes are skipped up front, without touching SQLite again
    assert cache.set("k3", "v3", ttl_seconds=60) is False
    assert cache.get("k3") is None


def test_read_only_database_without_a_table_reads_as_empty(tmp_path, monkeypatch):
    """A 0-byte ``cache.db`` that cannot be migrated has no ``kv`` table at all.

    That is still not an error: every read is a miss and every write a no-op.
    """
    db = tmp_path / "cache.db"
    db.write_bytes(b"")
    monkeypatch.setattr(DocCache, "_write_possible", lambda self: False)

    cache = DocCache(db_path=str(db))
    assert cache.read_only is True
    assert cache.get("k") is None
    assert cache.get_entry("k", include_expired=True) is None
    assert cache.peek("k") is None
    assert cache.stats() == {"entries": 0, "expired": 0}
    assert cache.set("k", "v", ttl_seconds=60) is False


def test_forced_migration_failure_still_raises_when_nothing_is_readable(tmp_path, monkeypatch):
    """Read-only mode needs a database that *can* be read.

    A path that is not a database at all has nothing to fall back to, so the
    error still propagates from ``__init__`` and the caller drops the cache —
    the A13 B3 contract every repo's ``try/except DocCache()`` relies on.
    """
    def refusing_migration(cls, conn):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(DocCache, "_ensure_schema", classmethod(refusing_migration))
    # A directory is not a database: nothing can be opened, read or written.
    with pytest.raises(sqlite3.OperationalError):
        DocCache(db_path=str(tmp_path))

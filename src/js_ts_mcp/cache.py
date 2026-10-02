"""SQLite-backed TTL key-value cache.

A tiny single-process cache used to persist fetched artifacts (currently the
search index) between runs. Only stdlib ``sqlite3`` is used.

Design notes:
- One short-lived connection per operation, guarded by a re-entrant-free
  plain :class:`threading.Lock`, so concurrent calls from different threads
  of the same process cannot interleave writes. Connections are created and
  closed inside each call, so ``check_same_thread`` never becomes an issue.
- Expired rows are NOT deleted eagerly; :meth:`DocCache.get` simply ignores
  them and :meth:`DocCache.stats` reports how many there are.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

__all__ = ["DocCache", "default_db_path"]


def default_db_path() -> str:
    """Return the default cache.db path (expanduser'd, parent dirs not created).

    Honors ``JS_TS_MCP_CACHE_DIR``; falls back to ``FLUTTER_DOCS_MCP_CACHE_DIR``,
    ``JAVA_SPRING_MCP_CACHE_DIR`` and ``PYTHON_DOCS_MCP_CACHE_DIR`` for
    compatibility; defaults to ``~/.cache/js-ts-mcp/cache.db``.
    """
    base = os.environ.get(
        "JS_TS_MCP_CACHE_DIR",
        os.environ.get(
            "FLUTTER_DOCS_MCP_CACHE_DIR",
            os.environ.get(
                "JAVA_SPRING_MCP_CACHE_DIR",
                os.environ.get("PYTHON_DOCS_MCP_CACHE_DIR", "~/.cache/js-ts-mcp"),
            ),
        ),
    )
    return os.path.join(os.path.expanduser(base), "cache.db")


class DocCache:
    """A small SQLite-backed TTL cache (single table ``kv``).

    Schema::

        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, expires_at REAL)

    ``expires_at`` is an absolute POSIX timestamp (``time.time() + ttl``).
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = os.path.expanduser(str(db_path)) if db_path else default_db_path()
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS kv ("
                "key TEXT PRIMARY KEY, value TEXT, expires_at REAL)"
            )

    # -- internals ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    @staticmethod
    def _is_expired(expires_at: float | None, now: float | None = None) -> bool:
        if expires_at is None:  # rows without an expiry never expire
            return False
        return (now if now is not None else time.time()) >= expires_at

    # -- public API --------------------------------------------------------

    def get(self, key: str) -> str | None:
        """Return the stored value if present and not expired, else ``None``."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value, expires_at FROM kv WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        value, expires_at = row
        if self._is_expired(expires_at):
            return None
        return value

    def set(self, key: str, value: str, ttl_seconds: float) -> None:
        """Insert or update ``key`` with an expiry of ``time.time() + ttl_seconds``."""
        expires_at = time.time() + ttl_seconds
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO kv (key, value, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, expires_at = excluded.expires_at",
                (key, value, expires_at),
            )

    def stats(self) -> dict:
        """Return ``{"entries": total_rows, "expired": expired_rows}``.

        Counts only — expired rows are never deleted here.
        """
        now = time.time()
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0]
            expired = conn.execute(
                "SELECT COUNT(*) FROM kv WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            ).fetchone()[0]
        return {"entries": int(total), "expired": int(expired)}

    def peek(self, key: str) -> str | None:
        """Return the stored value ignoring expiry (``None`` when absent).

        Used by callers that want to fall back to a stale copy when a
        refresh fails (see :mod:`python_docs_mcp.search`).
        """
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row is not None else None

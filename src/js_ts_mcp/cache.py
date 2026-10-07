"""SQLite-backed TTL key-value cache.

A tiny single-process cache used to persist fetched artifacts (the search
index, parsed docs, npm metadata and, since the politeness layer landed, the
HTTP validators + raw body of each cached page) between runs. Only stdlib
``sqlite3`` is used.

Design notes:
- One short-lived connection per operation, guarded by a re-entrant-free
  plain :class:`threading.Lock`, so concurrent calls from different threads
  of the same process cannot interleave writes. Connections are created and
  closed inside each call, so ``check_same_thread`` never becomes an issue.
- Expired rows are NOT deleted eagerly; :meth:`DocCache.get` simply ignores
  them and :meth:`DocCache.stats` reports how many there are.
- **Schema migration is additive, idempotent and runs once.** ``etag`` /
  ``last_modified`` / ``body`` were added to ``kv`` after the first release,
  when the politeness layer started reusing cached pages for conditional GET.
  :meth:`_ensure_schema` is called from :meth:`__init__` only (A13 B3 — it used
  to run on every connection) and issues ``ALTER TABLE … ADD COLUMN`` only for
  columns that are actually missing, so a database written by the old schema
  keeps working untouched (existing rows get NULL, nothing is rewritten or
  dropped). Never ``DROP``/recreate here — real users have a warm cache. When
  the database cannot be opened or migrated at all, constructing
  :class:`DocCache` raises and the caller degrades to **no cache** (A13 B3).
- ``body`` is a **BLOB**, not TEXT (unlike the sibling repos): this repo's
  largest artifact is the MDN sitemap, which some CDNs serve as raw gzip
  without a ``Content-Encoding`` header (see ``search._fetch_mdn_sitemap``).
  Storing those bytes in a TEXT column would mangle them on the way back.
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

        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, expires_at REAL,
                         etag TEXT, last_modified TEXT, body BLOB)

    ``expires_at`` is an absolute POSIX timestamp (``time.time() + ttl``).
    ``etag`` / ``last_modified`` hold the HTTP validators of the response the
    value came from and ``body`` holds its raw bytes, so an expired entry can be
    revalidated with a conditional GET instead of being re-downloaded — see
    :meth:`get_entry`.
    """

    #: Additive columns introduced after the first release. Order matters only
    #: for readability; each is added independently when missing.
    _MIGRATIONS: tuple[tuple[str, str], ...] = (
        ("etag", "TEXT"),
        ("last_modified", "TEXT"),
        ("body", "BLOB"),
    )

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = os.path.expanduser(str(db_path)) if db_path else default_db_path()
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        # A13 B3: the schema is created/migrated exactly ONCE, here — never on a
        # per-operation connection.  Two reasons:
        #   * A12 F-A12-3: a ``PRAGMA table_info`` plus a possible ``ALTER TABLE``
        #     on every single read is work nobody asked for;
        #   * A12 F-A12-2: that per-connection migration was the exact line that
        #     raised ``OperationalError: attempt to write a readonly database``
        #     for a cache directory the user cannot write, which took down every
        #     tool in flutter-mcp.  A failure here propagates **on purpose**:
        #     every repo wraps ``DocCache()`` in try/except and degrades to "no
        #     cache", so one unwritable directory must never become a tool
        #     failure — and it must be visible in ``*_status()``.
        conn = sqlite3.connect(self.db_path)
        try:
            self._ensure_schema(conn)
        finally:
            conn.close()

    # -- internals ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """A short-lived connection for one operation.

        No schema work happens here (A13 B3) — :meth:`__init__` did it once.
        """
        return sqlite3.connect(self.db_path)

    @classmethod
    def _ensure_schema(cls, conn: sqlite3.Connection) -> None:
        """Create ``kv`` if needed and add any missing column. Idempotent.

        Called exactly once per :class:`DocCache`, from :meth:`__init__`
        (A13 B3 — it used to run on *every* connection).  It stays idempotent:
        a database written by an older version is still migrated in place, a
        repeated call is a no-op, and ``ALTER TABLE ADD COLUMN`` with a NULL
        default does not rewrite rows, so a warm cache survives the upgrade.
        """
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "key TEXT PRIMARY KEY, value TEXT, expires_at REAL, "
            "etag TEXT, last_modified TEXT, body BLOB)"
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(kv)")}
        for column, sql_type in cls._MIGRATIONS:
            if column not in existing:
                conn.execute(f"ALTER TABLE kv ADD COLUMN {column} {sql_type}")
        conn.commit()

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

    def set(
        self,
        key: str,
        value: str,
        ttl_seconds: float,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        body: bytes | str | None = None,
    ) -> None:
        """Insert or update ``key`` with an expiry of ``time.time() + ttl_seconds``.

        ``etag`` / ``last_modified`` / ``body`` are optional and always written
        as given (``None`` clears them) so a row never keeps validators from a
        previous response — a stale validator could produce a wrong ``304``.
        Callers that only refresh the parsed value must use :meth:`set_value`,
        which leaves the revalidation columns alone.
        """
        expires_at = time.time() + ttl_seconds
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO kv (key, value, expires_at, etag, last_modified, body) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, expires_at = excluded.expires_at, "
                "etag = excluded.etag, last_modified = excluded.last_modified, "
                "body = excluded.body",
                (key, value, expires_at, etag, last_modified, body),
            )

    def set_value(self, key: str, value: str, ttl_seconds: float) -> None:
        """Upsert only ``value`` + expiry, leaving validators/body untouched.

        Used by the server, which caches the *parsed* result under the source
        URL while the fetcher keeps the raw body + ``etag`` of that same URL in
        the same row.  Using :meth:`set` here would NULL the validators and
        silently disable conditional GET.
        """
        expires_at = time.time() + ttl_seconds
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO kv (key, value, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, expires_at = excluded.expires_at",
                (key, value, expires_at),
            )

    def set_validators(
        self,
        key: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        body: bytes | str | None = None,
        ttl_seconds: float | None = None,
    ) -> None:
        """Upsert only the revalidation columns (``etag``/``last_modified``/``body``).

        ``value`` is left alone (NULL for a brand-new row) and ``expires_at`` is
        only touched when ``ttl_seconds`` is given. A row whose ``value`` is NULL
        never answers :meth:`get` but can still be revalidated.
        """
        expires_at = None if ttl_seconds is None else time.time() + ttl_seconds
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO kv (key, value, expires_at, etag, last_modified, body) "
                "VALUES (?, NULL, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "etag = excluded.etag, last_modified = excluded.last_modified, "
                "body = excluded.body, "
                "expires_at = COALESCE(excluded.expires_at, kv.expires_at)",
                (key, expires_at, etag, last_modified, body),
            )

    def get_entry(self, key: str, *, include_expired: bool = False) -> dict | None:
        """Return the whole row: ``{"value", "expired", "etag", "last_modified", "body"}``.

        With ``include_expired=True`` an expired row is returned too — that is
        exactly what the conditional-GET path needs: the validators and the body
        of a page whose TTL has run out, so the refresh can ask
        ``If-None-Match`` instead of downloading it again. ``None`` when the key
        is absent (or expired and ``include_expired`` is false). ``body`` comes
        back as ``bytes`` (BLOB column) or ``None``.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value, expires_at, etag, last_modified, body FROM kv WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        value, expires_at, etag, last_modified, body = row
        expired = self._is_expired(expires_at)
        if expired and not include_expired:
            return None
        if isinstance(body, str):  # a row written before the BLOB switch
            body = body.encode("utf-8", "replace")
        return {
            "value": value,
            "expired": expired,
            "etag": etag,
            "last_modified": last_modified,
            "body": body,
        }

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
        refresh fails (see :mod:`js_ts_mcp.search`).
        """
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row is not None else None

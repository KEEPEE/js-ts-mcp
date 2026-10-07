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
  the database cannot be opened at all, constructing :class:`DocCache`
  raises and the caller degrades to **no cache** (A13 B3).
- **A database that cannot be written is a supported state, not an error**
  (P5).  A root-owned ``cache.db``, a ``0444`` file or a directory without
  write permission makes the migration raise ``sqlite3.OperationalError``
  (``attempt to write a readonly database``).  The cache then runs in
  ``read_only`` mode: the migration is skipped, every later connection is
  opened through a ``file:…?mode=ro`` URI, reads keep working, and
  :meth:`DocCache.set` / :meth:`~DocCache.set_value` /
  :meth:`~DocCache.set_validators` are silent no-ops returning ``False`` —
  the cache is an optimisation, never a required dependency.  ``*_status()``
  reports ``read_only: true`` and that alone never changes ``overall``.
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
import urllib.request

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


def _read_only_url(db_path: str) -> str:
    """SQLite URI that opens ``db_path`` strictly read-only (``mode=ro``).

    ``urllib.request.pathname2url`` percent-escapes the path, so a cache
    directory containing spaces or a literal ``%`` can neither break out of the
    URI nor smuggle in another query parameter (``?mode=rwc``).  ``mode=ro``
    never creates a file and never writes: SQLite refuses a write up front
    instead of failing mid-transaction.
    """
    return f"file:{urllib.request.pathname2url(db_path)}?mode=ro"


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

    #: Columns :meth:`get_entry` reads.  A read-only database may predate the
    #: additive migration (which could not run), so in that mode the columns
    #: actually present are discovered from the file.
    _ROW_COLUMNS: tuple[str, ...] = (
        "value",
        "expires_at",
        "etag",
        "last_modified",
        "body",
    )

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = os.path.expanduser(str(db_path)) if db_path else default_db_path()
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        #: True when the database (or its directory) cannot be written.
        self.read_only = False
        #: Why the cache went read-only; surfaced by ``*_status()``.
        self.read_only_reason: str | None = None
        #: Columns really present in ``kv`` — narrowed in read-only mode.
        self._columns: frozenset[str] = frozenset(self._ROW_COLUMNS)
        # A13 B3: the schema is created/migrated exactly ONCE, here — never on a
        # per-operation connection.  Two reasons:
        #   * A12 F-A12-3: a ``PRAGMA table_info`` plus a possible ``ALTER TABLE``
        #     on every single read is work nobody asked for;
        #   * A12 F-A12-2: that per-connection migration was the exact line that
        #     raised ``OperationalError: attempt to write a readonly database``
        #     for a cache directory the user cannot write, which took down every
        #     tool in flutter-mcp.
        # P5: "cannot write" is now a supported state rather than a failure.
        # :meth:`_open_and_migrate` catches the migration's
        # ``sqlite3.OperationalError``, tells "cannot write" apart from "cannot
        # open at all" and degrades to read-only mode in the first case.  Only
        # the second still propagates, which keeps the contract every repo's
        # ``try/except DocCache()`` relies on (A13 B3) — and it is what the CI
        # bug was: the migration raised on a non-root runner while local runs
        # passed because root bypasses the ``chmod 0444`` the test applied.
        self._open_and_migrate()

    # -- internals ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """A short-lived connection for one operation.

        No schema work happens here (A13 B3) — :meth:`__init__` did it once.
        In read-only mode the connection is opened through a ``mode=ro`` URI,
        so SQLite refuses writes up front instead of mid-transaction.
        """
        if self.read_only:
            return sqlite3.connect(_read_only_url(self.db_path), uri=True)
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

    def _open_and_migrate(self) -> None:
        """Create/migrate the schema once, or fall back to read-only mode.

        Two failures look alike from SQLite's side and must be told apart:

        * **cannot write** — the file, or the directory holding it (the
          rollback journal lives there), is not writable: a root-owned
          ``cache.db``, ``0444``, a read-only mount.  The database is still
          readable, so the cache degrades: the migration is skipped, later
          connections use a ``mode=ro`` URI and writes become no-ops.
        * **cannot open** — the path is not a database at all (a directory) or
          the file does not exist in a directory that cannot create it.  There
          is nothing to read from that, so the error propagates and the caller
          drops the cache entirely (the A13 B3 contract).
        """
        if not self._write_possible() and self._read_only_probe():
            self._enter_read_only("the database or its directory is not writable")
            return
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                self._ensure_schema(conn)
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            if not self._read_only_probe():
                raise
            self._enter_read_only(f"{type(exc).__name__}: {exc}")

    def _write_possible(self) -> bool:
        """Cheap up-front hint: can SQLite write here at all?

        The rollback journal is created **next to** the database, so a
        directory without write permission makes even a 0644 ``cache.db``
        unwritable.  ``os.access`` is only a hint — root bypasses the
        permission bits entirely (which is how this bug stayed invisible
        locally) and permissions can change after construction — which is why
        :meth:`_enter_read_only` is also reachable from a refused write.
        """
        if os.path.exists(self.db_path) and not os.access(self.db_path, os.W_OK):
            return False
        return os.access(os.path.dirname(self.db_path) or ".", os.W_OK)

    def _read_only_probe(self) -> bool:
        """True when the file opens strictly read-only (``mode=ro``).

        This is what separates "cannot write" (recoverable → read-only mode)
        from "cannot open at all" (not recoverable → keep raising).
        """
        try:
            conn = sqlite3.connect(_read_only_url(self.db_path), uri=True)
        except sqlite3.Error:
            return False
        try:
            conn.execute("PRAGMA table_info(kv)").fetchall()
        except sqlite3.Error:
            return False
        finally:
            conn.close()
        return True

    def _discover_columns(self) -> set[str] | None:
        """Columns a read-only database really has, or ``None`` if uninspectable.

        An empty set is a real answer: the file exists but has no ``kv`` table
        (a 0-byte ``cache.db`` that cannot be migrated), which means every read
        is a miss.  ``None`` means "could not look", and then the previously
        known column set is kept rather than guessed at.
        """
        try:
            conn = self._connect()
        except sqlite3.Error:
            return None
        try:
            return {row[1] for row in conn.execute("PRAGMA table_info(kv)")}
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def _enter_read_only(self, reason: str) -> bool:
        """Downgrade this cache to read-only; returns ``False`` (the no-op).

        The cache is an optimisation, not a dependency: losing write access
        must cost the caching, never a tool call.  Later connections open with
        ``mode=ro``, writes are skipped up front, and ``*_status()`` says why.
        """
        self.read_only = True
        self.read_only_reason = reason
        columns = self._discover_columns()
        if columns is not None:
            self._columns = frozenset(columns)
        return False

    def _table_readable(self) -> bool:
        """False when there is no ``kv`` table to read (e.g. an empty file)."""
        return bool(self._columns)

    def _row_select(self) -> str:
        """SELECT list for :meth:`get_entry`, NULL for columns that are absent.

        A read-only database can predate the additive migration — the
        ``ALTER TABLE`` was skipped because nothing could be written — so
        ``etag`` / ``last_modified`` / ``body`` may not exist.  Naming them
        literally would raise ``no such column``; ``NULL AS`` keeps the row API
        uniform and reads as "this row has no validators yet".
        """
        return ", ".join(
            column if column in self._columns else f"NULL AS {column}"
            for column in self._ROW_COLUMNS
        )

    @staticmethod
    def _is_expired(expires_at: float | None, now: float | None = None) -> bool:
        if expires_at is None:  # rows without an expiry never expire
            return False
        return (now if now is not None else time.time()) >= expires_at

    # -- public API --------------------------------------------------------

    def get(self, key: str) -> str | None:
        """Return the stored value if present and not expired, else ``None``."""
        if not self._table_readable():
            return None
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
    ) -> bool:
        """Insert or update ``key`` with an expiry of ``time.time() + ttl_seconds``.

        Returns ``True`` when the row was written, ``False`` when the cache is
        read-only — a silent no-op, never an exception.

        ``etag`` / ``last_modified`` / ``body`` are optional and always written
        as given (``None`` clears them) so a row never keeps validators from a
        previous response — a stale validator could produce a wrong ``304``.
        Callers that only refresh the parsed value must use :meth:`set_value`,
        which leaves the revalidation columns alone.
        """
        if self.read_only:
            return False
        expires_at = time.time() + ttl_seconds
        try:
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
        except sqlite3.OperationalError as exc:
            return self._enter_read_only(f"{type(exc).__name__}: {exc}")
        return True

    def set_value(self, key: str, value: str, ttl_seconds: float) -> bool:
        """Upsert only ``value`` + expiry, leaving validators/body untouched.

        Returns ``True`` when the row was written, ``False`` when the cache is
        read-only — a silent no-op, never an exception.

        Used by the server, which caches the *parsed* result under the source
        URL while the fetcher keeps the raw body + ``etag`` of that same URL in
        the same row.  Using :meth:`set` here would NULL the validators and
        silently disable conditional GET.
        """
        if self.read_only:
            return False
        expires_at = time.time() + ttl_seconds
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO kv (key, value, expires_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value = excluded.value, expires_at = excluded.expires_at",
                    (key, value, expires_at),
                )
        except sqlite3.OperationalError as exc:
            return self._enter_read_only(f"{type(exc).__name__}: {exc}")
        return True

    def set_validators(
        self,
        key: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        body: bytes | str | None = None,
        ttl_seconds: float | None = None,
    ) -> bool:
        """Upsert only the revalidation columns (``etag``/``last_modified``/``body``).

        Returns ``True`` when the row was written, ``False`` when the cache is
        read-only — a silent no-op, never an exception.

        ``value`` is left alone (NULL for a brand-new row) and ``expires_at`` is
        only touched when ``ttl_seconds`` is given. A row whose ``value`` is NULL
        never answers :meth:`get` but can still be revalidated.
        """
        if self.read_only:
            return False
        expires_at = None if ttl_seconds is None else time.time() + ttl_seconds
        try:
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
        except sqlite3.OperationalError as exc:
            return self._enter_read_only(f"{type(exc).__name__}: {exc}")
        return True

    def get_entry(self, key: str, *, include_expired: bool = False) -> dict | None:
        """Return the whole row: ``{"value", "expired", "etag", "last_modified", "body"}``.

        With ``include_expired=True`` an expired row is returned too — that is
        exactly what the conditional-GET path needs: the validators and the body
        of a page whose TTL has run out, so the refresh can ask
        ``If-None-Match`` instead of downloading it again. ``None`` when the key
        is absent (or expired and ``include_expired`` is false). ``body`` comes
        back as ``bytes`` (BLOB column) or ``None``.
        """
        if not self._table_readable():
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._row_select()} FROM kv WHERE key = ?", (key,)
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
        if not self._table_readable():
            return {"entries": 0, "expired": 0}
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
        if not self._table_readable():
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row is not None else None

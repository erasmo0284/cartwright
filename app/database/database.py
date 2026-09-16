"""SQLite connection management and schema migration.

Design notes
------------

*Connection per thread.* The GUI thread, the browser worker thread and the
scheduler all read and write. SQLite connections are not safe to share across
threads, so each thread lazily gets its own via :class:`threading.local`.
WAL journalling lets readers proceed while a writer holds the write lock, and
``busy_timeout`` makes a contended write wait rather than raise.

*Migrations, not ad-hoc DDL.* The schema is defined only in
:mod:`app.database.migrations`. No repository is permitted to issue DDL, so
the schema of a given version is knowable from one place.

*Foreign keys on.* SQLite defaults them off, per connection. They are enabled
on every connection so that ``ON DELETE CASCADE`` actually fires.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.core.errors import AppError, ErrorCode
from app.core.timeutil import now_iso, utcnow
from app.database.migrations import MIGRATIONS, Migration

logger = logging.getLogger("app.database")

#: How long a connection waits for a competing writer before giving up.
BUSY_TIMEOUT_MS = 10_000

#: Keep this many rolling backups of the database.
BACKUP_RETENTION = 5


class Database:
    """Owns the database file, its connections and its schema version."""

    def __init__(self, path: Path, *, backups_dir: Path | None = None) -> None:
        self._path = path
        self._backups_dir = backups_dir
        self._local = threading.local()
        self._all_connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._write_lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    # ---- connections ----------------------------------------------------

    def connection(self) -> sqlite3.Connection:
        """This thread's connection, opening it on first use."""
        existing = getattr(self._local, "connection", None)
        if existing is not None:
            return existing

        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,  # explicit transaction control
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # Keep temp material in memory: the data directory may be on a slow
        # or roaming volume.
        connection.execute("PRAGMA temp_store = MEMORY")

        self._local.connection = connection
        with self._connections_lock:
            self._all_connections.append(connection)
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a write inside a transaction, serialised across threads.

        The process-wide lock is deliberate. Contention is low (one browser
        operation at a time), and serialising writes removes any chance of
        two threads interleaving a read-modify-write on purchase state --
        the one place where a lost update could cause a duplicate order.
        """
        connection = self.connection()
        with self._write_lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        """Run a statement outside an explicit transaction (autocommit)."""
        return self.connection().execute(sql, params)

    def query_all(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> list[sqlite3.Row]:
        return list(self.connection().execute(sql, params).fetchall())

    def query_one(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        return self.connection().execute(sql, params).fetchone()

    def query_scalar(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    # ---- schema ---------------------------------------------------------

    def current_version(self) -> int:
        """Highest applied migration version, or 0 for a fresh database."""
        connection = self.connection()
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                name       TEXT    NOT NULL,
                applied_at TEXT    NOT NULL
            )
            """
        )
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        return int(row["version"])

    def target_version(self) -> int:
        return max((migration.version for migration in MIGRATIONS), default=0)

    def migrate(self) -> int:
        """Apply every outstanding migration. Returns the resulting version.

        Each migration runs in its own transaction, so a failure leaves the
        database at the last fully applied version rather than half-migrated.
        """
        current = self.current_version()
        target = self.target_version()
        if current > target:
            raise AppError(
                ErrorCode.DATABASE_ERROR,
                context={
                    "reason": "database_newer_than_application",
                    "database_version": current,
                    "application_version": target,
                },
                detail_override=(
                    "This data was created by a newer version of the app. "
                    "Install the latest version to continue."
                ),
            )
        if current == target:
            return current

        if current > 0:
            self.backup(reason="pre-migration")

        for migration in sorted(MIGRATIONS, key=lambda item: item.version):
            if migration.version <= current:
                continue
            # ``name`` is a reserved LogRecord attribute: passing it in
            # ``extra`` makes logging raise KeyError. The reserved names are
            # checked by a test so this cannot creep back in.
            logger.info(
                "Applying migration",
                extra={
                    "version": migration.version,
                    "migration_name": migration.name,
                },
            )
            try:
                self._apply(migration)
            except sqlite3.Error as exc:
                raise AppError(
                    ErrorCode.DATABASE_ERROR,
                    context={
                        "reason": "migration_failed",
                        "version": migration.version,
                        "name": migration.name,
                    },
                    cause=exc,
                ) from exc

        final = self.current_version()
        logger.info("Schema up to date", extra={"version": final})
        return final

    def _apply(self, migration: Migration) -> None:
        connection = self.connection()
        # ``executescript`` issues its own COMMIT before running, which would
        # break out of the transaction below, so the script is split and the
        # statements are run individually. That keeps each migration atomic:
        # a failure rolls the whole thing back rather than leaving half a
        # schema behind.
        statements = split_sql_statements(migration.sql)
        with self._write_lock:
            # Foreign key enforcement cannot be changed inside a transaction,
            # so it is suspended around the DDL and re-checked afterwards.
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    connection.execute(statement)
                if migration.apply is not None:
                    migration.apply(connection)
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (migration.version, migration.name, now_iso()),
                )
            except Exception:
                connection.execute("ROLLBACK")
                connection.execute("PRAGMA foreign_keys = ON")
                raise
            connection.execute("COMMIT")
            connection.execute("PRAGMA foreign_keys = ON")
            violations = list(connection.execute("PRAGMA foreign_key_check").fetchall())
            if violations:
                raise sqlite3.IntegrityError(
                    f"Migration {migration.version} left "
                    f"{len(violations)} foreign key violation(s)"
                )

    # ---- maintenance ----------------------------------------------------

    def backup(self, *, reason: str = "manual") -> Path | None:
        """Copy the database to the backups directory using SQLite's own
        backup API, which is safe while other connections are open.
        """
        if self._backups_dir is None:
            return None
        self._backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        destination = self._backups_dir / f"app-{stamp}-{reason}.db"
        target: sqlite3.Connection | None = None
        try:
            # ``with sqlite3.connect(...)`` only commits -- it does not close.
            # On Windows an open handle makes the file undeletable, so the
            # connection is closed explicitly.
            target = sqlite3.connect(destination)
            self.connection().backup(target)
        except sqlite3.Error as exc:
            logger.warning("Backup failed", extra={"reason": reason}, exc_info=exc)
            return None
        finally:
            if target is not None:
                target.close()
        self._prune_backups()
        logger.info("Database backed up", extra={"file": destination.name})
        return destination

    def _prune_backups(self) -> None:
        if self._backups_dir is None:
            return
        backups = sorted(
            self._backups_dir.glob("app-*.db"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[BACKUP_RETENTION:]:
            try:
                stale.unlink()
            except OSError:
                continue

    def vacuum(self) -> None:
        """Reclaim space. Cheap for a database of this size."""
        with self._write_lock:
            self.connection().execute("VACUUM")

    def integrity_check(self) -> tuple[bool, str]:
        """Run SQLite's own integrity check for the diagnostics screen."""
        try:
            row = self.connection().execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            return False, str(exc)
        result = str(row[0]) if row else "unknown"
        return result == "ok", result

    def file_size_bytes(self) -> int:
        try:
            return self._path.stat().st_size
        except OSError:
            return 0

    def close_current_thread(self) -> None:
        """Close this thread's connection. Called as a worker thread exits."""
        connection = getattr(self._local, "connection", None)
        if connection is None:
            return
        with self._connections_lock:
            if connection in self._all_connections:
                self._all_connections.remove(connection)
        try:
            connection.close()
        except sqlite3.Error:
            pass
        self._local.connection = None

    def close_all(self) -> None:
        """Close every connection. Called once during shutdown."""
        with self._connections_lock:
            connections = list(self._all_connections)
            self._all_connections.clear()
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._local.connection = None


def split_sql_statements(script: str) -> list[str]:
    """Split a migration script into individually executable statements.

    Uses :func:`sqlite3.complete_statement`, which understands string literals
    and nested ``BEGIN ... END`` blocks, so a future migration containing a
    trigger splits correctly where a naive ``split(";")`` would not.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue  # skip leading comments and blank lines between statements
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement and statement != ";":
                statements.append(statement)
            buffer = ""
    remainder = buffer.strip()
    if remainder:
        raise ValueError(f"Migration script ends with an incomplete statement: {remainder[:120]!r}")
    return statements


def restore_backup(backup_file: Path, database_file: Path) -> None:
    """Replace the live database with a backup.

    The caller must have closed the :class:`Database` first. WAL sidecar
    files are removed so SQLite cannot replay a journal belonging to the
    replaced file.
    """
    for suffix in ("-wal", "-shm"):
        sidecar = database_file.with_name(database_file.name + suffix)
        sidecar.unlink(missing_ok=True)
    shutil.copy2(backup_file, database_file)

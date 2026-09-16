"""The settings key-value table.

Settings had their SQL inline in :mod:`app.config` at first, which made
``SettingsService`` an unacknowledged seventh repository and broke the rule
that only this package issues SQL. The queries are trivial, but keeping them
here means the rule is true rather than nearly true -- and a reader looking
for "where does the schema get touched?" has one answer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.timeutil import now_iso
from app.database.database import Database

logger = logging.getLogger("app.database.settings")


class SettingsRepository:
    """Reads and writes the ``settings`` table.

    Values are stored as JSON text so a setting can be a scalar, a mapping or
    a list without a schema change.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def load_all(self) -> dict[str, Any]:
        """Every stored setting, decoded.

        A value that will not parse is skipped and logged rather than
        raising: one corrupt row must not stop the application starting, and
        the caller falls back to that field's default.
        """
        stored: dict[str, Any] = {}
        for row in self._db.query_all("SELECT key, value FROM settings"):
            try:
                stored[str(row["key"])] = json.loads(row["value"])
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring an unreadable setting", extra={"key": row["key"]}
                )
        return stored

    def save(self, values: dict[str, Any]) -> None:
        """Write several settings in one transaction."""
        if not values:
            return
        stamp = now_iso()
        with self._db.transaction() as conn:
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings (key, value, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (key, json.dumps(value), stamp),
                )

    def delete_all_except(self, preserved: frozenset[str]) -> None:
        """Remove every setting except the named keys.

        Used by "reset to defaults", which deliberately keeps the stored
        Amazon account status: clearing it would make a browser profile that
        is still signed in look disconnected.
        """
        if not preserved:
            with self._db.transaction() as conn:
                conn.execute("DELETE FROM settings")
            return
        keys = tuple(sorted(preserved))
        placeholders = ", ".join("?" for _ in keys)
        with self._db.transaction() as conn:
            conn.execute(
                f"DELETE FROM settings WHERE key NOT IN ({placeholders})", keys
            )

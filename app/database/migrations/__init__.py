"""Schema migrations.

A migration is an immutable, numbered unit of schema change. Once a version
has shipped its SQL is never edited -- a correction becomes a new migration.
:data:`MIGRATIONS` is the ordered registry that
:meth:`app.database.database.Database.migrate` walks.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from app.database.migrations import m0001_initial, m0002_rules_brand


@dataclass(frozen=True)
class Migration:
    """One numbered schema change.

    ``sql`` is executed with ``executescript``. ``apply`` is an optional Python
    hook for data transformations that SQL alone cannot express; it runs inside
    the same transaction, after the SQL.
    """

    version: int
    name: str
    sql: str
    apply: Callable[[sqlite3.Connection], None] | None = None


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="initial_schema",
        sql=m0001_initial.SQL,
    ),
    Migration(
        version=2,
        name="rules_brand",
        sql=m0002_rules_brand.SQL,
    ),
)

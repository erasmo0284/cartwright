"""Repositories: the only code permitted to issue SQL.

Each repository owns one table group and exposes intention-named methods
(``claim_due_jobs``, ``record_submission``) rather than generic CRUD, so the
invariants of that table group live next to its queries. No repository issues
DDL -- the schema comes from :mod:`app.database.migrations` alone.
"""

from __future__ import annotations

from app.database.repositories.activity import ActivityRepository
from app.database.repositories.orders import OrderRepository
from app.database.repositories.products import ProductRepository
from app.database.repositories.purchases import PurchaseRepository
from app.database.repositories.rules import RulesRepository
from app.database.repositories.settings import SettingsRepository
from app.database.repositories.watch import WatchRepository

__all__ = [
    "ActivityRepository",
    "OrderRepository",
    "ProductRepository",
    "PurchaseRepository",
    "RulesRepository",
    "SettingsRepository",
    "WatchRepository",
]

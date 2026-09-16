"""The composition root.

Everything the application needs is constructed here, once, in dependency
order, and handed to the UI. Nothing else reaches for a global: a screen is
given the services it uses, which is what makes the screens testable and the
startup order explicit.

Shutdown matters as much as startup. The browser worker owns a real Chromium
process and a signed-in profile, and closing it cleanly is what preserves the
user's Amazon session. :meth:`AppContext.shutdown` therefore runs in a fixed
order with bounded waits, so a wedged step cannot leave an orphaned browser
behind.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, Signal

from app.automation.browser_manager import BrowserManager, configure_browsers_path
from app.automation.browser_worker import BrowserWorker, WorkerState
from app.config import SettingsService
from app.core.errors import AppError
from app.database.database import Database
from app.database.repositories import (
    ActivityRepository,
    OrderRepository,
    ProductRepository,
    PurchaseRepository,
    RulesRepository,
    WatchRepository,
)
from app.diagnostics.logger import setup_logging
from app.monitoring.monitor_service import MonitorService
from app.monitoring.scheduler import Scheduler
from app.paths import AppPaths, get_paths
from app.purchasing.product_service import ProductService
from app.purchasing.purchase_service import PurchaseService
from app.version import VERSION

logger = logging.getLogger("app.context")

#: How long to wait for the browser thread to finish on shutdown. Generous,
#: because the last thing it does is close Chromium cleanly, and cutting that
#: short can cost the user their signed-in session.
SHUTDOWN_TIMEOUT_MS = 25_000


@dataclass(frozen=True)
class Repositories:
    """The repository set, grouped so screens can be given just what they need."""

    products: ProductRepository
    rules: RulesRepository
    watches: WatchRepository
    purchases: PurchaseRepository
    orders: OrderRepository
    activity: ActivityRepository


class AppContext(QObject):
    """Owns every long-lived object in the application."""

    #: Emitted when the worker's state changes, for the status bar and tray.
    worker_state_changed = Signal(str)
    #: Emitted with a message while the browser is being installed.
    install_progress = Signal(str)

    def __init__(self, paths: AppPaths | None = None) -> None:
        super().__init__()
        self.paths = (paths or get_paths()).ensure()

        # Playwright reads this in the Node driver it spawns, so it must be
        # set before any Playwright import happens anywhere.
        configure_browsers_path(self.paths)

        setup_logging(self.paths.logs_dir)
        logger.info("Starting up", extra={"version": VERSION})

        self.database = Database(
            self.paths.database_file, backups_dir=self.paths.backups_dir
        )
        try:
            self.database.migrate()
        except Exception:
            # Close before the failure escapes. The caller's response to a
            # database it cannot open is to move the file aside and restore a
            # backup, and on Windows an open SQLite handle can prevent that.
            # A file so damaged that the PRAGMAs failed is already closed by
            # ``Database.connection``; this covers the rest -- a migration
            # that fails on an otherwise healthy, and therefore open, file.
            self.database.close_all()
            raise

        self.repositories = Repositories(
            products=ProductRepository(self.database),
            rules=RulesRepository(self.database),
            watches=WatchRepository(self.database),
            purchases=PurchaseRepository(self.database),
            orders=OrderRepository(self.database),
            activity=ActivityRepository(self.database),
        )
        self.settings = SettingsService(self.database, parent=self)
        self.settings.reload()

        # Apply the stored log level now that settings are readable.
        setup_logging(self.paths.logs_dir, level=self.settings.current.log_level)

        self._thread = QThread(self)
        self._thread.setObjectName("browser")
        self.worker = BrowserWorker(self.paths)
        self.worker.moveToThread(self._thread)
        self._thread.started.connect(self.worker.run)
        self.worker.state_changed.connect(self.worker_state_changed)
        self.worker.install_progress.connect(self.install_progress)

        self.notifier = self._build_notifier()

        self.product_service = ProductService(
            self.worker,
            self.repositories.products,
            self.repositories.activity,
            self.settings,
            parent=self,
        )
        self.purchase_service = PurchaseService(
            worker=self.worker,
            products=self.repositories.products,
            rules_repo=self.repositories.rules,
            purchases=self.repositories.purchases,
            orders=self.repositories.orders,
            watches=self.repositories.watches,
            activity=self.repositories.activity,
            settings=self.settings,
            parent=self,
        )
        self.scheduler = Scheduler(parent=self)
        self.monitor_service = MonitorService(
            worker=self.worker,
            scheduler=self.scheduler,
            watches=self.repositories.watches,
            products=self.repositories.products,
            rules_repo=self.repositories.rules,
            purchases=self.repositories.purchases,
            activity=self.repositories.activity,
            settings=self.settings,
            purchase_service=self.purchase_service,
            notifier=self.notifier,
            parent=self,
        )
        self.health = self._build_health()
        self._shut_down = False

    # ---- optional subsystems --------------------------------------------

    def _build_notifier(self) -> object | None:
        """The notifier, or ``None`` if notifications are unavailable.

        Constructed defensively: a failure to set up a notification channel
        must never stop the application from starting, because everything
        else -- including the safety checks -- still works without it.
        """
        try:
            from app.notifications.notifier import Notifier

            return Notifier(
                self.settings, self.repositories.activity, tray=None, parent=self
            )
        except Exception:  # noqa: BLE001
            logger.warning("Notifications are unavailable", exc_info=True)
            return None

    def _build_health(self) -> object | None:
        try:
            from app.diagnostics.health import HealthService

            return HealthService(
                paths=self.paths,
                database=self.database,
                settings=self.settings,
                browser_manager=self.worker.manager,
                notifier=self.notifier,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Health checks are unavailable", exc_info=True)
            return None

    @property
    def browser(self) -> BrowserManager:
        return self.worker.manager

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> list[int]:
        """Start background work. Returns purchases needing attention.

        Crash recovery runs before the scheduler starts, so an interrupted
        order is resolved into ``UNKNOWN`` -- and therefore blocks any new
        purchase of that product -- before monitoring could trigger one.
        """
        self._thread.start()

        uncertain: list[int] = []
        try:
            uncertain = self.purchase_service.recover_after_restart()
        except AppError:
            logger.exception("Could not recover interrupted purchases")

        self._restore_pending_carts()

        if self.settings.current.monitoring_enabled:
            self.monitor_service.start()

        return uncertain

    def _restore_pending_carts(self) -> None:
        """Queue a restore for any cart left altered by an interrupted run.

        The user's cart is theirs; leaving items in Saved for later because
        the app was closed mid-purchase would be a mess it created and should
        clean up.
        """
        from app.automation.cart_manager import IsolationJournal

        pending: list[tuple[int, IsolationJournal]] = []
        for job in self.repositories.purchases.list_recent(limit=50):
            journal = IsolationJournal.from_mapping(job.isolation_journal)
            if journal.has_pending_restore:
                pending.append((job.id, journal))

        for job_id, journal in pending:
            logger.info(
                "Queueing a cart restore left over from a previous run",
                extra={"purchase_job_id": job_id},
            )

            def restore(session: object, journal: IsolationJournal = journal, job_id: int = job_id) -> None:
                from app.automation.amazon_adapter import ADAPTER

                ADAPTER.restore_cart(session, journal)  # type: ignore[arg-type]
                self.repositories.purchases.set_isolation_journal(
                    job_id, journal.to_json()
                )

            self.worker.submit(
                "Put your cart back the way it was",
                restore,  # type: ignore[arg-type]
                context={"kind": "restore", "job_id": job_id},
            )

    def shutdown(self) -> None:
        """Stop everything in a safe order. Idempotent."""
        if self._shut_down:
            return
        self._shut_down = True
        logger.info("Shutting down")

        try:
            self.monitor_service.stop()
        except Exception:  # noqa: BLE001
            logger.warning("Monitoring did not stop cleanly", exc_info=True)

        # Ask the worker to finish; it closes the browser as it exits, which
        # is what flushes the signed-in session to the profile.
        self.worker.shutdown()
        if self._thread.isRunning():
            self._thread.quit()
            if not self._thread.wait(SHUTDOWN_TIMEOUT_MS):
                logger.error(
                    "Browser thread did not stop in time; terminating it"
                )
                self._thread.terminate()
                self._thread.wait(3_000)

        try:
            self.repositories.activity.prune()
        except Exception:  # noqa: BLE001
            logger.debug("Could not prune the activity feed", exc_info=True)

        try:
            self.database.close_all()
        except Exception:  # noqa: BLE001
            logger.warning("Database did not close cleanly", exc_info=True)

        logger.info("Shutdown complete")

    @property
    def worker_state(self) -> WorkerState:
        return self.worker.state

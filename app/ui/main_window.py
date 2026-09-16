"""The application shell.

Holds the navigation, the pages and the tray icon, and is the single place
where service signals are turned into things a person sees. Keeping that
wiring here rather than inside the pages means a page can be built and tested
on its own, and there is one file to read to find out what happens when a
purchase is blocked.

Two behaviours deserve their reasoning stated:

**Closing the window does not quit.** The point of the application is to keep
watching while the user gets on with something else, so the close button
hides to the tray by default and says so the first time. The user can change
that in Settings, and Exit from the tray always really exits.

**The test-mode banner is always visible when test mode is on.** It is the
difference between a button that spends money and one that does not, so it
is not tucked away in Settings.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from app.automation.browser_worker import WorkerState
from app.automation.selectors import cart_url, home_url, orders_url
from app.branding import BRAND
from app.config import CloseButtonAction, NotificationKind
from app.core.errors import ActionHint, AppError, ErrorCode
from app.database.records import WatchAction
from app.purchasing.models import PurchaseMode
from app.purchasing.product_service import Inspection
from app.purchasing.purchase_service import (
    BlockedOutcome,
    PurchaseReview,
    TestRunResult,
)
from app.ui.activity.activity_page import ActivityPage
from app.ui.app_context import AppContext
from app.ui.dashboard.dashboard_page import DashboardPage
from app.ui.dialogs import (
    AboutDialog,
    BlockedDialog,
    CartPermissionDialog,
    ConfirmPurchaseDialog,
    TestResultDialog,
    UncertainOrderDialog,
    VerificationDialog,
    confirm,
    show_error,
)
from app.ui.purchase.new_purchase_page import NewPurchasePage
from app.ui.settings.settings_page import SettingsPage
from app.ui.theme import IconSet, ThemeManager, StatusSeverity, tray_icon
from app.ui.watchlist.watch_editor_dialog import WatchEditorDialog
from app.ui.watchlist.watchlist_page import WatchlistPage
from app.winint.tray import TrayController
from app.winint.window_raise import show_and_raise

logger = logging.getLogger("app.ui.main_window")

#: Navigation entries: object name, label, icon.
NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("dashboard", "Overview", "dashboard"),
    ("purchase", "New Purchase", "purchase"),
    ("watchlist", "Watch List", "watchlist"),
    ("activity", "Activity", "activity"),
    ("settings", "Settings", "settings"),
)


class MainWindow(QMainWindow):
    """The single application window."""

    def __init__(
        self,
        context: AppContext,
        theme: ThemeManager,
        *,
        guard: object | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._context = context
        self._theme = theme
        self._guard = guard
        # Built from the application's theme, not the system's, and rebuilt
        # whenever the theme changes -- otherwise the glyphs keep the colour
        # they had when the window was created and vanish against the new
        # background.
        self._icons = IconSet(theme.current_tokens())
        self._really_quitting = False
        self._told_about_tray = False
        self._verification_dialog: VerificationDialog | None = None
        self._pending_inspection: Inspection | None = None

        self.setWindowTitle(BRAND.display_name)
        self.setMinimumSize(QSize(940, 640))
        self.resize(1120, 760)

        self._build_ui()
        self._build_tray()
        self._connect_services()
        self._refresh_all()

    # ---- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._banner = QLabel()
        self._banner.setObjectName("TestModeBanner")
        self._banner.setWordWrap(True)
        self._banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._banner.setVisible(False)
        outer.addWidget(self._banner)

        split = QWidget()
        split_layout = QHBoxLayout(split)
        split_layout.setContentsMargins(0, 0, 0, 0)
        split_layout.setSpacing(0)

        self._nav = QListWidget()
        self._nav.setObjectName("NavList")
        self._nav.setFixedWidth(196)
        self._nav.setIconSize(QSize(18, 18))
        self._nav.setAccessibleName("Sections")
        self._nav.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        for key, label, icon in NAV_ITEMS:
            item = QListWidgetItem(self._icons.icon(icon), label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setSizeHint(QSize(0, 38))
            self._nav.addItem(item)
        self._nav.currentRowChanged.connect(self._on_nav_changed)
        split_layout.addWidget(self._nav)

        self._pages = QStackedWidget()

        self._dashboard = DashboardPage(
            watches=self._context.repositories.watches,
            activity=self._context.repositories.activity,
            orders=self._context.repositories.orders,
            settings=self._context.settings,
        )
        self._purchase_page = NewPurchasePage(self._context.settings)
        self._watchlist = WatchlistPage(
            watches=self._context.repositories.watches,
            products=self._context.repositories.products,
        )
        self._activity = ActivityPage(self._context)
        self._settings_page = SettingsPage(self._context, self._theme)

        for page in (
            self._dashboard,
            self._purchase_page,
            self._watchlist,
            self._activity,
            self._settings_page,
        ):
            self._pages.addWidget(page)

        split_layout.addWidget(self._pages, 1)
        outer.addWidget(split, 1)

        self.setCentralWidget(central)

        status = QStatusBar()
        status.setSizeGripEnabled(True)
        self._status_label = QLabel()
        status.addWidget(self._status_label, 1)
        self._monitor_label = QLabel()
        status.addPermanentWidget(self._monitor_label)
        self.setStatusBar(status)

        self._build_shortcuts()
        self._nav.setCurrentRow(0)

    def _build_shortcuts(self) -> None:
        for index, (key, label, _icon) in enumerate(NAV_ITEMS):
            action = QAction(label, self)
            action.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            action.triggered.connect(
                lambda _checked=False, row=index: self._nav.setCurrentRow(row)
            )
            self.addAction(action)

        new_purchase = QAction("New purchase", self)
        new_purchase.setShortcut(QKeySequence.StandardKey.New)
        new_purchase.triggered.connect(self._show_new_purchase)
        self.addAction(new_purchase)

        about = QAction("About", self)
        about.setShortcut(QKeySequence("F1"))
        about.triggered.connect(lambda: AboutDialog(self).exec())
        self.addAction(about)

    def _build_tray(self) -> None:
        self._tray: TrayController | None = None
        if not TrayController.is_available():
            logger.warning("No system tray is available on this desktop")
            return
        self._tray = TrayController(tray_icon, parent=self)
        self._tray.open_requested.connect(self._on_tray_open)
        self._tray.pause_toggled.connect(self._on_tray_pause)
        self._tray.check_now_requested.connect(
            lambda: self._context.monitor_service.check_now()
        )
        self._tray.exit_requested.connect(self._on_exit)
        self._tray.show()
        # The notifier can now fall back to a tray balloon if a real toast
        # cannot be shown.
        if self._context.notifier is not None:
            setattr(self._context.notifier, "_tray", self._tray)

    # ---- wiring ----------------------------------------------------------

    def _connect_services(self) -> None:
        context = self._context

        context.worker_state_changed.connect(self._on_worker_state)
        context.install_progress.connect(self._on_install_progress)

        product = context.product_service
        product.inspected.connect(self._on_inspected)
        product.inspection_failed.connect(self._purchase_page.show_failure)
        product.progress.connect(
            lambda _task, message: self._purchase_page.update_progress(message)
        )
        product.inspection_cancelled.connect(self._purchase_page.show_cancelled)

        purchase = context.purchase_service
        purchase.ready_for_confirmation.connect(self._on_ready_for_confirmation)
        purchase.test_completed.connect(self._on_test_completed)
        purchase.purchase_blocked.connect(self._on_purchase_blocked)
        purchase.purchase_completed.connect(self._on_purchase_completed)
        purchase.purchase_failed.connect(self._on_purchase_failed)
        purchase.purchase_uncertain.connect(self._on_purchase_uncertain)
        purchase.purchase_cancelled.connect(lambda _id: self._refresh_all())
        purchase.progress.connect(self._on_purchase_progress)

        monitor = context.monitor_service
        monitor.watchlist_changed.connect(self._refresh_all)
        monitor.monitoring_changed.connect(self._on_monitoring_changed)
        monitor.progress.connect(self._watchlist.set_progress)
        monitor.check_completed.connect(
            lambda result: self._watchlist.clear_progress(result.watch_job_id)
        )
        monitor.check_failed.connect(
            lambda job_id, _error: self._watchlist.clear_progress(job_id)
        )

        context.settings.changed.connect(lambda _s: self._refresh_banner())
        self._theme.theme_changed.connect(self._on_theme_changed)

        # Dashboard
        self._dashboard.new_purchase_requested.connect(self._show_new_purchase)
        self._dashboard.add_watch_requested.connect(self._show_new_purchase)
        self._dashboard.open_amazon_requested.connect(self._open_amazon)
        self._dashboard.connect_amazon_requested.connect(self._connect_amazon)
        self._dashboard.toggle_monitoring_requested.connect(self._toggle_monitoring)
        self._dashboard.show_watchlist_requested.connect(
            lambda: self._nav.setCurrentRow(2)
        )
        self._dashboard.show_activity_requested.connect(
            lambda: self._nav.setCurrentRow(3)
        )
        self._dashboard.show_attention_requested.connect(
            lambda: self._nav.setCurrentRow(2)
        )

        # New purchase
        self._purchase_page.inspect_requested.connect(self._on_inspect_requested)
        self._purchase_page.cancel_requested.connect(self._on_cancel_inspection)
        self._purchase_page.prepare_requested.connect(self._on_prepare_requested)
        self._purchase_page.watch_requested.connect(self._on_watch_requested)
        self._purchase_page.open_product_requested.connect(self._open_url)

        # Watch list
        self._watchlist.add_watch_requested.connect(self._show_new_purchase)
        self._watchlist.check_now_requested.connect(self._on_check_now)
        self._watchlist.pause_requested.connect(
            self._context.monitor_service.pause_watch
        )
        self._watchlist.resume_requested.connect(
            self._context.monitor_service.resume_watch
        )
        self._watchlist.edit_requested.connect(self._on_edit_watch)
        self._watchlist.remove_requested.connect(self._on_remove_watch)
        self._watchlist.open_product_requested.connect(self._on_open_watch_product)
        self._watchlist.view_history_requested.connect(self._on_view_history)

        # Settings
        settings_page = self._settings_page
        for signal_name, handler in (
            ("open_amazon_requested", self._open_amazon),
            ("test_connection_requested", self._test_connection),
            ("reconnect_requested", self._connect_amazon),
            ("clear_session_requested", self._clear_session),
            ("repair_browser_requested", self._repair_browser),
            ("auto_buy_consent_requested", self._ask_auto_buy_consent),
        ):
            signal = getattr(settings_page, signal_name, None)
            if signal is not None:
                signal.connect(handler)

    # ---- lifecycle -------------------------------------------------------

    def prepare(self, *, start_in_tray: bool) -> None:
        """Show the window, or start hidden in the tray."""
        # Connected first, and whichever way this starts: a second launch
        # asks the running instance to show itself, and an app that started
        # in the tray is exactly the case where the user needs that most.
        if self._guard is not None:
            activate = getattr(self._guard, "activate_requested", None)
            if activate is not None:
                activate.connect(lambda _payload: self._on_tray_open())

        if start_in_tray and self._tray is not None:
            logger.info("Starting hidden in the system tray")
            self._tray.show_message(
                BRAND.display_name,
                "Running in the system tray. Watching continues in the "
                "background.",
                4,
            )
            return
        self.show()
        show_and_raise(self)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        """Hide to the tray unless the user asked to exit, or told us to."""
        if self._really_quitting:
            event.accept()
            return

        action = self._context.settings.current.close_button_action
        stay_running = (
            action is CloseButtonAction.MINIMIZE_TO_TRAY
            and self._tray is not None
            and self._context.settings.current.continue_in_tray
        )
        if not stay_running:
            self._on_exit()
            if not self._really_quitting:
                # The user declined to stop a purchase that is in progress.
                # Accepting the close here would hide the window while the
                # process kept running, with no tray icon to bring it back.
                event.ignore()
                return
            event.accept()
            return

        event.ignore()
        self.hide()
        if not self._told_about_tray:
            self._told_about_tray = True
            self._tray.show_message(  # type: ignore[union-attr]
                BRAND.display_name,
                "Still running here, and still watching. Right-click this icon "
                "to quit.",
                5,
            )

    def _on_exit(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self._busy_with_a_purchase() and not confirm(
            self,
            title="A purchase is in progress",
            message=(
                "Closing now stops it. If an order has already been submitted, "
                "the app will ask you to check your Amazon orders next time it "
                "starts."
            ),
            confirm_text="Close anyway",
            destructive=True,
        ):
            return
        self._really_quitting = True
        logger.info("Exiting at the user's request")
        QApplication.instance().quit()  # type: ignore[union-attr]

    def _busy_with_a_purchase(self) -> bool:
        return any(
            job.state.is_live
            for job in self._context.repositories.purchases.list_recent(limit=20)
        )

    # ---- navigation ------------------------------------------------------

    def _on_nav_changed(self, row: int) -> None:
        if row < 0:
            return
        self._pages.setCurrentIndex(row)
        key = NAV_ITEMS[row][0]
        if key == "purchase":
            self._purchase_page.focus_input()
        elif key == "watchlist":
            self._watchlist.refresh()
        elif key == "activity":
            self._activity.refresh()
        elif key == "dashboard":
            self._dashboard.refresh()

    def _show_new_purchase(self) -> None:
        self._nav.setCurrentRow(1)
        show_and_raise(self)

    # ---- refresh ---------------------------------------------------------

    def _refresh_all(self) -> None:
        self._dashboard.refresh()
        self._watchlist.refresh()
        self._refresh_banner()
        self._refresh_tray()
        self._dashboard.set_monitoring(self._context.monitor_service.is_monitoring)
        self._monitor_label.setText(
            "Monitoring on"
            if self._context.monitor_service.is_monitoring
            else "Monitoring paused"
        )

    def _on_theme_changed(self, _scheme: str) -> None:
        """Re-render the icons for the new theme.

        Icons are rasterised from SVG in a fixed colour, so a theme change
        needs new pixmaps; without this the navigation glyphs stay the old
        colour and become invisible against the new background.
        """
        tokens = self._theme.current_tokens()
        self._icons = IconSet(tokens)
        for row, (_key, _label, icon_name) in enumerate(NAV_ITEMS):
            item = self._nav.item(row)
            if item is not None:
                item.setIcon(self._icons.icon(icon_name))

        # Widgets that rasterise an icon or paint themselves (sparklines,
        # status dots) cannot pick a colour up from the stylesheet, so they
        # are told explicitly.
        from app.ui.components.common import apply_theme

        apply_theme(self, tokens)

    def _refresh_banner(self) -> None:
        if self._context.settings.current.test_mode:
            self._banner.setText(
                "TEST MODE - the app will go all the way to Amazon's order "
                "button and stop. No order can be placed."
            )
            self._banner.setVisible(True)
        else:
            self._banner.setVisible(False)
        self._dashboard.refresh()

    def _refresh_tray(self) -> None:
        if self._tray is None:
            return
        counts = self._context.monitor_service.counts()
        self._tray.set_state(
            watching=counts.get("watching", 0),
            needs_attention=counts.get("needs_attention", 0),
            paused=not self._context.monitor_service.is_monitoring,
            monitoring_enabled=self._context.settings.current.monitoring_enabled,
        )

    # ---- worker ----------------------------------------------------------

    def _on_worker_state(self, state_value: str) -> None:
        try:
            state = WorkerState(state_value)
        except ValueError:
            return
        self._status_label.setText(state.label)
        if state is WorkerState.NEEDS_BROWSER:
            self._status_label.setText(
                "The browser needs installing. Use Repair browser in Settings."
            )

    def _on_install_progress(self, message: str) -> None:
        self._status_label.setText(message)

    # ---- inspection ------------------------------------------------------

    def _on_inspect_requested(self, text: str) -> None:
        try:
            task_id = self._context.product_service.inspect(text)
        except AppError as error:
            self._purchase_page.show_failure(error)
            return
        self._inspect_task = task_id
        self._purchase_page.show_progress("Opening Amazon...")

    def _on_cancel_inspection(self) -> None:
        task_id = getattr(self, "_inspect_task", None)
        if task_id is not None:
            self._context.product_service.cancel(task_id)

    def _on_inspected(self, inspection: Inspection) -> None:
        self._pending_inspection = inspection
        self._purchase_page.show_inspection(inspection)
        self._activity.refresh()
        self._dashboard.refresh()

    # ---- purchase --------------------------------------------------------

    def _on_prepare_requested(self, rules: object, allow_set_aside: bool) -> None:
        inspection = self._pending_inspection
        if inspection is None:
            return
        try:
            self._context.purchase_service.start(
                product_id=inspection.record.id,
                rules=rules,  # type: ignore[arg-type]
                mode=PurchaseMode.ASSISTED,
                allow_set_aside=allow_set_aside,
            )
        except AppError as error:
            self._handle_error(error)
            return
        self._purchase_page.show_progress("Preparing the order...")

    def _on_purchase_progress(self, _job_id: int, message: str) -> None:
        self._purchase_page.update_progress(message)
        self._status_label.setText(message)

    def _on_ready_for_confirmation(self, review: PurchaseReview) -> None:
        self._purchase_page.update_progress("Ready for your confirmation.")
        if self._context.notifier is not None and review.order_total is not None:
            from app.notifications import notifier as builders

            self._context.notifier.notify(
                builders.purchase_ready(
                    review.product.display_title,
                    review.order_total,
                    review.purchase_job_id,
                )
            )
        show_and_raise(self)

        dialog = ConfirmPurchaseDialog(review, self)
        if dialog.exec() != ConfirmPurchaseDialog.DialogCode.Accepted:
            self._context.purchase_service.cancel(review.purchase_job_id)
            self._purchase_page.show_cancelled()
            self._refresh_all()
            return
        try:
            self._context.purchase_service.confirm(review.purchase_job_id)
            self._purchase_page.show_progress("Placing your order...")
        except AppError as error:
            self._handle_error(error)

    def _on_test_completed(self, result: TestRunResult) -> None:
        self._purchase_page.update_progress("Test finished.")
        show_and_raise(self)
        dialog = TestResultDialog(result, self)
        dialog.exec()
        if dialog.open_settings_requested:
            self._nav.setCurrentRow(4)
        self._refresh_all()
        self._activity.refresh()

    def _on_purchase_blocked(self, outcome: BlockedOutcome) -> None:
        self._purchase_page.update_progress("Stopped. Nothing was ordered.")
        if self._context.notifier is not None:
            from app.notifications import notifier as builders

            reason = (
                outcome.report.blocking_failures[0].title
                if outcome.report and outcome.report.blocking_failures
                else outcome.error.title
            )
            self._context.notifier.notify(
                builders.purchase_blocked(
                    outcome.product.display_title, reason, outcome.purchase_job_id
                )
            )

        if outcome.error.code is ErrorCode.CART_CONFLICT:
            self._offer_cart_isolation(outcome)
        else:
            show_and_raise(self)
            dialog = BlockedDialog(outcome, self)
            dialog.exec()
            self._perform_action(dialog.chosen_action, outcome)
        self._refresh_all()
        self._activity.refresh()

    def _offer_cart_isolation(self, outcome: BlockedOutcome) -> None:
        """Ask whether the other cart items may be set aside, then retry."""
        titles = outcome.error.context.get("unexpected") or []
        from app.purchasing.models import CartLine

        lines = tuple(
            CartLine(asin=None, title=str(title), quantity=1) for title in titles
        )
        if not lines:
            show_and_raise(self)
            BlockedDialog(outcome, self).exec()
            return

        show_and_raise(self)
        dialog = CartPermissionDialog(
            product_title=outcome.product.display_title,
            foreign_lines=lines,
            parent=self,
        )
        if dialog.exec() != CartPermissionDialog.DialogCode.Accepted:
            return
        # The job row could have been pruned between the block and this
        # answer, so the lookup is guarded rather than dereferenced.
        job = self._context.repositories.purchases.get(outcome.purchase_job_id)
        rules = (
            self._context.repositories.rules.get(job.rules_id)
            if job is not None
            else None
        )
        if rules is None:
            logger.warning(
                "The rules for a blocked purchase were no longer available",
                extra={"purchase_job_id": outcome.purchase_job_id},
            )
            return
        try:
            self._context.purchase_service.start(
                product_id=outcome.product.id,
                rules=rules,
                mode=PurchaseMode.ASSISTED,
                allow_set_aside=True,
            )
            self._purchase_page.show_progress("Setting your cart aside...")
        except AppError as error:
            self._handle_error(error)

    def _on_purchase_completed(self, order: object) -> None:
        self._purchase_page.update_progress("Order confirmed.")
        product = self._context.repositories.products.get(
            getattr(order, "product_id", None) or 0
        )
        if self._context.notifier is not None:
            from app.notifications import notifier as builders

            self._context.notifier.notify(
                builders.purchase_completed(
                    product.display_title if product else "your item",
                    getattr(order, "total"),
                    getattr(order, "amazon_order_number", None),
                    getattr(order, "purchase_job_id", None) or 0,
                )
            )
        show_and_raise(self)
        number = getattr(order, "amazon_order_number", None)
        view_activity = confirm(
            self,
            title="Order placed",
            message=(
                "Amazon confirmed the order"
                + (f" ({number})." if number else ".")
                + f" Total {getattr(order, 'total').format()}."
            ),
            confirm_text="View activity",
            cancel_text="Close",
        )
        if view_activity:
            self._nav.setCurrentRow(3)
        self._purchase_page.reset()
        self._refresh_all()
        self._activity.refresh()

    def _on_purchase_failed(self, job_id: int, error: AppError) -> None:
        self._purchase_page.update_progress("Stopped.")
        if error.code is ErrorCode.VERIFICATION_REQUIRED:
            self._show_verification_handoff()
            return
        self._handle_error(error)
        self._refresh_all()

    def _on_purchase_uncertain(self, job_id: int) -> None:
        job = self._context.repositories.purchases.get(job_id)
        product = (
            self._context.repositories.products.get(job.product_id) if job else None
        )
        show_and_raise(self)
        dialog = UncertainOrderDialog(
            product_title=product.display_title if product else "your item",
            expected_total=None,
            detail=job.outcome_detail if job else None,
            parent=self,
        )
        dialog.check_orders_requested.connect(
            lambda: self._open_url(orders_url())
        )
        if dialog.exec() != UncertainOrderDialog.DialogCode.Accepted:
            self._refresh_all()
            return
        answer = dialog.order_was_placed
        if answer is None:
            return
        try:
            self._context.purchase_service.resolve_uncertain(
                job_id, order_was_placed=answer, note=dialog.note
            )
        except AppError as error:
            self._handle_error(error)
        self._refresh_all()
        self._activity.refresh()

    def raise_uncertain_purchases(self, job_ids: list[int]) -> None:
        """Bring unresolved purchases to the user's attention at startup."""
        for job_id in job_ids:
            self._on_purchase_uncertain(job_id)

    # ---- watches ---------------------------------------------------------

    def _on_watch_requested(
        self, rules: object, action: object, interval_seconds: int
    ) -> None:
        inspection = self._pending_inspection
        if inspection is None:
            return
        dialog = WatchEditorDialog(
            settings=self._context.settings,
            rules=rules,  # type: ignore[arg-type]
            snapshot=inspection.snapshot,
            product_title=inspection.record.display_title,
            action=action if isinstance(action, WatchAction) else WatchAction.NOTIFY,
            interval_seconds=interval_seconds,
            is_new=True,
            parent=self,
        )
        dialog.auto_buy_consent_requested.connect(
            lambda: dialog.set_auto_buy_acknowledged(self._ask_auto_buy_consent())
        )
        if dialog.exec() != WatchEditorDialog.DialogCode.Accepted:
            return
        self._context.monitor_service.create_watch(
            product_id=inspection.record.id,
            rules=dialog.rules(),
            action=dialog.action(),
            interval_seconds=dialog.interval_seconds(),
            trigger_in_stock=dialog.trigger_in_stock(),
            trigger_target_price=dialog.trigger_target_price(),
            expires_at=dialog.expires_at(),
        )
        self._nav.setCurrentRow(2)
        self._refresh_all()

    def _on_edit_watch(self, watch_job_id: int) -> None:
        view = self._context.repositories.watches.view(watch_job_id)
        if view is None:
            return
        dialog = WatchEditorDialog.for_existing(view, self._context.settings, self)
        dialog.auto_buy_consent_requested.connect(
            lambda: dialog.set_auto_buy_acknowledged(self._ask_auto_buy_consent())
        )
        if dialog.exec() != WatchEditorDialog.DialogCode.Accepted:
            return
        assert view.rules.rules_id is not None
        self._context.repositories.rules.update(view.rules.rules_id, dialog.rules())
        self._context.repositories.watches.update_settings(
            watch_job_id,
            action=dialog.action(),
            interval_seconds=dialog.interval_seconds(),
            trigger_in_stock=dialog.trigger_in_stock(),
            trigger_target_price=dialog.trigger_target_price(),
            expires_at=dialog.expires_at(),
            clear_expiry=dialog.expires_at() is None,
        )
        self._refresh_all()

    def _on_check_now(self, watch_job_id: int) -> None:
        self._context.monitor_service.check_now(watch_job_id or None)

    def _on_remove_watch(self, watch_job_id: int) -> None:
        view = self._context.repositories.watches.view(watch_job_id)
        if view is None:
            return
        if not confirm(
            self,
            title=f"Stop watching {view.product.display_title}?",
            message=(
                "The watch and its price history are removed. Any order "
                "already placed on Amazon is unaffected."
            ),
            confirm_text="Stop watching",
            destructive=True,
        ):
            return
        self._context.monitor_service.remove_watch(watch_job_id)
        self._refresh_all()

    def _on_open_watch_product(self, watch_job_id: int) -> None:
        view = self._context.repositories.watches.view(watch_job_id)
        if view is not None:
            self._open_url(view.product.product_url)

    def _on_view_history(self, watch_job_id: int) -> None:
        view = self._context.repositories.watches.view(watch_job_id)
        if view is None:
            return
        self._nav.setCurrentRow(3)
        self._activity.refresh()

    # ---- account and browser --------------------------------------------

    def _connect_amazon(self) -> None:
        from app.automation.amazon_adapter import ADAPTER

        self._status_label.setText("Opening Amazon so you can sign in...")
        self._context.worker.submit(
            "Connect your Amazon account",
            lambda session: ADAPTER.connect_account(session),
            context={"kind": "account", "operation": "connect"},
        )
        self._connect_account_signals()

    def _test_connection(self) -> None:
        from app.automation.amazon_adapter import ADAPTER

        self._status_label.setText("Checking your Amazon sign-in...")
        self._context.worker.submit(
            "Check your Amazon sign-in",
            lambda session: ADAPTER.check_session(session),
            context={"kind": "account", "operation": "test"},
        )
        self._connect_account_signals()

    def _connect_account_signals(self) -> None:
        """Connect the account result handlers exactly once."""
        if getattr(self, "_account_signals_connected", False):
            return
        self._account_signals_connected = True
        self._context.worker.finished.connect(self._on_account_result)
        self._context.worker.failed.connect(self._on_account_failed)

    def _on_account_result(self, _task: int, result: object, context: object) -> None:
        if not (isinstance(context, dict) and context.get("kind") == "account"):
            return
        signed_in = bool(getattr(result, "signed_in", False))
        name = getattr(result, "account_name", None)
        from app.core.timeutil import now_iso

        self._context.settings.update(
            amazon_connected=signed_in,
            amazon_account_label=name,
            amazon_last_verified_at=now_iso() if signed_in else None,
        )
        self._status_label.setText(
            "Amazon account connected." if signed_in else "Not signed in to Amazon."
        )
        self._refresh_all()

    def _on_account_failed(self, _task: int, error: object, context: object) -> None:
        if not (isinstance(context, dict) and context.get("kind") == "account"):
            return
        if isinstance(error, AppError):
            if error.code is ErrorCode.VERIFICATION_REQUIRED:
                self._show_verification_handoff()
                return
            self._handle_error(error)

    def _clear_session(self) -> None:
        if not confirm(
            self,
            title="Clear the saved Amazon session?",
            message=(
                "This signs the app's own private browser out of Amazon. Your "
                "normal Chrome or Edge is not touched. You will need to sign in "
                "again before the app can check prices."
            ),
            confirm_text="Clear session",
            destructive=True,
        ):
            return
        self._context.worker.submit(
            "Clear the saved Amazon session",
            lambda session: session.manager.clear_profile(),
            context={"kind": "browser", "operation": "clear"},
        )
        self._context.settings.update(
            amazon_connected=False,
            amazon_account_label=None,
            amazon_last_verified_at=None,
        )
        self._refresh_all()

    def _repair_browser(self) -> None:
        self._status_label.setText("Repairing the browser...")
        self._context.worker.submit(
            "Repair the browser",
            lambda session: session.manager.install_chromium(
                on_progress=lambda message: self._context.install_progress.emit(message)
            ),
            context={"kind": "browser", "operation": "repair"},
        )

    def _open_amazon(self) -> None:
        from app.automation.amazon_adapter import ADAPTER

        self._context.worker.submit(
            "Open Amazon",
            lambda session: ADAPTER.open_page(session, home_url()),
            context={"kind": "browser", "operation": "open"},
        )

    def _open_url(self, url: str) -> None:
        """Open a link in the user's normal browser.

        Deliberately not the app's automation browser: a link the user asked
        to read belongs in the browser they already have set up.
        """
        QDesktopServices.openUrl(QUrl(url))

    # ---- verification handoff -------------------------------------------

    def _show_verification_handoff(self) -> None:
        if self._verification_dialog is not None:
            return
        if self._context.notifier is not None:
            from app.notifications import notifier as builders

            self._context.notifier.notify(builders.verification_needed())

        dialog = VerificationDialog(parent=self)
        self._verification_dialog = dialog
        dialog.show_browser_requested.connect(
            lambda: self._context.worker.submit(
                "Show the browser",
                lambda session: session.show_browser(),
                context={"kind": "browser", "operation": "focus"},
            )
        )
        dialog.finished.connect(lambda _result: self._clear_verification_dialog())
        show_and_raise(self)
        dialog.show()

    def _clear_verification_dialog(self) -> None:
        self._verification_dialog = None

    # ---- misc ------------------------------------------------------------

    def _ask_auto_buy_consent(self) -> bool:
        """Show the one-time consent for automatic purchasing."""
        if self._context.settings.current.auto_buy_acknowledged:
            return True
        from app.ui.settings.auto_buy_dialog import AutoBuyConsentDialog

        dialog = AutoBuyConsentDialog(self)
        dialog.exec()
        granted = bool(getattr(dialog, "accepted_consent", False))
        if granted:
            self._context.settings.update(auto_buy_acknowledged=True)
            logger.warning("Automatic purchasing was switched on by the user")
        setter = getattr(self._settings_page, "set_auto_buy_acknowledged", None)
        if setter is not None:
            setter(granted)
        return granted

    def _toggle_monitoring(self) -> None:
        monitor = self._context.monitor_service
        monitor.set_enabled(not monitor.is_monitoring)
        self._refresh_all()

    def _on_monitoring_changed(self, running: bool) -> None:
        self._dashboard.set_monitoring(running)
        self._refresh_tray()
        self._monitor_label.setText(
            "Monitoring on" if running else "Monitoring paused"
        )

    def _on_tray_open(self) -> None:
        self.show()
        show_and_raise(self)

    def _on_tray_pause(self, paused: bool) -> None:
        self._context.monitor_service.set_enabled(not paused)

    def _handle_error(self, error: AppError) -> None:
        action = show_error(error, self)
        self._perform_action(action, None)

    def _perform_action(
        self, action: ActionHint | None, outcome: BlockedOutcome | None
    ) -> None:
        """Carry out whatever next step the user picked in a dialog."""
        if action is None:
            return
        if action in {ActionHint.CONNECT_AMAZON, ActionHint.RECONNECT}:
            self._connect_amazon()
        elif action is ActionHint.OPEN_BROWSER:
            self._open_amazon()
        elif action is ActionHint.SHOW_BROWSER_WINDOW:
            self._show_verification_handoff()
        elif action is ActionHint.CLEAR_SESSION:
            self._clear_session()
        elif action is ActionHint.REPAIR_BROWSER:
            self._repair_browser()
        elif action is ActionHint.OPEN_CART:
            self._open_url(cart_url())
        elif action is ActionHint.OPEN_AMAZON_ORDERS:
            self._open_url(orders_url())
        elif action is ActionHint.OPEN_PRODUCT_PAGE and outcome is not None:
            self._open_url(outcome.product.product_url)
        elif action in {ActionHint.EDIT_RULES, ActionHint.CHECK_PRODUCT_AGAIN}:
            self._nav.setCurrentRow(1)
        elif action is ActionHint.PAUSE_MONITORING:
            self._context.monitor_service.set_enabled(False)
        elif action in {ActionHint.OPEN_LOGS, ActionHint.RUN_DIAGNOSTICS}:
            self._nav.setCurrentRow(4)

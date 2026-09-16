r"""Sending notifications, and deciding when not to.

Why this module is not a thin wrapper around a toast library
------------------------------------------------------------

*Windows silently drops toasts from an application it cannot identify.* A
toast needs a registered Application User Model ID (see
:mod:`app.winint.aumid`). Without one the library call succeeds, no exception
is raised, nothing is logged by Windows, and the notification simply never
appears -- not on screen and not in the Action Center. Nothing in this
process can detect that after the fact. That single failure mode is the
reason for three things here: the AUMID is registered and declared before the
first toast, :meth:`Notifier.self_test` exists so the user can prove the
channel works with their own eyes, and a system tray balloon is used whenever
the toast path raises.

*A watch job checks the same product every few minutes.* A product that sits
below its target price would otherwise notify on every check. Every request
therefore carries a dedupe key and a window, and the delivery log in the
database is the memory -- so the quiet period survives a restart. The one
exception is :attr:`app.config.NotificationKind.always_on`: anything that
reports money being spent, or a purchase waiting on the user, is delivered
every time. A missed "order placed" is worse than a repeated one.

*Toast callbacks do not arrive on the GUI thread.* When the user clicks a
toast button, WinRT invokes the handler on a COM thread pool thread. Touching
a widget from there is undefined behaviour, so the handler marshals the
button's argument string onto this object with a queued
``QMetaObject.invokeMethod`` call and :attr:`Notifier.action_invoked` is
emitted on the GUI thread. The toaster and every toast still awaiting a click
are held on the instance as well: the callback lives on the ``Toast`` object,
so a garbage-collected toast is a click that never arrives.

*Notification text is deliberately incomplete.* A toast appears on the lock
screen and in the Action Center, where anyone near the machine can read it.
Prices are shown, because a price is the news. The delivery address, the
payment method and the order number are not, and belong in the activity feed
inside the application instead.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from PySide6.QtCore import Q_ARG, QMetaObject, QObject, Qt, Signal, Slot

from app.branding import BRAND
from app.config import NotificationKind, SettingsService
from app.core.money import Money
from app.database.repositories.activity import ActivityRepository
from app.diagnostics.logger import get_logger
from app.paths import get_paths
from app.winint.aumid import register_aumid, set_process_aumid

logger = get_logger("notifications")

#: Windows truncates a toast's first line at roughly this width on a standard
#: display, and a truncated title loses the news. Enforced by the tests.
MAX_TITLE_CHARS: Final = 64

#: Two lines of body text is all the Action Center shows before eliding.
MAX_BODY_CHARS: Final = 180

#: A product name is trimmed to leave room for the rest of the sentence, but
#: never below this, or the body stops identifying which product it is about.
MIN_PRODUCT_CHARS: Final = 24

#: Default quiet period after a delivered notification with the same key.
DEFAULT_DEDUPE_SECONDS: Final = 3_600

#: How long a tray balloon stays on screen.
TRAY_SECONDS: Final = 10

#: Values recorded in ``notification_events.delivery_method``.
METHOD_TOAST: Final = "toast"
METHOD_TRAY: Final = "tray"

#: Values recorded in ``notification_events.suppressed_why``. Plain English:
#: they are shown in the activity feed as the reason nothing appeared.
SUPPRESSED_TURNED_OFF: Final = "turned off in settings"
SUPPRESSED_ALREADY_SENT: Final = "already sent recently"
SUPPRESSED_NO_CHANNEL: Final = "no notification channel available"

#: Names for the delivery channels, shown on the Settings screen.
CHANNEL_TOAST: Final = "Windows notifications"
CHANNEL_TRAY: Final = "System tray only"
CHANNEL_NONE: Final = "None"

#: ``kind`` recorded for the Settings screen's test notification. Not a
#: :class:`NotificationKind`: it is not an application event and must never
#: be silenceable or dedupable.
SELF_TEST_KIND: Final = "self_test"

#: Icon Windows draws on the toast, if the build has written one out.
ICON_FILE_NAME: Final = "app.ico"

#: Toasts kept alive while waiting for a click. Far more than can be on
#: screen at once; the bound is only so a long-running process cannot grow
#: this list without limit.
MAX_TOASTS_HELD: Final = 16


@dataclass(frozen=True)
class NotificationRequest:
    """One notification, fully worded and ready to deliver.

    ``dedupe_key`` identifies "the same news about the same thing" and must
    therefore include the relevant job or product, so that two products can
    never silence each other. ``payload`` is the argument string handed back
    through :attr:`Notifier.action_invoked` when the user clicks a button, and
    is how the application knows which job a click refers to.
    """

    kind: NotificationKind
    title: str
    body: str
    dedupe_key: str
    dedupe_seconds: int = DEFAULT_DEDUPE_SECONDS
    actions: tuple[str, ...] = ()
    payload: str | None = None


def _load_windows_toasts() -> Any:
    """Import ``windows_toasts``, raising if it is unusable.

    Imported here rather than at module scope for two reasons: this module has
    to import on a machine with no Windows notification stack at all (the test
    suite runs on one), and the import pulls in the WinRT bindings, which cost
    noticeably more than every other import in the application. A test
    replaces this function so the suite never pops a real toast.
    """
    if sys.platform != "win32":
        raise RuntimeError("Windows notifications are only available on Windows")
    import windows_toasts  # noqa: PLC0415

    return windows_toasts


class Notifier(QObject):
    """The one way the application tells the user something happened."""

    #: Emitted on the GUI thread with the argument string of the toast button
    #: the user clicked, which is the request's ``payload``.
    action_invoked = Signal(str)

    def __init__(
        self,
        settings: SettingsService,
        activity: ActivityRepository,
        tray: object | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._activity = activity
        self._tray = tray

        # Strong references. See the module docstring: the toaster owns the
        # WinRT notifier and each toast owns the click handler, so anything
        # collected early is a click that never arrives.
        self._toaster: Any = None
        self._toasts: list[Any] = []
        self._toasts_lock = threading.Lock()

        self._identity_ready = False
        #: Set once the toast path has failed, so the Settings screen and the
        #: health check stop claiming a channel that does not work.
        self._toast_failed = False

    # ---- sending ---------------------------------------------------------

    def notify(self, request: NotificationRequest) -> bool:
        """Deliver ``request`` if it should be. Returns whether it was shown.

        Every outcome, including every suppression and its reason, is written
        to the notification log, so "why did I not get told?" is answerable
        after the fact.
        """
        if not self._settings.current.notification_allowed(request.kind):
            self._record(request, delivered=False, why=SUPPRESSED_TURNED_OFF)
            return False

        # Purchase-critical news bypasses the quiet period entirely: the user
        # must always learn about money being spent.
        if not request.kind.always_on and self._activity.was_notified_recently(
            request.dedupe_key, within_seconds=request.dedupe_seconds
        ):
            self._record(request, delivered=False, why=SUPPRESSED_ALREADY_SENT)
            return False

        try:
            self._send_toast(
                request.title, request.body, request.actions, request.payload
            )
        except Exception as exc:  # noqa: BLE001 - any failure means "use the tray"
            self._toast_failed = True
            logger.warning(
                "Could not show a Windows notification, falling back to the tray",
                extra={"kind": request.kind.value, "reason": str(exc)},
            )
        else:
            self._record(request, delivered=True, method=METHOD_TOAST)
            return True

        if self._show_tray_message(request.title, request.body):
            self._record(request, delivered=True, method=METHOD_TRAY)
            return True

        logger.warning(
            "Nothing on this computer can show a notification",
            extra={"kind": request.kind.value},
        )
        self._record(request, delivered=False, why=SUPPRESSED_NO_CHANNEL)
        return False

    def self_test(self) -> tuple[bool, str]:
        """Send one real notification and report whether the channel works.

        ``True`` means a Windows notification was accepted. It is deliberately
        not enough to conclude that the user saw it -- Windows can still drop
        a toast for an unidentified application, or hide it under focus assist
        -- so the explanation asks the user to confirm with their own eyes.
        """
        title = f"{BRAND.short_name} test"
        body = "Notifications are working. This is the only message this test sends."

        try:
            self._send_toast(title, body, (), None)
        except Exception as exc:  # noqa: BLE001
            self._toast_failed = True
            logger.info("Notification self test could not send a toast: %s", exc)
        else:
            self._record_raw(
                kind=SELF_TEST_KIND,
                dedupe_key=SELF_TEST_KIND,
                title=title,
                body=body,
                delivered=True,
                method=METHOD_TOAST,
            )
            return True, (
                "A test notification was sent. If nothing appeared, open "
                "Windows Settings and allow notifications for "
                f"{BRAND.display_name}."
            )

        if self._show_tray_message(title, body):
            self._record_raw(
                kind=SELF_TEST_KIND,
                dedupe_key=SELF_TEST_KIND,
                title=title,
                body=body,
                delivered=True,
                method=METHOD_TRAY,
            )
            return False, (
                "Windows notifications are not available, so a message was "
                "shown next to the clock instead. You will still be told what "
                "happens, but only while the app is running."
            )

        self._record_raw(
            kind=SELF_TEST_KIND,
            dedupe_key=SELF_TEST_KIND,
            title=title,
            body=body,
            delivered=False,
            why=SUPPRESSED_NO_CHANNEL,
        )
        return False, (
            "This computer has no way to show a notification right now, so "
            "the app can only report what happened inside its own window."
        )

    def available_channel(self) -> str:
        """How notifications can reach the user, for the Settings screen."""
        if self._toast_channel_usable():
            return CHANNEL_TOAST
        if self._tray_usable():
            return CHANNEL_TRAY
        return CHANNEL_NONE

    # ---- delivery paths --------------------------------------------------

    def _send_toast(
        self,
        title: str,
        body: str,
        actions: tuple[str, ...],
        payload: str | None,
    ) -> None:
        """Show a real Action Center toast. Raises when it cannot be sent."""
        module = _load_windows_toasts()
        self._ensure_identity()
        toaster = self._ensure_toaster(module)

        buttons = [
            module.ToastButton(label, arguments=payload or label) for label in actions
        ]
        toast = module.Toast(
            text_fields=[title, body],
            actions=buttons,
            on_activated=self._on_toast_activated,
            on_dismissed=lambda _event: self._forget_toast(toast),
            on_failed=lambda _event: self._forget_toast(toast),
        )
        self._hold_toast(toast)
        try:
            toaster.show_toast(toast)
        except Exception:
            self._forget_toast(toast)
            raise

    def _show_tray_message(self, title: str, body: str) -> bool:
        """Show a tray balloon. Returns whether there was a tray to use."""
        show = getattr(self._tray, "show_message", None)
        if not callable(show):
            return False
        try:
            show(title, body, TRAY_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not show a tray message: %s", exc)
            return False
        return True

    def _ensure_toaster(self, module: Any) -> Any:
        """The cached toaster. Built on first use and then reused.

        ``InteractableWindowsToaster`` is used even for a toast with no
        buttons so there is one code path to reason about, and because it is
        the variant that attaches the notification to this application's own
        registered identity rather than to whichever interpreter is running.
        """
        if self._toaster is None:
            self._toaster = module.InteractableWindowsToaster(
                BRAND.display_name, BRAND.aumid
            )
        return self._toaster

    def _ensure_identity(self) -> None:
        """Register and declare the AUMID once, before the first toast.

        A failure here is logged but not raised: the toast is still attempted,
        because the registration may already be in place from an earlier run
        or from the installer.
        """
        if self._identity_ready:
            return
        self._identity_ready = True
        if not register_aumid(_icon_path()):
            logger.warning(
                "Could not register the notification identity; Windows may "
                "not show notifications for this app"
            )
        set_process_aumid()

    # ---- toast lifetime and callbacks ------------------------------------

    def _hold_toast(self, toast: Any) -> None:
        with self._toasts_lock:
            self._toasts.append(toast)
            del self._toasts[:-MAX_TOASTS_HELD]

    def _forget_toast(self, toast: Any) -> None:
        """Drop a toast the user has finished with. Runs on a WinRT thread."""
        with self._toasts_lock:
            try:
                self._toasts.remove(toast)
            except ValueError:
                pass

    def _on_toast_activated(self, event: Any) -> None:
        """Handle a toast click. **Runs on a WinRT/COM thread.**

        Nothing Qt-owned may be touched here, so the argument string is queued
        onto this object and re-emitted on the GUI thread.
        """
        arguments = str(getattr(event, "arguments", "") or "")
        QMetaObject.invokeMethod(
            self,
            "_emit_action",
            Qt.ConnectionType.QueuedConnection,
            Q_ARG(str, arguments),
        )

    @Slot(str)
    def _emit_action(self, arguments: str) -> None:
        """Re-emit a toast click on the GUI thread."""
        logger.info("Notification clicked", extra={"payload": arguments})
        self.action_invoked.emit(arguments)

    # ---- channel probing -------------------------------------------------

    def _toast_channel_usable(self) -> bool:
        if self._toast_failed:
            return False
        try:
            _load_windows_toasts()
        except Exception:  # noqa: BLE001
            return False
        return True

    def _tray_usable(self) -> bool:
        return callable(getattr(self._tray, "show_message", None))

    # ---- logging ---------------------------------------------------------

    def _record(
        self,
        request: NotificationRequest,
        *,
        delivered: bool,
        method: str | None = None,
        why: str | None = None,
    ) -> None:
        self._record_raw(
            kind=request.kind.value,
            dedupe_key=request.dedupe_key,
            title=request.title,
            body=request.body,
            delivered=delivered,
            method=method,
            why=why,
        )

    def _record_raw(
        self,
        *,
        kind: str,
        dedupe_key: str,
        title: str,
        body: str,
        delivered: bool,
        method: str | None = None,
        why: str | None = None,
    ) -> None:
        self._activity.record_notification(
            kind=kind,
            dedupe_key=dedupe_key,
            title=title,
            body=body,
            delivered=delivered,
            delivery_method=method,
            suppressed_why=why,
        )


def _icon_path() -> Path | None:
    """The application icon to draw on a toast, when one has been written.

    The icon is generated into the data directory rather than shipped as a
    file, so it may legitimately be absent; Windows draws a blank tile for a
    path it cannot read, which is worse than no path at all.
    """
    candidate = get_paths().config_dir / ICON_FILE_NAME
    return candidate if candidate.is_file() else None


# ---------------------------------------------------------------------------
# Wording
#
# One builder per NotificationKind. Keeping the copy here means it is reviewed
# together, the length limits are enforced in one place, and no call site can
# invent its own wording or leak a detail that does not belong on a lock
# screen.
# ---------------------------------------------------------------------------


def _clamp(text: str, limit: int) -> str:
    """Collapse whitespace and cut ``text`` to ``limit`` characters."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: max(0, limit - 3)].rstrip()}..."


def _compose_body(product_title: str, tail: str) -> str:
    """Build ``"<product> <tail>"`` so that the whole body fits.

    The tail carries the actual news, so it is kept whole and the product
    name absorbs the length limit.
    """
    budget = MAX_BODY_CHARS - len(tail) - 1
    name = _clamp(product_title, max(MIN_PRODUCT_CHARS, budget))
    return _clamp(f"{name} {tail}", MAX_BODY_CHARS)


def target_price_reached(
    product_title: str,
    price: Money,
    target: Money,
    watch_job_id: int,
) -> NotificationRequest:
    """Price fell to or below the user's target.

    The price is part of the dedupe key, so a *further* drop is reported even
    inside the quiet period.
    """
    return NotificationRequest(
        kind=NotificationKind.TARGET_PRICE_REACHED,
        title=_clamp(f"Price dropped to {price.format()}", MAX_TITLE_CHARS),
        body=_compose_body(
            product_title,
            f"is now {price.format()}. Your target was {target.format()}.",
        ),
        dedupe_key=f"target_price_reached:{watch_job_id}:{price.cents}",
        actions=("Open the app",),
        payload=f"watch/{watch_job_id}",
    )


def back_in_stock(product_title: str, watch_job_id: int) -> NotificationRequest:
    """A product the user is watching can be bought again."""
    return NotificationRequest(
        kind=NotificationKind.BACK_IN_STOCK,
        title="Back in stock",
        body=_compose_body(product_title, "is available to buy again."),
        dedupe_key=f"back_in_stock:{watch_job_id}",
        actions=("Open the app",),
        payload=f"watch/{watch_job_id}",
    )


def seller_changed(
    product_title: str,
    expected: str,
    actual: str,
    watch_job_id: int,
) -> NotificationRequest:
    """Someone other than the agreed seller is now offering the product."""
    return NotificationRequest(
        kind=NotificationKind.SELLER_CHANGED,
        title="Seller changed",
        body=_compose_body(
            product_title,
            f"is now sold by {_clamp(actual, 40)} instead of "
            f"{_clamp(expected, 40)}. Nothing was bought.",
        ),
        dedupe_key=f"seller_changed:{watch_job_id}:{actual}",
        actions=("Open the app",),
        payload=f"watch/{watch_job_id}",
    )


def purchase_ready(
    product_title: str,
    total: Money,
    purchase_job_id: int,
) -> NotificationRequest:
    """Everything checked out and the purchase is waiting on the user."""
    return NotificationRequest(
        kind=NotificationKind.PURCHASE_READY,
        title="Waiting for you to confirm",
        body=_compose_body(
            product_title,
            f"is ready to buy for {total.format()}. Open the app to check the "
            "details and confirm.",
        ),
        dedupe_key=f"purchase_ready:{purchase_job_id}",
        actions=("Review and confirm",),
        payload=f"purchase/{purchase_job_id}",
    )


def purchase_completed(
    product_title: str,
    total: Money,
    order_number: str | None,
    purchase_job_id: int,
) -> NotificationRequest:
    """An order was placed.

    ``order_number`` is accepted so callers do not have to remember a second
    rule, but it is deliberately left out of the text: a toast is readable on
    the lock screen, and the order number belongs in the activity feed inside
    the application.
    """
    return NotificationRequest(
        kind=NotificationKind.PURCHASE_COMPLETED,
        title="Order placed",
        body=_compose_body(
            product_title,
            f"was bought for {total.format()}. Open the app to see the order "
            "details.",
        ),
        dedupe_key=f"purchase_completed:{purchase_job_id}",
        actions=("Open the app",),
        payload=f"purchase/{purchase_job_id}",
    )


def purchase_blocked(
    product_title: str,
    reason: str,
    purchase_job_id: int,
) -> NotificationRequest:
    """A purchase was stopped before any order was placed."""
    return NotificationRequest(
        kind=NotificationKind.PURCHASE_BLOCKED,
        title="Purchase stopped",
        body=_compose_body(
            product_title,
            f"was not bought. {_clamp(reason, 90)}",
        ),
        dedupe_key=f"purchase_blocked:{purchase_job_id}",
        actions=("Open the app",),
        payload=f"purchase/{purchase_job_id}",
    )


def login_expired() -> NotificationRequest:
    """The stored Amazon session is no longer signed in."""
    return NotificationRequest(
        kind=NotificationKind.LOGIN_EXPIRED,
        title="Amazon sign-in has expired",
        body=(
            "Nothing can be checked until you sign in to Amazon again. Open "
            "the app to sign in."
        ),
        dedupe_key="login_expired",
        actions=("Sign in again",),
        payload="account/signin",
    )


def verification_needed() -> NotificationRequest:
    """Amazon is asking for a code or a challenge only the user can answer."""
    return NotificationRequest(
        kind=NotificationKind.VERIFICATION_NEEDED,
        title="Amazon needs you to confirm it is you",
        body=(
            "Amazon is asking for a verification step. Open the app and "
            "finish it in the browser window to carry on."
        ),
        dedupe_key="verification_needed",
        actions=("Open the app",),
        payload="account/verify",
    )


def monitoring_error(product_title: str, detail: str) -> NotificationRequest:
    """A product could not be checked and the reason needs the user."""
    return NotificationRequest(
        kind=NotificationKind.MONITORING_ERROR,
        title="Could not check a product",
        body=_compose_body(
            product_title,
            f"could not be checked. {_clamp(detail, 90)}",
        ),
        dedupe_key=f"monitoring_error:{product_title}",
        actions=("Open the app",),
        payload="activity",
    )

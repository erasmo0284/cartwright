"""Render every screen and dialog to PNG, for visual review.

Builds the real application against a throwaway data directory seeded with
believable content, then grabs each page and dialog. Nothing here touches the
user's real data, the network or Amazon: the browser worker thread is never
started.

Usage::

    .venv\\Scripts\\python.exe scripts\\capture_screens.py [output-dir]
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _seed(context: object) -> None:
    """Fill the database with content that exercises every visual state."""
    from app.core.money import Money
    from app.core.timeutil import iso_in
    from app.database.records import (
        ActivityCategory,
        ActivitySeverity,
        WatchAction,
        WatchStatus,
    )
    from app.purchasing.models import (
        Availability,
        ConditionPolicy,
        ItemCondition,
        ProductSnapshot,
        PurchaseRules,
        SellerPolicy,
        VariationSnapshot,
    )

    repositories = context.repositories  # type: ignore[attr-defined]
    products = repositories.products
    rules_repo = repositories.rules
    watches = repositories.watches
    activity = repositories.activity

    def usd(amount: str) -> Money:
        return Money.from_decimal(amount, "USD")

    catalogue = [
        {
            "asin": "B01N5OSTVQ",
            "title": "Klein Tools CL800 Digital Clamp Meter, AC/DC Auto-Ranging",
            "brand": "Klein Tools",
            "price": usd("109.97"),
            "target": usd("100.00"),
            "seller": "Amazon.com",
            "status": WatchStatus.WAITING_FOR_PRICE,
            "action": WatchAction.NOTIFY,
            "available": Availability.IN_STOCK,
        },
        {
            "asin": "B07YFF9KWP",
            "title": "Milwaukee M18 REDLITHIUM XC5.0 Extended Capacity Battery Pack",
            "brand": "Milwaukee",
            "price": usd("89.99"),
            "target": usd("79.99"),
            "seller": "Amazon.com",
            "status": WatchStatus.TARGET_REACHED,
            "action": WatchAction.BUY,
            "available": Availability.IN_STOCK,
        },
        {
            "asin": "B08N5WRWNW",
            "title": "DEWALT 20V MAX Cordless Drill / Driver Kit with Two Batteries",
            "brand": "DEWALT",
            "price": None,
            "target": usd("149.00"),
            "seller": None,
            "status": WatchStatus.NEEDS_VERIFICATION,
            "action": WatchAction.NOTIFY,
            "available": Availability.OUT_OF_STOCK,
        },
        {
            "asin": "B09XS7JWHH",
            "title": "Fluke 117 Electricians True RMS Multimeter",
            "brand": "Fluke",
            "price": usd("204.99"),
            "target": usd("185.00"),
            "seller": "XYZ Marketplace LLC",
            "status": WatchStatus.PAUSED,
            "action": WatchAction.NOTIFY,
            "available": Availability.IN_STOCK,
        },
    ]

    for index, item in enumerate(catalogue):
        snapshot = ProductSnapshot(
            asin=str(item["asin"]),
            title=str(item["title"]),
            brand=str(item["brand"]),
            image_url=None,
            url=f"https://www.amazon.com/dp/{item['asin']}",
            price=item["price"],  # type: ignore[arg-type]
            availability=item["available"],  # type: ignore[arg-type]
            availability_text=(
                "In Stock"
                if item["available"] is Availability.IN_STOCK
                else "Currently unavailable"
            ),
            seller=item["seller"],  # type: ignore[arg-type]
            ships_from=item["seller"],  # type: ignore[arg-type]
            condition=ItemCondition.NEW,
            variation=VariationSnapshot({"Color": "Black"} if index % 2 == 0 else {}),
            max_quantity=30,
            prime_eligible=True,
            delivery_estimate="FREE delivery Thursday, September 24",
        )
        record = products.upsert_from_snapshot(snapshot)

        # A short price history, so the sparkline has something to draw.
        from dataclasses import replace

        if item["price"] is not None:
            base = item["price"].cents  # type: ignore[union-attr]
            for step, delta in enumerate((900, 600, 300, 150, 0, -200, -50)):
                products.record_observation(
                    record.id,
                    replace(snapshot, price=Money(base + delta, "USD")),
                )

        stored_rules = rules_repo.create(
            PurchaseRules(
                expected_asin=str(item["asin"]),
                quantity=1,
                max_item_price=item["target"],  # type: ignore[arg-type]
                max_order_total=(
                    Money(item["target"].cents + 3_000, "USD")  # type: ignore[union-attr]
                    if item["target"]
                    else None
                ),
                seller_policy=SellerPolicy.AMAZON_ONLY,
                condition_policy=ConditionPolicy.NEW_ONLY,
                expected_variation=snapshot.variation,
                expected_seller=item["seller"],  # type: ignore[arg-type]
                brand=str(item["brand"]),
            )
        )
        assert stored_rules.rules_id is not None
        job = watches.create(
            product_id=record.id,
            rules_id=stored_rules.rules_id,
            action=item["action"],  # type: ignore[arg-type]
            interval_seconds=300 * (index + 1),
            expires_at=iso_in(86_400 * 30) if index == 1 else None,
            first_check_immediately=False,
        )
        watches.record_check_success(
            job.id,
            summary=(
                f"{item['price'].format()} - waiting for {item['target'].format()}"
                if item["price"] and item["target"]
                else "Out of stock"
            ),
            status=item["status"],  # type: ignore[arg-type]
        )
        if item["status"] is WatchStatus.PAUSED:
            watches.pause(job.id)

    events = [
        (
            ActivityCategory.PURCHASE,
            ActivitySeverity.SUCCESS,
            "Purchased Klein Tools CL800 Clamp Meter",
            "Order 112-1234567-7654321",
            usd("117.94"),
        ),
        (
            ActivityCategory.WATCH_CHECK,
            ActivitySeverity.INFO,
            "Checked Milwaukee M18 Battery",
            "$89.99 - waiting for $79.99",
            usd("89.99"),
        ),
        (
            ActivityCategory.PURCHASE,
            ActivitySeverity.BLOCKED,
            "Purchase blocked: DEWALT 20V MAX Drill",
            "Seller changed: expected Amazon.com, found XYZ Marketplace LLC",
            None,
        ),
        (
            ActivityCategory.WARNING,
            ActivitySeverity.WARNING,
            "Amazon needs your attention",
            "A security check appeared while checking Fluke 117",
            None,
        ),
        (
            ActivityCategory.ACCOUNT,
            ActivitySeverity.INFO,
            "Amazon account connected",
            "Signed in as John",
            None,
        ),
        (
            ActivityCategory.ERROR,
            ActivitySeverity.ERROR,
            "Check failed: Fluke 117 Multimeter",
            "Amazon took too long to respond. Trying again later.",
            None,
        ),
    ]
    for category, severity, title, detail, amount in events:
        activity.add(
            category=category,
            severity=severity,
            title=title,
            detail=detail,
            amount=amount,
        )

    context.settings.update(  # type: ignore[attr-defined]
        amazon_connected=True,
        amazon_account_label="John",
        amazon_last_verified_at=iso_in(-600),
    )


def _inspection(context: object) -> "object":
    """A checked product, for the New Purchase page with its rules showing.

    Built from the seeded database so the page renders exactly as it does
    after a real "Check product": the summary card, the pre-filled rules and
    the buttons that act on them.
    """
    from app.purchasing.product_service import Inspection, suggest_rules

    snapshot = _review().snapshot
    products = context.repositories.products  # type: ignore[attr-defined]
    record = products.upsert_from_snapshot(snapshot)
    rules = suggest_rules(
        snapshot,
        default_seller_policy=context.settings.current.default_seller_policy,  # type: ignore[attr-defined]
        default_condition_policy=context.settings.current.default_condition_policy,  # type: ignore[attr-defined]
    )
    return Inspection(snapshot=snapshot, record=record, suggested_rules=rules)


def _review() -> "object":
    """A believable purchase review, for the confirmation dialog."""
    from app.core.money import Money
    from app.database.records import ProductRecord
    from app.purchasing.models import (
        Availability,
        CartLine,
        CartStrategy,
        CheckoutSnapshot,
        ConditionPolicy,
        ItemCondition,
        ProductSnapshot,
        PurchaseRules,
        SellerPolicy,
        VariationSnapshot,
    )
    from app.purchasing.purchase_guard import GUARD
    from app.purchasing.purchase_service import PurchaseReview

    def usd(amount: str) -> Money:
        return Money.from_decimal(amount, "USD")

    asin = "B01N5OSTVQ"
    snapshot = ProductSnapshot(
        asin=asin,
        title="Klein Tools CL800 Digital Clamp Meter, AC/DC Auto-Ranging",
        brand="Klein Tools",
        price=usd("109.97"),
        availability=Availability.IN_STOCK,
        availability_text="In Stock",
        seller="Amazon.com",
        ships_from="Amazon.com",
        condition=ItemCondition.NEW,
        variation=VariationSnapshot({"Color": "Black"}),
        max_quantity=30,
        prime_eligible=True,
        delivery_estimate="Thursday, September 24",
    )
    rules = PurchaseRules(
        expected_asin=asin,
        quantity=1,
        max_item_price=usd("120.00"),
        max_order_total=usd("135.00"),
        seller_policy=SellerPolicy.AMAZON_ONLY,
        condition_policy=ConditionPolicy.NEW_ONLY,
        expected_variation=VariationSnapshot({"Color": "Black"}),
        expected_seller="Amazon.com",
        expected_address_label="John D., Raleigh, NC 27601",
        expected_payment_label="Visa ending in 1234",
        brand="Klein Tools",
    )
    checkout = CheckoutSnapshot(
        lines=(
            CartLine(
                asin=asin,
                title=snapshot.title,
                quantity=1,
                unit_price=usd("109.97"),
                line_price=usd("109.97"),
            ),
        ),
        item_subtotal=usd("109.97"),
        shipping=usd("0.00"),
        tax=usd("7.97"),
        order_total=usd("117.94"),
        address_label="John D., Raleigh, NC 27601",
        payment_label="Visa ending in 1234",
        place_order_control_found=True,
    )
    product = ProductRecord(
        id=1,
        asin=asin,
        marketplace="www.amazon.com",
        title=snapshot.title,
        brand="Klein Tools",
        image_url=None,
        canonical_url=f"https://www.amazon.com/dp/{asin}",
    )
    return PurchaseReview(
        purchase_job_id=1,
        product=product,
        snapshot=snapshot,
        checkout=checkout,
        rules=rules,
        report=GUARD.check_final(rules, snapshot, checkout),
        strategy=CartStrategy.BUY_NOW,
    )


def _blocked_outcome() -> "object":
    """A blocked purchase, for the blocked dialog."""
    from dataclasses import replace

    from app.core.errors import AppError, ErrorCode
    from app.purchasing.purchase_guard import GUARD
    from app.purchasing.purchase_service import BlockedOutcome

    review = _review()
    bad_snapshot = replace(review.snapshot, seller="XYZ Marketplace LLC")  # type: ignore[attr-defined]
    report = GUARD.check_final(
        review.rules, bad_snapshot, review.checkout  # type: ignore[attr-defined]
    )
    return BlockedOutcome(
        purchase_job_id=1,
        product=review.product,  # type: ignore[attr-defined]
        report=report,
        error=AppError(
            ErrorCode.SELLER_CHANGED,
            context={"expected": "Amazon.com", "actual": "XYZ Marketplace LLC"},
        ),
    )


def _test_result() -> "object":
    from app.purchasing.purchase_service import TestRunResult

    review = _review()
    return TestRunResult(
        purchase_job_id=1,
        product=review.product,  # type: ignore[attr-defined]
        snapshot=review.snapshot,  # type: ignore[attr-defined]
        checkout=review.checkout,  # type: ignore[attr-defined]
        report=review.report,  # type: ignore[attr-defined]
        strategy=review.strategy,  # type: ignore[attr-defined]
        order_button_located=True,
    )


def _grab(widget: object, destination: Path) -> None:
    """Capture what is actually on screen for ``widget``.

    ``QWidget.grab()`` re-renders the widget tree directly, which ignores a
    scroll area's viewport clipping: children scrolled out of view are painted
    anyway and appear to overlap the visible rows. Grabbing through the screen
    captures the composited result instead, so what is reviewed is what a user
    would see.
    """
    from PySide6.QtWidgets import QApplication

    screen = QApplication.primaryScreen()
    pixmap = None
    if screen is not None:
        try:
            pixmap = screen.grabWindow(widget.winId())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pixmap = None
    if pixmap is None or pixmap.isNull():
        pixmap = widget.grab()  # type: ignore[attr-defined]
    pixmap.save(str(destination))


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    # Not under build/, which ``build.py --clean`` deletes: these are review
    # evidence and should outlive a rebuild.
    output = Path(arguments[0]) if arguments else PROJECT_ROOT / "docs" / "screens"
    output.mkdir(parents=True, exist_ok=True)

    data_dir = Path(tempfile.mkdtemp(prefix="apb-screens-"))
    os.environ["APB_DATA_DIR"] = str(data_dir)

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    application = QApplication(sys.argv[:1])

    from app.config import ThemePreference
    from app.paths import AppPaths, reset_paths_cache
    from app.ui.app_context import AppContext
    from app.ui.main_window import MainWindow
    from app.ui.theme import ThemeManager, ThemeMode

    reset_paths_cache()
    paths = AppPaths(root=data_dir).ensure()
    context = AppContext(paths=paths)
    _seed(context)

    theme = ThemeManager(application)

    captured: list[Path] = []

    for mode_name, mode in (("light", ThemeMode.LIGHT), ("dark", ThemeMode.DARK)):
        theme.set_mode(mode)
        application.processEvents()

        window = MainWindow(context, theme)
        window.resize(1180, 800)
        window.show()
        for _ in range(12):
            application.processEvents()

        pages = ("dashboard", "purchase", "watchlist", "activity", "settings")
        for index, page in enumerate(pages):
            window._nav.setCurrentRow(index)  # noqa: SLF001 - review tooling
            for _ in range(10):
                application.processEvents()
            destination = output / f"{mode_name}-{index + 1}-{page}.png"
            _grab(window, destination)
            captured.append(destination)

        # The rules editor, which is where a person decides what "buy it"
        # means and is therefore the screen most worth looking at.
        window._nav.setCurrentRow(1)  # noqa: SLF001
        try:
            window._purchase_page.show_inspection(_inspection(context))  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 - a capture must not fail the run
            print(f"  (could not render the rules editor: {exc})")
        else:
            for _ in range(10):
                application.processEvents()
            destination = output / f"{mode_name}-2b-purchase-rules.png"
            _grab(window, destination)
            captured.append(destination)

        # The empty states, which a seeded database hides.
        window._nav.setCurrentRow(1)  # noqa: SLF001
        window._purchase_page.reset()  # noqa: SLF001
        for _ in range(8):
            application.processEvents()
        destination = output / f"{mode_name}-6-purchase-empty.png"
        _grab(window, destination)
        captured.append(destination)

        window.close()

        # Dialogs, captured on their own so their layout is judged at its own
        # natural size rather than inside the window.
        from app.ui.dialogs import (
            AboutDialog,
            BlockedDialog,
            ConfirmPurchaseDialog,
            TestResultDialog,
            UncertainOrderDialog,
            VerificationDialog,
        )
        from app.ui.dialogs.cart_permission_dialog import CartPermissionDialog
        from app.ui.settings.auto_buy_dialog import AutoBuyConsentDialog

        from app.purchasing.models import CartLine

        dialogs: list[tuple[str, object]] = [
            ("confirm-purchase", ConfirmPurchaseDialog(_review())),
            ("blocked", BlockedDialog(_blocked_outcome())),
            ("test-result", TestResultDialog(_test_result())),
            (
                "uncertain-order",
                UncertainOrderDialog(
                    product_title="Klein Tools CL800 Clamp Meter",
                    expected_total=__import__(
                        "app.core.money", fromlist=["Money"]
                    ).Money.from_decimal("117.94"),
                ),
            ),
            ("verification", VerificationDialog()),
            (
                "cart-permission",
                CartPermissionDialog(
                    product_title="Klein Tools CL800 Clamp Meter",
                    foreign_lines=(
                        CartLine(asin="B0DOGFOOD01", title="Large bag of dog food", quantity=2),
                        CartLine(asin="B0KETTLE001", title="Electric kettle", quantity=1),
                    ),
                ),
            ),
            ("auto-buy-consent", AutoBuyConsentDialog()),
            ("about", AboutDialog()),
        ]

        for name, dialog in dialogs:
            dialog.show()  # type: ignore[attr-defined]
            for _ in range(10):
                application.processEvents()
            destination = output / f"{mode_name}-dialog-{name}.png"
            _grab(dialog, destination)
            captured.append(destination)
            dialog.close()  # type: ignore[attr-defined]
            application.processEvents()

    context.shutdown()
    shutil.rmtree(data_dir, ignore_errors=True)

    print(f"Captured {len(captured)} screens into {output}")  # noqa: T201
    for path in captured:
        size = path.stat().st_size if path.exists() else 0
        marker = "" if size > 2_000 else "   <-- suspiciously small"
        print(f"  {path.name} ({size / 1024:.0f} KiB){marker}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

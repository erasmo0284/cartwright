"""Tests for what the purchase screens actually put on screen.

Both of these cover a defect that every other test in the suite was blind to,
because the widgets were all constructed correctly and merely ended up out of
sight:

* the confirmation dialog opened at its minimum height, which left "Where it
  goes and how it is paid" -- the delivery address and the payment method --
  below the fold on the one screen where real money is about to be spent;
* the New Purchase page kept its action card inside the scroll area, so after
  a product was checked the buttons that act on it were off-screen at the
  window size the application actually opens at.

Neither is visible to a test that only asks whether a widget exists, so these
assert on geometry: where a widget lands relative to the viewport that clips
it. The screen the tests run on is the offscreen platform's fixed one, which
is shorter than a real display, so the dialog's screen-height cap is lifted
where the content height is the thing under test and asserted on its own
instead.

Everything runs under the offscreen platform, set before the first
:class:`QApplication` exists because Qt reads it exactly once at that point.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QApplication,
    QLabel,
    QScrollArea,
    QWidget,
)

from app.core.money import Money  # noqa: E402
from app.database.database import Database  # noqa: E402
from app.database.records import ProductRecord  # noqa: E402
from app.paths import AppPaths  # noqa: E402
from app.purchasing.models import (  # noqa: E402
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
from app.purchasing.product_service import Inspection, suggest_rules  # noqa: E402
from app.purchasing.purchase_guard import GUARD  # noqa: E402
from app.purchasing.purchase_service import PurchaseReview  # noqa: E402
from app.ui.app_context import AppContext  # noqa: E402
from app.ui.components import Card  # noqa: E402
from app.ui.dialogs.confirm_purchase_dialog import ConfirmPurchaseDialog  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.purchase.new_purchase_page import NewPurchasePage  # noqa: E402
from app.ui.theme import ThemeManager  # noqa: E402

ASIN = "B01N5OSTVQ"

#: The card that was below the fold, and the two facts inside it that a person
#: has to be able to check before approving an order.
DELIVERY_TITLE = "Where it goes and how it is paid"
ADDRESS = "John D., Raleigh, NC 27601"
PAYMENT = "Visa ending in 1234"

#: The window size the application opens at, and the one the screenshots are
#: reviewed at. The action buttons have to be reachable here without scrolling.
WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 800

#: A height budget far larger than any real screen, for the tests that are
#: about the content's own height rather than about the cap.
TALL_SCREEN = 10_000


# ---------------------------------------------------------------------------
# Fixtures and content
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qt_app() -> Iterator[QApplication]:
    """One offscreen application for the module; Qt permits only one.

    It is never destroyed: tearing it down while other Qt objects are alive
    crashes the interpreter.
    """
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    yield app


@pytest.fixture
def context(
    qt_app: QApplication, app_paths: AppPaths, database: Database
) -> Iterator[AppContext]:
    """A real context, never started: ``start()`` would launch Chromium."""
    built = AppContext(paths=app_paths)
    try:
        yield built
    finally:
        built.shutdown()


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


def snapshot() -> ProductSnapshot:
    return ProductSnapshot(
        asin=ASIN,
        title="Klein Tools CL800 Digital Clamp Meter, AC/DC Auto-Ranging",
        brand="Klein Tools",
        url=f"https://www.amazon.com/dp/{ASIN}",
        price=usd("109.97"),
        availability=Availability.IN_STOCK,
        availability_text="In Stock",
        seller="Amazon.com",
        ships_from="Amazon.com",
        condition=ItemCondition.NEW,
        variation=VariationSnapshot({"Color": "Black"}),
        variation_picker_present=True,
        max_quantity=30,
        buy_now_available=True,
        add_to_cart_available=True,
        prime_eligible=True,
        delivery_estimate="Thursday, September 24",
    )


def review() -> PurchaseReview:
    """A believable purchase review, as the confirmation dialog receives it."""
    product = snapshot()
    rules = PurchaseRules(
        expected_asin=ASIN,
        quantity=1,
        max_item_price=usd("120.00"),
        max_order_total=usd("135.00"),
        seller_policy=SellerPolicy.AMAZON_ONLY,
        condition_policy=ConditionPolicy.NEW_ONLY,
        expected_variation=VariationSnapshot({"Color": "Black"}),
        expected_seller="Amazon.com",
        expected_address_label=ADDRESS,
        expected_payment_label=PAYMENT,
        brand="Klein Tools",
    )
    checkout = CheckoutSnapshot(
        lines=(
            CartLine(
                asin=ASIN,
                title=product.title,
                quantity=1,
                unit_price=usd("109.97"),
                line_price=usd("109.97"),
            ),
        ),
        item_subtotal=usd("109.97"),
        shipping=usd("0.00"),
        tax=usd("7.97"),
        order_total=usd("117.94"),
        address_label=ADDRESS,
        payment_label=PAYMENT,
        place_order_control_found=True,
    )
    return PurchaseReview(
        purchase_job_id=1,
        product=ProductRecord(
            id=1,
            asin=ASIN,
            marketplace="www.amazon.com",
            title=product.title,
            brand="Klein Tools",
            image_url=None,
            canonical_url=f"https://www.amazon.com/dp/{ASIN}",
        ),
        snapshot=product,
        checkout=checkout,
        rules=rules,
        report=GUARD.check_final(rules, product, checkout),
        strategy=CartStrategy.BUY_NOW,
    )


def inspection(context: AppContext) -> Inspection:
    """A checked product, exactly as a successful "Check product" produces."""
    product = snapshot()
    record = context.repositories.products.upsert_from_snapshot(product)
    settings = context.settings.current
    return Inspection(
        snapshot=product,
        record=record,
        suggested_rules=suggest_rules(
            product,
            default_seller_policy=settings.default_seller_policy,
            default_condition_policy=settings.default_condition_policy,
        ),
    )


def titled_card(parent: QWidget, title: str) -> Card:
    """The card carrying ``title``; :meth:`Card.set_title` records it as the
    accessible name, so this finds it the way a screen reader would."""
    for card in parent.findChildren(Card):
        if card.accessibleName() == title:
            return card
    raise AssertionError(f"no card titled {title!r}")


def action_buttons(page: NewPurchasePage) -> dict[str, QAbstractButton]:
    """The buttons the page exists for, by the name used in these assertions.

    They are reached through the page's own attributes rather than by their
    labels, because the buy button is relabelled "Run a test" in test mode and
    takes its accessible name from its text.
    """
    return {
        "the buy button": page._prepare_button,  # noqa: SLF001 - the real widget
        "the watch button": page._watch_button,  # noqa: SLF001 - the real widget
    }


def settle(app: QApplication, rounds: int = 10) -> None:
    """Let Qt finish laying out before anything is measured."""
    for _ in range(rounds):
        app.processEvents()


def bottom_in(widget: QWidget, ancestor: QWidget) -> int:
    """``widget``'s bottom edge, in ``ancestor``'s coordinates."""
    return widget.mapTo(ancestor, QPoint(0, widget.height())).y()


def top_in(widget: QWidget, ancestor: QWidget) -> int:
    """``widget``'s top edge, in ``ancestor``'s coordinates."""
    return widget.mapTo(ancestor, QPoint(0, 0)).y()


# ---------------------------------------------------------------------------
# The confirmation dialog
# ---------------------------------------------------------------------------


class TestConfirmPurchaseDialogFits:
    def test_the_address_and_the_payment_method_are_above_the_fold(
        self, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The delivery card must be inside the viewport when the dialog opens.

        This is the defect this test was written for: the dialog opened at its
        minimum height, and the one card a person has to check before spending
        money was underneath the scrollbar.
        """
        monkeypatch.setattr(
            ConfirmPurchaseDialog, "_height_budget", lambda self: TALL_SCREEN
        )
        dialog = ConfirmPurchaseDialog(review())
        dialog.show()
        settle(qt_app)

        scroll = dialog.findChild(QScrollArea)
        assert scroll is not None, "the scroll area must be kept for small screens"
        viewport = scroll.viewport()
        card = titled_card(dialog, DELIVERY_TITLE)

        assert top_in(viewport, dialog) >= 0
        assert bottom_in(card, dialog) <= bottom_in(viewport, dialog), (
            "the delivery card is cut off at the dialog's own size"
        )

        texts = [label.text() for label in card.findChildren(QLabel)]
        assert ADDRESS in texts
        assert PAYMENT in texts

        dialog.close()

    def test_the_dialog_opens_at_the_height_its_content_needs(
        self, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The size hint follows the content, not the minimum size."""
        monkeypatch.setattr(
            ConfirmPurchaseDialog, "_height_budget", lambda self: TALL_SCREEN
        )
        dialog = ConfirmPurchaseDialog(review())

        hint = dialog.sizeHint()
        assert hint.height() > dialog.minimumHeight(), (
            "a size hint at the minimum is what hid the delivery card"
        )
        assert dialog.height() == hint.height()

        dialog.close()

    def test_a_screen_too_short_for_the_content_scrolls_instead(
        self, qt_app: QApplication, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Growing past the bottom of the screen would be worse than scrolling.

        At 150% scaling there is genuinely not enough room, so the dialog has
        to stay within the screen and let the body scroll.
        """
        budget = 520
        monkeypatch.setattr(
            ConfirmPurchaseDialog, "_height_budget", lambda self: budget
        )
        dialog = ConfirmPurchaseDialog(review())

        assert dialog.sizeHint().height() <= budget
        scroll = dialog.findChild(QScrollArea)
        assert scroll is not None
        assert (
            scroll.verticalScrollBarPolicy()
            is not Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        dialog.close()


# ---------------------------------------------------------------------------
# The New Purchase page
# ---------------------------------------------------------------------------


class TestNewPurchaseActionsStayOnScreen:
    def test_the_action_buttons_are_not_inside_the_scrolled_content(
        self, context: AppContext
    ) -> None:
        """Pinning them is what keeps them on screen whatever the rules cost.

        If they go back inside the scroll area, the page's own height stops
        bounding them and the window size decides whether they can be seen.
        """
        page = NewPurchasePage(context.settings)
        scroll = page.findChild(QScrollArea)
        assert scroll is not None

        for name, button in action_buttons(page).items():
            assert not scroll.isAncestorOf(button), f"{name} is inside the scroll area"

    def test_the_actions_are_visible_with_a_product_checked(
        self, context: AppContext, qt_app: QApplication
    ) -> None:
        """The screenshot's own window size, with a product on screen.

        The buttons, the mode note and the rule warning all sit in the same
        card, so asserting on the card covers the note that qualifies them as
        well as the buttons themselves.
        """
        window = MainWindow(context, ThemeManager(qt_app))
        window.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        window.show()
        settle(qt_app)

        page = window._purchase_page  # noqa: SLF001 - asserting on the real page
        page.show_inspection(inspection(context))
        settle(qt_app)

        card = titled_card(page, "What should happen")
        assert top_in(card, window) >= 0
        assert bottom_in(card, window) <= window.height(), (
            "the action card runs off the bottom of the window"
        )
        for name, button in action_buttons(page).items():
            assert not button.isHidden(), f"{name} is hidden"
            assert top_in(button, window) >= 0, f"{name} is above the window"
            assert bottom_in(button, window) <= window.height(), (
                f"{name} is below the bottom of the window"
            )

        window.close()

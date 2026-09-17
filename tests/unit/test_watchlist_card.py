"""Tests for the watch card's picture and its column alignment.

Both come from reviewing screenshots of the real screens:

* every card showed a 72x72 box containing the words "No image", because
  nothing in the application ever loaded a product picture;
* each card measured its own label and value columns from its own text, so
  "Now", "Target" and "Availability" sat in a different place on every card
  and the list did not read as a list.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.automation.image_capture import thumbnail_path  # noqa: E402
from app.core.money import Money  # noqa: E402
from app.core.timeutil import utcnow  # noqa: E402
from app.database.records import (  # noqa: E402
    PriceObservation,
    ProductRecord,
    WatchAction,
    WatchJobRecord,
    WatchJobView,
    WatchStatus,
)
from app.paths import AppPaths  # noqa: E402
from app.purchasing.models import (  # noqa: E402
    Availability,
    ConditionPolicy,
    ItemCondition,
    PurchaseRules,
    SellerPolicy,
)
from app.ui import images  # noqa: E402
from app.ui.watchlist.watchlist_page import (  # noqa: E402
    LABEL_COLUMN_WIDTH,
    NO_IMAGE_TEXT,
    _WatchCard,
)

ASIN = "B01N5OSTVQ"


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _forget_decoded() -> None:
    images.clear_cache()


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


def view(
    *, asin: str = ASIN, title: str = "Klein Tools CL800 Clamp Meter"
) -> WatchJobView:
    """One watched product, as the page receives it from the repositories."""
    now = utcnow()
    return WatchJobView(
        job=WatchJobRecord(
            id=1,
            product_id=1,
            rules_id=1,
            status=WatchStatus.WATCHING,
            action=WatchAction.NOTIFY,
            trigger_in_stock=False,
            trigger_target_price=True,
            interval_seconds=600,
            expires_at=None,
            next_check_at=now + timedelta(minutes=4),
            last_checked_at=now,
            last_check_summary="Checked just now",
            last_error_code=None,
            consecutive_failures=0,
            paused_reason=None,
            checks_performed=3,
            created_at=now,
            updated_at=now,
        ),
        product=ProductRecord(
            id=1,
            asin=asin,
            marketplace="www.amazon.com",
            title=title,
            brand="Klein Tools",
            image_url=None,
            canonical_url=None,
            created_at=now,
            updated_at=now,
        ),
        rules=PurchaseRules(
            expected_asin=asin,
            quantity=1,
            max_item_price=usd("100.00"),
            max_order_total=usd("120.00"),
            seller_policy=SellerPolicy.AMAZON_ONLY,
            condition_policy=ConditionPolicy.NEW_ONLY,
        ),
        latest=PriceObservation(
            id=1,
            product_id=1,
            observed_at=now,
            price=usd("109.47"),
            in_stock=True,
            availability=Availability.IN_STOCK,
            availability_text="In Stock",
            seller="Amazon.com",
            ships_from="Amazon.com",
            condition=ItemCondition.NEW,
            variation_fingerprint=None,
        ),
    )


def stored_picture(paths: AppPaths, asin: str = ASIN) -> None:
    """Put a real PNG where the last check would have left one."""
    paths.images_dir.mkdir(parents=True, exist_ok=True)
    pixmap = QPixmap(64, 48)
    pixmap.fill()
    assert pixmap.save(str(thumbnail_path(paths, asin, "www.amazon.com")), "PNG")


class TestTheCardsPicture:
    def test_the_stored_picture_is_shown(self, app_paths: AppPaths, qt_app) -> None:
        stored_picture(app_paths)
        card = _WatchCard(view())
        card.update_view(view(), ())

        shown = card._image.pixmap()  # noqa: SLF001 - asserting on the rendering
        assert not shown.isNull(), "the card should show the product"
        assert card._image.text() == "", "and not the placeholder words"  # noqa: SLF001

    def test_the_picture_keeps_its_shape(self, app_paths: AppPaths, qt_app) -> None:
        """A portrait photograph must not be stretched into the box."""
        stored_picture(app_paths)
        card = _WatchCard(view())
        card.update_view(view(), ())
        shown = card._image.pixmap()  # noqa: SLF001
        assert shown.width() >= shown.height(), "a 64x48 source stays landscape"

    def test_no_picture_leaves_the_placeholder(
        self, app_paths: AppPaths, qt_app
    ) -> None:
        """A product that has never been checked has nothing to show."""
        card = _WatchCard(view())
        card.update_view(view(), ())
        assert card._image.text() == NO_IMAGE_TEXT  # noqa: SLF001

    def test_a_picture_appearing_later_replaces_the_placeholder(
        self, app_paths: AppPaths, qt_app
    ) -> None:
        """The card is reused across refreshes, so it must switch cleanly."""
        card = _WatchCard(view())
        card.update_view(view(), ())
        assert card._image.text() == NO_IMAGE_TEXT  # noqa: SLF001

        stored_picture(app_paths)
        images.clear_cache()
        card.update_view(view(), ())
        assert card._image.text() == ""  # noqa: SLF001
        assert not card._image.pixmap().isNull()  # noqa: SLF001


class TestColumnAlignment:
    def test_two_cards_put_their_labels_in_the_same_place(
        self, app_paths: AppPaths, qt_app
    ) -> None:
        """Different text, same columns: that is what makes it read as a list."""
        short = _WatchCard(view(title="Short"))
        long_view = view(
            title=(
                "A product with a very long name indeed, of the sort Amazon "
                "writes when it is describing every feature in the title"
            )
        )
        long = _WatchCard(long_view)

        for column in range(6):
            assert short._grid.columnMinimumWidth(  # noqa: SLF001
                column
            ) == long._grid.columnMinimumWidth(column)  # noqa: SLF001

    def test_the_label_columns_are_wide_enough_for_the_longest_label(
        self, app_paths: AppPaths, qt_app
    ) -> None:
        card = _WatchCard(view())
        card.update_view(view(), ())
        for pair in range(3):
            assert (
                card._grid.columnMinimumWidth(pair * 2)  # noqa: SLF001
                == LABEL_COLUMN_WIDTH
            )

    def test_the_value_columns_share_the_space(
        self, app_paths: AppPaths, qt_app
    ) -> None:
        """Equal stretch, so the three pairs divide the card evenly."""
        card = _WatchCard(view())
        card.update_view(view(), ())
        stretches = [
            card._grid.columnStretch(pair * 2 + 1)  # noqa: SLF001
            for pair in range(3)
        ]
        assert stretches == [1, 1, 1]
        labels = [card._grid.columnStretch(pair * 2) for pair in range(3)]  # noqa: SLF001
        assert labels == [0, 0, 0]

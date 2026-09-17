"""Tests for the rules editor's warnings.

The editor is where a person decides what "buy it" means, so the warnings it
shows are part of the safety story rather than decoration. Each one exists
because of a specific way a rule set can be wrong in a way the guard would
enforce silently: limits that can never be satisfied, a seller rule with
nothing in it, and -- the reason this file was added -- a product whose
version could not be read, where the rule ends up with no version expectation
at all and the user should be told rather than left to assume.

Runs under the offscreen platform. No window is shown.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.money import Money  # noqa: E402
from app.purchasing.models import (  # noqa: E402
    Availability,
    ConditionPolicy,
    ItemCondition,
    ProductSnapshot,
    PurchaseRules,
    SellerPolicy,
    VariationSnapshot,
)
from app.ui.purchase.rules_editor import RulesEditor  # noqa: E402

ASIN = "B01N5OSTVQ"


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


@pytest.fixture(scope="session")
def qt_app() -> Iterator[QApplication]:
    app = QApplication.instance() or QApplication([])
    yield app  # type: ignore[misc]


@pytest.fixture
def snapshot() -> ProductSnapshot:
    return ProductSnapshot(
        asin=ASIN,
        title="Klein Tools CL800 Clamp Meter",
        brand="Klein Tools",
        price=usd("109.97"),
        availability=Availability.IN_STOCK,
        seller="Amazon.com",
        ships_from="Amazon.com",
        condition=ItemCondition.NEW,
        variation=VariationSnapshot({"Color": "Black"}),
        variation_picker_present=True,
        max_quantity=30,
        buy_now_available=True,
        add_to_cart_available=True,
    )


@pytest.fixture
def rules() -> PurchaseRules:
    return PurchaseRules(
        expected_asin=ASIN,
        quantity=1,
        max_item_price=usd("120.00"),
        max_order_total=usd("135.00"),
        seller_policy=SellerPolicy.AMAZON_ONLY,
        condition_policy=ConditionPolicy.NEW_ONLY,
        expected_variation=VariationSnapshot({"Color": "Black"}),
        brand="Klein Tools",
    )


@pytest.fixture
def editor(qt_app: QApplication) -> RulesEditor:
    return RulesEditor()


class TestUnreadableVariationWarning:
    def test_a_readable_version_produces_no_warning(
        self, editor: RulesEditor, rules: PurchaseRules, snapshot: ProductSnapshot
    ) -> None:
        editor.set_rules(rules, snapshot=snapshot)
        assert editor.current_warning() == ""

    def test_a_product_with_no_versions_produces_no_warning(
        self, editor: RulesEditor, rules: PurchaseRules, snapshot: ProductSnapshot
    ) -> None:
        """Most products have no picker at all; that is not worth a warning."""
        plain = replace(
            snapshot,
            variation=VariationSnapshot(),
            variation_picker_present=False,
        )
        editor.set_rules(
            replace(rules, expected_variation=VariationSnapshot()), snapshot=plain
        )
        assert editor.current_warning() == ""

    def test_an_unreadable_version_is_said_out_loud(
        self, editor: RulesEditor, rules: PurchaseRules, snapshot: ProductSnapshot
    ) -> None:
        """The rule pins the item code and nothing else, so say exactly that.

        Without this the user sees a normal-looking rule and assumes the
        colour on screen was recorded. It was not: the guard can only compare
        what was stored, and nothing was.
        """
        unreadable = replace(
            snapshot,
            variation=VariationSnapshot(),
            variation_picker_present=True,
        )
        assert unreadable.variation_unreadable is True

        editor.set_rules(
            replace(rules, expected_variation=VariationSnapshot()),
            snapshot=unreadable,
        )
        warning = editor.current_warning()

        assert warning, "an unreadable version must not pass in silence"
        assert "several versions" in warning
        assert ASIN in warning, "the user is told what the purchase is pinned to"
        assert "_" not in warning, "no internal identifiers in user-facing text"

    def test_the_warning_reaches_the_badge_and_the_signal(
        self, editor: RulesEditor, rules: PurchaseRules, snapshot: ProductSnapshot
    ) -> None:
        """Shown on screen, not merely available to a caller."""
        seen: list[str] = []
        editor.warning_changed.connect(seen.append)
        editor.set_rules(
            replace(rules, expected_variation=VariationSnapshot()),
            snapshot=replace(
                snapshot,
                variation=VariationSnapshot(),
                variation_picker_present=True,
            ),
        )
        assert seen and "several versions" in seen[-1]
        # isVisibleTo, not isVisible: the editor itself is never shown in
        # a test, and a child of a hidden parent is never "visible".
        assert editor._warning.isVisibleTo(editor) is True  # noqa: SLF001
        assert "several versions" in editor._warning.text()  # noqa: SLF001

    def test_a_rule_that_can_never_succeed_is_reported_first(
        self, editor: RulesEditor, rules: PurchaseRules, snapshot: ProductSnapshot
    ) -> None:
        """Ordering matters: an impossible limit is the more urgent problem."""
        editor.set_rules(
            replace(
                rules,
                expected_variation=VariationSnapshot(),
                quantity=2,
                max_item_price=usd("100.00"),
                max_order_total=usd("120.00"),
            ),
            snapshot=replace(
                snapshot,
                variation=VariationSnapshot(),
                variation_picker_present=True,
            ),
        )
        warning = editor.current_warning()
        assert "no order could ever go through" in warning

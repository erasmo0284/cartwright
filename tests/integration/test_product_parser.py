"""Parser and classifier tests against real Chromium and fixture pages.

These are the tests that catch a parsing bug that a unit test with hand-fed
strings would miss: the fixtures contain Amazon's real markup shapes, so the
selectors, the DOM traversal and the money parsing are all exercised
together.
"""

from __future__ import annotations

import pytest

from app.automation.login_detector import DETECTOR, PageKind
from app.automation.product_parser import PARSER
from app.core.errors import ErrorCode
from app.core.money import Money
from app.purchasing.models import Availability, ItemCondition
from tests.fixtures import amazon_pages as pages

pytestmark = pytest.mark.integration

ASIN = pages.DEFAULT_ASIN
PRODUCT_URL = f"https://www.amazon.com/dp/{ASIN}"


def usd(amount: str) -> Money:
    return Money.from_decimal(amount, "USD")


class TestProductPage:
    def test_reads_every_field(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_page())
        snapshot = PARSER.parse(reader, expected_asin=ASIN)

        assert snapshot.asin == ASIN
        assert snapshot.title == pages.DEFAULT_TITLE
        assert snapshot.brand == "Klein Tools"
        assert snapshot.price == usd("109.97")
        assert snapshot.list_price == usd("129.99")
        assert snapshot.availability is Availability.IN_STOCK
        assert snapshot.seller == "Amazon.com"
        assert snapshot.ships_from == "Amazon.com"
        assert snapshot.condition is ItemCondition.NEW
        assert snapshot.variation.dimensions == {"Color": "Black"}
        assert snapshot.max_quantity == 30
        assert snapshot.prime_eligible is True
        assert snapshot.buy_now_available is True
        assert snapshot.add_to_cart_available is True
        assert snapshot.subscription_preselected is False
        assert snapshot.image_url is not None
        assert "September 24" in (snapshot.delivery_estimate or "")

    def test_price_is_not_read_from_the_list_price(self, load) -> None:
        """The struck-through price sits next to the real one."""
        reader = load(
            PRODUCT_URL, pages.product_page(price="89.99", list_price="149.99")
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.price == usd("89.99")

    def test_split_price_markup_does_not_multiply_by_a_hundred(self, load) -> None:
        """With no screen-reader span, the split nodes must still read right."""
        reader = load(
            PRODUCT_URL,
            pages.product_page(
                price="109.97", list_price=None, include_offscreen_price=False
            ),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.price == usd("109.97")
        assert snapshot.price != usd("10997.00")

    def test_split_price_with_thousands_separator(self, load) -> None:
        reader = load(
            PRODUCT_URL,
            pages.product_page(
                price="1299.00", list_price=None, include_offscreen_price=False
            ),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.price == usd("1299.00")

    def test_unit_price_does_not_win(self, load) -> None:
        reader = load(
            PRODUCT_URL,
            pages.product_page(price="24.99", list_price=None, unit_price="0.42"),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.price == usd("24.99")

    def test_third_party_seller_is_reported_verbatim(self, load) -> None:
        reader = load(
            PRODUCT_URL,
            pages.product_page(seller="XYZ Marketplace LLC", ships_from="Amazon.com"),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.seller == "XYZ Marketplace LLC"
        assert snapshot.seller_is_amazon is False

    def test_a_seller_link_that_is_only_a_label_is_not_the_seller(
        self, load
    ) -> None:
        """"Learn more about the seller" is a control, not a name.

        A live signed-in page served exactly this: the id the parser trusted
        first held a link label, and the seller's name sat in a different
        element. The rehearsal reported the seller as "Learn more about the
        seller" -- which blocked, because the rule was Amazon-only, but for
        the wrong reason, and under a manufacturer or approved-list rule that
        string would have been stored as the expectation.
        """
        reader = load(
            PRODUCT_URL,
            pages.product_page(
                seller="Aproca Direct",
                ships_from="Amazon",
                seller_link_is_a_label=True,
            ),
        )
        snapshot = PARSER.parse(reader)

        assert snapshot.seller == "Aproca Direct"
        assert snapshot.ships_from == "Amazon"
        assert snapshot.seller_is_amazon is False

    def test_the_older_layout_still_reads_the_seller_from_the_link(
        self, load
    ) -> None:
        """Signed out, the same id holds the name. Both must work."""
        reader = load(
            PRODUCT_URL, pages.product_page(seller="XYZ Marketplace LLC")
        )
        assert PARSER.parse(reader).seller == "XYZ Marketplace LLC"

    def test_missing_seller_is_none_not_a_guess(self, load) -> None:
        reader = load(
            PRODUCT_URL, pages.product_page(seller=None, ships_from=None)
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.seller is None

    def test_used_condition_is_detected(self, load) -> None:
        reader = load(
            PRODUCT_URL, pages.product_page(condition="Used - Like New")
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.condition is ItemCondition.USED

    def test_renewed_condition_is_detected(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_page(condition="Renewed"))
        snapshot = PARSER.parse(reader)
        assert snapshot.condition is ItemCondition.RENEWED

    def test_inline_twister_variation(self, load) -> None:
        reader = load(
            PRODUCT_URL,
            pages.product_page(
                dimensions={"Color": "Red", "Size": "Large"}, inline_twister=True
            ),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.variation.dimensions == {"Color": "Red", "Size": "Large"}

    def test_legacy_twister_variation(self, load) -> None:
        reader = load(
            PRODUCT_URL,
            pages.product_page(dimensions={"Size": "Medium", "Style": "Deluxe"}),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.variation.dimensions == {"Size": "Medium", "Style": "Deluxe"}

    def test_unvaried_product_has_an_empty_variation(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_page(dimensions={}))
        snapshot = PARSER.parse(reader)
        assert snapshot.variation.is_empty
        assert snapshot.variation_picker_present is False
        assert snapshot.variation_unreadable is False

    def test_a_picker_whose_labels_are_unfamiliar_is_reported_as_unreadable(
        self, load
    ) -> None:
        """Two states that look alike and are not: none, and unreadable.

        Labels outside ``KNOWN_VARIATION_LABELS`` are dropped, because Amazon
        puts non-dimension labels in the same markup and treating those as
        dimensions would block ordinary purchases. The cost is that a renamed
        dimension leaves the rule with no variation expectation at all, and
        the guard can only compare what was stored. Recording that the picker
        was there is what lets the user be told, instead of assuming the
        colour they were looking at was written down.
        """
        reader = load(
            PRODUCT_URL,
            pages.product_page(dimensions={"Finish Tone": "Matte Charcoal"}),
        )
        snapshot = PARSER.parse(reader)

        assert snapshot.variation.is_empty, "the label is not a known dimension"
        assert snapshot.variation_picker_present is True
        assert snapshot.variation_unreadable is True

    def test_an_unfamiliar_label_beside_a_familiar_one_is_not_unreadable(
        self, load
    ) -> None:
        """Something was read, so the expectation is not empty."""
        reader = load(
            PRODUCT_URL,
            pages.product_page(dimensions={"Color": "Black", "Finish Tone": "Matte"}),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.variation.dimensions == {"Color": "Black"}
        assert snapshot.variation_picker_present is True
        assert snapshot.variation_unreadable is False

    def test_the_inline_picker_is_detected_too(self, load) -> None:
        reader = load(
            PRODUCT_URL,
            pages.product_page(
                dimensions={"Finish Tone": "Matte"}, inline_twister=True
            ),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.variation_picker_present is True
        assert snapshot.variation_unreadable is True

    def test_variation_fingerprint_changes_with_the_selection(self, load) -> None:
        black = PARSER.parse(
            load(PRODUCT_URL, pages.product_page(dimensions={"Color": "Black"}))
        )
        red = PARSER.parse(
            load(PRODUCT_URL, pages.product_page(dimensions={"Color": "Red"}))
        )
        assert black.variation.fingerprint != red.variation.fingerprint

    def test_limited_quantity_is_read(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_page(max_quantity=3))
        snapshot = PARSER.parse(reader)
        assert snapshot.max_quantity == 3

    def test_asin_comes_from_the_hidden_input(self, load) -> None:
        """The hidden field wins over the URL slug, which can be stale."""
        reader = load(
            "https://www.amazon.com/some-slug/dp/B0DIFFEREN",
            pages.product_page(asin="B0REALASIN"),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.asin == "B0REALASIN"

    def test_malformed_asin_is_refused_rather_than_trusted(self, load) -> None:
        """An 11-character value is not an ASIN, so it must not be used."""
        reader = load(
            "https://www.amazon.com/dp/B07XYZ1234",
            pages.product_page(asin="B0TOOLONG123"),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.asin == "B07XYZ1234"


class TestUnavailableProduct:
    def test_out_of_stock(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_unavailable())
        snapshot = PARSER.parse(reader, expected_asin=ASIN)
        assert snapshot.availability is Availability.OUT_OF_STOCK
        assert snapshot.price is None
        assert snapshot.in_stock is False
        assert snapshot.add_to_cart_available is False

    def test_no_price_is_none_not_zero(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_unavailable())
        snapshot = PARSER.parse(reader, expected_asin=ASIN)
        assert snapshot.price is None


class TestSubscribeAndSave:
    def test_preselected_subscription_is_flagged(self, load) -> None:
        reader = load(
            PRODUCT_URL,
            pages.product_page(subscribe_and_save=True, subscribe_preselected=True),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.subscription_preselected is True

    def test_one_time_selected_is_not_flagged(self, load) -> None:
        reader = load(
            PRODUCT_URL,
            pages.product_page(subscribe_and_save=True, subscribe_preselected=False),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.subscription_preselected is False

    def test_no_subscription_offer_is_not_flagged(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_page(subscribe_and_save=False))
        snapshot = PARSER.parse(reader)
        assert snapshot.subscription_preselected is False

    def test_a_renamed_accordion_row_is_still_treated_as_a_subscription(
        self, load
    ) -> None:
        """The ids are Amazon's and they change; the wording is the backstop.

        Relying on the row ids alone meant a renamed Subscribe & Save row
        read as "one-time purchase" and the guard's subscription check
        passed -- an unwanted recurring order.
        """
        reader = load(
            PRODUCT_URL,
            pages.product_page(
                subscribe_and_save=True,
                subscribe_preselected=True,
                rename_subscription_ids=True,
            ),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.subscription_preselected is True

    def test_a_renamed_row_is_flagged_even_when_one_time_looks_selected(
        self, load
    ) -> None:
        """Being unable to confirm is reported as a subscription, not as a no.

        With the one-time row renamed too, nothing on the page can be shown
        to be the active option, so the only safe answer is "subscription".
        """
        reader = load(
            PRODUCT_URL,
            pages.product_page(
                subscribe_and_save=True,
                subscribe_preselected=False,
                rename_subscription_ids=True,
            ),
        )
        snapshot = PARSER.parse(reader)
        assert snapshot.subscription_preselected is True

    def test_a_recommendation_strip_mentioning_the_words_is_not_a_subscription(
        self, load
    ) -> None:
        """The wording check is scoped to the buy box for a reason.

        "Subscribe & Save" appears in recommendation strips on pages with no
        subscription option at all. Treating those as subscriptions would
        block ordinary purchases.
        """
        page = pages.product_page(subscribe_and_save=False).replace(
            "</body>",
            '<div id="similarities_feature_div">Subscribe &amp; Save on '
            "related items and deliver every 2 months</div></body>",
        )
        reader = load(PRODUCT_URL, page)
        snapshot = PARSER.parse(reader)
        assert snapshot.subscription_preselected is False


class TestPageClassification:
    def test_product_page(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_page())
        result = DETECTOR.classify(reader)
        assert result.kind is PageKind.PRODUCT
        assert result.signed_in is True
        assert result.account_name == "John"
        assert not result.needs_human

    def test_signed_out_product_page(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_page(signed_in=False))
        result = DETECTOR.classify(reader)
        assert result.kind is PageKind.PRODUCT
        assert result.signed_in is False

    def test_sign_in_page(self, load) -> None:
        reader = load("https://www.amazon.com/ap/signin", pages.sign_in_page())
        result = DETECTOR.classify(reader)
        assert result.kind is PageKind.SIGN_IN
        assert result.needs_human
        assert result.error_code is ErrorCode.LOGIN_EXPIRED

    def test_mfa_page(self, load) -> None:
        reader = load("https://www.amazon.com/ap/mfa", pages.mfa_page())
        result = DETECTOR.classify(reader)
        assert result.kind is PageKind.MFA
        assert result.needs_human
        assert result.error_code is ErrorCode.VERIFICATION_REQUIRED

    def test_captcha_served_at_a_product_url_is_still_a_captcha(self, load) -> None:
        """Trusting the URL would make the app read a price off a challenge."""
        reader = load(PRODUCT_URL, pages.captcha_page())
        result = DETECTOR.classify(reader)
        assert result.kind is PageKind.CAPTCHA
        assert result.needs_human

    def test_service_error_page(self, load) -> None:
        reader = load(PRODUCT_URL, pages.service_error_page())
        result = DETECTOR.classify(reader)
        assert result.kind is PageKind.SERVICE_ERROR
        assert result.error_code is ErrorCode.RATE_LIMITED
        assert not result.needs_human

    def test_not_found_page(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_not_found())
        result = DETECTOR.classify(reader)
        assert result.kind is PageKind.NOT_FOUND

    def test_cart_page(self, load) -> None:
        reader = load(
            "https://www.amazon.com/gp/cart/view.html", pages.cart_page()
        )
        assert DETECTOR.classify(reader).kind is PageKind.CART

    def test_checkout_page(self, load) -> None:
        reader = load(
            "https://www.amazon.com/gp/buy/spc/handlers/display.html",
            pages.checkout_page(),
        )
        assert DETECTOR.classify(reader).kind is PageKind.CHECKOUT

    def test_confirmation_page(self, load) -> None:
        reader = load(
            "https://www.amazon.com/gp/buy/thankyou/handlers/display.html",
            pages.confirmation_page(),
        )
        assert DETECTOR.classify(reader).kind is PageKind.CONFIRMATION

    def test_addon_sheet(self, load) -> None:
        reader = load(PRODUCT_URL, pages.add_to_cart_addon_sheet())
        assert DETECTOR.classify(reader).kind is PageKind.ADDON_SHEET


class TestSessionState:
    def test_signed_in_session(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_page(signed_in=True))
        state = DETECTOR.session_state(reader)
        assert state.signed_in is True
        assert state.usable is True
        assert state.account_name == "John"

    def test_signed_out_session(self, load) -> None:
        reader = load(PRODUCT_URL, pages.product_page(signed_in=False))
        state = DETECTOR.session_state(reader)
        assert state.signed_in is False
        assert state.usable is False

    def test_sign_in_page_means_not_signed_in(self, load) -> None:
        reader = load("https://www.amazon.com/ap/signin", pages.sign_in_page())
        state = DETECTOR.session_state(reader)
        assert state.signed_in is False

    def test_captcha_means_verification_needed(self, load) -> None:
        reader = load(PRODUCT_URL, pages.captcha_page())
        state = DETECTOR.session_state(reader)
        assert state.needs_verification is True
        assert state.usable is False

    def test_a_signed_in_nav_bar_with_a_sign_in_form_is_not_usable(
        self, load, page, site
    ) -> None:
        """Amazon can cache a signed-in nav bar on a page that re-challenges."""
        html = pages.product_page(signed_in=True).replace(
            "</body>",
            '<form name="signIn"><input type="password" id="ap_password"></form></body>',
        )
        site.add("www.amazon.com/dp/", html)
        page.goto(PRODUCT_URL, wait_until="domcontentloaded")
        from app.automation.page_reader import PageReader

        state = DETECTOR.session_state(PageReader(page))
        assert state.signed_in is False


class TestNoNetworkEscape:
    def test_every_request_is_intercepted(self, load, site) -> None:
        """Proof these tests cannot reach Amazon."""
        load(PRODUCT_URL, pages.product_page())
        site.assert_no_unexpected_requests()

"""End-to-end proof that a real automation run leaks nothing into the logs.

The unit tests in ``test_redaction.py`` prove the filter works on strings.
This test proves the *wiring*: it runs the real parser and classifier, with
the real logging configuration, against a page stuffed with credentials, and
then reads the log files off disk looking for any of them.

This is the test that would catch someone adding a well-meaning
``logger.debug(page.content())``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.automation.cart_manager import MANAGER as CART
from app.automation.checkout_manager import MANAGER as CHECKOUT
from app.automation.login_detector import DETECTOR
from app.automation.product_parser import PARSER
from app.diagnostics.logger import reset_logging, setup_logging
from tests.fixtures import amazon_pages as pages

pytestmark = pytest.mark.integration

#: Values planted in the fixture pages. None of these may appear in a log.
SECRETS = {
    "session_token": "Atza|IwEBIExampleSessionTokenValue0123456789",
    "at_main": "AtzaSecretAtMainCookieValue987654321",
    "ubid": "133-9988776-5544332ubidsecret",
    "password": "hunter2SuperSecret",
    "card": "4111111111111111",
    "cvv_value": "917",
    "csrf": "gJc0SecretCsrfTokenValue",
    "otp": "482913",
}


def _page_with_secrets(base_html: str) -> str:
    """Plant credentials everywhere a real Amazon page might carry them."""
    injected = f"""
    <input type="hidden" name="anti-csrftoken-a2z" value="{SECRETS['csrf']}">
    <input type="hidden" name="session-token" value="{SECRETS['session_token']}">
    <input type="password" id="ap_password" value="{SECRETS['password']}">
    <input type="hidden" name="addCreditCardNumber" value="{SECRETS['card']}">
    <input type="hidden" name="cvv" value="{SECRETS['cvv_value']}">
    <input type="hidden" id="auth-mfa-otpcode-value" value="{SECRETS['otp']}">
    <div id="planted-cookies" style="display:none">
      Cookie: session-token={SECRETS['session_token']}; at-main={SECRETS['at_main']};
      ubid-main={SECRETS['ubid']}
    </div>
    <script>
      window.__state = {{
        "session-token": "{SECRETS['session_token']}",
        "password": "{SECRETS['password']}",
        "cardNumber": "{SECRETS['card']}"
      }};
    </script>
    """
    return base_html.replace("</body>", f"{injected}</body>")


def _log_text(logs_dir: Path) -> str:
    """Everything written to every log file, as one string."""
    for handler in logging.getLogger().handlers:
        handler.flush()
    collected: list[str] = []
    for path in sorted(logs_dir.glob("*")):
        if path.is_file():
            collected.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(collected)


@pytest.fixture
def logging_to(tmp_path: Path):
    """Configure the real logging stack into a throwaway directory."""
    logs_dir = tmp_path / "logs"
    reset_logging()
    setup_logging(logs_dir, level=logging.DEBUG, console=False)
    yield logs_dir
    reset_logging()


class TestNoSecretsReachTheLogs:
    def test_product_page_with_credentials(self, load, logging_to) -> None:
        reader = load(
            "https://www.amazon.com/dp/B07XYZ1234",
            _page_with_secrets(pages.product_page()),
        )
        snapshot = PARSER.parse(reader, expected_asin="B07XYZ1234")
        DETECTOR.classify(reader)

        # The run must still have worked.
        assert snapshot.price is not None
        assert snapshot.seller == "Amazon.com"

        text = _log_text(logging_to)
        assert text, "nothing was logged, so this test proves nothing"
        for name, value in SECRETS.items():
            assert value not in text, f"{name} leaked into the logs"

    def test_cart_page_with_credentials(self, load, logging_to) -> None:
        reader = load(
            "https://www.amazon.com/gp/cart/view.html",
            _page_with_secrets(pages.cart_with_other_items()),
        )
        cart = CART.read_cart(reader)
        assert len(cart.lines) == 3

        text = _log_text(logging_to)
        for name, value in SECRETS.items():
            assert value not in text, f"{name} leaked into the logs"

    def test_checkout_page_with_credentials(self, load, logging_to) -> None:
        reader = load(
            "https://www.amazon.com/gp/buy/spc/handlers/display.html",
            _page_with_secrets(pages.checkout_page()),
        )
        checkout = CHECKOUT.read_checkout(reader)
        assert checkout.order_total is not None

        text = _log_text(logging_to)
        for name, value in SECRETS.items():
            assert value not in text, f"{name} leaked into the logs"

    def test_sign_in_page_with_credentials(self, load, logging_to) -> None:
        """The sign-in page is the highest-risk one to classify."""
        reader = load(
            "https://www.amazon.com/ap/signin?openid.claimed_id=secretclaim",
            _page_with_secrets(pages.sign_in_page()),
        )
        DETECTOR.session_state(reader)

        text = _log_text(logging_to)
        for name, value in SECRETS.items():
            assert value not in text, f"{name} leaked into the logs"
        assert "secretclaim" not in text, "a sign-in query string leaked"

    def test_url_query_strings_are_not_logged(self, load, logging_to) -> None:
        reader = load(
            "https://www.amazon.com/dp/B07XYZ1234?session-id=SECRETSESSION123"
            "&pd_rd_r=SECRETREF456",
            pages.product_page(),
        )
        PARSER.parse(reader, expected_asin="B07XYZ1234")
        DETECTOR.classify(reader)

        text = _log_text(logging_to)
        assert "SECRETSESSION123" not in text
        assert "SECRETREF456" not in text

    def test_useful_information_still_reaches_the_logs(
        self, load, logging_to
    ) -> None:
        """Redaction must not have been achieved by logging nothing."""
        reader = load(
            "https://www.amazon.com/dp/B07XYZ1234",
            _page_with_secrets(pages.product_page()),
        )
        PARSER.parse(reader, expected_asin="B07XYZ1234")

        text = _log_text(logging_to)
        assert "B07XYZ1234" in text, "the ASIN should be logged"
        assert "Product read" in text, "the parse step should be logged"
        assert "Amazon.com" in text, "the seller should be logged"


class TestDiagnosticsExport:
    def test_export_contains_no_secrets(self, load, logging_to, tmp_path) -> None:
        """The support bundle must be safe to email."""
        import json

        from app.database.database import Database
        from app.paths import AppPaths

        reader = load(
            "https://www.amazon.com/dp/B07XYZ1234",
            _page_with_secrets(pages.product_page()),
        )
        PARSER.parse(reader, expected_asin="B07XYZ1234")

        paths = AppPaths(root=tmp_path / "data").ensure()
        database = Database(paths.database_file, backups_dir=paths.backups_dir)
        database.migrate()
        try:
            from app.config import SettingsService
            from app.database.repositories import ActivityRepository
            from app.diagnostics import report as report_module

            settings = SettingsService(database)
            activity = ActivityRepository(database)
            activity.record_diagnostic(
                step="read_price",
                error_code="unexpected_page",
                page_url=(
                    "https://www.amazon.com/dp/B07XYZ1234?session-token="
                    f"{SECRETS['session_token']}"
                ),
                screenshot_file="shot.png",
                metadata={"cookies": f"at-main={SECRETS['at_main']}"},
            )

            builder = getattr(report_module, "build_report", None)
            if builder is None:  # pragma: no cover - API guard
                pytest.skip("build_report is not available")

            payload = builder(
                paths=paths,
                database=database,
                settings=settings,
                activity=activity,
            )
            serialised = json.dumps(payload, default=str)
        finally:
            database.close_all()

        for name, value in SECRETS.items():
            assert value not in serialised, f"{name} leaked into the export"

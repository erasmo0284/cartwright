"""Integration-test harness: a real browser, fixture pages, no network.

Every request is intercepted and answered from
:mod:`tests.fixtures.amazon_pages`, so these tests exercise real Playwright
against real Chromium while being incapable of reaching Amazon -- and
therefore incapable of placing an order. A request to an address the test did
not register fails the test loudly rather than escaping to the internet.

The browser here is launched headless and independently of
:class:`app.automation.browser_manager.BrowserManager`, which deliberately
runs headed: what is under test is the parsing and the flow, not the profile
lifecycle.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from app.paths import AppPaths


def _browsers_dir() -> Path:
    """Where Chromium is installed.

    Resolved exactly as the application resolves it, rather than by
    reconstructing the path -- ``AppPaths`` calls ``resolve()``, which under
    Windows filesystem redirection (a packaged host process, for instance)
    yields a different absolute path for the same directory. Duplicating the
    logic here would make the tests look in the wrong place.
    """
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override)
    # The real user directory is deliberate: these tests need the browser the
    # application installed, and the ``app_paths`` fixture's temporary root
    # would never contain one.
    saved = os.environ.pop("APB_DATA_DIR", None)
    try:
        from app.paths import get_paths, reset_paths_cache

        reset_paths_cache()
        return get_paths().playwright_browsers_dir
    finally:
        if saved is not None:
            os.environ["APB_DATA_DIR"] = saved
        from app.paths import reset_paths_cache as _reset

        _reset()


def pytest_configure(config: pytest.Config) -> None:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_browsers_dir()))


@pytest.fixture(scope="session")
def playwright_instance() -> Iterator[object]:
    playwright = pytest.importorskip(
        "playwright.sync_api", reason="Playwright is not installed"
    )
    instance = playwright.sync_playwright().start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture(scope="session")
def browser(playwright_instance) -> Iterator[object]:
    browsers_dir = Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"])
    if not any(browsers_dir.glob("chromium-*")):
        pytest.skip(
            "Chromium is not installed. Run scripts/install_browser.py first."
        )
    # ``channel="chromium"`` is required, not cosmetic: a plain
    # ``headless=True`` selects the separate ``chromium-headless-shell``
    # binary, which the application deliberately does not install (it is
    # another 270 MB that a headed browser can never use). The channel opts
    # into Chrome's newer headless mode on the full build instead.
    instance = playwright_instance.chromium.launch(headless=True, channel="chromium")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def context(browser) -> Iterator[object]:
    ctx = browser.new_context(viewport={"width": 1360, "height": 900})
    ctx.set_default_timeout(5_000)
    ctx.set_default_navigation_timeout(10_000)
    try:
        yield ctx
    finally:
        ctx.close()


class FixtureSite:
    """Serves fixture HTML for Amazon URLs, and blocks everything else."""

    def __init__(self, context: object) -> None:
        self._context = context
        self._routes: dict[str, str] = {}
        self._status: dict[str, int] = {}
        self.unexpected: list[str] = []
        context.route("**/*", self._handle)

    def add(self, url_fragment: str, html: str, *, status: int = 200) -> None:
        """Answer any URL containing ``url_fragment`` with ``html``."""
        self._routes[url_fragment.lower()] = html
        self._status[url_fragment.lower()] = status

    def _handle(self, route: object, request: object) -> None:
        url = request.url.lower()
        # Images and fonts are irrelevant to parsing; answer them cheaply so
        # a page does not stall waiting for them.
        if request.resource_type in {"image", "font", "media", "stylesheet"}:
            route.fulfill(status=200, body=b"", content_type="image/png")
            return
        for fragment, html in self._routes.items():
            if fragment in url:
                route.fulfill(
                    status=self._status[fragment],
                    content_type="text/html; charset=utf-8",
                    body=html,
                )
                return
        self.unexpected.append(request.url)
        route.fulfill(
            status=404,
            content_type="text/html",
            body="<html><body>unrouted request</body></html>",
        )

    def assert_no_unexpected_requests(self) -> None:
        assert not self.unexpected, f"unrouted requests: {self.unexpected}"


@pytest.fixture
def site(context) -> Iterator[FixtureSite]:
    fixture_site = FixtureSite(context)
    yield fixture_site


@pytest.fixture
def page(context) -> Iterator[object]:
    new_page = context.new_page()
    try:
        yield new_page
    finally:
        new_page.close()


@pytest.fixture
def load(page, site) -> Callable[..., object]:
    """Serve ``html`` at ``url`` and navigate to it, returning a PageReader.

    ``also`` registers further documents that the page will itself request, as
    a mapping of URL to HTML. The Buy Now modal is served from its own address
    inside an iframe, and a document the test did not register is answered with
    a 404 and recorded as unexpected -- so a frame that matters has to be
    routed deliberately, exactly like the page that hosts it.
    """
    from app.automation.page_reader import PageReader

    def _load(
        url: str, html: str, *, also: Mapping[str, str] | None = None
    ) -> PageReader:
        for other_url, other_html in (also or {}).items():
            site.add(other_url.split("://", 1)[-1], other_html)
        fragment = url.split("://", 1)[-1]
        site.add(fragment, html)
        page.goto(url, wait_until="domcontentloaded")
        return PageReader(page)

    return _load

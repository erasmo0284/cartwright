"""Tests for the product thumbnail cache.

The pictures exist because the interface was showing the words "No image" on
every watch card and a cart icon on every purchase confirmation: nothing had
ever loaded one. They are taken by screenshotting the image element on the
page the browser has already rendered, which is why there is no HTTP client
anywhere near this -- see ``docs/SECURITY.md``, which states that the
application makes no outbound requests of its own and is checked by a grep.

What matters here is that a missing, corrupt or oversized picture is an
ordinary outcome that leaves the interface working, because this code runs
while somebody is waiting to buy something.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.automation.image_capture import (  # noqa: E402
    CACHE_MAX_AGE_SECONDS,
    MAX_BYTES,
    capture_thumbnail,
    is_fresh,
    thumbnail_path,
)
from app.paths import AppPaths  # noqa: E402
from app.ui import images  # noqa: E402

ASIN = "B01N5OSTVQ"


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _forget_decoded() -> None:
    images.clear_cache()


def png_bytes(size: int = 8) -> bytes:
    """A real PNG, produced by Qt rather than pasted in as a literal."""
    pixmap = QPixmap(size, size)
    pixmap.fill()
    path = Path(os.environ["TEMP"]) / f"apb-test-{size}.png"
    assert pixmap.save(str(path), "PNG")
    return path.read_bytes()


class FakeLocator:
    """Stands in for a Playwright locator around the product image."""

    def __init__(self, data: bytes | None = None, error: Exception | None = None):
        self._data = data
        self._error = error
        self.calls = 0

    def screenshot(self, timeout: int | None = None) -> bytes:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._data or b""


class FakeReader:
    """A page reader that hands back one locator, or none."""

    def __init__(self, locator: object | None):
        self._locator = locator
        self.lookups = 0

    def find(self, chain, *, scope=None, visible_only=False):
        self.lookups += 1
        if self._locator is None:
            return None, None
        return self._locator, None


class TestCapture:
    def test_a_picture_is_written_and_found_again(
        self, app_paths: AppPaths, qt_app
    ) -> None:
        reader = FakeReader(FakeLocator(png_bytes()))
        written = capture_thumbnail(
            reader, app_paths, asin=ASIN, marketplace="www.amazon.com"
        )
        assert written is not None and written.exists()
        assert written == thumbnail_path(app_paths, ASIN, "www.amazon.com")
        assert images.thumbnail(ASIN, paths=app_paths) is not None

    def test_a_fresh_picture_is_not_taken_again(
        self, app_paths: AppPaths, qt_app
    ) -> None:
        """A product's photograph does not change between price checks."""
        locator = FakeLocator(png_bytes())
        reader = FakeReader(locator)
        capture_thumbnail(reader, app_paths, asin=ASIN, marketplace="www.amazon.com")
        capture_thumbnail(reader, app_paths, asin=ASIN, marketplace="www.amazon.com")
        assert locator.calls == 1, "the second call should have used the cache"

    def test_force_retakes_it(self, app_paths: AppPaths, qt_app) -> None:
        locator = FakeLocator(png_bytes())
        reader = FakeReader(locator)
        capture_thumbnail(reader, app_paths, asin=ASIN, marketplace="www.amazon.com")
        capture_thumbnail(
            reader, app_paths, asin=ASIN, marketplace="www.amazon.com", force=True
        )
        assert locator.calls == 2

    def test_a_stale_picture_is_retaken(self, app_paths: AppPaths, qt_app) -> None:
        path = thumbnail_path(app_paths, ASIN, "www.amazon.com")
        app_paths.images_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes())
        old = time.time() - CACHE_MAX_AGE_SECONDS - 60
        os.utime(path, (old, old))
        assert not is_fresh(path)

        locator = FakeLocator(png_bytes())
        capture_thumbnail(
            FakeReader(locator), app_paths, asin=ASIN, marketplace="www.amazon.com"
        )
        assert locator.calls == 1

    def test_no_image_element_is_not_an_error(
        self, app_paths: AppPaths, qt_app
    ) -> None:
        assert (
            capture_thumbnail(
                FakeReader(None), app_paths, asin=ASIN, marketplace="www.amazon.com"
            )
            is None
        )

    def test_a_failing_screenshot_is_not_an_error(
        self, app_paths: AppPaths, qt_app
    ) -> None:
        """A picture must never be the reason a purchase fails."""
        reader = FakeReader(FakeLocator(error=RuntimeError("detached")))
        assert (
            capture_thumbnail(
                reader, app_paths, asin=ASIN, marketplace="www.amazon.com"
            )
            is None
        )

    def test_an_oversized_response_is_refused(
        self, app_paths: AppPaths, qt_app
    ) -> None:
        """A thumbnail is tens of kilobytes; this is not a thumbnail."""
        reader = FakeReader(FakeLocator(b"\x89PNG" + b"\x00" * (MAX_BYTES + 1)))
        assert (
            capture_thumbnail(
                reader, app_paths, asin=ASIN, marketplace="www.amazon.com"
            )
            is None
        )
        assert not thumbnail_path(app_paths, ASIN, "www.amazon.com").exists()

    def test_pictures_live_under_the_data_directory(
        self, app_paths: AppPaths
    ) -> None:
        path = thumbnail_path(app_paths, ASIN, "www.amazon.com")
        assert app_paths.images_dir in path.parents
        assert app_paths.images_dir in app_paths.all_directories()

    def test_one_file_per_marketplace(self, app_paths: AppPaths) -> None:
        """The same ASIN is a different product on a different storefront."""
        assert thumbnail_path(app_paths, ASIN, "www.amazon.com") != thumbnail_path(
            app_paths, ASIN, "www.amazon.co.uk"
        )


class TestLoading:
    def test_a_missing_picture_is_none(self, app_paths: AppPaths, qt_app) -> None:
        assert images.thumbnail(ASIN, paths=app_paths) is None

    def test_no_asin_is_none(self, app_paths: AppPaths, qt_app) -> None:
        assert images.thumbnail(None, paths=app_paths) is None
        assert images.thumbnail("", paths=app_paths) is None

    def test_a_file_that_is_not_an_image_is_none(
        self, app_paths: AppPaths, qt_app
    ) -> None:
        """A truncated write must leave the interface working."""
        app_paths.images_dir.mkdir(parents=True, exist_ok=True)
        thumbnail_path(app_paths, ASIN, "www.amazon.com").write_bytes(b"not a png")
        assert images.thumbnail(ASIN, paths=app_paths) is None

    def test_the_decoded_picture_is_reused(self, app_paths: AppPaths, qt_app) -> None:
        """The watch list rebuilds its cards on every refresh."""
        app_paths.images_dir.mkdir(parents=True, exist_ok=True)
        thumbnail_path(app_paths, ASIN, "www.amazon.com").write_bytes(png_bytes())
        first = images.thumbnail(ASIN, paths=app_paths)
        second = images.thumbnail(ASIN, paths=app_paths)
        assert first is not None
        assert first is second, "the same pixmap should be handed back"

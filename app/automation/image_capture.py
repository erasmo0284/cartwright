"""Keeping a picture of the product, without fetching anything.

The application makes no outbound network requests of its own -- that is a
documented property, checked by a grep over ``app/`` in
``docs/SECURITY.md`` -- so downloading a thumbnail with an HTTP client is not
available, and would not be desirable: it would be traffic the user cannot
see in the browser window in front of them.

The browser has already loaded the image in order to display it. So the
thumbnail is taken as a **screenshot of that element**, which adds no
request at all, cannot be blocked by CORS, needs no credentials, and is
literally a picture of what the user was looking at.

Cached under the data directory by marketplace and ASIN, and refreshed only
when it is old: a product's photograph is not the sort of thing that changes
between price checks.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Final

from app.automation import selectors
from app.automation.page_reader import PageReader
from app.paths import AppPaths

logger = logging.getLogger("app.automation.image")

#: How long a cached thumbnail is trusted. Long, because product photographs
#: change rarely and a stale one is a cosmetic problem, not a safety one.
CACHE_MAX_AGE_SECONDS: Final[int] = 30 * 24 * 60 * 60

#: A ceiling on what is written to disk. An element screenshot of a 500px
#: image is tens of kilobytes; anything approaching this is not a thumbnail.
MAX_BYTES: Final[int] = 2 * 1024 * 1024

#: How long to wait for the element. Short on purpose: a missing picture must
#: never slow down a purchase, which is the thing the user is waiting for.
SCREENSHOT_TIMEOUT_MS: Final[int] = 4_000


def thumbnail_path(paths: AppPaths, asin: str, marketplace: str) -> Path:
    """Where this product's thumbnail lives, whether or not it exists yet."""
    safe_marketplace = marketplace.replace("/", "-").replace("\\", "-")
    return paths.images_dir / f"{safe_marketplace}-{asin.upper()}.png"


def is_fresh(path: Path, *, now: float | None = None) -> bool:
    """Whether a cached thumbnail is recent enough to keep."""
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return False
    return 0 <= age < CACHE_MAX_AGE_SECONDS


def capture_thumbnail(
    reader: PageReader,
    paths: AppPaths,
    *,
    asin: str,
    marketplace: str,
    force: bool = False,
) -> Path | None:
    """Save a picture of the product image, and return where it went.

    Returns ``None`` when there was nothing to photograph, which is an
    ordinary outcome: the product page may not have rendered its image yet,
    and the interface shows a placeholder. Every failure here is swallowed,
    because a thumbnail is decoration and the caller is in the middle of
    reading a product for a purchase.
    """
    if not asin:
        return None

    destination = thumbnail_path(paths, asin, marketplace)
    if not force and is_fresh(destination):
        return destination

    locator, _ = reader.find(selectors.PRODUCT_IMAGE, visible_only=True)
    if locator is None:
        logger.info("No product image to photograph", extra={"asin": asin})
        return None

    try:
        data = locator.screenshot(timeout=SCREENSHOT_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - a picture is never worth failing for
        logger.info("Could not photograph the product image", extra={"asin": asin})
        return None

    if not data or len(data) > MAX_BYTES:
        logger.info(
            "Ignoring an implausible product image",
            extra={"asin": asin, "bytes": len(data or b"")},
        )
        return None

    try:
        paths.images_dir.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    except OSError:
        logger.warning("Could not store the product image", extra={"asin": asin})
        return None

    logger.info(
        "Stored a product image", extra={"asin": asin, "bytes": len(data)}
    )
    return destination

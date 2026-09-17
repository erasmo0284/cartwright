"""Loading the cached product thumbnails for display.

The pictures themselves are taken by :mod:`app.automation.image_capture`,
from the page the browser had already rendered. This module only reads what
is on disk, which is why it can be called freely from a paint-adjacent code
path: a missing or unreadable file is an ordinary answer, not an error, and
the caller shows its placeholder.

Decoded pixmaps are kept in a small cache because the watch list rebuilds
its cards on every refresh, and decoding the same PNG once per refresh per
card is work nobody asked for.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from PySide6.QtGui import QPixmap

from app.automation.image_capture import thumbnail_path
from app.paths import AppPaths, get_paths

logger = logging.getLogger("app.ui.images")

#: How many decoded thumbnails to keep. A watch list of any sane length fits,
#: and each one is a few tens of kilobytes.
CACHE_LIMIT: Final[int] = 64

_CACHE: dict[str, QPixmap] = {}


def clear_cache() -> None:
    """Forget every decoded thumbnail. Used by tests and after a purge."""
    _CACHE.clear()


def thumbnail(
    asin: str | None,
    marketplace: str = "www.amazon.com",
    *,
    paths: AppPaths | None = None,
) -> QPixmap | None:
    """The cached picture of a product, or ``None`` if there is not one.

    ``None`` covers every uninteresting case at once: no ASIN, no file, a
    file that is not an image, a file being written as it is read. Callers
    fall back to a placeholder, so none of those is worth distinguishing.
    """
    if not asin:
        return None
    key = f"{marketplace}:{asin.upper()}"
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    resolved = paths or get_paths()
    path: Path = thumbnail_path(resolved, asin, marketplace)
    if not path.exists():
        return None

    pixmap = QPixmap()
    try:
        loaded = pixmap.load(str(path))
    except Exception:  # noqa: BLE001 - decoding is the caller's least concern
        loaded = False
    if not loaded or pixmap.isNull():
        logger.debug("A cached product image could not be decoded")
        return None

    if len(_CACHE) >= CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = pixmap
    return pixmap

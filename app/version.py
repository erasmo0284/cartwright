"""Single source of truth for the application version.

Keep this module free of imports so that build scripts can read it without
pulling in the rest of the application (or its third-party dependencies).
"""

from __future__ import annotations

VERSION = "1.0.1"
"""Semantic version of the application."""

BUILD_CHANNEL = "release"
"""Either ``release`` or ``dev``. Set by the build script for tagged builds."""

VERSION_TUPLE = tuple(int(part) for part in VERSION.split("."))

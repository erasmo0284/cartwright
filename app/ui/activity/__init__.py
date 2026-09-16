"""The Activity screen: a plain log of everything the program has done.

Kept in its own package so the screen, and the small widget bridge it needs,
can grow without the rest of the interface importing them.
"""

from __future__ import annotations

from app.ui.activity.activity_page import ActivityPage

__all__ = ["ActivityPage"]

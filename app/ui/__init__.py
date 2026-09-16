"""The desktop user interface.

Sub-packages are organised by screen (``dashboard``, ``purchase``,
``watchlist``, ``activity``, ``settings``, ``dialogs``) plus ``components``
for widgets shared between them and ``theme`` for the visual layer.

Nothing in here may import the automation or database layers directly at
module level; screens receive their collaborators from the application
assembly, so the UI stays constructible in tests with nothing behind it.
"""

from __future__ import annotations

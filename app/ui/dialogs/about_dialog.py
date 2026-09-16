"""The About box.

Small, but it carries the independence disclaimer, which is a requirement
rather than a nicety: the application automates a person's own Amazon
account and must not imply any relationship with Amazon.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from app.branding import BRAND
from app.ui.components import ButtonRow, PrimaryButton, SubtleButton
from app.ui.theme import app_icon, font_body, font_title
from app.version import BUILD_CHANNEL, VERSION


class AboutDialog(QDialog):
    """Shows the product name, version and legal notice."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"About {BRAND.display_name}")
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(48, 48))
        icon.setAlignment(Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(icon)

        name = QLabel(BRAND.display_name)
        name.setFont(font_title())
        name.setProperty("role", "title")
        layout.addWidget(name)

        tagline = QLabel(BRAND.tagline)
        tagline.setProperty("role", "subtitle")
        tagline.setWordWrap(True)
        layout.addWidget(tagline)

        version = QLabel(
            f"Version {VERSION}"
            + (f" ({BUILD_CHANNEL})" if BUILD_CHANNEL != "release" else "")
        )
        version.setFont(font_body())
        version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(version)

        disclaimer = QLabel(BRAND.disclaimer)
        disclaimer.setProperty("role", "caption")
        disclaimer.setWordWrap(True)
        disclaimer.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(disclaimer)

        safety = QLabel(
            "This app never stores your Amazon password, your one-time codes "
            "or your card details. You sign in directly with Amazon in its "
            "own browser window."
        )
        safety.setProperty("role", "caption")
        safety.setWordWrap(True)
        layout.addWidget(safety)

        layout.addStretch(1)

        self._logs_requested = False
        logs = SubtleButton("Open logs folder")
        logs.clicked.connect(self._on_logs)

        close = PrimaryButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)

        layout.addWidget(ButtonRow(logs, close, align="right"))

    def _on_logs(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from app.paths import get_paths

        self._logs_requested = True
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(get_paths().logs_dir)))

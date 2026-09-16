"""Centralised branding.

Every user-visible product name, identifier and path fragment is derived from
this module so the application can be re-branded by editing a single file.
Nothing else in the codebase should hard-code the product name.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Branding:
    """Names and identifiers that together define the product's identity."""

    #: Full product name, shown in window titles, the About box and installers.
    display_name: str = "Amazon Purchase Bot"

    #: Short name for cramped surfaces (tray tooltip, taskbar).
    short_name: str = "Purchase Bot"

    #: Vendor / publisher name used by the installer and file metadata.
    publisher: str = "Amazon Purchase Bot"

    #: Folder name under %LOCALAPPDATA%. Must be a valid Windows directory name.
    data_folder_name: str = "AmazonPurchaseBot"

    #: Executable stem produced by the build (``<exe_name>.exe``).
    exe_name: str = "AmazonPurchaseBot"

    #: Windows Application User Model ID. Required for Action Center toasts to
    #: display the correct application name and icon. Convention is
    #: ``Company.Product.SubProduct.Version``.
    aumid: str = "AmazonPurchaseBot.Desktop"

    #: Name of the HKCU\...\Run registry value used for "start with Windows".
    startup_registry_value: str = "AmazonPurchaseBot"

    #: Named pipe / local socket key used for single-instance enforcement.
    single_instance_key: str = "AmazonPurchaseBot.SingleInstance"

    #: Qt organisation/application names (affect QSettings, which we do not use
    #: for real data, but which Qt consults for some platform integrations).
    qt_organization: str = "AmazonPurchaseBot"
    qt_application: str = "AmazonPurchaseBot"

    #: Shown in the About dialog and the documentation footer.
    tagline: str = "Watch, verify and buy a product on your terms."

    #: Legal notice shown in About / first-run. Deliberately explicit that this
    #: is an independent tool.
    disclaimer: str = (
        "This application is an independent tool. It is not affiliated with, "
        "produced by, endorsed by or connected to Amazon.com, Inc. or any of "
        "its subsidiaries. You remain responsible for your Amazon account and "
        "for complying with Amazon's Conditions of Use."
    )


BRAND = Branding()
"""The active branding. Replace this object to re-brand the application."""

# PyInstaller build specification.
#
# Run through ``scripts/build.py``, which generates the version resource and
# the application icon first.
#
# The three decisions that shape this file:
#
# 1. **onedir, not onefile.** A onefile build re-extracts its entire archive
#    to %TEMP% on every launch. With Qt plus the Playwright Node driver that
#    is hundreds of megabytes of disk churn per start, it invites an
#    antivirus scan each time, and its bootloader cleanup is fragile. An
#    installer makes the single-file property worthless anyway.
#
# 2. **Chromium is NOT bundled.** It is ~430 MB and is installed into
#    %LOCALAPPDATA% on first run instead, so an application update does not
#    re-ship the browser. Playwright's own PyInstaller hooks would happily
#    sweep a browser into the bundle if one had been installed with
#    PLAYWRIGHT_BROWSERS_PATH=0, which is exactly why the build script
#    refuses to run in that state.
#
# 3. **No UPX.** Packing is the single biggest cause of antivirus false
#    positives and has a history of corrupting Qt DLLs. The 20-30% saved is
#    not worth a support burden on someone else's PC.
#
# Playwright ships its own hooks through the ``pyinstaller40`` entry point,
# so its Node driver and JavaScript are collected automatically; there is no
# need to list them here.

import sys
from pathlib import Path

# ``SPECPATH`` is provided by PyInstaller.
PROJECT_ROOT = Path(SPECPATH).parent  # noqa: F821
sys.path.insert(0, str(PROJECT_ROOT))

from app.branding import BRAND  # noqa: E402
from app.version import VERSION  # noqa: E402

BUILD_DIR = PROJECT_ROOT / "build"
ICON_FILE = BUILD_DIR / "app.ico"
VERSION_FILE = BUILD_DIR / "version_info.txt"

# Qt modules that are pulled in transitively but never used. Excluding a
# module also prevents its PyInstaller hook running, which is what stops its
# DLLs, plugins and translations being collected.
QT_EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtWebView",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtSerialPort",
    "PySide6.QtSerialBus",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSensors",
    "PySide6.QtTextToSpeech",
    "PySide6.QtVirtualKeyboard",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtHttpServer",
    "PySide6.QtDesigner",
    "PySide6.QtUiTools",
    "PySide6.QtTest",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtConcurrent",
    "PySide6.QtDBus",
    "PySide6.QtHelp",
    "PySide6.QtSql",
    "PySide6.QtXml",
    "PySide6.QtAsyncio",
]

STDLIB_EXCLUDES = [
    "tkinter",
    "unittest",
    "pydoc_data",
    "test",
    "distutils",
    "lib2to3",
    "idlelib",
    "ensurepip",
    "setuptools",
    "pip",
    "numpy",
    "matplotlib",
    "PIL",
    "pytest",
    "_pytest",
]

analysis = Analysis(  # noqa: F821
    [str(PROJECT_ROOT / "app" / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[],
    # Imported lazily inside functions, so PyInstaller cannot see them by
    # static analysis -- but Playwright's hook only fires if the module is
    # known, so both API surfaces are declared.
    hiddenimports=[
        "playwright.sync_api",
        "playwright.async_api",
        "windows_toasts",
        "winrt.windows.ui.notifications",
        "winrt.windows.data.xml.dom",
        "app.database.migrations.m0001_initial",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=QT_EXCLUDES + STDLIB_EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)  # noqa: F821

executable = EXE(  # noqa: F821
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=BRAND.exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON_FILE) if ICON_FILE.exists() else None,
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
)

# A second, console-attached executable from the same analysis. Support can
# ask a user to run this to see start-up output, which a windowed build
# swallows entirely.
debug_executable = EXE(  # noqa: F821
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name=f"{BRAND.exe_name}-debug",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ICON_FILE) if ICON_FILE.exists() else None,
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
)

collection = COLLECT(  # noqa: F821
    executable,
    debug_executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=BRAND.exe_name,
)

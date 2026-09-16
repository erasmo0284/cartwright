# Building

## Development setup

Python 3.14 is what this was built and tested on. 3.12 or 3.13 should also
work; nothing in the code needs 3.14 specifically.

```bash
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe scripts\install_browser.py
```

Run from source:

```bash
.venv\Scripts\python.exe -m app.main
```

Run the tests:

```bash
.venv\Scripts\python.exe -m pytest tests
```

Capture every screen and dialog as PNGs in `build/screens/`, in both light and
dark themes, for visual review:

```bash
.venv\Scripts\python.exe scripts\capture_screens.py
```

## Building the application

```bash
.venv\Scripts\python.exe scripts\build.py --clean
```

Output: `dist/AmazonPurchaseBot/` — about 213 MB, containing
`AmazonPurchaseBot.exe` (windowed) and `AmazonPurchaseBot-debug.exe`
(console).

The script generates the multi-resolution `.ico` and the Windows version
resource first, then runs PyInstaller, then reports the size and the eight
largest files, then runs the built executable's `--version` as a smoke test.

## Building the installer

Needs the Inno Setup compiler:

```bash
winget install -e --id JRSoftware.InnoSetup
```

Then:

```bash
.venv\Scripts\python.exe scripts\build.py --installer
```

Output: `installer/output/AmazonPurchaseBot-1.0.0-Setup.exe` — about 55 MB.

`scripts/build.py` looks for `ISCC.exe` in the per-user location that
`winget` uses (`%LOCALAPPDATA%\Programs\Inno Setup 6\`) as well as the two
Program Files locations, so it works whether Inno was installed with or
without administrator rights.

### Testing the installer

```powershell
# install, unattended
.\installer\output\AmazonPurchaseBot-1.0.0-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART

# uninstall, unattended (keeps the user's data)
& "$env:LOCALAPPDATA\Programs\AmazonPurchaseBot\unins000.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

## The three packaging decisions, and why

### onedir, not onefile

A onefile build re-extracts its whole archive to `%TEMP%` on **every**
launch. With Qt plus Playwright's Node driver that is hundreds of megabytes
of disk churn per start, it invites an antivirus scan each time, and its
bootloader cleanup is fragile if the process is killed. Since the app ships
with an installer anyway, the single-file property buys nothing.

### Chromium is not bundled

It is ~430 MB on disk. Bundling it would mean re-shipping the browser with
every application update, and in onefile mode re-extracting it on every
launch.

Instead `PLAYWRIGHT_BROWSERS_PATH` is set to
`%LOCALAPPDATA%\AmazonPurchaseBot\browser\playwright` **before Playwright is
imported** (the value is read by the Node driver process it spawns, and must
match between install time and run time), and the browser is installed on
first run.

`--no-shell` is passed to the installer, which skips
`chromium-headless-shell` — another 270 MB that a headed browser can never
use.

`scripts/build.py` hard-fails if a browser has been installed *inside* the
`playwright` package (which is what `PLAYWRIGHT_BROWSERS_PATH=0` does),
because Playwright's own PyInstaller hook would sweep it into the bundle
without saying anything.

> A consequence worth knowing when writing tests: with the headless shell
> absent, `chromium.launch(headless=True)` fails with "Executable doesn't
> exist". Use `channel="chromium"` to get Chrome's newer headless mode on the
> full build — which is what `tests/integration/conftest.py` does.

### PySide6-Essentials, not PySide6

`PySide6` is a meta-package that also pulls `PySide6-Addons`: QtWebEngine,
Qt3D, QtCharts, QtMultimedia and more, roughly 400 MB installed and none of
it used. Playwright is the browser here, so QtWebEngine in particular would
be pure weight.

`installer/app.spec` additionally excludes ~40 Qt modules by name. Excluding
a module also prevents its PyInstaller hook from running, which is what stops
its DLLs, plugins and translations being collected.

### No UPX

Packing is the biggest single cause of antivirus false positives and has a
history of corrupting Qt DLLs. The 20–30% it saves is not worth the support
burden on someone else's PC.

## What ends up in the build

| Item | Size | Note |
|---|---|---|
| `playwright/driver/node.exe` | 89 MB | Playwright's driver. Not removable. |
| `PySide6/opengl32sw.dll` | 20 MB | Software OpenGL. **Kept deliberately** — it is the fallback on machines with no GPU driver, in VMs and over Remote Desktop, which is exactly where a friend's PC might land. |
| Qt6Core / Qt6Gui / Qt6Widgets | 25 MB | |
| `python314.dll` | 6.5 MB | |
| Everything else | ~70 MB | Qt plugins, translations, winrt bindings |
| **Total** | **213 MB** | 55 MB compressed in the installer |

## Code signing

The build is unsigned, so SmartScreen warns on first run. To sign, set
`SignTool` in `installer/setup.iss` and add a `codesign` step after
PyInstaller. An OV certificate builds reputation over time; an EV certificate
gets immediate SmartScreen trust.

The embedded version resource (publisher, product name, version,
description) is there partly to help reputation on an unsigned binary.

## This build

The artefacts that accompany this documentation were produced on
2026-09-16 with:

| | |
|---|---|
| Version | 1.0.0 (`release`) |
| Python | 3.14.6 (64-bit) |
| PySide6-Essentials | 6.11.2 (Qt 6.11.2) |
| Playwright | 1.63.0 (Chromium build 1243) |
| PyInstaller | 6.22.3 |
| Inno Setup | 6.7.3 |
| Host | Windows 11 Pro 26200 |
| `dist\AmazonPurchaseBot\` | 213 MB, onedir, no UPX |
| `AmazonPurchaseBot-1.0.0-Setup.exe` | 57,918,659 bytes (55 MB) |
| Installer SHA-256 | `0e5e6a29bfa4949d68ac6ec68bbe0ca313cdd15d4765179bda808d9ad4519acf` |
| Installed size | 217 MB in `%LOCALAPPDATA%\Programs\AmazonPurchaseBot` |

The browser is **not** in either artefact: it is downloaded on first run into
the user's own data directory, which is why the installer is 55 MB rather
than half a gigabyte.

## Repeatable builds

- `app/version.py` is the single source of the version. `scripts/build.py`
  reads it, and `setup.iss` receives it as `/DMyAppVersion`.
- `requirements.txt` pins every runtime dependency exactly, including the
  Playwright version, which determines the Chromium build the app downloads.
- `installer/setup.iss` has a fixed `AppId`. **Never change it** — it is what
  makes an install an upgrade rather than a second copy side by side.

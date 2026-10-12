# Raven tech stack

Everything Raven is built with, why it was chosen, and what that means for running and
distributing the app. For how the pieces fit together see
[ARCHITECTURE.md](ARCHITECTURE.md).

## At a glance

| Layer | Technology |
|---|---|
| Language | Python ≥ 3.10 (CI on 3.12; also tested on 3.13) |
| Desktop UI | Qt 6 through **PySide6** (Essentials), Fusion style + generated stylesheet, inline SVG icons |
| Networking | **asyncio** + **websockets** over TLS (`ssl` from the standard library) |
| Cryptography | **cryptography** (certificates), `hashlib`/`hmac` (PBKDF2-SHA256, HMAC-SHA256, SHA-256) |
| Screen capture | **mss** → **Pillow** (resize, JPEG) |
| Input injection | **pynput** |
| Discovery | **zeroconf** (mDNS/Bonjour) |
| System info | **psutil** (interfaces, MAC addresses, netmasks) |
| OS integration | `ctypes` (Win32), PyObjC (macOS, via pynput), command-line tools |
| Packaging | **PyInstaller** (Windows `.exe` folder, macOS `.app`) |
| Tests | **pytest** + **pytest-asyncio**, Xvfb / offscreen Qt |
| CI | GitHub Actions (Ubuntu tests; Windows + macOS packaging) |

## Runtime dependencies

The minimum versions are declared in `pyproject.toml`. "Tested" is what CI and
development ran with in October 2026.

| Package | Minimum | Tested | Used for | Where | License |
|---|---|---|---|---|---|
| PySide6-Essentials | 6.6 | 6.12.0 | Widgets, painting, clipboard, SVG icons, system tray, signals across threads | `raven/gui/` | LGPL-3.0 / GPL-2.0 / GPL-3.0 |
| websockets | 13.0 | 17.2 | WebSocket server and client on asyncio (`websockets.asyncio`) | `host/server.py`, `client/session.py` | BSD-3-Clause |
| mss | 9.0 | 10.2.0 | Fast per-monitor screen capture (GDI on Windows, CoreGraphics on macOS, X11 on Linux) | `platform/capture.py` | MIT |
| pynput | 1.7.6 | 1.8.2 | Mouse and keyboard injection; cursor position | `platform/inputs.py` | LGPL-3.0 |
| Pillow | 10.0 | 12.3.0 | BGRX → RGB, downscaling, JPEG encoding | `platform/capture.py` | MIT-CMU (HPND) |
| cryptography | 41.0 | 50.0.2 | EC P-256 key and self-signed X.509 certificate generation | `security.py` | Apache-2.0 or BSD-3-Clause |
| zeroconf | 0.131 | 0.151.5 | mDNS advertising (AsyncZeroconf) and browsing | `discovery.py` | LGPL-2.1-or-later |
| psutil | 5.9 | 7.2.2 | Interface addresses, MAC for Wake-on-LAN, subnet broadcast | `platform/netinfo.py` | BSD-3-Clause |

**Transitive dependencies worth knowing:**

- **macOS:** `pyobjc-framework-Quartz` and `pyobjc-framework-ApplicationServices` (from
  pynput), which bring `pyobjc-framework-Cocoa`. Raven uses them directly for:
  - `CGPreflightScreenCaptureAccess` / `CGRequestScreenCaptureAccess`;
  - `AXIsProcessTrusted` / `AXIsProcessTrustedWithOptions`;
  - `NSPasteboard.changeCount`.
- **Linux:** `python-xlib` and `evdev` (from pynput); `ifaddr` (from zeroconf).
- `shiboken6` (from PySide6).

**Standard library highlights:**

| Module | Used for |
|---|---|
| `asyncio` | Network event loop |
| `ssl` | TLS contexts; minimum TLS 1.2 |
| `hashlib`, `hmac`, `secrets` | PBKDF2, HMAC, SHA-256, nonces, salts, temp-file names |
| `ipaddress` | LAN address filter |
| `concurrent.futures` | Capture executors; the GUI ↔ loop bridge |
| `json`, `struct`, `base64` | Wire encoding |
| `plistlib` | macOS LaunchAgent |
| `winreg` | Windows Run key |
| `subprocess` | Power and clipboard tools |

## Development dependencies

`pip install -e ".[dev]"`

| Package | Minimum | Tested | Used for | License |
|---|---|---|---|---|
| pytest | 8 | 9.1.1 | Test runner (`asyncio_mode = auto`) | MIT |
| pytest-asyncio | 0.23 | 1.4.0 | Async tests and fixtures | Apache-2.0 |
| PyInstaller | 6.0 | 6.22.3 | Building `Raven.exe` / `Raven.app` (`packaging/raven.spec`) | GPL-2.0-or-later with bootloader exception |

**Linux test hosts** also need the system packages `xvfb`, `libegl1`,
`libxkbcommon-x11-0` and `libxcb-cursor0` (installed in CI). Run the suite with
`xvfb-run -a pytest`. Qt-only tests use `QT_QPA_PLATFORM=offscreen`.

## Operating-system integration

| Capability | Windows | macOS | Linux (development) |
|---|---|---|---|
| Screen capture | mss (GDI BitBlt); per-monitor DPI-aware process | mss (CoreGraphics); needs **Screen Recording** permission | mss (X11) |
| Input | pynput (`SendInput`) | pynput (Quartz events); needs **Accessibility** permission | pynput (X11) |
| Sleep | `powrprof.SetSuspendState` (ctypes) | `pmset sleepnow` | `systemctl suspend` |
| Lock | `user32.LockWorkStation` (ctypes) | `pmset displaysleepnow` | `loginctl lock-session` |
| Restart / shut down | `shutdown /r` / `shutdown /s` | AppleScript via System Events (needs Automation consent), fallback `sudo -n shutdown` | `systemctl reboot` / `poweroff` |
| Clipboard (GUI) | Qt `QClipboard` + `GetClipboardSequenceNumber` | Qt `QClipboard` + `NSPasteboard.changeCount` | Qt `QClipboard` |
| Clipboard (headless) | PowerShell `Get-Clipboard` / `Set-Clipboard` | `pbpaste` / `pbcopy` | `wl-paste`/`wl-copy`, `xclip` or `xsel` |
| Start at login | `HKCU\…\CurrentVersion\Run` | LaunchAgent plist | — |
| Permission prompts | Firewall prompt on first listen | `CGRequestScreenCaptureAccess`, `AXIsProcessTrustedWithOptions`, and deep links to System Settings | — |
| Config directory | `%APPDATA%\Raven` | `~/Library/Application Support/Raven` | `~/.config/raven` |

The `Info.plist` of the macOS bundle declares `NSAppleEventsUsageDescription`,
`NSLocalNetworkUsageDescription` and `NSBonjourServices` (`_raven._tcp`).

## Why these choices

- **Python + PySide6.**
  - One codebase covers both the host and the controller on every OS.
  - Qt gives a native-feeling, themeable desktop UI with a system tray, a clipboard and
    cross-thread signals.
  - The Python ecosystem already has solid capture (mss), input (pynput) and mDNS
    (zeroconf) libraries, so no native code was needed.
  - Rejected alternatives:
    - **Electron:** native input modules (robotjs/nut.js) are fragile to build.
    - **Rust/Tauri:** more work for the same features.
    - **Go:** input libraries need cgo.
- **websockets over TLS instead of raw sockets.**
  - Message framing, keepalive pings and back-pressure come for free, and TLS comes from
    the standard library.
  - JSON control messages are easy to inspect and extend.
  - WebRTC was rejected: it needs signalling and ICE/STUN, which a LAN-only app doesn't.
- **JPEG frames with change detection instead of a video codec.**
  - It needs no codec dependencies and stays readable at any quality level.
  - It costs little on a LAN, and a static screen sends nothing.
  - A hardware H.264 path could lower bandwidth later (see Future work in the
    architecture doc).
- **mss over Pillow `ImageGrab` or Qt screen grabs.** It is faster, works per monitor,
  and runs off the GUI thread, which the headless host needs.
- **pynput.** One API for Windows, macOS and X11 injection, with named special keys that
  map directly onto Raven's portable key names.
- **The cryptography library** generates certificates only. All proofs use the standard
  library, so the security-critical code is short and auditable (`raven/security.py`).
- **zeroconf.** A pure-Python mDNS implementation with an asyncio API that interoperates
  with Apple's Bonjour.
- **PyInstaller.** Produces the most reliable PySide6 bundles on Windows and macOS. The
  one-folder layout keeps Qt as separate shared libraries (see Licensing).

## Licensing notes for distributors

Raven's own code has no license file yet. Choose one before publishing builds. The
dependencies allow distributing a closed or open app, with these obligations:

- **LGPL components** (PySide6/Qt, pynput, zeroconf, python-xlib):
  - Ship them as replaceable libraries. PyInstaller's one-folder bundle keeps the Qt and
    Python packages as separate files, which satisfies this.
  - Include their license texts, and offer the source of any modifications (Raven makes
    none).
- **PyInstaller** is GPL, but its bootloader exception lets you distribute the bundles
  under any license.
- **BSD/MIT/Apache components** need their copyright notices included with
  distributions.

This is a summary, not legal advice. Check each package's license file if you plan to
sell or redistribute builds.

## Upgrading dependencies

- **Minimum versions** in `pyproject.toml` are those whose APIs Raven relies on:
  - `websockets.asyncio`, introduced in websockets 13;
  - mss `MSS`/`mss()` (both are supported);
  - Qt 6.5+ `QStyleHints.colorScheme` for following the OS theme (used only when present).
- **After upgrading**, run the full suite (`xvfb-run -a pytest`); `test_attacks.py`
  exercises the server's error handling broadly.
- **Watch for:**
  - websockets changing `ServerConnection.protocol.max_message_size`, which the server
    raises after login;
  - mss changing monitor units (points vs pixels on Retina);
  - PySide6 changing `Qt.convertFromPlainText` or `QTimer.singleShot` overloads.
- **CI actions** (`actions/checkout@v4`, `actions/setup-python@v5`,
  `actions/upload-artifact@v4`) currently log a GitHub warning that they target
  Node.js 20. Move to their newer major versions when convenient.

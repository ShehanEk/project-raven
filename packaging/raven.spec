# PyInstaller spec: `pyinstaller packaging/raven.spec` from the repo root.
# Produces dist/Raven.exe (Windows, one folder) or dist/Raven.app (macOS).
import sys
from PyInstaller.utils.hooks import collect_submodules

hidden = collect_submodules("zeroconf") + collect_submodules("pynput")

a = Analysis(
    ["../raven/__main__.py"],
    pathex=[".."],
    hiddenimports=hidden,
    excludes=["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Raven",
    console=False,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Raven")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Raven.app",
        bundle_identifier="com.raven.remote",
        info_plist={
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
            # Shown when Raven asks System Events to shut down / restart.
            "NSAppleEventsUsageDescription": "Raven uses System Events to shut down or restart this Mac when you ask it to from another computer.",
            "NSLocalNetworkUsageDescription": "Raven connects to your other computers on the local network.",
            "NSBonjourServices": ["_raven._tcp"],
        },
    )

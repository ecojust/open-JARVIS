# Native onedir build. Execute through scripts/build_release.py.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

root = Path(SPECPATH).resolve().parent
version = os.environ.get("JARVIS_BUILD_VERSION", "0.1.0")
numeric_version = version.split("-", 1)[0].split("+", 1)[0]
icon_dir = Path(os.environ["JARVIS_BUILD_ICON_DIR"])
signing_identity = os.environ.get("JARVIS_MACOS_SIGN_IDENTITY") or None
entitlements = str(root / "packaging" / "macos" / "entitlements.plist") if signing_identity else None

datas = [
    (str(root / "assets" / "branding"), "assets/branding"),
    (str(root / "examples"), "examples"),
    (str(root / "static"), "static"),
    (str(root / "LICENSE"), "."),
    (str(icon_dir / "third-party-licenses"), "third-party-licenses"),
]
binaries = []
hiddenimports = [
    "uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl",
    "uvicorn.lifespan.on", "sounddevice",
    "_sounddevice", "webrtcvad", "_webrtcvad", "numpy", "httpx", "openai",
]
for package in ("sherpa_onnx",):
    package_data, package_binaries, package_imports = collect_all(package)
    datas += package_data
    binaries += package_binaries
    hiddenimports += package_imports
# The local sounddevice hook selects only the target architecture's PortAudio.
hiddenimports += ["_sounddevice_data"]
hiddenimports += collect_submodules("agent_framework")
hiddenimports += collect_submodules("jarvis")
for distribution in ("agent-framework-core", "agent-framework-openai", "openai", "sherpa-onnx", "sherpa-onnx-core", "PySide6-Essentials", "shiboken6"):
    datas += copy_metadata(distribution)

# GUI is intentionally native Qt; avoid the Chromium runtime and model caches.
excluded = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore",
    "tkinter", "matplotlib", "torch", "tensorflow", "pytest", "IPython",
]
a = Analysis(
    [str(root / "packaging" / "entrypoint.py")],
    pathex=[str(root)], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[str(root / "packaging" / "hooks")], hooksconfig={},
    runtime_hooks=[], excludes=excluded, noarchive=False,
)
pyz = PYZ(a.pure)
icon = str(icon_dir / ("open-jarvis.icns" if sys.platform == "darwin" else "open-jarvis.ico"))
common = dict(
    exclude_binaries=True, debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, icon=icon, target_arch=None,
    codesign_identity=signing_identity, entitlements_file=entitlements,
)
gui = EXE(pyz, a.scripts, [], name="open-JARVIS", console=False, **common)
# Separate console subsystem retains redirected stdio for JSON-lines IPC.
worker = EXE(pyz, a.scripts, [], name="open-JARVIS-worker", console=True, **common)
coll = COLLECT(gui, worker, a.binaries, a.datas, strip=False, upx=False, name="open-JARVIS")
if sys.platform == "darwin":
    app = BUNDLE(
        coll, name="Open JARVIS.app", icon=icon,
        bundle_identifier="com.ecojust.openjarvis",
        info_plist={
            "CFBundleName": "Open JARVIS", "CFBundleDisplayName": "Open JARVIS",
            "CFBundleShortVersionString": numeric_version, "CFBundleVersion": numeric_version,
            "NSMicrophoneUsageDescription": "JARVIS 使用麦克风在本机识别你的语音指令。",
            "NSHighResolutionCapable": True,
            # COLLECT also contains the console worker; the app itself is GUI.
            "LSBackgroundOnly": False,
        },
        codesign_identity=signing_identity, entitlements_file=entitlements,
    )

"""Single entry point: a native desktop app by default, explicit worker roles."""

import argparse
import importlib.metadata
import importlib.util
import importlib
import json
import multiprocessing
import os
import platform
import sys


def self_test() -> int:
    from .paths import asset_path, model_dir

    assets = {
        name: asset_path(name).is_file()
        for name in (
            "assets/branding/open-jarvis-sheep.png",
            "examples/example-sales.csv",
            "static/index.html",
        )
    }
    modules = {}
    for name in (
        "PySide6",
        "sherpa_onnx",
        "sounddevice",
        "webrtcvad",
        "numpy",
        "agent_framework",
        "fastapi",
    ):
        try:
            modules[name] = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            modules[name] = False
    versions = {}
    for name in (
        "PySide6-Essentials",
        "sherpa-onnx",
        "agent-framework-core",
        "agent-framework-openai",
    ):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    native_imports = {}
    for name in (
        "PySide6.QtWidgets",
        "sherpa_onnx",
        "sounddevice",
        "webrtcvad",
        "numpy",
        "agent_framework.openai",
    ):
        try:
            importlib.import_module(name)
            native_imports[name] = {"ok": True}
        except Exception as exc:
            native_imports[name] = {"ok": False, "error_type": type(exc).__name__}
    result = {
        "ok": all(assets.values())
        and all(modules.values())
        and all(item["ok"] for item in native_imports.values()),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "assets": assets,
        "modules": modules,
        "versions": versions,
        "native_imports": native_imports,
        "models": {name: model_dir(name).is_dir() for name in ("sensevoice", "kokoro")},
        "hardware_tested": False,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open JARVIS 桌面语音工作助理")
    parser.add_argument(
        "--role",
        choices=(
            "desktop",
            "backend",
            "audio",
            "download-stt",
            "download-tts",
            "self-test",
        ),
        default="desktop",
    )
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--archive", help="使用已下载的 Kokoro 模型压缩包")
    arguments = parser.parse_args(argv)
    from .paths import load_environment, model_dir

    load_environment()
    if arguments.role == "self-test":
        return self_test()
    if arguments.role == "desktop":
        from .desktop import main as desktop_main

        return desktop_main()
    if arguments.role == "backend":
        import uvicorn
        from .app import create_app
        from .config import Settings

        port = arguments.port or int(os.getenv("JARVIS_DESKTOP_PORT", "8765"))
        if not 1 <= port <= 65535:
            parser.error("端口必须在 1–65535 范围内")
        uvicorn.run(
            create_app(Settings.from_env()),
            host="127.0.0.1",
            port=port,
            log_level="info",
        )
        return 0
    if arguments.role == "audio":
        from .audio_process import main as audio_main

        return audio_main() or 0
    if arguments.role == "download-stt":
        from .download_model import download

        download(model_dir("sensevoice"))
        return 0
    if arguments.role == "download-tts":
        from pathlib import Path
        from .download_tts_model import download

        download(
            model_dir("kokoro"), Path(arguments.archive) if arguments.archive else None
        )
        return 0
    return 2


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1)

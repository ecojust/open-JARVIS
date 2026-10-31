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


def _office_roundtrip_checks() -> dict:
    """Exercise real packaged templates without touching the user workspace."""
    from pathlib import Path
    from tempfile import TemporaryDirectory

    checks = {}
    sentinel = "Open JARVIS 自检"
    with TemporaryDirectory(prefix="jarvis-office-self-test-") as temporary:
        for extension in ("docx", "xlsx", "pptx"):
            path = Path(temporary) / f"roundtrip.{extension}"
            try:
                if extension == "docx":
                    from docx import Document

                    document = Document()
                    document.add_paragraph(sentinel)
                    document.save(path)
                    reopened = Document(path)
                    valid = reopened.paragraphs[0].text == sentinel
                elif extension == "xlsx":
                    from openpyxl import Workbook, load_workbook

                    workbook = Workbook()
                    workbook.active["A1"] = sentinel
                    workbook.save(path)
                    workbook.close()
                    reopened = load_workbook(path, read_only=True)
                    try:
                        valid = reopened.active["A1"].value == sentinel
                    finally:
                        reopened.close()
                else:
                    from pptx import Presentation

                    presentation = Presentation()
                    slide = presentation.slides.add_slide(presentation.slide_layouts[0])
                    slide.shapes.title.text = sentinel
                    presentation.save(path)
                    reopened = Presentation(path)
                    valid = reopened.slides[0].shapes.title.text == sentinel
                if not valid:
                    raise ValueError("Office roundtrip content did not match.")
                checks[extension] = {
                    "ok": True,
                    "reopened": True,
                    "size_bytes": path.stat().st_size,
                }
            except Exception as exc:
                checks[extension] = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:500],
                }
    return checks


def _ffmpeg_self_test() -> dict:
    """Run the bundled native executable with a fixed, bounded command."""
    import subprocess
    from pathlib import Path

    try:
        from imageio_ffmpeg._definitions import FNAME_PER_PLATFORM, get_platform
        from imageio_ffmpeg._utils import _get_bin_dir

        filename = FNAME_PER_PLATFORM.get(get_platform())
        if not filename:
            raise ValueError("No bundled FFmpeg is defined for this platform.")
        executable = Path(_get_bin_dir()) / filename
        if not executable.is_file():
            raise FileNotFoundError("Bundled FFmpeg resource is missing.")
        completed = subprocess.run(
            [str(executable), "-nostdin", "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first_line = (completed.stdout or "").splitlines()
        version_line = first_line[0][:500] if first_line else ""
        if completed.returncode != 0 or not version_line.startswith("ffmpeg version "):
            raise ValueError(
                f"Bundled FFmpeg failed (status {completed.returncode}): {version_line}"
            )
        return {
            "ok": True,
            "bundled": True,
            "executable": filename,
            "version_line": version_line,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:500],
        }


def _font_self_test() -> dict:
    """Verify the shipped font source and draw distinct Chinese glyphs."""
    import hashlib

    try:
        from PIL import Image, ImageDraw, ImageFont
        from .paths import asset_path

        resource = "assets/fonts/NotoSansSC-Regular.otf"
        font_path = asset_path(resource)
        metadata = json.loads(asset_path("assets/fonts/source.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256()
        with font_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != metadata["sha256"]:
            raise ValueError("Bundled font differs from its recorded source checksum.")
        license_text = asset_path("assets/fonts/OFL.txt").read_text(encoding="utf-8")
        if "SIL OPEN FONT LICENSE" not in license_text:
            raise ValueError("Bundled font license is missing or incomplete.")
        font = ImageFont.truetype(str(font_path), 24)
        if len({bytes(font.getmask(character)) for character in "中文\ufffd"}) != 3:
            raise ValueError("Bundled font did not render distinct Chinese glyphs.")
        rendered = Image.new("L", (256, 64), 0)
        ImageDraw.Draw(rendered).text((0, 0), "中文输出", fill=255, font=font)
        if rendered.getbbox() is None:
            raise ValueError("Bundled font produced an empty image.")
        return {
            "ok": True,
            "resource": resource,
            "sha256": digest.hexdigest(),
            "family": font.getname()[0],
            "glyphs_checked": "中文",
            "license": metadata["license"],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:500],
        }


def self_test() -> int:
    from .paths import asset_path, model_dir

    assets = {
        name: asset_path(name).is_file()
        for name in (
            "assets/branding/open-jarvis-sheep.png",
            "examples/example-sales.csv",
            "static/index.html",
            "assets/fonts/NotoSansSC-Regular.otf",
            "assets/fonts/OFL.txt",
            "assets/fonts/source.json",
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
        "docx",
        "openpyxl",
        "pptx",
        "imageio_ffmpeg",
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
        "python-docx",
        "openpyxl",
        "python-pptx",
        "Pillow",
        "imageio-ffmpeg",
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
            native_imports[name] = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            }
    office_roundtrip = _office_roundtrip_checks()
    ffmpeg = _ffmpeg_self_test()
    font_render = _font_self_test()
    result = {
        "ok": all(assets.values())
        and all(modules.values())
        and all(item["ok"] for item in native_imports.values())
        and all(item["ok"] for item in office_roundtrip.values())
        and ffmpeg["ok"]
        and font_render["ok"],
        "platform": sys.platform,
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "assets": assets,
        "modules": modules,
        "versions": versions,
        "native_imports": native_imports,
        "office_roundtrip": office_roundtrip,
        "ffmpeg": ffmpeg,
        "font_render": font_render,
        "models": {name: model_dir(name).is_dir() for name in ("sensevoice", "kokoro")},
        "hardware_tested": False,
    }
    # Keep redirected self-test output valid on Windows legacy code pages too.
    print(json.dumps(result, ensure_ascii=True))
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

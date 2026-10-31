"""Reproducible development measurements, never a substitute for native audio QA.

python scripts/measure_runtime.py --samples 30 --output artifacts/performance.json
python scripts/measure_runtime.py --samples 30 --voice --output artifacts/performance.json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def probe_widget():
    import psutil
    from PySide6.QtWidgets import QApplication
    from jarvis.desktop_client import DesktopController

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    controller = DesktopController(app, autostart=False)
    app.processEvents()
    result = {
        "startup_to_processed_events_ms": (
            time.perf_counter_ns() - int(os.environ["JARVIS_MEASURE_START_NS"])
        )
        / 1_000_000,
        "rss_mib": psutil.Process().memory_info().rss / 1024**2,
        "tile_visible": controller.tile.isVisible(),
        "result_window_created": controller.results is not None,
        "webengine_loaded": any("WebEngine" in name for name in sys.modules),
        "scope": "widget_only_no_backend_or_speech_models",
    }
    controller.cleanup()
    return result


def probe_voice():
    import psutil
    import numpy as np
    from jarvis.paths import load_environment, model_dir
    from jarvis.tts_runtime import build_tts_engine, synthesize

    load_environment()
    start = time.perf_counter()
    engine = build_tts_engine()
    load_ms = (time.perf_counter() - start) * 1000
    samples = []
    audio = None
    for _ in range(3):
        start = time.perf_counter()
        audio = synthesize(engine, "你好，我是贾维斯。")
        values = np.asarray(audio.samples)
        samples.append(
            {
                "synthesis_ms": (time.perf_counter() - start) * 1000,
                "audio_seconds": len(values) / audio.sample_rate,
                "finite_samples": bool(np.isfinite(values).all()),
            }
        )
    import sherpa_onnx

    directory = model_dir("sensevoice")
    start = time.perf_counter()
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(directory / "model.int8.onnx"),
        tokens=str(directory / "tokens.txt"),
        language="zh",
        use_itn=True,
        num_threads=2,
        provider="cpu",
    )
    stt_load_ms = (time.perf_counter() - start) * 1000
    stream = recognizer.create_stream()
    stream.accept_waveform(
        audio.sample_rate, np.asarray(audio.samples, dtype=np.float32)
    )
    start = time.perf_counter()
    recognizer.decode_stream(stream)
    return {
        "scope": "real_models_synthesized_audio_no_microphone_or_speaker",
        "tts_model_load_ms": load_ms,
        "tts_samples": samples,
        "stt_model_load_ms": stt_load_ms,
        "stt_decode_ms": (time.perf_counter() - start) * 1000,
        "stt_text": stream.result.text,
        "combined_process_rss_mib": psutil.Process().memory_info().rss / 1024**2,
        "actual_audio_output_tested": False,
    }


def summary(values):
    ordered = sorted(values)
    return {
        "count": len(values),
        "p50": ordered[(len(values) - 1) // 2],
        "p95": ordered[max(0, math.ceil(len(values) * 0.95) - 1)],
        "max": ordered[-1],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument(
        "--voice",
        action="store_true",
        help="Also run real installed STT/TTS models, without audio hardware",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--probe", choices=("widget", "voice"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe:
        print(
            json.dumps(
                probe_widget() if args.probe == "widget" else probe_voice(),
                ensure_ascii=False,
            )
        )
        return
    if not 1 <= args.samples <= 100:
        parser.error("--samples must be 1–100")
    import psutil

    result = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "logical_cpus": os.cpu_count(),
        "machine_memory_gib": round(psutil.virtual_memory().total / 1024**3, 2),
        "qt_platform": "offscreen",
        "release_acceptance": False,
        "widget_samples": [],
    }
    with tempfile.TemporaryDirectory(prefix="jarvis-measure-") as directory:
        environment = dict(
            os.environ,
            QT_QPA_PLATFORM="offscreen",
            JARVIS_DATA_DIR=directory,
            JARVIS_CONFIG_FILE=str(Path(directory) / "settings.json"),
            PYTHONUTF8="1",
        )
        for _ in range(args.samples):
            environment["JARVIS_MEASURE_START_NS"] = str(time.perf_counter_ns())
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--probe", "widget"],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
            )
            if completed.returncode:
                raise RuntimeError("Widget probe failed: " + completed.stderr[-1500:])
            result["widget_samples"].append(
                json.loads(completed.stdout.strip().splitlines()[-1])
            )
    result["widget_startup_ms"] = summary(
        [row["startup_to_processed_events_ms"] for row in result["widget_samples"]]
    )
    result["widget_rss_mib"] = summary(
        [row["rss_mib"] for row in result["widget_samples"]]
    )
    if args.voice:
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--probe", "voice"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        result["voice"] = (
            json.loads(completed.stdout.strip().splitlines()[-1])
            if completed.returncode == 0
            else {"error": completed.stderr[-1500:]}
        )
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()

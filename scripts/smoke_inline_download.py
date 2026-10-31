"""Exercise inline model progress/ETA through real Qt, QProcess and local HTTP.

Developer validation only. The tiny downloaded files are explicitly labelled
test fixtures, not usable speech weights; they live in a temporary directory.
No existing model, microphone, speaker or external model service is used.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def worker(config_path):
    """Only this test worker substitutes the pinned files and source endpoints."""
    from jarvis import download_model
    from jarvis.model_download import DownloadSource

    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    download_model.MODEL_FILES = {
        name: (value["size"], value["sha256"])
        for name, value in config["files"].items()
    }
    download_model.MODEL_SOURCES = (
        DownloadSource("本机 HTTP 503 测试源", config["base"] + "/blocked"),
        DownloadSource("本机限速 HTTP 测试源", config["base"] + "/slow"),
    )
    download_model.download(Path(config["directory"]))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts/inline-settings-check"
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QProcess
    from PySide6.QtWidgets import QApplication, QDialog, QLabel
    from jarvis.desktop_client import DesktopController
    from jarvis.model_catalog import verify_installed

    body = {
        "tokens.txt": b"Explicit HTTP download fixture, not speech tokens.\n",
        "model.int8.onnx": b"not-an-inference-model\0" * 150_000,
    }
    requests = []
    events = []
    stopped = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append(self.path)
            if self.path.startswith("/blocked/"):
                self.send_response(503)
                self.end_headers()
                return
            name = self.path.removeprefix("/slow/")
            if name not in body:
                self.send_error(404)
                return
            value = body[name]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            try:
                for offset in range(0, len(value), 64 * 1024):
                    if stopped.is_set():
                        return
                    self.wfile.write(value[offset : offset + 64 * 1024])
                    self.wfile.flush()
                    if name.endswith(".onnx"):
                        stopped.wait(0.125)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    controller = None
    report = {
        "scope": "actual Qt / QProcess / localhost HTTP; synthetic pinned files",
        "production_model_weights_downloaded": False,
        "speech_inference_started": False,
        "existing_models_modified": False,
        "checks": {},
    }
    try:
        with tempfile.TemporaryDirectory(prefix="jarvis-inline-smoke-") as tmp:
            directory = Path(tmp)
            fixture_model = directory / "fixture-stt"
            config = directory / "worker.json"
            config.write_text(
                json.dumps(
                    {
                        "base": f"http://127.0.0.1:{server.server_port}",
                        "directory": str(fixture_model),
                        "files": {
                            name: {
                                "size": len(value),
                                "sha256": hashlib.sha256(value).hexdigest(),
                            }
                            for name, value in body.items()
                        },
                    }
                ),
                encoding="utf-8",
            )

            class FixtureProcess(QProcess):
                def start(self, command, arguments):
                    if arguments[-1:] == ["download-stt"]:
                        return super().start(
                            sys.executable,
                            [str(Path(__file__).resolve()), "--worker", str(config)],
                        )
                    raise AssertionError(
                        "The smoke must never start inference or a backend"
                    )

            with (
                patch.dict(
                    os.environ,
                    {
                        "JARVIS_DATA_DIR": str(directory),
                        "JARVIS_CONFIG_FILE": str(directory / "settings.json"),
                        "JARVIS_STT_MODEL_DIR": str(fixture_model),
                        "JARVIS_TTS_MODEL_DIR": str(directory / "unused-tts"),
                        "UI_SPEECH_ENABLED": "false",
                        "OPENAI_API_KEY": "",
                        "OPENAI_MODEL": "",
                        "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
                        "NO_PROXY": "127.0.0.1,localhost",
                    },
                ),
                ExitStack() as cleanup,
            ):
                app = QApplication.instance() or QApplication([])
                app.setQuitOnLastWindowClosed(False)
                controller = DesktopController(
                    app, autostart=False, process_factory=FixtureProcess
                )
                cleanup.callback(controller.cleanup)
                # Prevent post-download service reload from loading fixture bytes.
                controller.tasks["explicit-smoke-hold"] = {"status": "running"}
                original_state = controller._set_model_state

                def record_state(model_kind, **values):
                    original_state(model_kind, **values)
                    if model_kind == "stt":
                        keep = (
                            "phase",
                            "source",
                            "source_index",
                            "source_count",
                            "completed_bytes",
                            "total_bytes",
                            "eta_seconds",
                            "bytes_per_second",
                        )
                        events.append(
                            {key: values[key] for key in keep if key in values}
                        )

                controller._set_model_state = record_state

                def until(predicate, timeout=45):
                    deadline = time.monotonic() + timeout
                    while not predicate():
                        app.processEvents()
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "Inline download did not reach the expected state"
                            )
                        time.sleep(0.01)
                    app.processEvents()

                def label_fixture(dialog):
                    dialog.setWindowTitle("JARVIS 设置 · 本机 HTTP 下载测试样例")
                    dialog.findChild(QLabel, "heading").setText(
                        "设置 · HTTP 下载测试样例"
                    )

                controller.show_settings()
                first = controller.settings_dialog
                label_fixture(first)
                first.fields["OPENAI_MODEL"].setText("未保存的测试输入")
                app.processEvents()
                assert not any(key.endswith("MODEL_DIR") for key in first.fields)
                first.model_rows["stt"].download_button.click()
                active_worker = controller._downloads["stt"]
                until(
                    lambda: (
                        controller._model_states["stt"].get("eta_seconds") is not None
                    )
                )
                assert first.fields["OPENAI_MODEL"].text() == "未保存的测试输入"
                assert [
                    w
                    for w in app.topLevelWidgets()
                    if isinstance(w, QDialog) and w.isVisible()
                ] == [first]
                report["checks"]["same_settings_no_extra_dialog"] = True
                report["checks"]["unsaved_form_preserved"] = True
                report["checks"]["no_model_import_fields"] = True
                first.reject()
                assert active_worker.state() == QProcess.ProcessState.Running
                controller.show_settings()
                dialog = controller.settings_dialog
                label_fixture(dialog)
                assert dialog is not first
                assert controller._downloads["stt"] is active_worker
                until(lambda: dialog.model_rows["stt"].progress.value() >= 350)
                row = dialog.model_rows["stt"]
                assert 0 < row.progress.value() < 1000
                assert not row.eta_label.isHidden()
                assert "预计剩余" in row.eta_label.text()
                report["visible_eta"] = row.eta_label.text()
                report["visible_source"] = row.source_label.text()
                report["checks"]["close_reopen_keeps_worker_progress_eta"] = True
                app.processEvents()
                dialog.grab().save(str(output / "01-inline-download-fixture.png"))
                until(lambda: not controller._downloads)
                assert controller._model_states["stt"]["phase"] == "ready"
                assert controller.pending_settings
                assert verify_installed("stt", fixture_model)
                assert row.eta_label.isHidden()
                assert row.download_button.isEnabled()
                assert [
                    w
                    for w in app.topLevelWidgets()
                    if isinstance(w, QDialog) and w.isVisible()
                ] == [dialog]
                report["checks"]["verified_fixture_installed_without_inference"] = True
                report["checks"]["ready_clears_eta"] = True
                report["checks"]["source_fallback_observed"] = any(
                    value.get("phase") == "switching" for value in events
                )
                report["checks"]["real_eta_events_observed"] = any(
                    isinstance(value.get("eta_seconds"), (int, float))
                    and value["eta_seconds"] > 0
                    for value in events
                )
                dialog.grab().save(str(output / "02-inline-ready-fixture.png"))
                controller.cleanup()
                app.processEvents()
                controller = None
    finally:
        if controller is not None:
            controller.cleanup()
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    report["requests"] = requests
    report["events"] = events
    report["passed"] = all(report["checks"].values())
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

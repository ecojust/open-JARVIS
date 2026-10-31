"""Real localhost HTTP transfers exercise fallback and transactional installs."""

import errno
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from jarvis import download_model as stt
from jarvis import download_tts_model as tts
from jarvis import model_catalog as catalog
from jarvis.model_download import DownloadSource, ModelDownloadError, download_verified


class DownloadFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.requests = []
        self.routes = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                route = outer.routes[self.path]
                if route.get("delay"):
                    time.sleep(route["delay"])
                body = route.get("body", b"")
                self.send_response(route.get("status", 200))
                self.send_header(
                    "Content-Type", route.get("type", "application/octet-stream")
                )
                if "length" in route:
                    self.send_header("Content-Length", str(route["length"]))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.body = b"pinned model fixture " * 5000
        self.events = []

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def source(self, path):
        return DownloadSource(path.strip("/"), self.base + path)

    def report(self, phase, **data):
        self.events.append({"phase": phase, **data})

    def transfer(self, *paths, request_hook=None, **kwargs):
        hooks = {"request": [request_hook]} if request_hook else None
        with httpx.Client(timeout=2, trust_env=False, event_hooks=hooks) as client:
            return download_verified(
                client,
                self.root / "model",
                tuple(self.source(path) for path in paths),
                len(self.body),
                hashlib.sha256(self.body).hexdigest(),
                self.report,
                **kwargs,
            )

    def test_http_failure_and_bad_hash_switch_to_identical_pinned_bytes(self):
        self.routes.update(
            {
                "/blocked": {"status": 503},
                "/corrupt": {"body": b"x" * len(self.body)},
                "/good": {"body": self.body, "length": len(self.body)},
                "/unused": {"body": self.body},
            }
        )
        selected = self.transfer("/blocked", "/corrupt", "/good", "/unused")
        self.assertEqual(selected.name, "good")
        self.assertEqual(self.requests, ["/blocked", "/corrupt", "/good"])
        self.assertEqual((self.root / "model").read_bytes(), self.body)
        switching = [e for e in self.events if e["phase"] == "switching"]
        self.assertEqual([e["source_index"] for e in switching], [2, 3])
        self.assertEqual(switching[-1]["previous_source"], "corrupt")
        self.assertEqual(switching[-1]["reason"], "SHA-256 校验失败")
        self.assertTrue(all(e["completed_bytes"] == 0 for e in switching))
        self.assertTrue(all(e["source_count"] == 4 for e in switching))

    def test_disconnected_partial_is_discarded_and_progress_resets_to_baseline(self):
        self.routes.update(
            {
                "/partial": {"body": b"broken data", "length": len(self.body)},
                "/good": {"body": self.body},
            }
        )
        self.transfer(
            "/partial", "/good", completed_bytes=42, total_bytes=42 + len(self.body)
        )
        switching = next(e for e in self.events if e["phase"] == "switching")
        self.assertEqual(switching["completed_bytes"], 42)
        self.assertEqual((self.root / "model").read_bytes(), self.body)
        self.assertEqual(self.events[-1]["completed_bytes"], 42 + len(self.body))

    def test_html_length_and_short_chunked_responses_fall_back(self):
        self.routes.update(
            {
                "/html": {"body": b"<html>login</html>", "type": "Text/HTML"},
                "/wrong-length": {"body": b"x", "length": 1},
                "/short": {"body": b"x"},
                "/good": {"body": self.body},
            }
        )
        self.transfer("/html", "/wrong-length", "/short", "/good")
        self.assertEqual(self.requests, ["/html", "/wrong-length", "/short", "/good"])
        self.assertEqual((self.root / "model").read_bytes(), self.body)

    def test_read_timeout_switches_without_unbounded_retries(self):
        self.routes.update(
            {
                "/timeout": {"delay": 0.25, "body": self.body},
                "/good": {"body": self.body},
            }
        )

        def shorten_only_failing_source(request):
            if request.url.path == "/timeout":
                request.extensions["timeout"] = httpx.Timeout(0.1).as_dict()

        self.transfer("/timeout", "/good", request_hook=shorten_only_failing_source)
        self.assertEqual(self.requests, ["/timeout", "/good"])
        self.assertEqual((self.root / "model").read_bytes(), self.body)
        self.assertIn(
            "超时", next(e["reason"] for e in self.events if e["phase"] == "switching")
        )

    def test_all_sources_fail_once_and_remove_partial_no_secrets_in_error(self):
        self.routes.update(
            {"/http?signature=secret": {"status": 403}, "/bad": {"body": b"bad"}}
        )
        with self.assertRaises(ModelDownloadError) as raised:
            with httpx.Client(timeout=1, trust_env=False) as client:
                download_verified(
                    client,
                    self.root / "model",
                    (
                        DownloadSource("Primary", self.base + "/http?signature=secret"),
                        self.source("/bad"),
                    ),
                    len(self.body),
                    hashlib.sha256(self.body).hexdigest(),
                    self.report,
                )
        self.assertEqual(len(self.requests), 2)
        self.assertFalse((self.root / "model").exists())
        self.assertIn("所有 2 个", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn(self.base, str(raised.exception))

    def test_disk_error_is_not_retried_and_existing_target_is_preserved(self):
        self.routes.update({"/one": {"body": self.body}, "/two": {"body": self.body}})
        target = self.root / "model"
        target.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            self.transfer("/one", "/two")
        self.assertEqual(target.read_bytes(), b"keep")
        self.assertEqual(self.requests, ["/one"])
        target.unlink()
        self.requests.clear()
        original_open = Path.open

        class FullDisk(io.BytesIO):
            def write(self, value):
                raise OSError(errno.ENOSPC, "No space left on device")

        def open_file(path, *args, **kwargs):
            if path == target and args == ("xb",):
                path.touch()
                return FullDisk()
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", open_file), self.assertRaises(OSError):
            self.transfer("/one", "/two")
        self.assertEqual(self.requests, ["/one"])
        self.assertFalse(target.exists())

    def test_attempt_deadline_bounds_slow_source(self):
        self.routes.update({"/slow": {"body": self.body}, "/good": {"body": self.body}})
        times = iter([0.0, 1801.0])
        clock = SimpleNamespace(monotonic=lambda: next(times, 1801.0))
        with patch("jarvis.model_download.time", clock):
            self.transfer("/slow", "/good")
        self.assertEqual((self.root / "model").read_bytes(), self.body)
        self.assertIn("单个来源下载超时", [e.get("reason") for e in self.events])

    def test_stt_install_preserves_verified_file_and_reuses_installed_cache(self):
        destination = self.root / "stt"
        destination.mkdir()
        tokens = b"fixture tokens"
        (destination / "tokens.txt").write_bytes(tokens)
        expected = {
            "tokens.txt": (len(tokens), hashlib.sha256(tokens).hexdigest()),
            "model.int8.onnx": (len(self.body), hashlib.sha256(self.body).hexdigest()),
        }
        self.routes.update(
            {
                "/one/model.int8.onnx": {"status": 503},
                "/two/model.int8.onnx": {"body": self.body},
            }
        )
        with (
            patch.object(stt, "MODEL_FILES", expected),
            patch.object(
                stt, "MODEL_SOURCES", (self.source("/one"), self.source("/two"))
            ),
        ):
            stt.download(
                destination, progress=lambda event, data: self.events.append(data)
            )
            self.assertTrue(catalog.verify_installed("stt", destination))
            switching = next(e for e in self.events if e["phase"] == "switching")
            self.assertEqual(switching["completed_bytes"], len(tokens))
            self.requests.clear()
            stt.download(destination, progress=lambda *_: None)
            self.assertEqual(self.requests, [])
        self.assertEqual((destination / "tokens.txt").read_bytes(), tokens)
        self.assertFalse(list(self.root.glob(".jarvis-stt-*")))

    def test_tts_bad_archive_fallback_installs_and_cached_verify_skips_network(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:bz2") as bundle:
            for name in tts.REQUIRED_FILES:
                data = ("fixture " + name).encode()
                info = tarfile.TarInfo(tts.MODEL_NAME + "/" + name)
                info.size = len(data)
                bundle.addfile(info, io.BytesIO(data))
        body = archive.getvalue()
        self.routes.update(
            {"/bad": {"body": b"x" * len(body)}, "/good": {"body": body}}
        )
        destination = self.root / "tts"
        with (
            patch.object(tts, "MODEL_SHA256", hashlib.sha256(body).hexdigest()),
            patch.object(tts, "MODEL_SIZE", len(body)),
            patch.object(
                tts, "MODEL_SOURCES", (self.source("/bad"), self.source("/good"))
            ),
        ):
            tts.download(destination, progress=lambda *_: None)
            self.assertTrue(catalog.verify_installed("tts", destination))
            self.assertEqual(self.requests, ["/bad", "/good"])
            self.requests.clear()
            tts.download(destination, progress=lambda *_: None)
            self.assertEqual(self.requests, [])
        self.assertFalse(list(self.root.glob(".jarvis-tts-*")))

    def test_tts_archive_failure_uses_pinned_files_and_remembers_healthy_mirror(self):
        files = {name: ("fixture " + name).encode() for name in tts.REQUIRED_FILES}
        pinned = {
            name: (len(body), hashlib.sha256(body).hexdigest())
            for name, body in files.items()
        }
        first = next(iter(files))
        self.routes["/archive"] = {"status": 503}
        self.routes["/hf/" + first] = {"status": 403}
        self.routes.update(
            {"/mirror/" + name: {"body": body} for name, body in files.items()}
        )
        sources = (
            DownloadSource("HF", self.base + "/hf", 2),
            DownloadSource("Mirror", self.base + "/mirror", 3),
        )
        destination = self.root / "tts-files"
        with (
            patch.object(
                tts,
                "MODEL_SOURCES",
                (DownloadSource("GitHub", self.base + "/archive", 1),),
            ),
            patch.object(tts, "FILE_SOURCES", sources),
            patch.object(tts, "KOKORO_FILES", pinned),
        ):
            tts.download(
                destination, progress=lambda event, data: self.events.append(data)
            )
            self.assertTrue(catalog.verify_installed("tts", destination))
            requests = [
                "/archive",
                "/hf/" + first,
                *["/mirror/" + name for name in files],
            ]
            self.assertEqual(self.requests, requests)
            self.requests.clear()
            tts.download(destination, progress=lambda *_: None)
            self.assertEqual(self.requests, [])
        for name, body in files.items():
            self.assertEqual((destination / name).read_bytes(), body)
        transitions = [e for e in self.events if e["phase"] == "switching"]
        self.assertEqual([e["source_index"] for e in transitions], [2, 3])
        self.assertTrue(all(e["source_count"] == 3 for e in transitions))
        self.assertEqual(
            transitions[0]["total_bytes"], sum(len(body) for body in files.values())
        )
        self.assertEqual(self.events[-1]["verification"], "pinned-file-sha256")
        self.assertEqual(self.events[-1]["total_bytes"], transitions[0]["total_bytes"])

    def test_tts_all_three_sources_fail_without_replacing_previous_model(self):
        destination = self.root / "previous-tts"
        destination.mkdir()
        previous = destination / "tokens.txt"
        previous.write_bytes(b"keep previous model")
        body = b"model fixture"
        self.routes.update(
            {
                "/archive": {"status": 503},
                "/hf/model.onnx": {"body": b"x" * len(body)},
                "/mirror/model.onnx": {"body": b"y" * len(body)},
            }
        )
        with (
            patch.object(
                tts,
                "MODEL_SOURCES",
                (DownloadSource("GitHub", self.base + "/archive", 1),),
            ),
            patch.object(
                tts,
                "FILE_SOURCES",
                (
                    DownloadSource("HF", self.base + "/hf", 2),
                    DownloadSource("Mirror", self.base + "/mirror", 3),
                ),
            ),
            patch.object(
                tts,
                "KOKORO_FILES",
                {"model.onnx": (len(body), hashlib.sha256(body).hexdigest())},
            ),
            self.assertRaisesRegex(ModelDownloadError, "所有 3 个"),
        ):
            tts.download(
                destination, progress=lambda event, data: self.events.append(data)
            )
        self.assertEqual(previous.read_bytes(), b"keep previous model")
        self.assertFalse((destination / "model.onnx").exists())
        self.assertEqual(self.events[-1]["phase"], "failed")
        self.assertFalse(list(self.root.glob(".jarvis-tts-*")))

    def test_cancellation_does_not_switch_and_staging_is_removed(self):
        expected = {
            "model.int8.onnx": (len(self.body), hashlib.sha256(self.body).hexdigest())
        }
        self.routes["/one/model.int8.onnx"] = {"body": self.body}

        def cancel_after_bytes(event, data):
            if data["phase"] == "downloading" and data["completed_bytes"]:
                raise KeyboardInterrupt("explicit test cancellation")

        with (
            patch.object(stt, "MODEL_FILES", expected),
            patch.object(
                stt, "MODEL_SOURCES", (self.source("/one"), self.source("/two"))
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            stt.download(self.root / "cancelled", progress=cancel_after_bytes)
        self.assertEqual(self.requests, ["/one/model.int8.onnx"])
        self.assertFalse((self.root / "cancelled").exists())
        self.assertFalse(list(self.root.glob(".jarvis-stt-*")))


if __name__ == "__main__":
    unittest.main()

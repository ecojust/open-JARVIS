"""Resume pinned model transfers across real localhost HTTP sources."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import socket
import tempfile
import threading
import unittest

import httpx

from jarvis.model_download import DownloadSource, TransferProgress, download_verified


class ModelResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.target = Path(self.temporary.name) / "model.onnx"
        self.body = bytes(range(256)) * 2048
        self.half = len(self.body) // 2
        self.requests = []
        self.routes = {}
        self.events = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                route = owner.routes[self.path]
                request = {
                    "path": self.path,
                    "range": self.headers.get("Range"),
                    "response_bytes": 0,
                }
                owner.requests.append(request)
                match = re.fullmatch(r"bytes=(\d+)-", request["range"] or "")
                offset = int(match[1]) if match else 0
                kind = route.get("kind", "range")
                if kind == "partial":
                    payload = route.get("prefix", owner.body[: owner.half])
                    status, length = 200, len(owner.body)
                    content_range = None
                elif kind == "reject":
                    payload, status, length = b"", 416, 0
                    content_range = f"bytes */{len(owner.body)}"
                elif kind == "html":
                    payload, status = b"<html>Sign in</html>", 200
                    length, content_range = len(payload), None
                elif kind == "ignore" or not request["range"]:
                    payload, status = owner.body, 200
                    length, content_range = len(payload), None
                else:
                    payload, status = owner.body[offset:], 206
                    length = len(payload)
                    start, end, total = offset, len(owner.body) - 1, len(owner.body)
                    if kind == "wrong-start":
                        start += 1
                    elif kind == "wrong-end":
                        end -= 1
                    elif kind == "wrong-total":
                        total += 1
                    content_range = f"bytes {start}-{end}/{total}"
                request["status"] = status
                self.send_response(status)
                self.send_header(
                    "Content-Type",
                    "text/html" if kind == "html" else "application/octet-stream",
                )
                self.send_header("Content-Length", str(length))
                if content_range:
                    self.send_header("Content-Range", content_range)
                self.end_headers()
                try:
                    request["response_bytes"] = self.wfile.write(payload)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                if kind == "partial":
                    # Advertise the full file, then close before it has arrived.
                    # A real httpx.RemoteProtocolError follows delivered chunks.
                    try:
                        self.connection.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def report(self, phase, **values):
        self.events.append({"phase": phase, **values})

    def download(self, *paths, baseline=0, report=None):
        with httpx.Client(timeout=2, trust_env=False) as client:
            return download_verified(
                client,
                self.target,
                tuple(DownloadSource(path[1:], self.base + path) for path in paths),
                len(self.body),
                hashlib.sha256(self.body).hexdigest(),
                report or self.report,
                completed_bytes=baseline,
                total_bytes=baseline + len(self.body),
            )

    def assert_complete(self):
        self.assertEqual(self.target.read_bytes(), self.body)

    def assert_monotonic(self):
        completed = [
            event["completed_bytes"]
            for event in self.events
            if event["phase"] in {"downloading", "switching", "verifying"}
        ]
        self.assertEqual(completed, sorted(completed))

    def assert_visible_restart(self):
        restart_events = [
            event
            for event in self.events
            if event.get("restart")
            or any(
                text
                in (
                    str(event.get("message", "")) + str(event.get("reason", ""))
                ).lower()
                for text in ("重新", "重下", "从头", "restart", "reset")
            )
        ]
        self.assertTrue(
            restart_events, "A fresh transfer must explain the progress reset"
        )

    def test_half_download_continues_on_mirror_and_keeps_aggregate_progress(self):
        self.routes = {"/primary": {"kind": "partial"}, "/mirror": {}}
        selected = self.download("/primary", "/mirror", baseline=1234)
        self.assertEqual(selected.name, "mirror")
        self.assertEqual(
            [(r["path"], r["range"]) for r in self.requests],
            [("/primary", None), ("/mirror", f"bytes={self.half}-")],
        )
        self.assertEqual(self.requests[-1]["status"], 206)
        self.assertEqual(
            self.requests[-1]["response_bytes"], len(self.body) - self.half
        )
        switched = next(e for e in self.events if e["phase"] == "switching")
        self.assertEqual(switched["completed_bytes"], 1234 + self.half)
        self.assertEqual(switched["resumed_bytes"], self.half)
        self.assertEqual(self.events[-1]["completed_bytes"], 1234 + len(self.body))
        self.assert_monotonic()
        self.assert_complete()

    def test_source_ignoring_range_is_skipped_before_resetting_partial(self):
        self.routes = {
            "/primary": {"kind": "partial"},
            "/legacy": {"kind": "ignore"},
            "/mirror": {},
        }
        self.download("/primary", "/legacy", "/mirror")
        self.assertEqual(
            [(r["path"], r["range"]) for r in self.requests],
            [
                ("/primary", None),
                ("/legacy", f"bytes={self.half}-"),
                ("/mirror", f"bytes={self.half}-"),
            ],
        )
        self.assert_monotonic()
        self.assert_complete()

    def test_invalid_ranges_and_non_file_responses_never_change_the_resume_offset(self):
        for kind in ("wrong-start", "wrong-end", "wrong-total", "reject", "html"):
            with self.subTest(kind=kind):
                self.target.unlink(missing_ok=True)
                self.requests.clear()
                self.events.clear()
                self.routes = {
                    "/primary": {"kind": "partial"},
                    "/invalid": {"kind": kind},
                    "/mirror": {},
                }
                self.download("/primary", "/invalid", "/mirror")
                self.assertEqual(
                    [(r["path"], r["range"]) for r in self.requests],
                    [
                        ("/primary", None),
                        ("/invalid", f"bytes={self.half}-"),
                        ("/mirror", f"bytes={self.half}-"),
                    ],
                )
                self.assert_monotonic()
                self.assert_complete()

    def test_unavailable_resume_has_bounded_explicit_fresh_download(self):
        self.routes = {"/primary": {"kind": "partial"}, "/legacy": {"kind": "ignore"}}
        self.download("/primary", "/legacy")
        self.assertIn(
            ("/legacy", f"bytes={self.half}-"),
            [(r["path"], r["range"]) for r in self.requests],
        )
        self.assertIn(
            ("/legacy", None), [(r["path"], r["range"]) for r in self.requests]
        )
        self.assertLessEqual(len(self.requests), 6)
        self.assert_visible_restart()
        self.assert_complete()

    def test_poisoned_prefix_requires_full_hash_and_recovers_from_fresh_mirror(self):
        self.routes = {
            "/poisoned": {"kind": "partial", "prefix": b"x" * self.half},
            "/mirror": {},
        }
        self.download("/poisoned", "/mirror")
        requests = [(r["path"], r["range"]) for r in self.requests]
        self.assertIn(("/mirror", f"bytes={self.half}-"), requests)
        self.assertIn(("/mirror", None), requests)
        self.assertLessEqual(len(self.requests), 6)
        self.assertTrue(
            any(
                "SHA-256" in str(e.get("reason", "")) + str(e.get("message", ""))
                for e in self.events
            )
        )
        self.assert_visible_restart()
        fresh_progress = [
            event
            for event in self.events
            if event["phase"] == "downloading"
            and event["source"] == "mirror"
            and event["resumed_bytes"] == 0
        ]
        self.assertTrue(
            len(fresh_progress) >= 2
            and all(
                "SHA-256 校验失败，正在从头重新下载当前文件" in event["message"]
                for event in fresh_progress
            ),
            "The restart reason must remain visible throughout the fresh transfer",
        )
        self.assert_complete()

    def test_switch_clears_eta_without_clearing_downloaded_bytes(self):
        self.routes = {"/primary": {"kind": "partial"}, "/mirror": {}}
        now = [0.0]
        measured = TransferProgress(self.report, clock=lambda: now[0])

        def report(phase, **values):
            # The HTTP transfer is real; only the measurement clock is controlled.
            if phase == "downloading" and values["completed_bytes"]:
                now[0] += 1.0
            measured(phase, **values)

        self.download("/primary", "/mirror", report=report)
        switched = next(e for e in self.events if e["phase"] == "switching")
        self.assertEqual(switched["completed_bytes"], self.half)
        self.assertIsNone(switched["eta_seconds"])
        self.assertIsNone(switched["bytes_per_second"])
        first_mirror = next(
            e
            for e in self.events
            if e["phase"] == "downloading" and e["source"] == "mirror"
        )
        self.assertEqual(first_mirror["completed_bytes"], self.half)
        self.assertIsNone(first_mirror["eta_seconds"])
        self.assert_monotonic()
        self.assert_complete()


if __name__ == "__main__":
    unittest.main()

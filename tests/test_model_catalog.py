"""Compatibility, integrity and restart behavior of the GUI model installer."""

from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

import httpx

from jarvis import model_catalog as catalog
from jarvis import paths
from jarvis import download_model as stt
from jarvis import download_tts_model as tts


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def files(self, kind, destination):
        destination.mkdir(parents=True, exist_ok=True)
        for name in catalog.legacy_spec(kind).required_files:
            p = destination / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(("installer test fixture: " + name).encode())
        return destination

    def manifest(self, kind, directory):
        return catalog.write_manifest(kind, directory, provenance="test-fixture")

    def test_application_upgrade_reuses_fixed_model_revision_and_legacy_contract(self):
        with (
            patch.dict(os.environ, {"JARVIS_DATA_DIR": str(self.root)}, clear=True),
            patch.object(sys, "frozen", True, create=True),
        ):
            old = catalog.managed_directory("stt")
            self.files("stt", old)
            recorded = self.manifest("stt", old)
            self.assertEqual(paths.model_dir("sensevoice"), old)
            with patch.dict(os.environ, {"JARVIS_VERSION": "99.0.0"}):
                self.assertEqual(paths.model_dir("sensevoice"), old)
            future = replace(catalog.current_spec("stt"), revision="new-format-2027")
            defaults = {**catalog.DEFAULT_REVISIONS, "stt": future.revision}
            with (
                patch.object(
                    catalog, "SUPPORTED_MODELS", (*catalog.SUPPORTED_MODELS, future)
                ),
                patch.object(catalog, "DEFAULT_REVISIONS", defaults),
            ):
                self.assertEqual(paths.model_dir("sensevoice"), old)
                self.assertEqual(
                    catalog.model_status("stt")["version"], recorded["revision"]
                )
                legacy = self.files("stt", self.root / "unversioned")
                self.assertEqual(
                    catalog.inspect_directory("stt", legacy)["version"],
                    recorded["revision"],
                )
                self.assertNotEqual(
                    catalog.inspect_directory("stt", legacy)["version"], future.revision
                )

    def test_unknown_abi_and_future_manifest_are_retained_and_never_selected_as_ready(
        self,
    ):
        directory = self.files("tts", self.root / "explicit")
        original = self.manifest("tts", directory)
        for changes in (
            {"runtime_abi": "unsupported-new-runtime"},
            {"schema_version": 2},
        ):
            manifest = {**original, **changes}
            (directory / catalog.MANIFEST_NAME).write_text(json.dumps(manifest))
            with patch.dict(
                os.environ,
                {
                    "JARVIS_DATA_DIR": str(self.root),
                    "JARVIS_TTS_MODEL_DIR": str(directory),
                },
                clear=True,
            ):
                self.assertEqual(catalog.model_status("tts")["state"], "incompatible")
                self.assertEqual(catalog.download_directory("tts"), directory)
                with self.assertRaises(ValueError):
                    tts.download(directory, progress=lambda *_: None)
            self.assertEqual(
                json.loads((directory / catalog.MANIFEST_NAME).read_text()), manifest
            )

    def test_gui_status_avoids_hashing_but_worker_detects_same_size_corruption(self):
        directory = self.files("tts", self.root / "model")
        self.manifest("tts", directory)
        with patch.object(
            catalog.hashlib,
            "sha256",
            side_effect=AssertionError("GUI must not hash weights"),
        ):
            self.assertEqual(
                catalog.inspect_directory("tts", directory)["state"], "ready"
            )
        target = directory / "model.onnx"
        target.write_bytes(b"!" * target.stat().st_size)
        self.assertEqual(catalog.inspect_directory("tts", directory)["state"], "ready")
        self.assertFalse(catalog.verify_installed("tts", directory))
        target.write_bytes(b"short")
        self.assertEqual(
            catalog.inspect_directory("tts", directory)["state"], "missing"
        )

    def test_manifest_cannot_reference_files_outside_model_directory(self):
        directory = self.files("stt", self.root / "model")
        manifest = self.manifest("stt", directory)
        for name in ("../outside", "C:/outside", "tokens.txt:stream", "./tokens.txt"):
            with self.subTest(name=name):
                candidate = {
                    **manifest,
                    "files": {
                        **manifest["files"],
                        name: {"size": 1, "sha256": "a" * 64},
                    },
                }
                (directory / catalog.MANIFEST_NAME).write_text(json.dumps(candidate))
                self.assertEqual(
                    catalog.inspect_directory("stt", directory)["state"], "incompatible"
                )
                self.assertFalse(catalog.verify_installed("stt", directory))

    def test_new_download_uses_user_revision_directory_and_preserves_legacy(self):
        with (
            patch.dict(os.environ, {"JARVIS_DATA_DIR": str(self.root)}, clear=True),
            patch.object(sys, "frozen", True, create=True),
        ):
            destination = catalog.download_directory("tts")
            self.assertEqual(
                destination,
                self.root / "models" / "kokoro" / catalog.current_spec("tts").revision,
            )
            legacy = self.files("tts", self.root / "models" / "kokoro")
            self.assertEqual(catalog.download_directory("tts"), legacy)
            self.assertEqual(
                catalog.model_status("tts")["verification"], "legacy-compatible"
            )

    def test_stt_download_publishes_only_verified_files_and_json_progress(self):
        destination = self.root / "stt"
        data = {"tokens.txt": b"fixture tokens", "model.int8.onnx": b"fixture model"}
        expected = {
            name: (len(body), hashlib.sha256(body).hexdigest())
            for name, body in data.items()
        }
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, content=data[request.url.path.rsplit("/", 1)[1]]
            )
        )
        client = httpx.Client
        output = io.StringIO()
        with (
            patch.object(stt, "MODEL_FILES", expected),
            patch.object(stt.httpx, "Client", lambda **_: client(transport=transport)),
            redirect_stdout(output),
        ):
            stt.download(destination)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[0]["data"]["phase"], "checking")
        self.assertEqual(events[-1]["event"], "download.completed")
        self.assertTrue(any(e["data"]["phase"] == "downloading" for e in events))
        self.assertTrue(catalog.verify_installed("stt", destination))
        self.assertFalse(list(self.root.glob(".jarvis-stt-*")))
        with (
            patch.object(stt, "MODEL_FILES", expected),
            patch.object(
                stt.httpx,
                "Client",
                side_effect=AssertionError("Cached model must not download"),
            ),
        ):
            stt.download(destination, progress=lambda *_: None)

    def test_failed_download_keeps_old_files_and_cleans_staging(self):
        destination = self.root / "stt"
        destination.mkdir()
        old = destination / "tokens.txt"
        old.write_bytes(b"keep this previous file")
        expected = {
            "tokens.txt": (1, hashlib.sha256(b"x").hexdigest()),
            "model.int8.onnx": (1, hashlib.sha256(b"y").hexdigest()),
        }
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"wrong size")
        )
        client = httpx.Client
        events = []
        with (
            patch.object(stt, "MODEL_FILES", expected),
            patch.object(stt.httpx, "Client", lambda **_: client(transport=transport)),
        ):
            with self.assertRaises(ValueError):
                stt.download(
                    destination,
                    progress=lambda event, data: events.append((event, data)),
                )
        self.assertEqual(old.read_bytes(), b"keep this previous file")
        self.assertFalse((destination / "model.int8.onnx").exists())
        self.assertEqual(events[-1][0], "download.error")
        self.assertFalse(list(self.root.glob(".jarvis-stt-*")))

    def test_tts_verified_archive_installs_manifest_and_legacy_cache_gets_fixed_abi(
        self,
    ):
        archive = self.root / "fixture.tar.bz2"
        with tarfile.open(archive, "w:bz2") as bundle:
            for name in tts.REQUIRED_FILES:
                data = ("fixture " + name).encode()
                info = tarfile.TarInfo(tts.MODEL_NAME + "/" + name)
                info.size = len(data)
                bundle.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        destination = self.root / "tts"
        with (
            patch.object(tts, "MODEL_SHA256", digest),
            patch.object(tts, "MODEL_SIZE", archive.stat().st_size),
        ):
            tts.download(destination, archive, progress=lambda *_: None)
        self.assertTrue(tts.model_ready(destination))
        self.assertTrue(catalog.verify_installed("tts", destination))
        manifest = json.loads((destination / catalog.MANIFEST_NAME).read_text())
        self.assertEqual(manifest["provenance"], "official-archive-sha256:" + digest)
        legacy = self.files("tts", self.root / "old-tts")
        future = replace(catalog.current_spec("tts"), revision="future-model")
        with (
            patch.object(
                catalog, "SUPPORTED_MODELS", (*catalog.SUPPORTED_MODELS, future)
            ),
            patch.object(
                catalog,
                "DEFAULT_REVISIONS",
                {**catalog.DEFAULT_REVISIONS, "tts": future.revision},
            ),
            patch.object(
                tts.httpx,
                "Client",
                side_effect=AssertionError("Legacy weights must not download again"),
            ),
        ):
            tts.download(legacy, progress=lambda *_: None)
        imported = json.loads((legacy / catalog.MANIFEST_NAME).read_text())
        self.assertEqual(imported["revision"], catalog.LEGACY_REVISIONS["tts"])
        self.assertEqual(imported["provenance"], "legacy-local-import")
        self.assertTrue(catalog.verify_installed("tts", legacy))

    def test_repair_retains_previous_directory_and_publish_failure_rolls_back(self):
        destination = self.root / "model"
        destination.mkdir()
        (destination / "previous").write_text("keep")
        staged = self.root / "staged"
        staged.mkdir()
        (staged / "new").write_text("new")
        original_rename = Path.rename

        def fail_stage(path, target):
            if path == staged:
                raise OSError("simulated publish failure")
            return original_rename(path, target)

        with patch.object(Path, "rename", fail_stage), self.assertRaises(OSError):
            catalog.publish_directory(staged, destination)
        self.assertEqual((destination / "previous").read_text(), "keep")
        backup = catalog.publish_directory(staged, destination)
        self.assertEqual((destination / "new").read_text(), "new")
        self.assertEqual((backup / "previous").read_text(), "keep")

    def test_killed_worker_lock_and_staging_can_be_recovered_without_touching_other_target(
        self,
    ):
        destination = self.root / "install"
        other = self.root / ".jarvis-stt-other"
        other.mkdir()
        (other / ".installer-owner.json").write_text(
            json.dumps(
                {
                    "kind": "stt",
                    "target": str(self.root / "other-model"),
                    "pid": os.getpid(),
                }
            )
        )
        script = """from pathlib import Path
import os,sys
from jarvis.model_catalog import installation_lock, staging_directory
p=Path(sys.argv[1])
with installation_lock(p):
    with staging_directory(p, 'stt') as staging:
        (staging/'partial-download').write_bytes(b'partial')
        os._exit(0)
"""
        subprocess.run(
            [sys.executable, "-c", script, str(destination)],
            check=True,
            timeout=15,
            cwd=Path(__file__).resolve().parents[1],
        )
        stale = [p for p in self.root.glob(".jarvis-stt-*") if p != other]
        self.assertEqual(len(stale), 1)
        with catalog.installation_lock(destination):
            with catalog.staging_directory(destination, "stt") as fresh:
                self.assertTrue(fresh.is_dir())
                self.assertFalse(stale[0].exists())
                self.assertTrue(other.is_dir())
        self.assertTrue(other.is_dir())

    def test_kill_during_publish_recovers_previous_or_completed_model_on_status_query(
        self,
    ):
        for moment in ("before", "after"):
            with self.subTest(moment=moment):
                destination = self.files("stt", self.root / ("target-" + moment))
                staged = self.files("stt", self.root / ("staged-" + moment))
                original = (destination / "model.int8.onnx").read_bytes()
                (staged / "model.int8.onnx").write_bytes(b"new completed model fixture")
                self.manifest("stt", destination)
                self.manifest("stt", staged)
                script = """from pathlib import Path
import os,sys
from jarvis.model_catalog import installation_lock,publish_directory
target,staged=Path(sys.argv[1]),Path(sys.argv[2])
original_rename=Path.rename
def rename(path,destination):
    result=original_rename(path,destination)
    if (sys.argv[3]=='before' and path==target) or (sys.argv[3]=='after' and path==staged):
        os._exit(0)
    return result
Path.rename=rename
with installation_lock(target):
    publish_directory(staged,target)
"""
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        str(destination),
                        str(staged),
                        moment,
                    ],
                    check=True,
                    timeout=15,
                    cwd=Path(__file__).resolve().parents[1],
                )
                with patch.dict(
                    os.environ, {"JARVIS_STT_MODEL_DIR": str(destination)}, clear=True
                ):
                    status = catalog.model_status("stt")
                self.assertEqual(status["state"], "ready")
                expected = (
                    original if moment == "before" else b"new completed model fixture"
                )
                self.assertEqual(
                    (destination / "model.int8.onnx").read_bytes(), expected
                )
                self.assertTrue(catalog.verify_installed("stt", destination))
                self.assertFalse(
                    (
                        destination.parent / ("." + destination.name + ".publish.json")
                    ).exists()
                )


if __name__ == "__main__":
    unittest.main()

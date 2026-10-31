import importlib.util
import json
from pathlib import Path
import runpy
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "jarvis_build_release", ROOT / "scripts" / "build_release.py"
)
build_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_release)


class PackagingTests(unittest.TestCase):
    def test_portaudio_hook_excludes_foreign_architecture_libraries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            libraries = root / "_sounddevice_data" / "portaudio-binaries"
            libraries.mkdir(parents=True)
            for name in (
                "libportaudio.dylib",
                "libportaudio64bit.dll",
                "libportaudio64bit-asio.dll",
                "libportaudio32bit.dll",
                "libportaudioarm64.dll",
            ):
                (libraries / name).write_bytes(b"mock native library")
            helpers = types.ModuleType("PyInstaller.utils.hooks")
            helpers.get_module_file_attribute = lambda name: str(
                root / "sounddevice.py"
            )
            mocked_modules = {
                "PyInstaller": types.ModuleType("PyInstaller"),
                "PyInstaller.utils": types.ModuleType("PyInstaller.utils"),
                "PyInstaller.utils.hooks": helpers,
            }
            cases = (
                ("darwin", "arm64", {"libportaudio.dylib"}),
                ("darwin", "x86_64", {"libportaudio.dylib"}),
                (
                    "win32",
                    "AMD64",
                    {"libportaudio64bit.dll", "libportaudio64bit-asio.dll"},
                ),
            )
            with patch.dict("sys.modules", mocked_modules):
                for system, machine, expected in cases:
                    with (
                        self.subTest(system=system, machine=machine),
                        patch("sys.platform", system),
                        patch("platform.machine", return_value=machine),
                    ):
                        collected = runpy.run_path(
                            str(ROOT / "packaging/hooks/hook-sounddevice.py")
                        )
                        self.assertEqual(
                            {
                                Path(source).name
                                for source, destination in collected["binaries"]
                            },
                            expected,
                        )

    def test_native_target_rejects_cross_arch_and_unsupported_hosts(self):
        self.assertEqual(build_release.native_target("Windows", "AMD64"), "windows-x64")
        self.assertEqual(build_release.native_target("Darwin", "arm64"), "macos-arm64")
        self.assertEqual(build_release.native_target("Darwin", "x86_64"), "macos-x64")
        with self.assertRaises(ValueError):
            build_release.verify_target(
                "macos-arm64", system="Darwin", machine="x86_64"
            )
        with self.assertRaises(ValueError):
            build_release.native_target("Linux", "x86_64")
        with self.assertRaises(ValueError):
            build_release.native_target("Windows", "arm64")

    def test_version_and_signing_require_explicit_safe_inputs(self):
        for invalid in ("../1.0", "1.0.0; echo test", "1.0.0\n", "v1.0.0"):
            self.assertIsNone(build_release.VERSION_PATTERN.fullmatch(invalid))
        self.assertIsNotNone(build_release.VERSION_PATTERN.fullmatch("1.0.0-rc.1"))
        with self.assertRaises(ValueError):
            build_release.validate_signing("macos-arm64", False, False)
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                build_release.validate_signing("macos-arm64", True, False)
            with self.assertRaises(ValueError):
                build_release.validate_signing("windows-x64", True, False)
        build_release.validate_signing("windows-x64", False, True)

    def test_bundle_inspection_rejects_models_private_data_and_browser_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ordinary-library.dll").write_bytes(b"library")
            build_release.inspect_bundle(root)
            for filename in (
                "model.int8.onnx",
                "voices.bin",
                "jarvis.sqlite3",
                ".env",
                "QtWebEngineCore.dll",
            ):
                target = root / filename
                target.write_bytes(b"not permitted in the bundle")
                with self.assertRaises(ValueError, msg=filename):
                    build_release.inspect_bundle(root)
                target.unlink()

    def test_approved_logo_exports_preserve_format_and_have_source_provenance(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is an optional build dependency")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            metadata = build_release.generate_icons(root)
            self.assertEqual(
                metadata["source_sha256"],
                json.loads((ROOT / "assets/branding/source.json").read_text())[
                    "sha256"
                ],
            )
            with Image.open(root / "open-jarvis.png") as image:
                self.assertEqual(image.size, (1024, 1024))
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.getpixel((0, 0))[3], 0)
            with Image.open(root / "open-jarvis.ico") as image:
                self.assertTrue(
                    {(16, 16), (32, 32), (256, 256)}.issubset(image.ico.sizes())
                )
            with Image.open(root / "open-jarvis.icns") as image:
                self.assertEqual(image.format, "ICNS")
            for filename, digest in metadata["files"].items():
                self.assertEqual(build_release.sha256(root / filename), digest)

    def test_tampered_master_stops_before_icon_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            branding = root / "assets/branding"
            branding.mkdir(parents=True)
            (branding / "open-jarvis-sheep.png").write_bytes(b"unapproved image")
            (branding / "source.json").write_text(json.dumps({"sha256": "wrong"}))
            with patch.object(build_release, "ROOT", root):
                with self.assertRaises(ValueError):
                    build_release.generate_icons(root / "out")
            self.assertFalse((root / "out").exists())

    def test_manifest_never_claims_hardware_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            artifact = output / "example.exe"
            artifact.write_bytes(b"test installer")
            path = build_release.write_manifest(
                output,
                artifact,
                target="windows-x64",
                version="0.1.0",
                signed=False,
                self_test={"ok": True},
            )
            payload = json.loads(path.read_text())
            self.assertFalse(payload["hardware_tested"])
            self.assertFalse(payload["models_bundled"])
            self.assertEqual(payload["release_status"], "unsigned-test-build")
            self.assertEqual(payload["sha256"], build_release.sha256(artifact))
            self.assertIn(
                payload["sha256"], (output / "example.exe.sha256").read_text()
            )


if __name__ == "__main__":
    unittest.main()

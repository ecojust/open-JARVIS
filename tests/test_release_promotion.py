"""Verify the complete release asset set before any GitHub publication."""

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts.promote_release import verify_candidates


VERSION = "0.1.0-alpha.8"
TARGETS = {
    "macos-x64": ("darwin", "x86_64", ".dmg"),
    "macos-arm64": ("darwin", "arm64", ".dmg"),
    "windows-x64": ("win32", "AMD64", "-setup.exe"),
}


def candidates(root):
    """Use synthetic installer bytes and their real SHA-256, never a network."""
    files = []
    for target, (platform, architecture, suffix) in TARGETS.items():
        directory = root / ("candidate-" + target)
        directory.mkdir(parents=True)
        installer = directory / f"open-JARVIS-{VERSION}-{target}{suffix}"
        installer.write_bytes(("synthetic installer: " + target).encode())
        digest = hashlib.sha256(installer.read_bytes()).hexdigest()
        manifest = installer.with_suffix(".manifest.json")
        manifest.write_text(
            json.dumps(
                {
                    "version": VERSION,
                    "target": target,
                    "artifact": installer.name,
                    "sha256": digest,
                    "signed": False,
                    "release_status": "unsigned-test-build",
                    "models_bundled": False,
                    "hardware_tested": False,
                    "self_test": {
                        "ok": True,
                        "frozen": True,
                        "platform": platform,
                        "architecture": architecture,
                        "hardware_tested": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        checksum = directory / (installer.name + ".sha256")
        checksum.write_text(f"{digest}  {installer.name}\n", encoding="utf-8")
        files.extend((installer, manifest, checksum))
    return files


def manifest_path(root):
    return next((root / "candidate-macos-x64").glob("*.manifest.json"))


class ReleasePromotionTests(unittest.TestCase):
    def test_complete_verified_matrix_returns_exactly_nine_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = candidates(root)
            actual = verify_candidates(root, VERSION)
            self.assertEqual(len(actual), 9)
            self.assertEqual(
                {path.resolve() for path in actual},
                {path.resolve() for path in expected},
            )
            self.assertTrue(all(path.is_file() for path in actual))

    def test_missing_target_or_unexpected_asset_prevents_partial_publication(self):
        for defect in ("missing_target", "extra_asset", "missing_checksum"):
            with (
                self.subTest(defect=defect),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                candidates(root)
                directory = root / "candidate-windows-x64"
                if defect == "missing_target":
                    shutil.rmtree(directory)
                elif defect == "extra_asset":
                    (directory / "unexpected.txt").write_text("not a release asset")
                else:
                    next(directory.glob("*.sha256")).unlink()
                with self.assertRaises(ValueError):
                    verify_candidates(root, VERSION)

    def test_wrong_identity_or_acceptance_claim_is_rejected(self):
        changes = (
            ("version", "0.1.0-alpha.7"),
            ("target", "macos-arm64"),
            ("artifact", "../outside.dmg"),
            ("artifact", "another-installer.dmg"),
            ("signed", True),
            ("models_bundled", True),
            ("hardware_tested", True),
        )
        for key, value in changes:
            with (
                self.subTest(key=key, value=value),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                candidates(root)
                path = manifest_path(root)
                manifest = json.loads(path.read_text())
                manifest[key] = value
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ValueError):
                    verify_candidates(root, VERSION)

    def test_corrupt_installer_or_checksum_file_is_rejected(self):
        for defect in ("installer", "digest", "checksum_name", "checksum_digest"):
            with (
                self.subTest(defect=defect),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                candidates(root)
                path = manifest_path(root)
                manifest = json.loads(path.read_text())
                installer = path.parent / manifest["artifact"]
                checksum = path.parent / (installer.name + ".sha256")
                if defect == "installer":
                    installer.write_bytes(installer.read_bytes() + b"corrupted")
                elif defect == "digest":
                    manifest["sha256"] = "0" * 64
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                elif defect == "checksum_name":
                    checksum.write_text(f"{manifest['sha256']}  unrelated.dmg\n")
                else:
                    checksum.write_text(f"{'0' * 64}  {installer.name}\n")
                with self.assertRaises(ValueError):
                    verify_candidates(root, VERSION)

    def test_frozen_runtime_must_pass_on_the_matching_platform_and_architecture(self):
        changes = (
            ("ok", False),
            ("frozen", False),
            ("platform", "win32"),
            ("architecture", "arm64"),
        )
        for key, value in changes:
            with (
                self.subTest(key=key, value=value),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                candidates(root)
                path = manifest_path(root)
                manifest = json.loads(path.read_text())
                manifest["self_test"][key] = value
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(ValueError):
                    verify_candidates(root, VERSION)


if __name__ == "__main__":
    unittest.main()

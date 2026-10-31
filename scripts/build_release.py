#!/usr/bin/env python3
"""Build native desktop installers; this script never uploads a release.

Run on the target architecture with requirements-build.txt installed. Unsigned
test packages require --allow-unsigned and carry explicit manifest metadata.
"""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
TARGETS = {
    "macos-x64": ("Darwin", "x86_64"),
    "macos-arm64": ("Darwin", "arm64"),
    "windows-x64": ("Windows", "x86_64"),
}
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")


def native_target(system: str | None = None, machine: str | None = None) -> str:
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    for target, expected in TARGETS.items():
        if (system, machine) == expected:
            return target
    raise ValueError(
        f"Unsupported native build host: {system}/{machine}. Build on a supported target host."
    )


def verify_target(target: str, *, system=None, machine=None) -> None:
    actual = native_target(system, machine)
    if target != actual:
        raise ValueError(
            f"Requested {target}, but this interpreter runs on {actual}; cross-compilation is not supported."
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generate_icons(output: Path) -> dict:
    """Format/size conversion of the approved master; no artwork changes."""
    source = ROOT / "assets" / "branding" / "open-jarvis-sheep.png"
    metadata = json.loads((source.parent / "source.json").read_text(encoding="utf-8"))
    if sha256(source) != metadata["sha256"]:
        raise ValueError(
            "Brand master hash differs from assets/branding/source.json; review the approved source first."
        )
    from PIL import Image

    output.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as master:
        if master.mode != "RGBA" or master.width != master.height:
            raise ValueError("Brand master must be a square RGBA image.")
        icon = master.resize((1024, 1024), Image.Resampling.LANCZOS)
        icon.save(output / "open-jarvis.png")
        icon.save(
            output / "open-jarvis.ico",
            sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)],
        )
        icon.save(output / "open-jarvis.icns")
    manifest = {
        "source": "assets/branding/open-jarvis-sheep.png",
        "source_sha256": metadata["sha256"],
        "conversion": "Pillow format/size conversion; artwork and transparent border preserved",
        "files": {
            path.name: sha256(path) for path in sorted(output.glob("open-jarvis.*"))
        },
    }
    (output / "icons.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def run(arguments, *, env=None, capture=False):
    # No shell interpolation; signing keys are never passed as command text.
    return subprocess.run(
        [str(arg) for arg in arguments],
        check=True,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=capture,
    )


def annotate_failure(message: str) -> None:
    """Make safe build diagnostics readable through the GitHub Checks API."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Native packaging failed::{escaped}", flush=True)


def run_pyinstaller(arguments, *, env) -> None:
    """Stream the compiler log and retain a useful failure tail for annotations."""
    recent = deque(maxlen=30)
    with subprocess.Popen(
        [str(arg) for arg in arguments],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as process:
        for line in process.stdout:
            print(line, end="", flush=True)
            recent.append(line)
        returncode = process.wait()
    if returncode:
        annotate_failure("PyInstaller failed:\n" + "".join(recent)[-12000:])
        raise ValueError(f"PyInstaller exited with status {returncode}.")


def frozen_self_test(worker: Path, evidence: Path) -> dict:
    """Validate the real bundled imports before producing any installer."""
    try:
        completed = subprocess.run(
            [str(worker), "--role", "self-test"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        annotate_failure("Frozen runtime self-test exceeded 90 seconds.")
        raise ValueError("Frozen runtime self-test timed out.") from exc
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {"ok": False, "error": "Frozen self-test returned invalid JSON."}
    report = {
        "returncode": completed.returncode,
        "self_test": payload,
        "stderr": completed.stderr[-12000:],
    }
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        report["stdout"] = completed.stdout[-12000:]
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if (
        completed.returncode != 0
        or not isinstance(payload, dict)
        or payload.get("ok") is not True
    ):
        annotate_failure(
            "Frozen runtime self-test failed:\n"
            + json.dumps(report, ensure_ascii=False)[-24000:]
        )
        raise ValueError(
            "Frozen runtime self-test failed; installer will not be created. "
            f"Diagnostic evidence: {evidence}"
        )
    print(json.dumps({"frozen_self_test": payload}, ensure_ascii=False), flush=True)
    return payload


def collect_license_inventory(output: Path) -> None:
    """Keep installed dependency license files; flag missing ones for review."""
    output.mkdir(parents=True, exist_ok=True)
    inventory = []
    for distribution in sorted(
        importlib.metadata.distributions(),
        key=lambda d: d.metadata.get("Name", "").lower(),
    ):
        name = distribution.metadata.get("Name", "unknown")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)
        saved = []
        for file in distribution.files or ():
            leaf = Path(str(file)).name.lower()
            if not (
                leaf.startswith(("license", "copying", "notice"))
                or ".dist-info/licenses/" in str(file).replace("\\", "/")
            ):
                continue
            source = Path(distribution.locate_file(file))
            if not source.is_file() or source.stat().st_size > 5 * 1024 * 1024:
                continue
            filename = (
                f"{hashlib.sha256(str(file).encode()).hexdigest()[:12]}-{source.name}"
            )
            target = output / safe_name / filename
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, target)
            saved.append(str(target.relative_to(output)))
        inventory.append(
            {
                "name": name,
                "version": distribution.version,
                "license_files": saved,
                "review_required": not bool(saved),
            }
        )
    for location in (
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
    ):
        if location.is_file():
            shutil.copyfile(location, output / "PYTHON-LICENSE.txt")
            break
    (output / "inventory.json").write_text(
        json.dumps(inventory, indent=2) + "\n", encoding="utf-8"
    )


def find_iscc() -> Path:
    located = shutil.which("ISCC.exe") or shutil.which("iscc")
    if located:
        return Path(located)
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        candidate = (
            Path(os.environ.get(variable, "C:/Program Files (x86)"))
            / "Inno Setup 6"
            / "ISCC.exe"
        )
        if candidate.is_file():
            return candidate
    raise ValueError("Inno Setup 6 is required to build the Windows installer.")


def find_signtool() -> Path:
    located = shutil.which("signtool.exe")
    if located:
        return Path(located)
    base = (
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        / "Windows Kits"
        / "10"
        / "bin"
    )
    candidates = sorted(base.glob("*/x64/signtool.exe"), reverse=True)
    if not candidates:
        raise ValueError("Windows SDK signtool.exe is required for signing.")
    return candidates[0]


def validate_signing(target: str, sign: bool, allow_unsigned: bool) -> None:
    if not sign and not allow_unsigned:
        raise ValueError(
            "Choose --sign for a signed build or --allow-unsigned for an explicitly marked test package."
        )
    if sign:
        required = (
            ("JARVIS_MACOS_SIGN_IDENTITY", "JARVIS_NOTARY_PROFILE")
            if target.startswith("macos")
            else ("JARVIS_WINDOWS_CERT_SHA1",)
        )
        if any(not os.environ.get(name) for name in required):
            raise ValueError(
                "Signing configuration is incomplete: " + ", ".join(required)
            )


def windows_sign(path: Path) -> None:
    run(
        [
            find_signtool(),
            "sign",
            "/sha1",
            os.environ["JARVIS_WINDOWS_CERT_SHA1"],
            "/fd",
            "SHA256",
            "/tr",
            "http://timestamp.digicert.com",
            "/td",
            "SHA256",
            path,
        ],
        capture=True,
    )
    run([find_signtool(), "verify", "/pa", path], capture=True)


def notarize(path: Path) -> None:
    arguments = [
        "xcrun",
        "notarytool",
        "submit",
        path,
        "--keychain-profile",
        os.environ["JARVIS_NOTARY_PROFILE"],
        "--wait",
        "--output-format",
        "json",
    ]
    if os.environ.get("JARVIS_NOTARY_KEYCHAIN"):
        arguments += ["--keychain", os.environ["JARVIS_NOTARY_KEYCHAIN"]]
    completed = run(arguments, capture=True)
    response = json.loads(completed.stdout)
    if response.get("status") != "Accepted":
        raise ValueError(
            "Apple notarization did not accept this artifact; inspect the notary submission in the signing environment."
        )


def package_macos(
    app: Path, output: Path, name: str, *, sign: bool, work: Path
) -> Path:
    if sign:
        run(["codesign", "--verify", "--deep", "--strict", app], capture=True)
        app_zip = work / "app-notarization.zip"
        run(["ditto", "-c", "-k", "--keepParent", app, app_zip])
        notarize(app_zip)
        run(["xcrun", "stapler", "staple", app], capture=True)
    stage = work / "dmg-content"
    stage.mkdir()
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    artifact = output / f"{name}.dmg"
    run(
        [
            "hdiutil",
            "create",
            "-volname",
            "Open JARVIS",
            "-srcfolder",
            stage,
            "-ov",
            "-format",
            "UDZO",
            artifact,
        ]
    )
    if sign:
        run(
            [
                "codesign",
                "--force",
                "--sign",
                os.environ["JARVIS_MACOS_SIGN_IDENTITY"],
                "--timestamp",
                artifact,
            ],
            capture=True,
        )
        notarize(artifact)
        run(["xcrun", "stapler", "staple", artifact], capture=True)
        run(["xcrun", "stapler", "validate", artifact], capture=True)
    return artifact


def package_windows(
    bundle: Path, output: Path, name: str, version: str, *, sign: bool
) -> Path:
    if sign:
        for filename in ("open-JARVIS.exe", "open-JARVIS-worker.exe"):
            windows_sign(bundle / filename)
    output_name = name + "-setup"
    run(
        [
            find_iscc(),
            f"/DAppVersion={version}",
            f"/DSourceDir={bundle}",
            f"/DOutputDir={output}",
            f"/DOutputName={output_name}",
            ROOT / "packaging" / "windows" / "installer.iss",
        ]
    )
    artifact = output / f"{output_name}.exe"
    if sign:
        windows_sign(artifact)
    return artifact


def write_manifest(
    output: Path,
    artifact: Path,
    *,
    target: str,
    version: str,
    signed: bool,
    self_test: dict,
) -> Path:
    distributions = [
        "PyInstaller",
        "Pillow",
        "PySide6-Essentials",
        "sherpa-onnx",
        "agent-framework-core",
        "agent-framework-openai",
    ]
    versions = {}
    for distribution in distributions:
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "missing"
    manifest = {
        "version": version,
        "target": target,
        "artifact": artifact.name,
        "sha256": sha256(artifact),
        "signed": signed,
        "release_status": "requires-hardware-and-performance-acceptance"
        if signed
        else "unsigned-test-build",
        "python": platform.python_version(),
        "os": platform.platform(),
        "machine": platform.machine(),
        "dependencies": versions,
        "models_bundled": False,
        "hardware_tested": False,
        "self_test": self_test,
    }
    path = output / f"{artifact.stem}.manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (output / f"{artifact.name}.sha256").write_text(
        f"{manifest['sha256']}  {artifact.name}\n", encoding="utf-8"
    )
    return path


def inspect_bundle(bundle: Path) -> None:
    """Catch unexpectedly bundled models, user data or a browser runtime."""
    forbidden = []
    for path in bundle.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if (
            "qtwebengine" in name
            or name.endswith(".onnx")
            or name in {"voices.bin", ".env", "jarvis.sqlite3"}
        ):
            forbidden.append(path.relative_to(bundle).as_posix())
    if forbidden:
        raise ValueError(
            "Unexpected heavy runtime/model/user data in bundle: "
            + ", ".join(forbidden[:10])
        )


def build(
    target: str,
    version: str,
    *,
    sign=False,
    allow_unsigned=False,
    output: Path | None = None,
) -> Path:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("Version must be SemVer, for example 0.1.0 or 0.1.0-rc.1.")
    verify_target(target)
    validate_signing(target, sign, allow_unsigned)
    output = (output or ROOT / "dist" / "releases").resolve()
    output.mkdir(parents=True, exist_ok=True)
    work = ROOT / "build" / f"release-{target}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    generate_icons(work / "icons")
    collect_license_inventory(work / "icons" / "third-party-licenses")
    build_env = dict(
        os.environ,
        JARVIS_BUILD_VERSION=version,
        JARVIS_BUILD_ICON_DIR=str(work / "icons"),
    )
    if not sign:
        build_env.pop("JARVIS_MACOS_SIGN_IDENTITY", None)
    run_pyinstaller(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            work / "dist",
            "--workpath",
            work / "pyinstaller",
            ROOT / "packaging" / "open-jarvis.spec",
        ],
        env=build_env,
    )
    if target.startswith("macos"):
        app = work / "dist" / "Open JARVIS.app"
        inspect_bundle(app)
        worker = app / "Contents" / "MacOS" / "open-JARVIS-worker"
    else:
        bundle = work / "dist" / "open-JARVIS"
        inspect_bundle(bundle)
        worker = bundle / "open-JARVIS-worker.exe"
    payload = frozen_self_test(worker, work / "frozen-self-test.json")
    name = f"open-JARVIS-{version}-{target}"
    artifact = (
        package_macos(app, output, name, sign=sign, work=work)
        if target.startswith("macos")
        else package_windows(bundle, output, name, version, sign=sign)
    )
    if not artifact.is_file():
        raise ValueError("Installer tool did not create the expected artifact.")
    write_manifest(
        output, artifact, target=target, version=version, signed=sign, self_test=payload
    )
    print(
        f"Built {artifact.name} ({'signed' if sign else 'UNSIGNED TEST BUILD'}). Native hardware/performance acceptance remains required."
    )
    return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sign", action="store_true")
    parser.add_argument("--allow-unsigned", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check-host",
        action="store_true",
        help="Validate only the native interpreter OS/architecture",
    )
    args = parser.parse_args()
    try:
        if args.check_host:
            verify_target(args.target)
            print(
                json.dumps(
                    {
                        "target": args.target,
                        "system": platform.system(),
                        "machine": platform.machine(),
                    }
                )
            )
            return 0
        build(
            args.target,
            args.version,
            sign=args.sign,
            allow_unsigned=args.allow_unsigned,
            output=args.output,
        )
    except (
        ValueError,
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        # Subprocess output can contain signing identity details; do not dump it.
        print(f"Build stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

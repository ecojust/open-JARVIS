# Native release candidates

This directory contains the build scaffolding for the three required targets:

| Target | Native build host | Artifact |
| --- | --- | --- |
| `macos-x64` | Intel macOS / x86_64 Python | DMG with `Open JARVIS.app` |
| `macos-arm64` | Apple Silicon macOS / arm64 Python | DMG with `Open JARVIS.app` |
| `windows-x64` | x64 Windows / AMD64 Python | Inno Setup EXE |

Builds refuse to cross-compile. The GitHub Actions matrix uses the explicit
`macos-15-intel`, `macos-15` (arm64), and `windows-2022` labels documented in
[actions/runner-images](https://github.com/actions/runner-images). The script
also verifies the interpreter architecture rather than trusting the runner name.
These runner versions do not establish the minimum supported consumer OS; that
still requires dependency and native installation acceptance.

## Build a test installer

Install Python 3.12 and the build dependencies on the appropriate host:

```sh
python -m pip install -r requirements-build.txt
python scripts/build_release.py --target macos-arm64 --version 0.1.0-rc.1 --allow-unsigned
```

Use `macos-x64` or `windows-x64` on the other hosts. Windows also needs Inno Setup
6 (the hosted Windows runner includes it); signed builds need the Windows SDK
SignTool. The script writes installers, SHA-256 files and build manifests to
`dist/releases/`. It does not upload or publish anything.

The default has no implicit signing fallback: select either `--sign` or
`--allow-unsigned`. Unsigned output is a test candidate, not a production release.
Native microphone/speaker, performance, upgrade and permission checks remain
required even when the hardware-free packaged self-test passes.

## Frozen process roles

The onedir bundle includes a windowed `open-JARVIS` GUI executable and a console
`open-JARVIS-worker` executable sharing the same native libraries. On Windows
their names end in `.exe`; in the macOS app both reside in `Contents/MacOS`.
The worker preserves redirected standard input/output for audio JSON-lines IPC
and backend logs; the GUI starts without a terminal window.

The frozen launcher selects the worker for `backend`, `audio`, model download
and `self-test` roles. Both executables use the same Python entry point and
`multiprocessing.freeze_support()`. Do not launch a frozen GUI as though it were
an ordinary `python -m uvicorn` interpreter.

The build runs the packaged worker with `--role self-test`. It also rejects
accidentally bundled QtWebEngine files, ONNX models, `.env` files and the user's
SQLite database. Normal installer contents include the sheep artwork, example
CSV, local web fallback resources, required runtime data and license inventory.
The GUI dependency is `PySide6-Essentials`; the full PySide6 metapackage and its
unused WebEngine/Addons components are not required.
The local sounddevice hook selects only the target PortAudio dylib or x64 DLLs;
the source wheel also contains foreign-architecture libraries that must not be
bundled. Current PySide6-Essentials 6.12.0 macOS wheels require macOS 14 or newer;
the dependency floor is known, but installer/device acceptance still remains.

## Branding and model resources

The icon generator checks the approved PNG against
`assets/branding/source.json`, then converts the same artwork to PNG, multi-size
ICO and ICNS. It preserves the existing composition and transparent margin;
generated files live in the build directory. Both Mac architectures use the same
artwork. This is format/size conversion, not a new design.

Model weights, voice files and user data are never embedded in the executable.
They are installed separately by the application's model download commands and
stored outside the read-only application directory.

## Signing

macOS signed builds require a Developer ID certificate already present in a
keychain and these environment variables:

```text
JARVIS_MACOS_SIGN_IDENTITY
JARVIS_NOTARY_PROFILE
JARVIS_NOTARY_KEYCHAIN    (optional for a non-default keychain)
```

PyInstaller signs nested binaries using the identity and the microphone
entitlement. The script verifies the app signature, notarizes and staples the
app, then creates, signs, notarizes and staples the DMG. The entitlement does not
disable library validation or add browser/JIT permissions; actual dependency
requirements must be verified on target machines.

Windows signing uses an installed Current User certificate identified by
`JARVIS_WINDOWS_CERT_SHA1`. SignTool signs the GUI, worker and installer, uses a
SHA-256 timestamp, and verifies the signatures. No certificate password is passed
to the build script. A signature does not guarantee SmartScreen reputation.

The manual workflow can provision certificates from Actions secrets. macOS uses
`MACOS_CERTIFICATE_BASE64`, `MACOS_CERTIFICATE_PASSWORD`, `MACOS_SIGN_IDENTITY`,
`APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD`. Windows uses
`WINDOWS_CERTIFICATE_BASE64`, `WINDOWS_CERTIFICATE_PASSWORD`. Temporary files and
the macOS keychain are removed after the job. Credentials must never be committed.

## Release policy

`ci.yml` validates deterministic tests and runtime dependencies on all three
targets without downloading speech models or requiring microphone hardware.
`release.yml` is manual only. It builds all three candidates, preserves manifests
and checksums, and creates a **draft prerelease only if explicitly requested**.
No push, tag, test result or build automatically publishes a public release.

The draft is held for the hardware and performance gates in the root design.
The license collector saves installed dependency license files and an inventory;
missing or embedded-native licenses still need distribution review before
publication. This scaffold is not evidence that signing, notarization, native
installation, device permissions or all three builds have already passed.

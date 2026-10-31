"""Verify an existing native build before creating a draft GitHub prerelease."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


TARGETS = {
    "macos-x64": ("darwin", "x86_64"),
    "macos-arm64": ("darwin", "arm64"),
    "windows-x64": ("win32", "x86_64"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_version(version):
    require(
        len(version) <= 80
        and re.fullmatch(r"\d+\.\d+\.\d+-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*", version),
        "This workflow promotes explicit prerelease versions only.",
    )


def verify_candidates(root: Path, version: str) -> list[Path]:
    validate_version(version)
    root = Path(root)
    require(root.is_dir() and not root.is_symlink(), "Missing candidate directory")
    require(
        {p.name for p in root.iterdir()} == {f"candidate-{t}" for t in TARGETS},
        "Expected exactly the three native candidate directories",
    )
    assets = []
    for target, (platform, architecture) in TARGETS.items():
        folder = root / f"candidate-{target}"
        require(
            folder.is_dir() and not folder.is_symlink(), "Invalid candidate directory"
        )
        stem = f"open-JARVIS-{version}-{target}"
        filename = stem + ("-setup.exe" if target == "windows-x64" else ".dmg")
        installer = folder / filename
        manifest_file = folder / (installer.stem + ".manifest.json")
        checksum = folder / (filename + ".sha256")
        expected = (installer, manifest_file, checksum)
        require(
            set(folder.iterdir()) == set(expected), f"Unexpected assets for {target}"
        )
        require(
            all(p.is_file() and not p.is_symlink() for p in expected),
            "Release assets must be regular files",
        )
        require(manifest_file.stat().st_size < 1024 * 1024, "Oversized build manifest")
        require(checksum.stat().st_size < 1024, "Oversized checksum file")
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        require(isinstance(manifest, dict), "Invalid manifest")
        require(manifest.get("version") == version, "Mixed candidate versions")
        require(manifest.get("target") == target, "Wrong candidate target")
        require(manifest.get("artifact") == filename, "Unexpected installer name")
        require(
            all(
                manifest.get(key) is False
                for key in ("signed", "models_bundled", "hardware_tested")
            ),
            "Expected explicitly unsigned preview builds without bundled models",
        )
        checks = manifest.get("self_test", {})
        require(isinstance(checks, dict), "Missing frozen self-test")
        require(
            checks.get("ok") is True and checks.get("frozen") is True,
            "Frozen self-test did not pass",
        )
        actual_arch = str(checks.get("architecture", "")).lower()
        actual_arch = {"amd64": "x86_64", "aarch64": "arm64"}.get(
            actual_arch, actual_arch
        )
        require(
            checks.get("platform") == platform and actual_arch == architecture,
            "Frozen target mismatch",
        )
        with installer.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        require(digest == manifest.get("sha256"), "Installer checksum mismatch")
        require(
            checksum.read_text(encoding="utf-8").strip() == f"{digest}  {filename}",
            "Checksum file mismatch",
        )
        assets.extend(expected)
    return assets


def gh_api(repo, path):
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/{path}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def check_run(repo, run_id, source_sha, version):
    validate_version(version)
    require(re.fullmatch(r"[0-9a-f]{40}", source_sha), "Invalid source commit")
    require(str(run_id).isdigit(), "Invalid build run ID")
    run = gh_api(repo, f"actions/runs/{run_id}")
    require(
        run["conclusion"] == "success" and run["status"] == "completed",
        "Build is not successful",
    )
    require(run["head_sha"] == source_sha, "Build source commit mismatch")
    require(
        run["path"] == ".github/workflows/release.yml"
        and run["event"] == "workflow_dispatch",
        "Unexpected build workflow",
    )
    require(
        run["head_repository"]["id"] == run["repository"]["id"],
        "Fork builds cannot be promoted",
    )
    ci = gh_api(
        repo,
        f"actions/workflows/ci.yml/runs?head_sha={source_sha}&status=success&per_page=100",
    )
    require(
        any(
            r["head_sha"] == source_sha and r["conclusion"] == "success"
            for r in ci["workflow_runs"]
        ),
        "Missing successful native CI for this source commit",
    )
    artifacts = gh_api(repo, f"actions/runs/{run_id}/artifacts?per_page=100")[
        "artifacts"
    ]
    selected = [a for a in artifacts if a["name"].startswith("candidate-")]
    require(
        len(selected) == 3
        and {a["name"] for a in selected} == {f"candidate-{t}" for t in TARGETS},
        "Incomplete native artifact matrix",
    )
    require(all(not a["expired"] for a in selected), "Candidate artifacts have expired")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write("artifact_ids=" + ",".join(str(a["id"]) for a in selected) + "\n")
    print(f"Verified source {source_sha} and three candidates from run {run_id}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check-run", "draft"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--candidates", type=Path, default=Path("candidates"))
    args = parser.parse_args()
    repo = os.environ["GH_REPO"]
    validate_version(args.version)
    if args.mode == "check-run":
        check_run(repo, args.run_id, args.source_sha, args.version)
        return
    assets = verify_candidates(args.candidates, args.version)
    notes = Path("docs/releases") / f"{args.version}.md"
    require(notes.is_file(), "Missing reviewed release notes")
    # Only a private draft is created here. Public publication is a separate,
    # explicitly authorized action after all nine uploaded assets are checked.
    subprocess.run(
        [
            "gh",
            "release",
            "create",
            f"v{args.version}",
            *(str(p) for p in assets),
            "--repo",
            repo,
            "--target",
            args.source_sha,
            "--draft",
            "--prerelease",
            "--title",
            f"Open JARVIS {args.version}",
            "--notes-file",
            str(notes),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()

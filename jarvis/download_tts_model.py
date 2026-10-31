"""Download and verify the official sherpa-onnx Kokoro v1.1 model bundle."""

import argparse
import hashlib
import shutil
import tarfile
from pathlib import Path, PurePosixPath

import httpx

from .kokoro_files import KOKORO_FILES, KOKORO_REPO, KOKORO_REVISION
from .model_download import (
    DOWNLOAD_TIMEOUT,
    DownloadSource,
    TransferProgress,
    download_verified,
)


MODEL_NAME = "kokoro-multi-lang-v1_1"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{MODEL_NAME}.tar.bz2"
FILE_SOURCES = (
    DownloadSource(
        "Hugging Face",
        f"https://huggingface.co/{KOKORO_REPO}/resolve/{KOKORO_REVISION}",
        1,
    ),
    DownloadSource(
        "HF Mirror",
        f"https://hf-mirror.com/{KOKORO_REPO}/resolve/{KOKORO_REVISION}",
        2,
    ),
)
# Published SHA-256 in the official GitHub release asset metadata.
MODEL_SHA256 = "b78e1ac42b852e839cceaafbe88f66806bf5860992d1d7b9a3f2ad1198427aed"
MODEL_SIZE = 364_855_331
DOWNLOAD_SIZE = sum(size for size, _ in KOKORO_FILES.values())
REQUIRED_FILES = (
    "model.onnx",
    "voices.bin",
    "tokens.txt",
    "lexicon-us-en.txt",
    "lexicon-zh.txt",
    "date-zh.fst",
    "number-zh.fst",
    "phone-zh.fst",
    "espeak-ng-data/phondata",
    "espeak-ng-data/phonindex",
    "espeak-ng-data/phontab",
)


def model_ready(directory: Path) -> bool:
    from .model_catalog import inspect_directory

    return inspect_directory("tts", Path(directory))["state"] == "ready"


def verify_archive(archive: Path) -> None:
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != MODEL_SHA256:
        raise ValueError("模型压缩包 SHA-256 校验失败，请重新下载官方模型")


def safe_extract(archive: Path, destination: Path) -> Path:
    """Extract ordinary files/directories only, into a new private directory.

    Every member is checked before extraction. Symlinks, hardlinks, special
    devices, traversal, duplicate files and oversized bundles are rejected.
    The caller must verify the official archive digest before calling this.
    """
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("模型解压临时目录必须为空")
    with tarfile.open(archive, "r:bz2") as bundle:
        members = bundle.getmembers()
        total = 0
        seen = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise ValueError("模型压缩包包含非法路径")
            if not path.parts or path.parts[0] != MODEL_NAME:
                raise ValueError("模型压缩包的目录结构不正确")
            if not member.isdir() and not member.isfile():
                raise ValueError("模型压缩包不能包含链接或特殊文件")
            target = destination.joinpath(*path.parts)
            if not target.resolve().is_relative_to(destination):
                raise ValueError("模型压缩包试图越过目标目录")
            if member.isfile() and path in seen:
                raise ValueError("模型压缩包包含重复文件")
            seen.add(path)
            total += member.size
            if member.size < 0 or total > 1_500_000_000:
                raise ValueError("模型压缩包解压大小异常")
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError("无法读取模型压缩包文件")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    return destination / MODEL_NAME


def _download_files(client, destination, report):
    """All sources expose identical pinned files, allowing Range continuation."""
    total = sum(size for size, _ in KOKORO_FILES.values())
    sources = list(FILE_SOURCES)
    completed = 0
    for name, (size, digest) in KOKORO_FILES.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        selected = download_verified(
            client,
            target,
            tuple(
                DownloadSource(source.name, f"{source.url}/{name}", source.index)
                for source in sources
            ),
            size,
            digest,
            report,
            completed_bytes=completed,
            total_bytes=total,
        )
        completed += size
        # Remember a healthy provider rather than paying another timeout for
        # every file. A failed prefix continues on the alternate fixed source.
        sources.sort(key=lambda source: source.name != selected.name)
    return total


def download(directory: Path, archive: Path | None = None, *, progress=None) -> None:
    from .model_catalog import (
        MANIFEST_NAME,
        Progress,
        inspect_directory,
        installation_lock,
        publish_directory,
        staging_directory,
        verify_installed,
        write_manifest,
    )

    directory = Path(directory).resolve()
    report = TransferProgress(Progress("tts", directory, progress))
    try:
        with installation_lock(directory):
            report("checking", message="正在检查中文播报模型", force=True)
            status = inspect_directory("tts", directory)
            if status["state"] == "incompatible":
                raise ValueError(status["message"])
            if status["state"] == "ready" and verify_installed(
                "tts", directory, report
            ):
                if not (directory / MANIFEST_NAME).is_file():
                    write_manifest(
                        "tts",
                        directory,
                        provenance="legacy-local-import",
                        progress=report,
                        revision=status["version"],
                    )
                report(
                    "ready",
                    completed_bytes=DOWNLOAD_SIZE,
                    total_bytes=DOWNLOAD_SIZE,
                    message=status["message"],
                    verification="legacy-local-import"
                    if status["verification"] == "legacy-compatible"
                    else status["verification"],
                    force=True,
                )
                return
            with staging_directory(directory, "tts") as temporary:
                temporary = Path(temporary)
                if archive is None:
                    # Mixing a partially received compressed archive with a
                    # different file layout cannot support byte continuation.
                    # Use one pinned file representation on both online sources.
                    unpacked = temporary / "files"
                    with httpx.Client(
                        follow_redirects=True, timeout=DOWNLOAD_TIMEOUT
                    ) as client:
                        total = _download_files(client, unpacked, report)
                    verification = "pinned-file-sha256"
                    provenance = verification + ":" + KOKORO_REVISION
                else:
                    # Developer/offline archive checks still verify the original
                    # published bundle. This is not an automatic network source.
                    archive = Path(archive)
                    report("verifying", message="正在校验官方模型压缩包", force=True)
                    verify_archive(archive)
                    report("installing", message="正在解压模型", force=True)
                    unpacked = safe_extract(archive, temporary / "unpacked")
                    total = MODEL_SIZE
                    verification = "official-archive-sha256"
                    provenance = verification + ":" + MODEL_SHA256
                if not model_ready(unpacked):
                    raise ValueError("模型缺少必要文件")
                write_manifest(
                    "tts",
                    unpacked,
                    provenance=provenance,
                    progress=report,
                )
                report("installing", message="正在启用已校验模型", force=True)
                backup = publish_directory(unpacked, directory)
            report(
                "ready",
                completed_bytes=total,
                total_bytes=total,
                message="中文播报模型下载完成，已校验并保存",
                force=True,
                retained_directory=str(backup) if backup else None,
                verification=verification,
            )
    except (httpx.HTTPError, OSError, ValueError, tarfile.TarError) as exc:
        report("failed", message=str(exc), force=True)
        raise


def main() -> None:
    from .paths import load_environment
    from .model_catalog import download_directory

    load_environment()
    parser = argparse.ArgumentParser(description="下载并校验 Kokoro v1.1 中文语音模型")
    parser.add_argument(
        "--archive", type=Path, help="使用已下载的官方 tar.bz2（仍校验 SHA-256）"
    )
    parser.add_argument("--directory", type=Path, help="模型安装目录")
    args = parser.parse_args()
    directory = args.directory or download_directory("tts")
    try:
        download(directory, args.archive)
    except (httpx.HTTPError, OSError, ValueError, tarfile.TarError) as exc:
        raise SystemExit(
            f"语音模型安装失败：{exc}。请在客户端设置中重试下载。"
        ) from exc


if __name__ == "__main__":
    main()

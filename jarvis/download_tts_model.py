"""Download and verify the official sherpa-onnx Kokoro v1.1 model bundle."""

import argparse
import hashlib
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import httpx


MODEL_NAME = "kokoro-multi-lang-v1_1"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/{MODEL_NAME}.tar.bz2"
# Published SHA-256 in the official GitHub release asset metadata.
MODEL_SHA256 = "b78e1ac42b852e839cceaafbe88f66806bf5860992d1d7b9a3f2ad1198427aed"
MODEL_SIZE = 364_855_331
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
    return all(
        (directory / name).is_file() and (directory / name).stat().st_size > 0
        for name in REQUIRED_FILES
    )


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


def download(directory: Path, archive: Path | None = None) -> None:
    directory = directory.resolve()
    if model_ready(directory):
        print(f"Kokoro 模型已存在：{directory}", flush=True)
        return
    if directory.exists() and any(directory.iterdir()):
        raise ValueError(
            f"{directory} 已有不完整的文件。请移走旧文件或使用其他模型目录"
        )
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".jarvis-kokoro-", dir=directory.parent
    ) as temporary:
        temporary = Path(temporary)
        if archive is None:
            archive = temporary / "model.tar.bz2"
            print("正在下载 Kokoro v1.1 多语言语音模型（约 348 MiB）…", flush=True)
            with httpx.Client(
                follow_redirects=True, timeout=httpx.Timeout(90, connect=30)
            ) as client:
                with client.stream("GET", MODEL_URL) as response:
                    response.raise_for_status()
                    size = 0
                    previous = -1
                    with archive.open("wb") as output:
                        for chunk in response.iter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > MODEL_SIZE + 1024:
                                raise ValueError("模型压缩包大小异常")
                            output.write(chunk)
                            progress = size // (20 * 1024 * 1024)
                            if progress != previous:
                                print(
                                    f"  {size / 1024 / 1024:.0f} / 348 MiB", flush=True
                                )
                                previous = progress
        print("正在校验模型并解压…", flush=True)
        verify_archive(archive)
        unpacked = safe_extract(archive, temporary / "unpacked")
        if not model_ready(unpacked):
            raise ValueError("模型压缩包缺少必要文件")
        if directory.exists():
            directory.rmdir()  # Only an empty destination may be replaced.
        unpacked.rename(directory)
    print(f"本地语音模型就绪：{directory}", flush=True)


def main() -> None:
    from .paths import load_environment, model_dir

    load_environment()
    parser = argparse.ArgumentParser(description="下载并校验 Kokoro v1.1 中文语音模型")
    parser.add_argument(
        "--archive", type=Path, help="使用已下载的官方 tar.bz2（仍校验 SHA-256）"
    )
    parser.add_argument("--directory", type=Path, help="模型安装目录")
    args = parser.parse_args()
    directory = args.directory or model_dir("kokoro")
    try:
        download(directory, args.archive)
    except (httpx.HTTPError, OSError, ValueError, tarfile.TarError) as exc:
        raise SystemExit(
            f"语音模型安装失败：{exc}\n请重试 python -m jarvis.download_tts_model"
        ) from exc


if __name__ == "__main__":
    main()

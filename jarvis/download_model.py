"""Explicit one-time download of the local Chinese STT model."""

import hashlib
from pathlib import Path

import httpx

from .model_download import (
    DOWNLOAD_TIMEOUT,
    DownloadSource,
    TransferProgress,
    download_verified,
)


MODEL_REPO = "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/2365baeacb507f821a0c8120fcee3d484dba7a07"
MODEL_SOURCES = (
    DownloadSource("Hugging Face", MODEL_REPO),
    DownloadSource("HF Mirror", MODEL_REPO.replace("huggingface.co", "hf-mirror.com")),
)
MODEL_FILES = {
    "tokens.txt": (
        315894,
        "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
    ),
    "model.int8.onnx": (
        239233841,
        "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
    ),
}


def verified(path: Path, size: int, digest: str) -> bool:
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest() == digest


def download(directory: Path, *, progress=None) -> None:
    """Install a pinned model transactionally from the GUI's download worker."""
    import shutil
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
    report = TransferProgress(Progress("stt", directory, progress))
    try:
        with installation_lock(directory):
            report("checking", message="正在检查语音识别模型", force=True)
            status = inspect_directory("stt", directory)
            if status["state"] == "incompatible":
                raise ValueError(status["message"])
            # A previous release's unversioned downloads have pinned file
            # hashes too; verify them without downloading or moving weights.
            if all(
                verified(directory / name, size, digest)
                for name, (size, digest) in MODEL_FILES.items()
            ):
                if (directory / MANIFEST_NAME).is_file() and not verify_installed(
                    "stt", directory, report
                ):
                    raise ValueError(
                        "已安装模型校验失败，原文件已保留。请升级客户端后在设置中重试下载。"
                    )
                if not (directory / MANIFEST_NAME).is_file():
                    write_manifest(
                        "stt",
                        directory,
                        provenance="legacy-pinned-file-sha256",
                        progress=report,
                        revision=status["version"],
                    )
                report(
                    "ready",
                    completed_bytes=sum(v[0] for v in MODEL_FILES.values()),
                    total_bytes=sum(v[0] for v in MODEL_FILES.values()),
                    message="语音识别模型已就绪，无需重复下载",
                    force=True,
                    verification="installed-manifest",
                )
                return
            total = sum(value[0] for value in MODEL_FILES.values())
            downloaded = 0
            sources = [
                DownloadSource(source.name, source.url, index)
                for index, source in enumerate(MODEL_SOURCES, start=1)
            ]
            with staging_directory(directory, "stt") as temporary:
                staged = Path(temporary) / "model"
                staged.mkdir()
                with httpx.Client(
                    follow_redirects=True, timeout=DOWNLOAD_TIMEOUT
                ) as client:
                    for name, (expected_size, expected_digest) in MODEL_FILES.items():
                        target = staged / name
                        if verified(directory / name, expected_size, expected_digest):
                            shutil.copyfile(directory / name, target)
                            downloaded += expected_size
                            continue
                        selected = download_verified(
                            client,
                            target,
                            tuple(
                                DownloadSource(
                                    source.name, f"{source.url}/{name}", source.index
                                )
                                for source in sources
                            ),
                            expected_size,
                            expected_digest,
                            report,
                            completed_bytes=downloaded,
                            total_bytes=total,
                        )
                        sources.sort(key=lambda source: source.name != selected.name)
                        downloaded += expected_size
                write_manifest(
                    "stt", staged, provenance="pinned-file-sha256", progress=report
                )
                report("installing", message="正在启用已校验模型", force=True)
                backup = publish_directory(staged, directory)
            report(
                "ready",
                completed_bytes=total,
                total_bytes=total,
                message="语音识别模型下载完成，已校验并保存",
                force=True,
                retained_directory=str(backup) if backup else None,
                verification="pinned-file-sha256",
            )
    except (httpx.HTTPError, OSError, ValueError) as exc:
        report("failed", message=str(exc), force=True)
        raise


def main():
    from .paths import load_environment
    from .model_catalog import download_directory

    load_environment()
    try:
        download(download_directory("stt"))
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise SystemExit(f"模型下载失败：{exc}。请在客户端设置中重试下载。") from exc


if __name__ == "__main__":
    main()

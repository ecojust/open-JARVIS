"""Explicit one-time download of the local Chinese STT model."""

import hashlib
from pathlib import Path

import httpx


MODEL_REPO = "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/2365baeacb507f821a0c8120fcee3d484dba7a07"
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


def download(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(
        follow_redirects=True, timeout=httpx.Timeout(60, connect=20)
    ) as client:
        for name, (expected_size, expected_digest) in MODEL_FILES.items():
            target = directory / name
            if verified(target, expected_size, expected_digest):
                print(f"已存在：{target}", flush=True)
                continue
            temporary = target.with_suffix(target.suffix + ".part")
            print(f"正在下载 {name} …", flush=True)
            try:
                with client.stream("GET", f"{MODEL_REPO}/{name}") as response:
                    response.raise_for_status()
                    if "text/html" in response.headers.get("content-type", ""):
                        raise ValueError("模型站点返回了网页，未下载到模型文件")
                    size = 0
                    digest = hashlib.sha256()
                    previous_mb = -1
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > expected_size:
                                raise ValueError(f"{name} 下载大小与固定版本不一致")
                            handle.write(chunk)
                            digest.update(chunk)
                            mb = size // (10 * 1024 * 1024)
                            if mb != previous_mb:
                                print(f"  {size / 1024 / 1024:.0f} MB", flush=True)
                                previous_mb = mb
                    if size != expected_size or digest.hexdigest() != expected_digest:
                        raise ValueError(f"{name} 大小或 SHA-256 校验失败")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            print(f"已保存：{target}", flush=True)
    print("模型就绪。运行 python -m jarvis.desktop 启动桌面助理。", flush=True)


def main():
    from .paths import load_environment, model_dir

    load_environment()
    directory = model_dir("sensevoice")
    try:
        download(directory)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise SystemExit(
            f"模型下载失败：{exc}\n可以重新运行，或手动将 model.int8.onnx 和 tokens.txt 放到 {directory}"
        ) from exc


if __name__ == "__main__":
    main()

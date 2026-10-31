"""Versioned local speech-model contract, independent of application releases.

The GUI only reads small metadata and file sizes. Full hashes run in the
explicit download worker. Keep supported model contracts when adding a newer
one: replacing the application must not invalidate an already installed model.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import time
import uuid


MANIFEST_NAME = ".openjarvis-model.json"
MANIFEST_SCHEMA = 1
MAX_MANIFEST_BYTES = 512 * 1024
TTS_REQUIRED_FILES = (
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


@dataclass(frozen=True)
class ModelSpec:
    kind: str
    family: str
    model_id: str
    revision: str
    runtime_abi: str
    display_name: str
    size_bytes: int
    required_files: tuple[str, ...]
    display_version: str = ""


# These are model-format contracts, not the app's version. Add a contract when
# upgrading a model; retain old entries while their runtime adapters are kept.
SUPPORTED_MODELS = (
    ModelSpec(
        "stt",
        "sensevoice",
        "sensevoice-int8",
        "2365baeacb507f821a0c8120fcee3d484dba7a07",
        "sherpa-sensevoice-int8-v1",
        "SenseVoice 语音识别",
        239549735,
        ("model.int8.onnx", "tokens.txt"),
        "2024-07-17 · INT8",
    ),
    ModelSpec(
        "tts",
        "kokoro",
        "kokoro-multilingual",
        "v1.1-b78e1ac42b85",
        "sherpa-kokoro-multilingual-v1",
        "Kokoro 中文播报",
        364855331,
        TTS_REQUIRED_FILES,
        "v1.1 · 多语言",
    ),
)
DEFAULT_REVISIONS = {spec.kind: spec.revision for spec in SUPPORTED_MODELS}
LEGACY_REVISIONS = {
    "stt": "2365baeacb507f821a0c8120fcee3d484dba7a07",
    "tts": "v1.1-b78e1ac42b85",
}


def normalize_kind(kind: str) -> str:
    value = {"sensevoice": "stt", "kokoro": "tts"}.get(kind, kind)
    if value not in {"stt", "tts"}:
        raise ValueError("未知语音模型类型")
    return value


def current_spec(kind: str) -> ModelSpec:
    kind = normalize_kind(kind)
    return next(
        spec
        for spec in SUPPORTED_MODELS
        if spec.kind == kind and spec.revision == DEFAULT_REVISIONS[kind]
    )


def legacy_spec(kind: str) -> ModelSpec:
    kind = normalize_kind(kind)
    return next(
        spec
        for spec in SUPPORTED_MODELS
        if spec.kind == kind and spec.revision == LEGACY_REVISIONS[kind]
    )


def _read_manifest(directory: Path):
    path = directory / MANIFEST_NAME
    if path.is_symlink():
        raise ValueError("模型兼容清单不能使用符号链接")
    if not path.exists():
        return None
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("模型兼容清单无效")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("模型兼容清单格式无效")
    return value


def _manifest_spec(kind, manifest):
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != MANIFEST_SCHEMA
    ):
        raise ValueError(
            "模型清单版本不受当前应用支持；原模型已保留，请选择兼容模型或升级应用。"
        )
    spec = next(
        (
            item
            for item in SUPPORTED_MODELS
            if item.kind == normalize_kind(kind)
            and item.model_id == manifest.get("model_id")
            and item.revision == manifest.get("revision")
            and item.runtime_abi == manifest.get("runtime_abi")
        ),
        None,
    )
    if spec is None:
        raise ValueError(
            "模型格式或版本不受当前应用支持；原模型已保留，请在模型管理中选择兼容模型。"
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= 4096:
        raise ValueError("模型文件清单无效")
    for name, item in files.items():
        path = PurePosixPath(name)
        if (
            not name
            or path.as_posix() != name
            or name == "."
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or ":" in name
            or not isinstance(item, dict)
            or type(item.get("size")) is not int
            or item["size"] < 0
            or not isinstance(item.get("sha256"), str)
            or len(item["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in item["sha256"])
        ):
            raise ValueError("模型文件清单包含无效路径、尺寸或校验值")
    if not all(name in files for name in spec.required_files):
        raise ValueError("模型清单缺少运行所需文件")
    if sum(item["size"] for item in files.values()) > 1_500_000_000:
        raise ValueError("模型文件总量超过安装预算")
    return spec


def _regular_file(directory, name):
    path = directory / name
    if not path.is_file() or path.is_symlink():
        return False
    parent = path.parent
    while parent != directory:
        if parent.is_symlink() or parent == parent.parent:
            return False
        parent = parent.parent
    return not directory.is_symlink()


def inspect_directory(kind: str, directory: Path) -> dict:
    """Bounded metadata check; never hash weights or initialize model inference."""
    kind = normalize_kind(kind)
    directory = Path(directory)
    spec = current_spec(kind)
    result = {
        "kind": kind,
        "display_name": spec.display_name,
        "version": spec.revision,
        "display_version": spec.display_version or spec.revision,
        "size_bytes": spec.size_bytes,
        "directory": str(directory),
        "state": "missing",
        "verification": "none",
        "message": "尚未下载模型",
    }
    try:
        manifest = _read_manifest(directory)
        if manifest is not None:
            spec = _manifest_spec(kind, manifest)
            result["version"] = spec.revision
            result["display_version"] = spec.display_version or spec.revision
            result["verification"] = "installed-manifest"
        else:
            # Unversioned directories were shipped with this original model
            # format. Never silently relabel them as a future default revision.
            spec = legacy_spec(kind)
            result["version"] = spec.revision
        for name in spec.required_files:
            if (
                not _regular_file(directory, name)
                or (directory / name).stat().st_size <= 0
            ):
                result["message"] = "模型文件不完整，请在模型管理中下载或重试。"
                return result
            if (
                manifest
                and (directory / name).stat().st_size != manifest["files"][name]["size"]
            ):
                result["message"] = "模型文件尺寸已改变，请在模型管理中重新下载。"
                return result
        result.update(state="ready", message="模型已就绪")
        if manifest is None:
            result.update(
                verification="legacy-compatible",
                message="兼容旧版模型，可继续使用，无需重复下载",
            )
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result.update(state="incompatible", message=str(exc))
        return result


def managed_directory(kind: str) -> Path:
    from .paths import user_data_dir

    spec = current_spec(kind)
    return user_data_dir() / "models" / spec.family / spec.revision


def select_default_directory(kind: str, development: Path | None = None) -> Path:
    """Prefer installed current/compatible contracts, then unchanged legacy paths."""
    from .paths import user_data_dir

    spec = current_spec(kind)
    family = user_data_dir() / "models" / spec.family
    preferred = managed_directory(kind)
    recover_if_needed(preferred)
    recover_if_needed(family)
    if inspect_directory(kind, preferred)["state"] == "ready":
        return preferred
    for supported in SUPPORTED_MODELS:
        if supported.kind != spec.kind:
            continue
        candidate = family / supported.revision
        if (
            candidate != preferred
            and inspect_directory(kind, candidate)["state"] == "ready"
        ):
            return candidate
    if development is not None and development.is_dir():
        recover_if_needed(development)
        return development
    if inspect_directory(kind, family)["state"] == "ready":
        return family
    if preferred.exists():
        return preferred
    # Preserve the old default path when missing as well, for existing settings
    # and installers. New downloads use download_directory(), not this fallback.
    return family


def model_status(kind: str) -> dict:
    from .paths import model_dir

    spec = current_spec(kind)
    return inspect_directory(spec.kind, model_dir(spec.family))


def download_directory(kind: str) -> Path:
    """Honor explicit model paths; do not report success for an unused download."""
    from .paths import model_dir

    spec = current_spec(kind)
    selected = model_dir(spec.family)
    variable = "JARVIS_STT_MODEL_DIR" if spec.kind == "stt" else "JARVIS_TTS_MODEL_DIR"
    if (
        os.getenv(variable, "").strip()
        or inspect_directory(kind, selected)["state"] == "ready"
    ):
        return selected
    return managed_directory(kind)


def emit_progress(kind, phase, *, progress=None, **data):
    event = (
        "download.completed"
        if phase == "ready"
        else "download.error"
        if phase == "failed"
        else "download.progress"
    )
    payload = {"kind": normalize_kind(kind), "phase": phase, **data}
    if progress is not None:
        progress(event, payload)
    else:
        # ASCII JSON avoids Windows redirected-console code-page failures.
        print(
            json.dumps({"event": event, "data": payload}, ensure_ascii=True), flush=True
        )


class Progress:
    def __init__(self, kind, directory, callback=None):
        self.kind, self.directory, self.callback = kind, str(directory), callback
        self.last_time, self.last_phase = 0.0, None

    def __call__(
        self,
        phase,
        *,
        completed_bytes=0,
        total_bytes=0,
        message="",
        force=False,
        **extra,
    ):
        now = time.monotonic()
        if (
            not force
            and phase == self.last_phase
            and now - self.last_time < 0.2
            and completed_bytes != total_bytes
        ):
            return
        self.last_time, self.last_phase = now, phase
        emit_progress(
            self.kind,
            phase,
            progress=self.callback,
            directory=self.directory,
            completed_bytes=completed_bytes,
            total_bytes=total_bytes,
            message=message,
            **extra,
        )


@contextmanager
def installation_lock(directory: Path):
    """OS-owned lock is released even when the GUI cancels/kills the worker."""
    directory = Path(directory)
    directory.parent.mkdir(parents=True, exist_ok=True)
    path = directory.parent / ("." + directory.name + ".install.lock")
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("该模型正在另一个窗口中下载，请等待完成。") from exc
        try:
            _recover_publication(directory)
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _publication_journal(directory):
    return directory.parent / ("." + directory.name + ".publish.json")


def _recover_publication(directory):
    """Under the target lock, recover a kill between the two directory renames."""
    journal = _publication_journal(directory)
    if not journal.exists():
        return
    if journal.is_symlink() or journal.stat().st_size > 2048:
        raise ValueError("模型安装恢复记录无效；已保留原文件。")
    record = json.loads(journal.read_text(encoding="utf-8"))
    name = record.get("backup") if isinstance(record, dict) else None
    prefix = "." + directory.name + ".previous-"
    if (
        not isinstance(record, dict)
        or record.get("schema_version") != 1
        or not isinstance(name, str)
        or re.fullmatch(re.escape(prefix) + "[0-9a-f]{32}", name) is None
    ):
        raise ValueError("模型安装恢复路径无效；已保留原文件。")
    backup = directory.parent / name
    if directory.is_symlink() or backup.is_symlink():
        raise ValueError("模型恢复目录不能使用符号链接")
    if not directory.exists():
        if not backup.is_dir():
            raise ValueError(
                "模型安装尚未完成且找不到旧目录，请在模型管理中选择新的目录。"
            )
        backup.rename(directory)
    journal.unlink()


def recover_if_needed(directory):
    """Usually a single exists check; never wait for an active installer."""
    if _publication_journal(directory).exists():
        try:
            with installation_lock(directory):
                pass
        except (ValueError, OSError):
            # A running installer owns the transaction. Its GUI progress stays
            # active; the eventual worker completion refreshes model status.
            pass


@contextmanager
def staging_directory(directory: Path, kind: str):
    """Called under the target lock; safely reclaim a killed worker's staging.

    A lock holder is the only installer for this target. Other targets' staging
    directories are preserved, even when their PID has already exited.
    """
    kind = normalize_kind(kind)
    target = str(Path(directory).resolve())
    prefix = f".jarvis-{kind}-"
    marker_name = ".installer-owner.json"
    for old in directory.parent.glob(prefix + "*"):
        marker = old / marker_name
        if (
            old.is_symlink()
            or not old.is_dir()
            or not marker.is_file()
            or marker.is_symlink()
        ):
            continue
        try:
            if marker.stat().st_size > 2048:
                continue
            value = json.loads(marker.read_text(encoding="utf-8"))
            if value.get("target") == target and value.get("kind") == kind:
                shutil.rmtree(old)
        except (OSError, ValueError, AttributeError):
            # A stale antivirus/file handle need not prevent a new download.
            continue
    with tempfile.TemporaryDirectory(prefix=prefix, dir=directory.parent) as temporary:
        path = Path(temporary)
        (path / marker_name).write_text(
            json.dumps({"target": target, "kind": kind, "pid": os.getpid()}),
            encoding="utf-8",
        )
        yield path


def verify_installed(kind: str, directory: Path, progress=None) -> bool:
    """Worker-only integrity verification; legacy files remain explicitly marked."""
    status = inspect_directory(kind, directory)
    if status["state"] != "ready":
        return False
    manifest = _read_manifest(directory)
    if manifest is None:
        return True
    files = manifest["files"]
    total, completed = sum(item["size"] for item in files.values()), 0
    for name, item in files.items():
        if not _regular_file(directory, name):
            return False
        path = directory / name
        if path.stat().st_size != item["size"]:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                completed += len(chunk)
                if progress:
                    progress(
                        "verifying",
                        completed_bytes=completed,
                        total_bytes=total,
                        message="正在校验已安装模型",
                    )
        if digest.hexdigest() != item["sha256"]:
            return False
    return True


def write_manifest(
    kind: str, directory: Path, *, provenance: str, progress=None, revision=None
):
    """Record every extracted file after verification of the pinned source."""
    spec = (
        current_spec(kind)
        if revision is None
        else next(
            spec
            for spec in SUPPORTED_MODELS
            if spec.kind == normalize_kind(kind) and spec.revision == revision
        )
    )
    paths = sorted(
        path
        for path in directory.rglob("*")
        if path.is_file() and path.name not in {MANIFEST_NAME, MANIFEST_NAME + ".part"}
    )
    if not 1 <= len(paths) <= 4096:
        raise ValueError("模型文件数量异常")
    total = sum(path.stat().st_size for path in paths)
    if total > 1_500_000_000:
        raise ValueError("模型目录超过 1.5 GB 安装预算，请选择仅包含语音模型的目录。")
    completed, files = 0, {}
    for path in paths:
        name = path.relative_to(directory).as_posix()
        if not _regular_file(directory, name):
            raise ValueError("模型文件不能使用符号链接")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                completed += len(chunk)
                if progress:
                    progress(
                        "verifying",
                        completed_bytes=completed,
                        total_bytes=total,
                        message="正在生成模型完整性清单",
                    )
        files[name] = {"size": path.stat().st_size, "sha256": digest.hexdigest()}
    value = {
        "schema_version": MANIFEST_SCHEMA,
        "model_id": spec.model_id,
        "revision": spec.revision,
        "runtime_abi": spec.runtime_abi,
        "provenance": provenance,
        "files": files,
    }
    _manifest_spec(kind, value)
    encoded = json.dumps(value, ensure_ascii=True, indent=2).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("模型清单超过大小限制")
    temporary = directory / (MANIFEST_NAME + ".part")
    if temporary.is_symlink():
        raise ValueError("模型清单临时文件不能使用符号链接")
    try:
        temporary.write_bytes(encoded)
        temporary.replace(directory / MANIFEST_NAME)
    finally:
        temporary.unlink(missing_ok=True)
    return value


def publish_directory(staged: Path, destination: Path) -> Path | None:
    """Publish only a completed model; retain any previous directory for rollback."""
    backup = None
    journal = _publication_journal(destination)
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError("模型目标必须是普通目录")
        backup = destination.with_name(
            "." + destination.name + ".previous-" + uuid.uuid4().hex
        )
        temporary = journal.with_name(journal.name + ".part-" + uuid.uuid4().hex)
        try:
            temporary.write_text(
                json.dumps({"schema_version": 1, "backup": backup.name}),
                encoding="utf-8",
            )
            temporary.replace(journal)
        finally:
            temporary.unlink(missing_ok=True)
    try:
        if backup is not None:
            destination.rename(backup)
        staged.rename(destination)
    except BaseException:
        if backup is not None and backup.exists() and not destination.exists():
            backup.rename(destination)
        journal.unlink(missing_ok=True)
        raise
    journal.unlink(missing_ok=True)
    return backup

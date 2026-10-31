"""Bounded, sequential failover for pinned model files in private staging."""

from collections import deque
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import time

import httpx


DOWNLOAD_TIMEOUT = httpx.Timeout(15, connect=10)
SOURCE_DEADLINE_SECONDS = 30 * 60


class TransferProgress:
    """Measure aggregate download throughput without timers or extra I/O.

    Each worker owns one estimator. A short rolling window includes connection
    gaps between small files, so Kokoro's file fallback does not restart its ETA
    377 times. Source changes and byte-counter rollbacks discard old samples.
    The underlying ``Progress`` continues to throttle IPC output.
    """

    WINDOW_SECONDS = 12.0
    SAMPLE_SECONDS = 0.2
    WARMUP_SECONDS = 1.0

    def __init__(self, report, *, clock=None):
        self.report = report
        self.clock = clock or time.monotonic
        # At most 5 samples/second plus a boundary sample. maxlen also bounds
        # memory if a caller supplies an unusual test clock or timing pattern.
        self.samples = deque(maxlen=64)
        self.source = None
        self.last_completed = 0

    def _reset(self):
        self.samples.clear()
        self.source = None
        self.last_completed = 0

    def __call__(self, phase, *, completed_bytes=0, total_bytes=0, **data):
        eta, speed = None, None
        if phase == "downloading":
            now = self.clock()
            source = (data.get("source"), data.get("source_index"), total_bytes)
            if source != self.source or completed_bytes < self.last_completed:
                self._reset()
                self.source = source
            self.last_completed = completed_bytes
            if not self.samples:
                self.samples.append((now, completed_bytes))
            elif now - self.samples[-1][0] >= self.SAMPLE_SECONDS:
                self.samples.append((now, completed_bytes))
            # Retain one sample just before the rolling boundary, avoiding a
            # near-zero denominator after a long connection gap.
            while (
                len(self.samples) > 2
                and now - self.samples[1][0] >= self.WINDOW_SECONDS
            ):
                self.samples.popleft()
            started, baseline = self.samples[0]
            elapsed, transferred = now - started, completed_bytes - baseline
            if (
                elapsed >= self.WARMUP_SECONDS
                and transferred > 0
                and 0 <= completed_bytes < total_bytes
            ):
                speed = transferred / elapsed
                eta = math.ceil((total_bytes - completed_bytes) / speed)
        elif phase != "verifying":
            self._reset()
        # Verifying may be between two files. Preserve measurement history but
        # never claim a network ETA for hashing, installation or completion.
        self.report(
            phase,
            completed_bytes=completed_bytes,
            total_bytes=total_bytes,
            **{**data, "eta_seconds": eta, "bytes_per_second": speed},
        )


@dataclass(frozen=True)
class DownloadSource:
    name: str
    url: str
    index: int = 0


class ModelDownloadError(ValueError):
    """All configured sources failed; the installed model was not changed."""

    def __init__(self, failures):
        self.failures = tuple(failures)
        detail = "；".join(f"{name}：{reason}" for name, reason in failures)
        super().__init__(
            f"所有 {len(failures)} 个下载来源均失败（{detail}）。请稍后重试。"
        )


class _SourceFailure(ValueError):
    pass


def _reason(error):
    # Never surface signed redirect URLs, response bodies or transport secrets.
    if isinstance(error, _SourceFailure):
        return str(error)
    if isinstance(error, httpx.TimeoutException):
        return "连接或读取超时"
    if isinstance(error, httpx.HTTPStatusError):
        return f"服务器返回 HTTP {error.response.status_code}"
    if isinstance(error, httpx.TooManyRedirects):
        return "重定向次数过多"
    return "网络连接中断或响应无效"


def download_verified(
    client,
    target: Path,
    sources: tuple[DownloadSource, ...],
    expected_size: int,
    expected_digest: str,
    report,
    *,
    completed_bytes=0,
    total_bytes=None,
    source_count=None,
):
    """Try each source once; only publish bytes matching the pinned contract.

    ``target`` must be a new private staging path. An unsuccessful attempt is
    removed before switching, so bytes from different servers never mix. Local
    disk failures and cancellation propagate immediately instead of wasting
    another full model transfer. The caller owns the installation transaction.
    """
    if not sources or len(sources) > 16:
        raise ValueError("模型下载来源配置无效")
    target = Path(target)
    total = total_bytes if total_bytes is not None else expected_size
    failures = []
    previous_source = ""
    for index, source in enumerate(sources, start=1):
        details = {
            "source": source.name,
            "source_index": source.index or index,
            "source_count": source_count or len(sources),
            "filename": target.name,
        }
        if failures:
            report(
                "switching",
                completed_bytes=completed_bytes,
                total_bytes=total,
                message=f"{previous_source} {failures[-1][1]}，正在切换到 {source.name}",
                previous_source=previous_source,
                reason=failures[-1][1],
                force=True,
                **details,
            )
        report(
            "downloading",
            completed_bytes=completed_bytes,
            total_bytes=total,
            message=f"正在从 {source.name} 下载 {target.name}",
            force=True,
            **details,
        )
        created = False
        started = time.monotonic()
        try:
            with client.stream(
                "GET", source.url, headers={"Accept-Encoding": "identity"}
            ) as response:
                response.raise_for_status()
                if "text/html" in response.headers.get("content-type", "").lower():
                    raise _SourceFailure("返回了网页而非模型文件")
                content_length = response.headers.get("content-length")
                if (
                    content_length is not None
                    and response.headers.get("content-encoding", "identity")
                    == "identity"
                    and (
                        not content_length.isdecimal()
                        or (content_length.lstrip("0") or "0") != str(expected_size)
                    )
                ):
                    raise _SourceFailure("文件大小与固定版本不一致")
                size, digest = 0, hashlib.sha256()
                with target.open("xb") as output:
                    created = True
                    # Keep the transport's small chunks: buffering until a
                    # megabyte is full could hide a slow trickle indefinitely.
                    for chunk in response.iter_bytes():
                        if time.monotonic() - started > SOURCE_DEADLINE_SECONDS:
                            raise _SourceFailure("单个来源下载超时")
                        size += len(chunk)
                        if size > expected_size:
                            raise _SourceFailure("文件大小超过固定版本")
                        output.write(chunk)
                        digest.update(chunk)
                        report(
                            "downloading",
                            completed_bytes=completed_bytes + size,
                            total_bytes=total,
                            message=f"正在从 {source.name} 下载 {target.name}",
                            **details,
                        )
                if time.monotonic() - started > SOURCE_DEADLINE_SECONDS:
                    raise _SourceFailure("单个来源下载超时")
                report(
                    "verifying",
                    completed_bytes=completed_bytes + size,
                    total_bytes=total,
                    message=f"正在校验 {target.name}",
                    force=True,
                    **details,
                )
                if size != expected_size:
                    raise _SourceFailure("文件未下载完整")
                if digest.hexdigest() != expected_digest:
                    raise _SourceFailure("SHA-256 校验失败")
            return source
        except (httpx.HTTPError, _SourceFailure) as error:
            if created:
                target.unlink(missing_ok=True)
            failures.append((source.name, _reason(error)))
            previous_source = source.name
        except OSError:
            # Storage/permission errors do not improve by changing the server.
            if created:
                target.unlink(missing_ok=True)
            raise
    raise ModelDownloadError(failures)

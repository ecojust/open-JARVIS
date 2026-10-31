"""Bounded, sequential failover for pinned model files in private staging."""

from collections import deque
from dataclasses import dataclass
import hashlib
import math
import re
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
            **{
                **data,
                # Controller snapshots merge fields. Whole-model verification
                # must not inherit the last transferred file's identity.
                "filename": data.get("filename", ""),
                "eta_seconds": eta,
                "bytes_per_second": speed,
            },
        )


@dataclass(frozen=True)
class DownloadSource:
    name: str
    url: str
    index: int = 0


class ModelDownloadError(ValueError):
    """All configured sources failed; the installed model was not changed."""

    def __init__(self, failures):
        # A source may get one explicit clean retry after incompatible Range
        # support or a poisoned prefix. Count distinct sources, not attempts.
        self.failures = tuple(dict(failures).items())
        detail = "；".join(f"{name}：{reason}" for name, reason in self.failures)
        super().__init__(
            f"所有 {len(self.failures)} 个下载来源均失败（{detail}）。请稍后重试。"
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


class _RangeUnavailable(_SourceFailure):
    pass


class _IntegrityFailure(_SourceFailure):
    pass


def _check_response(response, offset, expected_size):
    """Validate byte identity before appending any response data to a prefix."""
    if offset and response.status_code in {200, 416}:
        raise _RangeUnavailable("来源不支持当前文件的断点续传")
    response.raise_for_status()
    if response.status_code not in {200, 206}:
        raise _SourceFailure("响应状态不适用于模型下载")
    if "text/html" in response.headers.get("content-type", "").lower():
        raise _SourceFailure("返回了网页而非模型文件")
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise _SourceFailure("来源返回了不同编码的文件，不能安全续传")
    if response.status_code == 206:
        value = response.headers.get("content-range", "")
        expected = f"bytes {offset}-{expected_size - 1}/{expected_size}"
        if value != expected:
            raise _RangeUnavailable("来源返回的续传范围与固定文件不一致")
    elif offset:
        raise _RangeUnavailable("来源未返回有效的续传范围")
    length = response.headers.get("content-length")
    if length is not None and (
        re.fullmatch(r"[0-9]+", length) is None
        or (length.lstrip("0") or "0") != str(expected_size - offset)
    ):
        raise _SourceFailure("文件大小与固定版本不一致")


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
    """Resume a pinned file across mirrors, then verify its complete SHA-256.

    The caller owns a private staging transaction. Network failures retain the
    received prefix and its rolling hash; another source must honor the exact
    Range before bytes may be appended. A source gets at most one initial
    attempt and one explicit clean retry. A restart is only needed after an
    integrity failure or when no remaining source supports the saved range.
    Local I/O errors and cancellation never trigger another network attempt.
    """
    if not sources or len(sources) > 16:
        raise ValueError("模型下载来源配置无效")
    target = Path(target)
    total = total_bytes if total_bytes is not None else expected_size
    failures = []
    previous_source = ""
    size, digest, created = 0, hashlib.sha256(), False
    pending = deque((source, index, None) for index, source in enumerate(sources, 1))
    deferred_fresh = {}
    fresh_attempted = set()
    discarded_reason = None

    def discard_prefix():
        nonlocal size, digest, created
        if created:
            target.unlink(missing_ok=True)
        size, digest, created = 0, hashlib.sha256(), False

    while pending or deferred_fresh:
        if not pending:
            # Try all viable Range candidates before discarding any bytes.
            pending.extend(
                (source, index, "可用来源不支持断点续传，正在重新下载当前文件")
                for index, source in deferred_fresh.items()
                if index not in fresh_attempted
            )
            deferred_fresh.clear()
            if not pending:
                break
        source, index, restart_reason = pending.popleft()
        if restart_reason:
            fresh_attempted.add(index)
            discard_prefix()
        # An integrity failure may have already discarded the whole file.
        # Explain that reset even when the next mirror is its initial attempt.
        restart_reason = restart_reason or discarded_reason
        discarded_reason = None
        offset = size
        details = {
            "source": source.name,
            "source_index": source.index or index,
            "source_count": source_count or len(sources),
            "filename": target.name,
            "resumed_bytes": offset,
        }
        if failures:
            reason = failures[-1][1]
            message = (
                f"{restart_reason}：{source.name}"
                if restart_reason
                else f"{previous_source} {reason}，切换到 {source.name}继续下载"
            )
            report(
                "switching",
                completed_bytes=completed_bytes + size,
                total_bytes=total,
                message=message,
                previous_source=previous_source,
                reason=reason,
                restart_reason=restart_reason,
                force=True,
                **details,
            )
        download_message = (
            f"{restart_reason}；" if restart_reason else ""
        ) + f"正在从 {source.name} {'续传' if offset else '下载'} {target.name}"
        report(
            "downloading",
            completed_bytes=completed_bytes + size,
            total_bytes=total,
            message=download_message,
            # Small-file boundaries share the aggregate transfer's throttle.
            # A real failure/restart remains an immediate, visible transition.
            force=bool(failures),
            **details,
        )
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        started = time.monotonic()
        try:
            with client.stream("GET", source.url, headers=headers) as response:
                _check_response(response, offset, expected_size)
                mode = "ab" if offset else "wb" if created else "xb"
                with target.open(mode) as output:
                    created = True
                    for chunk in response.iter_bytes():
                        if time.monotonic() - started > SOURCE_DEADLINE_SECONDS:
                            raise _SourceFailure("单个来源下载超时")
                        if size + len(chunk) > expected_size:
                            raise _IntegrityFailure("文件大小超过固定版本")
                        output.write(chunk)
                        size += len(chunk)
                        digest.update(chunk)
                        report(
                            "downloading",
                            completed_bytes=completed_bytes + size,
                            total_bytes=total,
                            message=download_message,
                            **details,
                        )
                if time.monotonic() - started > SOURCE_DEADLINE_SECONDS:
                    raise _SourceFailure("单个来源下载超时")
                if size != expected_size:
                    raise _SourceFailure("文件未下载完整，已保留下载进度")
                # The rolling digest comparison below is instantaneous. Do not
                # bounce the whole UI into verification for every small file.
                # The final manifest pass still verifies the complete model.
                transfer_complete = completed_bytes + size >= total
                report(
                    "verifying" if transfer_complete else "downloading",
                    completed_bytes=completed_bytes + size,
                    total_bytes=total,
                    message=f"正在校验 {target.name}"
                    if transfer_complete
                    else download_message,
                    force=transfer_complete,
                    **details,
                )
                if digest.hexdigest() != expected_digest:
                    raise _IntegrityFailure("SHA-256 校验失败")
            return source
        except (httpx.HTTPError, _SourceFailure) as error:
            # A transport can fail after sending the entire pinned file. Its
            # complete size/hash are authoritative even if the socket closes.
            if (
                created
                and not isinstance(error, _IntegrityFailure)
                and size == expected_size
                and digest.hexdigest() == expected_digest
            ):
                transfer_complete = completed_bytes + size >= total
                report(
                    "verifying" if transfer_complete else "downloading",
                    completed_bytes=completed_bytes + size,
                    total_bytes=total,
                    message=f"已收到完整文件，正在确认 {target.name}"
                    if transfer_complete
                    else download_message,
                    force=transfer_complete,
                    **details,
                )
                return source
            failures.append((source.name, _reason(error)))
            previous_source = source.name
            if isinstance(error, _RangeUnavailable) and offset:
                deferred_fresh[index] = source
            elif isinstance(error, _IntegrityFailure):
                # A corrupt earlier prefix can poison a perfectly good mirror.
                # Give the current source one clean, explicitly reported retry.
                discard_prefix()
                discarded_reason = f"{_reason(error)}，正在从头重新下载当前文件"
                if offset and index not in fresh_attempted:
                    pending.appendleft((source, index, discarded_reason))
        except OSError:
            discard_prefix()
            raise
    discard_prefix()
    raise ModelDownloadError(failures)

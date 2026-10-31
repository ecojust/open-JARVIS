"""Create a bounded narrated slide video from an actual PowerPoint file.

No video is published. Heavy dependencies are lazy and the speech model lives
in a disposable worker, so cancellation can stop native synthesis and reclaim
its memory. FFmpeg receives fixed arguments and no shell command text.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import wave
import zipfile


MAX_SLIDES = 6
MAX_NARRATION_CHARS = 200
MAX_VIDEO_SECONDS = 600
DEFAULT_TIMEOUT = 240


def _workspace_path(workspace, path):
    from .tools import ToolRegistry

    return ToolRegistry(Path(workspace), None).resolve_path(path)


def _check_alive(deadline, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise ValueError("视频生成已取消，未发布任何内容")
    if time.monotonic() >= deadline:
        raise ValueError("视频生成超过时间预算，已停止工作进程")


def _presentation(path):
    if (
        not path.is_file()
        or path.suffix.lower() != ".pptx"
        or path.stat().st_size > 20 * 1024**2
    ):
        raise ValueError("请提供工作目录内不超过20MiB的实际PPTX文件")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if (
                len(members) > 3000
                or sum(item.file_size for item in members) > 100 * 1024**2
            ):
                raise ValueError("PPTX解压规模超出视频工具限制")
        from pptx import Presentation

        presentation = Presentation(path)
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("无法读取PPTX结构") from exc
    if not 1 <= len(presentation.slides) <= MAX_SLIDES:
        raise ValueError(f"单次讲解视频支持1–{MAX_SLIDES}页PPT；请先拆分演示文稿")
    slides = []
    for index, slide in enumerate(presentation.slides, 1):
        title_shape = slide.shapes.title
        title = title_shape.text.strip() if title_shape is not None else f"第{index}页"
        bullets, sources = [], []
        for shape in slide.shapes:
            if shape == title_shape or not shape.has_text_frame:
                continue
            for paragraph in shape.text_frame.paragraphs:
                value = paragraph.text.strip()
                if not value:
                    continue
                if value.startswith("参考来源："):
                    for run in paragraph.runs:
                        url = run.hyperlink.address
                        if url and url.startswith(("https://", "http://")):
                            sources.append(
                                {"title": run.text.strip() or "PPT来源", "url": url}
                            )
                    continue
                if value == "Open JARVIS" or re.fullmatch(r"\d+\s*/\s*\d+", value):
                    continue
                bullets.append(value.removeprefix("• "))
        if not bullets:
            raise ValueError(f"第{index}页没有可用的文字要点；目前仅支持文字型PPT讲解")
        if len(bullets) > 8:
            raise ValueError(f"第{index}页文字过密，请拆分为每页至多8个要点")
        frame = slide.notes_slide.notes_text_frame if slide.has_notes_slide else None
        notes = frame.text.strip() if frame is not None else ""
        slides.append(
            {"title": title, "bullets": bullets, "notes": notes, "sources": sources}
        )
    return {
        "schema_version": 1,
        "kind": "presentation",
        "title": presentation.core_properties.title or path.stem,
        "preview_notice": "依据实际PPTX文字生成的讲解画面；不保证与Office逐像素一致。",
        "slides": slides,
        "sources": [],
    }


def _narrations(deck):
    from .tts_runtime import prepare_text

    texts = []
    for index, slide in enumerate(deck["slides"], 1):
        original = slide["notes"] or "。".join([slide["title"], *slide["bullets"]])
        # Reject overlong scripts instead of silently truncating the user's talk.
        if len(original) > MAX_NARRATION_CHARS:
            raise ValueError(
                f"第{index}页讲稿超过{MAX_NARRATION_CHARS}字，请精简PPT备注后重试"
            )
        text = prepare_text(original, maximum=MAX_NARRATION_CHARS)
        if not text:
            raise ValueError(f"第{index}页没有可用讲稿")
        texts.append(text)
    return texts


def _speech_worker(texts, directory, speaker, speed):
    """Spawn target: actual Kokoro synthesis, not device playback."""
    directory = Path(directory)
    response = {}
    try:
        import numpy as np
        from .tts_runtime import build_tts_engine, speech_chunks, synthesize

        engine = build_tts_engine()
        pages, total = [], 0.0
        for index, text in enumerate(texts):
            chunks = []
            sample_rate = None
            for chunk in speech_chunks(text):
                audio = synthesize(engine, chunk, speaker=speaker, speed=speed)
                samples = np.asarray(audio.samples, dtype=np.float32).reshape(-1)
                if (
                    not samples.size
                    or not np.isfinite(samples).all()
                    or float(np.max(np.abs(samples))) < 1e-5
                ):
                    raise ValueError("Kokoro没有生成有效音频采样")
                if sample_rate is not None and sample_rate != audio.sample_rate:
                    raise ValueError("语音片段采样率不一致")
                sample_rate = audio.sample_rate
                pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
                chunks.append((chunk, pcm))
            output = directory / f"speech-{index:02d}.wav"
            cues, position = [], 0
            with wave.open(str(output), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(sample_rate)
                for chunk, samples in chunks:
                    start = position / sample_rate
                    writer.writeframes(samples.tobytes())
                    position += len(samples)
                    cues.append(
                        {"text": chunk, "start": start, "end": position / sample_rate}
                    )
            duration = position / sample_rate
            if not 0 < duration <= 180:
                raise ValueError("单页语音时长超出180秒限制")
            total += duration
            if total > MAX_VIDEO_SECONDS:
                raise ValueError("讲解视频总时长超出10分钟限制")
            pages.append(
                {
                    "file": output.name,
                    "duration": duration,
                    "sample_rate": sample_rate,
                    "samples": position,
                    "cues": cues,
                }
            )
        response = {
            "pages": pages,
            "duration": total,
            "engine": "sherpa-onnx/Kokoro-v1.1",
        }
    except Exception as exc:
        response = {"error": f"语音合成失败：{type(exc).__name__}：{str(exc)[:400]}"}
    (directory / "speech-result.json").write_text(
        json.dumps(response, ensure_ascii=False), encoding="utf-8"
    )


def _stop_process(process):
    if process.is_alive():
        process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join(1)


def _synthesize_narration(texts, directory, speaker, speed, deadline, cancel_event):
    from .download_tts_model import model_ready
    from .paths import model_dir

    if not model_ready(model_dir("kokoro")):
        raise ValueError("缺少Kokoro本地模型，无法生成真实讲解音轨；请先下载TTS模型")
    process = multiprocessing.get_context("spawn").Process(
        target=_speech_worker,
        args=(texts, str(directory), speaker, speed),
        daemon=True,
    )
    process.start()
    try:
        while process.is_alive():
            _check_alive(deadline, cancel_event)
            process.join(0.1)
        _check_alive(deadline, cancel_event)
        result_path = directory / "speech-result.json"
        if process.exitcode != 0 or not result_path.is_file():
            raise ValueError("语音工作进程失败，未生成讲解视频")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("error"):
            raise ValueError(result["error"])
        if len(result.get("pages", [])) != len(texts):
            raise ValueError("语音工作进程返回了不完整的页数")
        return result
    finally:
        _stop_process(process)
        process.close()


def _ffmpeg_path():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        executable = get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise ValueError("缺少FFmpeg视频编码器，请安装包含视频组件的客户端") from exc
    if not Path(executable).is_file():
        raise ValueError("FFmpeg编码器不可用")
    return executable


def probe_video(path: Path) -> dict:
    """Inspect the real container, including when only bundled FFmpeg exists."""
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size > 200 * 1024**2:
        raise ValueError("视频不存在或超过200MiB预览限制")
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0,
        )
        if completed.returncode:
            raise ValueError("无法读取实际视频容器：" + completed.stderr[-500:])
        payload = json.loads(completed.stdout)
        video = next(
            (
                row
                for row in payload.get("streams", [])
                if row.get("codec_type") == "video"
            ),
            None,
        )
        audio = next(
            (
                row
                for row in payload.get("streams", [])
                if row.get("codec_type") == "audio"
            ),
            None,
        )
        if not video:
            raise ValueError("文件没有实际视频轨道")
        result = {
            "duration_seconds": float(payload["format"]["duration"]),
            "width": int(video.get("width", 0)),
            "height": int(video.get("height", 0)),
            "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name") if audio else None,
            "has_audio": audio is not None,
            "verification_source": "ffprobe_container",
        }
    else:
        # No output option means FFmpeg reads headers then exits. It does not
        # decode the complete movie, and no untrusted filter text is executed.
        completed = subprocess.run(
            [_ffmpeg_path(), "-nostdin", "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0,
        )
        detail = completed.stderr[:64000]
        duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", detail)
        video = re.search(r"Video:\s*([^,\s]+)[^\n]*?\b(\d{2,5})x(\d{2,5})\b", detail)
        audio = re.search(r"Audio:\s*([^,\s]+)", detail)
        if not duration or not video:
            raise ValueError("无法从实际视频容器读取时长和画面")
        hours, minutes, seconds = map(float, duration.groups())
        result = {
            "duration_seconds": hours * 3600 + minutes * 60 + seconds,
            "width": int(video.group(2)),
            "height": int(video.group(3)),
            "video_codec": video.group(1),
            "audio_codec": audio.group(1) if audio else None,
            "has_audio": audio is not None,
            "verification_source": "ffmpeg_container_headers",
        }
    if (
        not math.isfinite(result["duration_seconds"])
        or not 0 < result["duration_seconds"] <= MAX_VIDEO_SECONDS + 5
    ):
        raise ValueError("视频实际时长无效或超过预览限制")
    if not (1 <= result["width"] <= 8192 and 1 <= result["height"] <= 8192):
        raise ValueError("视频画面尺寸超过有界预览限制")
    return result


def video_cover_png(path: Path) -> bytes:
    """Decode the actual first video frame; do not trust an adjacent poster."""
    path = Path(path).resolve()
    probe_video(path)
    completed = subprocess.run(
        [
            _ffmpeg_path(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            str(path),
            "-vf",
            "scale=960:540:force_original_aspect_ratio=decrease",
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-dn",
            "-threads",
            "1",
            "-f",
            "image2pipe",
            "-c:v",
            "png",
            "pipe:1",
        ],
        capture_output=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt"
        else 0,
    )
    data = completed.stdout
    if (
        completed.returncode
        or not data.startswith(b"\x89PNG\r\n\x1a\n")
        or len(data) > 2 * 1024**2
    ):
        raise ValueError("无法从实际视频生成有界首帧预览")
    return data


def _encode(command, directory, deadline, cancel_event):
    log = directory / "ffmpeg.log"
    with log.open("wb") as handle:
        process = subprocess.Popen(
            command,
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if os.name == "nt"
            else 0,
        )
        try:
            while process.poll() is None:
                _check_alive(deadline, cancel_event)
                time.sleep(0.05)
            if process.returncode:
                raise ValueError(
                    "视频编码失败："
                    + log.read_text(encoding="utf-8", errors="replace")[-1200:]
                )
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)


def _timestamp(value):
    milliseconds = int(round(value * 1000))
    hours, remaining = divmod(milliseconds, 3_600_000)
    minutes, remaining = divmod(remaining, 60_000)
    seconds, milliseconds = divmod(remaining, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def create_explainer_video(
    workspace,
    path,
    *,
    presentation_path,
    manifest_path=None,
    speaker=3,
    speed=1.0,
    overwrite=False,
    timeout=DEFAULT_TIMEOUT,
    cancel_event=None,
):
    """Return real MP4/SRT/script artifacts; never upload or fabricate audio."""
    if type(speaker) is not int or not 0 <= speaker <= 102:
        raise ValueError("音色编号必须是0–102的整数")
    if (
        type(speed) not in (int, float)
        or not math.isfinite(speed)
        or not 0.5 <= speed <= 2.0
    ):
        raise ValueError("语速必须位于0.5–2.0")
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 5 <= timeout <= 300
    ):
        raise ValueError("视频生成时间预算必须为5–300秒")
    if type(overwrite) is not bool:
        raise ValueError("overwrite必须是布尔值")
    deadline = time.monotonic() + timeout
    workspace = Path(workspace).resolve()
    target = _workspace_path(workspace, path)
    source = _workspace_path(workspace, presentation_path)
    if target.suffix.lower() != ".mp4":
        raise ValueError("讲解视频输出必须使用.mp4文件名")
    deck = _presentation(source)
    texts = _narrations(deck)
    if manifest_path is not None:
        manifest_file = _workspace_path(workspace, manifest_path)
        if not manifest_file.is_file() or manifest_file.stat().st_size > 1024**2:
            raise ValueError("PPT预览清单缺失或过大")
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if [slide.get("title") for slide in manifest.get("slides", [])] != [
            slide["title"] for slide in deck["slides"]
        ]:
            raise ValueError("PPT预览清单与实际演示文稿不一致")
    outputs = {
        "video": target,
        "subtitles": target.with_name(target.name + ".srt"),
        "narration": target.with_name(target.name + ".narration.md"),
        "metadata": target.with_name(target.name + ".video.json"),
        "poster": target.with_name(target.name + ".poster.png"),
    }
    for output in outputs.values():
        _workspace_path(workspace, output.relative_to(workspace).as_posix())
        if output.exists() and not overwrite:
            raise ValueError("视频或配套文件已存在，默认不覆盖")
    ffmpeg = _ffmpeg_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    published = []
    with tempfile.TemporaryDirectory(
        prefix=".jarvis-video-", dir=target.parent
    ) as temporary:
        directory = Path(temporary)
        from .office_tools import render_presentation_images

        image_dir = directory / "slides"
        image_dir.mkdir()
        images = render_presentation_images(deck, image_dir)
        if len(images) != len(deck["slides"]):
            raise ValueError("PPT页面渲染不完整")
        _check_alive(deadline, cancel_event)
        speech = _synthesize_narration(
            texts, directory, speaker, speed, deadline, cancel_event
        )
        segments, subtitle_rows, elapsed, cue_index = [], [], 0.0, 1
        combined_audio = directory / "narration.wav"
        sample_rate = speech["pages"][0]["sample_rate"]
        # One continuous PCM track prevents per-clip AAC encoder priming from
        # accumulating subtitle drift. Short silent tails align slide cuts to
        # exact video frames; every cue follows the actual PCM sample clock.
        with wave.open(str(combined_audio), "wb") as combined:
            combined.setnchannels(1)
            combined.setsampwidth(2)
            combined.setframerate(sample_rate)
            timeline_samples = 0
            for index, (image, page) in enumerate(zip(images, speech["pages"])):
                if page["sample_rate"] != sample_rate:
                    raise ValueError("PPT页语音采样率不一致")
                frame_count = math.ceil(page["samples"] / sample_rate * 24)
                aligned_samples = round(frame_count / 24 * sample_rate)
                elapsed = timeline_samples / sample_rate
                with wave.open(str(directory / page["file"]), "rb") as source_audio:
                    if (
                        source_audio.getnchannels(),
                        source_audio.getsampwidth(),
                        source_audio.getframerate(),
                        source_audio.getnframes(),
                    ) != (1, 2, sample_rate, page["samples"]):
                        raise ValueError("语音文件与采样清单不一致")
                    combined.writeframes(source_audio.readframes(page["samples"]))
                    combined.writeframes(
                        b"\0\0" * max(0, aligned_samples - page["samples"])
                    )
                segment = directory / f"segment-{index:02d}.mp4"
                _encode(
                    [
                        ffmpeg,
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-loop",
                        "1",
                        "-framerate",
                        "24",
                        "-i",
                        str(image),
                        "-frames:v",
                        str(frame_count),
                        "-an",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-tune",
                        "stillimage",
                        "-crf",
                        "22",
                        "-pix_fmt",
                        "yuv420p",
                        "-threads",
                        "2",
                        str(segment),
                    ],
                    directory,
                    deadline,
                    cancel_event,
                )
                if not segment.is_file() or not segment.stat().st_size:
                    raise ValueError("FFmpeg未生成有效视频片段")
                segments.append(segment)
                for cue in page["cues"]:
                    subtitle_rows.append(
                        f"{cue_index}\n{_timestamp(elapsed + cue['start'])} --> {_timestamp(elapsed + cue['end'])}\n{cue['text']}\n"
                    )
                    cue_index += 1
                timeline_samples += aligned_samples
            elapsed = timeline_samples / sample_rate
        concat = directory / "segments.txt"
        # Filenames are generated ASCII basenames; never interpolate user paths
        # into concat directives or FFmpeg filter expressions.
        concat.write_text(
            "".join(f"file '{segment.name}'\n" for segment in segments),
            encoding="ascii",
        )
        encoded = directory / "final.mp4"
        _encode(
            [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(concat),
                "-i",
                str(combined_audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-t",
                f"{elapsed:.6f}",
                "-movflags",
                "+faststart",
                str(encoded),
            ],
            directory,
            deadline,
            cancel_event,
        )
        if not encoded.is_file() or encoded.stat().st_size < 1024:
            raise ValueError("编码器未生成有效MP4")
        if encoded.stat().st_size > 200 * 1024**2:
            raise ValueError("视频文件超过200MiB限制")
        verified = probe_video(encoded)
        if (
            verified["video_codec"] != "h264"
            or verified["audio_codec"] != "aac"
            or not verified["has_audio"]
        ):
            raise ValueError("生成的视频没有通过H.264/AAC实际轨道检查")
        subtitle = directory / "subtitles.srt"
        subtitle.write_text("\n".join(subtitle_rows), encoding="utf-8")
        script = directory / "narration.md"
        source_urls = list(
            dict.fromkeys(
                source["url"] for slide in deck["slides"] for source in slide["sources"]
            )
        )
        source_markdown = (
            "\n\n## PPT中记录的参考来源\n\n"
            + "\n".join(
                f"- [{index}]({url})" for index, url in enumerate(source_urls, 1)
            )
            if source_urls
            else ""
        )
        script.write_text(
            "# "
            + deck["title"]
            + "：讲解稿\n\n"
            + "\n\n".join(
                f"## 第{i + 1}页：{slide['title']}\n\n{text}"
                for i, (slide, text) in enumerate(zip(deck["slides"], texts))
            )
            + source_markdown
            + "\n\n本视频为基于PPT的合成语音讲解，未执行任何平台发布。\n",
            encoding="utf-8",
        )
        metadata = {
            "schema_version": 1,
            "kind": "video",
            "title": deck["title"],
            "source_presentation": source.relative_to(workspace).as_posix(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": verified["duration_seconds"],
            "narration_duration_seconds": speech["duration"],
            "timeline_duration_seconds": elapsed,
            "timing_basis": "Kokoro PCM sample counts; slide boundaries padded to 24fps frames",
            "verification_source": verified["verification_source"],
            "width": 1280,
            "height": 720,
            "fps": 24,
            "video_codec": "h264",
            "audio_codec": "aac",
            "speech_engine": speech["engine"],
            "speaker": speaker,
            "slide_count": len(deck["slides"]),
            "source_urls": source_urls,
            "subtitles_format": "external_srt",
            "audio_generated": True,
            "published": False,
            "preview_notice": deck["preview_notice"],
            "subtitles_path": outputs["subtitles"].relative_to(workspace).as_posix(),
            "poster_path": outputs["poster"].relative_to(workspace).as_posix(),
        }
        metadata_file = directory / "video.json"
        metadata_file.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        staged = {
            "video": encoded,
            "subtitles": subtitle,
            "narration": script,
            "metadata": metadata_file,
            "poster": Path(images[0]),
        }
        mime = {
            "video": "video/mp4",
            "subtitles": "application/x-subrip",
            "narration": "text/markdown",
            "metadata": "application/json",
            "poster": "image/png",
        }
        _check_alive(deadline, cancel_event)
        try:
            for key in ("video", "subtitles", "narration", "metadata", "poster"):
                output = outputs[key]
                if not overwrite and output.exists():
                    raise ValueError("输出文件已被其他操作创建，未覆盖")
                if overwrite:
                    os.replace(staged[key], output)
                else:
                    # Atomic no-clobber publication on the same filesystem.
                    os.link(staged[key], output)
                published.append(
                    {
                        "path": output.relative_to(workspace).as_posix(),
                        "size": output.stat().st_size,
                        "media_type": mime[key],
                    }
                )
        except (OSError, ValueError) as exc:
            if published:
                return {
                    "error": f"部分文件已保存，但后续保存失败：{exc}",
                    "artifacts": published,
                    "published": False,
                }
            raise
    return {
        "saved": True,
        "path": target.relative_to(workspace).as_posix(),
        "size": target.stat().st_size,
        "media_type": "video/mp4",
        "artifacts": published,
        "video": metadata,
        "published": False,
    }

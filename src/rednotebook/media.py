"""Local media inspection. Never substitutes captions for unseen media content."""

import hashlib
from pathlib import Path

import av
from PIL import Image

from rednotebook.errors import DomainError


def inspect_media(path, kind):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 200 * 1024 * 1024:
        raise DomainError("media_missing_or_too_large")
    result = {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
        "kind": kind,
        "semantic_analysis": "not_performed",
        "transcription": "not_performed",
    }
    try:
        if kind == "image":
            with Image.open(path) as im:
                if im.width * im.height > 32_000_000:
                    raise DomainError("media_pixel_budget_exceeded")
                im.verify()
            with Image.open(path) as im:
                result.update(width=im.width, height=im.height, format=im.format)
        else:
            with av.open(str(path), options={"protocol_whitelist": "file"}) as container:
                streams = container.streams.video
                if not streams:
                    raise DomainError("video_stream_missing")
                stream = streams[0]
                # Decode the first frame; metadata alone is not a decoding test.
                frame = next(container.decode(video=0))
                result.update(
                    width=frame.width,
                    height=frame.height,
                    decoded_frames=1,
                    duration_seconds=float(container.duration / av.time_base)
                    if container.duration
                    else None,
                    codec=stream.codec_context.name,
                    audio_streams=len(container.streams.audio),
                )
    except (ValueError, OSError, StopIteration, av.error.FFmpegError, Image.DecompressionBombError):
        raise DomainError("media_decode_failed") from None
    return result


def extract_media(path, kind, *, max_frames=8):
    """OCR original image or bounded time-spaced video frames locally with Apple Vision."""
    import json
    import subprocess
    import tempfile

    result = inspect_media(path, kind)
    helper = Path(__file__).with_name("ocr.swift")
    if not Path("/usr/bin/swift").exists():
        raise DomainError("local_ocr_runtime_missing")
    if not 1 <= max_frames <= 12:
        raise DomainError("frame_budget_invalid")

    def ocr(file):
        try:
            run = subprocess.run(
                ["/usr/bin/swift", str(helper), str(file)],
                capture_output=True,
                timeout=60,
                check=True,
            )
            return json.loads(run.stdout)
        except (subprocess.SubprocessError, ValueError):
            raise DomainError("local_ocr_failed") from None

    segments = []
    if kind == "image":
        segments = [{"time_seconds": None, "regions": ocr(path)}]
    else:
        with tempfile.TemporaryDirectory(prefix="rednotebook-frames-") as folder:
            with av.open(str(path), options={"protocol_whitelist": "file"}) as container:
                duration = result.get("duration_seconds")
                if not duration or duration > 1800:
                    raise DomainError("video_duration_missing_or_over_budget")
                for i in range(max_frames):
                    target = duration * i / max_frames
                    container.seek(int(target * av.time_base))
                    for frame in container.decode(video=0):
                        if frame.time is not None and frame.time >= target:
                            file = Path(folder) / "frame.png"
                            frame.to_image().save(file)
                            segments.append({"time_seconds": frame.time, "regions": ocr(file)})
                            break
    return result | {
        "ocr": segments,
        "processor": "Apple Vision accurate zh-Hans/en-US",
        "machine_extracted": True,
        "human_verified": False,
        "coverage": "whole_image" if kind == "image" else "sampled_frames_only",
        "transcription": "not_performed",
        "text": "\n".join(r["text"] for s in segments for r in s["regions"]),
    }

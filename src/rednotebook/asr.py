"""Local ASR: explicit model download separated from offline transcription."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from rednotebook.errors import DomainError
from rednotebook.media import inspect_media


def prepare(size="small", root=Path("private/models")):
    if size not in {"tiny", "base", "small"}:
        raise DomainError("asr_model_not_allowed")
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        raise DomainError("asr_optional_dependency_missing") from None

    target = root / f"faster-whisper-{size}"
    try:
        download_model(size, output_dir=str(target))
    except Exception:
        raise DomainError("asr_model_download_failed") from None
    hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in target.iterdir()
        if p.is_file() and p.name != "manifest.json"
    }
    (target / "manifest.json").write_text(json.dumps({"size": size, "files": hashes}, indent=2))
    return {"path": str(target.resolve()), "size": size, "files": hashes}


def transcribe(db, source_id, file, model_path, language=None):
    db.require_source(source_id)
    metadata = inspect_media(file, "video")
    if not metadata.get("audio_streams"):
        return {"state": "no_audio_track", "segments": [], "metadata": metadata}
    duration = metadata.get("duration_seconds")
    if duration is None or duration > 1800:
        raise DomainError("asr_duration_over_budget")
    manifest_path = model_path / "manifest.json"
    if not manifest_path.is_file():
        raise DomainError("asr_model_not_prepared")
    manifest = json.loads(manifest_path.read_text())
    for name, hashed in manifest["files"].items():
        if (
            Path(name).name != name
            or hashlib.sha256((model_path / name).read_bytes()).hexdigest() != hashed
        ):
            raise DomainError("asr_model_hash_mismatch")
    env = dict(os.environ, HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "rednotebook.asr",
                str(file.resolve()),
                str(model_path.resolve()),
                language or "auto",
            ],
            env=env,
            capture_output=True,
            timeout=600,
            check=True,
        )
        result = json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        raise DomainError("asr_timeout") from None
    except (subprocess.SubprocessError, ValueError):
        raise DomainError("asr_failed") from None
    db.require_source(source_id)
    return result | {
        "metadata": metadata,
        "source_id": source_id,
        "model_files": manifest["files"],
        "human_verified": False,
        "processor": "faster-whisper/" + manifest["size"],
        "media_text": "\n".join(
            f"[ASR {s['start']:.2f}-{s['end']:.2f}s 待核验] {s['text']}" for s in result["segments"]
        ),
    }


if __name__ == "__main__":
    from faster_whisper import WhisperModel

    model = WhisperModel(
        sys.argv[2], device="cpu", compute_type="int8", local_files_only=True, cpu_threads=4
    )
    segments, info = model.transcribe(
        sys.argv[1],
        language=None if sys.argv[3] == "auto" else sys.argv[3],
        beam_size=5,
        vad_filter=True,
        condition_on_previous_text=False,
        word_timestamps=True,
    )
    rows = [
        {
            "start": s.start,
            "end": s.end,
            "text": s.text,
            "avg_logprob": s.avg_logprob,
            "no_speech_prob": s.no_speech_prob,
            "words": [
                {"start": w.start, "end": w.end, "word": w.word, "probability": w.probability}
                for w in (s.words or [])
            ],
        }
        for s in segments
    ]
    print(
        json.dumps(
            {
                "state": "complete",
                "language": info.language,
                "language_probability": info.language_probability,
                "speech_seconds_after_vad": info.duration_after_vad,
                "segments": rows,
            },
            ensure_ascii=False,
        )
    )

"""Longform video processor - creates videos from audio + background images/videos."""

import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple

import httpx

MAX_LONGFORM_DURATION_SECONDS = 7200  # 2 hours
DOWNLOAD_TIMEOUT = 300  # 5 min per file


def _clean_ffmpeg_error(result, max_len: int = 4000) -> str:
    """Extract the meaningful part of ffmpeg's stderr, filtering out progress spam."""
    stderr = result.stderr or ""
    lines = [l for l in stderr.splitlines() if not l.strip().startswith("frame=")]
    cleaned = "\n".join(lines).strip()
    prefix = ""
    if result.returncode is not None and result.returncode < 0:
        prefix = f"[Process killed by signal {-result.returncode}] "
    else:
        prefix = f"[Exit code {result.returncode}] "
    return prefix + (cleaned[-max_len:] if cleaned else stderr[-max_len:])


def download_media(url: str, dest: Path) -> None:
    """Download a single media file (audio, image, or video) from URL to dest."""
    with httpx.stream("GET", url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True) as r:
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)


def get_media_duration(path: Path) -> float:
    """
    Get duration of an audio or video file in seconds using ffprobe.
    Raises ValueError if duration cannot be determined.
    """
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(f"Invalid media file: {result.stderr or result.stdout}")

    raw = (result.stdout or "").strip()
    if not raw or raw.upper() == "N/A":
        raise ValueError("Could not determine media duration (ffprobe returned N/A).")

    try:
        duration = float(raw)
    except ValueError as exc:
        raise ValueError(f"Could not parse media duration from ffprobe output: {raw!r}") from exc

    return duration


def concatenate_audio(audio_paths: List[Path], output_path: Path) -> float:
    """
    Concatenate multiple audio files into one.
    Returns the total duration in seconds.
    """
    list_file = output_path.parent / "audio_list.txt"
    with open(list_file, "w") as f:
        for p in audio_paths:
            f.write(f"file '{p.absolute()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",

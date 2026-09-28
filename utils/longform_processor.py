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
        "-i", str(list_file),
        "-c", "copy",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"Audio concatenation failed: {_clean_ffmpeg_error(result)}")

    total_duration = get_media_duration(output_path)
    list_file.unlink()
    return total_duration


def create_video_from_images(
    image_paths: List[Path],
    audio_path: Path,
    output_path: Path,
    quality: str,
    audio_duration: float,
) -> float:
    """
    Create a video from images and audio.
    Images are looped/cycled to match audio duration.
    Fixed aspect ratio: 16:9
    Resolution: 720p or 1080p
    Returns final video duration (capped at 2 hours).
    """
    width, height = (1280, 720) if quality == "720" else (1920, 1080)
    num_images = len(image_paths)
    duration_per_image = audio_duration / num_images
    final_duration = min(audio_duration, MAX_LONGFORM_DURATION_SECONDS)

    inputs = []
    filter_parts = []

    for i, img_path in enumerate(image_paths):
        inputs.extend(["-loop", "1", "-t", str(duration_per_image), "-i", str(img_path)])
        filter_parts.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v{i}]"
        )

    inputs.extend(["-i", str(audio_path)])
    audio_idx = num_images

    filter_parts.append(
        "".join([f"[v{i}]" for i in range(num_images)]) +
        f"concat=n={num_images}:v=1:a=0[v]"
    )
    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", f"{audio_idx}:a",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-t", str(final_duration),
        "-shortest",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0:
        raise RuntimeError(f"Video creation failed: {_clean_ffmpeg_error(result)}")

    return final_duration


def create_video_from_videos(
    video_paths: List[Path],
    audio_path: Path,
    output_path: Path,
    quality: str,
    audio_duration: float,
) -> float:
    """
    Create a video from background videos and audio.
    Videos are looped via -stream_loop (re-reads the file from disk instead of
    buffering frames in memory) and trimmed to match audio duration.
    Fixed aspect ratio: 16:9
    Resolution: 720p or 1080p
    Returns final video duration (capped at 2 hours).
    """
    width, height = (1280, 720) if quality == "720" else (1920, 1080)
    final_duration = min(audio_duration, MAX_LONGFORM_DURATION_SECONDS)

    # If there are multiple background videos, concatenate them into one first
    if len(video_paths) > 1:
        combined_bg = output_path.parent / "combined_bg.mp4"
        list_file = output_path.parent / "bg_list.txt"
        with open(list_file, "w") as f:
            for p in video_paths:
                f.write(f"file '{p.absolute()}'\n")

        concat_cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(combined_bg),
        ]
        result = subprocess.run(concat_cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"Background concat failed: {_clean_ffmpeg_error(result)}")

        list_file.unlink()
        bg_input = combined_bg
    else:
        bg_input = video_paths[0]

    # Loop the file with -stream_loop (re-reads from disk, no in-memory buffering)
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", str(bg_input),
        "-i", str(audio_path),
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-t", str(final_duration),
        "-shortest",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    if result.returncode != 0:
        raise RuntimeError(f"Video creation from videos failed: {_clean_ffmpeg_error(result)}")

    return final_duration


def process_longform_video(
    audio_urls: List[str],
    background_source: str,
    background_urls: List[str],
    quality: str,
    temp_dir: Path,
) -> Tuple[Path, float]:
    """
    Main processing function for longform videos.

    Args:
        audio_urls: List of audio file URLs (1-30)
        background_source: Either 'images' or 'videos'
        background_urls: List of background media URLs (1-15 for images, 1-5 for videos)
        quality: '720' or '1080'
        temp_dir: Temporary directory for processing

    Returns:
        (output_path, duration_seconds)
    """
    audio_paths = []
    for i, url in enumerate(audio_urls):
        dest = temp_dir / f"audio_{i}.mp3"
        download_media(url, dest)
        audio_paths.append(dest)

    combined_audio = temp_dir / "combined_audio.mp3"
    total_audio_duration = concatenate_audio(audio_paths, combined_audio)

    if total_audio_duration > MAX_LONGFORM_DURATION_SECONDS:
        total_audio_duration = MAX_LONGFORM_DURATION_SECONDS

    bg_paths = []
    for i, url in enumerate(background_urls):
        if background_source == "images":
            ext = "jpg"
            dest = temp_dir / f"bg_{i}.{ext}"
        else:
            dest = temp_dir / f"bg_video_{i}.mp4"
        download_media(url, dest)
        bg_paths.append(dest)

    output_path = temp_dir / "longform_output.mp4"

    if background_source == "images":
        final_duration = create_video_from_images(
            bg_paths,
            combined_audio,
            output_path,
            quality,
            total_audio_duration,
        )
    else:  # videos
        final_duration = create_video_from_videos(
            bg_paths,
            combined_audio,
            output_path,
            quality,
            total_audio_duration,
        )

    return output_path, final_duration

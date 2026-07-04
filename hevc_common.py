"""Shared probing/encoding logic for HEVC batch re-encoding.

Used by both the CLI and the TUI so progress reporting stays consistent.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

CRF = 20
PRESET = "medium"
PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]

# Hardware encode via macOS VideoToolbox (hevc_videotoolbox). No software
# preset applies here - VideoToolbox has no "slow/medium/fast" knob, just a
# quality target. NOTE: untested on real hardware (this was developed on
# Linux) - -q:v constant-quality mode requires a reasonably recent
# ffmpeg/macOS; if it errors, the fallback is bitrate-based (-b:v) control.
HW_ENCODER = "hevc_videotoolbox"
HW_QUALITY = 65  # 1-100, higher = better (opposite direction from CRF)

# (label, value) pairs for the TUI's encoder dropdown. "software" uses
# preset+crf; anything else is a hardware backend using hw_quality instead.
# Intel VAAPI was evaluated and ruled out on the Linux media server itself
# (see README) - only VideoToolbox is offered for now.
ENCODERS = [
    ("Software (libx265)", "software"),
    ("Hardware - VideoToolbox (macOS)", "videotoolbox"),
]

DURATION_TOLERANCE = 2.0  # allowed seconds of drift between source/output duration


def find_mkv_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.mkv") if not p.name.startswith("."))


def probe_codec(path: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return out.stdout.strip() or None


def probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip())
    except (subprocess.CalledProcessError, OSError, ValueError):
        return 0.0


async def probe_codec_and_duration_async(path: Path) -> tuple[Optional[str], float]:
    """Like probe_codec + probe_duration combined into a single ffprobe call,
    run as a non-blocking subprocess so callers (e.g. a TUI event loop
    scanning hundreds of files) stay responsive while probes are in flight.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name:format=duration",
            "-of", "default=noprint_wrappers=1",
            str(path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except OSError:
        return None, 0.0

    codec: Optional[str] = None
    duration = 0.0
    for line in stdout.decode(errors="ignore").splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "codec_name":
            codec = value.strip() or None
        elif key == "duration":
            with contextlib.suppress(ValueError):
                duration = float(value.strip())
    return codec, duration


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_bytes(num_bytes: float) -> str:
    value = float(num_bytes)
    sign = "-" if value < 0 else ""
    value = abs(value)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{sign}{value:.1f} {unit}"
        value /= 1024
    return f"{sign}{value:.1f} TB"


def output_path_for(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem} [HEVC 10bit CRF{CRF}].mkv")


def tmp_output_path_for(input_path: Path) -> Path:
    return input_path.with_name(f".{input_path.name}.tmp.mkv")


def durations_match(a: float, b: float, tol: float = DURATION_TOLERANCE) -> bool:
    return abs(a - b) <= tol


def _parse_out_time(value: str) -> float:
    """Parse ffmpeg -progress's out_time field, e.g. '00:01:23.456789'."""
    try:
        h, m, s = value.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except ValueError:
        return 0.0


@dataclass
class EncodeResult:
    returncode: int
    stderr: str


ProgressCallback = Callable[[float, float], None]  # (out_time_seconds, speed)


async def encode_async(
    input_path: Path,
    output_path: Path,
    on_progress: Optional[ProgressCallback] = None,
    preset: str = PRESET,
    crf: int = CRF,
    hardware: bool = False,
    hw_quality: int = HW_QUALITY,
) -> EncodeResult:
    """Run ffmpeg re-encoding input_path -> output_path as HEVC 10-bit.

    Audio, subtitles, chapters, and metadata are copied untouched. No banner
    or per-line stats are printed by ffmpeg itself; progress is reported
    exclusively through on_progress via ffmpeg's machine-readable -progress
    stream, so nothing is ever waiting on stdin.

    hardware=True switches to macOS VideoToolbox hardware encoding
    (hevc_videotoolbox) instead of software libx265 - preset/crf are ignored
    in that case, hw_quality is used instead.
    """
    if hardware:
        video_opts = ["-c:v", HW_ENCODER, "-profile:v", "main10", "-pix_fmt", "p010le",
                      "-q:v", str(hw_quality)]
    else:
        video_opts = ["-c:v", "libx265", "-pix_fmt", "yuv420p10le",
                      "-preset", preset, "-crf", str(crf)]

    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(input_path),
        "-map", "0",
        "-map_metadata", "0",
        "-map_chapters", "0",
        *video_opts,
        "-c:a", "copy",
        "-c:s", "copy",
        "-max_muxing_queue_size", "9999",
        "-progress", "pipe:1", "-nostats",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    assert proc.stdout is not None
    speed = 0.0
    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="ignore").strip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            if key == "out_time":
                out_time = _parse_out_time(value)
                if on_progress is not None:
                    on_progress(out_time, speed)
            elif key == "speed":
                try:
                    speed = float(value.rstrip("x\n"))
                except ValueError:
                    speed = 0.0
    except asyncio.CancelledError:
        proc.terminate()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        _close_transport(proc)
        raise

    stderr_bytes = await proc.stderr.read() if proc.stderr else b""
    returncode = await proc.wait()
    _close_transport(proc)
    return EncodeResult(returncode=returncode, stderr=stderr_bytes.decode(errors="ignore"))


def _close_transport(proc: "asyncio.subprocess.Process") -> None:
    """Explicitly release the subprocess's pipe transport.

    Without this, the transport can end up garbage-collected after the
    asyncio loop has already been closed (e.g. right after quitting the TUI
    mid-encode), which prints a harmless but noisy "Event loop is closed"
    traceback from BaseSubprocessTransport.__del__.
    """
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()

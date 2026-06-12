from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_ffmpeg_missing_logged = False

# Downsampling to GIF_FPS shrinks long clips, but a short high-fps loop has too
# few frames to spare — a 5-frame 0.15s gif at 15fps is 2 frames and reads as a
# still image. Only cap fps when the result still has enough frames to animate;
# otherwise keep the source timing, ceilinged below the browser frame-delay clamp.
_MIN_DOWNSAMPLED_FRAMES = 16
_MAX_GIF_FPS = 50


def _parse_rate(value: str) -> float:
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(value)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _choose_fps(src_fps: float, duration: float, cap: int) -> int | None:
    """The fps to convert at, or None to keep the source's native frame timing."""
    if src_fps <= 0 or src_fps <= cap:
        return None  # unknown or already at/below the cap — don't resample
    if not duration or duration * cap < _MIN_DOWNSAMPLED_FRAMES:
        # short loop (or unknown length): keep its frames, but stay browser-safe
        return None if src_fps <= _MAX_GIF_FPS else _MAX_GIF_FPS
    return cap


async def _probe_source(path: Path) -> tuple[float, float]:
    """Return (avg_frame_rate, duration_seconds); zeros when probing fails."""
    args = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate:format=duration",
        "-of",
        "default=nokey=1:noprint_wrappers=1",
        str(path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return 0.0, 0.0
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return 0.0, 0.0
    parts = out.decode(errors="replace").split()
    fps = _parse_rate(parts[0]) if parts else 0.0
    try:
        duration = float(parts[1]) if len(parts) > 1 else 0.0
    except ValueError:
        duration = 0.0
    return fps, duration


@dataclass(frozen=True)
class Download:
    data: bytes | None
    too_big: bool


async def fetch_media(client: httpx.AsyncClient, url: str, max_bytes: int) -> Download:
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > max_bytes:
            return Download(None, True)
        buf = bytearray()
        async for chunk in response.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                return Download(None, True)
        return Download(bytes(buf), False)


def gif_ffmpeg_args(src: str, dst: str, *, fps: int | None, max_width: int) -> list[str]:
    rate = f"fps={fps}," if fps else ""  # None/0 keeps the source's native timing
    vf = (
        f"{rate}scale='min(iw,{max_width})':'min(ih,{max_width})'"
        ":force_original_aspect_ratio=decrease:flags=lanczos,"
        "split[s0][s1];[s0]palettegen=stats_mode=diff[p];"
        "[s1][p]paletteuse=dither=bayer:bayer_scale=4"
    )
    return ["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-vf", vf, "-loop", "0", dst]


async def convert_to_gif(mp4: bytes, *, fps: int, max_width: int, max_bytes: int) -> bytes | None:
    with tempfile.TemporaryDirectory(prefix="gifharvest-") as tmp:
        src = Path(tmp) / "in.mp4"
        dst = Path(tmp) / "out.gif"
        src.write_bytes(mp4)
        src_fps, duration = await _probe_source(src)
        args = gif_ffmpeg_args(
            str(src), str(dst), fps=_choose_fps(src_fps, duration, fps), max_width=max_width
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            # no ffmpeg on PATH must degrade to the mp4 upload, not kill posting
            global _ffmpeg_missing_logged
            if not _ffmpeg_missing_logged:
                _ffmpeg_missing_logged = True
                logger.error(
                    "ffmpeg unavailable (%s) — CONVERT_TO_GIF is on but mp4s "
                    "will be uploaded unconverted",
                    exc,
                )
            return None
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(
                "ffmpeg gif conversion failed (rc=%s): %s",
                proc.returncode,
                stderr.decode(errors="replace").strip(),
            )
            return None
        gif = dst.read_bytes()
        if len(gif) > max_bytes:
            logger.info("converted gif too big: %d > %d bytes", len(gif), max_bytes)
            return None
        return gif

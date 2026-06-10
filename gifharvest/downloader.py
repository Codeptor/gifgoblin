from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_ffmpeg_missing_logged = False


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


def gif_ffmpeg_args(src: str, dst: str, *, fps: int, max_width: int) -> list[str]:
    vf = (
        f"fps={fps},scale='min(iw,{max_width})':'min(ih,{max_width})'"
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
        args = gif_ffmpeg_args(str(src), str(dst), fps=fps, max_width=max_width)
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

from __future__ import annotations

import shutil
import subprocess
from collections.abc import AsyncIterator

import httpx
import pytest

from gifharvest.downloader import Download, convert_to_gif, fetch_media, gif_ffmpeg_args

MEDIA_URL = "https://video.twimg.com/tweet_video/abc.mp4"


class ChunkStream(httpx.AsyncByteStream):
    """Response stream without an auto-added Content-Length header."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_fetch_media_success():
    body = b"mp4-bytes" * 100

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with make_client(handler) as client:
        result = await fetch_media(client, MEDIA_URL, max_bytes=len(body))

    assert result == Download(body, False)


class ExplodingStream(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        raise AssertionError("body must not be streamed when Content-Length exceeds max_bytes")
        yield b""  # pragma: no cover


async def test_fetch_media_too_big_via_content_length():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "1000"}, stream=ExplodingStream())

    async with make_client(handler) as client:
        result = await fetch_media(client, MEDIA_URL, max_bytes=999)

    assert result == Download(None, True)


async def test_fetch_media_too_big_via_streamed_body():
    chunks = [b"x" * 400, b"y" * 400, b"z" * 400]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkStream(chunks))

    async with make_client(handler) as client:
        result = await fetch_media(client, MEDIA_URL, max_bytes=1000)

    assert result == Download(None, True)


async def test_fetch_media_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    async with make_client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_media(client, MEDIA_URL, max_bytes=1_000_000)


def test_gif_ffmpeg_args():
    args = gif_ffmpeg_args("/tmp/in.mp4", "/tmp/out.gif", fps=15, max_width=480)

    assert args[0] == "ffmpeg"
    assert args[-1] == "/tmp/out.gif"
    assert args[args.index("-i") + 1] == "/tmp/in.mp4"
    assert args[args.index("-loop") + 1] == "0"
    vf = args[args.index("-vf") + 1]
    assert vf.startswith("fps=15,scale='min(480,iw)':-2:flags=lanczos,")
    assert "palettegen=stats_mode=diff" in vf
    assert "paletteuse=dither=bayer:bayer_scale=4" in vf


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def tiny_mp4(tmp_path) -> bytes:
    path = tmp_path / "tiny.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=0.5:size=64x64:rate=10",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


@needs_ffmpeg
async def test_convert_to_gif_produces_gif(tiny_mp4):
    gif = await convert_to_gif(tiny_mp4, fps=10, max_width=64, max_bytes=10_000_000)

    assert gif is not None
    assert gif.startswith(b"GIF8")


@needs_ffmpeg
async def test_convert_to_gif_respects_max_bytes(tiny_mp4):
    assert await convert_to_gif(tiny_mp4, fps=10, max_width=64, max_bytes=1) is None


async def test_convert_to_gif_missing_ffmpeg_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert await convert_to_gif(b"not-a-real-mp4", fps=10, max_width=64, max_bytes=1000) is None

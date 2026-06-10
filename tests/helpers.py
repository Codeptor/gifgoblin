from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from gifharvest.models import GifCandidate, MediaKind

BASE = datetime(2026, 6, 1, tzinfo=UTC)


def anim(url: str = "https://video.twimg.com/tweet_video/abc.mp4") -> SimpleNamespace:
    return SimpleNamespace(thumbnailUrl="thumb", videoUrl=url)


def video_variant(
    url: str = "https://video.twimg.com/ext_tw_video/v720.mp4",
    bitrate: int = 832_000,
    content_type: str = "video/mp4",
) -> SimpleNamespace:
    return SimpleNamespace(url=url, bitrate=bitrate, contentType=content_type)


def video(variants: list | None = None, duration: int = 5000) -> SimpleNamespace:
    return SimpleNamespace(
        thumbnailUrl="thumb",
        variants=variants if variants is not None else [video_variant()],
        duration=duration,
    )


def tweet(
    tid: int = 1,
    user: str = "shitposter",
    minutes: int = 0,
    animated: tuple | list = (),
    videos: tuple | list = (),
    retweeted: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=tid,
        url=f"https://x.com/{user}/status/{tid}",
        date=BASE + timedelta(minutes=minutes),
        user=SimpleNamespace(username=user),
        media=SimpleNamespace(photos=[], videos=list(videos), animated=list(animated)),
        retweetedTweet=retweeted,
        quotedTweet=None,
    )


def candidate(
    tid: int = 1,
    media_url: str = "https://video.twimg.com/tweet_video/abc.mp4",
    minutes: int = 0,
    author: str = "shitposter",
    tracked: str = "shitposter",
    kind: MediaKind = MediaKind.GIF,
) -> GifCandidate:
    return GifCandidate(
        tweet_id=tid,
        author=author,
        tracked_handle=tracked,
        tweet_url=f"https://x.com/{author}/status/{tid}",
        media_url=media_url,
        kind=kind,
        tweet_date=BASE + timedelta(minutes=minutes),
        via_retweet=False,
    )

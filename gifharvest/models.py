from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MediaKind(StrEnum):
    GIF = "gif"
    VIDEO = "video"


@dataclass(frozen=True)
class GifCandidate:
    tweet_id: int
    author: str
    tracked_handle: str
    tweet_url: str
    media_url: str
    kind: MediaKind
    tweet_date: datetime
    via_retweet: bool

    @property
    def fallback_url(self) -> str:
        return f"https://d.fxtwitter.com/i/status/{self.tweet_id}"

    @property
    def filename(self) -> str:
        return f"{self.author}_{self.tweet_id}.mp4"

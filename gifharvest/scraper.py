from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from .models import GifCandidate, MediaKind

if TYPE_CHECKING:
    from .config import Config
    from .db import Store

logger = logging.getLogger(__name__)

HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,15}")

_PROFILE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?#].*)?",
    re.IGNORECASE,
)


def normalize_handle(raw: str) -> str | None:
    text = raw.strip()
    if not text:
        return None
    url_match = _PROFILE_URL_RE.fullmatch(text)
    if url_match:
        text = url_match.group(1)
    text = text.removeprefix("@")
    if not HANDLE_RE.fullmatch(text):
        return None
    return text.lower()


_STATUS_URL_RE = re.compile(
    r"(?:https?://)?(?:(?:www|mobile|d)\.)?"
    r"(?:x|twitter|fxtwitter|vxtwitter|fixupx|fixvx)\.com/"
    r"(?:i/|[A-Za-z0-9_]{1,15}/)?status(?:es)?/(\d+)",
    re.IGNORECASE,
)


def parse_tweet_url(raw: str) -> int | None:
    text = raw.strip()
    if text.isdigit():
        return int(text)
    match = _STATUS_URL_RE.match(text)
    return int(match.group(1)) if match else None


def extract_candidates(
    tweet: Any,
    tracked_handle: str,
    *,
    include_retweets: bool,
    include_videos: bool,
) -> list[GifCandidate]:
    src = tweet
    via_retweet = False
    if getattr(tweet, "retweetedTweet", None) is not None:
        if not include_retweets:
            return []
        src = tweet.retweetedTweet
        via_retweet = True

    media = getattr(src, "media", None)
    if media is None:
        return []

    def build(media_url: str, kind: MediaKind) -> GifCandidate:
        return GifCandidate(
            tweet_id=src.id,
            author=src.user.username,
            tracked_handle=tracked_handle,
            tweet_url=src.url,
            media_url=media_url,
            kind=kind,
            tweet_date=src.date,
            via_retweet=via_retweet,
        )

    candidates = [build(entry.videoUrl, MediaKind.GIF) for entry in media.animated]
    if include_videos:
        for vid in media.videos:
            best = max(
                (v for v in vid.variants if v.contentType == "video/mp4"),
                key=lambda v: v.bitrate,
                default=None,
            )
            if best is not None:
                candidates.append(build(best.url, MediaKind.VIDEO))
    return candidates


def plan_posts(
    candidates: list[GifCandidate],
    *,
    first_run: bool,
    backfill_count: int,
) -> tuple[list[GifCandidate], list[GifCandidate]]:
    ordered = sorted(candidates, key=lambda c: c.tweet_date)
    if not first_run:
        return ordered, []
    if backfill_count <= 0:
        return [], ordered
    return ordered[-backfill_count:], ordered[:-backfill_count]


class TwitterScraper:
    def __init__(self, api: Any, cfg: Config):
        self._api = api
        self._cfg = cfg

    async def resolve_user_id(self, store: Store, handle: str) -> int | None:
        cached = await store.get_user_id(handle)
        if cached is not None:
            return cached
        user = await self._api.user_by_login(handle)
        if user is None:
            logger.warning("could not resolve @%s — suspended, renamed, or a typo?", handle)
            return None
        await store.set_user_id(handle, user.id)
        return user.id

    async def has_active_accounts(self) -> bool:
        stats = await self._api.pool.stats()
        return stats.get("active", 0) > 0

    async def account_health(self) -> dict[str, int | list[str]]:
        infos = await self._api.pool.accounts_info()
        errors = sorted(
            x["username"]
            for x in infos
            if not x.get("active") or not x.get("logged_in") or x.get("error_msg")
        )
        return {
            "total": len(infos),
            "active": sum(1 for x in infos if x.get("active")),
            "logged_in": sum(1 for x in infos if x.get("logged_in")),
            "errors": errors,
        }

    async def fetch_tweet(self, tweet_id: int) -> list[GifCandidate] | None:
        """Every gif/video on one explicitly requested tweet, or None when unfetchable.

        The poll-loop opt-ins don't apply here: a pasted link is explicit intent,
        so retweets resolve to their original and plain videos are included.
        """
        tweet = await self._api.tweet_details(tweet_id)
        if tweet is None:
            logger.warning("could not fetch tweet %d — deleted, protected, or transient", tweet_id)
            return None
        return extract_candidates(
            tweet,
            tweet.user.username.lower(),
            include_retweets=True,
            include_videos=True,
        )

    async def fetch_new(self, store: Store, handle: str) -> list[GifCandidate] | None:
        """Return new candidates for a handle, or None when resolution failed."""
        uid = await self.resolve_user_id(store, handle)
        if uid is None:
            # None (not []) so callers don't mistake a transient resolution
            # failure for "nothing new" and burn first-run backfill protection
            return None
        fresh: list[GifCandidate] = []
        batch_urls: set[str] = set()
        async for tweet in self._api.user_tweets(uid, limit=self._cfg.scrape_limit):
            # user_tweets yields every Tweet object in the GQL response, so
            # RT/quote originals and promoted tweets surface standalone —
            # keep only the tracked author's own timeline entries
            if tweet.user.id != uid:
                continue
            for cand in extract_candidates(
                tweet,
                handle,
                include_retweets=self._cfg.include_retweets,
                include_videos=self._cfg.include_videos,
            ):
                if cand.media_url in batch_urls or await store.is_seen(cand):
                    continue
                batch_urls.add(cand.media_url)
                fresh.append(cand)
        return sorted(fresh, key=lambda c: c.tweet_date)

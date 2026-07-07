from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from helpers import BASE, anim, candidate, tweet, user_id_for, video, video_variant

from gifharvest.db import Store
from gifharvest.models import MediaKind
from gifharvest.scraper import (
    TwitterScraper,
    extract_candidates,
    normalize_handle,
    parse_tweet_url,
    parse_tweet_urls,
    plan_posts,
)


def _extract(tw, tracked="shitposter", *, retweets=False, videos=False, video_gif_max_seconds=0.0):
    return extract_candidates(
        tw,
        tracked,
        include_retweets=retweets,
        include_videos=videos,
        video_gif_max_seconds=video_gif_max_seconds,
    )


# -- extract_candidates: GIFs ---------------------------------------------------


def test_gif_extraction_all_fields():
    tw = tweet(
        tid=42,
        user="Poaster",
        minutes=5,
        animated=[anim("https://video.twimg.com/tweet_video/xyz.mp4")],
    )
    (cand,) = _extract(tw, tracked="poaster")
    assert cand.tweet_id == 42
    assert cand.author == "Poaster"
    assert cand.tracked_handle == "poaster"
    assert cand.tweet_url == "https://x.com/Poaster/status/42"
    assert cand.media_url == "https://video.twimg.com/tweet_video/xyz.mp4"
    assert cand.kind is MediaKind.GIF
    assert cand.tweet_date == BASE + timedelta(minutes=5)
    assert cand.via_retweet is False


def test_multiple_animated_entries_yield_one_candidate_each():
    tw = tweet(animated=[anim("https://v/a.mp4"), anim("https://v/b.mp4")])
    urls = [c.media_url for c in _extract(tw)]
    assert urls == ["https://v/a.mp4", "https://v/b.mp4"]


def test_tweet_without_media_entries_yields_nothing():
    assert _extract(tweet()) == []


# -- extract_candidates: videos -------------------------------------------------


def test_videos_excluded_by_default():
    tw = tweet(videos=[video()])
    assert _extract(tw) == []


def test_include_videos_picks_only_highest_bitrate_mp4_variant():
    variants = [
        video_variant("https://v/low.mp4", bitrate=256_000),
        video_variant("https://v/high.mp4", bitrate=2_176_000),
        video_variant(
            "https://v/playlist.m3u8", bitrate=9_999_999, content_type="application/x-mpegURL"
        ),
    ]
    tw = tweet(tid=7, videos=[video(variants)])
    (cand,) = _extract(tw, videos=True)
    assert cand.media_url == "https://v/high.mp4"
    assert cand.kind is MediaKind.VIDEO
    assert cand.tweet_id == 7


def test_video_with_only_non_mp4_variants_is_skipped():
    variants = [video_variant("https://v/p.m3u8", content_type="application/x-mpegURL")]
    tw = tweet(videos=[video(variants)])
    assert _extract(tw, videos=True) == []


def test_short_video_retagged_as_gif_for_conversion():
    tw = tweet(videos=[video(duration=3000)])
    (cand,) = _extract(tw, videos=True, video_gif_max_seconds=5.0)
    assert cand.kind is MediaKind.GIF


def test_video_at_threshold_is_gif():
    tw = tweet(videos=[video(duration=5000)])
    (cand,) = _extract(tw, videos=True, video_gif_max_seconds=5.0)
    assert cand.kind is MediaKind.GIF


def test_long_video_stays_video():
    tw = tweet(videos=[video(duration=8000)])
    (cand,) = _extract(tw, videos=True, video_gif_max_seconds=5.0)
    assert cand.kind is MediaKind.VIDEO


def test_short_video_stays_video_when_threshold_disabled():
    # the poll loop passes 0 — videos must remain mp4 uploads there
    tw = tweet(videos=[video(duration=2000)])
    (cand,) = _extract(tw, videos=True, video_gif_max_seconds=0.0)
    assert cand.kind is MediaKind.VIDEO


def test_gif_and_video_in_same_tweet():
    tw = tweet(animated=[anim("https://v/g.mp4")], videos=[video()])
    kinds = [c.kind for c in _extract(tw, videos=True)]
    assert kinds == [MediaKind.GIF, MediaKind.VIDEO]


# -- extract_candidates: retweets -----------------------------------------------


def test_retweets_dropped_by_default():
    original = tweet(tid=10, user="og_author", animated=[anim()])
    rt = tweet(tid=99, user="tracked_guy", minutes=60, retweeted=original)
    assert _extract(rt, tracked="tracked_guy") == []


def test_include_retweets_resolves_original_tweet():
    original = tweet(tid=10, user="og_author", minutes=-120, animated=[anim("https://v/og.mp4")])
    rt = tweet(tid=99, user="tracked_guy", minutes=60, retweeted=original)
    (cand,) = _extract(rt, tracked="tracked_guy", retweets=True)
    assert cand.tweet_id == 10
    assert cand.author == "og_author"
    assert cand.tweet_url == "https://x.com/og_author/status/10"
    assert cand.tweet_date == BASE + timedelta(minutes=-120)
    assert cand.media_url == "https://v/og.mp4"
    assert cand.via_retweet is True
    assert cand.tracked_handle == "tracked_guy"


# -- normalize_handle ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@Foo", "foo"),
        ("foo", "foo"),
        ("Some_User99", "some_user99"),
        ("  @Foo  ", "foo"),
        ("https://x.com/Foo?s=20", "foo"),
        ("x.com/foo/status/123", "foo"),
        ("twitter.com/foo", "foo"),
        ("https://mobile.twitter.com/foo/status/123", "foo"),
        ("HTTPS://WWW.X.COM/Foo", "foo"),
        ("http://www.twitter.com/Bar_Baz", "bar_baz"),
        ("A" * 15, "a" * 15),
    ],
)
def test_normalize_handle_accepts(raw: str, expected: str):
    assert normalize_handle(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "@",
        "has space",
        "foo-bar",
        "a" * 16,
        "https://example.com/foo",
        "x.com",
        "x.com/",
        "héllo",
    ],
)
def test_normalize_handle_rejects(raw: str):
    assert normalize_handle(raw) is None


# -- fetch_new ---------------------------------------------------------------------


class FakeAPI:
    def __init__(self, tweets: list, resolvable: bool = True, detail=None, thread=None):
        self._tweets = tweets
        self._resolvable = resolvable
        self._detail = detail
        self._thread = thread or []
        self.requested_thread_limit = None

    async def user_by_login(self, handle: str):
        if not self._resolvable:
            return None
        return SimpleNamespace(id=user_id_for(handle), username=handle)

    async def user_tweets(self, uid: int, limit: int = -1):
        for tw in self._tweets:
            yield tw

    async def tweet_details(self, twid: int):
        return self._detail

    async def tweet_thread(self, twid: int, limit: int = -1):
        self.requested_thread_limit = limit
        for tw in self._thread:
            yield tw


def make_scraper(
    tweets: list,
    *,
    retweets: bool = False,
    resolvable: bool = True,
    detail=None,
    thread=None,
    video_gif_max_seconds: float = 0.0,
):
    cfg = SimpleNamespace(
        scrape_limit=20,
        include_retweets=retweets,
        include_videos=False,
        video_gif_max_seconds=video_gif_max_seconds,
        twitter_thread_limit=20,
    )
    return TwitterScraper(FakeAPI(tweets, resolvable, detail, thread), cfg)


@pytest.fixture
async def store(tmp_path):
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


async def test_fetch_new_drops_foreign_author_tweets(store: Store):
    # twscrape yields the RT's original tweet standalone (retweetedTweet=None);
    # it must not leak past INCLUDE_RETWEETS=false
    original = tweet(tid=10, user="og_author", animated=[anim("https://v/og.mp4")])
    rt = tweet(tid=99, user="tracked_guy", minutes=60, retweeted=original)
    scraper = make_scraper([rt, original])
    assert await scraper.fetch_new(store, "tracked_guy") == []


async def test_fetch_new_with_retweets_attributes_via_wrapper(store: Store):
    original = tweet(tid=10, user="og_author", minutes=-120, animated=[anim("https://v/og.mp4")])
    rt = tweet(tid=99, user="tracked_guy", minutes=60, retweeted=original)
    scraper = make_scraper([rt, original], retweets=True)
    (cand,) = await scraper.fetch_new(store, "tracked_guy")
    assert cand.tweet_id == 10
    assert cand.author == "og_author"
    assert cand.via_retweet is True


async def test_fetch_new_keeps_own_tweets_and_skips_seen(store: Store):
    own = tweet(tid=1, user="shitposter", animated=[anim("https://v/a.mp4")])
    seen = tweet(tid=2, user="shitposter", minutes=5, animated=[anim("https://v/b.mp4")])
    scraper = make_scraper([own, seen])
    await store.mark_seen(candidate(tid=2, media_url="https://v/b.mp4"))
    (cand,) = await scraper.fetch_new(store, "shitposter")
    assert cand.tweet_id == 1


async def test_fetch_new_returns_none_when_resolution_fails(store: Store):
    scraper = make_scraper([], resolvable=False)
    assert await scraper.fetch_new(store, "ghost") is None


# -- parse_tweet_url / fetch_tweet ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://x.com/foo/status/123", 123),
        ("https://twitter.com/foo/status/123?s=20&t=abc", 123),
        ("x.com/i/status/456", 456),
        ("https://mobile.twitter.com/foo/statuses/789", 789),
        ("https://fxtwitter.com/foo/status/321", 321),
        ("https://d.fxtwitter.com/foo/status/321", 321),
        ("https://vxtwitter.com/foo/status/55/photo/1", 55),
        ("987654321", 987654321),
    ],
)
def test_parse_tweet_url_accepts(raw: str, expected: int):
    assert parse_tweet_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "https://x.com/foo",
        "not a url",
        "https://youtube.com/status/1",
        "x.com/foo/status/abc",
    ],
)
def test_parse_tweet_url_rejects(raw: str):
    assert parse_tweet_url(raw) is None


def test_parse_tweet_urls_extracts_links_from_chat_text():
    assert parse_tweet_urls(
        "check this https://x.com/foo/status/123?s=20 and https://fxtwitter.com/bar/status/456"
    ) == [123, 456]


async def test_fetch_tweet_includes_videos_despite_optin_flags():
    tw = tweet(tid=7, user="poster", animated=[anim("https://v/g.mp4")], videos=[video()])
    scraper = make_scraper([], detail=tw)  # cfg has include_videos=False
    cands = await scraper.fetch_tweet(7)
    assert {c.kind for c in cands} == {MediaKind.GIF, MediaKind.VIDEO}
    assert all(c.tweet_id == 7 for c in cands)


async def test_fetch_tweet_resolves_retweet_to_original():
    original = tweet(tid=10, user="og_author", animated=[anim("https://v/og.mp4")])
    rt = tweet(tid=99, user="Linker", minutes=60, retweeted=original)
    scraper = make_scraper([], detail=rt)
    (cand,) = await scraper.fetch_tweet(99)
    assert cand.tweet_id == 10
    assert cand.author == "og_author"
    assert cand.via_retweet is True
    assert cand.tracked_handle == "linker"


async def test_fetch_tweet_converts_short_video_to_gif():
    tw = tweet(tid=8, user="poster", videos=[video(duration=3000)])
    scraper = make_scraper([], detail=tw, video_gif_max_seconds=5.0)
    (cand,) = await scraper.fetch_tweet(8)
    assert cand.kind is MediaKind.GIF
    assert cand.tweet_id == 8


async def test_fetch_tweet_keeps_long_video_as_video():
    tw = tweet(tid=9, user="poster", videos=[video(duration=20000)])
    scraper = make_scraper([], detail=tw, video_gif_max_seconds=5.0)
    (cand,) = await scraper.fetch_tweet(9)
    assert cand.kind is MediaKind.VIDEO


async def test_fetch_tweet_none_when_unfetchable():
    scraper = make_scraper([], detail=None)
    assert await scraper.fetch_tweet(1) is None


async def test_fetch_tweet_empty_for_no_media():
    scraper = make_scraper([], detail=tweet(tid=3, user="texter"))
    assert await scraper.fetch_tweet(3) == []


async def test_fetch_thread_media_uses_linked_author_and_includes_videos():
    linked = tweet(tid=10, user="threader", videos=[video()])
    linked.conversationId = 10
    same_author_reply = tweet(
        tid=11,
        user="threader",
        minutes=1,
        videos=[video([video_variant("https://v/reply.mp4", bitrate=2_000)])],
    )
    same_author_reply.conversationId = 10
    other_author_reply = tweet(
        tid=12,
        user="replyguy",
        minutes=2,
        videos=[video([video_variant("https://v/nope.mp4", bitrate=2_000)])],
    )
    other_author_reply.conversationId = 10
    scraper = make_scraper(
        [], detail=linked, thread=[linked, same_author_reply, other_author_reply]
    )

    candidates = await scraper.fetch_thread_media(10)

    assert [c.tweet_id for c in candidates] == [10, 11]
    assert [c.kind for c in candidates] == [MediaKind.VIDEO, MediaKind.VIDEO]
    assert candidates[0].tracked_handle == "threader"
    assert scraper._api.requested_thread_limit == 20


async def test_fetch_thread_media_none_when_unfetchable():
    scraper = make_scraper([], detail=None)
    assert await scraper.fetch_thread_media(1) is None


# -- plan_posts -------------------------------------------------------------------


def test_plan_posts_regular_run_posts_everything_oldest_first():
    cands = [candidate(tid=i, minutes=i * 10) for i in (3, 1, 2)]
    to_post, to_skip = plan_posts(cands, first_run=False, backfill_count=3)
    assert [c.tweet_id for c in to_post] == [1, 2, 3]
    assert to_skip == []


def test_plan_posts_first_run_keeps_newest_n_oldest_first():
    cands = [candidate(tid=i, minutes=i * 10) for i in (4, 1, 5, 2, 3)]
    to_post, to_skip = plan_posts(cands, first_run=True, backfill_count=2)
    assert [c.tweet_id for c in to_post] == [4, 5]
    assert [c.tweet_id for c in to_skip] == [1, 2, 3]


def test_plan_posts_first_run_zero_backfill_skips_everything():
    cands = [candidate(tid=1, minutes=10), candidate(tid=2, minutes=20)]
    to_post, to_skip = plan_posts(cands, first_run=True, backfill_count=0)
    assert to_post == []
    assert [c.tweet_id for c in to_skip] == [1, 2]


def test_plan_posts_first_run_backfill_larger_than_batch_posts_all():
    cands = [candidate(tid=2, minutes=20), candidate(tid=1, minutes=10)]
    to_post, to_skip = plan_posts(cands, first_run=True, backfill_count=10)
    assert [c.tweet_id for c in to_post] == [1, 2]
    assert to_skip == []


# -- model helpers ----------------------------------------------------------------


def test_fallback_url_format():
    assert candidate(tid=123).fallback_url == "https://d.fxtwitter.com/i/status/123"

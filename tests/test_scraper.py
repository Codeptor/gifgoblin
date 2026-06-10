from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from helpers import BASE, anim, candidate, tweet, user_id_for, video, video_variant

from gifharvest.db import Store
from gifharvest.models import MediaKind
from gifharvest.scraper import TwitterScraper, extract_candidates, normalize_handle, plan_posts


def _extract(tw, tracked="shitposter", *, retweets=False, videos=False):
    return extract_candidates(tw, tracked, include_retweets=retweets, include_videos=videos)


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
    def __init__(self, tweets: list, resolvable: bool = True):
        self._tweets = tweets
        self._resolvable = resolvable

    async def user_by_login(self, handle: str):
        if not self._resolvable:
            return None
        return SimpleNamespace(id=user_id_for(handle), username=handle)

    async def user_tweets(self, uid: int, limit: int = -1):
        for tw in self._tweets:
            yield tw


def make_scraper(tweets: list, *, retweets: bool = False, resolvable: bool = True):
    cfg = SimpleNamespace(scrape_limit=20, include_retweets=retweets, include_videos=False)
    return TwitterScraper(FakeAPI(tweets, resolvable), cfg)


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

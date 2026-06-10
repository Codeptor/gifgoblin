from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from helpers import candidate

from gifharvest.db import Store


@pytest.fixture
async def store(tmp_path) -> AsyncIterator[Store]:
    s = await Store.open(tmp_path / "t.db")
    yield s
    await s.close()


# -- tracked accounts --------------------------------------------------------


async def test_add_handle_normalizes_to_lowercase(store: Store):
    assert await store.add_handle("FooBar") is True
    assert await store.handles() == ["foobar"]


async def test_add_duplicate_handle_returns_false(store: Store):
    assert await store.add_handle("foobar") is True
    assert await store.add_handle("foobar") is False
    assert await store.add_handle("FooBar") is False
    assert await store.handles() == ["foobar"]


async def test_handles_sorted(store: Store):
    await store.add_handle("zeta")
    await store.add_handle("Alpha")
    assert await store.handles() == ["alpha", "zeta"]


async def test_remove_handle(store: Store):
    await store.add_handle("foobar")
    assert await store.remove_handle("FooBar") is True
    assert await store.handles() == []


async def test_remove_unknown_handle_returns_false(store: Store):
    assert await store.remove_handle("nobody") is False


async def test_user_id_roundtrip(store: Store):
    await store.add_handle("foobar")
    assert await store.get_user_id("foobar") is None
    await store.set_user_id("FooBar", 12345)
    assert await store.get_user_id("foobar") == 12345
    assert await store.get_user_id("FOOBAR") == 12345


async def test_first_scrape_lifecycle(store: Store):
    await store.add_handle("foobar")
    assert await store.is_first_scrape("foobar") is True
    assert await store.is_first_scrape("unknown") is True
    await store.mark_scraped("FooBar")
    assert await store.is_first_scrape("foobar") is False


# -- dedupe -------------------------------------------------------------------


async def test_mark_seen_dedupes_by_media_url_only(store: Store):
    c = candidate(tid=1, media_url="https://video.twimg.com/tweet_video/abc.mp4")
    assert await store.is_seen(c) is False
    await store.mark_seen(c)
    assert await store.is_seen(c) is True

    # a sibling candidate of the same tweet stays postable (partial-failure retry)
    same_tweet = candidate(tid=1, media_url="https://video.twimg.com/tweet_video/other.mp4")
    assert await store.is_seen(same_tweet) is False

    same_media = candidate(tid=999, media_url="https://video.twimg.com/tweet_video/abc.mp4")
    assert await store.is_seen(same_media) is True

    fresh = candidate(tid=2, media_url="https://video.twimg.com/tweet_video/new.mp4")
    assert await store.is_seen(fresh) is False


async def test_mark_tweet_seen_blocks_every_media_of_that_tweet(store: Store):
    await store.mark_tweet_seen(1)
    assert await store.is_seen(candidate(tid=1, media_url="https://v/x.mp4")) is True
    assert await store.is_seen(candidate(tid=1, media_url="https://v/y.mp4")) is True
    assert await store.is_seen(candidate(tid=2, media_url="https://v/z.mp4")) is False


async def test_record_post_marks_seen_and_updates_stats(store: Store):
    await store.add_handle("shitposter")
    c = candidate(tid=7)
    await store.record_post(c)
    assert await store.is_seen(c) is True

    stats = await store.stats()
    assert stats["tracked"] == 1
    assert stats["posts"] == 1
    assert stats["last_posted"] is not None


async def test_stats_empty(store: Store):
    stats = await store.stats()
    assert stats == {"tracked": 0, "posts": 0, "last_posted": None}


# -- settings -------------------------------------------------------------------


async def test_settings_get_set_overwrite(store: Store):
    assert await store.get_setting("cursor") is None
    await store.set_setting("cursor", "100")
    assert await store.get_setting("cursor") == "100"
    await store.set_setting("cursor", "200")
    assert await store.get_setting("cursor") == "200"

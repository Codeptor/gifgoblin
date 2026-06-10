from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from .models import GifCandidate

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked (
    handle TEXT PRIMARY KEY,
    user_id INTEGER,
    first_scraped INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_tweets (id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS seen_media (url TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS posts (
    tweet_id INTEGER NOT NULL,
    media_url TEXT NOT NULL,
    author TEXT NOT NULL,
    tracked_handle TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    PRIMARY KEY (tweet_id, media_url)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    @classmethod
    async def open(cls, path: Path) -> Store:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.executescript(_SCHEMA)
        await db.commit()
        return cls(db)

    async def close(self) -> None:
        await self._db.close()

    # -- tracked accounts ----------------------------------------------------

    async def add_handle(self, handle: str) -> bool:
        cur = await self._db.execute(
            "INSERT OR IGNORE INTO tracked (handle, added_at) VALUES (?, ?)",
            (handle.lower(), _now()),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def remove_handle(self, handle: str) -> bool:
        cur = await self._db.execute("DELETE FROM tracked WHERE handle = ?", (handle.lower(),))
        await self._db.commit()
        return cur.rowcount > 0

    async def handles(self) -> list[str]:
        async with self._db.execute("SELECT handle FROM tracked ORDER BY handle") as cur:
            return [row[0] for row in await cur.fetchall()]

    async def get_user_id(self, handle: str) -> int | None:
        async with self._db.execute(
            "SELECT user_id FROM tracked WHERE handle = ?", (handle.lower(),)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row and row[0] else None

    async def set_user_id(self, handle: str, user_id: int) -> None:
        await self._db.execute(
            "UPDATE tracked SET user_id = ? WHERE handle = ?", (user_id, handle.lower())
        )
        await self._db.commit()

    async def is_first_scrape(self, handle: str) -> bool:
        async with self._db.execute(
            "SELECT first_scraped FROM tracked WHERE handle = ?", (handle.lower(),)
        ) as cur:
            row = await cur.fetchone()
        return row is None or row[0] == 0

    async def mark_scraped(self, handle: str) -> None:
        await self._db.execute(
            "UPDATE tracked SET first_scraped = 1 WHERE handle = ?", (handle.lower(),)
        )
        await self._db.commit()

    # -- dedupe ---------------------------------------------------------------

    async def is_seen(self, candidate: GifCandidate) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM seen_tweets WHERE id = ? "
            "UNION SELECT 1 FROM seen_media WHERE url = ? LIMIT 1",
            (candidate.tweet_id, candidate.media_url),
        ) as cur:
            return await cur.fetchone() is not None

    async def mark_seen(self, candidate: GifCandidate) -> None:
        await self._db.execute(
            "INSERT OR IGNORE INTO seen_media (url) VALUES (?)", (candidate.media_url,)
        )
        await self._db.commit()

    async def mark_tweet_seen(self, tweet_id: int) -> None:
        # tweet-level dedupe is only safe once every candidate of the tweet is
        # handled — a premature insert would permanently drop a failed sibling,
        # since is_seen matches on tweet_id alone
        await self._db.execute("INSERT OR IGNORE INTO seen_tweets (id) VALUES (?)", (tweet_id,))
        await self._db.commit()

    async def record_post(self, candidate: GifCandidate) -> None:
        await self.mark_seen(candidate)
        await self._db.execute(
            "INSERT OR IGNORE INTO posts (tweet_id, media_url, author, tracked_handle, posted_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                candidate.tweet_id,
                candidate.media_url,
                candidate.author,
                candidate.tracked_handle,
                _now(),
            ),
        )
        await self._db.commit()

    # -- settings / stats -----------------------------------------------------

    async def get_setting(self, key: str) -> str | None:
        async with self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self._db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._db.commit()

    async def stats(self) -> dict[str, int | str | None]:
        async with self._db.execute("SELECT COUNT(*) FROM tracked") as cur:
            tracked = (await cur.fetchone())[0]
        async with self._db.execute("SELECT COUNT(*), MAX(posted_at) FROM posts") as cur:
            posts, last_posted = await cur.fetchone()
        return {"tracked": tracked, "posts": posts, "last_posted": last_posted}

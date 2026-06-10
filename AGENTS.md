# gifharvest — agent notes

Scrape GIFs from tracked X/Twitter accounts (twscrape, cookie-auth donor accounts), repost to one Discord channel (discord.py), SQLite dedupe so nothing is ever posted twice.

## Architecture

| Module | Responsibility | Key surface |
|---|---|---|
| `gifharvest/config.py` | Env/.env configuration | frozen `Config`, `Config.load(require_discord=True)` |
| `gifharvest/models.py` | Core data types | `MediaKind{GIF,VIDEO}`, frozen `GifCandidate` (`.fallback_url` → d.fxtwitter.com, `.filename` → `author_tweetid.mp4`) |
| `gifharvest/db.py` | SQLite store (aiosqlite) | `Store.open(path)`, `add_handle/remove_handle/handles`, `get_user_id/set_user_id`, `is_first_scrape/mark_scraped`, `is_seen/mark_seen/record_post`, `get_setting/set_setting`, `stats()` |
| `gifharvest/scraper.py` | twscrape wrapper: resolve handles, pull tweets, extract animated-gif/video candidates, plan what to post | scrape per handle → candidate list; `plan_posts` applies dedupe + first-run backfill |
| `gifharvest/downloader.py` | Fetch media bytes (httpx), optional ffmpeg mp4→gif conversion (two-pass palette) | size-aware: gif falls back to mp4, mp4 over limit falls back to embed link |
| `gifharvest/bot.py` | discord.py client, background poll loop, slash commands (`/track`, `/scan`, `/harveststats`) | posts upload + caption `@author · <tweet link>` |
| `gifharvest/cli.py` | Console entrypoint `gifharvest` | `run`, `scrape [--mark-seen]`, `track add/remove/list`, `accounts add/list`, `stats` |

Data flow: **scraper** (twscrape → `GifCandidate`s) → **plan_posts** (Store dedupe, backfill cap) → **downloader** (bytes, optional gif conversion) → **bot** (Discord upload or fallback embed) → **store** (`record_post`).

Tests: `tests/helpers.py` has factories `tweet()/anim()/video()/video_variant()/candidate()` returning SimpleNamespace stubs shaped like twscrape models.

## Conventions

- **uv only** — never pip, never venv directly. `uv run pytest`, `uv run ruff ...`, `uv run gifharvest ...`.
- Format + lint: `uv run ruff format .` and `uv run ruff check .` (line length 100, target py311, rules E,F,I,UP,B,SIM).
- Tests: `uv run pytest` (asyncio_mode=auto). All tests pass before committing.
- `from __future__ import annotations` in every module; `logging.getLogger(__name__)` in library code (CLI may print).
- Comments only for non-obvious *why*.
- Commits: concise imperative with scoped prefixes (`fix(bot):`, `feat(scraper):`). No co-author lines.

## Known constraints

- X "GIFs" are not gif files — they are looping mp4s under `animated_gif` media. Mixed-media tweets can carry a gif plus videos, so one tweet can yield several candidates.
- twscrape's `user_tweets` yields every Tweet object found in the GQL response — RT/quote originals surface standalone (with `retweetedTweet=None`) and promoted tweets are included. `fetch_new` keeps only tweets authored by the tracked uid; retweets are resolved through the wrapper tweet so attribution works.
- Discord free-tier upload limit is 10 MB; the bot auto-uses the guild's `filesize_limit` when `MAX_UPLOAD_BYTES=0` (boosted guilds get more). Oversized files fall back to a d.fxtwitter.com embed link.
- Two separate databases: twscrape's donor-account pool lives in `ACCOUNTS_DB` (`data/accounts.db`), gifharvest state in `DB_PATH` (`data/gifharvest.db`). Don't conflate them.
- Donor X accounts can get rate-limited or banned at any time — that's why burners are used and why rate-limit headroom scales with pool size.
- First scrape of a newly tracked handle: only the newest `BACKFILL_COUNT` candidates are posted; everything else is marked seen. Subsequent scrapes post all new candidates.
- Dedupe: `is_seen` matches tweet id OR media URL. `mark_seen`/`record_post` insert only the media URL; the tweet id is inserted via `mark_tweet_seen` only once every candidate of that tweet is handled — so a failed sibling of a posted candidate stays retryable. Retweet-safe either way (same media via a retweet won't repost).
- `CONVERT_TO_GIF=true` needs ffmpeg on PATH; conversion falls back to mp4 upload if ffmpeg is missing or the resulting gif exceeds the upload limit.
- `data/` and `.env` are gitignored — never commit them.

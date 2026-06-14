from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


def _as_float(value: str | None, default: float) -> float:
    if value is None or value.strip() == "":
        return default
    return float(value)


@dataclass(frozen=True)
class Config:
    discord_token: str
    channel_id: int
    alert_channel_id: int | None
    guild_id: int | None
    poll_minutes: float
    scrape_limit: int
    backfill_count: int
    include_retweets: bool
    include_videos: bool
    message_links_enabled: bool
    auto_relogin: bool
    convert_to_gif: bool
    max_upload_bytes: int  # 0 = auto-detect from the guild's boost tier
    gif_fps: int
    gif_max_width: int
    video_gif_max_seconds: float  # /get only: convert videos this short to gif (0 = off)
    db_path: Path
    accounts_db: Path
    log_level: str

    @classmethod
    def load(cls, *, require_discord: bool = True) -> Config:
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        channel_id = _as_int(os.getenv("GIF_CHANNEL_ID"), 0)
        if require_discord:
            if not token:
                raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")
            if channel_id <= 0:
                raise SystemExit("GIF_CHANNEL_ID is not set (see .env.example)")
        guild_id = _as_int(os.getenv("GUILD_ID"), 0)
        alert_channel_id = _as_int(os.getenv("ALERT_CHANNEL_ID"), 0)
        return cls(
            discord_token=token,
            channel_id=channel_id,
            alert_channel_id=alert_channel_id or None,
            guild_id=guild_id or None,
            poll_minutes=_as_float(os.getenv("POLL_MINUTES"), 10.0),
            scrape_limit=_as_int(os.getenv("SCRAPE_LIMIT"), 20),
            backfill_count=_as_int(os.getenv("BACKFILL_COUNT"), 3),
            include_retweets=_as_bool(os.getenv("INCLUDE_RETWEETS"), False),
            include_videos=_as_bool(os.getenv("INCLUDE_VIDEOS"), False),
            message_links_enabled=_as_bool(os.getenv("MESSAGE_LINKS_ENABLED"), False),
            auto_relogin=_as_bool(os.getenv("AUTO_RELOGIN"), True),
            convert_to_gif=_as_bool(os.getenv("CONVERT_TO_GIF"), True),
            max_upload_bytes=_as_int(os.getenv("MAX_UPLOAD_BYTES"), 0),
            gif_fps=_as_int(os.getenv("GIF_FPS"), 15),
            gif_max_width=_as_int(os.getenv("GIF_MAX_WIDTH"), 480),
            video_gif_max_seconds=_as_float(os.getenv("VIDEO_GIF_MAX_SECONDS"), 5.0),
            db_path=Path(os.getenv("DB_PATH") or "data/gifharvest.db"),
            accounts_db=Path(os.getenv("ACCOUNTS_DB") or "data/accounts.db"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

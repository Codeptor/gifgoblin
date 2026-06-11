from __future__ import annotations

from pathlib import Path

import pytest

from gifharvest.config import Config, _as_bool, _as_float, _as_int

ENV_VARS = (
    "DISCORD_TOKEN",
    "GIF_CHANNEL_ID",
    "ALERT_CHANNEL_ID",
    "GUILD_ID",
    "POLL_MINUTES",
    "SCRAPE_LIMIT",
    "BACKFILL_COUNT",
    "INCLUDE_RETWEETS",
    "INCLUDE_VIDEOS",
    "MESSAGE_LINKS_ENABLED",
    "CONVERT_TO_GIF",
    "MAX_UPLOAD_BYTES",
    "GIF_FPS",
    "GIF_MAX_WIDTH",
    "DB_PATH",
    "ACCOUNTS_DB",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    # find_dotenv walks up from config.py's directory (not cwd), so chdir alone
    # would not stop a real project .env from repopulating the deleted vars
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.chdir(tmp_path)
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_without_discord():
    cfg = Config.load(require_discord=False)
    assert cfg.discord_token == ""
    assert cfg.channel_id == 0
    assert cfg.alert_channel_id is None
    assert cfg.guild_id is None
    assert cfg.poll_minutes == 10.0
    assert cfg.scrape_limit == 20
    assert cfg.backfill_count == 3
    assert cfg.include_retweets is False
    assert cfg.include_videos is False
    assert cfg.message_links_enabled is False
    assert cfg.convert_to_gif is True
    assert cfg.max_upload_bytes == 0
    assert cfg.gif_fps == 15
    assert cfg.gif_max_width == 480
    assert cfg.db_path == Path("data/gifharvest.db")
    assert cfg.accounts_db == Path("data/accounts.db")
    assert cfg.log_level == "INFO"


def test_missing_token_exits(monkeypatch):
    monkeypatch.setenv("GIF_CHANNEL_ID", "123")
    with pytest.raises(SystemExit, match="DISCORD_TOKEN"):
        Config.load(require_discord=True)


def test_missing_channel_exits(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "tok")
    with pytest.raises(SystemExit, match="GIF_CHANNEL_ID"):
        Config.load(require_discord=True)


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_bool_truthy(raw, monkeypatch):
    monkeypatch.setenv("INCLUDE_RETWEETS", raw)
    assert Config.load(require_discord=False).include_retweets is True


@pytest.mark.parametrize("raw", ["0", "off", ""])
def test_bool_falls_to_default(raw):
    # default=True distinguishes "parsed False" from "fell back to default"
    assert _as_bool(raw, default=False) is False
    expected = raw == ""  # "" falls back to default; "0"/"off" parse as False
    assert _as_bool(raw, default=True) is expected


def test_bool_unset_uses_default():
    assert _as_bool(None, default=False) is False
    assert _as_bool(None, default=True) is True
    assert Config.load(require_discord=False).include_videos is False


def test_int_and_float_parsing(monkeypatch):
    monkeypatch.setenv("SCRAPE_LIMIT", "50")
    monkeypatch.setenv("POLL_MINUTES", "2.5")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "8388608")
    cfg = Config.load(require_discord=False)
    assert cfg.scrape_limit == 50
    assert cfg.poll_minutes == 2.5
    assert cfg.max_upload_bytes == 8388608


def test_int_and_float_empty_string_falls_to_default(monkeypatch):
    monkeypatch.setenv("SCRAPE_LIMIT", "")
    monkeypatch.setenv("POLL_MINUTES", "")
    cfg = Config.load(require_discord=False)
    assert cfg.scrape_limit == 20
    assert cfg.poll_minutes == 10.0
    assert _as_int("", 7) == 7
    assert _as_float("  ", 1.5) == 1.5


def test_guild_id_unset_or_zero_is_none(monkeypatch):
    assert Config.load(require_discord=False).guild_id is None
    monkeypatch.setenv("GUILD_ID", "0")
    assert Config.load(require_discord=False).guild_id is None


def test_guild_id_set(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "987654321")
    assert Config.load(require_discord=False).guild_id == 987654321


def test_alert_channel_id_set(monkeypatch):
    monkeypatch.setenv("ALERT_CHANNEL_ID", "123456789")
    assert Config.load(require_discord=False).alert_channel_id == 123456789

from __future__ import annotations

import asyncio
import io
import logging
from datetime import UTC, datetime
from pathlib import PurePosixPath

import discord
import httpx
from discord import app_commands
from discord.ext import commands, tasks
from twscrape import NoAccountError

from .config import Config
from .db import Store
from .downloader import convert_to_gif, fetch_media
from .models import GifCandidate, MediaKind
from .scraper import TwitterScraper, normalize_handle, parse_tweet_url, parse_tweet_urls, plan_posts

log = logging.getLogger(__name__)

DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024

_POST_PAUSE_SECONDS = 1.5
# when converting to gif, the source clip may exceed the Discord upload limit
# (only the output gif must fit); cap the source download to avoid huge fetches
_CONVERT_SOURCE_LIMIT = 50 * 1024 * 1024
_DONOR_ALERT_SETTING = "donor_account_alert_state"
_DONOR_RELOGIN_SETTING = "donor_account_relogin_state"


def _format_timestamp(raw: str | None) -> str:
    if not raw:
        return "never"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return f"<t:{int(dt.timestamp())}:R>"


def _format_handles(handles: list[str]) -> str:
    if not handles:
        return "none"
    visible = ", ".join(f"@{h}" for h in handles[:20])
    if len(handles) > 20:
        visible += f", +{len(handles) - 20} more"
    return visible


def _format_latest_post(latest: dict[str, int | str] | None) -> str:
    if latest is None:
        return "never"
    return (
        f"@{latest['author']} via @{latest['tracked_handle']} "
        f"{_format_timestamp(str(latest['posted_at']))} - <{latest['tweet_url']}>"
    )


def _format_account_health(health: dict[str, int | list[str]]) -> str:
    total = int(health["total"])
    if total == 0:
        return "0 configured"

    active = int(health["active"])
    errors = [str(x) for x in health["errors"]]
    summary = f"{active}/{total} active"
    if errors:
        visible = ", ".join(f"@{x}" for x in errors[:5])
        if len(errors) > 5:
            visible += f", +{len(errors) - 5} more"
        summary += f" - check {visible}"
    reloginable = [str(x) for x in health.get("reloginable", [])]
    if reloginable:
        summary += f" - auto-relogin available for {len(reloginable)}"
    return summary


def _donor_alert_state(health: dict[str, int | list[str]]) -> tuple[str, str | None]:
    total = int(health["total"])
    active = int(health["active"])
    errors = [str(x) for x in health["errors"]]
    if total == 0:
        return "none", "No X donor accounts are configured. Add one with `gifharvest accounts add`."
    if errors or active < total:
        bad = ", ".join(f"@{x}" for x in errors) if errors else "unknown account"
        reloginable = ",".join(str(x) for x in health.get("reloginable", []))
        return (
            f"bad:{total}:{active}:{','.join(errors)}:{reloginable}",
            f"X donor account needs refresh: {bad}. "
            f"Health is {active}/{total} active. "
            "Run `gifharvest accounts relogin <username>` if credentials are stored, "
            "or refresh cookies with `gifharvest accounts browser-refresh <username>`.",
        )
    return "ok", None


class GifHarvestBot(commands.Bot):
    def __init__(self, cfg: Config, store: Store, scraper: TwitterScraper):
        intents = discord.Intents.default()
        if cfg.message_links_enabled:
            intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            activity=discord.CustomActivity(name="harvesting gifs"),
        )
        self.cfg = cfg
        self.store = store
        self.scraper = scraper
        self.http_client = httpx.AsyncClient(timeout=60, follow_redirects=True)
        self._cycle_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        await self.add_cog(HarvestCog(self))
        if self.cfg.guild_id:
            guild = discord.Object(id=self.cfg.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.poller.change_interval(minutes=self.cfg.poll_minutes)
        self.poller.start()

    @tasks.loop(minutes=10)
    async def poller(self) -> None:
        try:
            result = await self.run_cycle()
            log.info(
                "cycle done: %d handles, %d posted, %d errors",
                result["handles"],
                result["posted"],
                result["errors"],
            )
        except Exception:
            log.exception("poll cycle failed")

    @poller.before_loop
    async def _wait_ready(self) -> None:
        await self.wait_until_ready()

    async def _channel_and_limit(self) -> tuple[discord.abc.Messageable, int]:
        channel = self.get_channel(self.cfg.channel_id) or await self.fetch_channel(
            self.cfg.channel_id
        )
        if self.cfg.max_upload_bytes > 0:
            limit = self.cfg.max_upload_bytes
        else:
            guild = getattr(channel, "guild", None)
            limit = getattr(guild, "filesize_limit", DEFAULT_UPLOAD_LIMIT)
        return channel, limit

    async def run_cycle(self) -> dict:
        async with self._cycle_lock:
            channel, limit = await self._channel_and_limit()

            handles = await self.store.handles()
            posted = 0
            errors = 0
            for handle in handles:
                try:
                    candidates = await self.scraper.fetch_new(self.store, handle)
                except NoAccountError:
                    if await self.scraper.has_active_accounts():
                        log.warning(
                            "all twscrape accounts are rate-limited — aborting this "
                            "cycle, retrying on the next poll"
                        )
                    else:
                        log.error(
                            "no usable twscrape account in the pool — add one with "
                            "`gifharvest accounts add` (see README); aborting this cycle"
                        )
                    break
                except Exception:
                    log.exception("scrape failed for @%s", handle)
                    errors += 1
                    continue
                if candidates is None:
                    # resolution can fail transiently; skipping mark_scraped keeps
                    # first-run backfill protection for the next attempt
                    errors += 1
                    continue

                first_run = await self.store.is_first_scrape(handle)
                to_post, to_skip = plan_posts(
                    candidates, first_run=first_run, backfill_count=self.cfg.backfill_count
                )
                failed_tweets: set[int] = set()
                for candidate in to_skip:
                    await self.store.mark_seen(candidate)
                for candidate in to_post:
                    try:
                        await self._post(channel, candidate, limit)
                        await self.store.record_post(candidate)
                        posted += 1
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code < 500:
                            # 4xx means the media is permanently gone — retrying
                            # every cycle forever is pointless
                            log.warning(
                                "media for %s gone (HTTP %d) — marking seen",
                                candidate.tweet_url,
                                exc.response.status_code,
                            )
                            await self.store.mark_seen(candidate)
                        else:
                            log.exception("failed to post %s", candidate.tweet_url)
                            failed_tweets.add(candidate.tweet_id)
                        errors += 1
                    except Exception:
                        # not marked seen, so it gets retried next cycle
                        log.exception("failed to post %s", candidate.tweet_url)
                        failed_tweets.add(candidate.tweet_id)
                        errors += 1
                    await asyncio.sleep(_POST_PAUSE_SECONDS)
                # tweet-id dedupe only once every candidate of a tweet is handled,
                # so a failed sibling of a posted candidate stays retryable
                handled = {c.tweet_id for c in to_post} | {c.tweet_id for c in to_skip}
                for tweet_id in handled - failed_tweets:
                    await self.store.mark_tweet_seen(tweet_id)
                await self.store.mark_scraped(handle)

            await self.store.mark_poll_completed()
            await self._maybe_alert_account_health()
            return {"handles": len(handles), "posted": posted, "errors": errors}

    async def _maybe_alert_account_health(self) -> None:
        try:
            health = await self.scraper.account_health()
            state, message = _donor_alert_state(health)
            reloginable = [str(x) for x in health.get("reloginable", [])]
            relogin_state = await self.store.get_setting(_DONOR_RELOGIN_SETTING)
            if self.cfg.auto_relogin and message and reloginable and relogin_state != state:
                log.warning("attempting automatic X relogin for: %s", ", ".join(reloginable))
                health = await self.scraper.relogin_unhealthy_accounts()
                await self.store.set_setting(_DONOR_RELOGIN_SETTING, state)
                state, message = _donor_alert_state(health)
                if message:
                    message = f"Automated X relogin failed. {message}"

            previous = await self.store.get_setting(_DONOR_ALERT_SETTING)
            if state == previous:
                return

            if state == "ok":
                if previous and previous != "ok":
                    await self._send_alert("X donor account health recovered.")
                await self.store.set_setting(_DONOR_ALERT_SETTING, state)
                return
            if message:
                await self._send_alert(message)
                await self.store.set_setting(_DONOR_ALERT_SETTING, state)
        except Exception:
            log.exception("failed to check donor account health")

    async def _send_alert(self, content: str) -> None:
        channel_id = self.cfg.alert_channel_id or self.cfg.channel_id
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        await channel.send(content, allowed_mentions=discord.AllowedMentions.none())

    async def post_now(self, candidates: list[GifCandidate]) -> tuple[int, int]:
        """Post explicitly requested candidates straight to the gif channel."""
        channel, limit = await self._channel_and_limit()
        posted = 0
        errors = 0
        failed: set[int] = set()
        for i, candidate in enumerate(candidates):
            if i:
                await asyncio.sleep(_POST_PAUSE_SECONDS)
            try:
                await self._post(channel, candidate, limit)
                await self.store.record_post(candidate)
                posted += 1
            except Exception:
                log.exception("failed to post %s", candidate.tweet_url)
                failed.add(candidate.tweet_id)
                errors += 1
        # keeps the poller from re-posting the same tweet later if its author
        # is (or becomes) tracked; failed tweets stay eligible
        for tweet_id in {c.tweet_id for c in candidates} - failed:
            await self.store.mark_tweet_seen(tweet_id)
        return posted, errors

    async def post_tweet_link(self, tweet_id: int) -> str:
        try:
            candidates = await self.scraper.fetch_tweet(tweet_id)
        except NoAccountError:
            if await self.scraper.has_active_accounts():
                return "All donor accounts are rate-limited - try again in a few minutes."
            return (
                "No donor X account configured - add or refresh one with `gifharvest accounts add`."
            )

        if candidates is None:
            channel, _ = await self._channel_and_limit()
            await channel.send(
                f"https://d.fxtwitter.com/i/status/{tweet_id}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return (
                "I could not fetch media directly from X, so I posted the fxtwitter "
                f"fallback link to <#{self.cfg.channel_id}>."
            )
        if not candidates:
            return "That tweet has no gif or video."

        posted, errors = await self.post_now(candidates)
        summary = f"Posted {posted} item(s) to <#{self.cfg.channel_id}>."
        if errors:
            summary += f" {errors} failed - check the logs."
        return summary

    async def on_message(self, message: discord.Message) -> None:
        await self.process_commands(message)
        if (
            not self.cfg.message_links_enabled
            or message.author.bot
            or not message.guild
            or not message.content
        ):
            return

        tweet_ids = parse_tweet_urls(message.content)
        if not tweet_ids:
            return

        summaries: list[str] = []
        for tweet_id in tweet_ids[:3]:
            summaries.append(await self.post_tweet_link(tweet_id))
        if len(tweet_ids) > 3:
            summaries.append(f"Skipped {len(tweet_ids) - 3} extra link(s).")

        has_failure = any("failed" in s.lower() for s in summaries)
        if message.channel.id != self.cfg.channel_id or has_failure:
            await message.reply(
                "\n".join(summaries),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _post(self, channel, c: GifCandidate, limit: int) -> None:
        caption = f"**@{c.author}**"
        if c.via_retweet:
            caption += f" (rt via @{c.tracked_handle})"
        caption += f" · <{c.tweet_url}>"

        async def send_fallback() -> None:
            # fallback link stays un-angle-bracketed so Discord embeds the video
            await channel.send(
                f"{caption}\n{c.fallback_url}",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        will_convert = self.cfg.convert_to_gif and c.kind is MediaKind.GIF
        # a clip being converted may be larger than the upload limit — only the
        # output gif must fit — so allow a bigger source download when converting
        source_limit = max(limit, _CONVERT_SOURCE_LIMIT) if will_convert else limit
        dl = await fetch_media(self.http_client, c.media_url, source_limit)
        if dl.too_big:
            await send_fallback()
            return

        data, filename = dl.data, c.filename
        if will_convert:
            gif = await convert_to_gif(
                data,
                fps=self.cfg.gif_fps,
                max_width=self.cfg.gif_max_width,
                max_bytes=limit,
            )
            if gif is not None:
                data = gif
                filename = str(PurePosixPath(filename).with_suffix(".gif"))
            elif len(data) > limit:
                # neither the converted gif nor the raw source fits the upload limit
                await send_fallback()
                return

        await channel.send(
            caption,
            file=discord.File(io.BytesIO(data), filename=filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def close(self) -> None:
        # stop the poller and let an in-flight cycle unwind before tearing down
        # the http client/store, otherwise shutdown races a cycle mid-post
        self.poller.cancel()
        async with self._cycle_lock:
            pass
        await self.http_client.aclose()
        await super().close()


class HarvestCog(commands.Cog):
    track = app_commands.Group(
        name="track",
        description="Manage the tracked X/Twitter accounts",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: GifHarvestBot):
        self.bot = bot

    @track.command(name="add", description="Start tracking an X/Twitter account")
    @app_commands.describe(handle="Account handle or profile URL")
    async def track_add(self, interaction: discord.Interaction, handle: str) -> None:
        normalized = normalize_handle(handle)
        if normalized is None:
            await interaction.response.send_message(
                f"`{handle}` doesn't look like a valid handle.", ephemeral=True
            )
            return
        added = await self.bot.store.add_handle(normalized)
        if added:
            message = f"Now tracking **@{normalized}**."
        else:
            message = f"**@{normalized}** is already tracked."
        await interaction.response.send_message(message, ephemeral=True)

    @track.command(name="remove", description="Stop tracking an X/Twitter account")
    @app_commands.describe(handle="Account handle or profile URL")
    async def track_remove(self, interaction: discord.Interaction, handle: str) -> None:
        normalized = normalize_handle(handle)
        if normalized is None:
            await interaction.response.send_message(
                f"`{handle}` doesn't look like a valid handle.", ephemeral=True
            )
            return
        removed = await self.bot.store.remove_handle(normalized)
        if removed:
            message = f"Stopped tracking **@{normalized}**."
        else:
            message = f"**@{normalized}** wasn't tracked."
        await interaction.response.send_message(message, ephemeral=True)

    @track.command(name="list", description="Show the tracked accounts")
    async def track_list(self, interaction: discord.Interaction) -> None:
        handles = await self.bot.store.handles()
        if not handles:
            await interaction.response.send_message("Nothing tracked yet.", ephemeral=True)
            return
        listing = "\n".join(f"- @{h}" for h in handles)
        await interaction.response.send_message(
            f"Tracking {len(handles)} account(s):\n{listing}", ephemeral=True
        )

    @app_commands.command(name="scan", description="Run a harvest cycle right now")
    @app_commands.default_permissions(manage_guild=True)
    async def scan(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.bot.run_cycle()
        summary = (
            f"Scanned {result['handles']} handle(s): "
            f"{result['posted']} posted, {result['errors']} error(s)."
        )
        try:
            await interaction.followup.send(summary, ephemeral=True)
        except discord.HTTPException:
            # interaction tokens expire after 15 minutes; a long cycle (plus
            # waiting on a poller-held cycle lock) can outlive them
            log.warning("scan finished after the interaction expired: %s", summary)
            if interaction.channel is not None:
                await interaction.channel.send(summary)

    @app_commands.command(
        name="get", description="Fetch a tweet's gif/video and post it to the channel"
    )
    @app_commands.describe(link="X/Twitter status link")
    async def get(self, interaction: discord.Interaction, link: str) -> None:
        tweet_id = parse_tweet_url(link)
        if tweet_id is None:
            await interaction.response.send_message(
                f"`{link}` doesn't look like a tweet link.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        summary = await self.bot.post_tweet_link(tweet_id)
        await interaction.followup.send(summary, ephemeral=True)

    @app_commands.command(name="harveststats", description="Show harvest stats")
    async def harveststats(self, interaction: discord.Interaction) -> None:
        stats = await self.bot.store.stats()
        last = stats["last_posted"] or "never"
        await interaction.response.send_message(
            f"Tracked accounts: **{stats['tracked']}**\n"
            f"GIFs posted: **{stats['posts']}**\n"
            f"Last post: **{last}**",
            ephemeral=True,
        )

    @app_commands.command(name="status", description="Show bot health and harvest status")
    @app_commands.default_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        handles = await self.bot.store.handles()
        stats = await self.bot.store.stats()
        latest = await self.bot.store.latest_post()
        last_poll = await self.bot.store.get_setting("last_poll_completed_at")
        account_health = await self.bot.scraper.account_health()

        await interaction.response.send_message(
            "**gifgoblin status**\n"
            f"Tracked accounts ({len(handles)}): {_format_handles(handles)}\n"
            f"Last poll: **{_format_timestamp(last_poll)}**\n"
            f"Last posted GIF: {_format_latest_post(latest)}\n"
            f"Total posted: **{stats['posts']}**\n"
            f"Donor accounts: **{_format_account_health(account_health)}**\n"
            f"Poll interval: **{self.bot.cfg.poll_minutes:g} min**",
            ephemeral=True,
        )

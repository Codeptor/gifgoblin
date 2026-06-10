from __future__ import annotations

import asyncio
import io
import logging
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
from .scraper import TwitterScraper, normalize_handle, plan_posts

log = logging.getLogger(__name__)

DEFAULT_UPLOAD_LIMIT = 10 * 1024 * 1024

_POST_PAUSE_SECONDS = 1.5


class GifHarvestBot(commands.Bot):
    def __init__(self, cfg: Config, store: Store, scraper: TwitterScraper):
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
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

    async def run_cycle(self) -> dict:
        async with self._cycle_lock:
            channel = self.get_channel(self.cfg.channel_id) or await self.fetch_channel(
                self.cfg.channel_id
            )
            if self.cfg.max_upload_bytes > 0:
                limit = self.cfg.max_upload_bytes
            else:
                guild = getattr(channel, "guild", None)
                limit = getattr(guild, "filesize_limit", DEFAULT_UPLOAD_LIMIT)

            handles = await self.store.handles()
            posted = 0
            errors = 0
            for handle in handles:
                try:
                    candidates = await self.scraper.fetch_new(self.store, handle)
                except NoAccountError:
                    log.error(
                        "no usable twscrape account in the pool — add one with "
                        "`gifharvest accounts add` (see README); aborting this cycle"
                    )
                    break
                except Exception:
                    log.exception("scrape failed for @%s", handle)
                    errors += 1
                    continue

                first_run = await self.store.is_first_scrape(handle)
                to_post, to_skip = plan_posts(
                    candidates, first_run=first_run, backfill_count=self.cfg.backfill_count
                )
                for candidate in to_skip:
                    await self.store.mark_seen(candidate)
                for candidate in to_post:
                    try:
                        await self._post(channel, candidate, limit)
                        await self.store.record_post(candidate)
                        posted += 1
                    except Exception:
                        # not marked seen, so it gets retried next cycle
                        log.exception("failed to post %s", candidate.tweet_url)
                        errors += 1
                    await asyncio.sleep(_POST_PAUSE_SECONDS)
                await self.store.mark_scraped(handle)

            return {"handles": len(handles), "posted": posted, "errors": errors}

    async def _post(self, channel, c: GifCandidate, limit: int) -> None:
        dl = await fetch_media(self.http_client, c.media_url, limit)
        caption = f"**@{c.author}**"
        if c.via_retweet:
            caption += f" (rt via @{c.tracked_handle})"
        caption += f" · <{c.tweet_url}>"

        if dl.too_big:
            # fallback link stays un-angle-bracketed so Discord embeds the video
            await channel.send(
                f"{caption}\n{c.fallback_url}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        data, filename = dl.data, c.filename
        if self.cfg.convert_to_gif and c.kind is MediaKind.GIF:
            gif = await convert_to_gif(
                data,
                fps=self.cfg.gif_fps,
                max_width=self.cfg.gif_max_width,
                max_bytes=limit,
            )
            if gif:
                data = gif
                filename = str(PurePosixPath(filename).with_suffix(".gif"))

        await channel.send(
            caption,
            file=discord.File(io.BytesIO(data), filename=filename),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def close(self) -> None:
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
        await interaction.followup.send(
            f"Scanned {result['handles']} handle(s): "
            f"{result['posted']} posted, {result['errors']} error(s).",
            ephemeral=True,
        )

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

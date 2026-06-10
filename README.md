# gifharvest

Scrapes GIFs from a tracked list of shitpost X/Twitter accounts and reposts them to a Discord channel. Never posts the same thing twice.

Repo: https://github.com/Codeptor/TwitterGIFharvest

## How it works

A background loop polls every `POLL_MINUTES`. Each tracked handle is scraped via [twscrape](https://github.com/vladkens/twscrape) using cookie-authenticated donor X accounts. X stores "GIFs" as looping mp4s (`animated_gif` media) — those are picked out of each tweet (plain videos and retweets are opt-in via `INCLUDE_VIDEOS` / `INCLUDE_RETWEETS`). A SQLite store dedupes by both tweet id and media URL, so retweets and reposts of an already-seen GIF are skipped. New candidates are posted to `GIF_CHANNEL_ID` as an mp4 upload captioned `@author · <tweet link>`; files over the guild upload limit fall back to a `d.fxtwitter.com` embed link. With `CONVERT_TO_GIF=true` the mp4 is converted to a real `.gif` via ffmpeg (two-pass palette), falling back to the mp4 if the gif exceeds the upload limit. The first scrape of a newly tracked handle posts only the newest `BACKFILL_COUNT` and marks the rest seen, so adding a handle doesn't flood the channel.

## Setup

1. Install dependencies:

   ```sh
   uv sync
   ```

2. Create a Discord app at https://discord.com/developers/applications — add a bot, copy the bot token. No privileged intents needed. Invite it with:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=52224
   ```

   (View Channel + Send Messages + Embed Links + Attach Files.)

3. Configure:

   ```sh
   cp .env.example .env
   ```

   Fill `DISCORD_TOKEN` and `GIF_CHANNEL_ID` (enable Developer Mode in Discord, right-click the channel → Copy ID). Optionally set `GUILD_ID` so slash commands sync instantly instead of taking ~1h globally.

4. Add an X donor account. Use a **burner** — the ban risk is real, do not use an account you care about. Log into x.com with the burner, open devtools → Application → Cookies → copy the `auth_token` and `ct0` values, then:

   ```sh
   uv run gifharvest accounts add <burner_username> --cookies "auth_token=...; ct0=..."
   ```

   Rate limits scale with the number of donor accounts in the pool — add more burners if you track many handles.

5. Track some handles:

   ```sh
   uv run gifharvest track add <handle> [<handle> ...]
   ```

6. Verify scraping works (dry run, posts nothing):

   ```sh
   uv run gifharvest scrape
   ```

7. Run the bot:

   ```sh
   uv run gifharvest run
   ```

## CLI

```
uv run gifharvest run                                  # start the Discord bot + poll loop
uv run gifharvest scrape [--mark-seen]                 # dry-run scrape; --mark-seen marks results seen
uv run gifharvest track add|remove|list [<handles>]    # manage tracked handles
uv run gifharvest accounts add <user> --cookies "..."  # add a donor X account
uv run gifharvest accounts list                        # show donor account pool
uv run gifharvest stats                                # store stats
```

## Slash commands

- `/track add|remove|list` — manage tracked handles (mutating subcommands require Manage Guild)
- `/scan` — trigger an immediate scrape (Manage Guild)
- `/harveststats` — store stats

## Running as a service (systemd)

A user unit is provided in `deploy/gifharvest.service`:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/gifharvest.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gifharvest
```

Logs: `journalctl --user -u gifharvest -f`. For the service to run while you are logged out: `loginctl enable-linger $USER`.

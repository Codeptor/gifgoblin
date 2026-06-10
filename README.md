# gifgoblin

Scrapes GIFs from a tracked list of shitpost X/Twitter accounts and reposts them to a Discord channel. Never posts the same thing twice. (The Python package and CLI command are still `gifharvest` — only the product/repo name is gifgoblin.)

Repo: https://github.com/Codeptor/gifgoblin

## How it works

A background loop polls every `POLL_MINUTES`. Each tracked handle is scraped via [twscrape](https://github.com/vladkens/twscrape) using cookie-authenticated donor X accounts. X stores "GIFs" as looping mp4s (`animated_gif` media) — those are picked out of each tweet (plain videos and retweets are opt-in via `INCLUDE_VIDEOS` / `INCLUDE_RETWEETS`). A SQLite store dedupes by both tweet id and media URL, so retweets and reposts of an already-seen GIF are skipped. New candidates are posted to `GIF_CHANNEL_ID` captioned `@author · <tweet link>`. By default (`CONVERT_TO_GIF=true`) each mp4 is converted to a real autoplaying, looping `.gif` via ffmpeg (two-pass palette), scaled so its longest side fits within `GIF_MAX_WIDTH` (fit-in-box, never upscaled); if ffmpeg is missing or the converted gif exceeds the guild upload limit, the bot automatically falls back to uploading the mp4. Set `CONVERT_TO_GIF=false` to post raw mp4s instead. Files over the guild upload limit fall back to a `d.fxtwitter.com` embed link. The first scrape of a newly tracked handle posts only the newest `BACKFILL_COUNT` and marks the rest seen, so adding a handle doesn't flood the channel.

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
   uv run gifharvest accounts add <burner_username>
   ```

   Paste the cookie string (`auth_token=...; ct0=...`) at the hidden prompt. `--cookies "..."` works too, but it leaves the `auth_token` (a full account-takeover credential) in your shell history and `/proc` — prefer the prompt. Re-running the command for an existing username replaces its stored cookies, which is how you recover an expired session.

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
uv run gifharvest accounts add <user> [--cookies ...]  # add/refresh a donor X account (cookies prompted if omitted)
uv run gifharvest accounts list                        # show donor account pool
uv run gifharvest stats                                # store stats
```

## Slash commands

- `/track add|remove|list` — manage tracked handles (requires Manage Guild; Discord applies group permissions to all subcommands)
- `/scan` — trigger an immediate scrape (Manage Guild)
- `/harveststats` — store stats

## Running as a service (systemd)

A user unit is provided in `deploy/gifgoblin.service`. It expects the repo cloned at `~/gifgoblin` (the default directory of a fresh `git clone https://github.com/Codeptor/gifgoblin`):

```sh
mkdir -p ~/.config/systemd/user
cp deploy/gifgoblin.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gifgoblin
```

Logs: `journalctl --user -u gifgoblin -f`. For the service to run while you are logged out: `loginctl enable-linger $USER`.

## Running with Docker

Docker Compose runs the bot with ffmpeg included, using your local `.env` and `data/` directory for secrets and state:

```sh
cp .env.example .env
# fill DISCORD_TOKEN, GIF_CHANNEL_ID, GUILD_ID, and add donor cookies locally
docker compose up -d --build
docker compose logs -f gifgoblin
```

Stop it with:

```sh
docker compose down
```

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
uv run gifharvest accounts credentials <user>          # store burner login/email creds for automated relogin
uv run gifharvest accounts relogin [<user> ...]        # rerun twscrape's X login flow
uv run gifharvest accounts browser-refresh <user>      # open a browser, log into X, extract auth_token/ct0
uv run gifharvest accounts list                        # show donor account pool
uv run gifharvest stats                                # store stats
```

For the most automated path, store **burner-only** credentials once:

```sh
uv run gifharvest accounts credentials <burner_username> --login-now
```

This stores the X password, verification email, email IMAP/app-password, optional
TOTP seed, and optional proxy in `data/accounts.db`. After that, cookie expiry can
usually be repaired on the VPS with:

```sh
uv run gifharvest accounts relogin <burner_username>
```

In Docker on the VPS:

```sh
cd /home/deploy/bots/gifgoblin
docker compose exec -it gifgoblin gifharvest accounts credentials <burner_username> --login-now
docker compose exec -T gifgoblin gifharvest accounts list
```

`accounts browser-refresh` uses a Playwright Chromium profile under
`.browser-profiles/<user>` and waits for you to log into X manually. On a headless
VPS, this requires a browser UI such as SSH X11 forwarding; otherwise use
`accounts add` and paste the cookie string at the hidden prompt.

With Docker on the VPS:

```sh
cd /home/deploy/bots/gifgoblin
docker compose run --rm -it gifgoblin sh -lc 'python -m playwright install --with-deps chromium && gifharvest accounts browser-refresh <burner_username>'
```

If the VPS has no browser UI, use:

```sh
cd /home/deploy/bots/gifgoblin
docker compose run --rm -i gifgoblin gifharvest accounts add <burner_username>
```

The bot checks donor account health after each poll. If a donor is inactive or
`logged_in=no`, it first tries `twscrape` relogin once for accounts with stored
credentials. If automated recovery fails or no stored credentials exist, it sends
a one-time warning to `ALERT_CHANNEL_ID` (or `GIF_CHANNEL_ID` when unset), then
sends a recovery message once health returns.

## Slash commands

- `/track add|remove|list` — manage tracked handles (requires Manage Guild; Discord applies group permissions to all subcommands)
- `/scan` — trigger an immediate scrape (Manage Guild)
- `/get <tweet link>` — fetch that tweet's gif/video and post it to the channel; accepts x.com / twitter.com / fxtwitter / vxtwitter links and bare tweet ids, and includes retweets and plain videos regardless of the poll-loop opt-ins (open to everyone — restrict it under Server Settings → Integrations if needed)
- `/status` — show tracked accounts, poll timing, latest GIF, total posts, donor account health, and polling interval (Manage Guild)
- `/harveststats` — store stats

To post GIFs when someone simply pastes a tweet link in chat, enable
`MESSAGE_LINKS_ENABLED=true` and turn on **Message Content Intent** for the bot in
the Discord Developer Portal. Without that portal setting, Discord does not send
message text to the bot.

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

## Deploying from GitHub Actions

Pushes to `main` can deploy to the VPS through `.github/workflows/deploy.yml`. The workflow SSHes to `/home/deploy/bots/gifgoblin`, resets to the pushed commit, runs `docker compose up -d --build`, and posts an update message to Discord channel `1385304293845766366`.

Required repository secret:

```text
VPS_SSH_KEY
```

`VPS_SSH_KEY` must be a private SSH key whose public key is present in `/home/deploy/.ssh/authorized_keys` on the VPS.

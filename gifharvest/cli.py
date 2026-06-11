from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import logging
import sys
from pathlib import Path

from twscrape import API, NoAccountError, set_log_level

from .config import Config
from .db import Store
from .scraper import TwitterScraper, normalize_handle
from .twpatch import apply_xclid_fallback

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"

_NO_ACCOUNT_HINT = (
    f"{RED}No usable X account in the pool.{RESET}\n"
    "Add a donor account with cookies from a logged-in browser session:\n"
    "  uv run gifharvest accounts add <user>"
)

_RATE_LIMITED_HINT = (
    f"{RED}All donor accounts are currently rate-limited.{RESET}\n"
    "Wait for the limits to reset, or add more burners to the pool."
)


def _open_api(cfg: Config, **kwargs) -> API:
    # twscrape opens its pool db with a bare sqlite connect and never creates
    # the parent directory — a fresh clone would crash on `accounts add`
    cfg.accounts_db.parent.mkdir(parents=True, exist_ok=True)
    # tolerate twscrape's broken x-client-transaction-id generation (see twpatch)
    apply_xclid_fallback()
    return API(str(cfg.accounts_db), **kwargs)


def _setup_logging(cfg: Config) -> None:
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if cfg.log_level != "DEBUG":
        set_log_level("WARNING")


async def cmd_run() -> None:
    cfg = Config.load(require_discord=True)
    _setup_logging(cfg)
    store = await Store.open(cfg.db_path)
    try:
        api = _open_api(cfg, raise_when_no_account=True)
        scraper = TwitterScraper(api, cfg)
        # imported lazily so track/stats/accounts never pay the discord import cost
        from .bot import GifHarvestBot

        bot = GifHarvestBot(cfg, store, scraper)
        async with bot:
            await bot.start(cfg.discord_token)
    finally:
        await store.close()


async def cmd_scrape(mark_seen: bool) -> None:
    cfg = Config.load(require_discord=False)
    _setup_logging(cfg)
    store = await Store.open(cfg.db_path)
    try:
        handles = await store.handles()
        if not handles:
            print("No handles tracked. Add some with: uv run gifharvest track add <handle>")
            return

        api = _open_api(cfg, raise_when_no_account=True)
        scraper = TwitterScraper(api, cfg)
        total = 0
        try:
            for handle in handles:
                candidates = await scraper.fetch_new(store, handle)
                if candidates is None:
                    print(f"{BOLD}@{handle}{RESET}  {RED}could not resolve{RESET}")
                    continue
                print(f"{BOLD}@{handle}{RESET}  ({len(candidates)} new)")
                for c in candidates:
                    rt = " (RT)" if c.via_retweet else ""
                    print(
                        f"  {DIM}{c.tweet_date.isoformat()}{RESET}"
                        f"  [{c.kind.value}] @{c.author}{rt}  {CYAN}{c.media_url}{RESET}"
                    )
                    if mark_seen:
                        await store.mark_seen(c)
                if mark_seen:
                    for tweet_id in {c.tweet_id for c in candidates}:
                        await store.mark_tweet_seen(tweet_id)
                total += len(candidates)
        except NoAccountError:
            rate_limited = await scraper.has_active_accounts()
            print(_RATE_LIMITED_HINT if rate_limited else _NO_ACCOUNT_HINT, file=sys.stderr)
            sys.exit(1)

        suffix = " — marked seen" if mark_seen else " (left unposted; the bot will pick them up)"
        print(f"\n{BOLD}{total}{RESET} new candidate(s) across {len(handles)} handle(s){suffix}")
    finally:
        await store.close()


async def cmd_track(action: str, raw_handles: list[str]) -> None:
    cfg = Config.load(require_discord=False)
    store = await Store.open(cfg.db_path)
    try:
        if action == "list":
            tracked = await store.handles()
            if not tracked:
                print("No handles tracked.")
                return
            for h in tracked:
                print(f"  @{h}")
            return

        for raw in raw_handles:
            handle = normalize_handle(raw)
            if handle is None:
                print(f"{RED}✗{RESET} {raw}: not a valid handle")
                continue
            if action == "add":
                if await store.add_handle(handle):
                    print(f"{GREEN}✓{RESET} @{handle} added")
                else:
                    print(f"{DIM}-{RESET} @{handle} already tracked")
            else:
                if await store.remove_handle(handle):
                    print(f"{GREEN}✓{RESET} @{handle} removed")
                else:
                    print(f"{RED}✗{RESET} @{handle} was not tracked")
    finally:
        await store.close()


def _read_cookies() -> str:
    # keep the auth_token off argv: no shell history, no /proc/<pid>/cmdline
    if sys.stdin.isatty():
        return getpass.getpass('cookie string ("auth_token=...; ct0=..."): ').strip()
    return sys.stdin.read().strip()


def _read_optional(prompt: str, *, secret: bool = False) -> str | None:
    value = getpass.getpass(prompt).strip() if secret else input(prompt).strip()
    return value or None


async def cmd_accounts_add(username: str, cookies: str | None) -> None:
    cfg = Config.load(require_discord=False)
    _setup_logging(cfg)
    if cookies is None:
        cookies = _read_cookies()
    if not cookies:
        print(f"{RED}✗{RESET} empty cookie string", file=sys.stderr)
        sys.exit(1)
    api = _open_api(cfg)
    # add_account_cookies silently ignores existing usernames — delete first
    # so re-running with fresh cookies actually refreshes the session
    existing = await api.pool.get_account(username)
    if existing is not None:
        await api.pool.delete_accounts(username)
    await api.pool.add_account_cookies(username, cookies)
    verb = "cookies refreshed" if existing is not None else "added to the pool"
    print(f"{GREEN}✓{RESET} account @{username} {verb}")


async def cmd_accounts_credentials(username: str, login_now: bool) -> None:
    cfg = Config.load(require_discord=False)
    _setup_logging(cfg)
    print(
        "These credentials are stored in accounts.db for automated twscrape relogin.\n"
        "Use a burner X account and an email app-password, not your main password."
    )
    password = _read_optional("X password: ", secret=True)
    email = _read_optional("Email address for X verification codes: ")
    email_password = _read_optional("Email IMAP/app password: ", secret=True)
    mfa_code = _read_optional("TOTP seed/base32 secret (optional): ", secret=True)
    proxy = _read_optional("Proxy URL (optional): ")

    if not password or not email or not email_password:
        print(
            f"{RED}✗{RESET} X password, email, and email IMAP/app password are required.",
            file=sys.stderr,
        )
        sys.exit(1)

    api = _open_api(cfg)
    existing = await api.pool.get_account(username)
    if existing is not None:
        await api.pool.delete_accounts(username)
    await api.pool.add_account(
        username=username,
        password=password,
        email=email,
        email_password=email_password,
        proxy=proxy,
        mfa_code=mfa_code,
    )
    print(f"{GREEN}✓{RESET} stored login credentials for @{username}")

    if login_now:
        await cmd_accounts_relogin([username])


async def cmd_accounts_browser_refresh(
    username: str, profile_dir: str | None, headless: bool
) -> None:
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            f"{RED}✗{RESET} Playwright is not installed in this environment.\n"
            "Run `uv sync` after pulling the latest repo, then try again.",
            file=sys.stderr,
        )
        sys.exit(1)

    if headless:
        print(f"{DIM}Headless mode only works if this browser profile is already logged in.{RESET}")

    cfg = Config.load(require_discord=False)
    _setup_logging(cfg)
    user_data_dir = Path(profile_dir or f".browser-profiles/{username}").expanduser()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    print(f"Opening Chromium with profile: {user_data_dir}")
    print("Log into X in the browser window, then come back here and press Enter.")
    try:
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(user_data_dir),
                headless=headless,
                viewport={"width": 1280, "height": 900},
            )
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://x.com/home", wait_until="domcontentloaded")
            await asyncio.to_thread(input, "Press Enter after X is logged in...")
            cookies = await context.cookies(["https://x.com", "https://twitter.com"])
            await context.close()
    except PlaywrightError as exc:
        print(f"{RED}✗{RESET} could not launch browser: {exc}", file=sys.stderr)
        print(
            "\nOn a VPS, this needs a browser UI, for example SSH X11 forwarding.\n"
            "Install the browser once with:\n"
            "  docker compose run --rm gifgoblin python -m playwright install chromium\n"
            "If there is no GUI, use the reliable fallback:\n"
            "  docker compose run --rm -i gifgoblin gifharvest accounts add "
            f"{username}",
            file=sys.stderr,
        )
        sys.exit(1)

    cookie_map = {c["name"]: c["value"] for c in cookies}
    auth_token = cookie_map.get("auth_token")
    ct0 = cookie_map.get("ct0")
    if not auth_token or not ct0:
        print(
            f"{RED}✗{RESET} auth_token and ct0 were not found. "
            "Make sure the browser is logged into x.com, then retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    await cmd_accounts_add(username, f"auth_token={auth_token}; ct0={ct0}")


async def cmd_accounts_relogin(usernames: list[str]) -> None:
    cfg = Config.load(require_discord=False)
    _setup_logging(cfg)
    api = _open_api(cfg)
    if usernames:
        print(f"Relogging: {', '.join('@' + x for x in usernames)}")
        await api.pool.relogin(usernames)
    else:
        scraper = TwitterScraper(api, cfg)
        health = await scraper.account_health()
        reloginable = [str(x) for x in health["reloginable"]]
        if not reloginable:
            print("No unhealthy donor accounts with stored credentials found.")
            return
        print(f"Relogging unhealthy account(s): {', '.join('@' + x for x in reloginable)}")
        await api.pool.relogin(reloginable)
    await cmd_accounts_list()


async def cmd_accounts_list() -> None:
    cfg = Config.load(require_discord=False)
    _setup_logging(cfg)
    api = _open_api(cfg)
    infos = await api.pool.accounts_info()
    if not infos:
        print("No accounts in the pool.")
        return

    headers = ("username", "active", "logged_in", "last_used", "total_req", "error")
    rows = [
        (
            x["username"],
            "yes" if x["active"] else "no",
            "yes" if x["logged_in"] else "no",
            x["last_used"].strftime("%Y-%m-%d %H:%M") if x["last_used"] else "-",
            str(x["total_req"]),
            x["error_msg"] or "",
        )
        for x in infos
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    print(BOLD + "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)) + RESET)
    for row in rows:
        cells = [cell.ljust(w) for cell, w in zip(row, widths, strict=True)]
        ok = row[1] == "yes"
        cells[1] = f"{GREEN if ok else RED}{cells[1]}{RESET}"
        print("  ".join(cells))


async def cmd_stats() -> None:
    cfg = Config.load(require_discord=False)
    store = await Store.open(cfg.db_path)
    try:
        s = await store.stats()
        print(f"{BOLD}tracked{RESET}      {s['tracked']}")
        print(f"{BOLD}posts{RESET}        {s['posts']}")
        print(f"{BOLD}last_posted{RESET}  {s['last_posted'] or '-'}")
    finally:
        await store.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gifharvest",
        description="Scrape GIFs from tracked X accounts and repost them to Discord.",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="run the Discord bot with the background poll loop (default)")

    p_scrape = sub.add_parser("scrape", help="dry-run one scrape cycle without Discord")
    p_scrape.add_argument(
        "--mark-seen",
        action="store_true",
        help="mark found candidates as seen so the bot never posts them",
    )

    p_track = sub.add_parser("track", help="manage tracked handles")
    track_sub = p_track.add_subparsers(dest="track_command", required=True)
    track_sub.add_parser("add", help="track handles").add_argument("handles", nargs="+")
    track_sub.add_parser("remove", help="untrack handles").add_argument("handles", nargs="+")
    track_sub.add_parser("list", help="list tracked handles")

    p_acc = sub.add_parser("accounts", help="manage twscrape donor accounts")
    acc_sub = p_acc.add_subparsers(dest="accounts_command", required=True)
    p_acc_add = acc_sub.add_parser("add", help="add a donor account via cookies")
    p_acc_add.add_argument("username")
    p_acc_add.add_argument(
        "--cookies",
        help='cookie string, e.g. "auth_token=...; ct0=..." (omit to enter it '
        "at a hidden prompt or pipe it via stdin)",
    )
    p_acc_credentials = acc_sub.add_parser(
        "credentials", help="store full burner credentials for automated relogin"
    )
    p_acc_credentials.add_argument("username")
    p_acc_credentials.add_argument(
        "--login-now",
        action="store_true",
        help="run twscrape relogin immediately after storing credentials",
    )
    p_acc_relogin = acc_sub.add_parser(
        "relogin", help="run twscrape relogin for usernames, or unhealthy accounts"
    )
    p_acc_relogin.add_argument("usernames", nargs="*")
    p_acc_refresh = acc_sub.add_parser(
        "browser-refresh", help="refresh cookies from a manual X browser login"
    )
    p_acc_refresh.add_argument("username")
    p_acc_refresh.add_argument(
        "--profile-dir",
        help="persistent browser profile directory (default: .browser-profiles/<username>)",
    )
    p_acc_refresh.add_argument(
        "--headless",
        action="store_true",
        help="run without a visible browser; only useful if the profile is already logged in",
    )
    acc_sub.add_parser("list", help="list pool accounts")

    sub.add_parser("stats", help="show database stats")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    command = args.command or "run"

    match command:
        case "run":
            with contextlib.suppress(KeyboardInterrupt):
                asyncio.run(cmd_run())
        case "scrape":
            asyncio.run(cmd_scrape(args.mark_seen))
        case "track":
            asyncio.run(cmd_track(args.track_command, getattr(args, "handles", [])))
        case "accounts":
            if args.accounts_command == "add":
                asyncio.run(cmd_accounts_add(args.username, args.cookies))
            elif args.accounts_command == "credentials":
                asyncio.run(cmd_accounts_credentials(args.username, args.login_now))
            elif args.accounts_command == "relogin":
                asyncio.run(cmd_accounts_relogin(args.usernames))
            elif args.accounts_command == "browser-refresh":
                asyncio.run(
                    cmd_accounts_browser_refresh(args.username, args.profile_dir, args.headless)
                )
            else:
                asyncio.run(cmd_accounts_list())
        case "stats":
            asyncio.run(cmd_stats())

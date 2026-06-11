"""Resilience patch for twscrape's x-client-transaction-id generation.

As of 2026-06 X shipped a new ``x-web`` frontend build that removed the script
manifest and ``ondemand.s`` indices twscrape parses to build the
``x-client-transaction-id`` header. ``XClIdGen.create()`` therefore raises
"Failed to parse scripts" and, because that happens *before* the request is sent,
every GraphQL call aborts — scraping stops entirely (twscrape issue #248).

Empirically X still serves cookie-authenticated read endpoints (UserByScreenName,
UserTweets, …) when the header is an empty string, so we fall back to an empty
``x-client-transaction-id`` whenever generation fails. Real ids are still used
whenever twscrape *can* build them, so this transparently self-heals once the
parser is fixed upstream (a dependency bump + restart re-tries real generation).
"""

from __future__ import annotations

import logging

import twscrape.queue_client as _qc

logger = logging.getLogger(__name__)


class _EmptyXClId:
    """Stand-in transaction-id generator that yields an empty header value."""

    def calc(self, method: str, path: str) -> str:
        return ""


_EMPTY = _EmptyXClId()
_warned = False


def apply_xclid_fallback() -> None:
    """Make twscrape tolerate a broken x-client-transaction-id generator.

    Idempotent. Wraps ``XClIdGenStore.get`` so a generation failure caches and
    returns an empty-header generator for the process instead of raising.
    """
    store = _qc.XClIdGenStore
    if getattr(store, "_gifharvest_patched", False):
        return

    original_get = store.get.__func__  # unwrap the existing classmethod

    async def get(cls, username: str, fresh: bool = False):
        try:
            return await original_get(cls, username, fresh=fresh)
        except Exception as exc:
            global _warned
            if not _warned:
                logger.warning(
                    "twscrape could not build x-client-transaction-id (%s); falling back "
                    "to an empty header (X currently accepts it). See gifharvest/twpatch.py.",
                    exc,
                )
                _warned = True
            # cache so subsequent requests skip the failing 3x retry this process
            cls.items[username] = _EMPTY
            return _EMPTY

    store.get = classmethod(get)
    store._gifharvest_patched = True

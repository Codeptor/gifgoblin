from __future__ import annotations

import twscrape.queue_client as qc

from gifharvest.twpatch import apply_xclid_fallback


async def test_fallback_returns_empty_header_when_generation_fails(monkeypatch):
    qc.XClIdGenStore.items.clear()
    monkeypatch.setattr(qc.XClIdGenStore, "_gifharvest_patched", False, raising=False)

    async def boom(cls, username, fresh=False):
        raise Exception("Failed to parse scripts")

    monkeypatch.setattr(qc.XClIdGenStore, "get", classmethod(boom))

    apply_xclid_fallback()
    gen = await qc.XClIdGenStore.get("burner")
    assert gen.calc("GET", "/i/api/graphql/x/UserByScreenName") == ""
    # failure is cached so the next call skips the failing retry path
    assert qc.XClIdGenStore.items["burner"] is gen


async def test_fallback_preserves_real_generator_on_success(monkeypatch):
    qc.XClIdGenStore.items.clear()
    monkeypatch.setattr(qc.XClIdGenStore, "_gifharvest_patched", False, raising=False)
    sentinel = object()

    async def ok(cls, username, fresh=False):
        return sentinel

    monkeypatch.setattr(qc.XClIdGenStore, "get", classmethod(ok))

    apply_xclid_fallback()
    assert await qc.XClIdGenStore.get("burner") is sentinel


async def test_apply_is_idempotent(monkeypatch):
    monkeypatch.setattr(qc.XClIdGenStore, "_gifharvest_patched", False, raising=False)
    apply_xclid_fallback()
    # a classmethod yields a fresh bound method per access, so compare __func__
    first = qc.XClIdGenStore.get.__func__
    apply_xclid_fallback()
    assert qc.XClIdGenStore.get.__func__ is first

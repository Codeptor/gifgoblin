from __future__ import annotations

from types import SimpleNamespace

from gifharvest.bot import _donor_alert_state, _format_account_health
from gifharvest.scraper import TwitterScraper


class FakePool:
    def __init__(self, infos: list[dict], accounts: list[SimpleNamespace]):
        self._infos = infos
        self._accounts = accounts

    async def accounts_info(self) -> list[dict]:
        return self._infos

    async def get_all(self) -> list[SimpleNamespace]:
        return self._accounts


def _scraper(infos: list[dict], accounts: list[SimpleNamespace] | None = None) -> TwitterScraper:
    api = SimpleNamespace(pool=FakePool(infos, accounts or []))
    return TwitterScraper(api, SimpleNamespace())


def _info(username: str, *, active: bool, logged_in: bool, error_msg=None) -> dict:
    return {"username": username, "active": active, "logged_in": logged_in, "error_msg": error_msg}


async def test_cookie_auth_donor_is_healthy_despite_logged_in_no():
    # the exact state a freshly cookie-refreshed donor reports: active, not
    # logged_in, no error — must NOT be flagged as needing a refresh
    health = await _scraper([_info("donor1", active=True, logged_in=False)]).account_health()
    assert health["errors"] == []
    state, message = _donor_alert_state(health)
    assert state == "ok"
    assert message is None


async def test_error_msg_literal_none_string_is_not_an_error():
    # twscrape stores "no error" as the string "None", not Python None
    health = await _scraper(
        [_info("donor1", active=True, logged_in=False, error_msg="None")]
    ).account_health()
    assert health["errors"] == []
    assert _donor_alert_state(health)[0] == "ok"


async def test_inactive_donor_alerts():
    health = await _scraper([_info("burner", active=False, logged_in=False)]).account_health()
    assert health["errors"] == ["burner"]
    state, message = _donor_alert_state(health)
    assert state != "ok"
    assert "burner" in message


async def test_errored_donor_alerts():
    health = await _scraper(
        [_info("burner", active=True, logged_in=True, error_msg="rate limited")]
    ).account_health()
    assert health["errors"] == ["burner"]
    assert _donor_alert_state(health)[0] != "ok"


async def test_no_accounts_alerts():
    health = await _scraper([]).account_health()
    state, message = _donor_alert_state(health)
    assert state == "none"
    assert "No X donor accounts" in message


def test_format_account_health_omits_logged_in():
    summary = _format_account_health(
        {"total": 1, "active": 1, "logged_in": 0, "errors": [], "reloginable": []}
    )
    assert summary == "1/1 active"
    assert "logged in" not in summary

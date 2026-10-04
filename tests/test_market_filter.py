# -*- coding: utf-8 -*-
"""Market Cap filtreleme ve Jupiter istemci ayristirma testleri.

Ag baglantisi GEREKTIRMEZ: requests.Session yerine sahte (fake)
session enjekte edilerek API yanitlari taklit edilir. Boylece
$20,000 alti coin'lerin otomatik elendigi deterministik dogrulanir.
"""

from __future__ import annotations

import pytest

from tests.conftest import MEME_A, MEME_B, MEME_C
from paperbot.data.jupiter_client import JupiterClient
from paperbot.data.market_data import MarketDataService


class FakeResponse:
    """requests.Response'un testte ihtiyac duyulan minimum sozlesmesi."""

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class FakeSession:
    """URL'e gore onceden hazirlanmis JSON donduren sahte HTTP istemcisi."""

    def __init__(self, tokens_payload=None, prices_payload=None, fail=False):
        self.headers: dict[str, str] = {}
        self._tokens_payload = tokens_payload or []
        self._prices_payload = prices_payload or {"data": {}}
        self._fail = fail
        self.calls: list[str] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        if self._fail:
            import requests as _rq

            raise _rq.ConnectionError("test-te taklit edilen ag hatasi")
        if "/search" in url:
            return FakeResponse(self._tokens_payload)
        return FakeResponse(self._prices_payload)


def _token_row(mint, symbol, market_cap):
    return {
        "address": mint,
        "symbol": symbol,
        "name": f"{symbol} Coin",
        "marketCap": market_cap,
        "categories": ["memecoin"],
    }


@pytest.fixture()
def jupiter_with_pool() -> JupiterClient:
    """Uc tokenlik sahte havuz: $50K / $12K / $400K."""
    session = FakeSession(
        tokens_payload=[
            _token_row(MEME_A, "TESTA", 50_000.0),
            _token_row(MEME_B, "TESTB", 12_000.0),   # <- $20K alti: ELENECEK
            _token_row(MEME_C, "TESTC", 400_000.0),
        ]
    )
    return JupiterClient(
        price_api_url="https://fake/price/v3",
        tokens_api_url="https://fake/tokens/v2",
        session=session,
    )


@pytest.fixture()
def service(jupiter_with_pool) -> MarketDataService:
    return MarketDataService(
        jupiter=jupiter_with_pool,
        min_market_cap_usd=20_000.0,
        quote_max_age_seconds=90,
    )


class TestMarketCapFilter:
    def test_under_20k_is_skipped(self, service):
        accepted = service.refresh_candidate_pool()
        mints = {t.mint_address for t in accepted}
        assert MEME_A in mints
        assert MEME_C in mints
        # Gorev adimi 2: $20K alti otomatik pas gecilmeli
        assert MEME_B not in mints
        assert service.filtered_out_count == 1

    def test_sorted_by_lowest_market_cap_first(self, service):
        accepted = service.refresh_candidate_pool()
        caps = [t.market_cap_usd for t in accepted]
        assert caps == sorted(caps)

    def test_exact_threshold_passes(self, jupiter_with_pool):
        """Tam $20,000 filtreden GECMELI (< karsilastirmasi, <= degil)."""
        session = FakeSession(tokens_payload=[_token_row(MEME_B, "TESTB", 20_000.0)])
        client = JupiterClient("https://fake/p", "https://fake/t", session=session)
        svc = MarketDataService(client, 20_000.0, 90)
        accepted = svc.refresh_candidate_pool()
        assert len(accepted) == 1


class TestQuoteParsing:
    def test_price_v3_parsing(self, service):
        svc_session = service._jupiter._session
        svc_session._prices_payload = {
            "data": {MEME_A: {"USD": "0.00123"}, MEME_C: {"USD": "0.5"}}
        }
        quotes = service.update_prices([MEME_A, MEME_C])
        assert quotes[MEME_A].price_usd == pytest.approx(0.00123)
        assert quotes[MEME_A].source == "jupiter"

    def test_invalid_entries_are_dropped(self, service):
        svc_session = service._jupiter._session
        svc_session._prices_payload = {
            "data": {
                MEME_A: {"USD": "not-a-number"},
                MEME_C: {"USD": "-1"},
            }
        }
        assert service.update_prices([MEME_A, MEME_C]) == {}

    def test_malformed_token_rows_survive(self):
        # NOT: "x" adresi uzunluk kuralina uydugu icin parse edilir;
        # asil dogrulama bozuk/eksik alanli kaydin (None donen) atlanmasi.
        session = FakeSession(
            tokens_payload=[
                {"address": MEME_A},                   # symbol/marketCap eksik -> elenir
                _token_row(MEME_A, "TESTA", 50_000.0),  # saglam kayit
            ]
        )
        client = JupiterClient("https://fake/p", "https://fake/t", session=session)
        tokens = client.top_tokens()
        assert len(tokens) == 1
        assert tokens[0].symbol == "TESTA"


class TestNetworkResilience:
    def test_api_failure_returns_empty_not_crash(self):
        session = FakeSession(fail=True)
        client = JupiterClient("https://fake/p", "https://fake/t", session=session)
        assert client.top_tokens() == []
        assert client.fetch_prices([MEME_A]) == {}

    def test_get_price_none_when_api_down(self, service):
        service._jupiter._session._fail = True
        # Havuzda olmayan/bayatsiz mint icin fiyat yoksa None donmeli
        assert service.get_price(MEME_A) is None

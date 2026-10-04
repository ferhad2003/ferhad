# -*- coding: utf-8 -*-
"""PaperBroker muhasebe testleri (gorev adimi 3 dogrulamasi).

Dogrulanan davranislar:
  - Alimda nakit eksilir, pozisyon acilir, slippage+fee uygulanir.
  - Satimda kesir mantigi ve realize P&L dogru hesaplanir.
  - Ayni token'a cift giris engellenir.
  - Nakit yetersizse bütce kirpilir ya da islem reddedilir.
  - State JSON'a yazilip geri okunabilir (crash recovery).
"""

from __future__ import annotations

import pytest

from tests.conftest import MEME_A, make_quote, make_token
from paperbot.models import PositionState
from paperbot.portfolio.broker import PaperBroker


@pytest.fixture()
def broker(tmp_path) -> PaperBroker:
    """Izole gecici dizinde temiz bir paper hesap."""
    return PaperBroker(
        starting_balance_usd=10_000.0,
        slippage_pct=0.5,           # %0.5 kotu fiyat senaryosu
        gas_fee_usd=0.01,
        state_file=tmp_path / "state.json",
    )


TOKEN = make_token(MEME_A, "TESTA", 50_000.0)


class TestBuy:
    def test_buy_opens_position_and_debits_cash(self, broker):
        trade = broker.buy(TOKEN, make_quote(MEME_A, 0.10), budget_usd=100.0)
        assert trade is not None
        # Efektif alis fiyati = 0.10 * 1.005 = 0.1005
        assert trade.price_usd == pytest.approx(0.1005)
        # Harcama: (100 - 0.01) fee oncesi alim + 0.01 fee = ~100
        assert broker.cash_usd == pytest.approx(9_900.0, abs=1e-6)
        assert TOKEN.key in broker.positions
        pos = broker.positions[TOKEN.key]
        assert pos.quantity == pytest.approx(99.99 / 0.1005)
        assert pos.state == PositionState.OPEN

    def test_duplicate_entry_rejected(self, broker):
        broker.buy(TOKEN, make_quote(MEME_A, 0.10), 100.0)
        second = broker.buy(TOKEN, make_quote(MEME_A, 0.12), 100.0)
        assert second is None
        assert len(broker.positions) == 1

    def test_budget_capped_to_cash(self, broker):
        # 1 milyonluk bütce istegi 10K nakde kirpilmali
        trade = broker.buy(TOKEN, make_quote(MEME_A, 0.10), 1_000_000.0)
        assert trade is not None
        assert broker.cash_usd >= 0.0
        assert broker.cash_usd < 10.0  # neredeyse tamami harcandi

    def test_insufficient_cash_rejected(self, broker):
        broker.cash_usd = 0.005  # gas altinda
        assert broker.buy(TOKEN, make_quote(MEME_A, 0.10), 100.0) is None


class TestSell:
    def _open(self, broker):
        broker.buy(TOKEN, make_quote(MEME_A, 0.10), 100.0)

    def test_partial_sell_keeps_half_quantity(self, broker):
        self._open(broker)
        pos = broker.positions[TOKEN.key]
        original_qty = pos.quantity
        trade = broker.sell(MEME_A, make_quote(MEME_A, 0.20), fraction=0.5, reason="TAKE_PROFIT")
        assert trade is not None
        assert trade.quantity == pytest.approx(original_qty * 0.5)
        assert pos.quantity == pytest.approx(original_qty * 0.5)
        # Efektif satis fiyati = 0.20 * 0.995 = 0.199
        assert trade.price_usd == pytest.approx(0.199)
        # Giris 0.1005'e karsa ~%98 kar realize olmali (fee haric)
        assert trade.realized_pnl_usd > 45.0

    def test_full_sell_closes_position(self, broker):
        self._open(broker)
        trade = broker.sell(MEME_A, make_quote(MEME_A, 0.085), fraction=1.0, reason="STOP_LOSS")
        assert trade is not None
        assert TOKEN.key not in broker.positions
        # Zararla kapanis: realize P&L negatif
        assert trade.realized_pnl_usd < 0

    def test_sell_missing_position_returns_none(self, broker):
        assert broker.sell("YOKTOKEN", make_quote("YOKTOKEN", 1.0), 1.0, "MANUAL") is None


class TestAccountingConservation:
    def test_cash_plus_holdings_conservation_roundtrip(self, broker):
        """Al-sat turunda para yok olmamali/sisteme sicramamali (fee kadari azalir)."""
        start_equity = broker.cash_usd
        broker.buy(TOKEN, make_quote(MEME_A, 0.10), 100.0)
        mid_value = broker.positions[TOKEN.key].quantity * 0.10
        assert start_equity - 100.0 <= broker.cash_usd + mid_value <= start_equity

        broker.sell(MEME_A, make_quote(MEME_A, 0.10), fraction=1.0, reason="MANUAL")
        # Ayni fiyattan turun iki tarafi da %0.5 slippage + 2x0.01 fee yakar
        loss = start_equity - broker.cash_usd
        assert 0.9 < loss < 1.1  # ~$1 civari maliyet (100$'lik turda)


class TestPersistence:
    def test_state_saved_and_reloaded(self, tmp_path):
        state_file = tmp_path / "state.json"
        broker = PaperBroker(10_000.0, 0.5, 0.01, state_file)
        broker.buy(TOKEN, make_quote(MEME_A, 0.10), 100.0)
        broker.save_state()

        restored = PaperBroker(10_000.0, 0.5, 0.01, state_file)
        assert restored.cash_usd == pytest.approx(broker.cash_usd)
        assert set(restored.positions) == set(broker.positions)
        rp = restored.positions[TOKEN.key]
        bp = broker.positions[TOKEN.key]
        assert rp.quantity == pytest.approx(bp.quantity)
        assert rp.entry_price == pytest.approx(bp.entry_price)

    def test_corrupt_state_falls_back_clean(self, tmp_path):
        """Bozuk JSON botu dusurmeyecek; temiz hesapla devam eder."""
        state_file = tmp_path / "state.json"
        state_file.write_text("{bozuk json!!", encoding="utf-8")
        broker = PaperBroker(10_000.0, 0.5, 0.01, state_file)
        assert broker.cash_usd == 10_000.0
        assert broker.positions == {}

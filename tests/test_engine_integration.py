# -*- coding: utf-8 -*-
"""Ucdan uca (integration) test: al -> 2x TP -> trailing stop / SL.

PaperTradingEngine'in gercek akisi, sahte piyasa verisiyle tur tur
simule edilir. Boylece gorev adimlari 1-4'UN BIRLIKTE calistigi
dogrulanir: fiyat takibi -> filtre -> paper islem -> risk karari.
"""

from __future__ import annotations

import pytest

from config.settings import Config
from paperbot.data.market_data import MarketDataService
from paperbot.engine import PaperTradingEngine
from paperbot.execution.trade_ledger import TradeLedger
from paperbot.models import PositionState, Side
from paperbot.portfolio.broker import PaperBroker
from paperbot.risk.risk_manager import RiskManager
from tests.conftest import MEME_A, MEME_B, MEME_C, make_quote, make_token


class FakeJupiter:
    """MarketDataService'in bekledigi sozlesmeyi karsilayan sahte istemci.

    Fiyatlar test tarafindan set_price() ile kontrol edilir; boylece
    senaryolar (dusus/yukselis) deterministik olarak turlara dagitilir.
    """

    def __init__(self, tokens):
        self._tokens = tokens
        self._prices: dict[str, float] = {}

    def top_tokens(self, category="memecoin", order_by="marketCap", limit=50, page=1):
        return list(self._tokens)

    def fetch_prices(self, mints):
        result = {}
        for mint in mints:
            price = self._prices.get(mint)
            if price is not None and price > 0:
                result[mint] = make_quote(mint, price)
        return result

    def set_price(self, mint: str, price: float) -> None:
        self._prices[mint] = price


@pytest.fixture()
def engine(tmp_path):
    """Tam bagimlikli motor; state/ledger gecici dizize yazilir."""
    cfg = Config(
        starting_balance_usd=10_000.0,
        min_market_cap_usd=20_000.0,
        max_position_pct=5.0,
        stop_loss_pct=15.0,
        take_profit_multiplier=2.0,
        trailing_stop_pct=20.0,
        simulated_slippage_pct=0.5,
        simulated_gas_fee_usd=0.01,
        scan_interval_seconds=1,
        state_file=tmp_path / "state.json",
        trade_log_file=tmp_path / "trades.csv",
    )
    fake = FakeJupiter(
        tokens=[
            make_token(MEME_A, "TESTA", 50_000.0),   # strateji hedefi
            make_token(MEME_B, "TESTB", 12_000.0),   # $20K alti - ASLA ALINMAMALI
            make_token(MEME_C, "TESTC", 400_000.0),  # havuzda ama oncelik dusuk
        ]
    )
    fake.set_price(MEME_A, 0.10)
    fake.set_price(MEME_B, 0.01)
    fake.set_price(MEME_C, 1.00)

    market = MarketDataService(fake, cfg.min_market_cap_usd, cfg.quote_max_age_seconds)
    broker = PaperBroker(
        cfg.starting_balance_usd,
        cfg.simulated_slippage_pct,
        cfg.simulated_gas_fee_usd,
        cfg.state_file,
    )
    risk = RiskManager(
        cfg.stop_loss_pct, cfg.take_profit_multiplier,
        cfg.trailing_stop_pct, cfg.max_position_pct,
    )
    ledger = TradeLedger(cfg.trade_log_file)
    eng = PaperTradingEngine(cfg, market, broker, risk, ledger)
    market.refresh_candidate_pool()
    return eng, fake, broker, ledger


class TestFullLifecycle:
    def test_low_market_cap_never_bought(self, engine):
        eng, fake, broker, _ = engine
        fake.set_price(MEME_B, 0.0001)  # ucuzlasa bile almamali
        eng.run_cycle()
        assert MEME_B not in broker.positions
        buys = [t for t in broker.trade_history if t.side == Side.BUY]
        assert all(t.mint_address != MEME_B for t in buys)

    def test_entry_tp_and_trailing_stop_flow(self, engine):
        eng, fake, broker, ledger = engine

        # TUR 1: TESTA 0.10 -> pozisyon acilmali
        result = eng.run_cycle()
        assert len(result["entries"]) >= 1
        pos = broker.positions[MEME_A.lower()]
        entry_price = pos.entry_price
        assert pos.state == PositionState.OPEN

        # TUR 2: %10 yukselis -> henuz aksiyon yok
        fake.set_price(MEME_A, entry_price * 1.10)
        result = eng.run_cycle()
        assert result["exits"] == []

        # TUR 3: 2x -> TAKE PROFIT (yarim satim)
        fake.set_price(MEME_A, entry_price * 2.0)
        result = eng.run_cycle()
        tp_trades = [t for t in result["exits"] if t.reason == "TAKE_PROFIT"]
        assert len(tp_trades) == 1
        assert tp_trades[0].side == Side.SELL
        assert pos.state == PositionState.TP_PARTIAL_DONE
        assert pos.take_profit_done is True
        remaining_qty = pos.quantity

        # TUR 4: tepe 3x -> trailing tetiklenmemeli, tepe guncellenmeli
        fake.set_price(MEME_A, entry_price * 3.0)
        result = eng.run_cycle()
        assert result["exits"] == []
        assert pos.peak_price == pytest.approx(entry_price * 3.0)

        # TUR 5: tepeden %25 dusus -> TRAILING STOP kalanini kapatir
        fake.set_price(MEME_A, entry_price * 3.0 * 0.75)
        result = eng.run_cycle()
        trail_trades = [t for t in result["exits"] if t.reason == "TRAILING_STOP"]
        assert len(trail_trades) == 1
        assert trail_trades[0].quantity == pytest.approx(remaining_qty)
        assert MEME_A.lower() not in broker.positions

        # Muhasebe denetimi: bu senaryo KARLA kapanmali (giris 1x, cikis ~2.25x)
        realized = sum(t.realized_pnl_usd for t in broker.trade_history if t.side == Side.SELL)
        assert realized > 0

    def test_stop_loss_full_exit(self, engine):
        eng, fake, broker, _ = engine
        eng.run_cycle()  # giris
        pos = broker.positions[MEME_A.lower()]
        entry_price = pos.entry_price

        # %20 anilik duse -> SL tam kapanis
        fake.set_price(MEME_A, entry_price * 0.80)
        result = eng.run_cycle()
        sl = [t for t in result["exits"] if t.reason == "STOP_LOSS"]
        assert len(sl) == 1
        assert sl[0].realized_pnl_usd < 0
        assert MEME_A.lower() not in broker.positions

    def test_cooldown_blocks_immediate_reentry(self, engine):
        eng, fake, broker, _ = engine
        eng.run_cycle()
        pos = broker.positions[MEME_A.lower()]
        fake.set_price(MEME_A, pos.entry_price * 0.80)  # SL tetikle
        eng.run_cycle()
        # Ayni tur icinde yeniden alinmamali (cooldown 3600sn varsayilan)
        assert MEME_A.lower() not in broker.positions

    def test_csv_ledger_written(self, engine):
        eng, fake, broker, ledger = engine
        eng.run_cycle()
        rows = ledger.read_all()
        assert len(rows) >= 1
        assert rows[0]["symbol"] in {"TESTA", "TESTC"}

    def test_state_survives_restart(self, engine, tmp_path):
        """Crash/restart senaryosu: state dosyasindan portfoy geri yuklenir."""
        eng, fake, broker, _ = engine
        eng.run_cycle()
        assert broker.positions

        restored = PaperBroker(
            starting_balance_usd=10_000.0,
            slippage_pct=0.5,
            gas_fee_usd=0.01,
            state_file=tmp_path / "state.json",
        )
        assert set(restored.positions) == set(broker.positions)
        assert restored.cash_usd == pytest.approx(broker.cash_usd)

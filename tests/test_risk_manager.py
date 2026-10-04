# -*- coding: utf-8 -*-
"""Risk yonetim stratejisinin birim testleri (gorev adimi 4 dogrulamasi).

Senaryolar:
  - Stop-Loss: %15 dususte tam kapanis karari.
  - Take-Profit: 2x'te yarimini sat karari; ikinci kez tetiklenmemeli.
  - Trailing Stop: TP sonrasi tepe-%20 dususte kalanini kapat karari.
  - SL'in TP'den once calismasi (ayni tikta cakisma).
  - Fiyat normal bantta ise HICBIR aksiyon cikmamali.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_quote
from paperbot.models import PositionState
from paperbot.portfolio.broker import Position
from paperbot.risk.risk_manager import RiskAction, RiskManager


@pytest.fixture()
def risk() -> RiskManager:
    """Yapilandirmasiz saf risk motoru (esikler gorev spesifikasyonundan)."""
    return RiskManager(
        stop_loss_pct=15.0,
        take_profit_multiplier=2.0,
        trailing_stop_pct=20.0,
        max_position_pct=5.0,
    )


def _open_position(price: float = 0.10) -> Position:
    """Test icin tipik acik pozisyon: $0.10'dan girilmis."""
    return Position(
        mint="TESTTOKENAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        symbol="TESTA",
        quantity=1000.0,
        entry_price=price,
        invested_usd=100.0,
    )


class TestStopLoss:
    def test_triggers_at_minus_15_percent(self, risk):
        pos = _open_position(0.10)
        decision = risk.evaluate_position(pos, make_quote(pos.mint, 0.085))
        assert decision.action == RiskAction.STOP_LOSS_FULL
        assert decision.fraction == 1.0
        assert "Stop-Loss" in decision.reason

    def test_triggers_below_threshold_too(self, risk):
        pos = _open_position(0.10)
        # %40 dusus => kesinlikle kapanmali
        decision = risk.evaluate_position(pos, make_quote(pos.mint, 0.06))
        assert decision.action == RiskAction.STOP_LOSS_FULL

    def test_not_triggered_at_minus_14(self, risk):
        pos = _open_position(0.10)
        decision = risk.evaluate_position(pos, make_quote(pos.mint, 0.086))
        assert decision.action == RiskAction.NONE


class TestTakeProfit:
    def test_half_sell_at_2x(self, risk):
        pos = _open_position(0.10)
        decision = risk.evaluate_position(pos, make_quote(pos.mint, 0.20))
        assert decision.action == RiskAction.TAKE_PROFIT_HALF
        assert decision.fraction == 0.5

    def test_only_once_flag_respected(self, risk):
        pos = _open_position(0.10)
        pos.take_profit_done = True
        pos.state = PositionState.TP_PARTIAL_DONE
        # 2x hala gecerli ama bayrak yuzunden TP TEKRAR CALISMAMALI;
        # trailing de henuz tetiklenmiyor (fiyat tepeyle ayni)
        decision = risk.evaluate_position(pos, make_quote(pos.mint, 0.20))
        assert decision.action == RiskAction.NONE

    def test_no_tp_below_2x(self, risk):
        pos = _open_position(0.10)
        decision = risk.evaluate_position(pos, make_quote(pos.mint, 0.19))
        assert decision.action == RiskAction.NONE


class TestTrailingStop:
    def test_triggers_after_peak_drawdown_20(self, risk):
        pos = _open_position(0.10)
        pos.take_profit_done = True
        pos.state = PositionState.TP_PARTIAL_DONE
        # Once tepeyi gor: 0.30 (evaluate sirasinda peak guncellenir)
        risk.evaluate_position(pos, make_quote(pos.mint, 0.30))
        assert pos.peak_price == pytest.approx(0.30)
        # Tepeden %20+ dusus: 0.30 -> 0.235 (%21.7)
        decision = risk.evaluate_position(pos, make_quote(pos.mint, 0.235))
        assert decision.action == RiskAction.TRAILING_STOP_FULL
        assert decision.fraction == 1.0

    def test_not_active_before_take_profit(self, risk):
        """TP uygulanmadiysa trailing CALISMAZ (spec: 'kalan %50 icin')."""
        pos = _open_position(0.10)
        risk.evaluate_position(pos, make_quote(pos.mint, 0.30))  # tepe
        # %25 dusus var ama state OPEN -> trailing yok, SL de yok (+%150'deyiz)
        decision = risk.evaluate_position(pos, make_quote(pos.mint, 0.225))
        assert decision.action == RiskAction.NONE

    def test_no_trail_while_rising(self, risk):
        pos = _open_position(0.10)
        pos.take_profit_done = True
        pos.state = PositionState.TP_PARTIAL_DONE
        risk.evaluate_position(pos, make_quote(pos.mint, 0.30))
        decision = risk.evaluate_position(pos, make_quote(pos.mint, 0.35))
        assert decision.action == RiskAction.NONE
        assert pos.peak_price == pytest.approx(0.35)


class TestPriorityAndSafety:
    def test_stop_loss_wins_over_take_profit_in_crash_jump(self, risk):
        """Giris 0.10 -> aniden 0.08: hem SL kosulu var; tek karar SL olmali."""
        pos = _open_position(0.10)
        decision = risk.evaluate_position(pos, make_quote(pos.mint, 0.08))
        assert decision.action == RiskAction.STOP_LOSS_FULL

    def test_zero_price_is_no_action(self, risk):
        pos = _open_position(0.10)
        decision = risk.evaluate_position(pos, make_quote(pos.mint, 0.0))
        assert decision.action == RiskAction.NONE

    def test_peak_never_decreases(self, risk):
        pos = _open_position(0.10)
        risk.evaluate_position(pos, make_quote(pos.mint, 0.25))
        risk.evaluate_position(pos, make_quote(pos.mint, 0.15))
        assert pos.peak_price == pytest.approx(0.25)

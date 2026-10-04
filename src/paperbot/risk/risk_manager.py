# -*- coding: utf-8 -*-
"""Risk yonetimi motoru (gorev adimi 4).

Uygulanan strateji - SAF (Stop-Loss First) siralamasi:
  1. STOP-LOSS      : Fiyat girise gore %15 dustuyse -> POZISYON TAM KAPANIR.
                      Bu kontrol HER zaman once calisir; 2x TP ile ayni
                      tikta caksan tek-yonlu fiyat atlamalarinda bile
                      sermaye koruma oncelikli olur.
  2. TAKE-PROFIT    : Fiyat girisin 2 katina ulastiysa ve henuz TP
                      uygulanmadiysa -> pozisyonun %50'si satilir,
                      durum TP_PARTIAL_DONE'e gecer.
  3. TRAILING STOP  : TP sonrasindaki KALAN %50 icin tepe fiyattan
                      %TRAILING_STOP_PCT dusus gorulurse -> kalan kapatilir.
                      Trailing ayrica tepe-fiyat zehirlenmesine karsi
                      yalnizca TP sonrasi aktif hale gelir.

Ek guvenlik cerceveleri:
  - Bayat/eksik fiyatla karar verilmez ("no fresh quote, no action").
  - Pozisyon basina maks. sermaye orani (MAX_POSITION_PCT) burada uretilir.
  - Ayni token'a cirp-cik (churn) korumasi: kapanistan sonra cooldown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

from paperbot.models import PositionState, Quote, TokenInfo
from paperbot.portfolio.broker import PaperBroker, Position
from paperbot.utils.logger import get_logger

logger = get_logger("risk")


class RiskAction(str, Enum):
    """Risk motorunun uretebileceği karar türleri."""

    NONE = "NONE"
    OPEN_POSITION = "OPEN_POSITION"
    STOP_LOSS_FULL = "STOP_LOSS_FULL"          # %15 dusus => tamamini kapat
    TAKE_PROFIT_HALF = "TAKE_PROFIT_HALF"      # 2x => yarisini sat
    TRAILING_STOP_FULL = "TRAILING_STOP_FULL"  # tepeden dusus => kalanini kapat


@dataclass(frozen=True)
class RiskDecision:
    """Sinyal + gerekce birlikte tasinir ki loglar denetlenebilir olsun."""

    action: RiskAction
    fraction: float           # Satilacak kesir (1.0 => tam kapanis)
    reason: str               # Insan okunur aciklama (log/CSV icin)
    sell_fraction_for_record: str  # TradeRecord.reason alani eslemesi


_NO_ACTION = RiskDecision(RiskAction.NONE, 0.0, "", "")


class RiskManager:
    """Pozisyon yasam dongusu kararlarinin tek yetkili kaynagi.

    Args:
        stop_loss_pct: Giris fiyatina gore zararla kapatma esigi (%).
        take_profit_multiplier: Kismi TP tetikleyen kat (2.0 => 2x).
        trailing_stop_pct: Tepe fiyattan takip eden stop yuzdesi.
        max_position_pct: Tek pozisyon icin portfeye gore maks. pay (%).
        min_entry_notional_usd: Bundan kucuk alim emri uretilmez.
        cooldown_seconds: Ayni token'a yeniden giris icin bekleme suresi.
    """

    def __init__(
        self,
        stop_loss_pct: float,
        take_profit_multiplier: float,
        trailing_stop_pct: float,
        max_position_pct: float,
        min_entry_notional_usd: float = 5.0,
        cooldown_seconds: int = 3600,
    ) -> None:
        self._sl_pct = stop_loss_pct
        self._tp_mult = take_profit_multiplier
        self._ts_pct = trailing_stop_pct
        self._max_pos_pct = max_position_pct
        self._min_notional = min_entry_notional_usd
        self._cooldown = cooldown_seconds
        # mint -> son kapanis zamani (cirp-cik korumasinin durumu)
        self._last_exit_at: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Pozisyon olusturma karari
    # ------------------------------------------------------------------
    def position_size_usd(self, total_equity_usd: float, broker: PaperBroker) -> float:
        """Toplam ozsermayeye gore pozisyon butcesi uretir (nakit kirpmali).

        Strateji kurali: pozisyon boyutu portfoyun TOPLAM DEGERININ
        yuzdesidir; ancak odeme gucu olarak hicbir zaman mevcut nakdi
        gecemez.
        """
        budget = total_equity_usd * (self._max_pos_pct / 100.0)
        return min(budget, broker.cash_usd)

    def should_enter(
        self, token: TokenInfo, quote: Quote, broker: PaperBroker
    ) -> bool:
        """Yeni pozisyon acmak icin tum guvenlik kapilarindan gecmeli mi?

        Kontroller: market cap alt siniri cagiran tarafda zaten uygulandi;
        burada cooldown, minimum tutar ve nakit yeterliligi denetlenir.
        """
        key = token.key
        last_exit = self._last_exit_at.get(key)
        if last_exit is not None and (time.time() - last_exit) < self._cooldown:
            logger.debug(
                "Cooldown aktif: %s (%.0f sn bekleniyor)",
                token.symbol,
                self._cooldown - (time.time() - last_exit),
            )
            return False

        if broker.cash_usd < self._min_notional:
            return False

        # Giris butcesi, mevcut nakdin yuzdesi olarak hesaplanir;
        # toplam equity'ye gore boyutlandirma bot dongusunde yapilir.
        budget = broker.cash_usd * (self._max_pos_pct / 100.0)
        if budget < self._min_notional:
            return False
        return True

    # ------------------------------------------------------------------
    # Mevcut pozisyon icin cikis karari (SL / TP / Trailing)
    # ------------------------------------------------------------------
    def evaluate_position(self, position: Position, quote: Quote) -> RiskDecision:
        """Tek bir pozisyon + guncel fiyat icin risk karari uretir.

        ONEMLI: Tepi (peak_price) buraya YAZILMAZdan once guncellenir;
        trailing stop daima gorulen en yuksek fiyati referans alir.
        """
        price = quote.price_usd
        if price <= 0:
            return _NO_ACTION

        # --- Adim A: Zirve guncellemesi (tepe hicbir zaman dusmez) ---
        if price > position.peak_price:
            position.peak_price = price

        pnl_pct = position.unrealized_pnl_pct(price)
        multiple = position.multiple_of_entry(price)

        # --- Adim B: STOP-LOSS (-%15) => her kosulda tam kapanis ---
        if pnl_pct <= -self._sl_pct:
            return RiskDecision(
                action=RiskAction.STOP_LOSS_FULL,
                fraction=1.0,
                reason=(
                    f"Stop-Loss: girise gore %{pnl_pct:.2f} <= -%{self._sl_pct:.0f}"
                ),
                sell_fraction_for_record="STOP_LOSS",
            )

        # --- Adim C: TAKE-PROFIT (2x) => ilk kez ise yarisi sat ---
        if multiple >= self._tp_mult and not position.take_profit_done:
            return RiskDecision(
                action=RiskAction.TAKE_PROFIT_HALF,
                fraction=0.5,
                reason=(
                    f"Take-Profit: fiyat girisin {multiple:.2f}>= {self._tp_mult:.1f} kati"
                ),
                sell_fraction_for_record="TAKE_PROFIT",
            )

        # --- Adim D: TRAILING STOP => yalnizca TP uygulanmis pozisyonlarda ---
        if position.state == PositionState.TP_PARTIAL_DONE:
            drawdown = position.drawdown_from_peak_pct(price)
            # Trail asla entry stop-loss'unun gerisine dusmez: kar kilidi
            trailing_floor = position.peak_price * (1.0 - self._ts_pct / 100.0)
            if drawdown >= self._ts_pct and price <= trailing_floor:
                return RiskDecision(
                    action=RiskAction.TRAILING_STOP_FULL,
                    fraction=1.0,
                    reason=(
                        f"Trailing-Stop: tepe ${position.peak_price:.8f}'den "
                        f"%{drawdown:.2f} dusus >= %{self._ts_pct:.0f}"
                    ),
                    sell_fraction_for_record="TRAILING_STOP",
                )

        return _NO_ACTION

    # ------------------------------------------------------------------
    # Kararin portfoye uygulanmasi (broker ile koordinasyon)
    # ------------------------------------------------------------------
    def apply_decision(
        self, broker: PaperBroker, decision: RiskDecision, quote: Quote
    ) -> None:
        """RiskDecision'i gercek paper emirlerine cevirip yurutur.

        Side-effect'ler:
          - broker.sell(...) ile simule satim + muhasebe kaydi
          - TP sonrasi pozisyon durumu ve bayrak guncellemesi
          - tam kapanislarda cooldown saati baslatmasi
        """
        if decision.action == RiskAction.NONE:
            return

        key = quote.mint_address.lower()
        position = broker.positions.get(key)
        if position is None:
            # Yarism durumu: ayni dongude pozisyon zaten kapanmis olabilir
            return

        if decision.action == RiskAction.TAKE_PROFIT_HALF:
            trade = broker.sell(key, quote, fraction=0.5, reason=decision.sell_fraction_for_record)
            if trade is not None:
                # Satim gerceklesmisse ve pozisyon henuz yasiyorsa (toz
                # miktar kapanmis olabilir) durumu TP_PARTIAL_DONE yap.
                live = broker.positions.get(key)
                if live is not None:
                    live.take_profit_done = True
                    live.state = PositionState.TP_PARTIAL_DONE
                    logger.info(
                        "TP uygulandi: %s yarisi satildi, kalan %%50 trailing stop takipte",
                        live.symbol,
                    )
                else:
                    # Pozisyon tamamen kapandiysa artik cooldown baslar;
                    # yeniden 2x koasilu olmasini engellemek icin kayit tutulur.
                    self._last_exit_at[key] = time.time()
            return

        if decision.action in (
            RiskAction.STOP_LOSS_FULL,
            RiskAction.TRAILING_STOP_FULL,
        ):
            broker.sell(key, quote, fraction=1.0, reason=decision.sell_fraction_for_record)
            # Kapanan pozisyon icin cooldown baslat
            self._last_exit_at[key] = time.time()
            logger.info("%s | %s", decision.reason, decision.action.value)

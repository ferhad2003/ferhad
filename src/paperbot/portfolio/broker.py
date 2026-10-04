# -*- coding: utf-8 -*-
"""Paper Trading hesap defteri (portfoliyo cekirdegi).

Bu sinif, gercek cuzdan OLMADAN simule edilen nakit bakiyesini ve
acik token pozisyonlarini tutar. Tum para/math kararlari burada
toplanmistir; boylece risk motoru ve yurutmek katmani sadece
"ne zaman" sorusuna odaklanir, "ne kadar para" sorusuna degil.

Kalicilik: JSON dosyasina yazilarak bot yeniden baslatildiginda
portfoyunun kaybolmasi onlenir (crash-safe atomic write).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from paperbot.models import PositionState, Quote, Side, TokenInfo, TradeRecord
from paperbot.utils.logger import get_logger

logger = get_logger("portfolio")


@dataclass
class Position:
    """Tek bir token'daki acik pozisyonun tam durumu.

    Attributes:
        mint / symbol: Token kimligi.
        quantity: Eldeki token adedi (TP satimina gore azalir).
        entry_price: Ortalama giris fiyati (USD).
        opened_at: Pozisyonun acilis zamani.
        peak_price: Giris sonrasi gorulen EN YUKSEK fiyat - trailing
            stop bu tepeye gore hesaplanir.
        state: OPEN / TP_PARTIAL_DONE / CLOSED.
        invested_usd: Bu pozisyona toplam harcanan nakit (muhasebe).
        realized_pnl_usd: Kismen/tamamen satimlarda dolarlasan P&L.
        take_profit_done: 2x TP uygulandi mi?
    """

    mint: str
    symbol: str
    quantity: float
    entry_price: float
    opened_at: float = field(default_factory=time.time)
    peak_price: float = 0.0
    state: PositionState = PositionState.OPEN
    invested_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    take_profit_done: bool = False

    def __post_init__(self) -> None:
        # Ilk tepiyi en az giris fiyati yap; boylece %20 trailing
        # hemen tetiklenmez, once tepe asagi yonde genisler.
        if self.peak_price <= 0:
            self.peak_price = self.entry_price

    # --- Hesaplanan degerler (metotlar tek dogruluk kaynagidir) ---
    def market_value_usd(self, current_price: float) -> float:
        """Guncel fiyata gore pozisyonun dolar degeri."""
        return self.quantity * current_price

    def unrealized_pnl_pct(self, current_price: float) -> float:
        """Yuzde bazinda bas-bas P&L ((fiyat/giris)-1)*100."""
        if self.entry_price <= 0:
            return 0.0
        return (current_price / self.entry_price - 1.0) * 100.0

    def multiple_of_entry(self, current_price: float) -> float:
        """Fiyat girisin kac kati? (2.0 => 2x, TP tetik esigi)."""
        if self.entry_price <= 0:
            return 0.0
        return current_price / self.entry_price

    def drawdown_from_peak_pct(self, current_price: float) -> float:
        """Tepe fiyattan yuzde kaç dusus var? (trailing stop girdisi)."""
        if self.peak_price <= 0:
            return 0.0
        return (1.0 - current_price / self.peak_price) * 100.0


class PaperBroker:
    """Simule edilmis emir yürütücü + muhasebe defteri.

    Args:
        starting_balance_usd: Baslangic nakit (USD).
        slippage_pct: Her isleme uygulanan fiyat kotulmesi yuzdesi.
        gas_fee_usd: Sabit islem ucreti (Solana gas sabitleme payi).
        state_file: Portfoy durumunun kalici yazildigi JSON yolu.
    """

    def __init__(
        self,
        starting_balance_usd: float,
        slippage_pct: float,
        gas_fee_usd: float,
        state_file: Path,
    ) -> None:
        self.cash_usd = starting_balance_usd
        self.starting_balance_usd = starting_balance_usd
        self._slippage_pct = slippage_pct
        self._gas_fee_usd = gas_fee_usd
        self._state_file = Path(state_file)

        # mint -> Position : yalnizca ACIK pozisyonlar
        self.positions: dict[str, Position] = {}
        # Tum gecmis islemlerin sirali kaydi (CSV'ye de yazilir)
        self.trade_history: list[TradeRecord] = []
        self._next_paper_id = 1

        # Eger daha once kaydedilmis bir portfoy varsa geri yukle
        self._load_state()

    # ------------------------------------------------------------------
    # Fiyat modeli (slippage simulasyonu)
    # ------------------------------------------------------------------
    def effective_buy_price(self, mid_price: float) -> float:
        """Alimda odenecek efektif fiyat: referans fiyat * (1+slippage)."""
        return mid_price * (1.0 + self._slippage_pct / 100.0)

    def effective_sell_price(self, mid_price: float) -> float:
        """Satimda elde edilecek efektif fiyat: referans * (1-slippage)."""
        return mid_price * (1.0 - self._slippage_pct / 100.0)

    # ------------------------------------------------------------------
    # Emir yurutme (paper)
    # ------------------------------------------------------------------
    def buy(self, token: TokenInfo, quote: Quote, budget_usd: float) -> TradeRecord | None:
        """Belirtilen USD bütceyle simule alim emri yürütür.

        Kurallar:
          - Nakit yetersizse bütce mevcut nakde kirpilir.
          - Ciktida quantity = net nakit / efektif fiyat (fee dahil).
          - Ayni mint icin acik pozisyon varsa ORTALAMA GIRIS (DCA)
            uygulanmaz; strateji basitligi icin ek alim reddedilir.

        Returns:
            TradeRecord ya da islem gerceklesmediyse None.
        """
        if budget_usd <= 0:
            return None
        # Tekrar-giris korumasi: her token icin tek acik pozisyon
        if token.key in self.positions:
            logger.debug("%s icin zaten acik pozisyon var; ek alim atlandi", token.symbol)
            return None
        # Nakit kontrolu + otomatik kirpma
        if budget_usd > self.cash_usd:
            budget_usd = self.cash_usd
        if budget_usd <= self._gas_fee_usd:
            logger.warning("Yeterli nakit yok: %s alimi iptal", token.symbol)
            return None

        exec_price = self.effective_buy_price(quote.price_usd)
        # Fee'yi nakitten dusup kalan parayla alim yapilir (gercekci akis)
        spendable = budget_usd - self._gas_fee_usd
        quantity = spendable / exec_price
        cost_notional = quantity * exec_price
        total_outflow = cost_notional + self._gas_fee_usd

        self.cash_usd -= total_outflow

        position = Position(
            mint=token.mint_address,
            symbol=token.symbol,
            quantity=quantity,
            entry_price=exec_price,
            invested_usd=total_outflow,
        )
        self.positions[token.key] = position

        trade = TradeRecord(
            paper_id=self._next_paper_id,
            timestamp=time.time(),
            mint_address=token.mint_address,
            symbol=token.symbol,
            side=Side.BUY,
            quantity=quantity,
            price_usd=exec_price,
            notional_usd=cost_notional,
            fee_usd=self._gas_fee_usd,
            net_cash_delta_usd=-total_outflow,
            reason="ENTRY",
        )
        self._record_trade(trade)
        logger.info(
            "PAPER BUY  | %-8s | %.4f @ $%.8f | maliyet $%.2f (fee $%.2f) | nakit $%.2f",
            token.symbol, quantity, exec_price, total_outflow,
            self._gas_fee_usd, self.cash_usd,
        )
        return trade

    def sell(
        self,
        mint: str,
        quote: Quote,
        fraction: float,
        reason: str,
    ) -> TradeRecord | None:
        """Pozisyonun belirtilen kesrini simule sat.

        Args:
            fraction: 0.0-1.0 arasi satilacak oran (0.5 => yarisini sat).
            reason: TAKE_PROFIT / STOP_LOSS / TRAILING_STOP / MANUAL.

        Returns:
            TradeRecord ya da pozisyon/fiyat yoksa None.
        """
        key = mint.lower()
        position = self.positions.get(key)
        if position is None or position.quantity <= 0:
            return None
        fraction = max(0.0, min(1.0, fraction))
        if fraction == 0.0:
            return None

        sell_qty = position.quantity * fraction
        exec_price = self.effective_sell_price(quote.price_usd)
        gross_inflow = sell_qty * exec_price
        net_inflow = max(0.0, gross_inflow - self._gas_fee_usd)

        # Dolar gerceklesen P&L: satilan miktarin ort. maliyetiyle karsilastir
        unit_cost = position.invested_usd / position.quantity if position.quantity else 0.0
        realized = net_inflow - unit_cost * sell_qty

        position.quantity -= sell_qty
        position.invested_usd -= unit_cost * sell_qty
        position.realized_pnl_usd += realized
        self.cash_usd += net_inflow

        # Miktar sembolik seviyenin altina dustuyse pozisyonu kapat
        if position.quantity * exec_price < 0.01 or fraction >= 1.0:
            # Tam kapanis: kalan toz miktari da hesaba katmadan kapat
            position.quantity = 0.0
            position.state = PositionState.CLOSED
            del self.positions[key]

        trade = TradeRecord(
            paper_id=self._next_paper_id,
            timestamp=time.time(),
            mint_address=position.mint,
            symbol=position.symbol,
            side=Side.SELL,
            quantity=sell_qty,
            price_usd=exec_price,
            notional_usd=gross_inflow,
            fee_usd=self._gas_fee_usd,
            net_cash_delta_usd=net_inflow,
            reason=reason,
            realized_pnl_usd=realized,
        )
        self._record_trade(trade)
        logger.info(
            "PAPER SELL | %-8s | %.4f (%.0f%%) @ $%.8f | gelir $%.2f | P&L $%+.2f | sebep=%s",
            position.symbol, sell_qty, fraction * 100.0, exec_price,
            net_inflow, realized, reason,
        )
        return trade

    # ------------------------------------------------------------------
    # Portfoy goruntuleme
    # ------------------------------------------------------------------
    def mark_to_market(self, quotes: dict[str, Quote]) -> dict:
        """Acik pozisyonlari guncel fiyatlarla degerleyip ozet dondurur.

        Quotes sozlugunde fiyat OLMAYAN tokenlar degerlemeye dahil
        edilmez (muhabazeli davranis); toplam hatali hesaplanmasin diye
        eksik token loglanır.
        """
        holdings_value = 0.0
        rows = []
        for key, pos in self.positions.items():
            quote = quotes.get(key)
            if quote is None:
                logger.debug("Fiyat bulunamadi, degerleme atlandi: %s", pos.symbol)
                continue
            value = pos.quantity * quote.price_usd
            holdings_value += value
            rows.append(
                {
                    "symbol": pos.symbol,
                    "quantity": pos.quantity,
                    "entry_price": pos.entry_price,
                    "last_price": quote.price_usd,
                    "peak_price": pos.peak_price,
                    "pnl_pct": pos.unrealized_pnl_pct(quote.price_usd),
                    "state": pos.state.value,
                }
            )

        equity = self.cash_usd + holdings_value
        return {
            "cash_usd": self.cash_usd,
            "holdings_usd": holdings_value,
            "equity_usd": equity,
            "total_return_pct": (equity / self.starting_balance_usd - 1.0) * 100.0,
            "open_positions": len(self.positions),
            "positions": rows,
        }

    # ------------------------------------------------------------------
    # Ic yardimcilar ve kalicilik
    # ------------------------------------------------------------------
    def _record_trade(self, trade: TradeRecord) -> None:
        """Islemi hem bellege hem disariya (CSV callback'i ile) kaydeder."""
        self.trade_history.append(trade)
        self._next_paper_id += 1

    def to_dict(self) -> dict:
        """Portfoy durumunu JSON-lanabilir sozluge cevir."""
        return {
            "cash_usd": self.cash_usd,
            "starting_balance_usd": self.starting_balance_usd,
            "next_paper_id": self._next_paper_id,
            "positions": {
                key: {
                    "mint": p.mint,
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "opened_at": p.opened_at,
                    "peak_price": p.peak_price,
                    "state": p.state.value,
                    "invested_usd": p.invested_usd,
                    "realized_pnl_usd": p.realized_pnl_usd,
                    "take_profit_done": p.take_profit_done,
                }
                for key, p in self.positions.items()
            },
        }

    def save_state(self) -> None:
        """Atomik yazim: once gecici dosya, sonra rename (yarim yazim riski yok)."""
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2)
        fd, tmp_path = tempfile.mkstemp(dir=self._state_file.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_path, self._state_file)
        except OSError as exc:
            logger.error("Portfoy durumu kaydedilemedi: %s", exc)
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _load_state(self) -> None:
        """Kayitli JSON varsa yukle; bozuksa yedekleyip temiz basla."""
        if not self._state_file.exists():
            return
        try:
            with open(self._state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            self.cash_usd = float(data["cash_usd"])
            self.starting_balance_usd = float(
                data.get("starting_balance_usd", self.starting_balance_usd)
            )
            self._next_paper_id = int(data.get("next_paper_id", 1))
            for key, raw in (data.get("positions") or {}).items():
                self.positions[key] = Position(
                    mint=raw["mint"],
                    symbol=raw["symbol"],
                    quantity=float(raw["quantity"]),
                    entry_price=float(raw["entry_price"]),
                    opened_at=float(raw.get("opened_at", time.time())),
                    peak_price=float(raw.get("peak_price", 0.0)),
                    state=PositionState(raw.get("state", "OPEN")),
                    invested_usd=float(raw.get("invested_usd", 0.0)),
                    realized_pnl_usd=float(raw.get("realized_pnl_usd", 0.0)),
                    take_profit_done=bool(raw.get("take_profit_done", False)),
                )
            logger.info(
                "Kalici portfoy yuklendi: nakit $%.2f, acik pozisyon %d",
                self.cash_usd, len(self.positions),
            )
        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            # Bozuk state dosyasi botu dusurmesin: yedekle ve sifirdan basla
            backup = self._state_file.with_suffix(".corrupt.bak")
            try:
                os.replace(self._state_file, backup)
            except OSError:
                pass
            logger.warning(
                "Portfoy state okunamadi (%s); yedeklendi ve temiz baslandi", exc
            )

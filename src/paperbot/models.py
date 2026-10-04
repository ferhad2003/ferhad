# -*- coding: utf-8 -*-
"""Botun ortak veri modelleri (dataclass) ve sabitleri.

Bu modul, sistemin hicbir yerinde "ham sozluk" dolasmasini engeller;
tum piyasa verisi TokenInfo / Quote nesneleriyle, tum pozisyon
durumlari PositionState enum'i ile temsil edilir.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    """Islem yonu: alim ya da satim."""

    BUY = "BUY"
    SELL = "SELL"


class PositionState(str, Enum):
    """Bir pozisyonun yasam dongusu durumlari.

    Durum gecisleri (risk motoru tarafindan yonetilir):
      OPEN --(fiyat 2x goruldu)--> TP_PARTIAL_DONE --(tepe-%20 dusus)--> CLOSED
      OPEN --(%15 dusus)--> CLOSED (stop-loss)
      TP_PARTIAL_DONE --(%15 dusus, entry'ye gore)--> CLOSED (stop-loss)
    """

    OPEN = "OPEN"                      # Tamamı açık, henüz take-profit olmadı
    TP_PARTIAL_DONE = "TP_PARTIAL_DONE"  # 2x görüldü, yarısı satıldı, trailing aktif
    CLOSED = "CLOSED"                  # Pozisyon tamamen kapandı


@dataclass(frozen=True)
class TokenInfo:
    """Bir memecoin'in statik kimlik ve piyasa bilgisi.

    Attributes:
        mint_address: Tokenin Solana mint adresi (benzersiz kimlik).
        symbol: Kisa sembol (ornek: BONK).
        name: Tokenin tam adi.
        market_cap_usd: Anlik piyasa degeri (USD). Bu alan $20K
            filtrelemesinin temel girdisidir.
        is_meme: Jupiter meta verisindeki kategori etiketi; sadece
            memecoin adaylarini tarariz.
    """

    mint_address: str
    symbol: str
    name: str
    market_cap_usd: float
    is_meme: bool = True

    @property
    def key(self) -> str:
        """Pozisyon/esleme sozluklerinde kullanilan benzersiz anahtar."""
        return self.mint_address.lower()


@dataclass(frozen=True)
class Quote:
    """Anlik fiyat teklifi (real-time price snapshot).

    Attributes:
        mint_address: Token mint adresi.
        price_usd: USD cinsinden son fiyat.
        timestamp: Fiyatin okundugu Unix zamani (saniye). Bayatlik
            kontrolu icin kullanilir.
        source: Veri kaynagi etiketi ("jupiter", "rpc", "mock").
    """

    mint_address: str
    price_usd: float
    timestamp: float = field(default_factory=time.time)
    source: str = "unknown"

    def age_seconds(self, now: float | None = None) -> float:
        """Teklifin yasini saniye olarak dondurur."""
        current = now if now is not None else time.time()
        return max(0.0, current - self.timestamp)

    def is_fresh(self, max_age_seconds: int, now: float | None = None) -> bool:
        """Teklif henuz guncel mi? (bayat fiyata islem yapmayiz)."""
        return self.age_seconds(now) <= max_age_seconds


@dataclass
class TradeRecord:
    """Kaydedilen (simule edilmis) bir islemin muhasebe kaydi.

    Paper trading oldugu icin burada transaction signature yoktur;
    yerine bot ici artan bir "paper_id" kullanimindadir.
    """

    paper_id: int
    timestamp: float
    mint_address: str
    symbol: str
    side: Side
    quantity: float          # Token adedi
    price_usd: float         # Uygulanan efektif fiyat (slippage dahil)
    notional_usd: float      # Quantity * price (fee oncesi tutar)
    fee_usd: float           # Simule gas + slippage maliyeti
    net_cash_delta_usd: float  # Nakit akisina etkisi (- alim / + satim)
    reason: str              # Islem sebebi: ENTRY / STOP_LOSS / TAKE_PROFIT / TRAILING_STOP
    realized_pnl_usd: float = 0.0  # Yalnizca satim islemlerinde dolar gerceklesen kar/zarar

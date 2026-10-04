# -*- coding: utf-8 -*-
"""Islem defterinin CSV olarak kalici kaydi.

Her yurutulen paper emir, denetlenebilirlik ve performans raporu
icin data/trades.csv dosyasina eklenir. Dosya mevcut degilse basligi
otomatik olusturulur; yazma hatalari botu asla durdurmaz (yalnizca
uyari loglanir) - cunku muhasebe bellegi zaten PaperBroker'da tutulur.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path

from paperbot.models import TradeRecord
from paperbot.utils.logger import get_logger

logger = get_logger("ledger")

# CSV sutun siralamasi - degisirse eski dosyalar icin migrasyon gerekir
_CSV_HEADER = [
    "paper_id",
    "timestamp_iso",
    "mint_address",
    "symbol",
    "side",
    "quantity",
    "price_usd",
    "notional_usd",
    "fee_usd",
    "net_cash_delta_usd",
    "reason",
    "realized_pnl_usd",
]


class TradeLedger:
    """TradeRecord akisini CSV dosyasina yanlis-bilgiye-direncli yazan defter."""

    def __init__(self, csv_path: Path) -> None:
        self._path = Path(csv_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Baslik yoksa dosyayi olustur ve header yaz
        if not self._path.exists() or self._path.stat().st_size == 0:
            try:
                with open(self._path, "w", newline="", encoding="utf-8") as handle:
                    csv.writer(handle).writerow(_CSV_HEADER)
            except OSError as exc:
                logger.warning("Defter dosyasi olusturulamadi: %s", exc)

    def append(self, trade: TradeRecord) -> None:
        """Tek bir islemi dosyanin sonuna satir olarak ekler."""
        row = [
            trade.paper_id,
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(trade.timestamp)),
            trade.mint_address,
            trade.symbol,
            trade.side.value,
            f"{trade.quantity:.10f}",
            f"{trade.price_usd:.12f}",
            f"{trade.notional_usd:.6f}",
            f"{trade.fee_usd:.6f}",
            f"{trade.net_cash_delta_usd:.6f}",
            trade.reason,
            f"{trade.realized_pnl_usd:.6f}",
        ]
        try:
            with open(self._path, "a", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(row)
        except OSError as exc:
            # Deftere yazamamak islem mantigini bozmaz; yalnizca uyariliriz
            logger.error("Islem defterine yazilamadi: %s", exc)

    def read_all(self) -> list[dict]:
        """Gecmis islemleri okur (raporlama/analiz icin)."""
        if not self._path.exists():
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                return list(csv.DictReader(handle))
        except OSError as exc:
            logger.error("Islem defteri okunamadi: %s", exc)
            return []

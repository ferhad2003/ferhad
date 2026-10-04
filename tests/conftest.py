# -*- coding: utf-8 -*-
"""Test sabitleri ve yardimcilari (fixtures).

Buradaki mint adresleri gercek token'lara benzer uzunlukta, ancak
test amacli URETILMIS sahte adreslerdir. Fiyatlar da deterministik
test senaryolari icin sabittir; ag baglantisi GEREKTIRMEZ.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Paket yollarini test calisma klasorunden bagimsiz hale getir
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))          # config/ paketi icin
sys.path.insert(0, str(_ROOT / "src"))  # paperbot paketi icin

# --- Sahte ama gecerli gorunumlu Solana mint adresleri ---
MEME_A = "TESTTOKENAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"   # $50K market cap
MEME_B = "TESTTOKENBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"   # $12K -> ELENMELI
MEME_C = "TESTTOKENCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"    # $400K -> BUYUK CAP

STARTING_BALANCE = 10_000.0


def make_token(mint: str, symbol: str, market_cap: float):
    """TokenInfo ureten kisayol (import dolasimini sadelestirir)."""
    from paperbot.models import TokenInfo

    return TokenInfo(
        mint_address=mint,
        symbol=symbol,
        name=f"{symbol} Test Coin",
        market_cap_usd=market_cap,
        is_meme=True,
    )


def make_quote(mint: str, price: float):
    """Quote ureten kisayol."""
    from paperbot.models import Quote

    return Quote(mint_address=mint, price_usd=price, source="mock")

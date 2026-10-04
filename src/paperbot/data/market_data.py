# -*- coding: utf-8 -*-
"""Piyasa verisi katmani: aday tarama + Market Cap filtreleme.

Akisin tasarimi:
  - refresh_candidate_pool(): Jupiter'dan memecoin adaylarini ceker,
    $20K alti market cap'e sahip tokenlari PAS GECER (gorev adimi 2).
  - get_price(mint): Once bellekteki guncel teklifi dondurur; bayatsa
    Jupiter Price API'den yeniler (real-time takibin cekirdegi).
  - Fiyat okuyamayan token icin HICBIR islem uretilmez; sistem "veri
    yoksa islem yok" ilkesiyle korunakli kalir.
"""

from __future__ import annotations

import time

from paperbot.data.jupiter_client import JupiterClient
from paperbot.models import Quote, TokenInfo
from paperbot.utils.logger import get_logger

logger = get_logger("market")


class MarketDataService:
    """Token katalogu + canli fiyat sozlugunu tek noktadan yoneten servis.

    Args:
        jupiter: Enjekte edilmis JupiterClient (testlerde sahte istemci
            verilebilir - dependency injection).
        min_market_cap_usd: Alt sinir; bunun altindaki coin'ler elenir.
        quote_max_age_seconds: Bir fiyat kac saniye guncel sayilir.
        candidate_pool_size: Alim adayi olarak izlenecek maks. token.
    """

    def __init__(
        self,
        jupiter: JupiterClient,
        min_market_cap_usd: float,
        quote_max_age_seconds: int,
        candidate_pool_size: int = 50,
    ) -> None:
        self._jupiter = jupiter
        self._min_market_cap = min_market_cap_usd
        self._max_age = quote_max_age_seconds
        self._pool_size = candidate_pool_size

        # mint -> TokenInfo : su an izlenen gecerli aday havuzu
        self._candidates: dict[str, TokenInfo] = {}
        # mint -> Quote : en son okunan fiyatlar
        self._latest_quotes: dict[str, Quote] = {}
        # Sayac: kaç aday $20K filtresine takildi (raporlama icin)
        self.filtered_out_count = 0

    # ------------------------------------------------------------------
    # Aday havuzunun yenilenmesi ve Market Cap filtrelemesi
    # ------------------------------------------------------------------
    def refresh_candidate_pool(self) -> list[TokenInfo]:
        """Jupiter'dan memecoin listesini cekip filtreleri uygular.

        Returns:
            Filtreden gecmis, market cap'e gore dusukten yuksek sirali
            aday listesi. (Kucuk cap once gelir: strateji kucuk-cap
            avcidisidir.)
        """
        raw_tokens = self._jupiter.top_tokens(
            category="memecoin", limit=self._pool_size
        )

        accepted: list[TokenInfo] = []
        for token in raw_tokens:
            # --- Adim 2 geregi: $20,000 alti market cap AVTOMATIK ELenir ---
            if token.market_cap_usd < self._min_market_cap:
                self.filtered_out_count += 1
                logger.debug(
                    "Pas gecildi (MarketCap $%.0f < $%.0f): %s [%s]",
                    token.market_cap_usd,
                    self._min_market_cap,
                    token.symbol,
                    token.mint_address[:8],
                )
                continue
            accepted.append(token)

        # Mevcut aday havuzunu atomik sekilde degistir
        self._candidates = {t.key: t for t in accepted}

        # Havuzdan cikan tokenlarin eski fiyatlarini da temizle (bellek hijyeni)
        stale_mints = set(self._latest_quotes) - set(self._candidates)
        for mint in stale_mints:
            self._latest_quotes.pop(mint, None)

        logger.info(
            "Aday havuzu guncellendi: %d kabul / %d elenen (toplam %d ham)",
            len(accepted),
            self.filtered_out_count,
            len(raw_tokens),
        )
        return sorted(accepted, key=lambda t: t.market_cap_usd)

    @property
    def candidates(self) -> dict[str, TokenInfo]:
        """Su an izlenen aday sozlugu (salt-okur gorunum)."""
        return dict(self._candidates)

    # ------------------------------------------------------------------
    # Real-time fiyat takibi
    # ------------------------------------------------------------------
    def get_price(self, mint: str, now: float | None = None) -> Quote | None:
        """Tek bir token icin guncel fiyat teklifi dondurur.

        Bayatlik kontrolu: kayittaki teklif ``quote_max_age_seconds``
        icindeyse ag'a gidilmez (rate-limit dostu); aksi halde
        Jupiter'dan yenisi cekilir.
        """
        current_time = now if now is not None else time.time()
        cached = self._latest_quotes.get(mint)
        if cached is not None and cached.is_fresh(self._max_age, current_time):
            return cached

        fresh = self._jupiter.fetch_prices([mint])
        quote = fresh.get(mint)
        if quote is not None:
            self._latest_quotes[mint] = quote
            return quote

        # Ag hatasi: varsa ESKI teklifi dondurmayi REDDEDIYORUZ,
        # cunku bayat fiyata risk karari verilmemeli.
        return None

    def update_prices(self, mints: list[str]) -> dict[str, Quote]:
        """Toplu fiyat guncellemesi (tarama dongusunun hizli yolu).

        Tek HTTP istegiyle cok sayida mint sorgulanir; basarili
        sonuclar dahili fiyatluk defterine yazilir.
        """
        if not mints:
            return {}
        quotes = self._jupiter.fetch_prices(list(mints))
        self._latest_quotes.update(quotes)
        return quotes

    def cache_quote(self, quote: Quote) -> None:
        """Harici kaynaktan (ornek: mock/test) fiyat enjekte eder."""
        self._latest_quotes[quote.mint_address] = quote

    def register_candidate(self, token: TokenInfo) -> None:
        """Manuel aday kaydi (testler ve gelecekteki sinyal kaynaklari icin)."""
        if token.market_cap_usd >= self._min_market_cap:
            self._candidates[token.key] = token

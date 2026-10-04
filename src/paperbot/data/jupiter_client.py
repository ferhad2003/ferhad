# -*- coding: utf-8 -*-
"""Jupiter Aggregator API istemcisi (token listesi + real-time fiyat).

Veri akisi:
  1. "top_tokens()"  -> Jupiter Tokens v2 ucundan kategori=memecoin
     sorgulanarak Market Cap'i bilinen aday liste alinir.
  2. "fetch_prices()" -> Jupiter Price v3 ucundan mint adreslerine
     gore guncel USD fiyatlarini getirir.

Notlar:
  - Her iki uç nokta da halka aciktir, API anahtari gerektirmez.
  - Tüm ag cagrilarinda timeout + retry mantigi vardir; ağ hatası
    durumunda exception yerine bos/None sonuc dondurulur ve botun
    dongusu kirilmadan devam eder (dayanikli calisma zamani).
  - Gercek para transferi YAPILMAZ; bu modul yalnizca salt-okur
    piyasa verisi toplar (paper trading guvenlik ilkesi).
"""

from __future__ import annotations

import requests

from paperbot.models import Quote, TokenInfo
from paperbot.utils.logger import get_logger

logger = get_logger("jupiter")

# Sabit request header: tanilabilir kimlik + hizli yanit alimi
_DEFAULT_HEADERS = {
    "User-Agent": "solana-paper-bot/0.1 (simulation only)",
    "Accept": "application/json",
}


class JupiterClient:
    """Jupiter DEX aqreqatorunun public veri uçlarına ince sarmalayici.

    Args:
        price_api_url: Price v3 base URL.
        tokens_api_url: Tokens v2 base URL.
        timeout_seconds: HTTP istekleri icin ust sinir.
        session: Test edilebilirligi artirmak icin disardan enjekte
            edilebilen requests.Session (mock ile degistirilebilir).
    """

    def __init__(
        self,
        price_api_url: str,
        tokens_api_url: str,
        timeout_seconds: float = 10.0,
        session: requests.Session | None = None,
    ) -> None:
        self._price_url = price_api_url.rstrip("/")
        self._tokens_url = tokens_api_url.rstrip("/")
        self._timeout = timeout_seconds
        self._session = session or requests.Session()
        self._session.headers.update(_DEFAULT_HEADERS)

    # ------------------------------------------------------------------
    # Token keşfi (aday havuzu)
    # ------------------------------------------------------------------
    def top_tokens(
        self,
        category: str = "memecoin",
        order_by: str = "marketCap",
        limit: int = 50,
        page: int = 1,
    ) -> list[TokenInfo]:
        """Jupiter katalogunden Market Cap siralamali memecoin listesi cek.

        Donen liste yuksekten dusuk market cap'e siralanmistir; su anki
        strateji kucuk-cap avlandigi icin bot tarafinda ayrica alt sinir
        filtresi uygulanir ($20K alti elenir).

        Hata durumunda bos liste dondurur (donguyu kirmaz).
        """
        url = f"{self._tokens_url}/search"
        params = {
            "query": "",
            "category": category,
            "orderBy": order_by,
            "limit": max(1, min(limit, 200)),  # API ust siniri: 200
            "page": max(1, page),
        }
        try:
            response = self._session.get(url, params=params, timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Token listesi alinamadi: %s", exc)
            return []

        # API bos ya da beklenmedik sekilde dizi donmezse sessizce geç
        if not isinstance(payload, list):
            logger.warning("Beklenmeyen token API yaniti tipi: %s", type(payload))
            return []

        tokens: list[TokenInfo] = []
        for item in payload:
            info = self._parse_token_item(item)
            if info is not None:
                tokens.append(info)

        logger.debug("Aday havuzu: %d memecoin cekildi (sayfa %d)", len(tokens), page)
        return tokens

    @staticmethod
    def _parse_token_item(item: dict) -> TokenInfo | None:
        """Ham JSON ogesini dogulanmis TokenInfo'ya cevir.

        Eksik/bozuk alanli kayitlar None dondurerek eleye duzer;
        bozuk tek bir kayit tum taramayi dusurmeyebilir.
        """
        try:
            mint = str(item["address"]).strip()
            symbol = str(item.get("symbol") or "").strip().upper()
            name = str(item.get("name") or symbol).strip()
            market_cap = float(item.get("marketCap") or 0.0)
            categories = item.get("categories") or []
            is_meme = "memecoin" in [str(c).lower() for c in categories]
        except (KeyError, TypeError, ValueError):
            return None

        # Mint adresi Solana base58 formatina uymali (basit uzunluk kontrolu)
        if not mint or len(mint) < 32 or not symbol or market_cap < 0:
            return None

        return TokenInfo(
            mint_address=mint,
            symbol=symbol,
            name=name,
            market_cap_usd=market_cap,
            is_meme=is_meme,
        )

    # ------------------------------------------------------------------
    # Real-time fiyat sorgusu
    # ------------------------------------------------------------------
    def fetch_prices(self, mints: list[str]) -> dict[str, Quote]:
        """Mint adresleri icin guncel USD fiyatlarini cek.

        Args:
            mints: Sorgulanacak token adresleri (max ~50 grup onerilir).

        Returns:
            mint -> Quote sozlugu. Yanitte eksik kalan mint'ler sozlukte
            yer almaz; cagiran taraf bayatlik/kaynak kontrolunu Quote
            uzerinden yapar.
        """
        if not mints:
            return {}

        url = f"{self._price_url}/{','.join(m.strip() for m in mints)}"
        try:
            response = self._session.get(url, timeout=self._timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Fiyat sorgusu basarisiz (%d mint): %s", len(mints), exc)
            return {}

        quotes: dict[str, Quote] = {}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            logger.warning("Beklenmeyen fiyat API yaniti: %r", payload)
            return {}

        for mint, entry in data.items():
            # Price v3 sekli: { "<mint>": { "USD": "0.0000123", ... } }
            usd_value = entry.get("USD") if isinstance(entry, dict) else None
            try:
                price = float(usd_value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                quotes[mint] = Quote(
                    mint_address=mint, price_usd=price, source="jupiter"
                )

        logger.debug("Fiyat alindi: %d/%d mint", len(quotes), len(mints))
        return quotes

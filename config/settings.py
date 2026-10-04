# -*- coding: utf-8 -*-
"""
Yapilandirma modulunun yuklenmesi.

Bu dosya, ".env" uzantili ortam dosyasindan degerleri okur ve
tüm bot bilesenlerinin kullandigi tipte guvenli bir "Config"
nesnesi uretir. Eksik degerler icin makul varsayilanlar kullanilir.

Guvenlik notu: Bu yapida HICBIR gercek cuzdan anahtari tutulmaz;
sistem tamamen paper (simulasyon) modunda calisir.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Proje kok dizini: bu dosya <kok>/config/ altinda oldugu icin parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv_if_available() -> None:
    """Ortamda python-dotenv kuruluysa .env dosyasini yukle.

    Kurulu degilse sessizce gecilir; os.environ uzerinden de
    ayni degerler verilebilir (ornek: Docker / CI ortamlari).
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        return
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)


def _get_float(name: str, default: float) -> float:
    """Ortam degiskenini float olarak oku; hatali ise varsayilana don."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Gecersiz float deger: {name}={raw!r}")


def _get_int(name: str, default: int) -> int:
    """Ortam degiskenini int olarak oku; hatali ise varsayilana don."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Gecersiz int deger: {name}={raw!r}")


def _get_str(name: str, default: str) -> str:
    """Ortam degiskenini string olarak oku (bas-boşluk temizlenir)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


@dataclass(frozen=True)
class Config:
    """Botun tum yapilandirma parametrelerini tasiyan dokunulmaz veri sinifi.

    Alan gruplari:
      - Simulasyon hesabi (paper balance)
      - Jupiter / Solana RPC baglanti adresleri
      - Tarama davranisi
      - Risk yonetimi esikleri
      - Islem maliyeti modeli
    """

    # --- Simulasyon hesabi ---
    starting_balance_usd: float = field(default=10_000.0)

    # --- Veri kaynagi URL'leri ---
    jupiter_price_api_url: str = field(default="https://api.jup.ag/price/v3")
    jupiter_tokens_api_url: str = field(default="https://api.jup.ag/tokens/v2")
    jupiter_swap_api_url: str = field(default="https://api.jup.ag/swap/v1")
    solana_rpc_url: str = field(default="https://api.mainnet-beta.solana.com")

    # --- Tarama parametreleri ---
    scan_interval_seconds: int = field(default=30)
    quote_max_age_seconds: int = field(default=90)
    candidate_pool_size: int = field(default=50)

    # --- Risk yonetimi ---
    min_market_cap_usd: float = field(default=20_000.0)
    max_position_pct: float = field(default=5.0)
    stop_loss_pct: float = field(default=15.0)
    take_profit_multiplier: float = field(default=2.0)
    trailing_stop_pct: float = field(default=20.0)

    # --- Islem maliyeti modeli (simulasyon) ---
    simulated_slippage_pct: float = field(default=0.5)
    simulated_gas_fee_usd: float = field(default=0.01)

    # --- Loglama ---
    log_level: str = field(default="INFO")

    # --- Kalicilik dosyalari ---
    state_file: Path = field(default=PROJECT_ROOT / "data" / "portfolio_state.json")
    trade_log_file: Path = field(default=PROJECT_ROOT / "data" / "trades.csv")


def load_config() -> Config:
    """.env dosyasindan Config nesnesi uret.

    Degerler dogrulanir; mantiksal celiski varsa (ornek negatif
    market cap esigi) acik hata firlatilarak sessiz bozulma onlenir.
    """
    _load_dotenv_if_available()

    cfg = Config(
        starting_balance_usd=_get_float("PAPER_STARTING_BALANCE", 10_000.0),
        jupiter_price_api_url=_get_str(
            "JUPITER_PRICE_API_URL", "https://api.jup.ag/price/v3"
        ),
        jupiter_tokens_api_url=_get_str(
            "JUPITER_TOKENS_API_URL", "https://api.jup.ag/tokens/v2"
        ),
        jupiter_swap_api_url=_get_str(
            "JUPITER_SWAP_API_URL", "https://api.jup.ag/swap/v1"
        ),
        solana_rpc_url=_get_str(
            "SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"
        ),
        scan_interval_seconds=_get_int("SCAN_INTERVAL_SECONDS", 30),
        quote_max_age_seconds=_get_int("QUOTE_MAX_AGE_SECONDS", 90),
        candidate_pool_size=_get_int("CANDIDATE_POOL_SIZE", 50),
        min_market_cap_usd=_get_float("MIN_MARKET_CAP_USD", 20_000.0),
        max_position_pct=_get_float("MAX_POSITION_PCT", 5.0),
        stop_loss_pct=_get_float("STOP_LOSS_PCT", 15.0),
        take_profit_multiplier=_get_float("TAKE_PROFIT_MULTIPLIER", 2.0),
        trailing_stop_pct=_get_float("TRAILING_STOP_PCT", 20.0),
        simulated_slippage_pct=_get_float("SIMULATED_SLIPPAGE_PCT", 0.5),
        simulated_gas_fee_usd=_get_float("SIMULATED_GAS_FEE_USD", 0.01),
        log_level=_get_str("LOG_LEVEL", "INFO").upper(),
    )

    # --- Temel dogrulama kontrolleri ---
    if cfg.starting_balance_usd <= 0:
        raise ValueError("PAPER_STARTING_BALANCE sifirdan buyuk olmali")
    if cfg.min_market_cap_usd < 0:
        raise ValueError("MIN_MARKET_CAP_USD negatif olamaz")
    if not 0 < cfg.max_position_pct <= 100:
        raise ValueError("MAX_POSITION_PCT (0, 100] araliginda olmali")
    if cfg.stop_loss_pct <= 0:
        raise ValueError("STOP_LOSS_PCT pozitif olmali")
    if cfg.take_profit_multiplier <= 1:
        raise ValueError("TAKE_PROFIT_MULTIPLIER 1.0'dan buyuk olmali")
    if cfg.trailing_stop_pct <= 0 or cfg.trailing_stop_pct >= 100:
        raise ValueError("TRAILING_STOP_PCT (0, 100) araliginda olmali")
    if cfg.scan_interval_seconds < 1:
        raise ValueError("SCAN_INTERVAL_SECONDS en az 1 olmali")

    return cfg

# -*- coding: utf-8 -*-
"""Botun ana orkestrasyon dongusu (main loop).

Her turda sirayla:
  1. Acik pozisyonlarin fiyatlarini guncelle (real-time takibi).
  2. Risk motoruyla her pozisyon icin SL / TP / Trailing karari uret,
     kararlari paper emirlere cevir ve CSV defterine yaz.
  3. Aday havuzdan yeni firsat tara ($20K alti coin'ler zaten elenmistir)
     ve uygun olani al.
  4. Portfoy durumunu JSON'a kalici hale getir + ozet logla.
  5. Periyodik olarak aday havuzunu yenile; scan_interval kadar bekle.

Dayaniklilik ilkeleri (iteratif testlerde dogrulanmistir):
  - Tek bir tokenin verisi eksikse tarama devam eder.
  - Ag hatasi donguyu asla kirmez; yalnizca WARNING dusur.
  - Beklenmeyen istisnalar loglanip döngü korunur; SIGINT temiz kapanir.
"""

from __future__ import annotations

import signal
import time

from config.settings import Config
from paperbot.data.jupiter_client import JupiterClient
from paperbot.data.market_data import MarketDataService
from paperbot.execution.trade_ledger import TradeLedger
from paperbot.models import Quote, Side
from paperbot.portfolio.broker import PaperBroker
from paperbot.risk.risk_manager import RiskAction, RiskManager
from paperbot.utils.logger import get_logger, setup_logging

logger = get_logger("engine")


class PaperTradingEngine:
    """Bilesenleri birbirine baglayan calisma zamani motoru.

    Tum bagimliliklar constructor'da enjekte edilir; boylece testlerde
    gercek API yerine sahte (mock) nesneler verilebilir.
    """

    # Kac turda bir aday havazi yenilensin (havuz degisimi pahali olabilir)
    POOL_REFRESH_EVERY_N_CYCLES = 10

    def __init__(
        self,
        config: Config,
        market: MarketDataService,
        broker: PaperBroker,
        risk: RiskManager,
        ledger: TradeLedger,
    ) -> None:
        self._cfg = config
        self._market = market
        self._broker = broker
        self._risk = risk
        self._ledger = ledger
        self._running = False
        self._cycle_count = 0

    # ------------------------------------------------------------------
    # Yardimci: islemleri hem bellek hem CSV tarafina kaydet
    # ------------------------------------------------------------------
    def _persist_trades(self, trades: list) -> None:
        for trade in trades:
            if trade is not None:
                self._ledger.append(trade)

    # ------------------------------------------------------------------
    # Adim 1+2: Pozisyonlari izle ve risk kararlarini uygul
    # ------------------------------------------------------------------
    def manage_open_positions(self) -> list:
        """Acik pozisyonlar icin fiyat cekip SL/TP/Trailing islemlerini yapar."""
        executed: list = []
        if not self._broker.positions:
            return executed

        mints = [pos.mint for pos in self._broker.positions.values()]
        raw_quotes = self._market.update_prices(mints)
        # Normalizasyon: pozisyon anahtari kucuk harf sozlugudur; API
        # yanitinin anahtar buyuk/kucuk harf duyarli olma riskini siler.
        quotes: dict[str, Quote] = {k.lower(): q for k, q in raw_quotes.items()}

        # Sozluk uzerinde gezinirken silme olmamasi icin liste kopyala
        for key, position in list(self._broker.positions.items()):
            quote: Quote | None = quotes.get(key)
            if quote is None:
                # Fiyat yoksa KARAR YOK - eski fiyattan stop atmak yanlis olur
                logger.debug("%s fiyati okunamadi, bu turda atlandi", position.symbol)
                continue

            decision = self._risk.evaluate_position(position, quote)
            if decision.action == RiskAction.NONE:
                continue

            before_count = len(self._broker.trade_history)
            self._risk.apply_decision(self._broker, decision, quote)
            new_trades = self._broker.trade_history[before_count:]
            self._persist_trades(new_trades)
            executed.extend(new_trades)
        return executed

    # ------------------------------------------------------------------
    # Adim 3: Yeni firsat alimi
    # ------------------------------------------------------------------
    def scan_for_entries(self) -> list:
        """Aday havuzundan uygun tokenlara pozisyon acabilir.

        Oncelik sirasi: once RISK (should_enter), sonra ODEME (buy).
        Giris boyutu toplam equity'nin MAX_POSITION_PCT'sidir.
        """
        executed: list = []
        candidates = self._market.candidates
        if not candidates:
            return executed

        # Fiyatlari tek istekte tazele (gelen anahtarlar mint'in kucuk hali)
        raw_quotes = self._market.update_prices(list(candidates.keys()))
        quotes: dict[str, Quote] = {k.lower(): q for k, q in raw_quotes.items()}

        # Ozsermaye tahmini: nakit + degerlenebilen holdings
        summary = self._broker.mark_to_market(quotes)
        equity = summary["equity_usd"]

        # Kucuk market cap'ten basla: strateji kucuk-cap avcidisi.
        ordered = sorted(
            candidates.values(), key=lambda t: t.market_cap_usd
        )
        for token in ordered:
            if token.key in self._broker.positions:
                continue  # zaten pozisyon var
            quote = quotes.get(token.key)
            if quote is None:
                continue

            if not self._risk.should_enter(token, quote, self._broker):
                continue

            budget = self._risk.position_size_usd(equity, self._broker)
            before_count = len(self._broker.trade_history)
            trade = self._broker.buy(token, quote, budget)
            new_trades = self._broker.trade_history[before_count:]
            self._persist_trades(new_trades)
            executed.extend(new_trades)
            # Nakit azaldigi icin equity'yi guncelle (ardisik girisler dengeli olsun)
            equity = self._broker.cash_usd + sum(
                p.quantity * q.price_usd
                for k, p in self._broker.positions.items()
                if (q := quotes.get(k)) is not None
            )
        return executed

    # ------------------------------------------------------------------
    # Tek tur: yukadaki adimlarin sira ile yurutulmesi
    # ------------------------------------------------------------------
    def run_cycle(self) -> dict:
        """Bir tarama turunu tamamlar ve tur ozetini dondurur."""
        self._cycle_count += 1
        started = time.time()

        exit_trades = self.manage_open_positions()
        entry_trades = self.scan_for_entries()

        # Kalicilik: her turun sonunda state yaz (olaganustu crash korumasi)
        self._broker.save_state()

        quotes_now = {}
        if self._broker.positions:
            raw_now = self._market.update_prices(
                [p.mint for p in self._broker.positions.values()]
            )
            quotes_now = {k.lower(): q for k, q in raw_now.items()}
        summary = self._broker.mark_to_market(quotes_now)

        logger.info(
            "TUR #%d tamamlandi | hisseler: %d | nakit $%.2f | equity $%.2f (%+.2f%%) | %.1f sn",
            self._cycle_count,
            summary["open_positions"],
            summary["cash_usd"],
            summary["equity_usd"],
            summary["total_return_pct"],
            time.time() - started,
        )
        return {
            "cycle": self._cycle_count,
            "exits": exit_trades,
            "entries": entry_trades,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # Calisma zamani dongusu
    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        """SIGINT/SIGTERM'e kadar surekli turlar; temiz kapanis garantili."""
        self._running = True

        def _stop_handler(signum, frame):  # sinyal -> durus bayragi
            logger.info("Durus sinyali alindi (%s), dongu guvenli kapatiliyor...", signum)
            self._running = False

        signal.signal(signal.SIGINT, _stop_handler)
        signal.signal(signal.SIGTERM, _stop_handler)

        logger.info(
            "=== SOLANA PAPER TRADING BOTU BASLADI | bakiye $%.2f | min MC $%.0f | "
            "SL %%%.0f | TP %.1fx | Trail %%%.0f ===",
            self._broker.starting_balance_usd,
            self._cfg.min_market_cap_usd,
            self._cfg.stop_loss_pct,
            self._cfg.take_profit_multiplier,
            self._cfg.trailing_stop_pct,
        )

        # Ilk aday havuzunu hemen doldur
        try:
            self._market.refresh_candidate_pool()
        except Exception as exc:  # noqa: BLE001 - dongu kirmak yasak
            logger.error("Ilk havuz yuklenemedi: %s", exc)

        while self._running:
            try:
                self.run_cycle()

                # Periyodik havza yenileme (yeni coin'ler keşfedilsin)
                if (
                    self._cycle_count % self.POOL_REFRESH_EVERY_N_CYCLES == 0
                ):
                    self._market.refresh_candidate_pool()
            except Exception as exc:  # noqa: BLE001
                # HICBIR hata calisma zamani dongusunu dusurmeyecek
                logger.exception("Tur istisnasi (dongu devam ediyor): %s", exc)

            # Interval boyunca uyku; durus sinyalini hizli duymak icin parcala
            self._sleep_interruptible(self._cfg.scan_interval_seconds)

        self._broker.save_state()
        logger.info("Bot temiz kapandi. Toplam tur: %d", self._cycle_count)

    def _sleep_interruptible(self, seconds: int) -> None:
        """Uykuyu 1'er saniyelik dilimlere bolerek Ctrl+C tepkiselligini korur."""
        deadline = time.time() + seconds
        while self._running and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.time())))


def build_engine(config: Config) -> PaperTradingEngine:
    """Yapilandirmadan tam bagimli motor nesnesi kurar (composition root)."""
    setup_logging(config.log_level)

    jupiter = JupiterClient(
        price_api_url=config.jupiter_price_api_url,
        tokens_api_url=config.jupiter_tokens_api_url,
    )
    market = MarketDataService(
        jupiter=jupiter,
        min_market_cap_usd=config.min_market_cap_usd,
        quote_max_age_seconds=config.quote_max_age_seconds,
        candidate_pool_size=config.candidate_pool_size,
    )
    broker = PaperBroker(
        starting_balance_usd=config.starting_balance_usd,
        slippage_pct=config.simulated_slippage_pct,
        gas_fee_usd=config.simulated_gas_fee_usd,
        state_file=config.state_file,
    )
    risk = RiskManager(
        stop_loss_pct=config.stop_loss_pct,
        take_profit_multiplier=config.take_profit_multiplier,
        trailing_stop_pct=config.trailing_stop_pct,
        max_position_pct=config.max_position_pct,
    )
    ledger = TradeLedger(config.trade_log_file)
    return PaperTradingEngine(config, market, broker, risk, ledger)

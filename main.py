# -*- coding: utf-8 -*-
"""Bot giris noktasi (CLI).

Kullanim:
    python main.py run            -> Surekli tarama dongusunu baslatir
    python main.py once           -> Tek bir tarama turu calistirir (kumanda testi)
    python main.py status         -> Portfoy ozetini yazdirir
    python main.py reset --yes    -> Paper hesabi sifirlar (state + trades silinir)

Guvenlik: private key YOK, ag islemleri yalnizca salt-okur; al-sat
tamamen yerel muhasebe uzerinde simule edilir.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 'src' altindaki paketlerin import edilebilmesi icin yol ekle
_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))

from config.settings import load_config  # noqa: E402
from paperbot.engine import build_engine  # noqa: E402
from paperbot.execution.trade_ledger import TradeLedger  # noqa: E402
from paperbot.portfolio.broker import PaperBroker  # noqa: E402
from paperbot.utils.logger import setup_logging  # noqa: E402


def cmd_run(cfg) -> int:
    """Tam calisma zamani dongusu."""
    engine = build_engine(cfg)
    engine.run_forever()
    return 0


def cmd_once(cfg) -> int:
    """Tek tur: hizli dogrulama / cron ile calistirma icin ideal."""
    engine = build_engine(cfg)
    engine._market.refresh_candidate_pool()
    result = engine.run_cycle()
    print(
        f"Tur bitti: {len(result['entries'])} alim, "
        f"{len(result['exits'])} satim islemi."
    )
    return 0


def cmd_status(cfg) -> int:
    """Kayitli portfoy durumunu ve son islemleri raporla (ag kullanmaz)."""
    broker = PaperBroker(
        starting_balance_usd=cfg.starting_balance_usd,
        slippage_pct=cfg.simulated_slippage_pct,
        gas_fee_usd=cfg.simulated_gas_fee_usd,
        state_file=cfg.state_file,
    )
    ledger = TradeLedger(cfg.trade_log_file)

    print("=" * 60)
    print("PAPER TRADING PORTFOY DURUMU")
    print("=" * 60)
    print(f"Nakit bakiye      : ${broker.cash_usd:,.2f}")
    print(f"Acik pozisyon     : {len(broker.positions)}")
    for pos in broker.positions.values():
        print(
            f"  - {pos.symbol:<10} adet={pos.quantity:,.2f} "
            f"giris=${pos.entry_price:.8f} tepe=${pos.peak_price:.8f} "
            f"durum={pos.state.value}"
        )

    rows = ledger.read_all()
    realized = sum(float(r["realized_pnl_usd"]) for r in rows if r["side"] == "SELL")
    sells = [r for r in rows if r["side"] == "SELL"]
    wins = [r for r in sells if float(r["realized_pnl_usd"]) > 0]
    print("-" * 60)
    print(f"Toplam islem kaydi: {len(rows)} (satim: {len(sells)})")
    print(f"Gerceklesen P&L   : ${realized:+,.2f}")
    if sells:
        print(f"Kazanma orani     : %{len(wins) / len(sells) * 100:.1f}")
    return 0


def cmd_reset(cfg, assume_yes: bool) -> int:
    """Simulasyon hesaplarini sifirla. Onay mekanizmalidir."""
    targets = [cfg.state_file, cfg.trade_log_file]
    existing = [p for p in targets if Path(p).exists()]
    if not existing:
        print("Sifirlanacak dosya yok; temiz durum zaten mevcut.")
        return 0
    if not assume_yes:
        answer = input(f"'{', '.join(map(str, existing))}' silinecek. Onay? [y/N] ")
        if answer.strip().lower() != "y":
            print("Islem iptal edildi.")
            return 1
    for path in existing:
        os.remove(path)
        print(f"Silindi: {path}")
    print("Paper hesap sifirlandi. Bot yeni $%.2f bakiyeyle baslayacak." % cfg.starting_balance_usd)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="paperbot",
        description="Solana memecoin paper trading botu (simulasyon - gercek para yok)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Surekli tarama dongusunu baslat")
    sub.add_parser("once", help="Tek bir tarama turu calistir")
    sub.add_parser("status", help="Portfoy ve islem ozetini goster")
    reset_parser = sub.add_parser("reset", help="Paper hesabi sifirla")
    reset_parser.add_argument("--yes", action="store_true", help="Onayi atla")

    args = parser.parse_args(argv)
    cfg = load_config()
    setup_logging(cfg.log_level)

    if args.command == "run":
        return cmd_run(cfg)
    if args.command == "once":
        return cmd_once(cfg)
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "reset":
        return cmd_reset(cfg, assume_yes=args.yes)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

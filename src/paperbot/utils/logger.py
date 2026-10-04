# -*- coding: utf-8 -*-
"""Merkezi loglama yardimcisi.

Botun tum bilesenleri ayni formatli, hem konsola hem dosyaya yazan
bir logger kullanir. Islem bildirimleri (AL/SAT) ayrica "trades"
adinda ayr bir logger ile logs/paperbot.log dosyasina da duser.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Log dosyasinin ve klasorunun standart konumu
_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
_LOG_FILE = _LOG_DIR / "paperbot.log"

# Bir kere kurulum yapilsin; tekrar cagrildiginda mevcut logger donsun
_initialized = False


def setup_logging(level: str = "INFO") -> None:
    """Kok logger'a konsol + dosya handler'larini ekler.

    Args:
        level: "DEBUG", "INFO", "WARNING" veya "ERROR".
    """
    global _initialized
    if _initialized:
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root = logging.getLogger("paperbot")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    # Gereksiz duplicate loglari onlemek icin propagate kapatilir
    root.propagate = False

    _initialized = True


def get_logger(name: str) -> logging.Logger:
    """paperbot kok adi altinda isimlendirilmis logger dondurur."""
    return logging.getLogger(f"paperbot.{name}")

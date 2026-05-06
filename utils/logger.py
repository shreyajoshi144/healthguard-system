"""
utils/logger.py
───────────────
Centralised logging setup for the Patient Satisfaction platform.
All modules should use:  from utils.logger import get_logger
                         logger = get_logger(__name__)
"""

from __future__ import annotations
import logging
import os
from logging.handlers import RotatingFileHandler

# ── Constants ──────────────────────────────────────────────────────────────────
LOG_DIR  = "logs"
LOG_FILE = os.path.join(LOG_DIR, "app.log")
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# ── Ensure log directory exists ────────────────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)

# ── Root logger configuration (run once at import) ────────────────────────────
def _configure_root_logger() -> None:
    root = logging.getLogger()
    if root.handlers:          # already configured in this process
        return

    root.setLevel(logging.DEBUG)
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # Console handler — INFO and above
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    # File handler — DEBUG and above, rotating at 5 MB, keep last 3 files
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    root.addHandler(console_handler)
    root.addHandler(file_handler)


_configure_root_logger()


# ── Public factory ─────────────────────────────────────────────────────────────
def get_logger(name: str) -> logging.Logger:
    """
    Return a named logger. Usage:
        logger = get_logger(__name__)
        logger.info("Pipeline started")
        logger.error("Something went wrong: %s", exc)
    """
    return logging.getLogger(name)

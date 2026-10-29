"""Bounded application diagnostics; never log request bodies or query values."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_application_logging(root: Path) -> None:
    logger = logging.getLogger("backend")
    logger.setLevel(logging.INFO)
    directory = root / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "application.log"
    # App factories run repeatedly in tests; do not retain handlers for old roots.
    for handler in list(logger.handlers):
        if getattr(handler, "_application_handler", False):
            logger.removeHandler(handler)
            handler.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in (logging.StreamHandler(), RotatingFileHandler(
            target, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")):
        handler.setFormatter(formatter)
        handler._application_handler = True
        logger.addHandler(handler)
from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .runtime_paths import get_logs_dir


_LOGGER_NAME = "recorder"
_MAX_LOG_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 5


def configure_app_logging() -> Path:
    log_dir = get_logs_dir()
    log_path = log_dir / "recorder.log"
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(
            log_path,
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(threadName)s | %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return log_path


def get_logger(name: str) -> logging.Logger:
    configure_app_logging()
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")


def install_global_exception_logging() -> None:
    logger = get_logger("crash")

    def _sys_excepthook(exc_type: type[BaseException], exc_value: BaseException, exc_traceback: object) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        logger.exception("Unhandled exception in main thread", exc_info=(exc_type, exc_value, exc_traceback))
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    def _threading_excepthook(args: threading.ExceptHookArgs) -> None:
        logger.exception(
            "Unhandled exception in background thread: %s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_excepthook
    threading.excepthook = _threading_excepthook
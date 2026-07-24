from __future__ import annotations

import logging
import os
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

from .storage import app_data_root


LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3
LOG_LEVEL_ENV = "LOGPILOT_LOG_LEVEL"
_LOGGER_NAME = "logpilot"
_CONFIG_LOCK = threading.Lock()
_ORIGINAL_EXCEPTHOOK = sys.excepthook
_ORIGINAL_THREAD_EXCEPTHOOK = threading.excepthook
_HOOKS_INSTALLED = False

# Library imports stay silent until an application entry point configures a file handler.
logging.getLogger(_LOGGER_NAME).addHandler(logging.NullHandler())


def log_directory() -> Path:
    return app_data_root() / "logs"


def configure_logging(component: str, *, level: int | None = None) -> Path:
    safe_component = re.sub(r"[^a-z0-9-]+", "-", component.strip().lower()).strip("-") or "app"
    path = log_directory() / f"logpilot-{safe_component}.log"
    logger = logging.getLogger(_LOGGER_NAME)
    resolved_level = level if level is not None else _configured_level()
    with _CONFIG_LOCK:
        for handler in list(logger.handlers):
            if not getattr(handler, "_logpilot_handler", False):
                continue
            if Path(getattr(handler, "baseFilename", "")) == path:
                handler.setLevel(resolved_level)
                logger.setLevel(resolved_level)
                return path
            logger.removeHandler(handler)
            handler.close()

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"LogPilot logging unavailable at {path}: {exc}", file=sys.stderr)
            return path
        handler._logpilot_handler = True  # type: ignore[attr-defined]
        handler.setLevel(resolved_level)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s pid=%(process)d thread=%(threadName)s "
                "%(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(handler)
        logger.setLevel(resolved_level)
        logger.propagate = False
        _install_exception_hooks()
        logger.info("logging_initialized component=%s path=%s", safe_component, path)
    return path


def shutdown_logging() -> None:
    global _HOOKS_INSTALLED
    logger = logging.getLogger(_LOGGER_NAME)
    with _CONFIG_LOCK:
        for handler in list(logger.handlers):
            if getattr(handler, "_logpilot_handler", False):
                logger.removeHandler(handler)
                handler.close()
        if _HOOKS_INSTALLED:
            sys.excepthook = _ORIGINAL_EXCEPTHOOK
            threading.excepthook = _ORIGINAL_THREAD_EXCEPTHOOK
            _HOOKS_INSTALLED = False


def _configured_level() -> int:
    value = os.environ.get(LOG_LEVEL_ENV, "INFO").strip().upper()
    return {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }.get(value, logging.INFO)


def _install_exception_hooks() -> None:
    global _HOOKS_INSTALLED
    if _HOOKS_INSTALLED:
        return

    def log_uncaught(
        exception_type: type[BaseException],
        exception: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        if issubclass(exception_type, KeyboardInterrupt):
            _ORIGINAL_EXCEPTHOOK(exception_type, exception, traceback)
            return
        logging.getLogger(f"{_LOGGER_NAME}.uncaught").critical(
            "uncaught_exception",
            exc_info=(exception_type, exception, traceback),
        )
        _ORIGINAL_EXCEPTHOOK(exception_type, exception, traceback)

    def log_thread_uncaught(args: threading.ExceptHookArgs) -> None:
        logging.getLogger(f"{_LOGGER_NAME}.uncaught").critical(
            "uncaught_thread_exception thread=%s",
            args.thread.name if args.thread else "unknown",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        _ORIGINAL_THREAD_EXCEPTHOOK(args)

    sys.excepthook = log_uncaught
    threading.excepthook = log_thread_uncaught
    _HOOKS_INSTALLED = True

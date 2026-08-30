"""Private rotating diagnostics for windowed application builds."""

from __future__ import annotations

import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "package_audit"
LOG_FILENAME = "package-audit.log"


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            # Permission bits are not consistently supported on Windows.
            pass
        return stream


def configure_diagnostics(app_dir: Path) -> Path:
    """Configure a small private rotating log and return its path."""
    app_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        app_dir.chmod(0o700)
    except OSError:
        pass

    log_path = app_dir / LOG_FILENAME
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    existing = next(
        (
            handler
            for handler in logger.handlers
            if isinstance(handler, _PrivateRotatingFileHandler)
            and Path(handler.baseFilename) == log_path.resolve()
        ),
        None,
    )
    if existing is None:
        handler = _PrivateRotatingFileHandler(
            log_path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
    return log_path


def install_exception_hooks(log_path: Path) -> None:
    """Log uncaught main-thread and worker-thread exceptions."""
    logger = logging.getLogger(LOGGER_NAME)
    previous_hook = sys.excepthook

    def main_thread_hook(exc_type, exc, traceback) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            previous_hook(exc_type, exc, traceback)
            return
        logger.critical("Unhandled application exception", exc_info=(exc_type, exc, traceback))
        try:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(
                None,
                "Unexpected error",
                f"Package Audit encountered an unexpected error. Details were written to:\n{log_path}",
            )
        except Exception:  # noqa: BLE001 - never mask the original exception
            previous_hook(exc_type, exc, traceback)

    def worker_thread_hook(args: threading.ExceptHookArgs) -> None:
        logger.critical(
            "Unhandled worker-thread exception",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = main_thread_hook
    threading.excepthook = worker_thread_hook

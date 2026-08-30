"""Tests for private windowed-build diagnostics."""

from __future__ import annotations

import logging
import os
import sys
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QMessageBox

from app.diagnostics import LOGGER_NAME, configure_diagnostics, install_exception_hooks


@pytest.fixture(autouse=True)
def close_test_log_handlers():
    logger = logging.getLogger(LOGGER_NAME)
    original_handlers = tuple(logger.handlers)
    original_sys_hook = sys.excepthook
    original_thread_hook = threading.excepthook
    yield
    sys.excepthook = original_sys_hook
    threading.excepthook = original_thread_hook
    for handler in tuple(logger.handlers):
        if handler not in original_handlers:
            logger.removeHandler(handler)
            handler.close()


def test_configure_diagnostics_writes_private_log(tmp_path):
    log_path = configure_diagnostics(tmp_path / "state")
    logger = logging.getLogger(LOGGER_NAME)
    logger.info("diagnostic test message")
    for handler in logger.handlers:
        handler.flush()

    assert "diagnostic test message" in log_path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert log_path.parent.stat().st_mode & 0o777 == 0o700
        assert log_path.stat().st_mode & 0o777 == 0o600


def test_configure_diagnostics_does_not_duplicate_handlers(tmp_path):
    app_dir = tmp_path / "state"
    configure_diagnostics(app_dir)
    logger = logging.getLogger(LOGGER_NAME)
    matching_before = [
        handler
        for handler in logger.handlers
        if getattr(handler, "baseFilename", None) == str(app_dir / "package-audit.log")
    ]

    configure_diagnostics(app_dir)

    matching_after = [
        handler
        for handler in logger.handlers
        if getattr(handler, "baseFilename", None) == str(app_dir / "package-audit.log")
    ]
    assert len(matching_before) == len(matching_after) == 1


def test_exception_hooks_log_main_and_worker_failures(tmp_path, monkeypatch):
    log_path = configure_diagnostics(tmp_path / "state")
    messages = []
    monkeypatch.setattr(
        QMessageBox,
        "critical",
        lambda _parent, title, message: messages.append((title, message)),
    )
    install_exception_hooks(log_path)

    main_error = RuntimeError("main failure")
    sys.excepthook(RuntimeError, main_error, None)
    worker_error = ValueError("worker failure")
    threading.excepthook(
        SimpleNamespace(
            exc_type=ValueError,
            exc_value=worker_error,
            exc_traceback=None,
        )
    )
    for handler in logging.getLogger(LOGGER_NAME).handlers:
        handler.flush()

    log_contents = log_path.read_text(encoding="utf-8")
    assert "Unhandled application exception" in log_contents
    assert "main failure" in log_contents
    assert "Unhandled worker-thread exception" in log_contents
    assert "worker failure" in log_contents
    assert len(messages) == 1
    assert messages[0][0] == "Unexpected error"
    assert str(log_path) in messages[0][1]

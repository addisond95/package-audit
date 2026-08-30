"""Helpers for replacing user-facing export files atomically."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def spreadsheet_safe_cell(value: str) -> str:
    """Prevent a text value from being interpreted as a spreadsheet formula."""
    if value.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


@contextmanager
def atomic_output_path(output_path: Path) -> Iterator[Path]:
    """Yield a private sibling path and replace *output_path* on success.

    Export failures must not truncate a user's existing report. A sibling
    temporary file keeps the final rename on the same filesystem, where
    :func:`os.replace` is atomic.
    """
    output_path = Path(output_path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.stem}-",
        suffix=output_path.suffix or ".tmp",
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        yield temporary_path
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)

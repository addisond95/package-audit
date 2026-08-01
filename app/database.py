"""SQLite persistence for audit state and manual report rows.

Audit progress is keyed by a PDF content hash so an audit can be paused and
resumed simply by reopening the same export.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.models import (
    DoubleLoggedPackage,
    PackageError,
    normalize_carrier,
    normalize_last4,
    normalize_location,
    normalize_unit,
)


class AuditDatabase:
    """Lightweight data-access layer over a single SQLite file."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.create_tables()

    def __enter__(self) -> AuditDatabase:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def create_tables(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_state (
                    pdf_hash TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    audited INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (pdf_hash, item_id)
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS package_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pdf_hash TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    location TEXT NOT NULL,
                    carrier TEXT NOT NULL,
                    last4 TEXT NOT NULL,
                    note TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS double_logged (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pdf_hash TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    location TEXT NOT NULL,
                    carrier TEXT NOT NULL,
                    last4 TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_package_errors_hash ON package_errors (pdf_hash)"
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_double_logged_hash ON double_logged (pdf_hash)")

    def load_state(self, pdf_hash: str) -> dict[str, bool]:
        rows = self.conn.execute(
            "SELECT item_id, audited FROM audit_state WHERE pdf_hash = ?",
            (pdf_hash,),
        ).fetchall()

        return {item_id: bool(audited) for item_id, audited in rows}

    def set_state(self, pdf_hash: str, item_id: str, audited: bool) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO audit_state (pdf_hash, item_id, audited)
                VALUES (?, ?, ?)
                ON CONFLICT(pdf_hash, item_id)
                DO UPDATE SET audited = excluded.audited
                """,
                (pdf_hash, item_id, int(audited)),
            )

    def clear_audit_state(self, pdf_hash: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM audit_state WHERE pdf_hash = ?", (pdf_hash,))

    def clear_manual_rows(self, pdf_hash: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM package_errors WHERE pdf_hash = ?", (pdf_hash,))
            self.conn.execute("DELETE FROM double_logged WHERE pdf_hash = ?", (pdf_hash,))

    def clear_all_for_pdf(self, pdf_hash: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM audit_state WHERE pdf_hash = ?", (pdf_hash,))
            self.conn.execute("DELETE FROM package_errors WHERE pdf_hash = ?", (pdf_hash,))
            self.conn.execute("DELETE FROM double_logged WHERE pdf_hash = ?", (pdf_hash,))

    def load_package_errors(self, pdf_hash: str) -> list[PackageError]:
        rows = self.conn.execute(
            """
            SELECT unit, location, carrier, last4, note
            FROM package_errors
            WHERE pdf_hash = ?
            ORDER BY unit, carrier, last4
            """,
            (pdf_hash,),
        ).fetchall()

        return [
            PackageError(
                unit=normalize_unit(unit),
                location=normalize_location(location),
                carrier=normalize_carrier(carrier),
                last4=normalize_last4(last4),
                note=note.strip(),
            )
            for unit, location, carrier, last4, note in rows
        ]

    def replace_package_errors(self, pdf_hash: str, rows: list[PackageError]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM package_errors WHERE pdf_hash = ?", (pdf_hash,))
            self.conn.executemany(
                """
                INSERT INTO package_errors (pdf_hash, unit, location, carrier, last4, note)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        pdf_hash,
                        normalize_unit(row.unit),
                        normalize_location(row.location),
                        normalize_carrier(row.carrier),
                        normalize_last4(row.last4),
                        row.note.strip(),
                    )
                    for row in rows
                ],
            )

    def load_double_logged(self, pdf_hash: str) -> list[DoubleLoggedPackage]:
        rows = self.conn.execute(
            """
            SELECT unit, location, carrier, last4
            FROM double_logged
            WHERE pdf_hash = ?
            ORDER BY unit, carrier, last4
            """,
            (pdf_hash,),
        ).fetchall()

        return [
            DoubleLoggedPackage(
                unit=normalize_unit(unit),
                location=normalize_location(location),
                carrier=normalize_carrier(carrier),
                last4=normalize_last4(last4),
            )
            for unit, location, carrier, last4 in rows
        ]

    def replace_double_logged(self, pdf_hash: str, rows: list[DoubleLoggedPackage]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM double_logged WHERE pdf_hash = ?", (pdf_hash,))
            self.conn.executemany(
                """
                INSERT INTO double_logged (pdf_hash, unit, location, carrier, last4)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        pdf_hash,
                        normalize_unit(row.unit),
                        normalize_location(row.location),
                        normalize_carrier(row.carrier),
                        normalize_last4(row.last4),
                    )
                    for row in rows
                ],
            )

    def close(self) -> None:
        self.conn.close()

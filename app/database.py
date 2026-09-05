"""SQLite persistence for audit state and manual report rows.

Audit progress is keyed by a PDF content hash so an audit can be paused and
resumed simply by reopening the same export.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models import (
    DoubleLoggedPackage,
    PackageError,
    ScannerAlert,
    ScannerEvent,
    normalize_carrier,
    normalize_location,
    normalize_tracking,
    normalize_unit,
    resolve_last4,
)


class AuditDatabase:
    """Lightweight data-access layer over a single SQLite file."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.db_path.parent.chmod(0o700)
        except OSError:
            # Permission bits are not consistently supported on Windows.
            pass
        self.conn = sqlite3.connect(str(db_path))
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            # Permission bits are not consistently supported on Windows.
            pass
        self.create_tables()

    def __enter__(self) -> AuditDatabase:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _last4(last4: str, tracking: str) -> str:
        return resolve_last4(last4, tracking)

    @staticmethod
    def _json_object(value: str) -> dict[str, Any]:
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    @staticmethod
    def _json_string_list(value: str) -> tuple[str, ...]:
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return ()
        if not isinstance(decoded, list):
            return ()
        return tuple(item for item in decoded if isinstance(item, str))

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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
            self._ensure_column("package_errors", "tracking", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("double_logged", "tracking", "TEXT NOT NULL DEFAULT ''")
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_package_errors_hash ON package_errors (pdf_hash)"
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_double_logged_hash ON double_logged (pdf_hash)")
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scanner_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pdf_hash TEXT NOT NULL,
                    alert_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    carrier TEXT NOT NULL,
                    tracking TEXT NOT NULL,
                    last4 TEXT NOT NULL,
                    message TEXT NOT NULL,
                    item_ids TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    UNIQUE (pdf_hash, alert_key)
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scanner_events (
                    pdf_hash TEXT NOT NULL,
                    scan_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    unit TEXT NOT NULL,
                    carrier TEXT NOT NULL,
                    tracking TEXT NOT NULL,
                    last4 TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (pdf_hash, scan_id)
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scanner_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pdf_hash TEXT NOT NULL,
                    scan_key TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    suggested_item_id TEXT NOT NULL,
                    chosen_item_id TEXT NOT NULL,
                    features TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scanner_model (
                    model_key TEXT PRIMARY KEY,
                    model_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

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

    def set_states(self, pdf_hash: str, states: list[tuple[str, bool]]) -> None:
        """Persist several audit-state changes in one transaction."""
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO audit_state (pdf_hash, item_id, audited)
                VALUES (?, ?, ?)
                ON CONFLICT(pdf_hash, item_id)
                DO UPDATE SET audited = excluded.audited
                """,
                [(pdf_hash, item_id, int(audited)) for item_id, audited in states],
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
            self.conn.execute("DELETE FROM scanner_alerts WHERE pdf_hash = ?", (pdf_hash,))
            self.conn.execute("DELETE FROM scanner_events WHERE pdf_hash = ?", (pdf_hash,))
            self.conn.execute("DELETE FROM scanner_feedback WHERE pdf_hash = ?", (pdf_hash,))

    def load_package_errors(self, pdf_hash: str) -> list[PackageError]:
        rows = self.conn.execute(
            """
            SELECT unit, location, carrier, tracking, last4, note
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
                tracking=normalize_tracking(tracking),
                last4=resolve_last4(last4, tracking),
                note=note.strip(),
            )
            for unit, location, carrier, tracking, last4, note in rows
        ]

    def replace_package_errors(self, pdf_hash: str, rows: list[PackageError]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM package_errors WHERE pdf_hash = ?", (pdf_hash,))
            self.conn.executemany(
                """
                INSERT INTO package_errors (pdf_hash, unit, location, carrier, tracking, last4, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        pdf_hash,
                        normalize_unit(row.unit),
                        normalize_location(row.location),
                        normalize_carrier(row.carrier),
                        normalize_tracking(row.tracking),
                        self._last4(row.last4, row.tracking),
                        row.note.strip(),
                    )
                    for row in rows
                ],
            )

    def add_package_error_if_missing(self, pdf_hash: str, row: PackageError) -> bool:
        unit = normalize_unit(row.unit)
        tracking = normalize_tracking(row.tracking)
        last4 = self._last4(row.last4, tracking)
        note = row.note.strip()
        if tracking:
            existing = self.conn.execute(
                "SELECT 1 FROM package_errors WHERE pdf_hash = ? AND tracking = ?",
                (pdf_hash, tracking),
            ).fetchone()
        else:
            existing = self.conn.execute(
                """
                SELECT 1 FROM package_errors
                WHERE pdf_hash = ? AND unit = ? AND last4 = ? AND note = ?
                """,
                (pdf_hash, unit, last4, note),
            ).fetchone()
        if existing:
            return False
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO package_errors (pdf_hash, unit, location, carrier, tracking, last4, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pdf_hash,
                    unit,
                    normalize_location(row.location),
                    normalize_carrier(row.carrier),
                    tracking,
                    last4,
                    note,
                ),
            )
        return True

    def delete_package_error(self, pdf_hash: str, row: PackageError) -> None:
        """Delete one exact automatic row without touching similar manual rows."""
        tracking = normalize_tracking(row.tracking)
        with self.conn:
            self.conn.execute(
                """
                DELETE FROM package_errors
                WHERE pdf_hash = ? AND unit = ? AND location = ? AND carrier = ?
                  AND tracking = ? AND last4 = ? AND note = ?
                """,
                (
                    pdf_hash,
                    normalize_unit(row.unit),
                    normalize_location(row.location),
                    normalize_carrier(row.carrier),
                    tracking,
                    self._last4(row.last4, tracking),
                    row.note.strip(),
                ),
            )

    def load_double_logged(self, pdf_hash: str) -> list[DoubleLoggedPackage]:
        rows = self.conn.execute(
            """
            SELECT unit, location, carrier, tracking, last4
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
                tracking=normalize_tracking(tracking),
                last4=resolve_last4(last4, tracking),
            )
            for unit, location, carrier, tracking, last4 in rows
        ]

    def replace_double_logged(self, pdf_hash: str, rows: list[DoubleLoggedPackage]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM double_logged WHERE pdf_hash = ?", (pdf_hash,))
            self.conn.executemany(
                """
                INSERT INTO double_logged (pdf_hash, unit, location, carrier, tracking, last4)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        pdf_hash,
                        normalize_unit(row.unit),
                        normalize_location(row.location),
                        normalize_carrier(row.carrier),
                        normalize_tracking(row.tracking),
                        self._last4(row.last4, row.tracking),
                    )
                    for row in rows
                ],
            )

    def add_double_logged_if_missing(self, pdf_hash: str, row: DoubleLoggedPackage) -> bool:
        unit = normalize_unit(row.unit)
        tracking = normalize_tracking(row.tracking)
        last4 = self._last4(row.last4, tracking)
        existing = self.conn.execute(
            """
            SELECT 1 FROM double_logged
            WHERE pdf_hash = ? AND unit = ?
              AND (
                  (? <> '' AND tracking <> '' AND tracking = ?)
                  OR ((? = '' OR tracking = '') AND last4 = ?)
              )
            """,
            (pdf_hash, unit, tracking, tracking, tracking, last4),
        ).fetchone()
        if existing:
            return False
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO double_logged (pdf_hash, unit, location, carrier, tracking, last4)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    pdf_hash,
                    unit,
                    normalize_location(row.location),
                    normalize_carrier(row.carrier),
                    tracking,
                    last4,
                ),
            )
        return True

    def delete_double_logged(self, pdf_hash: str, row: DoubleLoggedPackage) -> None:
        """Delete one exact automatic duplicate without removing nearby manual data."""
        tracking = normalize_tracking(row.tracking)
        with self.conn:
            self.conn.execute(
                """
                DELETE FROM double_logged
                WHERE pdf_hash = ? AND unit = ? AND location = ? AND carrier = ?
                  AND tracking = ? AND last4 = ?
                """,
                (
                    pdf_hash,
                    normalize_unit(row.unit),
                    normalize_location(row.location),
                    normalize_carrier(row.carrier),
                    tracking,
                    self._last4(row.last4, tracking),
                ),
            )

    def upsert_scanner_alert(self, pdf_hash: str, alert: ScannerAlert) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO scanner_alerts (
                    pdf_hash, alert_key, kind, severity, unit, carrier, tracking,
                    last4, message, item_ids, resolved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pdf_hash, alert_key) DO UPDATE SET
                    kind = excluded.kind,
                    severity = excluded.severity,
                    unit = excluded.unit,
                    carrier = excluded.carrier,
                    tracking = excluded.tracking,
                    last4 = excluded.last4,
                    message = excluded.message,
                    item_ids = excluded.item_ids,
                    resolved = excluded.resolved
                """,
                (
                    pdf_hash,
                    alert.alert_key,
                    alert.kind,
                    alert.severity,
                    normalize_unit(alert.unit),
                    normalize_carrier(alert.carrier),
                    normalize_tracking(alert.tracking),
                    self._last4(alert.last4, alert.tracking),
                    alert.message.strip(),
                    json.dumps(list(alert.item_ids)),
                    int(alert.resolved),
                    alert.created_at or self._now(),
                ),
            )

    def load_scanner_alerts(self, pdf_hash: str, *, include_resolved: bool = True) -> list[ScannerAlert]:
        if include_resolved:
            rows = self.conn.execute(
                """
                SELECT alert_key, kind, severity, unit, carrier, tracking, last4,
                       message, item_ids, resolved, created_at
                FROM scanner_alerts WHERE pdf_hash = ?
                ORDER BY resolved, created_at DESC, id DESC
                """,
                (pdf_hash,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT alert_key, kind, severity, unit, carrier, tracking, last4,
                       message, item_ids, resolved, created_at
                FROM scanner_alerts WHERE pdf_hash = ? AND resolved = 0
                ORDER BY created_at DESC, id DESC
                """,
                (pdf_hash,),
            ).fetchall()
        return [
            ScannerAlert(
                alert_key=alert_key,
                kind=kind,
                severity=severity,
                unit=unit,
                carrier=carrier,
                tracking=tracking,
                last4=last4,
                message=message,
                item_ids=self._json_string_list(item_ids),
                resolved=bool(resolved),
                created_at=created_at,
            )
            for (
                alert_key,
                kind,
                severity,
                unit,
                carrier,
                tracking,
                last4,
                message,
                item_ids,
                resolved,
                created_at,
            ) in rows
        ]

    def resolve_scanner_alert(self, pdf_hash: str, alert_key: str, resolved: bool = True) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE scanner_alerts SET resolved = ? WHERE pdf_hash = ? AND alert_key = ?",
                (int(resolved), pdf_hash, alert_key),
            )

    def delete_scanner_alert(self, pdf_hash: str, alert_key: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM scanner_alerts WHERE pdf_hash = ? AND alert_key = ?",
                (pdf_hash, alert_key),
            )

    def save_scanner_event(self, pdf_hash: str, event: ScannerEvent) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO scanner_events (
                    pdf_hash, scan_id, status, confidence, unit, carrier, tracking,
                    last4, item_id, message, details, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pdf_hash, scan_id) DO UPDATE SET
                    status = excluded.status,
                    confidence = excluded.confidence,
                    unit = excluded.unit,
                    carrier = excluded.carrier,
                    tracking = excluded.tracking,
                    last4 = excluded.last4,
                    item_id = excluded.item_id,
                    message = excluded.message,
                    details = excluded.details
                """,
                (
                    pdf_hash,
                    event.scan_id,
                    event.status,
                    event.confidence,
                    normalize_unit(event.unit),
                    normalize_carrier(event.carrier),
                    normalize_tracking(event.tracking),
                    self._last4(event.last4, event.tracking),
                    event.item_id,
                    event.message,
                    json.dumps(event.details or {}, sort_keys=True),
                    event.created_at or self._now(),
                ),
            )

    def load_scanner_events(self, pdf_hash: str, limit: int = 100) -> list[ScannerEvent]:
        rows = self.conn.execute(
            """
            SELECT scan_id, status, confidence, unit, carrier, tracking, last4,
                   item_id, message, details, created_at
            FROM scanner_events WHERE pdf_hash = ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (pdf_hash, limit),
        ).fetchall()
        return [
            ScannerEvent(
                scan_id=scan_id,
                status=status,
                confidence=confidence,
                unit=unit,
                carrier=carrier,
                tracking=tracking,
                last4=last4,
                item_id=item_id,
                message=message,
                details=self._json_object(details),
                created_at=created_at,
            )
            for (
                scan_id,
                status,
                confidence,
                unit,
                carrier,
                tracking,
                last4,
                item_id,
                message,
                details,
                created_at,
            ) in rows
        ]

    def scanner_units_for_tracking(self, pdf_hash: str, tracking: str) -> set[str]:
        """Return every non-blank unit previously recorded for a tracking value."""
        normalized = normalize_tracking(tracking)
        if not normalized:
            return set()
        rows = self.conn.execute(
            """
            SELECT DISTINCT unit FROM scanner_events
            WHERE pdf_hash = ? AND tracking = ? AND unit <> ''
            """,
            (pdf_hash, normalized),
        ).fetchall()
        return {normalize_unit(row[0]) for row in rows}

    def record_scanner_feedback(
        self,
        pdf_hash: str,
        scan_key: str,
        outcome: str,
        suggested_item_id: str = "",
        chosen_item_id: str = "",
        features: dict[str, Any] | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO scanner_feedback (
                    pdf_hash, scan_key, outcome, suggested_item_id,
                    chosen_item_id, features, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pdf_hash,
                    scan_key,
                    outcome,
                    suggested_item_id,
                    chosen_item_id,
                    json.dumps(features or {}, sort_keys=True),
                    self._now(),
                ),
            )

    def scanner_feedback_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) FROM scanner_feedback").fetchone()
        return int(row[0])

    def load_scanner_model(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT model_json FROM scanner_model WHERE model_key = 'default'").fetchone()
        return self._json_object(row[0]) if row else None

    def save_scanner_model(self, model: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO scanner_model (model_key, model_json, updated_at)
                VALUES ('default', ?, ?)
                ON CONFLICT(model_key) DO UPDATE SET
                    model_json = excluded.model_json,
                    updated_at = excluded.updated_at
                """,
                (json.dumps(model, sort_keys=True), self._now()),
            )

    def close(self) -> None:
        self.conn.close()

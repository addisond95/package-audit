import sys


def export_smoke() -> None:
    """Exercise report and highlighted-PDF exports in source or frozen builds."""
    import tempfile
    from pathlib import Path

    import pymupdf as fitz
    from PySide6.QtGui import QColor

    from app.audit_report import write_audit_report
    from app.export_pdf import write_highlighted_pdf
    from app.export_utils import spreadsheet_safe_cell
    from app.models import AuditEntry

    payload = "1Z999AA10123456784"
    entry = AuditEntry(
        "one",
        0,
        "1701S",
        "Mathiesen",
        f"UPS - #1 - {payload}",
        "South",
        "2026-08-28",
        audited=True,
    )
    with tempfile.TemporaryDirectory(prefix="package-audit-export-smoke-") as temporary_directory:
        directory = Path(temporary_directory)
        source_pdf = directory / "source.pdf"
        highlighted_pdf = directory / "highlighted.pdf"
        report_path = directory / "audit.txt"
        with fitz.open() as document:
            page = document.new_page()
            page.insert_text((50, 100), f"1701S Mathiesen UPS - #1 - {payload}")
            document.save(source_pdf)

        result = write_highlighted_pdf(
            source_pdf,
            highlighted_pdf,
            [entry],
            QColor(80, 200, 120, 95),
        )
        if result.highlighted_item_ids != (entry.item_id,) or result.unresolved_item_ids:
            raise RuntimeError("Highlighted-PDF smoke test did not resolve the audited row.")
        with fitz.open(highlighted_pdf) as document:
            if len(list(document[0].annots() or [])) != 1:
                raise RuntimeError("Highlighted-PDF smoke test did not create one annotation.")

        entry.audited = False
        write_audit_report(report_path, [entry], [], [], source_pdf.name)
        if "1701S | 6784" not in report_path.read_text(encoding="utf-8"):
            raise RuntimeError("Audit-report smoke test did not contain the expected package.")
        if spreadsheet_safe_cell("=1+1") != "'=1+1":
            raise RuntimeError("Spreadsheet formula neutralization smoke test failed.")
    print("export-smoke-ok")


def scanner_smoke() -> None:
    import io

    import qrcode
    import zxingcpp
    from PIL import Image

    from app.models import AuditEntry
    from app.scanner_server import ScannerCoordinator, ScannerServer, create_scanner_app

    payload = "1Z999AA10123456784"
    image = qrcode.make(payload).convert("RGB")
    if not isinstance(image, Image.Image):
        raise RuntimeError("QR generation did not return a Pillow image.")
    if not any(result.text == payload for result in zxingcpp.read_barcodes(image)):
        raise RuntimeError("ZXing could not decode the generated smoke-test QR code.")

    coordinator = ScannerCoordinator()
    coordinator.configure(
        "smoke",
        [AuditEntry("one", 0, "1701S", "Mathiesen", f"UPS - #1 - {payload}", "", "")],
    )
    image_bytes = io.BytesIO()
    image.save(image_bytes, format="PNG")
    scanned = coordinator.process_image(image_bytes.getvalue())
    if scanned.get("status") != "confirm" or scanned.get("unit") != "1701S":
        raise RuntimeError("Phone scanner did not resolve the exact tracking number to its audit unit.")
    coordinator.drain_actions()
    confirmed = coordinator.confirm(scanned["scan_id"], "one")
    if confirmed.get("status") != "matched":
        raise RuntimeError("Phone scanner confirmation did not queue the audit match.")
    coordinator.drain_actions()
    web_app = create_scanner_app(coordinator, "123456", "scanner-smoke-secret")
    web_app.config["TESTING"] = True
    client = web_app.test_client()
    if client.post("/pair", data={"code": "123456"}).status_code != 302:
        raise RuntimeError("Phone scanner pairing smoke test failed.")
    phone_page = client.get("/scanner")
    if phone_page.status_code != 200 or b"Scan package" not in phone_page.data:
        raise RuntimeError("Phone scanner interface smoke test failed.")
    status = client.get("/api/status").get_json()
    if status.get("packages") != 1 or status.get("remaining") != 1:
        raise RuntimeError("Phone scanner progress smoke test failed.")

    server = ScannerServer(coordinator)
    server.host_address = "127.0.0.1"
    server.start()
    try:
        if not server.running or server.port <= 0:
            raise RuntimeError("Scanner server did not start successfully.")
    finally:
        server.stop()
    print("scanner-smoke-ok")


def remote_scanner_smoke() -> None:
    """Exercise a real temporary Cloudflare route when cloudflared is installed."""
    from app.models import AuditEntry
    from app.scanner_server import ScannerCoordinator, ScannerServer

    coordinator = ScannerCoordinator()
    coordinator.configure(
        "remote-smoke",
        [
            AuditEntry(
                "one",
                0,
                "1701S",
                "Test Resident",
                "UPS - #1 - 1Z999AA10123456784",
                "South",
                "2026-09-05",
            )
        ],
    )
    server = ScannerServer(coordinator, remote=True)
    server.start()
    try:
        if not server.running or not server.url.startswith("https://"):
            raise RuntimeError("Remote scanner HTTPS page was not reachable through Cloudflare.")
    finally:
        server.stop()
    print("remote-scanner-smoke-ok")


def ui_smoke() -> None:
    """Construct, show, and cleanly close the real window offscreen."""
    import os
    import tempfile
    from pathlib import Path

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    import app.main_window as main_window
    from app.constants import APP_NAME
    from app.theme import build_stylesheet

    with tempfile.TemporaryDirectory(prefix="package-audit-smoke-") as temporary_directory:
        main_window.APP_DIR = Path(temporary_directory)
        app = QApplication.instance() or QApplication([])
        app.setApplicationName(APP_NAME)
        app.setStyle("Fusion")
        app.setStyleSheet(build_stylesheet())
        window = main_window.PackageAuditApp()
        window.show()
        QTimer.singleShot(50, window.close)
        QTimer.singleShot(5_000, app.quit)
        if app.exec() != 0:
            raise RuntimeError("Qt UI smoke test exited unsuccessfully.")
    print("ui-smoke-ok")


if __name__ == "__main__":
    if "--export-smoke" in sys.argv:
        export_smoke()
    elif "--remote-scanner-smoke" in sys.argv:
        remote_scanner_smoke()
    elif "--scanner-smoke" in sys.argv:
        scanner_smoke()
    elif "--ui-smoke" in sys.argv:
        ui_smoke()
    else:
        from app.main_window import main

        main()

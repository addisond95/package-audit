import sys


def scanner_smoke() -> None:
    import qrcode
    import zxingcpp
    from PIL import Image

    from app.models import AuditEntry
    from app.scanner_server import ScannerCoordinator, ScannerServer

    payload = "1Z999AA10123456784"
    image = qrcode.make(payload).convert("RGB")
    assert isinstance(image, Image.Image)
    assert any(result.text == payload for result in zxingcpp.read_barcodes(image))

    coordinator = ScannerCoordinator()
    coordinator.configure(
        "smoke",
        [AuditEntry("one", 0, "1701S", "Mathiesen", f"UPS - #1 - {payload}", "", "")],
    )
    server = ScannerServer(coordinator)
    server.host_address = "127.0.0.1"
    server.start()
    try:
        assert server.running and server.port > 0
    finally:
        server.stop()
    print("scanner-smoke-ok")


if __name__ == "__main__":
    if "--scanner-smoke" in sys.argv:
        scanner_smoke()
    else:
        from app.main_window import main

        main()

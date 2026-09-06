"""Tests for the optional Cloudflare Quick Tunnel lifecycle."""

from __future__ import annotations

import io
import subprocess

import pytest

import app.scanner_tunnel as scanner_tunnel
from app.scanner_tunnel import (
    CloudflareQuickTunnel,
    TunnelStartupError,
    TunnelUnavailableError,
    find_cloudflared,
)


class _FakeProcess:
    def __init__(self, output: str, *, returncode: int | None = None) -> None:
        self.stdout = io.StringIO(output)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("cloudflared", timeout)
        return self.returncode


def test_find_cloudflared_honors_explicit_executable(tmp_path, monkeypatch):
    executable = tmp_path / "cloudflared"
    executable.write_text("placeholder", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setenv("PACKAGE_AUDIT_CLOUDFLARED", str(executable))

    assert find_cloudflared() == str(executable)


def test_missing_cloudflared_has_actionable_install_message(monkeypatch):
    monkeypatch.setattr(scanner_tunnel, "find_cloudflared", lambda: None)

    with pytest.raises(TunnelUnavailableError, match="brew install cloudflared"):
        CloudflareQuickTunnel().start("http://127.0.0.1:1234")


def test_quick_tunnel_extracts_https_url_and_stops_process():
    process = _FakeProcess(
        "Starting tunnel\nYour quick Tunnel has been created! https://calm-box.trycloudflare.com\n"
        "Registered tunnel connection connIndex=0 protocol=http2\n"
    )
    calls = []

    def popen(command, **options):
        calls.append((command, options))
        return process

    tunnel = CloudflareQuickTunnel(
        executable="/test/cloudflared",
        popen_factory=popen,
        readiness_probe=lambda _url: True,
    )

    assert tunnel.start("http://127.0.0.1:4321") == "https://calm-box.trycloudflare.com"
    assert tunnel.running
    assert calls[0][0] == [
        "/test/cloudflared",
        "tunnel",
        "--no-autoupdate",
        "--url",
        "http://127.0.0.1:4321",
    ]
    assert calls[0][1]["shell"] is False

    tunnel.stop()

    assert process.terminated
    assert not tunnel.running


def test_quick_tunnel_reports_early_process_failure():
    process = _FakeProcess("Unable to reach the Cloudflare edge\n", returncode=1)
    tunnel = CloudflareQuickTunnel(
        executable="/test/cloudflared",
        popen_factory=lambda *_args, **_options: process,
    )

    with pytest.raises(TunnelStartupError, match="Unable to reach the Cloudflare edge"):
        tunnel.start("http://127.0.0.1:4321")

    assert not tunnel.running


def test_quick_tunnel_waits_until_public_route_is_reachable():
    process = _FakeProcess("https://calm-box.trycloudflare.com\nRegistered tunnel connection connIndex=0\n")
    attempts = []

    def probe(url):
        attempts.append(url)
        return len(attempts) == 2

    tunnel = CloudflareQuickTunnel(
        executable="/test/cloudflared",
        startup_timeout=2,
        popen_factory=lambda *_args, **_options: process,
        readiness_probe=probe,
    )

    assert tunnel.start("http://127.0.0.1:4321") == "https://calm-box.trycloudflare.com"
    assert len(attempts) == 2
    tunnel.stop()


def test_quick_tunnel_mentions_config_file_limitation():
    process = _FakeProcess("A config.yaml file is present\n", returncode=1)
    tunnel = CloudflareQuickTunnel(
        executable="/test/cloudflared",
        popen_factory=lambda *_args, **_options: process,
    )

    with pytest.raises(TunnelStartupError, match="Temporarily move that config file"):
        tunnel.start("http://127.0.0.1:4321")

"""Temporary Cloudflare tunnel support for the phone scanner."""

from __future__ import annotations

import os
import platform
import queue
import re
import shutil

# Required to supervise cloudflared with a fixed argv and shell disabled.
import subprocess  # nosec B404
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from typing import TextIO
from urllib.parse import urlparse

_TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)
_TUNNEL_READY = re.compile(r"registered tunnel connection", re.IGNORECASE)


class TunnelError(OSError):
    """Base error for starting a remote scanner tunnel."""


class TunnelUnavailableError(TunnelError):
    """Raised when the cloudflared executable cannot be found."""


class TunnelStartupError(TunnelError):
    """Raised when cloudflared cannot establish a quick tunnel."""


def find_cloudflared() -> str | None:
    """Find cloudflared, including Homebrew paths absent from macOS app PATHs."""
    configured = os.environ.get("PACKAGE_AUDIT_CLOUDFLARED")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None

    discovered = shutil.which("cloudflared")
    if discovered:
        return discovered

    candidates = [
        Path("/opt/homebrew/bin/cloudflared"),
        Path("/usr/local/bin/cloudflared"),
    ]
    if platform.system() == "Windows":
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            candidates.append(Path(program_files) / "cloudflared" / "cloudflared.exe")

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def cloudflared_install_help() -> str:
    """Return concise platform-specific installation guidance."""
    if platform.system() == "Darwin":
        command = "brew install cloudflared"
    elif platform.system() == "Windows":
        command = "winget install --id Cloudflare.cloudflared"
    else:
        command = "Install cloudflared from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    return f"Remote scanning needs the free cloudflared utility. Install it with:\n\n{command}"


def _https_route_ready(url: str) -> bool:
    """Confirm the generated hostname resolves and reaches its scanner origin."""
    if _TUNNEL_URL.fullmatch(url) is None:
        return False
    host = urlparse(url).hostname
    if not host:
        return False
    connection = HTTPSConnection(host, timeout=3)
    try:
        connection.request("GET", "/", headers={"User-Agent": "PackageAudit/remote-check"})
        response = connection.getresponse()
        response.read(1)
        return response.status == 200
    except (HTTPException, OSError):
        return False
    finally:
        with suppress(OSError):
            connection.close()


class CloudflareQuickTunnel:
    """Run one cloudflared Quick Tunnel and expose its temporary HTTPS URL."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        startup_timeout: float = 15.0,
        popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
        readiness_probe: Callable[[str], bool] | None = None,
    ) -> None:
        self._executable = executable
        self._startup_timeout = startup_timeout
        self._popen_factory = popen_factory or subprocess.Popen
        self._readiness_probe = readiness_probe or _https_route_ready
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._messages: queue.Queue[str | None] = queue.Queue()
        self._recent_output: deque[str] = deque(maxlen=12)
        self.public_url: str | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self, local_url: str) -> str:
        """Start a Quick Tunnel pointing at *local_url* and return its public URL."""
        if self.running and self.public_url:
            return self.public_url

        executable = self._executable or find_cloudflared()
        if not executable:
            raise TunnelUnavailableError(cloudflared_install_help())

        creationflags = 0
        if platform.system() == "Windows":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            self._process = self._popen_factory(
                [
                    executable,
                    "tunnel",
                    "--no-autoupdate",
                    "--url",
                    local_url,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
                shell=False,
            )
        except OSError as exc:
            raise TunnelStartupError(f"Could not launch cloudflared: {exc}") from exc

        if self._process.stdout is None:  # pragma: no cover - defensive
            self.stop()
            raise TunnelStartupError("cloudflared did not provide startup output.")

        self._reader = threading.Thread(
            target=self._read_output,
            args=(self._process.stdout,),
            name="cloudflared-output",
            daemon=True,
        )
        self._reader.start()

        deadline = time.monotonic() + self._startup_timeout
        discovered_url: str | None = None
        connection_ready = False
        next_probe_at = 0.0
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                message = self._messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self._process.poll() is not None:
                    break
                message = ""

            if message is None:
                if self._process.poll() is not None:
                    break
            else:
                match = _TUNNEL_URL.search(message)
                if match:
                    discovered_url = match.group(0).rstrip("/")
                if _TUNNEL_READY.search(message):
                    connection_ready = True
            now = time.monotonic()
            if discovered_url and connection_ready and now >= next_probe_at:
                if self._readiness_probe(discovered_url):
                    self.public_url = discovered_url
                    return self.public_url
                next_probe_at = now + 0.5

        exited = self._process.poll() is not None
        details = self._format_recent_output()
        self.stop()
        if discovered_url and connection_ready:
            message = (
                "Cloudflare connected, but its temporary HTTPS address did not become reachable. "
                "Wait one minute, then click Remote Phone Scanner again."
            )
        elif exited:
            message = "cloudflared exited before it created a remote scanner address."
        else:
            message = "Timed out while cloudflared was creating the remote scanner address."
        if details:
            message += f"\n\ncloudflared reported:\n{details}"
        if "config" in details.lower():
            message += (
                "\n\nQuick Tunnels cannot start while a cloudflared config file is active. "
                "Temporarily move that config file and try again."
            )
        raise TunnelStartupError(message)

    def _read_output(self, stream: TextIO) -> None:
        try:
            try:
                for line in iter(stream.readline, ""):
                    clean_line = line.strip()
                    if clean_line:
                        self._recent_output.append(clean_line)
                        self._messages.put(clean_line)
            except (OSError, ValueError):
                pass
        finally:
            self._messages.put(None)

    def _format_recent_output(self) -> str:
        return "\n".join(list(self._recent_output)[-6:])

    def stop(self) -> None:
        """Stop the tunnel process if one is running."""
        process = self._process
        if process is None:
            return

        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            else:
                process.wait()
        except (OSError, subprocess.SubprocessError):
            with suppress(OSError):
                process.kill()

        if process.stdout is not None:
            with suppress(OSError, ValueError):
                process.stdout.close()
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=1)

        self._process = None
        self._reader = None
        self.public_url = None

"""Safe lifecycle management for PartyPad's temporary Wi-Fi access point."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


AP_IP = "192.168.12.1"
HERE = Path(__file__).parent


class AccessPoint:
    """Start and stop the privileged AP helper without disrupting client Wi-Fi."""

    def __init__(self, interface: str, ssid: str, password: str):
        self.interface = interface
        self.ssid = ssid
        self.password = password
        self.process: subprocess.Popen | None = None
        self.stop_path: str | None = None

    def start(self, controller_url: str) -> None:
        required = ("iw", "hostapd", "dnsmasq", "iptables", "sysctl")
        missing = [name for name in required if shutil.which(name) is None]
        if missing:
            raise SystemExit(f"[ap] missing required command(s): {', '.join(missing)}")

        try:
            modes = subprocess.run(
                ["iw", "list"], check=True, capture_output=True, text=True
            ).stdout
        except (subprocess.CalledProcessError, FileNotFoundError) as error:
            raise SystemExit(f"[ap] could not inspect Wi-Fi capabilities: {error}") from error
        if not re.search(r"^\s*\* AP\s*$", modes, re.MULTILINE):
            raise SystemExit("[ap] this Wi-Fi device does not report AP mode support.")

        channel = self.current_channel()
        ready_path = self._unused_temp_path("partypad-ready-")
        self.stop_path = self._unused_temp_path("partypad-stop-")
        command = [
            sys.executable,
            str(HERE / "ap_helper.py"),
            self.interface,
            self.ssid,
            self.password,
            str(channel),
            ready_path,
            self.stop_path,
            str(os.getpid()),
            controller_url,
        ]
        if os.geteuid() != 0:
            pkexec = shutil.which("pkexec")
            if not pkexec:
                raise SystemExit("[ap] creating an access point requires pkexec (polkit).")
            command.insert(0, pkexec)

        print(f"[ap] starting temporary Wi-Fi {self.ssid!r} on {self.interface}")
        print(f"[ap] using the current radio channel ({channel}) to preserve client Wi-Fi")
        try:
            self.process = subprocess.Popen(command, start_new_session=True)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                returncode = self.process.poll()
                if returncode is not None:
                    self.process = None
                    self.stop_path = None
                    raise SystemExit(
                        f"[ap] access-point helper exited during startup (status {returncode})."
                    )
                if Path(ready_path).exists():
                    return
                time.sleep(0.1)
            self.stop()
            raise SystemExit("[ap] timed out waiting for the wireless network to become ready.")
        except OSError as error:
            self.process = None
            raise SystemExit(f"[ap] could not start access-point helper: {error}") from error
        finally:
            Path(ready_path).unlink(missing_ok=True)

    def current_channel(self) -> int:
        """Return the station channel, or a safe 2.4 GHz default when offline."""
        link = subprocess.run(
            ["iw", "dev", self.interface, "link"], capture_output=True, text=True
        ).stdout
        match = re.search(r"freq:\s*(\d+)", link)
        if not match:
            return 6
        frequency = int(match.group(1))
        if frequency == 2484:
            return 14
        if frequency < 2484:
            return (frequency - 2407) // 5
        return (frequency - 5000) // 5

    def stop(self) -> None:
        if self.process is None:
            return
        print("[ap] stopping temporary Wi-Fi and restoring network state")
        try:
            if self.stop_path is not None:
                Path(self.stop_path).touch()
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print("[ap] helper did not stop promptly; leaving its cleanup process running")
        finally:
            self.process = None
            self.stop_path = None

    @staticmethod
    def _unused_temp_path(prefix: str) -> str:
        with tempfile.NamedTemporaryFile(prefix=prefix) as temporary:
            path = temporary.name
        return path

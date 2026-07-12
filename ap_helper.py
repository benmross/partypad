"""Privileged, short-lived AP helper. Invoked by server.py through pkexec."""

import argparse
import fcntl
import html
import http.server
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def run(*args, check=True):
    result = subprocess.run(args, check=False, text=True, capture_output=True)
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"status {result.returncode}"
        raise RuntimeError(f"{' '.join(args)}: {detail}")
    return result


def run_retry(*args, timeout=5):
    """Retry operations that race with NetworkManager releasing a new link."""
    deadline = time.monotonic() + timeout
    while True:
        result = run(*args, check=False)
        if result.returncode == 0:
            return result
        if time.monotonic() >= deadline:
            detail = result.stderr.strip() or result.stdout.strip() or f"status {result.returncode}"
            raise RuntimeError(f"{' '.join(args)}: {detail}")
        time.sleep(0.2)


def default_route_interface(route_output: str, excluded: str) -> str | None:
    """Extract the first usable interface from `ip route show default`."""
    for line in route_output.splitlines():
        fields = line.split()
        if "dev" in fields:
            interface = fields[fields.index("dev") + 1]
            if interface != excluded:
                return interface
    return None


def deletion_rule(insertion_rule: tuple[str, ...]) -> tuple[str, ...]:
    """Convert an iptables insertion rule into its exact deletion rule."""
    rule = list(insertion_rule)
    insertion = rule.index("-I")
    rule[insertion] = "-D"
    if rule[insertion + 2] == "1":
        del rule[insertion + 2]
    return tuple(rule)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("interface")
    parser.add_argument("ssid")
    parser.add_argument("password")
    parser.add_argument("channel", type=int)
    parser.add_argument("ready_file")
    parser.add_argument("stop_file")
    parser.add_argument("server_pid", type=int)
    parser.add_argument("controller_url")
    args = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit("ap_helper must run as root")

    lock_file = open("/run/partypad-ap.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("[ap] another PartyPad access point is already starting or running")

    ap_interface = "ap0"
    children = []
    firewall_rules = []
    httpd = None
    forwarding_was = None
    stopping = False

    def stop(_signum=None, _frame=None):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    with tempfile.TemporaryDirectory(prefix="partypad-ap-") as temp_dir:
        temp = Path(temp_dir)
        hostapd_conf = temp / "hostapd.conf"
        dnsmasq_conf = temp / "dnsmasq.conf"
        try:
            if Path(f"/sys/class/net/{ap_interface}").exists():
                sys.exit(f"[ap] {ap_interface} already exists; stop the previous AP first")

            run("iw", "dev", args.interface, "interface", "add", ap_interface, "type", "__ap")
            run("nmcli", "device", "set", ap_interface, "managed", "no", check=False)
            # hostapd must configure AP mode/channel before iwlwifi will allow
            # this virtual interface up. Assigning an address while down is OK;
            # hostapd owns the later IFF_UP transition.
            run_retry("ip", "address", "add", "192.168.12.1/24", "dev", ap_interface)

            band = "a" if args.channel > 14 else "g"
            hostapd_conf.write_text(
                f"interface={ap_interface}\n"
                "driver=nl80211\n"
                f"ssid={args.ssid}\n"
                f"hw_mode={band}\n"
                f"channel={args.channel}\n"
                "wpa=2\n"
                "wpa_key_mgmt=WPA-PSK\n"
                "rsn_pairwise=CCMP\n"
                f"wpa_passphrase={args.password}\n"
            )
            dnsmasq_conf.write_text(
                f"interface={ap_interface}\n"
                "bind-interfaces\n"
                "dhcp-range=192.168.12.10,192.168.12.100,255.255.255.0,12h\n"
                "dhcp-option=3,192.168.12.1\n"
                "dhcp-option=6,192.168.12.1\n"
            )

            route_match = default_route_interface(
                run("ip", "-4", "route", "show", "default", check=False).stdout,
                ap_interface,
            )
            if route_match:
                forwarding_was = Path("/proc/sys/net/ipv4/ip_forward").read_text().strip()
                run("sysctl", "-q", "-w", "net.ipv4.ip_forward=1")
                firewall_rules = [
                    (
                        "-t",
                        "nat",
                        "-I",
                        "POSTROUTING",
                        "1",
                        "-s",
                        "192.168.12.0/24",
                        "-o",
                        route_match,
                        "-j",
                        "MASQUERADE",
                    ),
                    ("-I", "FORWARD", "1", "-i", ap_interface, "-o", route_match, "-j", "ACCEPT"),
                    (
                        "-I",
                        "FORWARD",
                        "1",
                        "-i",
                        route_match,
                        "-o",
                        ap_interface,
                        "-m",
                        "conntrack",
                        "--ctstate",
                        "RELATED,ESTABLISHED",
                        "-j",
                        "ACCEPT",
                    ),
                ]
                for rule in firewall_rules:
                    run("iptables", "-w", *rule)
                mode = f"sharing internet from {route_match}"
            else:
                with dnsmasq_conf.open("a") as conf:
                    conf.write("address=/#/192.168.12.1\n")
                mode = "offline captive portal"

            page = (
                "<!doctype html><meta name=viewport content='width=device-width'>"
                "<title>PartyPad</title><style>body{font:20px system-ui;text-align:center;"
                "padding:12vh 24px;background:#171923;color:white}a{display:inline-block;"
                "padding:18px 26px;border-radius:14px;background:#65d1ff;color:#07131a;"
                "font-weight:700;text-decoration:none}</style><h1>PartyPad</h1>"
                "<p>Wi-Fi connected. Open your controller:</p>"
                f"<a href='{html.escape(args.controller_url, quote=True)}'>Open Controller</a>"
            ).encode()

            class LandingHandler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(page)))
                    self.end_headers()
                    self.wfile.write(page)

                def log_message(self, _format, *_args):
                    pass

            httpd = http.server.ThreadingHTTPServer(("192.168.12.1", 80), LandingHandler)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()

            children.append(subprocess.Popen(["hostapd", str(hostapd_conf)]))
            children.append(
                subprocess.Popen(
                    ["dnsmasq", "--keep-in-foreground", "--conf-file=" + str(dnsmasq_conf)]
                )
            )
            time.sleep(1)
            failed = [p.returncode for p in children if p.poll() is not None]
            if failed:
                sys.exit(f"[ap] AP service failed during startup (status {failed[0]})")
            Path(args.ready_file).touch()
            print(
                f"[ap] {args.ssid!r} is ready on {ap_interface} (channel {args.channel}; {mode})",
                flush=True,
            )

            while (
                not stopping
                and not Path(args.stop_file).exists()
                and Path(f"/proc/{args.server_pid}").exists()
                and all(p.poll() is None for p in children)
            ):
                time.sleep(0.25)
        except (RuntimeError, OSError) as error:
            print(f"[ap] setup failed: {error}", file=sys.stderr, flush=True)
            sys.exit(1)
        finally:
            Path(args.ready_file).unlink(missing_ok=True)
            Path(args.stop_file).unlink(missing_ok=True)
            if httpd is not None:
                httpd.shutdown()
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
            for child in reversed(children):
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
            for rule in reversed(firewall_rules):
                run("iptables", "-w", *deletion_rule(rule), check=False)
            if forwarding_was == "0":
                run("sysctl", "-q", "-w", "net.ipv4.ip_forward=0", check=False)
            run("ip", "link", "set", ap_interface, "down", check=False)
            run("iw", "dev", ap_interface, "del", check=False)
            run("nmcli", "device", "set", args.interface, "managed", "yes", check=False)


if __name__ == "__main__":
    main()

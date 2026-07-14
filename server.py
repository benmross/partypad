"""PartyPad system selection, web controllers, and emulator backends.

    phone browser --WebSocket--> PartyPad --DSU/UDP--> Dolphin
                                     |
                                     +--uinput/evdev--> RetroArch

Systems select a controller mode and a default emulator backend through the
registry in systems.py. Phones feed canonical pad state over a WebSocket and
are assigned one of four stable player slots.

The DSU wire format below matches the reference implementation in
joaorb64/joycond-cemuhook (a well-used DSU server), verified against the
cemuhook protocol spec (https://v1993.github.io/cemuhook-protocol/).
"""

import argparse
import asyncio
import json
import math
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import time
from binascii import crc32
from pathlib import Path

from aiohttp import web, WSMsgType
import qrcode

from online_transport import OnlineHost, available_update, create_session, load_device_token
from systems import SUPPORTED_SYSTEMS, SYSTEMS, get_system

HERE = Path(__file__).parent
STATIC = HERE / "static"
CERT_DIR = HERE / "certs"

# ---------------------------------------------------------------------------
# Motion mapping (phone sensors -> DSU frame Dolphin expects)
#
# Dolphin's DSU client names the three accel floats X=Left/Right, Y=Up/Down,
# Z=Forward/Back, and the three gyro floats pitch, yaw, roll (deg/s).
# Match WiiMoteDSU's Wii Remote motion frame. The browser client first normalizes
# Android gravity polarity to the working iOS convention. For the verified
# steering pose, autorotation is locked and the phone's top edge points left:
#   DSU accel X = phone X, DSU accel Y = phone Z, DSU accel Z = -phone Y
#   DSU gyro  X =  phone beta, DSU gyro  Y = -phone alpha, DSU gyro  Z = phone gamma
# Browser DeviceMotion rotationRate alpha/beta/gamma are degrees/s.
# ---------------------------------------------------------------------------
SGN_ACCEL_X, SGN_ACCEL_Y, SGN_ACCEL_Z = 1.0, 1.0, -1.0
SGN_GYRO_PITCH, SGN_GYRO_YAW, SGN_GYRO_ROLL = 1.0, -1.0, 1.0
# The IR pointer is computed on the phone (quaternion ray projection) and arrives as px/py.

# Runtime config. In pointer-only mode we feed Dolphin a stable, level IMU (zero gyro,
# gravity straight "down") so the noisy/​roll-coupled phone IMU can't fight the IR pointer.
CONFIG = {"pointer_only": False, "gyro": False}
LEVEL_ACCEL = (0.0, -1.0, 0.0)  # DSU g: no left/right or fwd/back tilt, gravity down

# ---------------------------------------------------------------------------
# DSU / cemuhook protocol
# ---------------------------------------------------------------------------

DSU_MAGIC_SERVER = b"DSUS"
DSU_PROTOCOL_VERSION = 1001
DSU_SERVER_ID = 0xFFFFFFFF
DSU_PORT = 26760
MAX_PADS = 4
CLIENT_TIMEOUT = 5.0  # seconds since last request before we forget a Dolphin client
SLOT_RECONNECT_GRACE = 30.0
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

MSG_VERSION = bytes([0x00, 0x00, 0x10, 0x00])
MSG_PORTS = bytes([0x01, 0x00, 0x10, 0x00])
MSG_DATA = bytes([0x02, 0x00, 0x10, 0x00])

# Digital buttons we track per pad (DS4-style names, as Dolphin exposes them).
BUTTON_NAMES = (
    "cross",
    "circle",
    "square",
    "triangle",
    "l1",
    "r1",
    "l2",
    "r2",
    "l3",
    "r3",
    "share",
    "options",
    "ps",
    "dpad_up",
    "dpad_down",
    "dpad_left",
    "dpad_right",
)
CONTROLLER_INPUT_KEYS = {"t", "b", "left_x", "left_y", "px", "py", "m", "o", "aim", "rc"}
MOTION_KEYS = {"ax", "ay", "az", "ra", "rb", "rg", "orient", "accel_polarity"}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def finite_float(value):
    """Return a finite float, or None for malformed/untrusted controller input."""
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def bounded_number(value, limit):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and abs(value) <= limit
    )


def valid_controller_input(value):
    """Validate the complete untrusted phone-input schema before state changes."""
    if not isinstance(value, dict) or value.get("t") != "i":
        return False
    if any(key not in CONTROLLER_INPUT_KEYS for key in value):
        return False
    buttons = value.get("b", {})
    if not isinstance(buttons, dict) or any(
        key not in BUTTON_NAMES or not isinstance(button, bool)
        for key, button in buttons.items()
    ):
        return False
    if any(
        key in value and not bounded_number(value[key], 1.5)
        for key in ("left_x", "left_y", "px", "py")
    ):
        return False
    motion = value.get("m")
    if motion is not None:
        if not isinstance(motion, dict) or any(key not in MOTION_KEYS for key in motion):
            return False
        if any(key in motion and not bounded_number(motion[key], 50) for key in ("ax", "ay", "az")):
            return False
        if any(key in motion and not bounded_number(motion[key], 5000) for key in ("ra", "rb", "rg")):
            return False
        if "orient" in motion and not bounded_number(motion["orient"], 360):
            return False
        if "accel_polarity" in motion and (
            isinstance(motion["accel_polarity"], bool)
            or motion["accel_polarity"] not in (-1, 1)
        ):
            return False
    for key, length in (("o", 3), ("aim", 2)):
        if key in value and (
            not isinstance(value[key], list)
            or len(value[key]) != length
            or any(not bounded_number(item, 360) for item in value[key])
        ):
            return False
    return "rc" not in value or value["rc"] is True or value["rc"] == 1


def write_log(logf, slot: int, d: dict):
    """Append one diagnostic sample (raw sensors + computed aim/cursor) as JSONL."""
    o = d.get("o") or [None, None, None]
    aim = d.get("aim") or [None, None]
    m = d.get("m") or {}
    rec = {
        "t": round(time.time(), 3),
        "slot": slot,
        "a": o[0],
        "b": o[1],
        "g": o[2],  # raw deviceorientation (deg)
        "az": aim[0],
        "el": aim[1],  # client-computed aim (deg)
        "px": d.get("px"),
        "py": d.get("py"),  # client-computed cursor (-1..1)
        "ax": m.get("ax"),
        "ay": m.get("ay"),
        "az_g": m.get("az"),  # accel (g)
        "ra": m.get("ra"),
        "rb": m.get("rb"),
        "rg": m.get("rg"),  # rot rate (deg/s)
        "orient": m.get("orient"),
        "accel_polarity": m.get("accel_polarity"),
    }
    if d.get("rc"):
        rec["rc"] = 1  # recenter marker
    logf.write(json.dumps(rec) + "\n")


def build_packet(msg_type: bytes, data: bytes) -> bytes:
    """Wrap a message body in the 16-byte DSU header with a valid CRC32."""
    body = msg_type + data
    packet = bytearray()
    packet += DSU_MAGIC_SERVER  # 0:4  "DSUS"
    packet += struct.pack("<H", DSU_PROTOCOL_VERSION)  # 4:6  version 1001
    packet += struct.pack("<H", len(body))  # 6:8  length of (msgtype + data)
    packet += b"\x00\x00\x00\x00"  # 8:12 CRC32 placeholder
    packet += struct.pack("<I", DSU_SERVER_ID)  # 12:16 server id
    packet += body  # 16:  msgtype + data
    crc = crc32(packet) & 0xFFFFFFFF
    packet[8:12] = struct.pack("<I", crc)
    return bytes(packet)


class PadState:
    """Latest input state for one player slot, updated from its WebSocket."""

    def __init__(self, slot: int):
        self.slot = slot
        self.connected = False
        self.mac = bytes([0x00, 0x00, 0x00, 0x00, 0x00, slot + 1])
        self.reset()

    def reset(self):
        self.buttons = {name: False for name in BUTTON_NAMES}
        self.left_x = 0.0
        self.left_y = 0.0
        self.right_x = 0.0  # IR pointer X (mapped to Right stick in the Dolphin profile)
        self.right_y = 0.0  # IR pointer Y
        self.accel = (0.0, 0.0, 0.0)  # DSU g:   (X left/right, Y up/down, Z fwd/back)
        self.gyro = (0.0, 0.0, 0.0)  # DSU deg/s: (pitch, yaw, roll)
        self.motion_ts = 0
        self.touch_until = 0.0

    def update_from_json(self, d: dict):
        b = d.get("b")
        if isinstance(b, dict):
            for name in BUTTON_NAMES:
                if isinstance(b.get(name), bool):
                    self.buttons[name] = b[name]
        for attr in ("left_x", "left_y"):
            if attr in d:
                value = finite_float(d[attr])
                if value is not None:
                    setattr(self, attr, clamp(value, -1.0, 1.0))
        # IR pointer, already computed on the phone (quaternion ray -> px/py in -1..1)
        if "px" in d:
            value = finite_float(d["px"])
            if value is not None:
                self.right_x = clamp(value, -1.0, 1.0)
        if "py" in d:
            value = finite_float(d["py"])
            if value is not None:
                self.right_y = clamp(value, -1.0, 1.0)
        if d.get("rc"):
            self.touch_until = time.monotonic() + 0.12

        m = d.get("m")
        if isinstance(m, dict):
            self._update_motion(m)

    def _update_motion(self, m: dict):
        self.motion_ts = time.monotonic_ns() // 1000
        if CONFIG["pointer_only"]:
            # Stable, level remote so the IR pointer (px/py) isn't fought by the IMU.
            self.accel = LEVEL_ACCEL
            self.gyro = (0.0, 0.0, 0.0)
            return
        # Accelerometer (g) and rotation rate (deg/s), in the phone's frame.
        ax = finite_float(m.get("ax", 0.0)) or 0.0
        ay = finite_float(m.get("ay", 0.0)) or 0.0
        az = finite_float(m.get("az", 0.0)) or 0.0
        ra = finite_float(m.get("ra", 0.0)) or 0.0
        rb = finite_float(m.get("rb", 0.0)) or 0.0
        rg = finite_float(m.get("rg", 0.0)) or 0.0  # alpha/beta/gamma rate
        # phone frame -> DSU frame (see mapping notes at top of file)
        self.accel = tuple(
            clamp(value, -50.0, 50.0)
            for value in (SGN_ACCEL_X * ax, SGN_ACCEL_Y * az, SGN_ACCEL_Z * ay)
        )
        if CONFIG["gyro"]:
            self.gyro = tuple(
                clamp(value, -5000.0, 5000.0)
                for value in (SGN_GYRO_PITCH * rb, SGN_GYRO_YAW * ra, SGN_GYRO_ROLL * rg)
            )
        else:
            # A normal Wii Remote's steering comes from gravity/tilt. Sending the
            # phone gyro makes a steady wheel behave as if steering were movement-based.
            self.gyro = (0.0, 0.0, 0.0)

    def shared_header(self) -> bytes:
        return (
            bytes(
                [
                    self.slot & 0xFF,
                    0x02 if self.connected else 0x00,  # slot state: connected/disconnected
                    0x02,  # device model: full gyro
                    0x02,  # connection type: bluetooth
                ]
            )
            + self.mac
            + bytes([0x00])
        )  # MAC + battery(n/a)

    def data_payload(self, counter: int) -> bytes:
        b = self.buttons

        buttons1 = (
            (b["share"] << 0)
            | (b["l3"] << 1)
            | (b["r3"] << 2)
            | (b["options"] << 3)
            | (b["dpad_up"] << 4)
            | (b["dpad_right"] << 5)
            | (b["dpad_down"] << 6)
            | (b["dpad_left"] << 7)
        )
        buttons2 = (
            (b["l2"] << 0)
            | (b["r2"] << 1)
            | (b["l1"] << 2)
            | (b["r1"] << 3)
            | (b["triangle"] << 4)
            | (b["circle"] << 5)
            | (b["cross"] << 6)
            | (b["square"] << 7)
        )

        def d(v):  # digital -> analog 0/255
            return 255 if v else 0

        def stick(v):  # -1..1 -> 0..255 (128 center)
            return clamp(int(v * 127) + 128, 0, 255)

        payload = bytearray()
        payload += bytes(
            [
                0x01,  # connected/active
            ]
        )
        payload += struct.pack("<I", counter)  # packet number
        payload += bytes(
            [
                buttons1,
                buttons2,
                255 if b["ps"] else 0,  # HOME/PS
                0x01 if time.monotonic() < self.touch_until else 0x00,  # Touch/Recenter button
                stick(self.left_x),
                stick(self.left_y),
                stick(self.right_x),
                stick(self.right_y),
                d(b["dpad_left"]),
                d(b["dpad_down"]),
                d(b["dpad_right"]),
                d(b["dpad_up"]),
                d(b["square"]),
                d(b["cross"]),
                d(b["circle"]),
                d(b["triangle"]),
                d(b["r1"]),
                d(b["l1"]),
                d(b["r2"]),
                d(b["l2"]),
                # touch pad 1 + 2 (unused): active,id,x(u16),y(u16) x2
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
                0x00,
            ]
        )
        payload += struct.pack("<Q", self.motion_ts)  # motion timestamp (us)
        payload += struct.pack(
            "<ffffff",  # accel(g) + gyro(deg/s)
            self.accel[0],
            self.accel[1],
            self.accel[2],
            self.gyro[0],
            self.gyro[1],
            self.gyro[2],
        )
        return bytes(payload)


class DSUServer(asyncio.DatagramProtocol):
    """Speaks DSU to Dolphin (the client) on 127.0.0.1:26760."""

    def __init__(self, pads):
        self.pads = pads  # list[PadState]
        self.transport = None
        self.clients = {}  # address -> {"ts": float, "slots": set[int]}
        self.counter = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, message, address):
        if len(message) < 20:
            return
        msg_type = message[16:20]
        if msg_type == MSG_VERSION:
            return  # Dolphin doesn't require a version reply
        elif msg_type == MSG_PORTS:
            self._on_ports_request(message, address)
        elif msg_type == MSG_DATA:
            self._on_data_request(message, address)

    def _on_ports_request(self, message, address):
        if len(message) < 24:
            return
        count = struct.unpack("<I", message[20:24])[0]
        for slot in message[24 : 24 + count]:
            if 0 <= slot < MAX_PADS:
                data = self.pads[slot].shared_header() + bytes([0x00])
                self.transport.sendto(build_packet(MSG_PORTS, data), address)

    def _on_data_request(self, message, address):
        reg_id = message[20]
        slot_id = message[21]
        client = self.clients.setdefault(address, {"ts": 0.0, "slots": set()})
        client["ts"] = time.time()
        if reg_id == 0:  # subscribe to all slots
            client["slots"] = set(range(MAX_PADS))
        elif reg_id == 1:  # subscribe to one slot
            client["slots"].add(slot_id)
        # reg_id == 2 (MAC-based) is unused here

    def broadcast_tick(self):
        """Send one data packet per (connected slot x subscribed client)."""
        if not self.clients:
            return
        now = time.time()
        for address in list(self.clients):
            if now - self.clients[address]["ts"] > CLIENT_TIMEOUT:
                del self.clients[address]

        self.counter += 1
        for pad in self.pads:
            if not pad.connected:
                continue
            packet = build_packet(MSG_DATA, pad.shared_header() + pad.data_payload(self.counter))
            for address, client in self.clients.items():
                if pad.slot in client["slots"]:
                    self.transport.sendto(packet, address)


# ---------------------------------------------------------------------------
# Web server: serves the controller page + WebSocket, assigns slots
# ---------------------------------------------------------------------------


class Hub:
    def __init__(self):
        self.pads = [PadState(i) for i in range(MAX_PADS)]
        self.connection_by_slot = {}  # slot -> transport-specific connection token
        self.client_by_slot = {}  # slot -> durable browser controller id
        self.reservations = {}  # controller id -> (slot, monotonic expiry)

    def claim_slot(self, client_id, now=None):
        now = time.monotonic() if now is None else now
        for slot, owner_id in self.client_by_slot.items():
            if owner_id == client_id:
                self.pads[slot].reset()
                return slot

        reservation = self.reservations.get(client_id)
        if reservation is not None:
            slot, expiry = reservation
            if expiry > now and not self.pads[slot].connected:
                self.pads[slot].connected = True
                self.pads[slot].reset()
                self.client_by_slot[slot] = client_id
                del self.reservations[client_id]
                return slot
            if expiry <= now:
                del self.reservations[client_id]

        reserved_slots = {
            slot for reserved_id, (slot, expiry) in list(self.reservations.items()) if expiry > now
        }
        self.reservations = {
            reserved_id: value for reserved_id, value in self.reservations.items() if value[1] > now
        }
        for i, pad in enumerate(self.pads):
            if not pad.connected and i not in reserved_slots:
                pad.connected = True
                pad.reset()
                self.client_by_slot[i] = client_id
                return i
        return None

    def release_slot(self, slot, client_id, connection, now=None):
        # A page reload can establish its replacement socket before the old
        # handler exits. Only the current socket is allowed to release the slot.
        if self.connection_by_slot.get(slot) is not connection:
            return False
        now = time.monotonic() if now is None else now
        self.pads[slot].connected = False
        self.pads[slot].reset()
        self.connection_by_slot.pop(slot, None)
        self.client_by_slot.pop(slot, None)
        self.reservations[client_id] = (slot, now + SLOT_RECONNECT_GRACE)
        return True


async def ws_handler(request):
    hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16_384)
    await ws.prepare(request)

    client_id = request.query.get("client", "")
    if not CLIENT_ID_RE.fullmatch(client_id):
        await ws.send_json({"t": "invalid_client"})
        await ws.close()
        return ws

    slot = hub.claim_slot(client_id)
    if slot is None:
        await ws.send_json({"t": "full"})
        await ws.close()
        return ws

    previous_ws = hub.connection_by_slot.get(slot)
    hub.connection_by_slot[slot] = ws
    if previous_ws is not None and previous_ws is not ws:
        await previous_ws.close()
    await ws.send_json(
        {
            "t": "welcome",
            "slot": slot,
            "player": slot + 1,
            "system": request.app["system"].id,
            "controller_mode": request.app["system"].controller_mode,
        }
    )
    print(f"[ws] player {slot + 1} connected")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    d = json.loads(msg.data)
                except ValueError:
                    continue
                if valid_controller_input(d):
                    hub.pads[slot].update_from_json(d)
                    logf = request.app.get("log")
                    if logf is not None:
                        write_log(logf, slot, d)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        if hub.release_slot(slot, client_id, ws):
            uinput = request.app.get("uinput")
            if uinput is not None:
                uinput.neutralize(slot)
            print(f"[ws] player {slot + 1} disconnected")
    return ws


async def index_handler(request):
    return web.FileResponse(STATIC / "index.html")


async def config_handler(request):
    system = request.app["system"]
    return web.json_response(
        {
            "system": system.id,
            "system_name": system.label,
            "controller_mode": system.controller_mode,
            "backend": request.app["backend"],
        }
    )


async def start_background(app):
    loop = asyncio.get_running_loop()
    hub = app["hub"]
    protocol = None
    if app["backend"] in ("dolphin", "both"):
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: DSUServer(hub.pads),
            local_addr=("127.0.0.1", DSU_PORT),
        )
        app["dsu_transport"] = transport
        app["dsu"] = protocol
        print(f"[dsu] serving on 127.0.0.1:{DSU_PORT} (point Dolphin here)")

    if app["backend"] in ("retroarch", "both"):
        if sys.platform != "linux":
            raise RuntimeError("the experimental RetroArch/uinput backend is available only on Linux")
        from uinput_backend import UInputBackend

        try:
            app["uinput"] = UInputBackend(MAX_PADS)
        except PermissionError as exc:
            raise RuntimeError(
                "cannot open /dev/uinput; grant this user uinput access (see README)"
            ) from exc
        print(f"[uinput] {MAX_PADS} stable PartyPad controllers ready for RetroArch")

    async def broadcaster():
        # 60 Hz output loop; reads latest pad state each tick.
        try:
            while True:
                if protocol is not None:
                    protocol.broadcast_tick()
                if app.get("uinput") is not None:
                    app["uinput"].update(hub.pads)
                await asyncio.sleep(1 / 60)
        except asyncio.CancelledError:
            pass

    app["broadcaster"] = loop.create_task(broadcaster())
    if app.get("online_session") is not None:
        app["online_host"] = OnlineHost(app, app["online_session"])
        app["online_task"] = loop.create_task(app["online_host"].run())


async def cleanup_background(app):
    if app.get("online_host") is not None:
        await app["online_host"].close()
    online_task = app.get("online_task")
    if online_task is not None:
        online_task.cancel()
        await asyncio.gather(online_task, return_exceptions=True)
    broadcaster = app.get("broadcaster")
    if broadcaster is not None:
        broadcaster.cancel()
        await asyncio.gather(broadcaster, return_exceptions=True)
    if app.get("dsu_transport") is not None:
        app["dsu_transport"].close()
    if app.get("uinput") is not None:
        app["uinput"].close()
    if app.get("log"):
        app["log"].close()


def get_lan_ip() -> str:
    """Best-effort local IP (the address phones should reach)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packets actually sent for UDP connect
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def wifi_qr_payload(ssid: str, password: str) -> str:
    """Build the de-facto ZXing Wi-Fi QR payload used by iOS and Android."""

    def escape(value: str) -> str:
        return "".join("\\" + char if char in r"\\;,:" else char for char in value)

    return f"WIFI:T:WPA;S:{escape(ssid)};P:{escape(password)};;"


def write_runtime_state(app) -> None:
    """Atomically expose bounded loopback-dashboard state to a parent process."""
    path = app.get("state_file")
    session = app.get("online_session")
    if path is None or session is None:
        return
    host = app.get("online_host")
    players = []
    if host is not None:
        players = [
            {
                "player": peer.slot + 1,
                "path": peer.path,
                "rtt_ms": peer.rtt_ms,
            }
            for peer in sorted(host.peers.values(), key=lambda item: item.slot)
        ]
    state = {
        "join_url": session["join_url"],
        "end_url": session["end_url"],
        "host_secret": session["host_secret"],
        "expires_at": session.get("expires_at"),
        "players": players,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(state, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_cert(ip: str, regen: bool = False):
    """Return (cert, key) paths, generating a self-signed cert for `ip` if needed.

    Motion sensors require a secure context (HTTPS) on iOS/Android. The cert need
    not be trusted — the user taps through the warning once — but we put the LAN IP
    in the SAN to keep that warning minimal.
    """
    cert, key, stamp = CERT_DIR / "cert.pem", CERT_DIR / "key.pem", CERT_DIR / "ip.txt"
    fresh = cert.exists() and key.exists() and stamp.exists() and stamp.read_text().strip() == ip
    if fresh and not regen:
        return cert, key
    CERT_DIR.mkdir(exist_ok=True)
    print(f"[tls] generating self-signed cert for {ip} …")
    try:
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "3650",
                "-subj",
                "/CN=partypad",
                "-addext",
                f"subjectAltName=IP:{ip},IP:127.0.0.1,DNS:localhost",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        sys.exit(f"[tls] openssl failed ({detail.strip()}). Install openssl or pass --http.")
    stamp.write_text(ip)
    return cert, key


def build_parser():
    parser = argparse.ArgumentParser(description="partypad web controller bridge")
    parser.add_argument(
        "--system",
        choices=tuple(SYSTEMS),
        metavar="SYSTEM",
        help="system being played; bypasses the interactive setup",
    )
    parser.add_argument(
        "--backend",
        choices=("dolphin", "retroarch", "both"),
        default=None,
        help="override the system's emulator backend",
    )
    parser.add_argument(
        "--profile",
        choices=("wii", "nes"),
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--host", default=None, help="web bind address")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="local web port (default: 8080; online mode uses an ephemeral loopback port)",
    )
    parser.add_argument(
        "--ip", default=None, help="LAN IP to advertise in the QR (default: auto-detect)"
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve plain HTTP (no motion sensors on most mobile browsers)",
    )
    parser.add_argument("--regen-cert", action="store_true", help="force a new TLS cert")
    parser.add_argument(
        "--online",
        action="store_true",
        help="accept controllers through the public PartyPad service from any network",
    )
    parser.add_argument(
        "--service-url",
        default=os.environ.get("PARTYPAD_SERVICE_URL", "https://partypad.benmross.com"),
        help="online session service (default: https://partypad.benmross.com)",
    )
    parser.add_argument("--state-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--log", action="store_true", help="write a diagnostic motion log to logs/*.jsonl"
    )
    parser.add_argument(
        "--pointer-only",
        action="store_true",
        help="send IR pointer normally, but hold IMU level and zero gyro",
    )
    parser.add_argument(
        "--gyro",
        action="store_true",
        help="forward phone gyro data (for MotionPlus-specific testing)",
    )
    parser.add_argument(
        "--ap", action="store_true", help="create a temporary local Wi-Fi access point"
    )
    parser.add_argument(
        "--ap-interface", default="wlan0", help="wireless interface for --ap (default: wlan0)"
    )
    parser.add_argument(
        "--ap-name", default="PartyPad", help="Wi-Fi network name for --ap (default: PartyPad)"
    )
    parser.add_argument(
        "--ap-password", default="partypad", help="Wi-Fi password for --ap (default: partypad)"
    )
    return parser


def choose_system(input_func=input, output=print):
    """Prompt for one of the systems whose controller support is implemented."""
    output("PartyPad setup — choose the system being played:")
    for index, system in enumerate(SUPPORTED_SYSTEMS, 1):
        output(f"  {index}. {system.label} [{system.id}] — {system.detail}")
    while True:
        try:
            answer = input_func("System: ").strip().lower()
        except EOFError as exc:
            raise ValueError("no interactive input; pass --system nes or --system wii") from exc
        for index, system in enumerate(SUPPORTED_SYSTEMS, 1):
            if answer in (str(index), system.id):
                return system
        output("Enter a listed number or system name.")


def resolve_system(args, input_func=input, output=print):
    """Resolve new system selection and the old backend/profile shorthand."""
    if args.system:
        system = get_system(args.system)
    elif args.profile:
        system = get_system(args.profile)
    elif args.backend in ("dolphin", "retroarch"):
        system = get_system("wii" if args.backend == "dolphin" else "nes")
    else:
        system = choose_system(input_func, output)

    if not system.supported:
        raise ValueError(
            f"system '{system.id}' is registered for future support but has no controller mode yet"
        )
    if args.profile and args.profile != system.controller_mode:
        raise ValueError(
            f"--profile {args.profile} conflicts with --system {system.id}; omit --profile"
        )
    backend = args.backend or system.backend
    if backend != "both" and backend != system.backend:
        raise ValueError(
            f"system '{system.id}' requires the {system.backend} backend (or --backend both)"
        )
    return system, backend


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        system, backend = resolve_system(args)
    except ValueError as exc:
        parser.error(str(exc))
    if args.ap and not 8 <= len(args.ap_password) <= 63:
        parser.error("--ap-password must be 8 to 63 characters")
    if args.ap and (not 1 <= len(args.ap_name.encode("utf-8")) <= 32 or "\n" in args.ap_name):
        parser.error("--ap-name must be 1 to 32 UTF-8 bytes without newlines")
    if args.ap and ("\n" in args.ap_password or not args.ap_password.isascii()):
        parser.error("--ap-password must contain ASCII characters without newlines")
    if args.ap and args.online:
        parser.error("--ap and --online are separate connection modes; choose one")
    CONFIG["pointer_only"] = args.pointer_only
    CONFIG["gyro"] = args.gyro
    port = args.port if args.port is not None else (0 if args.online else 8080)

    app = web.Application()
    app["hub"] = Hub()
    app["backend"] = backend
    app["system"] = system
    app["uinput"] = None
    app["log"] = None
    app["online_session"] = None
    app["state_file"] = args.state_file
    app["write_state"] = lambda: write_runtime_state(app)
    if args.log:
        log_dir = HERE / "logs"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / f"motion-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        app["log"] = open(log_path, "w", buffering=1)  # line-buffered
        print(f"[log] motion log: {log_path}")
    app.router.add_get("/", index_handler)
    app.router.add_get("/config", config_handler)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", STATIC)
    app.on_startup.append(start_background)
    app.on_cleanup.append(cleanup_background)

    access_point = None
    ap_ip = None
    if args.ap:
        if sys.platform != "linux":
            parser.error("--ap is available only on Linux")
        from hotspot import AP_IP, AccessPoint

        access_point = AccessPoint(args.ap_interface, args.ap_name, args.ap_password)
        ap_ip = AP_IP

    lan_ip = args.ip or ("127.0.0.1" if args.online else ap_ip if args.ap else get_lan_ip())

    if args.online:
        update = available_update(args.service_url)
        if update:
            print(f"[update] PartyPad {update[0]} is available: {update[1]}")
        device_token = load_device_token()
        if not device_token:
            parser.error("--online requires device authorization; run `python setup_online.py`")
        try:
            app["online_session"] = create_session(
                args.service_url,
                device_token,
                system=system.id,
                system_name=system.label,
                controller_mode=system.controller_mode,
                backend=backend,
            )
            write_runtime_state(app)
        except RuntimeError as exc:
            parser.error(str(exc))

    ssl_context = None
    if not args.http and not args.online:
        cert, key = ensure_cert(lan_ip, regen=args.regen_cert)
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(cert, key)
    scheme = "http" if args.http or args.online else "https"
    url = (
        app["online_session"]["join_url"]
        if app["online_session"] is not None
        else f"{scheme}://{lan_ip}:{port}/"
    )

    try:
        if access_point is not None:
            access_point.start(url)

        print("\n" + "=" * 44)
        print(f"  partypad — {system.label} via {backend}")
        print("=" * 44)
        if args.ap:
            print("  1. Join PartyPad Wi-Fi")
            wifi_qr = qrcode.QRCode(border=1)
            wifi_qr.add_data(wifi_qr_payload(args.ap_name, args.ap_password))
            wifi_qr.make()
            wifi_qr.print_ascii(invert=True)
            print(f"  Wi-Fi: {args.ap_name}  password: {args.ap_password}\n")
            print("  2. Open the controller")
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(invert=True)
        print(f"  {url}")
        if args.online:
            print("  (online session: phones may use Wi-Fi or cellular data)")
        elif not args.http:
            print("  (tap through the one-time certificate warning on each phone)")
        print()

        bind_host = args.host or ("127.0.0.1" if args.online else "0.0.0.0")
        web.run_app(app, host=bind_host, port=port, ssl_context=ssl_context, print=None)
    finally:
        if access_point is not None:
            access_point.stop()
        if args.state_file is not None:
            args.state_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

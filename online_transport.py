"""Outbound PartyPad transport for controllers on arbitrary networks.

The public service only coordinates sessions and relays as a fallback.  Each
phone also negotiates a WebRTC DataChannel directly with this process; ICE may
select a direct route or a managed TURN relay without opening an inbound port.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from aiohttp import ClientSession, WSMsgType

from device_auth import CLIENT_VERSION, PROTOCOL_VERSION, default_credential_store


def load_device_token() -> str | None:
    token = os.environ.get("PARTYPAD_DEVICE_TOKEN")
    if token:
        return token.strip()
    try:
        credential = default_credential_store().load()
    except RuntimeError:
        return None
    return credential.token if credential else None


def available_update(service_url: str, current: str = CLIENT_VERSION) -> tuple[str, str] | None:
    """Return `(latest_version, download_url)` without making startup depend on it."""
    request = urllib.request.Request(
        service_url.rstrip("/") + "/config",
        headers={"Accept": "application/json", "User-Agent": f"PartyPad/{current}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.load(response)
        latest = result.get("latest_client_version")
        download = result.get("download_url")
        current_parts = tuple(int(part) for part in current.split("."))
        latest_parts = tuple(int(part) for part in latest.split("."))
    except (OSError, ValueError, TypeError, AttributeError, urllib.error.URLError):
        return None
    if latest_parts > current_parts and isinstance(download, str):
        return latest, download
    return None


def create_session(
    service_url: str,
    device_token: str,
    *,
    system: str,
    system_name: str,
    controller_mode: str,
    backend: str,
) -> dict[str, Any]:
    """Create one short-lived public session before aiohttp owns the event loop."""
    body = json.dumps(
        {
            "system": system,
            "system_name": system_name,
            "controller_mode": controller_mode,
            "backend": backend,
        }
    ).encode()
    request = urllib.request.Request(
        service_url.rstrip("/") + "/api/sessions",
        data=body,
        headers={
            "Authorization": f"Device {device_token}",
            "Content-Type": "application/json",
            "User-Agent": f"PartyPad/{CLIENT_VERSION}",
            "X-PartyPad-Protocol": str(PROTOCOL_VERSION),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        raise RuntimeError(f"session service returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"cannot reach PartyPad session service: {exc}") from exc
    required = ("id", "host_secret", "join_url", "ws_url", "end_url", "ice_servers")
    if not isinstance(result, dict) or any(key not in result for key in required):
        raise RuntimeError("session service returned an incomplete response")
    rotated = result.get("rotated_device_token")
    rotated_expiry = result.get("device_token_expires_at")
    if rotated is not None or rotated_expiry is not None:
        if not isinstance(rotated, str) or not isinstance(rotated_expiry, str):
            raise RuntimeError("session service returned an invalid rotated credential")
        store = default_credential_store()
        credential = store.load()
        if credential is not None and credential.token == device_token:
            from device_auth import DeviceCredential

            store.save(
                DeviceCredential(
                    token=rotated,
                    device_id=credential.device_id,
                    device_name=credential.device_name,
                    expires_at=rotated_expiry,
                )
            )
    return result


@dataclass(eq=False)
class OnlinePeer:
    id: str
    client_id: str
    slot: int
    pc: Any = None
    last_sequence: int = -1
    pending_candidates: list[Any] = field(default_factory=list)
    path: str = "websocket"
    rtt_ms: int | None = None
    ice_attempt: int = 0


def ice_server_attempts(servers: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Expand Cloudflare URLs into ordered one-TURN configurations for aiortc."""
    stun: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    for server in servers:
        urls = server.get("urls", [])
        urls = [urls] if isinstance(urls, str) else urls if isinstance(urls, list) else []
        for url in urls:
            if not isinstance(url, str):
                continue
            item = {"urls": [url]}
            if isinstance(server.get("username"), str):
                item["username"] = server["username"]
            if isinstance(server.get("credential"), str):
                item["credential"] = server["credential"]
            if url.startswith("stun:"):
                stun.append(item)
            elif url.startswith(("turn:", "turns:")):
                turns.append(item)

    def priority(item: dict[str, Any]) -> int:
        url = item["urls"][0].lower()
        if url.startswith("turns:"):
            return 2
        if "transport=tcp" in url:
            return 1
        return 0

    turns.sort(key=priority)
    base = stun[:1]
    return [[*base, turn] for turn in turns] or [base]


class OnlineHost:
    """Maintain signaling, relay fallback, and WebRTC peers for one session."""

    def __init__(self, app, session: dict[str, Any]):
        self.app = app
        self.session = session
        self.ws = None
        self.peers: dict[str, OnlinePeer] = {}
        self.negotiation_tasks: dict[str, asyncio.Task] = {}
        self.stopping = False

    def _notify_state(self):
        callback = self.app.get("write_state")
        if callback is not None:
            callback()

    async def run(self):
        delay = 1.0
        async with ClientSession() as client:
            while not self.stopping:
                try:
                    ssl_context = (
                        ssl.create_default_context()
                        if urlparse(self.session["ws_url"]).scheme == "wss"
                        else None
                    )
                    async with client.ws_connect(
                        self.session["ws_url"], heartbeat=20, ssl=ssl_context
                    ) as ws:
                        self.ws = ws
                        await ws.send_json(
                            {
                                "t": "auth",
                                "role": "host",
                                "secret": self.session["host_secret"],
                                "protocol": PROTOCOL_VERSION,
                            }
                        )
                        delay = 1.0
                        async for msg in ws:
                            if msg.type == WSMsgType.TEXT:
                                try:
                                    message = json.loads(msg.data)
                                except ValueError:
                                    continue
                                if isinstance(message, dict):
                                    await self._on_message(message)
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                                break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self.stopping:
                        print(f"[online] signaling disconnected ({exc}); retrying")
                finally:
                    self.ws = None
                if not self.stopping:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 15.0)

    async def close(self):
        self.stopping = True
        if self.ws is not None:
            await self.ws.close()
        for task in self.negotiation_tasks.values():
            task.cancel()
        await asyncio.gather(*self.negotiation_tasks.values(), return_exceptions=True)
        self.negotiation_tasks.clear()
        for peer in list(self.peers.values()):
            await self._remove_peer(peer.id)
        try:
            async with ClientSession() as client:
                response = await client.delete(
                    self.session["end_url"],
                    json={"secret": self.session["host_secret"]},
                    ssl=(
                        ssl.create_default_context()
                        if urlparse(self.session["end_url"]).scheme == "https"
                        else None
                    ),
                    timeout=5,
                )
                if response.status >= 400:
                    print(f"[online] session revocation returned HTTP {response.status}")
        except Exception as exc:
            print(f"[online] could not revoke session during cleanup: {exc}")

    async def _send(self, message: dict[str, Any]):
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_json(message)

    async def _on_message(self, message: dict[str, Any]):
        kind = message.get("t")
        if kind == "auth_ok":
            print("[online] signaling connected")
        elif kind == "peers":
            current = {item["peer"] for item in message.get("peers", [])}
            for peer_id in set(self.peers) - current:
                await self._remove_peer(peer_id)
            for item in message.get("peers", []):
                await self._add_peer(item.get("peer", ""), item.get("client", ""))
        elif kind == "peer_join":
            await self._add_peer(message.get("peer", ""), message.get("client", ""))
        elif kind == "peer_leave":
            await self._remove_peer(message.get("peer", ""))
        elif kind == "offer":
            peer_id = message.get("peer", "")
            previous = self.negotiation_tasks.pop(peer_id, None)
            if previous is not None:
                previous.cancel()
            task = asyncio.create_task(self._handle_offer(peer_id, message.get("sdp", "")))
            self.negotiation_tasks[peer_id] = task
            task.add_done_callback(lambda done, key=peer_id: self._negotiation_done(key, done))
        elif kind == "candidate":
            await self._handle_candidate(message.get("peer", ""), message.get("candidate"))
        elif kind == "input":
            self._handle_input(message.get("peer", ""), message.get("seq"), message.get("data"))
        elif kind == "diagnostic":
            peer = self.peers.get(message.get("peer", ""))
            if peer is not None:
                if message.get("name") == "transport" and isinstance(message.get("dimension"), str):
                    peer.path = message["dimension"] or "webrtc"
                if message.get("name") == "rtt_ms" and isinstance(message.get("value"), (int, float)):
                    peer.rtt_ms = max(0, min(round(message["value"]), 60_000))
                self._notify_state()
        elif kind == "expired":
            self.stopping = True
            print("[online] session expired; restart PartyPad to create a new one")
            if self.ws is not None:
                await self.ws.close()

    def _negotiation_done(self, peer_id: str, task: asyncio.Task):
        if self.negotiation_tasks.get(peer_id) is task:
            self.negotiation_tasks.pop(peer_id, None)
        if not task.cancelled() and task.exception() is not None:
            print(f"[online] WebRTC task failed: {task.exception()}")

    async def _add_peer(self, peer_id: str, client_id: str):
        if not peer_id or not client_id:
            return
        existing = self.peers.get(peer_id)
        if existing is not None and existing.client_id == client_id:
            await self._welcome(existing)
            return
        hub = self.app["hub"]
        slot = hub.claim_slot(client_id)
        if slot is None:
            await self._send({"t": "control", "peer": peer_id, "message": {"t": "full"}})
            return
        peer = OnlinePeer(peer_id, client_id, slot)
        previous = hub.connection_by_slot.get(slot)
        hub.connection_by_slot[slot] = peer
        self.peers[peer_id] = peer
        if previous is not None and previous is not peer and hasattr(previous, "close"):
            result = previous.close()
            if asyncio.iscoroutine(result):
                await result
        await self._welcome(peer)
        self._notify_state()
        print(f"[online] player {slot + 1} joined session")

    async def _welcome(self, peer: OnlinePeer):
        system = self.app["system"]
        await self._send(
            {
                "t": "control",
                "peer": peer.id,
                "message": {
                    "t": "welcome",
                    "slot": peer.slot,
                    "player": peer.slot + 1,
                    "system": system.id,
                    "controller_mode": system.controller_mode,
                },
            }
        )

    async def _remove_peer(self, peer_id: str):
        peer = self.peers.pop(peer_id, None)
        if peer is None:
            return
        task = self.negotiation_tasks.pop(peer_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if peer.pc is not None:
            await peer.pc.close()
        hub = self.app["hub"]
        if hub.release_slot(peer.slot, peer.client_id, peer):
            uinput = self.app.get("uinput")
            if uinput is not None:
                uinput.neutralize(peer.slot)
            print(f"[online] player {peer.slot + 1} disconnected")
        self._notify_state()

    def _handle_input(self, peer_id: str, sequence: Any, data: Any):
        peer = self.peers.get(peer_id)
        if (
            peer is None
            or not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or not 0 <= sequence <= 2**53 - 1
            or not isinstance(data, dict)
        ):
            return
        if sequence <= peer.last_sequence:
            return
        from server import valid_controller_input

        if valid_controller_input(data):
            try:
                self.app["hub"].pads[peer.slot].update_from_json(data)
            except (TypeError, ValueError, OverflowError):
                return
            peer.last_sequence = sequence
            logf = self.app.get("log")
            if logf is not None:
                from server import write_log

                write_log(logf, peer.slot, data)

    async def _handle_offer(self, peer_id: str, sdp: str):
        peer = self.peers.get(peer_id)
        if peer is None or not sdp or len(sdp) > 128_000:
            return
        started = time.monotonic()
        print(f"[online] negotiating WebRTC for player {peer.slot + 1}")
        try:
            from aiortc import (
                RTCConfiguration,
                RTCIceServer,
                RTCPeerConnection,
                RTCSessionDescription,
            )
        except ImportError:
            await self._send(
                {
                    "t": "control",
                    "peer": peer_id,
                    "message": {"t": "transport", "path": "relay", "detail": "WebRTC unavailable"},
                }
            )
            return

        if peer.pc is not None:
            await peer.pc.close()
        attempts = ice_server_attempts(self.session["ice_servers"])
        selected = attempts[min(peer.ice_attempt, len(attempts) - 1)]
        ice_servers = [
            RTCIceServer(
                urls=item.get("urls", []),
                username=item.get("username"),
                credential=item.get("credential"),
            )
            for item in selected
        ]
        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        peer.pc = pc
        pending = peer.pending_candidates
        peer.pending_candidates = []
        for candidate in pending:
            await self._add_candidate(pc, candidate)

        @pc.on("datachannel")
        def on_datachannel(channel):
            if channel.label != "input":
                channel.close()
                return

            @channel.on("message")
            def on_data(message):
                try:
                    decoded = json.loads(message) if isinstance(message, str) else None
                except ValueError:
                    return
                if isinstance(decoded, dict):
                    self._handle_input(peer_id, decoded.get("seq"), decoded.get("data"))

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            if pc.connectionState in ("failed", "closed") and peer.pc is pc:
                await self._send(
                    {
                        "t": "control",
                        "peer": peer_id,
                        "message": {"t": "transport", "path": "relay"},
                    }
                )
                if pc.connectionState == "failed" and peer.ice_attempt + 1 < len(attempts):
                    peer.ice_attempt += 1
                    await self._send(
                        {
                            "t": "control",
                            "peer": peer_id,
                            "message": {"t": "ice_restart", "attempt": peer.ice_attempt + 1},
                        }
                    )

        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            await self._send({"t": "answer", "peer": peer_id, "sdp": pc.localDescription.sdp})
            elapsed = time.monotonic() - started
            print(f"[online] WebRTC answer sent for player {peer.slot + 1} ({elapsed:.1f}s)")
        except Exception as exc:
            await pc.close()
            if peer.pc is pc:
                peer.pc = None
            print(f"[online] WebRTC negotiation failed for player {peer.slot + 1}: {exc}")

    async def _handle_candidate(self, peer_id: str, value: Any):
        peer = self.peers.get(peer_id)
        if peer is None or (value is not None and not isinstance(value, dict)):
            return
        if peer.pc is None:
            if len(peer.pending_candidates) < 64:
                peer.pending_candidates.append(value)
            return
        await self._add_candidate(peer.pc, value)

    @staticmethod
    async def _add_candidate(pc, value: Any):
        if value is None:
            await pc.addIceCandidate(None)
            return
        candidate_sdp = value.get("candidate")
        if not isinstance(candidate_sdp, str) or not candidate_sdp.startswith("candidate:"):
            return
        if len(candidate_sdp) > 4096:
            return
        from aiortc.sdp import candidate_from_sdp

        try:
            candidate = candidate_from_sdp(candidate_sdp.split(":", 1)[1])
        except ValueError:
            return
        sdp_mid = value.get("sdpMid")
        sdp_mline_index = value.get("sdpMLineIndex")
        candidate.sdpMid = sdp_mid if isinstance(sdp_mid, str) else None
        candidate.sdpMLineIndex = (
            sdp_mline_index
            if isinstance(sdp_mline_index, int) and not isinstance(sdp_mline_index, bool)
            else None
        )
        if candidate.sdpMid is None and candidate.sdpMLineIndex is None:
            return
        try:
            await pc.addIceCandidate(candidate)
        except (ValueError, RuntimeError):
            return

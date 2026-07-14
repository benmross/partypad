#!/usr/bin/env python3
"""Opt-in real-browser smoke for signaling, WebRTC input, and neutralization."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import WSMsgType, web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

from online_transport import OnlineHost


STATIC = ROOT / "static"


async def make_app(received: list[dict]) -> tuple[web.Application, list[RTCPeerConnection]]:
    app = web.Application()
    peers: list[RTCPeerConnection] = []

    async def index(_request):
        return web.FileResponse(STATIC / "index.html")

    async def signaling(request):
        ws = web.WebSocketResponse(max_msg_size=128_000)
        await ws.prepare(request)
        await ws.send_json({"t": "hello"})
        pc: RTCPeerConnection | None = None
        pending: list[dict | None] = []
        offer_task: asyncio.Task | None = None

        async def negotiate(sdp: str) -> None:
            assert pc is not None
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            for candidate in pending:
                await OnlineHost._add_candidate(pc, candidate)
            pending.clear()
            await pc.setLocalDescription(await pc.createAnswer())
            await ws.send_json({"t": "answer", "sdp": pc.localDescription.sdp})

        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    value = json.loads(message.data)
                except ValueError:
                    continue
                if not isinstance(value, dict):
                    continue
                if value.get("t") == "auth":
                    await ws.send_json({
                        "t": "auth_ok",
                        "config": {
                            "system": "wii", "system_name": "Wii",
                            "controller_mode": "wii", "backend": "dolphin",
                        },
                        "ice_servers": [],
                    })
                elif value.get("t") == "offer" and isinstance(value.get("sdp"), str):
                    if pc is not None:
                        await pc.close()
                    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                    peers.append(pc)

                    @pc.on("datachannel")
                    def on_datachannel(channel):
                        @channel.on("message")
                        def on_message(payload):
                            try:
                                envelope = json.loads(payload)
                            except (TypeError, ValueError):
                                return
                            if isinstance(envelope, dict):
                                received.append(envelope)

                    offer_task = asyncio.create_task(negotiate(value["sdp"]))
                    offer_task.add_done_callback(
                        lambda task: print(f"WebRTC negotiation failed: {task.exception()}")
                        if not task.cancelled() and task.exception() is not None else None
                    )
                elif value.get("t") == "candidate":
                    candidate = value.get("candidate")
                    if pc is None or pc.remoteDescription is None:
                        if len(pending) < 64 and (candidate is None or isinstance(candidate, dict)):
                            pending.append(candidate)
                    else:
                        await OnlineHost._add_candidate(pc, candidate)
                elif value.get("t") == "input":
                    received.append({"relay": True, **value})
        finally:
            if offer_task is not None:
                await asyncio.gather(offer_task, return_exceptions=True)
        return ws

    app.router.add_get("/", index)
    app.router.add_static("/static", STATIC)
    app.router.add_get("/api/sessions/{session}/ws", signaling)
    return app, peers


def run_firefox(url: str, received: list[dict], timeout: float) -> None:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service
    from selenium.webdriver.support.ui import WebDriverWait

    options = Options()
    options.add_argument("-headless")
    options.set_preference("media.peerconnection.ice.loopback", True)
    options.set_preference("media.peerconnection.ice.obfuscate_host_addresses", False)
    snap_root = Path("/snap/firefox/current/usr/lib/firefox")
    service = None
    if (snap_root / "firefox").exists():
        options.binary_location = str(snap_root / "firefox")
        service = Service(executable_path=str(snap_root / "geckodriver"))
    driver = webdriver.Firefox(options=options, service=service)
    wait = WebDriverWait(driver, timeout)
    try:
        driver.get(url)
        wait.until(lambda browser: browser.find_element(By.ID, "join-btn").is_enabled())
        driver.find_element(By.ID, "join-btn").click()
        wait.until(lambda browser: "direct" in browser.find_element(By.ID, "status").text.lower()
                   or "webrtc" in browser.find_element(By.ID, "status").text.lower())
        driver.execute_script("document.querySelector('.btn.a').dispatchEvent(new PointerEvent('pointerdown',{pointerId:1,bubbles:true}))")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not any(
            item.get("data", {}).get("b", {}).get("cross") is True for item in received
        ):
            time.sleep(0.05)
        if not any(item.get("data", {}).get("b", {}).get("cross") is True for item in received):
            raise RuntimeError("WebRTC DataChannel did not deliver the pressed button")
        driver.execute_script("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not any(
            item.get("data", {}).get("b", {}).get("cross") is False for item in received
        ):
            time.sleep(0.05)
        if not any(item.get("data", {}).get("b", {}).get("cross") is False for item in received):
            raise RuntimeError("page lifecycle did not neutralize the pressed button")
        if not any(not item.get("relay") for item in received):
            raise RuntimeError("input never moved from WebSocket relay to WebRTC")
    finally:
        driver.quit()


async def smoke(timeout: float) -> None:
    received: list[dict] = []
    app, peers = await make_app(received)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    socket = site._server.sockets[0]  # aiohttp exposes the selected ephemeral port here.
    port = socket.getsockname()[1]
    try:
        await asyncio.to_thread(
            run_firefox,
            f"http://127.0.0.1:{port}/#/join/abcdefghijklmnop/{'s' * 32}",
            received,
            timeout,
        )
    finally:
        for peer in peers:
            await peer.close()
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    asyncio.run(smoke(args.timeout))
    print("Real-browser WebRTC smoke passed")


if __name__ == "__main__":
    main()

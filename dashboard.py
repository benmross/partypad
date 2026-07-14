"""Loopback-only first-run dashboard for packaged PartyPad builds."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import urllib.request
import webbrowser
from pathlib import Path

from aiohttp import web
import qrcode
import qrcode.image.svg

import setup_dolphin
from device_auth import authorize_device, default_credential_store

DASHBOARD_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>PartyPad</title><style>
body{font:16px system-ui;max-width:850px;margin:2rem auto;padding:0 1rem;background:#111827;color:#f9fafb}
.card{background:#1f2937;padding:1rem;margin:1rem 0;border-radius:12px}button,select{font:inherit;padding:.55rem;margin:.25rem}
button{cursor:pointer}.error{color:#fca5a5}a{color:#93c5fd;overflow-wrap:anywhere}.players{display:grid;grid-template-columns:repeat(4,1fr);gap:.5rem}
.player{background:#374151;padding:.8rem;border-radius:8px}code{white-space:pre-wrap}img{background:white;max-width:220px}
</style><h1>PartyPad</h1><div id=error class=error></div>
<div class=card><h2>Authorization</h2><p id=auth>Checking…</p><button onclick="post('/authorize')">Authorize laptop</button></div>
<div class=card><h2>Dolphin</h2><p id=dolphin>Checking…</p><button onclick="post('/dolphin/setup')">Set up</button><button onclick="post('/dolphin/revert')">Revert</button></div>
<div class=card><h2>Session</h2><select id=system><option value=wii>Nintendo Wii</option><option value=nes>Nintendo NES (Linux only)</option></select>
<button onclick="post('/session/start',{system:system.value})">Start online session</button><button onclick="post('/session/stop')">Stop session</button>
<p><a id=join></a></p><img id=qr hidden><div class=players id=players></div></div>
<div class=card><h2>Diagnostics</h2><code id=logs></code></div>
<script>
async function post(path,data={}){let r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});let j=await r.json();if(!r.ok)document.querySelector('#error').textContent=j.error||'Request failed';else document.querySelector('#error').textContent='';await refresh()}
async function refresh(){let s=await (await fetch('/api/status')).json();auth.textContent=s.authorization;dolphin.textContent=s.dolphin;
join.textContent=s.join_url||'';join.href=s.join_url||'#';qr.hidden=!s.qr;qr.src=s.qr||'';logs.textContent=(s.logs||[]).join('\n');
players.innerHTML=[1,2,3,4].map(n=>{let p=(s.players||[]).find(x=>x.player===n);return `<div class=player>Player ${n}<br>${p?(p.path+(p.rtt_ms==null?'':' · '+p.rtt_ms+' ms')):'waiting'}</div>`}).join('')}
setInterval(refresh,1000);refresh();</script>"""


class DashboardState:
    def __init__(self, service_url: str):
        self.service_url = service_url
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task | None = None
        self.authorization: asyncio.Task | None = None
        self.authorization_error = ""
        self.logs: list[str] = []
        descriptor, name = tempfile.mkstemp(prefix="partypad-state-", suffix=".json")
        os.close(descriptor)
        self.state_path = Path(name)
        self.state_path.unlink(missing_ok=True)

    def command(self, system: str) -> list[str]:
        arguments = ["bridge", "--system", system, "--online", "--service-url", self.service_url,
                     "--state-file", str(self.state_path)]
        if getattr(sys, "frozen", False):
            return [sys.executable, *arguments]
        return [sys.executable, str(Path(__file__).with_name("partypad.py")), *arguments]

    async def start(self, system: str) -> None:
        if self.process is not None and self.process.returncode is None:
            raise ValueError("a session is already running")
        if system not in ("wii", "nes"):
            raise ValueError("unsupported system")
        if default_credential_store().load() is None:
            raise ValueError("authorize this laptop first")
        self.logs = []
        self.process = await asyncio.create_subprocess_exec(
            *self.command(system),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.reader = asyncio.create_task(self._read_output())

    async def _read_output(self) -> None:
        assert self.process and self.process.stdout
        async for raw in self.process.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line and not line.startswith(("█", "▀", "▄")):
                self.logs = [*self.logs[-39:], line[:500]]

    def session_state(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    async def stop(self) -> None:
        if self.process is None or self.process.returncode is not None:
            self.state_path.unlink(missing_ok=True)
            return
        state = self.session_state()
        if isinstance(state.get("end_url"), str) and isinstance(state.get("host_secret"), str):
            def revoke():
                request = urllib.request.Request(
                    state["end_url"],
                    data=json.dumps({"secret": state["host_secret"]}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="DELETE",
                )
                with urllib.request.urlopen(request, timeout=5):
                    pass
            try:
                await asyncio.to_thread(revoke)
            except OSError:
                pass
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except asyncio.TimeoutError:
            self.process.kill()
            await self.process.wait()
        if self.reader:
            await self.reader
        self.state_path.unlink(missing_ok=True)

    async def close(self) -> None:
        await self.stop()


def dolphin_status() -> tuple[str, Path | None]:
    try:
        path = setup_dolphin.select_dolphin_dir(setup_dolphin.discover_dolphin_dirs())
    except ValueError as exc:
        return str(exc), None
    configured = (path / setup_dolphin.MANIFEST_NAME).exists()
    return f"{path} — {'configured' if configured else 'not configured'}", path


def qr_data(value: str) -> str:
    image = qrcode.make(value, image_factory=qrcode.image.svg.SvgPathImage)
    encoded = base64.b64encode(image.to_string()).decode()
    return f"data:image/svg+xml;base64,{encoded}"


def create_app(service_url: str, launch_token: str) -> web.Application:
    app = web.Application(client_max_size=16 * 1024)
    state = DashboardState(service_url)

    @web.middleware
    async def local_auth(request, handler):
        if request.path.startswith("/launch/"):
            return await handler(request)
        if request.cookies.get("partypad-dashboard") != launch_token:
            raise web.HTTPForbidden(text="This dashboard link is not authorized.")
        return await handler(request)

    app.middlewares.append(local_auth)

    async def launch(_request):
        if _request.match_info["token"] != launch_token:
            raise web.HTTPForbidden(text="Invalid dashboard launch token.")
        response = web.HTTPFound("/")
        response.set_cookie("partypad-dashboard", launch_token, httponly=True, samesite="Strict")
        raise response

    async def index(_request):
        return web.Response(
            text=DASHBOARD_HTML,
            content_type="text/html",
            headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'self'; frame-ancestors 'none'"},
        )

    async def status(_request):
        credential = default_credential_store().load()
        authorization = (
            f"Authorized as {credential.device_name} until {credential.expires_at}"
            if credential else "Not authorized"
        )
        if state.authorization is not None and not state.authorization.done():
            authorization = "Waiting for browser authorization…"
        elif state.authorization_error:
            authorization = f"Authorization failed: {state.authorization_error}"
        dolphin, _path = dolphin_status()
        session = state.session_state()
        join_url = session.get("join_url") if isinstance(session.get("join_url"), str) else ""
        return web.json_response({
            "authorization": authorization,
            "dolphin": dolphin,
            "join_url": join_url,
            "qr": qr_data(join_url) if join_url else "",
            "players": session.get("players", []),
            "logs": state.logs,
        }, headers={"Cache-Control": "no-store"})

    async def action(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("invalid request")
            if request.path == "/session/start":
                await state.start(str(body.get("system", "wii")))
            elif request.path == "/session/stop":
                await state.stop()
            elif request.path == "/authorize":
                if state.authorization is None or state.authorization.done():
                    state.authorization_error = ""
                    state.authorization = asyncio.create_task(
                        asyncio.to_thread(authorize_device, service_url)
                    )
                    def authorization_done(task):
                        try:
                            task.result()
                        except Exception as exc:
                            state.authorization_error = str(exc)
                    state.authorization.add_done_callback(authorization_done)
            elif request.path in ("/dolphin/setup", "/dolphin/revert"):
                _description, path = dolphin_status()
                if path is None:
                    raise ValueError("select a Dolphin user directory with the CLI first")
                if setup_dolphin.dolphin_is_running():
                    raise ValueError("close Dolphin before changing its configuration")
                await asyncio.to_thread(
                    setup_dolphin.revert if request.path.endswith("revert") else setup_dolphin.install,
                    path,
                )
            return web.json_response({"ok": True})
        except (OSError, RuntimeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

    app.router.add_get("/launch/{token}", launch)
    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    for path in ("/authorize", "/dolphin/setup", "/dolphin/revert", "/session/start", "/session/stop"):
        app.router.add_post(path, action)
    app.on_cleanup.append(lambda _app: state.close())
    return app


async def run_dashboard(service_url: str) -> None:
    token = secrets.token_urlsafe(24)
    app = create_app(service_url, token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    await web.SockSite(runner, sock).start()
    url = f"http://127.0.0.1:{port}/launch/{token}"
    print(f"PartyPad dashboard: {url}")
    webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="open the PartyPad local dashboard")
    parser.add_argument(
        "--service-url",
        default=os.environ.get("PARTYPAD_SERVICE_URL", "https://partypad.benmross.com"),
    )
    args = parser.parse_args(argv)
    try:
        asyncio.run(run_dashboard(args.service_url))
    except KeyboardInterrupt:
        pass

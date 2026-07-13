# PartyPad agent guide

## Purpose and maturity

PartyPad turns phone browsers into emulator controllers. Dolphin over the
DSU/cemuhook protocol is the primary backend; Linux uinput/evdev support for a
RetroArch NES controller is experimental. The project is early alpha; preserve
working behavior and describe limitations honestly.

## Architecture

- `server.py`: aiohttp/WebSocket server, DSU protocol, pad state, TLS, and CLI.
- `online_transport.py`: outbound hosted-session signaling, relay input, trickled
  remote ICE candidates, and the optional aiortc WebRTC host.
- `systems.py`: system registry, controller modes, and backend selection.
- `uinput_backend.py`: experimental Linux virtual controllers for RetroArch.
- `hotspot.py`: unprivileged AP lifecycle and Polkit helper orchestration.
- `ap_helper.py`: narrow root helper for ap0, hostapd/dnsmasq, captive landing,
  forwarding, NAT, and cleanup.
- `setup_dolphin.py`: reversible Dolphin INI configuration.
- `setup_retroarch.py`: reversible user-local RetroArch autoconfiguration.
- `setup_online.py`: private host-token generation and Worker secret upload.
- `cloudflare/`: Worker/Durable Object signaling service, static asset binding,
  TURN credential provisioning, and pinned deployment toolchain.
- `static/`: phone controller UI, browser sensors, WebRTC trickle ICE, and
  WebSocket input fallback.
- `tests/`: standard-library unit tests.

## Commands

```sh
uv sync
uv sync --extra online
uv run python -m unittest discover -s tests -v
uv run python -m py_compile server.py online_transport.py systems.py uinput_backend.py hotspot.py ap_helper.py setup_dolphin.py setup_online.py setup_retroarch.py
uv run python server.py --help
node --check static/app.js
cd cloudflare && nvm install && npm ci && npm run check
```

## Safety rules

- Never start AP mode during remote work without confirming how the session is
  connected and preparing cleanup.
- Do not disconnect or repurpose the station interface. PartyPad uses virtual
  `ap0` and the station's current channel.
- Root-helper changes must be reversible on normal exit, exceptions, signals,
  and parent death. Keep firewall rules exact and scoped to PartyPad's subnet.
- Never commit `certs/`, `logs/`, `.venv/`, emulator configs, secrets, or local
  network identifiers.
- Never print or commit the host token, TURN token, TURN API token, `.dev.vars`,
  or live session secrets. Public join URLs are bearer credentials until their
  session is revoked or expires.
- Revoke temporary online sessions after integration tests. A Worker deploy
  disconnects active signaling sockets, so avoid deploying during live play.
- Treat signaling and controller payloads as untrusted. Keep message, candidate,
  sequence, and numeric validation bounded on both the Worker and Python host.
- Motion mappings are hardware/browser-sensitive. Do not change global axis
  signs without comparative logs from affected and currently working devices.

## Style and scope

- Target Python 3.11+ and keep dependencies small.
- Prefer standard-library tests and pure helper functions for privileged logic.
- Update README limitations and tests with behavior changes.
- Preserve immediate WebSocket input fallback while changing WebRTC. Do not
  force TURN or remove direct/TCP/TLS candidates based on one network test;
  PartyPad must continue to work across campus Wi-Fi and cellular NATs.
- aiortc gathers its local answer candidates as a batch. Browsers trickle their
  candidates through the Durable Object so offer creation must not wait for ICE
  gathering to complete; candidates may legally arrive before the offer task.
- Keep emulator backends separable; RetroArch support must not be coupled into
  Dolphin configuration or DSU packet construction.

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
- `device_auth.py`: verifier-bound device authorization and OS-keyring/private-
  file credential storage.
- `dashboard.py` and `partypad.py`: token-protected loopback dashboard and the
  unified source/packaged entry point.
- `systems.py`: system registry, controller modes, and backend selection.
- `uinput_backend.py`: experimental Linux virtual controllers for RetroArch.
- `hotspot.py`: unprivileged AP lifecycle and Polkit helper orchestration.
- `ap_helper.py`: narrow root helper for ap0, hostapd/dnsmasq, captive landing,
  forwarding, NAT, and cleanup.
- `setup_dolphin.py`: reversible Dolphin INI configuration.
- `setup_retroarch.py`: reversible user-local RetroArch autoconfiguration.
- `setup_online.py`: desktop device authorization/status/forget CLI.
- `cloudflare/`: Worker/Durable Object signaling service, static asset binding,
  TURN credential provisioning, and pinned deployment toolchain.
- `static/`: phone controller UI, browser sensors, WebRTC trickle ICE, and
  WebSocket input fallback.
- `tests/`: standard-library unit tests.

## Project direction

- `docs/roadmaps/umd-self-service.md` is the canonical plan for self-service
  UMD authorization, abuse controls, cross-platform Dolphin support, packaged
  desktop releases, and the campus pilot. Read it before working in those areas
  and update it when decisions or milestone status change.
- The production service uses per-device authorization through Cloudflare
  Access and D1. The old shared `HOST_TOKEN` was deleted; never reintroduce,
  embed, or distribute one.
- The root README documents current behavior. Roadmap items must not be
  described as shipped until their gate is actually met and tested.

## Commands

```sh
uv sync
uv sync --extra online
uv run python -m unittest discover -s tests -v
uv run python -m py_compile server.py online_transport.py device_auth.py systems.py uinput_backend.py hotspot.py ap_helper.py setup_dolphin.py setup_online.py setup_retroarch.py dashboard.py partypad.py version.py tools/generate_sbom.py tools/browser_smoke.py
uv run python server.py --help
node --check static/app.js
cd cloudflare && npm ci && npm run check && npm test
```

The Worker requires the exact Node 22.23.x line pinned by `cloudflare/.nvmrc`
and `package.json`. `nvm` is convenient but not assumed to exist; use any Node
version manager or installation that supplies the pinned version. In minimal or
noninteractive agent shells, `uv` may also be absent from `PATH` even when the
repository `.venv` exists; `.venv/bin/python` is a valid fallback for Python
tests and CLIs, but dependency lock/build validation still requires `uv`.

## Durable testing and operations notes

- The dashboard binds an ephemeral `127.0.0.1` port and authenticates through
  its printed `/launch/<token>` path. When testing across SSH, forward that
  exact port and preserve the complete launch path while replacing only the
  browser-side port. Each restart creates a new port and token.
- `DASHBOARD_HTML` embeds JavaScript in a Python string. Escape sequences must
  survive both parsers: a JavaScript `\n` requires `\\n` in the Python source.
  Validate the script extracted from the rendered `dashboard.DASHBOARD_HTML`,
  not merely the source lines. Use explicit DOM lookups; do not depend on HTML
  element IDs becoming browser globals.
- Dashboard `Ctrl+C` is signal-driven. Keep bridge termination and hosted-
  session revocation concurrent so shutdown is prompt even if the network call
  takes its full timeout.
- Controller selected-path/RTT text refreshes every five seconds, while RTT
  telemetry remains at thirty seconds to avoid multiplying D1 writes. Preserve
  that separation when changing diagnostics cadence.
- Source checkout, locally built PyInstaller, isolated-home first-run, native
  Arch Dolphin 2606, direct/UDP Wi-Fi, TURN/UDP cellular, and clean session stop
  have passed. CI package builds and command smoke tests pass on Linux x64,
  Windows x64, macOS Intel, and macOS Apple Silicon. Downloaded-artifact clean-
  laptop testing and real Windows/macOS Dolphin testing remain outstanding.
- Production D1 tracks hosted sessions in `session_owners`, not `sessions`.
  Before a Worker deploy, query for unended/unexpired rows and confirm the count
  is zero. Deploys disconnect active Durable Object sockets.
- `static/` is a Worker asset binding. A phone UI change is not live merely
  because GitHub was pushed; deploy the Worker and verify the hosted asset.
- Cloudflare Access verification expects `ACCESS_TEAM_DOMAIN` to contain only
  the team subdomain (for example `example`, not
  `example.cloudflareaccess.com`). `/activate` must redirect through Access,
  while `/config`, controller assets, signaling, and device-code create/poll
  endpoints remain public.
- Wrangler OAuth is already configured on the production host. Check
  `npx wrangler whoami` before attempting login. During SSH-only reauthentication,
  the browser's localhost callback must be forwarded to the remote callback
  port or completed in a browser on that host; opening the login URL alone does
  not make an unreachable localhost callback work.

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
  device credentials, authorization codes, or live session secrets. Public join
  URLs are bearer credentials until their session is revoked or expires.
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

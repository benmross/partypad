# PartyPad agent guide

## Purpose and maturity

PartyPad turns phone browsers into emulator controllers. Dolphin over the
DSU/cemuhook protocol is the primary backend; Linux uinput/evdev support for a
RetroArch NES controller is experimental. The project is early alpha; preserve
working behavior and describe limitations honestly.

## Architecture

- `server.py`: aiohttp/WebSocket server, DSU protocol, pad state, TLS, and CLI.
- `systems.py`: system registry, controller modes, and backend selection.
- `uinput_backend.py`: experimental Linux virtual controllers for RetroArch.
- `hotspot.py`: unprivileged AP lifecycle and Polkit helper orchestration.
- `ap_helper.py`: narrow root helper for ap0, hostapd/dnsmasq, captive landing,
  forwarding, NAT, and cleanup.
- `setup_dolphin.py`: reversible Dolphin INI configuration.
- `setup_retroarch.py`: reversible user-local RetroArch autoconfiguration.
- `static/`: phone controller UI and browser sensor handling.
- `tests/`: standard-library unit tests.

## Commands

```sh
uv sync
uv run python -m unittest discover -s tests -v
uv run python -m py_compile server.py systems.py uinput_backend.py hotspot.py ap_helper.py setup_dolphin.py setup_retroarch.py
uv run python server.py --help
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
- Motion mappings are hardware/browser-sensitive. Do not change global axis
  signs without comparative logs from affected and currently working devices.

## Style and scope

- Target Python 3.11+ and keep dependencies small.
- Prefer standard-library tests and pure helper functions for privileged logic.
- Update README limitations and tests with behavior changes.
- Keep emulator backends separable; RetroArch support must not be coupled into
  Dolphin configuration or DSU packet construction.

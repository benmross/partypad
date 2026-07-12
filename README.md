# PartyPad

PartyPad turns phones into controllers for Dolphin and, experimentally, RetroArch.
Players scan a QR code, open a browser, and are assigned one of four controller
slots—no phone app or per-player IP configuration required.

> **Project status: early alpha.** Buttons and Dolphin integration work well on
> the tested setup. Motion mapping, Android behavior, hardware compatibility,
> and the portable access-point mode still need broader testing.

```text
phone browser --WebSocket--> PartyPad --DSU/UDP--> Dolphin
                                |
                                +--uinput/evdev--> RetroArch
```

## Current goals

- Make local multiplayer easy: join from a phone in seconds.
- Provide a faithful Wii Remote-style touch layout with motion and IR input.
- Work without venue Wi-Fi by safely creating a temporary Linux access point.
- Keep setup local, transparent, reversible, and free of hosted services.
- Grow RetroArch support from an NES proof of concept to authentic system layouts.

## What works today

- Up to four browser controllers with fixed DSU/cemuhook slots.
- Wii Remote buttons, D-pad, accelerometer, optional gyroscope, and IR pointer.
- Automatic Dolphin configuration with backups.
- Experimental four-player Linux uinput backend and landscape NES layout for RetroArch.
- HTTPS with a locally generated self-signed certificate for browser sensors.
- Optional `--ap` mode with Wi-Fi and URL QR codes.
- Internet sharing through the AP when the host has an active default route.
- Offline captive landing page when no upstream connection exists.
- AP, DHCP/DNS, forwarding, and firewall cleanup on normal exit or server crash.

## Requirements

- Linux and Python 3.11 or newer.
- [`uv`](https://docs.astral.sh/uv/) for the documented workflow.
- Dolphin and/or RetroArch, depending on the selected backend.
- Access to `/dev/uinput` for RetroArch (commonly provided by the `input` group).
- A phone with a modern browser on the same network.

Access-point mode additionally requires:

- A Wi-Fi adapter supporting nl80211 AP mode. Concurrent client + AP operation
  is required to preserve an existing Wi-Fi connection.
- `hostapd`, `dnsmasq`, `iw`, `iptables`, `polkit`, and NetworkManager's `nmcli`.
- A working desktop Polkit authentication agent for `pkexec`.

AP mode is currently developed and tested on Arch Linux with NetworkManager and
an Intel AX210. Other distributions, network managers, and adapters are not yet
verified.

## Quick start

```sh
git clone https://github.com/benmross/partypad.git
cd partypad
uv sync
uv run python server.py
```

With no arguments, PartyPad asks which supported system will be played:

```text
PartyPad setup — choose the system being played:
  1. Nintendo Entertainment System [nes] — RetroArch via uinput
  2. Nintendo Wii [wii] — Dolphin via DSU
System:
```

For scripts, shortcuts, and unattended startup, bypass the question explicitly:

```sh
uv run python server.py --system nes
uv run python server.py --system wii
```

Scan the URL QR code from each phone. The HTTPS certificate is self-signed, so
each phone must accept the browser warning once before joining.

Useful options:

```text
--port 8080          Web server port
--system SYSTEM      Select the system and bypass interactive setup
--backend BACKEND    Advanced override: dolphin, retroarch, or both
--ip ADDRESS         Override the address advertised in the QR code
--http               Plain HTTP; disables motion sensors on iOS
--pointer-only       Keep IR input but send a stable, level IMU
--gyro               Forward gyroscope data for MotionPlus testing
--log                Write motion diagnostics under logs/
--regen-cert         Regenerate the local TLS certificate
```

Run `uv run python server.py --help` for the complete list.

## Systems, controller modes, and backends

These are separate concepts:

- A **system** is what the players are emulating, such as `nes` or `wii`.
- A **controller mode** defines the phone layout and semantic control mapping.
- A **backend** delivers canonical pad state to an emulator. Dolphin uses DSU;
  RetroArch uses Linux uinput/evdev.

The registry in `systems.py` is the source of truth. A system is offered in the
interactive chooser only after its controller mode, backend mapping, tests, and
documentation are implemented. Supplying a registered roadmap system currently
produces an explicit unsupported error instead of silently showing the wrong
controller.

| System argument | Status | Controller mode | Default backend |
|---|---|---|---|
| `nes` | Experimental | Full-screen landscape NES controller | RetroArch/uinput |
| `wii` | Experimental | Portrait Wii Remote with pointer and motion | Dolphin/DSU |

Registered roadmap systems:

```text
amiga amstradcpc arcade atari2600 atari5200 atari7800 atarilynx bbcmicro c64
coleco cps daphne doom dosbox fba fds gamegear gb gba gbc gw intelli
mastersystem megadrive msx neogeo ngp pcecd pcengine pico8 pokemini psx quake
scummvm sega32x segacd sg-1000 snes supervision test tic80 vb wsc zx
```

Some of these will share physical controller families, but they remain separate
system entries so core-specific devices and remaps can be added later.

## Portable access point

```sh
uv run python server.py --system wii --ap
```

Defaults:

- Wi-Fi name: `PartyPad`
- Password: `partypad`
- Controller URL: `https://192.168.12.1:8080/`

The first QR joins the Wi-Fi; the second opens the controller. When a default
route exists, PartyPad shares it using temporary NAT rules. Without one, it
provides a local captive landing page. Customize the network with
`--ap-interface`, `--ap-name`, and `--ap-password`.

The privileged helper is intentionally narrow. It creates `ap0`, runs hostapd
and dnsmasq, enables scoped forwarding/NAT when needed, and reverts its changes
on shutdown. Review [`ap_helper.py`](ap_helper.py) before using AP mode on a
system with custom firewall or network policy.

## Dolphin setup

Close Dolphin, then run:

```sh
uv run python setup_dolphin.py
```

This adds `partypad:127.0.0.1:26760` to `DSUClient.ini` and maps Wii Remotes
1–4 to PartyPad slots 1–4. Existing configuration files are backed up once with
the suffix `.partypad-bak`. Dolphin must remain closed while the files change.

| Phone control | DSU field | Wii input |
|---|---|---|
| A | Cross | A |
| B | Square | B |
| 1 | Triangle | 1 |
| 2 | Circle | 2 |
| − / + | Share / Options | − / + |
| HOME | PS | Home |
| D-pad | Pad N/S/W/E | D-pad |

## RetroArch proof of concept

RetroArch support currently targets Linux's `udev` controller driver. PartyPad
creates four virtual controllers at startup so player ordering remains stable as
phones join and leave. First install the user-local autoconfiguration profile:

```sh
uv run python setup_retroarch.py
```

Revert it at any time with `uv run python setup_retroarch.py --revert`. Then
start PartyPad before RetroArch:

```sh
uv run python server.py --system nes
```

The default RetroArch profile is a landscape NES controller. It maps the visible
NES A and B buttons to RetroPad A (east) and B (south), matching Mesen's default
Standard Controller device. In RetroArch, use the `udev` controller driver and confirm that
`PartyPad Virtual Controller` is assigned to ports 1–4. The Mesen core's standard
NES joypad is the initial test target.

For a non-default uinput device permission setup, grant only the user running
PartyPad read/write access to `/dev/uinput`; do not run the web server as root.

## Limitations

- **Experimental RetroArch support:** the uinput backend currently covers a
  standard digital/analog RetroPad and an NES phone layout. Mouse, lightgun,
  paddle, keyboard, rumble, motion, and system-specific layouts are not yet
  implemented. RetroArch support is Linux-only.
- **Experimental motion:** the current mapping was tuned on iOS. Android Chrome,
  including Pixel devices, may report gravity axes with different signs; Android
  steering is known to be incorrect and awaits device-side logging and testing.
- **Wii portrait layout:** the Wii controller and motion frame assume a portrait
  phone. The NES controller is designed for landscape use.
- **Self-signed HTTPS:** browser warnings are expected. PartyPad does not install
  a certificate authority or transmit certificates off the host.
- **AP hardware constraints:** some adapters cannot run client and AP modes at
  once, and concurrent modes may be restricted to one radio channel.
- **Linux-specific AP mode:** the normal server is portable Python, but the AP
  helper depends on Linux networking tools and Polkit.
- **No authentication on the controller page:** anyone who can reach the server
  can claim one of four slots. Use only on trusted local networks.
- **No system service or packaged release yet:** run it from the checkout.

## Roadmap

1. Normalize and test motion across iOS and Android devices.
2. Add automated integration tests for AP startup and cleanup in a network
   namespace.
3. Implement shared controller families and add SNES, Genesis, PlayStation, and arcade layouts.
4. Add RetroArch mouse, lightgun, paddle, keyboard, rumble, and motion device support.
5. Package releases and improve support beyond Arch Linux/NetworkManager.

## Development

```sh
uv sync
uv run python -m unittest discover -s tests -v
uv run python -m py_compile server.py systems.py uinput_backend.py hotspot.py ap_helper.py setup_dolphin.py setup_retroarch.py
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for contribution guidance and
[`SECURITY.md`](SECURITY.md) for reporting security problems.

## License

PartyPad is available under the [MIT License](LICENSE).

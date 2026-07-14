# PartyPad

PartyPad turns phones into controllers for Dolphin and, experimentally, RetroArch.
Players scan a QR code, open a browser, and are assigned one of four controller
slots—no phone app or per-player IP configuration required.

> **Project status: early alpha.** Buttons and Dolphin integration work well on
> the tested setup. Motion mapping, Android behavior, hardware compatibility,
> and the portable access-point mode still need broader testing.

```text
phone browser --WebRTC/relay--> PartyPad --DSU/UDP--> Dolphin
                                  |
                                  +--uinput/evdev--> RetroArch
```

## Current goals

- Make local multiplayer easy: join from a phone in seconds.
- Provide a faithful Wii Remote-style touch layout with motion and IR input.
- Work without venue Wi-Fi by safely creating a temporary Linux access point.
- Preserve a fully local mode while allowing controllers to join securely from
  unrelated Wi-Fi networks or cellular data.
- Grow RetroArch support from an NES proof of concept to authentic system layouts.

## What works today

- Up to four browser controllers with fixed DSU/cemuhook slots.
- A disconnected phone's player slot is held for 30 seconds and reclaimed when
  that browser reconnects; a new phone cannot take the reserved slot meanwhile.
- Wii Remote buttons, D-pad, accelerometer, optional gyroscope, and IR pointer.
- Automatic Dolphin configuration with backups.
- Experimental four-player Linux uinput backend and landscape NES layout for RetroArch.
- HTTPS with a locally generated self-signed certificate for browser sensors.
- Optional `--ap` mode with Wi-Fi and URL QR codes.
- Internet sharing through the AP when the host has an active default route.
- Offline captive landing page when no upstream connection exists.
- AP, DHCP/DNS, forwarding, and firewall cleanup on normal exit or server crash.
- Optional online sessions with trusted HTTPS, direct WebRTC when available,
  managed TURN on restrictive networks, and a WebSocket reliability fallback.

## Requirements

- Python 3.11 or newer. The source workflow and real emulator integration are
  currently verified on Linux; unsigned Windows and macOS packages build in CI
  but still need hardware testing.
- [`uv`](https://docs.astral.sh/uv/) for the documented workflow.
- Dolphin and/or RetroArch, depending on the selected backend.
- Access to `/dev/uinput` for RetroArch (commonly provided by the `input` group).
- A phone with a modern browser. Local mode requires the same network; online
  mode works across unrelated networks and cellular data.

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

The unified entry point opens the loopback dashboard:

```sh
uv run python partypad.py
```

The dashboard shows authorization and Dolphin setup state, starts/stops online
sessions, displays the controller QR/link, and reports player transport/RTT.
Its random launch credential is stored in an HttpOnly same-site cookie and the
server binds only to `127.0.0.1`. The development/advanced terminal interface
remains available with `uv run python partypad.py serve` or `server.py`.

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

### Online sessions

Online mode is the recommended option on eduroam, guest Wi-Fi, cellular data,
or any network that isolates devices:

```sh
uv sync --extra online
uv run python server.py --system wii --online
```

Authorize a laptop once through the system browser before its first online
session:

```sh
uv run python setup_online.py
```

The command always prints the complete activation link, so authorization also
works from SSH and other terminals where a browser cannot open automatically.

The protocol-v1 Worker and production D1 schema are deployed. Cloudflare Access
protects the authentication hostname, and the Worker is configured to verify
that application's audience and team issuer. Authenticated browser approval,
one-time code consumption, credential issuance, and local credential loading
have passed a production smoke test. Deployment details are in
[`cloudflare/README.md`](cloudflare/README.md).

The desktop uses a verifier-bound device code and receives its own revocable
credential; there is no shared host token. The OS credential service is used
when Python's optional `keyring` package exposes one, with a private user config
file as the fallback. `--status` shows the local authorization state and
`--forget` removes the local copy (use the authenticated device page to revoke
the server-side credential).

The QR points to `https://partypad.benmross.com`. Phones do not need to share a
network with the computer. Both sides make outbound connections; WebRTC ICE
selects a direct route when one works and otherwise uses Cloudflare Realtime
TURN. Input begins over the signaling relay while WebRTC negotiates and falls
back to that relay if real-time transports are blocked entirely.

The browser sends its offer immediately and trickles ICE candidates as they are
discovered, so slow cellular candidates do not block controller input or
negotiation. The Python aiortc endpoint gathers its answer candidates as a
batch, which can take several seconds on a computer with multiple physical or
virtual network interfaces. The phone status reports `direct/UDP`, `TURN/UDP`,
TCP/TLS TURN, or `relay` for the selected path.

Each session uses independent random host and join
secrets. The join secret is carried in the URL fragment, which is not sent in
HTTP requests or referrer headers. Normal shutdown revokes the session
immediately; abandoned sessions expire after four hours.

Useful options:

```text
--port PORT          Local web port (8080 by default; ephemeral in online mode)
--system SYSTEM      Select the system and bypass interactive setup
--backend BACKEND    Advanced override: dolphin, retroarch, or both
--ip ADDRESS         Override the address advertised in the QR code
--http               Plain HTTP; disables motion sensors on most mobile browsers
--online             Accept controllers from any network through the hosted service
--service-url URL    Override the hosted session service
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
| `wii` | Experimental | Wii Remote touch layout with pointer and motion | Dolphin/DSU |

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

Close Dolphin, preview the exact changes, then configure it:

```sh
uv run python setup_dolphin.py --preview
uv run python setup_dolphin.py
```

This adds `partypad:127.0.0.1:26760` to `DSUClient.ini` and maps Wii Remotes
1–4 to PartyPad slots 1–4. It discovers current Windows, macOS, Linux XDG,
legacy Linux, and Flatpak directories plus `DOLPHIN_EMU_USERPATH`. Portable
builds use `--portable-dir`; custom Dolphin user directories use
`--dolphin-user-dir`. Multiple populated directories are never guessed between.
Existing files are backed up once, writes are atomic and idempotent, and
`--revert` restores the original state. Dolphin must remain closed while files
change unless `--force` explicitly overrides the protection.

| Phone control | DSU field | Wii input |
|---|---|---|
| A | Cross | A |
| B | Square | B |
| 1 | Triangle | 1 |
| 2 | Circle | 2 |
| − / + | Share / Options | − / + |
| HOME | PS | Home |
| D-pad | Pad N/S/W/E | D-pad |

For Mario Kart-style steering, keep screen autorotation locked and hold the
phone in landscape with its top edge pointing left, like a horizontal Wii
Remote. Tap the player status on the controller to show live motion diagnostics.
For comparative device testing, start the server with `--log`; JSONL samples are
written under the ignored `logs/` directory.

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

## Native alpha builds

The PyInstaller specification creates a self-contained executable with the
online dependencies and controller assets embedded:

```sh
uv sync --extra online --group build
uv run pyinstaller --clean --noconfirm partypad.spec
./dist/partypad --help
```

Tagged CI builds native Linux x86-64, Windows x64, macOS Intel, and macOS Apple
Silicon artifacts. Draft releases contain unsigned archives, a Windows
installer, SHA-256 checksums, and a CycloneDX runtime SBOM. These are technical
alpha artifacts: Windows signing and Apple signing/notarization remain required
before PartyPad is presented as easy to install. See
[`docs/signing-and-release.md`](docs/signing-and-release.md).

## Limitations

- **Experimental RetroArch support:** the uinput backend currently covers a
  standard digital/analog RetroPad and an NES phone layout. Mouse, lightgun,
  paddle, keyboard, rumble, motion, and system-specific layouts are not yet
  implemented. RetroArch support is Linux-only.
- **Unsigned packaged alpha:** native CI and a locally built Linux one-file
  executable have passed; the Linux executable completed the configured-user
  online flow and isolated-home first-run checks. No downloaded release
  artifact or signed/notarized build has passed a clean-laptop end-to-end test
  yet. Expect OS security warnings.
- **Experimental motion:** testing on iPhone Safari and Motorola Chrome found
  opposite gravity polarity in `accelerationIncludingGravity`. PartyPad
  normalizes Android to the working iOS convention before constructing DSU
  motion data. Other browser and hardware combinations still need verification.
- **Wii orientation:** the touch layout is portrait-oriented, while verified
  Mario Kart steering uses the phone sideways with autorotation locked and its
  top edge pointing left. Other grip orientations are not yet normalized.
- **Local self-signed HTTPS:** browser warnings are expected in local and AP
  modes. Online sessions use a publicly trusted certificate and do not warn.
- **AP hardware constraints:** some adapters cannot run client and AP modes at
  once, and concurrent modes may be restricted to one radio channel.
- **Linux-specific AP mode:** the normal server is portable Python, but the AP
  helper depends on Linux networking tools and Polkit.
- **Unauthenticated local mode:** anyone who can reach the local server can claim
  one of four slots. Online sessions require a high-entropy per-session join
  secret, but anyone given that QR can join until the session ends.
- **Hosted-service dependency:** online mode depends on the configured
  Cloudflare Worker and Realtime TURN service. Local and AP modes remain fully
  self-contained and continue to work if that service is unavailable.
- **WebRTC setup time:** controller input starts over the WebSocket relay
  immediately, but aiortc can take several seconds to finish its ICE answer on
  hosts with multiple network interfaces before direct or TURN transport takes
  over.
- **No signed public release or system service yet:** run from the checkout or
  build the documented unsigned technical-alpha executable locally.

## Roadmap

The detailed plan for self-service UMD authorization, cross-platform Dolphin
support, packaged desktop applications, and a measured campus rollout is in
[`docs/roadmaps/umd-self-service.md`](docs/roadmaps/umd-self-service.md). It
distinguishes current behavior from proposed milestones and is the canonical
handoff for future contributors.

1. Expand motion testing and normalization across more iOS and Android devices,
   browsers, and grip orientations.
2. Add automated integration tests for AP startup and cleanup in a network
   namespace.
3. Implement shared controller families and add SNES, Genesis, PlayStation, and arcade layouts.
4. Add RetroArch mouse, lightgun, paddle, keyboard, rumble, and motion device support.
5. Package releases and improve support beyond Arch Linux/NetworkManager.

## Development

```sh
uv sync
uv sync --extra online
uv run python -m unittest discover -s tests -v
uv run python -m py_compile server.py online_transport.py device_auth.py systems.py uinput_backend.py hotspot.py ap_helper.py setup_dolphin.py setup_online.py setup_retroarch.py dashboard.py partypad.py version.py tools/generate_sbom.py tools/browser_smoke.py
node --check static/app.js
cd cloudflare && npm ci && npm run check && npm test
```

Cloudflare development requires Node 22.23.x as pinned in
`cloudflare/.nvmrc`; `nvm` is optional, and any version manager may provide the
pinned Node release.

An opt-in Firefox smoke test drives the real controller page through trickled
ICE, WebRTC DataChannel input, and page-lifecycle neutralization. It requires a
local Firefox and GeckoDriver:

```sh
uv sync --extra online --group browser
uv run --frozen python tools/browser_smoke.py
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for contribution guidance and
[`SECURITY.md`](SECURITY.md) for reporting security problems.

## License

PartyPad is available under the [MIT License](LICENSE).

# UMD self-service and cross-platform release roadmap

Status: proposed implementation roadmap

Last updated: 2026-07-13

Canonical goal: anyone at the University of Maryland should be able to download
PartyPad, authorize a laptop, configure Dolphin, and accept phone controllers
from eduroam or cellular data without contacting the service operator.

This document is the durable handoff for that goal. Read it before changing
online authentication, Cloudflare signaling, WebRTC, packaging, or Dolphin
configuration. Update it when a decision changes or a milestone is completed.

## Target experience

The finished first-run flow should be:

```text
Download the PartyPad build for the laptop
                    |
                    v
Launch it without installing Python or development tools
                    |
                    v  first use only
Verify an @umd.edu address in the system browser
                    |
                    v
Detect and reversibly configure Dolphin
                    |
                    v
Create an online session and display its QR code
                    |
                    v
Phones join from eduroam, other Wi-Fi, or cellular data
```

Only the laptop host authenticates. Controllers continue to use the random
per-session QR bearer secret so friends and guests do not need accounts.

Success means a normal user does not need Git, Python, `uv`, Node, Wrangler, a
Cloudflare account, router configuration, an inbound firewall rule, or manual
approval from the PartyPad operator.

## Current state

As of the date above:

- `https://partypad.benmross.com` is a deployed Cloudflare Worker with a Durable
  Object per PartyPad session.
- The Worker serves the phone UI, authenticates session participants, forwards
  signaling and fallback controller input, and provisions Cloudflare Realtime
  TURN credentials.
- The Python host makes only outbound connections. Phones may be on unrelated
  Wi-Fi networks or cellular data.
- Controller input begins immediately over a WebSocket relay. The browser then
  sends a WebRTC offer immediately and trickles ICE candidates through the
  Durable Object. aiortc returns a complete, non-trickled answer.
- ICE may select direct UDP, Cloudflare TURN over UDP/TCP/TLS, or remain on the
  WebSocket fallback. Full controller state and sequence numbers make packet
  loss and path transitions safe.
- aiortc currently uses only the first usable STUN URI and first usable TURN URI
  from an `RTCIceServer` list. Cloudflare orders UDP/3478 first, so the Python
  host does not yet provide a proven adaptive retry across UDP, TCP, and TLS
  TURN allocation transports even though browsers receive all of those URLs.
  The WebSocket fallback still works when host-side WebRTC cannot establish.
- The current phone diagnostic derives `UDP`/`TCP` from the selected candidate's
  protocol. For relay candidates, it should prefer the WebRTC stats
  `relayProtocol` field when available so the displayed TURN allocation
  transport is not misleading.
- A home-Wi-Fi laptop and cellular phone have been verified with both direct UDP
  and TURN/UDP. Candidate forwarding and Cloudflare relay allocation have also
  been exercised independently.
- On the current multi-homed Linux development laptop, browser-side trickle ICE
  makes negotiation begin immediately; aiortc's local answer gathering commonly
  takes about five seconds because Wi-Fi, IPv6, Tailscale, and libvirt addresses
  are probed together. WebSocket input remains usable during this interval.
- Normal shutdown revokes the session. Abandoned sessions expire after eight
  hours.
- The public service is not yet self-service. Session creation requires one
  private `HOST_TOKEN`, currently provisioned only on an operator-authorized
  machine.
- The desktop project remains Linux-oriented. `evdev` is unconditional,
  `setup_dolphin.py` assumes the Linux XDG config location and `pgrep`, and no
  standalone application builds exist.

Relevant implementation files:

- `server.py`: lifecycle, DSU server, pad state, CLI, and online session startup.
- `online_transport.py`: host signaling, WebSocket fallback reception, remote
  candidate handling, and aiortc peer connections.
- `static/app.js`: phone signaling, immediate input fallback, trickle ICE, and
  transport diagnostics.
- `cloudflare/src/index.ts`: session API, TURN credential issuance, Durable
  Object authentication, signaling, and relay forwarding.
- `cloudflare/wrangler.jsonc`: deployed bindings, custom domain, assets, and
  Durable Object migration.
- `setup_online.py`: current one-operator host-token provisioning.
- `setup_dolphin.py`: current Linux Dolphin configuration patcher.
- `systems.py` and `uinput_backend.py`: backend separation and Linux RetroArch
  support.

## Non-negotiable properties

Future work must preserve these properties:

- Do not put a universal host credential in a downloadable application. Any
  embedded shared token must be considered public and is not an authentication
  mechanism.
- Keep public controller joins separate from host authorization. Possession of
  a live QR secret is sufficient to join that session but cannot create another
  session.
- Keep the join secret in the URL fragment so it is not sent in normal HTTP
  requests or referrer headers.
- Retain immediate WebSocket controller input while WebRTC negotiates or fails.
- Retain direct ICE candidates as the preferred low-latency path and managed
  TURN plus WebSocket as reliability fallbacks.
- Treat session, signaling, candidate, and controller messages as untrusted and
  bound their sizes, types, sequences, and numeric values.
- Do not log controller payloads, join secrets, device tokens, TURN credentials,
  or UMD email addresses.
- Keep Dolphin configuration reversible and refuse unsafe edits while Dolphin
  is running unless the user explicitly overrides the warning.
- Keep AP mode and RetroArch optional and Linux-specific. They must not prevent
  the Dolphin/online core from running on Windows and macOS.
- Do not require administrator privileges for ordinary online Dolphin use.
- Maintain backward compatibility during service migrations so an older client
  can at least use the WebSocket fallback or receive an actionable upgrade
  error.

## Architecture decision: UMD host authorization

### Recommended pilot identity

Use Cloudflare Access email one-time passwords on a separate authentication
host such as `auth.partypad.benmross.com`, with an allow policy for addresses
ending in `@umd.edu`.

This is recommended for the first campus pilot because it:

- verifies control of a UMD email address without the operator approving users;
- can be deployed independently of a formal university application review;
- keeps authentication in the browser, where email OTP is familiar;
- allows the desktop application to use an OAuth-style device flow without
  collecting a UMD password.

Email-domain verification does not necessarily prove current enrollment or
employment. That distinction is acceptable for an initial UMD-community pilot
and must be stated accurately.

### Institutional identity endpoint

Official UMD OIDC is the preferred long-term identity provider if the project
needs authoritative current-student or employee status. UMD supports OIDC, CAS,
and SAML, but applications must be registered and reviewed. That process may
require a Unit Head plus data steward and security/compliance approval.

Request only authentication and the minimum stable subject identifier. Do not
request student records, course membership, contact data, or other directory
attributes unless a concrete feature later requires them.

The OTP pilot must be designed so its identity assertion can later be replaced
by UMD OIDC without changing desktop device tokens or public controller joins.

### Device authorization flow

Implement a device flow rather than asking users to copy API tokens:

```text
PartyPad desktop                    Authenticated browser
       |                                      |
       | POST /api/device/authorizations      |
       |------------------------------------->|
       | device_code, user_code, verifier     |
       |<-------------------------------------|
       |                                      |
       | open /activate and show user_code    |
       |------------------------------------->|
       |                              @umd.edu OTP
       |                                      |
       |                 approve matching code|
       |                                      |
       | poll with device_code + verifier     |
       |------------------------------------->|
       | scoped device token                  |
       |<-------------------------------------|
```

Security details:

- Generate a high-entropy `device_code` and separate human-readable
  `user_code`.
- Generate a local verifier when authorization begins. Store only its hash
  server-side so observing the user code cannot steal the resulting credential.
- Device codes should expire in roughly ten minutes and be single-use.
- Polling should have a server-provided minimum interval and rate limits.
- The activation page must display the device description and user code before
  approval to reduce accidental or malicious device authorization.
- Issue a different opaque random token to every authorized laptop. Store only
  a keyed hash of the token in the service database.
- Give device tokens a bounded lifetime with rotation, explicit revocation, and
  a `last_used_at` timestamp.
- Store tokens in Windows Credential Manager, macOS Keychain, or Linux Secret
  Service. A `0600` user config file is the documented fallback.
- Identify users internally with a provider subject or a keyed hash of the
  normalized email. Do not persist plaintext email unless support requirements
  justify it and a privacy policy describes it.

### Proposed service boundaries

Use two logical public surfaces:

- `auth.partypad.benmross.com`: Access-protected activation and device
  management pages.
- `partypad.benmross.com`: public controller assets, public session WebSockets,
  and authenticated desktop APIs.

The exact hostname split may change, but Access policy rules must never put the
controller QR path behind UMD login.

Suggested API shape:

```text
POST   /api/device/authorizations       create a pending device flow
GET    /api/device/authorizations/:id   poll status with verifier
POST   /activate                        Access-protected approval
GET    /api/devices                     list the current user's devices
DELETE /api/devices/:id                 revoke a device
POST   /api/sessions                    create a session with a device token
DELETE /api/sessions/:id                revoke with the session host secret
GET    /api/sessions/:id/ws             host/controller signaling
```

Preserve the existing `HOST_TOKEN` path temporarily as an operator recovery
credential. During migration, accept either the old operator token or a device
token. Enroll the operator laptop, verify the new flow, and only then remove or
rotate the shared token.

### Proposed data ownership

- Continue using one Durable Object instance per live session for ordered
  signaling, connection limits, and expiry.
- Use D1 or another transactional store for identities, devices, token hashes,
  revocations, and small authorization records. Do not use eventually
  consistent storage as the only source for revocation decisions.
- A short-lived Durable Object may coordinate each pending device flow if that
  makes browser approval and desktop polling simpler.
- Store aggregate operational metrics separately from identity records.
- Define retention periods before collecting any audit data.

## Abuse, privacy, and cost controls

Self-service authorization changes the threat model even though controller
traffic is small.

Minimum controls before an open campus pilot:

- one or two active sessions per device;
- four controllers per session;
- a shorter default session TTL, preferably four hours, refreshed only while
  the host is connected;
- per-IP limits on device-code creation and failed polling;
- per-identity and per-device session creation limits;
- TURN credentials issued only after authorized session creation;
- TURN credential TTL no longer than the live session TTL;
- a global session-creation kill switch that does not terminate existing games;
- a way for a user to revoke all of their device credentials;
- Cloudflare billing alerts and an operator-visible usage dashboard;
- aggregate counts for session creation, transport selection, negotiation time,
  disconnects, and relayed bytes;
- no controller state, motion samples, session secrets, IP addresses, or email
  addresses in normal application logs.

Cloudflare currently documents 1,000 GB of free Realtime transfer before
charging $0.05 per GB of egress. STUN is free. PartyPad should still measure
real bytes per player-hour before estimating campus-scale cost.

Publish a short privacy statement before recruiting users. It should explain:

- what identity assertion is used;
- what device/session metadata is stored and for how long;
- that WebRTC DataChannels are end-to-end encrypted even when TURN relays them;
- that Cloudflare necessarily processes connection metadata such as IPs and
  timing;
- how a user revokes a laptop and requests deletion.

## Cross-platform desktop work

The portable target is the same 64-bit desktop matrix Dolphin supports:

- Windows 10 and newer;
- macOS 11 and newer;
- mainstream Linux distributions.

Android Dolphin is not part of the laptop-host target.

### Separate portable and Linux-only dependencies

- Remove unconditional `evdev` installation on Windows and macOS by using a
  Linux environment marker or a `retroarch` extra.
- Lazy-import `uinput_backend.py` only when the selected backend needs it.
- Ensure importing `server.py`, selecting Dolphin, and using online mode never
  imports Linux-only packages.
- Keep `aiortc` in the online distribution. Confirm wheels and bundled native
  libraries on every release platform.
- Audit `hotspot.py` imports so an unavailable Linux networking tool affects
  only `--ap`, not normal startup.

### Cross-platform Dolphin discovery

Refactor `setup_dolphin.py` into pure discovery, patching, and CLI/UI layers.
Support:

- the normal Windows user directory under Documents;
- the macOS directory under `~/Library/Application Support/Dolphin`;
- current and legacy Linux XDG locations;
- `DOLPHIN_EMU_USERPATH`;
- portable Dolphin directories;
- Flatpak paths and an explicit user-selected directory;
- a future `--dolphin-user-dir` override used by both CLI and packaged UI.

Never guess between multiple populated Dolphin directories. Show the candidates
and ask the user, preserving the choice for future launches.

Replace `pgrep` with cross-platform process detection or a safe file-write
workflow. Preserve original backups, make repeated setup idempotent, show a
preview of files to be changed, and implement an explicit tested revert command.

Test configuration against current Dolphin stable and development builds. If
the INI schema changes, fail with a useful message instead of writing a mapping
known to be wrong.

### Application UX

A small local web dashboard is preferable to introducing a heavy native GUI
framework. The packaged executable can bind an ephemeral loopback port and open
the system browser.

The dashboard should show:

- authorization state and authorized device name;
- detected Dolphin installation/user directory;
- setup, backup, and revert status;
- system/controller selection;
- session QR and copyable URL;
- players 1-4 and reconnect state;
- current WebSocket/direct/TURN path and RTT per controller;
- actionable errors for offline service, missing Dolphin, unsupported config,
  and expired authorization;
- a clean Stop Session action.

The terminal CLI should remain supported for development and advanced users.

## Release engineering

Users must not install a development toolchain. Tagged GitHub releases should
be built on native CI runners because desktop binaries cannot be safely
cross-compiled as one generic artifact.

Initial artifacts:

- Windows x64 executable plus an installer;
- macOS Apple Silicon and Intel builds, or a universal build if all bundled
  dependencies support it;
- Linux x86-64 AppImage or standalone archive.

Release requirements:

- embedded Python runtime and all common/online dependencies;
- reproducible dependency locks;
- automated unit, syntax, Worker type, and platform smoke tests;
- artifact hashes and a software bill of materials;
- a version endpoint/protocol compatibility check;
- an in-app update notification linking to the signed GitHub release;
- Windows Authenticode signing before a broad nontechnical launch;
- Apple signing and notarization before describing macOS setup as easy;
- no automatic replacement of user files outside the tested Dolphin patcher.

Unsigned pilot builds are acceptable only if warnings and manual steps are
described honestly. Code-signing credentials belong in protected CI secrets and
must never enter the repository or an agent transcript.

## Networking and reliability hardening

The current hybrid transport remains the intended foundation. Before broad
release, add:

- a protocol version in HTTP session creation and WebSocket authentication;
- an actionable minimum-client-version response;
- WebRTC ICE restart after network changes or disrupted TURN allocations;
- periodic selected-pair and RTT updates instead of a one-time snapshot;
- correct TURN diagnostics using `relayProtocol` and preserve the distinction
  between client-to-TURN transport and the relayed peer transport;
- tested host-side fallback among Cloudflare's UDP, TCP, and TLS TURN URLs,
  accounting for aiortc's one-TURN-server behavior through controlled retry or
  an upstream-supported alternative rather than private monkeypatches;
- explicit signaling reconnection tests while an existing DataChannel remains
  alive;
- browser lifecycle handling for sleep, wake, backgrounding, and cellular/Wi-Fi
  transitions;
- structured but secret-free connection diagnostics exportable by a tester;
- Worker integration tests for device auth, quotas, session expiry, candidate
  ordering, reconnects, and revocation;
- a real-browser end-to-end WebRTC test in CI or a scheduled environment;
- backward-compatible Worker deployments or a maintenance mechanism, since a
  Durable Object deployment disconnects active signaling sockets.

Do not remove candidate types or force TURN based on one campus observation.
UMD buildings, access points, carriers, VPNs, and laptop virtual interfaces will
produce different ICE results.

## Delivery phases and gates

### Phase 1: device authorization

Deliver:

- Access-protected `@umd.edu` activation;
- device-code flow with verifier binding;
- hashed, revocable device credentials;
- migration support for the existing operator token;
- self-service device listing and revocation;
- tests for expiry, replay, polling, and unauthorized session creation.

Gate: a new UMD user can authorize a development checkout without receiving a
secret or approval from the operator.

### Phase 2: service guardrails

Deliver:

- per-device and per-identity session limits;
- rate limits and abuse responses;
- shorter active-aware session TTL;
- privacy-safe metrics, billing alerts, and kill switch;
- written privacy and acceptable-use summaries.

Gate: intentionally abusive test clients cannot generate unbounded sessions or
TURN credentials, and the operator can observe and stop new usage.

### Phase 3: portable Dolphin core

Deliver:

- platform-marked dependencies and lazy Linux imports;
- Windows/macOS/Linux Dolphin directory discovery;
- portable/Flatpak/custom-directory handling;
- cross-platform running-process protection;
- idempotent setup, preview, backup, and revert tests.

Gate: source-based PartyPad online mode and Dolphin setup pass on all three
desktop operating systems without administrator privileges.

### Phase 4: packaged application

Deliver:

- native CI release artifacts;
- first-run local dashboard;
- embedded runtime and dependencies;
- version/update checks;
- checksums and SBOM;
- initial signing/notarization plan and signed artifacts before public beta.

Gate: a clean laptop with Dolphin but no Python or developer tools reaches a
working session using only a downloaded PartyPad artifact.

### Phase 5: UMD pilot

Recruit roughly 10-25 consenting testers across:

- Windows, macOS, and Linux;
- several campus buildings and eduroam access points;
- iPhone and Android browsers;
- Verizon, AT&T, and T-Mobile where available;
- one through four controllers;
- current Dolphin stable and development versions.

Measure:

- install and first-run completion rate;
- time from launch to QR;
- time from Join to immediate relay input;
- time until WebRTC takeover;
- direct, TURN/UDP, TURN/TCP/TLS, and WebSocket proportions;
- RTT, jitter, disconnects, and stuck-input incidents;
- Dolphin configuration and revert success;
- relayed bytes per player-hour;
- authorization and update failure rates.

Gate: the targets below are met or revised from evidence, and no unresolved
high-severity security/configuration-loss issue remains.

### Phase 6: public UMD beta and institutional identity

Deliver:

- signed, documented releases;
- support/status/privacy pages;
- tested recovery and credential revocation;
- formal UMD OIDC request if stronger affiliation validation is warranted;
- migration from OTP identity to OIDC without reauthorizing every device where
  practical.

Gate: PartyPad can be shared publicly at UMD without one-to-one setup help.

## Initial service-level targets

Treat these as beta goals to validate, not current guarantees:

- fresh download, authorization, and Dolphin configuration in under five
  minutes;
- repeat launch to QR in under ten seconds;
- controller input available over WebSocket within one second after Join;
- WebRTC direct or TURN takeover within eight seconds for at least 95% of
  successful joins;
- no administrator privilege for standard online Dolphin use;
- four controllers without stuck input after ordinary packet loss;
- session recovery after brief signaling loss;
- fully reversible Dolphin changes;
- no operator interaction for authorization, ordinary use, or device
  revocation.

## Recommended implementation order

Work in this order unless a documented dependency changes:

1. Specify protocol versions and the device authorization API.
2. Implement device authorization alongside the existing `HOST_TOKEN` path.
3. Add the device database, revocation, quotas, rate limits, metrics, alerts, and
   kill switch.
4. Enroll the operator laptop through the new flow and test rollback.
5. Separate portable Dolphin dependencies from Linux AP/RetroArch features.
6. Implement and test cross-platform Dolphin discovery/setup/revert.
7. Build the first native CI artifacts and local dashboard.
8. Add reliability diagnostics and ICE restart.
9. Run the measured campus pilot.
10. Sign releases, publish support/privacy material, and open the UMD beta.
11. Pursue official UMD OIDC when the pilot establishes the need and an
    institutional sponsor/approval path.

Do not distribute a build with the current shared `HOST_TOKEN` as a shortcut.
Authentication is on the critical path before broad packaging.

## Next concrete engineering tasks

A fresh coding agent should start here:

1. Write an API/protocol design under `docs/` for versioned device
   authorization, including request/response examples, token rotation,
   revocation, and migration from `HOST_TOKEN`.
2. Decide and document the durable device store schema and retention policy.
3. Prototype the Access-protected activation path on a non-production route;
   do not put `/`, controller assets, or session WebSockets behind Access.
4. Add Worker tests before changing production authentication.
5. Implement dual authorization in `/api/sessions`: existing operator token plus
   revocable device token.
6. Add a desktop device-flow client that stores credentials through an abstract
   credential-store interface with a private-file fallback.
7. Only after that vertical slice works, begin the dependency and Dolphin-path
   cross-platform refactor.

Before any Cloudflare deployment, confirm no live game is using the service,
because deployments disconnect Durable Object WebSockets. Revoke all temporary
sessions created by integration tests. Never print or commit actual host, TURN,
device, session, Access, signing, or code-signing secrets.

## Open decisions

Resolve these with evidence and record the outcome here or in an ADR:

- whether Cloudflare Access OTP remains sufficient after the pilot or official
  UMD OIDC is required before public beta;
- D1 versus another transactional device/revocation store;
- exact device-token lifetime and rotation UX;
- whether one identity may authorize unlimited laptops or a small default cap;
- whether four-hour sessions should refresh while the authenticated host socket
  remains alive;
- packaging technology after testing aiortc and native dependency collection on
  each operating system;
- the safest host-side TURN transport retry strategy given aiortc's single TURN
  URI selection;
- Windows installer and signing provider;
- Apple universal versus architecture-specific builds and notarization budget;
- how to detect portable Dolphin installations without scanning unrelated
  directories;
- whether update notifications are sufficient initially or signed automatic
  updates are worth the additional risk and complexity;
- what support and data-retention commitments are realistic for an
  owner-operated campus service.

## Primary references

- UMD SSO and Enterprise Directory integration request; supported protocols and
  approval process:
  <https://itsupport.umd.edu/itsupport/?id=kb_article_view&sysparm_article=KB0012285>
- Cloudflare Access policies and email-domain selectors:
  <https://developers.cloudflare.com/cloudflare-one/access-controls/policies/>
- Cloudflare Access path policy behavior:
  <https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/>
- Cloudflare Realtime TURN pricing, encryption, routing, and operational notes:
  <https://developers.cloudflare.com/realtime/turn/faq/>
- Cloudflare TURN credential generation and trickle ICE guidance:
  <https://developers.cloudflare.com/realtime/turn/generate-credentials/>
- aiortc support for remote trickle candidates before remote description:
  <https://aiortc.readthedocs.io/en/stable/changelog.html>
- Dolphin supported operating systems:
  <https://dolphin-emu.org/docs/faq/>
- Dolphin global, portable, and custom user-directory behavior:
  <https://dolphin-emu.org/docs/guides/controlling-global-user-directory/>

Verify these references again when implementing a phase because service APIs,
pricing, university processes, and supported platform versions can change.

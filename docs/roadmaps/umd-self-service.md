# Public self-service and cross-platform release roadmap

Status: proposed implementation roadmap

Last updated: 2026-07-13

Canonical goal: anyone should be able to download PartyPad, authorize a laptop,
configure Dolphin, and accept phone controllers from the same network or across
the internet without contacting the service operator. UMD is the first campus
pilot and a useful place to test restrictive networks; university affiliation
is not a product eligibility requirement.

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
Verify an identity in the system browser
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
per-session QR bearer secret so friends and guests do not need accounts. Host
authorization must support people at home, parties, schools other than UMD, and
other ordinary settings; it must not depend on an `@umd.edu` address.

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
- The worktree compensates for aiortc accepting only one TURN URI at a time by
  retrying controlled UDP, TCP, then TLS URL sets while retaining STUN/direct
  candidates and the immediate WebSocket fallback. The phone diagnostic uses
  WebRTC's `relayProtocol` when available. These paths still need forced-network
  validation outside unit/integration tests.
- A home-Wi-Fi laptop and cellular phone have been verified with both direct UDP
  and TURN/UDP. Candidate forwarding and Cloudflare relay allocation have also
  been exercised independently.
- On the current multi-homed Linux development laptop, browser-side trickle ICE
  makes negotiation begin immediately; aiortc's local answer gathering commonly
  takes about five seconds because Wi-Fi, IPv6, Tailscale, and libvirt addresses
  are probed together. WebSocket input remains usable during this interval.
- Protocol v1 and its migrated production D1 database were deployed on
  2026-07-13. The old shared `HOST_TOKEN` secret was deleted. Device-code
  creation, public status/policy pages, D1 writes, custom domains, and the
  fail-closed activation boundary pass hosted smoke tests. Access browser
  approval is waiting only on the Access application/policy and its audience
  and team values.
- The deployed service implements verifier-bound device codes, Access-JWT
  browser approval, hashed single-use credentials, device revocation, session
  quotas, a kill switch, retention cleanup, and four-hour sessions. Normal
  dashboard shutdown explicitly revokes its session.
- The current worktree also has a desktop device-flow client with an abstract
  credential store, OS-keyring adapter, and private-file fallback. It no longer
  uses `HOST_TOKEN`; online authorization will become usable when the deployed
  Worker's Access configuration is completed.
- `evdev` is Linux-marked and AP/uinput imports are lazy. Dolphin configuration
  discovery, preview, atomic idempotent setup, backup, and explicit revert are
  implemented for Windows, macOS, Linux XDG/legacy, Flatpak, environment,
  portable, and explicit paths, with tests. Native platform CI and real Dolphin
  stable/development discovery are still outstanding on native platforms. The
  generated configuration has been accepted by the current Linux Flatpak
  stable build (Dolphin 2606); no standalone builds have been published.
- The worktree has privacy-safe hourly service aggregates, an Access-subject
  restricted operator metrics/kill-switch API, retention cleanup, and written
  privacy, acceptable-use, support, status, operations, and signing material.
  Account billing alerts still require operator configuration.
- A token-protected loopback dashboard implements authorization, Dolphin
  setup/revert, system selection, online start/stop, QR/link, player slots, and
  transport/RTT display. A locally built Linux PyInstaller executable passes
  packaged command smoke tests. Native tag CI is configured for Linux x64,
  Windows x64 plus Inno Setup, macOS Intel, and Apple Silicon with SBOMs,
  checksums, and draft unsigned-alpha releases; those native jobs and a
  clean-laptop flow have not yet run.
- Reliability work in the worktree includes protocol enforcement, periodic
  selected-path/RTT updates, TURN allocation diagnostics via `relayProtocol`,
  lifecycle neutralization, ICE restart, controlled host TURN URL retries, a
  secret-free browser diagnostics export, candidate ordering tests, and host
  signaling reconnect tests. An opt-in headless Firefox smoke now exercises the
  real controller page, trickled ICE, direct WebRTC DataChannel input, and
  pagehide neutralization against aiortc. Browser-matrix CI and
  network-transition field evidence remain outstanding.

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
- `device_auth.py` and `setup_online.py`: verifier-bound device enrollment,
  credential storage, local status, and credential removal.
- `dashboard.py` and `partypad.py`: loopback desktop UI and packaged entrypoint.
- `setup_dolphin.py`: cross-platform Dolphin discovery and reversible setup.
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
  or email addresses.
- Keep Dolphin configuration reversible and refuse unsafe edits while Dolphin
  is running unless the user explicitly overrides the warning.
- Keep AP mode and RetroArch optional and Linux-specific. They must not prevent
  the Dolphin/online core from running on Windows and macOS.
- Do not require administrator privileges for ordinary online Dolphin use.
- Reject obsolete clients with an actionable minimum-version response. There
  are no external users yet, so the initial self-service release does not carry
  forward the prototype's shared host credential or versionless protocol.

## Architecture decision: public host authorization

### Recommended initial identity

Use Cloudflare Access email one-time passwords on a separate authentication
host such as `auth.partypad.benmross.com`. The authorization layer must accept
any supported verified email address, not just a university domain. If access
must be limited while capacity and abuse controls are being proven, use an
explicit pilot cohort or invite mechanism that can be removed without changing
the identity or device-token model.

This is recommended initially because it:

- verifies control of an email address without the operator issuing a shared
  credential;
- can be deployed independently of a formal university application review;
- keeps authentication in the browser, where email OTP is familiar;
- allows the desktop application to use an OAuth-style device flow without
  collecting a password.

Email-domain verification does not necessarily prove current enrollment or
employment, nor does a verified email prove a real-world identity. PartyPad
does not need either property for ordinary public use. Treat email verification
as an account-recovery and abuse-control signal, not as proof that someone is
entitled to use the software.

### Optional institutional identity providers

Official UMD OIDC may be useful for a UMD-sponsored pilot if the project needs
authoritative current-student or employee status. It is not the preferred or
required identity for the general service, and adding it must not prevent users
from other colleges or outside academia from authorizing devices. UMD supports
OIDC, CAS, and SAML, but applications must be registered and reviewed. That
process may require a Unit Head plus data steward and security/compliance
approval.

Request only authentication and the minimum stable subject identifier. Do not
request student records, course membership, contact data, or other directory
attributes unless a concrete feature later requires them.

The authorization service should use a provider-neutral internal subject so
additional OIDC providers can be linked without changing desktop device tokens
or public controller joins. Do not make a UMD-specific identifier part of the
session protocol or stored device credential.

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
       |                           email OTP / OIDC
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
controller QR path behind host login or restrict it by email domain.

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

Remove the existing `HOST_TOKEN` path when device authorization lands. Nobody
currently depends on the prototype, so retaining a universal recovery
credential would add risk without providing compatibility value. Recovery must
use the same per-device enrollment and revocation model as every other host.

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

Minimum controls before an open campus pilot, and required controls before a
general public beta:

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
real bytes per player-hour before estimating public-service cost. Capacity and
spend limits should fail closed for new sessions with an honest status message;
they must not be implemented by permanently restricting eligibility to UMD.

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
- a maintenance mechanism for future Worker deployments, since a Durable Object
  deployment disconnects active signaling sockets.

Do not remove candidate types or force TURN based on one campus observation.
UMD buildings, access points, carriers, VPNs, and laptop virtual interfaces will
produce different ICE results.

## Delivery phases and gates

Implementation status is tracked conservatively: local code and tests do not
satisfy a phase gate that requires configured cloud infrastructure, native OS
CI, packaged artifacts, or pilot evidence.

### Phase 1: device authorization

Deliver:

- activation for any supported verified email identity;
- provider-neutral internal identities, with optional cohort/invite controls
  kept separate from identity eligibility;
- device-code flow with verifier binding;
- hashed, revocable device credentials;
- removal of the existing operator-token path;
- self-service device listing and revocation;
- tests for expiry, replay, polling, and unauthorized session creation.

Gate: a new user can authorize a development checkout without receiving a
shared secret or one-to-one approval from the operator. A temporary pilot
cohort may control rollout, but the flow and data model are not UMD-specific.

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

### Phase 5: UMD-led pilot

Recruit roughly 10-25 consenting testers, beginning with the UMD community but
including off-campus use and, where practical, testers with no UMD affiliation.
Cover:

- Windows, macOS, and Linux;
- several campus buildings and eduroam access points;
- home networks, parties, and at least one non-UMD school or similarly
  restrictive network;
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

### Phase 6: public beta

Deliver:

- signed, documented releases;
- support/status/privacy pages;
- tested recovery and credential revocation;
- removal of any pilot-only cohort or university-domain eligibility rule;
- capacity limits, abuse controls, and graceful new-session throttling sized
  from pilot measurements;
- optional UMD OIDC for UMD-specific pilot or community features if stronger
  affiliation validation is warranted, without making it the only login path;
- account linking from OTP identity to optional OIDC providers without
  reauthorizing every device where practical.

Gate: PartyPad can be downloaded and used without one-to-one setup help by
people at home, at parties, at UMD or another college, or elsewhere. Remaining
restrictions are documented technical, safety, abuse, or capacity limits—not
university affiliation requirements.

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
2. Implement device authorization and remove the existing `HOST_TOKEN` path.
3. Add the device database, revocation, quotas, rate limits, metrics, alerts, and
   kill switch.
4. Enroll the operator laptop through the new flow and test rollback.
5. Separate portable Dolphin dependencies from Linux AP/RetroArch features.
6. Implement and test cross-platform Dolphin discovery/setup/revert.
7. Build the first native CI artifacts and local dashboard.
8. Add reliability diagnostics and ICE restart.
9. Run the measured UMD-led pilot as the first rollout cohort, including home
   and non-UMD network tests.
10. Sign releases, publish support/privacy material, remove pilot eligibility
    restrictions, and open the general public beta.
11. Pursue official UMD OIDC only if a UMD-specific need and an institutional
    sponsor/approval path emerge; retain the general login path.

Do not distribute a build with the current shared `HOST_TOKEN` as a shortcut.
Authentication is on the critical path before broad packaging.

## Next concrete engineering tasks

A fresh coding agent should start here:

1. Complete the Access application/policy on the authentication hostname, set
   its audience/team secrets, and exercise activation without protecting `/`,
   controller assets, or session WebSockets. D1, custom domains, migrations,
   the hashing secret, and deletion of the old shared host secret are complete.
2. Run the complete Worker regression suite against a preview deployment,
   including Access activation, one-time verifier consumption, revocation,
   identity/device caps, session alarms, signaling reconnect, retention, and
   the public policy/status surfaces now covered locally.
3. Run the configured Windows, macOS Intel/Apple Silicon, and Linux CI for
   imports, packaged runtime collection, and Dolphin discovery/setup;
   test the generated mapping with current Dolphin stable and development
   builds on each platform.
4. Add the Firefox WebRTC smoke to a controlled browser-matrix job, then
   exercise sleep/wake, Wi-Fi/cellular changes, and forced UDP/TCP/TLS TURN
   retry in a scheduled network environment.
5. After cloud authorization is configured, run the clean-laptop dashboard
   flow end to end and use the resulting evidence to revise packaging and
   service-level targets.

Before any Cloudflare deployment, confirm no live game is using the service,
because deployments disconnect Durable Object WebSockets. Revoke all temporary
sessions created by integration tests. Never print or commit actual host, TURN,
device, session, Access, signing, or code-signing secrets.

## Open decisions

Resolve these with evidence and record the outcome here or in an ADR:

- whether Cloudflare Access OTP is suitable for general public authorization or
  a provider-neutral authentication service is needed before public beta;
- how temporary pilot cohorts or invites are represented without coupling them
  to email domains or permanent account eligibility;
- whether optional UMD OIDC provides enough value for UMD-specific features to
  justify its registration and support cost;
- whether the initial 90-day device-token lifetime, 14-day rotation threshold,
  and 24-hour overlap specified in the protocol are appropriate after pilot
  evidence;
- whether the initial ten-active-laptop identity cap should change after pilot
  evidence;
- whether four-hour sessions should refresh while the authenticated host socket
  remains alive (the current implementation uses a fixed four-hour expiry);
- whether PyInstaller remains the packaging technology after its configured
  native jobs test aiortc and native dependency collection on every target;
- the safest host-side TURN transport retry strategy given aiortc's single TURN
  URI selection;
- Windows installer and signing provider;
- Apple universal versus architecture-specific builds and notarization budget;
- how to detect portable Dolphin installations without scanning unrelated
  directories;
- whether update notifications are sufficient initially or signed automatic
  updates are worth the additional risk and complexity;
- what support, capacity, and data-retention commitments are realistic for an
  owner-operated public service.

Resolved for the initial implementation: use D1 as the transactional identity,
device, revocation, authorization, quota, and kill-switch store. Revisit only
if measured consistency, latency, or operational constraints contradict that
choice.

Resolved for initial artifact experiments: use PyInstaller one-file builds,
Inno Setup for the unsigned Windows alpha installer, and tar archives on Linux
and macOS. This is provisional until native CI and clean-laptop tests provide
evidence; it is not a decision to skip platform signing or notarization.

## Primary references

- UMD SSO and Enterprise Directory integration request; supported protocols and
  approval process if optional UMD identity is later added:
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

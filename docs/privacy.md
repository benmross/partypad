# PartyPad privacy summary

Status: pre-pilot draft; publish and review before recruiting testers.

PartyPad uses a verified identity only to authorize and revoke laptop devices.
Friends who join a controller session do not create accounts. Their random join
secret stays in the URL fragment and is not sent in ordinary HTTP requests.

## Data PartyPad stores

- A provider name and keyed pseudonymous subject for the laptop owner. PartyPad
  does not persist the plaintext email address supplied to Cloudflare Access.
- Laptop name, platform, client version, authorization/last-use timestamps,
  expiry, and revocation state.
- Keyed hashes of device credentials and short-lived authorization codes. Raw
  credentials are returned once and are not stored by the service.
- Live session ownership, creation, expiry, and end timestamps for quota and
  cleanup purposes.
- Hourly aggregate counts and sums for sessions, joins, disconnects, transport
  type, negotiation time, RTT, and WebSocket-relayed bytes. These aggregates
  contain no identity, device, session, IP, email, secret, or controller-state
  dimension.

Normal application logs must not contain email addresses, IP addresses,
identity assertions, device/user codes, device or session tokens, join URLs,
controller buttons, motion samples, or TURN credentials. Cloudflare necessarily
processes network metadata such as IP addresses and request timing as the
infrastructure provider under its own terms.

## Controller traffic

WebRTC DataChannels are encrypted end to end between the phone browser and the
laptop, including when Cloudflare TURN relays packets. While WebRTC connects,
or if it cannot connect, controller state is forwarded through the session's
authenticated Cloudflare Durable Object WebSocket. PartyPad does not retain
controller state in its database or aggregate metrics.

## Retention

- Pending device authorizations are removed within 24 hours after expiry or use.
- Live session ownership rows are removed within 24 hours after expiry.
- Revoked device records and an identity with no remaining devices are removed
  after 30 days; credential hashes are deleted when the device is revoked.
- Rate-limit buckets are removed within 24 hours after their window.
- Privacy-safe hourly aggregates are retained for 13 months.

## Control and deletion

An authenticated user can list devices, revoke one device, or revoke all of
their devices. Removing a credential only from the laptop does not revoke the
server copy; use the authenticated device-management page as well. Before that
page is publicly deployed, deletion requests require private contact through
the repository's security-reporting channel in `SECURITY.md` so identity data
is not posted in a public issue.

This is an early-alpha service description, not a promise that the public beta
is already available. Material collection or retention changes must update this
document before a pilot uses them.

# Device authorization and protocol v1

Status: implementation contract for phases 1 and 2

Last updated: 2026-07-13

This document defines the public desktop authorization API. It is the contract
between packaged/source desktop clients, the public session Worker, and the
browser activation surface. It deliberately does not make a university email
address or any other institutional identifier part of the protocol.

## Trust boundaries

- `partypad.benmross.com` serves controller assets and the public session API.
  Controller joins stay public and are authorized only by the random secret in
  the URL fragment.
- `auth.partypad.benmross.com` serves activation and device-management pages
  behind an identity-aware proxy. The Worker must validate the proxy assertion;
  a caller-supplied email header is never an identity assertion.
- Desktop clients never receive identity-provider tokens or plaintext identity
  values. They receive an opaque, device-specific PartyPad credential.
- There is no universal desktop credential or operator-token bypass.

All JSON API responses use `Cache-Control: no-store`. Secrets, verifier values,
user codes, identity assertions, and controller state must not be logged.

## Protocol negotiation

Version 1 clients send this header on every desktop HTTP request:

```http
X-PartyPad-Protocol: 1
```

They also send `"protocol": 1` in the first WebSocket authentication message.
A missing or unsupported version is rejected. HTTP clients receive:

```http
HTTP/1.1 426 Upgrade Required
Content-Type: application/json

{
  "error": "client_upgrade_required",
  "message": "This PartyPad version is no longer supported.",
  "minimum_protocol": 1,
  "download_url": "https://github.com/benmross/partypad/releases/latest"
}
```

`GET /config` advertises `protocol.current`, `protocol.minimum`, and the latest
release URL. Additive response fields do not require a protocol bump. Removing
or changing the meaning of a field, authentication method, or signaling message
does.

## Device-code flow

### Create an authorization

`POST /api/device/authorizations` is public and rate-limited by source IP. The
desktop generates a 32-byte verifier and sends only its SHA-256 digest.

```json
{
  "verifier_hash": "sha256-base64url-without-padding",
  "device_name": "Ben's MacBook Pro",
  "platform": "macos",
  "client_version": "0.2.0"
}
```

Names are UTF-8, trimmed, and limited to 80 characters. Platform is one of
`windows`, `macos`, or `linux`; version is limited to 40 characters. A success
response is `201`:

```json
{
  "device_code": "opaque-32-byte-base64url-value",
  "user_code": "ABCD-EFGH",
  "verification_uri": "https://auth.partypad.benmross.com/activate",
  "verification_uri_complete": "https://auth.partypad.benmross.com/activate?code=ABCD-EFGH",
  "expires_in": 600,
  "interval": 5
}
```

The service stores HMAC hashes of both codes, the supplied verifier digest,
bounded device metadata, timestamps, and status. It never stores either raw
code. User-code collisions are retried. A pending authorization expires after
10 minutes.

### Browser approval

`GET /activate?code=ABCD-EFGH` and `POST /activate` exist only on the protected
authentication hostname. Before approval the page shows the normalized code,
device name, platform, creation time, and expiry. The POST includes the code and
an anti-CSRF value bound to the authenticated browser session.

The Worker validates the identity assertion and maps `(provider, subject)` to a
provider-neutral internal identity. For email OTP, `subject` is a keyed HMAC of
the normalized address; plaintext email is not persisted. Pilot cohort/invite
checks, if enabled, are a separate authorization policy after identity lookup.

Approval atomically changes `pending` to `approved` and records the approving
identity. Rejecting changes it to `denied`. Repeated approval, expired codes,
and already-consumed codes do not issue credentials.

### Poll and consume

The desktop polls `GET /api/device/authorizations/{device_code}` with:

```http
Authorization: Verifier <original-32-byte-verifier>
X-PartyPad-Protocol: 1
```

The service hashes the path code with its server key and hashes the verifier
with SHA-256 before constant-time comparison. Polls faster than `interval`
receive `429` plus `Retry-After`. Responses are:

```json
{ "status": "pending", "interval": 5, "expires_in": 523 }
```

```json
{ "status": "denied" }
```

On the first approved poll, one transaction creates a device and consumes the
authorization. The only response containing the credential is:

```json
{
  "status": "authorized",
  "device": {
    "id": "dev_opaque-public-id",
    "name": "Ben's MacBook Pro",
    "expires_at": "2026-10-11T18:00:00Z"
  },
  "device_token": "ppd_opaque-random-value"
}
```

A later poll returns `410 authorization_consumed` and can never replay the
token. Device tokens last 90 days initially. A successfully authenticated use
inside the last 14 days rotates a token that expires in fewer than 14 days; the
session response carries the replacement once, in `rotated_device_token` and
`device_token_expires_at`. The desktop must save the replacement before
discarding the prior token. The old token remains valid for a 24-hour overlap
unless revoked, preventing a crash during storage from locking out the device.
An identity may keep up to ten active laptops initially; authorizing another
returns `409 device_limit` until an unused device is revoked. This is an abuse
guardrail, not an identity-provider or university eligibility rule.

## Authorized session creation

`POST /api/sessions` keeps its existing controller-configuration body and
requires:

```http
Authorization: Device ppd_opaque-random-value
```

Device authentication checks the keyed token hash, expiry, device revocation,
identity revocation, active-session quota, and service kill switch before TURN
credentials are requested. A successful session is owned by the device so it
can be counted and expired. The join URL and host secret remain session-scoped
bearer credentials and are unchanged.

`401` means the credential is missing or invalid. `403 device_revoked` and
`403 identity_revoked` require a new authorization or identity recovery.
`409 session_limit` means an existing session must be stopped. `429` includes a
`Retry-After` value. `503 new_sessions_disabled` is the operator kill switch and
must not disconnect existing sessions.

## Device management

The protected authentication hostname provides:

- `GET /api/devices`: active and recently revoked devices for the authenticated
  identity; never returns credential hashes.
- `DELETE /api/devices/{id}`: revoke one owned device and its outstanding token
  generations.
- `DELETE /api/devices`: revoke all devices for the identity.

Revocation takes effect for new session creation immediately. Device records
use opaque public IDs, so sequential database IDs are never exposed.

## Errors and bounds

Errors use a stable machine code and a human-readable message:

```json
{ "error": "invalid_request", "message": "device_name is too long" }
```

Request bodies are limited to 8 KiB for device APIs and 16 KiB for session
creation. Unknown fields are ignored for forward compatibility; known fields
with the wrong type are rejected. Code creation, failed verifier checks, and
session creation are independently rate-limited. Comparisons of secret hashes
must be timing-safe.

## Retention and privacy

- Pending authorization rows: delete within 24 hours after expiry/consumption.
- Device records: retain while active; delete credential hashes immediately on
  revocation and delete tombstones after 30 days.
- Identity records: delete 30 days after the last device is removed unless an
  abuse/security hold with a documented expiry applies.
- Session ownership/quota rows: delete within 24 hours after session expiry.
- Rate-limit buckets: expire within 24 hours.
- Aggregate metrics: retain for 13 months and contain no IP, identity, device,
  controller, session-secret, or credential dimensions.

Cloudflare may process network metadata as infrastructure provider. PartyPad's
normal application logs must not record IP addresses, raw identity assertions,
email addresses, device/user codes, tokens, join URLs, or controller payloads.

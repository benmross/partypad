# Hosted service operations runbook

This runbook covers safe, secret-free operations. Never paste live secrets,
Access assertions, join URLs, device tokens, or TURN credentials into issues,
logs, or command transcripts.

## Initial account configuration

1. The production `partypad-devices` database is already bound and migrated.
   For a replacement account, create it with `wrangler d1 create`, update the
   ID in `cloudflare/wrangler.jsonc`, and apply every migration remotely.
2. Protect only `auth.partypad.benmross.com` with Cloudflare Access. Leave the
   controller hostname, assets, session sockets, and device create/poll APIs
   public.
3. Configure `AUTH_HASH_KEY`, `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD`, and
   `ADMIN_ACCESS_SUB` as Worker secrets. Configure the scoped TURN issuer values.
4. Set Cloudflare billing notifications and a conservative Realtime budget in
   the account dashboard. This account-owned control cannot be configured or
   verified from the repository.
5. Run `npm run check`, `npm test`, and a Wrangler dry-run. Confirm no active
   game, announce maintenance, deploy, then exercise authorization, revocation,
   session creation/end, WebSocket fallback, and WebRTC without logging secrets.

## Pause and resume new sessions

The kill switch affects new sessions only. It does not disconnect current play.
From `cloudflare/`, after authenticating Wrangler to the correct account:

```sh
npx wrangler d1 execute partypad-devices --remote \
  --command "UPDATE service_settings SET value='false', updated_at=unixepoch() WHERE key='new_sessions_enabled'"
```

Resume by setting `value='true'`. Confirm state without exposing credentials:

```sh
npx wrangler d1 execute partypad-devices --remote \
  --command "SELECT value, updated_at FROM service_settings WHERE key='new_sessions_enabled'"
```

The Access-subject-restricted `/api/admin/new-sessions` API provides the same
control for a future operator UI. If the setting is missing or unreadable,
session creation fails closed.

## Usage review

The restricted `/api/admin/metrics?hours=168` endpoint and the
`aggregate_metrics` D1 table expose only hourly aggregate dimensions. Review
session volume, TURN/direct/WebSocket proportions, negotiation time, RTT,
disconnects, and relayed bytes weekly during a pilot. Compare Cloudflare's
billing dashboard to PartyPad aggregates; the application estimate is not a
billing source of truth.

Investigate unexplained growth by pausing new sessions first. Do not add raw IP,
identity, session, or controller logging as an incident shortcut.

## Deployment and rollback

A Worker deployment disconnects Durable Object WebSockets. Before deploying,
query active unexpired `session_owners`, confirm with testers that no live game
is running, and pause new sessions if necessary. Apply migrations before code
that requires them. Keep the prior Git revision and use a normal redeploy for
rollback; do not delete D1 tables or Durable Objects. Re-run the smoke flow after
either deploy or rollback and revoke every temporary test session.

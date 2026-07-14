# PartyPad Cloudflare service

This Worker serves the controller UI, coordinates short-lived sessions with a
Durable Object, and provisions Cloudflare Realtime TURN credentials. Controller
input normally travels over WebRTC; the signaling WebSocket is also the
reliability fallback on networks that block WebRTC completely.

The browser sends its offer immediately and trickles candidates through the
Durable Object. The Python host accepts candidates before or during offer
processing, then returns aiortc's complete answer. Preserve the WebSocket input
path during negotiation.

The Cloudflare build/deploy toolchain is pinned to Node 22.23.1. With `nvm`
installed, select it before installing dependencies:

```sh
nvm install
npm ci
```

The production `partypad-devices` D1 database is bound in `wrangler.jsonc` and
its migrations were applied on 2026-07-13. For a replacement account or local
database, create the database and apply the schema:

```sh
npx wrangler d1 create partypad-devices
npx wrangler d1 migrations apply partypad-devices --local
npx wrangler d1 migrations apply partypad-devices --remote
```

Configure Cloudflare Access on `auth.partypad.benmross.com` without protecting
the controller hostname. Set `ACCESS_TEAM_DOMAIN` to the Access team subdomain
(without `.cloudflareaccess.com`) and `ACCESS_AUD` to the application audience.
Upload those values and a new random `AUTH_HASH_KEY` as Worker secrets; never
reuse or print the hash key. Then upload the two Cloudflare Realtime TURN values:

```sh
npx wrangler secret put AUTH_HASH_KEY
npx wrangler secret put ACCESS_TEAM_DOMAIN
npx wrangler secret put ACCESS_AUD
npx wrangler secret put TURN_KEY_ID
npx wrangler secret put TURN_KEY_API_TOKEN
```

The Access policy must permit the supported verified identities on only the
authentication hostname. The Worker cryptographically verifies the Access JWT;
forwarding an email header is not sufficient. The public controller assets,
session WebSockets, `/config`, and device-code creation/polling stay outside
Access.

Create a Cloudflare Realtime TURN key in **Realtime → TURN**. Use the returned
TURN key `uid` as `TURN_KEY_ID` and its one-time `key` value as
`TURN_KEY_API_TOKEN`. These are the scoped TURN credential issuer values, not a
general Cloudflare API token. If they are omitted in local development, the
service advertises Cloudflare STUN only; production should always configure
both so restrictive networks have a relay.

Install with `npm ci`, run locally with `npm run dev`, validate with
`npm run check && npm test`, and deploy with `npm run deploy`. A deploy
disconnects active Durable Object sockets, so confirm no game is active first.

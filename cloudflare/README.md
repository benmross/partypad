# PartyPad Cloudflare service

This Worker serves the controller UI, coordinates short-lived sessions with a
Durable Object, and provisions Cloudflare Realtime TURN credentials. Controller
input normally travels over WebRTC; the signaling WebSocket is also the
reliability fallback on networks that block WebRTC completely.

The browser sends its offer immediately and trickles candidates through the
Durable Object. The Python host accepts candidates before or during offer
processing, then returns aiortc's complete answer. Keep candidate forwarding
backward-compatible and preserve the WebSocket input path during negotiation.

The Cloudflare build/deploy toolchain is pinned to Node 22.23.1. With `nvm`
installed, select it before installing dependencies:

```sh
nvm install
npm ci
```

Generate and upload the host credential from the repository root:

```sh
uv run python setup_online.py
```

Then upload the two Cloudflare Realtime TURN values from this directory:

```sh
npx wrangler secret put TURN_KEY_ID
npx wrangler secret put TURN_KEY_API_TOKEN
```

The setup script stores the matching host token in the user's private config
directory and sends it to the Worker without printing it.

Create a Cloudflare Realtime TURN key in **Realtime → TURN**. Use the returned
TURN key `uid` as `TURN_KEY_ID` and its one-time `key` value as
`TURN_KEY_API_TOKEN`. These are the scoped TURN credential issuer values, not a
general Cloudflare API token. If they are omitted in local development, the
service advertises Cloudflare STUN only; production should always configure
both so restrictive networks have a relay.

Install with `npm ci`, run locally with `npm run dev`, validate with
`npm run check`, and deploy with `npm run deploy`. The Wrangler configuration
deploys directly to the `partypad.benmross.com` custom domain.

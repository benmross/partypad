import { DurableObject } from "cloudflare:workers";

interface Env {
  ASSETS: Fetcher;
  SESSIONS: DurableObjectNamespace<PartySession>;
  HOST_TOKEN: string;
  TURN_KEY_ID?: string;
  TURN_KEY_API_TOKEN?: string;
}

interface ControllerConfig {
  system: string;
  system_name: string;
  controller_mode: string;
  backend: string;
}

interface IceServer {
  urls: string | string[];
  username?: string;
  credential?: string;
}

interface SessionRecord {
  hostHash: string;
  joinHash: string;
  expiresAt: number;
  config: ControllerConfig;
  iceServers: IceServer[];
}

interface SocketAttachment {
  role: "pending" | "host" | "controller";
  peer: string;
  client: string;
}

const SESSION_TTL_SECONDS = 8 * 60 * 60;
const CLIENT_ID_RE = /^[A-Za-z0-9_-]{16,128}$/;

function json(data: unknown, status = 200): Response {
  return Response.json(data, {
    status,
    headers: {
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

function secureAsset(response: Response): Response {
  const secured = new Response(response.body, response);
  secured.headers.set(
    "Content-Security-Policy",
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
      "connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
  );
  secured.headers.set("Permissions-Policy", "accelerometer=(self), gyroscope=(self)");
  secured.headers.set("Referrer-Policy", "no-referrer");
  secured.headers.set("X-Content-Type-Options", "nosniff");
  secured.headers.set("X-Frame-Options", "DENY");
  return secured;
}

function randomSecret(bytes = 24): string {
  const value = new Uint8Array(bytes);
  crypto.getRandomValues(value);
  return btoa(String.fromCharCode(...value))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

async function sha256(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function validConfig(value: unknown): value is ControllerConfig {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return ["system", "system_name", "controller_mode", "backend"].every(
    (key) => typeof record[key] === "string" && (record[key] as string).length <= 80,
  );
}

async function turnServers(env: Env): Promise<IceServer[]> {
  if (!env.TURN_KEY_ID || !env.TURN_KEY_API_TOKEN) {
    return [{ urls: ["stun:stun.cloudflare.com:3478"] }];
  }
  const response = await fetch(
    `https://rtc.live.cloudflare.com/v1/turn/keys/${env.TURN_KEY_ID}/credentials/generate-ice-servers`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.TURN_KEY_API_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ttl: SESSION_TTL_SECONDS }),
    },
  );
  if (!response.ok) {
    throw new Error(`TURN credential request failed with HTTP ${response.status}`);
  }
  const result = (await response.json()) as { iceServers?: IceServer[] };
  if (!Array.isArray(result.iceServers)) throw new Error("TURN response omitted iceServers");
  return result.iceServers;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/config") {
      return json({ online_service: true });
    }
    if (request.method === "POST" && url.pathname === "/api/sessions") {
      if (!env.HOST_TOKEN) return json({ error: "service is not configured" }, 503);
      if (request.headers.get("Authorization") !== `Bearer ${env.HOST_TOKEN}`) {
        return json({ error: "unauthorized" }, 401);
      }
      let config: unknown;
      try {
        config = await request.json();
      } catch {
        return json({ error: "invalid JSON" }, 400);
      }
      if (!validConfig(config)) return json({ error: "invalid controller configuration" }, 400);

      const id = randomSecret(12);
      const hostSecret = randomSecret(32);
      const joinSecret = randomSecret(24);
      const expiresAt = Date.now() + SESSION_TTL_SECONDS * 1000;
      let iceServers: IceServer[];
      try {
        iceServers = await turnServers(env);
      } catch (error) {
        console.error(error);
        return json({ error: "could not provision the real-time relay" }, 502);
      }

      const stub = env.SESSIONS.getByName(id);
      const initialized = await stub.fetch("https://session.internal/initialize", {
        method: "POST",
        body: JSON.stringify({
          hostHash: await sha256(hostSecret),
          joinHash: await sha256(joinSecret),
          expiresAt,
          config,
          iceServers,
        } satisfies SessionRecord),
      });
      if (!initialized.ok) return json({ error: "could not initialize session" }, 500);

      const origin = url.origin;
      return json(
        {
          id,
          host_secret: hostSecret,
          join_url: `${origin}/#/join/${id}/${joinSecret}`,
          ws_url: `${origin.replace(/^http/, "ws")}/api/sessions/${id}/ws`,
          end_url: `${origin}/api/sessions/${id}`,
          ice_servers: iceServers,
          expires_at: new Date(expiresAt).toISOString(),
        },
        201,
      );
    }

    const match = url.pathname.match(/^\/api\/sessions\/([A-Za-z0-9_-]{16,32})\/ws$/);
    if (request.method === "GET" && match) {
      return env.SESSIONS.getByName(match[1]).fetch(request);
    }
    const sessionMatch = url.pathname.match(/^\/api\/sessions\/([A-Za-z0-9_-]{16,32})$/);
    if (request.method === "DELETE" && sessionMatch) {
      const stub = env.SESSIONS.getByName(sessionMatch[1]);
      return stub.fetch("https://session.internal/end", {
        method: "DELETE",
        body: await request.text(),
      });
    }
    if (url.pathname.startsWith("/api/")) return json({ error: "not found" }, 404);
    if (url.pathname.startsWith("/static/")) {
      const assetUrl = new URL(request.url);
      assetUrl.pathname = url.pathname.slice("/static".length);
      return secureAsset(await env.ASSETS.fetch(new Request(assetUrl, request)));
    }
    return secureAsset(await env.ASSETS.fetch(request));
  },
} satisfies ExportedHandler<Env>;

export class PartySession extends DurableObject<Env> {
  private record: SessionRecord | null = null;

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    state.blockConcurrencyWhile(async () => {
      this.record = (await state.storage.get<SessionRecord>("session")) ?? null;
    });
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/initialize" && request.method === "POST") {
      if (this.record !== null) return new Response("already initialized", { status: 409 });
      const record = (await request.json()) as SessionRecord;
      if (!record.hostHash || !record.joinHash || record.expiresAt <= Date.now()) {
        return new Response("invalid session", { status: 400 });
      }
      this.record = record;
      await this.ctx.storage.put("session", record);
      await this.ctx.storage.setAlarm(record.expiresAt);
      return new Response(null, { status: 204 });
    }
    if (url.pathname === "/end" && request.method === "DELETE") {
      if (!this.record) return new Response(null, { status: 204 });
      let secret = "";
      try {
        const body = (await request.json()) as { secret?: unknown };
        if (typeof body.secret === "string") secret = body.secret;
      } catch {
        return new Response("invalid JSON", { status: 400 });
      }
      if ((await sha256(secret)) !== this.record.hostHash) {
        return new Response("unauthorized", { status: 401 });
      }
      await this.expire("session ended");
      return new Response(null, { status: 204 });
    }
    if (url.pathname.endsWith("/ws") && request.headers.get("Upgrade") === "websocket") {
      if (!this.record || this.record.expiresAt <= Date.now()) {
        return new Response("session expired", { status: 410 });
      }
      const pair = new WebSocketPair();
      const [client, server] = Object.values(pair);
      this.ctx.acceptWebSocket(server);
      server.serializeAttachment({
        role: "pending",
        peer: randomSecret(12),
        client: "",
      } satisfies SocketAttachment);
      server.send(JSON.stringify({ t: "hello" }));
      return new Response(null, { status: 101, webSocket: client });
    }
    return new Response("not found", { status: 404 });
  }

  async webSocketMessage(ws: WebSocket, raw: string | ArrayBuffer): Promise<void> {
    if (typeof raw !== "string" || raw.length > 128_000 || !this.record) {
      ws.close(1008, "invalid message");
      return;
    }
    let message: Record<string, unknown>;
    try {
      message = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      ws.close(1008, "invalid JSON");
      return;
    }
    const attachment = ws.deserializeAttachment() as SocketAttachment;
    if (attachment.role === "pending") {
      await this.authenticate(ws, attachment, message);
      return;
    }
    if (attachment.role === "host") this.fromHost(message);
    else this.fromController(ws, attachment, message);
  }

  async webSocketClose(ws: WebSocket): Promise<void> {
    const attachment = ws.deserializeAttachment() as SocketAttachment | null;
    if (attachment?.role === "controller") {
      this.sendHost({ t: "peer_leave", peer: attachment.peer });
    }
  }

  async alarm(): Promise<void> {
    await this.expire("session expired");
  }

  private async expire(reason: string): Promise<void> {
    for (const ws of this.ctx.getWebSockets()) ws.close(1000, reason);
    this.record = null;
    await this.ctx.storage.deleteAll();
  }

  private sockets(role?: SocketAttachment["role"]): Array<[WebSocket, SocketAttachment]> {
    return this.ctx
      .getWebSockets()
      .map((ws) => [ws, ws.deserializeAttachment() as SocketAttachment] as [WebSocket, SocketAttachment])
      .filter(([, attachment]) => !role || attachment.role === role);
  }

  private async authenticate(
    ws: WebSocket,
    attachment: SocketAttachment,
    message: Record<string, unknown>,
  ): Promise<void> {
    if (message.t !== "auth" || typeof message.secret !== "string") {
      ws.close(1008, "authenticate first");
      return;
    }
    if (message.role === "host" && (await sha256(message.secret)) === this.record!.hostHash) {
      for (const [other] of this.sockets("host")) other.close(1000, "host reconnected");
      attachment.role = "host";
      ws.serializeAttachment(attachment);
      ws.send(JSON.stringify({ t: "auth_ok" }));
      ws.send(
        JSON.stringify({
          t: "peers",
          peers: this.sockets("controller").map(([, item]) => ({
            peer: item.peer,
            client: item.client,
          })),
        }),
      );
      return;
    }
    if (
      message.role === "controller" &&
      (await sha256(message.secret)) === this.record!.joinHash &&
      typeof message.client === "string" &&
      CLIENT_ID_RE.test(message.client)
    ) {
      const controllers = this.sockets("controller");
      for (const [other, item] of controllers) {
        if (item.client === message.client) other.close(1000, "controller reconnected");
      }
      if (controllers.filter(([, item]) => item.client !== message.client).length >= 4) {
        ws.send(JSON.stringify({ t: "full" }));
        ws.close(1008, "session full");
        return;
      }
      attachment.role = "controller";
      attachment.client = message.client;
      ws.serializeAttachment(attachment);
      ws.send(
        JSON.stringify({
          t: "auth_ok",
          config: this.record!.config,
          ice_servers: this.record!.iceServers,
        }),
      );
      this.sendHost({ t: "peer_join", peer: attachment.peer, client: attachment.client });
      return;
    }
    ws.close(1008, "authentication failed");
  }

  private fromHost(message: Record<string, unknown>): void {
    if (typeof message.peer !== "string") return;
    const target = this.sockets("controller").find(([, item]) => item.peer === message.peer)?.[0];
    if (!target) return;
    if (message.t === "answer" && typeof message.sdp === "string") {
      target.send(JSON.stringify({ t: "answer", sdp: message.sdp }));
    } else if (message.t === "control" && message.message && typeof message.message === "object") {
      target.send(JSON.stringify({ t: "control", message: message.message }));
    }
  }

  private fromController(
    _ws: WebSocket,
    attachment: SocketAttachment,
    message: Record<string, unknown>,
  ): void {
    if (message.t === "offer" && typeof message.sdp === "string" && message.sdp.length <= 128_000) {
      this.sendHost({ t: "offer", peer: attachment.peer, sdp: message.sdp });
    } else if (
      message.t === "candidate" &&
      (message.candidate === null ||
        (message.candidate && typeof message.candidate === "object"))
    ) {
      this.sendHost({ t: "candidate", peer: attachment.peer, candidate: message.candidate });
    } else if (
      message.t === "input" &&
      Number.isSafeInteger(message.seq) &&
      message.data &&
      typeof message.data === "object"
    ) {
      this.sendHost({ t: "input", peer: attachment.peer, seq: message.seq, data: message.data });
    }
  }

  private sendHost(message: unknown): void {
    const host = this.sockets("host")[0]?.[0];
    if (host) host.send(JSON.stringify(message));
  }
}

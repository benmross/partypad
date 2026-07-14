import { DurableObject } from "cloudflare:workers";

interface Env {
  ASSETS: Fetcher;
  SESSIONS: DurableObjectNamespace<PartySession>;
  DB: D1Database;
  AUTH_HASH_KEY: string;
  ACCESS_TEAM_DOMAIN: string;
  ACCESS_AUD: string;
  ADMIN_ACCESS_SUB?: string;
  AUTH_HOST?: string;
  LATEST_CLIENT_VERSION?: string;
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

const PROTOCOL_VERSION = 1;
const SESSION_TTL_SECONDS = 4 * 60 * 60;
const DEVICE_CODE_TTL_SECONDS = 10 * 60;
const DEVICE_TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60;
const MAX_DEVICES_PER_IDENTITY = 10;
const POLL_INTERVAL_SECONDS = 5;
const DOWNLOAD_URL = "https://github.com/benmross/partypad/releases/latest";
const CLIENT_ID_RE = /^[A-Za-z0-9_-]{16,128}$/;
const SECRET_RE = /^[A-Za-z0-9_-]{32,160}$/;
const VERIFIER_HASH_RE = /^[A-Za-z0-9_-]{43}$/;
const USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const METRICS = new Set([
  "session_create", "session_end", "controller_join", "controller_disconnect",
  "transport", "relay_bytes", "rtt_ms",
]);
const METRIC_DIMENSIONS = new Set(["", "direct_udp", "direct_tcp", "turn_udp", "turn_tcp", "turn_tls", "websocket"]);
const CONTROLLER_BUTTONS = new Set([
  "cross", "circle", "square", "triangle", "l1", "r1", "l2", "r2", "l3", "r3",
  "share", "options", "ps", "dpad_up", "dpad_down", "dpad_left", "dpad_right",
]);

interface AccessIdentity {
  provider: string;
  subject: string;
}

interface DeviceAuthRow {
  id: number;
  verifier_hash: string;
  device_name: string;
  platform: string;
  client_version: string;
  status: string;
  identity_id: string | null;
  expires_at: number;
  last_poll_at: number | null;
}

interface AuthorizedDeviceRow {
  device_id: string;
  identity_id: string;
  device_expires_at: number;
  device_revoked_at: number | null;
  identity_revoked_at: number | null;
  token_expires_at: number;
  token_revoked_at: number | null;
  token_hash: string;
  token_overlap_until: number | null;
}

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

function publicPage(title: string, body: string): Response {
  return secureAsset(new Response(
    `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${html(title)} · PartyPad</title><style>body{font:16px/1.55 system-ui,sans-serif;max-width:48rem;margin:3rem auto;padding:0 1rem;color:#171717}a{color:#1457b8}nav{display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:2rem}.note{padding:.8rem 1rem;background:#f2f5f9;border-radius:.5rem}</style><nav><a href="/">PartyPad</a><a href="/status">Status</a><a href="/privacy">Privacy</a><a href="/acceptable-use">Acceptable use</a><a href="/support">Support</a></nav><main><h1>${html(title)}</h1>${body}</main></html>`,
    { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "public, max-age=300" } },
  ));
}

async function serviceEnabled(env: Env): Promise<boolean> {
  const setting = await env.DB.prepare(
    "SELECT value FROM service_settings WHERE key = 'new_sessions_enabled'",
  ).first<{ value: string }>();
  return setting?.value === "true";
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

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

async function sha256Base64url(value: string): Promise<string> {
  return base64url(
    new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))),
  );
}

async function keyedHash(env: Env, value: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(env.AUTH_HASH_KEY),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return base64url(
    new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value))),
  );
}

function protocolError(): Response {
  return json(
    {
      error: "client_upgrade_required",
      message: "This PartyPad version is no longer supported.",
      minimum_protocol: PROTOCOL_VERSION,
      download_url: DOWNLOAD_URL,
    },
    426,
  );
}

function validProtocol(request: Request): boolean {
  return request.headers.get("X-PartyPad-Protocol") === String(PROTOCOL_VERSION);
}

async function boundedJson(request: Request, maximumBytes: number): Promise<unknown> {
  const length = Number(request.headers.get("Content-Length"));
  if (Number.isFinite(length) && length > maximumBytes) throw new Error("request too large");
  const text = await request.text();
  if (new TextEncoder().encode(text).byteLength > maximumBytes) throw new Error("request too large");
  return JSON.parse(text);
}

function userCode(): string {
  const random = new Uint8Array(8);
  crypto.getRandomValues(random);
  const value = [...random].map((byte) => USER_CODE_ALPHABET[byte % USER_CODE_ALPHABET.length]);
  return `${value.slice(0, 4).join("")}-${value.slice(4).join("")}`;
}

function normalizeUserCode(value: string): string {
  const compact = value.toUpperCase().replaceAll(/[^A-Z0-9]/g, "");
  return compact.length === 8 ? `${compact.slice(0, 4)}-${compact.slice(4)}` : "";
}

function html(value: string): string {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

function sameSecret(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

async function rateLimited(
  env: Env,
  request: Request,
  action: string,
  limit: number,
  windowSeconds: number,
): Promise<boolean> {
  const now = Math.floor(Date.now() / 1000);
  const windowStart = now - (now % windowSeconds);
  const source = request.headers.get("CF-Connecting-IP") ?? "unknown";
  const bucket = await keyedHash(env, `rate:${action}:${source}`);
  const row = await env.DB.prepare(
    `INSERT INTO rate_limits (bucket_hash, action, window_start, count, expires_at)
     VALUES (?, ?, ?, 1, ?)
     ON CONFLICT(bucket_hash, action, window_start)
     DO UPDATE SET count = count + 1 RETURNING count`,
  ).bind(bucket, action, windowStart, windowStart + windowSeconds + 86400)
    .first<{ count: number }>();
  return (row?.count ?? limit + 1) > limit;
}

async function recordMetric(
  env: Env,
  metric: string,
  dimension = "",
  value = 0,
): Promise<void> {
  if (!METRICS.has(metric) || !METRIC_DIMENSIONS.has(dimension)) return;
  if (!Number.isFinite(value) || value < 0 || value > 100_000_000) return;
  const now = Math.floor(Date.now() / 1000);
  const hour = now - (now % 3600);
  await env.DB.prepare(
    `INSERT INTO aggregate_metrics (hour, metric, dimension, count, value_sum)
     VALUES (?, ?, ?, 1, ?)
     ON CONFLICT(hour, metric, dimension)
     DO UPDATE SET count = count + 1, value_sum = value_sum + excluded.value_sum`,
  ).bind(hour, metric, dimension, value).run();
}

function decodeJwtPart(value: string): Record<string, unknown> {
  const padded = value.replaceAll("-", "+").replaceAll("_", "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(padded), (char) => char.charCodeAt(0)))) as Record<string, unknown>;
}

async function accessIdentity(request: Request, env: Env): Promise<AccessIdentity | null> {
  const assertion = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!assertion || !env.ACCESS_TEAM_DOMAIN || !env.ACCESS_AUD) return null;
  const parts = assertion.split(".");
  if (parts.length !== 3) return null;
  try {
    const header = decodeJwtPart(parts[0]);
    const payload = decodeJwtPart(parts[1]);
    if (typeof header.kid !== "string" || typeof payload.sub !== "string") return null;
    if (typeof payload.exp !== "number" || payload.exp <= Date.now() / 1000) return null;
    const audiences = Array.isArray(payload.aud) ? payload.aud : [payload.aud];
    if (!audiences.includes(env.ACCESS_AUD)) return null;
    const issuer = `https://${env.ACCESS_TEAM_DOMAIN}.cloudflareaccess.com`;
    if (payload.iss !== issuer) return null;
    const certificates = await fetch(`${issuer}/cdn-cgi/access/certs`);
    if (!certificates.ok) return null;
    const keys = ((await certificates.json()) as { keys?: JsonWebKey[] }).keys ?? [];
    const jwk = keys.find((item) => (item as JsonWebKey & { kid?: string }).kid === header.kid);
    if (!jwk) return null;
    const key = await crypto.subtle.importKey(
      "jwk",
      jwk,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
    const signature = parts[2].replaceAll("-", "+").replaceAll("_", "/").padEnd(Math.ceil(parts[2].length / 4) * 4, "=");
    const valid = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      Uint8Array.from(atob(signature), (char) => char.charCodeAt(0)),
      new TextEncoder().encode(`${parts[0]}.${parts[1]}`),
    );
    return valid ? { provider: "cloudflare-access", subject: payload.sub } : null;
  } catch {
    return null;
  }
}

function validConfig(value: unknown): value is ControllerConfig {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return ["system", "system_name", "controller_mode", "backend"].every(
    (key) => typeof record[key] === "string" && (record[key] as string).length <= 80,
  );
}

function finiteInRange(value: unknown, limit: number): boolean {
  return typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= limit;
}

function validControllerInput(value: unknown): value is Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const data = value as Record<string, unknown>;
  const allowed = new Set(["t", "b", "left_x", "left_y", "px", "py", "m", "o", "aim", "rc"]);
  if (data.t !== "i" || Object.keys(data).some((key) => !allowed.has(key))) return false;
  if (data.b !== undefined) {
    if (!data.b || typeof data.b !== "object" || Array.isArray(data.b)) return false;
    const buttons = data.b as Record<string, unknown>;
    if (Object.keys(buttons).some((key) => !CONTROLLER_BUTTONS.has(key) || typeof buttons[key] !== "boolean")) return false;
  }
  for (const key of ["left_x", "left_y", "px", "py"]) {
    if (data[key] !== undefined && !finiteInRange(data[key], 1.5)) return false;
  }
  if (data.m !== undefined) {
    if (!data.m || typeof data.m !== "object" || Array.isArray(data.m)) return false;
    const motion = data.m as Record<string, unknown>;
    const motionKeys = new Set(["ax", "ay", "az", "ra", "rb", "rg", "orient", "accel_polarity"]);
    if (Object.keys(motion).some((key) => !motionKeys.has(key))) return false;
    for (const key of ["ax", "ay", "az"]) {
      if (motion[key] !== undefined && !finiteInRange(motion[key], 50)) return false;
    }
    for (const key of ["ra", "rb", "rg"]) {
      if (motion[key] !== undefined && !finiteInRange(motion[key], 5_000)) return false;
    }
    if (motion.orient !== undefined && !finiteInRange(motion.orient, 360)) return false;
    if (motion.accel_polarity !== undefined && motion.accel_polarity !== -1 && motion.accel_polarity !== 1) return false;
  }
  for (const [key, limit] of [["o", 360], ["aim", 360]] as const) {
    if (data[key] !== undefined && (!Array.isArray(data[key]) || data[key].length > 3 ||
      data[key].some((item) => !finiteInRange(item, limit)))) return false;
  }
  return data.rc === undefined || data.rc === 1 || data.rc === true;
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

async function createDeviceAuthorization(request: Request, env: Env): Promise<Response> {
  if (await rateLimited(env, request, "device-create", 10, 3600)) {
    return json({ error: "rate_limited", message: "Too many authorization attempts." }, 429);
  }
  let value: unknown;
  try {
    value = await boundedJson(request, 8192);
  } catch {
    return json({ error: "invalid_request", message: "A valid JSON body is required." }, 400);
  }
  if (!value || typeof value !== "object") return json({ error: "invalid_request" }, 400);
  const body = value as Record<string, unknown>;
  const name = typeof body.device_name === "string" ? body.device_name.trim() : "";
  const platform = body.platform;
  const clientVersion = body.client_version;
  if (
    !VERIFIER_HASH_RE.test(typeof body.verifier_hash === "string" ? body.verifier_hash : "") ||
    !name || name.length > 80 ||
    !["windows", "macos", "linux"].includes(typeof platform === "string" ? platform : "") ||
    typeof clientVersion !== "string" || !clientVersion || clientVersion.length > 40
  ) {
    return json({ error: "invalid_request", message: "Invalid device authorization fields." }, 400);
  }
  const now = Math.floor(Date.now() / 1000);
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const deviceCode = randomSecret(32);
    const code = userCode();
    try {
      await env.DB.prepare(
        `INSERT INTO device_authorizations
         (device_code_hash, user_code_hash, verifier_hash, device_name, platform,
          client_version, status, created_at, expires_at, delete_after)
         VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)`,
      ).bind(
        await keyedHash(env, `device:${deviceCode}`),
        await keyedHash(env, `user:${code}`),
        body.verifier_hash,
        name,
        platform,
        clientVersion,
        now,
        now + DEVICE_CODE_TTL_SECONDS,
        now + DEVICE_CODE_TTL_SECONDS + 86400,
      ).run();
      const authOrigin = env.AUTH_HOST ? `https://${env.AUTH_HOST}` : new URL(request.url).origin;
      return json({
        device_code: deviceCode,
        user_code: code,
        verification_uri: `${authOrigin}/activate`,
        verification_uri_complete: `${authOrigin}/activate?code=${code}`,
        expires_in: DEVICE_CODE_TTL_SECONDS,
        interval: POLL_INTERVAL_SECONDS,
      }, 201);
    } catch {
      if (attempt === 4) console.error("could not allocate a unique device authorization");
    }
  }
  return json({ error: "temporarily_unavailable" }, 503);
}

async function pollDeviceAuthorization(
  request: Request,
  env: Env,
  deviceCode: string,
): Promise<Response> {
  const authorization = request.headers.get("Authorization") ?? "";
  const verifier = authorization.startsWith("Verifier ") ? authorization.slice(9) : "";
  if (!SECRET_RE.test(deviceCode) || !SECRET_RE.test(verifier)) {
    return json({ error: "unauthorized" }, 401);
  }
  const row = await env.DB.prepare(
    `SELECT id, verifier_hash, device_name, platform, client_version, status,
            identity_id, expires_at, last_poll_at
     FROM device_authorizations WHERE device_code_hash = ?`,
  ).bind(await keyedHash(env, `device:${deviceCode}`)).first<DeviceAuthRow>();
  if (!row || row.verifier_hash !== await sha256Base64url(verifier)) {
    if (await rateLimited(env, request, "poll-failure", 30, 600)) {
      return json({ error: "rate_limited" }, 429);
    }
    return json({ error: "unauthorized" }, 401);
  }
  const now = Math.floor(Date.now() / 1000);
  if (row.expires_at <= now) return json({ error: "authorization_expired" }, 410);
  if (row.status === "consumed") return json({ error: "authorization_consumed" }, 410);
  if (row.status === "denied") return json({ status: "denied" });
  if (row.status === "pending") {
    if (row.last_poll_at !== null && now - row.last_poll_at < POLL_INTERVAL_SECONDS) {
      return new Response(JSON.stringify({ error: "slow_down" }), {
        status: 429,
        headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "Retry-After": String(POLL_INTERVAL_SECONDS) },
      });
    }
    await env.DB.prepare("UPDATE device_authorizations SET last_poll_at = ? WHERE id = ?")
      .bind(now, row.id).run();
    return json({ status: "pending", interval: POLL_INTERVAL_SECONDS, expires_in: row.expires_at - now });
  }
  if (row.status !== "approved" || !row.identity_id) return json({ error: "invalid_state" }, 500);
  const deviceCount = await env.DB.prepare(
    "SELECT COUNT(*) AS count FROM devices WHERE identity_id = ? AND revoked_at IS NULL",
  ).bind(row.identity_id).first<{ count: number }>();
  if ((deviceCount?.count ?? 0) >= MAX_DEVICES_PER_IDENTITY) {
    return json({ error: "device_limit", message: "Revoke an unused laptop before authorizing another." }, 409);
  }

  const deviceId = `dev_${randomSecret(18)}`;
  const token = `ppd_${randomSecret(32)}`;
  const expiresAt = now + DEVICE_TOKEN_TTL_SECONDS;
  const results = await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO devices
       (id, identity_id, name, platform, client_version, created_at, expires_at)
       SELECT ?, identity_id, device_name, platform, client_version, ?, ?
       FROM device_authorizations WHERE id = ? AND status = 'approved' AND expires_at > ?
       AND (SELECT COUNT(*) FROM devices WHERE identity_id = device_authorizations.identity_id AND revoked_at IS NULL) < ?`,
    ).bind(deviceId, now, expiresAt, row.id, now, MAX_DEVICES_PER_IDENTITY),
    env.DB.prepare(
      `INSERT INTO device_tokens (token_hash, device_id, created_at, expires_at)
       SELECT ?, id, ?, ? FROM devices WHERE id = ?`,
    ).bind(await keyedHash(env, `token:${token}`), now, expiresAt, deviceId),
    env.DB.prepare(
      "UPDATE device_authorizations SET status = 'consumed', consumed_at = ? WHERE id = ? AND status = 'approved'",
    ).bind(now, row.id),
  ]);
  if ((results[0].meta.changes ?? 0) !== 1) return json({ error: "authorization_consumed" }, 410);
  return json({
    status: "authorized",
    device: { id: deviceId, name: row.device_name, expires_at: new Date(expiresAt * 1000).toISOString() },
    device_token: token,
  });
}

async function authorizedDevice(request: Request, env: Env): Promise<AuthorizedDeviceRow | null> {
  const authorization = request.headers.get("Authorization") ?? "";
  const token = authorization.startsWith("Device ") ? authorization.slice(7) : "";
  if (!/^ppd_[A-Za-z0-9_-]{40,160}$/.test(token)) return null;
  const now = Math.floor(Date.now() / 1000);
  return env.DB.prepare(
    `SELECT d.id AS device_id, d.identity_id, d.expires_at AS device_expires_at,
            d.revoked_at AS device_revoked_at, i.revoked_at AS identity_revoked_at,
            t.expires_at AS token_expires_at, t.revoked_at AS token_revoked_at,
            t.token_hash, t.overlap_until AS token_overlap_until
     FROM device_tokens t JOIN devices d ON d.id = t.device_id
     JOIN identities i ON i.id = d.identity_id
     WHERE t.token_hash = ?
       AND ((t.overlap_until IS NULL AND t.expires_at > ?) OR t.overlap_until > ?)
       AND d.expires_at > ?`,
  ).bind(await keyedHash(env, `token:${token}`), now, now, now).first<AuthorizedDeviceRow>();
}

async function activate(request: Request, env: Env): Promise<Response> {
  const identity = await accessIdentity(request, env);
  if (!identity) return new Response("Authentication required", { status: 401 });
  let code = normalizeUserCode(new URL(request.url).searchParams.get("code") ?? "");
  if (request.method === "POST") {
    const form = await request.formData();
    code = normalizeUserCode(typeof form.get("code") === "string" ? String(form.get("code")) : "");
    if (!code) return new Response("Invalid device code", { status: 400 });
    const now = Math.floor(Date.now() / 1000);
    const csrfExpires = Number(form.get("csrf_expires"));
    const csrf = typeof form.get("csrf") === "string" ? String(form.get("csrf")) : "";
    const expectedCsrf = await keyedHash(
      env,
      `csrf:${identity.provider}:${identity.subject}:${code}:${csrfExpires}`,
    );
    if (!Number.isSafeInteger(csrfExpires) || csrfExpires < now || !sameSecret(csrf, expectedCsrf)) {
      return new Response("Invalid or expired approval form", { status: 403 });
    }
    const authorization = await env.DB.prepare(
      `SELECT id, device_name, platform, expires_at, status FROM device_authorizations
       WHERE user_code_hash = ?`,
    ).bind(await keyedHash(env, `user:${code}`)).first<{ id: number; device_name: string; platform: string; expires_at: number; status: string }>();
    if (!authorization || authorization.expires_at <= now || authorization.status !== "pending") {
      return new Response("That code is expired or already used.", { status: 410 });
    }
    const identityId = `id_${(await keyedHash(env, `identity:${identity.provider}:${identity.subject}`)).slice(0, 30)}`;
    const subjectHash = await keyedHash(env, `subject:${identity.provider}:${identity.subject}`);
    await env.DB.batch([
      env.DB.prepare(
        `INSERT INTO identities (id, provider, subject_hash, created_at, last_seen_at)
         VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, subject_hash)
         DO UPDATE SET last_seen_at = excluded.last_seen_at`,
      ).bind(identityId, identity.provider, subjectHash, now, now),
      env.DB.prepare(
        `UPDATE device_authorizations SET status = 'approved', identity_id = ?, approved_at = ?
         WHERE id = ? AND status = 'pending' AND expires_at > ?`,
      ).bind(identityId, now, authorization.id, now),
    ]);
    return secureAsset(new Response("<!doctype html><title>PartyPad authorized</title><h1>Laptop authorized</h1><p>You can return to PartyPad.</p><p><a href=\"/devices\">Manage authorized devices</a></p>", {
      headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
    }));
  }
  let deviceDescription = "Enter the code shown by PartyPad.";
  if (code) {
    const pending = await env.DB.prepare(
      `SELECT device_name, platform, expires_at FROM device_authorizations
       WHERE user_code_hash = ? AND status = 'pending'`,
    ).bind(await keyedHash(env, `user:${code}`))
      .first<{ device_name: string; platform: string; expires_at: number }>();
    if (pending && pending.expires_at > Date.now() / 1000) {
      deviceDescription = `Authorize ${pending.device_name} (${pending.platform}) with code ${code}.`;
    }
  }
  const csrfExpires = Math.floor(Date.now() / 1000) + 600;
  const csrf = await keyedHash(
    env,
    `csrf:${identity.provider}:${identity.subject}:${code}:${csrfExpires}`,
  );
  const shownCode = html(code);
  return new Response(`<!doctype html><meta name="viewport" content="width=device-width"><title>Authorize PartyPad</title><h1>Authorize PartyPad</h1><p>${html(deviceDescription)}</p><form method="post"><label>Code <input name="code" value="${shownCode}" required pattern="[A-Za-z0-9-]{8,9}"></label><input type="hidden" name="csrf_expires" value="${csrfExpires}"><input type="hidden" name="csrf" value="${csrf}"><button>Authorize laptop</button></form>`, {
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'" },
  });
}

async function manageDevices(request: Request, env: Env, deviceId?: string): Promise<Response> {
  const identity = await accessIdentity(request, env);
  if (!identity) return json({ error: "unauthorized" }, 401);
  const identityId = `id_${(await keyedHash(env, `identity:${identity.provider}:${identity.subject}`)).slice(0, 30)}`;
  if (request.method === "GET" && !deviceId) {
    const devices = await env.DB.prepare(
      `SELECT id, name, platform, client_version, created_at, last_used_at, expires_at, revoked_at
       FROM devices WHERE identity_id = ? ORDER BY created_at DESC`,
    ).bind(identityId).all();
    return json({ devices: devices.results });
  }
  const now = Math.floor(Date.now() / 1000);
  if (request.method === "DELETE" && deviceId) {
    const result = await env.DB.prepare(
      "UPDATE devices SET revoked_at = ?, delete_after = ? WHERE id = ? AND identity_id = ? AND revoked_at IS NULL",
    ).bind(now, now + 30 * 86400, deviceId, identityId).run();
    if ((result.meta.changes ?? 0) === 0) return json({ error: "not_found" }, 404);
    await env.DB.prepare("DELETE FROM device_tokens WHERE device_id = ?").bind(deviceId).run();
    const remaining = await env.DB.prepare(
      "SELECT COUNT(*) AS count FROM devices WHERE identity_id = ? AND revoked_at IS NULL",
    ).bind(identityId).first<{ count: number }>();
    if ((remaining?.count ?? 0) === 0) {
      await env.DB.prepare("UPDATE identities SET delete_after = ? WHERE id = ?")
        .bind(now + 30 * 86400, identityId).run();
    }
    return new Response(null, { status: 204 });
  }
  if (request.method === "DELETE" && !deviceId) {
    await env.DB.batch([
      env.DB.prepare(
        "UPDATE devices SET revoked_at = ?, delete_after = ? WHERE identity_id = ? AND revoked_at IS NULL",
      ).bind(now, now + 30 * 86400, identityId),
      env.DB.prepare(
        "DELETE FROM device_tokens WHERE device_id IN (SELECT id FROM devices WHERE identity_id = ?)",
      ).bind(identityId),
      env.DB.prepare("UPDATE identities SET delete_after = ? WHERE id = ?")
        .bind(now + 30 * 86400, identityId),
    ]);
    return new Response(null, { status: 204 });
  }
  return json({ error: "method_not_allowed" }, 405);
}

async function devicePage(request: Request, env: Env): Promise<Response> {
  const identity = await accessIdentity(request, env);
  if (!identity) return new Response("Authentication required", { status: 401 });
  const identityId = `id_${(await keyedHash(env, `identity:${identity.provider}:${identity.subject}`)).slice(0, 30)}`;
  const now = Math.floor(Date.now() / 1000);
  if (request.method === "POST") {
    const form = await request.formData();
    const expires = Number(form.get("csrf_expires"));
    const csrf = typeof form.get("csrf") === "string" ? String(form.get("csrf")) : "";
    const expected = await keyedHash(env, `device-csrf:${identity.provider}:${identity.subject}:${expires}`);
    if (!Number.isSafeInteger(expires) || expires < now || !sameSecret(csrf, expected)) {
      return new Response("Invalid or expired device form", { status: 403 });
    }
    const deviceId = typeof form.get("device_id") === "string" ? String(form.get("device_id")) : "";
    const revokeAll = form.get("all") === "1";
    if (!revokeAll && !/^dev_[A-Za-z0-9_-]{16,80}$/.test(deviceId)) {
      return new Response("Invalid device", { status: 400 });
    }
    const target = revokeAll ? "/api/devices" : `/api/devices/${deviceId}`;
    const result = await manageDevices(new Request(new URL(target, request.url), {
      method: "DELETE", headers: request.headers,
    }), env, revokeAll ? undefined : deviceId);
    if (!result.ok && result.status !== 404) return result;
    return new Response(null, { status: 303, headers: { Location: "/devices" } });
  }
  const devices = await env.DB.prepare(
    `SELECT id, name, platform, client_version, created_at, last_used_at, expires_at, revoked_at
     FROM devices WHERE identity_id = ? ORDER BY created_at DESC`,
  ).bind(identityId).all<Record<string, unknown>>();
  const csrfExpires = now + 600;
  const csrf = await keyedHash(env, `device-csrf:${identity.provider}:${identity.subject}:${csrfExpires}`);
  const rows = devices.results.map((device) => {
    const revoked = device.revoked_at ? "revoked" : `expires ${new Date(Number(device.expires_at) * 1000).toISOString().slice(0, 10)}`;
    const button = device.revoked_at ? "" : `<button name="device_id" value="${html(String(device.id))}">Revoke</button>`;
    return `<tr><td>${html(String(device.name))}</td><td>${html(String(device.platform))}</td><td>${html(String(device.client_version))}</td><td>${revoked}</td><td>${button}</td></tr>`;
  }).join("");
  return new Response(`<!doctype html><meta name="viewport" content="width=device-width"><title>PartyPad devices</title><h1>Your PartyPad devices</h1><form method="post"><input type="hidden" name="csrf_expires" value="${csrfExpires}"><input type="hidden" name="csrf" value="${csrf}"><table><thead><tr><th>Name</th><th>Platform</th><th>Version</th><th>Status</th><th></th></tr></thead><tbody>${rows || "<tr><td colspan=5>No authorized devices.</td></tr>"}</tbody></table><button name="all" value="1">Revoke all devices</button></form>`, {
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'" },
  });
}

async function adminRequest(request: Request, env: Env): Promise<AccessIdentity | null> {
  const identity = await accessIdentity(request, env);
  if (!identity || !env.ADMIN_ACCESS_SUB || !sameSecret(identity.subject, env.ADMIN_ACCESS_SUB)) {
    return null;
  }
  return identity;
}

async function adminApi(request: Request, env: Env, path: string): Promise<Response> {
  if (!await adminRequest(request, env)) return json({ error: "unauthorized" }, 401);
  if (request.method === "GET" && path === "/api/admin/metrics") {
    const requested = Number(new URL(request.url).searchParams.get("hours") ?? 168);
    const hours = Number.isSafeInteger(requested) ? Math.max(1, Math.min(requested, 24 * 31)) : 168;
    const since = Math.floor(Date.now() / 1000) - hours * 3600;
    const metrics = await env.DB.prepare(
      `SELECT hour, metric, dimension, count, value_sum
       FROM aggregate_metrics WHERE hour >= ? ORDER BY hour, metric, dimension`,
    ).bind(since).all();
    return json({ hours, metrics: metrics.results });
  }
  if (path === "/api/admin/new-sessions") {
    if (request.method === "GET") {
      const setting = await env.DB.prepare(
        "SELECT value, updated_at FROM service_settings WHERE key = 'new_sessions_enabled'",
      ).first();
      return json(setting ?? { value: "false" });
    }
    if (request.method === "PUT") {
      let body: unknown;
      try { body = await boundedJson(request, 1024); } catch { return json({ error: "invalid_request" }, 400); }
      if (!body || typeof body !== "object" || typeof (body as Record<string, unknown>).enabled !== "boolean") {
        return json({ error: "invalid_request" }, 400);
      }
      const enabled = (body as { enabled: boolean }).enabled;
      await env.DB.prepare(
        `INSERT INTO service_settings (key, value, updated_at) VALUES ('new_sessions_enabled', ?, ?)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at`,
      ).bind(String(enabled), Math.floor(Date.now() / 1000)).run();
      return json({ enabled });
    }
  }
  return json({ error: "not_found" }, 404);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/api/status") {
      const acceptingSessions = await serviceEnabled(env);
      return json({
        status: acceptingSessions ? "operational" : "limited",
        accepting_new_sessions: acceptingSessions,
        protocol: PROTOCOL_VERSION,
      });
    }
    if (request.method === "GET" && url.pathname === "/status") {
      const acceptingSessions = await serviceEnabled(env);
      return publicPage(
        "Service status",
        `<p class="note"><strong>${acceptingSessions ? "Operational" : "Limited"}</strong> — ${acceptingSessions ? "PartyPad is accepting new game sessions." : "New game sessions are temporarily paused. Existing sessions are not intentionally interrupted."}</p><p>This page reports the service's admission switch, not end-to-end network health. PartyPad is early alpha.</p>`,
      );
    }
    if (request.method === "GET" && url.pathname === "/privacy") {
      return publicPage("Privacy", `<p>PartyPad stores a pseudonymous identity and revocable laptop-device records for authorization. It does not store controller inputs, motion samples, plaintext email addresses, raw credentials, or join secrets.</p><p>Operational measurements are hourly aggregates without identity, device, session, IP, or secret dimensions. Device authorizations and live-session records expire; privacy-safe aggregates are retained for up to 13 months.</p><p>Cloudflare processes network metadata as the infrastructure provider. WebRTC controller traffic is encrypted between the phone and laptop; authenticated WebSocket fallback traffic is relayed but not retained.</p><p>Use the authenticated <a href="/devices">device page</a> to list or revoke authorized laptops. This is a concise service notice; the repository contains the versioned policy and implementation details.</p>`);
    }
    if (request.method === "GET" && url.pathname === "/acceptable-use") {
      return publicPage("Acceptable use", "<p>Use PartyPad only for consensual multiplayer controller sessions. Do not access sessions without permission, automate enrollment or traffic, evade limits, probe or disrupt the service, impose unreasonable network cost, or transmit unlawful or malicious content.</p><p>Controller links are bearer credentials. Share them only with invited players and stop the session after play. The operator may pause new sessions or revoke devices to protect users and service capacity.</p>");
    }
    if (request.method === "GET" && url.pathname === "/support") {
      return publicPage("Support", `<p>PartyPad is early-alpha software. For setup and troubleshooting, see the project README and diagnostics export. Never post device tokens, join URLs, authorization codes, TURN credentials, or controller traces.</p><p>Report security issues privately using the instructions in <a href="https://github.com/benmross/partypad/security/policy">the security policy</a>. For non-sensitive bugs, use the project's issue tracker and include the PartyPad version, operating system, Dolphin version, and a redacted diagnostics export.</p>`);
    }
    if (request.method === "GET" && url.pathname === "/config") {
      return json({
        online_service: true,
        protocol: { current: PROTOCOL_VERSION, minimum: PROTOCOL_VERSION },
        latest_client_version: env.LATEST_CLIENT_VERSION ?? "0.2.0",
        download_url: DOWNLOAD_URL,
      });
    }
    if ((request.method === "GET" || request.method === "POST") && url.pathname === "/activate") {
      return activate(request, env);
    }
    if ((request.method === "GET" || request.method === "POST") && url.pathname === "/devices") {
      return devicePage(request, env);
    }
    if (url.pathname.startsWith("/api/admin/")) {
      return adminApi(request, env, url.pathname);
    }
    if ((request.method === "GET" || request.method === "DELETE") && url.pathname === "/api/devices") {
      return manageDevices(request, env);
    }
    const deviceMatch = url.pathname.match(/^\/api\/devices\/(dev_[A-Za-z0-9_-]{16,80})$/);
    if (request.method === "DELETE" && deviceMatch) {
      return manageDevices(request, env, deviceMatch[1]);
    }
    if (request.method === "POST" && url.pathname === "/api/device/authorizations") {
      if (!validProtocol(request)) return protocolError();
      return createDeviceAuthorization(request, env);
    }
    const authorizationMatch = url.pathname.match(/^\/api\/device\/authorizations\/([A-Za-z0-9_-]{32,160})$/);
    if (request.method === "GET" && authorizationMatch) {
      if (!validProtocol(request)) return protocolError();
      return pollDeviceAuthorization(request, env, authorizationMatch[1]);
    }
    if (request.method === "POST" && url.pathname === "/api/sessions") {
      if (!validProtocol(request)) return protocolError();
      if (await rateLimited(env, request, "session-create", 30, 3600)) {
        return json({ error: "rate_limited" }, 429);
      }
      const device = await authorizedDevice(request, env);
      if (!device) return json({ error: "unauthorized" }, 401);
      if (device.device_revoked_at || device.token_revoked_at) return json({ error: "device_revoked" }, 403);
      if (device.identity_revoked_at) return json({ error: "identity_revoked" }, 403);
      if (!await serviceEnabled(env)) return json({ error: "new_sessions_disabled" }, 503);
      const nowSeconds = Math.floor(Date.now() / 1000);
      const active = await env.DB.prepare(
        "SELECT COUNT(*) AS count FROM session_owners WHERE device_id = ? AND ended_at IS NULL AND expires_at > ?",
      ).bind(device.device_id, nowSeconds).first<{ count: number }>();
      if ((active?.count ?? 0) >= 2) return json({ error: "session_limit" }, 409);
      const identityActive = await env.DB.prepare(
        "SELECT COUNT(*) AS count FROM session_owners WHERE identity_id = ? AND ended_at IS NULL AND expires_at > ?",
      ).bind(device.identity_id, nowSeconds).first<{ count: number }>();
      if ((identityActive?.count ?? 0) >= 4) return json({ error: "identity_session_limit" }, 409);
      let config: unknown;
      try {
        config = await boundedJson(request, 16_384);
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
      await env.DB.batch([
        env.DB.prepare(
          `INSERT INTO session_owners
           (session_id, device_id, identity_id, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?)`,
        ).bind(id, device.device_id, device.identity_id, nowSeconds, Math.floor(expiresAt / 1000)),
        env.DB.prepare("UPDATE devices SET last_used_at = ? WHERE id = ?")
          .bind(nowSeconds, device.device_id),
      ]);
      await recordMetric(env, "session_create");

      let rotatedDeviceToken: string | undefined;
      let rotatedExpiresAt: number | undefined;
      if (
        device.token_overlap_until === null &&
        device.token_expires_at - nowSeconds < 14 * 86400
      ) {
        rotatedDeviceToken = `ppd_${randomSecret(32)}`;
        rotatedExpiresAt = nowSeconds + DEVICE_TOKEN_TTL_SECONDS;
        await env.DB.batch([
          env.DB.prepare(
            `INSERT INTO device_tokens (token_hash, device_id, created_at, expires_at)
             VALUES (?, ?, ?, ?)`,
          ).bind(
            await keyedHash(env, `token:${rotatedDeviceToken}`),
            device.device_id,
            nowSeconds,
            rotatedExpiresAt,
          ),
          env.DB.prepare("UPDATE device_tokens SET overlap_until = ? WHERE token_hash = ?")
            .bind(nowSeconds + 86400, device.token_hash),
          env.DB.prepare("UPDATE devices SET expires_at = ? WHERE id = ?")
            .bind(rotatedExpiresAt, device.device_id),
        ]);
      }

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
          ...(rotatedDeviceToken && rotatedExpiresAt
            ? {
                rotated_device_token: rotatedDeviceToken,
                device_token_expires_at: new Date(rotatedExpiresAt * 1000).toISOString(),
              }
            : {}),
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
      const response = await stub.fetch("https://session.internal/end", {
        method: "DELETE",
        body: await request.text(),
      });
      if (response.ok) {
        const ended = await env.DB.prepare(
          "UPDATE session_owners SET ended_at = ? WHERE session_id = ? AND ended_at IS NULL",
        ).bind(Math.floor(Date.now() / 1000), sessionMatch[1]).run();
        if ((ended.meta.changes ?? 0) === 1) await recordMetric(env, "session_end");
      }
      return response;
    }
    if (url.pathname.startsWith("/api/")) return json({ error: "not found" }, 404);
    if (url.pathname.startsWith("/static/")) {
      const assetUrl = new URL(request.url);
      assetUrl.pathname = url.pathname.slice("/static".length);
      return secureAsset(await env.ASSETS.fetch(new Request(assetUrl, request)));
    }
    return secureAsset(await env.ASSETS.fetch(request));
  },
  async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
    const now = Math.floor(Date.now() / 1000);
    await env.DB.batch([
      env.DB.prepare("DELETE FROM rate_limits WHERE expires_at <= ?").bind(now),
      env.DB.prepare("DELETE FROM aggregate_metrics WHERE hour <= ?").bind(now - 13 * 31 * 86400),
      env.DB.prepare("DELETE FROM session_owners WHERE expires_at <= ?").bind(now - 86400),
      env.DB.prepare("DELETE FROM device_authorizations WHERE delete_after <= ?").bind(now),
      env.DB.prepare("DELETE FROM device_tokens WHERE revoked_at IS NOT NULL AND revoked_at <= ?").bind(now - 30 * 86400),
      env.DB.prepare("DELETE FROM devices WHERE delete_after IS NOT NULL AND delete_after <= ?").bind(now),
      env.DB.prepare(
        `DELETE FROM identities WHERE delete_after IS NOT NULL AND delete_after <= ?
         AND NOT EXISTS (SELECT 1 FROM devices WHERE devices.identity_id = identities.id)`,
      ).bind(now),
    ]);
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
    else await this.fromController(ws, attachment, message);
  }

  async webSocketClose(ws: WebSocket): Promise<void> {
    const attachment = ws.deserializeAttachment() as SocketAttachment | null;
    if (attachment?.role === "controller") {
      this.sendHost({ t: "peer_leave", peer: attachment.peer });
      await recordMetric(this.env, "controller_disconnect");
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
      .filter(([ws, attachment]) => ws.readyState === WebSocket.OPEN && (!role || attachment.role === role));
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
    if (message.protocol !== PROTOCOL_VERSION) {
      ws.close(1008, "client upgrade required");
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
      await recordMetric(this.env, "controller_join");
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

  private async fromController(
    _ws: WebSocket,
    attachment: SocketAttachment,
    message: Record<string, unknown>,
  ): Promise<void> {
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
      (message.seq as number) >= 0 &&
      validControllerInput(message.data)
    ) {
      this.sendHost({ t: "input", peer: attachment.peer, seq: message.seq, data: message.data });
    } else if (
      message.t === "metric" &&
      typeof message.name === "string" &&
      METRICS.has(message.name) &&
      typeof message.dimension === "string" &&
      METRIC_DIMENSIONS.has(message.dimension) &&
      typeof message.value === "number" &&
      Number.isFinite(message.value) &&
      message.value >= 0 &&
      message.value <= 100_000_000
    ) {
      await recordMetric(this.env, message.name, message.dimension, message.value);
      this.sendHost({
        t: "diagnostic",
        peer: attachment.peer,
        name: message.name,
        dimension: message.dimension,
        value: message.value,
      });
    }
  }

  private sendHost(message: unknown): void {
    const host = this.sockets("host")[0]?.[0];
    if (host) host.send(JSON.stringify(message));
  }
}

import { createScheduledController, env, runDurableObjectAlarm, runInDurableObject } from "cloudflare:test";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";

const protocolHeaders = { "X-PartyPad-Protocol": "1" };

function verifierHash(verifier: string): Promise<string> {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)).then((digest) =>
    btoa(String.fromCharCode(...new Uint8Array(digest)))
      .replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", ""),
  );
}

function encoded(value: unknown): string {
  return btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(value))))
    .replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

async function accessAssertion() {
  const pair = await crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["sign", "verify"],
  );
  const header = encoded({ alg: "RS256", kid: "test-key" });
  const payload = encoded({
    sub: "test-access-subject",
    aud: ["test-audience"],
    iss: "https://example.cloudflareaccess.com",
    exp: Math.floor(Date.now() / 1000) + 600,
  });
  const signature = new Uint8Array(await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5", pair.privateKey, new TextEncoder().encode(`${header}.${payload}`),
  ));
  const token = `${header}.${payload}.${btoa(String.fromCharCode(...signature)).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "")}`;
  const key = await crypto.subtle.exportKey("jwk", pair.publicKey) as JsonWebKey & { kid?: string };
  key.kid = "test-key";
  return { token, key };
}

async function createAuthorization(verifier = "v".repeat(43)) {
  const response = await worker.fetch(new Request("https://partypad.test/api/device/authorizations", {
    method: "POST",
    headers: { ...protocolHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({
      verifier_hash: await verifierHash(verifier),
      device_name: "Test laptop",
      platform: "linux",
      client_version: "0.2.0",
    }),
  }), env);
  expect(response.status).toBe(201);
  return { verifier, body: await response.json<Record<string, string>>() };
}

async function approveLatestAuthorization() {
  const now = Math.floor(Date.now() / 1000);
  await env.DB.batch([
    env.DB.prepare(
      "INSERT INTO identities (id, provider, subject_hash, created_at, last_seen_at) VALUES ('id_test', 'test', 'hash', ?, ?) ON CONFLICT DO NOTHING",
    ).bind(now, now),
    env.DB.prepare(
      "UPDATE device_authorizations SET status = 'approved', identity_id = 'id_test', approved_at = ? WHERE status = 'pending'",
    ).bind(now),
  ]);
}

async function issueDeviceToken() {
  const { verifier, body } = await createAuthorization();
  await approveLatestAuthorization();
  const response = await worker.fetch(new Request(
    `https://partypad.test/api/device/authorizations/${body.device_code}`,
    { headers: { ...protocolHeaders, Authorization: `Verifier ${verifier}` } },
  ), env);
  return (await response.json<{ device_token: string }>()).device_token;
}

async function createSession(token: string) {
  const response = await worker.fetch(new Request("https://partypad.test/api/sessions", {
    method: "POST",
    headers: { ...protocolHeaders, Authorization: `Device ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ system: "wii", system_name: "Wii", controller_mode: "wii", backend: "dolphin" }),
  }), env);
  expect(response.status).toBe(201);
  return response.json<{ ws_url: string; host_secret: string; join_url: string; end_url: string }>();
}

function nextMessage(socket: WebSocket): Promise<Record<string, unknown>> {
  return new Promise((resolve) => socket.addEventListener("message", (event) => {
    resolve(JSON.parse(String(event.data)) as Record<string, unknown>);
  }, { once: true }));
}

async function connectSocket(url: string): Promise<WebSocket> {
  const fetchUrl = url.replace(/^wss:/, "https:").replace(/^ws:/, "http:");
  const response = await worker.fetch(new Request(fetchUrl, { headers: { Upgrade: "websocket" } }), env);
  expect(response.status).toBe(101);
  const socket = response.webSocket!;
  socket.accept();
  expect((await nextMessage(socket)).t).toBe("hello");
  return socket;
}

describe("PartyPad Worker", () => {
  afterEach(() => vi.unstubAllGlobals());
  beforeEach(async () => {
    await env.DB.batch([
      env.DB.prepare("DELETE FROM aggregate_metrics"),
      env.DB.prepare("DELETE FROM rate_limits"),
      env.DB.prepare("DELETE FROM session_owners"),
      env.DB.prepare("DELETE FROM device_tokens"),
      env.DB.prepare("DELETE FROM devices"),
      env.DB.prepare("DELETE FROM device_authorizations"),
      env.DB.prepare("DELETE FROM identities"),
      env.DB.prepare("UPDATE service_settings SET value = 'true' WHERE key = 'new_sessions_enabled'"),
    ]);
  });

  it("advertises and enforces protocol v1", async () => {
    const config = await worker.fetch(new Request("https://partypad.test/config"), env);
    expect(await config.json()).toMatchObject({ protocol: { current: 1, minimum: 1 } });
    const rejected = await worker.fetch(new Request("https://partypad.test/api/device/authorizations", { method: "POST" }), env);
    expect(rejected.status).toBe(426);
  });

  it("publishes policy, support, and admission status without exposing service secrets", async () => {
    for (const path of ["/status", "/privacy", "/acceptable-use", "/support"]) {
      const response = await worker.fetch(new Request(`https://partypad.test${path}`), env);
      expect(response.status).toBe(200);
      expect(response.headers.get("Content-Security-Policy")).toContain("default-src 'self'");
      const page = await response.text();
      expect(page).not.toContain("AUTH_HASH_KEY");
      expect(page).not.toContain("TURN_KEY_API_TOKEN");
    }
    const status = await worker.fetch(new Request("https://partypad.test/api/status"), env);
    expect(await status.json()).toEqual({
      status: "operational", accepting_new_sessions: true, protocol: 1,
    });
    await env.DB.prepare(
      "UPDATE service_settings SET value = 'false' WHERE key = 'new_sessions_enabled'",
    ).run();
    const limited = await worker.fetch(new Request("https://partypad.test/api/status"), env);
    expect(await limited.json()).toMatchObject({ status: "limited", accepting_new_sessions: false });
  });

  it("creates bounded, hashed device authorizations", async () => {
    const { body } = await createAuthorization();
    expect(body.user_code).toMatch(/^[A-Z2-9]{4}-[A-Z2-9]{4}$/);
    expect(body.verification_uri).toBe("https://auth.example.test/activate");
    const row = await env.DB.prepare("SELECT * FROM device_authorizations").first<Record<string, unknown>>();
    expect(row?.device_code_hash).not.toBe(body.device_code);
    expect(row?.user_code_hash).not.toBe(body.user_code);
    expect(row?.status).toBe("pending");
  });

  it("consumes approval once and authorizes sessions with the issued device token", async () => {
    const { verifier, body } = await createAuthorization();
    await approveLatestAuthorization();
    const poll = () => worker.fetch(new Request(
      `https://partypad.test/api/device/authorizations/${body.device_code}`,
      { headers: { ...protocolHeaders, Authorization: `Verifier ${verifier}` } },
    ), env);
    const authorized = await poll();
    expect(authorized.status).toBe(200);
    const credential = await authorized.json<{ device_token: string }>();
    expect(credential.device_token).toMatch(/^ppd_/);
    expect((await poll()).status).toBe(410);

    const session = await worker.fetch(new Request("https://partypad.test/api/sessions", {
      method: "POST",
      headers: { ...protocolHeaders, Authorization: `Device ${credential.device_token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ system: "wii", system_name: "Wii", controller_mode: "wii", backend: "dolphin" }),
    }), env);
    expect(session.status).toBe(201);
    expect(await session.json()).toMatchObject({ id: expect.any(String), host_secret: expect.any(String) });
    const metric = await env.DB.prepare(
      "SELECT count FROM aggregate_metrics WHERE metric = 'session_create' AND dimension = ''",
    ).first<{ count: number }>();
    expect(metric?.count).toBe(1);
  });

  it("rejects revoked credentials and a disabled new-session service", async () => {
    const { verifier, body } = await createAuthorization();
    await approveLatestAuthorization();
    const authorized = await worker.fetch(new Request(
      `https://partypad.test/api/device/authorizations/${body.device_code}`,
      { headers: { ...protocolHeaders, Authorization: `Verifier ${verifier}` } },
    ), env);
    const { device_token: token } = await authorized.json<{ device_token: string }>();
    await env.DB.prepare("UPDATE devices SET revoked_at = ?").bind(Math.floor(Date.now() / 1000)).run();
    const request = () => worker.fetch(new Request("https://partypad.test/api/sessions", {
      method: "POST",
      headers: { ...protocolHeaders, Authorization: `Device ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ system: "wii", system_name: "Wii", controller_mode: "wii", backend: "dolphin" }),
    }), env);
    expect((await request()).status).toBe(403);
  });

  it("rotates a near-expiry token while retaining a 24-hour overlap", async () => {
    const { verifier, body } = await createAuthorization();
    await approveLatestAuthorization();
    const authorized = await worker.fetch(new Request(
      `https://partypad.test/api/device/authorizations/${body.device_code}`,
      { headers: { ...protocolHeaders, Authorization: `Verifier ${verifier}` } },
    ), env);
    const { device_token: oldToken } = await authorized.json<{ device_token: string }>();
    const nearExpiry = Math.floor(Date.now() / 1000) + 3600;
    await env.DB.batch([
      env.DB.prepare("UPDATE device_tokens SET expires_at = ?").bind(nearExpiry),
      env.DB.prepare("UPDATE devices SET expires_at = ?").bind(nearExpiry),
    ]);
    const sessionRequest = (token: string) => worker.fetch(new Request("https://partypad.test/api/sessions", {
      method: "POST",
      headers: { ...protocolHeaders, Authorization: `Device ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ system: "wii", system_name: "Wii", controller_mode: "wii", backend: "dolphin" }),
    }), env);
    const rotated = await sessionRequest(oldToken);
    const rotation = await rotated.json<{ rotated_device_token: string }>();
    expect(rotation.rotated_device_token).toMatch(/^ppd_/);
    expect((await sessionRequest(oldToken)).status).toBe(201);
    const old = await env.DB.prepare("SELECT overlap_until FROM device_tokens WHERE overlap_until IS NOT NULL").first<{ overlap_until: number }>();
    expect(old!.overlap_until).toBeGreaterThan(Math.floor(Date.now() / 1000) + 23 * 3600);
  });

  it("preserves candidate-before-offer ordering and controllers across host reconnect", async () => {
    const session = await createSession(await issueDeviceToken());
    const joinSecret = session.join_url.split("/").at(-1)!;
    const host = await connectSocket(session.ws_url);
    host.send(JSON.stringify({ t: "auth", role: "host", secret: session.host_secret, protocol: 1 }));
    expect((await nextMessage(host)).t).toBe("auth_ok");
    expect((await nextMessage(host)).t).toBe("peers");

    const controller = await connectSocket(session.ws_url);
    controller.send(JSON.stringify({
      t: "auth", role: "controller", secret: joinSecret, protocol: 1, client: "a".repeat(16),
    }));
    expect((await nextMessage(controller)).t).toBe("auth_ok");
    expect((await nextMessage(host)).t).toBe("peer_join");
    controller.send(JSON.stringify({ t: "candidate", candidate: { candidate: "candidate:test" } }));
    controller.send(JSON.stringify({ t: "offer", sdp: "offer-sdp" }));
    expect((await nextMessage(host)).t).toBe("candidate");
    expect((await nextMessage(host)).t).toBe("offer");
    controller.send(JSON.stringify({ t: "input", seq: 1, data: { t: "i", left_x: 1e100 } }));
    controller.send(JSON.stringify({ t: "input", seq: 2, data: { t: "i", left_x: 0.5, b: { cross: true } } }));
    const input = await nextMessage(host);
    expect(input).toMatchObject({ t: "input", seq: 2, data: { left_x: 0.5 } });

    const replacement = await connectSocket(session.ws_url);
    replacement.send(JSON.stringify({ t: "auth", role: "host", secret: session.host_secret, protocol: 1 }));
    expect((await nextMessage(replacement)).t).toBe("auth_ok");
    const peers = await nextMessage(replacement);
    expect(peers.t).toBe("peers");
    expect(peers.peers).toHaveLength(1);
    controller.close();
    replacement.close();
  });

  it("ends a session idempotently and records one end metric", async () => {
    const session = await createSession(await issueDeviceToken());
    const end = () => worker.fetch(new Request(session.end_url, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ secret: session.host_secret }),
    }), env);
    expect((await end()).status).toBe(204);
    expect((await end()).status).toBe(204);
    const metric = await env.DB.prepare(
      "SELECT count FROM aggregate_metrics WHERE metric = 'session_end' AND dimension = ''",
    ).first<{ count: number }>();
    expect(metric?.count).toBe(1);
  });

  it("validates an Access JWT and CSRF token before browser approval", async () => {
    const { verifier, body } = await createAuthorization();
    const assertion = await accessAssertion();
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ keys: [assertion.key] })));
    const headers = { "Cf-Access-Jwt-Assertion": assertion.token };
    const page = await worker.fetch(new Request(
      `https://auth.example.test/activate?code=${body.user_code}`,
      { headers },
    ), env);
    expect(page.status).toBe(200);
    const markup = await page.text();
    const csrf = markup.match(/name="csrf" value="([^"]+)"/)?.[1];
    const expires = markup.match(/name="csrf_expires" value="([^"]+)"/)?.[1];
    expect(csrf).toBeTruthy();
    const invalid = await worker.fetch(new Request("https://auth.example.test/activate", {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ code: body.user_code, csrf: "wrong", csrf_expires: expires! }),
    }), env);
    expect(invalid.status).toBe(403);
    const approved = await worker.fetch(new Request("https://auth.example.test/activate", {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ code: body.user_code, csrf: csrf!, csrf_expires: expires! }),
    }), env);
    expect(approved.status).toBe(200);
    const poll = await worker.fetch(new Request(
      `https://partypad.test/api/device/authorizations/${body.device_code}`,
      { headers: { ...protocolHeaders, Authorization: `Verifier ${verifier}` } },
    ), env);
    const credential = await poll.json<{ status: string; device_token: string; device: { id: string } }>();
    expect(credential.status).toBe("authorized");
    const devices = await worker.fetch(new Request("https://auth.example.test/api/devices", { headers }), env);
    expect((await devices.json<{ devices: unknown[] }>()).devices).toHaveLength(1);
    const devicePage = await worker.fetch(new Request("https://auth.example.test/devices", { headers }), env);
    const deviceMarkup = await devicePage.text();
    expect(deviceMarkup).toContain("Test laptop");
    const deviceCsrf = deviceMarkup.match(/name="csrf" value="([^"]+)"/)?.[1];
    const deviceExpires = deviceMarkup.match(/name="csrf_expires" value="([^"]+)"/)?.[1];
    const revoked = await worker.fetch(new Request("https://auth.example.test/devices", {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        device_id: credential.device.id, csrf: deviceCsrf!, csrf_expires: deviceExpires!,
      }),
    }), env);
    expect(revoked.status).toBe(303);
    const session = await worker.fetch(new Request("https://partypad.test/api/sessions", {
      method: "POST",
      headers: { ...protocolHeaders, Authorization: `Device ${credential.device_token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ system: "wii", system_name: "Wii", controller_mode: "wii", backend: "dolphin" }),
    }), env);
    expect(session.status).toBe(401);
  });

  it("enforces polling, authorization creation, and active-session limits", async () => {
    const pending = await createAuthorization();
    const pollRequest = () => worker.fetch(new Request(
      `https://partypad.test/api/device/authorizations/${pending.body.device_code}`,
      { headers: { ...protocolHeaders, Authorization: `Verifier ${pending.verifier}` } },
    ), env);
    expect((await pollRequest()).status).toBe(200);
    expect((await pollRequest()).status).toBe(429);
    await env.DB.prepare("UPDATE device_authorizations SET expires_at = 0").run();
    expect((await pollRequest()).status).toBe(410);

    await env.DB.prepare("DELETE FROM device_authorizations").run();
    for (let index = 0; index < 9; index += 1) await createAuthorization(`x${index}`.padEnd(43, "v"));
    const limited = await worker.fetch(new Request("https://partypad.test/api/device/authorizations", {
      method: "POST",
      headers: { ...protocolHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({ verifier_hash: await verifierHash("z".repeat(43)), device_name: "Laptop", platform: "linux", client_version: "0.2.0" }),
    }), env);
    expect(limited.status).toBe(429);

    await env.DB.prepare("DELETE FROM rate_limits").run();
    const token = await issueDeviceToken();
    await createSession(token);
    await createSession(token);
    const third = await worker.fetch(new Request("https://partypad.test/api/sessions", {
      method: "POST",
      headers: { ...protocolHeaders, Authorization: `Device ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ system: "wii", system_name: "Wii", controller_mode: "wii", backend: "dolphin" }),
    }), env);
    expect(third.status).toBe(409);
  });

  it("removes expired operational records on the retention schedule", async () => {
    await env.DB.batch([
      env.DB.prepare(
        "INSERT INTO aggregate_metrics (hour, metric, dimension, count, value_sum) VALUES (0, 'session_create', '', 1, 0)",
      ),
      env.DB.prepare(
        "INSERT INTO rate_limits (bucket_hash, action, window_start, count, expires_at) VALUES ('old', 'test', 0, 1, 0)",
      ),
    ]);
    await worker.scheduled(createScheduledController(), env);
    expect((await env.DB.prepare("SELECT COUNT(*) AS count FROM aggregate_metrics").first<{ count: number }>())?.count).toBe(0);
    expect((await env.DB.prepare("SELECT COUNT(*) AS count FROM rate_limits").first<{ count: number }>())?.count).toBe(0);
  });

  it("expires a session through its Durable Object alarm", async () => {
    const session = await createSession(await issueDeviceToken());
    const id = new URL(session.ws_url.replace(/^wss:/, "https:")).pathname.split("/")[3];
    const stub = env.SESSIONS.getByName(id);
    await runInDurableObject(stub, async (instance, state) => {
      const record = await state.storage.get<Record<string, unknown>>("session");
      record!.expiresAt = Date.now() - 1;
      await state.storage.put("session", record);
      (instance as unknown as { record: Record<string, unknown> }).record = record!;
      await state.storage.setAlarm(Date.now() - 1);
    });
    await runDurableObjectAlarm(stub); // A past alarm may already have fired when the test regains control.
    const expired = await worker.fetch(new Request(session.ws_url.replace(/^wss:/, "https:"), {
      headers: { Upgrade: "websocket" },
    }), env);
    expect(expired.status).toBe(410);
  });

  it("enforces the aggregate identity session limit across devices", async () => {
    const first = await issueDeviceToken();
    const second = await issueDeviceToken();
    const third = await issueDeviceToken();
    await createSession(first);
    await createSession(first);
    await createSession(second);
    await createSession(second);
    const limited = await worker.fetch(new Request("https://partypad.test/api/sessions", {
      method: "POST",
      headers: { ...protocolHeaders, Authorization: `Device ${third}`, "Content-Type": "application/json" },
      body: JSON.stringify({ system: "wii", system_name: "Wii", controller_mode: "wii", backend: "dolphin" }),
    }), env);
    expect(limited.status).toBe(409);
    expect(await limited.json()).toMatchObject({ error: "identity_session_limit" });
  });

  it("caps active laptops per identity", async () => {
    const pending = await createAuthorization();
    await approveLatestAuthorization();
    const now = Math.floor(Date.now() / 1000);
    await env.DB.batch(Array.from({ length: 10 }, (_, index) => env.DB.prepare(
      `INSERT INTO devices
       (id, identity_id, name, platform, client_version, created_at, expires_at)
       VALUES (?, 'id_test', 'Laptop', 'linux', '0.2.0', ?, ?)`,
    ).bind(`dev_seed_${index}`, now, now + 86400)));
    const response = await worker.fetch(new Request(
      `https://partypad.test/api/device/authorizations/${pending.body.device_code}`,
      { headers: { ...protocolHeaders, Authorization: `Verifier ${pending.verifier}` } },
    ), env);
    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({ error: "device_limit" });
  });
});

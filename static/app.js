"use strict";

const state = { b: {}, left_x: 0, left_y: 0, px: 0, py: 0, m: null };
let dirty = true;
let ws = null;
let rtc = null;
let inputChannel = null;
let inputSequence = 0;
let lastInputSent = 0;
let playerNumber = 0;
let joined = false;
let hostedLanding = false;
let reconnectDelay = 500;
let reconnectTimer = null;
let debugOn = false;
const G = 9.80665;
const D2R = Math.PI / 180;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
// Tested Motorola Chrome reports accelerationIncludingGravity with the opposite
// gravity polarity from tested iPhone Safari. Normalize Android to the working
// iOS convention used by PartyPad's DSU mapping. This is a full-vector inversion,
// not an axis remap; other browser/device combinations remain experimental.
const IS_ANDROID = /Android/i.test(navigator.userAgent);
const ACCEL_POLARITY = IS_ANDROID ? -1 : 1;
const CLIENT_ID_KEY = "partypad-controller-id";
const onlineMatch = location.hash.match(/^#\/join\/([A-Za-z0-9_-]{16,32})\/([A-Za-z0-9_-]{20,128})$/);
const onlineSession = onlineMatch ? { id: onlineMatch[1], secret: onlineMatch[2] } : null;

function controllerId() {
  const makeId = () => crypto.randomUUID
    ? crypto.randomUUID().replaceAll("-", "")
    : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
  try {
    let id = localStorage.getItem(CLIENT_ID_KEY);
    if (!id) {
      id = makeId();
      localStorage.setItem(CLIENT_ID_KEY, id);
    }
    return id;
  } catch {
    // Storage can be unavailable in private/restricted browser modes. The id
    // still remains stable for reconnects during this page's lifetime.
    if (!window.partypadControllerId) {
      window.partypadControllerId = makeId();
    }
    return window.partypadControllerId;
  }
}

function connect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const endpoint = onlineSession
    ? `${proto}://${location.host}/api/sessions/${onlineSession.id}/ws`
    : `${proto}://${location.host}/ws?client=${encodeURIComponent(controllerId())}`;
  ws = new WebSocket(endpoint);
  ws.onopen = () => { reconnectDelay = 500; setStatus("connected"); };
  ws.onclose = (event) => {
    inputChannel = null;
    if (rtc) rtc.close();
    rtc = null;
    const terminal = event.reason.includes("expired") || event.reason.includes("authentication") ||
      event.reason.includes("full");
    if (terminal) {
      joined = false;
      showJoin();
      setStatus(event.reason || "session unavailable");
    } else if (onlineSession && joined) {
      setStatus("reconnecting…");
      reconnectTimer = setTimeout(connect, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 10000);
    } else {
      setStatus("disconnected — tap Join");
      showJoin();
    }
  };
  ws.onerror = () => setStatus("connection error");
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch { return; }
    if (m.t === "hello" && onlineSession) {
      ws.send(JSON.stringify({ t: "auth", role: "controller", secret: onlineSession.secret,
                               client: controllerId() }));
    } else if (m.t === "auth_ok" && onlineSession) {
      const config = m.config || {};
      applyControllerMode(config.controller_mode || "wii", config.system);
      startWebRtc(m.ice_servers || []);
      setStatus("connected · relay");
    } else if (m.t === "control") {
      handleControl(m.message || {});
    } else if (m.t === "answer" && rtc) {
      rtc.setRemoteDescription({ type: "answer", sdp: m.sdp }).catch(() => {
        setStatus("connected · relay");
      });
    } else {
      handleControl(m);
    }
  };
}

function handleControl(m) {
  if (m.t === "welcome") {
      applyControllerMode(m.controller_mode || "wii", m.system);
      playerNumber = m.player;
      document.getElementById("player").textContent = m.player;
      setStatus("Player " + m.player + (onlineSession ? " · relay" : ""));
    } else if (m.t === "full") {
      joined = false;
      setStatus("all 4 controllers in use");
    } else if (m.t === "transport" && m.path) {
      setStatus(`Player ${playerNumber || "?"} · ${m.path}`);
    }
}
function setStatus(s) { document.getElementById("status").textContent = s; }

async function selectedPath(pc) {
  try {
    const stats = await pc.getStats();
    let pair = null;
    stats.forEach((item) => {
      if (item.type === "candidate-pair" && item.state === "succeeded" &&
          (item.selected || item.nominated)) pair = item;
    });
    if (!pair) return "WebRTC";
    const local = stats.get(pair.localCandidateId);
    const remote = stats.get(pair.remoteCandidateId);
    const relayed = local?.candidateType === "relay" || remote?.candidateType === "relay";
    const protocol = (local?.protocol || remote?.protocol || "").toUpperCase();
    const rtt = pair.currentRoundTripTime == null ? "" : ` · ${Math.round(pair.currentRoundTripTime * 1000)} ms`;
    return `${relayed ? "TURN" : "direct"}${protocol ? "/" + protocol : ""}${rtt}`;
  } catch {
    return "WebRTC";
  }
}

async function startWebRtc(iceServers) {
  if (!onlineSession || !window.RTCPeerConnection || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (rtc) rtc.close();
  rtc = new RTCPeerConnection({ iceServers, iceCandidatePoolSize: 1 });
  inputChannel = rtc.createDataChannel("input", { ordered: false, maxRetransmits: 0 });
  rtc.onicecandidate = ({ candidate }) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ t: "candidate", candidate: candidate ? candidate.toJSON() : null }));
    }
  };
  inputChannel.onopen = async () => {
    const path = await selectedPath(rtc);
    setStatus(`Player ${playerNumber || "?"} · ${path}`);
  };
  inputChannel.onclose = () => setStatus(`Player ${playerNumber || "?"} · relay`);
  rtc.onconnectionstatechange = () => {
    if (["failed", "disconnected", "closed"].includes(rtc.connectionState)) {
      setStatus(`Player ${playerNumber || "?"} · relay`);
    }
  };
  try {
    await rtc.setLocalDescription(await rtc.createOffer());
    // Send the offer immediately. ICE candidates continue over signaling as
    // they are discovered, avoiding tens of seconds of blocking on cellular.
    ws.send(JSON.stringify({ t: "offer", sdp: rtc.localDescription.sdp }));
  } catch {
    setStatus(`Player ${playerNumber || "?"} · relay`);
  }
}

function applyControllerMode(mode, system) {
  document.body.dataset.system = system || mode;
  document.body.dataset.controllerMode = mode;
  document.body.classList.toggle("profile-nes", mode === "nes");
  document.body.classList.toggle("profile-wii", mode === "wii");
  const a = document.querySelector(".btn.a");
  const b = document.querySelector(".btn.bkey");
  if (mode === "nes") {
    // Mesen's default Standard Controller maps NES A/B to RetroPad A/B.
    // The udev profile maps those to the east/south physical positions.
    a.dataset.field = "circle";
    b.dataset.field = "cross";
    document.querySelector(".join-card .sub").textContent = "Tap to grab an NES controller";
    document.querySelector(".hint").textContent = "Turn your phone sideways for the controller layout.";
  } else {
    a.dataset.field = "cross";
    b.dataset.field = "square";
  }
}

if (!onlineSession) {
  fetch("/config")
    .then((response) => response.json())
    .then((config) => {
      if (config.online_service) {
        hostedLanding = true;
        document.querySelector(".join-card .sub").textContent = "No active controller session";
        document.querySelector(".hint").textContent = "Start PartyPad on the computer, then scan its QR code.";
        document.getElementById("join-btn").disabled = true;
      } else {
        applyControllerMode(config.controller_mode || "wii", config.system);
      }
    })
    .catch(() => applyControllerMode("wii", "wii"));
}

let pendingRc = false;
const r2 = (n) => Math.round(n * 100) / 100;
function pump(timestamp) {
  const heartbeat = onlineSession && timestamp - lastInputSent >= 50;
  if ((dirty || heartbeat) && ws && ws.readyState === WebSocket.OPEN) {
    const msg = { t: "i", b: state.b, left_x: state.left_x, left_y: state.left_y,
                  px: pointer.px, py: pointer.py };
    if (state.m) msg.m = state.m;
    // raw orientation + aim, for diagnostic logging
    msg.o = [r2(motion.oa), r2(motion.ob), r2(motion.og)];
    msg.aim = [r2(pointer.az / D2R), r2(pointer.el / D2R)];
    if (pendingRc) { msg.rc = 1; pendingRc = false; }
    if (onlineSession) {
      const envelope = { seq: ++inputSequence, data: msg };
      if (inputChannel && inputChannel.readyState === "open" && inputChannel.bufferedAmount < 65536) {
        try {
          inputChannel.send(JSON.stringify(envelope));
        } catch {
          if (ws.bufferedAmount < 65536) ws.send(JSON.stringify({ t: "input", ...envelope }));
        }
      } else if (ws.bufferedAmount < 65536) {
        ws.send(JSON.stringify({ t: "input", ...envelope }));
      }
    } else if (ws.bufferedAmount < 65536) {
      ws.send(JSON.stringify(msg));
    }
    lastInputSent = timestamp;
    dirty = false;
  }
  if (debugOn) drawDebug();
  requestAnimationFrame(pump);
}
function setButton(field, down) {
  if (!!state.b[field] === down) return;
  state.b[field] = down; dirty = true;
}

// ---- momentary buttons (multitouch) ----
function bindButtons() {
  document.querySelectorAll("[data-field]").forEach((el) => {
    const press = (e) => { e.preventDefault(); try { el.setPointerCapture(e.pointerId); } catch {}
      el.classList.add("pressed"); setButton(el.dataset.field, true); };
    const release = (e) => { e.preventDefault(); el.classList.remove("pressed");
      setButton(el.dataset.field, false); };
    el.addEventListener("pointerdown", press);
    el.addEventListener("pointerup", release);
    el.addEventListener("pointercancel", release);
    el.addEventListener("lostpointercapture", release);
  });
}

// ---- D-pad: 8-way from touch position ----
function bindDpad() {
  const dpad = document.getElementById("dpad");
  const controls = document.querySelector(".controls");
  const arrows = { dpad_up: document.getElementById("arU"), dpad_down: document.getElementById("arD"),
                   dpad_left: document.getElementById("arL"), dpad_right: document.getElementById("arR") };
  let activeId = null;
  const apply = (up, down, left, right) => {
    setButton("dpad_up", up); setButton("dpad_down", down);
    setButton("dpad_left", left); setButton("dpad_right", right);
    arrows.dpad_up.classList.toggle("on", up); arrows.dpad_down.classList.toggle("on", down);
    arrows.dpad_left.classList.toggle("on", left); arrows.dpad_right.classList.toggle("on", right);
  };
  const update = (e) => {
    const r = dpad.getBoundingClientRect();
    const nx = (e.clientX - (r.left + r.width / 2)) / (r.width / 2);
    const ny = (e.clientY - (r.top + r.height / 2)) / (r.height / 2);
    // Every point in the D-pad region belongs to a directional sector. Adjacent
    // sectors overlap near the diagonals, so the gaps around the visible cross
    // remain useful hit area and diagonal input still works.
    const ax = Math.abs(nx), ay = Math.abs(ny);
    if (ax === 0 && ay === 0) return;
    apply(ny < 0 && ay >= ax * 0.45, ny > 0 && ay >= ax * 0.45,
          nx < 0 && ax >= ay * 0.45, nx > 0 && ax >= ay * 0.45);
  };
  const clear = () => {
    activeId = null;
    apply(false, false, false, false);
  };
  const start = (e) => {
    e.preventDefault();
    // A missing pointerup must never lock out subsequent touches. A new touch
    // takes ownership and clears any direction left behind by the old pointer.
    if (activeId !== null && activeId !== e.pointerId) clear();
    activeId = e.pointerId;
    try { dpad.setPointerCapture(e.pointerId); } catch {}
    update(e);
  };
  const move = (e) => { if (e.pointerId !== activeId) return; e.preventDefault(); update(e); };
  const end = (e) => { if (e.pointerId === activeId) clear(); };
  dpad.addEventListener("pointerdown", start);
  dpad.addEventListener("pointermove", move);
  dpad.addEventListener("pointerup", end);
  dpad.addEventListener("pointercancel", end);
  dpad.addEventListener("lostpointercapture", end);
  controls.addEventListener("pointerdown", (e) => {
    if (!document.body.classList.contains("profile-wii") || e.target.closest?.("button, #dpad")) return;
    const r = dpad.getBoundingClientRect();
    // Mobile browsers do not consistently include oversized generated content
    // in hit testing. Delegate otherwise-unused touches in the D-pad's vertical
    // band from the full-width controls container instead.
    if (e.clientY >= r.top && e.clientY <= r.bottom) start(e);
  });
  // Some mobile browsers can lose capture during fullscreen/orientation/UI
  // transitions. Window-level releases and lifecycle changes are fallbacks.
  window.addEventListener("pointerup", end, true);
  window.addEventListener("pointercancel", end, true);
  window.addEventListener("blur", clear);
  document.addEventListener("visibilitychange", () => { if (document.hidden) clear(); });
}

function bindRecenter() {
  const rc = document.getElementById("recenter");
  rc.addEventListener("pointerdown", (e) => { e.preventDefault(); rc.classList.add("pressed"); recenterPointer(); });
  rc.addEventListener("pointerup", () => rc.classList.remove("pressed"));
  rc.addEventListener("pointercancel", () => rc.classList.remove("pressed"));
}

// ---- IR pointer: device orientation -> quaternion -> world ray -> azimuth/elevation ----
// Euler angles couple, so raw alpha/beta deltas smear pitch into yaw. Instead we build
// the phone's pointing direction in world space and take true spherical angles, which
// are independent. Everything is relative to the "recenter" pose.
const POINT_LOCAL = [0, 1, 0];   // phone's top edge = the direction it points (Wii Remote grip)
const POINT_RANGE = 26 * D2R;    // radians from center for full stick deflection
let SGN_PX = 1, SGN_PY = 1;      // Pointer axes only; independent of Wii Remote motion/steering

function quatFromEuler(alpha, beta, gamma) {
  // W3C deviceorientation is intrinsic Z-X'-Y'' (radians)
  const cZ = Math.cos(alpha / 2), sZ = Math.sin(alpha / 2);
  const cX = Math.cos(beta / 2),  sX = Math.sin(beta / 2);
  const cY = Math.cos(gamma / 2), sY = Math.sin(gamma / 2);
  return {
    w: cX * cY * cZ - sX * sY * sZ,
    x: sX * cY * cZ - cX * sY * sZ,
    y: cX * sY * cZ + sX * cY * sZ,
    z: cX * cY * sZ + sX * sY * cZ,
  };
}
function rotateVec(q, v) {              // q * v * q^-1
  const tx = 2 * (q.y * v[2] - q.z * v[1]);
  const ty = 2 * (q.z * v[0] - q.x * v[2]);
  const tz = 2 * (q.x * v[1] - q.y * v[0]);
  return [
    v[0] + q.w * tx + (q.y * tz - q.z * ty),
    v[1] + q.w * ty + (q.z * tx - q.x * tz),
    v[2] + q.w * tz + (q.x * ty - q.y * tx),
  ];
}

const pointer = { az: 0, el: 0, px: 0, py: 0, has: false };
let pointerBase = null;

function recenterPointer() {
  if (pointer.has) pointerBase = { az: pointer.az, el: pointer.el };
  pointer.px = 0; pointer.py = 0; pendingRc = true; dirty = true;
}

// ---- motion sensors ----
const motion = { ax: 0, ay: 0, az: 0, ra: 0, rb: 0, rg: 0, oa: 0, ob: 0, og: 0, active: false };

function onDeviceMotion(e) {
  const a = e.accelerationIncludingGravity;
  if (a && a.x != null) {
    const mag = Math.hypot(a.x, a.y, a.z);
    const k = mag > 3 ? 1 / G : 1;        // normalize to g whether m/s^2 or already-g
    motion.ax = ACCEL_POLARITY * a.x * k;
    motion.ay = ACCEL_POLARITY * a.y * k;
    motion.az = ACCEL_POLARITY * a.z * k;
  }
  const r = e.rotationRate;
  if (r && r.alpha != null) { motion.ra = r.alpha; motion.rb = r.beta; motion.rg = r.gamma; }
  motion.active = true;
  state.m = { ax: motion.ax, ay: motion.ay, az: motion.az,
              ra: motion.ra, rb: motion.rb, rg: motion.rg,
              orient: screen.orientation ? screen.orientation.angle : window.orientation || 0,
              accel_polarity: ACCEL_POLARITY };
  dirty = true;
}

function onDeviceOrientation(e) {
  if (e.alpha == null) return;
  motion.oa = e.alpha; motion.ob = e.beta; motion.og = e.gamma;   // raw, for logging
  const q = quatFromEuler(e.alpha * D2R, e.beta * D2R, e.gamma * D2R);
  const d = rotateVec(q, POINT_LOCAL);
  pointer.az = Math.atan2(d[0], d[1]);                       // yaw in the horizontal plane
  pointer.el = Math.atan2(d[2], Math.hypot(d[0], d[1]));     // elevation above horizontal
  pointer.has = true;
  if (pointerBase) {
    let dAz = pointer.az - pointerBase.az;
    dAz = Math.atan2(Math.sin(dAz), Math.cos(dAz));          // shortest-path unwrap
    const dEl = pointer.el - pointerBase.el;
    pointer.px = clamp(SGN_PX * dAz / POINT_RANGE, -1, 1);
    pointer.py = clamp(SGN_PY * dEl / POINT_RANGE, -1, 1);
  }
  motion.active = true;
  dirty = true;
}

async function enableMotion() {
  const askMotion = window.DeviceMotionEvent && DeviceMotionEvent.requestPermission;
  const askOrient = window.DeviceOrientationEvent && DeviceOrientationEvent.requestPermission;
  try {
    if (askMotion) { const p = await DeviceMotionEvent.requestPermission(); if (p !== "granted") return false; }
    if (askOrient) { const p = await DeviceOrientationEvent.requestPermission(); if (p !== "granted") return false; }
  } catch { return false; }
  window.addEventListener("devicemotion", onDeviceMotion);
  window.addEventListener("deviceorientation", onDeviceOrientation);
  return true;
}

// ---- debug readout ----
function drawDebug() {
  const el = document.getElementById("debug");
  const f = (n) => (n >= 0 ? " " : "") + n.toFixed(2);
  el.textContent =
    `motion ${motion.active ? "ON" : "off"}  base ${pointerBase ? "set" : "—"}  accel×${ACCEL_POLARITY}\n` +
    `accel g   x${f(motion.ax)} y${f(motion.ay)} z${f(motion.az)}\n` +
    `rot deg/s a${f(motion.ra)} b${f(motion.rb)} g${f(motion.rg)}\n` +
    `aim deg   az${f(pointer.az / D2R)} el${f(pointer.el / D2R)}\n` +
    `cursor    px${f(pointer.px)} py${f(pointer.py)}`;
}

// ---- join / lifecycle ----
async function join() {
  if (hostedLanding) return;
  joined = true;
  document.getElementById("join").classList.add("hidden");
  document.getElementById("pad").classList.remove("hidden");
  const ok = await enableMotion();     // request sensors first, inside the tap gesture
  try { if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen(); } catch {}
  connect();
  if (!ok) setTimeout(() => setStatus("motion off (buttons only)"), 800);
}
function showJoin() {
  joined = false;
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  if (inputChannel) inputChannel.close();
  if (rtc) rtc.close();
  inputChannel = null;
  rtc = null;
  document.getElementById("join").classList.remove("hidden");
  document.getElementById("pad").classList.add("hidden");
}

document.getElementById("join-btn").addEventListener("click", join);
document.getElementById("status").addEventListener("click", () => {
  debugOn = !debugOn;
  document.getElementById("debug").classList.toggle("hidden", !debugOn);
});
bindButtons();
bindDpad();
bindRecenter();
requestAnimationFrame(pump);

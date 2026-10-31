"""The observer console's one page: HTML, CSS and JavaScript, inline.

No build step and nothing fetched from anywhere else (the site may have no
internet). The page carries no secret and no data: it signs in, then reads
``/console/api/events`` (server-sent events; polling ``/console/api/status``
if the stream fails) and draws everything from that, building the DOM with
``textContent`` only, so a string from a device or a request cannot become
markup. Themes: dark (the default), red (for a dark control room: red on
black, no other hue) and light; the choice is kept in the browser.
"""

from __future__ import annotations

import html

TITLE = "robofast console"

CSS = """
:root, :root[data-theme="dark"] {
  --bg: #0b0d10; --panel: #14181d; --line: #262c34; --text: #d5dbe3; --dim: #8892a0;
  --ok: #3fb27f; --warn: #e0a030; --bad: #e5534b; --accent: #5aa0e6; --btn: #1d232b;
}
:root[data-theme="red"] {
  --bg: #000; --panel: #0d0000; --line: #3a0000; --text: #d02020; --dim: #801010;
  --ok: #c01818; --warn: #ff3030; --bad: #ff4040; --accent: #e02020; --btn: #1a0000;
}
:root[data-theme="light"] {
  --bg: #f4f5f7; --panel: #fff; --line: #d8dce2; --text: #1d232b; --dim: #5d6673;
  --ok: #1e7f52; --warn: #a86500; --bad: #c0332b; --accent: #1f62b0; --btn: #eef0f3;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--text); }
body { font: 15px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; padding: 0 12px 40px; }
header { position: sticky; top: 0; z-index: 2; background: var(--bg); display: flex;
  flex-wrap: wrap; gap: 8px 16px; align-items: center; padding: 10px 0;
  border-bottom: 1px solid var(--line); }
header h1 { font-size: 17px; margin: 0; }
.grow { flex: 1; }
.dim { color: var(--dim); }
main { display: grid; gap: 12px; margin-top: 12px;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr)); }
section { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 12px; min-width: 0; }
section.wide { grid-column: 1 / -1; }
section h2 { font-size: 14px; text-transform: uppercase; letter-spacing: .06em;
  color: var(--dim); margin: 0 0 8px; }
h3 { font-size: 15px; margin: 10px 0 4px; }
.card { border-top: 1px solid var(--line); padding: 8px 0; }
.card:first-of-type { border-top: 0; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 2px 12px; }
.kv dt { color: var(--dim); }
.kv dd { margin: 0; overflow-wrap: anywhere; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-weight: 600;
  border: 1px solid currentColor; font-size: 13px; }
.ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); }
.big { font-size: 20px; font-weight: 700; }
button, input, textarea, select { font: inherit; color: var(--text); background: var(--btn);
  border: 1px solid var(--line); border-radius: 6px; }
button { padding: 8px 12px; min-height: 40px; cursor: pointer; }
button.danger { border-color: var(--bad); color: var(--bad); }
button:disabled { opacity: .4; cursor: default; }
input, textarea { padding: 8px; width: 100%; }
textarea { min-height: 140px; font-family: ui-monospace, monospace; font-size: 13px; }
.actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
ul.list { list-style: none; margin: 0; padding: 0; }
ul.list li { padding: 4px 0; border-top: 1px solid var(--line); overflow-wrap: anywhere; }
ul.list li:first-child { border-top: 0; }
li.acked { opacity: .55; }
.bar { height: 8px; background: var(--line); border-radius: 4px; overflow: hidden; }
.bar > div { height: 100%; width: 0; background: var(--accent); }
.mono { font-family: ui-monospace, monospace; font-size: 13px; }
.scroll { max-height: 360px; overflow: auto; }
dialog { background: var(--panel); color: var(--text); border: 1px solid var(--line);
  border-radius: 8px; width: min(94vw, 460px); }
dialog::backdrop { background: rgba(0,0,0,.6); }
#signin { max-width: 460px; }
button.emergency { background: var(--bad); color: var(--bg); border-color: var(--bad);
  font-weight: 800; letter-spacing: .04em; min-height: 44px; padding: 8px 14px; }
#emergency-dialog { border: 2px solid var(--bad); }
#emergency-dialog h3 { color: var(--bad); }
#em-text, #em-result { white-space: pre-line; }
.strip { display: flex; flex-wrap: wrap; gap: 8px; }
.thumb { display: flex; flex-direction: column; align-items: center; gap: 2px; cursor: pointer;
  background: none; border: 1px solid var(--line); padding: 4px; min-height: 0; }
.thumb canvas { width: 150px; height: 150px; background: #000; image-rendering: pixelated; }
#img-canvas { width: 100%; max-width: 900px; display: block; background: #000; touch-action: none;
  cursor: crosshair; image-rendering: pixelated; margin-top: 6px; }
.actions select, .actions label { font-size: 14px; }
a.button { display: inline-flex; align-items: center; padding: 8px 12px; min-height: 40px;
  border: 1px solid var(--line); border-radius: 6px; background: var(--btn); color: var(--text);
  text-decoration: none; }
label.check { display: flex; gap: 8px; align-items: center; margin: 8px 0; }
label.check input { width: auto; }
.hidden { display: none !important; }
"""

BODY = """
<header id="top">
  <button id="emergency" class="emergency hidden" type="button"
    aria-haspopup="dialog">EMERGENCY CLOSE</button>
  <h1>robofast console</h1>
  <span id="conn" class="badge warn">connecting</span>
  <span id="age" class="dim"></span>
  <span class="grow"></span>
  <span id="who" class="dim"></span>
  <select id="theme" aria-label="theme">
    <option value="dark">dark</option><option value="red">red light</option>
    <option value="light">light</option>
  </select>
  <button id="signout" class="hidden" type="button">sign out</button>
</header>
<section id="signin" class="hidden">
  <h2>Sign in</h2>
  <p class="dim">Paste your console token (ask the site admin for a named one).
  It is sent once; the page keeps only a session cookie.</p>
  <input id="token" type="password" autocomplete="off" aria-label="token">
  <div class="actions"><button id="signin-go" type="button">sign in</button></div>
  <p id="signin-msg" class="bad"></p>
</section>
<main id="main" class="hidden">
  <section id="alarms" class="wide"><h2>Alarms</h2><ul class="list" id="alarm-list"></ul></section>
  <section id="weather"><h2>Weather</h2><div id="weather-body"></div></section>
  <section id="enclosure"><h2>Enclosure</h2><div id="enclosure-body"></div></section>
  <section id="telescopes" class="wide"><h2>Telescopes</h2><div id="telescope-body"></div></section>
  <section id="images" class="wide"><h2>Images</h2>
    <div class="actions">
      <label>guiders <select id="img-rate" aria-label="guider refresh rate">
        <option value="0">paused</option><option value="0.5">0.5 Hz</option>
        <option value="1">1 Hz</option><option value="2" selected>2 Hz</option>
        <option value="4">4 Hz</option></select></label>
      <span id="img-rate-note" class="dim"></span>
    </div>
    <div id="guider-strip" class="strip"></div>
    <div class="actions">
      <select id="img-source" aria-label="image source"></select>
      <select id="img-scale" aria-label="scale">
        <option value="zscale">zscale</option><option value="99.5">99.5%</option>
        <option value="99">99%</option><option value="minmax">min-max</option></select>
      <select id="img-stretch" aria-label="stretch">
        <option value="linear">linear</option><option value="asinh">asinh</option>
        <option value="log">log</option><option value="sqrt">sqrt</option></select>
      <select id="img-max" aria-label="resolution">
        <option value="256">256 px</option><option value="512" selected>512 px</option>
        <option value="1024">1024 px</option></select>
      <button id="img-zin" type="button" aria-label="zoom in">+</button>
      <button id="img-zout" type="button" aria-label="zoom out">&minus;</button>
      <button id="img-fit" type="button">fit</button>
      <a id="img-fits" class="button" download>FITS</a>
    </div>
    <canvas id="img-canvas" width="512" height="512" aria-label="image"></canvas>
    <div id="img-info" class="mono dim"></div>
  </section>
  <section id="spectrograph"><h2>Spectrograph</h2><div id="spectrograph-body"></div></section>
  <section id="plan"><h2>Plan</h2><div id="plan-body"></div></section>
  <section id="timeline" class="wide"><h2>Timeline</h2>
    <ul class="list scroll mono" id="timeline-list"></ul></section>
  <section id="requests" class="wide"><h2>Requests and ToOs</h2>
    <div id="requests-body">
      <p class="dim">A request group as JSON (the request API's format; docs/console.md).
      Submitting goes through the request API and the scheduler's drop box.</p>
      <textarea id="rq-json" aria-label="request group JSON" spellcheck="false"></textarea>
      <div class="actions">
        <button id="rq-validate" type="button">validate</button>
        <button id="rq-submit" type="button">submit request</button>
        <button id="rq-too" type="button">submit ToO</button>
      </div>
      <div class="actions">
        <input id="rq-cancel-id" placeholder="request group id" aria-label="group id to cancel">
        <button id="rq-cancel" class="danger" type="button">cancel group</button>
        <button id="rq-list" type="button">my groups</button>
      </div>
      <pre id="rq-out" class="mono scroll"></pre>
    </div>
  </section>
</main>
<dialog id="emergency-dialog" aria-labelledby="em-title">
  <h3 id="em-title">Emergency close</h3>
  <label for="em-enclosure" class="dim">Enclosure</label>
  <select id="em-enclosure"></select>
  <p id="em-text"></p>
  <label class="check"><input id="em-direct" type="checkbox">
    skip the supervisor: send the close straight to the dome</label>
  <label for="em-reason" class="dim">Why (optional; recorded with your name)</label>
  <input id="em-reason" autocomplete="off">
  <p id="em-result" class="mono"></p>
  <div class="actions">
    <button id="em-cancel" type="button">cancel</button>
    <button id="em-go" class="emergency" type="button">CLOSE NOW</button>
  </div>
</dialog>
<dialog id="confirm">
  <h3 id="confirm-title"></h3>
  <p id="confirm-text"></p>
  <label for="confirm-reason" class="dim">Why (recorded with your name)</label>
  <input id="confirm-reason" autocomplete="off">
  <div class="actions">
    <button id="confirm-cancel" type="button">cancel</button>
    <button id="confirm-go" class="danger" type="button">confirm</button>
  </div>
</dialog>
"""

JS = r"""
"use strict";
const $ = (id) => document.getElementById(id);
let session = null, stream = null, pollTimer = null, lastDoc = null, lastAt = 0;

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function fill(id, ...kids) { const el = $(id); el.replaceChildren(...kids.flat(Infinity)); return el; }
function fmt(v, digits) {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(digits ?? 2);
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v);
}
function ago(iso) {
  if (!iso) return "—";
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (!isFinite(s)) return String(iso);
  if (s < 90) return Math.round(s) + " s ago";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  return Math.round(s / 3600) + " h ago";
}
function badge(text, cls) { return h("span", {class: "badge " + cls}, text); }
function kv(pairs) {
  return h("dl", {class: "kv"}, pairs.filter(Boolean).map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]));
}
function safeBadge(safe) {
  return safe === true ? badge("SAFE", "ok") : safe === false ? badge("UNSAFE", "bad") : badge("UNKNOWN", "warn");
}

// -- theme -------------------------------------------------------------------------
function setTheme(t) {
  document.documentElement.dataset.theme = t;
  $("theme").value = t;
  try { localStorage.setItem("robofast-theme", t); } catch (e) { /* private mode */ }
}
let savedTheme = "dark";
try { savedTheme = localStorage.getItem("robofast-theme") || "dark"; } catch (e) { /* none */ }
setTheme(["dark", "red", "light"].includes(savedTheme) ? savedTheme : "dark");
$("theme").addEventListener("change", (ev) => setTheme(ev.target.value));

// -- calls -------------------------------------------------------------------------
async function call(method, path, body) {
  const opts = {method, credentials: "same-origin", headers: {}};
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = typeof body === "string" ? body : JSON.stringify(body);
  }
  if (method !== "GET" && session && session.csrf) opts.headers["X-CSRF-Token"] = session.csrf;
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { data = {error: "no JSON answer"}; }
  if (r.status === 401) signedOut();
  return {status: r.status, ok: r.ok, data};
}

function ask(title, text, danger) {
  return new Promise((resolve) => {
    const d = $("confirm");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    $("confirm-reason").value = "";
    $("confirm-go").className = danger ? "danger" : "";
    const done = (v) => { d.close(); $("confirm-go").onclick = null; $("confirm-cancel").onclick = null; resolve(v); };
    $("confirm-go").onclick = () => {
      const r = $("confirm-reason").value.trim();
      if (!r) { $("confirm-reason").focus(); return; }
      done(r);
    };
    $("confirm-cancel").onclick = () => done(null);
    d.showModal();
    $("confirm-reason").focus();
  });
}

async function act(action, body, title, text, danger) {
  const reason = await ask(title, text, danger);
  if (reason === null) return;
  const r = await call("POST", "/console/api/" + action, {...body, reason, confirm: true});
  if (!r.ok) alert(action + " refused: " + (r.data && r.data.error || r.status));
  else if (r.data.result && r.data.result.hold_confirmed === false)
    alert("Closed, but the sequencer has not confirmed the hold yet: watch it.");
}

// -- sign in ----------------------------------------------------------------------
function signedOut() {
  session = null;
  if (stream) { stream.close(); stream = null; }
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  $("main").classList.add("hidden");
  $("signout").classList.add("hidden");
  $("emergency").classList.add("hidden");
  $("signin").classList.remove("hidden");
  $("who").textContent = "";
  setConn("signed out", "warn");
}
function signedIn(s) {
  session = s;
  $("emergency").classList.toggle("hidden", !s.can.operate);
  $("signin").classList.add("hidden");
  $("main").classList.remove("hidden");
  $("signout").classList.remove("hidden");
  $("who").textContent = s.name + (s.can.operate ? " (operator)" : " (read only)");
  $("requests").classList.toggle("hidden", !s.requests);
  connect();
  loadImageSources();
}
$("signin-go").addEventListener("click", async () => {
  const token = $("token").value.trim();
  $("token").value = "";
  const r = await call("POST", "/console/login", {token});
  if (r.ok) { $("signin-msg").textContent = ""; signedIn(r.data); }
  else $("signin-msg").textContent = r.data && r.data.error || "sign-in failed";
});
$("signout").addEventListener("click", async () => { await call("POST", "/console/logout", {}); signedOut(); });

// -- the feed -----------------------------------------------------------------------
function setConn(text, cls) { const c = $("conn"); c.textContent = text; c.className = "badge " + cls; }
function connect() {
  if (window.EventSource) {
    stream = new EventSource("/console/api/events");
    stream.addEventListener("status", (ev) => { setConn("live", "ok"); render(JSON.parse(ev.data)); });
    stream.addEventListener("signed-out", () => signedOut());
    stream.onerror = () => {
      setConn("reconnecting", "warn");
      if (stream && stream.readyState === EventSource.CLOSED) { stream = null; startPolling(); }
    };
  } else startPolling();
}
function startPolling() {
  if (pollTimer) return;
  const poll = async () => {
    const r = await call("GET", "/console/api/status");
    if (r.ok) { setConn("polling", "ok"); render(r.data); } else setConn("no data", "bad");
  };
  poll();
  pollTimer = setInterval(poll, 5000);
}
setInterval(() => {
  if (!lastAt) return;
  const s = Math.round((Date.now() - lastAt) / 1000);
  $("age").textContent = "updated " + s + " s ago";
  if (s > 20) setConn("stale", "bad");
}, 1000);

// -- drawing -------------------------------------------------------------------------
const can = () => session && session.can.operate;
function opButton(label, action, body, title, text, danger) {
  return h("button", {type: "button", class: danger ? "danger" : "", disabled: !can(),
    onclick: () => act(action, body, title, text, danger)}, label);
}

function render(doc) {
  lastDoc = doc; lastAt = Date.now();
  const encs = Object.entries(doc.enclosures || {});
  const multi = encs.length > 1;
  const title = (name) => multi ? h("h3", {}, name) : null;
  const alarms = encs.flatMap(([, e]) => e.alarms || []);
  alarms.sort((a, b) => (a.ack ? 1 : 0) - (b.ack ? 1 : 0));
  fill("alarm-list", alarms.length ? alarms.map((a) => h("li", {class: a.ack ? "acked" : ""},
    badge(a.level, a.level === "warning" ? "warn" : "bad"), " ",
    multi ? a.enclosure + ": " : "", a.text, a.detail ? h("span", {class: "dim"}, " — " + a.detail) : null,
    a.ack ? h("span", {class: "dim"}, " (acknowledged by " + a.ack.by + ", " + ago(a.ack.time) + ": " + a.ack.reason + ")")
      : h("span", {}, " ", opButton("acknowledge", "ack", {id: a.id, enclosure: a.enclosure},
          "Acknowledge alarm", a.text, false))
  )) : h("li", {class: "dim"}, "no alarms"));

  fill("weather-body", encs.map(([name, e]) => {
    const w = e.weather || {}, b = w.bridge;
    const parts = [title(name), h("div", {class: "big"}, safeBadge(w.safe)), h("p", {}, fmt(w.reason))];
    if (b && b.reachable) {
      if ((b.would_close || []).length) parts.push(h("p", {class: "warn"}, "would close: " + b.would_close.join("; ")));
      if (b.rain_until) parts.push(h("p", {class: "warn"}, "rain hold until " + b.rain_until));
      for (const [sn, s] of Object.entries(b.sources || {})) {
        parts.push(h("div", {class: "card"}, h("strong", {}, sn), " ",
          badge(fmt(s.state), s.state === "safe" ? "ok" : s.state === "unsafe" ? "bad" : "warn"),
          h("span", {class: "dim"}, " age " + fmt(s.age, 0) + " s"),
          kv(Object.entries(s.values || {}).map(([k, v]) => [k, fmt(v)])),
          (s.reasons || []).length ? h("p", {class: "dim"}, s.reasons.join("; ")) : null));
      }
    } else if (b) parts.push(h("p", {class: "bad"}, "weather bridge unreachable"));
    return h("div", {}, parts);
  }));

  fill("enclosure-body", encs.map(([name, e]) => {
    const n = e.enclosure || {};
    if (!n.reachable) return h("div", {}, title(name), h("p", {class: "bad"}, "safety supervisor unreachable"));
    const l = n.lease;
    const holds = Object.entries(n.holds || {});
    return h("div", {}, title(name),
      h("div", {class: "big"}, badge(fmt(n.shutter), n.shutter === "CLOSED" ? "ok" : (!n.shutter || n.shutter === "ERROR") ? "bad" : "warn")),
      kv([
        ["fault", n.fault ? h("span", {class: "bad"}, n.fault) : "none"],
        ["open allowed", fmt(n.open_allowed)],
        ["lease", l ? ("#" + l.id + " " + l.holder + (l.live ? " live" : "") + (l.opened ? ", opened" : "") +
          (l.revoked ? ", revoked: " + fmt(l.reason) : "") + (l.quarantined ? ", QUARANTINED" : "") +
          (l.released ? ", released" : "")) : "none"],
        ["heartbeat", n.heartbeat_age === null || n.heartbeat_age === undefined ? "—" : fmt(n.heartbeat_age, 1) + " s ago"],
        ["holds", holds.length ? holds.map(([t, x]) => t + ": " + x.action + " by " + x.operator + " (" + x.reason + ")").join("; ") : "none"],
        ["sequencer", e.sequencer ? (e.sequencer.running ? "running" : e.sequencer.running === false ? h("span", {class: "bad"}, "not running") : "—") : "—"],
      ]),
      h("div", {class: "actions"},
        opButton("pause all", "pause", {enclosure: name}, "Pause " + name,
          "Every telescope finishes its current exposure, then holds. The roof is not touched.", false),
        opButton("resume all", "resume", {enclosure: name}, "Resume " + name,
          "Lift the holds. The supervisor alone decides when to open.", false),
        opButton("STOP + close", "stop", {enclosure: name}, "Stop " + name + " now",
          "Abort every exposure, park every telescope and close (the lease is revoked). " +
          "The telescopes stay held until resumed.", true),
        opButton("clear lease", "clear_lease", {enclosure: name}, "Clear the dead lease",
          "Only once the telescopes are safe: this lets the supervisor grant a new lease.", true),
        opButton("clear faults", "clear_faults", {enclosure: name}, "Clear enclosure faults",
          "ClearFaults on the enclosure controller (refused unless it is closed).", true)));
  }));

  fill("telescope-body", encs.flatMap(([name, e]) => Object.entries(e.telescopes || {}).map(([t, x]) => {
    const v = x.values || {}, g = x.guider, cur = x.current || {};
    const val = (k, d) => v[k] ? fmt(v[k].value, d) + (v[k].unit ? " " + v[k].unit : "") + (v[k].stale ? " (stale)" : "") : "—";
    const st = x.state;
    return h("div", {class: "card"},
      h("h3", {}, (multi ? name + " / " : "") + t, " ",
        badge(fmt(st), st === "observing" ? "ok" : (st === "gave_up" || st === "backoff") ? "bad" : "warn"),
        x.exposing ? badge("exposing", "ok") : null,
        x.hold ? badge("held: " + x.hold.action, "warn") : null),
      kv([
        ["observing", cur.observation || cur.configuration || cur.request ? fmt(cur.observation || cur.configuration) + " (" + fmt(cur.request) + ")" : "—"],
        ["alt / az", val("alt", 1) + " / " + val("az", 1)],
        ["focus", val("focus", 0)],
        ["guiding", g ? (g.reachable ? fmt(g.state) + ", rms " + fmt(g.rms) + " px, flux " + fmt(g.flux, 0) : "guider unreachable")
          : (v.guide_state ? fmt(v.guide_state.value) + ", rms " + val("guide_rms") + ", flux " + val("guide_flux", 0) : "—")],
        ["last frame", v.last_frame_start ? fmt(v.last_frame_start.value) + " (" + val("last_frame_exptime", 1) + ")" : "—"],
        x.hold ? ["hold", x.hold.action + " by " + x.hold.operator + ", " + ago(x.hold.since) + ": " + x.hold.reason] : null,
        x.consecutive_errors ? ["errors", fmt(x.consecutive_errors)] : null,
      ]),
      h("div", {class: "actions"},
        x.hold ? opButton("resume", "resume", {enclosure: name, telescope: t}, "Resume " + t, "Lift the hold on " + t + ".", false)
          : opButton("pause", "pause", {enclosure: name, telescope: t}, "Pause " + t,
              t + " finishes its current exposure, then holds. The roof is not touched.", false),
        opButton("stop now", "stop", {enclosure: name, telescope: t}, "Stop " + t + " now",
          "Abort " + t + "'s exposure and slew and hold it (it stops tracking; it is parked when the enclosure closes). The roof is not touched.", true),
        opButton("park now", "park", {enclosure: name, telescope: t}, "Park " + t + " now",
          "Abort " + t + "'s exposure and slew, hold it and park it now. Only this mount moves: the roof and the other telescopes are not touched. Resume lifts the hold.", true)));
  })));

  fill("spectrograph-body", encs.map(([name, e]) => {
    const s = e.spectrograph;
    if (s === null || s === undefined) return h("div", {}, title(name), h("p", {class: "dim"}, "none configured"));
    const hl = (e.sequencer || {}).spectrograph;
    const health = hl ? h("div", {class: "actions"},
      hl.down ? badge("DOWN: " + hl.down, "bad") : badge("up", "ok"),
      hl.failures ? h("span", {class: "dim"}, " " + hl.failures + " failure(s)") : null,
      opButton("clear spectrograph", "clear_spectrograph", {enclosure: name}, "Clear the spectrograph",
        "The sequencer takes spectra again (it marked the spectrograph down after repeated failures). Only once it is fixed.", true)) : null;
    if (!s.reachable) return h("div", {}, title(name), health, h("p", {class: "bad"}, "spectrograph service unreachable"));
    const queue = (s.queue || []).length ? h("p", {class: "dim"}, "waiting: " +
      s.queue.map((q) => fmt(q.client) + " (" + fmt(q.what) + ", " + fmt(q.waiting, 0) + " s)").join("; ")) : null;
    const j = s.job;
    const bar = h("div", {class: "bar"}, h("div", {}));
    if (j && j.exptime && j.elapsed !== null) bar.firstChild.style.width = Math.min(100, 100 * j.elapsed / j.exptime) + "%";
    return h("div", {}, title(name), health,
      h("div", {class: "big"}, badge(s.busy ? "busy: " + s.busy : "idle", s.busy ? "ok" : "warn")), queue,
      j ? h("div", {}, kv([
        ["job", "#" + j.id + " " + fmt(j.kind) + " " + fmt(j.state) + (j.phase ? " (" + j.phase + ")" : "")],
        ["frame", fmt(j.frames_done) + " of " + fmt(j.frames) + ": " + fmt(j.frame_kind) + " " + fmt(j.object)],
        ["exposure", fmt(j.elapsed, 0) + " / " + fmt(j.exptime, 0) + " s" + (j.target_counts ? ", target " + fmt(j.target_counts, 0) + " counts" : "")],
        j.error ? ["error", h("span", {class: "bad"}, j.error)] : null,
      ]), bar) : null,
      kv([["exp. meter", fmt(s.expmeter_total, 0)], ["CCD", fmt(s.ccd_temp, 1) + " C"],
          ["iodine stage", fmt(s.stage)], ["pressure", fmt(s.spec_pressure, 4)]]));
  }));

  fill("plan-body", encs.map(([name, e]) => {
    const p = e.plan || {};
    return h("div", {}, title(name),
      h("h3", {}, "Recent decisions", p.record ? h("span", {class: "dim"}, " (" + p.record + ")") : null),
      h("ul", {class: "list scroll"}, (p.recent || []).slice().reverse().map((d) => h("li", {},
        h("span", {class: "dim mono"}, (d.t || "").slice(11, 19) + " "), h("strong", {}, d.kind), " ",
        [d.telescope, d.request || d.group, d.observation, d.outcome, d.why || d.reason, d.policy]
          .filter((x) => x !== undefined && x !== null).join(" · "),
        d.score !== undefined ? h("span", {class: "dim"}, " score " + fmt(d.score)) : null))),
      h("h3", {}, "Upcoming (" + fmt(p.queue_count ?? (p.upcoming || []).length) + " pending)"),
      h("ul", {class: "list scroll"}, (p.upcoming || []).map((u) => h("li", {},
        u.too ? badge("ToO", "warn") : null, " ", u.id, h("span", {class: "dim"},
          " p" + fmt(u.priority) + " " + fmt(u.submitter) + " " + fmt(u.intake) + (u.cancel_pending ? " (cancel pending)" : ""))))));
  }));

  const tl = encs.flatMap(([name, e]) => (e.timeline || []).map((x) => ({...x, enc: name})));
  tl.sort((a, b) => (a.time < b.time ? 1 : -1));
  fill("timeline-list", tl.map((x) => h("li", {},
    h("span", {class: "dim"}, (x.time || "").slice(11, 19) + " "), multi ? x.enc + " " : "",
    x.kind === "command" ? x.source + " → " + fmt(x.device) + " " + x.what + " " + x.outcome
      : fmt(x.source) + ": " + fmt(x.what) + (x.detail && Object.keys(x.detail).length ? " " + JSON.stringify(x.detail) : ""))));
}

// -- emergency close ---------------------------------------------------------------------
function emergencyText(names) {
  const encs = (lastDoc && lastDoc.enclosures) || {};
  const parts = names.map((n) => {
    const em = ((encs[n] || {}).enclosure || {}).emergency || {};
    const park = em.park_before_close ? " The supervisor parks the telescopes first (roll-off roof)." : "";
    const direct = em.fallback === "dome"
      ? " If the supervisor does not answer within 2 s, the close goes straight to the dome: it does NOT wait for the telescopes to park, and the roof stays latched shut until someone clears the faults."
      : " If the supervisor does not answer, the console has no other way to close it (no emergency channel configured).";
    return (names.length > 1 ? n + ": " : "") + "Closes now through the safety supervisor." + park + direct;
  });
  return parts.join("\n") + "\nEvery telescope is stopped and held, so nothing reopens until someone resumes.";
}
function openEmergency() {
  const names = (session && Object.keys(session.enclosures || {})) || [];
  const sel = $("em-enclosure");
  sel.replaceChildren(...(names.length > 1 ? [h("option", {value: ""}, "all enclosures")] : []),
    ...names.map((n) => h("option", {value: n}, n)));
  const update = () => { $("em-text").textContent = emergencyText(sel.value ? [sel.value] : names); };
  sel.onchange = update;
  update();
  $("em-direct").checked = false;
  $("em-reason").value = "";
  $("em-result").textContent = "";
  $("em-go").disabled = false;
  $("emergency-dialog").showModal();
}
$("emergency").addEventListener("click", openEmergency);
$("em-cancel").addEventListener("click", () => $("emergency-dialog").close());
$("em-go").addEventListener("click", async () => {
  $("em-go").disabled = true;
  $("em-result").textContent = "closing...";
  const body = {reason: $("em-reason").value.trim(), direct: $("em-direct").checked, confirm: true};
  if ($("em-enclosure").value) body.enclosure = $("em-enclosure").value;
  const r = await call("POST", "/console/api/emergency_close", body);
  if (r.ok) {
    const res = (r.data.result || {}).enclosures || {};
    $("em-result").textContent = Object.entries(res).map(([n, x]) =>
      n + ": closing via " + x.via + (x.supervisor ? " (supervisor: " + x.supervisor + ")" : "")).join("\n");
  } else {
    $("em-result").textContent = "NOT CLOSED: " + ((r.data && r.data.error) || r.status);
    $("em-go").disabled = false;
  }
});

// -- images ---------------------------------------------------------------------------
// Frames are scaled on the server (PNG); each guider thumbnail is fetched at
// the chosen rate while the page is visible, the viewer when its source
// changes (ETag: an unchanged frame costs a 304 and no pixels).
const imgState = {sources: [], thumbs: {}, view: null, timer: null, busy: {}, bytes: 0, since: Date.now()};
async function fetchFrame(enc, src, params, etag) {
  const q = new URLSearchParams(params).toString();
  const r = await fetch("/console/api/image/" + encodeURIComponent(enc) + "/" + encodeURIComponent(src) + "?" + q,
    {credentials: "same-origin", headers: etag ? {"If-None-Match": etag} : {}});
  if (r.status === 304) return {same: true};
  if (r.status === 401) { signedOut(); return null; }
  if (!r.ok) { let e = null; try { e = (await r.json()).error; } catch (x) { /* no JSON */ } return {error: e || String(r.status)}; }
  const blob = await r.blob();
  imgState.bytes += blob.size;
  let meta = {};
  try { meta = JSON.parse(r.headers.get("X-Frame") || "{}"); } catch (e) { /* none */ }
  return {bitmap: await createImageBitmap(blob), meta, etag: r.headers.get("ETag")};
}
function drawOverlays(ctx, meta, sx, sy, ox, oy) {
  const o = meta.overlays || {};
  const X = (x) => (x - ox) * sx, Y = (y) => (y - oy) * sy;
  ctx.lineWidth = Math.max(1, (window.devicePixelRatio || 1));
  const css = getComputedStyle(document.documentElement);
  if (o.window) { ctx.strokeStyle = css.getPropertyValue("--dim"); ctx.strokeRect(X(o.window.x0), Y(o.window.y0), (o.window.x1 - o.window.x0) * sx, (o.window.y1 - o.window.y0) * sy); }
  if (o.target) {
    ctx.strokeStyle = css.getPropertyValue("--ok");
    const x = X(o.target.x + 0.5), y = Y(o.target.y + 0.5), r = Math.max(3, o.target.r * sx);
    ctx.beginPath(); ctx.arc(x, y, r, 0, 2 * Math.PI); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(x - 2 * r, y); ctx.lineTo(x - r, y); ctx.moveTo(x + r, y); ctx.lineTo(x + 2 * r, y);
    ctx.moveTo(x, y - 2 * r); ctx.lineTo(x, y - r); ctx.moveTo(x, y + r); ctx.lineTo(x, y + 2 * r); ctx.stroke();
  }
  if (o.star) {
    ctx.strokeStyle = css.getPropertyValue("--warn");
    const x = X(o.star.x + 0.5), y = Y(o.star.y + 0.5);
    ctx.beginPath(); ctx.moveTo(x - 5, y - 5); ctx.lineTo(x + 5, y + 5); ctx.moveTo(x + 5, y - 5); ctx.lineTo(x - 5, y + 5); ctx.stroke();
  }
}
async function loadImageSources() {
  const r = await call("GET", "/console/api/images");
  if (!r.ok) return;
  const all = [];
  for (const [enc, list] of Object.entries(r.data.sources || {})) for (const s of list) all.push({...s, enc});
  imgState.sources = all;
  const multi = Object.keys(r.data.sources || {}).length > 1;
  const label = (s) => (multi ? s.enc + " / " : "") + s.label;
  $("images").classList.toggle("hidden", all.length === 0);
  fill("img-source", all.map((s, i) => h("option", {value: String(i)}, label(s))));
  fill("guider-strip", all.filter((s) => s.kind === "guider").map((s) => {
    const c = h("canvas", {width: 150, height: 150});
    const cap = h("span", {class: "dim mono"}, label(s));
    imgState.thumbs[s.enc + "/" + s.id] = {canvas: c, cap, etag: null};
    return h("button", {type: "button", class: "thumb", title: "open in the viewer",
      onclick: () => { $("img-source").value = String(all.indexOf(s)); setView(); }}, c, cap);
  }));
  if (all.length) setView();
  scheduleImages();
}
async function refreshThumb(s) {
  const key = s.enc + "/" + s.id, t = imgState.thumbs[key];
  if (!t || imgState.busy[key]) return;
  imgState.busy[key] = true;
  try {
    const f = await fetchFrame(s.enc, s.id, {max: 128, scale: "zscale", stretch: "linear"}, t.etag);
    if (!f || f.same) return;
    if (f.error) { t.cap.textContent = s.label + ": " + f.error; return; }
    t.etag = f.etag;
    const ctx = t.canvas.getContext("2d");
    ctx.imageSmoothingEnabled = false;
    const [w, hh] = [f.bitmap.width, f.bitmap.height], k = Math.min(150 / w, 150 / hh);
    ctx.clearRect(0, 0, 150, 150);
    ctx.drawImage(f.bitmap, 0, 0, w * k, hh * k);
    const reg = f.meta.region || [0, 0, w, hh], sx = k * w / (reg[2] - reg[0]);
    drawOverlays(ctx, f.meta, sx, sx, reg[0], reg[1]);
    t.cap.textContent = s.label + " · " + fmt(f.meta.state) + " · #" + fmt(f.meta.count);
  } catch (e) { t.cap.textContent = s.label + ": " + e; }
  finally { imgState.busy[key] = false; }
}
function scheduleImages() {
  if (imgState.timer) clearInterval(imgState.timer);
  imgState.timer = null;
  const rate = parseFloat($("img-rate").value);
  if (!(rate > 0)) { $("img-rate-note").textContent = "paused"; return; }
  imgState.timer = setInterval(() => {
    if (!session || document.visibilityState !== "visible" || $("main").classList.contains("hidden")) return;
    for (const s of imgState.sources) if (s.kind === "guider") refreshThumb(s);
    const v = imgState.view;
    if (v && (v.src.kind === "guider" || Date.now() - v.checked > 5000)) refreshView();
    const secs = (Date.now() - imgState.since) / 1000;
    if (secs > 10) {
      $("img-rate-note").textContent = "~" + (imgState.bytes / 1024 / secs).toFixed(1) + " kB/s";
      imgState.bytes = 0; imgState.since = Date.now();
    }
  }, 1000 / rate);
}
$("img-rate").addEventListener("change", scheduleImages);
function setView() {
  const s = imgState.sources[parseInt($("img-source").value, 10)];
  if (!s) return;
  imgState.view = {src: s, region: null, shape: null, meta: null, etag: null, checked: 0};
  $("img-fits").href = "/console/api/fits/" + encodeURIComponent(s.enc) + "/" + encodeURIComponent(s.id);
  refreshView();
}
for (const id of ["img-source"]) $(id).addEventListener("change", setView);
for (const id of ["img-scale", "img-stretch", "img-max"]) $(id).addEventListener("change", () => { if (imgState.view) { imgState.view.etag = null; refreshView(); } });
async function refreshView() {
  const v = imgState.view;
  if (!v || v.busy) return;
  v.busy = true;
  try {
    const params = {max: $("img-max").value, scale: $("img-scale").value, stretch: $("img-stretch").value};
    if (v.region) Object.assign(params, {x0: v.region[0], y0: v.region[1], x1: v.region[2], y1: v.region[3]});
    const f = await fetchFrame(v.src.enc, v.src.id, params, v.etag);
    v.checked = Date.now();
    if (!f || f.same) return;
    if (f.error) { $("img-info").textContent = f.error; return; }
    v.etag = f.etag; v.meta = f.meta; v.shape = f.meta.shape; v.bitmap = f.bitmap;
    drawView();
  } finally { v.busy = false; }
}
function drawView() {
  const v = imgState.view, c = $("img-canvas");
  if (!v || !v.bitmap) return;
  const m = v.meta, reg = m.region, rw = reg[2] - reg[0], rh = reg[3] - reg[1];
  const dpr = window.devicePixelRatio || 1, cssW = c.clientWidth || 512;
  c.width = Math.round(cssW * dpr);
  c.height = Math.round(Math.min(c.width * rh / rw, 1.2 * window.innerHeight * dpr));
  const ctx = c.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  const k = Math.min(c.width / rw, c.height / rh);
  ctx.fillStyle = "#000"; ctx.fillRect(0, 0, c.width, c.height);
  ctx.drawImage(v.bitmap, 0, 0, rw * k, rh * k);
  v.k = k;
  drawOverlays(ctx, m, k, k, reg[0], reg[1]);
  $("img-info").textContent = [m.file || (m.telescope ? m.telescope + " guider" : ""), m.object, m.state,
    m.exposure !== undefined ? "exp " + fmt(m.exposure) + " s" : null, m.date_obs || m.utc_end,
    m.shape ? m.shape[0] + "×" + m.shape[1] : null, "bin " + m.binning + (m.view_binning > 1 ? " (shown ÷" + m.view_binning + ")" : ""), m.scale + "/" + m.stretch].filter(Boolean).join(" · ");
}
function zoom(f, cx, cy) {
  const v = imgState.view;
  if (!v || !v.shape) return;
  const [W, H] = v.shape, reg = v.region || [0, 0, W, H];
  const rw = reg[2] - reg[0], rh = reg[3] - reg[1];
  const nw = Math.max(8, Math.min(W, Math.round(rw / f))), nh = Math.max(8, Math.min(H, Math.round(rh / f)));
  const mx = cx ?? (reg[0] + rw / 2), my = cy ?? (reg[1] + rh / 2);
  const x0 = Math.max(0, Math.min(W - nw, Math.round(mx - nw / 2))), y0 = Math.max(0, Math.min(H - nh, Math.round(my - nh / 2)));
  v.region = (nw >= W && nh >= H) ? null : [x0, y0, x0 + nw, y0 + nh];
  v.etag = null;
  refreshView();
}
$("img-zin").addEventListener("click", () => zoom(2));
$("img-zout").addEventListener("click", () => zoom(0.5));
$("img-fit").addEventListener("click", () => { if (imgState.view) { imgState.view.region = null; imgState.view.etag = null; refreshView(); } });
function framePoint(ev) {
  const v = imgState.view, c = $("img-canvas"), rect = c.getBoundingClientRect();
  if (!v || !v.meta || !v.k) return null;
  const dpr = c.width / rect.width, reg = v.meta.region;
  return [reg[0] + (ev.clientX - rect.left) * dpr / v.k, reg[1] + (ev.clientY - rect.top) * dpr / v.k];
}
let drag = null;
$("img-canvas").addEventListener("pointerdown", (ev) => { drag = {x: ev.clientX, y: ev.clientY, p: framePoint(ev)}; });
$("img-canvas").addEventListener("pointerup", async (ev) => {
  const d = drag; drag = null;
  const v = imgState.view;
  if (!d || !v || !v.shape) return;
  const moved = Math.hypot(ev.clientX - d.x, ev.clientY - d.y);
  const p = framePoint(ev);
  if (!p || !d.p) return;
  if (moved > 6) {  // drag: pan
    const [W, H] = v.shape, reg = v.region || [0, 0, W, H], rw = reg[2] - reg[0], rh = reg[3] - reg[1];
    const x0 = Math.max(0, Math.min(W - rw, Math.round(reg[0] - (p[0] - d.p[0])))), y0 = Math.max(0, Math.min(H - rh, Math.round(reg[1] - (p[1] - d.p[1]))));
    v.region = (rw >= W && rh >= H) ? null : [x0, y0, x0 + rw, y0 + rh];
    v.etag = null; refreshView(); return;
  }
  const x = Math.floor(p[0]), y = Math.floor(p[1]);
  const r = await call("GET", "/console/api/pixel/" + encodeURIComponent(v.src.enc) + "/" + encodeURIComponent(v.src.id) + "?x=" + x + "&y=" + y);
  if (r.ok) $("img-info").textContent = "x " + x + ", y " + y + (r.data.detector ? " (detector " + r.data.detector.map((q) => q.toFixed(1)).join(", ") + ")" : "") + ": " + fmt(r.data.value, 1);
});
$("img-canvas").addEventListener("wheel", (ev) => { ev.preventDefault(); const p = framePoint(ev); zoom(ev.deltaY < 0 ? 2 : 0.5, p && p[0], p && p[1]); }, {passive: false});
window.addEventListener("resize", drawView);

// -- requests --------------------------------------------------------------------------
function show(r) { $("rq-out").textContent = JSON.stringify(r.data, null, 1); }
$("rq-validate").addEventListener("click", async () => show(await call("POST", "/console/api/v1/requestgroups/validate", $("rq-json").value)));
$("rq-submit").addEventListener("click", async () => show(await call("POST", "/console/api/v1/requestgroups", $("rq-json").value)));
$("rq-too").addEventListener("click", async () => {
  if (!confirm("Submit this target of opportunity? It may interrupt an observation.")) return;
  show(await call("POST", "/console/api/v1/toos", $("rq-json").value));
});
$("rq-cancel").addEventListener("click", async () => {
  const id = $("rq-cancel-id").value.trim();
  if (!id || !confirm("Cancel request group " + id + "?")) return;
  show(await call("POST", "/console/api/v1/requestgroups/" + encodeURIComponent(id) + "/cancel", {}));
});
$("rq-list").addEventListener("click", async () => show(await call("GET", "/console/api/v1/requestgroups")));

// -- start ------------------------------------------------------------------------------
(async () => {
  const r = await call("GET", "/console/session");
  if (r.ok) signedIn(r.data); else signedOut();
})();
"""


def render_page(nonce: str) -> str:
    """The page, with ``nonce`` on its inline style and script (the CSP
    allows nothing else)."""
    n = html.escape(nonce, quote=True)
    return (
        "<!doctype html>\n"
        '<html lang="en" data-theme="dark">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="dark light">\n'
        '<meta name="referrer" content="no-referrer">\n'
        f"<title>{TITLE}</title>\n"
        f'<style nonce="{n}">{CSS}</style>\n</head>\n<body>\n'
        f"{BODY}\n"
        f'<script nonce="{n}">{JS}</script>\n</body>\n</html>\n'
    )

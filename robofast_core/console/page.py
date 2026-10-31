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
.hidden { display: none !important; }
"""

BODY = """
<header id="top">
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
  $("signin").classList.remove("hidden");
  $("who").textContent = "";
  setConn("signed out", "warn");
}
function signedIn(s) {
  session = s;
  $("signin").classList.add("hidden");
  $("main").classList.remove("hidden");
  $("signout").classList.remove("hidden");
  $("who").textContent = s.name + (s.can.operate ? " (operator)" : " (read only)");
  $("requests").classList.toggle("hidden", !s.requests);
  connect();
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
    if (!s.reachable) return h("div", {}, title(name), h("p", {class: "bad"}, "spectrograph service unreachable"));
    const j = s.job;
    const bar = h("div", {class: "bar"}, h("div", {}));
    if (j && j.exptime && j.elapsed !== null) bar.firstChild.style.width = Math.min(100, 100 * j.elapsed / j.exptime) + "%";
    return h("div", {}, title(name),
      h("div", {class: "big"}, badge(s.busy ? "busy: " + s.busy : "idle", s.busy ? "ok" : "warn")),
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

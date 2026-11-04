/* AquaSense dashboard - vanilla JS, no dependencies. */
"use strict";
const $ = id => document.getElementById(id);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hhmm = t => t.slice(11, 16);
const fmtDT = t => `${t.slice(5, 10).split("-").reverse().join("/")} ${hhmm(t)}`;
const pct = v => `${Math.round(v * 10) / 10}%`;
const dur = m => (m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60 ? (m % 60) + " min" : ""}`.trim());

const S = { view: "overview", devices: [], id: null, detail: null, readings: [], range: "today", lastOk: 0, timers: [] };

/* ---------- storage + api ---------- */
const store = {
  get: k => { try { return sessionStorage.getItem(k) ?? localStorage.getItem(k); } catch { return null; } },
  set(k, v, persist) { try { (persist ? localStorage : sessionStorage).setItem(k, v); } catch { /* storage blocked */ } },
  clear() { ["tok", "name"].forEach(k => { try { localStorage.removeItem(k); sessionStorage.removeItem(k); } catch { /* ignore */ } }); },
};

async function api(path, opt = {}) {
  const tok = store.get("tok");
  let res;
  try {
    res = await fetch(path, { ...opt, headers: { "Content-Type": "application/json", ...(tok ? { Authorization: "Bearer " + tok } : {}) } });
  } catch { throw new Error("Cannot reach the server. Check your connection."); }
  if (opt.raw && res.ok) return res;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 401 && tok && path !== "/login") { signOut("Your session expired. Sign in again."); }
    throw new Error(data.error || "Something went wrong. Try again.");
  }
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: JSON.stringify(body) });

function toast(msg, bad) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : ""); t.textContent = msg;
  $("toasts").append(t); setTimeout(() => t.remove(), 3800);
}

/* ---------- theme ---------- */
function applyTheme(t) { document.documentElement.dataset.theme = t; }
applyTheme(store.get("theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
$("themeBtn").onclick = () => {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(t); store.set("theme", t, true);
};

/* ---------- auth ---------- */
function showAuth(which) {
  $("auth").classList.remove("hide"); $("app").classList.add("hide");
  $("loginForm").classList.toggle("hide", which === "signup");
  $("signupForm").classList.toggle("hide", which !== "signup");
  $("lerr").textContent = $("serr").textContent = "";
  (which === "signup" ? $("sn") : $("le")).focus();
}
$$("[data-go]").forEach(b => b.onclick = () => showAuth(b.dataset.go));
$$("[data-eye]").forEach(b => b.onclick = () => {
  const i = $(b.dataset.eye), show = i.type === "password";
  i.type = show ? "text" : "password"; b.textContent = show ? "Hide" : "Show";
  b.setAttribute("aria-label", show ? "Hide password" : "Show password");
});

function strength(p) {
  let s = 0;
  if (p.length >= 8) s++;
  if (/[A-Za-z]/.test(p) && /\d/.test(p)) s++;
  if (p.length >= 12) s++;
  if (/[^A-Za-z0-9]/.test(p)) s++;
  return s;
}
$("sp").oninput = e => {
  const s = strength(e.target.value);
  document.querySelector(".meter").dataset.s = e.target.value ? s : 0;
  $("pwhint").textContent = ["Use 8+ characters with a letter and a number.", "Weak. Add a number and more characters.", "Fair. Longer is stronger.", "Good.", "Strong."][e.target.value ? s : 0];
};

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
async function authSubmit(e, btn, errEl, path, body) {
  e.preventDefault(); errEl.textContent = ""; btn.disabled = true;
  const label = btn.textContent; btn.textContent = "Please wait";
  try {
    const d = await post(path, body);
    const persist = path === "/login" ? $("remember").checked : false;
    store.clear(); store.set("tok", d.token, persist); store.set("name", d.name, persist);
    boot();
  } catch (x) { errEl.textContent = x.message; }
  finally { btn.disabled = false; btn.textContent = label; }
}
$("loginForm").onsubmit = e => {
  const email = $("le").value.trim(), pw = $("lp").value;
  if (!EMAIL.test(email)) { e.preventDefault(); $("lerr").textContent = "Enter a valid email address."; return; }
  if (!pw) { e.preventDefault(); $("lerr").textContent = "Enter your password."; return; }
  authSubmit(e, $("lbtn"), $("lerr"), "/login", { email, password: pw });
};
$("signupForm").onsubmit = e => {
  const name = $("sn").value.trim(), email = $("se").value.trim(), pw = $("sp").value;
  const bad = name.length < 2 ? "Enter your full name." : !EMAIL.test(email) ? "Enter a valid email address."
    : pw.length < 8 ? "Password must be at least 8 characters." : !(/[A-Za-z]/.test(pw) && /\d/.test(pw)) ? "Password must include a letter and a number."
    : pw !== $("sc").value ? "Passwords do not match." : "";
  if (bad) { e.preventDefault(); $("serr").textContent = bad; return; }
  authSubmit(e, $("sbtn"), $("serr"), "/api/signup", { name, email, password: pw });
};

function signOut(msg) {
  store.clear(); S.timers.forEach(clearInterval); S.timers = [];
  S.id = null; S.detail = null; S.devices = [];
  showAuth("login"); if (msg) $("lerr").textContent = msg;
}
$("logout").onclick = () => signOut();

/* ---------- routing ---------- */
function route() {
  const v = location.hash.slice(1);
  S.view = ["overview", "devices", "alerts", "settings"].includes(v) ? v : "overview";
  $$(".view").forEach(el => el.classList.toggle("hide", el.id !== "view-" + S.view));
  $$(".side a").forEach(a => a.classList.toggle("on", a.dataset.view === S.view));
  if (S.view === "alerts") loadAlerts();
  if (S.view === "settings") fillSettings();
}
window.addEventListener("hashchange", route);

/* ---------- data ---------- */
async function boot() {
  if (!store.get("tok")) return showAuth("login");
  try {
    const me = await api("/api/me");
    $("who").textContent = me.name; $("avatar").textContent = me.name[0].toUpperCase();
    $("accName").textContent = me.name; $("accEmail").textContent = me.email;
    $("accSince").textContent = me.member_since ? new Date(me.member_since).toLocaleDateString(undefined, { dateStyle: "long" }) : "-";
    $("auth").classList.add("hide"); $("app").classList.remove("hide");
    await refreshAll(true); route();
    S.timers.forEach(clearInterval);
    S.timers = [
      setInterval(() => { if (!document.hidden) refreshAll(); }, 10000),
      setInterval(tickLive, 1000),
      setInterval(() => post("/api/refresh", {}).then(d => store.set("tok", d.token, !!localStorage.getItem("tok"))).catch(() => {}), 600000),
    ];
  } catch { showAuth("login"); }
}

async function refreshAll(first) {
  try {
    S.devices = await api("/api/devices");
    if (!S.devices.find(d => d.id === S.id)) S.id = (S.devices[0] || {}).id;
    fillSelect(); renderDevices(); renderAlertCount();
    if (S.id) await loadDetail();
    S.lastOk = Date.now(); tickLive();
  } catch (e) {
    $("live").className = "live bad"; $("liveText").textContent = "Connection lost. Retrying";
    if (first) toast(e.message, true);
  }
}

async function loadDetail() {
  const [detail, readings] = await Promise.all([api(`/api/devices/${S.id}`), api(`/api/devices/${S.id}/readings?range=${S.range}`)]);
  S.detail = detail; S.readings = readings; renderOverview();
}

function tickLive() {
  const el = $("live"), sec = Math.round((Date.now() - S.lastOk) / 1000);
  if (!S.lastOk) return;
  el.className = "live " + (sec < 25 ? "ok" : "bad");
  $("liveText").textContent = sec < 25 ? (sec < 2 ? "Live, just updated" : `Live, updated ${sec}s ago`) : `No update for ${sec}s`;
}

function fillSelect() {
  const sel = $("deviceSelect");
  sel.innerHTML = S.devices.map(d => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("");
  sel.value = S.id;
}
$("deviceSelect").onchange = e => { S.id = e.target.value; loadDetail(); renderDevices(); fillSettings(); };

/* ---------- overview ---------- */
const levelClass = (lvl, thr) => lvl < thr ? "low" : lvl < thr + 20 ? "mid" : "ok";

function renderOverview() {
  const d = S.detail; if (!d) return;
  const cur = d.current, thr = d.low_threshold, offline = !d.online;
  const b = $("banner");
  if (!cur) { b.className = "banner off"; b.textContent = "No readings yet for this tank."; }
  else if (offline) {
    b.className = "banner off";
    b.innerHTML = `<span>Device offline. Showing the last known level.</span><small>Last seen ${esc(new Date(d.last_seen).toLocaleString())}</small>`;
  } else if (d.is_low) {
    b.className = "banner low";
    b.innerHTML = `<span>Low water alert: ${esc(d.name)} is at ${pct(cur.level_percent)}, below the ${thr}% limit.</span><small>${d.pump_state === "on" ? "Pump is running" : "Pump is off"}</small>`;
  } else {
    b.className = "banner ok";
    b.innerHTML = `<span>All good: ${esc(d.name)} is at ${pct(cur.level_percent)}, above the ${thr}% limit.</span>` +
      (d.hours_to_low ? `<small>About ${d.hours_to_low} h until the alert level at the current rate</small>` : "");
  }
  if (!cur) return;

  const y = 20 + (100 - cur.level_percent) / 100 * 260, ty = 20 + (100 - thr) / 100 * 260;
  $("water").style.transform = `translateY(${y}px)`;
  $("gauge").setAttribute("class", levelClass(cur.level_percent, thr));
  $("thrLine").setAttribute("y1", ty); $("thrLine").setAttribute("y2", ty);
  $("thrText").setAttribute("y", ty + 4); $("thrText").textContent = thr + "%";
  $("curVal").textContent = pct(cur.level_percent);
  $("curSub").textContent = `Reading from ${fmtDT(cur.timestamp)}`;
  $("curLitres").textContent = `${d.litres.toLocaleString()} of ${d.capacity_litres.toLocaleString()} litres`;

  const t = d.today;
  $("minVal").textContent = pct(t.min.level_percent); $("minSub").textContent = `at ${hhmm(t.min.timestamp)} on ${t.date}`;
  $("maxVal").textContent = pct(t.max.level_percent); $("maxSub").textContent = `at ${hhmm(t.max.timestamp)} on ${t.date}`;
  const tr = d.trend_per_hour;
  $("trendVal").textContent = tr === null ? "--" : `${tr > 0 ? "+" : ""}${tr}%/h`;
  $("trendSub").textContent = tr === null ? "Needs two readings" : tr > 0 ? "Filling" : tr < 0 ? "Draining" : "Steady";
  $("pumpVal").textContent = d.pump_state.toUpperCase();
  $("pumpSub").textContent = d.pump_mode === "auto" ? `Auto: runs below ${30}%` : `Manual override: ${d.pump_mode}`;
  $$("#pumpSeg button").forEach(btn => { btn.classList.toggle("on", btn.dataset.mode === d.pump_mode); btn.disabled = offline; });

  renderChart(); renderEvents(d); renderReadings();
}

function renderEvents(d) {
  const lows = d.episodes.filter(e => e.started.slice(0, 10) === d.today.date || e.active);
  $("evtSub").textContent = `Below ${d.low_threshold}% on ${d.today.date}`;
  $("events").innerHTML = lows.length ? lows.map(e =>
    `<li><span>${hhmm(e.lowest_at)} reached ${pct(e.lowest)}</span><span>${e.active ? "Ongoing" : "Lasted " + dur(e.duration_minutes)}</span></li>`).join("")
    : `<li class="none"><span>No low-level events</span><span></span></li>`;
}

function renderReadings() {
  const thr = S.detail.low_threshold, rows = [...S.readings].reverse().slice(0, 8);
  $("readRows").innerHTML = rows.map(r => {
    const low = r.level_percent < thr;
    return `<tr><td>${esc(fmtDT(r.timestamp))}</td><td>${pct(r.level_percent)}</td><td><span class="tag ${low ? "low" : "ok"}">${low ? "Below limit" : "Normal"}</span></td></tr>`;
  }).join("");
}

function renderChart() {
  const pts = S.readings, thr = S.detail.low_threshold, W = 720, H = 280, L = 40, R = 14, T = 12, B = 30;
  if (!pts.length) { $("chart").innerHTML = `<p class="empty">No readings in this range.</p>`; return; }
  const n = pts.length, x = i => L + (n < 2 ? (W - L - R) / 2 : i * (W - L - R) / (n - 1)), y = v => T + (100 - v) * (H - T - B) / 100;
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Tank level over time with the ${thr}% alert line"><defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--aqua)" stop-opacity=".28"/><stop offset="1" stop-color="var(--aqua)" stop-opacity="0"/></linearGradient></defs>`;
  [0, 25, 50, 75, 100].forEach(v => s += `<line class="grid-l" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="ax" x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${v}%</text>`);
  s += `<rect class="lowband" x="${L}" y="${y(thr)}" width="${W - L - R}" height="${y(0) - y(thr)}"/>`;
  const line = pts.map((p, i) => `${x(i)},${y(p.level_percent)}`).join(" ");
  s += `<polygon class="area" points="${x(0)},${y(0)} ${line} ${x(n - 1)},${y(0)}"/><polyline class="line" points="${line}"/>`;
  s += `<line class="thr-l" x1="${L}" x2="${W - R}" y1="${y(thr)}" y2="${y(thr)}"/><text class="ax" x="${W - R}" y="${y(thr) - 5}" text-anchor="end" fill="var(--alert)">Alert limit ${thr}%</text>`;
  const step = Math.max(1, Math.ceil(n / 8));
  pts.forEach((p, i) => {
    const low = p.level_percent < thr;
    if (n <= 40 || low) s += `<circle class="dot${low ? " low" : ""}" cx="${x(i)}" cy="${y(p.level_percent)}" r="${low ? 5 : 3.5}"/>`;
    if (i % step === 0) s += `<text class="ax" x="${x(i)}" y="${H - 8}" text-anchor="middle">${S.range === "today" ? hhmm(p.timestamp) : fmtDT(p.timestamp).slice(0, 5)}</text>`;
  });
  s += `<line id="cross" class="cross hide" y1="${T}" y2="${H - B}"/><rect id="hit" x="${L}" y="${T}" width="${W - L - R}" height="${H - T - B}" fill="transparent"/></svg>`;
  $("chart").innerHTML = s;
  const hit = $("hit"), tip = $("tip"), cross = $("cross");
  const near = ev => {
    const r = $("chart").firstChild.getBoundingClientRect(), px = (ev.clientX - r.left) / r.width * W;
    return Math.max(0, Math.min(n - 1, Math.round((px - L) / ((W - L - R) / Math.max(n - 1, 1)))));
  };
  hit.onmousemove = ev => {
    const i = near(ev), p = pts[i], r = $("chart").firstChild.getBoundingClientRect(), k = r.width / W;
    cross.classList.remove("hide"); cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
    tip.classList.remove("hide"); tip.textContent = `${fmtDT(p.timestamp)}  ${pct(p.level_percent)}`;
    tip.style.left = x(i) * k + "px"; tip.style.top = y(p.level_percent) * k + "px";
  };
  hit.onmouseleave = () => { tip.classList.add("hide"); cross.classList.add("hide"); };
}

/* controls on overview */
$$("#rangeSeg button").forEach(b => b.onclick = () => {
  S.range = b.dataset.range; $$("#rangeSeg button").forEach(x => x.classList.toggle("on", x === b)); loadDetail();
});
$$("#pumpSeg button").forEach(b => b.onclick = async () => {
  try { await post(`/api/devices/${S.id}/pump`, { mode: b.dataset.mode }); toast(`Pump set to ${b.dataset.mode}`); await refreshAll(); }
  catch (e) { toast(e.message, true); }
});
$("simLevel").oninput = e => $("simOut").textContent = e.target.value + "%";
async function addReading(level) {
  try { await post(`/api/devices/${S.id}/readings`, { level_percent: level }); toast(`Reading added: ${level}%`); await refreshAll(); }
  catch (e) { toast(e.message, true); }
}
$("simAdd").onclick = () => addReading(Number($("simLevel").value));
$$("[data-preset]").forEach(b => b.onclick = () => { $("simLevel").value = b.dataset.preset; $("simOut").textContent = b.dataset.preset + "%"; addReading(Number(b.dataset.preset)); });
$("exportBtn").onclick = async () => {
  try {
    const res = await api(`/api/devices/${S.id}/export.csv`, { raw: true }), url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: `${S.id}-readings.csv` });
    document.body.append(a); a.click(); a.remove(); URL.revokeObjectURL(url);
  } catch (e) { toast(e.message, true); }
};

/* ---------- tanks ---------- */
function renderDevices() {
  $("devGrid").innerHTML = S.devices.map(d => {
    const c = d.current, sp = d.spark || [], pts = sp.map((v, i) => `${sp.length < 2 ? 50 : i * 100 / (sp.length - 1)},${38 - v * 0.36}`).join(" ");
    const tag = !d.online ? `<span class="tag off">Offline</span>` : d.is_low ? `<span class="tag low">Low</span>` : `<span class="tag ok">Normal</span>`;
    return `<button class="card dev${d.id === S.id ? " sel" : ""}${d.is_low ? " isLow" : ""}" data-id="${esc(d.id)}">
      <div class="dev-top"><div><h3>${esc(d.name)}</h3><span class="muted small">${esc(d.location)}</span></div>${tag}</div>
      <div class="lvl">${c ? pct(c.level_percent) : "--"}</div>
      <div class="muted small">${d.litres !== undefined ? d.litres.toLocaleString() + " L of " + d.capacity_litres.toLocaleString() + " L" : ""}</div>
      <svg viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true"><polyline class="spark" points="${pts}" vector-effect="non-scaling-stroke"/></svg></button>`;
  }).join("");
  $$("#devGrid .dev").forEach(b => b.onclick = () => { S.id = b.dataset.id; location.hash = "overview"; $("deviceSelect").value = S.id; loadDetail(); renderDevices(); });
}

/* ---------- alerts ---------- */
let alertFilter = "all", alertData = [];
async function renderAlertCount() {
  try {
    alertData = await api("/api/alerts");
    const n = alertData.filter(a => a.active).length, el = $("alertCount");
    el.textContent = n; el.classList.toggle("hide", !n);
  } catch { /* shown by refresh */ }
}
async function loadAlerts() { await renderAlertCount(); drawAlerts(); }
function drawAlerts() {
  const rows = alertData.filter(a => alertFilter === "all" || (alertFilter === "active") === a.active);
  $("alertRows").innerHTML = rows.length ? rows.map(a => `<tr><td>${esc(a.device_name)}</td><td>${esc(fmtDT(a.started))}</td>
    <td>${pct(a.lowest)} <span class="muted small">at ${hhmm(a.lowest_at)}</span></td><td>${a.active ? "Ongoing" : dur(a.duration_minutes)}</td>
    <td><span class="tag ${a.active ? "low" : "ok"}">${a.active ? "Active" : "Resolved"}</span></td></tr>`).join("")
    : `<tr><td colspan="5" class="empty">No alerts to show.</td></tr>`;
}
$$("#alertFilter button").forEach(b => b.onclick = () => { alertFilter = b.dataset.f; $$("#alertFilter button").forEach(x => x.classList.toggle("on", x === b)); drawAlerts(); });

/* ---------- settings ---------- */
function fillSettings() {
  const d = S.devices.find(x => x.id === S.id); if (!d) return;
  $("setName").value = d.name; $("setThr").value = d.low_threshold;
}
$("devForm").onsubmit = async e => {
  e.preventDefault(); $("devErr").textContent = "";
  try { await post(`/api/devices/${S.id}/settings`, { name: $("setName").value, low_threshold: Number($("setThr").value) }); toast("Tank settings saved"); await refreshAll(); }
  catch (x) { $("devErr").textContent = x.message; }
};
$("pwForm").onsubmit = async e => {
  e.preventDefault(); $("pwErr").textContent = "";
  try { await post("/api/me/password", { current_password: $("pwCur").value, new_password: $("pwNew").value }); toast("Password updated"); e.target.reset(); }
  catch (x) { $("pwErr").textContent = x.message; }
};

boot();

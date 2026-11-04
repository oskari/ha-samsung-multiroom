"use strict";
// Samsung Speakers UI. All paths are relative so it works behind HA ingress.

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let S = null;            // latest /api/status
const rendered = {};     // section -> last rendered JSON, to avoid needless redraws

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch("api/" + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

async function job(id, onLog) {
  for (;;) {
    const j = await api("jobs/" + id);
    if (onLog) onLog(j.log);
    if (j.state === "error") throw new Error(j.log[j.log.length - 1] || "Failed");
    if (j.state === "done") return j;
    await sleep(700);
  }
}

function toast(msg, ms = 4000) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), ms);
}

async function guarded(btn, fn) {
  if (btn) btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message, 7000); } finally { if (btn) btn.disabled = false; }
  refresh();
}

// ---------------------------------------------------------------- dialog
function dialog(title, html, okLabel = "OK") {
  const d = $("#dialog");
  $("#dialog-title").textContent = title;
  $("#dialog-body").innerHTML = html;
  $("#dialog-ok").textContent = okLabel;
  d.showModal();
  return new Promise((resolve) => {
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
  });
}

// ---------------------------------------------------------------- helpers
const bars = (rssi) => {
  const n = Math.max(0, Math.min(5, parseInt(rssi, 10) || 0));
  return `<span class="bars" title="signal ${n}/5">${"▮".repeat(n)}${"▯".repeat(5 - n)}</span>`;
};
const speakerByIp = (ip) => (S?.speakers || []).find((s) => s.ip === ip);
const speakerByMac = (mac) => (S?.speakers || []).find((s) => s.mac === mac);
const bridgeForMac = (mac) => (S?.bridges || []).find((b) => b.speaker_mac === mac);

function volumeOptions(selected) {
  let html = `<option value="">Keep the speaker's own volume</option>`;
  for (let v = 1; v <= 30; v++) html += `<option value="${v}" ${String(selected) === String(v) ? "selected" : ""}>${v} / 30</option>`;
  return html;
}

function setOptions(select, options, keep = true) {
  const prev = select.value;
  select.innerHTML = options.map(([v, label]) => `<option value="${esc(v)}">${esc(label)}</option>`).join("");
  if (keep && options.some(([v]) => v === prev)) select.value = prev;
}

function changed(key, data) {
  const json = JSON.stringify(data);
  if (rendered[key] === json) return false;
  rendered[key] = json;
  return true;
}

// ---------------------------------------------------------------- render: speakers
function renderSpeakers() {
  const el = $("#speaker-list");
  if (el.contains(document.activeElement) && document.activeElement.type === "range") return;
  if (!changed("speakers", [S.speakers, S.bridges.map((b) => b.speaker_mac), S.scanning])) return;
  if (!S.speakers.length) {
    el.innerHTML = `<div class="card empty">${S.scanning ? "Searching…" :
      "No speakers found yet. If they're on another network, add it below."}</div>`;
    return;
  }
  el.innerHTML = S.speakers.map((s) => {
    const role = s.role === "main" ? `<span class="badge info">Leads "${esc(s.group)}"</span>`
      : s.role === "sub" ? `<span class="badge">In ${esc(speakerByIp(s.main_ip)?.name || s.main_ip)}'s group</span>` : "";
    const br = bridgeForMac(s.mac);
    return `<div class="card" data-ip="${esc(s.ip)}">
      <div class="card-head"><div><h2>${esc(s.name)}</h2>
        <span class="muted small">${esc(s.model)}</span></div>${role}</div>
      <dl class="kv">
        <dt>Address</dt><dd>${esc(s.ip)}</dd>
        <dt>Wi-Fi</dt><dd>${s.connection === "wireless" ? `${esc(s.ssid)} ${bars(s.rssi)} <span class="muted small">ch ${esc(s.channel)}</span>` : esc(s.connection || "—")}</dd>
        <dt>Spotify</dt><dd>${br ? esc(br.name) : '<span class="muted">no bridge</span>'}</dd>
      </dl>
      <div class="vol"><span class="muted">Volume</span>
        <input type="range" min="0" max="30" value="${s.volume ?? 0}" data-vol="${esc(s.ip)}">
        <span>${s.volume ?? "?"}</span></div>
      <div class="actions">
        <button class="secondary" data-wifi="${esc(s.ip)}">Change Wi-Fi</button>
        ${br || s.role === "sub" ? "" : `<button class="secondary" data-spotify="${esc(s.ip)}">Add to Spotify</button>`}
      </div></div>`;
  }).join("");
}

function renderSubnets() {
  if (!changed("subnets", [S.subnets, S.local_subnets])) return;
  $("#subnet-list").innerHTML =
    S.local_subnets.map((n) => `<span class="chip">${esc(n)} <span class="muted">(this host)</span></span>`).join("") +
    S.subnets.map((n) => `<span class="chip">${esc(n)}<button data-rmnet="${esc(n)}" title="remove">✕</button></span>`).join("");
}

// ---------------------------------------------------------------- render: groups
function renderGroups() {
  if (!changed("groups", [S.groups, S.speakers.map((s) => [s.ip, s.name, s.role])])) return;
  $("#group-list").innerHTML = S.groups.length ? S.groups.map((g) => `<div class="card">
      <div class="card-head"><h2>${esc(g.name)}</h2>
        <button class="danger" data-ungroup="${esc(g.main.ip)}">Ungroup all</button></div>
      <dl class="kv"><dt>Main</dt><dd>${esc(g.main.name)} <span class="muted small">${esc(g.main.ip)}</span></dd>
      ${g.members.map((m) => `<dt>Member</dt><dd>${esc(m.name)}
        <button class="secondary small" data-ungroup="${esc(m.ip)}">Remove</button></dd>`).join("")}
      </dl></div>`).join("")
    : `<div class="card empty">No groups.</div>`;
  const free = S.speakers.filter((s) => s.role !== "sub");
  setOptions($("#group-main"), free.map((s) => [s.ip, `${s.name} (${s.ip})`]));
  renderGroupMembers();
}

function renderGroupMembers() {
  const main = $("#group-main").value;
  const checked = new Set($$("#group-members input:checked").map((i) => i.value));
  const g = S.groups.find((x) => x.main.ip === main);
  if (g) g.members.forEach((m) => checked.add(m.ip));
  const options = S.speakers.filter((s) => s.ip !== main && (!s.role || s.main_ip === main));
  $("#group-members").innerHTML = options.length ? options.map((s) => `<label>
      <input type="checkbox" value="${esc(s.ip)}" ${checked.has(s.ip) ? "checked" : ""}>${esc(s.name)}
      <span class="muted small">&nbsp;${esc(s.ip)}</span></label>`).join("")
    : `<p class="muted">No other ungrouped speakers.</p>`;
  if (g && !$("#group-name").value) $("#group-name").value = g.name;
}

// ---------------------------------------------------------------- render: spotify
function renderSpotify() {
  const sp = S.spotify;
  if (changed("spotify", sp)) {
    const so = sp.soloist || {};
    let html = `<div class="card-head"><h2>Spotify Connect (Spotify Soloist)</h2>
      ${sp.ready ? '<span class="badge ok">Ready</span>' : '<span class="badge warn">Setup needed</span>'}</div>`;
    if (!sp.accepted) {
      html += `<div class="notice bad">Spotify Soloist is downloaded from Spotify and used under
        <a href="https://www.spotify.com/legal/end-user-agreement/" target="_blank" rel="noopener">Spotify's terms</a>.
        Turn on <b>Accept Spotify's terms</b> in this app's <b>Configuration</b> tab, then restart the app.</div>`;
    } else if (!sp.has_key) {
      html += `<div class="notice bad">Create a Soloist API key (needs Spotify Premium) at
        <a href="https://developer.spotify.com/dashboard/soloist" target="_blank" rel="noopener">developer.spotify.com/dashboard/soloist</a>
        and paste it into this app's <b>Configuration</b> tab, then restart the app.</div>`;
    } else if (sp.reason) {
      html += `<div class="notice bad">${esc(sp.reason)}</div>`;
    }
    if (so.present) {
      const soon = so.days_left !== undefined && so.days_left < 14;
      html += `<dl class="kv"><dt>Soloist</dt><dd>${esc(so.version || "?")}</dd>
        <dt>Build expires</dt><dd>${esc(so.expires || "?")} ${so.days_left !== undefined ?
        `<span class="badge ${soon ? "warn" : ""}">${so.days_left} days</span>` : ""}
        <span class="muted small">(renewed automatically)</span></dd></dl>`;
    }
    if (sp.error) html += `<div class="notice bad">${esc(sp.error)}</div>`;
    if (sp.accepted) html += `<div class="actions"><button class="secondary" id="soloist-update">
      ${so.present ? "Download the latest Soloist now" : "Download Soloist"}</button></div>`;
    $("#spotify-status").innerHTML = html;
  }

  const bl = $("#bridge-list");
  if (!bl.contains(document.activeElement) && changed("bridges", S.bridges)) {
    bl.innerHTML = S.bridges.map((b) => {
      const spk = speakerByMac(b.speaker_mac) || speakerByIp(b.speaker_ip);
      let badge, help = "";
      if (b.state === "error") badge = '<span class="badge bad">Error</span>';
      else if (b.state === "pairing") {
        badge = '<span class="badge warn">Pairing</span>';
        help = `In Spotify (same network), open <b>Devices</b> and pick <b>${esc(b.name)}</b>.`;
      } else if (b.state === "stopped") badge = '<span class="badge">Stopped</span>';
      else if (!b.logged_in) {
        badge = '<span class="badge warn">Not paired</span>';
        help = `Open Spotify on a device on the same network, tap <b>Devices</b> and pick <b>${esc(b.name)}</b>
          to link it to your account. It then stays signed in.`;
      } else if (b.status === "playing") badge = '<span class="badge ok">Playing</span>';
      else badge = `<span class="badge info">${b.active ? esc(b.status) : "Ready"}</span>`;
      return `<div class="card">
        <div class="card-head"><div><h2>${esc(b.name)}</h2>
          <span class="muted small">on ${esc(spk ? spk.name : b.speaker_ip)}${spk && spk.role === "main" ? " (whole group)" : ""}</span></div>${badge}</div>
        ${b.error ? `<div class="notice bad">${esc(b.error)}</div>` : ""}
        ${help ? `<div class="notice">${help}</div>` : ""}
        <dl class="kv">
          ${b.track ? `<dt>Track</dt><dd>${esc(b.track)}</dd>` : ""}
          ${b.spotify_volume != null ? `<dt>Spotify volume</dt><dd>${esc(b.spotify_volume)}%</dd>` : ""}
          <dt>Stream</dt><dd>${b.streaming ? `connected <span class="muted small">${b.rate ? b.rate.toFixed(2) + "×" : ""}</span>` : '<span class="muted">idle</span>'}</dd>
        </dl>
        <label>Speaker volume while playing
          <select data-bvol="${esc(b.id)}">${volumeOptions(b.speaker_volume)}</select></label>
        <div class="actions">
          <button class="secondary" data-bpair="${esc(b.id)}">Pair again</button>
          <button class="secondary" data-brestart="${esc(b.id)}">Restart</button>
          <button class="danger" data-bremove="${esc(b.id)}">Remove</button>
        </div>
        <details><summary class="muted small">Log</summary><pre class="log">${esc((b.log || []).join("\n"))}</pre></details>
      </div>`;
    }).join("");
  }

  const free = S.speakers.filter((s) => s.role !== "sub" && !bridgeForMac(s.mac));
  setOptions($("#bridge-speaker"), free.length
    ? free.map((s) => [s.ip, `${s.name} (${s.ip})${s.role === "main" ? " – whole group" : ""}`])
    : [["", S.speakers.length ? "Every speaker already has a Spotify bridge" : "No speakers found yet"]]);
  $("#bridge-add").disabled = !free.length;
  if (!$("#bridge-volume").options.length) $("#bridge-volume").innerHTML = volumeOptions("");
}

// ---------------------------------------------------------------- refresh
async function refresh() {
  try {
    S = await api("status");
  } catch (e) {
    $("#scan-info").textContent = "Can't reach the app: " + e.message;
    return;
  }
  $("#scan-info").textContent = S.scanning ? "Searching…" :
    S.last_scan ? `Last search ${new Date(S.last_scan * 1000).toLocaleTimeString()}` : "";
  $("#scan-btn").disabled = S.scanning;
  renderSpeakers();
  renderSubnets();
  renderGroups();
  renderSpotify();
}

// ---------------------------------------------------------------- actions
async function scan(btn) {
  await guarded(btn, async () => {
    const { job: id } = await api("scan", {});
    refresh();
    await job(id);
  });
}

async function changeWifi(ip) {
  const s = speakerByIp(ip);
  const body = `<p class="muted">Loading the networks ${esc(s?.name || ip)} can see…</p>`;
  const opened = dialog(`Wi-Fi for ${s?.name || ip}`, body, "Connect");
  let nets = [];
  try {
    const { job: id } = await api("speaker/networks", { ip });
    nets = (await job(id)).result || [];
  } catch (e) { /* fall back to typing the name */ }
  $("#dialog-body").innerHTML = `
    ${nets.length ? `<div class="checks">${nets.map((n, i) => `<label><input type="radio" name="net" value="${i}" ${i === 0 ? "checked" : ""}>
      ${esc(n.ssid)}&nbsp;${n.rssi !== undefined ? bars(n.rssi) : ""}
      <span class="muted small">&nbsp;${n.lockstatus === "lock" ? "🔒" : "open"}</span></label>`).join("")}
      <label><input type="radio" name="net" value="other">Other network</label></div>` : ""}
    <label>Network name <input id="w-ssid" placeholder="exact name, case-sensitive" ${nets.length ? "disabled" : ""}></label>
    <label>Password <input id="w-pw" type="password" autocomplete="off"></label>
    <p class="muted small">The speaker leaves its current network right away. Use the 2.4 GHz network if
      your router has separate names.</p>`;
  $$('#dialog-body input[name="net"]').forEach((r) => r.addEventListener("change",
    () => ($("#w-ssid").disabled = r.value !== "other" || !r.checked)));
  if (!(await opened)) return;
  const pick = $('#dialog-body input[name="net"]:checked');
  const req = { ip, password: $("#w-pw").value };
  if (pick && pick.value !== "other") req.index = parseInt(nets[parseInt(pick.value, 10)].index, 10);
  else req.ssid = $("#w-ssid").value.trim();
  if (req.index === undefined && !req.ssid) return toast("No network chosen.");
  await guarded(null, async () => {
    const { job: id } = await api("speaker/wifi", req);
    const j = await job(id);
    toast(j.log[j.log.length - 1] || "Sent.", 8000);
  });
}

async function hotspotStatus() {
  const h = await api("hotspot").catch((e) => ({ usable: false, reason: e.message }));
  const el = $("#hotspot-status");
  el.className = "notice" + (h.usable ? "" : " bad");
  el.innerHTML = h.usable ? `This Home Assistant host's Wi-Fi (${esc(h.interface)}) will briefly join the speaker's
    setup Wi-Fi and is switched off again afterwards.` : esc(h.reason);
  $("#hotspot-ui").hidden = !h.usable;
}

// ---------------------------------------------------------------- wiring
document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  const d = t.dataset;
  if (t.closest("#tabs")) {
    $$("#tabs button").forEach((b) => b.classList.toggle("active", b === t));
    $$(".tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + d.tab));
    if (d.tab === "setup") hotspotStatus();
    return;
  }
  if (t.id === "scan-btn") return scan(t);
  if (d.wifi) return changeWifi(d.wifi);
  if (d.spotify) {
    $('#tabs button[data-tab="spotify"]').click();
    $("#bridge-speaker").value = d.spotify;
    $("#bridge-name").value = speakerByIp(d.spotify)?.name || "";
    return $("#bridge-name").focus();
  }
  if (t.id === "subnet-add") return guarded(t, async () => {
    const { job: id } = await api("subnets/add", { subnet: $("#subnet-input").value });
    $("#subnet-input").value = "";
    refresh();
    await job(id);
  });
  if (d.rmnet) return guarded(t, () => api("subnets/remove", { subnet: d.rmnet }));
  if (t.id === "ip-add") return guarded(t, async () => {
    const { job: id } = await api("speakers/add", { ip: $("#ip-input").value.trim() });
    await job(id);
    $("#ip-input").value = "";
  });
  if (d.ungroup) return guarded(t, async () => {
    const { job: id } = await api("groups/ungroup", { ip: d.ungroup });
    const j = await job(id);
    toast(j.log.join(" · "), 8000);
  });
  if (t.id === "group-create") return guarded(t, async () => {
    const members = $$("#group-members input:checked").map((i) => i.value);
    const { job: id } = await api("groups/create", { main: $("#group-main").value, members, name: $("#group-name").value });
    const j = await job(id);
    toast(j.log.join(" · "), 8000);
  });
  if (t.id === "bridge-add") return guarded(t, async () => {
    await api("bridges", { speaker_ip: $("#bridge-speaker").value, name: $("#bridge-name").value,
      speaker_volume: $("#bridge-volume").value });
    $("#bridge-name").value = "";
    toast("Added. Now pick it in Spotify's Devices list once to link it to your account.", 8000);
  });
  if (d.bpair) return guarded(t, () => api(`bridges/${d.bpair}/pair`, {}));
  if (d.brestart) return guarded(t, () => api(`bridges/${d.brestart}/restart`, {}));
  if (d.bremove) {
    if (!(await dialog("Remove this Spotify bridge?", "<p>It disappears from Spotify. You can add it again later.</p>", "Remove"))) return;
    return guarded(t, () => api(`bridges/${d.bremove}/remove`, {}));
  }
  if (t.id === "soloist-update") return guarded(t, async () => {
    const { job: id } = await api("soloist/update", {});
    const j = await job(id);
    toast(j.log[j.log.length - 1] || "Updated.");
  });
  if (t.id === "hotspot-find") return guarded(t, async () => {
    $("#hotspot-list").innerHTML = '<p class="muted">Scanning (up to a minute)…</p>';
    const { job: id } = await api("hotspot/find", {});
    const aps = (await job(id)).result || [];
    $("#hotspot-list").innerHTML = aps.length ? aps.map((a, i) => `<label><input type="radio" name="hs" value="${esc(a.ssid)}" ${i === 0 ? "checked" : ""}>
      ${esc(a.ssid)} <span class="muted small">&nbsp;signal ${esc(a.signal)}%</span></label>`).join("")
      : '<p class="muted">No speaker in setup mode found. Hold its button for 5 seconds and try again.</p>';
    $("#hotspot-form").hidden = !aps.length;
  });
  if (t.id === "hotspot-go") return guarded(t, async () => {
    const hotspot = $('input[name="hs"]:checked')?.value;
    const ssid = $("#hotspot-ssid").value.trim();
    if (!hotspot || !ssid) throw new Error("Pick the speaker and type your Wi-Fi name.");
    const st = $("#hotspot-status");
    st.className = "notice";
    const { job: id } = await api("hotspot/setup", { hotspot, ssid, password: $("#hotspot-password").value });
    await job(id, (log) => (st.innerHTML = log.map(esc).join("<br>")));
    st.innerHTML += "<br><b>Done.</b> The speaker should appear under Speakers within a minute or two.";
    setTimeout(() => scan(null), 30000);
  });
});

document.addEventListener("change", (ev) => {
  const t = ev.target;
  if (t.dataset.vol) {
    t.nextElementSibling.textContent = t.value;
    guarded(null, () => api("speaker/volume", { ip: t.dataset.vol, volume: parseInt(t.value, 10) }));
  } else if (t.dataset.bvol) {
    guarded(null, () => api(`bridges/${t.dataset.bvol}/update`, { speaker_volume: t.value }));
  } else if (t.id === "group-main") {
    $("#group-name").value = "";
    renderGroupMembers();
  }
});
document.addEventListener("input", (ev) => {
  if (ev.target.dataset.vol) ev.target.nextElementSibling.textContent = ev.target.value;
});

refresh();
setInterval(refresh, 3000);

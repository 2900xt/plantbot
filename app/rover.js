// Rover tab: live camera, drive pad, Scan and E-stop for the plantbot rover.
// Talks straight to the rover's own server (plantbot on the Pi): WebSocket /ws for commands and
// telemetry, /stream.mjpg for video. Scan photos reach LeafLink through the rover's uplink, so
// they show up under Images like any other capture.
import './rover.css';

const $ = id => document.getElementById(id);
const panel = $('rover');
const KEY = 'leaflink-rover-url';
const DRIVE_MS = 100; // the rover's watchdog stops it after 500 ms without a drive command

let base = '';
let ws = null;
let retry = null;
let t = null; // latest telemetry
const held = new Set();
let driveTimer = null;
let wasDriving = false;

try { $('rover-url').value = localStorage.getItem(KEY) || $('rover-url').value; } catch {}

function visible() { return !panel.hidden && document.visibilityState === 'visible'; }

function setStatus(text, kind) {
  const chip = $('rover-status');
  chip.textContent = text;
  chip.dataset.kind = kind;
}

function connect() {
  let url;
  try { url = new URL($('rover-url').value); } catch { setStatus('Bad address', 'bad'); return; }
  base = url.origin;
  try { localStorage.setItem(KEY, base); } catch {}
  clearTimeout(retry); retry = null;
  if (ws) { ws.onclose = null; ws.close(); }
  if (location.protocol === 'https:' && url.protocol === 'http:') {
    setStatus('Blocked: open LeafLink over http to reach the rover', 'bad');
    return;
  }
  setStatus('Connecting…', 'wait');
  ws = new WebSocket(base.replace(/^http/, 'ws') + '/ws');
  ws.onopen = () => { setStatus('Connected', 'ok'); syncStream(); };
  ws.onmessage = e => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.telemetry) { t = msg.telemetry; render(); }
    if (msg.scan_progress) $('rover-state').textContent = `Analyzing… ${msg.scan_progress.label}`;
    if (msg.scan) renderScan(msg.scan);
  };
  ws.onclose = () => {
    ws = null; t = null;
    setStatus('Offline, retrying', 'bad');
    syncStream();
    retry = setTimeout(() => { retry = null; connect(); }, 2000);
  };
}

function send(obj) { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); }

// ---------- video: only stream while the tab is on screen ----------
function syncStream() {
  const img = $('rover-stream');
  const want = visible() && ws && ws.readyState === WebSocket.OPEN;
  if (want && !img.getAttribute('src')) img.src = base + '/stream.mjpg';
  if (!want && img.getAttribute('src')) { img.removeAttribute('src'); }
  img.hidden = !want;
  $('rover-video-note').hidden = !!want;
  $('rover-hud').hidden = !want;
}
$('rover-stream').onerror = () => { $('rover-stream').removeAttribute('src'); setTimeout(syncStream, 1500); };

// ---------- drive ----------
const KEYS = { w: 'f', arrowup: 'f', s: 'b', arrowdown: 'b', a: 'l', arrowleft: 'l', d: 'r', arrowright: 'r' };

function command() {
  const v = Number($('rover-speed').value);
  const f = (held.has('f') ? 1 : 0) - (held.has('b') ? 1 : 0);
  const turn = (held.has('r') ? 1 : 0) - (held.has('l') ? 1 : 0);
  if (!f && !turn) return [0, 0];
  if (!f) return [turn * v * 0.8, -turn * v * 0.8]; // spin in place
  return [v * f * (turn > 0 ? 1 : 1 - 0.5 * Math.abs(turn)), v * f * (turn < 0 ? 1 : 1 - 0.5 * Math.abs(turn))];
}

function tick() {
  if (!visible()) held.clear();
  const [l, r] = command();
  if (l || r) { send({ drive: [l, r] }); wasDriving = true; }
  else if (wasDriving) { send({ drive: [0, 0] }); wasDriving = false; }
  if (!held.size && !wasDriving) { clearInterval(driveTimer); driveTimer = null; }
}

function press(dir) {
  if (dir === 's') { held.clear(); send({ drive: [0, 0] }); return; }
  held.add(dir);
  if (!driveTimer) { tick(); driveTimer = setInterval(tick, DRIVE_MS); }
}
function release(dir) { held.delete(dir); }

for (const b of document.querySelectorAll('[data-drive]')) {
  const dir = b.dataset.drive;
  b.addEventListener('pointerdown', e => { e.preventDefault(); b.setPointerCapture(e.pointerId); press(dir); });
  for (const ev of ['pointerup', 'pointercancel', 'lostpointercapture']) b.addEventListener(ev, () => release(dir));
}

function typing(e) { return /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && e.target.type !== 'range'; }
window.addEventListener('keydown', e => {
  if (!visible() || typing(e) || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (KEYS[k]) { e.preventDefault(); if (!e.repeat) press(KEYS[k]); }
  else if (k === ' ') { e.preventDefault(); if (!e.repeat) send({ cmd: 'scan' }); }
});
window.addEventListener('keyup', e => { const k = KEYS[e.key.toLowerCase()]; if (k) release(k); });
window.addEventListener('blur', () => held.clear());
document.addEventListener('visibilitychange', () => { held.clear(); syncStream(); });
new MutationObserver(() => { if (panel.hidden) held.clear(); else if (!ws && !retry) connect(); syncStream(); }).observe(panel, { attributes: true, attributeFilter: ['hidden'] });

$('rover-speed').oninput = () => { $('rover-speed-out').textContent = Math.round($('rover-speed').value * 100) + '%'; };

// ---------- actions ----------
$('rover-form').onsubmit = e => { e.preventDefault(); connect(); };
$('rover-scan').onclick = () => send({ cmd: 'scan' });
$('rover-auto').onclick = () => send({ cmd: t && t.mode === 'auto' ? 'auto_off' : 'auto_on' });
$('rover-estop').onclick = () => { held.clear(); send({ cmd: 'estop' }); };
$('rover-reset').onclick = () => send({ cmd: 'reset' });

// ---------- render ----------
const fmt = (x, d = 2) => (x == null || Number.isNaN(x) ? '—' : Number(x).toFixed(d));
const pct = r => (r == null ? '—' : `${r >= 1 ? '+' : ''}${Math.round((r - 1) * 100)}%`);

function render() {
  if (!t) return;
  const busy = !['IDLE', 'WARMUP'].includes(t.state);
  $('rover-state').textContent = t.state === 'ESTOP' ? `E-STOP${t.error ? ': ' + t.error : ''}` : `${t.state}${t.mode === 'auto' ? ' · auto' : ''}`;
  $('rover-state').dataset.state = t.state;
  $('rover-warm').textContent = t.warm ? '' : `sensors warming up, ${Math.ceil(t.warmup_remaining_s)} s`;
  $('rover-scan').disabled = busy && t.mode !== 'auto';
  $('rover-auto').textContent = t.mode === 'auto' ? 'Stop auto' : 'Auto row';
  $('rover-reset').hidden = t.state !== 'ESTOP';
  const plantMq2 = t.plant_sensor !== 'bme680';
  $('rv-plant').textContent = pct(plantMq2 ? t.ratios.mq2 : t.ratios.gas);
  $('rv-bg').textContent = pct(plantMq2 ? t.ratios.gas : t.ratios.mq2);
  $('rv-diff').textContent = t.ratios.diff == null ? '—' : `${pct(t.ratios.diff)}${t.diff_calibrated ? '' : ' (uncalibrated)'}`;
  $('rv-range').textContent = t.range_cm == null ? 'no echo' : `${fmt(t.range_cm, 0)} cm`;
  const ll = t.leaflink;
  $('rv-link').textContent = !ll || !ll.enabled ? 'off (set LEAFLINK_URL on the rover)' : ll.ok ? `sending as ${ll.device}` : ll.ok === false ? `failing: ${ll.error}` : 'starting';
  $('rv-link').dataset.kind = !ll || !ll.enabled ? 'off' : ll.ok ? 'ok' : 'bad';
  $('rover-devices').replaceChildren(...Object.entries(t.devices || {}).map(([k, v]) => {
    const s = document.createElement('span');
    s.textContent = `${k}: ${v}`;
    s.dataset.kind = ['ok', 'dummy', 'mock', 'disabled'].includes(v) ? (v === 'ok' ? 'ok' : 'off') : 'bad';
    return s;
  }));
  $('rover-hud').textContent = `L ${fmt(t.motors[0])}  R ${fmt(t.motors[1])}${t.watchdog_trips ? `  · watchdog ${t.watchdog_trips}` : ''}`;
}

function renderScan(s) {
  const box = $('rover-last');
  const img = document.createElement('img');
  img.src = base + (s.images ? s.images.crop : `/scans/${s.id}/crop.jpg`);
  img.alt = 'Isolated plant from the last scan';
  const dl = document.createElement('dl');
  dl.className = 'rover-readings';
  const rows = [
    ['Rover score', `${Math.round(s.health)} · ${s.status}`],
    ['On-board guess', `${s.label} (${Math.round(s.p_healthy * 100)}% healthy)`],
    ['VOC', s.explain],
    ['Leaf model', t && t.leaflink && t.leaflink.enabled ? 'sent to LeafLink, see Images' : 'not sent: rover uplink is off'],
  ];
  for (const [k, v] of rows) { const dt = document.createElement('dt'); dt.textContent = k; const dd = document.createElement('dd'); dd.textContent = v; dl.append(dt, dd); }
  box.replaceChildren(img, dl);
}

if (!panel.hidden) connect(); // otherwise on first visit to the tab

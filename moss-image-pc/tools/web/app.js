/* MOSS Image Studio: the phone web app. Talks to the app on the computer (moss_phone.py) over a small JSON API. */
'use strict';

const csrf = document.body.dataset.csrf;
const $ = (id) => document.getElementById(id);
const S = {
  opts: null, state: null, mode: 'new', tab: 'create',
  source: null,            // picture to edit: {name, w, h}
  strokes: [],             // painted area, in 0..1 picture coordinates
  gallery: { items: [], total: 0, q: '', loaded: false, busy: false },
  viewer: { names: [], i: 0, info: null },
  picking: false, lastDone: null, online: true,
};

// ---------------- small helpers ----------------
function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v === true) e.setAttribute(k, '');
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid);
  return e;
}
const enc = encodeURIComponent;
const picUrl = (kind, name, v) => `/${kind}/${name.split('/').map(enc).join('/')}` + (v ? `?v=${v}` : '');
function store(key, value) {
  try { if (value === undefined) return JSON.parse(localStorage.getItem('moss.' + key)); localStorage.setItem('moss.' + key, JSON.stringify(value)); }
  catch (e) { return null; }
}
function mmss(s) { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function sizeText(bytes) { return bytes > 1e6 ? (bytes / 1e6).toFixed(1) + ' MB' : Math.round(bytes / 1e3) + ' KB'; }

let toastTimer;
function toast(msg, bad = false, ms = 3800) {
  const t = $('toast');
  t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

class ApiError extends Error { constructor(msg, status, data) { super(msg); this.status = status; this.data = data || {}; } }
async function api(path, body, raw) {
  const opt = { headers: {}, credentials: 'same-origin', cache: 'no-store' };
  if (body !== undefined) {
    opt.method = 'POST'; opt.headers['X-CSRF-Token'] = csrf;
    if (raw) { opt.body = body; opt.headers['Content-Type'] = raw; }
    else { opt.body = JSON.stringify(body); opt.headers['Content-Type'] = 'application/json'; }
  }
  let r;
  try { r = await fetch(path, opt); }
  catch (e) { throw new ApiError("Can't reach the computer. Is it on, with MOSS Image Studio open?", 0); }
  if (r.status === 401) { location.reload(); throw new ApiError('Sign in again.', 401); }
  let data = {};
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) throw new ApiError(data.message || `The computer answered ${r.status}.`, r.status, data);
  return data;
}

// Sends a request; if the computer asks first (cost, memory), asks here and sends it again.
async function withConfirm(path, params) {
  const confirmed = new Set(params.confirmed || []);
  for (;;) {
    try { return await api(path, Object.assign({}, params, { confirmed: [...confirmed] })); }
    catch (e) {
      if (e.status === 409 && e.data.confirm && !confirmed.has(e.data.confirm)) {
        if (!confirm(e.message)) return null;
        confirmed.add(e.data.confirm);
        continue;
      }
      throw e;
    }
  }
}

// ---------------- options and the Create form ----------------
async function loadOptions() {
  S.opts = await api('/api/options');
  const o = S.opts;
  const model = $('model'); model.textContent = '';
  for (const m of o.models) {
    model.append(el('option', { value: m.key, disabled: !m.available },
      m.name + (m.available ? '' : (m.cloud ? ' (needs API key)' : ' (not downloaded)'))));
  }
  const saved = store('model');
  const pick = [saved, o.model].find((k) => o.models.some((m) => m.key === k && m.available)) ||
    (o.models.find((m) => m.available) || {}).key;
  if (pick) model.value = pick;

  const shape = $('shape'); shape.textContent = '';
  const g1 = el('optgroup', { label: 'Shapes' }, o.shapes.map((s) => el('option', { value: s }, s)));
  shape.append(g1);
  if (o.screens.length) {
    shape.append(el('optgroup', { label: "The computer's screens" },
      o.screens.map((s) => el('option', { value: s.label }, s.label))));
  }
  shape.append(el('optgroup', { label: 'Other' }, el('option', { value: 'Custom' }, 'Custom size')));
  const tier = $('tier'); tier.textContent = '';
  o.tiers.forEach((t, i) => tier.append(el('option', { value: i }, t.replace(/^(.*?) \(~(.*)\)$/, (all, name, mp) => `${mp} (${name.toLowerCase()})`))));   // "1 MP (standard)"
  const sampler = $('adv-sampler'); sampler.textContent = '';
  for (const s of o.samplers) sampler.append(el('option', { value: s }, s || "Model's default"));

  const sh = store('shape'); if (sh && [...shape.options].some((x) => x.value === sh)) shape.value = sh;
  const ti = store('tier'); if (ti != null) tier.value = ti;
  const cn = store('count'); if (cn) $('count').value = cn;
  const cw = store('cw'), chh = store('ch'); if (cw) $('cw').value = cw; if (chh) $('ch').value = chh;
  const tc = store('textCheck'); $('text-check').checked = tc == null ? o.text_check : tc;

  const list = $('compare-list'); list.textContent = '';
  for (const m of o.models) {
    list.append(el('label', { class: 'check' }, el('input', { type: 'checkbox', value: m.key, disabled: !m.available, onchange: updateGo }),
      ` ${m.name}` + (m.available ? '' : ' (not available)')));
  }
  renderMore();
  onModelChange(true);
  onShapeChange();
}

function model() { return S.opts && S.opts.models.find((m) => m.key === $('model').value); }
function compareKeys() { return [...$('compare-list').querySelectorAll('input:checked')].map((i) => i.value); }
function hasPaint() { return S.strokes.some((s) => !s.erase); }

function onModelChange(initial) {
  const m = model(); if (!m) return;
  if (!initial) store('model', m.key);
  $('model-note').textContent = m.note + (m.license ? `  License: ${m.license}.` : '');
  fillAdvanced(m);
  $('adv').hidden = m.cloud;
  $('seed').disabled = m.cloud;
  $('seed').placeholder = m.cloud ? 'Not used by Nano Banana' : 'Random';
  updateMode();
}

function fillAdvanced(m) {
  $('adv-steps').value = m.adv.steps; $('adv-cfg').value = m.adv.cfg;
  $('adv-sampler').value = m.adv.sampler || ''; $('adv-negative').value = m.adv.negative || '';
}

function onShapeChange() {
  const v = $('shape').value;
  store('shape', v);
  $('custom-size').hidden = v !== 'Custom';
  $('tier-wrap').hidden = v === 'Custom';
  const scr = S.opts && S.opts.screens.find((s) => s.label === v);
  $('screen-opts').hidden = !scr;
  $('screen-split').closest('label').hidden = !scr || scr.parts < 2;
  refreshEstimate();
}

function setMode(mode) {
  S.mode = mode;
  for (const b of document.querySelectorAll('.seg [data-mode]')) b.classList.toggle('on', b.dataset.mode === mode);
  updateMode();
}

function updateMode() {
  const edit = S.mode === 'edit';
  const m = model();
  $('edit-card').hidden = !edit;
  $('size-new').hidden = edit;
  $('compare').hidden = edit;
  const painted = edit && hasPaint();
  $('prompt-label').textContent = !edit ? 'What do you want to see?' : painted ? 'What goes in the painted area?' : 'What should change?';
  $('prompt').placeholder = !edit ? 'A cozy cabin in a snowy forest at dusk, warm light in the windows'
    : painted ? 'A red sports car' : 'Make it night, with the windows lit';
  let note = 'Choose the picture to change. With an instruction ("make it night"), the whole picture is redrawn; paint an area to change only that part and keep the rest exactly.';
  if (edit && m) {
    if (m.cloud && painted) note = "Nano Banana can't use a painted area: clear it, or pick a model that runs on the computer.";
    else if (!m.cloud && !m.edit && !painted) note = (m.no_edit || `${m.name} can't follow edit instructions.`).replace('click Paint area... and paint', 'tap Paint area and paint');
    else if (painted) note = 'Only the painted area changes; the rest of the picture is kept exactly.';
  }
  $('src-note').textContent = note;
  $('src-paint').disabled = !S.source;
  $('src-paint').textContent = hasPaint() ? 'Change painted area' : 'Paint area';
  updateGo();
  refreshEstimate();
}

function updateGo() {
  const n = +$('count').value;
  const cmp = S.mode === 'new' ? compareKeys() : [];
  const go = $('go');
  if (cmp.length >= 2) go.textContent = `Compare ${cmp.length} models`;
  else if (S.mode === 'edit') go.textContent = n > 1 ? `Make ${n} edits` : 'Edit picture';
  else go.textContent = n > 1 ? `Make ${n} pictures` : 'Make picture';
}

const refreshEstimate = debounce(async () => {
  const m = model(); if (!m) return;
  const q = new URLSearchParams({ model: m.key, mode: S.mode, shape: $('shape').value, tier: $('tier').value,
    w: $('cw').value, h: $('ch').value, source: S.source ? S.source.name : '', masked: hasPaint() ? '1' : '0' });
  try {
    const r = await api('/api/estimate?' + q);
    let text = r.text || '';
    if (S.mode === 'edit' && r.w) text = `Keeps the picture's shape: ${text}`;
    $('size-line').textContent = text;
    $('size-warn').textContent = r.warning || ''; $('size-warn').hidden = !r.warning;
  } catch (e) { $('size-line').textContent = ''; }
}, 300);

const refreshTokens = debounce(async () => {
  const text = $('prompt').value.trim();
  if (!text) { $('tokens').textContent = ''; return; }
  try {
    const r = await api('/api/tokens?text=' + enc(text.slice(0, 6000)));
    $('tokens').textContent = r.tokens == null ? '' : `${r.tokens} tokens`;
  } catch (e) { /* ignore */ }
}, 700);

function collect() {
  const m = model();
  const p = {
    mode: S.mode, prompt: $('prompt').value, model: m ? m.key : '', shape: $('shape').value, tier: +$('tier').value,
    w: +$('cw').value, h: +$('ch').value, count: +$('count').value, seed: $('seed').value.trim() || 'random',
    text_check: $('text-check').checked, screen_upscale: $('screen-upscale').checked, screen_split: $('screen-split').checked,
  };
  if (m && !m.cloud) {
    p.adv = { steps: +$('adv-steps').value || null, cfg: +$('adv-cfg').value || null,
      sampler: $('adv-sampler').value, negative: $('adv-negative').value };
  }
  if (S.mode === 'new') { const cmp = compareKeys(); if (cmp.length >= 2) p.compare = cmp; }
  return p;
}

async function make(params) {
  const go = $('go');
  go.disabled = true;
  try {
    if (params.mode === 'edit') {
      if (!S.source) throw new ApiError('Choose the picture to edit first.');
      params.source = S.source.name;
      if (hasPaint()) params.mask = await maskDataUrl();
    }
    const r = await withConfirm('/api/make', params);
    if (r) { toast(r.message); poll(true); }
  } catch (e) { toast(e.message, true, 7000); }
  finally { go.disabled = false; }
}

// ---------------- the picture to edit ----------------
async function setSource(name, w, h) {
  if (!w || !h) {
    try { const info = await api('/api/info?name=' + enc(name)); w = info.width; h = info.height; } catch (e) { toast(e.message, true); return; }
  }
  S.source = { name, w, h };
  S.strokes = [];
  const box = $('src-thumb'); box.textContent = '';
  box.append(el('img', { src: picUrl('view', name), alt: '' }));
  updateMode();
}

function drawThumbMask() {
  const box = $('src-thumb');
  const old = box.querySelector('canvas'); if (old) old.remove();
  if (!hasPaint() || !S.source) return;
  const c = el('canvas'); const r = S.source.w / S.source.h;
  c.width = 224; c.height = Math.round(224 / r);
  paintStrokes(c.getContext('2d'), c.width, c.height, 'rgba(255,48,48,1)');
  c.style.opacity = '0.55';
  box.append(c);
}

async function uploadPhoto(file) {
  toast('Sending the photo to the computer...', false, 60000);
  const blob = await shrinkPhoto(file);
  const r = await api('/api/upload', blob, blob.type || 'image/jpeg');
  $('toast').hidden = true;
  return r;
}

// Phones take big photos (and iPhones use HEIC): redraw it here as a JPEG no bigger than the largest picture.
async function shrinkPhoto(file) {
  const maxPixels = (S.opts && S.opts.limits.max_pixels) || 4953600;
  let img;
  try {
    img = await createImageBitmap(file, { imageOrientation: 'from-image' });
  } catch (e) {
    img = await new Promise((ok, fail) => {
      const i = new Image(); const u = URL.createObjectURL(file);
      i.onload = () => { ok(i); }; i.onerror = () => fail(new ApiError("That photo couldn't be opened here.")); i.src = u;
    });
  }
  const w0 = img.width, h0 = img.height;
  const k = Math.min(1, Math.sqrt(maxPixels / (w0 * h0)));
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(w0 * k)); c.height = Math.max(1, Math.round(h0 * k));
  c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
  return await new Promise((ok) => c.toBlob((b) => ok(b || file), 'image/jpeg', 0.93));
}

// ---------------- painting the area to change ----------------
const P = { img: null, canvas: null, ctx: null, scale: 1, tool: 'paint', drawing: null, pointers: new Set() };

function brushRadius() { return (+$('p-brush').value * 0.006 + 0.004); }   // of the picture's shorter side

function paintStrokes(ctx, w, h, color, eraseColor) {
  const short = Math.min(w, h);
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  for (const s of S.strokes.concat(P.drawing ? [P.drawing] : [])) {
    if (s.erase && !eraseColor) ctx.globalCompositeOperation = 'destination-out';
    else ctx.globalCompositeOperation = 'source-over';
    const col = s.erase ? (eraseColor || '#000') : color;
    ctx.strokeStyle = col; ctx.fillStyle = col;
    ctx.lineWidth = s.r * short * 2;
    if (s.pts.length === 1) {
      ctx.beginPath(); ctx.arc(s.pts[0][0] * w, s.pts[0][1] * h, s.r * short, 0, Math.PI * 2); ctx.fill();
    } else {
      ctx.beginPath(); ctx.moveTo(s.pts[0][0] * w, s.pts[0][1] * h);
      for (const [x, y] of s.pts.slice(1)) ctx.lineTo(x * w, y * h);
      ctx.stroke();
    }
  }
  ctx.globalCompositeOperation = 'source-over';
}

async function openPainter() {
  if (!S.source) return;
  $('painter').hidden = false;
  document.body.style.overflow = 'hidden';
  P.canvas = $('p-canvas'); P.ctx = P.canvas.getContext('2d');
  P.saved = S.strokes.map((s) => ({ erase: s.erase, r: s.r, pts: s.pts.slice() }));
  P.img = new Image();
  P.img.onload = () => { fitPainter(); renderPainter(); };
  P.img.src = picUrl('view', S.source.name);
}

function fitPainter() {
  const stage = $('p-stage');
  const r = S.source.w / S.source.h;
  let w = stage.clientWidth - 16, h = w / r;
  if (h > stage.clientHeight - 16) { h = stage.clientHeight - 16; w = h * r; }
  const dpr = window.devicePixelRatio || 1;
  P.canvas.style.width = w + 'px'; P.canvas.style.height = h + 'px';
  P.canvas.width = Math.round(w * dpr); P.canvas.height = Math.round(h * dpr);
  P.overlay = document.createElement('canvas');
  P.overlay.width = P.canvas.width; P.overlay.height = P.canvas.height;
}

function renderPainter() {
  if (!P.img || !P.overlay) return;
  const { canvas, ctx } = P; const W = canvas.width, H = canvas.height;
  ctx.globalAlpha = 1; ctx.drawImage(P.img, 0, 0, W, H);
  const o = P.overlay.getContext('2d');
  o.clearRect(0, 0, W, H);
  paintStrokes(o, W, H, 'rgb(255,48,48)');
  ctx.globalAlpha = 0.5; ctx.drawImage(P.overlay, 0, 0); ctx.globalAlpha = 1;
}

function painterPoint(e) {
  const r = P.canvas.getBoundingClientRect();
  return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
}

function setupPainter() {
  const c = $('p-canvas');
  c.addEventListener('pointerdown', (e) => {
    P.pointers.add(e.pointerId);
    if (P.pointers.size > 1) { P.drawing = null; renderPainter(); return; }      // two fingers: not a stroke
    c.setPointerCapture(e.pointerId);
    P.drawing = { erase: P.tool === 'erase' || e.button === 2 || e.shiftKey, r: brushRadius(), pts: [painterPoint(e)] };
    renderPainter();
  });
  c.addEventListener('pointermove', (e) => {
    if (!P.drawing) return;
    const pts = P.drawing.pts, p = painterPoint(e), last = pts[pts.length - 1];
    if (Math.hypot(p[0] - last[0], p[1] - last[1]) > 0.002) { pts.push(p); renderPainter(); }
  });
  const end = (e) => {
    P.pointers.delete(e.pointerId);
    if (P.drawing) { S.strokes.push(P.drawing); P.drawing = null; renderPainter(); }
  };
  c.addEventListener('pointerup', end); c.addEventListener('pointercancel', end);
  c.addEventListener('contextmenu', (e) => e.preventDefault());
  for (const b of document.querySelectorAll('[data-tool]')) {
    b.addEventListener('click', () => {
      P.tool = b.dataset.tool;
      document.querySelectorAll('[data-tool]').forEach((x) => x.classList.toggle('on', x === b));
    });
  }
  $('p-undo').onclick = () => { S.strokes.pop(); renderPainter(); };
  $('p-clear').onclick = () => { S.strokes = []; renderPainter(); };
  $('p-cancel').onclick = () => { S.strokes = P.saved; closePainter(); };
  $('p-done').onclick = () => closePainter();
  window.addEventListener('resize', () => { if (!$('painter').hidden) { fitPainter(); renderPainter(); } });
}

function closePainter() {
  $('painter').hidden = true; document.body.style.overflow = '';
  drawThumbMask(); updateMode();
}

// The painted area at the picture's real size: white where it should change.
async function maskDataUrl() {
  const c = document.createElement('canvas');
  c.width = S.source.w; c.height = S.source.h;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#000'; ctx.fillRect(0, 0, c.width, c.height);
  paintStrokes(ctx, c.width, c.height, '#fff', '#000');
  return c.toDataURL('image/png');
}

// ---------------- live status ----------------
let pollTimer;
async function poll(soon) {
  clearTimeout(pollTimer);
  if (soon === true) { pollTimer = setTimeout(poll, 300); return; }
  if (document.hidden) return;
  try {
    const st = await api('/api/state');
    S.online = true;
    renderState(st);
  } catch (e) {
    S.online = false;
    const chip = $('pc-chip'); chip.textContent = 'Offline'; chip.className = 'chip off';
  }
  const busy = S.state && (S.state.busy || S.state.queue.length || S.state.tasks.some((t) => t.state === 'running'));
  pollTimer = setTimeout(poll, busy ? 1500 : 4000);
}

function renderState(st) {
  const prev = S.state; S.state = st;
  const chip = $('pc-chip');
  if (st.busy === 'picture') { chip.textContent = st.job && st.job.progress != null ? `Working ${Math.round(st.job.progress)}%` : 'Working'; chip.className = 'chip busy'; }
  else if (st.busy === 'tool') { chip.textContent = 'Busy'; chip.className = 'chip busy'; }
  else { chip.textContent = st.queue.length ? `${st.queue.length} waiting` : 'Ready'; chip.className = 'chip'; }

  const now = $('now');
  if (st.busy) {
    now.hidden = false;
    const j = st.job;
    $('now-title').textContent = j ? (j.fix ? 'Fixing words: ' : '') + (j.label || j.prompt) : 'Working on the computer';
    let status = st.status || '';
    if (j) {
      let left = '';
      if (j.progress != null && j.progress > 3) left = `, about ${mmss(j.elapsed * (100 - j.progress) / j.progress)} left`;
      else if (j.estimate) left = ` (${j.estimate})`;
      status = `${j.model}, ${j.w} x ${j.h}. ${mmss(j.elapsed)}${left}. ${status}`;
    }
    $('now-status').textContent = status;
    const bar = $('now-bar');
    if (j && j.progress != null) { bar.parentElement.classList.remove('indeterminate'); bar.style.width = j.progress + '%'; }
    else { bar.parentElement.classList.add('indeterminate'); bar.style.width = ''; }
    $('now-cancel').hidden = !j;
  } else now.hidden = true;

  const badge = $('badge');
  const n = st.queue.length + (st.busy === 'picture' ? 1 : 0);
  badge.textContent = n; badge.hidden = !n;
  if (S.tab === 'queue') renderQueue();
  $('wp-restore').hidden = !st.wallpaper_backup;
  renderFacts();

  if (prev && st.done_count > prev.done_count) {
    const fresh = st.finished.slice(0, st.done_count - prev.done_count);
    const pic = fresh.find((f) => f.name);
    const bad = fresh.find((f) => f.error);
    if (pic) {
      toast(`Done: ${pic.label}. Tap Queue or Gallery to see it.`);
      if (navigator.vibrate) navigator.vibrate(60);
    } else if (bad && bad.error !== 'cancelled') toast(`No picture: ${bad.error}`, true, 7000);
    if (S.gallery.loaded) loadGallery(true);
  }
  if (prev) {
    for (const t of st.tasks) {
      const old = prev.tasks.find((x) => x.id === t.id);
      if (old && old.state === 'running' && t.state !== 'running' && (t.kind === 'upscale' || t.kind === 'fixwords')) {
        if (t.state === 'done') toast(t.kind === 'upscale' ? 'The bigger copy is ready (in the Gallery).' : `Words fixed${t.result && !t.result.ok ? ' (some still not right)' : ''}. Saved in the Gallery.`);
        else toast(`${t.title} didn't work: ${t.error}`, true, 7000);
        if (S.gallery.loaded) loadGallery(true);
      }
    }
  }
}

function renderQueue() {
  const st = S.state; if (!st) return;
  const w = $('waiting'); w.textContent = '';
  if (!st.queue.length) w.append(el('div', { class: 'empty' }, 'Nothing waiting.'));
  for (const q of st.queue) {
    w.append(el('div', { class: 'item' },
      el('div', { class: 'grow' }, el('div', { class: 't' }, q.label), el('div', { class: 's' }, q.model + (q.phone ? ' (from a phone)' : ''))),
      el('button', { class: 'ghost small danger', type: 'button', onclick: () => queueAction('remove', q.qid) }, 'Remove')));
  }
  $('q-run').hidden = !st.queue.length || st.busy === 'picture';
  $('q-stop').hidden = !st.queue.length || !st.queue_running;
  $('q-clear').hidden = !st.queue.length;

  const t = $('tasks'); t.textContent = '';
  if (!st.tasks.length) t.append(el('div', { class: 'empty' }, 'Prompt tools, bigger copies and word fixes show here.'));
  for (const k of st.tasks) {
    const stateText = k.state === 'running' ? k.message : k.state === 'done' ? 'Done' : `Didn't work: ${k.error}`;
    const row = el('div', { class: 'item' },
      el('div', { class: 'grow' }, el('div', { class: 't' }, k.title), el('div', { class: 's ' + (k.state === 'failed' ? 'bad' : k.state === 'done' ? 'ok' : '') }, stateText)));
    if (k.state === 'running') row.prepend(el('span', { class: 'spin' }));
    if (k.state === 'done' && typeof k.result === 'string' && (k.kind === 'enhance' || k.kind === 'describe')) {
      row.append(el('button', { class: 'btn small', type: 'button', onclick: () => usePrompt(k.result) }, 'Use'));
    }
    const pic = k.state === 'done' && (k.kind === 'upscale' ? k.result : k.kind === 'fixwords' ? k.result && k.result.name : null);
    if (pic) row.append(el('button', { class: 'btn small', type: 'button', onclick: () => openViewer([pic], 0) }, 'Open'));
    t.append(row);
  }

  const f = $('finished'); f.textContent = '';
  if (!st.finished.length) f.append(el('div', { class: 'empty' }, 'Pictures made since the app started show here.'));
  const names = st.finished.filter((x) => x.name).map((x) => x.name);
  for (const x of st.finished) {
    if (x.name) {
      f.append(el('div', { class: 'fin', onclick: () => openViewer(names, names.indexOf(x.name)) },
        el('img', { src: picUrl('thumb', x.name), loading: 'lazy', alt: '' }), el('div', {}, x.label)));
    } else {
      f.append(el('div', { class: 'fin failed' }, `${x.label}: ${x.error === 'cancelled' ? 'cancelled' : 'no picture (' + x.error + ')'}`));
    }
  }
}

async function queueAction(action, qid) {
  try { const r = await api('/api/queue', { action, qid }); toast(r.message); poll(true); }
  catch (e) { toast(e.message, true); }
}

// ---------------- gallery ----------------
async function loadGallery(reset) {
  const g = S.gallery;
  if (g.busy) return;
  g.busy = true;
  try {
    const offset = reset ? 0 : g.items.length;
    const r = await api(`/api/gallery?q=${enc(g.q)}&offset=${offset}&limit=60`);
    g.items = reset ? r.items : g.items.concat(r.items);
    g.total = r.total; g.loaded = true;
    renderGallery(reset);
  } catch (e) { toast(e.message, true); }
  finally { g.busy = false; }
}

function renderGallery(reset) {
  const g = S.gallery, grid = $('grid');
  if (reset) grid.textContent = '';
  const have = grid.children.length;
  g.items.slice(have).forEach((it, k) => {
    const i = have + k;
    grid.append(el('button', { type: 'button', title: it.prompt, onclick: () => galleryTap(i) },
      el('img', { src: picUrl('thumb', it.name, it.mtime), loading: 'lazy', alt: it.title || '' })));
  });
  $('g-count').textContent = g.total ? `${g.total} picture${g.total === 1 ? '' : 's'}` + (g.q ? ` matching "${g.q}"` : '') : (g.q ? 'Nothing matches.' : 'No pictures yet.');
  $('g-more').hidden = g.items.length >= g.total;
}

function galleryTap(i) {
  const it = S.gallery.items[i];
  if (S.picking) { S.picking = false; $('pick-banner').hidden = true; setSource(it.name); showTab('create'); return; }
  openViewer(S.gallery.items.map((x) => x.name), i);
}

// ---------------- viewer ----------------
function openViewer(names, i) {
  S.viewer.names = names; S.viewer.i = Math.max(0, i);
  $('viewer').hidden = false; document.body.style.overflow = 'hidden';
  showPicture();
}
function closeViewer() { $('viewer').hidden = true; document.body.style.overflow = ''; $('v-img').src = ''; }

async function showPicture() {
  const v = S.viewer, name = v.names[v.i];
  if (!name) { closeViewer(); return; }
  $('v-pos').textContent = v.names.length > 1 ? `${v.i + 1} of ${v.names.length}` : '';
  $('v-prev').disabled = v.i <= 0; $('v-next').disabled = v.i >= v.names.length - 1;
  $('v-img').src = picUrl('view', name);
  $('v-download').href = picUrl('pic', name) + '?download=1';
  $('v-download').setAttribute('download', name.split('/').pop());
  $('v-prompt').textContent = '';
  $('v-facts').textContent = '';
  $('v-scroll').scrollTop = 0;
  try {
    const info = await api('/api/info?name=' + enc(name));
    if (v.names[v.i] !== name) return;
    v.info = info;
    $('v-prompt').textContent = info.prompt || '(no prompt saved in this picture)';
    const facts = [['Size', `${info.width} x ${info.height}, ${sizeText(info.bytes)}`], ['File', info.file]];
    if (info.reuse.seed != null) facts.push(['Seed', String(info.reuse.seed)]);
    if (info.date) facts.push(['Made', info.date.replace(/^(\d+):(\d+):(\d+)/, '$1-$2-$3')]);
    if (info.settings) facts.push(['Settings', info.settings]);
    if (info.tags && info.tags.length) facts.push(['Tags', info.tags.join(', ')]);
    for (const [k, val] of facts) $('v-facts').append(el('dt', {}, k), el('dd', {}, val));
    const o = S.opts || {};
    $('v-upscale').hidden = !o.upscaler; $('v-fix').hidden = !o.fixer;
    $('v-describe').hidden = !(o.describers && o.describers.length);
    $('v-variations').hidden = !info.prompt;
    $('v-reuse').hidden = !info.prompt;
  } catch (e) { toast(e.message, true); }
}

function setupViewer() {
  $('v-close').onclick = closeViewer;
  $('v-prev').onclick = () => { if (S.viewer.i > 0) { S.viewer.i--; showPicture(); } };
  $('v-next').onclick = () => { if (S.viewer.i < S.viewer.names.length - 1) { S.viewer.i++; showPicture(); } };
  let x0 = null, y0 = null;
  const wrap = $('v-img-wrap');
  wrap.addEventListener('touchstart', (e) => { if (e.touches.length === 1) { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; } else x0 = null; }, { passive: true });
  wrap.addEventListener('touchend', (e) => {
    if (x0 == null || (window.visualViewport && window.visualViewport.scale > 1.05)) return;
    const dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
    if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) (dx < 0 ? $('v-next') : $('v-prev')).click();
    x0 = null;
  });
  $('v-reuse').onclick = () => { reuse(S.viewer.info); };
  $('v-variations').onclick = () => {
    const p = reuseParams(S.viewer.info); if (!p) return;
    make(Object.assign(p, { count: 4, seed: 'random', mode: 'new' }));
  };
  $('v-edit').onclick = () => {
    const info = S.viewer.info; if (!info) return;
    closeViewer(); setMode('edit'); setSource(info.name, info.width, info.height); showTab('create');
    $('prompt').value = ''; refreshTokens(); $('prompt').focus();
  };
  $('v-upscale').onclick = () => upscaleSheet(S.viewer.info);
  $('v-fix').onclick = () => fixSheet(S.viewer.info);
  $('v-describe').onclick = () => describeSheet(S.viewer.info.name);
  $('v-wallpaper').onclick = () => wallpaperSheet(S.viewer.info);
  $('v-delete').onclick = async () => {
    const info = S.viewer.info; if (!info) return;
    if (!confirm(`Delete ${info.file}? It goes to the ${S.opts && S.opts.pc.os === 'macOS' ? 'Trash' : 'Recycle Bin'} on the computer, so it can be put back there.`)) return;
    try {
      const r = await api('/api/delete', { name: info.name });
      toast(r.message);
      S.viewer.names.splice(S.viewer.i, 1);
      S.gallery.items = S.gallery.items.filter((x) => x.name !== info.name);
      S.gallery.total = Math.max(0, S.gallery.total - 1);
      renderGallery(true);
      if (S.viewer.i >= S.viewer.names.length) S.viewer.i = S.viewer.names.length - 1;
      showPicture();
    } catch (e) { toast(e.message, true); }
  };
}

function reuseParams(info) {
  if (!info || !info.prompt) return null;
  const r = info.reuse, m = S.opts.models.find((x) => x.key === r.model && x.available);
  const p = { mode: 'new', prompt: info.prompt, model: m ? m.key : $('model').value, count: 1,
    text_check: $('text-check').checked };
  if (r.shape) { p.shape = r.shape; p.tier = r.tier || 0; }
  else if (r.w && r.h) { p.shape = 'Custom'; p.w = r.w; p.h = r.h; }
  else { p.shape = $('shape').value; p.tier = +$('tier').value; }
  return p;
}

function reuse(info) {
  const p = reuseParams(info); if (!p) return;
  closeViewer(); setMode('new');
  $('prompt').value = p.prompt;
  if ([...$('model').options].some((o) => o.value === p.model && !o.disabled)) { $('model').value = p.model; onModelChange(); }
  if ([...$('shape').options].some((o) => o.value === p.shape)) $('shape').value = p.shape;
  if (p.shape === 'Custom') { $('cw').value = p.w; $('ch').value = p.h; } else $('tier').value = p.tier;
  onShapeChange();
  $('seed').value = info.reuse.seed != null ? info.reuse.seed : '';
  showTab('create'); refreshTokens();
  toast(info.reuse.seed != null ? 'Prompt, size and seed loaded. Change a few words for a variation, or clear the seed.' : 'Prompt loaded.');
}

function usePrompt(text) {
  $('prompt').value = text; closeSheet(); showTab('create'); refreshTokens();
  toast('Prompt filled in.');
}

// ---------------- sheets ----------------
function openSheet(title, ...content) {
  $('sheet-title').textContent = title;
  const body = $('sheet-body'); body.textContent = ''; body.append(...content);
  $('sheet').hidden = false; $('sheet-back').hidden = false;
}
function closeSheet() { $('sheet').hidden = true; $('sheet-back').hidden = true; S.sheetTask = null; }

function seg(names, value) {
  const box = el('div', { class: 'seg' });
  box.value = value;
  for (const n of names) {
    box.append(el('button', { type: 'button', class: n === value ? 'on' : '', onclick: (e) => {
      box.value = n; box.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === e.currentTarget));
    } }, n));
  }
  return box;
}

function usingSelect(list) {
  const s = el('select'); for (const o of list) s.append(el('option', { value: o.id }, o.label));
  return s;
}

// Starts a tool on the computer and shows its progress and result in the open sheet.
async function runTask(params, out, onDone) {
  out.textContent = ''; out.append(el('p', { class: 'hint' }, el('span', { class: 'spin' }), 'Starting...'));
  let t;
  try { t = await api('/api/task', params); }
  catch (e) { out.textContent = ''; out.append(el('p', { class: 'warn' }, e.message)); return; }
  S.sheetTask = t.id; poll(true);
  const id = t.id;
  for (;;) {
    await new Promise((ok) => setTimeout(ok, 1500));
    if (S.sheetTask !== id) return;              // sheet closed: the result stays under Queue > Tools
    try { t = await api('/api/task?id=' + id); } catch (e) { continue; }
    if (t.state === 'running') { out.querySelector('.hint').lastChild.textContent = t.message; continue; }
    out.textContent = '';
    if (t.state === 'failed') out.append(el('p', { class: 'warn' }, "Didn't work: " + t.error));
    else onDone(t.result);
    return;
  }
}

function enhanceSheet() {
  const o = S.opts;
  if (!o.enhancers.length) { openSheet('Improve the prompt', el('p', {}, 'No prompt enhancer is set up on the computer. It needs llama.cpp and a Qwen model there (or a Gemini API key for the cloud one).')); return; }
  const idea = el('textarea', { rows: 4 }); idea.value = $('prompt').value;
  const using = usingSelect(o.enhancers); const len = seg(['Detailed', 'Short'], 'Detailed');
  const out = el('div');
  const go = el('button', { class: 'primary wide', type: 'button', onclick: () => {
    if (!idea.value.trim()) { toast('Type an idea first.', true); return; }
    runTask({ kind: 'enhance', idea: idea.value, using: using.value, length: len.value }, out, (text) => {
      out.append(el('div', { class: 'result' }, text),
        el('div', { class: 'row-btns' }, el('button', { class: 'primary', type: 'button', onclick: () => usePrompt(text) }, 'Use this'),
          el('button', { class: 'btn', type: 'button', onclick: () => go.click() }, 'Try again')));
    });
  } }, 'Improve');
  openSheet('Improve the prompt',
    el('p', { class: 'hint' }, 'Turns a short idea into a detailed prompt: subject, setting, lighting, mood and style.'),
    el('label', { class: 'field' }, el('span', {}, 'Idea'), idea),
    el('label', { class: 'field' }, el('span', {}, 'Using'), using),
    el('div', { class: 'field' }, len), el('div', { class: 'field' }, go), out);
}

function describeSheet(name) {
  const o = S.opts;
  if (!o.describers.length) { openSheet('Prompt from a picture', el('p', {}, 'No describer is set up on the computer. Get it from "Write a prompt from a picture" in the app there (or add a Gemini API key for the cloud one).')); return; }
  let pic = name || (S.mode === 'edit' && S.source ? S.source.name : null);
  const thumb = el('div', { class: 'src-thumb' });
  const showThumb = () => { thumb.textContent = ''; if (pic) thumb.append(el('img', { src: picUrl('view', pic), alt: '' })); else thumb.append(el('span', {}, 'No picture chosen')); };
  showThumb();
  const file = el('input', { type: 'file', accept: 'image/*', hidden: true, onchange: async () => {
    if (!file.files[0]) return;
    try { const r = await uploadPhoto(file.files[0]); pic = r.name; showThumb(); } catch (e) { toast(e.message, true); }
  } });
  const using = usingSelect(o.describers); const len = seg(['Detailed', 'Short'], 'Detailed');
  const out = el('div');
  const go = el('button', { class: 'primary wide', type: 'button', onclick: () => {
    if (!pic) { toast('Choose a picture first.', true); return; }
    runTask({ kind: 'describe', name: pic, using: using.value, length: len.value }, out, (text) => {
      out.append(el('div', { class: 'result' }, text),
        el('div', { class: 'row-btns' }, el('button', { class: 'primary', type: 'button', onclick: () => { closeViewer(); usePrompt(text); } }, 'Use this'),
          el('button', { class: 'btn', type: 'button', onclick: () => go.click() }, 'Try again')));
    });
  } }, 'Write a prompt');
  openSheet('Prompt from a picture',
    el('div', { class: 'src-row' }, thumb, el('div', { class: 'src-actions' },
      el('button', { class: 'btn', type: 'button', onclick: () => file.click() }, 'Photo from phone'), file,
      el('p', { class: 'hint' }, 'Works with photos and AI pictures: the prompt describes what is visible.'))),
    el('label', { class: 'field' }, el('span', {}, 'Using'), using),
    el('div', { class: 'field' }, len), el('div', { class: 'field' }, go), out);
}

function upscaleSheet(info) {
  if (!info) return;
  const out = el('div');
  const choice = (f) => el('button', { class: 'btn', type: 'button', onclick: () => {
    runTask({ kind: 'upscale', name: info.name, factor: f }, out, (name) => {
      out.append(el('p', {}, 'Saved a bigger copy.'), el('div', { class: 'row-btns' },
        el('button', { class: 'primary', type: 'button', onclick: () => { closeSheet(); openViewer([name], 0); } }, 'Open it')));
      if (S.gallery.loaded) loadGallery(true);
    });
  } }, `${f}x: ${info.width * f} x ${info.height * f}`);
  openSheet('Make a bigger copy',
    el('p', { class: 'hint' }, 'AI upscaling (Real-ESRGAN) on the computer. The original is kept; the copy is saved next to it.'),
    el('div', { class: 'choice' }, choice(2), choice(4)), out);
}

function fixSheet(info) {
  if (!info) return;
  const words = el('textarea', { rows: 4 }); words.value = (info.words || []).join('\n');
  const out = el('div');
  const go = el('button', { class: 'primary wide', type: 'button', onclick: () => {
    runTask({ kind: 'fixwords', name: info.name, words: words.value }, out, (r) => {
      out.append(el('p', { class: r.ok ? '' : 'warn' }, r.ok ? 'Words checked: correct.' : `Some words still aren't right. It reads: ${r.seen || 'nothing readable'}`),
        el('div', { class: 'row-btns' }, el('button', { class: 'primary', type: 'button', onclick: () => { closeSheet(); openViewer([r.name], 0); } }, 'Open it')));
      if (S.gallery.loaded) loadGallery(true);
    });
  } }, 'Fix the words');
  openSheet('Fix words',
    el('p', { class: 'hint' }, `What should the words say? One line for each piece of text, exactly as it should read. Only the wrong words are redrawn (with ${S.opts.fixer_name}); the result is saved as a new copy. About a minute for each wrong piece of text.`),
    el('label', { class: 'field' }, el('span', {}, 'Words'), words), el('div', { class: 'field' }, go), out);
}

function wallpaperSheet(info) {
  if (!info) return;
  const screens = (S.opts && S.opts.pc.screens) || [];
  const set = async (how, screen) => {
    try { const r = await api('/api/wallpaper', { name: info.name, how, screen }); toast(r.message); closeSheet(); poll(true); }
    catch (e) { toast(e.message, true); }
  };
  const opts = [];
  if (info.screen_parts.length) opts.push(['One per screen (sharpest, made for these screens)', () => set('per-screen')]);
  if (screens.length > 1) opts.push(['Span across all screens', () => set('span')]);
  opts.push(['Same picture on every screen', () => set('all')]);
  if (screens.length > 1) screens.forEach((s, i) => opts.push([`Only on the ${s.name} screen (${s.w} x ${s.h})`, () => set('one', i)]));
  openSheet('Set as the computer\'s wallpaper',
    el('p', { class: 'hint' }, 'Changes the desktop picture on the computer. "Put the previous wallpaper back" is under More.'),
    el('div', { class: 'choice' }, opts.map(([t, fn]) => el('button', { class: 'btn', type: 'button', onclick: fn }, t))));
}

// ---------------- More tab ----------------
function renderMore() {
  const o = S.opts;
  const list = $('model-list'); list.textContent = '';
  for (const m of o.models) {
    list.append(el('li', {}, el('span', {}, m.name),
      el('span', { class: 's' }, !m.available ? (m.cloud ? 'needs an API key' : 'not downloaded') : m.cloud ? 'cloud' : m.edit ? 'ready, edits too' : 'ready')));
  }
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  $('install-tip').textContent = standalone ? 'Opened from the home screen.' : ios
    ? 'For an app icon: in Safari, tap Share, then "Add to Home Screen".'
    : 'For an app icon: in Chrome\'s menu, tap "Add to Home screen" (or "Install app").';
  renderFacts();
}

function renderFacts() {
  const o = S.opts, st = S.state; if (!o) return;
  const f = $('pc-facts'); f.textContent = '';
  const facts = [['Name', o.pc.name], ['System', o.pc.os], ['App', 'MOSS Image Studio ' + o.pc.version],
    ['Screens', o.pc.screens.map((s) => `${s.name} ${s.w} x ${s.h}`).join(', ') || 'none found']];
  if (st) facts.push(['Now', st.busy ? (st.status || 'working') : (st.queue.length ? `${st.queue.length} waiting` : 'ready')]);
  for (const [k, v] of facts) f.append(el('dt', {}, k), el('dd', {}, v));
}

// ---------------- tabs and wiring ----------------
function showTab(name) {
  S.tab = name;
  for (const t of document.querySelectorAll('.tab')) t.hidden = t.id !== 'tab-' + name;
  for (const b of document.querySelectorAll('.tabs [data-tab]')) b.classList.toggle('on', b.dataset.tab === name);
  if (name === 'gallery' && !S.gallery.loaded) loadGallery(true);
  if (name === 'queue') renderQueue();
  if (name !== 'gallery' && S.picking) { S.picking = false; $('pick-banner').hidden = true; }
  window.scrollTo(0, 0);
}

function wire() {
  for (const b of document.querySelectorAll('.tabs [data-tab]')) b.onclick = () => showTab(b.dataset.tab);
  for (const b of document.querySelectorAll('.seg [data-mode]')) b.onclick = () => setMode(b.dataset.mode);
  $('model').onchange = () => onModelChange();
  $('shape').onchange = onShapeChange;
  $('tier').onchange = () => { store('tier', $('tier').value); refreshEstimate(); };
  $('cw').oninput = () => { store('cw', $('cw').value); refreshEstimate(); };
  $('ch').oninput = () => { store('ch', $('ch').value); refreshEstimate(); };
  $('count').onchange = () => { store('count', $('count').value); updateGo(); };
  $('text-check').onchange = () => store('textCheck', $('text-check').checked);
  $('prompt').oninput = refreshTokens;
  $('adv-reset').onclick = () => { const m = model(); if (m) fillAdvanced(m); };
  $('btn-clear').onclick = () => { $('prompt').value = ''; $('seed').value = ''; refreshTokens(); $('prompt').focus(); };
  $('btn-enhance').onclick = enhanceSheet;
  $('btn-describe').onclick = () => describeSheet(null);
  $('go').onclick = () => {
    if (!$('prompt').value.trim()) { toast(S.mode === 'edit' ? 'Describe what should change first.' : 'Type what you want to see first.', true); $('prompt').focus(); return; }
    make(collect());
  };
  $('src-file').onchange = async (e) => {
    const f = e.target.files[0]; e.target.value = '';
    if (!f) return;
    try { const r = await uploadPhoto(f); await setSource(r.name, r.width, r.height); } catch (err) { toast(err.message, true, 8000); }
  };
  $('src-gallery').onclick = () => { S.picking = true; showTab('gallery'); $('pick-banner').hidden = false; };
  $('pick-cancel').onclick = () => { S.picking = false; $('pick-banner').hidden = true; showTab('create'); };
  $('src-paint').onclick = openPainter;
  $('now-cancel').onclick = async () => {
    if (!confirm('Stop the picture being made now?')) return;
    try { const r = await api('/api/cancel', {}); toast(r.message); poll(true); } catch (e) { toast(e.message, true); }
  };
  $('pc-chip').onclick = () => showTab('queue');
  $('q-run').onclick = () => queueAction('run');
  $('q-stop').onclick = () => queueAction('stop');
  $('q-clear').onclick = () => { if (confirm('Remove every waiting picture?')) queueAction('clear'); };
  const search = debounce(() => { S.gallery.q = $('g-search').value.trim(); loadGallery(true); }, 400);
  $('g-search').oninput = search;
  $('g-more').onclick = () => loadGallery(false);
  $('sheet-close').onclick = closeSheet; $('sheet-back').onclick = closeSheet;
  $('wp-restore').onclick = async () => { try { const r = await api('/api/wallpaper-restore', {}); toast(r.message); poll(true); } catch (e) { toast(e.message, true); } };
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { if (!$('sheet').hidden) closeSheet(); else if (!$('viewer').hidden) closeViewer(); }
    if (!$('viewer').hidden && $('sheet').hidden) { if (e.key === 'ArrowLeft') $('v-prev').click(); if (e.key === 'ArrowRight') $('v-next').click(); }
  });
  setupPainter(); setupViewer();
}

wire();
loadOptions().catch((e) => toast(e.message, true, 8000));
poll();

/* ATE Log Parser front end. Talks to Python through window.pywebview.api (see app/main.py).
   When opened in a normal browser (no pywebview) it runs on demo data so the look can be checked. */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const S = { files: [], traveller: '', output: '', stats: 'pass', profile: '', profiles: {}, recent: [], busy: false, version: '' };
let api = null;

/* ---------------- helpers ---------------- */
function toast(msg, err) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : '');
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.add('hidden'), err ? 6000 : 3200);
}
const base = p => String(p || '').split(/[\\/]/).pop();
const kb = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
const stageClass = s => 'st-' + (s === 'Post-Bake' || s === 'Bake' ? 'Bake' : (['Room', 'Hot', 'Cold'].includes(s) ? s : 'Room'));

/* ---------------- tabs ---------------- */
document.querySelectorAll('.tab').forEach(b => b.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === b));
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('hidden', p.id !== 'page-' + b.dataset.page));
  if (b.dataset.page === 'recent') renderRecent();
  if (b.dataset.page === 'profiles') renderProfile();
});

/* ---------------- segmented ---------------- */
function placePill() {
  const seg = $('#stats'), on = seg.querySelector('button.on'), pill = seg.querySelector('.pill');
  pill.style.left = on.offsetLeft + 'px'; pill.style.width = on.offsetWidth + 'px';
}
$('#stats').querySelectorAll('button').forEach(b => b.onclick = () => {
  S.stats = b.dataset.v; $('#stats').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); placePill();
});
window.addEventListener('resize', placePill);

/* ---------------- files ---------------- */
async function addPaths(paths) {
  if (!paths || !paths.length) return;
  setStatus('Reading headers…', true);
  try {
    const got = await api.scan(paths, S.profile);
    const have = new Set(S.files.map(f => f.path));
    const add = got.filter(f => !have.has(f.path));
    S.files = S.files.concat(add).sort((a, b) => (a.start || '').localeCompare(b.start || '') || a.name.localeCompare(b.name));
    renderFiles();
    toast(add.length ? `Added ${add.length} file${add.length > 1 ? 's' : ''}` : 'No new STDF files found there');
  } catch (e) { toast(String(e), true); }
  setStatus(S.files.length ? 'Ready' : 'Ready');
}
function renderFiles() {
  const n = S.files.length;
  $('#fcount').textContent = n ? `· ${n}` : '';
  $('#drop').classList.toggle('hidden', n > 0);
  $('#files').classList.toggle('hidden', n === 0);
  $('#go').disabled = !n || S.busy;
  const cnt = {};
  S.files.forEach(f => { const k = f.ok ? f.stage : 'Unreadable'; cnt[k] = (cnt[k] || 0) + 1; });
  const lots = [...new Set(S.files.map(f => f.lot).filter(Boolean))];
  $('#chips').innerHTML = !n ? '' :
    Object.entries(cnt).map(([k, v]) => `<span class="chip ${k === 'Unreadable' ? 'st-err' : stageClass(k)}"><span class="dot"></span>${esc(k)} · ${v}</span>`).join('') +
    (lots.length ? `<span class="chip">Lot ${esc(lots.slice(0, 3).join(', '))}${lots.length > 3 ? ' +' + (lots.length - 3) : ''}</span>` : '');
  $('#files').innerHTML = S.files.map((f, i) => `
    <div class="frow" title="${esc(f.path)}">
      <span class="chip ${f.ok ? stageClass(f.stage) : 'st-err'}" style="justify-content:center">${f.ok ? esc(f.stage) : 'Error'}</span>
      <div style="min-width:0"><div class="name">${esc(f.name)}</div>
        <div class="sub">${f.ok ? esc([f.folder, f.lot && 'lot ' + f.lot, f.job].filter(Boolean).join(' · ')) : esc(f.error)}</div></div>
      <div class="when tabular">${esc(f.start || '')}</div>
      <div class="size tabular">${kb(f.size || 0)}</div>
      <button class="icon-btn x" data-i="${i}" title="Remove"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
    </div>`).join('');
  $('#files').querySelectorAll('.x').forEach(b => b.onclick = () => { S.files.splice(+b.dataset.i, 1); renderFiles(); });
}
$('#addFiles').onclick = async () => addPaths(await api.pick_files());
$('#addFolder').onclick = async () => addPaths(await api.pick_folder());
$('#clearFiles').onclick = () => { S.files = []; renderFiles(); };
$('#drop').onclick = async () => addPaths(await api.pick_folder());

/* drag & drop: the visual state is handled here, the real paths come from Python (window.onDropPaths) */
['dragenter', 'dragover'].forEach(ev => document.addEventListener(ev, e => { e.preventDefault(); $('#drop').classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => document.addEventListener(ev, e => { e.preventDefault(); if (ev === 'drop' || !e.relatedTarget) $('#drop').classList.remove('over'); }));
window.onDropPaths = paths => { $('#drop').classList.remove('over'); addPaths(paths); };

/* ---------------- settings ---------------- */
function renderProfiles() {
  const names = Object.keys(S.profiles);
  const opts = names.map(n => `<option ${n === S.profile ? 'selected' : ''}>${esc(n)}</option>`).join('');
  $('#profile').innerHTML = opts; $('#profView').innerHTML = opts;
}
$('#profile').onchange = e => { S.profile = e.target.value; };
async function importProfile() {
  const r = await api.import_profile();
  if (!r || !r.ok) { if (r && r.error) toast(r.error, true); return; }
  S.profiles = r.profiles; S.profile = r.name; renderProfiles(); renderProfile(); toast('Profile imported: ' + r.name);
}
$('#importProfile').onclick = importProfile; $('#importProfile2').onclick = importProfile;
function setPath(kind, v) {
  S[kind] = v || '';
  const txt = kind === 'traveller' ? $('#travTxt') : $('#outTxt');
  txt.textContent = v ? base(v) : (kind === 'traveller' ? 'No traveller - report only' : 'Automatic - next to the STDF files');
  txt.title = v || ''; txt.classList.toggle('placeholder', !v);
  (kind === 'traveller' ? $('#travClear') : $('#outClear')).classList.toggle('hidden', !v);
}
$('#travPick').onclick = async () => { const p = await api.pick_traveller(); if (p) setPath('traveller', p); };
$('#travClear').onclick = () => setPath('traveller', '');
$('#outPick').onclick = async () => { const p = await api.pick_output(); if (p) setPath('output', p); };
$('#outClear').onclick = () => setPath('output', '');

/* ---------------- run ---------------- */
function setStatus(t, spinning) { $('#status').innerHTML = (spinning ? '<span class="spin"></span>' : '') + esc(t); }
function log(s) { const l = $('#log'); l.textContent += (l.textContent ? '\n' : '') + s; l.scrollTop = l.scrollHeight; }
$('#go').onclick = async () => {
  if (!S.files.length || S.busy) return;
  S.busy = true; $('#go').disabled = true;
  $('#log').textContent = ''; $('#resCard').classList.add('hidden');
  $('#progress').classList.remove('hidden'); setStatus('Working…', true);
  const r = await api.generate({ paths: S.files.filter(f => f.ok).map(f => f.path), profile: S.profile, traveller: S.traveller, output: S.output, stats: S.stats });
  if (!r || !r.ok) finish(), toast((r && r.error) || 'Could not start', true);
};
function finish() { S.busy = false; $('#progress').classList.add('hidden'); $('#go').disabled = !S.files.length; }
window.onLog = s => log(s);
window.onError = msg => { finish(); setStatus('Failed'); log('ERROR: ' + msg); toast(msg, true); };
window.onDone = sum => {
  finish(); setStatus('Done'); renderResult(sum);
  S.recent.unshift({ when: new Date().toISOString().slice(0, 16).replace('T', ' '), lot: sum.lot, outputs: sum.outputs, files: sum.files, stages: sum.stages });
};
function stageTile(t) {
  const lo = t.good_min, hi = t.good_max, exact = lo === hi;
  const pct = t.parts ? Math.round(100 * (exact ? lo : hi) / t.parts) : 0;
  return `<div class="tile kpi">
    <div class="stage"><span class="chip ${stageClass(t.stage)}"><span class="dot"></span>${esc(t.stage)}</span><span style="color:var(--faint);font-size:13px">${esc(t.device || '')}</span></div>
    <div><span class="num tabular">${exact ? lo : lo + '–' + hi}</span> <span class="of tabular">/ ${t.parts}</span></div>
    <div class="cap">good parts${exact ? '' : ' (range - some parts could not be told apart)'} · ${t.files} file${t.files > 1 ? 's' : ''}</div>
    <div class="meter"><i style="width:${pct}%"></i></div>
  </div>`;
}
function renderResult(sum) {
  $('#kpis').innerHTML = (sum.stages || []).map(stageTile).join('');
  const conf = (sum.stages || []).reduce((a, t) => a + (t.confirmed || 0), 0);
  const poss = (sum.stages || []).reduce((a, t) => a + (t.possible || 0), 0);
  const extra = (sum.stages || []).reduce((a, t) => a + (t.extra || 0), 0);
  const sk = (sum.skipped || []).length;
  $('#flags').innerHTML =
    `<span class="chip ${conf ? 'flag-bad' : 'flag-ok'}">${conf} confirmed repeat${conf === 1 ? '' : 's'} of a passed part</span>` +
    (poss ? `<span class="chip flag-warn">${poss} possible repeat${poss === 1 ? '' : 's'}</span>` : '') +
    (extra ? `<span class="chip flag-warn">${extra} extra insert${extra === 1 ? '' : 's'}</span>` : '') +
    (sk ? `<span class="chip">${sk} file${sk === 1 ? '' : 's'} skipped</span>` : '');
  const outs = sum.outputs || [];
  $('#outBtns').innerHTML = outs.map((o, i) =>
    `<button class="${i ? 'btn-ghost' : 'btn-primary'}" data-p="${esc(o)}">${i ? 'Open traveller copy' : 'Open report'}</button>`).join('') +
    (outs.length ? `<button class="btn-ghost" data-f="${esc(outs[0])}">Show in folder</button>` : '');
  $('#resCard').classList.remove('hidden');
  $('#resLot').textContent = (sum.lot ? '· lot ' + sum.lot : '') + ' · ' + (sum.files || 0) + ' files';
  document.querySelector('main').scrollTo({ top: 0, behavior: 'smooth' });
  $('#outBtns').querySelectorAll('[data-p]').forEach(b => b.onclick = () => api.open_path(b.dataset.p));
  $('#outBtns').querySelectorAll('[data-f]').forEach(b => b.onclick = () => api.open_folder(b.dataset.f));
}

/* ---------------- recent ---------------- */
function renderRecent() {
  const r = S.recent;
  $('#recent').innerHTML = !r.length ? '<div class="empty">No reports yet. Generated reports show up here.</div>' : r.map((x, i) => `
    <div class="list-item">
      <div class="logo" style="width:38px;height:38px;border-radius:14px;box-shadow:none"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"><path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/></svg></div>
      <div class="grow">
        <div style="font-weight:700">${esc(base((x.outputs || [])[0]) || 'Report')}</div>
        <div style="color:var(--muted);font-size:13px">${esc(x.when)}${x.lot ? ' · lot ' + esc(x.lot) : ''} · ${x.files || 0} files</div>
        <div class="chips" style="margin:8px 0 0">${(x.stages || []).map(t => `<span class="chip ${stageClass(t.stage)}">${esc(t.stage)} ${t.good_min === t.good_max ? t.good_min : t.good_min + '–' + t.good_max}/${t.parts}</span>`).join('')}</div>
      </div>
      ${(x.outputs || []).map((o, j) => `<button class="btn-ghost" data-p="${esc(o)}">${j ? 'Traveller' : 'Open'}</button>`).join('')}
      ${(x.outputs || []).length ? `<button class="icon-btn" title="Show in folder" data-f="${esc(x.outputs[0])}"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg></button>` : ''}
    </div>`).join('');
  $('#recent').querySelectorAll('[data-p]').forEach(b => b.onclick = async () => { if (!(await api.open_path(b.dataset.p))) toast('That file is no longer there', true); });
  $('#recent').querySelectorAll('[data-f]').forEach(b => b.onclick = () => api.open_folder(b.dataset.f));
}

/* ---------------- profiles ---------------- */
$('#profView').onchange = () => renderProfile();
async function renderProfile() {
  const name = $('#profView').value || S.profile;
  if (!name) return;
  const d = await api.profile_details(name);
  const row = (k, v) => `<div class="k">${k}</div><div class="v">${v}</div>`;
  const list = a => (a || []).length ? (a || []).map(x => `<span class="chip" style="margin:0 6px 6px 0">${esc(x)}</span>`).join('') : '<span style="color:var(--faint)">none</span>';
  $('#profDetails').innerHTML = `<div class="kv">
    ${row('Name', esc(d.name))}
    ${row('Fuse read-back test', d.readback_test ? `<span class="mono">${esc(d.readback_test)}</span>` : '<span style="color:var(--faint)">not set - parts matched by test values only</span>')}
    ${row('Blown-bit pattern', d.blown_bit_regex ? `<span class="mono">${esc(d.blown_bit_regex)}</span>` : '<span style="color:var(--faint)">not set</span>')}
    ${row('Device pattern', `<span class="mono">${esc(d.device_regex || '')}</span>`)}
    ${row('Stage rules', list((d.temperature_rules || []).map(r => r[0] + ' ← ' + r[1])))}
    ${row('Matching tests', list(d.compare_suites))}
    ${row('Skipped file tags', list(d.excluded_tags))}
    ${row('Traveller sheets', list(Object.entries(d.traveller_sheets || {}).map(([k, v]) => k + ': ' + v.join(', '))))}
  </div>`;
}

/* ---------------- start ---------------- */
async function start() {
  const info = await api.app_info();
  S.version = info.version; S.profiles = info.profiles || {}; S.recent = info.recent || [];
  S.profile = info.profile && S.profiles[info.profile] !== undefined ? info.profile : Object.keys(S.profiles)[0] || '';
  $('#subtitle').textContent = `STDF lot report & traveller fill-in · v${info.version}`;
  S.stats = info.stats || 'pass';
  $('#stats').querySelectorAll('button').forEach(x => x.classList.toggle('on', x.dataset.v === S.stats));
  renderProfiles(); renderFiles(); requestAnimationFrame(placePill);
}
if (window.pywebview && window.pywebview.api) { api = window.pywebview.api; start(); }
else window.addEventListener('pywebviewready', () => { api = window.pywebview.api; start(); });
if (location.search.includes('demo')) setTimeout(() => { if (!api) { api = demoApi(); start(); } }, 300);   // browser preview only

/* ---------------- demo data (browser preview only) ---------------- */
function demoApi() {
  const mk = (n, st, t, sz, folder = 'PROD1153') => ({ path: 'C:/Lots/' + folder + '/' + n, name: n, folder, size: sz, lot: 'PROD1153', job: 'CROW_1050_REV04P01', start: t, stage: st, ok: true });
  const demo = [
    mk('1050_ROOM_T1_PROD1153_20261104_0912.stdf', 'Room', '2026-11-04 09:12', 6.1e6), mk('1050_ROOM_T2_PROD1153_20261104_1130.stdf', 'Room', '2026-11-04 11:30', 2.2e6),
    mk('1050_ROOM_RETEST_PROD1153_20261104_1305.stdf', 'Room', '2026-11-04 13:05', 4.0e5), mk('1050_HOT_T1_PROD1153_20261105_0840.stdf', 'Hot', '2026-11-05 08:40', 5.8e6),
    mk('1050_HOT_T2_PROD1153_20261105_1557.stdf', 'Hot', '2026-11-05 15:57', 1.9e6), mk('1050_COLD_T1_PROD1153_20261106_0722.stdf', 'Cold', '2026-11-06 07:22', 2.4e6),
    mk('1050_POSTBAKE_PROD1153_20261106_1010.stdf', 'Post-Bake', '2026-11-06 10:10', 2.0e6),
  ];
  const profiles = { 'Generic (no fuse ID)': '', 'My device (local)': '' };
  const summary = { files: 7, lot: 'PROD1153', skipped: [], outputs: ['C:/Lots/PROD1153/STDF_Lot_Report_PROD1153.xlsx', 'C:/Lots/PROD1153/Crow_Lot_Traveller_PROD1153_STDF_fill-in.xlsx'],
    stages: [{ device: '1050', stage: 'Room', files: 3, parts: 100, extra: 2, good_min: 97, good_max: 97, confirmed: 0, possible: 0 },
             { device: '1050', stage: 'Hot', files: 2, parts: 95, extra: 0, good_min: 67, good_max: 83, confirmed: 0, possible: 4 },
             { device: '1050', stage: 'Cold', files: 1, parts: 35, extra: 0, good_min: 29, good_max: 35, confirmed: 0, possible: 0 },
             { device: '1050', stage: 'Post-Bake', files: 1, parts: 30, extra: 0, good_min: 30, good_max: 30, confirmed: 0, possible: 0 }] };
  const steps = ['Reading 7 STDF files…', '  1050_ROOM_T1_PROD1153_20261104_0912.stdf  100 parts', '  1050_HOT_T1_PROD1153_20261105_0840.stdf  95 parts',
    'Tracing parts across files…', 'Writing STDF_Lot_Report_PROD1153.xlsx', 'Writing traveller copy with STDF fill-in sheet', 'Done in 14.2 s'];
  return {
    app_info: async () => ({ version: '0.3.0', profiles, profile: 'My device (local)', recent: [{ when: '2026-11-05 18:02', lot: 'PROD1153', files: 5, outputs: summary.outputs, stages: summary.stages.slice(0, 2) }], stats: 'pass' }),
    profile_details: async n => ({ name: n, readback_test: n.includes('local') ? 'Device_Read_Fuse_Data' : '', blown_bit_regex: n.includes('local') ? 'EFUSE_(?P<un>UN)?BLOWN_BIT(?P<bit>\\d+)' : '',
      device_regex: '(10[25]0)', temperature_rules: [['Cold', 'COLD|_-55_'], ['Hot', 'HOT|_125_'], ['Room', 'ROOM|_25_']], compare_suites: ['UVLO_Threshold', 'Post_SS_Test', 'SlowTrip'],
      excluded_tags: ['SANITY', 'ENG', 'SAMPLE'], traveller_sheets: { Room: ['T1', 'T2', 'T3'], Hot: ['HOT_T1', 'HOT_T2'], Cold: ['COLD_T1', 'COLD_T2'] } }),
    pick_files: async () => [], pick_folder: async () => ['C:/Lots/PROD1153'], pick_traveller: async () => 'C:/Lots/PROD1153/Crow_Lot_Traveller_PROD1153.xlsx',
    pick_output: async () => '', import_profile: async () => ({ ok: false }), scan: async () => demo,
    generate: async () => { let i = 0; const t = setInterval(() => { window.onLog(steps[i++]); if (i >= steps.length) { clearInterval(t); window.onDone(summary); } }, 120); return { ok: true }; },
    open_path: async () => true, open_folder: async () => true,
  };
}

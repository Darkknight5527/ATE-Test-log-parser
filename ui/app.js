/* Lot Yield Viewer front end. Talks to Python through window.pywebview.api (see lot_yield_app.py). */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const AREA = { all: null, room: 'Room', postbake: 'PostBake', hot: 'Hot', cold: 'Cold' };
const KEY = { Room: 'room', PostBake: 'postbake', Hot: 'hot', Cold: 'cold' };
const LABEL = { Room: 'Room 25C', PostBake: 'Post-Bake 25C', Hot: 'Hot 125C', Cold: 'Cold -55C' };
const SHORT = { Room: 'Room', PostBake: 'Post-Bake', Hot: 'Hot', Cold: 'Cold' };
const FLOW = ['Room', 'PostBake', 'Hot', 'Cold'];
const S = {
  page: 'info', rows: [], groups: [], checked: new Set(), test: null, tests: [], area: 'all', output: '',
  busy: false, sum: null, view: 'all', outputs: {}, recent: [], chartKind: 'trend', lot: null, lots: [], theme: 'dark'
};
let api = null, chart = null;

/* ---------------- helpers ---------------- */
function toast(msg, err) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : '');
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.add('hidden'), err ? 6000 : 2600);
}
const base = p => String(p || '').split(/[\\/]/).pop();
const pct = (x, d = 2) => x == null ? '' : (100 * x).toFixed(d) + '%';
const num = n => n == null ? '' : Number(n).toLocaleString('en-US');
const fx = (v, d) => { if (v == null) return ''; const a = Math.abs(v);
  if (a >= 1e6 || (a < 1e-3 && v !== 0)) return Number(v).toExponential(3);
  if (d == null) d = a >= 1000 ? 1 : a >= 10 ? 3 : 4;
  const s = Number(v).toFixed(d); return s.includes('.') ? s.replace(/\.?0+$/, '') : s; };
const chip = (t, txt) => `<span class="chip st-${esc(t || 'skip')}"><span class="dot"></span>${esc(txt ?? SHORT[t] ?? t)}</span>`;
const day = ts => ts ? new Date(ts * 1000).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', timeZone: 'UTC' }) : '';
const span = (a, b) => { const x = day(a), y = day(b); return x === y ? x : `${x} – ${y}`; };
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const rgbv = v => `rgb(${css(v)})`;
async function copy(text, what) {
  try { await navigator.clipboard.writeText(String(text)); }
  catch (e) { const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
  toast('Copied ' + (what || text));
}
const ICON_COPY = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/></svg>';
const cbtn = (val, what) => `<button class="copy" data-copy="${esc(val)}" data-what="${esc(what || '')}" title="Copy">${ICON_COPY}</button>`;
document.addEventListener('click', e => { const b = e.target.closest('[data-copy]'); if (b) copy(b.dataset.copy, b.dataset.what || b.dataset.copy); });

/* ---------------- theme ---------------- */
function applyTheme(t) {
  S.theme = t; document.documentElement.dataset.theme = t;
  $('#themeIcon').innerHTML = t === 'dark'
    ? '<path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z"/>'
    : '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
  if (S.page === 'charts') drawChart();
}
$('#theme').onclick = () => { applyTheme(S.theme === 'dark' ? 'light' : 'dark'); api.set_theme(S.theme); };

/* ---------------- tabs ---------------- */
function showPage(name) {
  S.page = name;
  $$('.tab').forEach(x => x.classList.toggle('active', x.dataset.page === name));
  $$('.page').forEach(p => p.classList.toggle('hidden', p.id !== 'page-' + name));
  const side = ['info', 'charts', 'traveller'].includes(name);
  $('#side').classList.toggle('hidden', !side);
  $('#layout').classList.toggle('noside', !side);
  if (name === 'charts') { fillPairPick(); drawChart(); }
  if (name === 'traveller') renderTraveller();
  if (name === 'prod') loadLots();
  requestAnimationFrame(() => ['#area', '#view', '#chartKind'].forEach(placePill));
}
$$('.tab').forEach(b => b.onclick = () => showPage(b.dataset.page));

/* ---------------- segmented controls ---------------- */
function placePill(sel) {
  const seg = $(sel); if (!seg) return;
  const on = seg.querySelector('button.on'), pill = seg.querySelector('.pill');
  if (!on || !on.offsetWidth) return;
  pill.style.left = on.offsetLeft + 'px'; pill.style.width = on.offsetWidth + 'px';
}
function setSeg(sel, v) { $(sel).querySelectorAll('button').forEach(x => x.classList.toggle('on', x.dataset.v === v)); placePill(sel); }
$('#area').querySelectorAll('button').forEach(b => b.onclick = () => { S.area = b.dataset.v; setSeg('#area', S.area); });
$('#view').querySelectorAll('button').forEach(b => b.onclick = () => setView(b.dataset.v));
$('#chartKind').querySelectorAll('button').forEach(b => b.onclick = () => { S.chartKind = b.dataset.v; setSeg('#chartKind', S.chartKind); drawChart(); });
window.addEventListener('resize', () => ['#area', '#view', '#chartKind'].forEach(placePill));

/* ---------------- files ---------------- */
async function openPaths(paths) {
  if (!paths || !paths.length) return;
  setBusy('Reading files…');
  try {
    const st = await api.open_paths(paths);
    applyState(st, true);
    toast(st.added ? `Opened ${st.added} file${st.added > 1 ? 's' : ''}` : 'No new STDF files there');
    if (st.errors && st.errors.length) toast(st.errors[0], true);
  } catch (e) { toast(String(e), true); }
  setBusy('');
}
function setBusy(t) { $('#sumNote').textContent = t ? '· ' + t : ''; document.body.classList.toggle('wait', !!t); }
function applyState(st, keepChecks) {
  const before = new Set(S.rows.map(r => r.id));
  S.rows = st.rows; S.groups = st.groups;
  const ids = new Set(S.rows.map(r => r.id));
  S.checked = new Set([...S.checked].filter(i => ids.has(i)));
  S.rows.forEach(r => { if (!before.has(r.id)) S.checked.add(r.id); });
  if (!keepChecks) S.checked = new Set(S.rows.map(r => r.id));
  renderFiles(); renderInfo(); loadTests(); loadLimits();
  if (S.page === 'charts') { fillPairPick(); drawChart(); }
  if (S.page === 'traveller') renderTraveller();
}
const checkedIds = () => S.rows.filter(r => S.checked.has(r.id)).map(r => r.id);
function renderFiles() {
  const n = S.rows.length;
  $('#fcount').textContent = n ? `· ${S.checked.size} of ${n} checked` : '';
  $('#flist').innerHTML = !n ? '<div class="empty sm">No files yet.</div>' : S.rows.map(r => `
    <label class="fitem${r.note ? ' skip' : ''}" title="${esc(r.path)}">
      <input type="checkbox" data-id="${r.id}" ${S.checked.has(r.id) ? 'checked' : ''}>
      ${chip(r.area)}
      <span class="fname">${esc(shortName(r.name))}</span>
      <button class="icon-btn x" data-x="${r.id}" title="Remove"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
    </label>`).join('');
  $('#flist').querySelectorAll('input').forEach(c => c.onchange = () => {
    c.checked ? S.checked.add(c.dataset.id) : S.checked.delete(c.dataset.id);
    renderFiles(); renderInfo(); loadTests(); loadLimits(); if (S.test) loadTestDetail();
    if (S.page === 'charts') { fillPairPick(); drawChart(); }
    if (S.page === 'traveller') renderTraveller();
  });
  $('#flist').querySelectorAll('[data-x]').forEach(b => b.onclick = async e => { e.preventDefault(); applyState(await api.remove_files([b.dataset.x]), true); });
}
let PREFIX = '';
function shortName(n) {
  if (!PREFIX) {
    const names = S.rows.map(r => r.name); let pre = names[0] || '';
    names.forEach(x => { while (pre && !x.startsWith(pre)) pre = pre.slice(0, -1); });
    PREFIX = pre.slice(0, pre.lastIndexOf('_') + 1); if (PREFIX.length < 8) PREFIX = '-';
  }
  return PREFIX !== '-' && n.startsWith(PREFIX) ? n.slice(PREFIX.length) : n;
}
$('#checkAll').onclick = () => { S.checked = S.checked.size === S.rows.length ? new Set() : new Set(S.rows.map(r => r.id)); applyState({ rows: S.rows, groups: S.groups }, true); };
$('#clearFiles').onclick = async () => { PREFIX = ''; S.test = null; applyState(await api.clear_files(), true); };
$('#openFiles').onclick = async () => { PREFIX = ''; openPaths(await api.pick_files()); };
$('#openFolder').onclick = async () => { PREFIX = ''; openPaths(await api.pick_folder()); };
$('#drop').onclick = async () => { PREFIX = ''; openPaths(await api.pick_folder()); };
['dragenter', 'dragover'].forEach(ev => document.addEventListener(ev, e => { e.preventDefault(); document.body.classList.add('dragging'); }));
['dragleave', 'drop'].forEach(ev => document.addEventListener(ev, e => { e.preventDefault(); if (ev === 'drop' || !e.relatedTarget) document.body.classList.remove('dragging'); }));
window.onDropPaths = paths => {
  document.body.classList.remove('dragging'); PREFIX = '';
  if (S.page === 'prod' && S.lot) { lotAdd(paths); return; }
  if (S.page === 'prod') { newLot(paths); return; }
  showPage('info'); openPaths(paths);
};

/* ---------------- detailed info ---------------- */
function renderInfo() {
  const has = S.rows.length > 0;
  $('#drop').classList.toggle('hidden', has); $('#infoBody').classList.toggle('hidden', !has);
  if (!has) return;
  const rows = S.rows.filter(r => S.checked.has(r.id));
  const tot = rows.reduce((a, r) => (a.runs += r.runs, a.dev += r.devices, a.ret += r.retested, a.p += r.passed, a.f += r.failed, a), { runs: 0, dev: 0, ret: 0, p: 0, f: 0 });
  $('#ftable').innerHTML = `<tr><th>#</th><th class="l">File</th><th class="l">Area</th><th class="l">Stage</th><th class="l">Start</th>
    <th title="Insertions in the file, retests included">Tests run</th><th title="Physical chips in the file">Unique devices</th>
    <th title="Chips tested more than once in the file">Retested</th><th>Passed</th><th>Failed</th><th>Yield %</th>
    <th class="l">Lot ID</th><th class="l">Job name</th><th class="l">Tester</th><th>Sites</th></tr>` +
    rows.map((r, i) => `<tr class="${r.note ? 'dimrow' : ''}">
      <td class="dim">${i + 1}</td><td class="l name" title="${esc(r.name)}">${esc(shortName(r.name))}</td>
      <td class="l">${chip(r.area)}</td><td class="l">${r.note ? '<span class="tag">sanity · left out</span>' : esc(r.stage)}</td>
      <td class="l dim">${esc(r.start.slice(5, 16))}</td><td>${num(r.runs)}</td>
      <td><b>${num(r.devices)}</b>${r.repeats ? `<span class="tag" title="Same chip inserted again under another Part ID">+${r.repeats}</span>` : ''}</td>
      <td class="${r.retested ? '' : 'dim'}">${num(r.retested)}</td><td class="pass">${num(r.passed)}</td><td class="${r.failed ? 'fail' : 'dim'}">${num(r.failed)}</td>
      <td>${pct(r.yield_)}<span class="ybar"><i style="width:${100 * r.yield_}%"></i></span></td>
      <td class="l dim clip" title="${esc(r.lot)}">${esc(r.lot)}</td><td class="l dim clip" title="${esc(r.job)}">${esc(r.job)}</td>
      <td class="l dim">${esc(r.tester)}</td><td>${r.sites}</td></tr>`).join('') +
    (rows.length > 1 ? `<tr class="sum"><td></td><td class="l">Total (${rows.length} files)</td><td colspan="3"></td><td>${num(tot.runs)}</td><td>${num(tot.dev)}</td><td>${num(tot.ret)}</td>
      <td class="pass">${num(tot.p)}</td><td class="fail">${num(tot.f)}</td><td>${tot.dev ? pct(tot.p / tot.dev) : ''}</td><td colspan="4"></td></tr>` : '');
  $('#lotStrip').innerHTML = S.groups.map(g => g.error
    ? `<span class="chip flag-warn" title="${esc(g.error)}">${esc(g.key)} · ${esc(g.error.slice(0, 60))}</span>`
    : `<span class="chip">${esc(g.key)}${g.device && g.device !== '?' ? ' · ' + esc(g.device) : ''}</span>${g.warn ? `<span class="chip flag-warn" title="${esc(g.warn)}">lot names differ</span>` : ''}
       <span class="chip">${num(g.summary.lot_size)} chips</span><span class="chip flag-ok">${num(g.summary.good)} good · ${pct(g.summary.yield_)}</span>
       ${g.summary.hold ? `<span class="chip flag-warn">${g.summary.hold} on hold</span>` : ''}
       ${(g.summary.gaps || []).length ? `<button class="chip flag-warn chip-btn" data-gaps="${esc(g.key)}" title="Chips that skipped an insertion or were tested after failing - click for the list">${g.summary.gaps.length} off the test flow</button>` : ''}`).join('<span class="sep"></span>');
  $$('#lotStrip [data-gaps]').forEach(b => b.onclick = () => {
    const g = S.groups.find(x => x.key === b.dataset.gaps), box = $('#gapBox');
    const open = !box.classList.contains('hidden') && box.dataset.key === g.key;
    box.dataset.key = g.key; box.innerHTML = open ? '' : gapsList(g.summary.gaps); box.classList.toggle('hidden', open);
  });
  if (!S.groups.some(g => (g.summary?.gaps || []).length)) $('#gapBox').classList.add('hidden');
  const areas = new Set(rows.map(r => r.area));
  $('#area').querySelectorAll('button').forEach(b => b.disabled = b.dataset.v !== 'all' && !areas.has(AREA[b.dataset.v]));
  if (S.area !== 'all' && !areas.has(AREA[S.area])) { S.area = 'all'; setSeg('#area', 'all'); }
  $('#go').disabled = !rows.length || S.busy;
  requestAnimationFrame(() => placePill('#area'));
}

/* ---------------- tests ---------------- */
function filters() {
  return { q: $('#tsearch').value, pf: $('#fpf').value, cp_op: $('#fcpop').value, cp: $('#fcp').value, cpk_op: $('#fcpkop').value, cpk: $('#fcpk').value };
}
let tTimer = null;
function loadTests() { clearTimeout(tTimer); tTimer = setTimeout(_loadTests, 120); }
async function _loadTests() {
  const ids = checkedIds();
  if (!ids.length) { S.tests = []; $('#tlist').innerHTML = '<div class="empty sm">Check a file to list its tests.</div>'; $('#tcount').textContent = ''; return; }
  const r = await api.tests(ids, filters());
  S.tests = r.tests;
  $('#tcount').textContent = `· ${num(r.total)}`;
  $('#tlist').innerHTML = r.tests.map(t => `<div class="titem${S.test === t.num ? ' sel' : ''}" data-t="${t.num}" title="${esc(t.name)}">
    <span class="tnum">${t.num}</span><span class="tname">${esc(t.name)}</span>${t.fails ? `<span class="tfail">${t.fails}</span>` : ''}</div>`).join('') || '<div class="empty sm">No test matches.</div>';
  $('#tlist').querySelectorAll('.titem').forEach(el => el.onclick = () => selectTest(+el.dataset.t));
}
function selectTest(tn) {
  S.test = tn;
  $('#limTbl').querySelectorAll('tr.lrow').forEach(el => el.classList.toggle('sel', +el.dataset.t === tn));
  $('#tlist').querySelectorAll('.titem').forEach(el => el.classList.toggle('sel', +el.dataset.t === tn));
  loadTestDetail();
  if (S.page === 'charts') drawChart();
}
['#tsearch', '#fcp', '#fcpk'].forEach(s => $(s).oninput = loadTests);
['#fpf', '#fcpop', '#fcpkop'].forEach(s => $(s).onchange = loadTests);
$('#fclear').onclick = () => { $('#tsearch').value = ''; $('#fpf').value = 'any'; $('#fcp').value = ''; $('#fcpk').value = ''; loadTests(); };
async function loadTestDetail() {
  if (!S.test) return;
  const d = await api.test_detail(checkedIds(), S.test);
  if (!d) { $('#testBody').innerHTML = '<div class="empty">This test is not in the checked files.</div>'; return; }
  const t = d.test, u = t.units ? ` ${esc(t.units)}` : '';
  $('#testName').textContent = `· ${S.test} ${t.name}`;
  const lim = x => `${x.lo == null ? '–' : fx(x.lo)} … ${x.hi == null ? '–' : fx(x.hi)}${u}`;
  const limChips = (d.limits || []).map(x => `<span class="chip${d.same ? '' : ' flag-warn'}">${d.same ? 'Limits' : x.areas.map(a => SHORT[a] || a).join(' · ')} ${lim(x)}</span>`).join('');
  $('#testBody').innerHTML = `<div class="row wrap" style="gap:8px;margin-bottom:12px">
      ${limChips}
      <span class="chip ${d.total_fails ? 'flag-warn' : 'flag-ok'}">${d.total_fails} failing run${d.total_fails === 1 ? '' : 's'}</span></div>
    <div class="tbl-wrap"><table class="tbl"><tr><th class="l">Area</th><th class="l">File</th><th>Low</th><th>High</th><th>N</th><th>Fails</th><th>Min</th><th>Max</th><th>Mean</th><th>Std dev</th><th>Cp</th><th>Cpk</th></tr>
    ${d.rows.map(r => `<tr><td class="l">${chip(r.area)}</td><td class="l name fcl" title="${esc(r.file)}${r.job ? '\n' + esc(r.job) : ''}">${esc(shortName(r.file))}</td>
      <td class="${r.differs ? 'ldiff' : ''}">${fx(r.lo)}</td><td class="${r.differs ? 'ldiff' : ''}">${fx(r.hi)}</td><td>${r.n}</td>
      <td class="${r.fails ? 'fail' : 'dim'}">${r.fails}</td><td>${fx(r.min)}</td><td>${fx(r.max)}</td><td>${fx(r.mean)}</td><td>${fx(r.sd)}</td>
      <td class="${r.cp != null && r.cp < 1.33 ? 'warn' : ''}">${fx(r.cp, 2)}</td><td class="${r.cpk != null && r.cpk < 1.33 ? 'warn' : ''}">${fx(r.cpk, 2)}</td></tr>`).join('')}
    ${d.rows.length > 1 ? `<tr class="sum"><td class="l" colspan="4">All checked files${d.same ? '' : ' (limits differ - no Cp/Cpk)'}</td><td>${d.total.n}</td><td>${d.total_fails}</td><td>${fx(d.total.min)}</td><td>${fx(d.total.max)}</td>
      <td>${fx(d.total.mean)}</td><td>${fx(d.total.sd)}</td><td>${fx(d.total.cp, 2)}</td><td>${fx(d.total.cpk, 2)}</td></tr>` : ''}</table></div>
    <div class="row" style="gap:8px;margin-top:12px"><button class="btn-ghost sm" id="toTrend">Trend chart</button><button class="btn-ghost sm" id="toCorr">Correlation</button><button class="btn-ghost sm" id="toDelta">Delta</button></div>`;
  $('#toTrend').onclick = () => { S.chartKind = 'trend'; setSeg('#chartKind', 'trend'); showPage('charts'); };
  $('#toCorr').onclick = () => { S.chartKind = 'corr'; setSeg('#chartKind', 'corr'); showPage('charts'); };
  $('#toDelta').onclick = () => { S.chartKind = 'delta'; setSeg('#chartKind', 'delta'); showPage('charts'); };
}

/* ---------------- test limits ---------------- */
let limTimer = null, LIM = null;
function loadLimits() { clearTimeout(limTimer); limTimer = setTimeout(_loadLimits, 150); }
async function _loadLimits() {
  const ids = checkedIds();
  if (!ids.length) { LIM = null; $('#limTbl').innerHTML = ''; $('#limNote').textContent = ''; return; }
  const d = await api.limits(ids, $('#limQ').value, $('#limF').value);
  LIM = d;
  const A = d.areas, MAX = 1500;
  $('#limNote').textContent = `· ${num(d.total)} test${d.total === 1 ? '' : 's'} · ${num(d.differ)} differ between areas · ${num(d.tighter)} tighter than ${SHORT[A[0]] || A[0] || 'Room'}` + (d.total > MAX ? ` · first ${num(MAX)} shown, use the search` : '');
  const cell = (c, a, r) => {
    if (!c) return '<td class="dim b">–</td><td class="dim">–</td>';
    if (c.mixed) { const t = c.mixed.map(x => `${fx(x.lo)} … ${fx(x.hi)}`).join('\n'); return `<td class="ldiff b" title="${esc(t)}">varies</td><td class="ldiff" title="${esc(t)}">varies</td>`; }
    const t = r.tight[a] || '', kl = t.includes('lo') ? 'tight' : '', kh = t.includes('hi') ? 'tight' : '';
    return `<td class="b ${kl}">${c.lo == null ? '–' : fx(c.lo)}</td><td class="${kh}">${c.hi == null ? '–' : fx(c.hi)}</td>`;
  };
  $('#limTbl').innerHTML = `<tr><th rowspan="2">Test</th><th class="l" rowspan="2">Name</th><th class="l" rowspan="2">Units</th>${A.map(a => `<th class="grp" colspan="2">${esc(SHORT[a] || a)}</th>`).join('')}</tr>
    <tr>${A.map(() => '<th class="b">Low</th><th>High</th>').join('')}</tr>` +
    (d.rows.slice(0, MAX).map(r => `<tr class="lrow${S.test === r.num ? ' sel' : ''}" data-t="${r.num}"><td>${r.num}</td><td class="l name" title="${esc(r.name)}">${esc(r.name)}</td><td class="l">${esc(r.units || '')}</td>
      ${A.map(a => cell(r.cells[a], a, r)).join('')}</tr>`).join('') || `<tr><td class="l dim" colspan="${3 + 2 * A.length}">No test matches.</td></tr>`);
  $('#limTbl').querySelectorAll('tr.lrow').forEach(el => el.onclick = () => selectTest(+el.dataset.t));
}
$('#limQ').oninput = loadLimits;
$('#limF').onchange = loadLimits;
$('#limCopy').onclick = () => {
  if (!LIM || !LIM.rows.length) return;
  const A = LIM.areas, v = x => x == null ? '' : x;
  const head = ['Test', 'Name', 'Units', ...A.flatMap(a => [(SHORT[a] || a) + ' Low', (SHORT[a] || a) + ' High'])].join('\t');
  const body = LIM.rows.map(r => [r.num, r.name, r.units || '', ...A.flatMap(a => { const c = r.cells[a];
    return !c ? ['', ''] : c.mixed ? ['varies', 'varies'] : [v(c.lo), v(c.hi)]; })].join('\t'));
  copy([head, ...body].join('\n'), `${num(LIM.rows.length)} rows of limits - paste into WPS`);
};

/* ---------------- report ---------------- */
function setOutput(v) {
  S.output = v || ''; $('#outTxt').textContent = v ? base(v) : 'Automatic - next to the STDF files';
  $('#outTxt').title = v || ''; $('#outTxt').classList.toggle('placeholder', !v); $('#outClear').classList.toggle('hidden', !v);
}
$('#outPick').onclick = async () => { const p = await api.pick_output(); if (p) setOutput(p); };
$('#outClear').onclick = () => setOutput('');
function setStatus(t, spinning) { $('#status').innerHTML = (spinning ? '<span class="spin"></span>' : '') + esc(t); }
function log(s) { const l = $('#log'); l.textContent += (l.textContent ? '\n' : '') + s; l.scrollTop = l.scrollHeight; }
async function startReport(params) {
  if (S.busy) return;
  S.busy = true; $('#go').disabled = true;
  $('#log').textContent = ''; $('#progress').classList.remove('hidden'); setStatus('Working…', true);
  const r = await api.generate(params);
  if (!r || !r.ok) { finish(); setStatus('Ready'); toast((r && r.error) || 'Could not start', true); }
}
$('#go').onclick = () => startReport({ ids: checkedIds(), area: S.area, output: S.output, file_sheets: $('#fileSheets').checked });
$('#fileSheets').onchange = () => api.set_option('file_sheets', $('#fileSheets').checked);
function finish() { S.busy = false; $('#progress').classList.add('hidden'); $('#go').disabled = !S.checked.size; }
window.onLog = s => log(s);
window.onError = msg => { finish(); setStatus('Failed'); log('ERROR: ' + msg); toast(msg, true); };
window.onDone = sum => {
  finish(); setStatus('Done');
  S.sum = sum; S.outputs = { [sum.area]: sum.output };
  S.recent.unshift({ when: new Date().toISOString().slice(0, 16).replace('T', ' '), lot: sum.lot, area: sum.area, output: sum.output, files: sum.files.filter(f => f.used).length, good: sum.good, lot_size: sum.lot_size });
  $('#tabResult').disabled = false; showPage('result'); setView(sum.area);
};

/* ---------------- charts ---------------- */
function fillPairPick() {
  const rows = S.rows.filter(r => S.checked.has(r.id) && !r.note);
  const areas = FLOW.filter(a => rows.some(r => r.area === a));
  const opts = areas.map(a => `<option value="area:${a}">${LABEL[a]} · all files</option>`).join('') +
    rows.map(r => `<option value="file:${r.id}">${SHORT[r.area] || r.area} · ${esc(shortName(r.name))}</option>`).join('');
  const a = $('#selA').value, b = $('#selB').value;
  $('#selA').innerHTML = opts; $('#selB').innerHTML = opts;
  const vals = [...$('#selA').options].map(o => o.value);
  $('#selA').value = vals.includes(a) ? a : (vals[0] || '');
  $('#selB').value = vals.includes(b) ? b : (vals[1] || vals[0] || '');
}
['#selA', '#selB'].forEach(s => $(s).onchange = drawChart);
const SER = ['#4ade80', '#60a5fa', '#f472b6', '#facc15', '#a78bfa', '#fb923c', '#2dd4bf', '#f87171', '#94a3b8', '#c084fc'];
function baseOpts(xTitle, yTitle) {
  const fg = css('--muted'), grid = `rgb(${css('--ink')} / 0.08)`;
  return {
    responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
    plugins: { legend: { labels: { color: fg, boxWidth: 10, usePointStyle: true } }, tooltip: { mode: 'nearest', intersect: true } },
    scales: {
      x: { type: 'linear', title: { display: !!xTitle, text: xTitle, color: fg }, ticks: { color: fg }, grid: { color: grid } },
      y: { type: 'linear', title: { display: !!yTitle, text: yTitle, color: fg }, ticks: { color: fg }, grid: { color: grid } }
    }
  };
}
function limitLine(v, x0, x1, label, color) {
  return { type: 'line', label, data: [{ x: x0, y: v }, { x: x1, y: v }], borderColor: color, borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, order: 0 };
}
let drawTok = 0;
function newChart(cfg) { const c = $('#chart'); const old = Chart.getChart(c); if (old) old.destroy(); chart = new Chart(c, cfg); }
async function drawChart() {
  if (S.page !== 'charts') return;
  const tok = ++drawTok;
  $('#pairPick').classList.toggle('hidden', S.chartKind === 'trend');
  if (chart) { chart.destroy(); chart = null; }
  $('#chartStats').innerHTML = ''; $('#chartTitle').textContent = '';
  const empty = msg => { $('#chartEmpty').textContent = msg; $('#chartEmpty').classList.remove('hidden'); };
  $('#chartEmpty').classList.add('hidden');
  if (!S.rows.length) return empty('Open STDF files first.');
  if (!S.test) return empty('Pick a test on the left.');
  const bad = rgbv('--bad');
  if (S.chartKind === 'trend') {
    const d = await api.trend(checkedIds(), S.test);
    if (tok !== drawTok) return;
    if (!d.test) return empty('This test is not in the checked files.');
    const u = d.test.units ? ` (${d.test.units})` : '';
    $('#chartTitle').textContent = `${S.test} · ${d.test.name}`;
    let off = 0; const ds = [], lims = [];
    d.series.forEach((s, i) => {
      if (!s.points.length) return;
      const pts = s.points.map((p, k) => ({ x: off + k + 1, y: p.y, pid: p.pid, hb: p.hb, fail: p.fail }));
      const col = SER[i % SER.length];
      ds.push({ type: 'line', label: `${SHORT[s.area] || s.area} · ${s.start}`, file: s.file, data: pts, borderColor: col, backgroundColor: col,
        borderWidth: 1.2, pointRadius: pts.length > 400 ? 1.5 : 2.5, pointBackgroundColor: pts.map(p => p.fail ? bad : col), tension: 0 });
      lims.push({ x0: off + 0.5, x1: off + s.points.length + 0.5, lo: s.lo, hi: s.hi });
      off += s.points.length;
    });
    // limit lines per file (limits differ between temperatures); joined where neighbouring files share them
    const segs = [];
    lims.forEach(l => { const last = segs[segs.length - 1]; if (last && last.lo === l.lo && last.hi === l.hi) last.x1 = l.x1; else segs.push({ ...l }); });
    const loPts = [], hiPts = [];
    segs.forEach(g => { if (g.lo != null) loPts.push({ x: g.x0, y: g.lo }, { x: g.x1, y: g.lo }, { x: g.x1, y: null });
                        if (g.hi != null) hiPts.push({ x: g.x0, y: g.hi }, { x: g.x1, y: g.hi }, { x: g.x1, y: null }); });
    if (loPts.length) ds.push({ type: 'line', label: 'Low limit', data: loPts, borderColor: bad, borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, spanGaps: false });
    if (hiPts.length) ds.push({ type: 'line', label: 'High limit', data: hiPts, borderColor: bad, borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0, spanGaps: false });
    const o = baseOpts('Run (files side by side)', 'Value' + u);
    o.plugins.tooltip.callbacks = { title: c => c[0] && c[0].dataset.file ? shortName(c[0].dataset.file) : '', label: c => c.raw.pid !== undefined ? `PID ${c.raw.pid} · ${fx(c.raw.y)}${u} · HB ${c.raw.hb}${c.raw.fail ? ' · FAIL' : ''}` : `${c.dataset.label} ${fx(c.raw.y)}` };
    o.plugins.legend.position = ds.length > 8 ? 'right' : 'top';
    newChart({ type: 'line', data: { datasets: ds }, options: o });
    const n = d.series.reduce((a, s) => a + s.points.length, 0), f = d.series.reduce((a, s) => a + s.points.filter(p => p.fail).length, 0);
    $('#chartStats').innerHTML = `<span class="chip">${num(n)} values</span><span class="chip ${f ? 'flag-warn' : 'flag-ok'}">${f} failing</span>
      ${[...new Set(d.series.filter(s => s.points.length).map(s => `${s.area}|${s.lo}|${s.hi}`))].map(k => { const [a, lo, hi] = k.split('|');
        return `<span class="chip">${esc(SHORT[a] || a)} limits ${lo === 'null' ? '–' : fx(+lo)} … ${hi === 'null' ? '–' : fx(+hi)}</span>`; }).join('')}`;
    return;
  }
  const sel = v => { const [k, x] = String(v).split(':'); return k === 'area' ? { area: x } : { file: x }; };
  if (!$('#selA').value || !$('#selB').value) return empty('Pick A and B.');
  const d = await api.pair(sel($('#selA').value), sel($('#selB').value), S.test);
  if (tok !== drawTok) return;
  if (d.error) return empty(d.error);
  if (!d.points.length) return empty('No chip has a value for this test in both A and B.');
  const t = d.test || {}, u = t.units ? ` (${t.units})` : '';
  $('#chartTitle').textContent = `${S.test} · ${t.name || ''}`;
  const tip = c => c.raw.chip !== undefined || c.raw.pid_a !== undefined
    ? `${c.raw.chip ? c.raw.chip + ' · ' : ''}A ${fx(c.raw.a)} (PID ${c.raw.pid_a}) → B ${fx(c.raw.b)} (PID ${c.raw.pid_b}) · Δ ${fx(c.raw.d)}` : `${c.dataset.label}`;
  if (S.chartKind === 'corr') {
    const pts = d.points.map(p => ({ ...p, x: p.a, y: p.b }));
    const all = pts.flatMap(p => [p.x, p.y]).concat([...(d.lim_a || []), ...(d.lim_b || [])].filter(v => v != null));
    const lo = Math.min(...all), hi = Math.max(...all);
    const ds = [{ type: 'scatter', label: 'Chips', data: pts, backgroundColor: rgbv('--accent'), pointRadius: 4, pointBackgroundColor: pts.map(p => (p.hb_a !== 1 || p.hb_b !== 1) ? bad : rgbv('--accent')) },
      { type: 'line', label: 'A = B', data: [{ x: lo, y: lo }, { x: hi, y: hi }], borderColor: css('--muted'), borderDash: [3, 3], borderWidth: 1, pointRadius: 0 }];
    const [alo, ahi] = d.lim_a || [], [blo, bhi] = d.lim_b || [];
    const vline = (x, lab) => ({ type: 'line', label: lab, data: [{ x, y: lo }, { x, y: hi }], borderColor: rgbv('--warn'), borderDash: [6, 4], borderWidth: 1.5, pointRadius: 0 });
    if (blo != null) ds.push(limitLine(blo, lo, hi, 'B low limit', bad));
    if (bhi != null) ds.push(limitLine(bhi, lo, hi, 'B high limit', bad));
    if (alo != null && alo !== blo) ds.push(vline(alo, 'A low limit'));
    if (ahi != null && ahi !== bhi) ds.push(vline(ahi, 'A high limit'));
    const o = baseOpts('A · ' + d.label_a + u, 'B · ' + d.label_b + u);
    o.plugins.tooltip.callbacks = { label: tip };
    newChart({ type: 'scatter', data: { datasets: ds }, options: o });
  } else {
    const pts = d.points.slice().sort((p, q) => p.a - q.a).map((p, i) => ({ ...p, x: i + 1, y: p.d }));
    const m = d.delta.mean, sd = d.delta.sd || 0, n = pts.length;
    const ds = [{ type: 'scatter', label: 'Δ = B − A', data: pts, pointRadius: 4, backgroundColor: rgbv('--cold'), pointBackgroundColor: pts.map(p => (p.hb_a !== 1 || p.hb_b !== 1) ? bad : rgbv('--cold')) },
      limitLine(m, 0, n + 1, 'Mean', rgbv('--accent'))];
    if (sd) { ds.push(limitLine(m + 3 * sd, 0, n + 1, '+3σ', css('--muted'))); ds.push(limitLine(m - 3 * sd, 0, n + 1, '−3σ', css('--muted'))); }
    const o = baseOpts('Chips (sorted by value in A)', 'B − A' + u);
    o.plugins.tooltip.callbacks = { label: tip };
    newChart({ type: 'scatter', data: { datasets: ds }, options: o });
  }
  $('#chartStats').innerHTML = `<span class="chip">${d.matched} chips in both</span>
    ${d.r != null ? `<span class="chip">r = ${d.r.toFixed(3)}</span>` : ''}
    <span class="chip">Δ mean ${fx(d.delta.mean)}${t.units ? ' ' + esc(t.units) : ''}</span><span class="chip">Δ σ ${fx(d.delta.sd)}</span>
    <span class="chip">Δ range ${fx(d.delta.min)} … ${fx(d.delta.max)}</span>
    <span class="chip dim" title="How A and B were matched">matched by ${esc(d.how || 'fuse ID')}</span>
    ${d.shared ? `<span class="chip flag-warn">${d.shared} shared fuse codes skipped</span>` : ''}`;
}

/* ---------------- lot traveller ---------------- */
$('#trvRefresh').onclick = renderTraveller;
async function renderTraveller() {
  const ids = checkedIds();
  if (!ids.length) { $('#trv').innerHTML = '<div class="card"><div class="empty">Open and check the STDF files of a lot.</div></div>'; return; }
  $('#trv').innerHTML = '<div class="card"><div class="empty"><span class="spin"></span> Counting chips…</div></div>';
  const lots = await api.traveller(ids);
  $('#trv').innerHTML = lots.map(lot => lot.error ? `<div class="card"><div class="empty">${esc(lot.lot)}: ${esc(lot.error)}</div></div>` : `
    <div class="lot-head"><span class="chip">${esc(lot.lot)}</span>${lot.device && lot.device !== '?' ? `<span class="chip">${esc(lot.device)}</span>` : ''}</div>
    ${lot.areas.map(a => `<div class="card trv-area">
      <div class="card-head"><div class="card-title">${chip(a.area, a.label)}</div>
        <div class="row wrap" style="gap:6px">${a.totals.map(t => `<span class="chip">${esc(t.sheet.split(' - ')[0])}: ${t.lot_size} / ${t.passed} / ${t.failed}</span>`).join('')}</div></div>
      ${a.sheets.map(sh => `<div class="trv-sheet"><div class="trv-sheet-title">${esc(sh.name)}</div>
        <div class="trv-blocks">${sh.blocks.map((b, i) => block(b, i)).join('')}</div></div>`).join('')}
    </div>`).join('')}
    ${lot.left_out.length ? `<div class="card"><div class="card-title" style="margin-bottom:8px">Left out</div>${lot.left_out.map(x => `<div class="dim small">${esc(x.file)} — ${esc(x.why)}</div>`).join('')}</div>` : ''}`).join('');
}
function block(b, i) {
  const binsTsv = b.bins.map(n => n || '').join('\t');
  const other = Object.entries(b.other || {}).map(([k, n]) => `HB ${k}: ${n}`).join(', ');
  const row = (lab, val, show) => `<tr><td class="l dim">${lab}</td><td class="l">${show ?? esc(val)}</td><td>${cbtn(val, lab)}</td></tr>`;
  return `<div class="trv-block${b.rerun_later ? ' rerun' : ''}">
    <div class="trv-bhead"><span>${i + 1}.</span>${b.rerun_later ? '<span class="tag" title="Every chip of this file was tested again in a later initial-test file">re-run later · not on traveller</span>' : ''}
      ${b.repeats ? `<span class="tag" title="Chip inserted again in this file">${b.repeats} repeat insertion${b.repeats > 1 ? 's' : ''}</span>` : ''}</div>
    <table class="kv">
      ${row('File name', b.file, `<span class="name" title="${esc(b.file)}">${esc(shortName(b.file))}</span>`)}
      ${row('Start', b.start)}${row('Stop', b.stop)}
      ${row('Lot size', b.lot_size, `<b>${b.lot_size}</b> <span class="dim">(${b.runs} runs)</span>`)}
      ${row('Pass', b.passed, `<span class="pass">${b.passed}</span>`)}${row('Fail', b.failed, `<span class="${b.failed ? 'fail' : ''}">${b.failed}</span>`)}
    </table>
    <div class="binrow">${b.bins.map((n, k) => `<div class="bincell${k === 0 ? ' b1' : n ? ' bf' : ''}"><span>Bin ${k + 1}</span><b>${n || ''}</b></div>`).join('')}</div>
    ${other ? `<div class="hint">Other bins: ${esc(other)}</div>` : ''}
    <div class="row" style="gap:6px;margin-top:8px">
      <button class="btn-ghost sm" data-copy="${esc(binsTsv)}" data-what="bins 1–9">${ICON_COPY}Copy bins</button>
      <button class="btn-ghost sm" data-copy="${esc([b.file, b.start, b.stop, b.lot_size, b.passed, b.failed].join('\n'))}" data-what="block">${ICON_COPY}Copy block</button>
    </div></div>`;
}

/* ---------------- results ---------------- */
function setView(v) {
  const s = S.sum; if (!s) return;
  if (v !== 'all' && !s.areas[AREA[v]]) v = 'all';
  S.view = v;
  $('#view').querySelectorAll('button').forEach(b => b.disabled = b.dataset.v !== 'all' && !s.areas[AREA[b.dataset.v]]);
  setSeg('#view', v); renderResult();
}
function tile(cls, cap, val, of, meter, extra = '') {
  return `<div class="tile kpi ${cls}"><div class="stage"><span class="cap" style="margin:0">${esc(cap)}</span>${extra}</div>
    <div><span class="num tabular">${val}</span>${of ? ` <span class="of tabular">${of}</span>` : ''}</div>
    ${meter != null ? `<div class="meter"><i style="width:${Math.max(0, Math.min(100, meter))}%"></i></div>` : ''}</div>`;
}
function areaTile(t) {
  const a = S.sum.areas[t];
  return `<div class="tile kpi" data-go="${KEY[t]}" style="cursor:pointer">
    <div class="stage">${chip(t)}<span style="color:var(--faint);font-size:13px">${esc(span(a.first, a.last))}</span></div>
    <div><span class="num tabular">${num(a.good)}</span> <span class="of tabular">/ ${num(a.parts)}</span></div>
    <div class="cap">good at ${esc(SHORT[t])} · ${pct(a.yield, 1)}${t !== S.sum.flow[0] ? ` · ${num(a.lost)} lost` : ''}${a.hold ? ` · ${a.hold} on hold` : ''}</div>
    <div class="meter"><i style="width:${100 * a.yield}%"></i></div></div>`;
}
function renderResult() {
  const s = S.sum, t = AREA[S.view];
  $('#resLot').textContent = `· ${s.lot ? s.lot + ' · ' : ''}${s.files.filter(f => f.used).length} files · v${s.version}`;
  $('#flow').innerHTML = s.flow.map((k, i) => `${i ? '<span class="arr">→</span>' : ''}<div class="step${k === t ? ' sel' : ''}" data-go="${KEY[k]}"><b>${chip(k)}</b><span class="when">${esc(span(s.areas[k].first, s.areas[k].last))}</span></div>`).join('');
  if (!t) {
    $('#kpis').innerHTML = tile('', 'Lot size (unique chips)', num(s.lot_size)) + tile('kpi-good', 'Final good (Bin 1)', num(s.good), '/ ' + num(s.lot_size), 100 * s.yield) +
      tile('kpi-bad', 'Scrap', num(s.scrap)) + (s.hold ? tile('kpi-warn', 'On hold - not tested', num(s.hold)) : '') + tile('', 'Final lot yield', pct(s.yield)) + s.flow.map(areaTile).join('');
  } else {
    const a = s.areas[t], first = t === s.flow[0];
    $('#kpis').innerHTML = tile('', `Tested at ${SHORT[t]}`, num(a.parts), first ? '' : `· ${num(a.linked)} linked`) + tile('kpi-good', 'Pass (Bin 1)', num(a.good), '/ ' + num(a.parts), 100 * a.yield) +
      tile('kpi-bad', 'Fail', num(a.fail)) + tile('', 'Yield', pct(a.yield)) + tile('', 'Recovered by retest', num(a.recovered)) +
      (first ? '' : tile('kpi-bad', `Good chips lost at ${SHORT[t]}`, num(a.lost))) + (a.hold ? tile('kpi-warn', 'On hold - not tested here', num(a.hold)) : '');
  }
  const out = S.outputs[S.view];
  $('#flags').innerHTML = `<span class="chip ${s.exceptions ? 'flag-warn' : 'flag-ok'}">${s.exceptions} exception line${s.exceptions === 1 ? '' : 's'} (Exceptions sheet)</span>` +
    (s.files.some(f => !f.used) ? `<span class="chip">${s.files.filter(f => !f.used).length} file(s) left out</span>` : '') + `<span class="chip">Whole lot: ${num(s.good)} / ${num(s.lot_size)} good · ${pct(s.yield)}</span>`;
  const label = S.view === 'all' ? 'all areas' : SHORT[t];
  $('#outBtns').innerHTML = out ? `<button class="btn-primary" data-p="${esc(out)}">Open ${esc(label)} report</button><button class="btn-ghost" data-f="${esc(out)}">Show in folder</button>`
    : `<button class="btn-primary" id="exportView">Export ${esc(label)} report</button>`;
  $('#outBtns').querySelectorAll('[data-p]').forEach(b => b.onclick = async () => { if (!(await api.open_path(b.dataset.p))) toast('That file is no longer there', true); });
  $('#outBtns').querySelectorAll('[data-f]').forEach(b => b.onclick = () => api.open_folder(b.dataset.f));
  const ex = $('#exportView');
  if (ex) ex.onclick = async () => {
    ex.disabled = true; ex.innerHTML = '<span class="spin"></span>Writing…';
    const r = await api.export_area(S.view);
    if (r && r.ok) { S.outputs[S.view] = r.output; toast('Report written: ' + base(r.output)); } else toast((r && r.error) || 'Could not write the report', true);
    renderResult();
  };
  $$('#page-result [data-go]').forEach(el => el.onclick = () => setView(el.dataset.go));
  $('#resGaps').innerHTML = gapsCard((s.gaps || []).filter(g => !t || g.stage === t));
  renderWaterfall(); renderBins(); renderResFiles();
}
function wfRows(stages, areas, flow, filterT) {
  const multi = flow.length > 1;
  let html = '', prev = null;
  stages.forEach((x, i) => {
    if (x.temp !== prev && (multi || filterT)) {
      const a = areas[x.temp];
      html += `<tr class="band"><td colspan="9">${chip(x.temp, LABEL[x.temp])} <span style="color:var(--faint);font-weight:500;margin-left:8px">${esc(span(a.first, a.last))} · ${a.files} file${a.files === 1 ? '' : 's'}</span></td></tr>`;
      prev = x.temp;
    }
    const sy = x.tested ? x.pass / x.tested : 0, cy = x.population ? x.cum_good / x.population : 0;
    html += `<tr><td class="l">${esc(x.name)}</td><td>${num(x.tested)}</td><td class="pass">${num(x.pass)}</td><td class="${x.fail ? 'fail' : 'dim'}">${num(x.fail)}</td><td>${pct(sy)}</td>
      <td>${num(x.cum_good)}</td><td>${pct(cy)}<span class="ybar"><i style="width:${100 * cy}%"></i></span></td><td class="${x.recovered ? 'pass' : 'dim'}">${x.recovered == null ? '-' : num(x.recovered)}</td><td class="dim">${num(x.runs)}</td></tr>`;
    if ((i === stages.length - 1 || stages[i + 1].temp !== x.temp) && multi) {
      const a = areas[x.temp];
      html += `<tr class="sum"><td class="l">${esc(LABEL[x.temp])} summary</td><td>${num(a.parts)}</td><td class="pass">${num(a.good)}</td><td class="${a.fail ? 'fail' : 'dim'}">${num(a.fail)}</td><td>${pct(a.yield)}</td>
        <td>${num(a.good)}</td><td>${pct(a.yield)}<span class="ybar"><i style="width:${100 * a.yield}%"></i></span></td><td>${num(a.recovered)}</td><td>${num(a.runs)}</td></tr>`;
    }
  });
  return `<tr><th class="l">Stage</th><th>Tested</th><th>Pass</th><th>Fail</th><th>Stage yield</th><th>Cumul. pass</th><th>Cumul. yield</th><th>Recovered</th><th>Runs</th></tr>` + html;
}
function renderWaterfall() {
  const s = S.sum, t = AREA[S.view];
  $('#wf').innerHTML = wfRows(s.stages.filter(x => !t || x.temp === t), s.areas, s.flow, t);
  $('#wfNote').textContent = t ? LABEL[t] : 'All areas';
}
function renderBins() {
  const s = S.sum, t = AREA[S.view], rows = [];
  (t ? [t] : s.flow).forEach(k => s.areas[k].fail_bins.forEach(b => rows.push({ ...b, t: k })));
  rows.sort((a, b) => b.count - a.count);
  const max = Math.max(1, ...rows.map(r => r.count));
  $('#bins').innerHTML = !rows.length ? '<div class="empty">No failing chips here.</div>' : rows.slice(0, 10).map(b => `
    <div class="bin" title="${esc(LABEL[b.t])} · HBin ${b.hb} · SBin ${b.sb}"><div class="code">${t ? `HB${b.hb}<br>SB${b.sb}` : chip(b.t)}</div>
      <div style="min-width:0"><div class="nm">${esc(b.name || 'Bin ' + b.sb)}</div>${t ? '' : `<div class="code">HB${b.hb} · SB${b.sb}</div>`}<div class="bar"><i style="width:${100 * b.count / max}%"></i></div></div>
      <div class="n">${b.count}</div></div>`).join('');
}
const niceStatus = st => { const x = String(st || '').replace('Left out: ', ''); return /^identical copy/i.test(x) ? 'Duplicate copy' : /sanity/i.test(x) ? 'Sanity check' : /exclude/i.test(x) ? 'Name on leave-out list' : x; };
function renderResFiles() {
  const s = S.sum, t = AREA[S.view], q = ($('#fileSearch').value || '').toLowerCase();
  const rows = s.files.filter(f => (!t || f.temp === t) && (!q || f.name.toLowerCase().includes(q) || (f.stage || '').toLowerCase().includes(q)));
  $('#fileCount').textContent = `· ${rows.length}`;
  $('#rtbl').innerHTML = `<tr><th class="l">Area</th><th class="l">File</th><th class="l">Stage</th><th class="l">Start</th><th>Chips</th><th>Pass</th><th>Fail</th><th>Yield</th><th class="l">Status</th></tr>` +
    rows.map(f => { const y = f.parts ? f.pass / f.parts : 0; return `<tr><td class="l">${f.used ? chip(f.temp) : chip('skip', 'Left out')}</td><td class="l name" title="${esc(f.name)}">${esc(shortName(f.name))}</td>
      <td class="l">${esc(f.stage)}</td><td class="l dim">${esc(f.start)}</td><td>${f.used ? num(f.parts) : ''}</td><td class="pass">${f.used ? num(f.pass) : ''}</td>
      <td class="${f.parts - f.pass ? 'fail' : 'dim'}">${f.used ? num(f.parts - f.pass) : ''}</td><td>${f.used ? pct(y, 1) : ''}</td><td class="l ${f.used ? 'dim' : ''}">${esc(f.used ? 'Used' : niceStatus(f.status))}</td></tr>`; }).join('');
}
$('#fileSearch').oninput = renderResFiles;

/* ---------------- production lots ---------------- */
async function loadLots() {
  S.lots = await api.lots_list();
  $('#lots').innerHTML = !S.lots.length ? '<div class="empty sm">No saved lots yet.</div>' : S.lots.map(l => `
    <div class="lotitem${S.lot === l.name ? ' sel' : ''}" data-lot="${esc(l.name)}">
      <div><div class="lname">${esc(l.name)}</div><div class="dim small">${l.files} files${l.missing ? ` · <span class="fail">${l.missing} missing</span>` : ''} · ${esc(l.updated)}</div></div>
      ${l.last ? `<div class="lres"><b>${num(l.last.good)}</b><span class="dim">/ ${num(l.last.lot_size)}</span></div>` : ''}
    </div>`).join('');
  $('#lots').querySelectorAll('.lotitem').forEach(el => el.onclick = () => openLot(el.dataset.lot));
  if (S.lot && !S.lots.some(l => l.name === S.lot)) { S.lot = null; $('#lotView').innerHTML = '<div class="card"><div class="empty">Pick a saved lot.</div></div>'; }
}
$('#lotNew').onclick = async () => { const p = await api.pick_folder(); if (p && p.length) newLot(p); };
async function newLot(paths) {
  const r = await api.lot_create('', paths);
  if (!r.ok) return toast(r.error, true);
  toast('Saved lot ' + r.name); await loadLots(); openLot(r.name);
}
async function lotAdd(paths) {
  const r = await api.lot_add(S.lot, paths);
  toast(r.added.length ? `Added ${r.added.length} file(s) to ${S.lot}` : 'Those files are already in the lot');
  await loadLots(); openLot(S.lot);
}
async function openLot(name) {
  S.lot = name;
  $$('.lotitem').forEach(el => el.classList.toggle('sel', el.dataset.lot === name));
  $('#lotView').innerHTML = '<div class="card"><div class="empty"><span class="spin"></span> Reading the lot…</div></div>';
  const d = await api.lot_summary(name);
  if (d.error) { $('#lotView').innerHTML = `<div class="card"><div class="empty">${esc(d.error)}</div></div>`; return; }
  const used = d.file_list.filter(f => f.used).length;
  $('#lotView').innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <div class="card-head">
        <div><div class="card-title" style="font-size:18px;text-transform:none;letter-spacing:0;color:var(--fg)">${esc(name)}</div>
          <div class="dim small">${used} files used · updated ${esc(d.updated)}${d.folders.length ? ' · folder ' + esc(d.folders.map(base).join(', ')) : ''}</div></div>
        <div class="row wrap" style="gap:8px">
          <button class="btn-ghost sm" id="lAddFiles">Add files</button><button class="btn-ghost sm" id="lAddFolder">Add folder</button>
          <button class="btn-ghost sm" id="lCheck" ${d.folders.length ? '' : 'disabled'} title="Look in the lot's folder for STDF files that are not in the lot yet">Check folder for new files</button>
          <button class="btn-ghost sm" id="lView">Open in viewer</button><button class="btn-ghost sm" id="lRename">Rename</button><button class="icon-btn" id="lDel" title="Forget this lot (files are not touched)"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12M9 7V4h6v3"/></svg></button>
        </div>
      </div>
      ${d.missing.length ? `<div class="chip flag-warn" style="margin-bottom:12px">${d.missing.length} file(s) can no longer be found</div>` : ''}
      <div class="kpis">
        ${tile('', 'Initial lot size', num(d.lot_size))}${tile('kpi-good', 'Final total Bin 1', num(d.good), '/ ' + num(d.lot_size), 100 * d.yield)}
        ${tile('kpi-bad', 'Final net scrap', num(d.scrap))}${d.hold ? tile('kpi-warn', 'On hold - not tested', num(d.hold)) : ''}${tile('', 'Final overall lot yield', pct(d.yield))}
        ${tile('', 'Recovery from retests', num(d.rows.filter(r => r.recovered).reduce((a, r) => a + r.recovered, 0)), 'chips')}
      </div>
      <div class="row wrap" style="gap:10px;margin-top:14px;align-items:center">
        <div class="segmented seg-area" id="lArea"><div class="pill"></div>
          <button data-v="all" class="on">All</button><button data-v="room">Room</button><button data-v="postbake">Post-Bake</button><button data-v="hot">Hot</button><button data-v="cold">Cold</button></div>
        <button class="btn-primary" id="lReport">Report (Excel + Results)</button>
      </div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-head"><div class="card-title">Multi-stage recovery waterfall</div></div>
      <div class="tbl-wrap"><table class="tbl prodtbl">${prodRows(d)}</table></div>
    </div>
    ${gapsCard(d.gaps)}
    <div class="card">
      <div class="card-head"><div class="card-title">Files <span class="title-sub">· drop the newest STDF anywhere on this page to add it</span></div></div>
      <div class="tbl-wrap" style="max-height:420px"><table class="tbl"><tr><th class="l">Area</th><th class="l">File</th><th class="l">Stage</th><th class="l">Start</th><th class="l">Status</th><th></th></tr>
      ${d.file_list.map(f => `<tr><td class="l">${f.used ? chip(f.area) : chip('skip', 'Left out')}</td><td class="l name" title="${esc(f.path)}">${esc(f.name)}</td><td class="l">${esc(f.stage)}</td>
        <td class="l dim">${esc(f.start)}</td><td class="l dim">${esc(f.used ? 'Used' : niceStatus(f.status))}</td>
        <td><button class="icon-btn" data-rm="${esc(f.path)}" title="Remove from lot"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button></td></tr>`).join('')}
      </table></div>
    </div>`;
  let lArea = 'all';
  const areas = new Set(d.flow);
  $('#lArea').querySelectorAll('button').forEach(b => { b.disabled = b.dataset.v !== 'all' && !areas.has(AREA[b.dataset.v]); b.onclick = () => { lArea = b.dataset.v; setSeg('#lArea', lArea); }; });
  requestAnimationFrame(() => placePill('#lArea'));
  $('#lReport').onclick = async () => {
    $('#log').textContent = ''; S.busy = true; setStatus('Working…', true);
    const r = await api.lot_report(name, lArea);
    if (!r.ok) { S.busy = false; toast(r.error, true); } else toast('Writing the report…');
  };
  $('#lAddFiles').onclick = async () => { const p = await api.pick_files(); if (p.length) lotAdd(p); };
  $('#lAddFolder').onclick = async () => { const p = await api.pick_folder(); if (p.length) lotAdd(p); };
  $('#lCheck').onclick = async () => { const r = await api.lot_check_folder(name); toast(r.added.length ? `Added ${r.added.length} new file(s)` : 'No new files in the folder'); await loadLots(); openLot(name); };
  $('#lView').onclick = async () => { PREFIX = ''; await api.clear_files(); S.test = null; showPage('info'); openPaths(await api.lot_paths(name)); };
  $('#lDel').onclick = async () => { if (!confirmBox(`Forget lot ${name}? Its STDF files are not touched.`)) return; await api.lot_delete(name); S.lot = null; loadLots(); $('#lotView').innerHTML = '<div class="card"><div class="empty">Lot removed.</div></div>'; };
  $('#lRename').onclick = async () => { const n = window.prompt('New name for this lot', name); if (!n) return; const r = await api.lot_rename(name, n);
    if (!r.ok) return toast(r.error, true); S.lot = r.name; await loadLots(); openLot(r.name); };
  $('#lotView').querySelectorAll('[data-rm]').forEach(b => b.onclick = async () => { await api.lot_remove_file(name, b.dataset.rm); openLot(name); loadLots(); });
}
function confirmBox(msg) { return window.confirm(msg); }
function prodRows(d) {
  let html = `<tr><th class="l">Stage name</th><th>Tested count</th><th>Pass (Bin 1)</th><th>Fail count</th><th>Stage yield</th><th>Cumulative Bin 1</th><th>Cumulative lot yield</th><th>Yield gain</th></tr>`;
  let prev = null;
  d.rows.forEach((r, i) => {
    if (r.area !== prev && d.flow.length > 1) {
      const a = d.areas[r.area];
      html += `<tr class="band"><td colspan="8">${chip(r.area, LABEL[r.area])} <span style="color:var(--faint);font-weight:500;margin-left:8px">${esc(span(a.first, a.last))} · ${a.files} file${a.files === 1 ? '' : 's'}</span></td></tr>`;
      prev = r.area;
    }
    html += `<tr><td class="l">${esc(r.stage)}</td><td>${num(r.tested)}</td><td class="pass">${num(r.passed)}</td><td class="${r.failed ? 'fail' : 'dim'}">${num(r.failed)}</td>
      <td>${pct(r.stage_yield)}</td><td>${num(r.cum_good)}</td><td>${pct(r.cum_yield)}</td>
      <td class="${r.gain ? 'pass' : 'dim'}">${r.gain == null ? '-' : '+' + pct(r.gain)}</td></tr>`;
    if ((i === d.rows.length - 1 || d.rows[i + 1].area !== r.area) && d.flow.length > 1) {
      const a = d.areas[r.area];
      html += `<tr class="sum"><td class="l">${esc(LABEL[r.area])} summary</td><td>${num(a.parts)}</td><td class="pass">${num(a.good)}</td><td class="${a.fail ? 'fail' : 'dim'}">${num(a.fail)}</td>
        <td>${pct(a.yield)}</td><td>${num(a.good)}</td><td>${pct(a.yield)}</td><td></td></tr>`;
    }
  });
  return html;
}

/* ---------------- chips off the test flow ---------------- */
function gapsList(gaps) {
  if (!gaps || !gaps.length) return '';
  // group by kind of problem: the explanation once, then one short line per chip
  const groups = [];
  gaps.forEach(g => { let x = groups.find(y => y.title === g.title); if (!x) groups.push(x = { title: g.title, kind: g.kind, items: [] }); x.items.push(g); });
  const generic = g => g.kind === 'missing' && !g.later.length;
  return `<div class="gaps"><div class="gaps-head">Chips that did not follow the test flow · ${gaps.length} <span class="title-sub">· also in the report's Exceptions sheet</span></div>
    ${groups.map(x => `<div class="gap ${x.kind}">
      <div class="gap-top"><span class="chip ${x.kind === 'missing' ? 'flag-warn' : ''}">${esc(x.title)}</span><b>${x.items.length} chip${x.items.length > 1 ? 's' : ''}</b></div>
      ${generic(x.items[0]) ? `<div class="gap-txt">${esc(x.items[0].detail)}</div>` : ''}
      <div class="gap-list">${x.items.map(g => `<div class="gap-row"><b>${esc(g.parts)}</b><span class="dim clip" title="${esc(g.file)}">${esc(shortName(g.file))}</span>
        ${generic(g) ? '' : `<div class="gap-txt">${esc(g.detail)}</div>`}</div>`).join('')}</div></div>`).join('')}</div>`;
}
function gapsCard(gaps) {
  return gaps && gaps.length ? `<div class="card" style="margin-bottom:16px">${gapsList(gaps)}</div>` : '';
}

/* ---------------- device profile ---------------- */
S.profile = ''; S.profiles = [];
async function renderProfile() {
  $('#profName').textContent = S.profile || 'Profile';
  $('#profList').innerHTML = S.profiles.map(n => `<button class="prof-item${n === S.profile ? ' on' : ''}" data-prof="${esc(n)}">
    <span class="tick">${n === S.profile ? '✓' : ''}</span><span>${esc(n)}</span></button>`).join('');
  $('#profList').querySelectorAll('[data-prof]').forEach(b => b.onclick = () => selectProfile(b.dataset.prof));
  const d = await api.profile_details();
  $('#profBtn').classList.toggle('warn', !d.has_fuse_id);
  $('#profInfo').innerHTML = d.has_fuse_id
    ? `Fuse ID from <b>${esc(d.readback_test || d.fuse_bits_prefix)}</b>${d.match_tests.length ? ` · ${d.match_tests.length} matching tests` : ''}${d.device_regex ? ' · device versions split' : ''}.`
    : `<b>No fuse ID set.</b> Chips can only be followed by Part ID inside a file, so repeats across files are not found. Import your device profile.`;
}
async function selectProfile(name) {
  $('#profPop').classList.add('hidden');
  if (name === S.profile) return;
  toast('Switching profile – re-reading the loaded files…');
  const r = await api.profile_select(name);
  if (!r.ok) { toast(r.error, true); return; }
  S.profile = r.profile; PREFIX = ''; S.test = null; renderProfile();
  applyState(r.state); toast('Profile: ' + r.profile);
}
$('#profBtn').onclick = e => { e.stopPropagation(); $('#profPop').classList.toggle('hidden'); };
$('#profPop').onclick = e => e.stopPropagation();
document.addEventListener('click', () => $('#profPop').classList.add('hidden'));
$('#profImport').onclick = async () => {
  const r = await api.profile_import();
  if (!r.ok) { if (r.error) toast(r.error, true); return; }
  S.profiles = r.profiles; S.profile = r.profile; renderProfile(); applyState(r.state); toast('Profile imported: ' + r.profile);
};

/* ---------------- start ---------------- */
async function start() {
  const info = await api.app_info();
  S.profile = info.profile; S.profiles = info.profiles || []; renderProfile();
  $('#fileSheets').checked = info.file_sheets !== false;
  if (info.profile_error) toast(info.profile_error, true);
  S.recent = info.recent || [];
  applyTheme(info.theme || 'dark');
  $('#subtitle').textContent = `Room → Post-Bake → Hot → Cold · v${info.version}`;
  renderFiles(); renderInfo(); showPage('info');
}
if (window.pywebview && window.pywebview.api) { api = window.pywebview.api; start(); }
else window.addEventListener('pywebviewready', () => { api = window.pywebview.api; start(); });

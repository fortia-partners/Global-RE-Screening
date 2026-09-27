// node test_render.js — exercises every column set headlessly
const fs = require("fs");
let js = fs.readFileSync("index.html", "utf8")
  .split("<script>")[1].split("</script>")[0]
  .replace(/if\(checkGate\(\)\)\{ boot\(\); \} else \{ showGate\(\); \}\s*$/, "");
global.DB0 = JSON.parse(fs.readFileSync("data/snapshot.json", "utf8"));

const store = {};
const mk = id => ({
  id, innerHTML: "", textContent: "", hidden: false, value: "0", dataset: {}, style: {},
  classList: { add() {}, remove() {}, toggle() {}, contains() { return false } },
  setAttribute(k, v) { this[k] = v }, getAttribute() { return "true" },
  removeAttribute() {}, addEventListener() {},
  insertAdjacentHTML(p, s) { this.innerHTML += s },
  focus() {}, select() {}, blur() {}, scrollIntoView() {},
  querySelectorAll() { return [] }, querySelector() { return mk('x') },
  getBoundingClientRect() { return { left: 0, width: 500, top: 0, height: 200 } },
  setAttribute(k, v) { this[k] = v },
  offsetWidth: 1, scrollTop: 0, scrollLeft: 0,
});
global.document = {
  getElementById: i => store[i] || (store[i] = mk(i)),
  querySelector: s => store[s] || (store[s] = mk(s)),
  querySelectorAll: () => [], createElement: () => mk("x"),
  activeElement: null, addEventListener() {},
};
global.addEventListener = () => {};
global.matchMedia = () => ({ matches: false });
global.URL = { createObjectURL: () => "" };
global.Blob = class {};
// minimal sessionStorage, since node has none
const __ss = {};
global.sessionStorage = {
  getItem: k => (k in __ss ? __ss[k] : null),
  setItem: (k, v) => { __ss[k] = String(v); },
  removeItem: k => { delete __ss[k]; },
};
global.requestAnimationFrame = f => f();
global.location = { search: '' };
global.performance = { now: () => Date.now() };
global.setInterval = () => 0;
global.clearInterval = () => {};
global.URLSearchParams = URLSearchParams;
// serve the real detail files, so the drawer's tabs are exercised for real
global.fetch = async (url) => {
  const m = String(url).match(/data\/co\/(.+)\.json/);
  if (!m) return { ok: false };
  const f = 'data/co/' + decodeURIComponent(m[1]) + '.json';
  if (!fs.existsSync(f)) return { ok: false };
  return { ok: true, json: async () => JSON.parse(fs.readFileSync(f, 'utf8')) };
};

const test = `
DB = DB0; ROWS = DB.rows;
ROWS.forEach(r => { S.ccs.add(r.cc); S.rgs.add(r.rg); S.cats.add(r.cat); });
const errs = [];
const RE_ROW = new RegExp('<tr class="r"', 'g');
const RE_AGG = new RegExp('<tr class="agg"', 'g');
const RE_TH  = new RegExp('<th', 'g');

for (const c of Object.keys(COLSETS)) {
  try {
    setCols(c);
    const out = document.querySelector('#tb').innerHTML;
    const th  = document.querySelector('#th').innerHTML;
    const nrow = (out.match(RE_ROW) || []).length;
    const nagg = (out.match(RE_AGG) || []).length;
    const ncol = (th.match(RE_TH) || []).length;
    const dash = (out.match(new RegExp('>—<', 'g')) || []).length;
    console.log(c.padEnd(6), '->', String(nrow).padStart(4), 'rows',
      String(nagg).padStart(3), 'medians', String(ncol).padStart(3), 'cols',
      'sort=' + S.sort.padEnd(6), 'empty cells=' + dash);
  } catch (e) {
    errs.push(c + ': ' + e.message);
  }
}

// median-row content check on the flagship view
setCols('comp'); S.grp = 'cc-cat'; apply();
const html = document.querySelector('#tb').innerHTML;
const first = html.slice(html.indexOf('<tr class="agg">'));
const cells = (first.slice(0, first.indexOf('</tr>')).match(new RegExp('<td[^>]*>[^<]*', 'g')) || [])
  .map(s => s.replace(new RegExp('<td[^>]*>'), '').trim() || '·');
console.log('');
console.log('median row:', cells.join(' | '));

// verify the median maths against a hand computation
setCols('comp'); S.grp = 'flat'; S.flags.clear(); apply();
const navs = VIEW.map(r => r.pnav).filter(v => v != null).sort((a, b) => a - b);
const mid = navs.length % 2 ? navs[navs.length >> 1]
          : (navs[(navs.length >> 1) - 1] + navs[navs.length >> 1]) / 2;
console.log('P/NAV median check: computed', med(VIEW.map(r => r.pnav).filter(v => v != null)).toFixed(4),
            'vs expected', mid.toFixed(4));

// drawer still fine
VIEW = ROWS.filter(r => r.spy);
try { openRow(0); console.log('drawer ->', document.querySelector('#drawer').innerHTML.length, 'chars'); }
catch (e) { errs.push('drawer: ' + e.message); }


// global ranking mode: rank column present, sort applies across the whole universe
setCols('comp'); S.grp = 'flat'; S.sort = 'vy'; S.dir = -1; apply();
const gh = document.querySelector('#tb').innerHTML;
const nrk = (gh.match(new RegExp('<td class="rk">', 'g')) || []).length;
const th2 = document.querySelector('#th').innerHTML;
console.log('');
console.log('GLOBAL mode ->', nrk, 'rank cells,', (th2.match(new RegExp('<th', 'g')) || []).length, 'cols');
const desc = VIEW.every((r, i) => i === 0 || (VIEW[i-1].vy >= r.vy));
console.log('  sorted across all countries/sectors:', desc,
            '| #1', VIEW[0].t, VIEW[0].cc, VIEW[0].cat, (VIEW[0].vy*100).toFixed(2) + '%',
            '| #438', VIEW[VIEW.length-1].t);
const ccs = new Set(VIEW.slice(0, 20).map(r => r.cc));
console.log('  countries in global top 20:', ccs.size, [...ccs].join(','));

// group header row
setCols('brg'); S.grp = 'cc-cat'; apply();
console.log('');
console.log('group header:', document.querySelector('#thg').innerHTML
  .split('<th').filter(s => s.includes('>'))
  .map(s => s.slice(s.indexOf('>') + 1).replace('</th', '').trim() || '·').join(' | '));

// banker formatting: negatives in parentheses
PAREN = true; setCols('ret'); apply();
const rh = document.querySelector('#tb').innerHTML;
console.log('');
const countParen = s => s.split('>(').length - 1;
const countMinus = s => s.split('\u2212').length - 1;
console.log('PAREN on  -> parens:', countParen(rh), '| minus:', countMinus(rh));
PAREN = false; apply();
const rh2 = document.querySelector('#tb').innerHTML;
console.log('PAREN off -> parens:', countParen(rh2), '| minus:', countMinus(rh2));

// group colspans must total the column count, or the header row shears
console.log('');
for (const [k, g] of Object.entries(GROUPS)) {
  const sum = g.reduce((a, [, n]) => a + n, 0);
  const n = COLSETS[k].length;
  console.log((sum === n ? '  ok   ' : '  SHEAR') + ' ' + k.padEnd(6) + ' groups=' + sum + ' cols=' + n);
  if (sum !== n) errs.push('GROUPS.' + k + ' spans ' + sum + ' but view has ' + n);
}

// live-quote rescaling: a price move must invert the yield and scale the cap.
// If this is wrong every yield on screen is silently wrong, so it is asserted
// rather than eyeballed.
console.log('');
const lr = ROWS.find(r => r.ry > 0.02 && r.pnav && r.mcap) || ROWS[0];
LIVE[lr.t] = { p: lr.px / 2, v: 1e6, t: Date.now(), src: 'yahoo' };
const lv = L(lr);
const checks = [
  ['yield doubles when price halves', Math.abs(lv.ry - lr.ry * 2) < 1e-9],
  ['verified yield scales too',       Math.abs(lv.vy - lr.vy * 2) < 1e-9],
  ['2027E yield scales too',          Math.abs(lv.y27 - lr.y27 * 2) < 1e-9],
  ['P/NAV halves',                    Math.abs(lv.pnav - lr.pnav / 2) < 1e-9],
  ['discount follows P/NAV',          Math.abs(lv.disc - (lr.pnav / 2 - 1)) < 1e-9],
  ['market cap halves',               Math.abs(lv.mcap - lr.mcap / 2) < 1e-9],
  ['peer median untouched',           lv.pm === lr.pm],
  ['flagged as live',                 lv._live === true],
];
console.log('LIVE rescale on ' + lr.t + ' (price ' + lr.px + ' -> ' + (lr.px / 2) + '):');
for (const [lab, ok] of checks) {
  console.log('  ' + (ok ? 'ok   ' : 'FAIL ') + lab);
  if (!ok) errs.push('live rescale: ' + lab);
}
const untouched = L(ROWS.find(r => !LIVE[r.t]));
if (untouched._live) errs.push('live rescale: row without a quote was marked live');
console.log('  ' + (untouched._live ? 'FAIL ' : 'ok   ') + 'rows without a quote left alone');
delete LIVE[lr.t];

// localisation: SIGMA runs in Portuguese, so PT is the default. Verify the
// table has no half-translated headers and that sector names match SIGMA's.
console.log('');
LANG = 'pt';
setCols('ret'); renderHead(); apply();
console.log('PT headers (Perf): ' + COLS().map(c => t(c.l)).join(' | '));
setCols('yield'); renderHead(); apply();
const pth = document.querySelector('#th').innerHTML;
const untranslated = COLS().map(c => c.l).filter(l => t(l) === l && /[a-z]/.test(l));
console.log('PT untranslated in Dividend view: ' + (untranslated.length ? untranslated.join(', ') : 'none'));
const sig = ['Homebuilder \u00b7 mid-high', 'Homebuilder \u00b7 low income', 'Malls'];
console.log('sector names vs SIGMA: ' + sig.map(s => t(s)).join(' / ') + '  (expect HB MÉDIA/ALTA / HB BAIXA RENDA / SHOPPINGS)');
console.log('aggregate label: ' + t('MEDIAN') + '  (expect MÉDIA)');
if (t('MEDIAN') !== 'M\u00c9DIA') errs.push('PT aggregate label wrong');
LANG = 'en'; renderHead(); apply();
console.log('EN restored: ' + COLS().slice(2, 6).map(c => t(c.l)).join(' | '));
LANG = 'pt';

// responsive degradation: how many columns survive each breakpoint
// drawer tabs, against a real detail payload
(async () => {
  const r = ROWS.find(x => x.t === 'PLD') || ROWS[0];
  const det = await loadDetail(r.t);
  if (!det) { console.log(''); console.log('  detail: no file for ' + r.t); return; }
  console.log('');
  console.log('DETAIL ' + r.t + ': ' + det.px.w.p.length + ' weekly, ' + det.px.d.p.length +
    ' daily, ' + det.annual.length + 'A/' + det.quarterly.length + 'Q, ' +
    det.mult.length + ' mult pts');
  for (const tab of ['chart', 'fin', 'val', 'q']) {
    DTAB = tab;
    try {
      const html = renderTab(r, det);
      console.log('  ' + tab.padEnd(6) + String(html.length).padStart(6) + ' chars' +
        (html.includes('undefined') ? '   <-- CONTAINS undefined' : '') +
        (html.includes('NaN') ? '   <-- CONTAINS NaN' : ''));
      if (html.includes('undefined') || html.includes('NaN')) errs.push('tab ' + tab + ' rendered undefined/NaN');
    } catch (e) { errs.push('tab ' + tab + ': ' + e.message); }
  }
  for (const rg of Object.keys(RANGES)) {
    DRANGE = rg;
    const s = rangeSeries(det.px, rg);
    console.log('  range ' + rg.padEnd(4) + (s ? s.p.length + ' pts ' + (s.daily ? 'daily' : 'weekly') : 'EMPTY'));
    if (!s) errs.push('range ' + rg + ' empty');
  }
  DTAB = 'chart'; DRANGE = '5Y';
  console.log('');
  console.log(errs.length ? 'ERRORS:\\n ' + errs.join('\\n ') : 'OK — no runtime errors');
})();

// access gate: wrong code refuses, right code passes, session persists
console.log('');
try { sessionStorage.removeItem(GATE_KEY); } catch (e) {}
console.log('gate enabled: ' + GATE_ENABLED);
console.log('checkGate() before entry: ' + checkGate() + '  (expect false)');
if (checkGate() !== false) errs.push('gate: should be locked before a passcode is entered');
try {
  sessionStorage.setItem(GATE_KEY, '1');
  console.log('checkGate() after session flag set: ' + checkGate() + '  (expect true)');
  if (checkGate() !== true) errs.push('gate: should unlock once the session flag is set');
  sessionStorage.removeItem(GATE_KEY);
} catch (e) {
  console.log('sessionStorage unavailable in this harness — skipping the persistence check');
}

console.log('');
const P = ch => 'data-p="' + ch + '"';
for (const v of Object.keys(COLSETS)) {
  setCols(v); apply();
  const out = document.querySelector('#tb').innerHTML;
  const n = VIEW.length || 1;
  const c3 = (out.split(P('3')).length - 1) / n;
  const c2 = (out.split(P('2')).length - 1) / n;
  const tot = COLS().length;
  console.log('  ' + v.padEnd(6) + tot + ' cols  <1180px ' + (tot - c3) + '  <820px ' + (tot - c3 - c2));
}
setCols('yield');
console.log('  frozen left: ' + COLS().slice(0, 2).map(c => c.l).join(' + '));
console.log('  never drops: ' + COLS().filter(c => !c.p).map(c => c.l).join(', '));

console.log('');
console.log(errs.length ? 'ERRORS:\\n ' + errs.join('\\n ') : 'OK — no runtime errors');
`;
eval(js + test);

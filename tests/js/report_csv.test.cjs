const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const test = require('node:test');
const { runInNewContext } = require('node:vm');

const script = readFileSync(resolve(__dirname, '../../src/argus/reporting/assets/argus.js'), 'utf8');

function element(dataset = {}) {
  const attributes = new Map();
  const classes = new Set();
  return {
    dataset, value: '', checked: false, disabled: false, style: {}, listeners: {},
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        const on = force === undefined ? !classes.has(name) : force;
        if (on) classes.add(name); else classes.delete(name);
      },
    },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    querySelector() { return null; },
    hasAttribute() { return false; },
    setAttribute(name, value) { attributes.set(name, value); },
    getAttribute(name) { return attributes.get(name); },
    appendChild() {},
    click() { this.listeners.click(); },
  };
}

function browser(records, { runKeys = [], query = '' } = {}) {
  const controls = new Map([
    'argus-search', 'argus-verdict', 'argus-status', 'argus-tester',
    'argus-high-only', 'argus-flagged-only', 'argus-export-csv',
    'argus-run', 'argus-export-all-csv', 'argus-clear', 'argus-count', 'argus-no-matches',
  ].map(id => [id, element()]));
  const button = controls.get('argus-export-csv');
  button.dataset = { csvHeaders: '["Execution key","Tester","Findings"]', filename: 'report.csv' };
  const rows = records.map((record, i) => element({
    idx: String(i), search: record.join(' ').toLowerCase(), verdict: 'fail',
    tester: record[1], csv: JSON.stringify(record), run: runKeys[i] || '',
  }));
  const runToggles = new Map();
  const runSelects = new Map();
  const runExports = new Map();
  const headings = [...new Set(runKeys)].map(key => {
    const toggle = element({ runToggle: key });
    runToggles.set(key, toggle);
    runSelects.set(key, element({ runSelect: key }));
    runExports.set(key, element({ runExport: key }));
    return { ...element({ run: key }), querySelector: () => toggle };
  });
  rows.forEach(row => { row.parentNode = { querySelectorAll: () => rows }; });
  const downloads = [];
  const blobs = [];
  const revoked = [];
  const timers = [];
  const urls = [];
  runInNewContext(script, {
    document: {
      addEventListener() {},
      documentElement: element(),
      body: { ...element(), appendChild() {} },
      getElementById: id => controls.get(id) || null,
      querySelector: () => null,
      querySelectorAll: selector => ({
        'tr.argus-row': rows,
        'tr.argus-run-heading': headings,
        '[data-run-toggle]': [...runToggles.values()],
        '[data-run-select]': [...runSelects.values()],
        '[data-run-export]': [...runExports.values()],
      }[selector] || []),
      createElement: () => ({
        click() { downloads.push(this.download); },
        remove() {},
      }),
    },
    window: {}, Blob, URLSearchParams,
    URL: {
      createObjectURL(blob) { blobs.push(blob); return 'blob:report'; },
      revokeObjectURL(url) { revoked.push(url); },
    },
    location: { pathname: '/report.html', search: query, hash: '' },
    history: { replaceState(_state, _title, url) { urls.push(url); } },
    setTimeout: callback => timers.push(callback),
  });
  return {
    controls, rows, downloads, revoked, headings, runToggles, runSelects, runExports, urls,
    async export(target = button) {
      target.listeners.click();
      const contents = Buffer.from(await blobs.at(-1).arrayBuffer()).toString('utf8');
      timers.forEach(callback => callback());
      return contents;
    },
  };
}

test('CSV includes every filtered page in current sort order', async () => {
  const records = Array.from({ length: 80 }, (_, i) => [`MOBILE-E${i}`, i < 60 ? 'Alice' : 'Bob', 'Finding']);
  const page = browser(records);
  const tester = page.controls.get('argus-tester');
  tester.value = 'Alice';
  tester.listeners.change();
  assert.equal(page.rows.filter(row => row.style.display !== 'none').length, 50);
  page.rows.reverse();
  const csv = await page.export();
  const lines = csv.slice(1).trim().split('\r\n');
  assert.equal(lines.length, 61);
  assert.equal(lines[1], '"MOBILE-E59","Alice","Finding"');
  assert.equal(lines.at(-1), '"MOBILE-E0","Alice","Finding"');
  assert.ok(!csv.includes('Bob'));
  assert.deepEqual(page.downloads, ['report.csv']);
  assert.deepEqual(page.revoked, ['blob:report']);
});

test('CSV preserves Unicode and multiline quotes and neutralizes formulas', async () => {
  const page = browser([
    ['MOBILE-E1', 'Zoë, "QA"', 'First line\nSecond "line"'],
    ['MOBILE-E2', '=1+1', ' \t@SUM(A1:A2)'],
  ]);
  const csv = await page.export();
  assert.ok(csv.startsWith('\uFEFF'));
  assert.ok(csv.includes('"Zoë, ""QA""","First line\nSecond ""line"""'));
  assert.ok(csv.includes(`"'=1+1","' \t@SUM(A1:A2)"`));
  assert.ok(csv.endsWith('\r\n'));
});

test('CSV is disabled when there are no audits or no filter matches', () => {
  assert.equal(browser([]).controls.get('argus-export-csv').disabled, true);
  const page = browser([['MOBILE-E1', 'Alice', 'Finding']]);
  const tester = page.controls.get('argus-tester');
  tester.value = 'Nobody';
  tester.listeners.change();
  assert.equal(page.controls.get('argus-export-csv').disabled, true);
  tester.value = '';
  tester.listeners.change();
  assert.equal(page.controls.get('argus-export-csv').disabled, false);
});

test('run selection composes with tester filters and exports across pages', async () => {
  const records = Array.from({ length: 90 }, (_, i) => [`KEY-E${i}`, i < 80 ? 'Alice' : 'Bob', 'Finding']);
  const runKeys = records.map((_, i) => i < 60 ? 'KEY-C1' : 'KEY-C10');
  const page = browser(records, { runKeys });
  page.runSelects.get('KEY-C1').click();
  assert.equal(page.rows.filter(row => row.style.display !== 'none').length, 50);
  assert.equal(page.headings[1].style.display, 'none');
  let csv = await page.export();
  assert.equal(csv.slice(1).trim().split('\r\n').length, 61);
  assert.equal(page.downloads.at(-1), 'report-KEY-C1.csv');
  assert.ok(page.urls.at(-1).includes('run=KEY-C1'));
  assert.ok(!csv.includes('KEY-E60'));

  const run = page.controls.get('argus-run');
  run.value = 'KEY-C10';
  run.listeners.change();
  const tester = page.controls.get('argus-tester');
  tester.value = 'Bob';
  tester.listeners.change();
  csv = await page.export();
  assert.equal(csv.slice(1).trim().split('\r\n').length, 11);
  assert.ok(!csv.includes('Alice'));

  csv = await page.export(page.controls.get('argus-export-all-csv'));
  assert.equal(csv.slice(1).trim().split('\r\n').length, 91);
  assert.equal(page.downloads.at(-1), 'report.csv');
  csv = await page.export(page.runExports.get('KEY-C1'));
  assert.equal(csv.slice(1).trim().split('\r\n').length, 61);
});

test('collapsing a run preserves exports and clear restores all groups', async () => {
  const page = browser([
    ['KEY-E1', 'Alice', 'One'], ['KEY-E2', 'Bob', 'Two'],
  ], { runKeys: ['KEY-C1', 'KEY-C2'] });
  const toggle = page.runToggles.get('KEY-C1');
  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(page.rows[0].style.display, 'none');
  assert.equal(page.rows[1].style.display, '');
  assert.equal(page.headings[0].style.display, '');
  assert.ok(page.controls.get('argus-count').textContent.includes('1 hidden in collapsed runs'));
  assert.ok((await page.export()).includes('KEY-E1'));
  page.controls.get('argus-clear').click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.ok(page.rows.every(row => row.style.display === ''));
});

test('run links hydrate exactly and unknown runs cannot broaden the view', () => {
  const records = [['KEY-E1', 'Alice', 'One'], ['KEY-E2', 'Bob', 'Two']];
  const runKeys = ['KEY-C1', 'KEY-C2'];
  const page = browser(records, { runKeys, query: '?run=KEY-C2' });
  assert.equal(page.rows[0].style.display, 'none');
  assert.equal(page.rows[1].style.display, '');
  const missing = browser(records, { runKeys, query: '?run=KEY-C999' });
  assert.ok(missing.rows.every(row => row.style.display === 'none'));
  assert.equal(missing.controls.get('argus-no-matches').hidden, false);
  assert.equal(missing.controls.get('argus-export-csv').disabled, true);
  assert.equal(missing.controls.get('argus-export-all-csv').disabled, false);
});

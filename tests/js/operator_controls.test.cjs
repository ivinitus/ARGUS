// Run with: node --test tests/js/operator_controls.test.cjs
const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const test = require('node:test');
const { runInNewContext } = require('node:vm');

const script = readFileSync(resolve(__dirname, '../../src/argus/reporting/assets/operator.js'), 'utf8');
const runId = '1'.repeat(32);
const activeRun = { id: runId, phase: 'running', target: 'Example folder' };
const jsonResponse = (body, status = 200) => ({
  type: 'cors', status, ok: status >= 200 && status < 300,
  json: async () => body,
});
const settle = () => new Promise(resolve => setImmediate(resolve));

function element() {
  const listeners = new Map();
  return {
    hidden: false, disabled: false, textContent: '',
    classList: { toggle() {} },
    addEventListener: (name, listener) => listeners.set(name, listener),
    emit: (name, event = {}) => listeners.get(name)?.(event),
  };
}

function browser(respond, saved = {}) {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, element());
    return nodes.get(selector);
  };
  const form = node('[data-operator-form]');
  form.hidden = true;
  form.elements = { target: { value: '' } };
  form.querySelector = node;
  const root = { dataset: { apiUrl: 'https://api.example.test', team: 'web' }, querySelector: node };
  const document = { ...element(), hidden: false, querySelector: () => root };
  const timers = [];
  const window = {
    ...element(), confirm: () => true,
    setInterval: callback => timers.push(callback),
  };
  let now = 0;
  const storage = new Map([['argus-operator-web', JSON.stringify(saved)]]);
  const calls = [];
  runInNewContext(script, {
    document, window, crypto: webcrypto,
    Date: class extends Date { static now() { return now; } },
    sessionStorage: {
      getItem: key => storage.get(key),
      setItem: (key, value) => storage.set(key, value),
    },
    fetch: async (url, options) => {
      const path = new URL(url).pathname;
      calls.push({ path, options });
      return respond(path, options);
    },
  });
  return {
    node, form, calls, window,
    saved: () => JSON.parse(storage.get('argus-operator-web')),
    poll: () => timers[0](),
    time: value => { now = value; },
  };
}

test('a failed initial session check gives guidance and reconnects without reloading', async () => {
  let connected = false;
  const page = browser(path => {
    if (!connected) throw new TypeError('Failed to fetch');
    return jsonResponse(path === '/session' ? { alias: 'tester' } : { run: null });
  });
  await settle();
  assert.match(page.node('[data-operator-message]').textContent, /Check your connection or sign in again/);
  assert.doesNotMatch(page.node('[data-operator-message]').textContent, /Failed to fetch/);
  assert.equal(page.form.hidden, true);
  page.time(5000);
  page.poll();
  await settle();
  assert.equal(page.calls.length, 1, 'disconnected tabs must not retry every five seconds');
  connected = true;
  page.time(30000);
  page.poll();
  await settle();
  assert.equal(page.form.hidden, false);
  assert.equal(page.node('[data-operator-signin]').hidden, true);
  assert.equal(page.node('[data-operator-message]').textContent, '');
});

test('the audit form submits a comma-separated list as one idempotent request', async () => {
  const page = browser((path, options) => {
    if (options.method === 'POST') return jsonResponse({ ...activeRun, phase: 'launching' }, 202);
    if (path === '/session') return jsonResponse({ alias: 'tester' });
    return jsonResponse({ run: null });
  });
  await settle();
  const targets = Array.from({length: 20}, (_, i) => `QA-C${i}`).join(', ');
  page.form.elements.target.value = `  ${targets}  `;
  page.form.emit('submit', { preventDefault() {} });
  await settle();
  const posts = page.calls.filter(call => call.options.method === 'POST');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].path, '/teams/web/runs');
  assert.equal(JSON.parse(posts[0].options.body).target, targets);
  assert.match(posts[0].options.headers['Idempotency-Key'], /^[0-9a-f]{32}$/);
});

test('expired authentication marks status unavailable and restores the same run after sign-in', async () => {
  let mode = 'running';
  const page = browser(path => {
    if (path === '/session') return jsonResponse({ alias: 'tester' });
    if (mode === 'expired') return { type: 'opaqueredirect', status: 0 };
    if (path.endsWith('/logs')) return jsonResponse({ lines: [], ready: true });
    return jsonResponse({ ...activeRun, phase: mode });
  }, { run: runId });
  await settle();
  assert.equal(page.node('[data-operator-phase]').textContent, 'Running');
  mode = 'expired';
  page.poll();
  await settle();
  assert.equal(page.node('[data-operator-phase]').textContent, 'Status unavailable');
  assert.equal(page.node('[data-operator-stop]').hidden, true);
  assert.equal(page.node('[data-operator-signin]').hidden, false);
  assert.equal(page.saved().run, runId);
  const count = page.calls.length;
  page.poll();
  await settle();
  assert.equal(page.calls.length, count);
  mode = 'succeeded';
  page.window.emit('focus');
  await settle();
  assert.equal(page.node('[data-operator-phase]').textContent, 'Completed');
  assert.equal(page.node('[data-operator-message]').textContent, '');
  assert.equal(page.node('[data-operator-signin]').hidden, true);
});

test('a disconnect while restoring a saved run still displays reconnection guidance', async () => {
  const page = browser(path => {
    if (path === '/session') return jsonResponse({ alias: 'tester' });
    throw new TypeError('Failed to fetch');
  }, { run: runId });
  await settle();
  assert.equal(page.form.hidden, true);
  assert.match(page.node('[data-operator-message]').textContent, /Check your connection or sign in again/);
});

test('reconnection never replays a launch and preserves its idempotency key for an explicit retry', async () => {
  const posts = [];
  const page = browser((path, options) => {
    if (options.method === 'POST') {
      posts.push(options);
      if (posts.length === 1) throw new TypeError('Failed to fetch');
      return jsonResponse({ ...activeRun, phase: 'launching' }, 202);
    }
    if (path === '/session') return jsonResponse({ alias: 'tester' });
    if (path.endsWith('/status')) return jsonResponse({ run: null });
    if (path.endsWith('/logs')) return jsonResponse({ lines: [], ready: true });
    return jsonResponse(activeRun);
  });
  await settle();
  page.form.elements.target.value = 'Example folder';
  page.form.emit('submit', { preventDefault() {} });
  await settle();
  const key = page.saved().pending.key;
  assert.equal(posts.length, 1);
  page.window.emit('online');
  await settle();
  assert.equal(posts.length, 1, 'reconnecting may only read status');
  assert.equal(page.form.hidden, false);
  page.form.emit('submit', { preventDefault() {} });
  await settle();
  assert.equal(posts.length, 2);
  assert.equal(posts[0].headers['Idempotency-Key'], key);
  assert.equal(posts[1].headers['Idempotency-Key'], key);
});

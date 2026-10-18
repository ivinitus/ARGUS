import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { runInNewContext } from 'node:vm';

const source = readFileSync(new URL('../src/argus/reporting/assets/operator.js', import.meta.url), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const reply = (body, status = 200) => ({ok: status < 400, status, json: async () => body});

function browser(respond) {
  const nodes = new Map();
  const get = selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      hidden: true, disabled: false, textContent: '', listeners: {},
      classList: {toggle() {}},
      elements: {target: {value: 'QA-E123'}},
      addEventListener(name, listener) { this.listeners[name] = listener; },
      querySelector: get
    });
    return nodes.get(selector);
  };
  const root = get('[data-argus-operator]');
  root.dataset = {apiUrl: 'https://api.example.test', team: 'web'};
  const calls = [];
  const storage = new Map();
  let poll;
  runInNewContext(source, {
    document: {hidden: false, querySelector: get, addEventListener() {}},
    window: {setInterval(fn) { poll = fn; }, confirm: () => true, addEventListener() {}},
    crypto: webcrypto, Uint8Array, encodeURIComponent,
    sessionStorage: {getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v)},
    fetch: async (url, options) => {
      calls.push({path: new URL(url).pathname, options});
      return respond(new URL(url).pathname, options);
    }
  });
  return {get, calls, poll: async () => { poll(); await settle(); }};
}

test('expired authentication keeps launch controls hidden and offers sign-in', async () => {
  const page = browser(() => reply({error: 'Unauthorized'}, 401));
  await settle();
  assert.equal(page.get('[data-operator-form]').hidden, true);
  assert.equal(page.get('[data-operator-signin]').hidden, false);
  assert.match(page.get('[data-operator-message]').textContent, /Sign in/);
  assert.equal(page.calls.length, 1);
});

test('retry after an uncertain launch reuses the original request key', async () => {
  let attempts = 0;
  const run = {id: 'a'.repeat(32), phase: 'pending'};
  const page = browser((path, options) => {
    if (path === '/session') return reply({alias: 'alice'});
    if (path.endsWith('/status')) return reply({run: null});
    if (options.method === 'POST') {
      if (++attempts === 1) throw new Error('Connection lost');
      return reply(run, 202);
    }
    if (path.endsWith('/logs')) return reply({ready: false, lines: []});
    return reply(run);
  });
  await settle();
  const submit = page.get('[data-operator-form]').listeners.submit;
  submit({preventDefault() {}});
  await settle();
  submit({preventDefault() {}});
  await settle();
  const posts = page.calls.filter(call => call.options.method === 'POST');
  assert.equal(posts.length, 2);
  assert.equal(posts[0].options.headers['Idempotency-Key'], posts[1].options.headers['Idempotency-Key']);
  assert.equal(page.get('button[type="submit"]').disabled, true);
  assert.equal(page.get('[data-operator-phase]').textContent, 'Starting');
});

test('completed logs are fetched once and a later run replaces the completed run', async () => {
  let current = {id: 'b'.repeat(32), phase: 'succeeded'};
  const page = browser(path => {
    if (path === '/session') return reply({alias: 'alice'});
    if (path.endsWith('/status')) return reply({run: current});
    if (path.endsWith('/logs')) return reply({ready: true, lines: ['<script>literal log text</script>']});
    return reply(current);
  });
  await settle();
  await page.poll();
  assert.equal(page.calls.filter(call => call.path.endsWith('/logs')).length, 1);
  assert.equal(page.get('[data-operator-phase]').textContent, 'Completed');
  assert.equal(page.get('[data-operator-logs]').textContent, '<script>literal log text</script>');
  current = {id: 'c'.repeat(32), phase: 'running'};
  await page.poll();
  assert.equal(page.get('[data-operator-phase]').textContent, 'Running');
  assert.equal(page.get('[data-operator-stop]').hidden, false);
  await page.get('[data-operator-stop]').listeners.click();
  const stop = page.calls.find(call => call.path.endsWith('/stop'));
  assert.equal(stop.path, '/teams/web/runs/' + current.id + '/stop');
  assert.equal(stop.options.method, 'POST');
});

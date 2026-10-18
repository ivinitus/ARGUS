(function () {
  'use strict';
  const root = document.querySelector('[data-argus-operator]');
  if (!root) return;
  const api = root.dataset.apiUrl.replace(/\/$/, '');
  const team = root.dataset.team;
  const base = '/teams/' + encodeURIComponent(team);
  const form = root.querySelector('[data-operator-form]');
  const message = root.querySelector('[data-operator-message]');
  const stop = root.querySelector('[data-operator-stop]');
  const rerender = root.querySelector('[data-operator-rerender]');
  const help = root.querySelector('[data-operator-help]');
  if (help) {
    document.addEventListener('click', event => {
      if (help.open && !help.contains(event.target)) help.open = false;
    });
    help.addEventListener('keydown', event => {
      if (event.key === 'Escape' && help.open) {
        event.preventDefault();
        event.stopPropagation();
        help.open = false;
        help.querySelector('summary').focus();
      }
    });
  }
  const terminal = new Set(['succeeded', 'failed', 'stopped', 'rejected']);
  const labels = {
    launching: 'Starting', pending: 'Starting', provisioning: 'Starting',
    activating: 'Starting', running: 'Running', deactivating: 'Stopping',
    stopping: 'Stopping', deprovisioning: 'Stopping',
    succeeded: 'Completed', failed: 'Failed', stopped: 'Stopped',
    rejected: 'Not started', unconfirmed: 'Checking launch status'
  };
  let run = null;
  let authenticated = false;
  let busy = false;
  let polling = false;
  let connecting = false;
  let nextSessionCheck = 0;
  let pending = null;
  let completedLogsFor = null;
  const storageKey = 'argus-operator-' + team;

  function tell(text, error) {
    message.textContent = text;
    message.classList.toggle('error', !!error);
  }
  function save() {
    try { sessionStorage.setItem(storageKey, JSON.stringify({run: run && run.id, pending})); }
    catch (_) { /* Controls work without browser storage. */ }
  }
  function controls() {
    const active = run && !terminal.has(run.phase);
    form.querySelector('button[type="submit"]').disabled = busy || !authenticated || !!active;
    rerender.disabled = busy || !authenticated || !!active;
    stop.disabled = busy || !authenticated;
    stop.hidden = !authenticated || !active || !run || run.phase === 'unconfirmed';
  }
  function disconnected() {
    authenticated = false;
    nextSessionCheck = Date.now() + 30000;
    root.querySelector('[data-operator-signin]').hidden = false;
    root.querySelector('[data-operator-identity]').textContent = '';
    form.hidden = true;
    if (help) help.open = false;
    if (run) root.querySelector('[data-operator-phase]').textContent = 'Status unavailable';
    controls();
  }
  async function request(path, options) {
    let response;
    try {
      response = await fetch(api + path, {
        credentials: 'include', cache: 'no-store', redirect: 'manual',
        headers: {Accept: 'application/json'}, ...options
      });
    } catch (_) {
      disconnected();
      throw new Error('Could not connect to audit controls. Check your connection or sign in again to refresh the run status.');
    }
    if (response.type === 'opaqueredirect' || response.status === 401) {
      disconnected();
      throw new Error('Sign in to controls, then return to reconnect and refresh the run status.');
    }
    let body;
    try { body = await response.json(); }
    catch (_) { throw new Error('The API did not return a status. Refresh before retrying.'); }
    if (!response.ok) {
      const error = new Error(body.error || 'ARGUS could not complete the request.');
      error.status = response.status;
      throw error;
    }
    return body;
  }
  function display(value) {
    if (!run || !value || run.id !== value.id) {
      completedLogsFor = null;
      root.querySelector('[data-operator-logs]').textContent = '';
    }
    run = value;
    root.querySelector('[data-operator-run]').hidden = !run;
    if (!run) { controls(); return; }
    const progress = run.progress || {};
    root.querySelector('[data-operator-phase]').textContent = labels[run.phase] || run.phase;
    const parts = [progress.target || progress.folder || run.target || 'Audit task'];
    if (Number(progress.total) > 0) {
      parts.push((Number(progress.completed) || 0) + ' / ' + Number(progress.total) + ' keys');
    }
    if (progress.phase) parts.push(progress.phase);
    if (progress.last_key) parts.push('last ' + progress.last_key);
    if (progress.requested_by || run.requested_by) parts.push('by ' + (progress.requested_by || run.requested_by));
    root.querySelector('[data-operator-progress]').textContent = parts.join(' · ');
    root.querySelector('[data-operator-reports]').hidden = !terminal.has(run.phase);
    controls();
    save();
  }
  async function refresh() {
    if (!authenticated || polling || document.hidden) return;
    polling = true;
    try {
      if (run && !terminal.has(run.phase)) {
        display(await request(base + '/runs/' + encodeURIComponent(run.id)));
      } else {
        const status = await request(base + '/status');
        if (status.run) display(status.run);
      }
      if (run && run.phase !== 'launching' && run.phase !== 'unconfirmed'
          && run.phase !== 'rejected' && completedLogsFor !== run.id) {
        const logs = await request(base + '/runs/' + encodeURIComponent(run.id) + '/logs');
        root.querySelector('[data-operator-logs]').textContent =
          logs.lines.join('\n') || (logs.ready ? 'No recent activity.' : 'Waiting for the task to start.');
        if (terminal.has(run.phase)) completedLogsFor = run.id;
      }
    } catch (error) {
      tell(error.message || 'Could not refresh the run status.', true);
    } finally {
      polling = false;
      controls();
    }
  }
  function requestKey() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  }
  async function launch(action, body) {
    if (busy) return;
    const fingerprint = JSON.stringify([action, body]);
    if (!pending || pending.fingerprint !== fingerprint) pending = {fingerprint, key: requestKey()};
    save();
    busy = true;
    controls();
    tell(action === 'runs' ? 'Starting the audit…' : 'Refreshing existing reports…');
    try {
      const result = await request(base + '/' + action, {
        method: 'POST',
        headers: {
          Accept: 'application/json', 'Content-Type': 'application/json',
          'X-Argus-Request': '1', 'Idempotency-Key': pending.key
        },
        body: JSON.stringify(body)
      });
      pending = null;
      display(result);
      tell('Request accepted. You can follow its progress here.');
      await refresh();
    } catch (error) {
      if (error.status >= 400 && error.status < 500) pending = null;
      save();
      tell(error.message || 'Launch could not be confirmed. Retry with the same input to check the original request.', true);
    } finally {
      busy = false;
      controls();
    }
  }
  form.addEventListener('submit', function (event) {
    event.preventDefault();
    launch('runs', {target: form.elements.target.value.trim()});
  });
  rerender.addEventListener('click', function () { launch('rerender', {}); });
  stop.addEventListener('click', async function () {
    if (!run || busy || !window.confirm('Stop this run? Completed results will be kept.')) return;
    busy = true;
    controls();
    try {
      await request(base + '/runs/' + encodeURIComponent(run.id) + '/stop', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Argus-Request': '1'},
        body: '{}'
      });
      display({...run, phase: 'stopping'});
      tell('Stop requested. Waiting for the task to finish.');
    } catch (error) { tell(error.message || 'Could not request stop.', true); }
    finally { busy = false; controls(); }
  });
  async function init() {
    if (connecting || busy || document.hidden) return;
    connecting = true;
    nextSessionCheck = Date.now() + 30000;
    try {
      const session = await request('/session');
      authenticated = true;
      root.querySelector('[data-operator-signin]').hidden = true;
      root.querySelector('[data-operator-identity]').textContent = 'Signed in as ' + session.alias;
      form.hidden = false;
      tell('');
      try {
        const saved = JSON.parse(sessionStorage.getItem(storageKey) || '{}');
        pending = saved.pending || null;
        if (/^[0-9a-f]{32}$/.test(saved.run || '')) {
          display(await request(base + '/runs/' + saved.run));
        }
      } catch (error) {
        if (!authenticated) throw error;
        /* A stale run does not block starting a new one. */
      }
      await refresh();
    } catch (error) { tell(error.message || 'Sign in to controls to manage audits.', true); }
    finally { connecting = false; controls(); }
  }
  function reconnect() {
    if (document.hidden || connecting) return;
    if (authenticated) refresh();
    else init();
  }
  controls();
  init();
  window.setInterval(function () {
    if (connecting) return;
    if (authenticated) refresh();
    else if (Date.now() >= nextSessionCheck) init();
  }, 5000);
  document.addEventListener('visibilitychange', reconnect);
  window.addEventListener('online', reconnect);
  window.addEventListener('focus', reconnect);
})();

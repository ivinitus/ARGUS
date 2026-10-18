/* Personal review workspace. All source evidence is rendered as text or safe URLs. */
(() => {
  'use strict';
  try {
    if (localStorage.getItem('cs.theme') === 'dark') document.documentElement.setAttribute('data-cs-theme', 'dark');
  } catch (_) { /* Use the default theme when storage is unavailable. */ }
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const team = document.body.dataset.team;
  const folder = document.body.dataset.folder;
  const api = (document.body.dataset.api || '').replace(/\/$/, '');
  const base = `${api}/teams/${encodeURIComponent(team)}/reviews/${encodeURIComponent(folder)}/`;
  const labels = {pending:'Not started', unassigned:'Unassigned', in_progress:'In progress', investigation:'Needs investigation', reviewed:'Reviewed'};
  const headlines = JSON.parse($('review-rule-headlines').textContent);
  let queue, current, offset = 0, preview = null, loading = 0;
  if (location.pathname.endsWith('/review.html')) {
    history.replaceState(null, '', location.pathname.replace(/\.html$/, '') + location.search + location.hash);
  }
  $('login').hidden = true;
  $('title').textContent = folder.replace(/_/g, ' ');
  function notice(text, error = false) {
    $('message').hidden = !text;
    $('message').classList.toggle('error', error);
    $('message').replaceChildren(document.createTextNode(text));
    if (text) {
      const dismiss = document.createElement('button');
      dismiss.className = 'dismiss';
      dismiss.setAttribute('aria-label', 'Dismiss notification');
      dismiss.innerHTML = '<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M2 2l10 10M12 2L2 12" stroke="currentColor" stroke-width="2"/></svg>';
      dismiss.onclick = () => notice('');
      $('message').append(dismiss);
    }
  }
  async function request(route, body) {
    const response = await fetch(base + route, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: body === undefined ? {} : {'Content-Type':'application/json','X-Argus-Request':'1','Idempotency-Key':crypto.randomUUID().replace(/-/g, '')},
      ...(body === undefined ? {} : {body:JSON.stringify(body)}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
    return data;
  }
  const action = fn => async () => { try { await fn(); } catch (e) { notice(e.message, true); } };
  function options(id, items, placeholder) {
    const selected = $(id).value;
    $(id).innerHTML = `<option value="">${placeholder}</option>` + items.map(([value,label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join('');
    if ([...$(id).options].some(item => item.value === selected)) $(id).value = selected;
  }
  function invalidatePreview() { preview = null; $('allocation-preview').hidden = true; }
  function addReviewer(alias = '') {
    const row = document.createElement('div'); row.className = 'roster-row';
    row.innerHTML = `<label>Name<input value="${esc(alias)}" autocomplete="off" maxlength="100" aria-label="Name"></label><button type="button">Remove</button>`;
    row.querySelector('input').oninput = invalidatePreview;
    row.querySelector('button').onclick = () => {row.remove(); invalidatePreview();};
    $('roster').append(row);
  }
  function renderRoster() {
    $('roster').replaceChildren();
    if (queue.roster.length) queue.roster.forEach(row => addReviewer(row.alias));
    else {addReviewer(); addReviewer();}
  }
  function renderProgress() {
    const roster = [...queue.roster, ...(queue.retained_roster || [])];
    const retained = new Set((queue.retained_roster || []).map(row => row.alias));
    $('reviewer-progress').innerHTML = '<table><thead><tr>' + ['Reviewer','Assigned','Not started','In progress','Needs investigation','Reviewed'].map(text => `<th>${text}</th>`).join('') + '</tr></thead><tbody>' + roster.map(row => {
      const counts = queue.loads[row.alias] || {};
      return '<tr><td>' + esc(row.name || row.alias) + (retained.has(row.alias) ? ' (completed reviews)' : '') + '</td>' + ['assigned','pending','in_progress','investigation','reviewed'].map(key => `<td>${Number(counts[key] || 0)}</td>`).join('') + '</tr>';
    }).join('') + '</tbody></table>';
  }
  async function loadQueue(resetRoster = false) {
    const generation = ++loading;
    const params = new URLSearchParams({reviewer:$('reviewer').value, run:$('run').value, verdict:$('verdict').value, state:$('state').value, offset:String(offset), limit:'50'});
    const result = await request('queue?' + params);
    if (generation !== loading) return;
    queue = result;
    $('identity').textContent = 'Signed in as ' + queue.actor;
    options('reviewer', queue.roster.map(row => [row.alias,row.name || row.alias]), 'All reviewers');
    options('run', queue.runs.map(run => [run, queue.run_names?.[run] ? `${queue.run_names[run]} (${run})` : run]), 'All runs');
    $('stats').innerHTML = ['pending','investigation','reviewed','unassigned'].map(key => `<div class="stat"><strong>${Number(queue.totals[key] || 0)}</strong><span>${key === 'pending' ? 'Awaiting review' : labels[key]}</span></div>`).join('');
    $('queue-count').textContent = `${queue.filtered_total} matching cases · ${queue.total} total`;
    $('queue').innerHTML = queue.items.map(row => `<button class="case-row${current?.case.key === row.key ? ' selected' : ''}" data-key="${esc(row.key)}"><strong>${esc(row.test_case || row.key)}</strong><span>${esc(row.key)}</span><small>${esc(row.assignee || 'Unassigned')}</small><span class="review-status" data-state="${esc(row.review_state || row.state)}">${esc(labels[row.review_state] || labels[row.state] || row.state)}</span></button>`).join('');
    $('queue').querySelectorAll('[data-key]').forEach(button => {button.onclick = action(() => openCase(button.dataset.key));});
    $('previous').disabled = offset === 0;
    $('next').disabled = offset + queue.items.length >= queue.filtered_total;
    if (resetRoster) renderRoster();
    renderProgress();
  }
  function safeURL(value) {
    try { const url = new URL(value, location.href); return ['http:','https:'].includes(url.protocol) ? url.href : ''; }
    catch (_) { return ''; }
  }
  function findingTitle(finding) {
    const known = headlines[finding.rule] || headlines[String(finding.rule).toUpperCase()];
    return known || String(finding.description || finding.title || 'Finding').split(/(?<=\.)\s/)[0];
  }
  function draftDecisions() {
    const result = {};
    $('case').querySelectorAll('[data-finding]').forEach(row => {
      const decision = row.querySelector('select').value;
      if (decision) result[row.dataset.finding] = {decision,reason:row.querySelector('textarea').value.trim()};
    });
    return result;
  }
  function completionState() {
    const decisions = draftDecisions();
    const unresolved = current.case.findings.filter(row => !decisions[row.id] || decisions[row.id].decision === 'investigate').length;
    const reasons = Object.values(decisions).filter(row => row.decision === 'reject' && !row.reason).length;
    let hint = !current.case.ready ? 'This execution needs complete steps, evidence, and analysis before review can be completed.' : '';
    if (unresolved) hint = `Resolve ${unresolved} finding${unresolved === 1 ? '' : 's'} before completing this review.`;
    else if (reasons) hint = `Add a reason for ${reasons} rejected finding${reasons === 1 ? '' : 's'} before completing this review.`;
    const allowed = current.assignee === queue.actor && current.review.state !== 'reviewed';
    $('complete-review').disabled = !allowed || !!hint;
    $('completion-hint').textContent = hint;
    $('completion-hint').hidden = !hint;
  }
  async function openCase(key) {
    const result = await request('items/' + encodeURIComponent(key)); current = result;
    $('queue').querySelectorAll('[data-key]').forEach(button => {
      button.classList.toggle('selected', button.dataset.key === key);
      button.setAttribute('aria-pressed', String(button.dataset.key === key));
    });
    const item = result.case, review = result.review || {}, editable = result.assignee === queue.actor && review.state !== 'reviewed';
    const state = review.state || 'pending';
    $('case').innerHTML = `<h2>${esc(item.test_case || item.key)}</h2><p class="muted">${esc(item.key)} · ${esc(item.run || '')}</p><span class="badge ${state === 'reviewed' ? 'completed' : esc(state)}">${state === 'reviewed' ? 'Review completed' : esc(labels[state])}</span>` +
      (editable && state === 'pending' ? '<button id="start-review">Start review</button>' : '') +
      `<h3>Test steps</h3><ol class="steps">${(item.steps || []).map(step => `<li><p>${esc(step.description || '')}</p><p>${esc(step.expected || '')}</p><small>${esc(step.status || '')}</small></li>`).join('')}</ol>` +
      '<h3>Evidence</h3><div class="evidence">' + (item.evidence || []).map(evidence => {
        const url = safeURL(evidence.url); if (!url) return '<p>Evidence URL unavailable.</p>';
        const name = esc(evidence.name || 'Evidence');
        const media = String(evidence.type || '').startsWith('image/') ? `<img loading="lazy" src="${esc(url)}" alt="${name}">` : String(evidence.type || '').startsWith('video/') ? `<video controls preload="metadata" src="${esc(url)}"></video>` : String(evidence.type || '').startsWith('audio/') ? `<audio controls preload="metadata" src="${esc(url)}"></audio>` : '';
        return `<figure>${media}<figcaption><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${name}</a></figcaption></figure>`;
      }).join('') + '</div><h3>Findings</h3>' + item.findings.map(finding => {
        const saved = review.decisions?.[finding.id] || {};
        const description = String(finding.description || '').replace(/\btrace_links\b/g, 'linked tickets');
        return `<div class="finding" data-finding="${esc(finding.id)}"><strong>${esc(findingTitle(finding))}</strong><p>${esc(description)}</p><label>Finding decision<select aria-label="Finding decision" ${editable ? '' : 'disabled'}><option value="">Choose a decision</option>${[['confirm','Confirm'],['reject','Reject'],['investigate','Needs investigation']].map(([value,text]) => `<option value="${value}" ${value === saved.decision ? 'selected' : ''}>${text}</option>`).join('')}</select></label><label>Decision reason<textarea aria-label="Decision reason" maxlength="2000" ${editable ? '' : 'disabled'}>${esc(saved.reason || '')}</textarea></label></div>`;
      }).join('') + `<label>Review notes<textarea id="review-notes" maxlength="4000" ${editable ? '' : 'disabled'}>${esc(review.notes || '')}</textarea></label><p id="completion-hint" class="muted"></p><div class="actions"><button id="save-review" ${editable ? '' : 'disabled'}>Save progress</button><button id="investigate-review" ${editable ? '' : 'disabled'}>Needs investigation</button><button id="complete-review" class="primary">Complete case review</button></div><details><summary>Review history</summary><ul>${result.history.map(event => `<li>${esc(event.at)} · ${esc(event.actor)} · ${esc(labels[event.state] || event.action || '')}</li>`).join('')}</ul></details>`;
    if ($('start-review')) $('start-review').onclick = action(() => saveCase('in_progress'));
    $('save-review').onclick = action(() => saveCase('in_progress'));
    $('investigate-review').onclick = action(() => saveCase('investigation'));
    $('complete-review').onclick = action(() => saveCase('reviewed'));
    $('case').querySelectorAll('select,textarea').forEach(input => {input.addEventListener('input', completionState); input.addEventListener('change', completionState);});
    completionState();
  }
  async function saveCase(state) {
    const key = current.case.key;
    const result = await request('items/' + encodeURIComponent(key), {revision:current.case.revision, review_version:current.review_version, decisions:draftDecisions(), notes:$('review-notes').value, state, attested:state === 'reviewed'});
    await loadQueue();
    await openCase(key);
    notice(result.state === 'reviewed' ? `Review completed: ${key}.` : result.state === 'investigation' ? `Marked for investigation: ${key}.` : `Progress saved: ${key}.`);
    if (result.state === 'reviewed') {
      const next = queue.items.find(row => row.key !== key && row.assignee === queue.actor && row.state !== 'reviewed');
      if (next) await openCase(next.key);
    }
  }
  $('preview').onclick = action(async () => {
    const roster = [...$('roster').querySelectorAll('input')].map(input => ({alias:input.value.trim().toLowerCase()})).filter(row => row.alias);
    preview = await request('preview', {roster,version:queue.version,report_revision:queue.report_revision});
    queue.version = preview.version;
    $('allocation-preview').hidden = false;
    $('allocation-preview').innerHTML = '<ul>' + preview.roster.map(row => `<li>${esc(row.name)} (${esc(row.alias)}) · ${Number(preview.counts[row.alias] || 0)} cases</li>`).join('') + '</ul><p>' + esc(preview.explanation) + '</p>' +
      (preview.coverage_missing ? `<p>${preview.coverage_missing} cases are missing execution records and cannot be assigned yet.</p>` : '') +
      (Object.keys(preview.reassignments || {}).length ? `<p>Unfinished cases being reassigned (${Object.keys(preview.reassignments).length})</p>` : '') + '<button id="save-allocation">Save these assignments</button>';
    $('save-allocation').onclick = action(async () => {
      await request('allocate', {preview_id:preview.preview_id,version:queue.version,report_revision:queue.report_revision});
      invalidatePreview(); offset = 0; await loadQueue(true);
      if (current) await openCase(current.case.key);
      notice('Assignments saved.');
    });
  });
  $('add-reviewer').onclick = () => {addReviewer(); invalidatePreview();};
  ['reviewer','run','verdict','state'].forEach(id => {$(id).onchange = action(async () => {offset = 0; await loadQueue();});});
  $('refresh').onclick = action(async () => {invalidatePreview(); offset = 0; await loadQueue(true); if (current) await openCase(current.case.key);});
  $('previous').onclick = action(async () => {offset = Math.max(0, offset - 50); await loadQueue();});
  $('next').onclick = action(async () => {offset += 50; await loadQueue();});
  notice(''); action(() => loadQueue(true))();
})();
